"""Flights: schedule, lifecycle state machine, events and live tracking."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

from .aircraft import Aircraft, AircraftCategory
from .airports import Airport
from .cargo import LoadPlan
from .crew import CrewMember
from .geo import Coordinate, great_circle_distance, initial_bearing, intermediate_point
from .passengers import PassengerManifest
from .planning import TAXI_IN_MIN, TAXI_OUT_MIN, FlightPlan, Payload


class FlightType(Enum):
    SCHEDULED_PASSENGER = "scheduled passenger"
    CHARTER = "charter"
    PRIVATE = "private / corporate"
    CARGO = "cargo"
    FERRY = "ferry / positioning"


class FlightStatus(Enum):
    SCHEDULED = "scheduled"
    DISPATCHED = "dispatched"
    BOARDING = "boarding"
    DEPARTED = "departed"  # off blocks
    AIRBORNE = "airborne"
    LANDED = "landed"
    ARRIVED = "arrived"  # on blocks
    DIVERTED = "diverted"
    CANCELLED = "cancelled"


class FlightPhase(Enum):
    PREFLIGHT = "preflight"
    TAXI_OUT = "taxi out"
    TAKEOFF = "takeoff"
    CLIMB = "climb"
    CRUISE = "cruise"
    DESCENT = "descent"
    APPROACH = "approach"
    LANDING = "landing"
    TAXI_IN = "taxi in"
    PARKED = "parked"


_NEXT_PHASE = {
    FlightPhase.PREFLIGHT: {FlightPhase.TAXI_OUT},
    FlightPhase.TAXI_OUT: {FlightPhase.TAKEOFF, FlightPhase.PREFLIGHT},  # return to stand
    FlightPhase.TAKEOFF: {FlightPhase.CLIMB, FlightPhase.TAXI_IN},  # rejected take-off
    FlightPhase.CLIMB: {FlightPhase.CRUISE, FlightPhase.DESCENT, FlightPhase.APPROACH},
    FlightPhase.CRUISE: {FlightPhase.DESCENT, FlightPhase.CLIMB},  # step climb
    FlightPhase.DESCENT: {FlightPhase.APPROACH, FlightPhase.CRUISE, FlightPhase.CLIMB},
    FlightPhase.APPROACH: {FlightPhase.LANDING, FlightPhase.CLIMB},  # go-around
    FlightPhase.LANDING: {FlightPhase.TAXI_IN, FlightPhase.CLIMB},  # go-around / touch-and-go
    FlightPhase.TAXI_IN: {FlightPhase.PARKED},
    FlightPhase.PARKED: set(),
}

AIRBORNE_PHASES = {FlightPhase.TAKEOFF, FlightPhase.CLIMB, FlightPhase.CRUISE,
                   FlightPhase.DESCENT, FlightPhase.APPROACH, FlightPhase.LANDING}

# IATA standard delay codes (subset)
DELAY_CODES = {
    "11": "Late check-in", "15": "Boarding", "18": "Baggage processing",
    "21": "Cargo documentation", "31": "Aircraft documentation late",
    "41": "Aircraft defects", "46": "Aircraft change", "63": "Late crew boarding",
    "64": "Flight deck crew shortage", "71": "Departure station weather",
    "72": "Destination weather", "81": "ATFM en-route capacity", "87": "Airport facilities",
    "89": "Restrictions at departure airport", "93": "Aircraft rotation",
}


@dataclass
class FlightEvent:
    time: datetime
    kind: str
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.time:%Y-%m-%d %H:%MZ} {self.kind:<14} {self.detail}"


@dataclass
class FlightState:
    """A snapshot of a flight's progress (what an FMS progress page shows)."""

    time: datetime
    phase: FlightPhase
    position: Coordinate
    altitude_ft: float
    ground_speed_kt: float
    track: float
    distance_flown_nm: float
    distance_remaining_nm: float
    fuel_on_board_kg: float
    eta: datetime

    def __str__(self) -> str:
        return (f"{self.time:%H:%MZ} {self.phase.value:<9} {self.position}  "
                f"ALT {self.altitude_ft:>6,.0f}ft  GS {self.ground_speed_kt:>3.0f}kt  TRK {self.track:03.0f}  "
                f"FLOWN {self.distance_flown_nm:>6,.0f}NM  TOGO {self.distance_remaining_nm:>6,.0f}NM  "
                f"FOB {self.fuel_on_board_kg:>8,.0f}kg  ETA {self.eta:%H:%MZ}")


