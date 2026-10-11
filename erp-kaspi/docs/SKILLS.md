# SKILLS AUDIT + MAPPING (MASTER PROMPT §5, §56, §57)

Дата аудита: 2026-09-17 (сессия TD-016 inventory).
Источник ожидаемого списка: Master Prompt v5 §5 (обязательный список подходов)
и §56 (superpowers + production-oriented Skills должны быть изучены, классифицированы,
зарегистрированы, проверены на дубликаты).

## Расположение

- Канонический путь: `/home/workspace/Skills/<skill-name>/` (вне production source tree, §15).
- Skills НЕ входят в production archive и не входят в `Project/` (ruff-исключение
  `Skills/_extracted/*` в ruff.toml — наследие старой раскладки, пустое).
- Каждый Skill восстановлен 1:1 из workspace-архивов (не переписан).

## Инвентарь: 26/26 FOUND, frontmatter VALID (name == slug, description non-empty)

### A. Superpowers — методологические Skills (14/14, из `superpowers-main.zip`)

| # | Skill | Статус | Применение в ERP |
|---|---|---|---|
| 1 | brainstorming | FOUND | ADR/дизайн-решения (Kaspi API, DR-план) до кода |
| 2 | dispatching-parallel-agents | FOUND | параллельные подзадачи через `/zo/ask` |
| 3 | executing-plans | FOUND | исполнение планов фаз (PROGRESS.md) |
| 4 | finishing-a-development-branch | FOUND | git-завершение веток/коммитов |
| 5 | receiving-code-review | FOUND | обработка результатов аудитов |
| 6 | requesting-code-review | FOUND | ревью перед merge (Production Gate §122) |
| 7 | subagent-driven-development | FOUND | декомпозиция крупных задач (TD-016 и далее) |
| 8 | systematic-debugging | FOUND | root-cause анализ (метод TD-003 repro→fix) |
| 9 | test-driven-development | FOUND | новые фичи/API endpoints: тест до кода |
| 10 | using-git-worktrees | FOUND | изоляция экспериментов (git repo в Project) |
| 11 | using-superpowers | FOUND | входная точка: какие Skills когда |
| 12 | verification-before-completion | FOUND | evidence перед пометкой RESOLVED |
| 13 | writing-plans | FOUND | планы миграции (TD-016, TD-017) |
| 14 | writing-skills | FOUND | создание новых проектных Skills |

### B. Production stack — доменные Skills (10/10, из `production-stack-skills-main (1).zip`)

| # | Skill | Статус | Применение в ERP |
|---|---|---|---|
| 15 | production-security | FOUND | §100–104: JWT, роли, секреты, аудит |
| 16 | production-postgres | FOUND | schema.sql, EXCLUDE/триггеры, миграции, TD-003/008 |
| 17 | production-fastapi | FOUND | app/api: auth, валидация, error handling |
| 18 | production-docker | FOUND | docker/, Dockerfile hardening (TD-010) |
| 19 | production-monitoring | FOUND | мониторинг/алерты (TD-013) |
| 20 | production-error-handling | FOUND | единые коды ошибок API (финансы) |
| 21 | production-check | FOUND | Production Gate: чеклист готовности |
| 22 | production-deploy | FOUND | выкладка: pre-deploy валидация |
| 23 | production-review | FOUND | production-readiness ревью кода |
| 24 | production-planner | FOUND | планирование архитектуры production |

Master Prompt §5 в сумме называет 16 подходов (10 superpowers по имени + 6
«production …»). В репозитории skills их 24 (10 superpowers по §5 + 4
сопутствующих + 10 production) — покрытие §5 полное, «14 необходимых» из отчёта
Phase 0 = 14 SKILL.md в superpowers-архиве. Расхождений нет: все названные в §5
подходы присутствуют.

### C. Дополнительные, релевантные проекту (2, восстановлены из workspace-архивов)

