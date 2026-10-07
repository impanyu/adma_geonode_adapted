"""
Sreeja Vinod's Point to Polygon Coverage tool.

The geometry itself was checked against her own outputs: on the files she sent
(23_2436S_HV_Raw, 1330_31_UNL_ARDC, the NH3 as-applied and 21_BMOR_HF_Raw),
in every heading mode, ADMA's entry point writes polygons and attributes
identical to her script run with the default answers. What these tests pin
down is what that comparison cannot see: that the worker never stops at a
prompt, that "compute from the points" stays distinct from "use the
suggested column", and that the endpoints pass the choice through.
"""
import builtins
import os
import tempfile
from unittest.mock import patch

import geopandas as gpd
import numpy as np
import pandas as pd
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from shapely.geometry import Point

from filemanager.models import File, Folder
from filemanager.PointToPolygonTool_SV import (
    AUTO,
    HEADING_ADJUSTMENTS,
    M2_PER_AC,
    load_points,
    process_point_to_polygon,
    suggest_columns,
)

User = get_user_model()
MEDIA = tempfile.mkdtemp(prefix='adma-p2p-')

# Near Mead, Nebraska, in UTM 14N.
EAST, NORTH = 700000.0, 4560000.0
FT = 0.3048


def passes(n_passes=2, n_points=60, step_m=2.0, width_ft=20.0, gap_ft=20.0,
           extra=None):
    """Up-and-back passes of a 20 ft header, one point every 2 m.

    gap_ft is the distance between pass centre lines: 20 ft for a header that
    lines up exactly, less than that for passes that overlap.
    """
    rows, geoms = [], []
    for p in range(n_passes):
        x = EAST + p * gap_ft * FT
        northbound = p % 2 == 0
        for i in range(n_points):
            y = NORTH + (i if northbound else n_points - 1 - i) * step_m
            geoms.append(Point(x, y))
            rows.append({
                'Pass_Num': p + 1,
                'Swth_Wdth_': width_ft,
                'Distance_f': step_m / FT,
                'Track_deg_': 0.0 if northbound else 180.0,
                'Yld_Mass_D': 200.0 + i,
                'Area_Count': 'On',
                **(extra or {}),
            })
    return gpd.GeoDataFrame(rows, geometry=geoms, crs='EPSG:32614')


class PointToPolygonTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name

    def write(self, frame, name='pts.shp'):
        path = os.path.join(self.dir, name)
        frame.to_file(path)
        return path

    def run_tool(self, path, **kwargs):
        ok, message, outputs = process_point_to_polygon(path, os.path.join(self.dir, 'out'), **kwargs)
        self.assertTrue(ok, message)
        return message, gpd.read_file(outputs['coverage_components'][0])

    def test_a_pass_becomes_one_unbroken_strip(self):
        """Neighbours share edges: no gaps, no overlap, area = width x length."""
        _, out = self.run_tool(self.write(passes(n_passes=1)))

        self.assertEqual(len(out), 60)
        metric = out.to_crs('EPSG:32614')
        summed = metric.area.sum()
        covered = metric.union_all().area
        self.assertAlmostEqual(covered, summed, delta=summed * 1e-6)
        # 60 points 2 m apart cover 59 steps plus half a step at each end.
        self.assertAlmostEqual(summed, 20 * FT * 60 * 2.0, delta=1.0)
        self.assertEqual(out['OVLP_FLAG'].sum(), 0)

    def test_overlapping_passes_are_flagged_on_the_later_one(self):
        # Centre lines 12 ft apart with a 20 ft header: 8 ft, 40%, overlap.
        _, out = self.run_tool(self.write(passes(gap_ft=12.0)))

        first, second = out[out['Pass_Num'] == 1], out[out['Pass_Num'] == 2]
        self.assertEqual(first['OVLP_FLAG'].sum(), 0)
        self.assertGreater(second['OVLP_FLAG'].mean(), 0.9)
        self.assertAlmostEqual(second['OVLP_PCT'].median(), 40.0, delta=1.0)
        self.assertAlmostEqual(second['OVLP_FT'].median(), 8.0, delta=0.2)

    def test_the_point_attributes_are_kept_and_the_crs_is_the_inputs(self):
        frame = passes(n_passes=1).to_crs('EPSG:4326')
        _, out = self.run_tool(self.write(frame))

        self.assertEqual(out.crs.to_epsg(), 4326)
        for column in ('Yld_Mass_D', 'Pass_Num', 'BUF_W_FT', 'AREA_AC', 'OVLP_PCT', 'PARTIAL'):
            self.assertIn(column, out.columns)
        self.assertEqual(list(out['Yld_Mass_D'][:3]), [200.0, 201.0, 202.0])

    def test_left_alone_the_columns_are_the_ones_the_script_would_suggest(self):
        message, _ = self.run_tool(self.write(passes(n_passes=1)))
        self.assertIn('Width Swth_Wdth_', message)
        self.assertIn('distance Distance_f', message)
        self.assertIn('heading Track_deg_', message)

    def test_compute_from_points_is_not_replaced_by_the_suggestion(self):
        """None means "compute", as 0 does at her prompt -- not "guess"."""
        message, _ = self.run_tool(self.write(passes(n_passes=1)),
                                   distance_col=None, heading_col=None)
        self.assertIn('distance computed point spacing', message)
        self.assertIn('heading computed from consecutive points', message)
        self.assertNotIn('Distance_f', message)

    def test_a_column_that_is_not_there_is_refused_by_name(self):
        ok, message, _ = process_point_to_polygon(
            self.write(passes(n_passes=1)), self.dir, width_col='Header_Width')
        self.assertFalse(ok)
        self.assertIn('Header_Width', message)
        self.assertIn('Swth_Wdth_', message)

    def test_no_width_column_says_which_columns_there_are(self):
        frame = passes(n_passes=1).rename(columns={'Swth_Wdth_': 'Implement'})
        ok, message, _ = process_point_to_polygon(self.write(frame), self.dir)
        self.assertFalse(ok)
        self.assertIn('swath width', message)
        self.assertIn('Implement', message)

    def test_a_named_width_column_is_used_even_without_a_telling_name(self):
        frame = passes(n_passes=1).rename(columns={'Swth_Wdth_': 'Implement'})
        message, out = self.run_tool(self.write(frame), width_col='Implement')
        self.assertIn('Width Implement', message)
        self.assertEqual(out['BUF_W_FT'].median(), 20.0)

    def test_the_worker_is_never_asked_a_question(self):
        """A prompt in a Celery worker hangs it; every gap must be an error."""
        frame = passes(n_passes=1)
        path = self.write(frame)
        os.remove(path[:-4] + '.prj')

        with patch.object(builtins, 'input', side_effect=AssertionError('prompted')):
            ok, message, _ = process_point_to_polygon(path, self.dir)
        self.assertFalse(ok)
        self.assertIn('EPSG', message)

    def test_a_missing_prj_can_be_supplied_as_an_epsg_code(self):
        path = self.write(passes(n_passes=1))
        os.remove(path[:-4] + '.prj')
        _, out = self.run_tool(path, epsg=32614)
        self.assertEqual(len(out), 60)

    def test_a_lon_lat_csv_works_and_a_projected_one_asks_for_its_code(self):
        frame = passes(n_passes=1)
        lonlat = frame.to_crs('EPSG:4326')
        csv = pd.DataFrame(lonlat.drop(columns='geometry'))
        csv['Longitude'], csv['Latitude'] = lonlat.geometry.x, lonlat.geometry.y
        path = os.path.join(self.dir, 'pts.csv')
        csv.to_csv(path, index=False)
        _, out = self.run_tool(path)
        self.assertEqual(len(out), 60)

        projected = pd.DataFrame(frame.drop(columns='geometry'))
        projected['Easting'], projected['Northing'] = frame.geometry.x, frame.geometry.y
        path = os.path.join(self.dir, 'utm.csv')
        projected.to_csv(path, index=False)
        with patch.object(builtins, 'input', side_effect=AssertionError('prompted')):
            ok, message, _ = process_point_to_polygon(path, self.dir)
        self.assertFalse(ok)
        self.assertIn('EPSG', message)

    def test_every_heading_adjustment_runs_and_an_unknown_one_is_refused(self):
        path = self.write(passes(n_passes=1))
        for key in HEADING_ADJUSTMENTS:
            with self.subTest(adjust=key):
                self.run_tool(path, heading_adjust=key)
        ok, message, _ = process_point_to_polygon(path, self.dir, heading_adjust='wobble')
        self.assertFalse(ok)
        self.assertIn('smooth', message)

    def test_off_records_are_dropped_before_building(self):
        frame = passes(n_passes=1)
        frame.loc[:9, 'Area_Count'] = 'Off'
        _, out = self.run_tool(self.write(frame))
        self.assertEqual(len(out), 50)

    def test_suggestions_match_the_script_guesses(self):
        frame = passes(n_passes=1, extra={'Y_Offset_f': 0.0})
        self.assertEqual(suggest_columns(frame), {
            'width': 'Swth_Wdth_', 'distance': 'Distance_f',
            'heading': 'Track_deg_', 'offset': 'Y_Offset_f', 'pass': 'Pass_Num',
        })

    def test_load_points_never_prompts(self):
        path = os.path.join(self.dir, 'bad.csv')
        pd.DataFrame({'a': [1], 'b': [2]}).to_csv(path, index=False)
        with patch.object(builtins, 'input', side_effect=AssertionError('prompted')):
            with self.assertRaises(ValueError):
                load_points(path)

    def test_area_is_reported_in_acres(self):
        _, out = self.run_tool(self.write(passes(n_passes=1)))
        metric = out.to_crs('EPSG:32614')
        self.assertAlmostEqual(out['AREA_AC'].sum(), metric.area.sum() / M2_PER_AC, places=4)


