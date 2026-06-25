# Valid Yield Extractor — Column Mapping Dropdowns

**Date:** 2026-06-25
**Status:** Approved (design)
**Tool page:** `https://adma.unl.edu/tools/valid-yield-extractor/`

## Problem

The Valid Yield Extractor reads four logical columns from the user's input
shapefiles:

| Logical column | Source shapefile | Algorithm key |
|---|---|---|
| Plot ID | Rx treatment plots | `plot_id` |
| Target (prescribed) rate | Application / as-applied | `rxTarget` |
| Applied (actual) rate | Application / as-applied | `applied` |
| Yield | Harvest | `clnYield` |

Today these are **auto-detected** inside `filemanager/ValidYieldExtractorTool.py`
from fixed synonym lists. Two problems:

1. **No user control.** If a field is named something outside the synonym list,
   there is no way to tell the tool which column to use.
2. **Hard failure on plot ID.** `run()` detects the plot-ID column with a
   separate hardcoded list via `pick_column(...)` (line ~289) that raises
   `ValueError` when nothing matches, killing the whole run.

## Goal

Add optional **column-mapping dropdowns** to the tool UI. After the user selects
each input shapefile, the system reads that shapefile's columns and offers them
as dropdown choices, so the user can pick the correct column. Each dropdown
defaults to `— auto-detect —`, preserving today's behavior when left alone. The
chosen columns are threaded all the way into the algorithm.

Non-goals (YAGNI): CSV inputs (the file tree only lists `.shp`), changes to
Swath / Target CRS / Output handling, any DB schema change.

## Architecture

Three coordinated changes across the existing request flow:

```
Browser (valid_yield_extractor_tool.html)
  │  GET /api/valid-yield-extractor/columns/<file_id>/   ← NEW: read columns
  │  POST /api/valid-yield-extractor/run/ {... + 4 column fields}
  ▼
run_valid_yield_extractor (views.py)
  ▼  .delay(..., plot_id_col, target_rate_col, applied_rate_col, yield_col)
run_valid_yield_extractor_task (tasks.py)
  ▼  argparse.Namespace(... + overrides)
ValidYieldExtractorTool.run(args)   ← overrides take precedence over auto-detect
```

### Component 1 — Columns endpoint (NEW)

- **Route:** `path('api/valid-yield-extractor/columns/<str:file_id>/', views.valid_yield_extractor_columns, name='valid_yield_extractor_columns')` in `filemanager/urls.py`.
- **View:** `valid_yield_extractor_columns(request, file_id)` (GET, `@login_required`).
  - Look up `File` by id; 404 if missing.
  - Permission: allow if `file.owner == request.user or file.is_public`; else 403. (Mirrors `run_valid_yield_extractor`.)
  - Validate extension is `.shp`; else 400.
  - Read field names without a full load. Preferred: `pyogrio.read_info(path)["fields"]`. Fallback: `list(geopandas.read_file(path, rows=1).columns)` minus the geometry column. Wrap in try/except → 500 with generic message via `_internal_error_response`.
  - Response: `{ "success": true, "columns": ["Plot_Number", "Rate", ...] }`.
- **Interface contract:** input = file UUID in URL; output = JSON column-name list. No side effects.

### Component 2 — Frontend column mapping UI

In `templates/filemanager/valid_yield_extractor_tool.html`:

- **Markup:** a new card "Column mapping (auto-detected if left as default)" placed after the three file selectors, before Settings. Four `<select>` elements:
  - `id="plotIdColumn"` — Plot ID
  - `id="targetRateColumn"` — Target rate
  - `id="appliedRateColumn"` — Applied rate
  - `id="yieldColumn"` — Yield

  Each initially contains exactly one `<option value="">— auto-detect —</option>`.

- **Populate logic:** add a helper `loadColumnsForFile(fileId, selectEls)` that
  GETs the columns endpoint and rebuilds the given select(s): first option is
  always `— auto-detect —` (value `""`), followed by one option per column
  (text and value = column name, XSS-escaped via the existing escaping path /
  `textContent`). On error, leave only the auto-detect option and log.

