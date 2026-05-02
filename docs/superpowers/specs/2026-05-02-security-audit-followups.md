# Security Audit Findings & Follow-ups

- **Date:** 2026-05-02
- **Branch when audited:** `feature/johndeere-webhook` at `3a452c4` (most recent fix at audit time)
- **Audit scope:** Comprehensive — XSS, SSRF, BOLA/IDOR, CSRF, injection, auth, sessions, headers, file upload, container escape, secrets, rate limit, dependencies, public-data exposure, webhooks
- **Status legend:** ✅ Fixed in this session • 🔧 In progress • ❌ Not yet addressed (open)

This document captures everything still on the security backlog so future sessions can pick up without re-auditing.

## Items being addressed in the next commits

These are the four items the user approved for the immediate next pass.

### ✅ Critical 1 — Docker socket exposure

**Where:** `adma_geo/docker-compose-adma.yml` lines 47, 100. Both `django` and `celery` services bind-mount `/run/user/1007/docker.sock` to `/var/run/docker.sock` inside the container.

**Why it matters:** Any code execution inside the django or celery container can call the Docker API to spawn a privileged container that mounts `/`, achieving trivial host escape. The agent feature requires Docker access to spawn per-user agent containers. We have temporarily gated the agent feature to user `Yu` only, but the socket mount itself remains a breakout primitive even when agents are disabled.

