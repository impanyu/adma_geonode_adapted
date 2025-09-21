import os
import uuid
from pathlib import Path
from django.db import models
# from django.contrib.gis.db import models as gis_models  # Disabled for now
# from django.contrib.gis.geos import Point
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.conf import settings

User = get_user_model()

def get_upload_path(instance, filename):
    """Generate upload path based on folder structure"""
    if instance.folder:
        return f"uploads/{instance.folder.get_full_path()}/{filename}"
    return f"uploads/{filename}"

class Folder(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    parent = models.ForeignKey(
        'self', 
        on_delete=models.CASCADE, 
        null=True, 
        blank=True, 
        related_name='subfolders'
    )
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name='folders')
    is_public = models.BooleanField(default=False, help_text="Public folders are visible to everyone")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # ChromaDB integration
    chroma_id = models.CharField(max_length=255, null=True, blank=True, unique=True, 
                                help_text="Reference ID in ChromaDB vector database")
    embedding_updated_at = models.DateTimeField(null=True, blank=True,
                                               help_text="Last time embedding was updated")

    class Meta:
        unique_together = ('name', 'parent', 'owner')
        ordering = ['name']

    def __str__(self):
        return self.name

    def get_full_path(self):
        """Get the full path of the folder"""
        if self.parent:
            return f"{self.parent.get_full_path()}/{self.name}"
        return self.name

    def get_breadcrumbs(self):
        """Get list of parent folders for breadcrumb navigation"""
        breadcrumbs = []
        current = self
        while current:
            breadcrumbs.insert(0, current)
            current = current.parent
        return breadcrumbs
    
    def get_public_breadcrumbs(self):
        """Get list of public parent folders for public breadcrumb navigation"""
        breadcrumbs = []
        current = self
        while current and current.is_public:
            breadcrumbs.insert(0, current)
            current = current.parent
        return breadcrumbs

    def get_absolute_url(self):
        return reverse('filemanager:folder_detail', kwargs={'folder_id': self.id})
    
    def get_metadata_for_embedding(self):
        """Generate metadata text for embedding"""
        metadata_parts = [
            f"Folder: {self.name}",
            f"Owner: {self.owner.username}",
            f"Created: {self.created_at.strftime('%Y-%m-%d')}",
            f"Visibility: {'Public' if self.is_public else 'Private'}",
        ]
        
        if self.parent:
            metadata_parts.append(f"Parent folder: {self.parent.name}")
            metadata_parts.append(f"Full path: {self.get_full_path()}")
        
        # Add file count and types
        file_counts = {}
        for file in self.files.all():
            file_type = file.file_type or 'unknown'
            file_counts[file_type] = file_counts.get(file_type, 0) + 1
        
        if file_counts:
            file_summary = []
            for file_type, count in file_counts.items():
                file_summary.append(f"{count} {file_type} files")
            metadata_parts.append(f"Contains: {', '.join(file_summary)}")
        
        subfolder_count = self.subfolders.count()
        if subfolder_count > 0:
            metadata_parts.append(f"Contains {subfolder_count} subfolders")
        
        return " | ".join(metadata_parts)

