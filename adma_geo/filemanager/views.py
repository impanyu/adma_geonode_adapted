import json
import magic
import os
from pathlib import Path
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
from .models import Folder, File, Map
from .forms import RegistrationForm, FolderForm, FileUploadForm
from .tasks import process_gis_file_task

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
    
    # Calculate accurate statistics based on what actually exists
    active_files = File.objects.filter(owner=user, deletion_in_progress=False)
    active_folders = Folder.objects.filter(owner=user, deletion_in_progress=False)
    
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
        public_folders_queryset = Folder.objects.filter(
            is_public=True,
            deletion_in_progress=False
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
        public_files_queryset = File.objects.filter(
            is_public=True,
            deletion_in_progress=False
        ).filter(
            Q(folder=None) |  # No folder (orphaned)
            Q(folder__is_public=False)  # Folder is private (so this file is top public level)
        ).order_by('-created_at')
        
        # Get public maps
        public_maps_queryset = Map.objects.filter(
            is_public=True,
            deletion_in_progress=False
        ).order_by('-created_at')
        
        # Combine folders, files, and maps for unified pagination (100 items per page)
        page = self.request.GET.get('page', 1)
        items_per_page = 100
        
        # Calculate total items
        total_folders = public_folders_queryset.count()
        total_files = public_files_queryset.count()
        total_maps = public_maps_queryset.count()
        total_items = total_folders + total_files + total_maps
        
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
        public_maps = Map.objects.none()
        
        # Determine which content types are in this page range
        if start_index < total_folders:
            # Page starts with folders
            if end_index <= total_folders:
                # Page contains only folders
                public_folders = public_folders_queryset[start_index:end_index]
            else:
                # Page contains folders and other content
                public_folders = public_folders_queryset[start_index:]
                remaining_slots = end_index - total_folders
                
                if remaining_slots > 0 and remaining_slots <= total_files:
                    # Remaining slots filled with files
                    public_files = public_files_queryset[0:remaining_slots]
                elif remaining_slots > total_files:
                    # All files + some maps
                    public_files = public_files_queryset[0:total_files]
                    maps_count = remaining_slots - total_files
                    public_maps = public_maps_queryset[0:maps_count]
        elif start_index < total_folders + total_files:
            # Page starts with files
            files_start = start_index - total_folders
            if end_index <= total_folders + total_files:
                # Page contains only files
                public_files = public_files_queryset[files_start:files_start + items_per_page]
            else:
                # Page contains files and maps
                public_files = public_files_queryset[files_start:]
                maps_count = end_index - (total_folders + total_files)
                public_maps = public_maps_queryset[0:maps_count]
        else:
            # Page starts with maps
            maps_start = start_index - (total_folders + total_files)
            public_maps = public_maps_queryset[maps_start:maps_start + items_per_page]
        
        context['public_folders'] = public_folders
        context['public_files'] = public_files
        context['public_maps'] = public_maps
        context['page_obj'] = page_obj
        context['total_items'] = total_items
        
        # Statistics for public data
        context['stats'] = {
            'total_public_files': total_files,
            'total_public_folders': total_folders,
            'total_public_maps': total_maps,
        }
        
        return context

@login_required
def dashboard(request):
    """Main dashboard for authenticated users"""
    user = request.user
    
    # Get user's root folders (exclude items being deleted)
    folders_queryset = Folder.objects.filter(owner=user, parent=None, deletion_in_progress=False).order_by('name')
    
    # Get root-level files (not files inside folders, exclude items being deleted)
    files_queryset = File.objects.filter(owner=user, folder=None, deletion_in_progress=False).order_by('-created_at')
    
    # Combine folders and files for pagination
    # We'll handle pagination by getting separate pages and combining
    page = request.GET.get('page', 1)
    items_per_page = 100
    
    # Calculate total items
    total_folders = folders_queryset.count()
    total_files = files_queryset.count()
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
            folders = folders_queryset[start_index:end_index]
            recent_files = File.objects.none()
        else:
            # Page contains some folders and some files
            folders = folders_queryset[start_index:]
            files_start = 0
            files_end = end_index - total_folders
            recent_files = files_queryset[files_start:files_end]
    else:
        # Page starts with files
        folders = Folder.objects.none()
        files_start = start_index - total_folders
        files_end = files_start + items_per_page
        recent_files = files_queryset[files_start:files_end]
    
    # Robust statistics with automatic stale deletion recovery
    stats = get_robust_user_statistics(user)
    
    return render(request, 'filemanager/dashboard.html', {
        'folders': folders,
        'recent_files': recent_files,
        'stats': stats,
        'current_folder': None,
        'page_obj': page_obj,
        'total_items': total_items,
        'can_edit': True  # User can always edit their own dashboard
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
    
    return render(request, 'filemanager/folder_detail.html', {
        'folder': folder,
        'subfolders': subfolders,
        'files': files,
        'breadcrumbs': folder.get_breadcrumbs(),
        'can_edit': folder.owner == request.user,
        'page_obj': page_obj,
        'total_items': total_items,
    })

def public_folder_detail(request, folder_id):
    """Public view of folder contents"""
    folder = get_object_or_404(Folder, id=folder_id, is_public=True)
    
    # If user is authenticated and owns this folder, redirect to dashboard
    if request.user.is_authenticated and folder.owner == request.user:
        return redirect('filemanager:folder_detail', folder_id=folder_id)
    
    # Get public subfolders and files
    subfolders = folder.subfolders.filter(is_public=True)
    files = folder.files.filter(is_public=True)
    
    # Reuse the same template as private folder detail, but with public view context
    return render(request, 'filemanager/folder_detail.html', {
        'folder': folder,
        'subfolders': subfolders,
        'files': files,
        'breadcrumbs': folder.get_public_breadcrumbs(),  # Use public breadcrumbs
        'can_edit': False,  # Public users can't edit
        'is_public_view': True,  # Flag to adjust breadcrumbs and navigation
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
            print(f"Error processing Excel file: {e}")
            csv_data = None
            csv_headers = None
    
    return render(request, 'filemanager/file_detail.html', {
        'file': file_obj,
        'file_content': file_content,
        'csv_data': json.dumps(csv_data) if csv_data else None,
        'csv_headers': json.dumps(csv_headers) if csv_headers else None,
        'can_edit': file_obj.owner == request.user,
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
            print(f"Error processing Excel file: {e}")
            csv_data = None
            csv_headers = None
    
    # Reuse the same template as private file detail, but with can_edit=False
    return render(request, 'filemanager/file_detail.html', {
        'file': file_obj,
        'file_content': file_content,
        'csv_data': json.dumps(csv_data) if csv_data else None,
        'csv_headers': json.dumps(csv_headers) if csv_headers else None,
        'can_edit': False,  # Public users can't edit
        'is_public_view': True,  # Flag to adjust breadcrumbs and navigation
        'public_breadcrumbs': file_obj.get_public_breadcrumbs(),  # Add public breadcrumbs
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
            return JsonResponse({'error': str(e)}, status=500)
    
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
            for uploaded_file in files:
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
            
            return JsonResponse({
                'success': True,
                'files': uploaded_files,
                'message': f'Successfully uploaded {len(uploaded_files)} files'
            })
            
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=500)
    
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
                        
                print(f"Deleted GeoServer resources for {file_obj.name}")
            except Exception as e:
                print(f"Error deleting GeoServer resources for {file_obj.name}: {e}")
        
        # 2. Remove from all maps (this will trigger MapLayer deletion due to CASCADE)
        # Update affected maps to remove this layer from their Layer Groups
        affected_maps = []
        for map_layer in file_obj.map_memberships.all():
            affected_maps.append(map_layer.map)
        
        # 3. Remove ChromaDB embeddings
        try:
            from .embedding_service import embedding_service
            if file_obj.chroma_id:
                embedding_service.remove_embedding(file_obj.chroma_id)
                print(f"Deleted ChromaDB embedding for {file_obj.name}")
        except Exception as e:
            print(f"Error deleting ChromaDB embedding for {file_obj.name}: {e}")
            # Clear the chroma_id to prevent future issues and continue with deletion
            file_obj.chroma_id = None
            file_obj.save(update_fields=['chroma_id'])
        
        # 4. Remove physical file from disk
        try:
            if file_obj.file and file_obj.file.path:
                import os
                if os.path.exists(file_obj.file.path):
                    os.remove(file_obj.file.path)
                    print(f"Deleted physical file: {file_obj.file.path}")
        except Exception as e:
            print(f"Error deleting physical file for {file_obj.name}: {e}")
        
        # 5. Remove from PostgreSQL database (this will cascade to MapLayers)
        file_obj.delete()
        print(f"Deleted file {file_obj.name} from database")
        
        # 6. Update affected maps' Layer Groups in GeoServer
        for map_obj in affected_maps:
            try:
                from .geoserver_manager import LayerGroupManager
                layer_group_manager = LayerGroupManager()
                layer_group_manager.update_layer_group(map_obj)
                print(f"Updated Layer Group for map {map_obj.name}")
            except Exception as e:
                print(f"Error updating Layer Group for map {map_obj.name}: {e}")
                
    except Exception as e:
        print(f"Error in delete_file_complete for {file_obj.name}: {e}")
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
        print(f"Starting complete deletion of folder: {folder_obj.name}")
        
        # 1. Recursively delete all files in this folder
        for file_obj in folder_obj.files.all():
            print(f"Deleting file {file_obj.name} in folder {folder_obj.name}")
            delete_file_complete(file_obj)
        
        # 2. Recursively delete all subfolders
        for subfolder in folder_obj.subfolders.all():
            print(f"Deleting subfolder {subfolder.name} in folder {folder_obj.name}")
            delete_folder_complete(subfolder)
        
        # 3. Remove ChromaDB embeddings for the folder
        try:
            from .embedding_service import embedding_service
            if folder_obj.chroma_id:
                embedding_service.remove_embedding(folder_obj.chroma_id)
                print(f"Deleted ChromaDB embedding for folder {folder_obj.name}")
        except Exception as e:
            print(f"Error deleting ChromaDB embedding for folder {folder_obj.name}: {e}")
            # Clear the chroma_id to prevent future issues and continue with deletion
            folder_obj.chroma_id = None
            folder_obj.save(update_fields=['chroma_id'])
        
        # 4. Remove from PostgreSQL database
        folder_obj.delete()
        print(f"Deleted folder {folder_obj.name} from database")
        
    except Exception as e:
        print(f"Error in delete_folder_complete for {folder_obj.name}: {e}")
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
            return JsonResponse({'error': str(e)}, status=500)
    
    return JsonResponse({'error': 'Method not allowed'}, status=405)


@login_required
def check_deletion_status(request, task_id):
    """Check the status of an async deletion task"""
    try:
        from celery.result import AsyncResult
        
        task_result = AsyncResult(task_id)
        
        return JsonResponse({
            'task_id': task_id,
            'status': task_result.status,
            'result': task_result.result if task_result.ready() else None,
            'ready': task_result.ready(),
            'successful': task_result.successful() if task_result.ready() else None,
            'failed': task_result.failed() if task_result.ready() else None
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


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
        geoserver_info = {
            'workspace': file_obj.geoserver_workspace,
            'layer_name': file_obj.geoserver_layer_name,
            'wms_url': f"{settings.GEOSERVER_URL.replace('geoserver:8080', 'localhost:8080')}/wms",
            'wfs_url': f"{settings.GEOSERVER_URL.replace('geoserver:8080', 'localhost:8080')}/wfs",
            'is_published': file_obj.gis_status == 'published',
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
        geoserver_info = {
            'workspace': file_obj.geoserver_workspace,
            'layer_name': file_obj.geoserver_layer_name,
            'wms_url': f"{settings.GEOSERVER_URL.replace('geoserver:8080', 'localhost:8080')}/wms",
            'wfs_url': f"{settings.GEOSERVER_URL.replace('geoserver:8080', 'localhost:8080')}/wfs",
            'is_published': file_obj.gis_status == 'published',
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
            return JsonResponse({'error': str(e)}, status=500)
    
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
            return JsonResponse({'success': False, 'error': str(e)})
    
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
