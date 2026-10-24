#!/usr/bin/env python3
"""Profile a supported 2D table for report authoring."""

from __future__ import annotations

# Running from a deployed skill folder must not litter it with __pycache__/:
# the env var is inherited by spawned children (lint/package/spec_to_report),
# the flag covers imports made after this line in this process.
import os as _os, sys as _sys
_os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
_sys.dont_write_bytecode = True
import argparse
import hashlib
import json
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from table_input import load_table_file
from validate_table import TableValidationError


def _json_value(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _profile_value(value: Any, column_type: str) -> Any:
    """Keep profiling useful while avoiding exposure of identifier values."""
    if column_type == "id" and value not in (None, ""):
        digest = hashlib.sha256(str(value).encode("utf-8", errors="replace")).hexdigest()[:10]
        return f"<id:{digest}>"
    return _json_value(value)


MANIFEST_TOP_VALUES = 5


def column_stats(profile_columns: list[dict[str, Any]], quality: dict[str, Any] | None = None,
                 top: int = MANIFEST_TOP_VALUES) -> dict[str, dict[str, Any]]:
    """Compact per-column stats for the report manifest (数据源配置 副行 / 数据质量).

    The full profile keeps up to 20 top values and every measure/mean; the
    manifest only needs a short, stable summary that the editor can render
    WITHOUT loading rows (server mode has none). This is the ONE place that
    decides that shape — the runtime never recomputes stats, it only formats
    ``dataset.columnStats`` (server mode included).

    ``distinct``/``missing`` answer "can this be a dimension", ``invalid`` is
    the number of malformed cells validate_table turned into null (never zero),
    so a data-prep user can see why a total looks short.
    """
    quality = quality or {}
    invalid = quality.get("invalidValues") or {}
    samples = quality.get("invalidSamples") or {}
    text_fallback = quality.get("textFallback") or {}
    text_fallback_samples = quality.get("textFallbackSamples") or {}
    out: dict[str, dict[str, Any]] = {}
    for item in profile_columns:
        name = item.get("name")
        if not name:
            continue
        stat: dict[str, Any] = {
            "nonNull": int(item.get("nonNull") or 0),
            "missing": int(item.get("missing") or 0),
            "distinct": int(item.get("distinct") or 0),
        }
        for key in ("min", "max", "mean", "sum"):
            value = item.get(key)
            if value is not None and isinstance(value, (int, float)) and math.isfinite(float(value)):
                stat[key] = value
            elif value is not None and key in ("min", "max") and not isinstance(value, (int, float)):
                # Time columns carry the first/last value as a raw string.
                stat[key] = value
        tops = [{"value": t.get("value"), "count": int(t.get("count") or 0)}
                for t in (item.get("topValues") or [])[:top] if isinstance(t, dict)]
        if tops:
            stat["top"] = tops
        if invalid.get(name):
            stat["invalid"] = int(invalid[name])
            bad = [str(x) for x in (samples.get(name) or [])[:3]]
            if bad:
                stat["invalidSamples"] = bad
        if text_fallback.get(name):
            # 编码列翻转（validate_table 单一真源的翻转事实）：值没丢，只是整列
            # 改按文本保留 —— 徽标口径与「已按空」刻意不同（值在，但 sum 无意义）。
            stat["textFallback"] = int(text_fallback[name])
            tf_bad = [str(x) for x in (text_fallback_samples.get(name) or [])[:3]]
            if tf_bad:
                stat["textFallbackSamples"] = tf_bad
        out[name] = stat
    return out


# ════════ profile.facts 六类确定性事实（NA-01，docs/product-optimization-roadmap-2026-10-23.md）════════
#
# 「模型写洞察时只有列级统计可看」的现状诊断 → 在构建期预埋**确定性理解插座**：
# 每条 fact = {id, kind, statement(人话一句), evidence(数字与列/值引用), columns}，
# 纯 stdlib 从数据里算出来，调用方 Agent 以 `insights[].facts` / `{{fact:id}}` 绑定引用
# （NA-02 的存在性闸防幻觉）。skill 本体永不内置模型调用——facts 只是给 Agent 看的真话。
#
# 口径纪律：
# - 数值格式恒 `,.10g`（AGENTS 记过 `,.4g` 把 127,994 打成 1.28e+05 的事故）；
# - 无法判定的类**返回跳过原因**而不是编造（extract_facts 第二返回值）；
# - 每类有界、总数 ≤24；大表逐列扫描复用既有 profile 的全行遍历口径，
#   组合唯一性用「distinct 之积 ≥ 行数」的必要性条件先剪枝再实扫（≤10 次）。

FACT_KINDS = ("granularity", "top_mover", "concentration", "calendar_gap",
              "correlation", "sign")
_FACTS_CAP = 24
_GRAN_CAND_CAP = 8          # 参与键组合尝试的列数上限
_GRAN_SCAN_CAP = 10         # 键组合真实行扫描次数上限（剪枝后仍需有界）
_CONC_DIM_CAP = 2           # 集中度事实的维度数上限
_CORR_PAIR_CAP = 3          # 相关对事实条数上限
_CORR_FLOOR = 0.2           # |r| 低于此值视为噪声，诚实跳过
_SIGN_COL_CAP = 3           # 跨零列事实条数上限
_PEARSON_SAMPLE = 50000     # Pearson 行采样上限（等距步进，确定性）
_INTENSIVE_LABELS = {"Price", "Percentage", "PercentageChange", "Score",
                     "Temperature", "Sentiment", "Correlation"}


def _fmt_num(value: Any) -> str:
    """构建期统一数字口径：`,.10g`（对齐 decision_ledger 的锚点格式）。"""
    try:
        return f"{float(value):,.10g}"
    except (TypeError, ValueError):
        return str(value)


def _make_fact(kind: str, seq: int, statement: str, evidence: dict[str, Any],
               columns: list[str]) -> dict[str, Any]:
    return {"id": f"fact:{kind}:{seq}", "kind": kind, "statement": statement,
            "evidence": evidence, "columns": list(columns)}


def _fact_column_view(table: dict[str, Any]) -> tuple[list[str], list[list[Any]], dict[str, str]]:
    return (list(table.get("columns") or []), list(table.get("rows") or []),
            dict((table.get("meta") or {}).get("types") or {}))


def _is_num(v: Any) -> bool:
    if v is None or isinstance(v, bool) or (isinstance(v, str) and not v.strip()):
        return False
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def _measure_sums(cols: list[str], rows: list[list[Any]], types: dict[str, str]) -> dict[str, float]:
    """主指标选择依据：各数值列的 |sum|（一次全表遍历）。"""
    sums = {name: 0.0 for name in cols if types.get(name) == "measure"}
    if not sums:
        return sums
    idx = {name: cols.index(name) for name in sums}
    for row in rows:
        for name, ci in idx.items():
            v = row[ci] if ci < len(row) else None
            if _is_num(v):
                sums[name] += float(v)
    return sums


def _primary_measure(cols: list[str], rows: list[list[Any]], types: dict[str, str]) -> str | None:
    """主指标 = |sum| 最大的数值列（同分取列序靠前者，确定性）。"""
    sums = _measure_sums(cols, rows, types)
    best, best_key = None, -1.0
    for name in cols:  # 列序优先，tie-break 确定性
        if name in sums and abs(sums[name]) > best_key:
            best, best_key = name, abs(sums[name])
    return best


def _agg_of(measure: str) -> str:
    """intensive 语义（价格/比率/评分…）不可加总——集中度等聚合跟随语义用平均
    （对齐 suggest_report._INTENSIVE_HINTS 与 Flint resolveAggregationDefault）。"""
    from measure_semantics import semantic_label_of
    return "average" if semantic_label_of(None, measure) in _INTENSIVE_LABELS else "sum"


def _fact_granularity(cols: list[str], rows: list[list[Any]], types: dict[str, str],
                      distinct: dict[str, int]) -> tuple[dict[str, Any] | None, str | None]:
    """① 粒度判定：最小唯一键组合（≤3 列）；找不到也给「最接近组合」的诚实陈述。"""
    n = len(rows)
    if n < 2:
        return None, "行数不足 2，无法判定键组合唯一性"
    # 候选列：维度/时间优先（可读的业务键），id 类兜底；指标列永不进键。
    cand = [c for c in cols if types.get(c) in ("category", "time")]
    cand += [c for c in cols if types.get(c) == "id"]
    cand.sort(key=lambda c: (0 if types.get(c) in ("category", "time") else 1,
                             int(distinct.get(c, 0) or 0)))
    cand = cand[:_GRAN_CAND_CAP]
    if not cand:
        return None, "没有可作键的维度/时间/编号列"

    def dup_groups(combo: list[str]) -> int:
        seen = set()
        idx = [cols.index(c) for c in combo]
        for row in rows:
            key = tuple(str(row[ci]) if ci < len(row) else None for ci in idx)
            seen.add(key)
        return n - len(seen)

    from itertools import combinations
    best_near: tuple[int, list[str], bool] | None = None   # (dups, combo, exact)
    scans = 0
    for size in (1, 2, 3):
        for combo in combinations(cand, size):
            prod = _prod_distinct(combo, distinct)
            # 必要性剪枝：不同组合数 ≤ 各列 distinct 之积 < 行数 ⟹ 必有重复组合。
            if prod < n:
                continue
            if scans >= _GRAN_SCAN_CAP:
                # 扫描预算耗尽：distinct 积是组合数的上界 → dups ≥ n - prod（下界，
                # 陈述里写「至少」，绝不把估算当实数）。
                if best_near is None or (n - prod) > best_near[0]:
                    best_near = (max(n - prod, 1), list(combo), False)
                continue
            scans += 1
            dups = dup_groups(list(combo))
            if dups == 0:
                head = " × ".join(
                    ("一期「" + c + "」") if types.get(c) == "time" else ("一个「" + c + "」")
                    for c in combo)
                return _make_fact(
                    "granularity", 1,
                    f"一行 = {head}的组合：该 {size} 列组合在全表 {n:,} 行上恰一一对应（无重复组合）。",
                    {"rowKey": list(combo), "rows": n, "distinctCombos": n},
                    list(combo)), None
            # 「最接近唯一」= 实测重复组最少的组合（exact 恒优先于预算耗尽后的估算）。
            if best_near is None or (best_near[2] and dups < best_near[0]):
                best_near = (dups, list(combo), True)
    if best_near is not None:
        dups, combo, exact = best_near
        label = " × ".join(f"「{c}」" for c in combo)
        word = "仍有" if exact else "至少仍有"
        return _make_fact(
            "granularity", 1,
            f"≤3 列的组合里找不到唯一键：最接近的 {label} {word} {dups:,} 组重复——"
            f"行粒度比三维组合更细，或数据含重复行（构建 profile 的 duplicateRows 可核对）。",
            {"rowKey": list(combo), "duplicateGroups": dups, "rows": n, "exact": exact},
            list(combo)), None
    if cand:
        # 所有组合都被 distinct 积剪枝：挑 distinct 最高的单列给「至少仍有 n-distinct
        # 组重复」的下界陈述（distinct 积是组合数上界 → dups ≥ n - prod，绝不编实数）。
        best = max(cand, key=lambda c: int(distinct.get(c, 0) or 0))
        d = max(int(distinct.get(best, 0) or 0), 1)
        return _make_fact(
            "granularity", 1,
            f"≤3 列的组合里找不到唯一键：基数最高的「{best}」也只有 {d:,} 个不同值"
            f"（{n:,} 行，至少仍有 {n - d:,} 组重复）——行粒度比三维组合更细，"
            "或数据含重复行（构建 profile 的 duplicateRows 可核对）。",
            {"rowKey": [best], "distinct": d, "rows": n, "exact": False},
            [best]), None
    return None, "候选列的所有 ≤3 列组合都被 distinct 积剪枝（组合空间明显小于行数），粒度无法用 ≤3 列刻画"


def _prod_distinct(combo, distinct) -> int:
    prod = 1
    for c in combo:
        prod *= max(int(distinct.get(c, 0) or 0), 1)
    return prod


def _fact_top_mover(cols: list[str], rows: list[list[Any]], types: dict[str, str]) -> tuple[dict[str, Any] | None, str | None]:
    """② Top mover：最近一期对上一期的环比极值成员（主维度 × 主指标）。"""
    import date_parse
    time_col = next((c for c in cols if types.get(c) == "time"), None)
    if not time_col:
        return None, "无时间列，环比极值（top mover）无法判定"
    measure = _primary_measure(cols, rows, types)
    if not measure:
        return None, "无数值指标列，环比极值无法判定"
    ti = cols.index(time_col)
    mi = cols.index(measure)
    tvals = [r[ti] for r in rows if r[ti] is not None and str(r[ti]).strip() != ""]
    grain = date_parse.infer_grain(tvals, time_col)
    if not grain:
        return None, f"时间列「{time_col}」的粒度无法识别，环比极值无法判定"
    labels = sorted({str(date_parse.label(v, grain)) for v in tvals})
    if len(labels) < 2:
        return None, "时间列不足两期，环比极值无法判定"
    latest, prev = labels[-1], labels[-2]
    cat_cols = [c for c in cols if types.get(c) == "category"]
    counts: dict[str, set] = {c: set() for c in cat_cols}
    ci_map = {c: cols.index(c) for c in cat_cols}
    for row in rows:
        for c in cat_cols:
            v = row[ci_map[c]]
            if v is not None and str(v).strip() != "":
                counts[c].add(str(v))
    readable = sorted((c for c in cat_cols if len(counts[c]) >= 2),
                      key=lambda c: len(counts[c]))
    dim = readable[0] if readable else None
    if not dim:
        return None, "无可分组的维度列（基数 ≥2），环比极值无法判定"
    di = cols.index(dim)
    acc: dict[str, list[float]] = {}
    for row in rows:
        lab = date_parse.label(row[ti], grain) if row[ti] is not None else None
        if lab not in (latest, prev):
            continue
        v = row[mi]
        if not _is_num(v):
            continue
        key = str(row[di]) if row[di] is not None and str(row[di]).strip() != "" else "（空）"
        cell = acc.setdefault(key, [0.0, 0.0])
        cell[0 if lab == latest else 1] += float(v)
    movers = []
    for key, (now, before) in acc.items():
        delta = now - before
        movers.append((abs(delta), delta, now, before, key))
    if not movers:
        return None, "最近两期没有可聚合的指标值，环比极值无法判定"
    movers.sort(key=lambda m: (-m[0], m[4]))
    adelta, delta, now, before, key = movers[0]
    if before:
        pct = f"（{delta / abs(before) * 100:+,.1f}%）" if before else ""
    else:
        pct = "（上期为 0，无法算百分比）"
    stmt = (f"最近一期 {latest} 对上一期 {prev}：「{dim}」维度上 {measure} 环比变化最大的是"
            f"「{key}」（{_fmt_num(before)} → {_fmt_num(now)}，变化 {_fmt_num(delta)}{pct}）。")
    return _make_fact(
        "top_mover", 1, stmt,
        {"timeColumn": time_col, "grain": grain, "latest": latest, "previous": prev,
         "dimension": dim, "measure": measure, "member": key,
         "previousValue": before, "latestValue": now, "delta": delta},
        [time_col, dim, measure]), None


def _fact_concentration(cols: list[str], rows: list[list[Any]], types: dict[str, str],
                        distinct: dict[str, int]) -> tuple[list[dict[str, Any]], str | None]:
    """③ 集中度：主要维度上的 CR3 与 HHI（按主指标，intensive 语义用平均）。"""
    measure = _primary_measure(cols, rows, types)
    if not measure:
        return [], "无数值指标列，集中度无法判定"
    agg = _agg_of(measure)
    mi = cols.index(measure)
    cat_cols = [c for c in cols if types.get(c) == "category"
                and 3 <= int(distinct.get(c, 0) or 0) <= 50]
    cat_cols.sort(key=lambda c: int(distinct.get(c, 0) or 0))
    facts: list[dict[str, Any]] = []
    for dim in cat_cols[:_CONC_DIM_CAP]:
        di = cols.index(dim)
        acc: dict[str, float] = {}
        for row in rows:
            v = row[mi]
            if not _is_num(v):
                continue
            key = str(row[di]) if row[di] is not None and str(row[di]).strip() != "" else "（空）"
            acc[key] = acc.get(key, 0.0) + (float(v) if agg == "sum" else 1.0)
        if agg == "average":
            # intensive：先按维度求均值再比份额（把均值加总没有读者意义）。
            counts: dict[str, int] = {}
            for row in rows:
                if not _is_num(row[mi]):
                    continue
                key = str(row[di]) if row[di] is not None and str(row[di]).strip() != "" else "（空）"
                counts[key] = counts.get(key, 0) + 1
            acc = {k: acc[k] / counts[k] for k in acc if counts[k]}
        total = sum(acc.values())
        if total <= 0 or len(acc) < 3:
            continue
        ranked = sorted(acc.items(), key=lambda kv: (-kv[1], kv[0]))
        shares = [(k, v / total) for k, v in ranked]
        cr3 = sum(s for _k, s in shares[:3])
        hhi = sum(s * s for _k, s in shares)
        level = "高集中" if hhi > 0.25 else ("中等集中" if hhi >= 0.15 else "分散")
        top3 = "、".join(f"{k}（{v * 100:.1f}%）" for k, v in shares[:3])
        facts.append(_make_fact(
            "concentration", len(facts) + 1,
            f"按 {measure} {'均值' if agg == 'average' else '合计'}，前 3 个「{dim}」占 {cr3 * 100:.1f}%（CR3）；"
            f"HHI {hhi:.3f}（{level}）。头部：{top3}。",
            {"dimension": dim, "measure": measure, "aggregation": agg,
             "cr3": cr3, "hhi": hhi, "members": len(acc),
             "top3": [{"label": k, "share": s} for k, s in shares[:3]]},
            [dim, measure]))
    if not facts:
        return [], "没有基数 3–50 的可读维度列，集中度无法判定"
    return facts, None


def _fact_calendar_gap(cols: list[str], rows: list[list[Any]], types: dict[str, str]) -> tuple[dict[str, Any] | None, str | None]:
    """④ 断档异常期：时间列的缺失期（复用 date_parse 的 grain/label/sort_key 单源）。"""
    import datetime
    import date_parse
    time_col = next((c for c in cols if types.get(c) == "time"), None)
    if not time_col:
        return None, "无时间列，断档检查跳过"
    ti = cols.index(time_col)
    tvals = [r[ti] for r in rows if r[ti] is not None and str(r[ti]).strip() != ""]
    grain = date_parse.infer_grain(tvals, time_col)
    if not grain:
        return None, f"时间列「{time_col}」的粒度无法识别，断档检查跳过"
    labels = sorted({str(date_parse.label(v, grain)) for v in tvals})
    if len(labels) < 2:
        return None, "时间列不足两期，断档检查跳过"

    def next_label(label: str) -> str | None:
        try:
            if grain == "month":
                y, m = int(label[:4]), int(label[5:7])
                y, m = (y + 1, 1) if m == 12 else (y, m + 1)
                return f"{y:04d}-{m:02d}"
            if grain == "year":
                return f"{int(label) + 1:04d}"
            if grain == "quarter":
                y, q = int(label[:4]), int(label[6])
                y, q = (y + 1, 1) if q == 4 else (y, q + 1)
                return f"{y:04d}-Q{q}"
            if grain == "day":
                d = datetime.date(int(label[:4]), int(label[5:7]), int(label[8:10])) + datetime.timedelta(days=1)
                return d.isoformat()
            if grain == "week":
                w = date_parse.week_parts(label)
                if not w or not w[0]:
                    return None
                d = datetime.date.fromisocalendar(w[0], w[1], 1) + datetime.timedelta(days=7)
                iy, iw, _ = d.isocalendar()
                return f"{iy:04d}-W{iw:02d}"
        except (ValueError, IndexError):
            return None
        return None

    gaps: list[str] = []
    cur = labels[0]
    end = labels[-1]
    # 防御：期望序列上限（十年量级），异常跨度不做全枚举。
    for _ in range(10000):
        cur = next_label(cur)
        if cur is None or cur > end:
            break
        if cur not in set(labels):
            gaps.append(cur)
    gap_set = set(labels)
    if not gaps:
        return _make_fact(
            "calendar_gap", 1,
            f"时间列「{time_col}」按{ {'day': '日', 'week': '周', 'month': '月', 'quarter': '季度', 'year': '年'}[grain] }粒度"
            f"连续覆盖 {labels[0]}..{labels[-1]} 共 {len(labels)} 期，无缺失期。",
            {"timeColumn": time_col, "grain": grain, "first": labels[0],
             "last": labels[-1], "periods": len(labels), "gaps": []},
            [time_col]), None
    shown = "、".join(gaps[:5]) + ("…" if len(gaps) > 5 else "")
    return _make_fact(
        "calendar_gap", 1,
        f"时间列「{time_col}」在 {labels[0]}..{labels[-1]} 里缺失 {len(gaps)} 期：{shown}"
        "（断档口径与构建期时间解析 date_parse 一致；聚合/趋势图会把空档留空而不是造 0）。",
        {"timeColumn": time_col, "grain": grain, "first": labels[0],
         "last": labels[-1], "periods": len(labels), "gaps": gaps},
        [time_col]), None


def _fact_correlation(cols: list[str], rows: list[list[Any]], types: dict[str, str]) -> tuple[list[dict[str, Any]], str | None]:
    """⑤ 相关对：数值列两两 Pearson，取 |r| 最高 top3——陈述恒带「相关非因果」。"""
    num_cols = [c for c in cols if types.get(c) == "measure"]
    if len(num_cols) < 2:
        return [], "数值指标列不足 2，相关对无法判定"
    step = max(1, len(rows) // _PEARSON_SAMPLE)
    sample = rows[::step]
    series: dict[str, list[float]] = {}
    for c in num_cols:
        ci = cols.index(c)
        vals = [float(r[ci]) for r in sample if _is_num(r[ci])]
        if len(vals) >= 3:
            series[c] = vals
    pairs = []
    for i, a in enumerate(num_cols):
        for b in num_cols[i + 1:]:
            if a not in series or b not in series:
                continue
            # 两列都非空的配对样本（等长截取），零方差列的 r 无定义 → 跳过。
            ai, bi = cols.index(a), cols.index(b)
            xs, ys = [], []
            for r in sample:
                if _is_num(r[ai]) and _is_num(r[bi]):
                    xs.append(float(r[ai]))
                    ys.append(float(r[bi]))
            if len(xs) < 3:
                continue
            mx = sum(xs) / len(xs)
            my = sum(ys) / len(ys)
            sxx = sum((x - mx) ** 2 for x in xs)
            syy = sum((y - my) ** 2 for y in ys)
            if sxx <= 0 or syy <= 0:
                continue
            sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
            r = sxy / ((sxx ** 0.5) * (syy ** 0.5))
            pairs.append((abs(r), r, a, b, len(xs)))
    if not pairs:
        return [], "数值列两两之间无法计算 Pearson（零方差或样本不足）"
    pairs.sort(key=lambda p: (-p[0], p[2], p[3]))
    facts = []
    for ar, r, a, b, n in pairs:
        if ar < _CORR_FLOOR or len(facts) >= _CORR_PAIR_CAP:
            break
        strength = "强" if ar >= 0.8 else ("中等" if ar >= 0.5 else "弱")
        facts.append(_make_fact(
            "correlation", len(facts) + 1,
            f"「{a}」与「{b}」行级 Pearson r={r:.2f}（{strength}{'正' if r > 0 else '负'}相关，n={n:,}；相关非因果）。",
            {"a": a, "b": b, "r": r, "n": n},
            [a, b]))
    if not facts:
        return [], f"最高行级相关仅 |r|={pairs[0][0]:.2f}（<{_CORR_FLOOR}），无线性相关对可报"
    return facts, None


def _fact_sign(cols: list[str], rows: list[list[Any]], types: dict[str, str]) -> tuple[list[dict[str, Any]], str | None]:
    """⑥ 符号结构：跨零（正负混合）数值列 → 发散配色候选。

    这是既有 L21 lint（发散型指标无条件格式）的数据侧事实依据：server 报告没有
    行数据时，lint 可凭这条事实在任意符号语义列上给出发散建议（Flint type-registry
    diverging 分类的数据侧证据，refs/flint/src/core/type-registry.ts:63）。"""
    measures = [c for c in cols if types.get(c) == "measure"]
    if not measures:
        return [], "无数值指标列，符号结构无法判定"
    stats = {c: {"min": None, "max": None, "neg": 0, "pos": 0}
             for c in measures}
    idx = {c: cols.index(c) for c in measures}
    for row in rows:
        for c in measures:
            v = row[idx[c]]
            if not _is_num(v):
                continue
            f = float(v)
            st = stats[c]
            st["min"] = f if st["min"] is None else min(st["min"], f)
            st["max"] = f if st["max"] is None else max(st["max"], f)
            if f < 0:
                st["neg"] += 1
            elif f > 0:
                st["pos"] += 1
    facts = []
    for c in measures:
        st = stats[c]
        if st["min"] is not None and st["min"] < 0 < st["max"] and len(facts) < _SIGN_COL_CAP:
            facts.append(_make_fact(
                "sign", len(facts) + 1,
                f"「{c}」正负值混合（min {_fmt_num(st['min'])}，max {_fmt_num(st['max'])}，"
                f"负值 {st['neg']:,} 行）——发散配色候选（建议红负/正绿或以 0 为中点的发散色阶）。",
                {"column": c, "min": st["min"], "max": st["max"],
                 "negatives": st["neg"], "positives": st["pos"], "divergingCandidate": True},
                [c]))
    if not facts:
        return [], "所有数值列同号（无跨零列），无发散配色候选"
    return facts, None


def extract_facts(table: dict[str, Any],
                  distinct: dict[str, int] | None = None) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """六类确定性事实的唯一定点：返回 (facts ≤24, {kind: 跳过原因})。

    顺序固定（granularity → top_mover → concentration → calendar_gap →
    correlation → sign），超出总量上限时按序截断——facts 列表因此是确定性的，
    同一份数据重跑逐字节一致。"""
    cols, rows, types = _fact_column_view(table)
    distinct = distinct or {}
    facts: list[dict[str, Any]] = []
    skipped: dict[str, str] = {}

    def take(kind: str, produced: tuple) -> None:
        got, reason = produced
        if isinstance(got, list):
            if got:
                facts.extend(got)
            else:
                skipped[kind] = reason or "无可判定事实"
        elif got is not None:
            facts.append(got)
        else:
            skipped[kind] = reason or "无可判定事实"

    take("granularity", _fact_granularity(cols, rows, types, distinct))
    take("top_mover", _fact_top_mover(cols, rows, types))
    take("concentration", _fact_concentration(cols, rows, types, distinct))
    take("calendar_gap", _fact_calendar_gap(cols, rows, types))
    take("correlation", _fact_correlation(cols, rows, types))
    take("sign", _fact_sign(cols, rows, types))
    return facts[:_FACTS_CAP], skipped


def build_profile(table: dict[str, Any], source: str) -> dict[str, Any]:
    rows = table["rows"]
    columns = table["columns"]
    types = table["meta"]["types"]
    result: list[dict[str, Any]] = []
    for index, name in enumerate(columns):
        values = [row[index] for row in rows]
        non_null = [value for value in values if value is not None and value != ""]
        counts = Counter(str(value) for value in non_null)
        item: dict[str, Any] = {
            "name": name,
            "type": types[name],
            "nonNull": len(non_null),
            "missing": len(values) - len(non_null),
            "distinct": len(counts),
            "topValues": [{"value": _profile_value(value, types[name]), "count": count}
                          for value, count in counts.most_common(20)],
        }
        if types[name] == "measure" and non_null:
            numeric = sorted(float(value) for value in non_null)
            item.update({
                "min": _json_value(numeric[0]),
                "max": _json_value(numeric[-1]),
                "mean": _json_value(sum(numeric) / len(numeric)),
                "sum": _json_value(sum(numeric)),
            })
            # 语义候选（批次 2，design-semantics.md §2.7 LLM 注解策略的画像侧）：
            # 名称关键词推出的 Flint T2 标签（Price/Percentage/Revenue/Count），
            # 只对「会改变聚合语义」的标签给 hint——模型读画像拍板时不必裸猜。
            # 数值形态加推：0–1 有界数值 → Percentage（0-1 表示检测）。
            from measure_semantics import semantic_label_of
            sem = semantic_label_of(None, name)
            if sem is None and numeric and 0 <= numeric[0] and numeric[-1] <= 1 \
                    and numeric[-1] > 0:
                sem = "Percentage"
            if sem in ("Price", "Percentage", "PercentageChange", "Revenue",
                       "Cost", "Profit", "Score", "Temperature", "Sentiment",
                       "Correlation"):
                item["semanticHint"] = sem
                if sem == "Percentage":
                    # 表示检测（A2，Flint §3.4）：同一 percent 语义列的两种存值
                    #（0–1 比率 vs 0–100 百分数）——声明 measureTypes 时一并注明。
                    from measure_semantics import percent_representation
                    item["percentRepresentation"] = percent_representation(non_null)
        elif types[name] == "time" and non_null:
            ordered = sorted(str(value) for value in non_null)
            item.update({"min": ordered[0], "max": ordered[-1]})
        result.append(item)
    duplicate_rows = len(rows) - len({json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows})
    has_measure = any(c.get("type") == "measure" for c in result)
    record_dataset = bool(result) and not has_measure
    hints: list[str] = []
    if record_dataset:
        hints.append("recordDataset：每行 = 一条记录，无独立指标列；图表指标用行数（count / 合成指标「记录数」）")
    # Keep a small deterministic sample for the Agent's semantic pass. IDs are
    # masked in this view; the canonical table remains untouched for execution.
    sample_rows = []
    for row in rows[:50]:
        sample_rows.append([_profile_value(v, types[col]) for v, col in zip(row, columns)])
    quality = (table.get("meta") or {}).get("quality", {}) or {}
    for _fb_col, _fb_n in (quality.get("textFallback") or {}).items():
        _fb_samples = "、".join(str(x) for x in (quality.get("textFallbackSamples") or {}).get(_fb_col, [])[:3])
        hints.append(
            f"列「{_fb_col}」有 {_fb_n} 个非数值编码值（示例 {_fb_samples}）——已整列按编码列保留原值，"
            "不要声明为数值指标：去重计数（countd）可用，求和/平均不可用；如确需数值聚合请清洗源数据。")
    stats = column_stats(result, quality)
    # Carry the compact stats on the canonical table too: the singlefile / mixed
    # compile paths hand the canonical JSON to spec_to_report, and that is where
    # the manifest's ``datasets[].columnStats`` comes from. The returned profile
    # carries the same map so ``report_build --column-stats`` (server mode, which
    # deliberately never writes a canonical JSON) can forward it verbatim.
    meta = table.get("meta")
    if isinstance(meta, dict):
        meta["stats"] = stats
    # profile.facts（NA-01）：六类确定性事实是「模型写洞察时的真话来源」。除了
    # 落进 profile 产物，还随 canonical 表的 meta 携带——多数据集/合并路径的
    # compile 只拿得到 canonical JSON（没有 --column-stats 通道），facts 从
    # meta.facts 兜底进编译（spec_to_report.compile_spec），与 meta.stats 同一先例。
    distinct_by_name = {c.get("name"): int(c.get("distinct") or 0) for c in result}
    facts, facts_skipped = extract_facts(table, distinct_by_name)
    if isinstance(meta, dict):
        meta["facts"] = facts
    return {
        "source": source,
        "columnStats": stats,
        "rowCount": len(rows),
        "columnCount": len(columns),
        "duplicateRows": duplicate_rows,
        "columns": result,
        "sampleRows": sample_rows,
        "recordDataset": record_dataset,
        "hints": hints,
        "quality": quality,
        # 无法判定的类带原因（不编造）；facts 恒为列表（可为空），消费方按 id 引用。
        "facts": facts,
        "factsSkipped": facts_skipped,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Profile a pure 2D Excel or canonical JSON table")
    parser.add_argument("--table", required=True)
    parser.add_argument("--sheet", type=int, default=0)
    parser.add_argument("--out")
    parser.add_argument("--canonical-out")
    args = parser.parse_args()
    try:
        table = load_table_file(args.table, sheet=args.sheet)
        profile = build_profile(table, str(Path(args.table).resolve()))
        profile_text = json.dumps(profile, ensure_ascii=False, indent=2)
        if args.out:
            out_path = Path(args.out)
            # The model commonly points --out at build/profile.json before the
            # build dir exists; create it instead of failing the whole step.
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(profile_text + "\n", encoding="utf-8")
            # 机器模式（report_build 两个 spawn 点都继承 stdout）把数据事实打到
            # 构建控制台——编码列翻转/脏值置空必须让构建方当场看见，而不是只在
            # 报告的数据源配置里事后发现（真机教训：15,191 行洗空直到交付后才被问起）。
            for _h in profile.get("hints") or []:
                print("[data] " + _h)
            # profile.facts（NA-01）同口径上控制台：构建方当场能核对事实、
            # 拿 id 写 insights[].facts 绑定（NA-02 的证据引用）。
            for _f in profile.get("facts") or []:
                print(f"[fact] {_f.get('id')} {_f.get('statement')}")
            for _k, _reason in (profile.get("factsSkipped") or {}).items():
                print(f"[fact] ({_k} 未判定：{_reason})")
        else:
            print(profile_text)
        if args.canonical_out:
            Path(args.canonical_out).write_text(
                json.dumps(table, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        return 0
    except TableValidationError as error:
        print(f"Table contract violation: {error}", file=sys.stderr)
        return 2
    except Exception as error:
        print(f"Unable to profile table: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
