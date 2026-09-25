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

---
---

# Part 03: Redis for the view counter

> Same workflow as Part 02: section 03.1 (the predictions) is filled in,
> **committed and pushed**, and only then does anything get measured. The git
> timestamp is the proof. Every number below comes from our own runs.

**The entity we moved:** the *view* of an offer — a customer opening the detail
page of a surprise bag. Not the order, not the payment. Section 03.6 argues why
that line is where it is.

**The sequence under test** (`src/views.py`, one "page view" = three steps):

1. read the offer and its store (the page content)
2. count the view (`offers.view_count`)
3. record the event (`offer_views`)

**Setup.** Redis 7 and Postgres 16 in docker on the MacBook, plus the shared
course RDS in Singapore. Measuring against *both* Postgres instances is
deliberate: Redis runs locally, so "Redis vs. RDS" would mostly be a
measurement of the flight to Singapore, not of the two databases.

Primitives measured before predicting (inputs to the prediction, not the test):

```
rds    SELECT 1 on an open connection        median  39.2 ms
rds    pooled BEGIN + SELECT + COMMIT        median 165.6 ms   (~4 round-trips)
local  SELECT 1 on an open connection        median   0.20 ms
local  pooled BEGIN + SELECT + COMMIT        median   0.80 ms
redis  PING                                  median  0.147 ms
redis  INCR                                  median  0.130 ms
redis  4-command pipeline (the whole sequence) median 0.152 ms
```

The last two lines are the entire thesis of Part 03: **four Redis commands cost
the same as one**, because a pipeline is one packet out and one packet back.
We are not buying a faster database, we are buying fewer waits.

## 03.1 Predictions (written before running anything)

### Task 1 — base NoSQL write throughput (`tests/perf_nosql.py`, XADD)

| mode | prediction | reasoning |
|---|---|---|
| naive, 1 thread | **~6 000 records/s** (5 000–8 000) | 0.147 ms per round-trip + ~0.02 ms client work ≈ 0.17 ms per record |
| pipeline 1 000, 1 thread | **~60 000 records/s** (40 000–120 000) | the round-trip is amortised away; what is left is redis-py building command buffers in Python, ~15–20 µs per command |
| pipeline 1 000, 8 threads | **~100 000 records/s** | only ~1.5×, not 8×: the encoding is Python (GIL) and the Redis server is single-threaded |

**Expected bottleneck:** *not* Redis. In naive mode it is the round-trip; in
pipelined mode it is our own Python client. We expect `redis-server` to sit
well below saturation in both.

### Task 2 — the SQL sequence (`tests/perf_sequence.py --backend sql`)

Round-trips per page view, counting the way Part 02 taught us to (BEGIN is its
own message, the pre-ping is real):

| step | round-trips |
|---|---|
| pool checkout + `SELECT 1` pre-ping | 1 |
| `BEGIN` | 1 |
| `SELECT` offer + store | 1 |
| `UPDATE offers SET view_count = view_count + 1` | 1 |
| `INSERT INTO offer_views` | 1 |
| `COMMIT` | 1 |

- **RDS, 1 thread: ~4.3 views/s** (3.5–5). 6 × 39.2 ms = 235 ms per view.
  Bottleneck: the network, exactly as in Part 02, only worse — this sequence
  has 6 round-trips where `place_order` had 5.
- **Local Postgres, 1 thread: ~500 views/s** (350–700). 6 × 0.2 ms = 1.2 ms of
  network, plus the part RDS hides from us: this transaction *writes*, so
  `COMMIT` must fsync the WAL. We guess ~0.5–1 ms for that on docker-on-macOS,
  giving ~2 ms per view. **Bottleneck here is the fsync, not the network** —
  the first time in this project that the database itself is the limit.
- **Both, 8 threads on the same 20 offers:** we expect this to scale poorly,
  because step 2 updates one row per offer and every viewer of that offer
  queues behind the row lock until the holder commits. On RDS the lock is held
  across ~120 ms of network, so a single popular bag cannot exceed ~8 views/s
  no matter how many app servers we add.

