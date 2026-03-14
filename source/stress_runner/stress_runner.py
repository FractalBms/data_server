"""
Stress runner — simulates multiple projects/sites publishing battery data 24/7.

Each (project, site) pair runs as its own asyncio Task with its own cell
simulation loop.  A single shared paho-mqtt client (loop_start mode) is used
for all publishes.  A WebSocket server broadcasts stats every second and
handles get_status requests.

Topic format:
  {topic_prefix}/project={project_id}/site={site_id}/rack={rack_id}/module={module_id}/cell={cell_id}

Payload format:
  {"timestamp": 1700000000.0, "project_id": 0, "site_id": 0,
   "rack_id": 1, "module_id": 2, "cell_id": 3,
   "voltage": 3.74, "current": 0.12, "temperature": 24.8,
   "soc": 79.1, "internal_resistance": 0.0102}

Usage:
  python stress_runner.py --config config.spark.yaml

WebSocket API (ws://localhost:8769):
  Client → Server:
    {"type": "get_status"}

  Server → Client (every second and on connect):
    {"type": "stats", "total_published": 12345, "mps": 288,
     "active_tasks": 4,
     "projects": [{"project_id": 0,
                   "sites": [{"site_id": 0, "cells": 144, "published": 6000}]}]}
"""

import argparse
import asyncio
import json
import logging
import random
import signal
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

import paho.mqtt.client as mqtt
import websockets
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cell simulation  (same random-walk logic as generator.py)
# ---------------------------------------------------------------------------

@dataclass
class MeasurementState:
    """Tracks the current simulated value for a single measurement on a single cell."""
    value: float
    min: float
    max: float
    drift: float = 0.01  # max random walk step per sample as fraction of range

    def step(self) -> float:
        self.value += random.uniform(-self.drift, self.drift) * (self.max - self.min)
        self.value = max(self.min, min(self.max, self.value))
        return round(self.value, 4)


@dataclass
class CellState:
    """Tracks simulated state for all measurements on a single cell."""
    project_id: int
    site_id: int
    rack_id: int
    module_id: int
    cell_id: int
    measurements: Dict[str, MeasurementState] = field(default_factory=dict)


def build_cells_for_site(project_id: int, site_cfg: dict, cell_data_cfg: dict) -> List[CellState]:
    """Build all CellState objects for one (project, site) pair."""
    site_id = site_cfg["site_id"]
    racks = site_cfg["racks"]
    modules_per_rack = site_cfg["modules_per_rack"]
    cells_per_module = site_cfg["cells_per_module"]

    cells: List[CellState] = []
    for rack in range(racks):
        for module in range(modules_per_rack):
            for cell in range(cells_per_module):
                state = CellState(
                    project_id=project_id,
                    site_id=site_id,
                    rack_id=rack,
                    module_id=module,
                    cell_id=cell,
                )
                for name, cfg in cell_data_cfg.items():
                    nominal = cfg.get("nominal", (cfg["min"] + cfg["max"]) / 2)
                    initial = nominal + random.uniform(-0.05, 0.05) * (cfg["max"] - cfg["min"])
                    initial = max(cfg["min"], min(cfg["max"], initial))
                    state.measurements[name] = MeasurementState(
                        value=initial, min=cfg["min"], max=cfg["max"]
                    )
                cells.append(state)

    return cells


# ---------------------------------------------------------------------------
# Shared global state
# ---------------------------------------------------------------------------

# Per (project_id, site_id) published-message counters; updated by site tasks
g_site_counters: Dict[tuple, int] = {}

# Total messages published across all tasks
g_total_published: int = 0

# Approximate messages-per-second (updated by stats broadcaster)
g_mps: int = 0

# Number of active publish tasks
g_active_tasks: int = 0

# Project/site metadata (set once at startup)
g_topology: List[dict] = []  # [{project_id, sites: [{site_id, cells}]}]

# Connected WebSocket clients
g_ws_clients: Set = set()

# Shared MQTT client (paho, loop_start mode)
g_mqtt_client: Optional[mqtt.Client] = None

# Global stop event — set on SIGINT/SIGTERM
g_stop: asyncio.Event


