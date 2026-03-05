"""
Celery tasks for processing GIS files and managing embeddings
"""
from celery import shared_task
from django.contrib.auth import get_user_model
from .models import File, Folder
from .gis_utils import process_gis_file, publish_to_geoserver, bundle_and_publish_shapefile
from datetime import date
import json
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


@shared_task(bind=True)
def run_seeding_tool_task(self, file_id, output_dir_id=None):
    """
    Run the Seeding Tool on a .shp or .gpkg file.
    
    This task:
    1. Reads the input Point layer
    2. Creates seeding polygons (buffered by swath/boom width)
    3. Creates boundary polygons
    4. Generates a summary CSV
    5. Saves output files in the specified output directory or creates 'seeding_tool_output' folder
    6. Creates File records for the output files in Django
    
    Args:
        file_id: ID of the input File object
        output_dir_id: Optional ID of the Folder object for output. If not specified,
                       creates a 'seeding_tool_output' subdirectory in the input file's folder.
    """
    from django.conf import settings
    import os
    
    try:
        logger.info(f"Starting Seeding Tool task for file ID: {file_id}")
        
        # Get the file object
        try:
            file_obj = File.objects.get(id=file_id)
        except File.DoesNotExist:
            logger.error(f"File with ID {file_id} not found")
            return {"success": False, "error": f"File with ID {file_id} not found"}
        
        # Validate file type
        file_ext = os.path.splitext(file_obj.name)[1].lower()
        if file_ext not in ['.shp', '.gpkg']:
            error_msg = f"Seeding Tool only supports .shp and .gpkg files, got: {file_ext}"
            logger.error(error_msg)
            return {"success": False, "error": error_msg}
        
        # Get the input file path
        input_path = file_obj.file.path
        
        # Determine output directory and folder
        from .models import Folder
        output_folder_obj = None
        
        if output_dir_id:
            # Use specified output folder
            try:
                output_folder_obj = Folder.objects.get(id=output_dir_id)
            except Folder.DoesNotExist:
                logger.error(f"Output folder with ID {output_dir_id} not found")
                return {"success": False, "error": f"Output folder with ID {output_dir_id} not found"}
        else:
            # Auto-create 'seeding_tool_output' folder under the input file's parent folder
            # First, create a Django Folder record
            parent_folder = file_obj.folder  # This is the input file's parent folder (can be None for root)
            
            # Check if a folder with this name already exists
            existing_folder = Folder.objects.filter(
                name="seeding_tool_output",
                parent=parent_folder,
                owner=file_obj.owner
            ).first()
            
            if existing_folder:
                output_folder_obj = existing_folder
                logger.info(f"Using existing 'seeding_tool_output' folder: {output_folder_obj.id}")
            else:
                # Create new folder record
                output_folder_obj = Folder.objects.create(
                    name="seeding_tool_output",
                    parent=parent_folder,
                    owner=file_obj.owner,
                    is_public=file_obj.is_public  # Inherit visibility from source file
                )
                logger.info(f"Created new 'seeding_tool_output' folder: {output_folder_obj.id}")
        
        # Build the full output directory path under MEDIA_ROOT/uploads
        # output_folder_obj.get_full_path() returns relative path like "All the Strips/.../seeding_tool_output"
        # Files must be under MEDIA_ROOT/uploads/ for Django FileField to find them
        relative_output_dir = output_folder_obj.get_full_path()
        output_dir = os.path.join(settings.MEDIA_ROOT, 'uploads', relative_output_dir)
        
        # Ensure output directory exists on filesystem
        os.makedirs(output_dir, exist_ok=True)
        
        logger.info(f"Processing file: {input_path}")
        logger.info(f"Output directory: {output_dir}")
        
        # Import and run the seeding tool
        from .SeedingTool_asappled_single_ADMA_V1 import process_seeding_tool
        
        success, message, output_files = process_seeding_tool(input_path, output_dir)
        
        if not success:
            logger.error(f"Seeding Tool failed: {message}")
            # Update file processing log
            file_obj.processing_log = (file_obj.processing_log or "") + f"\n✗ Seeding Tool failed: {message}"
            file_obj.save(update_fields=['processing_log'])
            return {"success": False, "error": message}
        
        logger.info(f"Seeding Tool completed: {message}")
        
        # Create File records for the output files
        created_files = []
        
        # Helper function to create/update a file record
        def create_file_record(file_path):
            if not os.path.exists(file_path):
                return None
            try:
                # Calculate relative path for Django FileField
                media_root = settings.MEDIA_ROOT
                if file_path.startswith(str(media_root)):
                    relative_path = os.path.relpath(file_path, media_root)
                else:
                    relative_path = file_path
                
                file_name = os.path.basename(file_path)
                file_size = os.path.getsize(file_path)
                
                # Check if file already exists (by name and folder)
                existing_file = File.objects.filter(
                    name=file_name,
                    folder=output_folder_obj,
                    owner=file_obj.owner
                ).first()
                
                if existing_file:
                    # Update existing file
                    existing_file.file_size = file_size
                    existing_file.save(update_fields=['file_size', 'updated_at'])
                    logger.info(f"Updated existing file: {file_name}")
                    return {
                        'name': file_name,
                        'id': str(existing_file.id),
                        'updated': True
                    }
                else:
                    # Create new file record in the output folder
                    new_file = File(
                        name=file_name,
                        folder=output_folder_obj,
                        owner=file_obj.owner,
                        file_size=file_size,
                        is_public=file_obj.is_public,  # Inherit visibility from source file
                    )
                    # Set the file field to the relative path
                    new_file.file.name = relative_path
                    new_file.save()
                    
                    logger.info(f"Created new file record: {file_name}")
                    return {
                        'name': file_name,
                        'id': str(new_file.id),
                        'updated': False
                    }
                    
            except Exception as e:
                logger.error(f"Error creating file record for {file_path}: {e}")
                return None
        
        # Process shapefile components (all files in polygons_components and boundary_components)
        for component_key in ['polygons_components', 'boundary_components']:
            if component_key in output_files:
                for file_path in output_files[component_key]:
                    result = create_file_record(file_path)
                    if result:
                        created_files.append(result)
        
        # Process CSV summary file
        if 'summary' in output_files:
            summary_path = output_files['summary']
            logger.info(f"Processing summary CSV: {summary_path}")
            result = create_file_record(summary_path)
            if result:
                created_files.append(result)
            else:
                logger.warning(f"Failed to create file record for summary: {summary_path}")
        
        # Update source file processing log
        file_obj.processing_log = (file_obj.processing_log or "") + f"\n✓ Seeding Tool completed: {message}"
        file_obj.save(update_fields=['processing_log'])
        
        # Build output_files dict with just basenames for the result
        output_files_result = {}
        for k, v in output_files.items():
            if isinstance(v, str):
                output_files_result[k] = os.path.basename(v)
            elif isinstance(v, list):
                output_files_result[k] = [os.path.basename(f) for f in v]
        
        result = {
            "success": True,
            "message": message,
            "created_files": created_files,
            "output_files": output_files_result
        }
        
        logger.info(f"Seeding Tool task completed successfully: {result}")
        return result
        
    except Exception as e:
        logger.exception(f"Error in Seeding Tool task for file {file_id}")
        return {"success": False, "error": str(e)}


