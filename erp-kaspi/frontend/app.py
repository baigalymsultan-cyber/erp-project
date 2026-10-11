"""
Streamlit dashboard (MASTER PROMPT §7, §58–62).

§58 — RU/KK переключение языка, все суммы в KZT.
§15 — денежный формат ``1 234 567,00 ₸``: единая точка форматирования
      (fmt_money в Python, to_char-шаблон в SQL для таблиц).
§60 — sidebar с 10 обязательными модулями: Операции, Импорт, Банки,
      Заказы, Товары, Материалы, Производство, Зарплата, Отчёты, Настройки.
§61 — форма бригадира: Employee → Work Type → Order → Quantity → Date →
      Save; ставка определяется автоматически (frozen at INSERT).
§62 — отчёты с экспортом в Excel (frontend/reports.py).

TD-016 P5: UI не открывает БД вообще. Все данные приходят через
frontend/api_client → FastAPI (авторизация, изоляция ИП, аудит — §53/§54
на сервере). Единственная запись из UI — выработка бригадира — POST
/payroll/work-entries, тот же service layer add_work_entry (§61: ставка
замораживается сервисом, не в UI).

UX-контракт: фильтры → KPI → график → таблица (сверху вниз), пагинация
на стороне API (LIMIT/OFFSET + total), явные empty states, денежные суммы
не обрезаются молча — лимит страницы виден пользователю.
"""


from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from frontend.auth_gate import (
    current_auth,
    has_min_role,
    login_gate,
    sidebar_user_block,
)
from frontend.i18n import I18N
from frontend.pages.banks import module_banks
from frontend.pages.import_ import module_import
from frontend.pages.materials import module_materials
from frontend.pages.operations import module_operations
from frontend.pages.orders import module_orders
from frontend.pages.payroll import module_payroll
from frontend.pages.production import module_production
from frontend.pages.products import module_products
from frontend.pages.reports import module_reports
from frontend.pages.settings import module_settings
from frontend.theme import THEME_CSS

st.set_page_config(page_title="ERP — Kaspi / Финансы", page_icon="⢕", layout="wide")


MODULE_SUBTITLES = {
    "ru": {
        "Операции": "Реестр финансовых операций — фильтры, поиск, пагинация",
        "Импорт": "Пакеты импорта Excel/Kaspi (ETL) и их результаты",
        "Банки": "Счета ИП, выплаты Kaspi и журнал синхронизаций",
        "Заказы": "Заказы Kaspi Shop: статусы, суммы, себестоимость",
        "Товары": "Каталог товаров и цены продажи",
        "Материалы": "Материалы и расход по заказам",
        "Производство": "Выработка бригадира и учёт сдельных работ",
        "Зарплата": "Расчётные периоды и задолженность по выплатам",
        "Отчёты": "Сводные отчёты на вьюхах §63 с экспортом",
        "Настройки": "Окружение (§10) — без секретов в UI",
    },
    "kk": {
        "Операции": "Қаржы операцияларының тізілімі — сүзгі, іздеу, беттеу",
        "Импорт": "Excel/Kaspi импорт пакеттері (ETL) және нәтижелері",
        "Банки": "ШБ шоттары, Kaspi төлемдері және синхронизация журналы",
        "Заказы": "Kaspi Shop тапсырыстары: күйлер, сомалар, өзіндік құн",
        "Товары": "Тауарлар каталогы және сату бағалары",
        "Материалы": "Материалдар және тапсырыстар бойынша шығын",
        "Производство": "Шебер шығарылымы және кесімді жұмыстарды есепке алу",
        "Зарплата": "Есептік кезеңдер және төлемдер бойынша қарыз",
        "Отчёты": "§63 көріністеріндегі жиынтық есептер экспортпен",
        "Настройки": "Орта (§10) — құпиясөздерсіз",
    },
}


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


MODULE_RENDERERS = {
    "Операции": module_operations,
    "Импорт": module_import,
    "Банки": module_banks,
    "Заказы": module_orders,
    "Товары": module_products,
    "Материалы": module_materials,
    "Производство": module_production,
    "Зарплата": module_payroll,
    "Отчёты": module_reports,
    "Настройки": module_settings,
}


#: §53 — минимальная роль для административных модулей UI.
ADMIN_MODULE_ROLES = {
    "Сотрудники": "HR",
    "Мониторинг": "ADMIN",
    "Пользователи": "ADMIN",
    "Аудит": "DIRECTOR",
}


def render_admin_module(lang: str, name: str) -> None:
    """Lazy import — admin_modules импортирует frontend.app (kpi_row и др.)."""
    import frontend.admin_modules as admin

    {
        "Сотрудники": admin.module_employees,
        "Мониторинг": admin.module_monitoring,
        "Пользователи": admin.module_users,
        "Аудит": admin.module_audit,
    }[name](lang)


def main() -> None:
    # TD-014/TD-016: ни одна строка данных не рендерится без аутентификации.
    auth = current_auth()
    if auth is None:
        login_gate(THEME_CSS)
        return

    st.markdown(THEME_CSS, unsafe_allow_html=True)

    # §58 — язык интерфейса RU/KK.
    lang = st.sidebar.radio(
        "🌐",
        ["ru", "kk", "en"],
        format_func=lambda v: {"ru": "Русский", "kk": "Қазақша", "en": "English"}[v],
        key="lang",
    )
    t = I18N[lang]

    sidebar_user_block(lang)

    # §60 — sidebar: ровно 10 модулей в порядке ТЗ.
    module = st.sidebar.radio(t["modules"], MODULES, format_func=lambda m: t[m], key="module")
    st.sidebar.caption("MASTER PROMPT v3.0 — §58–62")

    # §53 — административные модули видны только по роли; проверка на той же
    # иерархии, что и в API (auth_gate.has_min_role).
    admin_modules = [
        name for name, minimum in ADMIN_MODULE_ROLES.items() if has_min_role(auth, minimum)
    ]
    admin_sel = None
    if admin_modules:
        st.sidebar.markdown(f"**{t['admin']}**")
        admin_sel = st.sidebar.selectbox(
            t["admin"],
            [None, *admin_modules],
            format_func=lambda v: "—" if v is None else t.get(v, v),
            key="admin_module",
        )

    st.title(f"⢕ {t[module]}")
    st.markdown(
        f'<div class="erp-sub">{MODULE_SUBTITLES[lang][module]}</div>', unsafe_allow_html=True
    )

    if admin_sel is not None:
        render_admin_module(lang, admin_sel)
        return

    MODULE_RENDERERS[module](lang)

if __name__ == "__main__":
    main()
