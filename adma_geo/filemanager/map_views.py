#!/usr/bin/env python3

"""
Views for composite maps functionality.

This module provides views for creating, managing, and viewing composite maps
that combine multiple spatial datasets.
"""

import json
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import ListView, DetailView, CreateView
from django.http import JsonResponse, HttpResponseForbidden
from django.db import models
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from django.contrib import messages
from django.db import transaction, IntegrityError
from django.db import models
from django.core.paginator import Paginator

from .models import Map, MapLayer, File
from .geoserver_layer_group_manager import LayerGroupManager


class MapsListView(LoginRequiredMixin, ListView):
    """List view for user's maps with panel/list view toggle"""
    model = Map
    template_name = 'filemanager/maps_list.html'
    context_object_name = 'maps'
    paginate_by = 20

    def get_queryset(self):
        """Filter maps by user and public/private scope"""
        user = self.request.user
        
        # Get user's own maps + public maps from others
        return Map.objects.filter(
            models.Q(owner=user) | models.Q(is_public=True)
        ).distinct().order_by('-updated_at')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        
        # View mode (panel or list)
        context['view_mode'] = self.request.GET.get('view', 'panel')
        
        # Separate user's maps from public maps
        user_maps = Map.objects.filter(owner=self.request.user).order_by('-updated_at')
        public_maps = Map.objects.filter(is_public=True).exclude(owner=self.request.user).order_by('-updated_at')
        
        context['user_maps'] = user_maps
        context['public_maps'] = public_maps
        
        return context


class MapDetailView(LoginRequiredMixin, DetailView):
    """Detail view for a specific map"""
    model = Map
    template_name = 'filemanager/map_detail.html'
    context_object_name = 'map_obj'
    pk_url_kwarg = 'map_id'

    def get_object(self, queryset=None):
        """Get map with permission check"""
        map_obj = get_object_or_404(Map, id=self.kwargs['map_id'])
        
        # Check permissions
        if not map_obj.is_public and map_obj.owner != self.request.user:
            raise HttpResponseForbidden("You don't have permission to view this map.")
        
        return map_obj

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        
        # Get map layers with their files
        map_layers = self.object.map_layers.select_related('file').order_by('layer_order')
        context['map_layers'] = map_layers
        
        # Check if user can edit this map
        context['can_edit'] = self.object.owner == self.request.user
        
        return context




