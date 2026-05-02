import os
from pathlib import Path

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# SECURITY WARNING: keep the secret key used in production secret!
# No default — production must set SECRET_KEY in env. Dev can fall back to a
# clearly-marked placeholder via the DJANGO_DEV_FALLBACK env var if desired.
SECRET_KEY = os.environ.get('SECRET_KEY') or (
    'django-insecure-dev-only-do-not-use-in-prod'
    if os.environ.get('DJANGO_DEV_FALLBACK') == '1'
    else None
)
if not SECRET_KEY:
    raise RuntimeError(
        "SECRET_KEY env var is required. Set DJANGO_DEV_FALLBACK=1 only for local dev."
    )

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = os.environ.get('DEBUG', 'False').lower() == 'true'

ALLOWED_HOSTS = ['localhost', '127.0.0.1', '0.0.0.0', 'adma.unl.edu']

# CSRF and CORS settings for production
CSRF_TRUSTED_ORIGINS = [
    'https://adma.unl.edu',
    'http://adma.unl.edu',
    'http://localhost',
    'https://localhost',
]

# Proxy settings for HTTPS detection
USE_X_FORWARDED_HOST = True
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    # 'django.contrib.gis',  # GeoDjango support (disabled for now)

    # Third party apps
    'crispy_forms',
    'crispy_bootstrap5',
    'rest_framework',
    'rest_framework.authtoken',
    'rest_framework_simplejwt',

    # Local apps
    'filemanager',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'adma_geo.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'adma_geo.wsgi.application'

# Database
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',  # Regular PostgreSQL backend
        'NAME': os.environ.get('POSTGRES_DB', 'adma_geo'),
        'USER': os.environ.get('POSTGRES_USER', 'adma_geo'),
        'PASSWORD': os.environ['POSTGRES_PASSWORD'],
        'HOST': os.environ.get('POSTGRES_HOST', 'db'),
        'PORT': os.environ.get('POSTGRES_PORT', '5432'),
    }
}

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'America/Chicago'  # Central Time (Nebraska)
USE_I18N = True
USE_TZ = True

# Static files (CSS, JavaScript, Images)
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [
    BASE_DIR / 'static',
]

# Media files
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Authentication settings
LOGIN_URL = '/accounts/login/'
LOGIN_REDIRECT_URL = '/dashboard/'
LOGOUT_REDIRECT_URL = '/'

# Crispy Forms
CRISPY_ALLOWED_TEMPLATE_PACKS = "bootstrap5"
CRISPY_TEMPLATE_PACK = "bootstrap5"

# Celery Configuration
CELERY_BROKER_URL = os.environ.get('REDIS_URL', 'redis://redis:6379/0')
CELERY_RESULT_BACKEND = os.environ.get('REDIS_URL', 'redis://redis:6379/0')
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = TIME_ZONE

# Explicit celery defaults — namespace='CELERY' loading in celery.py drops
# celery's built-in defaults for these, so we declare them here. Without
# them, `celery beat` / `celery worker` raise AttributeError on startup
# trying to read app.conf.<attr>.
CELERY_WORKER_LOG_FORMAT = (
    '[%(asctime)s: %(levelname)s/%(processName)s] %(message)s'
)
CELERY_WORKER_TASK_LOG_FORMAT = (
    '[%(asctime)s: %(levelname)s/%(processName)s] '
    '[%(task_name)s(%(task_id)s)] %(message)s'
)
CELERY_BEAT_SCHEDULE_FILENAME = '/tmp/celerybeat-schedule'
CELERY_BEAT_SCHEDULER = 'celery.beat:PersistentScheduler'
CELERY_BEAT_MAX_LOOP_INTERVAL = 300
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True

# Celery Beat Schedule (for periodic tasks)
from celery.schedules import crontab

CELERY_BEAT_SCHEDULE = {
    'sync-realm5-daily': {
        'task': 'filemanager.tasks.sync_realm5_task',
        'schedule': crontab(hour=2, minute=0),  # Run at 2:00 AM daily
    },
    'cleanup-idle-agents': {
        'task': 'filemanager.tasks.cleanup_idle_agents_task',
        'schedule': crontab(minute='*/5'),  # Every 5 minutes
    },
}

# Realm5 API Configuration
# API key should be set via environment variable (loaded from .env file)
REALM5_API_KEY = os.environ.get('REALM5_API_KEY')

# John Deere API Configuration
# Credentials should be set via environment variables
JD_CLIENT_ID = os.environ.get('JD_CLIENT_ID')
JD_CLIENT_SECRET = os.environ.get('JD_CLIENT_SECRET')
JD_REFRESH_TOKEN = os.environ.get('JD_REFRESH_TOKEN')
JD_ORG_ID = os.environ.get('JD_ORG_ID', '4193081')  # Default organization ID

# John Deere Webhook (Data Subscription Service)
# The URL John Deere will POST events to, and the Basic Auth creds JD will use.
JD_WEBHOOK_CALLBACK_URL = os.environ.get('JD_WEBHOOK_CALLBACK_URL')
JD_WEBHOOK_USERNAME = os.environ.get('JD_WEBHOOK_USERNAME')
JD_WEBHOOK_PASSWORD = os.environ.get('JD_WEBHOOK_PASSWORD')

# Agent Configuration
AGENT_IMAGE = os.environ.get('AGENT_IMAGE', 'adma-openclaw-agent:latest')
AGENT_NETWORK = os.environ.get('AGENT_NETWORK', 'adma_network')
AGENT_WORKSPACES_DIR = os.environ.get('AGENT_WORKSPACES_DIR', '/opt/adma/agent_workspaces')
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY', '')

# File Upload Settings
FILE_UPLOAD_MAX_MEMORY_SIZE = 500 * 1024 * 1024  # 500MB
DATA_UPLOAD_MAX_MEMORY_SIZE = 500 * 1024 * 1024  # 500MB
DATA_UPLOAD_MAX_NUMBER_FILES = 1000  # Allow up to 1000 files per upload for folder uploads

# GeoServer Configuration
GEOSERVER_URL = os.environ.get('GEOSERVER_URL', 'http://geoserver:8080/geoserver')
GEOSERVER_ADMIN_USER = os.environ.get('GEOSERVER_ADMIN_USER', 'admin')
GEOSERVER_ADMIN_PASSWORD = os.environ['GEOSERVER_ADMIN_PASSWORD']
GEOSERVER_WORKSPACE = 'adma_geo'

# Supported GIS file formats
# GIS file extensions that should be automatically processed and published to GeoServer
GIS_FILE_EXTENSIONS = [
    '.geojson', '.shp', '.tiff', '.tif', '.geotiff', '.geotif'
]

# All spatial file extensions (including ones that don't auto-process)
ALL_SPATIAL_EXTENSIONS = [
    '.gpkg', '.geojson', '.shp', '.kml', '.kmz',
    '.tiff', '.tif', '.geotiff', '.geotif', '.zip'
]

# Proxy Settings for HTTPS detection
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
USE_X_FORWARDED_HOST = True

# Django REST Framework Configuration
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.TokenAuthentication',
        'rest_framework.authentication.SessionAuthentication',  # Keep for web interface
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_RENDERER_CLASSES': [
        'rest_framework.renderers.JSONRenderer',
    ],
    'DEFAULT_PARSER_CLASSES': [
        'rest_framework.parsers.JSONParser',
        'rest_framework.parsers.MultiPartParser',
        'rest_framework.parsers.FormParser',
    ],
}

# ChromaDB and Embedding Settings
# NOTE: ChromaDB and embedding settings removed
# Search now uses PostgreSQL text matching instead of semantic embeddings

