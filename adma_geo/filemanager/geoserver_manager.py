"""
Comprehensive GeoServer management with systematic naming and cleanup
"""
import hashlib
import logging
import requests
import xml.etree.ElementTree as ET
from pathlib import Path
from django.conf import settings
from .gis_utils import GeoServerAPI

logger = logging.getLogger(__name__)


class SystematicGeoServerManager:
    """
    Manages GeoServer operations with systematic naming and proper cleanup
    """
    
    def __init__(self):
        self.geoserver_api = GeoServerAPI()
        self.workspace = getattr(settings, 'GEOSERVER_WORKSPACE', 'adma_geo')
        
    def generate_systematic_layer_name(self, file_obj):
        """
        Generate a systematic, predictable layer name that includes path information
        
        Format: {workspace}_{user_id}_{folder_path_hash}_{filename_clean}_{file_id_short}
        
        Args:
            file_obj: File model instance
            
        Returns:
            str: Systematic layer name
        """
        # Get folder path for uniqueness
        folder_path = self._get_folder_path(file_obj.folder)
        folder_hash = self._hash_path(folder_path)
        
        # Clean filename (remove extension and special chars)
        filename_clean = self._clean_filename(file_obj.name)
        
        # Short file ID for absolute uniqueness
        file_id_short = str(file_obj.id).replace('-', '')[:8]
        
        # User ID for multi-tenant separation
        user_id = file_obj.owner.id
        
        # Construct systematic name
        layer_name = f"{self.workspace}_{user_id}_{folder_hash}_{filename_clean}_{file_id_short}"
        
        # Ensure valid GeoServer name (max 60 chars, alphanumeric + underscore)
        layer_name = ''.join(c for c in layer_name if c.isalnum() or c == '_')[:60]
        
        logger.info(f"Generated systematic layer name: {file_obj.name} -> {layer_name}")
        return layer_name
    
    def _get_folder_path(self, folder):
        """Get full folder path for hashing"""
        if not folder:
            return "root"
        
        path_parts = []
        current = folder
        while current:
            path_parts.append(current.name)
            current = current.parent
        
        # Reverse to get root-to-leaf path
        return "/".join(reversed(path_parts))
    
    def _hash_path(self, path):
        """Generate short hash of folder path"""
        return hashlib.md5(path.encode()).hexdigest()[:6]
    
    def _clean_filename(self, filename):
        """Clean filename for use in layer name"""
        # Remove extension
        name = Path(filename).stem
        # Replace special chars with underscore
        name = ''.join(c if c.isalnum() else '_' for c in name)
        # Remove multiple underscores
        while '__' in name:
            name = name.replace('__', '_')
        # Trim to reasonable length
        return name[:20]
    
    def publish_file_to_geoserver(self, file_obj):
        """
        Publish file to GeoServer using temporary renamed files with systematic naming
        
        Strategy:
        1. Create temporary renamed copy with systematic naming
        2. Upload the renamed temp file to GeoServer
        3. Capture exact layer/datastore names from GeoServer
        4. Store metadata in original file object
        5. Clean up temporary file
        
        Args:
            file_obj: File model instance
            
        Returns:
            tuple: (success: bool, message: str, layer_name: str)
        """
        import tempfile
        import shutil
        
        try:
            # Generate systematic name for the temporary file
            systematic_name = self.generate_systematic_layer_name(file_obj)
            
            # Ensure workspace exists
            if not self.geoserver_api.create_workspace():
                return False, "Failed to create/verify GeoServer workspace", None
            
            file_path = file_obj.file.path
            file_ext = Path(file_path).suffix.lower()
            
            logger.info(f"Publishing with temporary renamed file strategy: {file_obj.name}")
            logger.info(f"Systematic name: {systematic_name}")
            
            # Create temporary renamed file
            with tempfile.TemporaryDirectory() as temp_dir:
                if file_ext in ['.tif', '.tiff', '.geotiff', '.geotif']:
                    success, actual_layer_name, actual_datastore_name = self._publish_raster_with_temp_rename(
                        systematic_name, file_path, temp_dir
                    )
                    layer_type = "raster"
                elif file_ext == '.shp':
                    success, actual_layer_name, actual_datastore_name = self._publish_shapefile_with_temp_rename(
                        systematic_name, file_obj, temp_dir
                    )
                    layer_type = "vector"
                else:
                    return False, f"Unsupported file type: {file_ext}", None
                
                if success and actual_layer_name and actual_datastore_name:
                    # Store complete metadata from GeoServer
                    file_obj.geoserver_layer_name = actual_layer_name
                    file_obj.geoserver_datastore_name = actual_datastore_name
                    file_obj.geoserver_workspace = self.workspace
                    file_obj.gis_status = 'published'
                    file_obj.processing_log += f"\\nPublished to GeoServer as {layer_type} layer: {actual_layer_name}"
                    file_obj.processing_log += f"\\nStored metadata - Layer: {actual_layer_name}, Datastore: {actual_datastore_name}, Workspace: {self.workspace}"
                
                    # Try to get spatial extent
                    try:
                        extent = self.geoserver_api.get_layer_extent(actual_layer_name)
                        if extent:
                            import json
                            file_obj.spatial_extent = json.dumps(extent)
                            file_obj.processing_log += f"\\nSpatial extent retrieved"
                    except Exception as e:
                        logger.warning(f"Could not get spatial extent: {e}")
                    
                    file_obj.save()
                    
                    return True, f"Successfully published {layer_type} layer: {actual_layer_name}", actual_layer_name
                else:
                    return False, f"Failed to publish {layer_type} to GeoServer", None
                    
            # Cleanup happens automatically when temp_dir context exits
                
        except Exception as e:
            logger.error(f"Error publishing file to GeoServer: {e}")
            return False, f"Error publishing to GeoServer: {str(e)}", None
    
    def _publish_raster_with_temp_rename(self, systematic_name, original_file_path, temp_dir):
        """
        Publish raster file using temporary renamed copy
        
        Returns:
            tuple: (success: bool, actual_layer_name: str, actual_datastore_name: str)
        """
        try:
            import shutil
            from pathlib import Path
            
            # Create temporary renamed file
            original_ext = Path(original_file_path).suffix
            temp_file_name = f"{systematic_name}{original_ext}"
            temp_file_path = Path(temp_dir) / temp_file_name
            
            logger.info(f"Creating temporary renamed raster: {temp_file_path}")
            
            # Copy original file to temp location with systematic name
            shutil.copy2(original_file_path, temp_file_path)
            
            # Upload the renamed temporary file to GeoServer
            success = self.geoserver_api.create_coverage_store_from_file(systematic_name, str(temp_file_path))
            
            if success:
                # For raster files, layer name and coverage store name are typically the same as what we specified
                actual_layer_name = systematic_name
                actual_datastore_name = systematic_name  # Coverage store name
                
                logger.info(f"Raster published successfully:")
                logger.info(f"  Layer: {actual_layer_name}")
                logger.info(f"  Coverage store: {actual_datastore_name}")
                
                return True, actual_layer_name, actual_datastore_name
            else:
                logger.error(f"Failed to publish renamed raster file")
                return False, None, None
                
        except Exception as e:
            logger.error(f"Error in raster temp rename publish: {e}")
            return False, None, None

    def _publish_shapefile_with_temp_rename(self, systematic_name, file_obj, temp_dir):
        """
        Publish shapefile using temporary renamed copy with all components
        
        Returns:
            tuple: (success: bool, actual_layer_name: str, actual_datastore_name: str)
        """
        try:
            import shutil
            from pathlib import Path
            
            # Find all shapefile components
            base_name = file_obj.name.replace('.shp', '')
            
            # Find all related components
            from .models import File
            components = File.objects.filter(
                owner=file_obj.owner,
                name__startswith=base_name,
                folder=file_obj.folder
            )
            
            logger.info(f"Creating temporary renamed shapefile bundle: {systematic_name}")
            
            # Copy all components with systematic naming
            component_files = {}
            for comp in components:
                parts = comp.name.split('.')
                if len(parts) >= 2:
                    ext = '.' + parts[-1].lower()
                    # Copy to temp directory with systematic name
                    temp_component_name = f"{systematic_name}{ext}"
                    temp_component_path = Path(temp_dir) / temp_component_name
                    
                    shutil.copy2(comp.file.path, temp_component_path)
                    component_files[ext] = str(temp_component_path)
                    
                    logger.info(f"  Copied component: {comp.name} -> {temp_component_name}")
            
            # Check required components
            required_exts = ['.shp', '.shx', '.dbf']
            missing = [ext for ext in required_exts if ext not in component_files]
            
            if missing:
                logger.error(f"Missing required shapefile components: {missing}")
                return False, None, None
            
            # Upload the main shapefile to GeoServer using the systematic name
            main_shp_path = component_files['.shp']
            success = self.geoserver_api.upload_shapefile(systematic_name, main_shp_path)
            
            if success:
                # For shapefiles uploaded with systematic naming, check what GeoServer actually created
                actual_layer_name, actual_datastore_name = self._verify_shapefile_creation(systematic_name)
                
                if actual_layer_name and actual_datastore_name:
                    logger.info(f"Shapefile published successfully:")
                    logger.info(f"  Layer: {actual_layer_name}")
                    logger.info(f"  Datastore: {actual_datastore_name}")
                    
                    return True, actual_layer_name, actual_datastore_name
                else:
                    logger.error(f"Could not verify shapefile creation in GeoServer")
                    return False, None, None
            else:
                logger.error(f"Failed to upload renamed shapefile to GeoServer")
                return False, None, None
                
        except Exception as e:
            logger.error(f"Error in shapefile temp rename publish: {e}")
            return False, None, None

    def _verify_shapefile_creation(self, expected_name):
        """
        Verify what layer and datastore names were actually created for the shapefile
        
        Returns:
            tuple: (actual_layer_name: str, actual_datastore_name: str)
        """
        try:
            import requests
            from django.conf import settings
            
            # Check if the expected layer exists
            layer_test_success = self._test_layer_exists(expected_name)
            
            if layer_test_success:
                # If expected name works, use it
                return expected_name, expected_name
            else:
                # Check for auto-numbered variations
                for i in range(1, 10):
                    test_name = f"{expected_name}{i}"
                    if self._test_layer_exists(test_name):
                        logger.info(f"Found auto-numbered layer: {test_name}")
                        return test_name, test_name
                
                logger.error(f"Could not find working layer for {expected_name}")
                return None, None
                
        except Exception as e:
            logger.error(f"Error verifying shapefile creation: {e}")
            return None, None

    def _test_layer_exists(self, layer_name):
        """Test if a layer exists and works in GeoServer WMS"""
        try:
            import requests
            from django.conf import settings
            
            wms_url = f'{settings.GEOSERVER_URL}/wms'
            params = {
                'SERVICE': 'WMS',
                'VERSION': '1.1.1',
                'REQUEST': 'GetMap',
                'LAYERS': f'{self.workspace}:{layer_name}',
                'STYLES': '',
                'FORMAT': 'image/png',
                'TRANSPARENT': 'true',
                'SRS': 'EPSG:4326',
                'BBOX': '-180,-90,180,90',
                'WIDTH': '64',
                'HEIGHT': '64'
            }
            
            response = requests.get(wms_url, params=params, timeout=10)
            
            if response.status_code == 200:
                content_type = response.headers.get('Content-Type', '')
                return 'image' in content_type
            
            return False
            
        except Exception:
            return False

    def _publish_raster(self, layer_name, file_path):
        """Legacy raster publish method (kept for backwards compatibility)"""
        return self.geoserver_api.create_coverage_store_from_file(layer_name, file_path)
    
    def _publish_shapefile(self, layer_name, file_obj):
        """
        Publish shapefile to GeoServer with automatic layer name detection:
        1. Use legacy bundling to upload shapefile
        2. Detect the actual layer name created by GeoServer
        3. Update database with the correct layer name
        """
        try:
            # For shapefiles, we need to use the legacy approach because
            # GeoServer creates layers with the file's base name, not our systematic name
            from .gis_utils import bundle_and_publish_shapefile_legacy
            
            logger.info(f"Publishing shapefile with auto-detection: {file_obj.name}")
            
            # Get layer list before publishing
            layers_before = self._get_workspace_layers()
            
            # Use legacy method to publish
            success, message = bundle_and_publish_shapefile_legacy(file_obj)
            
            if success:
                # Legacy method claims success, but we need to find the actual layer name
                # because GeoServer auto-numbers layers
                
                # Get layer list after publishing
                layers_after = self._get_workspace_layers()
                
                # Find new layers
                new_layers = set(layers_after) - set(layers_before)
                
                # Look for layers that match our file
                base_name = file_obj.name.replace('.shp', '')
                actual_layer_name = None
                
                # First check for exact base name matches in new layers
                for new_layer in new_layers:
                    if new_layer.startswith(base_name):
                        actual_layer_name = new_layer
                        logger.info(f"Found new layer from upload: {actual_layer_name}")
                        break
                
                # If no new layer found, try to detect by testing variations
                if not actual_layer_name:
                    actual_layer_name = self._detect_working_layer(base_name)
                
                if actual_layer_name:
                    # For shapefiles, datastore name typically matches layer name
                    actual_datastore_name = actual_layer_name
                    
                    # Update database with complete GeoServer metadata
                    old_layer_name = file_obj.geoserver_layer_name
                    file_obj.geoserver_layer_name = actual_layer_name
                    file_obj.geoserver_datastore_name = actual_datastore_name
                    file_obj.save()
                    
                    logger.info(f"Shapefile metadata detected and updated:")
                    logger.info(f"  File: {file_obj.name}")
                    logger.info(f"  Old layer name: {old_layer_name}")
                    logger.info(f"  Actual layer name: {actual_layer_name}")
                    logger.info(f"  Actual datastore name: {actual_datastore_name}")
                    
                    return True
                else:
                    logger.error(f"Could not detect actual layer name for {file_obj.name}")
                    return False
            else:
                logger.error(f"Legacy shapefile publishing failed: {message}")
                return False
                
        except Exception as e:
            logger.error(f"Error publishing shapefile: {e}")
            return False
    
    def _get_workspace_layers(self):
        """Get all layer names in the workspace"""
        try:
            import requests
            from django.conf import settings
            
            wms_url = f"{settings.GEOSERVER_URL}/wms?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetCapabilities"
            response = requests.get(wms_url, timeout=10)
            
            if response.status_code == 200:
                import xml.etree.ElementTree as ET
                root = ET.fromstring(response.content)
                ns = {'wms': 'http://www.opengis.net/wms'}
                layers = [element.text for element in root.findall('.//wms:Layer/wms:Name', ns)]
                
                # Filter for our workspace
                workspace_layers = []
                for layer in layers:
                    if layer.startswith('adma_geo:'):
                        workspace_layers.append(layer.replace('adma_geo:', ''))
                
                return workspace_layers
            else:
                logger.error(f"Failed to get WMS capabilities: {response.status_code}")
                return []
                
        except Exception as e:
            logger.error(f"Error getting workspace layers: {e}")
            return []
    
    def _detect_working_layer(self, base_name):
        """Detect the actual working layer name by testing variations"""
        try:
            import requests
            from django.conf import settings
            
            wms_url = f'{settings.GEOSERVER_URL}/wms'
            
            # Test numbered variations
            for i in range(1, 25):  # Test up to 25 variations
                test_layer = f'{base_name}{i}'
                
                params = {
                    'SERVICE': 'WMS',
                    'VERSION': '1.1.1',
                    'REQUEST': 'GetMap',
                    'LAYERS': f'adma_geo:{test_layer}',
                    'STYLES': '',
                    'FORMAT': 'image/png',
                    'TRANSPARENT': 'true',
                    'SRS': 'EPSG:4326',
                    'BBOX': '-180,-90,180,90',
                    'WIDTH': '128',
                    'HEIGHT': '128'
                }
                
                try:
                    response = requests.get(wms_url, params=params, timeout=5)
                    
                    if response.status_code == 200:
                        content_type = response.headers.get('Content-Type', '')
                        if 'image' in content_type:
                            logger.info(f"Detected working layer: {test_layer}")
                            return test_layer
                            
                except Exception:
                    continue
            
            # If numbered variations don't work, try the base name
            params = {
                'SERVICE': 'WMS',
                'VERSION': '1.1.1',
                'REQUEST': 'GetMap',
                'LAYERS': f'adma_geo:{base_name}',
                'STYLES': '',
                'FORMAT': 'image/png',
                'TRANSPARENT': 'true',
                'SRS': 'EPSG:4326',
                'BBOX': '-180,-90,180,90',
                'WIDTH': '128',
                'HEIGHT': '128'
            }
            
            try:
                response = requests.get(wms_url, params=params, timeout=5)
                if response.status_code == 200 and 'image' in response.headers.get('Content-Type', ''):
                    logger.info(f"Detected working layer: {base_name}")
                    return base_name
            except Exception:
                pass
            
            logger.error(f"Could not detect working layer for base name: {base_name}")
            return None
            
        except Exception as e:
            logger.error(f"Error detecting layer name: {e}")
            return None
    
    def delete_from_geoserver(self, file_obj):
        """
        Delete layer and datastore from GeoServer when file is deleted
        
        Args:
            file_obj: File model instance
            
        Returns:
            bool: Success status
        """
        if not file_obj.geoserver_layer_name:
            return True  # Nothing to delete
        
        try:
            workspace = file_obj.geoserver_workspace or self.workspace
            layer_name = file_obj.geoserver_layer_name
            
            logger.info(f"Deleting GeoServer resources for: {layer_name}")
            
            success = True
            
            # Delete layer
            layer_url = f"{settings.GEOSERVER_URL}/rest/workspaces/{workspace}/layers/{layer_name}"
            response = requests.delete(layer_url, auth=self.geoserver_api.auth)
            
            if response.status_code in [200, 404]:  # 404 = already deleted
                logger.info(f"Layer deleted: {layer_name}")
            else:
                logger.warning(f"Failed to delete layer {layer_name}: {response.status_code}")
                success = False
            
            # Delete datastore/coveragestore using stored metadata
            datastore_name = file_obj.geoserver_datastore_name or layer_name  # Fallback to layer name
            file_ext = Path(file_obj.name).suffix.lower()
            
            if file_ext in ['.tif', '.tiff', '.geotiff', '.geotif']:
                # Delete coverage store
                store_url = f"{settings.GEOSERVER_URL}/rest/workspaces/{workspace}/coveragestores/{datastore_name}"
                response = requests.delete(f"{store_url}?recurse=true", auth=self.geoserver_api.auth)
                store_type = "coverage store"
            else:
                # Delete datastore  
                store_url = f"{settings.GEOSERVER_URL}/rest/workspaces/{workspace}/datastores/{datastore_name}"
                response = requests.delete(f"{store_url}?recurse=true", auth=self.geoserver_api.auth)
                store_type = "datastore"
            
            if response.status_code in [200, 404]:
                logger.info(f"{store_type.title()} deleted: {datastore_name}")
            else:
                logger.warning(f"Failed to delete {store_type} {datastore_name}: {response.status_code}")
                success = False
            
            return success
            
        except Exception as e:
            logger.error(f"Error deleting from GeoServer: {e}")
            return False
    
    def check_layer_exists(self, layer_name):
        """Check if layer exists in GeoServer WMS capabilities"""
        try:
            wms_url = f"{settings.GEOSERVER_URL}/wms?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetCapabilities"
            response = requests.get(wms_url, timeout=5)
            
            if response.status_code == 200:
                root = ET.fromstring(response.content)
                ns = {'wms': 'http://www.opengis.net/wms'}
                wms_layers = [element.text for element in root.findall('.//wms:Layer/wms:Name', ns)]
                
                full_layer_name = f"{self.workspace}:{layer_name}"
                return full_layer_name in wms_layers
            
            return False
            
        except Exception as e:
            logger.error(f"Error checking layer existence: {e}")
            return False
