"""module_orders page module (R1 split)."""

from __future__ import annotations

from typing import Any

import pandas as pd
import plotly.express as px
import streamlit as st

from frontend.format import fmt_money
from frontend.i18n import I18N
from frontend.ui_kit import api_get, kpi_row, paged_table


def module_orders(lang: str) -> None:
    t = I18N[lang]

    statuses = [None, *api_get("/orders/statuses").get("statuses", [])]
    f1, f2 = st.columns([1, 2])
    st_sel = f1.selectbox(
        t["status"], statuses, format_func=lambda s: t["all"] if s is None else s, key="ord_status"
    )
    query = f2.text_input(t["search_orders"], key="ord_q", placeholder=t["search_orders_ph"])

    params: dict[str, Any] = {"q": query.strip()}
    if st_sel is not None:
        params["status"] = st_sel

    kpi = api_get("/orders/summary", **params)
    by_status = kpi.get("by_status") or []
    kpi_row(
        [
            (t["orders_kpi"], str(kpi["count"]), None),
            (t["sum"], fmt_money(kpi["total_amount"]), None),
            # statuses come from chk_orders_status: NEW/APPROVED/ASSEMBLING/
            # DELIVERING/COMPLETED/CANCELLED/RETURNED (audit #19 — "DONE"/
            # "DELIVERED" never matched, so completed orders were not highlighted).
            *(
                (
                    row["status"],
                    str(row["n"]),
                    "good" if row["status"] in ("COMPLETED", "DELIVERING") else None,
                )
                for row in by_status[:3]
            ),
        ]
    )

    if by_status:
        fig = px.pie(
            pd.DataFrame(by_status, columns=["status", "n"]),
            names="status",
            values="n",
            hole=0.55,
            color_discrete_sequence=px.colors.qualitative.Safe,
        )
        fig.update_layout(
            height=260,
            margin=dict(l=0, r=0, t=0, b=0),
            paper_bgcolor="rgba(0,0,0,0)",
            legend=dict(orientation="h", yanchor="bottom", y=1.05, title=None),
        )
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

    paged_table("/orders", params, per_page=25, key="orders", empty=t["no_orders"])
