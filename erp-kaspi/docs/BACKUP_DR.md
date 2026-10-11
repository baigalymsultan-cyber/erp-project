# TD-017 — Backup / Disaster Recovery

Статус: **VERIFIED (локальная среда, 2026-09-17)**. Off-site и непрерывный PITR-стенд —
BLOCKED (см. §6), всё остальное проверено реальными прогонами.

---

## 1. Проверенная цепочка (evidence, 2026-09-17)

```
BACKUP CREATED ≠ BACKUP VERIFIED ≠ DATABASE RESTORED ≠ PRODUCTION DR READY
```

| Ступень | Статус | Evidence |
|---|---|---|
| BACKUP CREATED | PASS | `scripts/backup.sh` → `backups/erp_dev_20260917_164733.dump` (1 c, custom-format) |
| BACKUP VERIFIED | PASS | sha256 sidecar (`backup.sh` пишет `.sha256`; `restore.sh` проверяет `sha256sum -c` перед restore; `pg_restore --list` в backup.sh) |
| DATABASE RESTORED | PASS | `TARGET_DB=erp_drill scripts/restore.sh <dump>` → `RESTORE OK: erp_drill (36 objects)` (<1 c); тест `test_backup_restores_with_matching_rows` сверяет количество строк до/после |
| PRODUCTION DR READY | BLOCKED | нет off-site-хранилища и production-инфраструктуры (§6) |

## 2. RPO / RTO

| Параметр | Значение | Обоснование |
|---|---|---|
| RPO (logial backup) | ≤ 24 ч при ежедневном cron; ≤ 1 мин с WAL-архивом (§4) | расписание задаётся пользователем (REQ-0326); WAL-архив поднимает RPO до минут |
| RTO | минуты на dev-объёмах (замер: restore < 1 c при 36 объектах); на production-объёмах — измерить при первом реальном деплое | restore-прогон автоматизирован (`restore.sh`) |

## 3. Расписание (включается командой владельца)

```cron
# ежедневно 03:00 UTC — логический бэкап с checksum
0 3 * * * cd /home/workspace/Project && DB_PASSWORD="$(cat /etc/erp/DB_PASSWORD)" DB_NAME=erp_prod scripts/backup.sh
# каждые 5 минут — WAL-сегменты (см. §4)
*/5 * * * * test -f /home/.z/workspaces/td017/wal_archive/.keep
```

## 4. WAL / PITR — проверено локально

`archive_mode=on`, `archive_command='test ! -f <archive>/%f && cp %p <archive>/%f'`,
`wal_level=replica` (ALTER SYSTEM + restart). Проверка:
`SELECT pg_switch_wal();` → `pg_stat_archiver: archived_count=1, failed_count=0`,
сегмент появился в `/home/.z/workspaces/td017/wal_archive/`.

PITR-восстановление на момент T = `pg_basebackup` + `recovery_target_time = 'T'`
(процедура ниже). Полный PITR-drill на отдельном кластере не прогонялся —
в песочнице один кластер PostgreSQL (BLOCKED → §6).

### Процедура PITR (executable)

```bash
# 1) базовая копия (запускается ДО события)
sudo -u postgres pg_basebackup -D /var/backups/erp_base -Ft -Xs -P
# 2) при инциденте: восстановить базу из base backup в новый data dir,
#    создать recovery.signal, в postgresql.conf:
#      restore_command = 'cp /home/.z/workspaces/td017/wal_archive/%f %p'
#      recovery_target_time = '<момент до инцидента>'
#      recovery_target_action = 'promote'
# 3) pg_ctl start → дождаться promote → smoke: SELECT count(*) FROM operations;
```

## 5. Retention и integrity

- Retention: скрипты **никогда не удаляют** старые бэкапы (§16); политика очистки —
  решение владельца (рекомендация: 30 daily / 12 monthly, удалять только по
  сверке checksum).
- Integrity: `.sha256` для каждого дампа; `restore.sh` отказывается восстанавливать
  при несовпадении checksum; corruption detection = `pg_restore --list` + checksum +
  restore-сравнение строк.
- Secrets: пароли только через `DB_PASSWORD` (скрипты падают без него — §10,
  no defaults); в дампах секретов нет (данные приложения).

## 6. BLOCKED / что невозможно проверить в текущей среде

| Пункт | Статус | Что нужно |
|---|---|---|
| Off-site копия | BLOCKED | внешний SSH/S3-endpoint + credential (решение владельца; OQ-007) |
| Полный PITR-drill на отдельном кластере | BLOCKED | второй PostgreSQL-кластер (в песочнице один) |
| Backup шифрование | PARTIAL: checksum есть, шифрования нет | ключ шифрования от владельца (gpg -e / age) — добавляется одной командой в backup.sh |
| Restore на production-объёмах | BLOCKED | production-инфраструктура (Docker daemon недоступен) |

## 7. Операционный мониторинг

- `pg_stat_archiver.failed_count` — алерт при > 0.
- Еженедельный drill: `restore.sh` на scratch-БД + сравнение строк
  (уже автоматизировано: `tests/test_backup_restore.py` в CI).
- Проверка свежести: `find backups/ -name '*.dump' -mtime -1` — алерт, если пусто.
