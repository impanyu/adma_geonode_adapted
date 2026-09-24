"""
The public dataset fetchers and the tool around them.

Nothing here touches the network: the fetchers are checked for the validation
they do before reaching out, and the task is checked for where it puts what
comes back. The live services were verified separately -- what these pin down
is the behaviour that would otherwise drift silently, above all that results
land somewhere the Third-Party Data panel actually looks.
"""
import os
import shutil
import tempfile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from filemanager.models import File, Folder
from filemanager.native_tool_tasks import (
    _area_of_interest,
    run_public_data_fetch_task,
)
from filemanager.public_datasets import (
    MAX_BBOX_DEGREES,
    PUBLIC_DATASETS,
    DatasetError,
    check_bbox,
    fetch_dataset,
)
from filemanager.templatetags.third_party import source_icon, source_label

User = get_user_model()
MEDIA = tempfile.mkdtemp(prefix='adma-pd-')


class RegistryTests(TestCase):
    def test_every_dataset_is_fully_described(self):
        self.assertTrue(PUBLIC_DATASETS)
        for key, dataset in PUBLIC_DATASETS.items():
            with self.subTest(dataset=key):
                self.assertEqual(dataset.key, key)
                self.assertIn(dataset.kind, {'raster', 'table'})
                self.assertIn(dataset.needs, {'bbox', 'point'})
                self.assertTrue(dataset.name and dataset.description)
                self.assertTrue(callable(dataset.fetch))
                for option in dataset.options:
                    self.assertIn('name', option)
                    self.assertIn('label', option)
                    self.assertIn(option['type'], {'select', 'date'})

    def test_an_unknown_dataset_lists_what_exists(self):
        with self.assertRaises(DatasetError) as caught:
            fetch_dataset('nope', {}, '/tmp')
        self.assertIn('usda_cdl', str(caught.exception))


class BoundingBoxTests(TestCase):
    def test_a_sane_box_is_accepted(self):
        check_bbox((-96.75, 40.78, -96.68, 40.83))

    def test_an_inverted_box_is_refused(self):
        with self.assertRaises(DatasetError):
            check_bbox((-96.68, 40.78, -96.75, 40.83))

    def test_an_oversized_box_is_refused_before_any_download(self):
        big = MAX_BBOX_DEGREES + 1
        with self.assertRaises(DatasetError) as caught:
            check_bbox((0.0, 0.0, big, big))
        self.assertIn('smaller', str(caught.exception))

    def test_coordinates_off_the_planet_are_refused(self):
        with self.assertRaises(DatasetError):
            check_bbox((-200.0, 0.0, -199.0, 1.0))


class AreaOfInterestTests(TestCase):
    def test_a_point_gets_a_box_so_rasters_have_something_to_clip(self):
        aoi = _area_of_interest(None, lat=40.8, lon=-96.7)

        self.assertAlmostEqual(aoi['lat'], 40.8)
        self.assertAlmostEqual(aoi['lon'], -96.7)
        minx, miny, maxx, maxy = aoi['bbox']
        self.assertLess(minx, maxx)
        self.assertLess(miny, maxy)

    def test_a_box_yields_its_own_centre_as_the_point(self):
        aoi = _area_of_interest(None, bbox=(-97.0, 40.0, -96.0, 41.0))

        self.assertAlmostEqual(aoi['lat'], 40.5)
        self.assertAlmostEqual(aoi['lon'], -96.5)

    def test_nothing_at_all_is_refused(self):
        from filemanager.native_tool_tasks import InputError
        with self.assertRaises(InputError):
            _area_of_interest(None)