# ---------------------------------------------------------------------------
# MQTT helpers
# ---------------------------------------------------------------------------

def make_mqtt_client(mqtt_cfg: dict) -> mqtt.Client:
    client_id = mqtt_cfg.get("client_id", "stress-runner")
    client = mqtt.Client(client_id=client_id)
    username = mqtt_cfg.get("username", "")
    if username:
        client.username_pw_set(username, mqtt_cfg.get("password", ""))

    def on_connect(c, userdata, flags, rc):
        log.info("MQTT connected to %s:%d (rc=%d)", mqtt_cfg["host"], mqtt_cfg["port"], rc)

    def on_disconnect(c, userdata, rc):
        log.warning("MQTT disconnected (rc=%d) — paho will attempt reconnect", rc)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    return client


def connect_mqtt_with_backoff(client: mqtt.Client, host: str, port: int) -> None:
    """Connect synchronously; called once at startup before tasks launch."""
    delay = 1.0
    while True:
        try:
            client.connect(host, port, keepalive=60)
            client.loop_start()
            log.info("MQTT loop started")
            return
        except Exception as exc:
            log.warning("MQTT connect failed (%s) — retrying in %.0fs", exc, delay)
            time.sleep(delay)
            delay = min(delay * 2, 60.0)


# ---------------------------------------------------------------------------
# Per-(project, site) publish loop
# ---------------------------------------------------------------------------

async def site_publish_loop(
    project_id: int,
    site_cfg: dict,
    config: dict,
) -> None:
    global g_total_published, g_active_tasks

    site_id = site_cfg["site_id"]
    interval = config["sample_interval_seconds"]
    topic_prefix = config["topic_prefix"]
    cell_data_cfg = config["cell_data"]

    cells = build_cells_for_site(project_id, site_cfg, cell_data_cfg)
    key = (project_id, site_id)
    g_site_counters[key] = 0

    log.info(
        "Project %d site %d: %d cells, interval %.1fs",
        project_id, site_id, len(cells), interval,
    )

    g_active_tasks += 1
    try:
        while not g_stop.is_set():
            t_start = time.monotonic()

            ts = time.time()
            batch_count = 0

            for cell in cells:
                if g_stop.is_set():
                    break

                values = {name: state.step() for name, state in cell.measurements.items()}

                topic = (
                    f"{topic_prefix}"
                    f"/project={cell.project_id}"
                    f"/site={cell.site_id}"
                    f"/rack={cell.rack_id}"
                    f"/module={cell.module_id}"
                    f"/cell={cell.cell_id}"
                )
                payload = {
                    "timestamp": ts,
                    "project_id": cell.project_id,
                    "site_id": cell.site_id,
                    "rack_id": cell.rack_id,
                    "module_id": cell.module_id,
                    "cell_id": cell.cell_id,
                    **values,
                }
                g_mqtt_client.publish(topic, json.dumps(payload), qos=1)
                batch_count += 1

            g_site_counters[key] = g_site_counters.get(key, 0) + batch_count
            g_total_published += batch_count

            elapsed = time.monotonic() - t_start
            sleep_for = max(0.0, interval - elapsed)
            if elapsed > interval:
                log.warning(
                    "Project %d site %d: publish loop took %.3fs > interval %.1fs",
                    project_id, site_id, elapsed, interval,
                )

            # Yield to the event loop; use asyncio.sleep so stop event is checked
            await asyncio.sleep(sleep_for if sleep_for > 0 else 0)

    finally:
        g_active_tasks -= 1
        log.info("Project %d site %d: task exiting", project_id, site_id)


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

def build_stats_message() -> dict:
    projects_out = []
    for proj in g_topology:
        pid = proj["project_id"]
        sites_out = []
        for s in proj["sites"]:
            sid = s["site_id"]
            published = g_site_counters.get((pid, sid), 0)
            sites_out.append({
                "site_id": sid,
                "cells": s["cells"],
                "published": published,
            })
        projects_out.append({"project_id": pid, "sites": sites_out})

    return {
        "type": "stats",
        "total_published": g_total_published,
        "mps": g_mps,
        "active_tasks": g_active_tasks,
        "projects": projects_out,
    }


