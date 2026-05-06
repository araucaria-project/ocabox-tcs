"""Persisted state and environment fingerprinting.

A trained adapter's parameters (`θ`, sufficient statistics, kernel
hyperparameters) should survive process restarts. But raw persistence is
not enough — when the *operating environment* has materially changed
since the snapshot was written, replaying the old parameters can be
worse than starting fresh.

`EnvironmentFingerprint` captures a coarse signature of the environment.
On load, the application compares the persisted fingerprint to the
current one; large delta → mark not-calibrated and force recalibration.

`PersistedState` is the metadata wrapper around the adapter's own
serialised payload.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Environment fingerprint
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EnvironmentFingerprint:
    """A coarse snapshot of the operating environment at the time `θ` was
    trained.

    All fields are optional — applications fill in what they have. The
    `compare` method returns a dict of per-field deltas; the application
    decides whether the deltas are "small enough" to trust the persisted
    state.

    Examples:
        Telescope guider: pose centroid (Alt, Az, Rot), ambient temperature,
        instrument config hash.
        Mobile robot: wheel-base config hash, gear ratio version.
        Manufacturing: tool serial number, last maintenance date.
    """

    pose_centroid: tuple[float, ...] | None = None
    """Mean of recent operating points in some natural coordinate."""

    temperature_range: tuple[float, float] | None = None
    """(min, max) temperature seen during training."""

    config_hash: str | None = None
    """Hash of relevant config items (instrument geometry, etc.)."""

    n_samples: int = 0
    """Number of empirical observations used to train this state."""

    last_update_ts: float = 0.0
    """UNIX timestamp of last parameter update."""

    extras: dict[str, Any] = field(default_factory=dict)
    """Application-specific extras."""

    def compare(self, other: EnvironmentFingerprint) -> dict[str, Any]:
        """Compute per-field deltas vs `other` (typically: now). Returns a
        dict suitable for logging / decision-making.

        Application decides what counts as "too far". This method is
        intentionally non-judgemental.
        """
        deltas: dict[str, Any] = {}

        if self.pose_centroid is not None and other.pose_centroid is not None:
            if len(self.pose_centroid) == len(other.pose_centroid):
                deltas["pose_centroid_delta"] = tuple(
                    a - b for a, b in zip(self.pose_centroid, other.pose_centroid, strict=False)
                )

        if self.temperature_range is not None and other.temperature_range is not None:
            mid_a = (self.temperature_range[0] + self.temperature_range[1]) / 2
            mid_b = (other.temperature_range[0] + other.temperature_range[1]) / 2
            deltas["temperature_midpoint_delta"] = mid_b - mid_a

        if self.config_hash is not None and other.config_hash is not None:
            deltas["config_hash_changed"] = self.config_hash != other.config_hash

        deltas["n_samples_delta"] = other.n_samples - self.n_samples
        deltas["age_seconds"] = other.last_update_ts - self.last_update_ts

        return deltas


# ---------------------------------------------------------------------------
# Persisted state container
# ---------------------------------------------------------------------------


@dataclass
class PersistedState:
    """Metadata wrapper around an adapter's serialised payload.

    `payload` is the raw `bytes` returned by `AdapterX.serialise()`.
    Loading: read this container, check fingerprint, hand `payload` to
    the appropriate adapter's `load()` factory.

    Concrete byte-format (e.g. JSON wrapper around base64-encoded
    payload, or msgpack, or pickle) is **not chosen here** — applications
    pick what fits their persistence venue (ocadb / config-fs / ...).
    Use `to_dict` / `from_dict` to interface with whatever serialiser
    you prefer.
    """

    adapter_kind: str
    """Class name of the adapter that produced `payload` (for sanity
    check on load)."""

    payload: bytes
    """Raw adapter-defined serialisation."""

    fingerprint: EnvironmentFingerprint
    """Operating environment at training time."""

    schema_version: int = 1
    """Bump when the persisted format changes incompatibly."""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # bytes don't survive JSON; caller is expected to re-encode if
        # using JSON. msgpack / pickle handle bytes natively.
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PersistedState:
        fp_data = data["fingerprint"]
        fingerprint = EnvironmentFingerprint(
            pose_centroid=(
                tuple(fp_data["pose_centroid"])
                if fp_data.get("pose_centroid") is not None
                else None
            ),
            temperature_range=(
                tuple(fp_data["temperature_range"])
                if fp_data.get("temperature_range") is not None
                else None
            ),
            config_hash=fp_data.get("config_hash"),
            n_samples=fp_data.get("n_samples", 0),
            last_update_ts=fp_data.get("last_update_ts", 0.0),
            extras=fp_data.get("extras", {}),
        )
        return cls(
            adapter_kind=data["adapter_kind"],
            payload=data["payload"],
            fingerprint=fingerprint,
            schema_version=data.get("schema_version", 1),
        )
