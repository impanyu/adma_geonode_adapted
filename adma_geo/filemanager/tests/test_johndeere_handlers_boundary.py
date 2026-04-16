from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase

from filemanager.johndeere_webhook_tasks import handle_boundary_event
from filemanager.models import File, Folder, JohnDeereWebhookEvent

User = get_user_model()


class TestHandleBoundaryEvent(TestCase):
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
        self.field = Folder.objects.create(
            name='Field One',
            parent=self.root,
            owner=self.user,
            is_third_party=True,
            third_party_source='johndeere',
            third_party_id='F1',
        )
        self.boundary_folder = Folder.objects.create(
            name='boundary',
            parent=self.field,
            owner=self.user,
        )

    def _event(self):
        return JohnDeereWebhookEvent.objects.create(
            jd_event_id='evt-b1',
            event_type_id='boundaryUpdated',
            org_id='4193081',
            target_resource_uri=(
                'https://sandboxapi.deere.com/platform/organizations/'
                '4193081/fields/F1/boundaries'
            ),
            payload={
                'eventId': 'evt-b1',
                'eventTypeId': 'boundaryUpdated',
                'orgId': '4193081',
                'targetResource': (
                    'https://sandboxapi.deere.com/platform/organizations/'
                    '4193081/fields/F1/boundaries'
                ),
            },
        )

    @patch('filemanager.johndeere_webhook_tasks._write_boundary_shapefile')
    @patch('filemanager.johndeere_webhook_tasks._build_jd_client')
    def test_writes_new_boundary_files_for_each_boundary(
        self, mock_client_factory, mock_write
    ):
        mock_client_factory.return_value.get_field_boundaries.return_value = [
            {'id': 'B1', 'name': 'North', 'multipolygons': [{}]},
        ]
        mock_write.return_value = ['North.shp', 'North.shx', 'North.dbf']

        evt = self._event()
        handle_boundary_event(evt)

        mock_client_factory.return_value.get_field_boundaries.assert_called_once_with(
            '4193081', 'F1'
        )
        mock_write.assert_called_once()

    @patch('filemanager.johndeere_webhook_tasks._write_boundary_shapefile',
           return_value=['North.shp'])
    @patch('filemanager.johndeere_webhook_tasks._build_jd_client')
    def test_archives_files_not_in_fresh_response(self, mock_client_factory, _w):
        # pre-existing boundary file that JD no longer returns
        stale = File.objects.create(
            name='OldSouth.shp', folder=self.boundary_folder, owner=self.user,
        )
        mock_client_factory.return_value.get_field_boundaries.return_value = [
            {'id': 'B1', 'name': 'North', 'multipolygons': [{}]},
        ]
        evt = self._event()
        handle_boundary_event(evt)
        stale.refresh_from_db()
        self.assertTrue(stale.is_archived)

    @patch('filemanager.johndeere_webhook_tasks._build_jd_client')
    def test_no_boundary_folder_is_created_if_field_folder_missing(
        self, mock_client_factory
    ):
        self.field.delete()
        mock_client_factory.return_value.get_field_boundaries.return_value = []
        evt = self._event()
        handle_boundary_event(evt)  # must not raise
        # no new folders appeared
        self.assertEqual(Folder.objects.filter(name='boundary').count(), 0)
