"""
The three new tool modules, on synthetic data whose right answer is known by
construction: a plane of exactly 10% slope, points on a raster of known values,
a square whose buffered area can be worked out on paper.
"""
import math
import os
import tempfile

import geopandas as gpd
import numpy as np
import rasterio
from django.test import SimpleTestCase
from rasterio.transform import from_origin
from shapely.geometry import Point, box

from filemanager.point_sampling import sample_rasters_at_points
from filemanager.terrain_analysis import analyse_terrain
from filemanager.vector_ops import apply_operation

CRS = 'EPSG:32614'


def write_raster(path, array, crs=CRS, pixel=10.0, origin=(500000.0, 4500000.0),
                 nodata=None, dtype='float32'):
    array = np.asarray(array, dtype=dtype)
    with rasterio.open(
        path, 'w', driver='GTiff', height=array.shape[0], width=array.shape[1],
        count=1, dtype=dtype, crs=crs, nodata=nodata,
        transform=from_origin(origin[0], origin[1], pixel, pixel),
    ) as destination:
        destination.write(array, 1)
    return path


class TerrainTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name

    def test_a_known_plane_gives_the_known_slope(self):
        """Ground rising 1 m every 10 m east is 10%, which is 5.71 degrees."""
        rows, cols = 20, 20
        elevation = np.tile(np.arange(cols) * 1.0, (rows, 1))  # +1 m per pixel
        dem = write_raster(os.path.join(self.dir, 'dem.tif'), elevation, pixel=10.0)

        ok, message, outputs = analyse_terrain(dem, self.dir, products=['slope'])

        self.assertTrue(ok, message)
        with rasterio.open(outputs['slope_tif']) as src:
            slope = src.read(1)
        interior = slope[2:-2, 2:-2]
        self.assertAlmostEqual(float(np.nanmean(interior)),
                               math.degrees(math.atan(0.1)), places=3)

    def test_flat_ground_is_flat(self):
        dem = write_raster(os.path.join(self.dir, 'flat.tif'), np.full((15, 15), 300.0))

        ok, message, outputs = analyse_terrain(dem, self.dir, products=['slope'])

        self.assertTrue(ok, message)
        with rasterio.open(outputs['slope_tif']) as src:
            self.assertLess(float(np.nanmax(src.read(1))), 1e-6)

    def test_aspect_of_an_east_facing_slope_points_east(self):
        # Elevation falls to the east, so the surface faces east: 90 degrees.
        rows, cols = 20, 20
        elevation = np.tile(np.arange(cols)[::-1] * 1.0, (rows, 1))
        dem = write_raster(os.path.join(self.dir, 'dem.tif'), elevation, pixel=10.0)

        ok, message, outputs = analyse_terrain(dem, self.dir, products=['aspect'])

        self.assertTrue(ok, message)
        with rasterio.open(outputs['aspect_tif']) as src:
            aspect = src.read(1)
        self.assertAlmostEqual(float(np.nanmean(aspect[2:-2, 2:-2])), 90.0, places=2)

    def test_a_degree_dem_is_reprojected_before_measuring(self):
        """
        A degree is not a distance. Measuring a gradient straight from degree
        spacing gives a slope wrong by a factor of about 100,000.
        """
        rows, cols = 20, 20
        elevation = np.tile(np.arange(cols) * 1.0, (rows, 1))
        dem = write_raster(
            os.path.join(self.dir, 'geo.tif'), elevation,
            crs='EPSG:4326', pixel=0.0001, origin=(-96.75, 40.83),
        )

        ok, message, outputs = analyse_terrain(dem, self.dir, products=['slope'])

        self.assertTrue(ok, message)
        with rasterio.open(outputs['slope_tif']) as src:
            self.assertTrue(src.crs.is_projected, src.crs)
            slope = src.read(1)
        # 1 m per ~8.5 m pixel is a steep but sane slope; degrees would have
        # produced something absurd.
        self.assertLess(float(np.nanmax(slope)), 89.0)
        self.assertGreater(float(np.nanmean(slope[3:-3, 3:-3])), 1.0)

    def test_a_dem_without_a_crs_is_refused(self):
        dem = write_raster(os.path.join(self.dir, 'nocrs.tif'),
                           np.zeros((10, 10)), crs=None)

        ok, message, _ = analyse_terrain(dem, self.dir, products=['slope'])

        self.assertFalse(ok)
        self.assertIn('no CRS', message)

    def test_an_unknown_product_is_refused(self):
        dem = write_raster(os.path.join(self.dir, 'dem.tif'), np.zeros((5, 5)))
        ok, message, _ = analyse_terrain(dem, self.dir, products=['nope'])
        self.assertFalse(ok)
        self.assertIn('Unknown product', message)


class PointSamplingTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name

    def _points(self, name='pts.shp'):
        path = os.path.join(self.dir, name)
        # Pixel (row 0, col 0) covers x 500000-500010, y 4499990-4500000.
        gpd.GeoDataFrame(
            {'id': [1, 2]},
            geometry=[Point(500005, 4499995), Point(500015, 4499995)],
            crs=CRS,
        ).to_file(path)
        return path

    def test_it_reads_the_value_under_each_point(self):
        raster = write_raster(
            os.path.join(self.dir, 'v.tif'),
            np.array([[11.0, 22.0], [33.0, 44.0]]), pixel=10.0,
        )
        points = self._points()

        ok, message, outputs = sample_rasters_at_points(
            points, [{'path': raster, 'band': 1, 'label': 'val'}], self.dir
        )

        self.assertTrue(ok, message)
        table = gpd.pd.read_csv(outputs['samples_csv'])
        self.assertEqual(sorted(table['val'].tolist()), [11.0, 22.0])

    def test_points_are_reprojected_onto_the_raster(self):
        raster = write_raster(
            os.path.join(self.dir, 'v.tif'),
            np.array([[11.0, 22.0], [33.0, 44.0]]), pixel=10.0,
        )
        path = os.path.join(self.dir, 'wgs.shp')
        gpd.GeoDataFrame(
            {'id': [1]}, geometry=[Point(500005, 4499995)], crs=CRS,
        ).to_crs('EPSG:4326').to_file(path)

        ok, message, outputs = sample_rasters_at_points(
            path, [{'path': raster, 'band': 1, 'label': 'val'}], self.dir
        )

        self.assertTrue(ok, message)
        table = gpd.pd.read_csv(outputs['samples_csv'])
        self.assertAlmostEqual(table['val'].iloc[0], 11.0)

    def test_nodata_becomes_blank_not_the_sentinel(self):
        raster = write_raster(
            os.path.join(self.dir, 'v.tif'),
            np.array([[-9999.0, 22.0], [33.0, 44.0]]), pixel=10.0, nodata=-9999.0,
        )
        points = self._points()

        ok, message, outputs = sample_rasters_at_points(
            points, [{'path': raster, 'band': 1, 'label': 'val'}], self.dir
        )

        self.assertTrue(ok, message)
        table = gpd.pd.read_csv(outputs['samples_csv'])
        self.assertTrue(table['val'].isna().any())
        self.assertNotIn(-9999.0, table['val'].dropna().tolist())

    def test_several_rasters_land_in_one_row_per_point(self):
        a = write_raster(os.path.join(self.dir, 'a.tif'), np.full((2, 2), 1.0), pixel=10.0)
        b = write_raster(os.path.join(self.dir, 'b.tif'), np.full((2, 2), 2.0), pixel=10.0)
        points = self._points()

        ok, message, outputs = sample_rasters_at_points(
            points,
            [{'path': a, 'band': 1, 'label': 'elev'},
             {'path': b, 'band': 1, 'label': 'cdl'}],
            self.dir,
        )

        self.assertTrue(ok, message)
        table = gpd.pd.read_csv(outputs['samples_csv'])
        self.assertEqual(len(table), 2)
        self.assertIn('elev', table.columns)
        self.assertIn('cdl', table.columns)

    def test_long_labels_do_not_collide_in_the_shapefile(self):
        a = write_raster(os.path.join(self.dir, 'a.tif'), np.full((2, 2), 1.0), pixel=10.0)
        b = write_raster(os.path.join(self.dir, 'b.tif'), np.full((2, 2), 2.0), pixel=10.0)
        points = self._points()

        ok, message, outputs = sample_rasters_at_points(
            points,
            [{'path': a, 'band': 1, 'label': 'soil_organic_carbon'},
             {'path': b, 'band': 1, 'label': 'soil_organic_matter'}],
            self.dir,
        )

        self.assertTrue(ok, message)
        written = gpd.read_file(outputs['samples_shp'][0])
        sampled = [c for c in written.columns if c.startswith('soil_organ')]
        self.assertEqual(len(sampled), 2, written.columns.tolist())


class VectorOperationTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name

    def _square(self, name='sq.shp', crs=CRS):
        path = os.path.join(self.dir, name)
        frame = gpd.GeoDataFrame(
            {'plot': ['a']}, geometry=[box(500000, 4500000, 500100, 4500100)], crs=CRS,
        )
        if crs != CRS:
            frame = frame.to_crs(crs)
        frame.to_file(path)
        return path

    def test_buffering_grows_the_area_by_the_right_amount(self):
        path = self._square()

        ok, message, outputs = apply_operation(
            path, self.dir, operation='buffer', distance=10.0
        )

        self.assertTrue(ok, message)
        result = gpd.read_file(outputs['result_shp'][0])
        # A 100 m square buffered by 10 m: 120x120 less the corners it rounds.
        expected = 100 * 100 + 4 * 100 * 10 + math.pi * 10 ** 2
        self.assertAlmostEqual(float(result.area.iloc[0]), expected, delta=expected * 0.01)

    def test_a_degree_layer_is_buffered_in_metres_and_comes_back_in_degrees(self):
        path = self._square('geo.shp', crs='EPSG:4326')

        ok, message, outputs = apply_operation(
            path, self.dir, operation='buffer', distance=10.0
        )

        self.assertTrue(ok, message)
        result = gpd.read_file(outputs['result_shp'][0])
        self.assertEqual(result.crs.to_epsg(), 4326)
        metric_area = float(result.to_crs(CRS).area.iloc[0])
        expected = 100 * 100 + 4 * 100 * 10 + math.pi * 10 ** 2
        self.assertAlmostEqual(metric_area, expected, delta=expected * 0.02)

    def test_a_negative_buffer_that_erases_everything_says_so(self):
        path = self._square()

        ok, message, _ = apply_operation(
            path, self.dir, operation='buffer', distance=-200.0
        )

        self.assertFalse(ok)
        self.assertIn('removed every feature', message)

    def test_clipping_keeps_only_the_overlap(self):
        path = self._square()
        mask_path = os.path.join(self.dir, 'mask.shp')
        gpd.GeoDataFrame(
            {'id': [1]}, geometry=[box(500000, 4500000, 500050, 4500100)], crs=CRS,
        ).to_file(mask_path)

        ok, message, outputs = apply_operation(
            path, self.dir, operation='clip', clip_path=mask_path
        )

        self.assertTrue(ok, message)
        result = gpd.read_file(outputs['result_shp'][0])
        self.assertAlmostEqual(float(result.area.iloc[0]), 50 * 100, delta=1.0)

    def test_clipping_with_no_overlap_says_so(self):
        path = self._square()
        mask_path = os.path.join(self.dir, 'mask.shp')
        gpd.GeoDataFrame(
            {'id': [1]}, geometry=[box(900000, 4900000, 900100, 4900100)], crs=CRS,
        ).to_file(mask_path)

        ok, message, _ = apply_operation(
            path, self.dir, operation='clip', clip_path=mask_path
        )

        self.assertFalse(ok)
        self.assertIn('may not overlap', message)

    def test_dissolve_groups_by_a_column(self):
        path = os.path.join(self.dir, 'strips.shp')
        gpd.GeoDataFrame(
            {'treat': ['N1', 'N1', 'N2']},
            geometry=[
                box(0, 0, 10, 10), box(10, 0, 20, 10), box(20, 0, 30, 10),
            ],
            crs=CRS,
        ).to_file(path)

        ok, message, outputs = apply_operation(
            path, self.dir, operation='dissolve', dissolve_by='treat'
        )

        self.assertTrue(ok, message)
        result = gpd.read_file(outputs['result_shp'][0])
        self.assertEqual(len(result), 2)

    def test_dissolving_by_a_missing_column_lists_the_real_ones(self):
        path = self._square()
        ok, message, _ = apply_operation(
            path, self.dir, operation='dissolve', dissolve_by='nope'
        )
        self.assertFalse(ok)
        self.assertIn('plot', message)