@override_settings(MEDIA_ROOT=MEDIA)
class PointToPolygonEndpointTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('grower', password='correct-horse-battery')
        self.client.login(username='grower', password='correct-horse-battery')
        self.folder = Folder.objects.create(name='field', owner=self.user)
        os.makedirs(os.path.join(MEDIA, 'uploads'), exist_ok=True)
        passes(n_passes=1, extra={'Y_Offset_f': 0.0}).to_file(
            os.path.join(MEDIA, 'uploads', 'harvest.shp'))
        self.shp = File(name='harvest.shp', folder=self.folder, owner=self.user, file_size=1)
        self.shp.file.name = 'uploads/harvest.shp'
        self.shp.save()

    def post(self, payload):
        return self.client.post(reverse('filemanager:run_point_to_polygon'),
                                data=payload, content_type='application/json')

    def test_the_columns_endpoint_preselects_the_scripts_guesses(self):
        response = self.client.get(
            reverse('filemanager:point_to_polygon_columns', args=[self.shp.id]))
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertIn('Swth_Wdth_', data['columns'])
        self.assertEqual(data['suggested']['width'], 'Swth_Wdth_')
        self.assertEqual(data['suggested']['offset'], 'Y_Offset_f')

    @patch('filemanager.native_tool_tasks.run_point_to_polygon_task.delay')
    def test_compute_from_points_reaches_the_task_as_none(self, delay):
        delay.return_value.id = 'task-p2p'
        response = self.post({'file_id': str(self.shp.id), 'width_col': 'Swth_Wdth_',
                              'distance_col': None, 'heading_col': '',
                              'heading_adjust': 'smooth'})
        self.assertEqual(response.status_code, 200, response.content)
        kwargs = delay.call_args.kwargs
        self.assertEqual(kwargs['width_col'], 'Swth_Wdth_')
        self.assertIsNone(kwargs['distance_col'])
        self.assertIsNone(kwargs['heading_col'])
        self.assertEqual(kwargs['heading_adjust'], 'smooth')
        self.assertEqual(kwargs['requesting_user_id'], self.user.id)

    @patch('filemanager.native_tool_tasks.run_point_to_polygon_task.delay')
    def test_columns_left_out_are_auto(self, delay):
        delay.return_value.id = 'task-p2p'
        self.post({'file_id': str(self.shp.id)})
        kwargs = delay.call_args.kwargs
        self.assertEqual((kwargs['width_col'], kwargs['distance_col'], kwargs['heading_col']),
                         (AUTO, AUTO, AUTO))

    def test_bad_choices_are_refused_before_queueing(self):
        for payload in ({'file_id': str(self.shp.id), 'heading_adjust': 'wobble'},
                        {'file_id': str(self.shp.id), 'epsg': 'utm'}):
            with self.subTest(payload=payload):
                self.assertEqual(self.post(payload).status_code, 400)
