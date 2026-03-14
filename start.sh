#!/usr/bin/env bash
# start.sh — bring up the full data server (both architectures)
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

GREEN='\033[0;32m'; BLUE='\033[0;34m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; GREY='\033[0;90m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[ OK ]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()     { echo -e "${RED}[FAIL]${NC}  $*"; exit 1; }

# ── Sanity checks ─────────────────────────────────────────────────
[ -f .venv/bin/python ] || die "Run setup first: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"

# ── Helper: is a manager already running on a given port? ─────────
manager_running() { ss -tlnp 2>/dev/null | grep -q ":$1 "; }

# ── Helper: start a background manager, log to /tmp ───────────────
start_manager() {
    local label="$1" config="$2" port="$3" logfile="/tmp/manager_${port}.log"
    if manager_running "$port"; then
        warn "$label already running on port $port"
    else
        .venv/bin/python manager/manager.py --config "$config" > "$logfile" 2>&1 &
        sleep 1
        if manager_running "$port"; then
            success "$label started on port $port  (log: $logfile)"
        else
            die "$label failed to start — check $logfile"
        fi
    fi
}

echo ""
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLUE}  DATA SERVER — startup${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

# ── 1. orig_aws systemd services ──────────────────────────────────
info "Checking orig_aws systemd services..."
for svc in flashmq influxdb telegraf; do
    if systemctl is-active --quiet "$svc" 2>/dev/null; then
        success "$svc is running"
    else
        warn "$svc is not running — attempting start (needs sudo)..."
        sudo systemctl start "$svc" && success "$svc started" || warn "Could not start $svc"
    fi
done

echo ""

# ── 2. orig_aws manager (port 8760) — generator + log tails ──────
start_manager "orig_aws manager" "orig_aws/manager_config.yaml" 8760

# ── 3. New NATS stack manager (port 8761) ─────────────────────────
start_manager "NATS stack manager" "manager/config.yaml" 8761

# ── Summary ───────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  All systems up!${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "  ${YELLOW}orig_aws stack${NC}  (FlashMQ → Telegraf → InfluxDB2)"
echo -e "  ${GREY}  FlashMQ    MQTT broker      port 1883${NC}"
echo -e "  ${GREY}  Telegraf   MQTT → InfluxDB2 (systemd)${NC}"
echo -e "  ${GREY}  InfluxDB2  http://localhost:8086  (admin / adminpassword123)${NC}"
echo -e "  ${GREY}  Manager    ws://localhost:8760  → dashboard.html (port 8760)${NC}"
echo -e "  ${GREY}  Monitor    html/orig_aws.html${NC}"
echo ""
echo -e "  ${YELLOW}New NATS stack${NC}  (Mosquitto → NATS → Parquet/S3)"
echo -e "  ${GREY}  Mosquitto  MQTT broker      port 1884${NC}"
echo -e "  ${GREY}  NATS       JetStream        port 4222${NC}"
echo -e "  ${GREY}  MinIO      S3-compatible    port 9000  (console: 9011)${NC}"
echo -e "  ${GREY}  Manager    ws://localhost:8761  → dashboard.html (port 8761)${NC}"
echo -e "  ${GREY}  Generator  ws://localhost:8765  → generator.html${NC}"
echo -e "  ${GREY}  AWS Store  ws://localhost:8766  → aws.html${NC}"
echo ""
echo -e "  ${YELLOW}HTML pages${NC}  (open in browser)"
echo -e "  ${GREY}  html/dashboard.html   — process manager & logs${NC}"
echo -e "  ${GREY}  html/orig_aws.html    — InfluxDB2 battery monitor (SVG charts)${NC}"
echo -e "  ${GREY}  html/generator.html   — live generator data${NC}"
echo -e "  ${GREY}  html/aws.html         — S3/MinIO Parquet store${NC}"
echo -e "  ${GREY}  html/subscriber.html  — query interface${NC}"
echo -e "  ${GREY}  html/costs.html       — AWS cost comparison${NC}"
echo -e "  ${GREY}  html/architecture.html — live pipeline diagrams (both stacks)${NC}"
echo ""
