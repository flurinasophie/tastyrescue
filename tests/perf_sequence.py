"""
Tasks 03.2 and 03.3 - the same business sequence, measured in Postgres and in
Redis.

The sequence is "open the detail page of a surprise bag" (src/views.py):
read the offer, count the view, record the view event.

  --backend sql    all three in one Postgres transaction
  --backend redis  all three in one Redis pipeline, flushed to Postgres later

  --db rds|local   which Postgres: the shared course server in Singapore, or
                   docker on this machine. Redis runs locally, so comparing it
                   against RDS alone would mostly measure the flight to
                   Singapore. Measuring both tells the two effects apart.

  --profile        per-step medians for one call (where the time actually goes)
  --threads N      N concurrent clients

Run:
  python -m tests.perf_sequence --backend sql   --db rds   --n 300 --profile
  python -m tests.perf_sequence --backend sql   --db local --n 2000
  python -m tests.perf_sequence --backend redis --n 50000
  python -m tests.perf_sequence --backend redis --n 200000 --threads 8
"""
import argparse
import random
import statistics
import threading
import time

from sqlalchemy import text

from src.cache import get_redis
from src.db import get_engine, get_session_factory
from src.views import (
    DETAIL_SQL, flush_view_counters, flush_view_stream, load_offer_into_cache,
    redis_view_count, view_offer_redis, view_offer_sql,
)

N_OFFERS = 20      # the "popular bags" everyone is browsing
N_USERS = 300


def pick_workload(Session, n_offers=N_OFFERS):
    """A fixed set of existing offers and users to browse.

    `n_offers=1` is the interesting case for the SQL backend: every viewer then
    updates the SAME row and has to queue behind its row lock.
    """
    with Session() as s:
        offer_ids = [row[0] for row in s.execute(text(
            "SELECT offer_id FROM offers ORDER BY offer_id DESC LIMIT :n"
        ), {"n": n_offers})]
        user_ids = [row[0] for row in s.execute(text(
            "SELECT user_id FROM users ORDER BY user_id LIMIT :n"
        ), {"n": N_USERS})]
    if not offer_ids or not user_ids:
        raise SystemExit("No offers/users found - run src.populate against this database first.")
    return offer_ids, user_ids


def profile_sql(Session, offer_ids, user_ids, rounds=20):
    """Where does one SQL page view spend its time? Same method as Part 02."""
    steps = {"checkout(pre-ping)": [], "SELECT(+BEGIN)": [], "UPDATE view_count": [],
             "INSERT offer_views": [], "COMMIT": []}
    for _ in range(rounds):
        offer_id, user_id = random.choice(offer_ids), random.choice(user_ids)
        session = Session()
        t0 = time.perf_counter()
        session.connection()                      # checkout + pre-ping
        t1 = time.perf_counter()
        session.execute(DETAIL_SQL, {"offer_id": offer_id}).one()
        t2 = time.perf_counter()
        session.execute(text("UPDATE offers SET view_count = view_count + 1 "
                             "WHERE offer_id = :offer_id"), {"offer_id": offer_id})
        t3 = time.perf_counter()
        session.execute(text("INSERT INTO offer_views (offer_id, user_id, source) "
                             "VALUES (:offer_id, :user_id, 'sql')"),
                        {"offer_id": offer_id, "user_id": user_id})
        t4 = time.perf_counter()
        session.commit()
        t5 = time.perf_counter()
        session.close()
        for name, dt in zip(steps, [t1 - t0, t2 - t1, t3 - t2, t4 - t3, t5 - t4]):
            steps[name].append(dt * 1000)

    print("\nper-step medians over", rounds, "calls:")
    total = 0.0
    for name, samples in steps.items():
        median = statistics.median(samples)
        total += median
        print(f"  {name:<22} {median:7.1f} ms")
    print(f"  {'TOTAL':<22} {total:7.1f} ms/view -> {1000 / total:.1f} views/sec\n")


def run_sql(n, Session, offer_ids, user_ids):
    for _ in range(n):
        session = Session()
        try:
            view_offer_sql(session, random.choice(offer_ids), random.choice(user_ids))
        finally:
            session.close()


def run_redis(n, r, offer_ids, user_ids):
    for _ in range(n):
        view_offer_redis(r, random.choice(offer_ids), random.choice(user_ids))


def run_flush(Session, r, offer_ids):
    """What the Redis numbers do NOT show: the Postgres work is deferred, not
    deleted. This measures the flusher, so we can state the honest amortised
    cost per view instead of pretending views became free."""
    with Session() as s:
        pending = redis_view_count(r, offer_ids)
        start = time.perf_counter()
        counters = flush_view_counters(s, r)
        t_counters = time.perf_counter() - start

        start = time.perf_counter()
        events = 0
        while True:
            report = flush_view_stream(s, r, batch=5000)
            if not report["inserted"]:
                break
            events += report["inserted"]
        t_events = time.perf_counter() - start

    print(f"RESULT flush: {counters['views_written']} counted views in {t_counters:.2f}s "
          f"({counters['offers']} offers), {events} events in {t_events:.2f}s")
    if pending:
        per_view_us = (t_counters + t_events) / pending * 1e6
        print(f"  pending before flush: {pending} views -> amortised Postgres cost "
              f"{per_view_us:.1f} us/view ({pending / max(t_counters + t_events, 1e-9):,.0f} views/sec)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=["sql", "redis", "flush"], required=True)
    parser.add_argument("--db", choices=["rds", "local"], default=None)
    parser.add_argument("--n", type=int, default=300)
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--offers", type=int, default=N_OFFERS,
                        help="how many distinct offers the load spreads over (1 = one hot row)")
    parser.add_argument("--profile", action="store_true")
    args = parser.parse_args()

    engine = get_engine(target=args.db)
    Session = get_session_factory(engine)
    offer_ids, user_ids = pick_workload(Session, args.offers)

    if args.backend == "flush":
        run_flush(Session, get_redis(), offer_ids)
        return

    if args.backend == "redis":
        # Warm the cache once, so we measure steady state and not 20 cache
        # misses. The cold case is its own question - see ADR 5, "3 a.m.".
        r = get_redis()
        with Session() as s:
            for offer_id in offer_ids:
                load_offer_into_cache(s, r, offer_id)

    if args.profile and args.backend == "sql":
        profile_sql(Session, offer_ids, user_ids)

    per_thread = args.n // args.threads
    label = (f"backend={args.backend}, n={per_thread * args.threads}, "
             f"threads={args.threads}, offers={len(offer_ids)}")
    if args.backend == "sql":
        label += f", db={args.db or 'rds (default)'}"
    print(f"Running {label} ...")

    def worker():
        if args.backend == "sql":
            run_sql(per_thread, Session, offer_ids, user_ids)
        else:
            run_redis(per_thread, get_redis(), offer_ids, user_ids)

    threads = [threading.Thread(target=worker) for _ in range(args.threads)]
    start = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - start

    done = per_thread * args.threads
    print(f"RESULT {label}: {done} page views in {elapsed:.2f}s -> "
          f"{done / elapsed:,.1f} views/sec")


if __name__ == "__main__":
    main()
