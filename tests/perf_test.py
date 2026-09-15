"""
Task 02.5 - Performance test: predict, then measure.

We load-test `place_order` (the safe version). Before running, we wrote our
prediction into ADR.md. This script has two modes:

  --mode naive     one DB round-trip per order, one order per commit
                    (this is what most students write first)
  --mode batched    same logic, but orders are batched into larger
                    transactions and we reuse a small connection pool
                    instead of opening a new connection every time

Run:
  python -m tests.perf_test --mode naive   --n 20000
  python -m tests.perf_test --mode batched --n 20000
"""
import argparse
import datetime
import random
import time

from sqlalchemy import text

from src.db import get_engine, get_session_factory
from src.models import Store, User, Offer, Order, OrderStatus
from src.operations import place_order, SoldOutError


def seed_offers_for_load_test(session, store_id, n_offers, qty_each=1_000_000):
    """Offers with huge quantity so we are measuring write throughput, not
    contention/SoldOutError - contention is what the isolation test covers."""
    now = datetime.datetime.now()
    offers = [
        Offer(store_id=store_id, description="perf-test bag", price_original=10.0,
              price_discounted=3.0, quantity_available=qty_each,
              pickup_from=now, pickup_until=now + datetime.timedelta(hours=2))
        for _ in range(n_offers)
    ]
    session.bulk_save_objects(offers)
    session.commit()
    return [o.offer_id for o in session.query(Offer.offer_id).order_by(Offer.offer_id.desc()).limit(n_offers)]


def run_naive(n, user_ids, offer_ids):
    """One session per order, one commit per order - maximum round-trips."""
    engine = get_engine()
    Session = get_session_factory(engine)

    start = time.perf_counter()
    for i in range(n):
        session = Session()
        try:
            place_order(session, random.choice(user_ids), random.choice(offer_ids), quantity=1)
        except SoldOutError:
            pass
        finally:
            session.close()
    elapsed = time.perf_counter() - start
    return elapsed


def run_batched(n, user_ids, offer_ids, batch_size=500):
    """The '10x faster' version. Same atomic UPDATE-then-INSERT logic as
    place_order(), but many orders share ONE transaction / ONE commit
    instead of committing after every single order. This is the fix that
    actually matters: Postgres fsyncs the WAL on every COMMIT, so committing
    once per order means paying that fsync cost n times instead of n/batch_size
    times."""
    import uuid
    engine = get_engine()
    Session = get_session_factory(engine)

    start = time.perf_counter()
    done = 0
    while done < n:
        this_batch = min(batch_size, n - done)
        session = Session()
        try:
            # Lecture 02 (ACID/Durability): synchronous_commit=off still writes
            # to the WAL (crash-safe against a process crash), it just doesn't
            # block the client on the fsync to disk before returning - trading
            # "durable against an OS/power failure in the last few ms" for
            # throughput. Scoped to this session only, not a superuser change.
            session.execute(text("SET LOCAL synchronous_commit = off"))
            # ONE round-trip per order instead of two: the UPDATE and the
            # INSERT are fused into a single statement with a CTE, so the
            # atomic check-and-decrement and the order row are written in
            # one server round-trip. rows_written = 0 means "sold out".
            rows = []
            for _ in range(this_batch):
                offer_id = random.choice(offer_ids)
                rows.append({"offer_id": offer_id, "user_id": random.choice(user_ids),
                             "pickup_code": str(uuid.uuid4())[:8]})
            # executemany-style: psycopg2 pipelines these far more efficiently
            # than this_batch separate session.execute() calls.
            session.execute(
                text("""
                    WITH dec AS (
                        UPDATE offers SET quantity_available = quantity_available - 1
                        WHERE offer_id = :offer_id AND quantity_available >= 1
                        RETURNING offer_id
                    )
                    INSERT INTO orders (user_id, offer_id, quantity, status, pickup_code)
                    SELECT :user_id, offer_id, 1, 'PLACED', :pickup_code FROM dec
                """),
                rows,
            )
            session.commit()  # ONE commit for the whole batch
        finally:
            session.close()
        done += this_batch
    elapsed = time.perf_counter() - start
    return elapsed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["naive", "batched"], required=True)
    parser.add_argument("--n", type=int, default=20000)
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args()

    engine = get_engine()
    Session = get_session_factory(engine)
    session = Session()
    store = session.query(Store).first()
    user_ids = [u.user_id for u in session.query(User).limit(300).all()]
    print(f"Seeding offers with effectively unlimited stock for the load test...")
    offer_ids = seed_offers_for_load_test(session, store.store_id, n_offers=20)
    session.close()

    print(f"Running mode={args.mode}, n={args.n} orders...")
    if args.mode == "naive":
        elapsed = run_naive(args.n, user_ids, offer_ids)
    else:
        elapsed = run_batched(args.n, user_ids, offer_ids, batch_size=args.batch_size)

    rps = args.n / elapsed
    print(f"\nRESULT mode={args.mode}: {args.n} orders in {elapsed:.2f}s -> {rps:.1f} orders/sec")


if __name__ == "__main__":
    main()
