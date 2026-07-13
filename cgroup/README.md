# cgroup v2 baseline + management toolkit

兩週 sprint 的產出：在 production server 上**收集 cgroup v2 baseline 數據**、**根據實測算出靜態 cgroup 規則**、**透過 Ansible 套用 + 監控 + 必要時 rollback**。

不依賴 Prometheus / Grafana / Meta oomd —— Python 標準函式庫 + SQLite + Ansible + bash，最小依賴。

---

## 目錄結構

```
cgroup/
├── collector/             # 採樣 collector
│   ├── collect.py                       Python 主程式（嵌入 schema）
│   ├── cgroup-baseline.service          systemd oneshot
│   └── cgroup-baseline.timer            每 60 秒觸發
│
├── ansible/               # 部署 / 套規則 / rollback / 監控
│   ├── ansible.cfg
│   ├── inventory.example                inventory 樣板
│   ├── deploy-verify.md                 部署 + 驗證 checklist
│   ├── playbooks/
│   │   ├── deploy-collector.yml         部署 collector
│   │   ├── apply-cgroup-rules.yml       套靜態 cgroup 規則（drop-in）
│   │   └── rollback-cgroup-rules.yml    砍掉所有規則
│   ├── roles/
│   │   ├── baseline_collector/          collector 部署 role
│   │   └── cgroup_slice/                cgroup 規則套用 role
│   └── scripts/                         運維工具
│       ├── check-db.py                  單次檢查遠端 DB
│       ├── diagnose.sh                  完整部署狀態診斷（local + remote）
│       ├── diagnose-server.sh           診斷 server 端（被 diagnose.sh 呼叫）
│       └── monitor.sh                   長存 polling + 定期 fetch DB
│
├── analysis/              # baseline 資料分析 + 視覺化
│   ├── analyze.py                       算 top services + 建議 cgroup rules
│   ├── visualize.py                     畫圖（4 張 PNG）
│   └── day8-procedure.md                Day 8 baseline 整理流程
│
├── k8s-adoption-analysis.zh-tw.md       「套用 k8s 那套」評估 + 逐項比對 + 落地 plan
├── k8s-ref/                             kubelet cm/eviction 原始碼（sparse clone，gitignored）
│
├── lecture/               # 技術筆記（HTML）
│   ├── cgroup-v2.zh-tw.html             cgroup v2 概念
│   ├── cgroup-systemd-mapping.zh-tw.html     systemd ↔ cgroup mapping
│   ├── cgroup-toolkit-quickstart.zh-tw.html  toolkit 操作手冊
│   ├── cgroup-toolkit-walkthrough.zh-tw.html toolkit 設計攻略
│   ├── cgroup-learning-roadmap.zh-tw.html    學習路線
│   ├── cgroup-setup-pitfalls.zh-tw.html      設定地雷
│   ├── meta-psi-automation.zh-tw.html        Meta PSI 自動化研究
│   ├── cgroup-grafana-realtime.zh-tw.html    v2 即時監測設計
│   └── gpu-monitor.html                       GPU 監控可行性
│
├── grafana/               # v2: 即時監測（與 v1 並存）
│   ├── exporter/                        stdlib HTTP exporter + systemd unit
│   ├── monitoring/                      laptop-side docker-compose 棧
│   ├── scripts/tunnel.sh                SSH reverse-tunnel helper
│   └── README.md
│
└── report/                # 階段性報告
    ├── cgroup-progress-2026-05-12.md         主報告
    ├── cgroup-progress-2026-05-12-djhih.md   個人版報告
    └── cgroup-progress-2026-05-12-djhih-script.md   口頭報告逐字稿
```

---

## 工作流程（標準路徑）

