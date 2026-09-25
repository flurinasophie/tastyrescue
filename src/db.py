"""
Database connection setup.

Reads connection info from environment variables (see .env.example).
Works against the shared course PostgreSQL (from the lecture slides) or
a local Postgres (e.g. via `docker compose up -d`, see docker-compose.yml).
"""
import os
from dotenv import load_dotenv
load_dotenv()
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

#host:database-1.c32ikym4q9dx.ap-southeast-1.rds.amazonaws.com
#Port: 5432
#u:student
#p:HSBCN323TestDb36111

# Defaults match the local docker-compose setup. Override with a .env file
# (copy .env.example -> .env) to point at the shared RDS instance instead.
DB_USER = os.getenv("DB_USER", "student")
DB_PASSWORD = os.getenv("DB_PASSWORD", "HSBCN323TestDb36111")
DB_HOST = os.getenv("DB_HOST", "database-1.c32ikym4q9dx.ap-southeast-1.rds.amazonaws.com")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "postgres")
# All our tables live in this schema, so we never touch other teams' tables
DB_SCHEMA = os.getenv("DB_SCHEMA", "tastyrescue")

DATABASE_URL = f"postgresql+psycopg2://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

# Part 03: the same Postgres, but on this machine (docker compose up -d db).
# Having both lets us separate "Redis is a different engine" from "Redis is in
# the same room" when we compare - see ADR.md section 3.
LOCAL_DATABASE_URL = os.getenv(
    "LOCAL_DATABASE_URL", "postgresql+psycopg2://postgres:postgres@localhost:5434/tastyrescue"
)
# "rds" (shared course server in Singapore) or "local" (docker on this machine)
DB_TARGET = os.getenv("DB_TARGET", "rds")

print("DB connection URL:", DATABASE_URL.replace(DB_PASSWORD, "***"))


def database_url(target: str | None = None) -> str:
    target = (target or DB_TARGET).lower()
    if target == "local":
        return LOCAL_DATABASE_URL
    if target == "rds":
        return DATABASE_URL
    raise ValueError(f"unknown DB target {target!r}, expected 'rds' or 'local'")


def get_engine(echo: bool = False, target: str | None = None):
    """One engine per process. pool_size/max_overflow matter for the perf test."""
    return create_engine(
        database_url(target),
        echo=echo,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
        connect_args={"options": f"-csearch_path={DB_SCHEMA}"},
    )


def get_session_factory(engine=None):
    engine = engine or get_engine()
    return sessionmaker(bind=engine, expire_on_commit=False)


if __name__ == "__main__":
    # quick connectivity check: python -m src.db
    eng = get_engine(echo=False)
    with eng.connect() as conn:
        print("Connected OK:", DATABASE_URL.replace(DB_PASSWORD, "***"))
