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

Either use the shared course RDS instance from the Lecture 05 slides, **or**
run Postgres locally with Docker (recommended for the load test / isolation
test so you're not hammering a shared database):

```bash
docker compose up -d
```

This starts Postgres 16 on `localhost:5432` with user/password/db all set
to match `.env.example`.

### 2. Python environment

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure the connection

```bash
cp .env.example .env
# edit .env if you're pointing at the shared RDS instance instead of local docker
```

### 4. Create tables and load sample data

```bash
python -m src.create_tables
python -m src.populate --stores 200 --users 2000 --offers 5000
```

### 5. Run everything

```bash
# Task 02.4 — sanity check all 5 atomic operations
python -m tests.test_operations

# Task 02.6 — the isolation anomaly, then the fix
python -m tests.isolation_test

# Task 02.5 — performance: naive vs. 10x-optimized
python -m tests.perf_test --mode naive   --n 15000
python -m tests.perf_test --mode batched --n 15000 --batch-size 1000
```

Read `ADR.md` for what these numbers mean and what we predicted beforehand.
