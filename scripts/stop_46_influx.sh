#!/usr/bin/env bash
# stop_46_influx.sh — stop the .46 influx/telegraf/generator stack

pkill -f "influxd.*influx-46"    2>/dev/null && echo "stopped influxd"    || echo "influxd not running"
pkill -f "telegraf.*telegraf-46" 2>/dev/null && echo "stopped telegraf"   || echo "telegraf not running"
pkill -f "stress_real_pub.*1883" 2>/dev/null && echo "stopped generator"  || echo "generator not running"

echo ""
echo "To wipe data:  rm -rf ~/data/influx-46/"
