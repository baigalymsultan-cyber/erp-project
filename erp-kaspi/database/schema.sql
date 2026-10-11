-- =====================================================================
-- PRODUCTION ERP / FINANCE / KASPI / MRP — KAZAKHSTAN
-- FILE: database/schema.sql
-- STEP 1 — DATABASE SCHEMA (MASTER PROMPT v3.0, §80–§92)
--
-- Target: PostgreSQL 15+ (verified on 17.11)
--
-- DDL ORDER (§84):
--   1. extensions
--   2. helper functions
--   3. base tables
--   4. dependent tables
--   5. deferred constraints (circular references)
--   6. indexes 
--   7. triggers
--   8. views
--   9. seed data
--
-- CONVENTIONS (§13, §14, §15):
--   money           NUMERIC(15,2)   — KZT, UI format: 1 234 567,00 ₸
--   quantity        NUMERIC(15,3)
--   timestamps      TIMESTAMPTZ     — stored in UTC, displayed in Asia/Almaty
--   raw API payload JSONB
--   encrypted data  BYTEA           — AES-256-GCM (see docs/adr/ADR.md ADR-009)
--
-- IDEMPOTENCY (§87): the whole script is re-runnable
--   (IF NOT EXISTS / OR REPLACE / ON CONFLICT DO NOTHING).
-- =====================================================================

\set ON_ERROR_STOP on

SET client_min_messages = WARNING;

-- =====================================================================
-- SECTION 1 — EXTENSIONS
-- =====================================================================

-- btree_gist: required for EXCLUDE constraints that combine an equality
-- operator on a scalar column with an overlap operator on a range
-- (§31 — no overlapping rate periods for the same work operation type).
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- =====================================================================
-- SECTION 2 — HELPER FUNCTIONS
-- =====================================================================

-- ---------------------------------------------------------------------
-- 2.1 fn_current_app_user()
-- Returns the application user id set by the backend for the current
-- transaction via:  SET LOCAL app.current_user_id = '<id>';
-- Used by audit triggers (§54). NULL when the change is made by a
-- migration, ETL job or direct DBA access — that is recorded as NULL,
-- never faked.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_current_app_user()
RETURNS BIGINT
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    v_raw TEXT;
BEGIN
    v_raw := current_setting('app.current_user_id', TRUE);
    IF v_raw IS NULL OR v_raw = '' THEN
        RETURN NULL;
    END IF;
    RETURN v_raw::BIGINT;
EXCEPTION
    WHEN invalid_text_representation THEN
        RETURN NULL;
END;
$$;

COMMENT ON FUNCTION fn_current_app_user() IS
'Returns app.current_user_id session variable as BIGINT, or NULL. Used by audit triggers (MASTER PROMPT §54).';

-- ---------------------------------------------------------------------
-- 2.2 fn_set_updated_at()
-- §56 — tables with updated_at must refresh the timestamp automatically.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_set_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at := CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION fn_set_updated_at() IS
'BEFORE UPDATE trigger: sets updated_at = CURRENT_TIMESTAMP (MASTER PROMPT §56).';

-- ---------------------------------------------------------------------
-- 2.3 fn_forbid_delete()
-- §16 — financial data must never be removed by a plain DELETE.
-- Corrections are made through reversal / correction / refund /
-- adjustment operations and status changes.
-- An explicit, audited escape hatch exists for maintenance:
--   SET LOCAL app.allow_financial_delete = 'on';
-- It is intentionally session-local and never used by the application.
-- TD-003 (2026-09-16): a session-custom GUC can be SET by ANY role --
-- PostgreSQL has no way to revoke SET on app.* parameters -- so gating the
-- hatch on the GUC alone was bypassable by the unprivileged application
-- role (reproduced: erp_app SET the flag and physically DELETEd a posted
-- operation). The hatch is therefore additionally gated on membership in
-- the schema-owner/migrator role, which the application role does not have
-- and cannot grant itself (TD-008). Fail-closed: if the role does not
-- exist, the hatch is unusable and DELETE stays forbidden.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_forbid_delete()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_hatch_role TEXT := 'erp_migrator';
BEGIN
    IF coalesce(current_setting('app.allow_financial_delete', TRUE), 'off') = 'on'
       AND coalesce((
               SELECT true
               FROM pg_roles r
               WHERE r.rolname = v_hatch_role
                 AND pg_has_role(current_user, r.rolname, 'member')
           ), false) THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION
        'DELETE is forbidden on financial table %.% (MASTER PROMPT §16). Use reversal / correction / refund / adjustment instead.',
        TG_TABLE_SCHEMA, TG_TABLE_NAME
        USING ERRCODE = 'restrict_violation';
END;
$$;

COMMENT ON FUNCTION fn_forbid_delete() IS
'BEFORE DELETE trigger: blocks physical deletion of financial rows (MASTER PROMPT §16).';

-- ---------------------------------------------------------------------
-- 2.4 fn_payroll_payment_no_overpay()
-- P2-1 (2026-10-03): DB-level guard against payroll overpayment.
-- The service pays exactly total_calculated once, but a direct SQL insert
-- or future code path could overpay. This trigger ensures
-- SUM(payroll_payments.amount) <= payroll_summary.total_calculated
-- for POSTED payments.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_payroll_payment_no_overpay()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_total NUMERIC(15,2);
    v_paid NUMERIC(15,2);
BEGIN
    -- Only POSTED payments count toward the cap
    IF NEW.status <> 'POSTED' THEN
        RETURN NEW;
    END IF;
    SELECT total_calculated INTO v_total
    FROM payroll_summary WHERE id = NEW.payroll_summary_id;
    IF NOT FOUND THEN
        RETURN NEW;  -- FK will reject; not our job
    END IF;
    SELECT coalesce(sum(amount), 0) INTO v_paid
    FROM payroll_payments
    WHERE payroll_summary_id = NEW.payroll_summary_id
      AND status = 'POSTED'
      AND id IS DISTINCT FROM NEW.id;
    IF v_paid + NEW.amount > v_total THEN
        RAISE EXCEPTION
            'payroll overpayment blocked: summary % total is %, already paid %, new payment %',
            NEW.payroll_summary_id, v_total, v_paid, NEW.amount
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION fn_payroll_payment_no_overpay() IS
'BEFORE INSERT OR UPDATE trigger: blocks payroll payments exceeding the summary total (§36).';

-- ---------------------------------------------------------------------
-- 2.5 fn_audit()
-- §54 — generic audit trigger. Records create / update / delete-attempt
-- with old and new row as JSONB.
-- Sensitive columns are masked so that secrets never reach the audit log
-- (§71 — never log passwords, tokens, keys).
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_audit()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_old       JSONB;
    v_new       JSONB;
    v_entity_id BIGINT;
    v_masked    TEXT[] := ARRAY['password_hash', 'encrypted_token', 'token', 'secret'];
    v_col       TEXT;
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        v_old := to_jsonb(OLD);
    END IF;
    IF TG_OP IN ('INSERT', 'UPDATE') THEN
        v_new := to_jsonb(NEW);
    END IF;

    FOREACH v_col IN ARRAY v_masked LOOP
        IF v_old ? v_col THEN
            v_old := jsonb_set(v_old, ARRAY[v_col], '"***MASKED***"'::JSONB);
        END IF;
        IF v_new ? v_col THEN
            v_new := jsonb_set(v_new, ARRAY[v_col], '"***MASKED***"'::JSONB);
        END IF;
    END LOOP;

    v_entity_id := coalesce((v_new ->> 'id')::BIGINT, (v_old ->> 'id')::BIGINT);

    INSERT INTO audit_log (user_id, entity, entity_id, action, old_data, new_data)
    VALUES (fn_current_app_user(), TG_TABLE_NAME, v_entity_id, TG_OP, v_old, v_new);

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION fn_audit() IS
'Generic AFTER INSERT/UPDATE/DELETE audit trigger writing to audit_log with masked secrets (MASTER PROMPT §54, §71).';

-- ---------------------------------------------------------------------
-- 2.5 fn_work_entry_freeze_rate()
-- §32 — RATE FREEZE. On INSERT the effective rate is looked up in
-- rates_history and copied into work_entries.rate_at_time.
-- Later changes in rates_history do NOT affect existing work entries.
-- Enforced in the database, not only in application code (ADR-008).
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_work_entry_freeze_rate()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_rate      NUMERIC(15,2);
    v_moment    TIMESTAMPTZ;
BEGIN
    -- entry_date is a calendar date; the rate is resolved at the start of that day (UTC).
    v_moment := NEW.entry_date::TIMESTAMPTZ;

    IF NEW.rate_at_time IS NULL THEN
        SELECT rh.rate
          INTO v_rate
          FROM rates_history rh
         WHERE rh.work_operation_type_id = NEW.work_operation_type_id
           AND rh.effective_from <= v_moment
           AND (rh.effective_to IS NULL OR rh.effective_to > v_moment)
         ORDER BY rh.effective_from DESC
         LIMIT 1;

        IF v_rate IS NULL THEN
            RAISE EXCEPTION
                'No effective rate found in rates_history for work_operation_type_id=% at %  (MASTER PROMPT §32).',
                NEW.work_operation_type_id, v_moment
                USING ERRCODE = 'no_data_found';
        END IF;

        NEW.rate_at_time := v_rate;
    END IF;

    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION fn_work_entry_freeze_rate() IS
'BEFORE INSERT on work_entries: copies the effective rate into rate_at_time (MASTER PROMPT §32).';

-- ---------------------------------------------------------------------
-- 2.6 fn_work_entry_protect_rate()
-- §32 — a frozen rate must not silently change afterwards.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_work_entry_protect_rate()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.rate_at_time IS DISTINCT FROM OLD.rate_at_time THEN
        RAISE EXCEPTION
            'work_entries.rate_at_time is frozen and cannot be modified (id=%, MASTER PROMPT §32). Create a correcting entry instead.',
            OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION fn_work_entry_protect_rate() IS
'BEFORE UPDATE on work_entries: forbids changing the frozen rate_at_time (MASTER PROMPT §32).';

-- ---------------------------------------------------------------------
-- 2.7 fn_order_material_freeze_price()
-- §38 — MATERIAL PRICE FREEZE. price_at_time is copied from
-- materials.current_price at the moment the material is attached to the
-- order. Later price changes do not alter historical cost.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_order_material_freeze_price()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_price NUMERIC(15,2);
BEGIN
    IF NEW.price_at_time IS NULL THEN
        SELECT m.current_price
          INTO v_price
          FROM materials m
         WHERE m.id = NEW.material_id;

        IF v_price IS NULL THEN
            RAISE EXCEPTION
                'materials.current_price is NULL for material_id=%; cannot freeze price (MASTER PROMPT §38).',
                NEW.material_id
                USING ERRCODE = 'no_data_found';
        END IF;

        NEW.price_at_time := v_price;
    END IF;

    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION fn_order_material_freeze_price() IS
'BEFORE INSERT on order_materials: copies materials.current_price into price_at_time (MASTER PROMPT §38).';

-- ---------------------------------------------------------------------
-- 2.8 fn_order_material_protect_price()
-- §38 — frozen material price is immutable.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_order_material_protect_price()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.price_at_time IS DISTINCT FROM OLD.price_at_time THEN
        RAISE EXCEPTION
            'order_materials.price_at_time is frozen and cannot be modified (order_id=%, material_id=%, MASTER PROMPT §38).',
            OLD.order_id, OLD.material_id
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION fn_order_material_protect_price() IS
'BEFORE UPDATE on order_materials: forbids changing the frozen price_at_time (MASTER PROMPT §38).';

-- ---------------------------------------------------------------------
-- 2.9 fn_log_order_status_change()
-- §50 — every order status change is recorded.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_log_order_status_change()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO order_status_history (order_id, old_status, new_status, changed_by, comment)
        VALUES (NEW.id, NULL, NEW.status, fn_current_app_user(), 'order created');
        RETURN NEW;
    END IF;

    IF NEW.status IS DISTINCT FROM OLD.status THEN
        INSERT INTO order_status_history (order_id, old_status, new_status, changed_by, comment)
        VALUES (NEW.id, OLD.status, NEW.status, fn_current_app_user(), NULL);
    END IF;

    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION fn_log_order_status_change() IS
'Records order status transitions into order_status_history (MASTER PROMPT §50).';

-- ---------------------------------------------------------------------
-- 2.10 fn_check_operation_category()
-- §21, §27 — the category of an operation must match the direction of
-- its operation type (an income operation cannot use an expense
-- category). Transfers are category-free.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_check_operation_category()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_type_kind     TEXT;
    v_category_kind TEXT;
BEGIN
    SELECT ot.kind INTO v_type_kind
      FROM operation_types ot
     WHERE ot.id = NEW.type_id;

    IF NEW.category_id IS NULL THEN
        RETURN NEW;
    END IF;

    SELECT c.kind INTO v_category_kind
      FROM categories c
     WHERE c.id = NEW.category_id;

    IF v_type_kind = 'TRANSFER' THEN
        RAISE EXCEPTION
            'Transfer operations must not reference a P&L category (operation type kind=TRANSFER, MASTER PROMPT §28, §76).'
            USING ERRCODE = 'check_violation';
    END IF;

    IF v_type_kind IS DISTINCT FROM v_category_kind THEN
        RAISE EXCEPTION
            'Category kind (%) does not match operation type kind (%) (MASTER PROMPT §21, §27).',
            v_category_kind, v_type_kind
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION fn_check_operation_category() IS
'Validates that operation category kind matches operation type kind; forbids categories on transfers (MASTER PROMPT §21, §27, §28).';

-- ---------------------------------------------------------------------
-- 2.11 fn_check_operation_sign()
-- §27 — the direction is defined by operation_type. The sign of
-- operations.amount must agree with operation_types.direction.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_check_operation_sign()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_direction TEXT;
BEGIN
    SELECT ot.direction INTO v_direction
      FROM operation_types ot
     WHERE ot.id = NEW.type_id;

    IF v_direction = 'IN' AND NEW.amount <= 0 THEN
        RAISE EXCEPTION
            'Inbound operation (direction=IN) requires a positive amount, got % (MASTER PROMPT §27).', NEW.amount
            USING ERRCODE = 'check_violation';
    END IF;

    IF v_direction = 'OUT' AND NEW.amount >= 0 THEN
        RAISE EXCEPTION
            'Outbound operation (direction=OUT) requires a negative amount, got % (MASTER PROMPT §27).', NEW.amount
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION fn_check_operation_sign() IS
'Validates operations.amount sign against operation_types.direction (MASTER PROMPT §27).';

-- ---------------------------------------------------------------------
-- 2.12 fn_protect_posted_operation()
-- Transfer ledger legs are immutable once posted. This guard is limited to
-- TRANSFER operations; broader reversal semantics are defined separately.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_protect_posted_operation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM operation_types WHERE id = OLD.type_id AND kind = 'TRANSFER'
    ) THEN
        IF OLD.status <> 'DRAFT' AND (
            NEW.id IS DISTINCT FROM OLD.id OR
            NEW.ip_id IS DISTINCT FROM OLD.ip_id OR
            NEW.type_id IS DISTINCT FROM OLD.type_id OR
            NEW.category_id IS DISTINCT FROM OLD.category_id OR
            NEW.counterparty_id IS DISTINCT FROM OLD.counterparty_id OR
            NEW.amount IS DISTINCT FROM OLD.amount OR
            NEW.operation_date IS DISTINCT FROM OLD.operation_date OR
            NEW.accrual_date IS DISTINCT FROM OLD.accrual_date OR
            NEW.external_id IS DISTINCT FROM OLD.external_id OR
            NEW.related_order_id IS DISTINCT FROM OLD.related_order_id OR
            NEW.related_operation_id IS DISTINCT FROM OLD.related_operation_id OR
            NEW.raw_payload_id IS DISTINCT FROM OLD.raw_payload_id OR
            NEW.account_id IS DISTINCT FROM OLD.account_id OR
            NEW.import_batch_id IS DISTINCT FROM OLD.import_batch_id OR
            NEW.import_fingerprint IS DISTINCT FROM OLD.import_fingerprint OR
            NEW.created_by IS DISTINCT FROM OLD.created_by
        ) THEN
            RAISE EXCEPTION
                'posted transfer operation % is immutable; '
                'use a paired transfer correction workflow.',
                OLD.id USING ERRCODE = 'check_violation';
        END IF;

        IF OLD.status = 'POSTED' AND NEW.status = 'REVERSED' THEN
            RAISE EXCEPTION
                'transfer operation % cannot be reversed independently; use a paired transfer correction workflow.',
                OLD.id USING ERRCODE = 'check_violation';
        END IF;

        IF NEW.status IS DISTINCT FROM OLD.status
           AND NOT (OLD.status = 'DRAFT' AND NEW.status = 'POSTED') THEN
            RAISE EXCEPTION
                'invalid transfer operation status transition from % to %.',
                OLD.status, NEW.status USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION fn_protect_posted_operation() IS
'Prevents mutation or one-leg reversal of POSTED transfer ledger operations; does not change other operation lifecycles.';

-- ---------------------------------------------------------------------
-- 2.12a fn_protect_posted_ledger_operation()
-- FIN-OPS-0001 / FINANCE-OPS-001 §7: once a NON-transfer operation leaves
-- DRAFT, its financial identity is immutable. Corrections go through
-- reversal / correction / refund / adjustment, never through UPDATE.
-- Transfer legs are guarded separately by fn_protect_posted_operation()
-- (ADR-015) and are deliberately skipped here.
--
-- Allowed status transitions: DRAFT -> DRAFT | POSTED, POSTED -> REVERSED.
-- The POSTED -> REVERSED edge keeps the current reverse_operation()
-- behaviour until ADR-016 decides the compensating reversal model.
-- Still mutable after posting (documented gaps, see TECHNICAL_DEBT TD-019):
--   comment        — overwritten by the current reverse_operation();
--   raw_payload_id, import_batch_id — ON DELETE SET NULL from tables that
--                    are not DELETE-protected yet;
--   status (only the edges above), updated_at (fn_set_updated_at).
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_protect_posted_ledger_operation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM operation_types WHERE id = OLD.type_id AND kind = 'TRANSFER'
    ) THEN
        RETURN NEW;
    END IF;

    IF OLD.status = 'DRAFT' THEN
        IF NEW.status NOT IN ('DRAFT', 'POSTED') THEN
            RAISE EXCEPTION
                'invalid operation status transition from % to % (FIN-OPS-0001).',
                OLD.status, NEW.status USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.id IS DISTINCT FROM OLD.id OR
       NEW.ip_id IS DISTINCT FROM OLD.ip_id OR
       NEW.type_id IS DISTINCT FROM OLD.type_id OR
       NEW.category_id IS DISTINCT FROM OLD.category_id OR
       NEW.counterparty_id IS DISTINCT FROM OLD.counterparty_id OR
       NEW.amount IS DISTINCT FROM OLD.amount OR
       NEW.operation_date IS DISTINCT FROM OLD.operation_date OR
       NEW.accrual_date IS DISTINCT FROM OLD.accrual_date OR
       NEW.external_id IS DISTINCT FROM OLD.external_id OR
       NEW.related_order_id IS DISTINCT FROM OLD.related_order_id OR
       NEW.related_operation_id IS DISTINCT FROM OLD.related_operation_id OR
       NEW.account_id IS DISTINCT FROM OLD.account_id OR
       NEW.import_fingerprint IS DISTINCT FROM OLD.import_fingerprint OR
       NEW.created_by IS DISTINCT FROM OLD.created_by OR
       NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION
            'operation % is % and its financial fields are immutable; '
            'use reversal / correction instead of UPDATE (FIN-OPS-0001, FINANCE-OPS-001 §7).',
            OLD.id, OLD.status USING ERRCODE = 'check_violation';
    END IF;

    IF NEW.status IS DISTINCT FROM OLD.status
       AND NOT (OLD.status = 'POSTED' AND NEW.status = 'REVERSED') THEN
        RAISE EXCEPTION
            'invalid operation status transition from % to % (FIN-OPS-0001).',
            OLD.status, NEW.status USING ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION fn_protect_posted_ledger_operation() IS
'FIN-OPS-0001: non-transfer operations are immutable after DRAFT; only DRAFT->POSTED and POSTED->REVERSED status edges are allowed. Transfer legs are guarded by fn_protect_posted_operation().';

