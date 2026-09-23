"""
Delineate management zones by clustering field measurements.

Yield monitors and imagery produce a dense point cloud; agronomists act on a
handful of zones. This clusters the points on one or more attributes and
labels each with the zone it belongs to, which is the usual first step toward
a variable-rate prescription.

k-means comes from scipy.cluster.vq rather than scikit-learn, which is not
installed. scipy's kmeans2 is the same algorithm; what it does not give us is
scikit-learn's feature scaling or its k-means++ seeding, so both are done here
-- see _standardise and the 'points' minit below.
"""
import logging
import os

import matplotlib
matplotlib.use('Agg')  # No display on the worker.

import matplotlib.pyplot as plt
import numpy as np
import geopandas as gpd
from scipy.cluster.vq import kmeans2

logger = logging.getLogger(__name__)

MAX_ZONES = 12
MIN_ZONES = 2


def _standardise(matrix):
    """
    Centre and scale each attribute to unit variance.

    k-means measures plain Euclidean distance, so without this an attribute
    that happens to be recorded in larger numbers -- yield in kg/ha beside an
    index between 0 and 1 -- would decide the clustering on its own.
    A constant column has zero spread; it carries no information, so it is
    divided by 1 and contributes nothing rather than producing NaN.
    """
    mean = matrix.mean(axis=0)
    spread = matrix.std(axis=0)
    spread = np.where(spread == 0, 1.0, spread)
    return (matrix - mean) / spread


def _zone_ranking(frame, columns, labels, zone_count):
    """
    Renumber zones from worst to best on the first attribute.

    kmeans2 labels clusters in whatever order it seeded them, so the same data
    can come back as zone 1 or zone 3 between runs. Ordering by mean makes the
    numbering mean something -- zone 1 is the low end -- and stable to re-run.
    """
    means = {}
    for zone in range(zone_count):
        member = labels == zone
        means[zone] = frame.loc[member, columns[0]].mean() if member.any() else np.inf

    order = sorted(means, key=lambda z: (np.isinf(means[z]), means[z]))
    return {old: new for new, old in enumerate(order, start=1)}


def _write_zone_map(frame, zone_column, png_path, title):
    figure, axes = plt.subplots(figsize=(9, 9))
    frame.plot(
        column=zone_column, ax=axes, categorical=True, legend=True,
        markersize=6, cmap='viridis',
        legend_kwds={'title': 'Zone', 'loc': 'lower right'},
    )
    axes.set_title(title)
    axes.set_axis_off()
    figure.tight_layout()
    figure.savefig(png_path, dpi=150, bbox_inches='tight')
    plt.close(figure)
    return png_path


def delineate_management_zones(
    input_path,
    output_dir,
    columns,
    zone_count=3,
    random_seed=42,
    output_basename=None,
):
    """
    Cluster ``input_path`` on ``columns`` into ``zone_count`` zones.

    Returns ``(success, message, output_files)``: the points carrying a `zone`
    attribute, a per-zone summary CSV, and a map.
    """
    if not os.path.exists(input_path):
        return False, f'Input file not found: {os.path.basename(input_path)}', {}

    if not columns:
        return False, 'Choose at least one column to cluster on.', {}

    if not MIN_ZONES <= zone_count <= MAX_ZONES:
        return False, f'Zone count must be between {MIN_ZONES} and {MAX_ZONES}.', {}

    try:
        frame = gpd.read_file(input_path)
    except Exception as exc:
        return False, f'Could not read the input file: {exc}', {}

    if frame.empty:
        return False, 'The input file contains no features.', {}

    missing = [c for c in columns if c not in frame.columns]
    if missing:
        available = ', '.join(c for c in frame.columns if c != frame.geometry.name)
        return False, (
            f'Column(s) not found: {", ".join(missing)}. '
            f'Available: {available}'
        ), {}

    numeric = frame[columns].apply(gpd.pd.to_numeric, errors='coerce')
    usable = numeric.notna().all(axis=1) & frame.geometry.notna()
    dropped = int((~usable).sum())

    if usable.sum() < zone_count:
        return False, (
            f'Only {int(usable.sum())} row(s) have a value in every chosen '
            f'column, which is fewer than the {zone_count} zones requested.'
        ), {}

    working = frame.loc[usable].reset_index(drop=True)
    matrix = _standardise(numeric.loc[usable].to_numpy(dtype='float64'))

    # 'points' seeds from actual observations; the default seeding can place a
    # centroid where there is no data and hand back an empty cluster.
    # missing='warn' keeps kmeans2 from raising if one empties anyway.
    centroids, labels = kmeans2(
        matrix, zone_count, minit='points', seed=random_seed, missing='warn',
    )

    found = len(np.unique(labels))
    renumber = _zone_ranking(working, columns, labels, zone_count)
    working['zone'] = [renumber[int(z)] for z in labels]

    os.makedirs(output_dir, exist_ok=True)
    base = output_basename or (
        f'{os.path.splitext(os.path.basename(input_path))[0]}_zones{zone_count}'
    )

    summary = (
        working.drop(columns=working.geometry.name)
        .groupby('zone')[columns]
        .agg(['count', 'mean', 'std', 'min', 'max'])
    )
    summary.columns = ['_'.join(c) for c in summary.columns]
    summary_path = os.path.join(output_dir, f'{base}_summary.csv')
    summary.reset_index().to_csv(summary_path, index=False)

    shp_path = os.path.join(output_dir, f'{base}.shp')
    working.to_file(shp_path)

    map_path = _write_zone_map(
        working, 'zone', os.path.join(output_dir, f'{base}_map.png'),
        f'{zone_count} management zones by {", ".join(columns)}',
    )

    output_files = {
        'zones_shp': [
            os.path.join(output_dir, f'{base}{ext}')
            for ext in ('.shp', '.shx', '.dbf', '.prj', '.cpg')
        ],
        'summary_csv': summary_path,
        'map_png': map_path,
    }

    message = f'Clustered {len(working)} point(s) into {found} zone(s) on {", ".join(columns)}.'
    if found < zone_count:
        message += (
            f' {zone_count} were requested, but the data did not separate into '
            'that many.'
        )
    if dropped:
        message += f' {dropped} row(s) were skipped for missing values or geometry.'

    return True, message, output_files


def numeric_columns(path):
    """The numeric attribute columns of a vector file, for populating the UI."""
    frame = gpd.read_file(path)
    geometry_name = frame.geometry.name if frame.geometry is not None else None
    found = []
    for column in frame.columns:
        if column == geometry_name:
            continue
        converted = gpd.pd.to_numeric(frame[column], errors='coerce')
        if converted.notna().any():
            found.append(column)
    return found
