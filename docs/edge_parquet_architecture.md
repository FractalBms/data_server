# Edge Parquet Architecture

## Overview

Move parquet file generation to the site controller (edge) rather than the cloud.
Each site runs the full NATS pipeline locally, generates compressed parquet files,
and uploads them directly to S3. AWS becomes a thin query layer rather than a
message processing pipeline.

---

## Architecture Diagram

```
SITE CONTROLLER (on-premise / edge)
┌─────────────────────────────────────────────────────────┐
│                                                         │
│  FlashMQ (MQTT broker)                                  │
│       │                                                 │
│       ▼                                                 │
│  NATS Bridge          ──────────────────────────────┐  │
│       │               NATS Leaf Node                │  │
│       ▼               (live feed to AWS)            │  │
│  NATS JetStream  ─────────────────────────────────► │  │
│       │                                             │  │
│       ▼                                             │  │
│  Parquet Writer                                     │  │
│       │                                             │  │
│       ▼                                             │  │
│  Local parquet files ──► S3 upload (every 60s)      │  │
│                                                     │  │
└─────────────────────────────────────────────────────┘  │
                                                          │
                         ┌────────────────────────────────┘
                         │ NATS Leaf Node connection
                         ▼
AWS
┌─────────────────────────────────────────────────────────┐
│                                                         │
│  NATS Server (live cache only — no JetStream needed)    │
│       │                                                 │
│       ▼                                                 │
│  Subscriber API  ◄──── S3 (parquet store)               │
│       │                                                 │
│       ▼                                                 │
│  Monitor / Viewer (browser)                             │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

---

## How It Works

### Edge (site controller)

The full pipeline already proven on spark-22b6 runs locally on each site:

- **FlashMQ** — unchanged, receives MQTT from battery/solar/wind/grid devices
- **NATS Bridge** — bridges MQTT topics to NATS JetStream subjects
- **NATS JetStream** — local buffer (1–2 hour retention), provides replay on restart
- **Parquet Writer** — flushes to local disk every 60 seconds, then uploads to S3
- **NATS Leaf Node** — forwards live messages to the AWS NATS server in real time

The site controller continues operating fully offline if the WAN link drops.
Parquet files queue locally and upload when connectivity resumes — no data loss.

### Cloud (AWS)

AWS becomes a thin read layer:

- **NATS Server** — receives live messages via Leaf Node from each site, fills the
  in-memory buffer in the Subscriber API for fast recent-history queries
- **Subscriber API** — serves history queries from S3 parquet, recent queries from
  the live buffer. Identical code to the current implementation
- **S3** — stores parquet files uploaded by each site. No change to query logic

---

## NATS Leaf Nodes

NATS has built-in support for leaf node connections. A site NATS server connects
outbound to the AWS NATS server over a single TLS connection and forwards subjects
matching a configured filter (e.g. `batteries.>`).

No code changes required — leaf node is configured in `nats-server.conf`:

```
# Site nats-server.conf
leafnodes {
  remotes [
    {
      url: "nats://aws-nats.example.com:7422"
      credentials: "/etc/nats/site.creds"
      account: "BATTERY_DATA"
    }
  ]
}
```

The AWS NATS server receives messages from all sites on the same subject space,
exactly as if they were published locally. The Subscriber API sees no difference.

---

## Data Flow Comparison

### Current architecture (cloud pipeline)

```
Site MQTT → [WAN] → AWS IoT Core → Lambda → InfluxDB
                  8,000 msgs/s × ~1 KB = 8 MB/s raw
                  = ~700 GB/day transfer per site
```

### Edge parquet architecture

```
Site MQTT → Local NATS → Parquet Writer → [WAN] → S3
                         60s flush × ~12 MB = ~17 GB/day
                         (same data, 40× less transfer)

Site NATS → [Leaf Node, single TCP connection] → AWS NATS
                         live messages only, no persistence cost
```

---

## Cost Comparison

### Per site, stress load (1,152 cells, ~8,000 msgs/s)

| Component | Current NATS (cloud) | Edge Parquet | InfluxDB (current AWS) |
|-----------|---------------------|--------------|----------------------|
| EC2 | $37 (`t3.large`) | $10 (`t3.small`) | $182 (`r5.xlarge`) |
| EBS JetStream | $2.50 | $0 | $25 |
| S3 storage | $10 | $10 | — |
| Data transfer | $14 | $0.35 | $3,600* |
| IoT Core | $0 | $0 | $3,000* |
| Lambda | $0 | $0 | $2,000* |
| **Total** | **~$64** | **~$21** | **~$76,200*** |

*Figures from actual AWS bill across 80 sites.

### At 80 sites

| | Edge Parquet | InfluxDB (actual) | Saving |
|--|--|--|--|
| EC2 (shared subscriber) | $20 | $36,000 | $35,980 |
| EBS/EFS | $0 | $10,000 | $10,000 |
| S3 (80 sites) | $800 | $3,000 | $2,200 |
| Data transfer | $28 | $3,600 | $3,572 |
| IoT Core | $0 | $3,000 | $3,000 |
| Lambda | $0 | $2,000 | $2,000 |
| CloudWatch | $10 | $2,600 | $2,590 |
| **Total** | **~$860/month** | **~$76,200/month** | **~$75,340/month** |

> Note: The Subscriber API on AWS is stateless and shared across all sites.
> EC2 cost does not scale with site count — one `t3.small` serves all 80 sites.

---

## Site Controller Requirements

The existing spark-22b6 stack is the reference implementation. Minimum hardware
for a production site controller:

| Resource | Minimum | Notes |
|----------|---------|-------|
| CPU | 2 cores | NATS + bridge + writer comfortably fit |
| RAM | 4 GB | NATS buffer + parquet writer in-memory batch |
| Disk | 20 GB | Local parquet queue (handles 24h offline) |
| Network | 1 Mbit/s | ~17 GB/day upload ≈ 1.6 Mbit/s average |

Suitable hardware: Raspberry Pi 5, industrial mini-PC, existing site SCADA server.

---

## Offline Resilience

| Scenario | Current cloud pipeline | Edge parquet |
|----------|----------------------|--------------|
| WAN link drops | **Data loss** — IoT Core drops messages | **No data loss** — parquet queues locally |
| AWS outage | **Total failure** | Site continues, uploads resume on recovery |
| Site controller restart | Replay from NATS (if stream retained) | Replay from NATS (local JetStream) |

---

## Migration Path

The edge architecture is a pure extension of the current proof-of-concept:

1. **Phase 1** — Deploy current spark-22b6 stack to 3–4 troubled sites (cloud pipeline,
   no site controller changes). Validates NATS→S3 in production. ~$22k/month saving.

2. **Phase 2** — Add local NATS + parquet writer to site controllers. Switch S3 upload
   to originate from site rather than AWS. Add NATS Leaf Node config. No AWS code changes.
   Additional ~$14/site/month saving on data transfer + EC2 reduction.

3. **Phase 3** — Full fleet rollout. AWS reduced to S3 + single Subscriber API instance.

---

## Key Files

| File | Purpose |
|------|---------|
| `source/parquet_writer/writer.py` | Parquet writer — runs unchanged on edge |
| `source/nats_bridge/bridge.py` | NATS bridge — runs unchanged on edge |
| `subscriber/api/server.py` | Subscriber API — runs on AWS, unchanged |
| `source/parquet_writer/config.yaml` | Writer config — change `endpoint_url` to real S3 |
| `subscriber/api/config.yaml` | Subscriber config — change `endpoint_url` to real S3 |
