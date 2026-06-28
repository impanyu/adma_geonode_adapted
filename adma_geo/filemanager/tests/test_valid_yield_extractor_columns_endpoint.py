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
