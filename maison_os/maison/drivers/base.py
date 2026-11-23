"""Ce qu'un pilote doit savoir faire.

Un pilote est supervisé par le hub : s'il plante, ses appareils passent hors ligne et il
redémarre avec une attente croissante. Il ne parle jamais aux autres pilotes : il publie
dans le hub et reçoit les ordres du hub (donc passés par la garde).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..core.hub import Hub
    from ..core.model import Device


class Driver(ABC):
    id: str

    @abstractmethod
    async def run(self, hub: "Hub") -> None:
        """Boucle de vie : se connecter, publier les appareils, écouter. Ne rend la main
        qu'en cas de panne (exception) ; le hub relance."""

    @abstractmethod
    async def command(self, device: "Device", key: str, value: Any) -> None:
        """Exécute un ordre déjà validé par le hub. Lève une exception en cas d'échec."""

    def ha_identifiers(self, device: "Device") -> dict[str, set[str]]:
        """Les identifiants que Home Assistant donne à cet appareil dans son registre
        (`identifiers` d'un appareil, par domaine), pour retrouver ses pièce et nom HA.
        Clés : `strong` (identité exacte), `weak` (ex. numéro de série). Facultatif."""
        return {}

    async def close(self) -> None:  # noqa: B027 — facultatif
        """Libère les connexions à l'arrêt."""
