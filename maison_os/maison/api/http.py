"""L'API REST, le flux d'événements (SSE) et la garde `Host`, sur un seul port.

- Lecture libre sur le réseau de la maison (comme Moli) ; écritures en JSON seulement
  (une page web étrangère ne peut pas en envoyer sans CORS, qui n'est jamais accordé).
- Le code de la maison (`pin`) prouve une personne : il faut pour valider une demande
  retenue et changer une étiquette.
- Garde `Host` sur toutes les routes, MCP compris : contre le DNS rebinding.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from ..config import PinGuard
from ..core.hub import CommandError, Hub
from ..core.model import Origin
from ..core.scenes import room_off, run_scene
from ..integrations import homeassistant as ha
from .people import People

MAX_BODY = 256 * 1024
PING_S = 25  # silence max du flux d'événements avant un « ping »


class HostGuard:
    """Refuse toute requête dont l'en-tête Host n'est ni une IP, ni localhost, ni un nom autorisé."""

    def __init__(self, app: ASGIApp, allowed: list[str]):
        self.app = app
        self.allowed = {h.lower() for h in allowed} | {"localhost"}

    def _ok(self, host: str) -> bool:
        host = host.strip().lower()
        if host.startswith("["):  # [ipv6]:port
            host = host[1 : host.find("]")]
        elif host.count(":") == 1:
            host = host.rsplit(":", 1)[0]
        if host in self.allowed:
            return True
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            return False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            host = dict(scope.get("headers") or []).get(b"host", b"").decode("latin-1")
            if not self._ok(host):
                resp = JSONResponse({"error": "hôte non autorisé"}, status_code=403)
                await resp(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _caller(request: Request) -> str:
    return request.client.host if request.client else "?"


async def _json(request: Request) -> dict[str, Any]:
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise ValueError("content-type application/json attendu")
    body = await request.body()
    if len(body) > MAX_BODY:
        raise ValueError("corps trop gros")
    data = json.loads(body or b"{}")
    if not isinstance(data, dict):
        raise ValueError("objet JSON attendu")
    return data


MAX_IMPORT_IDS = 200


def build_routes(hub: Hub, pins: PinGuard, house_name: str, ha_url: str = ha.DEFAULT_URL,
                 people: People | None = None) -> list[Route]:
    people = people or People(hub.data_dir or Path("."), {})

    async def health(request: Request) -> Response:
        return JSONResponse({
            "house": house_name,
            "devices": len(hub.devices),
            "online": sum(1 for d in hub.devices.values() if d.online),
            "drivers": hub.driver_status,
            "pin_configured": pins.configured,
        })

    async def devices(request: Request) -> Response:
        show_all = request.query_params.get("all") == "1"
        return JSONResponse([d.to_json() for d in hub.devices.values() if show_all or not d.hidden])

    async def device(request: Request) -> Response:
        dev = hub.devices.get(request.path_params["device_id"])
        if dev is None:
            return JSONResponse({"error": "appareil inconnu"}, status_code=404)
        return JSONResponse(dev.to_json())

    async def rooms(request: Request) -> Response:
        out: dict[str, list[str]] = {r: [] for r in hub.rooms()}
        for d in hub.devices.values():
            if d.effective_room and not d.hidden:
                out.setdefault(d.effective_room, []).append(d.id)
        return JSONResponse(out)

    async def command(request: Request) -> Response:
        try:
            data = await _json(request)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=415 if "content-type" in str(e) else 400)
        profile = people.who(request.headers.get("x-maison-cle"))
        if people.require_pairing and profile is None:
            return JSONResponse({"status": "error", "message": "Ce téléphone n'est pas associé à la maison"}, status_code=401)
        dev = hub.devices.get(str(data.get("device_id", "")))
        if profile and dev and not profile.may_use(dev.effective_room):
            hub.journal.write("command", device=dev.id, key=str(data.get("key", ""))[:40], actor=f"{profile.name}@{_caller(request)}",
                              origin="ui", result="refusé", reason=f"{dev.effective_room} fermé à ce profil")
            return JSONResponse({"status": "error", "message": f"{dev.effective_room} : réservé, pas pour ce profil"}, status_code=403)
        if request.headers.get("x-maison-origin") == "ui":
            origin = Origin.UI if pins.check(data.get("pin"), _caller(request)) else Origin.HOUSEHOLD
        else:
            origin = Origin.API
        actor = f"{profile.name}@{_caller(request)}" if profile else str(data.get("actor") or origin.value)
        result = await hub.command(
            str(data.get("device_id", "")), str(data.get("key", "")), data.get("value"),
            origin=origin, actor=actor[:64],
        )
        code = {"done": 200, "pending": 202}.get(result.status, 400)
        return JSONResponse(result.to_json(), status_code=code)

    async def approvals(request: Request) -> Response:
        return JSONResponse(hub.pending())

    async def resolve(request: Request) -> Response:
        try:
            data = await _json(request)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        if not pins.check(data.get("pin"), _caller(request)):
            return JSONResponse({"error": "code de la maison requis"}, status_code=403)
        result = await hub.resolve_approval(request.path_params["approval_id"], approve=bool(data.get("approve")), by=f"personne@{_caller(request)}")
        return JSONResponse(result.to_json(), status_code=200 if result.status == "done" else 400)

    async def label(request: Request) -> Response:
        try:
            data = await _json(request)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        if not pins.check(data.get("pin"), _caller(request)):
            return JSONResponse({"error": "code de la maison requis"}, status_code=403)
        try:
            dev = hub.set_label(request.path_params["device_id"], actor=f"personne@{_caller(request)}", origin=Origin.UI,
                                name=data.get("name"), room=data.get("room"), hidden=data.get("hidden"))
        except CommandError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse(dev.to_json())

    async def journal(request: Request) -> Response:
        try:
            limit = int(request.query_params.get("limit", "50"))
        except ValueError:
            limit = 50
        return JSONResponse(hub.journal.tail(limit))

    async def events(request: Request) -> Response:
        async def stream():
            snapshot = {"type": "snapshot", "devices": [d.to_json() for d in hub.devices.values()]}
            yield f"data: {json.dumps(snapshot, ensure_ascii=False)}\n\n"
            agen = hub.subscribe()
            # On attend l'événement suivant SANS l'annuler au bout du délai : annuler `__anext__`
            # (comme le fait `wait_for`) refermait l'abonnement, et le flux mourait au 1er silence.
            nxt = asyncio.ensure_future(agen.__anext__())
            try:
                while True:
                    done, _ = await asyncio.wait({nxt}, timeout=PING_S)
                    if nxt in done:
                        ev = nxt.result()
                        nxt = asyncio.ensure_future(agen.__anext__())
                        yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                    else:
                        yield ": ping\n\n"
                    if await request.is_disconnected():
                        break
            finally:
                nxt.cancel()
                with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                    await nxt
                await agen.aclose()

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"cache-control": "no-cache"})

    async def _ha_plan() -> ha.ImportPlan:
        token = ha.ha_token()
        if not token:
            raise ha.HAError("accès à Home Assistant non configuré (option homeassistant_api du module)")
        areas, devices = await ha.fetch_registries(ha_url, token)
        return ha.plan_import(hub, areas, devices)

    async def ha_import_preview(request: Request) -> Response:
        """Lecture seule : ce que Maison propose de reprendre de HA."""
        try:
            plan = await _ha_plan()
        except ha.HAError as e:
            return JSONResponse({"error": str(e)}, status_code=503)
        return JSONResponse(plan.to_json())

    async def ha_import_apply(request: Request) -> Response:
        """Applique les propositions choisies, une par appareil. Une personne, avec le code."""
        try:
            data = await _json(request)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        if not pins.check(data.get("pin"), _caller(request)):
            return JSONResponse({"error": "code de la maison requis"}, status_code=403)
        ids = data.get("device_ids")
        if not isinstance(ids, list) or not ids or len(ids) > MAX_IMPORT_IDS or not all(isinstance(i, str) for i in ids):
            return JSONResponse({"error": "device_ids : la liste des appareils à reprendre"}, status_code=400)
        try:
            plan = await _ha_plan()  # relu à l'instant : on n'applique jamais une proposition périmée
        except ha.HAError as e:
            return JSONResponse({"error": str(e)}, status_code=503)
        actor = f"personne@{_caller(request)}"
        by_id = {p.device_id: p for p in plan.proposals}
        applied, skipped = [], []
        for i in dict.fromkeys(ids):
            p = by_id.get(i)
            if p is None or not p.changes:
                skipped.append(i)
                continue
            try:
                hub.set_label(i, actor=actor, origin=Origin.UI, name=p.name, room=p.room)
            except CommandError as e:
                skipped.append(i)
                hub.journal.write("import_ha", device=i, actor=actor, result="refusé", error=str(e)[:200])
                continue
            applied.append(p.to_json())
        hub.journal.write("import_ha", actor=actor, applied=len(applied), skipped=len(skipped))
        return JSONResponse({"applied": applied, "skipped": skipped})

    async def scenes(request: Request) -> Response:
        return JSONResponse([s.to_json() for s in hub.scenes.values()])

    async def scene_run(request: Request) -> Response:
        sc = hub.scenes.get(request.path_params["scene_id"])
        if sc is None:
            return JSONResponse({"status": "error", "message": "ambiance inconnue"}, status_code=404)
        profile = people.who(request.headers.get("x-maison-cle"))
        if people.require_pairing and profile is None:
            return JSONResponse({"status": "error", "message": "Ce téléphone n'est pas associé à la maison"}, status_code=401)
        rooms = {sc.room} | {hub.devices[a["device"]].effective_room for a in sc.actions if a["device"] in hub.devices}
        if profile and any(not profile.may_use(r) for r in rooms if r):
            return JSONResponse({"status": "error", "message": f"{sc.name} : réservé, pas pour ce profil"}, status_code=403)
        actor = f"{profile.name}@{_caller(request)}" if profile else f"personne@{_caller(request)}"
        res = await run_scene(hub, sc, origin=Origin.HOUSEHOLD, actor=actor[:64])
        return JSONResponse(res, status_code=200 if res["status"] == "done" else 400)

    async def room_all_off(request: Request) -> Response:
        """Éteint d'un coup toutes les lumières (et prises) allumées d'une pièce."""
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        room = str(body.get("room") or "")[:64] if isinstance(body, dict) else ""
        profile = people.who(request.headers.get("x-maison-cle"))
        if people.require_pairing and profile is None:
            return JSONResponse({"status": "error", "message": "Ce téléphone n'est pas associé à la maison"}, status_code=401)
        sc = room_off(hub, room)
        if sc is None:
            return JSONResponse({"status": "error", "message": "pièce inconnue"}, status_code=404)
        if profile and not profile.may_use(sc.room):
            return JSONResponse({"status": "error", "message": f"{sc.room} : réservé, pas pour ce profil"}, status_code=403)
        actor = f"{profile.name}@{_caller(request)}" if profile else f"personne@{_caller(request)}"
        res = await run_scene(hub, sc, origin=Origin.HOUSEHOLD, actor=actor[:64])
        return JSONResponse(res, status_code=200 if res["status"] == "done" else 400)

    async def me(request: Request) -> Response:
        profile = people.who(request.headers.get("x-maison-cle"))
        return JSONResponse({"person": profile.name if profile else None,
                             "deny_rooms": sorted(profile.deny_rooms) if profile else [],
                             "require_pairing": people.require_pairing,
                             "profiles": list(people.profiles)})

    async def pair(request: Request) -> Response:
        """Associe CE téléphone à un profil : une personne de la maison, avec le code."""
        try:
            data = await _json(request)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        if not pins.check(data.get("pin"), _caller(request)):
            return JSONResponse({"error": "code de la maison requis"}, status_code=403)
        person = data.get("person")
        try:
            key = people.pair(str(person or ""), str(data.get("label") or ""))
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        hub.journal.write("telephone_associe", person=person, actor=f"personne@{_caller(request)}", origin="ui",
                          label=str(data.get("label") or "")[:60])
        return JSONResponse({"key": key, "person": person})

    async def index(request: Request) -> Response:
        return HTMLResponse(STATUS_PAGE.replace("{{house}}", house_name))

    return [
        Route("/etat", index),
        Route("/api/health", health),
        Route("/api/devices", devices),
        Route("/api/devices/{device_id:path}", device),
        Route("/api/rooms", rooms),
        Route("/api/command", command, methods=["POST"]),
        Route("/api/approvals", approvals),
        Route("/api/approvals/{approval_id}", resolve, methods=["POST"]),
        Route("/api/labels/{device_id:path}", label, methods=["PUT"]),
        Route("/api/journal", journal),
        Route("/api/moi", me),
        Route("/api/scenes", scenes),
        Route("/api/scenes/{scene_id}", scene_run, methods=["POST"]),
        Route("/api/pieces/eteindre", room_all_off, methods=["POST"]),
        Route("/api/associer", pair, methods=["POST"]),
        Route("/api/import/homeassistant", ha_import_preview),
        Route("/api/import/homeassistant", ha_import_apply, methods=["POST"]),
        Route("/api/events", events),
    ]


# Page d'état provisoire (le vrai tableau de bord React viendra en phase 2).
STATUS_PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{{house}}</title>
<style>body{font-family:system-ui,sans-serif;margin:2rem;background:#111;color:#eee}
h1{font-weight:600}button{font:inherit;padding:.4rem .9rem;border-radius:8px;border:0;background:#2c6ef2;color:#fff;cursor:pointer}input[type=password]{font:inherit;padding:.4rem;border-radius:8px;border:1px solid #444;background:#1d1d1f;color:#eee}.d{padding:.6rem 1rem;margin:.4rem 0;background:#1d1d1f;border-radius:10px;display:flex;justify-content:space-between;gap:1rem}
.off{opacity:.45}.r{color:#9a9a9a;font-size:.85rem}.v{font-variant-numeric:tabular-nums}</style></head>
<body><h1>{{house}}</h1><p class="r">Page d'état provisoire — en direct.</p><div id="list"></div>
<h2 style="font-weight:600;margin-top:2rem">Reprendre les pièces et les noms de Home Assistant</h2>
<p class="r">Lecture seule côté Home Assistant. Rien n'est changé sans le code de la maison.</p>
<button id="haPreview">Voir les propositions</button><div id="ha"></div>
<script>
const devs = {};
function esc(s){return String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function render(){const l=document.getElementById('list');l.innerHTML=Object.values(devs).sort((a,b)=>(a.room||'~').localeCompare(b.room||'~')).map(d=>{
const p=d.points||{};const on=p.on?p.on.value:null;const parts=[];
if(on!==null&&on!==undefined)parts.push(on?'allumé':'éteint');
if(p.brightness&&p.brightness.value!=null)parts.push(p.brightness.value+' %');
if(p.color_temp&&p.color_temp.value!=null&&(!p.color_mode||p.color_mode.value==='ct'))parts.push(p.color_temp.value+' K');
if(p.color&&p.color.value)parts.push('<span style="color:'+esc(p.color.value)+'">●</span>');
return '<div class="d '+(d.online?'':'off')+'"><div>'+esc(d.name)+'<div class="r">'+esc(d.room||'sans pièce')+'</div></div><div class="v">'+(d.online?parts.join(' · '):'injoignable')+'</div></div>'}).join('')}
document.getElementById('haPreview').onclick=async()=>{const box=document.getElementById('ha');box.textContent='Lecture de Home Assistant…';
const r=await fetch('/api/import/homeassistant');const j=await r.json();if(!r.ok){box.textContent=j.error||'erreur';return}
const rows=j.proposals.map(p=>'<div class="d"><label><input type="checkbox" value="'+esc(p.device_id)+'" '+(p.changes?'checked':'disabled')+'> '+esc(p.current.name)+' <span class="r">('+esc(p.current.room||'sans pièce')+')</span></label><div class="v">'+(p.changes?'→ '+esc(p.proposed.name||p.current.name)+' · '+esc(p.proposed.room||p.current.room||'sans pièce'):'déjà à jour')+'<div class="r">trouvé par '+esc(p.match)+'</div></div></div>').join('');
box.innerHTML=(rows||'<p class="r">Aucun appareil reconnu.</p>')+(j.unmatched_devices.length?'<p class="r">Sans équivalent dans Home Assistant : '+j.unmatched_devices.length+'</p>':'')
+'<p><input id="haPin" type="password" inputmode="numeric" placeholder="code de la maison"> <button id="haApply">Appliquer la sélection</button></p><p id="haMsg" class="r"></p>';
document.getElementById('haApply').onclick=async()=>{const ids=[...box.querySelectorAll('input[type=checkbox]:checked')].map(c=>c.value);const msg=document.getElementById('haMsg');
if(!ids.length){msg.textContent='Rien de coché.';return}
const a=await fetch('/api/import/homeassistant',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({pin:document.getElementById('haPin').value,device_ids:ids})});
const k=await a.json();msg.textContent=a.ok?('Appliqué : '+k.applied.length+' appareil(s).'):(k.error||'erreur')}};
const es=new EventSource('/api/events');es.onmessage=e=>{const ev=JSON.parse(e.data);
if(ev.type==='snapshot'){ev.devices.forEach(d=>devs[d.id]=d)}
else if(ev.type==='device'){devs[ev.device.id]=ev.device}
else if(ev.type==='state'&&devs[ev.device_id]){const p=devs[ev.device_id].points[ev.key];if(p)p.value=ev.value}
else if(ev.type==='online'&&devs[ev.device_id]){devs[ev.device_id].online=ev.online}
else if(ev.type==='removed'){delete devs[ev.device_id]}
render()};
</script></body></html>"""
