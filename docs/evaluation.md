# Extraction and matching evaluation

The evaluator runs the normal Core document ingest and processing pipeline against a controlled sample corpus. It writes evaluation data to the database and MinIO during the run, then removes rows reachable from its uploaded documents and deletes the corresponding stored files.

## Build an answer key

Put PDF, PNG, or JPG bills in one folder. Copy `eval/answer_key.template.csv`, replace the synthetic rows with one row for each filename, and save it outside the template path (for example `eval/answer_key.csv`). The template values are synthetic examples; matching bill images are not included. JSON is also accepted as a list of objects or `{"cases": [...]}`. JSON records may put expected extraction fields under `fields` and the outcome under `match_result`.

The fields are `vendor`, `invoice_number`, `invoice_date`, `due_date`, `currency`, `subtotal`, `tax`, `total`, and `po_number`. The expected outcome must be `matched`, `needs_review`, `no_match`, or `rejected`. Blank fields are excluded from extraction accuracy. Identifiers, dates, and currency compare exactly; vendor names compare without case sensitivity. Monetary values use a default absolute tolerance of 0.01. Override it with `--amount-tolerance`.

## Run

Set `EVAL_ALLOWED_DATABASES` in the Core container environment to a comma-separated list containing the exact database name intended for evaluation. The evaluator checks the database name parsed from `DATABASE_URL`; it does not print the URL. `--confirm` is required because processing writes test records. Use an isolated evaluation database and a non-production MinIO bucket where possible.

From the repository root, copy the corpus and answer key into the Core container, then run:

```sh
docker compose exec core mkdir -p /app/eval
docker compose cp ./sample-bills core:/app/eval/bills
docker compose cp ./answer_key.csv core:/app/eval/answer_key.csv
docker compose exec core uv run --no-sync python -m scripts.evaluate \
  --bills eval/bills --key eval/answer_key.csv --out eval/report \
  --concurrency 2 --confirm
```

Use `--concurrency N` to control parallel bills. The safety gate requires both `--confirm` and an exact database-name match in `EVAL_ALLOWED_DATABASES`. The run uses an existing application user as the uploader so the regular processing path can run. The legacy API evaluator remains available with `--api-url`, `--token`, `--samples-dir`, `--answer-key`, and `--report-dir`; it also requires `--confirm` and the allowlist gate and cleans up newly accepted uploads.

## Read the report

`report.md` summarizes field accuracy, match accuracy and confusion matrix, no-match and needs-review precision/recall, extraction failure rate, confidence calibration, throughput, latency percentiles, and the 100-bills-in-five-minutes target. For fewer than 100 bills, the target is explicitly labeled as a linear estimate. `report.json` contains the full metrics and per-bill records. `per_bill.csv` lists each file and any expected/actual mismatches or processing failure, for review in a spreadsheet.

Amount comparison is inclusive of the configured tolerance. Missing expected fields are ignored. Unknown confidence is not included in high/medium/low calibration bands. The cleanup removes evaluation documents and foreign-key descendants; cleanup errors fail the command rather than silently claiming success.
