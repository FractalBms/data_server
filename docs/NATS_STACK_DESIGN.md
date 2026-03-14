# NATS Stack Design Document

Battery telemetry pipeline — durable, zero-loss path from MQTT to queryable Parquet on S3.

---

## 1. Architecture Overview

```
  [Generator]
      |
      | MQTT QoS-1  :1883/:1884
      v
  [FlashMQ]  (MQTT broker)
      |
      | MQTT subscribe  batteries/#
      v
  [NATS Bridge]  (source/nats_bridge/bridge.py)
      |
      | NATS JetStream publish  batteries.*.*.*.*
      v
  [NATS JetStream]  — stream: BATTERY_DATA  (48-hour retention, file storage)
      |
      +------ pull consumer (durable) --------> [Parquet Writer]
      |                                               |
      |                                               | Parquet files, Snappy compressed
      |                                               v
      |                                          [MinIO / S3]
      |                                          battery-data bucket
      |                                          Hive partition layout
      |
      +------ push consumer (DeliverPolicy.NEW) ------> [Subscriber API :8767]
                                                              |
                                              +---------------+---------------+
                                              |                               |
                                    WebSocket clients                DuckDB query
                                    (live stream)                  (historical, reads S3)
                                              |
                                         [Flux shim :8768]
                                         (InfluxDB2-compatible HTTP)
```

**Separate orig_aws stack** (Telegraf + InfluxDB2) runs in parallel; the two stacks share the same FlashMQ broker but are otherwise independent.

---

## 2. Component Reference

### 2.1 Generator

| Property | Value |
|----------|-------|
| Source   | `source/generator/generator.py` |
| Input    | Config file + WebSocket control commands |
| Output   | MQTT topics on FlashMQ |
| Ports    | MQTT publish → :1883/:1884 ; WebSocket server :8765 |

Simulates a battery pack hierarchy (sites × racks × modules × cells).  Each cell has five measurements: `voltage`, `current`, `temperature`, `soc`, `internal_resistance`.  Values drift via bounded random walk.

**Topic modes:**

- `per_cell` — one MQTT message per cell per sample, JSON payload carrying all five measurements.
- `per_cell_item` — one MQTT message per measurement per cell per sample (5× message volume).

Default topology: 2 sites × 3 racks × 4 modules × 6 cells = **144 cells**.

---

### 2.2 NATS Bridge

| Property | Value |
|----------|-------|
| Source   | `source/nats_bridge/bridge.py` |
| Input    | MQTT subscription `batteries/#` on FlashMQ |
| Output   | NATS JetStream subject `batteries.*.*.*.*` |
| Config   | `source/nats_bridge/config.yaml` |
| Deps     | `aiomqtt`, `nats-py`, `pyyaml` |

The bridge is the **single ingestion point** for the durable pipeline.  All downstream components consume from NATS, never from MQTT directly.

**Topic conversion:**

```
MQTT:  batteries/site=0/rack=1/module=2/cell=3
NATS:  batteries.site=0.rack=1.module=2.cell=3
```

Slash `/` is replaced by dot `.` because NATS uses dots as hierarchy separators.  The wildcard equivalent of MQTT `batteries/#` is NATS `batteries.>`.

**Stream auto-creation:**  On startup the bridge calls `js.stream_info(stream_name)`.  If the stream does not exist it creates it with the config in section 4.  If it already exists the bridge skips creation — safe to restart.

**Reconnection:**  Both the MQTT and NATS connections are wrapped in an infinite retry loop with a 2-second back-off.  On reconnect the bridge drains the NATS connection before closing it.

---

### 2.3 Parquet Writer

| Property | Value |
|----------|-------|
| Source   | `source/parquet_writer/writer.py` |
| Input    | NATS JetStream pull consumer (durable: `parquet-writer`) |
| Output   | Parquet files uploaded to MinIO / S3 |
| Config   | `source/parquet_writer/config.yaml` |
| Deps     | `nats-py`, `pyarrow`, `boto3`, `pyyaml` |

The writer subscribes as a **durable pull consumer** (`DeliverPolicy.ALL`).  Messages are accumulated in an in-memory buffer and flushed periodically.  Acks are sent **only after** a successful S3 upload — see section 5 for the zero-loss guarantee.

Flush is triggered by whichever condition is met first:
- `flush_interval_seconds` has elapsed (default: 60 s)
- `max_messages` buffered messages reached (default: 10 000)

On shutdown (SIGINT/SIGTERM) a final flush is attempted before draining the NATS connection.

**Payload flattening:**  The generator emits measurements as `{"value": 3.71, "unit": "V"}` objects.  The writer flattens these to plain floats before writing Parquet so that DuckDB queries see simple numeric columns.

---

### 2.4 Subscriber API