_flight_ids = itertools.count(1)


@dataclass
class Flight:
    flight_number: str
    aircraft: Aircraft
    origin: Airport
    destination: Airport
    scheduled_departure: datetime
    scheduled_arrival: datetime
    flight_type: FlightType = FlightType.SCHEDULED_PASSENGER
    alternate: Optional[Airport] = None
    flight_id: int = field(default_factory=lambda: next(_flight_ids))
    status: FlightStatus = FlightStatus.SCHEDULED
    phase: FlightPhase = FlightPhase.PREFLIGHT
    crew: list[CrewMember] = field(default_factory=list)
    manifest: Optional[PassengerManifest] = None
    load_plan: Optional[LoadPlan] = None
    plan: Optional[FlightPlan] = None
    delay_minutes: int = 0
    delay_codes: list[str] = field(default_factory=list)
    off_block: Optional[datetime] = None
    takeoff_time: Optional[datetime] = None
    landing_time: Optional[datetime] = None
    on_block: Optional[datetime] = None
    events: list[FlightEvent] = field(default_factory=list)
    diverted_to: Optional[Airport] = None
    diversion_start: Optional["FlightState"] = None
    diversion_eta: Optional[datetime] = None

    def __post_init__(self) -> None:
        if self.scheduled_arrival <= self.scheduled_departure:
            raise ValueError("Scheduled arrival must be after departure")
        if self.origin.icao == self.destination.icao:
            raise ValueError("Origin and destination must differ")
        if self.manifest is None and self.aircraft.category is not AircraftCategory.CARGO:
            self.manifest = PassengerManifest(self.aircraft)
        if self.load_plan is None and self.aircraft.category is not AircraftCategory.BUSINESS:
            self.load_plan = LoadPlan(self.aircraft)

    # -- schedule ---------------------------------------------------------------
    @property
    def estimated_departure(self) -> datetime:
        return self.scheduled_departure + timedelta(minutes=self.delay_minutes)

    @property
    def estimated_arrival(self) -> datetime:
        if self.plan is not None:
            return self.estimated_departure + timedelta(minutes=self.plan.block_minutes)
        return self.scheduled_arrival + timedelta(minutes=self.delay_minutes)

    @property
    def scheduled_block_hours(self) -> float:
        return (self.scheduled_arrival - self.scheduled_departure).total_seconds() / 3600.0

    @property
    def actual_block_hours(self) -> Optional[float]:
        if self.off_block and self.on_block:
            return (self.on_block - self.off_block).total_seconds() / 3600.0
        return None

    @property
    def is_active(self) -> bool:
        return self.status not in (FlightStatus.ARRIVED, FlightStatus.CANCELLED)

    @property
    def is_airborne(self) -> bool:
        return self.phase in AIRBORNE_PHASES

    @property
    def arrival_airport(self) -> Airport:
        return self.diverted_to or self.destination

    def occupies(self, start: datetime, end: datetime) -> bool:
        if self.status is FlightStatus.CANCELLED:
            return False
        return self.estimated_departure < end and start < self.estimated_arrival

    # -- payload ----------------------------------------------------------------
    def payload(self) -> Payload:
        pax = self.manifest.passenger_mass_kg if self.manifest else 0.0
        bags = self.manifest.baggage_mass_kg if self.manifest else 0.0
        cargo, moment = 0.0, None
        if self.load_plan is not None and self.load_plan.total_weight_kg:
            cargo = self.load_plan.total_weight_kg
            moment = self.load_plan.moment_kgm
        return Payload(pax, bags, cargo, moment)

    # -- lifecycle -----------------------------------------------------------------
    def log(self, time: datetime, kind: str, detail: str = "") -> None:
        self.events.append(FlightEvent(time, kind, detail))

    def set_phase(self, phase: FlightPhase, time: datetime) -> None:
        if phase is self.phase:
            return
        if phase not in _NEXT_PHASE[self.phase]:
            raise ValueError(f"{self.flight_number}: invalid phase change {self.phase.value} -> {phase.value}")
        if self.phase is FlightPhase.APPROACH and phase is FlightPhase.CLIMB:
            self.log(time, "GO-AROUND")
        self.phase = phase
        self.log(time, "PHASE", phase.value)

    def _require(self, *allowed: FlightStatus) -> None:
        if self.status not in allowed:
            raise ValueError(f"{self.flight_number} is {self.status.value}; "
                             f"expected {', '.join(s.value for s in allowed)}")

    def delay(self, minutes: int, code: str, time: datetime, remark: str = "") -> None:
        self._require(FlightStatus.SCHEDULED, FlightStatus.DISPATCHED, FlightStatus.BOARDING)
        if code not in DELAY_CODES:
            raise ValueError(f"Unknown IATA delay code {code}")
        self.delay_minutes += minutes
        self.delay_codes.append(code)
        self.log(time, "DELAY", f"+{minutes}m code {code} {DELAY_CODES[code]} {remark}".strip())

    def cancel(self, reason: str, time: datetime) -> None:
        self._require(FlightStatus.SCHEDULED, FlightStatus.DISPATCHED, FlightStatus.BOARDING)
        self.status = FlightStatus.CANCELLED
        self.log(time, "CANCELLED", reason)

    def start_boarding(self, time: datetime) -> None:
        self._require(FlightStatus.DISPATCHED)
        self.status = FlightStatus.BOARDING
        self.log(time, "BOARDING")

    def push_back(self, time: datetime) -> None:
        self._require(FlightStatus.DISPATCHED, FlightStatus.BOARDING)
        if self.manifest is not None:
            offloaded = self.manifest.close_flight()
            if offloaded:
                self.log(time, "BAGS OFFLOAD", f"{len(offloaded)} unaccompanied passenger(s)")
        self.status = FlightStatus.DEPARTED
        self.off_block = time
        self.set_phase(FlightPhase.TAXI_OUT, time)
        self.log(time, "OFF BLOCKS", self.origin.icao)

    def take_off(self, time: datetime) -> None:
        self._require(FlightStatus.DEPARTED)
        self.set_phase(FlightPhase.TAKEOFF, time)
        self.status = FlightStatus.AIRBORNE
        self.takeoff_time = time
        self.log(time, "AIRBORNE", self.origin.icao)

    def land(self, time: datetime) -> None:
        self._require(FlightStatus.AIRBORNE, FlightStatus.DIVERTED)
        if self.phase is not FlightPhase.LANDING:
            if self.phase is not FlightPhase.APPROACH:
                self.set_phase(FlightPhase.APPROACH, time)
            self.set_phase(FlightPhase.LANDING, time)
        self.landing_time = time
        self.set_phase(FlightPhase.TAXI_IN, time)
        if self.status is not FlightStatus.DIVERTED:
            self.status = FlightStatus.LANDED
        self.log(time, "LANDED", self.arrival_airport.icao)

    def arrive(self, time: datetime) -> None:
        self._require(FlightStatus.LANDED, FlightStatus.DIVERTED)
        self.set_phase(FlightPhase.PARKED, time)
        self.on_block = time
        if self.status is not FlightStatus.DIVERTED:
            self.status = FlightStatus.ARRIVED
        self.log(time, "ON BLOCKS", self.arrival_airport.icao)

    def divert(self, airport: Airport, reason: str, time: datetime,
               eta: Optional[datetime] = None) -> None:
        self._require(FlightStatus.AIRBORNE)
        if self.plan is not None:
            self.diversion_start = self.state_at(time)
            if eta is None:
                dist = great_circle_distance(self.diversion_start.position, airport.location)
                speed = max(self.diversion_start.ground_speed_kt, self.aircraft.type.cruise_tas_kt * 0.8)
                eta = time + timedelta(minutes=dist / speed * 60 + 10)
        self.diversion_eta = eta
        self.diverted_to = airport
        self.status = FlightStatus.DIVERTED
        self.log(time, "DIVERSION", f"to {airport.icao}: {reason}")

    # -- tracking --------------------------------------------------------------------
    def state_at(self, time: datetime) -> FlightState:
        """Estimate where the flight is at ``time`` using its flight plan profile."""
        if self.plan is None:
            raise ValueError(f"{self.flight_number} has no flight plan")
        if self.diversion_start is not None and time >= self.diversion_start.time:
            return self._diversion_state(time)
        p = self.plan.profile
        route = self.plan.route
        fuel = self.plan.fuel
        takeoff = self.takeoff_time or (
            (self.off_block or self.estimated_departure) + timedelta(minutes=TAXI_OUT_MIN))
        eta = takeoff + timedelta(minutes=p.airborne_min)
        t = (time - takeoff).total_seconds() / 60.0
        level = p.cruise_altitude_ft
        start_fuel = fuel.block_kg
        if t < 0:
            taxi_used = 0.0
            if self.off_block and time >= self.off_block:
                taxi_used = fuel.taxi_kg * min(1.0, (time - self.off_block).total_seconds() / 60 / TAXI_OUT_MIN)
            phase = FlightPhase.TAXI_OUT if self.off_block and time >= self.off_block else FlightPhase.PREFLIGHT
            return FlightState(time, phase, self.origin.location, self.origin.elevation_ft, 0.0,
                               route.initial_track, 0.0, route.distance_nm, start_fuel - taxi_used, eta)
        fob = fuel.takeoff_kg
        if t <= p.climb_min:
            f = t / p.climb_min if p.climb_min else 1.0
            dist, alt, fob = p.climb_nm * f, self.origin.elevation_ft + (level - self.origin.elevation_ft) * f, fob - p.climb_fuel_kg * f
            phase, gs = FlightPhase.CLIMB, p.ground_speed_kt * 0.65
        elif t <= p.climb_min + p.cruise_min:
            f = (t - p.climb_min) / p.cruise_min if p.cruise_min else 1.0
            dist, alt = p.climb_nm + p.cruise_nm * f, level
            fob -= p.climb_fuel_kg + p.cruise_fuel_kg * f
            phase, gs = FlightPhase.CRUISE, p.ground_speed_kt
        elif t <= p.airborne_min:
            f = (t - p.climb_min - p.cruise_min) / p.descent_min if p.descent_min else 1.0
            dist = p.climb_nm + p.cruise_nm + p.descent_nm * f
            alt = level + (self.destination.elevation_ft - level) * f
            fob -= p.climb_fuel_kg + p.cruise_fuel_kg + p.descent_fuel_kg * f
            phase = FlightPhase.APPROACH if p.airborne_min - t <= 8 else FlightPhase.DESCENT
            gs = p.ground_speed_kt * 0.75
        else:
            dist, alt, fob = route.distance_nm, self.destination.elevation_ft, fuel.landing_kg
            phase = FlightPhase.TAXI_IN if t <= p.airborne_min + TAXI_IN_MIN else FlightPhase.PARKED
            gs = 0.0
        position, track = route.position_at(dist)
        return FlightState(time, phase, position, alt, gs, track, dist,
                           max(0.0, route.distance_nm - dist), fob, eta)

    def _diversion_state(self, time: datetime) -> FlightState:
        start, eta, target = self.diversion_start, self.diversion_eta, self.diverted_to
        total = (eta - start.time).total_seconds()
        f = min(1.0, (time - start.time).total_seconds() / total) if total > 0 else 1.0
        dist = great_circle_distance(start.position, target.location)
        position = intermediate_point(start.position, target.location, f)
        burn = self.aircraft.type.cruise_fuel_burn_kg_h * (time - start.time).total_seconds() / 3600
        landed = f >= 1.0
        phase = (FlightPhase.TAXI_IN if landed else
                 FlightPhase.APPROACH if (eta - time) <= timedelta(minutes=8) else FlightPhase.DESCENT)
        return FlightState(time, phase, position,
                           start.altitude_ft + (target.elevation_ft - start.altitude_ft) * f,
                           0.0 if landed else start.ground_speed_kt,
                           initial_bearing(position, target.location) if not landed else start.track,
                           start.distance_flown_nm + dist * f, dist * (1 - f),
                           max(0.0, start.fuel_on_board_kg - burn), eta)

    def summary(self) -> str:
        dep = self.estimated_departure
        delay = f" (+{self.delay_minutes}m)" if self.delay_minutes else ""
        return (f"{self.flight_number:<8} {self.aircraft.registration:<8} {self.aircraft.type.icao_code:<5}"
                f"{self.origin.icao}-{self.arrival_airport.icao}  STD {self.scheduled_departure:%d%b %H:%MZ}"
                f"  ETD {dep:%H:%MZ}{delay}  {self.status.value}")
