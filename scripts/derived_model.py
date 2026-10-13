"""Aggregate-level derived measures (派生指标): shape, validation, migration.

Two DIFFERENT things live under ``dataset.derived`` (docs/derived-measure-redesign.md §2)
and must never be conflated:

* ``columns`` — **row-level** whitelist ops (计算列). Materialised into real
  number columns by ``scripts/derived_eval.py``; every downstream layer treats
  them as ordinary columns.
* ``measures`` — **aggregate-level** nodes (派生指标): evaluated AFTER
  aggregation, so they can express ratios of sums and period-over-period
  windows that a row-level column cannot:

    {"id": "m1", "name": "毛利率", "kind": "formula",
     "expr": {"op": "div", "left": {"agg": "sum", "col": "毛利额"},
                              "right": {"agg": "sum", "col": "收入"}},
     "measureType": {"type": "percent"}, "additive": false}

    {"id": "m2", "name": "净额月环比", "kind": "window",
     "input": {"ref": "净额"}, "fn": "prev", "grain": "month", "offset": 1,
     "timeField": "订单月份", "output": "rate",
     "measureType": {"type": "percent"}, "additive": false}

Why aggregate-level is a separate node (grounding):
  * Flint marks ``Amount: additive`` but ``Price``/``Percentage``/``PercentageChange``
    as ``intensive`` (``refs/flint/src/core/type-registry.ts:104-122``) — a ratio
    re-summed across a coarser bucket is simply wrong, so ``additive`` is
    computed from the AST (``is_additive_expr``) and rides on the node.
  * ``mom``/``yoy``/``累计`` are ONE window operator with parameters
    (``fn``/``offset``/``grain``) rather than three hardcoded kinds: the old
    branch table hardcoded ``week → 52`` (wrong: 52 weeks ≠ a year, leap years
    have 53) and could not express 季度环比 / 两期同比 at all.
  * The window's grain is DECLARED on the node (user decision D2), which is what
    makes it chart-independent enough to live in the data source at all. The
    chart only owes one thing: its time axis must use ``timeField``.

This module is **pure** (no I/O, no table access): validation, normalisation and
migration only. Evaluation lands in P2 (``derived_eval``) and the request-layer
gate in ``spec_to_report`` refuses to *use* a derived measure until then with an
actionable message — storing a spec the compiler cannot evaluate would render an
empty card, which is worse than a clear error.
"""
from __future__ import annotations

from typing import Any, Iterable

# Whitelists. Adding a member means extending the request schema AND the
# evaluator in the same change (never a second arithmetic path).
EXPR_OPS = ("add", "sub", "mul", "div")
AGG_FNS = ("sum", "average", "count", "countd", "min", "max")
# WP-6（docs/product-iteration-plan.md §3）: 查询级窗口算子扩展。
#   prev/cum/ytd —— 时间窗口（timeField 闸门 + grain==bucket 严格相等校验沿用）；
#   rank/dense_rank/percent_rank —— 组内排名，partition = 当前聚合维度组合
#   （即图表聚合后的全部行），**无时间语义**：不声明 grain/timeField，
#   求值时也不走时间闸门。ytd = cum 按年重置（时间闸门沿用）。
WINDOW_FNS = ("prev", "cum", "rank", "dense_rank", "percent_rank", "ytd")
RANK_WINDOW_FNS = ("rank", "dense_rank", "percent_rank")
GRAINS = ("week", "month", "quarter", "year")
WINDOW_OUTPUTS = ("rate", "delta", "value")
# BI-H2: ``share`` = 占比（值 ÷ 分母 ×100）。分母由 scope 决定：all = 当前聚合全部
# 值之和（与表格合计行/colTotals 同口径），parent = 同父级组内之和（需 parentField）。
# 固定子集（filter）占比依赖图表筛选选择集，不是数据源层概念 → 不建（旧 chart 级
# 声明继续由运行时 legacy 路径求值）。
# ``filtered``（筛选型派生指标，docs/filtered-measures-and-week-fields-2026-10-09.md）：
# 条件聚合 AGG(CASE WHEN 谓词 THEN 列 END)——组宇宙由图表分组决定、组内只聚通过
# 筛选的行；同环比 = window 节点 input.ref 指向它（既有求值器零改动复用）。
MEASURE_KINDS = ("formula", "window", "share", "filtered")
# 筛选算子 = 既有筛选管道词汇（_row_filter / matchFilter / _filter_clause）的子集，
# 三端谓词语义逐一继承，不造第二份比较器。
FILTER_OPS = ("eq", "ne", "gt", "ge", "lt", "le", "between", "in")
MAX_FILTERS = 10
SHARE_SCOPES = ("all", "parent")
MAX_MEASURES = 16

