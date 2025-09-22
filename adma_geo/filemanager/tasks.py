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

# NOTE: File embedding generation task removed - no longer needed
# Search now uses PostgreSQL text matching instead of embeddings

# NOTE: Folder embedding generation task removed - no longer needed
# Search now uses PostgreSQL text matching instead of embeddings

# NOTE: Recursive embedding generation task removed - no longer needed
# Search now uses PostgreSQL text matching instead of embeddings

# NOTE: All embedding-related tasks removed - no longer needed
# Search now uses PostgreSQL text matching instead of embeddings


@shared_task(bind=True)
def delete_file_async_task(self, file_id, file_name):
    """
    Asynchronously delete a file with complete cleanup of all dependencies
    """
    import django
    django.setup()
    
    try:
        from django.apps import apps
        File = apps.get_model('filemanager', 'File')
        
        logger.info(f"Starting async deletion of file: {file_name} (ID: {file_id})")
        
        # Get the file object
        try:
            file_obj = File.objects.get(id=file_id)
        except File.DoesNotExist:
            logger.warning(f"File {file_name} (ID: {file_id}) not found - may have been already deleted")
            return f"File {file_name} not found - may have been already deleted"
        
        # Perform complete file deletion
        from .views import delete_file_complete
        delete_file_complete(file_obj)
        
        logger.info(f"Successfully completed async deletion of file: {file_name}")
        return f"Successfully deleted file: {file_name}"
        
    except Exception as e:
        logger.error(f"Error in async file deletion for {file_name}: {str(e)}")
        return f"Error deleting file {file_name}: {str(e)}"


@shared_task(bind=True)
def delete_folder_async_task(self, folder_id, folder_name):
    """
    Asynchronously delete a folder with complete recursive cleanup of all dependencies
    """
    import django
    django.setup()
    
    try:
        from django.apps import apps
        Folder = apps.get_model('filemanager', 'Folder')
        
        logger.info(f"Starting async deletion of folder: {folder_name} (ID: {folder_id})")
        
        # Get the folder object
        try:
            folder_obj = Folder.objects.get(id=folder_id)
        except Folder.DoesNotExist:
            logger.warning(f"Folder {folder_name} (ID: {folder_id}) not found - may have been already deleted")
            return f"Folder {folder_name} not found - may have been already deleted"
        
        # Perform complete folder deletion
        from .views import delete_folder_complete
        delete_folder_complete(folder_obj)
        
        logger.info(f"Successfully completed async deletion of folder: {folder_name}")
        return f"Successfully deleted folder: {folder_name}"
        
    except Exception as e:
        logger.error(f"Error in async folder deletion for {folder_name}: {str(e)}")
        return f"Error deleting folder {folder_name}: {str(e)}"


@shared_task(bind=True)
def toggle_folder_visibility_recursive_task(self, folder_id, is_public, folder_name):
    """
    Asynchronously toggle folder visibility recursively for all subfolders and files
    """
    import django
    django.setup()
    
    try:
        from django.apps import apps
        Folder = apps.get_model('filemanager', 'Folder')
        File = apps.get_model('filemanager', 'File')
        
        logger.info(f"Starting async recursive visibility toggle for folder: {folder_name} (ID: {folder_id}) to {'public' if is_public else 'private'}")
        
        # Get the folder object
        try:
            folder_obj = Folder.objects.get(id=folder_id)
        except Folder.DoesNotExist:
            logger.warning(f"Folder {folder_name} (ID: {folder_id}) not found - may have been deleted")
            return f"Folder {folder_name} not found - may have been deleted"
        
        # Recursive function to update visibility (skip the main folder since it's already updated)
        def update_recursive_visibility(current_folder, visibility, skip_current=False):
            count = 0
            
            # Update current folder (skip if this is the main folder)
            if not skip_current:
                current_folder.is_public = visibility
                current_folder.save(update_fields=['is_public'])
                count += 1
                logger.info(f"Updated folder: {current_folder.name} to {'public' if visibility else 'private'}")
            
            # Update all files in current folder
            for file_obj in current_folder.files.all():
                file_obj.is_public = visibility
                file_obj.save(update_fields=['is_public'])
                count += 1
                logger.info(f"Updated file: {file_obj.name} to {'public' if visibility else 'private'}")
            
            # Recursively update all subfolders
            for subfolder in current_folder.subfolders.all():
                count += update_recursive_visibility(subfolder, visibility, skip_current=False)
            
            return count
        
        # Perform recursive update (skip main folder since view already updated it)
        total_updated = update_recursive_visibility(folder_obj, is_public, skip_current=True)
        
        # Note: Embedding updates will be handled separately if needed
        logger.info(f"Completed recursive visibility update for folder {folder_name}")
        
        success_message = f"Successfully updated visibility for {total_updated} items (folder + subfolders + files) in {folder_name}"
        logger.info(success_message)
        return success_message
        
    except Exception as e:
        error_message = f"Error in async visibility toggle for {folder_name}: {str(e)}"
        logger.error(error_message)
        return error_message


@shared_task(bind=True)
def toggle_file_visibility_task(self, file_id, is_public, file_name):
    """
    Asynchronously toggle file visibility and update embeddings
    """
    import django
    django.setup()
    
    try:
        from django.apps import apps
        File = apps.get_model('filemanager', 'File')
        
        logger.info(f"Starting async visibility toggle for file: {file_name} (ID: {file_id}) to {'public' if is_public else 'private'}")
        
        # Get the file object
        try:
            file_obj = File.objects.get(id=file_id)
        except File.DoesNotExist:
            logger.warning(f"File {file_name} (ID: {file_id}) not found - may have been deleted")
            return f"File {file_name} not found - may have been deleted"
        
        # Update file visibility
        file_obj.is_public = is_public
        file_obj.save(update_fields=['is_public'])
        
        # Note: Embedding updates will be handled separately if needed
        logger.info(f"Completed visibility update for file {file_name}")
        
        success_message = f"Successfully updated visibility for file {file_name} to {'public' if is_public else 'private'}"
        logger.info(success_message)
        return success_message
        
    except Exception as e:
        error_message = f"Error in async visibility toggle for {file_name}: {str(e)}"
        logger.error(error_message)
        return error_message
