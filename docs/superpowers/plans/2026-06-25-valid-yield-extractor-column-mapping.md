# Valid Yield Extractor Column Mapping Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users pick which shapefile column maps to Plot ID, Target rate, Applied rate, and Yield via dropdowns that auto-populate from the selected shapefiles, threading the choices into the extractor algorithm (auto-detect remains the default).

**Architecture:** A new GET endpoint reads a shapefile's attribute field names for the UI. Four optional `<select>` menus in the existing tool template populate from that endpoint when each file is chosen and ride along in the run POST body. The view forwards them to the Celery task, which sets them on the `argparse.Namespace`; `ValidYieldExtractorTool.run()` uses any provided override in place of synonym auto-detection.

**Tech Stack:** Django (function views + `JsonResponse`), Celery task, geopandas/fiona, vanilla JS in a Django template, Django `TestCase`.

## Global Constraints

- Python column-reading uses libraries already in `requirements.txt`: `geopandas>=0.14.0`, `fiona>=1.9.0`. Do not add dependencies.
- Inputs are `.shp` only (the file tree lists only `.shp`). No CSV path.
- All four dropdowns are optional; empty value (`""`) / `None` = today's auto-detect behavior, unchanged.
- New task params are keyword-only with `None` defaults — existing positional `.delay(...)` calls and CLI/interactive callers of `run()` must keep working.
- Tests run with `python manage.py test <dotted.path>` from the `adma_geo/` directory (in dev, prefix with `docker-compose exec django`).
- Run all commands from `adma_geo/` (the Django project root containing `manage.py`).

---

### Task 1: Column-override helpers in the extractor algorithm

Add two small, unit-testable helpers to `ValidYieldExtractorTool.py` and wire them into `run()` so a user-supplied column name overrides synonym auto-detection.

**Files:**
- Modify: `filemanager/ValidYieldExtractorTool.py` (add helpers after `pick_column` ~line 116; edit `run()` plot-id block ~lines 287-292, app block ~line 304, harvest block ~line 313)
- Test: `filemanager/tests/test_valid_yield_extractor_columns.py` (new)

**Interfaces:**
- Produces:
  - `resolve_plot_id_column(plots, override=None) -> str` — returns `override` if truthy (raising `ValueError` if it is not a column of `plots`), else the auto-detected plot-id column via the existing `pick_column` list.
  - `apply_column_override(gdf, std_key, override_col) -> GeoDataFrame` — if `override_col` is truthy, sets `gdf[std_key] = gdf[override_col]` (raising `ValueError` if `override_col` is not a column); returns `gdf`. No-op when `override_col` is falsy.
  - `run(args)` reads overrides via `getattr(args, "plot_id_col", None)`, `getattr(args, "applied_col", None)`, `getattr(args, "target_col", None)`, `getattr(args, "yield_col", None)`.

- [ ] **Step 1: Write the failing test**

Create `filemanager/tests/test_valid_yield_extractor_columns.py`:

