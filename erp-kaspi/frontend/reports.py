"""
§62 — Отчёты: вкладка «Отчёты» в Streamlit-дашборде (TD-016 P5).

Каждый отчёт читает ТОЛЬКО данные — через API boundary (frontend/api_client,
E5–E8/E1/E10): агрегаты считает PostgreSQL на сервере, поэтому UI не может
разойтись с регистром. Прямого доступа к БД из UI больше нет (REQ-TD016-04);
изоляция ИП применяется на сервере в каждом endpoint (REQ-TD016-02).

Экспорт — Excel (§62): отдаёт НЕОБРАБОТАННЫЕ числа (строки Decimal → float),
аналитика в Excel не ломается. PDF намеренно не реализуется: §62 требует его
«при необходимости», необходимости нет (см. ADR-013).

UX-контракт: у каждого отчёта — empty state, суммы в §15-формате
(``1 234 567,00 ₸``).
"""

from __future__ import annotations

import io
from decimal import Decimal, InvalidOperation
from typing import Any

import pandas as pd
import plotly.express as px
import streamlit as st

from frontend import api_client
from frontend.format import fmt_money

_MONEY_KEYS = {
    "total_income",
    "total_expense",
    "net_profit",
    "transfers_in",
    "transfers_out",
    "total_calculated",
    "amount_paid",
    "amount_due",
    "piecework_total",
    "base_salary",
    "bonuses",
    "deductions",
    "amount",
    "current_price",
    "selling_price",
    "materials_cost",
    "labor_cost",
    "total_cost",
    "rate_at_time",
    "total_sum",
    "gross_amount",
    "commission_amount",
    "net_amount",
    "quantity",
}


