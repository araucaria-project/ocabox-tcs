"""Thumbnail HTTP server — pure-static aiohttp service.

Service type: ``thumbnail_svc.server`` (filename-derived). Variant
identifies the deployment role; for the guider-thumbnails instance use
``variant: guider``.

Configured via YAML, e.g.::

    services:
      - type: thumbnail_svc.server
        variant: guider
        bind: 0.0.0.0
        port: 8080
        public_host: thumbnails.observatory.lan   # advertised URL host
        cache_max_age_s: 1
        roots:
          - prefix: /guider/jk15-tcu
            directory: /mnt/observatory/guider/jk15-tcu
          - prefix: /guider/jk15-beso
            directory: /mnt/observatory/guider/jk15-beso

Discovery: published via standard TCS ``svc.status.>`` stream — UI
subscribes ``svc.status.thumbnail_svc.>`` and reads
``details.metrics.thumbnail_server`` for ``base_url`` + ``roots`` to
auto-fill the operator's thumbnail HTTP base.

Design constraints:
    - **Pure static** — no API endpoints, no upload, no auth (deploy
      behind nginx for auth/TLS in production). The /healthz endpoint
      is the only non-static route, and only for the framework's
      healthcheck loop.
    - **Read-only** — never writes the directory (the guider writes;
      we serve). aiohttp's ``add_static`` provides directory traversal
      protection by default.
    - **Single-process** — one ``BaseBlockingPermanentService`` runs
      the aiohttp app via ``run_service``; framework lifecycle handles
      graceful shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ocabox_tcs.base_service import (
    BaseBlockingPermanentService,
    BaseServiceConfig,
    config,
    service,
)
from ocabox_tcs.monitoring import Status


logger = logging.getLogger(__name__.rsplit(".", maxsplit=1)[-1])


@dataclass
class _RootCfg:
    """One served-directory mapping. ``prefix`` is the URL path
    component, ``directory`` the filesystem location it maps to."""
    prefix: str = "/"
    directory: str = ""


@config("thumbnail_svc.server")
@dataclass
class ThumbnailServerConfig(BaseServiceConfig):
    """Configurable surface for the thumbnail server service."""

    bind: str = "0.0.0.0"
    port: int = 8080
    # Hostname advertised in discovery metadata for clients to fetch
    # from. None → auto-detect via ``socket.gethostname()`` and assume
    # it resolves on the network the operator is on. Override for
    # split-horizon DNS or container deployments.
    public_host: str | None = None
    # Cache-Control max-age in seconds for served files. Thumbnails
    # change frequently; 1 s is a sweet spot — multiple clients pulling
    # the same ``latest.jpg`` within a second hit the cache once,
    # operators see ~1 s lag at most.
    cache_max_age_s: int = 1
    # CORS for browser access from arbitrary origins (UI may live on a
    # different host than the static server). ``"*"`` for permissive
    # (typical observatory LAN), or a specific origin in tightly-
    # controlled deployments.
    cors_origin: str = "*"
    # List of {prefix, directory} pairs. Empty = nothing served (logs a
    # warning but the service still boots so the operator can configure
    # via ``set_state`` if they want — useful for runtime mounts).
    roots: list[_RootCfg] = field(default_factory=list)


@service("thumbnail_svc.server")
class ThumbnailServer(BaseBlockingPermanentService):
    """Serve a fixed set of filesystem directories over HTTP for the
    guider UI (and any other consumer reading from the same NFS share).

    Lifecycle:
        - ``on_start`` → resolve roots, build the aiohttp app, register
          metric + healthcheck callbacks.
        - ``run_service`` → start ``AppRunner``, ``TCPSite``, then idle
          on the shutdown event. Framework cancels on ``stop_service``.
        - ``on_stop`` → close runner cleanly.

    The framework wraps ``run_service`` in its own task; we don't
    manage asyncio.Task creation ourselves.
    """

    svc_config: ThumbnailServerConfig

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._app: Any | None = None
        self._runner: Any | None = None
        self._site: Any | None = None
        self._roots: list[_RootCfg] = []
        self._stop_event = asyncio.Event()
        # Counters surfaced as metrics — auto-incremented by the
        # request middleware. Reset on service restart (counters here
        # are session-scope; for cumulative use Prometheus or similar).
        self._requests_total = 0
        self._bytes_served = 0
        self._requests_404 = 0
        self._last_error: str | None = None

    async def run_service(self) -> None:
        """Idles on a shutdown event after starting the HTTP listener.
        ``BaseBlockingPermanentService`` cancels this task on
        ``stop_service``; the ``CancelledError`` cleans up the site +
        runner via the finally block."""
        from aiohttp import web

        cfg = self.svc_config
        assert self._app is not None
        runner = web.AppRunner(self._app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, cfg.bind, cfg.port)
        try:
            await site.start()
        except OSError as e:
            self._last_error = f"bind {cfg.bind}:{cfg.port} failed: {e}"
            self.svc_logger.exception("ThumbnailServer bind failed: %s", e)
            await runner.cleanup()
            raise
        self._site = site
        self._runner = runner
        self.svc_logger.info(
            "ThumbnailServer: listening on http://%s:%d  (advertised host=%s)",
            cfg.bind, cfg.port, self._public_host(),
        )
        try:
            await self._stop_event.wait()
        except asyncio.CancelledError:
            raise
        finally:
            try:
                await runner.cleanup()
            except Exception as e:  # noqa: BLE001
                self.svc_logger.warning("ThumbnailServer cleanup error: %s", e)
            self._site = None
            self._runner = None

    async def on_stop(self) -> None:
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Request handling
    # ------------------------------------------------------------------

    @staticmethod
    def _normalise_roots(raw: list[Any]) -> list[_RootCfg]:
        """Accept dicts (from YAML) or _RootCfg instances; coerce."""
        out: list[_RootCfg] = []
        for r in raw or []:
            if isinstance(r, _RootCfg):
                out.append(r)
                continue
            if isinstance(r, dict):
                pref = str(r.get("prefix", "/")).rstrip("/")
                if not pref.startswith("/"):
                    pref = "/" + pref
                directory = str(r.get("directory", "")).rstrip("/")
                if directory:
                    out.append(_RootCfg(prefix=pref or "/", directory=directory))
        return out

    def _public_host(self) -> str:
        """Hostname advertised in discovery metadata. ``public_host`` from
        config wins; fall back to the OS hostname (works in observatory
        LANs with mDNS / managed DNS). Never returns ``0.0.0.0`` — that's
        a bind address, not a reachable URL host."""
        if self.svc_config.public_host:
            return self.svc_config.public_host
        try:
            return socket.gethostname()
        except Exception:  # noqa: BLE001
            return "localhost"

    def _make_middleware(self) -> Any:
        """Construct the per-request middleware closure with ``self``
        captured directly. Avoids cross-version aiohttp footguns
        around ``request.app`` introspection — we just close over the
        service instance and increment counters straight on it."""
        svc = self
        from aiohttp.web import Response, StreamResponse, middleware

        @middleware
        async def stats_mw(request: Any, handler: Any) -> Any:
            try:
                response: StreamResponse = await handler(request)
            except Exception:
                svc._requests_total += 1
                svc._requests_404 += 1
                raise
            svc._requests_total += 1
            if response.status == 404:
                svc._requests_404 += 1
            length = response.content_length
            if length:
                svc._bytes_served += int(length)
            response.headers.setdefault(
                "Access-Control-Allow-Origin", svc.svc_config.cors_origin)
            response.headers.setdefault(
                "Access-Control-Allow-Methods", "GET, HEAD")
            response.headers.setdefault("Access-Control-Max-Age", "86400")
            if isinstance(response, StreamResponse) and not isinstance(response, Response):
                response.headers.setdefault(
                    "Cache-Control",
                    f"public, max-age={svc.svc_config.cache_max_age_s}",
                )
            return response

        return stats_mw

    async def on_start(self) -> None:
        """Resolve config, build the aiohttp app, register monitor
        callbacks. Runs before ``run_service`` opens the listening
        socket so the route table is complete on first request.
        """
        cfg = self.svc_config
        self._roots = self._normalise_roots(cfg.roots)
        if not self._roots:
            self.svc_logger.warning(
                "ThumbnailServer: no roots configured — service will "
                "respond 404 to every request. Add ``roots:`` to YAML."
            )
        for r in self._roots:
            if not Path(r.directory).is_dir():
                self.svc_logger.warning(
                    "ThumbnailServer: directory missing for prefix=%s: %s",
                    r.prefix, r.directory,
                )
        if hasattr(self.monitor, "add_metric_cb"):
            self.monitor.add_metric_cb(self._discovery_metrics)
            self.monitor.add_metric_cb(self._runtime_metrics)
        if hasattr(self.monitor, "add_healthcheck_cb"):
            self.monitor.add_healthcheck_cb(self._healthcheck)

        from aiohttp import web
        app = web.Application(middlewares=[self._make_middleware()])
        # Only register routes for roots whose directory currently
        # exists. ``add_static`` raises if the path is missing, which
        # would crash the boot — and we'd lose the chance to serve the
        # roots that ARE present. Missing roots are flagged via the
        # healthcheck callback (DEGRADED) so the operator can still
        # see them; on the next mount of NFS the operator restarts.
        for r in self._roots:
            if not Path(r.directory).is_dir():
                self.svc_logger.warning(
                    "ThumbnailServer: skipping route %s — directory missing: %s",
                    r.prefix, r.directory,
                )
                continue
            app.router.add_static(
                r.prefix, r.directory,
                follow_symlinks=True, show_index=False,
            )
        app.router.add_get("/healthz", self._healthz_handler)
        app.router.add_get("/", self._index_handler)
        self._app = app
        self.svc_logger.info(
            "ThumbnailServer: %d root(s) configured, will bind %s:%d",
            len(self._roots), cfg.bind, cfg.port,
        )

    async def _healthz_handler(self, _request: Any) -> Any:
        from aiohttp import web
        missing = [r for r in self._roots if not Path(r.directory).is_dir()]
        body = {
            "status": "ok" if not missing else "degraded",
            "roots": [{"prefix": r.prefix, "directory": r.directory,
                       "exists": Path(r.directory).is_dir()}
                      for r in self._roots],
            "requests_total": self._requests_total,
            "bytes_served": self._bytes_served,
        }
        return web.json_response(body)

    async def _index_handler(self, _request: Any) -> Any:
        """Tiny HTML index — operator can hit the root URL in a browser
        and see what's served."""
        from aiohttp import web
        rows = "".join(
            f'<li><a href="{r.prefix}/">{r.prefix}</a> → '
            f'<code>{r.directory}</code> '
            f'({"✓" if Path(r.directory).is_dir() else "missing"})</li>'
            for r in self._roots
        )
        body = (
            "<!doctype html><meta charset='utf-8'>"
            "<title>thumbnail_svc</title>"
            "<style>body{font-family:ui-monospace,monospace;background:#09090b;"
            "color:#e4e4e7;padding:2rem;}a{color:#34d399}"
            "code{background:#27272a;padding:.1rem .3rem}</style>"
            f"<h1>thumbnail_svc · {self.svc_config.variant}</h1>"
            f"<p>requests={self._requests_total} · bytes={self._bytes_served}</p>"
            f"<ul>{rows or '<li><i>no roots configured</i></li>'}</ul>"
        )
        return web.Response(text=body, content_type="text/html")

    # ------------------------------------------------------------------
    # Discovery + runtime metrics + healthcheck
    # ------------------------------------------------------------------

    def _discovery_metrics(self) -> dict[str, Any]:
        """Static discovery payload — UI's auto-fill source. Stable
        across the service lifetime; runtime stats live in the
        ``_runtime_metrics`` callback to keep the discovery one
        cacheable."""
        host = self._public_host()
        port = self.svc_config.port
        return {
            "thumbnail_server": {
                "base_url": f"http://{host}:{port}",
                "bind": f"{self.svc_config.bind}:{self.svc_config.port}",
                "cache_max_age_s": self.svc_config.cache_max_age_s,
                "roots": [
                    {"prefix": r.prefix, "directory": r.directory}
                    for r in self._roots
                ],
            }
        }

    def _runtime_metrics(self) -> dict[str, Any]:
        """Live counters — request count, bytes, missing directories."""
        return {
            "thumbnail_runtime": {
                "requests_total": self._requests_total,
                "requests_404": self._requests_404,
                "bytes_served": self._bytes_served,
                "roots_missing": [
                    r.directory for r in self._roots
                    if not Path(r.directory).is_dir()
                ],
                "last_error": self._last_error,
            }
        }

    def _healthcheck(self) -> Status:
        """DEGRADED if any configured directory is missing (NFS not
        mounted, typo in YAML); ERROR if site setup failed; otherwise
        OK. The framework only escalates to unhealthy values from
        here, so OK doesn't override BUSY/IDLE set elsewhere."""
        if self._last_error is not None:
            return Status.ERROR
        if self._site is None and self._stop_event.is_set():
            # Service is shutting down — leave healthy; the framework
            # transitions to SHUTDOWN on its own.
            return Status.OK
        for r in self._roots:
            if not Path(r.directory).is_dir():
                return Status.DEGRADED
        return Status.OK
