"""
Part 03 - the workload: "a customer opens the detail page of a surprise bag".

This is deliberately NOT a single operation but a short sequence, as Task 03.2
asks for. Every page view does three things:

  1. read the offer + its store (the page content)
  2. count the view                       (offers.view_count)
  3. record the view event                (offer_views)

`view_offer_sql`   does all three in one Postgres transaction (Task 03.2).
`view_offer_redis` does all three in Redis in a single pipeline (Task 03.3),
                   and a background flusher moves the data to Postgres later.

The flushers are where we pay for it (Task 03.4). They can be told to crash
mid-flight, and the two of them fail in opposite ways on purpose:

  flush_view_counters  reads-and-clears the counter first, writes Postgres
                       second  -> at-most-once  -> a crash LOSES views
  flush_view_stream    writes Postgres first, advances the cursor second
                       -> at-least-once -> a crash DUPLICATES views

Neither is a bug we forgot to fix. Exactly-once delivery across two systems
that cannot share a transaction is not available; we get to pick which way it
breaks. See ADR.md sections 4 and 5.
"""
import random

from sqlalchemy import ARRAY, Integer, bindparam, text
from sqlalchemy.orm import Session

from src.cache import (
    DIRTY_SET, VIEW_STREAM, OFFER_CACHE_TTL_SECONDS, offer_key, views_key,
)

STREAM_CURSOR = "views:stream:cursor"

# The detail page itself: what the customer sees when they tap a bag.
DETAIL_SQL = text("""
    SELECT o.offer_id, o.description, o.price_discounted, o.quantity_available,
           s.name AS store_name
    FROM offers o
    JOIN stores s ON s.store_id = o.store_id
    WHERE o.offer_id = :offer_id
""")


class SimulatedCrash(RuntimeError):
    """The flusher process dying mid-flight (Task 03.4)."""


# ---------------------------------------------------------------------------
# Task 03.2 - the sequence, in Postgres
# ---------------------------------------------------------------------------
def view_offer_sql(session: Session, offer_id: int, user_id: int | None = None) -> dict:
    """Three statements, one transaction, one commit.

    Round-trips per call: pre-ping, BEGIN, SELECT, UPDATE, INSERT, COMMIT.
    The UPDATE is the interesting one: every viewer of the same popular bag
    updates the SAME row, so they queue up on its row lock until the holder
    commits. That is a second bottleneck on top of the network - see ADR 3.2.
    """
    row = session.execute(DETAIL_SQL, {"offer_id": offer_id}).one()

    session.execute(
        text("UPDATE offers SET view_count = view_count + 1 WHERE offer_id = :offer_id"),
        {"offer_id": offer_id},
    )
    session.execute(
        text("INSERT INTO offer_views (offer_id, user_id, source) "
             "VALUES (:offer_id, :user_id, 'sql')"),
        {"offer_id": offer_id, "user_id": user_id},
    )
    session.commit()
    return dict(row._mapping)


# ---------------------------------------------------------------------------
# Task 03.3 - the same sequence, in Redis
# ---------------------------------------------------------------------------
def load_offer_into_cache(session: Session, r, offer_id: int,
                          ttl: int = OFFER_CACHE_TTL_SECONDS) -> dict:
    """Cache-aside fill: on a miss we pay for one Postgres read, then serve the
    next `ttl` seconds from Redis.

    Note what we put in the hash: quantity_available. That is the field that
    makes the cache dangerous, and the stale-read demo in
    tests/consistency_test.py is built on exactly this line.
    """
    row = session.execute(DETAIL_SQL, {"offer_id": offer_id}).one()
    detail = {k: str(v) for k, v in row._mapping.items()}
    key = offer_key(offer_id)
    pipe = r.pipeline(transaction=True)
    pipe.hset(key, mapping=detail)
    pipe.expire(key, ttl)
    pipe.execute()
    return detail


def view_offer_redis(r, offer_id: int, user_id: int | None = None,
                     session: Session | None = None) -> dict:
    """The same three steps as view_offer_sql, but one network round-trip.

    A pipeline is not a transaction: it is four commands shipped in one packet
    and answered in one packet. That is the whole trick - we removed waiting,
    not work. The counter and the event still hit Redis; only the durability
    story changed.

    On a cache miss we fall back to Postgres (needs `session`). The counter and
    the event are recorded either way - a miss must never cost us a view.
    """
    key = offer_key(offer_id)
    pipe = r.pipeline(transaction=False)
    pipe.hgetall(key)                      # 1. the page content
    pipe.incr(views_key(offer_id))         # 2. the counter
    pipe.sadd(DIRTY_SET, offer_id)         # bookkeeping for the flusher
    pipe.xadd(VIEW_STREAM,                 # 3. the event log
              {"offer_id": offer_id, "user_id": user_id if user_id is not None else ""},
              maxlen=1_000_000, approximate=True)
    detail, _views, _dirty, _event_id = pipe.execute()

    if not detail:
        if session is None:
            raise LookupError(
                f"offer {offer_id} not in cache and no Postgres session given "
                f"to fill it from"
            )
        detail = load_offer_into_cache(session, r, offer_id)
    return detail


