# ADR: TastyRescue Data Model (Part 02)

> Workflow for section 1: fill in 1.1, **commit and push it**, and only then
> run the load test. The git timestamp is the proof that the prediction came
> first. Every number in this file must come from our own runs.

Environment: shared course PostgreSQL on AWS RDS (ap-southeast-1, Singapore),
our tables in schema `tastyrescue`. Client: MacBook in Bangkok (GMT+7), normal
WiFi, connecting over the public internet to Singapore. Server version
(`show server_version`): **18.3**.

Measured before predicting (this is input to the prediction, not the test):

```
SELECT 1 round-trip: median 32.2 ms, min 30.7, max 108.1
BEGIN + SELECT + COMMIT through the pool: median 132.6 ms  (~4 round-trips)
```

---

## 1. Performance test: `place_order` (Task 02.5)

### 1.1 Prediction (written before running anything)

- **Expected throughput, naive mode: ~8 orders/sec** (we would not be surprised
  by anything in 6–10).
- **Reasoning:** one `place_order` is a sequence of *blocking* round-trips from
  Bangkok to Singapore, and the client waits for each one:

  | step | round-trips |
  |---|---|
  | `pool_pre_ping` (`SELECT 1` before handing out the connection) | 1 |
  | `UPDATE offers … WHERE quantity_available >= 1` | 1 |
  | `INSERT INTO orders …` | 1 |
  | `COMMIT` | 1 |

  4 × 32 ms ≈ **128 ms per order → ~7.8 orders/sec**. That matches the 132.6 ms
  we measured for an empty pooled transaction, which is the same 4 round-trips
  with no real work in them — i.e. we predict the *work* is free and the
  *waiting* is everything.

- **Where we expect the bottleneck: network latency (round-trip count), not the
  database.** Why we think the usual suspects matter less here:
  - *WAL flush on COMMIT:* an RDS fsync is ~1 ms. It is inside our 32 ms
    COMMIT round-trip, so it is ~3% of it.
  - *Server CPU / index maintenance:* the UPDATE is one row found by primary
    key, the INSERT is one row plus two small indexes. Sub-millisecond.
  - *Row locks:* the load test spreads orders over 20 offers with effectively
    unlimited stock, and it is single-threaded, so nothing ever waits on a lock.
    (Contention is what Task 02.6 measures instead.)
  - The giveaway: an empty transaction costs the same as a real one. If the
    server were the bottleneck, it would not.

- **Expected throughput, batched mode: ~2 000–3 500 orders/sec.** A batch of 500
  is *also* ~4 round-trips (pre-ping, `SET LOCAL`, one set-based statement,
  COMMIT) ≈ 130 ms, plus server time to actually write 500 rows and a bigger
  payload to push over the wire. So we expect roughly 500 orders per 150–250 ms.
  That is a predicted **~300× speedup**, far beyond the required 10×, which is
  itself the evidence for the claim above: if removing waiting buys 300×, then
  waiting was ~all of it.
- **What would falsify this:** if naive came out at, say, 40 orders/sec, our
  round-trip count would be wrong (e.g. pre-ping not firing per order); if
  batched plateaued near 200/sec, the server, not the network, would be the
  real limit.

### 1.2 Measured: naive (one order = one transaction)

```
RESULT mode=naive, n=300: 300 orders in 60.79s -> 4.9 orders/sec
RESULT mode=naive, n=300: 300 orders in 63.13s -> 4.8 orders/sec
RESULT mode=naive, n=300: 300 orders in 74.55s -> 4.0 orders/sec
```

**Was the prediction right? Half right, and the half we got wrong is the
interesting one.**

- The *number* was wrong: we said ~8 orders/sec, we measured 4.0–4.9. We were
  optimistic by a factor of ~1.8.
- The *bottleneck call was right*: it is the round-trip count, not the server.

We then measured where a single order actually spends its time, instead of
guessing why we were off:

```
checkout(pre-ping)     median   32.8 ms
UPDATE(+BEGIN)         median   65.8 ms   <-- two round-trips, not one
INSERT flush           median   34.0 ms
COMMIT                 median   33.3 ms
close(reset)           median    0.0 ms
TOTAL                          165.9 ms/order  -> 6.0 orders/sec
```

