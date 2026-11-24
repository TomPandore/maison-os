"""Un faux Matter Server qui parle le même WebSocket que le vrai (schéma 14), chargé avec les
vraies données des ampoules de la maison. Il applique les commandes reçues à son état et
renvoie les `attribute_updated` comme le ferait une vraie ampoule.

Lancement manuel (pour essayer Maison sans le Pi) :
    python -m tests.fake_matter_server 5580
"""

from __future__ import annotations

import asyncio
import os
import copy
import json
import sys
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

FIXTURE = Path(__file__).parent / "fixtures" / "matter_nodes.json"


class FakeMatterServer:
    def __init__(self) -> None:
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.info: dict[str, Any] = data["server_info"]
        self.nodes: dict[int, dict[str, Any]] = {n["node_id"]: copy.deepcopy(n) for n in data["nodes"]}
        self.fade_s = 0.0  # > 0 : la luminosité fond par étapes, comme une vraie ampoule
        self.commands: list[dict[str, Any]] = []
        self.clients: set[web.WebSocketResponse] = set()
        self.runner: web.AppRunner | None = None
        self.port = 0

    async def start(self, port: int = 0) -> int:
        app = web.Application()
        app.router.add_get("/ws", self._ws)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", port)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        return self.port

    async def stop(self) -> None:
        await self.drop_clients()
        if self.runner:
            await self.runner.cleanup()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/ws"

    async def drop_clients(self) -> None:
        for ws in list(self.clients):
            await ws.close()

    async def emit(self, event: str, data: Any) -> None:
        for ws in list(self.clients):
            await ws.send_json({"event": event, "data": data})

    async def set_attr(self, node_id: int, path: str, value: Any) -> None:
        self.nodes[node_id]["attributes"][path] = value
        await self.emit("attribute_updated", [node_id, path, value])

    async def set_available(self, node_id: int, available: bool) -> None:
        self.nodes[node_id]["available"] = available
        await self.emit("node_updated", self.nodes[node_id])

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.clients.add(ws)
        await ws.send_json(self.info)
        try:
            async for msg in ws:
                if msg.type is not WSMsgType.TEXT:
                    continue
                req = json.loads(msg.data)
                mid = req.get("message_id")
                cmd = req.get("command")
                if cmd == "start_listening":
                    await ws.send_json({"message_id": mid, "result": list(self.nodes.values())})
                elif cmd == "device_command":
                    args = req["args"]
                    self.commands.append(args)
                    node = self.nodes.get(args["node_id"])
                    if node is None or not node["available"]:
                        await ws.send_json({"message_id": mid, "error_code": 5, "details": "node not available"})
                        continue
                    await ws.send_json({"message_id": mid, "result": None})
                    await self._apply(args)
                else:
                    await ws.send_json({"message_id": mid, "error_code": 1, "details": f"unknown command {cmd}"})
        finally:
            self.clients.discard(ws)
        return ws

    async def _apply(self, a: dict[str, Any]) -> None:
        n, ep, cl, name, p = a["node_id"], a["endpoint_id"], a["cluster_id"], a["command_name"], a.get("payload") or {}
        if cl == 6:
            await self.set_attr(n, f"{ep}/6/0", name == "On" if name in ("On", "Off") else not self.nodes[n]["attributes"][f"{ep}/6/0"])
        elif cl == 8 and name == "MoveToLevelWithOnOff":
            await self.set_attr(n, f"{ep}/6/0", True)
            if self.fade_s > 0:  # comme une vraie ampoule qui fond : elle renvoie ses étapes
                asyncio.get_running_loop().create_task(self._fade(n, f"{ep}/8/0", p["level"]))
            else:
                await self.set_attr(n, f"{ep}/8/0", p["level"])
        elif cl == 768 and name == "MoveToColorTemperature":
            await self.set_attr(n, f"{ep}/768/7", p["colorTemperatureMireds"])
            await self.set_attr(n, f"{ep}/768/8", 2)
        elif cl == 768 and name == "MoveToHueAndSaturation":
            await self.set_attr(n, f"{ep}/768/0", p["hue"])
            await self.set_attr(n, f"{ep}/768/1", p["saturation"])
            await self.set_attr(n, f"{ep}/768/8", 0)


    async def _fade(self, n: int, path: str, target: int) -> None:
        start = self.nodes[n]["attributes"].get(path) or 1
        steps = 8
        for i in range(1, steps + 1):
            await asyncio.sleep(self.fade_s / steps)
            await self.set_attr(n, path, round(start + (target - start) * i / steps))


async def _main(port: int) -> None:
    srv = FakeMatterServer()
    srv.fade_s = float(os.environ.get("FAUX_FONDU", "0"))
    await srv.start(port)
    print(f"Faux Matter Server sur {srv.url}")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(_main(int(sys.argv[1]) if len(sys.argv) > 1 else 5580))
