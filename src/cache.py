"""
Part 03 - the NoSQL side: Redis connection and key layout.

Everything Part 03 puts into Redis belongs to ONE entity: the *view* of an
offer (someone opening a surprise bag's detail page). Nothing here is money,
nothing here is a reservation - see ADR.md section 5 for why that line is
exactly where we drew it.

Key layout
----------
  offer:{id}            HASH    cached offer detail page (cache-aside, TTL)
  offer:{id}:views      STRING  view counter, INCR'd on every page view
  views:dirty           SET     offer ids whose counter has not been flushed
  views:stream          STREAM  the raw view events, flushed to Postgres later

Why a SET of dirty ids: we must never do `KEYS offer:*:views` in production
(O(n) over the whole keyspace, blocks the single-threaded server). The set is
maintained by the writer and read by the flusher.
"""
import os

import redis
from dotenv import load_dotenv

load_dotenv()

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))

# How long a cached offer hash is served before we go back to Postgres.
# This TTL is exactly the width of the stale-read window in
# tests/consistency_test.py - see ADR.md section 4.2.
OFFER_CACHE_TTL_SECONDS = int(os.getenv("OFFER_CACHE_TTL_SECONDS", "30"))

VIEW_STREAM = "views:stream"
DIRTY_SET = "views:dirty"


def get_redis(decode_responses: bool = True) -> redis.Redis:
    return redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        db=REDIS_DB,
        decode_responses=decode_responses,
    )


def offer_key(offer_id: int) -> str:
    return f"offer:{offer_id}"


def views_key(offer_id: int) -> str:
    return f"offer:{offer_id}:views"


if __name__ == "__main__":
    r = get_redis()
    print(f"Connected to Redis {REDIS_HOST}:{REDIS_PORT} db={REDIS_DB}")
    print("  ping:", r.ping())
    info = r.info("server")
    print("  version:", info["redis_version"])
    print("  keys:", r.dbsize())
