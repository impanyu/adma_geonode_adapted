from django.contrib import admin
from .models import Folder, File, Map, MapLayer, Tool, AgentSession, AgentMessage, ChatSession


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


@admin.register(Map)
class MapAdmin(admin.ModelAdmin):
    list_display = ['name', 'owner', 'is_public', 'layer_count', 'created_at', 'updated_at']
    list_filter = ['is_public', 'created_at']
    search_fields = ['name', 'description']
    raw_id_fields = ['owner']
    readonly_fields = ['geoserver_layer_group_name', 'created_at', 'updated_at']


@admin.register(MapLayer)
class MapLayerAdmin(admin.ModelAdmin):
    list_display = ['file', 'map', 'layer_order', 'opacity', 'is_visible', 'added_at']
    list_filter = ['is_visible', 'added_at']
    search_fields = ['file__name', 'map__name']
    raw_id_fields = ['map', 'file']


@admin.register(Tool)
class ToolAdmin(admin.ModelAdmin):
    list_display = ['name', 'slug', 'category', 'status', 'is_system_tool', 'is_public', 'is_active', 'usage_count', 'version']
    list_filter = ['category', 'status', 'is_system_tool', 'is_public', 'is_active']
    search_fields = ['name', 'slug', 'description', 'short_description']
    raw_id_fields = ['owner']
    readonly_fields = ['id', 'usage_count', 'created_at', 'updated_at']
    prepopulated_fields = {'slug': ('name',)}
    
    fieldsets = (
        ('Basic Information', {
            'fields': ('name', 'slug', 'short_description', 'description')
        }),
        ('Ownership & Visibility', {
            'fields': ('owner', 'is_system_tool', 'is_public', 'is_active')
        }),
        ('Categorization & Display', {
            'fields': ('category', 'status', 'icon', 'icon_color', 'version')
        }),
        ('Execution Configuration', {
            'fields': ('url_name', 'celery_task_name', 'input_config', 'output_config'),
            'classes': ('collapse',)
        }),
        ('Statistics & Metadata', {
            'fields': ('usage_count', 'id', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )


@admin.register(ChatSession)
class ChatSessionAdmin(admin.ModelAdmin):
    list_display = ['name', 'user', 'is_active', 'created_at', 'updated_at']
    list_filter = ['is_active', 'created_at']
    search_fields = ['name', 'user__username']


@admin.register(AgentSession)
class AgentSessionAdmin(admin.ModelAdmin):
    list_display = ['user', 'status', 'container_port', 'started_at', 'last_activity']
    list_filter = ['status']
    search_fields = ['user__username']
    readonly_fields = ['id', 'container_id', 'gateway_token', 'created_at', 'updated_at']


@admin.register(AgentMessage)
class AgentMessageAdmin(admin.ModelAdmin):
    list_display = ['session', 'role', 'short_content', 'created_at']
    list_filter = ['role', 'created_at']
    search_fields = ['content']
    readonly_fields = ['id', 'created_at']

    def short_content(self, obj):
        return obj.content[:80] + '...' if len(obj.content) > 80 else obj.content
    short_content.short_description = 'Content'


from .johndeere_webhook_tasks import process_johndeere_event_task
from .models import JohnDeereSubscription, JohnDeereWebhookEvent


@admin.register(JohnDeereSubscription)
class JohnDeereSubscriptionAdmin(admin.ModelAdmin):
    list_display = ('jd_subscription_id', 'org_id', 'is_active',
                    'client_endpoint', 'created_at')
    list_filter = ('is_active', 'org_id')
    readonly_fields = ('id', 'created_at', 'updated_at')
    search_fields = ('jd_subscription_id', 'org_id')


@admin.register(JohnDeereWebhookEvent)
class JohnDeereWebhookEventAdmin(admin.ModelAdmin):
    list_display = ('jd_event_id', 'event_type_id', 'org_id',
                    'status', 'received_at', 'processing_completed_at')
    list_filter = ('status', 'event_type_id', 'org_id')
    search_fields = ('jd_event_id', 'target_resource_uri')
    readonly_fields = (
        'id', 'jd_event_id', 'event_type_id', 'org_id',
        'target_resource_uri', 'payload', 'received_at',
        'processing_started_at', 'processing_completed_at',
        'related_folder', 'related_file',
    )
    ordering = ('-received_at',)
    actions = ['reprocess_event']

    @admin.action(description='Re-enqueue selected events for processing')
    def reprocess_event(self, request, queryset):
        count = 0
        for evt in queryset:
            evt.status = JohnDeereWebhookEvent.STATUS_PENDING
            evt.error_message = None
            evt.processing_started_at = None
            evt.processing_completed_at = None
            evt.save(update_fields=[
                'status', 'error_message',
                'processing_started_at', 'processing_completed_at',
            ])
            process_johndeere_event_task.delay(str(evt.id))
            count += 1
        self.message_user(request, f"Re-enqueued {count} event(s).")
