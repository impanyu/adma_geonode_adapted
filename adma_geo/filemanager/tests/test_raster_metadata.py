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

    def test_a_crs_without_an_epsg_code_still_fits_the_column(self):
        """
        File.crs is 50 characters. SoilGrids ships Homolosine, which has no
        EPSG code and a PROJ string far longer than that -- writing it raw
        made the whole processing task die on a database error.
        """
        import rasterio

        homolosine = rasterio.crs.CRS.from_proj4(
            '+proj=igh +lat_0=0 +lon_0=0 +datum=WGS84 +units=m +no_defs'
        )
        file_obj, path = self._raster('soil.tif', homolosine,
                                      origin=(-10857928.0, 4545172.0), pixel=250.0)

        ok, message = process_raster_file(file_obj, path)

        self.assertTrue(ok, message)
        self.assertLessEqual(len(file_obj.crs), 50)
        self.assertIn('igh', file_obj.crs)
        # The full definition is not lost, just moved somewhere it fits.
        self.assertIn('+proj=igh', file_obj.processing_log)

        file_obj.refresh_from_db()
        self.assertEqual(file_obj.gis_status, 'processed')


@override_settings(MEDIA_ROOT=MEDIA)
class VectorMetadataTests(TestCase):
    """process_vector_file assumed EPSG:4326 and a whole-world envelope."""

    def setUp(self):
        self.user = User.objects.create_user('vgrower', password='x')
        self.folder = Folder.objects.create(name='vectors', owner=self.user)
        self.directory = os.path.join(MEDIA, 'uploads', self.folder.get_full_path())
        os.makedirs(self.directory, exist_ok=True)

    def _layer(self, name, crs, geometry=None):
        import geopandas as gpd
        from shapely.geometry import Point

        path = os.path.join(self.directory, name)
        gpd.GeoDataFrame(
            {'plot': ['a', 'b']},
            geometry=geometry or [Point(500000, 4500000), Point(500300, 4500300)],
            crs=crs,
        ).to_file(path)

        file_obj = File(name=name, folder=self.folder, owner=self.user,
                        file_size=os.path.getsize(path))
        file_obj.file.name = os.path.relpath(path, MEDIA)
        file_obj.save()
        return file_obj, path

    def test_the_crs_is_read_not_assumed(self):
        from filemanager.gis_utils import process_vector_file

        file_obj, path = self._layer('plots.shp', 'EPSG:32614')

        ok, message = process_vector_file(file_obj, path)

        self.assertTrue(ok, message)
        self.assertEqual(file_obj.crs, 'EPSG:32614')

    def test_the_extent_is_the_layer_not_the_world(self):
        from filemanager.gis_utils import process_vector_file

        file_obj, path = self._layer('plots.shp', 'EPSG:32614')

        process_vector_file(file_obj, path)

        (west, south), (east, north) = json.loads(file_obj.spatial_extent)['coordinates']
        self.assertNotEqual([west, south, east, north], [-180, -90, 180, 90])
        # Easting 500000 in zone 14N is the central meridian itself, -99.
        self.assertAlmostEqual(west, -99.0, places=4)
        self.assertTrue(40 < south < 41, south)
        self.assertLess(west, east)
        self.assertLess(south, north)


