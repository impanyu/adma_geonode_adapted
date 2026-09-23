"""
The whole chain for one tool: real files on disk, the real Celery task run
eagerly, real File rows registered for what it wrote.

The module tests prove the maths and the view tests prove the validation.
What neither covers is the wiring between them -- output folder resolution,
path handling, and whether the shapefile sidecars actually get registered --
which is where a tool that passes every other test still hands the user a
folder they cannot use.
"""
import os
import shutil
import tempfile

import geopandas as gpd
import numpy as np
import rasterio
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rasterio.transform import from_origin
from shapely.geometry import Point, box

from filemanager.models import File, Folder
from filemanager.native_tool_tasks import (
    run_management_zones_task,
    run_zonal_statistics_task,
)

User = get_user_model()
CRS = 'EPSG:32614'

MEDIA = tempfile.mkdtemp(prefix='adma-e2e-')


@override_settings(MEDIA_ROOT=MEDIA)
class EndToEndTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(MEDIA, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.user = User.objects.create_user('grower', password='x')
        self.folder = Folder.objects.create(name='field-7', owner=self.user)
        self.directory = os.path.join(MEDIA, 'uploads', self.folder.get_full_path())
        os.makedirs(self.directory, exist_ok=True)

    def _register(self, absolute_path):
        """A File row pointing at a file that really exists, as upload would."""
        file_obj = File(
            name=os.path.basename(absolute_path),
            folder=self.folder,
            owner=self.user,
            file_size=os.path.getsize(absolute_path),
        )
        file_obj.file.name = os.path.relpath(absolute_path, MEDIA)
        file_obj.save()
        return file_obj

    def _raster(self, name='ndre.tif'):
        path = os.path.join(self.directory, name)
        values = np.zeros((10, 10), dtype='float32')
        values[:, :5] = 10.0
        values[:, 5:] = 20.0
        with rasterio.open(
            path, 'w', driver='GTiff', height=10, width=10, count=1,
            dtype='float32', crs=CRS, transform=from_origin(0.0, 100.0, 1.0, 1.0),
        ) as destination:
            destination.write(values, 1)
        return self._register(path)

    def _zones(self, name='plots.shp'):
        path = os.path.join(self.directory, name)
        gpd.GeoDataFrame(
            {'plot': ['west', 'east']},
            geometry=[box(0.5, 90.5, 4.5, 99.5), box(5.5, 90.5, 9.5, 99.5)],
            crs=CRS,
        ).to_file(path)
        return self._register(path)

    def test_zonal_statistics_writes_files_and_registers_them(self):
        zones = self._zones()
        raster = self._raster()

        result = run_zonal_statistics_task.apply(
            args=[str(zones.id), str(raster.id)],
            kwargs={'requesting_user_id': self.user.id},
        ).get()

        self.assertTrue(result['success'], result.get('error'))

        names = {f['name'] for f in result['created_files']}
        self.assertIn('plots_zonalstats.csv', names)
        # A .shp alone is unusable; the sidecars have to be registered too.
        for extension in ('.shp', '.shx', '.dbf', '.prj'):
            self.assertTrue(
                any(n.endswith(extension) for n in names),
                f'no {extension} was registered: {sorted(names)}',
            )

        # The rows must exist in the database, in a folder, pointing at
        # something real -- not just be reported by the task.
        output_folder = Folder.objects.get(id=result['output_folder_id'])
        self.assertEqual(output_folder.owner, self.user)
        self.assertEqual(output_folder.parent, self.folder)

        for entry in result['created_files']:
            row = File.objects.get(id=entry['id'])
            self.assertEqual(row.folder, output_folder)
            self.assertTrue(os.path.exists(row.file.path), row.file.name)
            self.assertGreater(row.file_size, 0)

        csv_row = File.objects.get(name='plots_zonalstats.csv')
        table = gpd.pd.read_csv(csv_row.file.path)
        by_plot = table.set_index('plot')
        self.assertAlmostEqual(by_plot.loc['west', 'val_mean'], 10.0, places=5)
        self.assertAlmostEqual(by_plot.loc['east', 'val_mean'], 20.0, places=5)

    def test_a_second_run_updates_rather_than_duplicates(self):
        zones = self._zones()
        raster = self._raster()
        kwargs = {'requesting_user_id': self.user.id}

        first = run_zonal_statistics_task.apply(
            args=[str(zones.id), str(raster.id)], kwargs=kwargs).get()
        second = run_zonal_statistics_task.apply(
            args=[str(zones.id), str(raster.id)], kwargs=kwargs).get()

        self.assertTrue(second['success'], second.get('error'))
        self.assertEqual(first['output_folder_id'], second['output_folder_id'])
        self.assertTrue(all(f['updated'] for f in second['created_files']))
        self.assertEqual(File.objects.filter(name='plots_zonalstats.csv').count(), 1)

    def test_management_zones_runs_end_to_end(self):
        path = os.path.join(self.directory, 'yield.shp')
        rng = np.random.default_rng(0)
        gpd.GeoDataFrame(
            {'yld': np.concatenate([rng.normal(10, .3, 40), rng.normal(50, .3, 40)])},
            geometry=[Point(float(i % 8), float(i // 8)) for i in range(80)],
            crs=CRS,
        ).to_file(path)
        source = self._register(path)

        result = run_management_zones_task.apply(
            args=[str(source.id), ['yld']],
            kwargs={'zone_count': 2, 'requesting_user_id': self.user.id},
        ).get()

        self.assertTrue(result['success'], result.get('error'))
        names = {f['name'] for f in result['created_files']}
        self.assertTrue(any(n.endswith('_map.png') for n in names), sorted(names))
        self.assertTrue(any(n.endswith('_summary.csv') for n in names), sorted(names))

    def test_another_users_file_is_refused_inside_the_task_too(self):
        """The view checks this; the task checks it again for replayed ids."""
        zones = self._zones()
        raster = self._raster()
        intruder = User.objects.create_user('intruder', password='x')

        result = run_zonal_statistics_task.apply(
            args=[str(zones.id), str(raster.id)],
            kwargs={'requesting_user_id': intruder.id},
        ).get()

        self.assertFalse(result['success'])
        self.assertIn('Permission denied', result['error'])
