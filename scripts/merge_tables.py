#!/usr/bin/env python3
"""v4 single-mode merge: fold N tables into ONE wide table.

Each ``relations[i]`` of the request declares ONE merge with
``tables:[A,B,C,…]`` and ``keys:[{a:{ds,col}, b:{ds,col}, mode}]``.
The merge walks the tables in order, joining each subsequent table onto the
running wide via the first ``keys[]`` entry that connects it back to the
chain (left side = the chain so far; right side = the next table). The
``mode`` of that key decides which side's key column survives the join:

  - ``a``  → keep the LEFT side's column, drop the right's key column.
  - ``b``  → keep the RIGHT side's column, drop the left's key column.
  - ``both`` → keep BOTH columns. On name collision, the right's key gets a
    ``<right_ds>__`` suffix so both names survive.

The first table in ``tables[]`` is the chain root — its row count is the
floor of the merged table's row count (LEFT JOIN semantics; unreferenced
right-table rows are dropped, never Cartesian-ed). The optional ``fields``
map per relation filters which columns of each table actually contribute.

本模块只做确定性数据搬运，不读 SQL、不注入 join 语法超出「键等值」的范围。
"""
from __future__ import annotations

import re
from typing import Any

_MONTH_RE = re.compile(r"^(\d{4})[-/年](\d{1,2})")
_YYYYMM_RE = re.compile(r"^(\d{4})(\d{2})$")
_FLOAT_INT_RE = re.compile(r"^\d+\.0$")


def col_names(table: dict[str, Any]) -> list[str]:
    """Normalise a canonical table's columns to a name list (server manifests
    carry ``[{name,type}]``; singlefile carries bare strings)."""
    out: list[str] = []
    for c in table.get("columns") or []:
        out.append(c.get("name") if isinstance(c, dict) else c)
    return out


def key_label(value: Any, bucket: str | None) -> str:
    """Normalise a join key the way the runtime does (so build-time and runtime
    merges agree): strip a trailing ``.0`` and bucket temporal keys."""
    if value is None:
        return ""
    s = str(value).strip()
    if _FLOAT_INT_RE.match(s):
        s = s[:-2]
    if bucket and bucket != "raw":
        m = _MONTH_RE.match(s) or _YYYYMM_RE.match(s)
        if m:
            if bucket == "year":
                return m.group(1)
            if bucket == "month":
                return f"{m.group(1)}-{int(m.group(2)):02d}"
    return s


def _filter_table(table: dict[str, Any], keep: list[str] | None) -> tuple[list[str], list[list[Any]]]:
    """Project a table to a subset of columns. ``keep=None`` → identity."""
    cols = col_names(table)
    if not keep:
        return cols, [list(r) for r in table.get("rows") or []]
    idx = []
    out_cols: list[str] = []
    seen: set = set()
    for c in keep:
        if c in cols and c not in seen:
            idx.append(cols.index(c))
            out_cols.append(c)
            seen.add(c)
    rows = [[r[i] for i in idx] for r in (table.get("rows") or [])]
    return out_cols, rows


def _suffix_if_collision(name: str, used: set, tag: str) -> str:
    out = name
    while out in used:
        out = f"{tag}__{out}"
    return out


