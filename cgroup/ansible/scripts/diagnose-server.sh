#!/bin/bash
# diagnose-server.sh — runs on cgroup target server via `ansible -m script`.
# All output captured by ansible and shown in diagnose.sh log.

set +e   # don't bail on individual command failures

echo "===== SERVER DIAGNOSTIC at $(date -Iseconds) ====="

# ---------- 2a. Collector files installed? ----------
echo ""
echo "--- 2a. Collector files installed ---"
ls -la /etc/systemd/system/cgroup-baseline.service 2>&1
ls -la /etc/systemd/system/cgroup-baseline.timer   2>&1
ls -la /usr/local/bin/cgroup-baseline-collect      2>&1
ls -la /var/lib/cgroup-baseline/                   2>&1

# ---------- 2b. Timer / service known to systemd? ----------
echo ""
echo "--- 2b. systemd unit state ---"
echo "is-enabled: $(systemctl is-enabled cgroup-baseline.timer 2>&1)"
echo "is-active : $(systemctl is-active  cgroup-baseline.timer 2>&1)"
systemctl list-timers cgroup-baseline.timer --no-pager 2>&1 | head -3
systemctl list-unit-files 'cgroup-baseline.*' 2>&1

# ---------- 2c. Has service ever run? (journal evidence) ----------
echo ""
echo "--- 2c. Journal evidence (cgroup-baseline.service runs) ---"
echo "wrote-samples count in last 30 days:"
journalctl --since "30 days ago" -u cgroup-baseline.service --no-pager 2>/dev/null \
  | grep -c "wrote.*samples"
echo "last 5 service journal lines:"
journalctl -u cgroup-baseline.service --no-pager -n 5 2>&1

# ---------- 2d. Server identity ----------
echo ""
echo "--- 2d. Server identity ---"
echo "machine-id: $(cat /etc/machine-id 2>/dev/null)"
echo "hostname  : $(hostname)"
echo "uptime    : $(uptime)"
echo "boot      : $(uptime -s)"

# ---------- 2e. Virtualization / container detection ----------
echo ""
echo "--- 2e. Virtualization / container ---"
echo "systemd-detect-virt: $(systemd-detect-virt 2>&1)"
[ -f /.dockerenv ]        && echo "/.dockerenv         : EXISTS (Docker container)" \
                          || echo "/.dockerenv         : absent"
[ -d /run/.containerenv ] && echo "/run/.containerenv  : EXISTS (podman/CRI-O)" \
                          || echo "/run/.containerenv  : absent"

# ---------- 2f. Root filesystem type ----------
echo ""
echo "--- 2f. Root filesystem type ---"
mount | grep " / " | head -3

# ---------- 2g. Filesystem creation times (reimage signature) ----------
echo ""
echo "--- 2g. Filesystem times (reimage signature) ---"
echo "/           ctime : $(stat -c '%y' / 2>/dev/null)"
echo "/var        ctime : $(stat -c '%y' /var 2>/dev/null)"
echo "/var/log    ctime : $(stat -c '%y' /var/log 2>/dev/null)"
echo "/etc        ctime : $(stat -c '%y' /etc 2>/dev/null)"

# ---------- 2h. Journal storage config ----------
echo ""
echo "--- 2h. Journal storage config ---"
grep -E "^Storage" /etc/systemd/journald.conf 2>/dev/null \
  || echo "Storage: (default — auto)"
[ -d /var/log/journal ] \
  && echo "/var/log/journal: EXISTS — journal is PERSISTENT" \
  || echo "/var/log/journal: ABSENT — journal is VOLATILE (lost on reboot!)"

# ---------- 2i. cloud-init / auto-provisioning evidence ----------
echo ""
echo "--- 2i. cloud-init evidence ---"
if [ -d /var/lib/cloud/instances ]; then
  echo "cloud-init instances:"
  ls /var/lib/cloud/instances/ 2>&1
else
  echo "(no /var/lib/cloud/ — not a cloud-init managed instance)"
fi

# ---------- 2j. Recent package activity ----------
echo ""
echo "--- 2j. Recent package activity (reimage signature) ---"
TODAY=$(date +%Y-%m-%d)
echo "dpkg install events today ($TODAY): $(grep "^$TODAY" /var/log/dpkg.log 2>/dev/null | grep -c ' install ')"
echo "yum/dnf install events today: $(grep "$TODAY" /var/log/dnf.log /var/log/yum.log 2>/dev/null | grep -c -i 'install')"
echo "/var/log/dpkg.log size: $(stat -c '%s bytes' /var/log/dpkg.log 2>/dev/null)"

echo ""
echo "===== END SERVER DIAGNOSTIC ====="
