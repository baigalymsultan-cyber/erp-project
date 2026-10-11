"""module_production page module (R1 split)."""

from __future__ import annotations

from typing import Any

import streamlit as st

from frontend.format import fmt_money
from frontend.i18n import I18N
from frontend.ui_kit import api_get, foreman_form, kpi_row, paged_table, period_range


def module_production(lang: str) -> None:
    t = I18N[lang]
    left, right = st.columns([1, 2])
    with left:
        foreman_form(lang)
    with right:
        start, end = period_range(lang, key="prod")
        kpi = api_get(
            "/reports/work-entries/summary", date_from=start.isoformat(), date_to=end.isoformat()
        )
        kpi_row(
            [
                (t["work_entries_kpi"], str(kpi["count"]), None),
                (t["sum"], fmt_money(kpi["total_sum"]), "good"),
            ]
        )

        employees = [
            None,
            *sorted(
                {
                    str(row["employee_name"])
                    for row in (
                        api_get(
                            "/reports/work-entries",
                            date_from=start.isoformat(),
                            date_to=end.isoformat(),
                            limit=200,
                        ).get("items")
                        or []
                    )
                }
            ),
        ]
        emp = st.selectbox(
            t["employee"],
            employees,
            format_func=lambda e: t["all"] if e is None else e,
            key="prod_emp",
        )

        params: dict[str, Any] = {
            "date_from": start.isoformat(),
            "date_to": end.isoformat(),
        }
        if emp is not None:
            params["employee_name"] = emp
        paged_table(
            "/reports/work-entries",
            params,
            per_page=20,
            key="prod",
            empty=t["no_work_entries"],
        )
