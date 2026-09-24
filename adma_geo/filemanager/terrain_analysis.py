"""
Slope, aspect, hillshade and relative position from an elevation raster.

Terrain drives where water goes, and so where a field is wet, where it erodes
and where yield falls away. These are the standard derivatives, computed from
a DEM -- the Public Data tool can fetch one from USGS.

The one thing worth getting right is units. A DEM in degrees has pixels whose
width in metres depends on latitude, so a gradient taken straight from degree
spacing produces a slope that is wrong everywhere and wrong by a different
factor north to south. Such a raster is reprojected to its local UTM zone
before anything is measured.
"""
import logging
import math
import os

import numpy as np
import rasterio
from rasterio.warp import Resampling, calculate_default_transform, reproject

logger = logging.getLogger(__name__)

PRODUCTS = {
    'slope': 'Steepness in degrees.',
    'aspect': 'The compass direction the ground faces, in degrees from north.',
    'hillshade': 'Shaded relief, for reading the terrain by eye.',
    'tpi': 'Topographic position: height above or below the local surroundings, '
           'which separates ridges from hollows.',
}

DEFAULT_PRODUCTS = ['slope', 'aspect', 'hillshade']


def _utm_epsg(longitude, latitude):
    """The UTM zone covering a point, as an EPSG code."""
    zone = int((longitude + 180.0) / 6.0) + 1
    return (32600 if latitude >= 0 else 32700) + zone


def _to_metric(source):
    """
    Return ``(elevation, x_size, y_size, profile)`` with spacing in metres.

    A projected DEM is read as it is. A geographic one is reprojected to its
    local UTM zone first, because a degree is not a unit of distance.
    """
    if source.crs is None:
        raise ValueError('The elevation raster has no CRS, so slope cannot be measured.')

    if source.crs.is_projected:
        data = source.read(1, masked=True).astype('float64')
        transform = source.transform
        profile = source.profile.copy()
        return data, abs(transform.a), abs(transform.e), profile

    west, south, east, north = source.bounds
    target = rasterio.crs.CRS.from_epsg(
        _utm_epsg((west + east) / 2.0, (south + north) / 2.0)
    )
    logger.info('Reprojecting a geographic DEM to %s before measuring slope', target)

    transform, width, height = calculate_default_transform(
        source.crs, target, source.width, source.height, *source.bounds
    )
    destination = np.empty((height, width), dtype='float32')
    reproject(
        source=rasterio.band(source, 1),
        destination=destination,
        src_transform=source.transform, src_crs=source.crs,
        dst_transform=transform, dst_crs=target,
        src_nodata=source.nodata, dst_nodata=source.nodata,
        resampling=Resampling.bilinear,
    )

    data = np.ma.masked_invalid(destination.astype('float64'))
    if source.nodata is not None:
        data = np.ma.masked_equal(data, source.nodata)

    profile = source.profile.copy()
    profile.update(crs=target, transform=transform, width=width, height=height)
    return data, abs(transform.a), abs(transform.e), profile


def _gradients(elevation, x_size, y_size):
    filled = elevation.filled(np.nan) if np.ma.isMaskedArray(elevation) else elevation
    # np.gradient's first axis is rows, which run north to south, so the y
    # gradient is negated to point up-north.
    dz_dy, dz_dx = np.gradient(filled, y_size, x_size)
    return dz_dx, -dz_dy


def _slope_degrees(dz_dx, dz_dy):
    return np.degrees(np.arctan(np.hypot(dz_dx, dz_dy)))


def _aspect_degrees(dz_dx, dz_dy):
    aspect = np.degrees(np.arctan2(dz_dy, -dz_dx))
    aspect = 90.0 - aspect
    aspect = np.where(aspect < 0, aspect + 360.0, aspect)
    # Flat ground faces nowhere; -1 is the usual marker rather than a
    # direction invented from rounding noise.
    return np.where(np.hypot(dz_dx, dz_dy) < 1e-12, -1.0, aspect)


