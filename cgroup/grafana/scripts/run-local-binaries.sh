#!/usr/bin/env bash
# run-local-binaries.sh — run the whole monitoring stack from release binaries,
# NO docker / NO root / NO sudo. For a locked-down demo box where you can't run
# (rootless) docker but can download + run binaries in your home dir.
#
# Starts three processes, all on 127.0.0.1 (same host, no tunnel/gateway needed):
#   exporter.py    127.0.0.1:9753   (your stdlib exporter)
#   prometheus     127.0.0.1:9090   (release binary, scrapes the exporter)
#   grafana        127.0.0.1:3000   (release binary, auto-provisions dashboard)
#
# Configs are GENERATED for binary mode (the committed docker configs point at
# container names / host.docker.internal, which don't apply here).
#
# Usage:
#   bash run-local-binaries.sh            # download (cached) + run, Ctrl-C stops all
#   GRAFANA_ADMIN_PASSWORD=secret bash run-local-binaries.sh
#   # then open http://127.0.0.1:3000  (admin / password)  -> cgroup overview
#
# Run inside tmux if you want it to survive terminal close.
set -euo pipefail

PROM_VER="${PROM_VER:-2.55.1}"
GRAF_VER="${GRAF_VER:-11.3.0}"

# --- paths ---
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
GRAFANA_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"          # cgroup/grafana
MON="$GRAFANA_ROOT/monitoring"
EXPORTER="$GRAFANA_ROOT/exporter/exporter.py"
DASH_DIR="$MON/grafana/dashboards"                    # reuse committed dashboard JSON
ALERTS="$MON/prometheus/alerts.yml"
WORK="${WORK:-$HOME/.cache/cgroup-grafana-local}"     # downloads + generated cfg + data
LOGS="$WORK/logs"

# --- admin password (reuse .env if present) ---
if [ -z "${GRAFANA_ADMIN_PASSWORD:-}" ] && [ -f "$MON/.env" ]; then
  GRAFANA_ADMIN_PASSWORD="$(grep -oP 'GRAFANA_ADMIN_PASSWORD=\K.*' "$MON/.env" 2>/dev/null || true)"
fi
GRAFANA_ADMIN_PASSWORD="${GRAFANA_ADMIN_PASSWORD:-admin}"

# --- arch ---
case "$(uname -m)" in
  x86_64)  ARCH=amd64 ;;
  aarch64|arm64) ARCH=arm64 ;;
  *) echo "ERROR: unsupported arch $(uname -m)"; exit 1 ;;
esac

command -v curl >/dev/null || { echo "ERROR: need curl"; exit 1; }
command -v python3 >/dev/null || { echo "ERROR: need python3"; exit 1; }
[ -f "$EXPORTER" ] || { echo "ERROR: exporter not found at $EXPORTER"; exit 1; }

mkdir -p "$WORK" "$LOGS"

# --- download + extract (cached) ---
PROM_DIR="$WORK/prometheus-${PROM_VER}.linux-${ARCH}"
if [ ! -x "$PROM_DIR/prometheus" ]; then
  echo "==> downloading Prometheus ${PROM_VER} (${ARCH})..."
  curl -fSL --retry 3 -o "$WORK/prom.tgz" \
    "https://github.com/prometheus/prometheus/releases/download/v${PROM_VER}/prometheus-${PROM_VER}.linux-${ARCH}.tar.gz"
  tar -xzf "$WORK/prom.tgz" -C "$WORK" && rm -f "$WORK/prom.tgz"
fi

GRAF_DIR="$WORK/grafana-v${GRAF_VER}"
if [ ! -x "$GRAF_DIR/bin/grafana" ] && [ ! -x "$GRAF_DIR/bin/grafana-server" ]; then
  echo "==> downloading Grafana ${GRAF_VER} (${ARCH})..."
  curl -fSL --retry 3 -o "$WORK/grafana.tgz" \
    "https://dl.grafana.com/oss/release/grafana-${GRAF_VER}.linux-${ARCH}.tar.gz"
  tar -xzf "$WORK/grafana.tgz" -C "$WORK" && rm -f "$WORK/grafana.tgz"
fi
# binary name differs across versions: prefer `grafana server`, fall back to grafana-server
GRAF_BIN="$GRAF_DIR/bin/grafana"; GRAF_ARGS=(server)
[ -x "$GRAF_BIN" ] || { GRAF_BIN="$GRAF_DIR/bin/grafana-server"; GRAF_ARGS=(); }

# --- generate binary-mode configs ---
CFG="$WORK/config"
mkdir -p "$CFG" "$CFG/provisioning/datasources" "$CFG/provisioning/dashboards" \
         "$WORK/prom-data" "$WORK/grafana-data" "$WORK/grafana-logs" "$WORK/grafana-plugins"

# Prometheus: scrape the LOCAL exporter (not host.docker.internal)
cat > "$CFG/prometheus.yml" <<EOF
global:
  scrape_interval: 15s
  scrape_timeout: 10s
$( [ -f "$ALERTS" ] && printf 'rule_files:\n  - %s\n' "$ALERTS" )
scrape_configs:
  - job_name: cgroup-exporter
    static_configs:
      - targets: ["127.0.0.1:9753"]
        labels:
          instance: local-binary