**Plan:** Replace the direct socket mount with [Tecnativa/docker-socket-proxy](https://github.com/Tecnativa/docker-socket-proxy). Restrict the proxy to only the API surface the agent manager uses (containers, images:read, networks:read). The django and celery containers point at the proxy URL instead of the raw socket.

**Status:** ✅ Fixed in commit `025a2f7` — added `docker-socket-proxy` service, removed raw socket mounts from django/celery, added `DOCKER_HOST=tcp://docker-socket-proxy:2375` env var to both services in both `docker-compose-adma.yml` and `docker-compose.yml`.

---

### ✅ High 3 — Login brute-force protection

**Where:** `adma_geo/adma_geo/settings.py` MIDDLEWARE list and `adma_geo/requirements.txt`. Django's built-in login view has no rate limit or lockout.

**Why it matters:** Password spraying against any user, including admin, is currently unrestricted. drakes_sodden's pen test could escalate from "create folders" to "guess admin password" using the same volume of automated requests.

**Plan:** Add `django-axes` to requirements (it's the most mature option). Configure 5 failed attempts → 1 hour lockout per (username, IP) combo. Surface lockout events in admin.

**Status:** ✅ Fixed in commit `27b286d` — added `django-axes>=6.0.0,<7.0.0` to requirements.txt; configured INSTALLED_APPS, MIDDLEWARE (AxesMiddleware last), AUTHENTICATION_BACKENDS (AxesStandaloneBackend first), and AXES_* settings block. Operator must run `python manage.py migrate` on next deploy.

---

### ✅ High 4 — HSTS and SSL redirect

**Where:** `adma_geo/adma_geo/settings.py`. `SECURE_SSL_REDIRECT` and `SECURE_HSTS_*` were added as commented stubs in the previous round.

**Why it matters:** Without `SECURE_SSL_REDIRECT=True`, a request to plain HTTP can be served (or downgrade attacks can begin). HSTS instructs browsers to refuse HTTP for the configured duration, defeating SSL stripping.

**Plan:** User confirmed prod is fully on HTTPS. Enable:

```python
SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31536000          # 1 year
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
```

**Status:** ✅ Fixed in commit `01e5029` — replaced the commented-out stubs with live settings. `SECURE_PROXY_SSL_HEADER` was already set, so no infinite-redirect risk behind nginx.

---

### ✅ High 5 — Celery tasks don't enforce `requesting_user_id`

**Where:** `adma_geo/filemanager/tasks.py` — every `run_*_tool_task` (seeding, si, shape-to-json, yield-summary, valid-yield-extractor) accepts a `file_id` and processes it without verifying it belongs to the calling user.

**Why it matters:** The previous fix injected `requesting_user_id` at the API layer (`api_run_tool`), but the tasks themselves accept any `file_id` they receive. A user who calls `/api/v1/tools/<slug>/run/` with another user's `file_id` still gets that file's data processed in their own output folder. The IDs are UUIDs so guessing is hard, but anyone who can read a public file's UUID (via search, public listings, etc.) can run tools on it.

**Plan:** Add a `requesting_user_id` parameter to each `run_*_tool_task`. Inside the task, fetch the input File and verify `file.owner_id == requesting_user_id` (with a public-file allowance if appropriate). On mismatch: reject the task with a clear error and log a security event.

**Status:** ✅ Fixed in commit `c8fac51` — added `requesting_user_id=None` kwarg and ownership check (with public-file allowance) to all 5 tasks: `run_seeding_tool_task`, `run_shape_to_json_task`, `run_si_tool_task` (primary: buffer_shp_id), `run_yield_summary_tool_task` (primary: treatment_file_id), `run_valid_yield_extractor_task` (primary: plots_file_id). Direct view callers pass no `requesting_user_id` so the check is skipped — those callers already authenticate at the view layer.

---

## Open backlog (not addressed yet)

### 🔴 Critical 2 — Live secrets in on-disk `.env` files

**Where:** `adma_geo/.env` and root `.env` on the production host.

**Contents (verified live):**
- OpenAI API key (`sk-proj-...`)
- John Deere `JD_CLIENT_SECRET`, `JD_REFRESH_TOKEN`
- Realm5 API key
- GeoServer admin password
- Django `SECRET_KEY`
- PostgreSQL password (also in old GeoNode `.env` for legacy stack)

**Risk model:** Files are NOT git-tracked (`.gitignore` covers them), so the only exposure is via shell access to the production host. If no untrusted party has had shell on the host, urgency is low — rotation is best practice but not emergency.

**Rotation effort per credential:**

| Credential | Effort | Impact |
|---|---|---|
| `POSTGRES_PASSWORD` | 5 min | Brief DB connection blip on restart |
| `GEOSERVER_ADMIN_PASSWORD` | 5 min | Brief layer-management blip |
| `SECRET_KEY` | 1 min | All sessions invalidated; users re-login. Pending password-reset emails become invalid. |
| `REALM5_API_KEY` | External vendor cycle | Realm5 sync paused until done |
| `OPENAI_API_KEY` | 5 min self-serve | Agent feature paused until done |
| `JD_CLIENT_SECRET` + `REFRESH_TOKEN` | Half day; vendor cycle | JD webhook subscription must be re-created |

**Recommendation:** rotate during a low-traffic window. Long-term: move to AWS Secrets Manager / HashiCorp Vault.

---

### 🟡 Medium 6 — `FILE_UPLOAD_MAX_MEMORY_SIZE`

✅ **Fixed** — Dropped to 10MB in commit `3a452c4`. Nginx `client_max_body_size` raised to 2G to allow large GeoTIFF uploads while spooling to disk.

---

### 🟡 Medium 7 — DRF tokens never expire

**Where:** `rest_framework.authtoken.Token` model — issued tokens have no expiry.

**Why it matters:** A leaked or compromised token is valid forever until manually deleted from the DB. The `simplejwt` package is already installed but unused.

**Plan:** Switch token-issuing endpoint from DRF Token to `simplejwt`'s sliding token, with refresh and rotation. OR add a `token_created_at` check that rotates tokens older than N days. Add a `DELETE /api/v1/auth/token/` self-revoke endpoint while at it.

**Status:** ❌ Not yet addressed.

---

### 🟡 Medium 8 — `requirements.txt` `>=` without upper bounds

**Where:** `adma_geo/requirements.txt`. Most packages use `>=` with no upper bound. The celery surprise (5.3 → 5.6 mid-build) was a direct consequence.

**Plan:**
1. Run `pip freeze` in the working container, paste exact versions.
2. Replace `>=X.Y.Z` with `==<exact>` for production-critical packages.
3. Keep `>=,<` ranges for nice-to-have packages where breaking changes are unlikely within a major.
4. Add a CI step that runs `pip-audit` against the locked versions on every PR.

**Status:** ❌ Not yet addressed.

---

### 🟢 Low 9 — `ALLOWED_HOSTS` includes `0.0.0.0`

**Where:** `adma_geo/adma_geo/settings.py:23`. Harmless at runtime — Django doesn't treat `0.0.0.0` specially in `ALLOWED_HOSTS`. Misleading.

**Plan:** Remove `'0.0.0.0'` from the list.

**Status:** ❌ Not yet addressed.

---

### 🟢 Low 10 — ZIP download member names can contain `../`

**Where:** `api_download_folder` in `api_views.py` — uses `file_obj.name` as the in-archive path component.

**Why it matters:** Server-side packaging is safe (we're writing, not extracting), but a downloaded ZIP with `../` members can write outside the destination on a vulnerable client extractor (zip slip on the user's machine).

**Plan:** Run each path component through `os.path.basename()` (or a stricter sanitizer) before adding to the archive. The folder structure inside the ZIP can still preserve user folders, just not `../`.

**Status:** ❌ Not yet addressed.

---

### 🟢 Low 11 — No self-revoke endpoint for API tokens

**Where:** Only admin can delete a `Token` row.

**Plan:** Add `DELETE /api/v1/auth/token/` that deletes `request.auth` (the token used to authenticate the call).

**Status:** ❌ Not yet addressed.

---

### 🟢 Low 12 — No audit log for failed logins

**Where:** Django's default login view emits to the `django.security` logger but with limited detail.

**Plan:** Subscribe to `django.contrib.auth.signals.user_login_failed` and emit a structured log line including username, IP, user-agent, timestamp. Will be partially superseded by `django-axes` (High 3) which also tracks failures.

**Status:** ❌ Not yet addressed (will be partially covered by High 3).

---

### 🟢 Low 13 — Owner username exposed in public file API

**Where:** `FileDetailSerializer`, `FolderDetailSerializer`, `MapSerializer` include `owner = StringRelatedField()` which resolves to `User.username`. The token API returns this for public files.

**Plan:** Either omit `owner` from public-API responses, or replace with a non-identifying display name (e.g. first name only, or `"Public User"`).

**Status:** ❌ Not yet addressed.

---

## Surprising context worth keeping

### MapDetailView permission bypass — `raise HttpResponseForbidden(...)` was a no-op

✅ **Fixed** in commit `1b90b8d`. Python accepted the `raise` (since `HttpResponseForbidden` is technically a class instance) but Django doesn't catch it as a 403 — it's not a `PermissionDenied`. The view fell through to `return map_obj` with no access check, so private maps were readable by anyone who knew the UUID. Now uses `raise PermissionDenied(...)` correctly.

### Root-directory `.env` for legacy GeoNode stack

There is a second `.env` in the repository root (NOT under `adma_geo/`), with a full set of credentials for the original GeoNode deployment: `GEOSERVER_ADMIN_PASSWORD`, `POSTGRES_PASSWORD`, `SECRET_KEY`, `OAUTH2_CLIENT_SECRET`, `ADMIN_PASSWORD`. If the legacy GeoNode stack is no longer running, this file should be deleted. If it is running, those credentials should be rotated alongside the adma_geo ones.

### `CORS_ALLOW_ALL_ORIGINS=True` in legacy GeoNode `.env`

Same legacy concern. If the GeoNode stack is active, it's broadcasting a wildcard CORS policy — any web page on any domain can make credentialed requests to its API. This bypasses CSRF protection in browsers.

### `adma_geo/cookies.txt`

✅ **Untracked** in commit `744c41a`. Empty libcurl cookie jar. The presence of this file suggests at some point auth cookies were being dumped to disk via `curl --cookie-jar`. The file is now in `.gitignore` and was empty, but the historical practice should be reviewed — there may still be other places where cookie jars are created.

### `DJANGO_DEV_FALLBACK=1` build-time escape hatch

The Dockerfile passes `DJANGO_DEV_FALLBACK=1` only for `collectstatic` so the build can succeed without a real `SECRET_KEY`. Runtime still requires the env var to be set. This is intentional — do not remove the fallback or production builds will fail.

---

## How to use this document

When the user asks "fix more security issues" in a future session, start here:
1. Read this file end-to-end.
2. Pick items based on the user's priority (Critical → High → Medium → Low).
3. After fixing an item, change its status from `❌` to `✅` and add the commit SHA.
4. If the audit reveals new findings during a fix, add them as new sections below.
