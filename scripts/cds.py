"""ISDA-style conversion between a CDS upfront cash amount and a par spread.

DTCC disseminates most single-name CDS trades as a standard-coupon contract plus
an unsigned "upfront" cash amount (UFRO), not as a spread. To chart a spread we
have to invert the standard pricer:

    cash_to_buyer = (coupon - spread) * RPV01(spread) + accrued

with a flat hazard rate h = spread / (1 - R), R = 40%, quarterly ACT/360 coupons
on IMM dates, and flat discounting.

Validated against trades that report BOTH an upfront and an explicit spread
(see scripts/validate.py) - the inversion reproduces the reported spread to
well under a basis point.
"""

from __future__ import annotations

import math
from datetime import date

RECOVERY = 0.40
# Flat discount rate. Spread output moves ~0.4bp per 100bp of discount rate,
# so a constant is well inside the noise of the trade prints themselves.
DISCOUNT = 0.04
IMM_MONTHS = (3, 6, 9, 12)


def prev_imm(d: date) -> date:
    """Last IMM roll date (20 Mar/Jun/Sep/Dec) on or before d."""
    for m in reversed(IMM_MONTHS):
        cand = date(d.year, m, 20)
        if cand <= d:
            return cand
    return date(d.year - 1, 12, 20)


def next_imm(d: date) -> date:
    for m in IMM_MONTHS:
        cand = date(d.year, m, 20)
        if cand > d:
            return cand
    return date(d.year + 1, 3, 20)


def coupon_schedule(trade: date, maturity: date) -> list[tuple[date, date]]:
    """Accrual periods from the current IMM period through maturity."""
    periods = []
    start = prev_imm(trade)
    while start < maturity:
        end = min(next_imm(start), maturity)
        periods.append((start, end))
        start = next_imm(start)
    return periods


def accrued_fraction(trade: date, coupon: float) -> float:
    """Coupon accrued since the last IMM date, as a fraction of notional."""
    return coupon * (trade - prev_imm(trade)).days / 360.0


def rpv01(trade: date, maturity: date, spread: float) -> float:
    """Risky annuity (a.k.a. risky duration) per unit notional."""
    h = spread / (1.0 - RECOVERY)
    total = 0.0
    for start, end in coupon_schedule(trade, maturity):
        # the elapsed stub of the current period is handled by accrued_fraction()
        dt = (end - max(start, trade)).days / 360.0
        t_end = max((end - trade).days, 0) / 365.0
        t_start = max((start - trade).days, 0) / 365.0
        t_mid = 0.5 * (t_start + t_end)
        q_end = math.exp(-h * t_end)
        q_start = math.exp(-h * t_start)
        total += dt * math.exp(-DISCOUNT * t_end) * q_end
        # premium accrued between the last coupon and a default
        total += 0.5 * dt * math.exp(-DISCOUNT * t_mid) * (q_start - q_end)
    return total


def principal(trade: date, maturity: date, coupon: float, spread: float) -> float:
    """Clean upfront received by the protection buyer, per unit notional."""
    return (coupon - spread) * rpv01(trade, maturity, spread)


def spread_from_principal(
    trade: date, maturity: date, coupon: float, target: float
) -> float | None:
    """Invert principal() for the par spread. Bisection: principal is monotone
    decreasing in spread over the range we care about."""
    lo, hi = 1e-5, 0.30
    if principal(trade, maturity, coupon, lo) < target:
        return None
    if principal(trade, maturity, coupon, hi) > target:
        return None
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if principal(trade, maturity, coupon, mid) > target:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def spreads_from_upfront(
    trade: date, maturity: date, coupon: float, cash: float
) -> list[float]:
    """DTCC reports the upfront unsigned, so two spreads are consistent with it:
    one below the coupon (buyer received cash) and one above (buyer paid).
    Returns both candidates; the caller picks using the day's anchor."""
    accrued = accrued_fraction(trade, coupon)
    out = []
    for target in (cash - accrued, -(cash + accrued)):
        s = spread_from_principal(trade, maturity, coupon, target)
        if s is not None:
            out.append(s)
    return out
