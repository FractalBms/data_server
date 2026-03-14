"""
Subscriber API — WebSocket server for battery telemetry.

Real-time: plain NATS core subscription on batteries.> broadcasts every
           incoming message to all connected WebSocket clients instantly.
History:   client sends a query_history request; DuckDB reads the
           partitioned Parquet files directly from S3/MinIO and returns
           the result set.

Wire protocol (all JSON):

  Client → Server
    {"type":"query_history","query_id":"1","site_id":"0",
     "from_ts":1234567890.0,"to_ts":1234567891.0,"limit":2000}

  Server → Client
    {"type":"live",    "data":{flat battery record}}
    {"type":"history", "query_id":"1","columns":[...],"rows":[...],
                       "total":N,"elapsed_ms":N}
    {"type":"error",   "query_id":"1","message":"..."}

Usage:
  pip install nats-py duckdb websockets pyyaml
  python subscriber.py
  python subscriber.py --config config.yaml
"""

import argparse
import asyncio
import json
import logging
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import duckdb
import nats
import websockets
import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# All connected WebSocket clients
_clients: set = set()
_executor = ThreadPoolExecutor(max_workers=4)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _flatten(row: dict) -> dict:
    """Flatten {value, unit} measurement objects to plain scalars."""
    return {
        k: (v["value"] if isinstance(v, dict) and "value" in v else v)
        for k, v in row.items()
    }


def _hour_partitions(from_ts: float, to_ts: float):
    """Yield (date_str, hour_str) for every UTC hour bucket in [from_ts, to_ts]."""
    dt = datetime.fromtimestamp(from_ts, tz=timezone.utc).replace(
        minute=0, second=0, microsecond=0
    )
    end = datetime.fromtimestamp(to_ts, tz=timezone.utc)
    while dt <= end:
        yield dt.strftime("%Y-%m-%d"), dt.strftime("%H")
        dt += timedelta(hours=1)


# ---------------------------------------------------------------------------
# DuckDB / S3  (called in thread executor — one connection per query)
# ---------------------------------------------------------------------------

def _make_conn(cfg: dict) -> duckdb.DuckDBPyConnection:
    s3 = cfg["s3"]
    conn = duckdb.connect()
    conn.execute("INSTALL httpfs; LOAD httpfs;")
    if s3.get("endpoint_url"):
        netloc = urlparse(s3["endpoint_url"]).netloc
        conn.execute(f"SET s3_endpoint='{netloc}';")
        conn.execute("SET s3_use_ssl=false;")
        conn.execute("SET s3_url_style='path';")
    conn.execute(f"SET s3_region='{s3.get('region', 'us-east-1')}';")
    if s3.get("access_key"):
        conn.execute(f"SET s3_access_key_id='{s3['access_key']}';")
        conn.execute(f"SET s3_secret_access_key='{s3['secret_key']}';")
    return conn


def _query_history(cfg: dict, site_id: str,
                   from_ts: float, to_ts: float, limit: int):
    """Query S3 Parquet files for a site/time-range. Returns (columns, rows)."""
    conn = _make_conn(cfg)
    bucket = cfg["s3"]["bucket"]
    prefix = cfg["s3"].get("prefix", "").strip("/")
    base   = "s3://{}/{}{}/site={}".format(
        bucket,
        prefix + "/" if prefix else "",
        "",          # no extra segment when prefix is empty
        site_id,
    )
    # Fix double-slash when prefix is empty
    base = f"s3://{bucket}/" + (f"{prefix}/" if prefix else "") + f"site={site_id}"

    # Target only the specific hour partitions that overlap the query window
    globs = [
        f"{base}/date={d}/hour={h}/*.parquet"
        for d, h in _hour_partitions(from_ts, to_ts)
    ]
    sources = ", ".join(f"'{g}'" for g in globs)

    try:
        result = conn.execute(f"""
            SELECT * FROM read_parquet([{sources}], union_by_name=true)
            WHERE timestamp >= {from_ts} AND timestamp <= {to_ts}
            ORDER BY timestamp
            LIMIT {limit}
        """)
        cols = [d[0] for d in result.description]
        rows = result.fetchall()
    except Exception as exc:
        # Some hour partitions may not exist — fall back to full-site glob
        log.warning("Partition query failed (%s) — falling back to site glob", exc)
        try:
            result = conn.execute(f"""
                SELECT * FROM read_parquet('{base}/date=*/hour=*/*.parquet',
                                           union_by_name=true)
                WHERE timestamp >= {from_ts} AND timestamp <= {to_ts}
                ORDER BY timestamp
                LIMIT {limit}
            """)
            cols = [d[0] for d in result.description]
            rows = result.fetchall()
        except Exception as fallback_exc:
            conn.close()
            raise RuntimeError(str(fallback_exc)) from fallback_exc

    conn.close()
    return cols, rows