### Task 3 — the same sequence in Redis (`--backend redis`)

- **1 thread: ~5 000 views/s** (4 000–7 000). One pipeline = 0.152 ms, plus
  ~0.05 ms of Python.
- **8 threads: ~15 000–25 000 views/s**, which is what clears the assignment's
  10 000 rps bar.
- **Expected speedup: ~1 200× against RDS, but only ~10× against local
  Postgres.** We predict that most of the headline number is geography and
  only the last factor of ten is "Redis is a different kind of store". If the
  local-Postgres run came out at, say, 50 views/s instead of 500, our fsync
  estimate would be wrong; if the Redis run came out below 3 000, the Python
  client would be costing far more than we think.

**What would falsify the whole model:** if `--threads 8` scaled linearly on
Redis (it should not, the server is single-threaded and our client holds the
GIL), or if the local Postgres run landed within 2× of Redis (then the pipeline
is not what matters and we have misread the primitives).

## 03.2 Task 1 measured — how fast does Redis take new records?

```
RESULT mode=naive,    n=20000,  threads=1:              7,685 records/sec
RESULT mode=naive,    n=40000,  threads=8:             24,382 records/sec
RESULT mode=pipeline, n=200000, threads=1, batch=100:  50,858 records/sec
RESULT mode=pipeline, n=200000, threads=1, batch=1000: 140,675 records/sec
RESULT mode=pipeline, n=200000, threads=1, batch=5000: 156,565 records/sec
RESULT mode=pipeline, n=400000, threads=4, batch=1000: 161,136 records/sec
RESULT mode=pipeline, n=400000, threads=8, batch=1000: 169,393 records/sec
```

**Prediction scorecard:** naive 7 685 vs. predicted ~6 000 (range 5 000–8 000):
right. Pipelined 140 675 vs. predicted ~60 000 (range 40 000–120 000): **wrong,
we were 2.3× too pessimistic**. Thread scaling 1.2× vs. predicted ~1.5×: right,
and right for the right reason.

**Why we were too pessimistic.** We assumed ~15–20 µs of Python per command.
The real number is ~7 µs: `redis-py` builds one flat byte buffer for the whole
pipeline and hands it to one `send()`, so per command we pay for string
formatting but not for a syscall. We priced the syscall in per command; it is
paid per *batch*.

**Where the bottleneck actually is — measured, not argued.** After a 200 000
record run (133 325 records/sec):

```
cmdstat_xadd: calls=200000, usec=132564, usec_per_call=0.66
```

Redis spent **0.13 s of CPU during a 1.50 s run: it was 8.8 % busy.** And the
same container with a C client at the same pipeline depth:

```
redis-benchmark -P 1000 -n 500000 -t set,lpush
  SET:   3,048,780 requests per second
  LPUSH: 2,747,252 requests per second
```

So the server can do ~20× what our Python client asks of it. **Our bottleneck
is the client, not the store** — which also explains the 1.2× thread scaling:
the work we are limited by (building command buffers in Python) holds the GIL,
so more threads do not help. The assignment's 10 000 rps bar is cleared by a
factor of 14 with a single Python thread.

## 03.3 Task 2 measured — the sequence in Postgres

```
RESULT backend=sql, n=300,  threads=1, offers=20, db=rds:      3.7 views/sec
RESULT backend=sql, n=240,  threads=8, offers=20, db=rds:     25.8 views/sec
RESULT backend=sql, n=240,  threads=8, offers=1,  db=rds:     10.7 views/sec
RESULT backend=sql, n=2000, threads=1, offers=20, db=local: 1,278.6 views/sec
RESULT backend=sql, n=2000, threads=1, offers=1,  db=local: 1,304.0 views/sec
RESULT backend=sql, n=4000, threads=8, offers=20, db=local: 4,060.3 views/sec
RESULT backend=sql, n=4000, threads=8, offers=1,  db=local: 2,264.0 views/sec
```

Per-step medians, RDS:

```
checkout(pre-ping)    46.0 ms
SELECT(+BEGIN)        77.9 ms      <- two round-trips, as Part 02 taught us
UPDATE view_count     37.2 ms
INSERT offer_views    38.6 ms
COMMIT                38.1 ms
TOTAL                237.9 ms/view -> 4.2 views/sec
```

**RDS: the prediction was as close as it gets.** We predicted 6 round-trips ×
39.2 ms = **235 ms**; the profile says **237.9 ms**. Part 02's lesson (BEGIN is
its own message) transferred correctly — we counted it this time and the model
held. End-to-end throughput (3.7/s) is below the profile's 4.2/s for the same
reason as in Part 02: WiFi jitter on a shared server over 80 seconds.

**Local Postgres: wrong by 2.5×, and the reason is interesting.** We predicted
~500 views/s on the assumption that a writing `COMMIT` has to fsync the WAL and
that this would cost 0.5–1 ms. Measured: 1 278 views/s, total 1.3 ms per view,
of which COMMIT is 0.3 ms. So we measured the fsync directly:

```
commit of a 1-row INSERT, synchronous_commit=on : 0.184 ms
commit of a 1-row INSERT, synchronous_commit=off: 0.137 ms
                                         delta:   0.047 ms
```

`fsync=on`, `wal_sync_method=fdatasync`, and turning the durability wait off
saves **47 µs**. A real fdatasync to an SSD is 0.5–2 ms. **Docker Desktop's
virtual disk is acknowledging the sync from the VM's page cache**, so our local
Postgres is not as durable as it claims and its numbers are optimistic. Our
prediction was right about the mechanism and wrong about this machine: on a
real server, the 500/s we predicted is closer to the truth than the 1 278/s we
measured. We keep the measured number and this caveat rather than quietly
enjoying the better figure.

**The hot row is real, and only visible under concurrency.** Single-threaded,
20 offers and 1 offer are identical (1 278 vs. 1 304 views/s) — nobody to
collide with. With 8 clients:

| | 20 offers | 1 offer | cost of the hot row |
|---|---|---|---|
| RDS | 25.8/s | 10.7/s | −59 % |
| local | 4 060/s | 2 264/s | −44 % |

We predicted "a single popular bag cannot exceed ~8 views/s on RDS"; measured
10.7/s. The mechanism is the one from Part 02 section 2.3: `UPDATE` takes a row
lock and holds it until COMMIT, which over the internet is ~76 ms of *waiting*
(INSERT + COMMIT) with the lock held. Adding application servers does not help;
this is a per-row ceiling.

## 03.4 Task 3 measured — the same sequence in Redis

```
RESULT backend=redis, n=200000, threads=1: 6,667.8 views/sec
RESULT backend=redis, n=200000, threads=2: 12,742.7 views/sec
RESULT backend=redis, n=200000, threads=4: 18,067.2 views/sec
RESULT backend=redis, n=200000, threads=8: 17,094.3 views/sec
```

Predicted 5 000 views/s single-threaded (range 4 000–7 000): **right**.
Predicted 15 000–25 000 with 8 threads: right, and the assignment's **10 000
rps bar is cleared from two threads onwards**. Four threads is the peak; eight
is slightly worse, because past the point where Redis is saturated on this
workload the threads only add contention for the GIL.

**The speedup, decomposed honestly:**

| comparison | factor | what it actually measures |
|---|---|---|
| Redis 1 thread vs. RDS 1 thread | **1 802×** | mostly Bangkok → Singapore |
| local Postgres vs. RDS, both 1 thread | 345× | *only* geography |
| **Redis vs. local Postgres, 1 thread** | **5.2×** | the honest engine/shape number |
| Redis peak (18 067) vs. local Postgres peak (4 060) | 4.4× | same, under concurrency |

We predicted ~1 200× against RDS (measured 1 802×) and ~10× against local
Postgres (measured 5.2×). **The second prediction is the one that matters and
it is the one we got wrong** — by exactly the factor by which the local
Postgres beat our fsync estimate above. The headline "1 800× faster" is a
statement about where our database is, not about what it is.

**What is left in Postgres — because the work was deferred, not deleted:**

