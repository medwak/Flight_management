"""Airport and runway data."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from .geo import Coordinate, great_circle_distance


@dataclass(frozen=True)
class Runway:
    designator: str  # e.g. "09L/27R"
    length_m: float

    @property
    def headings(self) -> tuple[float, float]:
        """Magnetic headings (approximate, from the designator) of both ends."""
        ends = self.designator.split("/")
        nums = [int("".join(ch for ch in end if ch.isdigit())) * 10.0 for end in ends]
        if len(nums) == 1:
            nums.append((nums[0] + 180.0) % 360.0)
        return nums[0] % 360.0, nums[1] % 360.0


@dataclass
class Airport:
    icao: str
    iata: str
    name: str
    city: str
    country: str
    location: Coordinate
    elevation_ft: float
    runways: list[Runway] = field(default_factory=list)
    timezone: str = "UTC"

    @property
    def longest_runway_m(self) -> float:
        return max((r.length_m for r in self.runways), default=0.0)

    def best_runway_heading(self, wind_direction: float) -> float:
        """Runway end heading most closely aligned into the wind."""
        best = None
        for runway in self.runways:
            for heading in runway.headings:
                diff = abs((wind_direction - heading + 180) % 360 - 180)
                if best is None or diff < best[0]:
                    best = (diff, heading)
        if best is None:
            raise ValueError(f"{self.icao} has no runways")
        return best[1]

    def distance_to(self, other: "Airport") -> float:
        return great_circle_distance(self.location, other.location)

    def __str__(self) -> str:
        return f"{self.icao}/{self.iata} {self.name}"


class AirportDatabase:
    def __init__(self, airports: Iterable[Airport] = ()):
        self._airports: dict[str, Airport] = {}
        for airport in airports:
            self.add(airport)

    def add(self, airport: Airport) -> None:
        self._airports[airport.icao.upper()] = airport

    def get(self, code: str) -> Airport:
        code = code.upper()
        if code in self._airports:
            return self._airports[code]
        for airport in self._airports.values():
            if airport.iata == code:
                return airport
        raise KeyError(f"Unknown airport: {code}")

    def find(self, code: str) -> Optional[Airport]:
        try:
            return self.get(code)
        except KeyError:
            return None

    def nearest(self, point: Coordinate, min_runway_m: float = 0.0,
                exclude: Iterable[str] = ()) -> list[tuple[Airport, float]]:
        """Airports sorted by distance from ``point`` (used for diversions)."""
        excluded = {e.upper() for e in exclude}
        candidates = [
            (a, great_circle_distance(point, a.location))
            for a in self._airports.values()
            if a.longest_runway_m >= min_runway_m and a.icao not in excluded
        ]
        return sorted(candidates, key=lambda item: item[1])

    def __contains__(self, code: str) -> bool:
        return self.find(code) is not None

    def __iter__(self):
        return iter(self._airports.values())

    def __len__(self) -> int:
        return len(self._airports)


def _ap(icao, iata, name, city, country, lat, lon, elev, runways, tz) -> Airport:
    return Airport(icao, iata, name, city, country, Coordinate(lat, lon), elev,
                   [Runway(d, l) for d, l in runways], tz)


def default_airports() -> AirportDatabase:
    """A small built-in database of major passenger, cargo and business aviation airports."""
    return AirportDatabase([
        _ap("KJFK", "JFK", "John F. Kennedy Intl", "New York", "US", 40.6413, -73.7781, 13,
            [("13R/31L", 4442), ("04L/22R", 3682), ("04R/22L", 2560)], "America/New_York"),
        _ap("KTEB", "TEB", "Teterboro", "Teterboro", "US", 40.8501, -74.0608, 9,
            [("06/24", 2134), ("01/19", 2135)], "America/New_York"),
        _ap("KLAX", "LAX", "Los Angeles Intl", "Los Angeles", "US", 33.9416, -118.4085, 125,
            [("07L/25R", 3685), ("06R/24L", 3135)], "America/Los_Angeles"),
        _ap("KORD", "ORD", "O'Hare Intl", "Chicago", "US", 41.9742, -87.9073, 672,
            [("10L/28R", 3962), ("09R/27L", 3427)], "America/Chicago"),
        _ap("KMEM", "MEM", "Memphis Intl", "Memphis", "US", 35.0424, -89.9767, 341,
            [("18C/36C", 3389), ("18L/36R", 2743)], "America/Chicago"),
        _ap("KVNY", "VNY", "Van Nuys", "Los Angeles", "US", 34.2098, -118.4899, 802,
            [("16R/34L", 2438)], "America/Los_Angeles"),
        _ap("EGLL", "LHR", "Heathrow", "London", "GB", 51.4700, -0.4543, 83,
            [("09L/27R", 3902), ("09R/27L", 3660)], "Europe/London"),
        _ap("EGGW", "LTN", "Luton", "London", "GB", 51.8747, -0.3683, 526,
            [("08/26", 2162)], "Europe/London"),
        _ap("LFPG", "CDG", "Charles de Gaulle", "Paris", "FR", 49.0097, 2.5479, 392,
            [("09L/27R", 4215), ("08R/26L", 4200)], "Europe/Paris"),
        _ap("EDDF", "FRA", "Frankfurt", "Frankfurt", "DE", 50.0379, 8.5622, 364,
            [("07C/25C", 4000), ("07R/25L", 4000), ("18", 4000)], "Europe/Berlin"),
        _ap("EHAM", "AMS", "Schiphol", "Amsterdam", "NL", 52.3105, 4.7683, -11,
            [("18R/36L", 3800), ("06/24", 3500)], "Europe/Amsterdam"),
        _ap("OMDB", "DXB", "Dubai Intl", "Dubai", "AE", 25.2532, 55.3657, 62,
            [("12L/30R", 4351), ("12R/30L", 4447)], "Asia/Dubai"),
        _ap("LIRF", "FCO", "Fiumicino", "Rome", "IT", 41.8003, 12.2389, 13,
            [("16R/34L", 3900), ("07/25", 3309)], "Europe/Rome"),
        _ap("HECA", "CAI", "Cairo Intl", "Cairo", "EG", 30.1219, 31.4056, 382,
            [("05C/23C", 4000), ("05L/23R", 3999)], "Africa/Cairo"),
        _ap("HSSK", "KRT", "Khartoum Intl", "Khartoum", "SD", 15.5895, 32.5532, 1265,
            [("18/36", 2980)], "Africa/Khartoum"),
        _ap("HKJK", "NBO", "Jomo Kenyatta Intl", "Nairobi", "KE", -1.3192, 36.9278, 5330,
            [("06/24", 4117)], "Africa/Nairobi"),
        _ap("HKMO", "MBA", "Moi Intl", "Mombasa", "KE", -4.0348, 39.5942, 200,
            [("03/21", 3350)], "Africa/Nairobi"),
        _ap("HKEL", "EDL", "Eldoret Intl", "Eldoret", "KE", 0.4045, 35.2389, 6941,
            [("08/26", 3475)], "Africa/Nairobi"),
        _ap("HAAB", "ADD", "Bole Intl", "Addis Ababa", "ET", 8.9779, 38.7993, 7625,
            [("07R/25L", 3800), ("07L/25R", 4600)], "Africa/Addis_Ababa"),
        _ap("FAOR", "JNB", "O. R. Tambo Intl", "Johannesburg", "ZA", -26.1392, 28.2460, 5558,
            [("03L/21R", 4418), ("03R/21L", 3400)], "Africa/Johannesburg"),
        _ap("VHHH", "HKG", "Hong Kong Intl", "Hong Kong", "HK", 22.3080, 113.9185, 28,
            [("07R/25L", 3800), ("07L/25R", 3800)], "Asia/Hong_Kong"),
        _ap("WSSS", "SIN", "Changi", "Singapore", "SG", 1.3644, 103.9915, 22,
            [("02L/20R", 4000), ("02C/20C", 4000)], "Asia/Singapore"),
    ])
