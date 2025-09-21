from django.urls import path
from . import views
from . import map_views

app_name = 'filemanager'

urlpatterns = [
    # Public URLs
    path('', views.HomeView.as_view(), name='home'),
    path('register/', views.RegisterView.as_view(), name='register'),
    path('search/', views.SearchView.as_view(), name='search'),
    path('documentation/', views.DocumentationView.as_view(), name='documentation'),
    path('public/folder/<uuid:folder_id>/', views.public_folder_detail, name='public_folder_detail'),
    path('public/file/<uuid:file_id>/', views.public_file_detail, name='public_file_detail'),
    path('public/file/<uuid:file_id>/download/', views.download_file, name='public_file_download'),
    
    # Authenticated URLs
    path('dashboard/', views.dashboard, name='dashboard'),
    path('folder/<uuid:folder_id>/', views.folder_detail, name='folder_detail'),
    path('file/<uuid:file_id>/', views.file_detail, name='file_detail'),
    path('file/<uuid:file_id>/download/', views.download_file, name='download_file'),
    path('file/<uuid:file_id>/map/', views.map_viewer, name='map_viewer'),
    
    # Public map viewer
    path('public/file/<uuid:file_id>/map/', views.public_map_viewer, name='public_map_viewer'),
    
    # Maps functionality
    path('maps/', map_views.MapsListView.as_view(), name='maps_list'),
    path('maps/create/', map_views.create_map_view, name='create_map'),
    path('maps/<uuid:map_id>/', map_views.MapDetailView.as_view(), name='map_detail'),
    path('maps/<uuid:map_id>/viewer/', map_views.MapViewerView.as_view(), name='composite_map_viewer'),
    
    # AJAX endpoints
    path('api/folder/create/', views.create_folder, name='create_folder'),
    path('api/files/upload/', views.upload_files, name='upload_files'),
    path('api/folders/upload/', views.upload_folders, name='upload_folders'),
    path('api/item/delete/', views.delete_item, name='delete_item'),
    path('api/item/toggle-visibility/', views.toggle_visibility, name='toggle_visibility'),
    path('api/search/', views.search_api, name='search_api'),
    
    # Map AJAX endpoints
    path('api/maps/check-name/', map_views.check_map_name, name='check_map_name'),
    path('api/maps/<uuid:map_id>/add-layer/', map_views.add_layer_to_map, name='add_layer_to_map'),
    path('api/maps/<uuid:map_id>/layers/<uuid:layer_id>/remove/', map_views.remove_layer_from_map, name='remove_layer_from_map'),
    path('api/maps/<uuid:map_id>/update-order/', map_views.update_layer_order, name='update_layer_order'),
    path('api/maps/<uuid:map_id>/delete/', map_views.delete_map, name='delete_map'),
]
