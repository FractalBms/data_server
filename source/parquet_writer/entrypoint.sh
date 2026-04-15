#!/bin/sh
set -e
cp /etc/writer/config.yaml /tmp/config.yaml
# MQTT_HOST secret takes priority; fall back to compiled-in DEFAULT_MQTT_HOST
RESOLVED_HOST="${MQTT_HOST:-${DEFAULT_MQTT_HOST:-localhost}}"
sed -i "s/MQTT_HOST_PLACEHOLDER/${RESOLVED_HOST}/g" /tmp/config.yaml
echo '=== resolved config ==='
grep -E 'host:|site_id:' /tmp/config.yaml
echo '======================='
exec /usr/local/bin/parquet_writer --config /tmp/config.yaml