# How many periods back a "same period last year" comparison needs, per grain.
# ``week`` is deliberately absent: 52 weeks ≠ 1 year (leap years have 53), so a
# week-grain YoY must be expressed explicitly (or rebuilt by the user) instead of
# silently producing an off-by-one comparison.
YOY_OFFSET = {"month": 12, "quarter": 4, "year": 1}

_MEASURE_FIELDS = {
    "formula": {"id", "name", "kind", "expr", "measureType", "additive", "decimals", "semantic"},
    "window": {"id", "name", "kind", "input", "fn", "grain", "offset", "timeField",
               "output", "measureType", "additive", "decimals", "semantic"},
    "share": {"id", "name", "kind", "input", "scope", "parentField",
              "measureType", "additive", "decimals", "semantic"},
    "filtered": {"id", "name", "kind", "agg", "col", "filters",
                 "measureType", "additive", "decimals", "semantic"},
}
# 计算列（行级）的定义字段。``aggregation``/``decimals`` 曾被白名单放行，但**没有任何
# 消费方**（编译侧与运行时都不读）——那是「写了没效果」的陷阱：请求作者以为设了默认
# 聚合/小数位。字段级口径统一走 ``datasets[].measureTypes[col]``（字段配置里的
# 「默认聚合 / 格式」），所以这里不再接受这两个键（带上会明确报错，而不是静默忽略）。
_COLUMN_FIELDS = {"name", "op", "left", "right", "unit"}


class DerivedError(ValueError):
    """Shape/semantics violation in a derived declaration (message is user-facing)."""


# ---------------------- normalisation --------------------------------------------

def normalize_columns(value: Any) -> list[dict]:
    """Canonicalise the row-level ``columns`` slot.

    Accepts the canonical ``{"columns": [...]}`` object **and** the legacy flat
    list (the runtime canvas wrote ``ds.derived = [...]`` in an early revision —
    that shape silently disabled every consumer that expected an object). Returns
    ``[]`` for anything absent/empty.
    """
    defs = value
    if isinstance(value, dict):
        defs = value.get("columns")
    if defs is None:
        return []
    if not isinstance(defs, list):
        raise DerivedError("derived.columns 必须是数组")
    out = []
    for i, item in enumerate(defs):
        if not isinstance(item, dict):
            raise DerivedError(f"derived.columns[{i}] 必须是对象")
        unknown = sorted(set(item) - _COLUMN_FIELDS)
        if unknown:
            hint = ""
            if set(unknown) & {"aggregation", "decimals"}:
                hint = "（字段的默认聚合/格式写在 datasets[].measureTypes）"
            raise DerivedError(f"derived.columns[{i}] 不支持字段：{unknown}{hint}")
        out.append(dict(item))
    return out


