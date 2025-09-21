"""
Management command to fix all existing shapefile records in the database
by updating their layer names and spatial extents from GeoServer.
"""
from django.core.management.base import BaseCommand, CommandError
from filemanager.models import File
from filemanager.gis_utils import GeoServerAPI
import json
import logging

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Fix all existing shapefile records by updating layer names and spatial extents from GeoServer'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be done without making changes',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Force update even if layer name appears correct',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        force = options['force']
        
        # Initialize GeoServer API
        geoserver_api = GeoServerAPI()
        
        # Find all published or processed shapefiles
        shapefiles = File.objects.filter(
            file_type='gis', 
            name__iendswith='.shp', 
            gis_status__in=['published', 'processed']
        )
        
        self.stdout.write(f"Found {shapefiles.count()} published shapefiles")
        
        fixed_count = 0
        error_count = 0
        
        for shapefile in shapefiles:
            self.stdout.write(f"\nProcessing: {shapefile.name} (ID: {shapefile.id})")
            
            try:
                # Get the original shapefile name (without extension)
                original_name = shapefile.name.replace('.shp', '')
                current_layer_name = shapefile.geoserver_layer_name
                
                # Check if layer name needs fixing
                needs_layer_fix = (
                    force or 
                    current_layer_name != original_name or
                    'yu_panunl_edu_' in current_layer_name or
                    current_layer_name is None
                )
                
                # Check if spatial extent needs fixing (world extent indicates wrong data)
                needs_extent_fix = False
                if shapefile.spatial_extent:
                    try:
                        extent_data = json.loads(shapefile.spatial_extent)
                        coords = extent_data.get('coordinates', [])
                        if coords == [[-180, -90], [180, 90]]:
                            needs_extent_fix = True
                    except:
                        needs_extent_fix = True
                else:
                    needs_extent_fix = True
                
                # Check if file needs publishing (processed but not published)
                needs_publishing = shapefile.gis_status == 'processed'
                
                if not needs_layer_fix and not needs_extent_fix and not needs_publishing:
                    self.stdout.write(f"  ✓ Already correct: {current_layer_name}")
                    continue
                
                # Get real extent from GeoServer
                real_extent = None
                if needs_extent_fix:
                    self.stdout.write(f"  → Getting spatial extent from GeoServer...")
                    real_extent = geoserver_api.get_layer_extent(original_name)
                    
                    if not real_extent:
                        self.stdout.write(f"  ⚠ Could not get extent for {original_name}")
                        error_count += 1
                        continue
                
                # Apply fixes
                if not dry_run:
                    if needs_publishing:
                        self.stdout.write(f"  → Trying to bundle and publish to GeoServer...")
                        from filemanager.gis_utils import bundle_and_publish_shapefile
                        try:
                            success, message = bundle_and_publish_shapefile(shapefile)
                            if success and 'published successfully' in message:
                                self.stdout.write(f"  ✓ Bundled and published to GeoServer")
                                shapefile.refresh_from_db()  # Get updated values
                            else:
                                self.stdout.write(f"  ⚠ Bundling/publishing failed: {message}")
                        except Exception as e:
                            self.stdout.write(f"  ❌ Bundling error: {str(e)}")
                    
                    if needs_layer_fix:
                        self.stdout.write(f"  → Fixing layer name: {current_layer_name} → {original_name}")
                        shapefile.geoserver_layer_name = original_name
                    
                    if needs_extent_fix and real_extent:
                        self.stdout.write(f"  → Updating spatial extent")
                        shapefile.spatial_extent = json.dumps(real_extent)
                    
                    if needs_layer_fix or needs_extent_fix:
                        shapefile.processing_log += f"\n[fix_shapefiles] Updated layer name and/or extent"
                        shapefile.save()
                    
                    self.stdout.write(f"  ✓ Fixed: {shapefile.name}")
                    fixed_count += 1
                else:
                    # Dry run - just show what would be done
                    if needs_publishing:
                        self.stdout.write(f"  [DRY RUN] Would try to bundle and publish to GeoServer")
                    if needs_layer_fix:
                        self.stdout.write(f"  [DRY RUN] Would fix layer name: {current_layer_name} → {original_name}")
                    if needs_extent_fix:
                        self.stdout.write(f"  [DRY RUN] Would update spatial extent")
                    fixed_count += 1
                    
            except Exception as e:
                self.stdout.write(f"  ❌ Error processing {shapefile.name}: {str(e)}")
                error_count += 1
                continue
        
        # Summary
        self.stdout.write(f"\n{'DRY RUN ' if dry_run else ''}Summary:")
        self.stdout.write(f"  ✓ Fixed: {fixed_count}")
        self.stdout.write(f"  ❌ Errors: {error_count}")
        
        if dry_run:
            self.stdout.write(f"\nRun again without --dry-run to apply changes")
        else:
            self.stdout.write(f"\nAll shapefile records have been updated!")
