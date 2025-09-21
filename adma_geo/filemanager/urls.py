from django.urls import path
from . import views

app_name = 'filemanager'

urlpatterns = [
    # Public URLs
    path('', views.HomeView.as_view(), name='home'),
    path('register/', views.RegisterView.as_view(), name='register'),
    path('public/folder/<uuid:folder_id>/', views.public_folder_detail, name='public_folder_detail'),
    path('public/file/<uuid:file_id>/', views.public_file_detail, name='public_file_detail'),
    
    # Authenticated URLs
    path('dashboard/', views.dashboard, name='dashboard'),
    path('folder/<uuid:folder_id>/', views.folder_detail, name='folder_detail'),
    path('file/<uuid:file_id>/', views.file_detail, name='file_detail'),
    path('file/<uuid:file_id>/download/', views.download_file, name='download_file'),
    path('file/<uuid:file_id>/map/', views.map_viewer, name='map_viewer'),
    
    # Public map viewer
    path('public/file/<uuid:file_id>/map/', views.public_map_viewer, name='public_map_viewer'),
    
    # AJAX endpoints
    path('api/folder/create/', views.create_folder, name='create_folder'),
    path('api/files/upload/', views.upload_files, name='upload_files'),
    path('api/item/delete/', views.delete_item, name='delete_item'),
]
