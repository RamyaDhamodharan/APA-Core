"""Idempotently persist extracted PO and goods receipt records for review."""

import logging
import traceback
import uuid

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.models.audit_models import AuditLog
from src.models.document_models import Document
from src.models.goods_receipt_models import GoodsReceipt
from src.models.purchase_order_models import PurchaseOrder
from src.services.llm_pipeline_services import validate_extracted_data
from src.services.vendor_resolution_services import resolve_vendor

logger = logging.getLogger(__name__)


def _same_number(column, value: str):
    return func.lower(func.trim(column)) == value.strip().lower()


def backfill_procurement_documents(
    db: Session, actor_id: uuid.UUID | None = None
) -> dict:
    from src.api.rest.routers.document_routers import (
        _document_failure_message,
        _find_duplicate_purchase_order,
        _purchase_order_number,
        _purchase_order_persistence_issues,
        _save_goods_receipt_from_extraction,
        _save_purchase_order_from_extraction,
    )

    summary = {
        "scanned": 0,
        "created": 0,
        "already_present": 0,
        "missing_identity": 0,
        "duplicates": 0,
        "failed": 0,
    }
    documents = (
        db.query(Document)
        .filter(
            Document.status == "needs_data_review",
            Document.doc_type.in_({"po", "goods_receipt"}),
            Document.extracted_json.is_not(None),
        )
        .order_by(Document.created_at.asc(), Document.id.asc())
        .with_for_update(skip_locked=True)
        .all()
    )
    for document in documents:
        summary["scanned"] += 1
        data = document.extracted_json
        if not isinstance(data, dict):
            summary["failed"] += 1
            continue
        try:
            validation_issues = validate_extracted_data(data, document.doc_type)
            raw_vendor = str(
                data.get("vendor") or data.get("vendor_name") or ""
            ).strip()
            tax_id = str(data.get("vendor_tax_id") or data.get("tax_id") or "").strip()
            vendor = resolve_vendor(db, raw_vendor or None, tax_id=tax_id or None)
            if document.doc_type == "po":
                po_number = _purchase_order_number(data)
                if not po_number or not (raw_vendor or tax_id):
                    summary["missing_identity"] += 1
                    document.failure_message = _document_failure_message(
                        ValueError(
                            "Missing required field: "
                            + ("order_number" if not po_number else "vendor")
                        )
                    )
                    db.commit()
                    continue
                validation_issues.extend(_purchase_order_persistence_issues(data))
                if (raw_vendor or tax_id) and vendor is None:
                    validation_issues.append(f"New vendor: {raw_vendor or tax_id}")
                existing = (
                    db.query(PurchaseOrder)
                    .filter(PurchaseOrder.source_document_id == document.id)
                    .first()
                )
                if existing is None:
                    existing = (
                        db.query(PurchaseOrder)
                        .filter(
                            PurchaseOrder.source_document_id.is_(None),
                            _same_number(PurchaseOrder.po_number, po_number),
                        )
                        .order_by(
                            PurchaseOrder.created_at.asc(), PurchaseOrder.id.asc()
                        )
                        .first()
                    )
                    if existing is not None:
                        existing.source_document_id = document.id
                        existing.created_by = getattr(
                            existing, "created_by", None
                        ) or getattr(document, "created_by", None)
                        existing.updated_by = getattr(document, "created_by", None)
                        if (
                            getattr(existing, "vendor_id", None) is None
                            and vendor is not None
                        ):
                            existing.vendor_id = vendor.id
                duplicate = _find_duplicate_purchase_order(
                    db, po_number, exclude_document_id=document.id
                )
                if existing is not None:
                    existing.field_confidence = data.get("field_confidence") or getattr(
                        existing, "field_confidence", None
                    )
                    existing.needs_verification = bool(validation_issues) or getattr(
                        existing, "needs_verification", False
                    )
                    summary["already_present"] += 1
                elif duplicate is not None:
                    summary["duplicates"] += 1
                    document.failure_message = (
                        f"Purchase order number {po_number} already exists."
                    )
                else:
                    _save_purchase_order_from_extraction(
                        db,
                        document,
                        data,
                        vendor,
                        validation_issues=validation_issues,
                    )
                    summary["created"] += 1
            else:
                gr_number = str(
                    data.get("gr_number")
                    or data.get("receipt_number")
                    or data.get("goods_receipt_number")
                    or ""
                ).strip()
                if not gr_number:
                    summary["missing_identity"] += 1
                    document.failure_message = _document_failure_message(
                        ValueError("Missing required field: gr_number")
                    )
                    db.commit()
                    continue
                existing = (
                    db.query(GoodsReceipt)
                    .filter(GoodsReceipt.source_document_id == document.id)
                    .first()
                )
                if existing is None:
                    existing = (
                        db.query(GoodsReceipt)
                        .filter(
                            GoodsReceipt.source_document_id.is_(None),
                            _same_number(GoodsReceipt.gr_number, gr_number),
                        )
                        .order_by(GoodsReceipt.created_at.asc(), GoodsReceipt.id.asc())
                        .first()
                    )
                    if existing is not None:
                        existing.source_document_id = document.id
                        existing.created_by = getattr(
                            existing, "created_by", None
                        ) or getattr(document, "created_by", None)
                        existing.updated_by = getattr(document, "created_by", None)
                        if (
                            getattr(existing, "vendor_id", None) is None
                            and vendor is not None
                        ):
                            existing.vendor_id = vendor.id
                if existing is not None:
                    existing.field_confidence = data.get("field_confidence") or getattr(
                        existing, "field_confidence", None
                    )
                    existing.needs_verification = bool(validation_issues) or getattr(
                        existing, "needs_verification", False
                    )
                    summary["already_present"] += 1
                else:
                    _save_goods_receipt_from_extraction(
                        db,
                        document,
                        data,
                        vendor,
                        validation_issues=validation_issues,
                    )
                    summary["created"] += 1
            document.status = "needs_data_review" if validation_issues else "extracted"
            document.failure_message = (
                _document_failure_message(ValueError("; ".join(validation_issues)))
                if validation_issues
                else None
            )
            db.commit()
        except Exception as exc:
            logger.warning(
                "Procurement document backfill failed for %s (%s in %s)",
                document.id,
                type(exc).__name__,
                traceback.extract_tb(exc.__traceback__)[-1].name,
            )
            db.rollback()
            current = db.query(Document).filter(Document.id == document.id).first()
            if current is not None:
                current.status = "needs_data_review"
                current.failure_message = (
                    "The extracted record could not be saved. Review its required "
                    "fields and retry."
                )
                db.commit()
            summary["failed"] += 1

    db.add(
        AuditLog(
            action="procurement_document_backfill",
            entity_type="system",
            result="error" if summary["failed"] else "success",
            message="Needs-review PO/GR documents were backfilled",
            output_summary=summary,
            created_by=actor_id,
        )
    )
    db.commit()
    return summary