@shared_task(bind=True)
def run_shape_to_json_task(self, file_id, output_dir_id=None):
    """
    Convert a shapefile to GeoJSON format.
    
    This task:
    1. Reads the input shapefile
    2. Converts to WGS84 (EPSG:4326) for GeoJSON compatibility
    3. Saves the GeoJSON file
    4. Creates a File record for the output in Django
    
    Args:
        file_id: ID of the input File object
        output_dir_id: Optional ID of the Folder object for output. If not specified,
                       creates a 'geojson_output' subdirectory in the input file's folder.
    """
    from django.conf import settings
    import os
    
    try:
        logger.info(f"Starting Shape to JSON task for file ID: {file_id}")
        
        # Get the file object
        try:
            file_obj = File.objects.get(id=file_id)
        except File.DoesNotExist:
            logger.error(f"File with ID {file_id} not found")
            return {"success": False, "error": f"File with ID {file_id} not found"}
        
        # Validate file type
        file_ext = os.path.splitext(file_obj.name)[1].lower()
        if file_ext != '.shp':
            error_msg = f"Shape to JSON only supports .shp files, got: {file_ext}"
            logger.error(error_msg)
            return {"success": False, "error": error_msg}
        
        # Get the input file path
        input_path = file_obj.file.path
        
        # Determine output directory and folder
        from .models import Folder
        output_folder_obj = None
        
        if output_dir_id:
            # Use specified output folder
            try:
                output_folder_obj = Folder.objects.get(id=output_dir_id)
            except Folder.DoesNotExist:
                logger.error(f"Output folder with ID {output_dir_id} not found")
                return {"success": False, "error": f"Output folder with ID {output_dir_id} not found"}
        else:
            # Auto-create 'geojson_output' folder under the input file's parent folder
            parent_folder = file_obj.folder
            
            # Check if a folder with this name already exists
            existing_folder = Folder.objects.filter(
                name="geojson_output",
                parent=parent_folder,
                owner=file_obj.owner
            ).first()
            
            if existing_folder:
                output_folder_obj = existing_folder
                logger.info(f"Using existing 'geojson_output' folder: {output_folder_obj.id}")
            else:
                # Create new folder record
                output_folder_obj = Folder.objects.create(
                    name="geojson_output",
                    parent=parent_folder,
                    owner=file_obj.owner,
                    is_public=file_obj.is_public
                )
                logger.info(f"Created new 'geojson_output' folder: {output_folder_obj.id}")
        
        # Build the full output directory path under MEDIA_ROOT/uploads
        relative_output_dir = output_folder_obj.get_full_path()
        output_dir = os.path.join(settings.MEDIA_ROOT, 'uploads', relative_output_dir)
        
        # Ensure output directory exists on filesystem
        os.makedirs(output_dir, exist_ok=True)
        
        logger.info(f"Processing file: {input_path}")
        logger.info(f"Output directory: {output_dir}")
        
        # Import and run the shape to json tool
        from .Shape_To_Json import process_shape_to_json
        
        success, message, output_files = process_shape_to_json(input_path, output_dir)
        
        if not success:
            logger.error(f"Shape to JSON failed: {message}")
            file_obj.processing_log = (file_obj.processing_log or "") + f"\n✗ Shape to JSON failed: {message}"
            file_obj.save(update_fields=['processing_log'])
            return {"success": False, "error": message}
        
        logger.info(f"Shape to JSON completed: {message}")
        
        # Create File records for the output files
        created_files = []
        
        for file_type, file_path in output_files.items():
            if os.path.exists(file_path):
                try:
                    media_root = settings.MEDIA_ROOT
                    if file_path.startswith(str(media_root)):
                        relative_path = os.path.relpath(file_path, media_root)
                    else:
                        relative_path = file_path
                    
                    file_name = os.path.basename(file_path)
                    file_size = os.path.getsize(file_path)
                    
                    # Check if file already exists
                    existing_file = File.objects.filter(
                        name=file_name,
                        folder=output_folder_obj,
                        owner=file_obj.owner
                    ).first()
                    
                    if existing_file:
                        existing_file.file_size = file_size
                        existing_file.save(update_fields=['file_size', 'updated_at'])
                        created_files.append({
                            'name': file_name,
                            'id': str(existing_file.id),
                            'updated': True
                        })
                        logger.info(f"Updated existing file: {file_name}")
                    else:
                        new_file = File(
                            name=file_name,
                            folder=output_folder_obj,
                            owner=file_obj.owner,
                            file_size=file_size,
                            is_public=file_obj.is_public,
                        )
                        new_file.file.name = relative_path
                        new_file.save()
                        
                        created_files.append({
                            'name': file_name,
                            'id': str(new_file.id),
                            'updated': False
                        })
                        logger.info(f"Created new file record: {file_name}")
                        
                except Exception as e:
                    logger.error(f"Error creating file record for {file_path}: {e}")
        
        # Update source file processing log
        file_obj.processing_log = (file_obj.processing_log or "") + f"\n✓ Shape to JSON completed: {message}"
        file_obj.save(update_fields=['processing_log'])
        
        result = {
            "success": True,
            "message": message,
            "created_files": created_files,
            "output_files": {k: os.path.basename(v) for k, v in output_files.items()}
        }
        
        logger.info(f"Shape to JSON task completed successfully: {result}")
        return result
        
    except Exception as e:
        logger.exception(f"Error in Shape to JSON task for file {file_id}")
        return {"success": False, "error": str(e)}


