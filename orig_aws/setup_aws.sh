#!/usr/bin/env bash
# orig_aws/setup_aws.sh — Native install on AWS EC2 (Ubuntu 22.04 / 24.04)
#
# Installs: FlashMQ, Telegraf, InfluxDB2
# Configures systemd services and copies configs.
#
# Usage: sudo ./setup_aws.sh
#
# NOTE: For production, also:
#   1. Mount EFS at /var/lib/influxdb2 BEFORE running this script
#   2. Set a real InfluxDB admin password and token
#   3. Configure TLS on FlashMQ (port 8883)

set -euo pipefail

GREEN='\033[0;32m'; BLUE='\033[0;34m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

[ "$(id -u)" -ne 0 ] && error "Run as root: sudo ./setup_aws.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── 1. System deps ─────────────────────────────────────────────────────────
info "Installing system packages..."
apt-get update -qq
apt-get install -y -qq curl gnupg lsb-release ca-certificates apt-transport-https
success "System packages ready"

# ── 2. FlashMQ ─────────────────────────────────────────────────────────────
info "Installing FlashMQ..."
if ! command -v flashmq &>/dev/null; then
    curl -fsSL https://www.flashmq.org/static/flashmq-repo-setup.sh | bash
    apt-get update -qq
    apt-get install -y -qq flashmq
    success "FlashMQ installed"
else
    warn "FlashMQ already installed — skipping"
fi

mkdir -p /etc/flashmq /var/log/flashmq
cp "$SCRIPT_DIR/flashmq/flashmq.conf" /etc/flashmq/flashmq.conf
chown -R flashmq:flashmq /var/log/flashmq 2>/dev/null || true

systemctl enable flashmq
systemctl restart flashmq
success "FlashMQ service started"

# ── 3. InfluxDB2 ───────────────────────────────────────────────────────────
info "Installing InfluxDB2..."
if ! command -v influxd &>/dev/null; then
    curl -fsSL https://repos.influxdata.com/influxdata-archive_compat.key \
        | gpg --dearmor -o /usr/share/keyrings/influxdata-archive.gpg
    echo "deb [signed-by=/usr/share/keyrings/influxdata-archive.gpg] \
https://repos.influxdata.com/debian stable main" \
        > /etc/apt/sources.list.d/influxdata.list
    apt-get update -qq
    apt-get install -y -qq influxdb2 influxdb2-cli
    success "InfluxDB2 installed"
else
    warn "InfluxDB2 already installed — skipping"
fi

# ── EFS mount check ───────────────────────────────────────────────────────
if mountpoint -q /var/lib/influxdb2; then
    success "EFS already mounted at /var/lib/influxdb2"
else
    warn "EFS NOT mounted at /var/lib/influxdb2 — using local disk"
    warn "For production, mount EFS before running this script:"
    warn "  Add to /etc/fstab:"
    warn "  fs-XXXXXXXX.efs.us-east-1.amazonaws.com:/ /var/lib/influxdb2 efs defaults,_netdev,tls 0 0"
fi

systemctl enable influxdb
systemctl restart influxdb
sleep 5  # wait for influxd to start

# ── Initial setup ─────────────────────────────────────────────────────────
if ! influx org list &>/dev/null 2>&1; then
    info "Running InfluxDB2 initial setup..."
    influx setup \
        --username admin \
        --password adminpassword123 \
        --org battery-org \
        --bucket battery-data \
        --retention 30d \
        --token battery-token-secret \
        --force
    success "InfluxDB2 initialized (org=battery-org, bucket=battery-data)"
else
    warn "InfluxDB2 already configured — skipping setup"
fi

# ── 4. Telegraf ────────────────────────────────────────────────────────────
info "Installing Telegraf..."
if ! command -v telegraf &>/dev/null; then
    apt-get install -y -qq telegraf
    success "Telegraf installed"
else
    warn "Telegraf already installed — skipping"
fi

# Patch telegraf.conf for native (localhost) instead of docker hostnames
sed 's|tcp://flashmq:1883|tcp://localhost:1883|g; s|http://influxdb:8086|http://localhost:8086|g' \
    "$SCRIPT_DIR/telegraf/telegraf.conf" > /etc/telegraf/telegraf.conf

systemctl enable telegraf
systemctl restart telegraf
success "Telegraf service started"

# ── 5. Python generator ────────────────────────────────────────────────────
info "Setting up Python generator..."
apt-get install -y -qq python3-pip python3-venv
python3 -m venv "$SCRIPT_DIR/.venv"
"$SCRIPT_DIR/.venv/bin/pip" install -q -r "$SCRIPT_DIR/requirements.txt"
success "Python environment ready at $SCRIPT_DIR/.venv"

# ── Summary ────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  Setup complete!${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo "  FlashMQ:   systemctl status flashmq    (MQTT port 1883)"
echo "  InfluxDB2: http://$(hostname -I | awk '{print $1}'):8086  (admin / adminpassword123)"
echo "  Telegraf:  systemctl status telegraf"
echo ""
echo "  Start generator:"
echo "    source $SCRIPT_DIR/.venv/bin/activate"
echo "    python $SCRIPT_DIR/source/generator.py --config $SCRIPT_DIR/source/config.yaml"
echo ""
