# loadgen — make the triage dashboard move

Small stressors that create cgroup load so you can watch the Grafana
**Host Detail** dashboard react (http://127.0.0.1:3000 → cgroup → Host Detail).

| program | what it does | panels it lights |
| --- | --- | --- |
| `hog.py`     | grows anonymous (real) memory and holds it | Top processes · memory, Memory by Service |
| `thrash.py`  | rereads a file bigger than its memory cap  | Memory/IO PSI, Disk read by Service |
| `burn.py`    | spins CPU workers at 100%                  | Top processes · CPU, CPU throttle / PSI (with a quota) |
| `cpuload.py` | holds workers at a chosen CPU% (duty cycle) | Top processes · CPU — with a known expected value to verify against |

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