@login_required
def create_map_view(request):
    """Create new composite map view"""
    if request.method == 'GET':
        # Get available spatial files (user's + public)
        user_files = File.objects.filter(
            owner=request.user,
            is_spatial=True,
            gis_status='published',
            geoserver_layer_name__isnull=False
        ).order_by('name')
        
        public_files = File.objects.filter(
            is_public=True,
            is_spatial=True,
            gis_status='published',
            geoserver_layer_name__isnull=False
        ).exclude(owner=request.user).order_by('name')
        
        context = {
            'user_files': user_files,
            'public_files': public_files,
        }
        
        return render(request, 'filemanager/create_map.html', context)
    
    elif request.method == 'POST':
        try:
            # Get form data
            map_name = request.POST.get('map_name', '').strip()
            map_description = request.POST.get('map_description', '').strip()
            is_public = request.POST.get('is_public') == 'on'
            selected_files = request.POST.getlist('selected_files')
            
            if not map_name:
                messages.error(request, 'Map name is required.')
                return redirect('filemanager:create_map')
            
            if not selected_files:
                messages.error(request, 'Please select at least one file for the map.')
                return redirect('filemanager:create_map')
            
            # Check if map with this name already exists for this user
            if Map.objects.filter(name=map_name, owner=request.user).exists():
                messages.error(request, f'A map named "{map_name}" already exists. Please choose a different name.')
                return redirect('filemanager:create_map')
            
            # Create map and add layers in a transaction
            with transaction.atomic():
                # Create the map
                map_obj = Map.objects.create(
                    name=map_name,
                    description=map_description,
                    owner=request.user,
                    is_public=is_public
                )
                
                # Add selected files as layers
                layer_order = 0
                for file_id in selected_files:
                    try:
                        file_obj = File.objects.get(
                            id=file_id,
                            is_spatial=True,
                            geoserver_layer_name__isnull=False
                        )
                        
                        # Check permissions
                        if file_obj.owner != request.user and not file_obj.is_public:
                            continue
                        
                        MapLayer.objects.create(
                            map=map_obj,
                            file=file_obj,
                            layer_order=layer_order
                        )
                        layer_order += 1
                        
                    except File.DoesNotExist:
                        continue
                
                # Create GeoServer Layer Group
                layer_group_manager = LayerGroupManager()
                success, message = layer_group_manager.create_layer_group(map_obj)
                
                if success:
                    # Calculate and store the center point based on all file bounding boxes
                    map_obj.calculate_and_update_center()
                    
                    # Generate embeddings for the new map
                    try:
                        from .tasks import generate_map_embedding_task
                        generate_map_embedding_task.delay(str(map_obj.id))
                    except Exception as e:
                        import logging
                        logger = logging.getLogger(__name__)
                        logger.error(f"Failed to generate embeddings for map {map_obj.name}: {e}")
                    
                    messages.success(request, f'Map "{map_name}" created successfully!')
                    return redirect('filemanager:map_detail', map_id=map_obj.id)
                else:
                    # Delete the map if GeoServer creation failed
                    map_obj.delete()
                    messages.error(request, f'Failed to create GeoServer Layer Group: {message}')
                    return redirect('filemanager:create_map')
                    
        except IntegrityError as e:
            # Handle unique constraint violation for map name
            if 'unique constraint' in str(e).lower() and 'name' in str(e).lower():
                messages.error(request, f'A map named "{map_name}" already exists. Please choose a different name.')
            else:
                messages.error(request, f'Database error: {str(e)}')
            return redirect('filemanager:create_map')
        except Exception as e:
            messages.error(request, f'Error creating map: {str(e)}')
            return redirect('filemanager:create_map')


@login_required
@require_http_methods(["POST"])
def add_layer_to_map(request, map_id):
    """Add a layer to an existing map"""
    try:
        map_obj = get_object_or_404(Map, id=map_id, owner=request.user)
        
        data = json.loads(request.body)
        file_id = data.get('file_id')
        layer_order = data.get('layer_order', map_obj.layer_count)
        
        file_obj = get_object_or_404(File, id=file_id)
        
        # Check permissions
        if file_obj.owner != request.user and not file_obj.is_public:
            return JsonResponse({'success': False, 'error': 'Permission denied'})
        
        # Check if file is already in map
        if MapLayer.objects.filter(map=map_obj, file=file_obj).exists():
            return JsonResponse({'success': False, 'error': 'File already in map'})
        
        # Add layer
        MapLayer.objects.create(
            map=map_obj,
            file=file_obj,
            layer_order=layer_order
        )
        
        # Update GeoServer Layer Group
        layer_group_manager = LayerGroupManager()
        success, message = layer_group_manager.update_layer_group(map_obj)
        
        if success:
            return JsonResponse({'success': True, 'message': 'Layer added successfully'})
        else:
            return JsonResponse({'success': False, 'error': f'GeoServer error: {message}'})
            
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})


@login_required
@require_http_methods(["POST"])
def remove_layer_from_map(request, map_id, layer_id):
    """Remove a layer from a map"""
    try:
        map_obj = get_object_or_404(Map, id=map_id, owner=request.user)
        map_layer = get_object_or_404(MapLayer, id=layer_id, map=map_obj)
        
        # Remove the layer
        map_layer.delete()
        
        # Update GeoServer Layer Group
        layer_group_manager = LayerGroupManager()
        success, message = layer_group_manager.update_layer_group(map_obj)
        
        if success:
            return JsonResponse({'success': True, 'message': 'Layer removed successfully'})
        else:
            return JsonResponse({'success': False, 'error': f'GeoServer error: {message}'})
            
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})