def normalize_measures(value: Any) -> list[dict]:
    """Canonicalise the aggregate-level ``measures`` slot (defaults filled in).

    Only shape/defaults — existence + DAG checks live in ``validate_measures``
    because they need the dataset's column set.
    """
    defs = value
    if isinstance(value, dict):
        defs = value.get("measures")
    if defs is None:
        return []
    if not isinstance(defs, list):
        raise DerivedError("derived.measures 必须是数组")
    if len(defs) > MAX_MEASURES:
        raise DerivedError(f"derived.measures 最多 {MAX_MEASURES} 项（当前 {len(defs)}）")
    # 语义注解受控词汇（lazy import：measure_semantics 拉 semantics 包，模块导入期
    # 不需要）。「semantic」槽位与 measureTypes[col].semantic 同一受控词汇。
    from measure_semantics import SEMANTIC_VOCAB, semantic_canonical
    out: list[dict] = []
    seen_names: set[str] = set()
    seen_ids: set[str] = set()
    for i, item in enumerate(defs):
        if not isinstance(item, dict):
            raise DerivedError(f"derived.measures[{i}] 必须是对象")
        kind = item.get("kind")
        if kind not in MEASURE_KINDS:
            raise DerivedError(
                f"derived.measures[{i}].kind 必须是 {'/'.join(MEASURE_KINDS)}；"
                f"行级公式请写在 derived.columns（op/left/right），聚合级公式用 kind='formula'")
        unknown = sorted(set(item) - _MEASURE_FIELDS[kind])
        if unknown:
            raise DerivedError(f"derived.measures[{i}]（{kind}）不支持字段：{unknown}")
        name = item.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 40:
            raise DerivedError(f"derived.measures[{i}].name 必须是非空字符串且 ≤40 字")
        if name in seen_names:
            raise DerivedError(f"derived.measures 名称重复：{name!r}")
        seen_names.add(name)
        mid = item.get("id") or f"m{i + 1}"
        if not isinstance(mid, str) or not mid.strip():
            raise DerivedError(f"derived.measures[{i}].id 必须是非空字符串")
        if mid in seen_ids:
            raise DerivedError(f"derived.measures id 重复：{mid!r}")
        seen_ids.add(mid)
        node = dict(item)
        node["id"] = mid
        node["name"] = name.strip()
        # 语义注解槽（2026-10-13 收尾批）：显式声明才写键（缺省不落盘，旧节点零漂移）；
        # 值归一到 Flint 受控标签——运行时 measureFormat/零基线/轴域据此渲染
        # （如毛利率显式 Percentage，不再依赖名字恰好含「率」）。
        sem = item.get("semantic")
        if sem is None:
            node.pop("semantic", None)
        else:
            canon = semantic_canonical(sem)
            if canon is None:
                raise DerivedError(
                    f"derived.measures[{i}].semantic 未知语义标签 {sem!r}；"
                    f"允许：{sorted(SEMANTIC_VOCAB)}")
            node["semantic"] = canon
        if kind == "formula":
            node["expr"] = _normalize_expr(item.get("expr"), f"derived.measures[{i}]")
            node.setdefault("measureType", {"type": "number"})
        elif kind == "filtered":
            agg = item.get("agg") or "count"
            if agg not in AGG_FNS:
                raise DerivedError(f"derived.measures[{i}].agg 必须是 {'/'.join(AGG_FNS)}")
            node["agg"] = agg
            col = item.get("col")
            if agg == "count":
                # count 允许省略目标列 = 数「通过筛选的行数」（COUNT(*) 条件版）；
                # 带列时是「通过筛选且该列非空」的行数（与 count 既有语义一致）。
                if col is not None and (not isinstance(col, str) or not col.strip()):
                    raise DerivedError(f"derived.measures[{i}].col 必须是非空字符串")
                if not (isinstance(col, str) and col.strip()):
                    node.pop("col", None)
                else:
                    node["col"] = col.strip()
            else:
                if not isinstance(col, str) or not col.strip():
                    raise DerivedError(
                        f"derived.measures[{i}].col 必填：count 之外的聚合都要声明目标列")
                node["col"] = col.strip()
            node["filters"] = _normalize_filters(item.get("filters"), f"derived.measures[{i}]")
            node.setdefault("measureType", {"type": "number"})
        elif kind == "share":
            node["input"] = _normalize_input(item.get("input"), f"derived.measures[{i}]")
            scope = item.get("scope") or "all"
            if scope not in SHARE_SCOPES:
                raise DerivedError(
                    f"derived.measures[{i}].scope 必须是 {'/'.join(SHARE_SCOPES)}"
                    "（all=占总计；parent=占父级，需 parentField）")
            node["scope"] = scope
            if scope == "parent":
                pf = item.get("parentField")
                if not isinstance(pf, str) or not pf.strip():
                    raise DerivedError(
                        f"derived.measures[{i}].parentField 必填：按父级占比要声明父级列"
                        "（图表维度应是它的子级）")
                node["parentField"] = pf.strip()
            else:
                node.pop("parentField", None)
            node.setdefault("measureType", {"type": "percent"})
        else:
            node["input"] = _normalize_input(item.get("input"), f"derived.measures[{i}]")
            fn = item.get("fn")
            if fn not in WINDOW_FNS:
                raise DerivedError(
                    f"derived.measures[{i}].fn 必须是 {'/'.join(WINDOW_FNS)}"
                    "（环比 = prev+offset 1、同比 = prev+offset 12(月)/4(季)/1(年)、"
                    "累计 = cum、年累计 = ytd、组内排名 = rank/dense_rank/percent_rank）")
            node["fn"] = fn
            if fn in RANK_WINDOW_FNS:
                # 排名族没有时间语义（partition = 当前聚合维度组合）：不声明、也不
                # 接受 grain/timeField/offset——它们是「写了没效果」的陷阱字段，
                # 归一阶段直接剥掉（与 cum 剥 offset 同一条先例）。
                node.pop("offset", None)
                node.pop("grain", None)
                node.pop("timeField", None)
                node["output"] = "rate" if fn == "percent_rank" else "value"
            else:
                grain = item.get("grain")
                if grain not in GRAINS:
                    raise DerivedError(f"derived.measures[{i}].grain 必须是 {'/'.join(GRAINS)}")
                node["grain"] = grain
                tfield = item.get("timeField")
                if not isinstance(tfield, str) or not tfield.strip():
                    raise DerivedError(
                        f"derived.measures[{i}].timeField 必填：窗口指标必须声明自己的时间列"
                        "（这正是它能放在数据源层、与图表无关的前提）")
                node["timeField"] = tfield.strip()
                if fn == "prev":
                    offset = item.get("offset")
                    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 1:
                        raise DerivedError(f"derived.measures[{i}].offset 必须是 ≥1 的整数（prev 的期数）")
                    node["offset"] = offset
                    node.setdefault("output", "rate")
                else:  # cum / ytd
                    node.pop("offset", None)
                    node["output"] = "delta"
            if node["output"] not in WINDOW_OUTPUTS:
                raise DerivedError(f"derived.measures[{i}].output 必须是 {'/'.join(WINDOW_OUTPUTS)}")
            node.setdefault("measureType", {"type": "percent"} if node["output"] == "rate"
                            else {"type": "number"})
        out.append(node)
    # Additivity is derived from the AST unless the author pinned it explicitly.
    for node in out:
        if "additive" not in node:
            node["additive"] = _additive_of(node, {n["name"]: n for n in out})
    return out


