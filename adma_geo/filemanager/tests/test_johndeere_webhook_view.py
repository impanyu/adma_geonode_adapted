from django.test import TestCase
from django.urls import reverse


class TestWebhookURL(TestCase):
    def test_webhook_url_resolves(self):
        url = reverse('api:johndeere_webhook')
        self.assertEqual(url, '/api/v1/webhooks/johndeere/')
