import base64
import json
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from filemanager.models import JohnDeereWebhookEvent


def basic_auth_header(username, password):
    token = base64.b64encode(f"{username}:{password}".encode('utf-8')).decode('ascii')
    return f"Basic {token}"


@override_settings(
    JD_WEBHOOK_USERNAME='jd_user',
    JD_WEBHOOK_PASSWORD='jd_pass',
    DEBUG=True,  # lets test client POST over HTTP without the HTTPS guard failing
)
class TestWebhookReceiver(TestCase):
    url = '/api/v1/webhooks/johndeere/'
    valid_payload = {
        'eventId': 'evt-001',
        'eventTypeId': 'fieldUpdated',
        'orgId': '4193081',
        'targetResource': 'https://sandboxapi.deere.com/platform/organizations/4193081/fields/F1',
        'timestamp': '2026-04-16T12:00:00Z',
    }

    def test_url_resolves(self):
        self.assertEqual(reverse('api:johndeere_webhook'), self.url)

    def test_missing_auth_returns_401(self):
        resp = self.client.post(
            self.url, data=json.dumps(self.valid_payload), content_type='application/json'
        )
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(JohnDeereWebhookEvent.objects.count(), 0)

    def test_wrong_password_returns_401(self):
        resp = self.client.post(
            self.url,
            data=json.dumps(self.valid_payload),
            content_type='application/json',
            HTTP_AUTHORIZATION=basic_auth_header('jd_user', 'WRONG'),
        )
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(JohnDeereWebhookEvent.objects.count(), 0)

    def test_wrong_username_returns_401(self):
        resp = self.client.post(
            self.url,
            data=json.dumps(self.valid_payload),
            content_type='application/json',
            HTTP_AUTHORIZATION=basic_auth_header('other', 'jd_pass'),
        )
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(JohnDeereWebhookEvent.objects.count(), 0)

    def test_malformed_json_returns_400(self):
        resp = self.client.post(
            self.url,
            data='not json',
            content_type='application/json',
            HTTP_AUTHORIZATION=basic_auth_header('jd_user', 'jd_pass'),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(JohnDeereWebhookEvent.objects.count(), 0)

    def test_missing_event_id_returns_400(self):
        bad = {k: v for k, v in self.valid_payload.items() if k != 'eventId'}
        resp = self.client.post(
            self.url,
            data=json.dumps(bad),
            content_type='application/json',
            HTTP_AUTHORIZATION=basic_auth_header('jd_user', 'jd_pass'),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(JohnDeereWebhookEvent.objects.count(), 0)

    def test_oversized_body_returns_413(self):
        big = dict(self.valid_payload)
        big['junk'] = 'x' * 200_000
        resp = self.client.post(
            self.url,
            data=json.dumps(big),
            content_type='application/json',
            HTTP_AUTHORIZATION=basic_auth_header('jd_user', 'jd_pass'),
        )
        self.assertEqual(resp.status_code, 413)

    @patch('filemanager.johndeere_webhook.process_johndeere_event_task.delay')
    def test_valid_event_acks_200_and_enqueues(self, mock_delay):
        resp = self.client.post(
            self.url,
            data=json.dumps(self.valid_payload),
            content_type='application/json',
            HTTP_AUTHORIZATION=basic_auth_header('jd_user', 'jd_pass'),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(JohnDeereWebhookEvent.objects.count(), 1)
        evt = JohnDeereWebhookEvent.objects.get()
        self.assertEqual(evt.jd_event_id, 'evt-001')
        self.assertEqual(evt.event_type_id, 'fieldUpdated')
        self.assertEqual(evt.org_id, '4193081')
        self.assertEqual(evt.status, 'pending')
        mock_delay.assert_called_once_with(str(evt.id))

    @patch('filemanager.johndeere_webhook.process_johndeere_event_task.delay')
    def test_duplicate_event_marks_skipped_and_does_not_enqueue(self, mock_delay):
        JohnDeereWebhookEvent.objects.create(
            jd_event_id='evt-001',
            event_type_id='fieldUpdated',
            org_id='4193081',
            payload={'eventId': 'evt-001'},
        )
        resp = self.client.post(
            self.url,
            data=json.dumps(self.valid_payload),
            content_type='application/json',
            HTTP_AUTHORIZATION=basic_auth_header('jd_user', 'jd_pass'),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(JohnDeereWebhookEvent.objects.count(), 1)  # no new row
        existing = JohnDeereWebhookEvent.objects.get()
        self.assertEqual(existing.status, 'skipped_duplicate')
        mock_delay.assert_not_called()

    @patch(
        'filemanager.johndeere_webhook.process_johndeere_event_task.delay',
        side_effect=RuntimeError("broker unavailable"),
    )
    def test_celery_dispatch_failure_returns_500_and_leaves_row(self, mock_delay):
        resp = self.client.post(
            self.url,
            data=json.dumps(self.valid_payload),
            content_type='application/json',
            HTTP_AUTHORIZATION=basic_auth_header('jd_user', 'jd_pass'),
        )
        self.assertEqual(resp.status_code, 500)
        # The row was created before the dispatch failed; a JD retry of the same
        # event will hit the duplicate path and mark it skipped_duplicate.
        self.assertEqual(JohnDeereWebhookEvent.objects.count(), 1)
        self.assertEqual(JohnDeereWebhookEvent.objects.get().status, 'pending')

    def test_missing_settings_returns_500(self):
        with self.settings(JD_WEBHOOK_USERNAME=None, JD_WEBHOOK_PASSWORD=None):
            resp = self.client.post(
                self.url,
                data=json.dumps(self.valid_payload),
                content_type='application/json',
                HTTP_AUTHORIZATION=basic_auth_header('jd_user', 'jd_pass'),
            )
            # Without configured credentials, the endpoint is unsafe to accept traffic.
            self.assertEqual(resp.status_code, 500)
