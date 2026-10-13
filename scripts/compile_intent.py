#!/usr/bin/env python3
"""Intent DSL compiler: ``chart_spec`` + ``semantic_types`` -> chart config.

Flint's core idea, made concrete for the skill: the author/agent declares field
*semantics* and chart *intent* (chartType + channel bindings); a deterministic
compiler derives the low-level chart config (viewType, dimension, measures,
aggregation, time bucketing, per-series type/axis). The author never writes
ECharts options, series types, or axis indices by hand.

The output matches the internal chart shape ``spec_to_report`` already consumes,
so a chart expressed as a ``chartSpec`` compiles to the same config as a classic
``presetId``+``encoding`` chart (plus composite seriesType/seriesAxis).

DLS sketch
----------
::
    "semantic_types": {
      "订单日期": {"type": "DateTime"},
      "商品销售额": {"type": "Revenue", "unit": "元"},
      "实付金额":   {"type": "Revenue"},
      "客户地区":   {"type": "Category"}
    },
    "chartSpec": {
      "chartType": "mixed",
      "title": "月度销售与毛利",
      "encodings": {
        "x": {"field": "订单日期", "timeUnit": "month"},
        "y": [
          {"measure": "商品销售额", "series": "bar",  "axis": "left"},
          {"measure": "实付金额",   "series": "line", "axis": "right"}
        ]
      }
    }
"""
from __future__ import annotations

from typing import Any

from semantics.type_registry import resolve_t2, is_non_additive
from semantics.channel_semantics import channel_action
from semantics.shape_rules import shape_rule, effective_measure_max, supports_composite
from semantics import flint_core as fc
from semantics.recommend import recommend

# Flint chartType (semantic) -> skill viewType (ECharts backend family).
CHARTTYPE_TO_VIEW = {
    "mixed": "bar",
    "bar": "bar", "column": "bar",
    "stackedBar": "stackedBar", "stacked": "stackedBar",
    "line": "line", "area": "area", "stackedArea": "area",
    "pie": "pie", "donut": "donut",
    "funnel": "funnel", "scatter": "scatter", "table": "table",
}

# Default series glyph per viewType.
BASE_SERIES = {"bar": "bar", "stackedBar": "bar", "line": "line", "area": "line"}

# Flint recommend display name -> skill chartType key (CHARTTYPE_TO_VIEW).
RECOMMEND_TO_KEY = {
    "Line Chart": "line",
    "Bar Chart": "bar",
    "Scatter Plot": "scatter",
    "Pie Chart": "pie",
    "Donut Chart": "donut",
    "Grouped Bar Chart": "bar",
    "Stacked Bar Chart": "stackedBar",
}


class IntentError(ValueError):
    pass


def _norm_encoding(enc: Any) -> dict[str, Any]:
    if not isinstance(enc, dict):
        return {}
    return enc


def _resolve_measure_enc(m: Any) -> dict[str, Any]:
    if isinstance(m, str):
        return {"measure": m}
    if isinstance(m, dict):
        return m
    raise IntentError(f"invalid measure encoding: {m!r}")


def _build_profile_fields(table: dict | None, semantic_types: dict[str, Any]) -> list[dict[str, Any]]:
    """Field list for the Flint recommendation engine from the table + semantics.
    Values feed the data-inference fallback when a column has no semantic label."""
    if not table:
        return [{"name": k, "semLabel": (v.get("type") if isinstance(v, dict) else v)}
                for k, v in semantic_types.items()]
    cols = table.get("columns") or []
    rows = table.get("rows") or []
    if not cols:
        return []
    fields = []
    for j, col in enumerate(cols):
        sem = semantic_types.get(col)
        label = sem.get("type") if isinstance(sem, dict) else sem
        values = [r[j] for r in rows if j < len(r)]
        fields.append({"name": col, "semLabel": label, "values": values})
    return fields


def _dimension_kind(field: str, semantic_types: dict[str, Any], table: dict | None) -> str:
    """Axis kind from moved-in Flint semantics: 'time' | 'ordinal' | 'category'.
    Prefers the declared semantic type's visEncodings, then data inference."""
    sem = semantic_types.get(field)
    label = sem.get("type") if isinstance(sem, dict) else sem
    t = fc.get_type(fc.canonical_alias(label)) if label else None
    if t and t["visEncodings"]:
        enc = t["visEncodings"][0]
        if enc == "temporal":
            return "time"
        if enc == "ordinal":
            return "ordinal"
        if enc == "quantitative":
            return "measure"
        return "category"
    # No label -> data inference.
    if table:
        cols = table.get("columns") or []
        if field in cols:
            j = cols.index(field)
            vis = fc.infer_vis_category([r[j] for r in table.get("rows") or [] if j < len(r)])
            if vis == "temporal":
                return "time"
            if vis == "ordinal":
                return "ordinal"
            if vis == "quantitative":
                return "measure"
    return "category"


