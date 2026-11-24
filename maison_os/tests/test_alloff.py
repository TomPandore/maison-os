"""« Tout éteindre » par pièce : éteint ce qui est allumé, jamais pour un profil à qui la pièce est fermée."""

from __future__ import annotations

from .test_matter import UID1, UID2, UID3, wait_for
from .test_scenes import house, key  # noqa: F401 — même maison de test (UID1 et UID2 dans le Bureau)


async def test_all_off_turns_off_the_room_only(house):  # noqa: F811
    http, hub = house["http"], house["hub"]
    k = await key(http, "Parent")
    for uid in (UID1, UID2):
        r = await http.post("/api/command", json={"device_id": uid, "key": "on", "value": True},
                            headers={"x-maison-cle": k, "x-maison-origin": "ui"})
        assert r.status_code == 200, (uid, r.text)
    await wait_for(lambda: all(hub.devices[u].points["on"].value is True for u in (UID1, UID2)))
    r = await http.post("/api/pieces/eteindre", json={"room": "bureau"}, headers={"x-maison-cle": k})
    assert r.status_code == 200, r.text
    await wait_for(lambda: hub.devices[UID1].points["on"].value is False and hub.devices[UID2].points["on"].value is False)
    assert UID3 not in r.json()["skipped"]  # l'autre pièce n'est pas concernée
    assert any(e.get("action") == "scene" and e.get("scene") == "tout-eteindre" for e in hub.journal.tail(20))


async def test_all_off_refused_for_closed_room_and_unknown(house):  # noqa: F811
    http = house["http"]
    assert (await http.post("/api/pieces/eteindre", json={"room": "Bureau"})).status_code == 401
    r = await http.post("/api/pieces/eteindre", json={"room": "Bureau"}, headers={"x-maison-cle": await key(http, "Invitée")})
    assert r.status_code == 403
    r = await http.post("/api/pieces/eteindre", json={"room": "Grenier"}, headers={"x-maison-cle": await key(http, "Parent")})
    assert r.status_code == 404
