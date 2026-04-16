from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from filemanager.johndeere_webhook_tasks import (
    handle_field_deletion,
    handle_field_event,
)
from filemanager.models import File, Folder, JohnDeereWebhookEvent

User = get_user_model()


class FieldHandlerTestBase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='jd_sync', password='x')
        self.root = Folder.objects.create(
            name='John Deere',
            owner=self.user,
            is_public=True,
            is_third_party=True,
            third_party_source='johndeere',
            third_party_id='johndeere_root',
        )

    def _event(self, event_type_id, target_field_id='F1'):
        return JohnDeereWebhookEvent.objects.create(
            jd_event_id=f'evt-{event_type_id}-{target_field_id}',
            event_type_id=event_type_id,
            org_id='4193081',
            target_resource_uri=(
                f'https://sandboxapi.deere.com/platform/organizations/'
                f'4193081/fields/{target_field_id}'
            ),
            payload={
                'eventId': f'evt-{event_type_id}-{target_field_id}',
                'eventTypeId': event_type_id,
                'orgId': '4193081',
                'targetResource': (
                    f'https://sandboxapi.deere.com/platform/organizations/'
                    f'4193081/fields/{target_field_id}'
                ),
            },
        )


class TestHandleFieldEvent(FieldHandlerTestBase):
    @patch('filemanager.johndeere_webhook_tasks._build_jd_client')
    def test_creates_new_field_folder_on_first_event(self, mock_client_factory):
        mock_client_factory.return_value.get_resource_by_link.return_value = {
            'id': 'F1', 'name': 'North 40',
        }
        evt = self._event('fieldCreated', 'F1')
        handle_field_event(evt)

        new_folder = Folder.objects.get(
            third_party_source='johndeere', third_party_id='F1',
        )
        self.assertEqual(new_folder.name, 'North 40')
        self.assertEqual(new_folder.parent, self.root)
        self.assertFalse(new_folder.is_archived)
        evt.refresh_from_db()
        self.assertEqual(evt.related_folder_id, new_folder.id)

    @patch('filemanager.johndeere_webhook_tasks._build_jd_client')
    def test_updates_existing_field_folder_name(self, mock_client_factory):
        Folder.objects.create(
            name='Old Name',
            parent=self.root,
            owner=self.user,
            is_third_party=True,
            third_party_source='johndeere',
            third_party_id='F1',
        )
        mock_client_factory.return_value.get_resource_by_link.return_value = {
            'id': 'F1', 'name': 'Renamed',
        }
        evt = self._event('fieldUpdated', 'F1')
        handle_field_event(evt)

        folder = Folder.objects.get(
            third_party_source='johndeere', third_party_id='F1',
        )
        self.assertEqual(folder.name, 'Renamed')

    @patch('filemanager.johndeere_webhook_tasks._build_jd_client')
    def test_resource_404_aborts_without_error(self, mock_client_factory):
        mock_client_factory.return_value.get_resource_by_link.return_value = None
        evt = self._event('fieldUpdated', 'F_GONE')
        # No exception, no folder created.
        handle_field_event(evt)
        self.assertFalse(
            Folder.objects.filter(third_party_id='F_GONE').exists()
        )


class TestHandleFieldDeletion(FieldHandlerTestBase):
    def test_soft_deletes_folder_and_all_descendants(self):
        field_folder = Folder.objects.create(
            name='field-to-archive',
            parent=self.root,
            owner=self.user,
            is_third_party=True,
            third_party_source='johndeere',
            third_party_id='F2',
        )
        sub = Folder.objects.create(
            name='boundary', parent=field_folder, owner=self.user,
        )
        f = File.objects.create(
            name='b.shp', folder=sub, owner=self.user,
        )

        evt = self._event('fieldArchived', 'F2')
        handle_field_deletion(evt)

        field_folder.refresh_from_db()
        sub.refresh_from_db()
        f.refresh_from_db()
        self.assertTrue(field_folder.is_archived)
        self.assertTrue(sub.is_archived)
        self.assertTrue(f.is_archived)

    def test_unknown_field_id_is_noop(self):
        evt = self._event('fieldDeleted', 'F_UNKNOWN')
        handle_field_deletion(evt)  # must not raise
