# Inventory Quantity Ledger — Implementation-Ready Spec

**Date:** 2026-10-04
**Status:** SPEC ONLY — no code. Refines `docs/INVENTORY_LEDGER_DESIGN_2026-10-04.md` (prior wave) into exact DDL, trigger code, service signatures, migration plan, and test plan.
**Scope:** P2-INV-001 (stock ledger), P2-INV-003 (reservations), P2-INV-004 (WIP derivation).
**Hard constraints:** QUANTITY ONLY — no `unit_cost`, no FIFO/average (owner-blocked valuation). Immutable INSERT-only ledger. Default BLOCK on negative on-hand. IP isolation. No invented business semantics.

All FK targets below were verified to exist in `database/schema.sql` on 2026-10-04 (HEAD `8dd4b68`).

---

## 1. Exact DDL

### 1.1 `stock_movements` — the immutable quantity ledger

```sql
CREATE TABLE IF NOT EXISTS stock_movements (
    id              BIGSERIAL       PRIMARY KEY,
    ip_id           BIGINT          NOT NULL REFERENCES ip_entities (id) ON DELETE RESTRICT,
    material_id     BIGINT          NOT NULL REFERENCES materials (id) ON DELETE RESTRICT,
    order_id        BIGINT          NULL     REFERENCES orders (id) ON DELETE CASCADE,
    counterparty_id BIGINT          NULL     REFERENCES counterparties (id) ON DELETE SET NULL,
    movement_date   DATE            NOT NULL,
    quantity_delta  NUMERIC(15,3)   NOT NULL,
    movement_type   VARCHAR(30)     NOT NULL,
    ref_type        VARCHAR(30)     NULL,
    ref_id          BIGINT          NULL,
    reason          TEXT            NULL,
    created_by      BIGINT          NULL REFERENCES users (id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT chk_stock_movements_qty_nonzero CHECK (quantity_delta <> 0),
    CONSTRAINT chk_stock_movements_type CHECK (movement_type IN (
        'RECEIPT',                 -- stock IN: supplier delivery, customer return to stock
        'ISSUE',                   -- stock OUT: general issue NOT tied to a production order
        'PRODUCTION_CONSUMPTION',  -- stock OUT: tied to order_id, written by add_order_material()
        'PRODUCTION_OUTPUT',       -- stock IN: finished-goods output (product-agnostic for now)
        'RETURN',                  -- stock OUT: return to supplier (counterparty_id set)
        'ADJUSTMENT',              -- signed correction from physical count (reason required)
        'SCRAP'                    -- stock OUT: waste/offcuts, always negative (reason required)
    )),
    CONSTRAINT chk_stock_movements_sign CHECK (
        (movement_type IN ('RECEIPT', 'PRODUCTION_OUTPUT') AND quantity_delta > 0)
        OR (movement_type IN ('ISSUE', 'PRODUCTION_CONSUMPTION', 'RETURN', 'SCRAP')
            AND quantity_delta < 0)
        OR (movement_type = 'ADJUSTMENT')  -- signed; direction carried by the sign
    ),
    CONSTRAINT chk_stock_movements_reason_required CHECK (
        movement_type NOT IN ('ADJUSTMENT', 'SCRAP')
        OR (reason IS NOT NULL AND length(btrim(reason)) > 0)
    ),
    CONSTRAINT chk_stock_movements_order_scope CHECK (
        -- PRODUCTION_* movements must reference the order they belong to.
        (movement_type NOT IN ('PRODUCTION_CONSUMPTION', 'PRODUCTION_OUTPUT'))
        OR order_id IS NOT NULL
    ),
    CONSTRAINT chk_stock_movements_return_counterparty CHECK (
        movement_type <> 'RETURN' OR counterparty_id IS NOT NULL
    )
);

-- Provenance / idempotency: one movement per source row. Partial index so
-- rows without provenance (manual adjustments) are unaffected.
CREATE UNIQUE INDEX IF NOT EXISTS uq_stock_movements_ref
    ON stock_movements (ref_type, ref_id)
    WHERE ref_type IS NOT NULL AND ref_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_stock_movements_ip_material
    ON stock_movements (ip_id, material_id, movement_date);
CREATE INDEX IF NOT EXISTS ix_stock_movements_order
    ON stock_movements (order_id) WHERE order_id IS NOT NULL;
```

