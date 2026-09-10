"""Collector backends — where do frames come from?

"""

from ocabox_tcs.services.guiding_svc.backends.base import CollectorBackend
from ocabox_tcs.services.guiding_svc.backends.direct_fetch import DirectFetchBackend
from ocabox_tcs.services.guiding_svc.backends.downloader_rpc import DownloaderRPCBackend
from ocabox_tcs.services.guiding_svc.backends.fits_watch import FitsWatchBackend
from ocabox_tcs.services.guiding_svc.backends.sim import SimBackend


__all__ = [
    "CollectorBackend",
    "DirectFetchBackend",
    "DownloaderRPCBackend",
    "FitsWatchBackend",
    "SimBackend",
]
