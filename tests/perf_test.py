"""
Task 02.5 - Performance test: predict, then measure.

We load-test `place_order`. The prediction was written into ADR.md and
committed BEFORE the first run (see git history).

Modes:
  --mode naive     one order = one session = one transaction = one commit.
                   4 network round-trips per order (pre-ping, UPDATE, INSERT,
                   COMMIT). This is what the ORM code in src/operations.py does.

  --mode batched   the optimised version: `batch_size` orders are sent as ONE
                   set-based statement (unnest over arrays -> aggregate per
                   offer -> UPDATE ... FROM -> INSERT ... SELECT, all in one
                   CTE) inside ONE transaction. Per batch: ~4 round-trips and
                   1 commit, instead of 4*batch_size round-trips and
                   batch_size commits.

  --sync-commit off   additionally disables the WAL fsync wait for that
                      transaction, to separate "fewer round-trips" from
                      "fewer disk flushes" in the post-mortem.

Run:
  python -m tests.perf_test --mode naive   --n 300
  python -m tests.perf_test --mode batched --n 5000 --batch-size 500
"""
import argparse
import datetime
import random
import time
import uuid

from sqlalchemy import text, bindparam, ARRAY, Integer, String

from src.db import get_engine, get_session_factory
from src.models import Store, User, Offer
from src.operations import place_order, SoldOutError

# One statement that places `len(offer_ids)` orders.
#
#  req  - the batch, unpacked from three parallel arrays into rows
#  want - how many units this batch wants per offer. We must aggregate,
#         because a single UPDATE cannot touch the same row twice.
#  dec  - the atomic check-and-decrement, same guard as place_order():
#         only offers with enough stock are updated, and only those come back
#  the INSERT then creates order rows for exactly the offers that succeeded
BATCH_SQL = text("""
    WITH req AS (
        SELECT * FROM unnest(:offer_ids, :user_ids, :codes)
                      AS t(offer_id, user_id, pickup_code)
    ),
    want AS (
        SELECT offer_id, count(*) AS n FROM req GROUP BY offer_id
    ),
    dec AS (
        UPDATE offers o
        SET quantity_available = o.quantity_available - want.n
        FROM want
        WHERE o.offer_id = want.offer_id
          AND o.quantity_available >= want.n
        RETURNING o.offer_id
    )
    INSERT INTO orders (user_id, offer_id, quantity, status, pickup_code)
    SELECT r.user_id, r.offer_id, 1, 'PLACED', r.pickup_code
    FROM req r JOIN dec d ON d.offer_id = r.offer_id
""").bindparams(
    bindparam("offer_ids", type_=ARRAY(Integer)),
    bindparam("user_ids", type_=ARRAY(Integer)),
    bindparam("codes", type_=ARRAY(String)),
)


def seed_offers_for_load_test(n_offers=20, qty_each=10_000_000):
    """Offers with effectively unlimited stock, so we measure write throughput
    rather than contention - contention is what the isolation test covers."""
    now = datetime.datetime.now()
    engine = get_engine()
    Session = get_session_factory(engine)
    with Session() as s:
        store_id = s.query(Store.store_id).first()[0]
        offers = [
            Offer(store_id=store_id, description="perf-test bag", price_original=10.0,
                  price_discounted=3.0, quantity_available=qty_each,
                  pickup_from=now, pickup_until=now + datetime.timedelta(hours=2))
            for _ in range(n_offers)
        ]
        s.add_all(offers)
        s.commit()
        return [o.offer_id for o in offers]


def run_naive(n, user_ids, offer_ids):
    """One session per order, one commit per order - maximum round-trips."""
    engine = get_engine()
    Session = get_session_factory(engine)

    start = time.perf_counter()
    for _ in range(n):
        session = Session()
        try:
            place_order(session, random.choice(user_ids), random.choice(offer_ids), quantity=1)
        except SoldOutError:
            pass
        finally:
            session.close()
    return time.perf_counter() - start


def run_batched(n, user_ids, offer_ids, batch_size=500, sync_commit="on"):
    """One statement and one commit per batch."""
    engine = get_engine()
    Session = get_session_factory(engine)

    start = time.perf_counter()
    done = 0
    while done < n:
        this_batch = min(batch_size, n - done)
        session = Session()
        try:
            if sync_commit == "off":
                # Lecture 02 (Durability): the transaction is still written to
                # the WAL, we just do not block the client until it is fsynced.
                # Trades "durable against OS/power loss in the last few ms" for
                # throughput. SET LOCAL = this transaction only.
                session.execute(text("SET LOCAL synchronous_commit = off"))
            session.execute(BATCH_SQL, {
                "offer_ids": [random.choice(offer_ids) for _ in range(this_batch)],
                "user_ids": [random.choice(user_ids) for _ in range(this_batch)],
                "codes": [uuid.uuid4().hex[:8] for _ in range(this_batch)],
            })
            session.commit()
        finally:
            session.close()
        done += this_batch
    return time.perf_counter() - start


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["naive", "batched"], required=True)
    parser.add_argument("--n", type=int, default=5000)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--sync-commit", choices=["on", "off"], default="on")
    args = parser.parse_args()

    engine = get_engine()
    Session = get_session_factory(engine)
    with Session() as s:
        user_ids = [u.user_id for u in s.query(User.user_id).limit(300).all()]
    offer_ids = seed_offers_for_load_test()

    label = f"mode={args.mode}, n={args.n}"
    if args.mode == "batched":
        label += f", batch_size={args.batch_size}, synchronous_commit={args.sync_commit}"
    print(f"Running {label} ...")

    if args.mode == "naive":
        elapsed = run_naive(args.n, user_ids, offer_ids)
    else:
        elapsed = run_batched(args.n, user_ids, offer_ids,
                              batch_size=args.batch_size, sync_commit=args.sync_commit)

    print(f"RESULT {label}: {args.n} orders in {elapsed:.2f}s -> {args.n / elapsed:.1f} orders/sec")


if __name__ == "__main__":
    main()
