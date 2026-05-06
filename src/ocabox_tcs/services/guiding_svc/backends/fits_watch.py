"""FitsWatchBackend — watch a directory for new FITS files written by an
external downloader process.

**Status**: skeleton. Useful when a separate OFP downloader writes FITS
to a shared filesystem and the guider just consumes new files. Hybrid
of pull (RPC) and watch (filesystem).

For frame iteration: skipped — SimBackend covers the testing case.
"""

from __future__ import annotations

from ocabox_tcs.services.guiding_svc.protocols.base import FetchedFrame


class FitsWatchBackend:
    """Watch a directory; emit `FetchedFrame` for each new FITS file.

    Args:
        watch_dir: Directory to monitor.
        pattern: Glob to match (e.g. "*.fits").
        notification: 'inotify' (Linux) | 'polling' (cross-platform).
    """

    def __init__(
        self,
        watch_dir: str,
        pattern: str = "*.fits",
        notification: str = "polling",
    ) -> None:
        self.watch_dir = watch_dir
        self.pattern = pattern
        self.notification = notification

    @property
    def name(self) -> str:
        return f"fits_watch({self.watch_dir})"

    async def open(self) -> None:
        raise NotImplementedError(
            "FitsWatchBackend.open — start filesystem watcher (inotify "
            "via `asyncinotify` on Linux, polling fallback elsewhere). "
            "Unlike submit-one backends, this one drives a stream "
            "naturally — `subscribe_stream` is the natural API."
        )

    async def close(self) -> None:
        raise NotImplementedError("FitsWatchBackend.close — stop watcher.")

    async def submit_one(
        self,
        exp_time: float,
        roi: tuple[int, int, int, int] | None = None,
        binning: int | tuple[int, int] = 1,
        gain: int | None = None,
    ) -> FetchedFrame:
        raise NotImplementedError(
            "FitsWatchBackend.submit_one — wait for next file matching "
            "filter; load FITS; convert to FetchedFrame. exp_time/roi/etc "
            "are advisory only — backend doesn't drive the camera, it just "
            "reads what was written."
        )
