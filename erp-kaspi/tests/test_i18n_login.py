"""
TD-015 HTML-фронтенд (i18n) — login gate теперь локализован через единый
каталог I18N в frontend.app (RU default, KZ; §58, §6 — одна система,
без запасного каталога в auth_gate).

Проверки:
  * каталог логина полон в обоих языках, значение не пустое и различается;
  * без выбранного языка login-экран рендерится на русском (§10 default);
  * при lang="kk" рендерятся казахские строки и не протекают русские;
  * при lang="ru" не протекают казахские строки;
  * формат-плейсхолдер err_locked не попадает в UI неподставленным.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from frontend.app import I18N

_LOGIN_KEYS = (
    "login_title",
    "login_username",
    "login_password",
    "login_submit",
    "err_locked",
    "err_inactive",
    "err_rate_limited",
    "err_invalid",
    "logout",
)

_RU_TITLE = "ERP — вход в систему"
_KK_TITLE = "ERP — жүйеге кіру"


# --------------------------------------------------------------------------- #
# catalog
# --------------------------------------------------------------------------- #


def test_login_catalog_complete_in_both_languages() -> None:
    missing = [k for k in _LOGIN_KEYS if k not in I18N["ru"] or k not in I18N["kk"]]
    assert not missing, f"нет ключей логина в I18N: {missing}"
    for lang in ("ru", "kk"):
        for key in _LOGIN_KEYS:
            assert isinstance(I18N[lang][key], str) and I18N[lang][key].strip(), (
                f"пустя строка {key} в {lang}"
            )


def test_login_catalog_ru_and_kz_differ() -> None:
    for key in _LOGIN_KEYS:
        assert I18N["ru"][key] != I18N["kk"][key], f"{key} совпадает в RU/KZ"


def test_err_locked_placeholders_are_format_targets() -> None:
    for lang in ("ru", "kk"):
        text = I18N[lang]["err_locked"]
        assert "{n}" in text and "{m}" in text, f"{lang}: err_locked без плейсхолдера"
        assert "{n}" not in text.replace("{n}", "", 1)
        assert "{" not in text.replace("{n}", "").replace("{m}", ""), (
            f"{lang}: лишний плейсхолдер в err_locked"
        )


# --------------------------------------------------------------------------- #
# rendering via AppTest
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def app_env() -> None:
    """AppTest imports frontend/app.py in this process — DB settings from env."""
    os.environ.setdefault("DATABASE_NAME", "erp_test")
    os.environ.setdefault("DATABASE_USER", "erp_app")
    os.environ.setdefault("DATABASE_HOST", "127.0.0.1")
    os.environ.setdefault("DATABASE_PORT", "5432")
    os.environ.setdefault(
        "DATABASE_PASSWORD", os.environ.get("TEST_DATABASE_PASSWORD", "devpass_local_only")
    )


@pytest.fixture(autouse=True)
def _clear_lang_key():
    st.session_state.pop("lang", None)
    yield
    st.session_state.pop("lang", None)


#: Absolute path to frontend/app.py (AppTest resolves relative to caller file).
_APP_PATH = str(Path(__file__).resolve().parent.parent / "frontend" / "app.py")


def _login_body(at: AppTest) -> str:
    return " ".join(m.value for m in at.markdown)


def test_login_screen_defaults_to_russian(app_env) -> None:
    at = AppTest.from_file(_APP_PATH, default_timeout=10)
    at.run()
    assert _RU_TITLE in _login_body(at)
    assert _KK_TITLE not in _login_body(at)
    assert [r.options for r in at.radio], "нет переключателя языка на логине"


def test_login_screen_kz_renders_kazakh(app_env) -> None:
    at = AppTest.from_file(_APP_PATH, default_timeout=10)
    at.session_state["lang"] = "kk"
    at.run()
    assert _KK_TITLE in _login_body(at)
    assert _RU_TITLE not in _login_body(at)
    texts = _login_body(at)
    for leaked in ("Имя пользователя", "Войти"):
        assert leaked not in texts


def test_login_screen_ru_does_not_leak_kazakh(app_env) -> None:
    at = AppTest.from_file(_APP_PATH, default_timeout=10)
    at.session_state["lang"] = "ru"
    at.run()
    body = _login_body(at)
    for leaked in ("Қолданушы аты", "жүйеге кіру"):
        assert leaked not in body