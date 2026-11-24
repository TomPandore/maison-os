"""Ambiances : appliquées par la garde, refusées aux profils pour qui la pièce est fermée."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import uvicorn

from maison.app import build_app
from maison.config import DEFAULTS, Config, PinGuard, _merge

from .fake_matter_server import FakeMatterServer
from .test_matter import UID1, UID2, wait_for

PIN = "246810"
SCENES = [
    {"id": "bureau-travail", "name": "Travail", "room": "Bureau", "actions": [
        {"device": UID1, "on": True, "brightness": 40, "color_temp": 4000},
        {"device": UID2, "on": False},
        {"device": "tuya:absente", "on": True, "brightness": 35}]},
]


@pytest.fixture
async def house(tmp_path, monkeypatch):
    monkeypatch.setenv("MAISON_ADDON_OPTIONS", str(tmp_path / "absent.json"))
    srv = FakeMatterServer()
    await srv.start()
    raw = _merge(DEFAULTS, {
        "drivers": {"matter": {"url": srv.url, "read_only": False}},
        "scenes": SCENES,
        "people": {"require_pairing": True, "profiles": [{"name": "Parent"}, {"name": "Invitée", "deny_rooms": ["Bureau"]}]},
    })
    PinGuard(tmp_path).set(PIN)
    app = build_app(Config(data_dir=tmp_path, raw=raw))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on"))
    task = asyncio.create_task(server.serve())
    await wait_for(lambda: server.started, timeout=5)
    port = server.servers[0].sockets[0].getsockname()[1]
    hub = app.state.hub
    await wait_for(lambda: len(hub.devices) == 3)
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=5) as http:
        for uid in (UID1, UID2):
            assert (await http.put(f"/api/labels/{uid}", json={"room": "Bureau", "pin": PIN})).status_code == 200
        yield {"http": http, "hub": hub}
    server.should_exit = True
    await task
    await srv.stop()


async def key(http, who):
    return (await http.post("/api/associer", json={"person": who, "pin": PIN})).json()["key"]


async def test_scene_sets_every_light_and_skips_missing(house):
    http, hub = house["http"], house["hub"]
    assert [s["name"] for s in (await http.get("/api/scenes")).json()] == ["Travail"]
    r = await http.post("/api/scenes/bureau-travail", json={}, headers={"x-maison-cle": await key(http, "Parent")})
    assert r.status_code == 200 and r.json()["skipped"] == ["tuya:absente"]
    await wait_for(lambda: hub.devices[UID1].points["brightness"].value == 40 and hub.devices[UID2].points["on"].value is False)
    assert hub.devices[UID1].points["on"].value is True
    assert any(e.get("action") == "scene" and e.get("actor", "").startswith("Parent@") for e in hub.journal.tail(20))


async def test_scene_closed_room_and_unpaired(house):
    http = house["http"]
    assert (await http.post("/api/scenes/bureau-travail", json={})).status_code == 401
    r = await http.post("/api/scenes/bureau-travail", json={}, headers={"x-maison-cle": await key(http, "Invitée")})
    assert r.status_code == 403
    assert (await http.post("/api/scenes/inconnue", json={})).status_code == 404
