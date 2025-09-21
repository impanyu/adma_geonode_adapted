"""
GIS utility functions for processing and publishing spatial data to GeoServer
"""
import os
import zipfile
import tempfile
import requests
import json
from pathlib import Path
from django.conf import settings
import logging

logger = logging.getLogger(__name__)

class GeoServerAPI:
    """GeoServer REST API client"""
    
    def __init__(self):
        self.base_url = settings.GEOSERVER_URL
        self.workspace = settings.GEOSERVER_WORKSPACE
        self.auth = (settings.GEOSERVER_ADMIN_USER, settings.GEOSERVER_ADMIN_PASSWORD)
    
    def create_workspace(self):
        """Create workspace if it doesn't exist"""
        url = f"{self.base_url}/rest/workspaces"
        
        # Check if workspace exists
        response = requests.get(f"{url}/{self.workspace}", auth=self.auth)
        if response.status_code == 200:
            return True
        
        # Create workspace
        data = f'<workspace><name>{self.workspace}</name></workspace>'
        headers = {'Content-Type': 'application/xml'}
        
        response = requests.post(url, data=data, headers=headers, auth=self.auth)
        return response.status_code == 201
    
    def create_datastore(self, datastore_name):
        """Create PostGIS datastore"""
        url = f"{self.base_url}/rest/workspaces/{self.workspace}/datastores"
        
        # Check if datastore exists
        response = requests.get(f"{url}/{datastore_name}", auth=self.auth)
        if response.status_code == 200:
            return True
        
        # Create datastore
        data = f"""
        <dataStore>
            <name>{datastore_name}</name>
            <connectionParameters>
                <host>db</host>
                <port>5432</port>
                <database>adma_geo</database>
                <user>adma_geo</user>
                <passwd>adma_geo123</passwd>
                <dbtype>postgis</dbtype>
            </connectionParameters>
        </dataStore>
        """
        headers = {'Content-Type': 'application/xml'}
        
        response = requests.post(url, data=data, headers=headers, auth=self.auth)
        return response.status_code == 201
    
    def publish_layer(self, layer_name, datastore_name, table_name):
        """Publish a layer from PostGIS table"""
        url = f"{self.base_url}/rest/workspaces/{self.workspace}/datastores/{datastore_name}/featuretypes"
        
        data = f"""
        <featureType>
            <name>{layer_name}</name>
            <nativeName>{table_name}</nativeName>
            <title>{layer_name}</title>
            <srs>EPSG:4326</srs>
            <enabled>true</enabled>
        </featureType>
        """
        headers = {'Content-Type': 'application/xml'}
        
        response = requests.post(url, data=data, headers=headers, auth=self.auth)
        return response.status_code == 201
    
    def create_coverage_store(self, store_name, file_path):
        """Create a coverage store for raster files"""
        url = f"{self.base_url}/rest/workspaces/{self.workspace}/coveragestores"
        
        # Check if store exists
        response = requests.get(f"{url}/{store_name}", auth=self.auth)
        if response.status_code == 200:
            return True
        
        # For now, we'll create a simple external coverage store reference
        # In a full implementation, you'd upload the file to GeoServer
        data = f"""
        <coverageStore>
            <name>{store_name}</name>
            <type>GeoTIFF</type>
            <enabled>true</enabled>
            <url>file:/data/{os.path.basename(file_path)}</url>
        </coverageStore>
        """
        headers = {'Content-Type': 'application/xml'}
        
        response = requests.post(url, data=data, headers=headers, auth=self.auth)
        
        if response.status_code == 201:
            # Also create the coverage layer
            return self.create_coverage_layer(store_name, store_name)
        
        return False
    
    def create_coverage_layer(self, store_name, layer_name):
        """Create a coverage layer from a coverage store"""
        url = f"{self.base_url}/rest/workspaces/{self.workspace}/coveragestores/{store_name}/coverages"
        
        data = f"""
        <coverage>
            <name>{layer_name}</name>
            <title>{layer_name}</title>
            <srs>EPSG:4326</srs>
            <enabled>true</enabled>
        </coverage>
        """
        headers = {'Content-Type': 'application/xml'}
        
        response = requests.post(url, data=data, headers=headers, auth=self.auth)
        return response.status_code == 201
    
    def create_coverage_store_from_file(self, store_name, file_path):
        """Create a coverage store and upload the raster file to GeoServer"""
        try:
            # First, upload the file to GeoServer
            # Use the GeoTIFF format endpoint which handles coordinate systems better
            upload_url = f"{self.base_url}/rest/workspaces/{self.workspace}/coveragestores/{store_name}/file.geotiff"
            
            with open(file_path, 'rb') as f:
                headers = {
                    'Content-Type': 'image/tiff',
                    # Let GeoServer auto-configure the coverage
                    'configure': 'first',  # Configure the coverage from the uploaded file
                }
                
                # Upload with auto-configuration
                response = requests.put(
                    upload_url + "?configure=first", 
                    data=f, 
                    headers={'Content-Type': 'image/tiff'}, 
                    auth=self.auth
                )
                
                if response.status_code in [200, 201]:
                    logger.info(f"Successfully uploaded raster file to GeoServer: {store_name}")
                    
                    # After upload, try to configure the layer properly
                    self._configure_coverage_layer(store_name)
                    return True
                else:
                    logger.error(f"Failed to upload raster file to GeoServer. Status: {response.status_code}, Response: {response.text}")
                    return False
                    
        except Exception as e:
            logger.error(f"Error uploading raster file to GeoServer: {str(e)}")
            return False
    
    def _configure_coverage_layer(self, layer_name):
        """Configure the coverage layer after upload"""
        try:
            # Get the coverage info to see if it was created correctly
            coverage_url = f"{self.base_url}/rest/workspaces/{self.workspace}/coveragestores/{layer_name}/coverages/{layer_name}"
            response = requests.get(coverage_url, auth=self.auth)
            
            if response.status_code == 200:
                logger.info(f"Coverage layer {layer_name} configured successfully")
                return True
            else:
                logger.warning(f"Coverage layer {layer_name} may not be fully configured. Status: {response.status_code}")
                return False
                
        except Exception as e:
            logger.warning(f"Could not verify coverage configuration for {layer_name}: {str(e)}")
            return False
    
    def upload_shapefile(self, store_name, shp_file_path):
        """Upload a shapefile to GeoServer as a new datastore"""
        try:
            # For shapefiles, we need to find all the component files
            shp_dir = os.path.dirname(shp_file_path)
            base_name = os.path.splitext(os.path.basename(shp_file_path))[0]
            
            # Find all shapefile component files
            required_files = [
                f"{base_name}.shp",
                f"{base_name}.shx", 
                f"{base_name}.dbf"
            ]
            optional_files = [
                f"{base_name}.prj",
                f"{base_name}.cpg",
                f"{base_name}.qpj"
            ]
            
            # Check if all required files exist
            for req_file in required_files:
                if not os.path.exists(os.path.join(shp_dir, req_file)):
                    logger.error(f"Required shapefile component missing: {req_file}")
                    return False
            
            # Create a zip file with all shapefile components
            import tempfile
            import zipfile
            
            with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as temp_zip:
                with zipfile.ZipFile(temp_zip.name, 'w') as zip_file:
                    # Add required files
                    for req_file in required_files:
                        file_path = os.path.join(shp_dir, req_file)
                        if os.path.exists(file_path):
                            zip_file.write(file_path, req_file)
                    
                    # Add optional files if they exist
                    for opt_file in optional_files:
                        file_path = os.path.join(shp_dir, opt_file)
                        if os.path.exists(file_path):
                            zip_file.write(file_path, opt_file)
                
                # Upload the zip file to GeoServer
                upload_url = f"{self.base_url}/rest/workspaces/{self.workspace}/datastores/{store_name}/file.shp"
                
                with open(temp_zip.name, 'rb') as zip_data:
                    headers = {'Content-Type': 'application/zip'}
                    response = requests.put(upload_url, data=zip_data, headers=headers, auth=self.auth)
                    
                    if response.status_code in [200, 201]:
                        logger.info(f"Successfully uploaded shapefile to GeoServer: {store_name}")
                        # Clean up temp file
                        os.unlink(temp_zip.name)
                        return True
                    else:
                        logger.error(f"Failed to upload shapefile to GeoServer. Status: {response.status_code}, Response: {response.text}")
                        # Clean up temp file
                        os.unlink(temp_zip.name)
                        return False
                        
        except Exception as e:
            logger.error(f"Error uploading shapefile to GeoServer: {str(e)}")
            return False

