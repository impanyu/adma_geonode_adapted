from unittest.mock import patch

from django.contrib.admin.sites import site
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from filemanager.models import JohnDeereSubscription, JohnDeereWebhookEvent

User = get_user_model()


class TestJohnDeereAdmin(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser('admin', 'a@x.com', 'pw')
        self.client.login(username='admin', password='pw')

    def test_subscription_is_registered(self):
        self.assertIn(JohnDeereSubscription, site._registry)

    def test_event_is_registered(self):
        self.assertIn(JohnDeereWebhookEvent, site._registry)

    def test_event_list_page_loads(self):
        url = reverse('admin:filemanager_johndeerewebhookevent_changelist')
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

    @patch('filemanager.admin.process_johndeere_event_task.delay')
    def test_reprocess_action_reenqueues_task(self, mock_delay):
        evt = JohnDeereWebhookEvent.objects.create(
            jd_event_id='evt-rx',
            event_type_id='fieldUpdated',
            org_id='4193081',
            payload={'eventId': 'evt-rx'},
            status='failed',
        )
        url = reverse('admin:filemanager_johndeerewebhookevent_changelist')
        resp = self.client.post(url, {
            'action': 'reprocess_event',
            '_selected_action': [str(evt.id)],
        })
        self.assertIn(resp.status_code, (200, 302))  # admin redirects on success
        mock_delay.assert_called_once_with(str(evt.id))
        evt.refresh_from_db()
        self.assertEqual(evt.status, 'pending')