-- ---------------------------------------------------------------------
-- 2.13 fn_protect_posted_transfer()
-- A posted transfer aggregate is immutable; one-leg reversal is blocked in
-- the service until a paired lifecycle decision is made.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_protect_posted_transfer()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.status = 'POSTED' AND (
        NEW.id IS DISTINCT FROM OLD.id OR
        NEW.from_ip_id IS DISTINCT FROM OLD.from_ip_id OR
        NEW.to_ip_id IS DISTINCT FROM OLD.to_ip_id OR
        NEW.from_account_id IS DISTINCT FROM OLD.from_account_id OR
        NEW.to_account_id IS DISTINCT FROM OLD.to_account_id OR
        NEW.amount IS DISTINCT FROM OLD.amount OR
        NEW.transfer_date IS DISTINCT FROM OLD.transfer_date OR
        NEW.reason IS DISTINCT FROM OLD.reason OR
        NEW.outbound_operation_id IS DISTINCT FROM OLD.outbound_operation_id OR
        NEW.inbound_operation_id IS DISTINCT FROM OLD.inbound_operation_id OR
        NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key OR
        NEW.status IS DISTINCT FROM OLD.status OR
        NEW.created_by IS DISTINCT FROM OLD.created_by
    ) THEN
        RAISE EXCEPTION
            'posted transfer % is immutable; use a paired transfer correction workflow.',
            OLD.id USING ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION fn_protect_posted_transfer() IS
'Prevents silent mutation or one-leg lifecycle changes to a POSTED transfer aggregate.';

CREATE OR REPLACE FUNCTION fn_transfer_ledger_pair_is_valid(p_transfer_id BIGINT)
RETURNS BOOLEAN
LANGUAGE plpgsql
STABLE
AS $$
BEGIN
    RETURN EXISTS (
        SELECT 1
        FROM transfers t
        JOIN operations outbound ON outbound.id = t.outbound_operation_id
        JOIN operation_types outbound_type ON outbound_type.id = outbound.type_id
        JOIN operations inbound ON inbound.id = t.inbound_operation_id
        JOIN operation_types inbound_type ON inbound_type.id = inbound.type_id
        WHERE t.id = p_transfer_id
          AND t.status = 'POSTED'
          AND t.from_ip_id <> t.to_ip_id
          AND t.from_account_id IS NOT NULL
          AND t.to_account_id IS NOT NULL
          AND t.idempotency_key IS NOT NULL
          AND length(btrim(t.idempotency_key)) > 0
          AND t.created_by IS NOT NULL
          AND outbound.status = 'POSTED'
          AND inbound.status = 'POSTED'
          AND outbound.ip_id = t.from_ip_id
          AND inbound.ip_id = t.to_ip_id
          AND outbound.account_id IS NOT DISTINCT FROM t.from_account_id
          AND inbound.account_id IS NOT DISTINCT FROM t.to_account_id
          AND outbound.amount = -t.amount
          AND inbound.amount = t.amount
          AND outbound.operation_date = t.transfer_date
          AND inbound.operation_date = t.transfer_date
          AND outbound.comment IS NOT DISTINCT FROM t.reason
          AND inbound.comment IS NOT DISTINCT FROM t.reason
          AND outbound.created_by = t.created_by
          AND inbound.created_by = t.created_by
          AND outbound.related_operation_id = inbound.id
          AND inbound.related_operation_id = outbound.id
          AND outbound_type.code = 'TRANSFER_OUT'
          AND outbound_type.direction = 'OUT'
          AND outbound_type.kind = 'TRANSFER'
          AND NOT outbound_type.affects_pnl
          AND inbound_type.code = 'TRANSFER_IN'
          AND inbound_type.direction = 'IN'
          AND inbound_type.kind = 'TRANSFER'
          AND NOT inbound_type.affects_pnl
          AND (
              SELECT count(*)
              FROM (
                  SELECT linked.outbound_operation_id AS operation_id FROM transfers linked
                  UNION ALL
                  SELECT linked.inbound_operation_id AS operation_id FROM transfers linked
              ) leg_refs
              WHERE leg_refs.operation_id = outbound.id
          ) = 1
          AND (
              SELECT count(*)
              FROM (
                  SELECT linked.outbound_operation_id AS operation_id FROM transfers linked
                  UNION ALL
                  SELECT linked.inbound_operation_id AS operation_id FROM transfers linked
              ) leg_refs
              WHERE leg_refs.operation_id = inbound.id
          ) = 1
    );
END;
$$;

COMMENT ON FUNCTION fn_transfer_ledger_pair_is_valid(BIGINT) IS
'Checks that a posted transfer and its two ledger operations have matching IPs, accounts, amount, type, date, actor, reason and reciprocal links.';

CREATE OR REPLACE FUNCTION fn_validate_transfer_ledger_pair()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    target_transfer_id BIGINT;
    operation_code TEXT;
    operation_status TEXT;
    reference_count BIGINT;
BEGIN
    IF TG_TABLE_NAME = 'transfers' THEN
        SELECT t.id INTO target_transfer_id
        FROM transfers t
        WHERE t.id = NEW.id AND t.status = 'POSTED';
        IF target_transfer_id IS NULL THEN
            RETURN NEW;
        END IF;
    ELSE
        SELECT ot.code, o.status INTO operation_code, operation_status
        FROM operations o
        JOIN operation_types ot ON ot.id = o.type_id
        WHERE o.id = NEW.id;
        IF NOT FOUND OR operation_status <> 'POSTED'
           OR operation_code NOT IN ('TRANSFER_OUT', 'TRANSFER_IN') THEN
            RETURN NEW;
        END IF;

        SELECT count(*), min(t.id) INTO reference_count, target_transfer_id
        FROM transfers t
        WHERE t.outbound_operation_id = NEW.id OR t.inbound_operation_id = NEW.id;
        IF reference_count <> 1 THEN
            RAISE EXCEPTION
                'posted transfer operation % must be linked to exactly one transfer',
                NEW.id USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    IF NOT fn_transfer_ledger_pair_is_valid(target_transfer_id) THEN
        RAISE EXCEPTION
            'posted transfer % does not have a matching ledger pair',
            target_transfer_id USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION fn_validate_transfer_ledger_pair() IS
'Enforces complete, reciprocal transfer/ledger linkage at transaction commit.';

-- =====================================================================
-- SECTION 3 — BASE TABLES (no outgoing FKs, or FKs only to other base tables)
-- =====================================================================

