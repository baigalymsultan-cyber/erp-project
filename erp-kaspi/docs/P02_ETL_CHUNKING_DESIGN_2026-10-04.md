# P0-2 ETL Chunking Design — Transaction Semantics Analysis

**Date**: 2026-10-04
**Status**: DESIGN ONLY — no code changes
**Scope**: `etl/import_excel.py::run_import()`

---

## 1. Current Transaction Boundaries

### 1.1 Call graph

`run_import()` has exactly **one production caller**:

| Caller | Transaction management |
|--------|----------------------|
| `main()` (CLI, `etl/import_excel.py:375`) | `with factory() as session, session.begin():` — ONE transaction for the entire import |
| `tests/test_etl.py` (15 tests) | `rollback_session` fixture (`conftest.py:104`) — begins a transaction, **always rolls back** |

`run_import()` itself **never commits, never rolls back** (except dry-run, where the *caller* rolls back). It relies entirely on the caller's transaction.

### 1.2 In-memory state accumulated during import

| State | Type | Scope | Purpose |
|-------|------|-------|---------|
| `batch` | `ImportBatch` ORM object | Whole run | Flushed at start to get `id` for `import_batch_id` FK |
| `existing_fps` | `set[str]` | Whole run | Pre-loaded from DB in ONE query (P0-1 fix); fingerprint duplicate guard |
| `seen_fps` | `set[str]` | Whole run | Fingerprints added by *this* run; catches in-file duplicates |
| `counters` | `dict` | Whole run | total / inserted / skipped / duplicates / errors |
| `errors` | `list[dict]` | Whole run | Row-level error details; capped at 50 in JSONB at end |

### 1.3 Idempotency guarantees (C-1/C-2) — must be preserved

1. **Fingerprint**: SHA-256 over normalized `(ip_id, date, type, amount, external_id)` — `row_fingerprint()`
2. **Pre-check**: `fingerprint in existing_fps or fingerprint in seen_fps` → skip as duplicate
3. **DB backstop**: Partial unique index `uq_operations_import_fingerprint` on `(ip_id, import_fingerprint)` — the ultimate guard
4. **TOCTOU race**: Two concurrent imports of the same file can both pass the pre-check; the loser hits the unique index → `IntegrityError` → **currently fails the entire import**

### 1.4 Batch lifecycle

```
RUNNING (created, flushed)
  → SUCCESS  (no errors)
  → PARTIAL  (some inserted, some row-level errors)
  → FAILED   (all errored, or import crashed)
  → DRY_RUN  (dry_run=True; caller rolls back everything per §38)
```

The `chk_import_batches_status` CHECK constraint allows only these five statuses.

### 1.5 Audit trail

Trigger-based (`fn_audit()` on INSERT/UPDATE/DELETE → `audit_log`). Works per-row, independent of transaction size. Chunking does not affect audit completeness.

---

## 2. What Breaks with Naive Chunking

If we simply insert `session.commit()` every 1000 rows inside `run_import()`:

| # | Breakage | Severity |
|---|----------|----------|
| 1 | **Tests break**: `rollback_session` expects zero commits; committed data survives rollback | HIGH — 15 tests |
| 2 | **Caller transaction conflict**: `main()` uses `with session.begin():`; calling `session.commit()` inside is undefined behavior in SQLAlchemy 2.x | HIGH |
| 3 | **`existing_fps` staleness**: After chunk 1 commits, chunk 2's pre-check won't see chunk 1's fingerprints in `existing_fps` | MEDIUM — but `seen_fps` (in-memory, persists across chunks) covers this |
| 4 | **Batch row visibility**: Batch counters only written at end; a crash mid-import leaves `RUNNING` with zero progress | MEDIUM |
| 5 | **Error list loss**: `errors` list is in-memory; a crash loses error details for committed chunks | LOW — errors are capped at 50; acceptable |
| 6 | **Dry-run violation**: If chunking commits during dry-run, §38 is violated (database must be untouched) | CRITICAL |

---

## 3. Atomicity Semantics: What Chunking Changes

### 3.1 Current: all-or-nothing (per import)

- 100k rows, row 99,999 has a DB error → **entire import rolled back**, zero rows persisted
- Locks held for minutes; WAL grows unbounded; OOM risk on large files
- This is the P0-2 problem

### 3.2 Proposed: per-chunk atomicity

- 100k rows in 100 chunks of 1000; chunk 57 has a DB error → chunks 1–56 **stay committed**, chunk 57+ not processed
- Each chunk is atomic (all rows in chunk commit together, or none do)
- The import as a whole is **no longer atomic** — this is an intentional semantic change

### 3.3 What "partial" means at each level

| Level | Current | Chunked |
|-------|---------|---------|
| Row validation error (bad date, unknown type) | Row skipped, counted in `errors`, import continues | **Unchanged** — row skipped, import continues |
| Chunk DB error (IntegrityError, connection blip) | Whole import fails | Chunk rolled back, already-committed chunks persist |
| Process crash / SIGKILL | Whole import lost | Committed chunks persist; batch stays `RUNNING` |

### 3.4 Batch status for interrupted imports

