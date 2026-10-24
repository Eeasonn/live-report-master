"""Bounded, approval-gated physical index advice for the local report server.

This module is the F05 logic core.  It observes only a bounded *semantic query
shape* and never stores SQL, filter values, prompts, or rows.  A proposal is a
persistent, hash-fenced intent; only an explicitly approved ``apply`` may issue
the one supported SQLite DDL.

Grounding (mandatory for this feature):

* ``refs/flint/src/core/aggregate.ts:7-20`` defines grouping dimensions as
  channels without an aggregate and describes aggregate outputs as derived
  fields.  The recorder mirrors those dimension/measure roles only.
* ``refs/flint/src/core/field-semantics.ts:139-150`` keeps field identity and
  intrinsic properties independent of visualization.  The advisor therefore
  records field/aggregation semantics rather than chart SQL or rendering data.

Only a single non-unique B-tree index on an existing column is supported, and
only for SQLite.  DuckDB stays read-only: ``server/store.py`` deliberately
discards ``index_columns`` in its DuckDB load path, so pretending to apply
SQLite DDL would be unsafe.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
import time
import weakref
from pathlib import Path
from typing import Any, Callable, Mapping

from .store import Engine
from ._platform import AggregateRequest


MAX_QUERY_SHAPES = 256
MAX_FILTER_PREDICATES = 16
MAX_SHAPE_IDS_PER_CANDIDATE = 16
MAX_PROPOSALS = 256
MAX_OBSERVATIONS = 1_000_000
SUPPORTED_APPLY_ENGINES = {"sqlite"}
_PROPOSAL_RE = re.compile(r"^index-proposal-[0-9a-f]{32}$")
_IDENT_RE = re.compile(r"^[^\x00]+$")
_MAX_ACTOR = 160
_MAX_COMMENT = 500


class IndexAdvisorError(ValueError):
    """Malformed, unsupported, or otherwise unsafe index-advisor input."""
    status = 400


class IndexProposalNotFound(IndexAdvisorError):
    """A proposal id is not present in the local proposal store."""
    status = 404


class IndexProposalConflict(IndexAdvisorError):
    """A proposal's optimistic fence no longer matches the engine."""
    status = 409


class IndexApprovalError(IndexAdvisorError):
    """An explicit admin/editor approval is missing or malformed."""
    status = 422


_SHAPE_LOCK = threading.RLock()
# Weak keys keep observations scoped to the live engine and bounded in memory.
_SHAPES: weakref.WeakKeyDictionary[Any, dict[tuple[str, str], dict[str, Any]]] = (
    weakref.WeakKeyDictionary()
)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def _hash(value: Any, length: int = 64) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()[:length]


def _valid_ident(value: Any, *, max_len: int = 256) -> bool:
    return isinstance(value, str) and 0 < len(value) <= max_len and bool(_IDENT_RE.fullmatch(value))


def _quote_identifier(value: Any, label: str) -> str:
    if not _valid_ident(value):
        raise IndexAdvisorError(f"{label} must be a non-empty identifier")
    return '"' + value.replace('"', '""') + '"'


def _unique_strings(values: Any, *, maximum: int) -> list[str] | None:
    if not isinstance(values, list) or len(values) > maximum:
        return None
    out: list[str] = []
    for value in values:
        if not _valid_ident(value):
            return None
        if value not in out:
            out.append(value)
    return out


def _filter_shape(value: Any, *, depth: int = 0,
                  out: list[dict[str, str]] | None = None) -> list[dict[str, str]] | None:
    """Strip filter values while retaining a bounded predicate shape."""
    if out is None:
        out = []
    if depth > 4 or len(out) > MAX_FILTER_PREDICATES:
        return None
    if isinstance(value, list):
        if len(value) > MAX_FILTER_PREDICATES:
            return None
        for child in value:
            if _filter_shape(child, depth=depth + 1, out=out) is None:
                return None
        return out
    if not isinstance(value, Mapping):
        return None
    column = value.get("column")
    if column is not None:
        if not _valid_ident(column):
            return None
        op = value.get("op", "=")
        if not isinstance(op, str) or not op or len(op) > 32:
            return None
        out.append({"column": column, "op": op.lower()})
    for key in ("all", "any", "not"):
        if key in value and _filter_shape(value[key], depth=depth + 1, out=out) is None:
            return None
    return out


