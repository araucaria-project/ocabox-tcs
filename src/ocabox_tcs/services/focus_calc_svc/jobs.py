"""Job model and manager for the focus-calculation service.

A *job* is one focusing session: a directory of FITS frames (possibly
still being written), a set of methods to run, and a NATS suffix under
which all of its traffic flows.

Job identity
------------
The inbound subject's suffix (everything after the configured command
root, dots preserved) IS the client-chosen job id, e.g. a message on
``focus.calc.job.zb08.20260804`` addresses job ``zb08.20260804``.
Responses are published with the same suffix. If a client reuses the
id of a *finished* job, the manager mints an *effective id* by
appending ``.h<8-hex>`` — the ``opened`` status message tells the
client which id to listen on. Messages for an *active* id always join
the running job (that is how files/stop reach it).

Lifecycle
---------
``open`` (explicit, or implicit with the first message carrying a
``path``) → any number of ``file`` notifications and/or folder-poll
discoveries, each triggering an update round (all methods re-evaluated,
one result message per method) → ``stop`` message, or idle timeout
(no new input for ``idle_timeout_s``) → ``closed`` status with final
results.

The manager is transport-agnostic: the service wires ``publish_result``
/ ``publish_status`` callables; tests inject fakes and a fake clock.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ocabox_tcs.services.focus_calc_svc.methods import (
    FocusMethod,
    MethodResult,
    create_method,
)


logger = logging.getLogger(__name__.rsplit(".", maxsplit=1)[-1])

#: Frame extensions picked up by the folder poll.
_FITS_SUFFIXES = {".fits", ".fit", ".fts"}

PublishCb = Callable[[str, dict[str, Any]], Awaitable[None]]
"""(effective_job_id, payload) → publish coroutine."""


@dataclass
class FocusJob:
    """State of one focusing session."""

    job_id: str                     # client-chosen suffix
    effective_id: str               # job_id, possibly + ".h<hash>"
    path: Path                      # frame directory (may not exist yet)
    method_names: list[str]
    params: dict[str, Any] = field(default_factory=dict)
    idle_timeout_s: float = 300.0
    scan: dict[str, Any] | None = None   # client-declared focuser scan plan (informational)

    files: list[str] = field(default_factory=list)
    methods: dict[str, FocusMethod] = field(default_factory=dict)
    last_results: dict[str, MethodResult] = field(default_factory=dict)
    seq: int = 0                    # update round counter
    last_input_mono: float = 0.0    # monotonic time of last new input
    closed: bool = False

    def discover_new_files(self) -> list[str]:
        """Poll the job directory for frames not seen yet (sorted by
        mtime so arrival order survives batch drops). Missing directory
        is fine — frames may not exist yet."""
        if not self.path.is_dir():
            return []
        known = set(self.files)
        found = [
            p for p in self.path.iterdir()
            if p.suffix.lower() in _FITS_SUFFIXES and str(p) not in known
        ]
        found.sort(key=lambda p: p.stat().st_mtime)
        return [str(p) for p in found]


class JobManager:
    """Owns all live jobs; the service delegates message handling and
    periodic ticking here."""

    def __init__(
        self,
        publish_result: PublishCb,
        publish_status: PublishCb,
        *,
        default_methods: list[str] | None = None,
        default_idle_timeout_s: float = 300.0,
        max_jobs: int = 16,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._publish_result = publish_result
        self._publish_status = publish_status
        self._default_methods = default_methods or ["laplacian"]
        self._default_idle_timeout_s = default_idle_timeout_s
        self._max_jobs = max_jobs
        self._clock = clock
        self.jobs: dict[str, FocusJob] = {}       # keyed by effective_id
        self._finished_ids: set[str] = set()       # client ids ever used & closed
        self.results_published = 0

    # ------------------------------------------------------------------
    # Message entry point
    # ------------------------------------------------------------------

    async def handle_message(self, job_id: str, data: dict[str, Any]) -> None:
        """Dispatch one inbound message addressed to ``job_id`` (the
        raw subject suffix)."""
        action = data.get("action") or ("open" if "path" in data else None)
        job = self._find_active(job_id)

        if action == "open" and job is None:
            await self._open(job_id, data)
        elif action == "open":
            # Re-open of a live id: treat as a parameter nudge, not an
            # error — but a changed path means the client really wants
            # a fresh job under a fresh id.
            logger.warning("job %s: 'open' while active — ignored", job.effective_id)
        elif job is None:
            logger.warning("message for unknown/closed job %s (action=%s) — dropped",
                           job_id, action)
        elif action == "file":
            fname = data.get("filename")
            if fname:
                await self._ingest(job, [str(Path(job.path) / fname)
                                         if not Path(fname).is_absolute() else fname])
        elif action == "stop":
            await self.close_job(job, reason="stop requested")
        else:
            logger.warning("job %s: unknown action %r — dropped", job.effective_id, action)

    # ------------------------------------------------------------------
    # Periodic tick (folder polling + idle expiry)
    # ------------------------------------------------------------------

    async def tick(self) -> None:
        for job in list(self.jobs.values()):
            new = job.discover_new_files()
            if new:
                await self._ingest(job, new)
            elif (self._clock() - job.last_input_mono) > job.idle_timeout_s:
                await self.close_job(job, reason=f"idle > {job.idle_timeout_s:.0f}s")

    async def close_all(self, reason: str) -> None:
        for job in list(self.jobs.values()):
            await self.close_job(job, reason=reason)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _find_active(self, job_id: str) -> FocusJob | None:
        for job in self.jobs.values():
            if job.job_id == job_id and not job.closed:
                return job
        return None

    def _effective_id(self, job_id: str) -> str:
        """Client id verbatim while unique; append a short stable-ish
        hash once the id has already lived and died."""
        if job_id not in self._finished_ids and job_id not in {
            j.job_id for j in self.jobs.values()
        }:
            return job_id
        salt = f"{job_id}:{self._clock():.6f}".encode()
        return f"{job_id}.h{hashlib.blake2b(salt, digest_size=4).hexdigest()}"

    async def _open(self, job_id: str, data: dict[str, Any]) -> None:
        if len(self.jobs) >= self._max_jobs:
            await self._publish_status(job_id, {
                "event": "rejected", "job_id": job_id,
                "reason": f"job limit reached ({self._max_jobs})",
            })
            return
        path = data.get("path")
        if not path:
            await self._publish_status(job_id, {
                "event": "rejected", "job_id": job_id,
                "reason": "open requires 'path'",
            })
            return
        effective = self._effective_id(job_id)
        job = FocusJob(
            job_id=job_id,
            effective_id=effective,
            path=Path(path),
            method_names=list(data.get("methods") or self._default_methods),
            params=dict(data.get("params") or {}),
            idle_timeout_s=float(data.get("idle_timeout_s")
                                 or self._default_idle_timeout_s),
            scan=data.get("scan"),
            last_input_mono=self._clock(),
        )
        for name in job.method_names:
            try:
                job.methods[name] = create_method(name)
            except (ValueError, RuntimeError) as exc:
                # Unknown/unavailable method → permanent failed result,
                # the job still runs the others.
                job.last_results[name] = MethodResult(
                    method=name, status="failed", message=str(exc))
                logger.warning("job %s: method %s unavailable: %s",
                               effective, name, exc)
        self.jobs[effective] = job
        logger.info("job %s opened: path=%s methods=%s idle_timeout=%.0fs",
                    effective, job.path, job.method_names, job.idle_timeout_s)
        await self._publish_status(effective, {
            "event": "opened",
            "job_id": job_id,
            "effective_id": effective,
            "path": str(job.path),
            "methods": job.method_names,
            "idle_timeout_s": job.idle_timeout_s,
        })
        # Frames may already be waiting in the folder.
        pre = job.discover_new_files()
        if pre:
            await self._ingest(job, pre)

    async def _ingest(self, job: FocusJob, new_files: list[str]) -> None:
        job.files.extend(new_files)
        job.last_input_mono = self._clock()
        job.seq += 1
        for name, method in job.methods.items():
            result = await method.update(job.files, job.params)
            job.last_results[name] = result
            self.results_published += 1
            await self._publish_result(job.effective_id, {
                "seq": job.seq,
                "job_id": job.job_id,
                "effective_id": job.effective_id,
                **result.to_payload(),
            })

    async def close_job(self, job: FocusJob, *, reason: str) -> None:
        if job.closed:
            return
        job.closed = True
        self.jobs.pop(job.effective_id, None)
        self._finished_ids.add(job.job_id)
        logger.info("job %s closed (%s): %d files, %d rounds",
                    job.effective_id, reason, len(job.files), job.seq)
        await self._publish_status(job.effective_id, {
            "event": "closed",
            "job_id": job.job_id,
            "effective_id": job.effective_id,
            "reason": reason,
            "files_total": len(job.files),
            "final": {n: r.to_payload() for n, r in job.last_results.items()},
        })
