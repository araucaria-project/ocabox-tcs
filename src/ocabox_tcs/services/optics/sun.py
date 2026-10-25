"""The sun at a telescope — the sky's light class (science / twilight / flat / day) follows its
altitude, and which class comes next follows its direction."""
from __future__ import annotations

from dataclasses import dataclass

import astropy.units as u
from astropy.coordinates import AltAz, EarthLocation, get_sun
from astropy.time import Time

#: how far ahead the direction is judged; far beyond the sun's noise, well inside one twilight
_AHEAD = 10 * u.min


@dataclass(frozen=True)
class Sun:
    """The sun now: its altitude in degrees and whether it climbs (the morning)."""

    alt_deg: float
    rising: bool


@dataclass(frozen=True)
class SunAltitude:
    """Callable: the :class:`Sun` now at ``location``; ``None`` without a location (the sky's class is
    then undefined, which validates nothing)."""

    location: EarthLocation | None

    @classmethod
    def from_config(cls, observatory: dict) -> SunAltitude:
        """From the ``lon``/``lat``/``elev`` of a telescope's ``observatory`` configuration."""
        try:
            return cls(EarthLocation(lon=observatory['lon'] * u.deg, lat=observatory['lat'] * u.deg,
                                     height=observatory.get('elev', 0) * u.m))
        except (KeyError, TypeError):
            return cls(None)

    def __call__(self) -> Sun | None:
        if self.location is None:
            return None
        now = Time.now()
        times = Time([now, now + _AHEAD])
        alt = get_sun(times).transform_to(AltAz(obstime=times, location=self.location)).alt.deg
        return Sun(float(alt[0]), bool(alt[1] > alt[0]))
