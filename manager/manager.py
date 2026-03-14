"""
Data Server Process Manager — starts, stops, and monitors all pipeline
components, streaming their log output to a browser dashboard via WebSocket.

WebSocket API (ws://localhost:8760):

  Client → Server:
    {"type": "get_status"}
    {"type": "start",     "component": "generator"}
    {"type": "stop",      "component": "generator"}
    {"type": "start_all"}
    {"type": "stop_all"}

  Server → Client:
    {"type": "status",  "components": {"name": {"status": "running"|"stopped"|"error", "pid": int}}}
    {"type": "log",     "component": "name", "line": "...", "stream": "stdout"|"stderr"}

Usage:
  python manager.py
  python manager.py --config config.yaml
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from collections import deque
from typing import Dict, Optional, Set

import websockets
import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # data_server root

# ---------------------------------------------------------------------------
# Component definition
# ---------------------------------------------------------------------------

class Component:
    def __init__(self, name: str, cfg: dict):
        self.name    = name
        self.cmd     = cfg["cmd"]
        self.cwd     = os.path.join(BASE_DIR, cfg.get("cwd", "."))
        self.env     = cfg.get("env", {})
        self.label   = cfg.get("label", name)
        self.process: Optional[asyncio.subprocess.Process] = None
        self.status  = "stopped"   # stopped | starting | running | error
        self.pid: Optional[int] = None
        self.logs: deque = deque(maxlen=200)
        self._reader_tasks: list = []

    def to_dict(self) -> dict:
        return {"status": self.status, "pid": self.pid, "label": self.label}

    async def start(self, broadcast_fn) -> None:
        if self.status == "running":
            return
        self.status = "starting"
        await broadcast_fn(self._status_msg())

        env = {**os.environ, **self.env}
        try:
            self.process = await asyncio.create_subprocess_exec(
                *self.cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
                env=env,
            )
            self.pid    = self.process.pid
            self.status = "running"
            log.info("Started %s (pid %d)", self.name, self.pid)
            await broadcast_fn(self._status_msg())

            # Stream stdout and stderr
            self._reader_tasks = [
                asyncio.create_task(self._stream(self.process.stdout, "stdout", broadcast_fn)),
                asyncio.create_task(self._stream(self.process.stderr, "stderr", broadcast_fn)),
                asyncio.create_task(self._wait(broadcast_fn)),
            ]
        except FileNotFoundError as exc:
            self.status = "error"
            self.pid    = None
            msg = f"[ERROR] Could not start: {exc}"
            self.logs.append(msg)
            await broadcast_fn({"type": "log", "component": self.name, "line": msg, "stream": "stderr"})
            await broadcast_fn(self._status_msg())

    async def stop(self, broadcast_fn) -> None:
        if self.process and self.status == "running":
            log.info("Stopping %s (pid %d)", self.name, self.pid)
            try:
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.process.kill()
            for t in self._reader_tasks:
                t.cancel()
            self._reader_tasks = []
        self.status  = "stopped"
        self.pid     = None
        self.process = None
        await broadcast_fn(self._status_msg())

    async def _stream(self, stream, stream_name: str, broadcast_fn) -> None:
        try:
            async for raw in stream:
                line = raw.decode(errors="replace").rstrip()
                if not line:
                    continue
                self.logs.append(line)
                await broadcast_fn({"type": "log", "component": self.name, "line": line, "stream": stream_name})
        except Exception:
            pass

    async def _wait(self, broadcast_fn) -> None:
        await self.process.wait()
        rc = self.process.returncode
        if self.status == "running":  # didn't stop intentionally
            self.status = "error" if rc != 0 else "stopped"
            self.pid    = None
            msg = f"[EXITED] return code {rc}"
            self.logs.append(msg)
            await broadcast_fn({"type": "log", "component": self.name, "line": msg, "stream": "stderr"})
            await broadcast_fn(self._status_msg())

    def _status_msg(self) -> dict:
        return {"type": "status", "components": {self.name: self.to_dict()}}


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

g_components: Dict[str, Component] = {}
g_connected: Set = set()


async def broadcast(msg: dict) -> None:
    if not g_connected:
        return
    data = json.dumps(msg, default=str)
    await asyncio.gather(*[ws.send(data) for ws in list(g_connected)], return_exceptions=True)


async def send_full_status(ws) -> None:
    await ws.send(json.dumps({
        "type":       "status",
        "components": {name: c.to_dict() for name, c in g_components.items()},
    }))
    # Replay last 20 log lines per component
    for name, comp in g_components.items():
        for line in list(comp.logs)[-20:]:
            await ws.send(json.dumps({"type": "log", "component": name, "line": line, "stream": "stdout"}))


async def ws_handler(websocket) -> None:
    g_connected.add(websocket)
    log.info("Dashboard client connected (%d total)", len(g_connected))
    await send_full_status(websocket)

    try:
        async for raw in websocket:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            t = msg.get("type")

            if t == "get_status":
                await send_full_status(websocket)

            elif t == "start":
                name = msg.get("component")
                if name in g_components:
                    asyncio.create_task(g_components[name].start(broadcast))

            elif t == "stop":
                name = msg.get("component")
                if name in g_components:
                    asyncio.create_task(g_components[name].stop(broadcast))

            elif t == "start_all":
                for comp in g_components.values():
                    asyncio.create_task(comp.start(broadcast))

            elif t == "stop_all":
                for comp in g_components.values():
                    asyncio.create_task(comp.stop(broadcast))

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        g_connected.discard(websocket)
        log.info("Dashboard client disconnected (%d remaining)", len(g_connected))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main_async(cfg: dict) -> None:
    global g_components

    order = cfg.get("order", list(cfg["components"].keys()))
    for name in order:
        comp_cfg = cfg["components"][name]
        g_components[name] = Component(name, comp_cfg)

    ws_cfg  = cfg.get("websocket", {})
    ws_host = ws_cfg.get("host", "0.0.0.0")
    ws_port = ws_cfg.get("port", 8760)

    log.info("Manager WebSocket on ws://%s:%d", ws_host, ws_port)
    log.info("Components: %s", ", ".join(g_components.keys()))

    async with websockets.serve(ws_handler, ws_host, ws_port):
        await asyncio.Event().wait()


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Data server process manager")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    try:
        asyncio.run(main_async(load_config(args.config)))
    except KeyboardInterrupt:
        log.info("Shutting down — stopping all components")
        async def stop_all():
            for comp in g_components.values():
                await comp.stop(lambda m: None)
        asyncio.run(stop_all())
