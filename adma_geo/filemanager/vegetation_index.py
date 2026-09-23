"""
Compute a vegetation index raster from reflectance bands.

The SI Tool already consumes NDRE rasters; this produces them, from either a
single multi-band image or one GeoTIFF per band. Alongside the index raster it
writes a coloured PNG, because a bare single-band float GeoTIFF is not
something anyone can check at a glance.

Every index here is a normalised difference of two bands, give or take a soil
adjustment, so they share one implementation and differ only in which bands
they name.
"""
import logging
import os

import matplotlib
matplotlib.use('Agg')  # No display on the worker.

import matplotlib.pyplot as plt
import numpy as np
import rasterio

logger = logging.getLogger(__name__)


class IndexDefinition:
    """One index: the bands it needs and how to combine them."""

    def __init__(self, key, name, bands, description, formula):
        self.key = key
        self.name = name
        self.bands = bands
        self.description = description
        self.formula = formula

    def compute(self, arrays, soil_factor=0.5):
        return self.formula(arrays, soil_factor)


def _normalised_difference(a, b):
    """(a - b) / (a + b), with the zero denominator left as NaN, not zero.

    A zero denominator means no reflectance was recorded, which is absence of
    data. Returning 0.0 there would place those pixels mid-scale and pull every
    zonal mean computed downstream toward it.
    """
    denominator = a + b
    out = np.full(a.shape, np.nan, dtype='float32')
    valid = denominator != 0
    out[valid] = (a[valid] - b[valid]) / denominator[valid]
    return out


INDEX_DEFINITIONS = {
    d.key: d for d in [
        IndexDefinition(
            'ndvi', 'NDVI', ['nir', 'red'],
            'Normalised Difference Vegetation Index -- general canopy vigour.',
            lambda a, _s: _normalised_difference(a['nir'], a['red']),
        ),
        IndexDefinition(
            'ndre', 'NDRE', ['nir', 'rededge'],
            'Normalised Difference Red Edge -- sensitive to nitrogen status in '
            'a closed canopy, where NDVI saturates.',
            lambda a, _s: _normalised_difference(a['nir'], a['rededge']),
        ),
        IndexDefinition(
            'gndvi', 'GNDVI', ['nir', 'green'],
            'Green NDVI -- responds to chlorophyll rather than overall biomass.',
            lambda a, _s: _normalised_difference(a['nir'], a['green']),
        ),
        IndexDefinition(
            'savi', 'SAVI', ['nir', 'red'],
            'Soil Adjusted Vegetation Index -- NDVI damped for bare soil, for '
            'early season or sparse stands.',
            lambda a, s: (
                (a['nir'] - a['red']) * (1.0 + s)
                / np.where((a['nir'] + a['red'] + s) == 0, np.nan, a['nir'] + a['red'] + s)
            ).astype('float32'),
        ),
    ]
}


def _read_band(source, band_number):
    array = source.read(band_number).astype('float32')
    if source.nodata is not None:
        array[array == source.nodata] = np.nan
    return array


def _write_preview(index_array, png_path, title, cmap='RdYlGn'):
    finite = index_array[np.isfinite(index_array)]
    if finite.size == 0:
        return None

    # Clip the colour scale to the middle 98% so a handful of extreme pixels
    # cannot flatten the whole image into one colour.
    low, high = np.percentile(finite, [1, 99])
    if low == high:
        low, high = float(finite.min()), float(finite.max())
    if low == high:
        high = low + 1e-6

    figure, axes = plt.subplots(figsize=(8, 8))
    image = axes.imshow(index_array, cmap=cmap, vmin=low, vmax=high)
    axes.set_title(title)
    axes.axis('off')
    figure.colorbar(image, ax=axes, shrink=0.75)
    figure.tight_layout()
    figure.savefig(png_path, dpi=150, bbox_inches='tight')
    plt.close(figure)
    return png_path


def compute_vegetation_index(
    index_key,
    band_sources,
    output_dir,
    soil_factor=0.5,
    output_basename=None,
):
    """
    Compute one index and write a GeoTIFF plus a PNG preview.

    ``band_sources`` maps each band name the index needs ('nir', 'red', ...) to
    ``(path, band_number)``. Pointing several names at the same path with
    different band numbers is how a single multi-band image is used.

    Returns ``(success, message, output_files)``.
    """
    definition = INDEX_DEFINITIONS.get(index_key)
    if definition is None:
        return False, (
            f'Unknown index {index_key!r}. Available: '
            f'{", ".join(sorted(INDEX_DEFINITIONS))}.'
        ), {}

    missing = [b for b in definition.bands if b not in band_sources]
    if missing:
        return False, (
            f'{definition.name} needs the {", ".join(missing)} band(s), '
            'which were not supplied.'
        ), {}

    arrays = {}
    profile = None
    shapes = {}

    for band_name in definition.bands:
        path, band_number = band_sources[band_name]
        if not os.path.exists(path):
            return False, f'Band file not found: {os.path.basename(path)}', {}
        try:
            with rasterio.open(path) as source:
                if band_number < 1 or band_number > source.count:
                    return False, (
                        f'Band {band_number} is out of range for '
                        f'{os.path.basename(path)}, which has {source.count}.'
                    ), {}
                arrays[band_name] = _read_band(source, band_number)
                shapes[band_name] = arrays[band_name].shape
                if profile is None:
                    profile = source.profile.copy()
        except Exception as exc:
            return False, f'Could not read {os.path.basename(path)}: {exc}', {}

    distinct = set(shapes.values())
    if len(distinct) > 1:
        detail = ', '.join(f'{name}={shape}' for name, shape in shapes.items())
        return False, (
            'The bands do not share a grid, so they cannot be combined '
            f'pixel by pixel ({detail}). Clip or resample them to a common '
            'grid first.'
        ), {}

    with np.errstate(invalid='ignore', divide='ignore'):
        index_array = definition.compute(arrays, soil_factor).astype('float32')

    os.makedirs(output_dir, exist_ok=True)
    first_path = band_sources[definition.bands[0]][0]
    base = output_basename or (
        f'{os.path.splitext(os.path.basename(first_path))[0]}_{definition.key}'
    )

    profile.update(dtype='float32', count=1, nodata=float('nan'), compress='lzw')
    tif_path = os.path.join(output_dir, f'{base}.tif')
    with rasterio.open(tif_path, 'w', **profile) as destination:
        destination.write(index_array, 1)

    png_path = _write_preview(
        index_array, os.path.join(output_dir, f'{base}_preview.png'), definition.name
    )

    output_files = {'index_tif': tif_path}
    if png_path:
        output_files['preview_png'] = png_path

    finite = index_array[np.isfinite(index_array)]
    coverage = 100.0 * finite.size / index_array.size if index_array.size else 0.0
    if finite.size:
        message = (
            f'{definition.name} computed over {coverage:.1f}% of the grid; '
            f'range {finite.min():.3f} to {finite.max():.3f}, '
            f'mean {finite.mean():.3f}.'
        )
    else:
        message = (
            f'{definition.name} produced no valid pixels -- check that the '
            'band numbers and nodata value are right.'
        )

    return True, message, output_files
