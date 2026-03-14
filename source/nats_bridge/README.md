# NATS Bridge

Subscribes to FlashMQ via MQTT and publishes every message into the NATS JetStream `BATTERY_DATA` stream.  This is the **single ingestion point** for the durable pipeline — all downstream components (Parquet Writer, Subscriber API) consume from NATS, never from MQTT directly.

---

## What it does

1. Connects to FlashMQ (MQTT broker) and subscribes to `batteries/#`.
2. Connects to NATS and locates (or creates) the `BATTERY_DATA` JetStream stream.
3. For each incoming MQTT message, converts the topic from MQTT format to NATS subject format and publishes it to JetStream.
4. On any connection failure (MQTT or NATS), reconnects automatically with a 2-second back-off.

---

## Quick Start

```bash
cd source/nats_bridge
pip install aiomqtt nats-py pyyaml
python bridge.py
# or
python bridge.py --config config.yaml
```

---

## Config Reference

File: `source/nats_bridge/config.yaml`

```yaml
mqtt:
  host: localhost
  port: 1884
  username: ""
  password: ""
  client_id: nats-bridge
  topic: "batteries/#"   # MQTT wildcard
  qos: 1

nats:
  url: "nats://localhost:4222"
  stream_name: BATTERY_DATA
  stream_subjects:
    - "batteries.>"        # NATS wildcard — equivalent to MQTT batteries/#
  max_age_hours: 48        # message retention window
```

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `mqtt.host` | string | `localhost` | FlashMQ hostname or IP |
| `mqtt.port` | int | `1884` | |
| `mqtt.topic` | string | `batteries/#` | MQTT subscription filter |
| `mqtt.qos` | int | `1` | QoS for MQTT subscription |
| `nats.url` | string | `nats://localhost:4222` | NATS server URL |
| `nats.stream_name` | string | `BATTERY_DATA` | JetStream stream to publish into |
| `nats.stream_subjects` | list | `["batteries.>"]` | Subjects bound to the stream |
| `nats.max_age_hours` | int | `48` | Retention window for stream messages |

---

## Topic Conversion

MQTT uses `/` as the hierarchy separator; NATS uses `.`.  The bridge replaces every `/` with `.`:

| MQTT topic | NATS subject |
|-----------|--------------|
| `batteries/site=0/rack=1/module=2/cell=3` | `batteries.site=0.rack=1.module=2.cell=3` |
| `batteries/site=0/rack=1/module=2/cell=3/voltage` | `batteries.site=0.rack=1.module=2.cell=3.voltage` |

MQTT wildcard `batteries/#` maps to NATS wildcard `batteries.>`.

---

## Stream Auto-Creation

On startup the bridge calls `js.stream_info(stream_name)`.

- If the stream **does not exist**, it is created with `StorageType.FILE`, `RetentionPolicy.LIMITS`, and the subjects from config.
- If the stream **already exists**, creation is skipped — safe to restart at any time.

The Parquet Writer waits (retry loop) until the stream exists before creating its durable consumer, so startup order does not matter.

---

## Reconnection Behaviour

Both MQTT and NATS connections are managed inside a single `while True` retry loop:

1. Connect to NATS, ensure stream exists.
2. Connect to MQTT, subscribe to `batteries/#`.
3. Forward messages until an `aiomqtt.MqttError` or `nats.errors.Error` is raised.
4. On any error: drain the NATS connection, sleep 2 seconds, go to step 1.

No messages are lost during reconnect because unacknowledged messages remain in the JetStream stream and the Parquet Writer's durable consumer replays them.
