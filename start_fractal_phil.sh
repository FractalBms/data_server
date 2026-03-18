#!/usr/bin/env bash
# Start the full fractal-phil simulation stack.
# Both FlashMQ instances, both writer.cpp instances, rsync-sim loop,
# Telegraf, and the subscriber API all run on this one machine.
#
# Run from the data_server root:
#   bash start_fractal_phil.sh [--no-gap-writer] [--no-telegraf]

set -euo pipefail
cd "$(dirname "$0")"

GAP_WRITER=true
TELEGRAF=true
for arg in "$@"; do
  case "$arg" in
    --no-gap-writer) GAP_WRITER=false ;;
    --no-telegraf)   TELEGRAF=false ;;
  esac
done

WRITER_BIN="source/parquet_writer_cpp/parquet_writer"
if [[ ! -x "$WRITER_BIN" ]]; then
  echo "ERROR: $WRITER_BIN not found — build it first (cd source/parquet_writer_cpp && make)"
  exit 1
fi

echo "=== Starting FlashMQ host :1883 ==="
flashmq --config /etc/flashmq/flashmq.conf &
sleep 0.5

echo "=== Starting FlashMQ aws-sim :1884 ==="
flashmq --config /etc/flashmq/flashmq-aws-sim.conf &
sleep 0.5

echo "=== Starting host writer.cpp → /srv/data/parquet ==="
(cd source/parquet_writer_cpp && nohup ./parquet_writer --config config.yaml \
  > /tmp/writer-host.log 2>&1) &

echo "=== Starting rsync-sim loop (5 s) ==="
(while true; do
  rsync -a --delete /srv/data/parquet/ /srv/data/parquet-aws-sim/
  sleep 5
done) > /tmp/rsync-sim.log 2>&1 &

if $GAP_WRITER; then
  echo "=== Starting gap writer → /srv/data/parquet-aws-sim/current_state.parquet ==="
  (cd source/parquet_writer_cpp && nohup ./parquet_writer --config ../../aws/gap_fill/config.yaml \
    > /tmp/writer-gap.log 2>&1) &
fi

if $TELEGRAF; then
  echo "=== Starting Telegraf → InfluxDB2 ==="
  telegraf --config source/telegraf/telegraf-aws-sim.conf > /tmp/telegraf.log 2>&1 &
fi

echo "=== Starting stress_runner ==="
(cd source/stress_runner && nohup python stress_runner.py --config config.yaml \
  > /tmp/stress_runner.log 2>&1) &

echo "=== Starting subscriber_api ==="
(cd subscriber/api && nohup python server.py --config config.fractal.yaml \
  > /tmp/subscriber-api.log 2>&1) &

echo ""
echo "Stack started. Logs:"
echo "  /tmp/writer-host.log      host writer.cpp"
echo "  /tmp/writer-gap.log       gap writer (if enabled)"
echo "  /tmp/rsync-sim.log        rsync-sim"
echo "  /tmp/telegraf.log         telegraf"
echo "  /tmp/stress_runner.log    stress_runner"
echo "  /tmp/subscriber-api.log   subscriber API  ws://localhost:8767"
