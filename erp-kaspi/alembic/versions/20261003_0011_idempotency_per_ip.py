"""Per-IP idempotency indexes (P1-1/M-1 security fix)

Revision ID: 0011_idempotency_per_ip
Revises: 0010_audit_coverage_extension
Create Date: 2026-10-03

P1-1/M-1: the idempotency replay lookup in POST /operations and
POST /payroll/work-entries is scoped to the request's IP (cross-IP
information leak fix). The unique indexes must match: a global
(import_fingerprint) index would reject a legitimate per-IP reuse of the
same key with a unique violation instead of creating the per-IP row.

This revision drops the global partial unique indexes and recreates them
as (ip_id, import_fingerprint) partial unique indexes:

- uq_operations_import_fingerprint ON operations (ip_id, import_fingerprint)
  WHERE import_fingerprint IS NOT NULL
- uq_work_entries_import_fingerprint ON work_entries (ip_id, import_fingerprint)
  WHERE import_fingerprint IS NOT NULL

No data is changed. Existing fingerprints are per-batch/per-import and do
not collide across IPs in practice; the index rebuild is safe.

database/schema.sql stays the source of truth (ADR-011).
"""

from __future__ import annotations

from alembic import op

revision = "0011_idempotency_per_ip"
down_revision = "0010_audit_coverage_extension"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_operations_import_fingerprint;")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_operations_import_fingerprint
            ON operations (ip_id, import_fingerprint)
            WHERE import_fingerprint IS NOT NULL;
        """
    )
    op.execute("DROP INDEX IF EXISTS uq_work_entries_import_fingerprint;")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_work_entries_import_fingerprint
            ON work_entries (ip_id, import_fingerprint)
            WHERE import_fingerprint IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_operations_import_fingerprint;")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_operations_import_fingerprint
            ON operations (import_fingerprint)
            WHERE import_fingerprint IS NOT NULL;
        """
    )
    op.execute("DROP INDEX IF EXISTS uq_work_entries_import_fingerprint;")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_work_entries_import_fingerprint
            ON work_entries (import_fingerprint)
            WHERE import_fingerprint IS NOT NULL;
        """
    )
