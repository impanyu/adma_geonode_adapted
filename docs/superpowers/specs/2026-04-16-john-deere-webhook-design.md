# John Deere Webhook Integration — Design Spec

- **Date:** 2026-04-16
- **Target repo:** `adma_geonode_project`
- **Target app:** `filemanager` (Django)
- **Primary author:** Yang Pan
- **Status:** Draft — awaiting implementation plan

## 1. Purpose

Replace the existing daily pull-based John Deere (JD) sync with an event-driven integration built on John Deere's Data Subscription Service (DSS). When a field, boundary, or field operation changes for the connected JD organization, JD will POST a webhook event to our Django app. The app will authenticate the event, persist it, and trigger a targeted refresh of only the affected resource — updating the matching `Folder`/`File` records and GeoServer layers without rescanning the whole org.

## 2. Goals & Non-goals

### Goals
- Subscribe to JD DSS events for **fields**, **boundaries**, and **field operations**.
- Receive events at a publicly reachable HTTPS endpoint authenticated with HTTP Basic Auth.
- Persist every received event for audit and dedup.
- On each event, fetch only the referenced resource from the JD API and upsert the corresponding local `Folder`/`File` records (and GeoServer layers for spatial data).
- Soft-delete local data on JD delete/archive events (mark `is_archived=True`; never remove files from disk).
- Provide a management command to CRUD JD event subscriptions; expose subscriptions and events in Django admin.
- Retain the existing `sync_johndeere_task` as a manual-only bootstrap/backfill path; remove its daily Celery Beat schedule.
- Keep single-org configuration for v1 while shaping models so multi-org is a trivial later addition.

### Non-goals (v1)
- Multi-org end-to-end support (data model supports it; no multi-org ops tooling or tests).
- Load/soak tests.
- Replay-attack defenses beyond `jd_event_id` dedup.
- Auto-bootstrap of subscriptions on Django startup (management command is the create path).
- A custom UI for managing subscriptions beyond Django admin.

## 3. Context: existing state

The repo already has:

- `adma_geo/filemanager/johndeere_client.py` — OAuth2 client (refresh-token flow) with methods for orgs, fields, boundaries, field operations.
- `adma_geo/filemanager/tasks.py :: sync_johndeere_task` — Celery task run daily at 03:00 by `CELERY_BEAT_SCHEDULE['sync-johndeere-daily']`.
- `adma_geo/filemanager/management/commands/setup_johndeere.py` — creates the root "John Deere" `Folder` (public, `third_party_source='johndeere'`).
- `adma_geo/filemanager/management/commands/sync_johndeere.py` — `--once` / `--async` manual sync trigger.
- Settings keys: `JD_CLIENT_ID`, `JD_CLIENT_SECRET`, `JD_REFRESH_TOKEN`, `JD_ORG_ID` (default `4193081`).
- URL roots: `/` → `filemanager.urls` (web); `/api/v1/` → `filemanager.api_urls` (token API).
- The JD API base URL in the client is the **sandbox** (`https://sandboxapi.deere.com/platform`).

## 4. Design decisions (from brainstorming)

| Question | Decision |
|---|---|
| Role of webhook vs daily poll | **Replace** the poll; fully event-driven |
| Resources to subscribe to | Fields **and** boundaries **and** field operations |
| Action per event | **Targeted** refresh of only the referenced resource |
| Auth for inbound events | HTTP Basic Auth (JD DSS built-in) |
| Callback URL scope | Fully env-configurable (`JD_WEBHOOK_CALLBACK_URL`) so sandbox and production can differ |
| Subscription CRUD | Management command **plus** `JohnDeereSubscription` model in admin |
| Deletion handling | Soft-delete (`is_archived=True`); files stay on disk, recoverable |
| Event persistence | Persist every event (`JohnDeereWebhookEvent` model) for audit + dedup |
| Multi-org | Single-org now, but models key by `org_id` so multi-org is additive |

## 5. Architecture

```
                    (HTTPS POST, Basic Auth)
  John Deere DSS ──────────────────────────────►  /api/v1/webhooks/johndeere/
                                                        │
                                   (auth check, dedup on event id, store row)
                                                        │
                                              ack 200 ◄─┤
                                                        │
                                                        ▼
                                          Celery: process_johndeere_event_task
                                                        │
                                     per event type ┌───┼──────────────┐
                                                    ▼   ▼              ▼
                                         fetch field  fetch boundary  fetch operation
                                         update Folder/File records + GeoServer
```

Subscription lifecycle is a separate out-of-band concern driven by a management command and a `JohnDeereSubscription` row in the DB; the runtime webhook path never creates or deletes JD subscriptions.

