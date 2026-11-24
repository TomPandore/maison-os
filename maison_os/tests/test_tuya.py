"""Pilote Tuya local : conversions et ordres, avec un faux appareil (aucun réseau)."""

from __future__ import annotations

import json

from maison.core.guard import GuardPolicy
from maison.core.hub import Hub
from maison.core.model import Origin
from maison.drivers.tuya import TuyaDriver, command_to_dps, dps_to_values, make_colour, parse_colour

from .test_matter import wait_for

DPS = {"20": True, "21": "white", "22": 505, "23": 500, "24": "00b403e803e8"}


def test_values_white_and_colour():
    assert dps_to_values(DPS) == {"on": True, "brightness": 50, "color_temp": 4250, "color_mode": "ct", "color": None}
    v = dps_to_values({**DPS, "21": "colour", "24": make_colour(240, 1000, 500)})
    assert v["color_mode"] == "hs" and v["brightness"] == 50 and v["color"] == "#0000ff"
    assert parse_colour("zz") is None


def test_commands_map_to_datapoints():
    assert command_to_dps("on", False, DPS) == {"20": False}
    assert command_to_dps("brightness", 35, DPS) == {"20": True, "21": "white", "22": 356}
    assert command_to_dps("color_temp", 2000, DPS) == {"20": True, "21": "white", "23": 0}
    out = command_to_dps("color", "#ff0000", DPS)
    assert out["21"] == "colour" and parse_colour(out["24"])[:2] == (0, 1000)
    colour = {**DPS, "21": "colour", "24": make_colour(120, 1000, 800)}
    assert parse_colour(command_to_dps("brightness", 25, colour)["24"]) == (120, 1000, 250)  # garde la teinte


class FakeBulb:
    def __init__(self, store):
        self.store = store

    def status(self):
        return {"dps": dict(self.store)}

    def set_multiple_values(self, dps):
        self.store.update(dps)
        return {"dps": dps}


async def test_driver_reads_and_commands(tmp_path):
    keys = tmp_path / "tuya.json"
    keys.write_text(json.dumps({"devices": [{"id": "abc123", "name": "Bande LED", "category": "dd", "local_key": "k" * 16}]}))
    store = dict(DPS)
    drv = TuyaDriver(keys, read_only=False, poll_s=0.05, device_factory=lambda conf: FakeBulb(store),
                     scanner=lambda: {"abc123": {"ip": "10.0.0.9", "version": "3.5"}})
    hub = Hub(tmp_path, GuardPolicy())
    hub.add_driver(drv)
    await hub.start()
    try:
        await wait_for(lambda: "tuya:abc123" in hub.devices and hub.devices["tuya:abc123"].online)
        dev = hub.devices["tuya:abc123"]
        assert dev.points["brightness"].value == 50 and dev.points["color_temp"].value == 4250
        hub.set_label(dev.id, actor="t", origin=Origin.UI, room="Bureau")
        r = await hub.command(dev.id, "color", "#0000ff", origin=Origin.UI, actor="test")
        assert r.status == "done" and store["21"] == "colour"
        await wait_for(lambda: hub.devices[dev.id].points["color_mode"].value == "hs")
    finally:
        await hub.stop()