@shared_task(bind=True)
def run_si_tool_task(
    self,
    treatment,
    imagery,
    si_column_name,
    field_column,
    buffer_sectors_file_id,
    ndre_file_id=None,
    csv_file_id=None,
    indicator_block_file_id=None,
    output_dir_id=None
):
    """
    Run the SI (Stress Index) Tool.
    
    This task:
    1. Validates inputs based on treatment/imagery combination
    2. Runs the appropriate SI calculation workflow
    3. Updates the CSV with SI values
    4. Creates File records for output files
    
    Args:
        treatment: 'STANDARD' or 'SBF'
        imagery: 'UAV' or 'SATELLITE'
        si_column_name: Column name for SI values (e.g., 'SI_08_01')
        field_column: Field column for grouping (e.g., 'Plot_Numbe')
        buffer_sectors_file_id: ID of the buffer sectors shapefile
        ndre_file_id: ID of the NDRE shapefile (for UAV)
        csv_file_id: ID of the CSV file to update
        indicator_block_file_id: ID of indicator block shapefile (for SBF)
        output_dir_id: Optional ID of output folder
    """
    from django.conf import settings
    import os
    
    try:
        logger.info(f"Starting SI Tool task: {treatment} + {imagery}")
        
        # Get buffer sectors file (required for all workflows)
        try:
            buffer_file = File.objects.get(id=buffer_sectors_file_id)
            buffer_sectors_path = buffer_file.file.path
        except File.DoesNotExist:
            return {"success": False, "error": "Buffer sectors file not found"}
        
        # Get NDRE file (required for UAV)
        ndre_path = None
        if ndre_file_id:
            try:
                ndre_file = File.objects.get(id=ndre_file_id)
                ndre_path = ndre_file.file.path
            except File.DoesNotExist:
                return {"success": False, "error": "NDRE file not found"}
        
        # Get CSV file (required for all workflows)
        csv_path = None
        csv_file = None
        if csv_file_id:
            try:
                csv_file = File.objects.get(id=csv_file_id)
                csv_path = csv_file.file.path
            except File.DoesNotExist:
                return {"success": False, "error": "CSV file not found"}
        
        # Get indicator block file (required for SBF)
        indicator_block_path = None
        if indicator_block_file_id:
            try:
                indicator_file = File.objects.get(id=indicator_block_file_id)
                indicator_block_path = indicator_file.file.path
            except File.DoesNotExist:
                return {"success": False, "error": "Indicator block file not found"}
        
        # Determine output directory
        from .models import Folder
        output_folder_obj = None
        
        if output_dir_id:
            try:
                output_folder_obj = Folder.objects.get(id=output_dir_id)
            except Folder.DoesNotExist:
                return {"success": False, "error": "Output folder not found"}
        else:
            # Auto-create 'si_tool_output' folder under the buffer file's parent folder
            parent_folder = buffer_file.folder
            
            existing_folder = Folder.objects.filter(
                name="si_tool_output",
                parent=parent_folder,
                owner=buffer_file.owner
            ).first()
            
            if existing_folder:
                output_folder_obj = existing_folder
            else:
                output_folder_obj = Folder.objects.create(
                    name="si_tool_output",
                    parent=parent_folder,
                    owner=buffer_file.owner,
                    is_public=buffer_file.is_public
                )
        
        # Build the full output directory path under MEDIA_ROOT/uploads
        relative_output_dir = output_folder_obj.get_full_path()
        output_dir = os.path.join(settings.MEDIA_ROOT, 'uploads', relative_output_dir)
        
        os.makedirs(output_dir, exist_ok=True)
        
        logger.info(f"Processing SI Tool: treatment={treatment}, imagery={imagery}")
        logger.info(f"Output directory: {output_dir}")
        
        # Import and run the SI tool
        from .ADMA_SI_Tool import process_si_tool
        
        success, message, output_files = process_si_tool(
            treatment=treatment,
            imagery=imagery,
            si_column_name=si_column_name,
            field_column=field_column,
            buffer_sectors_path=buffer_sectors_path,
            ndre_path=ndre_path,
            csv_path=csv_path,
            indicator_block_path=indicator_block_path,
            output_dir=output_dir
        )
        
        if not success:
            logger.error(f"SI Tool failed: {message}")
            buffer_file.processing_log = (buffer_file.processing_log or "") + f"\n✗ SI Tool failed: {message}"
            buffer_file.save(update_fields=['processing_log'])
            return {"success": False, "error": message}
        
        logger.info(f"SI Tool completed: {message}")
        
        # Create File records for output files
        created_files = []
        
        for file_type, file_path in output_files.items():
            if os.path.exists(file_path):
                try:
                    media_root = settings.MEDIA_ROOT
                    if file_path.startswith(str(media_root)):
                        relative_path = os.path.relpath(file_path, media_root)
                    else:
                        relative_path = file_path
                    
                    file_name = os.path.basename(file_path)
                    file_size = os.path.getsize(file_path)
                    
                    existing_file = File.objects.filter(
                        name=file_name,
                        folder=output_folder_obj,
                        owner=buffer_file.owner
                    ).first()
                    
                    if existing_file:
                        existing_file.file_size = file_size
                        existing_file.save(update_fields=['file_size', 'updated_at'])
                        created_files.append({
                            'name': file_name,
                            'id': str(existing_file.id),
                            'updated': True
                        })
                    else:
                        new_file = File(
                            name=file_name,
                            folder=output_folder_obj,
                            owner=buffer_file.owner,
                            file_size=file_size,
                            is_public=buffer_file.is_public,
                        )
                        new_file.file.name = relative_path
                        new_file.save()
                        
                        created_files.append({
                            'name': file_name,
                            'id': str(new_file.id),
                            'updated': False
                        })
                        
                except Exception as e:
                    logger.error(f"Error creating file record for {file_path}: {e}")
        
        # Update source file processing log
        buffer_file.processing_log = (buffer_file.processing_log or "") + f"\n✓ SI Tool completed: {message}"
        buffer_file.save(update_fields=['processing_log'])
        
        result = {
            "success": True,
            "message": message,
            "created_files": created_files,
            "output_files": {k: os.path.basename(v) for k, v in output_files.items()}
        }
        
        logger.info(f"SI Tool task completed successfully: {result}")
        return result
        
    except Exception as e:
        logger.exception(f"Error in SI Tool task")
        return {"success": False, "error": str(e)}


def _generate_all_json_for_device(device_folder, dev_eui, device_name, device_type, owner):
    """
    Generate all.json file for a device by reading all daily JSON files,
    calculating daily averages for all numeric variables,
    and storing one entry per day in chronological order.
    
    This function regenerates all.json from scratch each time.
    
    Args:
        device_folder: Folder object for the device
        dev_eui: Device EUI identifier
        device_name: Friendly name of the device
        device_type: Type of device (e.g., 'weather_station')
        owner: Owner of the files
    """
    import json
    import os
    from datetime import datetime
    from django.conf import settings
    from django.core.files.base import ContentFile
    from collections import defaultdict
    
    # Get all daily JSON files for this device (excluding all.json)
    daily_files = File.objects.filter(
        folder=device_folder,
        owner=owner,
        name__startswith=f"{dev_eui}_",
        name__endswith=".json"
    ).exclude(name="all.json").order_by('name')
    
    if not daily_files.exists():
        logger.info(f"No daily files found for device {dev_eui}, skipping all.json generation")
        return
    
    daily_averages = []
    all_variable_names = set()  # Track all variable names across all days
    
    for daily_file in daily_files:
        try:
            # Read the daily JSON file
            file_path = os.path.join(settings.MEDIA_ROOT, daily_file.file.name)
            
            if not os.path.exists(file_path):
                logger.warning(f"Daily file not found on disk: {file_path}")
                continue
            
            with open(file_path, 'r') as f:
                daily_data = json.load(f)
            
            observations = daily_data.get('observations', [])
            file_date = daily_data.get('date', '')
            
            if not observations:
                continue
            
            # Collect all numeric values for each variable
            variable_values = defaultdict(list)
            
            for obs in observations:
                for key, value in obs.items():
                    # Skip non-numeric fields like 'timestamp'
                    if key in ('timestamp', 'date', 'sampled_for_hour'):
                        continue
                    
                    # Try to convert to float for averaging
                    try:
                        if value is not None:
                            numeric_value = float(value)
                            variable_values[key].append(numeric_value)
                            all_variable_names.add(key)
                    except (ValueError, TypeError):
                        # Skip non-numeric values
                        continue
            
            # Calculate daily averages
            if variable_values:
                daily_avg = {
                    'date': file_date,
                    'observation_count': len(observations),
                }
                
                for var_name, values in variable_values.items():
                    if values:
                        avg_value = sum(values) / len(values)
                        # Round to 2 decimal places for readability
                        daily_avg[var_name] = round(avg_value, 2)
                
                daily_averages.append(daily_avg)
                
        except Exception as e:
            logger.warning(f"Error processing daily file {daily_file.name}: {e}")
            continue
    
    # Sort by date
    daily_averages.sort(key=lambda x: x.get('date', ''))
    
    # Create the all.json content
    all_json_data = {
        'dev_eui': dev_eui,
        'device_name': device_name,
        'device_type': device_type,
        'description': 'Daily averages of all numeric variables',
        'variables': sorted(list(all_variable_names)),
        'total_days': len(daily_averages),
        'date_range': {
            'start': daily_averages[0].get('date') if daily_averages else None,
            'end': daily_averages[-1].get('date') if daily_averages else None,
        },
        'generated_at': datetime.now().isoformat(),
        'daily_averages': daily_averages,
    }
    
    json_content = json.dumps(all_json_data, indent=2, default=str)
    
    # Check if all.json already exists - delete it first to regenerate
    existing_all_json = File.objects.filter(
        name="all.json",
        folder=device_folder,
        owner=owner
    ).first()
    
    if existing_all_json:
        # Delete the old file from disk and database
        try:
            old_file_path = os.path.join(settings.MEDIA_ROOT, existing_all_json.file.name)
            if os.path.exists(old_file_path):
                os.remove(old_file_path)
        except Exception as e:
            logger.warning(f"Could not delete old all.json file: {e}")
        existing_all_json.delete()
        logger.info(f"Deleted old all.json for device {dev_eui}")
    
    # Create new all.json file
    file_obj = File(
        name="all.json",
        folder=device_folder,
        owner=owner,
        file_type='text',
        mime_type='application/json',
        is_public=True,
        is_third_party=True,
        third_party_source='realm5',
        third_party_id=f"{dev_eui}_all",
    )
    
    file_obj.file.save("all.json", ContentFile(json_content.encode('utf-8')))
    file_obj.file_size = len(json_content)
    file_obj.save()
    
    logger.info(f"Created all.json for device {dev_eui} with {len(daily_averages)} daily averages")


