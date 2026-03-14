# InfluxDB2 — Setup & Reference

## Access

| Detail | Value |
|---|---|
| UI | http://localhost:8086 |
| Username | admin |
| Password | adminpassword123 |
| Organisation | battery-org |
| Bucket | battery-data |
| Admin token | battery-token-secret |
| Retention | 30 days |

---

## Sample Flux Queries

### Latest reading for a specific cell
```flux
from(bucket: "battery-data")
  |> range(start: -5m)
  |> filter(fn: (r) => r._measurement == "battery_cell")
  |> filter(fn: (r) => r.site_id == "0" and r.cell_id == "0")
  |> last()
```

### Average voltage per site over last hour
```flux
from(bucket: "battery-data")
  |> range(start: -1h)
  |> filter(fn: (r) => r._measurement == "battery_cell" and r._field == "voltage")
  |> group(columns: ["site_id"])
  |> mean()
```

### Cells with temperature > 40°C in last 10 minutes
```flux
from(bucket: "battery-data")
  |> range(start: -10m)
  |> filter(fn: (r) => r._measurement == "battery_cell" and r._field == "temperature")
  |> filter(fn: (r) => r._value > 40.0)
  |> group(columns: ["site_id", "rack_id", "cell_id"])
  |> last()
```

### Message rate per second (last 5 minutes)
```flux
from(bucket: "battery-data")
  |> range(start: -5m)
  |> filter(fn: (r) => r._measurement == "battery_cell" and r._field == "voltage")
  |> aggregateWindow(every: 1s, fn: count)
```

### SoC trend for all cells, downsampled to 1-minute averages
```flux
from(bucket: "battery-data")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "battery_cell" and r._field == "soc")
  |> aggregateWindow(every: 1m, fn: mean, createEmpty: false)
  |> yield(name: "soc_1m")
```

---

## AWS EFS Configuration (Production)

InfluxDB2 data is stored at `/var/lib/influxdb2` by default. On AWS, this is mounted
from EFS to allow cross-AZ access and independent scaling.

**Why this costs $10,000/month:**
- EFS Standard: $0.30/GB-month
- At 1s resolution across thousands of cells for 30-day retention:
  - ~3,000 cells × 8 fields × 1 point/sec × 30 days ≈ ~60 billion points
  - TSM storage engine compresses well but still reaches 30–40 TB
  - 35 TB × $0.30 = ~$10,500/month

**EFS mount (add to /etc/fstab on EC2):**
```
fs-XXXXXXXX.efs.us-east-1.amazonaws.com:/ /var/lib/influxdb2 efs defaults,_netdev,tls 0 0
```

**Alternative:** Use `gp3` EBS instead of EFS for ~10× lower cost if single-AZ is acceptable.
EBS `gp3` at 35 TB = ~$2,800/month vs EFS $10,500/month.

---

## Retention Policy

Default retention is 30 days (set at bucket creation).
To change via CLI:
```bash
influx bucket update --name battery-data --retention 90d
```

To add a downsampled bucket for long-term storage:
```bash
influx bucket create --name battery-data-1h --retention 365d
```
Then create a Flux task to downsample every hour:
```flux
option task = {name: "downsample-battery-1h", every: 1h}

from(bucket: "battery-data")
  |> range(start: -2h)
  |> filter(fn: (r) => r._measurement == "battery_cell")
  |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
  |> to(bucket: "battery-data-1h", org: "battery-org")
```
