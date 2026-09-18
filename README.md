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
python -m src.create_tables --reset
python -m src.populate --stores 200 --users 2000 --offers 5000
```

### 5. Run everything

```bash
# Task 02.4 — sanity check all 5 atomic operations
python -m tests.test_operations

# Task 02.6 — the isolation anomaly, then the fix
python -m tests.isolation_test
python -m pytest tests/isolation_test.py -v   # 6.1 FAILED, 6.2 PASSED

# Task 02.5 — performance: naive vs. 10x-optimized
python -m tests.perf_test --mode naive   --n 1000
python -m tests.perf_test --mode batched --n 1000 --batch-size 500
```

Read `ADR.md` for what these numbers mean and what we predicted beforehand.
