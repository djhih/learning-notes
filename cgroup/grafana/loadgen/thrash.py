#!/usr/bin/env python3
"""Page-cache thrasher: repeatedly read a file larger than the cgroup's memory
limit, forcing evict-then-reread.

On the Grafana "Host Detail" dashboard this lights the Victims panels
("Refault rate", "Reclaim — direct vs kswapd"): each reread finds the pages
already evicted and faults them back in, which is exactly a workingset refault.

Only thrashes inside a memory-capped cgroup (see run.sh). With no cap there is
enough RAM and nothing is ever evicted.
"""
import argparse
import os
import signal
import sys
import time


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--path", default="/var/tmp/loadgen-thrash.dat")
    ap.add_argument("--size-mb", type=int, default=1024, help="working-set file size (MB)")
    ap.add_argument("--duration", type=float, default=600.0)
    args = ap.parse_args()

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # run the finally: cleanup on stop

    nbytes = args.size_mb * 1024 * 1024
    if not os.path.exists(args.path) or os.path.getsize(args.path) < nbytes:
        with open(args.path, "wb") as f:
            f.write(b"\0" * nbytes)
    # drop it from cache so this cgroup becomes the first toucher (first-touch accounting)
    fd = os.open(args.path, os.O_RDONLY)
    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    os.close(fd)

    deadline = time.monotonic() + args.duration
    passes = 0
    try:
        while time.monotonic() < deadline:
            with open(args.path, "rb") as f:
                while f.read(4 * 1024 * 1024):
                    pass
            passes += 1
            print(f"reread pass {passes}", flush=True)
    finally:
        try:
            os.unlink(args.path)
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    main()