```
Step 1: Deploy（一次性，手動）
   ↓
   ansible-playbook -i inventory.ini playbooks/deploy-collector.yml \
     --ask-pass --ask-become-pass
   ↓
Step 2: Verify（部署完 90 秒內）
   ↓
   參照 ansible/deploy-verify.md Phase 2
   ↓
Step 3: 等 baseline 累積（5～7 天）
   ↓
   monitor.sh 可選，掛在 tmux 看健康狀態
   ↓
Step 4: 拉資料回來（Day 8）
   ↓
   ansible -m fetch  或  monitor.sh 的 hourly snapshot
   ↓
Step 5: 分析（Day 8）
   ↓
   analysis/analyze.py --db samples.db
   ↓ 拿到「top services」「建議規則」「PSI 候選」
   ↓
Step 6: 視覺化（Day 8）
   ↓
   analysis/visualize.py --db samples.db --out plots/
   ↓ 拿到 4 張 PNG 嵌進 report
   ↓
Step 7: Canary（Day 9）
   ↓
   填好 host_vars/<host>.yml 的 cgroup_rules
   ansible-playbook ... apply-cgroup-rules.yml --limit canary-host
   手動 systemctl restart <service>，盯一天
   ↓
Step 8: Rollout（Day 10～11）
   ↓
   套到剩下的 service
   ↓
Step 9: 寫 report（Day 13～14）
```

---

## 快速開始

### Deploy collector 到一台 server

```bash
cd ansible

# 1. 改 inventory
cp inventory.example inventory.ini
vi inventory.ini   # 把 IP 換成你的 server

# 2. 跑 deploy（需要密碼）
ansible-playbook -i inventory.ini playbooks/deploy-collector.yml \
  --ask-pass --ask-become-pass

# 3. 90 秒內驗證 — 參照 deploy-verify.md
```

### 監控 collector 是否還活著

```bash
cd ansible
tmux new -s monitor
bash scripts/monitor.sh
# 輸入密碼一次，Ctrl+B D detach
# 之後 tmux attach -t monitor 看狀態
```

### 等 1 週後分析

```bash
# 拉 DB 回本機
ansible -i inventory.ini baseline_targets -b -m fetch \
  -a 'src=/var/lib/cgroup-baseline/samples.db dest=~/cgroup-data/ flat=no' \
  --ask-pass --ask-become-pass

# 算 top services + 建議規則
python3 analysis/analyze.py --db ~/cgroup-data/*/var/lib/cgroup-baseline/samples.db

# 畫圖
python3 analysis/visualize.py --db ~/cgroup-data/*/var/lib/cgroup-baseline/samples.db \
  --out plots/
```

### 部署有問題不知道是哪裡

```bash
cd ansible
bash scripts/diagnose.sh
# 完整本機 + remote 診斷，輸出寫進 diagnose-YYYYMMDD-HHMMSS.log
```

---

## 工具選哪一個 — 速查

| 想做的事 | 用哪個工具 |
|---|---|
| 第一次部署 collector | `ansible-playbook playbooks/deploy-collector.yml` |
| 確認 collector 還在跑 | `scripts/check-db.py`（單次） 或 `scripts/monitor.sh`（持續） |
| 部署完查 timer/檔案/journal | `ansible/deploy-verify.md` Phase 2 |
| 部署失敗找原因 | `scripts/diagnose.sh` |
| baseline 收完算規則 | `analysis/analyze.py` |
| 畫圖嵌進報告 | `analysis/visualize.py` |
| 把規則套到 server | `ansible-playbook playbooks/apply-cgroup-rules.yml` |
| 緊急 rollback | `ansible-playbook playbooks/rollback-cgroup-rules.yml` |

---

## 認證設定

| 場景 | 設定 |
|---|---|
| 你坐電腦前手動 deploy | `--ask-pass --ask-become-pass`，prompt 兩次 |
| 自動化監控（cron / tmux long-running） | 走 SSH key + NOPASSWD sudo，或 monitor.sh 把密碼留在 process 記憶體 |
| 共用帳號、沒辦法設 SSH key、沒 NOPASSWD sudo | 用 monitor.sh tmux 方案 |

`monitor.sh` 的設計就是給「最受限環境」用 — 不需要 SSH key、不需要 sudo、密碼不落地。代價是要靠 tmux session 撐著、重開機要重輸密碼。

---

## 設計原則（為什麼這樣寫）

