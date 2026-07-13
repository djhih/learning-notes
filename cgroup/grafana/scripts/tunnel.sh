#!/bin/bash
# tunnel.sh — open an SSH reverse port-forward so the laptop's Prometheus
# (in docker-compose) can scrape the target's cgroup-exporter bound to
# 127.0.0.1:9753 on the remote.
#
# No sshpass / no SSH key required: ssh prompts for the password interactively.
# The tunnel is a single long-lived connection, so you only type it once per
# connect. Run inside tmux so the tunnel survives terminal close.
# (If the link drops, the reconnect loop runs ssh again and it will prompt
#  again — reattach the tmux session to type the password.)
#
# Usage:
#   tmux new -s cgroup-tunnel
#   bash scripts/tunnel.sh <target-host>
#   # enter password once
#   # Ctrl-B D to detach
#   # tmux attach -t cgroup-tunnel   <- come back later
#
# Stop:
#   Ctrl-C in foreground, or `tmux kill-session -t cgroup-tunnel`

set -u

SERVER="${1:-${SERVER:-}}"
USER_AT="${USER_AT:-alice}"
LOCAL_PORT="${LOCAL_PORT:-9753}"
REMOTE_PORT="${REMOTE_PORT:-9753}"
# Bind the local end of the forward to the docker bridge gateway, not 127.0.0.1.
# The Prometheus container scrapes `host.docker.internal`, which on native Linux
# resolves to the docker0 gateway (172.17.0.1) — a forward bound to 127.0.0.1
# would be unreachable from inside the container. 172.17.0.x stays off the LAN.
# Override with LOCAL_BIND=0.0.0.0 if your docker0 gateway differs.
LOCAL_BIND="${LOCAL_BIND:-172.17.0.1}"

if [ -z "$SERVER" ]; then
  echo "Usage: $0 <target-host>" >&2
  echo "  or set SERVER env var. USER_AT=$USER_AT LOCAL_PORT=$LOCAL_PORT" >&2
  exit 1
fi

echo "Forwarding ${LOCAL_BIND}:${LOCAL_PORT} -> ${USER_AT}@${SERVER}:127.0.0.1:${REMOTE_PORT}"
echo "Prometheus (in docker) reaches it via host.docker.internal:${LOCAL_PORT}"
echo "You'll be asked for ${USER_AT}@${SERVER}'s password on each (re)connect."
echo "Press Ctrl-C to stop."

# Loop so a transient drop reconnects automatically. ServerAlive keeps
# NAT/firewall mappings warm. ssh prompts for the password interactively
# (no sshpass) — fine because there is only one connection to authenticate.
while true; do
  ssh \
    -o StrictHostKeyChecking=accept-new \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    -o ExitOnForwardFailure=yes \
    -N \
    -L "${LOCAL_BIND}:${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT}" \
    "${USER_AT}@${SERVER}"
  rc=$?
  echo "Tunnel exited with rc=$rc — retrying in 10s..."
  sleep 10
done
