"""Guard the audited-router mutation inventory against unreviewed additions."""

import ast
from pathlib import Path

CORE_ROOT = Path(__file__).resolve().parents[1]
ROUTER_DIR = CORE_ROOT / "src" / "api" / "rest" / "routers"
AUDITED_ROUTER_FILES = {
    "invoice_routers.py",
    "issue_routers.py",
    "document_routers.py",
    "email_draft_routers.py",
    "command_center_routers.py",
}

# Each route is paired with a test module that exercises the corresponding audit or
# lifecycle behavior. Additions must be reviewed and assigned coverage or an explicit
# justified exclusion before this contract test can pass.
ROUTE_AUDIT_TESTS = {
    "document_routers.py:reclassify_other_document": "test_document_reclassify.py",
    "document_routers.py:upload_document": "test_document_pipeline.py",
    "document_routers.py:resolve_purchase_order_vendor": "test_document_pipeline.py",
    "document_routers.py:parse_document": "test_document_pipeline.py",
    "document_routers.py:retry_document_processing": "test_document_pipeline.py",
    "document_routers.py:backfill_procurement_documents_endpoint": (
        "test_procurement_auto_flow.py"
    ),
    "document_routers.py:reprocess_documents_admin": "test_document_retry_services.py",
    "invoice_routers.py:three_way_match_invoice": "test_invoice_matching_services.py",
    "invoice_routers.py:verify_invoice_fields": "test_invoice_verification.py",
    "invoice_routers.py:confirm_invoice_match": "test_invoice_matching_services.py",
    "invoice_routers.py:reject_invoice_match": "test_invoice_matching_services.py",
    "invoice_routers.py:approve_invoice_exception": "test_invoice_review.py",
    "invoice_routers.py:review_invoice": "test_invoice_review.py",
    "invoice_routers.py:approve_invoice": "test_invoice_review.py",
    "invoice_routers.py:reject_invoice": "test_invoice_review.py",
    "invoice_routers.py:hold_invoice": "test_invoice_review.py",
    "invoice_routers.py:resubmit_invoice": "test_invoice_issues.py",
    "invoice_routers.py:record_invoice_payment": "test_invoice_lifecycle.py",
    "invoice_routers.py:void_invoice_payment": "test_invoice_lifecycle.py",
    "invoice_routers.py:release_invoice_hold": "test_invoice_lifecycle.py",
    "invoice_routers.py:escalate_invoice": "test_invoice_lifecycle.py",
    "invoice_routers.py:reassign_invoice": "test_invoice_lifecycle.py",
    "invoice_routers.py:run_invoice_match": "test_invoice_matching_services.py",
    "invoice_routers.py:update_invoice": "test_invoice_verification.py",
    "invoice_routers.py:confirm_invoice_po": "test_invoice_lifecycle.py",
    "issue_routers.py:comment_issue": "test_invoice_issues.py",
    "issue_routers.py:assign_issue": "test_invoice_issues.py",
    "issue_routers.py:reassign_issue": "test_invoice_issues.py",
    "issue_routers.py:escalate_issue": "test_invoice_issues.py",
    "issue_routers.py:contact_vendor": "test_invoice_issues.py",
    "issue_routers.py:resolve_issue": "test_invoice_issues.py",
    "issue_routers.py:dismiss_issue": "test_invoice_issues.py",
    "issue_routers.py:reopen_issue": "test_invoice_issues.py",
    "email_draft_routers.py:generate_invoice_email_draft": "test_email_drafts.py",
    "email_draft_routers.py:generate_issue_email_draft": "test_email_drafts.py",
    "email_draft_routers.py:edit_email_draft": "test_email_drafts.py",
    "email_draft_routers.py:send_email_draft": "test_email_drafts.py",
    "email_draft_routers.py:discard_email_draft": "test_email_drafts.py",
    "email_draft_routers.py:regenerate_email_draft": "test_email_drafts.py",
}


def _mutating_route_handlers():
    handlers = set()
    for filename in AUDITED_ROUTER_FILES:
        path = ROUTER_DIR / filename
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                call = decorator if isinstance(decorator, ast.Call) else None
                target = call.func if call else decorator
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "router"
                    and target.attr in {"post", "put", "patch", "delete"}
                ):
                    handlers.add(f"{filename}:{node.name}")
    return handlers


def test_every_mutating_audited_router_has_declared_audit_test_coverage():
    handlers = _mutating_route_handlers()
    declared = set(ROUTE_AUDIT_TESTS)

    assert handlers == declared, (
        f"Undeclared mutating routes: {sorted(handlers - declared)}; "
        f"stale route entries: {sorted(declared - handlers)}"
    )
    for test_filename in ROUTE_AUDIT_TESTS.values():
        test_path = CORE_ROOT / "tests" / test_filename
        assert test_path.is_file(), f"Missing audit coverage test: {test_filename}"
        test_source = test_path.read_text(encoding="utf-8")
        assert any(
            marker in test_source
            for marker in (
                "AuditLog",
                "InvoiceStateHistory",
                "IssueEvent",
                "EmailDraftEvent",
            )
        ), f"{test_filename} does not inspect audit or state history"
