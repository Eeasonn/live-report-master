#!/usr/bin/env python3
"""Flint semantic core, ported INTO the skill (single codebase).

This is a faithful Python port of the *defining* Flint semantics from
``refs/flint/src/core`` (type-registry.ts / semantic-types.ts), so the skill
internally **is** a Flint implementation (ECharts backend) rather than a thin
wrapper around an external package. It is the single source of truth for:

  - axis/encoding family per semantic type (``visEncodings``: temporal/ordinal/
    nominal/quantitative/geographic) — the "连时间/有序/无序分类" axis type.
  - aggregation role (``aggRole``: additive vs intensive) — what may be summed.
  - zero-baseline (``zeroBaseline`` + ``zeroPad``) — whether a value axis starts
    at 0 or is padded.
  - format class, domain shape, diverging.
  - data-only inference (``infer_vis_category``) when no semantic label is given.

The port keeps Flint's field values verbatim (t0/t1/visEncodings/aggRole/
domainShape/diverging/formatClass/zeroBaseline/zeroPad) so the rules are
traceable to ``refs/flint/src/core/type-registry.ts``.
"""
from __future__ import annotations

from typing import Any

# Ported from refs/flint/src/core/type-registry.ts (subset the skill needs).
FLINT_TYPES: dict[str, dict[str, Any]] = {
    # Temporal
    "DateTime":   {"t0": "Temporal", "t1": "DateTime", "visEncodings": ["temporal"],
                   "aggRole": "dimension", "domainShape": "open", "diverging": "none",
                   "formatClass": "plain", "zeroBaseline": "none", "zeroPad": 0},
    "Year":       {"t0": "Temporal", "t1": "DateGranule", "visEncodings": ["temporal", "ordinal"],
                   "aggRole": "dimension", "domainShape": "open", "diverging": "none",
                   "formatClass": "integer", "zeroBaseline": "arbitrary", "zeroPad": 0.03},
    "Month":      {"t0": "Temporal", "t1": "DateGranule", "visEncodings": ["ordinal"],
                   "aggRole": "dimension", "domainShape": "cyclic", "diverging": "none",
                   "formatClass": "plain", "zeroBaseline": "none", "zeroPad": 0},
    "Week":       {"t0": "Temporal", "t1": "DateGranule", "visEncodings": ["ordinal"],
                   "aggRole": "dimension", "domainShape": "cyclic", "diverging": "none",
                   "formatClass": "plain", "zeroBaseline": "none", "zeroPad": 0},
    "Day":        {"t0": "Temporal", "t1": "DateGranule", "visEncodings": ["ordinal"],
                   "aggRole": "dimension", "domainShape": "cyclic", "diverging": "none",
                   "formatClass": "plain", "zeroBaseline": "none", "zeroPad": 0},
    "YearMonth":  {"t0": "Temporal", "t1": "DateGranule", "visEncodings": ["temporal", "ordinal"],
                   "aggRole": "dimension", "domainShape": "open", "diverging": "none",
                   "formatClass": "plain", "zeroBaseline": "none", "zeroPad": 0},
    "YearWeek":   {"t0": "Temporal", "t1": "DateGranule", "visEncodings": ["temporal", "ordinal"],
                   "aggRole": "dimension", "domainShape": "open", "diverging": "none",
                   "formatClass": "plain", "zeroBaseline": "none", "zeroPad": 0},
    "Duration":   {"t0": "Temporal", "t1": "Duration", "visEncodings": ["quantitative"],
                   "aggRole": "additive", "domainShape": "open", "diverging": "none",
                   "formatClass": "unit-suffix", "zeroBaseline": "meaningful", "zeroPad": 0},
    # Measures
    "Amount":     {"t0": "Measure", "t1": "Amount", "visEncodings": ["quantitative"],
                   "aggRole": "additive", "domainShape": "open", "diverging": "none",
                   "formatClass": "currency", "zeroBaseline": "meaningful", "zeroPad": 0},
    "Revenue":    {"t0": "Measure", "t1": "Amount", "visEncodings": ["quantitative"],
                   "aggRole": "additive", "domainShape": "open", "diverging": "none",
                   "formatClass": "currency", "zeroBaseline": "meaningful", "zeroPad": 0},
    "Quantity":   {"t0": "Measure", "t1": "Physical", "visEncodings": ["quantitative"],
                   "aggRole": "additive", "domainShape": "open", "diverging": "none",
                   "formatClass": "unit-suffix", "zeroBaseline": "meaningful", "zeroPad": 0},
    "Percentage": {"t0": "Measure", "t1": "Proportion", "visEncodings": ["quantitative"],
                   "aggRole": "intensive", "domainShape": "bounded", "diverging": "none",
                   "formatClass": "percent", "zeroBaseline": "contextual", "zeroPad": 0},
    "PercentageChange": {"t0": "Measure", "t1": "SignedMeasure", "visEncodings": ["quantitative"],
                   "aggRole": "intensive", "domainShape": "open", "diverging": "conditional",
                   "formatClass": "percent", "zeroBaseline": "contextual", "zeroPad": 0.05},
    "Profit":     {"t0": "Measure", "t1": "SignedMeasure", "visEncodings": ["quantitative"],
                   "aggRole": "signed-additive", "domainShape": "open", "diverging": "conditional",
                   "formatClass": "decimal", "zeroBaseline": "meaningful", "zeroPad": 0},
    "Count":      {"t0": "Measure", "t1": "GenericMeasure", "visEncodings": ["quantitative"],
                   "aggRole": "additive", "domainShape": "open", "diverging": "none",
                   "formatClass": "integer", "zeroBaseline": "meaningful", "zeroPad": 0},
    "Number":     {"t0": "Measure", "t1": "GenericMeasure", "visEncodings": ["quantitative"],
                   "aggRole": "additive", "domainShape": "open", "diverging": "none",
                   "formatClass": "decimal", "zeroBaseline": "meaningful", "zeroPad": 0},
    # Discrete
    "Rank":       {"t0": "Discrete", "t1": "Rank", "visEncodings": ["ordinal"],
                   "aggRole": "dimension", "domainShape": "open", "diverging": "none",
                   "formatClass": "integer", "zeroBaseline": "arbitrary", "zeroPad": 0.08},
    "Score":      {"t0": "Discrete", "t1": "Score", "visEncodings": ["quantitative", "ordinal"],
                   "aggRole": "intensive", "domainShape": "bounded", "diverging": "conditional",
                   "formatClass": "decimal", "zeroBaseline": "contextual", "zeroPad": 0.05},
    # Categorical
    "Category":   {"t0": "Categorical", "t1": "Entity", "visEncodings": ["nominal"],
                   "aggRole": "dimension", "domainShape": "open", "diverging": "none",
                   "formatClass": "plain", "zeroBaseline": "none", "zeroPad": 0},
    "Name":       {"t0": "Categorical", "t1": "Entity", "visEncodings": ["nominal"],
                   "aggRole": "dimension", "domainShape": "open", "diverging": "none",
                   "formatClass": "plain", "zeroBaseline": "none", "zeroPad": 0},
    "Region":     {"t0": "Geographic", "t1": "GeoPlace", "visEncodings": ["nominal"],
                   "aggRole": "dimension", "domainShape": "open", "diverging": "none",
                   "formatClass": "plain", "zeroBaseline": "none", "zeroPad": 0},
    "Status":     {"t0": "Categorical", "t1": "Coded", "visEncodings": ["nominal"],
                   "aggRole": "dimension", "domainShape": "open", "diverging": "none",
                   "formatClass": "plain", "zeroBaseline": "none", "zeroPad": 0},
}

