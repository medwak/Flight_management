"""Passenger bookings, check-in, seating and boarding."""

from __future__ import annotations

import itertools
import string
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .aircraft import Aircraft, CabinClass, CommercialAircraft


class PassengerType(Enum):
    ADULT = "ADT"
    CHILD = "CHD"
    INFANT = "INF"  # lap infant, no seat


class PassengerStatus(Enum):
    BOOKED = "booked"
    CHECKED_IN = "checked in"
    BOARDED = "boarded"
    OFFLOADED = "offloaded"
    NO_SHOW = "no show"


# EASA standard masses (kg) including hand baggage.
STANDARD_MASS_KG = {PassengerType.ADULT: 84.0, PassengerType.CHILD: 35.0, PassengerType.INFANT: 0.0}
STANDARD_BAG_KG = 15.0


@dataclass
class Passenger:
    first_name: str
    last_name: str
    pax_type: PassengerType = PassengerType.ADULT
    cabin: CabinClass = CabinClass.ECONOMY
    pnr: str = ""
    seat: Optional[str] = None
    checked_bags: int = 0
    status: PassengerStatus = PassengerStatus.BOOKED
    special_service: set[str] = field(default_factory=set)  # SSR codes e.g. WCHR, VGML

    @property
    def full_name(self) -> str:
        return f"{self.last_name.upper()}/{self.first_name.upper()}"

    @property
    def mass_kg(self) -> float:
        return STANDARD_MASS_KG[self.pax_type]

    @property
    def baggage_kg(self) -> float:
        return self.checked_bags * STANDARD_BAG_KG

    @property
    def needs_seat(self) -> bool:
        return self.pax_type is not PassengerType.INFANT


_pnr_counter = itertools.count(1)


def _new_pnr() -> str:
    n = next(_pnr_counter)
    chars = string.ascii_uppercase + string.digits
    code = ""
    for _ in range(6):
        n, r = divmod(n * 7919 + 13, len(chars))
        code += chars[r]
    return code


class SeatMap:
    """Generates seat identifiers for each cabin and tracks occupancy."""

    LAYOUT = {
        CabinClass.FIRST: "ADGK",
        CabinClass.BUSINESS: "ACDGHK",
        CabinClass.PREMIUM_ECONOMY: "ABCDEFG",
        CabinClass.ECONOMY: "ABCDEF",
    }

    def __init__(self, cabin_config: dict[CabinClass, int]):
        self.seats: dict[CabinClass, list[str]] = {}
        row = 1
        for cabin in CabinClass:
            count = cabin_config.get(cabin, 0)
            letters = self.LAYOUT[cabin]
            seats: list[str] = []
            while len(seats) < count:
                for letter in letters:
                    if len(seats) < count:
                        seats.append(f"{row}{letter}")
                row += 1
            self.seats[cabin] = seats
        self.occupied: dict[str, Passenger] = {}

    def free_seats(self, cabin: CabinClass) -> list[str]:
        return [s for s in self.seats.get(cabin, []) if s not in self.occupied]

    def assign(self, passenger: Passenger, seat: Optional[str] = None) -> str:
        free = self.free_seats(passenger.cabin)
        if seat is None:
            if not free:
                raise ValueError(f"No free seats in {passenger.cabin.name}")
            seat = free[0]
        elif seat not in free:
            raise ValueError(f"Seat {seat} is not available in {passenger.cabin.name}")
        if passenger.seat:
            self.occupied.pop(passenger.seat, None)
        self.occupied[seat] = passenger
        passenger.seat = seat
        return seat

    def release(self, passenger: Passenger) -> None:
        if passenger.seat:
            self.occupied.pop(passenger.seat, None)
            passenger.seat = None


