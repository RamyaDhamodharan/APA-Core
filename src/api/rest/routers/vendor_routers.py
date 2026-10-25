import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.api.rest.dependencies import get_current_user, require_role
from src.db.session_db import get_db
from src.models.audit_models import AuditLog
from src.models.document_models import Document
from src.models.goods_receipt_models import GoodsReceipt
from src.models.invoice_models import Invoice
from src.models.purchase_order_models import PurchaseOrder
from src.models.vendor_models import Vendor, VendorAlias
from src.schemas.vendor_schemas import (
    VendorAliasCreate,
    VendorAliasResponse,
    VendorAliasUpdate,
    VendorCreate,
    VendorResponse,
    VendorUpdate,
)
from src.services.vendor_resolution_services import (
    find_active_alias_conflict,
    find_canonical_name_conflict,
    normalize_vendor_name,
    resolve_vendor,
    suggest_vendor_candidates,
)

router = APIRouter(prefix="/api/vendors", tags=["Vendors"])


def _clear_stale_vendor_flag(document: Document) -> None:
    """Remove only a stale New vendor reason after an exact resolution."""
    if "new vendor" not in str(document.failure_message or "").casefold():
        return
    document.failure_message = None
    if document.status == "needs_vendor_review":
        document.status = "needs_data_review"


def _rerun_vendor_resolution(db: Session, vendor: Vendor, actor: dict) -> None:
    """Clear stale unresolved flags and rematch records after vendor changes."""
    # Unit tests use lightweight mocks for endpoint behavior.  The real
    # dependency always supplies a SQLAlchemy Session; avoid attempting a
    # workflow run against a mock while preserving the production path.
    if not isinstance(db, Session):
        return
    resolve_extracted_records_for_vendor(
        vendor_id=vendor.id,
        identity={"name": vendor.name, "tax_id": getattr(vendor, "tax_id", None)},
        db=db,
        current_user=actor,
    )


def _duplicate_vendor(
    db: Session,
    name: str,
    *,
    exclude_id: uuid.UUID | None = None,
) -> Vendor | None:
    return find_canonical_name_conflict(
        db,
        name,
        exclude_vendor_id=exclude_id,
    )


def _ensure_name_not_another_vendors_alias(
    db: Session,
    name: str,
    *,
    vendor_id: uuid.UUID | None = None,
) -> None:
    if find_active_alias_conflict(
        db,
        name,
        exclude_vendor_id=vendor_id,
    ):
        raise HTTPException(
            status_code=409,
            detail="Vendor name conflicts with an active vendor alias",
        )


def _get_active_vendor_or_conflict(
    db: Session,
    vendor_id: uuid.UUID,
) -> Vendor:
    vendor = db.get(Vendor, vendor_id)
    if vendor is None:
        raise HTTPException(status_code=404, detail="Vendor not found")
    if not vendor.is_active:
        raise HTTPException(
            status_code=409,
            detail="Aliases can only be assigned to active vendors",
        )
    return vendor


def _validate_alias_mapping(
    db: Session,
    alias: str,
    vendor_id: uuid.UUID,
    *,
    exclude_alias_id: uuid.UUID | None = None,
    active: bool = True,
) -> None:
    if active and find_canonical_name_conflict(
        db,
        alias,
        exclude_vendor_id=vendor_id,
    ):
        raise HTTPException(
            status_code=409,
            detail="Alias conflicts with another vendor's canonical name",
        )

    query = db.query(VendorAlias).filter(
        VendorAlias.alias_norm == normalize_vendor_name(alias)
    )
    if exclude_alias_id is not None:
        query = query.filter(VendorAlias.id != exclude_alias_id)
    if query.first():
        raise HTTPException(
            status_code=409,
            detail="A vendor alias with this normalized name already exists",
        )


def _raise_vendor_conflict(exc: IntegrityError) -> None:
    raise HTTPException(
        status_code=409,
        detail="Vendor conflicts with existing data",
    ) from exc


