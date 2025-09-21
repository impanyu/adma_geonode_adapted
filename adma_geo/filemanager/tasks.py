"""
Celery tasks for processing GIS files and managing embeddings
"""
from celery import shared_task
from django.contrib.auth import get_user_model
from .models import File, Folder
from .gis_utils import process_gis_file, publish_to_geoserver, bundle_and_publish_shapefile
import logging

logger = logging.getLogger(__name__)
User = get_user_model()

@shared_task(bind=True)
def process_gis_file_task(self, file_id):
    """
    Process a GIS file in the background
    """
    try:
        file_obj = File.objects.get(id=file_id)
        
        # Update status to processing
        file_obj.gis_status = 'processing'
        file_obj.processing_log = "Starting GIS file processing..."
        file_obj.save()
        
        # Process the file
        success, message = process_gis_file(file_obj)
        
        if success:
            file_obj.processing_log += f"\n✓ Processing completed: {message}"
            file_obj.save()
            
            # Trigger publishing to GeoServer
            publish_to_geoserver_task.delay(file_id)
            
            return f"Successfully processed GIS file: {file_obj.name}"
        else:
            file_obj.gis_status = 'error'
            file_obj.processing_log += f"\n✗ Processing failed: {message}"
            file_obj.save()
            
            return f"Failed to process GIS file: {message}"
            
    except File.DoesNotExist:
        return f"File with ID {file_id} not found"
    except Exception as e:
        logger.error(f"Error in process_gis_file_task: {str(e)}")
        try:
            file_obj = File.objects.get(id=file_id)
            file_obj.gis_status = 'error'
            file_obj.processing_log += f"\n✗ Task error: {str(e)}"
            file_obj.save()
        except:
            pass
        return f"Error processing GIS file: {str(e)}"

@shared_task(bind=True)
def publish_to_geoserver_task(self, file_id):
    """
    Publish processed GIS file to GeoServer
    """
    try:
        file_obj = File.objects.get(id=file_id)
        
        # Check if file is processed
        if file_obj.gis_status != 'processed':
            return f"File {file_obj.name} is not in processed state"
        
        # Check if this is a shapefile that needs bundling
        if file_obj.name.lower().endswith('.shp'):
            logger.info(f"Delegating shapefile to delayed publishing task: {file_obj.name}")
            # Use delayed task to allow all components to upload first
            delayed_shapefile_publish_task.apply_async(args=[file_id], countdown=30)  # Wait 30 seconds
            return f"Scheduled delayed publishing for shapefile: {file_obj.name}"
        
        # Use regular publishing for other file types
        success, message = publish_to_geoserver(file_obj)
        
        if success:
            file_obj.processing_log += f"\n✓ Published to GeoServer: {message}"
            file_obj.save()
            return f"Successfully published to GeoServer: {file_obj.name}"
        else:
            file_obj.gis_status = 'error'
            file_obj.processing_log += f"\n✗ Publishing failed: {message}"
            file_obj.save()
            return f"Failed to publish to GeoServer: {message}"
            
    except File.DoesNotExist:
        return f"File with ID {file_id} not found"
    except Exception as e:
        logger.error(f"Error in publish_to_geoserver_task: {str(e)}")
        try:
            file_obj = File.objects.get(id=file_id)
            file_obj.gis_status = 'error'
            file_obj.processing_log += f"\n✗ Publishing error: {str(e)}"
            file_obj.save()
        except:
            pass
        return f"Error publishing to GeoServer: {str(e)}"

@shared_task
def process_folder_gis_files(folder_id):
    """
    Process all GIS files in a folder (when a folder is uploaded as ZIP)
    """
    try:
        from .models import Folder
        
        folder = Folder.objects.get(id=folder_id)
        gis_files = folder.files.filter(is_spatial=True, gis_status='pending')
        
        processed_count = 0
        
        for file_obj in gis_files:
            # Trigger processing for each GIS file
            process_gis_file_task.delay(str(file_obj.id))
            processed_count += 1
        
        return f"Triggered processing for {processed_count} GIS files in folder {folder.name}"
        
    except Exception as e:
        logger.error(f"Error in process_folder_gis_files: {str(e)}")
        return f"Error processing folder GIS files: {str(e)}"

