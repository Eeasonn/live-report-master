"""Row-level derived columns (计算列) — ONE implementation, every consumer.

The whitelist-op derived column (``dataset.derived.columns[i] = {name, op,
left, right}``) is a **row-level** transform: for every raw row it appends
``left op right`` as a real value. Once materialised it is an ordinary numeric
column, so every downstream layer (aggregation, tables, totals, KPI, filters,
TOP-N) works without knowing it was derived — the same "pre-aggregate and
reference it by name" contract Flint documents in
``refs/flint/src/core/aggregate.ts`` ("the derived column name IS the contract").

Why this module exists (mirror of ``scripts/date_parse.py``)
------------------------------------------------------------
The transform was written twice in spirit — compile-time
(``spec_to_report._materialize_derived``) and it was simply MISSING at
runtime for server reports (``scripts/report_build.py`` rejected the whole
request in server mode). Deriving it in a third place is exactly the drift
this repo forbids, so the body lives here and BOTH consumers call it:

  - compile side: ``scripts/spec_to_report.py`` (manifest columns + static option)
  - server side:  ``server/derived_columns.py`` (SQLite table at register + reload)

Determinism is part of the contract: identical (table, defs) must produce
identical rows on both sides, byte for byte. Non-numeric operands and division
by zero yield ``None`` (rendered as an empty cell, never 0).
"""
from __future__ import annotations

import re

from typing import Any, Callable

# Whitelist (docs semantic-config-and-derived-redesign.md §2/§8). Adding an op
# means extending this map AND the request schema whitelist in
# ``validate_request`` — never a second arithmetic path.
#
# Every op is **null-safe**: a missing/non-numeric operand (``_num_at`` → None,
# the documented "空值" contract) yields None for the whole cell. ``add``/``sub``/
# ``mul`` used to raise ``TypeError: unsupported operand type(s) for +: 'NoneType'``
# on a single blank cell — the JS mirror already returned null there, so the two
# sides disagreed (and a dirty file crashed the compile instead of leaving a gap).
def _binop(fn: Callable[[Any, Any], Any], *, guard_zero: bool = False) -> Callable[[Any, Any], Any]:
    def inner(a: Any, b: Any) -> Any:
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            return None
        if guard_zero and b in (0,):
            return None
        return fn(a, b)
    return inner


DERIVED_OP_FNS: dict[str, Callable[[Any, Any], Any]] = {
    "add": _binop(lambda a, b: a + b),
    "sub": _binop(lambda a, b: a - b),
    "mul": _binop(lambda a, b: a * b),
    "div": _binop(lambda a, b: a / b, guard_zero=True),
}


# ---- 时间差（datediff，对标 Quick BI「获取时间差」，2026-09-14）----------------
# 行级 op：{name, op:'datediff', left: 日期列|__today__, right: 日期列|__today__,
# unit: year|quarter|month|week|day}。值解析走 date_parse（唯一真源——源数据的
# 2026/9/5、20260905、ISO 时间戳等写法全部覆盖）；任一侧解析失败 → None（与四则
# 同一条空安全契约）。单位 = **完整单位**（不满一个不计入，Quick BI 同款：11-05 vs
# 10-01 相差月份 = 1）；返回带符号整数（left − right）。
DERIVED_TODAY_TOKEN = "__today__"
DERIVED_UNITS = ("year", "quarter", "month", "week", "day")


def _date_parts_safe(v: Any):
    if v is None or v == "":
        return None
    try:
        import date_parse
        p = date_parse.parts(v)
    except Exception:
        return None
    if not p:
        return None
    # date_parse.parts 返回 (y, mo, d) 元组；归一成 dict 供 _datediff_full 消费。
    return {"y": int(p[0] or 0), "mo": int(p[1] or 0), "d": int(p[2] or 1)}


