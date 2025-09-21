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
from .models import Folder, File
from .forms import RegistrationForm, FolderForm, FileUploadForm
from .tasks import process_gis_file_task

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
            is_public=True
        ).filter(
            Q(parent=None) |  # No parent (truly top-level)
            Q(parent__is_public=False)  # Parent is private (so this is top public level)
        ).annotate(
            public_file_count=Count('files', filter=Q(files__is_public=True)),
            public_subfolder_count=Count('subfolders', filter=Q(subfolders__is_public=True)),
            file_count=Count('files'),
            subfolder_count=Count('subfolders')
        ).filter(
            Q(public_file_count__gt=0) | Q(public_subfolder_count__gt=0)  # Only folders with public content
        ).order_by('name')
        
        # Get top-level public files - files that are public and either:
        # 1. Have no folder (orphaned files), OR  
        # 2. Have a private folder (making them the highest public level)
        public_files_queryset = File.objects.filter(
            is_public=True
        ).filter(
            Q(folder=None) |  # No folder (orphaned)
            Q(folder__is_public=False)  # Folder is private (so this file is top public level)
        ).order_by('-created_at')
        
        # Combine folders and files for unified pagination (100 items per page)
        page = self.request.GET.get('page', 1)
        items_per_page = 100
        
        # Calculate total items
        total_folders = public_folders_queryset.count()
        total_files = public_files_queryset.count()
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
                public_folders = public_folders_queryset[start_index:end_index]
                public_files = File.objects.none()
            else:
                # Page contains some folders and some files
                public_folders = public_folders_queryset[start_index:]
                files_start = 0
                files_end = end_index - total_folders
                public_files = public_files_queryset[files_start:files_end]
        else:
            # Page starts with files
            public_folders = Folder.objects.none()
            files_start = start_index - total_folders
            files_end = files_start + items_per_page
            public_files = public_files_queryset[files_start:files_end]
        
        context['public_folders'] = public_folders
        context['public_files'] = public_files
        context['page_obj'] = page_obj
        context['total_items'] = total_items
        
        # Statistics for public data
        context['stats'] = {
            'total_public_files': total_files,
            'total_public_folders': total_folders,
        }
        
        return context

@login_required
def dashboard(request):
    """Main dashboard for authenticated users"""
    user = request.user
    
    # Get user's root folders with annotations
    folders_queryset = Folder.objects.filter(owner=user, parent=None).annotate(
        file_count=Count('files'),
        subfolder_count=Count('subfolders')
    ).order_by('name')
    
    # Get root-level files (not files inside folders)
    files_queryset = File.objects.filter(owner=user, folder=None).order_by('-created_at')
    
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
    
    # Statistics
    stats = {
        'total_files': File.objects.filter(owner=user).count(),
        'total_folders': Folder.objects.filter(owner=user).count(),
        'total_size': sum(f.file_size for f in File.objects.filter(owner=user)),
        'public_files': File.objects.filter(owner=user, is_public=True).count(),
    }
    
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
    
    # Get subfolders and files with pagination
    subfolders_queryset = folder.subfolders.annotate(
        file_count=Count('files'),
        subfolder_count=Count('subfolders')
    ).order_by('name')
    files_queryset = folder.files.all().order_by('-created_at')
    
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
    subfolders = folder.subfolders.filter(is_public=True).annotate(
        file_count=Count('files'),
        subfolder_count=Count('subfolders')
    )
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
            
            # Generate embedding for the created folder (if ChromaDB is available)
            try:
                from .tasks import generate_folder_embedding_task
                generate_folder_embedding_task.delay(str(folder.id))
            except Exception:
                # ChromaDB not available, skip embedding generation
                pass
            
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
                
                # Generate embedding for the uploaded file (if ChromaDB is available)
                try:
                    from .tasks import generate_file_embedding_task
                    generate_file_embedding_task.delay(str(file_obj.id))
                except Exception:
                    # ChromaDB not available, skip embedding generation
                    pass
                
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