| Property | Value |
|----------|-------|
| Source   | `subscriber/api/server.py` + `subscriber/api/flux_compat.py` |
| Input    | NATS JetStream (live), S3 Parquet (historical) |
| Output   | WebSocket stream on :8767 ; HTTP on :8768 |
| Config   | `subscriber/api/config.yaml` |
| Deps     | `nats-py`, `duckdb`, `websockets`, `boto3`, `pyyaml` |

Two APIs are served from one process.  See `docs/SUBSCRIBER_API.md` for the full API reference.

**Live path:**  A NATS push consumer with `DeliverPolicy.NEW` forwards messages to all connected WebSocket clients.  Only messages arriving _after_ the subscription is created are delivered — no historical backlog floods the client.

**Historical path:**  DuckDB reads Parquet files directly from S3 via the `httpfs` extension, using Hive partition pruning for efficiency.

**Flux shim:**  `flux_compat.py` implements a minimal asyncio HTTP server that accepts `POST /api/v2/query` with a Flux body, translates it to DuckDB SQL, and returns InfluxDB2 annotated CSV.  This allows `orig_aws.html` to query the NATS-stack data store without any browser-side changes — just swap the InfluxDB2 port (8086) to 8768.

---

### 2.5 AWS Data Store Server

| Property | Value |
|----------|-------|
| Source   | `aws/data_store/server.py` |
| Input    | S3/MinIO bucket (polls for new Parquet objects) |
| Output   | WebSocket stream on :8766 |
| Config   | `aws/data_store/config.yaml` |
| Deps     | `boto3`, `pyarrow`, `websockets`, `pyyaml` |

Provides a browser-visible view of Parquet files arriving in S3 and a synthetic data generator for testing without a live pipeline.

---

## 3. Data Flow — End to End

```
1. Generator wakes every sample_interval_seconds (default 1 s).
2. For each of the 144 cells it steps each measurement via random walk.
3. Publishes one MQTT message per cell (per_cell mode) or five per cell (per_cell_item).
   Total: 144 msg/s or 720 msg/s.
4. FlashMQ delivers to all subscribers, including the NATS Bridge.
5. Bridge converts topic format and calls js.publish() for each message.
   NATS JetStream stores the message durably on disk.
6. Parquet Writer's pull consumer fetches up to 500 messages per iteration.
   After flush_interval_seconds or max_messages, the buffer is serialised to
   Parquet (Snappy compressed) and uploaded to S3.
   ACKs are sent after upload succeeds.
7. Subscriber API's push consumer (DeliverPolicy.NEW) receives the same
   JetStream messages concurrently and forwards them to WebSocket clients.
8. For historical queries, the Subscriber API issues a DuckDB SQL query that
   reads Parquet files from S3 directly using the httpfs extension.
```

---

## 4. NATS JetStream Stream Configuration

Stream name: **BATTERY_DATA**

| Parameter | Value | Notes |
|-----------|-------|-------|
| `subjects` | `["batteries.>"]` | Matches all cell topics |
| `storage` | `FILE` | Persists to disk |
| `retention` | `LIMITS` | Age + size based |
| `max_age` | 48 hours | Safety buffer — enough for parquet_writer restart recovery |
| `ack_wait` | 60 s | Parquet writer consumer; matches max upload time |
| `max_deliver` | 10 | Redelivery attempts before giving up on a message |
| `deliver_policy` (live) | `NEW` | Push consumer only sees messages after subscribe |
| `deliver_policy` (writer) | `ALL` | Durable consumer replays from last ack position |

The bridge creates the stream on first start.  Subsequent starts skip creation (`stream_info` returns successfully).

---

## 5. Zero Data Loss Guarantee

The Parquet Writer never acknowledges a NATS message until the corresponding Parquet file has been successfully uploaded to S3:

```
buffer.append((payload, nats_msg))
...
await upload_with_retry(s3, bucket, key, data)   # up to 5 attempts, exponential backoff
for msg in msgs_to_ack:
    await msg.ack()                               # only reached if upload succeeded
```

Consequence: if the writer process crashes, or S3 is unreachable, or the upload fails on all retries, the NATS messages remain **unacknowledged**.  When the writer restarts it reconnects with the same durable consumer name and NATS replays all unacknowledged messages from the last committed position.

**Conditions under which data can be lost:**

- A message was acknowledged but the S3 upload was not yet durable (effectively impossible with synchronous `put_object`).
- The NATS stream's 48-hour retention window expires before the writer recovers — an extreme failure scenario.
- The NATS server disk is lost (not covered by this pipeline; use NATS clustering for HA).

---

## 6. Hive Partition Layout in S3

```
s3://battery-data/
  site=0/
    date=2026-03-14/
      hour=13/
        20260314T130009Z.parquet
        20260314T133335Z.parquet
      hour=14/
        20260314T140059Z.parquet
        ...
  site=1/
    date=2026-03-14/
      ...
```

