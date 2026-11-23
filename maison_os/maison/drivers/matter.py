"""Matter, par le Matter Server de la maison (le même que Home Assistant).

On ne réimplémente pas Matter : le réseau Matter (le « fabric ») est tenu par le Matter
Server (Open Home Foundation, matter.js), qui tourne à part. Ce pilote lui parle par son
API WebSocket, comme le fait Home Assistant. Les appareils déjà appairés marchent sans être
réappairés, et plusieurs clients peuvent parler au même serveur.

Messages (schéma >= 11) :
- à la connexion, le serveur envoie ses informations (`schema_version`, `sdk_version`…) ;
- `start_listening` → la liste des nœuds, puis les événements (`attribute_updated`,
  `node_added`, `node_updated`, `node_removed`) ;
- `device_command` → une commande de cluster sur un point de terminaison.

Attributs : chemin `"<endpoint>/<cluster>/<attribut>"`.
"""

from __future__ import annotations

import asyncio
import colorsys
import itertools
import logging
from typing import TYPE_CHECKING, Any

import aiohttp

from ..core.model import Access, Device, Kind, Point, PointSpec, now_ms
from .base import Driver

if TYPE_CHECKING:
    from ..core.hub import Hub

log = logging.getLogger("maison.matter")

MIN_SCHEMA = 11

# clusters
BASIC_INFO = 40
BRIDGED_BASIC_INFO = 57
DESCRIPTOR = 29
ON_OFF = 6
LEVEL = 8
COLOR = 768

# types d'appareils Matter (Descriptor/DeviceTypeList)
LIGHT_TYPES = {256, 257, 268, 269}  # on/off, variable, température, couleur étendue
PLUG_TYPES = {266, 267}  # prise on/off, prise variable

# ColorControl : bits de FeatureMap
FEAT_HS = 1
FEAT_XY = 8
FEAT_CT = 16
COLOR_MODES = {0: "hs", 1: "xy", 2: "ct"}
OPTIONS_EXECUTE_IF_OFF = {"optionsMask": 1, "optionsOverride": 1}


class MatterError(Exception):
    pass


def _attr(node: dict[str, Any], ep: int, cluster: int, attr: int, default: Any = None) -> Any:
    return node.get("attributes", {}).get(f"{ep}/{cluster}/{attr}", default)


def _has_cluster(node: dict[str, Any], ep: int, cluster: int) -> bool:
    prefix = f"{ep}/{cluster}/"
    return any(k.startswith(prefix) for k in node.get("attributes", {}))


def _device_types(node: dict[str, Any], ep: int) -> set[int]:
    out: set[int] = set()
    for entry in _attr(node, ep, DESCRIPTOR, 0, []) or []:
        if isinstance(entry, dict):
            dt = entry.get("0", entry.get("deviceType"))
            if isinstance(dt, int):
                out.add(dt)
    return out


def _endpoints_with(node: dict[str, Any], cluster: int) -> list[int]:
    eps = set()
    for k in node.get("attributes", {}):
        ep, cl, _ = k.split("/", 2)
        if int(cl) == cluster and int(ep) != 0:
            eps.add(int(ep))
    return sorted(eps)


def level_to_pct(level: Any) -> int | None:
    if not isinstance(level, (int, float)):
        return None
    return max(0, min(100, round(level * 100 / 254)))


def pct_to_level(pct: float, min_level: int = 1, max_level: int = 254) -> int:
    level = round(pct * 254 / 100)
    return max(min_level, min(max_level, level))


def mireds_to_kelvin(m: Any) -> int | None:
    if not isinstance(m, (int, float)) or m <= 0:
        return None
    return round(1_000_000 / m)


def kelvin_to_mireds(k: float) -> int:
    return round(1_000_000 / k)


def _rgb_hex(r: float, g: float, b: float) -> str:
    return "#{:02x}{:02x}{:02x}".format(*(max(0, min(255, round(c * 255))) for c in (r, g, b)))


def hs_to_hex(hue_254: Any, sat_254: Any) -> str | None:
    if not isinstance(hue_254, (int, float)) or not isinstance(sat_254, (int, float)):
        return None
    r, g, b = colorsys.hsv_to_rgb(hue_254 / 254, sat_254 / 254, 1.0)
    return _rgb_hex(r, g, b)


def xy_to_hex(x_raw: Any, y_raw: Any) -> str | None:
    if not isinstance(x_raw, (int, float)) or not isinstance(y_raw, (int, float)) or y_raw == 0:
        return None
    x, y = x_raw / 65535, y_raw / 65535
    z = 1.0 - x - y
    Y = 1.0
    X, Z = (Y / y) * x, (Y / y) * z
    r = X * 1.656492 - Y * 0.354851 - Z * 0.255038
    g = -X * 0.707196 + Y * 1.655397 + Z * 0.036152
    b = X * 0.051713 - Y * 0.121364 + Z * 1.011530

    def gamma(c: float) -> float:
        c = max(0.0, c)
        return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055

    r, g, b = gamma(r), gamma(g), gamma(b)
    m = max(r, g, b, 1e-9)
    return _rgb_hex(r / m, g / m, b / m)


