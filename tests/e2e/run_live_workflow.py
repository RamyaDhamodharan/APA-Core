"""Run authenticated HTTP E2E scenarios against the isolated local Compose stack."""

from __future__ import annotations

import base64
import json
import os
import random
import string
import time
import uuid
from collections.abc import Callable

import httpx
import psycopg

AUTH_URL = os.getenv("E2E_AUTH_URL", "http://e2e-auth:8000")
CORE_URL = os.getenv("E2E_CORE_URL", "http://e2e-core:8000")
DB_URL = os.getenv(
    "E2E_DB_URL",
    "postgresql://ap_e2e_user:e2e-only-postgres-password@"
    "e2e-postgres:5432/genworx_ap_test",
)
PASSWORD = "e2e-local-user-password-only"
RUN_ID = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))


def _pdf(payload: dict) -> bytes:
    encoded = base64.b64encode(json.dumps(payload, separators=(",", ":")).encode())
    chunks = [encoded[i : i + 96] for i in range(0, len(encoded), 96)]
    commands = ["BT /F1 8 Tf 35 800 Td (GW_AP_E2E_JSON_BEGIN) Tj"]
    for chunk in chunks:
        safe = chunk.decode("ascii")
        commands.append(f"0 -12 Td ({safe}) Tj")
    commands.extend(["0 -12 Td (GW_AP_E2E_JSON_END) Tj", "ET"])
    stream = "\n".join(commands).encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length "
        + str(len(stream)).encode()
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
    ]
    result = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f"{index} 0 obj\n".encode())
        result.extend(obj)
        result.extend(b"\nendobj\n")
    xref_offset = len(result)
    result.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    result.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        result.extend(f"{offset:010d} 00000 n \n".encode())
    result.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode()
    )
    return bytes(result)


def _base_docs(po_number: str, vendor: str, invoice_number: str, gr_number: str):
    line = {
        "item_code": "E2E-ITEM",
        "description": "Synthetic E2E widget",
        "uom": "EA",
        "qty": 5,
        "unit_price": 20,
        "line_amount": 100,
    }
    return {
        "po": {
            "document_type": "po",
            "vendor": vendor,
            "order_number": po_number,
            "po_date": "2026-10-20",
            "currency": "USD",
            "subtotal": 100,
            "tax": 0,
            "total": 100,
            "line_items": [dict(line)],
        },
        "gr": {
            "document_type": "goods_receipt",
            "vendor": vendor,
            "gr_number": gr_number,
            "order_number": po_number,
            "receipt_date": "2026-10-20",
            "line_items": [
                {
                    "item_code": line["item_code"],
                    "description": line["description"],
                    "uom": line["uom"],
                    "received_qty": 5,
                    "accepted_qty": 5,
                    "rejected_qty": 0,
                }
            ],
        },
        "invoice": {
            "document_type": "invoice",
            "vendor": vendor,
            "invoice_number": invoice_number,
            "order_number": po_number,
            "invoice_date": "2026-10-20",
            "due_date": "2026-11-20",
            "currency": "USD",
            "subtotal": 100,
            "tax": 0,
            "total": 100,
            "line_items": [dict(line)],
        },
    }


