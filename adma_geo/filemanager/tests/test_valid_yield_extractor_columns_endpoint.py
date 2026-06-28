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
