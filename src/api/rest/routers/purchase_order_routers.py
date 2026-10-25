import uuid
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select, true
from sqlalchemy.orm import Session, selectinload

from src.api.rest.dependencies import get_current_user, require_role
from src.db.session_db import get_db
from src.models.audit_models import AuditLog
from src.models.document_models import Document
from src.models.purchase_order_models import PurchaseOrder
from src.models.vendor_models import Vendor
from src.schemas.purchase_order_schemas import (
    PurchaseOrderListResponse,
    PurchaseOrderResponse,
    PurchaseOrderVendorReassignmentRequest,
)

router = APIRouter(
    prefix="/api/purchase-orders",
    tags=["Purchase Orders"],
    dependencies=[Depends(get_current_user)],
)


def _query():
    return select(PurchaseOrder).options(
        selectinload(PurchaseOrder.lines), selectinload(PurchaseOrder.vendor)
    )


def _owner_filter(owner_id: uuid.UUID, current_user: dict | None = None):
    if current_user and current_user.get("role") == "admin":
        return true()
    return or_(
        PurchaseOrder.created_by == owner_id,
        PurchaseOrder.source_document_id.in_(
            select(Document.id).where(
                or_(
                    Document.created_by == owner_id,
                    Document.source.in_(["email", "email_poll"]),
                )
            )
        ),
    )


@router.get("", response_model=PurchaseOrderListResponse)
def list_purchase_orders(
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    vendor_id: uuid.UUID | None = None,
    source_document_id: uuid.UUID | None = None,
    search: Annotated[str | None, Query(max_length=200)] = None,
    current_only: bool = False,
    # Retained for compatibility with older clients; PO confidence is not a filter.
    status: Literal["all", "needs_verification", "verified"] = "all",
    date_from: date | None = None,
    date_to: date | None = None,
    sort_by: Annotated[
        str, Query(pattern="^(po_date|po_number|created_at)$")
    ] = "po_date",
    sort_dir: Annotated[str, Query(pattern="^(asc|desc)$")] = "desc",
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> PurchaseOrderListResponse:
    try:
        owner_id = uuid.UUID(str(current_user["sub"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=401, detail="Authenticated user ID is invalid"
        ) from exc
    try:
        from src.services.procurement_backfill_services import (
            backfill_procurement_documents,
        )

        backfill_procurement_documents(db, owner_id)
    except Exception:
        db.rollback()
    filters = [_owner_filter(owner_id, current_user)]
    if current_only:
        filters.append(PurchaseOrder.is_current.is_(True))
    if vendor_id is not None:
        filters.append(PurchaseOrder.vendor_id == vendor_id)
    if source_document_id is not None:
        filters.append(PurchaseOrder.source_document_id == source_document_id)
    if search:
        filters.append(PurchaseOrder.po_number.ilike(f"%{search.strip()}%"))
    if date_from:
        filters.append(PurchaseOrder.po_date >= date_from)
    if date_to:
        filters.append(PurchaseOrder.po_date <= date_to)
    total = db.scalar(select(func.count(PurchaseOrder.id)).where(*filters)) or 0
    count_filters = [_owner_filter(owner_id, current_user)]
    if current_only:
        count_filters.append(PurchaseOrder.is_current.is_(True))
    if vendor_id is not None:
        count_filters.append(PurchaseOrder.vendor_id == vendor_id)
    if source_document_id is not None:
        count_filters.append(PurchaseOrder.source_document_id == source_document_id)
    if search:
        count_filters.append(PurchaseOrder.po_number.ilike(f"%{search.strip()}%"))
    if date_from:
        count_filters.append(PurchaseOrder.po_date >= date_from)
    if date_to:
        count_filters.append(PurchaseOrder.po_date <= date_to)
    counts = {
        "all": db.scalar(select(func.count(PurchaseOrder.id)).where(*count_filters))
        or 0
    }
    sort_column = getattr(PurchaseOrder, sort_by)
    sort_order = sort_column.asc() if sort_dir == "asc" else sort_column.desc()
    orders = db.scalars(
        _query()
        .where(*filters)
        .order_by(sort_order, PurchaseOrder.po_number)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return PurchaseOrderListResponse(
        items=orders, total=total, page=page, page_size=page_size, counts=counts
    )


@router.get("/{po_id}", response_model=PurchaseOrderResponse)
def get_purchase_order(
    po_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    try:
        owner_id = uuid.UUID(str(current_user["sub"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=401, detail="Authenticated user ID is invalid"
        ) from exc
    order = db.scalar(
        _query().where(PurchaseOrder.id == po_id, _owner_filter(owner_id, current_user))
    )
    if order is None:
        raise HTTPException(status_code=404, detail="Purchase order not found")
    return order


@router.patch(
    "/{po_id}/vendor",
    response_model=PurchaseOrderResponse,
    dependencies=[Depends(require_role("admin"))],
)
def reassign_purchase_order_vendor(
    po_id: uuid.UUID,
    payload: PurchaseOrderVendorReassignmentRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    try:
        owner_id = uuid.UUID(str(current_user["sub"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=401, detail="Authenticated user ID is invalid"
        ) from exc
    order = db.scalar(
        _query()
        .where(PurchaseOrder.id == po_id, _owner_filter(owner_id, current_user))
        .with_for_update()
    )
    if order is None:
        raise HTTPException(status_code=404, detail="Purchase order not found")
    vendor = db.get(Vendor, payload.vendor_id)
    if vendor is None:
        raise HTTPException(status_code=404, detail="Vendor not found")
    if not vendor.is_active:
        raise HTTPException(status_code=409, detail="Vendor is inactive")
    old_vendor_id = order.vendor_id
    order.vendor_id = vendor.id
    db.add(
        AuditLog(
            action="purchase_order_vendor_reassigned",
            entity_type="purchase_order",
            entity_id=order.id,
            old_value={"vendor_id": str(old_vendor_id) if old_vendor_id else None},
            new_value={"vendor_id": str(vendor.id), "vendor_name": vendor.name},
            created_by=owner_id,
            updated_by=owner_id,
        )
    )
    db.commit()
    return db.scalar(
        _query().where(
            PurchaseOrder.id == order.id,
            _owner_filter(owner_id, current_user),
        )
    )
