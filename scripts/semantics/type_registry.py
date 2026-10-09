#!/usr/bin/env python3
"""Semantic type registry (T0 Family / T1 Category / T2 Specific).

Single source of truth for the semantic types the skill understands, modelled on
Flint's ``semantic-types.ts`` / ``type-registry.ts`` (vendored in
``refs/flint/src/core``). The skill deliberately keeps a *converged* type set
rather than Flint's ~70 domain types: enough to drive encoding, formatting,
aggregation role, zero-baseline and non-additivity for a BI report, without the
cost of a huge registry.

Three tiers so the author/agent can annotate with whatever precision it has:
  - T0 Family     : ``measure`` / ``dimension`` / ``time``   (coarsest)
  - T1 Category   : ``money`` / ``ratio`` / ``count`` / ``duration`` / ``number``
  - T2 Specific   : ``currency`` / ``percent`` / ``count`` / ``duration`` /
                    ``number`` / ``unitless``  (the runtime's canonical formats)

A coarse label never fails validation — it degrades to a canonical T2 type
(Flint's "degrade gracefully, never fail on a coarse label").
"""
from __future__ import annotations

from typing import Any

# Canonical T2 format families the runtime knows how to render.
CANONICAL = {"currency", "percent", "count", "duration", "number", "unitless"}

# Per-T2-type rules: {format kind, encoding visual kind, aggregation role,
# zero-baseline (does a value axis start at 0?), non-additive (cannot sum/stack)}.
TYPE_RULES: dict[str, dict[str, Any]] = {
    "currency": {"encoding": "quantitative", "additive": True, "zero": True,
                 "format": "currency", "divisor": 10000, "unit": "万"},
    "percent":  {"encoding": "quantitative", "additive": True, "zero": True,
                 "format": "percent"},
    "count":    {"encoding": "quantitative", "additive": True, "zero": True,
                 "format": "count"},
    "duration": {"encoding": "quantitative", "additive": True, "zero": True,
                 "format": "duration", "unit": "天"},
    "number":   {"encoding": "quantitative", "additive": True, "zero": True,
                 "format": "number"},
    "unitless": {"encoding": "quantitative", "additive": False, "zero": True,
                 "format": "number"},
}

# ---- Currency table (拍板 6/7, docs/multidim-table-and-currency-design-2026-09-28.md §二) ----
# Built-in currency codes → display symbols. Flint keeps 14 codes in CURRENCY_MAP
# (refs/flint/src/core/field-semantics.ts:234-238); this table adds the 8
# Asia-Pacific codes the design doc fixes. **JS mirror** =
# ``assets/runtime/chart-adapter.js`` ``CURRENCY_SYMBOLS`` — both ends MUST stay
# byte-equal in content (keys + symbols); parity is pinned by
# ``tests/test_currency_types.py`` (same discipline as date_parse).
CURRENCY_SYMBOLS: dict[str, str] = {
    "USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "CNY": "¥", "KRW": "₩",
    "INR": "₹", "BRL": "R$", "CAD": "CA$", "AUD": "A$", "CHF": "CHF",
    "SEK": "kr", "NOK": "kr", "DKK": "kr", "HKD": "HK$", "TWD": "NT$",
    "SGD": "S$", "THB": "฿", "MYR": "RM", "PHP": "₱", "IDR": "Rp", "VND": "₫",
    "RUB": "₽",
}
# Display symbol for ``type:'currency'`` when neither ``currency`` nor ``symbol``
# is declared: CNY default (现状 ¥, zero drift).
CURRENCY_SYMBOL_DEFAULT = "¥"
# 拍板 7: divisor default scales per currency code — CNY/JPY keep the 万元口径
# (TYPE_RULES['currency'] 现状), every other code stays 原值. Applied ONLY to
# declarations that carry an explicit ``currency`` key AND author neither
# ``divisor`` nor ``unit``; entries without a ``currency`` key are untouched
# (they predate this axis — zero drift for existing CNY reports).
CURRENCY_DEFAULT_SCALES: dict[str, dict[str, Any]] = {
    "CNY": {"divisor": 10000, "unit": "万"},
    "JPY": {"divisor": 10000, "unit": "万"},
}


def currency_symbol(semantic: Any) -> str:
    """Display symbol for a measureTypes entry.

    Accepts the raw ``measureTypes[col]`` value: object form reads ``symbol``
    (wins over the table) then ``currency``; string form / missing keys /
    non-object → CNY default ``¥``. Unknown codes degrade to the default here
    (validate_request is the gate that rejects them with an actionable error).
    """
    if not isinstance(semantic, dict):
        return CURRENCY_SYMBOL_DEFAULT
    sym = semantic.get("symbol")
    if isinstance(sym, str) and sym.strip():
        return sym.strip()
    code = semantic.get("currency")
    if isinstance(code, str) and code.strip():
        return CURRENCY_SYMBOLS.get(code.strip().upper(), CURRENCY_SYMBOL_DEFAULT)
    return CURRENCY_SYMBOL_DEFAULT