## 6. Components

### 6.1 New files

**`adma_geo/filemanager/johndeere_webhook.py`**
- `johndeere_webhook_receiver(request)` — `@csrf_exempt`, POST-only.
- `_check_basic_auth(request)` — extracts the `Authorization` header; compares via `django.utils.crypto.constant_time_compare` against `JD_WEBHOOK_USERNAME`/`JD_WEBHOOK_PASSWORD`.
- `_parse_event(request)` — validates JSON structure; returns a dict or raises a typed error.

**`adma_geo/filemanager/johndeere_webhook_tasks.py`**
- `process_johndeere_event_task(event_id)` — dispatcher; loads the event row, routes by `event_type_id`.
- `handle_field_event(event)`, `handle_field_deletion(event)`, `handle_boundary_event(event)`, `handle_field_operation_event(event)` — per-resource handlers.
- `_upsert_field_folder(field_data)`, `_upsert_boundary_files(field_id, boundary_data)`, `_upsert_operation_folder(operation_data)` — helpers shared with the bootstrap path.

**`adma_geo/filemanager/management/commands/manage_johndeere_webhooks.py`**
- Subcommands: `--create`, `--list`, `--delete <sub_id>`, `--show <sub_id>`.
- Reads `JD_WEBHOOK_CALLBACK_URL`, `JD_WEBHOOK_USERNAME`, `JD_WEBHOOK_PASSWORD` from env.
- Writes `JohnDeereSubscription` rows only after JD returns a 201 with a subscription id.

### 6.2 Extended files

**`adma_geo/filemanager/johndeere_client.py`** — add:
- `create_subscription(client_endpoint, username, password, event_type_ids, org_id)` → `dict`.
- `list_subscriptions()` → `list[dict]` (handles pagination).
- `delete_subscription(sub_id)` → `bool` (True on 204 or 404).
- `get_resource_by_link(uri)` — generic follow-link fetch (events reference resources by URI in `targetResource`).

**`adma_geo/filemanager/models.py`** — add two models (see §6.3) and a boolean `is_archived` field on the existing `Folder` and `File` models (default `False`, indexed). Default querysets where the UI lists user-visible data should filter `is_archived=False`.

**`adma_geo/filemanager/api_urls.py`** — add:
```python
from . import johndeere_webhook
path('webhooks/johndeere/', johndeere_webhook.johndeere_webhook_receiver, name='johndeere_webhook'),
```
The URL is intentionally under `/api/v1/` (already the API root) but does **not** use token auth — it uses Basic Auth with JD-specific credentials.

**`adma_geo/adma_geo/settings.py`** — add:
```python
JD_WEBHOOK_CALLBACK_URL = os.environ.get('JD_WEBHOOK_CALLBACK_URL')
JD_WEBHOOK_USERNAME = os.environ.get('JD_WEBHOOK_USERNAME')
JD_WEBHOOK_PASSWORD = os.environ.get('JD_WEBHOOK_PASSWORD')
```
Remove `CELERY_BEAT_SCHEDULE['sync-johndeere-daily']` (timing sequenced per §10 — removed only after sandbox verification).

**`adma_geo/filemanager/admin.py`** — register `JohnDeereSubscription` (CRUD) and `JohnDeereWebhookEvent` (read-only list with filters on `status`, `event_type_id`, `received_at`; admin action "Reprocess event" re-enqueues the dispatcher task).

### 6.3 New models

```python
class JohnDeereSubscription(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    jd_subscription_id = models.CharField(max_length=128, unique=True)
    org_id = models.CharField(max_length=64, db_index=True)
    event_type_ids = models.JSONField(default=list)
    client_endpoint = models.URLField()
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class JohnDeereWebhookEvent(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('skipped_duplicate', 'Skipped (duplicate)'),
        ('skipped_unknown_type', 'Skipped (unknown type)'),
    ]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    jd_event_id = models.CharField(max_length=128, unique=True)  # enables dedup
    event_type_id = models.CharField(max_length=64, db_index=True)
    org_id = models.CharField(max_length=64, db_index=True)
    target_resource_uri = models.URLField(null=True, blank=True)
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)
    payload = models.JSONField()
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default='pending', db_index=True)
    processing_started_at = models.DateTimeField(null=True, blank=True)
    processing_completed_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(null=True, blank=True)
    related_folder = models.ForeignKey('Folder', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    related_file = models.ForeignKey('File', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
```

## 7. Data flow

### 7.1 Inbound request → ack (synchronous, fast path)

