#!/bin/sh
set -e
cp /etc/writer/config.yaml /tmp/config.yaml
sed -i "s/MQTT_HOST_PLACEHOLDER/${MQTT_HOST}/g" /tmp/config.yaml
sed -i "s/SITE_ID_PLACEHOLDER/${SITE_ID}/g"     /tmp/config.yaml
echo '=== resolved config ==='
grep -E 'host:|site_id:' /tmp/config.yaml
echo '======================='
exec /usr/local/bin/parquet_writer --config /tmp/config.yaml
