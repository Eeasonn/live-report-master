"""Validation and deterministic ordering for typed Agent plans."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from .agent_tools import ToolContractError, ToolRegistry


def _text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ToolContractError(f"{field} must be a non-empty string <= {limit} chars")
    return value.strip()


def _bounded_int(value: Any, field: str, low: int, high: int, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ToolContractError(f"{field} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ToolContractError(f"{field} must be an integer") from exc
    if number < low or number > high:
        raise ToolContractError(f"{field} must be between {low} and {high}")
    return number


def normalize_plan(raw: Mapping[str, Any], registry: ToolRegistry, *, role: str = "viewer",
                   context: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Validate a model plan without executing any step or side effect."""
    if not isinstance(raw, Mapping) or raw.get("kind") != "plan":
        raise ToolContractError("plan kind must be 'plan'")
    goal = _text(raw.get("goal"), "goal", 500)
    raw_context = raw.get("context") or {}
    if not isinstance(raw_context, Mapping):
        raise ToolContractError("plan context must be an object")
    merged_context = dict(context or {})
    for key in ("reportId", "chartId", "contextKey", "datasetRevision", "filterScopes"):
        if key in raw_context:
            merged_context[key] = raw_context[key]
    if not merged_context.get("reportId"):
        raise ToolContractError("plan context requires reportId")
    if merged_context.get("chartId") and not merged_context.get("contextKey") \
            and not merged_context.get("datasetRevision"):
        raise ToolContractError("card-scoped plan context requires contextKey or datasetRevision")
    for key in ("reportId", "chartId", "contextKey"):
        if key in merged_context and merged_context[key] is not None:
            limit = 32000 if key == "contextKey" else 240
            merged_context[key] = _text(merged_context[key], f"context.{key}", limit)
    if "datasetRevision" in merged_context and merged_context["datasetRevision"] is not None:
        revision = merged_context["datasetRevision"]
        if isinstance(revision, bool) or not isinstance(revision, (str, int, float, Mapping, list)):
            raise ToolContractError("context.datasetRevision must be JSON-compatible")
        try:
            if isinstance(revision, str):
                parsed_revision = json.loads(revision)
                revision_text = json.dumps(parsed_revision, ensure_ascii=False,
                                           sort_keys=True, separators=(",", ":"))
            else:
                revision_text = json.dumps(revision, ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ToolContractError("context.datasetRevision must be JSON-compatible") from exc
        if not revision_text or len(revision_text) > 512:
            raise ToolContractError("context.datasetRevision must be <= 512 chars")
        merged_context["datasetRevision"] = revision_text
    if "filterScopes" in merged_context and merged_context["filterScopes"] is not None:
        # The planner has no report manifest, so it validates the typed shape
        # and bounds here; the app/worker revalidates columns against the
        # current chart dataset before hashing or executing the plan.
        try:
            from .copilot import _FILTER_SCOPE_SET, _FILTER_SCOPE_NAMES, _normalize_filter_collection
            scopes = merged_context["filterScopes"]
            if not isinstance(scopes, Mapping) or set(scopes) - _FILTER_SCOPE_SET:
                raise ValueError("filterScopes must contain only global/tab/card/local")
            normalized_scopes = {}
            total = 0
            from .copilot import _filter_leaf_count
            for scope_name in _FILTER_SCOPE_NAMES:
                items = _normalize_filter_collection(
                    scopes.get(scope_name), None, f"context.filterScopes.{scope_name}")
                normalized_scopes[scope_name] = items
                total += sum(_filter_leaf_count(item) for item in items)
            if total > 10:
                raise ValueError("filterScopes exceeds 10 predicates")
            merged_context["filterScopes"] = normalized_scopes
        except Exception as exc:
            raise ToolContractError(f"context.filterScopes is invalid: {exc}") from exc
    steps = raw.get("steps")
    if not isinstance(steps, list) or not steps or len(steps) > 12:
        raise ToolContractError("plan steps must contain 1-12 items")

    normalized: list[dict[str, Any]] = []
    ids: set[str] = set()
    for step in steps:
        if not isinstance(step, Mapping):
            raise ToolContractError("each plan step must be an object")
        step_id = _text(step.get("id"), "step.id", 80)
        if step_id in ids:
            raise ToolContractError(f"duplicate step id: {step_id}")
        ids.add(step_id)
        tool = _text(step.get("tool"), "step.tool", 80)
        spec = registry.get(tool)
        if role not in spec.permissions:
            raise ToolContractError(f"role {role!r} cannot use tool {tool!r}")
        args = step.get("args") or {}
        if not isinstance(args, Mapping):
            raise ToolContractError(f"step {step_id} args must be an object")
        try:
            args_size = len(json.dumps(args, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise ToolContractError(f"step {step_id} args must be JSON-compatible") from exc
        if args_size > 64 * 1024:
            raise ToolContractError(f"step {step_id} args exceed 64KiB")
        registry.validate_args(tool, args)
        deps = step.get("dependsOn") or []
        if not isinstance(deps, list) or not all(isinstance(dep, str) for dep in deps):
            raise ToolContractError(f"step {step_id} dependsOn must be a string list")
        expected = step.get("expectedEvidence") or []
        if not isinstance(expected, list) or len(expected) > 8 or not all(isinstance(item, str) for item in expected):
            raise ToolContractError(f"step {step_id} expectedEvidence must be a string list of <=8 items")
        normalized.append({
            "id": step_id,
            "tool": spec.name,
            "args": dict(args),
            "dependsOn": list(dict.fromkeys(deps)),
            "requiresApproval": spec.requires_approval,
            "approved": bool(step.get("approved")),
            "expectedEvidence": [_text(item, f"step {step_id}.expectedEvidence", 120) for item in expected],
        })
    _topological_order(normalized)
    termination = _text(raw.get("termination") or "完成所有步骤并取得可验证证据", "termination", 300)
    budget = raw.get("budget") or {}
    if not isinstance(budget, Mapping):
        raise ToolContractError("plan budget must be an object")
    normalized_budget = {
        "maxSteps": _bounded_int(budget.get("maxSteps"), "budget.maxSteps", 1, 12, min(12, len(normalized))),
        "maxEvidenceBytes": _bounded_int(budget.get("maxEvidenceBytes"), "budget.maxEvidenceBytes", 16 * 1024, 512 * 1024, 512 * 1024),
        "maxSeconds": _bounded_int(budget.get("maxSeconds"), "budget.maxSeconds", 1, 120, 120),
        # Query budgets are independent from response/evidence budgets: a
        # small JSON result can still require scanning millions of rows.
        "maxScanRows": _bounded_int(budget.get("maxScanRows"), "budget.maxScanRows", 1, 2_000_000, 500_000),
        "maxGroups": _bounded_int(budget.get("maxGroups"), "budget.maxGroups", 1, 100_000, 50_000),
    }
    acceptance = raw.get("acceptanceTests") or []
    if not isinstance(acceptance, list) or len(acceptance) > 8 or not all(isinstance(item, str) for item in acceptance):
        raise ToolContractError("acceptanceTests must be a string list of <=8 items")
    return {"kind": "plan", "version": 1, "goal": goal, "context": merged_context,
            "steps": normalized, "termination": termination, "budget": normalized_budget,
            "acceptanceTests": [_text(item, "acceptanceTests", 160) for item in acceptance]}


def _topological_order(steps: list[Mapping[str, Any]]) -> list[str]:
    ids = {str(step["id"]) for step in steps}
    deps = {str(step["id"]): set(step.get("dependsOn") or []) for step in steps}
    for step_id, requirements in deps.items():
        unknown = requirements - ids
        if unknown:
            raise ToolContractError(f"step {step_id} depends on unknown step(s): {sorted(unknown)}")
    ordered: list[str] = []
    while deps:
        ready = sorted(step_id for step_id, requirements in deps.items() if not requirements)
        if not ready:
            raise ToolContractError("plan dependency cycle detected")
        ordered.extend(ready)
        for step_id in ready:
            deps.pop(step_id)
        for requirements in deps.values():
            requirements.difference_update(ready)
    return ordered


def topological_order(steps: list[Mapping[str, Any]]) -> list[str]:
    """Return the deterministic execution order for an already-normalized plan.

    Keeping this operation next to plan validation prevents a second executor-
    specific dependency implementation.  The order is lexical among ready
    nodes, so retries and evidence traces are reproducible.
    """
    return _topological_order(steps)


def plan_hash(plan: Mapping[str, Any]) -> str:
    payload = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def preview_plan(plan: Mapping[str, Any], entry: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Build a side-effect-free impact preview for proposal steps.

    The planner still owns ordering/schema; proposal validation owns field and
    Flint aggregation legality.  Keeping this seam separate means plan
    validation can show an impact diff without applying a manifest or invoking
    a tool.
    """
    previews: list[dict[str, Any]] = []
    for step in plan.get("steps") or []:
        tool = str(step.get("tool") or "")
        args = step.get("args") if isinstance(step.get("args"), Mapping) else {}
        item: dict[str, Any] = {
            "stepId": step.get("id"), "tool": tool,
            "requiresApproval": bool(step.get("requiresApproval")),
        }
        if tool == "propose_chart_change":
            try:
                from .agent_proposals import validate_proposal
                item["kind"] = "proposal"
                item["proposal"] = validate_proposal(entry, args)
            except Exception as exc:
                item["kind"] = "proposal"
                item["valid"] = False
                item["error"] = str(exc)[:300]
        elif tool == "propose_multi_card_change":
            item["kind"] = "proposal"
            raw = args.get("proposals") if isinstance(args, Mapping) else None
            checks = []
            if isinstance(raw, list):
                from .agent_proposals import validate_proposal
                for proposal in raw[:8]:
                    try:
                        checks.append(validate_proposal(entry, proposal))
                    except Exception as exc:
                        checks.append({"valid": False, "error": str(exc)[:300]})
            else:
                checks.append({"valid": False, "error": "proposals must be an array"})
            item["proposals"] = checks
        else:
            item["kind"] = "read" if not item["requiresApproval"] else "approval"
        previews.append(item)
    return previews
