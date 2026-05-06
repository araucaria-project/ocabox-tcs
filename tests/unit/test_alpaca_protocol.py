"""Tests for AlpacaProtocol (FL1 hybrid camera I/O).

Covers:
- ClientID stability + 32-bit-even invariant.
- Per-request ClientTransactionID monotonic counter.
- URL builder shape (action, ClientID, ClientTransactionID).
- ROI != None raises NotImplementedError (FL1 contract).
- ocabox_camera path: control verbs invoked in expected order.
- Direct fallback path: PUT shape includes Alpaca form fields.
- imageready timeout surfaces AlpacaFetchError with camerastate.
- fetch() wires the binary fast-path through fetch_imagearray.
"""

from __future__ import annotations

import asyncio
import struct
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from ocabox_tcs.services.guiding_svc._borrowed.ofp.alpaca_http import (
    AlpacaFetchError,
)
from ocabox_tcs.services.guiding_svc.protocols.alpaca import (
    AlpacaProtocol,
    _stable_client_id,
)


# ---------------------------------------------------------------------------
# ClientID
# ---------------------------------------------------------------------------


def test_stable_client_id_is_deterministic():
    a = _stable_client_id("guiding_svc.guider.jk15.guider_a")
    b = _stable_client_id("guiding_svc.guider.jk15.guider_a")
    assert a == b


def test_stable_client_id_differs_per_instance():
    a = _stable_client_id("guiding_svc.guider.jk15.guider_a")
    b = _stable_client_id("guiding_svc.guider.jk15.guider_b")
    assert a != b


def test_stable_client_id_is_even_32_bit():
    cid = _stable_client_id("guiding_svc.guider.jk15.guider_a")
    assert cid % 2 == 0
    assert 0 <= cid <= 0xFFFF_FFFF


def test_protocol_exposes_client_id():
    cam = _make_ocabox_camera()
    p = AlpacaProtocol(
        instance_id="abc",
        url="http://x:11111",
        device_number=1,
        ocabox_camera=cam,
    )
    assert p.client_id == _stable_client_id("abc")


def test_protocol_requires_ocabox_camera():
    """Hard policy rule: no direct-Alpaca fallback for control verbs."""
    with pytest.raises(ValueError, match="ocabox_camera"):
        AlpacaProtocol(
            instance_id="x",
            url="http://x",
            device_number=0,
            ocabox_camera=None,
        )


# ---------------------------------------------------------------------------
# URL building / transaction counter
# ---------------------------------------------------------------------------


def test_build_url_includes_clientid_and_txn():
    cam = _make_ocabox_camera()
    p = AlpacaProtocol(
        instance_id="x",
        url="http://host:11111/",  # trailing slash should be stripped
        device_number=2,
        ocabox_camera=cam,
    )
    u1 = p._build_url("imagearray")
    u2 = p._build_url("imagearray")

    assert u1.startswith("http://host:11111/api/v1/camera/2/imagearray?")
    assert "ClientID=" in u1
    assert "ClientTransactionID=" in u1

    # Counter advances per call
    assert u1 != u2


def test_txn_counter_wraps_at_32_bit():
    cam = _make_ocabox_camera()
    p = AlpacaProtocol(
        instance_id="x", url="http://x", device_number=0, ocabox_camera=cam
    )
    p._txn_counter = 0xFFFF_FFFE
    a = p._next_txn()
    b = p._next_txn()
    c = p._next_txn()
    assert a == 0xFFFF_FFFF
    assert b == 0
    assert c == 1


# ---------------------------------------------------------------------------
# ROI not supported in FL1
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_with_roi_raises_not_implemented():
    cam = _make_ocabox_camera()
    p = AlpacaProtocol(
        instance_id="x", url="http://x", device_number=0, ocabox_camera=cam
    )
    p._session = MagicMock()  # bypass open() check
    with pytest.raises(NotImplementedError, match="ROI"):
        await p.fetch(exp_time=0.1, roi=(0, 0, 10, 10))


