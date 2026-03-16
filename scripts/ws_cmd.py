#!/usr/bin/env python3
"""
ws_cmd.py — send a command to the manager WebSocket and stream responses.

Usage:
  python ws_cmd.py <ws-url> <command> [--verbose] [--timeout N]

Commands:  start_all, stop_all, start_all, rescan, get_status, stop, start
Exit codes:
  0  — all components reached the expected terminal state
  1  — manager unreachable
  2  — timeout waiting for terminal state
"""

import argparse
import asyncio
import json
import sys

import websockets


TERMINAL_FOR = {
    "start_all": "running",
    "stop_all":  "stopped",
}


async def run(url: str, command: str, verbose: bool, timeout: int) -> int:
    try:
        async with websockets.connect(url, open_timeout=5) as ws:
            # Drain the initial status/log burst the manager sends on connect
            await ws.send(json.dumps({"type": "get_status"}))
            initial = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
            components = initial.get("components", {})

            if verbose:
                print(f"[manager] connected — {len(components)} components")
                for name, info in components.items():
                    print(f"  {name}: {info['status']}  pid={info.get('pid', '-')}")
                print(f"[manager] sending: {command}")

            await ws.send(json.dumps({"type": command}))

            target = TERMINAL_FOR.get(command)
            deadline = asyncio.get_event_loop().time() + timeout

            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    print("[manager] timeout waiting for completion", file=sys.stderr)
                    return 2
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 2.0))
                except asyncio.TimeoutError:
                    # If no target state to wait for, we're done
                    if target is None:
                        return 0
                    continue

                msg = json.loads(raw)

                if msg["type"] == "log":
                    if verbose:
                        print(f"  [{msg['component']:16s}] {msg['line']}")

                elif msg["type"] == "status":
                    updates = msg.get("components", {})
                    components.update(updates)
                    if verbose:
                        for name, info in updates.items():
                            print(f"  [status] {name}: {info['status']}  pid={info.get('pid', '-')}")

                    # Check if all components reached the target state
                    if target:
                        done = all(
                            c["status"] in (target, "error")
                            for c in components.values()
                        )
                        if done:
                            if verbose:
                                print(f"[manager] all components {target}")
                            return 0

    except (OSError, websockets.exceptions.WebSocketException) as exc:
        print(f"[manager] unreachable at {url}: {exc}", file=sys.stderr)
        return 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("command")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()

    rc = asyncio.run(run(args.url, args.command, args.verbose, args.timeout))
    sys.exit(rc)


if __name__ == "__main__":
    main()
