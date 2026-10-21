import unittest
from datetime import date, datetime, timedelta, timezone

from flight_management import (CabinClass, CargoItem, Coordinate, LoadPlan, Passenger, PassengerType,
                               Payload, SpecialHandling, Uld, Wind, build_aircraft, create_flight_plan,
                               default_airports, get_type, great_circle_distance, initial_bearing,
                               parse_metar)
from flight_management.aircraft import BusinessJet, CargoAircraft, CommercialAircraft
from flight_management.cargo import dg_compatible
from flight_management.crew import CrewMember, CrewRole, DutyRecord
from flight_management.maintenance import Defect, MaintenanceTracker, MelCategory
from flight_management.passengers import PassengerManifest, PassengerStatus
from flight_management.planning import Route, available_flight_levels, select_cruise_level
from flight_management.weather import FlightCategory, crosswind_component, ground_speed, headwind_component

T0 = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
AIRPORTS = default_airports()


class GeoTests(unittest.TestCase):
    def test_distance_lhr_jfk(self):
        d = great_circle_distance(AIRPORTS.get("EGLL").location, AIRPORTS.get("KJFK").location)
        self.assertAlmostEqual(d, 2991, delta=15)

    def test_bearing_due_east(self):
        self.assertAlmostEqual(initial_bearing(Coordinate(0, 0), Coordinate(0, 10)), 90.0, places=3)

    def test_invalid_coordinate(self):
        with self.assertRaises(ValueError):
            Coordinate(95, 0)

    def test_iata_lookup(self):
        self.assertEqual(AIRPORTS.get("NBO").icao, "HKJK")


class WeatherTests(unittest.TestCase):
    def test_wind_components(self):
        wind = Wind(270, 20)
        self.assertAlmostEqual(headwind_component(270, wind), 20)
        self.assertAlmostEqual(headwind_component(90, wind), -20)
        self.assertAlmostEqual(crosswind_component(0, wind), 20)
        self.assertAlmostEqual(ground_speed(450, 90, wind), 470)

    def test_metar_decoding(self):
        m = parse_metar("EGLL 211250Z 24015G25KT 6000 -RA BKN012 OVC030 12/M01 Q1008")
        self.assertEqual(m.station, "EGLL")
        self.assertEqual(m.wind.gust, 25)
        self.assertEqual(m.visibility_m, 6000)
        self.assertEqual(m.ceiling_ft, 1200)
        self.assertEqual(m.dewpoint_c, -1)
        self.assertEqual(m.qnh_hpa, 1008)
        self.assertEqual(m.flight_category, FlightCategory.MVFR)
        self.assertIn("-RA", m.phenomena)

    def test_metar_us_format(self):
        m = parse_metar("KJFK 211751Z 04022G35KT 1/2SM +TSRA OVC004 18/17 A2990")
        self.assertEqual(m.flight_category, FlightCategory.LIFR)
        self.assertTrue(m.hazardous)
        self.assertAlmostEqual(m.visibility_m, 805, delta=2)


class AircraftTests(unittest.TestCase):
    def test_factory_returns_category_subclass(self):
        self.assertIsInstance(build_aircraft("A", "B77W"), CommercialAircraft)
        self.assertIsInstance(build_aircraft("B", "GLF6"), BusinessJet)
        self.assertIsInstance(build_aircraft("C", "B748"), CargoAircraft)

    def test_capacities(self):
        ac = build_aircraft("A", "B77W")
        self.assertEqual(ac.passenger_capacity, 354)
        self.assertEqual(ac.required_cabin_crew, 8)
        freighter = build_aircraft("C", "B77L")
        self.assertGreater(len(freighter.positions), 20)

    def test_unknown_type(self):
        with self.assertRaises(KeyError):
            get_type("XXXX")