@login_required
def delete_item(request):
    """Delete file or folder via AJAX"""
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            item_type = data.get('type')  # 'file' or 'folder'
            item_id = data.get('id')
            
            if item_type == 'file':
                item = get_object_or_404(File, id=item_id, owner=request.user)
                item_name = item.name
                item.delete()
            elif item_type == 'folder':
                item = get_object_or_404(Folder, id=item_id, owner=request.user)
                item_name = item.name
                item.delete()
            else:
                return JsonResponse({'error': 'Invalid item type'}, status=400)
            
            return JsonResponse({
                'success': True,
                'message': f'Successfully deleted {item_name}'
            })
            
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON data'}, status=400)
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=500)
    
    return JsonResponse({'error': 'Method not allowed'}, status=405)

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
                        
                        # Generate embedding for the created folder
                        from .tasks import generate_folder_embedding_task
                        generate_folder_embedding_task.delay(str(folder.id))
                    
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
                
                # Generate embedding for the uploaded file (if ChromaDB is available)
                try:
                    from .tasks import generate_file_embedding_task
                    generate_file_embedding_task.delay(str(file_obj.id))
                except Exception:
                    # ChromaDB not available, skip embedding generation
                    pass
                
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
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            item_type = data.get('type')
            item_id = data.get('id')
            
            if item_type == 'folder':
                folder = get_object_or_404(Folder, id=item_id, owner=request.user)
                folder.is_public = not folder.is_public
                folder.save()
                return JsonResponse({
                    'success': True, 
                    'is_public': folder.is_public,
                    'status': 'Public' if folder.is_public else 'Private'
                })
            elif item_type == 'file':
                file_obj = get_object_or_404(File, id=item_id, owner=request.user)
                file_obj.is_public = not file_obj.is_public
                file_obj.save()
                return JsonResponse({
                    'success': True, 
                    'is_public': file_obj.is_public,
                    'status': 'Public' if file_obj.is_public else 'Private'
                })
            else:
                return JsonResponse({'success': False, 'error': 'Invalid item type'})
            
        except json.JSONDecodeError:
            return JsonResponse({'success': False, 'error': 'Invalid JSON'})
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)})
    
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
    """Hybrid search page with semantic and metadata filtering"""
    template_name = 'filemanager/search.html'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Search'
        
        # Get search parameters
        query = self.request.GET.get('q', '').strip()
        content_type = self.request.GET.get('type', '')  # 'file', 'folder', or ''
        file_type = self.request.GET.get('file_type', '')
        is_spatial = self.request.GET.get('is_spatial', '')
        is_public = self.request.GET.get('is_public', '')
        owner_filter = self.request.GET.get('owner', '').strip()
        page = self.request.GET.get('page', 1)
        
        context.update({
            'query': query,
            'content_type': content_type,
            'file_type': file_type,
            'is_spatial': is_spatial,
            'is_public': is_public,
            'owner_filter': owner_filter,
        })
        
        # Initialize results
        search_results = []
        total_results = 0
        
        if query:
            try:
                from .embedding_service import embedding_service
                chromadb_available = True
            except RuntimeError as e:
                if "sqlite3" in str(e).lower():
                    # ChromaDB requires SQLite 3.35+, gracefully handle this
                    chromadb_available = False
                    context['chromadb_error'] = "ChromaDB requires SQLite 3.35+ to function. Vector search is temporarily unavailable."
                else:
                    raise e
            
            if chromadb_available:
                # Build filters for hybrid search
                filters = {}
                
                if content_type:
                    filters['type'] = content_type
                
                if file_type:
                    filters['file_type'] = file_type
                    
                if is_spatial:
                    filters['is_spatial'] = is_spatial.lower() == 'true'
                    
                if is_public:
                    filters['is_public'] = is_public.lower() == 'true'
                    
                if owner_filter:
                    filters['owner_username'] = owner_filter
                
                # Perform semantic search
                raw_results = embedding_service.search_similar(
                    query_text=query,
                    user_id=str(self.request.user.id) if self.request.user.is_authenticated else '',
                    filters=filters,
                    limit=1000  # Get more results for pagination
                )
                
                # Convert ChromaDB results to Django objects
                for result in raw_results:
                    try:
                        if result['type'] == 'file':
                            obj = File.objects.get(id=result['id'])
                            search_results.append({
                                'object': obj,
                                'type': 'file',
                                'similarity': result['similarity'],
                                'metadata_text': result['document']
                            })
                        elif result['type'] == 'folder':
                            obj = Folder.objects.get(id=result['id'])
                            search_results.append({
                                'object': obj,
                                'type': 'folder',
                                'similarity': result['similarity'],
                                'metadata_text': result['document']
                            })
                    except (File.DoesNotExist, Folder.DoesNotExist):
                        # Object might have been deleted, skip
                        continue
                
                total_results = len(search_results)
            else:
                # ChromaDB not available, show message about requirements
                total_results = 0
        
        # Pagination (100 items per page)
        items_per_page = 100
        paginator = Paginator(search_results, items_per_page)
        
        try:
            page_obj = paginator.page(page)
        except PageNotAnInteger:
            page_obj = paginator.page(1)
        except EmptyPage:
            page_obj = paginator.page(paginator.num_pages)
        
        context.update({
            'search_results': page_obj,
            'page_obj': page_obj,
            'total_results': total_results,
            'has_search': bool(query),
        })
        
        return context

@login_required
def search_api(request):
    """API endpoint for search suggestions and quick search"""
    if request.method == 'GET':
        query = request.GET.get('q', '').strip()
        limit = min(int(request.GET.get('limit', 10)), 50)  # Max 50 suggestions
        
        if not query or len(query) < 2:
            return JsonResponse({'suggestions': []})
        
        try:
            from .embedding_service import embedding_service
            chromadb_available = True
        except RuntimeError as e:
            if "sqlite3" in str(e).lower():
                return JsonResponse({'suggestions': [], 'error': 'ChromaDB requires SQLite 3.35+ to function'})
            else:
                raise e
        
        try:
            # Get quick search results
            results = embedding_service.search_similar(
                query_text=query,
                user_id=str(request.user.id),
                filters=None,
                limit=limit
            )
            
            # Format suggestions
            suggestions = []
            for result in results:
                try:
                    if result['type'] == 'file':
                        obj = File.objects.get(id=result['id'])
                        suggestions.append({
                            'id': str(obj.id),
                            'type': 'file',
                            'name': obj.name,
                            'icon': obj.get_icon_class(),
                            'url': obj.get_absolute_url(),
                            'is_public': obj.is_public,
                            'similarity': round(result['similarity'], 3)
                        })
                    elif result['type'] == 'folder':
                        obj = Folder.objects.get(id=result['id'])
                        suggestions.append({
                            'id': str(obj.id),
                            'type': 'folder', 
                            'name': obj.name,
                            'icon': 'fas fa-folder',
                            'url': obj.get_absolute_url(),
                            'is_public': obj.is_public,
                            'similarity': round(result['similarity'], 3)
                        })
                except (File.DoesNotExist, Folder.DoesNotExist):
                    continue
            
            return JsonResponse({'suggestions': suggestions})
            
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=500)
    
    return JsonResponse({'error': 'Method not allowed'}, status=405)
