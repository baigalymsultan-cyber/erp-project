"""Finance domain services — ledger operations, P&L, balances (§16)."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from app.models.finance import Operation, Transfer
from app.models.reference import OperationType
from app.schemas import OperationCreate, PayrollPeriodCreate
from app.services.common import _q2


def reverse_operation(
    session: Session,
    operation_id: int,
    comment: str | None = None,
) -> dict[str, object]:
    """Reversal (сторно) — §16: corrections never DELETE, history is kept.

    The original row stays in the ledger with status='REVERSED'
    (chk_operations_status); the exclusion from totals happens in the
    same place as everywhere else — the status='POSTED' filter, shared by
    v_ip_financial_summary and pnl(). Reversing twice or reversing a
    reversal is rejected: a REVERSED row must not silently disappear
    from the audit trail.
    """
    op = session.get(Operation, operation_id)
    if op is None:
        raise ValueError(f"operation {operation_id} does not exist")
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
            "only POSTED operations can be reversed (§16)"
        )
    op.status = "REVERSED"
    if comment:
        op.comment = comment
    session.flush()
    return {"id": op.id, "status": op.status}


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
    """
    # kind lives on operation_types (TD-005, chk_operation_types_kind):
    # INCOME/EXPENSE/TRANSFER; direction is only IN/OUT. Mirrors the
    # v_ip_financial_summary view (§63): ot.kind = 'INCOME' / 'EXPENSE',
    # status = 'POSTED' (§16).
    # GAP-001: direct-cost codes need no owner judgment (see docstring).
    direct_codes = ("EXPENSE_MATERIAL", "EXPENSE_KASPI_FEE")
    stmt = (
        select(
            func.coalesce(
                func.sum(
                    case(
                        (OperationType.kind == "INCOME", Operation.amount),
                        else_=0,
                    )
                ),
                0,
            ).label("total_income"),
            # §27: expense amounts are stored negative; the view negates
            # them back so total_expense is reported as a positive value.
            func.coalesce(
                func.sum(
                    case(
                        (OperationType.kind == "EXPENSE", -Operation.amount),
                        else_=0,
                    )
                ),
                0,
            ).label("total_expense"),
            # GAP-001 layers: direct cost = objectively sales-linked codes.
            func.coalesce(
                func.sum(
                    case(
                        (
                            and_(
                                OperationType.kind == "EXPENSE",
                                OperationType.code.in_(direct_codes),
                            ),
                            -Operation.amount,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("direct_cost"),
            # GAP-001: everything else in EXPENSE is unallocated operating
            # expense (OQ-010 allocation stays owner decision).
            func.coalesce(
                func.sum(
                    case(
                        (
                            and_(
                                OperationType.kind == "EXPENSE",
                                ~OperationType.code.in_(direct_codes),
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
        .where(OperationType.kind.in_(("INCOME", "EXPENSE")))
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
