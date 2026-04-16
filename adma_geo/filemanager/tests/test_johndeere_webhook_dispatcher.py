from unittest.mock import patch

from django.test import TestCase

from filemanager.johndeere_webhook_tasks import process_johndeere_event_task
from filemanager.models import JohnDeereWebhookEvent


def _event(event_type_id, payload_extra=None):
    payload = {'eventId': f'evt-{event_type_id}', 'eventTypeId': event_type_id}
    if payload_extra:
        payload.update(payload_extra)
    return JohnDeereWebhookEvent.objects.create(
        jd_event_id=payload['eventId'],
        event_type_id=event_type_id,
        org_id='4193081',
        payload=payload,
    )


class TestDispatcher(TestCase):
    @patch('filemanager.johndeere_webhook_tasks.handle_field_event')
    def test_field_updated_routes_to_field_handler(self, mock_h):
        evt = _event('fieldUpdated')
        result = process_johndeere_event_task(str(evt.id))
        mock_h.assert_called_once()
        self.assertEqual(mock_h.call_args[0][0].id, evt.id)
        evt.refresh_from_db()
        self.assertEqual(evt.status, 'completed')
        self.assertIsNotNone(evt.processing_started_at)
        self.assertIsNotNone(evt.processing_completed_at)
        self.assertEqual(result['status'], 'completed')

    @patch('filemanager.johndeere_webhook_tasks.handle_field_deletion')
    def test_field_archived_routes_to_deletion_handler(self, mock_h):
        evt = _event('fieldArchived')
        process_johndeere_event_task(str(evt.id))
        mock_h.assert_called_once()

    @patch('filemanager.johndeere_webhook_tasks.handle_boundary_event')
    def test_boundary_updated_routes_to_boundary_handler(self, mock_h):
        evt = _event('boundaryUpdated')
        process_johndeere_event_task(str(evt.id))
        mock_h.assert_called_once()

    @patch('filemanager.johndeere_webhook_tasks.handle_field_operation_event')
    def test_field_operation_updated_routes_to_operation_handler(self, mock_h):
        evt = _event('fieldOperationUpdated')
        process_johndeere_event_task(str(evt.id))
        mock_h.assert_called_once()

    def test_unknown_event_type_is_skipped(self):
        evt = _event('someUnmappedThing')
        process_johndeere_event_task(str(evt.id))
        evt.refresh_from_db()
        self.assertEqual(evt.status, 'skipped_unknown_type')

    @patch('filemanager.johndeere_webhook_tasks.handle_field_event',
           side_effect=RuntimeError("boom"))
    def test_handler_exception_marks_failed_and_stores_error(self, mock_h):
        evt = _event('fieldUpdated')
        with self.assertRaises(RuntimeError):
            # bind=True task exposes .apply() for sync invocation; calling the
            # plain function raises so Celery would retry.
            process_johndeere_event_task(str(evt.id))
        evt.refresh_from_db()
        self.assertEqual(evt.status, 'failed')
        self.assertIn('boom', evt.error_message)
