#!/usr/bin/env python3

"""
PostgreSQL-based search functionality.

This module provides text-based search capabilities using PostgreSQL's
full-text search and ILIKE operations, replacing the ChromaDB vector search.
"""

from django.db.models import Q
from django.contrib.postgres.search import SearchVector, SearchQuery, SearchRank
from .models import File, Folder, Map
import logging

logger = logging.getLogger(__name__)


class PostgresSearchService:
    """
    PostgreSQL-based search service for files, folders, and maps.
    
    Uses Django's built-in search capabilities with PostgreSQL full-text search
    and ILIKE pattern matching for comprehensive text search.
    """
    
    def search_all(self, query_text: str, user_id: str, filters: dict = None, limit: int = 100):
        """
        Search across files, folders, and maps using PostgreSQL text search.
        
        Args:
            query_text: Search query string
            user_id: ID of the user performing the search
            filters: Optional filters (type, file_type, is_spatial, is_public, owner)
            limit: Maximum number of results to return
            
        Returns:
            List of search results with metadata
        """
        if not query_text.strip():
            # If no query, use filter-only search
            return self._filter_only_search(user_id, filters, limit)
        
        results = []
        
        # Search files
        file_results = self._search_files(query_text, user_id, filters, limit)
        results.extend(file_results)
        
        # Search folders
        folder_results = self._search_folders(query_text, user_id, filters, limit)
        results.extend(folder_results)
        
        # Search maps
        map_results = self._search_maps(query_text, user_id, filters, limit)
        results.extend(map_results)
        
        # Sort by relevance (exact matches first, then partial matches)
        results = self._sort_by_relevance(results, query_text)
        
        return results[:limit]
    
    def _search_files(self, query_text: str, user_id: str, filters: dict = None, limit: int = 100):
        """Search files using PostgreSQL text search"""
        # Base queryset - user's files + public files
        queryset = File.objects.filter(
            Q(owner_id=user_id) | Q(is_public=True)
        ).exclude(deletion_in_progress=True)
        
        # Apply filters
        queryset = self._apply_filters(queryset, filters)
        
        # Apply text search using multiple fields
        search_fields = [
            'name', 'file_type', 'folder__name', 'owner__username'
        ]
        
        # Build search query using ILIKE for partial matching
        search_q = Q()
        for field in search_fields:
            search_q |= Q(**{f'{field}__icontains': query_text})
        
        # Also try full-text search if supported
        try:
            # PostgreSQL full-text search
            search_vector = SearchVector('name', 'file_type')
            search_query = SearchQuery(query_text)
            
            queryset = queryset.annotate(
                search=search_vector,
                rank=SearchRank(search_vector, search_query)
            ).filter(
                Q(search=search_query) | search_q
            ).order_by('-rank', 'name')
            
        except Exception:
            # Fallback to ILIKE search
            queryset = queryset.filter(search_q).order_by('name')
        
        # Convert to result format
        results = []
        for file_obj in queryset[:limit]:
            results.append({
                'type': 'file',
                'id': str(file_obj.id),
                'name': file_obj.name,
                'object': file_obj,
                'relevance': self._calculate_relevance(file_obj.name, query_text),
                'metadata': {
                    'file_type': file_obj.file_type,
                    'size': file_obj.get_size_display(),
                    'is_spatial': file_obj.is_spatial,
                    'is_public': file_obj.is_public,
                    'owner': file_obj.owner.username,
                    'created_at': file_obj.created_at.isoformat(),
                }
            })
        
        return results
    
    def _search_folders(self, query_text: str, user_id: str, filters: dict = None, limit: int = 100):
        """Search folders using PostgreSQL text search"""
        # Base queryset - user's folders + public folders
        queryset = Folder.objects.filter(
            Q(owner_id=user_id) | Q(is_public=True)
        ).exclude(deletion_in_progress=True)
        
        # Apply filters
        if filters:
            if filters.get('type') == 'file':
                return []  # Skip folders if specifically searching for files
        
        # Apply text search
        search_fields = ['name', 'owner__username']
        search_q = Q()
        for field in search_fields:
            search_q |= Q(**{f'{field}__icontains': query_text})
        
        queryset = queryset.filter(search_q).order_by('name')
        
        # Convert to result format
        results = []
        for folder_obj in queryset[:limit]:
            results.append({
                'type': 'folder',
                'id': str(folder_obj.id),
                'name': folder_obj.name,
                'object': folder_obj,
                'relevance': self._calculate_relevance(folder_obj.name, query_text),
                'metadata': {
                    'is_public': folder_obj.is_public,
                    'owner': folder_obj.owner.username,
                    'created_at': folder_obj.created_at.isoformat(),
                    'file_count': folder_obj.files.count(),
                    'subfolder_count': folder_obj.subfolders.count(),
                }
            })
        
        return results
    
    def _search_maps(self, query_text: str, user_id: str, filters: dict = None, limit: int = 100):
        """Search maps using PostgreSQL text search"""
        # Base queryset - user's maps + public maps
        queryset = Map.objects.filter(
            Q(owner_id=user_id) | Q(is_public=True)
        ).exclude(deletion_in_progress=True)
        
        # Apply filters
        if filters:
            if filters.get('type') == 'file':
                return []  # Skip maps if specifically searching for files
        
        # Apply text search
        search_fields = ['name', 'description', 'owner__username']
        search_q = Q()
        for field in search_fields:
            search_q |= Q(**{f'{field}__icontains': query_text})
        
        queryset = queryset.filter(search_q).order_by('name')
        
        # Convert to result format
        results = []
        for map_obj in queryset[:limit]:
            results.append({
                'type': 'map',
                'id': str(map_obj.id),
                'name': map_obj.name,
                'object': map_obj,
                'relevance': self._calculate_relevance(map_obj.name, query_text),
                'metadata': {
                    'description': map_obj.description or '',
                    'is_public': map_obj.is_public,
                    'owner': map_obj.owner.username,
                    'created_at': map_obj.created_at.isoformat(),
                    'layer_count': map_obj.map_layers.count(),
                }
            })
        
        return results
    
    def _filter_only_search(self, user_id: str, filters: dict = None, limit: int = 100):
        """Perform filter-only search when no text query is provided"""
        results = []
        
        if not filters or filters.get('type') in [None, 'file']:
            # Include files
            file_queryset = File.objects.filter(
                Q(owner_id=user_id) | Q(is_public=True)
            ).exclude(deletion_in_progress=True)
            
            file_queryset = self._apply_filters(file_queryset, filters)
            
            for file_obj in file_queryset[:limit]:
                results.append({
                    'type': 'file',
                    'id': str(file_obj.id),
                    'name': file_obj.name,
                    'object': file_obj,
                    'relevance': 1.0,
                })
        
        if not filters or filters.get('type') in [None, 'folder']:
            # Include folders
            folder_queryset = Folder.objects.filter(
                Q(owner_id=user_id) | Q(is_public=True)
            ).exclude(deletion_in_progress=True)
            
            for folder_obj in folder_queryset[:limit]:
                results.append({
                    'type': 'folder',
                    'id': str(folder_obj.id),
                    'name': folder_obj.name,
                    'object': folder_obj,
                    'relevance': 1.0,
                })
        
        if not filters or filters.get('type') in [None, 'map']:
            # Include maps
            map_queryset = Map.objects.filter(
                Q(owner_id=user_id) | Q(is_public=True)
            ).exclude(deletion_in_progress=True)
            
            for map_obj in map_queryset[:limit]:
                results.append({
                    'type': 'map',
                    'id': str(map_obj.id),
                    'name': map_obj.name,
                    'object': map_obj,
                    'relevance': 1.0,
                })
        
        return results[:limit]
    
    def _apply_filters(self, queryset, filters):
        """Apply filters to queryset"""
        if not filters:
            return queryset
        
        if filters.get('file_type'):
            queryset = queryset.filter(file_type=filters['file_type'])
        
        if filters.get('is_spatial') is not None:
            queryset = queryset.filter(is_spatial=filters['is_spatial'])
        
        if filters.get('is_public') is not None:
            queryset = queryset.filter(is_public=filters['is_public'])
        
        if filters.get('owner'):
            queryset = queryset.filter(owner__username__icontains=filters['owner'])
        
        return queryset
    
    def _calculate_relevance(self, text: str, query: str) -> float:
        """Calculate simple relevance score based on text matching"""
        text_lower = text.lower()
        query_lower = query.lower()
        
        # Exact match
        if text_lower == query_lower:
            return 1.0
        
        # Starts with query
        if text_lower.startswith(query_lower):
            return 0.9
        
        # Contains query
        if query_lower in text_lower:
            return 0.7
        
        # Default relevance
        return 0.5
    
    def _sort_by_relevance(self, results, query_text):
        """Sort results by relevance score"""
        return sorted(results, key=lambda x: x.get('relevance', 0), reverse=True)


# Create a global instance
postgres_search = PostgresSearchService()
