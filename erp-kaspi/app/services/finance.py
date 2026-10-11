"""Finance domain services — ledger operations, P&L, balances (§16)."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import and_, case, func, or_, select, text
from sqlalchemy.orm import Session

from app.models.finance import Operation, Transfer
from app.models.reference import OperationType
from app.schemas import OperationCreate, PayrollPeriodCreate
from app.services.common import _q2


def reverse_operation(
    session: Session,
    operation_id: int,
    reversal_reason: str,
    amount: Decimal | None = None,
    idempotency_key: str | None = None,
) -> dict[str, object]:
    """Reversal (сторно) — ADR-016: compensating operation, never status-flip.

    Creates a NEW POSTED operation with the inverse financial effect.
    The original operation is never mutated: no status change, no comment
    overwrite (Q1, Q3, Q5).

    - ``reversal_reason`` (Q5): mandatory business reason, stored on the
      reversal row (not on the original).
    - ``amount`` (Q4): partial reversal magnitude (positive Decimal).
      None = full reversal. The DB trigger enforces
      SUM(compensations) <= original.amount; concurrent reversals are
      serialized via FOR UPDATE on the original row.
    - ``idempotency_key``: client-provided key for safe retries. The
      reversal gets a deterministic external_id
      (``reversal:{original_id}:{key}``); a retry with the same key hits
      ON CONFLICT DO NOTHING and returns the existing reversal.

    Transfer legs cannot be reversed independently (existing check kept).
    Reversal operation_date = today (no backdating, ADR-016).
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    if not reversal_reason or not reversal_reason.strip():
        raise ValueError("reversal_reason is required (ADR-016 Q5)")

    op = session.get(Operation, operation_id)
    if op is None:
        raise ValueError(f"operation {operation_id} does not exist")

    # Transfer legs cannot be reversed independently (kept from §28).
    transfer_id = session.scalar(
        select(Transfer.id).where(
            or_(
                Transfer.outbound_operation_id == operation_id,
                Transfer.inbound_operation_id == operation_id,
            )
        )
    )
    if transfer_id is not None:
        raise ValueError("a transfer leg cannot be reversed independently")

    if op.status != "POSTED":
        raise ValueError(
            f"operation {operation_id} has status {op.status}; "
            "only POSTED operations can be reversed (ADR-016)"
        )

    # Determine the reversal kind: inverse direction of the original.
    # INCOME (IN) -> REVERSAL_OUT (OUT); EXPENSE (OUT) -> REVERSAL_IN (IN).
    # REVERSAL_IN (IN) -> REVERSAL_OUT (OUT); REVERSAL_OUT (OUT) -> REVERSAL_IN (IN).
    original_kind = session.scalar(
        select(OperationType.kind).where(OperationType.id == op.type_id)
    )
    if original_kind in ("INCOME", "REVERSAL_IN"):
        reversal_code = "REVERSAL_OUT"  # direction OUT, negative amount
        reversal_sign = -1
    elif original_kind in ("EXPENSE", "REVERSAL_OUT"):
        reversal_code = "REVERSAL_IN"  # direction IN, positive amount
        reversal_sign = 1
    else:
        raise ValueError(
            f"operation {operation_id} has kind {original_kind}; "
            "only INCOME/EXPENSE (and their reversals) can be reversed"
        )

    reversal_type_id = session.scalar(
        select(OperationType.id).where(OperationType.code == reversal_code)
    )
    if reversal_type_id is None:
        raise ValueError(
            f"operation type {reversal_code} does not exist "
            "(migration 0024_reversal_model not applied?)"
        )

    # Amount: full reversal defaults to the original magnitude.
    original_magnitude = abs(op.amount)
    if amount is None:
        reversal_magnitude = original_magnitude
    else:
        reversal_magnitude = Decimal(amount)
        if reversal_magnitude <= 0:
            raise ValueError("reversal amount must be positive")
        if reversal_magnitude > original_magnitude:
            raise ValueError(
                f"reversal amount {reversal_magnitude} exceeds "
                f"original magnitude {original_magnitude}"
            )

    reversal_amount = _q2(reversal_sign * reversal_magnitude)
    if reversal_amount == 0:
        raise ValueError("reversal amount cannot be zero (§27)")

    # Idempotency: deterministic external_id. A retry with the same key
    # collides on uq_operations_ip_type_external -> ON CONFLICT DO NOTHING.
    key_part = (idempotency_key or "").strip() or (
        "full" if amount is None else f"partial-{reversal_magnitude}"
    )
    # Sanitize: external_id is VARCHAR(100).
    safe_key = "".join(c for c in key_part if c.isalnum() or c in "-_.")[:40]
    external_id = f"reversal:{operation_id}:{safe_key}"[:100]

    today = session.scalar(select(func.current_date()))

    stmt = (
        pg_insert(Operation)
        .values(
            ip_id=op.ip_id,
            type_id=reversal_type_id,
            category_id=None,  # reversals net in totals; no category breakdown
            counterparty_id=op.counterparty_id,
            amount=reversal_amount,
            operation_date=today,  # ADR-016: no backdating
            accrual_date=today,
            external_id=external_id,
            reverses_operation_id=operation_id,
            reversal_reason=reversal_reason.strip(),
            account_id=op.account_id,
            status="POSTED",
            comment=None,
        )
        .on_conflict_do_nothing(
            index_elements=["ip_id", "type_id", "external_id"],
            # uq_operations_ip_type_external is partial (WHERE external_id IS NOT NULL);
            # index_where must match for ON CONFLICT to use it.
            index_where=text("external_id IS NOT NULL"),
        )
    )
    # ON CONFLICT DO NOTHING: whether the row was inserted or the conflict
    # was ignored (idempotent retry), the SELECT below finds the reversal
    # by its deterministic external_id.
    session.execute(stmt)
    reversal_id = session.scalar(
        select(Operation.id).where(
            Operation.ip_id == op.ip_id,
            Operation.type_id == reversal_type_id,
            Operation.external_id == external_id,
        )
    )
    if reversal_id is None:
        # Should not happen, but fail loudly rather than silently.
        raise RuntimeError(
            "reversal insert failed and existing row not found "
            "(concurrent delete?)"
        )

    session.flush()
    return {
        "id": reversal_id,
        "status": "POSTED",
        "reverses_operation_id": operation_id,
        "amount": str(reversal_amount),
    }


