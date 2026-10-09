#!/usr/bin/env python3
"""Shape rules: per-chartType dimension / measure capacity.

Mirrors the runtime ``shapeRules`` / ``effMeasureMax`` in ``chart-adapter.js``
and ``editor-runtime.js``, as a single source of truth for the compile side.
Rules derive from what each whitelisted series actually consumes:
  - bar / stackedBar cross a dimension × breakdown (可加 2 维).
  - line / area are single-axis trends (1 维, 时间或有序).
  - pie / donut / funnel take 1 维度 + 1 指标.
  - scatter takes 2–3 指标.
  - table takes any fields.
The composite (mixed bar/line) budget applies to a single cartesian dimension
and allows up to ``COMPOSITE_MAX`` measures.
"""
from __future__ import annotations

from typing import Any

COMPOSITE_MAX = 10
CARTESIAN = {"bar", "stackedBar", "line", "area"}


def shape_rule(view_type: str) -> dict[str, Any]:
    dim1 = {"min": 1, "max": 1}
    dim2 = {"min": 1, "max": 2}
    rules = {
        "bar":        {"dims": dim2, "meas": {"min": 1, "max": 2}, "note": "1-2 个维度 + 1-2 个指标",
                       "composite": True},
        "stackedBar": {"dims": dim2, "meas": {"min": 1, "max": 2}, "note": "1-2 个维度 + 1-2 个指标",
                       "composite": True},
        "line":       {"dims": dim1, "meas": {"min": 1, "max": 2}, "note": "1 个维度（时间/有序）+ 1-2 个指标",
                       "composite": True},
        "area":       {"dims": dim1, "meas": {"min": 1, "max": 2}, "note": "1 个维度（时间/有序）+ 1-2 个指标",
                       "composite": True},
        "pie":        {"dims": dim1, "meas": {"min": 1, "max": 1}, "note": "1 个维度 + 1 个指标", "composite": False},
        "donut":      {"dims": dim1, "meas": {"min": 1, "max": 1}, "note": "1 个维度 + 1 个指标", "composite": False},
        "funnel":     {"dims": dim1, "meas": {"min": 1, "max": 1}, "note": "≥3 个有序阶段 + 1 个指标", "composite": False},
        "scatter":    {"dims": {"min": 0, "max": 0}, "meas": {"min": 2, "max": 3}, "note": "2 个指标（x/y），可选第 3 个作大小",
                       "composite": False},
        "table":      {"dims": dim2, "meas": {"min": 1, "max": 100}, "note": "任意字段", "composite": False},
    }
    return rules.get(view_type, {"dims": {"min": 0, "max": 99}, "meas": {"min": 0, "max": 99},
                                 "note": "", "composite": False})


def effective_measure_max(view_type: str, dims_count: int) -> int:
    """The actual measure cap given the current dimension count (composite-aware).

    - A 2-dim crosstab (bar/stackedBar) leaves no room for a 2nd value axis -> 1.
    - A single cartesian dimension allows the composite budget (COMposite_MAX).
    - Otherwise the chart type's own measure max applies.
    """
    rule = shape_rule(view_type)
    if view_type in {"bar", "stackedBar"} and dims_count >= 2:
        return 1
    if view_type in CARTESIAN and dims_count < 2:
        return COMPOSITE_MAX
    return rule["meas"]["max"]


def supports_composite(view_type: str) -> bool:
    return bool(shape_rule(view_type).get("composite", False))
