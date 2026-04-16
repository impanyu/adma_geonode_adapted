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
from django.conf import settings
from django.utils import timezone

from .johndeere_client import JohnDeereClient
from .models import File, Folder, JohnDeereWebhookEvent

logger = logging.getLogger(__name__)


def _build_jd_client() -> JohnDeereClient:
    """Factory for a configured JohnDeereClient. Kept as a function so tests
    can patch it with one mock."""
    return JohnDeereClient(
        client_id=settings.JD_CLIENT_ID,
        client_secret=settings.JD_CLIENT_SECRET,
        refresh_token=settings.JD_REFRESH_TOKEN,
    )


def _get_jd_root_folder() -> Folder:
    return Folder.objects.get(
        third_party_source='johndeere',
        third_party_id='johndeere_root',
    )


def _parse_field_id_from_uri(uri: str) -> str:
    """
    Given a URI like
        https://.../organizations/{orgId}/fields/{fieldId}[...optional suffix]
    return ``{fieldId}``. Returns '' if the URI doesn't match that shape.
    """
    if not uri:
        return ''
    marker = '/fields/'
    idx = uri.find(marker)
    if idx < 0:
        return ''
    tail = uri[idx + len(marker):]
    return tail.split('/', 1)[0]


def _archive_folder_recursive(folder: Folder) -> None:
    """Mark the folder, all descendant folders, and all descendant files as archived."""
    folder.is_archived = True
    folder.save(update_fields=['is_archived'])
    File.objects.filter(folder=folder).update(is_archived=True)
    for child in folder.subfolders.all():
        _archive_folder_recursive(child)


def handle_field_event(event: JohnDeereWebhookEvent) -> None:
    """Fetch the field from JD and upsert its local Folder."""
    field_id = _parse_field_id_from_uri(event.target_resource_uri or '')
    if not field_id:
        logger.warning("JD event %s: could not parse field id from URI %r",
                       event.jd_event_id, event.target_resource_uri)
        return

    client = _build_jd_client()
    data = client.get_resource_by_link(event.target_resource_uri)
    if data is None:
        logger.info("JD event %s: field %s returned no data; skipping",
                    event.jd_event_id, field_id)
        return

    root = _get_jd_root_folder()
    name = data.get('name') or field_id

    folder, created = Folder.objects.update_or_create(
        third_party_source='johndeere',
        third_party_id=field_id,
        defaults={
            'name': name,
            'parent': root,
            'owner': root.owner,
            'is_public': root.is_public,
            'is_third_party': True,
            'is_archived': False,  # un-archive if previously archived
        },
    )

    event.related_folder = folder
    event.save(update_fields=['related_folder'])
    logger.info("JD event %s: field %s %s", event.jd_event_id,
                field_id, 'created' if created else 'updated')


def handle_field_deletion(event: JohnDeereWebhookEvent) -> None:
    """Soft-delete the field's local Folder and all its descendants."""
    field_id = _parse_field_id_from_uri(event.target_resource_uri or '')
    if not field_id:
        logger.warning("JD event %s: could not parse field id from URI %r",
                       event.jd_event_id, event.target_resource_uri)
        return

    try:
        folder = Folder.objects.get(
            third_party_source='johndeere',
            third_party_id=field_id,
        )
    except Folder.DoesNotExist:
        logger.info("JD event %s: field %s has no local folder; no-op",
                    event.jd_event_id, field_id)
        return

    _archive_folder_recursive(folder)
    event.related_folder = folder
    event.save(update_fields=['related_folder'])


def handle_boundary_event(event: JohnDeereWebhookEvent) -> None:
    """Regenerate shapefile components for the event's field."""
    field_id = _parse_field_id_from_uri(event.target_resource_uri or '')
    if not field_id:
        logger.warning("JD event %s: could not parse field id from URI %r",
                       event.jd_event_id, event.target_resource_uri)
        return

    try:
        field_folder = Folder.objects.get(
            third_party_source='johndeere',
            third_party_id=field_id,
        )
    except Folder.DoesNotExist:
        logger.info(
            "JD event %s: field %s has no local folder; skipping boundary update",
            event.jd_event_id, field_id,
        )
        return

    boundary_folder, _ = Folder.objects.get_or_create(
        name='boundary',
        parent=field_folder,
        owner=field_folder.owner,
        defaults={
            'is_third_party': True,
            'third_party_source': 'johndeere',
            'is_public': field_folder.is_public,
        },
    )

    client = _build_jd_client()
    boundaries = client.get_field_boundaries(event.org_id, field_id)

    fresh_file_names: set = set()
    for boundary in boundaries:
        fresh_file_names.update(
            _write_boundary_shapefile(boundary_folder, boundary)
        )

    # Archive any pre-existing boundary files not in the fresh response.
    File.objects.filter(folder=boundary_folder).exclude(
        name__in=fresh_file_names
    ).update(is_archived=True)

    event.related_folder = boundary_folder
    event.save(update_fields=['related_folder'])
    logger.info(
        "JD event %s: regenerated %d boundary(ies) for field %s",
        event.jd_event_id, len(boundaries), field_id,
    )


def _write_boundary_shapefile(boundary_folder: Folder, boundary: dict) -> list:
    """
    Convert one JD boundary to a shapefile and upsert each component File.

    Returns the list of component filenames written (e.g. ``['North.shp', 'North.shx', ...]``).
    """
    from django.core.files.base import ContentFile

    geojson = JohnDeereClient.boundary_to_geojson(boundary)
    if not geojson:
        return []
    name = (boundary.get('name') or boundary.get('id') or 'boundary').strip().replace(' ', '_')
    components = JohnDeereClient.geojson_to_shapefile_components(geojson, name=name)

    written: list = []
    for filename, blob in components.items():
        existing = File.objects.filter(
            name=filename, folder=boundary_folder
        ).first()
        if existing:
            existing.file.save(filename, ContentFile(blob), save=False)
            existing.file_size = len(blob)
            existing.is_archived = False
            existing.is_spatial = filename.lower().endswith('.shp')
            existing.is_third_party = True
            existing.third_party_source = 'johndeere'
            existing.third_party_id = boundary.get('id') or ''
            existing.save()
        else:
            new_file = File(
                name=filename,
                folder=boundary_folder,
                owner=boundary_folder.owner,
                file_size=len(blob),
                is_public=boundary_folder.is_public,
                is_spatial=filename.lower().endswith('.shp'),
                is_third_party=True,
                third_party_source='johndeere',
                third_party_id=boundary.get('id') or '',
            )
            new_file.file.save(filename, ContentFile(blob), save=False)
            new_file.save()
        written.append(filename)

    return written


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
