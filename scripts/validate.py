"""Offline invariants for published direct-only data and source-state rebuild."""
from __future__ import annotations
import argparse
import json
import math
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_data import DATA_POLICY, OUT, SCHEMA_VERSION, build_payload, compatible, load_artifacts


def validate(payload: dict) -> list[str]:
    if not compatible(payload):
        return ["Legacy or incompatible schema/report state"]
    rebuilt = build_payload(payload["report_state"], payload["coverage"]["files"],
                            start=date.fromisoformat(payload["coverage"]["start"]),
                            end=date.fromisoformat(payload["coverage"]["end"]), now=payload["updated"])
    errors = []
    for field in ("entities", "missing_entities", "methodology_details", "coverage"):
        if payload.get(field) != rebuilt[field]:
            errors.append(f"{field} differs from a fresh direct-only rebuild")
    for key, entity in payload["entities"].items():
        for point in entity["series"]:
            if not isinstance(point[1], (int, float)) or not math.isfinite(point[1]):
                errors.append(f"{key} has a nonfinite/non-numeric value")
            if point[2] < 1:
                errors.append(f"{key} has an empty aggregate")
        if entity["last"] is None and (entity["last_date"] is not None or entity["trades"]):
            errors.append(f"{key} missing value carries misleading date/count")
        for record in entity["recent_reports"]:
            if record["spread_bp"] is not None and record["quality_reasons"]:
                errors.append(f"{key} excluded report has a displayed spread")
    return errors


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", default=OUT)
    args = ap.parse_args()
    errors = validate(load_artifacts(args.path))
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        raise SystemExit(1)
    print(f"Validated schema {SCHEMA_VERSION}, {DATA_POLICY}, all entities and retained-state recomputation")


if __name__ == "__main__":
    main()