**Why we were wrong: we counted 4 round-trips, reality is 5.** We forgot that
`BEGIN` is a separate message. psycopg2 does not piggyback it onto the first
statement, so the step we wrote down as "UPDATE, 32 ms" is really "BEGIN, then
UPDATE" and costs 66 ms. Our whole prediction was `4 × RTT`; the correct model
is `5 × RTT` = 166 ms = 6.0 orders/sec.

The remaining gap between that 6.0 ceiling and the 4.0–4.9 we actually measured
is jitter: this is hotel/campus WiFi to another country on a *shared* class
server, and our own latency sample showed RTT ranging from 30.7 ms to 108 ms.
The slowest of the three runs (4.0) is the one where that jitter hurt most.

What this confirms: **server-side work is ~0**. Of the 166 ms, the database
spends maybe 1–2 ms doing the actual UPDATE and INSERT. Everything else is a
packet flying between Bangkok and Singapore.

### 1.3 Making it at least 10x faster

What we changed: all orders of a batch are sent as **one** set-based SQL
statement (`unnest` over three parallel arrays, aggregate the wanted quantity
per offer, then `UPDATE ... FROM` + `INSERT ... SELECT` in one CTE) inside
**one** transaction. Per batch of 500 that turns 2 500 round-trips and 500
commits into 5 round-trips and 1 commit. The atomic guard from
`place_order` survives: `AND o.quantity_available >= want.n`, and only offers
returned by the UPDATE get order rows.

```
RESULT mode=batched, n=5000, batch_size=100,  synchronous_commit=on:  5000 orders in  9.86s ->  506.9 orders/sec
RESULT mode=batched, n=5000, batch_size=500,  synchronous_commit=on:  5000 orders in  2.46s -> 2031.2 orders/sec
RESULT mode=batched, n=5000, batch_size=1000, synchronous_commit=on:  5000 orders in  1.70s -> 2947.8 orders/sec
RESULT mode=batched, n=5000, batch_size=500,  synchronous_commit=off: 5000 orders in  2.76s -> 1808.5 orders/sec
RESULT mode=batched, n=300,  batch_size=300,  synchronous_commit=on:   300 orders in  0.87s ->  345.2 orders/sec
```

**Speedup: 2 947.8 / 4.8 ≈ 614×** against the median naive run (required: 10×).
Correctness check, not just speed: after the runs, the perf-test offers account
for 31 255 order rows, which is exactly the 21 255 this session created plus
the 10 000 that already existed. The throughput is real inserts, not a
statement that silently matched zero rows.

**Which removed cost was responsible — round-trips, not commits.** The evidence
is the `synchronous_commit=off` line. Turning off the WAL fsync wait is the
change that targets *commit* cost, and it made things slightly **slower**
(1 808 vs 2 031 orders/sec — the difference is run-to-run noise on a shared
server, i.e. the effect is indistinguishable from zero). If the WAL flush had
been a meaningful share of the cost, removing it would have shown up. It did
not, so we reverted to the durable default.

Batch size tells the same story quantitatively. Each batch pays a fixed ~5
round-trips (~166 ms) regardless of size, so predicted throughput is
`batch_size / 0.166s`:

| batch_size | predicted | measured | ratio |
|---|---|---|---|
| 100 | 602/s | 507/s | 0.84 |
| 500 | 3 012/s | 2 031/s | 0.67 |
| 1000 | 6 024/s | 2 948/s | 0.49 |

Throughput scales almost linearly with batch size, which is the signature of a
*per-round-trip* cost being amortised. The ratio drifting down at 1000 is the
point where the other costs stop being negligible: a 1 000-row payload takes
real time to serialise and push over the wire, and the server now does enough
work per statement to be visible. That is the beginning of the crossover from
network-bound to server-bound — we just never reach it at these sizes.

Note the n=300 single-batch run (345/s): with only one batch, the fixed ~166 ms
is paid once and amortised over 300 orders instead of 5 000, which is why it
looks slower than the same batch size inside a longer run.

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

