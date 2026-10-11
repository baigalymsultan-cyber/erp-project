"""
Административные модули Streamlit UI (TD-014/TD-016 Phase 2).

Четыре модуля «Администрирование». Данные — ТОЛЬКО через api_client
(TD-016 P5: прямой DB-доступ убран; модуль не получает соединений):

  * Сотрудники  — GET /employees + GET /ips (HR+): фильтры статус/ИП/поиск;
  * Мониторинг  — GET /monitoring/summary + GET /kaspi/sync-log (ADMIN):
                  наличие таблиц, sync_log за 24 ч, stale RUNNING > 1 ч,
                  ошибки аутентификации;
  * Пользователи — GET /auth/users (ADMIN): активность, роли;
                  парольные хеши никогда не покидают БД (§55);
  * Аудит       — GET /audit-log (§54, DIRECTOR+): фильтры entity/action/
                  пользователь/даты + серверная пагинация.

Права доступа проверяет вызывающая сторона (frontend/app.py через
auth_gate.has_min_role) и дублирует сам API (require_privilege) — одна
семантика, две точки защиты (§53).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pandas as pd
import streamlit as st

from frontend import api_client
from frontend.i18n import I18N
from frontend.ui_kit import empty_state, kpi_row, paged_table


def module_employees(lang: str) -> None:
    """Реестр сотрудников (HR+): фильтры статус/ИП, поиск по ФИО, пагинация."""
    t = I18N[lang]

    f1, f2, f3 = st.columns([1, 1, 2])
    status = f1.selectbox(
        t["status"], [None, "ACTIVE", "ON_LEAVE", "FIRED"], key="emp_status",
        format_func=lambda s: t["all"] if s is None else s,
    )
    ips: list[dict[str, Any]] = [
        None,  # type: ignore[list-item]
        *api_client.get("/ips").get("items", []),
    ]
    ip_sel = f2.selectbox(
        t["ip"], ips, key="emp_ip", format_func=lambda r: t["all"] if r is None else r["name"],
    )
    query = f3.text_input(t["search_employees"], key="emp_q", placeholder="…")

    params: dict[str, Any] = {}
    if status is not None:
        params["status"] = status
    if ip_sel is not None:
        params["ip_id"] = ip_sel["id"]
    if query.strip():
        params["q"] = query.strip()

    kpi = api_client.get("/employees", limit=1, **params)
    kpi_row([
        (t["employees_total"], str(kpi.get("total", 0)), None),
        (t["employees_active"], str(kpi.get("active", 0)), "good"),
    ])

    paged_table(
        "/employees", params, per_page=25, key="employees", empty=t["empty_generic"],
        df_kwargs={"column_config": {
            "hired_at": st.column_config.DateColumn("Принят"),
            "fired_at": st.column_config.DateColumn("Уволен"),
        }},
    )


def module_monitoring(lang: str) -> None:
    """Зеркало GET /monitoring/summary (§72) + последние sync-запуски."""
    t = I18N[lang]

    summary = api_client.get("/monitoring/summary")
    db = summary.get("database", {})
    sync = summary.get("kaspi_sync_last_24h", {})
    auth = summary.get("auth", {})
    missing = list(db.get("missing_tables", []))

    db_ok = bool(db.get("ok"))
    kpi_row([
        ("БД", t["db_ok"] if db_ok else t["db_missing"], "good" if db_ok else "bad"),
        ("Sync FAILED 24ч", str(sync.get("failed", 0)), "bad" if sync.get("failed") else None),
        ("Sync PARTIAL 24ч", str(sync.get("partial", 0)), None),
        ("Stale RUNNING >1ч", str(sync.get("stale_running", 0)),
         "bad" if sync.get("stale_running") else None),
        ("Неудачных входов", str(auth.get("failed_logins_total", 0)), None),
    ])
    if not db_ok:
        st.error(t["db_missing_tables"].format(tables=", ".join(missing)))

    st.markdown(f"**{t['sync_recent']}**")
    recent = api_client.get("/kaspi/sync-log", limit=20).get("items", [])
    if recent:
        st.dataframe(
            _display(recent, {
                "id": "ID", "sync_type": "Тип", "status": "Статус",
                "sync_started_at": "Начало", "finished_at": "Окончание",
                "errors_count": "Ошибки",
            }),
            use_container_width=True, hide_index=True,
        )
    else:
        empty_state(t["empty_generic"])


def module_users(lang: str) -> None:
    """Пользователи и роли (ADMIN). Парольные хеши не отображаются (§55)."""
    t = I18N[lang]

    data = api_client.get("/auth/users")
    rows = list(data.get("items", []))
    if not rows:
        empty_state(t["empty_generic"])
        return
    table = [
        {
            "ID": u.get("id"),
            "Логин": u.get("username"),
            "ФИО": u.get("full_name") or "—",
            "Роли": ", ".join(u.get("roles") or []) or "—",
            "Активен": "✓" if u.get("is_active") else "✗",
        }
        for u in rows
    ]
    st.dataframe(pd.DataFrame(table), use_container_width=True, hide_index=True)

    st.markdown(f"**{t['roles_matrix']}**")
    counts: dict[str, int] = {}
    for u in rows:
        if u.get("is_active"):
            for role in u.get("roles") or []:
                counts[role] = counts.get(role, 0) + 1
    st.dataframe(
        pd.DataFrame(
            [{"Код": code, "Пользователей": n} for code, n in sorted(counts.items())]
        ),
        use_container_width=True, hide_index=True,
    )


def module_audit(lang: str) -> None:
    """Журнал аудита (§54): фильтры + серверная пагинация, только чтение."""
    t = I18N[lang]

    f1, f2, f3 = st.columns([1, 1, 1])
    # Facets — из свежих событий (сервер отдаёт их отсортированными по id desc);
    # полный DISTINCT по всей таблице через UI не нужен и дорог.
    recent = api_client.get("/audit-log", limit=200).get("items", [])
    entities = [None, *sorted({str(r["entity"]) for r in recent})]  # type: ignore[list-item]
    entity = f1.selectbox(t["audit_entity"], entities, key="aud_entity",
                          format_func=lambda e: t["all"] if e is None else e)
    actions = [None, *sorted({str(r["action"]) for r in recent})]  # type: ignore[list-item]
    action = f2.selectbox(t["audit_action"], actions, key="aud_action",
                          format_func=lambda a: t["all"] if a is None else a)
    users = [None, *{  # type: ignore[list-item]
        (r.get("user_id"), r.get("username") or "—") for r in recent
    }]
    user = f3.selectbox(t["audit_user"], users, key="aud_user",
                        format_func=lambda u: t["all"] if u is None else u[1])
    c1, c2, _ = st.columns([1, 1, 2])
    date_from = c1.date_input(t["date_from"], value=date.today() - timedelta(days=30),
                              key="aud_from")
    date_to = c2.date_input(t["date_to"], value=date.today(), key="aud_to")

    params: dict[str, Any] = {"created_from": date_from, "created_to": date_to}
    if entity is not None:
        params["entity"] = entity
    if action is not None:
        params["action"] = action
    if user is not None:
        params["user_id"] = user[0]

    kpi = api_client.get("/audit-log", limit=1, **params)
    kpi_row([(t["audit_events"], str(kpi.get("total", 0)), None)])

    paged_table("/audit-log", params, per_page=25, key="audit", empty=t["empty_generic"])


def _display(items: list[dict[str, Any]], columns: dict[str, str]) -> pd.DataFrame:
    """Переименовать ключи API в заголовки таблицы и погасить None."""
    frame = pd.DataFrame([{columns.get(k, k): v for k, v in row.items()} for row in items])
    for col in frame.columns:
        frame[col] = frame[col].map(lambda v: "—" if v is None else v)
    return frame


#: Рендереры административных модулей (frontend/app.py роутит по этому словарю).
ADMIN_RENDERERS = {
    "Сотрудники": module_employees,
    "Мониторинг": module_monitoring,
    "Пользователи": module_users,
    "Аудит": module_audit,
}
