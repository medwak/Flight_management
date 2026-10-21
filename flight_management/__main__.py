"""Command line interface.

    python -m flight_management demo
    python -m flight_management types
    python -m flight_management airports
    python -m flight_management plan B77W EGLL KJFK --payload 30000 --alternate KORD --wind 270/60
    python -m flight_management metar "EGLL 211250Z 24015G25KT 9999 BKN012 12/09 Q1008"
"""

from __future__ import annotations

import argparse
import sys

from .aircraft import AIRCRAFT_TYPES, get_type
from .airports import default_airports
from .demo import run_demo
from .planning import Payload, create_flight_plan
from .weather import Wind, parse_metar


def _parse_wind(text: str) -> Wind:
    try:
        direction, speed = text.split("/")
        return Wind(float(direction), float(speed))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("wind must be DIR/SPEED, e.g. 270/45") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="flight_management", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("demo", help="run an end-to-end operations scenario")
    sub.add_parser("types", help="list supported aircraft types")
    sub.add_parser("airports", help="list built-in airports")
    plan = sub.add_parser("plan", help="produce an operational flight plan")
    plan.add_argument("type", help="aircraft ICAO type code, e.g. B77W")
    plan.add_argument("origin")
    plan.add_argument("destination")
    plan.add_argument("--payload", type=float, default=None, help="payload kg (default 60%% of max)")
    plan.add_argument("--alternate")
    plan.add_argument("--wind", type=_parse_wind, default=Wind.calm(), help="average route wind DIR/KT")
    plan.add_argument("--fl", type=int, help="cruise flight level, e.g. 370")
    plan.add_argument("--extra", type=float, default=0.0, help="extra fuel kg")
    metar = sub.add_parser("metar", help="decode a METAR")
    metar.add_argument("raw")
    args = parser.parse_args(argv)

    if args.command == "demo":
        run_demo()
    elif args.command == "types":
        print(f"{'TYPE':<6}{'CATEGORY':<12}{'MODEL':<28}{'MTOW kg':>10}{'PAYLOAD kg':>12}{'RANGE NM':>10}")
        for t in sorted(AIRCRAFT_TYPES.values(), key=lambda t: (t.category.value, t.icao_code)):
            print(f"{t.icao_code:<6}{t.category.value:<12}{t.manufacturer + ' ' + t.model:<28}"
                  f"{t.max_takeoff_weight_kg:>10,.0f}{t.max_payload_kg:>12,.0f}{t.range_nm:>10,.0f}")
    elif args.command == "airports":
        for a in default_airports():
            print(f"{a.icao} {a.iata}  {a.name:<28} {a.city:<14} {a.country}  "
                  f"elev {a.elevation_ft:>5.0f}ft  longest rwy {a.longest_runway_m:,.0f} m")
    elif args.command == "plan":
        airports = default_airports()
        ac_type = get_type(args.type)
        payload = args.payload if args.payload is not None else ac_type.max_payload_kg * 0.6
        fp = create_flight_plan(
            ac_type, airports.get(args.origin), airports.get(args.destination), Payload(cargo_kg=payload),
            alternate=airports.get(args.alternate) if args.alternate else None,
            callsign="PLAN", wind=args.wind,
            cruise_altitude_ft=args.fl * 100 if args.fl else None, extra_fuel_kg=args.extra)
        print(fp.summary())
        return 0 if fp.is_valid else 2
    elif args.command == "metar":
        m = parse_metar(args.raw)
        print(f"Station {m.station}: wind {m.wind.direction:03.0f}/{m.wind.speed:.0f}kt"
              + (f" gust {m.wind.gust:.0f}" if m.wind.gust else "")
              + f", vis {m.visibility_m:.0f} m, ceiling {m.ceiling_ft or 'none'}"
              + f", temp {m.temperature_c}, QNH {m.qnh_hpa}, {m.flight_category.value}"
              + (f", weather {' '.join(m.phenomena)}" if m.phenomena else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
