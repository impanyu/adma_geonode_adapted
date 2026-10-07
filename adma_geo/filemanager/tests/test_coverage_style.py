"""
Point to Polygon coverage output, coloured by OVLP_PCT.

In GeoServer's default style the tool's output -- tens of thousands of thin
polygons -- is mostly outline and draws as one black block, hiding the very
thing the layer exists to show. These pin down that the overlap style keeps the
tool's own 25% flag as a class boundary, is applied only to layers that carry
the column, and that the key in the viewer comes from the same table.
"""
import os
import tempfile
import xml.etree.ElementTree as ET
from unittest.mock import MagicMock, patch

import geopandas as gpd
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from shapely.geometry import box

from filemanager.gis_utils import GeoServerAPI
from filemanager.models import File, Folder
from filemanager.PointToPolygonTool_SV import OVERLAP_FLAG_PCT

User = get_user_model()
MEDIA = tempfile.mkdtemp(prefix='adma-cov-')
SLD = '{http://www.opengis.net/sld}'


def coverage_frame(with_overlap=True):
    frame = gpd.GeoDataFrame(
        {'Yld_Mass_D': [210.0, 190.0]},
        geometry=[box(0, 0, 6, 2), box(0, 2, 6, 4)], crs='EPSG:32614')
    if with_overlap:
        frame['OVLP_PCT'] = [0.0, 40.0]
    return frame


class OverlapStyleTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.api = GeoServerAPI()

    def write(self, frame):
        path = os.path.join(self.tmp.name, 'harvest_rect.shp')
        frame.to_file(path)
        return path

    def test_the_sld_is_valid_and_has_one_rule_per_class(self):
        root = ET.fromstring(GeoServerAPI.overlap_sld())
        rules = root.findall(f'.//{SLD}Rule')
        self.assertEqual(len(rules), len(GeoServerAPI.OVERLAP_CLASSES))
        titles = [r.find(f'{SLD}Title').text for r in rules]
        self.assertEqual(titles, [c[3] for c in GeoServerAPI.OVERLAP_CLASSES])

    def test_the_classes_meet_without_gaps(self):
        classes = GeoServerAPI.OVERLAP_CLASSES
        self.assertIsNone(classes[0][0])
        self.assertIsNone(classes[-1][1])
        for (_, upper, _, _), (lower, _, _, _) in zip(classes, classes[1:]):
            self.assertEqual(upper, lower)

    def test_the_tools_own_flag_is_a_class_boundary(self):
        """Everything flagged by the tool is in a 'flagged' class, nothing else is."""
        lowers = [c[0] for c in GeoServerAPI.OVERLAP_CLASSES]
        self.assertIn(OVERLAP_FLAG_PCT, lowers)
        for low, _, _, title in GeoServerAPI.OVERLAP_CLASSES:
            flagged = low is not None and low >= OVERLAP_FLAG_PCT
            self.assertEqual('flagged' in title, flagged, title)

    def test_colours_are_distinct(self):
        colours = [c[2] for c in GeoServerAPI.OVERLAP_CLASSES]
        self.assertEqual(len(set(colours)), len(colours))

    def test_a_coverage_layer_gets_the_style(self):
        path = self.write(coverage_frame())
        calls = []

        def put(url, **kwargs):
            calls.append((url, kwargs.get('json')))
            return MagicMock(status_code=200)

        with patch('filemanager.gis_utils.requests.get', return_value=MagicMock(status_code=200)), \
             patch('filemanager.gis_utils.requests.put', side_effect=put):
            self.assertTrue(self.api.style_coverage_by_overlap('a_layer', path))

        layer_calls = [c for c in calls if '/rest/layers/' in c[0]]
        self.assertEqual(layer_calls[0][1], {'layer': {'defaultStyle': {'name': GeoServerAPI.OVERLAP_STYLE}}})
        # The existing style is rewritten, so a change to the classes reaches
        # layers published before it.
        self.assertTrue(any(c[0].endswith(f'/rest/styles/{GeoServerAPI.OVERLAP_STYLE}') for c in calls))

    def test_any_other_vector_is_left_alone(self):
        path = self.write(coverage_frame(with_overlap=False))
        with patch('filemanager.gis_utils.requests.get') as get, \
             patch('filemanager.gis_utils.requests.put') as put:
            self.assertFalse(self.api.style_coverage_by_overlap('a_layer', path))
        get.assert_not_called()
        put.assert_not_called()

    def test_a_geoserver_failure_does_not_raise(self):
        path = self.write(coverage_frame())
        with patch('filemanager.gis_utils.requests.get', side_effect=OSError('down')):
            self.assertFalse(self.api.style_coverage_by_overlap('a_layer', path))


@override_settings(MEDIA_ROOT=MEDIA)
class OverlapLegendTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('grower', password='correct-horse-battery')
        self.client.login(username='grower', password='correct-horse-battery')
        self.folder = Folder.objects.create(name='field', owner=self.user)
        os.makedirs(os.path.join(MEDIA, 'uploads'), exist_ok=True)

    def published(self, name, frame):
        frame.to_file(os.path.join(MEDIA, 'uploads', name))
        f = File(name=name, folder=self.folder, owner=self.user, file_size=1,
                 gis_status='published', geoserver_layer_name='layer_' + name[:-4],
                 geoserver_workspace='adma_geo')
        f.file.name = f'uploads/{name}'
        f.save()
        return f

    def test_the_viewer_shows_the_key_for_a_coverage_layer(self):
        f = self.published('harvest_rect.shp', coverage_frame())
        response = self.client.get(reverse('filemanager:map_viewer', args=[f.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Overlap with earlier passes')
        for _, _, colour, label in GeoServerAPI.OVERLAP_CLASSES:
            self.assertContains(response, colour)
            self.assertContains(response, label)

    def test_no_key_for_an_ordinary_layer(self):
        f = self.published('plots.shp', coverage_frame(with_overlap=False))
        response = self.client.get(reverse('filemanager:map_viewer', args=[f.id]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Overlap with earlier passes')
