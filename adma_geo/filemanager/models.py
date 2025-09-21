import os
import uuid
from pathlib import Path
from django.db import models
from django.contrib.auth import get_user_model
from django.urls import reverse

User = get_user_model()

def get_upload_path(instance, filename):
    """Generate upload path based on folder structure"""
    if instance.folder:
        return f"uploads/{instance.folder.get_full_path()}/{filename}"
    return f"uploads/{filename}"

class Folder(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    parent = models.ForeignKey(
        'self', 
        on_delete=models.CASCADE, 
        null=True, 
        blank=True, 
        related_name='subfolders'
    )
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name='folders')
    is_public = models.BooleanField(default=False, help_text="Public folders are visible to everyone")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('name', 'parent', 'owner')
        ordering = ['name']

    def __str__(self):
        return self.name

    def get_full_path(self):
        """Get the full path of the folder"""
        if self.parent:
            return f"{self.parent.get_full_path()}/{self.name}"
        return self.name

    def get_breadcrumbs(self):
        """Get list of parent folders for breadcrumb navigation"""
        breadcrumbs = []
        current = self
        while current:
            breadcrumbs.insert(0, current)
            current = current.parent
        return breadcrumbs

    def get_absolute_url(self):
        return reverse('filemanager:folder_detail', kwargs={'folder_id': self.id})

class File(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    file = models.FileField(upload_to=get_upload_path)
    folder = models.ForeignKey(
        Folder, 
        on_delete=models.CASCADE, 
        null=True, 
        blank=True, 
        related_name='files'
    )
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name='files')
    file_size = models.BigIntegerField(default=0)
    file_type = models.CharField(max_length=50, blank=True)  # 'image', 'text', 'document', 'other'
    mime_type = models.CharField(max_length=100, blank=True)
    is_public = models.BooleanField(default=False, help_text="Public files are visible to everyone")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('name', 'folder', 'owner')
        ordering = ['name']

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if self.file:
            # Set file size
            if not self.file_size:
                self.file_size = self.file.size
            
            # Set name from filename if not provided
            if not self.name:
                self.name = Path(self.file.name).stem
            
            # Determine file type based on extension
            file_ext = Path(self.file.name).suffix.lower()
            if file_ext in ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp']:
                self.file_type = 'image'
            elif file_ext in ['.txt', '.md', '.py', '.js', '.html', '.css', '.json', '.xml']:
                self.file_type = 'text'
            elif file_ext in ['.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx']:
                self.file_type = 'document'
            else:
                self.file_type = 'other'
        
        super().save(*args, **kwargs)

    def get_size_display(self):
        """Return human-readable file size"""
        size = self.file_size
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} PB"

    def get_icon_class(self):
        """Return FontAwesome icon class based on file type"""
        if self.file_type == 'image':
            return 'fas fa-image image-icon'
        elif self.file_type == 'text':
            return 'fas fa-file-code text-icon'
        elif self.file_type == 'document':
            return 'fas fa-file-alt document-icon'
        else:
            return 'fas fa-file file-icon'

    def can_preview(self):
        """Check if file can be previewed in browser"""
        return self.file_type in ['image', 'text']

    def get_absolute_url(self):
        return reverse('filemanager:file_detail', kwargs={'file_id': self.id})