class PlanningTests(unittest.TestCase):
    def test_semicircular_levels(self):
        self.assertTrue(all((lvl // 1000) % 2 == 1 for lvl in available_flight_levels(90) if lvl < 41000))
        self.assertTrue(all((lvl // 1000) % 2 == 0 for lvl in available_flight_levels(270) if lvl < 41000))

    def test_short_sector_flies_lower(self):
        t = get_type("A20N")
        self.assertLess(select_cruise_level(t, 150, 90), select_cruise_level(t, 1500, 90))
        self.assertLessEqual(select_cruise_level(t, 1500, 90, max_altitude_ft=31000), 31000)

    def test_route_generates_waypoints(self):
        route = Route.direct(AIRPORTS.get("EGLL"), AIRPORTS.get("KJFK"))
        self.assertGreater(len(route.waypoints), 5)
        self.assertAlmostEqual(route.distance_nm, AIRPORTS.get("EGLL").distance_to(AIRPORTS.get("KJFK")), delta=1)

    def test_fuel_plan_structure(self):
        plan = create_flight_plan(get_type("B788"), AIRPORTS.get("HKJK"), AIRPORTS.get("EGLL"),
                                  Payload(passengers_kg=20000, cargo_kg=5000), alternate=AIRPORTS.get("LFPG"))
        f = plan.fuel
        self.assertAlmostEqual(f.block_kg, f.taxi_kg + f.trip_kg + f.contingency_kg + f.alternate_kg
                               + f.final_reserve_kg + f.extra_kg)
        self.assertGreaterEqual(f.contingency_kg, 0.05 * f.trip_kg)
        self.assertGreater(f.alternate_kg, 0)
        self.assertTrue(plan.is_valid, plan.warnings)

    def test_headwind_increases_fuel(self):
        t, a, b = get_type("B788"), AIRPORTS.get("EGLL"), AIRPORTS.get("KJFK")
        calm = create_flight_plan(t, a, b, Payload(cargo_kg=10000))
        head = create_flight_plan(t, a, b, Payload(cargo_kg=10000), wind=Wind(270, 80))
        self.assertGreater(head.fuel.trip_kg, calm.fuel.trip_kg)

    def test_out_of_range_flagged(self):
        plan = create_flight_plan(get_type("E55P"), AIRPORTS.get("EGLL"), AIRPORTS.get("KJFK"), Payload())
        self.assertFalse(plan.is_valid)
        self.assertTrue(any("tank capacity" in w for w in plan.warnings))

    def test_overweight_flagged(self):
        t = get_type("A20N")
        plan = create_flight_plan(t, AIRPORTS.get("EGLL"), AIRPORTS.get("LFPG"), Payload(cargo_kg=t.max_payload_kg + 1000))
        self.assertTrue(any("MZFW" in w for w in plan.warnings))

    def test_hot_and_high_reduces_takeoff_weight(self):
        t = get_type("B77W")
        sea = create_flight_plan(t, AIRPORTS.get("OMDB"), AIRPORTS.get("EGLL"), Payload(cargo_kg=40000))
        high = create_flight_plan(t, AIRPORTS.get("HKJK"), AIRPORTS.get("EGLL"), Payload(cargo_kg=40000),
                                  origin_temp_c=30)
        self.assertGreater(high.takeoff_distance_m, sea.takeoff_distance_m)
        self.assertLess(high.max_payload_kg, sea.max_payload_kg + 1)

    def test_aft_cg_flagged(self):
        t = get_type("B77L")
        _, aft = t.payload_zone_m
        plan = create_flight_plan(t, AIRPORTS.get("KMEM"), AIRPORTS.get("KJFK"),
                                  Payload(cargo_kg=80000, cargo_moment_kgm=80000 * aft))
        self.assertTrue(any("CG" in w for w in plan.warnings))


class CargoTests(unittest.TestCase):
    def item(self, awb="1", weight=500, dg=None, cao=False, handling=()):
        return CargoItem(awb, "goods", 1, weight, 1.0, "HKJK", "EHAM", dg_class=dg,
                         cargo_aircraft_only=cao, special_handling=set(handling))

    def test_dg_segregation(self):
        self.assertFalse(dg_compatible("3", "5.1"))
        self.assertFalse(dg_compatible("1", "9"))
        self.assertTrue(dg_compatible("9", "3"))
        uld = Uld("AKE1", "AKE")
        uld.add(self.item(dg="3"))
        with self.assertRaises(ValueError):
            uld.add(self.item("2", dg="5.1"))

    def test_live_animals_and_dg(self):
        uld = Uld("PMC1", "PMC")
        uld.add(self.item(handling=[SpecialHandling.LIVE_ANIMAL]))
        ok, _ = uld.can_accept(self.item("2", dg="9"))
        self.assertFalse(ok)

    def test_uld_weight_limit(self):
        uld = Uld("AKE1", "AKE")
        with self.assertRaises(ValueError):
            uld.add(self.item(weight=2000))

    def test_cao_not_on_passenger_aircraft(self):
        plan = LoadPlan(build_aircraft("A", "B788"))
        with self.assertRaises(ValueError):
            plan.add_bulk(self.item(dg="9", cao=True))

    def test_auto_load_balances(self):
        ac = build_aircraft("C", "B77L")
        plan = LoadPlan(ac)
        ulds = []
        for i in range(10):
            u = Uld(f"PMC{i}", "PMC")
            u.add(self.item(str(i), weight=3000))
            ulds.append(u)
        self.assertEqual(plan.auto_load(ulds), [])
        target = ac.type.mac_to_arm(ac.type.empty_cg_mac)
        pitch = ac.positions[1].arm_m - ac.positions[0].arm_m
        self.assertLessEqual(abs(plan.moment_kgm / plan.total_weight_kg - target), pitch)
        fp = create_flight_plan(ac.type, AIRPORTS.get("HKJK"), AIRPORTS.get("EHAM"),
                                Payload(cargo_kg=plan.total_weight_kg, cargo_moment_kgm=plan.moment_kgm))
        self.assertFalse(any("CG" in w for w in fp.warnings), fp.warnings)

    def test_chargeable_weight(self):
        light = CargoItem("1", "pillows", 1, 50, 1.0, "A", "B")
        self.assertAlmostEqual(light.chargeable_weight_kg, 166.67)


class PassengerTests(unittest.TestCase):
    def setUp(self):
        self.manifest = PassengerManifest(build_aircraft("A", "A20N"))

    def test_booking_and_seating(self):
        p = Passenger("Ann", "Lee", cabin=CabinClass.BUSINESS)
        self.manifest.book(p)
        seat = self.manifest.check_in(p, bags=1)
        self.assertEqual(seat, "1A")
        self.manifest.board(p)
        self.assertEqual(p.status, PassengerStatus.BOARDED)
        self.assertEqual(self.manifest.passenger_mass_kg, 84)
        self.assertEqual(self.manifest.baggage_mass_kg, 15)

    def test_cabin_full(self):
        for i in range(12):
            self.manifest.book(Passenger(str(i), "X", cabin=CabinClass.BUSINESS))
        with self.assertRaises(ValueError):
            self.manifest.book(Passenger("late", "X", cabin=CabinClass.BUSINESS))

    def test_infant_needs_adult(self):
        with self.assertRaises(ValueError):
            self.manifest.book(Passenger("baby", "X", PassengerType.INFANT))

    def test_close_flight_marks_no_shows(self):
        a, b = Passenger("a", "X"), Passenger("b", "X")
        self.manifest.book(a)
        self.manifest.book(b)
        self.manifest.check_in(a, bags=1)
        offload = self.manifest.close_flight()
        self.assertEqual(b.status, PassengerStatus.NO_SHOW)
        self.assertEqual(offload, [a])


class CrewTests(unittest.TestCase):
    def test_type_rating_and_documents(self):
        c = CrewMember("1", "Pilot", CrewRole.CAPTAIN, type_ratings={"A20N"}, medical_expiry=date(2026, 1, 1))
        issues = c.legality_issues("B788", T0, T0 + timedelta(hours=5), 3)
        self.assertTrue(any("type-rated" in i for i in issues))
        self.assertTrue(any("expired" in i for i in issues))

    def test_rest_and_fdp(self):
        c = CrewMember("1", "Pilot", CrewRole.CAPTAIN, type_ratings={"A20N"})
        c.log_duty(DutyRecord("X1", T0 - timedelta(hours=10), T0 - timedelta(hours=2), 6))
        issues = c.legality_issues("A20N", T0, T0 + timedelta(hours=14), 12)
        self.assertTrue(any("rest" in i for i in issues))
        self.assertTrue(any("FDP" in i for i in issues))

    def test_28_day_limit(self):
        c = CrewMember("1", "Pilot", CrewRole.CAPTAIN, type_ratings={"A20N"})
        for d in range(1, 11):
            start = T0 - timedelta(days=d * 2)
            c.log_duty(DutyRecord(f"X{d}", start, start + timedelta(hours=11), 9.5))
        issues = c.legality_issues("A20N", T0, T0 + timedelta(hours=8), 7)
        self.assertTrue(any("28 days" in i for i in issues))


class MaintenanceTests(unittest.TestCase):
    def test_check_becomes_overdue(self):
        ac = build_aircraft("A", "A20N", "EGLL")
        tracker = MaintenanceTracker(ac, since=T0)
        ac.total_flight_hours += 800
        self.assertIn("A-check", tracker.overdue_checks(T0))
        tracker.perform_check("A-check", T0)
        self.assertEqual(tracker.overdue_checks(T0), [])

    def test_mel_expiry_and_no_go(self):
        ac = build_aircraft("A", "A20N", "EGLL")
        tracker = MaintenanceTracker(ac, since=T0)
        d = tracker.report_defect(Defect("Cabin light", T0, mel_category=MelCategory.B))
        self.assertEqual(tracker.airworthiness_issues(T0 + timedelta(days=1)), [])
        self.assertTrue(tracker.airworthiness_issues(T0 + timedelta(days=4)))
        tracker.rectify(d.defect_id, T0)
        tracker.report_defect(Defect("Engine fire loop fault", T0))
        self.assertEqual(ac.status.name, "AOG")


if __name__ == "__main__":
    unittest.main()
