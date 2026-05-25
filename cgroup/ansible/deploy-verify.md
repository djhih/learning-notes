# Collector 部署 + 驗證 checklist

> 從 `cgroup/ansible/` 目錄跑。每步看到「預期」的輸出再往下。

## 認證設定備忘

目前狀態：`alice` user + 只有密碼

每個 ansible 指令都要加：

```
--ask-pass --ask-become-pass
```

未來切到 SSH key 後 → 移除 `--ask-pass`
未來拿到 NOPASSWD sudo → 移除 `--ask-become-pass`

---

## Phase 0 — 部署前環境檢查

### Step 0.1 — 連線通且 sudo 可用

```bash
ansible -i inventory.ini baseline_targets -m ping --ask-pass
# 預期：192.168.0.1 | SUCCESS => {"ping": "pong"}

ansible -i inventory.ini baseline_targets -b -m command -a 'id' \
  --ask-pass --ask-become-pass
# 預期：uid=0(root)  ← sudo 升到 root 成功
```

### Step 0.2 — cgroup v2 + Python sqlite3 module

```bash
ansible -i inventory.ini baseline_targets -a 'stat -fc %T /sys/fs/cgroup' --ask-pass
# 預期：cgroup2fs

ansible -i inventory.ini baseline_targets \
  -a 'python3 -c "import sqlite3; print(sqlite3.sqlite_version)"' \
  --ask-pass
# 預期：印出版本號（例如 3.37.2）
```

兩個都過才往下。如果是 `cgroup` 而非 `cgroup2fs`，server 是 v1，不在此 toolkit 支援範圍。

### Step 0.3 — 數一下 cgroup 規模 + 確認 Docker / k8s 情境

```bash
ansible -i inventory.ini baseline_targets -b -m shell -a '
echo "=== 會被 collector 抓到的 ==="
ls -d /sys/fs/cgroup/system.slice/*.service 2>/dev/null | wc -l | xargs echo "system.slice services:"
ls -d /sys/fs/cgroup/system.slice/*.scope 2>/dev/null | wc -l | xargs echo "system.slice scopes:"
ls -d /sys/fs/cgroup/user.slice/user-*.slice 2>/dev/null | wc -l | xargs echo "user slices:"
echo ""
echo "=== 沒抓但可能你 care 的 ==="
ls -d /sys/fs/cgroup/docker/* 2>/dev/null | wc -l | xargs echo "Docker cgroupfs containers:"
find /sys/fs/cgroup/kubepods.slice -name "*.scope" 2>/dev/null | wc -l | xargs echo "k8s pod scopes:"
echo ""
echo "=== Docker cgroup driver（如果有跑 Docker） ==="
which docker > /dev/null && docker info 2>/dev/null | grep -i "cgroup driver" || echo "(no docker)"
' --ask-pass --ask-become-pass
```

判讀：

| 看到 | 意義 |
|---|---|
| 前三個加總 30~200 | 正常規模，往下跑 |
| 前三個 > 500 | 大規模，可以跑但要注意 DB 成長（看 §5）|
| Docker cgroupfs containers > 0 | **要加 pattern**：編輯 `collector/collect.py` 第 12-16 行，加 `"docker/*"` |
| k8s pod scopes > 0 | **要加 pattern**：加 `"kubepods.slice/**/*.scope"` |
| Docker driver: systemd | 不用改 collector，已涵蓋 |
| Docker driver: cgroupfs | 同上「要加 pattern」|

---

## Phase 1 — 部署

### Step 1.1 — Dry-run（可選，看會做什麼）

```bash
ansible-playbook -i inventory.ini playbooks/deploy-collector.yml \
  --check --diff --ask-pass --ask-become-pass
```

**注意**：最後一個 task `Enable and start collector timer` 在 `--check` 下會 fail，是已知行為（檔案沒真的寫入所以 systemctl 找不到 unit）。**正式跑會 OK**。

### Step 1.2 — 實跑

```bash
ansible-playbook -i inventory.ini playbooks/deploy-collector.yml \
  --ask-pass --ask-become-pass
```

預期最後：

```
PLAY RECAP **********************************
192.168.0.1 : ok=5  changed=4  unreachable=0  failed=0
```

`failed=0` = 成功。`changed=4` 表示有 4 個 task 做了實際變更（建目錄、copy script、copy .service、copy .timer），第 5 個 `Enable and start timer` 也算 ok 但通常算 changed 或 ok。

---

## Phase 2 — 部署後驗證

### Step 2.1 — Timer 啟用且在跑

```bash
ansible -i inventory.ini baseline_targets -b \
  -a 'systemctl is-enabled cgroup-baseline.timer' \
  --ask-pass --ask-become-pass
# 預期：enabled

ansible -i inventory.ini baseline_targets -b \
  -a 'systemctl is-active cgroup-baseline.timer' \
  --ask-pass --ask-become-pass
# 預期：active
```

### Step 2.2 — 下次觸發時間（< 60 秒後）

```bash
ansible -i inventory.ini baseline_targets -b \
  -a 'systemctl list-timers cgroup-baseline.timer --no-pager' \
  --ask-pass --ask-become-pass
```

預期：`NEXT` 欄看到未來時間，`LEFT` 應該 < 60s。

### Step 2.3 — Collector 至少跑過一次

