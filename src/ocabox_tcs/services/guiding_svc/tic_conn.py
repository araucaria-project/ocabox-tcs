"""TIC connection wrapper.

Holds the ``tic.config.observatory`` LiveDocument and ocaboxapi handles
(Observatory / Telescope / Camera / Mount). When ``service_mode`` is
True, every camera/mount handle has ``request_special_permission = True``
set so the guider can drive its components without claiming
whole-telescope ownership.
"""

from __future__ import annotations

import logging
from typing import Any


logger = logging.getLogger(__name__.rsplit(".", maxsplit=1)[-1])


class TicConn:
    """Wraps observatory config + TIC RPC handles for one telescope.

    Args:
        manager: Back-reference to GuiderManager.
        telescope_id: Identifier (e.g. ``jk15``).
        client_name: Observatory client name (looked up in
            ``site.client.<client_name>``).
        software_id: Free-form identifier surfaced in audit logs.
        obs_config_subject: NATS subject for observatory config.
        enable_observatory: Bootstrap the ``Observatory`` (ocaboxapi).
            False = LiveDocument-only (sim/dev).
        service_mode: When True, set ``request_special_permission = True``
            on each camera/mount handle returned.
    """

    def __init__(
        self,
        manager: Any,
        telescope_id: str,
        *,
        client_name: str = "CliClient",
        software_id: str | None = None,
        obs_config_subject: str = "tic.config.observatory",
        enable_observatory: bool = True,
        service_mode: bool = True,
    ) -> None:
        self.manager = manager
        self.svc_logger = manager.svc_logger
        self.telescope_id = telescope_id
        self.client_name = client_name
        self.software_id = software_id or f"guider/{telescope_id}"
        self.obs_config_subject = obs_config_subject
        self.enable_observatory = enable_observatory
        self.service_mode = service_mode

        self.obs_cfg: Any = None  # LiveDocument
        self.observatory: Any = None
        self.telescope: Any = None
        self._camera_handles: dict[str, Any] = {}
        self._mount_handle: Any = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def open(self) -> None:
        await self._open_livedocument()
        if self.enable_observatory:
            await self._open_observatory()

    async def _open_livedocument(self) -> None:
        try:
            from serverish.messenger import get_live_document

            self.obs_cfg = await get_live_document(self.obs_config_subject, wait=5.0)
            self.svc_logger.info(
                "TicConn opened LiveDocument for %s", self.obs_config_subject
            )
        except Exception as e:  # noqa: BLE001
            self.svc_logger.warning(
                "TicConn: could not open LiveDocument %s: %s "
                "(continuing without observatory config)",
                self.obs_config_subject,
                e,
            )
            self.obs_cfg = None

    async def _open_observatory(self) -> None:
        try:
            from ocaboxapi import Observatory
        except ImportError:
            self.svc_logger.warning(
                "TicConn: ocaboxapi not installed — service runs in "
                "LiveDocument-only mode (no camera/mount control). "
                "Install with `poetry install --extras oca`."
            )
            return

        try:
            self.observatory = Observatory(
                client_name=self.client_name,
                software_id=self.software_id,
                config_stream=self.obs_config_subject,
            )
            await self.observatory.load_client_cfg()
            self.observatory.connect()
            self.telescope = self.observatory.get_telescope(self.telescope_id)
            self.svc_logger.info(
                "TicConn ocabox connected: telescope=%s client=%s software=%s",
                self.telescope_id,
                self.client_name,
                self.software_id,
            )
        except Exception as e:  # noqa: BLE001
            self.svc_logger.error(
                "TicConn: ocaboxapi bootstrap failed for telescope %s: %s "
                "(real-camera path will be unavailable)",
                self.telescope_id,
                e,
            )
            self.observatory = None
            self.telescope = None

    async def close(self) -> None:
        # ocaboxapi has no explicit close; the underlying messenger is
        # owned by the framework. Just drop references.
        self._camera_handles.clear()
        self._mount_handle = None
        self.telescope = None
        self.observatory = None
        self.obs_cfg = None

    # ------------------------------------------------------------------
    # Handle accessors (set request_special_permission per service_mode)
    # ------------------------------------------------------------------

    def get_camera_handle(self, camera_id: str) -> Any | None:
        """Return an ``ocaboxapi.Camera`` for ``camera_id`` (the
        component name in ``tic.config.observatory``), or None when
        the observatory bootstrap is disabled / failed.

        Cached by camera_id. ``request_special_permission`` is set on
        first access if ``service_mode`` is True.
        """
        if self.telescope is None:
            return None
        if camera_id in self._camera_handles:
            return self._camera_handles[camera_id]
        try:
            cam = self.telescope.get_camera(id=camera_id)
        except Exception as e:  # noqa: BLE001
            self.svc_logger.error(
                "TicConn.get_camera_handle(%s): %s", camera_id, e
            )
            return None
        if self.service_mode:
            cam.request_special_permission = True
            self.svc_logger.info(
                "TicConn: camera %s handle obtained, "
                "request_special_permission=True (service-mode)",
                camera_id,
            )
        else:
            self.svc_logger.info(
                "TicConn: camera %s handle obtained (no service-mode flag)",
                camera_id,
            )
        self._camera_handles[camera_id] = cam
        return cam

    def get_mount_handle(self) -> Any | None:
        """Return the telescope mount handle, or None when unavailable.
        Cached. ``request_special_permission`` set per ``service_mode``.
        """
        if self.telescope is None:
            return None
        if self._mount_handle is not None:
            return self._mount_handle
        try:
            mount = self.telescope.get_mount()
        except Exception as e:  # noqa: BLE001
            self.svc_logger.error("TicConn.get_mount_handle: %s", e)
            return None
        if self.service_mode:
            mount.request_special_permission = True
            self.svc_logger.info(
                "TicConn: mount handle obtained, "
                "request_special_permission=True (service-mode)"
            )
        else:
            self.svc_logger.info("TicConn: mount handle obtained")
        self._mount_handle = mount
        return mount

    # ------------------------------------------------------------------
    # Observatory-config lookups (driven by LiveDocument)
    # ------------------------------------------------------------------

    def resolve_alpaca_endpoint(
        self, camera_id: str
    ) -> tuple[str, int] | None:
        """Look up Alpaca ``(base_url, device_number)`` for ``camera_id``.

        Resolution order per architecture (D3 / Phase 1C):
          1. ``components.<camera_id>.address`` (per-component override)
          2. observatory ``address`` (telescope default)

        Both are stripped of any trailing ``/api/v1`` so callers can
        prepend it consistently.

        Returns None when the LiveDocument is unavailable or the camera
        component isn't declared.
        """
        comp = self._component(camera_id)
        if comp is None:
            return None
        device_number = comp.get("device_number", 0)
        comp_addr = comp.get("address")
        obs_addr = self._observatory_dict().get("address") if self._observatory_dict() else None
        addr = comp_addr or obs_addr
        if not addr:
            self.svc_logger.warning(
                "resolve_alpaca_endpoint(%s): no address in component or observatory",
                camera_id,
            )
            return None
        # Normalise: strip trailing /api/v1[/]
        for tail in ("/api/v1/", "/api/v1"):
            if addr.endswith(tail):
                addr = addr[: -len(tail)]
                break
        return addr, int(device_number)

    def _observatory_dict(self) -> dict[str, Any] | None:
        """Best-effort access to ``config.telescopes.<tel>.observatory``."""
        if self.obs_cfg is None:
            return None
        # LiveDocument is dict-like with both [] and .attr access.
        try:
            tels = self.obs_cfg["config"]["telescopes"]
            return _as_dict(tels[self.telescope_id]["observatory"])
        except Exception as e:  # noqa: BLE001
            self.svc_logger.debug("_observatory_dict: %s", e)
            return None

    def _component(self, camera_id: str) -> dict[str, Any] | None:
        obs = self._observatory_dict()
        if obs is None:
            return None
        comps = obs.get("components")
        if not comps:
            return None
        comp = comps.get(camera_id) if isinstance(comps, dict) else None
        return _as_dict(comp) if comp is not None else None

    # ------------------------------------------------------------------
    # Camera info (kept for compatibility with skeleton callers)
    # ------------------------------------------------------------------

    def camera_info(self, camera_id: str) -> dict[str, Any]:
        """Look up CameraInfo for a camera by id from the LiveDocument.

        Best-effort: returns a stub dict with application defaults when
        the LiveDocument or component is unavailable, so the rest of the
        pipeline can boot.
        """
        comp = self._component(camera_id)
        if comp is None:
            self.svc_logger.debug(
                "camera_info(%s): no component entry, returning stub",
                camera_id,
            )
            return {
                "resolution": (4096, 4096),
                "pixel_scale": 1.0,
                "model": "unknown",
                "bit_depth": 16,
            }
        # Echo back the component dict; downstream code can pick what it needs.
        return dict(comp)


def _as_dict(value: Any) -> dict[str, Any] | None:
    """Best-effort coerce a LiveDocument node or plain dict to dict."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict"):
        try:
            return value.to_dict()
        except Exception:  # noqa: BLE001
            pass
    if hasattr(value, "items"):
        try:
            return dict(value.items())
        except Exception:  # noqa: BLE001
            pass
    return None
