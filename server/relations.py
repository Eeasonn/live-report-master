"""Runtime relation materialization (server-side wide table).

Why this exists
---------------
A server report serves a SCHEMA-ONLY manifest (no rows), so the browser cannot
merge tables itself. But the server already holds every source table in SQLite
(``h_register`` / ``h_dataset_add`` both go through ``store.Engine.load_table``),
so a wide table can simply be built here — no data ever has to reach the client.

Design constraint: ZERO parity risk
-----------------------------------
This module deliberately reuses ``scripts/merge_tables.materialize`` — the very
function the build-time pre-pass (``report_build._resolve_relations_spec``) calls.
There is exactly ONE merge implementation, so the runtime wide table is
byte-identical to what the next ``report_build`` would produce (column order
``[left key, left non-keys, right non-keys, right key survivor]``, ``{ds}__``
collision suffixes, first-row-wins on duplicate keys, per-key mode ``a/b/both``,
and ``relations[].renames`` aliases applied last).

Do NOT hand-write a second merge here (e.g. as generated SQL): SQL ``LEFT JOIN``
fans out on duplicate right-side keys while the canonical merge keeps the FIRST
match, and any naming drift silently reshapes the report on the next rebuild.

Namespace-free merge identity
-----------------------------
Server tables are namespaced per report (``<reportId>__<raw>``, Issue #8), while
a build-time merge sees the request's RAW ids. Feeding the namespaced ids into
``materialize`` would make its collision suffixes (``dssrv__inv__月份``) and thus
the merged COLUMN NAMES differ from the build-time result (``inv__月份``) — and
re-merging an already-built report would split every chart reference that used
the build-time name. So the merge runs on the report-namespace-free identity of
each chain table (``merge_identity``), while the ENGINE is still read by the real
(namespaced) id and the resulting dataset id keeps the namespace
(``wide_dataset_id`` on the original relation — the client's ``wideIdFor``
computes exactly that). Mirrored in JS by ``relMergeId``.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# scripts/ holds the canonical table + merge code; make it importable exactly
# like server/loader.py does for table_input.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def merge_identity(ds_id: str, report_id: str | None) -> str:
    """The report-namespace-free identity a dataset id merges under.

    ``'dssrv__sales'`` + report ``'dssrv'`` → ``'sales'`` (what the build-time
    merge would see). Mirrored in JS by ``relMergeId`` (editor-runtime.js) —
    keep the two in step (`tests/test_server_relation.py` asserts parity).
    """
    ds_id = str(ds_id or "")
    rid = str(report_id or "")
    prefix = f"{rid}__"
    if rid and ds_id.startswith(prefix):
        return ds_id[len(prefix):]
    return ds_id


def _chain_ids(relation: dict) -> list[str]:
    """Every dataset id the relation mentions (chain + both sides of every key)."""
    out: list[str] = []
    for t in (relation.get("tables") or []):
        if isinstance(t, str) and t not in out:
            out.append(t)
    for k in (relation.get("keys") or []):
        if not isinstance(k, dict):
            continue
        for side in ("a", "b"):
            ds = (k.get(side) or {}).get("ds")
            if isinstance(ds, str) and ds not in out:
                out.append(ds)
    return out


def merge_relation(relation: dict, report_id: str | None) -> dict:
    """A copy of the relation whose table ids are namespace-free.

    The result is what ``merge_tables.materialize`` must see for its column
    names (collision suffixes) to match the build-time merge. Pure copy — the
    caller's relation keeps the real ids (the wide dataset id depends on it).
    """
    import copy as _copy
    out = _copy.deepcopy(relation)
    ren = {ds: merge_identity(ds, report_id) for ds in _chain_ids(relation)}
    out["tables"] = [ren.get(t, t) for t in (out.get("tables") or [])]
    for k in (out.get("keys") or []):
        if not isinstance(k, dict):
            continue
        for side in ("a", "b"):
            if isinstance(k.get(side), dict) and k[side].get("ds") in ren:
                k[side]["ds"] = ren[k[side]["ds"]]
    fields = out.get("fields")
    if isinstance(fields, dict):
        out["fields"] = {ren.get(k, k): v for k, v in fields.items()}
    return out


def wide_dataset_id(relation: dict) -> str:
    """The dataset id a relation materializes into: ``'<rootTableId>__wide'``.

    Structural, NOT the display name: the id becomes a SQLite table name and a
    URL path segment, and ``rel.name`` is free-form user text (中文/空格/) that
    would have to be percent-encoded everywhere. The display name lives in
    ``label`` instead. Must match the runtime's ``wideIdFor(rel)``.
    """
    chain = [t for t in (relation.get("tables") or []) if isinstance(t, str)]
    if not chain:
        raise ValueError("relation has no tables")
    return f"{chain[0]}__wide"


def wide_label(relation: dict) -> str:
    name = relation.get("name")
    if isinstance(name, str) and name.strip():
        return name
    chain = [t for t in (relation.get("tables") or []) if isinstance(t, str)]
    return f"{chain[0]}（合并）" if chain else "合并宽表"


def merge_in_engine(engine, relation: dict, report_id: str | None = None) -> dict:
    """Read the source tables out of ``engine`` and merge them.

    Returns the canonical ``{columns, rows, meta}`` merged table (``meta``
    carries per-column ``provenance``). Merging happens under the
    namespace-free identity of each table so the column names equal the
    build-time result; the engine is read by the real id.
    Raises ``KeyError(datasetId)`` when a chain table is not loaded.
    """
    from merge_tables import materialize  # noqa: PLC0415  (scripts/ on sys.path above)
    from .store import _qcol

    chain = [t for t in (relation.get("tables") or []) if isinstance(t, str)]
    if len(chain) < 2:
        raise ValueError("relation needs >=2 tables")
    tables: dict[str, dict[str, Any]] = {}
    for ds in chain:
        info = engine.table_info(ds)
        if info is None:
            raise KeyError(ds)
        cols = list(info.columns)
        rows_raw = engine.execute(
            f'SELECT {", ".join(_qcol(c) for c in cols)} FROM {_qcol(ds)}'
        )
        tables[merge_identity(ds, report_id)] = {
            "columns": cols, "rows": [[r[c] for c in cols] for r in rows_raw]}
    return materialize(tables, [merge_relation(relation, report_id)],
                       merge_identity(chain[0], report_id))


def load_relation(engine, relation: dict, report_id: str | None = None):
    """Materialize a relation into the engine as a real table.

    Returns ``(datasetId, TableInfo, merged)`` — the merged table is returned
    too so the caller can carry per-column ``types``/roles onto the served
    manifest (``relation_wide_semantics``). Re-running replaces the previous
    table, so a re-merge after an edit is idempotent.
    """
    merged = merge_in_engine(engine, relation, report_id)
    ds_id = wide_dataset_id(relation)
    engine.drop_table(ds_id)
    info = engine.load_table(ds_id, merged["columns"], merged["rows"])
    return ds_id, info, merged


# Source role → dataset fieldRole. Mirrors the JS ``SEM_TOP_ROLE_MAP``
# (editor-runtime.js) — build-time per-column roles use ``dimension``/``id``
# while a dataset's ``fieldRoles`` uses ``category``/``ignore``.
_SEM_ROLE_MAP = {"time": "time", "measure": "measure", "dimension": "category",
                 "ordinal": "category", "id": "ignore", "ignore": "ignore"}


def source_column_semantics(entry: dict, relation: dict,
                            report_id: str | None = None) -> dict[str, dict[str, dict]]:
    """Per-chain-table ``{column: type}`` + ``{column: role}``, keyed by merge identity.

    Read from the SERVED manifest (``columns[].type`` / ``columnTypes`` /
    ``fieldRoles`` / ``columnRoles``) — that is where the build recorded what the
    source columns mean. A runtime-built wide table then carries the same
    semantics a build-time merged table would, instead of falling back to
    name/value heuristics (which typed every merged column as a plain 维度).
    """
    manifest = entry.get("manifest") or {}
    by_id = {d.get("id"): d for d in (manifest.get("datasets") or [])
             if isinstance(d, dict) and isinstance(d.get("id"), str)}
    out: dict[str, dict[str, dict]] = {}
    for ds in _chain_ids(relation):
        d = by_id.get(ds) or {}
        types = dict(d.get("columnTypes") or {})
        roles = {}
        for c in (d.get("columns") or []):
            if isinstance(c, dict) and c.get("name") and c.get("type"):
                types.setdefault(c["name"], c["type"])
        for col, r in (d.get("fieldRoles") or {}).items():
            role = (r or {}).get("role") if isinstance(r, dict) else r
            if isinstance(role, str) and role:
                roles[col] = role
        for col, r in (d.get("columnRoles") or {}).items():
            if col in roles:
                continue
            mapped = _SEM_ROLE_MAP.get(str(r))
            if mapped:
                roles[col] = mapped
        out[merge_identity(ds, report_id)] = {"types": types, "roles": roles}
    return out


def relation_wide_semantics(merged: dict, entry: dict, relation: dict,
                            report_id: str | None = None) -> dict[str, Any]:
    """``{columnTypes, fieldRoles}`` for a freshly merged wide table.

    Uses ``merge_tables``' per-column provenance (never a second naming walk) to
    carry each source column's type/role onto the merged column — so
    ``dssrv__inv__月份`` is born as 时间 instead of 维度.
    """
    from merge_tables import provenance_of
    prov = provenance_of(merged)
    src = source_column_semantics(entry, relation, report_id)
    column_types: dict[str, str] = {}
    field_roles: dict[str, str] = {}
    for col, p in zip(merged.get("columns") or [], prov):
        sem = src.get(p.get("ds") or "", {})
        t = (sem.get("types") or {}).get(p.get("col") or "")
        if t:
            column_types[col] = t
        r = (sem.get("roles") or {}).get(p.get("col") or "")
        if r:
            field_roles[col] = r
    return {"columnTypes": column_types, "fieldRoles": field_roles}


def wide_manifest_dataset(ds_id: str, label: str, merged: dict, row_count: int,
                          semantics: dict[str, Any] | None = None) -> dict[str, Any]:
    """The served ``manifest.datasets[]`` entry for a wide table.

    ``columns`` carry the merged column NAMES (server schema shape) plus the
    type we could resolve from provenance, so the browser classifies them
    without row data.
    """
    semantics = semantics or {}
    types = semantics.get("columnTypes") or {}
    cols = [{"name": c, "type": types.get(c) or "dimension"}
            for c in (merged.get("columns") or [])]
    return {"id": ds_id, "label": label, "columns": cols, "rowCount": row_count,
            "columnTypes": dict(types), "fieldRoles": dict(semantics.get("fieldRoles") or {})}


def refresh_wide_manifest(manifest: dict, ds_id: str, merged: dict,
                          semantics: dict[str, Any] | None = None) -> bool:
    """Re-point the served wide dataset at the just-merged columns.

    Called on boot/replay and after a runtime merge: the merge is the source of
    truth for the column LIST, while an existing user-set role/type entry is
    preserved (only missing columns are filled in). Returns True when the
    manifest was touched.
    """
    semantics = semantics or {}
    changed = False
    for d in (manifest.get("datasets") or []):
        if not isinstance(d, dict) or d.get("id") != ds_id:
            continue
        fresh = wide_manifest_dataset(ds_id, d.get("label") or ds_id, merged,
                                      d.get("rowCount") or 0, semantics)
        for key in ("columns", "rowCount"):
            if d.get(key) != fresh[key]:
                d[key] = fresh[key]
                changed = True
        for key in ("columnTypes", "fieldRoles"):
            cur = dict(d.get(key) or {})
            for col, val in (fresh.get(key) or {}).items():
                if col not in cur:
                    cur[col] = val
                    changed = True
            if cur:
                d[key] = cur
    return changed


def derived_entry(ds_id: str, relation: dict, row_count: int) -> dict:
    """A ``datasets[]`` registry entry for a derived wide table.

    ``source`` is None (there is no single file); ``derived`` tells the boot /
    reload paths to rebuild it from the source tables instead of reading a file.
    """
    return {
        "id": ds_id,
        "label": wide_label(relation),
        "rowCount": row_count,
        "source": None,
        "derived": {"kind": "relation", "relation": relation},
    }


def is_derived(d: dict) -> bool:
    return isinstance(d, dict) and (d.get("derived") or {}).get("kind") == "relation"


def replay_entry(engine, entry: dict, verbose: bool = False) -> list[dict]:
    """Rebuild every derived relation dataset of one registry entry.

    Plain (file-backed) datasets must already be loaded — call this AFTER the
    ordinary load pass. Missing sources are skipped with a warning so one bad
    entry never takes the whole server down. Returns ``[{id, rowCount}]``.
    """
    out: list[dict] = []
    report_id = entry.get("reportId")
    for d in entry.get("datasets", []) or []:
        if not is_derived(d):
            continue
        rel = d["derived"]["relation"]
        try:
            ds_id, info, merged = load_relation(engine, rel, report_id)
        except Exception as e:  # noqa: BLE001  (best-effort boot path)
            if verbose:
                print(f"[ai-report-server] WARN derived dataset {d.get('id')!r} failed: {e}",
                      file=sys.stderr)
            continue
        d["rowCount"] = info.row_count
        d["label"] = d.get("label") or wide_label(rel)
        # The re-merge is the source of truth for the column LIST — if it drifted
        # from what the served manifest claims, every chart on this table breaks.
        manifest = entry.get("manifest")
        if isinstance(manifest, dict):
            refresh_wide_manifest(manifest, ds_id, merged,
                                  relation_wide_semantics(merged, entry, rel, report_id))
        out.append({"id": ds_id, "rowCount": info.row_count})
    return out
