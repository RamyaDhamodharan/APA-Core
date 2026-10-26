-- GenWorx AP Assistant initial schema based on supplied LLD.
-- Local/sample-data use only. UUID PK and standard audit columns on each business table.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SCHEMA IF NOT EXISTS auth;
CREATE SCHEMA IF NOT EXISTS core;

CREATE TYPE auth.user_role AS ENUM ('associate', 'admin');
CREATE TYPE core.doc_type AS ENUM ('po', 'invoice', 'goods_receipt', 'other');
CREATE TYPE core.doc_status AS ENUM ('received', 'parsing', 'parsed', 'classified', 'extracted', 'needs_vendor_review', 'processed', 'ignored', 'other', 'incomplete', 'duplicate_blocked', 'failed');
CREATE TYPE core.match_status AS ENUM ('pending', 'needs_vendor_review', 'waiting_for_po', 'needs_po_confirmation', 'match_issues', 'no_match');
CREATE TYPE core.match_result AS ENUM ('match_ok', 'match_issues', 'no_match');
CREATE TYPE core.review_decision AS ENUM ('approved', 'rejected', 'on_hold');
CREATE TYPE core.invoice_workflow_status AS ENUM ('received', 'reading', 'verifying', 'matching', 'match_approved', 'match_issues', 'no_match', 'approved', 'rejected', 'on_hold', 'paid', 'overdue', 'resubmitted', 'failed');
CREATE TYPE core.step_result AS ENUM ('success', 'warning', 'error');
CREATE TYPE core.notify_status AS ENUM ('queued', 'sent', 'failed');
CREATE TYPE core.invoice_supporting_document_category AS ENUM (
  'delivery_note', 'contract', 'approval_email', 'tax_certificate', 'other'
);

CREATE TABLE auth.users (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name varchar NOT NULL,
  email varchar NOT NULL UNIQUE,
  role auth.user_role NOT NULL,
  is_active boolean NOT NULL DEFAULT true,
  password_hash varchar NOT NULL,
  must_change_password boolean NOT NULL DEFAULT false,
  temp_password_expires_at timestamptz,
  failed_login_count integer NOT NULL DEFAULT 0,
  locked_until timestamptz,
  last_login_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by uuid REFERENCES auth.users(id),
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by uuid REFERENCES auth.users(id)
);

CREATE TABLE auth.refresh_sessions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES auth.users(id),
  token_hash varchar(255) NOT NULL UNIQUE,
  expires_at timestamptz NOT NULL,
  is_revoked boolean NOT NULL DEFAULT false,
  revoked_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by uuid REFERENCES auth.users(id),
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by uuid REFERENCES auth.users(id)
);

CREATE TABLE core.vendors (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name varchar(200) NOT NULL,
  tax_id varchar(30),
  default_tax_rate numeric(7,4),
  is_active boolean NOT NULL DEFAULT true,
  contact_name varchar(120),
  contact_email varchar(200),
  contact_phone varchar(40),
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by uuid REFERENCES auth.users(id),
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by uuid REFERENCES auth.users(id)
);

CREATE TABLE core.vendor_aliases (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  vendor_id uuid NOT NULL REFERENCES core.vendors(id) ON DELETE CASCADE,
  alias_norm varchar(200) NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by uuid REFERENCES auth.users(id),
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by uuid REFERENCES auth.users(id),
  CONSTRAINT uq_vendor_alias UNIQUE (vendor_id, alias_norm)
);

CREATE TABLE core.documents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  original_filename varchar(300) NOT NULL,
  file_hash char(64) NOT NULL UNIQUE,
  storage_path varchar(500),
  parent_document_id uuid REFERENCES core.documents(id),
  status core.doc_status NOT NULL DEFAULT 'received',
  doc_type core.doc_type,
  classify_confidence double precision,
  classify_override varchar(30),
  sha256 char(64),
  mime varchar(120),
  size integer,
  raw_text text,
  extracted_json jsonb,
  email_sender varchar(200),
  email_subject varchar(500),
  email_received_at timestamptz,
  email_message_id varchar(300),
  email_uid integer,
  email_body_preview text,
  email_eml_key varchar(500),
  source varchar(40),
  source_flag varchar(100),
  failure_message varchar(500),
  failure_class varchar(24),
  retry_count integer NOT NULL DEFAULT 0,
  next_retry_at timestamptz,
  last_attempt_at timestamptz,
  bulk_reprocessed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by uuid REFERENCES auth.users(id),
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by uuid REFERENCES auth.users(id)
);
CREATE UNIQUE INDEX uq_documents_email_message_id_root ON core.documents(email_message_id)
  WHERE parent_document_id IS NULL AND email_message_id IS NOT NULL;