```bash
ansible -i inventory.ini baseline_targets -b \
  -a 'journalctl -u cgroup-baseline.service -n 10 --no-pager' \
  --ask-pass --ask-become-pass
```

預期看到：

```
... Starting cgroup v2 baseline collector (oneshot)...
... cgroup-baseline-collect[NNN]: [TIMESTAMP] wrote N samples to /var/lib/cgroup-baseline/samples.db
... Deactivated successfully.
... Finished cgroup v2 baseline collector (oneshot).
```

`wrote N samples` 那行是關鍵 — 表示 collector 真的有採到資料寫進 DB。

如果沒看到 `wrote N samples`，看看有沒有 traceback / error，貼回來看。

---

## Phase 3 — 等 5 分鐘確認資料累積

等 5 分鐘讓 timer 跑 4～5 輪，用 `scripts/check-db.py` 跑診斷（避免 inline python 的 shell escaping 問題）：

```bash
ansible -i inventory.ini baseline_targets -b \
  -m script -a 'scripts/check-db.py' \
  --ask-pass --ask-become-pass
```

ansible 會把 script 複製到 server 上跑，輸出大致：

```
db_size:  0.03 MB (/var/lib/cgroup-baseline/samples.db)
rows:     236
cgroups:  47
timespan: 240 sec (4.0 min, first=1778526225, last=1778526465)
samples:  5 (timer fired 5 times)

top 10 by memory_current at most-recent sample:
  12629.4 MB  user.slice/user-1000.slice
    502.0 MB  system.slice/snapd.service
    ...

last 5 sample timestamps:
  ts=1778526465  rows=47
  ts=1778526405  rows=47
  ts=1778526345  rows=47
  ts=1778526285  rows=47
  ts=1778526225  rows=48
```

預期：

| 欄位 | 預期值 |
|---|---|
| `rows` | ≈ `cgroups × samples`（例：47 × 5 = 235 左右） |
| `cgroups` | 你 server 上 collector 抓得到的 cgroup 數，跟 Step 0.3 那邊算的一致 |
| `timespan` | 接近 240～300 秒（5 分鐘 = 4～5 個 60s 間隔） |
| `samples` | timer 跑過的次數，4～5 |
| top 10 | 看到 service 名稱 + 合理的 MB 數值 |

如果 `rows` 沒在長 / `cgroups` 不合理小 / `samples` 沒增加，停下來 debug。

### 如果 script 跑不起來（少見）

可能 ansible 版本太舊不支援 `-m script` 的某些選項。fallback 用 raw inline：

```bash
# 直接看 DB 大小有沒有在長
ansible -i inventory.ini baseline_targets -b \
  -a 'ls -lh /var/lib/cgroup-baseline/samples.db' \
  --ask-pass --ask-become-pass

# 跑 sqlite3 CLI（要 server 上有裝）
ansible -i inventory.ini baseline_targets -b \
  -a 'sqlite3 /var/lib/cgroup-baseline/samples.db "SELECT COUNT(*) FROM samples"' \
  --ask-pass --ask-become-pass
```

DB 持續變大 = collector 正常運作。

---

## Phase 4 — 紀錄 deploy 時間 + 後續 check point

```bash
date
```

寫進工作紀錄：

```
2026-MM-DD HH:MM  collector deployed to 192.168.0.1
                  cgroups monitored: N
                  next check: +24h
                  full baseline ready: 2026-05-19 (Day 8)
```

---

## §5 — DB 成長率參考

每筆 row ≈ 200 bytes（22 INT/REAL + index）。

| cgroup 規模 | DB 成長 / 天 | 2 週累積 |
|---|---|---|
| 50 | 14 MB | 196 MB |
| 200 | 58 MB | 800 MB |
| 500 | 144 MB | 2 GB |
| 1000 | 288 MB | 4 GB |

2 週 sprint 內 `/var/lib/cgroup-baseline/` 用量都在可接受範圍。**長期跑（> 3 月）才要設 retention 砍舊資料**。

預估 DB 大小：

```bash
ansible -i inventory.ini baseline_targets -b \
  -a 'ls -lh /var/lib/cgroup-baseline/samples.db' \
  --ask-pass --ask-become-pass
# 部署當下 < 100KB
# 1 天後 ~14MB（50 cgroup 規模）
```

24 小時後跑這個確認成長率符合預期。

---

## 緊急情況：rollback collector

如果發現 collector 出問題要立刻停掉：

```bash
ansible -i inventory.ini baseline_targets -b -m shell -a '
systemctl disable --now cgroup-baseline.timer
rm -f /etc/systemd/system/cgroup-baseline.{service,timer}
rm -f /usr/local/bin/cgroup-baseline-collect
systemctl daemon-reload
echo "removed"
' --ask-pass --ask-become-pass
```

DB 資料預設保留在 `/var/lib/cgroup-baseline/samples.db`。要連資料一起砍再加 `rm -rf /var/lib/cgroup-baseline`。

---

## 拉資料回控制機

```bash
mkdir -p ~/cgroup-data
ansible -i inventory.ini baseline_targets -b -m fetch \
  -a 'src=/var/lib/cgroup-baseline/samples.db dest=~/cgroup-data/ flat=no' \
  --ask-pass --ask-become-pass

# 檔案位置
ls ~/cgroup-data/192.168.0.1/var/lib/cgroup-baseline/samples.db
```

之後用 pandas 分析這份 DB。