def _datediff_full(a, b, unit: str):
    """完整单位差（a − b，带符号）。两侧是 date_parse.parts 形状的 dict 或 None。"""
    if not a or not b:
        return None
    ya, ma, da = int(a.get("y") or 0), int(a.get("mo") or 0), int(a.get("d") or 1)
    yb, mb, db = int(b.get("y") or 0), int(b.get("mo") or 0), int(b.get("d") or 1)
    if not ya or not yb or not ma or not mb:
        return None
    from datetime import date as _date
    try:
        days = (_date(ya, ma, da) - _date(yb, mb, db)).days
    except ValueError:
        return None
    sign = 1 if days >= 0 else -1
    if unit == "day":
        return days
    if unit == "week":
        return sign * (abs(days) // 7)
    # 完整月数：月差 -（日的“周年未到”修正），再按符号取整。
    months = (ya - yb) * 12 + (ma - mb)
    if days >= 0:
        if da < db:
            months -= 1
        months = max(months, 0)
    else:
        if da > db:
            months += 1
        months = min(months, 0)
    if unit == "month":
        return months
    if unit == "quarter":
        return int(months / 3)
    if unit == "year":
        return int(months / 12)
    return None


def _num_at(row: list, index: int) -> float | int | None:
    v = row[index] if index < len(row) else None
    try:
        if v in (None, ""):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def materialize(table: dict, defs: list) -> dict:
    """Append whitelist-op derived columns to one table (docs §2).

    Deterministic, materialised columns (never expressions): each new column is
    ``left op right`` per raw row; non-numeric operands / div-by-zero → null.
    The returned copy keeps the original table untouched so legacy paths stay
    byte-identical when derived columns are absent.

    Trusts its input shape: validation (op whitelist, left/right existence, name
    uniqueness) lives in ``validate_request._validate_datasets``. It does still
    re-check operand/name existence so a hand-written call cannot silently
    produce a mis-aligned row (see ``tests/test_derived_graph.py``).
    """
    if not defs:
        return table
    columns = list(table.get("columns") or [])
    known = set(columns)
    def _ok(ref, seen) -> bool:
        if isinstance(ref, (int, float)) and not isinstance(ref, bool):
            return True
        return ref in seen

    declared = {str(d.get("name")) for d in defs}
    seen = set(known)
    for item in defs:
        for side in ("left", "right"):
            ref = item[side]
            if item.get("op") == "datediff" and ref == DERIVED_TODAY_TOKEN:
                continue
            if _ok(ref, seen):
                continue
            if ref in declared:
                raise ValueError(
                    f"派生列 {item['name']!r} 的 {side} 引用了写在其后面的计算列：{ref!r}"
                    "（只允许引用写在前面的计算列）")
            raise ValueError(f"派生列 {item['name']!r} 引用了未知列：{ref!r}（{side}）")
        if item["name"] in seen:
            raise ValueError(f"派生列 {item['name']!r} 与既有列冲突")
        seen.add(item["name"])
    out = dict(table)
    out["columns"] = columns + [item["name"] for item in defs]
    idx = {c: i for i, c in enumerate(columns)}
    # 操作数可以是**原始/已算出的列名**（字符串）或**常量**（数字）。列引用只允许
    # 指向「写在前面的」计算列 —— 前向引用/自引用会被 validate_columns 拦下，所以
    # 这里逐条 append 后把新列登记进 idx，链式引用（净利率 = 净流入 / 收入 之后
    # 再 净利率×100）自然成立，且不需要第二遍求值。
    next_idx = len(columns)
    for item in defs:
        idx[item["name"]] = next_idx
        next_idx += 1
    head = len(columns)

    def operand(row: list, ref) -> Any:
        if isinstance(ref, (int, float)) and not isinstance(ref, bool):
            return ref
        return _num_at(row, idx.get(ref, -1))

    def raw_operand(row: list, ref) -> Any:
        if ref == DERIVED_TODAY_TOKEN:
            return DERIVED_TODAY_TOKEN
        j = idx.get(ref, -1)
        return row[j] if 0 <= j < len(row) else None

    meta = dict(table.get("meta") or {})
    types = dict(meta.get("types") or {})
    out_rows = []
    for row in table.get("rows", []):
        nr = list(row)
        for item in defs:
            if item["op"] == "datediff":
                unit = item.get("unit") or "day"
                unit = unit if unit in DERIVED_UNITS else "day"
                a, b = raw_operand(nr, item["left"]), raw_operand(nr, item["right"])
                pa = _date_parts_safe(None if a == DERIVED_TODAY_TOKEN else a)
                pb = _date_parts_safe(None if b == DERIVED_TODAY_TOKEN else b)
                if a == DERIVED_TODAY_TOKEN:
                    from datetime import date as _today
                    t = _today.today()
                    pa = {"y": t.year, "mo": t.month, "d": t.day}
                if b == DERIVED_TODAY_TOKEN:
                    from datetime import date as _today
                    t = _today.today()
                    pb = {"y": t.year, "mo": t.month, "d": t.day}
                nr.append(_datediff_full(pa, pb, unit))
                continue
            nr.append(DERIVED_OP_FNS[item["op"]](operand(nr, item["left"]),
                                                 operand(nr, item["right"])))
        out_rows.append(nr)
    assert len(out["columns"]) == head + len(defs)
    out["rows"] = out_rows
    for item in defs:
        types[item["name"]] = "measure"
    meta["types"] = types
    out["meta"] = meta
    return out


# =====================================================================================
# Aggregate-level derived measures (派生指标) — the ONE evaluator.
#
# Contract: the caller has ALREADY grouped/aggregated (compile: from the raw table;
# runtime: from `/aggregate`), and hands over plain number series. This module only
# turns those series into the derived series — pure math, no table access — so the
# Python side (compile) and the JS mirror (assets/runtime/chart-adapter.js,
# `#region DERIVED_EVAL`) can be compared value-for-value on a shared fixture.
#
#   series  = {name: [values…]}          visible base measures
#           ∪ {"agg:sum:收入": [values…]} per-{agg,col} dependency (see agg_key)
#   labels  = [dimension label per row]   needed only by `window` nodes
#   result  = {node name: [values…]}
#
# Value semantics (parity-critical, do not "improve" one side only):
#   * rate   = (cur − prev) / prev × 100, rounded to 1 decimal with the SAME
#              half-up rule as JS `Math.round` (see _round_half_up). Divided by the
#              SIGNED previous value — this is what the chart-scoped mom/yoy did, so
#              a migrated measure keeps the exact numbers users already saw.
#   * delta  = cur − prev (unrounded: both languages compute the same IEEE double).
#   * cum    = running sum in TIME order; null only while every value so far is null.
#   * ytd    (WP-6) = cum reset at each year boundary: the first period of a new
#              year starts a fresh running sum. The year comes from the SAME parser
#              as everything temporal (`date_parse.parts`, JS mirror `dateParts`)
#              with a `\d{4}` string fallback for the sandboxed parity fixture; a
#              label with no resolvable year never resets (deterministic both sides).
#   * rank / dense_rank / percent_rank (WP-6) = within-group ranking over the
#              CURRENT aggregation (partition = the aggregated dimension combo, i.e.
#              the rows the caller handed over), sorted by value DESCENDING; nulls
#              are excluded (rank stays null, never 0):
#                rank         → competition ties 1,2,2,4
#                dense_rank   → dense ties 1,2,2,3
#                percent_rank → AVERAGE ties (1, 2.5, 2.5, 4 → "122.5" style) as
#                               avg_rank / non-null-count × 100, 1 decimal — a
#                               percent-units value like window.rate (×100 baked in).
#   * nulls propagate: any null operand yields null (never 0), so a chart shows an
#     empty cell instead of an invented value.
# =====================================================================================

AGG_KEY_PREFIX = "agg:"
# 筛选型派生指标（kind='filtered'）的依赖序列键前缀。filtered 的值 = 条件聚合本身，
# 在聚合阶段物化（不经过 evaluate 的后聚合数学），键必须与普通 {agg,col} 键区分——
# 同一列可以带着不同 filters 被请求多次。id 由 normalize_measures 恒补齐，键稳定。
FM_KEY_PREFIX = "fm:"


def fm_key(node: dict) -> str:
    """Series key for a ``filtered`` node's conditional-aggregation dependency."""
    return FM_KEY_PREFIX + str(node.get("id") or node.get("name") or "")


def agg_key(agg: str, col: str) -> str:
    """Lookup key for a per-(aggregation, column) dependency series.

    Colons are used as the separator (readable in fixtures/logs); a column name
    would have to literally start with ``agg:`` to collide, and the caller supplies
    exactly the keys ``required_aggregations`` asks for, so a collision would show up
    as a missing series rather than a silent mix-up.
    """
    return f"{AGG_KEY_PREFIX}{agg}:{col}"


def closure_nodes(nodes: list[dict], by_name: dict[str, dict] | None = None) -> list[dict]:
    """图上指标沿 ``{ref}`` 引用的**传递闭包**（含起点），保持声明顺序。

    公式指标引用另一个派生指标（M1 = M2/2，M2 不在图上）时，evaluate 必须先算出
    M2——只把图上指标传进 evaluate 会在 resolve_ref 处以「引用了未知序列」崩溃。
    未知名字（不在 by_name 里）按原样保留（调用方随后会按缺依赖报可行动错误）。"""
    nmap = {n.get("name"): n for n in (by_name or {}).values() if isinstance(n, dict) and n.get("name")}
    for n in nodes or []:
        if isinstance(n, dict) and n.get("name"):
            nmap[n["name"]] = n
    seen: set[str] = set()
    out: list[dict] = []

    def refs_of(n: dict) -> list[str]:
        refs: list[str] = []
        if not isinstance(n, dict):
            return refs
        if n.get("kind") == "formula":
            stack = [n.get("expr")]
            while stack:
                cur = stack.pop()
                if not isinstance(cur, dict):
                    continue
                if "op" in cur:
                    stack.append(cur.get("left"))
                    stack.append(cur.get("right"))
                elif cur.get("ref") is not None:
                    refs.append(cur["ref"])
            return refs
        inp = n.get("input") or {}
        if isinstance(inp, dict) and inp.get("ref") is not None:
            refs.append(inp["ref"])
        return refs

    def visit(name: str) -> None:
        if name in seen:
            return
        seen.add(name)
        node = nmap.get(name)
        if not isinstance(node, dict):
            return
        for r in refs_of(node):
            if r in nmap:
                visit(r)

    # 先访起点（保持传入顺序），再按声明顺序补闭包里其余节点（确定性输出）。
    for n in nodes or []:
        if isinstance(n, dict) and n.get("name"):
            visit(n["name"])
    for n in (by_name or {}).values():
        if isinstance(n, dict) and n.get("name") in seen and all(n is not x for x in out):
            out.append(n)
    for n in nodes or []:
        if isinstance(n, dict) and n.get("name") and all(n is not x for x in out):
            out.append(n)
    return out


def required_aggregations(nodes: list[dict], declared: set[str] | None = None,
                          default_agg: Any = "sum",
                          by_name: dict[str, dict] | None = None) -> list[dict]:
    """The ``{agg, col}`` series the caller must aggregate for these nodes.

    A ``formula`` names its aggregations explicitly (``{agg,col}``). A ``window``
    or ``share`` (BI-H2) node whose ``input.ref`` is a raw column (not a declared
    derived measure) needs that column aggregated too — by ``default_agg`` (the
    measure's declared aggregation, else sum) — a per-column ``{col: agg}`` mapping
    or one fallback string — which mirrors how the chart aggregates it today.

    A ``filtered`` node emits its conditional-aggregation dependency:
    ``{agg, col, filters, key}`` — the caller aggregates ONLY the rows passing
    ``filters`` per group (server: per-measure filters on the measure item;
    compile/local: the same grouping pass with a per-series predicate). The key
    is ``fm_key(node)`` and MUST be the lookup key in the caller's series dict —
    ``evaluate`` reads it back by the same rule.

    Returned in first-appearance order so the compile side can append the helper
    series deterministically (and the runtime request stays stable).
    """
    nmap = {n.get("name"): n for n in (by_name or {}).values() if isinstance(n, dict) and n.get("name")}
    for n in nodes or []:
        if isinstance(n, dict) and n.get("name"):
            nmap[n["name"]] = n
    names = set(declared or ()) | {n["name"] for n in nodes} | set(nmap)
    out: list[dict] = []
    seen: set[str] = set()
    visited: set[str] = set()

    def default_for(col: str) -> str:
        if isinstance(default_agg, dict):
            return default_agg.get(col) or "sum"
        return default_agg or "sum"

    def add(agg: str, col: str, filters: Any = None, key: str | None = None) -> None:
        k = key or agg_key(agg, col)
        if k in seen:
            return
        seen.add(k)
        dep: dict = {"agg": agg, "col": col, "key": k}
        if filters:
            dep["filters"] = filters
        out.append(dep)

    def walk_expr(n: Any) -> None:
        if not isinstance(n, dict):
            return
        if "op" in n:
            walk_expr(n.get("left"))
            walk_expr(n.get("right"))
        elif "col" in n:
            add(n.get("agg") or default_for(n["col"]), n["col"])
        elif n.get("ref") is not None:
            _expand_ref(n["ref"])

    def _expand_ref(ref: str) -> None:
        # ref 命中**已声明节点** ≠ 依赖已满足：沿节点递归收集它自己的依赖
        # （declared 名单曾把「已声明但不在本图上」的指标当成满足，公式引用
        # 未上图指标时编译直接崩溃且报错误导）。
        target = nmap.get(ref)
        if isinstance(target, dict):
            walk_node(target)
        elif ref not in names:
            add(default_for(ref), ref)

    def walk_node(node: dict) -> None:
        name = node.get("name")
        if name:
            if name in visited:
                return
            visited.add(name)
        if node.get("kind") == "formula":
            walk_expr(node.get("expr"))
            return
        if node.get("kind") == "filtered":
            # 条件聚合依赖：col 缺省（count 计行数）时以 "*" 下发——服务端只允许
            # 合成列 "*" 配 count（_agg_expr 的 COUNT(*) 语义）。
            col = node.get("col") or "*"
            add(node.get("agg") or "count", col, node.get("filters") or [], fm_key(node))
            return
        # window 与 share（BI-H2）共用同一种 input 形状：{agg,col} 显式聚合，
        # {ref} 指向已声明派生指标（沿闭包展开），指向原始列时按默认聚合补一条
        # 依赖序列。
        inp = node.get("input") or {}
        if "agg" in inp and inp.get("col"):
            add(inp["agg"], inp["col"])
        elif inp.get("ref"):
            _expand_ref(inp["ref"])

    for node in nodes or []:
        walk_node(node)
    return out


def _round_half_up(value: float, decimals: int) -> float:
    """Mirror of JS ``Math.round(v * 10**d) / 10**d`` (floor(x + .5) semantics)."""
    import math
    f = 10 ** decimals
    return math.floor(value * f + 0.5) / f


def _label_year(label: Any) -> int | None:
    """Calendar year of a bucket label (WP-6 ytd year reset).

    ONE parser rule: ``date_parse.parts`` is the source of truth (the runtime JS
    mirror is ``dateParts``); the ``\\d{4}`` regex is only the fallback for the
    sandboxed parity fixture (no parser import there) and for labels the parser
    cannot read — both languages then fall back identically, so a label either
    yields the same year on both sides or no year at all (never a reset on one
    side only).
    """
    s = "" if label is None else str(label)
    if s:
        try:
            import date_parse
            p = date_parse.parts(s)
            if p and p[0]:
                return int(p[0])
        except Exception:
            pass
    m = re.search(r"(\d{4})", s)
    return int(m.group(1)) if m else None


def _rank_series(src: list, fn: str) -> list:
    """rank / dense_rank / percent_rank over the CURRENT aggregation (WP-6).

    Sorted by value DESCENDING; null values are excluded (their rank stays null).
    Tie semantics: rank = competition (1,2,2,4); dense_rank = dense (1,2,2,3);
    percent_rank = AVERAGE ties expressed as ``avg_rank / non-null count × 100``
    rounded to 1 decimal (the "1, 2.5, 2.5, 4" style, JS-mirrored bit for bit).
    """
    vals = [_finite(v) for v in src]
    out: list = [None] * len(vals)
    present = [(v, i) for i, v in enumerate(vals) if v is not None]
    present.sort(key=lambda t: (-t[0], t[1]))   # value desc, original index asc
    if fn == "percent_rank":
        total = len(present)
        pos = 0
        while pos < total:
            end = pos
            while end + 1 < total and present[end + 1][0] == present[pos][0]:
                end += 1
            avg_rank = (pos + end) / 2 + 1      # 1-based average of the run's ranks
            for k in range(pos, end + 1):
                out[present[k][1]] = _round_half_up(avg_rank / total * 100, 1)
            pos = end + 1
        return out
    rank = 0
    dense = 0
    for pos, (v, i) in enumerate(present):
        if pos == 0 or v != present[pos - 1][0]:
            rank = pos + 1
            dense += 1
        out[i] = rank if fn == "rank" else dense
    return out


def _finite(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def evaluate(nodes: list[dict], series: dict, labels: list | None = None, *,
             dimension: str | None = None, bucket: str | None = None,
             time_sort_key: Any = None, parent_of: dict | None = None,
             partition_of: list | None = None) -> dict:
    """Evaluate every node from the base ``series`` (topological order).

    ``dimension``/``bucket`` describe the chart's own axis: a ``window`` node is only
    computable when its ``timeField`` IS the chart dimension and its ``grain`` equals
    the chart's bucket (the D2 strict-equality rule — a different grain would need a
    re-aggregation the caller has not done). Mismatch raises with an actionable
    message instead of quietly comparing the wrong periods.

    ``time_sort_key`` maps a label to a sortable key (compile: ``_time_sort_key``;
    runtime: the date_parse mirror). Without it the labels are used in the order the
    caller supplied (rows are already chronological in that case).

    ``parent_of`` (BI-H2): ``{node name: {label: parent label}}`` maps each chart
    dimension label to its parent-group label, needed ONLY by share nodes with
    ``scope='parent'``. The compile side builds it from the raw table; the runtime
    either rebuilds it from local rows or uses the compile-time ``parentShare`` bake.

    ``partition_of``（窗口 × breakdown）：与 labels 平行的分区键数组（交叉图里 =
    每行第二个维度的值）。prev/cum/ytd 的时间游标**在各分区内独立推进**——否则
    「2025-02 × 产品A」的环比会拿「2025-01 × 产品B」当上期（跨系列错值）。公式/
    share/rank 与分区无关，仍整列求值。
    """
    from derived_model import RANK_WINDOW_FNS, DerivedError, topo_order
    by_name = {n["name"]: n for n in nodes}
    order = topo_order(nodes)
    computed: dict[str, list] = {}

    def need_series(key: str, owner: str) -> list:
        vals = series.get(key)
        if vals is None:
            raise DerivedError(
                f"派生指标「{owner}」缺少依赖序列 {key!r}；调用方必须先用 required_aggregations "
                "提供该聚合序列（编译侧聚合、运行时随聚合请求一起取回）")
        return vals

    def resolve_ref(ref: str, owner: str) -> list:
        if ref in computed:
            return computed[ref]
        if ref in series:
            return series[ref]
        fallback = agg_key("sum", ref)
        if fallback in series:
            return series[fallback]
        # sum 键缺席时按「任意聚合的同一列」回退：required_aggregations 用调用方
        # 的 default_agg 生成依赖键（标量 default_agg 的调用方如图表聚合=average
        # 会产 agg:average:col），resolve_ref 若只认 sum 就会「引用了未知序列」。
        # 取排序后首个，PY/JS 两侧同序、确定性一致。
        for key in sorted(series):
            if isinstance(key, str) and key.startswith(AGG_KEY_PREFIX):
                parts = key.split(":", 2)
                if len(parts) == 3 and parts[2] == ref:
                    return series[key]
        raise DerivedError(
            f"派生指标「{owner}」引用了未知序列 {ref!r}：既不是已计算的派生指标，"
            f"也不是调用方提供的指标/聚合序列（可用：{sorted(series)[:8]}…）")

    def eval_expr(node: Any, owner: str, index: int) -> float | None:
        if not isinstance(node, dict):
            return None
        if "const" in node:
            return _finite(node["const"])
        if "ref" in node:
            return _finite(resolve_ref(node["ref"], owner)[index])
        if "col" in node:
            return _finite(need_series(agg_key(node.get("agg") or "sum", node["col"]), owner)[index])
        op = node.get("op")
        left = eval_expr(node.get("left"), owner, index)
        right = eval_expr(node.get("right"), owner, index)
        if left is None or right is None:
            return None
        if op == "add":
            return left + right
        if op == "sub":
            return left - right
        if op == "mul":
            return left * right
        if op == "div":
            return (left / right) if right != 0 else None
        return None

    for name in order:
        node = by_name[name]
        if node["kind"] == "formula":
            lo = len(next(iter(series.values()))) if series else len(labels or [])
            computed[name] = [eval_expr(node.get("expr") or {}, name, i) for i in range(lo)]
            continue
        # ---- filtered (筛选型派生指标) ------------------------------------------
        # 值 = 条件聚合序列本身（调用方已按 fm_key 聚好：组内只聚通过 filters 的行，
        # 组宇宙与基础序列一致）。空池语义由聚合方保证：count/countd → 0，
        # sum/average/min/max → None——这里只透传，绝不二次加工。
        if node["kind"] == "filtered":
            computed[name] = list(need_series(fm_key(node), name))
            continue
        # ---- share (BI-H2 占比) --------------------------------------------------
        # 与合计行/colTotals 同一口径：分母 = 当前聚合全部值之和（all）或同父级组内
        # 之和（parent）。空值不进分母也不进分子；分母 0/无父级 → null（绝不造 0%）。
        # 值四舍五入到 1 位（与 window.rate 同一条 _round_half_up 规则，JS 镜像逐位一致）。
        if node["kind"] == "share":
            inp = node.get("input") or {}
            src = (need_series(agg_key(inp["agg"], inp["col"]), name) if "agg" in inp and inp.get("col")
                   else resolve_ref(inp.get("ref") or "", name))
            vals = [_finite(v) for v in src]
            n0 = len(vals)
            if node.get("scope") == "parent":
                pm = (parent_of or {}).get(name)
                if pm is None:
                    raise DerivedError(
                        f"派生指标「{name}」按父级占比需要「标签 → 父级」映射（parent_of）；"
                        "调用方必须从原始行数据构建（编译侧）或使用编译期烘焙的 parentShare（运行时）")
                lb = labels if labels is not None else []
                if len(lb) != n0:
                    raise DerivedError(f"派生指标「{name}」的标签数与序列长度不一致（{len(lb)} vs {n0}）")
                parents = [pm.get(str(lb[i])) if lb[i] is not None else None for i in range(n0)]
                sums: dict = {}
                for i in range(n0):
                    p = parents[i]
                    if p is not None and vals[i] is not None:
                        sums[p] = sums.get(p, 0) + vals[i]
                computed[name] = [
                    _round_half_up(vals[i] / sums[p] * 100, 1)
                    if (p is not None and vals[i] is not None and sums.get(p)) else None
                    for i, p in enumerate(parents)]
            else:
                total = 0.0
                for v in vals:
                    if v is not None:
                        total += v
                computed[name] = [_round_half_up(v / total * 100, 1) if (v is not None and total) else None
                                  for v in vals]
            continue
        # ---- window -------------------------------------------------------------
        inp = node.get("input") or {}
        fn = node["fn"]
        # rank 族（WP-6）没有时间语义：partition = 当前聚合维度组合，不走 timeField/
        # bucket 闸门（任何图表维度都可排名）。src 在闸门前解析，其余窗口算子的
        # 报错顺序与既有契约完全一致（闸门优先于缺序列）。
        if fn in RANK_WINDOW_FNS:
            src = (need_series(agg_key(inp["agg"], inp["col"]), name) if "agg" in inp and inp.get("col")
                   else resolve_ref(inp.get("ref") or "", name))
            computed[name] = _rank_series(src, fn)
            continue
        tfield, grain = node["timeField"], node["grain"]
        if dimension and tfield != dimension:
            raise DerivedError(
                f"派生指标「{name}」依赖时间列 {tfield!r}，但图表维度是 {dimension!r}；"
                "请把该维度换成它（或改用其它派生指标）")
        if bucket and grain != bucket:
            raise DerivedError(
                f"派生指标「{name}」按 {grain} 计算，但图表按 {bucket} 分桶；"
                "分桶必须与指标声明一致（否则比的是错周期）")
        if not bucket:
            raise DerivedError(
                f"派生指标「{name}」需要图表声明时间分桶（{grain}）才能计算；"
                "请为时间维度设置分桶")
        src = (need_series(agg_key(inp["agg"], inp["col"]), name) if "agg" in inp and inp.get("col")
               else resolve_ref(inp.get("ref") or "", name))
        lb = labels if labels is not None else []
        if len(lb) != len(src):
            raise DerivedError(f"派生指标「{name}」的标签数与序列长度不一致（{len(lb)} vs {len(src)}）")
        idx_order = sorted(range(len(src)), key=lambda i: time_sort_key(lb[i])) if time_sort_key \
            else list(range(len(src)))
        # 窗口游标按分区独立推进（无分区 = 单一空键分区，行为不变）。
        parts_seq: dict[str, list[int]] = {}
        for i in idx_order:
            key = "" if (not partition_of or i >= len(partition_of)
                         or partition_of[i] is None) else str(partition_of[i])
            parts_seq.setdefault(key, []).append(i)
        out: list = [None] * len(src)
        if fn == "prev":
            offset = int(node.get("offset") or 1)
            output = node.get("output") or "rate"
            for seq in parts_seq.values():
                for pos, i in enumerate(seq):
                    j = pos - offset
                    cur, prev = _finite(src[i]), (_finite(src[seq[j]]) if j >= 0 else None)
                    if output == "value":
                        # 「近 N 期的值」= prev itself（node_summary 渲染「近 N 期值」，
                        # normalize_measures 放行该 output）。空 prev 保持 null。
                        out[i] = prev
                    elif cur is None or prev is None or prev == 0:
                        out[i] = None
                    else:
                        out[i] = _round_half_up((cur - prev) / prev * 100, 1) if output == "rate" else cur - prev
        else:  # cum / ytd（WP-6：ytd = cum 按年重置，闸门与 cum 同一条）
            is_ytd = fn == "ytd"
            for seq in parts_seq.values():
                running = None
                cur_year = None
                for i in seq:
                    if is_ytd:
                        y = _label_year(lb[i])
                        if running is not None and y is not None and y != cur_year:
                            running = None      # 跨年首期：重新从当期值起算
                        if y is not None:
                            cur_year = y
                    cur = _finite(src[i])
                    if cur is not None:
                        running = cur if running is None else running + cur
                    out[i] = running
        computed[name] = out
    return computed


# ---- 维度值分组（dimGroups，对标 Quick BI「分组维度」，2026-09-14）--------------
# ``ds.dimGroups = [{name, source, groups:[{label, members:[…]}], other?}]`` 声明一个
# **虚拟维度**：物化成真列后与普通维度无异（聚合/表格/筛选/导出零感知）。与计算列
# 同一条「物化一次、处处当普通列」契约；对齐 Quick BI 限制：分组维度不进计算字段
# 引用宇宙（calcColsAll 不收，请求校验的列宇宙只认原始列/计算列/派生指标）。
def materialize_dim_groups(table: dict, decls: list) -> dict:
    """Append dimension-group columns to one table (idempotent, like materialize).

    Mapping per row: 原值 ∈ groups[].members → groups[].label；未命中 → other（声明了
    才归并，否则**保留原值**——QBI 同语义）。broken declaration raises（与 materialize
    同一条「浮出而不是静默」规矩）。
    """
    if not decls:
        return table
    columns = list(table.get("columns") or [])
    idx = {c: i for i, c in enumerate(columns)}
    next_idx = len(columns)
    pending = []
    for d in decls:
        if not isinstance(d, dict):
            raise ValueError("dimGroups 声明必须是对象")
        name = str(d.get("name") or "").strip()
        source = d.get("source")
        groups = d.get("groups") if isinstance(d.get("groups"), list) else []
        if not name:
            raise ValueError("dimGroups 声明缺 name")
        if name in idx or name in {p["name"] for p in pending}:
            raise ValueError(f"分组维度 {name!r} 与既有列冲突")
        if source not in idx and source not in {p["source"] for p in pending}:
            raise ValueError(f"分组维度 {name!r} 引用了未知列：{source!r}")
        if not groups:
            raise ValueError(f"分组维度 {name!r} 至少要有一个分组")
        for g in groups:
            if not isinstance(g, dict) or not str(g.get("label") or "").strip() or not isinstance(g.get("members"), list):
                raise ValueError(f"分组维度 {name!r} 的每个分组都要有 label 和 members 数组")
        pending.append({"name": name, "source": source,
                        "groups": [{"label": str(g["label"]), "members": [str(m) for m in g["members"]]}
                                   for g in groups],
                        "other": (str(d["other"]) if d.get("other") else "")})
    if not pending:
        return table
    out = dict(table)
    out["columns"] = columns + [p["name"] for p in pending]
    for p in pending:
        idx[p["name"]] = next_idx
        next_idx += 1
    head = len(columns)
    out_rows = []
    for row in table.get("rows", []):
        nr = list(row)
        for p in pending:
            si = idx[p["source"]]
            v = nr[si] if si < len(nr) else None
            sv = "" if v is None else str(v)
            hit = ""
            for g in p["groups"]:
                if sv in g["members"]:
                    hit = g["label"]
                    break
            nr.append(hit or (p["other"] if (p["other"] and sv != "") else v))
        out_rows.append(nr)
    assert len(out["columns"]) == head + len(pending)
    out["rows"] = out_rows
    return out
