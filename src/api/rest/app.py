import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI

from src.api.middleware import setup_cors
from src.api.rest.routers.health_routers import router as health_router
from src.api.rest.routers import (
    audit_routers,
    command_center_routers,
    document_routers,
    document_workflow_routers,
    email_draft_routers,
    email_polling_routers,
    follow_up_routers,
    goods_receipt_routers,
    invoice_routers,
    invoice_supporting_document_routers,
    issue_routers,
    notification_routers,
    purchase_order_routers,
    user_routers,
    vendor_routers,
)
from src.services.docling_model_services import docling_models_available
from src.services.llm_pipeline_services import validate_llm_provider_config
from src.services.runtime_config_services import (
    warn_if_container_minio_endpoint_is_localhost,
    warn_if_container_redis_url_is_localhost,
)
from src.services.tolerance_config_services import validate_tolerance_config

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    warn_if_container_redis_url_is_localhost()
    warn_if_container_minio_endpoint_is_localhost()
    validate_llm_provider_config()
    validate_tolerance_config()
    logger.info(
        "Docling OCR/layout models available: %s",
        "yes" if docling_models_available() else "no",
    )
    yield


app = FastAPI(
    title="GenWorx AP Core Service",
    version="0.1.0",
    description="Core API for GenWorx AP Assistant",
    lifespan=lifespan,
)

# Routes
app.include_router(health_router)
app.include_router(vendor_routers.router)
app.include_router(document_routers.router)
app.include_router(email_polling_routers.router)
app.include_router(email_polling_routers.email_router)
app.include_router(email_draft_routers.router)
app.include_router(document_workflow_routers.router)
app.include_router(invoice_routers.router)
app.include_router(command_center_routers.router)
app.include_router(follow_up_routers.router)
app.include_router(user_routers.router)
app.include_router(issue_routers.router)
app.include_router(audit_routers.router)
app.include_router(invoice_supporting_document_routers.router)
app.include_router(purchase_order_routers.router)
if os.getenv("ENABLE_GOODS_RECEIPT", "true").casefold() in {"1", "true", "yes", "on"}:
    app.include_router(goods_receipt_routers.router)
app.include_router(notification_routers.router)

# Middleware
setup_cors(app)
