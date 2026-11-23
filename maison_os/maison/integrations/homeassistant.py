"""Import des pièces et des noms depuis Home Assistant (lecture seule côté HA).

Maison OS lit les registres de HA (pièces, appareils) par l'API WebSocket officielle,
retrouve chaque appareil par ses identifiants (jamais par son nom), et **propose** des
étiquettes. Rien n'est appliqué sans qu'une personne valide avec le code.

En module (add-on) : `homeassistant_api: true` dans `config.yaml` ; le Supervisor donne
alors le jeton `SUPERVISOR_TOKEN` et l'adresse `ws://supervisor/core/websocket`.
Hors module (pour essayer) : `MAISON_HA_TOKEN` (jeton longue durée, jamais dans un fichier
versionné) et `homeassistant.url` dans `data/maison.yaml`.

Inspiration : `tools/home-assistant/ha-areas-import.py` de moli-os (Apache-2.0).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import aiohttp

if TYPE_CHECKING:
    from ..core.hub import Hub

DEFAULT_URL = "ws://supervisor/core/websocket"
TIMEOUT_S = 10.0
MAX_TEXT = 64


class HAError(Exception):
    pass


def ha_token() -> str | None:
    return os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("MAISON_HA_TOKEN") or None


async def fetch_registries(url: str, token: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Lit les pièces (areas) et les appareils du registre HA. Ne modifie rien."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(url, timeout=aiohttp.ClientWSTimeout(ws_receive=TIMEOUT_S), max_msg_size=16 * 1024 * 1024) as ws:
                first = await ws.receive_json(timeout=TIMEOUT_S)
                if first.get("type") != "auth_required":
                    raise HAError("réponse inattendue de Home Assistant")
                await ws.send_json({"type": "auth", "access_token": token})
                auth = await ws.receive_json(timeout=TIMEOUT_S)
                if auth.get("type") != "auth_ok":
                    raise HAError("Home Assistant refuse le jeton (homeassistant_api activé ?)")
                out: list[list[dict[str, Any]]] = []
                for mid, cmd in ((1, "config/area_registry/list"), (2, "config/device_registry/list")):
                    await ws.send_json({"id": mid, "type": cmd})
                    while True:
                        msg = await ws.receive_json(timeout=TIMEOUT_S)
                        if msg.get("id") == mid and msg.get("type") == "result":
                            break
                    if not msg.get("success"):
                        raise HAError(f"{cmd} : {(msg.get('error') or {}).get('message', 'échec')}")
                    out.append([x for x in msg.get("result") or [] if isinstance(x, dict)])
                return out[0], out[1]
    except (aiohttp.ClientError, TimeoutError, OSError, ValueError) as e:
        raise HAError(f"Home Assistant injoignable ({type(e).__name__})") from None


def _clean(s: Any) -> str | None:
    if not isinstance(s, str):
        return None
    s = "".join(c for c in s if ord(c) >= 32).strip()
    return s[:MAX_TEXT] or None


@dataclass
class Proposal:
    device_id: str
    match: str  # "identifiant" | "numéro de série"
    ha_name: str | None
    ha_room: str | None
    current_name: str
    current_room: str | None
    name: str | None = None  # étiquette proposée (None = rien à changer)
    room: str | None = None

    @property
    def changes(self) -> bool:
        return self.name is not None or self.room is not None

    def to_json(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id, "match": self.match,
            "ha": {"name": self.ha_name, "room": self.ha_room},
            "current": {"name": self.current_name, "room": self.current_room},
            "proposed": {"name": self.name, "room": self.room},
            "changes": self.changes,
        }


@dataclass
class ImportPlan:
    proposals: list[Proposal] = field(default_factory=list)
    unmatched_devices: list[str] = field(default_factory=list)  # appareils Maison sans équivalent HA
    ha_devices_not_in_maison: int = 0  # appareils HA que Maison ne connaît pas (encore)
    ha_rooms: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "proposals": [p.to_json() for p in self.proposals],
            "unmatched_devices": self.unmatched_devices,
            "ha_devices_not_in_maison": self.ha_devices_not_in_maison,
            "ha_rooms": self.ha_rooms,
        }


def plan_import(hub: "Hub", areas: list[dict[str, Any]], ha_devices: list[dict[str, Any]]) -> ImportPlan:
    """Fait correspondre les appareils de Maison à ceux de HA et propose nom et pièce.

    Identité d'abord (`deviceid_…`), puis le numéro de série s'il est unique des deux côtés.
    Un appareil HA désactivé ou un service (`entry_type: service`) est ignoré."""
    area_names = {a.get("area_id"): _clean(a.get("name")) for a in areas}
    strong: dict[tuple[str, str], dict[str, Any]] = {}
    weak: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for d in ha_devices:
        if d.get("disabled_by") or d.get("entry_type") == "service":
            continue
        for ident in d.get("identifiers") or []:
            if not (isinstance(ident, (list, tuple)) and len(ident) == 2):
                continue
            key = (str(ident[0]), str(ident[1]))
            if key[1].startswith("serial_"):
                weak.setdefault(key, []).append(d)
            else:
                strong[key] = d

    plan = ImportPlan(ha_rooms=sorted(n for n in area_names.values() if n))
    matched_ha: set[str] = set()
    weak_used: dict[tuple[str, str], int] = {}
    candidates: list[tuple[Any, dict[str, Any] | None, str]] = []
    for dev in hub.devices.values():
        driver = hub.drivers.get(dev.driver)
        ids = driver.ha_identifiers(dev) if driver else {}
        found, how = None, ""
        for i in ids.get("strong", set()):
            if (dev.driver, i) in strong:
                found, how = strong[(dev.driver, i)], "identifiant"
                break
        if found is None:
            for i in ids.get("weak", set()):
                hits = weak.get((dev.driver, i), [])
                if len(hits) == 1:
                    weak_used[(dev.driver, i)] = weak_used.get((dev.driver, i), 0) + 1
                    found, how = hits[0], "numéro de série"
                    break
        candidates.append((dev, found, how))

    # un numéro de série porté par deux appareils de Maison n'identifie personne
    ambiguous = {k for k, n in weak_used.items() if n > 1}
    for dev, found, how in candidates:
        if found is not None and how == "numéro de série":
            ids = hub.drivers[dev.driver].ha_identifiers(dev).get("weak", set())
            if any((dev.driver, i) in ambiguous for i in ids):
                found = None
        if found is None:
            plan.unmatched_devices.append(dev.id)
            continue
        matched_ha.add(str(found.get("id")))
        ha_name = _clean(found.get("name_by_user")) or _clean(found.get("name"))
        ha_room = area_names.get(found.get("area_id"))
        p = Proposal(dev.id, how, ha_name, ha_room, dev.display_name, dev.effective_room)
        if ha_name and ha_name != dev.display_name:
            p.name = ha_name
        if ha_room and ha_room != dev.effective_room:
            p.room = ha_room
        plan.proposals.append(p)

    plan.ha_devices_not_in_maison = sum(
        1 for d in ha_devices
        if not d.get("disabled_by") and d.get("entry_type") != "service" and str(d.get("id")) not in matched_ha
    )
    return plan
