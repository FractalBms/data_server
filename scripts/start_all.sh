#!/usr/bin/env bash
# start_all.sh — start the full data stack on spark-22b6
# Usage: ./scripts/start_all.sh [-v|--verbose] spark_nats | spark_influx
#
# spark_nats  — starts data-server-spark + data-server-stress via systemctl.
#               Streams manager log output while services come up.
# spark_influx — ensures flashmq/influxdb/telegraf are running.
#
# -v / --verbose  stream live journal output while starting

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

VERBOSE=0
STACK=""
for arg in "$@"; do
    case "$arg" in
        -v|--verbose) VERBOSE=1 ;;
        *) STACK="$arg" ;;
    esac
done

if [[ -z "$STACK" ]]; then
    echo "Usage: $0 [-v] spark_nats | spark_influx"
    exit 1
fi

VENV="$(pwd)/.venv/bin/python"
MANAGER_URL="ws://localhost:8762"

G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; B='\033[0;34m'; N='\033[0m'
ok()   { echo -e "${G}  [ok]${N}   $*"; }
info() { echo -e "${B}  -->  ${N}  $*"; }
warn() { echo -e "${Y}  [warn]${N} $*"; }
fail() { echo -e "${R}  [FAIL]${N} $*"; }

wait_port() {
    local port="$1" label="$2"
    info "waiting for $label on :$port ..."
    for i in $(seq 1 40); do
        ss -tlnp "sport = :${port}" 2>/dev/null | grep -q ":${port}" && {
            ok "$label up on :$port"
            return 0
        }
        sleep 0.5
    done
    fail "$label did not come up on :$port after 20s"
    [[ $VERBOSE -eq 1 ]] && journalctl -u data-server-spark --no-pager -n 30
    return 1
}

# ── spark_nats ─────────────────────────────────────────────────────────────
if [[ "$STACK" == "spark_nats" ]]; then
    echo -e "\n${B}==> spark_nats${N}"

    # Stop first so systemd restarts cleanly
    info "stopping existing services"
    sudo systemctl stop data-server-spark data-server-stress 2>/dev/null || true
    sleep 1

    info "starting data-server-spark (manager + auto_start)"
    sudo systemctl start data-server-spark
    wait_port 8762 "Manager"

    info "starting data-server-stress (stress runner)"
    sudo systemctl start data-server-stress

    # Stream status from manager
    info "waiting for all services to reach running state..."
    VFLAG=$([[ $VERBOSE -eq 1 ]] && echo "-v" || echo "")
    "$VENV" scripts/ws_cmd.py $VFLAG --timeout 60 "$MANAGER_URL" get_status || true

    echo ""
    echo -e "${G}==> spark_nats running${N}"
    if [[ $VERBOSE -eq 1 ]]; then
        echo ""
        echo "    systemctl status:"
        systemctl is-active data-server-spark  && ok "data-server-spark active" || warn "data-server-spark not active"
        systemctl is-active data-server-stress && ok "data-server-stress active" || warn "data-server-stress not active"
        echo ""
        echo "    Process table:"
        ps aux | grep -E "minio|nats-server|bridge\.py|writer\.py|aws/data_store/server|subscriber/api/server|stress_runner" \
               | grep -v grep \
               | awk '{printf "    PID %-8s CPU %-5s  %s %s\n", $2, $3"%", $11, $12}' || true
    fi

# ── spark_influx ────────────────────────────────────────────────────────────
elif [[ "$STACK" == "spark_influx" ]]; then
    echo -e "\n${B}==> spark_influx${N}"
    for svc in flashmq influxdb telegraf; do
        if systemctl is-active --quiet "$svc"; then
            ok "$svc already running"
        else
            info "starting $svc"
            sudo systemctl start "$svc"
            ok "$svc started"
        fi
    done
    echo -e "\n${G}==> spark_influx running${N}"

else
    fail "Unknown stack: $STACK"
    echo "Usage: $0 [-v] spark_nats | spark_influx"
    exit 1
fi
