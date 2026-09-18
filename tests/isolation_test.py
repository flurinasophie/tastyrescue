"""
Task 02.6 - Isolation test: break it first, then fix it.

We pick `place_order` (reserving a surprise bag) because Offer.quantity_available
is a shared counter every concurrent customer races on - the textbook lost
update / overselling scenario from Lecture 05.

6.1: Two threads, two independent DB connections, both at READ COMMITTED
     (Postgres' default), both call place_order_unsafe() on the SAME offer that
     has exactly 1 unit left. A threading.Barrier forces both to finish their
     SELECT before either runs its UPDATE. Both are told "yes, 1 is available",
     both commit an order -> two orders for one bag. THIS TEST IS EXPECTED TO
     FAIL: the failure IS the deliverable.

6.2: Same experiment against the safe place_order() (atomic conditional
     UPDATE). Exactly one order succeeds, the other gets a clean SoldOutError,
     and quantity_available never goes negative. This test passes.

Run both as a script (prints the corrupted row):
    python -m tests.isolation_test

Run as pytest (6.1 FAILS on purpose, 6.2 PASSES):
    python -m pytest tests/isolation_test.py -v
"""
import datetime
import threading

from sqlalchemy import text

from src.db import get_engine, get_session_factory
from src.models import Store, User, Offer, Order
from src.operations import place_order_unsafe, place_order, SoldOutError

ENGINE = get_engine()
Session = get_session_factory(ENGINE)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def two_user_ids():
    with Session() as s:
        users = s.query(User.user_id).limit(2).all()
        return [u.user_id for u in users]


def make_scarce_offer(qty=1):
    """A fresh offer with exactly `qty` units, so the race is isolated from
    whatever else is in the table."""
    now = datetime.datetime.now()
    with Session() as s:
        store_id = s.query(Store.store_id).first()[0]
        offer = Offer(
            store_id=store_id,
            description="Race-condition test bag",
            price_original=10.0,
            price_discounted=3.0,
            quantity_available=qty,
            pickup_from=now,
            pickup_until=now + datetime.timedelta(hours=2),
        )
        s.add(offer)
        s.commit()
        return offer.offer_id


def race(fn, offer_id, user_ids):
    """Run `fn` in two threads on two separate connections, both pinned to
    READ COMMITTED, synchronised so both read before either writes.

    Returns (order_ids, errors).
    """
    barrier = threading.Barrier(len(user_ids), timeout=30)
    order_ids, errors = {}, {}

    def worker(idx, user_id):
        session = Session()
        try:
            # Be explicit about the isolation level instead of relying on the
            # server default, so the experiment states its own conditions.
            session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
            if fn is place_order_unsafe:
                order = fn(session, user_id, offer_id, quantity=1, sync=barrier.wait)
            else:
                # The safe version has no read-then-write window to sync on;
                # line them up just before the single atomic statement.
                barrier.wait()
                order = fn(session, user_id, offer_id, quantity=1)
            order_ids[idx] = order.order_id
        except SoldOutError as exc:
            errors[idx] = str(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=worker, args=(i, uid))
               for i, uid in enumerate(user_ids)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return order_ids, errors


def observe(offer_id):
    """What the database actually holds afterwards."""
    with Session() as s:
        qty = s.query(Offer.quantity_available).filter(Offer.offer_id == offer_id).scalar()
        n_orders = s.query(Order).filter(Order.offer_id == offer_id).count()
        rows = s.execute(text("""
            SELECT o.offer_id, o.quantity_available, r.order_id, r.user_id, r.pickup_code
            FROM offers o JOIN orders r USING (offer_id)
            WHERE o.offer_id = :oid
            ORDER BY r.order_id
        """), {"oid": offer_id}).all()
    return qty, n_orders, rows


def show(title, offer_id):
    qty, n_orders, rows = observe(offer_id)
    print(f"\n{title}")
    print(f"  offers.offer_id={offer_id}  quantity_available={qty}  orders for it={n_orders}")
    for r in rows:
        print(f"    order_id={r.order_id} user_id={r.user_id} pickup_code={r.pickup_code}")
    return qty, n_orders


