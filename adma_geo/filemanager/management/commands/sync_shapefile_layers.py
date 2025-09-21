#!/usr/bin/env python3

"""
Management command to detect and fix discrepancies between 
database layer names and actual GeoServer layer names for shapefiles.

This solves the problem where GeoServer auto-numbers layers 
(e.g., summary_summary1, summary_summary2) but our database 
has incorrect or systematic names.
"""

from django.core.management.base import BaseCommand
from django.conf import settings
from filemanager.models import File
import requests
import xml.etree.ElementTree as ET


class Command(BaseCommand):
    help = 'Synchronize shapefile layer names between database and GeoServer'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be changed without making changes',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Apply changes even if some tests fail',
        )

    def handle(self, *args, **options):
        self.stdout.write("=== Shapefile Layer Name Synchronization ===")
        
        dry_run = options.get('dry_run', False)
        force = options.get('force', False)
        
        if dry_run:
            self.stdout.write("🔍 DRY RUN MODE - No changes will be made")
        
        # Get all published shapefiles
        shp_files = File.objects.filter(
            name__endswith='.shp',
            gis_status='published'
        ).order_by('created_at')
        
        self.stdout.write(f"\n📊 Found {len(shp_files)} published shapefiles")
        
        fixed_count = 0
        error_count = 0
        
        for file_obj in shp_files:
            self.stdout.write(f"\n📁 {file_obj.name} (ID: {str(file_obj.id)[:8]}...)")
            self.stdout.write(f"   DB layer: {file_obj.geoserver_layer_name}")
            
            # Test if current layer name works
            if self._test_layer(file_obj.geoserver_layer_name):
                self.stdout.write(f"   ✅ Current layer name works correctly")
                continue
            
            self.stdout.write(f"   ❌ Current layer name doesn't work")
            
            # Try to detect the actual working layer
            base_name = file_obj.name.replace('.shp', '')
            actual_layer = self._detect_working_layer(base_name)
            
            if actual_layer:
                self.stdout.write(f"   🔍 Found working layer: {actual_layer}")
                
                if not dry_run:
                    # Update database
                    old_name = file_obj.geoserver_layer_name
                    file_obj.geoserver_layer_name = actual_layer
                    file_obj.save()
                    
                    self.stdout.write(f"   ✅ Updated database: {old_name} → {actual_layer}")
                    
                    # Verify the fix works
                    if self._test_layer(actual_layer):
                        self.stdout.write(f"   ✅ Verification successful")
                        self.stdout.write(f"   📍 Map: http://localhost/file/{file_obj.id}/map/")
                        fixed_count += 1
                    else:
                        self.stdout.write(f"   ❌ Verification failed")
                        error_count += 1
                else:
                    self.stdout.write(f"   🔄 Would update: {file_obj.geoserver_layer_name} → {actual_layer}")
                    fixed_count += 1
            else:
                self.stdout.write(f"   ❌ Could not detect working layer")
                error_count += 1
        
        # Summary
        self.stdout.write(f"\n📈 Synchronization Summary:")
        self.stdout.write(f"   ✅ Fixed: {fixed_count}")
        self.stdout.write(f"   ❌ Errors: {error_count}")
        self.stdout.write(f"   📊 Total: {len(shp_files)}")
        
        success_rate = (fixed_count / len(shp_files)) * 100 if len(shp_files) > 0 else 0
        self.stdout.write(f"   🎯 Success rate: {success_rate:.1f}%")
        
        if not dry_run and fixed_count > 0:
            self.stdout.write(f"\n🎉 Successfully synchronized {fixed_count} shapefile layer names!")
            self.stdout.write(f"💡 This solves the discrepancy between database and GeoServer layer names.")
        elif dry_run and fixed_count > 0:
            self.stdout.write(f"\n🔍 Dry run complete. Run without --dry-run to apply {fixed_count} fixes.")
        
        if error_count > 0:
            self.stdout.write(f"\n⚠️  {error_count} files need manual attention.")

    def _test_layer(self, layer_name):
        """Test if a layer name works in GeoServer WMS"""
        try:
            wms_url = f'{settings.GEOSERVER_URL}/wms'
            params = {
                'SERVICE': 'WMS',
                'VERSION': '1.1.1',
                'REQUEST': 'GetMap',
                'LAYERS': f'adma_geo:{layer_name}',
                'STYLES': '',
                'FORMAT': 'image/png',
                'TRANSPARENT': 'true',
                'SRS': 'EPSG:4326',
                'BBOX': '-180,-90,180,90',
                'WIDTH': '128',
                'HEIGHT': '128'
            }
            
            response = requests.get(wms_url, params=params, timeout=10)
            
            if response.status_code == 200:
                content_type = response.headers.get('Content-Type', '')
                return 'image' in content_type
            
            return False
            
        except Exception:
            return False

    def _detect_working_layer(self, base_name):
        """Detect the actual working layer name by testing variations"""
        try:
            # Test numbered variations (1-25)
            for i in range(1, 26):
                test_layer = f'{base_name}{i}'
                if self._test_layer(test_layer):
                    return test_layer
            
            # Test base name without number
            if self._test_layer(base_name):
                return base_name
            
            return None
            
        except Exception:
            return None

    def _get_all_workspace_layers(self):
        """Get all layer names in the workspace for debugging"""
        try:
            wms_url = f"{settings.GEOSERVER_URL}/wms?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetCapabilities"
            response = requests.get(wms_url, timeout=10)
            
            if response.status_code == 200:
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
                return []
                
        except Exception:
            return []
