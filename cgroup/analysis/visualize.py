#!/usr/bin/env python3
"""Visualize cgroup baseline data — produce PNG charts for report.

Usage:
  python3 visualize.py --db ~/cgroup-data/samples-20260520-1200.db --out ~/cgroup-plots

Outputs 4 PNG files into --out directory:
  top_memory_peak.png     — top N services by memory peak (bar)
  memory_timeline.png     — memory.current over time, top services (line)
  memory_distribution.png — memory variation per service (box plot)
  psi_timeline.png        — PSI signals over time (3 stacked lines: cpu/mem/io)
"""
import argparse
import sqlite3
import sys
from pathlib import Path

try:
    import pandas as pd
    import matplotlib
    matplotlib.use("Agg")            # headless: no display needed
    import matplotlib.pyplot as plt
except ImportError as e:
    print(f"ERROR: {e}", file=sys.stderr)
    print("Install: pip install pandas matplotlib", file=sys.stderr)
    raise SystemExit(1)


def load_db(path: Path) -> pd.DataFrame:
    db = sqlite3.connect(str(path))
    df = pd.read_sql("SELECT * FROM samples", db)
    db.close()

    # Filter to system.slice services (skip user.slice, since not the real workload)
    df = df[df["cgroup"].str.startswith("system.slice/")].copy()
    df["service"] = df["cgroup"].str.replace("system.slice/", "", regex=False)
    df["datetime"] = pd.to_datetime(df["ts"], unit="s")
    df["memory_mb"] = df["memory_current"] / 1024 / 1024
    df["memory_peak_mb"] = df["memory_peak"] / 1024 / 1024
    return df


def plot_top_memory_peak(df: pd.DataFrame, out: Path, top_n: int = 20) -> None:
    """Horizontal bar: top N services by observed memory peak."""
    top = (df.groupby("service")["memory_peak_mb"]
             .max()
             .sort_values(ascending=True)
             .tail(top_n))
    fig, ax = plt.subplots(figsize=(10, max(4, top_n * 0.35)))
    top.plot.barh(ax=ax)
    ax.set_title(f"Top {top_n} services by memory peak")
    ax.set_xlabel("Memory peak (MB)")
    ax.set_ylabel("")
    for i, v in enumerate(top.values):
        ax.text(v, i, f"  {v:.0f}", va="center", fontsize=8)
    fig.tight_layout()
    fig.savefig(out / "top_memory_peak.png", dpi=120, bbox_inches="tight")
    plt.close(fig)


def plot_memory_timeline(df: pd.DataFrame, out: Path, top_n: int = 8) -> None:
    """Line: memory.current over time for top N services by peak."""
    top_services = (df.groupby("service")["memory_peak_mb"]
                      .max().nlargest(top_n).index)
    fig, ax = plt.subplots(figsize=(14, 6))
    for svc in top_services:
        sub = df[df["service"] == svc].sort_values("datetime")
        ax.plot(sub["datetime"], sub["memory_mb"],
                label=svc[:30], linewidth=0.9)
    ax.set_title(f"memory.current over time — top {top_n} services")
    ax.set_xlabel("Time")
    ax.set_ylabel("Memory (MB)")
    ax.legend(loc="upper left", fontsize=8, ncol=2)
    ax.grid(True, alpha=0.3)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out / "memory_timeline.png", dpi=120, bbox_inches="tight")
    plt.close(fig)


def plot_memory_distribution(df: pd.DataFrame, out: Path, top_n: int = 15) -> None:
    """Box plot: memory.current distribution per top service.

    Shows variability — wide box = bursty, narrow = stable.
    Useful for deciding memory_high vs memory_max delta.
    """
    top_services = (df.groupby("service")["memory_peak_mb"]
                      .max().nlargest(top_n).index)
    sub = df[df["service"].isin(top_services)]
    order = (sub.groupby("service")["memory_mb"]
                .median().sort_values().index)
    box_data = [sub[sub["service"] == s]["memory_mb"].dropna().values for s in order]
    fig, ax = plt.subplots(figsize=(12, max(5, top_n * 0.35)))
    ax.boxplot(box_data, labels=[s[:25] for s in order], vert=False)
    ax.set_title(f"memory.current distribution — top {top_n} by peak")
    ax.set_xlabel("Memory (MB)")
    ax.grid(True, alpha=0.3, axis="x")
    fig.tight_layout()
    fig.savefig(out / "memory_distribution.png", dpi=120, bbox_inches="tight")
    plt.close(fig)


def plot_psi_timeline(df: pd.DataFrame, out: Path, top_n: int = 6) -> None:
    """3-panel stacked: cpu/mem/io PSI over time, top services by total PSI."""
    psi_cols = ["cpu_psi_some_avg60", "mem_psi_some_avg60", "io_psi_some_avg60"]
    score = (df.groupby("service")[psi_cols].mean().fillna(0))
    score["total"] = score.sum(axis=1)
    top_services = score["total"].nlargest(top_n).index

    fig, axes = plt.subplots(3, 1, figsize=(14, 10), sharex=True)
    for ax, col, title in zip(
        axes,
        psi_cols,
        ["CPU PSI (some, avg60)",
         "Memory PSI (some, avg60)",
         "IO PSI (some, avg60)"],
    ):
        for svc in top_services:
            sub = df[df["service"] == svc].sort_values("datetime")
            ax.plot(sub["datetime"], sub[col],
                    label=svc[:30], linewidth=0.8)
        ax.set_title(title)
        ax.set_ylabel("% time stalled")
        ax.legend(loc="upper left", fontsize=7, ncol=2)
        ax.grid(True, alpha=0.3)
    axes[-1].set_xlabel("Time")
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out / "psi_timeline.png", dpi=120, bbox_inches="tight")
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True, type=Path, help="Path to samples.db")
    ap.add_argument("--out", type=Path, default=Path("plots"), help="Output directory")
    ap.add_argument("--top", type=int, default=20, help="Top N for bar / box charts")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"ERROR: DB not found: {args.db}", file=sys.stderr)
        raise SystemExit(1)

    args.out.mkdir(parents=True, exist_ok=True)

    print(f"Loading {args.db}...")
    df = load_db(args.db)
    if len(df) == 0:
        print("ERROR: no system.slice rows in DB", file=sys.stderr)
        raise SystemExit(1)
    print(f"Loaded {len(df):,} rows, {df['service'].nunique()} services, "
          f"span {df['datetime'].max() - df['datetime'].min()}")

    print("(1/4) top_memory_peak.png ...")
    plot_top_memory_peak(df, args.out, args.top)

    print("(2/4) memory_timeline.png ...")
    plot_memory_timeline(df, args.out)

    print("(3/4) memory_distribution.png ...")
    plot_memory_distribution(df, args.out, args.top)

    print("(4/4) psi_timeline.png ...")
    plot_psi_timeline(df, args.out)

    print(f"\nDone. Plots in {args.out}/")
    for p in sorted(args.out.glob("*.png")):
        size_kb = p.stat().st_size / 1024
        print(f"  {p.name}  ({size_kb:.0f} KB)")


if __name__ == "__main__":
    main()