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
    
    def get_total_size(self):
        """Calculate total size of all files in this folder and its subfolders recursively"""
        total_size = 0
        
        # Add size of all files in this folder
        for file in self.files.all():
            total_size += file.file_size
        
        # Recursively add size of all subfolders
        for subfolder in self.subfolders.all():
            total_size += subfolder.get_total_size()
        
        return total_size
    
    @property
    def file_count(self):
        """Get total count of files in this folder (not including subfolders)"""
        return self.files.count()
    
    @property 
    def subfolder_count(self):
        """Get total count of subfolders in this folder"""
        return self.subfolders.count()

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

    @property
    def maps_containing_this_file(self):
        """Get all maps that contain this file"""
        return [membership.map for membership in self.map_memberships.all()]


class Map(models.Model):
    """
    Custom map that combines multiple spatial files into a composite view.
    Uses GeoServer Layer Groups to combine raster and vector layers.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, help_text="Name of the custom map")
    description = models.TextField(blank=True, null=True, help_text="Description of the map")
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name='maps')
    is_public = models.BooleanField(default=False, help_text="Whether this map is visible to everyone")
    
    # GeoServer integration
    geoserver_layer_group_name = models.CharField(
        max_length=255, 
        unique=True,
        help_text="Unique Layer Group name in GeoServer for this composite map"
    )
    geoserver_workspace = models.CharField(
        max_length=100, 
        default='adma_geo',
        help_text="GeoServer workspace containing the layer group"
    )
    
    # Map metadata
    center_lat = models.FloatField(null=True, blank=True, help_text="Map center latitude")
    center_lng = models.FloatField(null=True, blank=True, help_text="Map center longitude")
    zoom_level = models.IntegerField(default=10, help_text="Default zoom level")
    
    # Bounding box of all files in the map
    bbox_min_lat = models.FloatField(null=True, blank=True, help_text="Minimum latitude of all files")
    bbox_max_lat = models.FloatField(null=True, blank=True, help_text="Maximum latitude of all files")
    bbox_min_lng = models.FloatField(null=True, blank=True, help_text="Minimum longitude of all files")
    bbox_max_lng = models.FloatField(null=True, blank=True, help_text="Maximum longitude of all files")
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        unique_together = ('name', 'owner')

    def __str__(self):
        return f"{self.name} ({self.owner.username})"

    def get_absolute_url(self):
        from django.urls import reverse
        return reverse('filemanager:map_detail', kwargs={'map_id': self.id})

    def get_map_viewer_url(self):
        from django.urls import reverse
        return reverse('filemanager:composite_map_viewer', kwargs={'map_id': self.id})

    def generate_layer_group_name(self):
        """Generate unique layer group name for GeoServer"""
        return f"map_{self.owner.id}_{str(self.id).replace('-', '_')}"

    def save(self, *args, **kwargs):
        if not self.geoserver_layer_group_name:
            self.geoserver_layer_group_name = self.generate_layer_group_name()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Override delete to clean up GeoServer Layer Group"""
        if self.geoserver_layer_group_name:
            try:
                from .geoserver_layer_group_manager import LayerGroupManager
                layer_group_manager = LayerGroupManager()
                layer_group_manager.delete_layer_group(self)
            except Exception as e:
                import logging
                logger = logging.getLogger(__name__)
                logger.error(f"Failed to delete GeoServer Layer Group for map {self.name}: {e}")
        
        super().delete(*args, **kwargs)

    def calculate_and_update_center(self):
        """Calculate center point and zoom level from all file bounding boxes and update the model"""
        import requests
        from requests.auth import HTTPBasicAuth
        from django.conf import settings
        
        map_layers = self.map_layers.filter(
            is_visible=True,
            file__geoserver_layer_name__isnull=False
        ).select_related('file')
        
        if not map_layers:
            # Set default values if no layers
            self.center_lat = 40.0
            self.center_lng = -100.0
            self.zoom_level = 4
            self.save(update_fields=['center_lat', 'center_lng', 'zoom_level'])
            return
        
        min_lat, max_lat = None, None
        min_lng, max_lng = None, None
        
        for map_layer in map_layers:
            file_obj = map_layer.file
            
            try:
                # Query GeoServer for the layer's extent
                file_extension = file_obj.name.split('.')[-1].lower() if '.' in file_obj.name else ''
                if file_extension in ['tif', 'tiff', 'geotif', 'geotiff']:
                    # For TIF files, get coverage info
                    url = f"{settings.GEOSERVER_URL}/rest/workspaces/{file_obj.geoserver_workspace}/coveragestores/{file_obj.geoserver_datastore_name}/coverages/{file_obj.geoserver_layer_name}"
                else:
                    # For vector files, get feature type info
                    url = f"{settings.GEOSERVER_URL}/rest/workspaces/{file_obj.geoserver_workspace}/datastores/{file_obj.geoserver_datastore_name}/featuretypes/{file_obj.geoserver_layer_name}"
                
                headers = {'Accept': 'application/json'} if file_extension not in ['tif', 'tiff', 'geotif', 'geotiff'] else {}
                response = requests.get(url, auth=HTTPBasicAuth('admin', 'geoserver'), headers=headers, timeout=10)
                
                if response.status_code == 200:
                    if file_extension in ['tif', 'tiff', 'geotif', 'geotiff']:
                        # Parse XML response for TIF
                        import xml.etree.ElementTree as ET
                        root = ET.fromstring(response.text)
                        lat_lon_bbox = root.find('.//latLonBoundingBox')
                        if lat_lon_bbox is not None:
                            file_min_lng = float(lat_lon_bbox.find('minx').text)
                            file_max_lng = float(lat_lon_bbox.find('maxx').text)
                            file_min_lat = float(lat_lon_bbox.find('miny').text)
                            file_max_lat = float(lat_lon_bbox.find('maxy').text)
                        else:
                            continue
                    else:
                        # Parse JSON response for shapefiles
                        data = response.json()
                        lat_lon_bbox = data.get('featureType', {}).get('latLonBoundingBox')
                        if lat_lon_bbox:
                            file_min_lng = lat_lon_bbox['minx']
                            file_max_lng = lat_lon_bbox['maxx']
                            file_min_lat = lat_lon_bbox['miny']
                            file_max_lat = lat_lon_bbox['maxy']
                        else:
                            continue
                    
                    # Update overall bounds
                    if min_lat is None or file_min_lat < min_lat:
                        min_lat = file_min_lat
                    if max_lat is None or file_max_lat > max_lat:
                        max_lat = file_max_lat
                    if min_lng is None or file_min_lng < min_lng:
                        min_lng = file_min_lng
                    if max_lng is None or file_max_lng > max_lng:
                        max_lng = file_max_lng
                        
            except Exception as e:
                print(f"Error getting bounds for {file_obj.name}: {e}")
                continue
        
        # Calculate center, zoom, and bounding box
        if min_lat is not None and max_lat is not None and min_lng is not None and max_lng is not None:
            # Store bounding box
            self.bbox_min_lat = min_lat
            self.bbox_max_lat = max_lat
            self.bbox_min_lng = min_lng
            self.bbox_max_lng = max_lng
            
            # Calculate center
            self.center_lat = (min_lat + max_lat) / 2
            self.center_lng = (min_lng + max_lng) / 2
            
            # Calculate appropriate zoom level based on extent
            lat_range = max_lat - min_lat
            lng_range = max_lng - min_lng
            max_range = max(lat_range, lng_range)
            
            # Rough zoom level calculation (this will be overridden by map fit in frontend)
            if max_range > 10:
                self.zoom_level = 4
            elif max_range > 1:
                self.zoom_level = 8
            elif max_range > 0.1:
                self.zoom_level = 12
            elif max_range > 0.01:
                self.zoom_level = 16
            else:
                self.zoom_level = 18
        else:
            # Fallback to default
            self.center_lat = 40.0
            self.center_lng = -100.0
            self.zoom_level = 4
            self.bbox_min_lat = None
            self.bbox_max_lat = None
            self.bbox_min_lng = None
            self.bbox_max_lng = None
        
        # Save the calculated values
        self.save(update_fields=[
            'center_lat', 'center_lng', 'zoom_level',
            'bbox_min_lat', 'bbox_max_lat', 'bbox_min_lng', 'bbox_max_lng'
        ])

    @property
    def layer_count(self):
        """Get the number of layers in this map"""
        return self.map_layers.count()

    @property
    def file_count(self):
        """Get the number of unique files in this map"""
        return self.map_layers.values('file').distinct().count()


