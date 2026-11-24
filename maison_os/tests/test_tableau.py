"""Le tableau de bord : le plan privé de la maison est vérifié, ses images restent dans leur dossier."""

from __future__ import annotations

import json

import httpx
import pytest
from starlette.applications import Starlette

from maison.api.tableau import LayoutError, build_tableau_routes, validate_layout

PLAN = {
    "floors": [{"id": "rdc", "name": "Rez-de-chaussée", "image": "rdc.webp"},
               {"id": "etage", "name": "Étage", "image": "etage.webp"}],
    "lights": [
        {"device": "matter:AAAA", "floor": "rdc", "x": 0.57, "y": 0.34, "glow": {"x": 0.58, "y": 0.41, "r": 0.2}},
        {"device": "tuya:BBBB", "floor": "etage", "x": 0.6, "y": 0.66, "strip": {"x1": 0.4, "y1": 0.7, "x2": 0.7, "y2": 0.6}},
    ],
    "sun": {"lat": 45.12345, "lon": 5.67891},
    "inconnu": "ignoré",
}


def test_validate_keeps_only_known_fields_and_rounds_position():
    lay = validate_layout(PLAN)
    assert [f["id"] for f in lay["floors"]] == ["rdc", "etage"]
    assert lay["lights"][0]["glow"] == {"x": 0.58, "y": 0.41, "r": 0.2}
    assert "strip" in lay["lights"][1]
    assert lay["sun"] == {"lat": 45.1, "lon": 5.7}  # ~10 km, jamais l'adresse exacte
    assert "inconnu" not in lay


@pytest.mark.parametrize("bad", [
    {"floors": []},
    {"floors": [{"id": "rdc", "name": "RDC", "image": "../secret.webp"}]},
    {"floors": [{"id": "rdc", "name": "RDC", "image": "rdc.svg"}]},
    {"floors": [{"id": "rdc", "name": "RDC", "image": "a.webp"}, {"id": "rdc", "name": "Bis", "image": "b.webp"}]},
    {"floors": [{"id": "rdc", "name": "RDC", "image": "a.webp"}], "lights": [{"device": "x:1", "floor": "cave", "x": 0.1, "y": 0.1}]},
    {"floors": [{"id": "rdc", "name": "RDC", "image": "a.webp"}], "lights": [{"device": "x:1", "floor": "rdc", "x": 1.5, "y": 0.1}]},
    {"floors": [{"id": "rdc", "name": "RDC", "image": "a.webp"}], "lights": [{"device": "x:1", "floor": "rdc", "x": True, "y": 0.1}]},
    {"floors": [{"id": "rdc", "name": "<b>\x00</b>", "image": "a.webp"}]},
    {"floors": [{"id": "rdc", "name": "RDC", "image": "a.webp"}], "sun": {"lat": 200, "lon": 0}},
])
def test_validate_rejects(bad):
    with pytest.raises(LayoutError):
        validate_layout(bad)


@pytest.fixture
async def client(tmp_path):
    app = Starlette(routes=build_tableau_routes(tmp_path, "Maison <test>"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://maison") as c:
        yield c, tmp_path


async def test_page_and_missing_plan(client):
    c, _ = client
    r = await c.get("/")
    assert r.status_code == 200 and "<title>Maison test</title>" in r.text  # nom nettoyé, pas de balise injectée
    assert (await c.get("/api/tableau")).status_code == 404  # sans plan : la page montre la liste seulement


async def test_plan_and_images_stay_in_their_folder(client):
    c, data = client
    (data / "tableau").mkdir()
    (data / "tableau" / "tableau.json").write_text(json.dumps(PLAN), encoding="utf-8")
    (data / "tableau" / "rdc.webp").write_bytes(b"RIFF....WEBP")
    (data / "secret.webp").write_bytes(b"secret")
    (data / "pin.json").write_text("{}")

    lay = (await c.get("/api/tableau")).json()
    assert lay["floors"][0]["image"] == "rdc.webp" and "inconnu" not in lay

    r = await c.get("/tableau/media/rdc.webp")
    assert r.status_code == 200 and r.content == b"RIFF....WEBP"
    for path in ("/tableau/media/..%2Fsecret.webp", "/tableau/media/pin.json", "/tableau/media/absent.webp",
                 "/tableau/media/%2E%2E%2Fpin.json"):
        assert (await c.get(path)).status_code == 404, path


async def test_broken_plan_is_reported(client):
    c, data = client
    (data / "tableau").mkdir()
    (data / "tableau" / "tableau.json").write_text('{"floors": []}', encoding="utf-8")
    r = await c.get("/api/tableau")
    assert r.status_code == 500 and "tableau.json" in r.json()["error"]
