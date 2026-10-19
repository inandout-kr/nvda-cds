"""Build an auditable direct-report-only CDS dataset, never a modeled spread.

Unknown units/economic meaning stay null. Daily values, if verified, are
medians in a 4–6 remaining-year bucket, not a synthetic 5Y benchmark.
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import math
import os
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dtcc import calendar_days, fetch_day_result, utc_now

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "data", "cds.json")
STATE_OUT = os.path.join(ROOT, "docs", "data", "report-state.json")
HISTORY_START = date(2025, 10, 1)
SCHEMA_VERSION = 2
DATA_POLICY = "direct-reported-only"
TIMESTAMP_POLICY = "event-time-is-not-publication-time"
WATCHLIST = [
    ("NVIDIA", "NVIDIA", "NVDA", {"NVIDIACORP", "NVIDIACORPORATION"}),
    ("ORACLE", "Oracle", "ORCL", {"ORACLECORPORATION", "ORACLECORP"}),
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
FISN = "NA/CDS Corp SN Sr"
TENOR_MIN, TENOR_MAX = 4.0, 6.0
ENTITY_OF = {alias: key for key, _, _, aliases in WATCHLIST for alias in aliases}

# Fail closed. Both unit AND economic meaning must be verified in the actual
# DTCC SEC product feed before a rule may be added. Another SDR is insufficient.
NOTATION_RULES: dict[str, dict] = {}
NOTATION_VERIFICATION = {
    "status": "unverified",
    "reference_url": "https://dtcclearning.com/products-and-services/repository-services/gtr-cftc-canada-sec.html",
    "checked_at": "2026-10-19",
    "detail": "Current DTCC SEC Message Specification and PPD Transformation/Scaling Help require login. Public raw Spread-Leg notation and economic meaning are not both certified; no basis-point value is inferred.",
    "public_guide_url": "https://kgc0418-tdw-data-0.s3.amazonaws.com/gtr/static/gtr/docs/RT_PPD_quick_ref_guide.pdf",
}
RAW_FIELDS = (
    "Dissemination Identifier", "Original Dissemination Identifier", "Action type",
    "Event type", "Event timestamp", "Amendment indicator", "Execution Timestamp",
    "Effective Date", "Expiration Date", "Underlying Asset Name", "UPI FISN",
    "UPI Underlier Name", "Unique Product Identifier", "Underlier ID-Leg 1",
    "Underlier ID source-Leg 1", "Notional currency-Leg 1", "Notional currency-Leg 2",
    "Spread-Leg 1", "Spread-Leg 2", "Spread notation-Leg 1", "Spread notation-Leg 2",
    "Spread currency-Leg 1", "Spread currency-Leg 2", "Fixed rate-Leg 1", "Fixed rate-Leg 2",
    "Other payment amount", "Other payment type", "Other payment currency",
    "Notional amount-Leg 1", "Notional amount-Leg 2", "Price", "Price notation",
    "Package indicator", "Non-standardized term indicator",
)


def norm(name: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (name or "").upper())


def number(raw: str | None) -> tuple[float | None, str | None]:
    text = (raw or "").strip()
    if not text:
        return None, "missing"
    try:
        value = float(text.replace(",", ""))
    except (ValueError, OverflowError):
        return None, "invalid"
    if not math.isfinite(value):
        return None, "nonfinite"
    if not re.fullmatch(r"[+-]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", text):
        return None, "invalid"
    return value, None


def timestamp(raw: str | None) -> datetime | None:
    try:
        value = datetime.fromisoformat((raw or "").replace("Z", "+00:00"))
        return value.astimezone(timezone.utc) if value.tzinfo else None
    except ValueError:
        return None


def iso(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value else None


def state_record(row: dict, source: dict) -> dict:
    return {"raw": {field: (row.get(field) or "").strip() for field in RAW_FIELDS},
            "source_files": [{"date": source["date"], "url": source["url"]}]}


def identity(record: dict) -> str:
    return hashlib.sha256(json.dumps(record["raw"], sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def deduplicate(records: list[dict]) -> list[dict]:
    unique = {}
    for record in records:
        digest = identity(record)
        if digest not in unique:
            unique[digest] = copy.deepcopy(record)
        else:
            seen = {s["date"] for s in unique[digest]["source_files"]}
            unique[digest]["source_files"].extend(s for s in record["source_files"] if s["date"] not in seen)
    for record in unique.values():
        record["source_files"].sort(key=lambda s: s["date"])
    return sorted(unique.values(), key=lambda r: (r["raw"].get("Event timestamp", ""), identity(r)))


def retain_relevant(records: list[dict], extra: list[dict]) -> list[dict]:
    """Preserve exact entities and connected lifecycle rows, including sparse ones."""
    selected = list(records)
    pending = []
    for record in extra:
        if norm(record["raw"].get("Underlying Asset Name")) in ENTITY_OF:
            selected.append(record)
        elif record["raw"].get("Original Dissemination Identifier"):
            pending.append(record)
    known = {r["raw"].get(field) for r in selected for field in (
        "Dissemination Identifier", "Original Dissemination Identifier") if r["raw"].get(field)}
    while pending:
        remaining = []
        for record in pending:
            ids = {record["raw"].get(field) for field in (
                "Dissemination Identifier", "Original Dissemination Identifier")} - {None, ""}
            if ids & known:
                selected.append(record)
                known.update(ids)
            else:
                remaining.append(record)
        if len(remaining) == len(pending):
            break
        pending = remaining
    return deduplicate(selected)


def lifecycle_reasons(records: list[dict]) -> dict[str, list[str]]:
    """Exclude amended/error chains, not an unverified replacement or both rows."""
    parent = {}
    def find(item):
        parent.setdefault(item, item)
        if parent[item] != item:
            parent[item] = find(parent[item])
        return parent[item]
    def union(a, b):
        parent[find(a)] = find(b)
    ids_to_records = defaultdict(list)
    for record in records:
        raw = record["raw"]
        rid = raw.get("Dissemination Identifier")
        oid = raw.get("Original Dissemination Identifier")
        node = rid or "missing:" + identity(record)
        find(node)
        if oid:
            union(node, oid)
        if rid:
            ids_to_records[rid].append(record)
    blocked = defaultdict(set)
    for record in records:
        raw = record["raw"]
        rid = raw.get("Dissemination Identifier")
        root = find(rid or "missing:" + identity(record))
        if not rid:
            blocked[root].add("missing_report_id")
        if raw.get("Original Dissemination Identifier") or raw.get("Action type") != "NEWT" or raw.get("Amendment indicator") not in {"", "N", "FALSE", "false"}:
            blocked[root].add("amended_lifecycle_unverified")
        if raw.get("Action type") == "EROR":
            blocked[root].add("error_lifecycle")
    for rid, group in ids_to_records.items():
        if len(group) > 1:
            blocked[find(rid)].add("conflicting_duplicate_id")
    return {identity(record): sorted(blocked[find(record["raw"].get("Dissemination Identifier") or "missing:" + identity(record))])
            for record in records}


def inspect_record(record: dict, lifecycle: list[str] | None = None,
                   rules: dict[str, dict] | None = None) -> dict:
    raw = record["raw"]
    reasons = list(lifecycle or [])
    key = ENTITY_OF.get(norm(raw.get("Underlying Asset Name")))
    currencies = {raw.get(f"Notional currency-Leg {i}") for i in (1, 2)} - {None, ""}
    currency = next(iter(currencies)) if len(currencies) == 1 else "/".join(sorted(currencies)) or None
    scope_reasons = []
    if not key:
        scope_reasons.append("unmatched_exact_entity")
    if raw.get("UPI FISN") != FISN:
        scope_reasons.append("not_senior_corporate_cds")
    if currencies != {"USD"}:
        scope_reasons.append("not_usd")
    execution = timestamp(raw.get("Execution Timestamp"))
    event_time = timestamp(raw.get("Event timestamp"))
    maturity = None
    try:
        maturity = date.fromisoformat(raw.get("Expiration Date", ""))
    except ValueError:
        scope_reasons.append("invalid_maturity")
    if not execution:
        scope_reasons.append("invalid_execution_timestamp")
    years = (maturity - execution.date()).days / 365.25 if maturity and execution else None
    if years is not None and not TENOR_MIN <= years <= TENOR_MAX:
        scope_reasons.append("outside_4_6_year_bucket")
    reasons.extend(scope_reasons)
    if not event_time:
        reasons.append("invalid_source_event_timestamp")
    if raw.get("Action type") != "NEWT" or raw.get("Event type") != "TRAD":
        reasons.append("not_new_trade_report")
    if raw.get("Package indicator", "").upper() not in {"", "FALSE", "N"}:
        reasons.append("package_trade_unverified")
    values = []
    legs = []
    rules = NOTATION_RULES if rules is None else rules
    for leg in (1, 2):
        value_raw = raw.get(f"Spread-Leg {leg}", "")
        notation = raw.get(f"Spread notation-Leg {leg}", "")
        spread_currency = raw.get(f"Spread currency-Leg {leg}", "")
        legs.append({"leg": leg, "raw": value_raw or None, "notation": notation or None,
                     "currency": spread_currency or None})
        if not value_raw:
            continue
        value, error = number(value_raw)
        if error:
            reasons.append(f"{error}_reported_spread")
            continue
        rule = rules.get(notation)
        if not rule:
            reasons.append("unverified_spread_notation")
            continue
        if not rule.get("economic_meaning_verified"):
            reasons.append("unverified_spread_economic_meaning")
            continue
        if spread_currency and spread_currency != "USD":
            reasons.append("spread_currency_mismatch")
            continue
        converted = value * rule["multiplier_to_bp"]
        if not math.isfinite(converted):
            reasons.append("nonfinite_reported_spread")
            continue
        values.append(converted)
    if not any(leg["raw"] is not None for leg in legs):
        reasons.append("no_reported_spread")
    if len(set(values)) > 1:
        reasons.append("ambiguous_multi_leg_spread")
    reasons = sorted(set(reasons))
    return {
        "entity": key, "report_id": raw.get("Dissemination Identifier") or None,
        "original_report_id": raw.get("Original Dissemination Identifier") or None,
        "action": raw.get("Action type"), "event": raw.get("Event type"),
        "entity_name": raw.get("Underlying Asset Name") or None,
        "entity_alias": norm(raw.get("Underlying Asset Name")), "currency": currency,
        "execution_timestamp": iso(execution), "source_event_timestamp": iso(event_time),
        "source_event_timestamp_field": "Event timestamp",
        "dissemination_timestamp": None, "publication_delay_seconds": None,
        "publication_delay_status": "unavailable_no_dissemination_timestamp",
        "maturity_date": maturity.isoformat() if maturity else None,
        "remaining_years": round(years, 6) if years is not None else None,
        "upi_fisn": raw.get("UPI FISN") or None, "upi": raw.get("Unique Product Identifier") or None,
        "underlier_id": raw.get("Underlier ID-Leg 1") or None,
        "underlier_id_source": raw.get("Underlier ID source-Leg 1") or None,
        "upi_underlier_name": raw.get("UPI Underlier Name") or None,
        "documentation": None, "documentation_status": "not_available_in_selected_public_fields",
        "spread_legs": legs,
        "fixed_rates": [raw.get(f"Fixed rate-Leg {i}") or None for i in (1, 2)],
        "upfront": {"raw": raw.get("Other payment amount") or None,
                    "type": raw.get("Other payment type") or None,
                    "currency": raw.get("Other payment currency") or None},
        "spread_bp": values[0] if values and not reasons else None,
        "quality_reasons": reasons, "scope_eligible": not scope_reasons,
        "lifecycle_status": "excluded" if lifecycle else "unamended_new_report",
        "source_files": record["source_files"],
    }


def compatible(previous: dict) -> bool:
    return (previous.get("schema_version") == SCHEMA_VERSION
            and previous.get("data_policy") == DATA_POLICY
            and isinstance(previous.get("report_state"), list))


def collect(start: date, end: date, previous: dict | None = None, *,
            offline: bool = False, fetcher=None, today: date | None = None) -> tuple[list[dict], list[dict]]:
    previous = previous if previous and compatible(previous) else {}
    records = copy.deepcopy(previous.get("report_state", []))
    old_files = {f["date"]: f for f in previous.get("coverage", {}).get("files", [])}
    new_records = []
    fetcher = fetcher or fetch_day_result
    for day in calendar_days(start, end):
        result = fetcher(day, today=today or end, offline=offline)
        meta = result.metadata
        old_files[day.isoformat()] = meta
        if result.rows is None:
            continue
        if meta["status"] in {"downloaded", "cached"}:
            for record in records:
                record["source_files"] = [s for s in record["source_files"] if s["date"] != day.isoformat()]
            records = [r for r in records if r["source_files"]]
        source = {"date": day.isoformat(), "url": meta["url"]}
        for row in result.rows:
            if norm(row.get("Underlying Asset Name")) in ENTITY_OF or row.get("Original Dissemination Identifier"):
                new_records.append(state_record(row, source))
        print(f"{day.isoformat()} {meta['status']} {len(result.rows)} rows", file=sys.stderr)
    return retain_relevant(records, new_records), sorted(old_files.values(), key=lambda f: f["date"])


def change(series: list, days_back: int) -> float | None:
    if not series:
        return None
    cutoff = date.fromisoformat(series[-1][0]) - timedelta(days=days_back)
    earlier = [p for p in series[:-1] if date.fromisoformat(p[0]) <= cutoff]
    return round(series[-1][1] - earlier[-1][1], 4) if earlier else None


def build_payload(records: list[dict], files: list[dict], *, start: date,
                  end: date, now: str | None = None, rules=None) -> dict:
    records = deduplicate(records)
    lifecycle = lifecycle_reasons(records)
    reports = [inspect_record(r, lifecycle.get(identity(r)), rules) for r in records]
    entities = {}
    for key, name, ticker, _ in WATCHLIST:
        all_reports = sorted([r for r in reports if r["entity"] == key],
                             key=lambda r: (r["source_event_timestamp"] or "", r["report_id"] or ""), reverse=True)
        qualified = [r for r in all_reports if r["scope_eligible"]]
        raw_spread_reports = [r for r in qualified if r["action"] == "NEWT" and r["event"] == "TRAD"
                              and r["lifecycle_status"] == "unamended_new_report"
                              and any(leg["raw"] is not None for leg in r["spread_legs"])]
        eligible = [r for r in qualified if r["spread_bp"] is not None]
        days = defaultdict(list)
        for report in eligible:
            days[report["execution_timestamp"][:10]].append(report)
        series = [[day, round(statistics.median(r["spread_bp"] for r in group), 4), len(group)]
                  for day, group in sorted(days.items())]
        latest_group = days[series[-1][0]] if series else []
        last_trade = max(latest_group, key=lambda r: r["execution_timestamp"]) if latest_group else None
        quality = Counter(reason for r in qualified for reason in r["quality_reasons"])
        entities[key] = {
            "name": name, "ticker": ticker, "series": series,
            "last": series[-1][1] if series else None,
            "last_date": series[-1][0] if series else None,
            "trades": series[-1][2] if series else 0,
            "chg_1d": change(series, 1), "chg_1w": change(series, 7), "chg_1m": change(series, 30),
            "last_trade_timestamp": last_trade["execution_timestamp"] if last_trade else None,
            "last_source_event_timestamp": max((r["source_event_timestamp"] for r in latest_group), default=None),
            "last_dissemination_timestamp": None, "last_publication_delay_seconds": None,
            "latest_report": qualified[0] if qualified else None,
            "latest_spread_report": max(raw_spread_reports, key=lambda r: (r["execution_timestamp"] or "", r["source_event_timestamp"] or ""), default=None),
            "recent_reports": qualified[:40],
            "recent_excluded_scope_reports": [r for r in all_reports if not r["scope_eligible"]][:10],
            "quality_counts": dict(sorted(quality.items())), "eligible_report_count": len(eligible),
            "qualifying_report_count": len(qualified), "exact_entity_report_count": len(all_reports),
            "scope_exclusion_counts": dict(Counter(reason for r in all_reports if not r["scope_eligible"] for reason in r["quality_reasons"])),
        }
    successful = [f["date"] for f in files if f["status"] in {"cached", "downloaded"}]
    failures = [f for f in files if f["status"] in {"error", "stale_cache", "offline_missing"}]
    return {
        "schema_version": SCHEMA_VERSION, "data_policy": DATA_POLICY,
        "timestamp_policy": TIMESTAMP_POLICY,
        "updated": now or utc_now(), "entities": entities,
        "missing_entities": [key for key, entity in entities.items() if entity["last"] is None],
        "methodology_details": {
            "label": "Reported CDS spread", "aggregation": "Daily median of eligible direct public reports by UTC execution date; count means reports, not independently verified unique economic trades.",
            "maturity_bucket": "4–6 remaining years at execution; no synthetic 5Y normalization",
            "seniority": FISN, "currency": "USD",
            "notation_verification": copy.deepcopy(NOTATION_VERIFICATION),
            "lifecycle_policy": "Identical repeated reports deduplicated; conflicting IDs and entire amended/error chains excluded conservatively.",
            "timestamps": "Event timestamp is the source event time, not a dissemination/publication time. The selected public fields contain no certified dissemination timestamp; publication latency is unavailable and is never derived from event minus execution time.",
            "record_order": "Latest/recent report records are ordered by source_event_timestamp, not public dissemination order. latest_spread_report is selected by execution_timestamp, with source event time only as a tie-breaker.",
            "source_file_date": "source_files.date identifies the UTC day of the cumulative public dissemination file; it does not establish a precise dissemination timestamp or publication latency.",
            "limitations": ["Public reports are not a dealer close or authenticated par-spread quotation.",
                            "Documentation clause is not established by selected public fields; no cross-documentation adjustment is made.",
                            "No certified dissemination timestamp is available in the selected source fields; publication delay remains null regardless of event and execution timestamps.",
                            "Online runs refresh today and the prior two UTC source dates; older cached snapshots can miss later source republications until refreshed explicitly.",
                            "Build timestamp does not imply a fresh source file or recent trade."]},
        "coverage": {"start": start.isoformat(), "end": end.isoformat(),
                     "last_successful_file_date": max(successful) if successful else None,
                     "missing_files": [f["date"] for f in files if f["status"] == "not_found"],
                     "failed_files": [{"date": f["date"], "status": f["status"], "error": f.get("error")} for f in failures],
                     "files": files},
        "report_state": records,
    }


def encode_json(payload: dict) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")


def write_artifacts(payload: dict, out: str = OUT, state_out: str = STATE_OUT) -> None:
    """Keep the dashboard small; retain complete public audit state separately."""
    state = {"schema_version": SCHEMA_VERSION, "data_policy": DATA_POLICY,
             "report_state": payload["report_state"]}
    raw_state = encode_json(state)
    published = {key: value for key, value in payload.items() if key != "report_state"}
    published.update(report_state_file=os.path.basename(state_out),
                     report_state_sha256=hashlib.sha256(raw_state).hexdigest(),
                     report_state_count=len(state["report_state"]))
    for path, blob in ((state_out, raw_state), (out, encode_json(published))):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temporary = path + ".tmp"
        with open(temporary, "wb") as fh:
            fh.write(blob)
        os.replace(temporary, path)


def load_artifacts(path: str = OUT) -> dict:
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    if "report_state" in payload:
        return payload
    state_name = payload.get("report_state_file")
    if state_name != "report-state.json":
        return payload  # Legacy schema will trigger a whole migration.
    state_path = os.path.join(os.path.dirname(path), state_name)
    with open(state_path, "rb") as fh:
        blob = fh.read()
    if hashlib.sha256(blob).hexdigest() != payload.get("report_state_sha256"):
        raise ValueError("Retained report-state hash does not match dashboard artifact")
    state = json.loads(blob)
    if not compatible(state):
        raise ValueError("Retained report-state policy/schema mismatch")
    payload["report_state"] = state["report_state"]
    return payload


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--full", action="store_true", help="Rebuild all history, discarding any modeled series")
    ap.add_argument("--since", help="First source-file date YYYY-MM-DD")
    ap.add_argument("--end", help="Last source-file date (default current UTC date)")
    ap.add_argument("--offline", action="store_true", help="Use cached public files and disclose gaps")
    args = ap.parse_args()
    today = datetime.now(timezone.utc).date()
    end = date.fromisoformat(args.end) if args.end else today
    previous = {}
    if not args.full and os.path.exists(OUT):
        old = load_artifacts(OUT)
        if compatible(old):
            previous = old
        else:
            print("Legacy state discarded; rebuilding direct-only history", file=sys.stderr)
    start = (date.fromisoformat(args.since) if args.since else
             HISTORY_START if args.full or not previous else end - timedelta(days=10))
    records, files = collect(start, end, previous, offline=args.offline, today=today)
    coverage_start = min([start] + [date.fromisoformat(f["date"]) for f in files])
    payload = build_payload(records, files, start=coverage_start, end=end)
    write_artifacts(payload)
    print(f"{len(records)} retained source reports; {sum(e['eligible_report_count'] for e in payload['entities'].values())} eligible direct spreads; {len(payload['missing_entities'])} missing entities -> {OUT}")


if __name__ == "__main__":
    main()
