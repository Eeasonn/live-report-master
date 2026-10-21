"""Canonical, bounded report context exposed to Agent tools.

This module is deliberately a projection, not a second manifest model.  It
keeps report, chart, dataset, field roles, filters and time bucket together so
the model/tool layer cannot accidentally answer for a different card.  The
semantic fields originate in the compiled manifest, whose types and
aggregation rules are compiled from Flint field semantics.
"""
from __future__ import annotations

from typing import Any, Mapping

from .agent_extensions import normalize_alias_key, normalize_aliases


_TEMPORAL_TYPES = {
    "datetime", "date", "time", "timestamp", "year", "quarter", "month",
    "week", "day", "hour", "yearmonth", "yearquarter", "yearweek", "decade",
}
_TEMPORAL_ROLES = {"time", "temporal", "date", "datetime"}
_WINDOW_GRAINS = ("raw", "day", "week", "month", "quarter", "year")


def _calendar_summary(dataset: Mapping[str, Any]) -> dict[str, Any]:
    """Expose an explicit business calendar without making labels authoritative.

    The calendar is a domain overlay on Flint's temporal field/granule type;
    it is never inferred from a Chinese label or silently applied to a card.
    ``business_calendar`` delegates raw parsing to ``scripts/date_parse.py``.
    """
    try:
        from . import agent_extensions as business_calendar
    except ImportError:
        import sys
        from pathlib import Path
        skill_dir = str(Path(__file__).resolve().parent.parent)
        if skill_dir not in sys.path:
            sys.path.insert(0, skill_dir)
        from server import agent_extensions as business_calendar
    raw = dataset.get("calendar")
    return business_calendar.summary(raw)


