# John Deere Webhook Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the daily-poll John Deere sync with an event-driven webhook integration backed by John Deere's Data Subscription Service (DSS), inside the existing `filemanager` Django app.

**Architecture:** JD DSS POSTs events to `/api/v1/webhooks/johndeere/` with HTTP Basic Auth; the view dedups on `jd_event_id`, persists every event to `JohnDeereWebhookEvent`, and acks 200. A Celery dispatcher task routes events by `event_type_id` to per-resource handlers (`handle_field_event`, `handle_boundary_event`, `handle_field_operation_event`, `handle_field_deletion`) that do targeted refreshes via the existing `JohnDeereClient`. Subscription lifecycle is managed by a new management command plus a `JohnDeereSubscription` model visible in Django admin.

**Tech Stack:** Django 4.x, Celery, PostgreSQL, requests, pytest via `manage.py test` (Django test runner), existing `JohnDeereClient`, GeoServer (existing integration via `bundle_and_publish_shapefile`).

**Reference spec:** `docs/superpowers/specs/2026-04-16-john-deere-webhook-design.md`.

---

## Working directory

All paths below are relative to the repo root `adma_geonode_project/`. The Django project lives at `adma_geo/`; run Django commands with `cd adma_geo` first (or inside a running container as the README shows).

## Testing conventions