@override_settings(MEDIA_ROOT=MEDIA)
class FetchTaskTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(MEDIA, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.user = User.objects.create_user('grower', password='x')

    def _fake_fetch(self, directory, **_):
        path = os.path.join(directory, 'weather.csv')
        with open(path, 'w') as handle:
            handle.write('date,tmax\n2024-06-01,25.1\n')
        return True, 'fetched', {'weather_csv': path}

    def run_fetch(self, dataset='open_meteo', **kwargs):
        dataset_obj = PUBLIC_DATASETS[dataset]
        with patch.object(
            dataset_obj, 'fetch',
            side_effect=lambda aoi, directory, **p: self._fake_fetch(directory, **p),
        ):
            return run_public_data_fetch_task.apply(
                args=[dataset],
                kwargs={'lat': 40.8, 'lon': -96.7,
                        'requesting_user_id': self.user.id, **kwargs},
            ).get()

    def test_results_land_where_the_panel_looks_for_them(self):
        """
        The panel lists third-party folders with parent=None. Output buried in
        a subfolder would be saved but invisible, which defeats the point.
        """
        result = self.run_fetch()

        self.assertTrue(result['success'], result.get('error'))
        folder = Folder.objects.get(id=result['output_folder_id'])
        self.assertIsNone(folder.parent)
        self.assertTrue(folder.is_third_party)
        self.assertEqual(folder.third_party_source, 'open_meteo')

    def test_the_file_is_tagged_with_its_source(self):
        result = self.run_fetch()

        row = File.objects.get(id=result['created_files'][0]['id'])
        self.assertTrue(row.is_third_party)
        self.assertEqual(row.third_party_source, 'open_meteo')
        self.assertTrue(os.path.exists(row.file.path))

    def test_each_source_gets_its_own_folder(self):
        self.run_fetch(dataset='open_meteo')
        self.run_fetch(dataset='nasa_power')

        sources = set(
            Folder.objects.filter(owner=self.user, is_third_party=True)
            .values_list('third_party_source', flat=True)
        )
        self.assertEqual(sources, {'open_meteo', 'nasa_power'})

    def test_a_dataset_error_is_reported_not_raised(self):
        dataset = PUBLIC_DATASETS['open_meteo']
        with patch.object(dataset, 'fetch', side_effect=DatasetError('service is down')):
            result = run_public_data_fetch_task.apply(
                args=['open_meteo'],
                kwargs={'lat': 40.8, 'lon': -96.7, 'requesting_user_id': self.user.id},
            ).get()

        self.assertFalse(result['success'])
        self.assertIn('service is down', result['error'])


class EndpointTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('grower', password='correct-horse-battery')
        self.client.login(username='grower', password='correct-horse-battery')

    def post(self, payload):
        return self.client.post(
            reverse('filemanager:run_public_data_fetch'),
            data=payload, content_type='application/json',
        )

    def test_the_page_renders_with_every_dataset(self):
        response = self.client.get(reverse('filemanager:public_data_tool'))

        self.assertEqual(response.status_code, 200)
        for dataset in PUBLIC_DATASETS.values():
            self.assertContains(response, dataset.name)
        self.assertNotContains(response, '{#')

    def test_an_unauthenticated_call_gets_json(self):
        self.client.logout()
        response = self.post({})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response['Content-Type'], 'application/json')

    def test_no_area_at_all_is_refused(self):
        response = self.post({'dataset': 'open_meteo'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('boundary', response.json()['error'])

    def test_an_unknown_dataset_is_refused(self):
        response = self.post({'dataset': 'nope', 'lat': 40.8, 'lon': -96.7})
        self.assertEqual(response.status_code, 400)

    def test_coordinates_off_the_planet_are_refused(self):
        response = self.post({'dataset': 'open_meteo', 'lat': 999, 'lon': -96.7})
        self.assertEqual(response.status_code, 400)
        self.assertIn('Earth', response.json()['error'])

    @patch('filemanager.native_tool_tasks.run_public_data_fetch_task.delay')
    def test_only_declared_options_reach_the_fetcher(self, delay):
        """A request should not be able to pass arbitrary keyword arguments."""
        delay.return_value.id = 'task-1'

        response = self.post({
            'dataset': 'soilgrids',
            'bbox': [-96.75, 40.78, -96.68, 40.83],
            'params': {'soil_property': 'clay', 'output_dir': '/etc', 'rogue': 1},
        })

        self.assertEqual(response.status_code, 200)
        passed = delay.call_args.kwargs['params']
        self.assertEqual(passed, {'soil_property': 'clay'})


class SourceLabelTests(TestCase):
    def test_known_sources_read_properly(self):
        self.assertEqual(source_label('usda_cdl'), 'USDA Cropland Data Layer')
        self.assertEqual(source_label('johndeere'), 'John Deere')
        self.assertEqual(source_icon('nasa_power'), 'fa-satellite')

    def test_an_unknown_source_is_tidied_rather_than_shown_raw(self):
        self.assertEqual(source_label('some_new_source'), 'Some New Source')
        self.assertEqual(source_icon('some_new_source'), 'fa-cloud')

    def test_a_missing_source_falls_back(self):
        self.assertEqual(source_label(''), 'External')
        self.assertEqual(source_label(None), 'External')
