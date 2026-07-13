# cgroup v2 → Prometheus → Grafana 部署 step-by-step

把 cgroup v2 各項指標量化成 Prometheus exporter，再用 Grafana 視覺化的完整操作手冊。

分兩階段：

- **Stage A — 本機 smoke test**：全程在 laptop 上，不用 SSH，先證明 exporter ↔ Prometheus ↔ Grafana 接得起來。
- **Stage B — 正式部署**：用 Ansible 把 exporter 推到目標機，透過 SSH 隧道讓 laptop 的 Prometheus 抓。

先做 A 再做 B；A 過了，B 幾乎只剩填 host。

> 概念、架構、metrics 命名、設計取捨 → 看 [`README.md`](README.md)。
> 這份只講「怎麼一步步做、怎麼驗證、卡住怎麼查」。

---

## 0. 前置檢查（read-only，先確認再動手）

| 需求 | 檢查指令 | 本機實測 |
|---|---|---|
| cgroup v2 | `stat -fc %T /sys/fs/cgroup` | 要看到 `cgroup2fs` ✅ |
| Docker daemon | `docker info >/dev/null && echo ok` | ✅ Docker 29.x |
| docker compose | `docker compose version` 或 `docker-compose version` | ⚠️ 只有 v1 `docker-compose`（見下方坑 1） |
| Ansible（Stage B） | `ansible-playbook --version` | ✅ 2.10.8 |
| SSH 密碼登入（Stage B） | 能 `ssh alice@<target>` | ✅ 不需 sshpass，tunnel/monitor 用 ssh 多工，互動輸密碼一次 |
| `.env.example` 存在 | `ls grafana/monitoring/.env.example` | ✅ |

以下所有路徑都從 repo 內的 `cgroup/grafana/` 起算。

---

## Stage A — 本機 smoke test

### A1. 直接跑 exporter（先不經 Ansible）

本機測試時 Prometheus 容器是用 `host.docker.internal`（= docker0 gateway `172.17.0.1`）來抓，**不是** `127.0.0.1`，所以本機測試要 bind `0.0.0.0`：

```bash
cd grafana/exporter
CGROUP_EXPORTER_LISTEN=0.0.0.0:9753 python3 exporter.py
```

另開一個終端確認有資料：

```bash
curl -s 127.0.0.1:9753/metrics | head -40
# 應該看到一堆 cgroup_memory_current_bytes{service="..."} 數值
curl -s 127.0.0.1:9753/metrics | grep -c '^cgroup_'        # series 行數，應為數百~上千
```

> 正式機上 exporter 是 bind `127.0.0.1`（systemd unit 內設定，保持 loopback-only）。
> `0.0.0.0` 只用在這個本機 demo。

### A2. 起 Prometheus + Grafana

```bash
cd grafana/monitoring
cp .env.example .env
$EDITOR .env                       # 設 GRAFANA_ADMIN_PASSWORD（.env 已被 gitignore）
PYTHONNOUSERSITE=1 docker-compose up -d     # 見坑 1，為什麼要這個前綴
docker-compose ps                            # 兩個容器都應 Up
```

### A3. 驗證 Prometheus 抓得到

打開 <http://localhost:9090/targets>，`cgroup-exporter` 應在 ~15s 後變 **UP**。

或用指令（不開瀏覽器）：

```bash
curl -s http://localhost:9090/api/v1/targets \
  | python3 -c "import sys,json;t=json.load(sys.stdin)['data']['activeTargets'][0];print(t['health'], t.get('lastError',''))"
# 預期： up
```

跑兩個關鍵 PromQL 確認有資料：

```bash
curl -s 'http://localhost:9090/api/v1/query?query=count(cgroup_memory_current_bytes)'
curl -s 'http://localhost:9090/api/v1/query?query=count(rate(cgroup_cpu_usage_usec_total%5B1m%5D))'
```

### A4. 驗證 Grafana

<http://localhost:3000> → 登入 `admin` / `.env` 設的密碼 → 左側 Dashboards → **cgroup → cgroup overview**。