CREATE TABLE core.purchase_orders (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  po_number varchar(60) NOT NULL,
  version_no integer NOT NULL DEFAULT 1,
  is_current boolean NOT NULL DEFAULT true,
  vendor_id uuid REFERENCES core.vendors(id),
  source_document_id uuid REFERENCES core.documents(id),
  po_date date,
  currency char(3) NOT NULL,
  subtotal numeric(14,2),
  tax_amount numeric(14,2),
  tax_rate numeric(7,4),
  total_amount numeric(14,2),
  payment_terms jsonb,
  delivery_details jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by uuid REFERENCES auth.users(id),
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by uuid REFERENCES auth.users(id),
  CONSTRAINT uq_po_version UNIQUE (po_number, version_no)
);
CREATE UNIQUE INDEX uq_current_po_number ON core.purchase_orders(po_number) WHERE is_current;

CREATE TABLE core.po_lines (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  po_id uuid NOT NULL REFERENCES core.purchase_orders(id) ON DELETE CASCADE,
  item_code varchar(60),
  description varchar(300),
  uom varchar(10),
  qty numeric(14,3),
  unit_price numeric(14,4),
  line_amount numeric(14,2),
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by uuid REFERENCES auth.users(id),
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by uuid REFERENCES auth.users(id)
);

CREATE TABLE core.invoices (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  resubmitted_from_id uuid REFERENCES core.invoices(id),
  source_document_id uuid REFERENCES core.documents(id),
  invoice_no varchar(60),
  version_no integer NOT NULL DEFAULT 1,
  is_current boolean NOT NULL DEFAULT true,
  vendor_id uuid REFERENCES core.vendors(id),
  vendor_name_raw varchar(200),
  vendor_tax_id_raw varchar(30),
  invoice_date date,
  due_date date,
  currency char(3),
  subtotal numeric(14,2),
  tax_amount numeric(14,2),
  total_amount numeric(14,2),
  field_confidence jsonb NOT NULL DEFAULT '{}'::jsonb,
  gl_code varchar(60),
  po_number_raw varchar(60),
  po_id uuid REFERENCES core.purchase_orders(id),
  match_status core.match_status NOT NULL DEFAULT 'pending',
  final_status core.review_decision,
  workflow_status core.invoice_workflow_status NOT NULL DEFAULT 'received',
  final_run_id uuid,
  final_by uuid REFERENCES auth.users(id),
  final_at timestamptz,
  paid_at timestamptz,
  payment_method varchar(60),
  payment_reference varchar(200),
  owner_user_id uuid REFERENCES auth.users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by uuid REFERENCES auth.users(id),
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by uuid REFERENCES auth.users(id),
  CONSTRAINT uq_invoice_version UNIQUE (vendor_id, invoice_no, version_no)
);
CREATE UNIQUE INDEX uq_current_vendor_invoice ON core.invoices(vendor_id, invoice_no)
  WHERE is_current AND vendor_id IS NOT NULL AND invoice_no IS NOT NULL;
