"""
Clip a raster to a boundary, reproject it, or both.

Imagery arrives covering far more ground than the field of interest, and in
whatever projection the vendor chose. Both facts get in the way further down
the platform: the vegetation index tool needs its bands on one grid, and zonal
statistics reprojects the polygons to meet the raster, which is only sensible
when the raster is already somewhere reasonable.

Order matters here and is not configurable: clip first, then reproject. Doing
it the other way resamples the whole scene before throwing most of it away.
"""
import logging
import os

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.mask import mask as rio_mask
from rasterio.warp import Resampling, calculate_default_transform, reproject

logger = logging.getLogger(__name__)

RESAMPLING_METHODS = {
    'nearest': Resampling.nearest,
    'bilinear': Resampling.bilinear,
    'cubic': Resampling.cubic,
    'average': Resampling.average,
}

# Continuous imagery should be interpolated; a classified raster must not be,
# or averaging class 2 and class 4 invents a class 3 that means nothing.
DEFAULT_RESAMPLING = 'bilinear'


def _clip_to_boundary(source, boundary_path):
    """Return ``(array, profile)`` for the part of ``source`` inside the boundary."""
    boundary = gpd.read_file(boundary_path)
    if boundary.empty:
        raise ValueError('The boundary file contains no features.')

    geom_types = set(boundary.geom_type.dropna().unique())
    if not geom_types <= {'Polygon', 'MultiPolygon'}:
        raise ValueError(
            'The boundary must be polygons; this layer contains '
            f'{", ".join(sorted(geom_types))}.'
        )

    if boundary.crs is not None and source.crs is not None and boundary.crs != source.crs:
        boundary = boundary.to_crs(source.crs)

    geometries = [g for g in boundary.geometry if g is not None and not g.is_empty]
    if not geometries:
        raise ValueError('The boundary file has no usable geometry.')

    try:
        array, transform = rio_mask(source, geometries, crop=True, filled=True)
    except ValueError as exc:
        # rasterio says "Input shapes do not overlap raster" for this, which
        # is the single most likely mistake: mismatched areas, not bad data.
        raise ValueError(
            'The boundary does not overlap the raster. Check that they cover '
            f'the same ground ({exc}).'
        )

    profile = source.profile.copy()
    profile.update(
        height=array.shape[1], width=array.shape[2], transform=transform,
    )
    return array, profile


def clip_and_reproject(
    raster_path,
    output_dir,
    boundary_path=None,
    target_epsg=None,
    resampling=DEFAULT_RESAMPLING,
    output_basename=None,
):
    """
    Clip and/or reproject a raster.

    At least one of ``boundary_path`` and ``target_epsg`` must be given.
    Returns ``(success, message, output_files)``.
    """
    if not boundary_path and not target_epsg:
        return False, 'Choose a boundary to clip to, a target CRS, or both.', {}

    if not os.path.exists(raster_path):
        return False, f'Raster not found: {os.path.basename(raster_path)}', {}

    if boundary_path and not os.path.exists(boundary_path):
        return False, f'Boundary file not found: {os.path.basename(boundary_path)}', {}

    if resampling not in RESAMPLING_METHODS:
        return False, (
            f'Unknown resampling method {resampling!r}. Available: '
            f'{", ".join(sorted(RESAMPLING_METHODS))}.'
        ), {}

    os.makedirs(output_dir, exist_ok=True)
    base = output_basename or os.path.splitext(os.path.basename(raster_path))[0]
    steps = []

    try:
        with rasterio.open(raster_path) as source:
            source_crs = source.crs

            if boundary_path:
                array, profile = _clip_to_boundary(source, boundary_path)
                steps.append('clipped to the boundary')
            else:
                array, profile = source.read(), source.profile.copy()

        if target_epsg:
            destination_crs = rasterio.crs.CRS.from_epsg(int(target_epsg))
            if source_crs is None:
                return False, (
                    'The raster has no CRS recorded, so it cannot be '
                    'reprojected. Assign one first.'
                ), {}

            if destination_crs == source_crs:
                steps.append(f'already in EPSG:{target_epsg}, so left as it is')
            else:
                transform, width, height = calculate_default_transform(
                    source_crs, destination_crs,
                    profile['width'], profile['height'],
                    *rasterio.transform.array_bounds(
                        profile['height'], profile['width'], profile['transform']
                    ),
                )
                reprojected_profile = profile.copy()
                reprojected_profile.update(
                    crs=destination_crs, transform=transform,
                    width=width, height=height,
                )

                destination_array = np.empty(
                    (profile['count'], height, width), dtype=array.dtype
                )

                for index in range(profile['count']):
                    reproject(
                        source=array[index],
                        destination=destination_array[index],
                        src_transform=profile['transform'],
                        src_crs=source_crs,
                        dst_transform=transform,
                        dst_crs=destination_crs,
                        resampling=RESAMPLING_METHODS[resampling],
                        src_nodata=profile.get('nodata'),
                        dst_nodata=profile.get('nodata'),
                    )

                array, profile = destination_array, reprojected_profile
                steps.append(f'reprojected to EPSG:{target_epsg} ({resampling})')

    except ValueError as exc:
        return False, str(exc), {}
    except Exception as exc:
        logger.exception('Clip/reproject failed for %s', raster_path)
        return False, f'Could not process the raster: {exc}', {}

    suffix = []
    if boundary_path:
        suffix.append('clip')
    if target_epsg:
        suffix.append(f'epsg{target_epsg}')
    output_path = os.path.join(output_dir, f'{base}_{"_".join(suffix)}.tif')

    profile.update(compress='lzw')
    with rasterio.open(output_path, 'w', **profile) as destination:
        destination.write(array)

    message = (
        f'Raster {" and ".join(steps)}. '
        f'Output is {profile["width"]}x{profile["height"]} px, '
        f'{profile["count"]} band(s).'
    )
    return True, message, {'raster_tif': output_path}
