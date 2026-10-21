# Flight Management System

A pure-Python (3.10+, standard library only) flight operations and flight
management system for **commercial airliners**, **business jets** and
**cargo freighters**. It covers what an airline / operator operations control
centre and an aircraft FMS do, from building the schedule to following the
flight on blocks at the destination.

## Features

| Area | What it does |
| --- | --- |
| **Fleet** | Aircraft type catalogue (A320neo, 737 MAX 8, 787-8, 777-300ER, A350-900, Phenom 300E, Challenger 350, G650ER, G700, 767-300F, 777F, 747-8F) with weights, fuel, speeds, ceiling, range and runway performance. Separate `CommercialAircraft` (multi-class cabins and belly cargo), `BusinessJet` (VIP cabin and amenities) and `CargoAircraft` (main and lower deck ULD positions, DG certification) classes. |
| **Airports** | Built-in database of major passenger, cargo hub and business aviation airports with runways, elevations and time zones. Includes nearest-suitable-airport search for diversions. |
| **Navigation** | Great-circle routes with generated waypoints, optional via-points, semicircular and RVSM cruise level selection, and position along the route. |
| **Weather** | METAR decoder (ICAO and US formats), VFR/MVFR/IFR/LIFR category, hazardous weather detection, and wind triangle (ground speed, headwind and crosswind). |
| **Flight planning (OFP)** | Climb, cruise and descent profile. Fuel policy: taxi + trip + 5% contingency + alternate + final reserve (30 min for commercial, 45 min for business) + extra. |
| **Weight & balance** | ZFW, TOW and LW checked against MZFW, MTOW and MLW. Centre of gravity in %MAC checked against the envelope. Maximum allowable payload. |
| **Performance** | Take-off and landing distance adjusted for weight, elevation, temperature, wind and wet runways. Performance-limited take-off weight (hot-and-high airports such as Nairobi and Johannesburg). Crosswind limits. |
| **Passengers** | Bookings and PNRs, cabin capacity and overbooking, infants, check-in with seat maps, boarding, no-shows and offloading of bags of passengers who did not board, standard masses. |
| **Cargo** | Air waybills, chargeable weight, ULD types (AKE, PMC, PAG and others), IATA dangerous goods classes and segregation, CAO (cargo aircraft only) rules, special handling (PER, AVI, COL and others), automatic balanced load planning, and a NOTOC for the captain. |
| **Crew** | Roles, type ratings, licence and medical validity, EASA-style FTL (FDP 13 h or 17 h augmented, minimum rest, 100 h in 28 days, 900 h per year), crew location tracking, required crew complement (one cabin crew member per 50 seats, augmented long-haul crew, loadmaster for DG), and automatic crew suggestions. |
| **Maintenance** | Check programmes per category (weekly, A and C checks for airliners and freighters; phase inspections for business jets), limits by hours, cycles or days, defects, MEL deferrals (categories A to D) with expiry and operational restrictions (for example an altitude cap that is enforced on the flight plan), and AOG status. |
| **Operations control** | Scheduling with range, runway, turnaround and aircraft-location checks; tail swaps; dispatch release with go/no-go checks (aircraft, maintenance, crew, weights, fuel, weather, alternates); IATA delay codes; cancellations. |
| **Flight following** | Phase state machine (preflight, taxi, take-off, climb, cruise, descent, approach, landing, taxi-in, parked; go-arounds and step climbs allowed), live progress page (position, altitude, ground speed, fuel on board, ETA), diversions, OOOI times, on-time performance, utilisation and fleet status reports. |

## Quick start

```bash
python -m flight_management demo          # end-to-end scenario with all three categories
python -m flight_management types         # aircraft catalogue
python -m flight_management airports      # airport database
python -m flight_management plan B77W HKJK KJFK --payload 40000 --alternate KORD --wind 270/40
python -m flight_management metar "KJFK 211751Z 04022G35KT 1/2SM +TSRA OVC004 18/17 A2990"
```

`plan` exits with status 2 if the plan breaks any limit.

## Library usage

```python
from datetime import datetime, timedelta, timezone
from flight_management import (FlightManagementSystem, build_aircraft, CrewMember, CrewRole,
                               Passenger, Wind)

fms = FlightManagementSystem()
fms.add_aircraft(build_aircraft("5Y-AAA", "A20N", "HKJK"))
fms.add_crew(CrewMember("P1", "Capt. Kim", CrewRole.CAPTAIN, "HKJK", {"A20N"}))
fms.add_crew(CrewMember("P2", "FO Lee", CrewRole.FIRST_OFFICER, "HKJK", {"A20N"}))
for i in range(4):
    role = CrewRole.PURSER if i == 0 else CrewRole.CABIN_CREW
    fms.add_crew(CrewMember(f"C{i}", f"Cabin {i}", role, "HKJK", {"*"}))
fms.update_weather("HKJK 211800Z 06012KT 9999 FEW030 19/11 Q1022",
                   "HKMO 211800Z 12008KT 9999 SCT020 26/22 Q1012")

std = datetime(2026, 10, 22, 6, 0, tzinfo=timezone.utc)
flight = fms.schedule_flight("DA101", "5Y-AAA", "HKJK", "HKMO", std, alternate="HKJK")
pax = Passenger("Jane", "Doe")
flight.manifest.book(pax)
flight.manifest.check_in(pax, bags=1)

fms.assign_crew(flight, *(c.employee_id for c in fms.suggest_crew(flight)))
fms.set_route_wind(flight, Wind(90, 20))
print(fms.plan_flight(flight).summary())          # operational flight plan
print(fms.dispatch(flight, std - timedelta(hours=1)))  # go/no-go release

fms.advance(std + timedelta(minutes=40))
print(flight.state_at(std + timedelta(minutes=40)))     # FMS progress page
```

## Package layout

```
flight_management/
  geo.py          great-circle maths
  weather.py      wind triangle, METAR decoding
  airports.py     airports and runways
  aircraft.py     aircraft types, commercial, business and cargo airframes
  planning.py     route, cruise level, fuel, weight & balance, performance, OFP
  passengers.py   bookings, seat map, check-in and boarding
  cargo.py        AWBs, ULDs, dangerous goods, load plan, NOTOC
  crew.py         crew records and FTL legality
  maintenance.py  checks, defects, MEL
  flight.py       flight lifecycle, phases, events, tracking
  operations.py   FlightManagementSystem (scheduling, dispatch, flight following, reports)
  demo.py         scenario used by `python -m flight_management demo`
tests/            unittest suite
```

## Tests

```bash
python -m unittest discover -s tests -t .
```

## Disclaimer

Performance figures and regulatory rules are representative and simplified
for simulation and education. **Do not use this for real flight operations.**
