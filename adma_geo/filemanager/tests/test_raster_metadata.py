"""
Reading a raster's real CRS and extent.

process_raster_file used to guess from the filename and write a fixed envelope
around one file the author had, so every raster on the platform claimed the
same few hundred metres of Nebraska and would not open where it belonged.
"""
import json
import os
import shutil
import tempfile

import numpy as np
import rasterio
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rasterio.transform import from_origin

from filemanager.gis_utils import process_raster_file
from filemanager.models import File, Folder

User = get_user_model()
MEDIA = tempfile.mkdtemp(prefix='adma-raster-')


@override_settings(MEDIA_ROOT=MEDIA)
class RasterMetadataTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(MEDIA, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.user = User.objects.create_user('grower', password='x')
        self.folder = Folder.objects.create(name='rasters', owner=self.user)
        self.directory = os.path.join(MEDIA, 'uploads', self.folder.get_full_path())
        os.makedirs(self.directory, exist_ok=True)

    def _raster(self, name, crs, origin=(500000.0, 4500000.0), pixel=30.0):
        path = os.path.join(self.directory, name)
        with rasterio.open(
            path, 'w', driver='GTiff', height=10, width=10, count=1,
            dtype='uint8', crs=crs, transform=from_origin(*origin, pixel, pixel),
        ) as destination:
            destination.write(np.ones((10, 10), dtype='uint8'), 1)

        file_obj = File(name=name, folder=self.folder, owner=self.user,
                        file_size=os.path.getsize(path))
        file_obj.file.name = os.path.relpath(path, MEDIA)
        file_obj.save()
        return file_obj, path

    def test_the_crs_is_read_from_the_file_not_the_name(self):
        """The old code keyed off the filename; this one says 5070, not UTM."""
        file_obj, path = self._raster('cdl_2021.tif', 'EPSG:5070',
                                      origin=(-62777.0, 1980202.0))

        ok, message = process_raster_file(file_obj, path)

        self.assertTrue(ok, message)
        self.assertIn('5070', file_obj.crs)
        self.assertNotIn('32614', file_obj.crs)

    def test_the_extent_comes_from_the_raster_in_wgs84(self):
        file_obj, path = self._raster('cdl_2021.tif', 'EPSG:5070',
                                      origin=(-62777.0, 1980202.0))

        process_raster_file(file_obj, path)

        extent = json.loads(file_obj.spatial_extent)
        (west, south), (east, north) = extent['coordinates']
        # That Albers origin is central Nebraska.
        self.assertTrue(-99 < west < -95, west)
        self.assertTrue(39 < south < 43, south)
        self.assertLess(west, east)
        self.assertLess(south, north)

    def test_two_different_rasters_do_not_share_one_extent(self):
        """They used to: the envelope was hardcoded."""
        first, first_path = self._raster('a.tif', 'EPSG:4326',
                                         origin=(-96.7, 40.8), pixel=0.001)
        second, second_path = self._raster('b.tif', 'EPSG:4326',
                                           origin=(-120.5, 47.2), pixel=0.001)

        process_raster_file(first, first_path)
        process_raster_file(second, second_path)

        self.assertNotEqual(first.spatial_extent, second.spatial_extent)

    def test_a_raster_without_a_crs_is_an_error_not_a_guess(self):
        file_obj, path = self._raster('no_crs.tif', None)

        ok, message = process_raster_file(file_obj, path)

        self.assertFalse(ok)
        self.assertEqual(file_obj.gis_status, 'error')
        self.assertIn('no coordinate reference system', file_obj.processing_log)
        # Crucially it did not invent one.
        self.assertFalse(file_obj.crs)
