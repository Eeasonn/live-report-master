#!/usr/bin/env python3
"""Deterministic default ``report_request.json`` generator (Option A).

Reads a table and applies data-shape heuristics to emit a *complete, valid*
request. The Agent then only authors a **patch** (``report_intent.json``) that
overrides the few points it actually cares about (title, chart choice, roles,
narrative); everything else — layout, ECharts option, aggregation, packaging,
KPI/filter auto-fill — is already fully templated by the compile pipeline.

The generated request is intentionally minimal so it cannot drift from the
compiler's own inference: it carries ``title`` + ``charts`` only, and lets
``normalize_request`` infer ``columnRoles``/``measureTypes`` and auto-fill KPIs
and filter candidates (``_ensure_default_kpis`` / ``_default_filter_candidates``
in ``spec_to_report.py``).
"""
from __future__ import annotations

# Running from a deployed skill folder must not litter it with __pycache__/:
# the env var is inherited by spawned children (lint/package/spec_to_report),
# the flag covers imports made after this line in this process.
import os as _os, sys as _sys
_os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
_sys.dont_write_bytecode = True
import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from table_input import load_table_file
from profile_table import build_profile

# Top-level list fields are REPLACED wholesale by a patch; dict fields are
# shallow-merged (patch keys win); scalars are simply overridden.
_LIST_KEYS = {"charts", "kpis", "insights", "filters", "facts", "sections",
              "relations", "datasets"}
_DICT_KEYS = {"columnRoles", "measureTypes", "semantic_types", "style", "runtime"}

_ID_HINT = re.compile(r"^(id|sku|po|batch|code|no|订单号|单号|批次|号|编码|编号)", re.IGNORECASE)
_STAGE_HINT = re.compile(r"阶段|状态|环节|stage|status|funnel", re.IGNORECASE)

# Chart preset → visual family, mirroring validate_request.FAMILY (for the
# diversity budget so the default never reads as a wall of the same bars).
_FAMILY = {
    "line.trend": "趋势", "area.trend": "趋势",
    "bar.rank": "比较", "bar.stacked": "比较",
    "scatter.correlation": "关系",
    "pie.share": "构成", "donut.share": "构成",
    "funnel.stage": "流程",
    "table.aggregate": "明细", "table.detail": "明细",
}

_MAX_CHARTS = 5
_FAMILY_BUDGET = 2  # never more than 2 charts of the same family in a default


def _clean_title(stem: str) -> str:
    t = re.sub(r"[_\-\s]+", " ", stem).strip()
    return (t or "数据分析报告")[:120]