def _chain_join(left_cols: list[str], left_rows: list[list[Any]], lk: str,
                right_cols: list[str], right_rows: list[list[Any]], rk: str,
                mode: str, right_ds: str,
                left_prov: list[dict[str, str]] | None = None,
                ) -> tuple[list[str], list[list[Any]], list[dict[str, str]]]:
    """LEFT JOIN right onto left on lk == rk; apply per-key ``mode``.

    Returns (new_cols, new_rows, provenance). Always preserves left-side row
    count (rows with no right match keep left values and fill right columns
    with None). Left's NON-key columns always survive (renamed on collision
    with right's non-key additions). The ``mode`` only governs the key column
    itself.

    ``provenance`` is one ``{ds, col}`` per output column, in output order —
    the SINGLE source for "which source column did this merged column come
    from", used to carry per-column types/roles onto a runtime wide table
    (``server/relations.py``). Deriving it here (rather than re-walking the
    chain elsewhere) is deliberate: a second naming walk would drift from the
    names this function actually produces.
    """
    mode = mode or "both"
    li = left_cols.index(lk)
    ri = right_cols.index(rk)
    rindex: dict[str, list[Any]] = {}
    for r in right_rows:
        rindex.setdefault(key_label(r[ri], None), r)

    keep_lk = mode in ("a", "both")
    keep_rk = mode in ("b", "both")

    # Place the surviving key column first. mode='b' drops left's lk entirely
    # and uses right's rk (renamed if collision) in its slot.
    prov: list[dict[str, str]] = []
    if left_prov is None:
        left_prov = [{"ds": "", "col": c} for c in left_cols]
    used: set = set()
    cols: list[str] = []
    if keep_lk:
        cols.append(lk)
        used.add(lk)
        prov.append(dict(left_prov[li]))
    elif keep_rk:
        rename = _suffix_if_collision(rk, used, right_ds)
        cols.append(rename)
        used.add(rename)
        prov.append({"ds": right_ds, "col": rk})

    # Then left's NON-key columns (everything except lk). Rename if they
    # collide with already-used names (which only happens if mode='b' renamed
    # right's rk into lk's slot — in that case left's non-keys keep their
    # original names unless they collide with right_add below).
    left_add: list[tuple[int, str]] = []
    for i, c in enumerate(left_cols):
        if i == li:
            continue
        new_name = _suffix_if_collision(c, used, right_ds)
        used.add(new_name)
        left_add.append((i, new_name))
    cols += [n for _, n in left_add]
    prov += [dict(left_prov[i]) for i, _ in left_add]

    # Then right's NON-key columns.
    right_add: list[tuple[int, str]] = []
    for i, c in enumerate(right_cols):
        if i == ri:
            continue
        new_name = _suffix_if_collision(c, used, right_ds)
        used.add(new_name)
        right_add.append((i, new_name))
    cols += [n for _, n in right_add]
    prov += [{"ds": right_ds, "col": c} for i, c in enumerate(right_cols) if i != ri]

    # If mode='both', right's rk survives too (rename if collision).
    if mode == "both":
        rk_survivor = _suffix_if_collision(rk, used, right_ds)
        used.add(rk_survivor)
        cols.append(rk_survivor)
        prov.append({"ds": right_ds, "col": rk})

    # Build output rows.
    new_rows: list[list[Any]] = []
    for r in left_rows:
        k = key_label(r[li], None)
        rr = rindex.get(k)
        base: list[Any] = [r[i] for i, _ in left_add]
        if rr is None:
            extras = [None] * len(right_add)
            extra_rk: Any = None
        else:
            extras = [rr[i] for i, _ in right_add]
            extra_rk = rr[ri]
        if mode == "both":
            extras = extras + [extra_rk]  # rk_survivor is the LAST appended column
        if keep_lk:
            new_rows.append([r[li]] + base + extras)
        elif keep_rk:
            new_rows.append([(rr[ri] if rr else None)] + base + extras)
        else:
            new_rows.append(base + extras)
    return cols, new_rows, prov


def renamed_columns(cols: list[str], renames: dict[str, Any] | None) -> list[str]:
    """Apply a relation's ``renames`` map to the merged column list.

    ``renames`` maps a column's *pre-rename* merged name to the user's alias
    (``{合并前列名: 新名}``), so the alias survives a re-merge: the chain join
    rebuilds names from the source tables, then this runs LAST and restores the
    user's names. Aliases whose source column is gone (table set / key mode
    changed) simply do not match and are dropped.

    Names are de-duplicated in column order, so an alias that collides with a
    surviving column becomes ``新名__2`` instead of silently shadowing it.
    Mirrored in the runtime by ``renamedColumns`` (editor-runtime.js).
    """
    if not renames:
        return list(cols)
    out: list[str] = []
    used: set = set()
    for c in cols:
        want = renames.get(c, c)
        name = want
        n = 2
        while name in used:
            name = f"{want}__{n}"
            n += 1
        used.add(name)
        out.append(name)
    return out