CREATE INDEX ix_invoices_status_date ON core.invoices(match_status, final_status, invoice_date);
CREATE TABLE core.invoice_state_history (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  invoice_id uuid NOT NULL REFERENCES core.invoices(id) ON DELETE CASCADE,
  from_status varchar(30), to_status varchar(30) NOT NULL,
  changed_by uuid REFERENCES auth.users(id), reason text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_invoice_state_history_timeline ON core.invoice_state_history(invoice_id, created_at);
CREATE INDEX ix_invoices_due_date ON core.invoices(due_date);
CREATE INDEX ix_invoices_owner ON core.invoices(owner_user_id);
CREATE INDEX ix_invoices_po_id ON core.invoices(po_id);
CREATE INDEX ix_invoices_gl_code ON core.invoices(gl_code);

CREATE TABLE core.invoice_lines (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  invoice_id uuid NOT NULL REFERENCES core.invoices(id) ON DELETE CASCADE,
  item_code varchar(60),
  description varchar(300),
  uom varchar(10),
  qty numeric(14,3),
  unit_price numeric(14,4),
  line_amount numeric(14,2),
  field_confidence jsonb NOT NULL DEFAULT '{}'::jsonb,
  po_line_id uuid REFERENCES core.po_lines(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by uuid REFERENCES auth.users(id),
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by uuid REFERENCES auth.users(id)
);

CREATE TABLE core.goods_receipts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  gr_number varchar(60) NOT NULL,
  po_id uuid NOT NULL REFERENCES core.purchase_orders(id),
  vendor_id uuid NOT NULL REFERENCES core.vendors(id),
  source_document_id uuid REFERENCES core.documents(id),
  receipt_date date NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by uuid REFERENCES auth.users(id),
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by uuid REFERENCES auth.users(id),
  CONSTRAINT uq_goods_receipts_gr_number UNIQUE (gr_number)
);
CREATE INDEX ix_goods_receipts_po_id ON core.goods_receipts(po_id);
CREATE UNIQUE INDEX uq_goods_receipts_gr_number_norm ON core.goods_receipts(lower(trim(gr_number)));

CREATE TABLE core.goods_receipt_lines (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  goods_receipt_id uuid NOT NULL REFERENCES core.goods_receipts(id) ON DELETE CASCADE,
  po_line_id uuid NOT NULL REFERENCES core.po_lines(id),
  item_code varchar(60),
  description varchar(300),
  uom varchar(10),
  received_qty numeric(14,3) NOT NULL CHECK (received_qty > 0),
  accepted_qty numeric(14,3) NOT NULL CHECK (accepted_qty >= 0),
  rejected_qty numeric(14,3) NOT NULL DEFAULT 0 CHECK (rejected_qty >= 0),
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by uuid REFERENCES auth.users(id),
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by uuid REFERENCES auth.users(id),
  CONSTRAINT ck_gr_line_qty_balance CHECK (accepted_qty + rejected_qty = received_qty)
);

CREATE TABLE core.exchange_rates (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  from_currency char(3) NOT NULL,
  to_currency char(3) NOT NULL,
  rate numeric(14,6) NOT NULL CHECK (rate > 0),
  rate_date date NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by uuid REFERENCES auth.users(id),
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by uuid,
  CONSTRAINT uq_exchange_rate UNIQUE(from_currency, to_currency, rate_date)
);

CREATE TABLE core.match_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  invoice_id uuid NOT NULL REFERENCES core.invoices(id) ON DELETE CASCADE,
  run_no integer NOT NULL,
  po_id uuid REFERENCES core.purchase_orders(id),
  result core.match_result NOT NULL,
  exchange_rate_id uuid REFERENCES core.exchange_rates(id),
  rules_used jsonb NOT NULL DEFAULT '{}'::jsonb,
  differences jsonb NOT NULL DEFAULT '[]'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by uuid REFERENCES auth.users(id),
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by uuid,
  CONSTRAINT uq_match_run_no UNIQUE(invoice_id, run_no)
);
ALTER TABLE core.invoices ADD CONSTRAINT fk_invoice_final_run
  FOREIGN KEY (final_run_id) REFERENCES core.match_runs(id);

CREATE TABLE core.audit_log (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  action varchar(60) NOT NULL,
  entity_type varchar(40) NOT NULL,
  entity_id uuid,
  old_value jsonb,
  new_value jsonb,
  result core.step_result NOT NULL DEFAULT 'success',
  message varchar(500),
  reason text,
  step_name varchar(60),
  duration_ms integer,
  input_ref varchar(500),
  output_summary jsonb,
  technical_detail text,
  request_id varchar(100),
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by uuid REFERENCES auth.users(id),
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by uuid
);
CREATE INDEX ix_audit_entity_timeline ON core.audit_log(entity_type, entity_id, created_at);

CREATE TABLE core.document_workflows (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id uuid NOT NULL REFERENCES core.documents(id) ON DELETE CASCADE,
  status varchar(40) NOT NULL,
  result jsonb NOT NULL DEFAULT '{}'::jsonb,
  decision varchar(40),
  decision_target_id uuid,
  decision_target_type varchar(30),
  decision_reason varchar(500),
  decision_by uuid REFERENCES auth.users(id),
  decided_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_document_workflows_document UNIQUE(document_id)
);
CREATE INDEX ix_document_workflows_status ON core.document_workflows(status);