VIS_CATEGORIES = {"temporal", "ordinal", "nominal", "quantitative", "geographic"}
ADDITIVE = {"additive", "signed-additive"}
TIME_SERIES_T1 = "DateGranule"  # excludes Duration (a quantitative duration measure)


def get_type(name: str) -> dict[str, Any] | None:
    return FLINT_TYPES.get(name)


def canonical_alias(name: str) -> str:
    """Resolve a Skill measureTypes label to a Flint semantic type if known."""
    return {"currency": "Amount", "percent": "Percentage", "count": "Count",
            "duration": "Duration", "number": "Quantity", "unitless": "Number"}.get(name, name)


def infer_vis_category(values: list[Any]) -> str:
    """Data-only axis-type inference (ported from Flint inferVisCategory).

    number/integer→quantitative, boolean→nominal, date→temporal, string→nominal.
    """
    if not values:
        return "nominal"
    def is_boolean(v): return isinstance(v, bool) or (isinstance(v, str) and v.lower() in ("true", "false"))
    def is_number(v):
        if isinstance(v, bool):
            return False
        try:
            float(v)
            return True
        except (TypeError, ValueError):
            return False
    import re
    def looks_like_date(s):
        return bool(re.match(r"^\d|^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", str(s).strip(), re.I))
    def is_jsnull(v):
        try:
            return v != v  # NaN
        except Exception:
            return False
    non_null = [v for v in values if v is not None and v != "" and not is_jsnull(v)]
    if not non_null:
        return "nominal"
    if all(is_boolean(v) for v in non_null):
        return "nominal"
    if all(is_number(v) for v in non_null):
        return "quantitative"
    if all(looks_like_date(v) for v in non_null):
        return "temporal"
    return "nominal"


