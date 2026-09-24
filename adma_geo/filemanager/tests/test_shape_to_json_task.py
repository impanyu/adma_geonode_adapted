"""
Characterisation tests for run_shape_to_json_task.

The five original tool tasks each carry their own copy of the
"work out the output folder, then register what was written" block -- six
copies in tasks.py, none of them covered by a test. These pin the behaviour
of the simplest one so it can be moved onto the shared tool_io helpers
without guessing at what the current code does.
"""
import os
import shutil
import tempfile

import geopandas as gpd
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from shapely.geometry import Point

from filemanager.models import File, Folder
from filemanager.tasks import run_shape_to_json_task

User = get_user_model()
CRS = 'EPSG:32614'

MEDIA = tempfile.mkdtemp(prefix='adma-s2j-')


@override_settings(MEDIA_ROOT=MEDIA)
class ShapeToJsonTaskTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(MEDIA, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.user = User.objects.create_user('grower', password='x')
        self.folder = Folder.objects.create(name='field-3', owner=self.user)
        self.directory = os.path.join(MEDIA, 'uploads', self.folder.get_full_path())
        os.makedirs(self.directory, exist_ok=True)

        path = os.path.join(self.directory, 'points.shp')
        gpd.GeoDataFrame(
            {'yield': [1.0, 2.0]},
            geometry=[Point(0.0, 0.0), Point(1.0, 1.0)],
            crs=CRS,
        ).to_file(path)

        self.source = File(
            name='points.shp', folder=self.folder, owner=self.user,
            file_size=os.path.getsize(path),
        )
        self.source.file.name = os.path.relpath(path, MEDIA)
        self.source.save()

    def run_task(self, **kwargs):
        return run_shape_to_json_task.apply(
            args=[str(self.source.id)], kwargs=kwargs
        ).get()

    def test_it_writes_geojson_and_registers_it(self):
        result = self.run_task()

        self.assertTrue(result['success'], result.get('error'))
        self.assertEqual(len(result['created_files']), 1)

        row = File.objects.get(id=result['created_files'][0]['id'])
        self.assertTrue(row.name.endswith('.geojson'))
        self.assertTrue(os.path.exists(row.file.path))
        self.assertGreater(row.file_size, 0)

    def test_the_default_output_folder_sits_under_the_input(self):
        self.run_task()

        output = Folder.objects.get(name='geojson_output')
        self.assertEqual(output.parent, self.folder)
        self.assertEqual(output.owner, self.user)

    def test_a_second_run_reuses_the_folder_and_updates_the_file(self):
        self.run_task()
        second = self.run_task()

        self.assertTrue(second['success'], second.get('error'))
        self.assertEqual(Folder.objects.filter(name='geojson_output').count(), 1)
        self.assertEqual(File.objects.filter(name__endswith='.geojson').count(), 1)
        self.assertTrue(second['created_files'][0]['updated'])

    def test_output_is_reprojected_to_wgs84(self):
        """GeoJSON is defined in WGS84; the task documents converting to it."""
        result = self.run_task()

        row = File.objects.get(id=result['created_files'][0]['id'])
        written = gpd.read_file(row.file.path)
        self.assertEqual(written.crs.to_epsg(), 4326)

    def test_a_chosen_output_folder_is_honoured(self):
        chosen = Folder.objects.create(name='exports', owner=self.user)

        result = self.run_task(output_dir_id=str(chosen.id))

        self.assertTrue(result['success'], result.get('error'))
        row = File.objects.get(id=result['created_files'][0]['id'])
        self.assertEqual(row.folder, chosen)
        self.assertFalse(Folder.objects.filter(name='geojson_output').exists())

    def test_a_non_shapefile_is_refused(self):
        self.source.name = 'points.csv'
        self.source.save(update_fields=['name'])

        result = self.run_task()

        self.assertFalse(result['success'])
        self.assertIn('.shp', result['error'])

    def test_another_users_file_is_refused_when_the_caller_is_named(self):
        intruder = User.objects.create_user('intruder', password='x')

        result = self.run_task(requesting_user_id=intruder.id)

        self.assertFalse(result['success'])
        self.assertIn('Permission denied', result['error'])
