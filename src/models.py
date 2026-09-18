"""
ORM models for TastyRescue, mapped 1:1 onto the ER diagram from Part 01
(er-model-too-good-to-go.pdf): STORE -> OFFER -> ORDER -> PAYMENT / REVIEW,
with USER -> ORDER.

Entities kept deliberately close to the submitted diagram (Store/Offer/Order,
not Vendor/Branch/Bag/Reservation) so Part 02 follows what was actually
handed in, not the earlier draft wording.
"""
import datetime
import enum
from sqlalchemy import (
    Column, Integer, String, Numeric, DateTime, ForeignKey, CheckConstraint,
    Enum, Text, func
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class OrderStatus(str, enum.Enum):
    PLACED = "placed"
    PAID = "paid"
    PICKED_UP = "picked_up"
    CANCELLED = "cancelled"


class User(Base):
    __tablename__ = "users"

    user_id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)
    email = Column(String(160), nullable=False, unique=True)
    phone = Column(String(40))
    location = Column(String(160))

    orders = relationship("Order", back_populates="user")


class Store(Base):
    __tablename__ = "stores"

    store_id = Column(Integer, primary_key=True)
    name = Column(String(160), nullable=False)
    category = Column(String(80))
    address = Column(String(240))
    opening_hours = Column(String(120))

    offers = relationship("Offer", back_populates="store")


class Offer(Base):
    """One 'surprise bag' listing. This is the entity we expect to be huge
    and bursty (see Part 01, order-of-magnitude table)."""
    __tablename__ = "offers"

    offer_id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.store_id"), nullable=False, index=True)
    description = Column(String(240))
    price_original = Column(Numeric(8, 2), nullable=False)
    price_discounted = Column(Numeric(8, 2), nullable=False)
    quantity_available = Column(Integer, nullable=False)
    pickup_from = Column(DateTime)
    pickup_until = Column(DateTime, index=True)  # queried a lot: "bags closing soon"

    # Deliberate: this guard does NOT catch the lost update in Task 02.6 - the
    # counter never goes negative there, it just stops too early. See ADR 2.1.
    __table_args__ = (
        CheckConstraint("quantity_available >= 0", name="ck_offer_qty_non_negative"),
    )

    store = relationship("Store", back_populates="offers")
    orders = relationship("Order", back_populates="offer")


class Order(Base):
    """A reservation/purchase of (part of) an Offer's quantity."""
    __tablename__ = "orders"

    order_id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.user_id"), nullable=False, index=True)
    offer_id = Column(Integer, ForeignKey("offers.offer_id"), nullable=False, index=True)
    quantity = Column(Integer, nullable=False, default=1)
    status = Column(Enum(OrderStatus), nullable=False, default=OrderStatus.PLACED)
    pickup_code = Column(String(12))
    created_at = Column(DateTime, server_default=func.now())

    user = relationship("User", back_populates="orders")
    offer = relationship("Offer", back_populates="orders")
    payment = relationship("Payment", back_populates="order", uselist=False)
    review = relationship("Review", back_populates="order", uselist=False)


class Payment(Base):
    __tablename__ = "payments"

    payment_id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.order_id"), nullable=False, unique=True)
    amount = Column(Numeric(8, 2), nullable=False)
    method = Column(String(40))
    status = Column(String(20), default="captured")

    order = relationship("Order", back_populates="payment")


class Review(Base):
    __tablename__ = "reviews"

    review_id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.order_id"), nullable=False, unique=True)
    rating = Column(Integer, nullable=False)
    comment = Column(Text)
    created_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        CheckConstraint("rating BETWEEN 1 AND 5", name="ck_review_rating_range"),
    )

    order = relationship("Order", back_populates="review")
