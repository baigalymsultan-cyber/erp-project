"""module_import page module (R1 split)."""

from __future__ import annotations

import streamlit as st

from frontend.i18n import I18N
from frontend.ui_kit import api_get, kpi_row, paged_table


def module_import(lang: str) -> None:
    t = I18N[lang]
    data = api_get("/import-batches", limit=1)
    kpi = data.get("kpi") or {}
    kpi_row(
        [
            ("Batches", str(kpi.get("total", 0)), None),
            ("Inserted", f"{kpi.get('inserted', 0):,}".replace(",", " "), "good"),
            ("Errors", str(kpi.get("errors", 0)), "bad" if kpi.get("errors") else None),
        ]
    )
    st.caption("etl/import_excel.py — CLI: python -m etl.import_excel --file <xlsx> --ip <id>")
    paged_table("/import-batches", {}, per_page=20, key="import", empty=t["no_imports"])
