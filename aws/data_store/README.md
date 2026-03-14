# AWS Data Store Server

Monitors a MinIO (or AWS S3) bucket for incoming Parquet files and exposes a WebSocket interface for the browser-based AWS dashboard.  Also provides a synthetic Parquet data generator for testing the pipeline without a live generator.

---

## What it does

**Monitor mode** (continuous):
- Polls the configured S3 bucket every `poll_interval_seconds` (default 10 s).
- For each new object found, reads the Parquet file, extracts row count, column names, and a 5-row sample.
- Broadcasts a `file` message to all connected WebSocket clients.
- Tracks cumulative stats: total files, total rows, total bytes.

**Generate mode** (on-demand, via WebSocket command):
- Creates synthetic battery telemetry rows for the requested topology.
- Writes them as Parquet files directly into S3 with the same Hive partition layout used by the real pipeline (`site=N/date=YYYY-MM-DD/hour=HH/timestamp.parquet`).
- Sends `generate_progress` messages as each file is written.
- Useful for populating S3 without running the full NATS pipeline.

---

## Quick Start

```bash
cd aws/data_store
pip install boto3 pyarrow websockets pyyaml
python server.py
# or
python server.py --config config.yaml
```

---

## Config Reference

File: `aws/data_store/config.yaml`

```yaml
s3:
  endpoint_url: "http://localhost:9000"   # MinIO URL; omit for real AWS S3
  bucket: battery-data
  region: us-east-1
  access_key: minioadmin
  secret_key: minioadmin
  prefix: ""                              # optional key prefix to monitor

poll_interval_seconds: 10

websocket:
  host: 0.0.0.0
  port: 8766
```

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `s3.endpoint_url` | string | — | MinIO endpoint; omit for AWS S3 |
| `s3.bucket` | string | `battery-data` | Bucket to monitor (created if missing) |
| `s3.prefix` | string | `""` | Only monitor objects with this key prefix |
| `poll_interval_seconds` | int | `10` | Polling interval for new objects |
| `websocket.port` | int | `8766` | WebSocket server port |

---

## WebSocket API — Port 8766

Connect: `ws://<host>:8766`

### Client → Server

#### `get_status`

Request current status and stats.

```json
{ "type": "get_status" }
```

#### `set_interval`

Change the S3 poll interval at runtime.

```json
{ "type": "set_interval", "seconds": 5 }
```

#### `generate`

Generate synthetic Parquet files and upload them to S3.  All fields are optional.

```json
{
  "type":            "generate",
  "sites":           2,
  "racks_per_site":  3,
  "modules_per_rack": 4,
  "cells_per_module": 6,
  "rows_per_file":   500,
  "num_files":       1,
  "hours_ago":       1
}
```

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `sites` | int | `2` | Number of sites to generate data for |
| `racks_per_site` | int | `3` | |
| `modules_per_rack` | int | `4` | |
| `cells_per_module` | int | `6` | |
| `rows_per_file` | int | `500` | Rows written per Parquet file |
| `num_files` | int | `1` | Files to generate per site |
| `hours_ago` | float | `1` | Timestamp base — data will appear N hours in the past |

Total files written = `sites × num_files`.

### Server → Client

#### `status`

Sent on connect and after `set_interval`.

```json
{
  "type":          "status",
  "s3_connected":  true,
  "bucket":        "battery-data",
  "endpoint":      "http://localhost:9000",
  "poll_interval": 10
}
```

#### `stats`

Sent on connect and after each new file is processed.

```json
{
  "type":        "stats",
  "file_count":  42,
  "total_rows":  21000,
  "total_bytes": 1048576
}
```

#### `file`

Broadcast when a new Parquet object is found in S3.

```json
{
  "type":       "file",
  "key":        "site=0/date=2026-03-14/hour=14/20260314T140059Z.parquet",
  "size":       18432,
  "rows":       500,
  "columns":    ["timestamp", "site_id", "rack_id", "module_id", "cell_id",
                 "voltage", "current", "temperature", "soc", "internal_resistance"],
  "sample":     {
    "timestamp": [1700000001.0, 1700000002.0],
    "voltage":   [3.712, 3.714]
  },
  "arrived_at": "14:00:59"
}
```

#### `generate_progress`

Sent after each file is written during a `generate` operation.

```json
{
  "type":  "generate_progress",
  "done":  1,
  "total": 4,
  "key":   "site=0/date=2026-03-14/hour=13/20260314T130000Z.parquet"
}
```

#### `generate_done`

Sent when the `generate` operation completes (or fails).

```json
{
  "type":          "generate_done",
  "files_written": 4,
  "total_rows":    2000
}
```

On failure:

```json
{
  "type":          "generate_done",
  "error":         "Connection refused",
  "files_written": 0,
  "total_rows":    0
}
```
