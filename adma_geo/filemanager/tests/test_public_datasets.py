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
                self.assertIn(dataset.kind, {'raster', 'table', 'vector'})
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


class CroplandPaletteTests(TestCase):
    """
    The Cropland Data Layer's pixel values are class codes, not brightness.
    Without the palette USDA ships inside the GeoTIFF, every crop renders as a
    shade of grey and the raster tells the reader nothing.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='adma-cdl-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_the_clip_carries_the_palette_across(self):
        import numpy as np
        import rasterio
        from rasterio.transform import from_origin

        # A stand-in for the USDA source: paletted, with the real colours for
        # corn, soybeans and water.
        source_path = os.path.join(self.tmp, 'source.tif')
        palette = {
            1: (255, 210, 0, 255),      # corn
            5: (37, 111, 0, 255),       # soybeans
            111: (74, 111, 162, 255),   # open water
        }
        values = np.array([[1, 5], [111, 1]], dtype='uint8')
        with rasterio.open(
            source_path, 'w', driver='GTiff', height=2, width=2, count=1,
            dtype='uint8', crs='EPSG:5070',
            transform=from_origin(-62777.0, 1980202.0, 30.0, 30.0),
        ) as destination:
            destination.write(values, 1)
            destination.write_colormap(1, palette)

        # Re-clip it the way fetch_cdl does, and check the palette survives.
        out_path = os.path.join(self.tmp, 'clip.tif')
        with rasterio.open(source_path) as source:
            data = source.read(1)
            profile = source.profile.copy()
            carried = source.colormap(1)
        with rasterio.open(out_path, 'w', **profile) as destination:
            destination.write(data, 1)
            destination.write_colormap(1, carried)

        with rasterio.open(out_path) as written:
            self.assertEqual(written.colorinterp[0], rasterio.enums.ColorInterp.palette)
            result = written.colormap(1)

        for code, colour in palette.items():
            self.assertEqual(result[code], colour, f'class {code} lost its colour')

    def test_fetch_cdl_asks_for_the_palette(self):
        """Guard the call itself: dropping it is silent and only shows on a map."""
        import inspect

        from filemanager.public_datasets import fetch_cdl

        body = inspect.getsource(fetch_cdl)
        self.assertIn('colormap(1)', body)
        self.assertIn('write_colormap', body)


class DbfNameTests(TestCase):
    """
    A shapefile's .dbf caps field names at 10 characters. Truncating without
    then making them unique either loses a column silently or, in recent
    geopandas, fails the write outright -- which is how the watershed fetcher
    first broke.
    """

    def test_long_names_are_shortened_and_kept_distinct(self):
        from filemanager.dbf_names import unique_dbf_name

        taken = set()
        first = unique_dbf_name('soil_organic_carbon', taken)
        second = unique_dbf_name('soil_organic_matter', taken)

        self.assertEqual(first, 'soil_organ')
        self.assertNotEqual(first, second)
        self.assertLessEqual(len(second), 10)

    def test_a_frame_with_colliding_columns_can_be_written(self):
        import geopandas as gpd
        from shapely.geometry import Point

        from filemanager.dbf_names import dbf_safe_columns

        frame = gpd.GeoDataFrame(
            {'areaacres_total': [1], 'areaacres_public': [2],
             'areaacres_private': [3]},
            geometry=[Point(0, 0)], crs='EPSG:4326',
        )

        safe = dbf_safe_columns(frame)
        attributes = [c for c in safe.columns if c != safe.geometry.name]

        self.assertEqual(len(attributes), 3)
        self.assertEqual(len(set(attributes)), 3)
        self.assertTrue(all(len(c) <= 10 for c in attributes), attributes)

        with tempfile.TemporaryDirectory() as tmp:
            safe.to_file(os.path.join(tmp, 'ok.shp'))

    def test_the_geometry_column_is_left_alone(self):
        import geopandas as gpd
        from shapely.geometry import Point

        from filemanager.dbf_names import dbf_safe_columns

        frame = gpd.GeoDataFrame({'a': [1]}, geometry=[Point(0, 0)], crs='EPSG:4326')
        self.assertEqual(dbf_safe_columns(frame).geometry.name, 'geometry')


class OverpassFallbackTests(TestCase):
    """Overpass instances are shared and often busy; one being down is normal."""

    def test_more_than_one_instance_is_tried(self):
        from filemanager.public_datasets import ALLOWED_HOSTS, OVERPASS_URLS
        import urllib.parse

        self.assertGreater(len(OVERPASS_URLS), 1)
        for url in OVERPASS_URLS:
            host = urllib.parse.urlparse(url).hostname
            # A fallback the host allowlist refuses is not a fallback.
            self.assertIn(host, ALLOWED_HOSTS, host)


class CroplandMaskTests(TestCase):
    """
    USDA gives all four developed classes the same grey, so a town reads as one
    flat block that can swamp the field. Masking drops chosen classes to CDL's
    own background value, marks that value nodata, and makes its palette entry
    transparent -- so they vanish from the map and from Zonal Statistics
    instead of being counted as a crop.
    """

    def test_the_presets_name_the_grey_classes(self):
        from filemanager.public_datasets import CDL_MASK_PRESETS

        self.assertEqual(CDL_MASK_PRESETS['none'], ())
        # 121-124 are the four developed classes.
        self.assertEqual(set(CDL_MASK_PRESETS['developed']), {121, 122, 123, 124})
        # The wider preset has to include them too.
        self.assertTrue(
            {121, 122, 123, 124} <= set(CDL_MASK_PRESETS['non_agricultural'])
        )
        # ...and must not hide the crops themselves.
        for crop in (1, 5, 24, 36, 176):
            self.assertNotIn(crop, CDL_MASK_PRESETS['non_agricultural'])

    def test_an_unknown_mask_is_refused(self):
        from filemanager.public_datasets import DatasetError, fetch_cdl

        with self.assertRaises(DatasetError) as caught:
            fetch_cdl({'bbox': (-96.75, 40.78, -96.68, 40.83)}, '/tmp', mask='nope')
        self.assertIn('developed', str(caught.exception))

    def test_masked_pixels_become_transparent_nodata(self):
        """The mechanism, on a raster built to contain known classes."""
        import numpy as np
        import rasterio
        from rasterio.transform import from_origin

        from filemanager.public_datasets import CDL_MASK_PRESETS

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'cdl.tif')
            # Half corn, half developed.
            values = np.array([[1, 1], [123, 124]], dtype='uint8')
            palette = {
                0: (0, 0, 0, 255),
                1: (255, 210, 0, 255),
                123: (154, 154, 154, 255),
                124: (154, 154, 154, 255),
            }
            with rasterio.open(
                path, 'w', driver='GTiff', height=2, width=2, count=1,
                dtype='uint8', crs='EPSG:5070',
                transform=from_origin(-62777.0, 1980202.0, 30.0, 30.0),
            ) as dst:
                dst.write(values, 1)
                dst.write_colormap(1, palette)

            # Apply the same steps fetch_cdl does.
            hidden = CDL_MASK_PRESETS['developed']
            with rasterio.open(path) as src:
                data = src.read(1)
                profile = src.profile.copy()
                carried = dict(src.colormap(1))

            drop = np.isin(data, hidden)
            data = np.where(drop, 0, data).astype('uint8')
            profile['nodata'] = 0
            carried[0] = (0, 0, 0, 255)

            out = os.path.join(tmp, 'masked.tif')
            with rasterio.open(out, 'w', **profile) as dst:
                dst.write(data, 1)
                dst.write_colormap(1, carried)

            with rasterio.open(out) as src:
                self.assertEqual(src.nodata, 0)
                # The developed pixels are gone...
                self.assertNotIn(123, src.read(1).tolist()[0] + src.read(1).tolist()[1])
                # ...the nodata index has a colour the publisher can key
                # transparency to. Only the classes this fixture defines can
                # be checked against it; a palette read back reports black for
                # every index that was never set.
                self.assertEqual(src.colormap(1)[0][:3], (0, 0, 0))
                for code in (1, 123, 124):
                    self.assertNotEqual(src.colormap(1)[code][:3], (0, 0, 0), code)
                # ...and corn is untouched.
                self.assertEqual(src.colormap(1)[1], (255, 210, 0, 255))
                masked = src.read(1, masked=True)
                self.assertEqual(int(masked.count()), 2)


class SourceLabelCoverageTests(TestCase):
    """
    Every registered dataset must have a readable badge. A separate hand-kept
    list of names went stale the moment four datasets were added: their badges
    read "Ssurgo" and "Usgs Wbd" on the dashboard.
    """

    def test_every_dataset_source_has_a_proper_name_and_icon(self):
        from filemanager.public_datasets import PUBLIC_DATASETS
        from filemanager.templatetags.third_party import (
            DEFAULT_ICON, source_icon, source_label,
        )

        for dataset in PUBLIC_DATASETS.values():
            with self.subTest(source=dataset.source):
                label = source_label(dataset.source)
                self.assertEqual(label, dataset.name)
                # The title-cased fallback is what we are guarding against.
                self.assertNotEqual(label, dataset.source.replace('_', ' ').title())
                self.assertNotEqual(source_icon(dataset.source), DEFAULT_ICON)

    def test_the_sync_platforms_are_still_named(self):
        from filemanager.templatetags.third_party import source_icon, source_label

        self.assertEqual(source_label('johndeere'), 'John Deere')
        self.assertEqual(source_icon('johndeere'), 'fa-tractor')
        self.assertEqual(source_label('realm5'), 'Realm5')


class UnnamedCrsTests(TestCase):
    """
    GeoServer will not serve a coverage whose SRS it cannot name. MODIS is
    sinusoidal and gNATSGO and MTBS are Albers, none of which carry an EPSG
    code: GeoServer created the layer, set srs to null, quietly disabled the
    coverage, and every WMS tile came back as an opaque LayerNotDefined error
    image -- which also hid the basemap underneath it.
    """

    def test_a_crs_with_no_epsg_code_is_reprojected(self):
        import numpy as np
        import rasterio
        from rasterio.transform import from_origin

        from filemanager.public_datasets import _clip_cog

        sinusoidal = rasterio.crs.CRS.from_proj4(
            '+proj=sinu +lon_0=0 +x_0=0 +y_0=0 +a=6371007.181 +b=6371007.181 +units=m +no_defs'
        )
        self.assertIsNone(sinusoidal.to_epsg(), 'fixture must have no EPSG code')

        with tempfile.TemporaryDirectory() as tmp:
            source_path = os.path.join(tmp, 'sinu.tif')
            with rasterio.open(
                source_path, 'w', driver='GTiff', height=40, width=40, count=1,
                dtype='uint8', crs=sinusoidal,
                transform=from_origin(-10400000.0, 4600000.0, 250.0, 250.0),
            ) as dst:
                dst.write(np.full((40, 40), 7, dtype='uint8'), 1)

            out = os.path.join(tmp, 'clipped.tif')
            with rasterio.open(source_path) as src:
                west, south, east, north = rasterio.warp.transform_bounds(
                    src.crs, 'EPSG:4326', *src.bounds)
            _clip_cog(source_path, (west, south, east, north), out)

            with rasterio.open(out) as written:
                self.assertEqual(written.crs.to_epsg(), 4326)
                # Nearest neighbour: a class raster must not gain a class that
                # was never in it.
                self.assertEqual(set(np.unique(written.read(1)).tolist()) - {0}, {7})

    def test_a_crs_that_has_an_epsg_code_is_left_alone(self):
        import numpy as np
        import rasterio
        from rasterio.transform import from_origin

        from filemanager.public_datasets import _clip_cog

        with tempfile.TemporaryDirectory() as tmp:
            source_path = os.path.join(tmp, 'utm.tif')
            with rasterio.open(
                source_path, 'w', driver='GTiff', height=20, width=20, count=1,
                dtype='uint8', crs='EPSG:32614',
                transform=from_origin(500000.0, 4500000.0, 30.0, 30.0),
            ) as dst:
                dst.write(np.ones((20, 20), dtype='uint8'), 1)

            out = os.path.join(tmp, 'clipped.tif')
            with rasterio.open(source_path) as src:
                bounds = rasterio.warp.transform_bounds(src.crs, 'EPSG:4326', *src.bounds)
            _clip_cog(source_path, bounds, out)

            with rasterio.open(out) as written:
                # Already nameable, so no resampling is done to it.
                self.assertEqual(written.crs.to_epsg(), 32614)


class BandSetTests(TestCase):
    """The named band sets that collections with twenty-odd bands offer.

    Landsat carries 22 assets and GOES 43. Listing all of them in a dropdown
    would be unusable, so those datasets offer sets -- 'natural colour',
    'thermal' -- and what matters is that the chosen name reaches the fetcher
    as the right list of assets, and that a wrong name says so rather than
    quietly falling back to the default.
    """

    def _captured(self, key, **params):
        with patch('filemanager.public_datasets.fetch_pc_raster') as fetch:
            fetch.return_value = (True, 'ok', {})
            PUBLIC_DATASETS[key].fetch({'bbox': (-96.7, 40.8, -96.6, 40.9)},
                                       '/tmp', **params)
        return fetch.call_args.kwargs

    def test_a_named_set_becomes_the_list_of_assets(self):
        self.assertEqual(self._captured('landsat', bands='thermal')['assets'],
                         ['lwir11'])
        self.assertEqual(
            self._captured('landsat', bands='natural_colour')['assets'],
            ['red', 'green', 'blue'])

    def test_the_default_set_is_used_when_none_is_given(self):
        self.assertEqual(self._captured('landsat')['assets'],
                         ['red', 'green', 'blue'])

    def test_goes_channels_resolve_to_their_assets(self):
        self.assertEqual(self._captured('goes', bands='visible')['assets'],
                         ['C02_2km'])
        self.assertEqual(
            self._captured('goes', bands='day_night_pair')['assets'],
            ['C02_2km', 'C13_2km'])

    def test_an_unknown_set_is_refused_by_name(self):
        with self.assertRaises(DatasetError) as caught:
            self._captured('landsat', bands='infra_red')
        message = str(caught.exception)
        self.assertIn('infra_red', message)
        self.assertIn('thermal', message)

    def test_every_offered_choice_is_a_set_that_exists(self):
        for key in ('landsat', 'goes'):
            dataset = PUBLIC_DATASETS[key]
            choices = [o for o in dataset.options if o['name'] == 'bands']
            self.assertEqual(len(choices), 1, key)
            for choice in choices[0]['choices']:
                with self.subTest(dataset=key, band_set=choice):
                    assets = self._captured(key, bands=choice)['assets']
                    self.assertTrue(assets)


class ClimateNormalsTests(TestCase):
    """A normal is not tied to a year, so the month is a property filter.

    Asking the STAC API for July 2019 would return nothing: the items are one
    per month of a thirty-year period, with no year of their own. Getting this
    wrong would look like "no data covers that area" rather than like a bug.
    """

    def _captured(self, **params):
        with patch('filemanager.public_datasets.fetch_pc_raster') as fetch:
            fetch.return_value = (True, 'ok', {})
            PUBLIC_DATASETS['climate_normals'].fetch(
                {'bbox': (-96.7, 40.8, -96.6, 40.9)}, '/tmp', **params)
        return fetch.call_args.kwargs

    def test_the_month_is_asked_for_as_a_property_not_a_date(self):
        call = self._captured(month='4')
        self.assertEqual(call['query']['noaa_climate_normals:time_index'],
                         {'eq': 4})
        self.assertNotIn('datetime_range', call)

    def test_the_period_is_pinned_to_the_current_normals(self):
        query = self._captured()['query']
        self.assertEqual(query['noaa_climate_normals:period'], {'eq': '1991-2020'})
        self.assertEqual(query['noaa_climate_normals:frequency'],
                         {'eq': 'monthly'})

    def test_the_month_appears_in_the_name_and_the_band_label(self):
        call = self._captured(month='12', variable='prcp_norm')
        self.assertIn('december', call['basename'])
        self.assertIn('December', call['band_labels']['prcp_norm'])

    def test_a_month_outside_the_year_is_refused(self):
        for bad in ('0', '13', 'July', ''):
            with self.subTest(month=bad):
                with self.assertRaises(DatasetError):
                    self._captured(month=bad)

    def test_an_unknown_variable_lists_the_real_ones(self):
        with self.assertRaises(DatasetError) as caught:
            self._captured(variable='rainfall')
        self.assertIn('prcp_norm', str(caught.exception))

    def test_every_offered_variable_is_accepted(self):
        options = {o['name']: o for o in
                   PUBLIC_DATASETS['climate_normals'].options}
        for variable in options['variable']['choices']:
            with self.subTest(variable=variable):
                self.assertEqual(self._captured(variable=variable)['assets'],
                                 [variable])
        for month in options['month']['choices']:
            with self.subTest(month=month):
                self._captured(month=month)


class ExtraQueryTests(TestCase):
    def test_a_cloud_limit_and_a_property_filter_both_survive(self):
        """They share one STAC query field, so one must not overwrite the other."""
        captured = {}

        def search(collection, bbox, **extra):
            captured.update(extra)
            return []

        with patch('filemanager.public_datasets.pc_search', side_effect=search):
            from filemanager.public_datasets import fetch_pc_raster
            with self.assertRaises(DatasetError):
                fetch_pc_raster({'bbox': (-0.5, -0.5, 0.5, 0.5)}, '/tmp',
                                collection='test', assets=['a'],
                                cloud_under=15, query={'platform': {'eq': 'x'}})

        self.assertEqual(captured['query'],
                         {'platform': {'eq': 'x'},
                          'eo:cloud_cover': {'lt': 15.0}})


def _write_raster(path, data, crs='EPSG:4326', palette=None, nodata=None):
    import rasterio
    height, width = data.shape
    profile = dict(driver='GTiff', height=height, width=width, count=1,
                   dtype=data.dtype.name, crs=rasterio.crs.CRS.from_string(crs)
                   if isinstance(crs, str) else crs,
                   transform=rasterio.transform.from_bounds(0, 0, 1, 1,
                                                            width, height))
    if nodata is not None:
        profile['nodata'] = nodata
    with rasterio.open(path, 'w', **profile) as destination:
        destination.write(data, 1)
        if palette:
            destination.write_colormap(1, palette)
    return path


class ActiveFireStylingTests(TestCase):
    """The fire layer has to show fire, and say so when there is none.

    MODIS FireMask is a class code where 5 means "land, and it is not burning".
    Published raw, nearly every pixel is a 5, which GeoServer draws as flat
    opaque grey -- it hid the basemap and buried the handful of pixels that are
    the entire point of the layer. That is what was on the site: a fire layer
    with no visible fire, over a fetch that had in fact worked.
    """

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix='adma-fire-')
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

    def _fetch(self, data, **params):
        import numpy as np
        from filemanager import public_datasets

        path = os.path.join(self.directory, 'active_fire_2024-08-01.tif')
        _write_raster(path, np.array(data, dtype='uint8'))

        def fake(aoi, output_dir, **kwargs):
            return True, 'raw message', {'raster_tif': path}

        with patch.object(public_datasets, 'fetch_pc_raster', side_effect=fake):
            return path, public_datasets.fetch_active_fire(
                {'bbox': (-1, -1, 1, 1)}, self.directory, **params)

    def test_ground_that_is_not_burning_is_reported_not_published(self):
        with self.assertRaises(DatasetError) as caught:
            self._fetch([[5, 5, 4], [5, 3, 5], [5, 5, 5]])
        message = str(caught.exception)
        self.assertIn('Nothing was burning', message)
        # It should name what the satellite did see, so the user can tell a
        # clear day from a clouded-out one.
        self.assertIn('land, no fire', message)
        self.assertIn('cloud', message)

    def test_an_empty_result_leaves_no_file_behind(self):
        path = os.path.join(self.directory, 'active_fire_2024-08-01.tif')
        with self.assertRaises(DatasetError):
            self._fetch([[5, 5], [5, 5]])
        self.assertFalse(os.path.exists(path))

    def test_everything_not_burning_becomes_transparent_nodata(self):
        import rasterio

        path, (success, message, _) = self._fetch(
            [[5, 9, 4], [5, 8, 3], [7, 5, 5]])
        self.assertTrue(success)
        with rasterio.open(path) as source:
            self.assertEqual(source.nodata, 0)
            data = source.read(1)
            palette = source.colormap(1)

        # Only the three fire classes survive; the rest are nodata.
        self.assertEqual(sorted(set(data.flatten().tolist())), [0, 7, 8, 9])
        # Black for nodata, which is what the publisher keys transparency to,
        # and a colour no fire class uses.
        self.assertEqual(palette[0][:3], (0, 0, 0))
        for code in (7, 8, 9):
            self.assertNotEqual(palette[code][:3], (0, 0, 0))

    def test_the_message_counts_the_fire_by_confidence(self):
        _, (_, message, _) = self._fetch([[9, 9, 5], [8, 5, 5], [5, 5, 5]])
        self.assertIn('3 burning pixels of 9', message)
        self.assertIn('2 high confidence', message)
        self.assertIn('1 nominal confidence', message)

    def test_radiative_power_is_left_as_it_comes(self):
        """FRP is a measurement, not a class, so it needs no palette."""
        import numpy as np
        from filemanager import public_datasets

        path = os.path.join(self.directory, 'frp.tif')
        _write_raster(path, np.zeros((3, 3), dtype='uint8'))
        asked = {}

        def fake(aoi, output_dir, **kwargs):
            asked.update(kwargs)
            return True, 'raw message', {'raster_tif': path}

        with patch.object(public_datasets, 'fetch_pc_raster', side_effect=fake):
            success, message, _ = public_datasets.fetch_active_fire(
                {'bbox': (-1, -1, 1, 1)}, self.directory,
                band='radiative_power')

        self.assertTrue(success)
        self.assertEqual(message, 'raw message')
        self.assertEqual(asked['assets'], ['MaxFRP'])

    def test_an_unknown_choice_is_refused(self):
        with self.assertRaises(DatasetError):
            self._fetch([[9]], band='smoke')

    def test_one_date_without_the_other_is_refused(self):
        with self.assertRaises(DatasetError):
            self._fetch([[9]], start='2024-08-01')


class PaletteSurvivesTheClipTests(TestCase):
    """A class raster clipped without its palette renders as shades of grey.

    profile does not carry a colour map, so burn severity, land cover and the
    rest arrived in GeoServer as greyscale until the palette was copied
    deliberately -- readable only if you already knew the codes.
    """

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix='adma-pal-')
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

    def test_a_single_band_clip_keeps_the_colours(self):
        import numpy as np
        import rasterio
        from filemanager.public_datasets import _clip_cog

        palette = {1: (10, 20, 30, 255), 2: (200, 100, 50, 255)}
        source = _write_raster(
            os.path.join(self.directory, 'in.tif'),
            np.array([[1, 2], [2, 1]], dtype='uint8'), palette=palette)

        out = os.path.join(self.directory, 'out.tif')
        _clip_cog(source, (0.1, 0.1, 0.9, 0.9), out, indexes=[1])

        with rasterio.open(out) as clipped:
            kept = clipped.colormap(1)
        self.assertEqual(kept[1][:3], (10, 20, 30))
        self.assertEqual(kept[2][:3], (200, 100, 50))

    def test_a_stack_of_bands_gets_no_palette(self):
        """A colour map indexes one band, so on a stack it would mislead."""
        import numpy as np
        import rasterio

        from filemanager import public_datasets

        palette = {1: (10, 20, 30, 255)}
        for name in ('a', 'b'):
            _write_raster(os.path.join(self.directory, f'{name}.tif'),
                          np.array([[1, 1], [1, 1]], dtype='uint8'),
                          palette=palette)

        item = {'id': 'item', 'bbox': [-1, -1, 1, 1],
                'properties': {'datetime': '2024-01-01T00:00:00Z'},
                'assets': {'a': {'href': os.path.join(self.directory, 'a.tif')},
                           'b': {'href': os.path.join(self.directory, 'b.tif')}}}

        with patch.object(public_datasets, 'pc_search', return_value=[item]), \
             patch.object(public_datasets, 'pc_sign', side_effect=lambda h, c=None: h):
            _, _, outputs = public_datasets.fetch_pc_raster(
                {'bbox': (-0.5, -0.5, 0.5, 0.5)}, self.directory,
                collection='test', assets=['a', 'b'], basename='stacked')

        with rasterio.open(outputs['raster_tif']) as stacked:
            self.assertEqual(stacked.count, 2)
            with self.assertRaises(ValueError):
                stacked.colormap(1)


class BandInterpretationTests(TestCase):
    """The second band of a stack must not be read as an opacity mask.

    A GeoTIFF with two bands and no colour interpretation set is taken by GDAL,
    and therefore by GeoServer, as greyscale plus alpha. Every two-band layer
    on the site was being drawn with its second band as transparency: the fire
    layer disappeared outright because radiative power was all zeros, and the
    MODIS vegetation layer came back a third see-through because low EVI values
    stretched to a low alpha. Nothing raised; the map just showed less than it
    should, or nothing.
    """

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix='adma-interp-')
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

    def _written(self, count):
        import numpy as np
        import rasterio

        path = os.path.join(self.directory, f'{count}band.tif')
        profile = dict(driver='GTiff', height=4, width=4, count=count,
                       dtype='uint8', crs=rasterio.crs.CRS.from_epsg(4326),
                       transform=rasterio.transform.from_bounds(0, 0, 1, 1, 4, 4))
        from filemanager.public_datasets import _declare_bands_as_data
        with rasterio.open(path, 'w', **profile) as destination:
            destination.write(np.ones((count, 4, 4), 'uint8'))
            _declare_bands_as_data(destination)
        with rasterio.open(path) as written:
            return [c.name for c in written.colorinterp]

    def test_a_two_band_stack_says_its_second_band_is_data(self):
        self.assertEqual(self._written(2), ['gray', 'undefined'])

    def test_a_single_band_is_left_alone(self):
        self.assertEqual(self._written(1), ['gray'])

    def test_three_bands_stay_red_green_blue(self):
        """RGB is right for imagery and is what GDAL already assumes."""
        self.assertEqual(self._written(3), ['red', 'green', 'blue'])

    def test_a_four_band_stack_is_not_treated_as_rgba(self):
        self.assertEqual(self._written(4),
                         ['gray', 'undefined', 'undefined', 'undefined'])


class StackedGridTests(TestCase):
    """Every band of a stack goes on band one's grid, not on its own guess.

    Asking each band for the same pixel count and letting each work out its own
    reprojection does not reliably agree -- the two bands of chloris-biomass
    came back 13x12 and 14x13, and the stack could not be written at all.
    """

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix='adma-grid-')
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

    def test_bands_in_a_crs_with_no_epsg_code_land_on_one_grid(self):
        import numpy as np
        import rasterio
        from filemanager import public_datasets

        # Sinusoidal: no EPSG code, so each band gets reprojected.
        sinusoidal = ('+proj=sinu +lon_0=0 +x_0=0 +y_0=0 +R=6371007.181 '
                      '+units=m +no_defs')
        hrefs = {}
        for name, (width, height) in (('a', (23, 19)), ('b', (23, 19))):
            path = os.path.join(self.directory, f'{name}.tif')
            profile = dict(driver='GTiff', height=height, width=width, count=1,
                           dtype='int16',
                           crs=rasterio.crs.CRS.from_string(sinusoidal),
                           transform=rasterio.transform.from_bounds(
                               -8000000, 4000000, -7900000, 4100000,
                               width, height))
            with rasterio.open(path, 'w', **profile) as destination:
                destination.write(
                    np.arange(width * height, dtype='int16').reshape(height, width), 1)
            hrefs[name] = path

        # That sinusoidal window is lon -88.90..-88.81, lat 35.97..36.87.
        item = {'id': 'item', 'bbox': [-88.90, 35.97, -88.81, 36.88],
                'properties': {'datetime': '2024-01-01T00:00:00Z'},
                'assets': {k: {'href': v} for k, v in hrefs.items()}}

        with patch.object(public_datasets, 'pc_search', return_value=[item]), \
             patch.object(public_datasets, 'pc_sign', side_effect=lambda h, c=None: h):
            _, _, outputs = public_datasets.fetch_pc_raster(
                {'bbox': (-88.88, 36.10, -88.83, 36.70)}, self.directory,
                collection='test', assets=['a', 'b'], basename='pinned')

        with rasterio.open(outputs['raster_tif']) as stacked:
            self.assertEqual(stacked.count, 2)
            self.assertEqual(stacked.crs.to_epsg(), 4326)
            # Both bands exist at one size, which is the whole point.
            self.assertEqual(stacked.read(1).shape, stacked.read(2).shape)

    def test_a_band_on_a_different_grid_is_still_reported_clearly(self):
        """The guard stays, because a clear message beats a rasterio shape error."""
        import numpy as np
        import rasterio
        from filemanager import public_datasets

        shapes = iter([(20, 20), (17, 19)])

        def clip(signed_href, bbox, out_path, **kwargs):
            height, width = next(shapes)
            profile = dict(driver='GTiff', height=height, width=width, count=1,
                           dtype='uint8', crs=rasterio.crs.CRS.from_epsg(4326),
                           transform=rasterio.transform.from_bounds(
                               0, 0, 1, 1, width, height))
            with rasterio.open(out_path, 'w', **profile) as destination:
                destination.write(np.ones((height, width), 'uint8'), 1)
            return width, height, 1

        item = {'id': 'item', 'bbox': [-1, -1, 1, 1],
                'properties': {'datetime': '2024-01-01T00:00:00Z'},
                'assets': {'a': {'href': 'a'}, 'b': {'href': 'b'}}}

        with patch.object(public_datasets, 'pc_search', return_value=[item]), \
             patch.object(public_datasets, 'pc_sign', side_effect=lambda h, c=None: h), \
             patch.object(public_datasets, '_clip_cog', side_effect=clip):
            with self.assertRaises(DatasetError) as caught:
                public_datasets.fetch_pc_raster(
                    {'bbox': (-0.5, -0.5, 0.5, 0.5)}, self.directory,
                    collection='test', assets=['a', 'b'])

        self.assertIn('cannot be stacked', str(caught.exception))
