"""
Task 02.6 - Isolation test: break it first, then fix it.

We pick `place_order` (reserving a surprise bag) because Offer.quantity_available
is a shared counter every concurrent customer races on - the textbook lost
update / overselling scenario from Lecture 05.

6.1: Two threads, two independent DB sessions, both running at READ COMMITTED
     (Postgres' default - see Lecture 05), both call place_order_unsafe() on the
     SAME offer that has exactly 1 unit left. Both should be told "yes, you can
     have it" and both commit an order -> the offer goes to -1 (oversold) even
     though we never sold more than we had. That's the corrupted row.

6.2: Same experiment, calling the safe place_order() (atomic conditional
     UPDATE) instead. Exactly one of the two orders succeeds, the other gets a
     clean SoldOutError, and quantity_available never goes negative.

Run: python -m tests.isolation_test
"""
import datetime
import threading

from src.db import get_engine, get_session_factory
from src.models import Store, Offer
from src.operations import place_order_unsafe, place_order, SoldOutError


def make_scarce_offer(session, store_id, qty=1):
    now = datetime.datetime.now()
    offer = Offer(
        store_id=store_id,
        description="Race-condition test bag",
        price_original=10.0,
        price_discounted=3.0,
        quantity_available=qty,
        pickup_from=now,
        pickup_until=now + datetime.timedelta(hours=2),
    )
    session.add(offer)
    session.commit()
    return offer.offer_id


def run_concurrent(fn, offer_id, user_ids, results, errors, inject_delay=0.0):
    Session = get_session_factory(get_engine())

    def worker(user_id, idx):
        session = Session()
        try:
            if fn is place_order_unsafe:
                order = fn(session, user_id, offer_id, quantity=1, inject_delay=inject_delay)
            else:
                order = fn(session, user_id, offer_id, quantity=1)
            results[idx] = order.order_id
        except SoldOutError as e:
            errors[idx] = str(e)
        finally:
            session.close()

    threads = [threading.Thread(target=worker, args=(uid, i)) for i, uid in enumerate(user_ids)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def get_quantity(offer_id):
    Session = get_session_factory(get_engine())
    session = Session()
    qty = session.query(Offer.quantity_available).filter(Offer.offer_id == offer_id).scalar()
    session.close()
    return qty


def count_orders(offer_id):
    Session = get_session_factory(get_engine())
    session = Session()
    from src.models import Order
    n = session.query(Order).filter(Order.offer_id == offer_id).count()
    session.close()
    return n


def section(title):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def main():
    Session = get_session_factory(get_engine())
    session = Session()
    store = session.query(Store).first()
    user_a_id, user_b_id = [u.user_id for u in session.query(Store).first().offers[:0]] or [None, None]
    from src.models import User
    users = session.query(User).limit(2).all()
    user_a_id, user_b_id = users[0].user_id, users[1].user_id
    session.close()

    # --- 6.1: demonstrate the anomaly with the UNSAFE operation ---
    section("6.1 DEMONSTRATING THE ANOMALY (place_order_unsafe, READ COMMITTED)")
    session = Session()
    offer_id = make_scarce_offer(session, store.store_id, qty=1)
    session.close()
    print(f"Created offer {offer_id} with quantity_available = 1")
    print("Two users concurrently try to buy 1 unit each (0.3s delay between read and write)...")

    results, errors = {}, {}
    run_concurrent(place_order_unsafe, offer_id, [user_a_id, user_b_id], results, errors,
                    inject_delay=0.3)

    final_qty = get_quantity(offer_id)
    n_orders = count_orders(offer_id)
    print(f"Orders successfully placed: {len(results)} -> order_ids {list(results.values())}")
    print(f"Orders table row count for this offer: {n_orders}")
    print(f"Offer.quantity_available after both transactions: {final_qty}")

    if n_orders > 1:
        print(f"\n>>> ANOMALY CONFIRMED: {n_orders} orders were placed for a bag that only had 1 unit.")
        print(f">>> Corrupted row: offers.offer_id={offer_id}, quantity_available={final_qty}, "
              f"but {n_orders} paying customers each believe they reserved it.")
        print(f">>> This is a LOST UPDATE: both sessions read quantity_available=1 before either")
        print(f">>> committed, so both independently computed 1-1=0 and overwrote each other's write")
        print(f">>> with the SAME final value -- the counter even looks perfectly sane (0, not negative),")
        print(f">>> which is exactly why this bug is dangerous: nothing downstream would flag it.")
    else:
        print("\n>>> Anomaly did not reproduce this run (timing-dependent) - see ADR.md notes; rerun.")

    # --- 6.2: same experiment with the SAFE operation ---
    section("6.2 THE FIX (place_order, atomic conditional UPDATE)")
    session = Session()
    offer_id_2 = make_scarce_offer(session, store.store_id, qty=1)
    session.close()
    print(f"Created offer {offer_id_2} with quantity_available = 1")
    print("Two users concurrently try to buy 1 unit each...")

    results2, errors2 = {}, {}
    run_concurrent(place_order, offer_id_2, [user_a_id, user_b_id], results2, errors2)

    final_qty_2 = get_quantity(offer_id_2)
    n_orders_2 = count_orders(offer_id_2)
    print(f"Orders successfully placed: {len(results2)} -> order_ids {list(results2.values())}")
    print(f"Rejected with SoldOutError: {len(errors2)} -> {list(errors2.values())}")
    print(f"Orders table row count for this offer: {n_orders_2}")
    print(f"Offer.quantity_available after both transactions: {final_qty_2}")

    assert n_orders_2 == 1, "expected exactly one successful order"
    assert final_qty_2 == 0, "expected quantity_available to land exactly at 0, never negative"
    assert len(errors2) == 1, "expected exactly one SoldOutError"
    print("\n>>> PASS: exactly one order succeeded, one was cleanly rejected, quantity never went negative.")


if __name__ == "__main__":
    main()
