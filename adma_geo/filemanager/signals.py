#!/usr/bin/env python3

"""
Signal handlers for filemanager models.

This module provides signal handlers to automatically update GeoServer
Layer Groups when MapLayers are added, removed, or modified.
"""

from django.db.models.signals import post_save, post_delete, pre_delete
from django.dispatch import receiver
import logging

from .models import MapLayer, File, Folder, Map

logger = logging.getLogger(__name__)


@receiver(post_save, sender=MapLayer)
def update_layer_group_on_layer_add(sender, instance, created, **kwargs):
    """
    Update GeoServer Layer Group when a MapLayer is added or modified.
    """
    if created:
        # New layer added to map
        try:
            from .geoserver_layer_group_manager import LayerGroupManager
            layer_group_manager = LayerGroupManager()
            
            success, message = layer_group_manager.update_layer_group(instance.map)
            
            if success:
                logger.info(f"Layer Group updated after adding layer {instance.file.name} to map {instance.map.name}")
            else:
                logger.error(f"Failed to update Layer Group after adding layer: {message}")
                
        except Exception as e:
            logger.error(f"Error updating Layer Group after adding layer: {e}")


@receiver(post_delete, sender=MapLayer)
def update_layer_group_on_layer_remove(sender, instance, **kwargs):
    """
    Update GeoServer Layer Group when a MapLayer is removed.
    """
    try:
        from .geoserver_layer_group_manager import LayerGroupManager
        layer_group_manager = LayerGroupManager()
        
        success, message = layer_group_manager.update_layer_group(instance.map)
        
        if success:
            logger.info(f"Layer Group updated after removing layer {instance.file.name} from map {instance.map.name}")
        else:
            logger.error(f"Failed to update Layer Group after removing layer: {message}")
            
    except Exception as e:
        logger.error(f"Error updating Layer Group after removing layer: {e}")


@receiver(pre_delete, sender=File)
def remove_file_from_maps_on_delete(sender, instance, **kwargs):
    """
    Remove a file from all maps when the file is deleted.
    This ensures map integrity when spatial files are deleted.
    """
    if instance.is_spatial and instance.map_memberships.exists():
        try:
            from .geoserver_layer_group_manager import LayerGroupManager
            layer_group_manager = LayerGroupManager()
            
            # Get all maps containing this file
            affected_maps = set()
            for membership in instance.map_memberships.all():
                affected_maps.add(membership.map)
            
            # Remove the file from all maps (MapLayer deletion happens automatically due to CASCADE)
            # Update each affected map's Layer Group
            for map_obj in affected_maps:
                try:
                    success, message = layer_group_manager.update_layer_group(map_obj)
                    
                    if success:
                        logger.info(f"Updated Layer Group for map {map_obj.name} after file {instance.name} deletion")
                    else:
                        logger.error(f"Failed to update Layer Group for map {map_obj.name}: {message}")
                        
                except Exception as e:
                    logger.error(f"Error updating Layer Group for map {map_obj.name}: {e}")
                    
        except Exception as e:
            logger.error(f"Error handling file deletion from maps: {e}")


# Optional: Signal to auto-create Layer Groups when Maps are created
@receiver(post_save, sender='filemanager.Map')
def create_layer_group_on_map_create(sender, instance, created, **kwargs):
    """
    Create GeoServer Layer Group when a new Map is created.
    Note: This only creates an empty Layer Group. Layers are added separately.
    """
    if created and instance.map_layers.exists():
        try:
            from .geoserver_layer_group_manager import LayerGroupManager
            layer_group_manager = LayerGroupManager()
            
            success, message = layer_group_manager.create_layer_group(instance)
            
            if success:
                logger.info(f"Layer Group created for new map {instance.name}")
            else:
                logger.warning(f"Failed to create Layer Group for new map {instance.name}: {message}")
                
        except Exception as e:
            logger.error(f"Error creating Layer Group for new map: {e}")


# ==============================================================================
# AUTOMATIC EMBEDDING GENERATION SIGNALS
# ==============================================================================

