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
# NOTE: Embedding-related signals have been removed
# ==============================================================================
# 
# ChromaDB and vector search functionality has been completely removed from the system.
# Search now uses PostgreSQL text matching instead of semantic embeddings.
# 
# Files, folders, and maps are created without any embedding generation.
# Search functionality is handled by postgres_search.py using database queries.