1. JD POSTs JSON to `https://<host>/api/v1/webhooks/johndeere/` with `Authorization: Basic <base64>`.
2. View extracts credentials, compares both username and password via `constant_time_compare`. On mismatch → `401`, no DB write.
3. View parses JSON. If `eventId` missing/malformed → `400`.
4. View attempts `JohnDeereWebhookEvent.objects.create(jd_event_id=...)`. On `IntegrityError` (duplicate) → update status to `skipped_duplicate`, return `200`.
5. Otherwise, enqueue `process_johndeere_event_task.delay(event.id)`, return `200` with body `{"status": "accepted", "event_id": "<uuid>"}`.
6. Steps 2–5 are wrapped in try/except — any unhandled exception returns `500` so JD retries.

### 7.2 Dispatcher task → per-resource handler (async)

`process_johndeere_event_task(event_id)`:
1. Load event row, set `status='processing'`, `processing_started_at=now()`.
2. Read `event_type_id` and route:
   - `fieldCreated` / `fieldUpdated` → `handle_field_event`
   - `fieldArchived` / `fieldDeleted` → `handle_field_deletion`
   - `boundaryCreated` / `boundaryUpdated` → `handle_boundary_event`
   - `fieldOperationCreated` / `fieldOperationUpdated` → `handle_field_operation_event`
   - unknown → `status='skipped_unknown_type'`, log warning, return
3. On handler success → `status='completed'`, `processing_completed_at=now()`.
4. On exception → retry via Celery (see §8). After `max_retries` → `status='failed'`, store `error_message`.

**Note:** the exact JD `eventTypeId` string values must be verified against a live sandbox subscription response; a single mapping table in `johndeere_webhook_tasks.py` isolates this so renames/additions are a one-line change. This verification is an explicit step in the implementation plan.

### 7.3 Per-resource handlers

- **`handle_field_event(event)`** — call `client.get_field(org_id, field_id)`; upsert the field's `Folder` under the JD root (match by `third_party_id=field_id`, `third_party_source='johndeere'`); update name + metadata; if boundaries are embedded, sync them as files.
- **`handle_boundary_event(event)`** — call `client.get_field_boundaries(org_id, field_id)`; regenerate shapefile components in the field's boundary subfolder; re-publish to GeoServer via existing `bundle_and_publish_shapefile`. `File` rows for boundaries no longer returned by JD are marked `is_archived=True`.
- **`handle_field_operation_event(event)`** — call `client.get_field_operation(operation_id)`; upsert operation `Folder` + files. The existing per-operation block in `sync_johndeere_task` is factored into `_upsert_operation_folder` and reused here.
- **`handle_field_deletion(event)`** — find local `Folder` by `third_party_id`; set `is_archived=True` on folder and all descendant `Folder`/`File` rows; do NOT delete files from disk or GeoServer layers.

### 7.4 Soft-delete representation

Add `is_archived` (boolean, default `False`, indexed) to `Folder` and `File`. Existing list views (and templates that enumerate children) filter `is_archived=False` by default. Django admin surfaces archived items with a filter for operational recovery.

## 8. Error handling, idempotency, security

### 8.1 Idempotency
- `jd_event_id` carries a DB `UNIQUE` constraint. Duplicate POST → `IntegrityError` → mark `skipped_duplicate`, return `200`.
- Using `create()` + catch (not `get_or_create`) avoids a TOCTOU race on in-flight duplicates.
- Handlers are themselves idempotent: upserts match by `third_party_id`, never blindly create.

### 8.2 Retry
- **Endpoint**: only return `5xx` for our own bugs (DB unavailable, unhandled exception before ack). Auth and format failures are `4xx` — JD must not retry those.
- **Celery**: handler tasks use `autoretry_for=(requests.RequestException, TimeoutError)`, `retry_backoff=True`, `retry_backoff_max=600`, `max_retries=5`.
- **Terminal failure**: `status='failed'` + `error_message` stored; visible in admin.
- **Manual replay**: admin action "Reprocess event" re-enqueues `process_johndeere_event_task`.

### 8.3 Security
- **Transport**: HTTPS enforced at Nginx (existing). View rejects if `request.scheme != 'https'` when `DEBUG=False`.
- **Basic Auth**: both username and password compared with `constant_time_compare`. Missing/malformed `Authorization` header → `401`, no DB write, no logging of the attempted credentials.
- **Payload size**: guard `len(request.body) > 100_000` → `413`. JD payloads are small (<10 KB).
- **CSRF**: `@csrf_exempt` on the view (third-party POST authenticated via Basic Auth, not sessions).
- **Rate limit**: not for v1 — Basic Auth + dedup make spam harmless. Revisit if abuse appears.
- **Logging hygiene**: log `jd_event_id` and `event_type_id` only; never log the `Authorization` header or payload body (payload persists in the DB).

