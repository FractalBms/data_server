# AWS — Infrastructure & Cost Reduction

Changes required on the AWS side to support the new architecture and achieve ~$40k/month cost savings.

## Local Simulation (data_store/)

The `data_store/` directory simulates the AWS S3 side using **MinIO** — an S3-compatible object store that runs as a single binary, no Docker required.

### Install MinIO

```bash
cd aws/data_store
wget https://dl.min.io/server/minio/release/linux-amd64/minio
chmod +x minio
```

### Start MinIO

```bash
./minio server ./minio-data --console-address :9001
```

- S3 API:      http://localhost:9000
- Web console: http://localhost:9001  (minioadmin / minioadmin)

Data is stored in `./minio-data/` — delete this directory to reset.

### Install MQTT broker (Mosquitto)

For local testing, Mosquitto works identically to FlashMQ:

```bash
sudo apt install mosquitto
```

**FlashMQ** (production broker) is also free and open source (AGPL v3):
```bash
sudo apt install flashmq    # Ubuntu/Debian PPA
```

### Start the file monitor

Watches the bucket and logs every new Parquet file as it arrives:

```bash
pip install boto3 pyarrow pyyaml
python monitor.py
```

### Full local transfer pipeline (no Docker)

```
Terminal 1 — MQTT broker:     mosquitto -p 1883
Terminal 2 — MinIO (S3):      cd aws/data_store && ./minio server ./minio-data --console-address :9001
Terminal 3 — S3 monitor:      cd aws/data_store && python monitor.py
Terminal 4 — Parquet writer:  cd source/parquet_writer && python writer.py
Terminal 5 — Generator:       cd source/generator && python generator.py
                               → open html/generator.html and click Start
```

Data flows: `generator → Mosquitto/FlashMQ → parquet_writer → MinIO → monitor`

## What Needs to Be Done

### 1. S3 Bucket Setup
- Create bucket for Parquet storage (e.g. `battery-data-parquet`)
- Enable versioning (optional) and server-side encryption
- Set lifecycle policies:
  - Transition to S3 Infrequent Access after 30 days
  - Transition to Glacier after 90 days (if long retention needed)
  - Confirm retention requirements with stakeholders

### 2. IAM
- Create IAM role for Parquet Writer Service (EC2 instance role)
  - `s3:PutObject` on the Parquet bucket
- Create IAM role/policy for Data Server API
  - `s3:GetObject`, `s3:ListBucket` on the Parquet bucket

### 3. IoT Core — Investigate & Eliminate (~$3k/month)
- Audit active connections and message counts in IoT Core console
- Identify any rules engine configurations
- Check whether any Lambda functions are IoT Core-triggered
- If confirmed redundant: disable rules, disconnect clients, decommission

### 4. CloudWatch — Reduce (~$1.6k/month saving)
- Audit all log groups — set retention policies (suggest 14–30 days)
- Identify high-resolution custom metrics and downgrade to standard where possible
- Consider routing application logs to S3 instead of CloudWatch Logs

### 5. Lambda — Right-size (~$1k/month saving)
- Review memory allocation vs actual usage for all functions
- Reduce over-provisioned memory allocations
- Identify and remove any functions that exist solely to serve IoT Core rules

### 6. InfluxDB 2 + EFS — Decommission (~$30-35k/month saving)
- Do not decommission until Data Server API is live and validated
- Once validated: snapshot InfluxDB 2 data to S3 for archival
- Terminate InfluxDB 2 EC2 instance
- Unmount and delete EFS filesystem
- Remove associated security groups and ENIs

## Estimated Savings by Action

| Action | Saving |
|---|---|
| IoT Core elimination | ~$3k/month |
| CloudWatch retention policies | ~$1.6k/month |
| Lambda right-sizing | ~$1k/month |
| InfluxDB 2 EC2 decommission | ~$20-25k/month |
| EFS decommission | ~$10k/month |
| **Total** | **~$36-41k/month** |

## Open Questions
- Confirm which AWS account/region hosts IoT Core, InfluxDB 2, and EFS
- Confirm InfluxDB 2 data retention requirements before decommission
- Are there any other services (e.g. Kinesis, MSK) that may be contributing to EC2/EFS costs?
