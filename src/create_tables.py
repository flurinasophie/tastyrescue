"""
Task 02.1 - Create the tables.
Run: python -m src.create_tables
"""
from src.db import get_engine
from src.models import Base


def create_all(drop_first: bool = False):
    engine = get_engine(True)
    if drop_first:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    print("Tables created:", list(Base.metadata.tables.keys()))


if __name__ == "__main__":
    create_all(drop_first=True)
