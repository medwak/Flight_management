import unittest
from datetime import datetime, timedelta, timezone

from flight_management import (CargoItem, CrewMember, CrewRole, FlightManagementSystem, FlightPhase,
                               FlightStatus, Passenger, SchedulingError, Uld, build_aircraft)
from flight_management.flight import FlightType
from flight_management.maintenance import Defect

T0 = datetime(2026, 10, 21, 6, 0, tzinfo=timezone.utc)


def make_fms() -> FlightManagementSystem:
    fms = FlightManagementSystem()
    fms.add_aircraft(build_aircraft("5Y-AAA", "A20N", "HKJK"), since=T0)
    fms.add_aircraft(build_aircraft("5Y-AAB", "A20N", "HKJK"), since=T0)
    fms.add_aircraft(build_aircraft("N1BJ", "CL35", "KTEB"), since=T0)
    fms.add_aircraft(build_aircraft("5Y-CGO", "B77L", "HKJK"), since=T0)
    for i, (role, rating) in enumerate([
        (CrewRole.CAPTAIN, "A20N"), (CrewRole.FIRST_OFFICER, "A20N"),
        (CrewRole.CAPTAIN, "A20N"), (CrewRole.FIRST_OFFICER, "A20N"),
        (CrewRole.PURSER, "*"), (CrewRole.CABIN_CREW, "*"), (CrewRole.CABIN_CREW, "*"),
        (CrewRole.PURSER, "*"), (CrewRole.CABIN_CREW, "*"), (CrewRole.CABIN_CREW, "*"),
        (CrewRole.CAPTAIN, "B77L"), (CrewRole.FIRST_OFFICER, "B77L"), (CrewRole.LOADMASTER, "*"),
    ]):
        fms.add_crew(CrewMember(f"N{i}", f"Crew {i}", role, "HKJK", {rating}))
    fms.add_crew(CrewMember("U1", "BJ Capt", CrewRole.CAPTAIN, "KTEB", {"CL35"}))
    fms.add_crew(CrewMember("U2", "BJ FO", CrewRole.FIRST_OFFICER, "KTEB", {"CL35"}))
    fms.update_weather("HKJK 210600Z 06010KT 9999 FEW030 18/10 Q1022",
                       "HKMO 210600Z 12008KT 9999 SCT020 26/22 Q1012")
    return fms


def crew_up(fms, flight):
    fms.assign_crew(flight, *(c.employee_id for c in fms.suggest_crew(flight)))


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.fms = make_fms()

    def test_schedule_estimates_arrival(self):
        f = self.fms.schedule_flight("DA1", "5Y-AAA", "HKJK", "HKMO", T0 + timedelta(hours=2))
        self.assertGreater(f.scheduled_block_hours, 0.7)
        self.assertLess(f.scheduled_block_hours, 1.5)

    def test_aircraft_conflict(self):
        self.fms.schedule_flight("DA1", "5Y-AAA", "HKJK", "HKMO", T0 + timedelta(hours=2))
        with self.assertRaises(SchedulingError):
            self.fms.schedule_flight("DA3", "5Y-AAA", "HKJK", "HKMO", T0 + timedelta(hours=2, minutes=30))

    def test_location_continuity(self):
        self.fms.schedule_flight("DA1", "5Y-AAA", "HKJK", "HKMO", T0 + timedelta(hours=2))
        with self.assertRaises(SchedulingError):
            self.fms.schedule_flight("DA5", "5Y-AAA", "HKJK", "HKMO", T0 + timedelta(hours=6))
        self.fms.schedule_flight("DA2", "5Y-AAA", "HKMO", "HKJK", T0 + timedelta(hours=6))

    def test_range_limit(self):
        with self.assertRaises(SchedulingError):
            self.fms.schedule_flight("DA9", "5Y-AAA", "HKJK", "KJFK", T0)

    def test_freighter_cannot_fly_passengers(self):
        with self.assertRaises(SchedulingError):
            self.fms.schedule_flight("DA7", "5Y-CGO", "HKJK", "EHAM", T0,
                                     flight_type=FlightType.SCHEDULED_PASSENGER)


