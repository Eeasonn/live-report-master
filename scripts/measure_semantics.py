#!/usr/bin/env python3
"""Measure semantic types (thin wrapper over ``semantics.type_registry``).

Kept as a public facade so existing callers (``validate_request``,
``spec_to_report``) and tests keep working unchanged. The actual T0/T1/T2 rules
now live in ``scripts/semantics/`` — the single source of truth (Flint-inspired).
"""
from __future__ import annotations

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


def normalize_measure_type(raw: Any) -> dict[str, Any] | str | None:
    """Normalise a model-supplied measure type to a canonical T2 type.

    Accepts a canonical string, a {type, divisor, unit} object, a T1 family
    label, a T0 family label, or a Chinese token. Returns a canonical string,
    a canonical object, or ``None`` (unknown). Delegates to the registry.
    """
    return resolve_t2(raw)


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