| # | Skill | Источник | Применение в ERP |
|---|---|---|---|
| 25 | developing-with-streamlit | agent-skills-main.zip | frontend/ (Streamlit UI) — ключевой для TD-016 |
| 26 | excel-skill | excel-skill-main.zip | etl/import_excel.py (openpyxl-импорт, аудит openpyxl-кода) |

### D. Найдено, но НЕ Skills проекта (не восстанавливались, дубликатов не создавалось)

| Объект | Статус | Причина |
|---|---|---|
| `/home/workspace/SKILL.md` и `SKILL (1).md` | IRRELEVANT (identical files) | kaspi-*магазинный* skill (подбор товаров), не ERP Kaspi API; OQ-003 остаётся BLOCKED |
| `skills-main.zip` | NOT RESTORED | сторонний каталог (engineering/tdd и пр.) — дублирует superpowers; §57: конфликтов не создаём |
| `refactoring-ui-skill-main.zip`, `taste-skill-main.zip`, `hallmark-main.zip`, `beanpost-main.zip`, `anydesign-main.zip` и др. | NOT RESTORED | UI/дизайн-скиллы, к ERP-домену §5/§56 не относятся |

## Соответствие Requirements Matrix

Requirements Matrix (docs/REQUIREMENTS_MATRIX.md) REQ-ID на Skills не содержит —
Skills являются инженерными методологиями (§5), а не функциональными требованиями.
Mapping Skill → phase → files дан в таблицах выше; конфликтов с MASTER PROMPT
не обнаружено (§57: конфликтов нет, Skills не противоречат политикам —
в частности TDD/verification-before-completion совпадают с §4/§87/§124).

## Верификация доступности

- 26/26 `Skills/*/SKILL.md` читаются, frontmatter валиден (name == slug).
- Agent находит их по `/home/workspace/Skills/<name>/SKILL.md` перед каждой
  существенной задачей (§5).

## Использование в TD-016 (2026-09-17, финализация)

- Реально применены: writing-plans (матрица P1–P5), test-driven-development
  (тесты E5–E24 до endpoints), systematic-debugging (_seed_world-баг,
  alembic-драйф erp_migration_check), verification-before-completion
  (366/366 + ruff/mypy/alembic перед commit), code-review (grep-скан frontend),
  developing-with-streamlit (AppTest-контракты, session_state vs API-token),
  production-security (mutation-проверка IP-boundary).
- N/A для этой фазы: excel-skill, canvas-design, dating-profile, brand-guidelines
  (нет соответствующих артефактов в задаче); clarion-* (не ERP).

## Использование в TD-015 (2026-09-19)

- **Инвентаризация (Phase 0)**: 159 директорий в `/home/workspace/Skills/`, все
  с SKILL.md. Из ранее нераспакованных архивов извлечены наборы `skills-main`
  (tdd, code-review, diagnosing-bugs, codebase-design и др.), `refactoring-ui`,
  `anydesign`, `taste-skill` (13), `hallmark` — всё вне production tree.
  `production-stack` (10), `superpowers` (14), streamlit, excel — были извлечены ранее.
- Реально применены: **systematic-debugging** (мульти-процессный тест: env
  наследование + DATABASE_URL; TypeError-сигнатуры store; drift alembic 0004),
  **test-driven-development** (7 тестов TD-015: unit→integration→multi-process→
  fail-open→lock-contention; 6 тестов i18n login RU/KZ),
  **writing-plans/executing-plans** (TD015_MATRIX TD015-01…16 как план),
  **verification-before-completion** (379/379 + ruff/mypy/alembic на чистой БД
  перед commit; синхронизация матрицы TD015-05/10 с фактической fail-open
  политикой; mutation FAIL→restore→PASS на финальном коде),
  **production-postgres** (shared-таблица + индекс + opportunistic prune вместо Redis),
  **production-security** (нет FK на users — credential stuffing; секреты — только
  существующий DSN; 429 без утечки username-существования), **production-fastapi**
  (lazy import → ациклический граф импортов).
- N/A: tdd-skill дублирует test-driven-development (использован последний);
  UI/дизайн-скиллы (taste/anydesign/hallmark/canvas-design) — не ERP; clarion-* — не ERP.
