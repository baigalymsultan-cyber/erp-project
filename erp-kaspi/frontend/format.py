"""§15/REQ-0008 — единая точка форматирования денег и количеств.

Один источник правды для frontend/app.py и frontend/reports.py:
формат KZT по §15 — ``1 234 567,00 ₸`` (пробелы-разделители,
запятая-десятичная). В SQL-запросах используется KZT_SQL-шаблон,
в Python — fmt_money.
"""

from __future__ import annotations

from decimal import Decimal

#: SQL-шаблон: translate(to_char(...)) → ``1 234 567,00 ₸`` для st.dataframe.
KZT_SQL = "translate(to_char({expr}, 'FM999G999G999G990D00'), ',.', ' ,') || ' ₸'"


def fmt_money(value) -> str:
    """§15 — деньги в KZT: пробелы-разделители, запятая-десятичная, знак ₸.

    TD-016: API-суммы приходят как Decimal-safe строки — принимаем и числа,
    и их строковое представление.
    """
    if value is None:
        return "— ₸"
    if isinstance(value, str):
        value = Decimal(value)
    return f"{value:,.2f}".replace(",", " ").replace(".", ",") + " ₸"


def fmt_qty(value) -> str:
    """Количество NUMERIC(15,3) без хвостовых нулей: 12,5 / 3 / 0,125."""
    if value is None:
        return "—"
    return f"{value:,.3f}".rstrip("0").rstrip(".").replace(",", " ").replace(".", ",")