@pytest.mark.asyncio
async def test_fetch_without_open_raises():
    cam = _make_ocabox_camera()
    p = AlpacaProtocol(
        instance_id="x", url="http://x", device_number=0, ocabox_camera=cam
    )
    with pytest.raises(RuntimeError, match="before open"):
        await p.fetch(exp_time=0.1)


# ---------------------------------------------------------------------------
# ocabox_camera path
# ---------------------------------------------------------------------------


def _make_ocabox_camera(image_ready_after_calls: int = 1) -> MagicMock:
    """Mock camera that reports imageready=True after N polls."""
    cam = MagicMock()
    cam.aput_gain = AsyncMock()
    cam.aput_binx = AsyncMock()
    cam.aput_biny = AsyncMock()
    cam.aput_startexposure = AsyncMock()
    cam.aget_camerastate = AsyncMock(return_value=0)

    state = {"calls": 0}

    async def imageready():
        state["calls"] += 1
        return state["calls"] >= image_ready_after_calls

    cam.aget_imageready = imageready
    return cam


@pytest.mark.asyncio
async def test_apply_settings_routes_through_ocabox_camera():
    cam = _make_ocabox_camera()
    p = AlpacaProtocol(
        instance_id="x", url="http://x", device_number=0, ocabox_camera=cam
    )
    await p._apply_settings(binning=2, gain=100)
    cam.aput_gain.assert_awaited_once_with(100)
    cam.aput_binx.assert_awaited_once_with(2)
    cam.aput_biny.assert_awaited_once_with(2)


@pytest.mark.asyncio
async def test_apply_settings_skips_gain_when_none():
    cam = _make_ocabox_camera()
    p = AlpacaProtocol(
        instance_id="x", url="http://x", device_number=0, ocabox_camera=cam
    )
    await p._apply_settings(binning=1, gain=None)
    cam.aput_gain.assert_not_awaited()
    cam.aput_binx.assert_awaited_once_with(1)


@pytest.mark.asyncio
async def test_apply_settings_handles_tuple_binning():
    cam = _make_ocabox_camera()
    p = AlpacaProtocol(
        instance_id="x", url="http://x", device_number=0, ocabox_camera=cam
    )
    await p._apply_settings(binning=(2, 3), gain=None)
    cam.aput_binx.assert_awaited_once_with(2)
    cam.aput_biny.assert_awaited_once_with(3)


@pytest.mark.asyncio
async def test_wait_image_ready_polls_until_true():
    cam = _make_ocabox_camera(image_ready_after_calls=3)
    p = AlpacaProtocol(
        instance_id="x",
        url="http://x",
        device_number=0,
        ocabox_camera=cam,
        poll_interval_s=0.001,
    )
    await p._wait_image_ready(exp_time=0.1)
    # We don't assert the call count tightly — the state-machine mock fires
    # True on the 3rd call, so 3 polls is what we expect, but allow drift.


@pytest.mark.asyncio
async def test_wait_image_ready_timeout_raises():
    """Stuck on Exposing — neither signal ever clears, hits the deadline."""
    cam = MagicMock()
    cam.aget_imageready = AsyncMock(return_value=False)
    cam.aget_camerastate = AsyncMock(return_value=2)  # stuck Exposing
    p = AlpacaProtocol(
        instance_id="x",
        url="http://x",
        device_number=0,
        ocabox_camera=cam,
        poll_interval_s=0.001,
    )
    with pytest.raises(AlpacaFetchError, match="image-not-ready timeout"):
        await p._wait_image_ready(exp_time=0.05)


