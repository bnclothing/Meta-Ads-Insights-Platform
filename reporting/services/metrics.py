from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Iterable


ZERO = Decimal("0")


def decimal_value(value, default: Decimal | None = ZERO) -> Decimal | None:
    if value in (None, ""):
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default


def int_value(value, default: int = 0) -> int:
    try:
        return int(Decimal(str(value or 0)))
    except (InvalidOperation, TypeError, ValueError):
        return default


def safe_divide(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    if not denominator:
        return None
    return numerator / denominator


def percent_change(current: Decimal, baseline: Decimal) -> Decimal | None:
    if not baseline:
        return None
    return ((current - baseline) / baseline) * Decimal("100")


def average(values: Iterable[Decimal]) -> Decimal:
    materialized = list(values)
    return sum(materialized, ZERO) / Decimal(len(materialized)) if materialized else ZERO


def aggregate_rows(rows) -> dict:
    rows = list(rows)
    spend = sum((row.spend for row in rows), ZERO)
    # A missing result is not a zero-result day.  Once a period contains an
    # unavailable result metric, its aggregate result and derived CPL remain
    # unavailable until Meta returns a complete value.
    results_complete = all(row.results is not None for row in rows)
    results = sum((row.results for row in rows), ZERO) if rows and results_complete else (ZERO if not rows else None)
    impressions = sum((row.impressions for row in rows), 0)
    reach = sum((row.reach for row in rows), 0)
    clicks = sum((row.clicks for row in rows), 0)
    link_clicks = sum((row.link_clicks for row in rows), 0)
    return {
        "spend": spend,
        "results": results,
        "impressions": impressions,
        "reach": reach,
        "clicks": clicks,
        "link_clicks": link_clicks,
        "cost_per_result": safe_divide(spend, results) if results is not None else None,
        "ctr": safe_divide(Decimal(clicks) * Decimal("100"), Decimal(impressions)),
        "cpc": safe_divide(spend, Decimal(clicks)),
        "cpm": safe_divide(spend * Decimal("1000"), Decimal(impressions)),
    }
