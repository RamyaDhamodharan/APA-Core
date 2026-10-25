"""Run sample files through the authenticated API and score against an answer key."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import smtplib
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal, InvalidOperation
from email import policy
from email.parser import BytesParser
from pathlib import Path
from statistics import median
from typing import Any

import httpx

TERMINAL_STATUSES = {
    "processed",
    "extracted",
    "needs_vendor_review",
    "needs_data_review",
    "other",
    "ignored",
    "duplicate_blocked",
    "incomplete",
    "failed",
}
SUPPORTED_UPLOADS = {".pdf", ".png", ".jpg", ".jpeg"}
EVAL_FIELDS = (
    "vendor",
    "invoice_number",
    "invoice_date",
    "due_date",
    "currency",
    "subtotal",
    "tax",
    "total",
    "po_number",
)
MATCH_OUTCOMES = ("matched", "needs_review", "no_match", "rejected")
AMOUNT_FIELDS = {"subtotal", "tax", "total"}


def _load_answer_rows(path: Path) -> dict[str, dict[str, Any]]:
    if path.suffix.casefold() == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as stream:
            reader = csv.DictReader(stream)
            required_columns = {
                "filename",
                *EVAL_FIELDS,
                "expected_match_outcome",
            }
            missing_columns = required_columns - set(reader.fieldnames or [])
            if missing_columns:
                raise ValueError(
                    "Answer-key CSV is missing columns: "
                    + ", ".join(sorted(missing_columns))
                )
            rows = list(reader)
        for row in rows:
            if not row.get("filename"):
                raise ValueError("Each answer-key CSV row needs a filename")
            row["fields"] = {field: row.get(field) for field in EVAL_FIELDS}
            row["match_result"] = row.get("expected_match_outcome") or row.get(
                "match_result"
            )
        return {str(row["filename"]): row for row in rows}
    return _answer_rows(path)


def _normalize_outcome(value: Any) -> str:
    return {
        "ready_to_pay": "matched",
        "match_ok": "matched",
        "match_issues": "needs_review",
        "needs_vendor_review": "needs_review",
        "needs_data_review": "needs_review",
        "no_match": "no_match",
        "rejected": "rejected",
    }.get(str(value or "").casefold(), str(value or "").casefold())


def _validate_quality_key(expected_rows: dict[str, dict[str, Any]]) -> None:
    for filename, row in expected_rows.items():
        outcome = row.get("match_result") or row.get("expected_match_outcome")
        if str(outcome or "").casefold() not in MATCH_OUTCOMES:
            raise ValueError(
                f"{filename}: expected_match_outcome must be one of "
                + ", ".join(MATCH_OUTCOMES)
            )


def score_quality(
    records: list[dict[str, Any]],
    expected_rows: dict[str, dict[str, Any]],
    amount_tolerance: Decimal = Decimal("0.01"),
) -> dict[str, Any]:
    """Score extraction and outcome predictions with stable, testable rules."""
    correct: Counter[str] = Counter()
    totals: Counter[str] = Counter()
    confusion: dict[str, Counter[str]] = defaultdict(Counter)
    band_stats: dict[str, Counter[str]] = defaultdict(Counter)
    errors: dict[str, dict[str, Any]] = {}
    failures = 0
    for record in records:
        expected = expected_rows.get(record["filename"], {})
        fields = expected.get("fields") or {
            key: expected.get(key) for key in EVAL_FIELDS
        }
        actual = record.get("extracted_json") or {}
        band = str(record.get("confidence_band") or "unknown").casefold()
        mismatch = {}
        for name in EVAL_FIELDS:
            target = fields.get(name)
            if target in (None, ""):
                continue
            aliases = {
                "vendor": ("vendor", "vendor_name", "vendor_name_raw"),
                "tax": ("tax", "tax_amount"),
                "total": ("total", "total_amount"),
            }.get(name, (name,))
            got = next((actual[key] for key in aliases if key in actual), None)
            if name == "vendor":
                same = (
                    str(got or "").strip().casefold() == str(target).strip().casefold()
                )
            elif name in AMOUNT_FIELDS:
                try:
                    same = (
                        abs(Decimal(str(got)) - Decimal(str(target)))
                        <= amount_tolerance
                    )
                except (InvalidOperation, TypeError):
                    same = False
            else:
                same = str(got or "").strip() == str(target).strip()
            totals[name] += 1
            correct[name] += int(same)
            if band in {"high", "medium", "low"}:
                band_stats[band]["total"] += 1
                band_stats[band]["correct"] += int(same)
            if not same:
                mismatch[name] = {"expected": target, "actual": got}
        if mismatch or record.get("reason"):
            errors[record["filename"]] = {
                "mismatches": mismatch,
                "failure": record.get("reason"),
            }
        wanted = _normalize_outcome(
            expected.get("match_result") or expected.get("expected_match_outcome")
        )
        got_outcome = _normalize_outcome(
            record.get("match_result") or record.get("status")
        )
        if wanted:
            confusion[wanted][got_outcome or "unknown"] += 1
        if record.get("status") == "failed" or record.get("reason"):
            failures += 1
    classes = set(MATCH_OUTCOMES)
    all_actual = (
        set().union(*(set(row) for row in confusion.values())) if confusion else set()
    )
    matrix = {
        label: {
            actual: confusion[label].get(actual, 0)
            for actual in sorted(classes | all_actual)
        }
        for label in MATCH_OUTCOMES
    }
    totals_matches = sum(sum(row.values()) for row in matrix.values())
    matched_correct = sum(matrix[label].get(label, 0) for label in MATCH_OUTCOMES)

    def precision_recall(label: str) -> dict[str, float | None]:
        tp = matrix[label].get(label, 0)
        fp = sum(
            matrix[expected].get(label, 0)
            for expected in MATCH_OUTCOMES
            if expected != label
        )
        fn = sum(value for actual, value in matrix[label].items() if actual != label)
        return {
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None,
        }

    field_n = sum(totals.values())
    return {
        "fields": {
            name: {
                "correct": correct[name],
                "total": count,
                "accuracy": correct[name] / count if count else None,
            }
            for name, count in sorted(totals.items())
        },
        "field_accuracy": sum(correct.values()) / field_n if field_n else None,
        "match_accuracy": matched_correct / totals_matches if totals_matches else None,
        "match_confusion": matrix,
        "no_match": precision_recall("no_match"),
        "needs_review": precision_recall("needs_review"),
        "extraction_failure_rate": failures / len(records) if records else None,
        "confidence_calibration": {
            band: {
                "correct": values["correct"],
                "total": values["total"],
                "accuracy": values["correct"] / values["total"],
            }
            for band, values in sorted(band_stats.items())
        },
        "mismatches": errors,
    }


def _answer_rows(path: Path) -> dict[str, dict[str, Any]]:
    answer = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(answer, list):
        rows = answer
    elif isinstance(answer, dict) and isinstance(answer.get("cases"), list):
        rows = answer["cases"]
    elif isinstance(answer, dict):
        rows = [dict(value, filename=key) for key, value in answer.items()]
    else:
        raise ValueError("Answer key must be an object or a list of case records")
    return {str(row["filename"]): row for row in rows}


def _api_items(client: httpx.Client, path: str) -> list[dict[str, Any]]:
    response = client.get(path)
    response.raise_for_status()
    return response.json().get("items", [])


def _find_new_email_document(
    client: httpx.Client, previous_ids: set[str], timeout: float
):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = _api_items(client, "/api/documents?page_size=100&source=email_poll")
        added = [row for row in rows if str(row["id"]) not in previous_ids]
        if added:
            return added[0]
        time.sleep(1)
    raise TimeoutError("No document appeared for the submitted email before timeout")


def _submit_email(
    client: httpx.Client, path: Path, args, previous_ids: set[str]
) -> dict[str, Any]:
    if not args.smtp_host:
        raise ValueError(".eml samples require --smtp-host")
    message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
    with smtplib.SMTP(args.smtp_host, args.smtp_port, timeout=20) as smtp:
        smtp.send_message(message)
    poll = client.post("/api/email-polling/run")
    poll.raise_for_status()
    poll_result = poll.json()
    try:
        return _find_new_email_document(client, previous_ids, min(args.timeout, 3))
    except TimeoutError:
        if int(poll_result.get("duplicates", 0)) > 0:
            return {"result": "duplicate", "poll_result": poll_result}
        raise


def _wait_document(client: httpx.Client, document_id: str, timeout: float):
    deadline = time.monotonic() + timeout
    latest = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/documents/{document_id}")
        response.raise_for_status()
        latest = response.json()
        if latest.get("status") in TERMINAL_STATUSES:
            return latest
        time.sleep(1)
    raise TimeoutError(
        f"Document {document_id} did not reach a terminal status; last status="
        f"{latest.get('status') if latest else 'unknown'}"
    )


def _lookup_invoice(client, invoice_number: str | None):
    if not invoice_number:
        return None
    response = client.get(
        "/api/invoices", params={"search": invoice_number, "page_size": 100}
    )
    response.raise_for_status()
    return next(
        (
            row
            for row in response.json().get("items", [])
            if row.get("invoice_no") == invoice_number
        ),
        None,
    )


def _score(
    records: list[dict[str, Any]], expected_rows: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    field_totals: Counter[str] = Counter()
    field_correct: Counter[str] = Counter()
    match_total = match_correct = missed_duplicates = missed_overbilling = 0
    confusion: dict[str, Counter[str]] = defaultdict(Counter)
    for record in records:
        expected = expected_rows.get(record["filename"], {})
        actual_fields = record.get("extracted_json") or {}
        for field, expected_value in (expected.get("fields") or {}).items():
            field_totals[field] += 1
            if actual_fields.get(field) == expected_value:
                field_correct[field] += 1
        expected_match = expected.get("match_result")
        actual_match = record.get("match_result")
        if expected_match is not None:
            match_total += 1
            confusion[str(expected_match)][str(actual_match or "no_invoice")] += 1
            match_correct += expected_match == actual_match
        if expected.get("duplicate") and record.get("result") != "duplicate":
            missed_duplicates += 1
        if expected.get("overbilled") and actual_match not in {
            "match_issues",
            "no_match",
        }:
            missed_overbilling += 1
    fields = {
        name: {
            "correct": field_correct[name],
            "total": total,
            "accuracy": field_correct[name] / total if total else None,
        }
        for name, total in sorted(field_totals.items())
    }
    return {
        "fields": fields,
        "field_accuracy": (
            sum(field_correct.values()) / sum(field_totals.values())
            if field_totals
            else None
        ),
        "match_result_accuracy": match_correct / match_total if match_total else None,
        "match_correct": match_correct,
        "match_total": match_total,
        "missed_duplicates": missed_duplicates,
        "missed_overbilling": missed_overbilling,
        "match_confusion": {
            key: dict(value) for key, value in sorted(confusion.items())
        },
    }


def _write_reports(report_dir: Path, report: dict[str, Any]) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "evaluation.json").write_text(
        json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8"
    )
    lines = [
        "# AP-Core sample evaluation",
        "",
        f"- Bills submitted: {report['submitted']} (terminal: {report['terminal']})",
        f"- Elapsed: {report['elapsed_seconds']:.2f}s",
        (
            f"- Time for 100 bills: {report['time_for_100_seconds']:.2f}s "
            f"({report['time_for_100_basis']})"
        ),
        f"- Field accuracy: {report['scores']['field_accuracy']}",
        f"- Match accuracy: {report['scores']['match_result_accuracy']}",
        f"- Missed duplicates: {report['scores']['missed_duplicates']}",
        f"- Missed overbilling: {report['scores']['missed_overbilling']}",
        f"- Worker concurrency: {report['worker_concurrency']} "
        "(documents worker pool: prefork)",
        "",
        "## Per-field accuracy",
        "",
        "| Field | Correct | Total | Accuracy |",
        "|---|---:|---:|---:|",
    ]
    for field, values in report["scores"]["fields"].items():
        lines.append(
            f"| {field} | {values['correct']} | {values['total']} "
            f"| {values['accuracy']} |"
        )
    lines.extend(
        [
            "",
            "## Match confusion table",
            "",
            "| Expected \\ Actual |"
            + "".join(
                f" {name} |"
                for name in sorted(
                    {
                        col
                        for row in report["scores"]["match_confusion"].values()
                        for col in row
                    }
                )
            ),
            "|---|"
            + "---:|"
            * len(
                {
                    col
                    for row in report["scores"]["match_confusion"].values()
                    for col in row
                }
            ),
        ]
    )
    actuals = sorted(
        {col for row in report["scores"]["match_confusion"].values() for col in row}
    )
    for expected, row in report["scores"]["match_confusion"].items():
        lines.append(
            f"| {expected} |"
            + "".join(f" {row.get(actual, 0)} |" for actual in actuals)
        )
    lines.extend(["", "## Failures", ""])
    lines.extend(
        f"- {failure['filename']}: {failure['reason']}"
        for failure in report["failures"]
    )
    (report_dir / "evaluation.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _database_name(database_url: str) -> str:
    from sqlalchemy.engine import make_url

    return str(make_url(database_url).database or "")


def _validate_eval_safety(
    confirm: bool, database_url: str | None, allowed: str | None
) -> str:
    if not confirm:
        raise ValueError("Evaluation writes test data; pass --confirm to continue")
    if not database_url:
        raise ValueError("DATABASE_URL is not configured")
    name = _database_name(database_url)
    permitted = {value.strip() for value in (allowed or "").split(",") if value.strip()}
    if not name or name not in permitted:
        raise ValueError("Database name is not listed in EVAL_ALLOWED_DATABASES")
    return name


def _confidence_band(document, invoice) -> str:
    values = []
    confidence = getattr(document, "classify_confidence", None)
    if confidence is not None:
        values.append(float(confidence))
    field_confidence = getattr(invoice, "field_confidence", None) or {}
    values.extend(
        float(value)
        for value in field_confidence.values()
        if isinstance(value, (int, float))
    )
    if not values:
        return "unknown"
    value = min(values)
    return "high" if value >= 0.9 else "medium" if value >= 0.75 else "low"


def _process_bill(path: Path, timeout: float) -> dict[str, Any]:
    """Run through the same ingest_file and document processing path as upload."""

    from src.db.session_db import SessionLocal
    from src.models.document_models import Document
    from src.models.invoice_models import Invoice
    from src.models.user_models import User
    from src.services.document_ingest_services import ingest_file

    started = time.monotonic()
    db = SessionLocal()
    document_id = None
    try:
        user = db.query(User).first()
        if user is None:
            raise RuntimeError("Evaluation requires an existing application user")
        try:
            result = ingest_file(
                db, path.name, path.read_bytes(), None, user.id, "upload"
            )
        except Exception as exc:
            return {
                "filename": path.name,
                "status": "failed",
                "reason": f"Ingest pipeline error ({type(exc).__name__})",
                "latency_seconds": time.monotonic() - started,
            }
        if result.document_id is None or result.status != "received":
            return {
                "filename": path.name,
                "status": result.status,
                "reason": result.message,
                "latency_seconds": time.monotonic() - started,
            }
        document_id = result.document_id
    finally:
        db.close()

    deadline = time.monotonic() + timeout
    detail = None
    while time.monotonic() < deadline:
        db = SessionLocal()
        try:
            detail = db.query(Document).filter_by(id=document_id).first()
            if detail is not None and detail.status in TERMINAL_STATUSES:
                extracted = detail.extracted_json or {}
                invoice = (
                    db.query(Invoice).filter_by(source_document_id=document_id).first()
                )
                invoice_status = (
                    getattr(invoice, "match_status", None) if invoice else None
                )
                final_status = getattr(invoice, "status", None) if invoice else None
                outcome = (
                    "no_match"
                    if invoice_status == "no_match"
                    else "matched"
                    if final_status == "ready_to_pay"
                    or invoice_status in {"match_ok", "matched"}
                    else "rejected"
                    if final_status == "rejected"
                    else "needs_review"
                    if invoice
                    else "unknown"
                    if detail.status == "failed"
                    else "needs_review"
                    if detail.status in {"needs_data_review", "needs_vendor_review"}
                    else "no_match"
                )
                confidence_band = (
                    _confidence_band(detail, invoice) if invoice else "unknown"
                )
                return {
                    "filename": path.name,
                    "document_id": str(document_id),
                    "status": detail.status,
                    "failure_message": detail.failure_message,
                    "extracted_json": extracted,
                    "match_result": outcome,
                    "raw_match_status": invoice_status,
                    "confidence_band": confidence_band,
                    "reason": detail.failure_message
                    if detail.status == "failed"
                    else None,
                    "latency_seconds": time.monotonic() - started,
                }
        except Exception as exc:
            return {
                "filename": path.name,
                "document_id": str(document_id),
                "status": "failed",
                "reason": f"Processing result error ({type(exc).__name__})",
                "latency_seconds": time.monotonic() - started,
            }
        finally:
            db.close()
        time.sleep(0.5)
    return {
        "filename": path.name,
        "document_id": str(document_id),
        "status": getattr(detail, "status", "unknown"),
        "reason": "Processing timed out",
        "latency_seconds": time.monotonic() - started,
    }


def _cleanup_evaluation_data(document_ids: list[str]) -> None:
    """Remove rows linked to uploaded docs, plus their stored files."""
    if not document_ids:
        return
    from sqlalchemy import inspect, text

    from src.db.session_db import SessionLocal, engine
    from src.storage.minio_storage import delete_file

    inspector = inspect(engine)
    # Include only rows reachable through foreign keys from core.documents. This keeps
    # cleanup scoped to the documents created by this evaluation run.
    schemas = ["core", None]
    table_info = {}
    for schema in schemas:
        for table in inspector.get_table_names(schema=schema):
            key = (schema, table)
            table_info[key] = {
                "pks": inspector.get_pk_constraint(table, schema=schema).get(
                    "constrained_columns", []
                ),
                "fks": inspector.get_foreign_keys(table, schema=schema),
            }
    root = ("core", "documents")
    import uuid

    records = {root: [(uuid.UUID(value),) for value in document_ids]}
    order = [root]
    scanned = set()
    while True:
        progress = False
        for parent in list(order):
            if parent in scanned:
                continue
            scanned.add(parent)
            parent_values = records[parent]
            parent_schema, parent_name = parent
            for child, info in table_info.items():
                for fk in info["fks"]:
                    target = (
                        fk.get("referred_schema") or child[0],
                        fk.get("referred_table"),
                    )
                    if (
                        target != parent
                        or len(fk.get("constrained_columns", [])) != 1
                        or len(fk.get("referred_columns", [])) != 1
                    ):
                        continue
                    pk_cols = info["pks"]
                    if not pk_cols:
                        continue
                    col = fk["constrained_columns"][0]
                    values = [row[0] for row in parent_values]
                    if not values:
                        continue
                    schema_prefix = f'"{child[0]}".' if child[0] else ""
                    quoted_pks = ", ".join('"' + name + '"' for name in pk_cols)
                    statement = text(
                        f'SELECT {quoted_pks} FROM {schema_prefix}"{child[1]}" '
                        f'WHERE "{col}" = ANY(:values)'
                    )
                    with engine.connect() as connection:
                        found = [
                            tuple(row)
                            for row in connection.execute(
                                statement, {"values": values}
                            ).all()
                        ]
                    if found:
                        existing = records.setdefault(child, [])
                        for row in found:
                            if row not in existing:
                                existing.append(row)
                        if child not in order:
                            order.append(child)
                        progress = True
        if not progress:
            break
    db = SessionLocal()
    try:
        related_ids = [
            value for rows in records.values() for row in rows for value in row
        ]
        if ("core", "audit_log") in table_info and related_ids:
            db.execute(
                text('DELETE FROM "core"."audit_log" WHERE entity_id = ANY(:ids)'),
                {"ids": related_ids},
            )
        if ("core", "follow_ups") in table_info and related_ids:
            db.execute(
                text('DELETE FROM "core"."follow_ups" WHERE entity_id = ANY(:ids)'),
                {"ids": related_ids},
            )
        for schema, table in reversed(order):
            pks = table_info[(schema, table)]["pks"]
            if not pks:
                continue
            schema_prefix = f'"{schema}".' if schema else ""
            for row in records[(schema, table)]:
                where = " AND ".join(
                    f'"{column}" = :pk_{index}' for index, column in enumerate(pks)
                )
                params = {f"pk_{index}": value for index, value in enumerate(row)}
                if (schema, table) == root:
                    result = db.execute(
                        text(
                            f'SELECT storage_path FROM {schema_prefix}"{table}" '
                            f"WHERE {where}"
                        ),
                        params,
                    ).first()
                    if result and result[0]:
                        delete_file(result[0])
                db.execute(
                    text(f'DELETE FROM {schema_prefix}"{table}" WHERE {where}'), params
                )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _write_quality_reports(
    out_dir: Path,
    records: list[dict[str, Any]],
    metrics: dict[str, Any],
    elapsed: float,
) -> dict[str, Any]:
    latencies = [row.get("latency_seconds", 0) for row in records]
    sorted_latency = sorted(latencies)

    def percentile(p: float) -> float:
        if not sorted_latency:
            return 0.0
        if p == 0.5:
            return median(sorted_latency)
        return sorted_latency[
            min(len(sorted_latency) - 1, math.ceil(p * len(sorted_latency)) - 1)
        ]

    count = len(records)
    estimate = elapsed / count * 100 if count else 0
    report = {
        "bills": count,
        "elapsed_seconds": elapsed,
        "bills_per_minute": count * 60 / elapsed if elapsed else 0,
        "p50_latency_seconds": percentile(0.50),
        "p95_latency_seconds": percentile(0.95),
        "target_100_under_5_minutes": ("PASS" if elapsed < 300 else "FAIL")
        if count >= 100
        else ("ESTIMATE PASS" if estimate < 300 else "ESTIMATE FAIL"),
        "target_basis": "measured" if count >= 100 else "linear estimate",
        "estimated_100_seconds": None if count >= 100 else estimate,
        "metrics": metrics,
        "records": records,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(
        json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8"
    )
    lines = [
        "# Extraction and matching evaluation",
        "",
        f"- Bills: {count}",
        f"- Wall time: {elapsed:.2f}s",
        f"- Throughput: {report['bills_per_minute']:.2f} bills/min",
        f"- Per-bill latency p50/p95: {report['p50_latency_seconds']:.2f}s / "
        f"{report['p95_latency_seconds']:.2f}s",
        f"- 100 bills under 5 minutes: "
        f"{report['target_100_under_5_minutes']} ({report['target_basis']})",
        "",
        "## Field accuracy",
        "",
        "| Field | Correct | Total | Accuracy |",
        "|---|---:|---:|---:|",
    ]
    for name, value in metrics["fields"].items():
        lines.append(
            f"| {name} | {value['correct']} | {value['total']} | {value['accuracy']} |"
        )
    lines.extend(
        [
            "",
            f"Overall field accuracy: {metrics['field_accuracy']}",
            f"Match accuracy: {metrics['match_accuracy']}",
            f"Extraction failure rate: {metrics['extraction_failure_rate']}",
            "",
            "## Match confusion matrix",
            "",
            "| Expected \\ Actual |" + "".join(f" {name} |" for name in MATCH_OUTCOMES),
            "|---|" + "---:|" * len(MATCH_OUTCOMES),
        ]
    )
    for expected in MATCH_OUTCOMES:
        lines.append(
            f"| {expected} |"
            + "".join(
                f" {metrics['match_confusion'][expected].get(actual, 0)} |"
                for actual in MATCH_OUTCOMES
            )
        )
    lines.extend(
        [
            "",
            "## Precision and recall",
            "",
            f"- no_match: {metrics['no_match']}",
            f"- needs_review: {metrics['needs_review']}",
            "",
            "## Confidence calibration",
            "",
            json.dumps(metrics["confidence_calibration"], indent=2),
            "",
        ]
    )
    (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    with (out_dir / "per_bill.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=[
                "filename",
                "status",
                "match_result",
                "confidence_band",
                "latency_seconds",
                "mismatches",
                "failure",
            ],
        )
        writer.writeheader()
        for row in records:
            detail = metrics["mismatches"].get(row["filename"], {})
            writer.writerow(
                {
                    "filename": row["filename"],
                    "status": row.get("status"),
                    "match_result": row.get("match_result"),
                    "confidence_band": row.get("confidence_band"),
                    "latency_seconds": row.get("latency_seconds"),
                    "mismatches": json.dumps(detail.get("mismatches", {}), default=str),
                    "failure": detail.get("failure") or row.get("reason") or "",
                }
            )
    return report


def _main_quality(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate extraction and matching against an answer key"
    )
    parser.add_argument("--bills", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("eval/report"))
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--amount-tolerance", type=Decimal, default=Decimal("0.01"))
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args(argv)
    try:
        from src.db.session_db import DATABASE_URL

        _validate_eval_safety(
            args.confirm, DATABASE_URL, os.getenv("EVAL_ALLOWED_DATABASES")
        )
    except Exception as exc:
        parser.error(str(exc))
    if args.concurrency < 1 or args.amount_tolerance < 0:
        parser.error(
            "--concurrency must be positive and --amount-tolerance non-negative"
        )
    if not args.bills.is_dir() or not args.key.is_file():
        parser.error("--bills must be a folder and --key must be a CSV or JSON file")
    expected = _load_answer_rows(args.key)
    try:
        _validate_quality_key(expected)
    except ValueError as exc:
        parser.error(str(exc))
    samples = sorted(
        path
        for path in args.bills.iterdir()
        if path.is_file() and path.suffix.casefold() in SUPPORTED_UPLOADS
    )
    if not samples:
        parser.error("No PDF, PNG, or JPG bills were found")
    missing = {path.name for path in samples} - expected.keys()
    if missing:
        parser.error("Answer key is missing files: " + ", ".join(sorted(missing)))
    records = []
    document_ids = []
    started = time.monotonic()
    try:
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = {
                pool.submit(_process_bill, path, args.timeout): path for path in samples
            }
            for future in as_completed(futures):
                row = future.result()
                records.append(row)
                if row.get("document_id"):
                    document_ids.append(row["document_id"])
    finally:
        _cleanup_evaluation_data(document_ids)
    elapsed = time.monotonic() - started
    records.sort(key=lambda row: row["filename"])
    metrics = score_quality(records, expected, args.amount_tolerance)
    report = _write_quality_reports(args.out, records, metrics, elapsed)
    print(f"Field accuracy: {metrics['field_accuracy']}")
    print(f"Match accuracy: {metrics['match_accuracy']}")
    print(f"No match precision/recall: {metrics['no_match']}")
    print(f"Needs review precision/recall: {metrics['needs_review']}")
    print(f"Extraction failure rate: {metrics['extraction_failure_rate']}")
    print(
        f"Throughput: {report['bills_per_minute']:.2f} bills/min; "
        f"p50 {report['p50_latency_seconds']:.2f}s; "
        f"p95 {report['p95_latency_seconds']:.2f}s"
    )
    print(
        f"{report['target_100_under_5_minutes']}: 100 bills under 5 minutes "
        f"({report['target_basis']})"
    )
    print(
        f"Reports: {args.out / 'report.md'}, {args.out / 'report.json'}, "
        f"{args.out / 'per_bill.csv'}"
    )
    return 0


def main(argv=None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if "--bills" in arguments:
        return _main_quality(arguments)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://localhost:8000")
    parser.add_argument(
        "--token", required=True, help="Bearer token for the evaluation user"
    )
    parser.add_argument("--samples-dir", type=Path, required=True)
    parser.add_argument("--answer-key", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path, default=Path("evaluation-report"))
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--smtp-host")
    parser.add_argument("--smtp-port", type=int, default=3025)
    parser.add_argument("--baseline-report", type=Path)
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Confirm this evaluation may create and then clean test data",
    )
    args = parser.parse_args(argv)
    try:
        from src.db.session_db import DATABASE_URL

        _validate_eval_safety(
            args.confirm, DATABASE_URL, os.getenv("EVAL_ALLOWED_DATABASES")
        )
    except Exception as exc:
        parser.error(str(exc))
    if not args.samples_dir.is_dir() or not args.answer_key.is_file():
        parser.error("--samples-dir must exist and --answer-key must be a file")
    expected = _answer_rows(args.answer_key)
    samples = sorted(
        path
        for path in args.samples_dir.iterdir()
        if path.is_file()
        and (
            path.suffix.casefold() in SUPPORTED_UPLOADS
            or path.suffix.casefold() == ".eml"
        )
    )
    if not samples:
        parser.error("No PDF, image, or EML samples were found")

    started = time.monotonic()
    records: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    uploaded_document_ids: list[str] = []
    headers = {"Authorization": f"Bearer {args.token}"}
    with httpx.Client(
        base_url=args.api_url.rstrip("/"), headers=headers, timeout=30
    ) as client:
        for path in samples:
            record: dict[str, Any] = {"filename": path.name}
            try:
                if path.suffix.casefold() == ".eml":
                    before = {
                        str(row["id"])
                        for row in _api_items(
                            client, "/api/documents?page_size=100&source=email_poll"
                        )
                    }
                    summary = _submit_email(client, path, args, before)
                    if summary.get("result") == "duplicate":
                        record["result"] = "duplicate"
                        records.append(record)
                        continue
                    record["document_id"] = str(summary["id"])
                    uploaded_document_ids.append(record["document_id"])
                else:
                    response = client.post(
                        "/api/documents/upload",
                        files={"file": (path.name, path.read_bytes())},
                    )
                    response.raise_for_status()
                    result = response.json()
                    record["result"] = result.get("results", [{}])[0].get("result")
                    document_id = result.get("id") or result.get("results", [{}])[
                        0
                    ].get("document_id")
                    if not document_id:
                        records.append(record)
                        continue
                    record["document_id"] = document_id
                    if record["result"] == "accepted":
                        uploaded_document_ids.append(str(document_id))
                detail = _wait_document(client, record["document_id"], args.timeout)
                record.update(
                    status=detail.get("status"),
                    doc_type=detail.get("doc_type"),
                    failure_message=detail.get("failure_message"),
                    extracted_json=detail.get("extracted_json") or {},
                    step_log_count=sum(
                        item.get("action") == "pipeline_step"
                        for item in detail.get("audit_history", [])
                    ),
                )
                invoice_number = (record["extracted_json"] or {}).get("invoice_number")
                invoice = _lookup_invoice(client, invoice_number)
                if invoice:
                    record["invoice_id"] = str(invoice["invoice_id"])
                    record["match_result"] = invoice.get("match_status")
                    state = client.get(
                        f"/api/invoices/{invoice['invoice_id']}/state-history"
                    )
                    state.raise_for_status()
                    record["state_log_count"] = len(state.json())
                    timeline = client.get(
                        f"/api/invoices/{invoice['invoice_id']}/timeline"
                    )
                    timeline.raise_for_status()
                    record["step_log_count"] = sum(
                        bool(item.get("step_name")) for item in timeline.json()
                    )
                    if not record["state_log_count"] or not record["step_log_count"]:
                        failures.append(
                            {
                                "filename": path.name,
                                "reason": (
                                    "Bill is missing a state history or "
                                    "pipeline step log"
                                ),
                            }
                        )
                records.append(record)
                if detail.get("status") not in TERMINAL_STATUSES:
                    failures.append(
                        {
                            "filename": path.name,
                            "reason": "Document did not reach a terminal status",
                        }
                    )
                elif detail.get("status") == "failed":
                    failures.append(
                        {
                            "filename": path.name,
                            "reason": detail.get("failure_message")
                            or "Processing failed",
                        }
                    )
            except Exception as exc:
                record["reason"] = f"{type(exc).__name__}: {exc}"
                records.append(record)
                failures.append({"filename": path.name, "reason": record["reason"]})
    _cleanup_evaluation_data(uploaded_document_ids)
    elapsed = time.monotonic() - started
    score = _score(records, expected)
    count = len(samples)
    report = {
        "submitted": count,
        "terminal": sum(row.get("status") in TERMINAL_STATUSES for row in records),
        "elapsed_seconds": elapsed,
        "time_for_100_seconds": elapsed if count == 100 else (elapsed / count * 100),
        "time_for_100_basis": "measured 100-bill run"
        if count == 100
        else "linear estimate; run with 100 samples for a measured value",
        "worker_concurrency": int(__import__("os").getenv("WORKER_CONCURRENCY", "2")),
        "throughput_before_seconds": None,
        "throughput_after_seconds": elapsed if count == 100 else None,
        "baseline_report": str(args.baseline_report) if args.baseline_report else None,
        "scores": score,
        "state_and_step_log_coverage": {
            "bills": sum(bool(row.get("invoice_id")) for row in records),
            "with_state_log": sum(bool(row.get("state_log_count")) for row in records),
            "with_step_log": sum(bool(row.get("step_log_count")) for row in records),
        },
        "failures": failures,
        "records": records,
        "targets": {
            "field_accuracy": 0.95,
            "match_accuracy": 0.95,
            "missed_duplicates": 0,
            "seconds_for_100": 300,
        },
    }
    if args.baseline_report and args.baseline_report.is_file():
        baseline = json.loads(args.baseline_report.read_text(encoding="utf-8"))
        report["throughput_before_seconds"] = baseline.get("time_for_100_seconds")
    _write_reports(args.report_dir, report)
    print(f"Evaluation report: {args.report_dir / 'evaluation.md'}")
    print(f"Evaluation JSON: {args.report_dir / 'evaluation.json'}")
    print(
        "PASS coverage: "
        f"state={report['state_and_step_log_coverage']['with_state_log']}/"
        f"{report['state_and_step_log_coverage']['bills']}, step="
        f"{report['state_and_step_log_coverage']['with_step_log']}/"
        f"{report['state_and_step_log_coverage']['bills']}"
    )
    thresholds_met = (
        (score["field_accuracy"] or 0) >= 0.95
        and (score["match_result_accuracy"] or 0) >= 0.95
        and score["missed_duplicates"] == 0
        and score["missed_overbilling"] == 0
        and report["time_for_100_seconds"] < 300
        and report["state_and_step_log_coverage"]["with_state_log"]
        == report["state_and_step_log_coverage"]["bills"]
        and report["state_and_step_log_coverage"]["with_step_log"]
        == report["state_and_step_log_coverage"]["bills"]
        and not failures
    )
    print(f"{'PASS' if thresholds_met else 'FAIL'} evaluation targets")
    return 0 if thresholds_met else 1


if __name__ == "__main__":
    raise SystemExit(main())
