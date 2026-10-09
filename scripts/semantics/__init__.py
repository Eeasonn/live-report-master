"""Semantic rule engine for live-report-master (Flint-inspired single source
of truth).

The skill's standard rules (semantic types, channel semantics, shape rules)
live here as one authoritative module set, so the compiler/authoring path
never re-implements them in scattered places. Grounded in the vendored Flint
source under ``refs/flint/src/core`` (semantic-types / type-registry /
channel-semantics) and the skill's own measures.

Layers:
  - ``type_registry``  : T0/T1/T2 semantic types + per-type rules (format family,
                         aggregation role, zero-baseline, encoding kind, additive).
  - ``channel_semantics``: how a field maps onto a chart channel (dimension /
                         measure / breakdown) and what that forbids or degrades.
  - ``shape_rules``    : per-chartType dimension/measure capacity, incl. the
                         composite (mixed bar/line) budget.
"""
