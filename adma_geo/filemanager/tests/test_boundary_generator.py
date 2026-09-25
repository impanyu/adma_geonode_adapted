"""
Sreeja Vinod's field boundary generator.

Two of these cover bugs her own test data exposed, which are the reason the
vendored copy differs from what she emailed at all:

  * Richters_Clean_Yield_23.csv has its latitude and longitude columns the
    wrong way round. Trusting the names puts every point at latitude -97,
    which is nowhere; they project to nonsense, the outlier filter drops all
    21,511, and the run ends with an empty boundary.
  * Richters_clean_yield_23.shp is already in UTM. Reading its bounds as
    degrees produced a "zone" of 106286 and EPSG:138886, which does not exist.
"""
import math
import os
import tempfile

import geopandas as gpd
import numpy as np
from django.test import SimpleTestCase
from shapely.geometry import Point

from filemanager.BoundaryGeneratorTool_SV import (
    SHAPE_CHOICES,
    estimate_utm_crs,
    process_boundary_generation,
)

# A 200 m square of points near Lincoln, Nebraska, in UTM 14N.
EAST, NORTH = 637500.0, 4517900.0


def square_points(crs='EPSG:32614', step=20.0, size=200.0):
    points = [
        Point(EAST + x, NORTH + y)
        for x in np.arange(0, size + 1, step)
        for y in np.arange(0, size + 1, step)
    ]
    return gpd.GeoDataFrame({'id': range(len(points))}, geometry=points,
                            crs='EPSG:32614').to_crs(crs)


class BoundaryGeneratorTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name

    def _write(self, frame, name):
        path = os.path.join(self.dir, name)
        frame.to_file(path)
        return path

    def test_a_square_of_points_gives_about_that_area(self):
        path = self._write(square_points(), 'pts.shp')

        ok, message, outputs = process_boundary_generation(
            path, self.dir, buffer_ft=0.0, shape='convex'
        )

        self.assertTrue(ok, message)
        shp = [p for p in outputs['boundary_components'] if p.endswith('.shp')][0]
        area = float(gpd.read_file(shp).to_crs('EPSG:32614').area.iloc[0])
        self.assertAlmostEqual(area, 200 * 200, delta=200 * 200 * 0.02)

    def test_the_buffer_is_in_feet(self):
        path = self._write(square_points(), 'pts.shp')

        ok, message, outputs = process_boundary_generation(
            path, self.dir, buffer_ft=32.8084, shape='convex'  # 10 m
        )

        self.assertTrue(ok, message)
        shp = [p for p in outputs['boundary_components'] if p.endswith('.shp')][0]
        area = float(gpd.read_file(shp).to_crs('EPSG:32614').area.iloc[0])
        expected = 200 * 200 + 4 * 200 * 10 + math.pi * 10 ** 2
        self.assertAlmostEqual(area, expected, delta=expected * 0.03)

    def test_it_writes_the_two_layer_files_as_well(self):
        path = self._write(square_points(), 'pts.shp')

        ok, message, outputs = process_boundary_generation(path, self.dir, shape='convex')

        self.assertTrue(ok, message)
        self.assertTrue(outputs['qgis_style'].endswith('.qml'))
        self.assertTrue(outputs['arcgis_layer'].endswith('.lyrx'))
        for key in ('qgis_style', 'arcgis_layer'):
            self.assertTrue(os.path.exists(outputs[key]), key)
        # Outline only: a filled polygon would hide the imagery underneath.
        self.assertIn('"style" v="no"', open(outputs['qgis_style']).read())

    def test_the_output_keeps_the_input_coordinate_system(self):
        path = self._write(square_points(crs='EPSG:4326'), 'wgs.shp')

        ok, message, outputs = process_boundary_generation(path, self.dir, shape='convex')

        self.assertTrue(ok, message)
        shp = [p for p in outputs['boundary_components'] if p.endswith('.shp')][0]
        self.assertEqual(gpd.read_file(shp).crs.to_epsg(), 4326)

    def test_an_input_already_in_utm_picks_a_real_zone(self):
        """Reading projected bounds as degrees gave EPSG:138886, which is not a CRS."""
        frame = square_points(crs='EPSG:32614')

        self.assertEqual(estimate_utm_crs(frame), 'EPSG:32614')

        path = self._write(frame, 'utm.shp')
        ok, message, _ = process_boundary_generation(path, self.dir, shape='convex')
        self.assertTrue(ok, message)

    def test_a_csv_with_swapped_columns_is_corrected(self):
        """Her own test file has them the wrong way round."""
        frame = square_points(crs='EPSG:4326')
        path = os.path.join(self.dir, 'swapped.csv')
        with open(path, 'w') as handle:
            handle.write('Longitude,Latitude,Yield\n')
            for geom in frame.geometry:
                # Deliberately the wrong way round, as the real file has them.
                handle.write(f'{geom.y},{geom.x},150\n')

        ok, message, outputs = process_boundary_generation(path, self.dir, shape='convex')

        self.assertTrue(ok, message)
        shp = [p for p in outputs['boundary_components'] if p.endswith('.shp')][0]
        area = float(gpd.read_file(shp).to_crs('EPSG:32614').area.iloc[0])
        self.assertAlmostEqual(area, 200 * 200, delta=200 * 200 * 0.05)

    def test_a_csv_of_projected_metres_is_refused_with_advice(self):
        path = os.path.join(self.dir, 'utm.csv')
        with open(path, 'w') as handle:
            handle.write('X,Y\n')
            handle.write('637535.4,4517920.1\n637555.4,4517940.1\n')

        ok, message, _ = process_boundary_generation(path, self.dir)

        self.assertFalse(ok)
        self.assertIn('not latitude/longitude in degrees', message)

    def test_stray_fixes_are_dropped(self):
        frame = square_points()
        stray = gpd.GeoDataFrame(
            {'id': [9999]}, geometry=[Point(EAST + 50000, NORTH + 50000)],
            crs='EPSG:32614',
        )
        path = self._write(gpd.pd.concat([frame, stray]), 'stray.shp')

        ok, message, outputs = process_boundary_generation(
            path, self.dir, shape='convex', remove_outliers=True
        )

        self.assertTrue(ok, message)
        self.assertIn('outlier', message)
        shp = [p for p in outputs['boundary_components'] if p.endswith('.shp')][0]
        area = float(gpd.read_file(shp).to_crs('EPSG:32614').area.iloc[0])
        # Without the filter the hull would stretch 50 km and be vast.
        self.assertLess(area, 200 * 200 * 2)

    def test_an_unknown_shape_is_refused(self):
        path = self._write(square_points(), 'pts.shp')
        ok, message, _ = process_boundary_generation(path, self.dir, shape='blob')
        self.assertFalse(ok)
        self.assertIn('Unknown shape', message)
        for key in SHAPE_CHOICES:
            self.assertIn(key, message)

    def test_a_missing_file_is_refused(self):
        ok, message, _ = process_boundary_generation(
            os.path.join(self.dir, 'nope.shp'), self.dir
        )
        self.assertFalse(ok)
        self.assertIn('not found', message)
