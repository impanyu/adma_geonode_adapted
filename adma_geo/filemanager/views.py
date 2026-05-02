import json
import logging
import magic
import os
import unicodedata
from pathlib import Path
from urllib.parse import unquote
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib import messages
from django.views.generic import CreateView, TemplateView
from django.http import JsonResponse, HttpResponse, Http404, FileResponse
from django.db.models import Q, Count
from django.urls import reverse_lazy
from django.conf import settings
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from .models import Folder, File, Map, Tool
from .forms import RegistrationForm, FolderForm, FileUploadForm
from .tasks import process_gis_file_task
from .upload_validation import validate_uploaded_file_mime

logger = logging.getLogger(__name__)


def sanitize_folder_name(name: str) -> str:
    """
    Return a safe folder name, or '' if the input is rejected.

    Defends against path-traversal attempts that hide separators behind
    URL-encoding (%2e%2e%2f), HTML-style decoding, or overlong/decomposed
    UTF-8 (À®À®À¯ etc.). Also rejects control characters, NULL bytes,
    pure-dot names, and obvious template/injection delimiters. The
    returned value is the original (stripped) text — we only reject; we
    don't transform — so user-intended unicode names are preserved.
    """
    if not name:
        return ''

    # Decode URL-encoding then NFKC-normalize. NFKC turns overlong UTF-8
    # forms (e.g. À® → .) into canonical form so the substring tests below
    # catch the trick.
    decoded = unquote(name)
    normalized = unicodedata.normalize('NFKC', decoded).strip()

    if not normalized:
        return ''
    if len(normalized) > 200:
        return ''
    if '/' in normalized or '\\' in normalized:
        return ''
    if '..' in normalized:
        return ''
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in normalized):
        return ''
    if set(normalized) <= {'.'}:
        return ''
    # Reject characters used in common template/expression injection so we
    # don't accumulate Burp-collaborator garbage rows in the DB even if
    # they're harmless at the filesystem layer.
    if any(ch in normalized for ch in ('<', '>', '\x00')):
        return ''

    return name.strip()


def _internal_error_response(exc, message='An internal error occurred. Please try again.', status=500):
    """
    Standard 500-error response that logs the full exception server-side
    but returns only a generic message to the client. Use this everywhere
    instead of putting str(exc) into a JsonResponse.
    """
    logger.exception("Internal error: %s", exc)
    return JsonResponse({'error': message}, status=status)

def get_robust_user_statistics(user):
    """
    Calculate robust user statistics with automatic recovery of stale deletion flags.
    
    This function:
    1. Detects items marked for deletion longer than 5 minutes (likely failed deletions)
    2. Resets their deletion_in_progress flag to make them visible again
    3. Calculates accurate statistics based on what actually exists
    
    Returns: dict with statistics and debug info
    """
    from django.utils import timezone
    from datetime import timedelta
    import logging
    logger = logging.getLogger(__name__)
    
    # Find items that have been "in deletion" for more than 5 minutes (likely failed deletions)
    stale_threshold = timezone.now() - timedelta(minutes=5)
    
    # Find stale deletion items and reset their deletion_in_progress flag
    stale_files = File.objects.filter(
        owner=user, 
        deletion_in_progress=True,
        updated_at__lt=stale_threshold
    )
    stale_folders = Folder.objects.filter(
        owner=user,
        deletion_in_progress=True, 
        updated_at__lt=stale_threshold
    )
    
    stale_files_count = stale_files.count()
    stale_folders_count = stale_folders.count()
    
    if stale_files_count > 0 or stale_folders_count > 0:
        logger.warning(f"Found {stale_files_count} stale files and {stale_folders_count} stale folders marked for deletion - resetting flags")
        
        # Reset stale deletion flags
        stale_files.update(deletion_in_progress=False)
        stale_folders.update(deletion_in_progress=False)
    
    # Calculate accurate statistics based on what actually exists (exclude archived)
    active_files = File.objects.filter(owner=user, deletion_in_progress=False, is_archived=False)
    active_folders = Folder.objects.filter(owner=user, deletion_in_progress=False, is_archived=False)
    
    stats = {
        'total_files': active_files.count(),
        'total_folders': active_folders.count(),
        'total_size': sum(f.file_size for f in active_files),
        'public_files': active_files.filter(is_public=True).count(),
    }
    
    # Add debug information
    if stale_files_count > 0 or stale_folders_count > 0:
        stats['debug_info'] = {
            'stale_files_recovered': stale_files_count,
            'stale_folders_recovered': stale_folders_count,
            'calculation_method': 'robust_with_cleanup'
        }
    else:
        stats['debug_info'] = {
            'calculation_method': 'standard'
        }
    
    return stats

def generate_unique_name(name, owner, folder=None, is_folder=False):
    """
    Generate a unique name by adding _1, _2, etc. to avoid duplicates
    
    Args:
        name: Original name
        owner: User object
        folder: Parent folder (for files) or folder parent (for folders)
        is_folder: True if checking folder names, False for file names
    
    Returns:
        Unique name string
    """
    if is_folder:
        # For folders, check against other folders in the same parent
        base_queryset = Folder.objects.filter(owner=owner, parent=folder)
    else:
        # For files, check against other files in the same folder
        base_queryset = File.objects.filter(owner=owner, folder=folder)
    
    # Check if the original name already exists
    if not base_queryset.filter(name=name).exists():
        return name
    
    # Split name and extension for files
    if not is_folder and '.' in name:
        name_part, extension = os.path.splitext(name)
    else:
        name_part = name
        extension = ""
    
    # Try adding _1, _2, _3, etc. until we find a unique name
    counter = 1
    while True:
        if extension:
            new_name = f"{name_part}_{counter}{extension}"
        else:
            new_name = f"{name_part}_{counter}"
        
        if not base_queryset.filter(name=new_name).exists():
            return new_name
        
        counter += 1
        # Safety check to prevent infinite loop
        if counter > 1000:
            # If we somehow reach 1000, add timestamp
            import time
            timestamp = int(time.time())
            if extension:
                return f"{name_part}_{timestamp}{extension}"
            else:
                return f"{name_part}_{timestamp}"

class HomeView(TemplateView):
    """Public home page"""
    template_name = 'filemanager/public_home.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        
        # Get top-level public folders - folders that are public and either:
        # 1. Have no parent (truly top-level), OR
        # 2. Have a private parent (making them the highest public level)
        # EXCLUDE third-party folders (they go in a separate panel)
        public_folders_queryset = Folder.objects.filter(
            is_public=True,
            deletion_in_progress=False,
            is_third_party=False  # Exclude third-party folders
        ).filter(
            Q(parent=None) |  # No parent (truly top-level)
            Q(parent__is_public=False)  # Parent is private (so this is top public level)
        ).annotate(
            public_file_count=Count('files', filter=Q(files__is_public=True)),
            public_subfolder_count=Count('subfolders', filter=Q(subfolders__is_public=True))
        ).filter(
            Q(public_file_count__gt=0) | Q(public_subfolder_count__gt=0)  # Only folders with public content
        ).order_by('name')
        
        # Get top-level public files - files that are public and either:
        # 1. Have no folder (orphaned files), OR  
        # 2. Have a private folder (making them the highest public level)
        # EXCLUDE third-party files (they go in a separate panel)
        public_files_queryset = File.objects.filter(
            is_public=True,
            deletion_in_progress=False,
            is_third_party=False  # Exclude third-party files
        ).filter(
            Q(folder=None) |  # No folder (orphaned)
            Q(folder__is_public=False)  # Folder is private (so this file is top public level)
        ).order_by('-created_at')
        
        # Get third-party folders (top-level only, visible to all)
        third_party_folders = Folder.objects.filter(
            is_public=True,
            deletion_in_progress=False,
            is_third_party=True,
            parent=None  # Only top-level third-party folders
        ).order_by('name')
        
        # Get third-party files (orphaned or with private parent)
        third_party_files = File.objects.filter(
            is_public=True,
            deletion_in_progress=False,
            is_third_party=True
        ).filter(
            Q(folder=None) | Q(folder__is_public=False)
        ).order_by('-created_at')
        
        # Get public maps - separate panel, no pagination mixing
        public_maps = Map.objects.filter(
            is_public=True,
            deletion_in_progress=False
        ).order_by('-created_at')
        
        # Pagination for folders and files only (100 items per page)
        page = self.request.GET.get('page', 1)
        items_per_page = 100
        
        # Calculate total items (folders + files only, maps are separate)
        total_folders = public_folders_queryset.count()
        total_files = public_files_queryset.count()
        total_maps = public_maps.count()
        total_third_party_folders = third_party_folders.count()
        total_third_party_files = third_party_files.count()
        total_items = total_folders + total_files
        
        # Set up pagination
        paginator = Paginator(range(total_items), items_per_page)
        
        try:
            page_obj = paginator.page(page)
        except PageNotAnInteger:
            page_obj = paginator.page(1)
        except EmptyPage:
            page_obj = paginator.page(paginator.num_pages)
        
        # Calculate which items to show on this page
        start_index = (page_obj.number - 1) * items_per_page
        end_index = start_index + items_per_page
        
        # Initialize empty querysets
        public_folders = Folder.objects.none()
        public_files = File.objects.none()
        
        # Determine which content types are in this page range
        if start_index < total_folders:
            # Page starts with folders
            if end_index <= total_folders:
                # Page contains only folders
                public_folders = public_folders_queryset[start_index:end_index]
            else:
                # Page contains folders and files
                public_folders = public_folders_queryset[start_index:]
                remaining_slots = end_index - total_folders
                public_files = public_files_queryset[0:remaining_slots]
        else:
            # Page starts with files
            files_start = start_index - total_folders
            public_files = public_files_queryset[files_start:files_start + items_per_page]
        
        context['public_folders'] = public_folders
        context['public_files'] = public_files
        context['public_maps'] = public_maps  # All maps, separate panel
        context['third_party_folders'] = third_party_folders
        context['third_party_files'] = third_party_files
        context['page_obj'] = page_obj
        context['total_items'] = total_items
        
        # Statistics for public data
        context['stats'] = {
            'total_public_files': total_files,
            'total_public_folders': total_folders,
            'total_public_maps': total_maps,
            'total_third_party_folders': total_third_party_folders,
            'total_third_party_files': total_third_party_files,
        }
        
        return context

# Temporary access gate for the agent feature. The agent system runs Docker
# containers on behalf of users and has known security gaps; until those are
# fixed, restrict access to a single allowlisted username. To re-enable for
# everyone, delete the AGENT_ALLOWED_USERNAMES check sites (this decorator
# and `agent_user_required` import in api_views.py) and the {% if %} guard
# in base.html.
AGENT_ALLOWED_USERNAMES = frozenset({'Yu'})


def agent_user_required(view_func):
    """Allow only allowlisted usernames; everyone else gets 403."""
    from functools import wraps
    from django.http import HttpResponseForbidden, JsonResponse

    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        user = getattr(request, 'user', None)
        if not user or not user.is_authenticated:
            return HttpResponseForbidden('Authentication required')
        if user.username not in AGENT_ALLOWED_USERNAMES:
            # Match Content-Type to the request — JSON for API, HTML for page.
            accept = request.META.get('HTTP_ACCEPT', '')
            if 'application/json' in accept or request.path.startswith('/api/'):
                return JsonResponse(
                    {'error': 'Agent feature is currently unavailable for your account'},
                    status=403,
                )
            return HttpResponseForbidden(
                'Agent feature is currently unavailable for your account'
            )
        return view_func(request, *args, **kwargs)
    return _wrapped


@login_required
@agent_user_required
def agent_chat_page(request):
    """Agent chat page - renders the chat UI."""
    return render(request, 'filemanager/agent_chat.html')


@login_required
def dashboard(request):
    """Main dashboard for authenticated users"""
    user = request.user
    
    # Get user's root folders (exclude items being deleted and third-party items)
    user_folders_queryset = Folder.objects.filter(
        owner=user, 
        parent=None, 
        deletion_in_progress=False,
        is_third_party=False  # Exclude third-party folders
    ).order_by('name')
    
    # Get public root folders from OTHER users only (not current user's folders, exclude third-party)
    public_folders_from_others = Folder.objects.filter(
        is_public=True, 
        deletion_in_progress=False,
        parent=None,  # Only top-level folders
        is_third_party=False  # Exclude third-party folders
    ).exclude(owner=user).order_by('name')
    
    # Combine user's folders with public folders from others
    # User's folders first, then public folders from others
    from itertools import chain
    folders_list = list(chain(user_folders_queryset, public_folders_from_others))
    
    # Get root-level files (not files inside folders, exclude items being deleted and third-party)
    user_files_queryset = File.objects.filter(
        owner=user, 
        folder=None, 
        deletion_in_progress=False,
        is_third_party=False  # Exclude third-party files
    ).order_by('-created_at')
    
    # Get public root files from OTHER users only (exclude third-party)
    public_files_from_others = File.objects.filter(
        is_public=True, 
        deletion_in_progress=False,
        folder=None,  # Only files not inside any folder
        is_third_party=False  # Exclude third-party files
    ).exclude(owner=user).order_by('-created_at')
    
    # Combine user's files with public files from others
    files_list = list(chain(user_files_queryset, public_files_from_others))
    
    # Combine folders and files for pagination
    page = request.GET.get('page', 1)
    items_per_page = 100
    
    # Calculate total items
    total_folders = len(folders_list)
    total_files = len(files_list)
    total_items = total_folders + total_files
    
    # Set up pagination
    paginator = Paginator(range(total_items), items_per_page)
    
    try:
        page_obj = paginator.page(page)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)
    
    # Calculate which items to show on this page
    start_index = (page_obj.number - 1) * items_per_page
    end_index = start_index + items_per_page
    
    # Get the actual items for this page
    if start_index < total_folders:
        # Page starts with folders
        if end_index <= total_folders:
            # Page contains only folders
            folders = folders_list[start_index:end_index]
            recent_files = []
        else:
            # Page contains some folders and some files
            folders = folders_list[start_index:]
            files_start = 0
            files_end = end_index - total_folders
            recent_files = files_list[files_start:files_end]
    else:
        # Page starts with files
        folders = []
        files_start = start_index - total_folders
        files_end = files_start + items_per_page
        recent_files = files_list[files_start:files_end]
    
    # Robust statistics with automatic stale deletion recovery
    stats = get_robust_user_statistics(user)
    
    # Get all maps: user's maps + public maps from others (combined, no duplicates)
    user_maps = Map.objects.filter(owner=user, deletion_in_progress=False)
    public_maps_from_others = Map.objects.filter(is_public=True, deletion_in_progress=False).exclude(owner=user)
    all_maps = (user_maps | public_maps_from_others).distinct().order_by('-created_at')
    
    # Get third-party data visible to the current user:
    # - User's own third-party folders/files
    # - Public third-party folders/files from other users
    third_party_folders = Folder.objects.filter(
        is_third_party=True,
        deletion_in_progress=False,
        parent=None  # Only top-level third-party folders
    ).filter(
        Q(owner=user) | Q(is_public=True)  # User's own OR public
    ).distinct().order_by('third_party_source', 'name')
    
    third_party_files = File.objects.filter(
        is_third_party=True,
        deletion_in_progress=False,
        folder=None  # Only root-level third-party files
    ).filter(
        Q(owner=user) | Q(is_public=True)  # User's own OR public
    ).distinct().order_by('third_party_source', '-created_at')
    
    return render(request, 'filemanager/dashboard.html', {
        'folders': folders,
        'recent_files': recent_files,
        'stats': stats,
        'current_folder': None,
        'page_obj': page_obj,
        'total_items': total_items,
        'can_edit': True,  # User can always edit their own dashboard
        # Maps panel (user's maps + public maps)
        'all_maps': all_maps,
        # Third-party data panel
        'third_party_folders': third_party_folders,
        'third_party_files': third_party_files,
        'total_third_party_folders': third_party_folders.count(),
        'total_third_party_files': third_party_files.count(),
    })

