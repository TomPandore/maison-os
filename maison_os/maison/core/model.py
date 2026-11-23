"""Le vocabulaire de la maison : appareils, points, origines.

Règles :
- l'identité d'un appareil ne vient jamais de son nom : `<pilote>:<id natif stable>` ;
- le nom et la pièce sont des étiquettes, modifiables sans toucher à l'identité ;
- chaque valeur est typée et porte son unité.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Kind(str, Enum):
    BOOL = "bool"
    INT = "int"
    FLOAT = "float"
    TEXT = "text"
    COLOR = "color"  # "#rrggbb"
    ENUM = "enum"


class Access(str, Enum):
    READ = "r"
    WRITE = "w"
    READ_WRITE = "rw"

    @property
    def writable(self) -> bool:
        return self in (Access.WRITE, Access.READ_WRITE)


class Origin(str, Enum):
    """Qui donne l'ordre. La garde décide selon l'origine."""

    UI = "ui"  # une personne dans le tableau de bord, avec le code
    HOUSEHOLD = "household"  # le tableau de bord du foyer, sans code
    AUTOMATION = "automation"  # un automatisme validé par une personne
    SYSTEM = "system"  # la maison elle-même
    MCP = "mcp"  # un agent (Claude) par MCP
    API = "api"  # un script
    ASSISTANT = "assistant"  # Riley

    @property
    def is_agent(self) -> bool:
        return self in (Origin.MCP, Origin.API, Origin.ASSISTANT)


MAX_TEXT = 4000


@dataclass(frozen=True)
class PointSpec:
    key: str
    kind: Kind
    access: Access = Access.READ
    unit: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    label: str | None = None
    clamp: bool = False  # ramener dans les bornes au lieu de refuser (ex. température de blanc)

    def validate(self, value: Any) -> Any:
        """Valide et normalise une valeur à écrire. Lève ValueError sinon."""
        k = self.kind
        if k is Kind.BOOL:
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.lower() in ("on", "true", "1", "allume", "allumé"):
                return True
            if isinstance(value, str) and value.lower() in ("off", "false", "0", "eteint", "éteint"):
                return False
            raise ValueError(f"{self.key} attend vrai/faux")
        if k in (Kind.INT, Kind.FLOAT):
            if isinstance(value, bool):
                raise ValueError(f"{self.key} attend un nombre")
            try:
                num = float(value)
            except (TypeError, ValueError):
                raise ValueError(f"{self.key} attend un nombre") from None
            if num != num:  # NaN
                raise ValueError(f"{self.key} attend un nombre")
            if self.clamp:
                if self.minimum is not None:
                    num = max(num, self.minimum)
                if self.maximum is not None:
                    num = min(num, self.maximum)
            if self.minimum is not None and num < self.minimum:
                raise ValueError(f"{self.key} : minimum {self.minimum:g}{self.unit or ''}")
            if self.maximum is not None and num > self.maximum:
                raise ValueError(f"{self.key} : maximum {self.maximum:g}{self.unit or ''}")
            return int(round(num)) if k is Kind.INT else num
        if k is Kind.COLOR:
            if not isinstance(value, str):
                raise ValueError(f"{self.key} attend une couleur #rrggbb")
            v = value.strip().lower()
            if len(v) != 7 or v[0] != "#" or any(c not in "0123456789abcdef" for c in v[1:]):
                raise ValueError(f"{self.key} attend une couleur #rrggbb")
            return v
        if k is Kind.ENUM:
            if value not in self.choices:
                raise ValueError(f"{self.key} : choix possibles {', '.join(self.choices)}")
            return value
        if k is Kind.TEXT:
            if not isinstance(value, str):
                raise ValueError(f"{self.key} attend un texte")
            if len(value) > MAX_TEXT:
                raise ValueError(f"{self.key} : texte trop long")
            return value
        raise ValueError(f"type inconnu {k}")

    def to_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {"kind": self.kind.value, "access": self.access.value}
        if self.unit:
            d["unit"] = self.unit
        if self.minimum is not None:
            d["min"] = self.minimum
        if self.maximum is not None:
            d["max"] = self.maximum
        if self.choices:
            d["choices"] = list(self.choices)
        if self.label:
            d["label"] = self.label
        return d


@dataclass
class Point:
    spec: PointSpec
    value: Any = None
    updated_ms: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {**self.spec.to_json(), "value": self.value, "updated": self.updated_ms}


@dataclass
class Device:
    id: str  # "<pilote>:<id natif>", jamais dérivé d'un nom
    driver: str
    native_name: str  # le nom que l'appareil donne de lui-même
    kind: str  # "light", "switch", "sensor", "tv"…
    manufacturer: str | None = None
    model: str | None = None
    native_room: str | None = None
    online: bool = True
    points: dict[str, Point] = field(default_factory=dict)
    # étiquettes posées par la personne (prioritaires)
    name: str | None = None
    room: str | None = None
    hidden: bool = False
    native: dict[str, Any] = field(default_factory=dict)  # références propres au pilote

    @property
    def display_name(self) -> str:
        return self.name or self.native_name

    @property
    def effective_room(self) -> str | None:
        return self.room or self.native_room

    def to_json(self, with_points: bool = True) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "name": self.display_name,
            "native_name": self.native_name,
            "room": self.effective_room,
            "kind": self.kind,
            "driver": self.driver,
            "manufacturer": self.manufacturer,
            "model": self.model,
            "online": self.online,
        }
        if with_points:
            d["points"] = {k: p.to_json() for k, p in self.points.items()}
        return d


def now_ms() -> int:
    return int(time.time() * 1000)
