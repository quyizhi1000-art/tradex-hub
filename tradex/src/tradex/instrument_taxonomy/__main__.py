"""CLI for catalog refresh, status and exact profile inspection."""

from __future__ import annotations

import argparse
import json
import sys

from tradex.utils.symbol import normalize_symbol

from .service import InstrumentTaxonomyService
from .store import InstrumentTaxonomyReader


def _instrument_id(value: str) -> str:
    code = normalize_symbol(value)
    if code.startswith("6"):
        return f"{code}.SH"
    if code.startswith(("4", "8")):
        return f"{code}.BJ"
    return f"{code}.SZ"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tradex instrument relationship catalog")
    subparsers = parser.add_subparsers(dest="command", required=True)
    refresh = subparsers.add_parser("refresh", help="refresh and atomically publish the catalog")
    refresh.add_argument("--as-of", default="")
    subparsers.add_parser(
        "refresh-official-evidence",
        help="overlay official evidence on the accepted provider snapshot",
    )
    subparsers.add_parser("status", help="read accepted catalog status")
    show = subparsers.add_parser("show", help="show one exact stock profile")
    show.add_argument("symbol")
    args = parser.parse_args(argv)

    if args.command == "refresh":
        with InstrumentTaxonomyService() as service:
            payload = service.refresh(as_of=args.as_of or None).model_dump(mode="json")
    elif args.command == "refresh-official-evidence":
        with InstrumentTaxonomyService() as service:
            payload = service.refresh_official_evidence().model_dump(mode="json")
    else:
        with InstrumentTaxonomyReader() as reader:
            if args.command == "status":
                status = reader.status()
                payload = status.model_dump(mode="json") if status else {
                    "contract": "stock_relationship_catalog_status.v1",
                    "status": "unavailable",
                }
            else:
                profile = reader.get(_instrument_id(args.symbol))
                payload = profile.model_dump(mode="json") if profile else {
                    "instrument_id": _instrument_id(args.symbol),
                    "status": "unresolved",
                }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
