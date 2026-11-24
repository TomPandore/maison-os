"""Pilote Tuya en LOCAL (Wi-Fi de la maison, sans le cloud), avec `tinytuya`.

Les clés locales viennent de `<données>/tuya.json` (export fait une fois via le QR Smart Life,
comme Home Assistant ; voir le BRIEF). Le pilote trouve l'adresse de chaque appareil par la
découverte Tuya (UDP), lit son état toutes les quelques secondes et envoie les ordres.

Lumières Tuya « v2 » (catégories dd, dj, xdd, fwd…), points de données :
  20 allumé · 21 mode (white/colour/scene/music) · 22 luminosité 10-1000 ·
  23 température 0 (chaud) - 1000 (froid) · 24 couleur « hhhhssssvvvv » (h 0-360, s/v 0-1000)
"""

from __future__ import annotations

import asyncio
import colorsys
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from ..core.model import Access, Device, Kind, Point, PointSpec, now_ms
from .base import Driver

if TYPE_CHECKING:
    from ..core.hub import Hub

log = logging.getLogger("maison.tuya")
LIGHT_CATEGORIES = {"dd", "dj", "xdd", "fwd", "dc", "fsd", "tgq", "tyndj"}
K_MIN, K_MAX = 2000, 6500  # comme Home Assistant pour ces lumières


# ---------- conversions (pures, testées) ----------

def bright_to_pct(v: Any) -> int | None:
    return None if not isinstance(v, (int, float)) else max(0, min(100, round((v - 10) / 990 * 100)))


def pct_to_bright(p: float) -> int:
    return int(round(10 + max(0, min(100, p)) / 100 * 990))


def temp_to_kelvin(t: Any) -> int | None:
    return None if not isinstance(t, (int, float)) else round(K_MIN + max(0, min(1000, t)) / 1000 * (K_MAX - K_MIN))


def kelvin_to_temp(k: float) -> int:
    return int(round((max(K_MIN, min(K_MAX, k)) - K_MIN) / (K_MAX - K_MIN) * 1000))


def parse_colour(s: Any) -> tuple[int, int, int] | None:
    """« hhhhssssvvvv » → (h 0-360, s 0-1000, v 0-1000)."""
    if not isinstance(s, str) or len(s) != 12:
        return None
    try:
        return int(s[0:4], 16), int(s[4:8], 16), int(s[8:12], 16)
    except ValueError:
        return None


def make_colour(h: int, s: int, v: int) -> str:
    return f"{h % 361:04x}{max(0, min(1000, s)):04x}{max(10, min(1000, v)):04x}"


def hsv_to_hex(h: int, s: int) -> str:
    r, g, b = colorsys.hsv_to_rgb(h / 360, s / 1000, 1.0)
    return "#{:02x}{:02x}{:02x}".format(round(r * 255), round(g * 255), round(b * 255))


def hex_to_hs(color: str) -> tuple[int, int]:
    c = color.lstrip("#")
    r, g, b = (int(c[i:i + 2], 16) / 255 for i in (0, 2, 4))
    h, s, _ = colorsys.rgb_to_hsv(r, g, b)
    return round(h * 360), round(s * 1000)


def dps_to_values(dps: dict[str, Any]) -> dict[str, Any]:
    vals: dict[str, Any] = {}
    if "20" in dps:
        vals["on"] = bool(dps["20"])
    if dps.get("21") == "colour" and parse_colour(dps.get("24")):
        h, s, v = parse_colour(dps["24"])  # type: ignore[misc]
        vals.update(color=hsv_to_hex(h, s), color_mode="hs", brightness=max(1, round(v / 10)))
    else:
        if "22" in dps:
            vals["brightness"] = bright_to_pct(dps["22"])
        if "23" in dps:
            vals["color_temp"] = temp_to_kelvin(dps["23"])
        vals.update(color_mode="ct", color=None)
    return vals


def command_to_dps(key: str, value: Any, current: dict[str, Any]) -> dict[str, Any]:
    """L'ordre de Maison (déjà validé) → les points de données Tuya à écrire."""
    colour = current.get("21") == "colour" and parse_colour(current.get("24"))
    if key == "on":
        return {"20": bool(value)}
    if key == "brightness":
        if colour:
            h, s, _ = colour  # type: ignore[misc]
            return {"20": True, "24": make_colour(h, s, int(value) * 10)}
        return {"20": True, "21": "white", "22": pct_to_bright(value)}
    if key == "color_temp":
        out: dict[str, Any] = {"20": True, "21": "white", "23": kelvin_to_temp(value)}
        if colour:  # garder la luminosité perçue en quittant la couleur
            out["22"] = pct_to_bright(colour[2] / 10)
        return out
    if key == "color":
        h, s = hex_to_hs(str(value))
        v = colour[2] if colour else pct_to_bright(bright_to_pct(current.get("22")) or 100)
        return {"20": True, "21": "colour", "24": make_colour(h, s, v)}
    raise ValueError(f"point inconnu : {key}")


# ---------- pilote ----------

