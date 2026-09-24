"""
Celery tasks for the four native GIS tools.

These are thin: fetch the inputs, check the caller may read them, decide where
output goes, call the processing module, register what it wrote. The real work
lives in zonal_statistics, vegetation_index, management_zones and
raster_clip_reproject, which take and return plain paths so they can be tested
without Celery or a database.
"""
import logging

from celery import shared_task

from .models import File
from .tool_io import register_outputs, resolve_output_folder

# The processing modules are imported inside each task rather than here.
# They pull in matplotlib, rasterio and geopandas, and a Celery task module is
# imported by every process that touches filemanager.tasks -- including the web
# container. When one of those packages was missing from the worker image, a
# module-level import here took the whole worker down in a crash loop at
# startup instead of failing the one task that needed it.

logger = logging.getLogger(__name__)


class InputError(Exception):
    """An input is missing, or the caller may not read it."""


def _fetch(file_id, label, requesting_user_id=None):
    """
    Return the File for ``file_id``, refusing one the caller cannot read.

    The view layer checks this too. Repeating it here closes the gap where a
    task id is replayed directly, and costs one query.
    """
    try:
        file_obj = File.objects.get(id=file_id)
    except (File.DoesNotExist, ValueError, TypeError):
        raise InputError(f'{label} not found.')

    if (
        requesting_user_id is not None
        and file_obj.owner_id != requesting_user_id
        and not file_obj.is_public
    ):
        logger.warning(
            'BOLA attempt: user %s requested file %s owned by %s',
            requesting_user_id, file_id, file_obj.owner_id,
        )
        raise InputError('Permission denied.')

    return file_obj


def _finish(source_file, output_folder_id, default_folder_name, processor):
    """
    Run ``processor(output_dir)`` and register whatever it wrote.

    ``processor`` returns the usual ``(success, message, output_files)``.
    """
    folder, directory = resolve_output_folder(
        source_file, output_folder_id, default_name=default_folder_name
    )

    success, message, output_files = processor(directory)
    if not success:
        return {'success': False, 'error': message}

    created = register_outputs(
        output_files, folder, source_file.owner, is_public=source_file.is_public
    )

    return {
        'success': True,
        'message': message,
        'created_files': created,
        'output_folder_id': str(folder.id),
    }


def _guard(task_name, file_id):
    """Turn the two expected failure modes into a result dict, log the rest."""
    def wrap(fn):
        try:
            return fn()
        except InputError as exc:
            return {'success': False, 'error': str(exc)}
        except ValueError as exc:
            return {'success': False, 'error': str(exc)}
        except Exception:
            logger.exception('%s failed for %s', task_name, file_id)
            return {
                'success': False,
                'error': f'{task_name} failed (see server logs for {file_id}).',
            }
    return wrap


@shared_task(bind=True)
def run_zonal_statistics_task(
    self, vector_file_id, raster_file_id, output_folder_id=None,
    band=1, statistics=None, prefix='val', requesting_user_id=None,
):
    def body():
        from .zonal_statistics import compute_zonal_statistics
        zones = _fetch(vector_file_id, 'Zone layer', requesting_user_id)
        raster = _fetch(raster_file_id, 'Raster', requesting_user_id)
        return _finish(
            zones, output_folder_id, 'zonal_stats_output',
            lambda out: compute_zonal_statistics(
                zones.file.path, raster.file.path, out,
                band=int(band), statistics=statistics, prefix=prefix,
            ),
        )

    return _guard('Zonal statistics', vector_file_id)(body)


@shared_task(bind=True)
def run_vegetation_index_task(
    self, index_key, band_files, output_folder_id=None,
    soil_factor=0.5, requesting_user_id=None,
):
    """
    ``band_files`` maps a band name to ``[file_id, band_number]``. Several
    names may point at one file with different band numbers, which is how a
    multi-band image supplies everything at once.
    """
    def body():
        from .vegetation_index import compute_vegetation_index
        sources = {}
        first = None
        for band_name, spec in (band_files or {}).items():
            file_id, band_number = spec
            file_obj = _fetch(file_id, f'{band_name} band', requesting_user_id)
            first = first or file_obj
            sources[band_name] = (file_obj.file.path, int(band_number))

        if first is None:
            raise InputError('No bands were supplied.')

        return _finish(
            first, output_folder_id, 'vegetation_index_output',
            lambda out: compute_vegetation_index(
                index_key, sources, out, soil_factor=float(soil_factor)
            ),
        )

    return _guard('Vegetation index', index_key)(body)


@shared_task(bind=True)
def run_management_zones_task(
    self, file_id, columns, zone_count=3, output_folder_id=None,
    requesting_user_id=None,
):
    def body():
        from .management_zones import delineate_management_zones
        source = _fetch(file_id, 'Input layer', requesting_user_id)
        return _finish(
            source, output_folder_id, 'management_zones_output',
            lambda out: delineate_management_zones(
                source.file.path, out, list(columns or []),
                zone_count=int(zone_count),
            ),
        )

    return _guard('Management zones', file_id)(body)


