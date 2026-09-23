"""
The processing modules behind the four native GIS tools, exercised directly on
synthetic data -- no Celery, no database, no uploaded files.

Each test builds a raster or point layer whose right answer is known by
construction, so a wrong number is a wrong number rather than a judgement call.
"""
import os
import tempfile

import geopandas as gpd
import numpy as np
import rasterio
from django.test import SimpleTestCase
from rasterio.transform import from_origin
from shapely.geometry import Point, box

from filemanager.management_zones import delineate_management_zones, numeric_columns
from filemanager.raster_clip_reproject import clip_and_reproject
from filemanager.vegetation_index import compute_vegetation_index
from filemanager.zonal_statistics import compute_zonal_statistics

# A metric CRS, so a "1 unit" pixel is a 1 m pixel and the numbers below are
# easy to reason about.
CRS = 'EPSG:32614'


def write_raster(path, array, nodata=None, crs=CRS, origin=(0.0, 100.0)):
    array = np.asarray(array, dtype='float32')
    if array.ndim == 2:
        array = array[np.newaxis, :, :]
    transform = from_origin(origin[0], origin[1], 1.0, 1.0)
    with rasterio.open(
        path, 'w', driver='GTiff', height=array.shape[1], width=array.shape[2],
        count=array.shape[0], dtype='float32', crs=crs, transform=transform,
        nodata=nodata,
    ) as destination:
        destination.write(array)
    return path


class ZonalStatisticsTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name

    def test_means_match_the_values_under_each_polygon(self):
        # Left half all 10s, right half all 20s.
        values = np.zeros((10, 10), dtype='float32')
        values[:, :5] = 10.0
        values[:, 5:] = 20.0
        raster = write_raster(os.path.join(self.dir, 'v.tif'), values)

        # Raster spans x 0..10, y 90..100.
        zones = gpd.GeoDataFrame(
            {'name': ['left', 'right']},
            geometry=[box(0.5, 90.5, 4.5, 99.5), box(5.5, 90.5, 9.5, 99.5)],
            crs=CRS,
        )
        vector = os.path.join(self.dir, 'z.shp')
        zones.to_file(vector)

        ok, message, outputs = compute_zonal_statistics(vector, raster, self.dir)

        self.assertTrue(ok, message)
        table = gpd.pd.read_csv(outputs['statistics_csv'])
        by_name = table.set_index('name')
        self.assertAlmostEqual(by_name.loc['left', 'val_mean'], 10.0, places=5)
        self.assertAlmostEqual(by_name.loc['right', 'val_mean'], 20.0, places=5)

    def test_nodata_is_excluded_from_the_mean(self):
        """Averaging the sentinel in is the classic way these numbers go wrong."""
        values = np.full((10, 10), 10.0, dtype='float32')
        values[:, :5] = -9999.0
        raster = write_raster(os.path.join(self.dir, 'v.tif'), values, nodata=-9999.0)

        zones = gpd.GeoDataFrame(
            {'name': ['whole']}, geometry=[box(0.5, 90.5, 9.5, 99.5)], crs=CRS
        )
        vector = os.path.join(self.dir, 'z.shp')
        zones.to_file(vector)

        ok, message, outputs = compute_zonal_statistics(vector, raster, self.dir)

        self.assertTrue(ok, message)
        table = gpd.pd.read_csv(outputs['statistics_csv'])
        self.assertAlmostEqual(table.loc[0, 'val_mean'], 10.0, places=5)

    def test_zones_are_reprojected_to_the_raster(self):
        values = np.full((10, 10), 7.0, dtype='float32')
        raster = write_raster(os.path.join(self.dir, 'v.tif'), values)

        zones = gpd.GeoDataFrame(
            {'name': ['all']}, geometry=[box(0.5, 90.5, 9.5, 99.5)], crs=CRS
        ).to_crs('EPSG:4326')
        vector = os.path.join(self.dir, 'z.shp')
        zones.to_file(vector)

        ok, message, outputs = compute_zonal_statistics(vector, raster, self.dir)

        self.assertTrue(ok, message)
        table = gpd.pd.read_csv(outputs['statistics_csv'])
        self.assertAlmostEqual(table.loc[0, 'val_mean'], 7.0, places=5)

    def test_a_polygon_off_the_raster_reports_no_coverage(self):
        raster = write_raster(os.path.join(self.dir, 'v.tif'), np.ones((10, 10)))
        zones = gpd.GeoDataFrame(
            {'name': ['elsewhere']}, geometry=[box(500, 500, 510, 510)], crs=CRS
        )
        vector = os.path.join(self.dir, 'z.shp')
        zones.to_file(vector)

        ok, message, outputs = compute_zonal_statistics(vector, raster, self.dir)

        self.assertTrue(ok, message)
        self.assertIn('no raster coverage', message)
        table = gpd.pd.read_csv(outputs['statistics_csv'])
        self.assertEqual(table.loc[0, 'val_count'], 0)

    def test_points_are_rejected_with_a_useful_message(self):
        raster = write_raster(os.path.join(self.dir, 'v.tif'), np.ones((5, 5)))
        points = gpd.GeoDataFrame(
            {'name': ['p']}, geometry=[Point(1, 95)], crs=CRS
        )
        vector = os.path.join(self.dir, 'p.shp')
        points.to_file(vector)

        ok, message, _ = compute_zonal_statistics(vector, raster, self.dir)

        self.assertFalse(ok)
        self.assertIn('needs polygons', message)


class VegetationIndexTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name

    def test_ndvi_matches_the_formula(self):
        nir = write_raster(os.path.join(self.dir, 'nir.tif'), np.full((6, 6), 0.6))
        red = write_raster(os.path.join(self.dir, 'red.tif'), np.full((6, 6), 0.2))

        ok, message, outputs = compute_vegetation_index(
            'ndvi', {'nir': (nir, 1), 'red': (red, 1)}, self.dir
        )

        self.assertTrue(ok, message)
        with rasterio.open(outputs['index_tif']) as source:
            array = source.read(1)
        # (0.6 - 0.2) / (0.6 + 0.2) = 0.5
        self.assertAlmostEqual(float(np.nanmean(array)), 0.5, places=5)
        self.assertTrue(os.path.exists(outputs['preview_png']))

    def test_a_zero_denominator_is_nodata_not_zero(self):
        """0.0 would sit mid-scale and drag every downstream mean toward it."""
        nir = write_raster(os.path.join(self.dir, 'nir.tif'), np.zeros((4, 4)))
        red = write_raster(os.path.join(self.dir, 'red.tif'), np.zeros((4, 4)))

        ok, message, outputs = compute_vegetation_index(
            'ndvi', {'nir': (nir, 1), 'red': (red, 1)}, self.dir
        )

        self.assertTrue(ok, message)
        with rasterio.open(outputs['index_tif']) as source:
            array = source.read(1)
        self.assertTrue(np.all(np.isnan(array)))
        self.assertIn('no valid pixels', message)

    def test_mismatched_grids_are_refused(self):
        nir = write_raster(os.path.join(self.dir, 'nir.tif'), np.ones((6, 6)))
        red = write_raster(os.path.join(self.dir, 'red.tif'), np.ones((4, 4)))

        ok, message, _ = compute_vegetation_index(
            'ndvi', {'nir': (nir, 1), 'red': (red, 1)}, self.dir
        )

        self.assertFalse(ok)
        self.assertIn('do not share a grid', message)

    def test_one_multiband_image_can_supply_every_band(self):
        stack = np.stack([
            np.full((5, 5), 0.2),   # band 1: red
            np.full((5, 5), 0.6),   # band 2: nir
        ])
        path = write_raster(os.path.join(self.dir, 'stack.tif'), stack)

        ok, message, outputs = compute_vegetation_index(
            'ndvi', {'red': (path, 1), 'nir': (path, 2)}, self.dir
        )

        self.assertTrue(ok, message)
        with rasterio.open(outputs['index_tif']) as source:
            self.assertAlmostEqual(float(np.nanmean(source.read(1))), 0.5, places=5)

    def test_an_unknown_index_lists_what_is_available(self):
        ok, message, _ = compute_vegetation_index('nope', {}, self.dir)
        self.assertFalse(ok)
        self.assertIn('ndvi', message)


class ManagementZoneTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name

    def _two_clusters(self, path):
        rng = np.random.default_rng(0)
        low = rng.normal(10, 0.3, 60)
        high = rng.normal(50, 0.3, 60)
        frame = gpd.GeoDataFrame(
            {'yield_val': np.concatenate([low, high])},
            geometry=[Point(float(i % 12), float(i // 12)) for i in range(120)],
            crs=CRS,
        )
        frame.to_file(path)
        return path

    def test_two_separated_groups_come_back_as_two_zones(self):
        path = self._two_clusters(os.path.join(self.dir, 'y.shp'))

        ok, message, outputs = delineate_management_zones(
            path, self.dir, ['yield_val'], zone_count=2
        )

        self.assertTrue(ok, message)
        zones = gpd.read_file(outputs['zones_shp'][0])
        self.assertEqual(sorted(zones['zone'].unique().tolist()), [1, 2])
        # Zone 1 is the low end by construction of the ranking.
        self.assertLess(
            zones.loc[zones['zone'] == 1, 'yield_val'].mean(),
            zones.loc[zones['zone'] == 2, 'yield_val'].mean(),
        )

    def test_zone_numbering_is_stable_across_runs(self):
        """kmeans2 labels clusters in seeding order; the ranking fixes that."""
        path = self._two_clusters(os.path.join(self.dir, 'y.shp'))

        first = delineate_management_zones(
            path, os.path.join(self.dir, 'a'), ['yield_val'], zone_count=2
        )
        second = delineate_management_zones(
            path, os.path.join(self.dir, 'b'), ['yield_val'], zone_count=2
        )

        one = gpd.read_file(first[2]['zones_shp'][0])['zone'].tolist()
        two = gpd.read_file(second[2]['zones_shp'][0])['zone'].tolist()
        self.assertEqual(one, two)

    def test_attributes_are_scaled_before_clustering(self):
        """
        Without scaling, a column recorded in larger numbers decides the
        clustering alone. Here the big column is pure noise and the small one
        carries the real split, so an unscaled run would get it wrong.
        """
        rng = np.random.default_rng(1)
        signal = np.concatenate([np.full(60, 0.1), np.full(60, 0.9)])
        noise = rng.normal(5000, 500, 120)
        frame = gpd.GeoDataFrame(
            {'ndvi': signal, 'elev_mm': noise},
            geometry=[Point(float(i % 12), float(i // 12)) for i in range(120)],
            crs=CRS,
        )
        path = os.path.join(self.dir, 'mix.shp')
        frame.to_file(path)

        ok, message, outputs = delineate_management_zones(
            path, self.dir, ['ndvi', 'elev_mm'], zone_count=2
        )

        self.assertTrue(ok, message)
        zones = gpd.read_file(outputs['zones_shp'][0])
        # The ndvi split should be recovered cleanly despite elev_mm being
        # four orders of magnitude larger.
        crosstab = gpd.pd.crosstab(zones['ndvi'].round(1), zones['zone'])
        self.assertEqual(int((crosstab > 0).sum().sum()), 2)

    def test_rows_missing_a_value_are_skipped_not_fatal(self):
        frame = gpd.GeoDataFrame(
            {'yield_val': [1.0, 2.0, None, 50.0, 51.0, 52.0]},
            geometry=[Point(float(i), 0.0) for i in range(6)],
            crs=CRS,
        )
        path = os.path.join(self.dir, 'gap.shp')
        frame.to_file(path)

        ok, message, outputs = delineate_management_zones(
            path, self.dir, ['yield_val'], zone_count=2
        )

        self.assertTrue(ok, message)
        self.assertIn('skipped', message)
        self.assertEqual(len(gpd.read_file(outputs['zones_shp'][0])), 5)

    def test_a_missing_column_names_what_is_available(self):
        path = self._two_clusters(os.path.join(self.dir, 'y.shp'))
        ok, message, _ = delineate_management_zones(path, self.dir, ['nope'])
        self.assertFalse(ok)
        self.assertIn('yield_val', message)

    def test_numeric_columns_finds_the_usable_ones(self):
        path = self._two_clusters(os.path.join(self.dir, 'y.shp'))
        self.assertIn('yield_val', numeric_columns(path))


class ClipReprojectTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name

    def test_clipping_keeps_only_the_boundary(self):
        raster = write_raster(os.path.join(self.dir, 'v.tif'), np.ones((10, 10)))
        boundary = gpd.GeoDataFrame(
            {'id': [1]}, geometry=[box(0.0, 95.0, 5.0, 100.0)], crs=CRS
        )
        boundary_path = os.path.join(self.dir, 'b.shp')
        boundary.to_file(boundary_path)

        ok, message, outputs = clip_and_reproject(
            raster, self.dir, boundary_path=boundary_path
        )

        self.assertTrue(ok, message)
        with rasterio.open(outputs['raster_tif']) as source:
            self.assertEqual((source.height, source.width), (5, 5))

    def test_reprojection_changes_the_crs(self):
        raster = write_raster(os.path.join(self.dir, 'v.tif'), np.ones((10, 10)))

        ok, message, outputs = clip_and_reproject(raster, self.dir, target_epsg=4326)

        self.assertTrue(ok, message)
        with rasterio.open(outputs['raster_tif']) as source:
            self.assertEqual(source.crs.to_epsg(), 4326)

    def test_a_boundary_that_misses_the_raster_says_so(self):
        raster = write_raster(os.path.join(self.dir, 'v.tif'), np.ones((10, 10)))
        boundary = gpd.GeoDataFrame(
            {'id': [1]}, geometry=[box(900, 900, 950, 950)], crs=CRS
        )
        boundary_path = os.path.join(self.dir, 'b.shp')
        boundary.to_file(boundary_path)

        ok, message, _ = clip_and_reproject(
            raster, self.dir, boundary_path=boundary_path
        )

        self.assertFalse(ok)
        self.assertIn('does not overlap', message)

    def test_doing_neither_is_refused(self):
        raster = write_raster(os.path.join(self.dir, 'v.tif'), np.ones((4, 4)))
        ok, message, _ = clip_and_reproject(raster, self.dir)
        self.assertFalse(ok)
        self.assertIn('boundary', message)
