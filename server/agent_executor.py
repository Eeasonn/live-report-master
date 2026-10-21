"""Bounded execution loop for typed, read-first Agent plans.

This is intentionally a server seam rather than another model adapter.  A
normalized plan is executed in dependency order, each step is authorized by
the server-owned registry, and only the existing semantic ToolRunner may read
data.  Side-effecting steps stop at an explicit approval boundary; they are
never executed as a consequence of a model response.

Grounding: Flint aggregation is a caller-owned, pre-aggregation contract
(``refs/flint/src/core/aggregate.ts:7-20``), while channel meaning comes from
``refs/flint/src/core/resolve-semantics.ts``.  The executor therefore carries
typed tool arguments into ``agent_tool_runner`` and does not accept SQL,
ECharts options, or arbitrary model-generated code.
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .agent_planner import plan_hash, topological_order
from .agent_extensions import assess_confidence
from .agent_tool_runner import execute_read_tool
from .agent_tools import ToolContractError, ToolRegistry


class AgentExecutionError(RuntimeError):
    """A plan exceeded a server-side execution contract."""


def envelope(tool: str, args: Mapping[str, Any], value: Any) -> dict[str, Any]:
    """Attach server-owned evidence classification after a tool returns."""
    kind = "observed" if tool in {"aggregate", "aggregate_multi", "resolve_card",
                                  "get_semantic_context", "profile_dataset", "data_quality"} else "derived"
    semantic_refs = {
        "resolve_card": ["refs/flint/src/core/field-semantics.ts"],
        "get_semantic_context": ["refs/flint/src/core/field-semantics.ts",
                                 "refs/flint/src/core/resolve-semantics.ts"],
        "aggregate": ["refs/flint/src/core/aggregate.ts",
                       "refs/flint/src/core/type-registry.ts"],
        "aggregate_multi": ["refs/flint/src/core/aggregate.ts",
                             "refs/flint/src/core/type-registry.ts"],
        "derived_measure": ["refs/flint/src/core/aggregate.ts",
                             "refs/flint/src/core/type-registry.ts"],
        "contribution": ["refs/flint/src/core/aggregate.ts",
                          "refs/flint/src/core/filter-overflow.ts"],
        "drilldown": ["refs/flint/src/core/aggregate.ts"],
        "compare_periods": ["refs/flint/src/core/aggregate.ts"],
        "anomaly_scan": ["refs/flint/src/core/aggregate.ts"],
        "root_cause_scan": ["refs/flint/src/core/aggregate.ts",
                             "refs/flint/src/core/filter-overflow.ts"],
        "reconcile_cards": ["refs/flint/src/core/resolve-semantics.ts",
                            "refs/flint/src/core/aggregate.ts"],
        "trend_inflection": ["refs/flint/src/core/aggregate.ts"],
        "data_quality": ["refs/flint/src/core/field-semantics.ts"],
        "profile_dataset": ["refs/flint/src/core/field-semantics.ts"],
        "what_if": [],
    }.get(tool, ["refs/flint/src/core/aggregate.ts"])
    assumptions: list[str] = []
    limitations: list[str] = []
    confidence = {"level": "observed" if kind == "observed" else "derived",
                  "basis": "服务端真实数据/聚合" if kind == "observed" else "服务端规则或统计推导"}
    # The model is allowed to select a typed tool and its bounded arguments,
    # but it is never an evidence source.  Expose that distinction explicitly
    # so a downstream narrative layer cannot turn a model-selected hypothesis
    # into an observed fact.  This follows the prompt-injection boundary in
    # ``refs/security/owasp-llm-prompt-injection.md`` and the server-owned
    # aggregate seam in ``refs/flint/src/core/aggregate.ts:7-20``.
    inference = {
        "modelRole": "scenario_input" if tool == "what_if" else "typed_query_selector",
        "modelOutputUsedFor": "bounded tool arguments",
        "valueSource": "declared scenario input" if tool == "what_if" else "server tool result",
        "serverVerified": tool != "what_if",
    }
    if tool == "what_if":
        limitations = ["情景值来自声明的虚拟输入，不是服务端观测值"]
    else:
        limitations = []
    if tool == "compare_periods" and not ((args.get("labels") or {}).get("current") and
                                          (args.get("labels") or {}).get("previous")):
        assumptions.append("未提供期间标签时按服务端返回标签的确定性顺序选择前两期")
    if isinstance(value, Mapping) and value.get("truncated"):
        limitations.append("结果按查询 limit 截断，不能代表完整成员集合")
        confidence = {"level": "bounded", "basis": "结果含服务端截断，不能外推完整集合"}
        if "refs/flint/src/core/filter-overflow.ts" not in semantic_refs:
            semantic_refs = list(semantic_refs) + ["refs/flint/src/core/filter-overflow.ts"]
    if tool in {"anomaly_scan", "contribution"}:
        limitations.append("结论基于已聚合序列，不等同于因果解释")
    provenance = {"classification": kind, "tool": tool, "source": "server",
                  "generatedAt": datetime.now(timezone.utc).isoformat(),
                  "assumptions": assumptions, "limitations": limitations,
                  "confidence": confidence,
                  "inference": inference,
                  "causalClaim": False,
                  "semanticReferences": semantic_refs}
    # This is an explainable ordering signal only.  It is computed from the
    # typed provenance envelope, never from evidence values or model prose, and
    # is explicitly marked uncalibrated by assess_confidence().
    provenance["confidenceAssessment"] = assess_confidence(provenance)
    return provenance


def _check_cancel(cancel_check: Callable[[], bool] | None) -> None:
    if cancel_check is not None and cancel_check():
        raise AgentExecutionError("任务已取消")


def _bounded_json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))


def _contextualize_args(tool: str, args: Mapping[str, Any], plan: Mapping[str, Any],
                        entry: Mapping[str, Any]) -> dict[str, Any]:
    """Fill only safe defaults from the selected card; never invent fields."""
    output = dict(args)
    context = plan.get("context") or {}
    chart_id = context.get("chartId")
    if tool in {"resolve_card", "get_semantic_context", "aggregate", "aggregate_multi",
                "compare_periods", "contribution", "anomaly_scan", "trend_inflection",
                "root_cause_scan", "root_cause_multi", "drilldown", "data_quality", "profile_dataset", "reconcile_cards"} and chart_id:
        # The selected card is an isolation boundary; a model cannot silently
        # redirect a read step to another card while keeping the same context.
        output["chartId"] = chart_id
    charts = (entry.get("manifest") or {}).get("charts") or []
    chart = next((item for item in charts if isinstance(item, Mapping) and item.get("id") == chart_id), None)
    data = ((chart or {}).get("editor") or {}).get("data") or {}
    if tool in {"aggregate", "compare_periods", "contribution", "anomaly_scan", "root_cause_scan", "root_cause_multi", "trend_inflection", "drilldown"}:
        bound_dataset = data.get("datasetId")
        if bound_dataset and output.get("datasetId") and output.get("datasetId") != bound_dataset:
            raise ToolContractError("当前卡片已绑定另一个数据集；跨数据集查询必须使用显式对账工具")
        if not output.get("datasetId") and data.get("datasetId"):
            output["datasetId"] = data["datasetId"]
        if not output.get("dimension") and data.get("dimension"):
            output["dimension"] = data["dimension"]
        if not output.get("measure"):
            measures = data.get("measures") or []
            if measures:
                output["measure"] = measures[0]
    if tool in {"data_quality", "profile_dataset"} and data.get("datasetId"):
        if output.get("datasetId") and output.get("datasetId") != data.get("datasetId"):
            raise ToolContractError("当前卡片已绑定另一个数据集；跨数据集查询必须使用显式对账工具")
        output["datasetId"] = data["datasetId"]
    if tool == "aggregate_multi" and data.get("datasetId"):
        requests = output.get("requests")
        if isinstance(requests, list):
            for request in requests:
                if isinstance(request, Mapping) and request.get("datasetId") not in (None, data.get("datasetId")):
                    raise ToolContractError("当前卡片已绑定另一个数据集；跨数据集查询必须使用显式对账工具")
    # The browser's global/tab/card/local session scopes are carried in the
    # immutable plan context. Every read query receives them as an outer AND;
    # independent any groups stay independent (never flatten into one OR).
    filter_tools = {"aggregate", "aggregate_multi", "compare_periods", "contribution",
                    "drilldown", "anomaly_scan", "root_cause_scan", "root_cause_multi",
                    "trend_inflection", "recommend_actions"}
    if tool in filter_tools and context.get("filterScopes") is not None:
        from .copilot import merge_typed_filters
        scopes = context.get("filterScopes")
        if tool == "aggregate_multi":
            requests = output.get("requests")
            if not isinstance(requests, list):
                raise ToolContractError("aggregate_multi.requests must be an array")
            updated = []
            for request in requests:
                if not isinstance(request, Mapping):
                    raise ToolContractError("aggregate_multi request must be an object")
                merged, _ = merge_typed_filters(scopes, request.get("filter"))
                item = dict(request)
                item["filter"] = merged
                updated.append(item)
            output["requests"] = updated
        else:
            merged, _ = merge_typed_filters(scopes, output.get("filter"))
            output["filter"] = merged
    return output


def execute_plan(plan: Mapping[str, Any], registry: ToolRegistry, *, engine,
                 entry: Mapping[str, Any], role: str = "viewer",
                 cancel_check: Callable[[], bool] | None = None,
                 progress: Callable[[str], None] | None = None,
                 max_steps: int = 12, max_evidence_bytes: int = 512 * 1024,
                 max_seconds: float = 120.0) -> dict[str, Any]:
    """Execute a validated read-only plan and return an evidence trace.

    ``plan`` must already be normalized.  The function still checks its shape
    and hashable content at the execution boundary because callers may be LAN
    clients or a persisted job payload.  A step result is never silently
    fabricated: unsupported tools and approval-required tools are represented
    as an explicit blocked step and the plan terminates with ``needsApproval``.
    """
    if not isinstance(plan, Mapping) or plan.get("kind") != "plan":
        raise AgentExecutionError("只能执行已规范化的 plan")
    context = plan.get("context") or {}
    if isinstance(context, Mapping) and context.get("chartId") \
            and not context.get("contextKey") and not context.get("datasetRevision"):
        raise AgentExecutionError("card-scoped plan lacks contextKey/datasetRevision")
    if isinstance(context, Mapping) and context.get("chartId") and context.get("datasetRevision"):
        from .copilot import CopilotError, normalize_filter_scopes, validate_context_key, validate_dataset_revision
        try:
            # Recheck at worker execution time, not only at HTTP enqueue time:
            # a queued plan may wait while a same-sized dataset replacement
            # advances dataRevision.
            validate_dataset_revision(entry, str(context["chartId"]), context["datasetRevision"])
        except CopilotError as exc:
            raise AgentExecutionError(str(exc)) from exc
    if isinstance(context, Mapping) and context.get("chartId") and context.get("filterScopes") is not None:
        from .copilot import CopilotError, normalize_filter_scopes, validate_context_key
        try:
            scopes = normalize_filter_scopes(entry, str(context["chartId"]), context["filterScopes"])
            # A plan may not be reused after the browser changed a local
            # session filter. The context key includes the canonical scopes.
            if context.get("contextKey"):
                validate_context_key(entry, str(context["chartId"]), context["contextKey"], scopes)
            context["filterScopes"] = scopes
        except CopilotError as exc:
            raise AgentExecutionError(str(exc)) from exc
    steps = plan.get("steps")
    declared_budget = plan.get("budget") or {}
    if not isinstance(declared_budget, Mapping):
        raise AgentExecutionError("plan budget must be an object")
    max_steps = min(max_steps, int(declared_budget.get("maxSteps", max_steps)))
    max_evidence_bytes = min(max_evidence_bytes, int(declared_budget.get("maxEvidenceBytes", max_evidence_bytes)))
    max_seconds = min(max_seconds, float(declared_budget.get("maxSeconds", max_seconds)))
    # A direct LAN caller may bypass ``normalize_plan``.  Reject non-finite
    # budgets explicitly; ``nan`` would make both the deadline comparison and
    # per-step timeout checks false, turning a bounded Agent plan into an
    # effectively unbounded one.
    if max_steps < 1 or max_evidence_bytes < 1 or not math.isfinite(max_seconds) or max_seconds <= 0:
        raise AgentExecutionError("plan budget must be finite and positive")
    if not isinstance(steps, list) or not steps or len(steps) > max_steps:
        raise AgentExecutionError("plan steps exceed the execution budget")
    order = topological_order(steps)
    by_id = {str(step.get("id")): step for step in steps}
    if len(by_id) != len(steps):
        raise AgentExecutionError("plan contains duplicate step ids")
    started = time.monotonic()
    deadline = started + max_seconds
    results: dict[str, dict[str, Any]] = {}
    trace: list[dict[str, Any]] = []
    evidence_bytes = 0
    terminal = "completed"

    for step_id in order:
        _check_cancel(cancel_check)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AgentExecutionError("分析计划超过时间预算")
        step = by_id[step_id]
        dependencies = [str(dep) for dep in (step.get("dependsOn") or [])]
        blocked_by = [dep for dep in dependencies if results.get(dep, {}).get("status") != "done"]
        if blocked_by:
            item = {"id": step_id, "tool": step.get("tool"), "status": "skipped",
                    "blockedBy": blocked_by}
            trace.append(item)
            results[step_id] = item
            terminal = "blocked"
            continue
        tool = str(step.get("tool") or "")
        args = step.get("args") or {}
        if not isinstance(args, Mapping):
            raise AgentExecutionError(f"step {step_id} args must be an object")
        args = _contextualize_args(tool, args, plan, entry)
        # Carry the plan's hard query budgets into every data-reading step.
        # Explicit per-step values may tighten, but never bypass the plan cap.
        if tool in {"aggregate", "aggregate_multi", "compare_periods", "contribution",
                    "drilldown", "anomaly_scan", "root_cause_scan", "root_cause_multi", "trend_inflection", "data_quality"}:
            args = dict(args)
            plan_budget = declared_budget
            for field in ("maxScanRows", "maxGroups"):
                requested = args.get(field)
                ceiling = int(plan_budget.get(field) or 0)
                if requested is None:
                    args[field] = ceiling
                elif ceiling and int(requested) > ceiling:
                    raise AgentExecutionError(f"步骤 {step_id} 的 {field} 超出计划预算")
        try:
            spec = registry.get(tool)
            if role not in spec.permissions:
                raise ToolContractError(f"role {role!r} cannot use tool {tool!r}")
            registry.validate_args(tool, args)
        except ToolContractError as exc:
            raise AgentExecutionError(f"步骤 {step_id} 不可授权：{exc}") from exc
        if spec.requires_approval:
            item = {"id": step_id, "tool": tool, "status": "needsApproval",
                    "requiresApproval": True, "sideEffect": spec.side_effect}
            trace.append(item)
            results[step_id] = item
            terminal = "needsApproval"
            continue
        if progress:
            progress(f"执行 {tool}")
        try:
            value = execute_read_tool(tool, args, engine=engine, entry=entry,
                                      cancel_check=cancel_check,
                                      timeout_seconds=min(float(spec.timeout_seconds), remaining))
            if time.monotonic() > deadline:
                raise AgentExecutionError("分析计划超过时间预算")
        except (ToolContractError, ValueError, KeyError) as exc:
            item = {"id": step_id, "tool": tool, "status": "error",
                    "error": str(exc)[:500]}
            trace.append(item)
            results[step_id] = item
            terminal = "error"
            break
        _check_cancel(cancel_check)
        size = _bounded_json_size(value)
        evidence_bytes += size
        if evidence_bytes > max_evidence_bytes:
            raise AgentExecutionError("证据结果超过大小预算，请缩小筛选范围")
        item = {"id": step_id, "tool": tool, "status": "done", "evidence": value,
                "provenance": envelope(tool, args, value), "evidenceBytes": size}
        trace.append(item)
        results[step_id] = item

    return {"kind": "plan_result", "planHash": plan_hash(plan),
            "status": terminal, "steps": trace,
            "evidenceBytes": evidence_bytes,
            "elapsedMs": round((time.monotonic() - started) * 1000, 1),
            "answer": ("已完成分析计划并返回服务端证据。" if terminal == "completed"
                       else "计划包含需要审批的操作，已停在审批点。" if terminal == "needsApproval"
                       else "计划未能完成，已保留可诊断的执行轨迹。")}