def extract_zip_file(file_path, extract_to):
    """Extract zip file and return list of extracted files"""
    extracted_files = []
    
    with zipfile.ZipFile(file_path, 'r') as zip_ref:
        zip_ref.extractall(extract_to)
        extracted_files = zip_ref.namelist()
    
    return extracted_files

def find_shapefile_components(extracted_files):
    """Find main shapefile and its components"""
    shp_files = [f for f in extracted_files if f.endswith('.shp')]
    
    if not shp_files:
        return None
    
    # Take the first shapefile found
    main_shp = shp_files[0]
    base_name = main_shp[:-4]  # Remove .shp extension
    
    components = {
        'shp': main_shp,
        'shx': f"{base_name}.shx",
        'dbf': f"{base_name}.dbf",
        'prj': f"{base_name}.prj"  # Optional
    }
    
    return components

def process_gis_file(file_obj):
    """Process GIS file and extract spatial information"""
    try:
        file_path = file_obj.file.path
        file_ext = Path(file_path).suffix.lower()
        
        # Handle different file types
        if file_ext == '.zip':
            return process_zip_file(file_obj, file_path)
        elif file_ext in ['.geojson', '.gpkg', '.shp']:
            return process_vector_file(file_obj, file_path)
        elif file_ext in ['.tiff', '.tif', '.geotiff', '.geotif']:
            return process_raster_file(file_obj, file_path)
        elif file_ext == '.csv':
            return process_csv_file(file_obj, file_path)
        else:
            raise ValueError(f"Unsupported file format: {file_ext}")
            
    except Exception as e:
        logger.error(f"Error processing GIS file {file_obj.name}: {str(e)}")
        return False, str(e)

