"""
Battery data generator — publishes simulated cell data to an MQTT broker.
Exposes a WebSocket server for browser-based control and live data viewing.

Topic modes (set in config.yaml):
  per_cell       — one topic per cell, JSON payload with all measurements
  per_cell_item  — one topic per measurement per cell, JSON payload with single value

Usage:
  python generator.py
  python generator.py --config path/to/config.yaml

WebSocket API (ws://localhost:8765):
  Client → Server:
    {"type": "start"}
    {"type": "stop"}
    {"type": "update_config", "config": {"sites": 2, "racks_per_site": 3, ...}}
    {"type": "get_status"}

  Server → Client:
    {"type": "status",  "running": bool, "config": {...}, "cell_count": int}
    {"type": "stats",   "total_published": int, "mps": int, "last_loop_ms": float}
    {"type": "samples", "samples": [...]}
"""

import argparse
import asyncio
import json
import logging
import random
import time
from collections import deque
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
# Cell simulation
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
    site_id: int
    rack_id: int
    module_id: int
    cell_id: int
    measurements: Dict[str, MeasurementState] = field(default_factory=dict)

    @property
    def cell_path(self) -> str:
        return f"site={self.site_id}/rack={self.rack_id}/module={self.module_id}/cell={self.cell_id}"


def build_cells(config: dict) -> List[CellState]:
    cells = []
    for site in range(config["sites"]):
        for rack in range(config["racks_per_site"]):
            for module in range(config["modules_per_rack"]):
                for cell in range(config["cells_per_module"]):
                    state = CellState(site_id=site, rack_id=rack, module_id=module, cell_id=cell)
                    for name, cfg in config["cell_data"].items():
                        nominal = cfg.get("nominal", (cfg["min"] + cfg["max"]) / 2)
                        initial = nominal + random.uniform(-0.05, 0.05) * (cfg["max"] - cfg["min"])
                        initial = max(cfg["min"], min(cfg["max"], initial))
                        state.measurements[name] = MeasurementState(
                            value=initial, min=cfg["min"], max=cfg["max"]
                        )
                    cells.append(state)

    total = len(cells)
    log.info(
        "Built %d sites × %d racks × %d modules × %d cells = %d cells",
        config["sites"], config["racks_per_site"],
        config["modules_per_rack"], config["cells_per_module"], total,
    )
    return cells


def publish_cell(client: mqtt.Client, cell: CellState, config: dict) -> int:
    """Publish one sample for a cell. Returns number of messages published."""
    prefix = config["topic_prefix"]
    mode = config["topic_mode"]
    measurements_cfg = config["cell_data"]
    timestamp = time.time()

    values = {name: state.step() for name, state in cell.measurements.items()}

    if mode == "per_cell":
        topic = f"{prefix}/{cell.cell_path}"
        payload = {
            "timestamp": timestamp,
            "site_id": cell.site_id,
            "rack_id": cell.rack_id,
            "module_id": cell.module_id,
            "cell_id": cell.cell_id,
            **{
                name: {"value": val, "unit": measurements_cfg[name]["unit"]}
                for name, val in values.items()
            },
        }
        client.publish(topic, json.dumps(payload), qos=1)
        return 1

    elif mode == "per_cell_item":
        for name, val in values.items():
            topic = f"{prefix}/{cell.cell_path}/{name}"
            payload = {"timestamp": timestamp, "value": val, "unit": measurements_cfg[name]["unit"]}
            client.publish(topic, json.dumps(payload), qos=1)
        return len(values)

    raise ValueError(f"Unknown topic_mode: {mode!r}. Use 'per_cell' or 'per_cell_item'.")


# ---------------------------------------------------------------------------
# Global state shared between the generator loop and WebSocket handlers
# ---------------------------------------------------------------------------

g_running: bool = False
g_cells: List[CellState] = []
g_config: dict = {}
g_stats: dict = {"total_published": 0, "mps": 0, "last_loop_ms": 0.0}
g_connected: Set = set()
g_mqtt_client: Optional[mqtt.Client] = None


def public_config() -> dict:
    return {
        "sites":                  g_config["sites"],
        "racks_per_site":         g_config["racks_per_site"],
        "modules_per_rack":       g_config["modules_per_rack"],
        "cells_per_module":       g_config["cells_per_module"],
        "sample_interval_seconds": g_config["sample_interval_seconds"],
        "topic_mode":             g_config["topic_mode"],
    }


async def broadcast(message: dict) -> None:
    if not g_connected:
        return
    data = json.dumps(message)
    await asyncio.gather(
        *[ws.send(data) for ws in list(g_connected)],
        return_exceptions=True,
    )


# ---------------------------------------------------------------------------
# WebSocket handler
# ---------------------------------------------------------------------------

