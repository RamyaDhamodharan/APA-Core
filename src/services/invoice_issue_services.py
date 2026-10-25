"""
Application module for invoice issue synchronization and audit tracking services.
Flow: translate matching check outcomes into persistent InvoiceIssue records, assign owners, record timeline events, and auto-resolve fixed issues.
Called by: invoice_matching_services, issue_routers, celery tasks.
Calls: audit_models, invoice_issue_models.
Tables: invoice_issues, issue_events, audit_logs, users, invoices.
"""
import os
from datetime import UTC, datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.models.audit_models import AuditLog
from src.models.invoice_issue_models import InvoiceIssue, IssueEvent
from src.models.invoice_models import Invoice
from src.models.user_models import User

# Active statuses that indicate an issue remains unresolved
ACTIVE = {"open", "in_progress", "waiting_vendor", "escalated"}

# Canonical mapping from discrepancy token to issue_type identifier and descriptive label
ISSUE_NAMES = {
    "price": ("price_variance", "Invoice price differs from purchase order"),
    "unit_price": ("price_variance", "Invoice price differs from purchase order"),
    "quantity": (
        "quantity_mismatch",
        "Invoice quantity differs from purchase order or receipt",
    ),
    "receipt": ("missing_receipt", "Goods receipt evidence is missing or insufficient"),
    "tax": ("tax_mismatch", "Invoice tax differs from the expected tax"),
    "currency": ("currency_mismatch", "Invoice and purchase order currencies differ"),
    "po": ("missing_po", "No purchase order is linked to this invoice"),
    "vendor": ("unknown_vendor", "The invoice vendor could not be resolved"),
    "duplicate": ("duplicate_invoice", "A duplicate invoice was detected"),
    "confidence": ("low_confidence", "One or more extracted fields need verification"),
    "line_not_on_po": ("line_not_on_po", "An invoice line is not on the PO"),
    "line_total": ("line_total_variance", "An invoice line total does not reconcile"),
}


def _assignment(db: Session, exclude=None):
    """
    Select an active associate to assign as issue owner using the least-open workload strategy.
    Inputs: db (Session), exclude (UUID | None) - optional user ID to exclude from assignment pool.
    Output: UUID | None - ID of chosen associate user, or None if manual strategy is set.
    Effects: queries database for active associates, open issue counts, and active invoice workloads.
    Results: evenly distributes discrepancy review work among available associates.
    """
    # Step 1: Check if automated workload assignment is disabled
    if os.getenv("ASSIGNMENT_STRATEGY", "least_open").casefold() == "manual":
        return None

    # Step 2: Query active associate accounts
    associates = (
        db.query(User).filter(User.role == "associate", User.is_active.is_(True)).all()
    )
    if not associates:
        return None

    # Step 3: Count open issues currently assigned per associate
    counts = dict(
        db.query(InvoiceIssue.owner_user_id, func.count(InvoiceIssue.id))
        .filter(
            InvoiceIssue.owner_user_id.is_not(None), InvoiceIssue.status.in_(ACTIVE)
        )
        .group_by(InvoiceIssue.owner_user_id)
        .all()
    )

    # Step 4: Count unfinalized invoices assigned per associate
    open_invoice_counts = dict(
        db.query(Invoice.owner_user_id, func.count(Invoice.id))
        .filter(
            Invoice.owner_user_id.is_not(None),
            Invoice.workflow_status.in_(
                {
                    "received",
                    "reading",
                    "verifying",
                    "matching",
                    "match_approved",
                    "match_issues",
                    "no_match",
                    "on_hold",
                    "resubmitted",
                }
            ),
        )
        .group_by(Invoice.owner_user_id)
        .all()
    )

    # Step 5: Pick the associate with the lowest combined active workload
    pool = [user for user in associates if user.id != exclude] or associates
    return min(
        pool,
        key=lambda user: (
            counts.get(user.id, 0) + open_invoice_counts.get(user.id, 0),
            str(user.id),
        ),
    ).id


def add_issue_event(
    db: Session, issue: InvoiceIssue, event_type: str, user_id=None, note=None
):
    """
    Record an immutable event and audit entry in the issue timeline.
    Inputs: db (Session), issue (InvoiceIssue), event_type (str), user_id (UUID | None), note (str | None).
    Output: None.
    Effects: inserts rows into issue_events and audit_logs tables.
    Results: builds an auditable history of status changes, notes, and resolutions.
    """
    # Step 1: Add timeline event
    db.add(
        IssueEvent(issue_id=issue.id, event_type=event_type, user_id=user_id, note=note)
    )
    # Step 2: Record formal audit log
    db.add(
        AuditLog(
            action=f"invoice_issue_{event_type}",
            entity_type="invoice_issue",
            entity_id=issue.id,
            old_value=None,
            new_value={
                "status": issue.status,
                "owner_user_id": str(issue.owner_user_id)
                if getattr(issue, "owner_user_id", None)
                else None,
                "note": note,
            },
            result="success",
            message=f"Issue {event_type}",
            created_by=user_id,
        )
    )


