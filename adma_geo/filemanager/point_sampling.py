"""
Read raster values at point locations.

Zonal Statistics answers "what is the average over this field". This answers
the other half: "what was the value right here", for every yield monitor ping
or soil sample. Joining yield points to a crop layer, an elevation model and a
soil property is how a field's numbers become a table you can regress.

Several rasters can be sampled in one pass, so the output is one row per point
carrying every layer, ready to analyse.
"""
import logging
import os

import geopandas as gpd
import numpy as np
import rasterio

from .dbf_names import unique_dbf_name

logger = logging.getLogger(__name__)

def _column_name(label, taken):
    return unique_dbf_name(label, taken)


def sample_rasters_at_points(points_path, raster_specs, output_dir,
                             output_basename=None):
    """
    Sample each raster in ``raster_specs`` at every point in ``points_path``.

    ``raster_specs`` is a list of ``{'path': ..., 'band': 1, 'label': 'cdl'}``.

    Returns ``(success, message, output_files)``.
    """
    if not os.path.exists(points_path):
        return False, f'Point layer not found: {os.path.basename(points_path)}', {}
    if not raster_specs:
        return False, 'Choose at least one raster to sample.', {}

    try:
        points = gpd.read_file(points_path)
    except Exception as exc:
        return False, f'Could not read the point layer: {exc}', {}

    if points.empty:
        return False, 'The point layer contains no features.', {}
    if points.crs is None:
        return False, 'The point layer has no CRS, so it cannot be located on a raster.', {}

    # Polygons and lines are sampled at their representative point rather than
    # refused: it is a reasonable reading of "the value here", and saying so in
    # the message is better than failing on a mixed layer.
    geometry_types = set(points.geom_type.dropna().unique())
    non_point = geometry_types - {'Point', 'MultiPoint'}

    sample_geometries = (
        points.geometry if not non_point else points.geometry.representative_point()
    )

    taken = set()
    columns = {}
    summaries = []

    for spec in raster_specs:
        path = spec['path']
        band = int(spec.get('band') or 1)
        label = spec.get('label') or os.path.splitext(os.path.basename(path))[0]

        if not os.path.exists(path):
            return False, f'Raster not found: {os.path.basename(path)}', {}

        try:
            with rasterio.open(path) as raster:
                if band < 1 or band > raster.count:
                    return False, (
                        f'Band {band} is out of range for {os.path.basename(path)}, '
                        f'which has {raster.count}.'
                    ), {}

                # The points almost never share the raster's CRS. Moving the
                # points is exact; resampling the raster would not be.
                located = (
                    sample_geometries if raster.crs is None or points.crs == raster.crs
                    else gpd.GeoSeries(sample_geometries, crs=points.crs).to_crs(raster.crs)
                )
                coordinates = [(geom.x, geom.y) for geom in located]

                nodata = raster.nodata
                values = []
                for value in raster.sample(coordinates, indexes=band):
                    v = float(value[0])
                    if nodata is not None and v == nodata:
                        v = np.nan
                    values.append(v)
        except Exception as exc:
            logger.exception('Sampling %s failed', path)
            return False, f'Could not sample {os.path.basename(path)}: {exc}', {}

        array = np.array(values, dtype='float64')
        column = _column_name(label, taken)
        columns[column] = array
        hit = int(np.isfinite(array).sum())
        summaries.append(f'{label} {hit}/{len(array)}')

    enriched = points.copy()
    for column, array in columns.items():
        enriched[column] = array

    os.makedirs(output_dir, exist_ok=True)
    base = output_basename or (
        f'{os.path.splitext(os.path.basename(points_path))[0]}_sampled'
    )

    csv_path = os.path.join(output_dir, f'{base}.csv')
    table = enriched.drop(columns=enriched.geometry.name).copy()
    table['x'] = [geom.x for geom in sample_geometries]
    table['y'] = [geom.y for geom in sample_geometries]
    table.to_csv(csv_path, index=False)

    shp_path = os.path.join(output_dir, f'{base}.shp')
    enriched.to_file(shp_path)

    output_files = {
        'samples_csv': csv_path,
        'samples_shp': [
            os.path.join(output_dir, f'{base}{ext}')
            for ext in ('.shp', '.shx', '.dbf', '.prj', '.cpg')
        ],
    }

    message = (
        f'Sampled {len(points)} location(s): {"; ".join(summaries)} '
        f'(points with a value / total).'
    )
    if non_point:
        message += (
            f' The layer holds {", ".join(sorted(non_point))}, so each was '
            'sampled at its representative point.'
        )

    return True, message, output_files