def add_operation(
    session: Session,
    data: OperationCreate,
    *,
    status: str | None = None,
    account_id: int | None = None,
) -> int:
    """Append a ledger operation (§16 — no UPDATE/DELETE).

    ``status``/``account_id`` ride on the INSERT (a POSTED row is
    immutable — fn_protect_posted_ledger_operation blocks post-flush
    assignment). They are Python-level only: OperationCreate (the public
    API schema) is unchanged.
    """
    # The operations table has no direction column (TD-005): the sign rule is
    # enforced by triggers against operation_types.direction. The requested
    # direction is checked here so that a wrong request fails fast (§59).
    operation_type = session.execute(
        select(OperationType.direction, OperationType.kind).where(OperationType.id == data.type_id)
    ).one_or_none()
    if operation_type is None:
        raise ValueError(f"operation type {data.type_id} does not exist")
    type_direction, type_kind = operation_type
    if type_kind == "TRANSFER":
        raise ValueError(
            "transfer ledger legs must be created through the atomic transfer endpoint"
        )
    if type_direction != data.direction.value:
        raise ValueError(
            f"§27 direction mismatch: type {data.type_id} is {type_direction}, "
            f"request said {data.direction.value}"
        )
    # P2-4: validate the amount sign here so a wrong sign fails fast with 422
    # instead of hitting the DB trigger fn_check_operation_sign (§27) → 500.
    if data.direction.value == "OUT" and data.amount >= 0:
        raise ValueError("§27: OUT operations require a negative amount")
    if data.direction.value == "IN" and data.amount <= 0:
        raise ValueError("§27: IN operations require a positive amount")
    op = Operation(
        ip_id=data.ip_id,
        type_id=data.type_id,
        category_id=data.category_id,
        amount=data.amount,
        operation_date=data.operation_date,
        # Default accrual_date to operation_date (cash basis) if not provided.
        # Explicit None means pure payment/settlement (no P&L effect) — the
        # schema contract promises this, so distinguish omitted from
        # explicit-null via model_fields_set (D1/GAP-004). Pydantic
        # collapses both to None otherwise.
        accrual_date=(
            None
            if ("accrual_date" in data.model_fields_set and data.accrual_date is None)
            else (data.accrual_date if data.accrual_date is not None else data.operation_date)
        ),
        comment=data.comment,
        # §41/ADR-010 — external_id makes retries idempotent;
        # import_fingerprint deduplicates Excel rows (uq_operations_import_fingerprint)
        external_id=data.external_order_id,
        import_fingerprint=data.import_fingerprint,
    )
    # INSERT-time only: POSTED rows are immutable
    # (fn_protect_posted_ledger_operation), so status/account_id cannot be
    # assigned after flush. Bank statement posting needs both at INSERT.
    if status is not None:
        op.status = status
    if account_id is not None:
        op.account_id = account_id
    session.add(op)
    session.flush()
    return op.id


