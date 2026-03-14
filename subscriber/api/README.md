# Subscriber API

A single Python process that provides both real-time and historical access to battery telemetry stored in the NATS stack.

Two APIs are served from one event loop:

| API | Port | Protocol | Purpose |
|-----|------|----------|---------|
| WebSocket API | **8767** | WebSocket | Live NATS stream + DuckDB historical queries |
| Flux HTTP API | **8768** | HTTP/1.1 | InfluxDB2-compatible query endpoint |

---

## What it does

**Live path:**  A NATS JetStream push consumer with `DeliverPolicy.NEW` forwards messages arriving after the subscription is created to all connected WebSocket clients.  No historical backlog is delivered.

**Historical path:**  Client sends a `query_history` WebSocket message; the server runs a DuckDB SQL query directly against S3 Parquet files (via the `httpfs` extension) and returns columns + rows as JSON.

**Flux shim (`flux_compat.py`):**  A minimal asyncio HTTP server on port 8768 that accepts `POST /api/v2/query` with a Flux-language body, translates it to DuckDB SQL, and returns InfluxDB2 annotated CSV.  This lets `orig_aws.html` query the NATS-stack data store without any browser-side changes.

---

## Quick Start

```bash
cd subscriber/api
pip install nats-py duckdb websockets boto3 pyyaml
python server.py
# or
python server.py --config config.yaml
```

Both servers start immediately.  NATS and S3 connectivity is established asynchronously — clients receive `nats_connected: false` / `s3_connected: false` status until the background loops succeed.

---

## Config Reference

File: `subscriber/api/config.yaml`

```yaml
nats:
  url: "nats://localhost:4222"
  stream_name: BATTERY_DATA
  default_subject: "batteries.>"    # default when subscribe omits subject

s3:
  endpoint_url: "http://spark-22b6:9000"  # omit for real AWS S3
  bucket: battery-data
  region: us-east-1
  access_key: minioadmin
  secret_key: minioadmin
  prefix: ""

websocket:
  host: 0.0.0.0
  port: 8767

flux_http:
  port: 8768

history:
  default_limit: 1000     # rows returned if limit omitted
  max_limit: 10000        # hard cap regardless of client request
```

---

## WebSocket API Summary (port 8767)

Full reference: `docs/SUBSCRIBER_API.md`

**Client → Server:**

| Message type | Effect |
|-------------|--------|
| `get_status` | Returns `status` + `stats` |
| `subscribe` | Start live NATS push consumer; field `subject` defaults to `batteries.>` |
| `unsubscribe` | Stop live consumer |
| `query_history` | Run DuckDB query; fields: `query_id`, `site_id`, `from_ts`, `to_ts`, `limit` |

**Server → Client:**

| Message type | Notes |
|-------------|-------|
| `status` | `nats_connected`, `s3_connected`, `subscribed`, `subject` |
| `live` | One per NATS message; `subject` + `payload` (measurements flattened to floats) |
| `stats` | Broadcast every second: `live_total`, `live_per_sec`, `queries_run` |
| `history` | `columns`, `rows` (2-D array), `total`, `elapsed_ms` |
| `error` | `query_id` + `message` |

---

## Flux HTTP API Summary (port 8768)

**Pointing `orig_aws.html` here:**

In the orig_aws dashboard, set:
- **Host:** the machine running this server (e.g. `phil-dev`)
- **Port:** `8768`
- **Token:** any non-empty string (not validated)
- **Org / Bucket:** any value (server uses its own config)

The `/ping` endpoint returns 204 (not 200), which is handled correctly by `orig_aws.html` — it skips the ping check and connects directly.

**Supported Flux patterns:**

| Pattern | Keyword | Translates to |
|---------|---------|--------------|
| Time series | `aggregateWindow` | `GROUP BY` time bucket, `AVG()` per field |
| Heatmap | `last()` | Latest row per `(module_id, cell_id)` |
| Write rate | `count()` / `sum()` | `COUNT(*)` |

Output is InfluxDB2 annotated CSV with `#datatype`, `#group`, `#default` comment rows.

Full reference: `docs/SUBSCRIBER_API.md`

---

## Background Tasks

The server runs four concurrent asyncio tasks:

1. **WebSocket server** — accepts client connections on :8767.
2. **stats_loop** — broadcasts `stats` message to all clients every second.
3. **nats_connect_loop** — connects/reconnects to NATS every 5 seconds; restores live subscription after reconnect.
4. **s3_check_loop** — verifies S3 bucket accessibility every 15 seconds.
5. **serve_flux_api** — asyncio TCP server on :8768.
