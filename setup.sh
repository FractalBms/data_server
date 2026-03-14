#!/usr/bin/env bash
# Data Server — setup script
# Tested on: Ubuntu 22.04 / 24.04, x86_64 and ARM64 (NVIDIA DGX SPARK)
#
# What this does:
#   1. Detects CPU architecture (x86_64 or aarch64)
#   2. Downloads NATS server binary → manager/
#   3. Downloads MinIO binary       → aws/data_store/
#   4. Installs Mosquitto MQTT broker via apt
#   5. Installs Python dependencies for all components
#   6. Creates required data directories
#
# Usage:
#   chmod +x setup.sh && ./setup.sh

set -euo pipefail

# ── Colours ────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; BLUE='\033[0;34m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Architecture detection ─────────────────────────────────────────────────
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64)  NATS_ARCH="amd64"; MINIO_ARCH="amd64" ;;
  aarch64) NATS_ARCH="arm64"; MINIO_ARCH="arm64" ;;
  arm64)   NATS_ARCH="arm64"; MINIO_ARCH="arm64" ;;
  *)       error "Unsupported architecture: $ARCH" ;;
esac

info "Detected architecture: $ARCH  →  NATS=$NATS_ARCH  MinIO=$MINIO_ARCH"

OS="$(uname -s)"
[ "$OS" != "Linux" ] && error "This script is for Linux only (detected: $OS)"

# ── Versions ───────────────────────────────────────────────────────────────
NATS_VERSION="2.10.22"

# ── 1. System packages ─────────────────────────────────────────────────────
info "Installing system packages (mosquitto, python3-pip, unzip, curl) ..."
sudo apt-get update -qq
sudo apt-get install -y mosquitto mosquitto-clients python3-pip python3-venv unzip curl
success "System packages installed"

# ── 2. NATS server ─────────────────────────────────────────────────────────
NATS_TARGET="manager/nats-server"
if [ -f "$NATS_TARGET" ]; then
  warn "NATS server already exists at $NATS_TARGET — skipping download"
else
  NATS_ZIP="nats-server-v${NATS_VERSION}-linux-${NATS_ARCH}.zip"
  NATS_URL="https://github.com/nats-io/nats-server/releases/download/v${NATS_VERSION}/${NATS_ZIP}"
  info "Downloading NATS server v${NATS_VERSION} (${NATS_ARCH}) ..."
  curl -fsSL "$NATS_URL" -o "/tmp/${NATS_ZIP}"
  unzip -o "/tmp/${NATS_ZIP}" "nats-server-v${NATS_VERSION}-linux-${NATS_ARCH}/nats-server" -d /tmp/
  mv "/tmp/nats-server-v${NATS_VERSION}-linux-${NATS_ARCH}/nats-server" "$NATS_TARGET"
  chmod +x "$NATS_TARGET"
  rm -f "/tmp/${NATS_ZIP}"
  success "NATS server installed → $NATS_TARGET"
fi

# Verify
"$NATS_TARGET" --version | head -1 | xargs -I{} info "  {}"

# ── 3. MinIO ───────────────────────────────────────────────────────────────
MINIO_TARGET="aws/data_store/minio"
if [ -f "$MINIO_TARGET" ]; then
  warn "MinIO already exists at $MINIO_TARGET — skipping download"
else
  MINIO_URL="https://dl.min.io/server/minio/release/linux-${MINIO_ARCH}/minio"
  info "Downloading MinIO (${MINIO_ARCH}) ..."
  curl -fsSL "$MINIO_URL" -o "$MINIO_TARGET"
  chmod +x "$MINIO_TARGET"
  success "MinIO installed → $MINIO_TARGET"
fi

"$MINIO_TARGET" --version | head -1 | xargs -I{} info "  {}"

# ── 4. Python virtual environment ──────────────────────────────────────────
VENV_DIR="$SCRIPT_DIR/.venv"
if [ ! -d "$VENV_DIR" ]; then
  info "Creating Python virtual environment at .venv ..."
  python3 -m venv "$VENV_DIR"
  success "Virtual environment created"
else
  warn "Virtual environment already exists at .venv — skipping creation"
fi

source "$VENV_DIR/bin/activate"
pip install --upgrade pip -q

# ── 5. Python dependencies ─────────────────────────────────────────────────
info "Installing Python dependencies ..."
pip install -r requirements.txt -q
success "Python dependencies installed"

# ── 6. Data directories ─────────────────────────────────────────────────────
info "Creating data directories ..."
mkdir -p manager/nats-data
mkdir -p aws/data_store/minio-data
success "Data directories ready"

# ── 7. Update manager config to use local nats-server binary ──────────────
# The manager config references "nats-server" — update to use the local binary
# if nats-server is not on PATH
if ! command -v nats-server &>/dev/null; then
  warn "nats-server not on PATH — manager will use ./manager/nats-server"
  # The manager config already sets cwd: "manager" for the nats component
  # and cmd: ["nats-server", ...] — update to use relative path
  sed -i 's|cmd: \["nats-server",|cmd: ["./nats-server",|g' manager/config.yaml
  info "Updated manager/config.yaml to use ./nats-server"
fi

# ── Summary ────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  Setup complete!${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo "  To activate the Python environment:"
echo "    source .venv/bin/activate"
echo ""
echo "  To start everything:"
echo "    source .venv/bin/activate"
echo "    cd manager && python manager.py"
echo "    # then open http://localhost:8080/dashboard.html"
echo ""
echo "  Architecture: $ARCH"
echo "  NATS:  $NATS_TARGET  (v${NATS_VERSION})"
echo "  MinIO: $MINIO_TARGET"
echo ""
