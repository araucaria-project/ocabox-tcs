"""AlpacaProtocol — hybrid camera I/O.

Control verbs go through ``ocaboxapi.Camera`` (TIC). Image bytes only are
fetched directly via the Alpaca ``imagebytes`` binary endpoint, which is
otherwise too slow when round-tripped through TIC/JSON.

``ocabox_camera`` is required — there is no direct-Alpaca fallback for
control. ``ClientID`` is a stable hash of the TCS instance name; the
``imageready`` poll falls back to a ``camerastate`` transition when
another Alpaca client drains the flag.
"""

from __future__ import annotations

import asyncio
import logging
import time
import zlib
from typing import Any
from urllib.parse import urlencode

import aiohttp
from serverish.base import dt_utcnow_array

from ocaboxapi.exceptions import OcaboxAccessDenied, OcaboxServerError

from ocabox_tcs.services.guiding_svc._borrowed.ofp.alpaca_http import (
    AlpacaFetchError,
    fetch_imagearray,
)
from ocabox_tcs.services.guiding_svc.protocols.base import FetchedFrame


logger = logging.getLogger(__name__.rsplit(".", maxsplit=1)[-1])


# ASCOM Alpaca camerastate values used in `_wait_image_ready` fallback.
CAMERA_STATE_IDLE = 0
CAMERA_STATE_EXPOSING = 2


def _stable_client_id(instance_id: str) -> int:
    """Map a TCS instance name to a stable signed-int32 even ``ClientID``.

    Some Alpaca drivers parse ClientID as signed int32 — values above
    2^31-1 wrap to negative and are rejected.
    """
    return zlib.crc32(instance_id.encode("utf-8")) & 0x7FFF_FFFE


