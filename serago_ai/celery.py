"""
Celery application for SeraGo-AI.

Configures the Celery worker and Beat scheduler. The Beat schedule
runs the periodic scoring tasks (15-min active sweep, nightly batch).
"""

import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "serago_ai.settings")

app = Celery("serago_ai")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# ── Beat schedule ─────────────────────────────────────────────────────
# Configurable via env vars so deployment can adjust timing.

SCORE_BATCH_HOUR = int(os.environ.get("SCORE_BATCH_HOUR", "0"))
SCORE_BATCH_MINUTE = int(os.environ.get("SCORE_BATCH_MINUTE", "0"))

app.conf.beat_schedule = {
    # Periodic sweep: score new scraped jobs against active users
    # Runs every 15 minutes during peak hours
    "sweep-active-users": {
        "task": "matching_engine.tasks.sweep_active_users",
        "schedule": crontab(minute="*/15"),
    },

    # Nightly catch-all: score everything that's been missed
    # Configurable hour via SCORE_BATCH_HOUR env var
    "score-tonight-jobs": {
        "task": "matching_engine.tasks.score_nightly_batch",
        "schedule": crontab(hour=SCORE_BATCH_HOUR, minute=SCORE_BATCH_MINUTE),
    },

    # Score pending applications (deferred from when talents applied)
    # Runs at the same time as nightly batch
    "score-pending-applications": {
        "task": "matching_engine.score_pending_applications",
        "schedule": crontab(hour=SCORE_BATCH_HOUR, minute=SCORE_BATCH_MINUTE),
    },
}