```python
import geopandas as gpd
from shapely.geometry import Point
from django.test import SimpleTestCase

from filemanager.ValidYieldExtractorTool import (
    resolve_plot_id_column,
    apply_column_override,
)


def _points_gdf(**cols):
    n = len(next(iter(cols.values())))
    geom = [Point(i, i) for i in range(n)]
    return gpd.GeoDataFrame({**cols, "geometry": geom}, crs="EPSG:4326")


class ResolvePlotIdColumnTests(SimpleTestCase):
    def test_override_used_even_when_not_a_known_synonym(self):
        plots = _points_gdf(Weird_Name=["a", "b"])
        self.assertEqual(resolve_plot_id_column(plots, "Weird_Name"), "Weird_Name")

    def test_falls_back_to_autodetect_when_override_empty(self):
        plots = _points_gdf(Plot_Number=["1", "2"])
        self.assertEqual(resolve_plot_id_column(plots, None), "Plot_Number")

    def test_missing_override_column_raises_valueerror(self):
        plots = _points_gdf(Plot_Number=["1", "2"])
        with self.assertRaises(ValueError) as ctx:
            resolve_plot_id_column(plots, "Nope")
        self.assertIn("Nope", str(ctx.exception))


class ApplyColumnOverrideTests(SimpleTestCase):
    def test_override_copies_chosen_column_to_std_key(self):
        gdf = _points_gdf(Rate_Actual=[10.0, 20.0])
        out = apply_column_override(gdf, "applied", "Rate_Actual")
        self.assertEqual(list(out["applied"]), [10.0, 20.0])

    def test_noop_when_override_is_falsy(self):
        gdf = _points_gdf(Rate_Actual=[10.0, 20.0])
        out = apply_column_override(gdf, "applied", None)
        self.assertNotIn("applied", out.columns)

    def test_missing_override_column_raises_valueerror(self):
        gdf = _points_gdf(Rate_Actual=[10.0, 20.0])
        with self.assertRaises(ValueError) as ctx:
            apply_column_override(gdf, "applied", "Missing")
        self.assertIn("Missing", str(ctx.exception))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python manage.py test filemanager.tests.test_valid_yield_extractor_columns -v 2`
Expected: FAIL with `ImportError: cannot import name 'resolve_plot_id_column'`.

- [ ] **Step 3: Add the helpers**

In `filemanager/ValidYieldExtractorTool.py`, immediately after the `pick_column` function (ends ~line 116), add:

```python
PLOT_ID_CANDIDATES = ["Plot_Number", "Plot_Num", "Plot_Numbe", "Plot No",
                      "PlotNo", "PlotID", "Plot_ID", "Plot", "plot_number"]


def resolve_plot_id_column(plots, override=None):
    """Return the plots column to use as plot_id.

    A truthy ``override`` wins (and must be a real column); otherwise fall back
    to synonym auto-detection.
    """
    if override:
        if override not in plots.columns:
            raise ValueError(
                f"Plot ID column '{override}' not found in plots layer. "
                f"Available columns: {list(plots.columns)}"
            )
        return override
    return pick_column(plots, PLOT_ID_CANDIDATES)


def apply_column_override(gdf, std_key, override_col):
    """If ``override_col`` is set, copy it into the standardized ``std_key`` column."""
    if override_col:
        if override_col not in gdf.columns:
            raise ValueError(
                f"Column '{override_col}' (for '{std_key}') not found in layer. "
                f"Available columns: {list(gdf.columns)}"
            )
        gdf[std_key] = gdf[override_col]
    return gdf
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python manage.py test filemanager.tests.test_valid_yield_extractor_columns -v 2`
Expected: PASS (6 tests).

- [ ] **Step 5: Wire helpers into `run()`**

In `run()`, replace the plots plot-id detection block (currently ~lines 289-292):

```python
    plot_id_col = pick_column(plots, ["Plot_Number", "Plot_Num", "Plot_Numbe", "Plot No",
                                       "PlotNo", "PlotID", "Plot_ID", "Plot", "plot_number"])
    print(f"Using plots ID column: {plot_id_col}")
    plots = plots.rename(columns={plot_id_col: "plot_id"})
```

with:

```python
    plot_id_col = resolve_plot_id_column(plots, getattr(args, "plot_id_col", None))
    print(f"Using plots ID column: {plot_id_col}")
    plots = plots.rename(columns={plot_id_col: "plot_id"})
```

After the application standardize line (currently `app = standardize_columns(app, APP_MAP)`, ~line 304), add:

```python
    app = apply_column_override(app, "applied", getattr(args, "applied_col", None))
    app = apply_column_override(app, "rxTarget", getattr(args, "target_col", None))
```

After the harvest standardize line (currently `harv = standardize_columns(harv, HARV_MAP)`, ~line 313), add:

```python
    harv = apply_column_override(harv, "clnYield", getattr(args, "yield_col", None))
```

- [ ] **Step 6: Verify the full suite still passes**

