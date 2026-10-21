"""Aircraft types and the airframes that make up a fleet.

Three operational categories are supported:

* ``CommercialAircraft`` - scheduled passenger airliners with multi-class cabins
  and belly cargo.
* ``BusinessJet`` - private / corporate jets with a VIP cabin.
* ``CargoAircraft`` - freighters with main-deck ULD positions and
  dangerous-goods (cargo-aircraft-only) capability.

Performance figures are representative published values, rounded; they are
good enough for planning simulations but are not certified data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class AircraftCategory(Enum):
    COMMERCIAL = "commercial"
    BUSINESS = "business"
    CARGO = "cargo"


class AircraftStatus(Enum):
    AVAILABLE = "available"
    IN_FLIGHT = "in flight"
    MAINTENANCE = "in maintenance"
    AOG = "aircraft on ground"  # unserviceable
    RETIRED = "retired"


class CabinClass(Enum):
    FIRST = "F"
    BUSINESS = "J"
    PREMIUM_ECONOMY = "W"
    ECONOMY = "Y"


@dataclass(frozen=True)
class AircraftType:
    icao_code: str
    manufacturer: str
    model: str
    category: AircraftCategory
    max_takeoff_weight_kg: float
    max_landing_weight_kg: float
    max_zero_fuel_weight_kg: float
    operating_empty_weight_kg: float
    max_fuel_kg: float
    cruise_tas_kt: float
    cruise_mach: float
    service_ceiling_ft: int
    range_nm: float
    cruise_fuel_burn_kg_h: float
    takeoff_distance_m: float  # at MTOW, sea level, ISA
    landing_distance_m: float  # at MLW, sea level, dry
    length_m: float
    max_crosswind_kt: float = 33.0
    climb_rate_fpm: float = 2000.0
    descent_rate_fpm: float = 2000.0
    taxi_fuel_kg_min: float = 15.0
    min_cockpit_crew: int = 2
    cargo_volume_m3: float = 0.0
    engines: int = 2

    @property
    def max_payload_kg(self) -> float:
        return self.max_zero_fuel_weight_kg - self.operating_empty_weight_kg

    @property
    def holding_fuel_burn_kg_h(self) -> float:
        return self.cruise_fuel_burn_kg_h * 0.8

    # --- Simplified balance geometry (metres from the nose datum) ---------
    @property
    def mac_m(self) -> float:
        """Mean aerodynamic chord length."""
        return self.length_m * 0.10

    @property
    def lemac_m(self) -> float:
        """Leading edge of MAC; placed so the fuselage mid-point is 25% MAC."""
        return self.length_m * 0.5 - 0.25 * self.mac_m

    @property
    def payload_zone_m(self) -> tuple[float, float]:
        """Fore and aft limits of the cabin / cargo deck."""
        return self.length_m * 0.15, self.length_m * 0.85

    def arm_to_mac(self, arm_m: float) -> float:
        return (arm_m - self.lemac_m) / self.mac_m * 100.0

    def mac_to_arm(self, percent_mac: float) -> float:
        return self.lemac_m + percent_mac / 100.0 * self.mac_m

    cg_forward_limit_mac: float = 8.0
    cg_aft_limit_mac: float = 38.0
    empty_cg_mac: float = 25.0
    fuel_cg_mac: float = 27.0

    def __str__(self) -> str:
        return f"{self.manufacturer} {self.model} ({self.icao_code})"


@dataclass
class Aircraft:
    """A single airframe."""

    registration: str
    type: AircraftType
    base: str = ""  # ICAO code of home base
    location: str = ""  # ICAO code where the aircraft currently is
    status: AircraftStatus = AircraftStatus.AVAILABLE
    total_flight_hours: float = 0.0
    total_cycles: int = 0
    operator: str = ""

    def __post_init__(self) -> None:
        if not self.location:
            self.location = self.base

    @property
    def category(self) -> AircraftCategory:
        return self.type.category

    @property
    def passenger_capacity(self) -> int:
        return 0

    @property
    def is_serviceable(self) -> bool:
        return self.status in (AircraftStatus.AVAILABLE, AircraftStatus.IN_FLIGHT)

    def record_flight(self, block_hours: float, cycles: int = 1) -> None:
        self.total_flight_hours += block_hours
        self.total_cycles += cycles

    def describe(self) -> str:
        return (f"{self.registration} {self.type} [{self.category.value}] "
                f"at {self.location or '-'} - {self.status.value}, "
                f"{self.total_flight_hours:,.0f} FH / {self.total_cycles:,} cycles")


@dataclass
class CommercialAircraft(Aircraft):
    cabin_config: dict[CabinClass, int] = field(default_factory=dict)
    belly_cargo_capacity_kg: float = 0.0

    @property
    def passenger_capacity(self) -> int:
        return sum(self.cabin_config.values())

    @property
    def required_cabin_crew(self) -> int:
        """ICAO/EASA: one cabin crew member per 50 installed seats (rounded up)."""
        return -(-self.passenger_capacity // 50)


@dataclass
class BusinessJet(Aircraft):
    seats: int = 8
    berths: int = 0
    amenities: list[str] = field(default_factory=list)

    @property
    def passenger_capacity(self) -> int:
        return self.seats

    @property
    def required_cabin_crew(self) -> int:
        # A flight attendant is mandatory with 20+ seats; optional otherwise.
        return 1 if self.seats >= 20 else 0


@dataclass(frozen=True)
class UldPosition:
    """A loading position on a cargo deck."""

    code: str  # e.g. "A1", "P11", "41L"
    deck: str  # "main" or "lower"
    arm_m: float  # balance arm from nose datum
    max_weight_kg: float
    accepts: tuple[str, ...] = ("PMC", "PAG", "PLA", "AKE", "PAJ", "AMJ")


@dataclass
class CargoAircraft(Aircraft):
    positions: list[UldPosition] = field(default_factory=list)
    dangerous_goods_certified: bool = True
    temperature_controlled: bool = True
    supernumerary_seats: int = 2  # couriers / animal handlers

    @property
    def passenger_capacity(self) -> int:
        return self.supernumerary_seats

    @property
    def required_cabin_crew(self) -> int:
        return 0

    def position(self, code: str) -> UldPosition:
        for pos in self.positions:
            if pos.code == code:
                return pos
        raise KeyError(f"{self.registration} has no position {code}")


# ---------------------------------------------------------------------------
# Type catalogue
# ---------------------------------------------------------------------------
C, B, F = AircraftCategory.COMMERCIAL, AircraftCategory.BUSINESS, AircraftCategory.CARGO

AIRCRAFT_TYPES: dict[str, AircraftType] = {t.icao_code: t for t in [
    # Commercial airliners
    AircraftType("A20N", "Airbus", "A320neo", C, 79000, 67400, 64300, 44300, 18700,
                 450, 0.78, 39800, 3400, 2400, 2100, 1500, 37.6, 38, cargo_volume_m3=37.4),
    AircraftType("B38M", "Boeing", "737 MAX 8", C, 82190, 69310, 65952, 45070, 20730,
                 453, 0.79, 41000, 3550, 2300, 2500, 1600, 39.5, 33, cargo_volume_m3=44.0),
    AircraftType("B788", "Boeing", "787-8", C, 227930, 172365, 161025, 119950, 101300,
                 488, 0.85, 43100, 7355, 5000, 2600, 1700, 56.7, 38, cargo_volume_m3=137.0,
                 taxi_fuel_kg_min=25),
    AircraftType("B77W", "Boeing", "777-300ER", C, 351534, 251290, 237683, 167829, 145538,
                 490, 0.84, 43100, 7370, 7500, 3050, 2000, 73.9, 38, cargo_volume_m3=201.6,
                 taxi_fuel_kg_min=35),
    AircraftType("A359", "Airbus", "A350-900", C, 283000, 207000, 195700, 142400, 110523,
                 488, 0.85, 43100, 8100, 5800, 2600, 2000, 66.8, 35, cargo_volume_m3=172.4,
                 taxi_fuel_kg_min=28),
    # Business jets
    AircraftType("E55P", "Embraer", "Phenom 300E", B, 8415, 7650, 6400, 5360, 2428,
                 453, 0.78, 45000, 2010, 420, 978, 677, 15.9, 26, climb_rate_fpm=3000,
                 taxi_fuel_kg_min=3, cargo_volume_m3=2.2),
    AircraftType("CL35", "Bombardier", "Challenger 350", B, 18416, 15336, 12500, 10900, 6577,
                 459, 0.80, 45000, 3200, 700, 1468, 750, 20.9, 24, climb_rate_fpm=3500,
                 taxi_fuel_kg_min=5, cargo_volume_m3=3.0),
    AircraftType("GLF6", "Gulfstream", "G650ER", B, 47650, 37875, 27500, 24600, 21200,
                 488, 0.85, 51000, 7500, 1500, 1786, 1000, 30.4, 28, climb_rate_fpm=3500,
                 taxi_fuel_kg_min=8, cargo_volume_m3=5.5),
    AircraftType("GA7C", "Gulfstream", "G700", B, 49641, 38420, 28800, 25600, 22300,
                 488, 0.85, 51000, 7750, 1550, 1905, 1000, 33.5, 28, climb_rate_fpm=3500,
                 taxi_fuel_kg_min=8, cargo_volume_m3=5.4),
    # Freighters
    AircraftType("B763", "Boeing", "767-300F", F, 186880, 147870, 140160, 86183, 73000,
                 459, 0.80, 43100, 3255, 4800, 2700, 1800, 54.9, 33, cargo_volume_m3=438.0,
                 taxi_fuel_kg_min=20),
    AircraftType("B77L", "Boeing", "777F", F, 347815, 260816, 248115, 144379, 145538,
                 490, 0.84, 43100, 4970, 7800, 2830, 2000, 63.7, 38, cargo_volume_m3=653.0,
                 taxi_fuel_kg_min=35),
    AircraftType("B748", "Boeing", "747-8F", F, 447700, 346090, 329890, 197131, 182000,
                 493, 0.85, 43100, 4120, 10000, 3100, 2200, 76.3, 30, cargo_volume_m3=858.0,
                 taxi_fuel_kg_min=45, engines=4),
]}


def get_type(icao_code: str) -> AircraftType:
    try:
        return AIRCRAFT_TYPES[icao_code.upper()]
    except KeyError as exc:
        raise KeyError(f"Unknown aircraft type {icao_code}; known: "
                       f"{', '.join(sorted(AIRCRAFT_TYPES))}") from exc


def generate_uld_positions(ac_type: AircraftType, main_deck: int, lower_deck: int,
                           main_max_kg: Optional[float] = None,
                           lower_max_kg: float = 1588.0) -> list[UldPosition]:
    """Lay out evenly spaced main- and lower-deck positions along the payload zone."""
    fwd, aft = ac_type.payload_zone_m
    positions: list[UldPosition] = []
    if main_max_kg is None:
        main_max_kg = min(6804.0, ac_type.max_payload_kg / max(main_deck, 1) * 1.5)
    for i in range(main_deck):
        arm = fwd + (aft - fwd) * (i + 0.5) / main_deck
        positions.append(UldPosition(f"M{i + 1:02d}", "main", round(arm, 2), main_max_kg,
                                     ("PMC", "PAG", "PLA", "PAJ", "AMJ", "PGA")))
    for i in range(lower_deck):
        arm = fwd + (aft - fwd) * (i + 0.5) / lower_deck
        positions.append(UldPosition(f"L{i + 1:02d}", "lower", round(arm, 2), lower_max_kg,
                                     ("AKE", "AKH", "PMC", "PAG")))
    return positions


def build_aircraft(registration: str, type_code: str, base: str = "", **kwargs) -> Aircraft:
    """Factory that returns the right ``Aircraft`` subclass for a type code."""
    ac_type = get_type(type_code)
    if ac_type.category is AircraftCategory.COMMERCIAL:
        kwargs.setdefault("cabin_config", _default_cabin(ac_type))
        kwargs.setdefault("belly_cargo_capacity_kg", round(ac_type.cargo_volume_m3 * 160))
        return CommercialAircraft(registration, ac_type, base, **kwargs)
    if ac_type.category is AircraftCategory.BUSINESS:
        kwargs.setdefault("seats", {"E55P": 7, "CL35": 9, "GLF6": 14, "GA7C": 16}.get(ac_type.icao_code, 8))
        return BusinessJet(registration, ac_type, base, **kwargs)
    if "positions" not in kwargs:
        main = max(10, int(ac_type.cargo_volume_m3 / 22))
        lower = max(6, int(ac_type.cargo_volume_m3 / 60))
        kwargs["positions"] = generate_uld_positions(ac_type, main, lower)
    return CargoAircraft(registration, ac_type, base, **kwargs)


def _default_cabin(ac_type: AircraftType) -> dict[CabinClass, int]:
    return {
        "A20N": {CabinClass.BUSINESS: 12, CabinClass.ECONOMY: 156},
        "B38M": {CabinClass.BUSINESS: 16, CabinClass.ECONOMY: 156},
        "B788": {CabinClass.BUSINESS: 30, CabinClass.ECONOMY: 204},
        "B77W": {CabinClass.FIRST: 8, CabinClass.BUSINESS: 42,
                 CabinClass.PREMIUM_ECONOMY: 40, CabinClass.ECONOMY: 264},
        "A359": {CabinClass.BUSINESS: 42, CabinClass.PREMIUM_ECONOMY: 24,
                 CabinClass.ECONOMY: 259},
    }.get(ac_type.icao_code, {CabinClass.ECONOMY: 150})
