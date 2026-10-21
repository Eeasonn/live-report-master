"""Server-side validation/diff for chart proposals.

This is the safe half of proposal transactions: it proves the target card,
dataset and semantic fields before a browser previews anything.  It does not
write a manifest; persistence remains behind the existing edit-password save
gate until a future transaction endpoint can provide a real snapshot/commit.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

from .agent_tools import ToolContractError
from .agent_extensions import confirm_alias, normalize_aliases, normalize_alias_key


_VIEWS = {"bar", "stackedBar", "line", "area"}
_BUCKETS = {"raw", "day", "week", "month", "quarter", "year"}
_INTENSIVE_TYPES = {"price", "percentage", "percentagechange", "temperature",
                    "sentiment", "correlation", "score", "rating"}
_AGGREGATIONS = {"sum", "average", "count", "min", "max", "derived"}


def _chart(entry: Mapping[str, Any], chart_id: str) -> Mapping[str, Any]:
    for chart in (entry.get("manifest") or {}).get("charts") or []:
        if isinstance(chart, Mapping) and chart.get("id") == chart_id:
            return chart
    raise ToolContractError(f"chart {chart_id!r} is not available")


def _dataset(entry: Mapping[str, Any], dataset_id: str) -> Mapping[str, Any]:
    for dataset in (entry.get("manifest") or {}).get("datasets") or []:
        if isinstance(dataset, Mapping) and dataset.get("id") == dataset_id:
            return dataset
    raise ToolContractError(f"dataset {dataset_id!r} is not available")


def _chart_data(chart: Mapping[str, Any]) -> Mapping[str, Any]:
    editor = chart.get("editor")
    data = editor.get("data") if isinstance(editor, Mapping) else None
    return data if isinstance(data, Mapping) else {}


def _string_field_set(value: Any) -> set[str]:
    if isinstance(value, str) and value:
        return {value}
    if isinstance(value, list):
        return {item for item in value if isinstance(item, str) and item}
    return set()


def _names(dataset: Mapping[str, Any]) -> set[str]:
    out = set()
    raw_columns = dataset.get("columns")
    for column in raw_columns if isinstance(raw_columns, list) else []:
        name = column.get("name") if isinstance(column, Mapping) else column
        if isinstance(name, str):
            out.add(name)
    derived = dataset.get("derived") or {}
    if isinstance(derived, Mapping):
        raw_measures = derived.get("measures")
        out.update(item.get("name") for item in (raw_measures if isinstance(raw_measures, list) else [])
                   if isinstance(item, Mapping) and isinstance(item.get("name"), str))
    return out


def _column_roles(dataset: Mapping[str, Any]) -> dict[str, str]:
    raw_roles = dataset.get("columnRoles")
    roles = {str(name): str(role) for name, role in raw_roles.items()} \
        if isinstance(raw_roles, Mapping) else {}
    raw_columns = dataset.get("columns")
    for column in raw_columns if isinstance(raw_columns, list) else []:
        if not isinstance(column, Mapping) or not isinstance(column.get("name"), str):
            continue
        roles.setdefault(column["name"], str(column.get("type") or column.get("role") or ""))
    return roles


def _effective_measure_aggregation(dataset: Mapping[str, Any], data: Mapping[str, Any],
                                    measure: str, requested: Any) -> str:
    """Resolve the proposal's visible aggregation using Flint measure semantics."""
    derived = dataset.get("derived") or {}
    raw_measures = derived.get("measures") if isinstance(derived, Mapping) else None
    derived_names = {item.get("name") for item in (raw_measures if isinstance(raw_measures, list) else [])
                     if isinstance(item, Mapping)} if isinstance(derived, Mapping) else set()
    if measure in derived_names:
        return "derived"
    types = dataset.get("measureTypes") or {}
    declared = types.get(measure) if isinstance(types, Mapping) else None
    semantic = declared.get("type", declared.get("semantic")) if isinstance(declared, Mapping) else declared
    semantic_key = str(semantic or "").replace("_", "").replace("-", "").lower()
    current = data.get("aggregations") if isinstance(data.get("aggregations"), Mapping) else {}
    aggregation = str(requested or current.get(measure) or
                      (declared.get("aggregation") if isinstance(declared, Mapping) else None) or
                      ("average" if semantic_key in _INTENSIVE_TYPES else "sum"))
    if aggregation not in _AGGREGATIONS - {"derived"}:
        raise ToolContractError("proposal aggregation is unsupported")
    # Flint's registry marks Price/Percentage/Temperature/etc. as intensive;
    # ``refs/flint/src/core/type-registry.ts:104-122`` therefore forbids a
    # proposal from silently turning them into SUM.
    if aggregation == "sum" and semantic_key in _INTENSIVE_TYPES:
        raise ToolContractError(f"字段「{measure}」的 Flint 语义为 intensive，不能使用 sum；请改用 average")
    return aggregation


