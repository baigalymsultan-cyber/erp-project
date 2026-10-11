"""module_materials page module (R1 split)."""

from __future__ import annotations

from frontend.i18n import I18N


def module_materials(lang: str) -> None:
    from frontend.reports import _materials

    t = I18N[lang]
    _materials(t)