def materialize(tables: dict[str, dict[str, Any]], relations: list[dict[str, Any]],
                base_id: str) -> dict[str, Any]:
    """Fold the FIRST relation into ``base_id``'s table and return the merged
    table. Only one relation is materialized per request (the request-layer
    validator caps relations at 1); the merged table rides the ordinary
    multi-table downstream as the ``<base>__wide`` dataset — see
    ``report_build._resolve_relations_spec`` (D1 逻辑直通). Additional
    relations would need separate merged tables, which the single-wide-id
    naming (``<rootTable>__wide``) doesn't support yet.

    Returns ``{columns, rows, meta}`` (canonical table shape). Raises
    ``ValueError`` when chain connectivity fails — validate_request should
    catch it earlier; this is a defensive double-check.
    """
    if not relations:
        raise ValueError("materialize() needs ≥1 relation")
    rel = relations[0]
    if len(relations) > 1:
        raise ValueError(
            f"only one relation per request is materialized into the wide "
            f"table (got {len(relations)}); declare a wider single relation."
        )
    chain = rel.get("tables") or []
    keys = rel.get("keys") or []
    fields_filter = rel.get("fields") or {}

    # Step 1: project root table to its selected columns.
    cur_cols, cur_rows = _filter_table(tables[chain[0]], fields_filter.get(chain[0]))
    cur_prov: list[dict[str, str]] = [{"ds": chain[0], "col": c} for c in cur_cols]
    # Step 2: walk the rest of the chain in tables order, joining each via the
    # first key that connects it back to the chain-so-far.
    connected: set = {chain[0]}
    for next_ds in chain[1:]:
        key = next(
            (k for k in keys
             if (k["a"]["ds"] == next_ds and k["b"]["ds"] in connected)
             or (k["b"]["ds"] == next_ds and k["a"]["ds"] in connected)),
            None,
        )
        if key is None:
            raise ValueError(
                f"no key connects {next_ds!r} to the running chain "
                f"{sorted(connected)}; add a key in relation {rel.get('id')!r}"
            )
        # Orientation: A is in chain (left), B is the new table (right).
        if key["a"]["ds"] in connected:
            lk_ds, lk_col, rk_ds, rk_col = key["a"]["ds"], key["a"]["col"], key["b"]["ds"], key["b"]["col"]
        else:
            lk_ds, lk_col, rk_ds, rk_col = key["b"]["ds"], key["b"]["col"], key["a"]["ds"], key["a"]["col"]
        if lk_col not in cur_cols:
            raise ValueError(
                f"relation {rel.get('id')!r}: key {lk_ds}.{lk_col} not in "
                f"running chain columns {cur_cols}"
            )
        r_cols, r_rows = _filter_table(tables[rk_ds], fields_filter.get(rk_ds))
        if rk_col not in r_cols:
            raise ValueError(
                f"relation {rel.get('id')!r}: key {rk_ds}.{rk_col} not in "
                f"right table columns {r_cols}"
            )
        cur_cols, cur_rows, cur_prov = _chain_join(cur_cols, cur_rows, lk_col,
                                                   r_cols, r_rows, rk_col,
                                                   key.get("mode", "both"), rk_ds,
                                                   cur_prov)
        connected.add(next_ds)
    # Step 3: user aliases LAST (survive the re-merge). Aliases only rewrite the
    # NAME, so provenance stays in the same order.
    cur_cols = renamed_columns(cur_cols, rel.get("renames"))
    return {"columns": cur_cols, "rows": cur_rows,
            # ``provenance`` = merged column → the source column it came from.
            # Carried in ``meta`` so server-side consumers can type/role the
            # merged columns without re-walking the chain.
            "meta": {"provenance": [dict(p) for p in cur_prov]}}


def merged_dataset_id(base_id: str) -> str:
    return f"{base_id}__wide"


def provenance_of(merged: dict[str, Any]) -> list[dict[str, str]]:
    """The merged table's per-column provenance (``[]`` when unavailable).

    Use this instead of re-deriving names: it always matches ``columns``.
    """
    prov = ((merged.get("meta") or {}).get("provenance")) if isinstance(merged, dict) else None
    if not isinstance(prov, list) or len(prov) != len(merged.get("columns") or []):
        return []
    return [p for p in prov if isinstance(p, dict)]