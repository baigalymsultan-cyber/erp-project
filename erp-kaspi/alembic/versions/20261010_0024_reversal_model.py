"""Compensating-operation reversal model (ADR-016 PROPOSED).

Revision ID: 0024_reversal_model
Revises: 0023_production_output
Create Date: 2026-10-10

Implements the compensating-operation reversal model from
docs/REVERSAL_DECISIONS_2026-10-10.md (Q1-Q7) and ADR-016 (PROPOSED):

Q1: Reversal = new POSTED compensating operation, original never mutates.
Q2: New kinds REVERSAL_IN (IN only) and REVERSAL_OUT (OUT only);
    affects_pnl mirrors the original (TRUE).
Q3: Link via operations.reverses_operation_id; related_operation_id untouched.
Q4: Partial reversal allowed; DB trigger enforces
    SUM(compensations) <= original.amount; trigger asserts
    transaction_isolation = 'read committed'.
Q5: Mandatory reversal_reason; CHECK ((reverses_operation_id IS NULL) =
    (reversal_reason IS NULL)).
Q6: ACCOUNTANT privilege sufficient (no RBAC change).
Q7: v_ip_financial_summary nets reversal pairs (full reversal -> 0).

Also:
- Closes the POSTED -> REVERSED status edge in
  fn_protect_posted_ledger_operation (the status-flip model is superseded;
  legacy REVERSED rows are grandfathered, the status value stays valid).
- posting_locks table + BEFORE INSERT trigger rejecting inserts into
  locked periods (month granularity).

Idempotency: reversals use a deterministic external_id
('reversal:{original_id}:{key}') covered by the existing
uq_operations_ip_type_external partial unique index; the service uses
ON CONFLICT DO NOTHING.

All DDL is idempotent (IF NOT EXISTS / CREATE OR REPLACE / ON CONFLICT
DO NOTHING) so baseline + upgrade agree. Fresh databases get the same
DDL from database/schema.sql.
"""

from __future__ import annotations

from alembic import op

revision = "0024_reversal_model"
down_revision = "0023_production_output"
branch_labels = None
depends_on = None


