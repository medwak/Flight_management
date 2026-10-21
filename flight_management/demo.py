"""End-to-end demonstration: one commercial, one business and one cargo operation."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

from .aircraft import CabinClass, build_aircraft
from .cargo import CargoItem, SpecialHandling, Uld
from .crew import REPORT_BEFORE_DEPARTURE, CrewMember, CrewRole
from .maintenance import Defect, MelCategory
from .operations import FlightManagementSystem
from .passengers import Passenger, PassengerType
from .weather import Wind


def _header(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def build_demo_system(start: datetime) -> FlightManagementSystem:
    fms = FlightManagementSystem(operator="Demo Air")
    since = start - timedelta(days=2)
    fms.add_aircraft(build_aircraft("5Y-DAA", "B788", "HKJK", total_flight_hours=21500, total_cycles=5200), since)
    fms.add_aircraft(build_aircraft("5Y-DAB", "B788", "HKJK", total_flight_hours=18300, total_cycles=4400), since)
    fms.add_aircraft(build_aircraft("N650DA", "GLF6", "KTEB", total_flight_hours=3200, total_cycles=1400,
                                    amenities=["wifi", "galley", "shower"]), since)
    fms.add_aircraft(build_aircraft("5Y-DAF", "B77L", "HKJK", total_flight_hours=48000, total_cycles=16000), since)

    crew = [
        CrewMember("P001", "Capt. A. Wanjiru", CrewRole.CAPTAIN, "HKJK", {"B788"}),
        CrewMember("P002", "FO B. Otieno", CrewRole.FIRST_OFFICER, "HKJK", {"B788"}),
        CrewMember("P003", "FO C. Kamau", CrewRole.FIRST_OFFICER, "HKJK", {"B788"}),
        CrewMember("C001", "D. Achieng", CrewRole.PURSER, "HKJK", {"*"}),
        *[CrewMember(f"C1{i:02d}", f"Cabin crew {i}", CrewRole.CABIN_CREW, "HKJK", {"*"}) for i in range(1, 7)],
        CrewMember("P101", "Capt. E. Miller", CrewRole.CAPTAIN, "KTEB", {"GLF6"}),
        CrewMember("P102", "FO F. Garcia", CrewRole.FIRST_OFFICER, "KTEB", {"GLF6"}),
        CrewMember("P201", "Capt. G. Mwangi", CrewRole.CAPTAIN, "HKJK", {"B77L"}),
        CrewMember("P202", "FO H. Njoroge", CrewRole.FIRST_OFFICER, "HKJK", {"B77L"}),
        CrewMember("L001", "I. Kiprop", CrewRole.LOADMASTER, "HKJK", {"*"}),
        CrewMember("P900", "Capt. J. Expired", CrewRole.CAPTAIN, "HKJK", {"B788"},
                   medical_expiry=date(2020, 1, 1)),
    ]
    for c in crew:
        fms.add_crew(c)
    return fms


def run_demo() -> None:
    now = datetime(2026, 10, 21, 18, 0, tzinfo=timezone.utc)
    fms = build_demo_system(now)
    fms.update_weather(
        "HKJK 211800Z 06012KT 9999 FEW030 19/11 Q1022",
        "EGLL 211750Z 24015G25KT 6000 -RA BKN012 12/09 Q1008",
        "EGGW 211750Z 25012KT 8000 BKN015 11/08 Q1009",
        "KTEB 211751Z 31008KT 10SM FEW250 16/03 A3012",
        "EHAM 211755Z 22018KT 9999 SCT025 13/08 Q1011",
    )

    # ---------------------------------------------------------- commercial
    _header("1. COMMERCIAL - DA100 Nairobi -> London Heathrow (B787-8)")
    da100 = fms.schedule_flight("DA100", "5Y-DAA", "HKJK", "EGLL", now + timedelta(hours=5), alternate="LFPG")
    fms.set_route_wind(da100, Wind(330, 35))
    print(da100.summary())
    pax = []
    for i in range(170):
        cabin = CabinClass.BUSINESS if i < 24 else CabinClass.ECONOMY
        p = Passenger(f"Guest{i}", "Traveller", cabin=cabin)
        da100.manifest.book(p)
        pax.append(p)
    family = [Passenger("Amina", "Odhiambo"), Passenger("Baby", "Odhiambo", PassengerType.INFANT)]
    da100.manifest.book(*family)
    for p in pax[:165] + family:
        da100.manifest.check_in(p, bags=1)
    print(f"Passengers: {da100.manifest.passenger_count} (+{da100.manifest.infant_count} INF), "
          f"load factor {da100.manifest.load_factor:.0%}; seat {family[0].seat} for {family[0].full_name}")
    da100.load_plan.add_bulk(CargoItem("706-10000011", "Tea", 120, 3600, 9.0, "HKJK", "EGLL"))
    da100.load_plan.add_bulk(CargoItem("706-10000022", "Mail", 30, 800, 4.0, "HKJK", "EGLL"))

    try:
        fms.assign_crew(da100, "P900")
    except ValueError as exc:
        print(f"Crew check rejected: {exc}")
    suggested = fms.suggest_crew(da100)
    fms.assign_crew(da100, *(c.employee_id for c in suggested))
    print(f"Crew requirement: {fms.crew_requirement(da100).describe()}; assigned "
          + ", ".join(c.name for c in da100.crew))

    # A deferred defect: one pack inoperative => altitude restriction advisory
    fms.maintenance["5Y-DAA"].report_defect(Defect(
        "Pack 2 flow control valve inoperative", now - timedelta(hours=3), "21",
        MelCategory.C, operational_restriction="Max FL350 with one pack inoperative",
        max_altitude_ft=35000))

    plan = fms.plan_flight(da100)
    print()
    print(plan.summary())
    release = fms.dispatch(da100, now + timedelta(hours=3))
    print()
    print(release)

    # ---------------------------------------------------------- business
    _header("2. BUSINESS - DA9 Teterboro -> London Luton (Gulfstream G650ER)")
    da9 = fms.schedule_flight("DA9", "N650DA", "KTEB", "EGGW", now + timedelta(hours=2), alternate="EGLL")
    fms.set_route_wind(da9, Wind(270, 80))  # North Atlantic jet stream tailwind
    for name in ("Chair", "CEO", "CFO", "Counsel", "Assistant"):
        p = Passenger(name, "Exec", cabin=CabinClass.BUSINESS, special_service={"VGML"} if name == "CEO" else set())
        da9.manifest.book(p)
        da9.manifest.check_in(p, bags=2)
    fms.assign_crew(da9, *(c.employee_id for c in fms.suggest_crew(da9)))
    print(fms.plan_flight(da9).summary())
    print()
    print(fms.dispatch(da9, now))

    # ---------------------------------------------------------- cargo
    _header("3. CARGO - DA500 Nairobi -> Amsterdam (Boeing 777F) with flowers and DG")
    da500 = fms.schedule_flight("DA500", "5Y-DAF", "HKJK", "EHAM", now + timedelta(hours=3), alternate="EDDF")
    fms.set_route_wind(da500, Wind(320, 30))
    ulds = []
    for i in range(24):
        uld = Uld(f"PMC{1000 + i}DA", "PMC")
        uld.add(CargoItem(f"706-2000{i:04d}", "Fresh cut roses", 80, 2600, 10.5, "HKJK", "EHAM",
                          special_handling={SpecialHandling.PERISHABLE, SpecialHandling.COOL}))
        ulds.append(uld)
    dg = Uld("AKE5001DA", "AKE")
    dg.add(CargoItem("706-30000001", "Lithium ion batteries", 10, 400, 1.2, "HKJK", "EHAM",
                     dg_class="9", un_number="UN3480", cargo_aircraft_only=True))
    ulds.append(dg)
    flam = CargoItem("706-30000002", "Paint", 4, 150, 0.5, "HKJK", "EHAM", dg_class="3", un_number="UN1263")
    oxid = CargoItem("706-30000003", "Pool chlorine", 4, 150, 0.5, "HKJK", "EHAM", dg_class="5.1", un_number="UN2880")
    mixed = Uld("AKE5002DA", "AKE")
    mixed.add(flam)
    print(f"Segregation check: class 3 + class 5.1 in one ULD -> {mixed.can_accept(oxid)[1]}")
    ulds.append(mixed)
    unplaced = da500.load_plan.auto_load(ulds)
    print(f"Loaded {len(da500.load_plan.loaded)} ULDs, {da500.load_plan.total_weight_kg:,.0f} kg; "
          f"unplaced: {len(unplaced)}")
    print("NOTOC:")
    for line in da500.load_plan.notoc()[:3]:
        print(f"  {line}")
    print(f"  ... {len(da500.load_plan.notoc())} special loads in total")
    fms.assign_crew(da500, *(c.employee_id for c in fms.suggest_crew(da500)))
    print(fms.plan_flight(da500).summary())
    print()
    print(fms.dispatch(da500, now + timedelta(hours=1)))

    # ---------------------------------------------------------- flight following
    _header("4. FLIGHT FOLLOWING")
    clock = now
    for _ in range(14):
        clock += timedelta(hours=1)
        for msg in fms.advance(clock):
            print(f"  {msg}")
        airborne = [f for f in fms.flights.values() if f.is_airborne]
        for f in airborne:
            print(f"  >> {f.flight_number:<6} {f.state_at(clock)}")

    _header("5. OPERATIONS SUMMARY")
    for f in sorted(fms.flights.values(), key=lambda f: f.scheduled_departure):
        print(f.summary())
        block = f.actual_block_hours
        if block:
            print(f"         block {block:.2f} h, events {len(f.events)}")
    otp = fms.on_time_performance()
    print(f"On-time performance: {otp:.0%}" if otp is not None else "No completed flights")
    print()
    print(fms.fleet_report(clock))
    _diversion_example(fms, clock)


def _diversion_example(fms: FlightManagementSystem, now: datetime) -> None:
    _header("6. IRREGULAR OPS - DA101 London -> Nairobi: crew rest delay, then diversion")
    da100 = fms.find_flight("DA100")
    da101 = fms.schedule_flight("DA101", "5Y-DAA", "EGLL", "HKJK", now + timedelta(hours=2))
    for i in range(120):
        p = Passenger(f"Return{i}", "Traveller")
        da101.manifest.book(p)
        da101.manifest.check_in(p, bags=1)
    crew_ids = [c.employee_id for c in da100.crew]
    try:
        fms.assign_crew(da101, *crew_ids)
    except ValueError as exc:
        print(f"Crew check rejected: {str(exc).split('; ')[0]} ...")
    # Delay until the inbound crew has completed minimum rest.
    released = max(d.release for c in da100.crew for d in c.duties)
    ready = released + timedelta(hours=12) + REPORT_BEFORE_DEPARTURE
    minutes = 5 * math.ceil((ready - da101.estimated_departure).total_seconds() / 300)
    da101.delay(minutes, "64", now, "inbound crew in minimum rest")
    print(f"  {da101.events[-1]}")
    fms.assign_crew(da101, *crew_ids)
    print(fms.dispatch(da101, now))

    t = da101.estimated_departure + timedelta(hours=4)
    fms.advance(t)
    print(f"  >> {da101.flight_number:<6} {da101.state_at(t)}")
    target = fms.divert(da101, t, "Medical emergency - passenger cardiac arrest")
    print(f"Diverting to nearest suitable airport: {target} "
          f"({da101.diversion_start.position} -> ETA {da101.diversion_eta:%H:%MZ})")
    print(f"  >> {da101.flight_number:<6} {da101.state_at(t + timedelta(minutes=30))}")
    for msg in fms.advance(t + timedelta(hours=4)):
        print(f"  {msg}")
    print(da101.summary())
    print(f"5Y-DAA now at {fms.aircraft('5Y-DAA').location}; "
          f"crew at {da101.crew[0].location_at(t + timedelta(hours=4))}")
