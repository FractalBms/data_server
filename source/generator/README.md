# Generator

Publishes simulated battery cell telemetry to an MQTT broker and exposes a WebSocket control interface for the browser UI.

## What it does

- Builds a virtual battery pack from configurable topology (sites × racks × modules × cells).
- Simulates five measurements per cell (`voltage`, `current`, `temperature`, `soc`, `internal_resistance`) using a bounded random walk.
- Publishes MQTT messages at a fixed interval (default: 1 per second per cell).
- Exposes a WebSocket server so browsers can start/stop generation and receive live sample previews.

Default topology: **2 × 3 × 4 × 6 = 144 cells** → 144 MQTT messages/second in `per_cell` mode.

---

## Quick Start

```bash
cd source/generator
pip install paho-mqtt websockets pyyaml
python generator.py
# or specify a config file
python generator.py --config config.yaml
```

---

## Config Reference

File: `source/generator/config.yaml`

```yaml
broker:
  host: localhost
  port: 1884          # FlashMQ MQTT port
  username: ""
  password: ""
  client_id: battery-data-generator

websocket:
  host: 0.0.0.0
  port: 8765

# Pack topology
sites: 2
racks_per_site: 3
modules_per_rack: 4
cells_per_module: 6

sample_interval_seconds: 1

# per_cell       — one topic per cell, all measurements in one JSON payload
# per_cell_item  — one topic per measurement per cell
topic_mode: per_cell

topic_prefix: batteries

cell_data:
  voltage:
    min: 3.0
    max: 4.2
    unit: V
    nominal: 3.7
  current:
    min: -50.0
    max: 50.0
    unit: A
    nominal: 0.0
  temperature:
    min: 20.0
    max: 45.0
    unit: C
    nominal: 25.0
  soc:
    min: 0.0
    max: 100.0
    unit: percent
    nominal: 80.0
  internal_resistance:
    min: 0.001
    max: 0.05
    unit: ohm
    nominal: 0.01
```

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `broker.host` | string | `localhost` | FlashMQ hostname or IP |
| `broker.port` | int | `1884` | MQTT port |
| `sites` | int | `2` | Number of sites |
| `racks_per_site` | int | `3` | Racks per site |
| `modules_per_rack` | int | `4` | Modules per rack |
| `cells_per_module` | int | `6` | Cells per module |
| `sample_interval_seconds` | float | `1` | Seconds between full-pack publish cycles |
| `topic_mode` | string | `per_cell` | `per_cell` or `per_cell_item` |
| `topic_prefix` | string | `batteries` | MQTT topic prefix |

---

## Output Topic Structure

### `per_cell` mode

One MQTT message per cell per sample cycle.

```
batteries/site=0/rack=0/module=0/cell=0
batteries/site=0/rack=0/module=0/cell=1
...
batteries/site=1/rack=2/module=3/cell=5
```

**Payload:**

```json
{
  "timestamp": 1700000001.234,
  "site_id":   0,
  "rack_id":   0,
  "module_id": 0,
  "cell_id":   0,
  "voltage":             { "value": 3.712,  "unit": "V" },
  "current":             { "value": -1.23,  "unit": "A" },
  "temperature":         { "value": 24.5,   "unit": "C" },
  "soc":                 { "value": 79.1,   "unit": "percent" },
  "internal_resistance": { "value": 0.0104, "unit": "ohm" }
}
```

### `per_cell_item` mode

Five MQTT messages per cell per sample cycle (one per measurement).

```
batteries/site=0/rack=0/module=0/cell=0/voltage
batteries/site=0/rack=0/module=0/cell=0/current
batteries/site=0/rack=0/module=0/cell=0/temperature
batteries/site=0/rack=0/module=0/cell=0/soc
batteries/site=0/rack=0/module=0/cell=0/internal_resistance
```

**Payload:**

```json
{
  "timestamp": 1700000001.234,
  "value":     3.712,
  "unit":      "V"
}
```

---

## WebSocket API — Port 8765

Connect: `ws://<host>:8765`

### Client → Server

| Message | Effect |
|---------|--------|
| `{"type": "start"}` | Begin publishing MQTT messages |
| `{"type": "stop"}` | Pause publishing |
| `{"type": "get_status"}` | Request a `status` reply |
| `{"type": "update_config", "config": {...}}` | Hot-reload topology or settings; rebuilds cell list |

`update_config` fields (all optional, merged into running config):

```json
{
  "type": "update_config",
  "config": {
    "sites":                   2,
    "racks_per_site":          3,
    "modules_per_rack":        4,
    "cells_per_module":        6,
    "sample_interval_seconds": 1,
    "topic_mode":              "per_cell"
  }
}
```

### Server → Client

#### `status`

Sent on connect, after `start`/`stop`/`update_config`, and in response to `get_status`.

```json
{
  "type":       "status",
  "running":    true,
  "config":     {
    "sites": 2, "racks_per_site": 3, "modules_per_rack": 4,
    "cells_per_module": 6, "sample_interval_seconds": 1,
    "topic_mode": "per_cell"
  },
  "cell_count": 144
}
```

#### `stats`

Broadcast after each publish loop.

```json
{
  "type":           "stats",
  "total_published": 14400,
  "mps":             144,
  "last_loop_ms":    8.3
}
```

| Field | Notes |
|-------|-------|
| `total_published` | Cumulative MQTT messages published since start |
| `mps` | Messages per second in the last loop |
| `last_loop_ms` | Wall-clock time to publish one full cycle |

#### `samples`

Broadcast after each publish loop; up to 10 randomly selected cells.

```json
{
  "type": "samples",
  "samples": [
    {
      "cell_path": "site=0/rack=1/module=2/cell=3",
      "timestamp": 1700000001.234,
      "measurements": {
        "voltage":             { "value": 3.712,  "unit": "V" },
        "current":             { "value": -1.23,  "unit": "A" },
        "temperature":         { "value": 24.5,   "unit": "C" },
        "soc":                 { "value": 79.1,   "unit": "percent" },
        "internal_resistance": { "value": 0.0104, "unit": "ohm" }
      }
    }
  ]
}
```
