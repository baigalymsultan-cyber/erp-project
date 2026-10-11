"""module_banks page module (R1 split)."""

from __future__ import annotations

import streamlit as st

from frontend.i18n import I18N
from frontend.ui_kit import _display_frame, api_get, empty_state


def module_banks(lang: str) -> None:
    t = I18N[lang]

    st.markdown(f"**{t['ip_accounts']}**")
    accounts = api_get("/accounts", limit=200)
    if accounts.get("items"):
        st.dataframe(_display_frame(accounts["items"]), use_container_width=True, hide_index=True)
    else:
        empty_state(t["no_accounts"])

    st.markdown(f"**{t['kaspi_payouts']}**")
    payouts = api_get("/kaspi/payouts", limit=100)
    if payouts.get("payouts"):
        st.dataframe(_display_frame(payouts["payouts"]), use_container_width=True, hide_index=True)
    else:
        empty_state(t["no_kaspi_payouts"])

    st.markdown(f"**{t['sync_log']}**")
    syncs = api_get("/kaspi/sync-log", limit=50)
    if syncs.get("items"):
        st.dataframe(_display_frame(syncs["items"]), use_container_width=True, hide_index=True)
    else:
        empty_state(t["no_syncs"])
