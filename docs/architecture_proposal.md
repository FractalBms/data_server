# Data Server Architecture Proposal

## Current Architecture

```
Site systems → MQTT → FlashMQ (EC2) → Subscribers (real-time)
                           ↓
                      Telegraf → InfluxDB 2 (EC2/EFS)
AWS IoT Core (role unclear / possibly redundant)
```

## Proposed Architecture

```
Site systems → MQTT → FlashMQ (EC2)
                           ├── Subscribers (real-time, direct MQTT — unchanged)
                           └── NATS JetStream (bridge) ← suggested, see below
                                    ├── Parquet Writer Service → S3
                                    └── Data Server API (live stream)
                                                         ↑
                                     Data Server API (new)
                                       ├── GET /live      → NATS JetStream
                                       └── GET /history   → DuckDB → S3 Parquet
```

## Components

### FlashMQ (retained)
- Continues to serve real-time data directly to subscribers
- Bridges into NATS JetStream for durable internal consumers

### NATS JetStream (suggested — zero data loss requirement)
- Sits between FlashMQ and internal consumers (Parquet Writer, Data Server API)
- Provides durable, persistent message stream — messages written to disk
- Consumers can replay from any point — if Parquet Writer restarts, it catches up with no data loss
- At-least-once delivery guarantee
- **Cost: free (open source)** — runs as a single lightweight binary on existing EC2, or a t3.medium (~$30/month) if dedicated instance preferred
- Replaces the direct MQTT subscription used by Telegraf today
- Alternative to NATS JetStream: Apache Kafka (more complex, heavier, higher cost) or Redis Streams (simpler but less battle-tested at this scale)

### Parquet Writer Service (new, build on existing EC2)
- Subscribes to NATS JetStream (durable consumer — no data loss on restart)
- Buffers incoming messages in memory (e.g. 5-minute windows)
- Writes partitioned Parquet files: `s3://bucket/site={id}/date={YYYY-MM-DD}/hour={HH}/`
- Uploads to S3 via boto3
- Stack: Python + PyArrow + boto3

### S3 (replaces EFS + InfluxDB 2 storage)
- Cheap columnar time-series storage (~$23/TB/month)
- Parquet files partitioned by site/date/hour for efficient querying
- Retention policies to manage long-term storage costs

### Data Server API (new)
- Single interface for all subscriber data needs
- `/live` — proxies to FlashMQ for real-time data
- `/history` — queries S3 Parquet via embedded DuckDB
- Stack: Python (FastAPI recommended) + DuckDB (httpfs extension)
- Runs on existing EC2 — no new infra required

### DuckDB (embedded in Data Server API)
- Queries S3 Parquet directly — no separate DB server
- Fast columnar reads on time-series data
- SQL interface
- Free, open source

## Components Eliminated

| Component | Monthly Cost | Reason |
|---|---|---|
| InfluxDB 2 (EC2) | ~$20-25k | Replaced by S3 + DuckDB |
| EFS | ~$10k | InfluxDB 2 storage replaced by S3 |
| AWS IoT Core | ~$3k | Redundant — FlashMQ handles all routing |
| Telegraf | — | Replaced by Parquet Writer Service |

## Cost Savings

| Item | Current | Proposed | Saving |
|---|---|---|---|
| EC2 (InfluxDB 2) | ~$20-25k | ~$0 (decommissioned) | ~$20-25k |
| EFS | $10k | ~$0 (decommissioned) | ~$10k |
| S3 (Parquet storage) | $3k | ~$3-4k (slight increase) | -$1k |
| IoT Core | $3k | $0 | $3k |
| CloudWatch (log retention fix) | $2.6k | ~$1k | $1.6k |
| Lambda (right-size) | $2k | ~$1k | $1k |
| **Total** | **~$76k** | **~$35-37k** | **~$39-41k** |

Target saving: **~$40k/month**

## Implementation Phases

### Phase 1 — Quick wins (weeks, ~$5.6k/month saving)
- Investigate and eliminate IoT Core if redundant
- Fix CloudWatch log retention policies
- Right-size Lambda memory allocations

### Phase 2 — Parquet Writer Service (weeks, ~$0 saving but lays the foundation)
- Build and deploy Parquet Writer Service on existing EC2
- Run in parallel with Telegraf/InfluxDB 2 to validate data integrity
- Confirm S3 partitioning scheme works for query patterns

### Phase 3 — Data Server API + decommission (weeks to months, ~$35k/month saving)
- Build Data Server API with DuckDB historical query endpoint
- Validate historical queries against InfluxDB 2 data
- Decommission InfluxDB 2, EFS, and Telegraf once validated

## Open Questions
- Exact message volume and schema (needed to finalise Parquet partitioning strategy)
- What the real-time subscribers are (web apps, services, mobile?) — affects API design
- Whether any Lambda functions are IoT Core-triggered (affects Phase 1 scope)
- Data retention requirements for Parquet files on S3