@shared_task(bind=True)
def sync_realm5_task(self):
    """
    Sync data from Realm5 API.
    
    This task:
    1. Gets all devices from Realm5 API
    2. Creates folders for new devices under the realm5 root folder
    3. Fetches daily observations for each device
    4. Saves observations as JSON files (one per day)
    
    This task is designed to run daily via Celery Beat.
    """
    import json
    import os
    from datetime import date, timedelta
    from django.conf import settings
    from django.core.files.base import ContentFile
    from .realm5_client import Realm5Client
    
    logger.info("Starting Realm5 sync task...")
    
    # Configuration
    REALM5_API_KEY = getattr(settings, 'REALM5_API_KEY', 'U7nEMFir1hMKTucbRsqeC2joTYGXpJy2')
    SYNC_START_DATE = date(2024, 1, 1)  # Start syncing from this date
    
    results = {
        'success': True,
        'devices_found': 0,
        'folders_created': 0,
        'files_created': 0,
        'errors': []
    }
    
    try:
        # Get the realm5 root folder
        realm5_folder = Folder.objects.filter(
            name='realm5',
            parent=None,
            is_third_party=True,
            third_party_source='realm5'
        ).first()
        
        if not realm5_folder:
            error_msg = "Realm5 root folder not found. Run 'python manage.py setup_realm5' first."
            logger.error(error_msg)
            results['success'] = False
            results['errors'].append(error_msg)
            return results
        
        owner = realm5_folder.owner
        logger.info(f"Found realm5 root folder (ID: {realm5_folder.id}, Owner: {owner.username})")
        
        # Initialize Realm5 API client
        client = Realm5Client(api_key=REALM5_API_KEY)
        
        # Step 1: Get all devices
        try:
            devices = client.get_devices()
            results['devices_found'] = len(devices)
            logger.info(f"Found {len(devices)} devices from Realm5 API")
        except Exception as e:
            error_msg = f"Failed to fetch devices from Realm5 API: {e}"
            logger.error(error_msg)
            results['success'] = False
            results['errors'].append(error_msg)
            return results
        
        # Step 2: Clean up existing non-weather-station folders
        # We only want folders for weather_station devices
        # Build a set of valid folder identifiers (friendly_names and dev_euis for backward compatibility)
        weather_station_identifiers = set()
        for device in devices:
            if device.get('device_type') == 'weather_station':
                dev_eui_hex = device.get('dev_eui_hex')
                dev_eui_numeric = device.get('dev_eui') or device.get('devEui') or device.get('id')
                dev_eui = str(dev_eui_hex) if dev_eui_hex else str(dev_eui_numeric)
                friendly_name = device.get('friendly_name') or device.get('name') or dev_eui
                # Add both friendly_name and dev_eui to allow transition period
                weather_station_identifiers.add(friendly_name)
                weather_station_identifiers.add(dev_eui)
        
        # Find and remove folders for non-weather-station devices
        existing_folders = Folder.objects.filter(parent=realm5_folder, owner=owner)
        for folder in existing_folders:
            # Check if folder matches any weather station identifier (by name or third_party_id)
            if folder.name not in weather_station_identifiers and folder.third_party_id not in weather_station_identifiers:
                logger.info(f"Removing folder for non-weather-station device: {folder.name}")
                folder.delete()
        
        # Step 3: Process only weather station devices
        for device in devices:
            device_type = device.get('device_type', 'unknown')
            
            # Skip non-weather-station devices - we only sync weather stations
            if device_type != 'weather_station':
                logger.debug(f"Skipping non-weather-station device: {device.get('dev_eui_hex')} (type: {device_type})")
                continue
            
            # Get both hex and numeric dev_eui
            # - Use hex EUI for folder names (more readable)
            # - Use numeric EUI for API calls (required by observations endpoint)
            dev_eui_hex = device.get('dev_eui_hex')
            dev_eui_numeric = device.get('dev_eui') or device.get('devEui') or device.get('id')
            
            if not dev_eui_numeric:
                logger.warning(f"Device missing dev_eui: {device}")
                continue
            
            # Use hex for dev_eui reference, numeric for API calls
            dev_eui = str(dev_eui_hex) if dev_eui_hex else str(dev_eui_numeric)
            dev_eui_for_api = str(dev_eui_numeric)  # API always needs numeric
            device_name = device.get('friendly_name') or device.get('name') or dev_eui
            
            # Use friendly_name as folder name (cleaned up)
            folder_name = device_name.strip()
            
            logger.info(f"Processing weather station: {dev_eui} ({folder_name})")
            
            # Check if folder exists for this device by:
            # 1. First check by friendly_name (new behavior)
            # 2. Then check by third_party_id (dev_eui) for backward compatibility
            device_folder = Folder.objects.filter(
                name=folder_name,
                parent=realm5_folder,
                owner=owner
            ).first()
            
            found_by_third_party_id = False
            if not device_folder:
                # Also check by third_party_id (in case folder was created with old naming)
                device_folder = Folder.objects.filter(
                    third_party_id=dev_eui,
                    parent=realm5_folder,
                    owner=owner
                ).first()
                if device_folder:
                    found_by_third_party_id = True
            
            if not device_folder:
                # Create new folder for this weather station using friendly_name
                device_folder = Folder.objects.create(
                    name=folder_name,
                    parent=realm5_folder,
                    owner=owner,
                    is_public=True,  # Inherit public status from parent
                    is_third_party=True,
                    third_party_source='realm5',
                    third_party_id=dev_eui,  # Store dev_eui as ID for reference
                )
                results['folders_created'] += 1
                logger.info(f"Created folder for weather station: {folder_name} (dev_eui: {dev_eui})")
            elif found_by_third_party_id and device_folder.name != folder_name:
                # Folder was found by third_party_id but has old name - update it to friendly_name
                old_name = device_folder.name
                device_folder.name = folder_name
                device_folder.save(update_fields=['name'])
                logger.info(f"Renamed folder from '{old_name}' to '{folder_name}' (dev_eui: {dev_eui})")
            
            try:
                # Iterate from SYNC_START_DATE to yesterday, fetching observations day by day
                # We sync up to yesterday (not today) because the sync runs at 2 AM daily,
                # and today's data is not yet complete
                today = date.today()
                yesterday = today - timedelta(days=1)
                current_date = SYNC_START_DATE
                
                logger.info(f"Syncing observations for weather station: {dev_eui} from {SYNC_START_DATE} to {yesterday}")
                
                while current_date <= yesterday:
                    file_name = f"{dev_eui}_{current_date.isoformat()}.json"
                    
                    # Check if we already have a file for this date
                    existing_file = File.objects.filter(
                        name=file_name,
                        folder=device_folder,
                        owner=owner
                    ).exists()
                    
                    if existing_file:
                        logger.debug(f"File already exists, skipping: {file_name}")
                        current_date += timedelta(days=1)
                        continue
                    
                    # Fetch observations for this specific day using occurred_after/occurred_before
                    try:
                        observations = client.get_observations_by_day(dev_eui_for_api, current_date)
                        
                        if observations:
                            # Convert dict to list with timestamps
                            observations_list = [
                                {'timestamp': ts, **obs_data}
                                for ts, obs_data in observations.items()
                            ]
                            # Sort by timestamp
                            observations_list.sort(key=lambda x: x['timestamp'])
                            
                            # Create JSON file with observations
                            observation_data = {
                                'dev_eui': dev_eui,
                                'dev_eui_numeric': dev_eui_for_api,
                                'device_name': device_name,
                                'device_type': device_type,
                                'date': current_date.isoformat(),
                                'observation_count': len(observations_list),
                                'observations': observations_list,
                                'fetched_at': date.today().isoformat(),
                            }
                            
                            json_content = json.dumps(observation_data, indent=2, default=str)
                            
                            # Create file record
                            file_obj = File(
                                name=file_name,
                                folder=device_folder,
                                owner=owner,
                                file_type='text',
                                mime_type='application/json',
                                is_public=True,
                                is_third_party=True,
                                third_party_source='realm5',
                                third_party_id=f"{dev_eui}_{current_date.isoformat()}",
                            )
                            
                            # Save the file content
                            file_obj.file.save(file_name, ContentFile(json_content.encode('utf-8')))
                            file_obj.file_size = len(json_content)
                            file_obj.save()
                            
                            results['files_created'] += 1
                            logger.info(f"Created observation file: {file_name} ({len(observations_list)} observations)")
                        else:
                            logger.debug(f"No observations for {dev_eui} on {current_date}")
                            
                    except Exception as e:
                        logger.warning(f"Failed to fetch observations for {dev_eui} on {current_date}: {e}")
                        # Continue to next day even if one day fails
                    
                    current_date += timedelta(days=1)
                
                # Step 4: Generate all.json aggregated file for this device
                # This file contains daily averages of all numeric variables from all daily files
                try:
                    _generate_all_json_for_device(device_folder, dev_eui, device_name, device_type, owner)
                    logger.info(f"Generated all.json for device: {dev_eui}")
                except Exception as e:
                    logger.warning(f"Failed to generate all.json for device {dev_eui}: {e}")
                    results['errors'].append(f"Failed to generate all.json for {dev_eui}: {e}")
                    
            except Exception as e:
                error_msg = f"Error processing observations for device {dev_eui}: {e}"
                logger.error(error_msg)
                results['errors'].append(error_msg)
        
        logger.info(f"Realm5 sync completed: {results}")
        return results
        
    except Exception as e:
        logger.exception("Error in sync_realm5_task")
        results['success'] = False
        results['errors'].append(str(e))
        return results