def sync_invoice_issues(
    db: Session,
    invoice: Invoice,
    checks: list,
    user_id=None,
    missing_document_types: list[str] | None = None,
):
    """
    Upsert active findings and automatically resolve issues no longer found in the latest match.
    Inputs: db (Session), invoice (Invoice), checks (list[dict]), user_id (UUID | None), missing_document_types (list[str] | None).
    Output: list[str] - list of active finding issue_types for the invoice.
    Effects: creates or updates rows in invoice_issues, adds issue_events and audit_logs.
    Results: keeps invoice issues in exact sync with current invoice state.
    """
    findings = {}

    # Step 1: Map match check failures to canonical issue types
    for check in checks:
        if check.get("outcome") not in {"mismatch", "not_possible"}:
            continue
        name = (check.get("name") or "").casefold()
        mapped = next(
            (value for token, value in ISSUE_NAMES.items() if token in name), None
        )
        if mapped is None:
            continue
        issue_type, label = mapped
        findings[issue_type] = {
            "description": check.get("reason") or label,
            "expected": check.get("expected"),
            "actual": check.get("actual"),
            "severity": check.get("severity") or "warning",
        }

    # Step 2: Enforce mandatory PO linkage check
    if not getattr(invoice, "po_id", None):
        findings.setdefault(
            "missing_po",
            {
                "description": ISSUE_NAMES["po"][1],
                "expected": "linked purchase order",
                "actual": getattr(invoice, "po_number_raw", None),
                "severity": "error",
            },
        )

    # Step 3: Enforce vendor resolution check
    if not getattr(invoice, "vendor_id", None):
        findings.setdefault(
            "unknown_vendor",
            {
                "description": ISSUE_NAMES["vendor"][1],
                "expected": None,
                "actual": getattr(invoice, "vendor_name_raw", None),
                "severity": "error",
            },
        )

    # Step 4: Enforce Goods Receipt evidence check
    if "goods_receipt" in (missing_document_types or []):
        findings.setdefault(
            "missing_receipt",
            {
                "description": ISSUE_NAMES["receipt"][1],
                "expected": "goods receipt",
                "actual": None,
                "severity": "warning",
            },
        )

    # Step 5: Enforce extraction confidence threshold check
    confidence = getattr(invoice, "field_confidence", None) or {}
    threshold = int(os.getenv("FIELD_CONFIDENCE_MIN", "85"))
    confidence_values = {
        key: (value.get("confidence") if isinstance(value, dict) else value)
        for key, value in confidence.items()
    }
    low_fields = [
        key
        for key, value in confidence_values.items()
        if isinstance(value, (int, float)) and value < threshold
    ]
    if low_fields:
        confidence_unavailable = any(
            isinstance(confidence.get(key), dict)
            and confidence[key].get("derived", False)
            for key in low_fields
        )
        findings["low_confidence"] = {
            "description": (
                "Confidence unavailable, please verify"
                if confidence_unavailable
                else "Fields below confidence threshold: " + ", ".join(low_fields)
            ),
            "expected": threshold,
            "actual": {key: confidence[key] for key in low_fields},
            "severity": "warning",
        }

    # Step 6: Query existing open issues for this invoice
    active_rows = (
        db.query(InvoiceIssue)
        .filter(InvoiceIssue.invoice_id == invoice.id, InvoiceIssue.status.in_(ACTIVE))
        .all()
    )
    by_type = {row.issue_type: row for row in active_rows}

    # Step 7: Upsert findings - update existing or create new open issues
    for issue_type, finding in findings.items():
        issue = by_type.get(issue_type)
        if issue:
            issue.description = finding["description"]
            issue.expected_value = (
                {"value": finding["expected"]}
                if finding["expected"] is not None
                else None
            )
            issue.actual_value = (
                {"value": finding["actual"]} if finding["actual"] is not None else None
            )
            issue.severity = finding["severity"]
            continue
        # Create brand-new issue with automatic associate workload assignment
        issue = InvoiceIssue(
            invoice_id=invoice.id,
            issue_type=issue_type,
            severity=finding["severity"],
            description=finding["description"],
            expected_value={"value": finding["expected"]}
            if finding["expected"] is not None
            else None,
            actual_value={"value": finding["actual"]}
            if finding["actual"] is not None
            else None,
            status="open",
            owner_user_id=_assignment(db),
            due_at=datetime.now(UTC) + timedelta(days=3),
        )
        db.add(issue)
        db.flush()
        add_issue_event(db, issue, "created", user_id, issue.description)

    # Step 8: Automatically resolve active issues that no longer appear in latest match
    for issue_type, issue in by_type.items():
        if issue_type not in findings:
            issue.status = "resolved"
            issue.resolved_at = datetime.now(UTC)
            issue.resolution_note = (
                "Automatically resolved: latest match no longer finds this difference"
            )
            add_issue_event(db, issue, "resolved", None, issue.resolution_note)

    db.flush()
    return list(findings)