Run: `python manage.py test filemanager.tests.test_valid_yield_extractor_columns -v 2`
Expected: PASS (no regression; `run()` edits are covered by the helper contracts).

- [ ] **Step 7: Commit**

```bash
git add filemanager/ValidYieldExtractorTool.py filemanager/tests/test_valid_yield_extractor_columns.py
git commit -m "feat: column overrides in ValidYieldExtractorTool.run"
```

---

### Task 2: Shapefile columns reader + endpoint

Expose a GET endpoint returning a `.shp`'s attribute field names for the dropdowns.

**Files:**
- Modify: `filemanager/views.py` (add `read_shapefile_columns` helper + `valid_yield_extractor_columns` view; near the other valid-yield views ~line 3447)
- Modify: `filemanager/urls.py` (add route after line 72)
- Test: `filemanager/tests/test_valid_yield_extractor_columns_endpoint.py` (new)

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces:
  - `read_shapefile_columns(shp_path: str) -> list[str]` — attribute field names (no geometry), via `fiona`.
  - View `valid_yield_extractor_columns(request, file_id)` at GET `/api/valid-yield-extractor/columns/<file_id>/`, name `filemanager:valid_yield_extractor_columns`. JSON: `{"success": true, "columns": [...]}` or `{"success": false, "error": ...}` with status 404/403/400/500.

- [ ] **Step 1: Write the failing test**

Create `filemanager/tests/test_valid_yield_extractor_columns_endpoint.py`:

```python
import os
import tempfile
from unittest import mock

import geopandas as gpd
from shapely.geometry import Point
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from filemanager.models import File
from filemanager.views import read_shapefile_columns

User = get_user_model()
_MEDIA = tempfile.mkdtemp()


class ReadShapefileColumnsTests(TestCase):
    def test_returns_attribute_field_names(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "plots.shp")
        gpd.GeoDataFrame(
            {"Plot_Number": ["1", "2"], "Rate": [10, 20],
             "geometry": [Point(0, 0), Point(1, 1)]},
            crs="EPSG:4326",
        ).to_file(path)
        cols = read_shapefile_columns(path)
        self.assertIn("Plot_Number", cols)
        self.assertIn("Rate", cols)
        self.assertNotIn("geometry", cols)


@override_settings(MEDIA_ROOT=_MEDIA)
class ColumnsEndpointTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u", password="x")
        self.other = User.objects.create_user(username="o", password="x")
        self.client.force_login(self.user)

    def _make_file(self, name, owner=None, is_public=False):
        return File.objects.create(
            name=name, owner=owner or self.user, is_public=is_public,
            file=SimpleUploadedFile(name, b"dummy"),
        )

    def _url(self, fid):
        return f"/api/valid-yield-extractor/columns/{fid}/"

    @mock.patch("filemanager.views.read_shapefile_columns", return_value=["A", "B"])
    def test_success_returns_columns(self, _m):
        f = self._make_file("plots.shp")
        resp = self.client.get(self._url(f.id))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"success": True, "columns": ["A", "B"]})

    def test_not_shp_returns_400(self):
        f = self._make_file("plots.csv")
        resp = self.client.get(self._url(f.id))
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json()["success"])

    def test_missing_file_returns_404(self):
        resp = self.client.get(self._url("00000000-0000-0000-0000-000000000000"))
        self.assertEqual(resp.status_code, 404)

    def test_permission_denied_for_other_users_private_file(self):
        f = self._make_file("plots.shp", owner=self.other, is_public=False)
        resp = self.client.get(self._url(f.id))
        self.assertEqual(resp.status_code, 403)

    @mock.patch("filemanager.views.read_shapefile_columns", return_value=["A"])
    def test_public_file_of_other_user_allowed(self, _m):
        f = self._make_file("plots.shp", owner=self.other, is_public=True)
        resp = self.client.get(self._url(f.id))
        self.assertEqual(resp.status_code, 200)
```

> Note: `File` id is a UUID in this app; the 404 test uses a well-formed UUID string. If `File.id` is an integer in your schema, use `999999` instead.

