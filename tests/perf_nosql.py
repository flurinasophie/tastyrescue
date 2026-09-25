"""
Task 03.1 - NoSQL base performance: how fast can we create new records?

The entity is the view event (someone opened a bag's detail page), written as
one record per view into the Redis stream `views:stream`. No SQL involved,
nothing cached, nothing read - this measures the write path alone, so it is
the ceiling everything else in Part 03 is compared against.

Modes:
  --mode naive       one XADD per round-trip, one record at a time
  --mode pipeline    --batch records shipped in one packet, answered in one
  --threads N        N client threads in parallel (the server stays
                     single-threaded; this fills its socket queue instead)

Run:
  python -m tests.perf_nosql --mode naive    --n 20000
  python -m tests.perf_nosql --mode pipeline --n 200000 --batch 1000
  python -m tests.perf_nosql --mode pipeline --n 500000 --batch 1000 --threads 8
"""
import argparse
import random
import threading
import time

from src.cache import get_redis, VIEW_STREAM

BENCH_STREAM = "bench:views"


def write_naive(r, n, offer_ids, user_ids):
    for _ in range(n):
        r.xadd(BENCH_STREAM,
               {"offer_id": random.choice(offer_ids), "user_id": random.choice(user_ids)},
               maxlen=2_000_000, approximate=True)


def write_pipelined(r, n, offer_ids, user_ids, batch):
    done = 0
    while done < n:
        this_batch = min(batch, n - done)
        pipe = r.pipeline(transaction=False)
        for _ in range(this_batch):
            pipe.xadd(BENCH_STREAM,
                      {"offer_id": random.choice(offer_ids), "user_id": random.choice(user_ids)},
                      maxlen=2_000_000, approximate=True)
        pipe.execute()
        done += this_batch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["naive", "pipeline"], default="pipeline")
    parser.add_argument("--n", type=int, default=100_000, help="records to write in total")
    parser.add_argument("--batch", type=int, default=1000)
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--keep", action="store_true", help="do not delete the bench stream first")
    args = parser.parse_args()

    offer_ids = list(range(1, 201))
    user_ids = list(range(1, 2001))

    r = get_redis()
    if not args.keep:
        r.delete(BENCH_STREAM)
    r.ping()  # pay the connection setup before the clock starts

    per_thread = args.n // args.threads
    label = (f"mode={args.mode}, n={args.n}, threads={args.threads}"
             + (f", batch={args.batch}" if args.mode == "pipeline" else ""))
    print(f"Running {label} ...")

    def worker():
        conn = get_redis()
        conn.ping()
        if args.mode == "naive":
            write_naive(conn, per_thread, offer_ids, user_ids)
        else:
            write_pipelined(conn, per_thread, offer_ids, user_ids, args.batch)

    threads = [threading.Thread(target=worker) for _ in range(args.threads)]
    start = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - start

    written = per_thread * args.threads
    print(f"RESULT {label}: {written} records in {elapsed:.2f}s -> "
          f"{written / elapsed:,.0f} records/sec")
    print(f"  stream length now: {r.xlen(BENCH_STREAM):,} "
          f"(bench stream, separate from the real {VIEW_STREAM})")


if __name__ == "__main__":
    main()