**Type reconciliation vs the task's 9-type list.** The task lists RECEIPT, ISSUE, RESERVATION, RELEASE, PRODUCTION_CONSUMPTION, PRODUCTION_OUTPUT, RETURN, ADJUSTMENT, SCRAP. Decision: **7 ledger types** (above) + **RESERVATION/RELEASE as reservation-table lifecycle** (§3), not ledger rows. Rationale: `SUM(quantity_delta)` must equal physical on-hand at all times; intention rows would corrupt it. Prior design's `CONSUMPTION` → renamed `PRODUCTION_CONSUMPTION` (unambiguous vs `ISSUE`); `ADJUSTMENT_IN/OUT` → single `ADJUSTMENT` (sign carries direction, one code path); `RETURN_SUPPLIER` → `RETURN` + `counterparty_id`.

**Why signed delta:** `on_hand = SUM(quantity_delta)` — one column, one aggregate, no FILTER clauses to get wrong. `NUMERIC(15,3)` matches `order_materials.quantity` precision.

**Why no `unit_cost`:** financial valuation is a separate derivation layer (owner-blocked). Frozen historical cost already lives on `order_materials.price_at_time` (§38). The ledger stays correct regardless of the valuation method chosen later.

**Why no `warehouse_id`:** no warehouse entity exists in the schema (verified — no `warehouses` table). IP is the stock owner. If multi-warehouse is needed later, it is a new entity + nullable FK, not a retrofit.

### 1.2 Trigger functions

```sql
CREATE OR REPLACE FUNCTION fn_stock_movements_integrity()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_order_ip   BIGINT;
    v_on_hand    NUMERIC(15,3);
BEGIN
    -- Concurrency: serialize check-then-insert per material. Locks the
    -- catalog row; cross-IP movements of the SAME material serialize,
    -- different materials never block each other. See §6 for alternatives.
    PERFORM 1 FROM materials WHERE id = NEW.material_id FOR UPDATE;

    -- DI-001 pattern: movement IP must equal the order's IP.
    IF NEW.order_id IS NOT NULL THEN
        SELECT ip_id INTO v_order_ip FROM orders WHERE id = NEW.order_id;
        IF v_order_ip IS DISTINCT FROM NEW.ip_id THEN
            RAISE EXCEPTION
                'stock_movements: cross-IP movement blocked (DI-001). '
                'movement ip=%, order % ip=%',
                NEW.ip_id, NEW.order_id, v_order_ip
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    -- Negative-stock gate, default BLOCK (owner may flip to warn-only;
    -- the policy is this one branch, not a schema change).
    IF NEW.quantity_delta < 0 THEN
        SELECT COALESCE(SUM(quantity_delta), 0) INTO v_on_hand
        FROM stock_movements
        WHERE ip_id = NEW.ip_id AND material_id = NEW.material_id;
        IF v_on_hand + NEW.quantity_delta < 0 THEN
            RAISE EXCEPTION
                'stock_movements: insufficient stock for material % (ip=%). '
                'on_hand=%, requested=%',
                NEW.material_id, NEW.ip_id, v_on_hand, NEW.quantity_delta
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_stock_movements_integrity ON stock_movements;
CREATE TRIGGER trg_stock_movements_integrity
    BEFORE INSERT ON stock_movements
    FOR EACH ROW EXECUTE FUNCTION fn_stock_movements_integrity();

-- DI-005 pattern, byte-for-byte the settlement_links guard.
CREATE OR REPLACE FUNCTION fn_stock_movements_immutable()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF coalesce(current_setting('app.allow_financial_delete', TRUE), 'off') = 'on' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION
        'stock_movements: rows are immutable (DI-005 pattern). '
        'Post a compensating movement (§5), do not mutate.'
        USING ERRCODE = 'check_violation';
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS trg_stock_movements_immutable ON stock_movements;
CREATE TRIGGER trg_stock_movements_immutable
    BEFORE UPDATE OR DELETE ON stock_movements
    FOR EACH ROW EXECUTE FUNCTION fn_stock_movements_immutable();
```

