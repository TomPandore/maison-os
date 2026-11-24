"""Le tableau de bord de la maison : la page, son plan et ses images.

Les images de la maison et la position des lampes sont PRIVÉES : elles vivent dans
`<données>/tableau/` (jamais dans le dépôt). Ce module les lit et les vérifie :

    <données>/tableau/tableau.json
    {
      "floors": [{"id": "rdc", "name": "Rez-de-chaussée", "image": "rdc.webp"}, …],
      "lights": [
        {"device": "matter:…", "floor": "rdc", "x": 0.57, "y": 0.34,
         "glow": {"x": 0.58, "y": 0.41, "r": 0.2}},                        # halo rond
        {"device": "tuya:…", "floor": "etage", "x": 0.6, "y": 0.66,
         "strip": {"x1": 0.46, "y1": 0.73, "x2": 0.74, "y2": 0.6}}          # bande LED
      ],
      "sun": {"lat": 45.1, "lon": 5.7}                                       # pour le mode soir
    }

Coordonnées en fraction de l'image (0 à 1). Sans ce fichier, la page montre la liste
des pièces seulement.
"""

from __future__ import annotations

import json
import re
from importlib import resources
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import Route

IMAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}\.(webp|png|jpg|jpeg)$")
FLOOR_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
MAX_FLOORS = 8
MAX_LIGHTS = 200


class LayoutError(ValueError):
    pass


def _frac(v: Any, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1:
        raise LayoutError(f"{what} : nombre entre 0 et 1 attendu")
    return float(v)


def _text(v: Any, what: str, limit: int = 100) -> str:
    if not isinstance(v, str) or not v.strip() or len(v) > limit or any(ord(c) < 32 for c in v):
        raise LayoutError(f"{what} : texte invalide")
    return v.strip()


def validate_layout(raw: Any) -> dict[str, Any]:
    """Vérifie le plan et n'en garde que les champs connus (rien d'autre n'est servi)."""
    if not isinstance(raw, dict):
        raise LayoutError("objet JSON attendu")
    floors_raw = raw.get("floors")
    if not isinstance(floors_raw, list) or not 1 <= len(floors_raw) <= MAX_FLOORS:
        raise LayoutError(f"floors : 1 à {MAX_FLOORS} étages attendus")
    floors, ids = [], set()
    for i, f in enumerate(floors_raw):
        if not isinstance(f, dict):
            raise LayoutError(f"floors[{i}] : objet attendu")
        fid = _text(f.get("id"), f"floors[{i}].id", 31)
        if not FLOOR_ID.match(fid) or fid in ids:
            raise LayoutError(f"floors[{i}].id : identifiant invalide ou en double")
        image = _text(f.get("image"), f"floors[{i}].image", 50)
        if not IMAGE_NAME.match(image):
            raise LayoutError(f"floors[{i}].image : nom de fichier invalide")
        ids.add(fid)
        floors.append({"id": fid, "name": _text(f.get("name"), f"floors[{i}].name", 40), "image": image})

    lights_raw = raw.get("lights", [])
    if not isinstance(lights_raw, list) or len(lights_raw) > MAX_LIGHTS:
        raise LayoutError(f"lights : liste de {MAX_LIGHTS} lampes au plus")
    lights = []
    for i, li in enumerate(lights_raw):
        if not isinstance(li, dict):
            raise LayoutError(f"lights[{i}] : objet attendu")
        floor = li.get("floor")
        if floor not in ids:
            raise LayoutError(f"lights[{i}].floor : étage inconnu")
        out: dict[str, Any] = {
            "device": _text(li.get("device"), f"lights[{i}].device", 120),
            "floor": floor,
            "x": _frac(li.get("x"), f"lights[{i}].x"),
            "y": _frac(li.get("y"), f"lights[{i}].y"),
        }
        if "glow" in li:
            g = li["glow"]
            if not isinstance(g, dict):
                raise LayoutError(f"lights[{i}].glow : objet attendu")
            out["glow"] = {k: _frac(g.get(k), f"lights[{i}].glow.{k}") for k in ("x", "y", "r")}
        if "strip" in li:
            s = li["strip"]
            if not isinstance(s, dict):
                raise LayoutError(f"lights[{i}].strip : objet attendu")
            out["strip"] = {k: _frac(s.get(k), f"lights[{i}].strip.{k}") for k in ("x1", "y1", "x2", "y2")}
        lights.append(out)

    layout: dict[str, Any] = {"floors": floors, "lights": lights}
    sun = raw.get("sun")
    if sun is not None:
        if not isinstance(sun, dict):
            raise LayoutError("sun : objet attendu")
        lat, lon = sun.get("lat"), sun.get("lon")
        if isinstance(lat, bool) or isinstance(lon, bool) or not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)) \
                or not -90 <= lat <= 90 or not -180 <= lon <= 180:
            raise LayoutError("sun : lat (-90 à 90) et lon (-180 à 180) attendus")
        layout["sun"] = {"lat": round(float(lat), 1), "lon": round(float(lon), 1)}  # ~10 km : assez pour le soleil
    return layout


def load_layout(data_dir: Path) -> dict[str, Any] | None:
    path = data_dir / "tableau" / "tableau.json"
    if not path.exists():
        return None
    return validate_layout(json.loads(path.read_text(encoding="utf-8")))


def page_html() -> str:
    return resources.files("maison.api").joinpath("static/tableau.html").read_text(encoding="utf-8")


def build_tableau_routes(data_dir: Path, house_name: str) -> list[Route]:
    media_dir = (data_dir / "tableau").resolve()
    html = page_html().replace("{{house}}", house_name.replace("<", "").replace(">", "").replace("&", ""))

    async def page(request: Request) -> Response:
        return HTMLResponse(html, headers={"cache-control": "no-cache"})

    async def layout(request: Request) -> Response:
        try:
            lay = load_layout(data_dir)
        except (LayoutError, json.JSONDecodeError) as e:
            return JSONResponse({"error": f"tableau.json : {e}"}, status_code=500)
        if lay is None:
            return JSONResponse({"error": "pas de plan de la maison"}, status_code=404)
        return JSONResponse(lay)

    async def media(request: Request) -> Response:
        name = request.path_params["name"]
        if not IMAGE_NAME.match(name):
            return JSONResponse({"error": "nom invalide"}, status_code=404)
        path = (media_dir / name).resolve()
        if path.parent != media_dir or not path.is_file():
            return JSONResponse({"error": "image absente"}, status_code=404)
        return FileResponse(path, headers={"cache-control": "max-age=3600"})

    clean = house_name.replace("<", "").replace(">", "").replace("&", "")[:30]

    async def manifest(request: Request) -> Response:
        return JSONResponse({
            "name": clean, "short_name": clean, "start_url": "/", "scope": "/", "display": "standalone",
            "background_color": "#fbfaf7", "theme_color": "#fbfaf7", "lang": "fr",
            "icons": [{"src": "/icone-192.png", "sizes": "192x192", "type": "image/png"},
                      {"src": "/icone-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}],
        }, media_type="application/manifest+json")

    async def icon(request: Request) -> Response:
        size = request.path_params["size"]
        if size not in ("192", "512"):
            return JSONResponse({"error": "absent"}, status_code=404)
        data = resources.files("maison.api").joinpath(f"static/icone-{size}.png").read_bytes()
        return Response(data, media_type="image/png", headers={"cache-control": "max-age=86400"})

    return [
        Route("/", page),
        Route("/manifest.webmanifest", manifest),
        Route("/icone-{size}.png", icon),
        Route("/api/tableau", layout),
        Route("/tableau/media/{name}", media),
    ]
