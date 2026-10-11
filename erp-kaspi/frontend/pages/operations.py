"""module_operations page module (R1 split)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import streamlit as st

from frontend.format import fmt_money
from frontend.i18n import I18N
from frontend.ui_kit import api_get, daily_chart, ip_choices, kpi_row, paged_table, period_range


def module_operations(lang: str) -> None:
    t = I18N[lang]
    start, end = period_range(lang, key="ops")

    f1, f2, f3 = st.columns([1, 1, 2])
    direction = f1.selectbox(
        t["direction"], [t["both"], t["in_only"], t["out_only"]], key="ops_dir"
    )
    ips = ip_choices()
    ip_sel = f2.selectbox(
        t["ip"], ips, format_func=lambda r: t["all"] if r is None else r.name, key="ops_ip"
    )
    query = f3.text_input(t["search"], key="ops_q", placeholder="…")

    # operation_types.kind is INCOME/EXPENSE/TRANSFER (chk_operation_types_kind);
    # the filter used to send "IN"/"OUT", which matched nothing (audit #18).
    kind = {t["both"]: None, t["in_only"]: "INCOME", t["out_only"]: "EXPENSE"}[direction]

    params: dict[str, Any] = {
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "q": query.strip(),
    }
    if kind is not None:
        params["kind"] = kind
    if ip_sel is not None:
        params["ip_id"] = ip_sel.id

    kpi = api_get("/operations/summary", **params)
    # Decimal-safe API sums (§94) come back as strings — convert for §15 format.
    inc = Decimal(str(kpi["income"]))
    exp = Decimal(str(kpi["expense"]))
    kpi_row(
        [
            (t["income"], fmt_money(inc), "good"),
            (t["expense"], fmt_money(exp), "bad"),
            (t["financial_result"], fmt_money(inc - exp), None),
            (t["rows_total"], str(kpi["count"]), None),
        ]
    )
    status_counts = kpi["status_counts"]
    st.caption(
        " · ".join(
            (
                f"{t['posted_rows']}: {status_counts.get('POSTED', 0)}",
                f"{t['draft_rows']}: {status_counts.get('DRAFT', 0)}",
                f"{t['reversed_rows']}: {status_counts.get('REVERSED', 0)}",
                f"{t['corrected_rows']}: {status_counts.get('CORRECTED', 0)}",
                f"{t['transfer_rows']}: {kpi['transfer_count']}",
            )
        )
    )

    with st.expander(t["chart_daily"], expanded=True):
        daily_chart(start, end, lang)

    paged_table("/operations", params, per_page=25, key="ops", empty=t["empty"])
