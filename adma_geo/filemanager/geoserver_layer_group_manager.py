#!/usr/bin/env python3

"""
GeoServer Layer Group Manager for composite maps.

This module manages GeoServer Layer Groups which combine multiple
raster and vector layers into composite maps.
"""

import requests
import json
import logging
from django.conf import settings
from pathlib import Path

logger = logging.getLogger(__name__)


class LayerGroupManager:
    """
    Manages GeoServer Layer Groups for composite maps.
    
    Layer Groups allow combining multiple layers (raster + vector) 
    into a single composite layer that can be accessed via WMS.
    """
    
    def __init__(self):
        self.geoserver_url = settings.GEOSERVER_URL
        self.workspace = getattr(settings, 'GEOSERVER_WORKSPACE', 'adma_geo')
        self.auth = (settings.GEOSERVER_ADMIN_USER, settings.GEOSERVER_ADMIN_PASSWORD)
        self.headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }

    def create_layer_group(self, map_obj):
        """
        Create a new Layer Group in GeoServer for the given map.
        
        Args:
            map_obj: Map model instance
            
        Returns:
            tuple: (success: bool, message: str)
        """
        try:
            layer_group_name = map_obj.geoserver_layer_group_name
            
            logger.info(f"Creating Layer Group: {layer_group_name}")
            
            # Get all layers in the map, ordered by layer_order
            map_layers = map_obj.map_layers.filter(
                is_visible=True,
                file__geoserver_layer_name__isnull=False
            ).order_by('layer_order')
            
            if not map_layers.exists():
                return False, "No visible layers found in map"
            
            # Build layer group definition
            layer_group_data = self._build_layer_group_data(map_obj, map_layers)
            
            # Create the layer group
            url = f"{self.geoserver_url}/rest/workspaces/{self.workspace}/layergroups"
            
            response = requests.post(
                url,
                json=layer_group_data,
                auth=self.auth,
                headers=self.headers
            )
            
            if response.status_code in [200, 201]:
                logger.info(f"Layer Group created successfully: {layer_group_name}")
                return True, f"Layer Group created: {layer_group_name}"
            else:
                error_msg = f"Failed to create Layer Group: {response.status_code}"
                logger.error(f"{error_msg} - {response.text}")
                return False, error_msg
                
        except Exception as e:
            error_msg = f"Error creating Layer Group: {str(e)}"
            logger.error(error_msg)
            return False, error_msg

    def update_layer_group(self, map_obj):
        """
        Update an existing Layer Group with current map layers.
        
        Args:
            map_obj: Map model instance
            
        Returns:
            tuple: (success: bool, message: str)
        """
        try:
            layer_group_name = map_obj.geoserver_layer_group_name
            
            logger.info(f"Updating Layer Group: {layer_group_name}")
            
            # Get current layers
            map_layers = map_obj.map_layers.filter(
                is_visible=True,
                file__geoserver_layer_name__isnull=False
            ).order_by('layer_order')
            
            if not map_layers.exists():
                # If no layers, delete the layer group
                return self.delete_layer_group(map_obj)
            
            # Build updated layer group definition
            layer_group_data = self._build_layer_group_data(map_obj, map_layers)
            
            # Update the layer group
            url = f"{self.geoserver_url}/rest/workspaces/{self.workspace}/layergroups/{layer_group_name}"
            
            response = requests.put(
                url,
                json=layer_group_data,
                auth=self.auth,
                headers=self.headers
            )
            
            if response.status_code == 200:
                logger.info(f"Layer Group updated successfully: {layer_group_name}")
                return True, f"Layer Group updated: {layer_group_name}"
            else:
                error_msg = f"Failed to update Layer Group: {response.status_code}"
                logger.error(f"{error_msg} - {response.text}")
                return False, error_msg
                
        except Exception as e:
            error_msg = f"Error updating Layer Group: {str(e)}"
            logger.error(error_msg)
            return False, error_msg

    def delete_layer_group(self, map_obj):
        """
        Delete a Layer Group from GeoServer.
        
        Args:
            map_obj: Map model instance
            
        Returns:
            tuple: (success: bool, message: str)
        """
        try:
            layer_group_name = map_obj.geoserver_layer_group_name
            
            logger.info(f"Deleting Layer Group: {layer_group_name}")
            
            url = f"{self.geoserver_url}/rest/workspaces/{self.workspace}/layergroups/{layer_group_name}"
            
            response = requests.delete(url, auth=self.auth)
            
            if response.status_code in [200, 404]:  # 404 = already deleted
                logger.info(f"Layer Group deleted successfully: {layer_group_name}")
                return True, f"Layer Group deleted: {layer_group_name}"
            else:
                error_msg = f"Failed to delete Layer Group: {response.status_code}"
                logger.error(f"{error_msg} - {response.text}")
                return False, error_msg
                
        except Exception as e:
            error_msg = f"Error deleting Layer Group: {str(e)}"
            logger.error(error_msg)
            return False, error_msg

    def add_layer_to_group(self, map_obj, map_layer):
        """
        Add a new layer to an existing Layer Group.
        
        Args:
            map_obj: Map model instance
            map_layer: MapLayer model instance
            
        Returns:
            tuple: (success: bool, message: str)
        """
        return self.update_layer_group(map_obj)

    def remove_layer_from_group(self, map_obj, map_layer):
        """
        Remove a layer from an existing Layer Group.
        
        Args:
            map_obj: Map model instance
            map_layer: MapLayer model instance
            
        Returns:
            tuple: (success: bool, message: str)
        """
        return self.update_layer_group(map_obj)

    def _build_layer_group_data(self, map_obj, map_layers):
        """
        Build the Layer Group JSON data for GeoServer API.
        
        Args:
            map_obj: Map model instance
            map_layers: QuerySet of MapLayer instances
            
        Returns:
            dict: Layer Group definition for GeoServer
        """
        # Build layers and styles arrays
        layers = []
        styles = []
        
        for map_layer in map_layers:
            # Get full layer name (workspace:layer)
            workspace = map_layer.geoserver_workspace or self.workspace
            layer_name = map_layer.geoserver_layer_name
            full_layer_name = f"{workspace}:{layer_name}"
            
            layers.append(full_layer_name)
            
            # Add style (empty string for default style)
            style_name = map_layer.style_name or ""
            styles.append(style_name)
        
        # Calculate bounds (use world extent for now)
        bounds = self._calculate_bounds(map_obj, map_layers)
        
        # Build layer group data with correct structure for GeoServer API
        layer_group_data = {
            "layerGroup": {
                "name": map_obj.geoserver_layer_group_name,
                "title": map_obj.name,
                "abstractTxt": map_obj.description or f"Composite map: {map_obj.name}",
                "workspace": {
                    "name": self.workspace
                },
                "publishables": {
                    "published": [{"@type": "layer", "name": layer} for layer in layers]
                },
                "styles": {
                    "style": [{"name": style} for style in styles]
                },
                "bounds": bounds
            }
        }
        
        return layer_group_data

    def _calculate_bounds(self, map_obj, map_layers):
        """
        Calculate bounding box for the Layer Group.
        
        For now, returns a default world extent.
        In the future, this could calculate the actual extent
        from the individual layer extents.
        """
        return {
            "minx": -20037508.34,
            "maxx": 20037508.34,
            "miny": -20037508.34,
            "maxy": 20037508.34,
            "crs": "EPSG:3857"
        }

    def layer_group_exists(self, layer_group_name):
        """
        Check if a Layer Group exists in GeoServer.
        
        Args:
            layer_group_name: Name of the layer group
            
        Returns:
            bool: True if layer group exists
        """
        try:
            url = f"{self.geoserver_url}/rest/workspaces/{self.workspace}/layergroups/{layer_group_name}"
            response = requests.get(url, auth=self.auth)
            return response.status_code == 200
        except Exception:
            return False

    def get_layer_group_wms_url(self, map_obj, **params):
        """
        Generate WMS URL for the Layer Group.
        
        Args:
            map_obj: Map model instance
            **params: Additional WMS parameters
            
        Returns:
            str: WMS URL for the layer group
        """
        base_params = {
            'SERVICE': 'WMS',
            'VERSION': '1.1.1',
            'REQUEST': 'GetMap',
            'LAYERS': f"{self.workspace}:{map_obj.geoserver_layer_group_name}",
            'STYLES': '',
            'FORMAT': 'image/png',
            'TRANSPARENT': 'true',
            'SRS': 'EPSG:3857',
            'WIDTH': '512',
            'HEIGHT': '512'
        }
        
        # Update with provided parameters
        base_params.update(params)
        
        # Build query string
        query_string = '&'.join([f"{k}={v}" for k, v in base_params.items()])
        
        return f"{self.geoserver_url}/wms?{query_string}"