EOF

# Grafana datasource: point at the LOCAL prometheus (not the container name);
# uid pinned to "prometheus" so the committed dashboard resolves (see坑 3).
cat > "$CFG/provisioning/datasources/prometheus.yml" <<EOF
apiVersion: 1
datasources:
  - name: Prometheus
    type: prometheus
    uid: prometheus
    access: proxy
    url: http://127.0.0.1:9090
    isDefault: true
    editable: false
EOF

# Grafana dashboards provider: point at the committed dashboard JSON dir
cat > "$CFG/provisioning/dashboards/dashboards.yml" <<EOF
apiVersion: 1
providers:
  - name: cgroup
    orgId: 1
    folder: cgroup
    type: file
    options:
      path: $DASH_DIR
EOF

# --- start everything ---
pids=()
cleanup() {
  echo; echo "==> stopping..."
  for p in "${pids[@]:-}"; do kill "$p" 2>/dev/null || true; done
  wait 2>/dev/null || true
  echo "==> stopped."
}
trap cleanup INT TERM EXIT

alive() { kill -0 "$1" 2>/dev/null; }

# Launch a process, remember its pid, and fail loudly (with log tail) if it
# dies within ~1.5s — so a port clash / bad config never sits there silently.
start() {  # name logfile -- cmd...
  local name="$1" log="$2"; shift 2
  echo "==> starting $name  (log: $log)"
  "$@" >"$log" 2>&1 &
  local pid=$!; pids+=("$pid")
  sleep 1.5
  if ! alive "$pid"; then
    echo "!! $name 啟動失敗，log 尾巴："
    echo "------------------------------------------------------------"
    tail -n 25 "$log" 2>/dev/null
    echo "------------------------------------------------------------"
    echo "   常見原因：port 被佔（docker 棧還開著？ ss -ltn | grep -E ':(9090|3000|9753) ')"
    exit 1
  fi
}

start exporter   "$LOGS/exporter.log" \
  env CGROUP_EXPORTER_LISTEN=127.0.0.1:9753 python3 "$EXPORTER"

start prometheus "$LOGS/prometheus.log" \
  "$PROM_DIR/prometheus" \
  --config.file="$CFG/prometheus.yml" \
  --storage.tsdb.path="$WORK/prom-data" \
  --storage.tsdb.retention.time=15d \
  --web.listen-address=127.0.0.1:9090

start grafana    "$LOGS/grafana.log" \
  env GF_PATHS_DATA="$WORK/grafana-data" \
      GF_PATHS_LOGS="$WORK/grafana-logs" \
      GF_PATHS_PLUGINS="$WORK/grafana-plugins" \
      GF_PATHS_PROVISIONING="$CFG/provisioning" \
      GF_SERVER_HTTP_ADDR=127.0.0.1 \
      GF_SERVER_HTTP_PORT=3000 \
      GF_SECURITY_ADMIN_PASSWORD="$GRAFANA_ADMIN_PASSWORD" \
      GF_AUTH_ANONYMOUS_ENABLED=false \
      GF_USERS_ALLOW_SIGN_UP=false \
      "$GRAF_BIN" "${GRAF_ARGS[@]}" --homepath "$GRAF_DIR"

# --- wait for readiness + report ---
# --noproxy '*' so a corporate http_proxy/https_proxy env var doesn't send our
# 127.0.0.1 probes off to a proxy (a classic "service is up but readiness fails")
echo -n "==> waiting for stack to be ready (tail -f $LOGS/*.log to watch)"
ready=0
for _ in $(seq 1 60); do
  # if any process died during startup, stop waiting and show why
  for p in "${pids[@]}"; do
    if ! alive "$p"; then
      echo; echo "!! 有 process 在啟動中掛掉了，看 log："; tail -n 25 "$LOGS"/*.log; exit 1
    fi
  done
  if curl -sf --noproxy '*' --max-time 2 http://127.0.0.1:3000/api/health >/dev/null 2>&1 \
     && curl -sf --noproxy '*' --max-time 2 http://127.0.0.1:9090/-/ready  >/dev/null 2>&1; then
    ready=1; break
  fi
  echo -n "."; sleep 1
done
echo
[ "$ready" = 1 ] || echo "!! 等了 60s 還沒 ready，但 process 都還活著 — 多半是服務其實好了只是探測沒過（proxy？）。直接開瀏覽器試 http://127.0.0.1:3000 ，或看 $LOGS/*.log"

prom_target=$(curl -s --noproxy '*' --max-time 3 http://127.0.0.1:9090/api/v1/targets 2>/dev/null \
  | grep -o '"health":"[^"]*"' | head -1)
echo "----------------------------------------------------------------"
echo "  Grafana:     http://127.0.0.1:3000   (admin / $GRAFANA_ADMIN_PASSWORD)"
echo "               Dashboards -> cgroup -> cgroup overview"
echo "  Prometheus:  http://127.0.0.1:9090/targets   target ${prom_target:-?}"
echo "  Logs:        $LOGS/{exporter,prometheus,grafana}.log"
echo "  Data/cache:  $WORK"
echo "  Stop:        Ctrl-C (stops all three)"
echo "----------------------------------------------------------------"

wait
