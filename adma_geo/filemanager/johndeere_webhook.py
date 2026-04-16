"""
John Deere Data Subscription Service webhook receiver.

Flow:
  1. Check HTTP Basic Auth against JD_WEBHOOK_USERNAME/JD_WEBHOOK_PASSWORD.
  2. Parse the JSON payload; require an ``eventId`` field.
  3. Create a JohnDeereWebhookEvent row (unique ``jd_event_id`` gives dedup).
  4. Enqueue process_johndeere_event_task.delay(event.id) and ack 200.

Unknown exceptions return 500 so JD retries. Auth/format errors return 4xx so
JD does NOT retry them.

See docs/superpowers/specs/2026-04-16-john-deere-webhook-design.md §7, §8.
"""
import base64
import binascii
import json
import logging

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.utils.crypto import constant_time_compare
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .johndeere_webhook_tasks import process_johndeere_event_task
from .models import JohnDeereWebhookEvent

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 100_000  # JD DSS payloads are typically <10KB


class WebhookValidationError(Exception):
    """Raised when the incoming payload is malformed."""

    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def _check_basic_auth(request):
    expected_user = getattr(settings, 'JD_WEBHOOK_USERNAME', None)
    expected_pass = getattr(settings, 'JD_WEBHOOK_PASSWORD', None)

    if not expected_user or not expected_pass:
        logger.error("JD webhook received but JD_WEBHOOK_USERNAME/PASSWORD are not set")
        raise WebhookValidationError("webhook auth not configured", status_code=500)

    header = request.META.get('HTTP_AUTHORIZATION', '')
    if not header.startswith('Basic '):
        raise WebhookValidationError("missing or malformed Authorization header",
                                     status_code=401)

    try:
        decoded = base64.b64decode(header[len('Basic '):]).decode('utf-8')
    except (binascii.Error, UnicodeDecodeError):
        raise WebhookValidationError("malformed Basic auth token", status_code=401)

    if ':' not in decoded:
        raise WebhookValidationError("malformed Basic auth token", status_code=401)

    username, password = decoded.split(':', 1)
    user_ok = constant_time_compare(username, expected_user)
    pass_ok = constant_time_compare(password, expected_pass)
    if not (user_ok and pass_ok):
        raise WebhookValidationError("invalid credentials", status_code=401)


def _parse_event(request):
    if len(request.body) > MAX_BODY_BYTES:
        raise WebhookValidationError("payload too large", status_code=413)

    try:
        payload = json.loads(request.body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        raise WebhookValidationError("invalid JSON", status_code=400)

    if not isinstance(payload, dict):
        raise WebhookValidationError("payload must be a JSON object", status_code=400)

    event_id = payload.get('eventId')
    if not event_id or not isinstance(event_id, str):
        raise WebhookValidationError("missing eventId", status_code=400)

    return payload


@csrf_exempt
@require_POST
def johndeere_webhook_receiver(request):
    try:
        _check_basic_auth(request)
        payload = _parse_event(request)
    except WebhookValidationError as err:
        # Auth/format failures — do not retry. Do not log credentials.
        logger.info("JD webhook rejected: %s (status=%s)", err, err.status_code)
        return JsonResponse({"error": str(err)}, status=err.status_code)

    jd_event_id = payload['eventId']
    event_type_id = payload.get('eventTypeId', '') or ''
    # JD events use either 'orgId' or the org id embedded in targetResource; fall back to settings.
    org_id = payload.get('orgId') or getattr(settings, 'JD_ORG_ID', '') or ''
    target_resource_uri = payload.get('targetResource')

    try:
        with transaction.atomic():
            event = JohnDeereWebhookEvent.objects.create(
                jd_event_id=jd_event_id,
                event_type_id=event_type_id,
                org_id=str(org_id),
                target_resource_uri=target_resource_uri,
                payload=payload,
            )
    except IntegrityError:
        # Duplicate delivery — JD must stop retrying.
        # The savepoint is rolled back; outer transaction is still usable.
        JohnDeereWebhookEvent.objects.filter(jd_event_id=jd_event_id).update(
            status=JohnDeereWebhookEvent.STATUS_SKIPPED_DUPLICATE,
        )
        logger.info("JD webhook duplicate event id=%s (skipped)", jd_event_id)
        return JsonResponse({"status": "skipped_duplicate", "event_id": jd_event_id})
    except Exception:
        # DB failure or other — return 500 so JD retries.
        logger.exception("JD webhook DB persistence failed for id=%s", jd_event_id)
        return JsonResponse({"error": "internal error"}, status=500)

    try:
        process_johndeere_event_task.delay(str(event.id))
    except Exception:
        logger.exception("JD webhook Celery dispatch failed for id=%s", jd_event_id)
        return JsonResponse({"error": "internal error"}, status=500)

    return JsonResponse({"status": "accepted", "event_id": str(event.id)})