def _hillshade(dz_dx, dz_dy, azimuth=315.0, altitude=45.0):
    slope = np.arctan(np.hypot(dz_dx, dz_dy))
    aspect = np.arctan2(dz_dy, -dz_dx)
    zenith = math.radians(90.0 - altitude)
    azimuth_rad = math.radians(360.0 - azimuth + 90.0)
    shaded = (
        math.cos(zenith) * np.cos(slope)
        + math.sin(zenith) * np.sin(slope) * np.cos(azimuth_rad - aspect)
    )
    return np.clip(shaded * 255.0, 0, 255)


def _tpi(elevation, radius=3):
    """Elevation minus the mean of the surrounding window."""
    from scipy.ndimage import uniform_filter

    filled = elevation.filled(np.nan) if np.ma.isMaskedArray(elevation) else elevation
    valid = np.isfinite(filled)
    work = np.where(valid, filled, 0.0)

    size = radius * 2 + 1
    # Averaging the mask alongside the data keeps nodata from being counted as
    # zero elevation, which would drag every nearby value down.
    total = uniform_filter(work, size=size, mode='nearest')
    weight = uniform_filter(valid.astype('float64'), size=size, mode='nearest')
    with np.errstate(invalid='ignore', divide='ignore'):
        neighbourhood = np.where(weight > 0, total / weight, np.nan)
    return filled - neighbourhood


def analyse_terrain(dem_path, output_dir, products=None, azimuth=315.0, altitude=45.0,
                    tpi_radius=3, output_basename=None):
    """
    Derive terrain products from ``dem_path``.

    Returns ``(success, message, output_files)``.
    """
    products = list(products or DEFAULT_PRODUCTS)
    unknown = [p for p in products if p not in PRODUCTS]
    if unknown:
        return False, f'Unknown product(s): {", ".join(unknown)}.', {}
    if not products:
        return False, 'Choose at least one terrain product.', {}

    if not os.path.exists(dem_path):
        return False, f'Elevation raster not found: {os.path.basename(dem_path)}', {}

    try:
        with rasterio.open(dem_path) as source:
            elevation, x_size, y_size, profile = _to_metric(source)
    except ValueError as exc:
        return False, str(exc), {}
    except Exception as exc:
        logger.exception('Could not read %s', dem_path)
        return False, f'Could not read the elevation raster: {exc}', {}

    if elevation.size == 0:
        return False, 'The elevation raster is empty.', {}

    dz_dx, dz_dy = _gradients(elevation, x_size, y_size)

    computed = {}
    if 'slope' in products:
        computed['slope'] = _slope_degrees(dz_dx, dz_dy)
    if 'aspect' in products:
        computed['aspect'] = _aspect_degrees(dz_dx, dz_dy)
    if 'hillshade' in products:
        computed['hillshade'] = _hillshade(dz_dx, dz_dy, azimuth, altitude)
    if 'tpi' in products:
        computed['tpi'] = _tpi(elevation, radius=int(tpi_radius))

    os.makedirs(output_dir, exist_ok=True)
    base = output_basename or os.path.splitext(os.path.basename(dem_path))[0]

    output_files = {}
    for name, array in computed.items():
        out_profile = profile.copy()
        out_profile.update(dtype='float32', count=1, nodata=float('nan'),
                           compress='lzw', driver='GTiff')
        path = os.path.join(output_dir, f'{base}_{name}.tif')
        with rasterio.open(path, 'w', **out_profile) as destination:
            destination.write(array.astype('float32'), 1)
        output_files[f'{name}_tif'] = path

    finite = computed.get('slope')
    if finite is not None:
        values = finite[np.isfinite(finite)]
        detail = (
            f' Slope runs {values.min():.1f} to {values.max():.1f} degrees, '
            f'mean {values.mean():.1f}.' if values.size else ''
        )
    else:
        detail = ''

    message = (
        f'Computed {", ".join(products)} at {x_size:.1f} m pixels.{detail}'
    )
    return True, message, output_files
