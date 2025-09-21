import requests
import logging
from django.conf import settings
from django.core.management.base import BaseCommand
from filemanager.models import File
from filemanager.gis_utils import GeoServerAPI

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Comprehensive fix for shapefile WMS publishing issues'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be done without making changes',
        )
        parser.add_argument(
            '--force-republish',
            action='store_true',
            help='Force republishing of all shapefiles regardless of status',
        )

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS('=== Shapefile WMS Fix Tool ==='))
        
        dry_run = options['dry_run']
        force_republish = options['force_republish']
        
        if dry_run:
            self.stdout.write(self.style.WARNING('🔍 DRY RUN MODE - No changes will be made'))
        
        # Create GeoServer API instance
        geoserver_manager = GeoServerAPI()
        auth = (settings.GEOSERVER_ADMIN_USER, settings.GEOSERVER_ADMIN_PASSWORD)
        rest_url = f"{settings.GEOSERVER_URL}/rest"

        # Step 1: Identify problematic shapefiles
        self.stdout.write('\n📋 Step 1: Identifying shapefile issues...')
        
        if force_republish:
            shapefiles = File.objects.filter(name__iendswith='.shp', is_spatial=True)
            self.stdout.write(f'Found {shapefiles.count()} shapefiles (force republish mode)')
        else:
            shapefiles = File.objects.filter(
                name__iendswith='.shp', 
                is_spatial=True, 
                gis_status='published'
            )
            self.stdout.write(f'Found {shapefiles.count()} published shapefiles to check')
        
        problematic_files = []
        working_files = []
        
        for file_obj in shapefiles:
            self.stdout.write(f'\n🔍 Checking: {file_obj.name}')
            
            if not file_obj.geoserver_layer_name:
                self.stdout.write(f'   ❌ No GeoServer layer name assigned')
                problematic_files.append((file_obj, 'no_layer_name'))
                continue
            
            # Check if layer exists and is properly configured
            layer_name = file_obj.geoserver_layer_name
            workspace = file_obj.geoserver_workspace or 'adma_geo'
            
            try:
                # Check layer configuration
                layer_url = f"{rest_url}/workspaces/{workspace}/layers/{layer_name}"
                response = requests.get(layer_url, auth=auth)
                
                if response.status_code == 404:
                    self.stdout.write(f'   ❌ Layer not found in GeoServer')
                    problematic_files.append((file_obj, 'layer_missing'))
                    continue
                elif response.status_code != 200:
                    self.stdout.write(f'   ❌ Error accessing layer: {response.status_code}')
                    problematic_files.append((file_obj, 'layer_error'))
                    continue
                
                layer_config = response.json()
                layer_info = layer_config.get('layer', {})
                
                # Check if layer is enabled
                if not layer_info.get('enabled', False):
                    self.stdout.write(f'   ❌ Layer is disabled')
                    problematic_files.append((file_obj, 'layer_disabled'))
                    continue
                
                # Check if feature type exists
                ft_url = f"{rest_url}/workspaces/{workspace}/datastores/{layer_name}/featuretypes/{layer_name}"
                ft_response = requests.get(ft_url, auth=auth)
                
                if ft_response.status_code == 404:
                    self.stdout.write(f'   ❌ FeatureType missing')
                    problematic_files.append((file_obj, 'featuretype_missing'))
                    continue
                elif ft_response.status_code != 200:
                    self.stdout.write(f'   ❌ FeatureType error: {ft_response.status_code}')
                    problematic_files.append((file_obj, 'featuretype_error'))
                    continue
                
                # Check if data store exists
                ds_url = f"{rest_url}/workspaces/{workspace}/datastores/{layer_name}"
                ds_response = requests.get(ds_url, auth=auth)
                
                if ds_response.status_code == 404:
                    self.stdout.write(f'   ❌ DataStore missing')
                    problematic_files.append((file_obj, 'datastore_missing'))
                    continue
                elif ds_response.status_code != 200:
                    self.stdout.write(f'   ❌ DataStore error: {ds_response.status_code}')
                    problematic_files.append((file_obj, 'datastore_error'))
                    continue
                
                # If we get here, the layer seems properly configured
                self.stdout.write(f'   ✅ Layer appears properly configured')
                working_files.append(file_obj)
                
            except Exception as e:
                self.stdout.write(f'   ❌ Exception checking layer: {e}')
                problematic_files.append((file_obj, 'exception'))
        
        # Step 2: Report findings
        self.stdout.write(f'\n📊 Step 2: Analysis Results')
        self.stdout.write(f'   ✅ Working shapefiles: {len(working_files)}')
        self.stdout.write(f'   ❌ Problematic shapefiles: {len(problematic_files)}')
        
        if problematic_files:
            self.stdout.write(f'\n🔧 Step 3: Fixing problematic shapefiles...')
            
            fixed_count = 0
            failed_count = 0
            
            for file_obj, issue_type in problematic_files:
                self.stdout.write(f'\n🔧 Fixing: {file_obj.name} (Issue: {issue_type})')
                
                if dry_run:
                    self.stdout.write(f'   🔍 DRY RUN: Would attempt to fix {issue_type}')
                    continue
                
                try:
                    # Mark as pending and trigger reprocessing
                    file_obj.gis_status = 'pending'
                    file_obj.processing_log = f"Reprocessing due to WMS issue: {issue_type}"
                    file_obj.geoserver_layer_name = None
                    file_obj.geoserver_workspace = None
                    file_obj.save()
                    
                    # Trigger reprocessing
                    from filemanager.tasks import process_gis_file_task
                    process_gis_file_task.delay(str(file_obj.id))
                    
                    self.stdout.write(f'   ✅ Triggered reprocessing')
                    fixed_count += 1
                    
                except Exception as e:
                    self.stdout.write(f'   ❌ Failed to fix: {e}')
                    failed_count += 1
            
            self.stdout.write(f'\n📈 Step 3 Results:')
            self.stdout.write(f'   ✅ Fixed: {fixed_count}')
            self.stdout.write(f'   ❌ Failed: {failed_count}')
        
        # Step 4: Verify WMS capabilities
        self.stdout.write(f'\n🌐 Step 4: Checking WMS capabilities...')
        
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
            
            # Check how many of our working shapefiles are in WMS
            workspace = 'adma_geo'
            shapefile_wms_count = 0
            for file_obj in working_files:
                if file_obj.geoserver_layer_name:
                    layer_name = f"{workspace}:{file_obj.geoserver_layer_name}"
                    if layer_name in wms_layers:
                        shapefile_wms_count += 1
            
            self.stdout.write(f'   📋 Working shapefiles in WMS: {shapefile_wms_count}/{len(working_files)}')
            
        except Exception as e:
            self.stdout.write(f'   ❌ Error checking WMS capabilities: {e}')
        
        # Summary
        self.stdout.write(f'\n🎯 SUMMARY')
        self.stdout.write(f'=' * 50)
        if dry_run:
            self.stdout.write(f'🔍 DRY RUN COMPLETED - No changes made')
            self.stdout.write(f'📋 Found {len(problematic_files)} shapefiles that need fixing')
            self.stdout.write(f'💡 Run without --dry-run to apply fixes')
        else:
            self.stdout.write(f'✅ Fixed {fixed_count} shapefile WMS issues')
            if failed_count > 0:
                self.stdout.write(f'❌ Failed to fix {failed_count} shapefiles')
            self.stdout.write(f'⏳ Reprocessing tasks have been queued')
            self.stdout.write(f'💡 Check back in a few minutes for results')
        
        self.stdout.write(f'\n🗺️  Test your maps at: http://localhost/file/<file_id>/map/')
