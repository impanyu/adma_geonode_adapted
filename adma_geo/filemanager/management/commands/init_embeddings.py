"""
Django management command to initialize embeddings for existing files and folders
"""
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from filemanager.models import File, Folder
from filemanager.embedding_service import embedding_service
import time


class Command(BaseCommand):
    help = 'Initialize ChromaDB embeddings for existing files and folders'

    def add_arguments(self, parser):
        parser.add_argument(
            '--force',
            action='store_true',
            help='Force regenerate embeddings even if they exist',
        )
        parser.add_argument(
            '--files-only',
            action='store_true',
            help='Process only files',
        )
        parser.add_argument(
            '--folders-only',
            action='store_true',
            help='Process only folders',
        )
        parser.add_argument(
            '--batch-size',
            type=int,
            default=100,
            help='Number of items to process in each batch (default: 100)',
        )
        parser.add_argument(
            '--delay',
            type=float,
            default=0.1,
            help='Delay between items in seconds (default: 0.1)',
        )

    def handle(self, *args, **options):
        force = options['force']
        files_only = options['files_only']
        folders_only = options['folders_only']
        batch_size = options['batch_size']
        delay = options['delay']

        if files_only and folders_only:
            raise CommandError("Cannot specify both --files-only and --folders-only")

        self.stdout.write(
            self.style.SUCCESS(
                f'Starting embedding initialization (force={force}, batch_size={batch_size})'
            )
        )

        total_processed = 0
        start_time = timezone.now()

        try:
            # Process folders if not files-only
            if not files_only:
                folders_processed = self._process_folders(force, batch_size, delay)
                total_processed += folders_processed
                self.stdout.write(
                    self.style.SUCCESS(f'Processed {folders_processed} folders')
                )

            # Process files if not folders-only  
            if not folders_only:
                files_processed = self._process_files(force, batch_size, delay)
                total_processed += files_processed
                self.stdout.write(
                    self.style.SUCCESS(f'Processed {files_processed} files')
                )

            # Display summary
            end_time = timezone.now()
            duration = (end_time - start_time).total_seconds()
            
            self.stdout.write(
                self.style.SUCCESS(
                    f'\nCompleted successfully!'
                    f'\nTotal items processed: {total_processed}'
                    f'\nTime taken: {duration:.2f} seconds'
                    f'\nAverage time per item: {duration/total_processed:.3f} seconds'
                    if total_processed > 0 else ''
                )
            )
            
            # Display collection stats
            stats = embedding_service.get_collection_stats()
            if stats:
                self.stdout.write(
                    self.style.SUCCESS(
                        f'\nChromaDB Collection Stats:'
                        f'\nTotal embeddings: {stats.get("total_embeddings", "N/A")}'
                        f'\nCollection name: {stats.get("collection_name", "N/A")}'
                        f'\nSimilarity threshold: {stats.get("similarity_threshold", "N/A")}'
                    )
                )

        except KeyboardInterrupt:
            self.stdout.write(
                self.style.WARNING('\nProcess interrupted by user')
            )
        except Exception as e:
            raise CommandError(f'Error during embedding initialization: {str(e)}')

    def _process_folders(self, force, batch_size, delay):
        """Process folder embeddings"""
        self.stdout.write('Processing folders...')
        
        if force:
            folders = Folder.objects.all()
        else:
            folders = Folder.objects.filter(chroma_id__isnull=True)
        
        total_folders = folders.count()
        processed = 0
        
        if total_folders == 0:
            self.stdout.write('No folders to process')
            return 0
        
        self.stdout.write(f'Found {total_folders} folders to process')
        
        for i, folder in enumerate(folders.iterator(chunk_size=batch_size)):
            try:
                success = embedding_service.add_folder_embedding(folder)
                if success:
                    processed += 1
                    if (i + 1) % 10 == 0:
                        self.stdout.write(f'  Processed {i + 1}/{total_folders} folders')
                else:
                    self.stdout.write(
                        self.style.WARNING(f'Failed to process folder: {folder.name}')
                    )
                
                # Add delay to avoid overwhelming the system
                if delay > 0:
                    time.sleep(delay)
                    
            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(f'Error processing folder {folder.name}: {str(e)}')
                )
        
        return processed

    def _process_files(self, force, batch_size, delay):
        """Process file embeddings"""
        self.stdout.write('Processing files...')
        
        if force:
            files = File.objects.all()
        else:
            files = File.objects.filter(chroma_id__isnull=True)
        
        total_files = files.count()
        processed = 0
        
        if total_files == 0:
            self.stdout.write('No files to process')
            return 0
        
        self.stdout.write(f'Found {total_files} files to process')
        
        for i, file_obj in enumerate(files.iterator(chunk_size=batch_size)):
            try:
                success = embedding_service.add_file_embedding(file_obj)
                if success:
                    processed += 1
                    if (i + 1) % 10 == 0:
                        self.stdout.write(f'  Processed {i + 1}/{total_files} files')
                else:
                    self.stdout.write(
                        self.style.WARNING(f'Failed to process file: {file_obj.name}')
                    )
                
                # Add delay to avoid overwhelming the system
                if delay > 0:
                    time.sleep(delay)
                    
            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(f'Error processing file {file_obj.name}: {str(e)}')
                )
        
        return processed
