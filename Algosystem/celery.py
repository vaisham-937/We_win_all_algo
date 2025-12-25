# import os
# from celery import Celery

# # Set the default Django settings module for the 'celery' program.
# os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'Algosystem.settings')

# app = Celery('Algosystem')

# app.config_from_object('django.conf:settings', namespace='CELERY')

# # Load task modules from all registered Django apps.
# app.autodiscover_tasks()

# @app.task(bind=True, ignore_result=True)
# def debug_task(self):
#     print(f'Request: {self.request!r}')

import os
from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'Algosystem.settings')

app = Celery('Algosystem')

# Read celery settings from Django settings (prefix CELERY_)
app.config_from_object('django.conf:settings', namespace='CELERY')

# Basic runtime overrides (sensible defaults)
app.conf.broker_url = os.environ.get('CELERY_BROKER_URL', 'redis://127.0.0.1:6379/1')
app.conf.result_backend = os.environ.get('CELERY_RESULT_BACKEND', 'redis://127.0.0.1:6379/1')

app.conf.task_serializer = 'json'
app.conf.result_serializer = 'json'
app.conf.accept_content = ['json']
app.conf.timezone = 'Asia/Kolkata'
app.conf.enable_utc = False

# safety for HFT-ish tasks
app.conf.worker_prefetch_multiplier = 1    # don't prefetch too many tasks
app.conf.task_acks_late = True             # ack after success
app.conf.task_reject_on_worker_lost = True

# Load task modules from all registered Django apps.
app.autodiscover_tasks()

@app.task(bind=True, ignore_result=True)
def debug_task(self):
    print(f'Request: {self.request!r}')