def _to_number(value: Any) -> Any:
    """Decimal-safe string → float для Excel; прочее — как есть."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, str):
        try:
            return float(Decimal(value))
        except (InvalidOperation, ValueError):
            return value
    return value


def _frame(
    items: list[dict[str, Any]], labels: dict[str, str], *, money: bool = True
) -> pd.DataFrame:
    """items API → DataFrame с русскими заголовками; money — §15-формат."""
    if not items:
        return pd.DataFrame()
    frame = pd.DataFrame(items).rename(columns=labels)
    money_labels = {labels[k] for k in _MONEY_KEYS if k in labels}
    for col in money_labels & set(frame.columns):
        frame[col] = frame[col].map(lambda v: fmt_money(v) if money else _to_number(v))
    return frame


def _download(
    frame_display: pd.DataFrame,
    items: list[dict[str, Any]],
    labels: dict[str, str],
    key: str,
    sheet: str,
) -> None:
    """§62 — экспорт в Excel: сырые числа, не строки с '₸'."""
    if not items:
        return
    raw = _frame(items, labels, money=False)
    # M-2 (2026-10-03): Excel formula injection. Values starting with
    # =, +, -, @ become live formulas when opened. Prefix with a single
    # quote so Excel treats them as text.
    for col in raw.columns:
        if raw[col].dtype == object:
            raw[col] = raw[col].map(
                lambda v: f"'{v}" if isinstance(v, str) and v[:1] in ("=", "+", "-", "@") else v
            )
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        raw.to_excel(writer, index=False, sheet_name=sheet)
    st.download_button(
        "⬇ Excel",
        data=buffer.getvalue(),
        file_name=f"{sheet}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key=key,
    )


def _empty(message: str) -> None:
    st.markdown(
        f'<div style="border:1px solid #E5E7EB; border-radius:10px; '
        f'text-align:center; padding:1.6rem; color:#6B7280;">{message}</div>',
        unsafe_allow_html=True,
    )


def _get(path: str, **params: Any) -> list[dict[str, Any]]:
    """GET через api_client от имени текущей сессии (401/403 → логаут)."""
    from frontend.auth_gate import discard_session

    try:
        body = api_client.get(path, token=api_client.current_token(), **params)
    except api_client.ApiError as exc:
        if exc.status_code in (401, 403):
            discard_session()
            st.stop()
        raise
    return list(body.get("items", []))


def _ip_summary_labels(t: dict[str, str]) -> dict[str, str]:
    """Column labels for the IP summary report, from the I18N catalog."""
    return {
        "ip_name": t["col_ip"],
        "ip_bin": t["col_bin"],
        "period_month": t["month"],
        "total_income": t["col_income_kzt"],
        "total_expense": t["col_expense_kzt"],
        "net_profit": t["col_profit_kzt"],
        "transfers_in": t["col_transfers_in"],
        "transfers_out": t["col_transfers_out"],
        "operations_count": t["col_ops_count"],
    }


def _ip_summary(t: dict[str, str]) -> None:
    st.subheader(t["report_title_ip"])
    items = _get("/reports/ip-summary", limit=200)
    if not items:
        _empty(t["report_empty_ip"])
        return

    labels = _ip_summary_labels(t)
    display = _frame(items, labels)
    st.dataframe(display, use_container_width=True, hide_index=True)
    _download(display, items, labels, "dl_ip_summary", "ip_financial_summary")

    month_col = t["month"]
    income_col = t["col_income_kzt"]
    expense_col = t["col_expense_kzt"]
    profit_col = t["col_profit_kzt"]
    metric_col = t["col_metric"]
    sum_col = t["sum"]
    long = _frame(items, labels, money=False).melt(
        id_vars=[month_col],
        value_vars=[income_col, expense_col, profit_col],
        var_name=metric_col,
        value_name=sum_col,
    )
    long[sum_col] = long[sum_col].astype(float)
    long["label"] = long[sum_col].map(lambda v: fmt_money(v))
    fig = px.bar(
        long,
        x=month_col,
        y=sum_col,
        color=metric_col,
        barmode="group",
        color_discrete_map={income_col: "#15803D", expense_col: "#B91C1C", profit_col: "#2563EB"},
        custom_data=["label"],
    )
    fig.update_traces(hovertemplate="%{x}<br>%{customdata[0]}<extra></extra>")
    fig.update_layout(
        height=300,
        margin=dict(l=0, r=0, t=10, b=0),
        legend=dict(orientation="h", yanchor="bottom", y=1.0, title=None),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
    )
    fig.update_yaxes(gridcolor="#E5E7EB", zerolinecolor="#E5E7EB", tickformat="~s")
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})


def _kaspi_reconciliation(t: dict[str, str]) -> None:
    st.subheader(t["report_title_kaspi"])
    items = _get("/reports/kaspi-reconciliation", limit=200)
    if not items:
        _empty(t["report_empty_kaspi"])
        return
    labels = {k: k for k in items[0]}
    st.dataframe(_frame(items, labels), use_container_width=True, hide_index=True)
    _download(None, items, labels, "dl_reconciliation", "kaspi_reconciliation")


def _payroll(t: dict[str, str]) -> None:
    st.subheader(t["report_title_payroll"])
    items = _get("/reports/payroll-due", limit=200)
    if not items:
        _empty(t["report_empty_payroll"])
        return
    labels = {
        "employee_name": t["employee"],
        "payment_type": t["col_pay_type"],
        "period_start": t["col_period_start"],
        "period_end": t["col_period_end"],
        "period_status": t["col_period_status"],
        "total_calculated": t["col_accrued_kzt"],
        "amount_paid": t["col_paid_kzt"],
        "amount_due": t["col_due_kzt"],
    }
    display = _frame(items, labels)
    st.dataframe(display, use_container_width=True, hide_index=True)
    _download(display, items, labels, "dl_payroll", "payroll_due")


def _order_costing(t: dict[str, str]) -> None:
    st.subheader(t["report_title_costing"])
    items = _get("/reports/order-costing", limit=200)
    if not items:
        _empty(t["report_empty_costing"])
        return
    labels = {k: k for k in items[0]}
    st.dataframe(_frame(items, labels), use_container_width=True, hide_index=True)
    _download(None, items, labels, "dl_costing", "order_costing")


def _operations(t: dict[str, str]) -> None:
    st.subheader(t["report_title_ops"])
    items = _get("/operations", limit=200)
    if not items:
        _empty(t["report_empty_ops"])
        return
    labels = {
        "id": "ID",
        "operation_date": t["col_date"],
        "type_name": t["col_type"],
        "ip_name": t["col_ip"],
        "amount": t["sum"],
        "comment": t["col_comment"],
    }
    display = _frame(items, labels)
    st.dataframe(display, use_container_width=True, hide_index=True)
    _download(display, items, labels, "dl_operations", "operations")


def _materials(t: dict[str, str]) -> None:
    """§62 «materials» — реестр материалов с текущими ценами (§48)."""
    st.subheader(t["report_title_materials"])
    items = _get("/materials", limit=200)
    if not items:
        _empty(t["report_empty_materials"])
        return
    labels = {
        "sku": "SKU",
        "name": t["col_name"],
        "unit": t["col_unit"],
        "current_price": t["col_price"],
        "supplier_name": t["col_supplier"],
        "is_active": t["col_active"],
    }
    display = _frame(items, labels)
    st.dataframe(display, use_container_width=True, hide_index=True)
    _download(display, items, labels, "dl_materials", "materials")


def _work_entries(t: dict[str, str]) -> None:
    st.subheader(t["report_title_work"])
    items = _get("/reports/work-entries", limit=200)
    if not items:
        _empty(t["report_empty_work"])
        return
    labels = {k: k for k in items[0]}
    st.dataframe(_frame(items, labels), use_container_width=True, hide_index=True)
    _download(None, items, labels, "dl_work_entries", "work_entries_detail")


def render(t: dict[str, str]) -> None:
    """§62 — все отчёты одной вкладкой; каждый со своим Excel-экспортом."""
    options = [
        t["report_opt_ip"],
        t["report_opt_kaspi"],
        t["report_opt_payroll"],
        t["report_opt_costing"],
        t["report_opt_materials"],
        t["report_opt_ops"],
        t["report_opt_work"],
    ]
    report = st.selectbox(t["report_choose"], options, key="report_selector")
    {
        t["report_opt_ip"]: _ip_summary,
        t["report_opt_kaspi"]: _kaspi_reconciliation,
        t["report_opt_payroll"]: _payroll,
        t["report_opt_costing"]: _order_costing,
        t["report_opt_materials"]: _materials,
        t["report_opt_ops"]: _operations,
        t["report_opt_work"]: _work_entries,
    }[report](t)
