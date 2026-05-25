#!/bin/bash
# diagnose.sh — full cgroup-baseline deployment diagnostic.
#
# Usage:
#   cd cgroup/ansible
#   bash scripts/diagnose.sh
#
# Saves output to diagnose-YYYYMMDD-HHMMSS.log.
# Override defaults via env vars:
#   INVENTORY=inventory.ini  AUTH='--ask-pass --ask-become-pass'  bash scripts/diagnose.sh

set -u
cd "$(dirname "$0")/.."   # cd to ansible/ regardless of where invoked

INVENTORY="${INVENTORY:-inventory.ini}"
AUTH="${AUTH:---ask-pass --ask-become-pass}"
SERVER_IP="${SERVER_IP:-192.168.0.1}"

LOG="diagnose-$(date +%Y%m%d-%H%M%S).log"

# All output (this section onward) tee'd to LOG
{
  echo "===== diagnose started at $(date -Iseconds) ====="
  echo "cwd       : $(pwd)"
  echo "inventory : $INVENTORY"
  echo "auth      : $AUTH"
  echo "server ip : $SERVER_IP"
  echo ""

  echo "==========================================================="
  echo "PART 1: LOCAL — 本機證據"
  echo "==========================================================="

  echo ""
  echo "--- 1a. ansible-related shell history (last 30) ---"
  if grep -hE "ansible" ~/.bash_history ~/.zsh_history 2>/dev/null \
       | grep -v "^#" | tail -30 \
       | grep .; then
    :
  else
    echo "  (no ansible commands in history — may be trimmed, or never run)"
  fi
  echo ""
  echo "  search for 'wrote.*samples' in history (would prove you ever saw it):"
  if grep -hE "wrote.*samples" ~/.bash_history ~/.zsh_history 2>/dev/null \
       | head -5 | grep .; then
    :
  else
    echo "  (none — history doesn't preserve command output, only commands, so this is normal)"
  fi

  echo ""
  echo "--- 1b. SSH known_hosts ---"
  if grep -E "(^|,)$SERVER_IP " ~/.ssh/known_hosts 2>/dev/null | head -3 | grep .; then
    echo "  ($SERVER_IP is known — at least one manual ssh ever happened)"
  else
    echo "  (no entry for $SERVER_IP — ansible host_key_checking=False does not write here, so this may be normal)"
  fi

  echo ""
  echo "--- 1c. Ansible fact cache (/tmp/ansible-facts/) ---"
  if [ -d /tmp/ansible-facts ]; then
    ls -la /tmp/ansible-facts/ 2>&1
    if [ -f "/tmp/ansible-facts/$SERVER_IP" ]; then
      echo ""
      echo "  cached facts for $SERVER_IP:"
      python3 - <<PYEOF 2>&1
import json, os
try:
    with open("/tmp/ansible-facts/$SERVER_IP") as f:
        d = json.load(f)
    print("  machine-id  :", d.get("ansible_machine_id", "(none)"))
    print("  hostname    :", d.get("ansible_hostname", "(none)"))
    print("  uptime (s)  :", d.get("ansible_uptime_seconds", "(none)"))
    print("  virt type   :", d.get("ansible_virtualization_type", "(none)"))
    print("  os family   :", d.get("ansible_os_family", "(none)"))
    print("  cache mtime :", os.path.getmtime("/tmp/ansible-facts/$SERVER_IP"))
except Exception as e:
    print("  parse failed:", e)
PYEOF
    fi
  else
    echo "  (/tmp/ansible-facts/ absent — control machine rebooted, or ansible never gathered facts)"
  fi

  echo ""
  echo "--- 1d. inventory / config / source files ---"
  for f in inventory.ini inventory.example ansible.cfg \
           playbooks/deploy-collector.yml \
           roles/baseline_collector/tasks/main.yml \
           ../collector/collect.py \
           ../collector/cgroup-baseline.service \
           ../collector/cgroup-baseline.timer; do
    if [ -e "$f" ]; then
      stat -c "  %y  %5s bytes  $f" "$f"
    else
      echo "  (MISSING: $f)"
    fi
  done

  echo ""
  echo "--- 1e. previous deploy logs ---"
  if ls deploy-*.log 2>/dev/null | head -5 | grep .; then
    :
  else
    echo "  (no saved deploy logs — you didn't tee output to file before)"
  fi

  echo ""
  echo "--- 1f. ansible version ---"
  ansible --version 2>&1 | head -3

  echo ""
  echo "==========================================================="
  echo "PART 2: REMOTE — server 端狀態"
  echo "==========================================================="
  echo ""

  if [ ! -f scripts/diagnose-server.sh ]; then
    echo "ERROR: scripts/diagnose-server.sh not found"
    exit 1
  fi

  ansible -i "$INVENTORY" baseline_targets -b \
    -m script -a 'scripts/diagnose-server.sh' \
    $AUTH 2>&1

  echo ""
  echo "===== diagnose finished at $(date -Iseconds) ====="
} 2>&1 | tee "$LOG"

echo ""
echo "==========================================================="
echo "結果存在：$(pwd)/$LOG"
echo "貼這個檔案給 Claude 看，會幫你判讀 + 指出問題在哪。"
echo "==========================================================="
