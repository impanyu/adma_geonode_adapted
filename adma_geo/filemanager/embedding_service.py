"""
ChromaDB embedding service for semantic search functionality
"""
import os
import uuid
import logging
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime

import chromadb
from chromadb.config import Settings
from django.conf import settings
from django.utils import timezone

try:
    import openai
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

from .models import File, Folder, Map

logger = logging.getLogger(__name__)

class EmbeddingService:
    """Service for managing embeddings in ChromaDB"""
    
    def __init__(self):
        try:
            # Initialize ChromaDB client
            chroma_path = getattr(settings, 'CHROMADB_PATH', os.path.join(settings.BASE_DIR, 'chromadb'))
            os.makedirs(chroma_path, exist_ok=True)
            
            self.client = chromadb.PersistentClient(
                path=chroma_path,
                settings=Settings(anonymized_telemetry=False)
            )
            
            # Initialize embedding model
            self.embedding_provider = getattr(settings, 'EMBEDDING_PROVIDER', 'openai')  # 'openai' or 'sentence_transformers'
            self.provider = self.embedding_provider  # Alias for backward compatibility
            
            if self.embedding_provider == 'openai':
                if not OPENAI_AVAILABLE:
                    raise RuntimeError("OpenAI library not installed. Please install with: pip install openai")
                
                # Configure OpenAI
                self.openai_api_key = getattr(settings, 'OPENAI_API_KEY', os.getenv('OPENAI_API_KEY'))
                if not self.openai_api_key:
                    raise RuntimeError("OpenAI API key not found. Set OPENAI_API_KEY in settings or environment variables.")
                
                # Initialize OpenAI client
                self.openai_client = openai.OpenAI(api_key=self.openai_api_key)
                self.embedding_model_name = getattr(settings, 'OPENAI_EMBEDDING_MODEL', 'text-embedding-3-small')
                self.embedding_model = None  # Not needed for OpenAI
                
                logger.info(f"Initialized OpenAI embeddings with model: {self.embedding_model_name}")
                
            else:
                # Fallback to Sentence Transformers
                try:
                    from sentence_transformers import SentenceTransformer
                    model_name = getattr(settings, 'EMBEDDING_MODEL', 'all-MiniLM-L6-v2')
                    self.embedding_model = SentenceTransformer(model_name)
                    logger.info(f"Initialized Sentence Transformers with model: {model_name}")
                except ImportError:
                    raise RuntimeError("sentence-transformers library not installed. Please install with: pip install sentence-transformers")
            
            # Get or create collection
            collection_name = getattr(settings, 'CHROMADB_COLLECTION', 'adma_metadata')
            try:
                self.collection = self.client.get_collection(collection_name)
            except Exception:
                self.collection = self.client.create_collection(
                    name=collection_name,
                    metadata={"description": "ADMA file and folder metadata embeddings"}
                )
            
            # Similarity threshold for search results
            self.similarity_threshold = getattr(settings, 'EMBEDDING_SIMILARITY_THRESHOLD', 0.7)
            
            logger.info("EmbeddingService initialized successfully")
            
        except Exception as e:
            logger.error(f"Failed to initialize EmbeddingService: {e}")
            # Set defaults for failed initialization
            self.client = None
            self.embedding_model = None
            self.collection = None
            self.similarity_threshold = 0.7
            raise RuntimeError(f"ChromaDB initialization failed: {e}")
    
    def __del__(self):
        """Cleanup method to properly close ChromaDB connections"""
        try:
            if hasattr(self, 'client') and self.client:
                # ChromaDB doesn't have an explicit close method, but we can clear references
                self.client = None
                logger.debug("EmbeddingService cleanup completed")
        except Exception as e:
            logger.debug(f"Error during EmbeddingService cleanup: {e}")
    
    def generate_embedding(self, text: str) -> List[float]:
        """Generate embedding for given text using configured provider"""
        try:
            if self.embedding_provider == 'openai':
                # Use OpenAI API
                response = self.openai_client.embeddings.create(
                    model=self.embedding_model_name,
                    input=text
                )
                embedding = response.data[0].embedding
                return embedding
            else:
                # Use Sentence Transformers
                embedding = self.embedding_model.encode(text)
                return embedding.tolist()
        except Exception as e:
            logger.error(f"Error generating embedding with {self.embedding_provider}: {e}")
            return []
    
    def add_file_embedding(self, file_obj: File) -> bool:
        """Add or update file embedding in ChromaDB"""
        try:
            metadata_text = file_obj.get_metadata_for_embedding()
            embedding = self.generate_embedding(metadata_text)
            
            if not embedding:
                return False
            
            # Generate ChromaDB ID if not exists
            if not file_obj.chroma_id:
                file_obj.chroma_id = str(file_obj.id)
            
            # Prepare metadata for ChromaDB
            metadata = {
                "type": "file",
                "id": str(file_obj.id),
                "name": file_obj.name,
                "owner_id": str(file_obj.owner.id),
                "owner_username": file_obj.owner.username,
                "is_public": file_obj.is_public,
                "file_type": file_obj.file_type or "unknown",
                "is_spatial": file_obj.is_spatial,
                "folder_id": str(file_obj.folder.id) if file_obj.folder else None,
                "folder_name": file_obj.folder.name if file_obj.folder else None,
                "created_at": file_obj.created_at.isoformat(),
                "updated_at": file_obj.updated_at.isoformat(),
                "file_size": file_obj.file_size,
                "mime_type": file_obj.mime_type or "",
            }
            
            # Add GIS-specific metadata
            if file_obj.is_spatial:
                metadata.update({
                    "gis_status": file_obj.gis_status,
                    "crs": file_obj.crs or "",
                    "geoserver_layer_name": file_obj.geoserver_layer_name or "",
                })
            
            # Add to ChromaDB
            self.collection.upsert(
                ids=[file_obj.chroma_id],
                embeddings=[embedding],
                documents=[metadata_text],
                metadatas=[metadata]
            )
            
            # Update timestamp
            file_obj.embedding_updated_at = timezone.now()
            file_obj.save(update_fields=['chroma_id', 'embedding_updated_at'])
            
            logger.info(f"Added file embedding: {file_obj.name}")
            return True
            
        except Exception as e:
            logger.error(f"Error adding file embedding for {file_obj.name}: {e}")
            return False
    
    def add_folder_embedding(self, folder_obj: Folder) -> bool:
        """Add or update folder embedding in ChromaDB"""
        try:
            metadata_text = folder_obj.get_metadata_for_embedding()
            embedding = self.generate_embedding(metadata_text)
            
            if not embedding:
                return False
            
            # Generate ChromaDB ID if not exists
            if not folder_obj.chroma_id:
                folder_obj.chroma_id = str(folder_obj.id)
            
            # Prepare metadata for ChromaDB
            metadata = {
                "type": "folder",
                "id": str(folder_obj.id),
                "name": folder_obj.name,
                "owner_id": str(folder_obj.owner.id),
                "owner_username": folder_obj.owner.username,
                "is_public": folder_obj.is_public,
                "parent_id": str(folder_obj.parent.id) if folder_obj.parent else None,
                "parent_name": folder_obj.parent.name if folder_obj.parent else None,
                "full_path": folder_obj.get_full_path(),
                "created_at": folder_obj.created_at.isoformat(),
                "updated_at": folder_obj.updated_at.isoformat(),
                "file_count": folder_obj.files.count(),
                "subfolder_count": folder_obj.subfolders.count(),
            }
            
            # Add to ChromaDB
            self.collection.upsert(
                ids=[folder_obj.chroma_id],
                embeddings=[embedding],
                documents=[metadata_text],
                metadatas=[metadata]
            )
            
            # Update timestamp
            folder_obj.embedding_updated_at = timezone.now()
            folder_obj.save(update_fields=['chroma_id', 'embedding_updated_at'])
            
            logger.info(f"Added folder embedding: {folder_obj.name}")
            return True
            
        except Exception as e:
            logger.error(f"Error adding folder embedding for {folder_obj.name}: {e}")
            return False
    
    def generate_map_embedding(self, map_obj) -> bool:
        """Add or update map embedding in ChromaDB"""
        try:
            metadata_text = map_obj.get_metadata_for_embedding()
            embedding = self.generate_embedding(metadata_text)
            
            if not embedding:
                return False
            
            # Generate ChromaDB ID if not exists
            if not map_obj.chroma_id:
                map_obj.chroma_id = str(map_obj.id)
            
            # Get layer information
            layer_count = map_obj.map_layers.count()
            layer_names = [layer.file.name for layer in map_obj.map_layers.all()]
            layer_types = list(set([layer.file.file_type for layer in map_obj.map_layers.all()]))
            
            # Prepare metadata for ChromaDB (ensure no None values)
            metadata = {
                "type": "map",
                "id": str(map_obj.id),
                "name": map_obj.name or "",
                "owner_id": str(map_obj.owner.id),
                "owner_username": map_obj.owner.username or "",
                "is_public": bool(map_obj.is_public),
                "description": map_obj.description or "",
                "layer_count": int(layer_count),
                "layer_names": ", ".join(layer_names) if layer_names else "",
                "layer_types": ", ".join(layer_types) if layer_types else "",
                "file_type": "map",  # For search filtering
                "spatial": True,     # Maps are always spatial
                "created_at": map_obj.created_at.isoformat(),
                "updated_at": map_obj.updated_at.isoformat(),
                "center_lat": float(map_obj.center_lat) if map_obj.center_lat is not None else 0.0,
                "center_lng": float(map_obj.center_lng) if map_obj.center_lng is not None else 0.0,
                "zoom_level": int(map_obj.zoom_level) if map_obj.zoom_level is not None else 5,
            }
            
            # Add to ChromaDB with error handling
            try:
                self.collection.upsert(
                    ids=[map_obj.chroma_id],
                    embeddings=[embedding],
                    documents=[metadata_text],
                    metadatas=[metadata]
                )
                logger.info(f"Successfully upserted map embedding to ChromaDB: {map_obj.name}")
            except Exception as chromadb_error:
                logger.error(f"ChromaDB upsert failed for map {map_obj.name}: {chromadb_error}")
                logger.error(f"Metadata that caused the error: {metadata}")
                raise chromadb_error
            
            # Update timestamp
            map_obj.embedding_updated_at = timezone.now()
            map_obj.save(update_fields=['chroma_id', 'embedding_updated_at'])
            
            logger.info(f"Added map embedding: {map_obj.name}")
            return True
            
        except Exception as e:
            logger.error(f"Error adding map embedding for {map_obj.name}: {e}")
            return False
    
    def remove_embedding(self, chroma_id: str) -> bool:
        """Remove embedding from ChromaDB"""
        try:
            self.collection.delete(ids=[chroma_id])
            logger.info(f"Removed embedding: {chroma_id}")
            return True
        except Exception as e:
            logger.error(f"Error removing embedding {chroma_id}: {e}")
            return False
    
    def remove_file_embedding(self, file_obj) -> bool:
        """Remove file embedding from ChromaDB"""
        if not file_obj.chroma_id:
            logger.warning(f"File {file_obj.name} has no chroma_id to remove")
            return True
            
        try:
            success = self.remove_embedding(file_obj.chroma_id)
            if success:
                # Clear the chroma_id from the database
                file_obj.chroma_id = None
                file_obj.embedding_updated_at = None
                file_obj.save(update_fields=['chroma_id', 'embedding_updated_at'])
                logger.info(f"Removed file embedding: {file_obj.name}")
            return success
        except Exception as e:
            logger.error(f"Error removing file embedding for {file_obj.name}: {e}")
            return False
    
    def remove_folder_embedding(self, folder_obj) -> bool:
        """Remove folder embedding from ChromaDB"""
        if not folder_obj.chroma_id:
            logger.warning(f"Folder {folder_obj.name} has no chroma_id to remove")
            return True
            
        try:
            success = self.remove_embedding(folder_obj.chroma_id)
            if success:
                # Clear the chroma_id from the database
                folder_obj.chroma_id = None
                folder_obj.embedding_updated_at = None
                folder_obj.save(update_fields=['chroma_id', 'embedding_updated_at'])
                logger.info(f"Removed folder embedding: {folder_obj.name}")
            return success
        except Exception as e:
            logger.error(f"Error removing folder embedding for {folder_obj.name}: {e}")
            return False
    
    def remove_map_embedding(self, map_obj) -> bool:
        """Remove map embedding from ChromaDB"""
        if not map_obj.chroma_id:
            logger.warning(f"Map {map_obj.name} has no chroma_id to remove")
            return True
            
        try:
            success = self.remove_embedding(map_obj.chroma_id)
            if success:
                # Clear the chroma_id from the database
                map_obj.chroma_id = None
                map_obj.embedding_updated_at = None
                map_obj.save(update_fields=['chroma_id', 'embedding_updated_at'])
                logger.info(f"Removed map embedding: {map_obj.name}")
            return success
        except Exception as e:
            logger.error(f"Error removing map embedding for {map_obj.name}: {e}")
            return False
    
    def search_similar(self, 
                      query_text: str, 
                      user_id: str,
                      filters: Optional[Dict[str, Any]] = None,
                      limit: int = 100) -> List[Dict[str, Any]]:
        """
        Search for items using metadata filtering and optional semantic search
        
        Logic:
        1. First apply metadata filters to get eligible items
        2. If query_text is empty: return all eligible items (up to limit)
        3. If query_text provided: perform semantic search on eligible items
        
        Args:
            query_text: Text to search for (empty string for filter-only search)
            user_id: ID of the requesting user
            filters: Additional metadata filters
            limit: Maximum number of results
            
        Returns:
            List of matching items with metadata and optional similarity scores
        """
        try:
            # Build where clause for user access permissions
            where_clause = {
                "$or": [
                    {"owner_id": str(user_id)},  # User's own items
                    {"is_public": True}          # Public items
                ]
            }
            
            # Add additional filters
            if filters:
                # Combine user access with additional filters using $and
                combined_filters = {"$and": [where_clause]}
                
                for key, value in filters.items():
                    if value is not None and value != "":
                        if key == "type":
                            combined_filters["$and"].append({"type": value})
                        elif key == "file_type":
                            combined_filters["$and"].append({"file_type": value})
                        elif key == "is_spatial":
                            combined_filters["$and"].append({"is_spatial": value})
                        elif key == "is_public":
                            combined_filters["$and"].append({"is_public": value})
                        elif key == "owner_username":
                            combined_filters["$and"].append({"owner_username": {"$regex": value}})
                
                where_clause = combined_filters
            
            # Check if we have a search query
            query_text_stripped = query_text.strip() if query_text else ""
            
            if not query_text_stripped:
                # No search query - return all eligible items based on filters
                logger.info("Performing metadata-only search (no query text)")
                results = self.collection.get(
                    where=where_clause,
                    limit=limit,
                    include=["metadatas", "documents"]
                )
                
                # Process results without similarity scores
                items = []
                if results['ids']:
                    for i, item_id in enumerate(results['ids']):
                        metadata = results['metadatas'][i]
                        document = results['documents'][i]
                        
                        items.append({
                            'id': metadata['id'],
                            'type': metadata['type'],
                            'name': metadata['name'],
                            'metadata': metadata,
                            'document': document,
                            'similarity': None,  # No similarity for filter-only search
                            'distance': None
                        })
                
                logger.info(f"Metadata-only search returned {len(items)} results")
                return items
                
            else:
                # Have search query - perform semantic search on eligible items
                logger.info(f"Performing semantic search for query: '{query_text_stripped}'")
                
                # Generate embedding for query
                query_embedding = self.generate_embedding(query_text_stripped)
                if not query_embedding:
                    logger.warning("Failed to generate embedding for query")
                    return []
                
                # Search in ChromaDB with semantic similarity
                # Get enough results to apply threshold logic  
                search_limit = max(limit * 3, 100)  # Get more results for better filtering
                results = self.collection.query(
                    query_embeddings=[query_embedding],
                    n_results=search_limit,
                    where=where_clause,
                    include=["metadatas", "documents", "distances"]
                )
                
                # Process all results first
                all_items = []
                if results['ids'] and results['ids'][0]:
                    for i, item_id in enumerate(results['ids'][0]):
                        distance = results['distances'][0][i]
                        similarity = 1 - distance  # Convert distance to similarity
                        
                        metadata = results['metadatas'][0][i]
                        document = results['documents'][0][i]
                        
                        all_items.append({
                            'id': metadata['id'],
                            'type': metadata['type'],
                            'name': metadata['name'],
                            'metadata': metadata,
                            'document': document,
                            'similarity': similarity,
                            'distance': distance
                        })
                
                # Apply new logic: top 10 OR similarity > threshold, whichever is larger
                top_n = min(10, len(all_items))
                threshold_filtered = [item for item in all_items if item['similarity'] > self.similarity_threshold]
                
                if len(threshold_filtered) > top_n:
                    # Threshold filtering gives more results, use it
                    items = threshold_filtered[:limit]  # Still respect overall limit
                    logger.info(f"Using threshold filtering: {len(threshold_filtered)} items > {self.similarity_threshold} similarity (larger than top {top_n})")
                else:
                    # Top N gives more results (or equal), use it
                    items = all_items[:top_n]
                    logger.info(f"Using top-N filtering: top {top_n} results (larger than or equal to {len(threshold_filtered)} threshold results)")
                
                logger.info(f"Semantic search for '{query_text_stripped}' returned {len(items)} results")
                return items
            
        except Exception as e:
            logger.error(f"Error in search: {e}")
            return []
    
    def update_all_embeddings(self) -> Tuple[int, int]:
        """Update embeddings for all files and folders"""
        files_updated = 0
        folders_updated = 0
        
        try:
            # Update all files
            for file_obj in File.objects.all():
                if self.add_file_embedding(file_obj):
                    files_updated += 1
            
            # Update all folders
            for folder_obj in Folder.objects.all():
                if self.add_folder_embedding(folder_obj):
                    folders_updated += 1
            
            logger.info(f"Updated embeddings: {files_updated} files, {folders_updated} folders")
            
        except Exception as e:
            logger.error(f"Error updating all embeddings: {e}")
        
        return files_updated, folders_updated
    
    def get_collection_stats(self) -> Dict[str, Any]:
        """Get statistics about the ChromaDB collection"""
        try:
            count = self.collection.count()
            return {
                "total_embeddings": count,
                "collection_name": self.collection.name,
                "similarity_threshold": self.similarity_threshold
            }
        except Exception as e:
            logger.error(f"Error getting collection stats: {e}")
            return {}

# Global instance - use lazy initialization to avoid issues with Django auto-reload
_embedding_service_instance = None

def get_embedding_service():
    """Get or create the embedding service instance (lazy initialization)"""
    global _embedding_service_instance
    if _embedding_service_instance is None:
        _embedding_service_instance = EmbeddingService()
    return _embedding_service_instance

# For backward compatibility
class EmbeddingServiceProxy:
    """Proxy object that delegates to the lazily-initialized embedding service"""
    def __getattr__(self, name):
        return getattr(get_embedding_service(), name)

embedding_service = EmbeddingServiceProxy()
