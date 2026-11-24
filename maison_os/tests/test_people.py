"""Profils de la maison : un téléphone associé commande, mais jamais dans une pièce fermée à son profil."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import uvicorn

from maison.app import build_app
from maison.config import DEFAULTS, Config, PinGuard, _merge

from .fake_matter_server import FakeMatterServer
from .test_matter import UID1, UID2, wait_for

PIN = "246810"


@pytest.fixture
async def house(tmp_path, monkeypatch):
    monkeypatch.setenv("MAISON_ADDON_OPTIONS", str(tmp_path / "absent.json"))
    srv = FakeMatterServer()
    await srv.start()
    raw = _merge(DEFAULTS, {
        "drivers": {"matter": {"url": srv.url, "read_only": False}},
        "people": {"require_pairing": True, "profiles": [
            {"name": "Parent"}, {"name": "Invitée", "deny_rooms": ["Bureau"]}]},
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
        r = await http.put(f"/api/labels/{UID1}", json={"room": "Bureau", "pin": PIN})
        assert r.status_code == 200
        yield {"http": http, "hub": hub, "dir": tmp_path}
    server.should_exit = True
    await task
    await srv.stop()


def cmd(device, value=False):
    return {"device_id": device, "key": "on", "value": value}


async def test_unpaired_phone_can_look_not_command(house):
    http = house["http"]
    me = (await http.get("/api/moi")).json()
    assert me["person"] is None and me["require_pairing"] and me["profiles"] == ["Parent", "Invitée"]
    assert (await http.get("/api/devices")).status_code == 200
    r = await http.post("/api/command", json=cmd(UID2), headers={"x-maison-origin": "ui"})
    assert r.status_code == 401
    r = await http.post("/api/command", json=cmd(UID2))  # même sans l'en-tête de la page
    assert r.status_code == 401


async def test_pairing_needs_the_house_code(house):
    http = house["http"]
    assert (await http.post("/api/associer", json={"person": "Parent", "pin": "000000"})).status_code == 403
    assert (await http.post("/api/associer", json={"person": "Inconnu", "pin": PIN})).status_code == 400
    r = await http.post("/api/associer", json={"person": "Parent", "pin": PIN, "label": "Téléphone"})
    assert r.status_code == 200
    key = r.json()["key"]
    stored = (house["dir"] / "telephones.json").read_text(encoding="utf-8")
    assert key not in stored  # seule l'empreinte est gardée
    me = (await http.get("/api/moi", headers={"x-maison-cle": key})).json()
    assert me["person"] == "Parent" and me["deny_rooms"] == []


async def test_guest_cannot_touch_a_closed_room(house):
    http, hub = house["http"], house["hub"]
    key = (await http.post("/api/associer", json={"person": "Invitée", "pin": PIN})).json()["key"]
    h = {"x-maison-cle": key, "x-maison-origin": "ui"}
    r = await http.post("/api/command", json=cmd(UID1), headers=h)  # lampe du Bureau
    assert r.status_code == 403 and "Bureau" in r.json()["message"]
    r = await http.post("/api/command", json=cmd(UID2), headers=h)  # ailleurs : permis
    assert r.status_code == 200
    await wait_for(lambda: hub.devices[UID2].points["on"].value is False)
    tail = hub.journal.tail(10)
    assert any(e.get("result") == "refusé" and e.get("actor", "").startswith("Invitée@") for e in tail)
    assert any(e.get("actor", "").startswith("Invitée@") and e.get("result") != "refusé" for e in tail)


async def test_revoked_phone_loses_access_at_once(house):
    from maison.api.people import People
    http = house["http"]
    key = (await http.post("/api/associer", json={"person": "Parent", "pin": PIN})).json()["key"]
    h = {"x-maison-cle": key, "x-maison-origin": "ui"}
    assert (await http.post("/api/command", json=cmd(UID2), headers=h)).status_code == 200
    phones = People(house["dir"], {"profiles": [{"name": "Parent"}]})
    assert phones.revoke(phones.phones()[0]["id"])  # comme `python -m maison retirer ID`
    assert (await http.post("/api/command", json=cmd(UID2), headers=h)).status_code == 401


async def test_install_manifest(house):
    http = house["http"]
    m = (await http.get("/manifest.webmanifest")).json()
    assert m["display"] == "standalone" and m["start_url"] == "/"
    r = await http.get("/icone-192.png")
    assert r.status_code == 200 and r.content[:4] == b"\x89PNG"
    assert (await http.get("/icone-13.png")).status_code == 404
    page = (await http.get("/")).text
    assert 'rel="manifest"' in page and json  # la page déclare l'appli