def _normalize_input(value: Any, where: str) -> dict:
    """``input`` accepting ``{ref}`` / ``{agg,col}`` / a bare name string."""
    if isinstance(value, str) and value.strip():
        return {"ref": value.strip()}
    if not isinstance(value, dict):
        raise DerivedError(f"{where}.input 必须是 {{ref:'指标名'}} 或 {{agg:'sum',col:'列名'}}")
    if "ref" in value:
        unknown = sorted(set(value) - {"ref"})
        if unknown:
            raise DerivedError(f"{where}.input 不支持字段：{unknown}")
        ref = value.get("ref")
        if not isinstance(ref, str) or not ref.strip():
            raise DerivedError(f"{where}.input.ref 必须是非空字符串")
        return {"ref": ref.strip()}
    unknown = sorted(set(value) - {"agg", "col"})
    if unknown:
        raise DerivedError(f"{where}.input 不支持字段：{unknown}")
    if value.get("agg") not in AGG_FNS:
        raise DerivedError(f"{where}.input.agg 必须是 {'/'.join(AGG_FNS)}")
    col = value.get("col")
    if not isinstance(col, str) or not col.strip():
        raise DerivedError(f"{where}.input.col 必须是非空字符串")
    return {"agg": value["agg"], "col": col.strip()}


def _normalize_filters(value: Any, where: str) -> list[dict]:
    """Canonicalise the ``filtered`` node's filter list (docs D6).

    Shape is the EXISTING filter-pipeline predicate ``{column, op, value}`` —
    deliberately identical to the report-level filter bar, so `_row_filter` /
    `matchFilter` / `_predicate_sql` consume it with ZERO translation (a `col`
    key would be silently skipped by all three: `column` is what they read).
    ``value`` shape follows the op (between → [low, high], in → non-empty list,
    else a scalar). An empty value is rejected rather than read as "no
    constraint" — a measure-level predicate that silently drops itself would
    change the number with no visible reason."""
    if not isinstance(value, list) or not value:
        raise DerivedError(f"{where}.filters 必须是非空数组（至少 1 条筛选条件）")
    if len(value) > MAX_FILTERS:
        raise DerivedError(f"{where}.filters 最多 {MAX_FILTERS} 条")
    out: list[dict] = []
    for i, f in enumerate(value):
        fw = f"{where}.filters[{i}]"
        if not isinstance(f, dict):
            raise DerivedError(f"{fw} 必须是对象 {{column, op, value}}")
        unknown = sorted(set(f) - {"column", "op", "value"})
        if unknown:
            raise DerivedError(f"{fw} 不支持字段：{unknown}")
        col = f.get("column")
        if not isinstance(col, str) or not col.strip():
            raise DerivedError(f"{fw}.column 必须是非空字符串（与报告筛选同一形状）")
        op = f.get("op") or "eq"
        if op not in FILTER_OPS:
            raise DerivedError(f"{fw}.op 必须是 {'/'.join(FILTER_OPS)}")
        val = f.get("value")
        if op == "between":
            if not isinstance(val, (list, tuple)) or len(val) != 2:
                raise DerivedError(f"{fw}（between）的 value 必须是 [低, 高]")
            val = [val[0], val[1]]
        elif op == "in":
            if not isinstance(val, (list, tuple)) or not len(val):
                raise DerivedError(f"{fw}（in）的 value 必须是非空数组")
            val = list(val)
        elif val is None or (isinstance(val, str) and not val.strip()):
            raise DerivedError(f"{fw} 缺少 value（空值会被读成「不约束」，必须显式写）")
        out.append({"column": col.strip(), "op": op, "value": val})
    return out


