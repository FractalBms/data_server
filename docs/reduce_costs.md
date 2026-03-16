# Cost Reduction Analysis: NATS + DuckDB Proposal

## Fleet Overview

Production fleet: 6 namespaces (sbess1–4, vc54, vc85), each running an identical HMI v2 stack.

Pricing basis: AWS us-east-1 on-demand, m5 family — $35/vCPU/mo, $8.76/GiB/mo, 1.5× node overhead factor. **Compute only** — excludes EBS, data transfer, EKS control plane.

---

## Current Costs by Service (all 6 namespaces combined)

| Service | CPU total | RAM total | Monthly est. | NATS+DuckDB impact |
|---------|----------:|----------:|-------------:|-------------------|
| telegraf | 12,751m | 3.7 GiB | $718 | Reduce ~50% — push replaces scrape polling |
| influxdb | 4,414m | 15.7 GiB | $438 | **Replace** — DuckDB/Parquet |
| v1-alert-converter | 3,723m | 15.3 GiB | $397 | Reduce ~50% — NATS routing |
| telegraf-secondary | 3,099m | 9.5 GiB | $288 | **Eliminate** — DuckDB handles secondary path |
| flashmq (internal) | 2,243m | 5.5 GiB | $190 | **Replace** — NATS JetStream |
| v1-topic-converter | 2,966m | 2.5 GiB | $189 | Reduce ~50% — NATS subjects replace topic mapping |
| postgresql | 21m | 10.3 GiB | $137 | — |
| alert-service | 873m | 2.5 GiB | $78 | — |
| keycloak | 10m | 5.8 GiB | $77 | — |
| mongodb | 179m | 4.6 GiB | $69 | — |
| flashmq-customer | 511m | 1.4 GiB | $45 | **Replace** — NATS JetStream |
| api, frontend, traefik, etc. | — | — | ~$60 | — |
| **TOTAL** | **31,019m** | **80.4 GiB** | **~$2,685/mo** | |

---

## NATS + DuckDB Savings Projection

The six targeted services account for **$2,265/mo — 84% of fleet compute spend**.

| Action | Monthly saving |
|--------|---------------:|
| Replace InfluxDB → DuckDB/Parquet (full) | $438 |
| Eliminate telegraf-secondary | $288 |
| Reduce telegraf 50% (push vs poll) | $359 |
| Replace flashmq internal + customer → NATS JetStream | $235 |
| Reduce v1-alert-converter 50% (NATS routing) | $198 |
| Reduce v1-topic-converter 50% (NATS subjects) | $95 |
| **Total projected saving** | **~$1,613/mo** |

**Current: ~$2,685/mo → Projected: ~$1,072/mo. ~60% compute reduction.**

EBS storage savings from replacing InfluxDB with Parquet are not included above — time-series data in Parquet compresses 5–10× better than InfluxDB TSM files, so the actual saving will be higher.

---

## Why Each Service Is Expensive

### telegraf — $718/mo, 12,751m CPU

Telegraf is a **protocol bridge** — one job, one data path:

```
FlashMQ (MQTT broker)
  └─ batteries/#  (every battery topic, QoS 1)
      └─ Telegraf (mqtt_consumer → json_v2 parser)
          └─ InfluxDB v2 (influxdb_v2 output)
```

It subscribes to every battery MQTT topic, parses the JSON payload (voltage, current, temp, SoC, SoH, resistance, capacity, power), and writes line-protocol records to InfluxDB. No aggregation, filtering, or transformation — just format conversion.

**Why it's expensive:**

- **1-second interval, continuous flush** — `interval = "1s"`, `flush_interval = "10s"`, batch size 1,000. At battery-cell granularity across 6 sites (site → rack → module → cell), this is thousands of MQTT messages/second being parsed and written continuously.
- **Per-message CPU scales with fleet size** — every message is deserialized from JSON and re-serialised to InfluxDB line protocol. More cells = proportionally more CPU.
- **InfluxDB write amplification** — constant batch writes drive InfluxDB's TSM write buffer, which is a significant contributor to InfluxDB's own $438/mo RAM cost.
- **Large in-memory buffer** — `metric_buffer_limit = 100,000` keeps up to 100k points in RAM to absorb bursts, adding baseline memory pressure per pod.

**Why NATS eliminates this:**

Devices publish directly to NATS subjects. A single lightweight subscriber (~50 lines Python/Go) writes to Parquet/DuckDB — no JSON→line-protocol conversion, no secondary write path, no large in-flight buffer. The `telegraf-secondary` instance (parallel writes to a second retention tier) is fully eliminated since DuckDB handles retention via a single configurable query.

