"""Camera-array protocols — wire formats for fetching image arrays.

Pluggable below `DirectFetchBackend`. Each protocol talks to one type of
camera over its native wire format.
"""

from ocabox_tcs.services.guiding_svc.protocols.alpaca import AlpacaProtocol
from ocabox_tcs.services.guiding_svc.protocols.base import CameraArrayProtocol, FetchedFrame
from ocabox_tcs.services.guiding_svc.protocols.file_sim import FileSimProtocol
from ocabox_tcs.services.guiding_svc.protocols.iris import IrisProtocol


__all__ = [
    "AlpacaProtocol",
    "CameraArrayProtocol",
    "FetchedFrame",
    "FileSimProtocol",
    "IrisProtocol",
]