_DDL = """
-- ---------------------------------------------------------------------
-- Q2: new reversal kinds in operation_types
-- ---------------------------------------------------------------------
ALTER TABLE operation_types DROP CONSTRAINT IF EXISTS chk_operation_types_kind;
ALTER TABLE operation_types ADD CONSTRAINT chk_operation_types_kind
    CHECK (kind IN ('INCOME', 'EXPENSE', 'TRANSFER', 'REVERSAL_IN', 'REVERSAL_OUT'));

ALTER TABLE operation_types DROP CONSTRAINT IF EXISTS chk_operation_types_dir_kind;
ALTER TABLE operation_types ADD CONSTRAINT chk_operation_types_dir_kind
    CHECK (
        (kind = 'INCOME'      AND direction = 'IN')  OR
        (kind = 'EXPENSE'     AND direction = 'OUT') OR
        (kind = 'REVERSAL_IN' AND direction = 'IN')  OR
        (kind = 'REVERSAL_OUT' AND direction = 'OUT') OR
        (kind = 'TRANSFER')
    );

-- chk_operation_types_pnl needs no change: REVERSAL_* <> 'TRANSFER',
-- so affects_pnl=TRUE is allowed. We set affects_pnl=TRUE (Q2: mirrors original).

INSERT INTO operation_types (code, name, direction, kind, affects_pnl, is_system) VALUES
    ('REVERSAL_IN',  'Сторно (приход)', 'IN',  'REVERSAL_IN',  TRUE, TRUE),
    ('REVERSAL_OUT', 'Сторно (расход)', 'OUT', 'REVERSAL_OUT', TRUE, TRUE)
ON CONFLICT (code) DO NOTHING;

-- ---------------------------------------------------------------------
-- Q3/Q5: reverses_operation_id + reversal_reason on operations
-- ---------------------------------------------------------------------
ALTER TABLE operations
    ADD COLUMN IF NOT EXISTS reverses_operation_id BIGINT NULL
        REFERENCES operations (id) ON DELETE RESTRICT;
ALTER TABLE operations
    ADD COLUMN IF NOT EXISTS reversal_reason TEXT NULL;

ALTER TABLE operations DROP CONSTRAINT IF EXISTS chk_operations_reversal_link;
ALTER TABLE operations ADD CONSTRAINT chk_operations_reversal_link CHECK (
    (reverses_operation_id IS NULL) = (reversal_reason IS NULL)
);

ALTER TABLE operations DROP CONSTRAINT IF EXISTS chk_operations_reversal_no_self;
ALTER TABLE operations ADD CONSTRAINT chk_operations_reversal_no_self CHECK (
    reverses_operation_id IS NULL OR reverses_operation_id <> id
);

CREATE INDEX IF NOT EXISTS ix_operations_reverses_op
    ON operations (reverses_operation_id)
    WHERE reverses_operation_id IS NOT NULL;

COMMENT ON COLUMN operations.reverses_operation_id IS
    'ADR-016: the original operation this compensating reversal offsets. '
    'NULL for non-reversal operations. related_operation_id is untouched (TD-018).';
COMMENT ON COLUMN operations.reversal_reason IS
    'ADR-016 Q5: mandatory business reason for the reversal. '
    'NOT NULL when reverses_operation_id is set; NULL otherwise.';

-- ---------------------------------------------------------------------
-- Q4: partial-reversal cap trigger
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_reversal_cap_check()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_isolation       TEXT;
    v_original_amount NUMERIC(15,2);
    v_total_reversed  NUMERIC(15,2);
BEGIN
    IF NEW.reverses_operation_id IS NULL THEN
        RETURN NEW;
    END IF;

    -- Idempotent retry: if a reversal with the same external_id already
    -- exists, skip the cap check; ON CONFLICT DO NOTHING will handle it.
    IF NEW.external_id IS NOT NULL THEN
        PERFORM 1 FROM operations
        WHERE ip_id = NEW.ip_id
          AND type_id = NEW.type_id
          AND external_id = NEW.external_id;
        IF FOUND THEN
            RETURN NEW;
        END IF;
    END IF;

    -- Q4: the cap check must see a fresh snapshot; under REPEATABLE READ
    -- or SERIALIZABLE the SUM could miss a concurrent compensation.
    SELECT current_setting('transaction_isolation') INTO v_isolation;
    IF v_isolation IS DISTINCT FROM 'read committed' THEN
        RAISE EXCEPTION
            'reversal cap check requires transaction isolation read committed, got % (ADR-016 Q4)',
            v_isolation
            USING ERRCODE = 'check_violation';
    END IF;

    -- Lock the original row: serializes concurrent reversals of the same
    -- operation so the cap cannot be bypassed by a race.
    SELECT amount INTO v_original_amount
    FROM operations
    WHERE id = NEW.reverses_operation_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION
            'reversal: original operation % does not exist (ADR-016)',
            NEW.reverses_operation_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;

    -- Sum of existing compensations (absolute values). NEW row is not yet
    -- visible to other transactions; FOR UPDATE above serializes writers.
    SELECT COALESCE(SUM(ABS(amount)), 0) INTO v_total_reversed
    FROM operations
    WHERE reverses_operation_id = NEW.reverses_operation_id;

    IF v_total_reversed + ABS(NEW.amount) > ABS(v_original_amount) THEN
        RAISE EXCEPTION
            'reversal cap exceeded for operation %: '
            'already reversed %, new %, original % (ADR-016 Q4)',
            NEW.reverses_operation_id, v_total_reversed, ABS(NEW.amount), ABS(v_original_amount)
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION fn_reversal_cap_check() IS
    'ADR-016 Q4: partial-reversal cap. SUM(ABS(compensations)) <= ABS(original.amount). '
    'Asserts read committed isolation and takes FOR UPDATE on the original row.';

DROP TRIGGER IF EXISTS trg_operations_reversal_cap ON operations;
CREATE TRIGGER trg_operations_reversal_cap
    BEFORE INSERT ON operations
    FOR EACH ROW
    EXECUTE FUNCTION fn_reversal_cap_check();

-- ---------------------------------------------------------------------
-- Posting locks (month granularity)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS posting_locks (
    id           BIGSERIAL     PRIMARY KEY,
    ip_id        BIGINT        NOT NULL REFERENCES ip_entities (id) ON DELETE RESTRICT,
    period_start DATE          NOT NULL,
    period_end   DATE          NOT NULL,
    locked_at    TIMESTAMPTZ   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    locked_by    BIGINT        NULL REFERENCES users (id) ON DELETE SET NULL,

    CONSTRAINT chk_posting_locks_period CHECK (period_start <= period_end),
    CONSTRAINT uq_posting_locks_ip_period UNIQUE (ip_id, period_start, period_end)
);

COMMENT ON TABLE posting_locks IS
    'ADR-016: month-granularity posting locks. New operations (including '
    'reversals) cannot be posted into a locked period.';

CREATE OR REPLACE FUNCTION fn_posting_lock_check()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM posting_locks
        WHERE ip_id = NEW.ip_id
          AND NEW.operation_date >= period_start
          AND NEW.operation_date <= period_end
    ) THEN
        RAISE EXCEPTION
            'posting locked for ip % period containing % (ADR-016)',
            NEW.ip_id, NEW.operation_date
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION fn_posting_lock_check() IS
    'ADR-016: rejects INSERTs into locked posting periods.';

DROP TRIGGER IF EXISTS trg_operations_posting_lock ON operations;
CREATE TRIGGER trg_operations_posting_lock
    BEFORE INSERT ON operations
    FOR EACH ROW
    EXECUTE FUNCTION fn_posting_lock_check();

-- ---------------------------------------------------------------------
-- Close the POSTED -> REVERSED status edge (status-flip model superseded).
-- Legacy REVERSED rows are grandfathered; the status value stays valid
-- in chk_operations_status.
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
                'invalid operation status transition from % to % (FIN-OPS-001).',
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
       NEW.reverses_operation_id IS DISTINCT FROM OLD.reverses_operation_id OR
       NEW.reversal_reason IS DISTINCT FROM OLD.reversal_reason OR
       NEW.account_id IS DISTINCT FROM OLD.account_id OR
       NEW.import_fingerprint IS DISTINCT FROM OLD.import_fingerprint OR
       NEW.created_by IS DISTINCT FROM OLD.created_by OR
       NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION
            'operation % is % and its financial fields are immutable; '
            'use reversal / correction instead of UPDATE '
            '(FIN-OPS-001, FINANCE-OPS-001 §7, ADR-016).',
            OLD.id, OLD.status USING ERRCODE = 'check_violation';
    END IF;

    -- ADR-016: the POSTED -> REVERSED edge is closed. Reversal is a
    -- compensating POSTED operation, never a status flip (R1/R4).
    IF NEW.status IS DISTINCT FROM OLD.status THEN
        RAISE EXCEPTION
            'invalid operation status transition from % to % (FIN-OPS-001, ADR-016).',
            OLD.status, NEW.status USING ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION fn_protect_posted_ledger_operation() IS
    'FIN-OPS-001, ADR-016: non-transfer operations are immutable after DRAFT; '
    'only DRAFT->POSTED is allowed. The POSTED->REVERSED edge was closed by '
    'ADR-016 (compensating-operation model). Transfer legs are guarded by '
    'fn_protect_posted_operation().';

-- ---------------------------------------------------------------------
-- Q7: v_ip_financial_summary nets reversal pairs.
-- REVERSAL_OUT (negative amount) offsets INCOME; REVERSAL_IN (positive
-- amount, negated) offsets EXPENSE. Full reversal -> net 0 for the pair.
-- net_profit already uses affects_pnl (TRUE for REVERSAL_*), unchanged.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_ip_financial_summary AS
SELECT
    ip.id                                       AS ip_id,
    ip.name                                     AS ip_name,
    ip.bin                                      AS ip_bin,
    date_trunc('month', COALESCE(o.accrual_date, o.operation_date))::DATE AS period_month,
    -- income / expense (P&L only, transfers excluded; accrual basis).
    -- ADR-016 Q7: reversal pairs net — REVERSAL_OUT offsets INCOME,
    -- REVERSAL_IN offsets EXPENSE.
    coalesce(sum(o.amount) FILTER (WHERE ot.kind IN ('INCOME', 'REVERSAL_OUT')
        AND o.accrual_date IS NOT NULL),  0)::NUMERIC(15,2) AS total_income,
    coalesce(sum(-o.amount) FILTER (WHERE ot.kind IN ('EXPENSE', 'REVERSAL_IN')
        AND o.accrual_date IS NOT NULL), 0)::NUMERIC(15,2) AS total_expense,
    coalesce(sum(o.amount) FILTER (WHERE ot.affects_pnl
        AND o.accrual_date IS NOT NULL), 0)::NUMERIC(15,2) AS net_profit,
    -- revenue breakdown by source (gross; reversals have their own kinds)
    coalesce(sum(o.amount) FILTER (WHERE ot.code = 'SALE_KASPI_SHOP'
        AND o.accrual_date IS NOT NULL), 0)::NUMERIC(15,2) AS revenue_kaspi_shop,
    coalesce(sum(o.amount) FILTER (WHERE ot.code = 'SALE_KASPI_PAY'
        AND o.accrual_date IS NOT NULL),  0)::NUMERIC(15,2) AS revenue_kaspi_pay,
    -- expense breakdown (gross)
    coalesce(sum(-o.amount) FILTER (WHERE ot.code = 'EXPENSE_MATERIAL'
        AND o.accrual_date IS NOT NULL),  0)::NUMERIC(15,2) AS expense_material,
    coalesce(sum(-o.amount) FILTER (WHERE ot.code = 'EXPENSE_KASPI_FEE'
        AND o.accrual_date IS NOT NULL), 0)::NUMERIC(15,2) AS expense_kaspi_fee,
    coalesce(sum(-o.amount) FILTER (WHERE ot.code = 'SALARY_PAYMENT'
        AND o.accrual_date IS NOT NULL),    0)::NUMERIC(15,2) AS expense_salary,
    -- transfers: reported, never counted as income or expense (§28, §76)
    coalesce(sum(o.amount) FILTER (WHERE ot.code = 'TRANSFER_IN'),
        0)::NUMERIC(15,2) AS transfers_in,
    coalesce(sum(-o.amount) FILTER (WHERE ot.code = 'TRANSFER_OUT'),
        0)::NUMERIC(15,2) AS transfers_out,
    count(*) AS operations_count
FROM ip_entities ip
JOIN operations      o  ON o.ip_id = ip.id AND o.status = 'POSTED'
JOIN operation_types ot ON ot.id = o.type_id
GROUP BY ip.id, ip.name, ip.bin, date_trunc('month', COALESCE(o.accrual_date, o.operation_date));

COMMENT ON VIEW v_ip_financial_summary IS
    'MANDATORY VIEW (§63), ADR-016 Q7. Monthly financial summary per ИП. '
    'Transfers excluded from P&L (§28, §76). Reversal pairs net: full reversal -> 0 '
    '(REVERSAL_OUT offsets INCOME, REVERSAL_IN offsets EXPENSE).';
"""


