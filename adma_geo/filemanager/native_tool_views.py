"""
Pages and endpoints for the four native GIS tools.

Kept out of views.py, which is already long. Two things are shared here that
the older tools each re-implement: one tree builder, and one status endpoint.
Polling a Celery result does not depend on which task produced it, so the four
tools poll the same URL rather than carrying four identical copies.
"""
import json
import logging
import os

from celery.result import AsyncResult
from django.db.models import Q
from django.http import JsonResponse
from django.views.generic import TemplateView
from django.contrib.auth.mixins import LoginRequiredMixin

from .models import File, Folder
from .views import _internal_error_response, api_login_required

logger = logging.getLogger(__name__)

VECTOR_EXTENSIONS = ['.shp', '.gpkg', '.geojson']
RASTER_EXTENSIONS = ['.tif', '.tiff']


def _extension_filter(extensions):
    query = Q()
    for extension in extensions:
        query |= Q(name__iendswith=extension)
    return query


def build_tool_tree(user, file_extensions):
    """
    The folder/file tree the shared picker consumes.

    One payload carries every type a tool accepts; the picker narrows it
    client-side from its accept list, so a page with four inputs of different
    types still needs only this one tree.
    """
    matching = _extension_filter(file_extensions)

    def folder_to_dict(folder):
        return {
            'id': str(folder.id),
            'name': folder.name,
            'file_count': folder.files.filter(deletion_in_progress=False).count(),
            'subfolders': [
                folder_to_dict(sub)
                for sub in Folder.objects.filter(
                    parent=folder, deletion_in_progress=False
                ).order_by('name')
            ],
            'files': [
                {'id': str(f.id), 'name': f.name, 'size_display': f.get_size_display()}
                for f in File.objects.filter(
                    matching, folder=folder, deletion_in_progress=False
                ).order_by('name')
            ],
        }

    def file_dicts(queryset):
        return [
            {'id': str(f.id), 'name': f.name, 'size_display': f.get_size_display()}
            for f in queryset
        ]

    return {
        'my_folders': [
            folder_to_dict(f) for f in Folder.objects.filter(
                owner=user, parent__isnull=True, deletion_in_progress=False
            ).order_by('name')
        ],
        'my_root_files': file_dicts(File.objects.filter(
            matching, owner=user, folder__isnull=True, deletion_in_progress=False
        ).order_by('name')),
        'public_folders': [
            folder_to_dict(f) for f in Folder.objects.filter(
                is_public=True, parent__isnull=True, deletion_in_progress=False
            ).exclude(owner=user).order_by('name')
        ],
        'public_root_files': file_dicts(File.objects.filter(
            matching, is_public=True, folder__isnull=True, deletion_in_progress=False
        ).exclude(owner=user).order_by('name')),
    }


class _NativeToolView(LoginRequiredMixin, TemplateView):
    """A tool page: one tree of everything the tool accepts, plus its title."""

    accepted_extensions = []
    page_title = ''

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['tree_data'] = build_tool_tree(self.request.user, self.accepted_extensions)
        context['page_title'] = self.page_title
        return context


class ZonalStatisticsToolView(_NativeToolView):
    template_name = 'filemanager/zonal_statistics_tool.html'
    accepted_extensions = VECTOR_EXTENSIONS + RASTER_EXTENSIONS
    page_title = 'Zonal Statistics'


class VegetationIndexToolView(_NativeToolView):
    template_name = 'filemanager/vegetation_index_tool.html'
    accepted_extensions = RASTER_EXTENSIONS
    page_title = 'Vegetation Index'

    def get_context_data(self, **kwargs):
        from .vegetation_index import INDEX_DEFINITIONS
        context = super().get_context_data(**kwargs)
        context['indices'] = [
            {
                'key': d.key, 'name': d.name,
                'bands': d.bands, 'description': d.description,
            }
            for d in INDEX_DEFINITIONS.values()
        ]
        return context


