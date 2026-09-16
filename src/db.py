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

DATABASE_URL = f"postgresql+psycopg2://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

print("DB connection URL:", DATABASE_URL.replace(DB_PASSWORD, "***"))

def get_engine(echo: bool = False):
    """One engine per process. pool_size/max_overflow matter for the perf test."""
    return create_engine(
        DATABASE_URL,
        echo=echo,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
    )


def get_session_factory(engine=None):
    engine = engine or get_engine()
    return sessionmaker(bind=engine, expire_on_commit=False)


if __name__ == "__main__":
    # quick connectivity check: python -m src.db
    eng = get_engine(echo=False)
    with eng.connect() as conn:
        print("Connected OK:", DATABASE_URL.replace(DB_PASSWORD, "***"))
