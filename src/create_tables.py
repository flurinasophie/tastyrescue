"""
Task 02.1 - Create the tables.

Run: python -m src.create_tables            # create what is missing (safe)
     python -m src.create_tables --reset    # DROP our tables first, then create

Everything happens inside the schema from DB_SCHEMA (see src/db.py), so we can
never touch another team's tables on the shared course database - not even with
--reset.
"""
import argparse

from src.db import get_engine, DB_SCHEMA
from src.models import Base


def create_all(drop_first: bool = False, echo: bool = False):
    engine = get_engine(echo)
    if drop_first:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    print(f"Tables in schema {DB_SCHEMA}:", list(Base.metadata.tables.keys()))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true",
                        help="drop our tables (and their data) before creating them")
    parser.add_argument("--echo", action="store_true", help="log every SQL statement")
    args = parser.parse_args()

    if args.reset:
        answer = input(f"This DELETES all data in schema {DB_SCHEMA}. Type 'yes' to continue: ")
        if answer.strip().lower() != "yes":
            raise SystemExit("Aborted.")

    create_all(drop_first=args.reset, echo=args.echo)
