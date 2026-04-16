from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import TestCase

from filemanager.models import (
    File,
    Folder,
    JohnDeereSubscription,
    JohnDeereWebhookEvent,
)

User = get_user_model()


class TestIsArchivedFields(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='x')

    def test_folder_is_archived_default_false(self):
        folder = Folder.objects.create(name='f1', owner=self.user)
        self.assertFalse(folder.is_archived)

    def test_file_is_archived_default_false(self):
        folder = Folder.objects.create(name='f2', owner=self.user)
        f = File.objects.create(name='x.txt', folder=folder, owner=self.user)
        self.assertFalse(f.is_archived)


class TestJohnDeereSubscription(TestCase):
    def test_unique_jd_subscription_id(self):
        JohnDeereSubscription.objects.create(
            jd_subscription_id='sub-1',
            org_id='4193081',
            event_type_ids=['fieldCreated'],
            client_endpoint='https://example.test/api/v1/webhooks/johndeere/',
        )
        with self.assertRaises(IntegrityError):
            JohnDeereSubscription.objects.create(
                jd_subscription_id='sub-1',
                org_id='4193081',
                event_type_ids=['fieldUpdated'],
                client_endpoint='https://example.test/api/v1/webhooks/johndeere/',
            )

    def test_defaults(self):
        sub = JohnDeereSubscription.objects.create(
            jd_subscription_id='sub-2',
            org_id='4193081',
            event_type_ids=['fieldCreated'],
            client_endpoint='https://example.test/api/v1/webhooks/johndeere/',
        )
        self.assertTrue(sub.is_active)


class TestJohnDeereWebhookEvent(TestCase):
    def test_unique_jd_event_id(self):
        JohnDeereWebhookEvent.objects.create(
            jd_event_id='evt-1',
            event_type_id='fieldCreated',
            org_id='4193081',
            payload={'eventId': 'evt-1'},
        )
        with self.assertRaises(IntegrityError):
            JohnDeereWebhookEvent.objects.create(
                jd_event_id='evt-1',
                event_type_id='fieldCreated',
                org_id='4193081',
                payload={'eventId': 'evt-1'},
            )

    def test_defaults(self):
        evt = JohnDeereWebhookEvent.objects.create(
            jd_event_id='evt-2',
            event_type_id='fieldUpdated',
            org_id='4193081',
            payload={'eventId': 'evt-2'},
        )
        self.assertEqual(evt.status, 'pending')
        self.assertIsNone(evt.processing_started_at)