def field_semantics(sem_label: str | None, values: list[Any] | None = None) -> dict[str, Any]:
    """FieldSemantics: semantic label first, then data inference (axis type + role)."""
    if sem_label and sem_label != "":
        t = get_type(canonical_alias(sem_label))
        if t:
            return t
    vis = infer_vis_category(values or [])
    return {"t0": "Measure" if vis == "quantitative" else ("Temporal" if vis == "temporal" else "Categorical"),
            "t1": "GenericMeasure" if vis == "quantitative" else "DateGranule" if vis == "temporal" else "Entity",
            "visEncodings": [vis], "aggRole": "additive" if vis in ("quantitative",) else "dimension",
            "domainShape": "open", "diverging": "none",
            "formatClass": "decimal" if vis == "quantitative" else "plain",
            "zeroBaseline": "meaningful" if vis == "quantitative" else "none",
            "zeroPad": 0 if vis != "quantitative" else 0}


def is_ordinal(name: str) -> bool:
    t = get_type(canonical_alias(name))
    return bool(t and "ordinal" in t["visEncodings"])


def is_additive(name: str) -> bool:
    t = get_type(canonical_alias(name))
    return bool(t and t["aggRole"] in ADDITIVE)


def is_time_series(name: str) -> bool:
    t = get_type(canonical_alias(name))
    return bool(t and t["t0"] == "Temporal" and t["t1"] == TIME_SERIES_T1)


def is_categorical(name: str) -> bool:
    t = get_type(canonical_alias(name))
    return bool(t and (t["t0"] == "Categorical" or t["t0"] == "Geographic"))


def encoding(name: str) -> list[str]:
    t = get_type(canonical_alias(name))
    return (t or {}).get("visEncodings", ["nominal"])


def zero_baseline(name: str) -> str:
    t = get_type(canonical_alias(name))
    return (t or {}).get("zeroBaseline", "none")


def zero_pad(name: str) -> float:
    t = get_type(canonical_alias(name))
    return float((t or {}).get("zeroPad", 0))


def padded_domain(values: list, pad_fraction: float):
    """Flint computePaddedDomain → [paddedMin, paddedMax] or None."""
    nums = [float(v) for v in (values or []) if v is not None and str(v).strip() != ""]
    if pad_fraction <= 0 or len(nums) < 2:
        return None
    dmin, dmax = min(nums), max(nums)
    span = dmax - dmin
    if span <= 0:
        return None
    pad = span * pad_fraction
    return [dmin - pad, dmax + pad]


def zero_decision(sem_label: str | None, mark: str, values: list) -> dict:
    """Canonical port of Flint makeZeroDecision.

    Decides zero-baseline from zeroClass + mark + data range: meaningful
    (Amount/Count) bar/area/line pin 0; arbitrary (Rank/Year) data-fits unless
    a bar touches 0; contextual (Percentage) pins 0 when data crosses/is close
    to 0 or is bar/area, else data-fits with padding. Returns {"zero", "pad"}.
    """
    sem = canonical_alias(sem_label or "")
    t = get_type(sem)
    zero_class = t["zeroBaseline"] if t else "unknown"
    pad = t["zeroPad"] if t else 0.05
    is_bar_like = mark in ("bar", "area", "rect")
    is_scatter = mark in ("circle", "point")
    nums = [float(v) for v in (values or []) if v is not None and str(v).strip() != ""]
    data_min = min(nums) if nums else None
    data_max = max(nums) if nums else None

    if zero_class == "meaningful":
        if is_bar_like:
            return {"zero": True, "pad": 0}
        if is_scatter:
            if nums and data_min <= 0:
                return {"zero": True, "pad": 0}
            return {"zero": False, "pad": pad}
        # line/strip position: conventional zero reference stays on.
        return {"zero": True, "pad": 0}
    if zero_class == "arbitrary":
        if is_bar_like and nums and data_min <= 0:
            return {"zero": True, "pad": 0}
        return {"zero": False, "pad": pad}
    if zero_class == "contextual" and nums:
        if data_min <= 0:
            return {"zero": True, "pad": 0}
        proximity = data_min / data_max if data_max and data_max > 0 else 0
        if proximity < 0.3:
            return {"zero": True, "pad": 0}
        if is_bar_like:
            return {"zero": True, "pad": 0}
        return {"zero": False, "pad": 0.05}
    # unknown/no class: bar/area on a positional axis still pins 0; else data-fit.
    if is_bar_like:
        return {"zero": True, "pad": 0}
    return {"zero": False, "pad": 0.05}
