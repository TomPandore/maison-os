"""Import des pièces et des noms depuis Home Assistant : proposer, puis appliquer avec le code."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import uvicorn

from maison.app import build_app
from maison.config import DEFAULTS, Config, PinGuard, _merge

from .fake_home_assistant import TOKEN, FakeHomeAssistant
from .fake_matter_server import FakeMatterServer
from .test_matter import UID1, UID2, UID3, wait_for

PIN = "246810"
READ_ONLY_COMMANDS = {"config/area_registry/list", "config/device_registry/list"}


@pytest.fixture
async def stack(tmp_path, monkeypatch):
    monkeypatch.setenv("MAISON_ADDON_OPTIONS", str(tmp_path / "absent.json"))
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.setenv("MAISON_HA_TOKEN", TOKEN)
    matter, ha = FakeMatterServer(), FakeHomeAssistant()
    await matter.start()
    await ha.start()
    raw = _merge(DEFAULTS, {
        "guard": {"protected_rooms": ["Chambre enfant"]},
        "homeassistant": {"url": ha.url},
        "drivers": {"matter": {"url": matter.url}},
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
        yield {"http": http, "hub": hub, "ha": ha, "data": tmp_path, "monkeypatch": monkeypatch}
    server.should_exit = True
    await task
    await ha.stop()
    await matter.stop()


async def test_proposes_rooms_and_names(stack):
    r = await stack["http"].get("/api/import/homeassistant")
    assert r.status_code == 200, r.text
    plan = r.json()
    by_id = {p["device_id"]: p for p in plan["proposals"]}
    assert set(by_id) == {UID1, UID2, UID3} and plan["unmatched_devices"] == []
    assert by_id[UID1]["match"] == "identifiant"
    assert by_id[UID1]["proposed"] == {"name": "Lampe de bureau", "room": "Bureau"}
    assert by_id[UID2]["match"] == "numéro de série"  # HA n'a que le numéro de série pour celle-ci
    assert by_id[UID2]["proposed"] == {"name": "Lampe Plafond", "room": "Bureau"}
    assert by_id[UID3]["proposed"] == {"name": "Plafond", "room": "Salle à manger"}
    assert plan["ha_devices_not_in_maison"] == 2  # veilleuse ESPHome et téléphone, pas encore dans Maison
    assert "Chambre enfant" in plan["ha_rooms"]
    # rien n'a été appliqué, et HA n'a reçu que des lectures
    assert all(d.room is None for d in stack["hub"].devices.values())
    assert set(stack["ha"].commands) <= READ_ONLY_COMMANDS


async def test_apply_needs_the_pin_and_only_takes_what_was_ticked(stack):
    http, hub = stack["http"], stack["hub"]
    r = await http.post("/api/import/homeassistant", json={"device_ids": [UID1]})
    assert r.status_code == 403
    r = await http.post("/api/import/homeassistant", json={"device_ids": [UID1], "pin": "000000"})
    assert r.status_code == 403
    assert hub.devices[UID1].room is None

    r = await http.post("/api/import/homeassistant", json={"device_ids": [UID1, UID3, "matter:inconnu"], "pin": PIN})
    assert r.status_code == 200, r.text
    body = r.json()
    assert {a["device_id"] for a in body["applied"]} == {UID1, UID3} and body["skipped"] == ["matter:inconnu"]
    assert (hub.devices[UID1].display_name, hub.devices[UID1].effective_room) == ("Lampe de bureau", "Bureau")
    assert (hub.devices[UID3].display_name, hub.devices[UID3].effective_room) == ("Plafond", "Salle à manger")
    assert hub.devices[UID2].room is None  # pas coché, pas touché

    labels = json.loads((stack["data"] / "labels.json").read_text(encoding="utf-8"))
    assert labels[UID1] == {"name": "Lampe de bureau", "room": "Bureau"}
    actions = [e["action"] for e in hub.journal.tail(20)]
    assert actions.count("label") == 2 and "import_ha" in actions

    # relu : ce qui est appliqué est « déjà à jour »
    plan = (await http.get("/api/import/homeassistant")).json()
    assert {p["device_id"]: p["changes"] for p in plan["proposals"]} == {UID1: False, UID2: True, UID3: False}
    assert set(stack["ha"].commands) <= READ_ONLY_COMMANDS


async def test_clear_errors_without_access(stack):
    mp = stack["monkeypatch"]
    mp.setenv("MAISON_HA_TOKEN", "mauvais-jeton")
    r = await stack["http"].get("/api/import/homeassistant")
    assert r.status_code == 503 and "refuse le jeton" in r.json()["error"]
    mp.delenv("MAISON_HA_TOKEN")
    r = await stack["http"].get("/api/import/homeassistant")
    assert r.status_code == 503 and "homeassistant_api" in r.json()["error"]
