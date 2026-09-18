# ADR: TastyRescue Data Model (Part 02)

> Workflow for section 1: fill in 1.1, **commit and push it**, and only then
> run the load test. The git timestamp is the proof that the prediction came
> first. Every number in this file must come from our own runs.

Environment: shared course PostgreSQL on AWS RDS (ap-southeast-1, Singapore),
our tables in schema `tastyrescue`. Client: _(whose laptop, which network,
Bangkok/other)_. Server version (from `python -m src.db`): _..._

---

## 1. Performance test: `place_order` (Task 02.5)

### 1.1 Prediction (written before running anything)

- **Expected throughput, naive mode:** _... orders/sec_
- **Reasoning:** _how many network round-trips does one `place_order` need?
  (pool pre-ping, UPDATE, INSERT, COMMIT) What is the round-trip time from
  our laptop to the server? (`ping` is blocked on RDS, so estimate it or
  time `SELECT 1` in a loop.) What does that imply for orders/sec?_
- **Where we expect the bottleneck:** _network latency? the COMMIT / WAL
  flush? CPU on the server? index maintenance? row locks? Say which one and
  why the others matter less._
- **Expected throughput, batched mode, and why:** _..._

### 1.2 Measured: naive (one order = one transaction)

```
paste the exact output here (run it 3 times, the server is shared)
```

Was the prediction right? If not, why not? _..._

### 1.3 Making it at least 10x faster

What we changed: all orders of a batch are sent as **one** set-based SQL
statement (`unnest` over arrays, then `UPDATE ... FROM` + `INSERT ... SELECT`
in one CTE) inside **one** transaction. Per batch of 500 that turns roughly
_N_ round-trips and 500 commits into 1 round-trip and 1 commit.

```
paste output of batched runs here (try batch sizes 100, 500, 1000)
```

Explanation of the delta: _which of the removed costs (round-trips vs.
commits) was responsible for most of the speedup, and how do you know?_

Trade-offs we accept:
- Orders in a batch are committed together, so a crash loses the whole
  uncommitted batch, and the customer waits until the batch is flushed.
- Inside one batch, all orders for the same offer succeed or fail together.
- With several concurrent batch writers, offers must be locked in a fixed
  order, otherwise deadlocks become possible.

---

## 2. Isolation test: overselling the last bag (Task 02.6)

### 2.1 The anomaly: lost update, leading to a double booking

`place_order_unsafe` reads `quantity_available`, checks it in Python and
writes back the value Python computed. Two customers, two connections,
`READ COMMITTED`, offer with 1 bag left:

| time | Transaction A                       | Transaction B                       |
|------|-------------------------------------|-------------------------------------|
| t1   | SELECT qty -> 1                     |                                     |
| t2   |                                     | SELECT qty -> 1                     |
| t3   | UPDATE offers SET qty = 0           |                                     |
| t4   | INSERT order, COMMIT                |                                     |
| t5   |                                     | UPDATE offers SET qty = 0 (waited for A's lock, then overwrites) |
| t6   |                                     | INSERT order, COMMIT                |

Both succeed. B's write overwrites A's decrement: that is the lost update.
The counter shows a harmless-looking 0, but two orders exist for one bag.
The CHECK `quantity_available >= 0` does not catch it because the value
never goes negative.

The test puts a `threading.Barrier` between the SELECT and the UPDATE so that
t1 and t2 both happen before t3. The barrier does not create the bug, it
forces an interleaving that happens by chance under real load.

Output of `python -m pytest tests/isolation_test.py -v` and
`python -m tests.isolation_test`:

```
paste here
```

Corrupted rows (query from DBeaver, screenshot optional):

```sql
SELECT f.offer_id, f.quantity_available, o.order_id, o.user_id, o.pickup_code
FROM tastyrescue.offers f JOIN tastyrescue.orders o USING (offer_id)
WHERE f.offer_id = <offer_id printed by the test>;
```

### 2.2 The fix: atomic conditional UPDATE

```sql
UPDATE offers
SET quantity_available = quantity_available - :qty
WHERE offer_id = :offer_id AND quantity_available >= :qty
RETURNING offer_id
```

Check and decrement are one statement. B's UPDATE blocks on A's row lock;
when A commits, Postgres (still at READ COMMITTED) re-evaluates B's WHERE
clause against the new row version, finds `0 >= 1` false, and updates
nothing. B gets `SoldOutError`. Same test, now passing: _paste output_.

### 2.3 What the fix cost us

- **Lock waits:** buyers of the same offer are serialised on its row lock
  until the holder commits. The longer the transaction after the UPDATE
  (INSERT, COMMIT, network), the longer others wait. _(optional: measure
  with many threads on one offer)_
- **Failures become visible:** the loser gets `SoldOutError` and the app
  must handle it ("sold out"). No retries needed, unlike `SERIALIZABLE`.
- **Alternatives considered:**
  - `SELECT ... FOR UPDATE`: also correct, but holds the lock across an
    extra round-trip, and locking several offers in different orders can
    deadlock.
  - `REPEATABLE READ` / `SERIALIZABLE`: B aborts with a serialization
    error (40001), so we would need retry logic and waste work under
    contention.
- **Remaining risk:** the conditional UPDATE only protects this one
  counter. Any new code path that writes `quantity_available` with
  read-then-write reintroduces the bug.

---

## 3. Index decisions

| Column | Indexed? | Why |
|---|---|---|
| all `*_id` primary keys | yes (automatic) | lookups by id, used by every operation |
| `users.email`, `payments.order_id`, `reviews.order_id`, `orders.pickup_code` | yes (UNIQUE) | business rules; the index comes with the constraint |
| `offers.store_id`, `orders.user_id`, `orders.offer_id` | yes (explicit) | Postgres does NOT index foreign keys automatically; needed for "offers of a store", "my orders", counting orders per offer, and FK checks on delete |
| `offers.pickup_until` | yes | "bags closing soon" query |
| `reviews.comment`, `offers.description` | no | never filtered on; an index would only slow down writes |