def _low_card_dims(columns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dimension (category) columns with a readable, low cardinality (2..12),
    excluding entity/id keys."""
    out = []
    for c in columns:
        if c.get("type") != "category":
            continue
        name = c.get("name") or ""
        if _ID_HINT.match(name):
            continue
        card = int(c.get("distinct", 0) or 0)
        if 2 <= card <= 12:
            out.append(c)
    # Prefer fewer distinct values (more readable axis) first.
    out.sort(key=lambda c: int(c.get("distinct", 0) or 0))
    return out


def suggest_charts(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Data-shape heuristics → a short, family-diverse chart list.

    Record-type datasets (no numeric measure column; each row = one record) use
    the synthetic row-count measure ``*`` with ``count`` aggregation — the same
    count semantics the compiler/runtime/server all understand.
    """
    columns: list[dict[str, Any]] = profile.get("columns") or []
    by_name = {c.get("name"): c for c in columns}
    measures = [c.get("name") for c in columns if c.get("type") == "measure"]
    times = [c.get("name") for c in columns if c.get("type") == "time"]
    dims = [c.get("name") for c in columns if c.get("type") == "category"]
    low_dims = [c.get("name") for c in _low_card_dims(columns)]
    stage_dims = [d for d in dims if _STAGE_HINT.search(d or "") and
                  (by_name.get(d) and 3 <= int(by_name[d].get("distinct", 0) or 0) <= 12)]

    has_measures = bool(measures)
    count_label = "记录数"

    def bind(dim: str, ms: list[str], **extra: Any) -> dict[str, Any]:
        """Chart encoding; record-type data binds COUNT(*) + count aggregation."""
        enc: dict[str, Any] = {"dimension": dim, "measures": ms}
        if not has_measures:
            enc["aggregation"] = "count"
        enc.update(extra)
        return enc

    family_used: dict[str, int] = {}
    charts: list[dict[str, Any]] = []

    def add(preset: str, title: str, encoding: dict[str, Any] | None) -> None:
        fam = _FAMILY.get(preset, "其他")
        if len(charts) >= _MAX_CHARTS:
            return
        if family_used.get(fam, 0) >= _FAMILY_BUDGET:
            return
        family_used[fam] = family_used.get(fam, 0) + 1
        chart: dict[str, Any] = {"id": f"chart-{len(charts) + 1}", "presetId": preset,
                                 "title": title}
        if encoding:
            chart["encoding"] = encoding
        charts.append(chart)

    primary = [measures[0]] if has_measures else ["*"]

    # 1) Trend over time (highest value signal).
    if times:
        add("line.trend", f"{measures[0] if has_measures else count_label} 随时间趋势",
            bind(times[0], primary))

    # 2) Rank/compare over a readable category.
    if low_dims:
        add("bar.rank", f"{low_dims[0]} {('对比' if has_measures else count_label)}",
            bind(low_dims[0], primary))

    # 3) Two-measure relationship (real measures only).
    if len(measures) >= 2:
        add("scatter.correlation", f"{measures[0]} × {measures[1]} 关联",
            {"measures": measures[:2]})

    # 4) Two-category crosstab (grouped/stacked).
    if len(low_dims) >= 2:
        add("bar.stacked", f"{low_dims[0]} × {low_dims[1]} 构成",
            bind(low_dims[0], primary, breakdown=low_dims[1]))

    # 5) Stage flow (only with a clear stage-hint name + a real measure).
    if stage_dims and has_measures:
        add("funnel.stage", f"{stage_dims[0]} 转化",
            {"dimension": stage_dims[0], "measures": [measures[0]]})

    # 6) Aggregate evidence table.
    if dims or times:
        add("table.aggregate", "数据明细",
            bind((times[0] if times else dims[0]), measures[:2] if has_measures else ["*"]))

    # Fallback: no dimensions at all → a raw detail table.
    if not charts:
        add("table.detail", "原始明细", None)

    return charts


def suggest_request(table_path: str | Path) -> dict[str, Any]:
    """Build a complete, valid default request from a table file."""
    table = load_table_file(str(table_path))
    profile = build_profile(table, str(Path(table_path).resolve()))
    stem = Path(table_path).stem
    # Ultra-high-cardinality category columns (门店编码/上报时间-like entity keys
    # and timestamps the profiler could not type as id/time) can never render as
    # a readable chart dimension: above the singlefile cardinality cap (2000)
    # they push an otherwise-fitting dataset into server mode AND bloat filter
    # candidates. Suggest ignoring them so the default stays singlefile; a real
    # authoring run needed this hinted or the agent had to discover it by trial.
    ignores = {c.get("name"): "ignore" for c in profile.get("columns") or []
               if c.get("type") == "category" and int(c.get("distinct", 0) or 0) > 2000}
    if ignores:
        trimmed = dict(profile)
        trimmed["columns"] = [c for c in profile.get("columns") or [] if c.get("name") not in ignores]
    else:
        trimmed = profile
    return {
        "schemaVersion": 1,
        "templateId": "standard-bi-v1",
        "title": _clean_title(stem),
        "charts": suggest_charts(trimmed),
        **({"columnRoles": ignores} if ignores else {}),
    }


def merge_request(default: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Patch-over-default merge: the Agent writes only what it wants to change."""
    merged = dict(default)
    for key, value in patch.items():
        if key in _LIST_KEYS or key not in _DICT_KEYS:
            merged[key] = value
        else:
            base = dict(merged.get(key) or {})
            if isinstance(value, dict):
                base.update(value)
                merged[key] = base
            else:
                merged[key] = value
    merged.setdefault("schemaVersion", 1)
    merged.setdefault("templateId", "standard-bi-v1")
    return merged


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate a default report_request.json from a table")
    ap.add_argument("--table", required=True, help=".xlsx, .csv or canonical JSON")
    ap.add_argument("--out", default=None, help="output path (default: stdout)")
    ap.add_argument("--patch", default=None, help="optional patch JSON to merge over the default")
    args = ap.parse_args()

    try:
        request = suggest_request(args.table)
        if args.patch:
            patch = json.loads(Path(args.patch).read_text(encoding="utf-8"))
            request = merge_request(request, patch)
        text = json.dumps(request, ensure_ascii=False, indent=2) + "\n"
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
        else:
            sys.stdout.write(text)
        return 0
    except Exception as exc:  # noqa: BLE001 — surfaced as a clean CLI failure
        print(f"suggest_report: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
