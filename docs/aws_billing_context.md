# AWS Billing Context

## Monthly Bill Summary

| Service | Cost (pre-discount) |
|---|---|
| EC2 | $36,000 |
| EFS | $10,000 |
| Data Transfer | $3,600 |
| S3 | $3,000 |
| IoT | $3,000 |
| CloudWatch | $2,600 |
| Lambda | $2,000 |
| **Total** | **~$76,000** |

## Optimization Status

### Already Addressed
- EC2, EFS, S3, Data Transfer — reviewed/optimized

### Still To Optimize (~$7.6k potential)

**CloudWatch ($2,600) — flagged as high**
- Possible causes: high-resolution custom metrics, excessive log ingestion/retention, dashboards/alarms
- Action: audit log groups for retention policies, check custom metric count, consider shipping logs to S3 instead

**IoT ($3,000)**
- Possible causes: IoT Core message volume, connection minutes, rules engine executions
- Action: review message frequency, connection patterns, and rules engine usage

**Lambda ($2,000)**
- Possible causes: high invocation count or overprovisioned memory
- Action: review memory allocation vs actual usage, check invocation frequency and duration
