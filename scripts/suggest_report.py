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
import date_parse

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


# Flint 聚合角色（refs/flint/src/core/type-registry.ts:104-122）：intensive 量
# 不可加总、只能平均——榜首事实的聚合方式跟随语义，标题不写「跨品类加总」式伪事实。
_INTENSIVE_HINTS = {"Price", "Percentage", "PercentageChange", "Score",
                    "Temperature", "Sentiment", "Correlation"}


def _measure_hint(profile: dict[str, Any], name: str) -> str:
    for c in profile.get("columns") or []:
        if c.get("name") == name:
            return c.get("semanticHint") or ""
    return ""


def _time_span(table: dict[str, Any] | None, col: str) -> tuple[str, str] | None:
    """(首期, 末期) 日历序标签——数据形态能支撑的最低限度「半结论」素材。

    只认真日期值（``parts`` 为 None 的单元一律跳过）；不足两期或首末同 label
    （单期数据）不产出，回退描述式标题。周值（parts 恒 None）天然跳过。"""
    if not table or not col:
        return None
    cols = table.get("columns") or []
    if col not in cols:
        return None
    ci = cols.index(col)
    values: list[Any] = []
    for row in table.get("rows") or []:
        v = row[ci] if ci < len(row) else None
        if v is None or str(v).strip() == "" or date_parse.parts(v) is None:
            continue
        values.append(v)
    if len(values) < 2:
        return None
    bucket = date_parse.infer_grain(values, col) or "day"
    keyed = sorted(((date_parse.sort_key(v), str(date_parse.label(v, bucket) or ""))
                    for v in values), key=lambda kv: kv[0])
    lo, hi = keyed[0][1], keyed[-1][1]
    return (lo, hi) if lo and hi and lo != hi else None


def _rank_top1(table: dict[str, Any] | None, dim: str, measure: str | None,
               intensive: bool) -> str | None:
    """榜首事实：``dim`` 各取值按 ``measure`` 聚合（intensive→average，否则 sum）
    的最大者。单一取值不产出（flint-chart-author「don't chart blind」的
    one-real-entity 陷阱：只有一组时「居首」无信息量）。"""
    if not table or not dim:
        return None
    cols = table.get("columns") or []
    if dim not in cols or (measure is not None and measure not in cols):
        return None
    di = cols.index(dim)
    mi = cols.index(measure) if measure else None
    acc: dict[str, list[float]] = {}  # key → [sum, count]
    for row in table.get("rows") or []:
        key = row[di] if di < len(row) else None
        if key is None or str(key).strip() == "":
            continue
        key = str(key).strip()
        if mi is None:  # 记录型数据：按行计数
            cell = acc.setdefault(key, [0.0, 0.0])
            cell[0] += 1.0
            cell[1] += 1.0
            continue
        v = row[mi] if mi < len(row) else None
        if v is None or (isinstance(v, str) and not v.strip()):
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        cell = acc.setdefault(key, [0.0, 0.0])
        cell[0] += v
        cell[1] += 1.0
    if len(acc) < 2:
        return None

    def _val(cell: list[float]) -> float:
        return cell[0] / cell[1] if intensive else cell[0]

    return max(acc.items(), key=lambda kv: _val(kv[1]))[0]


def _fact_of(facts: list[dict[str, Any]], kind: str,
             dim: str | None = None, measure: str | None = None,
             columns: set[str] | None = None) -> dict[str, Any] | None:
    """从 profile.facts 里挑一条匹配的事实（NA-01 消费端：suggest 的结论式标题
    只从 facts 里挑真话——没有匹配事实就不写主张句，退回半结论式默认标题）。"""
    for f in facts or []:
        if not isinstance(f, dict) or f.get("kind") != kind:
            continue
        ev = f.get("evidence") or {}
        if dim is not None and ev.get("dimension") != dim:
            continue
        if measure is not None and ev.get("measure") != measure:
            continue
        if columns is not None and set(f.get("columns") or []) != columns:
            continue
        return f
    return None


