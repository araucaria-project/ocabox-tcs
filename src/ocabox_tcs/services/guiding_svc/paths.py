"""Output path layout for guider artefacts.

Application-specific replacement for OFP's `Folders` (which is tied to
Dynaconf and OFP's settings.toml schema). Reads paths from the
TCS-style YAML config.

"""

from __future__ import annotations

import os
from pathlib import Path


class GuiderPaths:
    """Compute output paths for guider artefacts (FITS, thumbnails,
    calibration masters).

    Initialised from a config block of the form:
        guider:
          output_root: /mnt/data/guider
          layout: "{tel_id}/{date}/{cam_id}/{kind}"
    `kind` ∈ {raw, stacked, thumb, dark, bias, flat}.
    """

    DEFAULT_LAYOUT = "{tel_id}/{date}/{cam_id}/{kind}"

    def __init__(
        self,
        output_root: str,
        layout: str = DEFAULT_LAYOUT,
        tel_id: str = "unknown",
    ) -> None:
        self.output_root = Path(output_root).expanduser()
        self.layout = layout
        self.tel_id = tel_id

    def for_kind(self, cam_id: str, kind: str, date: str | None = None) -> Path:
        """Return the directory for a specific output kind."""
        from datetime import datetime, timezone

        date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rel = self.layout.format(
            tel_id=self.tel_id,
            date=date,
            cam_id=cam_id,
            kind=kind,
        )
        return self.output_root / rel

    def ensure(self, path: Path) -> Path:
        """`mkdir -p` and return the path."""
        os.makedirs(path, exist_ok=True)
        return path
