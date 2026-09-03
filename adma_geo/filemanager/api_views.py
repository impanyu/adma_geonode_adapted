#!/usr/bin/env python3

"""
Token-based API views for file management.
These APIs use token authentication instead of session authentication.
"""

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework.authtoken.models import Token
from django.contrib.auth import authenticate
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404
from django.db import transaction
import zipfile
import io
import os
from django.utils.text import slugify

from .models import File, Folder
from .serializers import (
    FileUploadSerializer, FolderUploadSerializer, FileDownloadSerializer,
    FolderDownloadSerializer, TokenCreateSerializer, FileSerializer, FolderSerializer
)
from .views import generate_unique_name
from .tasks import process_gis_file_task


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
        return Response(
            {'error': f'Upload failed: {str(e)}'}, 
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


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
        return Response(
            {'error': f'Folder upload failed: {str(e)}'}, 
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


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
            file_obj.open_content('rb'),
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
        return Response(
            {'error': f'Download failed: {str(e)}'}, 
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


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
                queryset = File.objects.filter(folder_id=folder_id, owner=request.user)
            else:
                # For public folders owned by others, only show public files
                queryset = File.objects.filter(folder_id=folder_id, is_public=True)
        except Folder.DoesNotExist:
            return Response(
                {'error': 'Folder not found'},
                status=status.HTTP_404_NOT_FOUND
            )
    else:
        # No folder specified - list user's own files
        if include_public:
            # Include user's files AND all public files
            queryset = File.objects.filter(Q(owner=request.user) | Q(is_public=True))
        else:
            # Only user's own files
            queryset = File.objects.filter(owner=request.user)
    
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
                queryset = Folder.objects.filter(parent_id=parent_id, owner=request.user)
            else:
                # For public folders owned by others, only show public subfolders
                queryset = Folder.objects.filter(parent_id=parent_id, is_public=True)
        except Folder.DoesNotExist:
            return Response(
                {'error': 'Parent folder not found'},
                status=status.HTTP_404_NOT_FOUND
            )
    else:
        # No parent specified - list root folders
        if include_public:
            # Include user's folders AND all public folders
            queryset = Folder.objects.filter(Q(owner=request.user) | Q(is_public=True)).filter(parent__isnull=True)
        else:
            # Only user's own root folders
            queryset = Folder.objects.filter(owner=request.user, parent__isnull=True)
    
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
                        with file_obj.open_content('rb') as f:
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
        return Response(
            {'error': f'Download failed: {str(e)}'}, 
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


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
        return Response(
            {'error': f'Failed to get folder info: {str(e)}'}, 
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


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
