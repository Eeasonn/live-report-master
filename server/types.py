"""Internal types for the report server.

This module is the canonical type glossary for the server package.
Linters ignore it (no runtime side effects); treat docstrings here as
the source of truth that other modules reference.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ColumnSchema:
    """Schema for a single column in a loaded dataset."""
    name: str
    sql_type: str            # TEXT | INTEGER | REAL
    inferred_type: str        # dimension | measure | time | id
    non_null: int = 0
    distinct: int = 0
    min: Optional[float] = None
    max: Optional[float] = None
    mean: Optional[float] = None
    sum: Optional[float] = None
    sample_min: Optional[str] = None   # for time columns
    sample_max: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None or k in ("name", "sql_type", "inferred_type")}


@dataclass
class TableInfo:
    """Loaded table metadata held by the engine."""
    name: str
    columns: list[str]
    schema: list[ColumnSchema]
    row_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "columns": [c.to_dict() for c in self.schema],
            "rowCount": self.row_count,
        }


@dataclass
class AggregateRequest:
    """Parsed POST /aggregate body."""
    group_by: list[str] = field(default_factory=list)
    measures: list[dict[str, Any]] = field(default_factory=list)  # [{column, aggregation, divisor?}]
    bucket: Optional[dict[str, str]] = None                      # {source, bucketed, bucket}
    filter: list[dict[str, Any]] = field(default_factory=list)
    order_by: list[dict[str, str]] = field(default_factory=list)  # [{column, direction}]
    limit: Optional[int] = None
    offset: int = 0
    # BI-H3：TOP-K「其他」合并——LIMIT 截断时补一行/一组合计尾部「其他（N 项）」。
    top_k_other: bool = False
    # 客户端语义层下传的可加性掩码（与 measures 平行）：服务端只有列名+聚合方式，
    # 拿不到 measureTypes——占比/比率型列由客户端判定后否决进「其他」行
    # （true=可加，False=否决，None/缺省=沿用聚合方式判断，向后兼容老客户端）。
    add_mask: Optional[list] = None
    # Agent/query safety budgets.  These are hard fail-closed limits, not
    # semantic truncation: a request exceeding them returns an actionable
    # error rather than silently changing Flint's grouped result.
    max_scan_rows: Optional[int] = None
    max_groups: Optional[int] = None


@dataclass
class AggregateResponse:
    """Response body for /aggregate."""
    columns: list[str]
    rows: list[list[Any]]
    aggregates: dict[str, dict[str, float]] = field(default_factory=dict)
    truncated: bool = False
    truncated_reason: Optional[str] = None
    group_count: int = 0
    # Pre-LIMIT group total (only populated when the result was truncated) —
    # the runtime renders「已显示前 N 项（共 M 项）」from it. 0 = unknown.
    group_total: int = 0
    # BI-H3：本次响应合并了「其他（N 项）」尾部行时的标记（{"omitted": N}）。
    other_merged: Optional[dict[str, Any]] = None
    # Bounded query-budget diagnostics; never includes raw rows or SQL.
    query_budget: Optional[dict[str, Any]] = None
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "columns": self.columns,
            "rows": self.rows,
            "aggregates": self.aggregates,
            "truncated": self.truncated,
            "groupCount": self.group_count,
            "groupTotal": self.group_total,
            "elapsedMs": self.elapsed_ms,
        }
        if self.truncated_reason:
            out["truncatedReason"] = self.truncated_reason
        if self.other_merged:
            out["otherMerged"] = self.other_merged
        if self.query_budget:
            out["queryBudget"] = self.query_budget
        return out
