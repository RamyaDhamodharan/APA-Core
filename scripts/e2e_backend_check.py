"""Run an authenticated end-to-end check against the isolated Docker stack."""

from __future__ import annotations

import base64
import imaplib
import json
import os
import smtplib
import socket
import time
import uuid
from collections.abc import Callable
from datetime import datetime

import httpx
import psycopg
from minio import Minio

from tests.e2e.sample_documents import (
    build_mail,
    invoice_payload,
    pdf_bytes,
    po_payload,
)

CORE_PORT = os.getenv("E2E_CORE_HOST_PORT", "18000")
AUTH_PORT = os.getenv("E2E_AUTH_HOST_PORT", "18001")
CORE_URL = os.getenv("E2E_CORE_URL", f"http://127.0.0.1:{CORE_PORT}")
AUTH_URL = os.getenv("E2E_AUTH_URL", f"http://127.0.0.1:{AUTH_PORT}")
DB_URL = os.getenv(
    "E2E_DB_URL",
    "postgresql://ap_e2e_user:e2e-only-postgres-password@"
    "127.0.0.1:15433/genworx_ap_e2e",
)
ADMIN_EMAIL = os.getenv("E2E_ADMIN_EMAIL", "admin@localhost")
ASSOCIATE_EMAIL = os.getenv("E2E_ASSOCIATE_EMAIL", "associate@localhost")
TEST_PASSWORD = "e2e-local-user-password-only"
SMTP_HOST = os.getenv("E2E_SMTP_HOST", "127.0.0.1")
SMTP_PORT = int(
    os.getenv("E2E_SMTP_PORT", os.getenv("E2E_GREENMAIL_SMTP_HOST_PORT", "13025"))
)
IMAP_HOST = os.getenv("E2E_IMAP_HOST", "127.0.0.1")
IMAP_PORT = int(
    os.getenv("E2E_IMAP_PORT", os.getenv("E2E_GREENMAIL_IMAP_HOST_PORT", "13143"))
)
MINIO_ENDPOINT = os.getenv(
    "E2E_MINIO_ENDPOINT", f"127.0.0.1:{os.getenv('E2E_MINIO_HOST_PORT', '19000')}"
)
MINIO_BUCKET = os.getenv("E2E_MINIO_BUCKET", "ap-documents")
RUN_ID = uuid.uuid4().hex[:10]


