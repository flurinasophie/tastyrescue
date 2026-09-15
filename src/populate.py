"""
Task 02.3 - Populate the tables with sample rows.
Run: python -m src.populate --stores 200 --users 2000 --offers 5000
"""
import argparse
import datetime
import random

from faker import Faker
from src.db import get_engine, get_session_factory
from src.models import Store, User, Offer

fake = Faker()
CATEGORIES = ["bakery", "restaurant", "cafe", "convenience_store", "hotel_buffet"]


def populate(n_stores=200, n_users=2000, n_offers=5000, seed=42):
    random.seed(seed)
    Faker.seed(seed)

    engine = get_engine()
    Session = get_session_factory(engine)
    session = Session()

    print(f"Inserting {n_stores} stores...")
    stores = [
        Store(
            name=fake.company(),
            category=random.choice(CATEGORIES),
            address=fake.address().replace("\n", ", "),
            opening_hours="10:00-22:00",
        )
        for _ in range(n_stores)
    ]
    session.bulk_save_objects(stores, return_defaults=True)
    session.commit()

    print(f"Inserting {n_users} users...")
    users = [
        User(
            name=fake.name(),
            email=fake.unique.email(),
            phone=fake.phone_number()[:40],
            location=fake.city(),
        )
        for _ in range(n_users)
    ]
    session.bulk_save_objects(users, return_defaults=True)
    session.commit()

    store_ids = [s.store_id for s in session.query(Store.store_id).all()]

    print(f"Inserting {n_offers} offers...")
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    offers = []
    for _ in range(n_offers):
        price_original = round(random.uniform(5, 25), 2)
        offers.append(
            Offer(
                store_id=random.choice(store_ids),
                description=random.choice(
                    ["Bakery surprise bag", "Lunch surprise bag", "Dinner surprise bag"]
                ),
                price_original=price_original,
                price_discounted=round(price_original / 3, 2),
                quantity_available=random.randint(1, 10),
                pickup_from=now + datetime.timedelta(hours=random.randint(1, 6)),
                pickup_until=now + datetime.timedelta(hours=random.randint(7, 10)),
            )
        )
    session.bulk_save_objects(offers)
    session.commit()

    print("Done.")
    session.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--stores", type=int, default=200)
    parser.add_argument("--users", type=int, default=2000)
    parser.add_argument("--offers", type=int, default=5000)
    args = parser.parse_args()
    populate(n_stores=args.stores, n_users=args.users, n_offers=args.offers)