@login_required
@require_http_methods(["POST"])
def update_layer_order(request, map_id):
    """Update layer ordering in a map"""
    try:
        map_obj = get_object_or_404(Map, id=map_id, owner=request.user)
        
        data = json.loads(request.body)
        layer_orders = data.get('layer_orders', [])
        
        # Update layer orders
        for item in layer_orders:
            layer_id = item.get('layer_id')
            new_order = item.get('order')
            
            MapLayer.objects.filter(
                id=layer_id,
                map=map_obj
            ).update(layer_order=new_order)
        
        # Update GeoServer Layer Group
        layer_group_manager = LayerGroupManager()
        success, message = layer_group_manager.update_layer_group(map_obj)
        
        if success:
            return JsonResponse({'success': True, 'message': 'Layer order updated'})
        else:
            return JsonResponse({'success': False, 'error': f'GeoServer error: {message}'})
            
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})


@login_required
@require_http_methods(["GET"])
def check_map_name(request):
    """Check if a map name already exists for the current user"""
    map_name = request.GET.get('name', '').strip()
    if not map_name:
        return JsonResponse({'exists': False})
    
    exists = Map.objects.filter(name=map_name, owner=request.user).exists()
    return JsonResponse({'exists': exists})


@login_required
@require_http_methods(["POST"])
def toggle_map_visibility(request, map_id):
    """Toggle map visibility between private and public"""
    try:
        map_obj = get_object_or_404(Map, id=map_id, owner=request.user)
        
        import json
        data = json.loads(request.body)
        is_public = data.get('is_public', False)
        
        map_obj.is_public = is_public
        map_obj.save(update_fields=['is_public'])
        
        # Regenerate embeddings since visibility changed
        try:
            from .tasks import generate_map_embedding_task
            generate_map_embedding_task.delay(str(map_obj.id))
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Failed to regenerate embeddings for map {map_obj.name}: {e}")
        
        status = 'public' if is_public else 'private'
        return JsonResponse({
            'success': True, 
            'message': f'Map "{map_obj.name}" is now {status}',
            'is_public': is_public
        })
        
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})


@login_required
def get_available_layers(request, map_id):
    """Get available spatial files that can be added to the map"""
    try:
        map_obj = get_object_or_404(Map, id=map_id, owner=request.user)
        
        # Get files already in the map
        existing_file_ids = set(map_obj.map_layers.values_list('file_id', flat=True))
        
        # Get available spatial files (user's + public) that are not already in the map
        user_files = File.objects.filter(
            owner=request.user,
            is_spatial=True,
            gis_status='published',
            geoserver_layer_name__isnull=False
        ).exclude(id__in=existing_file_ids).order_by('name')
        
        public_files = File.objects.filter(
            is_public=True,
            is_spatial=True,
            gis_status='published',
            geoserver_layer_name__isnull=False
        ).exclude(owner=request.user).exclude(id__in=existing_file_ids).order_by('name')
        
        # Format response
        user_files_data = []
        for file_obj in user_files:
            user_files_data.append({
                'id': str(file_obj.id),
                'name': file_obj.name,
                'file_type': file_obj.file_type,
                'folder': file_obj.folder.get_full_path() if file_obj.folder else None,
                'created_at': file_obj.created_at.strftime('%Y-%m-%d'),
                'size': file_obj.get_size_display(),
            })
        
        public_files_data = []
        for file_obj in public_files:
            public_files_data.append({
                'id': str(file_obj.id),
                'name': file_obj.name,
                'file_type': file_obj.file_type,
                'folder': file_obj.folder.get_full_path() if file_obj.folder else None,
                'owner': file_obj.owner.username,
                'created_at': file_obj.created_at.strftime('%Y-%m-%d'),
                'size': file_obj.get_size_display(),
            })
        
        return JsonResponse({
            'success': True,
            'user_files': user_files_data,
            'public_files': public_files_data
        })
        
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})


