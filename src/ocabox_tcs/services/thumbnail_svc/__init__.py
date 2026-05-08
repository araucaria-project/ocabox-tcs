"""Static-file HTTP server for guider thumbnails (and any other JPEG/PNG
artefacts a service writes to a shared filesystem).

Architecture: pure data-plane sidecar — operates in parallel with the
guider services. The guider writes JPEGs + emits NATS notifications
with the absolute filesystem path; this service exposes the directory
over HTTP so browsers can fetch the bytes natively. The HTTP base URL
is announced via the standard ``svc.status.>`` discovery stream so UIs
auto-fill it without the operator having to configure two URLs.

Why HTTP and not NATS for the bytes:
    - Browser ``<img>`` decodes JPEGs natively, range requests + cache
      headers + ETag are reduced-to-zero implementation effort.
    - NATS is sized for low-latency small messages; ferrying 2 MB JPEGs
      through a NATS gateway × N clients adds load that has no
      pub/sub benefit (every fetch is point-to-point on demand).
    - We can swap aiohttp for nginx in production without touching the
      guider service or the UI — operator just edits the static-server
      directive. This service is the *Python sidecar* path useful when
      no nginx is around (dev, small deployments).

Why a separate TCS service rather than embedding in the guider:
    - One thumbnail server can serve multiple guider instances sharing
      an NFS volume — single deployment touch-point per observatory.
    - Restart policies + monitoring + tcsctl visibility for free
      (TCS framework gives us all of that).
    - Operator can run thumbnails on a different host than the guider
      (e.g. closer to where the operator sits, away from the camera box).
"""
