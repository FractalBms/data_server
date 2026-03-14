# Subscriber API Reference

The Subscriber API is a single Python process (`subscriber/api/server.py`) that exposes two APIs:

- **WebSocket API** on port **8767** — live NATS stream + historical DuckDB queries
- **Flux HTTP API** on port **8768** — InfluxDB2-compatible `POST /api/v2/query`

Both APIs share the same DuckDB connection and S3 credentials.  The WebSocket server and the Flux HTTP server are started as concurrent asyncio tasks in the same event loop.

---

## 1. WebSocket API — Port 8767

Connect: `ws://<host>:8767`

All messages are JSON objects with a `type` field.

### 1.1 Client → Server Messages

#### `get_status`

Request current connection status and statistics.  The server replies with a `status` message followed by a `stats` message.

```json
{ "type": "get_status" }
```

---

#### `subscribe`

Start a live NATS push consumer (`DeliverPolicy.NEW`).  Only messages arriving **after** this call are delivered.  Subscribing again replaces the existing subscription.

```json
{
  "type":    "subscribe",
  "subject": "batteries.>"
}
```

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `subject` | string | `batteries.>` (from config) | NATS subject filter; use `batteries.site=0.>` to restrict to one site |

---

#### `unsubscribe`

Stop the live NATS push consumer.

```json
{ "type": "unsubscribe" }
```

---

#### `query_history`

Run a historical query against S3 Parquet via DuckDB.  The server responds with a `history` or `error` message.

```json
{
  "type":     "query_history",
  "query_id": "q1",
  "site_id":  "0",
  "from_ts":  1700000000.0,
  "to_ts":    1700003600.0,
  "limit":    1000
}
```

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `query_id` | string | `"q"` | Echoed back in the response; use to correlate async responses |
| `site_id` | string | `""` | Hive partition value; omit (or `""`) to query all sites |
| `from_ts` | float | `now - 3600` | Unix timestamp seconds (start of range, inclusive) |
| `to_ts` | float | `now` | Unix timestamp seconds (end of range, inclusive) |
| `limit` | int | `default_limit` from config (1000) | Capped at `max_limit` (10 000) |

Results are ordered by `timestamp DESC` (most recent first).

---

### 1.2 Server → Client Messages

#### `status`

Sent on connect, on any connection state change, and in response to `get_status` or `subscribe`/`unsubscribe`.

```json
{
  "type":           "status",
  "nats_connected": true,
  "s3_connected":   true,
  "subscribed":     true,
  "subject":        "batteries.>"
}
```

| Field | Type | Notes |
|-------|------|-------|
| `nats_connected` | bool | Whether the NATS server is reachable |
| `s3_connected` | bool | Whether the S3/MinIO bucket is reachable (checked every 15 s) |
| `subscribed` | bool | Whether a live NATS push subscription is active |
| `subject` | string | Active NATS subject, or `""` if not subscribed |

---

#### `live`

One message per NATS message received on the active subscription.  Measurements are flattened from `{value, unit}` objects to plain scalars.

```json
{
  "type":    "live",
  "subject": "batteries.site=0.rack=1.module=2.cell=3",
  "payload": {
    "timestamp":           1700000001.234,
    "site_id":             0,
    "rack_id":             1,
    "module_id":           2,
    "cell_id":             3,
    "voltage":             3.712,
    "current":             -1.23,
    "temperature":         24.5,
    "soc":                 79.1,
    "internal_resistance": 0.0104
  }
}
```

---

#### `stats`

Broadcast every second by a background loop, and also in response to `get_status`.

```json
{
  "type":        "stats",
  "live_total":  4320,
  "live_per_sec": 144,
  "queries_run": 3
}
```

| Field | Type | Notes |
|-------|------|-------|
| `live_total` | int | Total live messages received since server start |
| `live_per_sec` | int | Messages received in the last 1 second (sliding window) |
| `queries_run` | int | Total historical queries executed since server start |

---

#### `history`

Response to `query_history`.  Contains column names and rows as a 2D array (columnar access: `rows[i][columns.indexOf("voltage")]`).

