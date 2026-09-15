from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path('admin/', admin.site.urls),
    path('accounts/', include('django.contrib.auth.urls')),
    # Mounted after contrib.auth on purpose: Django takes the first match, so
    # /accounts/login/ and friends stay with the existing password views and
    # allauth only serves what they do not define (/accounts/google/login/
    # and its callback).
    path('accounts/', include('allauth.urls')),
    path('api/v1/', include('filemanager.api_urls')),  # Token-based APIs
    path('', include('filemanager.urls')),  # Web interface
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