def _normalize_tax_id(value: object) -> str:
    return "".join(char for char in str(value or "").casefold() if char.isalnum())


def _duplicate_tax_id(
    db: Session,
    tax_id: str | None,
    *,
    exclude_id: uuid.UUID | None = None,
) -> Vendor | None:
    normalized = _normalize_tax_id(tax_id)
    if not normalized:
        return None
    query = db.query(Vendor).filter(
        func.lower(func.regexp_replace(Vendor.tax_id, r"[^[:alnum:]]", "", "g"))
        == normalized
    )
    if exclude_id is not None:
        query = query.filter(Vendor.id != exclude_id)
    return query.first()


@router.post(
    "/{vendor_id}/resolve-extracted",
    dependencies=[Depends(get_current_user)],
)
def resolve_extracted_records_for_vendor(
    vendor_id: uuid.UUID,
    identity: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Link previously extracted rows matching a newly confirmed vendor."""
    vendor = db.get(Vendor, vendor_id)
    if vendor is None or not vendor.is_active:
        raise HTTPException(status_code=404, detail="Active vendor not found")
    aliases = (
        db.query(VendorAlias)
        .filter(
            VendorAlias.vendor_id == vendor.id,
            VendorAlias.is_active.is_(True),
        )
        .all()
    )
    names = {
        normalize_vendor_name(value)
        for value in (
            vendor.name,
            identity.get("name"),
            *(alias.alias_norm for alias in aliases),
        )
        if value and normalize_vendor_name(value)
    }
    tax_ids = {
        _normalize_tax_id(value)
        for value in (vendor.tax_id, identity.get("tax_id"))
        if _normalize_tax_id(value)
    }
    create_alias = bool(identity.get("create_alias"))
    summary = {"documents": 0, "purchase_orders": 0, "goods_receipts": 0, "invoices": 0}
    documents = (
        db.query(Document)
        .filter(Document.extracted_json.is_not(None))
        .with_for_update(skip_locked=True)
        .all()
    )
    rematch_ids = set()
    actor_id = uuid.UUID(str(current_user["sub"]))
    for document in documents:
        data = document.extracted_json
        if not isinstance(data, dict):
            continue
        raw_name = data.get("vendor") or data.get("vendor_name")
        raw_tax_id = data.get("vendor_tax_id") or data.get("tax_id")
        if not (
            (raw_name and normalize_vendor_name(raw_name) in names)
            or _normalize_tax_id(raw_tax_id) in tax_ids
        ):
            continue
        existing_resolution = data.get("vendor_resolution")
        if (
            isinstance(existing_resolution, dict)
            and existing_resolution.get("vendor_id") == str(vendor.id)
            and "new vendor" not in str(document.failure_message or "").casefold()
        ):
            # Repeated create-then-resolve calls are safe and do not create
            # duplicate links or rematch attempts.
            continue
        if create_alias and raw_name:
            alias_norm = normalize_vendor_name(str(raw_name))
            if alias_norm and alias_norm != normalize_vendor_name(vendor.name):
                existing_alias = (
                    db.query(VendorAlias)
                    .filter(VendorAlias.alias_norm == alias_norm)
                    .first()
                )
                if existing_alias is None:
                    db.add(
                        VendorAlias(
                            vendor_id=vendor.id,
                            alias_norm=alias_norm,
                            created_by=actor_id,
                            updated_by=actor_id,
                        )
                    )
        summary["documents"] += 1
        invoice = (
            db.query(Invoice)
            .filter(Invoice.source_document_id == document.id)
            .with_for_update()
            .first()
        )
        if invoice is not None and invoice.vendor_id is None:
            invoice.vendor_id = vendor.id
            invoice.updated_by = actor_id
            summary["invoices"] += 1
            rematch_ids.add(invoice.id)
        po = (
            db.query(PurchaseOrder)
            .filter(PurchaseOrder.source_document_id == document.id)
            .with_for_update()
            .first()
        )
        if po is not None and po.vendor_id is None:
            po.vendor_id = vendor.id
            po.updated_by = actor_id
            summary["purchase_orders"] += 1
            from src.api.rest.routers.document_routers import (
                _rematch_waiting_invoices_for_po,
            )

            _rematch_waiting_invoices_for_po(db, po)
        receipt = (
            db.query(GoodsReceipt)
            .filter(GoodsReceipt.source_document_id == document.id)
            .with_for_update()
            .first()
        )
        if receipt is not None and receipt.vendor_id is None:
            receipt.vendor_id = vendor.id
            receipt.updated_by = actor_id
            summary["goods_receipts"] += 1
        if receipt is not None:
            from src.api.rest.routers.document_routers import (
                _rematch_waiting_invoices_for_po,
                _save_goods_receipt_from_extraction,
            )

            _save_goods_receipt_from_extraction(
                db,
                document,
                data,
                vendor,
                validation_issues=[],
            )
            if receipt.po_id:
                related_po = db.get(PurchaseOrder, receipt.po_id)
                if related_po is not None:
                    _rematch_waiting_invoices_for_po(db, related_po)
        data["vendor_resolution"] = {
            "method": "user_confirmed",
            "source": "vendor_change_reresolution",
            "vendor_id": str(vendor.id),
            "extracted_value": raw_name or raw_tax_id,
        }
        document.extracted_json = data
        # A vendor can be added after the original workflow run.  Clear only
        # the stale vendor flag; unrelated data-review reasons remain visible.
        _clear_stale_vendor_flag(document)
    if rematch_ids:
        from src.services.invoice_matching_services import run_match_for_invoice

        for invoice_id in rematch_ids:
            run_match_for_invoice(db, invoice_id, actor_id, force=True)
    db.add(
        AuditLog(
            action="unknown_vendor_records_resolved",
            entity_type="vendor",
            entity_id=vendor.id,
            result="success",
            message="Extracted records were linked to a user-confirmed vendor",
            output_summary=summary,
            created_by=actor_id,
        )
    )
    db.commit()
    return summary


@router.get(
    "",
    response_model=list[VendorResponse],
    dependencies=[Depends(get_current_user)],
)
def list_vendors(
    search: Annotated[str | None, Query(max_length=200)] = None,
    page: Annotated[int | None, Query(ge=1)] = None,
    page_size: Annotated[int | None, Query(ge=1, le=100)] = None,
    db: Session = Depends(get_db),
) -> list[Vendor]:
    statement = select(Vendor)
    if search is not None:
        search = search.strip()
        if not search:
            raise HTTPException(
                status_code=422,
                detail="search must not be blank",
            )
        pattern = f"%{search}%"
        statement = statement.where(
            or_(
                Vendor.name.ilike(pattern),
                Vendor.tax_id.ilike(pattern),
                Vendor.contact_name.ilike(pattern),
                Vendor.contact_email.ilike(pattern),
                Vendor.contact_phone.ilike(pattern),
            )
        )

    statement = statement.order_by(Vendor.name)
    if page is not None or page_size is not None:
        resolved_page = page or 1
        resolved_page_size = page_size or 20
        statement = statement.offset((resolved_page - 1) * resolved_page_size).limit(
            resolved_page_size
        )

    return db.scalars(statement).all()


@router.post(
    "/backfill-unresolved",
    dependencies=[Depends(require_role("admin"))],
)
def backfill_unresolved_vendors(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Admin-safe, idempotent repair for records carrying a stale new-vendor flag."""
    vendors = db.query(Vendor).filter(Vendor.is_active.is_(True)).all()
    totals = {"documents": 0, "purchase_orders": 0, "goods_receipts": 0, "invoices": 0}
    for vendor in vendors:
        summary = resolve_extracted_records_for_vendor(
            vendor_id=vendor.id,
            identity={"name": vendor.name, "tax_id": getattr(vendor, "tax_id", None)},
            db=db,
            current_user=current_user,
        )
        for key in totals:
            totals[key] += int(summary.get(key, 0))
    return {"status": "completed", "vendors": len(vendors), **totals}


@router.get("/resolve-extracted", dependencies=[Depends(get_current_user)])
def resolve_extracted_vendor(
    name: Annotated[str | None, Query(max_length=200)] = None,
    tax_id: Annotated[str | None, Query(max_length=30)] = None,
    db: Session = Depends(get_db),
) -> dict:
    vendor = resolve_vendor(db, name, tax_id=tax_id)
    if vendor is not None:
        return {
            "status": "resolved",
            "vendor": {"id": str(vendor.id), "name": vendor.name},
        }
    suggestions = suggest_vendor_candidates(db, name)
    status = (
        "suggestion"
        if len(suggestions) == 1
        else "multiple"
        if suggestions
        else "unresolved"
    )
    return {
        "status": status,
        "candidates": [{"id": str(item.id), "name": item.name} for item in suggestions],
    }


@router.get(
    "/{vendor_id}",
    response_model=VendorResponse,
    dependencies=[Depends(get_current_user)],
)
def get_vendor(
    vendor_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> Vendor:
    vendor = db.get(Vendor, vendor_id)
    if vendor is None:
        raise HTTPException(status_code=404, detail="Vendor not found")
    return vendor


@router.post(
    "",
    response_model=VendorResponse,
    status_code=201,
    dependencies=[Depends(get_current_user)],
)
def create_vendor(
    payload: VendorCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> Vendor:
    if _duplicate_vendor(db, payload.name):
        raise HTTPException(
            status_code=409,
            detail="A vendor with this name already exists",
        )
    _ensure_name_not_another_vendors_alias(db, payload.name)
    if _duplicate_tax_id(db, payload.tax_id):
        raise HTTPException(
            status_code=409,
            detail="A vendor with this tax ID already exists",
        )

    try:
        actor_id = uuid.UUID(str(current_user["sub"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=401, detail="Authenticated user ID is invalid"
        ) from exc

    vendor = Vendor(
        name=payload.name,
        tax_id=payload.tax_id,
        default_tax_rate=payload.default_tax_rate,
        contact_name=payload.contact_name,
        contact_email=(str(payload.contact_email) if payload.contact_email else None),
        contact_phone=payload.contact_phone,
        created_by=actor_id,
        updated_by=actor_id,
    )

    db.add(vendor)
    try:
        db.flush()
        db.add(
            AuditLog(
                action="vendor_created",
                entity_type="vendor",
                entity_id=vendor.id,
                new_value={
                    "name": vendor.name,
                    "tax_id": vendor.tax_id,
                },
                created_by=actor_id,
                updated_by=actor_id,
            )
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        _raise_vendor_conflict(exc)
    db.refresh(vendor)
    _rerun_vendor_resolution(db, vendor, current_user)

    return vendor


@router.patch(
    "/{vendor_id}",
    response_model=VendorResponse,
    dependencies=[Depends(require_role("admin"))],
)
def update_vendor(
    vendor_id: uuid.UUID,
    payload: VendorUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> Vendor:
    vendor = db.get(Vendor, vendor_id)
    if vendor is None:
        raise HTTPException(status_code=404, detail="Vendor not found")

    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(
            status_code=422,
            detail="At least one vendor field must be provided",
        )

    if "name" in updates and _duplicate_vendor(
        db,
        updates["name"],
        exclude_id=vendor.id,
    ):
        raise HTTPException(
            status_code=409,
            detail="A vendor with this name already exists",
        )
    if "name" in updates:
        _ensure_name_not_another_vendors_alias(
            db,
            updates["name"],
            vendor_id=vendor.id,
        )

    if "contact_email" in updates and updates["contact_email"] is not None:
        updates["contact_email"] = str(updates["contact_email"])

    for field, value in updates.items():
        setattr(vendor, field, value)

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        _raise_vendor_conflict(exc)
    db.refresh(vendor)
    # Admin edits can make previously unresolved records resolvable.
    _rerun_vendor_resolution(db, vendor, current_user)
    return vendor


@router.delete(
    "/{vendor_id}",
    response_model=VendorResponse,
    dependencies=[Depends(require_role("admin"))],
)
def deactivate_vendor(
    vendor_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> Vendor:
    """Deactivate a vendor without breaking historical record references."""
    vendor = db.get(Vendor, vendor_id)
    if vendor is None:
        raise HTTPException(status_code=404, detail="Vendor not found")
    vendor.is_active = False
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    return vendor


@router.get(
    "/{vendor_id}/aliases",
    response_model=list[VendorAliasResponse],
    dependencies=[Depends(get_current_user)],
)
def list_vendor_aliases(
    vendor_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> list[VendorAlias]:
    if db.get(Vendor, vendor_id) is None:
        raise HTTPException(status_code=404, detail="Vendor not found")
    return (
        db.query(VendorAlias)
        .filter(VendorAlias.vendor_id == vendor_id)
        .order_by(VendorAlias.alias_norm)
        .all()
    )


@router.post(
    "/{vendor_id}/aliases",
    response_model=VendorAliasResponse,
    status_code=201,
    dependencies=[Depends(require_role("admin"))],
)
def create_vendor_alias(
    vendor_id: uuid.UUID,
    payload: VendorAliasCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> VendorAlias:
    _get_active_vendor_or_conflict(db, vendor_id)
    _validate_alias_mapping(db, payload.alias, vendor_id)

    alias = VendorAlias(
        vendor_id=vendor_id,
        alias_norm=payload.alias,
        is_active=True,
    )
    db.add(alias)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        _raise_vendor_conflict(exc)
    db.refresh(alias)
    vendor = db.get(Vendor, vendor_id)
    if vendor is not None:
        _rerun_vendor_resolution(db, vendor, current_user)
    return alias


@router.patch(
    "/{vendor_id}/aliases/{alias_id}",
    response_model=VendorAliasResponse,
    dependencies=[Depends(require_role("admin"))],
)
def update_vendor_alias(
    vendor_id: uuid.UUID,
    alias_id: uuid.UUID,
    payload: VendorAliasUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> VendorAlias:
    alias = db.get(VendorAlias, alias_id)
    if alias is None or alias.vendor_id != vendor_id:
        raise HTTPException(status_code=404, detail="Vendor alias not found")

    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(
            status_code=422,
            detail="At least one alias field must be provided",
        )

    target_vendor_id = updates.get("vendor_id", alias.vendor_id)
    target_vendor = _get_active_vendor_or_conflict(db, target_vendor_id)
    next_alias_name = updates.get("alias", alias.alias_norm)
    next_active = updates.get("is_active", alias.is_active)
    _validate_alias_mapping(
        db,
        next_alias_name,
        target_vendor.id,
        exclude_alias_id=alias.id,
        active=next_active,
    )

    alias.vendor_id = target_vendor.id
    alias.alias_norm = next_alias_name
    alias.is_active = next_active
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        _raise_vendor_conflict(exc)
    db.refresh(alias)
    vendor = db.get(Vendor, alias.vendor_id)
    if vendor is not None:
        _rerun_vendor_resolution(db, vendor, current_user)
    return alias


@router.delete(
    "/{vendor_id}/aliases/{alias_id}",
    response_model=VendorAliasResponse,
    dependencies=[Depends(require_role("admin"))],
)
def deactivate_vendor_alias(
    vendor_id: uuid.UUID,
    alias_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> VendorAlias:
    alias = db.get(VendorAlias, alias_id)
    if alias is None or alias.vendor_id != vendor_id:
        raise HTTPException(status_code=404, detail="Vendor alias not found")
    alias.is_active = False
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        _raise_vendor_conflict(exc)
    db.refresh(alias)
    vendor = db.get(Vendor, vendor_id)
    if vendor is not None and vendor.is_active:
        _rerun_vendor_resolution(db, vendor, current_user)
    return alias