def _normalize_expr(value: Any, where: str) -> dict:
    """Canonicalise an expression AST (recursive)."""
    if not isinstance(value, dict):
        raise DerivedError(f"{where}.expr 必须是对象（{{const}} / {{agg,col}} / {{ref}} / {{op,left,right}}）")
    keys = set(value)
    if keys == {"const"}:
        c = value["const"]
        if isinstance(c, bool) or not isinstance(c, (int, float)):
            raise DerivedError(f"{where}.expr.const 必须是数字")
        return {"const": c}
    if "ref" in keys:
        unknown = sorted(keys - {"ref"})
        if unknown:
            raise DerivedError(f"{where}.expr 不支持字段：{unknown}")
        ref = value.get("ref")
        if not isinstance(ref, str) or not ref.strip():
            raise DerivedError(f"{where}.expr.ref 必须是非空字符串")
        return {"ref": ref.strip()}
    if "col" in keys and "agg" not in keys:
        raise DerivedError(
            f"{where}.expr 不能直接引用裸列 {value.get('col')!r}：聚合级公式必须先聚合"
            "（写 {agg:'sum',col:'…'}）。逐行计算请用 derived.columns（op/left/right）")
    if keys == {"agg", "col"}:
        if value["agg"] not in AGG_FNS:
            raise DerivedError(f"{where}.expr.agg 必须是 {'/'.join(AGG_FNS)}")
        col = value.get("col")
        if not isinstance(col, str) or not col.strip():
            raise DerivedError(f"{where}.expr.col 必须是非空字符串")
        return {"agg": value["agg"], "col": col.strip()}
    if keys == {"op", "left", "right"}:
        if value["op"] not in EXPR_OPS:
            raise DerivedError(f"{where}.expr.op 必须是 {'/'.join(EXPR_OPS)}")
        return {"op": value["op"],
                "left": _normalize_expr(value.get("left"), where),
                "right": _normalize_expr(value.get("right"), where)}
    unknown = sorted(keys - {"const", "ref", "agg", "col", "op", "left", "right"})
    raise DerivedError(
        f"{where}.expr 形状不合法（不支持字段：{unknown or sorted(keys)}）；"
        "允许：{const:1} / {ref:'指标名'} / {agg:'sum',col:'列名'} / {op:'add|sub|mul|div',left,right}")


# ---------------------- traversal helpers ----------------------------------------

def expr_columns(node: dict) -> list[str]:
    """Every raw column an expression reads (order of appearance, deduped)."""
    out: list[str] = []

    def walk(n: Any) -> None:
        if not isinstance(n, dict):
            return
        if "op" in n:
            walk(n.get("left"))
            walk(n.get("right"))
        elif "col" in n:
            if n["col"] not in out:
                out.append(n["col"])
    walk(node)
    return out


def expr_refs(node: dict) -> list[str]:
    """Every derived-measure name an expression references (deduped)."""
    out: list[str] = []

    def walk(n: Any) -> None:
        if not isinstance(n, dict):
            return
        if "op" in n:
            walk(n.get("left"))
            walk(n.get("right"))
        elif "ref" in n:
            if n["ref"] not in out:
                out.append(n["ref"])
    walk(node)
    return out


def node_refs(node: dict, names: Iterable[str] | None = None) -> list[str]:
    """Derived-measure DEPENDENCIES of a node (the DAG edges).

    A ``formula`` composes derived measures (a raw column reference must be
    written ``{agg,col}``), so every ``expr`` ref is a dependency. A ``window``
    node's ``input.ref`` may name EITHER a derived measure OR a raw column (the
    evaluator aggregates a raw column by its default aggregation) — only the
    former is a dependency. Pass ``names`` (the declared measure names) to filter;
    without it every ref is returned.
    """
    if node.get("kind") == "formula":
        return expr_refs(node.get("expr") or {})
    inp = node.get("input") or {}
    ref = inp.get("ref") if isinstance(inp, dict) else None
    if not ref:
        return []
    if names is None:
        return [ref]
    return [ref] if ref in set(names) else []


def node_columns(node: dict) -> list[str]:
    """Raw columns a node reads."""
    if node.get("kind") == "filtered":
        out: list[str] = []
        if node.get("col"):
            out.append(node["col"])
        for f in node.get("filters") or []:
            if isinstance(f, dict) and f.get("column") and f["column"] not in out:
                out.append(f["column"])
        return out
    if node.get("kind") == "formula":
        return expr_columns(node.get("expr") or {})
    inp = node.get("input") or {}
    return [inp["col"]] if isinstance(inp, dict) and inp.get("col") else []


def _is_const_only(n: Any) -> bool:
    """An expression composed only of constants (no aggregation, no refs)."""
    if not isinstance(n, dict):
        return False
    if "const" in n:
        return True
    if "op" in n:
        return _is_const_only(n.get("left")) and _is_const_only(n.get("right"))
    return False


