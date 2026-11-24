"""Pilote ESPHome en LOCAL, avec `aioesphomeapi` (la bibliothèque de Home Assistant).

Un module ESPHome = un appareil de la maison. Ses entités deviennent des points :
- la lumière → `on`, `brightness` (%), `color` (#rrggbb), `effect` ;
- un interrupteur → vrai/faux ; un nombre → nombre borné ; une liste → choix ;
- un bouton → point en écriture seule (écrire `true` = appuyer) ;
- un capteur binaire → lecture seule.

Dans `maison.yaml` :

    drivers:
      esphome:
        read_only: false
        devices:
          - {host: veilleuse-fusee.local, name: Fusée, room: Ma pièce}
          # key: … si le module a une clé de chiffrement d'API (sinon rien)
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from typing import TYPE_CHECKING, Any, Callable

from ..core.model import Access, Device, Kind, Point, PointSpec
from .base import Driver

if TYPE_CHECKING:
    from ..core.hub import Hub

log = logging.getLogger("maison.esphome")


def slug(text: str) -> str:
    """« Réveil - heure » → « reveil_heure »."""
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "_", t).strip("_")[:40] or "point"


def rgb_to_hex(r: float, g: float, b: float) -> str:
    m = max(r, g, b) or 1.0  # la couleur seule ; la luminosité est à part
    return "#{:02x}{:02x}{:02x}".format(*(round(max(0.0, c / m) * 255) for c in (r, g, b)))


def hex_to_rgb(color: str) -> tuple[float, float, float]:
    c = color.lstrip("#")
    return tuple(int(c[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


class EspHomeDriver(Driver):
    def __init__(self, devices: list[dict[str, Any]], id: str = "esphome", read_only: bool = True,
                 client_factory: Callable[[dict[str, Any]], Any] | None = None):
        self.id = id
        self.read_only = read_only
        self._confs = [d for d in devices if isinstance(d, dict) and d.get("host")]
        self._factory = client_factory
        self._nodes: dict[str, dict[str, Any]] = {}  # device_id → {client, by_key, by_point}

    def _client(self, conf: dict[str, Any]) -> Any:
        if self._factory:
            return self._factory(conf)
        from aioesphomeapi import APIClient
        return APIClient(str(conf["host"]), int(conf.get("port", 6053)), None, noise_psk=conf.get("key") or None)

    async def run(self, hub: Hub) -> None:
        if not self._confs:
            log.warning("aucun module ESPHome dans la configuration")
            await asyncio.Event().wait()
        await asyncio.gather(*(self._run_one(hub, c) for c in self._confs))

    async def _run_one(self, hub: Hub, conf: dict[str, Any]) -> None:
        delay = 2.0
        while True:
            dev_id = None
            try:
                dev_id = await self._session(hub, conf)
                delay = 2.0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — module éteint ou injoignable : on réessaie
                log.info("ESPHome %s injoignable : %s", conf.get("host"), str(e)[:120])
            for did, node in list(self._nodes.items()):
                if node["host"] == conf["host"]:
                    hub.set_online(did, False)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)

    async def _session(self, hub: Hub, conf: dict[str, Any]) -> str:
        client = self._client(conf)
        stopped = asyncio.Event()

        async def on_stop(expected: bool) -> None:
            stopped.set()

        await client.connect(on_stop=on_stop, login=True)
        try:
            info = await client.device_info()
            entities, _ = await client.list_entities_services()
            dev_id = f"{self.id}:{str(info.mac_address).replace(':', '').lower()}"
            dev, by_key, by_point = self._build(dev_id, conf, info, entities)
            self._nodes[dev_id] = {"client": client, "by_key": by_key, "by_point": by_point, "host": conf["host"]}
            hub.upsert_device(dev)
            hub.set_online(dev_id, True)

            def on_state(state: Any) -> None:
                ent = by_key.get(getattr(state, "key", None))
                if ent is None:
                    return
                for key, value in self._values(ent, state).items():
                    d = hub.devices.get(dev_id)
                    if d and key in d.points and d.points[key].value != value:
                        hub.update_point(dev_id, key, value)

            client.subscribe_states(on_state)
            log.info("ESPHome %s connecté (%d entités)", conf["host"], len(entities))
            await stopped.wait()
            return dev_id
        finally:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    def _build(self, dev_id: str, conf: dict[str, Any], info: Any, entities: list[Any]):
        rw = Access.READ if self.read_only else Access.READ_WRITE
        wo = Access.READ if self.read_only else Access.WRITE
        specs: list[PointSpec] = []
        by_key: dict[int, dict[str, Any]] = {}
        by_point: dict[str, dict[str, Any]] = {}
        kind = "device"
        for e in entities:
            t = type(e).__name__
            name = str(getattr(e, "name", "") or getattr(e, "object_id", ""))
            if t == "LightInfo" and kind != "light":
                kind = "light"
                effects = tuple(str(x) for x in (getattr(e, "effects", None) or []))
                ent = {"type": "light", "key": e.key, "name": name}
                specs += [PointSpec("on", Kind.BOOL, rw, label="Allumé"),
                          PointSpec("brightness", Kind.INT, rw, unit="%", minimum=1, maximum=100, label="Luminosité"),
                          PointSpec("color", Kind.COLOR, rw, label="Couleur")]
                if effects:
                    specs.append(PointSpec("effect", Kind.ENUM, rw, choices=effects, label="Effet"))
                for p in ("on", "brightness", "color", "effect"):
                    by_point[p] = ent
            else:
                key = slug(name)
                if key in by_point or key in ("on", "brightness", "color", "effect"):
                    continue
                if t == "SwitchInfo":
                    spec = PointSpec(key, Kind.BOOL, rw, label=name)
                elif t == "NumberInfo":
                    step = float(getattr(e, "step", 1) or 1)
                    spec = PointSpec(key, Kind.INT if step.is_integer() else Kind.FLOAT, rw,
                                     unit=(getattr(e, "unit_of_measurement", "") or None),
                                     minimum=getattr(e, "min_value", None), maximum=getattr(e, "max_value", None), label=name)
                elif t == "SelectInfo":
                    spec = PointSpec(key, Kind.ENUM, rw, choices=tuple(getattr(e, "options", ()) or ()), label=name)
                elif t == "ButtonInfo":
                    spec = PointSpec(key, Kind.BOOL, wo, label=name)
                elif t == "BinarySensorInfo":
                    spec = PointSpec(key, Kind.BOOL, Access.READ, label=name)
                else:
                    continue
                ent = {"type": t, "key": e.key, "name": name, "point": key}
                specs.append(spec)
                by_point[key] = ent
            by_key[e.key] = ent
        dev = Device(id=dev_id, driver=self.id,
                     native_name=str(conf.get("name") or getattr(info, "friendly_name", "") or info.name)[:64],
                     kind=kind, manufacturer="ESPHome", model=str(getattr(info, "model", "") or "")[:64] or None,
                     native_room=(str(conf["room"]).strip() or None) if conf.get("room") else None,
                     online=False, points={s.key: Point(s) for s in specs})
        dev.native = {"host": str(conf["host"]), "esphome": str(getattr(info, "esphome_version", ""))}
        return dev, by_key, by_point

    @staticmethod
    def _values(ent: dict[str, Any], st: Any) -> dict[str, Any]:
        if ent["type"] == "light":
            out: dict[str, Any] = {"on": bool(st.state)}
            if getattr(st, "brightness", None) is not None:
                out["brightness"] = max(1, round(float(st.brightness) * 100))
            if all(getattr(st, c, None) is not None for c in ("red", "green", "blue")):
                out["color"] = rgb_to_hex(st.red, st.green, st.blue)
            if getattr(st, "effect", None):
                out["effect"] = str(st.effect)
            return out
        if ent["type"] == "ButtonInfo" or getattr(st, "missing_state", False):
            return {}
        v = st.state
        return {ent["point"]: v if not isinstance(v, float) or not v.is_integer() else int(v)}

    async def command(self, device: Device, key: str, value: Any) -> None:
        if self.read_only:
            raise PermissionError("pilote ESPHome en lecture seule")
        node = self._nodes.get(device.id)
        if not node:
            raise OSError("module ESPHome non connecté")
        ent = node["by_point"].get(key)
        if ent is None:
            raise ValueError(f"point inconnu : {key}")
        c = node["client"]
        t = ent["type"]
        if t == "light":
            if key == "on":
                c.light_command(ent["key"], state=bool(value))
            elif key == "brightness":
                c.light_command(ent["key"], state=True, brightness=int(value) / 100)
            elif key == "color":
                c.light_command(ent["key"], state=True, rgb=hex_to_rgb(str(value)))
            elif key == "effect":
                c.light_command(ent["key"], state=True, effect=str(value))
        elif t == "SwitchInfo":
            c.switch_command(ent["key"], bool(value))
        elif t == "NumberInfo":
            c.number_command(ent["key"], float(value))
        elif t == "SelectInfo":
            c.select_command(ent["key"], str(value))
        elif t == "ButtonInfo":
            if value is True:
                c.button_command(ent["key"])
        else:
            raise ValueError(f"{key} : lecture seule")
