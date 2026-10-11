"""Business services — domain logic layer (MASTER PROMPT).

Split into domain modules; this package re-exports everything so
``from app.services import X`` keeps working (R1 mechanical split).
"""

from app.services.finance import (
    account_balances,
    add_operation,
    counterparty_balances,
    pnl,
    reverse_operation,
)
from app.services.kaspi import reconcile_payout, reconcile_payout_full
from app.services.orders import (
    add_order_material,
    kaspi_gross_vs_items,
    order_direct_cost,
    order_gross_profit,
)
from app.services.payroll import (
    PayrollSummaryLike,
    add_work_entry,
    approve_payroll_summary,
    calculate_payroll,
    effective_rate,
    pay_payroll_summary,
    payroll_period_totals,
    unapprove_payroll_summary,
)
from app.services.transfers import TransferIdempotencyConflict, TransferResult, add_transfer

__all__ = [
    "PayrollSummaryLike",
    "TransferIdempotencyConflict",
    "TransferResult",
    "account_balances",
    "add_operation",
    "add_order_material",
    "add_transfer",
    "add_work_entry",
    "approve_payroll_summary",
    "calculate_payroll",
    "counterparty_balances",
    "effective_rate",
    "kaspi_gross_vs_items",
    "order_direct_cost",
    "order_gross_profit",
    "pay_payroll_summary",
    "payroll_period_totals",
    "pnl",
    "reconcile_payout",
    "reconcile_payout_full",
    "reverse_operation",
    "unapprove_payroll_summary",
]