class PassengerManifest:
    """Bookings and departure-control for one flight."""

    def __init__(self, aircraft: Aircraft, overbooking_factor: float = 1.0):
        if isinstance(aircraft, CommercialAircraft):
            config = dict(aircraft.cabin_config)
        else:
            config = {CabinClass.BUSINESS: aircraft.passenger_capacity}
        self.capacity = config
        self.overbooking_factor = overbooking_factor
        self.seat_map = SeatMap(config)
        self.passengers: list[Passenger] = []

    # -- reservations ---------------------------------------------------------
    def booked(self, cabin: CabinClass) -> int:
        return sum(1 for p in self.passengers
                   if p.cabin is cabin and p.needs_seat
                   and p.status not in (PassengerStatus.OFFLOADED, PassengerStatus.NO_SHOW))

    def book(self, *passengers: Passenger) -> str:
        """Book one or more passengers under a single PNR."""
        pnr = _new_pnr()
        by_cabin: dict[CabinClass, int] = {}
        offered = [c for c, n in self.capacity.items() if n]
        for p in passengers:
            if len(offered) == 1:
                p.cabin = offered[0]  # single-cabin aircraft (business jets, freighters)
            if p.cabin not in self.capacity or self.capacity[p.cabin] == 0:
                raise ValueError(f"Cabin {p.cabin.name} not offered on this aircraft")
            if p.needs_seat:
                by_cabin[p.cabin] = by_cabin.get(p.cabin, 0) + 1
        adults = sum(1 for p in passengers if p.pax_type is PassengerType.ADULT)
        infants = sum(1 for p in passengers if p.pax_type is PassengerType.INFANT)
        if infants > adults:
            raise ValueError("Each infant must travel with an adult")
        for cabin, n in by_cabin.items():
            limit = int(self.capacity[cabin] * self.overbooking_factor)
            if self.booked(cabin) + n > limit:
                raise ValueError(f"{cabin.name} cabin is full")
        for p in passengers:
            p.pnr = pnr
            self.passengers.append(p)
        return pnr

    def find(self, pnr: str) -> list[Passenger]:
        return [p for p in self.passengers if p.pnr == pnr]

    # -- departure control ---------------------------------------------------
    def check_in(self, passenger: Passenger, seat: Optional[str] = None, bags: int = 0) -> str:
        if passenger.status is not PassengerStatus.BOOKED:
            raise ValueError(f"{passenger.full_name} cannot check in from status {passenger.status.value}")
        passenger.checked_bags = bags
        if passenger.needs_seat:
            self.seat_map.assign(passenger, seat)
        passenger.status = PassengerStatus.CHECKED_IN
        return passenger.seat or "INF"

    def board(self, passenger: Passenger) -> None:
        if passenger.status is not PassengerStatus.CHECKED_IN:
            raise ValueError(f"{passenger.full_name} is not checked in")
        passenger.status = PassengerStatus.BOARDED

    def close_flight(self) -> list[Passenger]:
        """Mark everyone not boarded as no-show/offloaded; returns bags to offload."""
        offload = []
        for p in self.passengers:
            if p.status is PassengerStatus.BOOKED:
                p.status = PassengerStatus.NO_SHOW
            elif p.status is PassengerStatus.CHECKED_IN:
                p.status = PassengerStatus.OFFLOADED
                self.seat_map.release(p)
                if p.checked_bags:
                    offload.append(p)
        return offload

    # -- load figures ---------------------------------------------------------
    def _counted(self) -> list[Passenger]:
        return [p for p in self.passengers
                if p.status in (PassengerStatus.BOOKED, PassengerStatus.CHECKED_IN, PassengerStatus.BOARDED)]

    @property
    def passenger_count(self) -> int:
        return sum(1 for p in self._counted() if p.needs_seat)

    @property
    def infant_count(self) -> int:
        return sum(1 for p in self._counted() if not p.needs_seat)

    @property
    def passenger_mass_kg(self) -> float:
        return sum(p.mass_kg for p in self._counted())

    @property
    def baggage_mass_kg(self) -> float:
        return sum(p.baggage_kg for p in self._counted())

    @property
    def load_factor(self) -> float:
        seats = sum(self.capacity.values())
        return self.passenger_count / seats if seats else 0.0