@shared_task(bind=True)
def run_raster_clip_reproject_task(
    self, raster_file_id, boundary_file_id=None, target_epsg=None,
    resampling='bilinear', output_folder_id=None, requesting_user_id=None,
):
    def body():
        from .raster_clip_reproject import clip_and_reproject
        raster = _fetch(raster_file_id, 'Raster', requesting_user_id)
        boundary_path = None
        if boundary_file_id:
            boundary = _fetch(boundary_file_id, 'Boundary layer', requesting_user_id)
            boundary_path = boundary.file.path

        return _finish(
            raster, output_folder_id, 'clip_reproject_output',
            lambda out: clip_and_reproject(
                raster.file.path, out,
                boundary_path=boundary_path,
                target_epsg=target_epsg,
                resampling=resampling,
            ),
        )

    return _guard('Clip/reproject', raster_file_id)(body)


def _area_of_interest(boundary_file, lat=None, lon=None, bbox=None):
    """
    Work out the area a public dataset should be fetched for.

    A boundary layer already in ADMA is the good path: its bounds become the
    box and its centre the point, so a field drawn once serves both the raster
    datasets and the point ones. Failing that, a box or a point can be typed in.
    """
    if boundary_file is not None:
        import geopandas as gpd

        frame = gpd.read_file(boundary_file.file.path)
        if frame.empty:
            raise InputError('The boundary layer has no features.')
        if frame.crs is None:
            raise InputError(
                'The boundary layer has no CRS recorded, so its position is '
                'unknown. Assign one, or type coordinates instead.'
            )
        bounds = frame.to_crs('EPSG:4326').total_bounds  # minx, miny, maxx, maxy
        minx, miny, maxx, maxy = (float(v) for v in bounds)
        # A single point or a perfectly thin strip has no area to clip, so give
        # it a small margin -- about 500 m -- rather than fail.
        if maxx - minx < 1e-6:
            minx, maxx = minx - 0.005, maxx + 0.005
        if maxy - miny < 1e-6:
            miny, maxy = miny - 0.005, maxy + 0.005
        return {
            'lat': (miny + maxy) / 2.0,
            'lon': (minx + maxx) / 2.0,
            'bbox': (minx, miny, maxx, maxy),
        }

    if bbox:
        minx, miny, maxx, maxy = (float(v) for v in bbox)
        return {'lat': (miny + maxy) / 2.0, 'lon': (minx + maxx) / 2.0,
                'bbox': (minx, miny, maxx, maxy)}

    if lat is None or lon is None:
        raise InputError('Choose a boundary layer, or give a point or a box.')

    lat, lon = float(lat), float(lon)
    # Roughly a 1 km box around the point, so the raster datasets have
    # something to clip even when only a point was given.
    margin = 0.005
    return {'lat': lat, 'lon': lon,
            'bbox': (lon - margin, lat - margin, lon + margin, lat + margin)}


def _public_data_folder(user, dataset, output_folder_id):
    """
    Where fetched public data lands: one top-level folder per source.

    Top-level matters. The Third-Party Data panel lists third-party folders
    with ``parent=None`` and third-party files with no folder at all, so
    results tucked into a subfolder beside the field they were fetched for
    would never appear there -- which is the whole point of fetching them.
    One folder per source also gives the panel the same shape it already has
    for John Deere and Realm5.
    """
    from .models import Folder

    if output_folder_id:
        try:
            return Folder.objects.get(id=output_folder_id)
        except Folder.DoesNotExist:
            raise InputError('Output folder not found.')

    folder, created = Folder.objects.get_or_create(
        name=dataset.name, parent=None, owner=user,
        defaults={
            'is_public': False,
            'is_third_party': True,
            'third_party_source': dataset.source,
        },
    )
    if not created and not folder.is_third_party:
        # An older run, or a folder the user made by hand under the same name.
        folder.is_third_party = True
        folder.third_party_source = dataset.source
        folder.save(update_fields=['is_third_party', 'third_party_source'])
    return folder


@shared_task(bind=True)
def run_public_data_fetch_task(
    self, dataset_key, boundary_file_id=None, lat=None, lon=None, bbox=None,
    params=None, output_folder_id=None, requesting_user_id=None,
):
    """Fetch one public dataset for an area and register it as the user's files."""
    def body():
        import os

        from django.conf import settings
        from django.contrib.auth import get_user_model

        from .public_datasets import PUBLIC_DATASETS, DatasetError
        from .tool_io import register_outputs

        dataset = PUBLIC_DATASETS.get(dataset_key)
        if dataset is None:
            raise InputError(f'Unknown dataset {dataset_key!r}.')

        boundary = (
            _fetch(boundary_file_id, 'Boundary layer', requesting_user_id)
            if boundary_file_id else None
        )

        owner = boundary.owner if boundary is not None else (
            get_user_model().objects.get(id=requesting_user_id)
        )

        aoi = _area_of_interest(boundary, lat=lat, lon=lon, bbox=bbox)
        folder = _public_data_folder(owner, dataset, output_folder_id)
        directory = os.path.join(
            settings.MEDIA_ROOT, 'uploads', folder.get_full_path()
        )
        os.makedirs(directory, exist_ok=True)

        try:
            success, message, output_files = dataset.fetch(
                aoi, directory, **(params or {})
            )
        except DatasetError as exc:
            return {'success': False, 'error': str(exc)}

        if not success:
            return {'success': False, 'error': message}

        created = register_outputs(
            output_files, folder, owner,
            is_public=False,
            # This is what puts the results in the Third-Party Data panel.
            extra_fields={
                'is_third_party': True,
                'third_party_source': dataset.source,
            },
        )

        return {
            'success': True,
            'message': message,
            'created_files': created,
            'output_folder_id': str(folder.id),
        }

    return _guard('Public data fetch', dataset_key)(body)
