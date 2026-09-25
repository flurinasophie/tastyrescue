"""
Part 03 schema changes, applied without touching the Part 02 data.

  * offers.view_count   - new column, default 0
  * offer_views         - new table (the event log)

Both are additive and idempotent, so this can be run against the shared RDS
(where our Part 02 rows live) and against the local docker Postgres.

Run:
  python -m src.migrate_part03                 # the target from .env (rds)
  python -m src.migrate_part03 --db local      # docker compose db on :5434
"""
import argparse

from sqlalchemy import text

from src.db import get_engine, DB_SCHEMA
from src.models import Base, OfferView


def migrate(target: str | None = None, echo: bool = False):
    engine = get_engine(echo, target=target)
    with engine.begin() as conn:
        # The local docker database starts empty, so our schema may not exist
        # yet. On the shared RDS it exists and our user is not allowed to
        # create schemas at all, so we must ask before we try.
        exists = conn.execute(text(
            "SELECT 1 FROM information_schema.schemata WHERE schema_name = :s"
        ), {"s": DB_SCHEMA}).scalar()
        if not exists:
            conn.execute(text(f'CREATE SCHEMA "{DB_SCHEMA}"'))
        conn.execute(text(
            f'ALTER TABLE IF EXISTS "{DB_SCHEMA}".offers '
            f'ADD COLUMN IF NOT EXISTS view_count integer NOT NULL DEFAULT 0'
        ))

    # create_all only creates what is missing: on RDS that is offer_views, on a
    # fresh local database it is the whole Part 02 schema as well.
    Base.metadata.create_all(engine)

    with engine.connect() as conn:
        n_offers = conn.execute(text("SELECT count(*) FROM offers")).scalar()
        n_views = conn.execute(text("SELECT count(*) FROM offer_views")).scalar()
    print(f"schema {DB_SCHEMA} ready: offers={n_offers}, offer_views={n_views}, "
          f"offers.view_count present")
    return engine


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", choices=["rds", "local"], default=None,
                        help="which Postgres to migrate (default: DB_TARGET from .env)")
    parser.add_argument("--echo", action="store_true")
    args = parser.parse_args()
    migrate(target=args.db, echo=args.echo)