```json
{
  "type":       "history",
  "query_id":   "q1",
  "columns":    ["timestamp", "site_id", "rack_id", "module_id", "cell_id",
                 "voltage", "current", "temperature", "soc", "internal_resistance"],
  "rows":       [
    [1700003599.1, 0, 1, 2, 3, 3.712, -1.23, 24.5, 79.1, 0.0104],
    ...
  ],
  "total":      847,
  "elapsed_ms": 231.4
}
```

| Field | Type | Notes |
|-------|------|-------|
| `columns` | string[] | Column names in order |
| `rows` | array of arrays | Each inner array matches `columns` order; numeric values are float, others are string |
| `total` | int | Number of rows returned (≤ `limit`) |
| `elapsed_ms` | float | Query execution time |

---

#### `error`

Returned when a `subscribe` or `query_history` command fails.

```json
{
  "type":     "error",
  "query_id": "q1",
  "message":  "No files found matching s3://battery-data/**/*.parquet"
}
```

---

### 1.3 Python Client Example

```python
import asyncio, json, time
import websockets

async def main():
    async with websockets.connect("ws://phil-dev:8767") as ws:
        # Request live subscription
        await ws.send(json.dumps({"type": "subscribe", "subject": "batteries.>"}))

        # Run a history query
        await ws.send(json.dumps({
            "type":     "query_history",
            "query_id": "q1",
            "site_id":  "0",
            "from_ts":  time.time() - 3600,
            "to_ts":    time.time(),
            "limit":    500,
        }))

        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "live":
                print("LIVE", msg["subject"], msg["payload"]["voltage"])
            elif msg["type"] == "history":
                print(f"HISTORY {msg['total']} rows in {msg['elapsed_ms']}ms")
                cols = msg["columns"]
                for row in msg["rows"][:5]:
                    print(dict(zip(cols, row)))
            elif msg["type"] == "stats":
                print("STATS", msg)

asyncio.run(main())
```

---

### 1.4 JavaScript Client Example

```js
const ws = new WebSocket("ws://phil-dev:8767");

ws.onopen = () => {
  ws.send(JSON.stringify({ type: "subscribe", subject: "batteries.>" }));
  ws.send(JSON.stringify({
    type: "query_history", query_id: "q1",
    from_ts: Date.now() / 1000 - 3600,
    to_ts:   Date.now() / 1000,
    limit:   1000,
  }));
};

ws.onmessage = ({ data }) => {
  const msg = JSON.parse(data);
  if (msg.type === "live") {
    console.log("live", msg.subject, msg.payload.voltage);
  } else if (msg.type === "history") {
    const idx = Object.fromEntries(msg.columns.map((c, i) => [c, i]));
    console.log(`${msg.total} rows, ${msg.elapsed_ms}ms`);
    msg.rows.slice(0, 5).forEach(r => console.log(r[idx.voltage]));
  }
};
```

---

## 2. Flux HTTP API — Port 8768

The Flux HTTP API is an InfluxDB2 shim that allows `orig_aws.html` (and any other InfluxDB2 client) to query the NATS-stack Parquet data without code changes.

### 2.1 Endpoints

| Method | Path | Response |
|--------|------|----------|
| `GET` | `/ping` | `204 No Content` |
| `GET` | `/health` | `204 No Content` |
| `OPTIONS` | any | `204 No Content` with CORS headers |
| `POST` | `/api/v2/query` | `200 text/csv` — InfluxDB2 annotated CSV |

All responses include CORS headers (`Access-Control-Allow-Origin: <origin>`).

### 2.2 Supported Flux Patterns

The shim recognises three query patterns by keyword inspection:

#### Pattern 1 — Time Series (`aggregateWindow`)

```flux
from(bucket: "battery-data")
  |> range(start: -1h)
  |> filter(fn: (r) => r.site_id == "0")
  |> filter(fn: (r) => r._field == "voltage")
  |> aggregateWindow(every: 10s, fn: mean)
```

Translates to DuckDB `GROUP BY` time bucket with `AVG()` per field.  Returns one annotated CSV table per field (`_field`, `_value`, `_time`).

#### Pattern 2 — Heatmap (`last()`)

```flux
from(bucket: "battery-data")
  |> range(start: -5m)
  |> filter(fn: (r) => r._field == "voltage")
  |> last()
```

