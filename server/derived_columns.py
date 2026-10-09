"""Server-side row-level derived columns (数据集「计算列」).

A server report serves a SCHEMA-ONLY manifest, so the browser never sees the
rows — but the server holds every source table in SQLite (``h_register`` /
``h_dataset_add`` both go through ``store.Engine.load_table``), so a row-level
derived column is materialised HERE. Without it, a request declaring
``datasets[].derived.columns`` produced a manifest whose column list contained
e.g. ``净流入`` while the SQLite table did not → every aggregate on it died with
``no such column``. Server mode used to reject such requests outright
(``report_build.py`` / ``serve_report.py``), which made "派生统一到数据源管理"
impossible for server reports.

Design constraint: ZERO parity risk
-----------------------------------
This module reuses ``scripts/derived_eval.materialize`` — the very function
``spec_to_report._materialize_derived`` calls. There is exactly ONE
implementation, so the SQLite column is byte-identical to the compiled/static
one. Do NOT re-implement the arithmetic here (or as generated SQL).

Where the declaration lives — and why it is a TABLE-level union
----------------------------------------------------------------
The registry entry records it (``datasets[i].derived = {kind:'columns', defs}``)
and the snapshot ``source`` file stays RAW (it is a copy of the user's upload),
so the declaration must be replayed on every load/reload — mirroring
``server/relations.py``'s replay for wide tables.

BUT report copies (保存为副本 / 版本) inherit their parent's ``datasets[]``:
several reports can therefore share ONE dataset id — and the engine table is
keyed by that id, while ``load_table`` is DROP+CREATE. Loading per entry is
last-writer-wins, so a copy WITHOUT a declaration silently erased the base
report's derived column on the next boot (reproduced on the real 335k-row PSI
report: 6 copies reloaded the same ``PSI__primary`` table). Hence the rule:

  * the file-backed pass loads tables only;
  * a SECOND pass applies the UNION of the declarations that point at each
    table id — exactly the two-pass doctrine already used for relations.

A name declared twice with different formulas resolves to the first registered
report (registry order); a column is never dropped from a shared table.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Iterable

# scripts/ holds the canonical derived-column math; make it importable exactly
# like server/relations.py does for the merge implementation.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

# Registry/payload marker. ``kind`` distinguishes this from the wide-table
# 'relation' marker so ``app._is_derived_ds`` / ``_prune_derived_datasets``
# (which are about relation datasets that have NO source file) never touch a
# columns-derived dataset — those DO have a source and reload normally.
DERIVED_KIND = "columns"


def columns_decl(value: Any) -> dict | None:
    """Normalise a ``derived`` payload into ``{kind:'columns', defs:[...]}``.

    Returns ``None`` when the payload is absent/empty or is another kind
    ('relation'), so callers can use it as a plain guard. Tolerates the
    manifest slot shape ``{columns:[...]}`` so a manifest dataset's ``derived``
    object can be passed straight through.

    **Only the row-level ``columns`` slot is materialised here** — the
    aggregate-level ``derived.measures`` are NOT the server's business: they are
    evaluated after aggregation by the SAME evaluator on the compile side and in
    the client (``scripts/derived_eval.evaluate`` / ``chart-adapter.derivedEvaluate``),
    so persisting them as a registry *table* declaration would be dead data that
    could drift. They still survive a restart / report copy because the served
    manifest (registry entry ``manifest.datasets[].derived``) carries them
    verbatim — see ``tests/test_server_derived_columns.py::test_manifest_keeps_measures``.
    """
    if not isinstance(value, dict):
        return None
    if value.get("kind") not in (None, DERIVED_KIND):
        return None
    defs = value.get("defs")
    if defs is None:
        defs = value.get("columns")
    defs = [d for d in (defs or []) if isinstance(d, dict) and d.get("name")]
    if not defs:
        return None
    return {"kind": DERIVED_KIND, "defs": [dict(d) for d in defs]}


def is_derived_columns(d: Any) -> bool:
    return isinstance(d, dict) and (d.get("derived") or {}).get("kind") == DERIVED_KIND


def apply_dataset_derived(table: dict, derived: Any) -> dict:
    """Materialise declared 计算列 onto a freshly built ``{columns, rows}`` table.

    Idempotent: a def whose column already exists is skipped, so a payload that
    already carries the column (or a double apply) cannot double-append. Broken
    declarations still raise — they must surface, not vanish.
    """
    decl = columns_decl(derived)
    if not decl or not isinstance(table, dict):
        return table
    present = set(table.get("columns") or [])
    pending = [d for d in decl["defs"] if d.get("name") not in present]
    if not pending:
        return table
    from derived_eval import materialize  # noqa: PLC0415  (scripts/ on sys.path above)

    return materialize(table, pending)


def table_defs(entries: Iterable[dict], ds_id: str, extra: Any = None) -> list[dict]:
    """Union of 计算列 defs declared for ONE table id across every registry entry.

    ``entries`` may be registry entries (``{datasets:[...]}``) or dataset dicts
    directly. First declaration wins per column name — deterministic because
    registry order is insertion order (base report registered before its
    copies).
    """
    out: list[dict] = []
    seen: set[str] = set()

    def feed(value: Any) -> None:
        decl = columns_decl(value)
        if not decl:
            return
        for item in decl["defs"]:
            name = item["name"]
            if name in seen:
                continue
            seen.add(name)
            out.append(item)

    feed(extra)
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        datasets = entry.get("datasets") if "datasets" in entry else [entry]
        for d in datasets or []:
            if isinstance(d, dict) and d.get("id") == ds_id:
                feed(d.get("derived"))
    return out


def ensure_table_defs(engine, ds_id: str, defs: list[dict],
                      index_columns: list[str] | None = None) -> int:
    # index_columns 透传给 load_table：补列走 drop+load 重建，不把原表索引带回
    # 的话，大表的筛选/分组索引会在一次 replay 后静默消失。
    """Materialise any of ``defs`` still missing from a loaded engine table.

    Returns the number of columns added (0 = nothing to do). Reads the table
    back through the engine so a table shared by several reports converges to
    the union of their declarations instead of the last writer's shape.
    """
    if not defs:
        return 0
    info = engine.table_info(ds_id)
    if info is None:
        return 0
    have = set(info.columns)
    missing = [d for d in defs if d.get("name") not in have]
    if not missing:
        return 0
    from derived_eval import materialize  # noqa: PLC0415
    from .store import _qcol  # noqa: PLC0415

    cols = list(info.columns)
    raw = engine.execute(f'SELECT {", ".join(_qcol(c) for c in cols)} FROM {_qcol(ds_id)}')
    table = {"columns": cols, "rows": [[r[c] for c in cols] for r in raw]}
    out = materialize(table, missing)
    engine.drop_table(ds_id)
    engine.load_table(ds_id, out["columns"], out["rows"], index_columns=list(index_columns or []))
    return len(missing)


def ensure_dim_groups(engine, ds_id: str, defs: list[dict],
                      index_columns: Optional[list[str]] = None) -> int:
    """Materialise any 维度值分组 defs still missing from a loaded engine table."""
    if not defs:
        return 0
    info = engine.table_info(ds_id)
    if info is None:
        return 0
    have = set(info.columns)
    missing = [d for d in defs if d.get("name") not in have]
    if not missing:
        return 0
    from derived_eval import materialize_dim_groups  # noqa: PLC0415
    from .store import _qcol  # noqa: PLC0415

    cols = list(info.columns)
    raw = engine.execute(f'SELECT {", ".join(_qcol(c) for c in cols)} FROM {_qcol(ds_id)}')
    table = {"columns": cols, "rows": [[r[c] for c in cols] for r in raw]}
    out = materialize_dim_groups(table, missing)
    engine.drop_table(ds_id)
    engine.load_table(ds_id, out["columns"], out["rows"], index_columns=list(index_columns or []))
    return len(missing)


def replay_all(engine, registry, verbose: bool = False) -> list[str]:
    """Apply the UNION of 计算列 declarations to every loaded table.

    Call AFTER the file-backed load pass (the sources must exist first) and
    before relations are replayed — same ordering rule as
    ``server/relations.replay_entry``.

    Deliberately table-centric rather than entry-centric: report copies share
    dataset ids, so replaying one entry would leave a table reloaded by another
    report without its column. Returns the dataset ids it touched.
    """
    entries = registry.raw_entries() if hasattr(registry, "raw_entries") else list(registry or [])
    ds_ids = sorted({d["id"] for e in entries if isinstance(e, dict)
                     for d in (e.get("datasets") or []) if isinstance(d, dict) and d.get("id")})
    touched: list[str] = []
    for ds_id in ds_ids:
        defs = table_defs(entries, ds_id)
        gdefs = dim_group_table_defs(entries, ds_id)
        if not defs and not gdefs:
            continue
        # 该表在注册表里声明的索引列（h_register/h_dataset_replace 写进 meta）：
        # 重建时带回，索引不因 replay 消失。
        idx_cols: list[str] = []
        for e2 in entries:
            if not isinstance(e2, dict):
                continue
            for d2 in (e2.get("datasets") or []):
                if isinstance(d2, dict) and d2.get("id") == ds_id:
                    for c in (d2.get("indexColumns") or []):
                        if c not in idx_cols:
                            idx_cols.append(c)
        try:
            if ensure_table_defs(engine, ds_id, defs, index_columns=idx_cols):
                touched.append(ds_id)
        except Exception as e:  # noqa: BLE001  (best-effort boot/reload path)
            if verbose:
                print(f"[ai-report-server] WARN 计算列 replay {ds_id!r} failed: {e}",
                      file=sys.stderr)
        try:
            if ensure_dim_groups(engine, ds_id, gdefs, index_columns=idx_cols):
                touched.append(ds_id)
        except Exception as e:  # noqa: BLE001
            if verbose:
                print(f"[ai-report-server] WARN 维度分组 replay {ds_id!r} failed: {e}",
                      file=sys.stderr)
    return touched


def dim_groups_defs(value: Any) -> list[dict] | None:
    """Normalise a ``ds.dimGroups`` payload into a plain list (None = absent)."""
    if not isinstance(value, list) or not value:
        return None
    defs = [d for d in value if isinstance(d, dict) and d.get("name") and d.get("source")]
    return defs or None


def dim_group_table_defs(entries: Iterable[dict], ds_id: str, extra: Any = None) -> list[dict]:
    """Union of 维度值分组 defs for ONE table id across registry entries（先到先得）。

    与 table_defs 同一条并集语义：副本共享表，只补缺失列、永不删列。
    """
    out: list[dict] = []
    seen: set[str] = set()

    def feed(value: Any) -> None:
        defs = dim_groups_defs(value)
        if not defs:
            return
        for item in defs:
            name = item["name"]
            if name in seen:
                continue
            seen.add(name)
            out.append(item)

    feed(extra)
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        datasets = entry.get("datasets") if "datasets" in entry else [entry]
        for d in datasets or []:
            if isinstance(d, dict) and d.get("id") == ds_id:
                feed(d.get("dimGroups"))
    return out


def apply_dim_groups(table: dict, defs: list[dict]) -> dict:
    """Materialise 维度值分组 defs onto a freshly built table (idempotent)."""
    if not defs or not isinstance(table, dict):
        return table
    present = set(table.get("columns") or [])
    pending = [d for d in defs if d.get("name") not in present]
    if not pending:
        return table
    from derived_eval import materialize_dim_groups  # noqa: PLC0415

    return materialize_dim_groups(table, pending)
