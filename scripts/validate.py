"""Hold-out check on the upfront -> par-spread inversion.

For every entity-day that contains at least one directly quoted spread, rebuild
that day's level using ONLY the upfront-quoted trades (anchored on the previous
day, exactly as the live pipeline does) and compare it to the quoted spreads.
That isolates the model error from the data.

    python scripts/validate.py
"""

from __future__ import annotations

import os
import statistics
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from build_data import HISTORY_START, WATCHLIST, collect, resolve_day


def main() -> None:
    today = datetime.now(timezone.utc).date()
    by_entity = collect(HISTORY_START, today)

    print(f"{'entity':11s} {'days':>5} {'median err':>11} {'p90 err':>9} {'max err':>9}")
    print("-" * 50)
    overall = []
    for key, *_ in WATCHLIST:
        prev = None
        errors = []
        for day in sorted(by_entity[key]):
            trades = by_entity[key][day]
            quoted = [t["spread"] for t in trades if t["tier"] == 0]
            derived = [t for t in trades if t["tier"] > 0]
            if quoted and derived:
                held = resolve_day(derived, prev)
                if held:
                    errors.append(abs(held[0] - statistics.median(quoted) * 1e4))
            resolved = resolve_day(trades, prev)
            if resolved:
                prev = resolved[0] / 1e4
        if errors:
            errors.sort()
            overall += errors
            p90 = errors[int(0.9 * (len(errors) - 1))]
            print(f"{key:11s} {len(errors):5d} {statistics.median(errors):10.2f}bp "
                  f"{p90:8.2f}bp {errors[-1]:8.2f}bp")
    overall.sort()
    print("-" * 50)
    print(f"{'ALL':11s} {len(overall):5d} {statistics.median(overall):10.2f}bp "
          f"{overall[int(0.9 * (len(overall) - 1))]:8.2f}bp {overall[-1]:8.2f}bp")


if __name__ == "__main__":
    main()
