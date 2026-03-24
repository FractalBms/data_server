#!/usr/bin/env bash
# gx10-evelyn-stop.sh — stop real_writer + stress_real_pub on gx10-d94c

pkill -f "real_writer.*config.gx10-evelyn"   2>/dev/null && echo "stopped real_writer"    || echo "real_writer not running"
pkill -f "stress_real_pub.*8769"             2>/dev/null && echo "stopped stress_real_pub" || echo "stress_real_pub not running"
