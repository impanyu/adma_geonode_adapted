from django.core.management.base import BaseCommand
from filemanager.models import Map


class Command(BaseCommand):
    help = 'Update center coordinates for existing maps based on their layer extents'

    def add_arguments(self, parser):
        parser.add_argument(
            '--map-id',
            type=str,
            help='Update specific map by ID',
        )

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS('=== Map Center Update ==='))
        
        if options['map_id']:
            # Update specific map
            try:
                map_obj = Map.objects.get(id=options['map_id'])
                self.update_map_center(map_obj)
            except Map.DoesNotExist:
                self.stdout.write(self.style.ERROR(f'Map with ID {options["map_id"]} not found'))
        else:
            # Update all maps
            maps = Map.objects.all()
            self.stdout.write(f'Found {maps.count()} maps to update')
            
            for map_obj in maps:
                self.update_map_center(map_obj)

    def update_map_center(self, map_obj):
        self.stdout.write(f'Updating map: {map_obj.name} (ID: {map_obj.id})')
        
        # Store old values
        old_center_lat = map_obj.center_lat
        old_center_lng = map_obj.center_lng
        old_zoom = map_obj.zoom_level
        
        # Calculate new center
        map_obj.calculate_and_update_center()
        
        # Refresh from database
        map_obj.refresh_from_db()
        
        self.stdout.write(
            f'  Old center: ({old_center_lat}, {old_center_lng}), zoom: {old_zoom}'
        )
        self.stdout.write(
            f'  New center: ({map_obj.center_lat}, {map_obj.center_lng}), zoom: {map_obj.zoom_level}'
        )
        self.stdout.write(self.style.SUCCESS(f'  ✅ Updated successfully'))