class ManagementZonesToolView(_NativeToolView):
    template_name = 'filemanager/management_zones_tool.html'
    accepted_extensions = VECTOR_EXTENSIONS
    page_title = 'Management Zones'

    def get_context_data(self, **kwargs):
        from .management_zones import MAX_ZONES, MIN_ZONES
        context = super().get_context_data(**kwargs)
        context['zone_choices'] = list(range(MIN_ZONES, MAX_ZONES + 1))
        return context


class RasterClipToolView(_NativeToolView):
    template_name = 'filemanager/raster_clip_tool.html'
    accepted_extensions = VECTOR_EXTENSIONS + RASTER_EXTENSIONS
    page_title = 'Clip & Reproject Raster'

    def get_context_data(self, **kwargs):
        from .raster_clip_reproject import DEFAULT_RESAMPLING, RESAMPLING_METHODS
        context = super().get_context_data(**kwargs)
        context['resampling_methods'] = sorted(RESAMPLING_METHODS)
        context['default_resampling'] = DEFAULT_RESAMPLING
        return context


# --- endpoints -------------------------------------------------------------

def _payload(request):
    try:
        return json.loads(request.body or '{}')
    except json.JSONDecodeError:
        raise ValueError('Invalid JSON')


def _readable_file(file_id, user, label, extensions=None):
    """Fetch a File the user may read, checking its extension."""
    try:
        file_obj = File.objects.get(id=file_id)
    except (File.DoesNotExist, ValueError, TypeError):
        raise ValueError(f'{label} not found.')

    if file_obj.owner != user and not file_obj.is_public:
        raise PermissionError(f'You do not have access to the {label.lower()}.')

    if extensions:
        extension = os.path.splitext(file_obj.name)[1].lower()
        if extension not in extensions:
            raise ValueError(
                f'{label} must be one of {", ".join(extensions)}; got {extension or "no extension"}.'
            )

    return file_obj


def _writable_folder(folder_id, user):
    if not folder_id:
        return None
    try:
        return Folder.objects.get(id=folder_id, owner=user)
    except (Folder.DoesNotExist, ValueError, TypeError):
        raise ValueError('Output folder not found.')


