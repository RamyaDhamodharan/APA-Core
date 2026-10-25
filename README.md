# AP Core Service

FastAPI starter for document ingestion, parsing/extraction, vendors, purchase orders, invoice matching, decisions, command center, notifications and audit.

## Windows local run
1. Install Python 3.11+ and `uv`.
2. In this folder: `uv sync --extra dev`
3. Copy `.env.example` to `.env` and configure your shared local PostgreSQL URL.
4. Run: `uv run uvicorn src.main:app --reload --port 8002`
5. Open `http://127.0.0.1:8002/docs` or `http://127.0.0.1:8002/health`.

## Local email polling test

1. Use a dedicated Gmail test mailbox. Enable 2-Step Verification and create
   an App Password in Google Account → Security → App passwords. Use that value
   as `IMAP_PASSWORD`; regular account passwords are rejected.
2. Copy the root `.env.example` to `.env` and set the required local
   `POSTGRES_PASSWORD` and `JWT_SECRET_KEY`, plus `IMAP_HOST=imap.gmail.com`,
   `IMAP_USER`, `IMAP_PASSWORD`, `EMAIL_PROVIDER=imap`,
   `EMAIL_POLL_ENABLED=true`, and the test mailbox in
   `TEST_MAILBOX_ALLOWLIST`.
3. From the repository root run `docker compose up -d --build`.
4. Create an administrator with
   `docker compose exec auth python scripts/create_admin.py --email you@example.com`.
5. Log in at `http://localhost:8001/docs` using `POST /api/auth/login` and copy
   the access token. In `http://localhost:8000/docs`, authorize with that token
   and call `POST /api/email-polling/run`.
6. Check `GET /api/documents` for the ingested email and attachment documents.

The scheduled poll uses Celery beat when email polling is enabled. The manual
admin endpoint works without waiting for the scheduled interval.

## Database initialization and migrations

AP-Auth and AP-Core share the `genworx_ap` database but maintain separate
Alembic version tables. AP-Auth owns authentication behavior and its
`alembic_version` history; AP-Core owns document, vendor, purchase-order,
invoice, matching, notification, and audit behavior and uses
`core_alembic_version`. Do not rename, share, or use one service's revision
table to record the other service's history.

The SQL bootstrap in `database/001_initial_schema.sql` describes the shared
baseline for a **new, empty** development database. It includes the users table
required by AP-Auth as well as AP-Core tables. The users text columns and
`must_change_password` default follow AP-Auth's model and initial migration;
the latter is `false`, and AP-Auth registration relies on that database
default. Do not change this default without coordinating the authentication
behavior and both schema definitions.

The live shared schema also contains `updated_at` and `updated_by` on
`audit_log`, `exchange_rates`, and `match_runs`. The bootstrap preserves those
columns; `updated_by` intentionally has no foreign key, matching the established
schema. The checked-in `db_schema.txt` is a partial column inventory, not a
complete schema specification.

The AP-Core baseline revision (`20261019_0001`) is a marker, not a
schema-creation or schema-validation migration. It may only be stamped after
the database has been initialized from the SQL bootstrap or its complete
existing shared schema has been inspected and reconciled with that baseline.
Never stamp an unknown or partially initialized database.

### Fresh development database

Create a new, empty PostgreSQL database named `genworx_ap`; do not point this
procedure at a database containing existing data. Configure `DATABASE_URL`
for that database in both services' environment files. Run the bootstrap once
from the AP-Core repository root. It creates the shared users table, so do not
run AP-Auth's `upgrade` for its table-creation revision on top of it.

```powershell
psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1 -f database/001_initial_schema.sql
# From AP-Core, enter AP-Auth after verifying its users table:
Push-Location ..\AP-Auth
uv run alembic stamp 0001
Pop-Location
# Continue from AP-Core:
uv run alembic stamp 20261019_0001
uv run alembic upgrade head
```

Stamping records each service's already-created schema in its own version
table; it does not execute the stamped revision. The AP-Core upgrade then
applies only revisions after its baseline.

### Existing development database

Back up the database first. Confirm that `DATABASE_URL` targets the intended
development database, then inspect the current revision and schema:

```powershell
pg_dump --format=custom --dbname="$env:DATABASE_URL" --file="$env:TEMP\genworx_ap-before-alembic.dump"
uv run alembic current --verbose
psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1 -c "SELECT to_regclass('public.core_alembic_version') AS version_table"
```