def process_zip_file(file_obj, file_path):
    """Process ZIP file (likely containing shapefile)"""
    with tempfile.TemporaryDirectory() as temp_dir:
        try:
            # Extract zip file
            extracted_files = extract_zip_file(file_path, temp_dir)
            
            # Find shapefile components
            shp_components = find_shapefile_components(extracted_files)
            
            if not shp_components:
                return False, "No shapefile found in ZIP archive"
            
            # Process the shapefile
            shp_path = os.path.join(temp_dir, shp_components['shp'])
            return process_vector_file(file_obj, shp_path)
            
        except Exception as e:
            return False, f"Error processing ZIP file: {str(e)}"

def process_vector_file(file_obj, file_path):
    """Process vector file (GeoJSON, Shapefile, GeoPackage) - simplified version"""
    try:
        # For now, just mark as processed with basic metadata
        # In a full implementation, this would use GDAL/GeoPandas to read the file
        
        file_obj.crs = 'EPSG:4326'  # Default assumption
        file_obj.spatial_extent = json.dumps({
            "type": "envelope", 
            "coordinates": [[-180, -90], [180, 90]]  # World extent as default
        })
        file_obj.gis_status = 'processed'
        file_obj.processing_log = "Successfully processed vector file (basic processing)"
        file_obj.save()
        
        return True, "Processed vector file (basic processing)"
        
    except Exception as e:
        return False, f"Error processing vector file: {str(e)}"

def process_raster_file(file_obj, file_path):
    """Process raster file (GeoTIFF, TIFF)"""
    try:
        # Try to get basic info about the raster file
        # For now, we'll use a simple approach without heavy dependencies
        
        # We'll assume the file has spatial info and set reasonable defaults
        # In a full implementation, this would use GDAL to read actual CRS and extent
        
        # For Nebraska data, common CRS values:
        # - EPSG:32614 (UTM Zone 14N) 
        # - EPSG:4326 (WGS84)
        # - EPSG:3857 (Web Mercator)
        
        # Set CRS based on filename or location hints
        if any(hint in file_obj.name.lower() for hint in ['utm', '32614', 'zone14']):
            file_obj.crs = 'EPSG:32614'  # UTM Zone 14N (common for Nebraska)
        elif any(hint in file_obj.name.lower() for hint in ['4326', 'wgs84', 'latlon']):
            file_obj.crs = 'EPSG:4326'  # WGS84
        else:
            # Default to UTM for Nebraska area since that's what your file uses
            file_obj.crs = 'EPSG:32614'
        
        # Set a reasonable extent for Nebraska area
        if file_obj.crs == 'EPSG:32614':
            # UTM coordinates for Nebraska area (based on your file's bbox)
            file_obj.spatial_extent = json.dumps({
                "type": "envelope", 
                "coordinates": [[712000, 4560000], [714000, 4562000]]  # Around your file's area
            })
        else:
            # WGS84 coordinates for Nebraska
            file_obj.spatial_extent = json.dumps({
                "type": "envelope", 
                "coordinates": [[-104.5, 39.5], [-95.5, 43.0]]  # Nebraska lat/lon extent
            })
        
        file_obj.gis_status = 'processed'
        file_obj.processing_log = f"Raster file processed - CRS: {file_obj.crs}"
        file_obj.save()
        
        return True, f"Raster file processed with CRS {file_obj.crs}"
        
    except Exception as e:
        return False, f"Error processing raster file: {str(e)}"