@shared_task(bind=True)
def delayed_shapefile_publish_task(self, file_id, max_retries=5):
    """
    Delayed task to publish shapefiles after allowing time for all components to upload
    """
    try:
        file_obj = File.objects.get(id=file_id)
        
        # Check if file is still in processed state (not already published)
        if file_obj.gis_status != 'processed':
            logger.info(f"Shapefile {file_obj.name} is no longer in processed state: {file_obj.gis_status}")
            return f"Shapefile {file_obj.name} status: {file_obj.gis_status}"
        
        # Check if this is a shapefile
        if not file_obj.name.lower().endswith('.shp'):
            logger.warning(f"File {file_obj.name} is not a shapefile, using regular publishing")
            return publish_to_geoserver_task.delay(file_id)
        
        # Get the base name and check for components
        base_name = file_obj.name.replace('.shp', '')
        required_exts = ['.shp', '.shx', '.dbf']
        
        # Find all related components
        components = File.objects.filter(
            owner=file_obj.owner,
            name__startswith=base_name,
            folder=file_obj.folder
        )
        
        # Check if we have all required components
        available_exts = set()
        for comp in components:
            parts = comp.name.split('.')
            if len(parts) >= 2:
                ext = '.' + parts[-1].lower()
                available_exts.add(ext)
        
        missing_components = [ext for ext in required_exts if ext not in available_exts]
        
        if missing_components:
            # If we're missing components and haven't hit max retries, retry later
            if self.request.retries < max_retries:
                logger.info(f"Missing components {missing_components} for {file_obj.name}, retrying in 10 seconds (attempt {self.request.retries + 1}/{max_retries})")
                raise self.retry(countdown=10, max_retries=max_retries)
            else:
                # Max retries reached, log error and continue with what we have
                logger.error(f"Max retries reached for {file_obj.name}, missing components: {missing_components}")
                file_obj.gis_status = 'error'
                file_obj.processing_log += f"\n✗ Missing shapefile components after {max_retries} retries: {', '.join(missing_components)}"
                file_obj.save()
                return f"Failed to find all components for {file_obj.name}"
        
        # All components available, proceed with bundling and publishing
        logger.info(f"All components available for {file_obj.name}, proceeding with bundling")
        success, message = bundle_and_publish_shapefile(file_obj)
        
        if success:
            file_obj.processing_log += f"\n✓ Auto-published to GeoServer: {message}"
            file_obj.save()
            logger.info(f"Successfully auto-published shapefile: {file_obj.name}")
            return f"Successfully auto-published shapefile: {file_obj.name}"
        else:
            file_obj.gis_status = 'error'
            file_obj.processing_log += f"\n✗ Auto-publishing failed: {message}"
            file_obj.save()
            logger.error(f"Failed to auto-publish shapefile: {file_obj.name} - {message}")
            return f"Failed to auto-publish shapefile: {message}"
            
    except File.DoesNotExist:
        return f"File with ID {file_id} not found"
    except Exception as e:
        if 'retry' not in str(e):  # Don't log retry exceptions
            logger.error(f"Error in delayed_shapefile_publish_task: {str(e)}")
        raise  # Re-raise for Celery to handle retries

@shared_task(bind=True)
def generate_file_embedding_task(self, file_id):
    """
    Generate embedding for a file in the background
    """
    try:
        file_obj = File.objects.get(id=file_id)
        
        from .embedding_service import embedding_service
        success = embedding_service.add_file_embedding(file_obj)
        
        if success:
            logger.info(f"Generated embedding for file: {file_obj.name}")
            return f"Generated embedding for file: {file_obj.name}"
        else:
            logger.error(f"Failed to generate embedding for file: {file_obj.name}")
            return f"Failed to generate embedding for file: {file_obj.name}"
            
    except File.DoesNotExist:
        logger.error(f"File with ID {file_id} not found")
        return f"File with ID {file_id} not found"
    except Exception as e:
        logger.error(f"Error generating file embedding: {str(e)}")
        return f"Error generating file embedding: {str(e)}"

