"""FileSimProtocol — concrete file-based simulator.

Emits frames from a folder of pre-recorded FITS files (or a single FITS
cycled), with configurable cadence. Useful for offline testing and
end-to-end smoke runs without hardware.

This is one of the few **concrete** implementations in the frame
iteration; everything else is stubbed.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import numpy as np
from serverish.base import dt_utcnow_array

from ocabox_tcs.services.guiding_svc.protocols.base import FetchedFrame


logger = logging.getLogger(__name__.rsplit(".", maxsplit=1)[-1])


class FileSimProtocol:
    """Simulate a camera by replaying FITS files (or canned arrays).

    Frame iteration uses **synthetic noise frames** (deterministic from a
    seed) when no `files_glob` resolves to anything; this keeps the
    skeleton runnable without test data on disk.

    Args:
        files_glob: Glob pattern resolving to FITS files. Frames are
            cycled in lexical order. When None or no files match, the
            protocol falls back to synthetic noise frames.
        sensor_shape: Sensor dimensions for synthetic frames (default
            512x512 — small enough for cheap CI runs).
        seed: Seed for synthetic frame generator (deterministic).
        readout_delay_s: Simulated readout time per fetch.
    """

    def __init__(
        self,
        files_glob: str | None = None,
        sensor_shape: tuple[int, int] = (512, 512),
        seed: int = 42,
        readout_delay_s: float = 0.05,
    ) -> None:
        self.files_glob = files_glob
        self.sensor_shape = sensor_shape
        self.seed = seed
        self.readout_delay_s = readout_delay_s
        self._files: list[Path] = []
        self._index: int = 0
        self._rng: np.random.Generator | None = None
        self._opened: bool = False

    @property
    def name(self) -> str:
        return f"file_sim({self.files_glob or 'synthetic'})"

    async def open(self) -> None:
        if self._opened:
            return
        if self.files_glob:
            from glob import glob

            self._files = sorted(Path(p) for p in glob(self.files_glob))
            logger.info("FileSimProtocol resolved %d files", len(self._files))
        if not self._files:
            self._rng = np.random.default_rng(self.seed)
            logger.info(
                "FileSimProtocol: no files; emitting synthetic noise frames "
                "(shape=%s, seed=%d)",
                self.sensor_shape,
                self.seed,
            )
        self._opened = True

    async def close(self) -> None:
        self._opened = False
        self._files = []
        self._rng = None

    async def fetch(
        self,
        exp_time: float,
        roi: tuple[int, int, int, int] | None = None,
        binning: int | tuple[int, int] = 1,
        gain: int | None = None,
    ) -> FetchedFrame:
        if not self._opened:
            raise RuntimeError("FileSimProtocol.fetch() before open()")
        await asyncio.sleep(self.readout_delay_s)

        if self._files:
            array = await asyncio.to_thread(self._read_fits, self._files[self._index])
            self._index = (self._index + 1) % len(self._files)
        else:
            assert self._rng is not None
            array = self._synthetic_frame()

        if roi is not None:
            x, y, w, h = roi
            array = array[y : y + h, x : x + w]

        return FetchedFrame(
            array=array,
            exp_time=exp_time,
            timestamp=dt_utcnow_array(),
            roi=roi,
            binning=binning,
            gain=gain,
            metadata={"source": "file_sim"},
        )

    # -- internals ---------------------------------------------------

    @staticmethod
    def _read_fits(path: Path) -> np.ndarray:
        # Lazy astropy import — keeps base import cheap.
        from astropy.io import fits

        with fits.open(path) as hdul:
            data = hdul[0].data  # type: ignore[index]
            return np.asarray(data, dtype=np.float32)

    def _synthetic_frame(self) -> np.ndarray:
        """Deterministic synthetic frame: Poisson noise + injected star
        near sensor centre with small jitter (so dummy guider has
        something to track).
        """
        assert self._rng is not None
        h, w = self.sensor_shape
        # Background: low Poisson + bias offset
        frame = self._rng.poisson(lam=200, size=(h, w)).astype(np.float32) + 100.0
        # Injected star with random sub-pixel jitter
        cy, cx = h // 2, w // 2
        jitter_y = self._rng.normal(0, 1.0)
        jitter_x = self._rng.normal(0, 1.0)
        ys, xs = np.mgrid[:h, :w]
        sigma = 2.0
        flux = 30_000.0
        gaussian = flux * np.exp(
            -((xs - cx - jitter_x) ** 2 + (ys - cy - jitter_y) ** 2)
            / (2 * sigma * sigma)
        )
        frame += gaussian.astype(np.float32)
        return frame