### 8.4 Subscription lifecycle edges
- `manage_johndeere_webhooks --create` errors clearly if `JD_WEBHOOK_CALLBACK_URL` / username / password are unset.
- On `--create`: writes `JohnDeereSubscription` only after JD's 201 returns `jd_subscription_id`. Failed attempts do not leave orphan rows.
- On `--delete`: deletes remotely first, then locally. Remote 404 is treated as "already gone" and the local row is still deleted.
- **No** auto-create on Django startup.

## 9. Testing strategy

### 9.1 Unit tests (fast, no network)

- **`test_johndeere_webhook_view.py`** — Django test client:
  - `401` on missing `Authorization`, wrong username, wrong password (no DB row created).
  - `400` on missing `eventId`, invalid JSON.
  - `413` on oversized body.
  - `200` + DB row created on valid event; Celery task enqueued (patch `.delay`).
  - `200` + `skipped_duplicate` on resent event (same `jd_event_id`).
  - `500` on simulated DB failure (task NOT enqueued).
- **`test_johndeere_webhook_tasks.py`** — patch `JohnDeereClient`:
  - Each handler covers its event types (created/updated/archived).
  - Dispatcher routes each `event_type_id` to the right handler; unknown → `skipped_unknown_type`.
  - Handler raises `requests.RequestException` → task retries; after max retries → `status='failed'`, `error_message` set.
- **`test_johndeere_client_subscriptions.py`** — mock `requests`:
  - `create_subscription` POSTs correct body, returns created `id`.
  - `list_subscriptions` handles pagination.
  - `delete_subscription` returns True on 204 and on 404; False otherwise.
- **`test_manage_johndeere_webhooks_command.py`** — `call_command(...)` with client patched:
  - `--create` writes row only on JD 201; errors if env vars missing.
  - `--list` prints DB rows joined with live JD state.
  - `--delete <id>` removes remote then local; handles already-gone.

### 9.2 Integration test (one, against sandbox, manual)

Separate command `python manage.py test_johndeere_webhook_live` (not in CI):
1. Create a sandbox subscription pointing at a configured tunnel URL.
2. Wait for an event (prints instructions for triggering one in the JD UI / Postman).
3. Assert the event row reaches `completed` and the referenced `Folder`/`File` exist locally.

### 9.3 Fixtures

`adma_geo/filemanager/tests/fixtures/johndeere_events/` — captured or spec-built JSON samples for `fieldCreated`, `fieldUpdated`, `fieldArchived`, `boundaryUpdated`, `fieldOperationCreated`, `fieldOperationUpdated`. Drives all unit tests.

## 10. Migration & rollout

1. Implement the models, view, client extensions, tasks, management command, and tests behind the new endpoint — but with `CELERY_BEAT_SCHEDULE['sync-johndeere-daily']` still active.
2. Make a data migration for `is_archived` on `Folder` and `File` (default `False`).
3. In sandbox: run `manage_johndeere_webhooks --create`, manually trigger events, verify the `completed` path end-to-end.
4. Capture real JD payload samples and confirm the dispatcher's event-type mapping.
5. Remove the `sync-johndeere-daily` entry from `CELERY_BEAT_SCHEDULE` once webhook coverage is verified in the target environment.
6. In production: deploy, set env vars, run `manage_johndeere_webhooks --create` against production JD.

## 11. Open items to verify during implementation

- Exact JD DSS `eventTypeId` strings for each resource/action combination (verify against a live sandbox subscription list or a captured payload).
- Exact JSON shape of the incoming event — `eventId` field name, where `targetResource` URI lives, where `orgId` and resource id are encoded. The design assumes a shape like `{"eventId", "eventTypeId", "targetResource", ...}`; parsing logic is centralized in `_parse_event` so adjustments stay in one place.
- Whether JD DSS accepts arbitrary username/password or has format constraints.
- Production JD webhook base URL (once `sandboxapi.deere.com` → `partnerapi.deere.com` cutover happens).

## 12. Rollback

- Disable the webhook subscription via `manage_johndeere_webhooks --delete <sub_id>`.
- Re-add `sync-johndeere-daily` to `CELERY_BEAT_SCHEDULE` to restore daily pull behavior.
- Archived data (`is_archived=True`) can be un-archived via Django admin; no disk-level data loss occurs during the webhook period.
