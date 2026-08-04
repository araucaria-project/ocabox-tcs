"""Focus-calculation service — NATS-driven, multi-job, multi-method.

Service type: ``focus_calc_svc.focus_calc`` (filename-derived).

A permanent TCS service that runs focus calculations as *jobs*. A job
is opened by a message pointing at a directory of FITS frames (which
may still be empty — frames can trickle in during a focuser scan); the
service re-evaluates the requested methods after every new frame and
publishes one result message per method per round, including partial
results and — for dynamic-search methods — a suggested next focuser
position or a convergence signal. The service never touches hardware;
the client (TOI, plan runner, a script) drives the focuser.

Message protocol (full spec: ``doc/focus-calc-service.md``)::

    IN   <root>.job.<job.id.parts...>      command channel (sub/pub, JetStream)
    OUT  <root>.result.<effective.id>      one message per method per round
    OUT  <root>.state.<effective.id>       job lifecycle: opened/rejected/closed

The subject suffix after ``<root>.job.`` is the client-chosen job id;
responses reuse it. If the id was already used by a finished job, the
service appends ``.h<hash>`` and announces the *effective id* in the
``opened`` message. Sub/pub (not RPC) because a job is a long-lived
conversation with many messages both ways; a fixed-image-set RPC
variant is a possible later addition.

Configuration (YAML)::

    services:
      - type: focus_calc_svc.focus_calc
        instance_context: main
        subject_root: focus.calc          # subjects live under this root
        default_methods: [laplacian]      # when 'open' names none
        default_idle_timeout_s: 300       # close job after silence
        poll_interval_s: 2.0              # folder polling cadence
        max_jobs: 16

JetStream: a stream must cover ``<root>.>`` (provision via
oca_nats_config, same as ``tic.command.>`` — see project follow-ups).

Observability: standard TCS status/heartbeat; metrics expose active
job count, per-job round counters and the method-availability map, so
``tcsctl --detailed`` shows at a glance what this instance can do.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from serverish.messenger import get_callbacksubscriber, get_publisher

from ocabox_tcs.base_service import (
    BaseBlockingPermanentService,
    BaseServiceConfig,
    config,
    service,
)
from ocabox_tcs.monitoring import Status
from ocabox_tcs.services.focus_calc_svc.jobs import JobManager
from ocabox_tcs.services.focus_calc_svc.methods import available_methods


logger = logging.getLogger(__name__.rsplit(".", maxsplit=1)[-1])


@config("focus_calc_svc.focus_calc")
@dataclass
class FocusCalcConfig(BaseServiceConfig):
    """Configurable surface of the focus-calculation service."""

    #: All subjects (job/result/state) hang under this root.
    subject_root: str = "focus.calc"
    #: Methods used when an ``open`` message names none.
    default_methods: list[str] = field(default_factory=lambda: ["laplacian"])
    #: Job auto-close after this much input silence (no new frames,
    #: no messages). Client can override per job.
    default_idle_timeout_s: float = 300.0
    #: Folder-poll cadence. Explicit ``file`` messages work without
    #: polling; the poll catches clients that only drop files.
    poll_interval_s: float = 2.0
    #: Ceiling on concurrently open jobs; further opens are rejected.
    max_jobs: int = 16


@service("focus_calc_svc.focus_calc")
class FocusCalcService(BaseBlockingPermanentService):
    """Run N concurrent focus-calculation jobs over NATS.

    Lifecycle:
        - ``on_start`` → build the :class:`JobManager`, open the
          command subscriber on ``<root>.job.>``, register metrics
          and healthcheck callbacks.
        - ``run_service`` → periodic tick: folder polling and idle
          expiry, until the framework cancels us.
        - ``on_stop`` → close the subscriber, close remaining jobs
          (clients get a ``closed`` state message with the final
          results and reason ``service stopping``).

    Job children as separate TCS-monitored objects: deliberate later
    option (multi-component services, roadmap Feature 3) — for now
    jobs surface through this service's metrics.
    """

    svc_config: FocusCalcConfig

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._manager: JobManager | None = None
        self._command_sub: Any | None = None
        self._publishers: dict[str, Any] = {}   # subject → MsgPublisher
        self._messages_in = 0
        self._last_error: str | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def on_start(self) -> None:
        cfg = self.svc_config
        self._manager = JobManager(
            publish_result=self._make_publish("result"),
            publish_status=self._make_publish("state"),
            default_methods=list(cfg.default_methods),
            default_idle_timeout_s=cfg.default_idle_timeout_s,
            max_jobs=cfg.max_jobs,
        )
        # ``deliver_policy='new'``: commands from before a restart must
        # not replay — a stale ``open`` would resurrect a dead job and
        # start publishing into a conversation nobody listens to.
        self._command_sub = get_callbacksubscriber(
            f"{cfg.subject_root}.job.>",
            deliver_policy="new",
        )
        await self._command_sub.open()
        await self._command_sub.subscribe(self._on_command)
        self.monitor.add_metric_cb(self._metrics)
        self.monitor.add_healthcheck_cb(self._healthcheck)
        self.svc_logger.info(
            "FocusCalc ready: subscribed %s.job.> (methods: %s)",
            cfg.subject_root,
            ", ".join(f"{k}={v}" for k, v in available_methods().items()),
        )

    async def run_service(self) -> None:
        assert self._manager is not None
        while self.is_running:
            try:
                await self._manager.tick()
            except Exception as exc:  # noqa: BLE001 — tick must survive anything
                self._last_error = str(exc)
                self.svc_logger.exception("job tick failed: %s", exc)
            await asyncio.sleep(self.svc_config.poll_interval_s)

    async def on_stop(self) -> None:
        if self._command_sub is not None:
            try:
                await self._command_sub.close()
            except Exception as exc:  # noqa: BLE001 — defensive teardown
                self.svc_logger.warning("error closing command subscriber: %s", exc)
            self._command_sub = None
        if self._manager is not None:
            await self._manager.close_all(reason="service stopping")
        for pub in self._publishers.values():
            try:
                await pub.close()
            except Exception:  # noqa: BLE001 — defensive teardown
                pass
        self._publishers.clear()

    # ------------------------------------------------------------------
    # NATS plumbing
    # ------------------------------------------------------------------

    async def _on_command(self, data: dict, meta: dict) -> bool:
        """``MsgCallbackSubscriber`` callback on ``<root>.job.>``.
        Always returns True — a bad message must not stop the stream."""
        self._messages_in += 1
        subject = (meta or {}).get("nats", {}).get("subject")
        prefix = f"{self.svc_config.subject_root}.job."
        if not subject or not subject.startswith(prefix):
            self.svc_logger.error("command without usable subject (%r) — dropped", subject)
            return True
        job_id = subject[len(prefix):]
        if not job_id:
            self.svc_logger.error("command with empty job suffix — dropped")
            return True
        try:
            assert self._manager is not None
            await self._manager.handle_message(job_id, data or {})
        except Exception as exc:  # noqa: BLE001 — channel must survive
            self._last_error = str(exc)
            self.svc_logger.exception("handling command for job %s failed: %s", job_id, exc)
        return True

    def _make_publish(self, leaf: str):
        """Publisher callable bound to ``<root>.<leaf>.<effective_id>``,
        with per-subject publisher caching."""
        root = self.svc_config.subject_root

        async def _publish(effective_id: str, payload: dict[str, Any]) -> None:
            subject = f"{root}.{leaf}.{effective_id}"
            pub = self._publishers.get(subject)
            if pub is None:
                pub = get_publisher(subject)
                self._publishers[subject] = pub
            await pub.publish(data=payload)

        return _publish

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    def _metrics(self) -> dict[str, Any]:
        mgr = self._manager
        return {
            "focus_calc": {
                "subject_root": self.svc_config.subject_root,
                "methods": available_methods(),
                "jobs_active": len(mgr.jobs) if mgr else 0,
                "jobs": {
                    eid: {
                        "path": str(j.path),
                        "files": len(j.files),
                        "rounds": j.seq,
                        "methods": j.method_names,
                    }
                    for eid, j in (mgr.jobs.items() if mgr else ())
                },
                "messages_in": self._messages_in,
                "results_published": mgr.results_published if mgr else 0,
                "last_error": self._last_error,
            }
        }

    def _healthcheck(self) -> Status:
        if self._command_sub is None:
            return Status.ERROR
        return Status.OK


if __name__ == "__main__":
    FocusCalcService.main()