def apply_currency_defaults(semantic: dict[str, Any]) -> dict[str, Any]:
    """拍板 7 (pure): fill the per-code default scale for an explicit currency
    declaration that authored neither ``divisor`` nor ``unit``. Returns the
    input unchanged when the entry has no ``currency`` key, has an explicit
    scale, or is not a currency type."""
    if not isinstance(semantic, dict) or semantic.get("type") != "currency":
        return semantic
    if "currency" not in semantic or semantic.get("divisor") is not None or semantic.get("unit") is not None:
        return semantic
    scale = CURRENCY_DEFAULT_SCALES.get(str(semantic.get("currency", "")).strip().upper())
    out = dict(semantic)
    if scale:
        out["divisor"] = scale["divisor"]
        out["unit"] = scale["unit"]
    else:
        out["divisor"] = 1
    return out


# Tier L1 family -> canonical T2 type. Order matters (first match wins), kept as
# a small closed vocabulary so it never surprises with an over-eager match.
#
# P1 redesign (docs/semantic-config-and-derived-redesign.md §1/§2): the
# keyword heuristic for "percent" was removed — `%` is a *display format*,
# not a type (see ``measureTypes[col].format='percent'``). Authors who want a
# non-additive percent column now declare ``type:'percent'`` explicitly; a
# column whose name happens to contain 率/占比/etc. is no longer auto-flagged.
FAMILY_MAP: list[tuple[tuple[str, ...], dict[str, Any] | str]] = [
    # money / price / revenue
    (("货币", "金钱", "收入", "盈利", "营收", "revenue", "money", "assets"),
     {"type": "currency", "divisor": 10000, "unit": "万"}),
    (("金额", "价格", "成本", "利润", "费用", "消费", "花费", "支出", "售价",
      "单价", "销售额", "营业额", "price", "cost", "sales", "profit", "amount"),
     {"type": "currency", "divisor": 10000, "unit": "万"}),
    (("currency",), "currency"),
    # duration
    (("时长", "耗时", "用时", "duration", "elapsed"), "duration"),
    # count of records / people
    (("人数", "用户数", "数量", "条数", "记录数", "count", "records", "users",
      "visits", "次数"), "count"),
    # plain numeric quantity
    (("数量", "数值", "指标", "figures", "number", "measure", "quantity"), "number"),
]

_FAMILY_WORDS: dict[str, Any] = {}
for _keys, _val in FAMILY_MAP:
    _canon = _val["type"] if isinstance(_val, dict) else _val
    for _k in _keys:
        _FAMILY_WORDS[_k] = _canon


# ---- T0 enums ----------------------------------------------------------------
T0_DIMENSION = {"dimension", "category", "categorical", "ordinal", "time"}
T0_MEASURE = {"measure", "metric", "numeric", "quantitative", "number"}


def resolve_t2(raw: Any) -> dict[str, Any] | str | None:
    """Normalise a model-supplied type to a canonical T2 type (string/object).

    Accepts a canonical string, a ``{type, divisor, unit, decimals}`` object, a
    T1 family label, a T0 family label, or a Chinese token. Returns ``None``
    only for a genuinely unknown label (the caller may then fall back to the
    column-name guess).
    """
    if isinstance(raw, dict):
        type_ = raw.get("type")
        if type_ not in CANONICAL:
            type_ = _FAMILY_WORDS.get(str(type_).lower(), type_)
            if type_ not in CANONICAL:
                return None
        out = dict(raw)
        out["type"] = type_
        return out
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    lw = s.lower()
    if lw in CANONICAL:
        return s
    if lw in _FAMILY_WORDS:
        # money family resolves to the 万元 currency object for a nicer default.
        for _keys, _val in FAMILY_MAP:
            if lw in _keys:
                return dict(_val) if isinstance(_val, dict) else _val
        return _FAMILY_WORDS[lw]
    # T0 enums.
    if lw in T0_MEASURE:
        return "number"
    if lw in T0_DIMENSION:
        return None  # dimension family has no numeric format; treated as category
    return None


def is_non_additive(type_: str | None) -> bool:
    """``unitless`` (and the rarely-used explicit ``percent`` type for raw
    0-100 columns) cannot be summed or stacked meaningfully. The default
    route for percent display is now ``measureTypes[col].format='percent'``
    on a ``type:'number'`` column, which IS additive — see docs §1."""
    return type_ in {"percent", "unitless"}


def type_rule(type_: str | None) -> dict[str, Any]:
    return TYPE_RULES.get(type_ or "", {})


def is_additive(type_: str | None) -> bool:
    return bool(type_rule(type_).get("additive", False))


def requires_zero_baseline(type_: str | None) -> bool:
    return bool(type_rule(type_).get("zero", True))


def default_format(type_: str) -> dict[str, Any]:
    """A concrete measure-format object for a canonical type (T2)."""
    rule = type_rule(type_)
    return {"type": type_, "divisor": float(rule.get("divisor", 1)),
            "unit": rule.get("unit", ""), "decimals": rule.get("decimals")}
