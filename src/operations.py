"""
Task 02.4 - Atomic business operations.

Five operations, one function each:
  1. register_user      - create a user
  2. post_offer          - a store posts a new surprise bag
  3. place_order_unsafe  - reserve a bag: READ-THEN-WRITE (deliberately racy,
                            used ONLY to demonstrate the anomaly in Task 02.6)
  3'. place_order        - reserve a bag: the SAFE version (atomic conditional
                            UPDATE, the fix from Task 02.6)
  4. pay_order           - capture payment for an order
  5. leave_review        - optional post-pickup review

place_order is the interesting one: Offer.quantity_available is a shared
counter that many concurrent customers race to decrement. This is exactly
the "account balance" pattern from Lecture 05 (Isolation) - see ADR.md.
"""
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.models import User, Offer, Order, Payment, Review, OrderStatus


class SoldOutError(Exception):
    """Raised by the safe place_order when the offer no longer has capacity."""


# ---------------------------------------------------------------------------
# 1. Register a user
# ---------------------------------------------------------------------------
def register_user(session: Session, name: str, email: str, phone: str, location: str) -> User:
    user = User(name=name, email=email, phone=phone, location=location)
    session.add(user)
    session.commit()
    return user


# ---------------------------------------------------------------------------
# 2. Post a new offer (a store lists a surprise bag)
# ---------------------------------------------------------------------------
def post_offer(session: Session, store_id: int, description: str, price_original: float,
                price_discounted: float, quantity_available: int, pickup_from, pickup_until) -> Offer:
    offer = Offer(
        store_id=store_id,
        description=description,
        price_original=price_original,
        price_discounted=price_discounted,
        quantity_available=quantity_available,
        pickup_from=pickup_from,
        pickup_until=pickup_until,
    )
    session.add(offer)
    session.commit()
    return offer


# ---------------------------------------------------------------------------
# 3. Place an order - UNSAFE version (for Task 02.6.1: demonstrate the anomaly)
# ---------------------------------------------------------------------------
def place_order_unsafe(session: Session, user_id: int, offer_id: int, quantity: int = 1,
                        sync=None) -> Order:
    """
    Classic read-before-write antipattern (see Lecture 05, slide "ANTIPATTERN:
    READ BEFORE WRITE"): SELECT the current quantity in Python, decide in
    Python, then UPDATE. Under READ COMMITTED (Postgres' default) two
    concurrent transactions can both read the same quantity_available before
    either commits its UPDATE -> lost update / overselling.

    `sync` is an optional zero-argument callable invoked between the SELECT and
    the UPDATE. The isolation test passes a threading.Barrier's wait() so that
    BOTH transactions are guaranteed to have read before EITHER writes. It does
    not create the bug - the missing row lock and the missing WHERE guard below
    are the bug - it only removes the luck from an inherently timing-dependent
    interleaving that happens on its own under real load. See ADR.md 2.1.
    """
    offer = session.query(Offer).filter(Offer.offer_id == offer_id).one()

    if sync is not None:
        sync()

    if offer.quantity_available < quantity:
        raise SoldOutError(f"Offer {offer_id} is sold out")

    # THE BUG: this UPDATE has no WHERE guard on the value we just read, and
    # no row lock was taken on the SELECT above. Two sessions can both pass
    # the check above and both apply this UPDATE.
    offer.quantity_available = offer.quantity_available - quantity

    order = Order(user_id=user_id, offer_id=offer_id, quantity=quantity,
                   status=OrderStatus.PLACED, pickup_code=str(uuid.uuid4())[:8])
    session.add(order)
    session.commit()
    return order


# ---------------------------------------------------------------------------
# 3'. Place an order - SAFE version (Task 02.6.2: the fix)
# ---------------------------------------------------------------------------
def place_order(session: Session, user_id: int, offer_id: int, quantity: int = 1) -> Order:
    """
    Fix: a single atomic conditional UPDATE. The WHERE clause re-checks
    quantity_available >= quantity in the SAME statement that decrements it,
    so Postgres' row-level locking (taken implicitly by UPDATE) makes the
    check-and-decrement atomic - no explicit application-level lock needed.
    This is the "LETS BE ATOMIC" pattern from Lecture 05.

    If two sessions race, the second one's UPDATE simply matches 0 rows
    (because the first one already lowered the quantity), so rowcount == 0
    and we raise SoldOutError instead of overselling.
    """
    result = session.execute(
        text(
            """
            UPDATE offers
            SET quantity_available = quantity_available - :qty
            WHERE offer_id = :offer_id
              AND quantity_available >= :qty
            """
        ),
        {"qty": quantity, "offer_id": offer_id},
    )

    if result.rowcount == 0:
        session.rollback()
        raise SoldOutError(f"Offer {offer_id} is sold out")

    order = Order(user_id=user_id, offer_id=offer_id, quantity=quantity,
                   status=OrderStatus.PLACED, pickup_code=str(uuid.uuid4())[:8])
    session.add(order)
    session.commit()
    return order


# ---------------------------------------------------------------------------
# 4. Pay for an order
# ---------------------------------------------------------------------------
def pay_order(session: Session, order_id: int, amount: float, method: str = "card") -> Payment:
    payment = Payment(order_id=order_id, amount=amount, method=method, status="captured")
    session.add(payment)
    order = session.query(Order).filter(Order.order_id == order_id).one()
    order.status = OrderStatus.PAID
    session.commit()
    return payment


# ---------------------------------------------------------------------------
# 5. Leave a review
# ---------------------------------------------------------------------------
def leave_review(session: Session, order_id: int, rating: int, comment: str = "") -> Review:
    review = Review(order_id=order_id, rating=rating, comment=comment)
    session.add(review)
    session.commit()
    return review