class File(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    file = models.FileField(upload_to=get_upload_path)
    folder = models.ForeignKey(
        Folder, 
        on_delete=models.CASCADE, 
        null=True, 
        blank=True, 
        related_name='files'
    )
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name='files')
    file_size = models.BigIntegerField(default=0)
    file_type = models.CharField(max_length=50, blank=True)  # 'image', 'text', 'document', 'gis', 'other'
    mime_type = models.CharField(max_length=100, blank=True)
    is_public = models.BooleanField(default=False, help_text="Public files are visible to everyone")
    
    # GIS-specific fields
    is_spatial = models.BooleanField(default=False, help_text="Whether this is a spatial/GIS file")
    geoserver_layer_name = models.CharField(max_length=255, blank=True, null=True, help_text="Layer name in GeoServer")
    geoserver_datastore_name = models.CharField(max_length=255, blank=True, null=True, help_text="Datastore/Coverage store name in GeoServer")
    geoserver_workspace = models.CharField(max_length=100, blank=True, null=True, help_text="GeoServer workspace")
    spatial_extent = models.TextField(null=True, blank=True, help_text="Spatial extent of the data (JSON)")
    crs = models.CharField(max_length=50, blank=True, null=True, help_text="Coordinate Reference System")
    
    # Processing status for GIS files
    GIS_STATUS_CHOICES = [
        ('pending', 'Pending Processing'),
        ('processing', 'Processing'),
        ('processed', 'Processed'),
        ('published', 'Published to GeoServer'),
        ('error', 'Processing Error'),
    ]
    gis_status = models.CharField(max_length=20, choices=GIS_STATUS_CHOICES, default='pending', blank=True)
    processing_log = models.TextField(blank=True, help_text="Log of processing steps")
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # ChromaDB integration
    chroma_id = models.CharField(max_length=255, null=True, blank=True, unique=True, 
                                help_text="Reference ID in ChromaDB vector database")
    embedding_updated_at = models.DateTimeField(null=True, blank=True,
                                               help_text="Last time embedding was updated")

    class Meta:
        unique_together = ('name', 'folder', 'owner')
        ordering = ['name']

    def __str__(self):
        return self.name
    
    def delete(self, *args, **kwargs):
        """Override delete to clean up GeoServer resources"""
        # Clean up GeoServer resources if this is a spatial file
        if self.is_spatial and self.geoserver_layer_name:
            try:
                from .geoserver_manager import SystematicGeoServerManager
                geoserver_manager = SystematicGeoServerManager()
                geoserver_manager.delete_from_geoserver(self)
            except Exception as e:
                # Log error but don't prevent file deletion
                import logging
                logger = logging.getLogger(__name__)
                logger.error(f"Failed to delete GeoServer resources for {self.name}: {e}")
        
        # Clean up ChromaDB embeddings if they exist
        if self.chroma_id:
            try:
                from .embedding_service import EmbeddingService
                embedding_service = EmbeddingService()
                embedding_service.delete_file_embedding(self.chroma_id)
            except Exception as e:
                # Log error but don't prevent file deletion
                import logging
                logger = logging.getLogger(__name__)
                logger.error(f"Failed to delete ChromaDB embedding for {self.name}: {e}")
        
        # Proceed with normal deletion
        super().delete(*args, **kwargs)

    def save(self, *args, **kwargs):
        if self.file:
            # Set file size
            if not self.file_size:
                self.file_size = self.file.size
            
            # Set name from filename if not provided
            if not self.name:
                self.name = Path(self.file.name).stem
            
            # Determine file type based on extension
            file_ext = Path(self.file.name).suffix.lower()
            
            # Check if it's a spatial file (for display purposes)
            if file_ext in getattr(settings, 'ALL_SPATIAL_EXTENSIONS', []):
                self.is_spatial = True
                if not self.geoserver_workspace:
                    self.geoserver_workspace = getattr(settings, 'GEOSERVER_WORKSPACE', 'adma_geo')
            
            # Determine file type for display and processing
            if file_ext == '.csv':
                self.file_type = 'csv'
            elif file_ext in ['.xlsx', '.xls']:
                self.file_type = 'spreadsheet'
            elif file_ext in getattr(settings, 'GIS_FILE_EXTENSIONS', []) or file_ext in getattr(settings, 'ALL_SPATIAL_EXTENSIONS', []):
                self.file_type = 'gis'
            elif file_ext in ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp']:
                self.file_type = 'image'
            elif file_ext in ['.txt', '.md', '.py', '.js', '.html', '.css', '.json', '.xml']:
                self.file_type = 'text'
            elif file_ext in ['.pdf', '.doc', '.docx', '.ppt', '.pptx']:
                self.file_type = 'document'
            else:
                self.file_type = 'other'
        
        super().save(*args, **kwargs)

    def get_size_display(self):
        """Return human-readable file size"""
        size = self.file_size
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} PB"

    def get_icon_class(self):
        """Return FontAwesome icon class based on file type"""
        if self.file_type == 'image':
            return 'fas fa-image image-icon'
        elif self.file_type == 'text':
            return 'fas fa-file-code text-icon'
        elif self.file_type == 'csv':
            return 'fas fa-table csv-icon'
        elif self.file_type == 'spreadsheet':
            return 'fas fa-file-excel spreadsheet-icon'
        elif self.file_type == 'document':
            return 'fas fa-file-alt document-icon'
        elif self.file_type == 'gis':
            return 'fas fa-map-marked-alt gis-icon'
        else:
            return 'fas fa-file file-icon'

    def can_preview(self):
        """Check if file can be previewed in browser"""
        return self.file_type in ['image', 'text', 'csv', 'spreadsheet', 'gis']

    def get_absolute_url(self):
        return reverse('filemanager:file_detail', kwargs={'file_id': self.id})
    
    def get_public_breadcrumbs(self):
        """Get list of public parent folders for public breadcrumb navigation"""
        breadcrumbs = []
        if self.folder:
            # Start from the file's folder and trace up to highest public folder
            current = self.folder
            while current and current.is_public:
                breadcrumbs.insert(0, current)
                current = current.parent
        return breadcrumbs
    
    def get_metadata_for_embedding(self):
        """Generate metadata text for embedding"""
        metadata_parts = [
            f"File: {self.name}",
            f"Type: {self.file_type or 'unknown'}",
            f"Size: {self.get_size_display()}",
            f"Owner: {self.owner.username}",
            f"Created: {self.created_at.strftime('%Y-%m-%d')}",
            f"Visibility: {'Public' if self.is_public else 'Private'}",
        ]
        
        if self.folder:
            metadata_parts.append(f"Folder: {self.folder.name}")
            metadata_parts.append(f"Path: {self.folder.get_full_path()}/{self.name}")
        
        if self.mime_type:
            metadata_parts.append(f"MIME type: {self.mime_type}")
        
        # GIS-specific metadata
        if self.is_spatial:
            metadata_parts.append("Spatial/GIS file")
            if self.crs:
                metadata_parts.append(f"Coordinate system: {self.crs}")
            if self.gis_status:
                metadata_parts.append(f"GIS status: {self.gis_status}")
            if self.geoserver_layer_name:
                metadata_parts.append(f"GeoServer layer: {self.geoserver_layer_name}")
        
        return " | ".join(metadata_parts)