AP-Auth and AP-Core histories must be checked independently. An empty
`alembic_version` does not indicate AP-Core's state, and an empty
`core_alembic_version` does not indicate AP-Auth's state. Before stamping either
service, verify the full shared schema against the relevant service's existing
objects and migration contract. Do not run AP-Auth's initial table-creation
revision against a users table already created by the shared bootstrap.

For an existing database, take a backup, inspect both version tables without
modifying them, and reconcile all schema differences before choosing either
service's stamp or upgrade procedure. Never stamp over an unknown revision or
run a table-creation revision against existing data. The AP-Core alias
revision checks existing alias data and aborts rather than resolving duplicate
or blank normalized names automatically.

The ORM metadata is not yet a complete representation of every bootstrap
column. Alembic autogenerate is configured not to propose drops for database
tables or columns absent from that metadata; review all generated revisions
before applying them.

## Email polling

Set `EMAIL_POLL_ENABLED=true` and configure `IMAP_HOST`, `IMAP_USER`, and
`IMAP_PASSWORD` for a dedicated TEST mailbox. Polling checks only unseen mail;
PDF, PNG, and JPEG attachments enter the normal document pipeline. Keep
`TEST_MAILBOX_ALLOWLIST` limited to test addresses. Gmail requires 2-Step
Verification and an app password from Google Account → Security → App
passwords. Outlook consumer accounts can use an app password when two-step
verification is enabled; Microsoft 365 work accounts may require an admin to
enable authenticated IMAP or app passwords. Never use a production mailbox for
testing. Configure the interval with `EMAIL_POLL_INTERVAL_SECONDS` (default
120) and the folder with `IMAP_FOLDER` (default `INBOX`). Supported document
attachments are PDF, PNG, JPEG, and macro-free DOCX. DOCX files must have the
Office Open XML MIME type and a valid, bounded package structure; legacy `.doc`
and macro-enabled `.docm`/`.dotm` Word formats are rejected, as are packages
containing VBA projects.

## Invoice verification and tax checks

Invoice extraction stores a 0–100 confidence score for header and line fields.
Missing model confidence is marked as derived and conservatively scored; any
field below `FIELD_CONFIDENCE_MIN` (default 85) sends the invoice to the
associate verification queue before matching. Associates can confirm values or
edit them with a reason through the invoice verification API. `GL_CODE_LIST` is
an optional comma-separated code list; when configured, an unknown code is
shown as a validation warning.

Tax matching uses the purchase order's tax amount when present; otherwise it
uses the purchase order tax rate against the PO subtotal, then the vendor's
default tax rate against the invoice subtotal. A difference greater than
`INVOICE_TAX_TOLERANCE` (default 0.01 in invoice currency) is a match issue.
Tax rates are percentages (for example, `18` means 18%).
Invoices without a matching PO or a resolved vendor become No Match and are
rejected with a reason. Sender notifications are queued only for exact
addresses in `TEST_MAILBOX_ALLOWLIST`.

## Vendor email drafts

Vendor-facing email is stored in the outbox as a draft for a user to review,
edit, and send. Draft recipients and CC recipients must each exactly match an
address in `TEST_MAILBOX_ALLOWLIST`; blocked drafts stay available for editing
but cannot be sent. Configure `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`,
`SMTP_PASSWORD`, `SMTP_FROM`, and `SMTP_USE_SSL` for delivery. SMTP retries use
`EMAIL_DRAFT_MAX_ATTEMPTS` (default 3). The two intake notices for a missing
bill attachment and a No Match invoice can auto-send only to allowlisted test
mailboxes; other vendor notifications are kept as drafts. Sent messages carry
an `[AP-...]` reference so inbound replies can be attached to the invoice
timeline. Issue contact-vendor actions use the same outbox. Issue reminders
create drafts and do not auto-send.

## Payments, command center, and follow-ups

Use **Mark as paid** (`POST /api/invoices/{id}/pay`) to record an external
payment. Provide `paid_date`, `method` (`bank_transfer`, `cheque`, `upi`,
`cash`, or `other`), and `reference`. AP stores the record; it never contacts a
bank or initiates a payment. A mistaken record can be corrected with
`POST /api/invoices/{id}/pay/void` and a reason.

