import requests
import logging
from django.conf import settings
from django.core.management.base import BaseCommand
from filemanager.models import File

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Enable disabled shapefile layers in GeoServer for WMS access'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be done without making changes',
        )

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS('=== Enable Shapefile Layers Tool ==='))
        
        dry_run = options['dry_run']
        
        if dry_run:
            self.stdout.write(self.style.WARNING('🔍 DRY RUN MODE - No changes will be made'))
        
        auth = (settings.GEOSERVER_ADMIN_USER, settings.GEOSERVER_ADMIN_PASSWORD)
        rest_url = f"{settings.GEOSERVER_URL}/rest"
        
        # Find all published shapefiles
        shapefiles = File.objects.filter(
            name__iendswith='.shp', 
            is_spatial=True, 
            gis_status='published',
            geoserver_layer_name__isnull=False
        )
        
        self.stdout.write(f'\n📋 Found {shapefiles.count()} published shapefiles to check')
        
        enabled_count = 0
        failed_count = 0
        already_enabled = 0
        
        for file_obj in shapefiles:
            self.stdout.write(f'\n🔍 Processing: {file_obj.name}')
            
            layer_name = file_obj.geoserver_layer_name
            workspace = file_obj.geoserver_workspace or 'adma_geo'
            
            try:
                # Check current layer status
                layer_url = f"{rest_url}/workspaces/{workspace}/layers/{layer_name}"
                response = requests.get(layer_url, auth=auth)
                
                if response.status_code != 200:
                    self.stdout.write(f'   ❌ Cannot access layer (status: {response.status_code})')
                    failed_count += 1
                    continue
                
                layer_config = response.json()
                layer_info = layer_config.get('layer', {})
                current_enabled = layer_info.get('enabled', False)
                
                if current_enabled:
                    self.stdout.write(f'   ✅ Layer already enabled')
                    already_enabled += 1
                    continue
                
                self.stdout.write(f'   🔧 Layer is disabled - enabling...')
                
                if dry_run:
                    self.stdout.write(f'   🔍 DRY RUN: Would enable layer')
                    enabled_count += 1
                    continue
                
                # Enable the layer
                update_payload = {
                    "layer": {
                        "enabled": True
                    }
                }
                
                update_response = requests.put(
                    layer_url, 
                    json=update_payload, 
                    auth=auth,
                    headers={'Content-Type': 'application/json'}
                )
                
                if update_response.status_code in [200, 201]:
                    self.stdout.write(f'   ✅ Layer enabled successfully')
                    enabled_count += 1
                    
                    # Update the file's processing log
                    file_obj.processing_log += f"\\n✓ Layer enabled for WMS access"
                    file_obj.save()
                    
                else:
                    self.stdout.write(f'   ❌ Failed to enable layer (status: {update_response.status_code})')
                    self.stdout.write(f'      Response: {update_response.text[:200]}')
                    failed_count += 1
                
            except Exception as e:
                self.stdout.write(f'   ❌ Exception: {e}')
                failed_count += 1
        
        # Verify WMS capabilities after enabling
        self.stdout.write(f'\n🌐 Checking WMS capabilities...')
        
        try:
            wms_capabilities_url = f"{settings.GEOSERVER_URL}/wms?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetCapabilities"
            response = requests.get(wms_capabilities_url)
            response.raise_for_status()
            
            import xml.etree.ElementTree as ET
            root = ET.fromstring(response.content)
            ns = {'wms': 'http://www.opengis.net/wms'}
            
            wms_layers = []
            for layer_element in root.findall('.//wms:Layer/wms:Name', ns):
                wms_layers.append(layer_element.text)
            
            self.stdout.write(f'   📋 Total WMS layers available: {len(wms_layers)}')
            
            # Check how many of our shapefiles are now in WMS
            workspace = 'adma_geo'
            shapefile_wms_count = 0
            for file_obj in shapefiles:
                if file_obj.geoserver_layer_name:
                    layer_name = f"{workspace}:{file_obj.geoserver_layer_name}"
                    if layer_name in wms_layers:
                        shapefile_wms_count += 1
            
            self.stdout.write(f'   📋 Shapefiles now in WMS: {shapefile_wms_count}/{shapefiles.count()}')
            
            # List the shapefile layers that are now available
            if shapefile_wms_count > 0:
                self.stdout.write(f'\\n✅ Available shapefile WMS layers:')
                for file_obj in shapefiles:
                    if file_obj.geoserver_layer_name:
                        layer_name = f"{workspace}:{file_obj.geoserver_layer_name}"
                        if layer_name in wms_layers:
                            self.stdout.write(f'   - {layer_name} ({file_obj.name})')
            
        except Exception as e:
            self.stdout.write(f'   ❌ Error checking WMS capabilities: {e}')
        
        # Summary
        self.stdout.write(f'\n🎯 SUMMARY')
        self.stdout.write(f'=' * 50)
        
        if dry_run:
            self.stdout.write(f'🔍 DRY RUN COMPLETED - No changes made')
            self.stdout.write(f'📋 Would enable {enabled_count} disabled layers')
        else:
            self.stdout.write(f'✅ Enabled {enabled_count} shapefile layers')
            self.stdout.write(f'📋 {already_enabled} layers were already enabled')
            
        if failed_count > 0:
            self.stdout.write(f'❌ Failed to process {failed_count} layers')
        
        self.stdout.write(f'\\n🗺️  Test your maps at: http://localhost/file/<file_id>/map/')
        
        if not dry_run and enabled_count > 0:
            self.stdout.write(f'\\n💡 Tip: Clear your browser cache if layers still don\'t appear')
