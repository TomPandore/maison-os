"""Les ambiances (scènes) : un nom, une pièce, et l'état voulu de quelques lumières.

Dans `maison.yaml` :

    scenes:
      - id: bureau-travail
        name: Travail
        room: Bureau
        actions:
          - {device: matter:node2, on: true, brightness: 100, color_temp: 4500}
          - {device: matter:node1, on: false}

Chaque réglage passe par `hub.command` : la garde s'applique comme pour un ordre isolé
(pièce protégée, heures calmes…), et tout est journalisé. Un appareil absent est sauté.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .model import Origin

if TYPE_CHECKING:
    from .hub import Hub

SCENE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
ORDER = ("on", "color_temp", "color", "brightness")  # « on » d'abord ; la luminosité en dernier


@dataclass
class Scene:
    id: str
    name: str
    room: str | None
    actions: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "room": self.room, "devices": [a["device"] for a in self.actions]}


def load_scenes(raw: Any) -> dict[str, Scene]:
    out: dict[str, Scene] = {}
    for s in raw or []:
        if not isinstance(s, dict) or not SCENE_ID.match(str(s.get("id", ""))) or not str(s.get("name", "")).strip():
            continue
        actions = []
        for a in s.get("actions") or []:
            if isinstance(a, dict) and isinstance(a.get("device"), str):
                actions.append({k: v for k, v in a.items() if k == "device" or k in ORDER})
        out[s["id"]] = Scene(s["id"], str(s["name"]).strip()[:40], (str(s["room"]).strip() or None) if s.get("room") else None, actions)
    return out


async def run_scene(hub: Hub, scene: Scene, *, origin: Origin, actor: str) -> dict[str, Any]:
    done, skipped, held, errors = [], [], [], []
    for a in scene.actions:
        dev = hub.devices.get(a["device"])
        if dev is None or not dev.online:
            skipped.append(a["device"])
            continue
        steps = [(k, a[k]) for k in ORDER if k in a and k in dev.points]
        if a.get("on") is False:
            steps = [("on", False)]  # éteindre : le reste ne compte pas
        elif "on" in a and any(k in ("brightness", "color_temp", "color") for k, _ in steps):
            steps = [s for s in steps if s[0] != "on"] + [("on", True)]  # régler, puis s'assurer qu'elle est allumée
        for key, value in steps:
            r = await hub.command(dev.id, key, value, origin=origin, actor=actor)
            (done if r.status == "done" else held if r.status == "pending" else errors).append(f"{dev.display_name} : {key} → {r.message}" if r.status != "done" else dev.id)
    hub.journal.write("scene", scene=scene.id, actor=actor, origin=origin.value,
                      done=len(set(done)), skipped=len(skipped), held=len(held), errors=len(errors))
    return {"scene": scene.id, "status": "error" if errors and not done else "done",
            "skipped": skipped, "held": held, "errors": errors}
