"""Celery application for background document processing."""

import logging
import os

from celery import Celery
from celery.schedules import schedule
from celery.signals import worker_init

from src.services.docling_model_services import docling_models_available
from src.services.llm_pipeline_services import validate_llm_provider_config
from src.services.runtime_config_services import (
    warn_if_container_minio_endpoint_is_localhost,
    warn_if_container_redis_url_is_localhost,
)
from src.services.tolerance_config_services import validate_tolerance_config

logger = logging.getLogger(__name__)

warn_if_container_redis_url_is_localhost()
warn_if_container_minio_endpoint_is_localhost()
validate_llm_provider_config()
validate_tolerance_config()


@worker_init.connect
def log_docling_model_availability(**_kwargs) -> None:
    logger.info(
        "Docling OCR/layout models available: %s",
        "yes" if docling_models_available() else "no",
    )

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

celery_app = Celery(
    "genworx_ap_core",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=["src.worker.tasks"],
)

celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_default_retry_delay=10,
    broker_connection_retry_on_startup=True,
    task_default_queue="documents",
    task_routes={
        "ap.process_document": {"queue": "documents"},
        "ap.sweep_stuck_documents": {"queue": "documents"},
        "ap.poll_email": {"queue": "email"},
        "ap.retry_email_message": {"queue": "email"},
        "ap.send_notification": {"queue": "email"},
        "ap.mark_overdue": {"queue": "email"},
        "ap.run_follow_ups": {"queue": "email"},
    },
)

# The documents Compose worker uses this environment value with a prefork pool.
# Each child process caches its Docling converter/model once in memory.
WORKER_CONCURRENCY = max(1, int(os.getenv("WORKER_CONCURRENCY", "2")))

celery_app.conf.beat_schedule = {
    "mark-overdue-invoices-hourly": {
        "task": "ap.mark_overdue",
        "schedule": schedule(3600),
    },
    "run-follow-ups-every-15-minutes": {
        "task": "ap.run_follow_ups",
        "schedule": schedule(900),
    },
    "sweep-stuck-documents-every-five-minutes": {
        "task": "ap.sweep_stuck_documents",
        "schedule": schedule(
            max(30, int(os.getenv("DOCUMENT_STUCK_SWEEP_INTERVAL_SECONDS", "300")))
        ),
    },
}

if (
    os.getenv("EMAIL_POLL_ENABLED", "false").lower() == "true"
    and os.getenv("EMAIL_PROVIDER", "imap").lower() == "imap"
):
    celery_app.conf.beat_schedule["poll-ap-email"] = {
        "task": "ap.poll_email",
        "schedule": schedule(float(os.getenv("EMAIL_POLL_INTERVAL_SECONDS", "300"))),
    }