@login_required
def folder_detail(request, folder_id):
    """View folder contents"""
    folder = get_object_or_404(Folder, id=folder_id)
    
    # Check permissions
    if folder.owner != request.user and not folder.is_public:
        messages.error(request, "You don't have permission to view this folder.")
        return redirect('filemanager:dashboard')
    
    # Get subfolders and files with pagination (exclude items being deleted)
    subfolders_queryset = folder.subfolders.filter(deletion_in_progress=False).order_by('name')
    files_queryset = folder.files.filter(deletion_in_progress=False).order_by('-created_at')
    
    # Combine subfolders and files for pagination
    page = request.GET.get('page', 1)
    items_per_page = 100
    
    # Calculate total items
    total_subfolders = subfolders_queryset.count()
    total_files = files_queryset.count()
    total_items = total_subfolders + total_files
    
    # Set up pagination
    paginator = Paginator(range(total_items), items_per_page)
    
    try:
        page_obj = paginator.page(page)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)
    
    # Calculate which items to show on this page
    start_index = (page_obj.number - 1) * items_per_page
    end_index = start_index + items_per_page
    
    # Get the actual items for this page
    if start_index < total_subfolders:
        # Page starts with subfolders
        if end_index <= total_subfolders:
            # Page contains only subfolders
            subfolders = subfolders_queryset[start_index:end_index]
            files = File.objects.none()
        else:
            # Page contains some subfolders and some files
            subfolders = subfolders_queryset[start_index:]
            files_start = 0
            files_end = end_index - total_subfolders
            files = files_queryset[files_start:files_end]
    else:
        # Page starts with files
        subfolders = Folder.objects.none()
        files_start = start_index - total_subfolders
        files_end = files_start + items_per_page
        files = files_queryset[files_start:files_end]
    
    # Check if this is the John Deere root folder
    is_jd_root = False
    jd_all_field_boundaries = []
    
    # Check if this is a John Deere field folder (has johndeere parent and boundary subfolder)
    jd_field_boundary_file = None
    is_jd_field = False
    
    # Check if this is a John Deere field operation folder
    jd_operation_boundary_file = None
    is_jd_operation = False
    
    if folder.is_third_party and folder.third_party_source == 'johndeere':
        # Check if this IS the John Deere root folder
        if folder.parent is None and folder.name == 'John Deere':
            is_jd_root = True
            # Collect all field boundaries from child field folders
            for field_folder in folder.subfolders.all():
                boundary_folder_name = f"{field_folder.name}_boundary"
                boundary_folder = field_folder.subfolders.filter(name=boundary_folder_name).first()
                if boundary_folder:
                    boundary_file = File.objects.filter(
                        folder=boundary_folder,
                        name__endswith='.shp',
                        is_spatial=True
                    ).first()
                    if boundary_file and boundary_file.spatial_extent:
                        jd_all_field_boundaries.append({
                            'name': field_folder.name,
                            'folder_id': str(field_folder.id),
                            'file': boundary_file,
                            'extent': boundary_file.spatial_extent,
                        })
        
        # Check if parent is the John Deere root folder (this would make it a field folder)
        elif folder.parent and folder.parent.name == 'John Deere':
            is_jd_field = True
            # Look for {field_name}_boundary subfolder
            boundary_folder_name = f"{folder.name}_boundary"
            boundary_folder = folder.subfolders.filter(name=boundary_folder_name).first()
            if boundary_folder:
                # Find the .shp file in the boundary folder
                jd_field_boundary_file = File.objects.filter(
                    folder=boundary_folder,
                    name__endswith='.shp',
                    is_spatial=True
                ).first()
        
        # Check if this is a field operation folder (parent is 'field_operations')
        elif folder.parent and folder.parent.name == 'field_operations':
            is_jd_operation = True
            # Look for {operation_id}_boundary subfolder
            boundary_folder_name = f"{folder.name}_boundary"
            boundary_folder = folder.subfolders.filter(name=boundary_folder_name).first()
            if boundary_folder:
                # Find the .shp file in the boundary folder
                jd_operation_boundary_file = File.objects.filter(
                    folder=boundary_folder,
                    name__endswith='.shp',
                    is_spatial=True
                ).first()
    
    # Check if this is the Realm5 root folder
    is_realm5_root = False
    realm5_device_locations = []
    
    # Device location mapping (latitude, longitude)
    REALM5_DEVICE_LOCATIONS = {
        'Farm Shop Weatherstation': (41.176062, -96.471528),
        'Entomology Weather Station': (41.163352, -96.498613),
        'Cow/Calf Weather Station': (41.132303, -96.464161),
    }
    
    # Check if this is a Realm5 device folder
    is_realm5_device = False
    realm5_all_data = None
    realm5_all_variables = None
    
    if folder.is_third_party and folder.third_party_source == 'realm5':
        # Check if this IS the Realm5 root folder
        if folder.parent is None and folder.name == 'realm5':
            is_realm5_root = True
            # Collect all device folders with their locations
            for device_folder in folder.subfolders.all():
                device_name = device_folder.name
                # Try to match device name with known locations
                location = REALM5_DEVICE_LOCATIONS.get(device_name)
                if not location:
                    # Try partial match
                    for known_name, coords in REALM5_DEVICE_LOCATIONS.items():
                        if known_name.lower() in device_name.lower() or device_name.lower() in known_name.lower():
                            location = coords
                            break
                
                if location:
                    realm5_device_locations.append({
                        'name': device_name,
                        'folder_id': str(device_folder.id),
                        'lat': location[0],
                        'lon': location[1],
                    })
        
        # Check if parent is the Realm5 root folder (this would make it a device folder)
        elif folder.parent and folder.parent.name == 'realm5':
            is_realm5_device = True
            
            # Get device location for the map
            device_name = folder.name
            device_location = REALM5_DEVICE_LOCATIONS.get(device_name)
            if not device_location:
                # Try partial match
                for known_name, coords in REALM5_DEVICE_LOCATIONS.items():
                    if known_name.lower() in device_name.lower() or device_name.lower() in known_name.lower():
                        device_location = coords
                        break
            
            if device_location:
                realm5_device_locations.append({
                    'name': device_name,
                    'folder_id': str(folder.id),
                    'lat': device_location[0],
                    'lon': device_location[1],
                })
            
            # Look for all.json file in this folder
            all_json_file = File.objects.filter(folder=folder, name='all.json').first()
            if all_json_file and all_json_file.file_size < 5 * 1024 * 1024:  # Max 5MB
                try:
                    with all_json_file.file.open('r') as f:
                        all_json_data = json.load(f)
                    
                    # Check if it has the expected structure
                    if 'daily_averages' in all_json_data and isinstance(all_json_data['daily_averages'], list):
                        daily_averages = all_json_data['daily_averages']
                        if daily_averages:
                            # Get variables from the data
                            first_avg = daily_averages[0]
                            realm5_all_variables = [key for key in first_avg.keys() 
                                                   if key not in ('date', 'observation_count') 
                                                   and isinstance(first_avg.get(key), (int, float))]
                            
                            realm5_all_data = {
                                'device_name': all_json_data.get('device_name'),
                                'dev_eui': all_json_data.get('dev_eui'),
                                'device_type': all_json_data.get('device_type'),
                                'total_days': all_json_data.get('total_days', len(daily_averages)),
                                'date_range': all_json_data.get('date_range', {}),
                                'daily_averages': daily_averages,
                                'variables': realm5_all_variables,
                            }
                except Exception as e:
                    logger.warning("Error loading all.json for Realm5 device: %s", type(e).__name__)

    # Pre-escape spatial_extent strings (raw JSON strings from DB) so that
    # </script> sequences cannot break out of JS context (XSS A-6/A-7 fix).
    def _escape_extent(extent_str):
        if not extent_str:
            return None
        return extent_str.replace('&', '\\u0026').replace('<', '\\u003c').replace('>', '\\u003e')

    for boundary in jd_all_field_boundaries:
        boundary['extent_safe'] = _escape_extent(boundary.get('extent'))

    jd_field_extent_safe = _escape_extent(
        jd_field_boundary_file.spatial_extent if jd_field_boundary_file else None
    )
    jd_operation_extent_safe = _escape_extent(
        jd_operation_boundary_file.spatial_extent if jd_operation_boundary_file else None
    )

    return render(request, 'filemanager/folder_detail.html', {
        'folder': folder,
        'subfolders': subfolders,
        'files': files,
        'breadcrumbs': folder.get_breadcrumbs(),
        'can_edit': folder.owner == request.user,
        'page_obj': page_obj,
        'total_items': total_items,
        'total_subfolders': total_subfolders,
        'total_files': total_files,
        'is_jd_root': is_jd_root,
        'jd_all_field_boundaries': jd_all_field_boundaries,
        'is_jd_field': is_jd_field,
        'jd_field_boundary_file': jd_field_boundary_file,
        'jd_field_extent_safe': jd_field_extent_safe,
        'is_jd_operation': is_jd_operation,
        'jd_operation_boundary_file': jd_operation_boundary_file,
        'jd_operation_extent_safe': jd_operation_extent_safe,
        'is_realm5_root': is_realm5_root,
        'realm5_device_locations': realm5_device_locations,
        'is_realm5_device': is_realm5_device,
        'realm5_all_data': realm5_all_data,
        'realm5_all_variables': realm5_all_variables,
    })

