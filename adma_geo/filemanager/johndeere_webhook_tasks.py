"""
Celery tasks for processing John Deere webhook events.

``process_johndeere_event_task`` is the single entry point the webhook view
enqueues. It loads the persisted event row, looks up the correct handler by
``event_type_id``, invokes it, and updates the row's status.

Per-resource handlers (``handle_field_event``, ``handle_boundary_event``,
``handle_field_operation_event``, ``handle_field_deletion``) are filled in by
Tasks 6–8 below. In this task they exist as stubs so the dispatcher can be
tested in isolation.

Event-type strings below are from the JD DSS spec. Verify against a real
sandbox subscription response before production rollout — if JD returns a
different casing or adds new types, update EVENT_TYPE_HANDLERS in one place.
"""
import logging
import sys
from typing import Callable, Dict

import requests
from celery import shared_task
from django.utils import timezone

from .models import JohnDeereWebhookEvent

logger = logging.getLogger(__name__)


def handle_field_event(event: JohnDeereWebhookEvent) -> None:
    """Implemented in Task 6."""
    raise NotImplementedError("handle_field_event implemented in Task 6")


def handle_field_deletion(event: JohnDeereWebhookEvent) -> None:
    """Implemented in Task 6."""
    raise NotImplementedError("handle_field_deletion implemented in Task 6")


def handle_boundary_event(event: JohnDeereWebhookEvent) -> None:
    """Implemented in Task 7."""
    raise NotImplementedError("handle_boundary_event implemented in Task 7")


def handle_field_operation_event(event: JohnDeereWebhookEvent) -> None:
    """Implemented in Task 8."""
    raise NotImplementedError("handle_field_operation_event implemented in Task 8")


# Maps JD event-type strings to the *name* of the handler in this module.
# Using names (not direct function references) lets unittest.mock.patch swap
# the implementation without the dict becoming stale.
EVENT_TYPE_HANDLERS: Dict[str, str] = {
    'fieldCreated': 'handle_field_event',
    'fieldUpdated': 'handle_field_event',
    'fieldArchived': 'handle_field_deletion',
    'fieldDeleted': 'handle_field_deletion',
    'boundaryCreated': 'handle_boundary_event',
    'boundaryUpdated': 'handle_boundary_event',
    'fieldOperationCreated': 'handle_field_operation_event',
    'fieldOperationUpdated': 'handle_field_operation_event',
}

# Keep a reference to this module so callers (and tests) can inspect the
# Callable[...] type from the public dict via getattr at runtime.
_this_module = sys.modules[__name__]


@shared_task(
    bind=True,
    autoretry_for=(requests.RequestException, TimeoutError),
    retry_backoff=True,
    retry_backoff_max=600,
    max_retries=5,
)
def process_johndeere_event_task(self, event_id):
    """
    Dispatcher: load the event, route to the right handler, update status.
    Re-raises handler exceptions so Celery applies the retry/backoff policy.
    """
    try:
        event = JohnDeereWebhookEvent.objects.get(pk=event_id)
    except JohnDeereWebhookEvent.DoesNotExist:
        logger.error("process_johndeere_event_task: event %s not found", event_id)
        return {"event_id": str(event_id), "status": "not_found"}

    event.status = JohnDeereWebhookEvent.STATUS_PROCESSING
    event.processing_started_at = timezone.now()
    event.save(update_fields=['status', 'processing_started_at'])

    handler_name = EVENT_TYPE_HANDLERS.get(event.event_type_id)
    handler: Callable[[JohnDeereWebhookEvent], None] | None = (
        getattr(_this_module, handler_name) if handler_name else None
    )
    if handler is None:
        event.status = JohnDeereWebhookEvent.STATUS_SKIPPED_UNKNOWN_TYPE
        event.processing_completed_at = timezone.now()
        event.save(update_fields=['status', 'processing_completed_at'])
        logger.warning(
            "JD event %s has unknown event_type_id=%r; skipping",
            event.jd_event_id, event.event_type_id,
        )
        return {"event_id": str(event.id), "status": "skipped_unknown_type"}

    try:
        handler(event)
    except Exception as exc:
        # Mark failed; re-raise so Celery's autoretry (if configured for this
        # exception type) handles the retry loop. Each retry attempt starts by
        # resetting status to 'processing' above, so an eventual success still
        # ends at 'completed'. If retries exhaust, the row stays at 'failed'.
        event.error_message = f"{type(exc).__name__}: {exc}"
        event.status = JohnDeereWebhookEvent.STATUS_FAILED
        event.save(update_fields=['status', 'error_message'])
        logger.exception(
            "JD event %s (type=%s) handler failed",
            event.jd_event_id, event.event_type_id,
        )
        raise

    event.status = JohnDeereWebhookEvent.STATUS_COMPLETED
    event.processing_completed_at = timezone.now()
    event.save(update_fields=['status', 'processing_completed_at'])
    return {"event_id": str(event.id), "status": "completed"}
