from django.contrib import admin
from .models import Folder, File

@admin.register(Folder)
class FolderAdmin(admin.ModelAdmin):
    list_display = ['name', 'owner', 'parent', 'is_public', 'created_at']
    list_filter = ['is_public', 'created_at']
    search_fields = ['name']
    raw_id_fields = ['parent', 'owner']

@admin.register(File)
class FileAdmin(admin.ModelAdmin):
    list_display = ['name', 'owner', 'folder', 'file_type', 'file_size', 'is_public', 'created_at']
    list_filter = ['file_type', 'is_public', 'created_at']
    search_fields = ['name']
    raw_id_fields = ['folder', 'owner']
    readonly_fields = ['file_size', 'file_type', 'mime_type']
