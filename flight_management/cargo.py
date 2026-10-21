"""Cargo handling: air waybills, ULDs, dangerous goods and load planning."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .aircraft import Aircraft, CargoAircraft, CommercialAircraft, UldPosition


class SpecialHandling(Enum):
    """IATA special handling codes (subset)."""

    PERISHABLE = "PER"
    LIVE_ANIMAL = "AVI"
    VALUABLE = "VAL"
    PHARMA = "PIL"
    HUMAN_REMAINS = "HUM"
    OVERSIZE = "BIG"
    HEAVY = "HEA"
    COOL = "COL"  # temperature controlled


# IATA DG class -> description
DG_CLASSES = {
    "1": "Explosives", "1.4S": "Explosives (1.4S)", "2.1": "Flammable gas",
    "2.2": "Non-flammable gas", "2.3": "Toxic gas", "3": "Flammable liquid",
    "4.1": "Flammable solid", "4.2": "Spontaneously combustible",
    "4.3": "Dangerous when wet", "5.1": "Oxidizer", "5.2": "Organic peroxide",
    "6.1": "Toxic", "6.2": "Infectious", "7": "Radioactive", "8": "Corrosive",
    "9": "Miscellaneous",
}

# Pairs that must not be loaded in the same ULD / adjacent positions
# (simplified from IATA DGR table 9.3.A).
_INCOMPATIBLE = {
    frozenset({"3", "5.1"}), frozenset({"2.1", "5.1"}), frozenset({"4.1", "5.1"}),
    frozenset({"4.2", "5.1"}), frozenset({"4.3", "8"}), frozenset({"3", "5.2"}),
    frozenset({"4.2", "5.2"}), frozenset({"2.1", "2.3"}),
}


def dg_compatible(class_a: str, class_b: str) -> bool:
    if class_a == class_b:
        return True
    # Explosives (except 1.4S) must be segregated from everything else.
    if "1" in (class_a, class_b):
        return False
    return frozenset({class_a, class_b}) not in _INCOMPATIBLE


@dataclass
class CargoItem:
    awb: str  # air waybill number, e.g. "176-12345675"
    description: str
    pieces: int
    weight_kg: float
    volume_m3: float
    origin: str
    destination: str
    shipper: str = ""
    consignee: str = ""
    dg_class: Optional[str] = None
    un_number: Optional[str] = None
    cargo_aircraft_only: bool = False
    special_handling: set[SpecialHandling] = field(default_factory=set)

    def __post_init__(self) -> None:
        if self.weight_kg <= 0 or self.volume_m3 <= 0 or self.pieces <= 0:
            raise ValueError("Cargo pieces, weight and volume must be positive")
        if self.dg_class is not None and self.dg_class not in DG_CLASSES:
            raise ValueError(f"Unknown dangerous goods class {self.dg_class}")

    @property
    def is_dangerous(self) -> bool:
        return self.dg_class is not None

    @property
    def chargeable_weight_kg(self) -> float:
        """Greater of actual and volumetric weight (IATA 1:6000 => 166.67 kg/m3)."""
        return max(self.weight_kg, self.volume_m3 * 166.67)


# ULD type -> (tare kg, max gross kg, usable volume m3)
ULD_SPECS = {
    "AKE": (82, 1588, 4.3),
    "AKH": (75, 1135, 3.6),
    "PMC": (110, 6804, 11.5),
    "PAG": (105, 6033, 10.8),
    "PLA": (90, 3174, 9.0),
    "PAJ": (120, 6804, 16.0),
    "AMJ": (360, 6804, 17.5),
    "PGA": (500, 13608, 23.0),
}


@dataclass
class Uld:
    uld_id: str  # e.g. "PMC12345AA"
    uld_type: str
    items: list[CargoItem] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.uld_type not in ULD_SPECS:
            raise ValueError(f"Unknown ULD type {self.uld_type}")

    @property
    def tare_kg(self) -> float:
        return ULD_SPECS[self.uld_type][0]

    @property
    def max_gross_kg(self) -> float:
        return ULD_SPECS[self.uld_type][1]

    @property
    def max_volume_m3(self) -> float:
        return ULD_SPECS[self.uld_type][2]

    @property
    def gross_weight_kg(self) -> float:
        return self.tare_kg + sum(i.weight_kg for i in self.items)

    @property
    def volume_used_m3(self) -> float:
        return sum(i.volume_m3 for i in self.items)

    @property
    def dg_classes(self) -> set[str]:
        return {i.dg_class for i in self.items if i.dg_class}

    def can_accept(self, item: CargoItem) -> tuple[bool, str]:
        if self.gross_weight_kg + item.weight_kg > self.max_gross_kg:
            return False, "exceeds ULD max gross weight"
        if self.volume_used_m3 + item.volume_m3 > self.max_volume_m3:
            return False, "exceeds ULD volume"
        if item.dg_class:
            for existing in self.dg_classes:
                if not dg_compatible(existing, item.dg_class):
                    return False, f"DG class {item.dg_class} incompatible with {existing}"
        if SpecialHandling.LIVE_ANIMAL in item.special_handling and self.dg_classes:
            return False, "live animals cannot share a ULD with dangerous goods"
        if item.dg_class and any(SpecialHandling.LIVE_ANIMAL in i.special_handling for i in self.items):
            return False, "dangerous goods cannot share a ULD with live animals"
        if item.dg_class == "7" and any(SpecialHandling.PERISHABLE in i.special_handling
                                        or SpecialHandling.LIVE_ANIMAL in i.special_handling
                                        for i in self.items):
            return False, "radioactive material must be separated from food and animals"
        return True, ""

    def add(self, item: CargoItem) -> None:
        ok, reason = self.can_accept(item)
        if not ok:
            raise ValueError(f"Cannot load {item.awb} into {self.uld_id}: {reason}")
        self.items.append(item)


@dataclass
class LoadedPosition:
    position: UldPosition
    uld: Uld


class LoadPlan:
    """Assigns ULDs to positions on a specific aircraft and validates the result."""

    def __init__(self, aircraft: Aircraft):
        if not isinstance(aircraft, (CargoAircraft, CommercialAircraft)):
            raise TypeError("Load plans apply to cargo and commercial aircraft")
        self.aircraft = aircraft
        self.loaded: dict[str, LoadedPosition] = {}
        self.bulk_kg: float = 0.0  # loose belly cargo on airliners

    @property
    def available_positions(self) -> list[UldPosition]:
        if isinstance(self.aircraft, CargoAircraft):
            return [p for p in self.aircraft.positions if p.code not in self.loaded]
        return []

    def assign(self, uld: Uld, position_code: str) -> None:
        if not isinstance(self.aircraft, CargoAircraft):
            raise TypeError("ULD positions are only modelled on freighters; use add_bulk()")
        pos = self.aircraft.position(position_code)
        if position_code in self.loaded:
            raise ValueError(f"Position {position_code} already occupied")
        if uld.uld_type not in pos.accepts:
            raise ValueError(f"{uld.uld_type} not accepted at {position_code}")
        if uld.gross_weight_kg > pos.max_weight_kg:
            raise ValueError(f"{uld.uld_id} ({uld.gross_weight_kg:.0f} kg) exceeds "
                             f"{position_code} limit {pos.max_weight_kg:.0f} kg")
        for item in uld.items:
            if item.is_dangerous and not self.aircraft.dangerous_goods_certified:
                raise ValueError(f"{self.aircraft.registration} is not DG certified")
        self.loaded[position_code] = LoadedPosition(pos, uld)

    def add_bulk(self, item: CargoItem) -> None:
        """Load cargo loose in the belly of a passenger aircraft."""
        if not isinstance(self.aircraft, CommercialAircraft):
            raise TypeError("Bulk loading applies to passenger aircraft")
        if item.cargo_aircraft_only:
            raise ValueError(f"{item.awb} is Cargo Aircraft Only (CAO) - cannot fly on a passenger aircraft")
        if item.dg_class in ("1", "2.3"):
            raise ValueError(f"DG class {item.dg_class} is forbidden on passenger aircraft")
        if self.bulk_kg + item.weight_kg > self.aircraft.belly_cargo_capacity_kg:
            raise ValueError("Belly cargo capacity exceeded")
        self.bulk_kg += item.weight_kg

    def auto_load(self, ulds: list[Uld]) -> list[Uld]:
        """Greedy balanced loading: heaviest ULD first, each into the position
        that keeps the cumulative payload CG closest to the target CG.

        Returns ULDs that could not be placed.
        """
        target = self.aircraft.type.mac_to_arm(self.aircraft.type.empty_cg_mac)
        unplaced = []
        for uld in sorted(ulds, key=lambda u: u.gross_weight_kg, reverse=True):
            weight = self.total_weight_kg + uld.gross_weight_kg
            moment = self.moment_kgm
            candidates = sorted(
                (p for p in self.available_positions
                 if uld.uld_type in p.accepts and uld.gross_weight_kg <= p.max_weight_kg),
                key=lambda p: (abs((moment + uld.gross_weight_kg * p.arm_m) / weight - target),
                               abs(p.arm_m - target)),
            )
            for pos in candidates:
                try:
                    self.assign(uld, pos.code)
                    break
                except ValueError:
                    continue
            else:
                unplaced.append(uld)
        return unplaced

    @property
    def total_weight_kg(self) -> float:
        return self.bulk_kg + sum(lp.uld.gross_weight_kg for lp in self.loaded.values())

    @property
    def moment_kgm(self) -> float:
        """Moment about the nose datum (bulk cargo assumed at the mid-fuselage)."""
        mid = self.aircraft.type.length_m * 0.5
        return self.bulk_kg * mid + sum(
            lp.uld.gross_weight_kg * lp.position.arm_m for lp in self.loaded.values())

    def notoc(self) -> list[str]:
        """Notification to Captain: dangerous goods and special loads on board."""
        lines = []
        for code, lp in sorted(self.loaded.items()):
            for item in lp.uld.items:
                if item.is_dangerous or item.special_handling:
                    tags = []
                    if item.is_dangerous:
                        tags.append(f"DG {item.dg_class} {DG_CLASSES[item.dg_class]}"
                                    + (f" {item.un_number}" if item.un_number else ""))
                    tags += sorted(s.value for s in item.special_handling)
                    lines.append(f"{code} {lp.uld.uld_id} AWB {item.awb} "
                                 f"{item.weight_kg:.0f}kg: {', '.join(tags)}")
        return lines