def semantic_query_shape(table_name: str, request: AggregateRequest) -> dict[str, Any] | None:
    """Return a bounded, value-free semantic query shape or ``None``.

    This is intentionally an aggregation-level shape, not a SQL fingerprint.
    Equivalent filter values share a shape; arbitrary free-form DDL can never
    become a candidate.
    """
    if not _valid_ident(table_name):
        return None
    group_by = _unique_strings(request.group_by, maximum=6)
    if group_by is None:
        return None
    filter_shape = _filter_shape(request.filter)
    if filter_shape is None:
        return None
    measures: list[dict[str, str]] = []
    if not isinstance(request.measures, list) or len(request.measures) > 12:
        return None
    for item in request.measures:
        if not isinstance(item, Mapping):
            return None
        column = item.get("column")
        if column != "*" and not _valid_ident(column):
            return None
        aggregation = item.get("aggregation", "sum")
        if not isinstance(aggregation, str) or not aggregation or len(aggregation) > 24:
            return None
        measures.append({"column": str(column), "aggregation": aggregation.lower()})
    order_by: list[dict[str, str]] = []
    if not isinstance(request.order_by, list) or len(request.order_by) > 6:
        return None
    for item in request.order_by:
        if not isinstance(item, Mapping) or not _valid_ident(item.get("column")):
            return None
        direction = str(item.get("direction", "desc")).lower()
        if direction not in {"asc", "desc"}:
            return None
        order_by.append({"column": item["column"], "direction": direction})
    bucket = request.bucket if isinstance(request.bucket, Mapping) else None
    bucket_shape = None
    if bucket:
        source = bucket.get("source")
        bucket_name = bucket.get("bucket", "month")
        if not _valid_ident(source) or not isinstance(bucket_name, str) or len(bucket_name) > 24:
            return None
        bucket_shape = {"source": source, "bucket": bucket_name.lower()}
    limit = request.limit
    offset = request.offset
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int)
                              or limit < 0 or limit > 10_000):
        return None
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or offset > 10_000:
        return None
    filter_columns: list[str] = []
    for item in filter_shape:
        if item["column"] not in filter_columns:
            filter_columns.append(item["column"])
    roles: list[dict[str, str]] = []
    for column in group_by:
        roles.append({"column": column, "role": "group"})
    for column in filter_columns:
        roles.append({"column": column, "role": "filter"})
    shape: dict[str, Any] = {
        "datasetId": table_name,
        "groupBy": group_by,
        "filter": filter_shape,
        "filterColumns": filter_columns,
        "measures": measures,
        "bucket": bucket_shape,
        "orderBy": order_by,
        "limit": limit,
        "offset": offset,
        "candidateRoles": roles,
    }
    shape["shapeHash"] = _hash(shape, 24)
    return shape


def record_query_shape(engine: Engine, table_name: str, request: AggregateRequest) -> None:
    """Record one bounded shape after aggregate validation; never store values/SQL."""
    shape = semantic_query_shape(table_name, request)
    if shape is None or not shape["candidateRoles"]:
        return
    key = (table_name, shape["shapeHash"])
    now = time.time()
    with _SHAPE_LOCK:
        shapes = _SHAPES.setdefault(engine, {})
        item = shapes.get(key)
        if item is None:
            item = {"shape": shape, "observations": 0, "lastSeen": now}
            shapes[key] = item
        item["observations"] = min(MAX_OBSERVATIONS, int(item.get("observations", 0)) + 1)
        item["lastSeen"] = now
        if len(shapes) > MAX_QUERY_SHAPES:
            victims = sorted(
                shapes.items(),
                key=lambda pair: (int(pair[1].get("observations", 0)),
                                  float(pair[1].get("lastSeen", 0))),
            )
            for victim_key, _ in victims[:len(shapes) - MAX_QUERY_SHAPES]:
                shapes.pop(victim_key, None)