The filename is the UTC flush timestamp in compact ISO-8601 format (`YYYYMMDDTHHMMSSz`).

DuckDB reads this layout with `hive_partitioning=true`, which exposes `site`, `date`, and `hour` as virtual columns.  Queries filtered by `site_id` receive partition pruning automatically.

**Parquet schema** (per row):

| Column | Type | Notes |
|--------|------|-------|
| `timestamp` | float64 | Unix epoch seconds |
| `site_id` | int/float | From generator |
| `rack_id` | int/float | |
| `module_id` | int/float | |
| `cell_id` | int/float | |
| `voltage` | float64 | Volts |
| `current` | float64 | Amps |
| `temperature` | float64 | Celsius |
| `soc` | float64 | Percent |
| `internal_resistance` | float64 | Ohms |

---

## 7. Flux Compatibility Shim Design

`flux_compat.py` is a minimal asyncio TCP server that speaks HTTP/1.1.  It does **not** depend on any HTTP framework.

**Supported endpoints:**

| Method | Path | Action |
|--------|------|--------|
| `OPTIONS` | `*` | CORS preflight → 204 |
| `GET` | `/ping` | Health probe → 204 |
| `GET` | `/health` | Health probe → 204 |
| `POST` | `/api/v2/query` | Execute Flux query → annotated CSV |

**Query classification** (by keyword scan of the Flux string):

| Keyword present | Query type | DuckDB translation |
|-----------------|------------|--------------------|
| `aggregateWindow` | `timeseries` | `GROUP BY time bucket`, `AVG()` per field |
| `last()` | `heatmap` | `ROW_NUMBER() OVER (PARTITION BY module_id, cell_id ORDER BY timestamp DESC)` |
| neither | `writerate` | `COUNT(*)` |

**Flux parsing is regex-based** — not a full Flux parser.  Supported Flux patterns:

- `range(start: -1h)` / `range(start: -30m)` etc.
- `filter(fn: (r) => r.site_id == "0")` style equality filters.
- `aggregateWindow(every: 10s, fn: mean)`

**Output format** is InfluxDB2 annotated CSV with `#datatype`, `#group`, `#default` comment rows, followed by data rows.  `orig_aws.html` parses this format natively.

---

## 8. Two-Machine Deployment

### phil-dev (x86-64)

Runs the NATS-stack manager on port **8761** and the orig_aws manager on **8760**.

| Process | Port | Note |
|---------|------|------|
| NATS Bridge | — | Connects to FlashMQ on localhost and NATS on localhost |
| Parquet Writer | — | Connects to NATS on localhost, uploads to MinIO on spark-22b6 |
| Subscriber API | 8767, 8768 | WebSocket + Flux HTTP |
| Generator | 8765 | WebSocket control |

### spark-22b6 (arm64)

Runs the NATS-stack manager on port **8762** and the orig_aws manager on **8763**.

| Process | Port | Note |
|---------|------|------|
| MinIO | 9000 | S3-compatible object store |
| NATS Server | 4222 | JetStream enabled |
| FlashMQ | 1883/1884 | MQTT broker |

**localStorage keys used by browser UI:**

| Key | Default | Points to |
|-----|---------|-----------|
| `ds_host` | `phil-dev` | NATS-stack Subscriber API (8767/8768) |
| `aws_host` | `spark-22b6` | MinIO/S3 store monitor (8766) |

---

## 9. Configuration Files

| File | Purpose |
|------|---------|
| `source/generator/config.yaml` | Topology (sites/racks/modules/cells), MQTT broker, topic mode |
| `source/nats_bridge/config.yaml` | MQTT broker, NATS URL, stream name and subjects |
| `source/parquet_writer/config.yaml` | NATS consumer, S3/MinIO credentials, buffer tuning, compression |
| `subscriber/api/config.yaml` | NATS URL, S3 credentials, WebSocket port, Flux HTTP port, history limits |
| `aws/data_store/config.yaml` | S3/MinIO credentials, poll interval, WebSocket port |
| `manager/config.yaml` | Process registry for dashboard.html manager UI |

---

## 10. Dependencies

| Package | Used by | Purpose |
|---------|---------|---------|
| `aiomqtt` | nats_bridge | Async MQTT client |
| `paho-mqtt` | generator | Sync MQTT client (simpler for publish-only use) |
| `nats-py` | nats_bridge, parquet_writer, subscriber/api | NATS JetStream client |
| `pyarrow` | parquet_writer, aws/data_store | Parquet read/write |
| `boto3` | parquet_writer, subscriber/api, aws/data_store | S3 / MinIO API |
| `duckdb` | subscriber/api | In-process SQL over S3 Parquet |
| `websockets` | generator, subscriber/api, aws/data_store | WebSocket server |
| `pyyaml` | all | Config file parsing |
| `botocore` | parquet_writer, aws/data_store | AWS exception types |
