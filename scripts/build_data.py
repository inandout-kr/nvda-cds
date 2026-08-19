"""Build docs/data/cds.json from DTCC public dissemination files.

Usage:
    python scripts/build_data.py                 # incremental (last 10 days)
    python scripts/build_data.py --full          # rebuild history from scratch
    python scripts/build_data.py --since 2026-01-01
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cds import spreads_from_upfront
from dtcc import business_days, fetch_day

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "data", "cds.json")
HISTORY_START = date(2025, 10, 1)

# (key, display name, ticker, reference-entity names as they appear in the feed,
# uppercased and stripped of punctuation). Reporting parties spell the same
# entity several ways, and near-miss names in the same file are DIFFERENT credits
# - CoreWeave's loan CDS ("COREWEAVE FINANCING-DDTL") and SPVs trade hundreds of
# basis points away from CoreWeave Inc, so the match has to be exact.
WATCHLIST = [
    ("NVIDIA", "NVIDIA", "NVDA", {"NVIDIACORP", "NVIDIACORPORATION"}),
    ("ORACLE", "Oracle", "ORCL", {"ORACLECORPORATION", "ORACLECORP", "ORACLECOP"}),
    ("COREWEAVE", "CoreWeave", "CRWV", {"COREWEAVEINC"}),
    ("BROADCOM", "Broadcom", "AVGO", {"BROADCOMINC"}),
    ("AMD", "AMD", "AMD", {"ADVANCEDMICRODEVICESINC"}),
    ("MICROSOFT", "Microsoft", "MSFT", {"MICROSOFTCORPORATION", "MICROSOFTCORP", "MICROSOFT"}),
    ("META", "Meta Platforms", "META", {"METAPLATFORMSINC"}),
    ("ALPHABET", "Alphabet", "GOOGL", {"ALPHABETINC"}),
    ("AMAZON", "Amazon", "AMZN", {"AMAZONCOMINC"}),
    ("SOFTBANK", "SoftBank Group", "9984.T", {"SOFTBANKGROUPCORP"}),
    ("DELL", "Dell Technologies", "DELL", {"DELLINC"}),
    ("INTEL", "Intel", "INTC", {"INTELCORPORATION", "INTELCORP"}),
]

# senior unsecured single-name corporate CDS; excludes loan CDS and index trades
FISN = "NA/CDS Corp SN Sr"

TENOR_MIN, TENOR_MAX = 4.0, 6.0  # years to maturity that count as the 5Y benchmark
ANCHOR_BAND = 0.5  # drop trades more than +-50% away from the day's anchor
# a few reports put something other than a par spread in the spread field
# (CoreWeave prints of 1-4bp against a 500bp coupon); no real 5Y corporate CDS
# trades that tight, so treat those rows as upfront-quoted instead
QUOTE_MIN, QUOTE_MAX = 0.0005, 0.30


def norm(name: str) -> str:
    return re.sub(r"[^A-Z]", "", (name or "").upper())


ENTITY_OF = {alias: key for key, _, _, aliases in WATCHLIST for alias in aliases}


def num(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        return float(raw.replace(",", "").replace("+", "").strip())
    except ValueError:
        return None


def parse_trade(row: dict) -> dict | None:
    """Turn one disseminated row into a 5Y CDS observation, or None."""
    if row.get("Action type") != "NEWT" or row.get("Event type") != "TRAD":
        return None
    if (row.get("UPI FISN") or "").strip() != FISN:
        return None
    key = ENTITY_OF.get(norm(row.get("Underlying Asset Name")))
    if key is None:
        return None

    try:
        ts = datetime.fromisoformat(row["Execution Timestamp"].replace("Z", "+00:00"))
        maturity = date.fromisoformat(row["Expiration Date"])
    except (KeyError, ValueError):
        return None

    traded = ts.date()
    years = (maturity - traded).days / 365.25
    if not TENOR_MIN <= years <= TENOR_MAX:
        return None

    spread = num(row.get("Spread-Leg 1")) or num(row.get("Spread-Leg 2"))
    if spread and QUOTE_MIN <= spread <= QUOTE_MAX:  # quoted in spread terms already
        return {"key": key, "ts": ts, "date": traded, "spread": spread, "tier": 0}

    coupon = num(row.get("Fixed rate-Leg 1")) or num(row.get("Fixed rate-Leg 2"))
    notional_raw = row.get("Notional amount-Leg 1") or ""
    notional = num(notional_raw)
    cash = num(row.get("Other payment amount"))
    if not coupon or not notional or not cash:
        return None
    if "UFRO" not in (row.get("Other payment type") or ""):
        return None

    candidates = spreads_from_upfront(traded, maturity, coupon, cash / notional)
    if not candidates:
        return None
    # notional is capped at 5,000,000+ in the public feed, so a block trade can
    # scale the implied spread; tier 2 trades get a wider trust discount later.
    tier = 1 if "+" not in notional_raw else 2
    return {"key": key, "ts": ts, "date": traded, "candidates": candidates, "tier": tier}


def resolve_day(trades: list[dict], prev: float | None) -> tuple[float, list[dict]] | None:
    """Pick the spread branch for each trade and return (median bp, kept trades)."""
    explicit = [t["spread"] for t in trades if t["tier"] == 0]
    anchor = statistics.median(explicit) if explicit else prev
    if anchor is None:
        # no explicit print and no prior level: fall back to the sub-coupon branch,
        # which is the right one for an investment-grade name at a 100bp coupon
        anchor = statistics.median(
            [t["candidates"][0] for t in trades if t["tier"] > 0] or [0]
        )
        if not anchor:
            return None

    resolved = [
        {**t, "spread": t["spread"] if t["tier"] == 0
         else min(t["candidates"], key=lambda c: abs(c - anchor))}
        for t in trades
    ]
    kept = [t for t in resolved
            if t["tier"] == 0 or abs(t["spread"] - anchor) <= ANCHOR_BAND * anchor]
    # if the anchor rejects everything it is the anchor that is stale, not the
    # trades - fall back to the day's own prints so the series re-baselines
    kept = kept or resolved
    if not kept:
        return None
    return statistics.median([t["spread"] for t in kept]) * 1e4, kept


def collect(start: date, end: date) -> dict[str, dict[date, list[dict]]]:
    by_entity: dict[str, dict[date, list[dict]]] = {k: {} for k, *_ in WATCHLIST}
    for d in business_days(start, end):
        rows = fetch_day(d)
        if rows is None:
            continue
        for row in rows:
            t = parse_trade(row)
            if t:
                by_entity[t["key"]].setdefault(t["date"], []).append(t)
    return by_entity


def change(series: list, days_back: int) -> float | None:
    """Spread change vs the last observation at least `days_back` days earlier."""
    if not series:
        return None
    last_day = date.fromisoformat(series[-1][0])
    cutoff = last_day - timedelta(days=days_back)
    earlier = [p for p in series[:-1] if date.fromisoformat(p[0]) <= cutoff]
    if not earlier:
        return None
    return round(series[-1][1] - earlier[-1][1], 1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="rebuild the whole history")
    ap.add_argument("--since", help="YYYY-MM-DD start date")
    args = ap.parse_args()

    today = datetime.now(timezone.utc).date()
    if args.since:
        start = date.fromisoformat(args.since)
    elif args.full:
        start = HISTORY_START
    else:
        start = today - timedelta(days=10)

    previous = {}
    if not args.full and os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as fh:
            previous = json.load(fh).get("entities", {})

    by_entity = collect(start, today)

    entities = {}
    last_trades = []
    for key, name, ticker, _ in WATCHLIST:
        old = previous.get(key, {}).get("series", [])
        series = [p for p in old if not (start <= date.fromisoformat(p[0]) <= today)]
        prev_level = series[-1][1] / 1e4 if series else None

        for day in sorted(by_entity[key]):
            resolved = resolve_day(by_entity[key][day], prev_level)
            if not resolved:
                continue
            level_bp, kept = resolved
            series.append([day.isoformat(), round(level_bp, 1), len(kept)])
            prev_level = level_bp / 1e4
            if key == "NVIDIA":
                last_trades.extend(
                    {"ts": t["ts"].isoformat().replace("+00:00", "Z"),
                     "spread": round(t["spread"] * 1e4, 1),
                     "quoted": t["tier"] == 0}
                    for t in kept
                )

        series.sort(key=lambda p: p[0])
        entities[key] = {
            "name": name,
            "ticker": ticker,
            "series": series,
            "last": series[-1][1] if series else None,
            "last_date": series[-1][0] if series else None,
            "trades": series[-1][2] if series else 0,
            "chg_1d": change(series, 1),
            "chg_1w": change(series, 7),
            "chg_1m": change(series, 30),
        }

    last_trades.sort(key=lambda t: t["ts"], reverse=True)
    payload = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "entities": entities,
        "nvidia_trades": last_trades[:40],
    }

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"))

    nv = entities["NVIDIA"]
    print(f"NVIDIA 5Y {nv['last']}bp on {nv['last_date']} ({nv['trades']} trades), "
          f"{len(nv['series'])} days of history -> {OUT}")


if __name__ == "__main__":
    main()