The project uses Django's built-in test runner (`python manage.py test`). Tests live alongside the app at `adma_geo/filemanager/tests/`. Create that directory if it doesn't exist (the existing repo has no `tests/` yet — this is fine; Django's default test discovery picks up `test_*.py` files anywhere inside the app). Import paths below assume `adma_geo/` is the project root on `PYTHONPATH` (which it is under `manage.py`).

Run **one** test at a time with:
```bash
cd adma_geo
python manage.py test filemanager.tests.test_johndeere_webhook_view.TestWebhookReceiver.test_missing_auth_returns_401 -v 2
```
Run **all** new tests with:
```bash
cd adma_geo
python manage.py test filemanager.tests -v 2
```

---

## Task 1: Scaffold new files and URL route (skeleton, no behavior)

**Files:**
- Create: `adma_geo/filemanager/tests/__init__.py`
- Create: `adma_geo/filemanager/tests/test_johndeere_webhook_view.py`
- Create: `adma_geo/filemanager/johndeere_webhook.py`
- Create: `adma_geo/filemanager/johndeere_webhook_tasks.py`
- Modify: `adma_geo/filemanager/api_urls.py`

- [ ] **Step 1.1: Create empty test package**

Create `adma_geo/filemanager/tests/__init__.py` with no content (empty file). This enables Django test discovery.

- [ ] **Step 1.2: Write a failing URL-resolution test**

Create `adma_geo/filemanager/tests/test_johndeere_webhook_view.py`:

```python
from django.test import TestCase
from django.urls import reverse


class TestWebhookURL(TestCase):
    def test_webhook_url_resolves(self):
        url = reverse('api:johndeere_webhook')
        self.assertEqual(url, '/api/v1/webhooks/johndeere/')
```

- [ ] **Step 1.3: Run test to verify it fails**

```bash
cd adma_geo
python manage.py test filemanager.tests.test_johndeere_webhook_view.TestWebhookURL -v 2
```
Expected: `NoReverseMatch: Reverse for 'johndeere_webhook' not found`.

- [ ] **Step 1.4: Create empty view module**

Create `adma_geo/filemanager/johndeere_webhook.py`:

```python
"""
John Deere Data Subscription Service webhook receiver.

Authenticates incoming events via HTTP Basic Auth, dedups on the JD event id,
persists the event, acks 200, and dispatches to a Celery task for processing.
"""
import logging

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

logger = logging.getLogger(__name__)


@csrf_exempt
@require_POST
def johndeere_webhook_receiver(request):
    """Placeholder — real behavior added in Task 3."""
    return JsonResponse({"status": "not_implemented"}, status=501)
```

- [ ] **Step 1.5: Create empty tasks module**

Create `adma_geo/filemanager/johndeere_webhook_tasks.py`:

```python
"""
Celery tasks for processing John Deere webhook events.

See docs/superpowers/specs/2026-04-16-john-deere-webhook-design.md §7.
"""
import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True)
def process_johndeere_event_task(self, event_id):
    """Placeholder — real behavior added in Task 5."""
    logger.info("process_johndeere_event_task called with event_id=%s", event_id)
    return {"event_id": str(event_id), "status": "not_implemented"}
```

- [ ] **Step 1.6: Wire the URL route**

Modify `adma_geo/filemanager/api_urls.py`. Add the import near the top and the route at the end of `urlpatterns`:

```python
#!/usr/bin/env python3

"""
Token-based API URLs for file management.
"""

from django.urls import path
from . import api_views
from . import johndeere_webhook

app_name = 'api'

urlpatterns = [
    # ... (keep all existing entries unchanged)

    # John Deere webhook (Basic Auth, not token auth)
    path(
        'webhooks/johndeere/',
        johndeere_webhook.johndeere_webhook_receiver,
        name='johndeere_webhook',
    ),
]
```

Keep every pre-existing route in the file exactly as it was; only add the `import` and append the new `path(...)` entry.

- [ ] **Step 1.7: Run test to verify it passes**

```bash
cd adma_geo
python manage.py test filemanager.tests.test_johndeere_webhook_view.TestWebhookURL -v 2
```
Expected: `OK`.

- [ ] **Step 1.8: Commit**

```bash
cd ..  # back to repo root
git add adma_geo/filemanager/tests/__init__.py \
        adma_geo/filemanager/tests/test_johndeere_webhook_view.py \
        adma_geo/filemanager/johndeere_webhook.py \
        adma_geo/filemanager/johndeere_webhook_tasks.py \
        adma_geo/filemanager/api_urls.py
git commit -m "Scaffold John Deere webhook view, tasks module, and URL route"
```

---

## Task 2: Data model — `is_archived` on Folder/File + two new JD webhook models

**Files:**
- Modify: `adma_geo/filemanager/models.py`
- Create: `adma_geo/filemanager/migrations/0017_johndeere_webhook_models.py` (generated)
- Create: `adma_geo/filemanager/tests/test_johndeere_webhook_models.py`

- [ ] **Step 2.1: Write failing model tests**

Create `adma_geo/filemanager/tests/test_johndeere_webhook_models.py`:

```python
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
```

- [ ] **Step 2.2: Run test to verify it fails**

```bash
cd adma_geo
python manage.py test filemanager.tests.test_johndeere_webhook_models -v 2
```
Expected: `ImportError: cannot import name 'JohnDeereSubscription'` (or similar).

- [ ] **Step 2.3: Add `is_archived` field to `Folder` and `File`**

In `adma_geo/filemanager/models.py`:

Inside the `Folder` class definition, alongside the other boolean flags (next to `is_public` or `deletion_in_progress`), add:

```python
    is_archived = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Soft-delete flag. When True, folder is hidden from normal listings.",
    )
```

Inside the `File` class definition, alongside the other boolean flags, add:

```python
    is_archived = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Soft-delete flag. When True, file is hidden from normal listings.",
    )
```

- [ ] **Step 2.4: Add the two new models to `models.py`**

At the bottom of `adma_geo/filemanager/models.py` (after all existing model classes), append:

```python
class JohnDeereSubscription(models.Model):
    """
    Tracks a John Deere Data Subscription Service subscription created by our app.

    One row per active subscription. Remote id returned by JD at creation time
    is stored in ``jd_subscription_id`` and is the key for remote CRUD.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    jd_subscription_id = models.CharField(max_length=128, unique=True)
    org_id = models.CharField(max_length=64, db_index=True)
    event_type_ids = models.JSONField(default=list)
    client_endpoint = models.URLField(max_length=500)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"JD subscription {self.jd_subscription_id} (org={self.org_id})"


class JohnDeereWebhookEvent(models.Model):
    """Every JD webhook event we receive, for dedup + audit."""

    STATUS_PENDING = 'pending'
    STATUS_PROCESSING = 'processing'
    STATUS_COMPLETED = 'completed'
    STATUS_FAILED = 'failed'
    STATUS_SKIPPED_DUPLICATE = 'skipped_duplicate'
    STATUS_SKIPPED_UNKNOWN_TYPE = 'skipped_unknown_type'

    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_PROCESSING, 'Processing'),
        (STATUS_COMPLETED, 'Completed'),
        (STATUS_FAILED, 'Failed'),
        (STATUS_SKIPPED_DUPLICATE, 'Skipped (duplicate)'),
        (STATUS_SKIPPED_UNKNOWN_TYPE, 'Skipped (unknown type)'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    jd_event_id = models.CharField(max_length=128, unique=True)
    event_type_id = models.CharField(max_length=64, db_index=True)
    org_id = models.CharField(max_length=64, db_index=True)
    target_resource_uri = models.URLField(max_length=1000, null=True, blank=True)
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)
    payload = models.JSONField()
    status = models.CharField(
        max_length=32, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True
    )
    processing_started_at = models.DateTimeField(null=True, blank=True)
    processing_completed_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(null=True, blank=True)
    related_folder = models.ForeignKey(
        'Folder', null=True, blank=True, on_delete=models.SET_NULL, related_name='+'
    )
    related_file = models.ForeignKey(
        'File', null=True, blank=True, on_delete=models.SET_NULL, related_name='+'
    )

    class Meta:
        ordering = ['-received_at']

    def __str__(self):
        return f"{self.event_type_id} {self.jd_event_id} [{self.status}]"
```

- [ ] **Step 2.5: Generate the migration**

```bash
cd adma_geo
python manage.py makemigrations filemanager --name johndeere_webhook_models
```

Expected: creates `adma_geo/filemanager/migrations/0017_johndeere_webhook_models.py`. Open it and sanity-check — it should contain `AddField` for `is_archived` on both Folder and File, plus `CreateModel` for the two new models. No other changes should appear.

- [ ] **Step 2.6: Apply the migration in test DB and run tests**

```bash
cd adma_geo
python manage.py test filemanager.tests.test_johndeere_webhook_models -v 2
```
Expected: all 6 assertions `OK`.

- [ ] **Step 2.7: Commit**

```bash
cd ..
git add adma_geo/filemanager/models.py \
        adma_geo/filemanager/migrations/0017_johndeere_webhook_models.py \
        adma_geo/filemanager/tests/test_johndeere_webhook_models.py
git commit -m "Add is_archived flag and JohnDeere webhook models"
```

---

## Task 3: Webhook view — Basic Auth, dedup, persistence, ack

**Files:**
- Modify: `adma_geo/filemanager/johndeere_webhook.py`
- Modify: `adma_geo/filemanager/tests/test_johndeere_webhook_view.py`
- Modify: `adma_geo/adma_geo/settings.py` (add 3 env-driven settings)

### 3a. Add settings

- [ ] **Step 3.1: Add env-driven webhook settings**

In `adma_geo/adma_geo/settings.py`, find the "John Deere API Configuration" block (around line 165) and append the three new settings so the block becomes:

```python
# John Deere API Configuration
# Credentials should be set via environment variables
JD_CLIENT_ID = os.environ.get('JD_CLIENT_ID')
JD_CLIENT_SECRET = os.environ.get('JD_CLIENT_SECRET')
JD_REFRESH_TOKEN = os.environ.get('JD_REFRESH_TOKEN')
JD_ORG_ID = os.environ.get('JD_ORG_ID', '4193081')  # Default organization ID

# John Deere Webhook (Data Subscription Service)
# The URL John Deere will POST events to, and the Basic Auth creds JD will use.
JD_WEBHOOK_CALLBACK_URL = os.environ.get('JD_WEBHOOK_CALLBACK_URL')
JD_WEBHOOK_USERNAME = os.environ.get('JD_WEBHOOK_USERNAME')
JD_WEBHOOK_PASSWORD = os.environ.get('JD_WEBHOOK_PASSWORD')
```

### 3b. Write the full view test suite (failing)

- [ ] **Step 3.2: Replace the placeholder URL test with the full suite**

Replace the entire contents of `adma_geo/filemanager/tests/test_johndeere_webhook_view.py` with:

```python
import base64
import json
from unittest.mock import patch

from django.test import Client, TestCase, override_settings
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
```

- [ ] **Step 3.3: Run the test file to verify it fails**

```bash
cd adma_geo
python manage.py test filemanager.tests.test_johndeere_webhook_view -v 2
```
Expected: multiple failures (every test except `test_url_resolves` fails because the view still returns 501).

### 3c. Implement the view

- [ ] **Step 3.4: Replace the placeholder view with the real implementation**

Replace the entire contents of `adma_geo/filemanager/johndeere_webhook.py` with:

```python
"""
John Deere Data Subscription Service webhook receiver.

Flow:
  1. Check HTTP Basic Auth against JD_WEBHOOK_USERNAME/JD_WEBHOOK_PASSWORD.
  2. Parse the JSON payload; require an ``eventId`` field.
  3. Create a JohnDeereWebhookEvent row (unique ``jd_event_id`` gives dedup).
  4. Enqueue process_johndeere_event_task.delay(event.id) and ack 200.

Unknown exceptions return 500 so JD retries. Auth/format errors return 4xx so
JD does NOT retry them.

See docs/superpowers/specs/2026-04-16-john-deere-webhook-design.md §7, §8.
"""
import base64
import binascii
import json
import logging

from django.conf import settings
from django.db import IntegrityError
from django.http import JsonResponse
from django.utils.crypto import constant_time_compare
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .johndeere_webhook_tasks import process_johndeere_event_task
from .models import JohnDeereWebhookEvent

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 100_000  # JD DSS payloads are typically <10KB


class WebhookValidationError(Exception):
    """Raised when the incoming payload is malformed."""

    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def _check_basic_auth(request):
    expected_user = getattr(settings, 'JD_WEBHOOK_USERNAME', None)
    expected_pass = getattr(settings, 'JD_WEBHOOK_PASSWORD', None)

    if not expected_user or not expected_pass:
        logger.error("JD webhook received but JD_WEBHOOK_USERNAME/PASSWORD are not set")
        raise WebhookValidationError("webhook auth not configured", status_code=500)

    header = request.META.get('HTTP_AUTHORIZATION', '')
    if not header.startswith('Basic '):
        raise WebhookValidationError("missing or malformed Authorization header",
                                     status_code=401)

    try:
        decoded = base64.b64decode(header[len('Basic '):]).decode('utf-8')
    except (binascii.Error, UnicodeDecodeError):
        raise WebhookValidationError("malformed Basic auth token", status_code=401)

    if ':' not in decoded:
        raise WebhookValidationError("malformed Basic auth token", status_code=401)

    username, password = decoded.split(':', 1)
    user_ok = constant_time_compare(username, expected_user)
    pass_ok = constant_time_compare(password, expected_pass)
    if not (user_ok and pass_ok):
        raise WebhookValidationError("invalid credentials", status_code=401)


def _parse_event(request):
    if len(request.body) > MAX_BODY_BYTES:
        raise WebhookValidationError("payload too large", status_code=413)

    try:
        payload = json.loads(request.body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        raise WebhookValidationError("invalid JSON", status_code=400)

    if not isinstance(payload, dict):
        raise WebhookValidationError("payload must be a JSON object", status_code=400)

    event_id = payload.get('eventId')
    if not event_id or not isinstance(event_id, str):
        raise WebhookValidationError("missing eventId", status_code=400)

    return payload


@csrf_exempt
@require_POST
def johndeere_webhook_receiver(request):
    try:
        _check_basic_auth(request)
        payload = _parse_event(request)
    except WebhookValidationError as err:
        # Auth/format failures — do not retry. Do not log credentials.
        logger.info("JD webhook rejected: %s (status=%s)", err, err.status_code)
        return JsonResponse({"error": str(err)}, status=err.status_code)

    jd_event_id = payload['eventId']
    event_type_id = payload.get('eventTypeId', '') or ''
    # JD events use either 'orgId' or the org id embedded in targetResource; fall back to settings.
    org_id = payload.get('orgId') or getattr(settings, 'JD_ORG_ID', '') or ''
    target_resource_uri = payload.get('targetResource')

    try:
        event = JohnDeereWebhookEvent.objects.create(
            jd_event_id=jd_event_id,
            event_type_id=event_type_id,
            org_id=str(org_id),
            target_resource_uri=target_resource_uri,
            payload=payload,
        )
    except IntegrityError:
        # Duplicate delivery — JD must stop retrying.
        JohnDeereWebhookEvent.objects.filter(jd_event_id=jd_event_id).update(
            status=JohnDeereWebhookEvent.STATUS_SKIPPED_DUPLICATE,
        )
        logger.info("JD webhook duplicate event id=%s (skipped)", jd_event_id)
        return JsonResponse({"status": "skipped_duplicate", "event_id": jd_event_id})
    except Exception:
        # DB failure or other — return 500 so JD retries.
        logger.exception("JD webhook DB persistence failed for id=%s", jd_event_id)
        return JsonResponse({"error": "internal error"}, status=500)

    try:
        process_johndeere_event_task.delay(str(event.id))
    except Exception:
        logger.exception("JD webhook Celery dispatch failed for id=%s", jd_event_id)
        return JsonResponse({"error": "internal error"}, status=500)

    return JsonResponse({"status": "accepted", "event_id": str(event.id)})
```

- [ ] **Step 3.5: Run the view tests — expect all green**

```bash
cd adma_geo
python manage.py test filemanager.tests.test_johndeere_webhook_view -v 2
```
Expected: 10 assertions, all `OK`.

- [ ] **Step 3.6: Commit**

```bash
cd ..
git add adma_geo/filemanager/johndeere_webhook.py \
        adma_geo/filemanager/tests/test_johndeere_webhook_view.py \
        adma_geo/adma_geo/settings.py
git commit -m "Implement John Deere webhook receiver with Basic Auth and dedup"
```

---

## Task 4: Extend `JohnDeereClient` with subscription CRUD

**Files:**
- Modify: `adma_geo/filemanager/johndeere_client.py`
- Create: `adma_geo/filemanager/tests/test_johndeere_client_subscriptions.py`

- [ ] **Step 4.1: Write failing client tests**

Create `adma_geo/filemanager/tests/test_johndeere_client_subscriptions.py`:

```python
from unittest.mock import Mock, patch

from django.test import TestCase

from filemanager.johndeere_client import JohnDeereClient


class TestSubscriptionClient(TestCase):
    def setUp(self):
        self.client = JohnDeereClient('cid', 'csec', 'refresh')
        self.client.access_token = 'token'  # skip refresh

    @patch.object(JohnDeereClient, '_make_request')
    def test_create_subscription_posts_correct_body(self, mock_req):
        mock_req.return_value = Mock(
            status_code=201,
            json=lambda: {'id': 'SUB-123'},
            text='',
        )
        result = self.client.create_subscription(
            client_endpoint='https://example.test/api/v1/webhooks/johndeere/',
            username='u',
            password='p',
            event_type_ids=['fieldCreated', 'fieldUpdated'],
            org_id='4193081',
        )
        self.assertEqual(result['id'], 'SUB-123')
        args, kwargs = mock_req.call_args
        self.assertEqual(args[0], 'POST')
        self.assertEqual(args[1], '/eventSubscriptions')
        body = kwargs['json']
        self.assertEqual(body['clientEndpoint']['uri'],
                         'https://example.test/api/v1/webhooks/johndeere/')
        self.assertEqual(body['clientEndpoint']['username'], 'u')
        self.assertEqual(body['clientEndpoint']['password'], 'p')
        self.assertEqual(
            sorted(t for t in body['eventTypeIds']),
            ['fieldCreated', 'fieldUpdated'],
        )
        self.assertEqual(body['scopes'][0]['objectType'], 'organization')
        self.assertEqual(body['scopes'][0]['objectId'], '4193081')

    @patch.object(JohnDeereClient, '_make_request')
    def test_list_subscriptions_handles_pagination(self, mock_req):
        page1 = Mock(
            status_code=200,
            json=lambda: {
                'values': [{'id': 'A'}, {'id': 'B'}],
                'links': [
                    {'rel': 'nextPage',
                     'uri': 'https://sandboxapi.deere.com/platform/eventSubscriptions?offset=2'}
                ],
            },
            text='',
        )
        page2 = Mock(
            status_code=200,
            json=lambda: {'values': [{'id': 'C'}], 'links': []},
            text='',
        )
        mock_req.side_effect = [page1, page2]

        result = self.client.list_subscriptions()
        self.assertEqual([s['id'] for s in result], ['A', 'B', 'C'])

    @patch.object(JohnDeereClient, '_make_request')
    def test_delete_subscription_204_returns_true(self, mock_req):
        mock_req.return_value = Mock(status_code=204, text='')
        self.assertTrue(self.client.delete_subscription('SUB-1'))
        mock_req.assert_called_once_with('DELETE', '/eventSubscriptions/SUB-1')

    @patch.object(JohnDeereClient, '_make_request')
    def test_delete_subscription_404_returns_true(self, mock_req):
        mock_req.return_value = Mock(status_code=404, text='not found')
        self.assertTrue(self.client.delete_subscription('SUB-1'))

    @patch.object(JohnDeereClient, '_make_request')
    def test_delete_subscription_other_returns_false(self, mock_req):
        mock_req.return_value = Mock(status_code=500, text='boom')
        self.assertFalse(self.client.delete_subscription('SUB-1'))

    @patch.object(JohnDeereClient, '_make_request')
    def test_get_resource_by_link_strips_base_url(self, mock_req):
        mock_req.return_value = Mock(
            status_code=200,
            json=lambda: {'id': 'F1'},
            text='',
        )
        # URI starting with the sandbox base URL
        uri = 'https://sandboxapi.deere.com/platform/organizations/4193081/fields/F1'
        data = self.client.get_resource_by_link(uri)
        self.assertEqual(data['id'], 'F1')
        args, _ = mock_req.call_args
        self.assertEqual(args[0], 'GET')
        self.assertEqual(args[1], '/organizations/4193081/fields/F1')

    @patch.object(JohnDeereClient, '_make_request')
    def test_get_resource_by_link_returns_none_on_error(self, mock_req):
        mock_req.return_value = Mock(status_code=404, text='not found')
        uri = 'https://sandboxapi.deere.com/platform/organizations/4193081/fields/F1'
        self.assertIsNone(self.client.get_resource_by_link(uri))
```

- [ ] **Step 4.2: Run tests — expect failures (methods don't exist yet)**

```bash
cd adma_geo
python manage.py test filemanager.tests.test_johndeere_client_subscriptions -v 2
```
Expected: `AttributeError: 'JohnDeereClient' object has no attribute 'create_subscription'` (and similar for the others).

- [ ] **Step 4.3: Add the four methods to `JohnDeereClient`**

In `adma_geo/filemanager/johndeere_client.py`, **inside the `JohnDeereClient` class** (before the `@staticmethod` block at the end), append these methods:

```python
    def create_subscription(
        self,
        client_endpoint: str,
        username: str,
        password: str,
        event_type_ids: List[str],
        org_id: str,
    ) -> Dict[str, Any]:
        """
        POST /eventSubscriptions — create a new webhook subscription.

        Returns the parsed JSON body (the new subscription, including ``id``).
        """
        body = {
            'clientEndpoint': {
                'uri': client_endpoint,
                'username': username,
                'password': password,
            },
            'eventTypeIds': list(event_type_ids),
            'scopes': [
                {'objectType': 'organization', 'objectId': str(org_id)},
            ],
        }
        response = self._make_request('POST', '/eventSubscriptions', json=body)
        if response.status_code not in (200, 201):
            raise Exception(
                f"Failed to create subscription: {response.status_code} - {response.text}"
            )
        return response.json()

    def list_subscriptions(self) -> List[Dict[str, Any]]:
        """GET /eventSubscriptions with pagination handling."""
        endpoint = '/eventSubscriptions'
        all_subs: List[Dict[str, Any]] = []
        while endpoint:
            response = self._make_request('GET', endpoint)
            if response.status_code != 200:
                logger.error(
                    "Failed to list subscriptions: %s - %s",
                    response.status_code, response.text,
                )
                break
            data = response.json()
            all_subs.extend(data.get('values', []))
            next_uri = None
            for link in data.get('links', []):
                if link.get('rel') == 'nextPage':
                    next_uri = link.get('uri')
                    break
            if next_uri:
                endpoint = next_uri.replace(self.API_BASE_URL, '')
            else:
                endpoint = None
        return all_subs

    def delete_subscription(self, subscription_id: str) -> bool:
        """DELETE /eventSubscriptions/{id}. Returns True on 204 or 404, else False."""
        response = self._make_request('DELETE', f'/eventSubscriptions/{subscription_id}')
        if response.status_code in (204, 200, 404):
            return True
        logger.error(
            "Failed to delete subscription %s: %s - %s",
            subscription_id, response.status_code, response.text,
        )
        return False

    def get_resource_by_link(self, uri: str) -> Optional[Dict[str, Any]]:
        """
        Follow an absolute URI returned in an event's ``targetResource`` field
        and return the parsed JSON. Returns None on non-200.
        """
        if uri.startswith(self.API_BASE_URL):
            endpoint = uri[len(self.API_BASE_URL):]
        else:
            endpoint = uri
        response = self._make_request('GET', endpoint)
        if response.status_code != 200:
            logger.error(
                "Failed to fetch resource %s: %s - %s",
                uri, response.status_code, response.text,
            )
            return None
        return response.json()
```

- [ ] **Step 4.4: Run client tests — expect green**

```bash
cd adma_geo
python manage.py test filemanager.tests.test_johndeere_client_subscriptions -v 2
```
Expected: 7 assertions, all `OK`.

- [ ] **Step 4.5: Commit**

```bash
cd ..
git add adma_geo/filemanager/johndeere_client.py \
        adma_geo/filemanager/tests/test_johndeere_client_subscriptions.py
git commit -m "Add subscription CRUD and generic link fetch to JohnDeereClient"
```

---

## Task 5: Dispatcher task + event-type routing (handlers stubbed)

**Files:**
- Modify: `adma_geo/filemanager/johndeere_webhook_tasks.py`
- Create: `adma_geo/filemanager/tests/test_johndeere_webhook_dispatcher.py`

- [ ] **Step 5.1: Write failing dispatcher tests**

Create `adma_geo/filemanager/tests/test_johndeere_webhook_dispatcher.py`:

```python
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
```

- [ ] **Step 5.2: Run tests — expect failures**

```bash
cd adma_geo
python manage.py test filemanager.tests.test_johndeere_webhook_dispatcher -v 2
```
Expected: failures — the handler names don't exist yet and the task is a placeholder.

- [ ] **Step 5.3: Replace the placeholder tasks module with the dispatcher + stubbed handlers**

Replace the contents of `adma_geo/filemanager/johndeere_webhook_tasks.py` with:

```python
"""
Celery tasks for processing John Deere webhook events.

``process_johndeere_event_task`` is the single entry point the webhook view
enqueues. It loads the persisted event row, looks up the correct handler by
``event_type_id``, invokes it, and updates the row's status.

Per-resource handlers (``handle_field_event``, ``handle_boundary_event``,
``handle_field_operation_event``, ``handle_field_deletion``) are filled in by
Tasks 6–8 below. In this task they exist as stubs so the dispatcher can be
tested in isolation.

Event-type strings below are from the JD DSS spec. Verify against a real
sandbox subscription response before production rollout — if JD returns a
different casing or adds new types, update EVENT_TYPE_HANDLERS in one place.
"""
import logging
from typing import Callable, Dict

import requests
from celery import shared_task
from django.utils import timezone

from .models import JohnDeereWebhookEvent

logger = logging.getLogger(__name__)


def handle_field_event(event: JohnDeereWebhookEvent) -> None:
    """Implemented in Task 6."""
    raise NotImplementedError("handle_field_event implemented in Task 6")


def handle_field_deletion(event: JohnDeereWebhookEvent) -> None:
    """Implemented in Task 6."""
    raise NotImplementedError("handle_field_deletion implemented in Task 6")


def handle_boundary_event(event: JohnDeereWebhookEvent) -> None:
    """Implemented in Task 7."""
    raise NotImplementedError("handle_boundary_event implemented in Task 7")


def handle_field_operation_event(event: JohnDeereWebhookEvent) -> None:
    """Implemented in Task 8."""
    raise NotImplementedError("handle_field_operation_event implemented in Task 8")


EVENT_TYPE_HANDLERS: Dict[str, Callable[[JohnDeereWebhookEvent], None]] = {
    'fieldCreated': handle_field_event,
    'fieldUpdated': handle_field_event,
    'fieldArchived': handle_field_deletion,
    'fieldDeleted': handle_field_deletion,
    'boundaryCreated': handle_boundary_event,
    'boundaryUpdated': handle_boundary_event,
    'fieldOperationCreated': handle_field_operation_event,
    'fieldOperationUpdated': handle_field_operation_event,
}


@shared_task(
    bind=True,
    autoretry_for=(requests.RequestException, TimeoutError),
    retry_backoff=True,
    retry_backoff_max=600,
    max_retries=5,
)
def process_johndeere_event_task(self, event_id):
    """
    Dispatcher: load the event, route to the right handler, update status.
    Re-raises handler exceptions so Celery applies the retry/backoff policy.
    """
    try:
        event = JohnDeereWebhookEvent.objects.get(pk=event_id)
    except JohnDeereWebhookEvent.DoesNotExist:
        logger.error("process_johndeere_event_task: event %s not found", event_id)
        return {"event_id": str(event_id), "status": "not_found"}

    event.status = JohnDeereWebhookEvent.STATUS_PROCESSING
    event.processing_started_at = timezone.now()
    event.save(update_fields=['status', 'processing_started_at'])

    handler = EVENT_TYPE_HANDLERS.get(event.event_type_id)
    if handler is None:
        event.status = JohnDeereWebhookEvent.STATUS_SKIPPED_UNKNOWN_TYPE
        event.processing_completed_at = timezone.now()
        event.save(update_fields=['status', 'processing_completed_at'])
        logger.warning(
            "JD event %s has unknown event_type_id=%r; skipping",
            event.jd_event_id, event.event_type_id,
        )
        return {"event_id": str(event.id), "status": "skipped_unknown_type"}

    try:
        handler(event)
    except Exception as exc:
        # Mark failed; re-raise so Celery's autoretry (if configured for this
        # exception type) handles the retry loop. Each retry attempt starts by
        # resetting status to 'processing' above, so an eventual success still
        # ends at 'completed'. If retries exhaust, the row stays at 'failed'.
        event.error_message = f"{type(exc).__name__}: {exc}"
        event.status = JohnDeereWebhookEvent.STATUS_FAILED
        event.save(update_fields=['status', 'error_message'])
        logger.exception(
            "JD event %s (type=%s) handler failed",
            event.jd_event_id, event.event_type_id,
        )
        raise

    event.status = JohnDeereWebhookEvent.STATUS_COMPLETED
    event.processing_completed_at = timezone.now()
    event.save(update_fields=['status', 'processing_completed_at'])
    return {"event_id": str(event.id), "status": "completed"}
```

- [ ] **Step 5.4: Run dispatcher tests — expect green**

```bash
cd adma_geo
python manage.py test filemanager.tests.test_johndeere_webhook_dispatcher -v 2
```
Expected: 6 assertions, all `OK`.

- [ ] **Step 5.5: Run the full test module so far to make sure nothing regressed**

```bash
cd adma_geo
python manage.py test filemanager.tests -v 2
```
Expected: all green (models + view + client + dispatcher tests).

- [ ] **Step 5.6: Commit**

```bash
cd ..
git add adma_geo/filemanager/johndeere_webhook_tasks.py \
        adma_geo/filemanager/tests/test_johndeere_webhook_dispatcher.py
git commit -m "Add John Deere webhook dispatcher with event-type routing"
```

---

## Task 6: Field handlers (`handle_field_event`, `handle_field_deletion`)

**Files:**
- Modify: `adma_geo/filemanager/johndeere_webhook_tasks.py`
- Create: `adma_geo/filemanager/tests/test_johndeere_handlers_field.py`

### Context for the handler

The JD "John Deere" root `Folder` is created by `setup_johndeere` management command (`third_party_source='johndeere'`, `third_party_id='johndeere_root'`). Each field is a subfolder directly under it, identified by `third_party_id=<field_id>`. Handlers must upsert by that pair.

- [ ] **Step 6.1: Write failing tests**

Create `adma_geo/filemanager/tests/test_johndeere_handlers_field.py`:

```python
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
```

- [ ] **Step 6.2: Run tests — expect failures**

```bash
cd adma_geo
python manage.py test filemanager.tests.test_johndeere_handlers_field -v 2
```
Expected: `NotImplementedError` for both handlers.

- [ ] **Step 6.3: Implement the field handlers**

In `adma_geo/filemanager/johndeere_webhook_tasks.py`, replace the placeholder `handle_field_event` and `handle_field_deletion` functions with real implementations, and add the helper `_build_jd_client`. The **resulting** tasks file should have these functions above the `EVENT_TYPE_HANDLERS` dict:

```python
from django.conf import settings
from django.contrib.auth import get_user_model

from .johndeere_client import JohnDeereClient
from .models import File, Folder


def _build_jd_client() -> JohnDeereClient:
    """Factory for a configured JohnDeereClient. Kept as a function so tests
    can patch it with one mock."""
    return JohnDeereClient(
        client_id=settings.JD_CLIENT_ID,
        client_secret=settings.JD_CLIENT_SECRET,
        refresh_token=settings.JD_REFRESH_TOKEN,
    )


def _get_jd_root_folder() -> Folder:
    return Folder.objects.get(
        third_party_source='johndeere',
        third_party_id='johndeere_root',
    )


def _parse_field_id_from_uri(uri: str) -> str:
    """
    Given a URI like
        https://.../organizations/{orgId}/fields/{fieldId}[...optional suffix]
    return ``{fieldId}``. Returns '' if the URI doesn't match that shape.
    """
    if not uri:
        return ''
    marker = '/fields/'
    idx = uri.find(marker)
    if idx < 0:
        return ''
    tail = uri[idx + len(marker):]
    return tail.split('/', 1)[0]


def handle_field_event(event: JohnDeereWebhookEvent) -> None:
    """Fetch the field from JD and upsert its local Folder."""
    field_id = _parse_field_id_from_uri(event.target_resource_uri or '')
    if not field_id:
        logger.warning("JD event %s: could not parse field id from URI %r",
                       event.jd_event_id, event.target_resource_uri)
        return

    client = _build_jd_client()
    data = client.get_resource_by_link(event.target_resource_uri)
    if data is None:
        logger.info("JD event %s: field %s returned no data; skipping",
                    event.jd_event_id, field_id)
        return

    root = _get_jd_root_folder()
    name = data.get('name') or field_id

    folder, created = Folder.objects.update_or_create(
        third_party_source='johndeere',
        third_party_id=field_id,
        defaults={
            'name': name,
            'parent': root,
            'owner': root.owner,
            'is_public': root.is_public,
            'is_third_party': True,
            'is_archived': False,  # un-archive if previously archived
        },
    )

    event.related_folder = folder
    event.save(update_fields=['related_folder'])
    logger.info("JD event %s: field %s %s", event.jd_event_id,
                field_id, 'created' if created else 'updated')


def handle_field_deletion(event: JohnDeereWebhookEvent) -> None:
    """Soft-delete the field's local Folder and all its descendants."""
    field_id = _parse_field_id_from_uri(event.target_resource_uri or '')
    if not field_id:
        logger.warning("JD event %s: could not parse field id from URI %r",
                       event.jd_event_id, event.target_resource_uri)
        return

    try:
        folder = Folder.objects.get(
            third_party_source='johndeere',
            third_party_id=field_id,
        )
    except Folder.DoesNotExist:
        logger.info("JD event %s: field %s has no local folder; no-op",
                    event.jd_event_id, field_id)
        return

    _archive_folder_recursive(folder)
    event.related_folder = folder
    event.save(update_fields=['related_folder'])


def _archive_folder_recursive(folder: Folder) -> None:
    """Mark the folder, all descendant folders, and all descendant files as archived."""
    folder.is_archived = True
    folder.save(update_fields=['is_archived'])
    File.objects.filter(folder=folder).update(is_archived=True)
    for child in folder.subfolders.all():
        _archive_folder_recursive(child)
```

Remove the two `raise NotImplementedError` stubs for these functions (but keep the `handle_boundary_event` and `handle_field_operation_event` stubs as-is for now — Tasks 7 and 8 replace them).

The Django `get_user_model` import is unused by this task but kept above since later tasks (admin) may need it; remove if your linter complains — no functional impact.

- [ ] **Step 6.4: Run field handler tests — expect green**

```bash
cd adma_geo
python manage.py test filemanager.tests.test_johndeere_handlers_field -v 2
```
Expected: 5 assertions, all `OK`.

- [ ] **Step 6.5: Also re-run dispatcher tests to confirm no regression**

```bash
cd adma_geo
python manage.py test filemanager.tests.test_johndeere_webhook_dispatcher -v 2
```
Expected: still green.

- [ ] **Step 6.6: Commit**

```bash
cd ..
git add adma_geo/filemanager/johndeere_webhook_tasks.py \
        adma_geo/filemanager/tests/test_johndeere_handlers_field.py
git commit -m "Implement field event and field deletion handlers"
```

---

## Task 7: Boundary handler (`handle_boundary_event`)

**Files:**
- Modify: `adma_geo/filemanager/johndeere_webhook_tasks.py`
- Create: `adma_geo/filemanager/tests/test_johndeere_handlers_boundary.py`

### Context

A field's boundary geometry is stored locally as a shapefile (`.shp` + `.shx` + `.dbf` + `.prj` + `.cpg`) inside a `boundary` subfolder under the field's folder. `JohnDeereClient.boundary_to_geojson` already converts JD's boundary JSON to a GeoJSON feature; `JohnDeereClient.geojson_to_shapefile_components` produces the per-extension bytes. The existing bulk sync calls both and writes the components through Django `FileField`.

The handler regenerates the shapefile for the event's field. Any boundary files whose names don't correspond to a boundary in the fresh JD response are marked `is_archived=True`.

- [ ] **Step 7.1: Write failing tests**

Create `adma_geo/filemanager/tests/test_johndeere_handlers_boundary.py`:

```python
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
```

- [ ] **Step 7.2: Run — expect failures**

```bash
cd adma_geo
python manage.py test filemanager.tests.test_johndeere_handlers_boundary -v 2
```
Expected: `NotImplementedError` for `handle_boundary_event`.

- [ ] **Step 7.3: Implement the boundary handler**

In `adma_geo/filemanager/johndeere_webhook_tasks.py`, replace the `handle_boundary_event` placeholder with:

```python
def handle_boundary_event(event: JohnDeereWebhookEvent) -> None:
    """Regenerate shapefile components for the event's field."""
    field_id = _parse_field_id_from_uri(event.target_resource_uri or '')
    if not field_id:
        logger.warning("JD event %s: could not parse field id from URI %r",
                       event.jd_event_id, event.target_resource_uri)
        return

    try:
        field_folder = Folder.objects.get(
            third_party_source='johndeere',
            third_party_id=field_id,
        )
    except Folder.DoesNotExist:
        logger.info(
            "JD event %s: field %s has no local folder; skipping boundary update",
            event.jd_event_id, field_id,
        )
        return

    boundary_folder, _ = Folder.objects.get_or_create(
        name='boundary',
        parent=field_folder,
        owner=field_folder.owner,
        defaults={
            'is_third_party': True,
            'third_party_source': 'johndeere',
            'is_public': field_folder.is_public,
        },
    )

    client = _build_jd_client()
    boundaries = client.get_field_boundaries(event.org_id, field_id)

    fresh_file_names: set = set()
    for boundary in boundaries:
        fresh_file_names.update(
            _write_boundary_shapefile(boundary_folder, boundary)
        )

    # Archive any pre-existing boundary files not in the fresh response.
    File.objects.filter(folder=boundary_folder).exclude(
        name__in=fresh_file_names
    ).update(is_archived=True)

    event.related_folder = boundary_folder
    event.save(update_fields=['related_folder'])
    logger.info(
        "JD event %s: regenerated %d boundary(ies) for field %s",
        event.jd_event_id, len(boundaries), field_id,
    )


def _write_boundary_shapefile(boundary_folder: Folder, boundary: dict) -> list:
    """
    Convert one JD boundary to a shapefile and upsert each component File.

    Returns the list of component filenames written (e.g. ``['North.shp', 'North.shx', ...]``).
    """
    from django.core.files.base import ContentFile

    geojson = JohnDeereClient.boundary_to_geojson(boundary)
    if not geojson:
        return []
    name = (boundary.get('name') or boundary.get('id') or 'boundary').strip().replace(' ', '_')
    components = JohnDeereClient.geojson_to_shapefile_components(geojson, name=name)

    written: list = []
    for filename, blob in components.items():
        existing = File.objects.filter(
            name=filename, folder=boundary_folder
        ).first()
        if existing:
            existing.file.save(filename, ContentFile(blob), save=False)
            existing.file_size = len(blob)
            existing.is_archived = False
            existing.is_spatial = filename.lower().endswith('.shp')
            existing.is_third_party = True
            existing.third_party_source = 'johndeere'
            existing.third_party_id = boundary.get('id') or ''
            existing.save()
        else:
            new_file = File(
                name=filename,
                folder=boundary_folder,
                owner=boundary_folder.owner,
                file_size=len(blob),
                is_public=boundary_folder.is_public,
                is_spatial=filename.lower().endswith('.shp'),
                is_third_party=True,
                third_party_source='johndeere',
                third_party_id=boundary.get('id') or '',
            )
            new_file.file.save(filename, ContentFile(blob), save=False)
            new_file.save()
        written.append(filename)

    return written
```

- [ ] **Step 7.4: Run boundary tests — expect green**

```bash
cd adma_geo
python manage.py test filemanager.tests.test_johndeere_handlers_boundary -v 2
```
Expected: 3 assertions, all `OK`.

- [ ] **Step 7.5: Commit**

```bash
cd ..
git add adma_geo/filemanager/johndeere_webhook_tasks.py \
        adma_geo/filemanager/tests/test_johndeere_handlers_boundary.py
git commit -m "Implement boundary event handler with shapefile regeneration"
```

---

## Task 8: Field operation handler (`handle_field_operation_event`)

**Files:**
- Modify: `adma_geo/filemanager/johndeere_webhook_tasks.py`
- Create: `adma_geo/filemanager/tests/test_johndeere_handlers_operation.py`

### Context

For v1 we do not mirror the fine-grained per-operation folder logic from `sync_johndeere_task` (which is ~500 lines and mixes many concerns). Instead, the handler fetches the operation, upserts a simple `Folder` under the matching field's folder named by operation id, and stores the JSON response as a `fieldOperation.json` file inside that folder. This gives us a reliable event-driven record; the richer shapefile-per-operation-layer logic can be ported from `sync_johndeere_task` later without changing the public interface.

- [ ] **Step 8.1: Write failing tests**

Create `adma_geo/filemanager/tests/test_johndeere_handlers_operation.py`:

```python
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
```

- [ ] **Step 8.2: Run — expect failures**

```bash
cd adma_geo
python manage.py test filemanager.tests.test_johndeere_handlers_operation -v 2
```
Expected: `NotImplementedError`.

- [ ] **Step 8.3: Implement the handler**

In `adma_geo/filemanager/johndeere_webhook_tasks.py`, replace `handle_field_operation_event`:

```python
def handle_field_operation_event(event: JohnDeereWebhookEvent) -> None:
    """Upsert a folder for the field operation, and a JSON record file."""
    import json
    from django.core.files.base import ContentFile

    uri = event.target_resource_uri or ''
    field_id = _parse_field_id_from_uri(uri)
    operation_id = _parse_operation_id_from_uri(uri)
    if not operation_id:
        logger.warning("JD event %s: could not parse operation id from URI %r",
                       event.jd_event_id, uri)
        return

    try:
        field_folder = Folder.objects.get(
            third_party_source='johndeere',
            third_party_id=field_id,
        )
    except Folder.DoesNotExist:
        logger.info(
            "JD event %s: field %s has no local folder; skipping operation update",
            event.jd_event_id, field_id,
        )
        return

    client = _build_jd_client()
    data = client.get_resource_by_link(uri)
    if data is None:
        logger.info(
            "JD event %s: operation %s returned no data; skipping",
            event.jd_event_id, operation_id,
        )
        return

    name = data.get('name') or operation_id

    op_folder, _ = Folder.objects.update_or_create(
        third_party_source='johndeere',
        third_party_id=operation_id,
        defaults={
            'name': name,
            'parent': field_folder,
            'owner': field_folder.owner,
            'is_public': field_folder.is_public,
            'is_third_party': True,
            'is_archived': False,
        },
    )

    blob = json.dumps(data, indent=2).encode('utf-8')
    existing = File.objects.filter(
        folder=op_folder, name='fieldOperation.json'
    ).first()
    if existing:
        existing.file.save('fieldOperation.json', ContentFile(blob), save=False)
        existing.file_size = len(blob)
        existing.is_archived = False
        existing.save()
        record = existing
    else:
        record = File(
            name='fieldOperation.json',
            folder=op_folder,
            owner=op_folder.owner,
            file_size=len(blob),
            mime_type='application/json',
            is_public=op_folder.is_public,
            is_third_party=True,
            third_party_source='johndeere',
            third_party_id=operation_id,
        )
        record.file.save('fieldOperation.json', ContentFile(blob), save=False)
        record.save()

    event.related_folder = op_folder
    event.related_file = record
    event.save(update_fields=['related_folder', 'related_file'])


def _parse_operation_id_from_uri(uri: str) -> str:
    """
    Given a URI like
        https://.../fields/{fieldId}/fieldOperations/{opId}
    return ``{opId}``. Returns '' if missing.
    """
    if not uri:
        return ''
    marker = '/fieldOperations/'
    idx = uri.find(marker)
    if idx < 0:
        return ''
    tail = uri[idx + len(marker):]
    return tail.split('/', 1)[0]
```

- [ ] **Step 8.4: Run — expect green**

```bash
cd adma_geo
python manage.py test filemanager.tests.test_johndeere_handlers_operation -v 2
```
Expected: 3 assertions, all `OK`.

- [ ] **Step 8.5: Run the full webhook-related suite**

```bash
cd adma_geo
python manage.py test filemanager.tests -v 2
```
Expected: all green (models + view + client + dispatcher + field handler + boundary handler + operation handler).

- [ ] **Step 8.6: Commit**

```bash
cd ..
git add adma_geo/filemanager/johndeere_webhook_tasks.py \
        adma_geo/filemanager/tests/test_johndeere_handlers_operation.py
git commit -m "Implement field operation event handler"
```

---

## Task 9: Management command `manage_johndeere_webhooks`

**Files:**
- Create: `adma_geo/filemanager/management/commands/manage_johndeere_webhooks.py`
- Create: `adma_geo/filemanager/tests/test_manage_johndeere_webhooks_command.py`

- [ ] **Step 9.1: Write failing command tests**

Create `adma_geo/filemanager/tests/test_manage_johndeere_webhooks_command.py`:

```python
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
```

- [ ] **Step 9.2: Run — expect failures**

```bash
cd adma_geo
python manage.py test filemanager.tests.test_manage_johndeere_webhooks_command -v 2
```
Expected: `Unknown command: 'manage_johndeere_webhooks'`.

- [ ] **Step 9.3: Implement the command**

Create `adma_geo/filemanager/management/commands/manage_johndeere_webhooks.py`:

```python
"""
Manage John Deere Data Subscription Service subscriptions.

Usage:
    python manage.py manage_johndeere_webhooks --create
    python manage.py manage_johndeere_webhooks --list
    python manage.py manage_johndeere_webhooks --show <sub_id>
    python manage.py manage_johndeere_webhooks --delete <sub_id>

Reads configuration from Django settings (env-driven):
    JD_CLIENT_ID, JD_CLIENT_SECRET, JD_REFRESH_TOKEN, JD_ORG_ID
    JD_WEBHOOK_CALLBACK_URL, JD_WEBHOOK_USERNAME, JD_WEBHOOK_PASSWORD
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from filemanager.johndeere_client import JohnDeereClient
from filemanager.models import JohnDeereSubscription


DEFAULT_EVENT_TYPE_IDS = [
    'fieldCreated', 'fieldUpdated', 'fieldArchived', 'fieldDeleted',
    'boundaryCreated', 'boundaryUpdated',
    'fieldOperationCreated', 'fieldOperationUpdated',
]


class Command(BaseCommand):
    help = 'Create, list, or delete John Deere DSS subscriptions.'

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument('--create', action='store_true',
                           help='Create a new subscription using settings values.')
        group.add_argument('--list', action='store_true',
                           help='List all subscriptions (local + remote).')
        group.add_argument('--show', metavar='SUB_ID',
                           help='Show one subscription by JD subscription id.')
        group.add_argument('--delete', metavar='SUB_ID',
                           help='Delete a subscription remotely and locally.')

    def handle(self, *args, **options):
        client = self._build_client()

        if options['create']:
            self._create(client)
        elif options['list']:
            self._list(client)
        elif options['show']:
            self._show(client, options['show'])
        elif options['delete']:
            self._delete(client, options['delete'])

    def _build_client(self):
        for key in ('JD_CLIENT_ID', 'JD_CLIENT_SECRET', 'JD_REFRESH_TOKEN'):
            if not getattr(settings, key, None):
                raise CommandError(f"{key} is not set")
        return JohnDeereClient(
            client_id=settings.JD_CLIENT_ID,
            client_secret=settings.JD_CLIENT_SECRET,
            refresh_token=settings.JD_REFRESH_TOKEN,
        )

    def _require_webhook_settings(self):
        missing = [
            k for k in ('JD_WEBHOOK_CALLBACK_URL',
                        'JD_WEBHOOK_USERNAME',
                        'JD_WEBHOOK_PASSWORD')
            if not getattr(settings, k, None)
        ]
        if missing:
            raise CommandError(
                "Missing required settings: " + ", ".join(missing)
            )

    def _create(self, client):
        self._require_webhook_settings()
        callback_url = settings.JD_WEBHOOK_CALLBACK_URL
        self.stdout.write(f"Creating subscription → {callback_url}")

        result = client.create_subscription(
            client_endpoint=callback_url,
            username=settings.JD_WEBHOOK_USERNAME,
            password=settings.JD_WEBHOOK_PASSWORD,
            event_type_ids=DEFAULT_EVENT_TYPE_IDS,
            org_id=settings.JD_ORG_ID,
        )
        jd_sub_id = result.get('id')
        if not jd_sub_id:
            raise CommandError(f"JD response missing id: {result}")

        JohnDeereSubscription.objects.create(
            jd_subscription_id=jd_sub_id,
            org_id=str(settings.JD_ORG_ID),
            event_type_ids=DEFAULT_EVENT_TYPE_IDS,
            client_endpoint=callback_url,
        )
        self.stdout.write(self.style.SUCCESS(
            f"Subscription created: {jd_sub_id}"
        ))

    def _list(self, client):
        remote = client.list_subscriptions()
        local = {
            s.jd_subscription_id: s
            for s in JohnDeereSubscription.objects.all()
        }
        self.stdout.write(f"Remote subscriptions: {len(remote)}")
        for sub in remote:
            sid = sub.get('id', '?')
            has_local = 'LOCAL' if sid in local else 'REMOTE-ONLY'
            self.stdout.write(f"  [{has_local}] {sid}")
        orphan_local = [sid for sid in local if not any(
            r.get('id') == sid for r in remote
        )]
        for sid in orphan_local:
            self.stdout.write(f"  [LOCAL-ONLY] {sid}")

    def _show(self, client, subscription_id):
        remote = client.list_subscriptions()
        for sub in remote:
            if sub.get('id') == subscription_id:
                self.stdout.write(str(sub))
                return
        raise CommandError(f"Subscription {subscription_id} not found remotely")

    def _delete(self, client, subscription_id):
        ok = client.delete_subscription(subscription_id)
        if not ok:
            raise CommandError(
                f"Remote delete failed for {subscription_id}; aborting local delete"
            )
        deleted = JohnDeereSubscription.objects.filter(
            jd_subscription_id=subscription_id
        ).delete()
        self.stdout.write(self.style.SUCCESS(
            f"Deleted {subscription_id} (local rows removed: {deleted[0]})"
        ))
```

- [ ] **Step 9.4: Run command tests — expect green**

```bash
cd adma_geo
python manage.py test filemanager.tests.test_manage_johndeere_webhooks_command -v 2
```
Expected: 5 assertions, all `OK`.

- [ ] **Step 9.5: Commit**

```bash
cd ..
git add adma_geo/filemanager/management/commands/manage_johndeere_webhooks.py \
        adma_geo/filemanager/tests/test_manage_johndeere_webhooks_command.py
git commit -m "Add manage_johndeere_webhooks management command"
```

---

## Task 10: Admin registration + "Reprocess event" action

**Files:**
- Modify: `adma_geo/filemanager/admin.py`
- Create: `adma_geo/filemanager/tests/test_johndeere_admin.py`

- [ ] **Step 10.1: Write failing admin tests**

Create `adma_geo/filemanager/tests/test_johndeere_admin.py`:

```python
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
```

- [ ] **Step 10.2: Run — expect failures**

```bash
cd adma_geo
python manage.py test filemanager.tests.test_johndeere_admin -v 2
```
Expected: `AssertionError: ... not in site._registry`.

- [ ] **Step 10.3: Register models + add action**

In `adma_geo/filemanager/admin.py`, append to the bottom of the file:

```python
from .johndeere_webhook_tasks import process_johndeere_event_task
from .models import JohnDeereSubscription, JohnDeereWebhookEvent


@admin.register(JohnDeereSubscription)
class JohnDeereSubscriptionAdmin(admin.ModelAdmin):
    list_display = ('jd_subscription_id', 'org_id', 'is_active',
                    'client_endpoint', 'created_at')
    list_filter = ('is_active', 'org_id')
    readonly_fields = ('id', 'created_at', 'updated_at')
    search_fields = ('jd_subscription_id', 'org_id')


@admin.register(JohnDeereWebhookEvent)
class JohnDeereWebhookEventAdmin(admin.ModelAdmin):
    list_display = ('jd_event_id', 'event_type_id', 'org_id',
                    'status', 'received_at', 'processing_completed_at')
    list_filter = ('status', 'event_type_id', 'org_id')
    search_fields = ('jd_event_id', 'target_resource_uri')
    readonly_fields = (
        'id', 'jd_event_id', 'event_type_id', 'org_id',
        'target_resource_uri', 'payload', 'received_at',
        'processing_started_at', 'processing_completed_at',
        'related_folder', 'related_file',
    )
    ordering = ('-received_at',)
    actions = ['reprocess_event']

    @admin.action(description='Re-enqueue selected events for processing')
    def reprocess_event(self, request, queryset):
        count = 0
        for evt in queryset:
            evt.status = JohnDeereWebhookEvent.STATUS_PENDING
            evt.error_message = None
            evt.processing_started_at = None
            evt.processing_completed_at = None
            evt.save(update_fields=[
                'status', 'error_message',
                'processing_started_at', 'processing_completed_at',
            ])
            process_johndeere_event_task.delay(str(evt.id))
            count += 1
        self.message_user(request, f"Re-enqueued {count} event(s).")
```

- [ ] **Step 10.4: Run admin tests — expect green**

```bash
cd adma_geo
python manage.py test filemanager.tests.test_johndeere_admin -v 2
```
Expected: 4 assertions, all `OK`.

- [ ] **Step 10.5: Commit**

```bash
cd ..
git add adma_geo/filemanager/admin.py \
        adma_geo/filemanager/tests/test_johndeere_admin.py
git commit -m "Register JD webhook models in admin with reprocess action"
```

---

## Task 11: Hide archived items from listing views/templates

**Files (read first, then touch minimally):**
- Modify: `adma_geo/filemanager/views.py`
- Modify: `adma_geo/filemanager/api_views.py`
- Modify: `adma_geo/filemanager/templates/filemanager/public_home.html`
- Create: `adma_geo/filemanager/tests/test_archived_visibility.py`

> **Important:** do not aggressively re-filter every queryset in the codebase. We only need to hide archived items from the *list* surfaces a normal user sees. Admin still shows them. Downloads by UUID still work. Use Grep to scan the list views before editing — the three files above are the expected scope.

- [ ] **Step 11.1: Write failing visibility tests**

Create `adma_geo/filemanager/tests/test_archived_visibility.py`:

```python
from django.contrib.auth import get_user_model
from django.test import TestCase

from filemanager.models import File, Folder

User = get_user_model()


class TestArchivedVisibility(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='u', password='x')
        self.client.force_login(self.user)
        self.live = Folder.objects.create(name='live', owner=self.user)
        self.dead = Folder.objects.create(
            name='dead', owner=self.user, is_archived=True,
        )
        File.objects.create(name='ok.txt', owner=self.user, folder=self.live)
        File.objects.create(
            name='gone.txt', owner=self.user, folder=self.live, is_archived=True,
        )

    def test_api_list_folders_excludes_archived(self):
        resp = self.client.get('/api/v1/folders/')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'live')
        self.assertNotContains(resp, 'dead')

    def test_api_list_files_excludes_archived(self):
        resp = self.client.get('/api/v1/files/')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'ok.txt')
        self.assertNotContains(resp, 'gone.txt')

    def test_dashboard_stats_counts_only_non_archived(self):
        resp = self.client.get('/api/dashboard/stats/')
        self.assertEqual(resp.status_code, 200)
        # The exact JSON key names differ by codebase; the invariant is that
        # the archived folder/file names do not appear in the counted response.
        self.assertNotContains(resp, 'dead')
        self.assertNotContains(resp, 'gone.txt')
```

- [ ] **Step 11.2: Run — expect failures**

```bash
cd adma_geo
python manage.py test filemanager.tests.test_archived_visibility -v 2
```
Expected: failures — archived items are currently still listed/counted.

- [ ] **Step 11.3: Find and update the three view functions**

The three endpoints the tests exercise are:

- `/api/dashboard/stats/` → `dashboard_stats` in `adma_geo/filemanager/views.py`
- `/api/v1/folders/` → `api_list_folders` in `adma_geo/filemanager/api_views.py`
- `/api/v1/files/` → `api_list_files` in `adma_geo/filemanager/api_views.py`

For each function:

1. Read it. Identify every `Folder.objects.filter(...)` / `File.objects.filter(...)` / `.subfolders.all()` / `.files.all()` queryset that contributes to the count or list the endpoint returns.
2. Add `is_archived=False` to that filter (pass as a kwarg to `filter(...)`, or chain `.filter(is_archived=False)` after an existing filter).
3. Do **not** touch detail views (`folder_detail`, `file_detail`, `download_file`) — direct access by UUID must continue to work so admin can recover archived items.

**Example transformation.** If the original code reads:

```python
def api_list_folders(request):
    folders = Folder.objects.filter(owner=request.user)
    return JsonResponse({'folders': [{'id': str(f.id), 'name': f.name} for f in folders]})
```

Change it to:

```python
def api_list_folders(request):
    folders = Folder.objects.filter(owner=request.user, is_archived=False)
    return JsonResponse({'folders': [{'id': str(f.id), 'name': f.name} for f in folders]})
```

Apply the same pattern to the other two functions. The failing tests from Step 11.1 assert the necessary behavior — iterate until they pass.

- [ ] **Step 11.4: Guard the John Deere sections of the public listing template**

In `adma_geo/filemanager/templates/filemanager/public_home.html`, the file has four spots (around lines 274, 307, 337, 364 in the current version) where the template iterates over third-party folders/files and renders a John Deere icon. Wrap the **body** of each of those iterations in `{% if not folder.is_archived %}...{% endif %}` (or `{% if not file.is_archived %}...{% endif %}` for files) so archived items are skipped. Grep for `third_party_source == 'johndeere'` to locate the exact blocks.

- [ ] **Step 11.5: Run tests — expect green**

```bash
cd adma_geo
python manage.py test filemanager.tests.test_archived_visibility -v 2
```
Expected: 3 assertions, all `OK`.

- [ ] **Step 11.6: Run the full test suite to catch regressions**

```bash
cd adma_geo
python manage.py test filemanager.tests -v 2
```
Expected: all green.

- [ ] **Step 11.7: Commit**

```bash
cd ..
git add adma_geo/filemanager/views.py \
        adma_geo/filemanager/api_views.py \
        adma_geo/filemanager/templates/filemanager/public_home.html \
        adma_geo/filemanager/tests/test_archived_visibility.py
git commit -m "Hide archived folders and files from normal listing surfaces"
```

---

## Task 12: Disable the daily Celery Beat schedule + update README

**Files:**
- Modify: `adma_geo/adma_geo/settings.py`
- Modify: `adma_geonode_project/README.md`
- Modify: `adma_geonode_project/adma_geo/env.production.example`

- [ ] **Step 12.1: Remove the daily poll from `CELERY_BEAT_SCHEDULE`**

In `adma_geo/adma_geo/settings.py`, remove the `'sync-johndeere-daily'` entry entirely. The updated block becomes:

```python
CELERY_BEAT_SCHEDULE = {
    'sync-realm5-daily': {
        'task': 'filemanager.tasks.sync_realm5_task',
        'schedule': crontab(hour=2, minute=0),  # Run at 2:00 AM daily
    },
    'cleanup-idle-agents': {
        'task': 'filemanager.tasks.cleanup_idle_agents_task',
        'schedule': crontab(minute='*/5'),  # Every 5 minutes
    },
}
```

Leave `sync_johndeere_task` defined in `tasks.py` (for manual bootstrap via `python manage.py sync_johndeere --once`), and leave the existing `setup_johndeere` / `sync_johndeere` management commands untouched.

- [ ] **Step 12.2: Update `env.production.example` with the new vars**

In `adma_geo/env.production.example` (append at the bottom; Grep for "JD_" first to find the existing JD block and place the new ones alongside it):

```
# John Deere webhook (Data Subscription Service)
JD_WEBHOOK_CALLBACK_URL=https://adma.example.com/api/v1/webhooks/johndeere/
JD_WEBHOOK_USERNAME=  # random; used by JD to authenticate its POSTs to us
JD_WEBHOOK_PASSWORD=  # random; generate e.g. `python -c 'import secrets; print(secrets.token_urlsafe(32))'`
```

- [ ] **Step 12.3: Update the main README**

In `adma_geonode_project/README.md`, find the "John Deere Operations Center" section (under "Third-Party Integrations"). Replace the existing block with:

```markdown
### John Deere Operations Center

Event-driven sync of field data, boundaries, and operations from John Deere via
the Data Subscription Service webhook.

**Setup:**
1. Register the application at [John Deere Developer Portal](https://developer.deere.com/).
2. Configure OAuth2 credentials and organization ID in `.env`:
   - `JD_CLIENT_ID`, `JD_CLIENT_SECRET`, `JD_REFRESH_TOKEN`, `JD_ORG_ID`
3. Configure webhook receiver settings:
   - `JD_WEBHOOK_CALLBACK_URL` — public HTTPS URL for your `/api/v1/webhooks/johndeere/` endpoint
   - `JD_WEBHOOK_USERNAME`, `JD_WEBHOOK_PASSWORD` — random credentials JD will use to authenticate its POSTs to us
4. Bootstrap the John Deere root folder:
   ```
   docker-compose exec django python manage.py setup_johndeere
   ```
5. Optional initial backfill (one-shot):
   ```
   docker-compose exec django python manage.py sync_johndeere --once
   ```
6. Create the JD subscription so events start flowing:
   ```
   docker-compose exec django python manage.py manage_johndeere_webhooks --create
   ```

**Sync model:** event-driven. JD POSTs every field / boundary / field-operation
change to our webhook; each event triggers a targeted refresh of that one
resource. No daily poll.

**Data Synced:**
- Field metadata and boundaries (as shapefiles)
- Field operations (as JSON; can be extended to richer artifacts)
- Soft-delete: JD archive/delete events mark local folders/files `is_archived=True`
  (recoverable via Django admin).

**Ops:**
- List or delete subscriptions: `python manage.py manage_johndeere_webhooks --list` / `--delete <sub_id>`
- Inspect events: Django admin → John Deere Webhook Events
```

- [ ] **Step 12.4: Run the full test suite once more**

```bash
cd adma_geo
python manage.py test filemanager.tests -v 2
```
Expected: all green.

- [ ] **Step 12.5: Commit**

```bash
cd ..
git add adma_geo/adma_geo/settings.py \
        adma_geo/env.production.example \
        README.md
git commit -m "Switch John Deere sync to event-driven; update docs"
```

---

## Task 13: Manual sandbox verification command

**Files:**
- Create: `adma_geo/filemanager/management/commands/verify_johndeere_webhook.py`

This one-off utility is not wired into CI; it exists to aid the manual sandbox verification step in §10 of the spec.

- [ ] **Step 13.1: Create the command**

Create `adma_geo/filemanager/management/commands/verify_johndeere_webhook.py`:

```python
"""
Manual verification helper for John Deere webhook plumbing.

Usage:
    python manage.py verify_johndeere_webhook

Prints the 10 most recent events from the DB along with their status and
timestamps, so an operator can confirm whether events are arriving and
being processed end-to-end.
"""
from django.core.management.base import BaseCommand

from filemanager.models import JohnDeereSubscription, JohnDeereWebhookEvent


class Command(BaseCommand):
    help = 'Print recent JD subscriptions and webhook events for verification.'

    def handle(self, *args, **options):
        subs = JohnDeereSubscription.objects.filter(is_active=True)
        self.stdout.write(self.style.SUCCESS(
            f"Active subscriptions: {subs.count()}"
        ))
        for s in subs:
            self.stdout.write(
                f"  {s.jd_subscription_id}  org={s.org_id}  "
                f"endpoint={s.client_endpoint}"
            )

        events = JohnDeereWebhookEvent.objects.order_by('-received_at')[:10]
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"Most recent events: {events.count()}"
        ))
        for e in events:
            self.stdout.write(
                f"  [{e.status}] {e.event_type_id}  "
                f"id={e.jd_event_id}  received={e.received_at.isoformat()}  "
                f"error={e.error_message or '-'}"
            )
```

- [ ] **Step 13.2: Smoke-test it runs**

```bash
cd adma_geo
python manage.py verify_johndeere_webhook
```
Expected: exits 0; prints zero active subs and zero events on a fresh DB.

- [ ] **Step 13.3: Commit**

```bash
cd ..
git add adma_geo/filemanager/management/commands/verify_johndeere_webhook.py
git commit -m "Add verify_johndeere_webhook helper for sandbox rollout"
```

---

## Final sanity check

- [ ] **Step F.1: Run the entire test suite**

```bash
cd adma_geo
python manage.py test filemanager -v 2
```
Expected: all green (existing tests + all new tests from Tasks 1–13).

- [ ] **Step F.2: Verify the URL is actually wired**

```bash
cd adma_geo
python manage.py show_urls 2>/dev/null | grep johndeere || \
  python -c "from django.urls import reverse; import django, os; \
             os.environ.setdefault('DJANGO_SETTINGS_MODULE','adma_geo.settings'); \
             django.setup(); print(reverse('api:johndeere_webhook'))"
```
Expected: prints `/api/v1/webhooks/johndeere/`.

- [ ] **Step F.3: Sandbox rollout**

Not code — human-operated. After deploy:

1. Set env vars: `JD_WEBHOOK_CALLBACK_URL`, `JD_WEBHOOK_USERNAME`, `JD_WEBHOOK_PASSWORD`.
2. `docker-compose exec django python manage.py migrate`
3. `docker-compose exec django python manage.py setup_johndeere`
4. `docker-compose exec django python manage.py sync_johndeere --once` (bootstrap backfill).
5. `docker-compose exec django python manage.py manage_johndeere_webhooks --create`
6. Trigger a field change in the JD sandbox UI.
7. `docker-compose exec django python manage.py verify_johndeere_webhook` and confirm the event reached `completed` status.
8. Once verified, promote the same sequence to production with the production `JD_WEBHOOK_CALLBACK_URL`.

Open follow-ups tracked in the design spec §11 — verify exact JD `eventTypeId` strings and adjust `EVENT_TYPE_HANDLERS` in `adma_geo/filemanager/johndeere_webhook_tasks.py` if JD uses different names.
