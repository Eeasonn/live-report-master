"""Server-owned read-only Agent tool execution.

The runner is deliberately narrow: it accepts semantic arguments, calls the
existing aggregator, and returns evidence. It never accepts SQL or raw ECharts
options. Write/notify tools remain proposal-only until an approval seam is
added.
"""
from __future__ import annotations

from typing import Any, Mapping

from .agent_analysis import (compare_periods, contribution, data_quality, detect_anomalies,
                             quality_diagnostics, rank_root_causes, rank_root_causes_multi, recommend_actions,
                             trend_inflections, what_if)
from .agent_context import build_context, safe_derived_measures
from .agent_tools import ToolContractError
from .aggregator import AggregationCancelled, AggregationError, aggregate, parse_request
import date_parse


def _dataset(entry: Mapping[str, Any], dataset_id: str) -> Mapping[str, Any]:
    for dataset in (entry.get("manifest") or {}).get("datasets") or []:
        if isinstance(dataset, Mapping) and dataset.get("id") == dataset_id:
            return dataset
    raise ToolContractError(f"dataset {dataset_id!r} is not available")


def _calendar_for(entry: Mapping[str, Any], args: Mapping[str, Any],
                  dataset: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Resolve an explicit calendar; never infer fiscal semantics from labels."""
    try:
        from . import agent_extensions as business_calendar
    except ImportError:
        import sys
        from pathlib import Path
        skill_dir = str(Path(__file__).resolve().parent.parent)
        if skill_dir not in sys.path:
            sys.path.insert(0, skill_dir)
        from server import agent_extensions as business_calendar
    dataset = dataset or _dataset(entry, str(args.get("datasetId") or ""))
    raw = args.get("calendar") if args.get("calendar") is not None else dataset.get("calendar")
    try:
        return business_calendar.normalize_calendar(raw)
    except (TypeError, ValueError) as exc:
        raise ToolContractError(f"calendar declaration is invalid: {exc}") from None


def _columns(dataset: Mapping[str, Any]) -> set[str]:
    output = set()
    for column in dataset.get("columns") or []:
        if isinstance(column, Mapping) and isinstance(column.get("name"), str):
            output.add(column["name"])
        elif isinstance(column, str):
            output.add(column)
    return output


def _sensitive(dataset: Mapping[str, Any], name: str) -> bool:
    privacy = dataset.get("privacy") or {}
    return isinstance(privacy, Mapping) and isinstance(name, str) and name in {
        item for item in (privacy.get("sensitiveColumns") or []) if isinstance(item, str)}


def _scoped_filters(entry: Mapping[str, Any], args: Mapping[str, Any]) -> Any:
    """Combine global report and selected-card filters before tool execution."""
    chart_id = args.get("chartId")
    context = build_context(entry, str(chart_id or "")) if chart_id else {}
    base = []
    for value in (context.get("globalFilters") or [], context.get("filter") or [], args.get("filter") or []):
        if isinstance(value, list):
            base.extend(value)
        elif isinstance(value, Mapping):
            base.extend(value.get("all") or [])
            # Each scope's OR group is a distinct predicate in the outer AND
            # list.  Flattening all scopes into one ``any`` group changes
            # (A OR B) AND (C OR D) into A OR B OR C OR D.
            if value.get("any"):
                base.append({"any": value.get("any")})
    return base


_INTENSIVE_SEMANTICS = {
    "price", "percentage", "percentagechange", "temperature", "sentiment",
    "correlation", "score", "rating",
}


def _aggregation_for(dataset: Mapping[str, Any], measure: str, requested: Any) -> str:
    declared = (dataset.get("measureTypes") or {}).get(measure)
    semantic = declared.get("semantic", declared.get("type")) if isinstance(declared, Mapping) else declared
    semantic_key = str(semantic or "").replace("_", "").replace("-", "").lower()
    default = declared.get("aggregation") if isinstance(declared, Mapping) else None
    aggregation = str(requested or default or ("average" if semantic_key in _INTENSIVE_SEMANTICS else "sum"))
    if aggregation not in {"sum", "average", "count", "min", "max"}:
        raise ToolContractError("unsupported aggregation")
    if aggregation == "sum" and semantic_key in _INTENSIVE_SEMANTICS:
        raise ToolContractError(
            f"字段「{measure}」的 Flint 语义为 intensive/{semantic}，不能使用 sum；请改用 average")
    return aggregation


def _aggregate_tool(engine, entry: Mapping[str, Any], args: Mapping[str, Any], *,
                    cancel_check=None, timeout_seconds: float | None = None) -> dict[str, Any]:
    dataset_id = args.get("datasetId")
    dimension = args.get("dimension")
    measure = args.get("measure")
    if not isinstance(dataset_id, str) or not dataset_id:
        raise ToolContractError("datasetId must be a non-empty string")
    if dimension is not None and not isinstance(dimension, str):
        raise ToolContractError("dimension must be a string or null")
    if not isinstance(measure, str) or not measure:
        raise ToolContractError("measure must be a non-empty string")
    dataset = _dataset(entry, dataset_id)
    calendar = _calendar_for(entry, args, dataset)
    if args.get("chartId"):
        card_context = build_context(entry, str(args.get("chartId")))
        bound_dataset = card_context.get("datasetId")
        if bound_dataset and dataset_id != bound_dataset:
            raise ToolContractError("当前卡片已绑定另一个数据集；跨数据集查询必须使用显式对账工具")
    columns = _columns(dataset)
    from .copilot import _aggregate_derived_answer
    derived = {str(item["name"]): item for item in safe_derived_measures(dataset)}
    if dimension and _sensitive(dataset, str(dimension)):
        raise ToolContractError(f"字段「{dimension}」被敏感字段策略禁止用于 Agent 查询")
    if _sensitive(dataset, str(measure)):
        raise ToolContractError(f"字段「{measure}」被敏感字段策略禁止用于 Agent 查询")
    if dimension and dimension not in columns:
        raise ToolContractError(f"unknown dimension: {dimension}")
    if measure not in columns and measure not in derived:
        raise ToolContractError(f"unknown measure: {measure}")
    requested_aggregation = args.get("aggregation")
    if requested_aggregation in (None, "") and args.get("chartId") and measure in columns:
        # The selected card may declare per-measure aggregation (mixed cards
        # are legal: revenue=sum + price=average). Inherit that semantic
        # default before applying the Flint intensive/additive legality gate;
        # otherwise a typed Agent query silently falls back to SUM and drifts
        # from the visible card.
        card_context = build_context(entry, str(args.get("chartId")))
        for semantic in card_context.get("measureSemantics") or []:
            if isinstance(semantic, Mapping) and semantic.get("name") == measure:
                requested_aggregation = semantic.get("aggregation")
                break
    aggregation = _aggregation_for(dataset, str(measure), requested_aggregation) if measure in columns else str(requested_aggregation or "sum")
    filters = _scoped_filters(entry, args)
    if not isinstance(filters, (list, dict)):
        raise ToolContractError("filter must be an array or all/any object")
    time_window = args.get("timeWindow")
    if time_window is not None:
        if not dimension or not isinstance(time_window, Mapping):
            raise ToolContractError("timeWindow requires a time dimension and an object")
        role = str((dataset.get("columnRoles") or {}).get(dimension) or "").lower()
        typed = (dataset.get("measureTypes") or {}).get(dimension)
        typed_kind = str(typed.get("type") if isinstance(typed, Mapping) else typed or "").lower()
        if not typed_kind:
            typed_column = next((item for item in dataset.get("columns") or []
                                 if isinstance(item, Mapping) and item.get("name") == dimension), None)
            typed_kind = str((typed_column or {}).get("type") or "").lower()
        if role not in {"time", "temporal", "date", "datetime"} and typed_kind not in {"time", "date", "datetime", "temporal"}:
            raise ToolContractError(f"timeWindow 只能用于时间字段：{dimension}")
        low, high = time_window.get("from"), time_window.get("to")
        if low in (None, "") and high in (None, ""):
            raise ToolContractError("timeWindow requires from or to")
        predicate = {"column": dimension, "op": "daterange", "value": [low, high]}
        if isinstance(filters, dict):
            filters = {"all": list(filters.get("all") or []) + [predicate],
                       "any": list(filters.get("any") or [])}
        else:
            filters = list(filters) + [predicate]
    bucket = args.get("bucket")
    if bucket is None and args.get("chartId"):
        bucket = (build_context(entry, str(args.get("chartId"))).get("dimensionBucket"))
    if bucket is not None and not isinstance(bucket, Mapping):
        raise ToolContractError("bucket must be an object or null")
    if isinstance(bucket, Mapping) and calendar.get("kind") == "fiscal":
        # The calendar is an explicit query semantic, not a label guess.  Copy
        # the request-owned bucket before attaching it so caller args remain
        # immutable and the aggregator can use the same declaration in SQL.
        bucket = dict(bucket)
        bucket["calendar"] = dict(calendar)
    grain = args.get("grain")
    if grain and isinstance(bucket, Mapping) and bucket.get("bucket") != grain:
        raise ToolContractError(f"时间粒度冲突：请求 {grain}，当前卡片为 {bucket.get('bucket')}")
    dimensions = args.get("dimensions")
    if not isinstance(dimensions, list):
        dimensions = [dimension] if dimension else []
    dimensions = [str(item) for item in dimensions if item]
    hierarchy_path = args.get("hierarchyPath")
    if hierarchy_path is not None:
        if not isinstance(hierarchy_path, list) or not hierarchy_path or len(hierarchy_path) > 6 \
                or any(not isinstance(item, str) or not item for item in hierarchy_path):
            raise ToolContractError("hierarchyPath must contain 1-6 dimension names")
        edges = {(str(edge.get("parent")), str(edge.get("child")))
                 for edge in (dataset.get("hierarchy") or []) if isinstance(edge, Mapping)}
        path = [str(item) for item in hierarchy_path]
        if any((parent, child) not in edges for parent, child in zip(path, path[1:])):
            raise ToolContractError("hierarchyPath 必须沿已声明的父子维度顺序下钻")
        if not dimensions:
            dimensions = path[:]
        elif dimensions[-len(path):] != path:
            raise ToolContractError("dimensions 必须包含 hierarchyPath 的连续层级")
    breakdown = args.get("breakdown")
    if breakdown and breakdown not in dimensions:
        dimensions.append(str(breakdown))
    sensitive_dimensions = [item for item in dimensions if _sensitive(dataset, item)]
    if sensitive_dimensions:
        raise ToolContractError(f"字段「{sensitive_dimensions[0]}」被敏感字段策略禁止用于 Agent 查询")
    unknown_dimensions = [item for item in dimensions if item not in columns]
    if unknown_dimensions:
        raise ToolContractError(f"unknown dimension: {unknown_dimensions[0]}")
    # Every Agent aggregation carries a hard server-owned work budget.  The
    # planner may tighten these values; it cannot remove the guard by omitting
    # them.  This protects large reports while preserving Flint's exact
    # grouping/measure semantics (refs/flint/src/core/aggregate.ts:7-20).
    max_scan_rows = args.get("maxScanRows") or 500_000
    max_groups = args.get("maxGroups") or 50_000
    raw_limit = args.get("limit")
    if raw_limit in (None, ""):
        limit = 50
    elif isinstance(raw_limit, bool) or not isinstance(raw_limit, int):
        raise ToolContractError("limit must be an integer")
    else:
        limit = raw_limit
    raw_scan_rows = args.get("maxScanRows")
    if raw_scan_rows in (None, ""):
        max_scan_rows = 500_000
    elif isinstance(raw_scan_rows, bool) or not isinstance(raw_scan_rows, int):
        raise ToolContractError("maxScanRows must be an integer")
    else:
        max_scan_rows = raw_scan_rows
    raw_groups = args.get("maxGroups")
    if raw_groups in (None, ""):
        max_groups = 50_000
    elif isinstance(raw_groups, bool) or not isinstance(raw_groups, int):
        raise ToolContractError("maxGroups must be an integer")
    else:
        max_groups = raw_groups
    request = {"groupBy": dimensions,
               "measures": [{"column": measure, "aggregation": aggregation}],
               "filter": filters, "bucket": bucket, "limit": min(limit, 500),
               "maxScanRows": max_scan_rows, "maxGroups": max_groups}
    if measure in derived:
        if not dimension:
            raise ToolContractError(f"派生指标「{measure}」需要一个图表维度才能计算")
        selected = next((chart for chart in (entry.get("manifest") or {}).get("charts") or []
                         if isinstance(chart, Mapping) and chart.get("id") == args.get("chartId")), None)
        try:
            result_columns, result_rows, result_bucket = _aggregate_derived_answer(
                engine, str(dataset_id), dict(dataset), selected, str(measure), str(dimension),
                filters, aggregation, cancel_check=cancel_check, timeout_seconds=timeout_seconds,
                max_scan_rows=int(max_scan_rows), max_groups=int(max_groups))
        except Exception as exc:
            raise ToolContractError(str(exc)) from exc
        return {"kind": "evidence", "datasetId": dataset_id, "dimension": dimensions,
                "measure": measure, "aggregation": "derived", "bucket": result_bucket,
                "hierarchyPath": list(hierarchy_path or []),
                "columns": result_columns, "rows": result_rows, "truncated": False,
                "groupCount": len(result_rows)}
    try:
        parsed = parse_request(request)
        result = aggregate(engine, dataset_id, parsed,
                           cancel_check=cancel_check, timeout_seconds=timeout_seconds).to_dict()
    except AggregationCancelled:
        raise
    except (AggregationError, TypeError, ValueError) as exc:
        # Do not leak parser/SQL implementation errors through the typed Agent
        # tool boundary.  The model/UI can only recover from a contract error.
        raise ToolContractError(str(exc)) from None
    return {"kind": "evidence", "datasetId": dataset_id, "dimension": dimensions,
            "measure": measure, "aggregation": aggregation,
            "hierarchyPath": list(hierarchy_path or []), **result}


# 序列类工具（compare/anomaly/trend/root_cause/recommend）分析的必须是**完整
# 分组序列**——2026-10-24 golden 语料（NA-03）在营销 256 天日粒度上实锤：缺省
# limit=50 的「按值 top-N」截断让 compare_periods 把序列中段（07-14/06-20）当成
# 「最近两期」，anomaly/trend 在任意子集上算信号。引擎单查询桶上限就是 500
# （_aggregate_tool 的 min(limit, 500)），序列工具一律取满；仍截断则 fail-closed
# 抛可行动错误，绝不静默分析任意子集（与 F03「不静默截断」同一纪律）。
SERIES_TOOL_MAX_BUCKETS = 500
_SERIES_TOOLS = frozenset({"compare_periods", "anomaly_scan", "root_cause_scan",
                           "trend_inflection", "recommend_actions"})


def _series_tool_args(args: Mapping[str, Any]) -> Mapping[str, Any]:
    return {**args, "limit": SERIES_TOOL_MAX_BUCKETS}


def _require_full_series(tool: str, result: Mapping[str, Any]) -> None:
    if result.get("truncated"):
        raise ToolContractError(
            f"{tool} 需要完整分组序列：当前维度超过 {SERIES_TOOL_MAX_BUCKETS} 个桶被截断，"
            "在截断子集上做期间/趋势/异常分析会得到静默错误结论。"
            "请用 timeWindow/bucket 显式收窄时间范围，或改用带显式 labels 的 aggregate。")


def _calendar_order_rows(rows: list[Any]) -> list[Any]:
    """Order grouped evidence with the shared calendar/category sort key."""
    return sorted(rows, key=lambda row: date_parse.sort_key(row[0])
                   if isinstance(row, list) and row else (1, ""))


# ---------------------------------------------------------------------------
# NA-05 PSI domain tools (read-only).  The pure math lives in
# ``agent_extensions`` (psi_domain section); everything here is the typed
# I/O seam: caller-declared columns, sensitive policy, ONE aggregation query
# on the existing seam, calendar ordering via the shared date parser.
# ---------------------------------------------------------------------------

_PSI_NON_METRIC_TYPES = {"category", "dimension", "string", "text", "boolean"}


def _psi_declared_column(dataset: Mapping[str, Any], name: Any, role_label: str,
                         *, require_metric: bool = False) -> str:
    """A PSI column must be declared by the caller, known and non-sensitive.

    ``require_metric`` is for the numeric inputs only (Sell In/Sell Out/
    Inventory); grouping/time columns may legitimately be declared category.
    """
    if not isinstance(name, str) or not name.strip():
        raise ToolContractError(f"{role_label} 必须是非空字符串（PSI 工具不猜列）")
    name = name.strip()
    if name not in _columns(dataset):
        raise ToolContractError(f"unknown {role_label}: {name}")
    if _sensitive(dataset, name):
        raise ToolContractError(f"字段「{name}」被敏感字段策略禁止用于 Agent 查询")
    declared = ""
    for column in dataset.get("columns") or []:
        if isinstance(column, Mapping) and column.get("name") == name:
            declared = str(column.get("type") or column.get("role") or "").lower()
            break
    roles = dataset.get("columnRoles")
    role = str(roles.get(name) or "").lower() if isinstance(roles, Mapping) else ""
    if require_metric and (declared in _PSI_NON_METRIC_TYPES or role in _PSI_NON_METRIC_TYPES):
        kind = declared or role
        raise ToolContractError(f"PSI 工具要求量值列；「{name}」被声明为 {kind}")
    return name


def _psi_metric_column(dataset: Mapping[str, Any], name: str) -> str:
    """A PSI metric column must aggregate additively (sum) per Flint semantics."""
    aggregation = _aggregation_for(dataset, name, None)
    if aggregation not in {"sum", "count"}:
        raise ToolContractError(
            f"PSI 工具要求可加量值列（sum）；「{name}」的 Flint 语义解析为 {aggregation}")
    return name


def _psi_period_query(engine, entry: Mapping[str, Any], dataset: Mapping[str, Any],
                      dataset_id: str, args: Mapping[str, Any], group_by: list[str],
                      metric_columns: list[str], *, cancel_check=None,
                      timeout_seconds: float | None = None) -> dict[str, Any]:
    """One real aggregation over caller-declared columns (the shared seam)."""
    filters = _scoped_filters(entry, args)
    if not isinstance(filters, (list, dict)):
        raise ToolContractError("filter must be an array or all/any object")
    request = {"groupBy": group_by,
               "measures": [{"column": column, "aggregation": "sum"} for column in metric_columns],
               "filter": filters, "limit": SERIES_TOOL_MAX_BUCKETS,
               "maxScanRows": int(args.get("maxScanRows") or 500_000),
               "maxGroups": int(args.get("maxGroups") or 50_000)}
    try:
        parsed = parse_request(request)
        result = aggregate(engine, dataset_id, parsed, cancel_check=cancel_check,
                           timeout_seconds=timeout_seconds).to_dict()
    except AggregationCancelled:
        raise
    except (AggregationError, TypeError, ValueError) as exc:
        raise ToolContractError(str(exc)) from None
    if result.get("truncated"):
        raise ToolContractError(
            f"PSI 工具需要完整分组序列：超过 {SERIES_TOOL_MAX_BUCKETS} 个分组被截断，"
            "在截断子集上做勾稽/扫描会得到静默错误结论。请用 filter 收窄成员，"
            "或声明更粗的时间粒度后重试。")
    return result


def _psi_args_common(entry: Mapping[str, Any], args: Mapping[str, Any],
                     metric_labels: list[str]) -> tuple[str, Mapping[str, Any], str, list[str], list[str]]:
    dataset_id = args.get("datasetId")
    if not isinstance(dataset_id, str) or not dataset_id:
        raise ToolContractError("datasetId must be a non-empty string")
    dataset = _dataset(entry, dataset_id)
    time_column = _psi_declared_column(dataset, args.get("timeColumn"), "timeColumn")
    metric_columns = [_psi_declared_column(dataset, args.get(label), label, require_metric=True)
                      for label in metric_labels]
    for column in metric_columns:
        _psi_metric_column(dataset, column)
    if len(set(metric_columns)) < len(metric_columns):
        raise ToolContractError("PSI 工具的各指标列必须互不相同")
    return dataset_id, dataset, time_column, metric_columns


def _psi_period_series(result: Mapping[str, Any], metric_columns: list[str]) -> list[dict[str, Any]]:
    """Aligned per-period records from a [period, m1, m2, …] grouped result."""
    series = []
    for row in result.get("rows") or []:
        if not isinstance(row, list) or len(row) < len(metric_columns) + 1:
            continue
        record = {"period": row[0]}
        for index, name in enumerate(metric_columns, start=1):
            value = row[index]
            record[name] = value if isinstance(value, (int, float)) else None
        series.append(record)
    return series


def _psi_tolerance_arg(args: Mapping[str, Any], key: str, default: float,
                       maximum: float) -> float:
    """XC-02: optional tolerance knobs, bounded before they reach the pure rule."""
    value = args.get(key)
    if value is None or value == "":
        return float(default)
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ToolContractError(f"{key} 必须是非负数字") from None
    if value < 0 or value > maximum:
        raise ToolContractError(f"{key} 必须在 0..{maximum} 内（收到 {value}）")
    return value


def _psi_row_scan(engine, entry: Mapping[str, Any], dataset: Mapping[str, Any],
                  dataset_id: str, args: Mapping[str, Any], group_by_column: str,
                  time_column: str, metric_columns: list[str]) -> list[dict[str, Any]]:
    """Bounded raw rows for stockout granularity="row" (XC-02).

    Row mode judges every filtered row independently, so it must scan the FULL
    filtered row set — a LIMIT-truncated scan would silently bias the hit
    counts/ratio.  The fetch is therefore capped at ``maxScanRows`` (default
    500k, schema-bounded) and exceeding the cap is a fail-closed contract
    error, never a silent partial answer.
    """
    filters = _scoped_filters(entry, args)
    if not isinstance(filters, (list, dict)):
        raise ToolContractError("filter must be an array or all/any object")
    info = engine.table_info(dataset_id)
    if info is None:
        raise ToolContractError(f"dataset {dataset_id!r} is not loaded")
    from .aggregator import _filter_clause, _quote
    where, params = _filter_clause(filters, set(info.columns), engine)
    cap = max(1, int(args.get("maxScanRows") or 500_000))
    columns = [group_by_column, time_column, metric_columns[0], metric_columns[1]]
    select = ", ".join(_quote(c) for c in columns)
    sql = (f"SELECT {select} FROM {_quote(dataset_id)} {where} "
           f"ORDER BY {_quote(time_column)} LIMIT {cap + 1}")
    try:
        raw = engine.execute(sql, tuple(params))
    except AggregationCancelled:
        raise
    except (AggregationError, TypeError, ValueError) as exc:
        raise ToolContractError(str(exc)) from None
    if len(raw) > cap:
        raise ToolContractError(
            f"stockout_scan granularity=row 需要逐行扫描全量筛选行：行数超过上限 {cap}，"
            "在截断行集上的行数/占比是静默错误结论。请用 filter 收窄范围后重试。")
    sell_out_col, inventory_col = metric_columns[0], metric_columns[1]
    return [{"member": row.get(columns[0]), "period": row.get(columns[1]),
             "sellOut": row.get(sell_out_col) if isinstance(row.get(sell_out_col), (int, float)) else None,
             "inventory": row.get(inventory_col) if isinstance(row.get(inventory_col), (int, float)) else None}
            for row in raw]


def execute_read_tool(name: str, args: Mapping[str, Any], *, engine, entry: Mapping[str, Any],
                      cancel_check=None, timeout_seconds: float | None = None) -> dict[str, Any]:
    """Execute a read-only tool using existing semantic aggregation code."""
    calendar = None
    if name in {"root_cause_scan", "root_cause_multi", "trend_inflection", "recommend_actions", "data_quality"}:
        calendar = _calendar_for(entry, args)
    if name == "resolve_card":
        chart_id = args.get("chartId")
        chart = next((chart for chart in (entry.get("manifest") or {}).get("charts") or []
                      if isinstance(chart, Mapping) and chart.get("id") == chart_id), None)
        if chart is None:
            raise ToolContractError(f"chart {chart_id!r} is not available")
        safe_context = build_context(entry, str(chart_id or ""))
        return {"kind": "card", "id": chart.get("id"), "title": chart.get("title"),
                "viewType": chart.get("viewType"),
                # Never return the raw editor.data object: it can contain
                # sensitive field bindings and unbounded filter payloads.
                # ReportContext is the single bounded/redacted projection.
                "context": safe_context, "data": safe_context}
    if name == "aggregate":
        return _aggregate_tool(engine, entry, args, cancel_check=cancel_check,
                               timeout_seconds=timeout_seconds)
    if name == "drilldown":
        result = _aggregate_tool(engine, entry, args, cancel_check=cancel_check,
                                 timeout_seconds=timeout_seconds)
        result["kind"] = "drilldown"
        return result
    if name == "get_semantic_context":
        card_id = args.get("chartId")
        return {"kind": "semantic_context", "context": build_context(entry, str(card_id or ""))}
    if name == "propose_derived_measure":
        # NA-04: proposal-only validation of a derived.measures node. This is
        # the same typed payload the proposals/validate endpoint consumes;
        # nothing is mutated here — persistence stays behind the editor
        # approval seam (ProposalTransaction), exactly like resolve_field's
        # inline alias_confirmation proposal.
        from .agent_proposals import validate_derived_measure
        try:
            check = validate_derived_measure(entry, {
                "kind": "derived_measure",
                "datasetId": args.get("datasetId"),
                "measure": args.get("measure"),
            })
        except Exception as exc:
            if isinstance(exc, ToolContractError):
                raise
            raise ToolContractError(str(exc)) from None
        result = dict(check)
        result["kind"] = "proposal"
        result["proposalKind"] = "derived_measure"
        return result
    if name == "resolve_field":
        context = build_context(entry, str(args.get("chartId") or ""))
        query = str(args.get("name") or "").strip().casefold()
        matches = [item for item in context.get("fields", [])
                   if str(item.get("name") or "").casefold() == query]
        matches += [item for item in context.get("aliases", [])
                    if str(item.get("alias") or "").casefold() == query]
        if not matches and query:
            import difflib
            candidates = []
            for item in list(context.get("fields", [])) + list(context.get("aliases", [])):
                label = str(item.get("name") or item.get("alias") or "").casefold()
                score = difflib.SequenceMatcher(None, query, label).ratio()
                if score >= 0.75:
                    candidate = dict(item)
                    # A fuzzy candidate is an inference even when the source
                    # item came from the manifest.  Keeping ``manifest`` here
                    # falsely tells the model that the spelling was authored
                    # and makes the confirmation gate meaningless.
                    candidate["source"] = "inferred"
                    candidate["confidence"] = round(score, 3)
                    candidate["requiresConfirmation"] = True
                    candidates.append((score, candidate))
            matches = [item for _score, item in sorted(candidates, key=lambda pair: -pair[0])[:5]]
        else:
            matches = [dict(item) for item in matches]
            for item in matches:
                item.setdefault("source", "manifest")
                item.setdefault("confidence", 1.0)
                item.setdefault("requiresConfirmation", False)
        requires_confirmation = any(item.get("requiresConfirmation") for item in matches)
        confirmed = False
        canonical = None
        if args.get("confirm") is True:
            if len(matches) != 1 or not requires_confirmation:
                raise ToolContractError("只有唯一的 inferred 字段候选可以被显式确认")
            candidate = matches[0]
            canonical = candidate.get("canonical") or candidate.get("name")
            if not isinstance(canonical, str) or not canonical:
                raise ToolContractError("字段候选缺少 canonical name")
            confirmed = True
        # XA-02（NA-06 别名跨报告学习目录）：解析失败 / 低置信（没有任何 manifest 级
        # 精确命中）时追加学习建议——只读、confidence 封顶 0.9、恒 requiresConfirmation
        # （永不自动采用，落账仍走 propose_alias_confirmation 确认闸）。既有 matches
        # 语义零改动：建议放独立 learnedSuggestions 字段；敏感列永不出现（NS-01）。
        learned_suggestions: list[dict[str, Any]] = []
        exact_hit = any(item.get("source") == "manifest" for item in matches)
        if query and not exact_hit:
            try:
                from .alias_learning import default_state_home, suggest, tenant_id
                ds_id = str(context.get("datasetId") or "")
                dataset = _dataset(entry, ds_id) if ds_id else {}
                privacy = dataset.get("privacy") or {}
                sensitive = {str(item) for item in ((privacy.get("sensitiveColumns") or [])
                                                    if isinstance(privacy, Mapping) else [])
                             if isinstance(item, str)}
                learned_suggestions = [
                    item for item in suggest(default_state_home(), tenant_id(),
                                             sorted(_columns(dataset)), query)
                    if item.get("canonical") not in sensitive
                    and item.get("alias") not in sensitive]
            except Exception:  # noqa: BLE001 — 学习建议是只读增强，绝不影响解析主语义
                learned_suggestions = []
        result = {"kind": "field_resolution", "query": args.get("name"),
                  "matches": matches, "ambiguous": len(matches) > 1,
                  "requiresConfirmation": requires_confirmation,
                  "confirmed": confirmed, "canonical": canonical}
        if learned_suggestions:
            result["learnedSuggestions"] = learned_suggestions
        if confirmed and canonical:
            # Confirmation is still proposal-only: the current turn may use
            # the canonical field, but persistence requires the editor
            # approval seam rather than silently mutating the manifest.
            result["proposal"] = {
                "kind": "alias_confirmation",
                "chartId": args.get("chartId"),
                "alias": args.get("name"),
                "canonical": canonical,
                "requiresApproval": True,
            }
        return result
    if name == "reconcile_cards":
        chart_ids = args.get("chartIds")
        if not isinstance(chart_ids, list) or not chart_ids or len(chart_ids) > 12:
            raise ToolContractError("chartIds must contain 1-12 cards")
        available_chart_ids = {str(chart.get("id")) for chart in (entry.get("manifest") or {}).get("charts") or []
                               if isinstance(chart, Mapping) and chart.get("id")}
        unknown_chart_ids = [str(chart_id) for chart_id in chart_ids
                             if str(chart_id) not in available_chart_ids]
        if unknown_chart_ids:
            raise ToolContractError(f"unknown chart: {unknown_chart_ids[0]}")
        contexts = [build_context(entry, str(chart_id)) for chart_id in chart_ids]
        fields = ("datasetId", "dimension", "measures", "filter", "globalFilters",
                  "dimensionBucket", "aggregation", "grain", "measureSemantics", "pivot")
        conflicts = []
        for field in fields:
            values = [context.get(field) for context in contexts]
            if any(value != values[0] for value in values[1:]):
                conflicts.append({"field": field, "values": values})
        # Flint resolves a field's intrinsic aggregation role in field
        # semantics, then lets the channel declaration select the effective
        # aggregation (refs/flint/src/core/field-semantics.ts:138-170 and
        # refs/flint/src/core/type-registry.ts:103-122).  A raw list equality
        # check is too weak for reconciliation: it treats reordered measures
        # as different and cannot explain that two same-name measures disagree
        # only on aggregation/type.  Build a bounded, redacted comparison from
        # the already projected contexts instead of inspecting editor data.
        def canonical(value: Any) -> Any:
            if isinstance(value, Mapping):
                return {str(key): canonical(value[key]) for key in sorted(value, key=str)}
            if isinstance(value, (list, tuple)):
                items = [canonical(item) for item in value]
                # Filter groups are conjunction/disjunction sets, so order is
                # not semantic. Keep authored order for axes and measures.
                return items
            return value

        def alias_map(context: Mapping[str, Any]) -> dict[str, str]:
            output: dict[str, str] = {}
            for item in context.get("aliases") or []:
                if isinstance(item, Mapping):
                    alias, target = item.get("alias"), item.get("canonical")
                    if isinstance(alias, str) and isinstance(target, str):
                        output[alias] = target
            return output

        def measure_map(context: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
            aliases = alias_map(context)
            output: dict[str, dict[str, Any]] = {}
            for item in context.get("measureSemantics") or []:
                if not isinstance(item, Mapping):
                    continue
                name = item.get("name")
                if not isinstance(name, str) or not name:
                    continue
                canonical_name = aliases.get(name, name)
                # Only expose semantic facts needed for matching.  Never copy
                # a formula or arbitrary manifest payload into the result.
                output[canonical_name] = {
                    "name": canonical_name,
                    "aggregation": item.get("aggregation"),
                    "measureType": item.get("measureType"),
                    "derived": bool(item.get("derived")),
                }
            return output

        def filter_signature(value: Any) -> Any:
            # all/any groups are sets of predicates for reconciliation. Sort
            # only the bounded predicate representations; values themselves
            # are already redacted by build_context.
            if isinstance(value, Mapping) and set(value).issubset({"all", "any"}):
                return {key: sorted((canonical(item) for item in (value.get(key) or [])), key=repr)
                        for key in sorted(value)}
            if isinstance(value, list):
                return sorted((canonical(item) for item in value), key=repr)
            return canonical(value)

        def pivot_signature(value: Any) -> Any:
            """Compare pivot semantics without treating value-chip order as meaning."""
            if not isinstance(value, Mapping):
                return canonical(value)
            normalized = dict(value)
            values = normalized.get("values")
            if isinstance(values, list):
                normalized["values"] = sorted(
                    (canonical(item) for item in values), key=repr)
            return canonical(normalized)

        measure_maps = [measure_map(context) for context in contexts]
        common_names = sorted(set.intersection(*(set(item) for item in measure_maps))) if measure_maps else []
        union_names = sorted(set.union(*(set(item) for item in measure_maps))) if measure_maps else []
        measure_mismatches = []
        for metric in common_names:
            values = [item[metric] for item in measure_maps]
            if any(value != values[0] for value in values[1:]):
                measure_mismatches.append({"name": metric, "values": values})
        missing_measures = [
            {"name": metric, "presentIn": [index for index, item in enumerate(measure_maps) if metric in item]}
            for metric in union_names if metric not in common_names
        ]
        def all_equal(values: list[Any]) -> bool:
            return bool(values) and all(value == values[0] for value in values[1:])

        semantic_comparison = {
            "equivalent": bool(contexts) and not measure_mismatches and not missing_measures
            and len({context.get("datasetId") for context in contexts}) == 1
            and all_equal([canonical(context.get("dimension")) for context in contexts])
            and all_equal([canonical(context.get("dimensionBucket")) for context in contexts])
            and all_equal([pivot_signature(context.get("pivot")) for context in contexts]),
            "measureMatches": common_names,
            "measureMismatches": measure_mismatches,
            "missingMeasures": missing_measures,
        }
        scope_signatures = [
            (filter_signature(context.get("globalFilters")), filter_signature(context.get("filter")))
            for context in contexts
        ]
        scope_equivalent = bool(scope_signatures) and all(item == scope_signatures[0] for item in scope_signatures[1:])
        remediation_actions = []
        if semantic_comparison["equivalent"] and not scope_equivalent:
            remediation_actions.append({
                "action": "align_filter_scope",
                "mode": "proposal_only",
                "requiresApproval": True,
                "reason": "卡片指标语义一致，仅筛选范围不同；对齐范围可能改变结果",
            })
        if semantic_comparison["measureMismatches"]:
            remediation_actions.append({
                "action": "align_measure_semantics",
                "mode": "proposal_only",
                "requiresApproval": True,
                "reason": "同名指标的聚合或 Flint measure type 不一致，不能自动改写",
            })
        return {"kind": "card_reconciliation", "cards": contexts,
                "consistent": not conflicts, "conflicts": conflicts,
                # Same metric semantics under a different active filter is a
                # useful distinction: the cards are semantically comparable,
                # but their scopes are not interchangeable. Dataset,
                # dimension, pivot or aggregation differences are not
                # semantically equivalent and must never be presented as a
                # safe cross-card reconciliation.
                "semanticEquivalent": semantic_comparison["equivalent"],
                "scopeEquivalent": scope_equivalent,
                "semanticComparison": semantic_comparison,
                "remediation": {"mode": "proposal_only", "actions": remediation_actions},
                "scopeComparison": {"equivalent": scope_equivalent}}
    if name == "aggregate_multi":
        requests = args.get("requests")
        if not isinstance(requests, list) or not requests or len(requests) > 8:
            raise ToolContractError("requests must contain 1-8 aggregate requests")
        if not all(isinstance(request, Mapping) for request in requests):
            raise ToolContractError("every aggregate_multi request must be an object")
        # Derived requests with one explicit semantic shape can share the
        # dependency aggregate. Mixed cards/filters/aggregations fall back to
        # the ordinary per-request path to preserve each card's semantics.
        effective_requests = [dict(request, **({"chartId": args.get("chartId")} if args.get("chartId") else {}))
                              for request in requests]
        if effective_requests and all(isinstance(request.get("measure"), str) for request in effective_requests):
            from .copilot import _aggregate_derived_multi_answers, _derived_measures
            first = effective_requests[0]
            dataset = _dataset(entry, str(first.get("datasetId") or ""))
            safe_derived = _derived_measures(dict(dataset))
            common_keys = ("datasetId", "dimension", "chartId", "filter", "bucket", "grain",
                           "timeWindow", "aggregation", "maxScanRows", "maxGroups")
            comparable = all(str(request.get("measure")) in safe_derived for request in effective_requests) \
                and all(all(request.get(key) == first.get(key) for key in common_keys) for request in effective_requests) \
                and first.get("aggregation") not in (None, "")
            if comparable:
                chart = next((item for item in (entry.get("manifest") or {}).get("charts") or []
                              if isinstance(item, Mapping) and item.get("id") == first.get("chartId")), None)
                filters = _scoped_filters(entry, first)
                batched = _aggregate_derived_multi_answers(
                    engine, str(first.get("datasetId")), dict(dataset), chart,
                    [str(request["measure"]) for request in effective_requests], str(first.get("dimension") or ""),
                    filters, str(first.get("aggregation")), cancel_check=cancel_check,
                    timeout_seconds=timeout_seconds,
                    max_scan_rows=int(first.get("maxScanRows") or 500_000),
                    max_groups=int(first.get("maxGroups") or 50_000))
                return {"kind": "multi_evidence", "results": batched}
        return {"kind": "multi_evidence", "results": [
            _aggregate_tool(engine, entry, {**request, **({"chartId": args.get("chartId")} if args.get("chartId") else {})}, cancel_check=cancel_check,
                            timeout_seconds=timeout_seconds) for request in requests
        ]}
    if name == "compare_periods":
        result = _aggregate_tool(engine, entry, _series_tool_args(args),
                                 cancel_check=cancel_check,
                                 timeout_seconds=timeout_seconds)
        _require_full_series(name, result)
        rows = result.get("rows") or []
        # GROUP BY order is not a time contract. Use the shared date parser
        # for both calendar labels (including unpadded 2026/9/5) and ordinary
        # categorical labels; this keeps Agent evidence aligned with the
        # compiler/runtime date semantics instead of lexically sorting dates.
        result["rows"] = _calendar_order_rows(rows)
        rows = result["rows"]
        labels = args.get("labels") or {}
        current_label = labels.get("current")
        previous_label = labels.get("previous")
        values = {row[0]: row[1] for row in rows if isinstance(row, list) and len(row) > 1}
        if current_label is None or previous_label is None:
            # GROUP BY order is not a time contract. Use a deterministic label
            # order for the fallback; callers with non-lexical business periods
            # must provide explicit labels in the typed args.
            ordered = sorted(values, key=date_parse.sort_key)
            previous_label, current_label = (ordered[-2], ordered[-1]) if len(ordered) >= 2 else (None, None)
        current = values.get(current_label) if current_label is not None else None
        previous = values.get(previous_label) if previous_label is not None else None
        return {"kind": "period_comparison", "currentLabel": current_label,
                "previousLabel": previous_label, "comparison": compare_periods(current, previous),
                "evidence": result}
    if name == "contribution":
        dataset = _dataset(entry, str(args.get("datasetId") or ""))
        from .agent_context import safe_derived_measures
        measure_name = str(args.get("measure") or "")
        if measure_name in {str(item["name"]) for item in safe_derived_measures(dataset)}:
            raise ToolContractError("贡献度分析只支持可加的基础指标；派生比率请先分析其依赖指标")
        result = _aggregate_tool(engine, entry, args, cancel_check=cancel_check,
                                 timeout_seconds=timeout_seconds)
        # Use the effective aggregation returned by the shared aggregate seam,
        # including a selected card's per-measure default.  Reading only the
        # raw request here lets an omitted ``aggregation`` silently validate as
        # SUM while the card actually used COUNT/AVERAGE (Flint's additive vs
        # intensive distinction; refs/flint/src/core/type-registry.ts:50-84).
        contribution_aggregation = str(result.get("aggregation") or
                                       _aggregation_for(dataset, measure_name, args.get("aggregation")))
        if contribution_aggregation not in {"sum", "count"}:
            raise ToolContractError(
                f"字段「{measure_name}」的聚合为 {contribution_aggregation}，不能计算成员贡献度；"
                "请使用可加指标的 sum 或 count")
        rows = result.get("rows") or []
        items = contribution([{"member": row[0], "value": row[1]} for row in rows
                              if isinstance(row, list) and len(row) > 1])
        observed_total = sum(float(item["value"]) for item in items)
        overall = result.get("aggregates") or {}
        measure = str(args.get("measure") or "")
        aggregate_total = (overall.get(measure) or {}).get(contribution_aggregation)
        return {"kind": "contribution", "items": items,
                "observedTotal": observed_total,
                "aggregateTotal": aggregate_total,
                "reconciles": (aggregate_total is None or result.get("truncated")
                                or abs(observed_total - float(aggregate_total)) < 1e-9),
                "coverage": "truncated" if result.get("truncated") else "complete",
                "evidence": result}
    if name == "anomaly_scan":
        result = _aggregate_tool(engine, entry, _series_tool_args(args),
                                 cancel_check=cancel_check,
                                 timeout_seconds=timeout_seconds)
        _require_full_series(name, result)
        ordered_rows = _calendar_order_rows(result.get("rows") or [])
        result["rows"] = ordered_rows
        rows = [row for row in ordered_rows
                if isinstance(row, list) and len(row) > 1]
        return {"kind": "anomaly_scan", "anomalies": detect_anomalies(
            [row[1] for row in rows]), "evidence": result}
    if name == "root_cause_scan":
        result = _aggregate_tool(engine, entry, _series_tool_args(args),
                                 cancel_check=cancel_check,
                                 timeout_seconds=timeout_seconds)
        _require_full_series(name, result)
        rows = [row for row in (result.get("rows") or [])
                if isinstance(row, list) and len(row) > 1]
        return {"kind": "root_cause_scan", **rank_root_causes(
            [row[0] for row in rows], [row[-1] for row in rows], calendar=calendar), "evidence": result}
    if name == "root_cause_multi":
        measures = args.get("measures")
        if not isinstance(measures, list) or not measures or len(measures) > 4 \
                or any(not isinstance(item, str) or not item for item in measures):
            raise ToolContractError("root_cause_multi.measures must contain 1-4 measure names")
        shared_keys = ("datasetId", "dimension", "aggregation", "filter", "bucket", "calendar",
                       "grain", "timeWindow", "maxScanRows", "maxGroups")
        requests = [{key: args[key] for key in shared_keys if key in args}
                    | {"measure": measure} for measure in measures]
        multi = execute_read_tool("aggregate_multi", {
            "chartId": args.get("chartId"), "requests": requests,
        }, engine=engine, entry=entry, cancel_check=cancel_check,
                                   timeout_seconds=timeout_seconds)
        series: dict[str, dict[str, Any]] = {}
        for measure, evidence in zip(measures, multi.get("results") or []):
            rows = [row for row in (evidence.get("rows") or [])
                    if isinstance(row, list) and len(row) > 1]
            series[measure] = {"labels": [row[0] for row in rows],
                               "values": [row[-1] for row in rows]}
        ranked = rank_root_causes_multi(series, calendar=calendar)
        return {"kind": "root_cause_multi", **ranked, "evidence": multi}
    if name == "trend_inflection":
        result = _aggregate_tool(engine, entry, _series_tool_args(args),
                                 cancel_check=cancel_check,
                                 timeout_seconds=timeout_seconds)
        _require_full_series(name, result)
        ordered_rows = _calendar_order_rows(result.get("rows") or [])
        result["rows"] = ordered_rows
        rows = [row for row in ordered_rows
                if isinstance(row, list) and len(row) > 1]
        return {"kind": "trend_inflection", **trend_inflections(
            [row[0] for row in rows], [row[-1] for row in rows], calendar=calendar), "evidence": result}
    if name == "recommend_actions":
        if not args.get("dimension"):
            raise ToolContractError("recommend_actions requires a dimension")
        result = _aggregate_tool(engine, entry, _series_tool_args(args),
                                 cancel_check=cancel_check,
                                 timeout_seconds=timeout_seconds)
        _require_full_series(name, result)
        ordered_rows = _calendar_order_rows(result.get("rows") or [])
        result["rows"] = ordered_rows
        rows = [row for row in ordered_rows
                if isinstance(row, list) and len(row) > 1]
        recommendations = recommend_actions(
            [row[0] for row in rows], [row[-1] for row in rows],
            base_args=args, truncated=bool(result.get("truncated")),
            limit=args.get("actionLimit", 6), calendar=calendar)
        # Keep the evidence server-owned and bounded: callers can follow the
        # typed action args, but no model prose is promoted to an executable
        # mutation.  The underlying series remains the single aggregate seam
        # (refs/flint/src/core/aggregate.ts:7-20).
        return {**recommendations, "evidence": result}
    if name == "what_if":
        try:
            result = what_if(args.get("value"), args.get("deltaPercent"),
                             args.get("absoluteDelta"), args.get("scenarios"),
                             args.get("links"), args.get("constraints"))
        except (TypeError, ValueError) as exc:
            raise ToolContractError(str(exc)) from None
        return {"kind": "what_if", **result}
    if name == "data_quality":
        dataset_id = args.get("datasetId")
        dataset = _dataset(entry, dataset_id)
        privacy = dataset.get("privacy") or {}
        sensitive = {item for item in (privacy.get("sensitiveColumns") or [])
                     if isinstance(item, str)} if isinstance(privacy, Mapping) else set()
        info = engine.table_info(dataset_id)
        if info is None:
            raise ToolContractError(f"dataset {dataset_id!r} is not loaded")
        columns = [str(column.name) for column in info.schema]
        # The engine's bounded sample API is not part of the semantic contract;
        # use a bounded, quoted projection for diagnostics only.  This is still
        # a server-owned query and never accepts model SQL.
        requested_scan = args.get("maxScanRows") or 500
        try:
            scan_limit = max(1, min(int(requested_scan), 500_000))
        except (TypeError, ValueError) as exc:
            raise ToolContractError("data_quality.maxScanRows must be an integer") from exc
        sample = engine.execute('SELECT * FROM "' + dataset_id.replace('"', '""') + '" LIMIT ' + str(scan_limit),
                                cancel_check=cancel_check, timeout_seconds=timeout_seconds)
        safe_positions = [index for index, column in enumerate(columns) if column not in sensitive]
        safe_columns = [columns[index] for index in safe_positions]
        rows = [[row.get(column) for column in safe_columns] for row in sample]
        raw_keys = args.get("keyColumns") or []
        if not isinstance(raw_keys, list) or len(raw_keys) > 4 or any(not isinstance(item, str) or not item for item in raw_keys):
            raise ToolContractError("data_quality.keyColumns must contain 0-4 column names")
        key_columns = [str(item) for item in raw_keys]
        unknown_keys = [item for item in key_columns if item not in safe_columns]
        if unknown_keys:
            raise ToolContractError(f"data_quality.keyColumns contains unknown or sensitive column: {unknown_keys[0]}")
        measure_columns = [name for name, decl in (dataset.get("measureTypes") or {}).items()
                           if name not in sensitive and
                           (isinstance(decl, Mapping) or str(decl).lower() in {"measure", "number", "numeric"})]
        requested_time = args.get("timeColumn")
        if requested_time is not None and (not isinstance(requested_time, str) or requested_time not in safe_columns):
            raise ToolContractError("data_quality.timeColumn must be a non-sensitive dataset column")
        time_column = requested_time or next((name for name, decl in (dataset.get("columnRoles") or {}).items()
                            if name not in sensitive and str(decl).lower() in {"time", "temporal", "date"}), None)
        return {"kind": "data_quality", "datasetId": dataset_id,
                "scannedRows": len(sample), "rowCount": info.row_count,
                "complete": len(sample) >= info.row_count,
                "schemaColumns": safe_columns,
                "rules": {"keyColumns": key_columns, "timeColumn": time_column},
                "quality": quality_diagnostics(rows, safe_columns,
                                                measure_columns=measure_columns,
                                                time_column=time_column,
                                                key_columns=key_columns,
                                                calendar=_calendar_for(entry, args, dataset))}
    if name == "profile_dataset":
        dataset_id = args.get("datasetId")
        dataset = _dataset(entry, dataset_id)
        privacy = dataset.get("privacy") or {}
        sensitive = {item for item in (privacy.get("sensitiveColumns") or [])
                     if isinstance(item, str)} if isinstance(privacy, Mapping) else set()
        info = engine.table_info(dataset_id)
        if info is None:
            raise ToolContractError(f"dataset {dataset_id!r} is not loaded")
        return {"kind": "dataset_profile", "datasetId": dataset_id,
                "rowCount": info.row_count,
                "columns": [{"name": column.name, "type": column.inferred_type,
                             "nonNull": column.non_null, "distinct": column.distinct,
                             "min": column.min, "max": column.max,
                             "mean": column.mean} for column in info.schema
                            if column.name not in sensitive]}
    if name in {"psi_balance_check", "inventory_weeks", "stockout_scan"}:
        from .agent_extensions import (psi_balance_periods, psi_normalize_grain,
                                        psi_stockout_classes, psi_weeks_of_supply)
        metric_labels = (["sellInColumn", "sellOutColumn", "inventoryColumn"]
                          if name == "psi_balance_check"
                          else ["sellOutColumn", "inventoryColumn"])
        dataset_id, dataset, time_column, metric_columns = _psi_args_common(entry, args, metric_labels)
        if name == "stockout_scan":
            group_by_column = _psi_declared_column(dataset, args.get("groupBy"), "groupBy")
            if group_by_column == time_column:
                raise ToolContractError("stockout_scan 的 groupBy 不能与 timeColumn 相同")
            # XC-02: granularity picks the judgment unit.  "sum" (default) keeps
            # the legacy member×period SUM buckets byte-identical; "row" judges
            # each filtered row independently (no in-group netting) with a
            # bounded, fail-closed full scan.
            granularity = args.get("granularity") or "sum"
            if granularity not in ("sum", "row"):
                raise ToolContractError("stockout_scan 的 granularity 必须是 'sum' 或 'row'")
            if granularity == "row":
                rows = _psi_row_scan(engine, entry, dataset, dataset_id, args,
                                     group_by_column, time_column, metric_columns)
                classification = psi_stockout_classes(
                    rows, top_members=int(args.get("topMembers") or 20), granularity="row")
            else:
                result = _psi_period_query(engine, entry, dataset, dataset_id, args,
                                            [group_by_column, time_column], metric_columns,
                                            cancel_check=cancel_check, timeout_seconds=timeout_seconds)
                rows = []
                for row in result.get("rows") or []:
                    if not isinstance(row, list) or len(row) < 4:
                        continue
                    rows.append({"member": row[0], "period": row[1],
                                  "sellOut": row[2] if isinstance(row[2], (int, float)) else None,
                                  "inventory": row[3] if isinstance(row[3], (int, float)) else None})
                classification = psi_stockout_classes(
                    rows, top_members=int(args.get("topMembers") or 20))
            labels = [row["period"] for row in rows]
            grain = date_parse.infer_grain(labels, time_column)
            # NOTE: the legacy output key for the INFERRED TIME grain is "grain"
            # (unchanged for both modes).  Row mode additionally carries
            # "granularity": "row" from the classifier itself.
            classification.update({
                "kind": "stockout_scan", "datasetId": dataset_id,
                "groupBy": group_by_column, "timeColumn": time_column,
                "grain": grain, "causalClaim": False,
                "columns": {"sellOut": metric_columns[0], "inventory": metric_columns[1]},
                "assumptions": (
                    ["row 粒度逐行判定（不做组内正负相抵）：Sell Out>0 且库存≤0/缺失 记有销无货；库存>0 且 Sell Out≤0 记有货无销",
                     "命中行数/占比按筛选后全量行计；命中样本有界 ≤50 条；同一成员的期列表为去重周期，超限显式标记"] if granularity == "row" else [
                    "命中判定基于服务端分组聚合值（sum 口径）：组内正负库存相抵后 ≤0 记为无货",
                    "成员×期缺失的组不参与判定（数据里没有该组合的行）"]),
            })
            if granularity == "row":
                classification["evidence"] = {
                    "columns": [group_by_column, time_column, metric_columns[0], metric_columns[1]],
                    "scannedRows": len(rows), "truncated": False,
                    "note": "row 粒度逐行判定；命中样本见各分类 samples（≤50 条有界）",
                }
            else:
                classification["evidence"] = result
            return classification
        result = _psi_period_query(engine, entry, dataset, dataset_id, args, [time_column],
                                    metric_columns, cancel_check=cancel_check,
                                    timeout_seconds=timeout_seconds)
        result["rows"] = _calendar_order_rows(result.get("rows") or [])
        labels = [row[0] for row in result.get("rows") or [] if isinstance(row, list) and row]
        inferred = date_parse.infer_grain(labels, time_column)
        if name == "inventory_weeks":
            grain = args.get("grain") or inferred
            try:
                grain = psi_normalize_grain(grain)
            except Exception as exc:
                raise ToolContractError(
                    f"timeColumn「{time_column}」的值无法解析为周/月/季/年粒度"
                    f"（infer={inferred!r}）；请显式声明 grain") from exc
            raw_series = _psi_period_series(result, metric_columns)
            series = [{"period": record["period"],
                        "sellOut": record[metric_columns[0]],
                        "inventory": record[metric_columns[1]]} for record in raw_series]
            weeks = psi_weeks_of_supply(series, grain=grain,
                                         window=int(args.get("window") or 3))
            weeks.update({
                "kind": "inventory_weeks", "datasetId": dataset_id,
                "timeColumn": time_column, "grain": grain, "causalClaim": False,
                "columns": {"sellOut": metric_columns[0], "inventory": metric_columns[1]},
                "assumptions": [
                    f"在库周数 = 期末库存 ÷（当期 {metric_columns[0]} ÷ 期内周数）；"
                    f"{grain} 粒度每期按 {weeks['grainWeeks']:.4f} 周换算",
                    "当期销量为 0/缺失时不编造周数（weeksOfSupply=null）",
                ],
                "evidence": result,
            })
            return weeks
        # psi_balance_check
        if inferred is None:
            raise ToolContractError(
                f"timeColumn「{time_column}」的值无法解析为时间粒度"
                f"（如 202501/2026-W35/2026-09-05）；库存恒等式是按期的勾稽，"
                "请声明一个真实的时间列")
        grain = inferred
        raw_series = _psi_period_series(result, metric_columns)
        series = [{"period": record["period"],
                    "sellIn": record[metric_columns[0]],
                    "sellOut": record[metric_columns[1]],
                    "inventory": record[metric_columns[2]]} for record in raw_series]
        # XC-02 容差分档：相对档缺省 1e-6（与旧口径一致），绝对档缺省 0.5（整数
        # 库存半个单位）；实际生效的一对随 toleranceUsed 报告，判定口径可审计。
        rel_tolerance = _psi_tolerance_arg(args, "tolerance", 1e-6, 1.0)
        abs_tolerance = _psi_tolerance_arg(args, "absTolerance", 0.5, 1000.0)
        balance = psi_balance_periods(series, grain=grain, tolerance=rel_tolerance,
                                      abs_tolerance=abs_tolerance)
        assumptions = [
            f"恒等式：期末库存 = 期初库存 + {metric_columns[0]} − {metric_columns[1]}；"
            f"期初取上一期观测期末库存（sum 口径，服务端聚合）",
            "首期无期初库存，标记 unverifiable，不假设期初为 0",
            f"容差分档：balanced ⇔ |Δ| ≤ max(相对 {rel_tolerance:g}×规模, 绝对 {abs_tolerance:g})；"
            "规模 = max(|期初|,|期末|,|流入|,|流出|)",
        ]
        if grain is None:
            assumptions.append("时间列无法解析为日历粒度，断档期检测跳过（勾稽本身不受影响）")
        return {"kind": "psi_balance_check", "datasetId": dataset_id,
                "timeColumn": time_column, "grain": grain,
                "columns": {"sellIn": metric_columns[0], "sellOut": metric_columns[1],
                             "inventory": metric_columns[2]},
                "periods": balance["records"], "gaps": balance["gaps"],
                "summary": balance["summary"], "causalClaim": False,
                "toleranceUsed": balance["toleranceUsed"],
                "assumptions": assumptions, "evidence": result}
    raise ToolContractError(f"tool {name!r} is not wired for execution")
