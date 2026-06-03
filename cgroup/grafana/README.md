# cgroup v2 — Grafana 即時監測（v2 pillar）

旁邊那條 v1 路線（[`../collector/`](../collector/) + [`../analysis/`](../analysis/)）跑 60 秒 / 一次的 SQLite baseline。**這一條** 是即時視覺化：target 上跑一個 stdlib HTTP exporter，laptop 上用 docker-compose 起 Prometheus + Grafana，中間靠 SSH reverse tunnel 連起來。

兩條完全獨立，target 上同時跑沒衝突。

---

## 架構

```
Target (alice)                            Dev laptop
─────────────                             ──────────
collect.py (v1)  → /var/lib/.../samples.db     docker-compose:
                                               ├─ prometheus  :9090
exporter.py (v2)                               └─ grafana     :3000
  127.0.0.1:9753                                     ▲
        ▲                                            │ scrape host.docker.internal:9753
        └──── ssh -L 9753:127.0.0.1:9753 ────────────┘
              (scripts/tunnel.sh, password-only)
```

為什麼 exporter bind `127.0.0.1` 而不開 LAN port：

- 公司 firewall 可能不放行隨機 high port inbound，省麻煩。
- alice 是共用帳號，metrics endpoint 沒有 auth，曝在 LAN 上不妥。
- SSH 隧道本來就是 password-only 流程的一部分，沒多增信任邊界。

---

## 快速開始

> 想要逐步操作 + 每一步的驗證指令 + 踩過的坑，看 **[`DEPLOY.zh-tw.md`](DEPLOY.zh-tw.md)**（先在本機 smoke test，再正式部署）。下面是濃縮版。

### 1. 部署 exporter（一次性，手動）

```bash
cd ../ansible
# inventory.ini 把 [exporter_targets] 群組填上 host
ansible-playbook -i inventory.ini playbooks/deploy-exporter.yml \
  --ask-pass --ask-become-pass
```

驗證 target：

```bash
ssh alice@<target> 'systemctl status cgroup-exporter.service'
ssh alice@<target> 'curl -s http://127.0.0.1:9753/metrics | head -20'
```

### 2. 起隧道（laptop，掛在 tmux）

```bash
tmux new -s cgroup-tunnel
bash scripts/tunnel.sh <target-ip>
# 輸入密碼一次，Ctrl-B D detach
```

### 3. 起 Prometheus + Grafana（laptop）

```bash
cd monitoring
cp .env.example .env
vi .env                          # 設 GRAFANA_ADMIN_PASSWORD
docker compose up -d
# 本機只有舊 docker-compose v1 時改用： PYTHONNOUSERSITE=1 docker-compose up -d （見 caveats）
```

打開瀏覽器：

- Prometheus targets：<http://localhost:9090/targets> — `cgroup-exporter` 應該 `UP`
- Grafana：<http://localhost:3000> — `admin` / `.env` 設的密碼，左邊 Dashboards → cgroup → cgroup overview

---

## 目錄結構

```
grafana/
├── exporter/
│   ├── exporter.py                  Python stdlib HTTP server
│   └── cgroup-exporter.service      systemd Type=simple
├── monitoring/                      laptop-side docker-compose stack
│   ├── docker-compose.yml
│   ├── .env.example
│   ├── prometheus/
│   │   ├── prometheus.yml           scrape config
│   │   └── alerts.yml               stub（等 baseline 後再開）
│   └── grafana/
│       ├── provisioning/
│       │   ├── datasources/prometheus.yml
│       │   └── dashboards/dashboards.yml
│       └── dashboards/
│           └── cgroup-overview.json
└── scripts/
    └── tunnel.sh                    SSH reverse-tunnel + tmux pattern
```

Ansible role 在 [`../ansible/roles/cgroup_exporter/`](../ansible/roles/cgroup_exporter/)，playbook 在 [`../ansible/playbooks/deploy-exporter.yml`](../ansible/playbooks/deploy-exporter.yml)，inventory 共用 [`../ansible/inventory.ini`](../ansible/inventory.ini)。

---

## Metrics 命名

刻意用 `cgroup_*` prefix（而非 cAdvisor 的 `container_*`），避免未來接 cAdvisor 時撞名。

| 指標 | Type | 說明 |
|---|---|---|
| `cgroup_memory_current_bytes` | gauge | `memory.current` |
| `cgroup_memory_peak_bytes` | gauge | `memory.peak` |
| `cgroup_memory_max_bytes` | gauge | `memory.max`（"max" 字串時省略） |
| `cgroup_memory_high_bytes` | gauge | `memory.high` |
| `cgroup_psi_{cpu,memory,io}_{some,full}_avg{10,60}` | gauge | PSI 即時值，0–100 |
| `cgroup_cpu_usage_usec_total` | counter | 累計 CPU 使用（usec） |
| `cgroup_cpu_throttled_usec_total` | counter | 累計被 throttle 時間 |
| `cgroup_psi_{cpu,memory,io}_{some,full}_seconds_total` | counter | PSI total（轉成秒） |
| `cgroup_memory_events_{low,high,max,oom,oom_kill}_total` | counter | `memory.events` 累計 |

Labels：`cgroup="system.slice/foo.service"`、`service="foo.service"`。

Counter 在 Grafana 一律用 `rate(...)`，不要直接看絕對值。

---

## 已知 caveats

| 議題 | 狀態 / 解法 |
|---|---|
| Docker container（`docker-*.scope`） | 不抓，跟 v1 一致；要做就同步改兩邊 |
| Kubernetes pods | 不抓 |
| Laptop 睡眠 → scrape 空洞 | 接受；counter 仍 monotonic，`rate()` 還能算 |
| Grafana auth | docker-compose 用 `.env` 密碼，預設不啟用匿名 |
| metrics endpoint 沒 auth | 限定 127.0.0.1 + SSH tunnel，未來搬正式 VM 再補 mTLS 或 basic auth |
| Alert 規則 | `monitoring/prometheus/alerts.yml` 是 stub，等 baseline 累積後再決定 threshold（見 [lecture note](../lecture/cgroup-grafana-realtime.zh-tw.html)） |
| `*.scope` random suffix 爆 cardinality | 已在 exporter `EXCLUDE_PREFIXES` 多排除 `run-` |
| `host.docker.internal` 在 Linux 解析到 docker0 gateway（`172.17.0.1`）而非 `127.0.0.1` | scrape 端要在那個 IP 收得到：`tunnel.sh` 已預設 bind `172.17.0.1`（`LOCAL_BIND`）；本機 demo 則用 `CGROUP_EXPORTER_LISTEN=0.0.0.0:9753` |
| 舊 `docker-compose` v1 報 `http+docker` scheme 錯誤 | 沒裝 v2 plugin 時：`PYTHONNOUSERSITE=1 docker-compose up -d`，繞過 `~/.local` 過新的 `requests` |

---

## 跟 v1 的分工

| 用途 | 用哪邊 |
|---|---|
| 一週 baseline、想算靜態 cgroup 規則 | v1 collector + `analyze.py` |
| 即時看現在發生什麼 | v2 exporter + Grafana |
| 出書面報告（PNG 嵌進 markdown） | v1 `visualize.py` |
| 將來接 alert（PagerDuty / Slack） | v2 Prometheus Alertmanager（等 baseline 後） |

設計細節跟取捨：[`../lecture/cgroup-grafana-realtime.zh-tw.html`](../lecture/cgroup-grafana-realtime.zh-tw.html)。