def compile_chart_spec(chart_spec: Any, semantic_types: dict[str, Any] | None,
                       table: dict | None = None) -> dict[str, Any]:
    """Compile a ``chart_spec`` (+ optional ``semantic_types``) into a chart config.

    Returns a dict compatible with ``spec_to_report``'s internal chart shape:
    ``{viewType, title, dimension, measures, aggregation, dimensionBucket?,
      roles?, seriesType?, seriesAxis?, limit?, sort?}``.
    """
    if not isinstance(chart_spec, dict):
        raise IntentError("chartSpec must be an object")
    semantic_types = semantic_types or {}
    enc = _norm_encoding(chart_spec.get("encodings"))
    chart_type = chart_spec.get("chartType") or "auto"
    ct_lower = str(chart_type).lower()
    if ct_lower in ("auto", "recommend", ""):
        # Flint recommendation (moved-in recommend.py): pick the top-scoring
        # chart type from declared semantics, so 'auto' intent is a real choice
        # driven by data — not a hardcoded default. Only chart types the skill
        # can render are candidates (heatmap etc. are skipped).
        recs = recommend(_build_profile_fields(table, semantic_types))
        chart_type = next((r["chartType"] for r in recs
                           if RECOMMEND_TO_KEY.get(r["chartType"])), "Bar Chart")
        ct_lower = RECOMMEND_TO_KEY.get(chart_type)
    view = CHARTTYPE_TO_VIEW.get(ct_lower)
    if view is None:
        raise IntentError(f"unsupported chartType: {chart_type!r}")

    out: dict[str, Any] = {"viewType": view, "title": chart_spec.get("title", "")}

    # ---- x (primary dimension) ----------------------------------------------
    x = _norm_encoding(enc.get("x"))
    dim = x.get("field")
    if not dim:
        raise IntentError("chartSpec.encodings.x.field is required")
    out["dimension"] = dim
    # Flint axis kind (time/ordinal/category) — drives default sort + axis type
    # in the rest of the compiler (moved-in flint_core.field_semantics).
    out["dimensionKind"] = _dimension_kind(dim, semantic_types, table)
    time_unit = x.get("timeUnit") or ((semantic_types.get(dim) or {}).get("timeUnit") if isinstance(semantic_types.get(dim), dict) else None)
    if time_unit:
        out["dimensionBucket"] = {"source": dim, "bucketed": dim, "bucket": time_unit}

    # ---- y (measures: 1..N) -------------------------------------------------
    y = enc.get("y") or []
    if isinstance(y, dict):
        # A single measure object (or {measures: [...]}).
        y = y.get("measures", [y]) if "measures" in y else [y]
    if not isinstance(y, list) or not y:
        raise IntentError("chartSpec.encodings.y (measures) is required")
    measures: list[str] = []
    series_type: dict[str, str] = {}
    series_axis: dict[str, int] = {}
    for item in y:
        m = _resolve_measure_enc(item)
        name = m.get("measure")
        if not name:
            raise IntentError("each measure encoding needs a 'measure' field")
        measures.append(name)
        series = m.get("series")
        if series in ("bar", "line"):
            series_type[name] = series
        axis = m.get("axis") or m.get("yAxis")
        if axis in ("left", 0, "0"):
            series_axis[name] = 0
        elif axis in ("right", 1, "1"):
            series_axis[name] = 1
    out["measures"] = measures
    if series_type:
        out["seriesType"] = series_type
    if series_axis:
        out["seriesAxis"] = series_axis

    # ---- aggregation ---------------------------------------------------------
    preferred_agg = chart_spec.get("aggregation")
    # For a measure that is non-additive (percent/unitless) default to average;
    # a category dragged into measures degrades to count (channel semantics).
    agg = preferred_agg or "sum"
    non_additive_measures = [m for m in measures if is_non_additive(
        (semantic_types.get(m) or {}).get("type") if isinstance(semantic_types.get(m), dict) else None)]
    if non_additive_measures and agg == "sum":
        agg = "average"
    out["aggregation"] = agg

    # ---- breakdown (optional 2nd dimension, cross-tab) -----------------------
    bd = _norm_encoding(enc.get("breakdown")).get("field")
    if bd:
        out["roles"] = {"breakdown": bd}

    # ---- limit / topK --------------------------------------------------------
    if chart_spec.get("limit"):
        out["limit"] = chart_spec["limit"]
    if chart_spec.get("topK"):
        out["topK"] = chart_spec["topK"]
    if chart_spec.get("sort"):
        out["sort"] = chart_spec["sort"]

    # ---- shape check (composite budget etc.) ---------------------------------
    dims_count = 2 if bd else 1
    cap = effective_measure_max(view, dims_count)
    if len(measures) > cap:
        raise IntentError(
            f"{chart_type} supports at most {cap} measures with {dims_count} dimension(s); got {len(measures)}")
    # A composite (bar+line) is only meaningful on a cartesian single-dimension.
    if out.get("seriesType") and not supports_composite(view):
        raise IntentError(f"{chart_type} does not support per-measure series types")
    return out


def resolve_field_kind(semantic_types: dict[str, Any], column: str) -> dict[str, Any]:
    """Best-effort field kind from a semantic_types entry (Flint field-semantics)."""
    st = semantic_types.get(column)
    if isinstance(st, dict):
        t = st.get("type")
        canon = resolve_t2(t)
        t2 = canon["type"] if isinstance(canon, dict) else canon
        if t2 in {"currency", "price", "percent", "count", "duration", "number", "unitless"}:
            return {"kind": "measure", "measureType": t2, "nonAdditive": is_non_additive(t2)}
        low = str(t).lower()
        if low in ("datetime", "date", "time", "month", "year", "timestamp"):
            return {"kind": "time", "measureType": None, "nonAdditive": False}
        return {"kind": "category", "measureType": None, "nonAdditive": False}
    if isinstance(st, str):
        # 字符串语义（validate_request 归一后的 canonical type）：包一层再走
        # dict 分支。原样自递归参数不变 = 必然 RecursionError。
        return resolve_field_kind({column: {"type": st}}, column)
    return {"kind": "category", "measureType": None, "nonAdditive": False}