def _additive_of(node: dict, by_name: dict, seen: tuple = ()) -> bool:
    """Whether the node may be SUMMED across a coarser grouping.

    Grounded in Flint's ``aggRole``: sums/counts are additive, averages and
    min/max are not, ratios are not, and a window comparison never is. A product
    is additive only when exactly one side is a constant expression.
    """
    if node.get("kind") in ("window", "share"):
        return False   # 占比是 intensive（Flint Percentage）：跨更粗分组重加总必错
        # window 一律不可加：prev/cum/ytd 是跨期运算、rank 族（WP-6）是组内序数——
        # 同 average 族的 intensive 语义，跨更粗分组 SUM 毫无意义。
    if node.get("kind") == "filtered":
        # 条件聚合与普通 {agg,col} 同一条可加性规则：sum/count 可加；
        # countd（去重跨组不可加）、average/min/max 不可加。
        return node.get("agg") in ("sum", "count")
    expr = node.get("expr") or {}

    def walk(n: Any) -> bool:
        if not isinstance(n, dict):
            return False
        if "const" in n:
            return True
        if "ref" in n:
            ref = n["ref"]
            if ref in seen:          # cycle: validate_measures reports it properly
                return False
            target = by_name.get(ref)
            if target is None:
                return False         # a raw-column ref is aggregated by the caller
            return _additive_of(target, by_name, seen + (ref,)) if target is not node else True
        if "col" in n:
            return n.get("agg") in ("sum", "count")
        op = n.get("op")
        if op in ("add", "sub"):
            return walk(n.get("left")) and walk(n.get("right"))
        if op == "mul":
            l, r = n.get("left"), n.get("right")
            if _is_const_only(l):
                return walk(r)
            if _is_const_only(r):
                return walk(l)
            return False
        return False

    return walk(expr)


def measure_ids(defs: Iterable[dict]) -> dict[str, str]:
    return {d["name"]: d["id"] for d in defs if d.get("name")}


def topo_order(defs: list[dict]) -> list[str]:
    """Names in dependency order (a referenced measure always comes first).

    Raises ``DerivedError`` on an unknown reference or a cycle — the evaluator
    walks entries in array order, so a cycle would otherwise silently produce
    Nones (``applyDerivedMeasures`` had exactly that bug).
    """
    by_name = {d["name"]: d for d in defs}
    order: list[str] = []
    state: dict[str, int] = {}   # 0 = visiting, 1 = done

    def visit(name: str, path: tuple) -> None:
        if state.get(name) == 1:
            return
        if state.get(name) == 0:
            raise DerivedError("派生指标存在循环引用：" + " → ".join(path + (name,)))
        node = by_name.get(name)
        if node is None:
            return
        state[name] = 0
        for ref in node_refs(node, by_name):
            visit(ref, path + (name,))
        state[name] = 1
        order.append(name)

    for d in defs:
        visit(d["name"], ())
    return order


# ---------------------- validation ------------------------------------------------

def validate_measures(defs: list[dict], columns: Iterable[str] | None, time_columns: Iterable[str] | None,
                      where: str = "datasets[].derived", check_columns: bool = True) -> None:
    """Existence + reference + DAG validation for the normalised measure nodes.

    ``columns`` is the known column universe (raw columns + row-level derived
    columns); ``time_columns`` the subset with a time role. Pass
    ``check_columns=False`` when validating a request BEFORE profiling (the
    compiler re-validates against the real table — see ``compile_spec``).
    """
    names = {d["name"] for d in defs}
    known_cols = set(columns or ())
    known_times = set(time_columns or ())
    for node in defs:
        label = f"{where}「{node['name']}」"
        if node["name"] in known_cols:
            raise DerivedError(f"{label} 与既有列同名（{node['name']!r}）")
        if check_columns:
            for col in node_columns(node):
                if col not in known_cols:
                    raise DerivedError(
                        f"{label} 引用了未知列 {col!r}；可用列：{sorted(known_cols)[:12]}"
                        + ("…" if len(known_cols) > 12 else ""))
        if node["kind"] == "formula":
            for ref in expr_refs(node.get("expr") or {}):
                if ref not in names:
                    raise DerivedError(f"{label} 引用了未知派生指标 {ref!r}；已声明：{sorted(names) or '（无）'}")
        elif node["kind"] == "share":
            if node.get("scope") == "parent" and check_columns and known_cols \
                    and node.get("parentField") not in known_cols:
                raise DerivedError(
                    f"{label} 的 parentField {node.get('parentField')!r} 不存在于列中；"
                    f"可用列：{sorted(known_cols)[:12]}" + ("…" if len(known_cols) > 12 else ""))
        elif node["kind"] == "filtered":
            # 目标列 + 条件列的存在性已由上方 node_columns 检查覆盖；无 ref、
            # 无时间语义（同环比 = window 节点引用它，不是它自己的声明）。
            pass
        else:
            # ``input.ref`` may name a derived measure OR a raw column (which the
            # evaluator aggregates by its default aggregation) — reject only a
            # ref nobody ever declared.
            ref = (node.get("input") or {}).get("ref")
            if ref and check_columns and ref not in names and ref not in known_cols:
                raise DerivedError(
                    f"{label} 的 input.ref {ref!r} 既不是已声明的派生指标，也不是已知列；"
                    f"已声明：{sorted(names) or '（无）'}；可用列：{sorted(known_cols)[:12]}")
            if node["fn"] not in RANK_WINDOW_FNS:   # 排名族无时间语义，不校验 timeField
                tfield = node["timeField"]
                if check_columns and known_times and tfield not in known_times:
                    raise DerivedError(
                        f"{label} 的 timeField {tfield!r} 不是时间列；数据集里的时间列：{sorted(known_times) or '（无）'}")
                if check_columns and known_cols and tfield not in known_cols:
                    raise DerivedError(f"{label} 的 timeField {tfield!r} 不存在于列中")
    topo_order(defs)  # cycle guard


