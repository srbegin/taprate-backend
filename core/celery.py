import os
from celery import Celery
from celery.schedules import crontab

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')

app = Celery('core')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()


app.conf.beat_schedule = {
    'flush-expired-tokens-daily': {
        'task': 'survey.tasks.cleanup_expired_tokens',
        'schedule': crontab(hour=3, minute=0),  # 3am UTC daily
    },
}
app.conf.timezone = 'UTC'