def upgrade() -> None:
    op.execute(_DDL)


def downgrade() -> None:
    # ADR-016 rollback: DDL rollback is safe until the first production
    # compensation; after that, forward-fix only (no financial data deleted).
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_operations_posting_lock ON operations;
        DROP FUNCTION IF EXISTS fn_posting_lock_check();
        DROP TABLE IF EXISTS posting_locks;

        DROP TRIGGER IF EXISTS trg_operations_reversal_cap ON operations;
        DROP FUNCTION IF EXISTS fn_reversal_cap_check();

        ALTER TABLE operations DROP CONSTRAINT IF EXISTS chk_operations_reversal_no_self;
        ALTER TABLE operations DROP CONSTRAINT IF EXISTS chk_operations_reversal_link;
        DROP INDEX IF EXISTS ix_operations_reverses_op;
        ALTER TABLE operations DROP COLUMN IF EXISTS reversal_reason;
        ALTER TABLE operations DROP COLUMN IF EXISTS reverses_operation_id;

        DELETE FROM operation_types WHERE code IN ('REVERSAL_IN', 'REVERSAL_OUT');

        ALTER TABLE operation_types DROP CONSTRAINT IF EXISTS chk_operation_types_dir_kind;
        ALTER TABLE operation_types ADD CONSTRAINT chk_operation_types_dir_kind
            CHECK (
                (kind = 'INCOME'  AND direction = 'IN')  OR
                (kind = 'EXPENSE' AND direction = 'OUT') OR
                (kind = 'TRANSFER')
            );

        ALTER TABLE operation_types DROP CONSTRAINT IF EXISTS chk_operation_types_kind;
        ALTER TABLE operation_types ADD CONSTRAINT chk_operation_types_kind
            CHECK (kind IN ('INCOME', 'EXPENSE', 'TRANSFER'));
        """
    )
    # NOTE: fn_protect_posted_ledger_operation is NOT restored here —
    # reopening the POSTED->REVERSED edge requires a deliberate decision.
    # The v_ip_financial_summary view is left as-is (CREATE OR REPLACE in
    # schema.sql baseline will restore it on fresh builds).
