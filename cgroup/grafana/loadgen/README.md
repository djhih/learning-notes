# loadgen — make the triage dashboard move

Small stressors that create cgroup load so you can watch the Grafana
**Host Detail** dashboard react (http://127.0.0.1:3000 → cgroup → Host Detail).

| program | what it does | panels it lights |
| --- | --- | --- |
| `hog.py`    | grows anonymous (real) memory and holds it | Culprits · anon, Anon growth |
| `thrash.py` | rereads a file bigger than its memory cap  | Victims · Refault rate, Reclaim (direct) |
| `burn.py`   | spins CPU workers                          | CPU usage rate, CPU throttle / PSI (with a quota) |

## Run

```bash
./run.sh all                 # hog + thrash + burn, auto-stops after 5 min
./run.sh hog                 # just one generator
./run.sh thrash --duration 120
./run.sh status              # live memory of the loadgen-* units
./run.sh clean               # stop everything now
```

By default the generators run as transient **`--user`** units (no root),
charged to your user slice — enough to see the panels move. Use **`--system`**
for one distinct service row per generator on the host view (needs `sudo`, and
enables CPU-quota throttling since the cpu controller is available there).

Each generator also runs standalone, e.g. `python3 hog.py --size-mb 800`, but
then it lands in whatever cgroup your shell is in and `thrash.py` won't evict
without a memory cap.