def _observed_shapes(engine: Engine, table_name: str | None = None) -> list[dict[str, Any]]:
    with _SHAPE_LOCK:
        shapes = _SHAPES.get(engine, {})
        return [copy.deepcopy(item) for (table, _), item in shapes.items()
                if table_name is None or table == table_name]


def _engine_name(engine: Engine) -> str:
    return str(getattr(engine, "engine_name", "") or "").lower()


def _sqlite_index_state(engine: Engine, dataset_id: str) -> tuple[list[dict[str, Any]], str]:
    """Read SQLite metadata only; non-SQLite is explicitly unsupported."""
    if _engine_name(engine) != "sqlite":
        raise IndexAdvisorError(
            f"engine {engine.engine_name!r} does not support the approved index operation")
    if engine.table_info(dataset_id) is None:
        raise IndexAdvisorError("unknown dataset")
    table = _quote_identifier(dataset_id, "datasetId")
    rows = engine.execute(f"PRAGMA index_list({table})", timeout_seconds=5)
    state: list[dict[str, Any]] = []
    for row in rows:
        name = row.get("name")
        if not isinstance(name, str) or not name:
            raise IndexAdvisorError("engine returned an invalid index name")
        info_rows = engine.execute(
            f"PRAGMA index_info({_quote_identifier(name, 'index name')})", timeout_seconds=5)
        columns = [item.get("name") for item in sorted(
            info_rows, key=lambda item: int(item.get("seqno", 0) or 0))]
        if any(not isinstance(column, str) for column in columns):
            raise IndexAdvisorError("engine returned an invalid index column")
        state.append({"name": name, "columns": columns,
                      "unique": bool(row.get("unique", 0)),
                      "origin": str(row.get("origin") or ""),
                      "partial": bool(row.get("partial", 0))})
    state.sort(key=lambda item: (item["name"], item["columns"]))
    return state, _hash(state)


def _read_index_state(engine: Engine, dataset_id: str) -> tuple[list[dict[str, Any]], str, bool]:
    if _engine_name(engine) != "sqlite":
        # Do not issue SQLite PRAGMA against DuckDB and do not claim support.
        state: list[dict[str, Any]] = []
        return state, _hash({"engine": _engine_name(engine), "indexes": state}), False
    state, digest = _sqlite_index_state(engine, dataset_id)
    return state, digest, True


def _table_fingerprint(engine: Engine, dataset_id: str) -> tuple[dict[str, Any], str]:
    info = engine.table_info(dataset_id)
    if info is None:
        raise IndexAdvisorError("unknown dataset")
    schema = []
    for column in info.schema or []:
        if hasattr(column, "to_dict"):
            schema.append(column.to_dict())
        elif hasattr(column, "__dict__"):
            schema.append(dict(column.__dict__))
        else:
            schema.append(str(column))
    state = {"datasetId": info.name, "columns": list(info.columns),
             "schema": schema, "rowCount": int(info.row_count),
             "engineRevision": int(getattr(engine, "revision", 0))}
    return state, _hash(state)


def current_fence(engine: Engine, dataset_id: str, column: str) -> dict[str, Any]:
    info = engine.table_info(dataset_id)
    if info is None:
        raise IndexProposalNotFound("unknown dataset")
    if not _valid_ident(column) or column not in info.columns:
        raise IndexAdvisorError("unknown dataset or column")
    indexes, index_hash, supported = _read_index_state(engine, dataset_id)
    table_state, dataset_hash = _table_fingerprint(engine, dataset_id)
    fence: dict[str, Any] = {
        "datasetId": dataset_id, "column": column, "engine": _engine_name(engine),
        "datasetHash": dataset_hash, "datasetState": table_state,
        "indexStateHash": index_hash, "indexState": indexes,
        "applySupported": supported,
    }
    fence["fenceHash"] = _hash(fence)
    return fence


