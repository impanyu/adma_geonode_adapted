#!/usr/bin/env python3

"""
Token-based API views for file management.
These APIs use token authentication instead of session authentication.
"""

import json

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework.authtoken.models import Token
from django.contrib.auth import authenticate
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.db import transaction
import zipfile
import io
import os
from django.utils.text import slugify

from django.db.models import Q, Count
from django.http import StreamingHttpResponse

from .models import File, Folder, Map, MapLayer, Tool, AgentSession, AgentMessage
from .serializers import (
    FileUploadSerializer, FolderUploadSerializer, FileDownloadSerializer,
    FolderDownloadSerializer, TokenCreateSerializer, FileSerializer, FolderSerializer,
    FileDetailSerializer, FolderDetailSerializer, MapSerializer, ToolSerializer,
    AgentMessageSerializer, AgentStatusSerializer,
)
import logging
from .views import generate_unique_name, _internal_error_response
from .tasks import process_gis_file_task

logger = logging.getLogger(__name__)


def _drf_internal_error(exc, message='An internal error occurred. Please try again.'):
    """DRF-compatible variant of _internal_error_response. Logs exc server-side."""
    logger.exception("Internal error: %s", exc)
    return Response({'error': message}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@permission_classes([AllowAny])  # No authentication required for token creation
def create_token(request):
    """
    Create authentication token for API access.
    
    POST /api/v1/auth/token/
    {
        "username": "your_username",
        "password": "your_password"
    }
    
    Returns:
    {
        "token": "your_api_token",
        "user_id": 123,
        "username": "your_username"
    }
    """
    serializer = TokenCreateSerializer(data=request.data)
    if serializer.is_valid():
        username = serializer.validated_data['username']
        password = serializer.validated_data['password']
        
        user = authenticate(username=username, password=password)
        if user:
            token, created = Token.objects.get_or_create(user=user)
            return Response({
                'token': token.key,
                'user_id': user.id,
                'username': user.username,
                'created': created
            })
        else:
            return Response(
                {'error': 'Invalid credentials'}, 
                status=status.HTTP_401_UNAUTHORIZED
            )
    
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def api_upload_files(request):
    """
    Upload files via token-based API.
    
    POST /api/v1/files/upload/
    Authorization: Token your_api_token
    Content-Type: multipart/form-data
    
    Form data:
    - files: List of files to upload
    - folder_id: Optional UUID of target folder
    - is_public: Boolean (default: false)
    
    Returns:
    {
        "success": true,
        "files": [
            {
                "id": "file-uuid",
                "name": "filename.ext",
                "file_type": "document",
                "is_spatial": false,
                "url": "/file/uuid/"
            }
        ],
        "message": "Successfully uploaded N files"
    }
    """
    serializer = FileUploadSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
    files = serializer.validated_data['files']
    folder_id = serializer.validated_data.get('folder_id')
    is_public = serializer.validated_data.get('is_public', False)
    
    # Get folder if specified
    folder = None
    if folder_id:
        try:
            folder = Folder.objects.get(id=folder_id, owner=request.user)
        except Folder.DoesNotExist:
            return Response(
                {'error': 'Folder not found or access denied'}, 
                status=status.HTTP_404_NOT_FOUND
            )
    
    uploaded_files = []
    
    try:
        with transaction.atomic():
            for uploaded_file in files:
                # Generate unique filename if duplicate exists
                unique_filename = generate_unique_name(
                    uploaded_file.name,
                    request.user,
                    folder=folder,
                    is_folder=False
                )
                
                # Create file object
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
                
                uploaded_files.append({
                    'id': str(file_obj.id),
                    'name': file_obj.name,
                    'file_type': file_obj.file_type,
                    'size': file_obj.get_size_display(),
                    'is_spatial': file_obj.is_spatial,
                    'is_public': file_obj.is_public,
                    'url': f'/file/{file_obj.id}/',
                    'download_url': f'/api/v1/files/{file_obj.id}/download/',
                })
        
        return Response({
            'success': True,
            'files': uploaded_files,
            'message': f'Successfully uploaded {len(uploaded_files)} files'
        }, status=status.HTTP_201_CREATED)
        
    except Exception as e:
        return _drf_internal_error(e)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def api_upload_folders(request):
    """
    Upload folder structure via token-based API.
    
    POST /api/v1/folders/upload/
    Authorization: Token your_api_token
    Content-Type: multipart/form-data
    
    Form data:
    - files: List of files with folder paths
    - file_paths: List of corresponding folder paths
    - folder_id: Optional UUID of parent folder
    - is_public: Boolean (default: false)
    
    Returns:
    {
        "success": true,
        "folders_created": 5,
        "files_uploaded": 15,
        "message": "Successfully uploaded folder structure"
    }
    """
    serializer = FolderUploadSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
    files = serializer.validated_data['files']
    file_paths = serializer.validated_data['file_paths']
    folder_id = serializer.validated_data.get('folder_id')
    is_public = serializer.validated_data.get('is_public', False)
    
    if len(files) != len(file_paths):
        return Response(
            {'error': 'Number of files must match number of file paths'}, 
            status=status.HTTP_400_BAD_REQUEST
        )
    
    # Get parent folder if specified
    parent_folder = None
    if folder_id:
        try:
            parent_folder = Folder.objects.get(id=folder_id, owner=request.user)
        except Folder.DoesNotExist:
            return Response(
                {'error': 'Parent folder not found or access denied'}, 
                status=status.HTTP_404_NOT_FOUND
            )
    
    try:
        with transaction.atomic():
            # Use the same logic as the original upload_folders view
            # This is a simplified version - you can expand it based on your needs
            folders_created = 0
            files_uploaded = 0
            
            # Create folder structure and upload files
            folder_cache = {}  # Cache created folders
            
            for uploaded_file, file_path in zip(files, file_paths):
                path_parts = file_path.split('/')
                
                # Create folder structure if needed
                current_folder = parent_folder
                for i, folder_name in enumerate(path_parts[:-1]):  # Exclude filename
                    folder_path = '/'.join(path_parts[:i+1])
                    
                    if folder_path not in folder_cache:
                        # Check if folder exists
                        try:
                            existing_folder = Folder.objects.get(
                                name=folder_name,
                                parent=current_folder,
                                owner=request.user
                            )
                            folder_cache[folder_path] = existing_folder
                        except Folder.DoesNotExist:
                            # Create new folder
                            unique_folder_name = generate_unique_name(
                                folder_name,
                                request.user,
                                folder=current_folder,
                                is_folder=True
                            )
                            new_folder = Folder.objects.create(
                                name=unique_folder_name,
                                parent=current_folder,
                                owner=request.user,
                                is_public=is_public
                            )
                            folder_cache[folder_path] = new_folder
                            folders_created += 1
                    
                    current_folder = folder_cache[folder_path]
                
                # Upload file to the final folder
                filename = path_parts[-1]
                unique_filename = generate_unique_name(
                    filename,
                    request.user,
                    folder=current_folder,
                    is_folder=False
                )
                
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
                
                files_uploaded += 1
            
            return Response({
                'success': True,
                'folders_created': folders_created,
                'files_uploaded': files_uploaded,
                'message': f'Successfully uploaded folder structure: {folders_created} folders, {files_uploaded} files'
            }, status=status.HTTP_201_CREATED)
            
    except Exception as e:
        return _drf_internal_error(e)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def api_download_file(request, file_id):
    """
    Download file via token-based API.
    
    GET /api/v1/files/{file_id}/download/
    Authorization: Token your_api_token
    
    Returns: File content with appropriate headers
    """
    try:
        file_obj = File.objects.get(id=file_id)
        
        # Check permissions
        if file_obj.owner != request.user and not file_obj.is_public:
            return Response(
                {'error': 'File not found or access denied'}, 
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Return file response
        response = FileResponse(
            file_obj.file.open('rb'),
            as_attachment=True,
            filename=file_obj.name
        )
        response['Content-Type'] = 'application/octet-stream'  # Generic content type
        response['Content-Length'] = file_obj.file_size
        
        return response
        
    except File.DoesNotExist:
        return Response(
            {'error': 'File not found'},
            status=status.HTTP_404_NOT_FOUND
        )
    except Exception as e:
        return _drf_internal_error(e)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def api_list_files(request):
    """
    List files via token-based API.
    
    GET /api/v1/files/
    Authorization: Token your_api_token
    
    Query parameters:
    - folder_id: Filter by folder (optional) - can list public folders owned by others
    - is_public: Filter by public status (optional)
    - file_type: Filter by file type (optional)
    - include_public: If 'true', include public files from other users (default: false)
    
    Returns:
    {
        "files": [
            {
                "id": "file-uuid",
                "name": "filename.ext",
                "file_type": "document",
                "is_spatial": false,
                "is_public": true,
                "created_at": "2023-01-01T00:00:00Z"
            }
        ],
        "count": 10
    }
    """
    from django.db.models import Q
    
    folder_id = request.query_params.get('folder_id')
    include_public = request.query_params.get('include_public', 'false').lower() in ('true', '1', 'yes')
    
    # If folder_id is specified, check if user can access that folder
    if folder_id:
        try:
            folder = Folder.objects.get(id=folder_id)
            # User can access if they own the folder OR the folder is public
            if folder.owner != request.user and not folder.is_public:
                return Response(
                    {'error': 'You do not have permission to access this folder'},
                    status=status.HTTP_403_FORBIDDEN
                )
            # List files in this folder (user's own files OR public files in public folder)
            if folder.owner == request.user:
                queryset = File.objects.filter(folder_id=folder_id, owner=request.user, is_archived=False)
            else:
                # For public folders owned by others, only show public files
                queryset = File.objects.filter(folder_id=folder_id, is_public=True, is_archived=False)
        except Folder.DoesNotExist:
            return Response(
                {'error': 'Folder not found'},
                status=status.HTTP_404_NOT_FOUND
            )
    else:
        # No folder specified - list user's own files
        if include_public:
            # Include user's files AND all public files
            queryset = File.objects.filter(Q(owner=request.user) | Q(is_public=True), is_archived=False)
        else:
            # Only user's own files
            queryset = File.objects.filter(owner=request.user, is_archived=False)
    
    # Apply additional filters
    is_public = request.query_params.get('is_public')
    if is_public is not None:
        is_public_bool = is_public.lower() in ('true', '1', 'yes')
        queryset = queryset.filter(is_public=is_public_bool)
    
    file_type = request.query_params.get('file_type')
    if file_type:
        queryset = queryset.filter(file_type=file_type)
    
    # Serialize and return
    serializer = FileSerializer(queryset, many=True)
    
    return Response({
        'files': serializer.data,
        'count': queryset.count()
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def api_list_folders(request):
    """
    List folders via token-based API.
    
    GET /api/v1/folders/
    Authorization: Token your_api_token
    
    Query parameters:
    - parent_id: Filter by parent folder (optional) - can list subfolders of public folders
    - is_public: Filter by public status (optional)
    - include_public: If 'true', include public folders from other users (default: false)
    
    Returns:
    {
        "folders": [
            {
                "id": "folder-uuid",
                "name": "folder_name",
                "is_public": true,
                "created_at": "2023-01-01T00:00:00Z"
            }
        ],
        "count": 5
    }
    """
    from django.db.models import Q
    
    parent_id = request.query_params.get('parent_id')
    include_public = request.query_params.get('include_public', 'false').lower() in ('true', '1', 'yes')
    
    # If parent_id is specified, check if user can access that parent folder
    if parent_id:
        try:
            parent_folder = Folder.objects.get(id=parent_id)
            # User can access if they own the folder OR the folder is public
            if parent_folder.owner != request.user and not parent_folder.is_public:
                return Response(
                    {'error': 'You do not have permission to access this folder'},
                    status=status.HTTP_403_FORBIDDEN
                )
            # List subfolders in this parent folder
            if parent_folder.owner == request.user:
                queryset = Folder.objects.filter(parent_id=parent_id, owner=request.user, is_archived=False)
            else:
                # For public folders owned by others, only show public subfolders
                queryset = Folder.objects.filter(parent_id=parent_id, is_public=True, is_archived=False)
        except Folder.DoesNotExist:
            return Response(
                {'error': 'Parent folder not found'},
                status=status.HTTP_404_NOT_FOUND
            )
    else:
        # No parent specified - list root folders
        if include_public:
            # Include user's folders AND all public folders
            queryset = Folder.objects.filter(Q(owner=request.user) | Q(is_public=True), is_archived=False).filter(parent__isnull=True)
        else:
            # Only user's own root folders
            queryset = Folder.objects.filter(owner=request.user, parent__isnull=True, is_archived=False)
    
    # Apply additional filters
    is_public = request.query_params.get('is_public')
    if is_public is not None:
        is_public_bool = is_public.lower() in ('true', '1', 'yes')
        queryset = queryset.filter(is_public=is_public_bool)
    
    # Serialize and return
    serializer = FolderSerializer(queryset, many=True)
    
    return Response({
        'folders': serializer.data,
        'count': queryset.count()
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def api_download_folder(request, folder_id):
    """
    Download folder as ZIP file via token-based API.
    
    GET /api/v1/folders/{folder_id}/download/
    Authorization: Token your_api_token
    
    Query parameters:
    - include_subfolders: Include subfolders recursively (default: true)
    
    Returns: ZIP file containing all files in the folder
    """
    try:
        folder = Folder.objects.get(id=folder_id)
        
        # Check permissions
        if folder.owner != request.user and not folder.is_public:
            return Response(
                {'error': 'Folder not found or access denied'}, 
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Get query parameter for including subfolders
        include_subfolders = request.query_params.get('include_subfolders', 'true').lower() in ('true', '1', 'yes')
        
        # Create ZIP file in memory
        zip_buffer = io.BytesIO()
        
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            file_count = 0
            total_size = 0
            
            # Helper function to add files from a folder to ZIP
            def add_folder_to_zip(current_folder, zip_path=""):
                nonlocal file_count, total_size
                
                # Add files from current folder
                files = File.objects.filter(folder=current_folder, owner=request.user)
                for file_obj in files:
                    try:
                        # Create file path in ZIP
                        if zip_path:
                            file_path_in_zip = f"{zip_path}/{file_obj.name}"
                        else:
                            file_path_in_zip = file_obj.name
                        
                        # Add file to ZIP
                        with file_obj.file.open('rb') as f:
                            zip_file.writestr(file_path_in_zip, f.read())
                        
                        file_count += 1
                        total_size += file_obj.file_size
                        
                    except Exception as e:
                        # Skip files that can't be read, but continue with others
                        continue
                
                # Recursively add subfolders if requested
                if include_subfolders:
                    subfolders = Folder.objects.filter(parent=current_folder, owner=request.user)
                    for subfolder in subfolders:
                        if zip_path:
                            subfolder_path = f"{zip_path}/{subfolder.name}"
                        else:
                            subfolder_path = subfolder.name
                        add_folder_to_zip(subfolder, subfolder_path)
            
            # Start adding files from the root folder
            add_folder_to_zip(folder)
            
            # If no files were added, create an empty marker file
            if file_count == 0:
                zip_file.writestr("_empty_folder.txt", "This folder is empty or contains no accessible files.")
        
        # Prepare ZIP file for download
        zip_buffer.seek(0)
        
        # Generate safe filename
        safe_folder_name = slugify(folder.name) or "folder"
        zip_filename = f"{safe_folder_name}.zip"
        
        # Create HTTP response with ZIP file
        response = HttpResponse(
            zip_buffer.getvalue(),
            content_type='application/zip'
        )
        response['Content-Disposition'] = f'attachment; filename="{zip_filename}"'
        response['Content-Length'] = len(zip_buffer.getvalue())
        
        # Add custom headers with folder info
        response['X-Folder-Name'] = folder.name
        response['X-File-Count'] = str(file_count)
        response['X-Total-Size'] = str(total_size)
        
        return response
        
    except Folder.DoesNotExist:
        return Response(
            {'error': 'Folder not found'},
            status=status.HTTP_404_NOT_FOUND
        )
    except Exception as e:
        return _drf_internal_error(e)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def api_folder_info(request, folder_id):
    """
    Get folder information before downloading.
    
    GET /api/v1/folders/{folder_id}/info/
    Authorization: Token your_api_token
    
    Returns folder details including file count and total size.
    """
    try:
        folder = Folder.objects.get(id=folder_id)
        
        # Check permissions
        if folder.owner != request.user and not folder.is_public:
            return Response(
                {'error': 'Folder not found or access denied'}, 
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Count files and calculate total size
        def count_folder_contents(current_folder):
            file_count = 0
            total_size = 0
            
            # Count files in current folder
            files = File.objects.filter(folder=current_folder, owner=request.user)
            file_count += files.count()
            total_size += sum(f.file_size for f in files)
            
            # Recursively count subfolders
            subfolders = Folder.objects.filter(parent=current_folder, owner=request.user)
            for subfolder in subfolders:
                sub_count, sub_size = count_folder_contents(subfolder)
                file_count += sub_count
                total_size += sub_size
            
            return file_count, total_size
        
        file_count, total_size = count_folder_contents(folder)
        
        # Generate ZIP filename
        safe_folder_name = slugify(folder.name) or "folder"
        zip_filename = f"{safe_folder_name}.zip"
        
        return Response({
            'folder_id': str(folder.id),
            'folder_name': folder.name,
            'zip_filename': zip_filename,
            'file_count': file_count,
            'total_size': total_size,
            'total_size_display': _format_file_size(total_size),
            'is_public': folder.is_public,
            'created_at': folder.created_at,
            'download_url': f'/api/v1/folders/{folder.id}/download/'
        })
        
    except Folder.DoesNotExist:
        return Response(
            {'error': 'Folder not found'},
            status=status.HTTP_404_NOT_FOUND
        )
    except Exception as e:
        return _drf_internal_error(e)


def _format_file_size(size_bytes):
    """Helper function to format file size in human-readable format"""
    if size_bytes == 0:
        return "0 B"
    
    size_names = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while size_bytes >= 1024 and i < len(size_names) - 1:
        size_bytes /= 1024.0
        i += 1
    
    return f"{size_bytes:.1f} {size_names[i]}"


# ============================================================
# Folder CRUD APIs
# ============================================================

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def api_create_folder(request):
    """
    Create a new folder.

    POST /api/v1/folders/create/
    {"name": "My Folder", "parent_id": "uuid-or-null", "is_public": false}
    """
    name = request.data.get('name')
    if not name:
        return Response({'error': 'name is required'}, status=status.HTTP_400_BAD_REQUEST)

    parent_id = request.data.get('parent_id')
    parent = None
    if parent_id:
        parent = get_object_or_404(Folder, id=parent_id, owner=request.user)

    is_public = request.data.get('is_public', False)

    if Folder.objects.filter(name=name, parent=parent, owner=request.user).exists():
        return Response({'error': 'Folder with this name already exists'}, status=status.HTTP_409_CONFLICT)

    folder = Folder.objects.create(
        name=name, parent=parent, owner=request.user, is_public=is_public,
    )
    return Response(FolderDetailSerializer(folder).data, status=status.HTTP_201_CREATED)


@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def api_delete_folder(request, folder_id):
    """Delete a folder owned by the user."""
    folder = get_object_or_404(Folder, id=folder_id, owner=request.user)
    folder.delete()
    return Response({'success': True}, status=status.HTTP_200_OK)


@api_view(['PATCH'])
@permission_classes([IsAuthenticated])
def api_update_folder(request, folder_id):
    """
    Update folder name or visibility.

    PATCH /api/v1/folders/<id>/
    {"name": "New Name", "is_public": true}
    """
    folder = get_object_or_404(Folder, id=folder_id, owner=request.user)
    if 'name' in request.data:
        folder.name = request.data['name']
    if 'is_public' in request.data:
        folder.is_public = request.data['is_public']
    folder.save()
    return Response(FolderDetailSerializer(folder).data)


# ============================================================
# File CRUD APIs
# ============================================================

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def api_file_metadata(request, file_id):
    """Get detailed file metadata."""
    file_obj = get_object_or_404(File, id=file_id)
    if file_obj.owner != request.user and not file_obj.is_public:
        return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)
    return Response(FileDetailSerializer(file_obj).data)


@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def api_delete_file(request, file_id):
    """Delete a file owned by the user."""
    file_obj = get_object_or_404(File, id=file_id, owner=request.user)
    file_obj.delete()
    return Response({'success': True}, status=status.HTTP_200_OK)


@api_view(['PATCH'])
@permission_classes([IsAuthenticated])
def api_update_file(request, file_id):
    """
    Update file name or visibility.

    PATCH /api/v1/files/<id>/
    {"name": "new_name.txt", "is_public": true}
    """
    file_obj = get_object_or_404(File, id=file_id, owner=request.user)
    if 'name' in request.data:
        file_obj.name = request.data['name']
    if 'is_public' in request.data:
        file_obj.is_public = request.data['is_public']
    file_obj.save()
    return Response(FileDetailSerializer(file_obj).data)


# ============================================================
# Map APIs
# ============================================================

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def api_list_maps(request):
    """List user's maps and optionally public maps."""
    include_public = request.query_params.get('include_public', 'false').lower() in ('true', '1')
    if include_public:
        qs = Map.objects.filter(Q(owner=request.user) | Q(is_public=True))
    else:
        qs = Map.objects.filter(owner=request.user)
    return Response({'maps': MapSerializer(qs, many=True).data, 'count': qs.count()})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def api_create_map(request):
    """Create a new map."""
    name = request.data.get('name')
    if not name:
        return Response({'error': 'name is required'}, status=status.HTTP_400_BAD_REQUEST)
    if Map.objects.filter(name=name, owner=request.user).exists():
        return Response({'error': 'Map with this name already exists'}, status=status.HTTP_409_CONFLICT)
    map_obj = Map.objects.create(
        name=name,
        description=request.data.get('description', ''),
        owner=request.user,
        is_public=request.data.get('is_public', False),
    )
    return Response(MapSerializer(map_obj).data, status=status.HTTP_201_CREATED)


@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def api_delete_map(request, map_id):
    """Delete a map owned by the user."""
    map_obj = get_object_or_404(Map, id=map_id, owner=request.user)
    map_obj.delete()
    return Response({'success': True})


@api_view(['PATCH'])
@permission_classes([IsAuthenticated])
def api_update_map(request, map_id):
    """Update map name, description, or visibility."""
    map_obj = get_object_or_404(Map, id=map_id, owner=request.user)
    for field in ('name', 'description', 'is_public'):
        if field in request.data:
            setattr(map_obj, field, request.data[field])
    map_obj.save()
    return Response(MapSerializer(map_obj).data)


# ============================================================
# Tool APIs
# ============================================================

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def api_list_tools(request):
    """List available tools for the current user."""
    tools = Tool.get_available_tools_for_user(request.user)
    return Response({'tools': ToolSerializer(tools, many=True).data, 'count': tools.count()})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def api_run_tool(request, tool_slug):
    """
    Run a tool by slug.

    POST /api/v1/tools/<slug>/run/
    Body varies by tool - typically includes file_id, output_folder_id, etc.
    """
    tool = get_object_or_404(Tool, slug=tool_slug, is_active=True)

    # Map slugs to their Celery tasks
    from celery import current_app
    task_name = tool.celery_task_name
    if not task_name:
        return Response({'error': 'Tool has no execution task configured'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        task = current_app.send_task(task_name, kwargs=request.data)
        tool.increment_usage()
        return Response({'success': True, 'task_id': task.id})
    except Exception as e:
        return _drf_internal_error(e)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def api_tool_status(request, tool_slug, task_id):
    """Check status of a tool execution task."""
    from celery.result import AsyncResult
    result = AsyncResult(task_id)
    response = {'status': result.status}
    if result.status == 'SUCCESS':
        response['result'] = result.result
    elif result.status == 'FAILURE':
        response['error'] = str(result.result)
    return Response(response)


# ============================================================
# User Info APIs
# ============================================================

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def api_user_profile(request):
    """Get current user profile."""
    user = request.user
    return Response({
        'id': user.id,
        'username': user.username,
        'email': user.email,
        'first_name': user.first_name,
        'last_name': user.last_name,
        'date_joined': user.date_joined,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def api_user_stats(request):
    """Get user statistics."""
    user = request.user
    files = File.objects.filter(owner=user)
    folders = Folder.objects.filter(owner=user)
    maps = Map.objects.filter(owner=user)

    total_size = sum(f.file_size for f in files)
    return Response({
        'file_count': files.count(),
        'folder_count': folders.count(),
        'map_count': maps.count(),
        'total_storage_bytes': total_size,
        'total_storage_display': _format_file_size(total_size),
        'public_files': files.filter(is_public=True).count(),
        'spatial_files': files.filter(is_spatial=True).count(),
    })


# ============================================================
# Search API (enhanced for token auth)
# ============================================================

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def api_search(request):
    """
    Search files, folders, and maps.

    GET /api/v1/search/?q=keyword&type=file|folder|map
    """
    query = request.query_params.get('q', '').strip()
    search_type = request.query_params.get('type', 'all')

    if not query:
        return Response({'error': 'q parameter is required'}, status=status.HTTP_400_BAD_REQUEST)

    results = {}

    if search_type in ('all', 'file'):
        files = File.objects.filter(
            Q(owner=request.user) | Q(is_public=True),
            name__icontains=query,
            is_archived=False,
        )[:20]
        results['files'] = FileDetailSerializer(files, many=True).data

    if search_type in ('all', 'folder'):
        folders = Folder.objects.filter(
            Q(owner=request.user) | Q(is_public=True),
            name__icontains=query,
            is_archived=False,
        )[:20]
        results['folders'] = FolderDetailSerializer(folders, many=True).data

    if search_type in ('all', 'map'):
        maps = Map.objects.filter(
            Q(owner=request.user) | Q(is_public=True),
            Q(name__icontains=query) | Q(description__icontains=query),
        )[:20]
        results['maps'] = MapSerializer(maps, many=True).data

    return Response(results)


# ============================================================
# Agent Chat APIs (session-auth, called from browser)
# ============================================================

from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST, require_GET
from django.views.decorators.csrf import csrf_protect


@login_required
@require_POST
def api_agent_chat(request):
    """
    Send a message to the user's AI agent asynchronously.
    Returns immediately with the user message ID and chat session ID.
    """
    body = json.loads(request.body)
    message = body.get('message', '').strip()
    chat_session_id = body.get('chat_session_id')
    if not message:
        return JsonResponse({'error': 'message is required'}, status=400)

    from .agent_manager import AgentContainerManager
    manager = AgentContainerManager()

    try:
        user_msg_id, cs_id = manager.send_message_async(
            request.user, message, chat_session_id=chat_session_id
        )
        return JsonResponse({'ok': True, 'user_message_id': user_msg_id, 'chat_session_id': cs_id})
    except Exception as e:
        return _internal_error_response(e)


@login_required
@require_GET
def api_agent_history(request):
    """
    Get agent chat history for a specific chat session.
    Supports ?chat_session_id=<id>&after=<message_id>
    """
    from .agent_manager import AgentContainerManager
    manager = AgentContainerManager()

    limit = int(request.GET.get('limit', 50))
    after_id = request.GET.get('after')
    chat_session_id = request.GET.get('chat_session_id')

    messages = manager.get_chat_history(
        request.user, chat_session_id=chat_session_id, limit=limit
    )

    if after_id:
        from .models import AgentMessage as AM
        try:
            after_msg = AM.objects.get(id=after_id)
            messages = [m for m in messages if m.created_at > after_msg.created_at]
        except AM.DoesNotExist:
            pass

    data = [
        {
            'id': str(m.id),
            'role': m.role,
            'content': m.content,
            'is_streaming': m.is_streaming,
            'metadata': m.metadata,
            'created_at': m.created_at.isoformat(),
        }
        for m in messages
    ]
    return JsonResponse({'messages': data, 'count': len(data)})


@login_required
@require_GET
def api_agent_sessions(request):
    """List all chat sessions for the user."""
    from .agent_manager import AgentContainerManager
    manager = AgentContainerManager()
    sessions = manager.get_chat_sessions(request.user)
    data = [
        {
            'id': str(s.id),
            'name': s.name,
            'created_at': s.created_at.isoformat(),
            'updated_at': s.updated_at.isoformat(),
        }
        for s in sessions
    ]
    return JsonResponse({'sessions': data})


@login_required
@require_POST
def api_agent_session_create(request):
    """Create a new chat session."""
    body = json.loads(request.body)
    name = body.get('name', 'New Chat').strip() or 'New Chat'
    from .models import ChatSession
    cs = ChatSession.objects.create(user=request.user, name=name)
    return JsonResponse({'ok': True, 'id': str(cs.id), 'name': cs.name})


@login_required
@require_POST
def api_agent_session_rename(request):
    """Rename a chat session."""
    body = json.loads(request.body)
    cs_id = body.get('chat_session_id')
    name = body.get('name', '').strip()
    if not cs_id or not name:
        return JsonResponse({'error': 'chat_session_id and name required'}, status=400)
    from .agent_manager import AgentContainerManager
    manager = AgentContainerManager()
    try:
        cs = manager.rename_chat_session(request.user, cs_id, name)
        return JsonResponse({'ok': True, 'id': str(cs.id), 'name': cs.name})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)


@login_required
@require_POST
def api_agent_session_delete(request):
    """Permanently delete a chat session and its data."""
    body = json.loads(request.body)
    cs_id = body.get('chat_session_id')
    if not cs_id:
        return JsonResponse({'error': 'chat_session_id required'}, status=400)
    from .agent_manager import AgentContainerManager
    manager = AgentContainerManager()
    try:
        manager.delete_chat_session(request.user, cs_id)
        return JsonResponse({'ok': True})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)


@login_required
@require_GET
def api_agent_status(request):
    """Get agent container status."""
    from .agent_manager import AgentContainerManager
    manager = AgentContainerManager()
    return JsonResponse(manager.get_status(request.user))


@login_required
@require_POST
def api_agent_stop(request):
    """Stop the user's agent container."""
    from .agent_manager import AgentContainerManager
    manager = AgentContainerManager()
    manager.stop_agent(request.user)
    return JsonResponse({'success': True, 'status': 'stopped'})