# ---------------------- migration (D4: 自动迁移 + 保留运行) --------------------------

def migrate_chart_derived(specs: dict, time_field: str | None = None,
                          base_agg: dict[str, str] | None = None,
                          measure_names: Iterable[str] | None = None) -> tuple[list[dict], dict[str, str]]:
    """Upgrade chart-scoped ``data.derived[token]`` specs to dataset-level nodes.

    Returns ``(nodes, unmigrated)`` where ``unmigrated`` maps ``token → reason``
    for specs that CANNOT be migrated losslessly. Those keep running on the old
    chart-scoped path (never dropped, never silently re-interpreted):

    * ``share`` with ``scope=parent|filter`` — its denominator depends on the
      chart's own dimension and on raw rows the server does not have, so it is a
      different (chart-scoped) concept; the user is asked to rebuild it.
    * ``yoy`` on ``week`` grain — 52 weeks ≠ a year; rebuilding is required
      rather than emitting an off-by-one comparison.
    * anything without a resolvable ``timeField`` (window kinds).

    ``mom``/``yoy``/``running``/``share=all`` all migrate exactly. For the
    share numerator/denominator the base is aggregated explicitly: a ``ref`` to
    another DECLARED measure stays a ref, otherwise it becomes
    ``{agg, col}`` using ``base_agg`` (the chart's own aggregation, default
    ``sum`` — the old runtime summed the base's aggregated values, so ``sum`` is
    the faithful mapping for the common case and the user can adjust it in the
    data-source editor).
    """
    nodes: list[dict] = []
    unmigrated: dict[str, str] = {}
    declared = {n for n in (measure_names or ()) if isinstance(n, str)}

    def base_expr(name: str) -> dict:
        if name in declared:
            return {"ref": name}
        agg = (base_agg or {}).get(name) or "sum"
        return {"agg": agg, "col": name}
    for token, spec in (specs or {}).items():
        if not isinstance(spec, dict):
            continue
        kind = spec.get("kind")
        base = spec.get("base")
        label = spec.get("label") or token
        if not isinstance(base, str) or not base.strip():
            unmigrated[token] = "缺少 base（被求指标），无法迁移"
            continue
        if kind in ("mom", "yoy", "running"):
            if not time_field:
                unmigrated[token] = "无法确定时间列（timeField），窗口指标无法迁移"
                continue
            grain = spec.get("granularity") or "month"
            if grain not in GRAINS:
                unmigrated[token] = f"未知粒度 {grain!r}"
                continue
            if kind == "running":
                nodes.append({"name": label, "kind": "window", "input": {"ref": base},
                              "fn": "cum", "grain": grain, "timeField": time_field,
                              "output": "delta", "measureType": {"type": "number"}})
                continue
            if kind == "mom":
                offset = 1
            else:  # yoy
                if grain not in YOY_OFFSET:
                    unmigrated[token] = ("周粒度的同比无法用固定期数表达（52 周 ≠ 一年），"
                                         "请重建为显式口径")
                    continue
                offset = YOY_OFFSET[grain]
            nodes.append({"name": label, "kind": "window", "input": {"ref": base},
                          "fn": "prev", "grain": grain, "offset": offset,
                          "timeField": time_field, "output": "rate",
                          "measureType": {"type": "percent"}})
            continue
        if kind == "share":
            scope = spec.get("scope")
            if isinstance(scope, dict) and scope.get("kind") == "filter":
                unmigrated[token] = ("固定子集（filter）的占比依赖图表筛选选择集，"
                                     "请重建为「占总计」占比，或用请求筛选表达")
                continue
            if isinstance(scope, dict) and scope.get("kind") == "parent":
                pcol = scope.get("parentCol")
                if not isinstance(pcol, str) or not pcol.strip():
                    unmigrated[token] = "按父级占比缺少 parentCol，无法迁移"
                    continue
                nodes.append({"name": label, "kind": "share", "input": base_expr(base),
                              "scope": "parent", "parentField": pcol.strip(),
                              "measureType": {"type": "percent"}})
                continue
            # BI-H2：share=all 迁为**真正的 share 节点**（旧实现迁成 div(x,x) 恒 100%，
            # 那是错的 —— 占比的分母是全部分组之和，表达式公式表达不出来）。
            nodes.append({"name": label, "kind": "share", "input": base_expr(base),
                          "scope": "all", "measureType": {"type": "percent"}})
            continue
        unmigrated[token] = f"未知的派生类型 {kind}"   # 与 JS 侧逐字一致（parity fixture）
    return nodes, unmigrated


