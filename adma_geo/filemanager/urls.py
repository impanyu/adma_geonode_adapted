from django.urls import path
from django.views.generic import RedirectView
from . import views
from . import map_views
from . import api_views
from . import native_tool_views

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
    
    # Agent chat page
    path('agent/', views.agent_chat_page, name='agent_chat'),

    # Authenticated URLs
    path('profile/', views.profile, name='profile'),
    path('dashboard/', views.dashboard, name='dashboard'),
    path('tools/', views.ToolsListView.as_view(), name='tools_list'),
    path('tools/seeding/', views.SeedingToolView.as_view(), name='seeding_tool'),
    path('tools/shape-to-json/', views.ShapeToJsonToolView.as_view(), name='shape_to_json_tool'),
    path('tools/si/', views.SIToolView.as_view(), name='si_tool'),
    path('tools/yield-summary/', views.YieldSummaryToolView.as_view(), name='yield_summary_tool'),
    path('folder/<uuid:folder_id>/', views.folder_detail, name='folder_detail'),
    path('file/<uuid:file_id>/', views.file_detail, name='file_detail'),
    path('file/<uuid:file_id>/download/', views.download_file, name='download_file'),
    path('file/<uuid:file_id>/geojson/', views.geojson_data, name='geojson_data'),
    path('file/<uuid:file_id>/map/', views.map_viewer, name='map_viewer'),
    
    # Public map viewer
    path('public/file/<uuid:file_id>/map/', views.public_map_viewer, name='public_map_viewer'),
    path('public/map/<uuid:map_id>/', map_views.public_map_detail, name='public_map_detail'),
    
    # Maps functionality
    path('maps/', map_views.MapsListView.as_view(), name='maps_list'),
    path('maps/create/', map_views.create_map_view, name='create_map'),
    path('maps/<uuid:map_id>/', map_views.MapDetailView.as_view(), name='map_detail'),
    
    # AJAX endpoints
    path('api/folder/create/', views.create_folder, name='create_folder'),
    path('api/files/upload/', views.upload_files, name='upload_files'),
    path('api/folders/upload/', views.upload_folders, name='upload_folders'),
    path('api/item/delete/', views.delete_item, name='delete_item'),
    path('api/item/toggle-visibility/', views.toggle_visibility, name='toggle_visibility'),
    path('api/task/<str:task_id>/status/', views.check_deletion_status, name='check_deletion_status'),
    path('api/dashboard/stats/', views.dashboard_stats, name='dashboard_stats'),
    path('api/search/', views.search_api, name='search_api'),
    
    # Seeding Tool endpoints
    path('api/seeding-tool/run/', views.run_seeding_tool, name='run_seeding_tool'),
    path('api/seeding-tool/status/<str:task_id>/', views.check_seeding_tool_status, name='check_seeding_tool_status'),
    path('api/seeding-tool/columns/<str:file_id>/', views.seeding_tool_columns, name='seeding_tool_columns'),
    
    # Shape to JSON Tool endpoints
    path('api/shape-to-json/run/', views.run_shape_to_json, name='run_shape_to_json'),
    path('api/shape-to-json/status/<str:task_id>/', views.check_shape_to_json_status, name='check_shape_to_json_status'),
    
    # SI Tool endpoints
    path('api/si-tool/run/', views.run_si_tool, name='run_si_tool'),
    path('api/si-tool/status/<str:task_id>/', views.check_si_tool_status, name='check_si_tool_status'),
    
    # Yield Summary Tool endpoints
    path('api/yield-summary-tool/run/', views.run_yield_summary_tool, name='run_yield_summary_tool'),
    path('api/yield-summary-tool/status/<str:task_id>/', views.check_yield_summary_tool_status, name='check_yield_summary_tool_status'),

    # Valid Yield Extractor Tool
    path('tools/valid-yield-extractor/', views.ValidYieldExtractorToolView.as_view(), name='valid_yield_extractor_tool'),
    path('api/valid-yield-extractor/run/', views.run_valid_yield_extractor, name='run_valid_yield_extractor'),
    path('api/valid-yield-extractor/status/<str:task_id>/', views.check_valid_yield_extractor_status, name='check_valid_yield_extractor_status'),
    path('api/valid-yield-extractor/columns/<str:file_id>/', views.valid_yield_extractor_columns, name='valid_yield_extractor_columns'),

    # File management endpoints
    path('api/file/rename/', views.rename_file, name='rename_file'),
    
    # Map AJAX endpoints
    path('api/maps/check-name/', map_views.check_map_name, name='check_map_name'),
    path('api/maps/<uuid:map_id>/available-layers/', map_views.get_available_layers, name='get_available_layers'),
    path('api/maps/<uuid:map_id>/add-layers/', map_views.add_layers_to_map, name='add_layers_to_map'),
    path('api/maps/<uuid:map_id>/add-layer/', map_views.add_layer_to_map, name='add_layer_to_map'),
    path('api/maps/<uuid:map_id>/layers/<uuid:layer_id>/remove/', map_views.remove_layer_from_map, name='remove_layer_from_map'),
    path('api/maps/<uuid:map_id>/layers/<uuid:layer_id>/opacity/', map_views.update_layer_opacity, name='update_layer_opacity'),
    path('api/maps/<uuid:map_id>/layers/<uuid:layer_id>/visibility/', map_views.update_layer_visibility, name='update_layer_visibility'),
    path('api/maps/<uuid:map_id>/update-order/', map_views.update_layer_order, name='update_layer_order'),
    path('api/maps/<uuid:map_id>/toggle-visibility/', map_views.toggle_map_visibility, name='toggle_map_visibility'),
    path('api/maps/<uuid:map_id>/delete/', map_views.delete_map, name='delete_map'),

    # Agent API endpoints (session-auth, browser-facing)
    path('api/agent/chat/', api_views.api_agent_chat, name='agent_chat_api'),
    path('api/agent/chat/history/', api_views.api_agent_history, name='agent_history'),
    path('api/agent/status/', api_views.api_agent_status, name='agent_status'),
    path('api/agent/stop/', api_views.api_agent_stop, name='agent_stop'),
    path('api/agent/sessions/', api_views.api_agent_sessions, name='agent_sessions'),
    path('api/agent/sessions/create/', api_views.api_agent_session_create, name='agent_session_create'),
    path('api/agent/sessions/rename/', api_views.api_agent_session_rename, name='agent_session_rename'),
    path('api/agent/sessions/delete/', api_views.api_agent_session_delete, name='agent_session_delete'),

    # Native GIS tools. The four share one status endpoint: polling a Celery
    # result does not depend on which task produced it.
    path('tools/zonal-statistics/', native_tool_views.ZonalStatisticsToolView.as_view(), name='zonal_statistics_tool'),
    path('tools/vegetation-index/', native_tool_views.VegetationIndexToolView.as_view(), name='vegetation_index_tool'),
    path('tools/management-zones/', native_tool_views.ManagementZonesToolView.as_view(), name='management_zones_tool'),
    path('tools/raster-clip/', native_tool_views.RasterClipToolView.as_view(), name='raster_clip_tool'),
    path('tools/public-data/', native_tool_views.PublicDataToolView.as_view(), name='public_data_tool'),
    path('tools/terrain/', native_tool_views.TerrainToolView.as_view(), name='terrain_tool'),
    path('tools/point-sampling/', native_tool_views.PointSamplingToolView.as_view(), name='point_sampling_tool'),
    path('tools/vector-ops/', native_tool_views.VectorOpsToolView.as_view(), name='vector_ops_tool'),
    path('tools/boundary-generator/', native_tool_views.BoundaryGeneratorToolView.as_view(), name='boundary_generator_tool'),

    path('api/zonal-statistics/run/', native_tool_views.run_zonal_statistics, name='run_zonal_statistics'),
    path('api/vegetation-index/run/', native_tool_views.run_vegetation_index, name='run_vegetation_index'),
    path('api/management-zones/run/', native_tool_views.run_management_zones, name='run_management_zones'),
    path('api/raster-clip/run/', native_tool_views.run_raster_clip_reproject, name='run_raster_clip_reproject'),
    path('api/public-data/run/', native_tool_views.run_public_data_fetch, name='run_public_data_fetch'),
    path('api/terrain/run/', native_tool_views.run_terrain_analysis, name='run_terrain_analysis'),
    path('api/point-sampling/run/', native_tool_views.run_point_sampling, name='run_point_sampling'),
    path('api/vector-ops/run/', native_tool_views.run_vector_operation, name='run_vector_operation'),
    path('api/boundary-generator/run/', native_tool_views.run_boundary_generator, name='run_boundary_generator'),
    path('api/native-tools/all-columns/<str:file_id>/', native_tool_views.vector_all_columns, name='native_tool_all_columns'),

    path('api/native-tools/status/<str:task_id>/', native_tool_views.native_tool_status, name='native_tool_status'),
    path('api/native-tools/columns/<str:file_id>/', native_tool_views.vector_columns, name='native_tool_columns'),
    path('api/native-tools/bands/<str:file_id>/', native_tool_views.raster_bands, name='native_tool_bands'),
]