- [ ] **Step 2: Run test to verify it fails**

Run: `python manage.py test filemanager.tests.test_valid_yield_extractor_columns_endpoint -v 2`
Expected: FAIL with `ImportError: cannot import name 'read_shapefile_columns'`.

- [ ] **Step 3: Add the helper and view**

In `filemanager/views.py`, just above `def run_valid_yield_extractor(request):` (~line 3447), add:

```python
def read_shapefile_columns(shp_path):
    """Return a shapefile's attribute field names (no geometry, no row load)."""
    import fiona
    with fiona.open(shp_path) as src:
        return list(src.schema["properties"].keys())


@login_required
def valid_yield_extractor_columns(request, file_id):
    """Return the attribute column names of a selected .shp for the column-mapping UI."""
    try:
        file_obj = File.objects.get(id=file_id)
    except File.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'File not found'}, status=404)

    if file_obj.owner != request.user and not file_obj.is_public:
        return JsonResponse({'success': False, 'error': 'Permission denied'}, status=403)

    ext = os.path.splitext(file_obj.name)[1].lower()
    if ext != '.shp':
        return JsonResponse(
            {'success': False, 'error': f'File must be a .shp file. Got: {ext}'},
            status=400,
        )

    try:
        columns = read_shapefile_columns(file_obj.file.path)
    except Exception as e:
        return _internal_error_response(e)

    return JsonResponse({'success': True, 'columns': columns})
```

- [ ] **Step 4: Add the route**

In `filemanager/urls.py`, after the status route (line 72), add:

```python
    path('api/valid-yield-extractor/columns/<str:file_id>/', views.valid_yield_extractor_columns, name='valid_yield_extractor_columns'),
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python manage.py test filemanager.tests.test_valid_yield_extractor_columns_endpoint -v 2`
Expected: PASS (6 tests).

- [ ] **Step 6: Commit**

```bash
git add filemanager/views.py filemanager/urls.py filemanager/tests/test_valid_yield_extractor_columns_endpoint.py
git commit -m "feat: endpoint to read shapefile columns for column mapping"
```

---

### Task 3: Forward column overrides through view and task

Thread the four optional fields from the run POST body into the Celery task and onto the `argparse.Namespace`.

**Files:**
- Modify: `filemanager/views.py` — `run_valid_yield_extractor` (~lines 3473-3555)
- Modify: `filemanager/tasks.py` — `run_valid_yield_extractor_task` signature (~line 2352) and the `argparse.Namespace(...)` build (~lines 2489-2498)
- Test: append to `filemanager/tests/test_valid_yield_extractor_columns_endpoint.py`

**Interfaces:**
- Consumes: `args.plot_id_col / applied_col / target_col / yield_col` read by Task 1's `run()`.
- Produces: `run_valid_yield_extractor_task` accepts keyword params `plot_id_col=None, target_rate_col=None, applied_rate_col=None, yield_col=None`; the view forwards request fields `plot_id_col`, `target_rate_col`, `applied_rate_col`, `yield_col`.

- [ ] **Step 1: Write the failing test**

Append to `filemanager/tests/test_valid_yield_extractor_columns_endpoint.py`:

```python
import json


@override_settings(MEDIA_ROOT=_MEDIA)
class RunForwardsColumnOverridesTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="ru", password="x")
        self.client.force_login(self.user)

    def _mk(self, name):
        return File.objects.create(
            name=name, owner=self.user,
            file=SimpleUploadedFile(name, b"dummy"),
        )

    @mock.patch("filemanager.views.run_valid_yield_extractor_task")
    def test_run_forwards_column_fields_to_task(self, mock_task):
        mock_task.delay.return_value = mock.Mock(id="task-123")
        plots, app, harv = self._mk("p.shp"), self._mk("a.shp"), self._mk("h.shp")
        body = {
            "plots_file_id": str(plots.id),
            "app_file_id": str(app.id),
            "harv_file_id": str(harv.id),
            "plot_id_col": "Plot_Number",
            "target_rate_col": "Tgt",
            "applied_rate_col": "Act",
            "yield_col": "Yld",
        }
        resp = self.client.post(
            "/api/valid-yield-extractor/run/",
            data=json.dumps(body), content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json()["success"])
        _, kwargs = mock_task.delay.call_args
        self.assertEqual(kwargs["plot_id_col"], "Plot_Number")
        self.assertEqual(kwargs["target_rate_col"], "Tgt")
        self.assertEqual(kwargs["applied_rate_col"], "Act")
        self.assertEqual(kwargs["yield_col"], "Yld")

    @mock.patch("filemanager.views.run_valid_yield_extractor_task")
    def test_blank_column_fields_forward_as_none(self, mock_task):
        mock_task.delay.return_value = mock.Mock(id="task-1")
        plots, app, harv = self._mk("p.shp"), self._mk("a.shp"), self._mk("h.shp")
        body = {
            "plots_file_id": str(plots.id),
            "app_file_id": str(app.id),
            "harv_file_id": str(harv.id),
            "plot_id_col": "",
        }
        resp = self.client.post(
            "/api/valid-yield-extractor/run/",
            data=json.dumps(body), content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        _, kwargs = mock_task.delay.call_args
        self.assertIsNone(kwargs["plot_id_col"])
        self.assertIsNone(kwargs["yield_col"])
```

> The view currently imports the task locally (`from .tasks import run_valid_yield_extractor_task`) inside the function. For the patch target `filemanager.views.run_valid_yield_extractor_task` to work, Step 3 moves this import to module level in `views.py`. If a module-level import causes a circular import at startup, keep the local import and change the test patch target to `filemanager.tasks.run_valid_yield_extractor_task` instead.

- [ ] **Step 2: Run test to verify it fails**

Run: `python manage.py test filemanager.tests.test_valid_yield_extractor_columns_endpoint.RunForwardsColumnOverridesTests -v 2`
Expected: FAIL — either `AttributeError` on the patch target or `KeyError`/assertion because the kwargs are not forwarded yet.

- [ ] **Step 3: Forward fields in the view**

In `filemanager/views.py`, ensure the task is importable at module level for patching. Near the top-level imports, add (if not already present):

```python
from .tasks import run_valid_yield_extractor_task
```

and delete the local `from .tasks import run_valid_yield_extractor_task` line inside `run_valid_yield_extractor` (~line 3547). (If this creates a circular import, revert both changes and instead patch `filemanager.tasks.run_valid_yield_extractor_task` in the test.)

In `run_valid_yield_extractor`, after the existing `rate_tolerance = data.get(...)` line (~line 3479), add:

```python
        plot_id_col = data.get('plot_id_col') or None
        target_rate_col = data.get('target_rate_col') or None
        applied_rate_col = data.get('applied_rate_col') or None
        yield_col = data.get('yield_col') or None
```

Replace the existing `.delay(...)` call (~lines 3548-3555) with:

```python
        task = run_valid_yield_extractor_task.delay(
            str(plots_file_id),
            str(app_file_id),
            str(harv_file_id),
            output_folder_id,
            crs,
            rate_tolerance,
            plot_id_col=plot_id_col,
            target_rate_col=target_rate_col,
            applied_rate_col=applied_rate_col,
            yield_col=yield_col,
        )
```

- [ ] **Step 4: Accept and apply params in the task**

In `filemanager/tasks.py`, extend the `run_valid_yield_extractor_task` signature (~line 2352) by adding the four keyword params (keep `requesting_user_id=None` last):

```python
def run_valid_yield_extractor_task(
    self,
    plots_file_id,
    app_file_id,
    harv_file_id,
    output_folder_id=None,
    crs="EPSG:26914",
    rate_tolerance=0.10,
    plot_id_col=None,
    target_rate_col=None,
    applied_rate_col=None,
    yield_col=None,
    requesting_user_id=None,
):
```

In the same task, extend the `argparse.Namespace(...)` build (~lines 2489-2498) by adding the override attributes the algorithm reads:

