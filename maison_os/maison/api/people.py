"""Les personnes de la maison et leurs téléphones.

- Les profils viennent de `maison.yaml` :

      people:
        require_pairing: true          # un téléphone non associé peut regarder, pas commander
        profiles:
          - name: Tomji                # tout est permis
          - name: Copine
            deny_rooms: [Bureau]       # ces pièces lui sont fermées (le Pi le vérifie)

- Un téléphone s'associe UNE fois à un profil avec le code de la maison : il reçoit une clé
  (gardée dans le téléphone) ; le Pi ne garde que son empreinte (`telephones.json`, 0600).
- Chaque commande de la page porte la clé (`x-maison-cle`). Le Pi en déduit la personne,
  refuse les pièces fermées à ce profil, et écrit son nom dans le journal.
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

MAX_PHONES = 50


@dataclass
class Profile:
    name: str
    deny_rooms: frozenset[str] = field(default_factory=frozenset)  # en minuscules

    def may_use(self, room: str | None) -> bool:
        return (room or "").strip().lower() not in self.deny_rooms

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "deny_rooms": sorted(self.deny_rooms)}


class People:
    def __init__(self, data_dir: Path, raw: dict[str, Any] | None):
        raw = raw or {}
        self.require_pairing = bool(raw.get("require_pairing", False))
        self.profiles: dict[str, Profile] = {}
        for p in raw.get("profiles") or []:
            if not isinstance(p, dict) or not isinstance(p.get("name"), str) or not p["name"].strip():
                continue
            name = p["name"].strip()[:40]
            deny = frozenset(str(r).strip().lower() for r in (p.get("deny_rooms") or []) if str(r).strip())
            self.profiles[name] = Profile(name, deny)
        self.path = data_dir / "telephones.json"
        self._phones: list[dict[str, Any]] = []
        self._mtime = 0.0
        self._reload()

    def _reload(self) -> None:
        """Relit le fichier s'il a changé (un téléphone retiré en ligne de commande l'est aussitôt)."""
        try:
            m = self.path.stat().st_mtime
        except FileNotFoundError:
            self._phones, self._mtime = [], 0.0
            return
        if m == self._mtime:
            return
        try:
            self._phones = list(json.loads(self.path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, TypeError):
            self._phones = []
        self._mtime = m

    @staticmethod
    def _hash(key: str) -> str:
        return hashlib.sha256(key.encode()).hexdigest()

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._phones, ensure_ascii=False, indent=1), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)
        self._mtime = self.path.stat().st_mtime

    def pair(self, person: str, label: str) -> str:
        self._reload()
        if person not in self.profiles:
            raise ValueError("profil inconnu")
        if len(self._phones) >= MAX_PHONES:
            raise ValueError("trop de téléphones associés")
        key = secrets.token_urlsafe(32)
        label = "".join(c for c in (label or "Téléphone") if ord(c) >= 32)[:60] or "Téléphone"
        self._phones.append({"id": secrets.token_hex(4), "person": person, "label": label,
                             "hash": self._hash(key), "created": int(time.time() * 1000)})
        self._save()
        return key

    def who(self, key: str | None) -> Profile | None:
        if not key or not isinstance(key, str) or len(key) > 200:
            return None
        self._reload()
        h = self._hash(key)
        for ph in self._phones:
            if hmac.compare_digest(ph.get("hash", ""), h):
                return self.profiles.get(ph.get("person", ""))  # profil supprimé = plus d'accès
        return None

    def phones(self) -> list[dict[str, Any]]:
        return [{k: v for k, v in ph.items() if k != "hash"} for ph in self._phones]

    def revoke(self, phone_id: str) -> bool:
        self._reload()
        before = len(self._phones)
        self._phones = [p for p in self._phones if p.get("id") != phone_id]
        if len(self._phones) != before:
            self._save()
            return True
        return False