def _alias_projection(manifest: Mapping[str, Any], dataset: Mapping[str, Any],
                      columns: list[str], derived_items: list[dict[str, Any]],
                      sensitive: set[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Project aliases through the S02/S09 confirmation seam.

    Authored/manifest aliases remain accepted.  Optional ``inferredAliases``
    stay visible as confirmation-required candidates, but a conflict or
    sensitive-field issue is removed from the resolver surface.  This keeps
    display aliases and AI field resolution on one fail-closed identity rule.
    """
    def merge_maps(*values: Any) -> dict[str, list[Any]]:
        merged: dict[str, list[Any]] = {}
        for value in values:
            if not isinstance(value, Mapping):
                continue
            for canonical, aliases in value.items():
                if not isinstance(canonical, str):
                    continue
                items = aliases if isinstance(aliases, list) else [aliases]
                merged.setdefault(canonical, []).extend(items)
        return merged

    authored = dataset.get("columnAliases")
    manifest_aliases = merge_maps(
        manifest.get("fieldAliases") if isinstance(manifest, Mapping) else None,
        dataset.get("aliases"),
    )
    inferred = dataset.get("inferredAliases")
    resolution = normalize_aliases(
        [*columns, *(str(item.get("name")) for item in derived_items
                     if isinstance(item, Mapping) and isinstance(item.get("name"), str))],
        authored=authored,
        manifest=manifest_aliases,
        inferred=inferred,
        sensitive_columns=sensitive,
    )
    blocked = {normalize_alias_key(issue.alias) for issue in resolution.issues if issue.alias}
    source_names = {"authored": "manifest_display_alias", "manifest": "manifest", "inferred": "inferred"}
    aliases = []
    for record in resolution.records:
        if record.alias_key in blocked:
            continue
        aliases.append({"alias": record.alias, "canonical": record.canonical,
                        "source": source_names.get(record.source, record.source),
                        "confidence": record.confidence,
                        "requiresConfirmation": record.requires_confirmation,
                        "confirmed": record.confirmed})
    aliases.sort(key=lambda item: (str(item["alias"]).casefold(), str(item["canonical"])))
    issues = [{"code": issue.code, "alias": issue.alias, "canonical": issue.canonical,
               "detail": issue.detail} for issue in resolution.issues[:24]]
    return aliases[:100], issues


def _columns(dataset: Mapping[str, Any]) -> list[str]:
    output: list[str] = []
    for item in dataset.get("columns") or []:
        name = item.get("name") if isinstance(item, Mapping) else item
        if isinstance(name, str) and name not in output:
            output.append(name)
    return output


def safe_derived_measures(dataset: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return aggregate-derived definitions whose dependency closure is safe.

    A derived name is not a physical column, but it can still be a privacy
    bypass when its formula/window references a sensitive physical field.  The
    Agent projection therefore hides the whole derived node (including its
    definition) whenever its transitive declared references intersect
    ``privacy.sensitiveColumns``.  This keeps the context and execution seam
    on the same policy boundary instead of redacting only the final name.
    """
    raw = dataset.get("derived")
    if not isinstance(raw, Mapping) or not isinstance(raw.get("measures"), list):
        return []
    items = [dict(item) for item in raw.get("measures") or []
             if isinstance(item, Mapping) and isinstance(item.get("name"), str)
             and item.get("name")]
    # Aggregate measures may depend on a materialized row-level derived
    # column. Include both node families in the closure so that the latter
    # cannot become a privacy bypass through an otherwise safe-looking ref.
    column_items = [dict(item) for item in raw.get("columns") or []
                    if isinstance(item, Mapping) and isinstance(item.get("name"), str)
                    and item.get("name")]
    by_name = {str(item["name"]): item for item in [*column_items, *items]}
    privacy = dataset.get("privacy") or {}
    sensitive = {str(name) for name in (privacy.get("sensitiveColumns") or [])
                 if isinstance(name, str)} if isinstance(privacy, Mapping) else set()
    memo: dict[str, set[str]] = {}

    def references(node: Mapping[str, Any], visiting: set[str]) -> set[str]:
        name = str(node.get("name") or "")
        if name in memo:
            return set(memo[name])
        if name in visiting:
            return set()
        next_visiting = set(visiting)
        next_visiting.add(name)
        found: set[str] = set()

        def walk(value: Any) -> None:
            if isinstance(value, str):
                if value in sensitive:
                    found.add(value)
                dependency = by_name.get(value)
                if dependency is not None and value != name:
                    found.update(references(dependency, next_visiting))
            elif isinstance(value, Mapping):
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(node)
        memo[name] = set(found)
        return found

    return [item for item in items if not references(item, set())]


def _temporal_fields(dataset: Mapping[str, Any], columns: list[str],
                     measure_types: Mapping[str, Any],
                     column_roles: Mapping[str, Any], sensitive: set[str]) -> list[str]:
    """Find temporal fields from the declared role/type, never from labels.

    Flint's semantic type lattice treats temporal/date-granule fields as an
    ordered semantic family (``refs/flint/src/core/semantic-types.ts:30-43,61-79``).
    Agent context must therefore advertise an explicit time binding rather than
    asking a model to infer it from a rendered axis or a Chinese field name.
    """
    output = []
    for name in columns[:200]:
        if name in sensitive:
            continue
        role = str(column_roles.get(name) or "").replace("_", "").lower()
        declared = measure_types.get(name) if isinstance(measure_types, Mapping) else None
        semantic = declared.get("type", declared.get("semantic")) if isinstance(declared, Mapping) else declared
        semantic_key = str(semantic or "").replace("_", "").replace("-", "").lower()
        if role in _TEMPORAL_ROLES or semantic_key in _TEMPORAL_TYPES:
            output.append(name)
    return output


def build_context(entry: Mapping[str, Any], chart_id: str = "") -> dict[str, Any]:
    manifest = entry.get("manifest") or {}
    charts = manifest.get("charts") or []
    chart = next((item for item in charts if isinstance(item, Mapping) and item.get("id") == chart_id), None)
    data = ((chart or {}).get("editor") or {}).get("data") or {}
    table_decl = (chart or {}).get("table") if isinstance((chart or {}).get("table"), Mapping) else {}
    dataset_id = data.get("datasetId")
    datasets = manifest.get("datasets") or []
    dataset = next((item for item in datasets if isinstance(item, Mapping) and item.get("id") == dataset_id), None)
    dataset = dataset or {}
    meta = manifest.get("meta") or {}
    dataset_revision = {"dataRevision": dataset.get("dataRevision"),
                        "rowCount": dataset.get("rowCount"),
                        "dataAsOf": dataset.get("dataAsOf") or (meta.get("dataAsOf") if isinstance(meta, Mapping) else None)}
    columns = _columns(dataset)
    measure_types = dataset.get("measureTypes") or manifest.get("measureTypes") or {}
    column_roles = dataset.get("columnRoles") or manifest.get("columnRoles") or {}
    privacy = dataset.get("privacy") or manifest.get("privacy") or {}
    sensitive = {str(name) for name in (privacy.get("sensitiveColumns") or [])
                 if isinstance(name, str)} if isinstance(privacy, Mapping) else set()
    derived_items = safe_derived_measures(dataset)
    derived_by_name = {str(item["name"]): item for item in derived_items}
    aliases, alias_issues = _alias_projection(manifest, dataset, columns, derived_items, sensitive)
    fields = []
    for name in columns[:200]:
        if name in sensitive:
            continue
        declared = measure_types.get(name) if isinstance(measure_types, Mapping) else None
        role = column_roles.get(name) if isinstance(column_roles, Mapping) else None
        fields.append({"name": name, "role": role,
                       "measureType": declared.get("type") if isinstance(declared, Mapping) else declared,
                       "aggregation": declared.get("aggregation") if isinstance(declared, Mapping) else None,
                       "sensitive": name in sensitive, "availableToAgent": name not in sensitive,
                       "source": "manifest", "confidence": 1.0,
                       "requiresConfirmation": False})
    for item in derived_items[:16]:
        name = str(item["name"])
        if name in {field["name"] for field in fields}:
            continue
        measure_type = item.get("measureType")
        fields.append({"name": name, "role": "measure", "measureType": (
            measure_type.get("type") if isinstance(measure_type, Mapping) else measure_type or "derived"),
            "aggregation": "derived", "derived": True,
            "sensitive": False, "availableToAgent": True,
            "source": "manifest", "confidence": 1.0,
            "requiresConfirmation": False})
    def bounded_filter(value: Any) -> Any:
        if isinstance(value, list):
            return value[:20]
        if isinstance(value, Mapping):
            return {key: list(value.get(key) or [])[:20]
                    for key in ("all", "any") if isinstance(value.get(key) or [], list)}
        return []
    global_filter = bounded_filter(manifest.get("filters"))
    card_filter = bounded_filter(data.get("filter"))
    if isinstance(global_filter, list):
        global_filter = [item for item in global_filter
                         if not (isinstance(item, Mapping) and item.get("column") in sensitive)]
    elif isinstance(global_filter, Mapping):
        global_filter = {key: [item for item in value
                               if not (isinstance(item, Mapping) and item.get("column") in sensitive)]
                         for key, value in global_filter.items()}
    safe_dimensions = [item for item in (data.get("dimensions") or [])[:6]
                       if isinstance(item, str) and item not in sensitive]
    known_measure_names = set(columns) | set(derived_by_name)
    safe_measures = [item for item in (data.get("measures") or [])[:12]
                     if isinstance(item, str) and item not in sensitive and item in known_measure_names]
    measure_semantics = []
    for name in safe_measures:
        if name in derived_by_name:
            declared_type = derived_by_name[name].get("measureType")
            measure_semantics.append({
                "name": name, "aggregation": "derived", "measureType": (
                    declared_type.get("type") if isinstance(declared_type, Mapping)
                    else declared_type or "derived"), "derived": True,
            })
            continue
        declared = measure_types.get(name) if isinstance(measure_types, Mapping) else None
        per_measure_aggregation = ((data.get("aggregations") or {}).get(name)
                                   if isinstance(data.get("aggregations"), Mapping) else None)
        declared_aggregation = (declared.get("aggregation")
                                if isinstance(declared, Mapping) else None)
        measure_semantics.append({
            "name": name,
            # Per-measure aggregation is the chart/runtime source of truth;
            # falling back to the chart-level value for every chip silently
            # misdescribes mixed measures (e.g. revenue=sum + price=average).
            # A chart-level per-measure override is the active channel
            # declaration; the dataset declaration is only the fallback.
            # Flint resolves aggregation from the channel encoding rather
            # than silently replacing an encoding with the field default
            # (refs/flint/src/core/aggregate.ts:34-54).  Reversing this order
            # makes a visible ``average`` card look like ``sum`` to Agent
            # tools whenever the dataset also carries a default aggregation.
            "aggregation": (per_measure_aggregation or declared_aggregation
                             or data.get("aggregation") or "sum"),
            "measureType": (declared.get("type") if isinstance(declared, Mapping) else declared),
        })
    temporal_fields = _temporal_fields(dataset, columns, measure_types, column_roles, sensitive)
    calendar = _calendar_summary(dataset)
    dimension_bucket = data.get("dimensionBucket") if isinstance(data.get("dimensionBucket"), Mapping) else {}
    raw_dimension = data.get("dimension")
    time_dimension = raw_dimension if isinstance(raw_dimension, str) and raw_dimension in temporal_fields else (
        temporal_fields[0] if temporal_fields else None)
    time_grain = dimension_bucket.get("bucket") if dimension_bucket else "raw"
    if time_grain not in _WINDOW_GRAINS:
        time_grain = "raw"
    window_bindings = []
    for item in derived_items:
        if not isinstance(item, Mapping) or item.get("kind") != "window":
            continue
        time_field = item.get("timeField")
        if not isinstance(time_field, str) or time_field in sensitive:
            continue
        window_bindings.append({
            "name": item.get("name"),
            "timeField": time_field,
            "grain": item.get("grain") or "raw",
            "fn": item.get("fn"),
            "offset": item.get("offset"),
            "requiresDimension": time_field,
            "requiresBucket": item.get("grain") or "raw",
        })
    hierarchy_edges = []
    for edge in (dataset.get("hierarchy") or [])[:24]:
        if not isinstance(edge, Mapping):
            continue
        parent, child = edge.get("parent"), edge.get("child")
        if (isinstance(parent, str) and isinstance(child, str)
                and parent not in sensitive and child not in sensitive):
            hierarchy_edges.append({"parent": parent, "child": child})
    hierarchy_order = [name for name in (dataset.get("hierarchyOrder") or [])[:24]
                       if isinstance(name, str) and name not in sensitive]
    # Flint pivot is a channel permutation over the same semantic fields, not
    # a new aggregation (``refs/flint/src/core/pivot.ts:7-19``).  Preserve the
    # authored row/column/value roles explicitly so an Agent can explain a
    # crosstab without guessing from rendered HTML or treating a value as a
    # dimension.  All lists stay bounded and sensitive fields are removed.
    roles = data.get("roles") if isinstance(data.get("roles"), Mapping) else {}
    raw_rows = data.get("rows") if isinstance(data.get("rows"), list) else (
        roles.get("rows") if isinstance(roles.get("rows"), list) else safe_dimensions[:1])
    raw_cols = data.get("cols") if isinstance(data.get("cols"), list) else (
        roles.get("cols") if isinstance(roles.get("cols"), list) else [])
    pivot_rows = [item for item in raw_rows[:6] if isinstance(item, str) and item not in sensitive]
    pivot_cols = [item for item in raw_cols[:6] if isinstance(item, str) and item not in sensitive]
    if isinstance(card_filter, list):
        card_filter = [item for item in card_filter
                       if not (isinstance(item, Mapping) and item.get("column") in sensitive)]
    elif isinstance(card_filter, Mapping):
        card_filter = {key: [item for item in value
                             if not (isinstance(item, Mapping) and item.get("column") in sensitive)]
                       for key, value in card_filter.items()}
    return {
        "reportId": manifest.get("reportId") or entry.get("reportId"),
        "title": manifest.get("title") or entry.get("title"),
        "chartId": chart_id or None,
        "chartTitle": (chart or {}).get("title"),
        "datasetId": dataset_id,
        "datasetRevision": dataset_revision,
        "fields": fields,
        "sensitiveFieldCount": len(sensitive),
        "dimension": raw_dimension if isinstance(raw_dimension, str) and raw_dimension not in sensitive else "",
        "dimensions": safe_dimensions,
        "measures": safe_measures,
        "measureSemantics": measure_semantics,
        "aggregation": data.get("aggregation") or (measure_semantics[0].get("aggregation")
                                                     if measure_semantics else None),
        "grain": time_grain,
        "timeSemantics": {
            "field": time_dimension,
            "isTemporal": time_dimension is not None,
            "availableFields": temporal_fields[:12],
            "grain": time_grain,
            "source": dimension_bucket.get("source") if dimension_bucket else None,
            "bucketed": dimension_bucket.get("bucketed") if dimension_bucket else None,
            "supportedWindowGrains": list(_WINDOW_GRAINS),
            "windowBindings": window_bindings[:16],
            "calendar": calendar,
            "bindingRule": "window 指标必须使用 requiresDimension 且分桶等于 requiresBucket",
        },
        "filter": card_filter,
        "globalFilters": global_filter,
        # Scope is part of the semantic contract: global/card predicates are
        # combined by the server with any request-local predicates as an outer
        # AND; an ``any`` list remains an OR group inside its own scope.  This
        # keeps the Agent from describing a local question as a report-wide
        # filter or flattening independent OR groups.
        "filterScopes": {"global": global_filter, "card": card_filter,
                          "local": [], "combine": "outer_and_scope_local_any"},
        "dimensionBucket": data.get("dimensionBucket"),
        "calendar": calendar,
        "pivot": {"layout": data.get("tableLayout") or data.get("layout") or table_decl.get("layout"),
                  "rows": pivot_rows, "columns": pivot_cols,
                  "values": [dict(item) for item in measure_semantics[:12]],
                  "dimensions": safe_dimensions,
                  "breakdown": (roles.get("breakdown")
                                if isinstance(roles.get("breakdown"), str)
                                and roles.get("breakdown") not in sensitive else "")},
        # Hierarchy is a semantic navigation constraint, not an instruction;
        # expose only bounded, non-sensitive edges so an Agent can request a
        # legal drill path (refs/flint/src/core/field-semantics.ts).
        "hierarchy": {"edges": hierarchy_edges, "order": hierarchy_order},
        "derivedMeasures": derived_items[:16],
        "aliases": aliases[:100],
        "aliasIssues": alias_issues,
        "semanticConflicts": (["dimensionBucket.source 与 bucketed 不一致"]
                               if isinstance(data.get("dimensionBucket"), Mapping)
                               and data["dimensionBucket"].get("source")
                               and data["dimensionBucket"].get("bucketed")
                               and data["dimensionBucket"].get("source") != data["dimensionBucket"].get("bucketed")
                               else []),
    }