1. **最小依賴** — collector 用 Python 標準函式庫，部署無 pip install
2. **systemd timer 取代 daemon** — 不用 sleep loop、失敗自動復活、升級就 cp 一個檔
3. **走 systemd drop-in 不直接戳 cgroup** — systemd 會覆寫直寫的值
4. **誤跑保護** — `cgroup_rules: {}` 預設、`become: false` 可 override
5. **rollback 不靠 ansible state** — 用檔名 marker `50-cgroup.conf` 找
6. **監控不要 sudo** — DB 預設 0644，alice 直接讀；只有部署 / 套規則才升權

完整推理見 [`lecture/cgroup-toolkit-walkthrough.zh-tw.html`](lecture/cgroup-toolkit-walkthrough.zh-tw.html)。

---

## 已知 caveats

| 議題 | 狀態 |
|---|---|
| Docker container（cgroupfs driver） | 不自動抓，要加 INCLUDE_PATTERN 在 `collect.py` |
| Kubernetes pods | 不自動抓，要加遞迴 glob（與 k8s 的關係見 [`k8s-adoption-analysis.zh-tw.md`](k8s-adoption-analysis.zh-tw.md)） |
| cgroup v1 | **不支援**，只做 v2 |
| GPU 資源限制 | cgroup 控不到（已查證），要用 NVIDIA MIG |
| 自動化 deploy / rollout | 沒做，**故意**手動跑 |
| Prometheus / Grafana | v2 已加，見 [`grafana/`](grafana/)（與 v1 並存，不衝突） |

---

## 接下來（next-sprint plan）

**方向已校準：不再拉長 baseline，改成「實際部署限制 → 看 OOM → 記錄死者」的驗證閉環。**
先確認限制安不安全，k8s 風格的強化（memory_low / 分級 / PSI）往後挪。

```
1. [跳過] 長 baseline          用既有資料即可
   └ 前提：先跑 analyze.py，確認目標 service 在 baseline 期間「本來就沒有 oom_kill」
2. [既有] ansible apply 限制    從 1～2 個低風險 service 開始（log collector / cron 類）
3. [既有] 看 OOM 數量           oom_kill 計數早就在收（collect.py / exporter 都有）
   └ Grafana: increase(cgroup_memory_events_oom_kill_total[1h])
   └ 同時當 canary 健康指標 + rollback 觸發條件
4. [新做] OOM 死者明細 logger   journald→SQLite 小腳本（不要 Graylog）
   ├ 來源：kernel OOM 行（journalctl -k 的 "Killed process …"）
   ├ 欄位：時間 / service / PID / comm / RSS
   └ 要漂亮 dashboard 再上 Loki（grafana/ 已有棧，Loki 是原生 log DB）
5. [既有] rollback              關鍵 service 一出現 OOM increase 即收回
```

**會咬人的細節（部署前必讀）：**
- `oom_kill` 是 cgroup 生命週期累計值，`systemctl restart` 後歸零 → 比「前 vs 後」要用 `increase()` 或差分，別看絕對值。
- `oom`（撞 limit）≠ `oom_kill`（真殺）。撞 `oom` 但沒 `oom_kill` 通常是 `MemoryHigh` 在軟限流，是健康狀態。
- 部署 `MemoryMax` 時要決定 `memory.oom.group` / `OOMPolicy=`：殺一個 vs 整組殺，直接影響你 log 看到的數量。

**為什麼不用 Graylog**：OOM 事件很稀疏（一天個位數），扛 Elasticsearch+MongoDB 違反「最小依賴」。journald→SQLite 跟既有 collector 同 DB、同 ethos。

**已知會被往後挪的 k8s 風格強化**（缺口分析見 [`k8s-adoption-analysis.zh-tw.md`](k8s-adoption-analysis.zh-tw.md) §5）：`memory_low` 保留下限、service 分級（QoS）、PSI 自動化（`psi-responder.py`）。

> 註：本專案做的是 k8s 沒做的「baseline→規則」反推；CPU 只設 weight 不設 quota 與 k8s「set requests, not limits」最佳實務一致，刻意保留。

- **全 fleet rollout**（如果 canary + round 1 都健康）

---

## 開發紀錄

主報告：[`report/cgroup-progress-2026-05-12.md`](report/cgroup-progress-2026-05-12.md)
口頭報告稿：[`report/cgroup-progress-2026-05-12-djhih-script.md`](report/cgroup-progress-2026-05-12-djhih-script.md)