def public_folder_detail(request, folder_id):
    """Public view of folder contents"""
    folder = get_object_or_404(Folder, id=folder_id, is_public=True)
    
    # If user is authenticated and owns this folder, redirect to dashboard
    if request.user.is_authenticated and folder.owner == request.user:
        return redirect('filemanager:folder_detail', folder_id=folder_id)
    
    # Get public subfolders and files with pagination
    subfolders_queryset = folder.subfolders.filter(is_public=True).order_by('name')
    files_queryset = folder.files.filter(is_public=True).order_by('-created_at')
    
    # Pagination setup
    page = request.GET.get('page', 1)
    items_per_page = 100
    
    # Calculate total items
    total_subfolders = subfolders_queryset.count()
    total_files = files_queryset.count()
    total_items = total_subfolders + total_files
    
    # Set up pagination
    paginator = Paginator(range(total_items), items_per_page)
    
    try:
        page_obj = paginator.page(page)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)
    
    # Calculate which items to show on this page
    start_index = (page_obj.number - 1) * items_per_page
    end_index = start_index + items_per_page
    
    # Get the actual items for this page
    if start_index < total_subfolders:
        # Page starts with subfolders
        if end_index <= total_subfolders:
            # Page contains only subfolders
            subfolders = subfolders_queryset[start_index:end_index]
            files = File.objects.none()
        else:
            # Page contains some subfolders and some files
            subfolders = subfolders_queryset[start_index:]
            files_start = 0
            files_end = end_index - total_subfolders
            files = files_queryset[files_start:files_end]
    else:
        # Page starts with files
        subfolders = Folder.objects.none()
        files_start = start_index - total_subfolders
        files_end = files_start + items_per_page
        files = files_queryset[files_start:files_end]
    
    # Check if this is the John Deere root folder
    is_jd_root = False
    jd_all_field_boundaries = []
    
    # Check if this is a John Deere field folder (has johndeere parent and boundary subfolder)
    jd_field_boundary_file = None
    is_jd_field = False
    
    # Check if this is a John Deere field operation folder
    jd_operation_boundary_file = None
    is_jd_operation = False
    
    if folder.is_third_party and folder.third_party_source == 'johndeere':
        # Check if this IS the John Deere root folder
        if folder.parent is None and folder.name == 'John Deere':
            is_jd_root = True
            # Collect all field boundaries from child field folders (public only)
            for field_folder in folder.subfolders.filter(is_public=True):
                boundary_folder_name = f"{field_folder.name}_boundary"
                boundary_folder = field_folder.subfolders.filter(name=boundary_folder_name, is_public=True).first()
                if boundary_folder:
                    boundary_file = File.objects.filter(
                        folder=boundary_folder,
                        name__endswith='.shp',
                        is_spatial=True,
                        is_public=True
                    ).first()
                    if boundary_file and boundary_file.spatial_extent:
                        jd_all_field_boundaries.append({
                            'name': field_folder.name,
                            'folder_id': str(field_folder.id),
                            'file': boundary_file,
                            'extent': boundary_file.spatial_extent,
                        })
        
        # Check if parent is the John Deere root folder (this would make it a field folder)
        elif folder.parent and folder.parent.name == 'John Deere':
            is_jd_field = True
            # Look for {field_name}_boundary subfolder
            boundary_folder_name = f"{folder.name}_boundary"
            boundary_folder = folder.subfolders.filter(name=boundary_folder_name, is_public=True).first()
            if boundary_folder:
                # Find the .shp file in the boundary folder
                jd_field_boundary_file = File.objects.filter(
                    folder=boundary_folder,
                    name__endswith='.shp',
                    is_spatial=True,
                    is_public=True
                ).first()
        
        # Check if this is a field operation folder (parent is 'field_operations')
        elif folder.parent and folder.parent.name == 'field_operations':
            is_jd_operation = True
            # Look for {operation_id}_boundary subfolder
            boundary_folder_name = f"{folder.name}_boundary"
            boundary_folder = folder.subfolders.filter(name=boundary_folder_name, is_public=True).first()
            if boundary_folder:
                # Find the .shp file in the boundary folder
                jd_operation_boundary_file = File.objects.filter(
                    folder=boundary_folder,
                    name__endswith='.shp',
                    is_spatial=True,
                    is_public=True
                ).first()
    
    # Check if this is the Realm5 root folder
    is_realm5_root = False
    realm5_device_locations = []
    
    # Device location mapping (latitude, longitude)
    REALM5_DEVICE_LOCATIONS = {
        'Farm Shop Weatherstation': (41.176062, -96.471528),
        'Entomology Weather Station': (41.163352, -96.498613),
        'Cow/Calf Weather Station': (41.132303, -96.464161),
    }
    
    # Check if this is a Realm5 device folder
    is_realm5_device = False
    realm5_all_data = None
    realm5_all_variables = None
    
    if folder.is_third_party and folder.third_party_source == 'realm5':
        # Check if this IS the Realm5 root folder
        if folder.parent is None and folder.name == 'realm5':
            is_realm5_root = True
            # Collect all device folders with their locations (public only)
            for device_folder in folder.subfolders.filter(is_public=True):
                device_name = device_folder.name
                # Try to match device name with known locations
                location = REALM5_DEVICE_LOCATIONS.get(device_name)
                if not location:
                    # Try partial match
                    for known_name, coords in REALM5_DEVICE_LOCATIONS.items():
                        if known_name.lower() in device_name.lower() or device_name.lower() in known_name.lower():
                            location = coords
                            break
                
                if location:
                    realm5_device_locations.append({
                        'name': device_name,
                        'folder_id': str(device_folder.id),
                        'lat': location[0],
                        'lon': location[1],
                    })
        
        # Check if parent is the Realm5 root folder (this would make it a device folder)
        elif folder.parent and folder.parent.name == 'realm5':
            is_realm5_device = True
            
            # Get device location for the map
            device_name = folder.name
            device_location = REALM5_DEVICE_LOCATIONS.get(device_name)
            if not device_location:
                # Try partial match
                for known_name, coords in REALM5_DEVICE_LOCATIONS.items():
                    if known_name.lower() in device_name.lower() or device_name.lower() in known_name.lower():
                        device_location = coords
                        break
            
            if device_location:
                realm5_device_locations.append({
                    'name': device_name,
                    'folder_id': str(folder.id),
                    'lat': device_location[0],
                    'lon': device_location[1],
                })
            
            # Look for all.json file in this folder
            all_json_file = File.objects.filter(folder=folder, name='all.json', is_public=True).first()
            if all_json_file and all_json_file.file_size < 5 * 1024 * 1024:  # Max 5MB
                try:
                    with all_json_file.file.open('r') as f:
                        all_json_data = json.load(f)
                    
                    # Check if it has the expected structure
                    if 'daily_averages' in all_json_data and isinstance(all_json_data['daily_averages'], list):
                        daily_averages = all_json_data['daily_averages']
                        if daily_averages:
                            # Get variables from the data
                            first_avg = daily_averages[0]
                            realm5_all_variables = [key for key in first_avg.keys() 
                                                   if key not in ('date', 'observation_count') 
                                                   and isinstance(first_avg.get(key), (int, float))]
                            
                            realm5_all_data = {
                                'device_name': all_json_data.get('device_name'),
                                'dev_eui': all_json_data.get('dev_eui'),
                                'device_type': all_json_data.get('device_type'),
                                'total_days': all_json_data.get('total_days', len(daily_averages)),
                                'date_range': all_json_data.get('date_range', {}),
                                'daily_averages': daily_averages,
                                'variables': realm5_all_variables,
                            }
                except Exception as e:
                    logger.warning("Error loading all.json for Realm5 device: %s", type(e).__name__)

    # Pre-escape spatial_extent strings (raw JSON strings from DB) so that
    # </script> sequences cannot break out of JS context (XSS A-6/A-7 fix).
    def _escape_extent(extent_str):
        if not extent_str:
            return None
        return extent_str.replace('&', '\\u0026').replace('<', '\\u003c').replace('>', '\\u003e')

    for boundary in jd_all_field_boundaries:
        boundary['extent_safe'] = _escape_extent(boundary.get('extent'))

    jd_field_extent_safe = _escape_extent(
        jd_field_boundary_file.spatial_extent if jd_field_boundary_file else None
    )
    jd_operation_extent_safe = _escape_extent(
        jd_operation_boundary_file.spatial_extent if jd_operation_boundary_file else None
    )

    # Reuse the same template as private folder detail, but with public view context
    return render(request, 'filemanager/folder_detail.html', {
        'folder': folder,
        'subfolders': subfolders,
        'files': files,
        'breadcrumbs': folder.get_public_breadcrumbs(),  # Use public breadcrumbs
        'can_edit': False,  # Public users can't edit
        'is_public_view': True,  # Flag to adjust breadcrumbs and navigation
        'page_obj': page_obj,
        'total_items': total_items,
        'total_subfolders': total_subfolders,
        'total_files': total_files,
        'is_jd_root': is_jd_root,
        'jd_all_field_boundaries': jd_all_field_boundaries,
        'is_jd_field': is_jd_field,
        'jd_field_boundary_file': jd_field_boundary_file,
        'jd_field_extent_safe': jd_field_extent_safe,
        'is_jd_operation': is_jd_operation,
        'jd_operation_boundary_file': jd_operation_boundary_file,
        'jd_operation_extent_safe': jd_operation_extent_safe,
        'is_realm5_root': is_realm5_root,
        'realm5_device_locations': realm5_device_locations,
        'is_realm5_device': is_realm5_device,
        'realm5_all_data': realm5_all_data,
        'realm5_all_variables': realm5_all_variables,
    })

@login_required
def file_detail(request, file_id):
    """View file details and preview"""
    file_obj = get_object_or_404(File, id=file_id)
    
    # Check permissions
    if file_obj.owner != request.user and not file_obj.is_public:
        messages.error(request, "You don't have permission to view this file.")
        return redirect('filemanager:dashboard')
    
    # For text files, read content for preview
    file_content = None
    csv_data = None
    csv_headers = None
    
    if file_obj.file_type == 'text' and file_obj.file_size < 1024 * 1024:  # Max 1MB for preview
        try:
            with file_obj.file.open('r') as f:
                file_content = f.read()
        except (UnicodeDecodeError, FileNotFoundError):
            file_content = None
    
    # For CSV files, parse data for table view and visualization
    elif file_obj.file_type == 'csv' and file_obj.file_size < 5 * 1024 * 1024:  # Max 5MB for CSV
        import csv
        try:
            with file_obj.file.open('r') as f:
                csv_reader = csv.reader(f)
                csv_headers = next(csv_reader, None)  # Get headers
                if csv_headers:
                    # Add row_number as first column
                    csv_headers = ['row_number'] + csv_headers
                    csv_data = []
                    for row_num, row in enumerate(csv_reader, 1):
                        if len(csv_data) < 1000:  # Limit to 1000 rows for display
                            csv_data.append([row_num] + row)
                        else:
                            break
        except (UnicodeDecodeError, FileNotFoundError, csv.Error):
            csv_data = None
            csv_headers = None
    
    # For spreadsheet files (Excel), parse data for table view and visualization
    elif file_obj.file_type == 'spreadsheet' and file_obj.file_size < 10 * 1024 * 1024:  # Max 10MB for Excel
        try:
            import pandas as pd
            # Read Excel file using pandas
            with file_obj.file.open('rb') as f:
                df = pd.read_excel(f, engine='openpyxl' if file_obj.file.name.endswith('.xlsx') else 'xlrd')
                
                # Convert to list format similar to CSV
                csv_headers = ['row_number'] + df.columns.tolist()
                csv_data = []
                
                for row_num, (_, row) in enumerate(df.iterrows(), 1):
                    if len(csv_data) < 1000:  # Limit to 1000 rows for display
                        row_data = [row_num] + [str(val) if pd.notna(val) else '' for val in row.values]
                        csv_data.append(row_data)
                    else:
                        break
        except Exception as e:
            logger.warning("Error processing Excel file: %s", type(e).__name__)
            csv_data = None
            csv_headers = None
    
    # Check if this is a Realm5 observation JSON file
    realm5_observation_data = None
    realm5_variables = None
    is_realm5_observation = False
    
    # Check if this is a Realm5 all.json aggregated file
    realm5_all_data = None
    realm5_all_variables = None
    is_realm5_all = False
    
    if (file_obj.is_third_party and 
        file_obj.third_party_source == 'realm5' and 
        file_obj.name.endswith('.json') and
        file_obj.file_size < 5 * 1024 * 1024):  # Max 5MB
        try:
            with file_obj.file.open('r') as f:
                observation_json = json.load(f)
                
            # Check if it's an all.json file (has daily_averages)
            if 'daily_averages' in observation_json and isinstance(observation_json['daily_averages'], list):
                daily_averages = observation_json['daily_averages']
                if daily_averages:
                    # Extract available variables from the first daily average
                    first_avg = daily_averages[0]
                    # Get all numeric variables (exclude 'date' and 'observation_count')
                    realm5_all_variables = [key for key in first_avg.keys() 
                                           if key not in ('date', 'observation_count') 
                                           and isinstance(first_avg.get(key), (int, float))]
                    
                    # Prepare data for visualization
                    realm5_all_data = {
                        'device_name': observation_json.get('device_name'),
                        'dev_eui': observation_json.get('dev_eui'),
                        'device_type': observation_json.get('device_type'),
                        'total_days': observation_json.get('total_days', len(daily_averages)),
                        'date_range': observation_json.get('date_range', {}),
                        'daily_averages': daily_averages,
                        'variables': realm5_all_variables,
                    }
                    is_realm5_all = True
            
            # Check if it's a daily observation file (has observations)
            elif 'observations' in observation_json and isinstance(observation_json['observations'], list):
                observations = observation_json['observations']
                if observations:
                    # Extract available variables from the first observation
                    first_obs = observations[0]
                    # Get all numeric variables (exclude 'timestamp')
                    realm5_variables = [key for key in first_obs.keys() 
                                       if key != 'timestamp' and isinstance(first_obs.get(key), (int, float))]
                    
                    # Prepare data for visualization
                    realm5_observation_data = {
                        'date': observation_json.get('date'),
                        'device_name': observation_json.get('device_name'),
                        'dev_eui': observation_json.get('dev_eui'),
                        'observation_count': observation_json.get('observation_count', len(observations)),
                        'observations': observations,
                        'variables': realm5_variables,
                    }
                    is_realm5_observation = True
        except Exception as e:
            logger.warning("Error parsing Realm5 observation JSON: %s", type(e).__name__)
            realm5_observation_data = None
            realm5_all_data = None
    
    return render(request, 'filemanager/file_detail.html', {
        'file': file_obj,
        'file_content': file_content,
        'csv_data': csv_data,
        'csv_headers': csv_headers,
        'can_edit': file_obj.owner == request.user,
        'is_realm5_observation': is_realm5_observation,
        'realm5_observation_data': realm5_observation_data,
        'realm5_variables': realm5_variables,
        'is_realm5_all': is_realm5_all,
        'realm5_all_data': realm5_all_data,
        'realm5_all_variables': realm5_all_variables,
    })

def public_file_detail(request, file_id):
    """Public view of file details"""
    file_obj = get_object_or_404(File, id=file_id, is_public=True)
    
    # If user is authenticated and owns this file, redirect to dashboard
    if request.user.is_authenticated and file_obj.owner == request.user:
        return redirect('filemanager:file_detail', file_id=file_id)
    
    # For text files, read content for preview
    file_content = None
    csv_data = None
    csv_headers = None
    
    if file_obj.file_type == 'text' and file_obj.file_size < 1024 * 1024:  # Max 1MB for preview
        try:
            with file_obj.file.open('r') as f:
                file_content = f.read()
        except (UnicodeDecodeError, FileNotFoundError):
            file_content = None
    
    # For CSV files, parse data for table view and visualization
    elif file_obj.file_type == 'csv' and file_obj.file_size < 5 * 1024 * 1024:  # Max 5MB for CSV
        import csv
        try:
            with file_obj.file.open('r') as f:
                csv_reader = csv.reader(f)
                csv_headers = next(csv_reader, None)  # Get headers
                if csv_headers:
                    # Add row_number as first column
                    csv_headers = ['row_number'] + csv_headers
                    csv_data = []
                    for row_num, row in enumerate(csv_reader, 1):
                        if len(csv_data) < 1000:  # Limit to 1000 rows for display
                            csv_data.append([row_num] + row)
                        else:
                            break
        except (UnicodeDecodeError, FileNotFoundError, csv.Error):
            csv_data = None
            csv_headers = None
    
    # For spreadsheet files (Excel), parse data for table view and visualization
    elif file_obj.file_type == 'spreadsheet' and file_obj.file_size < 10 * 1024 * 1024:  # Max 10MB for Excel
        try:
            import pandas as pd
            # Read Excel file using pandas
            with file_obj.file.open('rb') as f:
                df = pd.read_excel(f, engine='openpyxl' if file_obj.file.name.endswith('.xlsx') else 'xlrd')
                
                # Convert to list format similar to CSV
                csv_headers = ['row_number'] + df.columns.tolist()
                csv_data = []
                
                for row_num, (_, row) in enumerate(df.iterrows(), 1):
                    if len(csv_data) < 1000:  # Limit to 1000 rows for display
                        row_data = [row_num] + [str(val) if pd.notna(val) else '' for val in row.values]
                        csv_data.append(row_data)
                    else:
                        break
        except Exception as e:
            logger.warning("Error processing Excel file: %s", type(e).__name__)
            csv_data = None
            csv_headers = None
    
    # Check if this is a Realm5 observation JSON file
    realm5_observation_data = None
    realm5_variables = None
    is_realm5_observation = False
    
    # Check if this is a Realm5 all.json aggregated file
    realm5_all_data = None
    realm5_all_variables = None
    is_realm5_all = False
    
    if (file_obj.is_third_party and 
        file_obj.third_party_source == 'realm5' and 
        file_obj.name.endswith('.json') and
        file_obj.file_size < 5 * 1024 * 1024):  # Max 5MB
        try:
            with file_obj.file.open('r') as f:
                observation_json = json.load(f)
                
            # Check if it's an all.json file (has daily_averages)
            if 'daily_averages' in observation_json and isinstance(observation_json['daily_averages'], list):
                daily_averages = observation_json['daily_averages']
                if daily_averages:
                    # Extract available variables from the first daily average
                    first_avg = daily_averages[0]
                    # Get all numeric variables (exclude 'date' and 'observation_count')
                    realm5_all_variables = [key for key in first_avg.keys() 
                                           if key not in ('date', 'observation_count') 
                                           and isinstance(first_avg.get(key), (int, float))]
                    
                    # Prepare data for visualization
                    realm5_all_data = {
                        'device_name': observation_json.get('device_name'),
                        'dev_eui': observation_json.get('dev_eui'),
                        'device_type': observation_json.get('device_type'),
                        'total_days': observation_json.get('total_days', len(daily_averages)),
                        'date_range': observation_json.get('date_range', {}),
                        'daily_averages': daily_averages,
                        'variables': realm5_all_variables,
                    }
                    is_realm5_all = True
            
            # Check if it's a daily observation file (has observations)
            elif 'observations' in observation_json and isinstance(observation_json['observations'], list):
                observations = observation_json['observations']
                if observations:
                    # Extract available variables from the first observation
                    first_obs = observations[0]
                    # Get all numeric variables (exclude 'timestamp')
                    realm5_variables = [key for key in first_obs.keys() 
                                       if key != 'timestamp' and isinstance(first_obs.get(key), (int, float))]
                    
                    # Prepare data for visualization
                    realm5_observation_data = {
                        'date': observation_json.get('date'),
                        'device_name': observation_json.get('device_name'),
                        'dev_eui': observation_json.get('dev_eui'),
                        'observation_count': observation_json.get('observation_count', len(observations)),
                        'observations': observations,
                        'variables': realm5_variables,
                    }
                    is_realm5_observation = True
        except Exception as e:
            logger.warning("Error parsing Realm5 observation JSON: %s", type(e).__name__)
            realm5_observation_data = None
            realm5_all_data = None
    
    # Reuse the same template as private file detail, but with can_edit=False
    return render(request, 'filemanager/file_detail.html', {
        'file': file_obj,
        'file_content': file_content,
        'csv_data': csv_data,
        'csv_headers': csv_headers,
        'can_edit': False,  # Public users can't edit
        'is_public_view': True,  # Flag to adjust breadcrumbs and navigation
        'public_breadcrumbs': file_obj.get_public_breadcrumbs(),  # Add public breadcrumbs
        'is_realm5_observation': is_realm5_observation,
        'realm5_observation_data': realm5_observation_data,
        'realm5_variables': realm5_variables,
        'is_realm5_all': is_realm5_all,
        'realm5_all_data': realm5_all_data,
        'realm5_all_variables': realm5_all_variables,
    })

