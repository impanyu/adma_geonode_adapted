"""
Django management command to migrate existing GeoServer layers to systematic naming
"""
from django.core.management.base import BaseCommand
from django.conf import settings
from filemanager.models import File
from filemanager.geoserver_manager import SystematicGeoServerManager


class Command(BaseCommand):
    help = 'Migrate existing GeoServer layers to systematic naming convention'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be changed without making changes',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Force migration even if some files fail',
        )

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS('=== Systematic Naming Migration ==='))
        
        dry_run = options['dry_run']
        force = options['force']
        
        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN MODE - No changes will be made'))
        
        # Get all published spatial files
        spatial_files = File.objects.filter(
            is_spatial=True,
            gis_status='published',
            geoserver_layer_name__isnull=False
        ).order_by('-created_at')
        
        self.stdout.write(f'Found {spatial_files.count()} published spatial files to migrate')
        
        geoserver_manager = SystematicGeoServerManager()
        
        migrated_count = 0
        error_count = 0
        skipped_count = 0
        
        for file_obj in spatial_files:
            try:
                self.stdout.write(f'\\n📁 Processing: {file_obj.name} (ID: {str(file_obj.id)[:8]}...)')
                
                # Generate new systematic name
                new_layer_name = geoserver_manager.generate_systematic_layer_name(file_obj)
                current_layer_name = file_obj.geoserver_layer_name
                
                self.stdout.write(f'   Current: {current_layer_name}')
                self.stdout.write(f'   New:     {new_layer_name}')
                
                # Check if already using systematic naming
                if current_layer_name == new_layer_name:
                    self.stdout.write(f'   ✅ Already using systematic naming - skipping')
                    skipped_count += 1
                    continue
                
                # Check if new name already exists
                if geoserver_manager.check_layer_exists(new_layer_name):
                    self.stdout.write(f'   ⚠️  New name already exists - skipping')
                    skipped_count += 1
                    continue
                
                if dry_run:
                    self.stdout.write(f'   📝 Would migrate: {current_layer_name} → {new_layer_name}')
                    migrated_count += 1
                else:
                    # Step 1: Delete old GeoServer resources
                    self.stdout.write(f'   🗑️  Deleting old resources: {current_layer_name}')
                    delete_success = geoserver_manager.delete_from_geoserver(file_obj)
                    
                    if not delete_success and not force:
                        self.stdout.write(self.style.ERROR(f'   ❌ Failed to delete old resources'))
                        error_count += 1
                        continue
                    
                    # Step 2: Republish with new systematic name
                    self.stdout.write(f'   📤 Publishing with new name: {new_layer_name}')
                    
                    # Temporarily reset to trigger republishing
                    file_obj.geoserver_layer_name = None
                    file_obj.gis_status = 'processed'
                    file_obj.save()
                    
                    # Republish with systematic naming
                    success, message, actual_layer_name = geoserver_manager.publish_file_to_geoserver(file_obj)
                    
                    if success:
                        self.stdout.write(self.style.SUCCESS(f'   ✅ Migrated successfully: {actual_layer_name}'))
                        migrated_count += 1
                    else:
                        self.stdout.write(self.style.ERROR(f'   ❌ Migration failed: {message}'))
                        error_count += 1
                        
                        if not force:
                            continue
                    
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'   ❌ Error processing {file_obj.name}: {e}'))
                error_count += 1
                
                if not force:
                    continue
        
        # Summary
        self.stdout.write(self.style.SUCCESS('\\n=== Migration Summary ==='))
        self.stdout.write(f'🔄 Migrated: {migrated_count}')
        self.stdout.write(f'⏭️  Skipped: {skipped_count}')
        self.stdout.write(f'❌ Errors: {error_count}')
        
        if dry_run and migrated_count > 0:
            self.stdout.write(f'\\n💡 Run without --dry-run to apply {migrated_count} migrations')
        elif not dry_run and migrated_count > 0:
            self.stdout.write(f'\\n🎉 Successfully migrated {migrated_count} files to systematic naming!')
            self.stdout.write(f'\\n📋 Benefits of systematic naming:')
            self.stdout.write(f'   ✅ Path-aware differentiation')
            self.stdout.write(f'   ✅ Predictable layer names')
            self.stdout.write(f'   ✅ No more auto-numbering conflicts')
            self.stdout.write(f'   ✅ Proper cleanup on deletion')
