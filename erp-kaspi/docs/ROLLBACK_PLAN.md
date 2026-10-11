# ROLLBACK PLAN (§133, REQ-0333)

Каждый production release имеет стратегию отката для трёх уровней:
database migration, application deployment, configuration (§133).
План построен на фактической инфраструктуре репозитория: Alembic baseline
`0001_baseline_schema`, `scripts/backup.sh` / `scripts/restore.sh`,
docker-compose (`docker/`), env-only конфигурация (§10).

---

## 1. Database migration rollback

### 1.1 Принцип (§16)

`operations`, `kaspi_payouts`, `payroll_payments` — append-only. **Downgrade
миграции не применяется как штатный механизм**: `downgrade()` baseline-миграции
сбрасывает всю схему и по умолчанию отказывается выполняться
(`ERP_ALLOW_BASELINE_DOWNGRADE=1` — только осознанный ручной запуск, ADR-011).

### 1.2 Штатный откат миграции = restore из бэкапа

1. Остановить API: `docker compose stop api`.
2. Выбрать последний проверенный дамп: `backups/erp_<env>_YYYYMMDD_HHMMSS.dump`
   (целостность подтверждена парным `.sha256`).
3. Восстановить: `bash scripts/restore.sh` (пересоздаёт БД из дампа).
4. Свести версию схемы к коду: если rollback кода уводит на предыдущий релиз —
   `alembic stamp <previous_revision>` после restore (схема уже соответствует
   предыдущему релизу, повторный `upgrade head` НЕ запускать).
5. Запустить API, проверить `/health` и смоук-логин.

RPO/RTO: RPO = интервал расписания бэкапов (REQ-0326, расписание включается
командой пользователя); RTO = один restore-прогон (~минуты, проверено
`tests/test_backup_restore.py`: round-trip дампа со сравнением строк 12
бизнес-таблиц и контрольной суммы).

### 1.3 Forward-only исключение

Если миграция только добавляет объекты (индексы, view) и не трогает
финансовые таблицы — допустим `alembic downgrade -1` с последующим
`alembic upgrade head` исправленной миграцией. Критерий: миграция не содержит
`DROP`/`DELETE`/`TRUNCATE` над `operations`, `kaspi_payouts`,
`payroll_payments`, `order_items`, `work_entries`, `audit_log`.

---

## 2. Application deployment rollback

### 2.1 Артефакты релиза

Образ `erp-backend` (non-root, healthcheck, docker/Dockerfile). Перед каждым
деплоем текущий образ помечается тегом релиза: `erp-backend:<release>` и
`erp-backend:previous`. Previous-тег — и есть точка отката.

### 2.2 Процедура отката

1. `docker compose stop api`.
2. В `docker/docker-compose.yml` образ не фиксируется тегом в git — откат
   выполняется перезапуском предыдущего образа:
   `docker run -d --env-file .env --name erp-api erp-backend:previous`.
   (Для compose-инсталляций: править `image:` на `erp-backend:previous`.)
3. Дождаться healthcheck: `curl -fsS http://127.0.0.1:8000/health` →
   `"status":"ok"`.
4. Смоук: `/auth/login` валидным пользователем, `/monitoring/summary` (§72)
   — убедиться в отсутствии роста `errors_5xx_since_start`.

### 2.3 Совместимость схемы и кода

Контракт: миграции выполняются **до** запуска новой версии (CMD выполняет
`alembic upgrade head` перед uvicorn). Откат кода на предыдущий образ после
миграций возможен только если предыдущая версия совместима с новой схемой —
поэтому новые миграции должны быть additive (§16, §65: изменения схемы только
миграциями). Миграции с разрушающими изменениями требуют совестного
релиза кода и помечаются в описании миграции; их откат — только по §1.2
(restore).

---

## 3. Configuration rollback

### 3.1 Источник конфигурации

Только env (§10): `DATABASE_*`, `APP_JWT_SECRET`, `APP_ENV`, Kaspi-ключи.
Секреты не хранятся в git (.gitignore, REQ-0329), поэтому откат конфигурации
выполняется оператором из внешнего хранилища секретов, а не из репозитория.

### 3.2 Процедура

1. Перед деплоем сохранить текущий `.env` во внешнем защищённом хранилище
   (две последние известные рабочие версии: `.env.release-N`,
   `.env.release-N-1`).
2. Откат: остановить API → восстановить `.env` предыдущего релиза →
   запустить API → `/health`.
3. Проверить, что секрет текущего релиза не остался в окружении процесса
   (перезапуск процесса обязателен — uvicorn читает env на старте).

### 3.3 Особые случаи

- Смена `APP_JWT_SECRET` инвалидирует все выданные JWT (HS256) — пользователи
  перелогиниваются; откат секрета возвращает силу старым токенам.
- Смена `DATABASE_*` — фактически откат на другой инстанс БД; сопровождать
  §1.2, если целевая БД отстаёт по схеме.

---

## 4. Verification (acceptance)

| Проверка | Метод | Статус |
|---|---|---|
| Restore из дампа | `tests/test_backup_restore.py` (round-trip, 12 таблиц, sha256) | TESTED |
| Миграция на чистой БД | `alembic upgrade head`, повторный upgrade (§87) | TESTED |
| Миграция поверх готовой схемы | тесты `tests/test_alembic_baseline.py` | TESTED |
| Health после старта | healthcheck образа + `/health` (§70) | TESTED |
| Downgrade защищён | `ERP_ALLOW_BASELINE_DOWNGRADE` guard, тест | TESTED |

## 5. Incident-порядок (кратко)

1. Остановить источник ошибки (API процесс), сохранить дамп текущего
   состояния БД до любых действий.
2. Определить уровень: код / миграция / конфигурация — по логам
   (`/dev/shm`-логи в dev, request-логи §71) и `/monitoring/summary`.
3. Выполнить соответствующий раздел (1/2/3), не смешивая уровни.
4. После восстановления — повторить смоук (health, login, monitoring) и
   зафиксировать инцидент в `docs/TECHNICAL_DEBT.md` / ADR при изменении
   финансовых семантик (§54).
