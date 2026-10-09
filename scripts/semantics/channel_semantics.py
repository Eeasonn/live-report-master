#!/usr/bin/env python3
"""Channel semantics: how a field maps onto a chart channel.

Modelled on Flint's ``resolve-semantics.ts`` (Phase 0) — the decision of how a
field behaves once it lands on a specific chart channel (dimension / measure /
breakdown), not just what the field *is*. Mirrors the runtime's
``channelAction`` / ``fieldSemantic`` in ``chart-adapter.js``.

Key rules:
  - A category/ordinal column placed in a measure slot degrades to a row-count
    (the honest reading) rather than a silent sum-to-0.
  - A measure column placed in a breakdown slot is rejected (cannot split).
  - A non-additive measure cannot stack or be summed.
"""
from __future__ import annotations

from typing import Any

from .type_registry import is_non_additive
from . import flint_core as fc


def field_kind(semantic_type: str | None, is_numeric: bool, time_hint: bool) -> str:
    """TF2-ish kind for a field: 'measure' | 'time' | 'ordinal' | 'category'."""
    if semantic_type in {"currency", "percent", "count", "duration", "number", "unitless"}:
        return "measure"
    if semantic_type and semantic_type != "None":
        return "measure"
    if is_numeric:
        return "measure"
    if time_hint:
        return "time"
    return "category"


def channel_action(kind: str, channel: str, measure_type: str | None,
                   chart_type: str | None = None) -> dict[str, str]:
    """Decide how a field (given its ``kind``) should behave on a ``channel``.

    Returns ``{action: allow|reject|count, reason}``. ``count`` means the field
    is degraded to a row-count measure. Mirrors the runtime channelAction.
    """
    if channel == "measure":
        if kind == "measure":
            # A non-additive measure cannot go into a stacked share chart.
            if is_non_additive(measure_type) and chart_type in {"stackedBar", "area", "bar_stacked"}:
                return {"action": "reject", "reason": "nonAdditiveMeasureInStacked"}
            return {"action": "allow", "reason": ""}
        if kind == "time":
            return {"action": "reject", "reason": "timeAsMeasure"}
        # category / ordinal into a measure slot: honest reading is row-count.
        return {"action": "count", "reason": "categoryAsMeasure"}
    if channel == "breakdown":
        if kind == "measure":
            return {"action": "reject", "reason": "measureAsBreakdown"}
        return {"action": "allow", "reason": ""}
    if channel == "dimension":
        if kind == "measure" and is_non_additive(measure_type) and chart_type == "stackedBar":
            return {"action": "reject", "reason": "nonAdditiveMeasureInStacked"}
        return {"action": "allow", "reason": ""}
    return {"action": "allow", "reason": ""}


def aggregate_role(semantic_type: str | None) -> dict[str, bool]:
    """Does the measure support sum / average / stack? (Flint aggregation role.)"""
    return {"additive": not is_non_additive(semantic_type),
            "sum": not is_non_additive(semantic_type),
            "average": True,
            "min": True, "max": True,
            "count": True}


# ---- Flint-grade channel semantics (resolve-semantics.ts depth) ------------

# visEncodings -> Vega/Flint encoding type.
_ENC_TO_VL = {"temporal": "temporal", "ordinal": "ordinal",
              "nominal": "nominal", "quantitative": "quantitative"}
# aggRole -> sensible aggregation default.
_AGG_ROLE_DEFAULT = {"additive": "sum", "signed-additive": "sum",
                     "intensive": "average", "dimension": "none"}


def _resolved_entry(semantic_type: str | None) -> dict[str, Any] | None:
    label = fc.canonical_alias(semantic_type or "")
    return fc.get_type(label)


def semantic_type_label(semantic_type: str | None) -> str:
    """Flint semantic type label (Revenue/Percentage/Count/...)."""
    e = _resolved_entry(semantic_type)
    return fc.canonical_alias(semantic_type or "") if e else (semantic_type or "")


def encoding_type(semantic_type: str | None, values: list | None = None) -> str:
    """Vega/Flint encoding type: 'temporal' | 'ordinal' | 'nominal' | 'quantitative'."""
    e = _resolved_entry(semantic_type)
    if e and e["visEncodings"]:
        return _ENC_TO_VL.get(e["visEncodings"][0], "nominal")
    vis = fc.infer_vis_category(values or [])
    return _ENC_TO_VL.get(vis, "nominal")


def additive(semantic_type: str | None) -> bool:
    """May the measure be summed / stacked? (Flint aggRole additive|signed-additive.)"""
    e = _resolved_entry(semantic_type)
    if e:
        return e["aggRole"] in fc.ADDITIVE
    return not is_non_additive(semantic_type)


def stackable(semantic_type: str | None) -> bool:
    """May the measure join a stacked share chart? (additive AND non-diverging.)"""
    e = _resolved_entry(semantic_type)
    if e:
        return e["aggRole"] in fc.ADDITIVE and e["diverging"] == "none"
    return not is_non_additive(semantic_type)


def aggregation_default(semantic_type: str | None, values: list | None = None) -> str:
    """Recommended aggregation for a field: sum (additive) / average (rate) / none."""
    e = _resolved_entry(semantic_type)
    if e:
        return _AGG_ROLE_DEFAULT.get(e["aggRole"], "sum")
    if encoding_type(semantic_type, values) == "quantitative":
        return "sum"
    return "none"


def domain_shape(semantic_type: str | None) -> str:
    """Flint domainShape: 'open' | 'bounded' | 'cyclic'."""
    e = _resolved_entry(semantic_type)
    return (e or {}).get("domainShape", "open")


def cyclic(semantic_type: str | None) -> bool:
    return domain_shape(semantic_type) == "cyclic"


def nice(semantic_type: str | None, values: list | None = None) -> bool:
    """Should the value axis round ticks to nice intervals? (quantitative only.)"""
    return encoding_type(semantic_type, values) == "quantitative"


def sort_direction(semantic_type: str | None, values: list | None = None) -> str:
    """Natural order for the axis: ordinal/cyclic keep authored order, else 'desc'."""
    if encoding_type(semantic_type, values) in ("ordinal", "temporal"):
        return "asc"
    return "desc"


def resolve_channel_semantics(semantic_type: str | None, values: list | None = None,
                              channel: str = "y") -> dict[str, Any]:
    """Flint-grade ChannelSemantics: field identity + channel-specific decisions.

    Mirrors resolveChannelSemantics: encoding type, aggregation default,
    additivity/stackability, domain shape, nice tick, sort direction, and zero-
    baseline (delegated to the moved-in flint_core registry).
    """
    e = _resolved_entry(semantic_type)
    label = fc.canonical_alias(semantic_type or "") if e else (semantic_type or "")
    enc = encoding_type(semantic_type, values)
    scaletype = "linear"
    return {
        "semanticType": label,
        "encoding": enc,
        "type": enc,
        "additive": additive(semantic_type),
        "stackable": stackable(semantic_type),
        "aggregationDefault": aggregation_default(semantic_type, values),
        "domainShape": domain_shape(semantic_type),
        "cyclic": cyclic(semantic_type),
        "nice": nice(semantic_type, values),
        "scaleType": scaletype,
        "sortDirection": sort_direction(semantic_type, values),
        "zeroBaseline": fc.zero_baseline(label) if e else "none",
    }
