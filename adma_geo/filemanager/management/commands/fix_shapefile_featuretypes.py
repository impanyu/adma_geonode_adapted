import requests
import logging
from django.conf import settings
from django.core.management.base import BaseCommand
from filemanager.models import File

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Fix missing feature types for shapefile layers and ensure WMS accessibility'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be done without making changes',
        )

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS('=== Shapefile Feature Type Fix Tool ==='))
        
        dry_run = options['dry_run']
        
        if dry_run:
            self.stdout.write(self.style.WARNING('🔍 DRY RUN MODE - No changes will be made'))
        
        auth = (settings.GEOSERVER_ADMIN_USER, settings.GEOSERVER_ADMIN_PASSWORD)
        rest_url = f"{settings.GEOSERVER_URL}/rest"
        workspace = 'adma_geo'
        
        # Find all published shapefiles
        shapefiles = File.objects.filter(
            name__iendswith='.shp', 
            is_spatial=True, 
            gis_status='published',
            geoserver_layer_name__isnull=False
        )
        
        self.stdout.write(f'\\n📋 Found {shapefiles.count()} published shapefiles to fix')
        
        fixed_count = 0
        failed_count = 0
        already_working = 0
        
        for file_obj in shapefiles:
            self.stdout.write(f'\\n🔍 Processing: {file_obj.name}')
            self.stdout.write(f'   Layer name: {file_obj.geoserver_layer_name}')
            
            layer_name = file_obj.geoserver_layer_name
            
            try:
                # Step 1: Get the layer configuration to find the correct datastore
                layer_url = f"{rest_url}/workspaces/{workspace}/layers/{layer_name}.json"
                layer_response = requests.get(layer_url, auth=auth)
                
                if layer_response.status_code != 200:
                    self.stdout.write(f'   ❌ Cannot access layer: {layer_response.status_code}')
                    failed_count += 1
                    continue
                
                layer_config = layer_response.json()
                layer_info = layer_config.get('layer', {})
                resource_href = layer_info.get('resource', {}).get('href', '')
                
                # Extract datastore name from resource href
                # href format: http://geoserver:8080/geoserver/rest/workspaces/adma_geo/datastores/DATASTORE_NAME/featuretypes/LAYER_NAME.json
                if '/datastores/' not in resource_href:
                    self.stdout.write(f'   ❌ Cannot determine datastore from resource href: {resource_href}')
                    failed_count += 1
                    continue
                
                # Parse datastore name from href
                parts = resource_href.split('/datastores/')[1].split('/featuretypes/')[0]
                datastore_name = parts
                
                self.stdout.write(f'   📋 Datastore: {datastore_name}')
                
                # Step 2: Check if the feature type exists
                ft_url = f"{rest_url}/workspaces/{workspace}/datastores/{datastore_name}/featuretypes/{layer_name}.json"
                ft_response = requests.get(ft_url, auth=auth)
                
                if ft_response.status_code == 200:
                    # Feature type exists, check if it's enabled
                    ft_config = ft_response.json()
                    ft_info = ft_config.get('featureType', {})
                    ft_enabled = ft_info.get('enabled', False)
                    
                    if ft_enabled:
                        self.stdout.write(f'   ✅ FeatureType exists and is enabled')
                        
                        # Check if layer is enabled
                        layer_enabled = layer_info.get('enabled', False)
                        if layer_enabled:
                            self.stdout.write(f'   ✅ Layer is also enabled - should be working')
                            already_working += 1
                            continue
                        else:
                            self.stdout.write(f'   🔧 Layer needs enabling...')
                            if not dry_run:
                                # Enable the layer
                                layer_update = {"layer": {"enabled": True}}
                                enable_response = requests.put(
                                    layer_url.replace('.json', ''), 
                                    json=layer_update, 
                                    auth=auth,
                                    headers={'Content-Type': 'application/json'}
                                )
                                if enable_response.status_code in [200, 201]:
                                    self.stdout.write(f'   ✅ Layer enabled')
                                    fixed_count += 1
                                else:
                                    self.stdout.write(f'   ❌ Failed to enable layer: {enable_response.status_code}')
                                    failed_count += 1
                            else:
                                self.stdout.write(f'   🔍 DRY RUN: Would enable layer')
                                fixed_count += 1
                            continue
                    else:
                        self.stdout.write(f'   🔧 FeatureType exists but is disabled - enabling...')
                        if not dry_run:
                            # Enable the feature type
                            ft_update = {"featureType": {"enabled": True}}
                            ft_enable_response = requests.put(
                                ft_url.replace('.json', ''), 
                                json=ft_update, 
                                auth=auth,
                                headers={'Content-Type': 'application/json'}
                            )
                            if ft_enable_response.status_code in [200, 201]:
                                self.stdout.write(f'   ✅ FeatureType enabled')
                            else:
                                self.stdout.write(f'   ❌ Failed to enable FeatureType: {ft_enable_response.status_code}')
                        else:
                            self.stdout.write(f'   🔍 DRY RUN: Would enable FeatureType')
                
                elif ft_response.status_code == 404:
                    self.stdout.write(f'   ❌ FeatureType missing - needs to be created')
                    
                    if not dry_run:
                        # Create the missing feature type
                        ft_create_url = f"{rest_url}/workspaces/{workspace}/datastores/{datastore_name}/featuretypes"
                        ft_create_data = f'''<featureType>
                            <name>{layer_name}</name>
                            <nativeName>{layer_name}</nativeName>
                            <title>{layer_name}</title>
                            <srs>EPSG:4326</srs>
                            <enabled>true</enabled>
                        </featureType>'''
                        
                        create_response = requests.post(
                            ft_create_url,
                            data=ft_create_data,
                            auth=auth,
                            headers={'Content-Type': 'application/xml'}
                        )
                        
                        if create_response.status_code in [200, 201]:
                            self.stdout.write(f'   ✅ FeatureType created successfully')
                        else:
                            self.stdout.write(f'   ❌ Failed to create FeatureType: {create_response.status_code}')
                            self.stdout.write(f'      Response: {create_response.text[:200]}')
                            failed_count += 1
                            continue
                    else:
                        self.stdout.write(f'   🔍 DRY RUN: Would create FeatureType')
                    
                    # Now enable the layer
                    if not dry_run:
                        layer_update = {"layer": {"enabled": True}}
                        enable_response = requests.put(
                            layer_url.replace('.json', ''), 
                            json=layer_update, 
                            auth=auth,
                            headers={'Content-Type': 'application/json'}
                        )
                        if enable_response.status_code in [200, 201]:
                            self.stdout.write(f'   ✅ Layer enabled after FeatureType creation')
                            fixed_count += 1
                        else:
                            self.stdout.write(f'   ❌ Failed to enable layer after FeatureType creation: {enable_response.status_code}')
                            failed_count += 1
                    else:
                        self.stdout.write(f'   🔍 DRY RUN: Would enable layer after FeatureType creation')
                        fixed_count += 1
                        
                else:
                    self.stdout.write(f'   ❌ Error checking FeatureType: {ft_response.status_code}')
                    failed_count += 1
                
            except Exception as e:
                self.stdout.write(f'   ❌ Exception: {e}')
                failed_count += 1
        
        # Verify results by checking WMS capabilities
        self.stdout.write(f'\\n🌐 Checking WMS capabilities...')
        
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
            
            # Check shapefile layers specifically
            shapefile_wms_count = 0
            found_shapefile_layers = []
            for file_obj in shapefiles:
                if file_obj.geoserver_layer_name:
                    layer_name = f"{workspace}:{file_obj.geoserver_layer_name}"
                    if layer_name in wms_layers:
                        shapefile_wms_count += 1
                        found_shapefile_layers.append(layer_name)
            
            self.stdout.write(f'   📋 Shapefiles now in WMS: {shapefile_wms_count}/{shapefiles.count()}')
            
            if found_shapefile_layers:
                self.stdout.write(f'\\n✅ Working shapefile WMS layers:')
                for layer in found_shapefile_layers:
                    self.stdout.write(f'   - {layer}')
            
        except Exception as e:
            self.stdout.write(f'   ❌ Error checking WMS capabilities: {e}')
        
        # Summary
        self.stdout.write(f'\\n🎯 SUMMARY')
        self.stdout.write(f'=' * 50)
        
        if dry_run:
            self.stdout.write(f'🔍 DRY RUN COMPLETED - No changes made')
            self.stdout.write(f'📋 Would fix {fixed_count} shapefile layers')
        else:
            self.stdout.write(f'✅ Fixed {fixed_count} shapefile layers')
            self.stdout.write(f'📋 {already_working} layers were already working')
            
        if failed_count > 0:
            self.stdout.write(f'❌ Failed to fix {failed_count} layers')
        
        self.stdout.write(f'\\n🗺️  Test your maps at: http://localhost/file/<file_id>/map/')
        
        if not dry_run and fixed_count > 0:
            self.stdout.write(f'\\n💡 Tip: It may take a few seconds for GeoServer to update WMS capabilities')
