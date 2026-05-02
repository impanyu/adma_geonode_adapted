import os

import django
from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'adma_geo.settings')
django.setup()

from django.conf import settings as django_settings  # noqa: E402

# Build celery conf manually from CELERY_*-prefixed Django settings instead
# of using app.config_from_object('...', namespace='CELERY'). The namespace
# variant has lazy-loading behavior in some celery versions that wipes
# explicit defaults at beat startup, leaving worker_log_format and similar
# attributes missing on app.conf.
_celery_conf = {
    # Required defaults celery looks up at startup. Project values from
    # Django settings (loaded below) override these.
    'worker_log_format': (
        '[%(asctime)s: %(levelname)s/%(processName)s] %(message)s'
    ),
    'worker_task_log_format': (
        '[%(asctime)s: %(levelname)s/%(processName)s] '
        '[%(task_name)s(%(task_id)s)] %(message)s'
    ),
    'beat_scheduler': 'celery.beat:PersistentScheduler',
    'beat_schedule_filename': '/tmp/celerybeat-schedule',
    'beat_max_loop_interval': 300,
    'broker_connection_retry_on_startup': True,
}

# Pull every CELERY_* attribute from Django settings, lowercase the
# remainder, and merge in (project values override defaults above).
for _key in dir(django_settings):
    if _key.startswith('CELERY_'):
        _celery_conf[_key[len('CELERY_'):].lower()] = getattr(django_settings, _key)

app = Celery('adma_geo')
app.conf.update(**_celery_conf)
app.autodiscover_tasks()

@app.task(bind=True)
def debug_task(self):
    print(f'Request: {self.request!r}')
