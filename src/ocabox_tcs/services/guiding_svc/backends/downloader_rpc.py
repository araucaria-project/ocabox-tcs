"""DownloaderRPCBackend — fetch frames via NATS RPC to the OFP downloader.

**Status**: skeleton. Implementation deferred:
the OFP downloader's current RPC schema is FITS-pipeline-shaped (`fits_id`,
`file_location`, `header`, `symlinks`), not "give me array now". A new
RPC verb will be coordinated with Mirek when this backend is needed.

Until then: SimBackend handles frame/dummy iterations; DirectFetchBackend
+ AlpacaProtocol becomes the first real implementation per
"""

from __future__ import annotations

from ocabox_tcs.services.guiding_svc.protocols.base import FetchedFrame


class DownloaderRPCBackend:
    """Fetch frames via NATS RPC to oca-fits-proc downloader service.

    Args:
        rpc_subject: NATS subject for the downloader's array-only RPC.
            (Subject schema TBD; coordinate with OFP owner.)
        timeout_s: Per-fetch RPC timeout.
    """

    def __init__(self, rpc_subject: str, timeout_s: float = 10.0) -> None:
        self.rpc_subject = rpc_subject
        self.timeout_s = timeout_s

    @property
    def name(self) -> str:
        return f"downloader_rpc({self.rpc_subject})"

    async def open(self) -> None:
        raise NotImplementedError(
            "DownloaderRPCBackend.open — discover Messenger, register an "
            "MsgRpcRequester for self.rpc_subject. RPC subject schema is "
            "not yet defined (depends on OFP-side endpoint)."
        )

    async def close(self) -> None:
        raise NotImplementedError("DownloaderRPCBackend.close — release responder.")

    async def submit_one(
        self,
        exp_time: float,
        roi: tuple[int, int, int, int] | None = None,
        binning: int | tuple[int, int] = 1,
        gain: int | None = None,
    ) -> FetchedFrame:
        raise NotImplementedError(
            "DownloaderRPCBackend.submit_one — package params into an RPC "
            "request, await response, decode into FetchedFrame. Schema "
            "negotiation with Mirek required first."
        )