async def ws_handler(websocket) -> None:
    global g_running, g_cells, g_config

    g_connected.add(websocket)
    log.info("WebSocket client connected (%d total)", len(g_connected))

    # Send current state immediately on connect
    await websocket.send(json.dumps({
        "type": "status",
        "running": g_running,
        "config": public_config(),
        "cell_count": len(g_cells),
    }))
    await websocket.send(json.dumps({"type": "stats", **g_stats}))

    try:
        async for raw in websocket:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            t = msg.get("type")

            if t == "start":
                g_running = True
                log.info("Generation started via WebSocket")
                await broadcast({"type": "status", "running": True, "config": public_config(), "cell_count": len(g_cells)})

            elif t == "stop":
                g_running = False
                log.info("Generation stopped via WebSocket")
                await broadcast({"type": "status", "running": False, "config": public_config(), "cell_count": len(g_cells)})

            elif t == "update_config":
                updates = msg.get("config", {})
                g_config.update(updates)
                g_cells = build_cells(g_config)
                log.info("Config updated: %s — %d cells", updates, len(g_cells))
                await broadcast({"type": "status", "running": g_running, "config": public_config(), "cell_count": len(g_cells)})

            elif t == "get_status":
                await websocket.send(json.dumps({
                    "type": "status",
                    "running": g_running,
                    "config": public_config(),
                    "cell_count": len(g_cells),
                }))

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        g_connected.discard(websocket)
        log.info("WebSocket client disconnected (%d remaining)", len(g_connected))


# ---------------------------------------------------------------------------
# Generator loop
# ---------------------------------------------------------------------------

async def generator_loop() -> None:
    global g_stats

    while True:
        interval = g_config.get("sample_interval_seconds", 1)

        if g_running and g_cells and g_mqtt_client:
            t_start = time.monotonic()

            total = 0
            for cell in g_cells:
                total += publish_cell(g_mqtt_client, cell, g_config)

            elapsed = time.monotonic() - t_start

            g_stats = {
                "total_published": g_stats["total_published"] + total,
                "mps": round(total / max(elapsed, 0.001)),
                "last_loop_ms": round(elapsed * 1000, 1),
            }

            # Broadcast stats
            await broadcast({"type": "stats", **g_stats})

            # Broadcast a sample of up to 10 cells for display
            sample_cells = random.sample(g_cells, min(10, len(g_cells)))
            samples = [
                {
                    "cell_path": c.cell_path,
                    "timestamp": round(time.time(), 3),
                    "measurements": {
                        name: {"value": round(s.value, 4), "unit": g_config["cell_data"][name]["unit"]}
                        for name, s in c.measurements.items()
                    },
                }
                for c in sample_cells
            ]
            await broadcast({"type": "samples", "samples": samples})

            if elapsed > interval:
                log.warning("Loop took %.3fs > interval %ds — reduce cell count", elapsed, interval)

            await asyncio.sleep(max(0.0, interval - elapsed))
        else:
            await asyncio.sleep(0.1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main_async(config: dict) -> None:
    global g_config, g_cells, g_mqtt_client

    g_config = config
    g_cells = build_cells(config)

    broker = config["broker"]
    g_mqtt_client = mqtt.Client(client_id=broker.get("client_id", "battery-generator"))
    if broker.get("username"):
        g_mqtt_client.username_pw_set(broker["username"], broker.get("password", ""))

    g_mqtt_client.on_connect = lambda c, u, f, rc: log.info(
        "MQTT connected to %s:%s (rc=%d)", broker["host"], broker["port"], rc
    )
    g_mqtt_client.on_disconnect = lambda c, u, rc: log.warning("MQTT disconnected (rc=%d)", rc)

    log.info("Connecting to MQTT broker %s:%s ...", broker["host"], broker["port"])
    g_mqtt_client.connect(broker["host"], broker["port"], keepalive=60)
    g_mqtt_client.loop_start()

    ws_cfg = config.get("websocket", {})
    ws_host = ws_cfg.get("host", "0.0.0.0")
    ws_port = ws_cfg.get("port", 8765)
    log.info("WebSocket server listening on ws://%s:%d", ws_host, ws_port)

    async with websockets.serve(ws_handler, ws_host, ws_port):
        await generator_loop()


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Battery MQTT data generator")
    parser.add_argument("--config", default="config.yaml", help="Path to config file")
    args = parser.parse_args()

    cfg = load_config(args.config)
    try:
        asyncio.run(main_async(cfg))
    except KeyboardInterrupt:
        log.info("Shutting down")
        if g_mqtt_client:
            g_mqtt_client.loop_stop()
            g_mqtt_client.disconnect()