def _dispatch(request, build_task):
    """Shared request handling: parse, validate, queue, answer."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed'}, status=405)
    try:
        data = _payload(request)
        _writable_folder(data.get('output_folder_id'), request.user)
        task = build_task(data)
    except PermissionError as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=403)
    except ValueError as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=400)
    except Exception as exc:
        return _internal_error_response(exc)

    return JsonResponse({'success': True, 'task_id': task.id})


@api_login_required
def run_zonal_statistics(request):
    def build(data):
        from .native_tool_tasks import run_zonal_statistics_task
        zones = _readable_file(
            data.get('vector_file_id'), request.user, 'Zone layer', VECTOR_EXTENSIONS
        )
        raster = _readable_file(
            data.get('raster_file_id'), request.user, 'Raster', RASTER_EXTENSIONS
        )
        return run_zonal_statistics_task.delay(
            str(zones.id), str(raster.id),
            output_folder_id=data.get('output_folder_id'),
            band=int(data.get('band') or 1),
            statistics=data.get('statistics') or None,
            prefix=(data.get('prefix') or 'val')[:6],
            requesting_user_id=request.user.id,
        )

    return _dispatch(request, build)


@api_login_required
def run_vegetation_index(request):
    def build(data):
        from .native_tool_tasks import run_vegetation_index_task
        from .vegetation_index import INDEX_DEFINITIONS

        index_key = data.get('index')
        definition = INDEX_DEFINITIONS.get(index_key)
        if definition is None:
            raise ValueError(
                f'Choose one of: {", ".join(sorted(INDEX_DEFINITIONS))}.'
            )

        supplied = data.get('bands') or {}
        band_files = {}
        for band_name in definition.bands:
            spec = supplied.get(band_name) or {}
            file_obj = _readable_file(
                spec.get('file_id'), request.user,
                f'{band_name} band', RASTER_EXTENSIONS,
            )
            band_files[band_name] = [str(file_obj.id), int(spec.get('band') or 1)]

        return run_vegetation_index_task.delay(
            index_key, band_files,
            output_folder_id=data.get('output_folder_id'),
            soil_factor=float(data.get('soil_factor') or 0.5),
            requesting_user_id=request.user.id,
        )

    return _dispatch(request, build)


@api_login_required
def run_management_zones(request):
    def build(data):
        from .native_tool_tasks import run_management_zones_task
        source = _readable_file(
            data.get('file_id'), request.user, 'Input layer', VECTOR_EXTENSIONS
        )
        columns = data.get('columns') or []
        if not columns:
            raise ValueError('Choose at least one column to cluster on.')

        return run_management_zones_task.delay(
            str(source.id), list(columns),
            zone_count=int(data.get('zone_count') or 3),
            output_folder_id=data.get('output_folder_id'),
            requesting_user_id=request.user.id,
        )

    return _dispatch(request, build)


@api_login_required
def run_raster_clip_reproject(request):
    def build(data):
        from .native_tool_tasks import run_raster_clip_reproject_task
        raster = _readable_file(
            data.get('raster_file_id'), request.user, 'Raster', RASTER_EXTENSIONS
        )

        boundary_id = data.get('boundary_file_id') or None
        if boundary_id:
            boundary = _readable_file(
                boundary_id, request.user, 'Boundary layer', VECTOR_EXTENSIONS
            )
            boundary_id = str(boundary.id)

        target_epsg = data.get('target_epsg') or None
        if target_epsg:
            try:
                target_epsg = int(target_epsg)
            except (TypeError, ValueError):
                raise ValueError('The target CRS must be an EPSG code, such as 4326.')

        if not boundary_id and not target_epsg:
            raise ValueError('Choose a boundary to clip to, a target CRS, or both.')

        return run_raster_clip_reproject_task.delay(
            str(raster.id),
            boundary_file_id=boundary_id,
            target_epsg=target_epsg,
            resampling=data.get('resampling') or 'bilinear',
            output_folder_id=data.get('output_folder_id'),
            requesting_user_id=request.user.id,
        )

    return _dispatch(request, build)


@api_login_required
def native_tool_status(request, task_id):
    """
    Poll any of the four tasks.

    Reading a Celery result does not depend on which task produced it, so one
    endpoint serves all four rather than four identical copies.
    """
    try:
        result = AsyncResult(task_id)
        response = {'success': True, 'status': result.status}
        if result.ready():
            # A task that raised rather than returning gives a result that is
            # the exception itself, which is not JSON-serialisable.
            response['result'] = (
                result.result if result.successful()
                else {'success': False, 'error': 'The tool failed; see server logs.'}
            )
        return JsonResponse(response)
    except Exception as exc:
        return _internal_error_response(exc)


@api_login_required
def vector_columns(request, file_id):
    """The numeric attribute columns of a vector file, for the zone picker."""
    try:
        file_obj = _readable_file(file_id, request.user, 'Input layer', VECTOR_EXTENSIONS)
    except PermissionError as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=403)
    except ValueError as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=400)

    try:
        from .management_zones import numeric_columns
        columns = numeric_columns(file_obj.file.path)
    except Exception as exc:
        return _internal_error_response(exc)

    return JsonResponse({'success': True, 'columns': columns})


@api_login_required
def raster_bands(request, file_id):
    """How many bands a raster has, so the band pickers can be populated."""
    try:
        file_obj = _readable_file(file_id, request.user, 'Raster', RASTER_EXTENSIONS)
    except PermissionError as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=403)
    except ValueError as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=400)

    try:
        import rasterio
        with rasterio.open(file_obj.file.path) as source:
            count = source.count
            crs = str(source.crs) if source.crs else None
    except Exception as exc:
        return _internal_error_response(exc)

    return JsonResponse({'success': True, 'bands': count, 'crs': crs})
