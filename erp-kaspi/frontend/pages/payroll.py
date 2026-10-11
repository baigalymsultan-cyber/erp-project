"""module_payroll page module (R1 split)."""

from __future__ import annotations

from decimal import Decimal

from frontend.format import fmt_money
from frontend.i18n import I18N
from frontend.ui_kit import api_get, kpi_row


def module_payroll(lang: str) -> None:
    from frontend.reports import _payroll

    t = I18N[lang]
    kpi = api_get("/reports/payroll-due/summary")
    kpi_row(
        [
            (t["accrued"], fmt_money(kpi["calculated"]), None),
            (t["paid_out"], fmt_money(kpi["paid"]), "good"),
            (t["balance_due"], fmt_money(kpi["due"]), "bad" if Decimal(kpi["due"]) else None),
        ]
    )
    _payroll(t)