# ---------------------------------------------------------------------------
# Task 03.4 - the flushers, and how they break
# ---------------------------------------------------------------------------
def flush_view_counters(session: Session, r, crash_probability: float = 0.0,
                        atomic: bool = True, sync=None, rng=None) -> dict:
    """Move the Redis counters into offers.view_count.

    AT-MOST-ONCE by construction: we take the counts out of Redis first and
    write them to Postgres second. If the process dies in between, those views
    are gone from both systems - nobody is holding them any more.

    `crash_probability` makes the process die exactly in that window.

    `atomic=False` switches the read-and-clear from GETDEL (one atomic command)
    to GET followed by DEL, which is the read-then-write antipattern from Part
    02 all over again, only this time in Redis: every view that arrives between
    the GET and the DEL is deleted without ever being counted. `sync` is a
    zero-argument callable invoked in that window so a test can hit it on
    purpose instead of hoping for the right microsecond.
    """
    rng = rng or random
    offer_ids = [int(x) for x in r.smembers(DIRTY_SET)]
    if not offer_ids:
        return {"offers": 0, "views_written": 0, "views_taken": 0, "crashed": False}

    taken = []
    for offer_id in offer_ids:
        # Unmark BEFORE reading the counter, never after: a view that arrives
        # from here on re-marks the offer and is picked up by the next flush.
        # Unmarking afterwards would delete a marker set by a view we never
        # read, and that view would sit in Redis unnoticed until the next
        # unrelated view happened to mark the offer again.
        if atomic:
            pipe = r.pipeline(transaction=False)
            pipe.srem(DIRTY_SET, offer_id)
            pipe.getdel(views_key(offer_id))   # read and clear in one command
            _, n = pipe.execute()
            if sync is not None:
                sync()                       # <- concurrent views land here too,
                                             #    and survive: see the test
        else:
            r.srem(DIRTY_SET, offer_id)
            n = r.get(views_key(offer_id))
            if sync is not None:
                sync()                       # <- concurrent views land here
            r.delete(views_key(offer_id))    # ... and are thrown away
        if n:
            taken.append((offer_id, int(n)))

    views_taken = sum(n for _, n in taken)

    # The counts now exist only in this Python process's memory.
    if rng.random() < crash_probability:
        raise SimulatedCrash(
            f"flusher died holding {views_taken} views from {len(taken)} offers - "
            f"they were already removed from Redis and never reached Postgres"
        )

    if taken:
        session.execute(
            text("""
                UPDATE offers o
                SET view_count = o.view_count + v.n
                FROM (SELECT * FROM unnest(:ids, :counts) AS t(offer_id, n)) v
                WHERE o.offer_id = v.offer_id
            """),
            {"ids": [oid for oid, _ in taken], "counts": [n for _, n in taken]},
        )
        session.commit()

    return {"offers": len(taken), "views_written": views_taken,
            "views_taken": views_taken, "crashed": False}


def flush_view_stream(session: Session, r, batch: int = 1000,
                      crash_probability: float = 0.0, rng=None) -> dict:
    """Move the Redis stream events into the offer_views table.

    AT-LEAST-ONCE by construction, the opposite choice from the counter
    flusher: we write Postgres first and only then advance the cursor. A crash
    in between means the next run replays the same entries, so the events are
    never lost - they are duplicated. For an event log we prefer that, because
    duplicates can be de-duplicated later and lost rows cannot be recovered.
    """
    rng = rng or random
    cursor = r.get(STREAM_CURSOR) or "0-0"
    entries = r.xrange(VIEW_STREAM, min=f"({cursor}", max="+", count=batch)
    if not entries:
        return {"inserted": 0, "cursor": cursor, "duplicated": 0}

    offer_ids = [int(fields["offer_id"]) for _id, fields in entries]
    user_ids = [int(fields["user_id"]) if fields.get("user_id") else None
                for _id, fields in entries]
    # One statement for the whole batch, not one per event. Part 02 measured
    # what per-row round-trips cost; there is no reason to pay it again here.
    session.execute(
        text("""
            INSERT INTO offer_views (offer_id, user_id, source)
            SELECT offer_id, user_id, 'redis'
            FROM unnest(:offer_ids, :user_ids) AS t(offer_id, user_id)
        """).bindparams(
            bindparam("offer_ids", type_=ARRAY(Integer)),
            bindparam("user_ids", type_=ARRAY(Integer)),
        ),
        {"offer_ids": offer_ids, "user_ids": user_ids},
    )
    session.commit()
    rows = offer_ids

    # The rows are committed but the cursor has not moved yet. A crash here
    # means the next flush inserts them a second time.
    if rng.random() < crash_probability:
        raise SimulatedCrash(
            f"flusher died after committing {len(rows)} events but before "
            f"advancing the cursor - the next run will insert them again"
        )

    r.set(STREAM_CURSOR, entries[-1][0])
    return {"inserted": len(rows), "cursor": entries[-1][0], "duplicated": 0}


# ---------------------------------------------------------------------------
# helpers used by the tests and the perf runs
# ---------------------------------------------------------------------------
def reset_view_state(r, session: Session | None = None, offer_ids=None):
    """Clear the Redis side (and optionally the Postgres counters) so a test
    starts from a known state."""
    keys = [views_key(o) for o in (offer_ids or [])]
    keys += [offer_key(o) for o in (offer_ids or [])]
    if keys:
        r.delete(*keys)
    r.delete(DIRTY_SET, VIEW_STREAM, STREAM_CURSOR)
    if session is not None and offer_ids:
        session.execute(
            text("UPDATE offers SET view_count = 0 WHERE offer_id = ANY(:ids)"),
            {"ids": list(offer_ids)},
        )
        session.execute(
            text("DELETE FROM offer_views WHERE offer_id = ANY(:ids)"),
            {"ids": list(offer_ids)},
        )
        session.commit()


def postgres_view_count(session: Session, offer_ids) -> int:
    return session.execute(
        text("SELECT coalesce(sum(view_count), 0) FROM offers WHERE offer_id = ANY(:ids)"),
        {"ids": list(offer_ids)},
    ).scalar()


def redis_view_count(r, offer_ids) -> int:
    values = r.mget([views_key(o) for o in offer_ids])
    return sum(int(v) for v in values if v)
