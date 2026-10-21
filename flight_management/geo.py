"""Geodesy helpers: coordinates, great-circle distance, bearings.

All distances are in nautical miles (NM) and angles in degrees.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

EARTH_RADIUS_NM = 3440.065


@dataclass(frozen=True)
class Coordinate:
    latitude: float
    longitude: float

    def __post_init__(self) -> None:
        if not -90.0 <= self.latitude <= 90.0:
            raise ValueError(f"Latitude out of range: {self.latitude}")
        if not -180.0 <= self.longitude <= 180.0:
            raise ValueError(f"Longitude out of range: {self.longitude}")

    def __str__(self) -> str:
        ns = "N" if self.latitude >= 0 else "S"
        ew = "E" if self.longitude >= 0 else "W"
        return f"{abs(self.latitude):.4f}{ns} {abs(self.longitude):.4f}{ew}"


def great_circle_distance(a: Coordinate, b: Coordinate) -> float:
    """Haversine distance between two coordinates in nautical miles."""
    lat1, lon1 = math.radians(a.latitude), math.radians(a.longitude)
    lat2, lon2 = math.radians(b.latitude), math.radians(b.longitude)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_NM * math.asin(min(1.0, math.sqrt(h)))


def initial_bearing(a: Coordinate, b: Coordinate) -> float:
    """True initial course from ``a`` to ``b`` in degrees [0, 360)."""
    lat1, lat2 = math.radians(a.latitude), math.radians(b.latitude)
    dlon = math.radians(b.longitude - a.longitude)
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def intermediate_point(a: Coordinate, b: Coordinate, fraction: float) -> Coordinate:
    """Point at ``fraction`` (0..1) of the way along the great circle from a to b."""
    if not 0.0 <= fraction <= 1.0:
        raise ValueError("fraction must be between 0 and 1")
    lat1, lon1 = math.radians(a.latitude), math.radians(a.longitude)
    lat2, lon2 = math.radians(b.latitude), math.radians(b.longitude)
    delta = great_circle_distance(a, b) / EARTH_RADIUS_NM
    if delta == 0:
        return a
    sin_delta = math.sin(delta)
    fa = math.sin((1 - fraction) * delta) / sin_delta
    fb = math.sin(fraction * delta) / sin_delta
    x = fa * math.cos(lat1) * math.cos(lon1) + fb * math.cos(lat2) * math.cos(lon2)
    y = fa * math.cos(lat1) * math.sin(lon1) + fb * math.cos(lat2) * math.sin(lon2)
    z = fa * math.sin(lat1) + fb * math.sin(lat2)
    lat = math.atan2(z, math.sqrt(x * x + y * y))
    lon = math.atan2(y, x)
    return Coordinate(round(math.degrees(lat), 6), round(math.degrees(lon), 6))
