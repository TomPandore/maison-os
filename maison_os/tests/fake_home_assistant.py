"""Un faux Home Assistant qui parle l'API WebSocket (auth + registres), en lecture seule."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

FIXTURE = Path(__file__).parent / "fixtures" / "ha_registry.json"
TOKEN = "jeton-de-test"


class FakeHomeAssistant:
    def __init__(self) -> None:
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.areas: list[dict[str, Any]] = copy.deepcopy(data["areas"])
        self.devices: list[dict[str, Any]] = copy.deepcopy(data["devices"])
        self.commands: list[str] = []
        self.runner: web.AppRunner | None = None
        self.port = 0

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/api/websocket", self._ws)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/api/websocket"

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required", "ha_version": "2026.11.1"})
        auth = await ws.receive_json()
        if auth.get("access_token") != TOKEN:
            await ws.send_json({"type": "auth_invalid", "message": "Invalid access token"})
            await ws.close()
            return ws
        await ws.send_json({"type": "auth_ok", "ha_version": "2026.11.1"})
        async for msg in ws:
            if msg.type is not WSMsgType.TEXT:
                continue
            req = json.loads(msg.data)
            self.commands.append(req["type"])
            result = {"config/area_registry/list": self.areas, "config/device_registry/list": self.devices}.get(req["type"])
            if result is None:
                await ws.send_json({"id": req["id"], "type": "result", "success": False, "error": {"code": "unknown_command", "message": "Unknown command."}})
            else:
                await ws.send_json({"id": req["id"], "type": "result", "success": True, "result": result})
        return ws
