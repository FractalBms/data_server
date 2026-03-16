#!/usr/bin/env bash
# stop_all.sh — stop all data stack processes on spark-22b6
# Usage: ./scripts/stop_all.sh [-v|--verbose] [spark_nats | spark_influx | all]
#   Defaults to spark_nats.
#
# Preferred path: sends stop_all to the manager via WebSocket so the
# dashboard stays in sync.  Falls back to direct kill if manager is down.
#
# -v / --verbose  show each PID killed and confirm it's gone

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

G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; B='\033[0;34m'; N='\033[0m'
ok()   { echo -e "${G}  [ok]${N}   $*"; }
info() { echo -e "${B}  -->  ${N}  $*"; }
warn() { echo -e "${Y}  [warn]${N} $*"; }

manager_running() {
    ss -tlnp "sport = :8762" 2>/dev/null | grep -q ":8762"
}

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
        warn "stopping PID $pid  ($cmd)"
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

verbose_flag() { [[ $VERBOSE -eq 1 ]] && echo "-v" || echo ""; }

if [[ "$STACK" == "spark_nats" || "$STACK" == "all" ]]; then
    echo -e "\n${B}==> Stopping spark_nats${N}"

    if manager_running; then
        info "manager is up — sending stop_all via WebSocket"
        VFLAG=$(verbose_flag)
        if "$VENV" scripts/ws_cmd.py $VFLAG --timeout 30 "$MANAGER_URL" stop_all; then
            ok "manager stopped all services"
        else
            warn "manager stop_all did not complete cleanly — falling back to kill"
            kill_proc "stress_runner.py"
            kill_proc "subscriber/api/server.py"
            kill_proc "aws/data_store/server.py"
            kill_proc "source/parquet_writer/writer.py"
            kill_proc "source/nats_bridge/bridge.py"
            kill_proc "nats-server"
            kill_proc "minio server"
        fi
    else
        warn "manager not running — killing processes directly"
        kill_proc "stress_runner.py"
        kill_proc "subscriber/api/server.py"
        kill_proc "aws/data_store/server.py"
        kill_proc "source/parquet_writer/writer.py"
        kill_proc "source/nats_bridge/bridge.py"
        kill_proc "nats-server"
        kill_proc "minio server"
    fi

    if [[ $VERBOSE -eq 1 ]]; then
        echo ""
        echo "    Survivors check:"
        SURVIVORS=$(pgrep -f "minio|nats-server|bridge\.py|writer\.py|aws/data_store/server|subscriber/api/server|stress_runner" 2>/dev/null || true)
        if [[ -z "$SURVIVORS" ]]; then
            ok "all spark_nats processes gone"
        else
            for pid in $SURVIVORS; do
                warn "still running: PID $pid  $(ps -p $pid -o cmd= 2>/dev/null || echo '?')"
            done
        fi
    fi

    echo -e "${G}==> spark_nats stopped${N}"
fi

if [[ "$STACK" == "spark_influx" || "$STACK" == "all" ]]; then
    echo -e "\n${B}==> Stopping spark_influx${N}"
    for svc in telegraf influxdb flashmq; do
        if systemctl is-active --quiet "$svc" 2>/dev/null; then
            info "stopping $svc"
            sudo systemctl stop "$svc"
            ok "$svc stopped"
        else
            [[ $VERBOSE -eq 1 ]] && echo "         (not running: $svc)" || true
        fi
    done
    echo -e "${G}==> spark_influx stopped${N}"
fi
