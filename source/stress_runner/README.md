# Stress Runner

Simulates multiple projects and sites publishing battery telemetry data 24/7
over MQTT.  Used to validate throughput, partitioning, and stability of the
full data pipeline under sustained load.

---

## Purpose

The stress runner replaces manual testing with a long-running synthetic load.
Each configured (project, site) pair runs as an independent asyncio Task that
continuously simulates every cell in that site and publishes one MQTT message
per cell per `sample_interval_seconds`.  At 1 Hz with 3 sites × 4 racks × 8
modules × 12 cells the default `config.spark.yaml` produces **1,152 MQTT
messages per second** sustained.

---

## How cell count maps to message rate

```
messages/sec = projects × sites × racks × modules_per_rack × cells_per_module
             ÷ sample_interval_seconds
```

For the default spark config:

```
1 project × 3 sites × 4 racks × 8 modules × 12 cells / 1 s = 1,152 msg/s
```

Scale up by adding projects, sites, or reducing `sample_interval_seconds`.

---

## Config reference

### `mqtt`

| Key | Default | Description |
|-----|---------|-------------|
| `host` | `localhost` | MQTT broker hostname |
| `port` | `1883` | MQTT broker port |
| `client_id` | `stress-runner` | MQTT client identifier |
| `username` | `""` | Optional broker username |
| `password` | `""` | Optional broker password |

### `websocket`

| Key | Default | Description |
|-----|---------|-------------|
| `host` | `0.0.0.0` | WebSocket bind address |
| `port` | `8769` | WebSocket port |

### Top-level keys

| Key | Description |
|-----|-------------|
| `topic_prefix` | MQTT topic prefix, e.g. `batteries` |
| `sample_interval_seconds` | Seconds between publishes per cell (float OK) |

### `cell_data`

Each entry names a measurement field published in every payload.  Keys:

| Key | Description |
|-----|-------------|
| `min` | Minimum clamp value |
| `max` | Maximum clamp value |
| `unit` | Unit string stored in the payload |
| `nominal` | Starting value (random ±5 % of range applied at startup) |

Default measurements: `voltage`, `current`, `temperature`, `soc`,
`internal_resistance`.

### `projects`

A list of project objects:

```yaml
projects:
  - project_id: 0
    sites:
      - site_id: 0
        racks: 4
        modules_per_rack: 8
        cells_per_module: 12
```

Each `site` entry fully specifies the rack/module/cell hierarchy for that site.

---

## MQTT topic and payload

**Topic:**
```
{topic_prefix}/project={project_id}/site={site_id}/rack={rack_id}/module={module_id}/cell={cell_id}
```

**Payload (JSON):**
```json
{
  "timestamp": 1700000000.0,
  "project_id": 0,
  "site_id": 0,
  "rack_id": 1,
  "module_id": 2,
  "cell_id": 3,
  "voltage": 3.74,
  "current": 0.12,
  "temperature": 24.8,
  "soc": 79.1,
  "internal_resistance": 0.0102
}
```

The `project_id` field in the payload is read by the parquet writer to
partition output into `project={project_id}/site={site_id}/...` directories
in S3.  See [parquet writer config](../parquet_writer/config.yaml) for
partition layout details.

---

## WebSocket stats API (port 8769)

Connect to `ws://localhost:8769`.

On connect, and then every second, the server sends:

```json
{
  "type": "stats",
  "total_published": 12345,
  "mps": 1152,
  "active_tasks": 3,
  "projects": [
    {
      "project_id": 0,
      "sites": [
        {"site_id": 0, "cells": 384, "published": 4100},
        {"site_id": 1, "cells": 384, "published": 4050},
        {"site_id": 2, "cells": 384, "published": 4000}
      ]
    }
  ]
}
```

Send `{"type": "get_status"}` at any time to get an immediate reply with the
same structure.

---

## How to run

```bash
cd source/stress_runner
pip install paho-mqtt websockets pyyaml
python stress_runner.py --config config.spark.yaml
```

The runner logs one line per task on startup:

```
2024-01-01 00:00:00 INFO Project 0 site 0: 384 cells, interval 1.0s
```

Stop with Ctrl-C (SIGINT) or send SIGTERM.  All tasks drain their current
publish batch and exit cleanly before the MQTT connection closes.

---

## Keeping it alive with systemd

Create `/etc/systemd/system/data-server-stress.service`:

```ini
[Unit]
Description=Battery telemetry stress runner
After=network.target mosquitto.service

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/data_server/source/stress_runner
ExecStart=/usr/bin/python3 stress_runner.py --config config.spark.yaml
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now data-server-stress
sudo journalctl -u data-server-stress -f
```

---

## How `project_id` feeds parquet partitions

The parquet writer reads `project_id` directly from each message payload.
When the stress runner publishes with `project_id: 0` the writer stores rows
under `project=0/site=0/...`.  Add a second project (`project_id: 1`) to the
config and the writer automatically creates a separate `project=1/...`
partition tree in S3 — no writer config changes required.