async def ws_handler(websocket) -> None:
    g_ws_clients.add(websocket)
    log.info("WebSocket client connected (%d total)", len(g_ws_clients))

    # Send current stats immediately on connect
    await websocket.send(json.dumps(build_stats_message()))

    try:
        async for raw in websocket:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if msg.get("type") == "get_status":
                await websocket.send(json.dumps(build_stats_message()))

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        g_ws_clients.discard(websocket)
        log.info("WebSocket client disconnected (%d remaining)", len(g_ws_clients))


async def stats_broadcaster() -> None:
    """Broadcasts stats to all connected WebSocket clients every second."""
    global g_mps

    last_total = 0
    last_ts = time.monotonic()

    while not g_stop.is_set():
        await asyncio.sleep(1.0)

        now = time.monotonic()
        delta_t = now - last_ts
        delta_msgs = g_total_published - last_total
        g_mps = round(delta_msgs / max(delta_t, 0.001))
        last_total = g_total_published
        last_ts = now

        if not g_ws_clients:
            continue

        msg = json.dumps(build_stats_message())
        await asyncio.gather(
            *[ws.send(msg) for ws in list(g_ws_clients)],
            return_exceptions=True,
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main_async(config: dict) -> None:
    global g_mqtt_client, g_stop, g_topology

    g_stop = asyncio.Event()

    # Register shutdown handlers
    loop = asyncio.get_event_loop()

    def _shutdown():
        log.info("Shutdown signal received — stopping tasks")
        g_stop.set()

    loop.add_signal_handler(signal.SIGINT,  _shutdown)
    loop.add_signal_handler(signal.SIGTERM, _shutdown)

    # Build topology metadata for stats reporting
    g_topology = []
    for proj_cfg in config["projects"]:
        pid = proj_cfg["project_id"]
        sites_meta = []
        for site_cfg in proj_cfg["sites"]:
            total_cells = (
                site_cfg["racks"]
                * site_cfg["modules_per_rack"]
                * site_cfg["cells_per_module"]
            )
            sites_meta.append({"site_id": site_cfg["site_id"], "cells": total_cells})
        g_topology.append({"project_id": pid, "sites": sites_meta})

    # Set up MQTT
    mqtt_cfg = config["mqtt"]
    g_mqtt_client = make_mqtt_client(mqtt_cfg)
    log.info("Connecting to MQTT broker %s:%d ...", mqtt_cfg["host"], mqtt_cfg["port"])
    connect_mqtt_with_backoff(g_mqtt_client, mqtt_cfg["host"], mqtt_cfg["port"])

    # Start WebSocket server
    ws_cfg = config.get("websocket", {})
    ws_host = ws_cfg.get("host", "0.0.0.0")
    ws_port = ws_cfg.get("port", 8769)
    log.info("WebSocket server listening on ws://%s:%d", ws_host, ws_port)

    # Launch all site tasks
    tasks: List[asyncio.Task] = []
    for proj_cfg in config["projects"]:
        pid = proj_cfg["project_id"]
        for site_cfg in proj_cfg["sites"]:
            task = asyncio.create_task(
                site_publish_loop(pid, site_cfg, config),
                name=f"project={pid}/site={site_cfg['site_id']}",
            )
            tasks.append(task)

    log.info("Launched %d site task(s)", len(tasks))

    async with websockets.serve(ws_handler, ws_host, ws_port):
        broadcaster = asyncio.create_task(stats_broadcaster())
        # Wait until all site tasks finish (they exit when g_stop is set)
        await asyncio.gather(*tasks, return_exceptions=True)
        broadcaster.cancel()
        try:
            await broadcaster
        except asyncio.CancelledError:
            pass

    g_mqtt_client.loop_stop()
    g_mqtt_client.disconnect()
    log.info("Shutdown complete")


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Battery telemetry stress runner")
    parser.add_argument(
        "--config", default="config.spark.yaml",
        help="Path to YAML config file (default: config.spark.yaml)",
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    asyncio.run(main_async(cfg))
