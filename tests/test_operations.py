"""
Basic sanity check that every atomic operation from Task 02.4 works.
Run: python -m tests.test_operations
"""
from src.db import get_engine, get_session_factory
from src.models import Store, Offer
from src.operations import (
    register_user, post_offer, place_order, pay_order, leave_review, SoldOutError
)
import datetime


def main():
    engine = get_engine()
    Session = get_session_factory(engine)
    session = Session()

    store = session.query(Store).first()

    user = register_user(session, "Test User", f"test_{datetime.datetime.now().timestamp()}@example.com",
                          "+66123456789", "Bangkok")
    print("1. register_user  ->", user.user_id, user.email)

    now = datetime.datetime.now()
    offer = post_offer(session, store.store_id, "Sanity check bag", 15.0, 5.0, 3,
                        now, now + datetime.timedelta(hours=2))
    print("2. post_offer     ->", offer.offer_id, "qty:", offer.quantity_available)

    order = place_order(session, user.user_id, offer.offer_id, quantity=1)
    print("3. place_order    ->", order.order_id, "pickup_code:", order.pickup_code)

    session.refresh(offer)
    print("   offer.quantity_available after order:", offer.quantity_available)

    payment = pay_order(session, order.order_id, amount=5.0, method="card")
    print("4. pay_order      ->", payment.payment_id, payment.status)

    review = leave_review(session, order.order_id, rating=5, comment="Great bag!")
    print("5. leave_review   ->", review.review_id, review.rating)

    # sell out the remaining 2, then confirm SoldOutError on the 4th attempt
    place_order(session, user.user_id, offer.offer_id, quantity=2)
    try:
        place_order(session, user.user_id, offer.offer_id, quantity=1)
        print("ERROR: should have raised SoldOutError")
    except SoldOutError as e:
        print("SoldOutError correctly raised on 4th unit:", e)

    session.close()
    print("\nAll 5 operations OK.")


if __name__ == "__main__":
    main()