@receiver(post_save, sender=File)
def generate_file_embedding_on_create(sender, instance, created, **kwargs):
    """
    Automatically generate embedding for newly created files.
    This ensures all files have embeddings for search functionality.
    """
    if created:
        try:
            from .tasks import generate_file_embedding_task
            
            # Submit asynchronous task to generate embedding
            generate_file_embedding_task.delay(str(instance.id))
            
            logger.info(f"Scheduled embedding generation for file: {instance.name} (ID: {instance.id})")
            
        except ImportError:
            logger.warning(f"Embedding task not available - skipping embedding generation for file: {instance.name}")
        except Exception as e:
            logger.error(f"Failed to schedule embedding generation for file {instance.name}: {e}")


@receiver(post_save, sender=Folder)
def generate_folder_embedding_on_create(sender, instance, created, **kwargs):
    """
    Automatically generate embedding for newly created folders.
    This ensures all folders have embeddings for search functionality.
    """
    if created:
        try:
            from .tasks import generate_folder_embedding_task
            
            # Submit asynchronous task to generate embedding
            generate_folder_embedding_task.delay(str(instance.id))
            
            logger.info(f"Scheduled embedding generation for folder: {instance.name} (ID: {instance.id})")
            
        except ImportError:
            logger.warning(f"Embedding task not available - skipping embedding generation for folder: {instance.name}")
        except Exception as e:
            logger.error(f"Failed to schedule embedding generation for folder {instance.name}: {e}")


@receiver(post_save, sender=Map)
def generate_map_embedding_on_create(sender, instance, created, **kwargs):
    """
    Map embedding generation DISABLED due to ChromaDB corruption issue.
    
    Maps are created without embeddings and must be manually indexed
    using the management command: python manage.py generate_map_embeddings
    
    This prevents ChromaDB corruption that breaks search functionality.
    """
    if created:
        logger.warning(f"Map created without embedding: {instance.name} (ID: {instance.id})")
        logger.warning("Use 'python manage.py generate_map_embeddings' to add map to search index")
        
        # COMPLETELY DISABLED until ChromaDB corruption issue is resolved
        # Automatic map embedding causes "Error finding id" in ChromaDB
        pass


@receiver(pre_delete, sender=File)
def remove_file_embedding_on_delete(sender, instance, **kwargs):
    """
    Remove file embedding from ChromaDB when file is deleted.
    This keeps the embedding database clean.
    """
    if instance.chroma_id:
        try:
            from .embedding_service import embedding_service
            
            success = embedding_service.remove_file_embedding(instance)
            
            if success:
                logger.info(f"Removed embedding for deleted file: {instance.name} (ChromaDB ID: {instance.chroma_id})")
            else:
                logger.warning(f"Failed to remove embedding for deleted file: {instance.name}")
                
        except ImportError:
            logger.warning(f"Embedding service not available - skipping embedding removal for file: {instance.name}")
        except Exception as e:
            logger.error(f"Error removing embedding for deleted file {instance.name}: {e}")


@receiver(pre_delete, sender=Folder)
def remove_folder_embedding_on_delete(sender, instance, **kwargs):
    """
    Remove folder embedding from ChromaDB when folder is deleted.
    This keeps the embedding database clean.
    """
    if instance.chroma_id:
        try:
            from .embedding_service import embedding_service
            
            success = embedding_service.remove_folder_embedding(instance)
            
            if success:
                logger.info(f"Removed embedding for deleted folder: {instance.name} (ChromaDB ID: {instance.chroma_id})")
            else:
                logger.warning(f"Failed to remove embedding for deleted folder: {instance.name}")
                
        except ImportError:
            logger.warning(f"Embedding service not available - skipping embedding removal for folder: {instance.name}")
        except Exception as e:
            logger.error(f"Error removing embedding for deleted folder {instance.name}: {e}")


@receiver(pre_delete, sender=Map)
def remove_map_embedding_on_delete(sender, instance, **kwargs):
    """
    Remove map embedding from ChromaDB when map is deleted.
    This keeps the embedding database clean.
    """
    if instance.chroma_id:
        try:
            from .embedding_service import embedding_service
            
            success = embedding_service.remove_map_embedding(instance)
            
            if success:
                logger.info(f"Removed embedding for deleted map: {instance.name} (ChromaDB ID: {instance.chroma_id})")
            else:
                logger.warning(f"Failed to remove embedding for deleted map: {instance.name}")
                
        except ImportError:
            logger.warning(f"Embedding service not available - skipping embedding removal for map: {instance.name}")
        except Exception as e:
            logger.error(f"Error removing embedding for deleted map {instance.name}: {e}")
