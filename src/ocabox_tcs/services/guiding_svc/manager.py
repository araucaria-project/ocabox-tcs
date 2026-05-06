"""GuiderManager — heavy-lifting orchestrator.

Constructed by the @service entry (`guider.py`); owns the camera
collectors, pipelines, controllers, NATS plumbing.

Lifecycle (frame iteration):
  on_start:
    1. Open TicConn (LiveDocument for tic.config.observatory).
    2. For each configured camera:
       - Build CameraArrayCollector with the configured Backend.
       - Open it.
       - For each pipeline:
         - Build PipelineState from YAML.
         - Build SolverMethod (from METHODS registry; dummy for frame iter).
         - Build Pipeline + Controller.
         - Start Pipeline.
    3. Open NatsConn; register RPC handlers per pipeline.
  on_stop:
    Reverse the above.
"""

from __future__ import annotations

import logging
from typing import Any

from auto_adjust.stability import DampingGuard, SaturationGuard
from ocabox_tcs.services.guiding_svc.backends import (
    DirectFetchBackend,
    DownloaderRPCBackend,
    FitsWatchBackend,
    SimBackend,
)
from ocabox_tcs.services.guiding_svc.camera_array_collector import (
    CameraArrayCollector,
)
from ocabox_tcs.services.guiding_svc.controller import Controller
from ocabox_tcs.services.guiding_svc.nats_conn import NatsConn
from ocabox_tcs.services.guiding_svc.pipeline import Pipeline
from ocabox_tcs.services.guiding_svc.protocols import (
    AlpacaProtocol,
    FileSimProtocol,
    IrisProtocol,
)
from ocabox_tcs.services.guiding_svc.pulse_guide import build_pulse_guide_model
from ocabox_tcs.services.guiding_svc.stages.solver.methods import METHODS
from ocabox_tcs.services.guiding_svc.state import (
    AutoExposureConfig,
    CalibrationConfig,
    Mode,
    PipelineState,
    PreprocessingConfig,
    ROIConfig,
)


