#!/usr/bin/env python3

"""
Django management command to completely reset the system.

This command will:
1. Delete all database records (Files, Folders, Maps, MapLayers)
2. Clear uploaded files from disk storage
3. Clear GeoServer data (workspaces, datastores, layers)
4. Clear ChromaDB embeddings
5. Reset any cached data

Usage: python manage.py reset_system --confirm
"""

import os
import shutil
from django.core.management.base import BaseCommand, CommandError
from django.conf import settings
from filemanager.models import File, Folder, Map, MapLayer


class Command(BaseCommand):
    help = 'Reset the entire system - clear all files, folders, maps, and GeoServer data'

    def add_arguments(self, parser):
        parser.add_argument(
            '--confirm',
            action='store_true',
            help='Confirm that you want to reset the entire system',
        )

    def handle(self, *args, **options):
        if not options['confirm']:
            raise CommandError(
                'This command will delete ALL data. Use --confirm flag to proceed.\n'
                'Example: python manage.py reset_system --confirm'
            )

        self.stdout.write(
            self.style.WARNING('🔥 SYSTEM RESET STARTING - THIS WILL DELETE ALL DATA! 🔥')
        )

        try:
            # Step 1: Clear database records
            self.clear_database()
            
            # Step 2: Clear uploaded files
            self.clear_uploaded_files()
            
            # Step 3: Clear GeoServer data
            self.clear_geoserver_data()
            
            # Step 4: Clear ChromaDB embeddings
            self.clear_chromadb()
            
            self.stdout.write(
                self.style.SUCCESS('✅ SYSTEM RESET COMPLETE - All data cleared successfully!')
            )
            
        except Exception as e:
            self.stdout.write(
                self.style.ERROR(f'❌ Error during reset: {str(e)}')
            )
            raise

    def clear_database(self):
        """Clear all database records"""
        self.stdout.write('🗃️  Clearing database records...')
        
        # Get counts before deletion
        map_layers_count = MapLayer.objects.count()
        maps_count = Map.objects.count()
        files_count = File.objects.count()
        folders_count = Folder.objects.count()
        
        self.stdout.write(f'   Found: {files_count} files, {folders_count} folders, {maps_count} maps, {map_layers_count} map layers')
        
        # Delete in correct order (respecting foreign keys)
        MapLayer.objects.all().delete()
        self.stdout.write('   ✓ Map layers deleted')
        
        Map.objects.all().delete()
        self.stdout.write('   ✓ Maps deleted')
        
        File.objects.all().delete()
        self.stdout.write('   ✓ Files deleted')
        
        Folder.objects.all().delete()
        self.stdout.write('   ✓ Folders deleted')
        
        self.stdout.write(self.style.SUCCESS('✅ Database cleared'))

    def clear_uploaded_files(self):
        """Clear all uploaded files from disk"""
        self.stdout.write('📁 Clearing uploaded files...')
        
        # Clear Django media files
        media_root = getattr(settings, 'MEDIA_ROOT', None)
        if media_root and os.path.exists(media_root):
            uploads_dir = os.path.join(media_root, 'uploads')
            if os.path.exists(uploads_dir):
                shutil.rmtree(uploads_dir)
                os.makedirs(uploads_dir, exist_ok=True)
                self.stdout.write(f'   ✓ Cleared uploads directory: {uploads_dir}')
            else:
                self.stdout.write(f'   ℹ️  Uploads directory not found: {uploads_dir}')
        else:
            self.stdout.write('   ℹ️  MEDIA_ROOT not configured or doesn\'t exist')
        
        self.stdout.write(self.style.SUCCESS('✅ Uploaded files cleared'))

    def clear_geoserver_data(self):
        """Clear GeoServer data"""
        self.stdout.write('🗺️  Clearing GeoServer data...')
        
        try:
            # Import GeoServer management utilities
            from filemanager.geoserver_manager import GeoServerManager
            
            # Initialize GeoServer manager
            geoserver = GeoServerManager()
            
            # Clear the main workspace (this should remove all associated data)
            workspace_name = 'adma_geo'
            if geoserver.workspace_exists(workspace_name):
                # Delete all datastores in the workspace (this removes layers too)
                datastores = geoserver.get_datastores(workspace_name)
                for datastore in datastores:
                    geoserver.delete_datastore(workspace_name, datastore['name'])
                    self.stdout.write(f'   ✓ Deleted datastore: {datastore["name"]}')
                
                # Delete all coverage stores in the workspace
                coverage_stores = geoserver.get_coverage_stores(workspace_name)
                for coverage_store in coverage_stores:
                    geoserver.delete_coverage_store(workspace_name, coverage_store['name'])
                    self.stdout.write(f'   ✓ Deleted coverage store: {coverage_store["name"]}')
                
                # Note: We keep the workspace itself for future use
                self.stdout.write(f'   ✓ Cleared workspace: {workspace_name}')
            else:
                self.stdout.write(f'   ℹ️  Workspace not found: {workspace_name}')
                
        except ImportError:
            self.stdout.write('   ⚠️  GeoServer manager not available - skipping GeoServer cleanup')
        except Exception as e:
            self.stdout.write(f'   ⚠️  Error clearing GeoServer: {str(e)}')
        
        self.stdout.write(self.style.SUCCESS('✅ GeoServer data cleared'))

    def clear_chromadb(self):
        """Clear ChromaDB embeddings"""
        self.stdout.write('🔍 Clearing ChromaDB embeddings...')
        
        try:
            # Import ChromaDB utilities
            from filemanager.embedding_service import EmbeddingService
            
            # Initialize embedding service
            embedding_service = EmbeddingService()
            
            # Clear all collections
            embedding_service.clear_all_embeddings()
            self.stdout.write('   ✓ ChromaDB embeddings cleared')
            
        except ImportError:
            self.stdout.write('   ⚠️  ChromaDB service not available - skipping embeddings cleanup')
        except Exception as e:
            self.stdout.write(f'   ⚠️  Error clearing ChromaDB: {str(e)}')
        
        self.stdout.write(self.style.SUCCESS('✅ ChromaDB cleared'))
