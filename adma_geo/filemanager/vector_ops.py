"""
Buffer, clip and dissolve a vector layer.

The everyday edits that stand between a layer as delivered and a layer you can
analyse: pull a headland back from the field edge, cut a county-wide layer down
to one farm, merge strips into treatments.

Buffering is measured in metres, so a layer in degrees is reprojected to its
local UTM zone first and returned in the CRS it arrived in. A buffer taken in
degrees would vary with latitude and mean nothing.
"""
import logging
import os

import geopandas as gpd

logger = logging.getLogger(__name__)

OPERATIONS = {
    'buffer': 'Grow or shrink each feature by a fixed distance in metres.',
    'clip': 'Keep only the parts that fall inside another layer.',
    'dissolve': 'Merge features, optionally grouping by a column.',
}


def _utm_epsg(longitude, latitude):
    zone = int((longitude + 180.0) / 6.0) + 1
    return (32600 if latitude >= 0 else 32700) + zone


def _metric_crs(frame):
    """A projected CRS for ``frame``: its own if it has one, else local UTM."""
    from pyproj import CRS

    if frame.crs.is_projected:
        return frame.crs
    west, south, east, north = frame.to_crs('EPSG:4326').total_bounds
    return CRS.from_epsg(_utm_epsg((west + east) / 2.0, (south + north) / 2.0))


def apply_operation(input_path, output_dir, operation='buffer', distance=0.0,
                    clip_path=None, dissolve_by=None, output_basename=None):
    """
    Run one vector operation. Returns ``(success, message, output_files)``.
    """
    if operation not in OPERATIONS:
        return False, (
            f'Unknown operation. Available: {", ".join(sorted(OPERATIONS))}.'
        ), {}

    if not os.path.exists(input_path):
        return False, f'Input layer not found: {os.path.basename(input_path)}', {}

    try:
        frame = gpd.read_file(input_path)
    except Exception as exc:
        return False, f'Could not read the input layer: {exc}', {}

    if frame.empty:
        return False, 'The input layer contains no features.', {}
    if frame.crs is None:
        return False, 'The input layer has no CRS, so it cannot be measured or clipped.', {}

    original_crs = frame.crs
    detail = ''

    try:
        if operation == 'buffer':
            distance = float(distance)
            if distance == 0:
                return False, 'A buffer of zero metres would change nothing.', {}

            working_crs = _metric_crs(frame)
            working = frame.to_crs(working_crs)
            working['geometry'] = working.geometry.buffer(distance)
            working = working[~working.geometry.is_empty & working.geometry.notna()]

            if working.empty:
                return False, (
                    f'A {distance} m buffer removed every feature. A negative '
                    'buffer larger than the feature leaves nothing behind.'
                ), {}

            result = working.to_crs(original_crs)
            detail = f'buffered by {distance:g} m'

        elif operation == 'clip':
            if not clip_path or not os.path.exists(clip_path):
                return False, 'Choose a layer to clip to.', {}

            mask = gpd.read_file(clip_path)
            if mask.empty:
                return False, 'The clip layer contains no features.', {}
            if mask.crs is not None and mask.crs != frame.crs:
                mask = mask.to_crs(frame.crs)

            result = gpd.clip(frame, mask)
            if result.empty:
                return False, (
                    'Nothing survived the clip -- the two layers may not overlap.'
                ), {}
            detail = f'clipped from {len(frame)} to {len(result)} feature(s)'

        else:  # dissolve
            if dissolve_by:
                if dissolve_by not in frame.columns:
                    available = ', '.join(
                        c for c in frame.columns if c != frame.geometry.name
                    )
                    return False, (
                        f'Column {dissolve_by!r} not found. Available: {available}'
                    ), {}
                result = frame.dissolve(by=dissolve_by, as_index=False)
                detail = f'dissolved into {len(result)} group(s) by {dissolve_by}'
            else:
                result = frame.dissolve()
                detail = 'dissolved into a single feature'

    except Exception as exc:
        logger.exception('Vector %s failed for %s', operation, input_path)
        return False, f'The {operation} failed: {exc}', {}

    os.makedirs(output_dir, exist_ok=True)
    base = output_basename or (
        f'{os.path.splitext(os.path.basename(input_path))[0]}_{operation}'
    )
    shp_path = os.path.join(output_dir, f'{base}.shp')
    result.to_file(shp_path)

    output_files = {
        'result_shp': [
            os.path.join(output_dir, f'{base}{ext}')
            for ext in ('.shp', '.shx', '.dbf', '.prj', '.cpg')
        ]
    }
    return True, f'Layer {detail}.', output_files