@login_required
def create_folder(request):
    """Create a new folder via AJAX"""
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            folder_name = data.get('name', '').strip()
            parent_id = data.get('parent_id')
            is_public = data.get('is_public', False)

            if not folder_name:
                return JsonResponse({'error': 'Folder name is required'}, status=400)

            # Validate folder name. Rejects URL-encoded / Unicode-normalized
            # path traversal sequences (e.g. %2e%2e%2f, overlong UTF-8 like
            # À®À®À¯), control characters, NULL bytes, and pure-dot names.
            sanitized = sanitize_folder_name(folder_name)
            if not sanitized:
                return JsonResponse({'error': 'Folder name contains invalid characters'}, status=400)
            folder_name = sanitized
            
            # Get parent folder if specified
            parent_folder = None
            if parent_id:
                parent_folder = get_object_or_404(Folder, id=parent_id, owner=request.user)
            
            # Generate unique folder name if duplicate exists
            unique_folder_name = generate_unique_name(
                folder_name,
                request.user,
                folder=parent_folder,
                is_folder=True
            )
            
            # Create folder with unique name
            folder = Folder.objects.create(
                name=unique_folder_name,
                parent=parent_folder,
                owner=request.user,
                is_public=is_public
            )
            
            # Embedding generation handled automatically by post_save signal
            
            return JsonResponse({
                'success': True,
                'folder': {
                    'id': str(folder.id),
                    'name': folder.name,
                    'url': folder.get_absolute_url(),
                }
            })
            
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON data'}, status=400)
        except Exception as e:
            return _internal_error_response(e)

    return JsonResponse({'error': 'Method not allowed'}, status=405)

@login_required
def upload_files(request):
    """Upload files via AJAX"""
    if request.method == 'POST':
        try:
            files = request.FILES.getlist('files')
            folder_id = request.POST.get('folder_id')
            is_public = request.POST.get('is_public') == 'true'
            
            if not files:
                return JsonResponse({'error': 'No files selected'}, status=400)
            
            # Get folder if specified
            folder = None
            if folder_id:
                folder = get_object_or_404(Folder, id=folder_id, owner=request.user)
            
            uploaded_files = []
            rejected_count = 0
            for uploaded_file in files:
                # Validate MIME type before saving
                mime_ok, detected_mime = validate_uploaded_file_mime(uploaded_file)
                if not mime_ok:
                    logger.warning(
                        "MIME validation rejected upload: user=%s filename=%r detected_mime=%s",
                        request.user.id,
                        uploaded_file.name,
                        detected_mime,
                    )
                    rejected_count += 1
                    continue

                # Generate unique filename if duplicate exists
                unique_filename = generate_unique_name(
                    uploaded_file.name,
                    request.user,
                    folder=folder,
                    is_folder=False
                )

                # Create file object with unique name
                file_obj = File.objects.create(
                    name=unique_filename,
                    file=uploaded_file,
                    folder=folder,
                    owner=request.user,
                    is_public=is_public
                )

                # Trigger GIS processing for spatial files
                if file_obj.is_spatial:
                    process_gis_file_task.delay(str(file_obj.id))

                # Embedding generation handled automatically by post_save signal

                uploaded_files.append({
                    'id': str(file_obj.id),
                    'name': file_obj.name,
                    'size': file_obj.get_size_display(),
                    'url': file_obj.get_absolute_url(),
                    'is_spatial': file_obj.is_spatial,
                    'file_type': file_obj.file_type,
                })

            response_data = {
                'success': True,
                'files': uploaded_files,
                'message': f'Successfully uploaded {len(uploaded_files)} files',
            }
            if rejected_count:
                response_data['rejected_count'] = rejected_count
                response_data['rejected_message'] = (
                    f'{rejected_count} file(s) rejected: unsupported file type'
                )
            return JsonResponse(response_data)
            
        except Exception as e:
            return _internal_error_response(e)

    return JsonResponse({'error': 'Method not allowed'}, status=405)

@login_required
def download_file(request, file_id):
    """Download a file"""
    file_obj = get_object_or_404(File, id=file_id)
    
    # Check permissions
    if file_obj.owner != request.user and not file_obj.is_public:
        raise Http404("File not found")
    
    try:
        response = FileResponse(
            file_obj.file.open('rb'),
            as_attachment=True,
            filename=file_obj.name
        )
        return response
    except FileNotFoundError:
        raise Http404("File not found on disk")

def delete_file_complete(file_obj):
    """
    Complete deletion of a file including all dependencies:
    1. Remove from GeoServer (layer and datastore) if it's a spatial file
    2. Remove from all maps that contain this file
    3. Remove ChromaDB embeddings
    4. Remove physical file from disk
    5. Remove from PostgreSQL database
    """
    try:
        # 1. Remove from GeoServer if it's a spatial file
        if file_obj.is_spatial and file_obj.geoserver_layer_name:
            try:
                from .geoserver_manager import GeoServerManager
                geoserver = GeoServerManager()
                
                # Delete layer
                if file_obj.geoserver_layer_name:
                    workspace = file_obj.geoserver_workspace or 'adma_geo'
                    geoserver.delete_layer(workspace, file_obj.geoserver_layer_name)
                
                # Delete datastore/coverage store
                if file_obj.geoserver_datastore_name:
                    if file_obj.file_type in ['tif', 'tiff', 'geotiff', 'geotif']:
                        # For raster files, delete coverage store
                        geoserver.delete_coverage_store(workspace, file_obj.geoserver_datastore_name)
                    else:
                        # For vector files, delete datastore
                        geoserver.delete_datastore(workspace, file_obj.geoserver_datastore_name)
                        
                logger.info("Deleted GeoServer resources for file %s", file_obj.id)
            except Exception as e:
                logger.warning("Error deleting GeoServer resources for file %s: %s", file_obj.id, e)
        
        # 2. Remove from all maps (this will trigger MapLayer deletion due to CASCADE)
        # Update affected maps to remove this layer from their Layer Groups
        affected_maps = []
        for map_layer in file_obj.map_memberships.all():
            affected_maps.append(map_layer.map)
        
        # 3. Remove ChromaDB embeddings
        # ChromaDB embeddings removal - DISABLED (no longer using ChromaDB)
        # NOTE: ChromaDB and embedding service removed in favor of PostgreSQL search
        logger.debug("Skipping ChromaDB embedding cleanup for file %s (not using ChromaDB)", file_obj.id)
        
        # 4. Remove physical file from disk
        try:
            if file_obj.file and file_obj.file.path:
                import os
                if os.path.exists(file_obj.file.path):
                    os.remove(file_obj.file.path)
                    logger.info("Deleted physical file for file %s", file_obj.id)
        except Exception as e:
            logger.warning("Error deleting physical file for file %s: %s", file_obj.id, e)
        
        # 5. Remove from PostgreSQL database (this will cascade to MapLayers)
        file_obj.delete()
        logger.info("Deleted file %s from database", file_obj.id)
        
        # 6. Update affected maps' Layer Groups in GeoServer
        for map_obj in affected_maps:
            try:
                from .geoserver_manager import LayerGroupManager
                layer_group_manager = LayerGroupManager()
                layer_group_manager.update_layer_group(map_obj)
                logger.info("Updated Layer Group for map %s", map_obj.id)
            except Exception as e:
                logger.warning("Error updating Layer Group for map %s: %s", map_obj.id, e)
                
    except Exception as e:
        logger.exception("Error in delete_file_complete for file %s", file_obj.id)
        raise


def delete_folder_complete(folder_obj):
    """
    Complete recursive deletion of a folder including all dependencies:
    1. Recursively delete all files in the folder (with complete deletion)
    2. Recursively delete all subfolders
    3. Remove ChromaDB embeddings for the folder
    4. Remove from PostgreSQL database
    """
    try:
        logger.info("Starting complete deletion of folder %s", folder_obj.id)
        
        # 1. Recursively delete all files in this folder
        for file_obj in folder_obj.files.all():
            logger.debug("Deleting file %s in folder %s", file_obj.id, folder_obj.id)
            delete_file_complete(file_obj)
        
        # 2. Recursively delete all subfolders
        for subfolder in folder_obj.subfolders.all():
            logger.debug("Deleting subfolder %s in folder %s", subfolder.id, folder_obj.id)
            delete_folder_complete(subfolder)
        
        # 3. ChromaDB embeddings removal - DISABLED (no longer using ChromaDB)
        # NOTE: ChromaDB and embedding service removed in favor of PostgreSQL search
        logger.debug("Skipping ChromaDB embedding cleanup for folder %s (not using ChromaDB)", folder_obj.id)
        
        # 4. Remove from PostgreSQL database
        folder_obj.delete()
        logger.info("Deleted folder %s from database", folder_obj.id)
        
    except Exception as e:
        logger.exception("Error in delete_folder_complete for folder %s", folder_obj.id)
        raise


@login_required
def delete_item(request):
    """Delete file or folder via AJAX - async with immediate response"""
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            item_type = data.get('type')  # 'file' or 'folder'
            item_id = data.get('id')
            
            if item_type == 'file':
                item = get_object_or_404(File, id=item_id, owner=request.user)
                item_name = item.name
                
                # Mark as deletion in progress to hide from queries
                item.deletion_in_progress = True
                item.save(update_fields=['deletion_in_progress'])
                
                # Submit async deletion task
                from .tasks import delete_file_async_task
                task = delete_file_async_task.delay(str(item.id), item_name)
                
                # Return immediately for immediate UI feedback
                return JsonResponse({
                    'success': True,
                    'message': f'Deletion of {item_name} has been initiated',
                    'task_id': task.id,
                    'async': True
                })
                
            elif item_type == 'folder':
                item = get_object_or_404(Folder, id=item_id, owner=request.user)
                item_name = item.name
                
                # Mark as deletion in progress to hide from queries
                item.deletion_in_progress = True
                item.save(update_fields=['deletion_in_progress'])
                
                # Submit async deletion task
                from .tasks import delete_folder_async_task
                task = delete_folder_async_task.delay(str(item.id), item_name)
                
                # Return immediately for immediate UI feedback
                return JsonResponse({
                    'success': True,
                    'message': f'Deletion of {item_name} has been initiated',
                    'task_id': task.id,
                    'async': True
                })
            else:
                return JsonResponse({'error': 'Invalid item type'}, status=400)
            
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON data'}, status=400)
        except Exception as e:
            return _internal_error_response(e)

    return JsonResponse({'error': 'Method not allowed'}, status=405)


@login_required
def check_deletion_status(request, task_id):
    """Check the status of an async deletion task"""
    try:
        from celery.result import AsyncResult
        
        task_result = AsyncResult(task_id)
        
        ready = task_result.ready()
        successful = task_result.successful() if ready else None
        failed = task_result.failed() if ready else None
        # Never expose raw task result/exception text to the client
        result_payload = None
        if ready and successful:
            result_payload = task_result.result
        elif ready and failed:
            logger.error("Deletion task %s failed: %s", task_id, task_result.result)
            result_payload = 'Task failed (see server logs)'
        return JsonResponse({
            'task_id': task_id,
            'status': task_result.status,
            'result': result_payload,
            'ready': ready,
            'successful': successful,
            'failed': failed,
        })
    except Exception as e:
        return _internal_error_response(e)


@login_required
def map_viewer(request, file_id):
    """View GIS file on a map"""
    file_obj = get_object_or_404(File, id=file_id)
    
    # Check permissions
    if file_obj.owner != request.user and not file_obj.is_public:
        messages.error(request, "You don't have permission to view this file.")
        return redirect('filemanager:dashboard')
    
    # Check if it's a spatial file
    if not file_obj.is_spatial:
        messages.error(request, "This file is not a spatial/GIS file.")
        return redirect('filemanager:file_detail', file_id=file_id)
    
    # Get GeoServer layer info
    geoserver_info = None
    if file_obj.geoserver_layer_name and file_obj.gis_status in ['published', 'processed']:
        # Generate public-facing GeoServer URLs
        host = request.get_host()
        # Force HTTPS for adma.unl.edu domain, detect for others
        if 'adma.unl.edu' in host:
            scheme = 'https'  # Always use HTTPS for production domain
        else:
            scheme = 'https' if request.is_secure() else 'http'
        
        # Check if request is coming directly to Django dev server (not through proxy)
        # In dev, if accessing via port 8001/8000, use direct GeoServer URL
        # In production or when going through Nginx (port 80), use the proxy path
        if ':8001' in host or ':8000' in host:
            # Direct access to Django dev server - use direct GeoServer URL
            # GeoServer is accessible at localhost:8080 from the browser
            geoserver_base_url = "http://localhost:8080/geoserver"
        else:
            # Access through proxy (Nginx/Apache) - use the proxy path
            geoserver_base_url = f"{scheme}://{host}/geoserver"
        
        # Parse spatial extent if available
        spatial_extent = None
        if file_obj.spatial_extent:
            try:
                import json
                extent_data = json.loads(file_obj.spatial_extent)
                if extent_data.get('type') == 'envelope' and extent_data.get('coordinates'):
                    coords = extent_data['coordinates']
                    if len(coords) == 2 and len(coords[0]) == 2 and len(coords[1]) == 2:
                        spatial_extent = {
                            'minx': coords[0][0],
                            'miny': coords[0][1], 
                            'maxx': coords[1][0],
                            'maxy': coords[1][1]
                        }
            except (json.JSONDecodeError, KeyError, IndexError):
                pass
        
        geoserver_info = {
            'workspace': file_obj.geoserver_workspace,
            'layer_name': file_obj.geoserver_layer_name,
            'wms_url': f"{geoserver_base_url}/wms",
            'wfs_url': f"{geoserver_base_url}/wfs",
            'is_published': file_obj.gis_status == 'published',
            'spatial_extent': spatial_extent,
            'crs': getattr(file_obj, 'crs', 'EPSG:4326'),  # Default to WGS84 if not set
        }
    
    return render(request, 'filemanager/map_viewer.html', {
        'file': file_obj,
        'geoserver_info': geoserver_info,
    })