```
RESULT flush: 799,116 counted views in 0.01s (21 offers), 798,566 events in 11.54s
  amortised Postgres cost 14.4 us/view (69,214 views/sec)
```

Two different wins hide in that line:

- **The counter: 799 116 page views became 21 `UPDATE`s.** This is not "Redis
  is fast", it is a change in the *shape* of the work: Postgres never sees
  799 095 of those increments, and the hot-row lock contention from 03.3
  disappears with them, because there is no longer a row being updated per
  view.
- **The event log: still one row per view**, just written set-based in batches
  of 5 000 instead of one round-trip each — 69 214 rows/sec. Views are not
  free, they cost Postgres 14.4 µs each instead of 782 µs (1.3 ms) in the
  synchronous path, a 54× reduction on work we still do.

So the end-to-end ceiling of the whole system is not the 18 067 views/s that
Redis accepts, it is the ~69 000 views/s the flusher can drain — still well
above the requirement, and the number we would actually capacity-plan with.

## 03.5 Task 4 — what we gave up (`tests/consistency_test.py`, 5 passed)

These tests pass *when the damage happens*; the damage is the deliverable. All
four ran against the shared RDS, so the rows can be inspected in DBeaver.

**4.1 Lost writes — the flusher crashes (at-most-once).**

```
4.1 offer 5278: 500 real page views
    crash: flusher died holding 500 views from 1 offers - they were already
           removed from Redis and never reached Postgres
    Redis now holds:    0
    Postgres now holds: 0
    >>> 500 views vanished from both systems.
    after 500 more views and a clean flush: view_count=500
    (1000 views really happened, 500 lost, 50%)
```

`flush_view_counters` clears Redis first and writes Postgres second. A crash in
that window destroys views that really happened, and — this is the dangerous
part — the system is perfectly healthy afterwards. `view_count = 500` is a
plausible number. Nothing is corrupt, nothing alerts, the figure is simply
wrong forever. Same shape as the lost update in Part 02: the damage is
invisible in the row itself.

**4.2 Dirty counter — no crash needed at all.**

```
4.2 atomic=False: 110 real views, Postgres counted 100, lost 10
4.2 atomic=True:  110 real views, Postgres counted 110, lost  0
```

Reading the counter with `GET` and then clearing it with `DEL` is Part 02's
read-then-write antipattern, this time in Redis: every view arriving between
the two commands is deleted without being counted. `GETDEL` does both in one
command, and the views that arrive after it simply belong to the next flush.
The fix is the same idea as Part 02's atomic conditional `UPDATE`: make the
check-and-clear one operation. Writing this test also found a real bug in our
own flusher — it removed the offer's "dirty" marker *after* reading the
counter, so a view arriving in between would have had its marker deleted and
would have sat in Redis unnoticed. The marker is now removed first
(`src/views.py`), which makes a re-marked offer safe.

**4.3 Stale read — the cache sells a bag that is gone.**

```
4.3 offer 5281 right after the last bag was sold:
    Postgres (the truth): quantity_available = 0
    Redis    (what the customer sees): quantity_available = 1
    the customer taps 'reserve' -> SoldOutError
    >>> stale-read window: 1860 ms (TTL was 2s)
```

The window is exactly the cache TTL: with our production setting of 30 s, we
would promise a sold-out bag to every customer who opens that page for up to
half a minute. Note what saves us: the *reservation* still goes through
`place_order`, i.e. through Part 02's atomic conditional `UPDATE` in Postgres.
The cache can lie about availability, but it cannot oversell, because it is not
the thing that decides. That separation is the whole design.

**4.4 Duplicates — the opposite trade-off, on purpose.**

```
4.4 crash: flusher died after committing 50 events but before advancing the cursor
    50 real views -> 100 rows in offer_views (50 duplicates, 0 lost)
```