def _has_single_column_index(indexes: list[dict[str, Any]], column: str) -> bool:
    return any(item.get("columns") == [column] for item in indexes)


def _candidate_id(dataset_id: str, column: str, role: str, shape_ids: list[str]) -> str:
    return "idxc-" + _hash({"datasetId": dataset_id, "column": column,
                            "role": role, "shapeIds": shape_ids}, 24)


def suggestions_for_engine(engine: Engine, table_name: str | None = None,
                           *, min_observations: int = 2) -> dict[str, Any]:
    """Return read-only suggestions from observed bounded semantic shapes."""
    threshold = max(1, min(MAX_OBSERVATIONS, int(min_observations)))
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in _observed_shapes(engine, table_name):
        shape = item.get("shape") or {}
        table = shape.get("datasetId")
        count = int(item.get("observations", 0))
        shape_id = shape.get("shapeHash")
        for role_item in shape.get("candidateRoles") or []:
            column, role = role_item.get("column"), role_item.get("role")
            if not isinstance(table, str) or not isinstance(column, str) \
                    or role not in {"group", "filter"}:
                continue
            entry = grouped.setdefault((table, column, role),
                                       {"observations": 0, "shapeIds": [], "shapeCount": 0})
            entry["observations"] += count
            if isinstance(shape_id, str) and shape_id not in entry["shapeIds"]:
                entry["shapeIds"].append(shape_id)
                entry["shapeCount"] += 1
    rows: list[dict[str, Any]] = []
    for (table, column, role), stats in grouped.items():
        if stats["observations"] < threshold:
            continue
        info = engine.table_info(table)
        if info is None or column not in info.columns:
            continue
        indexes, index_hash, supported = _read_index_state(engine, table)
        shape_ids = list(stats["shapeIds"][:MAX_SHAPE_IDS_PER_CANDIDATE])
        rows.append({
            "candidateId": _candidate_id(table, column, role, shape_ids),
            "datasetId": table, "column": column, "role": role,
            "observations": stats["observations"], "shapeCount": stats["shapeCount"],
            "shapeIds": shape_ids, "engine": _engine_name(engine),
            "applySupported": supported, "indexed": _has_single_column_index(indexes, column),
            "indexStateHash": index_hash,
        })
    rows.sort(key=lambda item: (-item["observations"], item["datasetId"],
                                item["column"], item["role"]))
    return {
        "kind": "index_advice", "readOnly": True, "engine": _engine_name(engine),
        "applySupported": _engine_name(engine) in SUPPORTED_APPLY_ENGINES,
        "suggestions": rows[:32],
        "message": "建议仅供 DBA/管理员审批；服务端不会自动建索引。",
    }


def explain_candidate(engine: Engine, table_name: str, column: str) -> dict[str, Any]:
    """Run a bounded read-only plan probe; never creates or changes an index."""
    info = engine.table_info(table_name)
    if info is None or column not in info.columns:
        raise IndexAdvisorError("unknown dataset or index candidate column")
    if _engine_name(engine) != "sqlite":
        return {"kind": "index_explain", "readOnly": True, "datasetId": table_name,
                "column": column, "engine": engine.engine_name, "supported": False,
                "reason": "当前引擎没有安全的 SQLite EXPLAIN/索引应用契约"}
    sql = (f"EXPLAIN QUERY PLAN SELECT {_quote_identifier(column, 'column')} FROM "
           f"{_quote_identifier(table_name, 'datasetId')} WHERE "
           f"{_quote_identifier(column, 'column')} IS NOT NULL GROUP BY "
           f"{_quote_identifier(column, 'column')} LIMIT 1")
    try:
        rows = engine.execute(sql, timeout_seconds=5)
    except Exception as exc:
        return {"kind": "index_explain", "readOnly": True, "datasetId": table_name,
                "column": column, "engine": engine.engine_name, "supported": False,
                "error": type(exc).__name__}
    details = [str(row.get("detail") or row.get("explain_value") or row)[:500] for row in rows]
    return {"kind": "index_explain", "readOnly": True, "datasetId": table_name,
            "column": column, "engine": engine.engine_name, "supported": True,
            "plan": details, "usesIndex": any("INDEX" in item.upper() for item in details)}


