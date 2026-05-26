#!/usr/bin/env python3
"""Analyze cgroup baseline DB — Day 8 outputs.

Reads /path/to/samples.db, prints:
  1. Data quality check
  2. Top N services by memory peak
  3. Recommended cgroup rules (Ansible host_vars ready-to-paste)
  4. PSI candidates for next sprint

Usage:
  python3 analyze.py --db ~/cgroup-data/192.168.0.1/var/lib/cgroup-baseline/samples.db
"""
import argparse
import sqlite3
import sys
from pathlib import Path

try:
    import pandas as pd
except ImportError:
    print("ERROR: pandas not installed. Run: pip install pandas", file=sys.stderr)
    raise SystemExit(1)


# ---------- Load + Quality ----------

def load_db(path: Path) -> pd.DataFrame:
    db = sqlite3.connect(str(path))
    df = pd.read_sql("SELECT * FROM samples", db)
    db.close()
    return df


def quality_check(df: pd.DataFrame, db_path: Path) -> None:
    print("=" * 70)
    print("QUALITY CHECK")
    print("=" * 70)
    size_mb = db_path.stat().st_size / 1024 / 1024
    print(f"DB:           {db_path.name} ({size_mb:.1f} MB)")
    print(f"rows:         {len(df):,}")
    print(f"cgroups:      {df['cgroup'].nunique()} distinct")

    ts_min, ts_max = df['ts'].min(), df['ts'].max()
    span = ts_max - ts_min
    print(f"timespan:     {span / 86400:.2f} days "
          f"({pd.Timestamp(ts_min, unit='s')} → {pd.Timestamp(ts_max, unit='s')})")

    # Sampling regularity
    ts_unique = sorted(df['ts'].unique())
    if len(ts_unique) > 1:
        deltas = pd.Series([ts_unique[i + 1] - ts_unique[i]
                            for i in range(len(ts_unique) - 1)])
        gaps = deltas[deltas > 120]
        print(f"samples:      {len(ts_unique)} timer fires "
              f"(median gap {int(deltas.median())}s)")
        if len(gaps) > 0:
            print(f"  WARN: {len(gaps)} gaps > 120s (max {gaps.max()}s) — "
                  f"check journalctl for collector failures")
        else:
            print(f"  OK: no gaps > 120s")

    # NULL coverage on key columns
    print(f"null coverage:")
    for col in ['memory_current', 'memory_peak', 'cpu_usage_usec',
                'mem_psi_some_avg60', 'cpu_psi_some_avg60',
                'io_psi_some_avg60']:
        if col in df.columns:
            pct = df[col].isna().sum() / len(df) * 100
            mark = "  " if pct < 5 else "!!"
            print(f"  {mark} {col:<26} {pct:>5.1f}%")


# ---------- Per-service summary ----------

