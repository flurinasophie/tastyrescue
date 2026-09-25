"""
Task 03.4 - what we gave up by moving views out of Postgres.

Four demonstrations, all of them visible in the output and in the database:

  4.1  LOST WRITES        the flusher crashes between "take from Redis" and
                          "write to Postgres". Views that existed are gone from
                          both systems.
  4.2  DIRTY COUNTER      no crash at all: a non-atomic GET-then-DEL flush
                          throws away every view that arrives in that window.
                          The atomic GETDEL version keeps them - same test,
                          both branches.
  4.3  STALE READ         Postgres says the bag is sold out, the cache still
                          says it is available, and the customer is told to
                          come and get it. The window is measured in ms.
  4.4  DUPLICATES         the other failure mode: the stream flusher writes
                          Postgres first and crashes before advancing its
                          cursor, so the same views are inserted twice.

These tests PASS when the damage is reproduced - the damage is the deliverable.
Part 02's isolation test was the other way around (it fails on purpose) because
there we were showing a bug we then fixed. Here there is nothing to fix: this
is the price of the design, and Task 03.5 in ADR.md argues why we pay it.

Run:
  python -m pytest tests/consistency_test.py -v -s
"""
import datetime
import time

import pytest
from sqlalchemy import text

from src.cache import get_redis, offer_key, views_key
from src.db import get_engine, get_session_factory
from src.models import Offer, Store
from src.operations import place_order, SoldOutError
from src.views import (
    SimulatedCrash, flush_view_counters, flush_view_stream, load_offer_into_cache,
    postgres_view_count, redis_view_count, reset_view_state, view_offer_redis,
)

ENGINE = get_engine()
Session = get_session_factory(ENGINE)


@pytest.fixture
def r():
    return get_redis()


@pytest.fixture
def offer_id():
    """A fresh offer per test, so the numbers below are unambiguous."""
    now = datetime.datetime.now()
    with Session() as s:
        store_id = s.query(Store.store_id).first()[0]
        offer = Offer(store_id=store_id, description="Part 03 consistency test bag",
                      price_original=12.0, price_discounted=4.0, quantity_available=1,
                      pickup_from=now, pickup_until=now + datetime.timedelta(hours=2),
                      view_count=0)
        s.add(offer)
        s.commit()
        return offer.offer_id


def issue_views(r, offer_id, n, session=None):
    for _ in range(n):
        view_offer_redis(r, offer_id, user_id=None, session=session)


# ---------------------------------------------------------------------------
# 4.1 lost writes
# ---------------------------------------------------------------------------
def test_4_1_crash_in_flusher_loses_views(r, offer_id):
    """The flusher is at-most-once: it clears Redis first and writes Postgres
    second. A crash in that window destroys views that really happened."""
    with Session() as s:
        reset_view_state(r, s, [offer_id])
        load_offer_into_cache(s, r, offer_id)

        issue_views(r, offer_id, 500, session=s)
        assert redis_view_count(r, [offer_id]) == 500

        with pytest.raises(SimulatedCrash) as crash:
            flush_view_counters(s, r, crash_probability=1.0)

        in_redis = redis_view_count(r, [offer_id])
        in_postgres = postgres_view_count(s, [offer_id])

        print(f"\n4.1 offer {offer_id}: 500 real page views")
        print(f"    crash: {crash.value}")
        print(f"    Redis now holds:    {in_redis}")
        print(f"    Postgres now holds: {in_postgres}")
        print(f"    >>> {500 - in_redis - in_postgres} views vanished from both systems.")

        assert in_redis == 0, "Redis was cleared by the flusher before it crashed"
        assert in_postgres == 0, "Postgres never received them"

        # The system keeps working, which is what makes this dangerous: nothing
        # is broken afterwards, the number is just quietly too low forever.
        issue_views(r, offer_id, 500, session=s)
        flush_view_counters(s, r, crash_probability=0.0)
        final = postgres_view_count(s, [offer_id])
        print(f"    after 500 more views and a clean flush: view_count={final} "
              f"(1000 views really happened, {1000 - final} lost, {100 * (1000 - final) / 1000:.0f}%)")
        assert final == 500