def public_map_viewer(request, file_id):
    """Public view of GIS file on a map"""
    file_obj = get_object_or_404(File, id=file_id, is_public=True, is_spatial=True)
    
    # If user is authenticated and owns this file, redirect to dashboard map view
    if request.user.is_authenticated and file_obj.owner == request.user:
        return redirect('filemanager:map_viewer', file_id=file_id)
    
    # Get GeoServer layer info
    geoserver_info = None
    if file_obj.geoserver_layer_name and file_obj.gis_status in ['published', 'processed']:
        # Generate public-facing GeoServer URLs
        host = request.get_host()
        # Force HTTPS for adma.unl.edu domain, detect for others
        if 'adma.unl.edu' in host:
            scheme = 'https'  # Always use HTTPS for production domain
        else:
            scheme = 'https' if request.is_secure() else 'http'
        
        # Check if request is coming directly to Django dev server (not through proxy)
        # In dev, if accessing via port 8001/8000, use direct GeoServer URL
        # In production or when going through Nginx (port 80), use the proxy path
        if ':8001' in host or ':8000' in host:
            # Direct access to Django dev server - use direct GeoServer URL
            # GeoServer is accessible at localhost:8080 from the browser
            geoserver_base_url = "http://localhost:8080/geoserver"
        else:
            # Access through proxy (Nginx/Apache) - use the proxy path
            geoserver_base_url = f"{scheme}://{host}/geoserver"
        
        # Parse spatial extent if available
        spatial_extent = None
        if file_obj.spatial_extent:
            try:
                import json
                extent_data = json.loads(file_obj.spatial_extent)
                if extent_data.get('type') == 'envelope' and extent_data.get('coordinates'):
                    coords = extent_data['coordinates']
                    if len(coords) == 2 and len(coords[0]) == 2 and len(coords[1]) == 2:
                        spatial_extent = {
                            'minx': coords[0][0],
                            'miny': coords[0][1], 
                            'maxx': coords[1][0],
                            'maxy': coords[1][1]
                        }
            except (json.JSONDecodeError, KeyError, IndexError):
                pass
        
        geoserver_info = {
            'workspace': file_obj.geoserver_workspace,
            'layer_name': file_obj.geoserver_layer_name,
            'wms_url': f"{geoserver_base_url}/wms",
            'wfs_url': f"{geoserver_base_url}/wfs",
            'is_published': file_obj.gis_status == 'published',
            'spatial_extent': spatial_extent,
            'crs': getattr(file_obj, 'crs', 'EPSG:4326'),  # Default to WGS84 if not set
        }
    
    # Reuse the same template as private map viewer, but with public view context
    return render(request, 'filemanager/map_viewer.html', {
        'file': file_obj,
        'geoserver_info': geoserver_info,
        'is_public_view': True,  # Flag to adjust breadcrumbs and navigation
        'public_breadcrumbs': file_obj.get_public_breadcrumbs(),  # Add public breadcrumbs
    })

@login_required
def upload_folders(request):
    """Upload folders with their structure via AJAX"""
    if request.method == 'POST':
        try:
            files = request.FILES.getlist('files')
            file_paths = request.POST.getlist('file_paths')
            is_public = request.POST.get('is_public') == 'true'
            folder_id = request.POST.get('folder_id')  # Parent folder for uploads
            
            if not files or len(files) != len(file_paths):
                return JsonResponse({'error': 'No files selected or paths mismatch'}, status=400)
            
            # Get parent folder if specified
            parent_folder = None
            if folder_id:
                parent_folder = get_object_or_404(Folder, id=folder_id, owner=request.user)
            
            # First, identify unique root folders and their target names
            root_folders = {}  # original_name -> unique_name
            file_mappings = []  # (uploaded_file, original_path, mapped_path)
            
            # Collect all root folder names and determine unique names
            for uploaded_file, file_path in zip(files, file_paths):
                path_parts = file_path.split('/')
                if len(path_parts) > 1:  # File is inside a folder
                    root_folder_name = path_parts[0]
                    if root_folder_name not in root_folders:
                        # Generate unique name for this root folder
                        unique_root_name = generate_unique_name(
                            root_folder_name,
                            request.user,
                            folder=parent_folder,  # Use parent folder
                            is_folder=True
                        )
                        root_folders[root_folder_name] = unique_root_name
                
                file_mappings.append((uploaded_file, file_path, None))  # Will set mapped_path later
            
            # Update file mappings with unique root folder names
            for i, (uploaded_file, original_path, _) in enumerate(file_mappings):
                path_parts = original_path.split('/')
                if len(path_parts) > 1:
                    root_folder = path_parts[0]
                    unique_root = root_folders[root_folder]
                    mapped_path = f"{unique_root}/{'/'.join(path_parts[1:])}"
                else:
                    mapped_path = original_path  # File at root level
                file_mappings[i] = (uploaded_file, original_path, mapped_path)
            
            # Now create the folder structure and files
            created_folders = {}
            uploaded_files = []
            
            for uploaded_file, original_path, mapped_path in file_mappings:
                # Parse the mapped path to create folder structure
                path_parts = mapped_path.split('/')
                filename = path_parts[-1]
                folder_path = path_parts[:-1]
                
                # Create folder hierarchy
                current_folder = parent_folder  # Start with parent folder
                current_path = ""
                
                for folder_name in folder_path:
                    # Validate each folder name component. See
                    # sanitize_folder_name() — rejects encoded path traversal,
                    # control chars, etc. Skip components that don't pass.
                    folder_name = sanitize_folder_name(folder_name)
                    if not folder_name:
                        continue
                    current_path = f"{current_path}/{folder_name}" if current_path else folder_name

                    if current_path not in created_folders:
                        # Create new folder (no need to check for existing since we already made names unique)
                        folder = Folder.objects.create(
                            name=folder_name,
                            parent=current_folder,
                            owner=request.user,
                            is_public=is_public
                        )
                        created_folders[current_path] = folder
                        
                        # Embedding generation handled automatically by post_save signal
                    
                    current_folder = created_folders[current_path]
                
                # Generate unique filename if duplicate exists
                unique_filename = generate_unique_name(
                    filename,
                    request.user,
                    folder=current_folder,
                    is_folder=False
                )
                
                # Create file object with unique name
                file_obj = File.objects.create(
                    name=unique_filename,
                    file=uploaded_file,
                    folder=current_folder,
                    owner=request.user,
                    is_public=is_public
                )
                
                # Trigger GIS processing for spatial files  
                if file_obj.is_spatial:
                    process_gis_file_task.delay(str(file_obj.id))
                
                # Embedding generation handled automatically by post_save signal
                
                uploaded_files.append({
                    'id': str(file_obj.id),
                    'name': file_obj.name,
                    'size': file_obj.get_size_display(),
                    'folder': current_folder.name if current_folder else None,
                    'is_spatial': file_obj.is_spatial,
                    'file_type': file_obj.file_type,
                })
            
            folders_created = len(created_folders)
            files_created = len(uploaded_files)
            
            # Check if any folders were renamed
            renamed_folders = [f"{orig} → {unique}" for orig, unique in root_folders.items() if orig != unique]
            
            # Trigger recursive embedding generation for root folders
            # This will generate embeddings for all subfolders and files recursively
            for folder_path, folder_obj in created_folders.items():
                # Only trigger for root folders (no parent or parent is the specified parent folder)
                if folder_obj.parent == parent_folder:
                    try:
                        from .tasks import generate_recursive_embeddings_task
                        generate_recursive_embeddings_task.delay(str(folder_obj.id))
                    except Exception as e:
                        import logging
                        logger = logging.getLogger(__name__)
                        logger.error(f"Failed to trigger recursive embeddings for folder {folder_obj.name}: {e}")
            
            message = f'Successfully uploaded {files_created} files in {folders_created} folders'
            if renamed_folders:
                message += f'. Renamed duplicate folders: {", ".join(renamed_folders)}'
            
            return JsonResponse({
                'success': True,
                'folders_created': folders_created,
                'files_created': files_created,
                'renamed_folders': renamed_folders,
                'message': message
            })
            
        except Exception as e:
            return _internal_error_response(e)

    return JsonResponse({'error': 'Method not allowed'}, status=405)

@login_required
def toggle_visibility(request):
    """Toggle visibility - async for folders (recursive), immediate for files"""
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            item_type = data.get('type')
            item_id = data.get('id')
            
            if item_type == 'folder':
                folder = get_object_or_404(Folder, id=item_id, owner=request.user)
                new_visibility = not folder.is_public
                
                # Update the main folder immediately for UI feedback
                folder.is_public = new_visibility
                folder.save(update_fields=['is_public'])
                
                # Submit async task for recursive update of subfolders/files
                from .tasks import toggle_folder_visibility_recursive_task
                task = toggle_folder_visibility_recursive_task.delay(
                    str(folder.id), 
                    new_visibility, 
                    folder.name
                )
                
                return JsonResponse({
                    'success': True, 
                    'is_public': folder.is_public,
                    'status': 'Public' if folder.is_public else 'Private',
                    'task_id': task.id,
                    'async': True,
                    'message': f'Visibility change for "{folder.name}" and all contents started in background'
                })
                
            elif item_type == 'file':
                file_obj = get_object_or_404(File, id=item_id, owner=request.user)
                new_visibility = not file_obj.is_public
                
                # Update the file immediately for UI feedback
                file_obj.is_public = new_visibility
                file_obj.save(update_fields=['is_public'])
                
                # Submit async task for embedding update
                from .tasks import toggle_file_visibility_task
                task = toggle_file_visibility_task.delay(
                    str(file_obj.id), 
                    new_visibility, 
                    file_obj.name
                )
                
                return JsonResponse({
                    'success': True, 
                    'is_public': file_obj.is_public,
                    'status': 'Public' if file_obj.is_public else 'Private',
                    'task_id': task.id,
                    'async': True,
                    'message': f'Visibility change for "{file_obj.name}" completed'
                })
            else:
                return JsonResponse({'success': False, 'error': 'Invalid item type'})
            
        except json.JSONDecodeError:
            return JsonResponse({'success': False, 'error': 'Invalid JSON'})
        except Exception as e:
            return _internal_error_response(e)

    return JsonResponse({'success': False, 'error': 'Method not allowed'})

@login_required
def dashboard_stats(request):
    """API endpoint to get updated dashboard statistics with robust calculation and error handling"""
    if request.method == 'GET':
        user = request.user
        
        # Use the robust statistics helper function
        stats = get_robust_user_statistics(user)
        
        # Format storage size for display
        from django.template.defaultfilters import filesizeformat
        stats['total_size_formatted'] = filesizeformat(stats['total_size'])
        
        return JsonResponse({'success': True, 'stats': stats})
    
    return JsonResponse({'success': False, 'error': 'Method not allowed'})

class RegisterView(CreateView):
    form_class = RegistrationForm
    template_name = 'registration/register.html'
    success_url = reverse_lazy('filemanager:dashboard')

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, 'Account created successfully!')
        return response

class DocumentationView(TemplateView):
    """Documentation page view"""
    template_name = 'filemanager/documentation.html'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Documentation'
        return context


class SeedingToolView(LoginRequiredMixin, TemplateView):
    """Seeding Tool page - allows users to select input file and output folder"""
    template_name = 'filemanager/seeding_tool.html'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        
        # Build hierarchical tree data structure for the file browser
        tree_data = self._build_tree_data(user)
        
        context['tree_data'] = tree_data
        context['page_title'] = 'Seeding Tool'
        
        return context
    
    def _build_tree_data(self, user):
        """Build hierarchical folder/file tree for the browser UI"""
        
        def folder_to_dict(folder, include_files=True):
            """Convert a folder to a dictionary with its contents"""
            data = {
                'id': str(folder.id),
                'name': folder.name,
                'file_count': folder.files.filter(deletion_in_progress=False).count(),
                'subfolders': [],
                'files': []
            }
            
            # Get subfolders
            subfolders = Folder.objects.filter(
                parent=folder,
                deletion_in_progress=False
            ).order_by('name')
            
            for subfolder in subfolders:
                data['subfolders'].append(folder_to_dict(subfolder, include_files))
            
            # Get files (only shapefiles for input selection)
            if include_files:
                files = File.objects.filter(
                    folder=folder,
                    deletion_in_progress=False,
                    name__iendswith='.shp'
                ).order_by('name')
                
                for file in files:
                    data['files'].append({
                        'id': str(file.id),
                        'name': file.name,
                        'size_display': file.get_size_display()
                    })
            
            return data
        
        # Get user's root-level folders (no parent)
        my_root_folders = Folder.objects.filter(
            owner=user,
            parent__isnull=True,
            deletion_in_progress=False
        ).order_by('name')
        
        # Get user's root-level files (no folder)
        my_root_files = File.objects.filter(
            owner=user,
            folder__isnull=True,
            deletion_in_progress=False,
            name__iendswith='.shp'
        ).order_by('name')
        
        # Get public root-level folders (from other users)
        public_root_folders = Folder.objects.filter(
            is_public=True,
            parent__isnull=True,
            deletion_in_progress=False
        ).exclude(owner=user).order_by('name')
        
        # Get public root-level files (from other users)
        public_root_files = File.objects.filter(
            is_public=True,
            folder__isnull=True,
            deletion_in_progress=False,
            name__iendswith='.shp'
        ).exclude(owner=user).order_by('name')
        
        # Build tree structure
        tree_data = {
            'my_folders': [folder_to_dict(f) for f in my_root_folders],
            'my_root_files': [{
                'id': str(f.id),
                'name': f.name,
                'size_display': f.get_size_display()
            } for f in my_root_files],
            'public_folders': [folder_to_dict(f) for f in public_root_folders],
            'public_root_files': [{
                'id': str(f.id),
                'name': f.name,
                'size_display': f.get_size_display()
            } for f in public_root_files]
        }
        
        return tree_data


