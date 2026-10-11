"""module_products page module (R1 split)."""

from __future__ import annotations

from typing import Any

import streamlit as st

from frontend.format import fmt_money
from frontend.i18n import I18N
from frontend.ui_kit import api_get, kpi_row, paged_table


def module_products(lang: str) -> None:
    t = I18N[lang]
    f1, f2 = st.columns([2, 3])
    query = f1.text_input(t["search"], key="prod_q", placeholder=t["search_products_ph"])
    active = f2.selectbox(t["activity"], ["all", "ACTIVE", "INACTIVE"], key="prod_active")

    params: dict[str, Any] = {"q": query.strip()}
    if active != "all":
        params["active"] = active == "ACTIVE"

    kpi = api_get("/products/summary", **params)
    kpi_row(
        [
            (t["products_kpi"], str(kpi["count"]), None),
            (t["avg_price"], fmt_money(kpi["avg_price"]), None),
        ]
    )
    paged_table("/products", params, per_page=25, key="products", empty=t["no_products"])
