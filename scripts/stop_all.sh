#!/usr/bin/env bash
# stop_all.sh — stop all data stack processes on spark-22b6
# Usage: ./scripts/stop_all.sh [-v|--verbose] [spark_nats | spark_influx | all]
#   Defaults to spark_nats.
#
# Uses systemctl to stop managed services, then WebSocket stop_all to the
# manager for a clean shutdown of components.  Falls back to kill if needed.

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
STACK="${STACK:-spark_nats}"

VENV="$(pwd)/.venv/bin/python"
MANAGER_URL="ws://localhost:8762"

G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; B='\033[0;34d'; N='\033[0m'
ok()   { echo -e "${G}  [ok]${N}   $*"; }
info() { echo -e "  -->    $*"; }
warn() { echo -e "${Y}  [warn]${N} $*"; }

kill_proc() {
    local pattern="$1"
    local pids
    pids=$(pgrep -f "$pattern" 2>/dev/null || true)
    if [[ -z "$pids" ]]; then
        [[ $VERBOSE -eq 1 ]] && echo "         (not running: $pattern)" || true
        return 0
    fi
    for pid in $pids; do
        local cmd
        cmd=$(ps -p "$pid" -o cmd= 2>/dev/null || echo "?")
        warn "killing PID $pid  ($cmd)"
        kill "$pid" 2>/dev/null || true
    done
    sleep 1
    for pid in $pids; do
        if kill -0 "$pid" 2>/dev/null; then
            warn "PID $pid still alive — SIGKILL"
            kill -9 "$pid" 2>/dev/null || true
        else
            [[ $VERBOSE -eq 1 ]] && ok "PID $pid gone" || true
        fi
    done
}

if [[ "$STACK" == "spark_nats" || "$STACK" == "all" ]]; then
    echo -e "\n==> Stopping spark_nats"

    # 1. Tell manager to stop all components cleanly via WebSocket
    if ss -tlnp "sport = :8762" 2>/dev/null | grep -q ":8762"; then
        info "sending stop_all to manager"
        VFLAG=$([[ $VERBOSE -eq 1 ]] && echo "-v" || echo "")
        "$VENV" scripts/ws_cmd.py $VFLAG --timeout 30 "$MANAGER_URL" stop_all && \
            ok "manager stopped all services" || \
            warn "manager stop_all incomplete — will stop via systemctl anyway"
    else
        warn "manager not running on :8762"
    fi

    # 2. Stop the systemd services (prevents auto-restart)
    info "stopping systemd services"
    sudo systemctl stop data-server-stress data-server-spark 2>/dev/null && \
        ok "systemd services stopped" || warn "systemctl stop had errors (may not be installed)"

    # 3. Kill any survivors (e.g. processes started outside systemd)
    if [[ $VERBOSE -eq 1 ]]; then
        info "checking for survivors..."
        SURVIVORS=$(pgrep -f "minio|nats-server|bridge\.py|writer\.py|aws/data_store/server|subscriber/api/server|stress_runner" 2>/dev/null || true)
        if [[ -n "$SURVIVORS" ]]; then
            warn "survivors found — killing:"
            for pid in $SURVIVORS; do
                kill_proc "$(ps -p $pid -o comm= 2>/dev/null || echo $pid)"
            done
        else
            ok "all spark_nats processes gone"
        fi
    fi

    echo "==> spark_nats stopped"
fi

if [[ "$STACK" == "spark_influx" || "$STACK" == "all" ]]; then
    echo -e "\n==> Stopping spark_influx"
    for svc in telegraf influxdb flashmq; do
        if systemctl is-active --quiet "$svc" 2>/dev/null; then
            info "stopping $svc"
            sudo systemctl stop "$svc"
            ok "$svc stopped"
        else
            [[ $VERBOSE -eq 1 ]] && echo "         (not running: $svc)" || true
        fi
    done
    echo "==> spark_influx stopped"
fi
