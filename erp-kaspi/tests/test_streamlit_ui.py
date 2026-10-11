"""ШАГ 10 — §58–62 smoke: каждый модуль Streamlit-UI рендерится без SQL-ошибок.

AppTest запускает frontend/app.py как сценарий; erp_test уже содержит схему.
Любая SQL/схемная ошибка в модуле уронит тест, т.е. UI не может молча
разойтись со схемой (§1).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from streamlit.testing.v1 import AppTest

MODULES = [
    "Операции",
    "Импорт",
    "Банки",
    "Заказы",
    "Товары",
    "Материалы",
    "Производство",
    "Зарплата",
    "Отчёты",
    "Настройки",
]


@pytest.fixture(scope="module")
def app_env() -> None:
    """AppTest импортирует frontend/app.py в этом же процессе — БД из env."""
    os.environ.setdefault("DATABASE_NAME", "erp_test")
    os.environ.setdefault("DATABASE_USER", "erp_app")
    os.environ.setdefault("DATABASE_HOST", "127.0.0.1")
    os.environ.setdefault("DATABASE_PORT", "5432")
    os.environ.setdefault(
        "DATABASE_PASSWORD", os.environ.get("TEST_DATABASE_PASSWORD", "devpass_local_only")
    )


def _seed_smoke_user(*roles: str) -> dict[str, object]:
    """Real active user with the given roles — current_auth() revalidates.

    The revalidation (§12/§17) reads users/user_roles on every rerun, so the
    smoke harness must authenticate as a real row with real role rows, not a
    fabricated id or a payload-override role list (fresh DB roles win).
    """
    import uuid

    from sqlalchemy import text

    from app.core.database import get_session_factory
    from app.core.security import hash_password

    username = f"smoke-{roles[0].lower()}-{uuid.uuid4().hex[:10]}"
    with get_session_factory()() as s, s.begin():
        row = s.execute(
            text(
                "INSERT INTO users (username, password_hash) "
                "VALUES (:u, :h) RETURNING id"
            ),
            {"u": username, "h": hash_password("Smoke-Pass-2026!")},
        ).scalar_one()
        for code in roles:
            s.execute(
                text(
                    "INSERT INTO user_roles (user_id, role_id) "
                    "SELECT :uid, id FROM roles WHERE code = :code"
                ),
                {"uid": row, "code": code},
            )
    from frontend import api_client

    resp = api_client.request(
        "POST",
        "/auth/login",
        json={"username": username, "password": "Smoke-Pass-2026!"},
    )
    # P4: the gate trusts only a real JWT — seed the token the API issued,
    # not a fabricated payload (current_auth revalidates via GET /auth/me).
    return {
        "user_id": row,
        "username": username,
        "full_name": "Smoke User",
        "roles": list(roles),
        "_token": resp["access_token"],
    }


def _seed_smoke_admin() -> dict[str, object]:
    """Backward-compatible alias for the ADMIN smoke user."""
    return _seed_smoke_user("ADMIN")


@pytest.mark.parametrize("module", MODULES)
def test_module_renders(app_env, module: str) -> None:
    """Каждый модуль рендерится аутентифицированным ADMIN (login-gate пройден)."""
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(
        str(Path(__file__).resolve().parent.parent / "frontend" / "app.py"),
        default_timeout=30,
    )
    # TD-014: без аутентификации UI показывает login-экран, а не данные.
    admin = _seed_smoke_admin()
    at.session_state["auth_user"] = admin
    at.session_state["auth_token"] = admin["_token"]
    at.run()
    assert not at.exception, f"module {module}: {at.exception}"
    # §60 — переключаем sidebar на модуль и убеждаемся, что страница живая.
    radios = at.sidebar.radio
    assert radios, "sidebar with language + module radios expected"
    module_radio = radios[-1]
    module_radio.set_value(module).run()
    assert not at.exception, f"module {module}: {at.exception}"


# --------------------------------------------------------------------------- #
# TD-014: login-gate и админ-меню по ролям (AppTest)
# --------------------------------------------------------------------------- #


def _apptest() -> AppTest:
    from streamlit.testing.v1 import AppTest

    return AppTest.from_file(
        str(Path(__file__).resolve().parent.parent / "frontend" / "app.py"),
        default_timeout=30,
    )


def test_login_gate_blocks_anonymous(app_env) -> None:
    """Без аутентификации — экран входа, ни одной строки данных и меню модулей."""
    at = _apptest()
    at.run()
    assert not at.exception, str(at.exception)
    body = " ".join(e.body for e in at.markdown if hasattr(e, "body"))
    assert "ERP — вход в систему" in body
    assert not at.sidebar.radio, "module radio must not exist before login"


def test_admin_menu_role_filtered(app_env) -> None:
    """§53: VIEWER не видит админ-модули, ADMIN видит (та же иерархия, что в API)."""
    viewer = _apptest()
    vseed = _seed_smoke_user("VIEWER")
    viewer.session_state["auth_user"] = vseed
    viewer.session_state["auth_token"] = vseed["_token"]
    viewer.run()
    assert not viewer.exception, str(viewer.exception)
    assert not viewer.sidebar.selectbox, "VIEWER must not see the admin menu"

    admin = _apptest()
    aseed = _seed_smoke_admin()
    admin.session_state["auth_user"] = aseed
    admin.session_state["auth_token"] = aseed["_token"]
    admin.run()
    assert not admin.exception, str(admin.exception)
    assert admin.sidebar.selectbox, "ADMIN must see the admin menu"



def _dataframes(at) -> list:
    """All dataframes rendered by the run (Settings UI check)."""
    return [e.value for e in at.dataframe]


def test_settings_screen_masks_db_password(engine, applied_schema):
    """§10 — the Settings module must never render the DB password."""
    at = _apptest()
    at.session_state["auth_token"] = _seed_smoke_admin()["_token"]
    at.run()
    assert not at.exception
    # Switch the sidebar to the Settings module (the default is "Операции").
    module_radio = at.sidebar.radio[-1]
    module_radio.set_value("Настройки").run()
    assert not at.exception
    rows = [df for df in _dataframes(at)]
    texts = "\n".join(str(df.to_dict()) for df in rows) + "\n".join(
        str(m.value) for m in at.markdown
    )
    assert "••••••" in texts, "password mask must be shown"
    assert "devpass" not in texts, "raw DB password leaked into Settings UI"



def test_operations_screen_switches_ru_and_kk(app_env) -> None:
    at = _apptest()
    admin = _seed_smoke_admin()
    at.session_state["auth_user"] = admin
    at.session_state["auth_token"] = admin["_token"]
    at.run()
    assert not at.exception, str(at.exception)

    at.sidebar.radio[0].set_value("kk").run()
    assert not at.exception, str(at.exception)
    at.sidebar.radio[0].set_value("ru").run()
    assert not at.exception, str(at.exception)