class BackendE2E:
    def __init__(self) -> None:
        self.client = httpx.Client(timeout=25)
        self.results: list[tuple[str, bool, str]] = []
        self.admin_headers: dict[str, str] = {}
        self.associate_headers: dict[str, str] = {}
        self.vendor_name = f"Synthetic E2E Supplier {RUN_ID}"
        self.po_number = f"E2E-PO-{RUN_ID}"
        self.invoice_number = f"E2E-INV-{RUN_ID}"
        self.mismatch_number = f"E2E-MIS-{RUN_ID}"
        self.future_po_number = f"E2E-LATE-PO-{RUN_ID}"
        self.future_invoice_number = f"E2E-LATE-INV-{RUN_ID}"
        self.match_invoice_id = ""
        self.supporting_document_id = ""
        self.mail_filenames = {
            "matching": f"invoice_match_{RUN_ID}.pdf",
            "mismatch": f"invoice_mismatch_{RUN_ID}.pdf",
            "random": f"random_{RUN_ID}.pdf",
            "corrupt": f"invoice_corrupt_{RUN_ID}.pdf",
            "late": f"invoice_late_{RUN_ID}.pdf",
        }
        self.minio = Minio(
            MINIO_ENDPOINT,
            access_key="e2e-minio-user",
            secret_key="e2e-only-minio-password",
            secure=False,
        )

    def step(self, label: str, operation: Callable[[], str | None]) -> bool:
        try:
            evidence = operation() or "assertions passed"
            self.results.append((label, True, evidence))
            print(f"PASS | {label} | {evidence}", flush=True)
            return True
        except Exception as exc:
            evidence = f"{type(exc).__name__}: {self.safe_error(str(exc))[:240]}"
            self.results.append((label, False, evidence))
            print(f"FAIL | {label} | {evidence}", flush=True)
            return False

    @staticmethod
    def safe_error(message: str) -> str:
        for secret in (
            DB_URL,
            TEST_PASSWORD,
            "e2e-only-postgres-password",
            "e2e-only-jwt-signing-key-change-me",
            "e2e-only-minio-password",
            "test-only-password",
        ):
            message = message.replace(secret, "[redacted]")
        return message

    def db(self, query: str, params: tuple = ()):
        with psycopg.connect(DB_URL) as connection, connection.cursor() as cursor:
            cursor.execute(query, params)
            return cursor.fetchall()

    def request(self, method: str, path: str, *, auth=True, **kwargs) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}))
        acting_user = kwargs.pop("acting_user", "admin")
        if auth:
            headers.update(
                self.associate_headers
                if acting_user == "associate"
                else self.admin_headers
            )
        return self.client.request(
            method, f"{CORE_URL}{path}", headers=headers, **kwargs
        )

    def wait_http(self) -> None:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                auth = self.client.get(f"{AUTH_URL}/health")
                core = self.client.get(f"{CORE_URL}/health")
                if auth.status_code == 200 and core.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(1)
        raise AssertionError("Auth/Core health endpoints did not become ready")

    def assert_status(
        self, response: httpx.Response, expected: int, label: str
    ) -> None:
        if response.status_code != expected:
            raise AssertionError(
                f"{label}: expected HTTP {expected}, got {response.status_code}"
            )

    def login(self, email: str, password: str) -> tuple[dict[str, str], dict]:
        response = self.client.post(
            f"{AUTH_URL}/api/auth/login",
            json={"email": email, "password": password},
        )
        self.assert_status(response, 200, "login")
        token = response.json()["access_token"]
        encoded_claims = token.split(".")[1]
        padded_claims = encoded_claims + "=" * (-len(encoded_claims) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded_claims))
        return {"Authorization": f"Bearer {token}"}, claims

    def admin_login(self) -> str:
        self.wait_http()
        admin_headers, claims = self.login(ADMIN_EMAIL, TEST_PASSWORD)
        assert claims.get("role") == "admin", (
            "admin access token lacks admin role claim"
        )
        self.admin_headers = admin_headers

        return "Auth/Core healthy; admin login token has role=admin"

    def associate_rbac(self) -> str:
        associate_headers, associate_claims = self.login(ASSOCIATE_EMAIL, TEST_PASSWORD)
        assert associate_claims.get("role") == "associate"
        self.associate_headers = associate_headers
        forbidden = self.client.post(
            f"{CORE_URL}/api/vendors",
            headers=associate_headers,
            json={"name": f"Forbidden Vendor {RUN_ID}"},
        )
        self.assert_status(forbidden, 403, "associate vendor creation")

        return "associate role verified; vendor create returned 403"

    def create_vendor(self) -> str:
        vendor_response = self.request(
            "POST",
            "/api/vendors",
            json={"name": self.vendor_name, "contact_email": "supplier@localhost"},
        )
        self.assert_status(vendor_response, 201, "admin vendor creation")
        self.vendor_id = vendor_response.json()["id"]
        return f"created synthetic vendor {self.vendor_id}"

    def _upload(self, filename: str, content: bytes) -> dict:
        response = self.request(
            "POST",
            "/api/documents/upload",
            files=[("file", (filename, content, "application/pdf"))],
        )
        self.assert_status(response, 202, f"upload {filename}")
        return response.json()

    def _wait_document(self, filename: str, acceptable: set[str], timeout=90) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rows = self.db(
                "SELECT id::text, status::text, doc_type::text, storage_path, "
                "failure_message FROM core.documents WHERE original_filename = %s "
                "ORDER BY created_at DESC LIMIT 1",
                (filename,),
            )
            if rows and rows[0][1] in acceptable:
                return {
                    "id": rows[0][0],
                    "status": rows[0][1],
                    "doc_type": rows[0][2],
                    "storage_path": rows[0][3],
                    "failure_message": rows[0][4],
                }
            time.sleep(1)
        raise AssertionError(f"document {filename} did not reach {sorted(acceptable)}")

    def upload_po(self, po_number: str, *, label: str) -> tuple[dict, dict]:
        filename = f"po_{label}_{RUN_ID}.pdf"
        uploaded = self._upload(
            filename,
            pdf_bytes(po_payload(vendor=self.vendor_name, po_number=po_number)),
        )
        document = self._wait_document(filename, {"extracted", "failed"})
        assert document["status"] == "extracted", document["failure_message"]
        assert document["doc_type"] == "po"
        assert document["storage_path"].startswith("po/"), document["storage_path"]
        self._wait_object(document["storage_path"])
        return uploaded, document

    def _wait_object(self, key: str, timeout=25) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                self.minio.stat_object(MINIO_BUCKET, key)
                return
            except Exception:
                time.sleep(1)
        raise AssertionError(f"MinIO object missing for key prefix {key[:120]}")

    def seed_checkpoint(self) -> str:
        response = self.request("POST", "/api/email-polling/run")
        self.assert_status(response, 200, "initial email poll")
        status = self.request("GET", "/api/email-polling/status")
        self.assert_status(status, 200, "email poll status")
        payload = status.json()
        assert payload["last_uid"] == 0, (
            f"expected empty-mailbox checkpoint 0, got {payload['last_uid']}"
        )
        assert payload["last_result"] == {
            "emails": 0,
            "attachments": 0,
            "duplicates": 0,
            "filtered": 0,
            "failed": 0,
            "skipped_known": 0,
        }, payload["last_result"]
        return "first run saved UID checkpoint without backfill"

    def send_mail(self, raw: bytes) -> None:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as smtp:
            smtp.sendmail("supplier@localhost", "ap-test@localhost", raw)

    def mark_seen(self, message_id: str) -> None:
        with imaplib.IMAP4(IMAP_HOST, IMAP_PORT) as mailbox:
            mailbox.login("ap-test@localhost", "test-only-password")
            typ, _ = mailbox.select("INBOX")
            assert typ == "OK", "cannot select GreenMail INBOX"
            typ, result = mailbox.uid(
                "SEARCH", None, "HEADER", "Message-ID", message_id
            )
            assert typ == "OK" and result[0], "message to mark seen was not found"
            uid = result[0].split()[-1]
            typ, _ = mailbox.uid("STORE", uid, "+FLAGS.SILENT", "(\\Seen)")
            assert typ == "OK", "GreenMail did not mark message Seen"

    def send_mail_batch(self) -> str:
        matching_id = f"<match-{RUN_ID}@e2e.local>"
        matching = pdf_bytes(
            invoice_payload(
                vendor=self.vendor_name,
                invoice_number=self.invoice_number,
                po_number=self.po_number,
            )
        )
        mismatch = pdf_bytes(
            invoice_payload(
                vendor=self.vendor_name,
                invoice_number=self.mismatch_number,
                po_number=self.po_number,
                unit_price=21.0,
            )
        )
        random_pdf = pdf_bytes(
            {"document_type": "other", "description": "Synthetic random report"}
        )
        corrupt_pdf = b"%PDF-1.4\nsynthetic corrupt PDF with no valid xref\n%%EOF\n"
        messages = [
            build_mail(
                subject=f"Matching invoice {RUN_ID}",
                message_id=matching_id,
                attachments=(
                    (self.mail_filenames["matching"], matching, "application/pdf"),
                ),
            ),
            build_mail(
                subject=f"Mismatching invoice {RUN_ID}",
                message_id=f"<mismatch-{RUN_ID}@e2e.local>",
                attachments=(
                    (self.mail_filenames["mismatch"], mismatch, "application/pdf"),
                ),
            ),
            build_mail(
                subject=f"Random PDF {RUN_ID}",
                message_id=f"<random-{RUN_ID}@e2e.local>",
                attachments=(
                    (self.mail_filenames["random"], random_pdf, "application/pdf"),
                ),
            ),
            build_mail(
                subject=f"Corrupt PDF {RUN_ID}",
                message_id=f"<corrupt-{RUN_ID}@e2e.local>",
                attachments=(
                    (self.mail_filenames["corrupt"], corrupt_pdf, "application/pdf"),
                ),
            ),
            build_mail(
                subject=f"Text only {RUN_ID}",
                message_id=f"<text-{RUN_ID}@e2e.local>",
                body="Synthetic E2E text-only message.",
            ),
            build_mail(
                subject=f"Duplicate matching invoice {RUN_ID}",
                message_id=matching_id,
                attachments=(
                    (self.mail_filenames["matching"], matching, "application/pdf"),
                ),
            ),
        ]
        for index, raw in enumerate(messages):
            self.send_mail(raw)
            if index == 0:
                self.mark_seen(matching_id)
        return (
            "six synthetic messages sent; matching message marked Seen before polling"
        )

    def poll_messages(self) -> str:
        response = self.request("POST", "/api/email-polling/run")
        self.assert_status(response, 200, "email poll")
        expected = {
            "emails": 6,
            "attachments": 4,
            "duplicates": 1,
            "filtered": 0,
            "failed": 0,
            "skipped_known": 1,
        }
        assert response.json() == expected, f"poll counts differ: {response.json()}"
        status = self.request("GET", "/api/email-polling/status")
        self.assert_status(status, 200, "email poll status")
        state = status.json()
        assert state["last_uid"] >= 6, state
        assert state["uidvalidity"] is not None and state["last_run_at"], state
        assert state["last_result"] == expected, state["last_result"]
        return f"checkpoint={state['last_uid']}; counts={expected}"

    def validate_mail_results(self) -> str:
        matching = self._wait_document(
            self.mail_filenames["matching"], {"extracted", "failed"}
        )
        mismatch = self._wait_document(
            self.mail_filenames["mismatch"], {"extracted", "failed"}
        )
        random_doc = self._wait_document(
            self.mail_filenames["random"], {"other", "failed"}
        )
        corrupt = self._wait_document(
            self.mail_filenames["corrupt"], {"failed", "extracted"}
        )
        assert (
            matching["status"] == "extracted" and matching["doc_type"] == "invoice"
        ), matching
        assert (
            mismatch["status"] == "extracted" and mismatch["doc_type"] == "invoice"
        ), mismatch
        assert random_doc["status"] == "other" and random_doc["doc_type"] == "other", (
            random_doc
        )
        assert random_doc["storage_path"].startswith("other/"), random_doc[
            "storage_path"
        ]
        assert corrupt["status"] == "failed", corrupt
        message = (corrupt["failure_message"] or "").casefold()
        assert (
            message
            and "traceback" not in message
            and "password" not in message
            and "secret" not in message
        )
        assert (matching["storage_path"] or "").startswith("invoice/")
        self._wait_object(random_doc["storage_path"])
        self._wait_object(matching["storage_path"])
        root = self.db(
            "SELECT status::text, failure_message FROM core.documents "
            "WHERE email_subject = %s ORDER BY created_at DESC LIMIT 1",
            (f"Text only {RUN_ID}",),
        )
        assert (
            root
            and root[0][0] == "ignored"
            and root[0][1] == "No bill attachment found"
        ), root
        duplicate = self.db(
            "SELECT count(*) FROM core.audit_log "
            "WHERE action = 'email_skipped_duplicate'"
        )[0][0]
        assert duplicate > 0, "duplicate email has no skip audit entry"
        invoice = self.db(
            "SELECT id::text, match_status::text, final_status::text "
            "FROM core.invoices "
            "WHERE invoice_no = %s AND is_current",
            (self.invoice_number,),
        )
        assert invoice, "matching invoice was not saved"
        self.match_invoice_id = invoice[0][0]
        assert invoice[0][1] == "ready_for_review" and invoice[0][2] == "approved", (
            invoice[0]
        )
        match_result = self.db(
            "SELECT result::text FROM core.match_runs WHERE invoice_id=%s::uuid "
            "ORDER BY run_no DESC LIMIT 1",
            (self.match_invoice_id,),
        )
        assert match_result and match_result[0][0] == "match_ok", match_result
        mismatch_rows = self.db(
            "SELECT match_status::text, differences FROM core.match_runs "
            "WHERE invoice_id = (SELECT id FROM core.invoices WHERE invoice_no = %s) "
            "ORDER BY run_no DESC LIMIT 1",
            (self.mismatch_number,),
        )
        assert mismatch_rows and mismatch_rows[0][0] == "match_issues", mismatch_rows
        assert mismatch_rows[0][1], "unit price mismatch was not recorded"
        differences = json.dumps(mismatch_rows[0][1], default=str)
        assert "20" in differences and "21" in differences, differences
        return (
            "invoice approved; mismatch recorded; other routed; corrupt failed "
            "plainly; text ignored; duplicate audited"
        )

    def send_late_invoice(self) -> str:
        name = self.mail_filenames["late"]
        data = pdf_bytes(
            invoice_payload(
                vendor=self.vendor_name,
                invoice_number=self.future_invoice_number,
                po_number=self.future_po_number,
            )
        )
        self.send_mail(
            build_mail(
                subject=f"Late PO invoice {RUN_ID}",
                message_id=f"<late-{RUN_ID}@e2e.local>",
                attachments=((name, data, "application/pdf"),),
            )
        )
        response = self.request("POST", "/api/email-polling/run")
        self.assert_status(response, 200, "poll late invoice")
        document = self._wait_document(name, {"extracted", "failed"})
        assert document["status"] == "extracted", document
        invoice = self.db(
            "SELECT id::text, match_status::text FROM core.invoices "
            "WHERE invoice_no = %s",
            (self.future_invoice_number,),
        )
        assert invoice and invoice[0][1] == "waiting_for_po", invoice
        self.future_invoice_id = invoice[0][0]
        return f"invoice {invoice[0][0]} is waiting_for_po before its PO arrives"

    def duplicate_invoice_case(self) -> str:
        filename = f"invoice_same_number_new_message_{RUN_ID}.pdf"
        payload = invoice_payload(
            vendor=self.vendor_name,
            invoice_number=self.invoice_number,
            po_number=self.po_number,
        )
        payload["submission_note"] = "synthetic second email; unchanged invoice facts"
        self.send_mail(
            build_mail(
                subject=f"Duplicate invoice number {self.invoice_number}",
                message_id=f"<same-invoice-new-message-{RUN_ID}@e2e.local>",
                attachments=((filename, pdf_bytes(payload), "application/pdf"),),
            )
        )
        response = self.request("POST", "/api/email-polling/run")
        self.assert_status(response, 200, "poll repeated vendor invoice number")
        document = self._wait_document(filename, {"duplicate_blocked", "failed"})
        assert document["status"] == "duplicate_blocked", document
        return (
            "same vendor/invoice number in a distinct email was blocked as a duplicate"
        )

    def check_late_po_reprocess(self) -> str:
        _, po = self.upload_po(self.future_po_number, label="late-after")
        row = self.db(
            "SELECT match_status::text, final_status::text, po_id "
            "FROM core.invoices WHERE invoice_no = %s",
            (self.future_invoice_number,),
        )
        assert row and row[0][0] == "ready_for_review" and row[0][1] == "approved", row
        assert row[0][2] is not None, row
        return (
            f"later PO {po['id']} automatically rematched and approved "
            "the waiting invoice"
        )

    def supporting_documents(self) -> str:
        url = f"/api/invoices/{self.match_invoice_id}/supporting-documents"
        content = b"Synthetic E2E contract evidence."
        upload = self.request(
            "POST",
            url,
            data={"category": "contract", "description": "Synthetic audit attachment"},
            files={"files": (f"contract-{RUN_ID}.txt", content, "text/plain")},
        )
        self.assert_status(upload, 200, "supporting document upload")
        result = upload.json()["results"][0]
        assert result["status"] in {"uploaded", "success"}, result
        self.supporting_document_id = result["id"]
        listed = self.request("GET", url)
        self.assert_status(listed, 200, "supporting document list")
        assert any(row["id"] == self.supporting_document_id for row in listed.json())
        download_path = f"{url}/{self.supporting_document_id}/file"
        download = self.request("GET", download_path)
        self.assert_status(download, 200, "supporting document download")
        assert download.content == content
        audit = self.db(
            "SELECT count(*) FROM core.audit_log "
            "WHERE action='supporting_document_viewed' "
            "AND entity_id=%s::uuid",
            (self.match_invoice_id,),
        )[0][0]
        assert audit > 0, "supporting document view did not create audit entry"
        return (
            "upload, list, download and viewed audit verified for "
            f"{self.supporting_document_id}"
        )

    def support_delete(self) -> str:
        url = (
            f"/api/invoices/{self.match_invoice_id}/supporting-documents/"
            f"{self.supporting_document_id}"
        )
        missing_reason = self.request("DELETE", url, json={"reason": "  "})
        self.assert_status(
            missing_reason, 422, "supporting document empty delete reason"
        )
        row = self.db(
            "SELECT storage_key FROM core.invoice_supporting_documents "
            "WHERE id=%s::uuid",
            (self.supporting_document_id,),
        )
        assert row, "supporting document database row missing"
        storage_key = row[0][0]
        deleted = self.request("DELETE", url, json={"reason": "Synthetic E2E cleanup"})
        self.assert_status(deleted, 200, "supporting document soft delete")
        hidden = self.request(
            "GET", f"/api/invoices/{self.match_invoice_id}/supporting-documents"
        )
        assert all(item["id"] != self.supporting_document_id for item in hidden.json())
        self._wait_object(storage_key)
        return "empty reason rejected; soft-deleted row hidden; MinIO object remains"

    def check_audit_timeline(self) -> str:
        actions = {
            row[0]
            for row in self.db(
                "SELECT DISTINCT action FROM core.audit_log WHERE entity_id=%s::uuid",
                (self.match_invoice_id,),
            )
        }
        required = {
            "invoice_match_run",
            "supporting_document_upload",
            "supporting_document_viewed",
            "supporting_document_deleted",
        }
        assert required <= actions, (
            f"missing audit actions: {sorted(required - actions)}"
        )
        timeline = self.request(
            "GET", f"/api/invoices/{self.match_invoice_id}/timeline"
        )
        self.assert_status(timeline, 200, "invoice timeline")
        items = timeline.json()
        timeline_actions = [item["action"] for item in items]
        assert required <= set(timeline_actions), (
            f"timeline omitted actions: {required - set(timeline_actions)}"
        )
        timestamps = [
            datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
            for item in items
        ]
        assert timestamps == sorted(timestamps), "invoice timeline is not chronological"
        email_actions = {
            row[0]
            for row in self.db(
                "SELECT DISTINCT action FROM core.audit_log WHERE action IN "
                "('email_poll_run', 'attachment_ingested', "
                "'email_skipped_duplicate', 'email_ignored')"
            )
        }
        assert {
            "email_poll_run",
            "attachment_ingested",
            "email_skipped_duplicate",
            "email_ignored",
        } <= email_actions, f"missing email audit actions: {sorted(email_actions)}"
        return (
            "audit actions visible in chronological invoice timeline: "
            f"{sorted(required)}"
        )

    def check_idempotent_poll(self) -> str:
        before = self.db(
            "SELECT count(*) FROM core.documents WHERE source='email_poll'"
        )[0][0]
        response = self.request("POST", "/api/email-polling/run")
        self.assert_status(response, 200, "idempotent repeat poll")
        after = self.db(
            "SELECT count(*) FROM core.documents WHERE source='email_poll'"
        )[0][0]
        assert after == before, (
            f"repeat poll created {after - before} additional documents"
        )
        return f"document count remained {after} after repeat poll"

    def _send_invoice(self, number: str, payload: dict, label: str) -> tuple[str, dict]:
        filename = f"invoice_{label}_{RUN_ID}.pdf"
        raw = build_mail(
            subject=f"Invoice {number}",
            message_id=f"<{label}-{RUN_ID}@e2e.local>",
            attachments=((filename, pdf_bytes(payload), "application/pdf"),),
        )
        self.send_mail(raw)
        response = self.request("POST", "/api/email-polling/run")
        self.assert_status(response, 200, f"poll {label}")
        document = self._wait_document(filename, {"extracted", "failed", "other"})
        row = self.db(
            "SELECT id::text, workflow_status, match_status::text, final_status::text "
            "FROM core.invoices WHERE invoice_no=%s AND is_current "
            "ORDER BY created_at DESC LIMIT 1",
            (number,),
        )
        return (row[0][0] if row else ""), {
            "document": document,
            "invoice": row[0] if row else None,
            "filename": filename,
        }

    def quantity_mismatch(self) -> str:
        po_num = f"E2E-QTY-PO-{RUN_ID}"
        po_raw = build_mail(
            subject=f"Purchase order {po_num}",
            message_id=f"<qty-po-{RUN_ID}@e2e.local>",
            attachments=(
                (
                    f"po_qty_{RUN_ID}.pdf",
                    pdf_bytes(
                        po_payload(
                            vendor=self.vendor_name, po_number=po_num, quantity=80
                        )
                    ),
                    "application/pdf",
                ),
            ),
        )
        self.send_mail(po_raw)
        response = self.request("POST", "/api/email-polling/run")
        self.assert_status(response, 200, "poll quantity PO")
        number = f"E2E-QTY-INV-{RUN_ID}"
        invoice_id, result = self._send_invoice(
            number,
            invoice_payload(
                vendor=self.vendor_name,
                invoice_number=number,
                po_number=po_num,
                quantity=100,
            ),
            "qty",
        )
        assert invoice_id and result["document"]["status"] == "extracted", result
        match = self.db(
            "SELECT result::text, differences FROM core.match_runs "
            "WHERE invoice_id=%s::uuid ORDER BY run_no DESC LIMIT 1",
            (invoice_id,),
        )
        assert match and match[0][1], match
        difference_values = json.dumps(match[0][1], default=str)
        assert "80" in difference_values and "100" in difference_values, match
        return (
            "billed quantity 100 against PO quantity 80 raised match differences "
            f"({match[0][0]})"
        )

    def unknown_vendor_no_po(self) -> str:
        number = f"E2E-UNKNOWN-{RUN_ID}"
        invoice_id, result = self._send_invoice(
            number,
            invoice_payload(
                vendor=f"Unknown E2E Vendor {RUN_ID}",
                invoice_number=number,
                po_number=f"NO-PO-{RUN_ID}",
            ),
            "unknown",
        )
        assert invoice_id, result
        row = self.db(
            "SELECT workflow_status FROM core.invoices WHERE id=%s::uuid",
            (invoice_id,),
        )
        assert row and row[0][0] == "rejected", row
        notices = self.db(
            "SELECT count(*) FROM core.notifications n JOIN core.invoices i "
            "ON i.source_document_id=n.document_id WHERE i.id=%s::uuid "
            "AND n.recipient_email='supplier@localhost'",
            (invoice_id,),
        )
        # Email outbox may record the notification even when the task is still queued.
        if not notices or notices[0][0] == 0:
            notices = self.db(
                "SELECT count(*) FROM core.email_drafts "
                "WHERE invoice_id=%s::uuid AND kind='no_match'",
                (invoice_id,),
            )
        assert notices and notices[0][0] > 0, (
            "sender notification was not queued through the outbox"
        )
        return (
            "unknown vendor/no PO rejected and sender notification recorded via outbox"
        )

    def low_confidence_verify(self) -> str:
        number = f"E2E-LOWCONF-{RUN_ID}"
        confidence = dict.fromkeys(
            (
                "vendor",
                "invoice_number",
                "invoice_date",
                "due_date",
                "currency",
                "subtotal",
                "tax",
                "total",
                "order_number",
                "gl_code",
            ),
            99,
        )
        confidence["total"] = 20
        invoice_id, result = self._send_invoice(
            number,
            invoice_payload(
                vendor=self.vendor_name,
                invoice_number=number,
                po_number=self.po_number,
                field_confidence=confidence,
            ),
            "lowconf",
        )
        assert invoice_id, result
        row = self.db(
            "SELECT workflow_status FROM core.invoices WHERE id=%s::uuid", (invoice_id,)
        )
        assert row and row[0][0] == "verifying", row
        edited = self.request(
            "POST",
            f"/api/invoices/{invoice_id}/verify",
            json={
                "confirmed_values": {"invoice_number": number},
                "reason": "Synthetic E2E confirmed invoice number",
            },
            acting_user="associate",
        )
        self.assert_status(edited, 200, "verify low-confidence invoice")
        audit = self.db(
            "SELECT count(*) FROM core.audit_log "
            "WHERE entity_id=%s::uuid AND action='invoice_field_edited'",
            (invoice_id,),
        )[0][0]
        assert audit > 0, "verification edit was not audited"
        return "low-confidence invoice entered verifying; associate edit was audited"

    def overdue_and_buckets(self) -> str:
        number = f"E2E-OVERDUE-{RUN_ID}"
        invoice_id, result = self._send_invoice(
            number,
            invoice_payload(
                vendor=self.vendor_name,
                invoice_number=number,
                po_number=self.po_number,
                due_date="2026-10-19",
            ),
            "overdue",
        )
        assert invoice_id, result
        overdue = self.request(
            "GET", "/api/command-center/bills", params={"bucket": "overdue"}
        )
        self.assert_status(overdue, 200, "overdue command-center bucket")
        item = next(
            (
                item
                for item in overdue.json()["items"]
                if str(item["invoice_id"]) == invoice_id
            ),
            None,
        )
        if item is None:
            from celery import Celery

            Celery(
                "e2e",
                broker=(
                    f"redis://127.0.0.1:{os.getenv('E2E_REDIS_HOST_PORT', '16379')}/0"
                ),
            ).send_task("ap.mark_overdue", queue="email")
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                overdue = self.request(
                    "GET", "/api/command-center/bills", params={"bucket": "overdue"}
                )
                item = next(
                    (
                        item
                        for item in overdue.json()["items"]
                        if str(item["invoice_id"]) == invoice_id
                    ),
                    None,
                )
                if item:
                    break
                time.sleep(1)
        assert item and item["days_overdue"] >= 1, item
        self.overdue_invoice_id = invoice_id
        summary = self.request("GET", "/api/command-center/summary")
        self.assert_status(summary, 200, "command-center summary")
        assert set(summary.json()["buckets"]) >= {
            "ready_to_pay",
            "overdue",
            "rejected",
            "needs_attention",
            "all",
        }, summary.json()
        for bucket in ("ready_to_pay", "overdue", "rejected", "needs_attention", "all"):
            response = self.request(
                "GET", "/api/command-center/bills", params={"bucket": bucket}
            )
            self.assert_status(response, 200, f"command-center {bucket}")
        return (
            f"overdue invoice appears with days_overdue={item['days_overdue']}; "
            "all buckets responded"
        )

    def reject_correct_resubmit(self) -> str:
        number = f"E2E-RESUBMIT-{RUN_ID}"
        invoice_id, result = self._send_invoice(
            number,
            invoice_payload(
                vendor=self.vendor_name,
                invoice_number=number,
                po_number=self.po_number,
                unit_price=21.0,
                gl_code="BAD-GL",
            ),
            "resubmit",
        )
        assert invoice_id, result
        rejected = self.request(
            "POST",
            f"/api/invoices/{invoice_id}/reject",
            json={"reason": "Synthetic E2E wrong GL code"},
        )
        self.assert_status(rejected, 200, "reject wrong GL invoice")
        old = self.db(
            "SELECT id::text, version_no FROM core.invoices WHERE id=%s::uuid",
            (invoice_id,),
        )[0]
        corrected = self.request(
            "POST", f"/api/invoices/{invoice_id}/resubmit", json={"gl_code": "6100"}
        )
        self.assert_status(corrected, 200, "resubmit corrected invoice")
        new_id = corrected.json().get("id") or corrected.json().get("invoice_id")
        if not new_id:
            rows = self.db(
                "SELECT id::text FROM core.invoices "
                "WHERE resubmitted_from_id=%s::uuid AND is_current",
                (invoice_id,),
            )
            new_id = rows[0][0] if rows else None
        assert new_id and new_id != old[0], corrected.json()
        new = self.db(
            "SELECT gl_code, version_no, is_current FROM core.invoices "
            "WHERE id=%s::uuid",
            (new_id,),
        )
        assert new and new[0][0] == "6100" and new[0][1] == old[1] + 1 and new[0][2], (
            new
        )
        rejected_bucket = self.request(
            "GET", "/api/command-center/bills", params={"bucket": "rejected"}
        )
        self.assert_status(rejected_bucket, 200, "rejected command-center bucket")
        assert any(
            str(item["invoice_id"]) == old[0] and item["can_resubmit"]
            for item in rejected_bucket.json()["items"]
        ), "rejected bill missing from rejected bucket or cannot resubmit"
        return (
            f"rejected wrong-GL version {old[1]} and created corrected version {new_id}"
        )

    def corrupt_bulk_upload(self) -> str:
        files = [
            (
                "file",
                (f"corrupt_bulk_{RUN_ID}.pdf", b"%PDF-corrupt E2E", "application/pdf"),
            ),
            (
                "file",
                (
                    f"valid_bulk_{RUN_ID}.pdf",
                    pdf_bytes({"document_type": "other", "description": "synthetic"}),
                    "application/pdf",
                ),
            ),
        ]
        response = self.request("POST", "/api/documents/upload", files=files)
        self.assert_status(response, 202, "bulk upload corrupt and valid PDFs")
        results = response.json()["results"]
        assert len(results) == 2 and {row["filename"] for row in results} == {
            f"corrupt_bulk_{RUN_ID}.pdf",
            f"valid_bulk_{RUN_ID}.pdf",
        }, results
        assert all(row["status"] for row in results), results
        corrupt = self._wait_document(f"corrupt_bulk_{RUN_ID}.pdf", {"failed"})
        other = self._wait_document(f"valid_bulk_{RUN_ID}.pdf", {"other", "failed"})
        assert corrupt["status"] == "failed" and other["status"] == "other", (
            corrupt,
            other,
        )
        return "bulk API accepted separate per-file results for corrupt and valid PDF"

    def issue_draft_send_reply(self) -> str:
        invoice = self.db(
            "SELECT id::text FROM core.invoices WHERE invoice_no=%s "
            "AND is_current ORDER BY created_at DESC LIMIT 1",
            (self.mismatch_number,),
        )
        assert invoice, "price mismatch invoice was not stored"
        issues = self.request("GET", "/api/issues", params={"invoice": invoice[0][0]})
        self.assert_status(issues, 200, "list mismatch invoice issues")
        rows = issues.json().get(
            "items", issues.json() if isinstance(issues.json(), list) else []
        )
        issue = next(
            (
                row
                for row in rows
                if row.get("invoice_id")
                and row.get("status") in {"open", "in_progress"}
            ),
            None,
        )
        if not issue:
            rows = self.db(
                "SELECT id::text FROM core.invoice_issues "
                "WHERE invoice_id=(SELECT id FROM core.invoices WHERE invoice_no=%s) "
                "AND status IN ('open','in_progress') LIMIT 1",
                (self.mismatch_number,),
            )
            issue = {"id": rows[0][0]} if rows else None
        assert issue, "price mismatch did not create an open issue"
        created = self.request(
            "POST",
            f"/api/issues/{issue['id']}/contact-vendor",
            json={"kind": "price_variance"},
        )
        self.assert_status(created, 201, "create issue vendor draft")
        draft_id = str(created.json()["draft_id"])
        draft_response = self.request("GET", f"/api/email-drafts/{draft_id}")
        self.assert_status(draft_response, 200, "read vendor draft")
        draft = draft_response.json()
        assert not draft.get("blocked_by_allowlist"), draft
        sent = self.request("POST", f"/api/email-drafts/{draft_id}/send")
        self.assert_status(sent, 200, "send vendor draft")
        assert sent.json().get("sent") is True, sent.json()
        token = draft.get("thread_key")
        message_id = f"<reply-{RUN_ID}@supplier.local>"
        self.send_mail(
            build_mail(
                subject=f"Re: {token} price clarification",
                message_id=message_id,
                sender="supplier@localhost",
                extra_headers={
                    "In-Reply-To": str(draft.get("message_id") or "<none@local>")
                },
            )
        )
        polled = self.request("POST", "/api/email-polling/run")
        self.assert_status(polled, 200, "poll vendor reply")
        issue_detail = self.request("GET", f"/api/issues/{issue['id']}")
        self.assert_status(issue_detail, 200, "read issue after vendor reply")
        status = issue_detail.json().get("status")
        assert status == "in_progress", issue_detail.json()
        attention = self.request(
            "GET", "/api/command-center/bills", params={"bucket": "needs_attention"}
        )
        self.assert_status(attention, 200, "needs-attention command-center bucket")
        assert any(
            item.get("invoice_no") == self.mismatch_number
            for item in attention.json()["items"]
        ), "open price issue missing from needs_attention bucket"
        return (
            "issue draft sent via GreenMail; reply linked and issue returned "
            "in_progress"
        )

    def payment_and_followup(self) -> str:
        invoice_id = self.match_invoice_id
        ready = self.request(
            "GET", "/api/command-center/bills", params={"bucket": "ready_to_pay"}
        )
        self.assert_status(ready, 200, "ready-to-pay command-center bucket")
        assert any(
            str(row["invoice_id"]) == invoice_id for row in ready.json()["items"]
        ), "approved unpaid invoice missing from ready_to_pay"
        payment = self.request(
            "POST",
            f"/api/invoices/{invoice_id}/pay",
            json={
                "paid_date": "2026-10-22",
                "method": "bank_transfer",
                "reference": f"E2E-{RUN_ID}",
            },
        )
        self.assert_status(payment, 200, "record external payment")
        duplicate = self.request(
            "POST",
            f"/api/invoices/{invoice_id}/pay",
            json={
                "paid_date": "2026-10-22",
                "method": "bank_transfer",
                "reference": f"E2E-{RUN_ID}",
            },
        )
        self.assert_status(duplicate, 409, "prevent duplicate payment")
        from celery import Celery

        Celery(
            "e2e",
            broker=f"redis://127.0.0.1:{os.getenv('E2E_REDIS_HOST_PORT', '16379')}/0",
        ).send_task("ap.run_follow_ups", queue="email")
        deadline = time.monotonic() + 30
        followup = None
        while time.monotonic() < deadline:
            listing = self.request(
                "GET",
                "/api/follow-ups",
                params={"entity_type": "invoice", "entity_id": self.overdue_invoice_id},
            )
            self.assert_status(listing, 200, "list follow-ups")
            items = listing.json().get("items", [])
            followup = next(
                (item for item in items if item.get("stopped_at") is None), None
            )
            if followup:
                break
            time.sleep(1)
        assert followup, "follow-up task did not create an eligible follow-up"
        stopped = self.request(
            "POST",
            f"/api/follow-ups/{followup['id']}/stop",
            json={"reason": "Synthetic E2E complete"},
        )
        self.assert_status(stopped, 200, "stop follow-up")
        return "payment recorded once; follow-up created and stopped with reason"

    def run(self) -> int:
        self.step("service health and seeded admin login/role", self.admin_login)
        self.step("associate cannot create a vendor", self.associate_rbac)
        self.step("admin creates synthetic vendor", self.create_vendor)
        self.step(
            "first poll establishes checkpoint without backfill", self.seed_checkpoint
        )
        self.step("upload and process PO into MinIO po/", self._initial_po)
        self.step("send GreenMail messages and mark one Seen", self.send_mail_batch)
        self.step(
            "poll counts, UID checkpoint, and status endpoint", self.poll_messages
        )
        self.step(
            "mail classification, matching, failures, dedupe, and no-attachment",
            self.validate_mail_results,
        )
        self.step(
            "same vendor and invoice number from a distinct message is blocked",
            self.duplicate_invoice_case,
        )
        self.step("email invoice waits for its future PO", self.send_late_invoice)
        self.step(
            "later PO automatically rematches waiting invoice",
            self.check_late_po_reprocess,
        )
        self.step(
            "supporting document upload, list, and view audit",
            self.supporting_documents,
        )
        self.step(
            "supporting document delete requires reason and retains object",
            self.support_delete,
        )
        self.step(
            "audit log entries and ordered invoice timeline", self.check_audit_timeline
        )
        self.step("poll is idempotent", self.check_idempotent_poll)
        self.step("quantity 100 vs PO quantity 80", self.quantity_mismatch)
        self.step(
            "unknown vendor/no PO rejection and sender outbox notification",
            self.unknown_vendor_no_po,
        )
        self.step(
            "low-confidence verification and edit audit", self.low_confidence_verify
        )
        self.step(
            "overdue calculation and command-center buckets", self.overdue_and_buckets
        )
        self.step("reject wrong GL, correct and resubmit", self.reject_correct_resubmit)
        self.step("corrupt PDF isolated in a bulk API upload", self.corrupt_bulk_upload)
        self.step(
            "issue vendor draft send and reply linking", self.issue_draft_send_reply
        )
        self.step(
            "payment idempotency and follow-up creation/stop", self.payment_and_followup
        )
        passed = sum(ok for _, ok, _ in self.results)
        failed = len(self.results) - passed
        print(f"SUMMARY | {passed} passed; {failed} failed", flush=True)
        return 1 if failed else 0

    def _initial_po(self) -> str:
        filename = f"po_primary_{RUN_ID}.pdf"
        body = pdf_bytes(po_payload(vendor=self.vendor_name, po_number=self.po_number))
        self.send_mail(
            build_mail(
                subject=f"Purchase order {self.po_number}",
                message_id=f"<po-{RUN_ID}@e2e.local>",
                attachments=((filename, body, "application/pdf"),),
            )
        )
        polled = self.request("POST", "/api/email-polling/run")
        self.assert_status(polled, 200, "poll PO email")
        document = self._wait_document(filename, {"extracted", "failed"})
        assert document["status"] == "extracted" and document["doc_type"] == "po", (
            document
        )
        assert document["storage_path"].startswith("po/"), document
        self._wait_object(document["storage_path"])
        return f"PO email processed into MinIO {document['storage_path']}"


def wait_for_ports() -> None:
    for host, port, label in (
        (SMTP_HOST, SMTP_PORT, "GreenMail SMTP"),
        (IMAP_HOST, IMAP_PORT, "GreenMail IMAP"),
        (
            MINIO_ENDPOINT.split(":", 1)[0],
            int(MINIO_ENDPOINT.rsplit(":", 1)[-1]),
            "MinIO",
        ),
    ):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((host, port), timeout=2):
                    break
            except OSError:
                time.sleep(1)
        else:
            raise AssertionError(f"{label} did not open its local port")


def main() -> int:
    try:
        wait_for_ports()
        print("PASS | local GreenMail and MinIO ports are ready", flush=True)
    except Exception as exc:
        print(f"FAIL | local dependency readiness | {type(exc).__name__}: {exc}")
        print("SUMMARY | 0 passed; 1 failed", flush=True)
        return 1
    harness = BackendE2E()
    try:
        return harness.run()
    finally:
        harness.client.close()


if __name__ == "__main__":
    raise SystemExit(main())
