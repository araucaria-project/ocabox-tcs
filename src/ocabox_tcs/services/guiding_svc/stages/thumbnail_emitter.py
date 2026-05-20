"""ThumbnailEmitter — writes JPEG previews to disk + publishes notifications.

Consumes a tap of AnalysisFrame from the Stacker. For each kept frame
(every Nth, configurable) it normalises 1..99-percentile to 0..255,
resizes to fixed dimensions, writes a JPEG, and publishes a notification
on ``<prefix>.publish.<service>.<instance>.frame.thumbnail.ready`` with
the file path. Binary content is never sent over NATS.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image
from serverish.base import dt_utcnow_array

from ocabox_tcs.services.guiding_svc.stages.base import AnalysisFrame


logger = logging.getLogger(__name__.rsplit(".", maxsplit=1)[-1])


class ThumbnailEmitter:
    """Tap on the Stacker output that writes thumbnails for the UI.

    Args:
        in_queue: Tap queue of AnalysisFrame fed by Stacker.
        output_dir: Root directory for thumbnails. Each frame is written
            to ``<output_dir>/<instance>/<pipeline_id>/<seq>.jpg`` plus
            a stable ``latest.jpg`` symlink.
        instance: Service instance identifier (for path layout).
        pipeline_id: Pipeline identifier (for path layout + payload).
        notification_publisher: serverish publisher for the
            ``frame.thumbnail.ready`` subject. ``None`` = no NATS
            notification (still writes the file).
        size: ``(width, height)`` of the output JPEG.
        every_n: Write every Nth frame (1 = every frame).
        latest_link: Update a ``latest.jpg`` symlink alongside each new file.
        quality: JPEG quality 1..95.
    """

    def __init__(
        self,
        in_queue: asyncio.Queue[AnalysisFrame],
        *,
        output_dir: str | Path,
        instance: str,
        pipeline_id: str,
        notification_publisher: Any | None = None,
        size: tuple[int, int] = (480, 300),
        every_n: int = 1,
        latest_link: bool = True,
        quality: int = 80,
        max_files: int = 200,
    ) -> None:
        self.in_queue = in_queue
        self.output_dir = Path(output_dir) / instance / pipeline_id
        self.instance = instance
        self.pipeline_id = pipeline_id
        self.notification_publisher = notification_publisher
        self.size = size
        self.every_n = max(1, int(every_n))
        self.latest_link = bool(latest_link)
        self.quality = int(quality)
        self.max_files = max(2, int(max_files))

        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._frame_seq = 0
        self._kept_seq = 0

    async def start(self) -> None:
        if self._task is not None:
            return
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._running = True
        self._task = asyncio.create_task(self._run(), name="thumbnail_emitter")
        logger.info(
            "ThumbnailEmitter started: output=%s size=%s every_n=%d",
            self.output_dir, self.size, self.every_n,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        while self._running:
            try:
                frame = await self.in_queue.get()
            except asyncio.CancelledError:
                break

            self._frame_seq += 1
            if self._frame_seq % self.every_n != 0:
                continue

            try:
                path, thumb_dims = await asyncio.to_thread(self._write_thumbnail, frame)
            except Exception as e:  # noqa: BLE001
                logger.exception("thumbnail write failed: %s", e)
                continue

            # ``dimensions`` is the coordinate domain consumers (UI overlays,
            # centroid scatter, reticle SVG viewBox) must use — i.e. the
            # source frame shape, not the rendered JPEG size. When
            # ``thumbnails.size`` is configured smaller than the sensor
            # (operator-chosen JPEG downsample), the two diverge by a
            # constant factor; consumers anchor on the sensor frame and
            # rescale the bitmap visually via the SVG ``image`` element's
            # ``naturalWidth``/``Height``. ``thumbnail_shape`` is exposed
            # for transparency only.
            h, w = frame.array.shape[:2]
            sensor_dims = (int(w), int(h))

            self._kept_seq += 1
            if self.notification_publisher is not None:
                payload = {
                    "path": str(path),
                    "sequence": self._kept_seq,
                    "frame_seq": self._frame_seq,
                    "ts": dt_utcnow_array(),
                    "frame_ts": frame.timestamp,
                    "exp_time_total": frame.exp_time_total,
                    "n_stacked": frame.n_stacked,
                    "dimensions": list(sensor_dims),
                    "thumbnail_shape": list(thumb_dims),
                    "instance": self.instance,
                    "pipeline_id": self.pipeline_id,
                }
                try:
                    await self.notification_publisher.publish(
                        data=payload,
                        meta={"message_type": "default", "sender": self.instance},
                    )
                except Exception as e:  # noqa: BLE001
                    logger.exception("thumbnail notification publish failed: %s", e)

    def _write_thumbnail(self, frame: AnalysisFrame) -> tuple[Path, tuple[int, int]]:
        """Render a JPEG, write to disk, optionally update latest symlink.

        Old files are pruned so the directory stays bounded at roughly
        ``max_files`` (oldest by mtime drop first; the ``latest.jpg``
        symlink is excluded from the count).

        Returns ``(path, (width, height))``.
        """
        img8 = _normalise_to_uint8(frame.array)
        pil = Image.fromarray(img8, mode="L")
        pil.thumbnail(self.size, Image.Resampling.LANCZOS)

        seq_str = f"{self._kept_seq + 1:08d}"
        path = self.output_dir / f"{seq_str}.jpg"
        pil.save(path, format="JPEG", quality=self.quality, optimize=False)

        if self.latest_link:
            link = self.output_dir / "latest.jpg"
            tmp = self.output_dir / "latest.jpg.tmp"
            try:
                if tmp.exists() or tmp.is_symlink():
                    tmp.unlink()
                tmp.symlink_to(path.name)
                os.replace(tmp, link)
            except (OSError, NotImplementedError):
                # Filesystems without symlink support — fall back to a copy.
                pil.save(link, format="JPEG", quality=self.quality)

        self._prune_old(keep_latest=path)
        return path, pil.size

    def _prune_old(self, *, keep_latest: Path) -> None:
        """Drop oldest sequence files when more than ``max_files`` are present.

        The ``latest.jpg`` symlink is preserved unconditionally — if the
        rotation deletes the file it points to, we re-target the symlink
        to the newest remaining file before returning.
        """
        try:
            files = sorted(
                (p for p in self.output_dir.glob("*.jpg") if p.name != "latest.jpg"),
                key=lambda p: p.stat().st_mtime,
            )
        except OSError:
            return
        excess = len(files) - self.max_files
        if excess <= 0:
            return
        for old in files[:excess]:
            try:
                old.unlink()
            except OSError:
                pass
        # Re-target the latest.jpg symlink if it became dangling.
        link = self.output_dir / "latest.jpg"
        if self.latest_link and link.is_symlink():
            try:
                target = link.readlink()
                resolved = (self.output_dir / target).resolve()
                if not resolved.exists():
                    link.unlink()
                    link.symlink_to(keep_latest.name)
            except OSError:
                pass


def _normalise_to_uint8(arr: np.ndarray) -> np.ndarray:
    """Asinh stretch for a JPEG-friendly preview.

    Why asinh and not a simple percentile-clip:
      * Operators look at *shape* of bright PSFs (focus quality, coma,
        trefoil) and at the *fibre-entrance shadow* dip in the centre
        of saturated stars during fibre injection. Both live in the
        bright end of the dynamic range. A linear 1..99 percentile clip
        wipes them out — everything above the 99th-percentile is
        rendered as flat white.
      * Asinh is the astronomy-standard stretch (Lupton et al. 2004,
        used by SDSS / DECaLS image releases) — linear for sky and
        faint sources, logarithmic for bright peaks, smooth transition
        between. Preserves both the noise grain (FWHM checks) and
        intra-PSF structure (fibre shadow, near-saturated halo).

    Algorithm:
      bg = median(arr)
      sig = MAD(arr) * 1.4826   # robust σ
      x = (arr - bg) / sig
      out = asinh(scale * x) / asinh(scale * x_top)
      where x_top = the 99.99-percentile of x — only the few brightest
      pixels of the entire frame saturate to 255, everything else
      keeps detail.

    ``scale`` controls the "soft knee" — bigger = more compression of
    the bright end. 8 is a sane default tested on jk15 BESO with
    10kADU stars + sky 30..200 ADU.

    Falls back to a flat mid-grey when the array is uniform (clouds,
    closed dome, dark frame) so the operator sees *something* rather
    than a 0-or-255 binary.
    """
    a = arr
    if a.ndim != 2:
        a = np.asarray(a).squeeze()
    a = np.where(np.isfinite(a), a, 0).astype(np.float32, copy=False)

    bg = float(np.median(a))
    mad = float(np.median(np.abs(a - bg)))
    sigma = max(mad * 1.4826, 1.0)
    x = (a - bg) / sigma

    # Cap the soft "white" point at the 99.99 percentile — only the
    # brightest handful of pixels saturate to 255, everything below
    # keeps detail.
    x_top = float(np.percentile(x, 99.99))
    if x_top <= 0.5:  # nearly-uniform frame, e.g. dome closed
        return np.full_like(a, 128, dtype=np.uint8)

    scale = 8.0
    stretched = np.arcsinh(scale * x)
    norm = float(np.arcsinh(scale * x_top))
    out = np.clip(stretched / norm, 0.0, 1.0) * 255.0
    return out.astype(np.uint8)