def pnl(
    session: Session,
    period: PayrollPeriodCreate | None = None,
    ip_ids: list[int] | None = None,
) -> dict[str, Decimal]:
    """Income / expense / net profit; transfers excluded (§28, §63, §76).

    A transfer is a movement between own accounts: it is never income and
    never expense. The v_ip_financial_summary view implements the same
    rule in SQL — the service mirrors it for the API layer.

    Only POSTED operations count (§16): the view filters
    o.status = 'POSTED', so the service must agree, otherwise the UI and
    the reports would disagree with the ledger after a reversal.

    GAP-001 layered P&L: returns the flat keys (total_income, total_expense,
    net_profit — unchanged, backward compatible) PLUS layered keys:
    revenue / direct_cost / gross_margin / operating_expenses /
    operating_result / other_income / other_expenses / net_result.

    Direct-cost classification needs no owner judgment: EXPENSE_MATERIAL
    scales with production (order_direct_cost() already treats it as
    direct per §37/§49) and EXPENSE_KASPI_FEE is a per-sale commission
    (zero sales ⇒ zero fee). All other EXPENSE codes are reported as
    UNALLOCATED operating expenses — overhead ALLOCATION stays owner
    decision (OQ-010); the layer STRUCTURE does not. SALARY_PAYMENT mixes
    piecework (direct) with base/bonus (overhead) in one lump op (TD-020
    frozen), so it is surfaced as informational salary_unclassified rather
    than misclassified. other_income/other_expenses are 0: no codes exist
    for them (no depreciation/FX codes in the seeds); they are structural
    placeholders, not invented accounting.

    ADR-016 Q7: reversal pairs net. REVERSAL_OUT (negative amount) offsets
    INCOME; REVERSAL_IN (positive amount, negated) offsets EXPENSE. A full
    reversal yields net 0 for the pair. The GAP-001 layers classify a
    reversal by its ORIGINAL operation's type code (via reverses_operation_id)
    so direct_cost / operating_expenses also net correctly.
    """
    # kind lives on operation_types (TD-005, chk_operation_types_kind):
    # INCOME/EXPENSE/TRANSFER + ADR-016 REVERSAL_IN/REVERSAL_OUT.
    # Mirrors the v_ip_financial_summary view (§63, ADR-016 Q7).
    from sqlalchemy.orm import aliased

    OriginalOp = aliased(Operation)
    OriginalType = aliased(OperationType)

    # GAP-001: direct-cost codes need no owner judgment (see docstring).
    direct_codes = ("EXPENSE_MATERIAL", "EXPENSE_KASPI_FEE")

    # ADR-016 Q7: a reversal nets against the same P&L bucket as its
    # original. Classify by the original's type when this row is a reversal.
    def _is_income_kind():
        return or_(
            OperationType.kind == "INCOME",
            and_(
                OperationType.kind == "REVERSAL_OUT",
                OriginalType.kind == "INCOME",
            ),
        )

    def _is_expense_kind():
        return or_(
            OperationType.kind == "EXPENSE",
            and_(
                OperationType.kind == "REVERSAL_IN",
                OriginalType.kind == "EXPENSE",
            ),
        )

    def _is_direct_cost():
        return or_(
            and_(
                OperationType.kind == "EXPENSE",
                OperationType.code.in_(direct_codes),
            ),
            and_(
                OperationType.kind == "REVERSAL_IN",
                OriginalType.code.in_(direct_codes),
            ),
        )

    stmt = (
        select(
            func.coalesce(
                func.sum(
                    case(
                        (_is_income_kind(), Operation.amount),
                        else_=0,
                    )
                ),
                0,
            ).label("total_income"),
            # §27: expense amounts are stored negative; the view negates
            # them back so total_expense is reported as a positive value.
            # ADR-016 Q7: REVERSAL_IN nets against EXPENSE.
            func.coalesce(
                func.sum(
                    case(
                        (_is_expense_kind(), -Operation.amount),
                        else_=0,
                    )
                ),
                0,
            ).label("total_expense"),
            # GAP-001 layers: direct cost = objectively sales-linked codes.
            # ADR-016 Q7: reversals classified by original's code.
            func.coalesce(
                func.sum(
                    case(
                        (_is_direct_cost(), -Operation.amount),
                        else_=0,
                    )
                ),
                0,
            ).label("direct_cost"),
            # GAP-001: everything else in EXPENSE is unallocated operating
            # expense (OQ-010 allocation stays owner decision).
            # ADR-016 Q7: REVERSAL_IN not matching direct codes nets here.
            func.coalesce(
                func.sum(
                    case(
                        (
                            and_(
                                _is_expense_kind(),
                                ~_is_direct_cost(),
                            ),
                            -Operation.amount,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("operating_expenses"),
            # GAP-001 informational: SALARY_PAYMENT cannot be classified
            # (TD-020 frozen lump sum) — surfaced, not misclassified.
            func.coalesce(
                func.sum(
                    case(
                        (OperationType.code == "SALARY_PAYMENT", -Operation.amount),
                        else_=0,
                    )
                ),
                0,
            ).label("salary_unclassified"),
        )
        .join(OperationType, OperationType.id == Operation.type_id)
        # ADR-016 Q7: join original for reversal classification.
        .outerjoin(OriginalOp, OriginalOp.id == Operation.reverses_operation_id)
        .outerjoin(OriginalType, OriginalType.id == OriginalOp.type_id)
        .where(
            OperationType.kind.in_(("INCOME", "EXPENSE", "REVERSAL_IN", "REVERSAL_OUT"))
        )
        .where(Operation.status == "POSTED")
    )
    if period is not None:
        # P&L uses accrual_date ONLY (accrual basis). Historical operations
        # were backfilled with accrual_date = operation_date (migration 0015).
        stmt = stmt.where(Operation.accrual_date >= period.period_start).where(
            Operation.accrual_date <= period.period_end
        )
    # Pure payments/settlements (accrual_date IS NULL) never hit P&L
    # (prevents double-counting) — with or without a period. Mirrors
    # v_ip_financial_summary, which filters accrual_date IS NOT NULL
    # unconditionally (GAP-004).
    stmt = stmt.where(Operation.accrual_date.is_not(None))
    if ip_ids is not None:
        stmt = stmt.where(Operation.ip_id.in_(ip_ids))
    income, expense, direct, operating, salary_unclassified = session.execute(stmt).one()
    income, expense = _q2(Decimal(income)), _q2(Decimal(expense))
    direct, operating = _q2(Decimal(direct)), _q2(Decimal(operating))
    salary_unclassified = _q2(Decimal(salary_unclassified))
    # GAP-001 derived layers. Invariants (asserted in tests):
    #   direct + operating == total_expense
    #   net_result == net_profit
    gross_margin = _q2(income - direct)
    operating_result = _q2(gross_margin - operating)
    return {
        "total_income": income,
        "total_expense": expense,
        "net_profit": _q2(income - expense),
        # GAP-001 layered keys (7-layer P&L; overhead allocation = OQ-010).
        "revenue": income,
        "direct_cost": direct,
        "gross_margin": gross_margin,
        "operating_expenses": operating,
        "operating_result": operating_result,
        "salary_unclassified": salary_unclassified,
        # No codes exist for these (no depreciation/FX in seeds) — structural
        # placeholders, always 0, not invented accounting.
        "other_income": Decimal("0.00"),
        "other_expenses": Decimal("0.00"),
        "net_result": _q2(operating_result),
    }


def counterparty_balances(
    session: Session,
    ip_ids: list[int] | None = None,
) -> list[dict[str, object]]:
    """AR/AP Tier 1: net position per (IP, counterparty) — GAP-003.

    Reads v_counterparty_balances view:
    - total_accrued: SUM where accrual_date IS NOT NULL (economic events)
    - total_settled: SUM where accrual_date IS NULL (pure cash movements)
    - balance: accrued - settled (positive = AR, negative = AP)

    Only POSTED operations (view filters status='POSTED').
    IP isolation enforced via ip_ids filter.
    """
    from sqlalchemy import text

    query = """
        SELECT ip_id, counterparty_id, counterparty_name,
               total_accrued, total_settled, balance
        FROM v_counterparty_balances
        WHERE (:ip_ids IS NULL OR ip_id = ANY(:ip_ids))
        ORDER BY ip_id, counterparty_name
    """
    # Note: ANY with NULL array needs special handling
    if ip_ids is None:
        query = """
            SELECT ip_id, counterparty_id, counterparty_name,
                   total_accrued, total_settled, balance
            FROM v_counterparty_balances
            ORDER BY ip_id, counterparty_name
        """
        rows = session.execute(text(query)).mappings().all()
    else:
        rows = (
            session.execute(
                text("""
                SELECT ip_id, counterparty_id, counterparty_name,
                       total_accrued, total_settled, balance
                FROM v_counterparty_balances
                WHERE ip_id = ANY(:ip_ids)
                ORDER BY ip_id, counterparty_name
            """),
                {"ip_ids": ip_ids},
            )
            .mappings()
            .all()
        )

    return [dict(row) for row in rows]


def account_balances(
    session: Session,
    ip_ids: list[int] | None = None,
) -> list[dict[str, object]]:
    """GAP-002: Cash balance per account (treasury view).

    Sums POSTED operations by account_id. Positive = cash in account.
    Includes accounts with zero balance (LEFT JOIN).
    IP isolation enforced via ip_ids filter.
    """
    from sqlalchemy import text

    if ip_ids is None:
        query = """
            SELECT a.ip_id, a.id AS account_id, a.type, a.name,
                   COALESCE(SUM(o.amount), 0)::NUMERIC(15,2) AS balance
            FROM accounts a
            LEFT JOIN operations o ON o.account_id = a.id
                AND o.status = 'POSTED'
            WHERE a.is_active = TRUE
            GROUP BY a.ip_id, a.id, a.type, a.name
            ORDER BY a.ip_id, a.name
        """
        rows = session.execute(text(query)).mappings().all()
    else:
        rows = (
            session.execute(
                text("""
                SELECT a.ip_id, a.id AS account_id, a.type, a.name,
                       COALESCE(SUM(o.amount), 0)::NUMERIC(15,2) AS balance
                FROM accounts a
                LEFT JOIN operations o ON o.account_id = a.id
                    AND o.status = 'POSTED'
                WHERE a.is_active = TRUE AND a.ip_id = ANY(:ip_ids)
                GROUP BY a.ip_id, a.id, a.type, a.name
                ORDER BY a.ip_id, a.name
            """),
                {"ip_ids": ip_ids},
            )
            .mappings()
            .all()
        )

    return [dict(row) for row in rows]