def hex_to_hs(color: str) -> tuple[int, int]:
    r, g, b = (int(color[i : i + 2], 16) / 255 for i in (1, 3, 5))
    h, s, _ = colorsys.rgb_to_hsv(r, g, b)
    return round(h * 254) % 255, round(s * 254)


def hex_to_xy(color: str) -> tuple[int, int]:
    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (lin(int(color[i : i + 2], 16) / 255) for i in (1, 3, 5))
    X = r * 0.664511 + g * 0.154324 + b * 0.162028
    Y = r * 0.283881 + g * 0.668433 + b * 0.047685
    Z = r * 0.000088 + g * 0.072310 + b * 0.986039
    total = X + Y + Z
    if total == 0:
        return 20495, 21561  # blanc D65 approx.
    return round(X / total * 65535), round(Y / total * 65535)


class MatterDriver(Driver):
    def __init__(self, id: str = "matter", url: str = "ws://core-matter-server:5580/ws",
                 read_only: bool = True, transition_s: float = 0.5):
        self.id = id
        self.url = url
        self.read_only = read_only
        self.transition = max(0, min(600, round(transition_s * 10)))  # en dixièmes de seconde
        self.server_info: dict[str, Any] = {}
        self.nodes: dict[int, dict[str, Any]] = {}
        self._devices: dict[tuple[int, int], str] = {}  # (nœud, endpoint) → device_id
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._ids = itertools.count(1)
        self._hub: Hub | None = None

    # ---------- connexion ----------

    async def run(self, hub: "Hub") -> None:
        self._hub = hub
        self._session = aiohttp.ClientSession()
        try:
            async with self._session.ws_connect(self.url, heartbeat=30, max_msg_size=32 * 1024 * 1024) as ws:
                self._ws = ws
                first = await ws.receive_json(timeout=15)
                self.server_info = first
                schema = int(first.get("schema_version", 0))
                if schema < MIN_SCHEMA:
                    raise MatterError(f"Matter Server trop ancien (schéma {schema} < {MIN_SCHEMA})")
                log.info("Matter Server %s (schéma %s)", first.get("sdk_version"), schema)
                listener = asyncio.create_task(self._listen(ws))
                try:
                    nodes = await self._call("start_listening", timeout=60)
                    for node in nodes or []:
                        self._apply_node(node)
                    await listener  # rend la main quand la connexion tombe
                finally:
                    listener.cancel()
                raise MatterError("connexion au Matter Server perdue")
        finally:
            self._ws = None
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(MatterError("connexion perdue"))
            self._pending.clear()
            await self._session.close()
            self._session = None

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()

    async def _listen(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for msg in ws:
            if msg.type is not aiohttp.WSMsgType.TEXT:
                if msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE):
                    return
                continue
            try:
                data = msg.json()
            except ValueError:
                continue
            if "message_id" in data:
                fut = self._pending.pop(str(data["message_id"]), None)
                if fut and not fut.done():
                    if "error_code" in data:
                        fut.set_exception(MatterError(f"{data.get('details') or 'erreur'} (code {data['error_code']})"))
                    else:
                        fut.set_result(data.get("result"))
            elif "event" in data:
                if data["event"] == "server_shutdown":
                    log.warning("le Matter Server s'arrête")
                    return
                try:
                    self._on_event(data["event"], data.get("data"))
                except Exception:  # noqa: BLE001 — un événement bizarre ne coupe pas l'écoute
                    log.exception("événement Matter mal formé : %s", data.get("event"))

    async def _call(self, command: str, args: dict[str, Any] | None = None, timeout: float = 10) -> Any:
        if self._ws is None or self._ws.closed:
            raise MatterError("pas connecté au Matter Server")
        mid = str(next(self._ids))
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        msg: dict[str, Any] = {"message_id": mid, "command": command}
        if args is not None:
            msg["args"] = args
        await self._ws.send_json(msg)
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(mid, None)

    # ---------- événements ----------

    def _on_event(self, event: str, data: Any) -> None:
        if event == "attribute_updated" and isinstance(data, list) and len(data) == 3:
            node_id, path, value = data
            node = self.nodes.get(int(node_id))
            if node is None:
                return
            node.setdefault("attributes", {})[path] = value
            ep = int(str(path).split("/", 1)[0])
            dev_id = self._devices.get((int(node_id), ep))
            if dev_id:
                self._refresh(node, ep, dev_id)
            elif ep == 0:
                for (nid, e), did in self._devices.items():
                    if nid == int(node_id):
                        self._refresh(node, e, did)
        elif event in ("node_added", "node_updated") and isinstance(data, dict):
            self._apply_node(data)
        elif event == "node_removed":
            node_id = int(data)
            self.nodes.pop(node_id, None)
            for key in [k for k in self._devices if k[0] == node_id]:
                assert self._hub
                self._hub.remove_device(self._devices.pop(key))

    # ---------- nœuds → appareils ----------

    def _unique_id(self, node: dict[str, Any], ep: int, multi: bool) -> str:
        uid = _attr(node, ep, BRIDGED_BASIC_INFO, 18) if node.get("is_bridge") else None
        if not uid:
            uid = _attr(node, 0, BASIC_INFO, 18)
            if uid and multi:
                uid = f"{uid}-{ep}"
        if not uid:
            uid = f"node{node['node_id']}-{ep}" if multi else f"node{node['node_id']}"
        return f"{self.id}:{str(uid).replace('/', '_')}"

    def _apply_node(self, node: dict[str, Any]) -> None:
        assert self._hub
        node_id = int(node["node_id"])
        self.nodes[node_id] = node
        eps = _endpoints_with(node, ON_OFF)
        for ep in eps:
            dev = self._build_device(node, ep, multi=len(eps) > 1)
            self._devices[(node_id, ep)] = dev.id
            self._hub.upsert_device(dev)

    def _build_device(self, node: dict[str, Any], ep: int, multi: bool) -> Device:
        bridged = bool(node.get("is_bridge")) and _has_cluster(node, ep, BRIDGED_BASIC_INFO)
        info_ep, info_cl = (ep, BRIDGED_BASIC_INFO) if bridged else (0, BASIC_INFO)
        name = _attr(node, info_ep, info_cl, 5) or _attr(node, info_ep, info_cl, 3) or f"Matter {node['node_id']}"
        if multi and not bridged:
            name = f"{name} ({ep})"
        types = _device_types(node, ep)
        has_level, has_color = _has_cluster(node, ep, LEVEL), _has_cluster(node, ep, COLOR)
        if types & LIGHT_TYPES or (not types & PLUG_TYPES and (has_level or has_color)):
            kind = "light"
        else:
            kind = "switch"
        rw = Access.READ if self.read_only else Access.READ_WRITE
        specs = [PointSpec("on", Kind.BOOL, rw, label="Allumé")]
        if has_level:
            specs.append(PointSpec("brightness", Kind.INT, rw, unit="%", minimum=0, maximum=100, label="Luminosité"))
        if has_color:
            feats = _attr(node, ep, COLOR, 65532, 0) or 0
            if feats & FEAT_CT:
                k_max = mireds_to_kelvin(_attr(node, ep, COLOR, 16395)) or 6500
                k_min = mireds_to_kelvin(_attr(node, ep, COLOR, 16396)) or 2000
                specs.append(PointSpec("color_temp", Kind.INT, rw, unit="K", minimum=k_min, maximum=k_max, label="Température de blanc", clamp=True))
            if feats & (FEAT_HS | FEAT_XY):
                specs.append(PointSpec("color", Kind.COLOR, rw, label="Couleur"))
            specs.append(PointSpec("color_mode", Kind.ENUM, Access.READ, choices=("hs", "xy", "ct"), label="Mode de couleur"))
        dev = Device(
            id=self._unique_id(node, ep, multi),
            driver=self.id,
            native_name=str(name)[:64],
            kind=kind,
            manufacturer=_attr(node, info_ep, info_cl, 1),
            model=_attr(node, info_ep, info_cl, 3),
            online=bool(node.get("available", True)),
            points={s.key: Point(s) for s in specs},
        )
        dev.native = {"node_id": int(node["node_id"]), "endpoint": ep}
        ts = now_ms()
        for key, value in self._values(node, ep, dev).items():
            dev.points[key].value = value
            dev.points[key].updated_ms = ts
        return dev

    def _values(self, node: dict[str, Any], ep: int, dev: Device) -> dict[str, Any]:
        vals: dict[str, Any] = {}
        onoff = _attr(node, ep, ON_OFF, 0)
        vals["on"] = bool(onoff) if onoff is not None else None
        if "brightness" in dev.points:
            vals["brightness"] = level_to_pct(_attr(node, ep, LEVEL, 0))
        if "color_mode" in dev.points:
            mode = COLOR_MODES.get(_attr(node, ep, COLOR, 8))
            vals["color_mode"] = mode
            if "color_temp" in dev.points:
                vals["color_temp"] = mireds_to_kelvin(_attr(node, ep, COLOR, 7))
            if "color" in dev.points:
                if mode == "hs":
                    vals["color"] = hs_to_hex(_attr(node, ep, COLOR, 0), _attr(node, ep, COLOR, 1))
                elif mode == "xy":
                    vals["color"] = xy_to_hex(_attr(node, ep, COLOR, 3), _attr(node, ep, COLOR, 4))
                else:
                    vals["color"] = None  # en blanc (température), pas de couleur
        return vals

    def _refresh(self, node: dict[str, Any], ep: int, dev_id: str) -> None:
        assert self._hub
        dev = self._hub.devices.get(dev_id)
        if dev is None:
            return
        self._hub.set_online(dev_id, bool(node.get("available", True)))
        for key, value in self._values(node, ep, dev).items():
            self._hub.update_point(dev_id, key, value)

    # ---------- correspondance avec Home Assistant ----------

    def ha_identifiers(self, device: Device) -> dict[str, set[str]]:
        """Comme l'intégration Matter de HA (`matter/helpers.py`, `adapter.py`) :
        `deviceid_<fabric compressé, 16 hex>-<nœud, 16 hex>-MatterNodeDevice` (ou le numéro
        d'endpoint pour un appareil ponté) et `serial_<numéro de série>`."""
        ref = device.native
        node = self.nodes.get(ref.get("node_id", -1))
        if node is None:
            return {}
        node_id, ep = int(node["node_id"]), int(ref["endpoint"])
        bridged = bool(node.get("is_bridge")) and _has_cluster(node, ep, BRIDGED_BASIC_INFO)
        postfix = str(ep) if bridged else "MatterNodeDevice"
        strong: set[str] = set()
        fabric = self.server_info.get("compressed_fabric_id")
        if isinstance(fabric, int):
            strong.add(f"deviceid_{fabric:016X}-{node_id:016X}-{postfix}")
        serial = _attr(node, ep, BRIDGED_BASIC_INFO, 15) if bridged else _attr(node, 0, BASIC_INFO, 15)
        weak = {f"serial_{serial}"} if serial else set()
        return {"strong": strong, "weak": weak}

    # ---------- ordres ----------

    async def _device_command(self, node_id: int, ep: int, cluster: int, name: str, payload: dict[str, Any] | None = None) -> None:
        await self._call("device_command", {
            "node_id": node_id, "endpoint_id": ep, "cluster_id": cluster,
            "command_name": name, "payload": payload or {},
            "response_type": None, "timed_request_timeout_ms": None, "interaction_timeout_ms": None,
        }, timeout=4.5)

    async def command(self, device: Device, key: str, value: Any) -> None:
        if self.read_only:
            raise MatterError("Matter est en lecture seule (read_only)")
        ref = device.native
        if "node_id" not in ref:
            raise MatterError("appareil Matter inconnu")
        node_id, ep = ref["node_id"], ref["endpoint"]
        node = self.nodes.get(node_id, {})
        is_on = bool(_attr(node, ep, ON_OFF, 0))
        if key == "on":
            await self._device_command(node_id, ep, ON_OFF, "On" if value else "Off")
        elif key == "brightness":
            if value <= 0:
                await self._device_command(node_id, ep, ON_OFF, "Off")
                return
            lo = _attr(node, ep, LEVEL, 2) or 1
            hi = _attr(node, ep, LEVEL, 3) or 254
            await self._device_command(node_id, ep, LEVEL, "MoveToLevelWithOnOff", {
                "level": pct_to_level(value, lo, hi), "transitionTime": self.transition,
                "optionsMask": 0, "optionsOverride": 0,
            })
        elif key == "color_temp":
            if not is_on:
                await self._device_command(node_id, ep, ON_OFF, "On")
            await self._device_command(node_id, ep, COLOR, "MoveToColorTemperature", {
                "colorTemperatureMireds": kelvin_to_mireds(value), "transitionTime": self.transition,
                **OPTIONS_EXECUTE_IF_OFF,
            })
        elif key == "color":
            if not is_on:
                await self._device_command(node_id, ep, ON_OFF, "On")
            feats = _attr(node, ep, COLOR, 65532, 0) or 0
            if feats & FEAT_HS:
                hue, sat = hex_to_hs(value)
                await self._device_command(node_id, ep, COLOR, "MoveToHueAndSaturation", {
                    "hue": hue, "saturation": sat, "transitionTime": self.transition, **OPTIONS_EXECUTE_IF_OFF,
                })
            else:
                x, y = hex_to_xy(value)
                await self._device_command(node_id, ep, COLOR, "MoveToColor", {
                    "colorX": x, "colorY": y, "transitionTime": self.transition, **OPTIONS_EXECUTE_IF_OFF,
                })
        else:
            raise MatterError(f"point « {key} » non pilotable")
