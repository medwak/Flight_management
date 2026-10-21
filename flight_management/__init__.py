"""Flight management system for commercial, business and cargo aircraft operations."""

from .aircraft import (AIRCRAFT_TYPES, Aircraft, AircraftCategory, AircraftStatus, AircraftType,
                       BusinessJet, CabinClass, CargoAircraft, CommercialAircraft, build_aircraft,
                       get_type)
from .airports import Airport, AirportDatabase, Runway, default_airports
from .cargo import CargoItem, LoadPlan, SpecialHandling, Uld
from .crew import CrewMember, CrewRole
from .flight import Flight, FlightPhase, FlightStatus, FlightType
from .geo import Coordinate, great_circle_distance, initial_bearing
from .maintenance import Defect, MaintenanceTracker, MelCategory
from .operations import (CrewAssignmentError, DispatchRelease, FlightManagementSystem,
                         SchedulingError)
from .passengers import Passenger, PassengerManifest, PassengerType
from .planning import FlightPlan, Payload, Route, Waypoint, create_flight_plan
from .weather import Metar, Wind, parse_metar

__version__ = "1.0.0"

__all__ = [
    "AIRCRAFT_TYPES", "Aircraft", "AircraftCategory", "AircraftStatus", "AircraftType",
    "BusinessJet", "CabinClass", "CargoAircraft", "CommercialAircraft", "build_aircraft", "get_type",
    "Airport", "AirportDatabase", "Runway", "default_airports",
    "CargoItem", "LoadPlan", "SpecialHandling", "Uld",
    "CrewMember", "CrewRole",
    "Flight", "FlightPhase", "FlightStatus", "FlightType",
    "Coordinate", "great_circle_distance", "initial_bearing",
    "Defect", "MaintenanceTracker", "MelCategory",
    "CrewAssignmentError", "DispatchRelease", "FlightManagementSystem", "SchedulingError",
    "Passenger", "PassengerManifest", "PassengerType",
    "FlightPlan", "Payload", "Route", "Waypoint", "create_flight_plan",
    "Metar", "Wind", "parse_metar",
]