`python -m tests.isolation_test`:

```
========================================================================
6.1  ANOMALY: place_order_unsafe, READ COMMITTED, 1 bag, 2 buyers
========================================================================
Created offer 5044 with quantity_available = 1
Orders that succeeded: [10006, 10007]  rejected: []

Database state afterwards:
  offers.offer_id=5044  quantity_available=0  orders for it=2
    order_id=10006 user_id=2 pickup_code=a8fb83f2
    order_id=10007 user_id=1 pickup_code=f7f20799

>>> ANOMALY CONFIRMED: 2 orders exist for a bag that had 1 unit.
```

**The corrupted row**: `offers.offer_id = 5044` has `quantity_available = 0`,
and two order rows (10006 for user 2, 10007 for user 1) point at it. Two
customers will show up at the bakery holding a valid pickup code for the same
single bag.

The damning detail: `quantity_available = 0` is a *perfectly plausible* value.
The `CHECK (quantity_available >= 0)` constraint on `offers` does not fire,
because nothing ever went negative. One decrement was simply lost. Nothing in
the database flags this row as broken — you can only find it by comparing the
counter against `count(orders)`.

Under pytest, 6.1 fails by design and 6.2 passes
(`python -m pytest tests/isolation_test.py -v`):

```
tests/isolation_test.py::test_6_2_atomic_conditional_update_prevents_overselling PASSED [100%]
E       AssertionError: OVERSOLD: offer 5046 had 1 bag but 2 orders exist
        (quantity_available=0, which never went negative - that is why the
        CHECK constraint did not catch it)
E       assert 2 == 1
=========================== 1 failed, 1 passed in 2.93s ========================
```

Verify in DBeaver:

```sql
SELECT f.offer_id, f.quantity_available, o.order_id, o.user_id, o.pickup_code
FROM tastyrescue.offers f JOIN tastyrescue.orders o USING (offer_id)
WHERE f.offer_id IN (5044, 5046);
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
nothing. B gets `SoldOutError`. Note that the isolation *level* never changed:
the fix is in the shape of the statement, not in a stronger level.

Same race, now passing:

```
========================================================================
6.2  FIX: place_order, atomic conditional UPDATE, same race
========================================================================
Created offer 5045 with quantity_available = 1
Orders that succeeded: [10008]  rejected: ['Offer 5045 is sold out']

Database state afterwards:
  offers.offer_id=5045  quantity_available=0  orders for it=1
    order_id=10008 user_id=1 pickup_code=7e02f381

>>> PASS: one order, one clean SoldOutError, counter at 0 and never negative.
```

### 2.3 What the fix cost us

- **Lock waits:** buyers of the same offer are serialised on its row lock
  until the holder commits. Here that cost is unusually high, and the perf
  test explains why: the transaction holds the row lock across the INSERT and
  the COMMIT, which on our setup is ~100 ms of *network*, not of work. So one
  popular offer is limited to roughly 10 orders/sec no matter how many
  application servers we add. The fix for that is to shorten the transaction
  (or move the counter out of the hot row), not to weaken the guarantee.
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
| `users.email`, `payments.order_id`, `reviews.order_id` | yes (UNIQUE) | business rules ("one payment per order", "one review per order"); the index comes with the constraint, and it is what makes `pay_order` and `leave_review` safe under concurrency for free |
| `orders.pickup_code` | **no**, deliberately | we generate it as the first 8 hex chars of a UUID4 = 2^32 values. At 100 000 orders the birthday bound puts a collision at >50%, so a UNIQUE constraint here would fail load tests for a reason that has nothing to do with the business rule. Either widen the code to 16 chars and then add UNIQUE, or leave it non-unique. We left it. |
| `offers.store_id`, `orders.user_id`, `orders.offer_id` | yes (explicit) | Postgres does NOT index foreign keys automatically; needed for "offers of a store", "my orders", counting orders per offer, and FK checks on delete |
| `offers.pickup_until` | yes | "bags closing soon" query |
| `reviews.comment`, `offers.description` | no | never filtered on; an index would only slow down writes |