class TuyaDriver(Driver):
    def __init__(self, keys_file: Path, id: str = "tuya", read_only: bool = True, poll_s: float = 5.0,
                 device_factory: Callable[..., Any] | None = None, scanner: Callable[[], dict[str, Any]] | None = None):
        self.id = id
        self.keys_file = keys_file
        self.read_only = read_only
        self.poll_s = poll_s
        self._factory = device_factory
        self._scanner = scanner
        self._hub: Hub | None = None
        self._conf: dict[str, dict[str, Any]] = {}   # device_id → {local_key, name, ip, version}
        self._dps: dict[str, dict[str, Any]] = {}    # device_id → derniers points lus
        self._fails: dict[str, int] = {}

    def _new(self, conf: dict[str, Any]) -> Any:
        if self._factory:
            return self._factory(conf)
        import tinytuya
        d = tinytuya.Device(conf["id"], conf["ip"], conf["local_key"], version=float(conf.get("version") or 3.3))
        d.set_socketTimeout(4)
        d.set_socketRetryLimit(1)
        return d

    def _scan(self) -> dict[str, Any]:
        if self._scanner:
            return self._scanner()
        import tinytuya
        found = tinytuya.deviceScan(verbose=False, maxretry=10)
        return {d.get("gwId"): {"ip": ip, "version": d.get("version")} for ip, d in found.items() if d.get("gwId")}

    async def _locate(self) -> None:
        found = await asyncio.to_thread(self._scan)
        for tid, conf in self._conf.items():
            hit = found.get(conf["id"])
            if hit:
                conf.update(ip=hit["ip"], version=hit.get("version") or conf.get("version"))
                log.info("Tuya %s trouvé en %s (v%s)", conf["name"], hit["ip"], conf.get("version"))

    def _build(self, tid: str, conf: dict[str, Any]) -> Device:
        rw = Access.READ if self.read_only else Access.READ_WRITE
        specs = [
            PointSpec("on", Kind.BOOL, rw, label="Allumé"),
            PointSpec("brightness", Kind.INT, rw, unit="%", minimum=1, maximum=100, label="Luminosité"),
            PointSpec("color_temp", Kind.INT, rw, unit="K", minimum=K_MIN, maximum=K_MAX, label="Température de blanc", clamp=True),
            PointSpec("color", Kind.COLOR, rw, label="Couleur"),
            PointSpec("color_mode", Kind.ENUM, Access.READ, choices=("hs", "xy", "ct"), label="Mode de couleur"),
        ]
        dev = Device(id=tid, driver=self.id, native_name=str(conf.get("name") or "Lumière Tuya")[:64], kind="light",
                     manufacturer="Tuya", model=str(conf.get("product") or conf.get("name") or "")[:64] or None,
                     online=False, points={s.key: Point(s) for s in specs})
        dev.native = {"tuya_id": conf["id"]}
        return dev

    async def run(self, hub: Hub) -> None:
        self._hub = hub
        data = json.loads(self.keys_file.read_text(encoding="utf-8"))
        for d in data.get("devices", []):
            if d.get("category") in LIGHT_CATEGORIES and d.get("local_key"):
                tid = f"{self.id}:{d['id']}"
                self._conf[tid] = {"id": d["id"], "local_key": d["local_key"], "name": d.get("name"),
                                   "product": d.get("product"), "ip": d.get("ip"), "version": d.get("version")}
                hub.upsert_device(self._build(tid, self._conf[tid]))
        if not self._conf:
            log.warning("aucune lumière Tuya avec clé locale dans %s", self.keys_file)
        await self._locate()
        last_scan = asyncio.get_running_loop().time()
        while True:
            for tid, conf in self._conf.items():
                await self._poll(tid, conf)
            now = asyncio.get_running_loop().time()
            if any(self._fails.get(t, 0) >= 3 for t in self._conf) and now - last_scan > 60:
                await self._locate()  # adresse changée (DHCP) : on la retrouve
                last_scan = now
            await asyncio.sleep(self.poll_s)

    async def _poll(self, tid: str, conf: dict[str, Any]) -> None:
        assert self._hub
        if not conf.get("ip"):
            self._hub.set_online(tid, False)
            return
        try:
            st = await asyncio.to_thread(lambda: self._new(conf).status())
            dps = st.get("dps") if isinstance(st, dict) else None
            if not dps:
                raise OSError(str(st)[:120])
        except Exception as e:  # noqa: BLE001 — appareil injoignable : hors ligne, on réessaie
            self._fails[tid] = self._fails.get(tid, 0) + 1
            if self._fails[tid] >= 3:
                self._hub.set_online(tid, False)
            log.debug("Tuya %s : %s", conf.get("name"), e)
            return
        self._fails[tid] = 0
        self._dps[tid] = {**self._dps.get(tid, {}), **dps}
        self._hub.set_online(tid, True)
        for key, value in dps_to_values(self._dps[tid]).items():
            dev = self._hub.devices.get(tid)
            if dev and key in dev.points and dev.points[key].value != value:
                self._hub.update_point(tid, key, value)

    async def command(self, device: Device, key: str, value: Any) -> None:
        if self.read_only:
            raise PermissionError("pilote Tuya en lecture seule")
        conf = self._conf.get(device.id)
        if not conf or not conf.get("ip"):
            raise OSError("adresse de l'appareil inconnue")
        dps = command_to_dps(key, value, self._dps.get(device.id, {}))
        res = await asyncio.to_thread(lambda: self._new(conf).set_multiple_values(dps))
        if isinstance(res, dict) and res.get("Error"):
            raise OSError(str(res.get("Error")))
        self._dps[device.id] = {**self._dps.get(device.id, {}), **dps}
        assert self._hub
        for k, v in dps_to_values(self._dps[device.id]).items():
            self._hub.update_point(device.id, k, v)
