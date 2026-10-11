"""Shared UI kit — tables, charts, forms, API wrappers (R1 split)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pandas as pd
import plotly.express as px
import streamlit as st

from app.schemas import WorkEntryCreate
from frontend import api_client
from frontend.auth_gate import restrict_ip_choices
from frontend.format import fmt_money
from frontend.i18n import I18N
from frontend.theme import BAD, GOOD, LINE

_MONEY_KEYS = (
    "amount",
    "income",
    "expense",
    "profit",
    "total",
    "sum",
    "rate",
    "price",
    "paid",
    "due",
    "calculated",
    "gross",
    "commission",
    "net",
    "piecework",
    "base_salary",
    "bonuses",
    "deductions",
)


def _money_key(key: str) -> bool:
    low = key.lower()
    return any(m in low for m in _MONEY_KEYS)


def _frame_from_items(items: list[dict]) -> pd.DataFrame:
    """dict-строки API → DataFrame; суммы-строки → float для отображения."""
    return pd.DataFrame(items)


def _raw_frame(items: list[dict]) -> pd.DataFrame:
    """Для Excel-экспорта (§62): суммы API-строками → числа."""
    raw = pd.DataFrame(items)
    for col in raw.columns:
        if raw[col].dtype == object and len(raw) and _money_key(col):
            converted = pd.to_numeric(raw[col], errors="ignore")
            if not pd.api.types.is_object_dtype(converted):
                raw[col] = converted
    return raw


def _display_frame(items: list[dict]) -> pd.DataFrame:
    """Деньги — §15-формат; None — тире (§60 читаемость)."""
    raw = _raw_frame(items)
    frame = raw.copy()
    for col in frame.columns:
        if frame[col].dtype != object and len(frame) and _money_key(col):
            frame[col] = frame[col].map(fmt_money)
    for col in frame.columns:
        frame[col] = frame[col].map(lambda v: "\u2014" if v is None else v)
    return frame


def api_get(path: str, **params: Any) -> dict[str, Any]:
    """Один GET через api_client; 401/403 — logout, прочее — понятная ошибка."""
    token = api_client.current_token()
    try:
        return api_client.get(path, token=token, **params)
    except api_client.ApiError as exc:
        t = I18N[st.session_state.get("lang", "ru")]
        if exc.status_code in (401, 403):
            from frontend.auth_gate import discard_session

            discard_session()
            st.error(t["session_expired"])
            st.stop()
        st.error(t["api_error_retry"].format(status=exc.status_code))
        st.stop()


def api_post(path: str, json: dict[str, Any]) -> dict[str, Any]:
    token = api_client.current_token()
    try:
        return api_client.request("POST", path, token=token, json=json)  # type: ignore[arg-type]
    except api_client.ApiError as exc:
        if exc.status_code in (401, 403):
            from frontend.auth_gate import discard_session

            discard_session()
            st.error(I18N[st.session_state.get("lang", "ru")]["session_expired"])
            st.stop()
        raise


def kpi_row(items: list[tuple[str, str, str | None]]) -> None:
    """Полоса KPI-карточек: (label, value, tone) где tone — good|bad|None."""
    cols = st.columns(len(items))
    for col, (label, value, tone) in zip(cols, items, strict=True):
        with col:
            tone_cls = {"good": "erp-kpi-good", "bad": "erp-kpi-bad"}.get(tone or "", "")
            st.markdown(
                f'<div class="erp-card"><div class="erp-kpi-label">{label}</div>'
                f'<div class="erp-kpi-value {tone_cls}">{value}</div></div>',
                unsafe_allow_html=True,
            )


def empty_state(message: str) -> None:
    st.markdown(
        f'<div class="erp-card" style="text-align:center; padding:2rem 1rem;">'
        f'<div style="font-size:1.6rem;">🗂️</div>'
        f'<div style="color:var(--muted); margin-top:0.3rem;">{message}</div></div>',
        unsafe_allow_html=True,
    )


def chips(labels: dict[str, str]) -> str:
    return " ".join(f'<span class="erp-chip">{v}</span>' for v in labels.values())


def paged_table(
    path: str,
    params: dict,
    *,
    per_page: int = 25,
    key: str = "tbl",
    empty: str = "Данных нет",
    items_key: str = "items",
    df_kwargs: dict | None = None,
) -> int:
    """Серверная пагинация через API — данные не режутся молча (REQ-TD016-03).

    Первый вызов берёт limit=1 ради total (дёшево), затем страницу.
    Возвращает общее число строк.
    """
    meta = api_get(path, limit=1, offset=0, **params)
    total = int(meta.get("total", meta.get("count", 0)))
    if total == 0:
        empty_state(empty)
        return 0

    pages = (total + per_page - 1) // per_page
    page = st.number_input(
        "Стр.",
        min_value=1,
        max_value=int(pages),
        value=1,
        step=1,
        key=f"{key}_page",
        label_visibility="collapsed",
    )
    data = api_get(path, limit=per_page, offset=(int(page) - 1) * per_page, **params)
    items = data.get(items_key) or []
    if not items:
        empty_state(empty)
        return int(total)
    frame = _display_frame(items)
    kwargs = {"use_container_width": True, "hide_index": True, **(df_kwargs or {})}
    st.dataframe(frame, **kwargs)
    cap = I18N[st.session_state.get("lang", "ru")]
    st.caption(f"{total} {cap['rows_total'].lower()} · {cap['page']} {page}/{pages}")
    return int(total)


def money_column(col: str) -> dict:
    """Скрыть служебную числовую колонку сортировки для денежной пары."""
    return {col: None}


def period_range(lang: str, key: str = "period") -> tuple[date, date]:
    """Период по умолчанию — текущий месяц; выбор пресета или диапазона."""
    t = I18N[lang]
    today = date.today()
    presets = {
        t["day"]: (today, today),
        t["week"]: (today - timedelta(days=6), today),
        t["month"]: (today.replace(day=1), today),
        t["half"]: (today - timedelta(days=181), today),
        t["year"]: (today.replace(month=1, day=1), today),
        t["custom"]: None,
    }
    choice = st.selectbox(t["period"], list(presets), key=f"{key}_preset")
    if presets[choice] is not None:
        return presets[choice]
    c1, c2 = st.columns(2)
    start = c1.date_input(t["date_from"], value=today.replace(day=1), key=f"{key}_from")
    end = c2.date_input(t["date_to"], value=today, key=f"{key}_to")
    return min(start, end), max(start, end)


def daily_chart(start: date, end: date, lang: str) -> None:
    """График доходов/расходов по дням — только реальные POSTED-операции (E2)."""
    data = api_get("/operations/daily", date_from=start.isoformat(), date_to=end.isoformat())
    items = data.get("items") or []
    if not items:
        return
    t = I18N[lang]
    frame = pd.DataFrame(items)
    frame["day"] = frame["day"].astype(str)
    for col in ("income", "expense"):
        frame[col] = pd.to_numeric(frame[col])
    long = frame.melt(
        id_vars="day", value_vars=["income", "expense"], var_name="kind", value_name="amount"
    )
    long["kind"] = long["kind"].map({"income": t["income"], "expense": t["expense"]})
    long["label"] = long["amount"].map(lambda v: fmt_money(v))
    fig = px.bar(
        long,
        x="day",
        y="amount",
        color="kind",
        barmode="group",
        color_discrete_map={t["income"]: GOOD, t["expense"]: BAD},
        custom_data=["label"],
    )
    fig.update_traces(hovertemplate="%{x|%d.%m.%Y}<br>%{customdata[0]}<extra></extra>")
    fig.update_layout(
        height=300,
        margin=dict(l=0, r=0, t=12, b=0),
        legend=dict(orientation="h", yanchor="bottom", y=1.0, title=None),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
    )
    fig.update_yaxes(gridcolor=LINE, zerolinecolor=LINE, tickformat="~s")
    fig.update_xaxes(gridcolor=LINE)
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})


def status_chip(status: str | None) -> str:
    if status is None:
        return "—"
    s = str(status).upper()
    if s in ("POSTED", "SUCCESS", "CONFIRMED", "PAID", "RECONCILED", "RECEIVED", "ACTIVE"):
        cls = "erp-chip-ok"
    elif s in ("RUNNING", "PROCESSING", "OPEN", "PARTIAL", "EXPECTED", "DRAFT", "ON_LEAVE"):
        cls = "erp-chip-warn"
    elif s in ("FAILED", "ERROR", "CANCELLED", "DISCREPANCY", "RETURNED"):
        cls = "erp-chip-err"
    else:
        cls = ""
    return f'<span class="erp-chip {cls}">{status}</span>'


def ip_choices() -> list[Any]:
    """ИП-фильтр: /ips уже серверно изолирован (E22); 'все' — None сверху."""

    class _Ip:
        def __init__(self, d: dict) -> None:
            self.id = int(d["id"])
            self.name = str(d["name"])

    rows = [_Ip(i) for i in (api_get("/ips", limit=200).get("items") or [])]
    return restrict_ip_choices(rows)


def foreman_form(lang: str) -> None:
    t = I18N[lang]
    st.markdown(f"**{t['foreman_title']}**")
    st.caption(t["foreman_hint"])

    employees_raw = api_get("/employees", status="ACTIVE", limit=200).get("items") or []
    employees = [e for e in employees_raw if e.get("assigned_ip_id") is not None]
    work_types = api_get("/work-operation-types").get("items") or []
    orders_raw = api_get("/orders", limit=200).get("items") or []
    orders = [o for o in orders_raw if o.get("status") != "CANCELLED"]
    ips = ip_choices()
    allowed = restrict_ip_choices(ips)
    allowed_ids = {ip.id for ip in allowed if ip is not None}
    employees = [e for e in employees if e["assigned_ip_id"] in allowed_ids]

    if not employees or not work_types or not ips:
        empty_state(t["empty_employees"])
        return

    with st.container(border=True):
        employee = st.selectbox(
            t["employee"],
            employees,
            format_func=lambda r: r["full_name"],
            key="foreman_employee",
        )
        work_type = st.selectbox(
            t["work_type"],
            work_types,
            format_func=lambda r: f"{r['name']} ({r['unit']})",
            key="foreman_work_type",
        )
        order = st.selectbox(
            t["order"],
            [None, *orders],
            format_func=lambda r: (
                t["order_none"]
                if r is None
                else "#{} {} {}".format(
                    r["id"], r.get("external_order_id") or "", r.get("customer_name") or ""
                ).strip()
            ),
            key="foreman_order",
        )
        quantity = st.number_input(
            t["quantity"], min_value=0.001, value=1.0, step=1.0, key="foreman_qty"
        )
        entry_date = st.date_input(t["entry_date"], value=date.today(), key="foreman_date")

        # Аудит #20: запись нельзя молча привязывать к первому ИП —
        # это исказило бы финансы чужого ИП. Без явной привязки — отказ.
        if employee["assigned_ip_id"] is None:
            st.error(
                t.get(
                    "no_ip_assigned",
                    "У сотрудника не указан ИП — сохранить выработку нельзя. "
                    "Назначьте ИП в «Настройках».",
                )
            )
            st.stop()
        ip_id = int(employee["assigned_ip_id"])

        if st.button(t["save"], type="primary", key="foreman_save", use_container_width=True):
            data = WorkEntryCreate(
                employee_id=int(employee["id"]),
                work_operation_type_id=int(work_type["id"]),
                ip_id=ip_id,
                order_id=int(order["id"]) if order is not None else None,
                entry_date=entry_date,
                quantity=str(quantity),
            )
            try:
                # §54 — аудит пишется серверно (fn_audit + service layer);
                # ставка замораживается сервисом, не в UI (§61).
                result = api_post("/payroll/work-entries", data.model_dump(mode="json"))
                st.success(
                    t["saved"].format(
                        id=result["id"],
                        rate=fmt_money(result["rate_at_time"]),
                        total=fmt_money(result["total_sum"]),
                    )
                )
            except api_client.ApiError as exc:
                # Нет ставки на дату / нарушение бизнес-правил — показать, не ронять UI.
                no_rate = "no rate in force" in exc.kind
                message = t["no_rate"] if no_rate else t["err"].format(msg=exc.kind[:200])
                st.error(message)
