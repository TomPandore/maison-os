"""La garde : décide si un ordre s'exécute ou attend la validation d'une personne.

- Une personne (tableau de bord avec code), un automatisme validé, la maison elle-même :
  toujours obéis.
- Le tableau de bord du foyer sans code : obéi, sauf dans une pièce protégée.
- Un agent (Claude par MCP, un script, Riley) : retenu dans une pièce protégée, pendant
  les heures calmes, ou sur un appareil sans pièce. La demande attend qu'une personne
  l'autorise avec le code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from enum import Enum

from .model import Device, Origin


class Verdict(str, Enum):
    ALLOW = "allow"
    HOLD = "hold"


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str = ""


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


@dataclass
class GuardPolicy:
    protected_rooms: frozenset[str] = frozenset()
    quiet_start: time | None = None  # ex. 21:00
    quiet_end: time | None = None  # ex. 07:30
    hold_unassigned: bool = True

    @classmethod
    def from_config(cls, cfg: dict) -> "GuardPolicy":
        quiet = cfg.get("quiet_hours") or {}
        return cls(
            protected_rooms=frozenset(r.strip().casefold() for r in cfg.get("protected_rooms", []) if r.strip()),
            quiet_start=_parse_hhmm(quiet["start"]) if quiet.get("start") else None,
            quiet_end=_parse_hhmm(quiet["end"]) if quiet.get("end") else None,
            hold_unassigned=bool(cfg.get("hold_unassigned", True)),
        )

    def is_protected(self, room: str | None) -> bool:
        return bool(room) and room.casefold() in self.protected_rooms

    def in_quiet_hours(self, now: datetime) -> bool:
        if not self.quiet_start or not self.quiet_end:
            return False
        t = now.time()
        if self.quiet_start <= self.quiet_end:
            return self.quiet_start <= t < self.quiet_end
        return t >= self.quiet_start or t < self.quiet_end  # passe minuit

    def decide(self, origin: Origin, device: Device, now: datetime) -> Decision:
        if origin in (Origin.UI, Origin.AUTOMATION, Origin.SYSTEM):
            return Decision(Verdict.ALLOW)
        room = device.effective_room
        if origin is Origin.HOUSEHOLD:
            if self.is_protected(room):
                return Decision(Verdict.HOLD, f"pièce protégée ({room}) : le code est demandé")
            return Decision(Verdict.ALLOW)
        # agents
        if self.is_protected(room):
            return Decision(Verdict.HOLD, f"pièce protégée ({room}) : une personne doit valider")
        if self.in_quiet_hours(now):
            return Decision(Verdict.HOLD, "heures calmes : une personne doit valider")
        if self.hold_unassigned and not room:
            return Decision(Verdict.HOLD, "appareil sans pièce : une personne doit valider")
        return Decision(Verdict.ALLOW)
