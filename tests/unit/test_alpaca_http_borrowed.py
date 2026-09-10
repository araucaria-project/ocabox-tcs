"""Tests for the borrowed-from-ofp Alpaca HTTP helper.

Covers:
- decode_imagebytes round-trip on a few dtype combinations.
- decode_imagebytes warns on Rank != 2.
- fetch_imagearray binary path (mocked aiohttp response).
- fetch_imagearray JSON path.
- fetch_imagearray HTTP error → AlpacaFetchError.
"""

from __future__ import annotations

import logging
import struct
from contextlib import asynccontextmanager
from unittest.mock import MagicMock

import numpy as np
import pytest

from ocabox_tcs.services.guiding_svc._borrowed.ofp.alpaca_http import (
    AlpacaFetchError,
    decode_imagebytes,
    fetch_imagearray,
)


def _make_payload(arr: np.ndarray, *, transmission_dtype: str | None = None) -> bytes:
    """Build a synthetic Alpaca imagebytes payload around ``arr``."""
    img_to_idx = {
        "int16": 1, "int32": 2, "float64": 3, "float32": 4,
        "uint64": 5, "uint8": 6, "int64": 7, "uint16": 8, "uint32": 9,
    }
    img_idx = img_to_idx[arr.dtype.name]
    if transmission_dtype is None:
        transmission_dtype = arr.dtype.name
    trans_idx = img_to_idx[transmission_dtype]
    header = struct.pack(
        "iiIIiiiiiii",
        1,        # MetadataVersion
        0,        # ErrorNumber
        1,        # ClientTransactionID
        1,        # ServerTransactionID
        44,       # DataStart (header is 11 ints = 44 bytes)
        img_idx,
        trans_idx,
        2,                       # Rank
        arr.shape[0],            # Dimension1
        arr.shape[1],            # Dimension2
        0,                       # Dimension3
    )
    return header + arr.astype(transmission_dtype).tobytes()


# ---------------------------------------------------------------------------
# decode_imagebytes
# ---------------------------------------------------------------------------


def test_decode_uint16_round_trip():
    arr = (np.random.default_rng(0).integers(0, 60_000, size=(8, 5))).astype("uint16")
    out = decode_imagebytes(_make_payload(arr))
    assert out["Value"].shape == (8, 5)
    assert out["Value"].dtype == np.uint16
    assert (out["Value"] == arr).all()


def test_decode_uint32_image_transmitted_as_uint16():
    arr = (np.arange(12, dtype="uint32") * 1000).reshape((4, 3))
    out = decode_imagebytes(_make_payload(arr, transmission_dtype="uint16"))
    assert out["Value"].dtype == np.uint32
    assert (out["Value"] == arr).all()


def test_decode_int32_round_trip():
    arr = np.arange(-6, 6, dtype="int32").reshape((4, 3))
    out = decode_imagebytes(_make_payload(arr))
    assert out["Value"].dtype == np.int32
    assert (out["Value"] == arr).all()


def test_decode_warns_on_rank_other_than_2(caplog):
    arr = np.zeros((4, 3), dtype="uint16")
    payload = bytearray(_make_payload(arr))
    # Patch Rank=3 in-place (offset 28 = 7 ints * 4)
    payload[28:32] = struct.pack("i", 3)
    with caplog.at_level(logging.WARNING, logger="alpaca_http"):
        decode_imagebytes(bytes(payload))
    assert any("Rank=3" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# fetch_imagearray (mocked aiohttp)
# ---------------------------------------------------------------------------


class _FakeResp:
    """Minimal aiohttp.ClientResponse stub for fetch_imagearray."""

    def __init__(self, *, status: int, content_type: str, body: bytes | str):
        self.status = status
        self.headers = {"content-type": content_type}
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def read(self) -> bytes:
        assert isinstance(self._body, bytes)
        return self._body

    async def text(self) -> str:
        return self._body if isinstance(self._body, str) else self._body.decode()


def _session_returning(resp: _FakeResp) -> MagicMock:
    session = MagicMock()
    session.get = MagicMock(return_value=resp)
    return session


@pytest.mark.asyncio
async def test_fetch_imagearray_binary_path():
    arr = np.arange(20, dtype="uint16").reshape((4, 5))
    payload = _make_payload(arr)
    session = _session_returning(
        _FakeResp(status=200, content_type="application/imagebytes", body=payload)
    )
    out = await fetch_imagearray(session, "http://x/imagearray")
    assert isinstance(out, np.ndarray)
    assert out.shape == (4, 5)
    assert (out == arr).all()


@pytest.mark.asyncio
async def test_fetch_imagearray_json_path():
    body = '{"Value": [[1, 2, 3], [4, 5, 6]], "ErrorNumber": 0}'
    session = _session_returning(
        _FakeResp(status=200, content_type="application/json", body=body)
    )
    out = await fetch_imagearray(session, "http://x/imagearray")
    assert out.shape == (2, 3)
    assert (out == np.array([[1, 2, 3], [4, 5, 6]])).all()


@pytest.mark.asyncio
async def test_fetch_imagearray_http_error_raises():
    session = _session_returning(
        _FakeResp(status=500, content_type="text/plain", body=b"oops")
    )
    with pytest.raises(AlpacaFetchError, match="HTTP 500"):
        await fetch_imagearray(session, "http://x/imagearray")


@pytest.mark.asyncio
async def test_fetch_imagearray_json_missing_value_raises():
    session = _session_returning(
        _FakeResp(status=200, content_type="application/json", body='{"ErrorNumber": 0}')
    )
    with pytest.raises(AlpacaFetchError, match="no 'Value'"):
        await fetch_imagearray(session, "http://x/imagearray")


@pytest.mark.asyncio
async def test_fetch_imagearray_prefer_binary_false_sets_json_only_accept(monkeypatch):
    """When prefer_binary=False, Accept header omits imagebytes."""
    captured: dict = {}

    @asynccontextmanager
    async def fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        yield _FakeResp(status=200, content_type="application/json", body='{"Value": [[1]]}')

    session = MagicMock()
    session.get = fake_get
    await fetch_imagearray(session, "http://x/imagearray", prefer_binary=False)
    assert captured["headers"] == {"Accept": "application/json"}