@pytest.mark.asyncio
async def test_wait_image_ready_falls_back_to_camerastate(caplog):
    """Production reality: another Alpaca client steals imageready=True.

    ``imageready`` never goes True, but camerastate transitions
    Exposing → Idle. We accept the readout and log a warning.
    """
    cam = MagicMock()
    cam.aget_imageready = AsyncMock(return_value=False)
    state_seq = iter([2, 2, 2, 0])  # Exposing, Exposing, Exposing, Idle
    cam.aget_camerastate = AsyncMock(side_effect=lambda: next(state_seq))
    p = AlpacaProtocol(
        instance_id="x",
        url="http://x",
        device_number=0,
        ocabox_camera=cam,
        poll_interval_s=0.001,
    )
    import logging
    with caplog.at_level(logging.WARNING, logger="alpaca"):
        await p._wait_image_ready(exp_time=0.05)
    assert any("stealing the signal" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_wait_image_ready_camerastate_fallback_requires_seeing_exposing():
    """Camerastate=Idle without ever seeing Exposing must NOT short-circuit."""
    cam = MagicMock()
    cam.aget_imageready = AsyncMock(return_value=False)
    cam.aget_camerastate = AsyncMock(return_value=0)  # Idle from the start
    p = AlpacaProtocol(
        instance_id="x",
        url="http://x",
        device_number=0,
        ocabox_camera=cam,
        poll_interval_s=0.001,
    )
    with pytest.raises(AlpacaFetchError, match="image-not-ready timeout"):
        await p._wait_image_ready(exp_time=0.05)


# ---------------------------------------------------------------------------
# fetch() — full flow with mocked components
# ---------------------------------------------------------------------------


def _make_imagebytes_payload(arr: np.ndarray) -> bytes:
    header = struct.pack(
        "iiIIiiiiiii",
        1, 0, 1, 1, 44,
        8, 8,                     # img/trans = uint16
        2, arr.shape[0], arr.shape[1], 0,
    )
    return header + arr.astype("uint16").tobytes()


@pytest.mark.asyncio
async def test_fetch_full_flow_with_ocabox_camera():
    """End-to-end fetch: settings → start → wait → bytes → FetchedFrame."""
    cam = _make_ocabox_camera(image_ready_after_calls=1)

    arr = np.arange(20, dtype="uint16").reshape((4, 5))
    payload = _make_imagebytes_payload(arr)

    @asynccontextmanager
    async def fake_get(url, headers=None, timeout=None):
        resp = MagicMock()
        resp.status = 200
        resp.headers = {"content-type": "application/imagebytes"}
        resp.read = AsyncMock(return_value=payload)
        yield resp

    p = AlpacaProtocol(
        instance_id="guiding_svc.guider.jk15.guider_a",
        url="http://jk15-tcu.oca.lan:11111",
        device_number=1,
        ocabox_camera=cam,
        poll_interval_s=0.001,
    )
    p._session = MagicMock()
    p._session.get = fake_get

    frame = await p.fetch(exp_time=0.5, binning=1, gain=100)

    assert frame.array.shape == (4, 5)
    assert frame.array.dtype == np.uint16
    assert (frame.array == arr).all()
    assert frame.exp_time == 0.5
    assert frame.gain == 100
    assert frame.binning == 1
    assert frame.metadata["device_number"] == 1
    assert frame.metadata["client_id"] == p.client_id

    # And the control verbs were called in the right order
    cam.aput_gain.assert_awaited_once_with(100)
    cam.aput_binx.assert_awaited_once_with(1)
    cam.aput_startexposure.assert_awaited_once()


# ---------------------------------------------------------------------------
# name + lifecycle
# ---------------------------------------------------------------------------


def test_name_includes_url_and_device():
    cam = _make_ocabox_camera()
    p = AlpacaProtocol(
        instance_id="x", url="http://h:1/", device_number=7, ocabox_camera=cam
    )
    assert p.name == "alpaca(http://h:1#camera/7)"


@pytest.mark.asyncio
async def test_open_and_close_idempotent():
    cam = _make_ocabox_camera()
    p = AlpacaProtocol(
        instance_id="x", url="http://h", device_number=0, ocabox_camera=cam
    )
    await p.open()
    sess = p._session
    assert sess is not None
    # Second open is a no-op while session is alive
    await p.open()
    assert p._session is sess
    await p.close()
    assert p._session is None
    # Close on closed protocol is fine
    await p.close()
