"""Continuing airworthiness: scheduled checks, defects and the MEL."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

from .aircraft import Aircraft, AircraftCategory, AircraftStatus


@dataclass(frozen=True)
class CheckInterval:
    name: str
    flight_hours: Optional[float] = None
    cycles: Optional[int] = None
    days: Optional[int] = None
    downtime_hours: float = 8.0


# Representative check programmes per category ("whichever comes first").
MAINTENANCE_PROGRAMS: dict[AircraftCategory, list[CheckInterval]] = {
    AircraftCategory.COMMERCIAL: [
        CheckInterval("Weekly", days=7, downtime_hours=2),
        CheckInterval("A-check", flight_hours=750, cycles=750, days=120, downtime_hours=10),
        CheckInterval("C-check", flight_hours=7500, cycles=5000, days=730, downtime_hours=24 * 14),
    ],
    AircraftCategory.CARGO: [
        CheckInterval("Weekly", days=7, downtime_hours=2),
        CheckInterval("A-check", flight_hours=600, cycles=400, days=120, downtime_hours=12),
        CheckInterval("C-check", flight_hours=6000, cycles=3000, days=730, downtime_hours=24 * 18),
    ],
    AircraftCategory.BUSINESS: [
        CheckInterval("Phase 1", flight_hours=300, days=365, downtime_hours=16),
        CheckInterval("Phase 2", flight_hours=600, days=730, downtime_hours=48),
        CheckInterval("Major", flight_hours=4800, days=365 * 6, downtime_hours=24 * 21),
    ],
}


class MelCategory(Enum):
    """MEL rectification interval categories."""

    A = "A"  # as specified in the remarks
    B = "B"  # 3 calendar days
    C = "C"  # 10 calendar days
    D = "D"  # 120 calendar days

    @property
    def interval(self) -> timedelta:
        return {MelCategory.A: timedelta(days=1), MelCategory.B: timedelta(days=3),
                MelCategory.C: timedelta(days=10), MelCategory.D: timedelta(days=120)}[self]


_defect_ids = itertools.count(1)


@dataclass
class Defect:
    description: str
    reported: datetime
    ata_chapter: str = ""
    mel_category: Optional[MelCategory] = None  # None => no-go item, must be fixed
    operational_restriction: str = ""
    max_altitude_ft: Optional[int] = None  # e.g. one air-conditioning pack inoperative
    defect_id: int = field(default_factory=lambda: next(_defect_ids))
    rectified: Optional[datetime] = None

    @property
    def deferred(self) -> bool:
        return self.mel_category is not None

    @property
    def open(self) -> bool:
        return self.rectified is None

    def due(self) -> Optional[datetime]:
        return self.reported + self.mel_category.interval if self.mel_category else None


@dataclass
class CheckRecord:
    check: str
    performed: datetime
    flight_hours: float
    cycles: int


class MaintenanceTracker:
    """Tracks scheduled checks and defects for an aircraft."""

    def __init__(self, aircraft: Aircraft, program: Optional[list[CheckInterval]] = None,
                 last_checks: Optional[dict[str, CheckRecord]] = None,
                 since: Optional[datetime] = None):
        self.aircraft = aircraft
        self.program = program or MAINTENANCE_PROGRAMS[aircraft.category]
        start = since or datetime.now().astimezone()
        self.history: list[CheckRecord] = []
        self.last: dict[str, CheckRecord] = {
            c.name: CheckRecord(c.name, start, aircraft.total_flight_hours, aircraft.total_cycles)
            for c in self.program
        }
        if last_checks:
            self.last.update(last_checks)
        self.defects: list[Defect] = []

    # -- scheduled checks -----------------------------------------------------
    def remaining(self, check: CheckInterval, now: datetime) -> dict[str, float]:
        last = self.last[check.name]
        out: dict[str, float] = {}
        if check.flight_hours is not None:
            out["flight_hours"] = check.flight_hours - (self.aircraft.total_flight_hours - last.flight_hours)
        if check.cycles is not None:
            out["cycles"] = check.cycles - (self.aircraft.total_cycles - last.cycles)
        if check.days is not None:
            out["days"] = check.days - (now - last.performed).total_seconds() / 86400.0
        return out

    def overdue_checks(self, now: datetime) -> list[str]:
        return [c.name for c in self.program if any(v < 0 for v in self.remaining(c, now).values())]

    def checks_due_within(self, now: datetime, flight_hours: float = 0, cycles: int = 0,
                          days: float = 0) -> list[str]:
        """Checks that would fall due if the aircraft flew the given amount."""
        due = []
        for c in self.program:
            rem = self.remaining(c, now)
            if (rem.get("flight_hours", 1e9) < flight_hours or rem.get("cycles", 1e9) < cycles
                    or rem.get("days", 1e9) < days):
                due.append(c.name)
        return due

    def perform_check(self, name: str, when: datetime) -> CheckRecord:
        check = next((c for c in self.program if c.name == name), None)
        if check is None:
            raise KeyError(f"No check named {name}")
        record = CheckRecord(name, when, self.aircraft.total_flight_hours, self.aircraft.total_cycles)
        self.history.append(record)
        self.last[name] = record
        # A heavier check also satisfies all lighter ones in the programme.
        idx = self.program.index(check)
        for lighter in self.program[:idx]:
            self.last[lighter.name] = CheckRecord(lighter.name, when, record.flight_hours, record.cycles)
        self._update_status(when)
        return record

    # -- defects ---------------------------------------------------------------
    def report_defect(self, defect: Defect) -> Defect:
        self.defects.append(defect)
        self._update_status(defect.reported)
        return defect

    def rectify(self, defect_id: int, when: datetime) -> None:
        for d in self.defects:
            if d.defect_id == defect_id:
                d.rectified = when
                self._update_status(when)
                return
        raise KeyError(f"No defect {defect_id}")

    def open_defects(self) -> list[Defect]:
        return [d for d in self.defects if d.open]

    def airworthiness_issues(self, now: datetime) -> list[str]:
        issues = [f"{name} overdue" for name in self.overdue_checks(now)]
        for d in self.open_defects():
            if not d.deferred:
                issues.append(f"Open no-go defect #{d.defect_id}: {d.description}")
            elif d.due() < now:
                issues.append(f"MEL item #{d.defect_id} ({d.mel_category.value}) expired: {d.description}")
        return issues

    def altitude_limit(self) -> Optional[int]:
        limits = [d.max_altitude_ft for d in self.open_defects() if d.max_altitude_ft]
        return min(limits) if limits else None

    def restrictions(self) -> list[str]:
        return [d.operational_restriction for d in self.open_defects()
                if d.deferred and d.operational_restriction]

    def _update_status(self, now: datetime) -> None:
        if self.aircraft.status in (AircraftStatus.IN_FLIGHT, AircraftStatus.RETIRED,
                                    AircraftStatus.MAINTENANCE):
            return
        if self.airworthiness_issues(now):
            self.aircraft.status = AircraftStatus.AOG
        elif self.aircraft.status is AircraftStatus.AOG:
            self.aircraft.status = AircraftStatus.AVAILABLE