class YieldSummaryToolView(LoginRequiredMixin, TemplateView):
    """Yield Summary Tool page - allows users to select treatment and yield files"""
    template_name = 'filemanager/yield_summary_tool.html'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        
        # Build hierarchical tree data structure for the file browser
        tree_data = self._build_tree_data(user)
        
        context['tree_data'] = tree_data
        context['page_title'] = 'Yield Summary Tool'
        
        # Default parameter values
        context['default_buffer_distance'] = -30.0
        context['default_corn_price'] = 4.35
        context['default_n_price'] = 0.50
        
        return context
    
    def _build_tree_data(self, user):
        """Build hierarchical folder/file tree for the browser UI"""
        
        def folder_to_dict(folder, include_files=True):
            """Convert a folder to a dictionary with its contents"""
            data = {
                'id': str(folder.id),
                'name': folder.name,
                'file_count': folder.files.filter(deletion_in_progress=False).count(),
                'subfolders': [],
                'files': []
            }
            
            # Get subfolders
            subfolders = Folder.objects.filter(
                parent=folder,
                deletion_in_progress=False
            ).order_by('name')
            
            for subfolder in subfolders:
                data['subfolders'].append(folder_to_dict(subfolder, include_files))
            
            # Get files (only shapefiles for input selection)
            if include_files:
                files = File.objects.filter(
                    folder=folder,
                    deletion_in_progress=False,
                    name__iendswith='.shp'
                ).order_by('name')
                
                for file in files:
                    data['files'].append({
                        'id': str(file.id),
                        'name': file.name,
                        'size_display': file.get_size_display()
                    })
            
            return data
        
        # Get user's root-level folders (no parent)
        my_root_folders = Folder.objects.filter(
            owner=user,
            parent__isnull=True,
            deletion_in_progress=False
        ).order_by('name')
        
        # Get user's root-level files (no folder)
        my_root_files = File.objects.filter(
            owner=user,
            folder__isnull=True,
            deletion_in_progress=False,
            name__iendswith='.shp'
        ).order_by('name')
        
        # Get public root-level folders (from other users)
        public_root_folders = Folder.objects.filter(
            is_public=True,
            parent__isnull=True,
            deletion_in_progress=False
        ).exclude(owner=user).order_by('name')
        
        # Get public root-level files (from other users)
        public_root_files = File.objects.filter(
            is_public=True,
            folder__isnull=True,
            deletion_in_progress=False,
            name__iendswith='.shp'
        ).exclude(owner=user).order_by('name')
        
        # Build tree structure
        tree_data = {
            'my_folders': [folder_to_dict(f) for f in my_root_folders],
            'my_root_files': [{
                'id': str(f.id),
                'name': f.name,
                'size_display': f.get_size_display()
            } for f in my_root_files],
            'public_folders': [folder_to_dict(f) for f in public_root_folders],
            'public_root_files': [{
                'id': str(f.id),
                'name': f.name,
                'size_display': f.get_size_display()
            } for f in public_root_files]
        }
        
        return tree_data


class ToolsListView(LoginRequiredMixin, TemplateView):
    """List view for available tools with panel/list view toggle"""
    template_name = 'filemanager/tools_list.html'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        
        # View mode (panel or list)
        context['view_mode'] = self.request.GET.get('view', 'panel')
        context['page_title'] = 'Tools'
        
        # Get tools from database
        available_tools = Tool.get_available_tools_for_user(self.request.user)
        
        # Separate available tools from coming soon tools
        my_tools_qs = available_tools.filter(status='available')
        coming_soon_tools_qs = available_tools.filter(status='coming_soon')
        
        # Convert to list of dicts for template compatibility
        # This maintains backward compatibility with the existing template
        my_tools = []
        for tool in my_tools_qs:
            my_tools.append({
                'id': str(tool.id),
                'slug': tool.slug,
                'name': tool.name,
                'description': tool.short_description or tool.description,
                'icon': tool.icon,
                'color': tool.icon_color,
                'url_name': tool.url_name,
                'status': tool.status,
                'category': tool.get_category_display(),
                'version': tool.version,
                'usage_count': tool.usage_count,
                'is_system_tool': tool.is_system_tool,
                'owner': tool.owner.username if tool.owner else None,
            })
        
        coming_soon_tools = []
        for tool in coming_soon_tools_qs:
            coming_soon_tools.append({
                'id': str(tool.id),
                'slug': tool.slug,
                'name': tool.name,
                'description': tool.short_description or tool.description,
                'icon': tool.icon,
                'color': tool.icon_color,
                'url_name': tool.url_name,
                'status': tool.status,
                'category': tool.get_category_display(),
                'version': tool.version,
                'is_system_tool': tool.is_system_tool,
                'owner': tool.owner.username if tool.owner else None,
            })
        
        # Fallback: If no tools in database, show hardcoded tools (for backwards compatibility during migration)
        if not my_tools and not Tool.objects.filter(is_system_tool=True).exists():
            my_tools = [
                {
                    'id': 'seeding_tool',
                    'slug': 'seeding-tool',
                    'name': 'Seeding Tool',
                    'description': 'Process point shapefiles to create seeding polygons, boundaries, and summary statistics.',
                    'icon': 'fa-seedling',
                    'color': 'warning',
                    'url_name': 'filemanager:seeding_tool',
                    'status': 'available',
                    'category': 'GIS Processing',
                },
                {
                    'id': 'shape_to_json',
                    'slug': 'shape-to-json',
                    'name': 'Shape to JSON',
                    'description': 'Convert shapefiles to GeoJSON format for web mapping applications.',
                    'icon': 'fa-exchange-alt',
                    'color': 'primary',
                    'url_name': 'filemanager:shape_to_json_tool',
                    'status': 'available',
                    'category': 'Format Conversion',
                },
                {
                    'id': 'si_tool',
                    'slug': 'si-tool',
                    'name': 'SI Tool',
                    'description': 'Calculate Stress Index values from NDRE and buffer sector data.',
                    'icon': 'fa-chart-line',
                    'color': 'success',
                    'url_name': 'filemanager:si_tool',
                    'status': 'available',
                    'category': 'Analysis',
                },
                {
                    'id': 'yield_summary_tool',
                    'slug': 'yield-summary',
                    'name': 'Yield Summary',
                    'description': 'Analyze treatment sectors and yield data with ANOVA statistics and economic metrics.',
                    'icon': 'fa-chart-bar',
                    'color': 'info',
                    'url_name': 'filemanager:yield_summary_tool',
                    'status': 'available',
                    'category': 'Analysis',
                },
            ]
            coming_soon_tools = [
                {
                    'id': 'layer_merge',
                    'slug': 'layer-merge',
                    'name': 'Layer Merge',
                    'description': 'Merge multiple GIS layers into a single file.',
                    'icon': 'fa-layer-group',
                    'color': 'secondary',
                    'url_name': None,
                    'status': 'coming_soon',
                    'category': 'GIS Processing',
                },
            ]
        
        context['my_tools'] = my_tools
        context['coming_soon_tools'] = coming_soon_tools
        context['total_available'] = len(my_tools)
        context['total_coming_soon'] = len(coming_soon_tools)
        
        return context


class ShapeToJsonToolView(LoginRequiredMixin, TemplateView):
    """Shape to JSON Tool page - converts shapefiles to GeoJSON format"""
    template_name = 'filemanager/shape_to_json_tool.html'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        
        # Build hierarchical tree data structure for the file browser
        tree_data = self._build_tree_data(user)
        
        context['tree_data'] = tree_data
        context['page_title'] = 'Shape to JSON Tool'
        
        return context
    
    def _build_tree_data(self, user):
        """Build hierarchical folder/file tree for the browser UI"""
        
        def folder_to_dict(folder, include_files=True):
            """Convert a folder to a dictionary with its contents"""
            data = {
                'id': str(folder.id),
                'name': folder.name,
                'file_count': folder.files.filter(deletion_in_progress=False).count(),
                'subfolders': [],
                'files': []
            }
            
            # Get subfolders
            subfolders = Folder.objects.filter(
                parent=folder,
                deletion_in_progress=False
            ).order_by('name')
            
            for subfolder in subfolders:
                data['subfolders'].append(folder_to_dict(subfolder, include_files))
            
            # Get files (only shapefiles for input selection)
            if include_files:
                files = File.objects.filter(
                    folder=folder,
                    deletion_in_progress=False,
                    name__iendswith='.shp'
                ).order_by('name')
                
                for file in files:
                    data['files'].append({
                        'id': str(file.id),
                        'name': file.name,
                        'size_display': file.get_size_display()
                    })
            
            return data
        
        # Get user's root-level folders (no parent)
        my_root_folders = Folder.objects.filter(
            owner=user,
            parent__isnull=True,
            deletion_in_progress=False
        ).order_by('name')
        
        # Get user's root-level files (no folder)
        my_root_files = File.objects.filter(
            owner=user,
            folder__isnull=True,
            deletion_in_progress=False,
            name__iendswith='.shp'
        ).order_by('name')
        
        # Get public root-level folders (from other users)
        public_root_folders = Folder.objects.filter(
            is_public=True,
            parent__isnull=True,
            deletion_in_progress=False
        ).exclude(owner=user).order_by('name')
        
        # Get public root-level files (from other users)
        public_root_files = File.objects.filter(
            is_public=True,
            folder__isnull=True,
            deletion_in_progress=False,
            name__iendswith='.shp'
        ).exclude(owner=user).order_by('name')
        
        # Build tree structure
        tree_data = {
            'my_folders': [folder_to_dict(f) for f in my_root_folders],
            'my_root_files': [{
                'id': str(f.id),
                'name': f.name,
                'size_display': f.get_size_display()
            } for f in my_root_files],
            'public_folders': [folder_to_dict(f) for f in public_root_folders],
            'public_root_files': [{
                'id': str(f.id),
                'name': f.name,
                'size_display': f.get_size_display()
            } for f in public_root_files]
        }
        
        return tree_data


class SIToolView(LoginRequiredMixin, TemplateView):
    """SI (Stress Index) Tool page - calculates SI values from buffer sectors and NDRE data"""
    template_name = 'filemanager/si_tool.html'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        
        # Build hierarchical tree data structure for the file browsers
        # We need separate trees for shapefiles, CSV files, and TIF files
        shp_tree_data = self._build_tree_data(user, file_extensions=['.shp'])
        csv_tree_data = self._build_tree_data(user, file_extensions=['.csv'])
        tif_tree_data = self._build_tree_data(user, file_extensions=['.tif', '.tiff'])
        folder_tree_data = self._build_folder_tree(user)

        context['shp_tree_data'] = shp_tree_data
        context['csv_tree_data'] = csv_tree_data
        context['tif_tree_data'] = tif_tree_data
        context['folder_tree_data'] = folder_tree_data
        context['page_title'] = 'SI Tool'
        
        return context
    
    def _build_tree_data(self, user, file_extensions):
        """Build hierarchical folder/file tree for specific file extensions"""
        
        def folder_to_dict(folder, include_files=True):
            """Convert a folder to a dictionary with its contents"""
            data = {
                'id': str(folder.id),
                'name': folder.name,
                'file_count': folder.files.filter(deletion_in_progress=False).count(),
                'subfolders': [],
                'files': []
            }
            
            # Get subfolders
            subfolders = Folder.objects.filter(
                parent=folder,
                deletion_in_progress=False
            ).order_by('name')
            
            for subfolder in subfolders:
                data['subfolders'].append(folder_to_dict(subfolder, include_files))
            
            # Get files matching extensions
            if include_files:
                from django.db.models import Q
                q_filter = Q()
                for ext in file_extensions:
                    q_filter |= Q(name__iendswith=ext)
                
                files = File.objects.filter(
                    q_filter,
                    folder=folder,
                    deletion_in_progress=False
                ).order_by('name')
                
                for file in files:
                    data['files'].append({
                        'id': str(file.id),
                        'name': file.name,
                        'size_display': file.get_size_display()
                    })
            
            return data
        
        # Get user's root-level folders
        my_root_folders = Folder.objects.filter(
            owner=user,
            parent__isnull=True,
            deletion_in_progress=False
        ).order_by('name')
        
        # Get user's root-level files matching extensions
        from django.db.models import Q
        q_filter = Q()
        for ext in file_extensions:
            q_filter |= Q(name__iendswith=ext)
        
        my_root_files = File.objects.filter(
            q_filter,
            owner=user,
            folder__isnull=True,
            deletion_in_progress=False
        ).order_by('name')
        
        # Get public root-level folders
        public_root_folders = Folder.objects.filter(
            is_public=True,
            parent__isnull=True,
            deletion_in_progress=False
        ).exclude(owner=user).order_by('name')
        
        # Get public root-level files matching extensions
        public_root_files = File.objects.filter(
            q_filter,
            is_public=True,
            folder__isnull=True,
            deletion_in_progress=False
        ).exclude(owner=user).order_by('name')
        
        # Build tree structure
        tree_data = {
            'my_folders': [folder_to_dict(f) for f in my_root_folders],
            'my_root_files': [{
                'id': str(f.id),
                'name': f.name,
                'size_display': f.get_size_display()
            } for f in my_root_files],
            'public_folders': [folder_to_dict(f) for f in public_root_folders],
            'public_root_files': [{
                'id': str(f.id),
                'name': f.name,
                'size_display': f.get_size_display()
            } for f in public_root_files]
        }
        
        return tree_data
    
    def _build_folder_tree(self, user):
        """Build folder-only tree for output folder selection"""
        
        def folder_to_dict(folder):
            data = {
                'id': str(folder.id),
                'name': folder.name,
                'subfolders': []
            }
            
            subfolders = Folder.objects.filter(
                parent=folder,
                deletion_in_progress=False
            ).order_by('name')
            
            for subfolder in subfolders:
                data['subfolders'].append(folder_to_dict(subfolder))
            
            return data
        
        my_root_folders = Folder.objects.filter(
            owner=user,
            parent__isnull=True,
            deletion_in_progress=False
        ).order_by('name')
        
        return {
            'my_folders': [folder_to_dict(f) for f in my_root_folders]
        }


