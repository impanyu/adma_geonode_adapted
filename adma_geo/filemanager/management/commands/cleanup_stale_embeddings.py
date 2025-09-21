import logging
from django.core.management.base import BaseCommand
from filemanager.models import File, Folder, Map
from filemanager.embedding_service import embedding_service

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Clean up stale embeddings in ChromaDB that no longer have corresponding Django objects'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be deleted without actually deleting',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        
        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN MODE - No embeddings will be deleted'))
        
        self.stdout.write('Checking for stale embeddings in ChromaDB...')
        
        try:
            # Get all embeddings from ChromaDB
            all_embeddings = embedding_service.collection.get(
                limit=10000,  # Get a large number of embeddings
                include=['metadatas']
            )
            
            total_embeddings = len(all_embeddings['ids'])
            self.stdout.write(f'Found {total_embeddings} embeddings in ChromaDB')
            
            stale_embeddings = []
            
            # Check each embedding to see if its Django object still exists
            for i, embedding_id in enumerate(all_embeddings['ids']):
                metadata = all_embeddings['metadatas'][i]
                item_type = metadata.get('type')
                item_id = metadata.get('id')
                item_name = metadata.get('name', 'Unknown')
                
                if not item_id:
                    self.stdout.write(f'  Warning: Embedding {embedding_id} has no item ID')
                    continue
                
                exists = False
                
                try:
                    if item_type == 'file':
                        File.objects.get(id=item_id)
                        exists = True
                    elif item_type == 'folder':
                        Folder.objects.get(id=item_id)
                        exists = True
                    elif item_type == 'map':
                        Map.objects.get(id=item_id)
                        exists = True
                    else:
                        self.stdout.write(f'  Warning: Unknown item type "{item_type}" for embedding {embedding_id}')
                        continue
                        
                except (File.DoesNotExist, Folder.DoesNotExist, Map.DoesNotExist):
                    exists = False
                
                if not exists:
                    stale_embeddings.append({
                        'embedding_id': embedding_id,
                        'type': item_type,
                        'id': item_id,
                        'name': item_name
                    })
                    self.stdout.write(f'  Found stale: {item_type} "{item_name}" (ID: {item_id})')
            
            self.stdout.write(f'\nSummary:')
            self.stdout.write(f'  Total embeddings: {total_embeddings}')
            self.stdout.write(f'  Stale embeddings: {len(stale_embeddings)}')
            self.stdout.write(f'  Valid embeddings: {total_embeddings - len(stale_embeddings)}')
            
            if stale_embeddings:
                if dry_run:
                    self.stdout.write(f'\nWould delete {len(stale_embeddings)} stale embeddings')
                else:
                    self.stdout.write(f'\nDeleting {len(stale_embeddings)} stale embeddings...')
                    
                    deleted_count = 0
                    for stale in stale_embeddings:
                        try:
                            embedding_service.remove_embedding(stale['embedding_id'])
                            deleted_count += 1
                            if deleted_count % 10 == 0:
                                self.stdout.write(f'  Deleted {deleted_count}/{len(stale_embeddings)}')
                        except Exception as e:
                            self.stdout.write(
                                self.style.ERROR(f'  Failed to delete {stale["embedding_id"]}: {e}')
                            )
                    
                    self.stdout.write(
                        self.style.SUCCESS(f'Successfully deleted {deleted_count} stale embeddings')
                    )
            else:
                self.stdout.write(self.style.SUCCESS('No stale embeddings found!'))
                
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Error during cleanup: {e}'))
            logger.error(f"Error during embedding cleanup: {e}")
