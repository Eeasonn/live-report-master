"""Data-truth validation: re-aggregate declared chart values from the source dataset.

Same contract as the original skill's `truth.mapping`: declare per-measure
recompute rules in the chart manifest, then verify them against the live
SQL aggregation. The server is faster than the browser path because the
heavy lifting is pushed into SQL on the in-memory table.
"""
from __future__ import annotations

from typing import Any

from .aggregator import parse_request
from .store import Engine


def validate_truth(
    engine: Engine,
    dataset_id: str,
    truth: dict[str, Any],
    declared_series: list[dict[str, Any]],
    tolerance: float = 0.001,
) -> dict[str, Any]:
    """Validate a chart's declared numbers against the source dataset.

    Args:
        engine: data engine (table must already be loaded under dataset_id).
        truth: chart.truth mapping:
          {
            "dimension": "地区",
            "aggregation": "sum",
            "mapping": {
              "报告列": {"column": "数据源列", "aggregation": "sum|count|...|__count__"}
            }
          }
        declared_series: the chart's option.series data points to compare against.
            Each entry is a dict like {"name": "华东", "value": 1234.5} or
            {"x": "华东", "y": 1234.5}, etc. The first numeric field is taken.
        tolerance: relative tolerance; |delta| / max(|declared|, |recomputed|) <= tol.

    Returns:
        {
            "results": [{"dimension": ..., "declared": ..., "recomputed": ..., "ok": bool}, ...],
            "allOk": bool,
            "tolerance": float,
            "elapsedMs": int,
        }
    """
    import time
    start = time.monotonic()

    dimension = truth.get("dimension")
    if not dimension:
        return {"results": [], "allOk": True, "tolerance": tolerance, "elapsedMs": 0,
                "skipped": "no dimension declared"}

    mapping = truth.get("mapping") or {}
    if not mapping:
        return {"results": [], "allOk": True, "tolerance": tolerance, "elapsedMs": 0,
                "skipped": "no mapping declared"}

    # Build aggregate request: 1 dimension, N measures per mapping
    measures: list[dict[str, Any]] = []
    req_col_by_report: dict[str, str] = {}
    for report_col, src in mapping.items():
        src_col = src.get("column", report_col)
        src_agg = src.get("aggregation") or truth.get("aggregation") or "sum"
        if src_col == "__count__":
            src_agg = "count"
            # COUNT(*) 走 aggregator 的合成行数列 "*"（count 限定）：旧占位
            # report_col 会因「报告列不在数据集」在 aggregate 校验层 400。
            src_col = "*"
        req_col_by_report[report_col] = src_col
        measures.append({
            "column": report_col,        # report-side name (used as result column alias)
            "aggregation": src_agg,
            "sourceColumn": src_col,     # meta; ignored by aggregator
        })

    payload = {
        "groupBy": [dimension],
        "measures": [{"column": m["sourceColumn"], "aggregation": m["aggregation"]} for m in measures],
    }
    request = parse_request(payload)
    response = engine_aggregate(engine, dataset_id, request)

    # Build a lookup by dimension value
    index_by_label: dict[str, dict[str, float]] = {}
    for row in response.rows:
        label = str(row[0]) if row else ""
        values: dict[str, float] = {}
        for i, col_name in enumerate(response.columns[1:]):
            # 同一 sourceColumn 被声明成多个指标（不同聚合）时，响应列名重复——
            # 首胜不覆盖：后写会静默顶掉先声明的聚合读数（调试端点也要确定性）。
            if col_name in values:
                continue
            try:
                values[col_name] = float(row[i + 1]) if row[i + 1] is not None else 0.0
            except (TypeError, ValueError):
                values[col_name] = 0.0
        index_by_label[label] = values

    # Compare against declared series
    results: list[dict[str, Any]] = []
    for entry in declared_series:
        label = _declared_label(entry)
        if label is None:
            continue
        declared_values = _declared_values(entry)
        if declared_values is None:
            continue
        recomputed = index_by_label.get(str(label), {})

        # Walk through each declared measure column
        for report_col in mapping.keys():
            # declared_values holds scalar(s) under whatever name the series
            # shape used ('value', 'y', or column index). Pick the first one.
            declared_v = next(iter(declared_values.values()), None)
            if declared_v is None:
                continue
            recomp_v = recomputed.get(req_col_by_report.get(report_col, report_col), 0.0)
            ok = _within_tolerance(declared_v, recomp_v, tolerance)
            results.append({
                "dimension": str(label),
                "column": report_col,
                "declared": declared_v,
                "recomputed": recomp_v,
                "ok": ok,
            })

    all_ok = all(r["ok"] for r in results) if results else True
    elapsed = int((time.monotonic() - start) * 1000)
    return {
        "results": results,
        "allOk": all_ok,
        "tolerance": tolerance,
        "elapsedMs": elapsed,
    }


# ---------------- helpers ---------------------------------------------------------

def engine_aggregate(engine: Engine, table_name: str, request):
    """Thin wrapper around aggregator.aggregate, kept here to avoid import cycles."""
    from .aggregator import aggregate
    return aggregate(engine, table_name, request)


def _declared_label(entry: Any) -> str | None:
    """Pull the dimension label out of a series entry."""
    if isinstance(entry, dict):
        for key in ("name", "x", "category", "label"):
            if key in entry:
                return entry[key]
    return None


def _declared_value(v: Any) -> float | None:
    """null-safe float for one series data point.

    A compiled series legitimately carries ``null`` cells (window/rate gaps and
    the BI-H3 merged 「其他」 row's non-additive measures —
    ``spec_to_report._series_value`` is null-preserving by contract): there is
    no number to verify, so the caller skips the point. ``float(None)`` used to
    raise TypeError and turn the whole truth request into a 500, while the
    recompute side already mapped NULL → 0.0.
    """
    if v is None or isinstance(v, bool) or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def _declared_values(entry: Any) -> dict[str, float] | None:
    """Pull the numeric value(s) out of a series entry.

    Handles the common shapes:
      {"name": "A", "value": 123}
      {"x": "A", "y": 123}
      ["A", 123]
    A null/blank/unparseable point returns None (nothing to verify).
    """
    if isinstance(entry, dict):
        if "value" in entry:
            v = _declared_value(entry["value"])
            return {"value": v} if v is not None else None
        if "y" in entry:
            v = _declared_value(entry["y"])
            return {"y": v} if v is not None else None
    if isinstance(entry, (list, tuple)) and len(entry) >= 2:
        v = _declared_value(entry[1])
        return {"y": v} if v is not None else None
    return None


def _source_col_for(report_col: str, mapping: dict[str, Any]) -> str:
    """Reverse-lookup: which source column was reported under `report_col`."""
    src = mapping.get(report_col) or {}
    return src.get("column", report_col)


def _within_tolerance(a: float, b: float, tol: float) -> bool:
    if a == b:
        return True
    denom = max(abs(a), abs(b), 1e-12)
    return abs(a - b) / denom <= tol