class SearchView(TemplateView):
    """PostgreSQL-based search page with text matching and metadata filtering"""
    template_name = 'filemanager/search.html'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Search'
        
        # Get search parameters
        query = self.request.GET.get('q', '').strip()
        if not query:
            query = self.request.GET.get('prompt', '').strip()
        
        content_type = self.request.GET.get('type', '')  # 'file', 'folder', 'map', or ''
        file_type = self.request.GET.get('file_type', '')
        is_public_str = self.request.GET.get('is_public', '')
        page = self.request.GET.get('page', 1)
        
        # Convert is_public to boolean or None
        is_public = None
        if is_public_str.lower() == 'true':
            is_public = True
        elif is_public_str.lower() == 'false':
            is_public = False
        
        context.update({
            'query': query,
            'content_type': content_type,
            'file_type': file_type,
            'is_public': is_public_str,
        })
        
        # Initialize results
        search_results = []
        total_results = 0
        
        # Check if we should perform a search
        has_query = bool(query)
        has_filters = any([content_type, file_type, is_public_str])
        
        if has_query or has_filters:
            try:
                # Use the new search engine
                from .search_engine import search_engine
                
                # Perform search
                raw_results = search_engine.search(
                    user=self.request.user,
                    query=query if has_query else None,
                    content_type=content_type if content_type else None,
                    is_public=is_public,
                    file_type=file_type if file_type else None,
                    limit=1000  # Get more results for pagination
                )
                
                # Convert results to template format
                for result in raw_results:
                    search_results.append({
                        'object': result['object'],
                        'type': result['type'],
                        'relevance': result.get('relevance', 1.0),
                        'metadata_text': result['name']
                    })
                
                total_results = len(search_results)
                
            except Exception as e:
                # Handle search errors
                import logging
                logger = logging.getLogger(__name__)
                logger.error(f"Search error: {e}")
                context['search_error'] = "Search temporarily unavailable due to technical issues."
        
        # Pagination
        from django.core.paginator import Paginator
        paginator = Paginator(search_results, 100)  # 100 results per page
        
        try:
            page_number = int(page)
        except (ValueError, TypeError):
            page_number = 1
        
        page_obj = paginator.get_page(page_number)
        
        context.update({
            'search_results': page_obj.object_list,
            'page_obj': page_obj,
            'total_results': total_results,
            'has_results': bool(search_results),
            'has_search': has_query or has_filters,  # Template expects this variable
        })
        
        return context

@login_required
def search_api(request):
    """API endpoint for search suggestions using the new search engine"""
    if request.method == 'GET':
        query = request.GET.get('q', '').strip()
        limit = min(int(request.GET.get('limit', 10)), 50)  # Max 50 suggestions
        
        if not query or len(query) < 2:
            return JsonResponse({'suggestions': []})
        
        try:
            # Use the new search engine
            from .search_engine import search_engine
            
            # Perform search (authenticated users only due to @login_required)
            results = search_engine.search(
                user=request.user,
                query=query,
                content_type=None,  # All types
                is_public=None,     # All visibility
                file_type=None,     # All file types
                limit=limit
            )
            
            # Format suggestions
            suggestions = []
            for result in results:
                try:
                    obj = result['object']
                    obj_type = result['type']
                    
                    # Get appropriate icon
                    if obj_type == 'file':
                        icon = getattr(obj, 'get_icon_class', lambda: 'fas fa-file')()
                    elif obj_type == 'folder':
                        icon = 'fas fa-folder'
                    elif obj_type == 'map':
                        icon = 'fas fa-map'
                    else:
                        icon = 'fas fa-file'
                    
                    suggestions.append({
                        'id': result['id'],
                        'type': obj_type,
                        'name': obj.name,
                        'icon': icon,
                        'url': obj.get_absolute_url(),
                        'is_public': getattr(obj, 'is_public', False),
                        'relevance': result.get('relevance', 1.0)
                    })
                except Exception as e:
                    # Log and skip problematic items
                    import logging
                    logger = logging.getLogger(__name__)
                    logger.warning(f"Error processing search suggestion: {e}")
                    continue
            
            return JsonResponse({'suggestions': suggestions})
            
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Search API error: {e}")
            return JsonResponse({'suggestions': [], 'error': 'Internal search error'})
    
    return JsonResponse({'error': 'Method not allowed'}, status=405)


@login_required
def run_seeding_tool(request):
    """
    API endpoint to trigger the Seeding Tool on a .shp or .gpkg file.
    
    POST request with JSON body:
    {
        "file_id": "uuid-of-file",
        "output_folder_id": "uuid-of-folder" (optional)
    }
    
    Returns:
    {
        "success": true/false,
        "message": "...",
        "task_id": "celery-task-id" (if async)
    }
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed'}, status=405)
    
    try:
        data = json.loads(request.body)
        file_id = data.get('file_id')
        output_folder_id = data.get('output_folder_id')  # Optional
        
        if not file_id:
            return JsonResponse({'success': False, 'error': 'file_id is required'}, status=400)
        
        # Get the file object
        try:
            file_obj = File.objects.get(id=file_id)
        except File.DoesNotExist:
            return JsonResponse({'success': False, 'error': 'File not found'}, status=404)
        
        # Check permissions - user must be owner OR file must be public
        if file_obj.owner != request.user and not file_obj.is_public:
            return JsonResponse({'success': False, 'error': 'Permission denied'}, status=403)
        
        # Validate output folder if provided
        if output_folder_id:
            try:
                output_folder = Folder.objects.get(id=output_folder_id, owner=request.user)
            except Folder.DoesNotExist:
                return JsonResponse({'success': False, 'error': 'Output folder not found'}, status=404)
        
        # Validate file type
        file_ext = os.path.splitext(file_obj.name)[1].lower()
        if file_ext not in ['.shp', '.gpkg']:
            return JsonResponse({
                'success': False, 
                'error': f'Seeding Tool only supports .shp and .gpkg files. Got: {file_ext}'
            }, status=400)
        
        # Trigger the Celery task with optional output_folder_id
        from .tasks import run_seeding_tool_task
        task = run_seeding_tool_task.delay(str(file_id), output_folder_id)
        
        return JsonResponse({
            'success': True,
            'message': f'Seeding Tool started for {file_obj.name}',
            'task_id': task.id
        })
        
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        return _internal_error_response(e)


@login_required
def check_seeding_tool_status(request, task_id):
    """
    Check the status of a Seeding Tool task.
    
    GET request returns:
    {
        "success": true,
        "status": "PENDING" | "STARTED" | "SUCCESS" | "FAILURE",
        "result": {...} (if completed)
    }
    """
    try:
        from celery.result import AsyncResult
        
        result = AsyncResult(task_id)
        
        response = {
            'success': True,
            'status': result.status,
        }
        
        if result.ready():
            if result.successful():
                response['result'] = result.result
            else:
                logger.error("Task %s failed: %s", task_id, result.result)
                response['error'] = 'Task failed (see server logs for details)'
        
        return JsonResponse(response)
        
    except Exception as e:
        return _internal_error_response(e)


@login_required
def run_shape_to_json(request):
    """
    API endpoint to trigger the Shape to JSON Tool on a .shp file.
    
    POST request with JSON body:
    {
        "file_id": "uuid-of-file",
        "output_folder_id": "uuid-of-folder" (optional)
    }
    
    Returns:
    {
        "success": true/false,
        "message": "...",
        "task_id": "celery-task-id" (if async)
    }
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed'}, status=405)
    
    try:
        data = json.loads(request.body)
        file_id = data.get('file_id')
        output_folder_id = data.get('output_folder_id')  # Optional
        
        if not file_id:
            return JsonResponse({'success': False, 'error': 'file_id is required'}, status=400)
        
        # Get the file object
        try:
            file_obj = File.objects.get(id=file_id)
        except File.DoesNotExist:
            return JsonResponse({'success': False, 'error': 'File not found'}, status=404)
        
        # Check permissions - user must be owner OR file must be public
        if file_obj.owner != request.user and not file_obj.is_public:
            return JsonResponse({'success': False, 'error': 'Permission denied'}, status=403)
        
        # Validate output folder if provided
        if output_folder_id:
            try:
                output_folder = Folder.objects.get(id=output_folder_id, owner=request.user)
            except Folder.DoesNotExist:
                return JsonResponse({'success': False, 'error': 'Output folder not found'}, status=404)
        
        # Validate file type
        file_ext = os.path.splitext(file_obj.name)[1].lower()
        if file_ext != '.shp':
            return JsonResponse({
                'success': False, 
                'error': f'Shape to JSON only supports .shp files. Got: {file_ext}'
            }, status=400)
        
        # Trigger the Celery task with optional output_folder_id
        from .tasks import run_shape_to_json_task
        task = run_shape_to_json_task.delay(str(file_id), output_folder_id)
        
        return JsonResponse({
            'success': True,
            'message': f'Shape to JSON started for {file_obj.name}',
            'task_id': task.id
        })
        
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        return _internal_error_response(e)


@login_required
def check_shape_to_json_status(request, task_id):
    """
    Check the status of a Shape to JSON task.
    
    GET request returns:
    {
        "success": true,
        "status": "PENDING" | "STARTED" | "SUCCESS" | "FAILURE",
        "result": {...} (if completed)
    }
    """
    try:
        from celery.result import AsyncResult
        
        result = AsyncResult(task_id)
        
        response = {
            'success': True,
            'status': result.status,
        }
        
        if result.ready():
            if result.successful():
                response['result'] = result.result
            else:
                logger.error("Task %s failed: %s", task_id, result.result)
                response['error'] = 'Task failed (see server logs for details)'
        
        return JsonResponse(response)
        
    except Exception as e:
        return _internal_error_response(e)


@login_required
def run_si_tool(request):
    """
    API endpoint to trigger the SI (Sufficiency Index) Tool v2.

    POST request with JSON body:
    {
        "workflow": "standard_uav" | "standard_satellite" | "sbf_uav" | "sbf_satellite",
        "buffer_shp_id": "uuid",
        "csv_file_id": "uuid",
        "ndre_shp_id": "uuid" (UAV workflows),
        "nir_tif_id": "uuid" (SATELLITE workflows),
        "rededge_tif_id": "uuid" (SATELLITE workflows),
        "indicator_shp_id": "uuid" (SBF workflows),
        "field_column": "Plot_Numbe" (STANDARD workflows),
        "si_column_name": "SI" (optional, default "SI"),
        "output_folder_id": "uuid" (optional)
    }
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed'}, status=405)

    try:
        data = json.loads(request.body)

        workflow = data.get('workflow', '').strip()
        buffer_shp_id = data.get('buffer_shp_id')
        csv_file_id = data.get('csv_file_id')
        ndre_shp_id = data.get('ndre_shp_id')
        nir_tif_id = data.get('nir_tif_id')
        rededge_tif_id = data.get('rededge_tif_id')
        indicator_shp_id = data.get('indicator_shp_id')
        field_column = data.get('field_column', '').strip()
        si_column_name = data.get('si_column_name', 'SI').strip()
        output_folder_id = data.get('output_folder_id')

        valid_workflows = ['standard_uav', 'standard_satellite', 'sbf_uav', 'sbf_satellite']
        if workflow not in valid_workflows:
            return JsonResponse({'success': False, 'error': f'Workflow must be one of: {valid_workflows}'}, status=400)

        if not buffer_shp_id:
            return JsonResponse({'success': False, 'error': 'Buffer sectors shapefile is required'}, status=400)
        if not csv_file_id:
            return JsonResponse({'success': False, 'error': 'CSV file is required'}, status=400)
        if not si_column_name:
            return JsonResponse({'success': False, 'error': 'SI column name is required'}, status=400)

        # Validate buffer shapefile
        try:
            buffer_file = File.objects.get(id=buffer_shp_id)
            if buffer_file.owner != request.user and not buffer_file.is_public:
                return JsonResponse({'success': False, 'error': 'Permission denied for buffer sectors file'}, status=403)
        except File.DoesNotExist:
            return JsonResponse({'success': False, 'error': 'Buffer sectors file not found'}, status=404)

        # Validate CSV file
        try:
            csv_file = File.objects.get(id=csv_file_id)
            if csv_file.owner != request.user and not csv_file.is_public:
                return JsonResponse({'success': False, 'error': 'Permission denied for CSV file'}, status=403)
        except File.DoesNotExist:
            return JsonResponse({'success': False, 'error': 'CSV file not found'}, status=404)

        # Workflow-specific validation
        if workflow in ('standard_uav', 'standard_satellite'):
            if not field_column:
                return JsonResponse({'success': False, 'error': 'Field column is required for STANDARD workflows'}, status=400)

        if workflow in ('standard_uav', 'sbf_uav'):
            if not ndre_shp_id:
                return JsonResponse({'success': False, 'error': 'NDRE shapefile is required for UAV workflows'}, status=400)
            try:
                ndre_file = File.objects.get(id=ndre_shp_id)
                if ndre_file.owner != request.user and not ndre_file.is_public:
                    return JsonResponse({'success': False, 'error': 'Permission denied for NDRE file'}, status=403)
            except File.DoesNotExist:
                return JsonResponse({'success': False, 'error': 'NDRE file not found'}, status=404)

        if workflow in ('standard_satellite', 'sbf_satellite'):
            if not nir_tif_id:
                return JsonResponse({'success': False, 'error': 'NIR GeoTIFF is required for Satellite workflows'}, status=400)
            if not rededge_tif_id:
                return JsonResponse({'success': False, 'error': 'RedEdge GeoTIFF is required for Satellite workflows'}, status=400)
            try:
                nir_file = File.objects.get(id=nir_tif_id)
                if nir_file.owner != request.user and not nir_file.is_public:
                    return JsonResponse({'success': False, 'error': 'Permission denied for NIR file'}, status=403)
            except File.DoesNotExist:
                return JsonResponse({'success': False, 'error': 'NIR GeoTIFF file not found'}, status=404)
            try:
                rededge_file = File.objects.get(id=rededge_tif_id)
                if rededge_file.owner != request.user and not rededge_file.is_public:
                    return JsonResponse({'success': False, 'error': 'Permission denied for RedEdge file'}, status=403)
            except File.DoesNotExist:
                return JsonResponse({'success': False, 'error': 'RedEdge GeoTIFF file not found'}, status=404)

        if workflow in ('sbf_uav', 'sbf_satellite'):
            if not indicator_shp_id:
                return JsonResponse({'success': False, 'error': 'Indicator Block shapefile is required for SBF workflows'}, status=400)
            try:
                indicator_file = File.objects.get(id=indicator_shp_id)
                if indicator_file.owner != request.user and not indicator_file.is_public:
                    return JsonResponse({'success': False, 'error': 'Permission denied for Indicator Block file'}, status=403)
            except File.DoesNotExist:
                return JsonResponse({'success': False, 'error': 'Indicator Block file not found'}, status=404)

        # Validate output folder if provided
        if output_folder_id:
            try:
                output_folder = Folder.objects.get(id=output_folder_id, owner=request.user)
            except Folder.DoesNotExist:
                return JsonResponse({'success': False, 'error': 'Output folder not found'}, status=404)

        # Trigger the Celery task
        from .tasks import run_si_tool_task
        task = run_si_tool_task.delay(
            workflow=workflow,
            buffer_shp_id=str(buffer_shp_id),
            csv_file_id=str(csv_file_id),
            ndre_shp_id=str(ndre_shp_id) if ndre_shp_id else None,
            nir_tif_id=str(nir_tif_id) if nir_tif_id else None,
            rededge_tif_id=str(rededge_tif_id) if rededge_tif_id else None,
            indicator_shp_id=str(indicator_shp_id) if indicator_shp_id else None,
            field_column=field_column if field_column else None,
            si_column_name=si_column_name,
            output_folder_id=str(output_folder_id) if output_folder_id else None,
        )

        return JsonResponse({
            'success': True,
            'message': f'SI Tool ({workflow}) started',
            'task_id': task.id
        })

    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        return _internal_error_response(e)


@login_required
def check_si_tool_status(request, task_id):
    """
    Check the status of an SI Tool task.
    
    GET request returns:
    {
        "success": true,
        "status": "PENDING" | "STARTED" | "SUCCESS" | "FAILURE",
        "result": {...} (if completed)
    }
    """
    try:
        from celery.result import AsyncResult
        
        result = AsyncResult(task_id)
        
        response = {
            'success': True,
            'status': result.status,
        }
        
        if result.ready():
            if result.successful():
                response['result'] = result.result
            else:
                logger.error("Task %s failed: %s", task_id, result.result)
                response['error'] = 'Task failed (see server logs for details)'
        
        return JsonResponse(response)
        
    except Exception as e:
        return _internal_error_response(e)


@login_required
def run_yield_summary_tool(request):
    """
    API endpoint to trigger the Yield Summary Tool on treatment and yield shapefiles.
    
    POST request with JSON body:
    {
        "treatment_file_id": "uuid-of-treatment-file",
        "yield_file_id": "uuid-of-yield-file",
        "total_n_values": "122,99.5,122,98.5,105.5,122,122,65.5",
        "output_folder_id": "uuid-of-folder" (optional),
        "buffer_distance": -30.0 (optional, default -30),
        "corn_price": 4.35 (optional, default 4.35),
        "n_price": 0.50 (optional, default 0.50)
    }
    
    Returns:
    {
        "success": true/false,
        "message": "...",
        "task_id": "celery-task-id" (if async)
    }
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed'}, status=405)
    
    try:
        data = json.loads(request.body)
        treatment_file_id = data.get('treatment_file_id')
        yield_file_id = data.get('yield_file_id')
        total_n_values = data.get('total_n_values', '').strip()
        output_folder_id = data.get('output_folder_id')
        buffer_distance = data.get('buffer_distance', -30.0)
        corn_price = data.get('corn_price', 4.35)
        n_price = data.get('n_price', 0.50)
        
        if not treatment_file_id:
            return JsonResponse({'success': False, 'error': 'treatment_file_id is required'}, status=400)
        
        if not yield_file_id:
            return JsonResponse({'success': False, 'error': 'yield_file_id is required'}, status=400)
        
        if not total_n_values:
            return JsonResponse({'success': False, 'error': 'total_n_values is required'}, status=400)
        
        # Get the treatment file object
        try:
            treatment_file = File.objects.get(id=treatment_file_id)
        except File.DoesNotExist:
            return JsonResponse({'success': False, 'error': 'Treatment file not found'}, status=404)
        
        # Get the yield file object
        try:
            yield_file = File.objects.get(id=yield_file_id)
        except File.DoesNotExist:
            return JsonResponse({'success': False, 'error': 'Yield file not found'}, status=404)
        
        # Check permissions
        if treatment_file.owner != request.user and not treatment_file.is_public:
            return JsonResponse({'success': False, 'error': 'Permission denied for treatment file'}, status=403)
        
        if yield_file.owner != request.user and not yield_file.is_public:
            return JsonResponse({'success': False, 'error': 'Permission denied for yield file'}, status=403)
        
        # Validate output folder if provided
        if output_folder_id:
            try:
                output_folder = Folder.objects.get(id=output_folder_id, owner=request.user)
            except Folder.DoesNotExist:
                return JsonResponse({'success': False, 'error': 'Output folder not found'}, status=404)
        
        # Validate file types
        treatment_ext = os.path.splitext(treatment_file.name)[1].lower()
        yield_ext = os.path.splitext(yield_file.name)[1].lower()
        
        if treatment_ext != '.shp':
            return JsonResponse({
                'success': False, 
                'error': f'Treatment file must be a .shp file. Got: {treatment_ext}'
            }, status=400)
        
        if yield_ext != '.shp':
            return JsonResponse({
                'success': False, 
                'error': f'Yield file must be a .shp file. Got: {yield_ext}'
            }, status=400)
        
        # Trigger the Celery task
        from .tasks import run_yield_summary_tool_task
        task = run_yield_summary_tool_task.delay(
            str(treatment_file_id),
            str(yield_file_id),
            total_n_values,
            output_folder_id,
            buffer_distance,
            corn_price,
            n_price
        )
        
        return JsonResponse({
            'success': True,
            'message': f'Yield Summary Tool started for {treatment_file.name} and {yield_file.name}',
            'task_id': task.id
        })
        
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        return _internal_error_response(e)


