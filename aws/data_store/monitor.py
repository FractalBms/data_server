"""
S3 / MinIO monitor — polls the bucket and reports newly arrived Parquet files.
Simulates what would happen on the AWS side as files land from the source.

Usage:
  pip install boto3 pyarrow pyyaml
  python monitor.py
  python monitor.py --config config.yaml --interval 10
"""

import argparse
import logging
import time
from datetime import datetime, timezone

import boto3
import pyarrow.parquet as pq
import yaml
from botocore.exceptions import ClientError
from io import BytesIO

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


def build_s3_client(cfg: dict):
    s3_cfg = cfg["s3"]
    kwargs = dict(
        region_name           = s3_cfg.get("region", "us-east-1"),
        aws_access_key_id     = s3_cfg.get("access_key"),
        aws_secret_access_key = s3_cfg.get("secret_key"),
    )
    if s3_cfg.get("endpoint_url"):
        kwargs["endpoint_url"] = s3_cfg["endpoint_url"]
    return boto3.client("s3", **kwargs)


def list_keys(s3, bucket: str, prefix: str) -> list[dict]:
    keys = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.append({"key": obj["Key"], "size": obj["Size"], "last_modified": obj["LastModified"]})
    return keys


def peek_parquet(s3, bucket: str, key: str, max_rows: int = 3) -> dict:
    """Download a Parquet file and return basic metadata + sample rows."""
    try:
        resp = s3.get_object(Bucket=bucket, Key=key)
        data = resp["Body"].read()
        table = pq.read_table(BytesIO(data))
        return {
            "rows":    table.num_rows,
            "columns": table.schema.names,
            "sample":  table.slice(0, max_rows).to_pydict(),
        }
    except Exception as exc:
        return {"error": str(exc)}


def run(cfg: dict, interval: int) -> None:
    bucket  = cfg["s3"]["bucket"]
    prefix  = cfg["s3"].get("prefix", "")
    s3      = build_s3_client(cfg)
    seen    = set()
    total_rows = 0
    total_files = 0

    log.info("Monitoring s3://%s/%s — polling every %ds", bucket, prefix, interval)
    log.info("MinIO console: http://localhost:9001  (user: minioadmin / minioadmin)")

    while True:
        try:
            objects = list_keys(s3, bucket, prefix)
        except ClientError as exc:
            log.warning("Could not list bucket: %s — retrying", exc)
            time.sleep(interval)
            continue

        new_keys = [o for o in objects if o["key"] not in seen]

        if new_keys:
            for obj in new_keys:
                seen.add(obj["key"])
                total_files += 1
                meta = peek_parquet(s3, bucket, obj["key"])
                rows = meta.get("rows", "?")
                if isinstance(rows, int):
                    total_rows += rows
                cols = ", ".join(meta.get("columns", []))
                log.info(
                    "NEW FILE  %s  (%d bytes, %s rows)",
                    obj["key"], obj["size"], rows,
                )
                log.info("  columns: %s", cols)
                if "sample" in meta:
                    # Show first row as a flat summary
                    sample = meta["sample"]
                    first = {k: v[0] for k, v in sample.items() if v}
                    log.info("  sample:  %s", first)

            log.info(
                "── Totals: %d files, %d rows in bucket ──",
                total_files, total_rows,
            )
        else:
            log.debug("No new files (total in bucket: %d)", len(objects))

        time.sleep(interval)


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="S3/MinIO Parquet file monitor")
    parser.add_argument("--config",   default="config.yaml")
    parser.add_argument("--interval", type=int, default=10, help="Poll interval seconds")
    args = parser.parse_args()
    run(load_config(args.config), args.interval)
