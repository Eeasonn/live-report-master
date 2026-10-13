#!/usr/bin/env python3
"""Measure semantic types (thin wrapper over ``semantics.type_registry``).

Kept as a public facade so existing callers (``validate_request``,
``spec_to_report``) and tests keep working unchanged. The actual T0/T1/T2 rules
now live in ``scripts/semantics/`` — the single source of truth (Flint-inspired).
"""
from __future__ import annotations

import re
from typing import Any

from semantics.type_registry import (  # noqa: F401
    CANONICAL,
    CURRENCY_DEFAULT_SCALES,
    CURRENCY_SYMBOLS,
    CURRENCY_SYMBOL_DEFAULT,
    FAMILY_MAP,
    apply_currency_defaults,
    currency_symbol,
    is_non_additive,
    resolve_t2,
    type_rule,
)
from semantics import flint_core as _fc

# Flint T2 语义注解受控词汇（批次 2，design-semantics.md §2.7 LLM 注解策略）：
# measureTypes[col].semantic 允许的标签 = flint_core 注册表全键 + Rating 别名。
SEMANTIC_VOCAB: set[str] = set(_fc.FLINT_TYPES) | {"Rating"}


def normalize_measure_type(raw: Any) -> dict[str, Any] | str | None:
    """Normalise a model-supplied measure type to a canonical T2 type.

    Accepts a canonical string, a {type, divisor, unit} object, a T1 family
    label, a T0 family label, or a Chinese token. Returns a canonical string,
    a canonical object, or ``None`` (unknown). Delegates to the registry.
    """
    return resolve_t2(raw)


# ---- 语义标签解析 + aggRole 缺省聚合（2026-10-13 批，单一入口） ----------------
# 接线 Flint resolveAggregationDefault（refs/flint/src/core/field-semantics.ts:480-492，
# intensive → average）：此前 aggregationDefault 两端定义了零消费者，缺省聚合在五处
# 硬编码 sum —— 价格列被当销售额加总/堆叠就是从这里漏进去的。


def semantic_label_of(entry: Any, name: str = "") -> str | None:
    """Resolve a measure column's Flint semantic label (T2) for decision-making.

    Precedence: 声明 ``measureTypes[col].semantic``（批次 2 注解键）> 声明
    ``type``（canonical_alias 进 Flint 表）> 名称关键词（价族→Price、金额族→
    Revenue、率族→Percentage、量词→Count）。返回 Flint 标签或 ``None``。
    编译（zero_decision / 缺省聚合）、KPI、profile 语义候选共用这一个入口。
    """
    if isinstance(entry, dict):
        sem = entry.get("semantic")
        if isinstance(sem, str) and sem.strip():
            return _fc.canonical_alias(sem.strip())
        t = entry.get("type")
        if isinstance(t, str) and t:
            label = _fc.canonical_alias(t)
            return label if _fc.get_type(label) else None
    elif isinstance(entry, str) and entry.strip():
        label = _fc.canonical_alias(entry.strip())
        if _fc.get_type(label):
            return label
    n = str(name or "")
    if not n:
        return None
    if any(k in n for k in ("率", "占比", "百分比", "比例")):
        return "Percentage"
    # 价族（与 type_registry.FAMILY_MAP 价族行同词汇）：单价/售价是每商品属性，
    # 绝不折叠进可加 Revenue —— 竞品街价报告跨品类加总的病灶根因。
    if any(k in n for k in ("售价", "单价", "价格", "标价", "原价", "现价", "均价")) \
            or re.search(r"price", n, re.I):
        return "Price"
    if any(k in n for k in ("金额", "销售额", "收入", "营收", "营业额", "利润", "成本", "费用", "额")):
        return "Revenue"
    if any(k in n for k in ("量", "数", "个", "件", "笔")):
        return "Count"
    return None


def default_aggregation(entry: Any, name: str = "") -> str | None:
    """aggRole 缺省聚合（Flint resolveAggregationDefault）。

    intensive（Percentage/Price/Score/Temperature/Sentiment/Correlation/
    PercentageChange）→ ``average``；additive/signed-additive → ``sum``；
    语义未知 → ``None``（调用方保留既有 ``sum`` 回退，零漂移）。
    """
    label = semantic_label_of(entry, name)
    if not label:
        return None
    t = _fc.get_type(_fc.canonical_alias(label))
    if not t:
        return None
    if t["aggRole"] == "intensive":
        return "average"
    if t["aggRole"] in _fc.ADDITIVE:
        return "sum"
    return None


def lookahead_col_semantics(col: str, values: list[str] | None = None) -> dict[str, Any]:
    """Best-effort field semantics from a column name + optional sampled values.

    Returns a small dict the compiler/runtime use as the *default* semantic
    before an explicit ``measureTypes`` entry overrides it. Name hints win;
    numeric samples only confirm. Deterministic and cheap.
    """
    kind = "dimension"
    if values:
        numeric = 0
        non_empty = 0
        for v in values:
            if v is None or v == "":
                continue
            non_empty += 1
            try:
                float(v)
                numeric += 1
            except (TypeError, ValueError):
                pass
        if non_empty and numeric / non_empty > 0.9:
            kind = "measure"
    if kind == "measure":
        canon = normalize_measure_type(str(col or ""))
        if isinstance(canon, dict):
            canon = canon["type"]
        canon = canon or "number"
        return {"kind": "measure", "measureType": canon, "nonAdditive": is_non_additive(canon)}
    return {"kind": "dimension", "measureType": None, "nonAdditive": False}