class E2E:
    def __init__(self) -> None:
        self.client = httpx.Client(timeout=30)
        self.results: list[tuple[str, str, str]] = []
        self.email = f"e2e-{RUN_ID}@example.com"
        self._wait_for_services()
        response = self.client.post(
            f"{AUTH_URL}/api/auth/register",
            json={
                "email": self.email,
                "password": PASSWORD,
                "full_name": f"E2E User {RUN_ID}",
            },
        )
        self._expect(response, 201, "test user registration")
        response = self.client.post(
            f"{AUTH_URL}/api/auth/login",
            json={"email": self.email, "password": PASSWORD},
        )
        self._expect(response, 200, "test user login")
        self.token = response.json()["access_token"]
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def _wait_for_services(self) -> None:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                auth = self.client.get(f"{AUTH_URL}/health")
                core = self.client.get(f"{CORE_URL}/health")
                if auth.status_code == 200 and core.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(1)
        raise AssertionError("isolated Auth/Core health endpoints did not become ready")

    def _expect(self, response: httpx.Response, code: int, context: str) -> None:
        if response.status_code != code:
            raise AssertionError(
                f"{context}: expected HTTP {code}, got {response.status_code}: "
                f"{response.text[:500]}"
            )

    def request(self, method: str, path: str, *, auth=True, **kwargs):
        headers = dict(kwargs.pop("headers", {}))
        if auth:
            headers.update(self.headers)
        response = self.client.request(
            method, f"{CORE_URL}{path}", headers=headers, **kwargs
        )
        return response

    def vendor(self, label: str) -> str:
        name = f"E2E {label} {RUN_ID}"
        response = self.request("POST", "/api/vendors", json={"name": name})
        self._expect(response, 201, "create synthetic vendor")
        return name

    def upload_bytes(self, content: bytes, filename: str) -> dict:
        response = self.request(
            "POST",
            "/api/documents/upload",
            files={"file": (filename, content, "application/pdf")},
        )
        self._expect(response, 201, "upload synthetic document")
        return response.json()

    def upload(self, payload: dict, label: str) -> tuple[str, dict, bytes]:
        content = _pdf(payload)
        uploaded = self.upload_bytes(content, f"{RUN_ID}-{label}.pdf")
        document_id = uploaded["id"]
        parsed = self._wait_for_background_processing(document_id, label)
        if parsed.get("status") != "extracted":
            raise AssertionError(
                f"{label} processing ended as {parsed.get('status')}: "
                f"{parsed.get('failure_message')}"
            )
        response = self.request("POST", f"/api/documents/{document_id}/parse")
        self._expect(response, 200, f"read processed {label}")
        return document_id, response.json(), content

    def workflow(self, document_id: str) -> dict:
        response = self.request("GET", f"/api/document-workflows/{document_id}")
        self._expect(response, 200, "get document workflow")
        return response.json()

    def invoice_id(self, document_id: str, invoice_number: str) -> str:
        response = self.request(
            "GET", "/api/invoices", params={"search": invoice_number}
        )
        self._expect(response, 200, "find invoice through invoice API")
        for item in response.json()["items"]:
            if item.get("invoice_no") == invoice_number:
                return item["invoice_id"]
        raise AssertionError("invoice record was not exposed by workflow API")

    def match_invoice(self, invoice_id: str) -> dict:
        response = self.request("POST", f"/api/invoices/{invoice_id}/three-way-match")
        self._expect(response, 200, "run three-way matching")
        return response.json()

    def db(self, query: str, params: tuple = ()):
        with psycopg.connect(DB_URL) as connection, connection.cursor() as cursor:
            cursor.execute(query, params)
            return cursor.fetchall()

    def run_case(self, name: str, case: Callable[[], str]) -> None:
        try:
            evidence = case()
            self.results.append((name, "PASS", evidence))
        except Exception as exc:
            self.results.append((name, "FAIL", f"{type(exc).__name__}: {exc}"))

    def triple(self, order: tuple[str, str, str], label: str):
        vendor = self.vendor(label)
        po_no = f"PO-{RUN_ID}-{label}"
        inv_no = f"INV-{RUN_ID}-{label}"
        docs = _base_docs(po_no, vendor, inv_no, f"GR-{RUN_ID}-{label}")
        parsed = {}
        contents = {}
        for kind in order:
            doc_id, result, content = self.upload(docs[kind], f"{label}-{kind}")
            if result["status"] != "extracted":
                raise AssertionError(
                    f"{kind} extraction status was {result['status']}; "
                    f"issues={result.get('validation_issues')}"
                )
            parsed[kind] = doc_id
            contents[kind] = content
        workflow = self.workflow(parsed["invoice"])
        match = self.match_invoice(self.invoice_id(parsed["invoice"], inv_no))
        if workflow["status"] != "matched" or match.get("status") != "matched":
            raise AssertionError(
                f"expected matched, got workflow={workflow['status']} "
                f"matching={match.get('status')}"
            )
        if not match.get("invoice_approval_required"):
            raise AssertionError("match response did not retain invoice approval")
        final = self.db(
            "SELECT final_status::text FROM core.invoices WHERE invoice_no = %s",
            (inv_no,),
        )
        if final != [(None,)]:
            raise AssertionError(f"invoice was unexpectedly finalized: {final}")
        return parsed, docs, contents, workflow, match

    def scenarios(self) -> None:
        unauth = self.request("GET", "/api/purchase-orders", auth=False)
        self.results.append(
            (
                "Unauthorized API access",
                "PASS" if unauth.status_code == 401 else "FAIL",
                f"GET purchase orders returned HTTP {unauth.status_code}",
            )
        )

        for order, label in (
            (("po", "gr", "invoice"), "forward"),
            (("invoice", "po", "gr"), "invoice-first"),
            (("gr", "invoice", "po"), "receipt-first"),
        ):
            self.run_case(
                f"Successful upload order {label}",
                lambda order=order, label=label: self._matched_case(order, label),
            )

        self.run_case("PO + Invoice missing GR", self._missing_gr)
        self.run_case("PO + GR missing Invoice", self._missing_invoice)
        self.run_case(
            "Vendor/currency/total/item/unit/price/quantity mismatches",
            self._combined_mismatches,
        )
        self.run_case("Invoice PO reference mismatch", self._po_reference_mismatch)
        self.run_case(
            "Partial receipt and missing evidence", self._partial_and_missing_evidence
        )
        self.run_case("Later GR upload and matching retry", self._later_gr_and_retry)
        self.run_case(
            "Duplicate upload and idempotent parse", self._duplicate_and_idempotent
        )
        self.run_case("Human decision and persisted audit", self._human_decision_audit)

    def _matched_case(self, order, label):
        parsed, _, _, workflow, match = self.triple(order, label)
        docs_in_db = self.db(
            "SELECT count(*) FROM core.documents WHERE id = ANY(%s::uuid[])",
            (list(parsed.values()),),
        )[0][0]
        if docs_in_db != 3:
            raise AssertionError(
                f"expected 3 persisted source docs, found {docs_in_db}"
            )
        return (
            f"workflow={workflow['status']}; match={match['status']}; "
            f"persisted_documents={docs_in_db}; approval remains pending"
        )

    def _missing_gr(self):
        vendor = self.vendor("missing-gr")
        po_no, inv_no = f"PO-{RUN_ID}-MGR", f"INV-{RUN_ID}-MGR"
        docs = _base_docs(po_no, vendor, inv_no, f"GR-{RUN_ID}-MGR")
        self.upload(docs["po"], "missing-gr-po")
        inv_id, parsed, _ = self.upload(docs["invoice"], "missing-gr-invoice")
        wf = self.workflow(inv_id)
        if (
            wf["status"] == "matched"
            or "goods_receipt" not in wf["missing_document_types"]
        ):
            raise AssertionError(f"missing GR was not reported: {wf}")
        return f"status={wf['status']}; missing={wf['missing_document_types']}"

    def _missing_invoice(self):
        vendor = self.vendor("missing-invoice")
        po_no = f"PO-{RUN_ID}-MINV"
        docs = _base_docs(po_no, vendor, f"INV-{RUN_ID}-MINV", f"GR-{RUN_ID}-MINV")
        po_id, _, _ = self.upload(docs["po"], "missing-invoice-po")
        self.upload(docs["gr"], "missing-invoice-gr")
        wf = self.workflow(po_id)
        if wf["status"] == "matched" or "invoice" not in wf["missing_document_types"]:
            raise AssertionError(f"missing invoice was not reported: {wf}")
        return f"status={wf['status']}; missing={wf['missing_document_types']}"

    def _combined_mismatches(self):
        vendor = self.vendor("mismatch-po-vendor")
        other = self.vendor("mismatch-invoice-vendor")
        po_no, inv_no = f"PO-{RUN_ID}-MM", f"INV-{RUN_ID}-MM"
        po_lines = [
            {
                "item_code": "M-A",
                "description": "Price",
                "uom": "EA",
                "qty": 5,
                "unit_price": 10,
                "line_amount": 50,
            },
            {
                "item_code": "M-B",
                "description": "Qty",
                "uom": "EA",
                "qty": 5,
                "unit_price": 10,
                "line_amount": 50,
            },
            {
                "item_code": "M-C",
                "description": "Unit",
                "uom": "KG",
                "qty": 3,
                "unit_price": 5,
                "line_amount": 15,
            },
            {
                "item_code": "M-D",
                "description": "Item",
                "uom": "EA",
                "qty": 1,
                "unit_price": 5,
                "line_amount": 5,
            },
        ]
        po = {
            "document_type": "po",
            "vendor": vendor,
            "order_number": po_no,
            "po_date": "2026-10-20",
            "currency": "USD",
            "subtotal": 120,
            "tax": 0,
            "total": 120,
            "line_items": po_lines,
        }
        gr = {
            "document_type": "goods_receipt",
            "vendor": vendor,
            "gr_number": f"GR-{RUN_ID}-MM",
            "order_number": po_no,
            "receipt_date": "2026-10-20",
            "line_items": [
                {
                    "item_code": row["item_code"],
                    "description": row["description"],
                    "uom": row["uom"],
                    "received_qty": row["qty"],
                    "accepted_qty": row["qty"],
                    "rejected_qty": 0,
                }
                for row in po_lines
            ],
        }
        invoice_lines = [
            {**po_lines[0], "unit_price": 11, "line_amount": 55},
            {**po_lines[1], "qty": 6, "line_amount": 60},
            {**po_lines[2], "uom": "EA"},
            {**po_lines[3], "item_code": "M-UNKNOWN"},
        ]
        invoice = {
            "document_type": "invoice",
            "vendor": other,
            "invoice_number": inv_no,
            "order_number": po_no,
            "invoice_date": "2026-10-20",
            "currency": "EUR",
            "subtotal": 135,
            "tax": 0,
            "total": 135,
            "line_items": invoice_lines,
        }
        self.upload(po, "mismatch-po")
        self.upload(gr, "mismatch-gr")
        inv_doc, _, _ = self.upload(invoice, "mismatch-invoice")
        wf = self.workflow(inv_doc)
        checks = {(item["name"], item["outcome"]) for item in wf["checks"]}
        expected = {
            ("vendor", "mismatch"),
            ("currency", "mismatch"),
            ("document_total", "mismatch"),
            ("unit_price", "mismatch"),
            ("unit_of_measure", "mismatch"),
            ("invoice_vs_ordered_quantity", "mismatch"),
            ("invoice_line_link", "not_possible"),
        }
        missing = expected - checks
        if wf["status"] == "matched" or missing:
            raise AssertionError(
                f"mismatch evidence missing={missing}; status={wf['status']}"
            )
        return f"status={wf['status']}; checked={sorted(expected)}"

    def _po_reference_mismatch(self):
        vendor = self.vendor("reference")
        po_no, inv_no = f"PO-{RUN_ID}-REF", f"INV-{RUN_ID}-REF"
        docs = _base_docs(po_no, vendor, inv_no, f"GR-{RUN_ID}-REF")
        self.upload(docs["po"], "reference-po")
        self.upload(docs["gr"], "reference-gr")
        docs["invoice"]["order_number"] = f"NO-SUCH-PO-{RUN_ID}"
        inv_id, _, _ = self.upload(docs["invoice"], "reference-invoice")
        wf = self.workflow(inv_id)
        if wf["status"] == "matched" or "po" not in wf["missing_document_types"]:
            raise AssertionError(f"unrelated PO was associated: {wf}")
        return (
            f"status={wf['status']}; missing={wf['missing_document_types']}; "
            "no false PO link"
        )

    def _partial_and_missing_evidence(self):
        vendor = self.vendor("partial")
        po_no, inv_no = f"PO-{RUN_ID}-PART", f"INV-{RUN_ID}-PART"
        docs = _base_docs(po_no, vendor, inv_no, f"GR-{RUN_ID}-PART")
        docs["gr"]["line_items"][0].update(received_qty=3, accepted_qty=3)
        docs["invoice"].update(subtotal=60, total=60)
        docs["invoice"]["line_items"][0].update(qty=3, line_amount=60)
        self.upload(docs["po"], "partial-po")
        self.upload(docs["gr"], "partial-gr")
        inv_doc, _, _ = self.upload(docs["invoice"], "partial-invoice")
        wf = self.workflow(inv_doc)
        if wf["status"] != "partial_match" or not any(
            c["name"] == "partial_receipt" and c["outcome"] == "passed"
            for c in wf["checks"]
        ):
            raise AssertionError(f"partial delivery evidence missing: {wf}")

        vendor2 = self.vendor("missing-lines")
        po_no2, inv_no2 = f"PO-{RUN_ID}-EVID", f"INV-{RUN_ID}-EVID"
        docs2 = _base_docs(po_no2, vendor2, inv_no2, f"GR-{RUN_ID}-EVID")
        docs2["invoice"].pop("line_items")
        self.upload(docs2["po"], "evidence-po")
        self.upload(docs2["gr"], "evidence-gr")
        inv_doc2, _, _ = self.upload(docs2["invoice"], "evidence-invoice")
        wf2 = self.workflow(inv_doc2)
        if wf2["status"] == "matched" or not any(
            c["outcome"] == "not_possible" for c in wf2["checks"]
        ):
            raise AssertionError(f"missing line evidence was treated as a match: {wf2}")
        return f"partial={wf['status']}; missing line evidence={wf2['status']}"

    def _later_gr_and_retry(self):
        vendor = self.vendor("late-gr")
        po_no, inv_no = f"PO-{RUN_ID}-LATE", f"INV-{RUN_ID}-LATE"
        docs = _base_docs(po_no, vendor, inv_no, f"GR-{RUN_ID}-LATE")
        self.upload(docs["po"], "late-po")
        inv_doc, _, _ = self.upload(docs["invoice"], "late-invoice")
        before = self.workflow(inv_doc)
        if (
            before["status"] == "matched"
            or "goods_receipt" not in before["missing_document_types"]
        ):
            raise AssertionError(f"initial missing GR was not reported: {before}")
        self.upload(docs["gr"], "late-gr")
        response = self.request(
            "POST",
            f"/api/document-workflows/{inv_doc}/decisions",
            json={"action": "retry_match", "reason": "E2E later document retry"},
        )
        self._expect(response, 200, "retry workflow matching")
        after = self.workflow(inv_doc)
        if after["status"] != "matched":
            raise AssertionError(f"retry did not resolve workflow: {after}")
        if len(after["available_documents"]) < 3:
            raise AssertionError("retry did not retain and resolve existing documents")
        return (
            f"before={before['status']}; after={after['status']}; "
            "existing documents retained"
        )

    def _duplicate_and_idempotent(self):
        vendor = self.vendor("duplicate")
        po_no, inv_no = f"PO-{RUN_ID}-DUP", f"INV-{RUN_ID}-DUP"
        docs = _base_docs(po_no, vendor, inv_no, f"GR-{RUN_ID}-DUP")
        content = _pdf(docs["po"])
        first = self.upload_bytes(content, f"{RUN_ID}-duplicate.pdf")
        second = self.upload_bytes(content, f"{RUN_ID}-duplicate.pdf")
        if first["id"] != second["id"] or second["status"] != "duplicate_blocked":
            raise AssertionError(
                f"duplicate upload was not idempotently blocked: {second}"
            )
        self.upload(docs["gr"], "duplicate-gr")
        po_doc_id = first["id"]
        parsed = self._wait_parse(po_doc_id)
        again = self.request("POST", f"/api/documents/{po_doc_id}/parse")
        self._expect(again, 200, "repeat parse request")
        row = self.db(
            "SELECT count(*) FROM core.purchase_orders WHERE source_document_id = %s",
            (po_doc_id,),
        )
        source_count = self.db(
            "SELECT count(*) FROM core.documents WHERE file_hash = "
            "(SELECT file_hash FROM core.documents WHERE id = %s)",
            (po_doc_id,),
        )[0][0]
        if row[0][0] != 1 or source_count != 1 or parsed.get("status") != "extracted":
            raise AssertionError("duplicate/retry processing created duplicate records")
        return (
            "duplicate upload reused document; repeat parse preserved one PO/source row"
        )

    def _wait_parse(self, document_id: str) -> dict:
        result = self._wait_for_background_processing(document_id, "duplicate PO")
        if result.get("status") != "extracted":
            raise AssertionError(
                f"duplicate PO processing ended as {result.get('status')}"
            )
        response = self.request("POST", f"/api/documents/{document_id}/parse")
        self._expect(response, 200, "idempotent parse read")
        return response.json()

    def _wait_for_background_processing(self, document_id: str, label: str) -> dict:
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            rows = self.db(
                "SELECT status::text, failure_message "
                "FROM core.documents WHERE id = %s",
                (document_id,),
            )
            if rows and rows[0][0] in {
                "extracted",
                "needs_vendor_review",
                "failed",
            }:
                return {"status": rows[0][0], "failure_message": rows[0][1]}
            time.sleep(2)
        raise AssertionError(f"background processing timed out for {label}")

    def _human_decision_audit(self):
        vendor = self.vendor("human")
        po_no = f"PO-{RUN_ID}-HUMAN"
        docs = _base_docs(po_no, vendor, f"INV-{RUN_ID}-HUMAN", f"GR-{RUN_ID}-HUMAN")
        po_doc, _, _ = self.upload(docs["po"], "human-po")
        self.upload(docs["gr"], "human-gr")
        response = self.request(
            "POST",
            f"/api/document-workflows/{po_doc}/decisions",
            json={"action": "hold", "reason": "E2E human review hold"},
        )
        self._expect(response, 200, "persist human hold decision")
        result = self.workflow(po_doc)
        state = self.db(
            "SELECT decision, decision_by, decided_at IS NOT NULL "
            "FROM core.document_workflows WHERE document_id = %s",
            (po_doc,),
        )
        audits = self.db(
            "SELECT count(*) FROM core.audit_log "
            "WHERE entity_type = 'document_workflow' "
            "AND entity_id = %s AND action = 'document_workflow_hold'",
            (po_doc,),
        )[0][0]
        if (
            result.get("decision") != "hold"
            or state != [("hold", uuid.UUID(self.token_subject()), True)]
            or audits < 1
        ):
            raise AssertionError("human decision or actor/audit row was not persisted")
        return f"decision={state[0][0]}; actor persisted; audit rows={audits}"

    def token_subject(self) -> str:
        response = self.client.get(f"{AUTH_URL}/api/users/me", headers=self.headers)
        self._expect(response, 200, "authenticated profile")
        return response.json()["id"]

    def print_results(self) -> int:
        for name, status, evidence in self.results:
            print(f"{status:4} | {name} | {evidence}")
        passed = sum(status == "PASS" for _, status, _ in self.results)
        failed = sum(status == "FAIL" for _, status, _ in self.results)
        print(f"TOTAL | {passed} passed; {failed} failed")
        return 1 if failed else 0


def main() -> int:
    harness = E2E()
    harness.scenarios()
    return harness.print_results()


if __name__ == "__main__":
    raise SystemExit(main())
