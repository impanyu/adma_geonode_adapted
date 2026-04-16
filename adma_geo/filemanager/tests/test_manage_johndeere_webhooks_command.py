from io import StringIO
from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from filemanager.models import JohnDeereSubscription


@override_settings(
    JD_CLIENT_ID='cid',
    JD_CLIENT_SECRET='csec',
    JD_REFRESH_TOKEN='rtok',
    JD_ORG_ID='4193081',
    JD_WEBHOOK_CALLBACK_URL='https://example.test/api/v1/webhooks/johndeere/',
    JD_WEBHOOK_USERNAME='jd_user',
    JD_WEBHOOK_PASSWORD='jd_pass',
)
class TestManageJohnDeereWebhooks(TestCase):
    def _run(self, *args):
        out = StringIO()
        call_command('manage_johndeere_webhooks', *args, stdout=out, stderr=out)
        return out.getvalue()

    @patch('filemanager.management.commands.manage_johndeere_webhooks.JohnDeereClient')
    def test_create_writes_row_only_on_success(self, mock_client_cls):
        mock_client_cls.return_value.create_subscription.return_value = {
            'id': 'SUB-42',
        }
        output = self._run('--create')

        self.assertIn('SUB-42', output)
        sub = JohnDeereSubscription.objects.get(jd_subscription_id='SUB-42')
        self.assertEqual(sub.org_id, '4193081')
        self.assertEqual(
            sub.client_endpoint,
            'https://example.test/api/v1/webhooks/johndeere/',
        )
        mock_client_cls.return_value.create_subscription.assert_called_once()

    @patch('filemanager.management.commands.manage_johndeere_webhooks.JohnDeereClient')
    def test_list_prints_subscriptions(self, mock_client_cls):
        JohnDeereSubscription.objects.create(
            jd_subscription_id='LOCAL-1',
            org_id='4193081',
            event_type_ids=['fieldUpdated'],
            client_endpoint='https://example.test/api/v1/webhooks/johndeere/',
        )
        mock_client_cls.return_value.list_subscriptions.return_value = [
            {'id': 'LOCAL-1', 'scopes': [{'objectId': '4193081'}]},
            {'id': 'ORPHAN', 'scopes': [{'objectId': '4193081'}]},
        ]
        output = self._run('--list')
        self.assertIn('LOCAL-1', output)
        self.assertIn('ORPHAN', output)  # present remotely, not locally

    @patch('filemanager.management.commands.manage_johndeere_webhooks.JohnDeereClient')
    def test_delete_removes_remote_then_local(self, mock_client_cls):
        JohnDeereSubscription.objects.create(
            jd_subscription_id='SUB-X',
            org_id='4193081',
            event_type_ids=['fieldUpdated'],
            client_endpoint='https://example.test/api/v1/webhooks/johndeere/',
        )
        mock_client_cls.return_value.delete_subscription.return_value = True
        self._run('--delete', 'SUB-X')

        mock_client_cls.return_value.delete_subscription.assert_called_once_with('SUB-X')
        self.assertFalse(
            JohnDeereSubscription.objects.filter(jd_subscription_id='SUB-X').exists()
        )

    @override_settings(JD_WEBHOOK_CALLBACK_URL=None)
    def test_create_errors_when_callback_url_unset(self):
        with self.assertRaises(CommandError):
            self._run('--create')

    @override_settings(JD_WEBHOOK_USERNAME=None)
    def test_create_errors_when_auth_unset(self):
        with self.assertRaises(CommandError):
            self._run('--create')