~1 分鐘內 7 個 panel 應該都長出資料：top memory bar、CPU rate、CPU/memory/io PSI、OOM kill stat、memory trend。上方 `service` 下拉可以篩特定服務。

### A5. 收掉本機測試（要做 Stage B 前）

```bash
# 停掉前景的 exporter：Ctrl-C（或 kill 該 python3 PID）
cd grafana/monitoring
PYTHONNOUSERSITE=1 docker-compose down        # 想保留資料就不要下
```

---

## Stage B — 正式部署到目標機

### B1. SSH 連線（不需 sshpass）

`tunnel.sh` 跟 `monitor.sh` 已改用 **SSH 連線多工（ControlMaster）**，不需要 sshpass、不需要裝任何東西、不需要 sudo：

- `tunnel.sh`：單一連線，ssh 互動式問密碼一次。
- `monitor.sh`：開頭建一條 master 連線輸密碼一次，之後 loop 內的 ssh/scp 全部重用那條 socket，不再問密碼。

只要你能 `ssh alice@<target>`（密碼登入）就能用。

### B2. 填 inventory

編 `ansible/inventory.ini`，把 `[exporter_targets]` 下的 `localhost` 換成真正的機器：

```ini
[exporter_targets]
target1 ansible_host=10.0.0.12 ansible_user=alice
```

其他群組（`baseline_targets` / `cgroup_targets`）不用動。

### B3. 部署 exporter

```bash
cd ansible
ansible-playbook -i inventory.ini playbooks/deploy-exporter.yml \
  --ask-pass --ask-become-pass
```

這會把 `exporter.py` 放到目標機 `/usr/local/bin/cgroup-exporter`，裝好 systemd unit 並 enable + start。

### B4. 在目標機上驗證

```bash
ssh alice@<target> 'systemctl status cgroup-exporter.service'        # active (running)
ssh alice@<target> 'curl -s 127.0.0.1:9753/metrics | head -20'       # 有 cgroup_* 輸出
ssh alice@<target> 'systemctl restart cgroup-exporter && systemctl is-active cgroup-exporter'
```

### B5. 起 SSH 隧道（laptop，掛 tmux）

```bash
tmux new -s cgroup-tunnel
bash grafana/scripts/tunnel.sh <target-ip>     # USER_AT 預設 alice
# 輸入一次密碼；Ctrl-B D detach
```

`tunnel.sh` 的本機端會 bind 在 **docker0 gateway（`172.17.0.1:9753`）**，不是 `127.0.0.1` —— 因為 Prometheus 容器是從那個 IP 進來的（見坑 2）。如果你的 docker0 gateway 不是這個，用 `LOCAL_BIND=0.0.0.0 bash grafana/scripts/tunnel.sh <target-ip>`。

### B6. 起（或留著）監控 stack 並做最終驗證

```bash
cd grafana/monitoring
PYTHONNOUSERSITE=1 docker-compose up -d
```

- <http://localhost:9090/targets> → `cgroup-exporter` **UP**
- <http://localhost:3000> → dashboard 顯示的是**目標機**的服務

---

## 驗收清單（definition of done）

1. `curl 127.0.0.1:9753/metrics` 回傳帶 `service` label 的 `cgroup_*` series（A 在本機 / B 在目標機）。
2. Prometheus targets 頁 `cgroup-exporter` = **UP**。
3. PromQL `cgroup_memory_current_bytes`、`rate(cgroup_cpu_usage_usec_total[1m])` 都有資料。
4. Grafana「cgroup overview」7 個 panel 都有即時資料，`service` 篩選可用。
5. （Stage B）`systemctl status cgroup-exporter` 為 `active (running)`，重啟後仍正常。

---

## 卡住怎麼查（實測過的坑）

### 坑 1：`docker-compose` 報 `Not supported URL scheme http+docker`

本機只有舊的 `docker-compose` v1.29.2（沒有 `docker compose` v2 plugin），它跟 `~/.local` 裡較新的 `requests` 不相容。

**解法**：前面加 `PYTHONNOUSERSITE=1`，強制用系統的 `requests`：

