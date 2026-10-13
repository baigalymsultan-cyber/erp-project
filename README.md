# erp-project

ERP для малого бизнеса Казахстана (Kaspi Shop/Pay, финансы, заказы, мебельное производство, сдельная зарплата) + авторские скиллы.

## Структура

- `erp-kaspi/` — код проекта. Backend: Python 3.11+, FastAPI, PostgreSQL 15+, SQLAlchemy 2.x, Alembic. Frontend: Streamlit + Plotly.
- `skills/` — 8 авторских скиллов: neon, railway, coderabbit, betterstack, snyk, arena, apinex, erp-kaspi-guard.
- `prompts/` — `Project_PromPt.txt` (действующий Master Prompt), `FINANCE-OPS-001.txt` (архивный baseline).

## Ветки

- **main** — erp-kaspi на базе `arch-refactor-001` (646/646 тестов PASS на реальном PostgreSQL). Основная ветка.
- `reversal-impl-001` — PROPOSED: компенсирующая модель reversal (миграция 0024, 14/15 тестов). Ждёт confirm/override по Q1–Q7.
- `td020-impl-001` — PROPOSED: derived PAID статус периода (6/6 тестов). Ждёт confirm/override по TD-020.

## Оговорки

- Истории коммитов нет — залиты деревья файлов через GitHub API. Полная история с коммитами — в `erp-kaspi-branches.bundle` (передан отдельно).
- `database/schema.sql` на ветке reversal превышает лимит API на размер файла — все изменения схемы содержатся в миграции `0024_reversal_model`.
- Сознательно не залито: распакованные skill-архивы (`skills-new/`, `ecc-skills/`), установленные клиенты, `.venv`, кэши.

## Открытые решения

- [ ] CI по `arch-refactor-001` (push с локальной машины → GitHub Actions)
- [ ] Confirm/override: reversal Q1–Q7, TD-020
- [ ] Платформа деплоя (Railway подготовлен: проект `erp-kaspi`, сервисы `erp-api` + `erp-frontend` созданы)
