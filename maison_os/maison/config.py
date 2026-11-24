"""Configuration : `data/maison.yaml`, complétée par les options du module Home Assistant
(`/data/options.json`) quand Maison tourne comme module (add-on).

Aucun secret en clair dans le YAML : le code de la maison est gardé haché
(`data/pin.json`, scrypt), ou donné par les options du module.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "house_name": "Maison",
    "listen": "0.0.0.0:8790",
    "allowed_hosts": ["homeassistant.local", "homeassistant"],
    "rooms": [],
    "guard": {"protected_rooms": [], "quiet_hours": {}, "hold_unassigned": True},
    "homeassistant": {"url": "ws://supervisor/core/websocket"},
    "drivers": {"matter": {"enabled": True, "url": "ws://core-matter-server:5580/ws", "read_only": True, "transition_s": 0}},
}


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _from_addon_options(opts: dict[str, Any]) -> dict[str, Any]:
    """Traduit les options du module HA (plates) vers la config."""
    cfg: dict[str, Any] = {}
    if opts.get("allowed_hosts"):
        cfg["allowed_hosts"] = list(opts["allowed_hosts"])
    guard: dict[str, Any] = {}
    if "protected_rooms" in opts:
        guard["protected_rooms"] = list(opts["protected_rooms"] or [])
    if opts.get("quiet_start") and opts.get("quiet_end"):
        guard["quiet_hours"] = {"start": opts["quiet_start"], "end": opts["quiet_end"]}
    if guard:
        cfg["guard"] = guard
    matter: dict[str, Any] = {}
    if opts.get("matter_url"):
        matter["url"] = opts["matter_url"]
    if "matter_read_only" in opts:
        matter["read_only"] = bool(opts["matter_read_only"])
    if matter:
        cfg["drivers"] = {"matter": matter}
    return cfg


@dataclass
class Config:
    data_dir: Path
    raw: dict[str, Any] = field(default_factory=dict)
    addon_pin: str | None = None

    @property
    def listen(self) -> tuple[str, int]:
        host, _, port = str(self.raw["listen"]).rpartition(":")
        return host or "0.0.0.0", int(port)

    @classmethod
    def load(cls, data_dir: Path) -> "Config":
        data_dir.mkdir(parents=True, exist_ok=True)
        raw = dict(DEFAULTS)
        yml = data_dir / "maison.yaml"
        if yml.exists():
            raw = _merge(raw, yaml.safe_load(yml.read_text(encoding="utf-8")) or {})
        addon_pin = None
        opts_path = Path(os.environ.get("MAISON_ADDON_OPTIONS", "/data/options.json"))
        if opts_path.exists():
            opts = json.loads(opts_path.read_text(encoding="utf-8"))
            raw = _merge(raw, _from_addon_options(opts))
            addon_pin = str(opts.get("pin") or "") or None
        return cls(data_dir=data_dir, raw=raw, addon_pin=addon_pin)


class PinGuard:
    """Vérifie le code de la maison, avec un frein contre les essais en rafale."""

    MAX_FAILS = 5
    WINDOW_S = 60

    def __init__(self, data_dir: Path, plain_pin: str | None = None):
        self.path = data_dir / "pin.json"
        self._fails: dict[str, list[float]] = {}
        self._salt: bytes | None = None
        self._hash: bytes | None = None
        if plain_pin:
            self._salt = secrets.token_bytes(16)
            self._hash = self._kdf(plain_pin, self._salt)
        elif self.path.exists():
            d = json.loads(self.path.read_text(encoding="utf-8"))
            self._salt, self._hash = bytes.fromhex(d["salt"]), bytes.fromhex(d["hash"])

    @staticmethod
    def _kdf(pin: str, salt: bytes) -> bytes:
        return hashlib.scrypt(pin.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)

    @property
    def configured(self) -> bool:
        return self._hash is not None

    def set(self, pin: str) -> None:
        if len(pin) < 6 or not pin.isdigit():
            raise ValueError("le code doit faire au moins 6 chiffres")
        self._salt = secrets.token_bytes(16)
        self._hash = self._kdf(pin, self._salt)
        self.path.write_text(json.dumps({"salt": self._salt.hex(), "hash": self._hash.hex()}), encoding="utf-8")
        os.chmod(self.path, 0o600)

    def check(self, pin: Any, caller: str) -> bool:
        now = time.monotonic()
        fails = [t for t in self._fails.get(caller, []) if now - t < self.WINDOW_S]
        self._fails[caller] = fails
        if len(fails) >= self.MAX_FAILS or not self._hash or not self._salt or not isinstance(pin, str) or not pin:
            if isinstance(pin, str) and pin:
                fails.append(now)
            return False
        ok = hmac.compare_digest(self._kdf(pin[:64], self._salt), self._hash)
        if not ok:
            fails.append(now)
        return ok
