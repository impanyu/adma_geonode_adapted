#!/usr/bin/env python3

"""
Token-based API URLs for file management.
"""

from django.urls import path
from . import api_views, johndeere_webhook

app_name = 'api'

urlpatterns = [
    # Authentication
    path('auth/token/', api_views.create_token, name='create_token'),

    # File operations
    path('files/', api_views.api_list_files, name='list_files'),
    path('files/upload/', api_views.api_upload_files, name='upload_files'),
    path('files/<uuid:file_id>/download/', api_views.api_download_file, name='download_file'),
    path('files/<uuid:file_id>/metadata/', api_views.api_file_metadata, name='file_metadata'),
    path('files/<uuid:file_id>/delete/', api_views.api_delete_file, name='delete_file'),
    path('files/<uuid:file_id>/update/', api_views.api_update_file, name='update_file'),

    # Folder operations
    path('folders/', api_views.api_list_folders, name='list_folders'),
    path('folders/create/', api_views.api_create_folder, name='create_folder'),
    path('folders/upload/', api_views.api_upload_folders, name='upload_folders'),
    path('folders/<uuid:folder_id>/download/', api_views.api_download_folder, name='download_folder'),
    path('folders/<uuid:folder_id>/info/', api_views.api_folder_info, name='folder_info'),
    path('folders/<uuid:folder_id>/delete/', api_views.api_delete_folder, name='delete_folder'),
    path('folders/<uuid:folder_id>/update/', api_views.api_update_folder, name='update_folder'),

    # Map operations
    path('maps/', api_views.api_list_maps, name='list_maps'),
    path('maps/create/', api_views.api_create_map, name='create_map'),
    path('maps/<uuid:map_id>/delete/', api_views.api_delete_map, name='delete_map'),
    path('maps/<uuid:map_id>/update/', api_views.api_update_map, name='update_map'),

    # Tool operations
    path('tools/', api_views.api_list_tools, name='list_tools'),
    path('tools/<slug:tool_slug>/run/', api_views.api_run_tool, name='run_tool'),
    path('tools/<slug:tool_slug>/status/<str:task_id>/', api_views.api_tool_status, name='tool_status'),

    # User info
    path('user/profile/', api_views.api_user_profile, name='user_profile'),
    path('user/stats/', api_views.api_user_stats, name='user_stats'),

    # Search
    path('search/', api_views.api_search, name='search'),

    # John Deere webhook (Basic Auth, not token auth)
    path(
        'webhooks/johndeere/',
        johndeere_webhook.johndeere_webhook_receiver,
        name='johndeere_webhook',
    ),
]
