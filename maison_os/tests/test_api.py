"""De bout en bout : un vrai serveur HTTP, le MCP comme Claude l'utiliserait, la garde,
le code de la maison, le flux d'événements."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import uvicorn
from mcp import Client

from maison.app import build_app
from maison.config import DEFAULTS, Config, PinGuard, _merge

from .fake_matter_server import FakeMatterServer
from .test_matter import UID1, UID2, wait_for

PIN = "246810"


@pytest.fixture
async def stack(tmp_path, monkeypatch):
    monkeypatch.setenv("MAISON_ADDON_OPTIONS", str(tmp_path / "absent.json"))
    srv = FakeMatterServer()
    await srv.start()
    raw = _merge(DEFAULTS, {
        "house_name": "Maison test",
        "rooms": ["Bureau", "Chambre enfant"],
        "guard": {"protected_rooms": ["Chambre enfant"]},
        "drivers": {"matter": {"url": srv.url, "read_only": False}},
    })
    PinGuard(tmp_path).set(PIN)
    app = build_app(Config(data_dir=tmp_path, raw=raw))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on"))
    task = asyncio.create_task(server.serve())
    await wait_for(lambda: server.started, timeout=5)
    port = server.servers[0].sockets[0].getsockname()[1]
    hub = app.state.hub
    await wait_for(lambda: len(hub.devices) == 3)
    base = f"http://127.0.0.1:{port}"
    async with httpx.AsyncClient(base_url=base, timeout=5) as http:
        yield {"http": http, "base": base, "hub": hub, "fake": srv}
    server.should_exit = True
    await task
    await srv.stop()


async def test_rest_and_pin(stack):
    http = stack["http"]
    h = (await http.get("/api/health")).json()
    assert h["devices"] == 3 and h["pin_configured"] and h["drivers"]["matter"]["state"] == "running"
    assert (await http.get("/")).status_code == 200
    assert (await http.get("/etat")).status_code == 200

    # étiquettes : seulement avec le code
    r = await http.put(f"/api/labels/{UID1}", json={"room": "Chambre enfant"})
    assert r.status_code == 403
    r = await http.put(f"/api/labels/{UID1}", json={"room": "Chambre enfant", "name": "Veilleuse test", "pin": PIN})
    assert r.status_code == 200 and r.json()["room"] == "Chambre enfant"
    r = await http.put(f"/api/labels/{UID2}", json={"room": "Bureau", "name": "Plafond bureau", "pin": PIN})
    assert r.status_code == 200

    # écritures en JSON seulement
    r = await http.post("/api/command", content=b"device_id=x", headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 415

    # un script : obéi au bureau, retenu dans la chambre protégée
    r = await http.post("/api/command", json={"device_id": UID2, "key": "brightness", "value": 30})
    assert r.status_code == 200, r.text
    r = await http.post("/api/command", json={"device_id": UID1, "key": "on", "value": False})
    assert r.status_code == 202
    appr = r.json()["approval_id"]
    # le tableau de bord avec le code est obéi partout
    r = await http.post("/api/command", json={"device_id": UID1, "key": "brightness", "value": 20, "pin": PIN}, headers={"x-maison-origin": "ui"})
    assert r.status_code == 200

    # valider demande le code
    assert (await http.post(f"/api/approvals/{appr}", json={"approve": True, "pin": "000000"})).status_code == 403
    r = await http.post(f"/api/approvals/{appr}", json={"approve": True, "pin": PIN})
    assert r.status_code == 200, r.text
    assert stack["fake"].commands[-1]["command_name"] == "Off"

    journal = (await http.get("/api/journal?limit=20")).json()
    assert any(e.get("approved_by") for e in journal)


async def test_host_guard(stack):
    http = stack["http"]
    assert (await http.get("/api/health", headers={"host": "evil.example"})).status_code == 403
    assert (await http.post("/mcp", headers={"host": "evil.example"}, json={})).status_code == 403
    assert (await http.get("/api/health", headers={"host": "homeassistant.local:8790"})).status_code == 200


async def test_mcp_as_claude(stack):
    hub = stack["hub"]
    from maison.core.model import Origin

    hub.set_label(UID1, actor="papa", origin=Origin.UI, room="Chambre enfant")
    hub.set_label(UID2, actor="papa", origin=Origin.UI, room="Bureau")
    async with Client(f"{stack['base']}/mcp") as client:
        tools = {t.name for t in (await client.list_tools()).tools}
        assert {"house_overview", "get_device", "set_device", "pending_approvals", "recent_journal", "set_label"} <= tools

        ov = (await client.call_tool("house_overview", {})).structured_content
        assert [d["id"] for d in ov["rooms"]["Bureau"]] == [UID2]
        assert ov["protected_rooms"] == ["chambre enfant"]
        assert len(ov["unassigned"]) == 1  # le plafond de la salle à manger, pas encore rangé

        r = (await client.call_tool("set_device", {"device_id": UID2, "key": "color_temp", "value": 2700})).structured_content
        assert r["status"] == "done", r
        await wait_for(lambda: hub.devices[UID2].points["color_temp"].value == 2703)

        r = (await client.call_tool("set_device", {"device_id": UID1, "key": "on", "value": False})).structured_content
        assert r["status"] == "pending"
        pend = (await client.call_tool("pending_approvals", {})).structured_content
        items = pend["result"] if isinstance(pend, dict) and "result" in pend else pend
        assert items[0]["device_id"] == UID1

        r = (await client.call_tool("set_label", {"device_id": UID1, "room": "Bureau"})).structured_content
        assert "error" in r  # sortir d'une pièce protégée demande une personne


async def test_event_stream(stack):
    http = stack["http"]
    async with http.stream("GET", "/api/events") as resp:
        lines = resp.aiter_lines()
        first = await asyncio.wait_for(lines.__anext__(), 3)
        snap = json.loads(first.removeprefix("data: "))
        assert snap["type"] == "snapshot" and len(snap["devices"]) == 3
        await stack["fake"].set_attr(2, "1/6/0", False)
        while True:
            line = await asyncio.wait_for(lines.__anext__(), 3)
            if line.startswith("data: "):
                ev = json.loads(line.removeprefix("data: "))
                if ev.get("type") == "state":
                    assert ev["device_id"] == UID2 and ev["key"] == "on" and ev["value"] is False
                    break


async def test_event_stream_survives_quiet_periods(stack, monkeypatch):
    """Un flux sans événement pendant plusieurs pings doit rester ouvert et livrer la suite."""
    from maison.api import http as http_mod
    monkeypatch.setattr(http_mod, "PING_S", 0.2)
    http = stack["http"]
    async with http.stream("GET", "/api/events") as resp:
        lines = resp.aiter_lines()
        await asyncio.wait_for(lines.__anext__(), 3)  # snapshot
        pings = 0
        while pings < 3:  # au moins 3 silences de suite
            line = await asyncio.wait_for(lines.__anext__(), 3)
            if line.startswith(": ping"):
                pings += 1
        await stack["fake"].set_attr(2, "1/6/0", False)
        while True:
            line = await asyncio.wait_for(lines.__anext__(), 3)
            if line.startswith("data: ") and json.loads(line.removeprefix("data: ")).get("type") == "state":
                break
