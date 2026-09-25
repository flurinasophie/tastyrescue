# TastyRescue — Data Storages Course Project

Too Good To Go, reimagined for Bangkok. Team: Flurina Baumbach, Gregory von
Werne, Annabel von Morgenstern.

- `docs/part01-team-and-pitch.md` — Part 01 submission (team, pitch, order-of-magnitude table)
- `docs/er-model-too-good-to-go.pdf` — Part 01 ER diagram (source of truth for the schema below)
- `ADR.md` — Part 02 predictions, measurements, and the isolation anomaly writeup (read this for the viva)
- `src/` — the data model and business logic
- `tests/` — sanity test, isolation test, performance test

## Schema (from the Part 01 ER diagram)

`Store 1—n Offer`, `User 1—n Order`, `Offer 1—n Order`, `Order 1—1 Payment`,
`Order 1—0,1 Review`. See `src/models.py` for the full SQLAlchemy definitions.

## Setup

### 1. Get a PostgreSQL database

We use the shared course RDS instance (see `.env.example`). All our tables
live in the schema `tastyrescue`, so we never collide with other teams.

Optional local database: `docker compose up -d` starts Postgres 16 on
`localhost:5434` (user/password `postgres`, database `tastyrescue`).

### 2. Python environment

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure the connection

```bash
cp .env.example .env      # then put the class password into .env
python -m src.db          # connectivity check
```

### 4. Create tables and load sample data

```bash
python -m src.create_tables          # creates what is missing, touches no data
python -m src.populate --stores 200 --users 2000 --offers 5000
```

`--reset` drops our tables first and asks for confirmation. Everything is
scoped to the `tastyrescue` schema (see `DB_SCHEMA` in `.env`), so nothing can
reach another team's tables on the shared server.

### 5. Run everything

```bash
# Task 02.4 — sanity check all 5 atomic operations
python -m tests.test_operations

# Task 02.6 — the isolation anomaly, then the fix
python -m tests.isolation_test
python -m pytest tests/isolation_test.py -v   # 6.1 FAILED, 6.2 PASSED

# Task 02.5 — performance: naive vs. optimized (we measured 614x)
python -m tests.perf_test --mode naive   --n 300           # ~60s, it is slow on purpose
python -m tests.perf_test --mode batched --n 5000 --batch-size 1000
python -m tests.perf_test --mode batched --n 5000 --batch-size 500 --sync-commit off
```

Keep `--n` small for naive: at ~5 orders/sec over the internet, 1 000 orders
would take 3.5 minutes of pure waiting.

Read `ADR.md` for what these numbers mean and what we predicted beforehand.

## Part 03 — Redis for the view counter

The workload is "a customer opens a bag's detail page": read the offer, count
the view, record the event (`src/views.py`). Part 03 runs that sequence against
Postgres and against Redis, and then shows what the Redis version costs us.

```bash
docker compose up -d                  # Postgres on :5434 AND Redis on :6379
python -m src.migrate_part03          # adds offers.view_count + offer_views
python -m src.migrate_part03 --db local
python -m src.cache                   # Redis connectivity check
```

```bash
# Task 03.1 — how fast can Redis take new records at all?
python -m tests.perf_nosql --mode naive    --n 20000
python -m tests.perf_nosql --mode pipeline --n 200000 --batch 1000
python -m tests.perf_nosql --mode pipeline --n 500000 --batch 1000 --threads 8

# Task 03.2 — the same sequence in Postgres, remote and local
python -m tests.perf_sequence --backend sql --db rds   --n 300 --profile
python -m tests.perf_sequence --backend sql --db local --n 2000

# Task 03.3 — the sequence in Redis
python -m tests.perf_sequence --backend redis --n 50000
python -m tests.perf_sequence --backend redis --n 200000 --threads 8

# ... and what Postgres still has to do afterwards (the deferred work)
python -m tests.perf_sequence --backend flush --db local

# the hot row: everyone viewing the SAME bag (--offers 1)
python -m tests.perf_sequence --backend sql --db rds --n 240 --threads 8 --offers 1

# Task 03.4 — what we gave up: lost views, a dirty counter, a stale read,
# and duplicates. These tests PASS when the damage is reproduced.
python -m pytest tests/consistency_test.py -v -s
```

Local Postgres is measured on purpose: Redis runs on this machine, so comparing
it only against RDS in Singapore would measure the network, not the databases.
`ADR.md` section 03 keeps those two effects apart.
