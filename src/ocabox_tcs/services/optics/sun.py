"""Sun altitude at a telescope — the sky's light class (science / twilight / flat / day) follows it."""
from __future__ import annotations

from dataclasses import dataclass

import astropy.units as u
from astropy.coordinates import AltAz, EarthLocation, get_sun
from astropy.time import Time


@dataclass(frozen=True)
class SunAltitude:
    """Callable: the sun's altitude now, in degrees, at ``location``; ``None`` without a location
    (the sky's class is then undefined, which validates nothing)."""

    location: EarthLocation | None

    @classmethod
    def from_config(cls, observatory: dict) -> SunAltitude:
        """From the ``lon``/``lat``/``elev`` of a telescope's ``observatory`` configuration."""
        try:
            return cls(EarthLocation(lon=observatory['lon'] * u.deg, lat=observatory['lat'] * u.deg,
                                     height=observatory.get('elev', 0) * u.m))
        except (KeyError, TypeError):
            return cls(None)

    def __call__(self) -> float | None:
        if self.location is None:
            return None
        now = Time.now()
        return float(get_sun(now).transform_to(AltAz(obstime=now, location=self.location)).alt.deg)