# ---------------------------------------------------------------------------
# 4.2 dirty counter - no crash needed
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("atomic,expected_lost", [(False, 10), (True, 0)])
def test_4_2_non_atomic_flush_drops_concurrent_views(r, offer_id, atomic, expected_lost):
    """GET, then DEL is Part 02's read-then-write antipattern in Redis: every
    view that arrives between the two commands is deleted without being
    counted. GETDEL does both in one command, so nothing is lost - the views
    that arrive after it simply belong to the next flush."""
    with Session() as s:
        reset_view_state(r, s, [offer_id])
        load_offer_into_cache(s, r, offer_id)
        issue_views(r, offer_id, 100, session=s)

        # 10 customers open the page in exactly the wrong microsecond.
        def concurrent_views():
            issue_views(r, offer_id, 10, session=s)

        flush_view_counters(s, r, atomic=atomic, sync=concurrent_views)
        # whatever is left in Redis is not lost, it belongs to the next flush
        flush_view_counters(s, r)

        counted = postgres_view_count(s, [offer_id])
        lost = 110 - counted
        print(f"\n4.2 atomic={atomic}: 110 real views, Postgres counted {counted}, lost {lost}")
        assert lost == expected_lost


# ---------------------------------------------------------------------------
# 4.3 stale read
# ---------------------------------------------------------------------------
def test_4_3_cache_serves_a_sold_out_bag(r, offer_id):
    """The cache holds quantity_available. Postgres sells the last bag. Until
    the TTL expires, the app happily tells customers the bag is still there."""
    ttl = 2
    with Session() as s:
        reset_view_state(r, s, [offer_id])
        load_offer_into_cache(s, r, offer_id, ttl=ttl)

        user_id = s.execute(text("SELECT user_id FROM users LIMIT 1")).scalar()
        place_order(s, user_id, offer_id, quantity=1)   # the last bag is gone
        sold_out_at = time.perf_counter()

        truth = s.execute(text("SELECT quantity_available FROM offers WHERE offer_id = :o"),
                          {"o": offer_id}).scalar()
        cached = view_offer_redis(r, offer_id, user_id=user_id, session=s)

        print(f"\n4.3 offer {offer_id} right after the last bag was sold:")
        print(f"    Postgres (the truth): quantity_available = {truth}")
        print(f"    Redis    (what the customer sees): quantity_available = "
              f"{cached['quantity_available']}")
        assert truth == 0
        assert int(cached["quantity_available"]) == 1, "the cache is serving a sold-out bag"

        # What the customer experiences: the page says "1 left", the reservation
        # fails. We sold them a promise we could not keep.
        with pytest.raises(SoldOutError):
            place_order(s, user_id, offer_id, quantity=1)
        print("    the customer taps 'reserve' -> SoldOutError")

        # How long does the lie last? Until the TTL runs out.
        while r.exists(offer_key(offer_id)):
            time.sleep(0.05)
        window_ms = (time.perf_counter() - sold_out_at) * 1000
        print(f"    >>> stale-read window: {window_ms:.0f} ms (TTL was {ttl}s)")
        assert window_ms > 0


# ---------------------------------------------------------------------------
# 4.4 duplicates - the opposite trade-off
# ---------------------------------------------------------------------------
def test_4_4_stream_flush_duplicates_on_crash(r, offer_id):
    """The stream flusher writes Postgres first and moves its cursor second.
    A crash in that window costs us nothing but duplicates: the same 50 views
    are inserted again on the next run. For an event log we prefer this over
    4.1's silent loss, because duplicates are still in the data and can be
    removed later."""
    with Session() as s:
        reset_view_state(r, s, [offer_id])
        load_offer_into_cache(s, r, offer_id)
        issue_views(r, offer_id, 50, session=s)

        with pytest.raises(SimulatedCrash) as crash:
            flush_view_stream(s, r, crash_probability=1.0)
        print(f"\n4.4 crash: {crash.value}")

        flush_view_stream(s, r, crash_probability=0.0)
        rows = s.execute(text("SELECT count(*) FROM offer_views WHERE offer_id = :o "
                              "AND source = 'redis'"), {"o": offer_id}).scalar()
        print(f"    50 real views -> {rows} rows in offer_views "
              f"({rows - 50} duplicates, 0 lost)")
        assert rows == 100