class MapLayer(models.Model):
    """
    Individual layer within a composite map.
    Links spatial files to maps and tracks layer ordering and styling.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    map = models.ForeignKey(Map, on_delete=models.CASCADE, related_name='map_layers')
    file = models.ForeignKey(File, on_delete=models.CASCADE, related_name='map_memberships')
    
    # Layer properties
    layer_order = models.IntegerField(
        default=0, 
        help_text="Display order in the map (lower numbers appear on top)"
    )
    opacity = models.FloatField(
        default=1.0, 
        help_text="Layer opacity (0.0 = transparent, 1.0 = opaque)"
    )
    is_visible = models.BooleanField(default=True, help_text="Whether this layer is visible")
    
    # Style information
    style_name = models.CharField(
        max_length=255, 
        blank=True, 
        null=True,
        help_text="GeoServer style name for this layer"
    )
    
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['layer_order', 'added_at']
        unique_together = ('map', 'file')

    def __str__(self):
        return f"{self.file.name} in {self.map.name}"

    @property
    def geoserver_layer_name(self):
        """Get the GeoServer layer name for this file"""
        return self.file.geoserver_layer_name

    @property
    def geoserver_workspace(self):
        """Get the GeoServer workspace for this file"""
        return self.file.geoserver_workspace

    def get_full_layer_name(self):
        """Get the full workspace:layer name for GeoServer"""
        workspace = self.geoserver_workspace or 'adma_geo'
        return f"{workspace}:{self.geoserver_layer_name}"
