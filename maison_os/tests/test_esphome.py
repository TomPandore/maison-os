"""Pilote ESPHome : les entités d'un module deviennent les points d'un appareil."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from maison.core.guard import GuardPolicy
from maison.core.hub import Hub
from maison.core.model import Origin
from maison.drivers.esphome import EspHomeDriver, hex_to_rgb, rgb_to_hex, slug

from .test_matter import wait_for


@dataclass
class Info:
    name: str = "veilleuse"
    friendly_name: str = "Veilleuse"
    mac_address: str = "AA:BB:CC:00:11:22"
    model: str = "esp32-c3"
    esphome_version: str = "2026.10"


@dataclass
class Ent:
    key: int
    name: str
    effects: list = field(default_factory=list)
    options: list = field(default_factory=list)
    min_value: float = 0
    max_value: float = 0
    step: float = 1
    unit_of_measurement: str = ""


class LightInfo(Ent): ...
class SwitchInfo(Ent): ...
class NumberInfo(Ent): ...
class SelectInfo(Ent): ...
class ButtonInfo(Ent): ...


@dataclass
class St:
    key: int
    state: object = None
    brightness: float | None = None
    red: float | None = None
    green: float | None = None
    blue: float | None = None
    effect: str | None = None


class FakeClient:
    def __init__(self):
        self.sent = []
        self.cb = None

    async def connect(self, on_stop=None, login=False):
        self.on_stop = on_stop

    async def device_info(self):
        return Info()

    async def list_entities_services(self):
        return [LightInfo(1, "Fusée", effects=["None", "Étoiles"]), SwitchInfo(2, "Programmation automatique"),
                NumberInfo(3, "Coucher - heure", min_value=17, max_value=23), SelectInfo(4, "Couleur", options=["Bleu", "Rose"]),
                ButtonInfo(5, "Décollage !")], []

    def subscribe_states(self, cb):
        self.cb = cb
        cb(St(1, False, 1.0, 0.0, 0.0, 1.0, "None"))
        cb(St(2, False)); cb(St(3, 20.0)); cb(St(4, "Bleu"))

    def light_command(self, key, **kw):
        self.sent.append(("light", key, kw))
        self.cb(St(1, kw.get("state", True), kw.get("brightness", 1.0), *(kw.get("rgb") or (0.0, 0.0, 1.0)), kw.get("effect", "None")))

    def switch_command(self, key, state): self.sent.append(("switch", key, state))
    def number_command(self, key, state): self.sent.append(("number", key, state))
    def select_command(self, key, state): self.sent.append(("select", key, state))
    def button_command(self, key): self.sent.append(("button", key))
    async def disconnect(self): pass


def test_helpers():
    assert slug("Réveil - heure") == "reveil_heure" and slug("Décollage !") == "decollage"
    assert rgb_to_hex(0.5, 0.0, 0.25) == "#ff0080" and hex_to_rgb("#ff0000") == (1.0, 0.0, 0.0)


async def test_driver_maps_entities_and_commands(tmp_path):
    fake = FakeClient()
    drv = EspHomeDriver([{"host": "veilleuse.local", "name": "Fusée", "room": "Chambre test"}], read_only=False,
                        client_factory=lambda c: fake)
    hub = Hub(tmp_path, GuardPolicy())
    hub.add_driver(drv)
    await hub.start()
    try:
        did = "esphome:aabbcc001122"
        await wait_for(lambda: did in hub.devices and hub.devices[did].online and hub.devices[did].points["coucher_heure"].value == 20)
        dev = hub.devices[did]
        assert dev.kind == "light" and dev.effective_room == "Chambre test" and dev.display_name == "Fusée"
        assert dev.points["color"].value == "#0000ff" and dev.points["effect"].value == "None"
        assert set(dev.points) >= {"on", "brightness", "color", "effect", "programmation_automatique", "couleur", "decollage"}
        r = await hub.command(did, "color", "#ff0000", origin=Origin.UI, actor="t")
        assert r.status == "done"
        await wait_for(lambda: hub.devices[did].points["color"].value == "#ff0000")
        assert (await hub.command(did, "decollage", True, origin=Origin.UI, actor="t")).status == "done"
        assert (await hub.command(did, "couleur", "Rose", origin=Origin.UI, actor="t")).status == "done"
        assert ("button", 5) in fake.sent and ("select", 4, "Rose") in fake.sent
        assert (await hub.command(did, "couleur", "Vert", origin=Origin.UI, actor="t")).status == "error"
    finally:
        await hub.stop()
        await asyncio.sleep(0)