CREATE TABLE core.email_poll_state (
  mailbox varchar(300) NOT NULL,
  folder varchar(300) NOT NULL,
  uidvalidity integer NOT NULL,
  last_uid integer NOT NULL DEFAULT 0,
  last_run_at timestamptz,
  last_result jsonb,
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_email_poll_state PRIMARY KEY (mailbox, folder)
);

CREATE TABLE core.email_messages (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  message_id varchar(500) NOT NULL,
  uid integer NOT NULL,
  mailbox varchar(300) NOT NULL,
  folder varchar(300) NOT NULL DEFAULT 'INBOX',
  sender varchar(300),
  subject varchar(500),
  received_at timestamptz,
  body_preview text,
  raw_object_key varchar(500),
  status varchar(40) NOT NULL DEFAULT 'received',
  failure_reason varchar(200),
  filter_reason varchar(200),
  attempts integer NOT NULL DEFAULT 0,
  next_retry_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_email_messages_message_id UNIQUE (message_id)
);
CREATE INDEX ix_email_messages_mailbox_folder_uid
  ON core.email_messages(mailbox, folder, uid);
CREATE INDEX ix_email_messages_due_retry
  ON core.email_messages(mailbox, folder, next_retry_at)
  WHERE status = 'failed';

CREATE TABLE core.notifications (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id uuid NOT NULL REFERENCES core.documents(id),
  recipient_email varchar(200) NOT NULL,
  subject varchar(500),
  body text,
  status core.notify_status NOT NULL DEFAULT 'queued',
  attempts integer NOT NULL DEFAULT 0,
  sent_at timestamptz,
  last_error varchar(500),
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by uuid REFERENCES auth.users(id),
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by uuid REFERENCES auth.users(id)
);
CREATE TABLE core.email_drafts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  kind varchar(50) NOT NULL,
  invoice_id uuid REFERENCES core.invoices(id) ON DELETE CASCADE,
  issue_id uuid,
  document_id uuid REFERENCES core.documents(id),
  to_addresses jsonb NOT NULL DEFAULT '[]'::jsonb,
  cc_addresses jsonb NOT NULL DEFAULT '[]'::jsonb,
  subject varchar(500) NOT NULL,
  body text NOT NULL,
  status varchar(20) NOT NULL DEFAULT 'draft'
    CHECK (status IN ('draft','approved','sent','failed','discarded')),
  blocked_by_allowlist boolean NOT NULL DEFAULT false,
  created_by uuid REFERENCES auth.users(id),
  generated_by varchar(20) NOT NULL DEFAULT 'template',
  edited_by uuid REFERENCES auth.users(id),
  sent_by uuid REFERENCES auth.users(id),
  sent_at timestamptz,
  message_id varchar(500),
  thread_key varchar(100),
  last_error varchar(500),
  attempts integer NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_email_drafts_invoice_created ON core.email_drafts(invoice_id, created_at);
CREATE INDEX ix_email_drafts_thread_key ON core.email_drafts(thread_key);
CREATE TABLE core.email_draft_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  draft_id uuid NOT NULL REFERENCES core.email_drafts(id) ON DELETE CASCADE,
  event_type varchar(30) NOT NULL CHECK (event_type IN ('created','edited','sent','failed','discarded','vendor_replied')),
  user_id uuid REFERENCES auth.users(id),
  old_value jsonb,
  new_value jsonb,
  document_id uuid REFERENCES core.documents(id),
  message_id varchar(500),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE core.invoice_issues (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  invoice_id uuid NOT NULL REFERENCES core.invoices(id) ON DELETE CASCADE,
  issue_type varchar(50) NOT NULL,
  severity varchar(20) NOT NULL DEFAULT 'warning',
  description text NOT NULL,
  expected_value jsonb,
  actual_value jsonb,
  status varchar(30) NOT NULL DEFAULT 'open'
    CHECK (status IN ('open','in_progress','waiting_vendor','escalated','resolved','dismissed')),
  owner_user_id uuid REFERENCES auth.users(id),
  due_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  resolved_at timestamptz,
  resolution_note text
);
CREATE UNIQUE INDEX uq_invoice_issue_open_type
  ON core.invoice_issues(invoice_id, issue_type)
  WHERE status IN ('open','in_progress','waiting_vendor','escalated');
