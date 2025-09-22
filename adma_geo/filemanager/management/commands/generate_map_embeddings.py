#!/usr/bin/env python3

"""
Management command to generate embeddings for maps.

This command safely generates embeddings for maps without causing
ChromaDB corruption that can occur with automatic signal-based generation.

Usage:
    python manage.py generate_map_embeddings
    python manage.py generate_map_embeddings --map-id UUID
    python manage.py generate_map_embeddings --regenerate-all
"""

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from filemanager.models import Map
from filemanager.embedding_service import EmbeddingService
import logging

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Generate embeddings for maps to make them searchable'

    def add_arguments(self, parser):
        parser.add_argument(
            '--map-id',
            type=str,
            help='Generate embedding for a specific map ID',
        )
        parser.add_argument(
            '--regenerate-all',
            action='store_true',
            help='Regenerate embeddings for all maps (even those that already have embeddings)',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be done without actually generating embeddings',
        )

    def handle(self, *args, **options):
        try:
            embedding_service = EmbeddingService()
        except Exception as e:
            raise CommandError(f"Failed to initialize EmbeddingService: {e}")

        if options['map_id']:
            # Generate embedding for specific map
            self.handle_specific_map(options['map_id'], embedding_service, options['dry_run'])
        else:
            # Generate embeddings for all maps without embeddings
            self.handle_all_maps(embedding_service, options['regenerate_all'], options['dry_run'])

    def handle_specific_map(self, map_id, embedding_service, dry_run):
        """Generate embedding for a specific map"""
        try:
            map_obj = Map.objects.get(id=map_id)
        except Map.DoesNotExist:
            raise CommandError(f"Map with ID {map_id} not found")

        if dry_run:
            self.stdout.write(f"Would generate embedding for: {map_obj.name}")
            return

        self.stdout.write(f"Generating embedding for map: {map_obj.name}")
        
        try:
            success = embedding_service.generate_map_embedding(map_obj)
            if success:
                self.stdout.write(
                    self.style.SUCCESS(f"✅ Successfully generated embedding for: {map_obj.name}")
                )
            else:
                self.stdout.write(
                    self.style.ERROR(f"❌ Failed to generate embedding for: {map_obj.name}")
                )
        except Exception as e:
            self.stdout.write(
                self.style.ERROR(f"❌ Error generating embedding for {map_obj.name}: {e}")
            )

    def handle_all_maps(self, embedding_service, regenerate_all, dry_run):
        """Generate embeddings for all maps"""
        if regenerate_all:
            maps = Map.objects.all()
            action = "Regenerating embeddings for all maps"
        else:
            maps = Map.objects.filter(chroma_id__isnull=True)
            action = "Generating embeddings for maps without embeddings"

        total_maps = maps.count()
        
        if total_maps == 0:
            if regenerate_all:
                self.stdout.write("No maps found in the database")
            else:
                self.stdout.write("All maps already have embeddings")
            return

        self.stdout.write(f"{action}: {total_maps} maps")

        if dry_run:
            self.stdout.write("DRY RUN - No embeddings will be generated")
            for map_obj in maps:
                status = "has embedding" if map_obj.chroma_id else "needs embedding"
                self.stdout.write(f"  - {map_obj.name} ({status})")
            return

        success_count = 0
        error_count = 0

        for i, map_obj in enumerate(maps, 1):
            self.stdout.write(f"[{i}/{total_maps}] Processing: {map_obj.name}")
            
            try:
                success = embedding_service.generate_map_embedding(map_obj)
                if success:
                    self.stdout.write(f"  ✅ Success")
                    success_count += 1
                else:
                    self.stdout.write(f"  ❌ Failed")
                    error_count += 1
            except Exception as e:
                self.stdout.write(f"  ❌ Error: {e}")
                error_count += 1

        # Summary
        self.stdout.write(f"\n📊 SUMMARY:")
        self.stdout.write(f"  Successfully processed: {success_count}")
        self.stdout.write(f"  Errors: {error_count}")
        self.stdout.write(f"  Total: {total_maps}")

        if success_count > 0:
            self.stdout.write(
                self.style.SUCCESS(f"✅ Successfully generated {success_count} map embeddings")
            )

        if error_count > 0:
            self.stdout.write(
                self.style.ERROR(f"❌ {error_count} maps failed to generate embeddings")
            )

        # Check ChromaDB status
        try:
            collection = embedding_service.collection
            all_embeddings = collection.get()
            total_embeddings = len(all_embeddings['ids'])
            
            # Count map embeddings
            map_embeddings = 0
            for metadata in all_embeddings['metadatas']:
                if metadata and metadata.get('type') == 'map':
                    map_embeddings += 1
                    
            self.stdout.write(f"\n📊 ChromaDB Status:")
            self.stdout.write(f"  Total embeddings: {total_embeddings}")
            self.stdout.write(f"  Map embeddings: {map_embeddings}")
            
        except Exception as e:
            self.stdout.write(f"\n⚠️  Could not check ChromaDB status: {e}")
