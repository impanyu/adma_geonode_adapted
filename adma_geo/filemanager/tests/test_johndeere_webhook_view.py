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
    JD_ORG_ID='4193081',
    DEBUG=True,  # lets test client POST over HTTP without the HTTPS guard failing
)
class TestWebhookReceiver(TestCase):
    url = '/api/v1/webhooks/johndeere/'
    # Shape taken from the Consumer API samples in JD's Operations Center -
    # Webhook docs: an array, no event id, org carried in `metadata`.
    valid_payload = [{
        'clientKey': 'deere-0123456789',
        'eventTypeId': 'field',
        'targetResource': 'https://sandboxapi.deere.com/platform/organizations/4193081/fields/F1',
        'token': 'sub-token',
        'metadata': [{'key': 'orgId', 'value': '4193081'}],
        'links': [],
    }]

    def post(self, body, username='jd_user', password='jd_pass'):
        return self.client.post(
            self.url,
            data=body if isinstance(body, str) else json.dumps(body),
            content_type='application/json',
            HTTP_AUTHORIZATION=basic_auth_header(username, password),
        )

    def test_url_resolves(self):
        self.assertEqual(reverse('api:johndeere_webhook'), self.url)

    def test_missing_auth_returns_401(self):
        resp = self.client.post(
            self.url, data=json.dumps(self.valid_payload), content_type='application/json'
        )
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(JohnDeereWebhookEvent.objects.count(), 0)

    def test_wrong_password_returns_401(self):
        resp = self.post(self.valid_payload, password='WRONG')
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(JohnDeereWebhookEvent.objects.count(), 0)

    def test_wrong_username_returns_401(self):
        resp = self.post(self.valid_payload, username='other')
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(JohnDeereWebhookEvent.objects.count(), 0)

    def test_malformed_json_returns_400(self):
        resp = self.post('not json')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(JohnDeereWebhookEvent.objects.count(), 0)

    def test_missing_event_type_returns_400(self):
        bad = [{k: v for k, v in self.valid_payload[0].items() if k != 'eventTypeId'}]
        resp = self.post(bad)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(JohnDeereWebhookEvent.objects.count(), 0)

    def test_oversized_body_returns_413(self):
        big = [dict(self.valid_payload[0], junk='x' * 200_000)]
        resp = self.post(big)
        self.assertEqual(resp.status_code, 413)

    @patch('filemanager.johndeere_webhook.process_johndeere_event_task.delay')
    def test_valid_event_acks_204_and_enqueues(self, mock_delay):
        resp = self.post(self.valid_payload)
        # DSS treats anything but 204 as a failed delivery.
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(JohnDeereWebhookEvent.objects.count(), 1)
        evt = JohnDeereWebhookEvent.objects.get()
        self.assertEqual(evt.event_type_id, 'field')
        self.assertEqual(evt.org_id, '4193081')
        self.assertEqual(evt.status, 'pending')
        mock_delay.assert_called_once_with(str(evt.id))

    @patch('filemanager.johndeere_webhook.process_johndeere_event_task.delay')
    def test_batch_stores_every_event(self, mock_delay):
        batch = [
            dict(self.valid_payload[0], eventTypeId='field'),
            dict(self.valid_payload[0], eventTypeId='boundary'),
        ]
        resp = self.post(batch)
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(JohnDeereWebhookEvent.objects.count(), 2)
        self.assertEqual(mock_delay.call_count, 2)

    @patch('filemanager.johndeere_webhook.process_johndeere_event_task.delay')
    def test_subscription_verification_acks_without_storing(self, mock_delay):
        # The probe DSS sends when a subscription is created: it must get a 204
        # back, but there is nothing behind it to fetch or process.
        probe = [{
            'clientKey': 'deere-0123456789',
            'eventTypeId': 'subscriptionVerification',
            'targetResource': '',
            'token': 'sub-token',
            'metadata': [],
            'links': [],
        }]
        resp = self.post(probe)
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(JohnDeereWebhookEvent.objects.count(), 0)
        mock_delay.assert_not_called()

    @patch('filemanager.johndeere_webhook.process_johndeere_event_task.delay')
    def test_org_id_falls_back_to_settings_when_metadata_absent(self, mock_delay):
        no_meta = [{k: v for k, v in self.valid_payload[0].items() if k != 'metadata'}]
        resp = self.post(no_meta)
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(JohnDeereWebhookEvent.objects.get().org_id, '4193081')

    @patch(
        'filemanager.johndeere_webhook.process_johndeere_event_task.delay',
        side_effect=RuntimeError("broker unavailable"),
    )
    def test_celery_dispatch_failure_returns_500_and_leaves_row(self, mock_delay):
        resp = self.post(self.valid_payload)
        # Not a 204, so DSS retries the batch rather than dropping the event.
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(JohnDeereWebhookEvent.objects.count(), 1)
        self.assertEqual(JohnDeereWebhookEvent.objects.get().status, 'pending')

    def test_missing_settings_returns_500(self):
        with self.settings(JD_WEBHOOK_USERNAME=None, JD_WEBHOOK_PASSWORD=None):
            resp = self.post(self.valid_payload)
            # Without configured credentials, the endpoint is unsafe to accept traffic.
            self.assertEqual(resp.status_code, 500)

    @override_settings(DEBUG=False)
    def test_http_in_production_returns_403(self):
        # Django test client's default wsgi_request has scheme='http'; when
        # DEBUG=False, the view should reject before even looking at auth.
        resp = self.post(self.valid_payload)
        self.assertEqual(resp.status_code, 403)