@shared_task
def sync_realm5_scheduled():
    """
    Wrapper task for scheduled Realm5 sync.
    This is called by Celery Beat on a daily schedule.
    """
    logger.info("Running scheduled Realm5 sync...")
    return sync_realm5_task.delay()


@shared_task(bind=True)
def sync_johndeere_task(self):
    """
    Sync John Deere Operations Center data to local storage.
    
    This task:
    1. Uses refresh token to get a new access token
    2. Fetches all fields for the configured organization
    3. For each field:
       - Creates a folder if it doesn't exist
       - Saves field metadata as JSON
       - Saves field boundaries as shapefile
    4. For each field, fetches field operations:
       - Creates a folder for each operation if it doesn't exist
       - Saves operation metadata as JSON
       - Saves operation boundary/coverage as shapefile (if available)
    
    The sync is incremental - it only creates folders/files that don't exist.
    """
    from django.conf import settings
    from django.core.files.base import ContentFile
    from .johndeere_client import JohnDeereClient
    
    logger.info("Starting John Deere sync task...")
    
    # Configuration from settings
    JD_CLIENT_ID = getattr(settings, 'JD_CLIENT_ID', None)
    JD_CLIENT_SECRET = getattr(settings, 'JD_CLIENT_SECRET', None)
    JD_REFRESH_TOKEN = getattr(settings, 'JD_REFRESH_TOKEN', None)
    JD_ORG_ID = getattr(settings, 'JD_ORG_ID', '4193081')  # Default org ID
    
    results = {
        'success': True,
        'fields_found': 0,
        'field_folders_created': 0,
        'field_files_created': 0,
        'operations_found': 0,
        'operation_folders_created': 0,
        'operation_files_created': 0,
        'errors': []
    }
    
    # Validate configuration
    if not all([JD_CLIENT_ID, JD_CLIENT_SECRET, JD_REFRESH_TOKEN]):
        error_msg = "John Deere API credentials not configured. Set JD_CLIENT_ID, JD_CLIENT_SECRET, and JD_REFRESH_TOKEN in settings."
        logger.error(error_msg)
        results['success'] = False
        results['errors'].append(error_msg)
        return results
    
    try:
        # Get the John Deere root folder
        johndeere_folder = Folder.objects.filter(
            name='John Deere',
            parent=None,
            is_third_party=True,
            third_party_source='johndeere'
        ).first()
        
        if not johndeere_folder:
            error_msg = "John Deere root folder not found. Run 'python manage.py setup_johndeere' first."
            logger.error(error_msg)
            results['success'] = False
            results['errors'].append(error_msg)
            return results
        
        owner = johndeere_folder.owner
        logger.info(f"Found John Deere root folder (ID: {johndeere_folder.id}, Owner: {owner.username})")
        
        # Initialize John Deere API client
        client = JohnDeereClient(
            client_id=JD_CLIENT_ID,
            client_secret=JD_CLIENT_SECRET,
            refresh_token=JD_REFRESH_TOKEN
        )
        
        # Step 1: Get all fields for the organization
        try:
            fields = client.get_fields(JD_ORG_ID, embed_boundaries=True)
            results['fields_found'] = len(fields)
            logger.info(f"Found {len(fields)} fields from John Deere API for org {JD_ORG_ID}")
        except Exception as e:
            error_msg = f"Failed to fetch fields from John Deere API: {e}"
            logger.error(error_msg)
            results['success'] = False
            results['errors'].append(error_msg)
            return results
        
        # Step 2: Process each field
        for field in fields:
            field_id = field.get('id')
            field_name = field.get('name', field_id)
            
            if not field_id:
                logger.warning(f"Field missing ID: {field}")
                continue
            
            logger.info(f"Processing field: {field_id} ({field_name})")
            
            # Check if folder exists for this field (using field NAME as folder name)
            field_folder = Folder.objects.filter(
                name=field_name,
                parent=johndeere_folder,
                owner=owner
            ).first()
            
            field_is_new = False
            if not field_folder:
                # Create new folder for this field using the field's name
                field_folder = Folder.objects.create(
                    name=field_name,
                    parent=johndeere_folder,
                    owner=owner,
                    is_public=True,
                    is_third_party=True,
                    third_party_source='johndeere',
                    third_party_id=field_id,  # Still use ID for tracking
                )
                results['field_folders_created'] += 1
                field_is_new = True
                logger.info(f"Created folder for field: {field_name} (ID: {field_id})")
            
            # Only create metadata and boundary files if folder is new
            if field_is_new:
                # Save field metadata as JSON - use field_name for filename
                metadata_filename = f"{field_name}_metadata.json"
                field_metadata = {
                    'id': field_id,
                    'name': field_name,
                    'archived': field.get('archived'),
                    'source': field.get('source'),
                    'activeStartDate': field.get('activeStartDate'),
                    'activeEndDate': field.get('activeEndDate'),
                    'links': field.get('links', []),
                    'synced_at': str(date.today()),
                }
                
                # Check if metadata file already exists
                if not File.objects.filter(name=metadata_filename, folder=field_folder, owner=owner).exists():
                    json_content = json.dumps(field_metadata, indent=2, default=str)
                    metadata_file = File(
                        name=metadata_filename,
                        folder=field_folder,
                        owner=owner,
                        file_type='text',
                        mime_type='application/json',
                        is_public=True,
                        is_third_party=True,
                        third_party_source='johndeere',
                        third_party_id=f"{field_id}_metadata",
                    )
                    metadata_file.file.save(metadata_filename, ContentFile(json_content.encode('utf-8')))
                    metadata_file.file_size = len(json_content)
                    metadata_file.save()
                    results['field_files_created'] += 1
                    logger.info(f"Created metadata file: {metadata_filename}")
                
                # Get and save field boundaries
                boundaries = field.get('boundaries', [])
                if not boundaries:
                    # Fetch boundaries separately if not embedded
                    try:
                        boundaries = client.get_field_boundaries(JD_ORG_ID, field_id)
                    except Exception as e:
                        logger.warning(f"Failed to fetch boundaries for field {field_id}: {e}")
                        boundaries = []
                
                for boundary in boundaries:
                    boundary_id = boundary.get('id', 'boundary')
                    # Use field_name for boundary folder and file naming
                    boundary_folder_name = f"{field_name}_boundary"
                    boundary_file_basename = f"{field_name}_boundary"
                    
                    # Check if shapefile folder already exists
                    boundary_folder = Folder.objects.filter(
                        name=boundary_folder_name,
                        parent=field_folder,
                        owner=owner
                    ).first()
                    
                    if boundary_folder:
                        continue
                    
                    # Convert boundary to GeoJSON
                    geojson = client.boundary_to_geojson(boundary)
                    if not geojson:
                        logger.warning(f"Could not convert boundary {boundary_id} to GeoJSON")
                        continue
                    
                    # Convert GeoJSON to shapefile - use field_name based naming
                    shp_components = client.geojson_to_shapefile_components(geojson, boundary_file_basename)
                    if not shp_components:
                        # Save as GeoJSON instead (as a single file in the field folder)
                        geojson_filename = f"{boundary_folder_name}.geojson"
                        if not File.objects.filter(name=geojson_filename, folder=field_folder, owner=owner).exists():
                            geojson_content = json.dumps(geojson, indent=2)
                            geojson_file = File(
                                name=geojson_filename,
                                folder=field_folder,
                                owner=owner,
                                file_type='archive',
                                mime_type='application/geo+json',
                                is_public=True,
                                is_third_party=True,
                                third_party_source='johndeere',
                                third_party_id=f"{field_id}_{boundary_id}_geojson",
                            )
                            geojson_file.file.save(geojson_filename, ContentFile(geojson_content.encode('utf-8')))
                            geojson_file.file_size = len(geojson_content)
                            geojson_file.save()
                            results['field_files_created'] += 1
                            logger.info(f"Created GeoJSON file: {geojson_filename}")
                        continue
                    
                    # Create a folder for the shapefile components - use field_name based naming
                    boundary_folder = Folder.objects.create(
                        name=boundary_folder_name,
                        parent=field_folder,
                        owner=owner,
                        is_public=True,
                        is_third_party=True,
                        third_party_source='johndeere',
                        third_party_id=f"{field_id}_{boundary_id}_shp_folder",
                    )
                    results['field_folders_created'] += 1
                    logger.info(f"Created shapefile folder: {boundary_folder_name}")
                    
                    # Save each shapefile component as individual files
                    for filename, content in shp_components.items():
                        # Determine file type and mime type based on extension
                        ext = filename.split('.')[-1].lower()
                        mime_types = {
                            'shp': 'application/x-esri-shapefile',
                            'shx': 'application/x-esri-shapefile',
                            'dbf': 'application/x-dbf',
                            'prj': 'text/plain',
                            'cpg': 'text/plain',
                        }
                        mime_type = mime_types.get(ext, 'application/octet-stream')
                        
                        shp_component_file = File(
                            name=filename,
                            folder=boundary_folder,
                            owner=owner,
                            file_type='archive' if ext in ['shp', 'shx', 'dbf'] else 'text',
                            mime_type=mime_type,
                            is_public=True,
                            is_third_party=True,
                            third_party_source='johndeere',
                            third_party_id=f"{field_id}_{boundary_id}_{ext}",
                        )
                        shp_component_file.file.save(filename, ContentFile(content))
                        shp_component_file.file_size = len(content)
                        shp_component_file.save()
                        results['field_files_created'] += 1
                        
                        # Trigger GIS processing for .shp files
                        if filename.endswith('.shp'):
                            process_gis_file_task.delay(str(shp_component_file.id))
                            logger.info(f"Triggered GIS processing for: {filename}")
                    
                    logger.info(f"Created {len(shp_components)} shapefile components in folder: {boundary_folder_name}")
            
            # Step 3: Get field operations for this field
            try:
                operations = client.get_field_operations(JD_ORG_ID, field_id)
                results['operations_found'] += len(operations)
                logger.info(f"Found {len(operations)} operations for field {field_id}")
            except Exception as e:
                logger.warning(f"Failed to fetch operations for field {field_id}: {e}")
                operations = []
            
            # Create or get the "field_operations" folder under the field folder
            field_operations_folder = Folder.objects.filter(
                name="field_operations",
                parent=field_folder,
                owner=owner
            ).first()
            
            if not field_operations_folder and operations:
                field_operations_folder = Folder.objects.create(
                    name="field_operations",
                    parent=field_folder,
                    owner=owner,
                    is_public=True,
                    is_third_party=True,
                    third_party_source='johndeere',
                    third_party_id=f"{field_id}_field_operations",
                )
                results['field_folders_created'] += 1
                logger.info(f"Created field_operations folder for field: {field_name}")
            
            # Process each field operation
            for operation in operations:
                operation_id = operation.get('id')
                operation_type = operation.get('operationType', 'unknown')
                
                if not operation_id:
                    continue
                
                # Check if folder exists for this operation (now under field_operations folder)
                operation_folder = Folder.objects.filter(
                    name=operation_id,
                    parent=field_operations_folder,
                    owner=owner
                ).first()
                
                operation_is_new = False
                if not operation_folder:
                    # Create new folder for this operation under field_operations
                    operation_folder = Folder.objects.create(
                        name=operation_id,
                        parent=field_operations_folder,
                        owner=owner,
                        is_public=True,
                        is_third_party=True,
                        third_party_source='johndeere',
                        third_party_id=operation_id,
                    )
                    results['operation_folders_created'] += 1
                    operation_is_new = True
                    logger.info(f"Created folder for operation: {operation_id}")
                
                # Only create files if operation folder is new
                if operation_is_new:
                    # Save operation metadata as JSON
                    op_metadata_filename = f"{operation_id}_metadata.json"
                    
                    if not File.objects.filter(name=op_metadata_filename, folder=operation_folder, owner=owner).exists():
                        # Get detailed operation data
                        try:
                            detailed_operation = client.get_field_operation(operation_id)
                            if detailed_operation:
                                operation = detailed_operation
                        except Exception as e:
                            logger.warning(f"Failed to get detailed operation {operation_id}: {e}")
                        
                        operation_metadata = {
                            'id': operation_id,
                            'operationType': operation_type,
                            'title': operation.get('title'),
                            'startDate': operation.get('startDate'),
                            'endDate': operation.get('endDate'),
                            'archived': operation.get('archived'),
                            'totalArea': operation.get('totalArea'),
                            'links': operation.get('links', []),
                            'synced_at': str(date.today()),
                        }
                        
                        json_content = json.dumps(operation_metadata, indent=2, default=str)
                        op_metadata_file = File(
                            name=op_metadata_filename,
                            folder=operation_folder,
                            owner=owner,
                            file_type='text',
                            mime_type='application/json',
                            is_public=True,
                            is_third_party=True,
                            third_party_source='johndeere',
                            third_party_id=f"{operation_id}_metadata",
                        )
                        op_metadata_file.file.save(op_metadata_filename, ContentFile(json_content.encode('utf-8')))
                        op_metadata_file.file_size = len(json_content)
                        op_metadata_file.save()
                        results['operation_files_created'] += 1
                        logger.info(f"Created operation metadata file: {op_metadata_filename}")
                    
                    # Try to get operation boundary/coverage
                    try:
                        op_boundary = client.get_operation_boundary(operation_id)
                        if op_boundary:
                            op_shp_folder_name = f"{operation_id}_boundary"
                            
                            # Check if shapefile folder already exists
                            op_boundary_folder = Folder.objects.filter(
                                name=op_shp_folder_name,
                                parent=operation_folder,
                                owner=owner
                            ).first()
                            
                            if not op_boundary_folder:
                                # Convert to GeoJSON
                                op_geojson = client.boundary_to_geojson(op_boundary)
                                if op_geojson:
                                    # Try to convert to shapefile
                                    op_shp_components = client.geojson_to_shapefile_components(op_geojson, op_shp_folder_name)
                                    
                                    if op_shp_components:
                                        # Create a folder for the shapefile components
                                        op_boundary_folder = Folder.objects.create(
                                            name=op_shp_folder_name,
                                            parent=operation_folder,
                                            owner=owner,
                                            is_public=True,
                                            is_third_party=True,
                                            third_party_source='johndeere',
                                            third_party_id=f"{operation_id}_boundary_shp_folder",
                                        )
                                        results['operation_folders_created'] += 1
                                        logger.info(f"Created operation shapefile folder: {op_shp_folder_name}")
                                        
                                        # Save each shapefile component as individual files
                                        for filename, content in op_shp_components.items():
                                            ext = filename.split('.')[-1].lower()
                                            mime_types = {
                                                'shp': 'application/x-esri-shapefile',
                                                'shx': 'application/x-esri-shapefile',
                                                'dbf': 'application/x-dbf',
                                                'prj': 'text/plain',
                                                'cpg': 'text/plain',
                                            }
                                            mime_type = mime_types.get(ext, 'application/octet-stream')
                                            
                                            op_shp_component_file = File(
                                                name=filename,
                                                folder=op_boundary_folder,
                                                owner=owner,
                                                file_type='archive' if ext in ['shp', 'shx', 'dbf'] else 'text',
                                                mime_type=mime_type,
                                                is_public=True,
                                                is_third_party=True,
                                                third_party_source='johndeere',
                                                third_party_id=f"{operation_id}_boundary_{ext}",
                                            )
                                            op_shp_component_file.file.save(filename, ContentFile(content))
                                            op_shp_component_file.file_size = len(content)
                                            op_shp_component_file.save()
                                            results['operation_files_created'] += 1
                                            
                                            # Trigger GIS processing for .shp files
                                            if filename.endswith('.shp'):
                                                process_gis_file_task.delay(str(op_shp_component_file.id))
                                                logger.info(f"Triggered GIS processing for: {filename}")
                                        
                                        logger.info(f"Created {len(op_shp_components)} shapefile components in folder: {op_shp_folder_name}")
                                    else:
                                        # Save as GeoJSON (as a single file in the operation folder)
                                        op_geojson_filename = f"{op_shp_folder_name}.geojson"
                                        if not File.objects.filter(name=op_geojson_filename, folder=operation_folder, owner=owner).exists():
                                            geojson_content = json.dumps(op_geojson, indent=2)
                                            op_geojson_file = File(
                                                name=op_geojson_filename,
                                                folder=operation_folder,
                                                owner=owner,
                                                file_type='archive',
                                                mime_type='application/geo+json',
                                                is_public=True,
                                                is_third_party=True,
                                                third_party_source='johndeere',
                                                third_party_id=f"{operation_id}_boundary_geojson",
                                            )
                                            op_geojson_file.file.save(op_geojson_filename, ContentFile(geojson_content.encode('utf-8')))
                                            op_geojson_file.file_size = len(geojson_content)
                                            op_geojson_file.save()
                                            results['operation_files_created'] += 1
                                            logger.info(f"Created operation GeoJSON: {op_geojson_filename}")
                    except Exception as e:
                        logger.warning(f"Failed to get boundary for operation {operation_id}: {e}")
        
        logger.info(f"John Deere sync completed: {results}")
        return results
        
    except Exception as e:
        logger.exception("Error in sync_johndeere_task")
        results['success'] = False
        results['errors'].append(str(e))
        return results


