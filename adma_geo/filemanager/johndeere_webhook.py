"""
John Deere Data Subscription Service webhook receiver.

The wire format is fixed by JD's Consumer API (developer.deere.com → Operations
Center - Webhook → Getting Started). Three of its rules drive this module:

  * The body is a JSON *array* of events — DSS batches up to ``maxBatchSize``
    (10 by default) events into one POST.
  * The response must be ``204 No Content``, and must arrive within 5 seconds.
    Anything else counts as a failed delivery: on subscription creation it
    fails validation outright, and afterwards it makes DSS retry and eventually
    expire the subscription.
  * Authentication is a single ``Authorization`` header value configured
    per-client via ``PATCH /eventSubscriptionDelivery``. DSS has no notion of a
    username/password pair on the subscription itself, so we register
    ``Basic base64(user:pass)`` there and check it here.

Flow:
  1. Check HTTP Basic Auth against JD_WEBHOOK_USERNAME/JD_WEBHOOK_PASSWORD.
  2. Parse the JSON array of events.
  3. Ack ``subscriptionVerification`` events without persisting them — they are
     DSS probing the endpoint, not data.
  4. Create a JohnDeereWebhookEvent row per remaining event and enqueue
     process_johndeere_event_task.delay(event.id).
  5. Return 204.

Unknown exceptions return 500 so JD retries. Auth/format errors return 4xx so
JD does NOT retry them.

See docs/superpowers/specs/2026-04-16-john-deere-webhook-design.md §7, §8.
"""
import base64
import binascii
import json
import logging
import uuid

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import HttpResponse, JsonResponse
from django.utils.crypto import constant_time_compare
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .johndeere_webhook_tasks import process_johndeere_event_task
from .models import JohnDeereWebhookEvent

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 100_000  # JD DSS payloads are typically <10KB

VERIFICATION_EVENT_TYPE = 'subscriptionVerification'


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


def _parse_events(request):
    """Return the list of event dicts carried by this request."""
    content_length_header = request.META.get('CONTENT_LENGTH') or '0'
    try:
        content_length = int(content_length_header)
    except (TypeError, ValueError):
        content_length = 0
    if content_length > MAX_BODY_BYTES:
        raise WebhookValidationError("payload too large", status_code=413)
    if len(request.body) > MAX_BODY_BYTES:
        raise WebhookValidationError("payload too large", status_code=413)

    try:
        payload = json.loads(request.body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        raise WebhookValidationError("invalid JSON", status_code=400)

    # DSS always sends an array. A bare object is accepted too so that manual
    # curl probes of this endpoint behave the way people expect.
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        raise WebhookValidationError("payload must be a JSON array", status_code=400)

    for event in payload:
        if not isinstance(event, dict):
            raise WebhookValidationError("each event must be a JSON object",
                                         status_code=400)
        if not event.get('eventTypeId'):
            raise WebhookValidationError("missing eventTypeId", status_code=400)

    return payload


def _org_id_from_event(event):
    """DSS carries the org id in the ``metadata`` key/value list, not at top level."""
    for entry in event.get('metadata') or []:
        if isinstance(entry, dict) and entry.get('key') == 'orgId':
            return str(entry.get('value') or '')
    return str(getattr(settings, 'JD_ORG_ID', '') or '')


def _persist_and_enqueue(event):
    """Store one event and hand it to Celery. Returns False on a DB/dispatch failure."""
    # DSS events carry no id of their own, so we mint one. Re-delivery of the
    # same event therefore creates a second row; the handlers upsert by
    # resource id, so processing an event twice is a no-op, while dropping a
    # genuine second change would lose data.
    jd_event_id = uuid.uuid4().hex
    org_id = _org_id_from_event(event)

    try:
        with transaction.atomic():
            row = JohnDeereWebhookEvent.objects.create(
                jd_event_id=jd_event_id,
                event_type_id=event.get('eventTypeId', ''),
                org_id=org_id,
                target_resource_uri=event.get('targetResource') or None,
                payload=event,
            )
    except IntegrityError:
        logger.info("JD webhook duplicate event id=%s (skipped)", jd_event_id)
        return True
    except Exception:
        logger.exception("JD webhook DB persistence failed")
        return False

    try:
        process_johndeere_event_task.delay(str(row.id))
    except Exception:
        logger.exception("JD webhook Celery dispatch failed for id=%s", jd_event_id)
        return False

    return True


@csrf_exempt
@require_POST
def johndeere_webhook_receiver(request):
    if not request.is_secure() and not settings.DEBUG:
        logger.warning("JD webhook rejected: non-HTTPS in production")
        return JsonResponse({"error": "https required"}, status=403)

    try:
        _check_basic_auth(request)
        events = _parse_events(request)
    except WebhookValidationError as err:
        # Auth/format failures — do not retry. Do not log credentials.
        logger.info("JD webhook rejected: %s (status=%s)", err, err.status_code)
        response = JsonResponse({"error": str(err)}, status=err.status_code)
        if err.status_code == 401:
            response['WWW-Authenticate'] = 'Basic realm="JD Webhook"'
        return response

    for event in events:
        if event.get('eventTypeId') == VERIFICATION_EVENT_TYPE:
            # DSS probing the endpoint during subscription creation. Nothing to
            # store or process; the 204 below is the whole point of the call.
            logger.info("JD webhook: subscriptionVerification acknowledged")
            continue
        if not _persist_and_enqueue(event):
            # Let JD retry the batch rather than silently losing events.
            return JsonResponse({"error": "internal error"}, status=500)

    return HttpResponse(status=204)
