"""
Summarise a raster within each polygon of a vector layer.

Given field boundaries and, say, an NDRE raster, this answers "what is the
mean NDRE in each field" -- the operation behind most of the imagery work on
this platform, and the one people otherwise leave for desktop GIS.

Implemented directly on rasterio.mask rather than through rasterstats, which
is not installed. The masking call does the real work; what is worth care here
is everything around it: the polygons almost never share the raster's CRS, and
the raster's nodata value must be excluded or it drags every mean toward
whatever sentinel the sensor wrote.
"""
import logging
import os

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.mask import mask as rio_mask

logger = logging.getLogger(__name__)

# Keep this in step with STAT_FUNCTIONS below.
DEFAULT_STATISTICS = ['count', 'mean', 'min', 'max', 'std']

STAT_FUNCTIONS = {
    'count': lambda a: int(a.size),
    'mean': lambda a: float(np.mean(a)),
    'min': lambda a: float(np.min(a)),
    'max': lambda a: float(np.max(a)),
    'std': lambda a: float(np.std(a)),
    'sum': lambda a: float(np.sum(a)),
    'median': lambda a: float(np.median(a)),
    'range': lambda a: float(np.max(a) - np.min(a)),
}

# .dbf caps field names at 10 characters and silently truncates past that,
# which turns 'ndre_median' and 'ndre_mean' into the same column. Names are
# built short enough to survive the write.
DBF_FIELD_LIMIT = 10


def _column_name(prefix, stat, taken):
    """A per-statistic column name that fits in a .dbf field and stays unique."""
    base = f'{prefix}_{stat}'[:DBF_FIELD_LIMIT]
    if base not in taken:
        taken.add(base)
        return base
    for n in range(1, 100):
        suffix = str(n)
        candidate = f'{base[:DBF_FIELD_LIMIT - len(suffix)]}{suffix}'
        if candidate not in taken:
            taken.add(candidate)
            return candidate
    raise ValueError(f'Cannot build a unique column name for {prefix}_{stat}')


def compute_zonal_statistics(
    vector_path,
    raster_path,
    output_dir,
    band=1,
    statistics=None,
    prefix='val',
    output_basename=None,
):
    """
    Compute per-polygon statistics of ``raster_path`` and write them out.

    Returns ``(success, message, output_files)``. The outputs are a CSV of the
    statistics and a copy of the polygons carrying them as attributes.
    """
    statistics = list(statistics or DEFAULT_STATISTICS)
    unknown = [s for s in statistics if s not in STAT_FUNCTIONS]
    if unknown:
        return False, f'Unknown statistic(s): {", ".join(unknown)}', {}

    if not os.path.exists(vector_path):
        return False, f'Vector file not found: {os.path.basename(vector_path)}', {}
    if not os.path.exists(raster_path):
        return False, f'Raster file not found: {os.path.basename(raster_path)}', {}

    try:
        zones = gpd.read_file(vector_path)
    except Exception as exc:
        return False, f'Could not read the vector file: {exc}', {}

    if zones.empty:
        return False, 'The vector file contains no features.', {}

    geom_types = set(zones.geom_type.dropna().unique())
    if not geom_types <= {'Polygon', 'MultiPolygon'}:
        return False, (
            'Zonal statistics needs polygons; this layer contains '
            f'{", ".join(sorted(geom_types))}.'
        ), {}

    try:
        raster = rasterio.open(raster_path)
    except Exception as exc:
        return False, f'Could not read the raster: {exc}', {}

    with raster:
        if band < 1 or band > raster.count:
            return False, f'Band {band} is out of range; the raster has {raster.count}.', {}

        # The polygons are almost never in the raster's CRS. Reproject the
        # polygons rather than the raster: it is the cheaper side, and
        # resampling the raster would change the very values being summarised.
        if zones.crs is None:
            logger.warning('Vector layer has no CRS; assuming it matches the raster')
        elif raster.crs is not None and zones.crs != raster.crs:
            logger.info('Reprojecting zones from %s to %s', zones.crs, raster.crs)
            zones = zones.to_crs(raster.crs)

        nodata = raster.nodata
        rows = []
        empty_zones = 0

        for position, (index, feature) in enumerate(zones.iterrows()):
            record = {'zone_index': int(position)}
            geometry = feature.geometry

            if geometry is None or geometry.is_empty:
                values = np.array([], dtype='float64')
            else:
                try:
                    clipped, _ = rio_mask(
                        raster, [geometry], crop=True, filled=True,
                        nodata=nodata, indexes=[band],
                    )
                    values = clipped[0].astype('float64').ravel()
                    # Drop the sentinel and any NaN the sensor left behind;
                    # averaging them in is the classic way these numbers go
                    # quietly wrong.
                    if nodata is not None:
                        values = values[values != nodata]
                    values = values[~np.isnan(values)]
                except ValueError:
                    # rasterio raises this when a polygon misses the raster
                    # entirely, which is ordinary at a field's edge.
                    values = np.array([], dtype='float64')

            taken = set()
            if values.size == 0:
                empty_zones += 1
                for stat in statistics:
                    record[_column_name(prefix, stat, taken)] = 0 if stat == 'count' else None
            else:
                for stat in statistics:
                    record[_column_name(prefix, stat, taken)] = STAT_FUNCTIONS[stat](values)

            rows.append(record)

    stats_frame = gpd.pd.DataFrame(rows)

    base = output_basename or f'{os.path.splitext(os.path.basename(vector_path))[0]}_zonalstats'
    os.makedirs(output_dir, exist_ok=True)

    csv_path = os.path.join(output_dir, f'{base}.csv')
    # The attribute table is more useful alongside the identifying columns the
    # zones already carry, so join them back on rather than emitting bare stats.
    attributes = zones.drop(columns=zones.geometry.name).reset_index(drop=True)
    combined = gpd.pd.concat([attributes, stats_frame], axis=1)
    combined.to_csv(csv_path, index=False)

    shp_path = os.path.join(output_dir, f'{base}.shp')
    enriched = zones.reset_index(drop=True).join(stats_frame)
    enriched.to_file(shp_path)

    output_files = {
        'statistics_csv': csv_path,
        'zones_shp': [
            os.path.join(output_dir, f'{base}{ext}')
            for ext in ('.shp', '.shx', '.dbf', '.prj', '.cpg')
        ],
    }

    message = f'Summarised band {band} across {len(zones)} zone(s).'
    if empty_zones:
        message += f' {empty_zones} zone(s) had no raster coverage.'

    return True, message, output_files