`GET /api/command-center/summary` returns bucket totals, and
`GET /api/command-center/bills?bucket=...` lists ready-to-pay, overdue,
rejected, needs-attention, or all invoices. Due-date calculations use
`APP_TIMEZONE` (default `Asia/Kolkata`). Hourly overdue checks and 15-minute
follow-ups run through Celery beat on the email queue. Internal reminders are
queued only for addresses in `TEST_MAILBOX_ALLOWLIST`; vendor reminders remain
human-reviewed email drafts.

Reminder timing can be configured with `VERIFY_PENDING_DAYS` (1),
`APPROVAL_PENDING_DAYS` (2), `VENDOR_REPLY_REMINDER_DAYS` (3),
`VENDOR_REPLY_ESCALATE_DAYS` (7), `OVERDUE_REMINDER_DAYS` (1),
`REJECTED_REMINDER_DAYS` (7), `DUE_SOON_DAYS` (3), `HOLD_STALE_DAYS` (5), and
`MAX_REMINDERS` (3). `ASSIGNMENT_STRATEGY` accepts `least_open` or `manual`;
`ESCALATION_EMAIL` is used when it is allowlisted, otherwise escalations go to
other allowlisted associates.

Case 5: assert the sender notification is created through the email outbox and is only sent to allowlisted addresses.

## Seed and evaluate sample data

Import CSV or Excel files with explicit paths and formats, for example:

```powershell
uv run python scripts/seed_import.py --folder .\sample-data `
  --vendors vendors.xlsx --vendors-format xlsx `
  --purchase-orders purchase_orders.csv --purchase-orders-format csv `
  --goods-receipts goods_receipts.xlsx --goods-receipts-format xlsx
```

The importer groups PO and receipt line rows, skips records already present, and
prints created/skipped/error counts. Each source record is committed separately
so one malformed row does not roll back earlier imports.

Run sample PDFs/images through the API and score the result against a JSON
answer key:

```powershell
uv run python scripts/evaluate.py --api-url http://localhost:8000 `
  --token <access-token> --samples-dir .\sample-bills `
  --answer-key .\sample-bills\answer-key.json --report-dir .\evaluation-report `
  --confirm
```

Evaluation requires `--confirm` and the exact database name in the comma-separated
`EVAL_ALLOWED_DATABASES` environment variable. See `docs/evaluation.md` for the
guarded local-pipeline mode and report details.

The evaluator writes Markdown and JSON reports with per-field accuracy, match
accuracy/confusion, missed duplicates/overbilling, terminal failures, and
state/step-log coverage. EML samples are sent through SMTP and require
`--smtp-host` (port defaults to 3025). Supply `--baseline-report` to include a
before/after throughput comparison. For a measured 100-bill time, run exactly
100 samples; smaller sets report a linear estimate.

The documents worker uses `WORKER_CONCURRENCY` (default 2) with Celery's prefork
pool. Docling's converter cache is process-local, so each worker process loads
its models once and retains them for later documents. More processes increase
memory use; benchmark before raising the value. No before/after 100-bill timing
is checked into this repository because no sample corpus/answer key is present.

## Backend E2E check

From the repository root on Windows, with Docker Desktop and `uv` installed, run `powershell -ExecutionPolicy Bypass -File .\scripts\run_e2e.ps1` (or `make e2e-backend` where Make is installed). It works whether the main stack is running or stopped: the E2E project has distinct container names, its own PostgreSQL and MinIO volumes, an internal project network, and separately configured host ports in `scripts/e2e-ports.env`. GreenMail provides local SMTP/IMAP and `LLM_PROVIDER=stub` is deterministic. The runner applies both migration sets, seeds an isolated admin account, runs the HTTP/IMAP checks, then removes only the E2E project and its volumes. It does not connect to Gmail or call a paid LLM provider.
# Email polling retry and model cache

Email processing uses the `email` Celery queue; document processing uses the
`documents` queue. `EMAIL_MAX_ATTEMPTS` defaults to 5, and transient failures
are scheduled with increasing delays. `EMAIL_SENDER_BLOCKLIST` accepts comma
separated sender addresses, domains, or sender-name patterns.

The Compose `worker` mounts the named `hf-cache` volume at
`/root/.cache/huggingface`. The first document-processing run may download the
configured Docling models; later worker recreates reuse the cached model files.
