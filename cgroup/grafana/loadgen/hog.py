#!/usr/bin/env python3
"""Anonymous-memory hog: grow real (unreclaimable) memory and hold it.

On the Grafana "Host Detail" dashboard this lights the Culprits panels
(anon, "Anon growth — Top 5"): anon memory can't be reclaimed without swap, so
a steadily growing hog is exactly what squeezes everyone else's page cache.

Run inside a cgroup (see run.sh) or directly (charged to your user slice).
"""
import argparse
import time


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--size-mb", type=int, default=512, help="target anon size (MB)")
    ap.add_argument("--grow-mb", type=int, default=32, help="MB added each step")
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between steps")
    ap.add_argument("--hold", type=float, default=600.0, help="seconds to hold at target")
    args = ap.parse_args()

    blocks = []
    grown = 0
    while grown < args.size_mb:
        step = min(args.grow_mb, args.size_mb - grown)
        block = bytearray(step * 1024 * 1024)
        for i in range(0, len(block), 4096):   # touch every page so it faults in as anon
            block[i] = 1
        blocks.append(block)
        grown += step
        print(f"anon ~{grown} MB", flush=True)
        time.sleep(args.interval)

    print(f"holding {grown} MB for {args.hold:.0f}s", flush=True)
    time.sleep(args.hold)


if __name__ == "__main__":
    main()
