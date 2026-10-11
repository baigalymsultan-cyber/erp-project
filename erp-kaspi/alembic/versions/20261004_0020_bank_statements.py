"""Bank statement foundation (G-BANK-2..5): raw statements + normalized lines.

Revision ID: 0020_bank_statements
Revises: 0019_drop_redundant_indexes
Create Date: 2026-10-04

Pipeline: Provider -> Adapter -> Raw Statement -> Normalized Bank Transaction
-> Dedup -> Classification -> Operation -> Reconciliation.

1. raw_bank_statements: the source file preserved verbatim (BYTEA) — provenance.
   uq_raw_bank_statements_file (ip_id, provider_code, file_hash) rejects
   re-import of the same file.
2. bank_statement_lines: normalized staging rows (external facts). Immutable
   after import via trg_bank_statement_lines_immutable; only the
   reconciliation workflow columns (match_status/matched_operation_id/
   matched_at/matched_by) are mutable. uq_bank_statement_lines_fingerprint
   (ip_id, line_fingerprint) deduplicates overlapping statements.
3. bank_fee_knp_codes: commission KNP reference, created EMPTY on purpose —
   commission KNP codes are facts from real statements (§94), never invented.
4. Seed EXPENSE_BANK_FEE (operation_types) — the commission posting target.

Fresh databases get the same DDL from database/schema.sql (SECTION 3b);
everything here is idempotent (IF NOT EXISTS / CREATE OR REPLACE /
ON CONFLICT DO NOTHING) so baseline + upgrade agree.

No invented bank APIs: file import only (MT940 text). Halyk live API
deferred; Kaspi Pay / Freedom format-specific adapters wait for real
official/sample exports.
"""

from __future__ import annotations

from alembic import op

revision = "0020_bank_statements"
down_revision = "0019_drop_redundant_indexes"
branch_labels = None
depends_on = None


_TABLES_DDL = """
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
'G-BANK-2: исходный файл банковской выписки целиком. Provenance для всех строк.';
COMMENT ON COLUMN raw_bank_statements.file_hash IS
'SHA-256 содержимого файла. Повторная загрузка того же файла отклоняется.';
COMMENT ON COLUMN raw_bank_statements.account_id IS
'Счёт ERP, сопоставленный до/во время импорта. NULL = сопоставление отложено.';

CREATE TABLE IF NOT EXISTS bank_statement_lines (
    id                  BIGSERIAL       PRIMARY KEY,
    ip_id               BIGINT          NOT NULL REFERENCES ip_entities (id) ON DELETE RESTRICT,
    statement_id        BIGINT          NOT NULL
        REFERENCES raw_bank_statements (id) ON DELETE RESTRICT,
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
'G-BANK-3: нормализованные строки выписки (staging). Внешний факт; неизменяемы.';
COMMENT ON COLUMN bank_statement_lines.line_fingerprint IS
'SHA-256 нормализованного ядра строки. Дедупликация per-IP.';
COMMENT ON COLUMN bank_statement_lines.provider_extra IS
'Provider-specific поля + audit trail сверки.';

CREATE TABLE IF NOT EXISTS bank_fee_knp_codes (
    knp_code    VARCHAR(10)     PRIMARY KEY,
    description TEXT            NULL,
    created_at  TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT chk_bank_fee_knp_codes_code
        CHECK (knp_code ~ '^[0-9]{1,10}$')
);

COMMENT ON TABLE bank_fee_knp_codes IS
'G-BANK-4: справочник КНП банковских комиссий. Только по реальным выпискам.';
"""

_TRIGGER_DDL = """
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
"""

_SEED_DDL = """
INSERT INTO operation_types (code, name, direction, kind, affects_pnl, is_system)
VALUES ('EXPENSE_BANK_FEE', 'Комиссия банка', 'OUT', 'EXPENSE', TRUE, TRUE)
ON CONFLICT (code) DO NOTHING;
"""


def upgrade() -> None:
    op.execute(_TABLES_DDL)
    op.execute(_TRIGGER_DDL)
    op.execute(_SEED_DDL)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_bank_statement_lines_immutable ON bank_statement_lines;")
    op.execute("DROP FUNCTION IF EXISTS fn_bank_statement_lines_immutable();")
    op.execute("DROP INDEX IF EXISTS uq_bank_statement_lines_confirmed_op;")
    op.execute("DROP TABLE IF EXISTS bank_statement_lines;")
    op.execute("DROP TABLE IF EXISTS raw_bank_statements;")
    op.execute("DROP TABLE IF EXISTS bank_fee_knp_codes;")
    # Seed rollback: refuse to drop the type while ledger rows reference it.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM operations o
                JOIN operation_types t ON t.id = o.type_id
                WHERE t.code = 'EXPENSE_BANK_FEE'
            ) THEN
                RAISE EXCEPTION
                    'downgrade 0020 refused: operations reference EXPENSE_BANK_FEE';
            END IF;
            DELETE FROM operation_types
            WHERE code = 'EXPENSE_BANK_FEE' AND is_system;
        END;
        $$;
        """
    )