def _index_name(dataset_id: str, column: str) -> str:
    digest = hashlib.sha1(f"{dataset_id}\x00{column}".encode("utf-8")).hexdigest()[:12]
    return f"idx_{digest}"


def _validate_approval(approval: Any) -> dict[str, Any]:
    """Require explicit admin/editor booleans; no truthy-string shortcuts."""
    if not isinstance(approval, Mapping):
        raise IndexApprovalError("approval must be an object with adminApproval/editorApproval")
    admin = approval.get("adminApproval", approval.get("admin"))
    editor = approval.get("editorApproval", approval.get("editor"))
    if admin is not True or editor is not True:
        raise IndexApprovalError("approval requires adminApproval=true and editorApproval=true")
    normalized: dict[str, Any] = {"adminApproval": True, "editorApproval": True}
    for key in ("adminActor", "editorActor"):
        value = approval.get(key)
        if value is not None:
            if not isinstance(value, str) or not value.strip() or len(value) > _MAX_ACTOR \
                    or any(ord(ch) < 0x20 for ch in value):
                raise IndexApprovalError(f"{key} is invalid")
            normalized[key] = value.strip()
    comment = approval.get("comment")
    if comment is not None:
        if not isinstance(comment, str) or len(comment) > _MAX_COMMENT \
                or any(ord(ch) < 0x20 for ch in comment):
            raise IndexApprovalError("approval.comment is invalid")
        normalized["comment"] = comment
    return normalized


def _proposal_id(candidate: Mapping[str, Any], fence: Mapping[str, Any]) -> str:
    return "index-proposal-" + hashlib.sha256(
        _canonical({"candidate": candidate, "fence": fence, "operation": "create_index"}).encode("utf-8")
    ).hexdigest()[:32]


