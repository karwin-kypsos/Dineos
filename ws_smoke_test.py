"""One-off manual WebSocket smoke test — connects to the kitchen and table
session sockets, then a separate script (or curl) drives the REST flow while
this prints every frame received. Not part of the test suite; for eyeballing
real-time event delivery during manual verification.
"""
import asyncio
import json
import sys

import websockets


async def listen(name, url, duration):
    try:
        async with websockets.connect(url) as ws:
            print(f"[{name}] connected")
            try:
                while True:
                    frame = await asyncio.wait_for(ws.recv(), timeout=duration)
                    print(f"[{name}] << {frame}")
            except asyncio.TimeoutError:
                print(f"[{name}] done listening")
    except Exception as exc:
        print(f"[{name}] ERROR: {exc}")


async def main():
    kds_key = sys.argv[1]
    session_id = sys.argv[2]
    duration = float(sys.argv[3]) if len(sys.argv) > 3 else 8.0

    await asyncio.gather(
        listen("kitchen", f"ws://127.0.0.1:8000/ws/kitchen/?kds_key={kds_key}", duration),
        listen("table", f"ws://127.0.0.1:8000/ws/table/{session_id}/", duration),
    )


if __name__ == "__main__":
    asyncio.run(main())
