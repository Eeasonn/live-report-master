"""Deep, typed seam for Report Agent tools.

The model can propose a tool name and JSON arguments, but only this registry
decides whether the tool exists, what it may read/write, and whether it needs
approval. Implementations are registered by the server layer; this module does
not execute SQL, mutate reports, or call a provider.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping


class ToolContractError(ValueError):
    """A plan or tool call violates the server-owned tool contract."""


def validate_input_schema(schema: Mapping[str, Any], value: Mapping[str, Any], *, path: str = "args") -> None:
    """Small dependency-free JSON-Schema subset for typed Agent arguments.

    The skill deliberately has no pip dependency.  This covers the contract
    features used by the registry (object/array/scalar types, required,
    enum, min/max and additionalProperties) and fails closed before a tool
    reaches the SQL/manifest seam.
    """
    if not isinstance(value, Mapping):
        raise ToolContractError(f"{path} must be an object")
    if schema.get("type") not in (None, "object"):
        raise ToolContractError(f"{path} schema must be an object")
    required = schema.get("required") or []
    for name in required:
        if name not in value or value[name] is None or value[name] == "":
            raise ToolContractError(f"{path}.{name} is required")
    properties = schema.get("properties") or {}
    if schema.get("additionalProperties") is False:
        unknown = sorted(set(value) - set(properties))
        if unknown:
            raise ToolContractError(f"{path}.{unknown[0]} is not allowed")

    def matches_type(item: Any, expected: Any) -> bool:
        names = expected if isinstance(expected, list) else [expected]
        for kind in names:
            if kind == "string" and isinstance(item, str):
                return True
            if kind == "number" and isinstance(item, (int, float)) and not isinstance(item, bool):
                return True
            if kind == "integer" and isinstance(item, int) and not isinstance(item, bool):
                return True
            if kind == "boolean" and isinstance(item, bool):
                return True
            if kind == "array" and isinstance(item, list):
                return True
            if kind == "object" and isinstance(item, Mapping):
                return True
            if kind == "null" and item is None:
                return True
        return False

    for name, rule in properties.items():
        if name not in value or value[name] is None:
            continue
        item = value[name]
        if not isinstance(rule, Mapping):
            continue
        if rule.get("type") and not matches_type(item, rule["type"]):
            raise ToolContractError(f"{path}.{name} has invalid type")
        if "enum" in rule and item not in rule["enum"]:
            raise ToolContractError(f"{path}.{name} must be one of {rule['enum']}")
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            if rule.get("minimum") is not None and item < float(rule["minimum"]):
                raise ToolContractError(f"{path}.{name} must be >= {rule['minimum']}")
            if rule.get("maximum") is not None and item > float(rule["maximum"]):
                raise ToolContractError(f"{path}.{name} must be <= {rule['maximum']}")
        if isinstance(item, list):
            if rule.get("minItems") is not None and len(item) < int(rule["minItems"]):
                raise ToolContractError(f"{path}.{name} has too few items")
            if rule.get("maxItems") is not None and len(item) > int(rule["maxItems"]):
                raise ToolContractError(f"{path}.{name} has too many items")
            item_schema = rule.get("items")
            if isinstance(item_schema, Mapping):
                for index, child in enumerate(item):
                    child_path = f"{path}.{name}[{index}]"
                    if item_schema.get("type") == "object":
                        validate_input_schema(item_schema, child, path=child_path)
                    elif item_schema.get("type") and not matches_type(child, item_schema["type"]):
                        raise ToolContractError(f"{child_path} has invalid type")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    output_schema: Mapping[str, Any] = field(default_factory=dict)
    permissions: frozenset[str] = frozenset({"viewer"})
    estimated_cost: str = "low"
    timeout_seconds: int = 30
    cancellable: bool = True
    side_effect: str = "none"  # none | proposal | write | notify
    semantic_references: tuple[str, ...] = ()
    acceptance_tests: tuple[str, ...] = ()
    handler: Callable[..., Any] | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.name.replace("_", "").isalnum():
            raise ToolContractError("tool name must be an identifier")
        if self.estimated_cost not in {"low", "medium", "high"}:
            raise ToolContractError("estimated_cost must be low, medium or high")
        if self.side_effect not in {"none", "proposal", "write", "notify"}:
            raise ToolContractError("unsupported tool side_effect")
        if self.timeout_seconds < 1 or self.timeout_seconds > 3600:
            raise ToolContractError("tool timeout must be between 1 and 3600 seconds")
        if any(not isinstance(item, str) or not item.strip() or len(item) > 240
               for item in self.acceptance_tests):
            raise ToolContractError("acceptance_tests must contain bounded non-empty strings")

    @property
    def requires_approval(self) -> bool:
        return self.side_effect != "none"


class ToolRegistry:
    """Small interface hiding registration, lookup, permission and execution."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ToolContractError(f"duplicate tool: {spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolContractError(f"unknown tool: {name}") from exc

    def list(self) -> list[ToolSpec]:
        return [self._tools[name] for name in sorted(self._tools)]

    def authorize(self, name: str, role: str, *, approved: bool = False) -> ToolSpec:
        spec = self.get(name)
        if role not in spec.permissions:
            raise ToolContractError(f"role {role!r} cannot use tool {name!r}")
        if spec.requires_approval and not approved:
            raise ToolContractError(f"tool {name!r} requires approval")
        return spec

    def validate_args(self, name: str, args: Mapping[str, Any]) -> ToolSpec:
        spec = self.get(name)
        validate_input_schema(spec.input_schema, args, path=f"{name}.args")
        return spec

    def execute(self, name: str, args: Mapping[str, Any], *, role: str = "viewer",
                approved: bool = False, **runtime: Any) -> Any:
        spec = self.authorize(name, role, approved=approved)
        if not isinstance(args, Mapping):
            raise ToolContractError("tool args must be an object")
        validate_input_schema(spec.input_schema, args, path=f"{name}.args")
        if spec.handler is None:
            raise ToolContractError(f"tool {name!r} has no server handler")
        return spec.handler(dict(args), **runtime)


def default_registry() -> ToolRegistry:
    """Return declarations only; handlers are wired by the server adapter."""
    registry = ToolRegistry()
    proposal_tools = {
        "propose_chart_change", "propose_filter_change", "propose_multi_card_change",
        "propose_report_narrative", "propose_data_update", "propose_alias_confirmation",
        "propose_derived_measure",
    }
    approval_tools = {"export_evidence", "export_large_evidence"}
    read_schema = {"type": "object", "additionalProperties": False}
    text = {"type": "string"}
    calendar = {"type": "object", "additionalProperties": False,
                "properties": {"kind": {"type": "string", "enum": ["gregorian", "fiscal"]},
                               "fiscalStartMonth": {"type": "integer", "minimum": 1, "maximum": 12},
                               "weekStart": {"type": "integer", "enum": [0]}}}
    dataset_measure = {"type": "object", "required": ["datasetId", "measure"],
                       "additionalProperties": False,
                       "properties": {"datasetId": text, "measure": text,
                                       "dimension": text, "aggregation": {"type": "string", "enum": ["sum", "average", "count", "min", "max"]},
                                       "limit": {"type": "integer"}, "dimensions": {"type": "array", "items": text},
                                       "filter": {"type": ["array", "object"]}, "bucket": {"type": ["object", "null"]},
                                       "chartId": text, "breakdown": text, "grain": text,
                                       "hierarchyPath": {"type": "array", "minItems": 1, "maxItems": 6, "items": text},
                                       "labels": {"type": "object"},
                                       "calendar": calendar,
                                       "timeWindow": {"type": ["object", "null"]},
                                       "maxScanRows": {"type": "integer", "minimum": 1, "maximum": 2000000},
                                       "maxGroups": {"type": "integer", "minimum": 1, "maximum": 100000}}}
    what_if_constraints = {"type": "object", "additionalProperties": False,
                           "properties": {"min": {"type": ["number", "string"]},
                                          "max": {"type": ["number", "string"]},
                                          "nonNegative": {"type": "boolean"}}}
    what_if_link = {"type": "object", "additionalProperties": False,
                    "required": ["source", "target"],
                    "properties": {"source": text, "target": text,
                                    "ratio": {"type": ["number", "string"]},
                                    "offset": {"type": ["number", "string"]}}}
    root_cause_multi = {"type": "object", "additionalProperties": False,
                        "required": ["datasetId", "dimension", "measures"],
                        "properties": {key: value for key, value in dataset_measure["properties"].items()
                                       if key not in {"measure", "limit", "dimensions", "breakdown"}}
                        }
    root_cause_multi["properties"]["measures"] = {
        "type": "array", "minItems": 1, "maxItems": 4, "items": text}
    schemas = {
        "resolve_card": {"type": "object", "additionalProperties": False, "required": ["chartId"], "properties": {"chartId": text}},
        "get_semantic_context": {"type": "object", "additionalProperties": False, "required": ["chartId"], "properties": {"chartId": text}},
        "resolve_field": {"type": "object", "additionalProperties": False, "required": ["chartId", "name"], "properties": {"chartId": text, "name": text, "confirm": {"type": "boolean"}}},
        "propose_alias_confirmation": {"type": "object", "additionalProperties": False,
                                        "required": ["chartId", "name", "canonical"],
                                        "properties": {"chartId": text, "name": text, "canonical": text}},
        "reconcile_cards": {"type": "object", "additionalProperties": False, "required": ["chartIds"], "properties": {"chartIds": {"type": "array", "minItems": 1, "maxItems": 12, "items": text}}},
        "aggregate": dataset_measure,
        "aggregate_multi": {"type": "object", "additionalProperties": False, "required": ["requests"], "properties": {"chartId": text, "requests": {"type": "array", "minItems": 1, "maxItems": 8, "items": dataset_measure}}},
        "compare_periods": dataset_measure,
        "contribution": dataset_measure,
        "drilldown": dataset_measure,
        "anomaly_scan": dataset_measure,
        "root_cause_scan": dataset_measure,
        "root_cause_multi": root_cause_multi,
        "trend_inflection": dataset_measure,
        "recommend_actions": {"type": "object", "additionalProperties": False,
                               "required": ["datasetId", "dimension", "measure"],
                               "properties": {**dataset_measure["properties"],
                                              "actionLimit": {"type": "integer", "minimum": 1, "maximum": 6}}},
        "what_if": {"type": "object", "additionalProperties": False, "properties": {
            "value": {"type": ["number", "string"]},
            "deltaPercent": {"type": ["number", "string"]},
            "absoluteDelta": {"type": ["number", "string"]},
            "constraints": what_if_constraints,
            "links": {"type": "array", "minItems": 1, "maxItems": 12, "items": what_if_link},
            "scenarios": {"type": "array", "minItems": 1, "maxItems": 12, "items": {
                "type": "object", "additionalProperties": False, "required": ["name", "value"],
                "properties": {"name": text, "value": {"type": ["number", "string"]},
                               "deltaPercent": {"type": ["number", "string"]},
                               "absoluteDelta": {"type": ["number", "string"]},
                               "constraints": what_if_constraints}}},
        }},
        "data_quality": {"type": "object", "additionalProperties": False, "required": ["datasetId"], "properties": {
            "datasetId": text, "maxScanRows": {"type": "integer", "minimum": 1, "maximum": 500000},
            "keyColumns": {"type": "array", "maxItems": 4, "items": text}, "calendar": calendar,
            "timeColumn": text}},
        "profile_dataset": {"type": "object", "additionalProperties": False, "required": ["datasetId"], "properties": {"datasetId": text}},
        # NA-05 PSI domain pack (read-only). Column names are ALWAYS declared by
        # the caller — the server never guesses which column is SI/SO/INV.
        "psi_balance_check": {"type": "object", "additionalProperties": False,
                               "required": ["datasetId", "timeColumn", "sellInColumn",
                                             "sellOutColumn", "inventoryColumn"],
                               "properties": {"datasetId": text, "timeColumn": text,
                                               "sellInColumn": text, "sellOutColumn": text,
                                               "inventoryColumn": text,
                                               "tolerance": {"type": "number", "minimum": 0, "maximum": 1},
                                               "absTolerance": {"type": "number", "minimum": 0, "maximum": 1000},
                                               "filter": {"type": ["array", "object"]},
                                               "maxScanRows": {"type": "integer", "minimum": 1, "maximum": 2000000},
                                               "maxGroups": {"type": "integer", "minimum": 1, "maximum": 100000}}},
        "inventory_weeks": {"type": "object", "additionalProperties": False,
                             "required": ["datasetId", "timeColumn", "sellOutColumn", "inventoryColumn"],
                             "properties": {"datasetId": text, "timeColumn": text,
                                             "sellOutColumn": text, "inventoryColumn": text,
                                             "grain": {"type": "string", "enum": ["week", "month", "quarter", "year"]},
                                             "window": {"type": "integer", "minimum": 1, "maximum": 12},
                                             "filter": {"type": ["array", "object"]},
                                             "maxScanRows": {"type": "integer", "minimum": 1, "maximum": 2000000},
                                             "maxGroups": {"type": "integer", "minimum": 1, "maximum": 100000}}},
        "stockout_scan": {"type": "object", "additionalProperties": False,
                           "required": ["datasetId", "timeColumn", "sellOutColumn",
                                         "inventoryColumn", "groupBy"],
                           "properties": {"datasetId": text, "timeColumn": text,
                                           "sellOutColumn": text, "inventoryColumn": text,
                                           "groupBy": text,
                                           "granularity": {"type": "string", "enum": ["sum", "row"]},
                                           "filter": {"type": ["array", "object"]},
                                           "topMembers": {"type": "integer", "minimum": 1, "maximum": 50},
                                           "maxScanRows": {"type": "integer", "minimum": 1, "maximum": 2000000},
                                           "maxGroups": {"type": "integer", "minimum": 1, "maximum": 100000}}},
        "propose_chart_change": {"type": "object", "additionalProperties": False, "properties": {
            "chartId": text, "datasetId": text, "dimension": text, "measure": text,
            "breakdown": text, "aggregation": {"type": "string", "enum": ["sum", "average", "count", "min", "max"]},
            "viewType": text, "bucket": text, "topN": {"type": "integer", "minimum": 0, "maximum": 20}}},
        "propose_filter_change": {"type": "object", "additionalProperties": False, "properties": {
            "chartId": text, "filter": {"type": ["array", "object"]}}},
        "propose_multi_card_change": {"type": "object", "additionalProperties": False, "properties": {
            "proposals": {"type": "array", "minItems": 1, "maxItems": 8, "items": {"type": "object"}}}},
        "propose_report_narrative": {"type": "object", "additionalProperties": False, "properties": {
            "title": text, "body": text}},
        "propose_data_update": {"type": "object", "additionalProperties": False, "properties": {
            "datasetId": text, "mode": {"type": "string", "enum": ["replace", "upsert", "append"]},
            "keyCols": {"type": "array", "items": text}}},
        # NA-04: the measure node shape (formula/window/share/filtered) is NOT
        # duplicated here as JSON-Schema — ``scripts/derived_model`` is the
        # single validation source (unknown keys / DAG / window timeField /
        # filtered predicate whitelist) and this schema only bounds the
        # envelope before the typed seam runs. The schema validator only
        # recurses into arrays, so deeper object checks would be a second,
        # weaker copy of derived_model anyway.
        "propose_derived_measure": {"type": "object", "additionalProperties": False,
                                     "required": ["datasetId", "measure"],
                                     "properties": {"datasetId": text, "measure": {"type": "object"}}},
        "export_evidence": {"type": "object", "additionalProperties": False, "properties": {
            "jobId": text, "format": {"type": "string", "enum": ["json", "csv", "markdown"]}}},
        "export_large_evidence": {"type": "object", "additionalProperties": False, "properties": {
            "jobId": text, "format": {"type": "string", "enum": ["json", "csv", "markdown"]}}},
        "create_monitor": {"type": "object", "additionalProperties": False, "properties": {
            "name": text, "tool": text, "args": {"type": "object"},
            "intervalSeconds": {"type": "integer", "minimum": 30, "maximum": 86400}}},
    }
    for name, cost, timeout in (
        ("resolve_card", "low", 10),
        ("get_semantic_context", "low", 10),
        ("resolve_field", "low", 10),
        ("propose_alias_confirmation", "low", 20),
        ("reconcile_cards", "medium", 30),
        ("aggregate", "medium", 30),
        ("aggregate_multi", "medium", 45),
        ("compare_periods", "medium", 45),
        ("contribution", "medium", 45),
        ("drilldown", "high", 90),
        ("anomaly_scan", "high", 120),
        ("root_cause_scan", "high", 120),
        ("root_cause_multi", "high", 150),
        ("trend_inflection", "medium", 60),
        ("recommend_actions", "medium", 60),
        ("what_if", "low", 20),
        ("data_quality", "medium", 60),
        ("propose_chart_change", "low", 20),
        ("propose_filter_change", "low", 20),
        ("propose_multi_card_change", "medium", 30),
        ("propose_report_narrative", "low", 20),
        ("propose_data_update", "medium", 30),
        ("propose_derived_measure", "low", 20),
        ("export_evidence", "high", 180),
        ("export_large_evidence", "high", 180),
        ("profile_dataset", "medium", 60),
        ("create_monitor", "medium", 30),
        ("psi_balance_check", "medium", 60),
        ("inventory_weeks", "medium", 60),
        ("stockout_scan", "high", 120),
    ):
        semantic_refs = {
            "resolve_card": ("refs/flint/src/core/field-semantics.ts",),
            "get_semantic_context": ("refs/flint/src/core/field-semantics.ts",
                                      "refs/flint/src/core/resolve-semantics.ts"),
            "resolve_field": ("refs/flint/src/core/field-semantics.ts",),
            "propose_alias_confirmation": ("refs/flint/src/core/field-semantics.ts",),
            "aggregate": ("refs/flint/src/core/aggregate.ts",
                           "refs/flint/src/core/type-registry.ts"),
            "aggregate_multi": ("refs/flint/src/core/aggregate.ts",
                                 "refs/flint/src/core/type-registry.ts"),
            "contribution": ("refs/flint/src/core/aggregate.ts",
                              "refs/flint/src/core/filter-overflow.ts"),
            "drilldown": ("refs/flint/src/core/aggregate.ts",),
            "compare_periods": ("refs/flint/src/core/aggregate.ts",),
            "anomaly_scan": ("refs/flint/src/core/aggregate.ts",),
            "root_cause_scan": ("refs/flint/src/core/aggregate.ts",
                                 "refs/flint/src/core/filter-overflow.ts"),
            "root_cause_multi": ("refs/flint/src/core/aggregate.ts",
                                  "refs/flint/src/core/filter-overflow.ts"),
            "reconcile_cards": ("refs/flint/src/core/resolve-semantics.ts",
                                "refs/flint/src/core/aggregate.ts"),
            "trend_inflection": ("refs/flint/src/core/aggregate.ts",),
            "recommend_actions": ("refs/flint/src/core/aggregate.ts",
                                   "refs/flint/src/core/filter-overflow.ts"),
            "data_quality": ("refs/flint/src/core/field-semantics.ts",),
            "profile_dataset": ("refs/flint/src/core/field-semantics.ts",),
            "what_if": (),
            "propose_derived_measure": ("refs/flint/src/core/aggregate.ts",
                                         "refs/flint/src/core/type-registry.ts"),
            "psi_balance_check": ("refs/flint/src/core/aggregate.ts",
                                   "refs/flint/src/core/type-registry.ts"),
            "inventory_weeks": ("refs/flint/src/core/aggregate.ts",),
            "stockout_scan": ("refs/flint/src/core/aggregate.ts",
                               "refs/flint/src/core/filter-overflow.ts"),
        }.get(name, ("refs/flint/src/core/aggregate.ts",))
        # NA-04/NA-05: tools with a domain contract carry their own acceptance
        # tests; everything else keeps the generic bounded-evidence pair.
        custom_acceptance = {
            "propose_derived_measure": (
                "服务端复用 derived_model 校验（重名/未知列/DAG/窗口绑定/filtered 谓词），非法声明 fail-closed",
                "应用走 metadataOnly ProposalTransaction：snapshot/hash/rollback 齐备，回滚后 manifest 逐字节还原",
            ),
            "psi_balance_check": (
                "恒等式逐期勾稽（相对+绝对容差分档，toleranceUsed 报告判定口径），首期标记 unverifiable 不编造期初；断档期显式报告",
                "列名由调用方显式声明，敏感列 fail-closed；evidence 带 provenance 且 causalClaim=false",
            ),
            "inventory_weeks": (
                "换算口径（期内周数）写进 description 与 evidence.assumptions",
                "无销量期不编造周数（null），最新期与 N 期均值对比有界",
            ),
            "stockout_scan": (
                "两类命中（有销无货/有货无销）bounded top-N；granularity=sum（缺省，组内相抵）/row（逐行判定+命中样本≤50），超限 fail-closed 不静默截断",
                "列名由调用方显式声明，敏感列 fail-closed；evidence 带 provenance 且 causalClaim=false",
            ),
        }.get(name)
        if name == "create_monitor":
            registry.register(ToolSpec(name=name, estimated_cost=cost, timeout_seconds=timeout,
                                       permissions=frozenset({"editor", "admin"}),
                                       input_schema=schemas["create_monitor"],
                                       output_schema={"type": "object"},
                                       side_effect="write",
                                       acceptance_tests=("创建后可停用/删除且默认只读，不直接发送通知",)))
        else:
            side_effect = ("proposal" if name in proposal_tools
                           else "write" if name in approval_tools else "none")
            registry.register(ToolSpec(name=name, estimated_cost=cost, timeout_seconds=timeout,
                                       input_schema=schemas.get(name, read_schema),
                                       output_schema={"type": "object"},
                                       side_effect=side_effect,
                                       semantic_references=semantic_refs,
                                       acceptance_tests=custom_acceptance or (
                                           "服务端返回与工具 schema 相符的 bounded evidence",
                                           "取消/预算/权限边界不产生副作用",
                                       )))
    registry.register(ToolSpec(name="apply_proposal", permissions=frozenset({"editor"}),
                               input_schema={"type": "object", "additionalProperties": False,
                                             "properties": {"proposalId": {"type": "string"},
                                                             "approved": {"type": "boolean"}}},
                               side_effect="write", timeout_seconds=30,
                               semantic_references=("refs/flint/src/core/resolve-semantics.ts",),
                               acceptance_tests=("审批前不写入；审批后返回 snapshot/hash 校验结果",)))
    registry.register(ToolSpec(name="send_notification", permissions=frozenset({"editor", "admin"}),
                               input_schema={"type": "object", "additionalProperties": False,
                                             "properties": {"channel": {"type": "string"},
                                                             "destination": {"type": "string"},
                                                             "message": {"type": "string"},
                                                             "approved": {"type": "boolean"}}},
                               side_effect="notify", timeout_seconds=30,
                               acceptance_tests=("未审批或未 allowlist 时 fail-closed，并留下审计记录",)))
    return registry
