import os
from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'adma_geo.settings')

app = Celery('adma_geo')
app.config_from_object('django.conf:settings', namespace='CELERY')

# Force-set celery defaults that namespace='CELERY' translation drops in
# some celery versions. Without these, `celery beat` raises
# AttributeError on app.conf.worker_log_format / beat_scheduler / etc.
app.conf.update(
    worker_log_format=(
        '[%(asctime)s: %(levelname)s/%(processName)s] %(message)s'
    ),
    worker_task_log_format=(
        '[%(asctime)s: %(levelname)s/%(processName)s] '
        '[%(task_name)s(%(task_id)s)] %(message)s'
    ),
    beat_scheduler='celery.beat:PersistentScheduler',
    beat_schedule_filename='/tmp/celerybeat-schedule',
    beat_max_loop_interval=300,
    broker_connection_retry_on_startup=True,
)

app.autodiscover_tasks()

@app.task(bind=True)
def debug_task(self):
    print(f'Request: {self.request!r}')