# ---------------------- presentation ----------------------------------------------

def _agg_text(n: dict) -> str:
    fn = {"sum": "SUM", "average": "AVG", "count": "COUNT", "min": "MIN", "max": "MAX"}
    if n["agg"] == "countd":
        return f"COUNT(DISTINCT {n['col']})"
    return f"{fn[n['agg']]}({n['col']})"


def expr_text(n: Any) -> str:
    """Readable form of an expression (error messages / P3 UI summary)."""
    if not isinstance(n, dict):
        return "?"
    if "const" in n:
        return f"{n['const']}"
    if "ref" in n:
        return str(n["ref"])
    if "col" in n:
        return _agg_text(n)
    op = {"add": "+", "sub": "−", "mul": "×", "div": "/"}.get(n.get("op"), "?")
    return f"{expr_text(n.get('left'))} {op} {expr_text(n.get('right'))}"


def _filter_text(f: dict) -> str:
    """One filter condition in the reader's words (node_summary / UI tooltip)."""
    v = f.get("value")
    op = str(f.get("op") or "eq")
    if op == "between" and isinstance(v, (list, tuple)):
        vs = f"{v[0]}~{v[1]}"
    elif op == "in" and isinstance(v, (list, tuple)):
        vs = "/".join(str(x) for x in v)
    else:
        vs = str(v)
    cn = {"eq": "=", "ne": "≠", "gt": ">", "ge": "≥", "lt": "<", "le": "≤",
          "between": "介于", "in": "属于"}.get(op, op)
    return f"{f.get('column')} {cn} {vs}"


def node_summary(node: dict) -> str:
    """One-line human summary, in the reader's words (not the machine's).

    Machine phrasing ("X 的 1 期前，按 month") reads like a translation error; a
    BI user thinks in 环比/同比/累计. Examples::

        净流入 · 环比增长率（按月）        window prev + rate
        净流入 · 同比增长（按季）          window prevYear + delta
        净流入 · 累计（按月）              window cum
        毛利率 = SUM(毛利额) / SUM(收入)   formula
        COUNTD(门店)［销量 = 0 且 库存 > 1］  filtered
    """
    if node.get("kind") == "formula":
        return f"{node['name']} = {expr_text(node.get('expr'))}"
    if node.get("kind") == "filtered":
        target = _agg_text(node) if node.get("col") else "COUNT(*)"
        conds = " 且 ".join(_filter_text(f) for f in node.get("filters") or [])
        return f"{target}［{conds}］"
    if node.get("kind") == "share":
        inp = node.get("input") or {}
        target = inp.get("ref") or _agg_text(inp)
        if node.get("scope") == "parent":
            return f"{target} · 占「{node.get('parentField')}」内占比"
        return f"{target} · 占总计百分比"
    inp = node.get("input") or {}
    target = inp.get("ref") or _agg_text(inp)
    grain = _GRAIN_CN.get(str(node.get("grain")), str(node.get("grain") or ""))
    offset = int(node.get("offset") or 1)
    if node.get("fn") == "ytd":
        return f"{target} · 年累计（按{grain}）"
    if node.get("fn") == "rank":
        return f"{target} · 组内排名（降序）"
    if node.get("fn") == "dense_rank":
        return f"{target} · 组内密集排名（降序）"
    if node.get("fn") == "percent_rank":
        return f"{target} · 组内百分比排名"
    if node.get("fn") == "cum":
        return f"{target} · 累计（按{grain}）"
    if node.get("fn") == "prevYear":
        base = "同比"
    elif offset == 1:
        base = "环比"
    else:
        base = f"近 {offset} 期"
    tail = {"rate": "增长率", "delta": "增长", "value": "值"}.get(str(node.get("output")), "")
    return f"{target} · {base}{tail}（按{grain}）"


_GRAIN_CN = {"day": "日", "week": "周", "month": "月", "quarter": "季", "year": "年",
             "raw": "原始"}
