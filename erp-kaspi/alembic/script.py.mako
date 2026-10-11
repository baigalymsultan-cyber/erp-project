"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

MASTER PROMPT reference: §7 (Alembic), ADR-011 (schema.sql is the source of
truth — migrations are written by hand, autogenerate is disabled).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "raise NotImplementedError('upgrade() must be implemented')"}


def downgrade() -> None:
    ${downgrades if downgrades else "raise NotImplementedError('downgrade() must be implemented')"}
