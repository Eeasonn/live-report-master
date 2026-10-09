#!/usr/bin/env python3
"""Chart-type recommendation (ported from Flint chart-type-recommendation.ts).

Builds a deterministic `DataProfile` (measures / temporals / categoricals /
ordinals + cardinality) from field semantics, then scores candidate chart
types (Line over a time field + measure = 88; Bar over a category + measure =
80; Grouped/Stacked for two categories; …). The scores are SUGGESTIONS with a
reason string — never a hard gate — so the author/editor can still pick any type
(this is the Option C "意图族" entry, moved into the skill).
"""
from __future__ import annotations

from typing import Any

from semantics import flint_core as fc

PIE_MAX_SLICES = 8
LOW_CARD_AXIS = 12
LOW_CARD_SERIES = 8


def build_profile(fields: list[dict[str, Any]]) -> dict[str, Any]:
    """fields: [{name, semLabel, values}] -> {measures, temporals, categoricals,
    ordinals, dims, cardinality map}. Uses Flint semantics; no label -> data inference."""
    measures, temporals, categoricals, ordinals = [], [], [], []
    cardinality = {}
    for f in fields:
        values = f.get("values") or []
        card = len({str(v) for v in values if v is not None and v != ""})
        cardinality[f["name"]] = card
        sem = fc.canonical_alias(f.get("semLabel") or "")
        t = fc.get_type(sem)
        if t and fc.is_time_series(sem):
            temporals.append(f)
        elif t and fc.is_ordinal(sem):
            ordinals.append(f)
        elif t and (fc.is_categorical(sem)):
            categoricals.append(f)
        elif t and t["aggRole"] in fc.ADDITIVE:
            measures.append(f)
        else:
            # No/unknown label -> infer from data values.
            vis = fc.infer_vis_category(values)
            if vis == "temporal":
                temporals.append(f)
            elif vis == "quantitative":
                measures.append(f)
            elif vis == "nominal":
                categoricals.append(f)
    dims = categoricals + ordinals + temporals
    return {"measures": measures, "temporals": temporals,
            "categoricals": categoricals, "ordinals": ordinals,
            "dims": dims, "cardinality": cardinality}


def recommend_chart_types(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Scored chart-type suggestions ordered by descending score, stable ties."""
    measures = profile["measures"]
    temporals = profile["temporals"]
    cat_like = profile["categoricals"] + profile["ordinals"]  # discrete, non-temporal
    dims = profile["dims"]
    card = profile["cardinality"]
    low_catlike = [f for f in cat_like if 2 <= card.get(f["name"], 0) <= LOW_CARD_SERIES]
    low_dims = [f for f in dims if 2 <= card.get(f["name"], 0) <= LOW_CARD_AXIS]
    has_measure = len(measures) >= 1
    out: list[dict[str, Any]] = []

    def add(chart_type: str, score: int, reason: str) -> None:
        out.append({"chartType": chart_type, "score": score, "reason": reason})

    if len(temporals) >= 1 and has_measure:
        add("Line Chart", 88, "has a time field + a measure (trend over time)")
    if len(measures) >= 2:
        add("Scatter Plot", 84, "two quantitative measures (x/y relationship)")
    if len(cat_like) >= 1 and has_measure:
        add("Bar Chart", 80, "has a category axis + a measure")
    if len(cat_like) >= 2 and len(low_catlike) >= 1 and has_measure:
        add("Grouped Bar Chart", 72, "has two categories + a measure")
        add("Stacked Bar Chart", 70, "has two categories + a measure")
    if len(low_dims) >= 2 and has_measure:
        add("Heatmap", 68, "two discrete fields (cross-tab)")
    if len(cat_like) == 1 and not temporals and has_measure:
        c = cat_like[0]
        if 2 <= card.get(c["name"], 0) <= PIE_MAX_SLICES:
            add("Pie Chart", 66, "one category with a small number of slices + a measure")
    if len(cat_like) >= 1 and has_measure and not temporals:
        add("Grouped Bar Chart", 62, "categories + a measure")
    if not has_measure:
        add("Bar Chart", 60, "categories without a measure (counts)")
        if len(low_dims) >= 2:
            add("Heatmap", 55, "two discrete fields (cross-tab counts)")
    if len(measures) >= 2:
        add("Scatter Plot", 20, "fallback for numeric data")
    else:
        add("Bar Chart", 15, "fallback")
    # Stable sort: descending score, ties keep insertion order.
    out.sort(key=lambda x: -x["score"])
    return out


def recommend(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return recommend_chart_types(build_profile(fields))
