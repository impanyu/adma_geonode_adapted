from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

# Only the auth views that work here, rather than the whole contrib.auth set.
# That set includes password reset, which needs to send mail: there is no SMTP
# host configured, so the form rendered, accepted an address and could never
# deliver anything. Since registration no longer collects an address either,
# it could not work even with a mail server. Changing a password from inside
# the account needs no mail, so that stays -- and is now linked from the
# profile page, since it is the only way left to change one.
auth_patterns = [
    path('login/', auth_views.LoginView.as_view(), name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path(
        'password_change/',
        auth_views.PasswordChangeView.as_view(
            template_name='registration/password_change_form.html'
        ),
        name='password_change',
    ),
    path(
        'password_change/done/',
        auth_views.PasswordChangeDoneView.as_view(
            template_name='registration/password_change_done.html'
        ),
        name='password_change_done',
    ),
]

urlpatterns = [
    path('admin/', admin.site.urls),
    path('accounts/', include(auth_patterns)),
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