@shared_task(bind=True)
def generate_folder_embedding_task(self, folder_id):
    """
    Generate embedding for a folder in the background
    """
    try:
        folder_obj = Folder.objects.get(id=folder_id)
        
        from .embedding_service import embedding_service
        success = embedding_service.add_folder_embedding(folder_obj)
        
        if success:
            logger.info(f"Generated embedding for folder: {folder_obj.name}")
            return f"Generated embedding for folder: {folder_obj.name}"
        else:
            logger.error(f"Failed to generate embedding for folder: {folder_obj.name}")
            return f"Failed to generate embedding for folder: {folder_obj.name}"
            
    except Folder.DoesNotExist:
        logger.error(f"Folder with ID {folder_id} not found")
        return f"Folder with ID {folder_id} not found"
    except Exception as e:
        logger.error(f"Error generating folder embedding: {str(e)}")
        return f"Error generating folder embedding: {str(e)}"

@shared_task(bind=True)
def generate_recursive_embeddings_task(self, folder_id):
    """
    Generate embeddings for a folder and all its contents recursively
    """
    try:
        folder_obj = Folder.objects.get(id=folder_id)
        
        from .embedding_service import embedding_service
        
        # Generate embedding for this folder
        embedding_service.add_folder_embedding(folder_obj)
        
        # Generate embeddings for all files in this folder
        for file_obj in folder_obj.files.all():
            embedding_service.add_file_embedding(file_obj)
        
        # Recursively process subfolders
        for subfolder in folder_obj.subfolders.all():
            generate_recursive_embeddings_task.delay(str(subfolder.id))
        
        logger.info(f"Generated recursive embeddings for folder: {folder_obj.name}")
        return f"Generated recursive embeddings for folder: {folder_obj.name}"
        
    except Folder.DoesNotExist:
        logger.error(f"Folder with ID {folder_id} not found")
        return f"Folder with ID {folder_id} not found"
    except Exception as e:
        logger.error(f"Error generating recursive embeddings: {str(e)}")
        return f"Error generating recursive embeddings: {str(e)}"

@shared_task
def update_all_embeddings_task():
    """
    Update embeddings for all files and folders in the system
    """
    try:
        from .embedding_service import embedding_service
        files_updated, folders_updated = embedding_service.update_all_embeddings()
        
        message = f"Updated embeddings: {files_updated} files, {folders_updated} folders"
        logger.info(message)
        return message
        
    except Exception as e:
        logger.error(f"Error updating all embeddings: {str(e)}")
        return f"Error updating all embeddings: {str(e)}"

@shared_task
def generate_map_embedding_task(map_id):
    """
    Generate embeddings for a specific map
    """
    try:
        from .models import Map
        from .embedding_service import embedding_service
        
        map_obj = Map.objects.get(id=map_id)
        success = embedding_service.generate_map_embedding(map_obj)
        
        if success:
            message = f"Successfully generated embeddings for map: {map_obj.name}"
            logger.info(message)
            return message
        else:
            message = f"Failed to generate embeddings for map: {map_obj.name}"
            logger.error(message)
            return message
        
    except Map.DoesNotExist:
        logger.error(f"Map with ID {map_id} not found")
        return f"Map with ID {map_id} not found"
    except Exception as e:
        logger.error(f"Error generating map embeddings: {str(e)}")
        return f"Error generating map embeddings: {str(e)}"

@shared_task
def update_map_embeddings_task():
    """
    Update embeddings for all maps in the system
    """
    try:
        from .models import Map
        from .embedding_service import embedding_service
        
        maps_updated = 0
        for map_obj in Map.objects.all():
            try:
                if embedding_service.generate_map_embedding(map_obj):
                    maps_updated += 1
            except Exception as e:
                logger.error(f"Error updating embeddings for map {map_obj.name}: {str(e)}")
                continue
        
        message = f"Updated embeddings for {maps_updated} maps"
        logger.info(message)
        return message
        
    except Exception as e:
        logger.error(f"Error updating map embeddings: {str(e)}")
        return f"Error updating map embeddings: {str(e)}"
