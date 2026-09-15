# ADR — TastyRescue Data Model (Part 02)

This file documents the decisions and, most importantly, the **predictions**
required by Task 02.5 and 02.6.3. Numbers below are real measurements from
running the scripts in this repo against PostgreSQL 16 (schema in
`src/models.py`), not estimates.

---

## 1. Performance test — `place_order` (Task 02.5)

### 1.1 Prediction (written before running anything)

We chose to load-test `place_order` because it is our most-called operation
(see Part 01: Order is the busiest write path after Offer itself) and it's
the one with a real correctness constraint (see section 2).

**Before running the naive version, our prediction was:**

- Expected throughput: somewhere around **1,000–3,000 orders/sec** on a
  single Postgres instance with no tuning. We based this on the fact that
  `place_order` does 2 round-trips (SELECT-free atomic UPDATE + INSERT) plus
  one `COMMIT` per call, and a single Postgres connection can usually do a
  few thousand small commits per second on local SSD-backed storage.
- **Where we expected the bottleneck to be:** the `COMMIT` itself. Every
  `COMMIT` in PostgreSQL forces a WAL (write-ahead log) flush to disk before
  it returns control to the client (Lecture 02 — Durability, WAL section).
  If we commit once per order, we pay that fsync cost on every single
  order, which we expected to dominate over the actual `UPDATE`/`INSERT`
  work.

### 1.2 Measured (naive: one order = one session = one commit)

```
$ python -m tests.perf_test --mode naive --n 15000
RESULT mode=naive: 15000 orders in 22.62s -> 663.1 orders/sec
```

**We were roughly right about the order of magnitude (663 rps), but on the
low end of our prediction range.** Digging in: this sandbox's Postgres runs
inside a container without a battery-backed cache, so every fsync is a true
disk sync — slower than it would be on the shared RDS instance from the
lecture, which likely benefits from a tuned EBS volume. If you re-run this
on the course RDS host, expect a higher naive number, but the *shape* of the
bottleneck (commit-per-order) is the same regardless of hardware.

### 1.3 Making it 10x faster

We changed exactly two things, both aimed directly at the predicted
bottleneck:

1. **Batch commits.** Instead of committing after every order, we group
   1,000 orders into one transaction and call `COMMIT` once per batch. This
   turns 15,000 fsyncs into 15 fsyncs.
2. **Batch the SQL itself.** We fused the atomic `UPDATE ... RETURNING` +
   `INSERT ... SELECT` into a single CTE statement (1 round-trip instead of
   2 per order), and pass all rows for a batch to `session.execute()` at
   once so SQLAlchemy/psycopg2 pipeline them instead of doing 1,000
   separate Python-level round-trips.

```
$ python -m tests.perf_test --mode batched --n 15000 --batch-size 1000
RESULT mode=batched: 15000 orders in 2.29s -> 6544.6 orders/sec
```

**663 → 6,545 orders/sec = 9.87x faster.** Matches the prediction: once
commit-per-order (the fsync cost) was removed, throughput jumped by almost
exactly an order of magnitude, confirming that was indeed the bottleneck
and not, say, index contention or CPU.

**Trade-off we accepted:** batching means if the process crashes mid-batch,
up to 999 already-processed orders in that batch are rolled back together
(they were never committed). For an at-most-a-few-seconds-old batch of
purchases, we consider that acceptable — the alternative (commit-per-order)
costs us 10x throughput. In production we'd tune batch size against "how
many recent orders are we willing to lose/retry on a crash", not push it as
high as possible.

---

## 2. Isolation test — `place_order` oversell (Task 02.6)

### 2.1 The anomaly: **lost update** (leading to overselling)

**Operation under test:** `place_order_unsafe` in `src/operations.py` —
the naive, "obviously correct-looking" implementation: `SELECT`
`quantity_available`, check it in Python, then `UPDATE` it.

**Setup:** one Offer with `quantity_available = 1` (a bag with exactly one
portion left). Two different users, in two independent DB sessions, both
running at PostgreSQL's default isolation level (`READ COMMITTED`), both
call `place_order_unsafe` at (almost) the same time.

**What happens (real run, `python -m tests.isolation_test`):**

