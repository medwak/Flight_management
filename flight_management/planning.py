"""Flight planning: routes, cruise level, fuel policy, weight & balance and
take-off / landing performance.

The performance model is deliberately simple (a three-segment climb / cruise /
descent profile with a weight-sensitive fuel flow) but follows the structure of
a real operational flight plan (OFP):

    block fuel = taxi + trip + contingency + alternate + final reserve + extra
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Sequence

from .aircraft import AircraftCategory, AircraftType
from .airports import Airport
from .geo import Coordinate, great_circle_distance, initial_bearing, intermediate_point
from .weather import Wind, crosswind_component, ground_speed, headwind_component


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Waypoint:
    ident: str
    location: Coordinate


@dataclass(frozen=True)
class Leg:
    start: Waypoint
    end: Waypoint

    @property
    def distance_nm(self) -> float:
        return great_circle_distance(self.start.location, self.end.location)

    @property
    def track(self) -> float:
        return initial_bearing(self.start.location, self.end.location)


@dataclass
class Route:
    waypoints: list[Waypoint]

    @classmethod
    def direct(cls, origin: Airport, destination: Airport,
               via: Sequence[Waypoint] = (), spacing_nm: float = 400.0) -> "Route":
        """Great-circle route, optionally via named waypoints.

        Long legs are split into generated waypoints roughly every ``spacing_nm``
        so that progress can be tracked along the curved great circle.
        """
        anchors = [Waypoint(origin.icao, origin.location), *via,
                   Waypoint(destination.icao, destination.location)]
        points = [anchors[0]]
        counter = 1
        for a, b in zip(anchors, anchors[1:]):
            dist = great_circle_distance(a.location, b.location)
            segments = max(1, math.ceil(dist / spacing_nm))
            for i in range(1, segments):
                points.append(Waypoint(f"WP{counter:03d}", intermediate_point(a.location, b.location, i / segments)))
                counter += 1
            points.append(b)
        return cls(points)

    @property
    def legs(self) -> list[Leg]:
        return [Leg(a, b) for a, b in zip(self.waypoints, self.waypoints[1:])]

    @property
    def distance_nm(self) -> float:
        return sum(leg.distance_nm for leg in self.legs)

    @property
    def initial_track(self) -> float:
        return self.legs[0].track if self.legs else 0.0

    def position_at(self, distance_flown_nm: float) -> tuple[Coordinate, float]:
        """Position and track after flying ``distance_flown_nm`` along the route."""
        remaining = max(0.0, distance_flown_nm)
        for leg in self.legs:
            d = leg.distance_nm
            if remaining <= d:
                frac = remaining / d if d else 1.0
                return intermediate_point(leg.start.location, leg.end.location, frac), leg.track
            remaining -= d
        last = self.legs[-1] if self.legs else None
        return self.waypoints[-1].location, (last.track if last else 0.0)

    def __str__(self) -> str:
        return " ".join(w.ident for w in self.waypoints if not w.ident.startswith("WP"))


# ---------------------------------------------------------------------------
# Cruise level selection
# ---------------------------------------------------------------------------
def available_flight_levels(track: float) -> list[int]:
    """Semicircular/RVSM levels (feet) for a magnetic track (true used as proxy)."""
    eastbound = 0.0 <= track % 360.0 < 180.0
    levels = list(range(11000 if eastbound else 10000, 41000, 2000))
    levels += [41000, 45000, 49000] if eastbound else [43000, 47000, 51000]
    return sorted(levels)


def select_cruise_level(ac_type: AircraftType, distance_nm: float, track: float,
                        max_altitude_ft: Optional[int] = None) -> int:
    """Highest direction-appropriate level that suits the sector length and ceiling."""
    distance_cap = 12000 + distance_nm * 70
    cap = min(ac_type.service_ceiling_ft - 2000, distance_cap)
    if max_altitude_ft is not None:
        cap = min(cap, max_altitude_ft)
    levels = [lvl for lvl in available_flight_levels(track) if lvl <= cap]
    return levels[-1] if levels else available_flight_levels(track)[0]


# ---------------------------------------------------------------------------
# Profile & fuel
# ---------------------------------------------------------------------------
@dataclass
class FlightProfile:
    distance_nm: float
    cruise_altitude_ft: int
    tas_kt: float
    ground_speed_kt: float
    climb_min: float
    cruise_min: float
    descent_min: float
    climb_nm: float
    descent_nm: float
    climb_fuel_kg: float
    cruise_fuel_kg: float
    descent_fuel_kg: float

    @property
    def fuel_kg(self) -> float:
        return self.climb_fuel_kg + self.cruise_fuel_kg + self.descent_fuel_kg

    @property
    def cruise_nm(self) -> float:
        return self.distance_nm - self.climb_nm - self.descent_nm

    @property
    def airborne_min(self) -> float:
        return self.climb_min + self.cruise_min + self.descent_min


def compute_profile(ac_type: AircraftType, distance_nm: float, cruise_altitude_ft: int,
                    wind: Wind = Wind.calm(), track: float = 0.0,
                    payload_ratio: float = 0.5) -> FlightProfile:
    """Climb / cruise / descent time and fuel for a sector."""
    tas = ac_type.cruise_tas_kt * (0.75 + 0.25 * min(cruise_altitude_ft, 35000) / 35000)
    gs = ground_speed(tas, track, wind)
    climb_min = cruise_altitude_ft / ac_type.climb_rate_fpm
    descent_min = cruise_altitude_ft / ac_type.descent_rate_fpm
    # Average speed in climb/descent is lower than cruise.
    climb_nm = climb_min / 60 * gs * 0.65
    descent_nm = descent_min / 60 * gs * 0.75
    if climb_nm + descent_nm > distance_nm:
        scale = distance_nm / (climb_nm + descent_nm)
        climb_min *= scale
        descent_min *= scale
        climb_nm *= scale
        descent_nm *= scale
    cruise_nm = distance_nm - climb_nm - descent_nm
    cruise_min = cruise_nm / gs * 60 if gs > 0 else 0.0
    weight_factor = 0.92 + 0.16 * max(0.0, min(1.0, payload_ratio))
    # Lower cruise levels burn more per hour.
    altitude_factor = 1.0 + 0.25 * max(0.0, (33000 - cruise_altitude_ft) / 33000)
    burn = ac_type.cruise_fuel_burn_kg_h * weight_factor
    return FlightProfile(
        distance_nm, cruise_altitude_ft, tas, gs, climb_min, cruise_min, descent_min,
        climb_nm, descent_nm,
        climb_fuel_kg=burn * 1.6 * climb_min / 60,
        cruise_fuel_kg=burn * altitude_factor * cruise_min / 60,
        descent_fuel_kg=burn * 0.35 * descent_min / 60,
    )


@dataclass
class FuelPlan:
    taxi_kg: float
    trip_kg: float
    contingency_kg: float
    alternate_kg: float
    final_reserve_kg: float
    extra_kg: float = 0.0
    final_reserve_min: float = 30.0

    @property
    def block_kg(self) -> float:
        return (self.taxi_kg + self.trip_kg + self.contingency_kg + self.alternate_kg
                + self.final_reserve_kg + self.extra_kg)

    @property
    def takeoff_kg(self) -> float:
        return self.block_kg - self.taxi_kg

    @property
    def landing_kg(self) -> float:
        return self.takeoff_kg - self.trip_kg

    @property
    def minimum_diversion_kg(self) -> float:
        """Fuel below which the crew must declare minimum fuel."""
        return self.alternate_kg + self.final_reserve_kg

    def lines(self) -> list[str]:
        rows = [("TAXI", self.taxi_kg), ("TRIP", self.trip_kg), ("CONT", self.contingency_kg),
                ("ALTN", self.alternate_kg), (f"FINRES {self.final_reserve_min:.0f}m", self.final_reserve_kg),
                ("EXTRA", self.extra_kg), ("BLOCK", self.block_kg)]
        return [f"{name:<12}{kg:>10,.0f} kg" for name, kg in rows]


def final_reserve_minutes(ac_type: AircraftType) -> float:
    # Commercial air transport: 30 min holding (EASA CAT.OP.MPA.181).
    # Private/business jets under Part 91 / NCC: 45 min at normal cruise.
    return 45.0 if ac_type.category is AircraftCategory.BUSINESS else 30.0


# ---------------------------------------------------------------------------
# Weight, balance and performance
# ---------------------------------------------------------------------------
@dataclass
class Payload:
    passengers_kg: float = 0.0
    baggage_kg: float = 0.0
    cargo_kg: float = 0.0
    cargo_moment_kgm: Optional[float] = None  # if None cargo is assumed centred

    @property
    def total_kg(self) -> float:
        return self.passengers_kg + self.baggage_kg + self.cargo_kg


@dataclass
class Weights:
    oew_kg: float
    payload_kg: float
    zfw_kg: float
    tow_kg: float
    lw_kg: float
    zfw_cg_mac: float
    tow_cg_mac: float


def compute_weights(ac_type: AircraftType, payload: Payload, fuel: FuelPlan) -> Weights:
    mid = ac_type.length_m * 0.5
    empty_arm = ac_type.mac_to_arm(ac_type.empty_cg_mac)
    fuel_arm = ac_type.mac_to_arm(ac_type.fuel_cg_mac)
    cargo_moment = payload.cargo_moment_kgm if payload.cargo_moment_kgm is not None else payload.cargo_kg * mid
    payload_moment = (payload.passengers_kg + payload.baggage_kg) * mid + cargo_moment
    zfw = ac_type.operating_empty_weight_kg + payload.total_kg
    zfw_moment = ac_type.operating_empty_weight_kg * empty_arm + payload_moment
    tow = zfw + fuel.takeoff_kg
    tow_moment = zfw_moment + fuel.takeoff_kg * fuel_arm
    return Weights(
        oew_kg=ac_type.operating_empty_weight_kg,
        payload_kg=payload.total_kg,
        zfw_kg=zfw,
        tow_kg=tow,
        lw_kg=tow - fuel.trip_kg,
        zfw_cg_mac=ac_type.arm_to_mac(zfw_moment / zfw),
        tow_cg_mac=ac_type.arm_to_mac(tow_moment / tow),
    )


def _isa_temp_c(elevation_ft: float) -> float:
    return 15.0 - 1.98 * elevation_ft / 1000.0


def _environment_factor(elevation_ft: float, temperature_c: Optional[float]) -> float:
    factor = 1.0 + 0.07 * elevation_ft / 1000.0
    if temperature_c is not None:
        factor *= 1.0 + 0.01 * max(0.0, temperature_c - _isa_temp_c(elevation_ft))
    return factor


def takeoff_distance_required(ac_type: AircraftType, tow_kg: float, airport: Airport,
                              temperature_c: Optional[float] = None, headwind_kt: float = 0.0) -> float:
    ratio = tow_kg / ac_type.max_takeoff_weight_kg
    # Regulatory factoring: credit 50% of headwind, penalise 150% of tailwind.
    if headwind_kt >= 0:
        wind_factor = max(0.85, 1.0 - 0.005 * headwind_kt)
    else:
        wind_factor = 1.0 - 0.015 * headwind_kt
    return ac_type.takeoff_distance_m * ratio ** 2 * _environment_factor(airport.elevation_ft, temperature_c) * wind_factor


def performance_limited_tow(ac_type: AircraftType, airport: Airport, runway_m: Optional[float] = None,
                            temperature_c: Optional[float] = None) -> float:
    """Highest take-off weight at which the take-off distance fits the runway."""
    runway_m = runway_m or airport.longest_runway_m
    env = _environment_factor(airport.elevation_ft, temperature_c)
    limit = ac_type.max_takeoff_weight_kg * math.sqrt(runway_m / (ac_type.takeoff_distance_m * env))
    return min(limit, ac_type.max_takeoff_weight_kg)


def landing_distance_required(ac_type: AircraftType, lw_kg: float, airport: Airport,
                              wet: bool = False) -> float:
    ratio = lw_kg / ac_type.max_landing_weight_kg
    factor = 1.0 + 0.05 * airport.elevation_ft / 1000.0
    return ac_type.landing_distance_m * ratio * factor * (1.15 if wet else 1.0)


# ---------------------------------------------------------------------------
# Operational flight plan
# ---------------------------------------------------------------------------
@dataclass
class FlightPlan:
    callsign: str
    aircraft_type: AircraftType
    origin: Airport
    destination: Airport
    alternate: Optional[Airport]
    route: Route
    cruise_altitude_ft: int
    wind: Wind
    profile: FlightProfile
    fuel: FuelPlan
    payload: Payload
    weights: Weights
    takeoff_distance_m: float
    landing_distance_m: float
    max_payload_kg: float
    warnings: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not self.warnings

    @property
    def distance_nm(self) -> float:
        return self.route.distance_nm

    @property
    def airborne_minutes(self) -> float:
        return self.profile.airborne_min

    @property
    def block_minutes(self) -> float:
        return self.profile.airborne_min + TAXI_OUT_MIN + TAXI_IN_MIN

    def summary(self) -> str:
        t = self.aircraft_type
        w = self.weights
        hhmm = lambda m: f"{int(m // 60):02d}:{int(round(m % 60)):02d}"  # noqa: E731
        lines = [
            f"OPERATIONAL FLIGHT PLAN  {self.callsign}  {t.icao_code} ({t.category.value})",
            f"{self.origin.icao} -> {self.destination.icao}"
            + (f"  ALTN {self.alternate.icao}" if self.alternate else ""),
            f"ROUTE  {self.route}",
            f"DIST {self.distance_nm:,.0f} NM  FL{self.cruise_altitude_ft // 100:03d}  "
            f"TAS {self.profile.tas_kt:.0f}kt  GS {self.profile.ground_speed_kt:.0f}kt  "
            f"WIND {self.wind.direction:03.0f}/{self.wind.speed:.0f}",
            f"AIR TIME {hhmm(self.airborne_minutes)}  BLOCK {hhmm(self.block_minutes)}",
            "-" * 46,
            *self.fuel.lines(),
            "-" * 46,
            f"{'PAYLOAD':<8}{w.payload_kg:>10,.0f}   (max {self.max_payload_kg:,.0f})",
            f"{'ZFW':<8}{w.zfw_kg:>10,.0f}   MZFW {t.max_zero_fuel_weight_kg:>10,.0f}   CG {w.zfw_cg_mac:5.1f}%MAC",
            f"{'TOW':<8}{w.tow_kg:>10,.0f}   MTOW {t.max_takeoff_weight_kg:>10,.0f}   CG {w.tow_cg_mac:5.1f}%MAC",
            f"{'LW':<8}{w.lw_kg:>10,.0f}   MLW  {t.max_landing_weight_kg:>10,.0f}",
            f"TODR {self.takeoff_distance_m:,.0f} m / {self.origin.longest_runway_m:,.0f} m   "
            f"LDR {self.landing_distance_m:,.0f} m / {self.destination.longest_runway_m:,.0f} m",
        ]
        if self.warnings:
            lines.append("WARNINGS:")
            lines += [f"  ! {w}" for w in self.warnings]
        else:
            lines.append("PLAN OK - within all limits")
        return "\n".join(lines)


TAXI_OUT_MIN = 15.0
TAXI_IN_MIN = 7.0


def create_flight_plan(ac_type: AircraftType, origin: Airport, destination: Airport,
                       payload: Payload, alternate: Optional[Airport] = None,
                       callsign: str = "", wind: Wind = Wind.calm(),
                       cruise_altitude_ft: Optional[int] = None, extra_fuel_kg: float = 0.0,
                       via: Sequence[Waypoint] = (), origin_temp_c: Optional[float] = None,
                       destination_wet: bool = False, surface_wind: Optional[Wind] = None,
                       max_altitude_ft: Optional[int] = None) -> FlightPlan:
    """Build an operational flight plan.

    ``wind`` is the average wind along the route at cruise level;
    ``surface_wind`` is the departure aerodrome wind used for take-off performance.
    ``max_altitude_ft`` caps the cruise level (e.g. an MEL restriction).
    """
    route = Route.direct(origin, destination, via)
    track = route.initial_track
    distance = route.distance_nm
    level = cruise_altitude_ft or select_cruise_level(ac_type, distance, track, max_altitude_ft)
    warnings: list[str] = []
    if level > ac_type.service_ceiling_ft:
        warnings.append(f"FL{level // 100} above service ceiling")
    if max_altitude_ft is not None and level > max_altitude_ft:
        warnings.append(f"FL{level // 100} above restricted maximum FL{max_altitude_ft // 100}")

    payload_ratio = payload.total_kg / ac_type.max_payload_kg if ac_type.max_payload_kg else 0
    trip = compute_profile(ac_type, distance, level, wind, track, payload_ratio)
    burn_min = ac_type.cruise_fuel_burn_kg_h / 60.0
    hold_min = ac_type.holding_fuel_burn_kg_h / 60.0

    alternate_kg = 0.0
    if alternate is not None:
        alt_dist = destination.distance_to(alternate)
        alt_track = initial_bearing(destination.location, alternate.location)
        alt_level = select_cruise_level(ac_type, alt_dist, alt_track, max_altitude_ft)
        alternate_kg = compute_profile(ac_type, alt_dist, alt_level, wind, alt_track, payload_ratio).fuel_kg
    reserve_min = final_reserve_minutes(ac_type)
    reserve_burn = burn_min if ac_type.category is AircraftCategory.BUSINESS else hold_min
    fuel = FuelPlan(
        taxi_kg=ac_type.taxi_fuel_kg_min * TAXI_OUT_MIN,
        trip_kg=trip.fuel_kg,
        contingency_kg=max(0.05 * trip.fuel_kg, 5 * hold_min),
        alternate_kg=alternate_kg,
        final_reserve_kg=reserve_min * reserve_burn,
        extra_kg=extra_fuel_kg,
        final_reserve_min=reserve_min,
    )
    weights = compute_weights(ac_type, payload, fuel)

    # Limits
    if fuel.block_kg > ac_type.max_fuel_kg:
        warnings.append(f"Block fuel {fuel.block_kg:,.0f} kg exceeds tank capacity "
                        f"{ac_type.max_fuel_kg:,.0f} kg - sector beyond range")
    if weights.zfw_kg > ac_type.max_zero_fuel_weight_kg:
        warnings.append(f"ZFW exceeds MZFW by {weights.zfw_kg - ac_type.max_zero_fuel_weight_kg:,.0f} kg")
    perf_tow = performance_limited_tow(ac_type, origin, temperature_c=origin_temp_c)
    if weights.tow_kg > perf_tow:
        limiting = "MTOW" if perf_tow >= ac_type.max_takeoff_weight_kg else "runway/performance limit"
        warnings.append(f"TOW exceeds {limiting} ({perf_tow:,.0f} kg) by {weights.tow_kg - perf_tow:,.0f} kg")
    if weights.lw_kg > ac_type.max_landing_weight_kg:
        warnings.append(f"LW exceeds MLW by {weights.lw_kg - ac_type.max_landing_weight_kg:,.0f} kg")
    for label, cg in (("ZFW", weights.zfw_cg_mac), ("TOW", weights.tow_cg_mac)):
        if not ac_type.cg_forward_limit_mac <= cg <= ac_type.cg_aft_limit_mac:
            warnings.append(f"{label} CG {cg:.1f}%MAC outside envelope "
                            f"{ac_type.cg_forward_limit_mac:.0f}-{ac_type.cg_aft_limit_mac:.0f}%")

    sfc = surface_wind or Wind.calm()
    headwind = headwind_component(origin.best_runway_heading(sfc.direction), sfc) if sfc.speed else 0.0
    todr = takeoff_distance_required(ac_type, weights.tow_kg, origin, origin_temp_c, headwind)
    ldr = landing_distance_required(ac_type, weights.lw_kg, destination, destination_wet)
    if todr > origin.longest_runway_m:
        warnings.append(f"Take-off distance {todr:,.0f} m exceeds runway {origin.longest_runway_m:,.0f} m")
    if ldr > destination.longest_runway_m:
        warnings.append(f"Landing distance {ldr:,.0f} m exceeds runway {destination.longest_runway_m:,.0f} m")

    max_payload = min(
        ac_type.max_payload_kg,
        perf_tow - ac_type.operating_empty_weight_kg - fuel.takeoff_kg,
        ac_type.max_landing_weight_kg - ac_type.operating_empty_weight_kg - fuel.landing_kg,
    )
    return FlightPlan(callsign, ac_type, origin, destination, alternate, route, level, wind, trip,
                      fuel, payload, weights, todr, ldr, max(0.0, max_payload), warnings)


def crosswind_check(ac_type: AircraftType, airport: Airport, wind: Wind) -> Optional[str]:
    """Return a warning if the best runway's crosswind exceeds the aircraft limit."""
    if not airport.runways or wind.speed == 0:
        return None
    heading = airport.best_runway_heading(wind.direction)
    xwind = crosswind_component(heading, wind)
    if xwind > ac_type.max_crosswind_kt:
        return (f"{airport.icao}: crosswind {xwind:.0f} kt on runway heading {heading:03.0f} "
                f"exceeds {ac_type.max_crosswind_kt:.0f} kt limit")
    return None
