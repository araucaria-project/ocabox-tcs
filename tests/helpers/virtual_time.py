"""Virtual time for unit tests — deterministic replacement for real sleeps.

A unit test must never spend wall-clock time waiting for the code under
test to finish sleeping: the wait is pure cost, and "long enough" is a
guess that turns into a flake on a loaded machine.

``VirtualClock`` swaps the *module-local* ``asyncio`` and ``time``
bindings of the module under test (never the shared stdlib modules —
the event loop itself reads ``time.monotonic`` and would break), so
``asyncio.sleep`` returns immediately after advancing a fake clock that
``time.monotonic`` reads back. Every ``await`` point is preserved, so
task interleaving is unchanged; the requested delays are recorded, which
makes the timing contract assertable instead of merely endured.

Usage::

    clock = VirtualClock()
    clock.install(monkeypatch, module_under_test)
    await code_that_sleeps()
    assert clock.sleeps == [0.5]      # the delay is now an assertion
    assert clock.elapsed >= deadline  # ... and so is the deadline
"""

from __future__ import annotations

import asyncio
import time as _time
from typing import Any


class _ModuleShim:
    """Stand-in for a module: named overrides win, the rest proxies through.

    Lets a test replace one function of a module-level import (e.g.
    ``asyncio.sleep``) without hiding the dozens of other attributes the
    module under test legitimately uses (``asyncio.Queue``,
    ``asyncio.CancelledError``, ...).
    """

    def __init__(self, wrapped: Any, **overrides: Any) -> None:
        self.__dict__["_wrapped"] = wrapped
        self.__dict__.update(overrides)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.__dict__["_wrapped"], name)


class VirtualClock:
    """Fake monotonic clock advanced by the sleeps of the code under test."""

    # Starts at zero so that `elapsed` is the exact float sum of the
    # delays slept — a large origin would round the difference and break
    # `>= expected_delay` assertions.
    def __init__(self, start: float = 0.0) -> None:
        self._start = start
        self.now = start
        self.sleeps: list[float] = []

    @property
    def elapsed(self) -> float:
        return self.now - self._start

    def monotonic(self) -> float:
        return self.now

    def advance(self, delta: float) -> None:
        """Move the clock forward without a sleep (for wall-clock-driven
        expiry such as a TTL sweep, which no ``await`` will advance)."""
        self.now += delta

    async def sleep(self, delay: float = 0.0) -> None:
        self.sleeps.append(delay)
        self.now += delay
        # Real yield: the code under test relies on sleep as a suspension
        # point where sibling tasks get to run.
        await asyncio.sleep(0)

    def install(self, monkeypatch: Any, *modules: Any) -> None:
        """Redirect each module's ``asyncio.sleep`` / ``time.monotonic``."""
        for module in modules:
            if hasattr(module, "asyncio"):
                monkeypatch.setattr(
                    module, "asyncio", _ModuleShim(asyncio, sleep=self.sleep)
                )
            if hasattr(module, "time"):
                monkeypatch.setattr(
                    module, "time", _ModuleShim(_time, monotonic=self.monotonic)
                )
