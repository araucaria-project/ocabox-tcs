"""Guider service entry point.

Thin @service shell modeled on dome_follower's pattern. Heavy lifting
in `manager.py` (GuiderManager).

"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from ocabox_tcs.base_service import (
    BaseBlockingPermanentService,
    BaseServiceConfig,
    config,
    service,
)
from ocabox_tcs.services.guiding_svc.manager import GuiderManager


@config("guiding_svc.guider")
@dataclass
class GuiderServiceConfig(BaseServiceConfig):
    """Top-level config for the guider service.

    Cameras + pipelines declared as nested lists; see
    config/guider.example.yaml for a concrete example.
    """

    telescope_id: str = "unknown"
    subject_prefix: str = "svc"
    """NATS subject root prefix. Subjects compose as
    ``<prefix>.<kind>.<service>.<instance>...``."""

    service_name: str = "guider"
    """Service short name segment in NATS subjects."""

    poll_interval_s: float = 1.0

    cameras: list[Any] = field(default_factory=list)
    """One service instance = one camera; this list should hold a single
    entry, identified by the component name in ``tic.config.observatory``."""

    defaults: dict[str, Any] = field(default_factory=dict)

    tic_conn: dict[str, Any] = field(default_factory=dict)
    """TicConn overrides. Keys: ``client_name``, ``software_id``,
    ``obs_config_subject``, ``enable_observatory``, ``service_mode``."""


@service("guiding_svc.guider")
class GuiderService(BaseBlockingPermanentService):
    """Telescope guider service.

    Internally a small fleet of consumer–producer pipelines (Stacker →
    Solver → Enforcer) per camera, orchestrated by GuiderManager.
    """

    def __init__(self) -> None:
        super().__init__()
        self.manager: GuiderManager | None = None

    async def on_start(self) -> None:
        self.svc_logger.info(
            "Guider service starting (telescope=%s)",
            getattr(self.svc_config, "telescope_id", "unknown"),
        )
        self.manager = GuiderManager(service=self)
        await self.manager.on_start()

    async def run_service(self) -> None:
        """Main service loop. Pipelines are self-driven; this loop just
        keeps the service alive and serves as a place for future
        periodic housekeeping (calibration freshness checks, etc.).
        """
        interval = getattr(self.svc_config, "poll_interval_s", 1.0)
        while self.is_running:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break

    async def on_stop(self) -> None:
        self.svc_logger.info("Guider service stopping")
        if self.manager is not None:
            await self.manager.on_stop()
            self.manager = None


if __name__ == "__main__":
    GuiderService.main()