def per_service_summary(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate stats per cgroup (system.slice only)."""
    sdf = df[df['cgroup'].str.startswith('system.slice/')].copy()
    if len(sdf) == 0:
        return pd.DataFrame()

    # Per-window CPU usage: cores used between consecutive samples
    sdf = sdf.sort_values(['cgroup', 'ts'])
    sdf['cpu_delta_step'] = sdf.groupby('cgroup')['cpu_usage_usec'].diff()
    sdf['ts_delta_step']  = sdf.groupby('cgroup')['ts'].diff()
    # cores in this window = delta_usec / (delta_sec * 1_000_000)
    sdf['cpu_cores_step'] = sdf['cpu_delta_step'] / (sdf['ts_delta_step'] * 1_000_000)

    grouped = sdf.groupby('cgroup')

    s = pd.DataFrame({
        'peak_mb':         grouped['memory_peak'].max() / 1024 / 1024,
        'avg_mb':          grouped['memory_current'].mean() / 1024 / 1024,
        'p95_mb':          grouped['memory_current'].quantile(0.95) / 1024 / 1024,
        'cpu_delta_usec':  grouped['cpu_usage_usec'].max() - grouped['cpu_usage_usec'].min(),
        'ts_span':         grouped['ts'].max() - grouped['ts'].min(),
        'cpu_cores_peak':  grouped['cpu_cores_step'].max(),   # peak per-window
        'cpu_cores_p95':   grouped['cpu_cores_step'].quantile(0.95),
        'mem_psi':         grouped['mem_psi_some_avg60'].mean(),
        'cpu_psi':         grouped['cpu_psi_some_avg60'].mean(),
        'io_psi':          grouped['io_psi_some_avg60'].mean(),
        'oom_kills':       grouped['memory_events_oom_kill'].max(),
        'cpu_throttles':   grouped['cpu_nr_throttled'].max(),
        'samples':         grouped.size(),
    }).reset_index()

    # CPU cores used (average over whole period) = cumulative cpu_time / wall_time
    s['cpu_cores'] = s['cpu_delta_usec'] / (s['ts_span'] * 1_000_000)
    s['cpu_cores'] = s['cpu_cores'].fillna(0)
    s['cpu_cores_peak'] = s['cpu_cores_peak'].fillna(0)
    s['cpu_cores_p95']  = s['cpu_cores_p95'].fillna(0)

    s['service'] = s['cgroup'].str.replace('system.slice/', '', regex=False)

    return s.sort_values('peak_mb', ascending=False).reset_index(drop=True)


# ---------- Rule recommendations ----------

def round_to_size(mb: float) -> str:
    if mb < 64:
        return "64M"           # minimum sensible value
    elif mb < 1024:
        return f"{int(round(mb / 64)) * 64}M"
    elif mb < 10 * 1024:
        return f"{mb / 1024:.1f}G"
    else:
        return f"{int(round(mb / 1024))}G"


def cpu_weight_from_cores(cores: float) -> int:
    if cores >= 2.0:
        return 200
    elif cores >= 0.5:
        return 100
    elif cores >= 0.1:
        return 50
    else:
        return 25


def recommend_rules(summary: pd.DataFrame, top_n: int = 10) -> pd.DataFrame:
    """For top N services by memory peak, compute recommended cgroup rules."""
    top = summary.head(top_n).copy()
    top['memory_max']  = top['peak_mb'].apply(lambda x: round_to_size(x * 1.3))
    top['memory_high'] = top['peak_mb'].apply(lambda x: round_to_size(x * 1.15))
    # cpu_weight: use peak (with small fallback to avg if peak is NaN/0)
    top['cpu_weight']  = top['cpu_cores_peak'].fillna(top['cpu_cores']).apply(cpu_weight_from_cores)
    return top[['service', 'peak_mb', 'cpu_cores', 'cpu_cores_peak',
                'memory_max', 'memory_high', 'cpu_weight',
                'oom_kills', 'mem_psi']]


# ---------- PSI candidates ----------

def psi_candidates(summary: pd.DataFrame,
                   mem_t: float = 1.0,
                   cpu_t: float = 1.0,
                   io_t: float = 1.0) -> pd.DataFrame:
    """Services with notable PSI signals (above absolute thresholds)."""
    mask = ((summary['mem_psi'] > mem_t) |
            (summary['cpu_psi'] > cpu_t) |
            (summary['io_psi'] > io_t))
    out = summary[mask].copy()
    if len(out) == 0:
        return out
    out['psi_type'] = out.apply(
        lambda r: '+'.join(filter(None, [
            'mem' if r['mem_psi'] > mem_t else None,
            'cpu' if r['cpu_psi'] > cpu_t else None,
            'io'  if r['io_psi']  > io_t else None,
        ])), axis=1)
    return out[['service', 'psi_type',
                'mem_psi', 'cpu_psi', 'io_psi',
                'peak_mb']].sort_values(
        ['psi_type', 'mem_psi', 'cpu_psi', 'io_psi'], ascending=False)


# ---------- Output formatters ----------

def print_top_table(summary: pd.DataFrame, top_n: int) -> None:
    print(f"\n{'=' * 90}")
    print(f"TOP {top_n} SERVICES BY MEMORY PEAK")
    print('=' * 90)
    top = summary.head(top_n)
    print(f"{'service':<45} {'mem_peak':>9} {'mem_avg':>9} "
          f"{'cpu_avg':>8} {'cpu_p95':>8} {'cpu_peak':>9}")
    print('-' * 95)
    for _, r in top.iterrows():
        svc = r['service'][:43]
        print(f"{svc:<45} "
              f"{r['peak_mb']:>7.0f} MB "
              f"{r['avg_mb']:>7.0f} MB "
              f"{r['cpu_cores']:>8.2f} "
              f"{r['cpu_cores_p95']:>8.2f} "
              f"{r['cpu_cores_peak']:>9.2f}")


def print_rules_table(rules: pd.DataFrame) -> None:
    print(f"\n{'=' * 100}")
    print("RECOMMENDED CGROUP RULES (Day 9 canary candidates)")
    print('=' * 100)
    print(f"{'service':<35} {'peak_mb':>8} {'cpu_avg':>8} {'cpu_peak':>9} "
          f"{'memory_max':>11} {'memory_high':>12} {'cpu_weight':>11}")
    print('-' * 100)
    for _, r in rules.iterrows():
        svc = r['service'][:33]
        print(f"{svc:<35} "
              f"{r['peak_mb']:>8.0f} "
              f"{r['cpu_cores']:>8.2f} "
              f"{r['cpu_cores_peak']:>9.2f} "
              f"{r['memory_max']:>11} "
              f"{r['memory_high']:>12} "
              f"{r['cpu_weight']:>11}")

    has_oom = rules[rules['oom_kills'] > 0]
    if len(has_oom) > 0:
        print(f"\n  WARN: {len(has_oom)} service(s) had OOM kills in baseline period:")
        for _, r in has_oom.iterrows():
            print(f"    - {r['service']} ({r['oom_kills']} kills) — limit too low?")


def print_psi_table(psi: pd.DataFrame) -> None:
    print(f"\n{'=' * 70}")
    print("PSI CANDIDATES (next-sprint review)")
    print('=' * 70)
    if len(psi) == 0:
        print("(no service exceeded thresholds 1.0% — system is well-resourced)")
        return
    print(f"{'service':<35} {'type':>8} {'mem%':>6} {'cpu%':>6} {'io%':>6} {'peak':>9}")
    print('-' * 80)
    for _, r in psi.iterrows():
        svc = r['service'][:33]
        print(f"{svc:<35} {r['psi_type']:>8} "
              f"{r['mem_psi']:>6.2f} {r['cpu_psi']:>6.2f} {r['io_psi']:>6.2f} "
              f"{r['peak_mb']:>7.0f}MB")


def print_host_vars(rules: pd.DataFrame, top_n: int) -> None:
    print(f"\n{'=' * 70}")
    print(f"ANSIBLE host_vars READY TO PASTE (top {top_n})")
    print('=' * 70)
    print("# paste into ansible/host_vars/<host>.yml")
    print("cgroup_rules:")
    for _, r in rules.head(top_n).iterrows():
        print(f"  {r['service']}:")
        print(f"    memory_max:  \"{r['memory_max']}\"")
        print(f"    memory_high: \"{r['memory_high']}\"")
        print(f"    cpu_weight:  {r['cpu_weight']}")


def print_next_steps() -> None:
    print(f"\n{'=' * 70}")
    print("NEXT STEPS")
    print('=' * 70)
    print("""
1. Pick 3-5 services from the rules table above. Bias towards
   LOW-RISK services first (log collectors, cron jobs) — see report Q2.

2. Copy the host_vars YAML block into:
     ansible/host_vars/<target-host>.yml
   (delete the services you don't want in round 1)

3. Day 9 canary: pick ONE service, dry-run then apply:
     cd ansible
     ansible-playbook -i inventory.ini playbooks/apply-cgroup-rules.yml \\
       --check --diff --limit <canary-host> --tags <service>
     # then real run, then SSH in and `systemctl restart <service>`
     # observe for 1 working day before rolling out to others

4. PSI candidates → write into report §2.1 for next-sprint review.
""")


# ---------- Main ----------

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--db', required=True, type=Path, help='Path to samples.db')
    ap.add_argument('--top', type=int, default=20, help='Top N by memory peak')
    ap.add_argument('--rules-top', type=int, default=5, help='Generate rules for top N')
    ap.add_argument('--mem-psi', type=float, default=1.0, help='PSI threshold for memory')
    ap.add_argument('--cpu-psi', type=float, default=1.0, help='PSI threshold for cpu')
    ap.add_argument('--io-psi',  type=float, default=1.0, help='PSI threshold for io')
    ap.add_argument('--csv', type=Path, help='Optional: write full summary to CSV')
    args = ap.parse_args()

    if not args.db.exists():
        print(f"ERROR: DB not found: {args.db}", file=sys.stderr)
        raise SystemExit(1)

    df = load_db(args.db)
    if len(df) == 0:
        print("DB is empty.", file=sys.stderr)
        raise SystemExit(1)

    quality_check(df, args.db)

    summary = per_service_summary(df)
    if len(summary) == 0:
        print("\nNo system.slice services found — check INCLUDE_PATTERNS.",
              file=sys.stderr)
        raise SystemExit(1)

    print_top_table(summary, args.top)

    rules = recommend_rules(summary, args.rules_top)
    print_rules_table(rules)

    psi = psi_candidates(summary, args.mem_psi, args.cpu_psi, args.io_psi)
    print_psi_table(psi)

    print_host_vars(rules, args.rules_top)
    print_next_steps()

    if args.csv:
        summary.to_csv(args.csv, index=False)
        print(f"\nFull summary written to {args.csv}")


if __name__ == "__main__":
    main()
