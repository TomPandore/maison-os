"""Le serveur MCP : comment un agent (Claude) lit et pilote la maison.

Tout ordre passe par le hub, donc par la garde : dans une pièce protégée, aux heures
calmes ou sur un appareil sans pièce, l'ordre devient une demande qu'une personne valide
avec le code. L'agent ne peut ni valider ses propres demandes, ni voir le code.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from ..core.hub import CommandError, Hub
from ..core.model import Origin
from ..core.scenes import run_scene

INSTRUCTIONS = """Tu pilotes la maison de la famille par ces outils.
- Commence par `house_overview` pour voir les pièces, les appareils et leur état.
- `set_device` donne un ordre (ex. key="on", value=true ; key="brightness", value=40 ;
  key="color_temp", value=2700 ; key="color", value="#ff8800").
- Certains ordres sont retenus par la garde (pièce protégée, heures calmes, appareil sans
  pièce) : la réponse dit « pending » ; une personne doit valider dans le tableau de bord.
  Ne réessaie pas en boucle, dis-le simplement.
- Ne commande jamais un appareil d'une autre pièce que celle demandée."""


def _summary(dev) -> dict[str, Any]:
    vals = {k: p.value for k, p in dev.points.items()}
    writable = [k for k, p in dev.points.items() if p.spec.access.writable]
    return {"id": dev.id, "name": dev.display_name, "kind": dev.kind, "online": dev.online,
            "state": vals, "writable": writable}


def build_mcp(hub: Hub, house_name: str) -> MCPServer:
    server = MCPServer(name=f"{house_name} — maison", instructions=INSTRUCTIONS, version="0.1.0")

    @server.tool()
    async def house_overview() -> dict[str, Any]:
        """L'état de toute la maison : pièces, appareils, valeurs, et ce qui attend une validation."""
        rooms: dict[str, list[dict[str, Any]]] = {r: [] for r in hub.rooms()}
        unassigned: list[dict[str, Any]] = []
        for dev in hub.devices.values():
            if dev.hidden:
                continue
            (rooms.setdefault(dev.effective_room, []) if dev.effective_room else unassigned).append(_summary(dev))
        return {
            "house": house_name,
            "rooms": rooms,
            "unassigned": unassigned,
            "protected_rooms": sorted(hub.guard.protected_rooms),
            "pending_approvals": len(hub.pending()),
            "scenes": [s.to_json() for s in hub.scenes.values()],
        }

    @server.tool()
    async def get_device(device_id: str) -> dict[str, Any]:
        """Le détail d'un appareil : chaque point, son type, ses bornes, s'il s'écrit."""
        dev = hub.devices.get(device_id)
        if dev is None:
            return {"error": "appareil inconnu"}
        return dev.to_json()

    @server.tool()
    async def set_device(device_id: str, key: str, value: Any) -> dict[str, Any]:
        """Donne un ordre à un appareil (passe par la garde). Statuts : done, pending, error."""
        result = await hub.command(device_id, key, value, origin=Origin.MCP, actor="claude")
        return result.to_json()

    @server.tool()
    async def activate_scene(scene_id: str) -> dict[str, Any]:
        """Lance une ambiance (scène) par son id (voir `scenes` dans house_overview). Passe par la garde."""
        sc = hub.scenes.get(scene_id)
        if sc is None:
            return {"error": "ambiance inconnue", "scenes": list(hub.scenes)}
        return await run_scene(hub, sc, origin=Origin.MCP, actor="claude")

    @server.tool()
    async def pending_approvals() -> list[dict[str, Any]]:
        """Les ordres retenus qui attendent la validation d'une personne."""
        return hub.pending()

    @server.tool()
    async def recent_journal(limit: int = 30) -> list[dict[str, Any]]:
        """Les dernières écritures dans la maison (ordres, validations, étiquettes)."""
        return hub.journal.tail(limit)

    @server.tool()
    async def set_label(device_id: str, name: str | None = None, room: str | None = None) -> dict[str, Any]:
        """Renomme un appareil ou le range dans une pièce. Une pièce protégée demande une personne."""
        try:
            dev = hub.set_label(device_id, actor="claude", origin=Origin.MCP, name=name, room=room)
        except CommandError as e:
            return {"error": str(e)}
        return _summary(dev)

    return server
