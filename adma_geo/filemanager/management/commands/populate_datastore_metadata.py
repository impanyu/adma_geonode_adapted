#!/usr/bin/env python3

"""
Management command to populate missing datastore metadata for existing files.

This adds the geoserver_datastore_name field data for files that were 
uploaded before this metadata was being stored.
"""

from django.core.management.base import BaseCommand
from django.conf import settings
from filemanager.models import File
import requests


class Command(BaseCommand):
    help = 'Populate missing datastore metadata for existing spatial files'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be changed without making changes',
        )

    def handle(self, *args, **options):
        self.stdout.write("=== Populating Datastore Metadata ===")
        
        dry_run = options.get('dry_run', False)
        
        if dry_run:
            self.stdout.write("🔍 DRY RUN MODE - No changes will be made")
        
        # Get all published spatial files without datastore metadata
        files_without_datastore = File.objects.filter(
            is_spatial=True,
            gis_status='published',
            geoserver_datastore_name__isnull=True
        ).exclude(geoserver_layer_name__isnull=True).exclude(geoserver_layer_name='')
        
        self.stdout.write(f"\n📊 Found {len(files_without_datastore)} files missing datastore metadata")
        
        updated_count = 0
        error_count = 0
        
        for file_obj in files_without_datastore:
            self.stdout.write(f"\n📁 {file_obj.name}")
            self.stdout.write(f"   Layer: {file_obj.geoserver_layer_name}")
            
            # For most cases, datastore name = layer name
            # But we should verify this actually exists in GeoServer
            layer_name = file_obj.geoserver_layer_name
            workspace = file_obj.geoserver_workspace or 'adma_geo'
            
            # Determine if it's a raster or vector file
            file_extension = file_obj.name.lower()
            if any(ext in file_extension for ext in ['.tif', '.tiff', '.geotiff', '.geotif']):
                datastore_type = 'coveragestores'
                store_type_name = 'coverage store'
            else:
                datastore_type = 'datastores'
                store_type_name = 'datastore'
            
            # Check if datastore exists with the same name as layer
            datastore_name = layer_name
            if self._verify_datastore_exists(workspace, datastore_type, datastore_name):
                self.stdout.write(f"   ✅ {store_type_name.title()} found: {datastore_name}")
                
                if not dry_run:
                    file_obj.geoserver_datastore_name = datastore_name
                    file_obj.save()
                    self.stdout.write(f"   ✅ Metadata updated")
                else:
                    self.stdout.write(f"   🔄 Would update datastore metadata: {datastore_name}")
                
                updated_count += 1
            else:
                self.stdout.write(f"   ❌ {store_type_name.title()} not found: {datastore_name}")
                
                # Try to find alternative datastore names
                alternative = self._find_alternative_datastore(workspace, datastore_type, file_obj.name)
                if alternative:
                    self.stdout.write(f"   🔍 Found alternative: {alternative}")
                    
                    if not dry_run:
                        file_obj.geoserver_datastore_name = alternative
                        file_obj.save()
                        self.stdout.write(f"   ✅ Metadata updated with alternative")
                    else:
                        self.stdout.write(f"   🔄 Would update with alternative: {alternative}")
                    
                    updated_count += 1
                else:
                    self.stdout.write(f"   ❌ No working datastore found")
                    error_count += 1
        
        # Summary
        self.stdout.write(f"\n📈 Datastore Metadata Population Summary:")
        self.stdout.write(f"   ✅ Updated: {updated_count}")
        self.stdout.write(f"   ❌ Errors: {error_count}")
        self.stdout.write(f"   📊 Total: {len(files_without_datastore)}")
        
        if not dry_run and updated_count > 0:
            self.stdout.write(f"\n🎉 Successfully populated datastore metadata for {updated_count} files!")
            self.stdout.write(f"💡 Files now have complete GeoServer mapping for proper visualization and cleanup.")
        elif dry_run and updated_count > 0:
            self.stdout.write(f"\n🔍 Dry run complete. Run without --dry-run to apply {updated_count} updates.")

    def _geoserver_auth(self):
        """Return GeoServer admin credentials from settings (never hardcoded)."""
        return (
            settings.GEOSERVER_ADMIN_USER,
            settings.GEOSERVER_ADMIN_PASSWORD,
        )

    def _verify_datastore_exists(self, workspace, datastore_type, datastore_name):
        """Verify that a datastore exists in GeoServer"""
        try:
            url = f"{settings.GEOSERVER_URL}/rest/workspaces/{workspace}/{datastore_type}/{datastore_name}"
            response = requests.get(url, auth=self._geoserver_auth(), timeout=10)
            return response.status_code == 200
        except Exception:
            return False

    def _find_alternative_datastore(self, workspace, datastore_type, filename):
        """Try to find alternative datastore names based on filename patterns"""
        try:
            # Get all datastores
            url = f"{settings.GEOSERVER_URL}/rest/workspaces/{workspace}/{datastore_type}"
            response = requests.get(url, auth=self._geoserver_auth(),
                                  headers={'Accept': 'application/json'}, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                
                # Handle both datastores and coveragestores
                if datastore_type == 'datastores':
                    stores = data.get('dataStores', {}).get('dataStore', [])
                else:
                    stores = data.get('coverageStores', {}).get('coverageStore', [])
                
                if not isinstance(stores, list):
                    stores = [stores]
                
                # Look for stores that match the filename
                base_name = filename.replace('.shp', '').replace('.tif', '').replace('.tiff', '')
                
                for store in stores:
                    store_name = store.get('name', '')
                    if base_name.lower() in store_name.lower() or store_name.lower() in base_name.lower():
                        return store_name
            
            return None
            
        except Exception:
            return None