Translates to a window function query that returns the latest voltage row per `(module_id, cell_id)` pair.

#### Pattern 3 — Write Rate (`count()` / `sum()`)

```flux
from(bucket: "battery-data")
  |> range(start: -1m)
  |> filter(fn: (r) => r._field == "voltage")
  |> count()
  |> sum()
```

Translates to `COUNT(*)`.  Returns a single `_value` row.

### 2.3 Flux Parsing Rules

| Flux syntax | Parsed as |
|-------------|-----------|
| `range(start: -Ns)` | `now - N seconds` |
| `range(start: -Nm)` | `now - N*60 seconds` |
| `range(start: -Nh)` | `now - N*3600 seconds` |
| `range(start: -Nd)` | `now - N*86400 seconds` |
| `r.field == "value"` | `WHERE CAST(field AS VARCHAR) = 'value'` |
| `aggregateWindow(every: Xs)` | `GROUP BY floor(timestamp/X)*X` |

Supported filter column names: `site_id`, `rack_id`, `module_id`, `cell_id`.  The `_field` filter is used for query type classification only — all numeric columns are always returned.

### 2.4 Annotated CSV Output Format

Example timeseries response (truncated):

```
#datatype,string,long,dateTime:RFC3339,double,string
#group,false,false,false,false,true
#default,_result,,,,
,result,table,_time,_value,_field
,,0,2026-03-14T13:00:10Z,3.712340,voltage
,,0,2026-03-14T13:00:20Z,3.714110,voltage
...

#datatype,string,long,dateTime:RFC3339,double,string
...
,,1,2026-03-14T13:00:10Z,24.512300,temperature
...
```

A blank line separates each field table.

### 2.5 Pointing orig_aws.html at Port 8768

In `orig_aws.html`, change the InfluxDB2 host/port inputs:

- Host: `phil-dev` (or whichever machine runs the Subscriber API)
- Port: `8768` (instead of `8086`)
- Token: any non-empty string (the shim does not validate tokens)
- Org / Bucket: any value (the shim reads the bucket from its own config)

The `/ping` endpoint returns 204 (not 200), which is handled by the dashboard's connection probe logic — `orig_aws.html` skips the `/ping` check and connects directly.

### 2.6 HTTP Client Example

```python
import requests

response = requests.post(
    "http://phil-dev:8768/api/v2/query",
    data="""
        from(bucket: "battery-data")
          |> range(start: -1h)
          |> filter(fn: (r) => r.site_id == "0")
          |> aggregateWindow(every: 10s, fn: mean)
    """,
    headers={"Content-Type": "application/vnd.flux"},
)
print(response.text)
```

```js
const body = `from(bucket: "battery-data")
  |> range(start: -1h)
  |> aggregateWindow(every: 10s, fn: mean)`;

const resp = await fetch("http://phil-dev:8768/api/v2/query", {
  method: "POST",
  body,
});
const csv = await resp.text();
console.log(csv);
```

---

## 3. Configuration Reference

Config file: `subscriber/api/config.yaml`

```yaml
nats:
  url: "nats://localhost:4222"
  stream_name: BATTERY_DATA
  default_subject: "batteries.>"   # used when subscribe omits subject

s3:
  endpoint_url: "http://spark-22b6:9000"  # omit for real AWS S3
  bucket: battery-data
  region: us-east-1
  access_key: minioadmin
  secret_key: minioadmin
  prefix: ""                              # optional key prefix

websocket:
  host: 0.0.0.0
  port: 8767

flux_http:
  port: 8768

history:
  default_limit: 1000   # rows returned when limit omitted
  max_limit: 10000      # hard cap regardless of client request
```

---

## 4. Running the Server

```bash
cd subscriber/api
pip install nats-py duckdb websockets boto3 pyyaml
python server.py
# or
python server.py --config config.yaml
```

The process starts both the WebSocket server (:8767) and the Flux HTTP server (:8768) simultaneously.  NATS and S3 connectivity is established asynchronously — the WebSocket server accepts clients immediately and reports `nats_connected: false` / `s3_connected: false` until the background connection loops succeed.