class GuiderManager:
    """Orchestrate cameras, pipelines, NATS plumbing for the guider
    service.

    Args:
        service: The TCS BaseBlockingPermanentService instance (for
            access to svc_logger / svc_config / monitor / etc.).
    """

    def __init__(self, service: Any) -> None:
        self.service = service
        self.svc_logger: logging.Logger = service.svc_logger
        self.svc_config = service.svc_config
        self.collectors: dict[str, CameraArrayCollector] = {}
        self.pipelines: dict[str, Pipeline] = {}  # keyed by f"{cam}.{pipe}"
        self.controllers: dict[str, Controller] = {}
        self.tic_conn: Any = None
        self.nats_conn: NatsConn | None = None

    # ---- Lifecycle ----

    async def on_start(self) -> None:
        from ocabox_tcs.services.guiding_svc.tic_conn import TicConn

        cfg_dict = _config_to_dict(self.svc_config)
        tic_cfg = cfg_dict.get("tic_conn") or {}

        # 1. Observatory config (LiveDocument) + ocabox handles
        self.tic_conn = TicConn(
            manager=self,
            telescope_id=cfg_dict.get("telescope_id", "unknown"),
            client_name=tic_cfg.get("client_name", "CliClient"),
            software_id=tic_cfg.get("software_id"),
            obs_config_subject=tic_cfg.get(
                "obs_config_subject", "tic.config.observatory"
            ),
            enable_observatory=tic_cfg.get("enable_observatory", True),
            service_mode=tic_cfg.get("service_mode", True),
        )
        await self.tic_conn.open()

        # 2. NATS plumbing
        # NATS instance is composed `<telescope>.<camera>` (dot-hierarchical)
        # for clean wildcard matching; this is independent of the TCS
        # variant string (which forbids dots and uses `<tel>-<cam>`).
        self.nats_conn = NatsConn(
            manager=self,
            subject_prefix=cfg_dict.get("subject_prefix", "svc"),
            service=cfg_dict.get("service_name", "guider"),
            instance=self._nats_instance(cfg_dict),
        )
        await self.nats_conn.open()

        # 3. Cameras + pipelines
        cameras_cfg = cfg_dict.get("cameras") or []
        for cam_cfg in cameras_cfg:
            await self._build_camera(cam_cfg)

        self.svc_logger.info(
            "GuiderManager started: %d camera(s), %d pipeline(s)",
            len(self.collectors),
            len(self.pipelines),
        )

    async def on_stop(self) -> None:
        # Stop in reverse order
        for key, pipeline in list(self.pipelines.items()):
            try:
                await pipeline.stop()
            except Exception as e:  # noqa: BLE001
                self.svc_logger.exception("Failed to stop pipeline %s: %s", key, e)

        for cam_id, collector in list(self.collectors.items()):
            try:
                await collector.close()
            except Exception as e:  # noqa: BLE001
                self.svc_logger.exception(
                    "Failed to close collector %s: %s", cam_id, e
                )

        if self.nats_conn:
            await self.nats_conn.close()
        if self.tic_conn:
            await self.tic_conn.close()

        self.svc_logger.info("GuiderManager stopped")

    # ---- Construction helpers ----

    async def _build_camera(self, cam_cfg: dict[str, Any]) -> None:
        cam_id = cam_cfg["id"]
        backend = self._build_backend(cam_id, cam_cfg.get("backend", {}))
        collector = CameraArrayCollector(camera_id=cam_id, backend=backend)
        await collector.open()
        self.collectors[cam_id] = collector

        for pipe_cfg in cam_cfg.get("pipelines", []):
            await self._build_pipeline(cam_id, pipe_cfg, collector)

    def _build_backend(self, cam_id: str, backend_cfg: dict[str, Any]):
        kind = backend_cfg.get("type", "sim")
        if kind == "sim":
            protocol_cfg = backend_cfg.get("protocol", {})
            protocol = self._build_protocol(cam_id, protocol_cfg)
            return SimBackend(protocol=protocol)
        if kind == "direct_fetch":
            protocol_cfg = backend_cfg.get("protocol", {})
            protocol = self._build_protocol(cam_id, protocol_cfg)
            return DirectFetchBackend(protocol=protocol)
        if kind == "downloader_rpc":
            return DownloaderRPCBackend(
                rpc_subject=backend_cfg["rpc_subject"],
                timeout_s=backend_cfg.get("timeout_s", 10.0),
            )
        if kind == "fits_watch":
            return FitsWatchBackend(
                watch_dir=backend_cfg["watch_dir"],
                pattern=backend_cfg.get("pattern", "*.fits"),
                notification=backend_cfg.get("notification", "polling"),
            )
        raise ValueError(f"Unknown backend type {kind!r} for camera {cam_id}")

    def _build_protocol(self, cam_id: str, protocol_cfg: dict[str, Any]):
        kind = protocol_cfg.get("type", "file_sim")
        if kind == "file_sim":
            return FileSimProtocol(
                files_glob=protocol_cfg.get("files_glob"),
                sensor_shape=tuple(protocol_cfg.get("sensor_shape", (512, 512))),
                seed=protocol_cfg.get("seed", 42),
                readout_delay_s=protocol_cfg.get("readout_delay_s", 0.05),
            )
        if kind == "alpaca":
            # Resolve URL and device_number — explicit config wins, then
            # observatory LiveDocument, in that order. URL passed to
            # AlpacaProtocol is the *base* (no /api/v1 suffix); the
            # protocol appends per-call paths.
            url = protocol_cfg.get("url")
            device_number = protocol_cfg.get(
                "device_number", protocol_cfg.get("device_id")
            )
            if (url is None or device_number is None) and self.tic_conn is not None:
                resolved = self.tic_conn.resolve_alpaca_endpoint(cam_id)
                if resolved is not None:
                    auto_url, auto_dev = resolved
                    url = url or auto_url
                    if device_number is None:
                        device_number = auto_dev
            if url is None:
                raise ValueError(
                    f"camera {cam_id!r}: alpaca protocol url unresolved "
                    "(set protocol.url in config or ensure component is in "
                    "tic.config.observatory)"
                )
            if device_number is None:
                device_number = 0

            ocabox_camera = (
                self.tic_conn.get_camera_handle(cam_id)
                if self.tic_conn is not None
                else None
            )
            return AlpacaProtocol(
                instance_id=self._instance_id_for_camera(cam_id),
                url=url,
                device_number=int(device_number),
                ocabox_camera=ocabox_camera,
                prefer_binary=protocol_cfg.get("prefer_binary", True),
            )
        if kind == "iris":
            return IrisProtocol(**protocol_cfg)
        raise ValueError(f"Unknown protocol type {kind!r}")

    def _build_pulse_guide(
        self, pg_cfg: dict[str, Any]
    ) -> tuple[Any | None, dict[str, Any]]:
        """Build the pulse-guide model + Enforcer guards from per-pipeline config.

        Returns ``(model_or_None, enforcer_kwargs)``. When ``pg_cfg`` is empty
        or doesn't include the required jacobian, returns ``(None, {})`` and
        the Enforcer falls back to log-only.

        Recognised keys:
          ``jacobian.kN_px_per_ms``, ``jacobian.kE_px_per_ms`` (required for
            the FL1 fixed-Jacobian model),
          ``damping_alpha`` (default 0.5),
          ``duration_max_ms`` (default 1500),
          ``duration_min_ms`` (default 20).
        """
        jac = pg_cfg.get("jacobian") or {}
        if "kN_px_per_ms" not in jac or "kE_px_per_ms" not in jac:
            return None, {}

        model = build_pulse_guide_model(pg_cfg)
        alpha = float(pg_cfg.get("damping_alpha", 0.5))
        max_ms = float(pg_cfg.get("duration_max_ms", 1500.0))
        min_ms = float(pg_cfg.get("duration_min_ms", 20.0))
        kwargs: dict[str, Any] = {
            "damping": DampingGuard(alpha_min=alpha, alpha_max=alpha),
            "saturation_ms": SaturationGuard(lo=-max_ms, hi=max_ms),
            "min_pulse_ms": min_ms,
        }
        return model, kwargs

    def _nats_instance(self, cfg_dict: dict[str, Any]) -> str:
        """NATS instance segment = ``<telescope>.<camera>`` (hierarchical).

        Composed from telescope_id + the (single) configured camera so
        wildcard subscriptions work cleanly. Distinct from the TCS variant
        string, which uses a hyphen because TCS forbids dots in variants.
        """
        tel = cfg_dict.get("telescope_id", "unknown")
        cams = cfg_dict.get("cameras") or []
        cam_id = cams[0]["id"] if cams else "default"
        return f"{tel}.{cam_id}"

    def _instance_id_for_camera(self, cam_id: str) -> str:
        """Stable identifier used by AlpacaProtocol for ClientID + User-Agent.

        Format: ``<service_type>.<telescope>.<camera>`` — independent of
        the TCS variant string.
        """
        cfg = self.svc_config
        svc_type = getattr(cfg, "type", "guiding_svc.guider")
        tel = getattr(cfg, "telescope_id", "unknown")
        return f"{svc_type}.{tel}.{cam_id}"

    async def _build_pipeline(
        self,
        cam_id: str,
        pipe_cfg: dict[str, Any],
        collector: CameraArrayCollector,
    ) -> None:
        pipe_id = pipe_cfg["id"]

        method_name = pipe_cfg.get("method", "dummy")
        method_cls = METHODS.get(method_name)
        if method_cls is None:
            raise ValueError(f"Unknown solver method {method_name!r}")
        method = method_cls(**pipe_cfg.get("method_params", {}))

        state = PipelineState(
            pipeline_id=pipe_id,
            camera_id=cam_id,
            mode=Mode(pipe_cfg.get("mode", "off")),
            method=method_name,
            method_params=pipe_cfg.get("method_params", {}),
            selection_policy=pipe_cfg.get("selection_policy", "brightest_in_window"),
            exp_time=pipe_cfg.get("exp_time", 1.0),
            current_exp_time=pipe_cfg.get("exp_time", 1.0),
            binning=pipe_cfg.get("binning", 1),
            gain=pipe_cfg.get("gain"),
            frequency=pipe_cfg.get("frequency", 1.0),
            central_point=tuple(pipe_cfg.get("central_point", (1024.0, 1024.0))),
            wide_search_radius_px=pipe_cfg.get("wide_search_radius_px", 200),
            search_reg_px=pipe_cfg.get("search_reg_px", 25),
            stacking_count=pipe_cfg.get("stacking_count", 1),
            stacking_method=pipe_cfg.get("stacking_method", "median"),
            corrections_avg_no=pipe_cfg.get("corrections_avg_no", 5),
            corrections_avg_method=pipe_cfg.get("corrections_avg_method", "median"),
            adu_match_tolerance_per_sec=pipe_cfg.get("adu_match_tolerance_per_sec", 5_000.0),
            auto_exposure=AutoExposureConfig(**pipe_cfg.get("auto_exposure", {})),
            roi=ROIConfig(**pipe_cfg.get("roi", {})),
            calibration=CalibrationConfig(**pipe_cfg.get("calibration", {}) or {}),
            preprocessing=PreprocessingConfig(**pipe_cfg.get("preprocessing", {})),
            save_raw_fits=pipe_cfg.get("save_raw_fits", False),
            save_stacked_fits=pipe_cfg.get("save_stacked_fits", False),
            save_raw_thumbnails=pipe_cfg.get("save_raw_thumbnails", False),
            save_stacked_thumbnails=pipe_cfg.get("save_stacked_thumbnails", False),
        )

        pulse_guide_model, enforcer_kwargs = self._build_pulse_guide(
            pipe_cfg.get("pulse_guide") or {}
        )
        mount_handle = (
            self.tic_conn.get_mount_handle()
            if self.tic_conn is not None
            else None
        )
        pipeline = Pipeline(
            initial_state=state,
            collector=collector,
            method=method,
            queue_depth=pipe_cfg.get("queue_depth", 4),
            mount=mount_handle,
            pulse_guide_model=pulse_guide_model,
            enforcer_kwargs=enforcer_kwargs,
        )
        controller = Controller(pipeline)

        # Wire the controller into the Solver so the active method can call
        # controller.notify_acquired(...) on lock-state changes.
        if hasattr(pipeline, "_solver") and hasattr(pipeline._solver, "set_controller"):
            pipeline._solver.set_controller(controller)

        # Register RPCs and wire publishers (no-op when Messenger isn't open).
        if self.nats_conn is not None:
            await self.nats_conn.register_pipeline_rpcs(cam_id, pipe_id, controller)
            controller.state_publisher = self.nats_conn.state_publisher(cam_id, pipe_id)
            controller.events_publisher = self.nats_conn.events_publisher(cam_id, pipe_id)
            controller.journal_publisher = self.nats_conn.journal_publisher(cam_id, pipe_id)
            controller.sender_id = f"{cam_id}.{pipe_id}"

        await pipeline.start()

        key = f"{cam_id}.{pipe_id}"
        self.pipelines[key] = pipeline
        self.controllers[key] = controller


def _config_to_dict(svc_config: Any) -> dict[str, Any]:
    """Convert a TCS svc_config (dataclass or BaseServiceConfig) to a
    plain dict for our schema-flexible parsing."""
    from dataclasses import asdict, is_dataclass

    if is_dataclass(svc_config):
        return asdict(svc_config)
    if isinstance(svc_config, dict):
        return svc_config
    # Best-effort: attribute-style access
    return {
        k: getattr(svc_config, k)
        for k in dir(svc_config)
        if not k.startswith("_") and not callable(getattr(svc_config, k))
    }
