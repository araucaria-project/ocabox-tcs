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

import asyncio
import logging
from typing import Any

from serverish.base import dt_utcnow_array

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
from ocabox_tcs.monitoring import Status
from ocabox_tcs.services.guiding_svc.controller import Controller
from ocabox_tcs.services.guiding_svc.nats_conn import NatsConn
from ocabox_tcs.services.guiding_svc.pipeline import Pipeline
from ocabox_tcs.services.guiding_svc.protocols import (
    AlpacaProtocol,
    FileSimProtocol,
    IrisProtocol,
)
from ocabox_tcs.services.guiding_svc.pulse_guide import build_pulse_guide_model
from ocabox_tcs.services.guiding_svc.stages.thumbnail_emitter import ThumbnailEmitter
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
        self._metric_republish_task: asyncio.Task[None] | None = None
        self.tic_conn: Any = None
        self.nats_conn: NatsConn | None = None
        self._started_at: list[int] | None = None

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

        # 4. Discovery metadata — published as a metric on the standard
        # ``svc.status.<service>`` subject (the same one tcsctl reads).
        # UI clients subscribe last-per-subject to that topic and parse
        # ``details.metrics.guider`` for the per-instance subject + RPC map.
        self._started_at = dt_utcnow_array()
        monitor = getattr(self.service, "monitor", None)
        if monitor is not None:
            if hasattr(monitor, "add_metric_cb"):
                # Static service-discovery payload (subjects, RPC vocab).
                monitor.add_metric_cb(self._discovery_metrics)
                # Per-pipeline runtime stats (mode, lock, cycle ratio,
                # ages) — readable from ``svc.status.>`` so tcsctl shows
                # them in --detailed view.
                monitor.add_metric_cb(self._runtime_metrics)
            if hasattr(monitor, "add_healthcheck_cb"):
                # Healthcheck reports IDLE / OK / BUSY / DEGRADED based
                # on aggregate pipeline state. The framework calls this
                # periodically and propagates the returned Status to the
                # service-level status field that tcsctl displays.
                monitor.add_healthcheck_cb(self._healthcheck)
            # Force-republish status (with fresh metric callbacks)
            # every 10 s — the framework's heartbeat path is a
            # lightweight ping that doesn't carry metrics, and
            # ``_send_status_report`` only fires on status *change*.
            # For runtime metrics (cycle counters, ages) to look live
            # on the status stream, we have to nudge the publisher
            # ourselves.
            self._metric_republish_task = asyncio.create_task(
                self._metric_republish_loop(monitor),
                name="guider-metric-republish",
            )

    async def _metric_republish_loop(self, monitor: Any) -> None:
        """Status maintenance loop. Two things every 10 s:

        1. **Drive monitor status** to match aggregate pipeline state.
           The framework's ``add_healthcheck_cb`` only escalates to
           unhealthy values (ERROR / DEGRADED); IDLE / BUSY / OK
           transitions don't propagate via that hook because they're
           all "healthy". So we set them directly here via
           ``monitor.set_status`` — that triggers a status-change
           republish.

        2. **Force a status republish** even if the status didn't
           change, so ``tcsctl --detailed`` (which subscribes to
           ``svc.status.>`` last-per-subject) sees live cycle counters
           / ages from the metric callback. Heartbeats are a separate
           lighter stream that doesn't carry metrics.
        """
        try:
            while True:
                await asyncio.sleep(10.0)
                target = self._healthcheck()
                try:
                    current = monitor.get_status()
                    if target != current:
                        monitor.set_status(target, f"aggregate={target.value}")
                except Exception as e:  # noqa: BLE001
                    self.svc_logger.warning(
                        "status update failed: %s", e
                    )
                send = getattr(monitor, "_send_status_report", None)
                if send is None:
                    continue
                try:
                    await send()
                except Exception as e:  # noqa: BLE001
                    self.svc_logger.warning(
                        "metric republish failed: %s", e
                    )
        except asyncio.CancelledError:
            return

    async def on_stop(self) -> None:
        if self._metric_republish_task is not None:
            self._metric_republish_task.cancel()
            try:
                await self._metric_republish_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._metric_republish_task = None
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
                transpose=bool(protocol_cfg.get("transpose", False)),
            )
        if kind == "iris":
            return IrisProtocol(**protocol_cfg)
        raise ValueError(f"Unknown protocol type {kind!r}")

    def _build_thumbnail_emitter(
        self,
        cam_id: str,
        pipe_id: str,
        thumb_cfg: dict[str, Any],
    ) -> ThumbnailEmitter | None:
        """Build a ThumbnailEmitter for this pipeline if enabled in config.

        Recognised keys (all optional unless ``enabled``):
          ``enabled`` (bool, default False),
          ``output_dir`` (str, default ``/tmp/guider_thumbs``),
          ``size`` ([w, h], default [480, 300]),
          ``every_n`` (int, default 1),
          ``latest_link`` (bool, default True),
          ``quality`` (int, default 80),
          ``queue_depth`` (int, default 2 — small, drop-oldest semantics
            mean we never want more than a couple of frames in flight).
        """
        if not thumb_cfg.get("enabled"):
            return None
        in_q: asyncio.Queue = asyncio.Queue(maxsize=int(thumb_cfg.get("queue_depth", 2)))
        publisher = (
            self.nats_conn.thumbnail_notification_publisher(cam_id)
            if self.nats_conn is not None
            else None
        )
        size = thumb_cfg.get("size", [480, 300])
        return ThumbnailEmitter(
            in_queue=in_q,
            output_dir=thumb_cfg.get("output_dir", "/tmp/guider_thumbs"),
            instance=self.nats_conn.instance if self.nats_conn is not None else "unknown",
            pipeline_id=pipe_id,
            notification_publisher=publisher,
            size=tuple(size),
            every_n=int(thumb_cfg.get("every_n", 1)),
            latest_link=bool(thumb_cfg.get("latest_link", True)),
            quality=int(thumb_cfg.get("quality", 80)),
            max_files=int(thumb_cfg.get("max_files", 200)),
        )

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
        # Accept either FL1 diagonal (kN_px_per_ms + kE_px_per_ms) or
        # FL2 full 2×2 (kE_x/y_px_per_ms + kN_x/y_px_per_ms). Either is
        # enough to build a model; the dispatch happens inside
        # ``build_pulse_guide_model``. Without either, the Enforcer
        # falls back to log-only (no real guiding).
        full_keys = ("kE_x_px_per_ms", "kE_y_px_per_ms", "kN_x_px_per_ms", "kN_y_px_per_ms")
        has_diagonal = "kN_px_per_ms" in jac and "kE_px_per_ms" in jac
        has_full = all(k in jac for k in full_keys)
        if not (has_diagonal or has_full):
            return None, {}

        model = build_pulse_guide_model(pg_cfg)
        alpha = float(pg_cfg.get("damping_alpha", 0.5))
        max_ms = float(pg_cfg.get("duration_max_ms", 1500.0))
        min_ms = float(pg_cfg.get("duration_min_ms", 20.0))
        settle_ms = float(pg_cfg.get("post_pulse_settle_ms", 1000.0))
        kwargs: dict[str, Any] = {
            "damping": DampingGuard(alpha_min=alpha, alpha_max=alpha),
            "saturation_ms": SaturationGuard(lo=-max_ms, hi=max_ms),
            "min_pulse_ms": min_ms,
            "post_pulse_settle_ms": settle_ms,
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
            binning=pipe_cfg.get("binning", 1),
            gain=pipe_cfg.get("gain"),
            frequency=pipe_cfg.get("frequency", 1.0),
            central_point=tuple(pipe_cfg.get("central_point", (1024.0, 1024.0))),
            central_point_default=tuple(pipe_cfg.get("central_point", (1024.0, 1024.0))),
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
        thumbnail_emitter = self._build_thumbnail_emitter(
            cam_id, pipe_id, pipe_cfg.get("thumbnails") or {}
        )
        pipeline = Pipeline(
            initial_state=state,
            collector=collector,
            method=method,
            queue_depth=pipe_cfg.get("queue_depth", 4),
            mount=mount_handle,
            pulse_guide_model=pulse_guide_model,
            enforcer_kwargs=enforcer_kwargs,
            thumbnail_emitter=thumbnail_emitter,
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
            # Wire the Enforcer's event hook into the controller's
            # ``_publish_event`` so per-pulse chart annotations appear
            # on the same per-pipeline events subject the UI already
            # subscribes to. Single-channel = no extra subscription.
            if hasattr(pipeline, "_enforcer") and pipeline._enforcer is not None:
                pipeline._enforcer.event_publisher = controller._publish_event

        await pipeline.start()

        key = f"{cam_id}.{pipe_id}"
        self.pipelines[key] = pipeline
        self.controllers[key] = controller

    # ---- Discovery metadata ----

    def _discovery_metrics(self) -> dict[str, Any]:
        """Metric callback exposing the guider's subject scheme + RPC list.

        Returned data is included in ``svc.status.<service>`` reports under
        ``details.metrics.guider``. UIs and other consumers read the same
        ``svc.status.>`` stream tcsctl uses, so guider discovery is unified
        with the rest of the TCS service surface.

        Static once the pipelines are built — pipeline mode/state changes
        are published on the per-pipeline state subject; this metadata only
        carries the subject names + RPC vocabulary needed to find them.
        """
        if self.nats_conn is None:
            return {}
        from ocabox_tcs.services.guiding_svc.nats_conn import RPC_COMMANDS

        nc = self.nats_conn
        cfg_dict = _config_to_dict(self.svc_config)
        pipelines: list[dict[str, Any]] = []
        for key, pipeline in self.pipelines.items():
            cam_id, pipe_id = key.split(".", 1)
            snapshot = pipeline.state.snapshot()
            pipelines.append({
                "id": pipe_id,
                "camera_id": cam_id,
                "mode": snapshot.mode.value,
                "method": snapshot.method,
                "selection_policy": snapshot.selection_policy,
                "subjects": {
                    "rpc_root": f"{nc._root('rpc')}.pipeline.{pipe_id}.v1",
                    "state": nc.publish_subject(pipe_id, "state"),
                    "events": nc.publish_subject(pipe_id, "events"),
                    "journal": nc.publish_subject(pipe_id, "journal"),
                },
                "rpcs": list(RPC_COMMANDS),
            })
        return {
            "guider": {
                "service": nc.service,
                "instance": nc.instance,
                "telescope_id": cfg_dict.get("telescope_id", "unknown"),
                "variant": getattr(self.svc_config, "variant", None),
                "subject_prefix": nc.subject_prefix,
                "started_at": self._started_at,
                "subjects": {
                    "thumbnail_ready": f"{nc._root('publish')}.frame.thumbnail.ready",
                },
                "pipelines": pipelines,
            }
        }

    # ------------------------------------------------------------------
    # Runtime metrics + healthcheck — small per-cycle live numbers,
    # separate from the static discovery payload above so consumers
    # paying attention only to one or the other don't get noise.
    # ------------------------------------------------------------------

    def _runtime_metrics(self) -> dict[str, Any]:
        """Per-pipeline runtime stats — live mode, lock state, cycle
        counts and ratios, ages of the last cycle / last lock. Lands
        under ``details.metrics.guider_runtime`` on the standard
        ``svc.status.>`` topic, so tcsctl --detailed and any web UI can
        show them without subscribing to the per-pipeline state stream.
        """
        out: dict[str, Any] = {}
        for key, pipeline in self.pipelines.items():
            cam_id, pipe_id = key.split(".", 1)
            snap = pipeline.state.snapshot()
            rt = pipeline.runtime_snapshot()
            out[f"{cam_id}.{pipe_id}"] = {
                "mode": snap.mode.value,
                "acquired": bool(snap.acquired),
                **rt,
            }
        return {"guider_runtime": out}

    def _healthcheck(self) -> Status:
        """Aggregate pipeline state → service status.

        - All pipelines OFF → ``IDLE`` (camera idle, no work)
        - Any pipeline acquired (locked + tracking or guiding) → ``BUSY``
        - All non-OFF but no lock yet → ``OK``
        - Last cycle older than 30 s while non-OFF → ``DEGRADED``
          (camera I/O may have stalled — second observer hogging
          the device, network glitch, mount disconnect, etc.)
        """
        if not self.pipelines:
            return Status.OK
        states = [p.state.snapshot() for p in self.pipelines.values()]
        modes = [s.mode for s in states]
        # Mode import via state — local alias keeps the import set tight.
        from ocabox_tcs.services.guiding_svc.state import Mode  # noqa: PLC0415
        if all(m == Mode.OFF for m in modes):
            return Status.IDLE
        # At least one non-OFF pipeline. Check liveness.
        for pipeline in self.pipelines.values():
            snap = pipeline.state.snapshot()
            if snap.mode == Mode.OFF:
                continue
            rt = pipeline.runtime_snapshot()
            age = rt.get("last_cycle_age_s")
            if age is not None and age > 30.0:
                return Status.DEGRADED
        if any(s.acquired for s in states):
            return Status.BUSY
        return Status.OK


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
