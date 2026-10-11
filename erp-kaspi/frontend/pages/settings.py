"""module_settings page module (R1 split)."""

from __future__ import annotations

import os

import pandas as pd
import streamlit as st

from app.core.config import DatabaseSettings
from frontend.i18n import I18N


def module_settings(lang: str) -> None:
    t = I18N[lang]
    st.subheader(t["settings_title"])
    settings = DatabaseSettings.from_env()
    rows = [
        {"name": "APP_ENV", "value": os.environ.get("APP_ENV", "development")},
        {"name": "DATABASE_HOST", "value": settings.host},
        {"name": "DATABASE_PORT", "value": str(settings.port)},
        {"name": "DATABASE_NAME", "value": settings.name},
        {"name": "DATABASE_USER", "value": settings.user},
        {"name": "DATABASE_PASSWORD", "value": t["password_hidden"]},
    ]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    st.caption(t["db_password_note"])
    st.info(t["kaspi_blocked_note"], icon="🚧")