- `PARTIAL` = **import completed** but some rows had validation errors (existing semantic, unchanged)
- `FAILED` = **import did not complete** (crashed, DB error, interrupted)
- `RUNNING` with stale `started_at` = process died without updating status (detectable by monitoring)

**Decision**: Do NOT add a new status (avoids CHECK constraint migration). `FAILED` covers interrupted imports; `error_details` records the interruption reason.

---

## 4. Recommended Design

### 4.1 API change (backward compatible)

```python
def run_import(
    session: Session,
    rows: list[dict[str, str]],
    mapping: dict,
    *,
    ip_id: int,
    batch_name: str,
    dry_run: bool,
    batch_comment: str | None,
    chunk_size: int | None = None,  # NEW
) -> dict[str, object]:
```

- `chunk_size=None` (default): **exact current behavior** — no commits inside, caller manages transaction. All 15 existing tests pass unchanged.
- `chunk_size=N`: commit every N rows. Caller MUST NOT wrap in `session.begin()`.
- `dry_run=True`: `chunk_size` is **force-disabled** (treated as None). Double-guarded: no commits during dry-run, ever.

### 4.2 Caller restructure (`main()`)

```python
# BEFORE:
with factory() as session, session.begin():
    result = run_import(session, rows, mapping, ...)
    if args.dry_run:
        session.rollback()

# AFTER:
with factory() as session:
    # run_import manages its own commits when chunk_size is set.
    # No session.begin() — committing inside it is undefined behavior.
    result = run_import(
        session, rows, mapping,
        ...,
        chunk_size=None if args.dry_run else args.chunk_size,  # default 1000
    )
    if args.dry_run:
        session.rollback()  # §38 — still needed; run_import never commits on dry-run
```

Add CLI flag: `--chunk-size` (default 1000, `type=int`).

### 4.3 Chunk processing loop

```python
effective_chunk = None if dry_run else chunk_size

# ... existing setup: batch creation, type loading, fingerprint pre-load ...

row_iter = enumerate(rows, start=2)
chunk_buffer = []  # not needed — process directly, commit periodically

for index, row in row_iter:
    counters["total"] += 1
    try:
        # ... existing per-row logic (unchanged) ...
        # On success: session.add(Operation(...)); seen_fps.add(fp); counters["inserted"] += 1
        # On duplicate: counters["duplicates"] += 1; continue
    except Exception as exc:
        counters["errors"] += 1
        errors.append({"row": str(index), "error": f"{type(exc).__name__}: {exc}"})

    # Chunk boundary: commit every effective_chunk rows
    if effective_chunk and counters["total"] % effective_chunk == 0:
        _commit_chunk_progress(session, batch, counters, errors)

# Final commit for remaining rows
if effective_chunk:
    _commit_chunk_progress(session, batch, counters, errors)

# ... existing batch finalization (status computation, error_details cap) ...
if effective_chunk and not dry_run:
    session.commit()  # persist final batch status
```

### 4.4 Per-chunk commit with IntegrityError recovery

```python
def _commit_chunk_progress(session, batch, counters, errors):
    """Write batch progress counters and commit the chunk."""
    batch.rows_total = counters["total"]
    batch.rows_inserted = counters["inserted"]
    batch.rows_skipped = counters["skipped"]
    batch.rows_duplicates = counters["duplicates"]
    batch.rows_errors = counters["errors"]
    # Note: error_details written only at end (capped at 50); acceptable loss on crash
    try:
        session.commit()
    except IntegrityError:
        # Rare: concurrent import race on uq_operations_import_fingerprint.
        # Current behavior: entire import fails. New behavior: rollback this
        # chunk, refresh the guard set, retry once.
        session.rollback()
        _refresh_existing_fps(session, ip_id, rows)  # re-query DB
        # Caller re-processes the failed chunk's rows (now caught as duplicates)
        raise _ChunkRetryNeeded()
```

**Retry protocol** (handled by the chunk loop):
1. Catch `_ChunkRetryNeeded`
2. Remove this chunk's fingerprints from `seen_fps` (they were rolled back)
3. Re-process the chunk's raw rows (duplicates now caught by refreshed `existing_fps`)
4. If retry also raises `IntegrityError` → mark batch `FAILED`, re-raise (do not infinite-loop)

This is a **strict improvement** over current behavior (which fails the entire import on any `IntegrityError`).

### 4.5 Duplicate prevention on restart/resume

**Scenario**: Import crashes after chunk 3 of 10. Operator re-runs the same file.

| Mechanism | How it works |
|-----------|--------------|
| New `ImportBatch` row | Restart creates a new batch (new `batch_id`); the crashed batch stays `RUNNING`/`FAILED` as an audit record |
| `existing_fps` pre-load | Fresh DB query at restart start → finds fingerprints from chunks 1–3 → marks them as duplicates → skipped |
| `seen_fps` | Fresh empty set; in-memory only, not needed across restarts |
| Unique index | Backstop: if a fingerprint somehow slips through, DB rejects it |

**Result**: Only missing rows (chunks 4–10) are inserted. Fully idempotent. No resume cursor needed — the fingerprint set *is* the resume mechanism.

