"""The FlightManagementSystem: operations control tying fleet, crew, planning,
maintenance, dispatch and flight following together."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from .aircraft import Aircraft, AircraftCategory, AircraftStatus
from .airports import Airport, AirportDatabase, default_airports
from .cargo import LoadPlan
from .crew import (RELEASE_AFTER_ARRIVAL, REPORT_BEFORE_DEPARTURE, CrewMember, CrewRequirement,
                   CrewRole, DutyRecord, crew_complement_issues)
from .flight import Flight, FlightPhase, FlightStatus, FlightType
from .geo import initial_bearing
from .maintenance import CheckRecord, MaintenanceTracker
from .passengers import PassengerManifest, PassengerStatus
from .planning import (TAXI_IN_MIN, TAXI_OUT_MIN, FlightPlan, Payload, compute_profile,
                       create_flight_plan, crosswind_check, landing_distance_required,
                       select_cruise_level)
from .weather import FlightCategory, Metar, Wind, parse_metar

MIN_TURNAROUND = {
    AircraftCategory.COMMERCIAL: timedelta(minutes=45),
    AircraftCategory.CARGO: timedelta(minutes=90),
    AircraftCategory.BUSINESS: timedelta(minutes=30),
}

_PHASE_ORDER = list(FlightPhase)

DEFAULT_FLIGHT_TYPE = {
    AircraftCategory.COMMERCIAL: FlightType.SCHEDULED_PASSENGER,
    AircraftCategory.CARGO: FlightType.CARGO,
    AircraftCategory.BUSINESS: FlightType.PRIVATE,
}


class SchedulingError(ValueError):
    pass


class CrewAssignmentError(ValueError):
    pass


@dataclass
class DispatchRelease:
    flight: Flight
    time: datetime
    issues: list[str] = field(default_factory=list)  # no-go items
    advisories: list[str] = field(default_factory=list)  # operate with caution

    @property
    def approved(self) -> bool:
        return not self.issues

    def __str__(self) -> str:
        head = (f"DISPATCH {'RELEASED' if self.approved else 'REFUSED'}: "
                f"{self.flight.flight_number} at {self.time:%H:%MZ}")
        body = [f"  NO-GO: {i}" for i in self.issues] + [f"  NOTE:  {a}" for a in self.advisories]
        return "\n".join([head, *body])


class FlightManagementSystem:
    def __init__(self, airports: Optional[AirportDatabase] = None, operator: str = ""):
        self.airports = airports or default_airports()
        self.operator = operator
        self.fleet: dict[str, Aircraft] = {}
        self.maintenance: dict[str, MaintenanceTracker] = {}
        self.crew: dict[str, CrewMember] = {}
        self.flights: dict[int, Flight] = {}
        self.weather: dict[str, Metar] = {}
        self.winds_aloft: dict[int, Wind] = {}  # flight_id -> average route wind

    # ------------------------------------------------------------------ fleet
    def add_aircraft(self, aircraft: Aircraft, since: Optional[datetime] = None,
                     last_checks: Optional[dict[str, CheckRecord]] = None) -> Aircraft:
        if aircraft.registration in self.fleet:
            raise ValueError(f"{aircraft.registration} already in fleet")
        if aircraft.location and aircraft.location not in self.airports:
            raise ValueError(f"Unknown airport {aircraft.location}")
        self.fleet[aircraft.registration] = aircraft
        self.maintenance[aircraft.registration] = MaintenanceTracker(aircraft, since=since, last_checks=last_checks)
        return aircraft

    def aircraft(self, registration: str) -> Aircraft:
        try:
            return self.fleet[registration]
        except KeyError as exc:
            raise KeyError(f"Unknown aircraft {registration}") from exc

    def fleet_by_category(self, category: AircraftCategory) -> list[Aircraft]:
        return [a for a in self.fleet.values() if a.category is category]

    def send_to_maintenance(self, registration: str, check: str, start: datetime) -> datetime:
        """Ground an aircraft for a check; returns the expected release time."""
        ac = self.aircraft(registration)
        tracker = self.maintenance[registration]
        interval = next(c for c in tracker.program if c.name == check)
        end = start + timedelta(hours=interval.downtime_hours)
        clash = [f for f in self.flights_for_aircraft(registration) if f.occupies(start, end)]
        if clash:
            raise SchedulingError(f"{registration} has flights during the check: "
                                  + ", ".join(f.flight_number for f in clash))
        ac.status = AircraftStatus.MAINTENANCE
        return end

    def release_from_maintenance(self, registration: str, check: str, when: datetime) -> None:
        ac = self.aircraft(registration)
        tracker = self.maintenance[registration]
        tracker.perform_check(check, when)
        ac.status = AircraftStatus.AVAILABLE
        if tracker.airworthiness_issues(when):
            ac.status = AircraftStatus.AOG

    # ------------------------------------------------------------------- crew
    def add_crew(self, member: CrewMember) -> CrewMember:
        if member.employee_id in self.crew:
            raise ValueError(f"Crew {member.employee_id} already exists")
        self.crew[member.employee_id] = member
        return member

    def crew_requirement(self, flight: Flight) -> CrewRequirement:
        block = flight.plan.block_minutes / 60 if flight.plan else flight.scheduled_block_hours
        fdp = block + (REPORT_BEFORE_DEPARTURE + RELEASE_AFTER_ARRIVAL).total_seconds() / 3600
        augmented = fdp > 13.0
        pilots = flight.aircraft.type.min_cockpit_crew + (1 if augmented else 0) + (1 if fdp > 15.0 else 0)
        cabin = getattr(flight.aircraft, "required_cabin_crew", 0)
        loadmasters = 0
        if flight.aircraft.category is AircraftCategory.CARGO and flight.load_plan and flight.load_plan.notoc():
            loadmasters = 1
        return CrewRequirement(pilots, cabin, loadmasters, augmented)

    def _duty_window(self, flight: Flight) -> tuple[datetime, datetime]:
        return (flight.estimated_departure - REPORT_BEFORE_DEPARTURE,
                flight.estimated_arrival + RELEASE_AFTER_ARRIVAL)

    def _rostered_elsewhere(self, member: CrewMember, flight: Flight) -> bool:
        report, release = self._duty_window(flight)
        for other in self.flights.values():
            if other is flight or not other.is_active or member not in other.crew:
                continue
            o_report, o_release = self._duty_window(other)
            if o_report < release and report < o_release:
                return True
        return False

    def crew_issues(self, member: CrewMember, flight: Flight) -> list[str]:
        report, release = self._duty_window(flight)
        req = self.crew_requirement(flight)
        block = (flight.estimated_arrival - flight.estimated_departure).total_seconds() / 3600
        issues = member.legality_issues(flight.aircraft.type.icao_code, report, release, block, req.augmented)
        if self._rostered_elsewhere(member, flight):
            issues.append(f"{member.name}: already rostered on an overlapping flight")
        location = self._crew_location(member, report)
        if location and location != flight.origin.icao:
            issues.append(f"{member.name}: positioned at {location}, not {flight.origin.icao}")
        return issues

    def _crew_location(self, member: CrewMember, time: datetime) -> str:
        """Where the crew member will be at ``time``, including rostered (not yet flown) flights."""
        location, latest = member.location_at(time), None
        for other in self.flights.values():
            if member in other.crew and other.status is not FlightStatus.CANCELLED \
                    and other.estimated_arrival <= time and (latest is None or other.estimated_arrival > latest):
                latest = other.estimated_arrival
                location = other.arrival_airport.icao
        return location

    def assign_crew(self, flight: Flight, *employee_ids: str) -> None:
        members = [self.crew[e] for e in employee_ids]
        problems = [i for m in members for i in self.crew_issues(m, flight)]
        if problems:
            raise CrewAssignmentError("; ".join(problems))
        for m in members:
            if m not in flight.crew:
                flight.crew.append(m)

    def suggest_crew(self, flight: Flight) -> list[CrewMember]:
        """Pick legal, available crew to fill the requirement (base crew preferred)."""
        req = self.crew_requirement(flight)
        needed = {CrewRole.CAPTAIN: 1, CrewRole.FIRST_OFFICER: req.pilots - 1,
                  CrewRole.CABIN_CREW: req.cabin_crew, CrewRole.LOADMASTER: req.loadmasters}
        if req.cabin_crew:
            needed[CrewRole.PURSER] = 1
            needed[CrewRole.CABIN_CREW] = req.cabin_crew - 1
        chosen: list[CrewMember] = []
        for role, count in needed.items():
            pool = [c for c in self.crew.values() if c.role is role and c not in flight.crew
                    and not self.crew_issues(c, flight)]
            pool.sort(key=lambda c: (c.base != flight.origin.icao,
                                     c.flight_hours_between(flight.scheduled_departure - timedelta(days=28),
                                                            flight.scheduled_departure)))
            have = sum(1 for c in flight.crew if c.role is role)
            chosen.extend(pool[:max(0, count - have)])
        return chosen

    # -------------------------------------------------------------- scheduling
    def flights_for_aircraft(self, registration: str) -> list[Flight]:
        return sorted((f for f in self.flights.values() if f.aircraft.registration == registration),
                      key=lambda f: f.scheduled_departure)

    def find_flight(self, flight_number: str, on: Optional[datetime] = None) -> Flight:
        matches = [f for f in self.flights.values() if f.flight_number == flight_number
                   and (on is None or f.scheduled_departure.date() == on.date())]
        if not matches:
            raise KeyError(f"No flight {flight_number}")
        return matches[0]

    def estimate_block_time(self, aircraft: Aircraft, origin: Airport, destination: Airport,
                            wind: Wind = Wind.calm()) -> timedelta:
        distance = origin.distance_to(destination)
        track = initial_bearing(origin.location, destination.location)
        level = select_cruise_level(aircraft.type, distance, track)
        profile = compute_profile(aircraft.type, distance, level, wind, track)
        # Schedules are padded ~5% to absorb typical winds and ATC delays.
        minutes = (profile.airborne_min + TAXI_OUT_MIN + TAXI_IN_MIN) * 1.05
        return timedelta(minutes=5 * round(minutes / 5))

    def schedule_flight(self, flight_number: str, registration: str, origin: str, destination: str,
                        departure: datetime, arrival: Optional[datetime] = None,
                        alternate: Optional[str] = None, flight_type: Optional[FlightType] = None) -> Flight:
        ac = self.aircraft(registration)
        if ac.status is AircraftStatus.RETIRED:
            raise SchedulingError(f"{registration} is retired")
        orig, dest = self.airports.get(origin), self.airports.get(destination)
        alt = self.airports.get(alternate) if alternate else None
        distance = orig.distance_to(dest)
        if distance > ac.type.range_nm:
            raise SchedulingError(f"{orig.icao}-{dest.icao} ({distance:,.0f} NM) exceeds "
                                  f"{ac.type.icao_code} range ({ac.type.range_nm:,.0f} NM)")
        for apt in (orig, dest):
            if apt.longest_runway_m < ac.type.landing_distance_m:
                raise SchedulingError(f"{apt.icao} runway too short for {ac.type.icao_code}")
        arrival = arrival or departure + self.estimate_block_time(ac, orig, dest)
        ftype = flight_type or DEFAULT_FLIGHT_TYPE[ac.category]
        if ac.category is AircraftCategory.CARGO and ftype not in (FlightType.CARGO, FlightType.FERRY):
            raise SchedulingError("Freighters may only operate cargo or ferry flights")

        turnaround = MIN_TURNAROUND[ac.category]
        existing = [f for f in self.flights_for_aircraft(registration) if f.status is not FlightStatus.CANCELLED]
        for f in existing:
            if f.estimated_departure < arrival + turnaround and departure < f.estimated_arrival + turnaround:
                raise SchedulingError(f"{registration} conflicts with {f.flight_number} "
                                      f"({f.estimated_departure:%d%b %H:%M}-{f.estimated_arrival:%H:%M}) "
                                      f"incl. {turnaround.seconds // 60} min turnaround")
        previous = [f for f in existing if f.estimated_arrival <= departure]
        expected_at = previous[-1].arrival_airport.icao if previous else ac.location
        if expected_at and expected_at != orig.icao and ftype is not FlightType.FERRY:
            raise SchedulingError(f"{registration} will be at {expected_at}, not {orig.icao}; "
                                  "schedule a ferry flight first")

        flight = Flight(flight_number, ac, orig, dest, departure, arrival, ftype, alt)
        self.flights[flight.flight_id] = flight
        flight.log(departure - timedelta(days=1), "SCHEDULED", f"{orig.icao}-{dest.icao} on {registration}")
        return flight

    def swap_aircraft(self, flight: Flight, registration: str, time: datetime) -> None:
        """Tail swap (e.g. after a defect). Passenger cabin must be large enough."""
        new = self.aircraft(registration)
        if new.category is not flight.aircraft.category:
            raise SchedulingError("Replacement must be the same category")
        if flight.manifest and flight.manifest.passenger_count > new.passenger_capacity:
            raise SchedulingError(f"{registration} has only {new.passenger_capacity} seats")
        for f in self.flights_for_aircraft(registration):
            if f.is_active and f.occupies(flight.estimated_departure, flight.estimated_arrival):
                raise SchedulingError(f"{registration} is busy with {f.flight_number}")
        old = flight.aircraft.registration
        flight.aircraft = new
        flight.plan = None
        if flight.load_plan is not None:
            if flight.load_plan.loaded:
                raise SchedulingError("Re-plan cargo load before swapping a loaded freighter")
            bulk = flight.load_plan.bulk_kg
            flight.load_plan = LoadPlan(new)
            flight.load_plan.bulk_kg = bulk
        if flight.manifest is not None:
            for p in flight.manifest.passengers:
                flight.manifest.seat_map.release(p)
            old_pax = flight.manifest.passengers
            flight.manifest = PassengerManifest(new)
            flight.manifest.passengers = old_pax
            for p in old_pax:
                if p.status is PassengerStatus.CHECKED_IN and p.needs_seat:
                    flight.manifest.seat_map.assign(p)
        flight.log(time, "TAIL SWAP", f"{old} -> {registration}")
        if flight.status is FlightStatus.DISPATCHED:
            flight.status = FlightStatus.SCHEDULED  # must be re-dispatched

    # ---------------------------------------------------------------- weather
    def update_weather(self, *metars: str | Metar) -> None:
        for m in metars:
            metar = parse_metar(m) if isinstance(m, str) else m
            self.weather[metar.station] = metar

    def set_route_wind(self, flight: Flight, wind: Wind) -> None:
        self.winds_aloft[flight.flight_id] = wind

    # --------------------------------------------------------------- planning
    def plan_flight(self, flight: Flight, extra_fuel_kg: float = 0.0,
                    cruise_altitude_ft: Optional[int] = None) -> FlightPlan:
        metar = self.weather.get(flight.origin.icao)
        dest_metar = self.weather.get(flight.destination.icao)
        wet = bool(dest_metar and any(x in p for p in dest_metar.phenomena for x in ("RA", "SN", "DZ")))
        plan = create_flight_plan(
            flight.aircraft.type, flight.origin, flight.destination, flight.payload(),
            alternate=flight.alternate, callsign=flight.flight_number,
            wind=self.winds_aloft.get(flight.flight_id, Wind.calm()),
            cruise_altitude_ft=cruise_altitude_ft, extra_fuel_kg=extra_fuel_kg,
            origin_temp_c=metar.temperature_c if metar else None, destination_wet=wet,
            surface_wind=metar.wind if metar else None,
            max_altitude_ft=self.maintenance[flight.aircraft.registration].altitude_limit(),
        )
        flight.plan = plan
        flight.log(flight.estimated_departure - timedelta(hours=2), "OFP",
                   f"block fuel {plan.fuel.block_kg:,.0f} kg, {len(plan.warnings)} warning(s)")
        return plan

    # --------------------------------------------------------------- dispatch
    def dispatch(self, flight: Flight, now: datetime) -> DispatchRelease:
        release = DispatchRelease(flight, now)
        issues, notes = release.issues, release.advisories
        ac = flight.aircraft
        if flight.status is not FlightStatus.SCHEDULED:
            issues.append(f"Flight is {flight.status.value}")
        # Aircraft
        if ac.status not in (AircraftStatus.AVAILABLE,):
            issues.append(f"{ac.registration} is {ac.status.value}")
        tracker = self.maintenance[ac.registration]
        issues += [f"{ac.registration}: {i}" for i in tracker.airworthiness_issues(now)]
        block_h = (flight.estimated_arrival - flight.estimated_departure).total_seconds() / 3600
        due = tracker.checks_due_within(now, flight_hours=block_h, cycles=1,
                                        days=(flight.estimated_arrival - now).total_seconds() / 86400)
        issues += [f"{c} falls due before arrival" for c in due]
        notes += [f"MEL restriction: {r}" for r in tracker.restrictions()]
        if ac.location != flight.origin.icao:
            issues.append(f"{ac.registration} is at {ac.location}, not {flight.origin.icao}")
        # Crew
        req = self.crew_requirement(flight)
        issues += crew_complement_issues(flight.crew, req)
        for member in flight.crew:
            issues += self.crew_issues(member, flight)
        # Payload & flight plan (always recomputed with the latest load figures)
        plan = self.plan_flight(flight, extra_fuel_kg=flight.plan.fuel.extra_kg if flight.plan else 0.0)
        issues += plan.warnings
        if flight.load_plan is not None:
            notoc = flight.load_plan.notoc()
            notes += [f"NOTOC {line}" for line in notoc if " DG " in line]
            special = [line for line in notoc if " DG " not in line]
            if special:
                codes = sorted({c.strip() for line in special for c in line.split(": ", 1)[1].split(",")})
                notes.append(f"NOTOC {len(special)} special load(s): {', '.join(codes)}")
        if flight.manifest is not None and flight.manifest.passenger_count > ac.passenger_capacity:
            issues.append("Passengers exceed seat capacity")
        # Weather
        dest_metar = self.weather.get(flight.destination.icao)
        orig_metar = self.weather.get(flight.origin.icao)
        for metar, apt in ((orig_metar, flight.origin), (dest_metar, flight.destination)):
            if metar is None:
                notes.append(f"No current weather for {apt.icao}")
                continue
            if metar.hazardous:
                notes.append(f"{apt.icao} hazardous weather: {' '.join(metar.phenomena)}")
            xw = crosswind_check(ac.type, apt, metar.wind)
            if xw:
                issues.append(xw)
        if dest_metar and dest_metar.flight_category in (FlightCategory.IFR, FlightCategory.LIFR) \
                and flight.alternate is None:
            issues.append(f"Destination {dest_metar.flight_category.value} - an alternate is required")
        if dest_metar and dest_metar.flight_category is FlightCategory.LIFR:
            if dest_metar.visibility_m < 550 or (dest_metar.ceiling_ft or 9999) < 200:
                issues.append(f"Destination below CAT I minima (vis {dest_metar.visibility_m:.0f} m)")
        if flight.alternate:
            alt_metar = self.weather.get(flight.alternate.icao)
            if alt_metar and alt_metar.flight_category is FlightCategory.LIFR:
                issues.append(f"Alternate {flight.alternate.icao} below planning minima")
        if release.approved:
            flight.status = FlightStatus.DISPATCHED
            flight.log(now, "DISPATCHED", f"{len(notes)} advisory note(s)")
        else:
            flight.log(now, "DISPATCH REFUSED", f"{len(issues)} issue(s)")
        return release

    # ------------------------------------------------------- flight following
    def advance(self, now: datetime) -> list[str]:
        """Progress every dispatched / active flight to ``now``; returns new event lines."""
        messages: list[str] = []
        for flight in sorted(self.flights.values(), key=lambda f: f.estimated_departure):
            before = len(flight.events)
            self._advance_flight(flight, now)
            messages += [f"{flight.flight_number}: {e}" for e in flight.events[before:]]
        return messages

    def _advance_flight(self, flight: Flight, now: datetime) -> None:
        if flight.status in (FlightStatus.SCHEDULED, FlightStatus.CANCELLED, FlightStatus.ARRIVED):
            return
        ac = flight.aircraft
        etd = flight.estimated_departure
        if flight.status in (FlightStatus.DISPATCHED, FlightStatus.BOARDING):
            boarding = etd - timedelta(minutes=40)
            if flight.status is FlightStatus.DISPATCHED and flight.manifest is not None and now >= boarding:
                flight.start_boarding(boarding)
                for p in flight.manifest.passengers:
                    if p.status is PassengerStatus.CHECKED_IN:
                        flight.manifest.board(p)
            if now >= etd:
                flight.push_back(etd)
                ac.status = AircraftStatus.IN_FLIGHT
        if flight.status is FlightStatus.DEPARTED:
            t_off = flight.off_block + timedelta(minutes=TAXI_OUT_MIN)
            if now >= t_off:
                flight.take_off(t_off)
        if flight.status in (FlightStatus.AIRBORNE, FlightStatus.DIVERTED) and flight.landing_time is None:
            if flight.status is FlightStatus.DIVERTED:
                eta = flight.diversion_eta
                if now >= eta:
                    flight.land(eta)
            else:
                p = flight.plan.profile
                t0 = flight.takeoff_time
                top_of_descent = p.climb_min + p.cruise_min
                marks = [(FlightPhase.CLIMB, 0.0), (FlightPhase.CRUISE, p.climb_min),
                         (FlightPhase.DESCENT, top_of_descent),
                         (FlightPhase.APPROACH, max(top_of_descent, p.airborne_min - 8))]
                for phase, offset in marks:
                    at = t0 + timedelta(minutes=offset)
                    if now >= at and _PHASE_ORDER.index(flight.phase) < _PHASE_ORDER.index(phase):
                        flight.set_phase(phase, at)
                touchdown = t0 + timedelta(minutes=p.airborne_min)
                if now >= touchdown:
                    flight.land(touchdown)
        if flight.status in (FlightStatus.LANDED, FlightStatus.DIVERTED) and flight.landing_time:
            t_on = flight.landing_time + timedelta(minutes=TAXI_IN_MIN)
            if now >= t_on and flight.on_block is None:
                flight.arrive(t_on)
                self._complete_flight(flight)

    def _complete_flight(self, flight: Flight) -> None:
        ac = flight.aircraft
        ac.location = flight.arrival_airport.icao
        ac.status = AircraftStatus.AVAILABLE
        block = flight.actual_block_hours or flight.scheduled_block_hours
        ac.record_flight(block)
        self.maintenance[ac.registration]._update_status(flight.on_block)
        for member in flight.crew:
            member.log_duty(DutyRecord(flight.flight_number, flight.off_block - REPORT_BEFORE_DEPARTURE,
                                       flight.on_block + RELEASE_AFTER_ARRIVAL, block,
                                       flight.arrival_airport.icao))

    def divert(self, flight: Flight, now: datetime, reason: str,
               airport: Optional[str] = None, use_alternate: bool = False) -> Airport:
        """Divert an airborne flight to ``airport``, its planned alternate
        (``use_alternate``, e.g. for destination weather) or otherwise the
        nearest airport with enough runway (e.g. a medical emergency)."""
        self.advance(now)
        if flight.status is not FlightStatus.AIRBORNE:
            raise ValueError("Only airborne flights can divert")
        state = flight.state_at(now)
        if airport:
            target = self.airports.get(airport)
        elif use_alternate:
            if flight.alternate is None:
                raise ValueError(f"{flight.flight_number} has no planned alternate")
            target = flight.alternate
        else:
            need = landing_distance_required(flight.aircraft.type, flight.plan.weights.lw_kg, flight.destination)
            candidates = self.airports.nearest(state.position, min_runway_m=need, exclude=[flight.destination.icao])
            if not candidates:
                raise ValueError("No suitable diversion airport")
            target = candidates[0][0]
        flight.divert(target, reason, now)
        if flight.phase is FlightPhase.TAKEOFF:
            flight.set_phase(FlightPhase.CLIMB, now)
        if flight.phase in (FlightPhase.CLIMB, FlightPhase.CRUISE):
            flight.set_phase(FlightPhase.DESCENT, now)
        return target

    # ---------------------------------------------------------------- reports
    def departures(self, airport: str, start: datetime, end: datetime) -> list[Flight]:
        icao = self.airports.get(airport).icao
        return sorted((f for f in self.flights.values()
                       if f.origin.icao == icao and start <= f.estimated_departure < end),
                      key=lambda f: f.estimated_departure)

    def arrivals(self, airport: str, start: datetime, end: datetime) -> list[Flight]:
        icao = self.airports.get(airport).icao
        return sorted((f for f in self.flights.values()
                       if f.arrival_airport.icao == icao and start <= f.estimated_arrival < end),
                      key=lambda f: f.estimated_arrival)

    def on_time_performance(self, tolerance_min: int = 15) -> Optional[float]:
        done = [f for f in self.flights.values() if f.on_block]
        if not done:
            return None
        on_time = sum(1 for f in done
                      if f.on_block <= f.scheduled_arrival + timedelta(minutes=tolerance_min))
        return on_time / len(done)

    def utilization(self, start: datetime, end: datetime) -> dict[str, float]:
        """Scheduled block hours per day for each aircraft in the window."""
        days = max((end - start).total_seconds() / 86400, 1e-9)
        out = {}
        for reg in self.fleet:
            hours = sum(f.scheduled_block_hours for f in self.flights_for_aircraft(reg)
                        if f.status is not FlightStatus.CANCELLED and start <= f.scheduled_departure < end)
            out[reg] = hours / days
        return out

    def fleet_report(self, now: datetime) -> str:
        lines = ["FLEET STATUS"]
        for ac in sorted(self.fleet.values(), key=lambda a: (a.category.value, a.registration)):
            tracker = self.maintenance[ac.registration]
            nxt = []
            for check in tracker.program:
                rem = tracker.remaining(check, now)
                if "flight_hours" in rem:
                    nxt.append(f"{check.name} {rem['flight_hours']:.0f}FH")
                elif "days" in rem:
                    nxt.append(f"{check.name} {rem['days']:.0f}d")
            defects = len(tracker.open_defects())
            lines.append(f"  {ac.describe()}  next: {', '.join(nxt[:2])}"
                         + (f"  open defects: {defects}" if defects else ""))
        return "\n".join(lines)

