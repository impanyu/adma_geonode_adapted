"""
John Deere Operations Center API Client

This module provides a client for interacting with the John Deere Operations Center API.
It handles OAuth2 authentication with refresh tokens and provides methods for accessing
fields, boundaries, and field operations data.

API Documentation: https://developer.deere.com/dev-docs/
"""

import requests
import json
import logging
from typing import Optional, Dict, List, Any

logger = logging.getLogger(__name__)


class JohnDeereClient:
    """
    Client for John Deere Operations Center API.
    
    Handles OAuth2 authentication using refresh tokens and provides methods
    for accessing organization data including fields, boundaries, and field operations.
    """
    
    # John Deere OAuth2 endpoints
    TOKEN_URL = "https://signin.johndeere.com/oauth2/aus78tnlaysMraFhC1t7/v1/token"
    
    # API base URLs
    API_BASE_URL = "https://sandboxapi.deere.com/platform"  # Sandbox
    # API_BASE_URL = "https://partnerapi.deere.com/platform"  # Production
    
    # Default API version header
    API_VERSION = "application/vnd.deere.axiom.v3+json"
    
    def __init__(self, client_id: str, client_secret: str, refresh_token: str):
        """
        Initialize John Deere client.
        
        Args:
            client_id: Application ID from developer.deere.com
            client_secret: Application secret from developer.deere.com
            refresh_token: OAuth2 refresh token for the user
        """
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.access_token = None
        self._session = requests.Session()
    
    def _refresh_access_token(self) -> str:
        """
        Use refresh token to get a new access token.
        
        Returns:
            New access token string
            
        Raises:
            Exception: If token refresh fails
        """
        logger.info("Refreshing John Deere access token...")
        
        data = {
            'grant_type': 'refresh_token',
            'refresh_token': self.refresh_token,
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'scope': 'offline_access files ag3 work2 org2 eq2',  # Only request scopes that were granted originally
        }
        
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded',
            'Accept': 'application/json',
        }
        
        try:
            response = self._session.post(
                self.TOKEN_URL,
                data=data,
                headers=headers,
                timeout=30
            )
            
            if response.status_code != 200:
                error_msg = f"Token refresh failed: {response.status_code} - {response.text}"
                logger.error(error_msg)
                raise Exception(error_msg)
            
            token_data = response.json()
            self.access_token = token_data.get('access_token')
            
            # Update refresh token if a new one is provided
            new_refresh_token = token_data.get('refresh_token')
            if new_refresh_token:
                self.refresh_token = new_refresh_token
                logger.info("Received new refresh token")
            
            logger.info("Successfully refreshed access token")
            return self.access_token
            
        except requests.RequestException as e:
            logger.error(f"Network error during token refresh: {e}")
            raise
    
    def _get_headers(self) -> Dict[str, str]:
        """Get headers for API requests."""
        if not self.access_token:
            self._refresh_access_token()
        
        return {
            'Authorization': f'Bearer {self.access_token}',
            'Accept': self.API_VERSION,
        }
    
    def _make_request(self, method: str, endpoint: str, **kwargs) -> requests.Response:
        """
        Make an API request with automatic token refresh on 401.
        
        Args:
            method: HTTP method (GET, POST, etc.)
            endpoint: API endpoint (will be appended to base URL)
            **kwargs: Additional arguments to pass to requests
            
        Returns:
            Response object
        """
        url = f"{self.API_BASE_URL}{endpoint}"
        headers = self._get_headers()
        headers.update(kwargs.pop('headers', {}))
        
        response = self._session.request(
            method,
            url,
            headers=headers,
            timeout=kwargs.pop('timeout', 60),
            **kwargs
        )
        
        # If unauthorized, refresh token and retry once
        if response.status_code == 401:
            logger.info("Received 401, refreshing token and retrying...")
            self._refresh_access_token()
            headers = self._get_headers()
            headers.update(kwargs.pop('headers', {}))
            response = self._session.request(
                method,
                url,
                headers=headers,
                timeout=60,
                **kwargs
            )
        
        return response
    
    def get_organizations(self) -> List[Dict[str, Any]]:
        """
        Get list of organizations the user has access to.
        
        Returns:
            List of organization objects
        """
        response = self._make_request('GET', '/organizations')
        
        if response.status_code != 200:
            logger.error(f"Failed to get organizations: {response.status_code} - {response.text}")
            return []
        
        data = response.json()
        return data.get('values', [])
    
    def get_fields(self, org_id: str, embed_boundaries: bool = True) -> List[Dict[str, Any]]:
        """
        Get list of fields for an organization.
        
        Args:
            org_id: Organization ID
            embed_boundaries: If True, include boundary data in response
            
        Returns:
            List of field objects
        """
        endpoint = f"/organizations/{org_id}/fields"
        params = {}
        
        if embed_boundaries:
            params['embed'] = 'boundaries'
        
        all_fields = []
        
        while endpoint:
            response = self._make_request('GET', endpoint, params=params)
            
            if response.status_code != 200:
                logger.error(f"Failed to get fields: {response.status_code} - {response.text}")
                break
            
            data = response.json()
            fields = data.get('values', [])
            all_fields.extend(fields)
            
            # Handle pagination
            next_page = None
            for link in data.get('links', []):
                if link.get('rel') == 'nextPage':
                    next_page = link.get('uri')
                    break
            
            if next_page:
                # Extract just the path from the full URL
                endpoint = next_page.replace(self.API_BASE_URL, '')
                params = {}  # Params are in the URL now
            else:
                endpoint = None
        
        logger.info(f"Retrieved {len(all_fields)} fields for organization {org_id}")
        return all_fields
    
    def get_field(self, org_id: str, field_id: str) -> Optional[Dict[str, Any]]:
        """
        Get a specific field by ID.
        
        Args:
            org_id: Organization ID
            field_id: Field ID
            
        Returns:
            Field object or None if not found
        """
        endpoint = f"/organizations/{org_id}/fields/{field_id}"
        response = self._make_request('GET', endpoint, params={'embed': 'boundaries'})
        
        if response.status_code != 200:
            logger.error(f"Failed to get field {field_id}: {response.status_code}")
            return None
        
        return response.json()
    
    def get_field_boundaries(self, org_id: str, field_id: str) -> List[Dict[str, Any]]:
        """
        Get boundaries for a specific field.
        
        Args:
            org_id: Organization ID
            field_id: Field ID
            
        Returns:
            List of boundary objects
        """
        endpoint = f"/organizations/{org_id}/fields/{field_id}/boundaries"
        response = self._make_request('GET', endpoint)
        
        if response.status_code != 200:
            logger.error(f"Failed to get boundaries for field {field_id}: {response.status_code}")
            return []
        
        data = response.json()
        return data.get('values', [])
    
    def get_field_operations(self, org_id: str, field_id: str) -> List[Dict[str, Any]]:
        """
        Get field operations for a specific field.
        
        Args:
            org_id: Organization ID
            field_id: Field ID
            
        Returns:
            List of field operation objects
        """
        endpoint = f"/organizations/{org_id}/fields/{field_id}/fieldOperations"
        all_operations = []
        
        while endpoint:
            response = self._make_request('GET', endpoint)
            
            if response.status_code != 200:
                logger.error(f"Failed to get field operations: {response.status_code} - {response.text}")
                break
            
            data = response.json()
            operations = data.get('values', [])
            all_operations.extend(operations)
            
            # Handle pagination
            next_page = None
            for link in data.get('links', []):
                if link.get('rel') == 'nextPage':
                    next_page = link.get('uri')
                    break
            
            if next_page:
                endpoint = next_page.replace(self.API_BASE_URL, '')
            else:
                endpoint = None
        
        logger.info(f"Retrieved {len(all_operations)} field operations for field {field_id}")
        return all_operations
    
    def get_field_operation(self, operation_id: str) -> Optional[Dict[str, Any]]:
        """
        Get a specific field operation by ID.
        
        Args:
            operation_id: Field operation ID
            
        Returns:
            Field operation object or None if not found
        """
        endpoint = f"/fieldOperations/{operation_id}"
        response = self._make_request('GET', endpoint)
        
        if response.status_code != 200:
            logger.error(f"Failed to get field operation {operation_id}: {response.status_code}")
            return None
        
        return response.json()
    
    def get_operation_boundary(self, operation_id: str) -> Optional[Dict[str, Any]]:
        """
        Generate a boundary from a field operation.
        
        Args:
            operation_id: Field operation ID
            
        Returns:
            Generated boundary object or None if generation fails
        """
        endpoint = f"/fieldOperations/{operation_id}/boundary"
        response = self._make_request('GET', endpoint)
        
        if response.status_code == 400:
            # Field may already have an active boundary or has been merged
            logger.warning(f"Cannot generate boundary for operation {operation_id}: {response.text}")
            return None
        
        if response.status_code != 200:
            logger.error(f"Failed to generate boundary for operation {operation_id}: {response.status_code}")
            return None
        
        return response.json()
    
    def create_subscription(
        self,
        client_endpoint: str,
        username: str,
        password: str,
        event_type_ids: List[str],
        org_id: str,
    ) -> Dict[str, Any]:
        """
        POST /eventSubscriptions — create a new webhook subscription.

        Returns the parsed JSON body (the new subscription, including ``id``).
        """
        body = {
            'clientEndpoint': {
                'uri': client_endpoint,
                'username': username,
                'password': password,
            },
            'eventTypeIds': list(event_type_ids),
            'scopes': [
                {'objectType': 'organization', 'objectId': str(org_id)},
            ],
        }
        response = self._make_request('POST', '/eventSubscriptions', json=body)
        if response.status_code not in (200, 201):
            raise Exception(
                f"Failed to create subscription: {response.status_code} - {response.text}"
            )
        return response.json()

    def list_subscriptions(self) -> List[Dict[str, Any]]:
        """GET /eventSubscriptions with pagination handling."""
        endpoint = '/eventSubscriptions'
        all_subs: List[Dict[str, Any]] = []
        while endpoint:
            response = self._make_request('GET', endpoint)
            if response.status_code != 200:
                logger.error(
                    "Failed to list subscriptions: %s - %s",
                    response.status_code, response.text,
                )
                break
            data = response.json()
            all_subs.extend(data.get('values', []))
            next_uri = None
            for link in data.get('links', []):
                if link.get('rel') == 'nextPage':
                    next_uri = link.get('uri')
                    break
            if next_uri:
                endpoint = next_uri.replace(self.API_BASE_URL, '')
            else:
                endpoint = None
        return all_subs

    def delete_subscription(self, subscription_id: str) -> bool:
        """DELETE /eventSubscriptions/{id}. Returns True on 204 or 404, else False."""
        response = self._make_request('DELETE', f'/eventSubscriptions/{subscription_id}')
        if response.status_code in (204, 200, 404):
            return True
        logger.error(
            "Failed to delete subscription %s: %s - %s",
            subscription_id, response.status_code, response.text,
        )
        return False

    def get_resource_by_link(self, uri: str) -> Optional[Dict[str, Any]]:
        """
        Follow an absolute URI returned in an event's ``targetResource`` field
        and return the parsed JSON. Returns None on non-200.
        """
        if uri.startswith(self.API_BASE_URL):
            endpoint = uri[len(self.API_BASE_URL):]
        else:
            endpoint = uri
        response = self._make_request('GET', endpoint)
        if response.status_code != 200:
            logger.error(
                "Failed to fetch resource %s: %s - %s",
                uri, response.status_code, response.text,
            )
            return None
        return response.json()

    @staticmethod
    def boundary_to_geojson(boundary: Dict[str, Any]) -> Dict[str, Any]:
        """
        Convert John Deere boundary format to GeoJSON.
        
        Args:
            boundary: John Deere boundary object with multipolygons
            
        Returns:
            GeoJSON Feature object
        """
        multipolygons = boundary.get('multipolygons', [])
        
        if not multipolygons:
            return None
        
        # Convert to GeoJSON coordinates
        # John Deere format: multipolygons -> polygons -> rings -> points (lat, lon)
        # GeoJSON format: coordinates [[[lon, lat], ...], ...]
        
        polygons = []
        for polygon in multipolygons:
            rings = []
            for ring in polygon.get('rings', []):
                points = []
                for point in ring.get('points', []):
                    # GeoJSON uses [lon, lat] order
                    points.append([point.get('lon'), point.get('lat')])
                if points:
                    rings.append(points)
            if rings:
                polygons.append(rings)
        
        if not polygons:
            return None
        
        # Determine geometry type
        if len(polygons) == 1:
            geometry = {
                "type": "Polygon",
                "coordinates": polygons[0]
            }
        else:
            geometry = {
                "type": "MultiPolygon",
                "coordinates": polygons
            }
        
        # Build GeoJSON Feature
        properties = {
            "id": boundary.get('id'),
            "name": boundary.get('name'),
            "active": boundary.get('active'),
            "archived": boundary.get('archived'),
            "irrigated": boundary.get('irrigated'),
            "sourceType": boundary.get('sourceType'),
            "signalType": boundary.get('signalType'),
        }
        
        # Add area if available
        area = boundary.get('area')
        if area:
            properties['area_value'] = area.get('valueAsDouble')
            properties['area_unit'] = area.get('unit')
        
        return {
            "type": "Feature",
            "geometry": geometry,
            "properties": properties
        }
    
    @staticmethod
    def geojson_to_shapefile_components(geojson: Dict[str, Any], name: str = "boundary") -> Dict[str, bytes]:
        """
        Convert GeoJSON to shapefile components using fiona/pyshp.
        
        Args:
            geojson: GeoJSON Feature or FeatureCollection
            name: Base name for the shapefile
            
        Returns:
            Dictionary with shapefile component filenames and their binary content
        """
        import tempfile
        import os
        
        try:
            import geopandas as gpd
            
            # Create GeoDataFrame from GeoJSON
            gdf = gpd.GeoDataFrame.from_features(
                geojson if geojson.get('type') == 'FeatureCollection' else {'type': 'FeatureCollection', 'features': [geojson]},
                crs='EPSG:4326'
            )
            
            if gdf.empty:
                logger.warning("GeoJSON resulted in empty GeoDataFrame")
                return {}
            
            # Convert bool columns to string (shapefiles don't support bool)
            for col in gdf.columns:
                if col != 'geometry' and gdf[col].dtype == 'bool':
                    gdf[col] = gdf[col].astype(str)
            
            # Truncate column names to 10 characters (shapefile limitation)
            gdf.columns = [c if c == 'geometry' else c[:10] for c in gdf.columns]
            
            # Write to temp directory
            with tempfile.TemporaryDirectory() as tmpdir:
                shp_path = os.path.join(tmpdir, f"{name}.shp")
                gdf.to_file(shp_path)
                
                # Collect all shapefile components
                result = {}
                shapefile_extensions = ['.shp', '.shx', '.dbf', '.prj', '.cpg']
                
                for ext in shapefile_extensions:
                    filepath = os.path.join(tmpdir, f"{name}{ext}")
                    if os.path.exists(filepath):
                        with open(filepath, 'rb') as f:
                            result[f"{name}{ext}"] = f.read()
                
                return result
                
        except ImportError as e:
            logger.error(f"geopandas package not available: {e}")
            return {}
        except Exception as e:
            logger.error(f"Error converting GeoJSON to shapefile: {e}")
            return {}