CREATE INDEX ix_invoice_issues_status_due ON core.invoice_issues(status, due_at);
CREATE INDEX ix_invoice_issues_owner_status ON core.invoice_issues(owner_user_id, status);
ALTER TABLE core.email_drafts
  ADD CONSTRAINT fk_email_drafts_issue_id
  FOREIGN KEY (issue_id) REFERENCES core.invoice_issues(id) ON DELETE CASCADE;

CREATE TABLE core.issue_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  issue_id uuid NOT NULL REFERENCES core.invoice_issues(id) ON DELETE CASCADE,
  event_type varchar(30) NOT NULL CHECK (event_type IN
    ('created','comment','assigned','reassigned','escalated','vendor_contacted','vendor_replied','status_changed','resolved','dismissed','reopened')),
  user_id uuid REFERENCES auth.users(id),
  note text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_issue_events_timeline ON core.issue_events(issue_id, created_at);

CREATE TABLE core.payments (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  invoice_id uuid NOT NULL REFERENCES core.invoices(id) ON DELETE CASCADE,
  amount numeric(14,2) NOT NULL CHECK (amount >= 0),
  paid_date date NOT NULL,
  method varchar(20) NOT NULL CHECK (method IN
    ('bank_transfer','cheque','upi','cash','other')),
  reference varchar(200) NOT NULL,
  recorded_by uuid NOT NULL REFERENCES auth.users(id),
  note text,
  created_at timestamptz NOT NULL DEFAULT now(),
  voided_at timestamptz,
  voided_by uuid REFERENCES auth.users(id),
  void_reason text
);
CREATE UNIQUE INDEX uq_payments_active_invoice
  ON core.payments(invoice_id) WHERE voided_at IS NULL;

CREATE TABLE core.follow_ups (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  entity_type varchar(20) NOT NULL CHECK (entity_type IN ('invoice','issue')),
  entity_id uuid NOT NULL,
  kind varchar(40) NOT NULL CHECK (kind IN (
    'verify_pending','approval_pending','issue_unassigned','issue_waiting_vendor',
    'overdue','rejected_not_resubmitted','due_soon','on_hold_stale')),
  recipient varchar(200),
  attempts integer NOT NULL DEFAULT 0,
  last_sent_at timestamptz,
  next_due_at timestamptz NOT NULL,
  stopped_at timestamptz,
  stop_reason text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_followups_active_entity_kind
  ON core.follow_ups(entity_type, entity_id, kind) WHERE stopped_at IS NULL;
CREATE INDEX ix_follow_ups_due ON core.follow_ups(stopped_at, next_due_at);
CREATE INDEX ix_email_draft_events_draft_created ON core.email_draft_events(draft_id, created_at);
CREATE TABLE core.invoice_supporting_documents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  invoice_id uuid NOT NULL REFERENCES core.invoices(id),
  file_name varchar(300) NOT NULL,
  content_type varchar(150) NOT NULL,
  size_bytes integer NOT NULL CHECK (size_bytes > 0),
  sha256 varchar(64) NOT NULL,
  storage_key varchar(700) NOT NULL,
  category core.invoice_supporting_document_category NOT NULL,
  description text,
  uploaded_by uuid NOT NULL REFERENCES auth.users(id),
  uploaded_at timestamptz NOT NULL DEFAULT now(),
  deleted_at timestamptz,
  deleted_by uuid REFERENCES auth.users(id),
  deleted_reason text
);
CREATE INDEX ix_invoice_supporting_documents_invoice
  ON core.invoice_supporting_documents(invoice_id);
CREATE UNIQUE INDEX uq_invoice_supporting_documents_active_sha256
  ON core.invoice_supporting_documents(invoice_id, sha256)
  WHERE deleted_at IS NULL;
CREATE INDEX ix_documents_status ON core.documents(status);
CREATE INDEX ix_vendor_alias_norm ON core.vendor_aliases(alias_norm);
CREATE INDEX ix_refresh_user_revoked ON auth.refresh_sessions(user_id, is_revoked);

-- Keep updated_at maintained by application/SQLAlchemy on updates.
