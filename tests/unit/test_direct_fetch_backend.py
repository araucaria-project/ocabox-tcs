"""Tests for DirectFetchBackend (FL1 — A3).

The wrapper is thin: open/close delegate to the protocol; submit_one wraps
fetch with retry-once on AlpacaFetchError.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest
from serverish.base import dt_utcnow_array

from ocabox_tcs.services.guiding_svc._borrowed.ofp.alpaca_http import (
    AlpacaFetchError,
)
from ocabox_tcs.services.guiding_svc.backends import direct_fetch
from ocabox_tcs.services.guiding_svc.backends.direct_fetch import (
    DirectFetchBackend,
)
from ocabox_tcs.services.guiding_svc.protocols.base import FetchedFrame
from tests.helpers.virtual_time import VirtualClock


@pytest.fixture
def clock(monkeypatch) -> VirtualClock:
    """Virtual time for the retry back-off — the delay stays asserted
    (see the retry tests) but costs no wall clock."""
    vclock = VirtualClock()
    vclock.install(monkeypatch, direct_fetch)
    return vclock


def _make_frame(seed: int = 0) -> FetchedFrame:
    return FetchedFrame(
        array=np.full((4, 5), seed, dtype="uint16"),
        exp_time=0.5,
        timestamp=dt_utcnow_array(),
    )


def _make_protocol(fetch_side_effect=None, fetch_return_value=None) -> MagicMock:
    proto = MagicMock()
    proto.name = "alpaca(http://x#camera/1)"
    proto.open = AsyncMock()
    proto.close = AsyncMock()
    proto.fetch = AsyncMock(
        side_effect=fetch_side_effect, return_value=fetch_return_value
    )
    return proto


# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_and_close_delegate_to_protocol():
    proto = _make_protocol(fetch_return_value=_make_frame())
    be = DirectFetchBackend(protocol=proto)
    await be.open()
    proto.open.assert_awaited_once()
    await be.close()
    proto.close.assert_awaited_once()


def test_name_includes_protocol_name():
    proto = _make_protocol(fetch_return_value=_make_frame())
    be = DirectFetchBackend(protocol=proto)
    assert be.name == "direct_fetch(alpaca(http://x#camera/1))"


@pytest.mark.asyncio
async def test_submit_one_passes_params_to_protocol():
    expected = _make_frame(seed=42)
    proto = _make_protocol(fetch_return_value=expected)
    be = DirectFetchBackend(protocol=proto)
    out = await be.submit_one(exp_time=1.0, binning=2, gain=100)
    assert out is expected
    proto.fetch.assert_awaited_once_with(exp_time=1.0, roi=None, binning=2, gain=100)


@pytest.mark.asyncio
async def test_submit_one_retries_once_on_transient_error(clock):
    expected = _make_frame(seed=7)
    proto = _make_protocol(
        fetch_side_effect=[AlpacaFetchError("hiccup"), expected]
    )
    be = DirectFetchBackend(protocol=proto)
    out = await be.submit_one(exp_time=0.1)
    assert out is expected
    assert proto.fetch.await_count == 2
    assert clock.sleeps == [direct_fetch._RETRY_BACKOFF_S]


@pytest.mark.asyncio
async def test_submit_one_bubbles_after_second_failure(clock):
    proto = _make_protocol(
        fetch_side_effect=[AlpacaFetchError("first"), AlpacaFetchError("second")]
    )
    be = DirectFetchBackend(protocol=proto)
    with pytest.raises(AlpacaFetchError, match="second"):
        await be.submit_one(exp_time=0.1)
    assert proto.fetch.await_count == 2
    assert clock.sleeps == [direct_fetch._RETRY_BACKOFF_S]


@pytest.mark.asyncio
async def test_retry_can_be_disabled():
    proto = _make_protocol(
        fetch_side_effect=[AlpacaFetchError("once"), _make_frame()]
    )
    be = DirectFetchBackend(protocol=proto, retry_once=False)
    with pytest.raises(AlpacaFetchError, match="once"):
        await be.submit_one(exp_time=0.1)
    assert proto.fetch.await_count == 1


@pytest.mark.asyncio
async def test_non_alpaca_errors_pass_through():
    proto = _make_protocol(fetch_side_effect=ValueError("not transient"))
    be = DirectFetchBackend(protocol=proto)
    with pytest.raises(ValueError, match="not transient"):
        await be.submit_one(exp_time=0.1)
    # No retry on non-AlpacaFetchError types
    assert proto.fetch.await_count == 1
