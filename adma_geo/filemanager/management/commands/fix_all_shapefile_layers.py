"""
Django management command to automatically fix all shapefile layer name mismatches
This provides a general solution for the auto-numbering issue between database and GeoServer
"""
import requests
import xml.etree.ElementTree as ET
from django.core.management.base import BaseCommand
from django.conf import settings
from filemanager.models import File


class Command(BaseCommand):
    help = 'Automatically fix all shapefile layer name mismatches between database and GeoServer'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be fixed without making changes',
        )
        parser.add_argument(
            '--auto-fix',
            action='store_true', 
            help='Automatically fix all mismatches',
        )

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS('=== General Shapefile Layer Fix ==='))
        
        dry_run = options['dry_run']
        auto_fix = options['auto_fix']
        
        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN MODE - No changes will be made'))
        
        # Get all published shapefiles
        shapefiles = File.objects.filter(
            file_type='gis',
            is_spatial=True,
            gis_status='published',
            name__iendswith='.shp'
        ).order_by('-created_at')
        
        self.stdout.write(f'Found {shapefiles.count()} published shapefiles to check')
        
        # Get WMS capabilities once
        try:
            wms_url = f"{settings.GEOSERVER_URL}/wms?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetCapabilities"
            response = requests.get(wms_url, timeout=10)
            response.raise_for_status()
            
            root = ET.fromstring(response.content)
            ns = {'wms': 'http://www.opengis.net/wms'}
            wms_layers = [element.text for element in root.findall('.//wms:Layer/wms:Name', ns)]
            
            self.stdout.write(f'Retrieved {len(wms_layers)} layers from WMS capabilities')
            
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Failed to get WMS capabilities: {e}'))
            return
        
        # Check each shapefile
        fixed_count = 0
        working_count = 0
        error_count = 0
        
        for shp_file in shapefiles:
            try:
                self.stdout.write(f'\n📁 Checking: {shp_file.name} (ID: {str(shp_file.id)[:8]}...)')
                
                if not shp_file.geoserver_layer_name:
                    self.stdout.write(f'   ⚠️  No layer name set - skipping')
                    error_count += 1
                    continue
                
                workspace = shp_file.geoserver_workspace or 'adma_geo'
                expected_layer = f'{workspace}:{shp_file.geoserver_layer_name}'
                
                # Check if current layer name exists in WMS
                if expected_layer in wms_layers:
                    self.stdout.write(f'   ✅ Working: {shp_file.geoserver_layer_name}')
                    working_count += 1
                    
                    # Verify WMS actually returns data
                    if not dry_run:
                        wms_test_url = f"{settings.GEOSERVER_URL}/wms"
                        params = {
                            'SERVICE': 'WMS',
                            'VERSION': '1.1.1',
                            'REQUEST': 'GetMap',
                            'LAYERS': expected_layer,
                            'STYLES': '',
                            'FORMAT': 'image/png',
                            'TRANSPARENT': 'true',
                            'SRS': 'EPSG:4326',
                            'BBOX': '-180,-90,180,90',
                            'WIDTH': '256',
                            'HEIGHT': '256'
                        }
                        
                        try:
                            test_response = requests.get(wms_test_url, params=params, timeout=5)
                            if test_response.status_code == 200:
                                content_type = test_response.headers.get('Content-Type', '')
                                if 'image' in content_type:
                                    self.stdout.write(f'   ✅ WMS returns valid image')
                                else:
                                    self.stdout.write(f'   ⚠️  WMS returns non-image: {content_type}')
                            else:
                                self.stdout.write(f'   ⚠️  WMS GetMap failed: {test_response.status_code}')
                        except:
                            pass  # Don't fail on WMS test errors
                    
                    continue
                
                # Layer name mismatch - find the correct one
                base_name = shp_file.geoserver_layer_name
                similar_layers = [layer for layer in wms_layers if base_name in layer and layer.startswith(f'{workspace}:')]
                
                if similar_layers:
                    # Find the highest numbered version (most recent)
                    numbered_layers = []
                    for layer in similar_layers:
                        layer_name = layer.replace(f'{workspace}:', '')
                        if layer_name == base_name:
                            numbered_layers.append((0, layer_name))
                        else:
                            try:
                                suffix = layer_name.replace(base_name, '')
                                if suffix.isdigit():
                                    num = int(suffix)
                                    numbered_layers.append((num, layer_name))
                                else:
                                    numbered_layers.append((999, layer_name))
                            except:
                                numbered_layers.append((999, layer_name))
                    
                    if numbered_layers:
                        numbered_layers.sort(reverse=True)
                        correct_layer_name = numbered_layers[0][1]
                        
                        self.stdout.write(f'   🔧 Mismatch found:')
                        self.stdout.write(f'      Database: {base_name}')
                        self.stdout.write(f'      GeoServer: {correct_layer_name}')
                        
                        if dry_run:
                            self.stdout.write(f'   📝 Would fix: {base_name} → {correct_layer_name}')
                        elif auto_fix:
                            # Apply the fix
                            old_name = shp_file.geoserver_layer_name
                            shp_file.geoserver_layer_name = correct_layer_name
                            shp_file.processing_log += f'\nAuto-fixed layer name: {old_name} → {correct_layer_name}'
                            shp_file.save()
                            
                            self.stdout.write(self.style.SUCCESS(f'   ✅ Fixed: {base_name} → {correct_layer_name}'))
                            fixed_count += 1
                        else:
                            self.stdout.write(f'   ❓ Use --auto-fix to apply correction')
                            
                else:
                    self.stdout.write(f'   ❌ No matching layers found in GeoServer')
                    error_count += 1
                    
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'   ❌ Error processing {shp_file.name}: {e}'))
                error_count += 1
        
        # Summary
        self.stdout.write(self.style.SUCCESS('\n=== Summary ==='))
        self.stdout.write(f'✅ Already working: {working_count}')
        self.stdout.write(f'🔧 Fixed: {fixed_count}')
        self.stdout.write(f'❌ Errors: {error_count}')
        
        if dry_run and fixed_count > 0:
            self.stdout.write(f'\n💡 Run with --auto-fix to apply {fixed_count} corrections')
        elif auto_fix and fixed_count > 0:
            self.stdout.write(f'\n🎉 Successfully applied {fixed_count} corrections!')
            self.stdout.write(f'   All shapefiles should now work on maps')
