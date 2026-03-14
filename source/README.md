# Source — Parquet Writer Service

Runs on the FlashMQ EC2 instance. Consumes battery data from NATS JetStream (bridged from FlashMQ), buffers it, and writes partitioned Parquet files to S3. NATS JetStream provides zero data loss — if this service restarts, it replays missed messages from the durable stream.

## What Needs to Be Built

### 1. NATS JetStream Bridge (FlashMQ → NATS)
- FlashMQ forwards messages into NATS JetStream via its built-in bridge feature or a small bridge service
- NATS JetStream persists messages to disk with configurable retention
- Stream name TBD (e.g. `BATTERY_DATA`)
- Subject mapping from MQTT topics to NATS subjects (e.g. `sites.{id}.batteries.{id}`)

### 2. NATS JetStream Consumer
- Connect to NATS JetStream as a durable consumer (e.g. consumer name `parquet-writer`)
- Pull or push-based consumption — durable consumer tracks last acknowledged message
- On restart: automatically replays from last acknowledged position — no data loss
- Acknowledge messages only after successful S3 upload

### 3. In-Memory Buffer
- Accumulate incoming messages per site
- Flush on a time interval (e.g. every 5 minutes) or when buffer reaches a size threshold
- Backpressure handled by NATS — slow consumer pauses delivery rather than dropping messages

### 4. Parquet Writer
- Use PyArrow to serialise buffered messages to Parquet
- Schema TBD — depends on confirmed message format from site systems
- Apply snappy or zstd compression

### 5. S3 Uploader
- Upload completed Parquet files via boto3
- Partition path: `s3://{bucket}/site={site_id}/date={YYYY-MM-DD}/hour={HH}/{timestamp}.parquet`
- Acknowledge NATS messages only after confirmed S3 upload
- Retry on upload failure — messages remain unacknowledged and will be redelivered

### 6. Operational Concerns
- Logging and error reporting (avoid adding to CloudWatch costs — log to file or S3)
- Metrics: messages consumed, files written, upload failures, consumer lag
- Graceful shutdown (flush buffer and acknowledge before exit)

## Open Questions
- Confirm MQTT topic structure from site systems (needed for NATS subject mapping)
- Confirm message payload schema (fields, types, units)
- Agree on Parquet partition granularity (5-min files? hourly files with 5-min flushes?)
- IAM role / credentials for S3 write access
- NATS JetStream retention policy — how long to retain messages (e.g. 24h as safety buffer)

## Stack
- Python 3.11+
- `nats-py` — NATS JetStream client
- `pyarrow` — Parquet serialisation
- `boto3` — S3 upload

## NATS JetStream
- Open source, free — runs as a single binary on existing EC2
- No additional AWS cost unless a dedicated instance is preferred (~$30/month t3.medium)