### 1.3 Derived views

```sql
-- Physical truth: on-hand per (IP, material).
CREATE OR REPLACE VIEW v_material_stock AS
SELECT ip_id,
       material_id,
       SUM(quantity_delta)::NUMERIC(15,3) AS on_hand,
       COUNT(*)::BIGINT                   AS movement_count
FROM stock_movements
GROUP BY ip_id, material_id;

-- Availability = on-hand minus active, unexpired reservations (§3).
CREATE OR REPLACE VIEW v_material_availability AS
SELECT s.ip_id,
       s.material_id,
       s.on_hand,
       COALESCE(r.reserved_qty, 0)::NUMERIC(15,3) AS reserved_qty,
       (s.on_hand - COALESCE(r.reserved_qty, 0))::NUMERIC(15,3) AS available_qty
FROM v_material_stock s
LEFT JOIN (
    SELECT ip_id, material_id,
           SUM(quantity)::NUMERIC(15,3) AS reserved_qty
    FROM material_reservations
    WHERE status = 'ACTIVE'
      AND (expires_at IS NULL OR expires_at > now())
    GROUP BY ip_id, material_id
) r USING (ip_id, material_id);
```

**No stored `quantity_on_hand` anywhere** — stored aggregates drift; the view is the single source.

---

## 2. Service spec

### 2.1 `record_movement()`

```python
def record_movement(
    session: Session,
    *,
    ip_id: int,
    material_id: int,
    quantity_delta: Decimal,          # signed; sign must match movement_type (§1.1)
    movement_type: str,              # one of the 7 chk_stock_movements_type values
    movement_date: date | None = None,   # defaults to today (never future-dated by service)
    order_id: int | None = None,
    counterparty_id: int | None = None,
    ref_type: str | None = None,     # e.g. 'ORDER_MATERIALS' — for idempotency
    ref_id: int | None = None,
    reason: str | None = None,       # required for ADJUSTMENT / SCRAP (DB-enforced)
    created_by: int | None = None,
) -> int:  # returns stock_movements.id
```

**Atomicity rules:**
- The function only `session.add()` + `session.flush()` — it NEVER commits. The caller owns the transaction.
- All Python-side validation (type membership, sign-vs-type, future date) happens **before** `flush()` so violations raise `ValueError` (→ 422), not DB `check_violation` (→ 500). DB constraints remain the last line of defense.
- Idempotency: when `(ref_type, ref_id)` is supplied, check existence first and return the existing id instead of inserting (protects against retried API calls before the DB unique index fires).

### 2.2 `add_order_material()` integration (P2-INV-002)

Current signature: `add_order_material(session, order_id, data, created_by=None) -> OrderMaterial`.

**Change (same transaction, after the existing `session.flush()`):**

```python
    om = OrderMaterial(...)          # existing
    session.add(om)
    session.flush()                  # existing — populates id
    session.refresh(om)              # existing

    # NEW: mirror the consumption into the quantity ledger atomically.
    record_movement(
        session,
        ip_id=order.ip_id,           # NOT a client-supplied ip — DI-001 safe
        material_id=data.material_id,
        quantity_delta=-data.quantity,
        movement_type="PRODUCTION_CONSUMPTION",
        movement_date=date.today(),  # or om.used_at.date()
        order_id=order_id,
        ref_type="ORDER_MATERIALS",
        ref_id=om.id,
        created_by=created_by,
    )
    return om
```

- If the negative-stock gate fires, the exception rolls back **both** the `order_materials` row and the movement — no half-written consumption.
- `ip_id` is derived from the order, never from the request (the existing API resolves IP access from the order already).
- `movement_date`: use `om.used_at.date()` for consistency with the backfill (§2.3).

### 2.3 Backfill plan (existing `order_materials` rows)

One-time data migration (Alembic `0019`, `op.execute`):

