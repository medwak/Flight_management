"""Crew records, qualifications and flight-time-limitation (FTL) legality.

Limits follow a simplified version of EASA ORO.FTL:
* max basic flight duty period 13 h (extendable to 17 h with augmented crew),
* minimum rest at least as long as the preceding duty, and never under 12 h,
* 100 flight hours in any 28 days and 900 in a calendar year.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Iterable


class CrewRole(Enum):
    CAPTAIN = "CPT"
    FIRST_OFFICER = "FO"
    RELIEF_PILOT = "RP"
    PURSER = "PUR"
    CABIN_CREW = "CC"
    LOADMASTER = "LM"

    @property
    def is_pilot(self) -> bool:
        return self in (CrewRole.CAPTAIN, CrewRole.FIRST_OFFICER, CrewRole.RELIEF_PILOT)

    @property
    def is_cabin(self) -> bool:
        return self in (CrewRole.PURSER, CrewRole.CABIN_CREW)


MAX_FDP_HOURS = 13.0
MAX_FDP_AUGMENTED_HOURS = 17.0
MIN_REST_HOURS = 12.0
MAX_FLIGHT_HOURS_28D = 100.0
MAX_FLIGHT_HOURS_YEAR = 900.0
REPORT_BEFORE_DEPARTURE = timedelta(minutes=60)
RELEASE_AFTER_ARRIVAL = timedelta(minutes=30)


@dataclass
class DutyRecord:
    flight_number: str
    report: datetime
    release: datetime
    block_hours: float
    end_airport: str = ""

    @property
    def duty_hours(self) -> float:
        return (self.release - self.report).total_seconds() / 3600.0


@dataclass
class CrewMember:
    employee_id: str
    name: str
    role: CrewRole
    base: str = ""
    type_ratings: set[str] = field(default_factory=set)  # aircraft ICAO type codes
    license_expiry: date = date.max
    medical_expiry: date = date.max
    duties: list[DutyRecord] = field(default_factory=list)

    def is_qualified_for(self, type_code: str) -> bool:
        if self.role.is_pilot:
            return type_code in self.type_ratings
        # Cabin crew and loadmasters need a type familiarisation entry too,
        # but "*" means qualified on all types.
        return "*" in self.type_ratings or type_code in self.type_ratings

    def documents_valid(self, on: date) -> bool:
        return on <= self.license_expiry and on <= self.medical_expiry

    def flight_hours_between(self, start: datetime, end: datetime) -> float:
        return sum(d.block_hours for d in self.duties if start <= d.report < end)

    def last_release(self, before: datetime) -> datetime | None:
        releases = [d.release for d in self.duties if d.release <= before]
        return max(releases) if releases else None

    def last_duty_before(self, before: datetime) -> DutyRecord | None:
        past = [d for d in self.duties if d.release <= before]
        return max(past, key=lambda d: d.release) if past else None

    def location_at(self, time: datetime) -> str:
        """Airport where the crew member is (last duty's arrival, else home base)."""
        prev = self.last_duty_before(time)
        return prev.end_airport if prev and prev.end_airport else self.base

    def overlaps(self, report: datetime, release: datetime) -> bool:
        return any(d.report < release and report < d.release for d in self.duties)

    def legality_issues(self, type_code: str, report: datetime, release: datetime,
                        block_hours: float, augmented: bool = False) -> list[str]:
        """Return a list of reasons why this crew member may not operate the duty."""
        issues = []
        if not self.is_qualified_for(type_code):
            issues.append(f"{self.name}: not type-rated on {type_code}")
        if not self.documents_valid(report.date()):
            issues.append(f"{self.name}: licence or medical expired")
        if self.overlaps(report, release):
            issues.append(f"{self.name}: overlapping duty")
        duty = (release - report).total_seconds() / 3600.0
        max_fdp = MAX_FDP_AUGMENTED_HOURS if augmented else MAX_FDP_HOURS
        if duty > max_fdp:
            issues.append(f"{self.name}: FDP {duty:.1f}h exceeds {max_fdp:.0f}h limit")
        prev = self.last_duty_before(report)
        if prev is not None:
            rest = (report - prev.release).total_seconds() / 3600.0
            required = max(MIN_REST_HOURS, prev.duty_hours)
            if rest < required:
                issues.append(f"{self.name}: rest {rest:.1f}h below required {required:.1f}h")
        hours_28 = self.flight_hours_between(report - timedelta(days=28), report) + block_hours
        if hours_28 > MAX_FLIGHT_HOURS_28D:
            issues.append(f"{self.name}: {hours_28:.0f} flight hours in 28 days exceeds limit")
        year_start = datetime(report.year, 1, 1, tzinfo=report.tzinfo)
        hours_year = self.flight_hours_between(year_start, report) + block_hours
        if hours_year > MAX_FLIGHT_HOURS_YEAR:
            issues.append(f"{self.name}: {hours_year:.0f} flight hours this year exceeds limit")
        return issues

    def log_duty(self, record: DutyRecord) -> None:
        self.duties.append(record)


@dataclass
class CrewRequirement:
    pilots: int
    cabin_crew: int
    loadmasters: int = 0
    augmented: bool = False

    def describe(self) -> str:
        parts = [f"{self.pilots} pilots"]
        if self.cabin_crew:
            parts.append(f"{self.cabin_crew} cabin crew")
        if self.loadmasters:
            parts.append(f"{self.loadmasters} loadmaster")
        if self.augmented:
            parts.append("augmented")
        return ", ".join(parts)


def crew_complement_issues(crew: Iterable[CrewMember], requirement: CrewRequirement) -> list[str]:
    crew = list(crew)
    issues = []
    captains = sum(1 for c in crew if c.role is CrewRole.CAPTAIN)
    pilots = sum(1 for c in crew if c.role.is_pilot)
    cabin = sum(1 for c in crew if c.role.is_cabin)
    loadmasters = sum(1 for c in crew if c.role is CrewRole.LOADMASTER)
    if captains < 1:
        issues.append("No captain assigned")
    if pilots < requirement.pilots:
        issues.append(f"{pilots}/{requirement.pilots} pilots assigned")
    if cabin < requirement.cabin_crew:
        issues.append(f"{cabin}/{requirement.cabin_crew} cabin crew assigned")
    if loadmasters < requirement.loadmasters:
        issues.append(f"{loadmasters}/{requirement.loadmasters} loadmasters assigned")
    return issues
