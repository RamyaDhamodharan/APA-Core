"""
Application module for vendor resolution services.
Flow: normalize extracted vendor names, detect collisions, resolve active vendors, and suggest fuzzy candidates.
Called by: document_workflow_services, invoice_matching_services, vendor_routers.
Calls: vendor_models.
Tables: vendors, vendor_aliases.
"""
import re
import uuid
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.models.vendor_models import Vendor, VendorAlias

# Precompile regex patterns once at module level for high-throughput string normalization
_NON_ALPHANUM_SPACE = re.compile(r"[^a-z0-9\s]")
_MULTI_SPACE = re.compile(r"\s+")
_SUFFIX_PATTERN = re.compile(r"\s+(pvt|private|ltd|limited|llp|inc|co)\.?$")
_NON_ALPHANUM = re.compile(r"[^a-z0-9]")


def normalize_vendor_name(value: object) -> str:
    """
    Normalize extracted vendor text into a clean canonical comparison string.
    Inputs: value (object) - raw vendor name from document or user input.
    Output: str - normalized lowercase name stripped of punctuation and legal suffixes.
    Effects: pure function, no database operations.
    Results: consistent string representation for exact and fuzzy matching.
    """
    if value is None:
        return ""
    # Step 1: Strip non-alphanumeric characters (replace with spaces) and convert to lowercase
    normalized = _NON_ALPHANUM_SPACE.sub(" ", str(value).casefold())
    # Step 2: Collapse multiple whitespace characters into a single space
    normalized = _MULTI_SPACE.sub(" ", normalized).strip()
    # Step 3: Strip standard legal entity suffixes iteratively (e.g., 'Pvt Ltd', 'LLP', 'Inc')
    while _SUFFIX_PATTERN.search(normalized):
        normalized = _SUFFIX_PATTERN.sub("", normalized).strip()
    return normalized


def _normalized_name_expression(column: Any) -> Any:
    """
    Build a database SQL expression for matching trimmed and whitespace-collapsed names.
    Inputs: column (Any) - SQLAlchemy column expression.
    Output: Any - SQL expression.
    Effects: used in SQL query construction.
    Results: database-level normalized name comparison.
    """
    return func.lower(func.regexp_replace(func.trim(column), r"\s+", " ", "g"))


def find_canonical_name_conflict(
    db: Session,
    name: str,
    *,
    exclude_vendor_id: uuid.UUID | None = None,
) -> Vendor | None:
    """
    Check if a normalized name conflicts with an existing active vendor's canonical name.
    Inputs: db (Session), name (str), exclude_vendor_id (UUID | None).
    Output: Vendor | None - conflicting active vendor if found, else None.
    Effects: reads database rows from 'vendors' table.
    Results: detects naming conflicts during vendor creation or update.
    """
    # Step 1: Normalize incoming name
    normalized_name = normalize_vendor_name(name)
    if not normalized_name:
        return None
    # Step 2: Query active vendors matching the normalized name
    filters = [_normalized_name_expression(Vendor.name) == normalized_name]
    if exclude_vendor_id is not None:
        filters.append(Vendor.id != exclude_vendor_id)
    return db.query(Vendor).filter(*filters).first()


def find_active_alias_conflict(
    db: Session,
    name: str,
    *,
    exclude_vendor_id: uuid.UUID | None = None,
) -> VendorAlias | None:
    """
    Check if a normalized name conflicts with an active vendor alias.
    Inputs: db (Session), name (str), exclude_vendor_id (UUID | None).
    Output: VendorAlias | None - conflicting alias record if found, else None.
    Effects: reads database rows from 'vendor_aliases' table.
    Results: prevents duplicate active aliases across vendors.
    """
    # Step 1: Normalize incoming alias name
    normalized_name = normalize_vendor_name(name)
    if not normalized_name:
        return None
    # Step 2: Filter active aliases matching normalized string
    filters = [
        VendorAlias.alias_norm == normalized_name,
        VendorAlias.is_active.is_(True),
    ]
    if exclude_vendor_id is not None:
        filters.append(VendorAlias.vendor_id != exclude_vendor_id)
    return db.query(VendorAlias).filter(*filters).first()


def resolve_vendor(
    db: Session,
    name: object,
    *,
    tax_id: object = None,
) -> Vendor | None:
    """
    Resolve an extracted vendor name or tax ID to a single active canonical Vendor.
    Inputs: db (Session), name (object), tax_id (object | None).
    Output: Vendor | None - resolved Vendor if unique match found, else None.
    Effects: reads database rows from 'vendors' and 'vendor_aliases'.
    Results: deterministic vendor identification across invoices and purchase orders.
    """
    # Step 1: Prioritize exact Tax ID resolution (unambiguous government tax identifier)
    normalized_tax_id = _NON_ALPHANUM.sub("", str(tax_id or "").casefold())
    if normalized_tax_id:
        matches = (
            db.query(Vendor)
            .filter(
                Vendor.is_active.is_(True),
                func.lower(func.regexp_replace(Vendor.tax_id, r"[^[:alnum:]]", "", "g"))
                == normalized_tax_id,
            )
            .all()
        )
        # Exactly one matching active vendor with this tax ID -> resolved!
        if len(matches) == 1:
            return matches[0]
        # Ambiguous tax ID (multiple hits) -> treat as unresolved for safety
        if matches:
            return None
        return None

    # Step 2: Fall back to normalized name resolution
    normalized_name = normalize_vendor_name(name)
    if not normalized_name:
        return None

    # Step 3: Query aliases first (an explicitly verified alias takes precedence over canonical name collisions)
    alias_matches = (
        db.query(VendorAlias, Vendor)
        .join(Vendor, VendorAlias.vendor_id == Vendor.id)
        .filter(
            VendorAlias.alias_norm == normalized_name,
            VendorAlias.is_active.is_(True),
            Vendor.is_active.is_(True),
        )
        .all()
    )
    vendor_ids = {vendor.id for _, vendor in alias_matches}
    if len(vendor_ids) == 1:
        return alias_matches[0][1]
    if len(vendor_ids) > 1:
        return None

    # Step 4: Check canonical vendor names
    canonical_rows = db.query(Vendor).filter(Vendor.is_active.is_(True)).all()
    canonical_vendors = [
        vendor
        for vendor in canonical_rows
        if not hasattr(vendor, "name")
        or normalize_vendor_name(vendor.name) == normalized_name
    ]
    return canonical_vendors[0] if len(canonical_vendors) == 1 else None


def suggest_vendor_candidates(
    db: Session,
    name: object,
    *,
    threshold: float = 0.90,
) -> list[Vendor]:
    """
    Return high-similarity active vendors using fuzzy sequence matching without linking.
    Inputs: db (Session), name (object), threshold (float, default 0.90).
    Output: list[Vendor] - sorted list of high-similarity active vendors.
    Effects: reads active vendor records from database.
    Results: powers the UI suggestion card for unknown vendor resolution.
    """
    # Step 1: Normalize query name
    normalized = normalize_vendor_name(name)
    if not normalized:
        return []
    # Step 2: Compare against active vendors using SequenceMatcher
    candidates: list[tuple[float, Vendor]] = []
    for vendor in db.query(Vendor).filter(Vendor.is_active.is_(True)).all():
        score = SequenceMatcher(
            None, normalized, normalize_vendor_name(vendor.name)
        ).ratio()
        if score >= threshold:
            candidates.append((score, vendor))
    # Step 3: Sort candidates descending by match confidence score
    return [
        vendor
        for _, vendor in sorted(candidates, key=lambda item: item[0], reverse=True)
    ]