@login_required
def check_yield_summary_tool_status(request, task_id):
    """
    Check the status of a Yield Summary Tool task.
    
    GET request returns:
    {
        "success": true,
        "status": "PENDING" | "STARTED" | "SUCCESS" | "FAILURE",
        "result": {...} (if completed)
    }
    """
    try:
        from celery.result import AsyncResult
        
        result = AsyncResult(task_id)
        
        response = {
            'success': True,
            'status': result.status,
        }
        
        if result.ready():
            if result.successful():
                response['result'] = result.result
            else:
                logger.error("Task %s failed: %s", task_id, result.result)
                response['error'] = 'Task failed (see server logs for details)'
        
        return JsonResponse(response)
        
    except Exception as e:
        return _internal_error_response(e)


class ValidYieldExtractorToolView(LoginRequiredMixin, TemplateView):
    """Valid Yield Extractor Tool page - allows users to select plots, as-applied, and harvest files"""
    template_name = 'filemanager/valid_yield_extractor_tool.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user

        # Build hierarchical tree data structure for the file browser
        tree_data = self._build_tree_data(user)

        context['tree_data'] = tree_data
        context['page_title'] = 'Valid Yield Extractor'

        # Default parameter values
        context['default_crs'] = 'EPSG:26914'
        context['default_rate_tolerance'] = 0.10

        return context

    def _build_tree_data(self, user):
        """Build hierarchical folder/file tree for the browser UI"""

        def folder_to_dict(folder, include_files=True):
            """Convert a folder to a dictionary with its contents"""
            data = {
                'id': str(folder.id),
                'name': folder.name,
                'file_count': folder.files.filter(deletion_in_progress=False).count(),
                'subfolders': [],
                'files': []
            }

            # Get subfolders
            subfolders = Folder.objects.filter(
                parent=folder,
                deletion_in_progress=False
            ).order_by('name')

            for subfolder in subfolders:
                data['subfolders'].append(folder_to_dict(subfolder, include_files))

            # Get files (only shapefiles for input selection)
            if include_files:
                files = File.objects.filter(
                    folder=folder,
                    deletion_in_progress=False,
                    name__iendswith='.shp'
                ).order_by('name')

                for file in files:
                    data['files'].append({
                        'id': str(file.id),
                        'name': file.name,
                        'size_display': file.get_size_display()
                    })

            return data

        # Get user's root-level folders (no parent)
        my_root_folders = Folder.objects.filter(
            owner=user,
            parent__isnull=True,
            deletion_in_progress=False
        ).order_by('name')

        # Get user's root-level files (no folder)
        my_root_files = File.objects.filter(
            owner=user,
            folder__isnull=True,
            deletion_in_progress=False,
            name__iendswith='.shp'
        ).order_by('name')

        # Get public root-level folders (from other users)
        public_root_folders = Folder.objects.filter(
            is_public=True,
            parent__isnull=True,
            deletion_in_progress=False
        ).exclude(owner=user).order_by('name')

        # Get public root-level files (from other users)
        public_root_files = File.objects.filter(
            is_public=True,
            folder__isnull=True,
            deletion_in_progress=False,
            name__iendswith='.shp'
        ).exclude(owner=user).order_by('name')

        # Build tree structure
        tree_data = {
            'my_folders': [folder_to_dict(f) for f in my_root_folders],
            'my_root_files': [{
                'id': str(f.id),
                'name': f.name,
                'size_display': f.get_size_display()
            } for f in my_root_files],
            'public_folders': [folder_to_dict(f) for f in public_root_folders],
            'public_root_files': [{
                'id': str(f.id),
                'name': f.name,
                'size_display': f.get_size_display()
            } for f in public_root_files]
        }

        return tree_data


@login_required
def run_valid_yield_extractor(request):
    """
    API endpoint to trigger the Valid Yield Extractor Tool on plots, as-applied, and harvest shapefiles.

    POST request with JSON body:
    {
        "plots_file_id": "uuid-of-plots-file",
        "app_file_id": "uuid-of-app-file",
        "harv_file_id": "uuid-of-harv-file",
        "output_folder_id": "uuid-of-folder" (optional),
        "crs": "EPSG:26914" (optional),
        "rate_tolerance": 0.10 (optional)
    }

    Returns:
    {
        "success": true/false,
        "message": "...",
        "task_id": "celery-task-id" (if async)
    }
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed'}, status=405)

    try:
        data = json.loads(request.body)
        plots_file_id = data.get('plots_file_id')
        app_file_id = data.get('app_file_id')
        harv_file_id = data.get('harv_file_id')
        output_folder_id = data.get('output_folder_id')
        crs = data.get('crs', 'EPSG:26914')
        rate_tolerance = data.get('rate_tolerance', 0.10)

        if not plots_file_id:
            return JsonResponse({'success': False, 'error': 'plots_file_id is required'}, status=400)

        if not app_file_id:
            return JsonResponse({'success': False, 'error': 'app_file_id is required'}, status=400)

        if not harv_file_id:
            return JsonResponse({'success': False, 'error': 'harv_file_id is required'}, status=400)

        # Get file objects
        try:
            plots_file = File.objects.get(id=plots_file_id)
        except File.DoesNotExist:
            return JsonResponse({'success': False, 'error': 'Plots file not found'}, status=404)

        try:
            app_file = File.objects.get(id=app_file_id)
        except File.DoesNotExist:
            return JsonResponse({'success': False, 'error': 'As-applied file not found'}, status=404)

        try:
            harv_file = File.objects.get(id=harv_file_id)
        except File.DoesNotExist:
            return JsonResponse({'success': False, 'error': 'Harvest file not found'}, status=404)

        # Check permissions
        if plots_file.owner != request.user and not plots_file.is_public:
            return JsonResponse({'success': False, 'error': 'Permission denied for plots file'}, status=403)

        if app_file.owner != request.user and not app_file.is_public:
            return JsonResponse({'success': False, 'error': 'Permission denied for as-applied file'}, status=403)

        if harv_file.owner != request.user and not harv_file.is_public:
            return JsonResponse({'success': False, 'error': 'Permission denied for harvest file'}, status=403)

        # Validate output folder if provided
        if output_folder_id:
            try:
                output_folder = Folder.objects.get(id=output_folder_id, owner=request.user)
            except Folder.DoesNotExist:
                return JsonResponse({'success': False, 'error': 'Output folder not found'}, status=404)

        # Validate file types
        plots_ext = os.path.splitext(plots_file.name)[1].lower()
        app_ext = os.path.splitext(app_file.name)[1].lower()
        harv_ext = os.path.splitext(harv_file.name)[1].lower()

        if plots_ext != '.shp':
            return JsonResponse({
                'success': False,
                'error': f'Plots file must be a .shp file. Got: {plots_ext}'
            }, status=400)

        if app_ext != '.shp':
            return JsonResponse({
                'success': False,
                'error': f'As-applied file must be a .shp file. Got: {app_ext}'
            }, status=400)

        if harv_ext != '.shp':
            return JsonResponse({
                'success': False,
                'error': f'Harvest file must be a .shp file. Got: {harv_ext}'
            }, status=400)

        # Trigger the Celery task
        from .tasks import run_valid_yield_extractor_task
        task = run_valid_yield_extractor_task.delay(
            str(plots_file_id),
            str(app_file_id),
            str(harv_file_id),
            output_folder_id,
            crs,
            rate_tolerance
        )

        return JsonResponse({
            'success': True,
            'message': f'Valid Yield Extractor started for {plots_file.name}, {app_file.name}, and {harv_file.name}',
            'task_id': task.id
        })

    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        return _internal_error_response(e)


@login_required
def check_valid_yield_extractor_status(request, task_id):
    """
    Check the status of a Valid Yield Extractor task.

    GET request returns:
    {
        "success": true,
        "status": "PENDING" | "STARTED" | "SUCCESS" | "FAILURE",
        "result": {...} (if completed)
    }
    """
    try:
        from celery.result import AsyncResult

        result = AsyncResult(task_id)

        response = {
            'success': True,
            'status': result.status,
        }

        if result.ready():
            if result.successful():
                response['result'] = result.result
            else:
                logger.error("Task %s failed: %s", task_id, result.result)
                response['error'] = 'Task failed (see server logs for details)'

        return JsonResponse(response)

    except Exception as e:
        return _internal_error_response(e)


@login_required
def rename_file(request):
    """
    API endpoint to rename a file.
    
    POST request with JSON body:
    {
        "file_id": "uuid-of-file",
        "new_name": "new_filename.ext"
    }
    
    Returns:
    {
        "success": true/false,
        "message": "...",
        "new_name": "..." (if successful)
    }
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed'}, status=405)
    
    try:
        data = json.loads(request.body)
        file_id = data.get('file_id')
        new_name = data.get('new_name', '').strip()
        
        if not file_id:
            return JsonResponse({'success': False, 'error': 'file_id is required'}, status=400)
        
        if not new_name:
            return JsonResponse({'success': False, 'error': 'new_name is required'}, status=400)
        
        # Get the file object
        try:
            file_obj = File.objects.get(id=file_id)
        except File.DoesNotExist:
            return JsonResponse({'success': False, 'error': 'File not found'}, status=404)
        
        # Check permissions - only owner can rename
        if file_obj.owner != request.user:
            return JsonResponse({'success': False, 'error': 'Permission denied. Only the owner can rename this file.'}, status=403)
        
        # Validate new name
        if '/' in new_name or '\\' in new_name:
            return JsonResponse({'success': False, 'error': 'File name cannot contain slashes'}, status=400)
        
        if len(new_name) > 255:
            return JsonResponse({'success': False, 'error': 'File name is too long (max 255 characters)'}, status=400)
        
        # Check if a file with the same name already exists in the same folder
        existing_file = File.objects.filter(
            name=new_name,
            folder=file_obj.folder,
            owner=file_obj.owner
        ).exclude(id=file_obj.id).first()
        
        if existing_file:
            return JsonResponse({
                'success': False, 
                'error': f'A file named "{new_name}" already exists in this folder'
            }, status=400)
        
        # Update the file name
        old_name = file_obj.name
        file_obj.name = new_name
        file_obj.save(update_fields=['name', 'updated_at'])
        
        import logging
        logger = logging.getLogger(__name__)
        logger.info(f"File renamed: '{old_name}' -> '{new_name}' by user {request.user.username}")
        
        return JsonResponse({
            'success': True,
            'message': f'File renamed from "{old_name}" to "{new_name}"',
            'new_name': new_name
        })
        
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        return _internal_error_response(e)