# ---------------------------------------------------------------------------
# 6.1 - the anomaly. THIS TEST IS SUPPOSED TO FAIL.
# ---------------------------------------------------------------------------
def test_6_1_lost_update_oversells_the_last_bag():
    """EXPECTED TO FAIL - that failure is the deliverable for Task 02.6.1.

    One bag, two buyers, READ COMMITTED, read-then-write. Both buyers read
    quantity_available=1, both decide "available", both write the value they
    computed in Python (0). The second write overwrites the first: a lost
    update. The counter looks perfectly healthy at 0 - and two customers hold
    an order for the same single bag.
    """
    offer_id = make_scarce_offer(qty=1)
    order_ids, errors = race(place_order_unsafe, offer_id, two_user_ids())
    qty, n_orders, rows = observe(offer_id)

    print(f"\noffer {offer_id}: quantity_available={qty}, orders={n_orders}, "
          f"succeeded={list(order_ids.values())}, rejected={list(errors.values())}")
    for r in rows:
        print(f"  order_id={r.order_id} user_id={r.user_id} pickup_code={r.pickup_code}")

    assert n_orders == 1, (
        f"OVERSOLD: offer {offer_id} had 1 bag but {n_orders} orders exist "
        f"(quantity_available={qty}, which never went negative - that is why "
        f"the CHECK constraint did not catch it)"
    )
    assert qty == 0


# ---------------------------------------------------------------------------
# 6.2 - the fix. This test passes.
# ---------------------------------------------------------------------------
def test_6_2_atomic_conditional_update_prevents_overselling():
    """The same race against the safe place_order(): check and decrement happen
    in ONE statement, so the loser's WHERE clause is re-evaluated against the
    winner's committed row and matches nothing."""
    offer_id = make_scarce_offer(qty=1)
    order_ids, errors = race(place_order, offer_id, two_user_ids())
    qty, n_orders, _ = observe(offer_id)

    print(f"\noffer {offer_id}: quantity_available={qty}, orders={n_orders}, "
          f"succeeded={list(order_ids.values())}, rejected={list(errors.values())}")

    assert n_orders == 1, f"expected exactly one order, got {n_orders}"
    assert len(errors) == 1, f"expected exactly one SoldOutError, got {errors}"
    assert qty == 0, f"expected quantity_available to land on 0, got {qty}"


# ---------------------------------------------------------------------------
# script mode: run both experiments and print the corrupted row
# ---------------------------------------------------------------------------
def main():
    user_ids = two_user_ids()

    print("=" * 72)
    print("6.1  ANOMALY: place_order_unsafe, READ COMMITTED, 1 bag, 2 buyers")
    print("=" * 72)
    offer_id = make_scarce_offer(qty=1)
    print(f"Created offer {offer_id} with quantity_available = 1")
    order_ids, errors = race(place_order_unsafe, offer_id, user_ids)
    print(f"Orders that succeeded: {list(order_ids.values())}  rejected: {list(errors.values())}")
    qty, n_orders = show("Database state afterwards:", offer_id)

    if n_orders > 1:
        print(f"\n>>> ANOMALY CONFIRMED: {n_orders} orders exist for a bag that had 1 unit.")
        print(f">>> LOST UPDATE: both transactions read quantity_available=1, both computed")
        print(f">>> 1-1=0 in Python, and the second UPDATE overwrote the first with the same")
        print(f">>> value. quantity_available={qty} looks correct, which is exactly what makes")
        print(f">>> this dangerous: no constraint, no alert and no downstream check fires.")
    else:
        print("\n>>> Did not reproduce - unexpected with the barrier in place; rerun.")

    print("\n" + "=" * 72)
    print("6.2  FIX: place_order, atomic conditional UPDATE, same race")
    print("=" * 72)
    offer_id_2 = make_scarce_offer(qty=1)
    print(f"Created offer {offer_id_2} with quantity_available = 1")
    order_ids2, errors2 = race(place_order, offer_id_2, user_ids)
    print(f"Orders that succeeded: {list(order_ids2.values())}  rejected: {list(errors2.values())}")
    qty2, n_orders2 = show("Database state afterwards:", offer_id_2)

    assert n_orders2 == 1, "expected exactly one successful order"
    assert qty2 == 0, "expected quantity_available to land exactly on 0"
    assert len(errors2) == 1, "expected exactly one SoldOutError"
    print("\n>>> PASS: one order, one clean SoldOutError, counter at 0 and never negative.")


if __name__ == "__main__":
    main()