```bash
PYTHONNOUSERSITE=1 docker-compose up -d
```

（長久解：`sudo apt install docker-compose-plugin` 後改用 `docker compose`。）

### 坑 2：Prometheus target 一直 DOWN / Grafana panel 空白

Prometheus 容器抓的是 `host.docker.internal`，在 native Linux 上會解析成 **docker0 gateway `172.17.0.1`**，不是 `127.0.0.1`。

```bash
docker exec cgroup-prometheus cat /etc/hosts | grep docker.internal   # 確認解析到的 IP
```

所以監聽端必須在那個 IP 收得到：

- 本機測試 → exporter 用 `CGROUP_EXPORTER_LISTEN=0.0.0.0:9753`。
- 正式 → `tunnel.sh` 已預設把 `-L` 綁在 `172.17.0.1`（`LOCAL_BIND`）。

### target UP 但 `rate()` 沒值

`rate()` 至少要兩次 scrape（>15s）才算得出來。等一下再看。

### 坑 3：Prometheus target UP、`curl` 查得到資料，但 Grafana dashboard 全部 No Data

**這是跟坑 2 不同的另一層**：坑 2 是 Prometheus 抓不到 exporter；坑 3 是 Prometheus 有資料，但 Grafana 的 dashboard 接不到 Prometheus。兩個原因疊在一起：

**原因 A — datasource UID 不匹配（根因）**
dashboard JSON 的每個 panel 都寫死引用 `"datasource": {"uid": "prometheus"}`，但 datasource provisioning（`provisioning/datasources/prometheus.yml`）若**沒釘 `uid`**，Grafana 每次啟動會自動生一個隨機 UID（實測為 `PBFA97CFB590B2093`）→ panel 找不到那個 datasource → 全部 No Data。

```bash
# 診斷：比對 dashboard 要的 uid vs 實際 provision 出來的 uid
grep -o '"uid": *"[^"]*"' monitoring/grafana/dashboards/cgroup-overview.json | sort -u
curl -s -u admin:"$PW" http://localhost:3000/api/datasources | python3 -m json.tool | grep uid
```

**解法**：在 `provisioning/datasources/prometheus.yml` 釘死 `uid: prometheus`，重啟 grafana：

```yaml
datasources:
  - name: Prometheus
    type: prometheus
    uid: prometheus        # ← 對齊 dashboard 寫死的值，少了它就隨機
    url: http://prometheus:9090
```
```bash
PYTHONNOUSERSITE=1 docker-compose restart grafana
# 驗證：透過 uid 直接查，應回數字而非錯誤
curl -s -u admin:"$PW" \
  'http://localhost:3000/api/datasources/proxy/uid/prometheus/api/v1/query?query=count(cgroup_memory_current_bytes)'
```

**原因 B — `service` 變數殘留空值**
修好 A 之前載入過 dashboard 的話，`service` 變數當時解析失敗變成空字串，被寫進網址 `?var-service=`。F5 會沿用這個空值，panel 查 `{service=~""}` → 在 PromQL 中匹配不到任何 series（所有 series 的 `service` label 都非空）→ 還是 No Data。

**解法**：用乾淨網址強制帶 All，或在左上 `service` 下拉手動選 All，再 `Ctrl+Shift+R` 硬重整：

```
http://localhost:3000/d/cgroup-overview/cgroup-overview?var-service=$__all&from=now-30m&to=now
```

> 診斷分辨點：左上 `service` 下拉**有沒有列出 service**。空的 → 是原因 A（datasource）；有列但 panel 空 → 是原因 B（變數殘留）。

---

## 不在這份範圍內（刻意延後）

| 項目 | 狀態 |
|---|---|
| Alert 規則（`monitoring/prometheus/alerts.yml`） | stub，等累積 baseline 後再定 threshold（與 PSI 門檻決策綁一起） |
| 每服務 cgroup 限制（`ansible/group_vars/all.yml`、`apply-cgroup-rules.yml`） | 等 baseline 分析算出 memory/CPU 規則後再套 |
