"""
Generate 100 synthetic benchmark bills and ground-truth answer key for confidence calibration.
Covers:
- Clean bills (high confidence)
- Math discrepancies (capped at 60)
- Unknown vendors (capped at 50)
- Format errors (capped at 40)
- Blurry/degraded bills (penalized)
"""

import csv
import json
import os
import random

EVAL_DIR = os.path.join(os.path.dirname(__file__), "..", "eval")
SAMPLES_DIR = os.path.join(EVAL_DIR, "calibration_samples")
ANSWER_KEY_PATH = os.path.join(EVAL_DIR, "answer_key.csv")

os.makedirs(SAMPLES_DIR, exist_ok=True)

APPROVED_VENDORS = [
    "Apex Industrial Solutions",
    "Nexus Tech Hardware",
    "Continental Logistics",
    "Pinnacle Office Products",
    "Sterling Engineering",
    "Quantum Software Labs",
    "Beacon Facilities Corp",
    "Vanguard Raw Materials",
]

GL_CODES = ["5000", "5100", "6000", "6100", "6200", "6300", "6400", "6500", "7000"]


def generate_samples():
    random.seed(42)
    rows = []

    for i in range(1, 101):
        filename = f"bill_{i:03d}.json"
        vendor = random.choice(APPROVED_VENDORS)
        inv_no = f"INV-2026-{i:04d}"
        inv_date = "2026-10-15"
        due_date = "2026-11-14"
        po_num = f"PO-{random.randint(100, 999)}"
        gl_code = random.choice(GL_CODES)

        qty = random.randint(2, 10)
        unit_price = round(random.uniform(15.0, 150.0), 2)
        subtotal = round(qty * unit_price, 2)
        tax = round(subtotal * 0.10, 2)
        total = round(subtotal + tax, 2)

        # Ground truth
        truth = {
            "filename": filename,
            "vendor": vendor,
            "invoice_number": inv_no,
            "invoice_date": inv_date,
            "due_date": due_date,
            "currency": "USD",
            "subtotal": f"{subtotal:.2f}",
            "tax": f"{tax:.2f}",
            "total": f"{total:.2f}",
            "po_number": po_num,
            "gl_code": gl_code,
            "is_blurry": False,
            "anomaly_type": "clean",
        }

        # Extracted data payload
        extracted = {
            "document_type": "invoice",
            "vendor": vendor,
            "invoice_number": inv_no,
            "invoice_date": inv_date,
            "due_date": due_date,
            "currency": "USD",
            "subtotal": subtotal,
            "tax": tax,
            "total": total,
            "order_number": po_num,
            "gl_code": gl_code,
            "line_items": [
                {
                    "description": f"Standard Parts Lot #{i}",
                    "qty": qty,
                    "unit_price": unit_price,
                    "line_amount": subtotal,
                }
            ],
            "_meta": {
                "ocr_quality": 96.0,
                "is_blurry": False,
            },
        }

        # Inject archetypes
        if 71 <= i <= 78:
            # Math mismatch: total does not match subtotal + tax
            truth["anomaly_type"] = "math_mismatch"
            extracted["total"] = round(total + 15.00, 2)  # Corrupted total

        elif 79 <= i <= 84:
            # Unknown vendor not in master data
            truth["anomaly_type"] = "unknown_vendor"
            unknown_name = f"Unregistered Entity #{i}"
            truth["vendor"] = unknown_name
            extracted["vendor"] = unknown_name

        elif 85 <= i <= 88:
            # Blurry scan: reduced readability
            truth["anomaly_type"] = "blurry_scan"
            truth["is_blurry"] = True
            extracted["_meta"]["is_blurry"] = True
            extracted["_meta"]["ocr_quality"] = 65.0

        elif 89 <= i <= 92:
            # Invalid date format
            truth["anomaly_type"] = "format_error_date"
            extracted["due_date"] = "INVALID-DATE-STRING"

        elif 93 <= i <= 95:
            # Line math mismatch
            truth["anomaly_type"] = "line_math_mismatch"
            extracted["line_items"][0]["line_amount"] = round(subtotal + 25.00, 2)

        elif 96 <= i <= 100:
            # Slightly noisy OCR
            truth["anomaly_type"] = "noisy_ocr"
            extracted["_meta"]["ocr_quality"] = 82.0

        # Save sample JSON
        sample_path = os.path.join(SAMPLES_DIR, filename)
        with open(sample_path, "w", encoding="utf-8") as f:
            json.dump(extracted, f, indent=2)

        rows.append(truth)

    # Write answer key CSV
    fieldnames = [
        "filename",
        "vendor",
        "invoice_number",
        "invoice_date",
        "due_date",
        "currency",
        "subtotal",
        "tax",
        "total",
        "po_number",
        "gl_code",
        "anomaly_type",
    ]
    with open(ANSWER_KEY_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    print(f"Generated 100 calibration bills in {SAMPLES_DIR}")
    print(f"Written ground-truth answer key to {ANSWER_KEY_PATH}")


if __name__ == "__main__":
    generate_samples()