- **Triggers:** the existing file-selection handler (`selectFile`, ~line 842)
  sets the hidden input. Extend it so that:
  - plots selected → `loadColumnsForFile(id, [plotIdColumn])`
  - app selected → `loadColumnsForFile(id, [targetRateColumn, appliedRateColumn])`
  - harv selected → `loadColumnsForFile(id, [yieldColumn])`
  Re-selecting a file repopulates and resets that dropdown to auto-detect.

- **Submit:** the submit handler (~line 1142) adds to the JSON body:
  `plot_id_col`, `target_rate_col`, `applied_rate_col`, `yield_col`, each set to
  the select's value or `null` when value is `""`. Form validity still requires
  only the three file IDs (dropdowns are optional).

### Component 3 — Thread overrides into the algorithm

**`run_valid_yield_extractor` (views.py ~3448):** read the four optional fields
from `data`; pass them as keyword args to `run_valid_yield_extractor_task.delay(...)`.

**`run_valid_yield_extractor_task` (tasks.py ~2352):** add keyword params
`plot_id_col=None, target_rate_col=None, applied_rate_col=None, yield_col=None`
(after existing params, before/with `requesting_user_id` — all keyword,
backward compatible). Set them on the `argparse.Namespace`:
`plot_id_col=..., applied_col=..., target_col=..., yield_col=...`.

**`ValidYieldExtractorTool.run(args)`:** apply overrides, each taking precedence
over auto-detect; empty/None → unchanged behavior. Use
`getattr(args, "<name>", None)` so direct CLI/interactive callers (which don't
set these) keep working.

- Plot ID: replace the `pick_column(...)` call so it becomes
  `plot_id_col = args.plot_id_col or pick_column(plots, [...])`, then validate
  the override exists in `plots.columns` (raise a clear `ValueError` naming the
  bad column and listing available columns if not). Then rename → `plot_id` as today.
- Applied / Target / Yield: after the respective `standardize_columns(...)`
  call, if an override is set, assign the standard key directly from the chosen
  column: e.g. `if applied_col: app["applied"] = app[applied_col]` (validate the
  column exists first, same clear error). This guarantees the user's choice wins
  over any synonym match. Keys: applied→`applied`, target→`rxTarget`, yield→`clnYield`.

## Data flow (chosen column)

`<select> value "Rate_Actual"` → POST `applied_rate_col: "Rate_Actual"` →
view → `task.delay(applied_rate_col="Rate_Actual")` → `args.applied_col` →
`app["applied"] = app["Rate_Actual"]` → existing VAA/VHA pipeline unchanged.

## Error handling

- Columns endpoint: 404 (missing file), 403 (no permission), 400 (not `.shp`),
  500 (unreadable) — all JSON `{success:false,error}`. (Note: this app's AJAX
  pattern means the caller must send a valid CSRF cookie; unrelated to this
  feature but the GET endpoint needs none.)
- Frontend: on a failed columns fetch, dropdown shows only `— auto-detect —`;
  the run can still proceed (auto-detect path).
- Algorithm: an override naming a nonexistent column raises a `ValueError`
  surfaced through the task result as a failure message.

## Testing

- **Unit (algorithm):** `run()` with each override set picks the named column
  even when it is NOT in the synonym list; with overrides empty, behavior is
  byte-for-byte the same as before (auto-detect). Override naming a missing
  column raises `ValueError` mentioning the column.
- **View/endpoint:** columns endpoint returns the column list for an owned
  `.shp`; 403 for a non-owned non-public file; 400 for a non-`.shp`; 404 for a
  bad id. `run_valid_yield_extractor` forwards the four fields to `.delay`
  (assert via mock).
- **Manual:** on the live tool, select the three files, confirm each dropdown
  populates from the right shapefile, pick a non-default column, run, and verify
  output reflects the chosen column.

## Files touched

- `filemanager/urls.py` — 1 new route.
- `filemanager/views.py` — new `valid_yield_extractor_columns` view; extend
  `run_valid_yield_extractor` to forward 4 fields.
- `filemanager/tasks.py` — 4 new keyword params on the task; set on Namespace.
- `filemanager/ValidYieldExtractorTool.py` — apply overrides in `run()`.
- `templates/filemanager/valid_yield_extractor_tool.html` — column-mapping card,
  populate logic, submit-body fields.
