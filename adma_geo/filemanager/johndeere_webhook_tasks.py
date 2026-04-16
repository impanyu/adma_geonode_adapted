"""
Celery tasks for processing John Deere webhook events.

See docs/superpowers/specs/2026-04-16-john-deere-webhook-design.md §7.
"""
import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True)
def process_johndeere_event_task(self, event_id):
    """Placeholder — real behavior added in Task 5."""
    logger.info("process_johndeere_event_task called with event_id=%s", event_id)
    return {"event_id": str(event_id), "status": "not_implemented"}
