"""Le hub : l'état de la maison, le bus d'événements, le chemin de chaque ordre.

Chemin d'un ordre :
1. l'appareil existe, est joignable, le point existe et s'écrit, la valeur est valide ;
2. la garde : exécuté, ou retenu en demande que une personne autorise ;
3. envoi au pilote avec une échéance : un ordre que personne n'attend plus n'est jamais
   exécuté ;
4. journal ;
5. la confirmation revient du pilote comme un événement `state`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .guard import GuardPolicy, Verdict
from .journal import Journal
from .model import MAX_TEXT, Device, Origin, now_ms

if TYPE_CHECKING:
    from ..drivers.base import Driver

log = logging.getLogger("maison.hub")

COMMAND_DEADLINE_S = 5.0
APPROVAL_TTL_MS = 10 * 60 * 1000
MAX_PENDING = 50
MAX_NAME = 64


class CommandError(Exception):
    pass


@dataclass
class CommandResult:
    status: str  # "done" | "pending" | "error"
    message: str = ""
    approval_id: str | None = None

    def to_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {"status": self.status, "message": self.message}
        if self.approval_id:
            d["approval_id"] = self.approval_id
        return d


@dataclass
class Approval:
    id: str
    device_id: str
    key: str
    value: Any
    origin: Origin
    actor: str
    reason: str
    created_ms: int = field(default_factory=now_ms)

    @property
    def expired(self) -> bool:
        return now_ms() - self.created_ms > APPROVAL_TTL_MS

    def to_json(self, hub: "Hub") -> dict[str, Any]:
        dev = hub.devices.get(self.device_id)
        return {
            "id": self.id,
            "device_id": self.device_id,
            "device": dev.display_name if dev else self.device_id,
            "room": dev.effective_room if dev else None,
            "key": self.key,
            "value": self.value,
            "origin": self.origin.value,
            "actor": self.actor,
            "reason": self.reason,
            "created": self.created_ms,
        }


class Hub:
    def __init__(self, data_dir: Path | None, guard: GuardPolicy, rooms: list[str] | None = None):
        self.data_dir = data_dir
        self.scenes: dict = {}  # id → Scene (maison.core.scenes)
        self.guard = guard
        self.configured_rooms = list(rooms or [])
        self.devices: dict[str, Device] = {}
        self.drivers: dict[str, Driver] = {}
        self.approvals: dict[str, Approval] = {}
        self.journal = Journal(data_dir / "journal.jsonl" if data_dir else None)
        self._subscribers: set[asyncio.Queue] = set()
        self._labels: dict[str, dict[str, Any]] = self._load_labels()
        self._tasks: list[asyncio.Task] = []
        self.driver_status: dict[str, dict[str, Any]] = {}

    # ---------- étiquettes (nom, pièce, masqué) ----------

    def _labels_path(self) -> Path | None:
        return self.data_dir / "labels.json" if self.data_dir else None

    def _load_labels(self) -> dict[str, dict[str, Any]]:
        p = self._labels_path()
        if p and p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                log.warning("labels.json illisible, ignoré")
        return {}

    def _save_labels(self) -> None:
        p = self._labels_path()
        if not p:
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._labels, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)

    def _apply_labels(self, dev: Device) -> None:
        lab = self._labels.get(dev.id, {})
        dev.name = lab.get("name") or None
        dev.room = lab.get("room") or None
        dev.hidden = bool(lab.get("hidden", False))

    def set_label(
        self, device_id: str, *, actor: str, origin: Origin, name: str | None = None,
        room: str | None = None, hidden: bool | None = None,
    ) -> Device:
        dev = self.devices.get(device_id)
        if dev is None:
            raise CommandError("appareil inconnu")
        lab = dict(self._labels.get(device_id, {}))
        for field_name, val in (("name", name), ("room", room)):
            if val is None:
                continue
            val = val.strip()
            if len(val) > MAX_NAME or any(ord(c) < 32 for c in val):
                raise CommandError(f"{field_name} invalide")
            lab[field_name] = val
        if hidden is not None:
            lab["hidden"] = bool(hidden)
        # sortir un appareil d'une pièce protégée, ou l'y mettre, demande une personne
        old_room, new_room = dev.effective_room, lab.get("room") or dev.native_room
        if origin.is_agent and (self.guard.is_protected(old_room) or self.guard.is_protected(new_room)):
            if old_room != new_room:
                raise CommandError("changer la pièce d'un appareil d'une pièce protégée demande une personne")
        self._labels[device_id] = lab
        self._save_labels()
        self._apply_labels(dev)
        self.journal.write("label", device=device_id, actor=actor, origin=origin.value, label=lab)
        self.publish({"type": "device", "device": dev.to_json()})
        return dev

    # ---------- appareils ----------

    def upsert_device(self, dev: Device) -> None:
        # identité changée par le pilote : on reprend les étiquettes posées sous l'ancienne
        for old in dev.native.get("legacy_ids", []) if isinstance(dev.native, dict) else []:
            if old != dev.id and old in self._labels and dev.id not in self._labels:
                self._labels[dev.id] = self._labels.pop(old)
                self._save_labels()
                self.journal.write("label_migration", device=dev.id, ancien=old, actor="maison", origin=Origin.SYSTEM.value)
            if old != dev.id and old in self.devices:
                self.remove_device(old)
        self._apply_labels(dev)
        self.devices[dev.id] = dev
        self.publish({"type": "device", "device": dev.to_json()})

    def remove_device(self, device_id: str) -> None:
        if self.devices.pop(device_id, None):
            self.publish({"type": "removed", "device_id": device_id})

    def update_point(self, device_id: str, key: str, value: Any) -> None:
        dev = self.devices.get(device_id)
        if not dev or key not in dev.points:
            return
        pt = dev.points[key]
        if pt.value == value and pt.updated_ms is not None:
            return
        pt.value = value
        pt.updated_ms = now_ms()
        self.publish({"type": "state", "device_id": device_id, "key": key, "value": value, "ts": pt.updated_ms})

    def set_online(self, device_id: str, online: bool) -> None:
        dev = self.devices.get(device_id)
        if dev and dev.online != online:
            dev.online = online
            self.publish({"type": "online", "device_id": device_id, "online": online})

    def set_driver_offline(self, driver_id: str) -> None:
        for dev in self.devices.values():
            if dev.driver == driver_id:
                self.set_online(dev.id, False)

    def rooms(self) -> list[str]:
        seen: list[str] = []
        for r in self.configured_rooms + [d.effective_room for d in self.devices.values()]:
            if r and r not in seen:
                seen.append(r)
        return seen

    # ---------- bus ----------

    def publish(self, event: dict[str, Any]) -> None:
        for q in list(self._subscribers):
            if q.full():
                try:
                    q.get_nowait()  # le plus ancien cède sa place
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(event)

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subscribers.add(q)
        try:
            while True:
                yield await q.get()
        finally:
            self._subscribers.discard(q)

    # ---------- ordres ----------

    def _check(self, device_id: str, key: str, value: Any) -> tuple[Device, Any]:
        dev = self.devices.get(device_id)
        if dev is None:
            raise CommandError("appareil inconnu")
        pt = dev.points.get(key)
        if pt is None:
            raise CommandError(f"{dev.display_name} n'a pas de point « {key} »")
        if not pt.spec.access.writable:
            raise CommandError(f"« {key} » de {dev.display_name} est en lecture seule")
        if not dev.online:
            raise CommandError(f"{dev.display_name} est injoignable")
        try:
            clean = pt.spec.validate(value)
        except ValueError as e:
            raise CommandError(str(e)) from None
        return dev, clean

    async def command(self, device_id: str, key: str, value: Any, *, origin: Origin, actor: str) -> CommandResult:
        actor = (actor or origin.value)[:64]
        try:
            dev, clean = self._check(device_id, key, value)
        except CommandError as e:
            self.journal.write("command", device=device_id, key=key, origin=origin.value, actor=actor, result="refusé", error=str(e)[:200])
            return CommandResult("error", str(e))
        decision = self.guard.decide(origin, dev, datetime.now())
        if decision.verdict is Verdict.HOLD:
            appr = self._hold(dev, key, clean, origin, actor, decision.reason)
            self.journal.write("command", device=dev.id, key=key, value=_short(clean), origin=origin.value, actor=actor, result="retenu", reason=decision.reason, approval=appr.id)
            return CommandResult("pending", f"En attente de validation : {decision.reason}", appr.id)
        return await self._execute(dev, key, clean, origin, actor)

    async def _execute(self, dev: Device, key: str, value: Any, origin: Origin, actor: str, approved_by: str | None = None) -> CommandResult:
        driver = self.drivers.get(dev.driver)
        extra = {"approved_by": approved_by} if approved_by else {}
        if driver is None:
            self.journal.write("command", device=dev.id, key=key, value=_short(value), origin=origin.value, actor=actor, result="erreur", error="pilote absent", **extra)
            return CommandResult("error", "pilote absent")
        try:
            await asyncio.wait_for(driver.command(dev, key, value), timeout=COMMAND_DEADLINE_S)
        except TimeoutError:
            msg = f"{dev.display_name} n'a pas répondu à temps"
            self.journal.write("command", device=dev.id, key=key, value=_short(value), origin=origin.value, actor=actor, result="échéance", **extra)
            return CommandResult("error", msg)
        except Exception as e:  # noqa: BLE001 — un pilote ne fait jamais tomber le hub
            log.warning("ordre %s/%s échoué : %s", dev.id, key, e)
            self.journal.write("command", device=dev.id, key=key, value=_short(value), origin=origin.value, actor=actor, result="erreur", error=str(e)[:200], **extra)
            return CommandResult("error", f"{dev.display_name} : {e}")
        self.journal.write("command", device=dev.id, key=key, value=_short(value), origin=origin.value, actor=actor, result="ok", **extra)
        return CommandResult("done", "ok")

    # ---------- demandes d'autorisation ----------

    def _hold(self, dev: Device, key: str, value: Any, origin: Origin, actor: str, reason: str) -> Approval:
        self._purge_approvals()
        # une nouvelle demande du même appelant, même point, remplace l'ancienne
        for a in list(self.approvals.values()):
            if a.device_id == dev.id and a.key == key and a.actor == actor and a.origin == origin:
                del self.approvals[a.id]
        while len(self.approvals) >= MAX_PENDING:
            oldest = min(self.approvals.values(), key=lambda a: a.created_ms)
            del self.approvals[oldest.id]
        appr = Approval(secrets.token_urlsafe(8), dev.id, key, value, origin, actor, reason)
        self.approvals[appr.id] = appr
        self.publish({"type": "approval", "approval": appr.to_json(self)})
        return appr

    def _purge_approvals(self) -> None:
        for a in [a for a in self.approvals.values() if a.expired]:
            del self.approvals[a.id]

    def pending(self) -> list[dict[str, Any]]:
        self._purge_approvals()
        return [a.to_json(self) for a in sorted(self.approvals.values(), key=lambda a: a.created_ms)]

    async def resolve_approval(self, approval_id: str, *, approve: bool, by: str) -> CommandResult:
        """Appelé seulement par un chemin qui a vérifié une personne (code)."""
        self._purge_approvals()
        appr = self.approvals.pop(approval_id, None)
        if appr is None:
            return CommandResult("error", "demande inconnue ou expirée")
        self.publish({"type": "approval_resolved", "id": approval_id, "approved": approve})
        if not approve:
            self.journal.write("approval", id=approval_id, device=appr.device_id, key=appr.key, by=by, result="refusée")
            return CommandResult("done", "refusée")
        try:
            dev, clean = self._check(appr.device_id, appr.key, appr.value)
        except CommandError as e:
            self.journal.write("approval", id=approval_id, by=by, result="impossible", error=str(e))
            return CommandResult("error", str(e))
        self.journal.write("approval", id=approval_id, device=appr.device_id, key=appr.key, by=by, result="autorisée")
        return await self._execute(dev, appr.key, clean, appr.origin, appr.actor, approved_by=by)

    # ---------- pilotes supervisés ----------

    def add_driver(self, driver: "Driver") -> None:
        self.drivers[driver.id] = driver
        self.driver_status[driver.id] = {"state": "stopped"}

    async def start(self) -> None:
        for drv in self.drivers.values():
            self._tasks.append(asyncio.create_task(self._supervise(drv), name=f"driver:{drv.id}"))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks.clear()
        for drv in self.drivers.values():
            try:
                await drv.close()
            except Exception:  # noqa: BLE001
                pass

    async def _supervise(self, drv: "Driver") -> None:
        backoff = 1.0
        while True:
            self.driver_status[drv.id] = {"state": "running", "since": now_ms()}
            started = now_ms()
            try:
                await drv.run(self)
                err = "arrêt inattendu"
            except asyncio.CancelledError:
                self.driver_status[drv.id] = {"state": "stopped"}
                raise
            except Exception as e:  # noqa: BLE001 — un pilote qui plante redémarre, le hub reste debout
                err = f"{type(e).__name__}: {e}"[:300]
            log.warning("pilote %s : %s ; redémarrage dans %.0f s", drv.id, err, backoff)
            self.set_driver_offline(drv.id)
            self.driver_status[drv.id] = {"state": "error", "error": err, "retry_in_s": backoff}
            self.publish({"type": "driver", "driver": drv.id, **self.driver_status[drv.id]})
            if now_ms() - started > 60_000:
                backoff = 1.0
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


def _short(value: Any) -> Any:
    if isinstance(value, str) and len(value) > 200:
        return value[:200] + "…"
    return value


__all__ = ["Hub", "CommandResult", "CommandError", "Approval", "MAX_TEXT"]