```python
        args = argparse.Namespace(
            plots=plots_path, app=app_path, harv=harv_path,
            crs=crs, out=output_dir,
            plot_id_col=plot_id_col,
            applied_col=applied_rate_col,
            target_col=target_rate_col,
            yield_col=yield_col,
            plots_wkt=None, plots_easting=None, plots_northing=None,
            plots_lat=None, plots_lon=None, plots_crs_in='EPSG:4326',
            app_wkt=None, app_easting=None, app_northing=None,
            app_lat=None, app_lon=None, app_crs_in='EPSG:4326',
            harv_wkt=None, harv_easting=None, harv_northing=None,
            harv_lat=None, harv_lon=None, harv_crs_in='EPSG:4326',
        )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python manage.py test filemanager.tests.test_valid_yield_extractor_columns_endpoint.RunForwardsColumnOverridesTests -v 2`
Expected: PASS (2 tests).

- [ ] **Step 6: Run the full new-test module**

Run: `python manage.py test filemanager.tests.test_valid_yield_extractor_columns filemanager.tests.test_valid_yield_extractor_columns_endpoint -v 2`
Expected: PASS (all).

- [ ] **Step 7: Commit**

```bash
git add filemanager/views.py filemanager/tasks.py filemanager/tests/test_valid_yield_extractor_columns_endpoint.py
git commit -m "feat: forward column-mapping overrides view -> task -> algorithm"
```

---

### Task 4: Column-mapping dropdowns in the tool UI

Add the four `<select>` menus, populate them from the columns endpoint when files are chosen, and include the choices in the run POST body. No JS test harness exists in this project, so verification is manual in the browser.

**Files:**
- Modify: `templates/filemanager/valid_yield_extractor_tool.html` (markup before `<!-- Parameters Section -->` ~line 349; `selectFile` mode branches ~lines 845-860; submit body ~lines 1148-1155)

**Interfaces:**
- Consumes: GET `filemanager:valid_yield_extractor_columns` (Task 2); run POST fields (Task 3).
- Produces: selects `#plotIdColumn`, `#targetRateColumn`, `#appliedRateColumn`, `#yieldColumn`.

- [ ] **Step 1: Add the column-mapping markup**

In `templates/filemanager/valid_yield_extractor_tool.html`, immediately before the `<!-- Parameters Section -->` comment (~line 349), insert:

```html
                    <!-- Column Mapping Section -->
                    <div class="row">
                        <div class="col-12">
                            <h6 class="fw-bold mb-3">
                                <i class="fas fa-columns me-2 text-secondary"></i>
                                Column mapping
                                <small class="text-muted fw-normal">(auto-detected if left as default)</small>
                            </h6>
                            <div class="parameter-section">
                                <div class="row">
                                    <div class="col-md-6 mb-3">
                                        <label for="plotIdColumn" class="form-label"><strong>Plot ID</strong>
                                            <small class="text-muted">(from treatment plots)</small></label>
                                        <select class="form-select" id="plotIdColumn" name="plot_id_col">
                                            <option value="">— auto-detect —</option>
                                        </select>
                                    </div>
                                    <div class="col-md-6 mb-3">
                                        <label for="targetRateColumn" class="form-label"><strong>Target rate</strong>
                                            <small class="text-muted">(from application data)</small></label>
                                        <select class="form-select" id="targetRateColumn" name="target_rate_col">
                                            <option value="">— auto-detect —</option>
                                        </select>
                                    </div>
                                    <div class="col-md-6 mb-3">
                                        <label for="appliedRateColumn" class="form-label"><strong>Applied rate</strong>
                                            <small class="text-muted">(from application data)</small></label>
                                        <select class="form-select" id="appliedRateColumn" name="applied_rate_col">
                                            <option value="">— auto-detect —</option>
                                        </select>
                                    </div>
                                    <div class="col-md-6 mb-3">
                                        <label for="yieldColumn" class="form-label"><strong>Yield</strong>
                                            <small class="text-muted">(from harvest data)</small></label>
                                        <select class="form-select" id="yieldColumn" name="yield_col">
                                            <option value="">— auto-detect —</option>
                                        </select>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>

```

