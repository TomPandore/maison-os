"""Assemblage : config → hub → pilotes → API + MCP, sur un seul port."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from pathlib import Path

from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Mount

from .api.http import HostGuard, build_routes
from .api.mcp_tools import build_mcp
from .api.people import People
from .api.tableau import build_tableau_routes
from .config import Config, PinGuard
from .core.guard import GuardPolicy
from .core.hub import Hub
from .core.scenes import load_scenes
from .drivers.matter import MatterDriver
from .drivers.esphome import EspHomeDriver
from .drivers.tuya import TuyaDriver
from .integrations.homeassistant import DEFAULT_URL as DEFAULT_HA_URL

log = logging.getLogger("maison")


def build_hub(cfg: Config) -> Hub:
    hub = Hub(cfg.data_dir, GuardPolicy.from_config(cfg.raw.get("guard", {})), rooms=cfg.raw.get("rooms", []))
    hub.scenes = load_scenes(cfg.raw.get("scenes"))
    drivers = cfg.raw.get("drivers", {})
    m = drivers.get("matter") or {}
    if m.get("enabled", True):
        hub.add_driver(MatterDriver(
            url=m.get("url", "ws://core-matter-server:5580/ws"),
            read_only=bool(m.get("read_only", True)),
            transition_s=float(m.get("transition_s", 0)),
        ))
    t = drivers.get("tuya") or {}
    keys = cfg.data_dir / str(t.get("keys_file", "tuya.json"))
    if t.get("enabled", keys.exists()):  # actif dès qu'il y a des clés locales
        hub.add_driver(TuyaDriver(keys, read_only=bool(t.get("read_only", True)), poll_s=float(t.get("poll_s", 5))))
    e = drivers.get("esphome") or {}
    if e.get("devices") and e.get("enabled", True):
        hub.add_driver(EspHomeDriver(list(e["devices"]), read_only=bool(e.get("read_only", True))))
    return hub


def build_app(cfg: Config, hub: Hub | None = None) -> Starlette:
    hub = hub or build_hub(cfg)
    pins = PinGuard(cfg.data_dir, cfg.addon_pin)
    people = People(cfg.data_dir, cfg.raw.get("people"))
    house = str(cfg.raw.get("house_name", "Maison"))
    mcp = build_mcp(hub, house)
    # Notre garde Host couvre toutes les routes ; celle du SDK MCP ferait doublon.
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        host="0.0.0.0",
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        await hub.start()
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            await hub.stop()

    app = Starlette(
        routes=[*build_tableau_routes(cfg.data_dir, house),
                *build_routes(hub, pins, house, ha_url=str((cfg.raw.get("homeassistant") or {}).get("url") or DEFAULT_HA_URL),
                             people=people),
                Mount("/", app=mcp_app)],
        middleware=[Middleware(HostGuard, allowed=list(cfg.raw.get("allowed_hosts", [])))],
        lifespan=lifespan,
    )
    app.state.hub = hub
    app.state.pins = pins
    app.state.people = people
    if not pins.configured:
        log.warning("aucun code de la maison : `python -m maison set-pin` (ou l'option « pin » du module)")
    return app


def default_data_dir() -> Path:
    return Path("/data") if Path("/data/options.json").exists() else Path("data")
