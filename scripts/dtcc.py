"""DTCC SEC public files with explicit coverage and current-day refresh status.

Files contain reports disseminated on the named UTC date, which can include
older executions. Today and the prior two UTC dates are refreshed online.
"""
from __future__ import annotations
import csv
import hashlib
import io
import os
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

BASE = "https://pddata.dtcc.com/ppd/api/report/cumulative/sec/SEC_CUMULATIVE_CREDITS_{}.zip"
CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cache")


def _url(d: date) -> str:
    return BASE.format(d.strftime("%Y_%m_%d"))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class DayFetch:
    rows: list[dict] | None
    metadata: dict


def _read(raw: bytes) -> list[dict]:
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    if not reader.fieldnames or "Dissemination Identifier" not in reader.fieldnames:
        raise ValueError("Unexpected DTCC CSV schema")
    return list(reader)


def fetch_day_result(d: date, use_cache: bool = True, *, today: date | None = None,
                     offline: bool = False, attempts: int = 3) -> DayFetch:
    today = today or datetime.now(timezone.utc).date()
    path = os.path.join(CACHE, f"{d:%Y_%m_%d}.csv")
    meta = {"date": d.isoformat(), "url": _url(d), "checked_at": utc_now(),
            "status": None, "error": None, "fetched_at": None,
            "cache_modified_at": None, "csv_sha256": None}
    cached = os.path.exists(path)
    if use_cache and cached and (d < today - timedelta(days=2) or offline):
        with open(path, "rb") as fh:
            raw = fh.read()
        rows = _read(raw)
        meta.update(status="cached", csv_sha256=hashlib.sha256(raw).hexdigest(), cache_modified_at=datetime.fromtimestamp(
            os.path.getmtime(path), timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"))
        return DayFetch(rows, meta)
    if offline:
        meta.update(status="offline_missing", error="Public file is not cached; network disabled for this run")
        return DayFetch(None, meta)
    failure = ""
    not_found = False
    for attempt in range(max(1, attempts)):
        try:
            req = urllib.request.Request(_url(d), headers={"User-Agent": "nvda-cds/2.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                blob = resp.read()
            with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
                if len(names) != 1:
                    raise ValueError("Expected one CSV in source archive")
                raw = zf.read(names[0])
            rows = _read(raw)
            os.makedirs(CACHE, exist_ok=True)
            temp = path + ".tmp"
            with open(temp, "wb") as fh:
                fh.write(raw)
            os.replace(temp, path)
            meta.update(status="downloaded", fetched_at=utc_now(), csv_sha256=hashlib.sha256(raw).hexdigest())
            return DayFetch(rows, meta)
        except urllib.error.HTTPError as exc:
            failure = f"HTTP {exc.code}"
            if exc.code == 404:
                not_found = True
                break
            if exc.code < 500 and exc.code != 429:
                break
        except (OSError, ValueError, zipfile.BadZipFile, UnicodeError) as exc:
            failure = f"{type(exc).__name__}: {exc}"
        if attempt + 1 < attempts:
            time.sleep(min(2 ** attempt, 4))
    meta.update(status="not_found" if not_found else "error", error=failure)
    if use_cache and cached:
        with open(path, "rb") as fh:
            raw = fh.read()
        rows = _read(raw)
        meta.update(status="stale_cache", csv_sha256=hashlib.sha256(raw).hexdigest(), cache_modified_at=datetime.fromtimestamp(
            os.path.getmtime(path), timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"))
        return DayFetch(rows, meta)
    return DayFetch(None, meta)


def fetch_day(d: date, use_cache: bool = True) -> list[dict] | None:
    """Compatibility reader; coverage-aware callers use fetch_day_result."""
    result = fetch_day_result(d, use_cache)
    if result.metadata["status"] in {"error", "stale_cache"}:
        raise OSError(result.metadata["error"])
    return result.rows


def business_days(start: date, end: date):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def calendar_days(start: date, end: date):
    """Weekend files can contain late reports and lifecycle messages too."""
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)