-- ---------------------------------------------------------------------
-- 3.1 ip_entities                                        [ORIGINAL #1, §17]
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ip_entities (
    id          BIGSERIAL       PRIMARY KEY,
    name        VARCHAR(255)    NOT NULL,
    bin         VARCHAR(12)     NOT NULL UNIQUE,
    is_active   BOOLEAN         NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    closed_at   TIMESTAMPTZ     NULL,
    -- production additions (original columns above are preserved as-is, §81)
    updated_at  TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT chk_ip_entities_bin_length  CHECK (length(bin) = 12),
    CONSTRAINT chk_ip_entities_bin_digits  CHECK (bin ~ '^[0-9]{12}$'),
    CONSTRAINT chk_ip_entities_name        CHECK (length(btrim(name)) > 0),
    CONSTRAINT chk_ip_entities_closed_at   CHECK (closed_at IS NULL OR closed_at >= created_at),
    -- an entity that is closed cannot remain active
    CONSTRAINT chk_ip_entities_active_state CHECK (NOT (is_active AND closed_at IS NOT NULL))
);

COMMENT ON TABLE  ip_entities            IS 'Individual entrepreneurs (ИП) the system keeps books for (MASTER PROMPT §17).';
COMMENT ON COLUMN ip_entities.bin        IS 'БИН — exactly 12 digits, unique (§17).';
COMMENT ON COLUMN ip_entities.closed_at  IS 'Date the ИП was closed; NULL while operating.';

-- ---------------------------------------------------------------------
-- 3.2 operation_types                                    [ORIGINAL #3, §20]
--
-- direction defines the sign of operations.amount (§27).
-- kind       links the operation to the P&L category system (§21).
-- affects_pnl = FALSE for transfers — they are neither income nor
--               expense (§28, §76).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS operation_types (
    id          BIGSERIAL       PRIMARY KEY,
    code        VARCHAR(50)     NOT NULL UNIQUE,
    name        VARCHAR(255)    NOT NULL,
    direction   VARCHAR(3)      NOT NULL,
    kind        VARCHAR(20)     NOT NULL,
    affects_pnl BOOLEAN         NOT NULL DEFAULT TRUE,
    is_system   BOOLEAN         NOT NULL DEFAULT FALSE,
    is_active   BOOLEAN         NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT chk_operation_types_direction CHECK (direction IN ('IN', 'OUT')),
    CONSTRAINT chk_operation_types_kind      CHECK (kind IN ('INCOME', 'EXPENSE', 'TRANSFER')),
    CONSTRAINT chk_operation_types_pnl       CHECK (
        (kind = 'TRANSFER' AND affects_pnl = FALSE) OR
        (kind <> 'TRANSFER')
    ),
    CONSTRAINT chk_operation_types_dir_kind  CHECK (
        (kind = 'INCOME'  AND direction = 'IN')  OR
        (kind = 'EXPENSE' AND direction = 'OUT') OR
        (kind = 'TRANSFER')
    )
);

COMMENT ON TABLE  operation_types             IS 'Financial operation types; direction drives amount sign (MASTER PROMPT §20, §27).';
COMMENT ON COLUMN operation_types.affects_pnl IS 'FALSE for transfers between ИП — not income, not expense (§28, §76).';
COMMENT ON COLUMN operation_types.is_system   IS 'TRUE for the 7 mandatory types from §20 — must not be deleted or renamed.';

-- ---------------------------------------------------------------------
-- 3.3 categories                                         [ORIGINAL #4, §21]
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS categories (
    id          BIGSERIAL       PRIMARY KEY,
    code        VARCHAR(50)     NOT NULL UNIQUE,
    name        VARCHAR(255)    NOT NULL,
    kind        VARCHAR(20)     NOT NULL,
    parent_id   BIGINT          NULL REFERENCES categories (id) ON DELETE RESTRICT,
    is_system   BOOLEAN         NOT NULL DEFAULT FALSE,
    is_active   BOOLEAN         NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT chk_categories_kind      CHECK (kind IN ('INCOME', 'EXPENSE')),
    CONSTRAINT chk_categories_name      CHECK (length(btrim(name)) > 0),
    CONSTRAINT chk_categories_no_self   CHECK (parent_id IS NULL OR parent_id <> id),
    CONSTRAINT uq_categories_name_kind  UNIQUE (name, kind)
);

COMMENT ON TABLE  categories      IS 'Income and expense categories (MASTER PROMPT §21).';
COMMENT ON COLUMN categories.kind IS 'INCOME | EXPENSE — must match operation_types.kind of the operation (§21).';

-- ---------------------------------------------------------------------
-- 3.4 counterparties                                     [ORIGINAL #5, §22]
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS counterparties (
    id          BIGSERIAL       PRIMARY KEY,
    name        VARCHAR(255)    NOT NULL,
    bin_iin     VARCHAR(12)     NULL,
    phone       VARCHAR(32)     NULL,
    comment     TEXT            NULL,
    created_at  TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- production additions (§81 — original columns preserved)
    is_active   BOOLEAN         NOT NULL DEFAULT TRUE,
    updated_at  TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT chk_counterparties_name    CHECK (length(btrim(name)) > 0),
    CONSTRAINT chk_counterparties_bin_iin CHECK (bin_iin IS NULL OR bin_iin ~ '^[0-9]{12}$')
);

COMMENT ON TABLE  counterparties         IS 'Suppliers, customers and other counterparties (MASTER PROMPT §22).';
COMMENT ON COLUMN counterparties.bin_iin IS 'БИН/ИИН — 12 digits when known (§22).';

-- ---------------------------------------------------------------------
-- 3.5 employees                                         [ORIGINAL #11, §29]
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS employees (
    id              BIGSERIAL       PRIMARY KEY,
    full_name       VARCHAR(255)    NOT NULL,
    phone           VARCHAR(32)     NULL,
    position        VARCHAR(255)    NULL,
    assigned_ip_id  BIGINT          NULL REFERENCES ip_entities (id) ON DELETE RESTRICT,
    hired_at        DATE            NULL,
    fired_at        DATE            NULL,
    status          VARCHAR(20)     NOT NULL DEFAULT 'ACTIVE',
    payment_type    VARCHAR(20)     NOT NULL,
    -- production additions (§81)
    base_salary     NUMERIC(15,2)   NULL,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT chk_employees_status       CHECK (status IN ('ACTIVE', 'ON_LEAVE', 'FIRED')),
    CONSTRAINT chk_employees_payment_type CHECK (payment_type IN ('PIECEWORK', 'SALARY', 'MIXED')),
    CONSTRAINT chk_employees_full_name    CHECK (length(btrim(full_name)) > 0),
    CONSTRAINT chk_employees_dates        CHECK (fired_at IS NULL OR hired_at IS NULL OR fired_at >= hired_at),
    CONSTRAINT chk_employees_fired_status CHECK (
        (status = 'FIRED'  AND fired_at IS NOT NULL) OR
        (status <> 'FIRED' AND fired_at IS NULL)
    ),
    -- a salaried or mixed employee must have a base salary defined
    CONSTRAINT chk_employees_base_salary  CHECK (
        (payment_type = 'PIECEWORK' AND base_salary IS NULL) OR
        (payment_type IN ('SALARY', 'MIXED') AND base_salary IS NOT NULL AND base_salary >= 0)
    )
);

COMMENT ON TABLE  employees              IS 'Production and office employees (MASTER PROMPT §29).';
COMMENT ON COLUMN employees.payment_type IS 'PIECEWORK (сдельная) | SALARY (оклад) | MIXED (смешанная) (§29).';
COMMENT ON COLUMN employees.base_salary  IS 'Monthly base salary in KZT for SALARY/MIXED employees; NULL for PIECEWORK (§35).';

-- ---------------------------------------------------------------------
-- 3.6 work_operation_types                              [ORIGINAL #12, §30]
--
-- calc_mode reflects the §30 specification:
--   per_unit  — quantity is measured in `unit` (м2, пог_м, отверстие, шт)
--   per_hour  — quantity is hours
--   per_order — quantity is trips/orders
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS work_operation_types (
    id          BIGSERIAL       PRIMARY KEY,
    code        VARCHAR(50)     NOT NULL UNIQUE,
    name        VARCHAR(255)    NOT NULL,
    unit        VARCHAR(20)     NOT NULL,
    calc_mode   VARCHAR(20)     NOT NULL,
    is_active   BOOLEAN         NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT chk_work_operation_types_calc_mode CHECK (calc_mode IN ('per_unit', 'per_hour', 'per_order')),
    CONSTRAINT chk_work_operation_types_unit      CHECK (length(btrim(unit)) > 0),
    CONSTRAINT chk_work_operation_types_name      CHECK (length(btrim(name)) > 0)
);

COMMENT ON TABLE  work_operation_types           IS 'Piecework operation catalogue — 11 mandatory types (MASTER PROMPT §30).';
COMMENT ON COLUMN work_operation_types.calc_mode IS 'per_unit | per_hour | per_order (§30).';
COMMENT ON COLUMN work_operation_types.unit      IS 'Unit of measure: м2, пог_м, отверстие, шт, часы, рейс (§30).';

-- ---------------------------------------------------------------------
-- 3.7 payroll_periods                                   [ORIGINAL #15, §34]
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payroll_periods (
    id          BIGSERIAL       PRIMARY KEY,
    start_date  DATE            NOT NULL,
    end_date    DATE            NOT NULL,
    status      VARCHAR(20)     NOT NULL DEFAULT 'OPEN',
    -- production additions (§81)
    created_at  TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    closed_at   TIMESTAMPTZ     NULL,

    CONSTRAINT chk_payroll_periods_dates  CHECK (end_date >= start_date),
    CONSTRAINT chk_payroll_periods_status CHECK (status IN ('OPEN', 'PROCESSING', 'CLOSED', 'PAID')),
    CONSTRAINT uq_payroll_periods_range   UNIQUE (start_date, end_date),
    -- no two payroll periods may overlap in time
    CONSTRAINT excl_payroll_periods_overlap EXCLUDE USING gist (
        daterange(start_date, end_date, '[]') WITH &&
    )
);

COMMENT ON TABLE  payroll_periods        IS 'Payroll periods (MASTER PROMPT §34).';
COMMENT ON COLUMN payroll_periods.status IS 'OPEN | PROCESSING | CLOSED | PAID (§34).';

-- ---------------------------------------------------------------------
-- 3.8 users                       [ARCHITECTURAL EXTENSION, §52, §55]
--
-- Implements: REQ-0226, REQ-0218. Required by §52 (users/roles),
-- §53 (backend RBAC), §54 (audit_log.user_id), §55 (authentication).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id              BIGSERIAL       PRIMARY KEY,
    username        VARCHAR(100)    NOT NULL UNIQUE,
    email           VARCHAR(255)    NULL UNIQUE,
    full_name       VARCHAR(255)    NULL,
    password_hash   TEXT            NOT NULL,
    is_active       BOOLEAN         NOT NULL DEFAULT TRUE,
    employee_id     BIGINT          NULL REFERENCES employees (id) ON DELETE SET NULL,
    last_login_at   TIMESTAMPTZ     NULL,
    failed_logins   INTEGER         NOT NULL DEFAULT 0,
    locked_until    TIMESTAMPTZ     NULL,
    -- MASTER PROMPT §17 — password-change invalidation: JWTs issued before
    -- this moment are rejected by current_principal(). NULL = never changed.
    password_changed_at TIMESTAMPTZ NULL,
    -- MASTER PROMPT §17 — server-side token revocation: JWTs carry the
    -- version at issue; current_principal() rejects tokens whose version
    -- differs from the stored one. /auth/logout and the ADMIN
    -- revoke-sessions endpoint bump it, instantly killing every session.
    token_version   INTEGER         NOT NULL DEFAULT 1,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT chk_users_username CHECK (username ~ '^[A-Za-z0-9._-]{3,100}$'),
    CONSTRAINT chk_users_email    CHECK (email IS NULL OR email ~ '^[^@[:space:]]+@[^@[:space:]]+\.[A-Za-z]{2,}$'),
    -- §55: passwords are stored only as a secure hash, never in plaintext.
    -- Accepted prefixes: bcrypt ($2a$/$2b$/$2y$), argon2 ($argon2id$/$argon2i$/$argon2d$), scrypt ($scrypt$).
    CONSTRAINT chk_users_password_hash CHECK (
        password_hash ~ '^\$(2[aby]|argon2(id|i|d)|scrypt)\$'
    ),
    CONSTRAINT chk_users_failed_logins CHECK (failed_logins >= 0)
);

COMMENT ON TABLE  users               IS 'ARCHITECTURAL EXTENSION — application users (MASTER PROMPT §52, §55).';
COMMENT ON COLUMN users.password_hash IS 'Secure password hash only (bcrypt/argon2/scrypt). Plaintext is rejected by CHECK (§55).';

-- ---------------------------------------------------------------------
-- 3.9 roles                       [ARCHITECTURAL EXTENSION, §52, §53]
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS roles (
    id          BIGSERIAL       PRIMARY KEY,
    code        VARCHAR(50)     NOT NULL UNIQUE,
    name        VARCHAR(255)    NOT NULL,
    description TEXT            NULL,
    is_system   BOOLEAN         NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,

    -- Minimum length is 2: the mandatory role 'HR' (§52) is two characters.
    CONSTRAINT chk_roles_code CHECK (code ~ '^[A-Z][A-Z_]{1,49}$')
);

COMMENT ON TABLE roles IS 'ARCHITECTURAL EXTENSION — RBAC roles: ADMIN, DIRECTOR, ACCOUNTANT, HR, SHOP_MANAGER, BRIGADIER, VIEWER (MASTER PROMPT §52, §53).';

-- ---------------------------------------------------------------------
-- 3.10 user_roles                 [ARCHITECTURAL EXTENSION, §52]
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_roles (
    user_id     BIGINT          NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    role_id     BIGINT          NOT NULL REFERENCES roles (id) ON DELETE RESTRICT,
    granted_at  TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    granted_by  BIGINT          NULL REFERENCES users (id) ON DELETE SET NULL,

    CONSTRAINT pk_user_roles PRIMARY KEY (user_id, role_id)
);

COMMENT ON TABLE user_roles IS 'ARCHITECTURAL EXTENSION — user ↔ role assignment (MASTER PROMPT §52).';

-- ---------------------------------------------------------------------
-- 3.11 audit_log                  [ARCHITECTURAL EXTENSION, §54]
--
-- Implements: REQ-0217. Append-only.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    id          BIGSERIAL       PRIMARY KEY,
    user_id     BIGINT          NULL REFERENCES users (id) ON DELETE SET NULL,
    entity      VARCHAR(100)    NOT NULL,
    entity_id   BIGINT          NULL,
    action      VARCHAR(50)     NOT NULL,
    old_data    JSONB           NULL,
    new_data    JSONB           NULL,
    created_at  TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT chk_audit_log_action CHECK (action IN (
        'INSERT', 'UPDATE', 'DELETE',
        'CREATE', 'CORRECTION', 'PAYMENT', 'REFUND',
        'STATUS_CHANGE', 'RATE_CHANGE', 'PERMISSION_CHANGE', 'FINANCIAL_CHANGE',
        'LOGIN', 'LOGIN_FAILED', 'LOGOUT', 'REVOKE_SESSIONS'
    )),
    CONSTRAINT chk_audit_log_payload CHECK (old_data IS NOT NULL OR new_data IS NOT NULL OR action IN ('LOGIN', 'LOGIN_FAILED', 'LOGOUT', 'REVOKE_SESSIONS'))
);

COMMENT ON TABLE  audit_log          IS 'ARCHITECTURAL EXTENSION — append-only audit trail (MASTER PROMPT §54). Secrets are masked by fn_audit().';
COMMENT ON COLUMN audit_log.user_id  IS 'NULL when the change was made by a migration, ETL job or DBA — never faked.';

-- ---------------------------------------------------------------------
-- 3.12 products                   [ARCHITECTURAL EXTENSION, §46]
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS products (
    id              BIGSERIAL       PRIMARY KEY,
    sku             VARCHAR(64)     NOT NULL UNIQUE,
    name            VARCHAR(255)    NOT NULL,
    description     TEXT            NULL,
    unit            VARCHAR(20)     NOT NULL DEFAULT 'шт',
    selling_price   NUMERIC(15,2)   NOT NULL DEFAULT 0,
    is_active       BOOLEAN         NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT chk_products_sku           CHECK (length(btrim(sku)) > 0),
    CONSTRAINT chk_products_name          CHECK (length(btrim(name)) > 0),
    CONSTRAINT chk_products_selling_price CHECK (selling_price >= 0)
);

COMMENT ON TABLE products IS 'ARCHITECTURAL EXTENSION — product catalogue (MASTER PROMPT §46).';

-- ---------------------------------------------------------------------
-- 3.13 materials                  [ARCHITECTURAL EXTENSION, §48]
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS materials (
    id              BIGSERIAL       PRIMARY KEY,
    sku             VARCHAR(64)     NOT NULL UNIQUE,
    name            VARCHAR(255)    NOT NULL,
    unit            VARCHAR(20)     NOT NULL,
    current_price   NUMERIC(15,2)   NOT NULL,
    supplier_id     BIGINT          NULL REFERENCES counterparties (id) ON DELETE SET NULL,
    is_active       BOOLEAN         NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT chk_materials_sku           CHECK (length(btrim(sku)) > 0),
    CONSTRAINT chk_materials_name          CHECK (length(btrim(name)) > 0),
    CONSTRAINT chk_materials_unit          CHECK (length(btrim(unit)) > 0),
    CONSTRAINT chk_materials_current_price CHECK (current_price >= 0)
);

COMMENT ON TABLE  materials               IS 'ARCHITECTURAL EXTENSION — materials catalogue (MASTER PROMPT §48).';
COMMENT ON COLUMN materials.current_price IS 'Current price. Historical cost is frozen in order_materials.price_at_time (§38).';

-- ---------------------------------------------------------------------
-- 3.14 accounts                   [ARCHITECTURAL EXTENSION, §51]
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS accounts (
    id          BIGSERIAL       PRIMARY KEY,
    ip_id       BIGINT          NOT NULL REFERENCES ip_entities (id) ON DELETE RESTRICT,
    type        VARCHAR(20)     NOT NULL,
    name        VARCHAR(255)    NOT NULL,
    bank_code   VARCHAR(20)     NULL,
    is_active   BOOLEAN         NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT chk_accounts_type CHECK (type IN ('KASPI_PAY', 'KASPI_SHOP', 'BANK', 'CASH', 'OTHER')),
    CONSTRAINT chk_accounts_name CHECK (length(btrim(name)) > 0),
    CONSTRAINT uq_accounts_ip_name UNIQUE (ip_id, name)
);

COMMENT ON TABLE accounts IS 'ARCHITECTURAL EXTENSION — money accounts per ИП (MASTER PROMPT §51).';
CREATE UNIQUE INDEX IF NOT EXISTS uq_accounts_id_ip ON accounts (id, ip_id);

-- ---------------------------------------------------------------------
-- 3.15 tax_rates_history          [ARCHITECTURAL EXTENSION, §75]
--
-- Implements: REQ-0219. §75 forbids hard-coding changeable tax
-- parameters in Python. Actual values are NOT seeded — the tax regime
-- is an open question (OQ-001) and §94 forbids inventing them.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tax_rates_history (
    id              BIGSERIAL       PRIMARY KEY,
    code            VARCHAR(50)     NOT NULL,
    name            VARCHAR(255)    NOT NULL,
    rate            NUMERIC(9,6)    NOT NULL,
    base            VARCHAR(50)     NOT NULL,
    ip_id           BIGINT          NULL REFERENCES ip_entities (id) ON DELETE RESTRICT,
    effective_from  TIMESTAMPTZ     NOT NULL,
    effective_to    TIMESTAMPTZ     NULL,
    legal_reference TEXT            NULL,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT chk_tax_rates_history_rate   CHECK (rate >= 0),
    CONSTRAINT chk_tax_rates_history_period CHECK (effective_to IS NULL OR effective_to > effective_from),
    CONSTRAINT chk_tax_rates_history_base   CHECK (base IN ('REVENUE', 'PROFIT', 'PAYROLL', 'FIXED')),
    -- no overlapping periods for the same tax code within the same scope
    CONSTRAINT excl_tax_rates_history_overlap EXCLUDE USING gist (
        code WITH =,
        coalesce(ip_id, 0) WITH =,
        tstzrange(effective_from, effective_to, '[)') WITH &&
    )
);

COMMENT ON TABLE tax_rates_history IS 'ARCHITECTURAL EXTENSION — historised tax parameters; never hard-coded (MASTER PROMPT §75). Values pending OQ-001.';

-- ---------------------------------------------------------------------
-- 3.16 import_batches             [ARCHITECTURAL EXTENSION, §39–§41]
--
-- Implements: REQ-0228, REQ-0302. Supports Excel ETL run reporting and
-- idempotency (dry-run counters are stored for audit).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS import_batches (
    id                  BIGSERIAL       PRIMARY KEY,
    source_type         VARCHAR(30)     NOT NULL,
    source_name         TEXT            NOT NULL,
    source_hash         CHAR(64)        NULL,
    ip_id               BIGINT          NULL REFERENCES ip_entities (id) ON DELETE RESTRICT,
    is_dry_run          BOOLEAN         NOT NULL DEFAULT FALSE,
    started_at          TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at         TIMESTAMPTZ     NULL,
    rows_total          INTEGER         NOT NULL DEFAULT 0,
    rows_inserted       INTEGER         NOT NULL DEFAULT 0,
    rows_skipped        INTEGER         NOT NULL DEFAULT 0,
    rows_duplicates     INTEGER         NOT NULL DEFAULT 0,
    rows_errors         INTEGER         NOT NULL DEFAULT 0,
    status              VARCHAR(20)     NOT NULL DEFAULT 'RUNNING',
    error_details       JSONB           NULL,
    created_by          BIGINT          NULL REFERENCES users (id) ON DELETE SET NULL,

    CONSTRAINT chk_import_batches_source_type CHECK (source_type IN ('EXCEL', 'KASPI_SHOP', 'KASPI_PAY', 'BANK', 'MANUAL')),
    CONSTRAINT chk_import_batches_status      CHECK (status IN ('RUNNING', 'SUCCESS', 'PARTIAL', 'FAILED', 'DRY_RUN')),
    CONSTRAINT chk_import_batches_counters    CHECK (
        rows_total >= 0 AND rows_inserted >= 0 AND rows_skipped >= 0
        AND rows_duplicates >= 0 AND rows_errors >= 0
    ),
    CONSTRAINT chk_import_batches_finished    CHECK (finished_at IS NULL OR finished_at >= started_at),
    CONSTRAINT chk_import_batches_dry_run     CHECK (
        (is_dry_run = TRUE  AND status IN ('RUNNING', 'DRY_RUN', 'FAILED')) OR
        (is_dry_run = FALSE)
    )
);

COMMENT ON TABLE  import_batches            IS 'ARCHITECTURAL EXTENSION — ETL / sync run registry with dry-run reporting (MASTER PROMPT §39–§41).';
COMMENT ON COLUMN import_batches.source_hash IS 'SHA-256 of the source file — detects re-import of an identical file (§41).';

-- =====================================================================
-- SECTION 4 — DEPENDENT TABLES
-- =====================================================================

-- ---------------------------------------------------------------------
-- 4.1 kaspi_api_credentials                              [ORIGINAL #2, §18]
--
-- §18 — the token is stored encrypted and never exposed in plaintext.
-- §19 / ADR-009 — AES-256-GCM; the encryption key lives in the
-- environment, NEVER in the database. Only key_version is stored here.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS kaspi_api_credentials (
    id              BIGSERIAL       PRIMARY KEY,
    ip_id           BIGINT          NOT NULL REFERENCES ip_entities (id) ON DELETE RESTRICT,
    api_type        VARCHAR(10)     NOT NULL,
    encrypted_token BYTEA           NOT NULL,
    expires_at      TIMESTAMPTZ     NULL,
    status          VARCHAR(20)     NOT NULL DEFAULT 'ACTIVE',
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- production additions (§81 — original columns preserved)
    key_version     SMALLINT        NOT NULL DEFAULT 1,
    last_used_at    TIMESTAMPTZ     NULL,
    rotated_at      TIMESTAMPTZ     NULL,

    CONSTRAINT chk_kaspi_api_credentials_api_type CHECK (api_type IN ('SHOP', 'PAY')),
    CONSTRAINT chk_kaspi_api_credentials_status   CHECK (status IN ('ACTIVE', 'EXPIRED', 'ERROR', 'DISABLED')),
    -- AES-256-GCM envelope: key_version(1) || nonce(12) || ciphertext(>=1) || tag(16)
    CONSTRAINT chk_kaspi_api_credentials_token_len CHECK (octet_length(encrypted_token) >= 30),
    CONSTRAINT chk_kaspi_api_credentials_key_ver   CHECK (key_version >= 1),
    -- exactly one ACTIVE credential per (ИП, api_type) — see partial unique index below
    CONSTRAINT chk_kaspi_api_credentials_expired   CHECK (
        status <> 'EXPIRED' OR expires_at IS NOT NULL
    )
);

COMMENT ON TABLE  kaspi_api_credentials                 IS 'Kaspi API tokens per ИП, encrypted at rest (MASTER PROMPT §18, §19).';
COMMENT ON COLUMN kaspi_api_credentials.encrypted_token IS 'AES-256-GCM envelope: key_version(1)||nonce(12)||ciphertext||tag(16). Plaintext is never stored or logged (§18, §19, ADR-009).';
COMMENT ON COLUMN kaspi_api_credentials.key_version     IS 'Encryption key version for rotation. The key itself is NEVER stored in the database (§19).';

-- ---------------------------------------------------------------------
-- 4.2 orders                                             [ORIGINAL #6, §23]
--
-- §24 — a Kaspi Shop order is NOT a bank inflow at creation time.
-- No financial operation is generated here; money appears only through
-- the payout registry → actual payment → operations.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS orders (
    id                BIGSERIAL       PRIMARY KEY,
    ip_id             BIGINT          NOT NULL REFERENCES ip_entities (id) ON DELETE RESTRICT,
    external_order_id VARCHAR(100)    NULL,
    customer_name     VARCHAR(255)    NULL,
    customer_phone    VARCHAR(32)     NULL,
    delivery_address  TEXT            NULL,
    status            VARCHAR(20)     NOT NULL DEFAULT 'NEW',
    total_amount      NUMERIC(15,2)   NOT NULL DEFAULT 0,
    kaspi_commission  NUMERIC(15,2)   NOT NULL DEFAULT 0,
    delivery_fee      NUMERIC(15,2)   NOT NULL DEFAULT 0,
    created_at        TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- production additions (§81)
    source            VARCHAR(20)     NOT NULL DEFAULT 'MANUAL',
    order_date        TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at      TIMESTAMPTZ     NULL,
    raw_payload_id    BIGINT          NULL,      -- FK added in SECTION 5 (circular dependency)
    comment           TEXT            NULL,

    CONSTRAINT chk_orders_status CHECK (status IN (
        'NEW', 'APPROVED', 'ASSEMBLING', 'DELIVERING', 'COMPLETED', 'CANCELLED', 'RETURNED'
    )),
    CONSTRAINT chk_orders_source           CHECK (source IN ('KASPI_SHOP', 'KASPI_PAY', 'MANUAL', 'EXCEL')),
    CONSTRAINT chk_orders_total_amount     CHECK (total_amount >= 0),
    CONSTRAINT chk_orders_kaspi_commission CHECK (kaspi_commission >= 0),
    CONSTRAINT chk_orders_delivery_fee     CHECK (delivery_fee >= 0),
    CONSTRAINT chk_orders_completed_at     CHECK (completed_at IS NULL OR completed_at >= order_date),
    -- §23: UNIQUE (ip_id, external_order_id)
    CONSTRAINT uq_orders_ip_external UNIQUE (ip_id, external_order_id)
);

COMMENT ON TABLE  orders                  IS 'Customer orders from Kaspi Shop, Kaspi Pay, Excel or manual entry (MASTER PROMPT §23).';
COMMENT ON COLUMN orders.total_amount     IS 'Order revenue in KZT as reported by the source. Not recognised as cash until payout (§24).';
COMMENT ON COLUMN orders.kaspi_commission IS 'Commission as reported by Kaspi. Not computed by this system — the rule is unknown (OQ-005, §94).';
COMMENT ON COLUMN orders.external_order_id IS 'Order id in the source system; UNIQUE per ИП (§23). NULL for purely internal orders.';

-- ---------------------------------------------------------------------
-- 4.3 order_status_history        [ARCHITECTURAL EXTENSION, §50]
--
-- Implements: REQ-0212. Written automatically by
-- fn_log_order_status_change().
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS order_status_history (
    id          BIGSERIAL       PRIMARY KEY,
    order_id    BIGINT          NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
    old_status  VARCHAR(20)     NULL,
    new_status  VARCHAR(20)     NOT NULL,
    changed_at  TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    changed_by  BIGINT          NULL REFERENCES users (id) ON DELETE SET NULL,
    comment     TEXT            NULL,

    CONSTRAINT chk_order_status_history_new CHECK (new_status IN (
        'NEW', 'APPROVED', 'ASSEMBLING', 'DELIVERING', 'COMPLETED', 'CANCELLED', 'RETURNED'
    )),
    CONSTRAINT chk_order_status_history_old CHECK (old_status IS NULL OR old_status IN (
        'NEW', 'APPROVED', 'ASSEMBLING', 'DELIVERING', 'COMPLETED', 'CANCELLED', 'RETURNED'
    )),
    CONSTRAINT chk_order_status_history_change CHECK (old_status IS DISTINCT FROM new_status)
);

COMMENT ON TABLE order_status_history IS 'ARCHITECTURAL EXTENSION — every order status transition (MASTER PROMPT §50).';

-- ---------------------------------------------------------------------
-- 4.4 order_items                 [ARCHITECTURAL EXTENSION, §47]
--
-- §47 — the historical price is kept in the order item itself.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS order_items (
    id            BIGSERIAL       PRIMARY KEY,
    order_id      BIGINT          NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
    product_id    BIGINT          NULL REFERENCES products (id) ON DELETE RESTRICT,
    quantity      NUMERIC(15,3)   NOT NULL,
    unit_price    NUMERIC(15,2)   NOT NULL,
    discount      NUMERIC(15,2)   NOT NULL DEFAULT 0,
    total_amount  NUMERIC(15,2)   GENERATED ALWAYS AS (
                      round(quantity * unit_price, 2) - discount
                  ) STORED,
    -- production additions (§81)
    product_name  VARCHAR(255)    NULL,
    created_at    TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT chk_order_items_quantity   CHECK (quantity > 0),
    CONSTRAINT chk_order_items_unit_price CHECK (unit_price >= 0),
    CONSTRAINT chk_order_items_discount   CHECK (discount >= 0 AND discount <= round(quantity * unit_price, 2))
);

COMMENT ON TABLE  order_items              IS 'ARCHITECTURAL EXTENSION — order lines with historical price (MASTER PROMPT §47).';
COMMENT ON COLUMN order_items.unit_price   IS 'Price at the moment of sale — historical, never re-read from products (§47).';
COMMENT ON COLUMN order_items.total_amount IS 'GENERATED: round(quantity*unit_price,2) - discount (§47).';
COMMENT ON COLUMN order_items.product_name IS 'Denormalised product name snapshot for external items with no catalogue entry.';

-- ---------------------------------------------------------------------
-- 4.5 order_materials             [ARCHITECTURAL EXTENSION, §49]
--
-- §38 — price_at_time is frozen by trigger.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS order_materials (
    id             BIGSERIAL       PRIMARY KEY,
    order_id       BIGINT          NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
    material_id    BIGINT          NOT NULL REFERENCES materials (id) ON DELETE RESTRICT,
    quantity       NUMERIC(15,3)   NOT NULL,
    price_at_time  NUMERIC(15,2)   NOT NULL,
    total_cost     NUMERIC(15,2)   GENERATED ALWAYS AS (
                       round(quantity * price_at_time, 2)
                   ) STORED,
    -- production additions (§81)
    used_at        TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by     BIGINT          NULL REFERENCES users (id) ON DELETE SET NULL,
    created_at     TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at     TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT chk_order_materials_quantity      CHECK (quantity > 0),
    CONSTRAINT chk_order_materials_price_at_time CHECK (price_at_time >= 0)
);

COMMENT ON TABLE  order_materials               IS 'ARCHITECTURAL EXTENSION — materials consumed by an order (MASTER PROMPT §49).';
COMMENT ON COLUMN order_materials.price_at_time IS 'FROZEN material price at the moment of use; immutable (§38, ADR-008).';
COMMENT ON COLUMN order_materials.total_cost    IS 'GENERATED: round(quantity*price_at_time,2) — the material part of direct cost (§37).';

-- ---------------------------------------------------------------------
-- 4.6 raw_kaspi_payloads                                 [ORIGINAL #7, §26]
--
-- §26 — the original payload is kept for audit / debugging /
-- reprocessing. Fields of the payload are NOT invented (§94) — the raw
-- JSON is the source of truth until the official spec is available.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw_kaspi_payloads (
    id           BIGSERIAL       PRIMARY KEY,
    ip_id        BIGINT          NOT NULL REFERENCES ip_entities (id) ON DELETE RESTRICT,
    request_type VARCHAR(50)     NOT NULL,
    received_at  TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    payload      JSONB           NOT NULL,
    -- production additions (§81)
    api_type     VARCHAR(10)     NULL,
    external_id  VARCHAR(100)    NULL,
    payload_hash CHAR(64)        NULL,
    processed_at TIMESTAMPTZ     NULL,
    sync_log_id  BIGINT          NULL,      -- FK added in SECTION 5 (circular dependency)

    CONSTRAINT chk_raw_kaspi_payloads_api_type CHECK (api_type IS NULL OR api_type IN ('SHOP', 'PAY')),
    CONSTRAINT chk_raw_kaspi_payloads_type     CHECK (length(btrim(request_type)) > 0)
);

COMMENT ON TABLE  raw_kaspi_payloads              IS 'Original Kaspi API payloads kept for audit, debugging and reprocessing (MASTER PROMPT §26).';
COMMENT ON COLUMN raw_kaspi_payloads.payload_hash IS 'SHA-256 of the canonical payload — deduplicates identical responses (§41).';

-- ---------------------------------------------------------------------
-- 4.7 sync_log                                          [ORIGINAL #10, §45]
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sync_log (
    id                BIGSERIAL       PRIMARY KEY,
    ip_id             BIGINT          NOT NULL REFERENCES ip_entities (id) ON DELETE RESTRICT,
    api_type          VARCHAR(10)     NOT NULL,
    sync_started_at   TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sync_finished_at  TIMESTAMPTZ     NULL,
    records_fetched   INTEGER         NOT NULL DEFAULT 0,
    records_inserted  INTEGER         NOT NULL DEFAULT 0,
    records_updated   INTEGER         NOT NULL DEFAULT 0,
    errors_count      INTEGER         NOT NULL DEFAULT 0,
    error_details     JSONB           NULL,
    -- production additions (§81)
    status            VARCHAR(20)     NOT NULL DEFAULT 'RUNNING',
    period_from       TIMESTAMPTZ     NULL,
    period_to         TIMESTAMPTZ     NULL,

    CONSTRAINT chk_sync_log_api_type CHECK (api_type IN ('SHOP', 'PAY')),
    CONSTRAINT chk_sync_log_status   CHECK (status IN ('RUNNING', 'SUCCESS', 'PARTIAL', 'FAILED')),
    CONSTRAINT chk_sync_log_counters CHECK (
        records_fetched >= 0 AND records_inserted >= 0
        AND records_updated >= 0 AND errors_count >= 0
    ),
    CONSTRAINT chk_sync_log_finished CHECK (sync_finished_at IS NULL OR sync_finished_at >= sync_started_at),
    CONSTRAINT chk_sync_log_period   CHECK (period_to IS NULL OR period_from IS NULL OR period_to >= period_from)
);

COMMENT ON TABLE sync_log IS 'Kaspi synchronisation runs with counters and errors (MASTER PROMPT §45).';

-- ---------------------------------------------------------------------
-- 4.8 kaspi_payouts               [ARCHITECTURAL EXTENSION, §24, §44]
--
-- Implements: REQ-0221, REQ-0202. The payout registry is the step where
-- Kaspi Shop revenue becomes real money (§24).
-- Amounts are stored exactly as reported by Kaspi; no commission rule is
-- inferred (OQ-004, OQ-005, §94).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS kaspi_payouts (
    id                BIGSERIAL       PRIMARY KEY,
    ip_id             BIGINT          NOT NULL REFERENCES ip_entities (id) ON DELETE RESTRICT,
    external_payout_id VARCHAR(100)   NULL,
    payout_date       DATE            NOT NULL,
    gross_amount      NUMERIC(15,2)   NOT NULL,
    commission_amount NUMERIC(15,2)   NOT NULL DEFAULT 0,
    delivery_amount   NUMERIC(15,2)   NOT NULL DEFAULT 0,
    refund_amount     NUMERIC(15,2)   NOT NULL DEFAULT 0,
    net_amount        NUMERIC(15,2)   NOT NULL,
    status            VARCHAR(20)     NOT NULL DEFAULT 'EXPECTED',
    operation_id      BIGINT          NULL,      -- FK added in SECTION 5 (circular dependency)
    raw_payload_id    BIGINT          NULL REFERENCES raw_kaspi_payloads (id) ON DELETE SET NULL,
    reconciled_at     TIMESTAMPTZ     NULL,
    created_at        TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT chk_kaspi_payouts_status CHECK (status IN (
        'EXPECTED', 'RECEIVED', 'RECONCILED', 'DISCREPANCY', 'CANCELLED'
    )),
    CONSTRAINT chk_kaspi_payouts_gross      CHECK (gross_amount >= 0),
    CONSTRAINT chk_kaspi_payouts_commission CHECK (commission_amount >= 0),
    CONSTRAINT chk_kaspi_payouts_delivery   CHECK (delivery_amount >= 0),
    CONSTRAINT chk_kaspi_payouts_refund     CHECK (refund_amount >= 0),
    CONSTRAINT uq_kaspi_payouts_external    UNIQUE (ip_id, external_payout_id)
);

COMMENT ON TABLE  kaspi_payouts            IS 'ARCHITECTURAL EXTENSION — Kaspi payout registry; the point where Shop revenue becomes cash (MASTER PROMPT §24, §44).';
COMMENT ON COLUMN kaspi_payouts.net_amount IS 'Net amount as reported by Kaspi. Stored, not computed — the payout rule is unknown (OQ-004, §94).';

-- ---------------------------------------------------------------------
-- 4.9 kaspi_payout_items          [ARCHITECTURAL EXTENSION, §44]
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS kaspi_payout_items (
    id                BIGSERIAL       PRIMARY KEY,
    payout_id         BIGINT          NOT NULL REFERENCES kaspi_payouts (id) ON DELETE CASCADE,
    order_id          BIGINT          NULL REFERENCES orders (id) ON DELETE RESTRICT,
    external_order_id VARCHAR(100)    NULL,
    gross_amount      NUMERIC(15,2)   NOT NULL,
    commission_amount NUMERIC(15,2)   NOT NULL DEFAULT 0,
    delivery_amount   NUMERIC(15,2)   NOT NULL DEFAULT 0,
    refund_amount     NUMERIC(15,2)   NOT NULL DEFAULT 0,
    net_amount        NUMERIC(15,2)   NOT NULL,
    item_type         VARCHAR(20)     NOT NULL DEFAULT 'SALE',
    match_status      VARCHAR(30)     NOT NULL DEFAULT 'UNMATCHED',
    created_at        TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT chk_kaspi_payout_items_type CHECK (item_type IN ('SALE', 'REFUND', 'ADJUSTMENT', 'FEE', 'DELIVERY')),
    CONSTRAINT chk_kaspi_payout_items_match CHECK (match_status IN (
        'UNMATCHED', 'MATCHED', 'AMOUNT_MISMATCH', 'COMMISSION_MISMATCH',
        'DELIVERY_MISMATCH', 'DUPLICATE', 'MISSING_ORDER', 'PARTIAL', 'UNEXPLAINED'
    )),
    CONSTRAINT chk_kaspi_payout_items_amounts CHECK (
        commission_amount >= 0 AND delivery_amount >= 0 AND refund_amount >= 0
    ),
    CONSTRAINT chk_kaspi_payout_items_order_ref CHECK (
        order_id IS NOT NULL OR external_order_id IS NOT NULL OR item_type IN ('ADJUSTMENT', 'FEE')
    )
);

COMMENT ON TABLE  kaspi_payout_items              IS 'ARCHITECTURAL EXTENSION — payout lines used for Order ↔ Payout ↔ Operation reconciliation (MASTER PROMPT §44).';
COMMENT ON COLUMN kaspi_payout_items.match_status IS 'Reconciliation verdict — covers all 8 discrepancy classes of §44.';

-- ---------------------------------------------------------------------
-- 4.10 operations                                        [ORIGINAL #8, §27]
--
-- The central financial ledger.
-- §27  — amount NUMERIC(15,2), amount != 0, direction comes from type.
-- §16  — physical DELETE is blocked by trigger.
-- §41  — (ip_id, type_id, external_id) is unique when external_id is set,
--         which makes API/ETL retries idempotent (ADR-010).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS operations (
    id                  BIGSERIAL       PRIMARY KEY,
    ip_id               BIGINT          NOT NULL REFERENCES ip_entities (id) ON DELETE RESTRICT,
    type_id             BIGINT          NOT NULL REFERENCES operation_types (id) ON DELETE RESTRICT,
    category_id         BIGINT          NULL REFERENCES categories (id) ON DELETE RESTRICT,
    counterparty_id     BIGINT          NULL REFERENCES counterparties (id) ON DELETE RESTRICT,
    amount              NUMERIC(15,2)   NOT NULL,
    operation_date      DATE            NOT NULL,
    accrual_date        DATE            NULL,
    external_id         VARCHAR(100)    NULL,
    related_order_id    BIGINT          NULL REFERENCES orders (id) ON DELETE RESTRICT,
    related_operation_id BIGINT         NULL REFERENCES operations (id) ON DELETE RESTRICT,
    raw_payload_id      BIGINT          NULL REFERENCES raw_kaspi_payloads (id) ON DELETE SET NULL,
    comment             TEXT            NULL,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- production additions (§81)
    account_id          BIGINT          NULL,
    status              VARCHAR(20)     NOT NULL DEFAULT 'POSTED',
    import_batch_id     BIGINT          NULL REFERENCES import_batches (id) ON DELETE SET NULL,
    import_fingerprint  CHAR(64)        NULL,
    created_by          BIGINT          NULL REFERENCES users (id) ON DELETE SET NULL,
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,

    -- §27: amount must never be zero
    CONSTRAINT chk_operations_amount_nonzero CHECK (amount <> 0),
    CONSTRAINT chk_operations_status CHECK (status IN ('POSTED', 'REVERSED', 'CORRECTED', 'DRAFT')),
    CONSTRAINT chk_operations_no_self_reference CHECK (
        related_operation_id IS NULL OR related_operation_id <> id
    ),
    CONSTRAINT fk_operations_account_ip FOREIGN KEY (account_id, ip_id)
        REFERENCES accounts (id, ip_id) ON DELETE RESTRICT
);

COMMENT ON TABLE  operations                      IS 'Central financial ledger. DELETE is forbidden — use reversal/correction (MASTER PROMPT §27, §16).';
COMMENT ON COLUMN operations.amount               IS 'KZT, NUMERIC(15,2), never zero. Sign is validated against operation_types.direction (§27).';
COMMENT ON COLUMN operations.external_id          IS 'Id in the source system. UNIQUE per (ip_id,type_id) when present — makes retries idempotent (§41, ADR-010).';
COMMENT ON COLUMN operations.related_operation_id IS 'Link to the reversed/corrected operation, and pairs TRANSFER_OUT with TRANSFER_IN (§16, §28).';
COMMENT ON COLUMN operations.import_fingerprint   IS 'SHA-256 of the normalised source row — prevents duplicate Excel import (§41).';

-- ---------------------------------------------------------------------
-- 4.11 transfers                                         [ORIGINAL #9, §28]
--
-- §28 — a transfer consists of TRANSFER_OUT + TRANSFER_IN and is
-- neither income nor expense (enforced via operation_types.affects_pnl
-- = FALSE and excluded from P&L views).
-- ---------------------------------------------------------------------

-- ---------------------------------------------------------------------
-- 4.12 settlement_links                    [BOOK_SYNTHESIS_2026-10-03]
--
-- Many-to-many accrual↔payment matching. Stores relationship assertions,
-- not money. Paid/remaining amounts are always DERIVED via
-- SUM(allocated_amount), never stored.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS settlement_links (
    id                  BIGSERIAL       PRIMARY KEY,
    ip_id               BIGINT          NOT NULL REFERENCES ip_entities (id) ON DELETE RESTRICT,
    accrual_operation_id BIGINT         NOT NULL REFERENCES operations (id) ON DELETE RESTRICT,
    payment_operation_id BIGINT         NOT NULL REFERENCES operations (id) ON DELETE RESTRICT,
    allocated_amount    NUMERIC(15,2)   NOT NULL,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by          BIGINT          NULL REFERENCES users (id) ON DELETE SET NULL,

    CONSTRAINT chk_settlement_links_amount_positive CHECK (allocated_amount > 0),
    CONSTRAINT chk_settlement_links_no_self_link CHECK (
        accrual_operation_id <> payment_operation_id
    )
);

COMMENT ON TABLE settlement_links IS
'Many-to-many accrual↔payment matching. Stores relationship assertions, not money. '
'Paid/remaining amounts are always DERIVED via SUM(allocated_amount), never stored. '
'(ACCRUAL_MODEL_DESIGN_2026-10-03)';

CREATE INDEX IF NOT EXISTS ix_settlement_links_accrual
    ON settlement_links (accrual_operation_id);
CREATE INDEX IF NOT EXISTS ix_settlement_links_payment
    ON settlement_links (payment_operation_id);
CREATE INDEX IF NOT EXISTS ix_settlement_links_ip
    ON settlement_links (ip_id);

-- 4.12a settlement_links integrity triggers (0016, DI-001..DI-005)
CREATE OR REPLACE FUNCTION fn_settlement_links_integrity()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_accrual_ip BIGINT;
    v_payment_ip BIGINT;
    v_accrual_status VARCHAR(20);
    v_payment_status VARCHAR(20);
    v_accrual_date DATE;
    v_accrual_amount NUMERIC(15,2);
    v_total_allocated NUMERIC(15,2);
BEGIN
    SELECT status, ip_id, accrual_date, amount
    INTO v_accrual_status, v_accrual_ip, v_accrual_date, v_accrual_amount
    FROM operations WHERE id = NEW.accrual_operation_id
    FOR UPDATE;

    SELECT status, ip_id
    INTO v_payment_status, v_payment_ip
    FROM operations WHERE id = NEW.payment_operation_id;

    IF v_accrual_status IS DISTINCT FROM 'POSTED' THEN
        RAISE EXCEPTION
            'settlement_links: accrual operation % is %, must be POSTED (DI-004)',
            NEW.accrual_operation_id, v_accrual_status
            USING ERRCODE = 'check_violation';
    END IF;

    IF v_payment_status IS DISTINCT FROM 'POSTED' THEN
        RAISE EXCEPTION
            'settlement_links: payment operation % is %, must be POSTED (DI-004)',
            NEW.payment_operation_id, v_payment_status
            USING ERRCODE = 'check_violation';
    END IF;

    IF v_accrual_ip IS DISTINCT FROM NEW.ip_id OR
       v_payment_ip IS DISTINCT FROM NEW.ip_id THEN
        RAISE EXCEPTION
            'settlement_links: cross-IP link blocked (DI-001). '
            'link ip=%, accrual ip=%, payment ip=%',
            NEW.ip_id, v_accrual_ip, v_payment_ip
            USING ERRCODE = 'check_violation';
    END IF;

    IF v_accrual_date IS NULL THEN
        RAISE EXCEPTION
            'settlement_links: accrual operation % has NULL accrual_date, '
            'cannot be an accrual (DI-002)',
            NEW.accrual_operation_id
            USING ERRCODE = 'check_violation';
    END IF;

    SELECT COALESCE(SUM(allocated_amount), 0)
    INTO v_total_allocated
    FROM settlement_links
    WHERE accrual_operation_id = NEW.accrual_operation_id
      AND id IS DISTINCT FROM NEW.id;

    IF v_total_allocated + NEW.allocated_amount > ABS(v_accrual_amount) THEN
        RAISE EXCEPTION
            'settlement_links: over-allocation blocked (DI-003). '
            'accrual %=%, already allocated %, new % would exceed',
            NEW.accrual_operation_id, ABS(v_accrual_amount),
            v_total_allocated, NEW.allocated_amount
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_settlement_links_integrity ON settlement_links;
CREATE TRIGGER trg_settlement_links_integrity
    BEFORE INSERT OR UPDATE ON settlement_links
    FOR EACH ROW EXECUTE FUNCTION fn_settlement_links_integrity();

CREATE OR REPLACE FUNCTION fn_settlement_links_immutable()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_hatch_role TEXT := 'erp_migrator';
BEGIN
    -- Test purge via app.allow_financial_delete is gated on erp_migrator
    -- membership (TD-003), exactly like fn_forbid_delete. A bare GUC check
    -- is insufficient: any role can SET a custom GUC (SEC-HIGH 2026-10-04).
    IF coalesce(current_setting('app.allow_financial_delete', TRUE), 'off') = 'on'
       AND coalesce((
               SELECT true
               FROM pg_roles r
               WHERE r.rolname = v_hatch_role
                 AND pg_has_role(current_user, r.rolname, 'member')
           ), false) THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION
        'settlement_links: rows are immutable (DI-005). '
        'Create a new link for corrections, do not mutate.'
        USING ERRCODE = 'check_violation';
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS trg_settlement_links_immutable ON settlement_links;
CREATE TRIGGER trg_settlement_links_immutable
    BEFORE UPDATE OR DELETE ON settlement_links
    FOR EACH ROW EXECUTE FUNCTION fn_settlement_links_immutable();

-- SECTION 3b — BANK STATEMENTS (G-BANK-2..5, ARCHITECTURAL EXTENSION)
-- =====================================================================
-- Provider → Adapter → Raw Statement → Normalized Bank Transaction → Dedup →
-- Classification → Operation → Reconciliation.
-- Raw source is preserved verbatim (BYTEA); normalized lines are immutable
-- external facts (trigger fn_bank_statement_lines_immutable); the ledger
-- link is only via matched_operation_id. File import only (MT940 text) —
-- no invented bank APIs. Halyk live API deferred; Kaspi Pay / Freedom
-- format-specific adapters wait for real official/sample exports.

CREATE TABLE IF NOT EXISTS raw_bank_statements (
    id                  BIGSERIAL       PRIMARY KEY,
    ip_id               BIGINT          NOT NULL REFERENCES ip_entities (id) ON DELETE RESTRICT,
    provider_code       VARCHAR(20)     NOT NULL,
    account_id          BIGINT          NULL REFERENCES accounts (id) ON DELETE RESTRICT,
    file_name           TEXT            NOT NULL,
    file_hash           CHAR(64)        NOT NULL,
    content             BYTEA           NOT NULL,
    content_text        TEXT            NULL,
    statement_no        VARCHAR(50)     NULL,
    period_from         DATE            NULL,
    period_to           DATE            NULL,
    opening_balance     NUMERIC(15,2)   NULL,
    closing_balance     NUMERIC(15,2)   NULL,
    currency            VARCHAR(3)      NULL,
    import_batch_id     BIGINT          NULL REFERENCES import_batches (id) ON DELETE RESTRICT,
    received_at         TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    processed_at        TIMESTAMPTZ     NULL,
    created_by          BIGINT          NULL REFERENCES users (id) ON DELETE SET NULL,

    CONSTRAINT chk_raw_bank_statements_provider
        CHECK (provider_code IN ('HALYK', 'FREEDOM', 'EPAY', 'KASPI_PAY', 'MANUAL')),
    CONSTRAINT chk_raw_bank_statements_file_hash
        CHECK (file_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT chk_raw_bank_statements_currency
        CHECK (currency IS NULL OR currency ~ '^[A-Z]{3}$'),
    CONSTRAINT chk_raw_bank_statements_period
        CHECK (period_to IS NULL OR period_from IS NULL OR period_to >= period_from),
    CONSTRAINT uq_raw_bank_statements_file
        UNIQUE (ip_id, provider_code, file_hash)
);

CREATE INDEX IF NOT EXISTS ix_raw_bank_statements_ip
    ON raw_bank_statements (ip_id);
CREATE INDEX IF NOT EXISTS ix_raw_bank_statements_batch
    ON raw_bank_statements (import_batch_id);
CREATE INDEX IF NOT EXISTS ix_raw_bank_statements_account
    ON raw_bank_statements (account_id);

COMMENT ON TABLE raw_bank_statements IS
'G-BANK-2: исходный файл банковской выписки целиком (MT940-текст / xlsx-байты). Provenance для всех строк.';
COMMENT ON COLUMN raw_bank_statements.file_hash IS
'SHA-256 содержимого файла. Повторная загрузка того же файла тем же ИП и провайдером отклоняется (uq_raw_bank_statements_file).';
COMMENT ON COLUMN raw_bank_statements.account_id IS
'Счёт ERP, сопоставленный до/во время импорта. NULL = сопоставление отложено; строки при этом тоже ждут (account_id NULL).';

CREATE TABLE IF NOT EXISTS bank_statement_lines (
    id                  BIGSERIAL       PRIMARY KEY,
    ip_id               BIGINT          NOT NULL REFERENCES ip_entities (id) ON DELETE RESTRICT,
    statement_id        BIGINT          NOT NULL REFERENCES raw_bank_statements (id) ON DELETE RESTRICT,
    account_id          BIGINT          NULL REFERENCES accounts (id) ON DELETE RESTRICT,

    provider_code       VARCHAR(20)     NOT NULL,
    provider_account    VARCHAR(64)     NOT NULL,
    value_date          DATE            NOT NULL,
    entry_date          DATE            NULL,
    amount              NUMERIC(15,2)   NOT NULL,
    currency            VARCHAR(3)      NOT NULL DEFAULT 'KZT',
    direction           VARCHAR(3)      NOT NULL,
    document_number     VARCHAR(100)    NULL,
    bank_reference      VARCHAR(200)    NULL,

    counterparty_name   VARCHAR(255)    NULL,
    counterparty_iin    VARCHAR(12)     NULL,
    counterparty_bik    VARCHAR(20)     NULL,
    counterparty_account VARCHAR(64)    NULL,
    payment_purpose     TEXT            NULL,
    knp_code            VARCHAR(10)     NULL,

    balance_after       NUMERIC(15,2)   NULL,
    raw_line            TEXT            NOT NULL,
    line_no             INTEGER         NOT NULL,
    provider_extra      JSONB           NOT NULL DEFAULT '{}',

    line_fingerprint    CHAR(64)        NOT NULL,

    match_status        VARCHAR(20)     NOT NULL DEFAULT 'UNMATCHED',
    matched_operation_id BIGINT        NULL REFERENCES operations (id) ON DELETE RESTRICT,
    matched_at          TIMESTAMPTZ     NULL,
    matched_by          BIGINT          NULL REFERENCES users (id) ON DELETE SET NULL,

    created_at          TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT chk_bank_statement_lines_direction
        CHECK (direction IN ('IN', 'OUT')),
    CONSTRAINT chk_bank_statement_lines_amount_positive
        CHECK (amount > 0),
    CONSTRAINT chk_bank_statement_lines_currency
        CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT chk_bank_statement_lines_iin
        CHECK (counterparty_iin IS NULL OR counterparty_iin ~ '^[0-9]{12}$'),
    CONSTRAINT chk_bank_statement_lines_fingerprint
        CHECK (line_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT chk_bank_statement_lines_provider
        CHECK (provider_code IN ('HALYK', 'FREEDOM', 'EPAY', 'KASPI_PAY', 'MANUAL')),
    CONSTRAINT chk_bank_statement_lines_match_status
        CHECK (match_status IN ('UNMATCHED', 'SUGGESTED', 'CONFIRMED', 'IGNORED')),
    CONSTRAINT chk_bank_statement_lines_match_consistency
        CHECK (
            (match_status IN ('SUGGESTED', 'CONFIRMED') AND matched_operation_id IS NOT NULL)
            OR (match_status IN ('UNMATCHED', 'IGNORED') AND matched_operation_id IS NULL)
        ),
    CONSTRAINT chk_bank_statement_lines_line_no
        CHECK (line_no >= 0),
    CONSTRAINT uq_bank_statement_lines_fingerprint
        UNIQUE (ip_id, line_fingerprint)
);

CREATE INDEX IF NOT EXISTS ix_bank_statement_lines_statement
    ON bank_statement_lines (statement_id);
CREATE INDEX IF NOT EXISTS ix_bank_statement_lines_match
    ON bank_statement_lines (ip_id, match_status);
CREATE INDEX IF NOT EXISTS ix_bank_statement_lines_value_date
    ON bank_statement_lines (ip_id, value_date);
CREATE INDEX IF NOT EXISTS ix_bank_statement_lines_account
    ON bank_statement_lines (account_id);

-- Hard gate (reconciliation framework §1): one operation can never be
-- CONFIRMED-matched to two lines. SUGGESTED rows are excluded — the human
-- picks one candidate and rejects the rest.
CREATE UNIQUE INDEX IF NOT EXISTS uq_bank_statement_lines_confirmed_op
    ON bank_statement_lines (matched_operation_id)
    WHERE match_status = 'CONFIRMED';

COMMENT ON TABLE bank_statement_lines IS
'G-BANK-3: нормализованные строки выписки (staging). Внешний факт; неизменяемы после импорта (триггер). Связь с ledger — только через matched_operation_id.';
COMMENT ON COLUMN bank_statement_lines.line_fingerprint IS
'SHA-256 нормализованного ядра строки. Дедупликация per-IP до ledger.';
COMMENT ON COLUMN bank_statement_lines.provider_extra IS
'Provider-specific поля без нормализации + audit trail (ignore_line пишет reason сюда).';

-- Bank statement lines are external facts: immutable like settlement_links
-- (DI-005), but the reconciliation workflow columns (match_status /
-- matched_operation_id / matched_at / matched_by) must stay mutable — hence
-- the explicit column allow-list instead of fn_forbid_delete(). DELETE is
-- gated on the strong pattern (GUC + erp_migrator membership, TD-003).
CREATE OR REPLACE FUNCTION fn_bank_statement_lines_immutable()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_hatch_role TEXT := 'erp_migrator';
BEGIN
    -- Only the reconciliation workflow may change; statement facts are frozen.
    IF TG_OP = 'UPDATE' THEN
        IF NEW.ip_id IS DISTINCT FROM OLD.ip_id
           OR NEW.statement_id IS DISTINCT FROM OLD.statement_id
           OR NEW.account_id IS DISTINCT FROM OLD.account_id
           OR NEW.provider_code IS DISTINCT FROM OLD.provider_code
           OR NEW.provider_account IS DISTINCT FROM OLD.provider_account
           OR NEW.value_date IS DISTINCT FROM OLD.value_date
           OR NEW.entry_date IS DISTINCT FROM OLD.entry_date
           OR NEW.amount IS DISTINCT FROM OLD.amount
           OR NEW.currency IS DISTINCT FROM OLD.currency
           OR NEW.direction IS DISTINCT FROM OLD.direction
           OR NEW.document_number IS DISTINCT FROM OLD.document_number
           OR NEW.bank_reference IS DISTINCT FROM OLD.bank_reference
           OR NEW.counterparty_name IS DISTINCT FROM OLD.counterparty_name
           OR NEW.counterparty_iin IS DISTINCT FROM OLD.counterparty_iin
           OR NEW.counterparty_bik IS DISTINCT FROM OLD.counterparty_bik
           OR NEW.counterparty_account IS DISTINCT FROM OLD.counterparty_account
           OR NEW.payment_purpose IS DISTINCT FROM OLD.payment_purpose
           OR NEW.knp_code IS DISTINCT FROM OLD.knp_code
           OR NEW.balance_after IS DISTINCT FROM OLD.balance_after
           OR NEW.raw_line IS DISTINCT FROM OLD.raw_line
           OR NEW.line_no IS DISTINCT FROM OLD.line_no
           -- provider_extra: fact keys are frozen, but the 'audit' sub-key
           -- is the reconciliation workflow's append-only trail (verdicts,
           -- ignore/reopen reasons). Only changes outside 'audit' are blocked.
           OR (NEW.provider_extra - 'audit') IS DISTINCT FROM (OLD.provider_extra - 'audit')
           OR NEW.line_fingerprint IS DISTINCT FROM OLD.line_fingerprint
           OR NEW.created_at IS DISTINCT FROM OLD.created_at
        THEN
            RAISE EXCEPTION
                'bank_statement_lines: statement facts are immutable. '
                'Only match_status/matched_operation_id/matched_at/matched_by '
                'and the provider_extra.audit trail may change via the '
                'reconciliation workflow.'
                USING ERRCODE = 'check_violation';
        END IF;
        -- match_status/matched_operation_id/matched_at/matched_by + audit — разрешены
        RETURN NEW;
    END IF;
    -- DELETE: only via the §94 test purge (GUC + erp_migrator membership).
    IF coalesce(current_setting('app.allow_financial_delete', TRUE), 'off') = 'on'
       AND coalesce((
               SELECT true FROM pg_roles r
               WHERE r.rolname = v_hatch_role
                 AND pg_has_role(current_user, r.rolname, 'member')
           ), false) THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION
        'bank_statement_lines: DELETE forbidden (§16).'
        USING ERRCODE = 'restrict_violation';
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS trg_bank_statement_lines_immutable ON bank_statement_lines;
CREATE TRIGGER trg_bank_statement_lines_immutable
    BEFORE UPDATE OR DELETE ON bank_statement_lines
    FOR EACH ROW EXECUTE FUNCTION fn_bank_statement_lines_immutable();

-- KNP codes that identify bank commissions. Created EMPTY on purpose (§94):
-- commission KNP codes are facts from real statements, never invented.
CREATE TABLE IF NOT EXISTS bank_fee_knp_codes (
    knp_code    VARCHAR(10)     PRIMARY KEY,
    description TEXT            NULL,
    created_at  TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT chk_bank_fee_knp_codes_code
        CHECK (knp_code ~ '^[0-9]{1,10}$')
);

COMMENT ON TABLE bank_fee_knp_codes IS
'G-BANK-4: справочник КНП банковских комиссий. Наполняется только по реальным выпискам (§94 — коды не выдумываются).';

-- =====================================================================
CREATE TABLE IF NOT EXISTS transfers (
    id                    BIGSERIAL     PRIMARY KEY,
    from_ip_id            BIGINT        NOT NULL REFERENCES ip_entities (id) ON DELETE RESTRICT,
    to_ip_id              BIGINT        NOT NULL REFERENCES ip_entities (id) ON DELETE RESTRICT,
    from_account_id       BIGINT        NULL,
    to_account_id         BIGINT        NULL,
    amount                NUMERIC(15,2) NOT NULL,
    transfer_date         DATE          NOT NULL,
    reason                TEXT          NULL,
    outbound_operation_id BIGINT        NULL REFERENCES operations (id) ON DELETE RESTRICT,
    inbound_operation_id  BIGINT        NULL REFERENCES operations (id) ON DELETE RESTRICT,
    idempotency_key       VARCHAR(255)  NULL,
    -- production additions (§81)
    status                VARCHAR(20)   NOT NULL DEFAULT 'POSTED',
    created_by            BIGINT        NULL REFERENCES users (id) ON DELETE SET NULL,
    created_at            TIMESTAMPTZ   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at            TIMESTAMPTZ   NOT NULL DEFAULT CURRENT_TIMESTAMP,

    -- §28: from_ip_id <> to_ip_id
    CONSTRAINT chk_transfers_different_ip CHECK (from_ip_id <> to_ip_id),
    CONSTRAINT chk_transfers_amount       CHECK (amount > 0),
    CONSTRAINT chk_transfers_status       CHECK (status IN ('POSTED', 'REVERSED', 'DRAFT')),
    CONSTRAINT chk_transfers_posted_has_legs CHECK (
        status <> 'POSTED' OR (
            outbound_operation_id IS NOT NULL AND inbound_operation_id IS NOT NULL
        )
    ),
    CONSTRAINT chk_transfers_accounts_distinct CHECK (
        from_account_id IS NULL OR to_account_id IS NULL OR from_account_id <> to_account_id
    ),
    CONSTRAINT chk_transfers_keyed_pair CHECK (
        idempotency_key IS NULL OR (
            length(btrim(idempotency_key)) > 0
            AND from_account_id IS NOT NULL AND to_account_id IS NOT NULL
            AND created_by IS NOT NULL
            AND outbound_operation_id IS NOT NULL AND inbound_operation_id IS NOT NULL
        )
    ),
    CONSTRAINT chk_transfers_operations_distinct CHECK (
        outbound_operation_id IS NULL OR inbound_operation_id IS NULL
        OR outbound_operation_id <> inbound_operation_id
    ),
    CONSTRAINT fk_transfers_from_account_ip FOREIGN KEY (from_account_id, from_ip_id)
        REFERENCES accounts (id, ip_id) ON DELETE RESTRICT,
    CONSTRAINT fk_transfers_to_account_ip FOREIGN KEY (to_account_id, to_ip_id)
        REFERENCES accounts (id, ip_id) ON DELETE RESTRICT,
    CONSTRAINT uq_transfers_outbound UNIQUE (outbound_operation_id),
    CONSTRAINT uq_transfers_inbound  UNIQUE (inbound_operation_id)
);

COMMENT ON TABLE  transfers        IS 'Transfers between ИП: TRANSFER_OUT + TRANSFER_IN. Never income or expense (MASTER PROMPT §28, §76).';
COMMENT ON COLUMN transfers.amount IS 'Always positive; the direction is expressed by the two linked operations (§28).';

-- ---------------------------------------------------------------------
-- 4.12 rates_history                                    [ORIGINAL #13, §31]
--
-- §31 — no overlapping rate periods for the same work operation type
-- (enforced by an EXCLUDE constraint, not by application code).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rates_history (
    id                      BIGSERIAL       PRIMARY KEY,
    work_operation_type_id  BIGINT          NOT NULL REFERENCES work_operation_types (id) ON DELETE RESTRICT,
    rate                    NUMERIC(15,2)   NOT NULL,
    effective_from          TIMESTAMPTZ     NOT NULL,
    effective_to            TIMESTAMPTZ     NULL,
    -- production additions (§81)
    created_by              BIGINT          NULL REFERENCES users (id) ON DELETE SET NULL,
    comment                 TEXT            NULL,
    created_at              TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at              TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT chk_rates_history_rate   CHECK (rate >= 0),
    CONSTRAINT chk_rates_history_period CHECK (effective_to IS NULL OR effective_to > effective_from),
    -- §31: rate periods of one work operation type must not intersect
    CONSTRAINT excl_rates_history_overlap EXCLUDE USING gist (
        work_operation_type_id WITH =,
        tstzrange(effective_from, effective_to, '[)') WITH &&
    )
);

COMMENT ON TABLE  rates_history                IS 'Piecework rate history; periods of one type never overlap (MASTER PROMPT §31).';
COMMENT ON COLUMN rates_history.effective_to   IS 'NULL means the rate is currently in force. Half-open interval [from, to).';

-- ---------------------------------------------------------------------
-- 4.13 work_entries                                     [ORIGINAL #14, §33]
--
-- §32 — rate_at_time is frozen by trigger on INSERT and immutable.
-- §33 — total_sum is GENERATED ALWAYS AS (quantity * rate_at_time) STORED.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS work_entries (
    id                      BIGSERIAL       PRIMARY KEY,
    employee_id             BIGINT          NOT NULL REFERENCES employees (id) ON DELETE RESTRICT,
    work_operation_type_id  BIGINT          NOT NULL REFERENCES work_operation_types (id) ON DELETE RESTRICT,
    order_id                BIGINT          NULL REFERENCES orders (id) ON DELETE RESTRICT,
    ip_id                   BIGINT          NOT NULL REFERENCES ip_entities (id) ON DELETE RESTRICT,
    entry_date              DATE            NOT NULL,
    quantity                NUMERIC(15,3)   NOT NULL,
    rate_at_time            NUMERIC(15,2)   NOT NULL,
    total_sum               NUMERIC(15,2)   GENERATED ALWAYS AS (quantity * rate_at_time) STORED,
    created_by_user         BIGINT          NULL REFERENCES users (id) ON DELETE SET NULL,
    status                  VARCHAR(20)     NOT NULL DEFAULT 'DRAFT',
    created_at              TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- production additions (§81)
    payroll_period_id       BIGINT          NULL REFERENCES payroll_periods (id) ON DELETE RESTRICT,
    comment                 TEXT            NULL,
    import_batch_id         BIGINT          NULL REFERENCES import_batches (id) ON DELETE SET NULL,
    import_fingerprint      CHAR(64)        NULL,
    updated_at              TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT chk_work_entries_quantity CHECK (quantity > 0),
    CONSTRAINT chk_work_entries_rate     CHECK (rate_at_time >= 0),
    CONSTRAINT chk_work_entries_status   CHECK (status IN ('DRAFT', 'CONFIRMED', 'PAID')),
    -- a PAID entry must belong to a payroll period
    CONSTRAINT chk_work_entries_paid_period CHECK (status <> 'PAID' OR payroll_period_id IS NOT NULL)
);

COMMENT ON TABLE  work_entries              IS 'Piecework entries — the labour part of direct cost (MASTER PROMPT §33, §37).';
COMMENT ON COLUMN work_entries.rate_at_time IS 'FROZEN rate copied from rates_history on INSERT; immutable afterwards (§32, ADR-008).';
COMMENT ON COLUMN work_entries.total_sum    IS 'GENERATED ALWAYS AS (quantity * rate_at_time) STORED (§33).';
COMMENT ON COLUMN work_entries.status       IS 'DRAFT | CONFIRMED | PAID (§33).';

-- ---------------------------------------------------------------------
-- 4.14 payroll_summary                                  [ORIGINAL #16, §35]
--
-- §35 — total_calculated = piecework_total + base_salary + bonuses − deductions.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payroll_summary (
    id                 BIGSERIAL       PRIMARY KEY,
    employee_id        BIGINT          NOT NULL REFERENCES employees (id) ON DELETE RESTRICT,
    payroll_period_id  BIGINT          NOT NULL REFERENCES payroll_periods (id) ON DELETE RESTRICT,
    piecework_total    NUMERIC(15,2)   NOT NULL DEFAULT 0,
    base_salary        NUMERIC(15,2)   NOT NULL DEFAULT 0,
    bonuses            NUMERIC(15,2)   NOT NULL DEFAULT 0,
    deductions         NUMERIC(15,2)   NOT NULL DEFAULT 0,
    total_calculated   NUMERIC(15,2)   GENERATED ALWAYS AS (
                           piecework_total + base_salary + bonuses - deductions
                       ) STORED,
    created_at         TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- production additions (§81)
    ip_id              BIGINT          NULL REFERENCES ip_entities (id) ON DELETE RESTRICT,
    status             VARCHAR(20)     NOT NULL DEFAULT 'CALCULATED',
    calculated_at      TIMESTAMPTZ     NULL,
    updated_at         TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT chk_payroll_summary_piecework  CHECK (piecework_total >= 0),
    CONSTRAINT chk_payroll_summary_base       CHECK (base_salary >= 0),
    CONSTRAINT chk_payroll_summary_bonuses    CHECK (bonuses >= 0),
    CONSTRAINT chk_payroll_summary_deductions CHECK (deductions >= 0),
    CONSTRAINT chk_payroll_summary_status     CHECK (status IN ('CALCULATED', 'APPROVED', 'PAID', 'CANCELLED')),
    -- one summary per employee per period — prevents double payroll (§57)
    CONSTRAINT uq_payroll_summary_employee_period UNIQUE (employee_id, payroll_period_id)
);

COMMENT ON TABLE  payroll_summary                  IS 'Payroll calculation per employee per period (MASTER PROMPT §35).';
COMMENT ON COLUMN payroll_summary.total_calculated IS 'GENERATED: piecework_total + base_salary + bonuses - deductions (§35).';

-- ---------------------------------------------------------------------
-- 4.15 payroll_payments                                 [ORIGINAL #17, §36]
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payroll_payments (
    id                  BIGSERIAL       PRIMARY KEY,
    payroll_summary_id  BIGINT          NOT NULL REFERENCES payroll_summary (id) ON DELETE RESTRICT,
    payment_date        DATE            NOT NULL,
    amount              NUMERIC(15,2)   NOT NULL,
    payment_method      VARCHAR(20)     NOT NULL,
    operation_id        BIGINT          NULL REFERENCES operations (id) ON DELETE RESTRICT,
    -- production additions (§81)
    status              VARCHAR(20)     NOT NULL DEFAULT 'POSTED',
    comment             TEXT            NULL,
    created_by          BIGINT          NULL REFERENCES users (id) ON DELETE SET NULL,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT chk_payroll_payments_method CHECK (payment_method IN ('KASPI_TRANSFER', 'CASH')),
    CONSTRAINT chk_payroll_payments_amount CHECK (amount > 0),
    CONSTRAINT chk_payroll_payments_status CHECK (status IN ('POSTED', 'REVERSED')),
    -- §57: one ledger operation can back only one salary payment — blocks double payment
    CONSTRAINT uq_payroll_payments_operation UNIQUE (operation_id)
);

COMMENT ON TABLE  payroll_payments                IS 'Salary payments linked to the financial ledger (MASTER PROMPT §36).';
COMMENT ON COLUMN payroll_payments.payment_method IS 'KASPI_TRANSFER | CASH (§36).';

-- =====================================================================
-- SECTION 3c — STOCK LEDGER (P2-INV-001..004, ARCHITECTURAL EXTENSION)
-- =====================================================================
-- Quantity-only inventory ledger. Immutable INSERT-only rows; the database
-- is the enforcement layer (negative-stock gate, DI-001 cross-IP check,
-- DI-005 immutability). No unit_cost / no valuation (owner-blocked):
-- frozen historical cost lives on order_materials.price_at_time (§38).
-- Reservations are intentions, NOT ledger rows — they live in
-- material_reservations so SUM(quantity_delta) always equals physical
-- on-hand. WIP is derived by views, never stored.

CREATE TABLE IF NOT EXISTS stock_movements (
    id              BIGSERIAL       PRIMARY KEY,
    ip_id           BIGINT          NOT NULL REFERENCES ip_entities (id) ON DELETE RESTRICT,
    material_id     BIGINT          NOT NULL REFERENCES materials (id) ON DELETE RESTRICT,
    order_id        BIGINT          NULL     REFERENCES orders (id) ON DELETE CASCADE,
    counterparty_id BIGINT          NULL     REFERENCES counterparties (id) ON DELETE SET NULL,
    movement_date   DATE            NOT NULL,
    quantity_delta  NUMERIC(15,3)   NOT NULL,
    movement_type   VARCHAR(30)     NOT NULL,
    ref_type        VARCHAR(30)     NULL,
    ref_id          BIGINT          NULL,
    reason          TEXT            NULL,
    created_by      BIGINT          NULL REFERENCES users (id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT chk_stock_movements_qty_nonzero CHECK (quantity_delta <> 0),
    CONSTRAINT chk_stock_movements_type CHECK (movement_type IN (
        'RECEIPT',
        'ISSUE',
        'PRODUCTION_CONSUMPTION',
        'PRODUCTION_OUTPUT',
        'RETURN',
        'ADJUSTMENT',
        'SCRAP'
    )),
    CONSTRAINT chk_stock_movements_sign CHECK (
        (movement_type IN ('RECEIPT', 'PRODUCTION_OUTPUT') AND quantity_delta > 0)
        OR (movement_type IN ('ISSUE', 'PRODUCTION_CONSUMPTION', 'RETURN', 'SCRAP')
            AND quantity_delta < 0)
        OR (movement_type = 'ADJUSTMENT')
    ),
    CONSTRAINT chk_stock_movements_reason_required CHECK (
        movement_type NOT IN ('ADJUSTMENT', 'SCRAP')
        OR (reason IS NOT NULL AND length(btrim(reason)) > 0)
    ),
    CONSTRAINT chk_stock_movements_order_scope CHECK (
        (movement_type NOT IN ('PRODUCTION_CONSUMPTION', 'PRODUCTION_OUTPUT'))
        OR order_id IS NOT NULL
    ),
    CONSTRAINT chk_stock_movements_return_counterparty CHECK (
        movement_type <> 'RETURN' OR counterparty_id IS NOT NULL
    )
);

-- Provenance / idempotency: one movement per source row. Partial index so
-- rows without provenance (manual adjustments) are unaffected.
CREATE UNIQUE INDEX IF NOT EXISTS uq_stock_movements_ref
    ON stock_movements (ref_type, ref_id)
    WHERE ref_type IS NOT NULL AND ref_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_stock_movements_ip_material
    ON stock_movements (ip_id, material_id, movement_date);
CREATE INDEX IF NOT EXISTS ix_stock_movements_order
    ON stock_movements (order_id) WHERE order_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS material_reservations (
    id               BIGSERIAL       PRIMARY KEY,
    ip_id            BIGINT          NOT NULL REFERENCES ip_entities (id) ON DELETE RESTRICT,
    order_id         BIGINT          NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
    material_id      BIGINT          NOT NULL REFERENCES materials (id) ON DELETE RESTRICT,
    quantity         NUMERIC(15,3)   NOT NULL,
    status           VARCHAR(20)     NOT NULL DEFAULT 'ACTIVE',
    reserved_at      TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at       TIMESTAMPTZ     NULL,
    released_at      TIMESTAMPTZ     NULL,
    released_reason  VARCHAR(50)     NULL,
    created_by       BIGINT          NULL REFERENCES users (id) ON DELETE SET NULL,

    CONSTRAINT chk_reservations_qty CHECK (quantity > 0),
    CONSTRAINT chk_reservations_status CHECK (status IN ('ACTIVE', 'RELEASED', 'CONSUMED')),
    CONSTRAINT chk_reservations_release CHECK (
        (status = 'ACTIVE' AND released_at IS NULL)
        OR (status <> 'ACTIVE' AND released_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS ix_reservations_ip_material
    ON material_reservations (ip_id, material_id) WHERE status = 'ACTIVE';
CREATE INDEX IF NOT EXISTS ix_reservations_order
    ON material_reservations (order_id) WHERE status = 'ACTIVE';

COMMENT ON TABLE stock_movements IS 'P2-INV-001: immutable quantity ledger (INSERT-only). on_hand = SUM(quantity_delta) per (ip_id, material_id).';
COMMENT ON TABLE material_reservations IS 'P2-INV-003: stock reservations (intentions, not ledger rows). Expiry is read-time via v_material_availability.';

-- ---------------------------------------------------------------------
-- 3c.1 fn_stock_movements_integrity()
--
-- Serializes check-then-insert per material (FOR UPDATE on the catalog
-- row), enforces DI-001 (movement IP == order IP), and blocks movements
-- that would drive on-hand negative. The single enforcement point for
-- API, backfill and future importers.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_stock_movements_integrity()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_order_ip   BIGINT;
    v_on_hand    NUMERIC(15,3);
BEGIN
    -- Concurrency: serialize per material. materials is a global catalog,
    -- so same-material movements across IPs serialize; different materials
    -- never block each other (§6).
    PERFORM 1 FROM materials WHERE id = NEW.material_id FOR UPDATE;

    -- DI-001: movement IP must equal the order's IP.
    IF NEW.order_id IS NOT NULL THEN
        SELECT ip_id INTO v_order_ip FROM orders WHERE id = NEW.order_id;
        IF v_order_ip IS DISTINCT FROM NEW.ip_id THEN
            RAISE EXCEPTION
                'stock_movements: cross-IP movement blocked (DI-001). '
                'movement ip=%, order % ip=%',
                NEW.ip_id, NEW.order_id, v_order_ip
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    -- Negative-stock gate, default BLOCK (owner may flip to warn-only;
    -- the policy is this one branch, not a schema change).
    IF NEW.quantity_delta < 0 THEN
        SELECT COALESCE(SUM(quantity_delta), 0) INTO v_on_hand
        FROM stock_movements
        WHERE ip_id = NEW.ip_id AND material_id = NEW.material_id;
        IF v_on_hand + NEW.quantity_delta < 0 THEN
            RAISE EXCEPTION
                'stock_movements: insufficient stock for material % (ip=%). '
                'on_hand=%, requested=%',
                NEW.material_id, NEW.ip_id, v_on_hand, NEW.quantity_delta
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_stock_movements_integrity ON stock_movements;
CREATE TRIGGER trg_stock_movements_integrity
    BEFORE INSERT ON stock_movements
    FOR EACH ROW EXECUTE FUNCTION fn_stock_movements_integrity();

-- ---------------------------------------------------------------------
-- 3c.2 fn_stock_movements_immutable() — DI-005 STRONG pattern.
--
-- Same guard as fn_settlement_links_immutable (SEC-HIGH 2026-10-04):
-- the GUC escape requires erp_migrator membership, because any role can
-- SET a custom GUC. NEVER the GUC-only weak pattern.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_stock_movements_immutable()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_hatch_role TEXT := 'erp_migrator';
BEGIN
    -- Test purge via app.allow_financial_delete is gated on erp_migrator
    -- membership (TD-003), exactly like fn_forbid_delete. A bare GUC check
    -- is insufficient: any role can SET a custom GUC (SEC-HIGH 2026-10-04).
    IF coalesce(current_setting('app.allow_financial_delete', TRUE), 'off') = 'on'
       AND coalesce((
               SELECT true
               FROM pg_roles r
               WHERE r.rolname = v_hatch_role
                 AND pg_has_role(current_user, r.rolname, 'member')
           ), false) THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION
        'stock_movements: rows are immutable (DI-005 pattern). '
        'Post a compensating movement (§5), do not mutate.'
        USING ERRCODE = 'check_violation';
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS trg_stock_movements_immutable ON stock_movements;
CREATE TRIGGER trg_stock_movements_immutable
    BEFORE UPDATE OR DELETE ON stock_movements
    FOR EACH ROW EXECUTE FUNCTION fn_stock_movements_immutable();

-- ---------------------------------------------------------------------
-- 3c.3 fn_reservations_ip() — DI-001 for reservations.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_reservations_ip()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_order_ip BIGINT;
BEGIN
    SELECT ip_id INTO v_order_ip FROM orders WHERE id = NEW.order_id;
    IF v_order_ip IS DISTINCT FROM NEW.ip_id THEN
        RAISE EXCEPTION
            'material_reservations: cross-IP reservation blocked (DI-001). '
            'reservation ip=%, order % ip=%',
            NEW.ip_id, NEW.order_id, v_order_ip
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_reservations_ip ON material_reservations;
CREATE TRIGGER trg_reservations_ip
    BEFORE INSERT OR UPDATE OF ip_id, order_id ON material_reservations
    FOR EACH ROW EXECUTE FUNCTION fn_reservations_ip();

COMMENT ON FUNCTION fn_stock_movements_integrity() IS 'P2-INV-001: serializes per-material inserts, DI-001 cross-IP check, negative-stock gate.';
COMMENT ON FUNCTION fn_stock_movements_immutable() IS 'P2-INV-001: DI-005 STRONG immutability (GUC + erp_migrator membership).';
COMMENT ON FUNCTION fn_reservations_ip() IS 'P2-INV-003: DI-001 — reservation IP must equal the order IP.';

-- ---------------------------------------------------------------------
-- 3d. production_output     [ARCHITECTURAL EXTENSION, G-C2]
--
-- Quantity-only link between production and finished goods: one row per
-- production event (order_id NULL = make-to-stock, production without a
-- sales order).
--
-- NO unit_cost, NO valuation (O-C1 owner-blocked) — quantity only.
-- Rows are IMMUTABLE (DI-005 STRONG pattern): corrections are
-- compensating rows, never UPDATEs.
-- DI-001: when order_id is set, output IP must equal the order's IP.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS production_output (
    id              BIGSERIAL       PRIMARY KEY,
    ip_id           BIGINT          NOT NULL REFERENCES ip_entities (id) ON DELETE RESTRICT,
    order_id        BIGINT          NULL REFERENCES orders (id) ON DELETE CASCADE,
    product_id      BIGINT          NOT NULL REFERENCES products (id) ON DELETE RESTRICT,
    quantity        NUMERIC(15,3)   NOT NULL,
    produced_at     TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by      BIGINT          NULL REFERENCES users (id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reason          TEXT            NULL,

    CONSTRAINT chk_production_output_qty CHECK (quantity > 0)
);

CREATE INDEX IF NOT EXISTS ix_production_output_ip_product
    ON production_output (ip_id, product_id);
CREATE INDEX IF NOT EXISTS ix_production_output_order
    ON production_output (order_id)
    WHERE order_id IS NOT NULL;

COMMENT ON TABLE production_output IS
'G-C2: quantity-only production output records. order_id NULL = make-to-stock. '
'Rows immutable (DI-005 STRONG); no unit_cost, no valuation (O-C1 blocked).';

-- 3d.1 fn_production_output_immutable() — DI-005 STRONG pattern.
--
-- Copied from fn_stock_movements_immutable (SEC-HIGH 2026-10-04):
-- the GUC escape requires erp_migrator membership, because any role can
-- SET a custom GUC. NEVER the GUC-only weak pattern.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_production_output_immutable()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_hatch_role TEXT := 'erp_migrator';
BEGIN
    -- Test purge via app.allow_financial_delete is gated on erp_migrator
    -- membership (TD-003), exactly like fn_forbid_delete. A bare GUC check
    -- is insufficient: any role can SET a custom GUC (SEC-HIGH 2026-10-04).
    IF coalesce(current_setting('app.allow_financial_delete', TRUE), 'off') = 'on'
       AND coalesce((
               SELECT true
               FROM pg_roles r
               WHERE r.rolname = v_hatch_role
                 AND pg_has_role(current_user, r.rolname, 'member')
           ), false) THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION
        'production_output: rows are immutable (DI-005 pattern). '
        'Post a compensating row instead of UPDATE/DELETE.'
        USING ERRCODE = 'restrict_violation';
    RETURN OLD;
END;
$$;

DROP TRIGGER IF EXISTS trg_production_output_immutable ON production_output;
CREATE TRIGGER trg_production_output_immutable
    BEFORE UPDATE OR DELETE ON production_output
    FOR EACH ROW EXECUTE FUNCTION fn_production_output_immutable();

-- 3d.2 fn_production_output_ip() — DI-001 for production output.
--
-- Same pattern as fn_reservations_ip(): when order_id is set, the output
-- IP must equal the order's IP. NULL order_id (make-to-stock) skips
-- the check — the IP then comes from the request (API: ensure_ip_access).
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_production_output_ip()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_order_ip BIGINT;
BEGIN
    IF NEW.order_id IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT ip_id INTO v_order_ip FROM orders WHERE id = NEW.order_id;
    IF v_order_ip IS DISTINCT FROM NEW.ip_id THEN
        RAISE EXCEPTION
            'production_output: cross-IP output blocked (DI-001). '
            'output ip=%, order % ip=%',
            NEW.ip_id, NEW.order_id, v_order_ip
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_production_output_ip ON production_output;
CREATE TRIGGER trg_production_output_ip
    BEFORE INSERT OR UPDATE OF ip_id, order_id ON production_output
    FOR EACH ROW EXECUTE FUNCTION fn_production_output_ip();

COMMENT ON FUNCTION fn_production_output_immutable() IS 'G-C2: DI-005 STRONG immutability (GUC + erp_migrator membership).';
COMMENT ON FUNCTION fn_production_output_ip() IS 'G-C2: DI-001 — output IP must equal the order IP when order_id is set.';

-- =====================================================================
-- SECTION 5 — DEFERRED CONSTRAINTS (circular references)
--
-- These FKs cannot be declared inline because the referenced table is
-- created later in the file (orders ↔ raw_kaspi_payloads,
-- raw_kaspi_payloads ↔ sync_log, kaspi_payouts ↔ operations).
-- Added idempotently (§87).
-- =====================================================================

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_orders_raw_payload'
    ) THEN
        ALTER TABLE orders
            ADD CONSTRAINT fk_orders_raw_payload
            FOREIGN KEY (raw_payload_id) REFERENCES raw_kaspi_payloads (id) ON DELETE SET NULL;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_raw_kaspi_payloads_sync_log'
    ) THEN
        ALTER TABLE raw_kaspi_payloads
            ADD CONSTRAINT fk_raw_kaspi_payloads_sync_log
            FOREIGN KEY (sync_log_id) REFERENCES sync_log (id) ON DELETE SET NULL;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_kaspi_payouts_operation'
    ) THEN
        ALTER TABLE kaspi_payouts
            ADD CONSTRAINT fk_kaspi_payouts_operation
            FOREIGN KEY (operation_id) REFERENCES operations (id) ON DELETE RESTRICT;
    END IF;
END;
$$;

-- =====================================================================
-- SECTION 6 — INDEXES
--
-- 6.1 MANDATORY INDEXES (§64) — exact names required by MASTER PROMPT
-- 6.2 FOREIGN KEY / QUERY PATTERN INDEXES (§64 last paragraph, §74)
-- 6.3 UNIQUENESS-ENFORCING PARTIAL INDEXES (§41, §57)
-- =====================================================================

-- ---------------------------------------------------------------------
-- 6.1 Mandatory indexes (§64)
-- ---------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_operations_analytics
    ON operations (ip_id, operation_date, type_id);

CREATE INDEX IF NOT EXISTS idx_work_entries_emp
    ON work_entries (employee_id, entry_date, status);

CREATE INDEX IF NOT EXISTS idx_orders_status
    ON orders (ip_id, status, created_at);

CREATE INDEX IF NOT EXISTS idx_rates_lookup
    ON rates_history (work_operation_type_id, effective_from, effective_to);

-- ---------------------------------------------------------------------
-- 6.2 Additional indexes required by real query patterns (§64, §74)
-- ---------------------------------------------------------------------

-- operations: ledger lookups
CREATE INDEX IF NOT EXISTS idx_operations_category        ON operations (category_id);
CREATE INDEX IF NOT EXISTS idx_operations_counterparty    ON operations (counterparty_id);
CREATE INDEX IF NOT EXISTS idx_operations_related_order   ON operations (related_order_id);
CREATE INDEX IF NOT EXISTS idx_operations_related_op      ON operations (related_operation_id);
CREATE INDEX IF NOT EXISTS idx_operations_raw_payload     ON operations (raw_payload_id);
CREATE INDEX IF NOT EXISTS idx_operations_account         ON operations (account_id);
CREATE INDEX IF NOT EXISTS idx_operations_import_batch    ON operations (import_batch_id);
CREATE INDEX IF NOT EXISTS idx_operations_date            ON operations (operation_date);
CREATE INDEX IF NOT EXISTS idx_operations_status          ON operations (status) WHERE status <> 'POSTED';

-- orders
CREATE INDEX IF NOT EXISTS idx_orders_order_date          ON orders (order_date);
CREATE INDEX IF NOT EXISTS idx_orders_source              ON orders (source, status);
CREATE INDEX IF NOT EXISTS idx_orders_customer_phone      ON orders (customer_phone) WHERE customer_phone IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_orders_raw_payload         ON orders (raw_payload_id);

-- order composition
CREATE INDEX IF NOT EXISTS idx_order_items_order          ON order_items (order_id);
CREATE INDEX IF NOT EXISTS idx_order_items_product        ON order_items (product_id);
CREATE INDEX IF NOT EXISTS idx_order_materials_order      ON order_materials (order_id);
CREATE INDEX IF NOT EXISTS idx_order_materials_material   ON order_materials (material_id);
CREATE INDEX IF NOT EXISTS idx_order_status_history_order ON order_status_history (order_id, changed_at);

-- work entries: production analytics and payroll aggregation
CREATE INDEX IF NOT EXISTS idx_work_entries_order         ON work_entries (order_id);
CREATE INDEX IF NOT EXISTS idx_work_entries_type          ON work_entries (work_operation_type_id, entry_date);
CREATE INDEX IF NOT EXISTS idx_work_entries_ip_date       ON work_entries (ip_id, entry_date);
CREATE INDEX IF NOT EXISTS idx_work_entries_period        ON work_entries (payroll_period_id) WHERE payroll_period_id IS NOT NULL;

-- payroll
CREATE INDEX IF NOT EXISTS idx_payroll_summary_period     ON payroll_summary (payroll_period_id, status);
CREATE INDEX IF NOT EXISTS idx_payroll_summary_employee   ON payroll_summary (employee_id);
CREATE INDEX IF NOT EXISTS idx_payroll_payments_summary   ON payroll_payments (payroll_summary_id);
CREATE INDEX IF NOT EXISTS idx_payroll_payments_date      ON payroll_payments (payment_date);

-- transfers
CREATE INDEX IF NOT EXISTS idx_transfers_from             ON transfers (from_ip_id, transfer_date);
CREATE INDEX IF NOT EXISTS idx_transfers_to               ON transfers (to_ip_id, transfer_date);
CREATE UNIQUE INDEX IF NOT EXISTS uq_transfers_idempotency_key
    ON transfers (idempotency_key);

-- kaspi integration
CREATE INDEX IF NOT EXISTS idx_kaspi_credentials_ip       ON kaspi_api_credentials (ip_id, api_type, status);
CREATE INDEX IF NOT EXISTS idx_raw_payloads_ip_received   ON raw_kaspi_payloads (ip_id, received_at);
CREATE INDEX IF NOT EXISTS idx_raw_payloads_external      ON raw_kaspi_payloads (external_id) WHERE external_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_raw_payloads_unprocessed   ON raw_kaspi_payloads (received_at) WHERE processed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_raw_payloads_payload_gin   ON raw_kaspi_payloads USING gin (payload);
CREATE INDEX IF NOT EXISTS idx_sync_log_ip_started        ON sync_log (ip_id, api_type, sync_started_at);
CREATE INDEX IF NOT EXISTS idx_kaspi_payouts_ip_date      ON kaspi_payouts (ip_id, payout_date);
CREATE INDEX IF NOT EXISTS idx_kaspi_payouts_status       ON kaspi_payouts (status) WHERE status <> 'RECONCILED';
CREATE INDEX IF NOT EXISTS idx_kaspi_payout_items_payout  ON kaspi_payout_items (payout_id);
CREATE INDEX IF NOT EXISTS idx_kaspi_payout_items_order   ON kaspi_payout_items (order_id);
CREATE INDEX IF NOT EXISTS idx_kaspi_payout_items_match   ON kaspi_payout_items (match_status) WHERE match_status <> 'MATCHED';
CREATE INDEX IF NOT EXISTS idx_kaspi_payout_items_ext_ord ON kaspi_payout_items (external_order_id) WHERE external_order_id IS NOT NULL;

-- catalogues
CREATE INDEX IF NOT EXISTS idx_materials_supplier         ON materials (supplier_id);
CREATE INDEX IF NOT EXISTS idx_materials_active           ON materials (is_active) WHERE is_active;
CREATE INDEX IF NOT EXISTS idx_products_active            ON products (is_active) WHERE is_active;
CREATE INDEX IF NOT EXISTS idx_employees_ip_status        ON employees (assigned_ip_id, status);
CREATE INDEX IF NOT EXISTS idx_accounts_ip                ON accounts (ip_id, type);

-- security / audit
CREATE INDEX IF NOT EXISTS idx_audit_log_entity           ON audit_log (entity, entity_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_log_user             ON audit_log (user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_log_created          ON audit_log (created_at);
CREATE INDEX IF NOT EXISTS idx_users_employee             ON users (employee_id);
CREATE INDEX IF NOT EXISTS idx_user_roles_role            ON user_roles (role_id);

-- etl
CREATE INDEX IF NOT EXISTS idx_import_batches_started     ON import_batches (started_at);
CREATE INDEX IF NOT EXISTS idx_import_batches_source_hash ON import_batches (source_hash) WHERE source_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tax_rates_lookup           ON tax_rates_history (code, effective_from, effective_to);
-- P2-2 (2026-10-03): FK columns on financial paths were missing indexes
-- (slow JOINs, slow RESTRICT checks)
CREATE INDEX IF NOT EXISTS idx_kaspi_payouts_operation    ON kaspi_payouts (operation_id) WHERE operation_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_kaspi_payouts_raw_payload  ON kaspi_payouts (raw_payload_id) WHERE raw_payload_id IS NOT NULL;
-- idx_payroll_payments_operation dropped (migration 0019): redundant, covered by uq_payroll_payments_operation.
CREATE INDEX IF NOT EXISTS idx_transfers_from_account     ON transfers (from_account_id) WHERE from_account_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_transfers_to_account       ON transfers (to_account_id) WHERE to_account_id IS NOT NULL;
-- idx_transfers_outbound_op / idx_transfers_inbound_op dropped (migration 0019):
-- redundant, covered by uq_transfers_outbound / uq_transfers_inbound.
CREATE INDEX IF NOT EXISTS idx_payroll_summary_ip         ON payroll_summary (ip_id);

-- ---------------------------------------------------------------------
-- 6.3 Uniqueness-enforcing partial indexes (§41, §57, ADR-010)
--
-- IDEMPOTENCY: a repeated API sync or Excel import must not create a
-- second financial operation. Enforced by the database, because an
-- application-level "check then insert" is not race-safe (§57).
-- ---------------------------------------------------------------------

-- one external operation per (ИП, type)
CREATE UNIQUE INDEX IF NOT EXISTS uq_operations_ip_type_external
    ON operations (ip_id, type_id, external_id)
    WHERE external_id IS NOT NULL;

-- an identical normalised source row can be imported only once
CREATE UNIQUE INDEX IF NOT EXISTS uq_operations_import_fingerprint
    ON operations (ip_id, import_fingerprint)
    WHERE import_fingerprint IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_work_entries_import_fingerprint
    ON work_entries (ip_id, import_fingerprint)
    WHERE import_fingerprint IS NOT NULL;

-- the same raw payload is stored once per (ИП, request type)
CREATE UNIQUE INDEX IF NOT EXISTS uq_raw_payloads_hash
    ON raw_kaspi_payloads (ip_id, request_type, payload_hash)
    WHERE payload_hash IS NOT NULL;

-- exactly one ACTIVE credential per (ИП, api_type) (§18)
CREATE UNIQUE INDEX IF NOT EXISTS uq_kaspi_credentials_active
    ON kaspi_api_credentials (ip_id, api_type)
    WHERE status = 'ACTIVE';

-- a payout line must not be booked twice for the same order (§44)
CREATE UNIQUE INDEX IF NOT EXISTS uq_kaspi_payout_items_order
    ON kaspi_payout_items (payout_id, order_id, item_type)
    WHERE order_id IS NOT NULL;

-- one active (non-reversed) payment per payroll summary — blocks double salary payment (§57, §67)
CREATE UNIQUE INDEX IF NOT EXISTS uq_payroll_payments_active
    ON payroll_payments (payroll_summary_id)
    WHERE status = 'POSTED';

-- =====================================================================
-- SECTION 7 — TRIGGERS
-- =====================================================================

-- ---------------------------------------------------------------------
-- 7.1 updated_at maintenance (§56)
-- ---------------------------------------------------------------------
DO $$
DECLARE
    v_table TEXT;
    v_tables TEXT[] := ARRAY[
        'ip_entities', 'operation_types', 'categories', 'counterparties', 'employees',
        'work_operation_types', 'payroll_periods', 'users', 'roles', 'products',
        'materials', 'accounts', 'tax_rates_history', 'kaspi_api_credentials', 'orders',
        'order_items', 'order_materials', 'kaspi_payouts', 'kaspi_payout_items',
        'operations', 'transfers', 'rates_history', 'work_entries', 'payroll_summary',
        'payroll_payments'
    ];
BEGIN
    FOREACH v_table IN ARRAY v_tables LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS trg_%s_updated_at ON %I', v_table, v_table);
        EXECUTE format(
            'CREATE TRIGGER trg_%s_updated_at BEFORE UPDATE ON %I
             FOR EACH ROW EXECUTE FUNCTION fn_set_updated_at()',
            v_table, v_table
        );
    END LOOP;
END;
$$;

-- ---------------------------------------------------------------------
-- 7.2 DELETE protection on financial tables (§16, ADR-007)
-- ---------------------------------------------------------------------
DO $$
DECLARE
    v_table TEXT;
    v_tables TEXT[] := ARRAY[
        'operations', 'transfers', 'payroll_payments', 'payroll_summary',
        'kaspi_payouts', 'kaspi_payout_items', 'work_entries', 'audit_log',
        -- TD-019 (2026-10-01): provenance tables. operations/transfers keep
        -- ON DELETE SET NULL links to these; a plain DELETE would silently
        -- destroy the provenance chain (Absolute Rule 12), so they get the
        -- same fn_forbid_delete() guard. Maintenance hatch unchanged.
        'raw_kaspi_payloads', 'import_batches'
    ];
BEGIN
    FOREACH v_table IN ARRAY v_tables LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS trg_%s_forbid_delete ON %I', v_table, v_table);
        EXECUTE format(
            'CREATE TRIGGER trg_%s_forbid_delete BEFORE DELETE ON %I
             FOR EACH ROW EXECUTE FUNCTION fn_forbid_delete()',
            v_table, v_table
        );
    END LOOP;
END;
$$;

-- ---------------------------------------------------------------------
-- 7.3 Audit triggers (§54)
--
-- Covers: financial changes, payments, refunds, status changes,
-- rate changes, permission changes.
-- audit_log itself is NOT audited (no recursion).
-- ---------------------------------------------------------------------
DO $$
DECLARE
    v_table TEXT;
    v_tables TEXT[] := ARRAY[
        'operations', 'transfers', 'orders', 'kaspi_payouts', 'kaspi_payout_items',
        'work_entries', 'rates_history', 'payroll_summary', 'payroll_payments',
        'payroll_periods',
        'employees', 'materials', 'products', 'users', 'user_roles', 'roles',
        'kaspi_api_credentials', 'ip_entities', 'accounts', 'tax_rates_history',
        -- A-2/A-3/A-4 (2026-10-03): provenance, order line items, reference data
        'import_batches', 'raw_kaspi_payloads',
        'order_items', 'order_materials', 'order_status_history',
        'categories', 'counterparties', 'operation_types', 'work_operation_types'
    ];
BEGIN
    FOREACH v_table IN ARRAY v_tables LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS trg_%s_audit ON %I', v_table, v_table);
        EXECUTE format(
            'CREATE TRIGGER trg_%s_audit AFTER INSERT OR UPDATE OR DELETE ON %I
             FOR EACH ROW EXECUTE FUNCTION fn_audit()',
            v_table, v_table
        );
    END LOOP;
END;
$$;

-- ---------------------------------------------------------------------
-- 7.4 Payroll overpayment guard (P2-1, 2026-10-03)
-- ---------------------------------------------------------------------
DROP TRIGGER IF EXISTS trg_payroll_payments_no_overpay ON payroll_payments;
CREATE TRIGGER trg_payroll_payments_no_overpay
    BEFORE INSERT OR UPDATE ON payroll_payments
    FOR EACH ROW EXECUTE FUNCTION fn_payroll_payment_no_overpay();

-- ---------------------------------------------------------------------
-- 7.5 Rate freeze (§32, ADR-008)
-- ---------------------------------------------------------------------
DROP TRIGGER IF EXISTS trg_work_entry_freeze_rate ON work_entries;
CREATE TRIGGER trg_work_entry_freeze_rate
    BEFORE INSERT ON work_entries
    FOR EACH ROW EXECUTE FUNCTION fn_work_entry_freeze_rate();

DROP TRIGGER IF EXISTS trg_work_entry_protect_rate ON work_entries;
CREATE TRIGGER trg_work_entry_protect_rate
    BEFORE UPDATE ON work_entries
    FOR EACH ROW EXECUTE FUNCTION fn_work_entry_protect_rate();

-- ---------------------------------------------------------------------
-- 7.5 Material price freeze (§38, ADR-008)
-- ---------------------------------------------------------------------
DROP TRIGGER IF EXISTS trg_order_material_freeze_price ON order_materials;
CREATE TRIGGER trg_order_material_freeze_price
    BEFORE INSERT ON order_materials
    FOR EACH ROW EXECUTE FUNCTION fn_order_material_freeze_price();

DROP TRIGGER IF EXISTS trg_order_material_protect_price ON order_materials;
CREATE TRIGGER trg_order_material_protect_price
    BEFORE UPDATE ON order_materials
    FOR EACH ROW EXECUTE FUNCTION fn_order_material_protect_price();

-- ---------------------------------------------------------------------
-- 7.6 Order status history (§50)
-- ---------------------------------------------------------------------
DROP TRIGGER IF EXISTS trg_orders_status_history ON orders;
CREATE TRIGGER trg_orders_status_history
    AFTER INSERT OR UPDATE OF status ON orders
    FOR EACH ROW EXECUTE FUNCTION fn_log_order_status_change();

-- ---------------------------------------------------------------------
-- 7.7 Operation integrity: sign and category kind (§21, §27, §28)
-- ---------------------------------------------------------------------
DROP TRIGGER IF EXISTS trg_operations_check_sign ON operations;
CREATE TRIGGER trg_operations_check_sign
    BEFORE INSERT OR UPDATE OF amount, type_id ON operations
    FOR EACH ROW EXECUTE FUNCTION fn_check_operation_sign();

DROP TRIGGER IF EXISTS trg_operations_check_category ON operations;
CREATE TRIGGER trg_operations_check_category
    BEFORE INSERT OR UPDATE OF category_id, type_id ON operations
    FOR EACH ROW EXECUTE FUNCTION fn_check_operation_category();

DROP TRIGGER IF EXISTS trg_operations_protect_posted ON operations;
CREATE TRIGGER trg_operations_protect_posted
    BEFORE UPDATE ON operations
    FOR EACH ROW EXECUTE FUNCTION fn_protect_posted_operation();

-- FIN-OPS-0001: non-transfer posted-ledger immutability.
DROP TRIGGER IF EXISTS trg_operations_protect_posted_ledger ON operations;
CREATE TRIGGER trg_operations_protect_posted_ledger
    BEFORE UPDATE ON operations
    FOR EACH ROW EXECUTE FUNCTION fn_protect_posted_ledger_operation();

DROP TRIGGER IF EXISTS trg_transfers_protect_posted ON transfers;
CREATE TRIGGER trg_transfers_protect_posted
    BEFORE UPDATE ON transfers
    FOR EACH ROW EXECUTE FUNCTION fn_protect_posted_transfer();

DROP TRIGGER IF EXISTS trg_transfers_validate_ledger_pair ON transfers;
CREATE CONSTRAINT TRIGGER trg_transfers_validate_ledger_pair
    AFTER INSERT OR UPDATE ON transfers
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION fn_validate_transfer_ledger_pair();

DROP TRIGGER IF EXISTS trg_operations_validate_transfer_pair ON operations;
CREATE CONSTRAINT TRIGGER trg_operations_validate_transfer_pair
    AFTER INSERT OR UPDATE ON operations
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION fn_validate_transfer_ledger_pair();

-- =====================================================================
-- SECTION 8 — VIEWS
--
-- 8.1–8.3 MANDATORY VIEWS (§63)
-- 8.4–8.5 EXTENSIONS — explicitly marked as such (§63)
-- =====================================================================

-- ---------------------------------------------------------------------
-- 8.1 v_ip_financial_summary                            [MANDATORY, §63]
--
-- Financial summary per ИП per month.
-- §76 / §28 — transfers are excluded from income and expense
-- (operation_types.affects_pnl = FALSE) and reported separately.
-- Reversed operations are excluded from the totals (§16).
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_ip_financial_summary AS
SELECT
    ip.id                                       AS ip_id,
    ip.name                                     AS ip_name,
    ip.bin                                      AS ip_bin,
    -- Period by accrual_date for P&L, operation_date for transfers/cash.
    -- P&L columns filter WHERE accrual_date IS NOT NULL (no double-count).
    date_trunc('month', COALESCE(o.accrual_date, o.operation_date))::DATE AS period_month,
    -- income / expense (P&L only, transfers excluded; accrual basis)
    coalesce(sum(o.amount) FILTER (WHERE ot.kind = 'INCOME' AND o.accrual_date IS NOT NULL),  0)::NUMERIC(15,2)         AS total_income,
    coalesce(sum(-o.amount) FILTER (WHERE ot.kind = 'EXPENSE' AND o.accrual_date IS NOT NULL), 0)::NUMERIC(15,2)        AS total_expense,
    coalesce(sum(o.amount) FILTER (WHERE ot.affects_pnl AND o.accrual_date IS NOT NULL), 0)::NUMERIC(15,2)              AS net_profit,
    -- revenue breakdown by source
    coalesce(sum(o.amount) FILTER (WHERE ot.code = 'SALE_KASPI_SHOP' AND o.accrual_date IS NOT NULL), 0)::NUMERIC(15,2) AS revenue_kaspi_shop,
    coalesce(sum(o.amount) FILTER (WHERE ot.code = 'SALE_KASPI_PAY' AND o.accrual_date IS NOT NULL),  0)::NUMERIC(15,2) AS revenue_kaspi_pay,
    -- expense breakdown
    coalesce(sum(-o.amount) FILTER (WHERE ot.code = 'EXPENSE_MATERIAL' AND o.accrual_date IS NOT NULL),  0)::NUMERIC(15,2) AS expense_material,
    coalesce(sum(-o.amount) FILTER (WHERE ot.code = 'EXPENSE_KASPI_FEE' AND o.accrual_date IS NOT NULL), 0)::NUMERIC(15,2) AS expense_kaspi_fee,
    coalesce(sum(-o.amount) FILTER (WHERE ot.code = 'SALARY_PAYMENT' AND o.accrual_date IS NOT NULL),    0)::NUMERIC(15,2) AS expense_salary,
    -- transfers: reported, never counted as income or expense (§28, §76)
    coalesce(sum(o.amount) FILTER (WHERE ot.code = 'TRANSFER_IN'),   0)::NUMERIC(15,2)   AS transfers_in,
    coalesce(sum(-o.amount) FILTER (WHERE ot.code = 'TRANSFER_OUT'), 0)::NUMERIC(15,2)   AS transfers_out,
    count(*)                                                                             AS operations_count
FROM ip_entities ip
JOIN operations      o  ON o.ip_id = ip.id AND o.status = 'POSTED'
JOIN operation_types ot ON ot.id = o.type_id
GROUP BY ip.id, ip.name, ip.bin, date_trunc('month', COALESCE(o.accrual_date, o.operation_date));

COMMENT ON VIEW v_ip_financial_summary IS
'MANDATORY VIEW (§63). Monthly financial summary per ИП. Transfers are excluded from P&L (§28, §76); reversed operations excluded (§16).';

-- ---------------------------------------------------------------------
-- 8.x v_cash_flow — §13 FINANCE-OPS-001 «КУДА ДЕЛИСЬ МОИ ДЕНЬГИ?»
--
-- Cash-flow function with the fully specified §13 formula:
--   START BALANCE
--   + SALES + OTHER INFLOWS + PAYOUTS
--   - MATERIALS - PAYROLL - SUPPLIERS - COMMISSIONS - REFUNDS
--   - OPERATING EXPENSES - TRANSFERS OUT
--   = ENDING BALANCE
-- Rule: transfer ≠ expense (transfers shown separately, never as expense).
--
-- Only POSTED operations are counted (§16). Amounts are positive magnitudes
-- in the breakdown lines (sign is encoded by the line's position).
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION cash_flow(
    p_ip_id INT,
    p_period_start DATE,
    p_period_end DATE
)
RETURNS TABLE (
    ip_id INT,
    period_start DATE,
    period_end DATE,
    start_balance NUMERIC(15,2),
    sales NUMERIC(15,2),
    other_inflows NUMERIC(15,2),
    payouts NUMERIC(15,2),
    materials NUMERIC(15,2),
    payroll NUMERIC(15,2),
    suppliers NUMERIC(15,2),
    commissions NUMERIC(15,2),
    refunds NUMERIC(15,2),
    operating_expenses NUMERIC(15,2),
    transfers_out NUMERIC(15,2),
    ending_balance NUMERIC(15,2)
)
LANGUAGE sql STABLE AS $$
    WITH period_ops AS (
        SELECT o.amount, ot.code, ot.kind
        FROM operations o
        JOIN operation_types ot ON ot.id = o.type_id
        WHERE o.ip_id = p_ip_id
          AND o.status = 'POSTED'
          AND o.operation_date >= p_period_start
          AND o.operation_date <= p_period_end
    ),
    start_bal AS (
        SELECT coalesce(sum(o.amount), 0)::NUMERIC(15,2) AS bal
        FROM operations o
        WHERE o.ip_id = p_ip_id
          AND o.status = 'POSTED'
          AND o.operation_date < p_period_start
    )
    SELECT
        p_ip_id,
        p_period_start,
        p_period_end,
        (SELECT bal FROM start_bal),
        -- inflows (positive magnitudes)
        coalesce(sum(amount) FILTER (WHERE code IN ('SALE_KASPI_SHOP', 'SALE_KASPI_PAY')), 0)::NUMERIC(15,2),
        coalesce(sum(amount) FILTER (WHERE kind = 'INCOME' AND code NOT IN ('SALE_KASPI_SHOP', 'SALE_KASPI_PAY')), 0)::NUMERIC(15,2),
        -- payouts: Kaspi payouts arrive as INCOME operations linked from kaspi_payouts;
        -- they are part of inflows; shown separately when the link exists
        coalesce((
            SELECT sum(o.amount)::NUMERIC(15,2)
            FROM operations o
            JOIN kaspi_payouts kp ON kp.operation_id = o.id
            WHERE o.ip_id = p_ip_id
              AND o.status = 'POSTED'
              AND o.operation_date >= p_period_start
              AND o.operation_date <= p_period_end
        ), 0),
        -- outflows (positive magnitudes)
        coalesce(sum(-amount) FILTER (WHERE code = 'EXPENSE_MATERIAL'), 0)::NUMERIC(15,2),
        coalesce(sum(-amount) FILTER (WHERE code = 'SALARY_PAYMENT'), 0)::NUMERIC(15,2),
        0::NUMERIC(15,2),  -- suppliers: no dedicated type yet; mapped to operating_expenses
        coalesce(sum(-amount) FILTER (WHERE code = 'EXPENSE_KASPI_FEE'), 0)::NUMERIC(15,2),
        0::NUMERIC(15,2),  -- refunds: modeled as negative INCOME; shown in sales net
        coalesce(sum(-amount) FILTER (WHERE kind = 'EXPENSE'
            AND code NOT IN ('EXPENSE_MATERIAL', 'SALARY_PAYMENT', 'EXPENSE_KASPI_FEE')), 0)::NUMERIC(15,2),
        -- transfers: separate line, never an expense (§13 rule)
        coalesce(sum(-amount) FILTER (WHERE code = 'TRANSFER_OUT'), 0)::NUMERIC(15,2),
        -- ending = start + inflows - outflows - transfers_out
        (SELECT bal FROM start_bal)
        + coalesce(sum(amount) FILTER (WHERE kind = 'INCOME'), 0)
        + coalesce(sum(amount) FILTER (WHERE code = 'TRANSFER_IN'), 0)
        - coalesce(sum(-amount) FILTER (WHERE kind = 'EXPENSE'), 0)
        - coalesce(sum(-amount) FILTER (WHERE code = 'TRANSFER_OUT'), 0)
    FROM period_ops;
$$;

COMMENT ON FUNCTION cash_flow(INT, DATE, DATE) IS
'§13 FINANCE-OPS-001 «КУДА ДЕЛИСЬ МОИ ДЕНЬГИ?». Cash-flow breakdown with the specified formula. transfer ≠ expense.';


-- ---------------------------------------------------------------------
-- 8.2 v_payroll_due                                     [MANDATORY, §63]
--
-- What is still owed to each employee per payroll period:
-- total_calculated (§35) minus the sum of posted payments (§36).
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_payroll_due AS
SELECT
    ps.id                                   AS payroll_summary_id,
    ps.employee_id,
    e.full_name                             AS employee_name,
    e.payment_type,
    ps.payroll_period_id,
    pp.start_date                           AS period_start,
    pp.end_date                             AS period_end,
    pp.status                               AS period_status,
    ps.ip_id,
    ps.piecework_total,
    ps.base_salary,
    ps.bonuses,
    ps.deductions,
    ps.total_calculated,
    coalesce(paid.amount_paid, 0)::NUMERIC(15,2)                          AS amount_paid,
    (ps.total_calculated - coalesce(paid.amount_paid, 0))::NUMERIC(15,2)  AS amount_due,
    ps.status                               AS summary_status,
    coalesce(paid.payments_count, 0)        AS payments_count,
    paid.last_payment_date
FROM payroll_summary ps
JOIN employees       e  ON e.id = ps.employee_id
JOIN payroll_periods pp ON pp.id = ps.payroll_period_id
LEFT JOIN (
    SELECT
        payroll_summary_id,
        sum(amount)       AS amount_paid,
        count(*)          AS payments_count,
        max(payment_date) AS last_payment_date
    FROM payroll_payments
    WHERE status = 'POSTED'
    GROUP BY payroll_summary_id
) paid ON paid.payroll_summary_id = ps.id
WHERE ps.status <> 'CANCELLED';

COMMENT ON VIEW v_payroll_due IS
'MANDATORY VIEW (§63). Outstanding salary per employee per period: total_calculated − posted payments (§35, §36).';

-- ---------------------------------------------------------------------
-- 8.3 v_work_entries_detail                             [MANDATORY, §63]
--
-- Denormalised piecework register for the production report and the
-- brigadier UI (§61, §62).
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_work_entries_detail AS
SELECT
    we.id                       AS work_entry_id,
    we.entry_date,
    we.employee_id,
    e.full_name                 AS employee_name,
    e.position                  AS employee_position,
    e.payment_type              AS employee_payment_type,
    we.work_operation_type_id,
    wot.code                    AS work_operation_code,
    wot.name                    AS work_operation_name,
    wot.unit                    AS work_operation_unit,
    wot.calc_mode               AS work_operation_calc_mode,
    we.order_id,
    o.external_order_id,
    o.status                    AS order_status,
    we.ip_id,
    ip.name                     AS ip_name,
    we.quantity,
    we.rate_at_time,
    we.total_sum,
    we.status,
    we.payroll_period_id,
    we.created_by_user,
    u.username                  AS created_by_username,
    we.comment,
    we.created_at
FROM work_entries we
JOIN employees            e   ON e.id  = we.employee_id
JOIN work_operation_types wot ON wot.id = we.work_operation_type_id
JOIN ip_entities          ip  ON ip.id = we.ip_id
LEFT JOIN orders          o   ON o.id  = we.order_id
LEFT JOIN users           u   ON u.id  = we.created_by_user;

COMMENT ON VIEW v_work_entries_detail IS
'MANDATORY VIEW (§63). Denormalised piecework entries with employee, operation type, order and frozen rate (§33).';

-- ---------------------------------------------------------------------
-- 8.4 v_order_costing                    [VIEW EXTENSION, §37, §76]
--
-- EXTENSION — explicitly marked as such per §63.
-- Implements REQ-0308 at the reporting level:
--   Direct Cost  = Material Cost + Labour Cost
--   Gross Profit = Revenue − Direct Cost
--   Margin       = Gross Profit / Revenue
-- Kaspi commission and delivery fee are reported separately: they are
-- not part of Direct Cost as defined in §37.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_order_costing AS
SELECT
    o.id                            AS order_id,
    o.ip_id,
    ip.name                         AS ip_name,
    o.external_order_id,
    o.source,
    o.status,
    o.order_date,
    o.total_amount                  AS revenue,
    o.kaspi_commission,
    o.delivery_fee,
    coalesce(mat.material_cost, 0)::NUMERIC(15,2)  AS material_cost,
    coalesce(lab.labor_cost, 0)::NUMERIC(15,2)     AS labor_cost,
    (coalesce(mat.material_cost, 0) + coalesce(lab.labor_cost, 0))::NUMERIC(15,2)
                                                   AS total_direct_cost,
    (o.total_amount - coalesce(mat.material_cost, 0) - coalesce(lab.labor_cost, 0))::NUMERIC(15,2)
                                                   AS gross_profit,
    CASE
        WHEN o.total_amount > 0 THEN
            round(
                (o.total_amount - coalesce(mat.material_cost, 0) - coalesce(lab.labor_cost, 0))
                / o.total_amount * 100, 2
            )
        ELSE NULL
    END                                            AS margin_percent,
    coalesce(mat.materials_count, 0)               AS materials_count,
    coalesce(lab.work_entries_count, 0)            AS work_entries_count
FROM orders o
JOIN ip_entities ip ON ip.id = o.ip_id
LEFT JOIN (
    SELECT order_id, sum(total_cost) AS material_cost, count(*) AS materials_count
    FROM order_materials
    GROUP BY order_id
) mat ON mat.order_id = o.id
LEFT JOIN (
    SELECT order_id, sum(total_sum) AS labor_cost, count(*) AS work_entries_count
    FROM work_entries
    WHERE order_id IS NOT NULL AND status IN ('CONFIRMED', 'PAID')
    GROUP BY order_id
) lab ON lab.order_id = o.id;

COMMENT ON VIEW v_order_costing IS
'VIEW EXTENSION (§63). Order costing: Direct Cost = Material + Labour; Gross Profit; Margin (§37, §76). Commission/delivery reported separately.';

-- ---------------------------------------------------------------------
-- 8.5 v_kaspi_reconciliation             [VIEW EXTENSION, §44]
--
-- EXTENSION — explicitly marked as such per §63.
-- Order ↔ Payout ↔ Operation triangle used by the reconciliation
-- report (§44, §62). The verdict column mirrors match_status and adds
-- ledger presence.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_kaspi_reconciliation AS
SELECT
    o.id                            AS order_id,
    o.ip_id,
    o.external_order_id,
    o.status                        AS order_status,
    o.total_amount                  AS order_amount,
    o.kaspi_commission              AS order_commission,
    o.delivery_fee                  AS order_delivery_fee,
    pi.id                           AS payout_item_id,
    pi.payout_id,
    p.external_payout_id,
    p.payout_date,
    p.status                        AS payout_status,
    pi.gross_amount                 AS payout_gross_amount,
    pi.commission_amount            AS payout_commission,
    pi.delivery_amount              AS payout_delivery,
    pi.refund_amount                AS payout_refund,
    pi.net_amount                   AS payout_net_amount,
    pi.item_type,
    pi.match_status,
    p.operation_id,
    op.amount                       AS operation_amount,
    op.operation_date,
    op.status                       AS operation_status,
    (o.total_amount - pi.gross_amount)::NUMERIC(15,2)         AS amount_delta,
    (o.kaspi_commission - pi.commission_amount)::NUMERIC(15,2) AS commission_delta,
    (o.delivery_fee - pi.delivery_amount)::NUMERIC(15,2)       AS delivery_delta,
    CASE
        WHEN pi.id IS NULL                                    THEN 'NO_PAYOUT'
        WHEN p.operation_id IS NULL                           THEN 'PAYOUT_NOT_IN_LEDGER'
        -- P1 (2026-10-03): use the same 1.00 ₸ tolerance as the service
        -- (ReconciliationRule.amount_tolerance default). Strict <> caused
        -- the view to show AMOUNT_MISMATCH where the service says MATCHED.
        WHEN abs(o.total_amount - pi.gross_amount) > 1.00     THEN 'AMOUNT_MISMATCH'
        WHEN abs(o.kaspi_commission - pi.commission_amount) > 1.00 THEN 'COMMISSION_MISMATCH'
        WHEN abs(o.delivery_fee - pi.delivery_amount) > 1.00 THEN 'DELIVERY_MISMATCH'
        WHEN pi.refund_amount > 0                             THEN 'REFUND'
        ELSE 'OK'
    END                             AS verdict
FROM orders o
LEFT JOIN kaspi_payout_items pi ON pi.order_id = o.id
LEFT JOIN kaspi_payouts      p  ON p.id  = pi.payout_id
LEFT JOIN operations         op ON op.id = p.operation_id
WHERE o.source IN ('KASPI_SHOP', 'KASPI_PAY');

COMMENT ON VIEW v_kaspi_reconciliation IS
'VIEW EXTENSION (§63). Order ↔ Payout ↔ Operation reconciliation with per-row verdict (§44).';

-- =====================================================================
-- SECTION 9 — SEED DATA (§85, §86)
--
-- IDEMPOTENT (§87): ON CONFLICT DO NOTHING on natural keys.
-- Business data (ИП, employees, products) is NOT seeded — the values are
-- unknown (OQ-013) and §94 forbids inventing them.
-- =====================================================================

-- ---------------------------------------------------------------------
-- 9.1 operation_types — the 7 mandatory types (§20)
-- ---------------------------------------------------------------------
INSERT INTO operation_types (code, name, direction, kind, affects_pnl, is_system) VALUES
    ('SALE_KASPI_SHOP',   'Продажа Kaspi Магазин',      'IN',  'INCOME',   TRUE,  TRUE),
    ('SALE_KASPI_PAY',    'Продажа Kaspi Pay',          'IN',  'INCOME',   TRUE,  TRUE),
    ('EXPENSE_MATERIAL',  'Расход на материалы',        'OUT', 'EXPENSE',  TRUE,  TRUE),
    ('EXPENSE_KASPI_FEE', 'Комиссия Kaspi',             'OUT', 'EXPENSE',  TRUE,  TRUE),
    ('EXPENSE_BANK_FEE',  'Комиссия банка',             'OUT', 'EXPENSE',  TRUE,  TRUE),
    ('SALARY_PAYMENT',    'Выплата заработной платы',   'OUT', 'EXPENSE',  TRUE,  TRUE),
    ('TRANSFER_IN',       'Перевод между ИП (приход)',  'IN',  'TRANSFER', FALSE, TRUE),
    ('TRANSFER_OUT',      'Перевод между ИП (расход)',  'OUT', 'TRANSFER', FALSE, TRUE)
ON CONFLICT (code) DO NOTHING;

-- ---------------------------------------------------------------------
-- 9.2 categories — base income and expense categories (§21)
-- ---------------------------------------------------------------------
INSERT INTO categories (code, name, kind, is_system) VALUES
    -- Доходы
    ('INCOME_KASPI_SHOP',   'Kaspi Shop',            'INCOME',  TRUE),
    ('INCOME_KASPI_PAY',    'Kaspi Pay',             'INCOME',  TRUE),
    ('INCOME_OTHER',        'Прочие доходы',         'INCOME',  TRUE),
    -- Расходы
    ('EXPENSE_MATERIALS',   'Материалы',             'EXPENSE', TRUE),
    ('EXPENSE_KASPI_FEE',   'Комиссия Kaspi',        'EXPENSE', TRUE),
    ('EXPENSE_DELIVERY',    'Доставка',              'EXPENSE', TRUE),
    ('EXPENSE_SALARY',      'Зарплата',              'EXPENSE', TRUE),
    ('EXPENSE_PRODUCTION',  'Производство',          'EXPENSE', TRUE),
    ('EXPENSE_RENT',        'Аренда',                'EXPENSE', TRUE),
    ('EXPENSE_UTILITIES',   'Коммунальные расходы',  'EXPENSE', TRUE),
    ('EXPENSE_ADVERTISING', 'Реклама',               'EXPENSE', TRUE),
    ('EXPENSE_OTHER',       'Прочие расходы',        'EXPENSE', TRUE)
ON CONFLICT (code) DO NOTHING;

-- ---------------------------------------------------------------------
-- 9.3 work_operation_types — the 11 mandatory types (§30)
-- ---------------------------------------------------------------------
INSERT INTO work_operation_types (code, name, unit, calc_mode) VALUES
    ('SAWING',              'Расип ЛДСП/МДФ',            'м2',        'per_unit'),
    ('EDGING_STATIONARY',   'Кромка станок',             'пог_м',     'per_unit'),
    ('EDGING_MANUAL',       'Кромка вручную',            'пог_м',     'per_unit'),
    ('DRILLING_FITTING',    'Присадка фурнитуры',        'отверстие', 'per_unit'),
    ('MILLING',             'Фрезеровка',                'пог_м',     'per_unit'),
    ('DRILLING_CONFIRMAT',  'Сверление под евровинт',    'отверстие', 'per_unit'),
    ('ASSEMBLY',            'Сборка корпуса',            'шт',        'per_unit'),
    ('PACKAGING',           'Упаковка',                  'шт',        'per_unit'),
    ('LOADING',             'Погрузка',                  'часы',      'per_hour'),
    ('DELIVERY_MOUNT',      'Доставка и монтаж',         'рейс',      'per_order'),
    ('MATERIAL_RECEIVING',  'Приемка материала',         'часы',      'per_hour')
ON CONFLICT (code) DO NOTHING;

-- ---------------------------------------------------------------------
-- 9.4 rates_history — the 11 starting rates (§86)
--
-- effective_from = 2024-01-01 00:00:00+00 (§31, §86)
-- Idempotent: inserted only when no rate period exists for the type.
-- ---------------------------------------------------------------------
INSERT INTO rates_history (work_operation_type_id, rate, effective_from, effective_to, comment)
SELECT wot.id, v.rate, TIMESTAMPTZ '2024-01-01 00:00:00+00', NULL, 'Стартовая ставка (MASTER PROMPT §86)'
FROM (VALUES
    ('SAWING',             150.00),
    ('EDGING_STATIONARY',   80.00),
    ('EDGING_MANUAL',      120.00),
    ('DRILLING_FITTING',    30.00),
    ('MILLING',            200.00),
    ('DRILLING_CONFIRMAT',  20.00),
    ('ASSEMBLY',          2000.00),
    ('PACKAGING',          500.00),
    ('LOADING',           1500.00),
    ('DELIVERY_MOUNT',   10000.00),
    ('MATERIAL_RECEIVING', 1200.00)
) AS v (code, rate)
JOIN work_operation_types wot ON wot.code = v.code
WHERE NOT EXISTS (
    SELECT 1 FROM rates_history rh WHERE rh.work_operation_type_id = wot.id
);

-- ---------------------------------------------------------------------
-- 9.5 roles — the 7 mandatory RBAC roles (§52, §53)
-- ---------------------------------------------------------------------
INSERT INTO roles (code, name, description, is_system) VALUES
    ('ADMIN',        'Администратор',      'Полный доступ ко всей системе (§53)',                      TRUE),
    ('DIRECTOR',     'Директор',           'Доступ ко всем бизнес-данным (§53)',                       TRUE),
    ('ACCOUNTANT',   'Бухгалтер',          'Финансы: операции, сверка, отчеты (§53)',                  TRUE),
    ('HR',           'Кадры',              'Сотрудники и расчет зарплаты (§53)',                       TRUE),
    ('SHOP_MANAGER', 'Менеджер магазина',  'Заказы и Kaspi (§53)',                                     TRUE),
    ('BRIGADIER',    'Бригадир',           'Производство: ввод сдельных работ (§53, §61)',             TRUE),
    ('VIEWER',       'Наблюдатель',        'Только чтение (§53)',                                      TRUE)
ON CONFLICT (code) DO NOTHING;

-- =====================================================================
-- END OF SECTION 9
--
-- NOT SEEDED, intentionally (§94 — do not invent data):
--   ip_entities        — actual ИП names and БИН are unknown (OQ-013)
--   employees          — staff list and salaries are unknown (OQ-007)
--   users              — no default account is created; a bootstrap
--                        script with a caller-supplied password will be
--                        added in STEP 8 (a hard-coded default password
--                        would be a security defect, §55)
--   materials/products — catalogue is unknown
--   tax_rates_history  — tax regime is unknown (OQ-001, §75)
--   accounts           — bank/Kaspi account structure is unknown (OQ-009)
-- =====================================================================

-- ---------------------------------------------------------------------
-- TD-015 — auth_rate_events: shared sliding-window store for the §55
-- auth rate limiter (ADR-014). One row per rate-limited attempt; the
-- limiter COUNTs rows in (now - window, now] for the bucket key and
-- prunes rows older than the retention horizon. No FK to users on
-- purpose: unknown usernames and pre-auth IP floods must also be
-- limited (credential stuffing).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.auth_rate_events (
    id          BIGSERIAL PRIMARY KEY,
    bucket_key  VARCHAR(256) NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_auth_rate_events_bucket_time
    ON public.auth_rate_events (bucket_key, occurred_at);

COMMENT ON TABLE public.auth_rate_events IS
    'TD-015/ADR-014 — shared auth rate-limit events (§55); sliding '
    'window over bucket_key; prune rows beyond the retention horizon.';

-- ---------------------------------------------------------------------
-- 8.6 v_counterparty_balances                    [BOOK_SYNTHESIS_2026-10-03]
--
-- AR/AP Tier 1: counterparty-level balances derived from operations.
-- Formula: Balance = SUM(accruals) - SUM(cash settlements)
--   where accruals = operations with accrual_date NOT NULL
--         cash settlements = operations with accrual_date IS NULL
--
-- Positive balance = AR (counterparty owes us)
-- Negative balance = AP (we owe counterparty)
--
-- Proof of correctness:
--   Sale accrual +800k (accrual_date set), no payment → balance +800k (AR) ✓
--   Customer pays +800k (accrual_date NULL) → 800k - 800k = 0 (settled) ✓
--   Expense accrual -500k (accrual_date set), no payment → balance -500k (AP) ✓
--   Pay supplier -500k (accrual_date NULL) → -500k - (-500k) = 0 (settled) ✓
--
-- Note: Opening balances require opening_balances table (GAP-3B).
-- Invoice-level AR/AP (Tier 2) requires dedicated tables — deferred.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_counterparty_balances AS
SELECT
    o.ip_id,
    o.counterparty_id,
    c.name AS counterparty_name,
    COALESCE(
        SUM(o.amount) FILTER (WHERE o.accrual_date IS NOT NULL),
        0
    )::NUMERIC(15,2) AS total_accrued,
    COALESCE(
        SUM(o.amount) FILTER (WHERE o.accrual_date IS NULL),
        0
    )::NUMERIC(15,2) AS total_settled,
    (
        COALESCE(SUM(o.amount) FILTER (WHERE o.accrual_date IS NOT NULL), 0) -
        COALESCE(SUM(o.amount) FILTER (WHERE o.accrual_date IS NULL), 0)
    )::NUMERIC(15,2) AS balance
FROM operations o
LEFT JOIN counterparties c ON c.id = o.counterparty_id
WHERE o.counterparty_id IS NOT NULL
  AND o.status = 'POSTED'
GROUP BY o.ip_id, o.counterparty_id, c.name;

COMMENT ON VIEW v_counterparty_balances IS
'AR/AP Tier 1 (BOOK_SYNTHESIS_2026-10-03, PHASE 5). Counterparty balances: '
'Balance = SUM(accruals) - SUM(cash settlements). '
'Positive = AR (they owe us), Negative = AP (we owe them).';

-- ---------------------------------------------------------------------
-- 8.x v_material_stock / v_material_availability / v_wip_by_order /
--     v_finished_goods_stock
--                                              [EXTENSION, P2-INV-001..004, G-C2]
--
-- Physical truth: on-hand per (IP, material). No stored quantity_on_hand
-- anywhere — stored aggregates drift; these views are the single source.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_material_stock AS
SELECT ip_id,
       material_id,
       SUM(quantity_delta)::NUMERIC(15,3) AS on_hand,
       COUNT(*)::BIGINT                   AS movement_count
FROM stock_movements
GROUP BY ip_id, material_id;

-- Availability = on-hand minus active, unexpired reservations.
-- Expiry is read-time: no background job required for correctness.
CREATE OR REPLACE VIEW v_material_availability AS
SELECT s.ip_id,
       s.material_id,
       s.on_hand,
       COALESCE(r.reserved_qty, 0)::NUMERIC(15,3) AS reserved_qty,
       (s.on_hand - COALESCE(r.reserved_qty, 0))::NUMERIC(15,3) AS available_qty
FROM v_material_stock s
LEFT JOIN (
    SELECT ip_id, material_id,
           SUM(quantity)::NUMERIC(15,3) AS reserved_qty
    FROM material_reservations
    WHERE status = 'ACTIVE'
      AND (expires_at IS NULL OR expires_at > now())
    GROUP BY ip_id, material_id
) r USING (ip_id, material_id);

-- WIP per order: frozen material cost (price_at_time) + confirmed labour.
-- NEW (not started), COMPLETED, CANCELLED, RETURNED excluded per
-- chk_orders_status. DELIVERING included — goods left the shop but
-- revenue may not be recognized yet (conservative).
CREATE OR REPLACE VIEW v_wip_by_order AS
SELECT o.id                 AS order_id,
       o.ip_id,
       o.external_order_id,
       o.status,
       COALESCE(m.material_cost, 0)::NUMERIC(15,2) AS wip_materials,
       COALESCE(w.labour_cost, 0)::NUMERIC(15,2)    AS wip_labour,
       (COALESCE(m.material_cost, 0)
        + COALESCE(w.labour_cost, 0))::NUMERIC(15,2) AS wip_total
FROM orders o
LEFT JOIN (
    SELECT order_id, SUM(total_cost) AS material_cost
    FROM order_materials
    GROUP BY order_id
) m ON m.order_id = o.id
LEFT JOIN (
    SELECT order_id, SUM(total_sum) AS labour_cost
    FROM work_entries
    WHERE status IN ('CONFIRMED', 'PAID')
    GROUP BY order_id
) w ON w.order_id = o.id
WHERE o.status IN ('APPROVED', 'ASSEMBLING', 'DELIVERING');

COMMENT ON VIEW v_material_stock IS
'P2-INV-001: physical on-hand per (IP, material) = SUM(quantity_delta).';
COMMENT ON VIEW v_material_availability IS
'P2-INV-003: on-hand minus active, unexpired reservations (expiry read-time).';
COMMENT ON VIEW v_wip_by_order IS
'P2-INV-004: derived WIP per open order — frozen material cost + confirmed labour.';

-- Finished-goods stock per (IP, product): produced minus delivered.
-- Delivered = SUM(order_items.quantity) on COMPLETED orders
-- (chk_orders_status). order_items.product_id is NULLable — rows without
-- a product are excluded. Quantity only, no valuation (O-C1 blocked).
-- DI-001 (fn_production_output_ip) guarantees production_output for an
-- order shares the order's IP, so produced/delivered align per IP.
CREATE OR REPLACE VIEW v_finished_goods_stock AS
SELECT p.ip_id,
       p.product_id,
       p.produced_qty,
       COALESCE(d.delivered_qty, 0)::NUMERIC(15,3) AS delivered_qty,
       (p.produced_qty - COALESCE(d.delivered_qty, 0))::NUMERIC(15,3) AS on_hand
FROM (
    SELECT ip_id, product_id, SUM(quantity)::NUMERIC(15,3) AS produced_qty
    FROM production_output
    GROUP BY ip_id, product_id
) p
LEFT JOIN (
    SELECT o.ip_id, oi.product_id,
           SUM(oi.quantity)::NUMERIC(15,3) AS delivered_qty
    FROM order_items oi
    JOIN orders o ON o.id = oi.order_id
    WHERE o.status = 'COMPLETED'
      AND oi.product_id IS NOT NULL
    GROUP BY o.ip_id, oi.product_id
) d USING (ip_id, product_id);

COMMENT ON VIEW v_finished_goods_stock IS
'G-C2: finished-goods on-hand per (IP, product) = SUM(production_output.quantity) '
'- SUM(order_items.quantity on COMPLETED orders). Quantity only.';
