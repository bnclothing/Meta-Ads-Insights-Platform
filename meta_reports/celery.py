import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meta_reports.settings")

app = Celery("meta_reports")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