@login_required
@require_http_methods(["POST"])
def add_layers_to_map(request, map_id):
    """Add selected layers to the map"""
    try:
        map_obj = get_object_or_404(Map, id=map_id, owner=request.user)
        
        import json
        data = json.loads(request.body)
        selected_file_ids = data.get('file_ids', [])
        
        if not selected_file_ids:
            return JsonResponse({'success': False, 'error': 'No files selected'})
        
        # Get the current highest layer order
        max_order = map_obj.map_layers.aggregate(
            max_order=models.Max('layer_order')
        )['max_order'] or -1
        
        added_layers = []
        layer_order = max_order + 1
        
        with transaction.atomic():
            for file_id in selected_file_ids:
                try:
                    file_obj = File.objects.get(
                        id=file_id,
                        is_spatial=True,
                        gis_status='published',
                        geoserver_layer_name__isnull=False
                    )
                    
                    # Check permissions
                    if file_obj.owner != request.user and not file_obj.is_public:
                        continue
                    
                    # Check if already in map
                    if map_obj.map_layers.filter(file=file_obj).exists():
                        continue
                    
                    # Create map layer
                    map_layer = MapLayer.objects.create(
                        map=map_obj,
                        file=file_obj,
                        layer_order=layer_order
                    )
                    
                    added_layers.append({
                        'id': str(map_layer.id),
                        'name': file_obj.name,
                        'file_type': file_obj.file_type,
                        'layer_order': layer_order,
                        'workspace': file_obj.geoserver_workspace,
                        'geoserver_layer_name': file_obj.geoserver_layer_name,
                        'opacity': map_layer.opacity,
                        'is_visible': map_layer.is_visible
                    })
                    
                    layer_order += 1
                    
                except File.DoesNotExist:
                    continue
        
        if added_layers:
            # Recalculate map center and bounds
            map_obj.calculate_and_update_center()
            
            return JsonResponse({
                'success': True,
                'message': f'Added {len(added_layers)} layer(s) to the map',
                'added_layers': added_layers
            })
        else:
            return JsonResponse({
                'success': False,
                'error': 'No valid layers could be added'
            })
        
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})


@login_required
@require_http_methods(["POST"])
def update_layer_opacity(request, map_id, layer_id):
    """Update layer opacity"""
    try:
        map_obj = get_object_or_404(Map, id=map_id, owner=request.user)
        layer = get_object_or_404(MapLayer, id=layer_id, map=map_obj)
        
        import json
        data = json.loads(request.body)
        opacity = data.get('opacity')
        
        if opacity is None or opacity < 0 or opacity > 1:
            return JsonResponse({'success': False, 'error': 'Invalid opacity value'})
        
        layer.opacity = opacity
        layer.save(update_fields=['opacity'])
        
        return JsonResponse({
            'success': True,
            'message': f'Layer opacity updated to {opacity}',
            'opacity': opacity
        })
        
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})


@login_required
@require_http_methods(["POST"])
def update_layer_visibility(request, map_id, layer_id):
    """Update layer visibility"""
    try:
        map_obj = get_object_or_404(Map, id=map_id, owner=request.user)
        layer = get_object_or_404(MapLayer, id=layer_id, map=map_obj)
        
        import json
        data = json.loads(request.body)
        visible = data.get('visible', True)
        
        layer.is_visible = visible
        layer.save(update_fields=['is_visible'])
        
        return JsonResponse({
            'success': True,
            'message': f'Layer visibility updated to {"visible" if visible else "hidden"}',
            'visible': visible
        })
        
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})


@login_required
@require_http_methods(["DELETE"])
def delete_map(request, map_id):
    """Delete a map"""
    try:
        map_obj = get_object_or_404(Map, id=map_id, owner=request.user)
        map_name = map_obj.name
        
        # Delete the map (this will trigger GeoServer cleanup via model.delete())
        map_obj.delete()
        
        return JsonResponse({'success': True, 'message': f'Map "{map_name}" deleted successfully'})
        
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})