class IndexAdvisor:
    """Local proposal store and fenced SQLite index applier."""

    def __init__(self, engine: Engine, storage_dir: Path | str | None = None):
        self.engine = engine
        self.storage_dir = Path(storage_dir).expanduser() if storage_dir else None
        self._lock = threading.RLock()

    def _path(self, proposal_id: str) -> Path:
        if not _PROPOSAL_RE.fullmatch(proposal_id):
            raise IndexProposalNotFound("proposalId 格式无效")
        if self.storage_dir is None:
            raise IndexProposalNotFound("proposal store is not configured")
        return self.storage_dir / f"{proposal_id}.json"

    def _read(self, proposal_id: str) -> dict[str, Any]:
        path = self._path(proposal_id)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise IndexProposalNotFound("proposal 不存在或已损坏") from exc
        if not isinstance(value, dict) or value.get("proposalId") != proposal_id:
            raise IndexProposalNotFound("proposal 不存在或已损坏")
        return value

    def _write(self, proposal: Mapping[str, Any]) -> None:
        if self.storage_dir is None:
            raise IndexAdvisorError("proposal store is not configured")
        self.storage_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self._path(str(proposal.get("proposalId") or ""))
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(_canonical(dict(proposal)), encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise IndexAdvisorError(f"proposal 持久化失败：{exc}") from exc

    def _candidate(self, dataset_id: str, column: str, role: str | None,
                   min_observations: int) -> dict[str, Any]:
        if isinstance(min_observations, bool):
            raise IndexAdvisorError("minObservations must be an integer")
        try:
            threshold = int(min_observations)
        except (TypeError, ValueError) as exc:
            raise IndexAdvisorError("minObservations must be an integer") from exc
        if threshold < 1 or threshold > MAX_OBSERVATIONS:
            raise IndexAdvisorError("minObservations is outside the bounded range")
        if role is not None and (not isinstance(role, str) or role not in {"filter", "group"}):
            raise IndexAdvisorError("role must be filter or group")
        if not _valid_ident(dataset_id) or not _valid_ident(column):
            raise IndexAdvisorError("datasetId and column are required")
        info = self.engine.table_info(dataset_id)
        if info is None or column not in info.columns:
            raise IndexAdvisorError("unknown dataset or column")
        candidates = [item for item in suggestions_for_engine(
            self.engine, dataset_id, min_observations=threshold).get("suggestions", [])
                     if item.get("datasetId") == dataset_id and item.get("column") == column]
        if role is not None:
            candidates = [item for item in candidates if item.get("role") == role]
        if not candidates:
            raise IndexAdvisorError("column is not an observed bounded semantic filter/group query shape")
        if len(candidates) > 1:
            raise IndexAdvisorError("column has multiple semantic roles; specify role explicitly")
        return copy.deepcopy(candidates[0])

    def _build(self, dataset_id: str, column: str, role: str | None,
               min_observations: int) -> dict[str, Any]:
        candidate = self._candidate(dataset_id, column, role, min_observations)
        if _engine_name(self.engine) not in SUPPORTED_APPLY_ENGINES:
            raise IndexAdvisorError("当前引擎不支持安全索引应用；不会伪装成 SQLite")
        fence = current_fence(self.engine, dataset_id, column)
        if _has_single_column_index(fence["indexState"], column):
            raise IndexProposalConflict("column is already covered by an existing index")
        proposal_id = _proposal_id(candidate, fence)
        return {
            "kind": "index_proposal", "proposalId": proposal_id,
            "status": "pending", "readOnlySuggestion": True,
            "requiresApproval": True, "operation": "create_index",
            "datasetId": dataset_id, "column": column,
            "indexName": _index_name(dataset_id, column),
            "engine": self.engine.engine_name, "candidate": candidate,
            "fence": fence, "approvals": {"adminApproval": False, "editorApproval": False},
            "createdAt": time.time(),
        }

    def preview(self, dataset_id: str, column: str, *, role: str | None = None,
                min_observations: int = 2) -> dict[str, Any]:
        # No proposal directory is created and no engine mutation is possible.
        return self._build(dataset_id, column, role, min_observations)

    def propose(self, dataset_id: str, column: str, *, role: str | None = None,
                min_observations: int = 2) -> dict[str, Any]:
        with self._lock:
            proposal = self._build(dataset_id, column, role, min_observations)
            if self.storage_dir is None:
                raise IndexAdvisorError("proposal store is not configured")
            path = self._path(proposal["proposalId"])
            if path.exists():
                return self._read(proposal["proposalId"])
            # Capacity is file-count bounded; old applied proposals are not
            # deleted automatically because that could break idempotent replay.
            existing = list(self.storage_dir.glob("index-proposal-*.json")) if self.storage_dir.exists() else []
            if len(existing) >= MAX_PROPOSALS:
                raise IndexAdvisorError("proposal store capacity reached")
            self._write(proposal)
            return proposal

    def get(self, proposal_id: str) -> dict[str, Any]:
        with self._lock:
            return self._read(proposal_id)

    def approve(self, proposal_id: str, approval: Any) -> dict[str, Any]:
        normalized = _validate_approval(approval)
        with self._lock:
            proposal = self._read(proposal_id)
            if proposal.get("status") == "applied":
                return proposal
            proposal["approvals"] = normalized
            proposal["status"] = "approved"
            proposal["approvedAt"] = time.time()
            self._write(proposal)
            return proposal

    def _check_fence(self, proposal: Mapping[str, Any]) -> tuple[list[dict[str, Any]], str]:
        dataset_id = proposal.get("datasetId")
        column = proposal.get("column")
        if not isinstance(dataset_id, str) or not isinstance(column, str):
            raise IndexProposalConflict("proposal fence is malformed")
        if proposal.get("engine") != _engine_name(self.engine):
            raise IndexProposalConflict("engine changed; regenerate the index proposal")
        current = current_fence(self.engine, dataset_id, column)
        fence = proposal.get("fence")
        if not isinstance(fence, Mapping) or _canonical(current) != _canonical(fence):
            raise IndexProposalConflict("dataset/column/engine/index state changed; regenerate proposal")
        candidate = proposal.get("candidate")
        if not isinstance(candidate, Mapping):
            raise IndexProposalConflict("proposal candidate is missing")
        try:
            observed = self._candidate(dataset_id, column, candidate.get("role"),
                                       max(1, int(candidate.get("observations", 1))))
        except IndexAdvisorError as exc:
            raise IndexProposalConflict("semantic query shape changed; regenerate proposal") from exc
        # Candidate equality covers role, observed shape ids and counts; no raw
        # filter values can enter this comparison because the ledger excludes them.
        if _canonical(observed) != _canonical(candidate):
            raise IndexProposalConflict("semantic query shape changed; regenerate proposal")
        return current["indexState"], current["indexStateHash"]

    def apply(self, proposal_id: str, *, approval: Any = None,
              dry_run: bool = False,
              persist_index: Callable[[str, str], Any] | None = None) -> dict[str, Any]:
        with self._lock:
            proposal = self._read(proposal_id)
            # Idempotent replay is safe only after the prior durable status was
            # written.  It performs no second DDL and accepts no new fence.
            if proposal.get("status") == "applied":
                return {**proposal, "alreadyApplied": True, "applied": True}
            indexes, _ = self._check_fence(proposal)
            if dry_run:
                return {**proposal, "dryRun": True, "wouldApply": True,
                        "readOnly": True, "currentIndexState": indexes}
            normalized = _validate_approval(approval)
            stored = proposal.get("approvals")
            if not isinstance(stored, Mapping) or stored.get("adminApproval") is not True \
                    or stored.get("editorApproval") is not True:
                raise IndexApprovalError("proposal 尚未同时获得 admin/editor approval")
            if proposal.get("operation") != "create_index" or _engine_name(self.engine) != "sqlite":
                raise IndexAdvisorError("only SQLite create_index is supported")
            index_name = proposal.get("indexName")
            dataset_id = proposal.get("datasetId")
            column = proposal.get("column")
            if not all(isinstance(value, str) for value in (index_name, dataset_id, column)):
                raise IndexProposalConflict("proposal operation is malformed")
            if any(item.get("name") == index_name for item in indexes):
                raise IndexProposalConflict("target index appeared after preview; regenerate proposal")
            table = _quote_identifier(dataset_id, "datasetId")
            col = _quote_identifier(column, "column")
            idx = _quote_identifier(index_name, "indexName")
            created = False
            try:
                self.engine.execute_mutation(f"CREATE INDEX {idx} ON {table} ({col})")
                created = True
                after_indexes, after_hash = _sqlite_index_state(self.engine, dataset_id)
                if not any(item.get("name") == index_name and item.get("columns") == [column]
                           for item in after_indexes):
                    raise IndexAdvisorError("engine did not verify the requested index")
                if persist_index is not None:
                    persist_index(dataset_id, column)
                proposal = dict(proposal)
                proposal.update({"status": "applied", "approval": normalized,
                                 "appliedAt": time.time(), "applied": True,
                                 "appliedIndexStateHash": after_hash})
                self._write(proposal)
                return proposal
            except Exception:
                # Never leave an untracked DDL behind if persistence/verification
                # fails.  Rollback is deterministic and itself fail-closed.
                if created:
                    try:
                        self.engine.execute_mutation(f"DROP INDEX IF EXISTS {idx}")
                    except Exception as rollback_exc:
                        raise IndexAdvisorError(
                            f"index apply failed and rollback failed: {type(rollback_exc).__name__}")
                raise


__all__ = ["IndexAdvisor", "IndexAdvisorError", "IndexProposalConflict",
           "IndexProposalNotFound", "IndexApprovalError", "record_query_shape",
           "semantic_query_shape", "suggestions_for_engine", "explain_candidate"]
