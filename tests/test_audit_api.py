import uuid
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from src.api.rest.app import app
from src.api.rest.dependencies import get_current_user
from src.db.session_db import get_db
from src.models.audit_models import AuditLog, assign_audit_actor


def test_audit_log_requires_admin_role():
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": str(uuid.uuid4()),
        "role": "associate",
    }
    try:
        response = TestClient(app).get("/api/audit-log")
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert response.status_code == 403


def test_audit_log_admin_accepts_join_filters_and_sort_direction():
    query = MagicMock()
    query.filter.return_value = query
    query.count.return_value = 0
    result_page = query.order_by.return_value.offset.return_value.limit.return_value
    result_page.all.return_value = []
    db = MagicMock()
    db.query.return_value = query
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": str(uuid.uuid4()),
        "role": "admin",
    }
    app.dependency_overrides[get_db] = lambda: db
    try:
        response = TestClient(app).get(
            "/api/audit-log",
            params={
                "vendor_id": str(uuid.uuid4()),
                "gl_code": "6100",
                "sort_dir": "asc",
            },
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)
    assert response.status_code == 200
    assert query.filter.call_count >= 2


def test_new_audit_rows_get_explicit_user_or_system_actor():
    user_id = uuid.uuid4()
    user_action = AuditLog(
        action="invoice_approved",
        entity_type="invoice",
        entity_id=uuid.uuid4(),
        created_by=user_id,
        result="success",
    )
    system_action = AuditLog(
        action="invoice_match_run",
        entity_type="invoice",
        entity_id=uuid.uuid4(),
        result="success",
    )
    assign_audit_actor(None, None, user_action)
    assign_audit_actor(None, None, system_action)
    assert user_action.actor == str(user_id)
    assert system_action.actor == "system"