```sql
INSERT INTO stock_movements
    (ip_id, material_id, order_id, movement_date, quantity_delta,
     movement_type, ref_type, ref_id, reason, created_at)
SELECT o.ip_id,
       om.material_id,
       om.order_id,
       om.used_at::date,
       -om.quantity,
       'PRODUCTION_CONSUMPTION',
       'ORDER_MATERIALS',
       om.id,
       'backfill: pre-ledger consumption (migration 0019)',
       om.created_at
FROM order_materials om
JOIN orders o ON o.id = om.order_id
WHERE NOT EXISTS (
    SELECT 1 FROM stock_movements sm
    WHERE sm.ref_type = 'ORDER_MATERIALS' AND sm.ref_id = om.id
);
```

- `NOT EXISTS` guard → re-runnable, idempotent.
- **Ordering caveat:** if historical consumptions drove stock negative (no gate existed before), the backfill must run with the negative-stock gate **bypassed or ordered**. Spec: run backfill BEFORE creating `trg_stock_movements_integrity`, or insert in `used_at` order inside the migration while the trigger exists but the gate is evaluated per-row in chronological order (still fails if history ever went negative). **Decision: create the integrity trigger AFTER the backfill INSERT in the same migration transaction** — history is taken as-given; the gate applies only to new movements. Document the assumption: opening balances may be negative for legacy data; `v_material_stock` will show it honestly.
- After backfill, a verification query asserts `COUNT(*) == (SELECT COUNT(*) FROM order_materials)` for `ref_type='ORDER_MATERIALS'`.

### 2.4 New API surface (for implementer)

| Endpoint | Service | Notes |
|---|---|---|
| `POST /stock/receipts` | `record_movement(RECEIPT)` | body: material_id, quantity>0, movement_date, counterparty_id?, reason?. IP access via `ensure_ip_access`. |
| `POST /stock/issues` | `record_movement(ISSUE)` | reason recommended. |
| `POST /stock/adjustments` | `record_movement(ADJUSTMENT)` | reason REQUIRED (DB). |
| `POST /stock/scrap` | `record_movement(SCRAP)` | reason REQUIRED (DB). |
| `GET /stock/levels?ip_id=` | `v_material_stock` | VIEWER privilege, IP-scoped. |
| `GET /stock/availability?ip_id=` | `v_material_availability` | VIEWER privilege, IP-scoped. |
| `GET /reports/wip?ip_id=` | `v_wip_by_order` | §4. |
| Reservation endpoints | §3 | `POST /stock/reservations`, `POST /stock/reservations/{id}/release` |

Roles: stock writes = `SHOP_MANAGER`/`ACCOUNTANT`+ (mirror `POST /orders/{id}/materials` privilege); reads = `VIEWER`.

---

## 3. Reservation mechanics — decision

**Decision: separate `material_reservations` table, NOT ledger rows/flags.**

Rationale:
1. A reservation is an *intention*, not a physical movement. Ledger rows would corrupt the invariant `SUM(quantity_delta) = physical on-hand`.
2. A boolean flag on ledger rows (`is_reservation`) would require every on-hand query to add `WHERE NOT is_reservation` — one forgotten filter = wrong stock. Separate table makes the wrong query impossible.
3. Reservations have lifecycle metadata (expiry, release reason, status) that does not belong on an immutable ledger row.

```sql
CREATE TABLE IF NOT EXISTS material_reservations (
    id               BIGSERIAL       PRIMARY KEY,
    ip_id            BIGINT          NOT NULL REFERENCES ip_entities (id) ON DELETE RESTRICT,
    order_id         BIGINT          NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
    material_id      BIGINT          NOT NULL REFERENCES materials (id) ON DELETE RESTRICT,
    quantity         NUMERIC(15,3)   NOT NULL,
    status           VARCHAR(20)     NOT NULL DEFAULT 'ACTIVE',
    reserved_at      TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at       TIMESTAMPTZ     NULL,
    released_at      TIMESTAMPTZ     NULL,
    released_reason  VARCHAR(50)     NULL,
    created_by       BIGINT          NULL REFERENCES users (id) ON DELETE SET NULL,

    CONSTRAINT chk_reservations_qty CHECK (quantity > 0),
    CONSTRAINT chk_reservations_status CHECK (status IN ('ACTIVE', 'RELEASED', 'CONSUMED')),
    CONSTRAINT chk_reservations_release CHECK (
        (status = 'ACTIVE' AND released_at IS NULL)
        OR (status <> 'ACTIVE' AND released_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS ix_reservations_ip_material
    ON material_reservations (ip_id, material_id) WHERE status = 'ACTIVE';
CREATE INDEX IF NOT EXISTS ix_reservations_order
    ON material_reservations (order_id) WHERE status = 'ACTIVE';
```