class DispatchAndTrackingTests(unittest.TestCase):
    def setUp(self):
        self.fms = make_fms()
        self.flight = self.fms.schedule_flight("DA1", "5Y-AAA", "HKJK", "HKMO", T0 + timedelta(hours=2),
                                               alternate="HKJK")
        for i in range(100):
            p = Passenger(str(i), "Pax")
            self.flight.manifest.book(p)
            self.flight.manifest.check_in(p, bags=1)

    def test_dispatch_requires_crew(self):
        release = self.fms.dispatch(self.flight, T0)
        self.assertFalse(release.approved)
        self.assertTrue(any("captain" in i.lower() for i in release.issues))

    def test_full_lifecycle(self):
        crew_up(self.fms, self.flight)
        release = self.fms.dispatch(self.flight, T0)
        self.assertTrue(release.approved, release.issues)
        self.fms.advance(T0 + timedelta(hours=2, minutes=30))
        self.assertTrue(self.flight.is_airborne)
        state = self.flight.state_at(T0 + timedelta(hours=2, minutes=30))
        self.assertGreater(state.distance_flown_nm, 0)
        self.assertLess(state.fuel_on_board_kg, self.flight.plan.fuel.block_kg)
        self.fms.advance(T0 + timedelta(hours=5))
        self.assertEqual(self.flight.status, FlightStatus.ARRIVED)
        self.assertEqual(self.flight.phase, FlightPhase.PARKED)
        ac = self.fms.aircraft("5Y-AAA")
        self.assertEqual(ac.location, "HKMO")
        self.assertEqual(ac.total_cycles, 1)
        self.assertEqual(len(self.flight.crew[0].duties), 1)
        self.assertEqual(self.flight.crew[0].location_at(T0 + timedelta(hours=6)), "HKMO")

    def test_no_go_defect_blocks_dispatch(self):
        crew_up(self.fms, self.flight)
        self.fms.maintenance["5Y-AAA"].report_defect(Defect("Brake wear limit", T0))
        release = self.fms.dispatch(self.flight, T0)
        self.assertFalse(release.approved)
        self.fms.swap_aircraft(self.flight, "5Y-AAB", T0)
        self.assertTrue(self.fms.dispatch(self.flight, T0).approved)

    def test_bad_weather_requires_alternate(self):
        flight = self.fms.schedule_flight("DA3", "5Y-AAB", "HKJK", "HKMO", T0 + timedelta(hours=3))
        crew_up(self.fms, flight)
        self.fms.update_weather("HKMO 210600Z 12008KT 0800 FG VV002 22/22 Q1012")
        release = self.fms.dispatch(flight, T0)
        self.assertTrue(any("alternate" in i for i in release.issues))

    def test_diversion(self):
        crew_up(self.fms, self.flight)
        self.fms.dispatch(self.flight, T0)
        t = T0 + timedelta(hours=2, minutes=40)
        target = self.fms.divert(self.flight, t, "Weather", use_alternate=True)
        self.assertEqual(target.icao, "HKJK")
        self.fms.advance(t + timedelta(hours=2))
        self.assertEqual(self.flight.status, FlightStatus.DIVERTED)
        self.assertEqual(self.fms.aircraft("5Y-AAA").location, "HKJK")

    def test_crew_cannot_double_book(self):
        crew_up(self.fms, self.flight)
        other = self.fms.schedule_flight("DA3", "5Y-AAB", "HKJK", "HKMO", T0 + timedelta(hours=2, minutes=30))
        with self.assertRaises(ValueError):
            self.fms.assign_crew(other, self.flight.crew[0].employee_id)


class CategoryTests(unittest.TestCase):
    def setUp(self):
        self.fms = make_fms()

    def test_business_jet_flight(self):
        self.fms.update_weather("KTEB 211751Z 31008KT 10SM FEW250 16/03 A3012")
        f = self.fms.schedule_flight("BJ1", "N1BJ", "KTEB", "KORD", T0)
        self.assertEqual(f.flight_type, FlightType.PRIVATE)
        p = Passenger("VIP", "One")
        f.manifest.book(p)
        f.manifest.check_in(p)
        crew_up(self.fms, f)
        release = self.fms.dispatch(f, T0 - timedelta(hours=1))
        self.assertTrue(release.approved, release.issues)
        self.assertEqual(f.plan.fuel.final_reserve_min, 45)

    def test_cargo_flight_with_dg_needs_loadmaster(self):
        f = self.fms.schedule_flight("CG1", "5Y-CGO", "HKJK", "EHAM", T0 + timedelta(hours=2))
        uld = Uld("AKE1", "AKE")
        uld.add(CargoItem("1", "Batteries", 1, 300, 1, "HKJK", "EHAM", dg_class="9", cargo_aircraft_only=True))
        self.assertEqual(f.load_plan.auto_load([uld]), [])
        self.assertEqual(self.fms.crew_requirement(f).loadmasters, 1)
        crew_up(self.fms, f)
        release = self.fms.dispatch(f, T0)
        self.assertTrue(release.approved, release.issues)
        self.assertTrue(any("NOTOC" in a for a in release.advisories))


if __name__ == "__main__":
    unittest.main()