- [ ] **Step 2: Add the populate helper and triggers**

In the `<script>` block, add a helper near `selectFile` (e.g. just before `function selectOutputFolder`, ~line 865):

```javascript
// Populate column-mapping dropdown(s) from a selected shapefile's columns.
async function loadColumnsForFile(fileId, selectIds) {
    // Reset to just the auto-detect option while loading.
    selectIds.forEach(id => {
        document.getElementById(id).innerHTML = '<option value="">— auto-detect —</option>';
    });
    try {
        const resp = await fetch(`/api/valid-yield-extractor/columns/${fileId}/`);
        const data = await resp.json();
        if (!data.success) { return; }
        selectIds.forEach(id => {
            const sel = document.getElementById(id);
            data.columns.forEach(col => {
                const opt = document.createElement('option');
                opt.value = col;
                opt.textContent = col;   // textContent is XSS-safe
                sel.appendChild(opt);
            });
        });
    } catch (e) {
        console.error('Failed to load columns', e);
    }
}
```

Then extend the `selectFile` mode branches (~lines 845-860). Add one call at the end of each branch's body:

- In the `mode === 'plots'` branch, after `document.getElementById('summaryPlots').textContent = fileName;`:
  ```javascript
        loadColumnsForFile(fileId, ['plotIdColumn']);
  ```
- In the `mode === 'app'` branch, after `document.getElementById('summaryApp').textContent = fileName;`:
  ```javascript
        loadColumnsForFile(fileId, ['targetRateColumn', 'appliedRateColumn']);
  ```
- In the `mode === 'harv'` branch, after `document.getElementById('summaryHarv').textContent = fileName;`:
  ```javascript
        loadColumnsForFile(fileId, ['yieldColumn']);
  ```

- [ ] **Step 3: Include selections in the submit body**

In the submit handler, replace the `body: JSON.stringify({...})` object (~lines 1148-1155) with:

```javascript
            body: JSON.stringify({
                plots_file_id: plotsFileId,
                app_file_id: appFileId,
                harv_file_id: harvFileId,
                output_folder_id: folderId || null,
                crs: crs,
                rate_tolerance: rateTolerance,
                plot_id_col: document.getElementById('plotIdColumn').value || null,
                target_rate_col: document.getElementById('targetRateColumn').value || null,
                applied_rate_col: document.getElementById('appliedRateColumn').value || null,
                yield_col: document.getElementById('yieldColumn').value || null
            })
```

- [ ] **Step 4: Manual verification in the browser**

After deploying/restarting Django, open `https://adma.unl.edu/tools/valid-yield-extractor/` and confirm:
1. Selecting a plots `.shp` populates **Plot ID** with that file's columns; **Plot ID** alone changes (others stay auto-detect).
2. Selecting an application `.shp` populates both **Target rate** and **Applied rate**.
3. Selecting a harvest `.shp` populates **Yield**.
4. Re-selecting a different file repopulates and resets that dropdown to `— auto-detect —`.
5. Leaving all four as auto-detect and running produces the same result as before the change.
6. Picking a specific non-default column and running succeeds and reflects the chosen column.

- [ ] **Step 5: Commit**

```bash
git add templates/filemanager/valid_yield_extractor_tool.html
git commit -m "feat: column-mapping dropdowns in Valid Yield Extractor UI"
```

---

## Notes for the implementer

- The columns endpoint is GET and needs no CSRF token. The run POST relies on the app-wide AJAX CSRF cookie (now readable by JS after the earlier `CSRF_COOKIE_HTTPONLY = False` fix) — no extra handling here.
- Keep `getattr(args, ..., None)` in `run()`: direct CLI/interactive callers build their own `Namespace` without these attributes and must keep working.
- After all tasks, run the broader suite to confirm no regressions:
  `python manage.py test filemanager -v 1`