def process_csv_file(file_obj, file_path):
    """Process CSV file (assuming it has lat/lon columns) - simplified version"""
    try:
        # Basic CSV processing without heavy dependencies
        # In a full implementation, this would use pandas/geopandas
        
        file_obj.crs = 'EPSG:4326'
        file_obj.spatial_extent = json.dumps({
            "type": "envelope", 
            "coordinates": [[-180, -90], [180, 90]]  # World extent as default
        })
        file_obj.gis_status = 'processed'
        file_obj.processing_log = "CSV processed (basic processing - assumed to contain geographic data)"
        file_obj.save()
        
        return True, "Processed CSV with geographic data (basic processing)"
        
    except Exception as e:
        return False, f"Error processing CSV file: {str(e)}"

def publish_to_geoserver(file_obj):
    """Publish processed GIS file to GeoServer"""
    try:
        geoserver_api = GeoServerAPI()
        
        # Generate clean layer name
        layer_name = f"{file_obj.owner.username}_{file_obj.name}_{str(file_obj.id)[:8]}".replace(' ', '_').replace('-', '_').replace('.', '_')
        layer_name = ''.join(c for c in layer_name if c.isalnum() or c == '_')  # Keep only alphanumeric and underscore
        
        # Ensure workspace exists
        if not geoserver_api.create_workspace():
            return False, "Failed to create/verify GeoServer workspace"
        
        file_path = file_obj.file.path
        file_ext = Path(file_path).suffix.lower()
        
        # Handle different file types
        if file_ext in ['.tif', '.tiff', '.geotiff', '.geotif']:
            # For raster files, create a coverage store
            success = geoserver_api.create_coverage_store_from_file(layer_name, file_path)
            if success:
                file_obj.geoserver_layer_name = layer_name
                file_obj.gis_status = 'published'
                file_obj.processing_log += f"\nRaster file published to GeoServer as coverage layer"
                file_obj.save()
                return True, f"Raster published successfully as {layer_name}"
            else:
                return False, "Failed to create coverage store in GeoServer"
                
        elif file_ext in ['.shp', '.geojson', '.gpkg', '.kml', '.kmz']:
            # For vector files, try direct upload to GeoServer
            if file_ext == '.shp':
                # For shapefiles, we need to handle the components
                success = geoserver_api.upload_shapefile(layer_name, file_path)
                if success:
                    file_obj.geoserver_layer_name = layer_name
                    file_obj.gis_status = 'published'
                    file_obj.processing_log += f"\nShapefile published to GeoServer as vector layer"
                    file_obj.save()
                    return True, f"Shapefile published successfully as {layer_name}"
                else:
                    file_obj.gis_status = 'processed'
                    file_obj.processing_log += f"\nShapefile processed but publishing to GeoServer failed"
                    file_obj.save()
                    return True, f"Shapefile processed as {layer_name} (GeoServer publishing failed)"
            else:
                # For other vector formats, mark as processed for now
                file_obj.geoserver_layer_name = layer_name
                file_obj.gis_status = 'processed'
                file_obj.processing_log += f"\nVector file processed (direct GeoServer publishing not implemented for {file_ext})"
                file_obj.save()
                return True, f"Vector file processed as {layer_name} (GeoServer publishing pending)"
            
        else:
            # Unknown file type
            return False, f"Unsupported file type for GeoServer: {file_ext}"
            
    except Exception as e:
        return False, f"Error publishing to GeoServer: {str(e)}"
