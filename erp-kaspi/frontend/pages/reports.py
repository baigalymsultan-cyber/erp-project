"""module_reports page module (R1 split)."""

from __future__ import annotations

from frontend.i18n import I18N
from frontend.reports import render as render_reports


def module_reports(lang: str) -> None:
    t = I18N[lang]
    render_reports(t)
