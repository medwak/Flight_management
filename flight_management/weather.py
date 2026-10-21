"""Weather: wind triangle calculations and a lightweight METAR decoder."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


@dataclass(frozen=True)
class Wind:
    """Wind *from* ``direction`` (degrees true) at ``speed`` knots."""

    direction: float
    speed: float
    gust: Optional[float] = None

    @classmethod
    def calm(cls) -> "Wind":
        return cls(0.0, 0.0)


def headwind_component(track: float, wind: Wind) -> float:
    """Positive = headwind, negative = tailwind (knots)."""
    return wind.speed * math.cos(math.radians(wind.direction - track))


def crosswind_component(track: float, wind: Wind, use_gust: bool = True) -> float:
    """Absolute crosswind component in knots (gust used when available)."""
    speed = wind.gust if (use_gust and wind.gust) else wind.speed
    return abs(speed * math.sin(math.radians(wind.direction - track)))


def ground_speed(true_airspeed: float, track: float, wind: Wind) -> float:
    """Ground speed (kt) along ``track`` solving the wind triangle."""
    if wind.speed == 0:
        return true_airspeed
    angle = math.radians(wind.direction - track)
    cross = wind.speed * math.sin(angle)
    if abs(cross) >= true_airspeed:
        raise ValueError("Crosswind exceeds true airspeed; track cannot be held")
    along = wind.speed * math.cos(angle)
    return math.sqrt(true_airspeed ** 2 - cross ** 2) - along


class FlightCategory(Enum):
    VFR = "VFR"
    MVFR = "MVFR"
    IFR = "IFR"
    LIFR = "LIFR"


@dataclass
class Metar:
    station: str
    raw: str
    wind: Wind = field(default_factory=Wind.calm)
    visibility_m: float = 10000.0
    ceiling_ft: Optional[int] = None
    temperature_c: Optional[int] = None
    dewpoint_c: Optional[int] = None
    qnh_hpa: Optional[float] = None
    phenomena: list[str] = field(default_factory=list)

    @property
    def flight_category(self) -> FlightCategory:
        ceiling = self.ceiling_ft if self.ceiling_ft is not None else 99999
        vis_sm = self.visibility_m / 1609.34
        if ceiling < 500 or vis_sm < 1:
            return FlightCategory.LIFR
        if ceiling < 1000 or vis_sm < 3:
            return FlightCategory.IFR
        if ceiling <= 3000 or vis_sm <= 5:
            return FlightCategory.MVFR
        return FlightCategory.VFR

    @property
    def hazardous(self) -> bool:
        """Thunderstorms, freezing precipitation, volcanic ash etc."""
        hazards = ("TS", "FZ", "VA", "SS", "DS", "FC", "GR")
        return any(h in p for p in self.phenomena for h in hazards)


_WIND_RE = re.compile(r"^(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?(KT|MPS)$")
_TEMP_RE = re.compile(r"^(M?\d{2})/(M?\d{2})?$")
_CLOUD_RE = re.compile(r"^(FEW|SCT|BKN|OVC|VV)(\d{3})")
_WX_RE = re.compile(r"^(\+|-|VC)?(MI|PR|BC|DR|BL|SH|TS|FZ)?(DZ|RA|SN|SG|IC|PL|GR|GS|UP|BR|FG|FU|VA|DU|SA|HZ|PO|SQ|FC|SS|DS)+$")


def _parse_temp(value: str) -> int:
    return -int(value[1:]) if value.startswith("M") else int(value)


def parse_metar(raw: str) -> Metar:
    """Decode the common groups of an ICAO METAR string.

    >>> parse_metar("EGLL 211250Z 24015G25KT 9999 BKN012 12/09 Q1008").ceiling_ft
    1200
    """
    tokens = raw.strip().split()
    if tokens and tokens[0] in ("METAR", "SPECI"):
        tokens = tokens[1:]
    if not tokens:
        raise ValueError("Empty METAR")
    metar = Metar(station=tokens[0].upper(), raw=raw.strip())
    for tok in tokens[1:]:
        if tok == "CAVOK":
            metar.visibility_m = 10000.0
            continue
        m = _WIND_RE.match(tok)
        if m:
            factor = 1.94384 if m.group(4) == "MPS" else 1.0
            direction = 0.0 if m.group(1) == "VRB" else float(m.group(1))
            gust = float(m.group(3)) * factor if m.group(3) else None
            metar.wind = Wind(direction, float(m.group(2)) * factor, gust)
            continue
        if re.fullmatch(r"\d{4}", tok):
            metar.visibility_m = 10000.0 if tok == "9999" else float(tok)
            continue
        m = re.fullmatch(r"M?(?:(\d+)/(\d+)|(\d+))SM", tok)
        if m:
            miles = int(m.group(1)) / int(m.group(2)) if m.group(1) else float(m.group(3))
            metar.visibility_m = round(miles * 1609.34)
            continue
        m = _CLOUD_RE.match(tok)
        if m:
            if m.group(1) in ("BKN", "OVC", "VV"):
                height = int(m.group(2)) * 100
                if metar.ceiling_ft is None or height < metar.ceiling_ft:
                    metar.ceiling_ft = height
            continue
        m = _TEMP_RE.match(tok)
        if m:
            metar.temperature_c = _parse_temp(m.group(1))
            if m.group(2):
                metar.dewpoint_c = _parse_temp(m.group(2))
            continue
        if re.fullmatch(r"Q\d{4}", tok):
            metar.qnh_hpa = float(tok[1:])
            continue
        if re.fullmatch(r"A\d{4}", tok):
            metar.qnh_hpa = round(int(tok[1:]) / 100 * 33.8639, 1)
            continue
        if _WX_RE.match(tok):
            metar.phenomena.append(tok)
    return metar