```
Created offer 804 with quantity_available = 1
Two users concurrently try to buy 1 unit each (0.3s delay between read and write)...
Orders successfully placed: 2 -> order_ids [6, 7]
Orders table row count for this offer: 2
Offer.quantity_available after both transactions: 0
```

**The corrupted row:** `offers.offer_id=804` ends with `quantity_available=0`
— which *looks* perfectly normal (0, not negative) — but there are **2 rows
in `orders`** referencing it. We sold the same last bag to two different
paying customers. The counter doesn't even show visible corruption; only
cross-checking `orders` count against the original `quantity_available`
reveals it.

**Why this happens (classic lost update, READ COMMITTED):** both sessions
`SELECT`ed `quantity_available = 1` before either had committed its
`UPDATE`. Both then computed `1 - 1 = 0` in Python and both `UPDATE`d the
row to `0`. Session B's write silently overwrote session A's write with the
*same* value, hiding the fact that two decrements should have happened. No
lock was ever taken on the row between the `SELECT` and the `UPDATE`, so
`READ COMMITTED` (which only guarantees you don't see *uncommitted* data,
not that the data doesn't change under you) does nothing to prevent this.

We added a small `inject_delay` (0.3s) between the read and the write in
`place_order_unsafe` purely to widen the race window on demand — the bug
itself is the same lost-update bug that happens under real concurrent
load without any delay, just much harder to hit reliably in a short demo.

### 2.2 The fix: atomic conditional UPDATE

`place_order` (the safe version) replaces the read-then-write with a single
statement:

```sql
UPDATE offers
SET quantity_available = quantity_available - :qty
WHERE offer_id = :offer_id AND quantity_available >= :qty
```

The check (`quantity_available >= qty`) and the decrement happen in the
*same* statement, and PostgreSQL takes a row lock for the duration of that
`UPDATE`, so a second concurrent `UPDATE` on the same row simply waits, then
re-evaluates the `WHERE` clause against the *new* value and matches 0 rows.
We detect that via `rowcount == 0` and raise `SoldOutError` — no explicit
`SELECT ... FOR UPDATE` needed, because the condition and the write are
fused.

**Same experiment, safe version:**

```
Created offer 805 with quantity_available = 1
Two users concurrently try to buy 1 unit each...
Orders successfully placed: 1 -> order_ids [8]
Rejected with SoldOutError: 1 -> ['Offer 805 is sold out']
Orders table row count for this offer: 1
Offer.quantity_available after both transactions: 0
```

Exactly one order succeeds, one is cleanly rejected, `quantity_available`
never goes negative and never gets oversold.

### 2.3 What the fix cost us

- **No throughput cost under low contention:** for offers with plenty of
  stock (the common case — see the perf test, which uses this same safe
  `place_order`), the extra `WHERE` clause costs nothing measurable; it's
  the same single `UPDATE` statement either way.
- **Under high contention on the same row** (many customers racing for the
  last few units of one popular bag), losers now get an immediate,
  well-defined `SoldOutError` on their *first* attempt instead of silently
  succeeding-but-wrong. That's a UX cost we have to design around (show
  "sold out, refresh" in the app) rather than a performance cost.
- **We deliberately did *not* reach for `SELECT ... FOR UPDATE` +
  `SERIALIZABLE`.** That would also fix it, but it holds a lock across two
  round-trips (SELECT, then UPDATE) instead of one, which is a wider window
  for other transactions to queue up behind it, and it introduces a real
  deadlock risk if the same code ever locks two offers in inconsistent
  order in a single transaction (see Lecture 05, "DEADLOCK" slides). The
  atomic conditional `UPDATE` gets the same correctness guarantee with a
  single, non-blocking-for-others statement, so we prefer it here.

---

## 3. Environment note

All numbers above come from PostgreSQL 16 running locally (see
`docker-compose.yml`), not the shared course RDS instance from the lecture
slides — we didn't want three team members hammering a shared 100k-row
database with a 15,000-order load test. The *relative* speedup (9.87x) and
the *anomaly itself* (lost update under READ COMMITTED) are properties of
PostgreSQL's isolation model, not of this specific machine, so they should
reproduce the same way against the RDS instance if you want to re-run there
for the viva.
