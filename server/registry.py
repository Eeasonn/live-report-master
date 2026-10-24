"""Multi-report registry with on-disk persistence.

The registry holds metadata for every registered report: its manifest,
which datasets it references, when it was last reloaded, and where the
original canonical table lives on disk.

The registry itself does NOT hold table data — that's the engine's job.
Reloading is a separate operation: re-read the disk table, push it into
the engine, and update the registry entry.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

# Defaults live OUTSIDE the skill package so running the server never bloats the
# skill (a large indexed dataset would otherwise be written into the skill dir,
# ballooning it to hundreds of MB). We use a per-user state dir that manually
# started servers, start_server.cmd, and report_build agree on. report_build
# server mode overrides these with workdir-local paths (--workdir/build), so
# shared servers keep their state with the build, not in the package.
# LRM_STATE_HOME（2026-09-20 统一 manager）：测试/CI 用它把机器级状态隔离到
# 临时目录——统一 manager 下所有构建共用一份中央注册表，套件跑真实 server
# 构建时绝不能写进用户真实状态。
_USER_STATE = Path(os.environ.get("LRM_STATE_HOME") or (Path.home() / ".ai-html-report"))
_DEFAULT_REGISTRY = _USER_STATE / "registry.json"
_DEFAULT_DATA_DIR = _USER_STATE / "data"

# Backward-compatible aliases kept for any external importers.
DEFAULT_REGISTRY = _USER_STATE / "registry.json"
DEFAULT_DATA_DIR = _USER_STATE / "data"
_SKILL = Path(__file__).resolve().parent.parent


class Registry:
    def __init__(self, path: Path | str = DEFAULT_REGISTRY):
        self._path = Path(path).expanduser()
        self._lock = threading.RLock()
        self._entries: dict[str, dict[str, Any]] = {}
        self._load()

    # -- persistence ------------------------------------------------------------

    def _load(self) -> None:
        if not self._path.exists():
            self._entries = {}
            return
        try:
            entries = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(entries, dict):
                raise ValueError("registry root must be a JSON object")
            self._entries = entries
        except (json.JSONDecodeError, OSError, ValueError):
            # Corrupt or unreadable: start fresh, but QUARANTINE the bad file
            # first（改名保留，不动内容）。此前只置空不保留——下一次任意
            # register 会用只含新条目的 dict 覆盖整份 registry.json，全机其余
            # 报告从统一 manager 静默消失且无从找回。坏文件在，人工可恢复。
            self._entries = {}
            try:
                stamp = time.strftime("%Y%m%d-%H%M%S")
                quarantine = self._path.with_name(f"{self._path.name}.corrupt-{stamp}")
                seq = 0
                while quarantine.exists():
                    seq += 1
                    quarantine = self._path.with_name(
                        f"{self._path.name}.corrupt-{stamp}-{seq}")
                os.replace(self._path, quarantine)
                print(f"[ai-report-server] WARN registry 损坏（或形状非法），坏文件已保留 → "
                      f"{quarantine.name}；本次以空注册表启动",
                      file=sys.stderr)
            except OSError:
                pass

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # 固定 tmp 名在两进程并发 register 时互踩（A 的 replace 目标被 B 先删 →
        # FileNotFoundError；NP-01 并发探针 2026-10-24 实锤）——tmp 名带 pid+tid。
        tmp = self._path.with_suffix(
            self._path.suffix + f".tmp-{os.getpid()}-{threading.get_ident()}")
        tmp.write_text(
            json.dumps(self._entries, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        os.replace(tmp, self._path)
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass

    # -- CRUD -------------------------------------------------------------------

    def register(self, report_id: str, entry: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            entry["reportId"] = report_id
            entry.setdefault("registeredAt", time.time())
            entry["updatedAt"] = time.time()
            self._entries[report_id] = entry
            self._save()
            return entry

    def get(self, report_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            entry = self._entries.get(report_id)
            return json.loads(json.dumps(entry)) if entry else None  # deep copy

    def unregister(self, report_id: str) -> bool:
        with self._lock:
            if report_id in self._entries:
                del self._entries[report_id]
                self._save()
                return True
            return False

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [json.loads(json.dumps(e)) for e in self._entries.values()]

    def raw_entries(self) -> list[dict[str, Any]]:
        """Read-only view of the entries WITHOUT the deep copy ``list()`` makes.

        List/health/manager paths only read a few fields per entry. ``list()``
        JSON round-trips whole entries — and a report whose runtime-added dataset
        carries rows puts tens of MB in ONE entry — which made ``/api/reports``
        take seconds and blew the supervisor's probe timeouts (report_build then
        thought the port belonged to another report and moved the report's port).
        Callers MUST treat the returned dicts as read-only.
        """
        with self._lock:
            return list(self._entries.values())

    def items_raw(self) -> list[tuple[str, dict[str, Any]]]:
        """``(report_id, entry)`` pairs without deep copies (same contract as
        ``raw_entries``: read-only dicts, caller never mutates).

        表清单/共享表判定需要**按 reportId** 找「还有谁声明了这个 dataset id」，
        ``raw_entries`` 只给值不给键——给键的迭代只能放这里，不许调用方靠
        ``entry["reportId"]`` 反推（运行时新增的条目可能还没被 register 写上）。
        """
        with self._lock:
            return list(self._entries.items())

    def summary(self) -> dict[str, int]:
        """Cheap counts for /api/health — never deep-copies the entries.

        ``list()`` JSON round-trips every entry; a report whose runtime-added
        dataset carries rows makes the registry tens of MB, and that cost pushed
        ``/api/health`` past the supervisor's short ping timeout — servers then
        looked DEAD to the 报告管理 index (which ping-prunes). Counts only need
        the dict keys, so this stays O(entries) with no serialization.
        """
        with self._lock:
            datasets: set[str] = set()
            for entry in self._entries.values():
                for d in (entry.get("datasets") or []):
                    if isinstance(d, dict) and d.get("id"):
                        datasets.add(str(d["id"]))
            return {"reportCount": len(self._entries), "datasetCount": len(datasets)}

    def reload_disk(self) -> None:
        """Re-read the registry file from disk (used by /admin/reload-registry)."""
        with self._lock:
            self._load()

    @property
    def path(self) -> Path:
        return self._path


def snapshot_data_file(source: Path, data_dir: Path | None = None, *, namespace: str | None = None) -> Path:
    """Copy the source file into the canonical data dir (avoids path drift).

    The registry records the snapshot path so server reload survives the
    original file being moved or deleted.
    """
    data_dir = (data_dir or DEFAULT_DATA_DIR).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)
    safe_namespace = re.sub(r'[^A-Za-z0-9_-]+', '_', namespace).strip('_') if namespace else ''
    # Slugification collapses distinct CJK namespaces onto one name; when it
    # actually changed the namespace, append a short stable hash so two
    # datasets never share one snapshot file (reload would swap their data).
    if safe_namespace and safe_namespace != namespace:
        import hashlib
        safe_namespace = (f"{safe_namespace}-"
                          f"{hashlib.sha1(namespace.encode('utf-8')).hexdigest()[:8]}")
    target_name = f"{safe_namespace}__{source.name}" if safe_namespace else source.name
    target = data_dir / target_name
    if source.resolve() != target.resolve():
        # avoid recursive copy
        import shutil
        shutil.copy2(source, target)
    return target


# ============================================================================
# Canonical table loader (former server/loader.py — merged NQ-02, zero behavior
# change). Wraps the existing scripts/table_input.py so server modules get the
# same canonical {columns, rows, meta} shape that the rest of the skill uses.
# Intentionally thin; the heavy lifting lives in the shared
# scripts/table_input.py, imported lazily here exactly as before so importing
# the registry never pulls xlsx drivers.
# ============================================================================

# Make scripts/ importable when running this file from the skill root.
_LOADER_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_LOADER_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_LOADER_SCRIPTS_DIR))


def load(path: Path | str) -> dict:
    """Load xlsx/csv/json into canonical {columns, rows, meta}."""
    from table_input import load_table_file  # type: ignore[import-not-found]  # noqa: E402
    return load_table_file(Path(path))


# ============================================================================
# Runtime relation materialization (former server/relations.py — merged NQ-02
# wave 2, zero behavior change). Wide-table runtime merge: every public name
# (merge_identity, merge_relation, wide_dataset_id, wide_label, merge_in_engine,
# load_relation, source_column_semantics, relation_wide_semantics,
# wide_manifest_dataset, refresh_wide_manifest, derived_entry, is_derived,
# replay_entry) and its body is unchanged — only the module shell is absorbed.
# The former module's sys.path bootstrap for scripts/ is redundant here (the
# loader section above already inserts scripts/ at import; the lazy
# ``from merge_tables import …`` below relies on exactly that).
# ============================================================================

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
