import json
import magic
import os
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
        
        # Get pagination parameters
        folders_page = self.request.GET.get('folders_page', 1)
        files_page = self.request.GET.get('files_page', 1)
        
        # Get all public folders with pagination
        all_public_folders = Folder.objects.filter(
            is_public=True, 
            parent=None
        ).annotate(
            file_count=Count('files'),
            subfolder_count=Count('subfolders')
        ).order_by('-created_at')
        
        folders_paginator = Paginator(all_public_folders, 12)  # 12 folders per page
        try:
            public_folders = folders_paginator.page(folders_page)
        except PageNotAnInteger:
            public_folders = folders_paginator.page(1)
        except EmptyPage:
            public_folders = folders_paginator.page(folders_paginator.num_pages)
        
        # Get all public files with pagination
        all_public_files = File.objects.filter(is_public=True).order_by('-created_at')
        
        files_paginator = Paginator(all_public_files, 12)  # 12 files per page
        try:
            public_files = files_paginator.page(files_page)
        except PageNotAnInteger:
            public_files = files_paginator.page(1)
        except EmptyPage:
            public_files = files_paginator.page(files_paginator.num_pages)
        
        context['public_folders'] = public_folders
        context['public_files'] = public_files
        
        # Statistics for public data
        context['stats'] = {
            'total_public_files': File.objects.filter(is_public=True).count(),
            'total_public_folders': Folder.objects.filter(is_public=True).count(),
        }
        
        return context

@login_required
def dashboard(request):
    """Main dashboard for authenticated users"""
    user = request.user
    
    # Get user's root folders
    folders = Folder.objects.filter(owner=user, parent=None).annotate(
        file_count=Count('files'),
        subfolder_count=Count('subfolders')
    )
    
    # Get recent files (only root-level files, not files inside folders)
    recent_files = File.objects.filter(owner=user, folder=None).order_by('-created_at')[:6]
    
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
    })

@login_required
def folder_detail(request, folder_id):
    """View folder contents"""
    folder = get_object_or_404(Folder, id=folder_id)
    
    # Check permissions
    if folder.owner != request.user and not folder.is_public:
        messages.error(request, "You don't have permission to view this folder.")
        return redirect('filemanager:dashboard')
    
    # Get subfolders and files
    subfolders = folder.subfolders.annotate(
        file_count=Count('files'),
        subfolder_count=Count('subfolders')
    )
    files = folder.files.all()
    
    return render(request, 'filemanager/folder_detail.html', {
        'folder': folder,
        'subfolders': subfolders,
        'files': files,
        'breadcrumbs': folder.get_breadcrumbs(),
        'can_edit': folder.owner == request.user,
    })

def public_folder_detail(request, folder_id):
    """Public view of folder contents"""
    folder = get_object_or_404(Folder, id=folder_id, is_public=True)
    
    # Get public subfolders and files
    subfolders = folder.subfolders.filter(is_public=True).annotate(
        file_count=Count('files'),
        subfolder_count=Count('subfolders')
    )
    files = folder.files.filter(is_public=True)
    
    return render(request, 'filemanager/public_folder_detail.html', {
        'folder': folder,
        'subfolders': subfolders,
        'files': files,
        'breadcrumbs': folder.get_breadcrumbs(),
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
    if file_obj.file_type == 'text' and file_obj.file_size < 1024 * 1024:  # Max 1MB for preview
        try:
            with file_obj.file.open('r') as f:
                file_content = f.read()
        except (UnicodeDecodeError, FileNotFoundError):
            file_content = None
    
    return render(request, 'filemanager/file_detail.html', {
        'file': file_obj,
        'file_content': file_content,
        'can_edit': file_obj.owner == request.user,
    })

def public_file_detail(request, file_id):
    """Public view of file details"""
    file_obj = get_object_or_404(File, id=file_id, is_public=True)
    
    # For text files, read content for preview
    file_content = None
    if file_obj.file_type == 'text' and file_obj.file_size < 1024 * 1024:  # Max 1MB for preview
        try:
            with file_obj.file.open('r') as f:
                file_content = f.read()
        except (UnicodeDecodeError, FileNotFoundError):
            file_content = None
    
    return render(request, 'filemanager/public_file_detail.html', {
        'file': file_obj,
        'file_content': file_content,
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
                
                # Trigger GIS processing if it's a spatial file
                if file_obj.is_spatial:
                    process_gis_file_task.delay(str(file_obj.id))
                
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
    
    return render(request, 'filemanager/public_map_viewer.html', {
        'file': file_obj,
        'geoserver_info': geoserver_info,
    })

@login_required
def upload_folders(request):
    """Upload folders with their structure via AJAX"""
    if request.method == 'POST':
        try:
            files = request.FILES.getlist('files')
            file_paths = request.POST.getlist('file_paths')
            is_public = request.POST.get('is_public') == 'true'
            
            if not files or len(files) != len(file_paths):
                return JsonResponse({'error': 'No files selected or paths mismatch'}, status=400)
            
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
                            folder=None,  # Root level
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
                current_folder = None
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
                
                # Trigger GIS processing if it's a spatial file
                if file_obj.is_spatial:
                    process_gis_file_task.delay(str(file_obj.id))
                
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
