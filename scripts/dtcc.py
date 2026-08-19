"""DTCC SEC SDR public dissemination - download & parse single-name CDS trades.

Source: https://pddata.dtcc.com/ppd/  (free, no auth, SEC security-based swap regime)
File:   SEC_CUMULATIVE_CREDITS_YYYY_MM_DD.zip -> one CSV of all disseminated
        credit trades for that trade date.
"""

from __future__ import annotations

import csv
import io
import os
import urllib.error
import urllib.request
import zipfile
from datetime import date, timedelta

BASE = "https://pddata.dtcc.com/ppd/api/report/cumulative/sec/SEC_CUMULATIVE_CREDITS_{}.zip"
CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cache")


def _url(d: date) -> str:
    return BASE.format(d.strftime("%Y_%m_%d"))


def fetch_day(d: date, use_cache: bool = True) -> list[dict] | None:
    """Return the day's rows, or None if DTCC has no file for that date."""
    path = os.path.join(CACHE, f"{d:%Y_%m_%d}.csv")
    if use_cache and os.path.exists(path):
        with open(path, encoding="utf-8-sig", newline="") as fh:
            return list(csv.DictReader(fh))

    req = urllib.request.Request(_url(d), headers={"User-Agent": "nvda-cds/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            blob = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise

    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        raw = zf.read(zf.namelist()[0])

    os.makedirs(CACHE, exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(raw)
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))


def business_days(start: date, end: date):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)