`flush_view_stream` writes Postgres first and advances its cursor second, so a
crash duplicates instead of losing. Exactly-once across two systems that cannot
share a transaction does not exist; we only choose the direction of the
failure. We chose **loss for the counter** (it is an approximation anyway) and
**duplicates for the event log** (duplicates are still data and can be
de-duplicated; lost rows cannot be recovered). If we needed the log to be
exact, the fix is a unique key on the Redis stream id, which turns the
duplicate into a no-op at the cost of one more index.

## 03.6 Why this is acceptable for *this* entity (Task 5)

**The entity is the view, and a view is an approximation by nature.** Nobody
reconciles it, nobody is billed for it, and its consumers are a "trending bags"
sort order and a vendor dashboard that says "your bag was seen 1 240 times this
week". Test 4.1 lost 50 % of a small sample, but that sample was one flush;
at production volume a crash costs us exactly one flush interval. Part 01 puts
us at 150 000–500 000 reservations/day, and a customer browses on the order of
ten bags before reserving one, so call it ~5 million views/day. With a
1-second flush interval at our measured 18 000 views/s, a crashed flusher costs
~18 000 views — **0.4 % of a day, once per crash.** It moves no ranking, and no
human reading "seen 1 240 times" would notice if the true number were 1 245. **Compare that with the cost of a lost
`place_order`:** a customer is charged, shows up at the bakery, and there is no
bag. There is no acceptable percentage for that, and no dashboard that makes it
better.

**The entities we could not have moved, and why — the viva question:**

| entity | movable? | why |
|---|---|---|
| `offer_views` | yes, moved | approximate by nature, no reconciliation, no money |
| `offers.quantity_available` | **no** | the last-bag race from Part 02 is decided here. Redis `DECR` is atomic, but the *order* row it must agree with lives in Postgres, and no transaction spans both. An INCR that survives and an INSERT that does not is exactly the oversell we spent Part 02 eliminating |
| `orders` | **no** | the pickup code is the customer's proof. At-most-once would delete proof of a paid reservation; at-least-once would create two |
| `payments` | **never** | money, audit retention, and a 1:1 constraint with the order that Postgres enforces with a unique index. Losing 200 ms of these is not a performance trade-off, it is a missing transaction in an accounting system |
| `reviews` | could be | low volume, so there is nothing to gain — the cost of a second store is not repaid |

The rule we followed: **an entity can move to Redis if the business already
treats its value as approximate.** Views are counted, orders are owed.

**"It's 3 a.m. and the cache is empty — what happens to your Postgres?"** We
measured it instead of guessing. N viewers hit an empty cache simultaneously:

```
 50 viewers ->  9 went to Postgres (18 %)
200 viewers ->  8 went to Postgres ( 4 %)
```

The stampede does not scale with the crowd, it scales with **arrival rate ×
fill latency**: everyone who arrives before the first filler finishes its
`SELECT` also misses. Locally the fill takes ~0.4 ms, so at 18 000 views/s we
expect ~7 concurrent misses, and we measured 8–9. The same arithmetic against
RDS is the dangerous case: an 80 ms fill × 18 000 views/s ≈ **1 400 concurrent
misses against a Postgres that does 26 views/s** — the cache being empty turns
into an outage, not a slowdown. Mitigations, in the order we would apply them:
a per-key fill lock so exactly one client refills (the other 1 399 wait on
Redis, not on Postgres), jittered TTLs so the whole keyspace does not expire in
the same second, and serving the stale hash while the refill runs. We have not
implemented these; the measurement above is what tells us we would need them
before this design goes anywhere near production traffic.

## 03.7 Index decisions for Part 03

| Column | Indexed? | Why |
|---|---|---|
| `offer_views.offer_id` | yes | the only query we run on this table ("views of this offer") and the FK check |
| `offer_views.viewed_at` | **no** | we considered it for "views per day", but this table takes 69 000 inserts/sec in the flush and every index is paid on every insert. The reporting query is a nightly scan, and a scan is cheaper than slowing down the write path we just optimised |
| `offer_views.user_id` | **no**, deliberately | it is a foreign key, but we never ask "what did this user look at", and it is nullable (anonymous browsing) |
| `offers.view_count` | no | it is a payload column, only ever read after finding the offer by its primary key |
