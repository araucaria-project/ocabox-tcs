"""Alpaca ``imagearray`` HTTP fetch with binary fast-path.

Source: ``oca-fits-proc/fits_proc/http_conn.py``.

Provides ``decode_imagebytes`` (binary ``imagebytes`` payload decoder)
and ``fetch_imagearray`` (GET with content-negotiation, returns ndarray
either via the binary or JSON branch).
"""

from __future__ import annotations

import json
import logging
import struct
from typing import Any

import aiohttp
import numpy as np


logger = logging.getLogger(__name__.rsplit(".", maxsplit=1)[-1])


# Alpaca ``ImageArrayElementTypes`` — index → numpy dtype name (None = invalid).
_IMAGE_ELEMENT_TYPES: dict[int, str | None] = {
    0: None,
    1: "int16",
    2: "int32",
    3: "float64",
    4: "float32",
    5: "uint64",
    6: "uint8",
    7: "int64",
    8: "uint16",
    9: "uint32",
}


# Alpaca imagebytes header: 11 little-endian 32-bit integers.
_HEADER_KEYS = (
    "MetadataVersion",
    "ErrorNumber",
    "ClientTransactionID",
    "ServerTransactionID",
    "DataStart",
    "ImageElementType",
    "TransmissionElementType",
    "Rank",
    "Dimension1",
    "Dimension2",
    "Dimension3",
)
_HEADER_FORMAT = "iiIIiiiiiii"
_HEADER_SIZE = struct.calcsize(_HEADER_FORMAT)


def decode_imagebytes(data: bytes) -> dict[str, Any]:
    """Decode an Alpaca ``imagebytes`` payload into a dict with ``Value`` set
    to a 2-D numpy array.

    Returned keys mirror ofp: header fields plus ``Value`` (ndarray).
    Caller does not need to know the wire format — just access ``["Value"]``.
    """
    unpacked = struct.unpack(_HEADER_FORMAT, data[:_HEADER_SIZE])
    resp: dict[str, Any] = dict(zip(_HEADER_KEYS, unpacked, strict=True))

    idtype = _IMAGE_ELEMENT_TYPES[resp["ImageElementType"]]
    tdtype = _IMAGE_ELEMENT_TYPES[resp["TransmissionElementType"]]
    array_bytes = data[resp["DataStart"]:]

    if resp["Rank"] != 2:
        logger.warning("Alpaca imagebytes Rank=%d, expected 2", resp["Rank"])

    # Transmission dtype may narrow the on-wire representation; widen back to
    # the image dtype so e.g. uint16 transmitted as int16 doesn't leave high
    # values as negatives.
    flat = np.frombuffer(array_bytes, dtype=tdtype)
    if idtype is not None and idtype != tdtype:
        flat = flat.astype(idtype)

    # Alpaca binary is laid out Dim1-major to match the JSON branch's
    # nested-list shape (jk15-tcu confirmed).
    arr = flat.reshape((resp["Dimension1"], resp["Dimension2"]))
    resp["Value"] = arr
    logger.debug(
        "decode_imagebytes: shape=%s dtype=%s min=%d max=%d",
        arr.shape, arr.dtype, int(arr.min()), int(arr.max()),
    )
    return resp


async def fetch_imagearray(
    session: aiohttp.ClientSession,
    url: str,
    *,
    prefer_binary: bool = True,
    request_timeout: float = 30.0,
) -> np.ndarray:
    """Fetch an Alpaca ``imagearray`` and return the 2-D numpy array.

    Uses content-negotiation when ``prefer_binary`` is True — the server
    chooses ``application/imagebytes`` (binary) when available, falling back
    to ``application/json``. Either way the caller gets a numpy array with
    the camera's native dtype.

    Args:
        session: Caller-managed aiohttp session. We don't open/close —
            sessions are expensive and live with the protocol.
        url: Full Alpaca endpoint, e.g.
            ``http://jk15-tcu.oca.lan:11111/api/v1/camera/1/imagearray?ClientID=...&ClientTransactionID=...``.
        prefer_binary: Send the dual-Accept header. Set False to force
            JSON (debugging, or servers that mishandle the header).
        request_timeout: Per-request total deadline in seconds.

    Raises:
        AlpacaFetchError: HTTP status >=300, or response missing ``Value``.
    """
    headers = (
        {"Accept": "application/json, application/imagebytes"}
        if prefer_binary
        else {"Accept": "application/json"}
    )

    timeout = aiohttp.ClientTimeout(total=request_timeout)
    async with session.get(url, headers=headers, timeout=timeout) as resp:
        content_type = resp.headers.get("content-type", "") or ""
        if resp.status >= 300:
            raise AlpacaFetchError(
                f"HTTP {resp.status} from Alpaca {url}: {await resp.text()!r}"
            )
        is_json = "application/json" in content_type
        body: bytes | str
        if is_json:
            body = await resp.text()
        else:
            body = await resp.read()

    if is_json:
        decoded = json.loads(body)  # type: ignore[arg-type]
        try:
            value = decoded["Value"]
        except (KeyError, TypeError) as e:
            raise AlpacaFetchError(f"JSON response has no 'Value': {e}") from e
        return np.asarray(value)

    # Binary fast path.
    decoded = decode_imagebytes(body)  # type: ignore[arg-type]
    try:
        return decoded["Value"]
    except KeyError as e:
        raise AlpacaFetchError(f"Binary response has no 'Value': {e}") from e


class AlpacaFetchError(RuntimeError):
    """Raised by :func:`fetch_imagearray` on HTTP error or malformed payload."""