def validate_alias_confirmation(entry: Mapping[str, Any], proposal: Mapping[str, Any]) -> dict[str, Any]:
    """Validate an explicit inferred-alias confirmation as a proposal only."""
    if not isinstance(proposal, Mapping) or proposal.get("kind") != "alias_confirmation":
        raise ToolContractError("alias confirmation proposal kind is invalid")
    chart_id = str(proposal.get("chartId") or "")
    chart = _chart(entry, chart_id)
    data = _chart_data(chart)
    dataset_id = str(data.get("datasetId") or "")
    dataset = _dataset(entry, dataset_id)
    alias = proposal.get("alias", proposal.get("name"))
    canonical = proposal.get("canonical")
    if not isinstance(alias, str) or not isinstance(canonical, str) or not alias.strip() or not canonical.strip():
        raise ToolContractError("alias confirmation requires bounded alias and canonical field")
    raw_columns = dataset.get("columns")
    columns = [str(item.get("name") if isinstance(item, Mapping) else item)
               for item in (raw_columns if isinstance(raw_columns, list) else [])]
    privacy = dataset.get("privacy") or {}
    raw_sensitive = privacy.get("sensitiveColumns") if isinstance(privacy, Mapping) else None
    sensitive = {item for item in (raw_sensitive if isinstance(raw_sensitive, list) else [])
                 if isinstance(item, str)}
    manifest = entry.get("manifest") or {}
    manifest_aliases = {}
    if isinstance(manifest.get("fieldAliases"), Mapping):
        manifest_aliases.update(manifest["fieldAliases"])
    if isinstance(dataset.get("aliases"), Mapping):
        for key, value in dataset["aliases"].items():
            manifest_aliases.setdefault(key, value)
    # Flint keeps field identity separate from channel/display decisions
    # (``refs/flint/src/core/field-semantics.ts:138-170,212-227``), so
    # ``columnAliases`` is the editor's display-label map
    # (``canonical -> display label``), not the durable AI resolver catalog.
    # Keep it as an input for backwards compatibility, but persist an
    # explicit confirmation in ``dataset.aliases`` so a list of resolver
    # aliases can never corrupt the existing display-label contract.
    authored_aliases: dict[str, list[Any]] = {}
    for source in (dataset.get("columnAliases"), dataset.get("aliases")):
        if not isinstance(source, Mapping):
            continue
        for key, value in source.items():
            values = value if isinstance(value, list) else [value]
            authored_aliases.setdefault(str(key), []).extend(values)
    resolution = normalize_aliases(
        columns,
        authored=authored_aliases,
        manifest=manifest_aliases,
        inferred=[{"canonical": canonical, "alias": alias, "confidence": 1.0}],
        sensitive_columns=sensitive,
    )
    alias_key = normalize_alias_key(alias)
    blocked = next((issue for issue in resolution.issues
                    if issue.alias and normalize_alias_key(issue.alias) == alias_key), None)
    if blocked is not None:
        raise ToolContractError(f"alias candidate blocked: {blocked.code} {blocked.detail}")
    try:
        confirmed = confirm_alias(resolution, alias, canonical=canonical)
    except ValueError as exc:
        raise ToolContractError(str(exc)) from None
    before_map = dict(dataset.get("aliases") or {}) if isinstance(dataset.get("aliases"), Mapping) else {}
    after_map = {str(key): (list(value) if isinstance(value, list) else [value])
                 for key, value in before_map.items()}
    target = next(item for item in confirmed.accepted if item.alias_key == normalize_alias_key(alias))
    after_map.setdefault(target.canonical, [])
    if target.alias not in after_map[target.canonical]:
        after_map[target.canonical].append(target.alias)
    after_map = {key: sorted(set(values), key=str.casefold)
                 for key, values in sorted(after_map.items(), key=lambda item: str(item[0]))}
    before = {"datasetId": dataset_id, "aliases": before_map}
    after = {"datasetId": dataset_id, "aliases": after_map}
    diff = {"aliases": {"before": before_map, "after": after_map}}
    affected = []
    raw_charts = manifest.get("charts")
    for item in raw_charts if isinstance(raw_charts, list) else []:
        if not isinstance(item, Mapping) or item.get("id") == chart_id:
            continue
        item_data = _chart_data(item)
        fields = _string_field_set(item_data.get("measures")) | _string_field_set(item_data.get("dimensions"))
        if isinstance(item_data.get("dimension"), str) and item_data.get("dimension"):
            fields.add(item_data["dimension"])
        if canonical in fields or alias in fields:
            affected.append({"chartId": item.get("id"), "title": item.get("title") or item.get("id"),
                             "sharedFields": [canonical]})
    canonical_payload = {"kind": "alias_confirmation", "chartId": chart_id,
                         "datasetId": dataset_id, "diff": diff}
    proposal_id = hashlib.sha256(json.dumps(canonical_payload, ensure_ascii=False,
                                             sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"valid": True, "kind": "alias_confirmation", "proposalId": proposal_id,
            "requiresApproval": True, "before": before, "after": after, "diff": diff,
            "impact": {"targetChartId": chart_id, "siblingCharts": affected,
                       "affectedChartCount": len(affected) + 1},
            "warnings": ["确认后别名只写入当前数据集的 authored aliases，不会自动学习其他报告。"]}


def _apply_alias_confirmation(manifest: dict[str, Any], proposal: Mapping[str, Any]) -> None:
    dataset_id = str(proposal.get("datasetId") or "")
    alias = str(proposal.get("alias") or proposal.get("name") or "")
    canonical = str(proposal.get("canonical") or "")
    dataset = next((item for item in manifest.get("datasets") or []
                    if isinstance(item, dict) and item.get("id") == dataset_id), None)
    if dataset is None:
        raise ToolContractError(f"dataset {dataset_id!r} is not available")
    mapping = dataset.setdefault("aliases", {})
    if not isinstance(mapping, dict):
        raise ToolContractError("dataset columnAliases must be an object")
    values = mapping.get(canonical, [])
    values = list(values) if isinstance(values, list) else [values]
    if alias not in values:
        values.append(alias)
    mapping[canonical] = sorted(set(values), key=str.casefold)
    dataset["aliases"] = {key: mapping[key] for key in sorted(mapping, key=str)}


def validate_proposal(entry: Mapping[str, Any], proposal: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(proposal, Mapping):
        raise ToolContractError("proposal must be an object")
    if proposal.get("kind") == "alias_confirmation":
        return validate_alias_confirmation(entry, proposal)
    chart_id = proposal.get("chartId")
    chart = _chart(entry, str(chart_id or ""))
    data = _chart_data(chart)
    dataset_id = str(proposal.get("datasetId") or data.get("datasetId") or "")
    if dataset_id != data.get("datasetId"):
        raise ToolContractError("proposal cannot move a chart to another dataset")
    dataset = _dataset(entry, dataset_id)
    names = _names(dataset)
    roles = _column_roles(dataset)
    dimension = proposal.get("dimension")
    measure = proposal.get("measure")
    raw_breakdown = proposal.get("breakdown")
    if raw_breakdown not in (None, "") and not isinstance(raw_breakdown, str):
        raise ToolContractError("proposal breakdown must be a string")
    breakdown = raw_breakdown or ""
    if not isinstance(dimension, str) or not dimension:
        raise ToolContractError("proposal dimension must be a non-empty string")
    if not isinstance(measure, str) or not measure:
        raise ToolContractError("proposal measure must be a non-empty string")
    if dimension not in names or measure not in names or (breakdown and breakdown not in names):
        raise ToolContractError("proposal contains an unknown semantic field")
    # Align proposal validation with Flint's measure role/type registry
    # (``refs/flint/src/core/type-registry.ts:104-122``): a
    # dimension may be temporal or categorical, but a displayed measure must
    # be a declared numeric/measure field or an aggregate-level derived node.
    # Never let a syntactically valid string column produce an empty chart.
    derived_names = {item.get("name") for item in (dataset.get("derived") or {}).get("measures") or []
                     if isinstance(item, Mapping) and isinstance(item.get("name"), str)} \
        if isinstance(dataset.get("derived"), Mapping) else set()
    measure_role = roles.get(str(measure), "").replace("_", "").lower()
    if measure not in derived_names and measure_role not in {"measure", "number", "numeric", "quantitative"}:
        raise ToolContractError("proposal measure must be a declared numeric/measure field")
    aggregation = _effective_measure_aggregation(dataset, data, str(measure), proposal.get("aggregation"))
    if breakdown and breakdown == dimension:
        raise ToolContractError("breakdown must differ from dimension")
    view_type = proposal.get("viewType") or chart.get("viewType") or "bar"
    if view_type not in _VIEWS:
        raise ToolContractError("unsupported proposal viewType")
    bucket = proposal.get("bucket") or "raw"
    if bucket not in _BUCKETS:
        raise ToolContractError("unsupported proposal bucket")
    try:
        top_n = int(proposal.get("topN") or 0)
    except (TypeError, ValueError) as exc:
        raise ToolContractError("proposal topN must be an integer") from exc
    if top_n < 0 or top_n > 20 or (top_n and not breakdown):
        raise ToolContractError("proposal TOP-N must be 1-20 and require a breakdown")
    before = {
        "chartId": chart_id, "datasetId": data.get("datasetId"),
        "dimension": data.get("dimension"), "dimensions": list(data.get("dimensions")) if isinstance(data.get("dimensions"), list) else [],
        "measures": list(data.get("measures")) if isinstance(data.get("measures"), list) else [],
        "aggregations": dict(data.get("aggregations") or {}) if isinstance(data.get("aggregations"), Mapping) else {},
        "breakdown": ((data.get("roles") or {}).get("breakdown") or "")
                     if isinstance(data.get("roles"), Mapping) else "",
        "dimensionBucket": data.get("dimensionBucket"),
        "viewType": chart.get("viewType"),
        "topN": int(data.get("breakdownTopN") or 0) if isinstance(data.get("breakdownTopN"), (int, float, str)) else 0,
        "filter": data.get("filter") or [],
    }
    after = dict(before)
    after.update({"dimension": dimension, "dimensions": [dimension], "measures": [measure],
                  "aggregations": {**(before.get("aggregations") or {}), measure: aggregation},
                  "breakdown": breakdown, "viewType": view_type, "topN": top_n,
                  "dimensionBucket": ({"source": dimension, "bucketed": dimension, "bucket": bucket}
                                       if bucket != "raw" else None)})
    diff = {key: {"before": before.get(key), "after": after.get(key)}
            for key in after if before.get(key) != after.get(key)}
    # Report-level impact is an advisory graph, not an implicit multi-card
    # mutation.  Surface sibling cards that read the same dataset and any
    # field changed by this proposal so approval can see the blast radius.
    changed_fields = {str(value) for value in (dimension, measure, breakdown) if value}
    impacted = []
    raw_charts = (entry.get("manifest") or {}).get("charts")
    for sibling in raw_charts if isinstance(raw_charts, list) else []:
        if not isinstance(sibling, Mapping) or sibling.get("id") == chart_id:
            continue
        sibling_data = _chart_data(sibling)
        if sibling_data.get("datasetId") != dataset_id:
            continue
        sibling_fields = _string_field_set(sibling_data.get("measures"))
        sibling_fields.update(_string_field_set(sibling_data.get("dimensions")))
        if isinstance(sibling_data.get("dimension"), str) and sibling_data.get("dimension"):
            sibling_fields.add(sibling_data["dimension"])
        sibling_roles = sibling_data.get("roles")
        sibling_breakdown = sibling_roles.get("breakdown") if isinstance(sibling_roles, Mapping) else None
        if isinstance(sibling_breakdown, str) and sibling_breakdown:
            sibling_fields.add(sibling_breakdown)
        if changed_fields & sibling_fields:
            impacted.append({"chartId": sibling.get("id"),
                             "title": sibling.get("title") or sibling.get("id"),
                             "sharedFields": sorted(changed_fields & sibling_fields)})
    impact = {"targetChartId": chart_id, "siblingCharts": impacted,
              "affectedChartCount": len(impacted) + 1}
    canonical = {"chartId": chart_id, "datasetId": dataset_id, "diff": diff}
    proposal_id = hashlib.sha256(json.dumps(canonical, ensure_ascii=False,
                                             sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"valid": True, "proposalId": proposal_id, "requiresApproval": True,
            "before": before, "after": after, "diff": diff,
            "impact": impact,
            "warnings": (["TOP-N 成员在服务端聚合后筛选；请核对被截断提示。"] if top_n else [])}


def apply_proposals(manifest: Mapping[str, Any], proposals: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Apply already-validated chart proposals to a manifest copy only."""
    out = json.loads(json.dumps(manifest, ensure_ascii=False))
    seen: set[str] = set()
    for proposal in proposals:
        if proposal.get("kind") == "alias_confirmation":
            _apply_alias_confirmation(out, proposal)
            continue
        chart_id = str(proposal.get("chartId") or "")
        if chart_id in seen:
            raise ToolContractError(f"duplicate proposal chart: {chart_id}")
        seen.add(chart_id)
        chart = next((item for item in out.get("charts") or []
                      if isinstance(item, dict) and item.get("id") == chart_id), None)
        if chart is None:
            raise ToolContractError(f"chart {chart_id!r} is not available")
        data = ((chart.setdefault("editor", {})).setdefault("data", {}))
        data["dimension"] = proposal.get("dimension")
        data["dimensions"] = [proposal.get("dimension")]
        data["measures"] = [proposal.get("measure")]
        # Keep the aggregation explicit on the chart so applying a proposal
        # cannot reintroduce a dataset default that conflicts with the Flint
        # measure type (especially intensive Price/Percentage fields).
        aggregation = proposal.get("aggregation")
        if aggregation:
            aggregations = dict(data.get("aggregations") or {})
            aggregations[str(proposal.get("measure"))] = aggregation
            data["aggregations"] = aggregations
        roles = dict(data.get("roles") or {})
        breakdown = proposal.get("breakdown") or ""
        if breakdown:
            roles["breakdown"] = breakdown
        else:
            roles.pop("breakdown", None)
        data["roles"] = roles
        bucket = proposal.get("bucket") or "raw"
        if bucket == "raw":
            data.pop("dimensionBucket", None)
        else:
            data["dimensionBucket"] = {"source": proposal.get("dimension"),
                                        "bucketed": proposal.get("dimension"), "bucket": bucket}
        data["breakdownTopN"] = int(proposal.get("topN") or 0)
        if proposal.get("allowedValues"):
            data["allowedValues"] = list(proposal["allowedValues"])
        else:
            data.pop("allowedValues", None)
        chart["viewType"] = proposal.get("viewType") or chart.get("viewType") or "bar"
        if proposal.get("derivedMeasures"):
            data["derivedMeasures"] = json.loads(json.dumps(proposal["derivedMeasures"], ensure_ascii=False))
    return out


def manifest_hash(manifest: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def list_snapshot_metadata(state_home: Any, report_id: str, *, limit: int = 32,
                           current_hash: str | None = None) -> list[dict[str, Any]]:
    """List bounded, metadata-only proposal history without exposing manifests."""
    try:
        cap = max(1, min(int(limit), 64))
    except (TypeError, ValueError):
        cap = 32
    root = __import__("pathlib").Path(state_home) / "copilot-snapshots"
    rows: list[dict[str, Any]] = []
    raw_rows: list[dict[str, Any]] = []
    if not root.is_dir():
        return rows
    for path in sorted(root.glob("proposal-????????????????.json"),
                       key=lambda item: item.stat().st_mtime if item.exists() else 0,
                       reverse=True)[:128]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(value, Mapping) or value.get("reportId") != report_id:
            continue
        snapshot_id = str(value.get("snapshotId") or "")
        if not re.fullmatch(r"proposal-[A-Fa-f0-9]{16}", snapshot_id):
            continue
        raw_rows.append({
            "snapshotId": snapshot_id,
            "createdAt": value.get("createdAt"),
            "beforeHash": value.get("beforeHash"),
            "afterHash": value.get("afterHash"),
            "proposalIds": list(value.get("proposalIds") or [])[:8],
            "canRedo": isinstance(value.get("afterEntry"), Mapping),
        })
    # A proposal can be undone one committed step at a time.  Link metadata
    # by hash only; snapshot manifests never leave this function.  The chain
    # length is bounded so a malformed/cyclic history cannot expand the UI.
    raw_rows.sort(key=lambda item: float(item.get("createdAt") or 0), reverse=True)
    after_to_id = {item["afterHash"]: item["snapshotId"] for item in raw_rows
                   if item.get("afterHash")}
    by_id = {item["snapshotId"]: item for item in raw_rows}
    for item in raw_rows:
        parent = after_to_id.get(item.get("beforeHash"))
        if parent and parent != item["snapshotId"]:
            item["previousSnapshotId"] = parent
    for item in raw_rows:
        parent = item.get("previousSnapshotId")
        depth = 1
        cursor = parent
        seen = {item["snapshotId"]}
        while cursor and cursor not in seen and depth < 32:
            seen.add(cursor)
            depth += 1
            cursor = by_id.get(cursor, {}).get("previousSnapshotId")
        item["step"] = depth
        if current_hash is not None:
            item["canRollback"] = item.get("afterHash") == current_hash
        rows.append(item)
        if len(rows) >= cap:
            break
    return rows


def validate_transaction_scope(entry: Mapping[str, Any], proposals: list[Mapping[str, Any]],
                               *, engine: Any = None) -> dict[str, Any]:
    """Validate the physical/report scope of a multi-card proposal atomically.

    Chart edits are manifest-only, but a transaction can touch cards backed by
    more than one dataset.  This helper makes that boundary explicit instead
    of letting an apply call silently mix an unavailable source.  If an engine
    is supplied, every source must be loaded before the snapshot is written;
    no data mutation is performed here.  This preserves Flint's caller-owned
    aggregation boundary (``refs/flint/src/core/aggregate.ts:7-20``) while
    making cross-source proposal validation observable and fail-closed.
    """
    if not isinstance(proposals, list) or not proposals or len(proposals) > 8:
        raise ToolContractError("transaction proposals must contain 1-8 items")
    manifest = entry.get("manifest") or {}
    datasets = {str(item.get("id")): item for item in manifest.get("datasets") or []
                if isinstance(item, Mapping) and item.get("id")}
    dataset_ids: set[str] = set()
    affected_charts: list[dict[str, Any]] = []
    seen_charts: set[str] = set()
    for proposal in proposals:
        if not isinstance(proposal, Mapping):
            raise ToolContractError("transaction proposal must be an object")
        chart_id = str(proposal.get("chartId") or "")
        if chart_id in seen_charts:
            raise ToolContractError(f"duplicate proposal chart: {chart_id}")
        seen_charts.add(chart_id)
        chart = _chart(entry, chart_id)
        data = ((chart.get("editor") or {}).get("data") or {})
        dataset_id = str(data.get("datasetId") or proposal.get("datasetId") or "")
        if dataset_id not in datasets:
            raise ToolContractError(f"transaction dataset {dataset_id!r} is not declared")
        if str(proposal.get("datasetId") or dataset_id) != dataset_id:
            raise ToolContractError("transaction cannot move a chart to another dataset")
        dataset_ids.add(dataset_id)
        changed_fields = [str(proposal.get(key)) for key in ("dimension", "measure", "breakdown")
                          if proposal.get(key)]
        # This is an impact preview only.  It deliberately reports the typed
        # chart/dataset scope and never infers a cross-dataset join or mutates
        # another card; Flint keeps aggregation semantics at the caller-owned
        # encoding boundary (refs/flint/src/core/aggregate.ts:7-20).
        affected_charts.append({
            "chartId": chart_id,
            "title": chart.get("title") or chart_id,
            "datasetId": dataset_id,
            "changedFields": list(dict.fromkeys(changed_fields))[:3],
        })
    sources = []
    for dataset_id in sorted(dataset_ids):
        dataset = datasets[dataset_id]
        if engine is not None and engine.table_info(dataset_id) is None:
            raise ToolContractError(f"transaction dataset {dataset_id!r} is not loaded")
        sources.append({"datasetId": dataset_id,
                        "label": dataset.get("label") or dataset_id,
                        "rowCount": dataset.get("rowCount"),
                        "dataAsOf": dataset.get("dataAsOf"),
                        "dataRevision": dataset.get("dataRevision")})
    return {"datasetIds": sorted(dataset_ids),
            "crossDataset": len(dataset_ids) > 1,
            "sourceCount": len(dataset_ids),
            "sources": sources,
            "affectedCharts": affected_charts[:8],
            "affectedChartCount": len(affected_charts),
            "engine": getattr(engine, "engine_name", None) if engine is not None else None,
            "engineRevision": getattr(engine, "revision", None) if engine is not None else None,
            "validated": True}


def validate_alias_transaction_scope(entry: Mapping[str, Any],
                                     proposals: list[Mapping[str, Any]],
                                     *, engine: Any = None) -> dict[str, Any]:
    """Validate the bounded source scope for alias-only manifest proposals.

    Alias confirmation changes report metadata, not rows.  It still has to
    prove that every referenced dataset is declared and loaded before the
    snapshot/commit path runs.  Keeping this separate from chart scope avoids
    pretending that an alias proposal has chart dimensions/measures while
    preserving the same fail-closed transaction boundary.
    """
    if not isinstance(proposals, list) or not proposals or len(proposals) > 8:
        raise ToolContractError("transaction proposals must contain 1-8 items")
    manifest = entry.get("manifest") or {}
    datasets = {str(item.get("id")): item for item in manifest.get("datasets") or []
                if isinstance(item, Mapping) and item.get("id")}
    dataset_ids: set[str] = set()
    affected_charts: list[dict[str, Any]] = []
    seen_charts: set[str] = set()
    for proposal in proposals:
        if not isinstance(proposal, Mapping) or proposal.get("kind") != "alias_confirmation":
            raise ToolContractError("alias transaction cannot mix chart proposals")
        chart_id = str(proposal.get("chartId") or "")
        if chart_id in seen_charts:
            raise ToolContractError(f"duplicate proposal chart: {chart_id}")
        seen_charts.add(chart_id)
        chart = _chart(entry, chart_id)
        data = ((chart.get("editor") or {}).get("data") or {})
        dataset_id = str(data.get("datasetId") or proposal.get("datasetId") or "")
        if dataset_id not in datasets:
            raise ToolContractError(f"transaction dataset {dataset_id!r} is not declared")
        if str(proposal.get("datasetId") or dataset_id) != dataset_id:
            raise ToolContractError("transaction cannot move a chart to another dataset")
        dataset_ids.add(dataset_id)
        affected_charts.append({"chartId": chart_id,
                                "title": chart.get("title") or chart_id,
                                "datasetId": dataset_id,
                                "changedFields": ["aliases"]})
    sources = []
    for dataset_id in sorted(dataset_ids):
        dataset = datasets[dataset_id]
        if engine is not None and engine.table_info(dataset_id) is None:
            raise ToolContractError(f"transaction dataset {dataset_id!r} is not loaded")
        sources.append({"datasetId": dataset_id,
                        "label": dataset.get("label") or dataset_id,
                        "rowCount": dataset.get("rowCount"),
                        "dataAsOf": dataset.get("dataAsOf"),
                        "dataRevision": dataset.get("dataRevision")})
    return {"datasetIds": sorted(dataset_ids),
            "crossDataset": len(dataset_ids) > 1,
            "sourceCount": len(dataset_ids),
            "sources": sources,
            "affectedCharts": affected_charts[:8],
            "affectedChartCount": len(affected_charts),
            "engine": getattr(engine, "engine_name", None) if engine is not None else None,
            "engineRevision": getattr(engine, "revision", None) if engine is not None else None,
            "metadataOnly": True,
            "validated": True}