class AlpacaProtocol:
    """Alpaca camera fetcher with ocabox-only control.

    Args:
        instance_id: TCS instance identifier; drives ClientID + User-Agent.
        url: Alpaca server base URL (no ``/api/v1`` suffix; the protocol
            appends per-call paths).
        device_number: Alpaca camera device index.
        ocabox_camera: ``ocaboxapi.Camera`` handle. Required — no fallback.
        prefer_binary: Use the ``imagebytes`` binary content negotiation.
        request_timeout: Per-request HTTP deadline for image GET.
        poll_interval_s: ``imageready`` poll interval during exposure.
        transpose: When True, transpose the fetched 2-D array. Workaround
            for cameras whose downloader returns ASCOM X-major
            (``arr[x][y]``) data — flip to natural numpy ``arr[y][x]``
            so PIL/FFS see ``(height, width)``. Default False preserves
            existing behaviour for cameras already compensated downstream.
    """

    def __init__(
        self,
        *,
        instance_id: str,
        url: str,
        device_number: int,
        ocabox_camera: Any,
        prefer_binary: bool = True,
        request_timeout: float = 30.0,
        poll_interval_s: float = 0.05,
        transpose: bool = False,
    ) -> None:
        if ocabox_camera is None:
            raise ValueError(
                "AlpacaProtocol requires an ocabox_camera handle "
                "(direct-Alpaca control is not allowed)"
            )
        self.instance_id = instance_id
        self.url = url.rstrip("/")
        self.device_number = device_number
        self.ocabox_camera = ocabox_camera
        self.prefer_binary = prefer_binary
        self.request_timeout = request_timeout
        self.poll_interval_s = poll_interval_s
        self.transpose = transpose

        self._client_id = _stable_client_id(instance_id)
        self._txn_counter = 0
        self._session: aiohttp.ClientSession | None = None

        # Last (attempted) driver-side state per parameter. _apply_settings
        # skips the PUT when the requested value matches the cache,
        # otherwise pushes and updates the cache regardless of success —
        # the latter prevents retry-storms at frame rate when the driver
        # consistently rejects a value (out-of-range gain, wrong binning
        # mode). A successful apply at a new value will then unblock
        # subsequent attempts naturally. Cleared on close() so reopening
        # after a driver restart re-pushes everything.
        self._applied_gain: int | None = None
        self._applied_binx: int | None = None
        self._applied_biny: int | None = None

    @property
    def name(self) -> str:
        return f"alpaca({self.url}#camera/{self.device_number})"

    @property
    def client_id(self) -> int:
        return self._client_id

    async def open(self) -> None:
        if self._session is not None and not self._session.closed:
            return
        headers = {"User-Agent": f"tcs-guider/{self.instance_id}"}
        timeout = aiohttp.ClientTimeout(total=None, connect=10.0)
        self._session = aiohttp.ClientSession(headers=headers, timeout=timeout)
        logger.info(
            "AlpacaProtocol opened: instance=%s url=%s device=%d client_id=%d",
            self.instance_id, self.url, self.device_number, self._client_id,
        )

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
        # Forget cached driver state — a fresh open() may face a
        # restarted driver whose actual settings differ from what we
        # last observed.
        self._applied_gain = None
        self._applied_binx = None
        self._applied_biny = None

    async def fetch(
        self,
        exp_time: float,
        roi: tuple[int, int, int, int] | None = None,
        binning: int | tuple[int, int] = 1,
        gain: int | None = None,
    ) -> FetchedFrame:
        if self._session is None:
            raise RuntimeError("AlpacaProtocol.fetch called before open()")
        if roi is not None:
            raise NotImplementedError("ROI is not supported (full sensor only)")

        await self._apply_settings(binning=binning, gain=gain)
        await self.ocabox_camera.aput_startexposure(duration=exp_time, light=True)
        readout_ts = dt_utcnow_array()
        await self._wait_image_ready(exp_time)
        array = await self._fetch_bytes()
        # Per-camera axis-swap workaround: some downloaders return image
        # data in ASCOM X-major order (``arr[x][y]``), others have
        # already-compensated to numpy ``arr[y][x]``. Cameras that need
        # the transpose set ``protocol.transpose: true`` in the guider
        # config — without it the frame ends up rotated 90° relative to
        # the SVG viewBox / ``central_point`` semantics. Long-term the
        # right answer is to detect from ``tic.config.observatory``
        # ``resolution`` vs ``array.shape``; today's flag is the
        # operator-driven escape hatch that keeps existing cameras
        # working untouched.
        if self.transpose and hasattr(array, "ndim") and array.ndim == 2:
            array = array.T

        return FetchedFrame(
            array=array,
            exp_time=exp_time,
            timestamp=readout_ts,
            roi=None,
            binning=binning,
            gain=gain,
            metadata={
                "alpaca_url": self.url,
                "device_number": self.device_number,
                "client_id": self._client_id,
            },
        )

    async def _apply_settings(
        self,
        *,
        binning: int | tuple[int, int],
        gain: int | None,
    ) -> None:
        """Push gain/binning to the driver only when they actually change.

        Re-pushing every frame is wasteful (a 2 Hz pipeline issues
        ~6 redundant PUTs/s when settings are stable) and creates a
        contention window with any other Alpaca client that's reading
        the same camera.

        Gain vs binning failure handling is deliberately asymmetric:

        - **Gain** is a photometric scaling — a driver rejection (value
          out of range, mode-index vs numeric mismatch) only affects
          brightness, not geometry. Soft-fail: log a warning, cache the
          attempted value to suppress retry storms at frame rate, and
          let the fetch continue at whatever gain the camera currently
          has. Operator must change the config (or restart) to force
          another attempt.

        - **Binning** changes the coordinate domain of every downstream
          consumer (centroid, ``central_point``, ``guide_anchor``, the
          jacobian). A silent bin mismatch turns ~1 px drift into ~2 px
          pulse responses — exactly the failure mode where the system
          looks healthy but corrections diverge. We **must not** soft-
          fail here; let the exception propagate, fail the fetch, and
          surface ``degraded`` to the operator so they can intervene.
        """
        if isinstance(binning, tuple):
            binx, biny = binning
        else:
            binx = biny = binning

        if gain is not None and gain != self._applied_gain:
            await self._try_put_gain(gain)
            self._applied_gain = gain
        if binx != self._applied_binx:
            await self.ocabox_camera.aput_binx(binx)
            self._applied_binx = binx
        if biny != self._applied_biny:
            await self.ocabox_camera.aput_biny(biny)
            self._applied_biny = biny

    async def _try_put_gain(self, value: int) -> None:
        try:
            await self.ocabox_camera.aput_gain(value)
        except (OcaboxServerError, OcaboxAccessDenied) as e:
            logger.warning(
                "AlpacaProtocol(%s): driver rejected gain=%s — %s. "
                "Continuing with current driver gain.",
                self.instance_id, value, e,
            )

    # Fraction of the exposure time that must elapse before we trust
    # ``imageready``. The ZWO/ASCOM-Remote stack does not reliably clear
    # the flag on ``startexposure`` — polled immediately, it still
    # reports ``true`` from the *previous* readout, and fetching then
    # returns the previous buffer (observed live 2026-07-30: perfectly
    # alternating fresh/duplicate frames ~180 ms apart, halving the
    # effective frame rate — the FrameDeduplicator caught every one).
    # A frame physically cannot be ready before its exposure has run,
    # so refusing to trust the flag earlier costs nothing; 0.75 leaves
    # margin for camera-side duration rounding.
    _IMAGEREADY_TRUST_FRACTION = 0.75

    async def _wait_image_ready(self, exp_time: float) -> None:
        """Poll ``imageready`` or the ``camerastate`` 2 → 0 transition.

        Called immediately after ``startexposure``. Sleeps out the
        no-trust window (see ``_IMAGEREADY_TRUST_FRACTION``) before the
        first ``imageready`` poll. Some Alpaca clients drain
        ``imageready`` for themselves; the camerastate fallback keeps
        us working under that contention.
        """
        trust_after = self._IMAGEREADY_TRUST_FRACTION * exp_time
        if trust_after > 0:
            await asyncio.sleep(trust_after)
        deadline = time.monotonic() + exp_time + max(10.0, exp_time)
        saw_exposing = False
        first_loss_logged = False

        while True:
            if await self.ocabox_camera.aget_imageready():
                return
            state = await self.ocabox_camera.aget_camerastate()
            if state == CAMERA_STATE_EXPOSING:
                saw_exposing = True
            elif saw_exposing and state == CAMERA_STATE_IDLE:
                if not first_loss_logged:
                    logger.warning(
                        "_wait_image_ready: camerastate transitioned "
                        "Exposing→Idle without imageready=true (camera "
                        "firmware quirk, transient network glitch during "
                        "exposure, or another Alpaca client reading the "
                        "frame first). Proceeding to fetch the buffer "
                        "anyway. NOTE: the buffer may be a STALE repeat "
                        "of the previous frame — stale bytes contain a "
                        "perfectly detectable (old) star, so the "
                        "collector's FrameDeduplicator drops content-"
                        "identical frames before they reach the solver "
                        "(the 2026-07-29 frozen-buffer incident guided "
                        "blind on one for 10.5 h).",
                    )
                    first_loss_logged = True
                return
            if time.monotonic() > deadline:
                raise AlpacaFetchError(
                    f"image-not-ready timeout after "
                    f"{exp_time + max(10.0, exp_time):.1f}s "
                    f"(saw_exposing={saw_exposing} last_state={state})"
                )
            await asyncio.sleep(self.poll_interval_s)

    async def _fetch_bytes(self) -> Any:
        url = self._build_url("imagearray")
        assert self._session is not None
        try:
            return await fetch_imagearray(
                self._session,
                url,
                prefer_binary=self.prefer_binary,
                request_timeout=self.request_timeout,
            )
        except AlpacaFetchError:
            raise
        except aiohttp.ClientError as e:
            raise AlpacaFetchError(f"HTTP transport error: {e}") from e

    def _build_url(self, action: str) -> str:
        params = urlencode(
            {
                "ClientID": self._client_id,
                "ClientTransactionID": self._next_txn(),
            }
        )
        return f"{self.url}/api/v1/camera/{self.device_number}/{action}?{params}"

    def _next_txn(self) -> int:
        # Stay in signed-int32 range — see _stable_client_id.
        self._txn_counter = (self._txn_counter + 1) & 0x7FFF_FFFF
        return self._txn_counter