```sql
-- DI-001 for reservations: reservation IP must equal the order's IP.
CREATE OR REPLACE FUNCTION fn_reservations_ip()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_order_ip BIGINT;
BEGIN
    SELECT ip_id INTO v_order_ip FROM orders WHERE id = NEW.order_id;
    IF v_order_ip IS DISTINCT FROM NEW.ip_id THEN
        RAISE EXCEPTION
            'material_reservations: cross-IP reservation blocked (DI-001). '
            'reservation ip=%, order % ip=%',
            NEW.ip_id, NEW.order_id, v_order_ip
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_reservations_ip ON material_reservations;
CREATE TRIGGER trg_reservations_ip
    BEFORE INSERT OR UPDATE OF ip_id, order_id ON material_reservations
    FOR EACH ROW EXECUTE FUNCTION fn_reservations_ip();
```

**Lifecycle (service layer, not triggers — business flow stays in Python):**
- `reserve_stock(session, order_id, material_id, quantity, expires_at=None, created_by)`:
  1. Load order → `ip_id` (DI-001: reservation inherits order's IP).
  2. Check `available_qty` from `v_material_availability`. **Policy: warn, don't hard-block** on over-reserve (visibility-first; hard-block is an owner decision). Over-reserve records the reservation anyway and returns `{"warning": "over_reserved", "available": X}`.
  3. INSERT ACTIVE row.
- `release_reservation(session, reservation_id, reason)`: `status='RELEASED'`, `released_at=now()`, reason ∈ (`ORDER_CANCELLED`, `MANUAL`, `EXPIRED`, `SUPERSEDED`).
- **Order CANCELLED → release all ACTIVE reservations** for that order: hook in the order-status-change service (not a DB trigger — business flow stays in the service layer, consistent with the codebase).
- `CONSUME`: `add_order_material()` marks matching ACTIVE reservations `CONSUMED` (FIFO by `reserved_at`, consuming oldest first) up to the consumed quantity; the availability re-check is skipped for the reserved portion (already accounted for).
- **Expiry is read-time**, not a background job: `v_material_availability` excludes `expires_at <= now()`. An optional janitor job may flip expired rows to `RELEASED`/`EXPIRED` for reporting hygiene — not required for correctness.

---

## 4. WIP derivation view (no new table)

```sql
CREATE OR REPLACE VIEW v_wip_by_order AS
SELECT o.id                 AS order_id,
       o.ip_id,
       o.external_order_id,
       o.status,
       COALESCE(m.material_cost, 0)::NUMERIC(15,2) AS wip_materials,
       COALESCE(w.labour_cost, 0)::NUMERIC(15,2)   AS wip_labour,
       (COALESCE(m.material_cost, 0)
        + COALESCE(w.labour_cost, 0))::NUMERIC(15,2) AS wip_total
FROM orders o
LEFT JOIN (
    SELECT order_id, SUM(total_cost) AS material_cost   -- GENERATED, frozen price_at_time (§38)
    FROM order_materials
    GROUP BY order_id
) m ON m.order_id = o.id
LEFT JOIN (
    SELECT order_id, SUM(total_sum) AS labour_cost      -- GENERATED column
    FROM work_entries
    WHERE status IN ('CONFIRMED', 'PAID')               -- DRAFT excluded, mirrors order_direct_cost()
    GROUP BY order_id
) w ON w.order_id = o.id
WHERE o.status IN ('APPROVED', 'ASSEMBLING', 'DELIVERING');
```

**Semantics (verified against `chk_orders_status`):** `NEW` (not started), `COMPLETED` (done), `CANCELLED`/`RETURNED` (abandoned) excluded. `DELIVERING` included — goods left the shop but revenue may not be recognized yet (conservative). Material cost uses frozen `price_at_time`, so WIP is historically correct even after `materials.current_price` changes.

---

## 5. Correction flow — compensating movements

The ledger is immutable (`fn_stock_movements_immutable`, DI-005 pattern). Every correction is a new row. Examples:

| Error | Compensation |
|---|---|
| RECEIPT +100, should be +90 | `ADJUSTMENT −10`, reason=`"correction of movement #<id>: counted 90, recorded 100"` |
| RECEIPT posted to wrong material A (should be B) | `ADJUSTMENT −100` on A (reason: wrong material, see #id) + `RECEIPT +100` on B |
| PRODUCTION_CONSUMPTION linked to wrong order | `ADJUSTMENT +qty` on wrong order's material (reason: wrong order) + correct `PRODUCTION_CONSUMPTION −qty` with right `order_id` + `ref_type='ORDER_MATERIALS'` |
| SCRAP overstated | `ADJUSTMENT +qty`, reason=`"scrap correction, movement #<id>"` |
| Duplicate RECEIPT (double import) | `ADJUSTMENT −qty`, reason=`"duplicate of movement #<id>"` — plus fix the import dedup that allowed it |

**Rule:** the compensating `reason` must reference the original movement id. `ADJUSTMENT` is the only type allowed to go against the sign convention (signed by design); a negative `RECEIPT` is rejected by `chk_stock_movements_sign` — corrections never masquerade as original event types.

---

## 6. Concurrency

**Threat:** two sessions concurrently consume the last units. Without locking, both `SELECT SUM(...)` checks pass, both INSERT, stock goes negative.

**Spec (trigger-level, §1.2):** `PERFORM 1 FROM materials WHERE id = NEW.material_id FOR UPDATE;` before the on-hand check. Under the default `READ COMMITTED` isolation this serializes all inserts for the same `material_id`: the second transaction's `FOR UPDATE` waits, then re-reads the committed SUM including the first transaction's row.

**Trade-offs (documented for the implementer):**
- `materials` is a **global catalog** (no `ip_id`), so movements of the same material across different IPs serialize against each other. For a small-business ERP this is acceptable and far simpler than a lock table; contention is per-material, not global.
- Alternative: `pg_advisory_xact_lock(hashtext('stock:' || NEW.ip_id || ':' || NEW.material_id))` — per-(IP, material) granularity, no catalog-row lock, but advisory locks are invisible in `pg_locks` joins and harder to debug. **Implementer may choose either; the spec requires one of them.**
- `add_order_material()` itself needs no extra locking — the trigger is the single enforcement point, so API, backfill, and future importers all share it.
- The check-then-insert is inside one statement-trigger execution; no application-level "check then insert" may duplicate the gate (it would race).

**Test requirement:** a real two-connection concurrency test (see §8, case 11) — threads + separate connections, both consuming the last units; exactly one must succeed.

---

## 7. Migration plan

**Revisions** (latest existing: `0017_accounts_bank_code`):

1. **`0018_stock_ledger.py`** — `upgrade()`:
   - `stock_movements` table + indexes (§1.1)
   - `material_reservations` table + indexes (§3)
   - `fn_stock_movements_integrity()` + trigger (created but see ordering note)
   - `fn_stock_movements_immutable()` + trigger
   - `fn_reservations_ip()` + trigger (DI-001 for reservations)
   - views `v_material_stock`, `v_material_availability`, `v_wip_by_order`
   - `downgrade()`: drop triggers → views → functions → tables, reverse order; update module `_TABLES`/`_FUNCTIONS` (see below).
2. **`0019_backfill_order_materials_consumption.py`** — data migration, the `INSERT ... WHERE NOT EXISTS` from §2.3. **Ordering:** the integrity trigger is created in 0018 but the backfill runs in 0019 *after* it — legacy history may contain negative excursions, so 0019 must `DROP TRIGGER trg_stock_movements_integrity` … run backfill … re-`CREATE TRIGGER`. All inside the migration transaction. (Alternative considered: backfill inside 0018 before trigger creation — rejected: keeps 0018 purely structural, 0019 purely data, easier to review and re-run.)

**`database/schema.sql`** (source of truth): add §3.x section with both tables, triggers, views, in the same style (numbered sections, COMMENTs).

**`conftest.py` purge order:** insert `"stock_movements"` and `"material_reservations"` immediately before `"settlement_links"` — both reference `orders`/`materials`; both are immutable (need the `app.allow_financial_delete` escape, already set for the whole purge). New order head: `..., "material_reservations", "stock_movements", "settlement_links", "operations", ...`.

**Alembic baseline `_FUNCTIONS`:** the `test_downgrade_function_list_matches_database` test compares `pg_proc.fn_%` against the downgrade revision module's `_FUNCTIONS`. Add `fn_stock_movements_integrity()`, `fn_stock_movements_immutable()`, `fn_reservations_ip()` to that list (same fix pattern as commit `fbbb160`).

**Single-head check:** `alembic heads` must show one head after both revisions.

---

## 8. Test plan (12 cases, real PostgreSQL)

**Ledger integrity:**
1. RECEIPT +100 → `v_material_stock.on_hand = 100`; PRODUCTION_CONSUMPTION −30 → 70. `SUM(quantity_delta)` equals view.
2. UPDATE and DELETE on `stock_movements` → `check_violation` (DI-005 pattern), incl. via API-less direct SQL.
3. Cross-IP: order of IP-A, movement with `ip_id`=IP-B → `check_violation` (DI-001 pattern).
4. Negative-stock gate: on-hand 10, consume 11 → rejected; consume 10 → accepted, on-hand 0.
5. Sign/type guards: `quantity_delta=0` rejected; SCRAP +5 rejected; RECEIPT −5 rejected; ADJUSTMENT/SCRAP without `reason` rejected; PRODUCTION_CONSUMPTION without `order_id` rejected; RETURN without `counterparty_id` rejected.
6. Idempotency: double `record_movement` with same `(ref_type, ref_id)` → one row (service returns existing id; unique index as backstop).

**Service/API atomicity:**
7. `POST /orders/{id}/materials` creates the CONSUMPTION movement in the same transaction: force a failure (e.g., over-consumption vs gate) → neither `order_materials` nor movement row exists.
8. Backfill: seed N `order_materials` rows pre-ledger → run migration 0019 → exactly N CONSUMPTION rows, `movement_date = used_at::date`; re-run → still N (idempotent).
9. `POST /stock/receipts` IP isolation: restricted SHOP_MANAGER of IP-A cannot receipt into IP-B (403); cross-IP `order_id` rejected.

**Reservations:**
10. Reserve 30 of on-hand 100 → `on_hand` still 100, `available_qty` 70; release → 100; expiry (`expires_at` past) → excluded from `available_qty`; order CANCELLED → reservations RELEASED.

**Concurrency:**
11. Two threads, separate DB connections, on-hand = 10, both consume 10 concurrently → exactly one commits, the other gets `check_violation`; final on-hand ∈ {0} (never −10).

**Regression:**
12. Full pytest + Ruff + mypy + `alembic upgrade head` / `downgrade -1` / `upgrade head` round-trip on a scratch DB + `test_downgrade_function_list_matches_database` green with the three new functions in `_FUNCTIONS`.

---

## 9. Explicitly out of scope (owner decisions, unchanged)

- Valuation method (FIFO / weighted average / last price) and any `unit_cost` on the ledger.
- Negative-stock policy flip (block → warn); hard-block vs warn on over-reservation.
- Purchase→stock flow (auto / separate receipt / direct-to-order) — P2-INV-008.
- Finished-goods `product_id` on movements; BOM.
- Warehouse entity.

---

## 10. Implementer checklist

- [ ] `0018_stock_ledger.py` (tables, triggers, views, `_TABLES`/`_FUNCTIONS`)
- [ ] `0019_backfill_order_materials_consumption.py` (trigger drop/recreate ordering per §7)
- [ ] `database/schema.sql` §3.x section
- [ ] `conftest.py` purge order
- [ ] `record_movement()` + `add_order_material()` integration (§2.1–2.2)
- [ ] Reservation service functions + order-cancel hook
- [ ] API endpoints (§2.4) with IP isolation + privilege checks
- [ ] `tests/test_stock_ledger.py` (cases 1–12)
- [ ] Gate: targeted → full pytest → Ruff → mypy → Alembic single-head + round-trip → `git diff --check` → commit