### 4.6 Error rows across chunks

- `errors` list stays in-memory, written to `batch.error_details` (capped at 50) at end
- On crash: error details for committed chunks are lost (in-memory)
- **Accepted**: The cap is 50; the *count* (`rows_errors`) is persisted per chunk via `_commit_chunk_progress`. Operators can re-run to regenerate details.
- Alternative (rejected): writing error_details per chunk would bloat the JSONB with duplicates and complicate the cap logic.

### 4.7 Audit trail with multiple transactions

No change needed. `fn_audit()` fires per row on INSERT. Each chunk commit generates its audit rows. The audit log will show the batch's operations arriving in chunk-sized groups — this is accurate and desirable.

---

## 5. Risks and Mitigations

| # | Risk | Likelihood | Impact | Mitigation |
|---|------|------------|--------|------------|
| R-1 | **Silent partial import**: operator thinks import succeeded, but only 3/10 chunks committed | Low | HIGH | Batch status + counters show exact progress; CLI exits non-zero on `FAILED`; log chunk progress at INFO level |
| R-2 | **`seen_fps` memory**: 100k rows → ~6.4MB of SHA-256 strings | Certain | LOW | Acceptable; document the bound. If files grow to 1M+ rows, spill to a temp table (future) |
| R-3 | **Dry-run accidentally commits** | Very low | CRITICAL | Double guard: `effective_chunk = None if dry_run`; plus assertion `assert not (dry_run and effective_chunk)` |
| R-4 | **Test regression**: existing tests assume no commits | Certain if naive | MEDIUM | Default `chunk_size=None` preserves exact behavior; zero test changes needed |
| R-5 | **Concurrent import data corruption** | Very low | HIGH | Unique index is the backstop (unchanged); retry logic handles the race gracefully instead of failing |
| R-6 | **Batch row lock contention**: per-chunk UPDATE on batch row | Low | LOW | Single-writer per import; brief row lock; no contention in practice |
| R-7 | **Interrupted batch stays RUNNING forever** | Low | MEDIUM | Document as expected; monitoring can alert on `RUNNING` batches older than N hours; manual review via `/import-batches` |
| R-8 | **Chunk boundary splits a logical group**: e.g., a transfer pair (OUT+IN) lands in different chunks | Low | MEDIUM | Transfers are created via API, not ETL; ETL rows are independent operations. If future ETL supports paired rows, add grouping key to keep pairs in the same chunk |

---

## 6. Implementation Plan

### Phase A: Core chunking (no behavior change by default)
1. Add `chunk_size: int | None = None` parameter to `run_import()`
2. Extract per-chunk commit into `_commit_chunk_progress()` helper
3. Add `IntegrityError` retry logic (single retry, then fail)
4. Force-disable chunking when `dry_run=True`
5. Verify: all 15 existing ETL tests pass with `chunk_size=None` (default)

### Phase B: CLI integration
6. Remove `session.begin()` from `main()`; pass `chunk_size` through
7. Add `--chunk-size` CLI flag (default 1000)
8. Keep dry-run rollback in `main()` (unchanged semantic)

### Phase C: New tests (TDD)
9. `test_chunked_import_commits_per_chunk` — verify data visible after chunk commit
10. `test_chunked_import_resume_after_crash` — simulate crash (rollback mid-import), re-run, verify only missing rows inserted
11. `test_chunked_import_preserves_idempotency` — duplicate file with chunking → all duplicates
12. `test_dry_run_with_chunk_size_never_commits` — `chunk_size=1000, dry_run=True` → zero rows persisted
13. `test_chunk_integrity_error_retries` — mock concurrent fingerprint collision → retry succeeds

### Phase D: Documentation
14. Update `etl/` module docstring with chunking semantics
15. Document `RUNNING`-stale-batch monitoring in ops runbook

---

## 7. Open Questions (for owner, not blockers)

1. **Chunk size default**: 1000 is a reasonable default. Should it be configurable per-IP or globally? (Recommendation: global CLI flag is sufficient for now.)
2. **Stale RUNNING batches**: Should we add an automated reaper, or is manual review sufficient? (Recommendation: manual review via existing `/import-batches` endpoint; automate later if it becomes a problem.)
3. **New `INTERRUPTED` status**: Rejected for now (avoids CHECK migration). Revisit if operators find `FAILED` ambiguous for crashed imports.

---

## 8. Summary of Recommended Approach

**Do not** commit inside `run_import()` unconditionally. Instead:

1. Add opt-in `chunk_size` parameter (default `None` = current single-transaction behavior)
2. Restructure `main()` to let `run_import()` manage transactions when chunking is enabled
3. Commit per chunk, updating batch progress counters each time
4. On `IntegrityError`: rollback chunk, refresh fingerprint guards, retry once
5. Restart/resume is free: the fingerprint unique index + pre-load query make re-runs idempotent
6. Dry-run never commits (double-guarded)
7. All existing tests pass unchanged (they use the default `chunk_size=None`)

This preserves the C-1/C-2 idempotency guarantees, fixes the P0-2 long-transaction problem, and actually *improves* robustness against concurrent-import races.