### influxdb — $438/mo, 15.7 GiB RAM
InfluxDB holds a hot working set in memory for recent data and keeps indexes in RAM. DuckDB with columnar Parquet storage uses far lower working-set memory for the same analytical queries, and Parquet files on S3 replace expensive EBS-backed TSM storage.

### v1-alert-converter — $397/mo, 15.3 GiB RAM
Single pod per site doing high-throughput stateful message translation. RAM is dominated by in-flight alert state. NATS subject-per-device routing removes the fan-out translation entirely for new-format devices; the converter only needs to handle true v1 legacy traffic.

### telegraf-secondary — $288/mo, 9.5 GiB RAM
A secondary write path, presumably for a different retention tier or downstream consumer. Zero CPU on sbess1/3/4 (idle/standby), near-zero on sbess4 — paying 9.5 GiB RAM to keep state warm for a rarely-used path. DuckDB replaces this with a single configurable retention query.

### flashmq (internal + customer) — $235/mo combined
Two separate MQTT broker deployments (internal and customer-facing). NATS JetStream consolidates both into one cluster with subject-based tenant isolation, eliminating the per-site broker overhead and simplifying operations.

### v1-topic-converter — $189/mo, scales with message volume
Replica count varies by site load (4–8 pods). NATS subjects absorb the fan-out routing that these converters perform, scaling down replica count significantly.

---

## Site-Level Observations

### sbess3 is the busiest site
telegraf at 3,024m, influxdb at 1,356m / 4.5 GiB, alert-converter at 1,405m / 3.9 GiB — consistently the highest resource user across all services. Good pilot candidate: validates savings at peak load before fleet-wide rollout.

### sbess4 is the lightest site
alert-converter at only 76m / 1.1 GiB (vs sbess3's 1,405m / 3.9 GiB). Could be used as the low-risk first migration target.

### postgresql memory anomaly (sbess1)
sbess1 postgresql holds 6,476 MiB vs sbess2's 263 MiB and vc54's 58 MiB. Not a NATS/DuckDB concern but warrants investigation — possible connection pool leak or autovacuum backlog.

### telegraf-secondary memory anomaly
9.5 GiB RAM fleet-wide with near-zero CPU on several sites (sbess1: 1m, sbess3: 1m, sbess4: 1m). Something is holding large state without doing work. Worth investigating before migration regardless.

---

## Summary

| Scenario | Monthly | Annual |
|----------|--------:|-------:|
| Current (compute only) | $2,685 | $32,220 |
| Post-migration (compute only) | ~$1,072 | ~$12,864 |
| **Saving** | **~$1,613** | **~$19,360** |

These numbers cover compute costs only. The full picture including EBS volumes, data transfer, and EKS control plane fees would increase both the baseline and the projected savings — particularly the InfluxDB → Parquet transition, which significantly reduces storage I/O costs.

---

## Telegraf → NATS Migration: Minimum-Risk Path

telegraf can be replaced incrementally — no flag-day cutover required.

### Phase 1 — Dual output (zero risk)
telegraf has a native `[[outputs.nats]]` plugin. Add it alongside the existing `[[outputs.influxdb]]`. telegraf writes to both simultaneously. Nothing changes operationally; you're just tapping the stream.

### Phase 2 — DuckDB consumer (parallel writes)
A lightweight NATS subscriber (~50 lines Python/Go) writes incoming metrics to Parquet. Both InfluxDB and DuckDB now receive the same data. Validate query parity without any production risk.

### Phase 3 — Reduce telegraf scrape interval
Once DuckDB is the analytics source of truth, increase telegraf's `interval` (e.g. 10s → 60s). CPU drops proportionally, InfluxDB write volume drops 6×. Pod count can be reduced immediately — savings are realised before full migration.

### Phase 4 — Switch dashboards and alerting to DuckDB ⚠️
Redirect Grafana (or whatever queries InfluxDB) to DuckDB. Once no queries are hitting InfluxDB, decommission it.

> **Pending audit required before this step.** The critical question is whether existing dashboards and alert rules use Flux or InfluxQL — if so, those queries need rewriting for DuckDB SQL. This audit has not been done yet. See open question below.

### Phase 5 — Remove telegraf (optional)
If metric sources can push directly via NATS (most modern exporters can), telegraf becomes unnecessary. For legacy sources that require scraping, a minimal telegraf with only `[[outputs.nats]]` is much lighter than the current configuration.

---

## Open Questions

### ⚠️ What is querying InfluxDB?
Before Phase 4 can proceed, we need a codebase audit of everything that queries InfluxDB directly — dashboards, alerting rules, any service making HTTP calls to the InfluxDB API. If consumers depend on Flux or InfluxQL, those queries must be rewritten for DuckDB SQL before InfluxDB can be decommissioned. This is the single highest-risk dependency in the migration.
