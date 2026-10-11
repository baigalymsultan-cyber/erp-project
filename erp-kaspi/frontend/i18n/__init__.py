"""Frontend i18n — RU/KZ/EN (MASTER PROMPT requires all three).

The ``I18N`` dict maps lang code -> {key: translated string}.
``en`` is loaded if available; ``ru`` is the fallback default.
"""

from __future__ import annotations

from frontend.i18n.kk import I18N_KK
from frontend.i18n.ru import I18N_RU

try:
    from frontend.i18n.en import I18N_EN
except ImportError:  # en.py not yet generated
    I18N_EN = {}

I18N: dict[str, dict[str, str]] = {
    "ru": I18N_RU,
    "kk": I18N_KK,
    "en": I18N_EN or I18N_RU,  # fallback to RU until EN is complete
}

__all__ = ["I18N", "I18N_EN", "I18N_KK", "I18N_RU"]