def suggest_charts(profile: dict[str, Any],
                   table: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Data-shape heuristics → a short, family-diverse chart list.

    Record-type datasets (no numeric measure column; each row = one record) use
    the synthetic row-count measure ``*`` with ``count`` aggregation — the same
    count semantics the compiler/runtime/server all understand.

    ``table`` (optional, the canonical loaded table) upgrades two default titles
    from mechanical to data-backed semi-conclusions — the trend title gains the
    calendar time span, the rank title names the leader (aggregation follows the
    Flint additive/intensive role). Without it, titles stay descriptive. The
    model is expected to overwrite titles with real findings in its patch; these
    are just better placeholders (SKILL.md「标题写发现句」).

    ``profile.facts``（NA-01，六类确定性事实）是结论式标题的真话来源：有匹配事实
    才把半结论升级成主张句（带上占比），并把分析主张写进 ``chart.question``
    （指到 fact id）；无 facts 时零变化（现行半结论式默认标题，零回归）。
    """
    columns: list[dict[str, Any]] = profile.get("columns") or []
    by_name = {c.get("name"): c for c in columns}
    measures = [c.get("name") for c in columns if c.get("type") == "measure"]
    times = [c.get("name") for c in columns if c.get("type") == "time"]
    dims = [c.get("name") for c in columns if c.get("type") == "category"]
    low_dims = [c.get("name") for c in _low_card_dims(columns)]
    stage_dims = [d for d in dims if _STAGE_HINT.search(d or "") and
                  (by_name.get(d) and 3 <= int(by_name[d].get("distinct", 0) or 0) <= 12)]
    facts = [f for f in (profile.get("facts") or []) if isinstance(f, dict) and f.get("id")]

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

    def add(preset: str, title: str, encoding: dict[str, Any] | None,
            question: str | None = None) -> None:
        fam = _FAMILY.get(preset, "其他")
        if len(charts) >= _MAX_CHARTS:
            return
        if family_used.get(fam, 0) >= _FAMILY_BUDGET:
            return
        family_used[fam] = family_used.get(fam, 0) + 1
        chart: dict[str, Any] = {"id": f"chart-{len(charts) + 1}", "presetId": preset,
                                 "title": title}
        if question:
            chart["question"] = question
        if encoding:
            chart["encoding"] = encoding
        charts.append(chart)

    primary = [measures[0]] if has_measures else ["*"]

    # 1) Trend over time (highest value signal).
    if times:
        m0 = measures[0] if has_measures else count_label
        span = _time_span(table, times[0])
        title = f"{m0}走势（{span[0]}→{span[1]}）" if span else f"{m0} 随时间趋势"
        # 主张（有 top_mover 事实才写）：环比极值是这条趋势线的分析问题。
        tm = _fact_of(facts, "top_mover")
        q = None
        if tm:
            ev = tm.get("evidence") or {}
            q = (f"最近一期 {ev.get('latest')} 对 {ev.get('previous')}，哪个"
                 f"{ev.get('dimension')}的{ev.get('measure')}环比变化最大？（依据 {tm.get('id')}）")
        add("line.trend", title, bind(times[0], primary), question=q)

    # 2) Rank/compare over a readable category.
    if low_dims:
        rank_q: str | None = None
        if has_measures:
            intensive = _measure_hint(profile, measures[0]) in _INTENSIVE_HINTS
            top1 = _rank_top1(table, low_dims[0], measures[0], intensive)
            title = f"{top1}的{measures[0]}居首" if top1 else f"{low_dims[0]} 对比"
            # 集中度事实与榜首同源时，标题升级成带占比的主张句（从事实里挑真话）。
            conc = _fact_of(facts, "concentration", dim=low_dims[0], measure=measures[0]) if top1 else None
            share = None
            if conc:
                share = next((t.get("share") for t in (conc.get("evidence") or {}).get("top3") or []
                              if t.get("label") == top1), None)
            if share is not None:
                title = f"{top1}的{measures[0]}居首（占 {share * 100:.1f}%）"
                rank_q = (f"「{top1}」以 {share * 100:.1f}% 的{measures[0]}份额领先"
                          f"（依据 {conc.get('id')}）")
            add("bar.rank", title, bind(low_dims[0], primary), question=rank_q)
        else:
            top1 = _rank_top1(table, low_dims[0], None, False)
            title = f"{top1}记录数最多" if top1 else f"{low_dims[0]} {count_label}"
            add("bar.rank", title, bind(low_dims[0], primary))

    # 3) Two-measure relationship (real measures only).
    if len(measures) >= 2:
        corr = _fact_of(facts, "correlation", columns=set(measures[:2]))
        q = None
        if corr:
            r = (corr.get("evidence") or {}).get("r")
            q = (f"{measures[0]} 与 {measures[1]} 的行级相关有多强"
                 f"（r={r:.2f}，相关非因果；依据 {corr.get('id')}）" if isinstance(r, (int, float))
                 else f"{measures[0]} 与 {measures[1]} 的行级相关有多强？（依据 {corr.get('id')}）")
        add("scatter.correlation", f"{measures[0]} × {measures[1]} 关联",
            {"measures": measures[:2]}, question=q)

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
        "charts": suggest_charts(trimmed, table),
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