@override_settings(MEDIA_ROOT=MEDIA)
class CsvMetadataTests(TestCase):
    """process_csv_file recorded a whole-world envelope for any CSV at all."""

    def setUp(self):
        self.user = User.objects.create_user('cgrower', password='x')
        self.folder = Folder.objects.create(name='tables', owner=self.user)
        self.directory = os.path.join(MEDIA, 'uploads', self.folder.get_full_path())
        os.makedirs(self.directory, exist_ok=True)

    def _csv(self, name, text):
        path = os.path.join(self.directory, name)
        with open(path, 'w') as handle:
            handle.write(text)
        file_obj = File(name=name, folder=self.folder, owner=self.user,
                        file_size=os.path.getsize(path))
        file_obj.file.name = os.path.relpath(path, MEDIA)
        file_obj.save()
        return file_obj, path

    def test_the_extent_comes_from_the_coordinates(self):
        from filemanager.gis_utils import process_csv_file

        file_obj, path = self._csv('points.csv', (
            'latitude,longitude,yield\n'
            '40.80,-96.70,180\n'
            '40.85,-96.65,190\n'
        ))

        ok, message = process_csv_file(file_obj, path)

        self.assertTrue(ok, message)
        (west, south), (east, north) = json.loads(file_obj.spatial_extent)['coordinates']
        self.assertAlmostEqual(west, -96.70, places=4)
        self.assertAlmostEqual(north, 40.85, places=4)

    def test_a_csv_with_no_coordinates_is_not_a_map_layer(self):
        from filemanager.gis_utils import process_csv_file

        file_obj, path = self._csv('weather.csv', 'date,tmax\n2024-06-01,25.1\n')

        ok, message = process_csv_file(file_obj, path)

        self.assertFalse(ok)
        self.assertEqual(file_obj.gis_status, 'error')
        self.assertIn('No latitude/longitude columns', file_obj.processing_log)

    def test_projected_metres_are_not_taken_for_degrees(self):
        """x/y columns are often metres; guessing would misplace the data."""
        from filemanager.gis_utils import process_csv_file

        file_obj, path = self._csv('utm.csv', (
            'x,y,yield\n'
            '712000,4560000,180\n'
            '713000,4561000,190\n'
        ))

        ok, message = process_csv_file(file_obj, path)

        self.assertFalse(ok)
        self.assertIn('not lat/lon degrees', file_obj.processing_log)


@override_settings(MEDIA_ROOT=MEDIA)
class MapViewerExtentTests(TestCase):
    """
    The map viewer reads spatial_extent, which process_raster_file stores in
    WGS84 whatever the file's own CRS is. Transforming it from the file's
    native CRS instead read those degrees as metres, and threw outright on a
    projection OpenLayers cannot name -- a MODIS layer is sinusoidal and has
    no EPSG code, so the uncaught error took the whole map down, basemap
    included.
    """

    def setUp(self):
        self.user = User.objects.create_user('mapper', password='correct-horse-battery')
        self.client.login(username='mapper', password='correct-horse-battery')
        self.folder = Folder.objects.create(name='rasters', owner=self.user)
        self.directory = os.path.join(MEDIA, 'uploads', self.folder.get_full_path())
        os.makedirs(self.directory, exist_ok=True)

    def _published(self, name, crs_text):
        import numpy as np
        import rasterio
        from rasterio.transform import from_origin

        path = os.path.join(self.directory, name)
        with rasterio.open(
            path, 'w', driver='GTiff', height=4, width=4, count=1, dtype='uint8',
            crs='EPSG:4326', transform=from_origin(-96.75, 40.83, 0.01, 0.01),
        ) as destination:
            destination.write(np.ones((4, 4), dtype='uint8'), 1)

        file_obj = File(name=name, folder=self.folder, owner=self.user,
                        file_size=os.path.getsize(path))
        file_obj.file.name = os.path.relpath(path, MEDIA)
        file_obj.save()
        file_obj.crs = crs_text
        file_obj.gis_status = 'published'
        file_obj.geoserver_layer_name = 'adma_geo_test_layer'
        file_obj.geoserver_workspace = 'adma_geo'
        file_obj.spatial_extent = json.dumps({
            'type': 'envelope',
            'coordinates': [[-96.75, 40.79], [-96.71, 40.83]],
        })
        file_obj.save()
        return file_obj

    def test_the_extent_is_transformed_from_wgs84_not_the_files_crs(self):
        for crs_text in ('EPSG:4326', 'EPSG:32614', 'PROJ:sinu', 'PROJ:aea'):
            with self.subTest(crs=crs_text):
                file_obj = self._published(f'r_{crs_text.replace(":", "_")}.tif', crs_text)

                html = self.client.get(f'/file/{file_obj.id}/map/').content.decode()

                self.assertIn("'EPSG:4326',", html)
                # The file's own CRS must not be handed to OpenLayers: it
                # cannot resolve PROJ:sinu and throws on null.
                self.assertNotIn(f"transformExtent(\n", html.replace(' ', ''))
                self.assertNotIn('sourceCRS', html)

    def test_no_hand_rolled_utm_conversion_remains(self):
        """It approximated zone 14N in JavaScript, for one part of Nebraska."""
        file_obj = self._published('utm.tif', 'EPSG:32614')

        html = self.client.get(f'/file/{file_obj.id}/map/').content.decode()

        self.assertNotIn('utmToLonLat', html)
        self.assertNotIn('centralMeridian', html)
