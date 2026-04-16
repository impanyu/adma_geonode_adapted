import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from filemanager.johndeere_webhook_tasks import handle_field_operation_event
from filemanager.models import File, Folder, JohnDeereWebhookEvent

User = get_user_model()


class TestHandleFieldOperationEvent(TestCase):
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

    def _event(self, operation_id='OP1'):
        uri = (
            f'https://sandboxapi.deere.com/platform/organizations/4193081'
            f'/fields/F1/fieldOperations/{operation_id}'
        )
        return JohnDeereWebhookEvent.objects.create(
            jd_event_id=f'evt-op-{operation_id}',
            event_type_id='fieldOperationUpdated',
            org_id='4193081',
            target_resource_uri=uri,
            payload={
                'eventId': f'evt-op-{operation_id}',
                'eventTypeId': 'fieldOperationUpdated',
                'orgId': '4193081',
                'targetResource': uri,
            },
        )

    @patch('filemanager.johndeere_webhook_tasks._build_jd_client')
    def test_creates_operation_folder_and_json_file(self, mock_factory):
        mock_factory.return_value.get_resource_by_link.return_value = {
            'id': 'OP1',
            'name': 'Seeding pass',
            'fieldOperationType': 'SEEDING',
        }
        evt = self._event('OP1')
        handle_field_operation_event(evt)

        op_folder = Folder.objects.get(
            third_party_source='johndeere', third_party_id='OP1'
        )
        self.assertEqual(op_folder.parent, self.field)
        self.assertEqual(op_folder.name, 'Seeding pass')

        f = File.objects.get(folder=op_folder, name='fieldOperation.json')
        with f.file.open('rb') as fh:
            data = json.loads(fh.read().decode('utf-8'))
        self.assertEqual(data['id'], 'OP1')
        self.assertEqual(data['fieldOperationType'], 'SEEDING')

    @patch('filemanager.johndeere_webhook_tasks._build_jd_client')
    def test_updates_existing_operation_folder(self, mock_factory):
        Folder.objects.create(
            name='Old op',
            parent=self.field,
            owner=self.user,
            is_third_party=True,
            third_party_source='johndeere',
            third_party_id='OP1',
        )
        mock_factory.return_value.get_resource_by_link.return_value = {
            'id': 'OP1', 'name': 'Renamed op',
        }
        evt = self._event('OP1')
        handle_field_operation_event(evt)

        op_folder = Folder.objects.get(
            third_party_source='johndeere', third_party_id='OP1'
        )
        self.assertEqual(op_folder.name, 'Renamed op')

    @patch('filemanager.johndeere_webhook_tasks._build_jd_client')
    def test_noop_when_field_missing(self, mock_factory):
        self.field.delete()
        mock_factory.return_value.get_resource_by_link.return_value = {
            'id': 'OP1', 'name': 'Op'
        }
        evt = self._event('OP1')
        handle_field_operation_event(evt)  # must not raise
        self.assertFalse(
            Folder.objects.filter(third_party_id='OP1').exists()
        )