# ---------------------------------------------------------------------------
# WebSocket handler
# ---------------------------------------------------------------------------

async def _broadcast(msg: dict) -> None:
    if not _clients:
        return
    text = json.dumps(msg, default=str)
    await asyncio.gather(
        *(c.send(text) for c in list(_clients)),
        return_exceptions=True,
    )


async def _ws_handler(websocket, cfg: dict) -> None:
    _clients.add(websocket)
    log.info("Client connected %s  (total: %d)", websocket.remote_address, len(_clients))
    try:
        async for raw in websocket:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if msg.get("type") != "query_history":
                continue

            query_id = str(msg.get("query_id", ""))
            site_id  = str(msg.get("site_id",  "0"))
            from_ts  = float(msg.get("from_ts", time.time() - 300))
            to_ts    = float(msg.get("to_ts",   time.time()))
            limit    = int(msg.get("limit",   2000))

            t0 = time.monotonic()
            try:
                loop = asyncio.get_running_loop()
                cols, rows = await loop.run_in_executor(
                    _executor, _query_history, cfg, site_id, from_ts, to_ts, limit
                )
                elapsed = int((time.monotonic() - t0) * 1000)
                await websocket.send(json.dumps({
                    "type":       "history",
                    "query_id":   query_id,
                    "columns":    cols,
                    "rows":       [list(r) for r in rows],
                    "total":      len(rows),
                    "elapsed_ms": elapsed,
                }, default=str))
                log.info("History  site=%s  rows=%d  %dms", site_id, len(rows), elapsed)
            except Exception as exc:
                await websocket.send(json.dumps({
                    "type":     "error",
                    "query_id": query_id,
                    "message":  str(exc),
                }))
                log.error("Query failed: %s", exc)

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        _clients.discard(websocket)
        log.info("Client disconnected (total: %d)", len(_clients))


# ---------------------------------------------------------------------------
# NATS live subscription  (plain core sub — no JetStream consumer overhead)
# ---------------------------------------------------------------------------

async def _nats_subscriber(cfg: dict) -> None:
    url  = cfg["nats"]["url"]
    subj = cfg["nats"].get("subject_filter", "batteries.>")

    while True:
        try:
            nc = await nats.connect(url)
            log.info("NATS connected: %s", url)

            async def on_msg(msg):
                try:
                    payload = _flatten(json.loads(msg.data))
                    await _broadcast({"type": "live", "subject": msg.subject, "payload": payload})
                except Exception:
                    pass

            await nc.subscribe(subj, cb=on_msg)
            log.info("Live subscription active on '%s'", subj)

            while nc.is_connected:
                await asyncio.sleep(1)
            await nc.drain()

        except Exception as exc:
            log.warning("NATS error (%s) — reconnecting in 5s", exc)
            await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run(cfg: dict) -> None:
    host = cfg["server"].get("host", "0.0.0.0")
    port = cfg["server"].get("port", 8767)

    asyncio.create_task(_nats_subscriber(cfg))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT,  stop.set)
    loop.add_signal_handler(signal.SIGTERM, stop.set)

    async def handler(websocket):
        await _ws_handler(websocket, cfg)

    async with websockets.serve(handler, host, port):
        log.info("Subscriber API  ws://%s:%d", host, port)
        await stop.wait()

    log.info("Shutdown complete")


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Battery Subscriber API")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    asyncio.run(run(load_config(args.config)))
