"""
Styling a raster whose band count would otherwise be misread.

GeoServer reads a two-band coverage as greyscale plus alpha, so the second band
silently becomes transparency. That is how the active-fire layer came to be
invisible on the site -- its radiative-power band was all zeros, so alpha was
zero everywhere -- and why MODIS vegetation indices were a third see-through.

Declaring the bands as data inside the GeoTIFF does not help, because GeoServer
ignores ExtraSamples. What works is an explicit style naming which band to draw,
and these tests pin down when it gets applied.
"""
import os
import shutil
import tempfile
from unittest.mock import MagicMock, patch

from django.test import TestCase

from filemanager.gis_utils import GeoServerAPI


def _raster(path, count, palette=None, dtype='uint8'):
    import numpy as np
    import rasterio

    profile = dict(driver='GTiff', height=4, width=4, count=count, dtype=dtype,
                   crs=rasterio.crs.CRS.from_epsg(4326),
                   transform=rasterio.transform.from_bounds(0, 0, 1, 1, 4, 4))
    with rasterio.open(path, 'w', **profile) as destination:
        destination.write(np.ones((count, 4, 4), dtype))
        if palette:
            destination.write_colormap(1, palette)
    return path


class GreyBandStyleTests(TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix='adma-style-')
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.api = GeoServerAPI()

    def _styled(self, count, palette=None):
        """Returns the style name applied to the layer, or None."""
        path = _raster(os.path.join(self.directory, f'{count}.tif'),
                       count, palette=palette)
        applied = {}

        def get(url, **kwargs):
            # The shared style already exists, so nothing is created.
            return MagicMock(status_code=200)

        def put(url, **kwargs):
            if '/rest/layers/' in url:
                applied['style'] = (kwargs.get('json') or {}) \
                    .get('layer', {}).get('defaultStyle', {}).get('name')
            return MagicMock(status_code=200)

        with patch('filemanager.gis_utils.requests.get', side_effect=get), \
             patch('filemanager.gis_utils.requests.put', side_effect=put):
            self.api._style_multiband_as_grey('a_layer', path)
        return applied.get('style')

    def test_a_two_band_raster_is_told_which_band_to_draw(self):
        self.assertEqual(self._styled(2), self.api.GREY_BAND_STYLE)

    def test_a_four_band_raster_is_too(self):
        """Four bands would be read as RGBA, with band four as transparency."""
        self.assertEqual(self._styled(4), self.api.GREY_BAND_STYLE)

    def test_a_single_band_raster_is_left_alone(self):
        self.assertIsNone(self._styled(1))

    def test_three_bands_are_left_alone(self):
        """Red, green, blue is the right reading for imagery."""
        self.assertIsNone(self._styled(3))

    def test_a_paletted_raster_keeps_its_own_colours(self):
        """A grey ramp would throw away the palette, which is the whole meaning."""
        self.assertIsNone(self._styled(2, palette={0: (0, 0, 0, 255),
                                                   1: (255, 0, 0, 255)}))

    def test_the_style_names_band_one_as_a_grey_channel(self):
        sld = self.api.GREY_BAND_SLD
        self.assertIn('<GrayChannel>', sld)
        self.assertIn('<SourceChannelName>1</SourceChannelName>', sld)
        self.assertIn(self.api.GREY_BAND_STYLE, sld)

    def test_the_shared_style_is_created_only_when_it_is_missing(self):
        calls = []

        with patch('filemanager.gis_utils.requests.get',
                   return_value=MagicMock(status_code=200)), \
             patch('filemanager.gis_utils.requests.post',
                   side_effect=lambda *a, **k: calls.append(a) or MagicMock(status_code=201)):
            self.assertTrue(self.api._ensure_grey_band_style())
        self.assertEqual(calls, [], 'an existing style should not be recreated')

        with patch('filemanager.gis_utils.requests.get',
                   return_value=MagicMock(status_code=404)), \
             patch('filemanager.gis_utils.requests.post',
                   side_effect=lambda *a, **k: calls.append(a) or MagicMock(status_code=201)):
            self.assertTrue(self.api._ensure_grey_band_style())
        self.assertEqual(len(calls), 1)

    def test_a_failure_to_style_does_not_break_publishing(self):
        """A layer that renders oddly beats a layer that never gets published."""
        path = _raster(os.path.join(self.directory, 'two.tif'), 2)
        with patch('filemanager.gis_utils.requests.get',
                   side_effect=OSError('geoserver is down')):
            self.api._style_multiband_as_grey('a_layer', path)  # must not raise