@shared_task
def sync_johndeere_scheduled():
    """
    Wrapper task for scheduled John Deere sync.
    This is called by Celery Beat on a daily schedule.
    """
    logger.info("Running scheduled John Deere sync...")
    return sync_johndeere_task.delay()


@shared_task(bind=True)
def run_yield_summary_tool_task(
    self,
    treatment_file_id,
    yield_file_id,
    total_n_values,
    output_dir_id=None,
    buffer_distance=-30.0,
    corn_price=4.35,
    n_price=0.50
):
    """
    Run the Yield Summary Tool on treatment and yield shapefiles.
    
    This task:
    1. Reads treatment sector and yield shapefiles
    2. Creates buffered treatment polygons
    3. Performs spatial join and statistical analysis
    4. Generates summary shapefiles and Excel reports
    5. Creates File records for the output files in Django
    
    Args:
        treatment_file_id: ID of the treatment sector shapefile File object
        yield_file_id: ID of the yield shapefile File object
        total_n_values: Comma-separated Total N values for active sectors
        output_dir_id: Optional ID of the Folder object for output
        buffer_distance: Buffer distance in meters (default -30)
        corn_price: Corn price per bushel ($/bu, default 4.35)
        n_price: Nitrogen price per lb N ($/lb N, default 0.50)
    """
    from django.conf import settings
    import os
    
    try:
        logger.info(f"Starting Yield Summary Tool task for treatment file ID: {treatment_file_id}, yield file ID: {yield_file_id}")
        
        # Get the treatment file object
        try:
            treatment_file = File.objects.get(id=treatment_file_id)
        except File.DoesNotExist:
            logger.error(f"Treatment file with ID {treatment_file_id} not found")
            return {"success": False, "error": f"Treatment file with ID {treatment_file_id} not found"}
        
        # Get the yield file object
        try:
            yield_file = File.objects.get(id=yield_file_id)
        except File.DoesNotExist:
            logger.error(f"Yield file with ID {yield_file_id} not found")
            return {"success": False, "error": f"Yield file with ID {yield_file_id} not found"}
        
        # Validate file types
        treatment_ext = os.path.splitext(treatment_file.name)[1].lower()
        yield_ext = os.path.splitext(yield_file.name)[1].lower()
        
        if treatment_ext != '.shp':
            error_msg = f"Treatment file must be a .shp file, got: {treatment_ext}"
            logger.error(error_msg)
            return {"success": False, "error": error_msg}
        
        if yield_ext != '.shp':
            error_msg = f"Yield file must be a .shp file, got: {yield_ext}"
            logger.error(error_msg)
            return {"success": False, "error": error_msg}
        
        # Get the input file paths
        treatment_path = treatment_file.file.path
        yield_path = yield_file.file.path
        
        # Determine output directory and folder
        from .models import Folder
        output_folder_obj = None
        
        if output_dir_id:
            try:
                output_folder_obj = Folder.objects.get(id=output_dir_id)
            except Folder.DoesNotExist:
                logger.error(f"Output folder with ID {output_dir_id} not found")
                return {"success": False, "error": f"Output folder with ID {output_dir_id} not found"}
        else:
            # Auto-create 'yield_summary_output' folder under the treatment file's parent folder
            parent_folder = treatment_file.folder
            
            existing_folder = Folder.objects.filter(
                name="yield_summary_output",
                parent=parent_folder,
                owner=treatment_file.owner
            ).first()
            
            if existing_folder:
                output_folder_obj = existing_folder
                logger.info(f"Using existing 'yield_summary_output' folder: {output_folder_obj.id}")
            else:
                output_folder_obj = Folder.objects.create(
                    name="yield_summary_output",
                    parent=parent_folder,
                    owner=treatment_file.owner,
                    is_public=treatment_file.is_public
                )
                logger.info(f"Created new 'yield_summary_output' folder: {output_folder_obj.id}")
        
        # Build the full output directory path under MEDIA_ROOT/uploads
        relative_output_dir = output_folder_obj.get_full_path()
        output_dir = os.path.join(settings.MEDIA_ROOT, 'uploads', relative_output_dir)
        
        os.makedirs(output_dir, exist_ok=True)
        
        logger.info(f"Processing treatment file: {treatment_path}")
        logger.info(f"Processing yield file: {yield_path}")
        logger.info(f"Output directory: {output_dir}")
        
        # Import and run the yield summary tool
        from .yield_summary_tool_single_V4 import process_yield_summary
        
        success, message, output_files = process_yield_summary(
            treatment_path=treatment_path,
            yield_path=yield_path,
            output_dir=output_dir,
            total_n_values=total_n_values,
            buffer_distance=float(buffer_distance),
            corn_price=float(corn_price),
            n_price=float(n_price)
        )
        
        if not success:
            logger.error(f"Yield Summary Tool failed: {message}")
            treatment_file.processing_log = (treatment_file.processing_log or "") + f"\n✗ Yield Summary Tool failed: {message}"
            treatment_file.save(update_fields=['processing_log'])
            return {"success": False, "error": message}
        
        logger.info(f"Yield Summary Tool completed: {message}")
        
        # Create File records for the output files
        created_files = []
        
        def create_file_record(file_path):
            if not os.path.exists(file_path):
                return None
            try:
                media_root = settings.MEDIA_ROOT
                if file_path.startswith(str(media_root)):
                    relative_path = os.path.relpath(file_path, media_root)
                else:
                    relative_path = file_path
                
                file_name = os.path.basename(file_path)
                file_size = os.path.getsize(file_path)
                
                existing_file = File.objects.filter(
                    name=file_name,
                    folder=output_folder_obj,
                    owner=treatment_file.owner
                ).first()
                
                if existing_file:
                    existing_file.file_size = file_size
                    existing_file.save(update_fields=['file_size', 'updated_at'])
                    logger.info(f"Updated existing file: {file_name}")
                    return {
                        'name': file_name,
                        'id': str(existing_file.id),
                        'updated': True
                    }
                else:
                    new_file = File(
                        name=file_name,
                        folder=output_folder_obj,
                        owner=treatment_file.owner,
                        file_size=file_size,
                        is_public=treatment_file.is_public,
                    )
                    new_file.file.name = relative_path
                    new_file.save()
                    
                    logger.info(f"Created new file record: {file_name}")
                    return {
                        'name': file_name,
                        'id': str(new_file.id),
                        'updated': False
                    }
                    
            except Exception as e:
                logger.error(f"Error creating file record for {file_path}: {e}")
                return None
        
        # Process shapefile components
        for component_key in ['buffer_components', 'summary_shp_components']:
            if component_key in output_files:
                for file_path in output_files[component_key]:
                    result = create_file_record(file_path)
                    if result:
                        created_files.append(result)
        
        # Process Excel files
        for excel_key in ['summary_xlsx', 'stat_xlsx']:
            if excel_key in output_files:
                result = create_file_record(output_files[excel_key])
                if result:
                    created_files.append(result)
        
        # Update source file processing log
        treatment_file.processing_log = (treatment_file.processing_log or "") + f"\n✓ Yield Summary Tool completed: {message}"
        treatment_file.save(update_fields=['processing_log'])
        
        # Build output_files dict with just basenames for the result
        output_files_result = {}
        for k, v in output_files.items():
            if isinstance(v, str):
                output_files_result[k] = os.path.basename(v)
            elif isinstance(v, list):
                output_files_result[k] = [os.path.basename(f) for f in v]
        
        result = {
            "success": True,
            "message": message,
            "created_files": created_files,
            "output_files": output_files_result
        }
        
        logger.info(f"Yield Summary Tool task completed successfully: {result}")
        return result
        
    except Exception as e:
        logger.exception(f"Error in Yield Summary Tool task")
        return {"success": False, "error": str(e)}
