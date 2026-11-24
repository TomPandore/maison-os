"""Le pilote Matter contre un faux Matter Server chargé des vraies données de la maison."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, time

import pytest

from maison.core.guard import GuardPolicy
from maison.core.hub import Hub
from maison.core.model import Origin
from maison.drivers import matter as m
from maison.drivers.matter import MatterDriver

from .fake_matter_server import FakeMatterServer

UID1, UID2, UID3 = "matter:node1", "matter:node2", "matter:node3"
OLD_UID1 = "matter:UID-FICTIF-0001"  # ancienne identité (UniqueID), avant la v0.4


async def wait_for(cond, timeout=3.0):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition jamais atteinte")


@pytest.fixture
async def server():
    srv = FakeMatterServer()
    await srv.start()
    yield srv
    await srv.stop()


async def make_hub(tmp_path, server, read_only=True, guard=None):
    hub = Hub(tmp_path, guard or GuardPolicy())
    hub.add_driver(MatterDriver(url=server.url, read_only=read_only))
    await hub.start()
    await wait_for(lambda: len(hub.devices) == 3)
    return hub


async def test_reads_the_three_real_bulbs(tmp_path, server):
    hub = await make_hub(tmp_path, server)
    try:
        assert set(hub.devices) == {UID1, UID2, UID3}
        desk = hub.devices[UID1]
        assert desk.kind == "light"
        assert desk.native_name == "LED P45 E14 470LM MYKO RGBW"
        assert desk.manufacturer == "Kingfisher"
        v = {k: p.value for k, p in desk.points.items()}
        assert v == {"on": True, "brightness": 75, "color_temp": 4505, "color": None, "color_mode": "ct"}
        ct = desk.points["color_temp"].spec
        assert (ct.minimum, ct.maximum) == (2703, 6536)  # 370 et 153 mireds
        assert hub.devices[UID2].points["brightness"].value == 100
        assert hub.devices[UID3].online is False  # le plafond de la salle à manger, hors ligne
        assert all(not p.spec.access.writable for d in hub.devices.values() for p in d.points.values())
    finally:
        await hub.stop()


async def test_live_updates_and_availability(tmp_path, server):
    hub = await make_hub(tmp_path, server)
    events = []

    async def collect():
        async for ev in hub.subscribe():
            events.append(ev)

    t = asyncio.create_task(collect())
    try:
        await asyncio.sleep(0.05)
        await server.set_attr(2, "1/6/0", False)
        await wait_for(lambda: hub.devices[UID2].points["on"].value is False)
        await server.set_available(3, True)
        await wait_for(lambda: hub.devices[UID3].online)
        await server.set_attr(1, "1/768/8", 0)  # passe en couleur (teinte/saturation)
        await wait_for(lambda: hub.devices[UID1].points["color"].value is not None)
        assert hub.devices[UID1].points["color_mode"].value == "hs"
        assert any(e["type"] == "state" and e["device_id"] == UID2 and e["key"] == "on" for e in events)
    finally:
        t.cancel()
        await hub.stop()


async def test_read_only_refuses_orders(tmp_path, server):
    hub = await make_hub(tmp_path, server, read_only=True)
    try:
        r = await hub.command(UID1, "on", False, origin=Origin.UI, actor="test")
        assert r.status == "error" and "lecture seule" in r.message
        assert server.commands == []
    finally:
        await hub.stop()


async def test_orders_go_through_and_come_back(tmp_path, server):
    hub = await make_hub(tmp_path, server, read_only=False)
    try:
        r = await hub.command(UID1, "brightness", 40, origin=Origin.UI, actor="test")
        assert r.status == "done", r.message
        assert server.commands[-1]["command_name"] == "MoveToLevelWithOnOff"
        assert server.commands[-1]["payload"] == {"level": 102, "transitionTime": 0, "optionsMask": 0, "optionsOverride": 0}
        await wait_for(lambda: hub.devices[UID1].points["brightness"].value == 40)

        r = await hub.command(UID1, "color_temp", 2700, origin=Origin.UI, actor="test")
        assert r.status == "done"
        assert server.commands[-1]["payload"]["colorTemperatureMireds"] == 370
        await wait_for(lambda: hub.devices[UID1].points["color_temp"].value == 2703)

        await server.set_attr(2, "1/6/0", False)
        await wait_for(lambda: hub.devices[UID2].points["on"].value is False)
        r = await hub.command(UID2, "color", "#ff0000", origin=Origin.UI, actor="test")
        assert r.status == "done"
        names = [c["command_name"] for c in server.commands[-2:]]
        assert names == ["On", "MoveToHueAndSaturation"]  # lampe éteinte : on l'allume d'abord
        await wait_for(lambda: hub.devices[UID2].points["color"].value == "#ff0000")

        r = await hub.command(UID1, "on", False, origin=Origin.UI, actor="test")
        assert r.status == "done"
        await wait_for(lambda: hub.devices[UID1].points["on"].value is False)

        # validation des valeurs
        assert (await hub.command(UID1, "brightness", 140, origin=Origin.UI, actor="t")).status == "error"
        assert (await hub.command(UID1, "color", "rouge", origin=Origin.UI, actor="t")).status == "error"
        assert (await hub.command(UID1, "color_mode", "hs", origin=Origin.UI, actor="t")).status == "error"
        # appareil hors ligne
        r = await hub.command(UID3, "on", True, origin=Origin.UI, actor="t")
        assert r.status == "error" and "injoignable" in r.message
        assert any(e["result"] == "ok" for e in hub.journal.tail(50))
    finally:
        await hub.stop()


async def test_guard_holds_agents_in_protected_rooms(tmp_path, server):
    guard = GuardPolicy.from_config({"protected_rooms": ["Chambre enfant"]})
    hub = await make_hub(tmp_path, server, read_only=False, guard=guard)
    try:
        hub.set_label(UID1, actor="papa", origin=Origin.UI, room="Chambre enfant")
        hub.set_label(UID2, actor="papa", origin=Origin.UI, room="Bureau")

        r = await hub.command(UID1, "on", False, origin=Origin.MCP, actor="claude")
        assert r.status == "pending" and r.approval_id
        assert server.commands == []
        assert hub.pending()[0]["room"] == "Chambre enfant"

        # un agent ne peut pas sortir l'appareil de la pièce protégée
        from maison.core.hub import CommandError

        with pytest.raises(CommandError):
            hub.set_label(UID1, actor="claude", origin=Origin.MCP, room="Bureau")

        # une personne valide : l'ordre part
        res = await hub.resolve_approval(r.approval_id, approve=True, by="papa")
        assert res.status == "done"
        assert server.commands[-1]["command_name"] == "Off"

        # hors pièce protégée : l'agent est obéi
        assert (await hub.command(UID2, "brightness", 50, origin=Origin.MCP, actor="claude")).status == "done"
        # appareil sans pièce : retenu pour un agent, obéi pour une personne
        await server.set_available(3, True)
        await wait_for(lambda: hub.devices[UID3].online)
        assert (await hub.command(UID3, "on", True, origin=Origin.MCP, actor="claude")).status == "pending"
        assert (await hub.command(UID3, "on", True, origin=Origin.UI, actor="papa")).status == "done"
        # le foyer sans code est retenu dans la pièce protégée
        assert (await hub.command(UID1, "on", True, origin=Origin.HOUSEHOLD, actor="foyer")).status == "pending"
    finally:
        await hub.stop()


def test_quiet_hours():
    g = GuardPolicy.from_config({"quiet_hours": {"start": "21:00", "end": "07:30"}})
    assert g.in_quiet_hours(datetime(2026, 11, 23, 22, 0))
    assert g.in_quiet_hours(datetime(2026, 11, 24, 7, 0))
    assert not g.in_quiet_hours(datetime(2026, 11, 24, 7, 30))
    assert not g.in_quiet_hours(datetime(2026, 11, 24, 15, 0))
    assert g.quiet_start == time(21, 0)


async def test_reconnects_after_server_drop(tmp_path, server):
    hub = await make_hub(tmp_path, server)
    try:
        await server.drop_clients()
        await wait_for(lambda: not hub.devices[UID1].online)
        await wait_for(lambda: hub.devices[UID1].online, timeout=5)
        assert hub.driver_status["matter"]["state"] == "running"
    finally:
        await hub.stop()


def test_colour_conversions():
    assert m.hs_to_hex(0, 254) == "#ff0000"
    assert m.hex_to_hs("#ff0000") == (0, 254)
    h, s = m.hex_to_hs("#0000ff")
    back = m.hs_to_hex(h, s)  # la teinte n'a que 254 crans : à 1 % près
    assert all(abs(int(back[i:i + 2], 16) - int("0000ff"[i - 1:i + 1], 16)) <= 3 for i in (1, 3, 5))
    x, y = m.hex_to_xy("#ffffff")
    assert abs(x / 65535 - 0.3127) < 0.015 and abs(y / 65535 - 0.329) < 0.01  # matrice « Hue », blanc proche de D65
    assert m.xy_to_hex(*m.hex_to_xy("#ff0000")) == "#ff0000"
    assert m.mireds_to_kelvin(222) == 4505 and m.kelvin_to_mireds(2700) == 370
    assert m.level_to_pct(254) == 100 and m.pct_to_level(1) == 3 and m.pct_to_level(0.1) == 1


async def test_identity_survives_uniqueid_change(tmp_path, server):
    """Les ampoules MYKO tirent un nouvel UniqueID après une coupure de courant :
    l'appareil doit rester le même (numéro de nœud), sans fantôme."""
    hub = await make_hub(tmp_path, server)
    try:
        server.nodes[3]["attributes"]["0/40/18"] = "NOUVEL-UID-APRES-COUPURE"
        await server.set_available(3, True)  # le serveur renvoie le nœud entier (node_updated)
        await wait_for(lambda: hub.devices[UID3].online)
        assert set(hub.devices) == {UID1, UID2, UID3}
    finally:
        await hub.stop()


async def test_labels_follow_old_identity(tmp_path, server):
    """Les étiquettes posées avant la v0.4 (sous l'UniqueID) sont reprises et journalisées."""
    (tmp_path / "labels.json").write_text(json.dumps({OLD_UID1: {"name": "Lampe de bureau", "room": "Bureau"}}), encoding="utf-8")
    hub = await make_hub(tmp_path, server)
    try:
        assert hub.devices[UID1].name == "Lampe de bureau" and hub.devices[UID1].room == "Bureau"
        saved = json.loads((tmp_path / "labels.json").read_text(encoding="utf-8"))
        assert UID1 in saved and OLD_UID1 not in saved
        assert any(e.get("action") == "label_migration" for e in hub.journal.tail(20))
    finally:
        await hub.stop()
