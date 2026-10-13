#!/usr/bin/env python3
"""Validate the narrow report_request.json protocol without third parties."""
from __future__ import annotations

# Running from a deployed skill folder must not litter it with __pycache__/:
# the env var is inherited by spawned children (lint/package/spec_to_report),
# the flag covers imports made after this line in this process.
import os as _os, sys as _sys
_os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
_sys.dont_write_bytecode = True
import json
import re
from urllib.parse import urlparse
from pathlib import Path
from typing import Any

ALLOWED_ROLES = {"time", "dimension", "measure", "id", "ignore"}
ALLOWED_PRESETS = {
    "kpi.metric", "table.aggregate", "table.detail",
    "bar.rank", "bar.stacked", "line.trend", "area.trend", "scatter.correlation",
    "pie.share", "donut.share", "funnel.stage",
}
ALLOWED_FACTS = {"total", "count", "distinctCount", "topN", "shareOfTotal", "trend",
                "growthRate", "rankChange", "missingRate", "min", "max", "average"}
ALLOWED_FILTER_OPS = {"=", "!=", ">", ">=", "<", "<=", "eq", "ne", "gt", "gte", "lt", "lte",
                      "contains", "in", "not in", "is null", "is not null",
                      # 区间谓词（读者侧区间/日期范围筛选控件）：
                      #   between   → value=[low, high]（数值，任一可为 null = 开口）
                      #   daterange → value=['YYYY-MM-DD', 'YYYY-MM-DD']（按规范化 ISO 比较）
                      "between", "daterange"}
# ``price``（2026-10-13 批）：单价/售价 = Flint Price intensive（不可加、缺省
# average）；显式 ``currency`` 声明仍归 Amount（additive）——零漂移。
ALLOWED_MEASURE_TYPES = {"currency", "price", "percent", "count", "duration", "number", "unitless"}
ALLOWED_TEXT_KINDS = {"heading", "paragraph", "callout", "quote", "divider"}
ALLOWED_SURFACES = {"default", "soft", "emphasized"}
ALLOWED_ACCENTS = {"blue", "teal", "orange", "purple"}
ALLOWED_RADII = {"none", "small", "medium"}
ALLOWED_BORDERS = {"none", "hairline", "soft"}
# 统一文本卡（2026-09-15）：textStyle 只有两个「类型」新值（text/annotation）；
# 旧五值保留兼容（读时由 spec_to_report._resolve_card_style 归一），cardColor 是
# 正交色板（校验权威在此，spec_to_report 复用同一条常量不许第二份）。
# default=白底（缺省）；none=透明；white 仅为旧值兼容（与 default 同观感）。
ALLOWED_TEXT_STYLES = {"text", "annotation", "transparent", "white", "callout", "callout-transparent", "banner"}
CARD_COLORS = ("default", "none", "white", "soft", "accent", "green", "amber", "red", "dark")
SAFE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{1,79}$")
# 报告页面宽度档位（docs/page-width-and-table-autofit-design.md）。校验权威在此，
# spec_to_report/build_manifest 经 resolve_page_width 复用——不许第二份常量表。
# 下限 1280：1024 是文本容量估算的校准基线，更窄的档位会裁行，不提供。
PAGE_WIDTHS = (1280, 1440, 1600, 1920)

# 数据源独立性硬控词汇（docs/data-source-independence-and-merge-visibility-design.md D2）：
# "independent" = 各数据源完全独立、禁止 relations 合并；与 relations 互斥（矛盾守卫
# 见 validate_request）。校验权威在此；normalize_request 透传、build_manifest 烘焙、
# decision_ledger 记账（「数据源独立」行）。
DATA_SOURCE_MODES = ("default", "independent")

# ---- chart.table 请求层白名单（WP-3 请求层契约批，docs/product-iteration-plan.md §2.2）----
# 军规 3：新 editor.table.* 键必须三处同步——本白名单 + 双 JSON schema（report-request /
# report-manifest）+ 编译透传（spec_to_report 原样落 editor.table）；漏一边 = 构建期报错或
# 静默丢配置。sessionView 刻意不在名单：它是读者会话视图态（运行时 __sessionView），
# 不进请求/manifest。align 的「键=真实列名」存在性校验放编译侧（那里才有 canonical 表列）。
TABLE_SETTING_KEYS = {
    # 批次一（布局/中国式表格开关）
    "layout", "rowTotal", "mergeSame", "showBars", "totalPos", "totalLabel", "subtotalLabel",
    "limit", "showTotal", "showSubtotal", "showRank", "striped", "sortLevels", "dateScope",
    "dimScopes", "rowDrill", "measureNameRow", "groupTopN",
    # WP-3 新增（§2.2 契约表；运行时消费方 WP-1/2/4/5/7/8/9）
    "density", "align", "headStyle", "wrapText", "rankLabel", "rankHideOnTotal",
    "pageBy", "pagerMode", "pageSizeMax", "export", "condFormat", "subMeasures",
    "heightMode", "treeView", "mergeRowDims", "sparkline",
    # WP-11 总计计算方式（docs/product-iteration-plan.md §3 WP-11，对标 QBI）：
    # {指标列名: 合计聚合方式}——合计回**全量过滤原始行**按指定聚合重算，不是对
    # 页面值二次聚合。形状/枚举校验在此；「键=真实列名」存在性在编译侧核对
    # （_check_table_total_calc_columns，align 同款先例）；派生指标/非数值列的
    # 静默跳过闸门也在编译侧（那里才知道列的语义类型）。
    "totalCalc",
    # WP-14 过滤器字段区（docs/quickbi-filter-zone-research.md §6 拍板 3）：列筛选
    # 预声明 {列名: 条件 spec}，与运行时 tblFilters（editor.table.columnFilters）
    # 同一模型同一形状——不新造第二套过滤模型。not=true = 排除（NOT-IN，服务端
    # op 白名单既有）。「键=真实列名」存在性在编译侧核对（_check_table_column_filters，
    # align/totalCalc 同款先例）。
    "columnFilters",
}
TABLE_DENSITIES = ("compact", "standard", "relaxed")
# totalCalc 的合法合计聚合（WP-11）。与 server aggregator.SUPPORTED_AGG 对齐，
# 但刻意不含 "none"——合计必须是一个真聚合。
TABLE_TOTAL_CALCS = ("sum", "average", "count", "countd", "min", "max")
TABLE_ALIGN_H = ("left", "center", "right", "auto")
TABLE_ALIGN_V = ("top", "middle", "bottom", "auto")
TABLE_HEAD_ALIGNS = ("left", "center", "right")
TABLE_PAGE_BY = ("group", "row")
TABLE_PAGER_MODES = ("full", "simple")
TABLE_PAGE_SIZES = (10, 20, 30, 50, 80, 100, 200, 500)
TABLE_HEIGHT_MODES = ("grow", "fixed")
# 条件格式规则（WP-4 阶段二模型）：basis 固定枚举；actions 只锁键集与类型，
# 求值语义归 WP-4，这里不预设比较符白名单（op 仅要求非空字符串）。
COND_FORMAT_BASES = ("固定值", "百分比区间", "动态字段", "与0", "与均值")
COND_FORMAT_ACTIONS = ("text", "bg", "bold", "italic", "icon", "scale", "bar", "hideText", "fullRow")
SUB_MEASURE_GRAINS = ("year", "quarter", "month", "week", "day")
SUB_MEASURE_SHOWS = ("diff", "pct", "value")


def resolve_page_width(value: Any) -> int:
    """Normalize a request/manifest ``pageWidth`` to a px int (缺省 1280).

    ``"full"`` 是「撑满」的别名 → 1920（fluid 封顶：max-width 语义下，
    视口更窄时本来就跟随视口）。非法值报可行动错误，绝不静默回退——
    静默回退会让「设了没生效」变成查不到原因的悬案。
    """
    if value is None or value == "":
        return 1280
    if isinstance(value, str):
        token = value.strip().lower()
        if token == "full":
            return 1920
        if token.isdigit():
            value = int(token)
        else:
            raise RequestValidationError(
                f"pageWidth must be one of {list(PAGE_WIDTHS)} or \"full\"; got {value!r}")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
        raise RequestValidationError(
            f"pageWidth must be one of {list(PAGE_WIDTHS)} or \"full\"; got {value!r}")
    width = int(value)
    if width not in PAGE_WIDTHS:
        raise RequestValidationError(
            f"pageWidth must be one of {list(PAGE_WIDTHS)} or \"full\"; got {value!r}")
    return width


class RequestValidationError(ValueError):
    pass


def _normalize_sort_object(sort_obj: Any, chart: dict) -> str:
    """Accept the intuitive ``{column, direction}`` spelling for chart.sort.

    The compiler sorts the value axis by measures[0], so the object form is
    honoured when column is omitted or equals the first measure; anything else
    is rejected with a pointer at the enum form. Real authoring runs wrote the
    object form first every time — mapping it removes a guaranteed round-trip.
    """
    direction = sort_obj.get("direction") if isinstance(sort_obj, dict) else None
    if direction is not None and not isinstance(direction, str):
        raise RequestValidationError(f"chart.sort.direction must be a string; got {direction!r}")
    token = str(direction or (sort_obj.get("dir") if isinstance(sort_obj, dict) else None) or "desc").strip().lower()
    token = {"descending": "desc", "ascending": "asc", "降序": "desc", "升序": "asc"}.get(token, token)
    if token not in {"desc", "asc"}:
        raise RequestValidationError(
            f"chart.sort object form needs direction desc/asc; got {sort_obj!r}")
    column = sort_obj.get("column") if isinstance(sort_obj, dict) else None
    if column is not None:
        encoding = chart.get("encoding") if isinstance(chart.get("encoding"), dict) else {}
        measures = chart.get("measures") or encoding.get("measures") or []
        if measures and column != measures[0]:
            raise RequestValidationError(
                f"chart.sort object form only supports column == the first measure ({measures[0]!r}); got {column!r} — "
                "the value axis always sorts by measures[0]; use the enum form 'desc'/'asc'/'time'/'none'")
    return token


def _normalize_measure_types(measure_types: Any, label: str = "measureTypes") -> dict:
    """Validate + canonicalise a measureTypes map (top-level or per-dataset).

    T0/T1 tolerance: a model may write a coarse family label (money/ratio/count
    or Chinese 金额/占比/人数) or a canonical {type,...} object. Normalise to a
    canonical T2 type and write it back so downstream only ever sees canonical
    types. Never raise on a coarse label — normalise instead (Flint's
    "degrade gracefully" rule). Only reject genuinely unknown types."""
    if not isinstance(measure_types, dict):
        raise RequestValidationError(f"{label} must be an object")
    from measure_semantics import normalize_measure_type
    from semantics.type_registry import CURRENCY_SYMBOLS, apply_currency_defaults
    for column, semantic in list(measure_types.items()):
        if not isinstance(column, str):
            raise RequestValidationError(f"{label} keys must be column names")
        if isinstance(semantic, dict):
            allowed = set(semantic) - {"type", "divisor", "unit", "decimals", "aggregation", "format", "currency", "symbol", "semantic"}
            if allowed:
                raise RequestValidationError(f"unsupported measure type fields for {column!r}: {sorted(allowed)}")
            # 批次 2（2026-10-13）：semantic = Flint T2 语义注解键
            #（design-semantics.md §2.7 LLM 注解策略）。声明 semantic 的列，
            # 编译/缺省聚合/零基线按 Flint 注册表该类型执行（如 semantic:'Price'
            # → 不可加、缺省 average）；type 继续管格式族，二者正交。
            sem_label = semantic.get("semantic")
            if sem_label is not None:
                from measure_semantics import SEMANTIC_VOCAB
                from semantics import flint_core as _fc
                if not isinstance(sem_label, str) or not sem_label.strip():
                    raise RequestValidationError(f"measure semantic must be a non-empty label for {column!r}")
                if _fc.canonical_alias(sem_label.strip()) not in SEMANTIC_VOCAB:
                    raise RequestValidationError(
                        f"unknown measure semantic {sem_label!r} for {column!r} — use a Flint type label "
                        f"(Price/Revenue/Cost/Amount/Profit/Percentage/Count/Score/Rating/...)")
            if semantic.get("format") is not None and semantic["format"] not in {"percent"}:
                raise RequestValidationError(f"measure format must be 'percent' for {column!r}")
            if semantic.get("aggregation") is not None and semantic["aggregation"] not in {"sum", "average", "count", "countd", "min", "max"}:
                raise RequestValidationError(f"measure aggregation must be sum/average/count/countd/min/max for {column!r}")
            if semantic.get("divisor") is not None and float(semantic["divisor"]) <= 0:
                raise RequestValidationError(f"measure divisor must be positive for {column!r}")
            if semantic.get("decimals") is not None and not (0 <= int(semantic["decimals"]) <= 6):
                raise RequestValidationError(f"measure decimals must be 0-6 for {column!r}")
            # 拍板 6（docs/multidim-table-and-currency-design-2026-09-28.md §二）：
            # currency 必须在内置币表内——未知代码给可行动错误（提示改用 symbol
            # 自定义），绝不静默回退（比 Flint 的静默不覆盖更可行动）。
            cur = semantic.get("currency")
            if cur is not None:
                if not isinstance(cur, str) or not cur.strip():
                    raise RequestValidationError(f"measure currency must be a non-empty ISO code for {column!r}")
                if cur.strip().upper() not in CURRENCY_SYMBOLS:
                    raise RequestValidationError(
                        f"unknown measure currency {cur!r} for {column!r} — use one of the built-in codes "
                        f"({', '.join(sorted(CURRENCY_SYMBOLS))}), or declare a custom display via symbol instead")
            sym = semantic.get("symbol")
            if sym is not None:
                if not isinstance(sym, str) or not sym.strip():
                    raise RequestValidationError(f"measure symbol must be a non-empty string for {column!r}")
                if len(sym.strip()) > 4:
                    raise RequestValidationError(
                        f"measure symbol must be at most 4 characters for {column!r} (got {len(sym.strip())!s})")
            normalized = normalize_measure_type(semantic)
            if normalized is None:
                raise RequestValidationError(
                    f"invalid measure type for {column!r}: {semantic!r} "
                    f"(allowed: {', '.join(sorted(ALLOWED_MEASURE_TYPES))})")
            if isinstance(normalized, dict):
                # Preserve explicit overrides then canonicalize the type key.
                merged = dict(semantic)
                merged["type"] = normalized["type"]
                # Canonical currency shape: uppercase the ISO code, trim symbol
                # (empties never reach here — the guards above already raised).
                if isinstance(merged.get("currency"), str):
                    merged["currency"] = merged["currency"].strip().upper()
                if isinstance(merged.get("symbol"), str):
                    merged["symbol"] = merged["symbol"].strip()
                # 拍板 7: explicit currency declaration without divisor/unit gets
                # the per-code default scale (CNY/JPY → 万元口径, others → 原值).
                merged = apply_currency_defaults(merged)
                measure_types[column] = merged
            else:
                measure_types[column] = normalized
        else:
            normalized = normalize_measure_type(semantic)
            if normalized is None:
                raise RequestValidationError(
                    f"invalid measure type for {column!r}: {semantic!r} "
                    f"(allowed: {', '.join(sorted(ALLOWED_MEASURE_TYPES))})")
            measure_types[column] = normalized
    return measure_types


def _derived_universe(ds: dict, request: dict, derived_columns: list[str]) -> tuple[set[str], set[str]]:
    """Declared column universe for derived-measure validation.

    Validation runs BEFORE any file is read, so the only column knowledge is
    what the request declares: ``columnRoles`` (per-dataset, falling back to the
    top-level map for the primary table), ``measureTypes`` keys, and the row-level
    derived column names. ``spec_to_report.compile_spec`` re-validates against
    the REAL table columns, so a column that is simply undeclared here is not
    rejected — only a reference to something nobody ever mentioned is.
    """
    roles: dict = {}
    roles.update(request.get("columnRoles") or {})
    roles.update(ds.get("columnRoles") or {})
    measure_types = dict(request.get("measureTypes") or {})
    measure_types.update(ds.get("measureTypes") or {})
    universe = set(roles) | set(measure_types) | set(derived_columns or [])
    times = {c for c, r in roles.items() if r == "time"}
    return universe, times


def _validate_datasets(request: dict) -> tuple[list[str], str]:
    """Validate the optional multi-dataset declaration (P1, unrelated tables).

    Returns (ordered dataset ids, default dataset id). Requests without a
    ``datasets`` key stay single-table: the implicit id is ``primary``.
    Per-dataset columnRoles/measureTypes/hierarchy are the per-table semantic
    slots; cross-table column existence is enforced at compile time (files are
    not read here)."""
    declared = request.get("datasets")
    if declared is None:
        return ["primary"], "primary"
    if not isinstance(declared, list) or not declared or len(declared) > 8:
        raise RequestValidationError("datasets must contain 1-8 items")
    ds_ids: list[str] = []
    seen: set = set()
    for _i, ds in enumerate(declared):
        if not isinstance(ds, dict):
            raise RequestValidationError("each dataset must be an object")
        unknown_ds = sorted(set(ds) - {"id", "label", "file", "columnRoles", "measureTypes", "hierarchy", "derived", "role", "isDefault", "dimGroups", "primaryKey"})
        if unknown_ds:
            raise RequestValidationError(f"unsupported dataset fields: {unknown_ds}")
        # 增量合并主键（datasets[].primaryKey，2026-09-15）：形状在这里校验；
        # 列是否真的存在于表里由 compile_spec 对真实表再校验一次（请求层不读文件）。
        did = ds.get("id")
        if ds.get("primaryKey") is not None:
            pk = ds["primaryKey"]
            if not isinstance(pk, list) or not pk or len(pk) > 8 \
                    or not all(isinstance(c, str) and c.strip() for c in pk) \
                    or len(set(pk)) != len(pk):
                raise RequestValidationError(
                    f"dataset {did} primaryKey must be a list of 1-8 distinct non-empty column names")
        if not isinstance(did, str) or not SAFE_ID_RE.fullmatch(did) or did in seen:
            # Deterministic fallback: 'primary' for a single table, ds-1..n for many.
            candidate = "primary" if len(declared) == 1 else f"ds-{_i + 1}"
            suffix = 1
            while candidate in seen:
                candidate = f"{candidate}-{suffix}"
                suffix += 1
            ds["id"] = candidate
            did = candidate
        seen.add(did)
        ds_ids.append(did)
        if "label" in ds and (not isinstance(ds["label"], str) or len(ds["label"]) > 60):
            raise RequestValidationError(f"dataset {did} label must be a string of at most 60 chars")
        if "file" in ds:
            # file 收数组 = 构建期同构并集（2026-09-22）：N 个同构文件并成一张
            # 表（W35+W36 补新一期），语义真源 table_input.load_table_files。
            _f = ds["file"]
            _files = _f if isinstance(_f, list) else [_f]
            if not _files or len(_files) > 8 or not all(
                    isinstance(x, str) and x.strip() and len(x) <= 512 for x in _files):
                raise RequestValidationError(
                    f"dataset {did} file must be a path string, or an array of 1-8 path "
                    "strings (same-schema union: all files must share identical fields)")
        if ds.get("columnRoles") is not None:
            if not isinstance(ds["columnRoles"], dict):
                raise RequestValidationError(f"dataset {did} columnRoles must be an object")
            invalid = {col: role for col, role in ds["columnRoles"].items()
                       if (not isinstance(col, str)) or role not in ALLOWED_ROLES}
            if invalid:
                raise RequestValidationError(f"dataset {did} invalid column roles: {invalid}")
        if ds.get("measureTypes") is not None:
            ds["measureTypes"] = _normalize_measure_types(ds["measureTypes"], f"datasets[{did}].measureTypes")
        if ds.get("hierarchy") is not None and not isinstance(ds["hierarchy"], list):
            raise RequestValidationError(f"dataset {did} hierarchy must be an array")
        # 维度值分组（对标 Quick BI 分组维度，2026-09-14）：物化成真列的虚拟维度；
        # 不进计算字段引用宇宙（列宇宙校验只认原始列/计算列/派生指标）。
        if ds.get("dimGroups") is not None:
            dgs = ds["dimGroups"]
            if not isinstance(dgs, list):
                raise RequestValidationError(f"dataset {did} dimGroups must be an array")
            _dg_names = set()
            for _dg in dgs:
                if not isinstance(_dg, dict) or not str(_dg.get("name") or "").strip() or not _dg.get("source"):
                    raise RequestValidationError(f"dataset {did} dimGroups items need name + source")
                if _dg["name"] in _dg_names:
                    raise RequestValidationError(f"dataset {did} dimGroups duplicate name: {_dg['name']!r}")
                _dg_names.add(_dg["name"])
                _gs = _dg.get("groups")
                if not isinstance(_gs, list) or not _gs or not all(
                    isinstance(g, dict) and str(g.get("label") or "").strip() and isinstance(g.get("members"), list)
                    for g in _gs
                ):
                    raise RequestValidationError(f"dataset {did} dimGroups {_dg['name']!r} needs groups with label + members")
        # Table role: 'primary' (a fact table that may carry charts/KPIs, the
        # default) or 'lookup' (enrichment-only: participates in ``referential``
        # relations and never drives chart attribution / mode judgement).
        role = ds.get("role")
        if role is not None and role not in {"primary", "lookup"}:
            raise RequestValidationError(f"dataset {did} role must be 'primary' or 'lookup'")
        if "isDefault" in ds and not isinstance(ds["isDefault"], bool):
            raise RequestValidationError(f"dataset {did} isDefault must be a boolean")
        # P1: dataset.derived.columns is the new home for whitelist-op derived
        # columns (replacing the legacy top-level derivedColumns). We validate
        # the *final* shape here so compile_spec can trust it as canonical.
        # ``derived.measures`` is the AGGREGATE-level slot (派生指标: formula /
        # window) — shape + reference + DAG validated here against the DECLARED
        # column universe (columnRoles/measureTypes/derived columns; files are
        # not read in this function), then re-validated against the real table in
        # ``spec_to_report.compile_spec``.
        if ds.get("derived") is not None:
            derived_obj = ds["derived"]
            if not isinstance(derived_obj, dict):
                raise RequestValidationError(f"dataset {did} derived must be an object")
            unknown_derived_keys = sorted(set(derived_obj) - {"columns", "measures"})
            if unknown_derived_keys:
                raise RequestValidationError(
                    f"dataset {did} derived unsupported fields: {unknown_derived_keys}; supported: "
                    "['columns'（行级计算列 op/left/right）, 'measures'（聚合级派生指标 formula/window）]")
            cols = derived_obj.get("columns")
            if cols is not None:
                if not isinstance(cols, list):
                    raise RequestValidationError(f"dataset {did} derived.columns must be an array")
                # 0-8: an EMPTY slot is legal — the compiler's canonical slot is
                # ``{columns: [], measures: [...]}`` and normalize_request must stay
                # idempotent (re-validating the slot it just normalised must pass).
                if len(cols) > 8:
                    raise RequestValidationError(f"dataset {did} derived.columns must contain at most 8 items")
                seen_names: set = set()
                # 计算列之间的引用只允许「写在前面的」：这是可以**在请求层**判定
                # 的真错误（两个名字都在请求里），不必等编译期才知道。未知列仍留给
                # 编译期（请求可能还没声明全部字段）。
                _declared = {str(c.get("name")) for c in cols if isinstance(c, dict)}
                _prior: set = set()
                for _di, item in enumerate(cols):
                    if not isinstance(item, dict):
                        raise RequestValidationError(f"dataset {did} derived.column {_di + 1} must be an object")
                    # unit（datediff 的单位，缺省 day）是 derived_model/_COLUMN_FIELDS
                    # 与 derived_eval.materialize 都支持的形状字段——拒绝它会让
                    # 文档化的单位功能在请求层不可达（下面的 unit 校验永远跑不到）。
                    unknown_derived = sorted(set(item) - {"name", "op", "left", "right", "unit"})
                    if unknown_derived:
                        raise RequestValidationError(
                            f"dataset {did} derived.column unsupported fields: {unknown_derived}"
                            "（字段的默认聚合/格式写在 datasets[].measureTypes）")
                    name = item.get("name")
                    if not isinstance(name, str) or not name.strip() or len(name) > 40:
                        raise RequestValidationError(f"dataset {did} derived.column {_di + 1} name must be a non-empty string <= 40 chars")
                    if name in seen_names:
                        raise RequestValidationError(f"dataset {did} derived.column duplicate name: {name!r}")
                    seen_names.add(name)
                    if item.get("op") not in {"add", "sub", "mul", "div", "datediff"}:
                        raise RequestValidationError(f"dataset {did} derived.column {name!r} op must be add/sub/mul/div/datediff")
                    if item.get("op") == "datediff":
                        # 时间差（对标 Quick BI「获取时间差」，2026-09-14）：任一端可为
                        # __today__（编译期快照）；unit 完整单位白名单（缺省 day）。
                        if item.get("unit") is not None and item.get("unit") not in {"year", "quarter", "month", "week", "day"}:
                            raise RequestValidationError(f"dataset {did} derived.column {name!r} unit must be year/quarter/month/week/day")
                        if item.get("left") == "__today__" and item.get("right") == "__today__":
                            raise RequestValidationError(f"dataset {did} derived.column {name!r} 两端不能都是 __today__")
                    # 操作数 = 列名（字符串，可为原始列或**写在前面**的计算列）或常量（数字）
                    # 或 __today__（仅 datediff）。
                    for side in ("left", "right"):
                        operand = item.get(side)
                        if isinstance(operand, bool):
                            raise RequestValidationError(
                                f"dataset {did} derived.column {name!r} {side} must be a column name or a number")
                        if item.get("op") == "datediff" and operand == "__today__":
                            continue
                        if isinstance(operand, (int, float)):
                            continue
                        if not isinstance(operand, str) or not operand.strip():
                            raise RequestValidationError(
                                f"dataset {did} derived.column {name!r} {side} must be a column name or a number")
                        if operand in _declared and operand not in _prior:
                            raise RequestValidationError(
                                f"dataset {did} derived.column {name!r} {side} 引用了写在其后面的"
                                f"计算列 {operand!r}（只允许引用写在前面的计算列）")
                    _prior.add(str(name))
            if derived_obj.get("measures") is not None:
                from derived_model import DerivedError, normalize_measures, validate_measures
                try:
                    nodes = normalize_measures(derived_obj["measures"])
                    universe, times = _derived_universe(ds, request, [
                        c.get("name") for c in (cols or []) if isinstance(c, dict)])
                    validate_measures(nodes, universe, times,
                                      where=f"dataset {did} derived.measures",
                                      check_columns=bool(universe))
                except DerivedError as exc:
                    raise RequestValidationError(f"dataset {did}: {exc}") from None
                derived_obj["measures"] = nodes
    # Default attribution (the dataset new charts/KPIs/filters fall back to):
    # the explicit ``isDefault`` primary, else the first primary, else the first
    # declared table. Multiple primaries are allowed (co-facts); lookup tables
    # may not be the default.
    default_flags = [d["id"] for d in declared if d.get("isDefault")]
    if len(default_flags) > 1:
        raise RequestValidationError("at most one dataset may set isDefault=true")
    if default_flags:
        flagged = next(d for d in declared if d["id"] == default_flags[0])
        if (flagged.get("role") or "primary") != "primary":
            raise RequestValidationError("the isDefault dataset must be a 'primary' table")
    default_id = default_flags[0] if default_flags else next(
        (d["id"] for d in declared if (d.get("role") or "primary") == "primary"), ds_ids[0])
    return ds_ids, default_id


def _validate_relations(request: dict, ds_ids: list[str]) -> None:
    """Validate the optional cross-table relations (v4 single-mode merge).

    Each ``relations[i]`` is ONE merge: N tables joined into ONE wide table.
    The merge is a CHAIN — each subsequent table must connect back to one of
    the tables already in the chain via at least one ``keys[]`` entry. The
    first table in ``tables[]`` is the chain root (left side of every join).
    Per-key ``mode`` decides which side's key column survives the join
    (``a``/``b``/``both``); the optional ``fields`` map filters which columns
    of each table actually end up in the wide table.
    """
    relations = request.get("relations")
    if relations is None:
        return
    for d in (request.get("datasets") or []):
        # 并集数据集（file 数组）× relations 首期不组合：并集本身已产出一张单表，
        # 再 join 一层是双重复杂物（真机场景 W35+W36 并集不需要 relations）。
        # 重开条件：并集结果作为 relation 参与方的真实需求出现。
        if isinstance(d, dict) and isinstance(d.get("file"), list):
            raise RequestValidationError(
                f"dataset {d.get('id')} 的 file 是同构并集数组——暂不参与 relations 合并；"
                "请把其余表声明为独立数据集，或先在源侧外置成单文件。")
    if not isinstance(relations, list) or len(relations) > 16:
        raise RequestValidationError("relations must be an array of at most 16 items")
    if len(relations) > 1:
        # merge_tables.materialize 一次只物化一条关系（>1 直接 ValueError），
        # 且发生在 profile 全部做完之后的深水区——在请求层就给可行动报错。
        raise RequestValidationError(
            f"relations 目前只支持 1 条（got {len(relations)}）：合并引擎一次只物化一条关系；"
            "请拆成多次构建，或把其余数据表声明为普通 datasets[]")
    seen: set = set()
    for _i, rel in enumerate(relations):
        if not isinstance(rel, dict):
            raise RequestValidationError("each relation must be an object")
        unknown = sorted(set(rel) - {"id", "name", "tables", "keys", "fields", "renames"})
        if unknown:
            raise RequestValidationError(f"unsupported relation fields: {unknown}")
        rid = rel.get("id")
        if not isinstance(rid, str) or not SAFE_ID_RE.fullmatch(rid) or rid in seen:
            raise RequestValidationError(f"relation id invalid or duplicate: {rid!r}")
        seen.add(rid)
        if "name" in rel and (not isinstance(rel["name"], str) or not rel["name"].strip()):
            raise RequestValidationError(f"relation {rid!r} name must be a non-empty string")
        tbl = rel.get("tables")
        if not isinstance(tbl, list) or len(tbl) < 2:
            raise RequestValidationError(f"relation {rid!r}: tables must be an array of ≥2 datasetIds")
        if len(set(tbl)) != len(tbl):
            raise RequestValidationError(f"relation {rid!r}: tables contains duplicates")
        for ds in tbl:
            if ds not in ds_ids:
                raise RequestValidationError(f"relation {rid!r}: unknown datasetId {ds!r}")
        keys = rel.get("keys")
        if not isinstance(keys, list) or not keys:
            raise RequestValidationError(f"relation {rid!r}: keys must be a non-empty array")
        for ki, k in enumerate(keys):
            if not isinstance(k, dict):
                raise RequestValidationError(f"relation {rid!r} keys[{ki}] must be an object")
            unk = sorted(set(k) - {"a", "b", "mode", "name"})
            if unk:
                raise RequestValidationError(f"relation {rid!r} keys[{ki}] unsupported fields: {unk}")
            mode = k.get("mode", "both")
            if mode not in {"a", "b", "both"}:
                raise RequestValidationError(f"relation {rid!r} keys[{ki}] mode must be one of a/b/both")
            if "name" in k and (not isinstance(k["name"], str) or not k["name"].strip()):
                raise RequestValidationError(f"relation {rid!r} keys[{ki}] name must be a non-empty string")
            for side in ("a", "b"):
                ep = k.get(side)
                if not isinstance(ep, dict):
                    raise RequestValidationError(f"relation {rid!r} keys[{ki}].{side} must be an object")
                unk2 = sorted(set(ep) - {"ds", "col"})
                if unk2:
                    raise RequestValidationError(f"relation {rid!r} keys[{ki}].{side} unsupported fields: {unk2}")
                if ep.get("ds") not in tbl:
                    raise RequestValidationError(f"relation {rid!r} keys[{ki}].{side}.ds must be in tables[]")
                if not isinstance(ep.get("col"), str) or not ep["col"].strip():
                    raise RequestValidationError(f"relation {rid!r} keys[{ki}].{side}.col must be a non-empty string")
            a, b = k["a"], k["b"]
            if a["ds"] == b["ds"] and a["col"] == b["col"]:
                raise RequestValidationError(f"relation {rid!r} keys[{ki}] is a self-loop")
        # Chain connectivity: every table after tables[0] must connect back to
        # {tables[0], ..., tables[i-1]} via at least one key. (Unconnected
        # tables silently drop; a Cartesian would corrupt counts — reject.)
        connected = {tbl[0]}
        for idx in range(1, len(tbl)):
            ds = tbl[idx]
            link = any(
                (k["a"]["ds"] == ds and k["b"]["ds"] in connected)
                or (k["b"]["ds"] == ds and k["a"]["ds"] in connected)
                for k in keys
            )
            if not link:
                raise RequestValidationError(
                    f"relation {rid!r}: table {ds!r} is not connected to "
                    f"{sorted(connected)} — add a key linking it to the chain"
                )
            connected.add(ds)
        fields = rel.get("fields")
        if fields is not None:
            if not isinstance(fields, dict):
                raise RequestValidationError(f"relation {rid!r}: fields must be an object")
            for ds, cols in fields.items():
                if ds not in tbl:
                    raise RequestValidationError(f"relation {rid!r}: fields[{ds!r}] ds not in tables[]")
                if not isinstance(cols, list) or not cols or not all(isinstance(c, str) and c.strip() for c in cols):
                    raise RequestValidationError(f"relation {rid!r}: fields[{ds!r}] must be a non-empty list of column names")
        # ``renames`` = 合并后列名的用户别名 {合并前列名: 新名}。作用在链式 join 之后，
        # 所以重新合并（表集合/键模式变了）仍能保住用户改的名字。
        rn = rel.get("renames")
        if rn is not None:
            if not isinstance(rn, dict):
                raise RequestValidationError(f"relation {rid!r}: renames must be an object")
            for src, dst in rn.items():
                if not isinstance(src, str) or not src.strip():
                    raise RequestValidationError(f"relation {rid!r}: renames key must be a non-empty column name")
                if not isinstance(dst, str) or not dst.strip():
                    raise RequestValidationError(f"relation {rid!r}: renames[{src!r}] must map to a non-empty name")


def _validate_card_style(style: Any, where: str) -> None:
    """Whitelisted card-style preset (surface/accent/radius/border, plus the
    unified text-card ``textStyle`` kind + ``cardColor`` palette for text
    bands) shared by text bands, charts and KPI cards
    (docs/rich-text-cards-design.md). textStyle 是「类型」，cardColor 是正交色板。"""
    if style is None:
        return
    if not isinstance(style, dict):
        raise RequestValidationError(f"{where} style must be an object")
    unknown_style = sorted(set(style) - {"surface", "accent", "radius", "border", "textStyle", "cardColor"})
    if unknown_style:
        raise RequestValidationError(f"{where} unsupported style keys: {unknown_style}")
    if style.get("textStyle") not in (None, *ALLOWED_TEXT_STYLES):
        raise RequestValidationError(f"{where} style.textStyle must be one of {sorted(ALLOWED_TEXT_STYLES)}")
    if style.get("cardColor") not in (None, *CARD_COLORS):
        raise RequestValidationError(f"{where} style.cardColor must be one of {sorted(CARD_COLORS)}")
    if style.get("surface") not in (None, *ALLOWED_SURFACES):
        raise RequestValidationError(f"{where} style.surface must be one of {sorted(ALLOWED_SURFACES)}")
    if style.get("accent") not in (None, *ALLOWED_ACCENTS):
        raise RequestValidationError(f"{where} style.accent must be one of {sorted(ALLOWED_ACCENTS)}")
    if style.get("radius") not in (None, *ALLOWED_RADII):
        raise RequestValidationError(f"{where} style.radius must be one of {sorted(ALLOWED_RADII)}")
    if style.get("border") not in (None, *ALLOWED_BORDERS):
        raise RequestValidationError(f"{where} style.border must be one of {sorted(ALLOWED_BORDERS)}")


def validate_relation(relation: dict[str, Any], ds_ids: list[str]) -> None:
    """Validate ONE relation against the given dataset ids.

    Public entry used by the SERVER's runtime merge endpoint, so a relation
    authored live in the browser is held to the exact same rules as one in a
    build-time report_request.json (chain connectivity, per-key mode, fields,
    renames). One source of truth: delegates to ``_validate_relations``.
    """
    _validate_relations({"relations": [relation]}, list(ds_ids or []))


def _validate_chart_table(tbl: Any) -> None:
    """表格卡作者设置（批次一 + WP-3 请求层契约批，docs/product-iteration-plan.md §2.2）。

    布局/中国式表格开关在请求层直接声明，编译原样落 chart.editor.table（运行时唯一
    消费口，编辑态改设置即覆盖）。这里只锁形状与枚举；align 的「键=真实列名」存在性
    在编译侧核（spec_to_report 才有 canonical 表列）。非法值一律可行动报错，不静默回退；
    校验必须幂等（compile 管道会对同一 spec 跑两遍 validate_request）。
    """
    if not isinstance(tbl, dict):
        raise RequestValidationError("chart.table must be an object")
    unknown_tbl = sorted(set(tbl) - TABLE_SETTING_KEYS)
    if unknown_tbl:
        # Actionable rejection: point at where a rejected key actually lives.
        hints = []
        if "sessionView" in unknown_tbl:
            hints.append("sessionView 是读者会话视图态（运行时 __sessionView，刷新即还原），"
                         "不进请求/manifest；读者默认视图请用表格设置声明")
        raise RequestValidationError(
            f"unsupported chart.table fields: {unknown_tbl}; supported: {sorted(TABLE_SETTING_KEYS)}"
            + ("".join("；" + h for h in hints)))
    if "layout" in tbl and tbl["layout"] not in ("grouped", "matrix", "nd"):
        raise RequestValidationError("chart.table.layout must be 'grouped', 'matrix' or 'nd'")
    if "totalPos" in tbl and tbl["totalPos"] not in ("top", "bottom"):
        raise RequestValidationError("chart.table.totalPos must be 'top' or 'bottom'")
    for _b in ("rowTotal", "mergeSame", "showBars", "showTotal", "showSubtotal", "showRank", "striped", "rowDrill", "measureNameRow"):
        if _b in tbl and not isinstance(tbl[_b], bool):
            raise RequestValidationError(f"chart.table.{_b} must be a boolean")
    for _s in ("totalLabel", "subtotalLabel"):
        if _s in tbl and (not isinstance(tbl[_s], str) or not tbl[_s].strip() or len(tbl[_s]) > 12):
            raise RequestValidationError(f"chart.table.{_s} must be a non-empty string ≤12 chars")
    if "limit" in tbl and (not isinstance(tbl["limit"], int) or isinstance(tbl["limit"], bool) or not 1 <= tbl["limit"] <= 100):
        raise RequestValidationError("chart.table.limit must be an integer 1-100")
    # 组内 TopN（2026-09-14）：行区第 1 维分区内按首指标取前 N 行；0/缺省 = 关。
    if "groupTopN" in tbl and (not isinstance(tbl["groupTopN"], int) or isinstance(tbl["groupTopN"], bool) or not 0 <= tbl["groupTopN"] <= 100):
        raise RequestValidationError("chart.table.groupTopN must be an integer 0-100 (0 = off)")
    if "sortLevels" in tbl:
        sl = tbl["sortLevels"]
        if not isinstance(sl, list) or not sl or not all(
            isinstance(l, dict) and isinstance(l.get("col"), str) and l.get("col")
            and l.get("dir") in ("asc", "desc") for l in sl
        ):
            raise RequestValidationError("chart.table.sortLevels must be a list of {col, dir:asc|desc}")
    # 相对日期范围（P1.5）：col=时间维度列，mode recent（近 N 期）/ range（起止桶值）；
    # anchor 默认数据最新期（经营月报口径），可选自然今天。
    if "dateScope" in tbl:
        ds = tbl["dateScope"]
        if not isinstance(ds, dict):
            raise RequestValidationError("chart.table.dateScope must be an object")
        unknown_ds = sorted(set(ds) - {"col", "mode", "n", "unit", "anchor", "min", "max"})
        if unknown_ds:
            raise RequestValidationError(f"chart.table.dateScope unknown keys: {unknown_ds}")
        if not isinstance(ds.get("col"), str) or not ds.get("col"):
            raise RequestValidationError("chart.table.dateScope.col must be a column name")
        if ds.get("mode") not in ("recent", "range"):
            raise RequestValidationError("chart.table.dateScope.mode must be 'recent' or 'range'")
        if ds["mode"] == "recent":
            n = ds.get("n")
            if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= 100:
                raise RequestValidationError("chart.table.dateScope.n must be an integer 1-100")
            if ds.get("unit") not in ("week", "month", "quarter", "year"):
                raise RequestValidationError("chart.table.dateScope.unit must be week/month/quarter/year")
        else:
            mn, mx = ds.get("min"), ds.get("max")
            if (not isinstance(mn, str) or not mn) and (not isinstance(mx, str) or not mx):
                raise RequestValidationError("chart.table.dateScope needs min and/or max bucket values")
        if "anchor" in ds and ds["anchor"] not in ("data", "today"):
            raise RequestValidationError("chart.table.dateScope.anchor must be 'data' or 'today'")
    # 维度取值范围（P3，docs/table-crosstable-p3.md §3.5）：每个行/列维度一条。
    # time = 近 N 期/起止期（与 dateScope 同语义）；values = 类目值域多选。
    # 二者都经客户端统一管道过滤（derived 之后、列筛选之前），服务端零改动。
    if "dimScopes" in tbl:
        scopes = tbl["dimScopes"]
        if not isinstance(scopes, list) or not scopes or not all(isinstance(x, dict) for x in scopes):
            raise RequestValidationError("chart.table.dimScopes must be a non-empty list of objects")
        seen_cols = set()
        for sc in scopes:
            if not isinstance(sc.get("col"), str) or not sc.get("col"):
                raise RequestValidationError("chart.table.dimScopes[].col must be a column name")
            if sc["col"] in seen_cols:
                raise RequestValidationError(f"chart.table.dimScopes duplicate col: {sc['col']}")
            seen_cols.add(sc["col"])
            kind = sc.get("kind")
            if kind == "time":
                unknown_sc = sorted(set(sc) - {"col", "kind", "mode", "n", "unit", "anchor", "min", "max"})
                if unknown_sc:
                    raise RequestValidationError(f"chart.table.dimScopes[time] unknown keys: {unknown_sc}")
                if sc.get("mode") not in ("recent", "range"):
                    raise RequestValidationError("chart.table.dimScopes[].mode must be 'recent' or 'range'")
                if sc["mode"] == "recent":
                    n = sc.get("n")
                    if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= 100:
                        raise RequestValidationError("chart.table.dimScopes[].n must be an integer 1-100")
                    if sc.get("unit") not in ("week", "month", "quarter", "year"):
                        raise RequestValidationError("chart.table.dimScopes[].unit must be week/month/quarter/year")
                else:
                    mn, mx = sc.get("min"), sc.get("max")
                    if (not isinstance(mn, str) or not mn) and (not isinstance(mx, str) or not mx):
                        raise RequestValidationError("chart.table.dimScopes[] needs min and/or max bucket values")
                if "anchor" in sc and sc["anchor"] not in ("data", "today"):
                    raise RequestValidationError("chart.table.dimScopes[].anchor must be 'data' or 'today'")
            elif kind == "values":
                unknown_sc = sorted(set(sc) - {"col", "kind", "values"})
                if unknown_sc:
                    raise RequestValidationError(f"chart.table.dimScopes[values] unknown keys: {unknown_sc}")
                vv = sc.get("values")
                if not isinstance(vv, list) or not vv or not all(isinstance(x, (str, int, float)) and not isinstance(x, bool) for x in vv):
                    raise RequestValidationError("chart.table.dimScopes[].values must be a non-empty list of scalars")
            else:
                raise RequestValidationError("chart.table.dimScopes[].kind must be 'time' or 'values'")
    # 列筛选预声明（WP-14 过滤器字段区）：{列名: 条件 spec}，与运行时 tblFilters
    # （editor.table.columnFilters / editor-runtime tableColumnFiltersOf）同一模型：
    # values = 值域（not:true = 排除 NOT-IN；values 含 '__blank__' 哨兵 = 空白行）；
    # range = 数值开口区间（min/max 至少一端）。非法值可行动报错，不静默回退；
    # 校验幂等（compile 管道对同一 spec 跑两遍 validate_request）。
    if "columnFilters" in tbl:
        cf = tbl["columnFilters"]
        if not isinstance(cf, dict) or not cf:
            raise RequestValidationError(
                "chart.table.columnFilters must be a non-empty object {列名: 条件 spec}")
        _CF_KEYS = {"values": {"type", "values", "not"}, "range": {"type", "min", "max"}}

        def _num(x: Any) -> bool:
            return isinstance(x, (int, float)) and not isinstance(x, bool)

        for cf_col, cf_spec in cf.items():
            if not isinstance(cf_col, str) or not cf_col:
                raise RequestValidationError("chart.table.columnFilters keys must be column names")
            if not isinstance(cf_spec, dict):
                raise RequestValidationError(f"chart.table.columnFilters[{cf_col!r}] must be an object")
            cf_type = cf_spec.get("type")
            if cf_type not in _CF_KEYS:
                raise RequestValidationError(
                    f"chart.table.columnFilters[{cf_col!r}].type must be 'values' or 'range'; got {cf_type!r}")
            unknown_cf = sorted(set(cf_spec) - _CF_KEYS[cf_type])
            if unknown_cf:
                raise RequestValidationError(
                    f"chart.table.columnFilters[{cf_col!r}] unknown keys: {unknown_cf}; "
                    f"supported: {sorted(_CF_KEYS[cf_type])}")
            if cf_type == "values":
                cf_vals = cf_spec.get("values")
                if not isinstance(cf_vals, list) or not cf_vals or not all(
                    isinstance(x, (str, int, float)) and not isinstance(x, bool) for x in cf_vals
                ):
                    raise RequestValidationError(
                        f"chart.table.columnFilters[{cf_col!r}].values must be a non-empty list of scalars")
                if "not" in cf_spec and not isinstance(cf_spec["not"], bool):
                    raise RequestValidationError(
                        f"chart.table.columnFilters[{cf_col!r}].not must be a boolean (true = 排除已选 NOT-IN)")
            else:
                if not (_num(cf_spec.get("min")) or _num(cf_spec.get("max"))):
                    raise RequestValidationError(
                        f"chart.table.columnFilters[{cf_col!r}] range needs min and/or max numbers")
    # ---- WP-3 新增键（docs/product-iteration-plan.md §2.2 契约表）----
    # 密度档（WP-1 消费）：compact 32 / standard 40 / relaxed 48 行高。
    if "density" in tbl and tbl["density"] not in TABLE_DENSITIES:
        raise RequestValidationError(
            f"chart.table.density must be one of {list(TABLE_DENSITIES)}; got {tbl['density']!r}")
    # 每列对齐（WP-1 消费）：{列名: {h, v}}。请求层只做结构校验；键=真实列名的
    # 存在性在编译侧 _check_table_align_columns 核（那里有含物化列的 canonical 表列）。
    if "align" in tbl:
        align = tbl["align"]
        if not isinstance(align, dict) or not align:
            raise RequestValidationError("chart.table.align must be a non-empty object {列名: {h?, v?}}")
        for _ac, _av in align.items():
            if not isinstance(_av, dict):
                raise RequestValidationError(f"chart.table.align[{_ac!r}] must be an object {{h?, v?}}; got {_av!r}")
            unknown_a = sorted(set(_av) - {"h", "v"})
            if unknown_a:
                raise RequestValidationError(
                    f"chart.table.align[{_ac!r}] unknown keys: {unknown_a}; supported: ['h', 'v']")
            if "h" in _av and _av["h"] not in TABLE_ALIGN_H:
                raise RequestValidationError(
                    f"chart.table.align[{_ac!r}].h must be one of {list(TABLE_ALIGN_H)}; got {_av['h']!r}")
            if "v" in _av and _av["v"] not in TABLE_ALIGN_V:
                raise RequestValidationError(
                    f"chart.table.align[{_ac!r}].v must be one of {list(TABLE_ALIGN_V)}; got {_av['v']!r}")
    # 表头样式（WP-1 消费）：背景/字色/对齐/隐藏列表头。颜色收非空字符串
    # （建议 #rrggbb；运行时直接作 CSS 值）。
    if "headStyle" in tbl:
        hs = tbl["headStyle"]
        if not isinstance(hs, dict) or not hs:
            raise RequestValidationError("chart.table.headStyle must be a non-empty object")
        unknown_hs = sorted(set(hs) - {"bg", "fg", "align", "hideColHead"})
        if unknown_hs:
            raise RequestValidationError(
                f"chart.table.headStyle unknown keys: {unknown_hs}; supported: ['align', 'bg', 'fg', 'hideColHead']")
        for _c in ("bg", "fg"):
            if _c in hs and (not isinstance(hs[_c], str) or not hs[_c].strip()):
                raise RequestValidationError(f"chart.table.headStyle.{_c} must be a non-empty CSS color string (建议 #rrggbb); got {hs[_c]!r}")
        if "align" in hs and hs["align"] not in TABLE_HEAD_ALIGNS:
            raise RequestValidationError(
                f"chart.table.headStyle.align must be one of {list(TABLE_HEAD_ALIGNS)}; got {hs['align']!r}")
        if "hideColHead" in hs and not isinstance(hs["hideColHead"], bool):
            raise RequestValidationError("chart.table.headStyle.hideColHead must be a boolean")
    for _b in ("wrapText", "rankHideOnTotal", "treeView"):
        if _b in tbl and not isinstance(tbl[_b], bool):
            raise RequestValidationError(f"chart.table.{_b} must be a boolean")
    if "rankLabel" in tbl and (not isinstance(tbl["rankLabel"], str) or not tbl["rankLabel"].strip() or len(tbl["rankLabel"]) > 12):
        raise RequestValidationError("chart.table.rankLabel must be a non-empty string ≤12 chars")
    # 分页器（WP-2 消费）：pageBy=分页单位（group=组原子，row=平铺切片）；
    # pagerMode=simple 跳过总数计算；pageSizeMax 收固定档位（页容量选择器上限档）。
    if "pageBy" in tbl and tbl["pageBy"] not in TABLE_PAGE_BY:
        raise RequestValidationError(f"chart.table.pageBy must be one of {list(TABLE_PAGE_BY)}; got {tbl['pageBy']!r}")
    if "pagerMode" in tbl and tbl["pagerMode"] not in TABLE_PAGER_MODES:
        raise RequestValidationError(f"chart.table.pagerMode must be one of {list(TABLE_PAGER_MODES)}; got {tbl['pagerMode']!r}")
    if "pageSizeMax" in tbl and (not isinstance(tbl["pageSizeMax"], int) or isinstance(tbl["pageSizeMax"], bool)
                                or tbl["pageSizeMax"] not in TABLE_PAGE_SIZES):
        raise RequestValidationError(
            f"chart.table.pageSizeMax must be one of {list(TABLE_PAGE_SIZES)}; got {tbl['pageSizeMax']!r}")
    # 导出包（WP-2 消费）：卡头「导出」开关与命名 + XLSX 水印（页眉文本）。
    if "export" in tbl:
        ex = tbl["export"]
        if not isinstance(ex, dict) or not ex:
            raise RequestValidationError("chart.table.export must be a non-empty object")
        unknown_ex = sorted(set(ex) - {"enabled", "name", "watermark"})
        if unknown_ex:
            raise RequestValidationError(
                f"chart.table.export unknown keys: {unknown_ex}; supported: ['enabled', 'name', 'watermark']")
        if "enabled" in ex and not isinstance(ex["enabled"], bool):
            raise RequestValidationError("chart.table.export.enabled must be a boolean")
        if "name" in ex and (not isinstance(ex["name"], str) or not ex["name"].strip() or len(ex["name"]) > 40):
            raise RequestValidationError("chart.table.export.name must be a non-empty string ≤40 chars")
        if "watermark" in ex and (not isinstance(ex["watermark"], str) or len(ex["watermark"]) > 20):
            raise RequestValidationError("chart.table.export.watermark must be a string ≤20 chars（空串 = 不带水印）")
    # 条件格式规则数组（WP-4 阶段二模型；此处只锁结构，求值语义归 WP-4）：
    # {target, source?, basis, op?, value?, actions{...}, affectTotals?, enabled?}。
    if "condFormat" in tbl:
        rules = tbl["condFormat"]
        if not isinstance(rules, list) or not rules or not all(isinstance(r, dict) for r in rules):
            raise RequestValidationError("chart.table.condFormat must be a non-empty list of rule objects")
        for _ri, r in enumerate(rules):
            unknown_r = sorted(set(r) - {"target", "source", "basis", "op", "value", "actions", "affectTotals", "enabled"})
            if unknown_r:
                raise RequestValidationError(
                    f"chart.table.condFormat[{_ri}] unknown keys: {unknown_r}; supported: "
                    "['actions', 'affectTotals', 'basis', 'enabled', 'op', 'source', 'target', 'value']")
            if not isinstance(r.get("target"), str) or not r.get("target"):
                raise RequestValidationError(
                    f"chart.table.condFormat[{_ri}].target must be a non-empty column name (规则作用的指标或维度列)")
            if r.get("basis") not in COND_FORMAT_BASES:
                raise RequestValidationError(
                    f"chart.table.condFormat[{_ri}].basis must be one of {list(COND_FORMAT_BASES)}; got {r.get('basis')!r}")
            if "source" in r and (not isinstance(r["source"], str) or not r["source"]):
                raise RequestValidationError(
                    f"chart.table.condFormat[{_ri}].source must be a non-empty column name (basis=动态字段 的取值列)")
            if "op" in r and (not isinstance(r["op"], str) or not r["op"].strip()):
                raise RequestValidationError(f"chart.table.condFormat[{_ri}].op must be a non-empty string (比较符，语义归 WP-4)")
            if "value" in r and (r["value"] is None or isinstance(r["value"], (dict, bool))):
                raise RequestValidationError(
                    f"chart.table.condFormat[{_ri}].value must be a scalar or a list of scalars; got {r['value']!r}")
            if "actions" in r:
                acts = r["actions"]
                if not isinstance(acts, dict) or not acts:
                    raise RequestValidationError(f"chart.table.condFormat[{_ri}].actions must be a non-empty object")
                unknown_act = sorted(set(acts) - set(COND_FORMAT_ACTIONS))
                if unknown_act:
                    raise RequestValidationError(
                        f"chart.table.condFormat[{_ri}].actions unknown keys: {unknown_act}; supported: {list(COND_FORMAT_ACTIONS)}")
                for _a in ("bold", "italic", "bar", "hideText", "fullRow"):
                    if _a in acts and not isinstance(acts[_a], bool):
                        raise RequestValidationError(f"chart.table.condFormat[{_ri}].actions.{_a} must be a boolean")
                for _a in ("text", "bg", "icon"):
                    if _a in acts and (not isinstance(acts[_a], str) or not acts[_a].strip()):
                        raise RequestValidationError(f"chart.table.condFormat[{_ri}].actions.{_a} must be a non-empty string")
                if "scale" in acts and (isinstance(acts["scale"], bool) or not isinstance(acts["scale"], (int, float))):
                    raise RequestValidationError(f"chart.table.condFormat[{_ri}].actions.scale must be a number (字号倍率)")
            for _b in ("affectTotals", "enabled"):
                if _b in r and not isinstance(r[_b], bool):
                    raise RequestValidationError(f"chart.table.condFormat[{_ri}].{_b} must be a boolean")
    # 副指标（WP-5 消费；一期 pos 仅 right）。
    if "subMeasures" in tbl:
        subs = tbl["subMeasures"]
        if not isinstance(subs, list) or not subs or not all(isinstance(s, dict) for s in subs):
            raise RequestValidationError("chart.table.subMeasures must be a non-empty list of objects")
        for _si, s in enumerate(subs):
            unknown_s = sorted(set(s) - {"measure", "offsetN", "grain", "show", "pos"})
            if unknown_s:
                raise RequestValidationError(
                    f"chart.table.subMeasures[{_si}] unknown keys: {unknown_s}; supported: "
                    "['grain', 'measure', 'offsetN', 'pos', 'show']")
            if not isinstance(s.get("measure"), str) or not s.get("measure"):
                raise RequestValidationError(f"chart.table.subMeasures[{_si}].measure must be a non-empty measure name")
            if not isinstance(s.get("offsetN"), int) or isinstance(s["offsetN"], bool) or s["offsetN"] < 1:
                raise RequestValidationError(f"chart.table.subMeasures[{_si}].offsetN must be an integer ≥1 (对比 N 期前)")
            if s.get("grain") not in SUB_MEASURE_GRAINS:
                raise RequestValidationError(
                    f"chart.table.subMeasures[{_si}].grain must be one of {list(SUB_MEASURE_GRAINS)}; got {s.get('grain')!r}")
            if s.get("show") not in SUB_MEASURE_SHOWS:
                raise RequestValidationError(
                    f"chart.table.subMeasures[{_si}].show must be one of {list(SUB_MEASURE_SHOWS)}; got {s.get('show')!r}")
            if s.get("pos") != "right":
                raise RequestValidationError(
                    f"chart.table.subMeasures[{_si}].pos must be 'right'（一期仅支持「指标右侧」档）")
    # 高度三态（WP-7 消费）：grow(默认自适应生长) | fixed(=manualH 既有语义) | bounded 有界高度。
    if "heightMode" in tbl:
        hm = tbl["heightMode"]
        if isinstance(hm, str):
            if hm not in TABLE_HEIGHT_MODES:
                raise RequestValidationError(
                    f"chart.table.heightMode must be 'grow'|'fixed'|{{'bounded': {{'maxPx': 200..4000}}}}; got {hm!r}")
        elif isinstance(hm, dict):
            unknown_hm = sorted(set(hm) - {"bounded"})
            if "bounded" not in hm or unknown_hm:
                raise RequestValidationError(
                    f"chart.table.heightMode object form only supports {{'bounded': {{'maxPx'}}}}; got keys {sorted(hm)}")
            b = hm["bounded"]
            if not isinstance(b, dict) or sorted(b) != ["maxPx"]:
                raise RequestValidationError("chart.table.heightMode.bounded only supports {'maxPx': 整数}")
            mx = b.get("maxPx")
            if not isinstance(mx, int) or isinstance(mx, bool) or not 200 <= mx <= 4000:
                raise RequestValidationError("chart.table.heightMode.bounded.maxPx must be an integer 200-4000")
        else:
            raise RequestValidationError(
                f"chart.table.heightMode must be 'grow'|'fixed'|{{'bounded': {{'maxPx': 200..4000}}}}; got {hm!r}")
    # 行维形态（WP-8 消费）：mergeRowDims = 多行维合并一列（名称 + 列宽 px）。
    if "mergeRowDims" in tbl:
        mrd = tbl["mergeRowDims"]
        if not isinstance(mrd, dict) or not mrd:
            raise RequestValidationError("chart.table.mergeRowDims must be a non-empty object {name?, width?}")
        unknown_m = sorted(set(mrd) - {"name", "width"})
        if unknown_m:
            raise RequestValidationError(f"chart.table.mergeRowDims unknown keys: {unknown_m}; supported: ['name', 'width']")
        if "name" in mrd and (not isinstance(mrd["name"], str) or not mrd["name"].strip() or len(mrd["name"]) > 40):
            raise RequestValidationError("chart.table.mergeRowDims.name must be a non-empty string ≤40 chars")
        if "width" in mrd and (isinstance(mrd["width"], bool) or not isinstance(mrd["width"], (int, float)) or mrd["width"] <= 0):
            raise RequestValidationError("chart.table.mergeRowDims.width must be a positive number (px)")
    # 迷你图趋势列（WP-9 消费）：列区=时间维时行尾「趋势」列。
    if "sparkline" in tbl:
        sp = tbl["sparkline"]
        if not isinstance(sp, dict) or not sp:
            raise RequestValidationError("chart.table.sparkline must be a non-empty object {enabled, label?}")
        unknown_sp = sorted(set(sp) - {"enabled", "label"})
        if unknown_sp:
            raise RequestValidationError(f"chart.table.sparkline unknown keys: {unknown_sp}; supported: ['enabled', 'label']")
        if not isinstance(sp.get("enabled"), bool):
            raise RequestValidationError("chart.table.sparkline.enabled must be a boolean")
        if "label" in sp and (not isinstance(sp["label"], str) or not sp["label"].strip() or len(sp["label"]) > 12):
            raise RequestValidationError("chart.table.sparkline.label must be a non-empty string ≤12 chars")
    # 总计计算方式（WP-11，对标 QBI）：{指标列名: 合计聚合方式}，至少 1 项。
    # 请求层只锁形状与枚举（幂等：compile 管道对同一 spec 跑两遍）；「键=真实列名」
    # 存在性在编译侧 _check_table_total_calc_columns 核（那里有含物化计算列的
    # canonical 表列 + 派生指标名单，align 同款先例）。
    if "totalCalc" in tbl:
        tc = tbl["totalCalc"]
        if not isinstance(tc, dict) or not tc:
            raise RequestValidationError(
                "chart.table.totalCalc must be a non-empty object {指标列名: 聚合方式}; "
                f"聚合 ∈ {list(TABLE_TOTAL_CALCS)}")
        for _tc_col, _tc_agg in tc.items():
            if not isinstance(_tc_col, str) or not _tc_col.strip():
                raise RequestValidationError(
                    f"chart.table.totalCalc keys must be non-empty column names; got {_tc_col!r}")
            if _tc_agg not in TABLE_TOTAL_CALCS:
                raise RequestValidationError(
                    f"chart.table.totalCalc[{_tc_col!r}] must be one of {list(TABLE_TOTAL_CALCS)}; got {_tc_agg!r}")


def validate_request(request: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise RequestValidationError("request must be an object")
    if request.get("schemaVersion") != 1:
        raise RequestValidationError("schemaVersion must be 1")
    # templateId 分档（docs/free-authoring-discussion.md §2.2）：standard-bi-v1 =
    # 固定骨架 + 自动填充（缺省）；free-v1 = 自由模式，声明即全部（封面/KPI 条/
    # 筛选带只渲染显式声明的，编译器不再替作者做主）。
    if request.get("templateId") not in ("standard-bi-v1", "free-v1"):
        raise RequestValidationError('templateId must be "standard-bi-v1" or "free-v1"')
    allowed_keys = {"schemaVersion", "reportId", "templateId", "title", "subtitle", "analysisGoal",
                    "columnRoles", "measureTypes", "semantic_types", "facts", "kpis", "charts", "insights", "filters", "sections",
                    "texts", "kpiPosition",
                    "style", "runtime", "source", "provenance", "_chartWarnings", "dataTable", "fieldRoles", "hierarchy",
                    "datasets", "relations", "pageWidth", "cover", "dataSourceMode"}
    # P1 (docs/semantic-config-and-derived-redesign.md §1/§5):
    # 1. ``derivedColumns`` is no longer accepted at the top level — it has
    #    been promoted into ``datasets[i].derived.columns`` (per-table slot)
    #    by ``spec_to_report.normalize_request`` before compilation. Models
    #    that still author the legacy key receive a clear, actionable error
    #    instead of being silently ignored or migrated.
    if "derivedColumns" in request:
        raise RequestValidationError(
            "derivedColumns 已废弃；请改用 datasets[i].derived.columns（在每张表下声明 op/left/right/name）。"
            "顶层 derivedColumns 不再被接受。"
        )
    unknown_keys = sorted(set(request) - allowed_keys)
    if unknown_keys:
        raise RequestValidationError(f"unsupported request fields: {unknown_keys}")
    if request.get("templateId") == "free-v1":
        # free 档按声明渲染，不再有骨架兜底——全空声明会产出一张白纸，这里给
        # 可行动报错而不是让模型事后对着一页空白排查。
        # 清单 = 会真实成块的声明（与 build_manifest 的 blocks 发射点一一对应）：
        # facts 摘除——它只进 provenance 和 {{fact:id}} 模板（被 insights/KPI 引用
        # 才可见），facts-only 曾过门后死在「blocks: [] should be non-empty」；
        # cover 纳入——声明的封面是真实块，「只要一张封面」曾被误报「没有任何区块
        # 声明」（探针 p5）；texts 纳入——独立文本块（2026-09-28 全部放开批）。
        _free_blocks = ("charts", "kpis", "insights", "sections", "cover", "dataTable", "texts")
        if not any(request.get(k) for k in _free_blocks):
            raise RequestValidationError(
                'free-v1（自由模式）按声明渲染：当前请求没有任何可见区块声明'
                '（charts/kpis/insights/sections/cover/dataTable/texts 全空）——'
                "请至少声明一项；想快速看数请去掉 free 走标准模板"
                "（facts 是口径数值，须由 insights/KPI 引用才可见，不计入区块）")
    if not isinstance(request.get("title"), str) or not request["title"].strip():
        raise RequestValidationError("title is required")
    if request.get("reportId") is not None and not SAFE_ID_RE.fullmatch(str(request["reportId"])):
        raise RequestValidationError("reportId must match ^[A-Za-z][A-Za-z0-9_-]{1,79}$")
    if request.get("pageWidth") is not None:
        # 校验即归一："full"→1920；build_manifest 只消费 resolve 后的 int。
        resolve_page_width(request.get("pageWidth"))
    ds_ids, default_ds = _validate_datasets(request)
    _validate_relations(request, ds_ids)
    # 数据源独立性硬控词汇（D2，docs/data-source-independence-and-merge-visibility-design.md）：
    # 枚举校验 + 缺省归一 + 与 relations 的矛盾守卫。independent（各数据源完全独立、
    # 禁止合并）与 relations（表间合并）互斥——同现 = 请求自相矛盾，给可行动报错，
    # 不任选其一静默执行（W36 真机教训：用户强调独立的两个数据源被擅自合并）。
    _dsm = request.get("dataSourceMode")
    if _dsm is None:
        # 缺省归一：下游（normalize_request/build_manifest/decision_ledger）用
        # spec.get("dataSourceMode") 恒拿到规范值，不必各自兜底缺省。
        request["dataSourceMode"] = "default"
    elif _dsm not in DATA_SOURCE_MODES:
        raise RequestValidationError(
            f"dataSourceMode must be one of {list(DATA_SOURCE_MODES)}; got {_dsm!r}")
    elif _dsm == "independent" and (request.get("relations") or []):
        raise RequestValidationError(
            "dataSourceMode:\"independent\"（数据源完全独立）与 relations（表间合并）互斥，"
            "二者不可同时声明：要独立请去掉 relations；要合并请去掉 dataSourceMode"
            "（缺省 default 即可声明 relations）")
    # （D3，2026-09-22）旧「多数据集 × derived/dimGroups」硬闸整段删除：编译侧同步改为
    # 逐数据集物化，派生/维度值分组声明在其所属数据集上校验（_validate_datasets 的引用
    # 宇宙/DAG 校验 + 编译期真实表再校验兜底），多数据集报告各表各自派生是合法形态。
    # 旧闸的报错文案只给「拆报告 / relations 合并」两条出路——没有「保持独立」选项，
    # 在 W36 真机上直接把执行 Agent 推向了擅自合并。
    # P1: top-level ``derivedColumns`` is rejected above (clear error before this
    # point) so the legacy whitelist validation that lived here is removed —
    # the canonical shape is ``datasets[i].derived.columns`` and is validated by
    # ``_validate_datasets``.
    roles = request.get("columnRoles")
    if roles is None:
        roles = {}
    if not isinstance(roles, dict):
        raise RequestValidationError("columnRoles must be an object")
    invalid = {col: role for col, role in roles.items() if role not in ALLOWED_ROLES}
    if invalid:
        raise RequestValidationError(f"invalid column roles: {invalid}")
    charts = request.get("charts")
    # free 档（声明即全部）允许零图表——纯 KPI/文本报告是合法形态，charts 键可
    # 整个省略（视同 []）；零声明已被上面的 free 空区块门拦住。standard 档维持
    # 必填 1-12。此前省键会撞「charts must contain 0-12 items (free-v1)」——文案
    # 说的是数量，缺的却是键本身，把按设计文档省键的作者带去硬补 charts。
    # 容量上限分档（2026-09-28「全部放开」）：free 的上限是自由度的天花板，
    # 不再照搬 standard 骨架的 12/4/3/8——charts 30 / kpis 12 / insights 10 /
    # sections 20（KPI 条本就按 (n+3)//4 自动折行，多行带是既有布局数学）。
    _free = request.get("templateId") == "free-v1"
    if charts is None and _free:
        request["charts"] = charts = []
    _charts_max = 30 if _free else 12
    _charts_min = 0 if _free else 1
    if not isinstance(charts, list) or len(charts) < _charts_min or len(charts) > _charts_max:
        raise RequestValidationError(
            f"charts must contain 1-12 items" if not _free
            else f"charts must contain 0-{_charts_max} items (free-v1)")
    ids = set()
    chart_problems: list[str] = []
    # Auto-assign missing/invalid chart ids so the model never has to hand-write
    # them (a chart without an id previously failed with "duplicate/invalid chart
    # id: None", forcing a manual fix). This is a deterministic safety net: the
    # id is used as the block/element id, and collisions are avoided. The assign
    # runs FIRST so section references still resolve to ids even when a chart has
    # other problems — chart errors are collected per chart (see chart_problems),
    # not fail-fast, so one bad chart no longer masks the rest.
    def _check_chart(chart: dict) -> None:
        if not isinstance(chart, dict):
            raise RequestValidationError("each chart must be an object")
        chart_keys = {"id", "presetId", "title", "encoding", "dimension", "dimensions", "breakdown", "breakdowns", "measures", "aggregation", "limit", "topK", "topKOther", "sort", "chartSpec", "annotation", "annotationPlacement", "datasetId", "style", "axis", "series", "layout", "dimensionBucket", "pagination", "table"}
        unknown_chart = sorted(set(chart) - chart_keys)
        if unknown_chart:
            # Actionable rejection: name the allowed chart fields and point the
            # author at where a rejected key actually lives (a chart card-level
            # filters filter never exists here; option is a compile artifact).
            where = {
                "filters": "filters 是顶层字段（request.filters），不是 chart 字段；请移到顶层",
                "option": "option 是编译产物，不接受输入；如需表格分页请用 chart.pagination=true，TOP-N 用 chart.topK",
                "chartSpec": "chartSpec 已支持（intent DSL）；若仍报错请检查是否同时填了 chartSpec 与 dimension/measures",
            }
            hints = "；".join(h for k, h in where.items() if k in unknown_chart)
            raise RequestValidationError(
                f"unsupported chart fields: {unknown_chart}; supported: "
                f"{sorted(chart_keys)}.{(' ' + hints) if hints else ''}"
            )
        if chart.get("dimensions") is not None:
            _dims_list = chart["dimensions"]
            if not isinstance(_dims_list, list) or not all(isinstance(d, str) and d for d in _dims_list):
                raise RequestValidationError("chart.dimensions must be a list of column names (行维度有序列表)")
            if len(set(_dims_list)) != len(_dims_list):
                raise RequestValidationError("chart.dimensions must not contain duplicates")
            # 多维表格批 A（设计稿 §3.8）：行区容量 = 面板 shapeRules('table').dims.max
            # 同一口径 ≤3；超限在请求层给出可行动错误，而不是编译/渲染期静默截断。
            if len(_dims_list) > 3:
                raise RequestValidationError(
                    f"chart.dimensions supports at most 3 row dimensions (行区最多 3 层); got {len(_dims_list)}")
        # chart 级 measures 同款类型闸：裸字符串 "利润" 会对字符串逐字符迭代出
        # 「列名」，报不可行动的未知列错误（scatter 的 len==2 检查还会被 2 字符
        # 中文串骗过）。
        if chart.get("measures") is not None:
            _m_list = chart["measures"]
            if not isinstance(_m_list, list) or not all(isinstance(x, str) and x.strip() for x in _m_list):
                raise RequestValidationError("chart.measures must be a list of column names; got %r" % (_m_list,))
        pagination = chart.get("pagination")
        if pagination is not None and not isinstance(pagination, bool):
            raise RequestValidationError("chart.pagination must be a boolean (true = 分页展示，仅对 table 有意义)")
        # 表格卡作者设置（批次一 + WP-3 请求层契约批）：布局/中国式表格开关与
        # 显示层新键在请求层直接声明，编译原样落 chart.editor.table（运行时唯一
        # 消费口）。形状/枚举校验单一真源 = _validate_chart_table。
        tbl = chart.get("table")
        if tbl is not None:
            _validate_chart_table(tbl)
        _tko = chart.get("topKOther")
        if _tko is not None and not isinstance(_tko, bool):
            raise RequestValidationError("chart.topKOther must be a boolean（true = TOP-N 截断时把长尾合并为「其他（N 项）」）")
        _sort_v = chart.get("sort")
        if isinstance(_sort_v, dict):
            # The intuitive {column, direction} spelling — normalise to the enum
            # the compiler understands instead of rejecting it outright.
            _sort_v = _normalize_sort_object(_sort_v, chart)
            chart["sort"] = _sort_v
        if _sort_v is not None and _sort_v not in ("desc", "time", "none", "asc"):
            raise RequestValidationError(
                f"chart.sort must be one of desc/time/none/asc (or an object whose direction is desc/asc "
                f"and column is the first measure); got {_sort_v!r}")
        for _k in ("topK", "limit"):
            _v = chart.get(_k)
            if _v is not None and (not isinstance(_v, int) or isinstance(_v, bool) or not 1 <= _v <= 100):
                raise RequestValidationError(f"chart.{_k} must be an integer 1-100 (取前 N 名); got {_v!r}")
        if chart.get("chartSpec"):
            # Flint-style intent: presetId/encoding are derived by compile_intent.
            if not isinstance(chart["chartSpec"], dict):
                raise RequestValidationError("chart.chartSpec must be an object")
            if chart.get("presetId") is not None and chart.get("presetId") not in ALLOWED_PRESETS:
                raise RequestValidationError(f"unsupported chart preset: {chart.get('presetId')!r}")
        elif chart.get("presetId") not in ALLOWED_PRESETS:
            raise RequestValidationError(f"unsupported chart preset: {chart.get('presetId')!r}")
        encoding = chart.get("encoding")
        if encoding is not None:
            if not isinstance(encoding, dict):
                raise RequestValidationError("chart.encoding must be an object")
            for _arr_k in ("measures", "dimensions"):
                _arr_v = encoding.get(_arr_k)
                if _arr_k == "measures" and isinstance(_arr_v, list):
                    # 跨表图表的 measures 是合法的 dict 形状 [{column, datasetId}]。
                    _ok = all((isinstance(x, str) and x.strip()) or
                              (isinstance(x, dict) and isinstance(x.get("column"), str) and x.get("column"))
                              for x in _arr_v)
                elif isinstance(_arr_v, list):
                    _ok = all(isinstance(x, str) and x.strip() for x in _arr_v)
                else:
                    _ok = False
                if _arr_v is not None and not _ok:
                    raise RequestValidationError(
                        f"encoding.{_arr_k} must be a list of non-empty strings; got {_arr_v!r}"
                        + ("（单个字段请写成单元素列表：[\"字段\"]）" if isinstance(_arr_v, str) else ""))
            _dim_v = encoding.get("dimension")
            if _dim_v is not None and (not isinstance(_dim_v, str) or not _dim_v.strip()):
                raise RequestValidationError(f"encoding.dimension must be a non-empty string; got {_dim_v!r}")
            unknown_encoding = sorted(set(encoding) - {"dimension", "dimensions", "breakdown", "breakdowns", "measures", "aggregation", "limit", "sort", "roles", "measureTypes", "bucket", "topK", "topKOther", "pagination"})
            if unknown_encoding:
                hint = "（topK/pagination 是 chart 级字段，写在 chart 上而不是 encoding 里）" \
                    if {"topK", "pagination"} & set(unknown_encoding) else ""
                raise RequestValidationError(f"unsupported chart encoding fields: {unknown_encoding}{hint}")
            # topK/pagination/limit are also accepted here because the chart-level
            # normalisation below merges them into encoding; validation must stay
            # IDEMPOTENT (the compile pipeline runs validate_request twice over
            # the same spec object).
            _enc_topk = encoding.get("topK")
            if _enc_topk is not None and (not isinstance(_enc_topk, int) or isinstance(_enc_topk, bool) or not 1 <= _enc_topk <= 100):
                raise RequestValidationError(f"encoding.topK must be an integer 1-100 (取前 N 名); got {_enc_topk!r}")
            if encoding.get("pagination") is not None and not isinstance(encoding["pagination"], bool):
                raise RequestValidationError("encoding.pagination must be a boolean (true = 分页展示，仅对 table 有意义)")
            _enc_limit = encoding.get("limit")
            if _enc_limit is not None and (not isinstance(_enc_limit, int) or isinstance(_enc_limit, bool) or _enc_limit < 1):
                raise RequestValidationError(f"encoding.limit must be a positive integer (行上限); got {_enc_limit!r}")
            # encoding.breakdowns 同口校验（chart 级归一会并入 encoding，校验必须幂等）。
            _enc_bds = encoding.get("breakdowns")
            if _enc_bds is not None:
                if not isinstance(_enc_bds, list) or not _enc_bds or not all(isinstance(d, str) and d.strip() for d in _enc_bds):
                    raise RequestValidationError(
                        f"encoding.breakdowns must be a non-empty list of column names (1-3 项); got {_enc_bds!r}")
                if len(_enc_bds) > 3:
                    raise RequestValidationError(
                        f"encoding.breakdowns supports at most 3 column dimensions (列区最多 3 层); got {len(_enc_bds)}")
                if len(set(_enc_bds)) != len(_enc_bds):
                    raise RequestValidationError("encoding.breakdowns must not contain duplicates")
                if encoding.get("breakdown"):
                    raise RequestValidationError(
                        "encoding.breakdown 与 encoding.breakdowns 互斥：单列维用 breakdown，多层用 breakdowns")
        # The compiler consumes breakdown from encoding. Accept the legacy
        # top-level chart.breakdown spelling and normalise it in place so it is
        # no longer silently ignored.
        if "breakdown" in chart and chart.get("breakdown"):
            if not isinstance(chart.get("breakdown"), str) or not chart["breakdown"].strip():
                raise RequestValidationError(
                    f"chart.breakdown must be a non-empty string (交叉列列名); got {chart.get('breakdown')!r}")
            encoding = chart.setdefault("encoding", {})
            if not isinstance(encoding, dict):
                raise RequestValidationError("chart.encoding must be an object when chart.breakdown is present")
            # 互斥兜底：encoding.breakdowns 直写 + chart.breakdown（旧词汇）混用——
            # 本步骤晚于 encoding 级互斥检查，必须在这里再拦一次（拍板 4：不给静默合并）。
            if encoding.get("breakdowns"):
                raise RequestValidationError(
                    "chart.breakdown 与 chart.breakdowns/encoding.breakdowns 互斥：单列维用 breakdown，"
                    "多层列维用 breakdowns（列表 1-3 项），不要同现")
            encoding.setdefault("breakdown", chart["breakdown"])
        # 多维表格批 B（拍板 4，docs/multidim-table-and-currency-design-2026-09-28.md
        # §3.7）：chart.breakdowns: string[1..3]（列区多层）。与 chart.breakdown（单
        # 字符串）**互斥**（同现报可行动错，不给静默合并的暗道）；同样归一进 encoding，
        # 编译产物恒写 roles.cols 数组 + roles.breakdown=cols[0] 镜像。
        if "breakdowns" in chart and chart.get("breakdowns") is not None:
            _bds = chart["breakdowns"]
            if not isinstance(_bds, list) or not _bds or not all(isinstance(d, str) and d.strip() for d in _bds):
                raise RequestValidationError(
                    f"chart.breakdowns must be a non-empty list of column names (交叉列列名列表, 1-3 项); got {_bds!r}")
            if len(_bds) > 3:
                raise RequestValidationError(
                    f"chart.breakdowns supports at most 3 column dimensions (列区最多 3 层); got {len(_bds)}")
            if len(set(_bds)) != len(_bds):
                raise RequestValidationError("chart.breakdowns must not contain duplicates")
            if chart.get("breakdown"):
                raise RequestValidationError(
                    "chart.breakdown 与 chart.breakdowns 互斥：单列维用 breakdown（字符串），"
                    "多层列维用 breakdowns（列表 1-3 项），不要同现")
            encoding = chart.setdefault("encoding", {})
            if not isinstance(encoding, dict):
                raise RequestValidationError("chart.encoding must be an object when chart.breakdowns is present")
            encoding.setdefault("breakdowns", list(_bds))
        if chart.get("dimension") is not None and (not isinstance(chart.get("dimension"), str) or not chart["dimension"].strip()):
            # 与 encoding.measures/dimensions 同一道类型闸：裸数字/对象会拖到
            # 编译期才以「图表维度不存在」浮出，校验层直接说清。
            raise RequestValidationError(
                f"chart.dimension must be a non-empty string; got {chart.get('dimension')!r}")
        # The compiler reads topK/limit/sort/pagination from ``encoding`` — it only
        # falls back to the chart itself when NO encoding block exists. The schema
        # documents them as chart-level fields, so normalise them into encoding in
        # place. Previously a chart-level chart.pagination=true was silently
        # dropped and R2 then demanded the very flag the author had already set.
        if isinstance(chart.get("encoding"), dict):
            _enc = chart["encoding"]
            if isinstance(_enc.get("sort"), dict):
                _enc["sort"] = _normalize_sort_object(_enc["sort"], chart)
            if chart.get("sort") is not None:
                _enc.setdefault("sort", chart["sort"])
            if chart.get("pagination") is not None:
                _enc.setdefault("pagination", chart["pagination"])
            if chart.get("topK") is not None:
                _enc.setdefault("topK", chart["topK"])
            if chart.get("topKOther") is not None:
                _enc.setdefault("topKOther", chart["topKOther"])
            elif chart.get("limit") is not None and "topK" not in _enc:
                # chart.limit is the documented topN alias → canonical topK slot.
                # (encoding.limit is the row-cap slot with different semantics and
                # stays untouched.)
                _enc["topK"] = chart["limit"]
        if chart.get("dimensionBucket") is not None:
            db = chart["dimensionBucket"]
            if not isinstance(db, dict):
                raise RequestValidationError("chart.dimensionBucket must be an object")
            unknown_db = sorted(set(db) - {"source", "bucketed", "bucket"})
            if unknown_db:
                raise RequestValidationError(f"unsupported chart.dimensionBucket fields: {unknown_db}")
            # quarter 是一等粒度（date_parse.BUCKETS / derived_model.GRAINS /
            # aggregator._bucket_expr / 运行时粒度选择器全链路支持）——窗口指标
            # grain:'quarter' 要求图表分桶与声明一致，请求层拒绝它就配不成对。
            if db.get("bucket") not in {None, "raw", "month", "quarter", "week", "day", "year"}:
                raise RequestValidationError("chart.dimensionBucket.bucket must be raw/month/quarter/week/day/year")
        if chart.get("presetId") == "scatter.correlation":
            enc = chart.get("encoding") or {}
            measures = enc.get("measures") or []
            if len(measures) != 2:
                raise RequestValidationError("scatter.correlation requires exactly two measures")
            top_dim_role = roles.get(enc.get("dimension"))
            if top_dim_role == "measure":
                raise RequestValidationError("scatter.correlation dimension must not be a measure")
        axis = chart.get("axis")
        if axis is not None:
            if not isinstance(axis, dict) or not axis.get("dimensionKey"):
                raise RequestValidationError("chart.axis must be an object with a dimensionKey")
        series = chart.get("series")
        if series is not None:
            if not isinstance(series, list) or not series:
                raise RequestValidationError("chart.series must be a non-empty array")
            for s in series:
                if not isinstance(s, dict) or not s.get("column") or not s.get("datasetId"):
                    raise RequestValidationError("chart.series items need {column, datasetId}")
        annotation = chart.get("annotation")
        if annotation is not None and (not isinstance(annotation, str) or len(annotation) > 2000):
            raise RequestValidationError("chart.annotation must be a string of at most 2000 chars")
        placement = chart.get("annotationPlacement")
        if placement not in {None, "below", "right"}:
            raise RequestValidationError("chart.annotationPlacement must be 'below' or 'right'")
        layout = chart.get("layout")
        if layout is not None:
            if not isinstance(layout, dict):
                raise RequestValidationError("chart.layout must be an object")
            unknown_layout = sorted(set(layout) - {"x", "y", "w", "h"})
            if unknown_layout:
                raise RequestValidationError(f"chart.layout unsupported fields: {unknown_layout}")
            for key in ("x", "y", "w", "h"):
                if key in layout and (not isinstance(layout[key], int) or layout[key] < 0):
                    raise RequestValidationError(f"chart.layout.{key} must be a non-negative integer")
            if "w" in layout and not (1 <= layout["w"] <= 12):
                raise RequestValidationError("chart.layout.w must be 1-12")
            if "h" in layout and layout["h"] < 1:
                raise RequestValidationError("chart.layout.h must be at least 1")
        cds = chart.get("datasetId")
        if cds is not None and (not isinstance(cds, str) or cds not in ds_ids):
            raise RequestValidationError(f"chart references unknown datasetId: {cds!r}")
        _validate_card_style(chart.get("style"), f"chart {chart.get('id') or '?'}")

    for _idx, chart in enumerate(charts):
        if not isinstance(chart, dict):
            chart_problems.append(f"charts[{_idx}] must be an object")
            continue
        cid = chart.get("id")
        if not isinstance(cid, str) or not SAFE_ID_RE.fullmatch(cid) or cid in ids:
            # Deterministic fallback: chart-1, chart-2, ... or chart-<n>-1 if taken.
            base = f"chart-{_idx + 1}"
            candidate = base
            suffix = 1
            while candidate in ids:
                candidate = f"{base}-{suffix}"
                suffix += 1
            chart["id"] = candidate
        ids.add(chart["id"])
        try:
            _check_chart(chart)
        except RequestValidationError as exc:
            chart_problems.append(f"charts[{_idx}] ({chart['id']}): {exc}")

    # Chart-family diversity (non-fatal advisory): a report dominated by a single
    # chart family reads monotonous, even when each chart is individually valid.
    # Map each preset to the visual family it answers, then warn when one family
    # fills most of the report AND the data plausibly supports other families
    # (a second measure → relationship, a time/ordinal dimension → trend, ≥3
    # ordered stages → stage). Stored on the request as _chartWarnings so the
    # authoring layer can surface it without failing the gate.
    FAMILY = {
        "bar.rank": "比较", "bar.stacked": "比较",
        "line.trend": "趋势", "area.trend": "趋势",
        "scatter.correlation": "关系",
        "pie.share": "构成", "donut.share": "构成",
        "funnel.stage": "流程",
        "table.aggregate": "明细", "table.detail": "明细",
    }
    if charts:
        fam_counts = {}
        CHARTTYPE_TO_PRESET = {"mixed": "bar.rank", "bar": "bar.rank", "line": "line.trend",
                               "area": "area.trend", "stackedBar": "bar.stacked", "pie": "pie.share",
                               "donut": "donut.share", "funnel": "funnel.stage", "scatter": "scatter.correlation",
                               "table": "table.aggregate"}
        for chart in charts:
            if chart.get("chartSpec"):
                pseudo = (chart["chartSpec"] or {}).get("chartType")
                fid = FAMILY.get(CHARTTYPE_TO_PRESET.get(pseudo), "其他")
            else:
                fid = FAMILY.get(chart.get("presetId"), "其他")
            fam_counts[fid] = fam_counts.get(fid, 0) + 1
        dominant = max(fam_counts, key=fam_counts.get)
        dominant_count = fam_counts[dominant]
        # A single chart can't be monotonous; only judge reports with 3+ charts.
        if len(charts) >= 3 and dominant_count >= len(charts) - 1:
            measure_cols = [c for c, r in roles.items() if r == "measure"]
            dim_cols = [c for c, r in roles.items() if r in {"dimension", "time"}]
            hints = []
            if len(measure_cols) >= 2:
                hints.append("多指标→scatter.correlation")
            if any(r == "time" for r in roles.values()):
                hints.append("时间维→line/area.trend")
            if len(dim_cols) > 1:
                hints.append("多维度→bar.stacked 或 交叉表(table+breakdown)")
            if hints:
                warnings = request.setdefault("_chartWarnings", [])
                warnings.append(
                    f"报告 {dominant_count}/{len(charts)} 个图表都是「{dominant}」，可考虑混用："
                    + "; ".join(hints)
                )
    # Story sections: group charts under a narrative chapter. Purely structural —
    # sections never alter chart semantics (see docs/story-sections-design.md).
    # A chart id may belong to at most one section; unassigned charts fall into
    # an implicit trailing body section at compile time (backward compatible).
    sections = request.get("sections")
    section_problems: list[str] = []
    if sections is not None:
        # 上限分档：free 20（页签带可多节），standard 8（骨架假设不变）。
        _sections_max = 20 if request.get("templateId") == "free-v1" else 8
        if not isinstance(sections, list) or len(sections) > _sections_max:
            raise RequestValidationError(f"sections must contain at most {_sections_max} items")
        seen_sid: set = set()
        seen_chart: set = set()
        # Sections may reference charts by TITLE as well as by id: the
        # auto-assigned ids (chart-1, chart-2, …) are positions an author cannot
        # guess, and real authoring runs stalled on "references unknown chart id"
        # after writing bare titles. A unique title resolves to its chart id in
        # place; an ambiguous or unknown reference errors with the id↔title map.
        id_title: dict[str, str] = {}
        title_to_ids: dict[str, list[str]] = {}
        for ch in charts:
            if not isinstance(ch, dict) or not isinstance(ch.get("id"), str) or ch["id"] not in ids:
                continue
            t = ch.get("title").strip() if isinstance(ch.get("title"), str) else ""
            if t:
                id_title[ch["id"]] = t
                title_to_ids.setdefault(t, []).append(ch["id"])

        def _check_section(sec: dict, _si: int) -> None:
            unknown_sec = sorted(set(sec) - {"id", "title", "narrative", "insight", "charts", "textKind", "style"})
            if unknown_sec:
                raise RequestValidationError(f"unsupported section fields: {unknown_sec}")
            sid = sec.get("id")
            if not isinstance(sid, str) or not SAFE_ID_RE.fullmatch(sid) or sid in seen_sid:
                # Deterministic fallback: section-1, section-2, ... (same safety
                # net as chart ids) so the author never has to hand-write them.
                base = f"section-{_si + 1}"
                candidate, suffix = base, 1
                while candidate in seen_sid:
                    candidate = f"{base}-{suffix}"
                    suffix += 1
                sec["id"] = candidate
                sid = candidate
            seen_sid.add(sid)
            title = sec.get("title")
            if not isinstance(title, str) or (not title.strip() and sec.get("textKind") != "divider"):
                raise RequestValidationError(f"section {sid} requires a non-empty title")
            if isinstance(title, str) and len(title) > 120:
                raise RequestValidationError(f"section {sid} title may be at most 120 chars")
            for key, cap in (("narrative", 4000), ("insight", 2000)):
                value = sec.get(key)
                if value is not None and (not isinstance(value, str) or len(value) > cap):
                    raise RequestValidationError(f"section {sid} {key} must be a string of at most {cap} chars")
            # Text-card skeleton + card-level style preset (P2, whitelisted only).
            if sec.get("textKind") is not None and sec.get("textKind") not in ALLOWED_TEXT_KINDS:
                raise RequestValidationError(f"section {sid} textKind must be one of {sorted(ALLOWED_TEXT_KINDS)}")
            if sec.get("style") is not None:
                _validate_card_style(sec["style"], f"section {sid}")
            sec_charts = sec.get("charts")
            if sec_charts is None:
                sec_charts = []
            if not isinstance(sec_charts, list):
                raise RequestValidationError(f"section {sid} charts must be an array of chart ids")
            for _ci, cid in enumerate(sec_charts):
                if not isinstance(cid, str):
                    raise RequestValidationError(f"section {sid} charts[{_ci}] must be a chart id (string)")
                if cid not in ids:
                    hits = title_to_ids.get(cid.strip()) or []
                    if len(hits) == 1:
                        sec_charts[_ci] = hits[0]
                        cid = hits[0]
                    elif len(hits) > 1:
                        raise RequestValidationError(
                            f"section {sid} references chart title {cid!r} which matches {len(hits)} charts — "
                            "give the charts distinct titles or reference their ids")
                    else:
                        available = ", ".join(f"{i}（{t}）" for i, t in id_title.items()) \
                            or ", ".join(sorted(ids)) or "（无图表）"
                        raise RequestValidationError(
                            f"section {sid} references unknown chart id: {cid!r}; 现有图表: {available}")
                if cid in seen_chart:
                    raise RequestValidationError(f"chart {cid} may belong to at most one section")
                seen_chart.add(cid)

        for _si, sec in enumerate(sections):
            if not isinstance(sec, dict):
                section_problems.append(f"sections[{_si}] must be an object")
                continue
            try:
                _check_section(sec, _si)
            except RequestValidationError as exc:
                section_problems.append(f"sections[{_si}]: {exc}")
    if not isinstance(request.get("kpis"), (list, type(None))):
        raise RequestValidationError("kpis must be a list (got " + type(request.get("kpis")).__name__ + ")")
    # 上限分档：free 12（KPI 条按每行 4 张自动折行，(n+3)//4 行是既有布局数学），
    # standard 4（骨架垫满假设不变）。
    _kpis_max = 12 if request.get("templateId") == "free-v1" else 4
    if len(request.get("kpis") or []) > _kpis_max:
        raise RequestValidationError(f"kpis may contain at most {_kpis_max} items")
    kpi_problems: list[str] = []
    # 「静态 value KPI × 交互筛选」advisory 的判定要用**谓词非空**：空 dict 形状
    # （{} 或 {"all": []}）是真值但不含任何谓词，旧判定会凭空告诫「筛选后该值
    # 不会重算」，把没声明筛选的作者引去改没问题的声明（第七轮审视实锤）。
    _filters_raw = request.get("filters")
    if isinstance(_filters_raw, dict):
        _filters_declared = bool(list(_filters_raw.get("all") or []) or list(_filters_raw.get("any") or []))
    else:
        _filters_declared = bool(_filters_raw)
    # KPI 键白名单（ compilers/runtime 消费键 ∪ _canon_kpi 别名）。kpis/charts/
    # datasets 都有 unknown-key 检查，唯独 KPI 没有时，拼错键会静默降级
    # （如 datsetIds 拼错 → KPI 静默归属默认表，错数字零报错）。
    _KPI_KEYS = {"id", "label", "title", "type", "factType", "factId", "measure", "column", "field",
                 "dimension", "countDim", "aggregation", "datasetId", "measureType", "format",
                 "value", "rawValue", "derived", "style", "decimals", "unit", "compare", "target",
                 "bubbles", "goal"}
    for _ki, kpi in enumerate(request.get("kpis") or []):
        try:
            if not isinstance(kpi, dict):
                raise RequestValidationError("each kpi must be an object")
            unknown_kpi = sorted(set(kpi) - _KPI_KEYS)
            if unknown_kpi:
                raise RequestValidationError(f"unknown kpi fields: {unknown_kpi}")
            kid = kpi.get("datasetId")
            if kid is not None and (not isinstance(kid, str) or kid not in ds_ids):
                raise RequestValidationError(f"kpi references unknown datasetId: {kid!r}")
            _cmp = kpi.get("compare")
            if _cmp is not None and _cmp not in ("mom", "yoy"):
                # 拼错的值（如 "wow"）曾静默无对比副行——运行时只认 mom/yoy。
                raise RequestValidationError(f"kpi.compare must be 'mom' or 'yoy'; got {_cmp!r}")
            _validate_card_style(kpi.get("style"), "kpi")
        except RequestValidationError as exc:
            kpi_problems.append(f"kpis[{_ki}]: {exc}")
        if isinstance(kpi, dict) and kpi.get("value") is not None and _filters_declared:
            # A value-only KPI never re-computes (editor-runtime keeps the authored
            # number verbatim), so with interactive filters it goes stale. Point
            # the author at the script-computed fact types instead of letting the
            # report ship a number that contradicts the filtered charts.
            request.setdefault("_chartWarnings", []).append(
                f"kpis[{_ki}]（{kpi.get('label') or kpi.get('title') or '?'}）提供了静态 value 且请求声明了 filters —— "
                "筛选后该值不会重算；如需随筛选联动请改用脚本计算的口径（factType count/distinctCount + measure/dimension）")
    if not isinstance(request.get("insights"), (list, type(None))):
        raise RequestValidationError("insights must be a list (got " + type(request.get("insights")).__name__ + ")")
    # 上限分档：free 10，standard 3（关键洞察带的编辑部位假设不变）。
    _insights_max = 10 if request.get("templateId") == "free-v1" else 3
    if len(request.get("insights") or []) > _insights_max:
        raise RequestValidationError(f"insights may contain at most {_insights_max} items")
    for _ii, ins in enumerate(request.get("insights") or []):
        if isinstance(ins, dict):
            # 对象形态的合法键 = 渲染端 _insight_text 实际读取的键（text/content/
            # description/body/summary/finding/title/heading）；其它键一律拒绝。
            unknown_ins = sorted(set(ins) - {"text", "content", "description", "body", "summary", "finding", "title", "heading"})
            if unknown_ins:
                raise RequestValidationError(f"insights[{_ii}]: unknown fields {unknown_ins}（正文请用 text/title 等键）")
        elif not isinstance(ins, str):
            raise RequestValidationError(f"insights[{_ii}] must be a string or an object")
    # texts：独立文本块（2026-09-28「全部放开」批）。自由提示词最常见的「加一段
    # 说明文字」此前没有直接声明形态（insights=编辑部「关键洞察」卡、narrative 绑
    # 页签、annotation 绑图表）。texts 是 body 级自由卡：标题+正文（Markdown）、
    # textStyle/cardColor 走统一卡样式、layout 可钉任意位置、after 可指定跟在
    # 某张图表之后。渲染为段模型文本卡（与批注/叙述带同一 data-text-block 管线）。
    if not isinstance(request.get("texts"), (list, type(None))):
        raise RequestValidationError("texts must be a list (got " + type(request.get("texts")).__name__ + ")")
    if len(request.get("texts") or []) > 20:
        raise RequestValidationError("texts may contain at most 20 items")
    text_ids: set = set()
    for _ti, txt in enumerate(request.get("texts") or []):
        if not isinstance(txt, dict):
            raise RequestValidationError(f"texts[{_ti}] must be an object")
        unknown_txt = sorted(set(txt) - {"id", "title", "text", "style", "layout", "after"})
        if unknown_txt:
            raise RequestValidationError(f"texts[{_ti}]: unknown fields {unknown_txt}（正文请用 text 键）")
        if not isinstance(txt.get("text"), str) or not txt["text"].strip():
            raise RequestValidationError(f"texts[{_ti}].text is required（要显示的正文，支持 Markdown）")
        _tlay = txt.get("layout")
        if _tlay is not None:
            if not isinstance(_tlay, dict):
                raise RequestValidationError(f"texts[{_ti}].layout must be an object")
            if sorted(set(_tlay) - {"x", "y", "w", "h"}):
                raise RequestValidationError(f"texts[{_ti}].layout unsupported fields: {sorted(set(_tlay) - {'x', 'y', 'w', 'h'})}")
            for _k in ("x", "y", "w", "h"):
                if _k in _tlay and (not isinstance(_tlay[_k], int) or _tlay[_k] < 0):
                    raise RequestValidationError(f"texts[{_ti}].layout.{_k} must be a non-negative integer")
            if "w" in _tlay and not 1 <= _tlay["w"] <= 12:
                raise RequestValidationError(f"texts[{_ti}].layout.w must be 1-12")
            if "h" in _tlay and _tlay["h"] < 1:
                raise RequestValidationError(f"texts[{_ti}].layout.h must be at least 1")
        if txt.get("after") is not None and (not isinstance(txt.get("after"), str) or not txt["after"].strip()):
            raise RequestValidationError(f"texts[{_ti}].after must be a chart id（文本块插入该图表之后）")
        _validate_card_style(txt.get("style"), f"texts[{_ti}]")
        _tid = txt.get("id")
        if _tid is not None:
            if not isinstance(_tid, str) or not SAFE_ID_RE.fullmatch(_tid) or _tid in text_ids:
                raise RequestValidationError(f"duplicate/invalid text id: {_tid!r}")
            text_ids.add(_tid)
    # kpiPosition：KPI 条位置（free 限定）。top = 封面/筛选之下（缺省，standard
    # 一字不动）；bottom = 图表/文本区之后、洞察卡之下——「KPI 放底部」此前在
    # 请求层不可表达（带序固定 A 档）。
    _kpi_pos = request.get("kpiPosition")
    if _kpi_pos is not None:
        if request.get("templateId") != "free-v1":
            raise RequestValidationError('kpiPosition 仅 free-v1 支持（standard 骨架固定：封面→筛选→KPI→图表）')
        if _kpi_pos not in ("top", "bottom"):
            raise RequestValidationError(f'kpiPosition must be "top" or "bottom"; got {_kpi_pos!r}')
    if not isinstance(request.get("facts"), (list, type(None))):
        raise RequestValidationError("facts must be a list (got " + type(request.get("facts")).__name__ + ")")
    fact_ids = set()
    for fact in request.get("facts") or []:
        if not isinstance(fact, dict) or fact.get("type") not in ALLOWED_FACTS:
            raise RequestValidationError(f"unsupported fact: {fact!r}")
        if set(fact) - {"id", "type", "dimension", "measure", "aggregation", "n"}:
            raise RequestValidationError(f"unsupported fact fields: {sorted(set(fact) - {'id', 'type', 'dimension', 'measure', 'aggregation', 'n'})}")
        if not isinstance(fact.get("id"), str) or not SAFE_ID_RE.fullmatch(fact["id"]) or fact["id"] in fact_ids:
            raise RequestValidationError(f"duplicate/invalid fact id: {fact.get('id')!r}")
        fact_ids.add(fact["id"])
    measure_types = request.get("measureTypes") or {}
    request["measureTypes"] = _normalize_measure_types(measure_types)
    # semantic_types productization (docs/semantic-config-enhancement.md §3): the
    # former free-form field becomes a strict per-column measure declaration that
    # is normalised and merged into the canonical measureTypes map (explicit
    # measureTypes win on a column collision). Consumers that still read
    # semantic_types (intent DSL) see the same object untouched.
    semantic_types = request.get("semantic_types") or {}
    if semantic_types:
        if not isinstance(semantic_types, dict):
            raise RequestValidationError("semantic_types must be an object")
        for column, semantic in list(semantic_types.items()):
            if not isinstance(column, str):
                raise RequestValidationError("semantic_types keys must be column names")
            merged = None
            if isinstance(semantic, str):
                merged = {"type": semantic}
            elif isinstance(semantic, dict):
                unknown = set(semantic) - {"type", "aggregation", "decimals", "format"}
                if unknown:
                    raise RequestValidationError(f"unsupported semantic_types fields for {column!r}: {sorted(unknown)}")
                if semantic.get("format") is not None and semantic["format"] not in {"percent"}:
                    raise RequestValidationError(f"semantic_types[{column!r}].format must be 'percent'")
                merged = dict(semantic)
            else:
                raise RequestValidationError(f"semantic_types[{column!r}] must be a string or object")
            # Canonicalise the type token exactly like measureTypes.
            single = _normalize_measure_types({column: merged}, f"semantic_types[{column}].measureType")[column]
            semantic_types[column] = single
            if column not in request["measureTypes"]:
                request["measureTypes"][column] = single
        request["semantic_types"] = semantic_types
    filters = request.get("filters") or []
    if isinstance(filters, dict):
        if set(filters) - {"all", "any"}:
            raise RequestValidationError("filters only supports all/any groups")
        predicates = list(filters.get("all") or []) + ([{"any": list(filters.get("any") or [])}] if filters.get("any") else [])
    elif isinstance(filters, list):
        predicates = filters
    else:
        raise RequestValidationError("filters must be an array or {all,any}")
    leaves = 0
    def check_filter(predicate: Any, nested: bool = False) -> None:
        nonlocal leaves
        if not isinstance(predicate, dict):
            raise RequestValidationError(f"invalid filter predicate: {predicate!r}")
        if "any" in predicate:
            children = predicate.get("any") or []
            if not isinstance(children, list) or not (1 <= len(children) <= 10):
                raise RequestValidationError("filters.any must contain 1-10 predicates")
            if nested:
                raise RequestValidationError("filters.any may not be nested")
            for child in children:
                check_filter(child, True)
            return
        _op = str(predicate.get("op", "=")).lower()
        if _op not in ALLOWED_FILTER_OPS:
            # 可行动的报错：列出支持的算子，并指出区间怎么表达（否则作者会把
            # gte+lte 两条写成一条 non-AND 谓词，静默只生效一半）。
            raise RequestValidationError(
                f"invalid filter operator: {predicate!r} — 支持 {sorted(ALLOWED_FILTER_OPS)}；"
                "数值区间用 op:'between' + value:[low, high]，日期范围用 op:'daterange' + "
                "value:['YYYY-MM-DD','YYYY-MM-DD']（两端任一可为 null = 开口）")
        if _op in ("between", "daterange"):
            _val = predicate.get("value")
            if not isinstance(_val, (list, tuple)) or len(_val) != 2:
                raise RequestValidationError(
                    f"filter op {_op!r} 需要 value:[low, high] 两元素数组（任一可为 null 表示开口）: {predicate!r}")
            # 元素必须是标量或 null：dict 参与比较会在 _row_filter / 服务端深处
            # 产生不可预期行为（Number({...}) = NaN / float(dict) 抛错）。
            for _bv in _val:
                if _bv is not None and not isinstance(_bv, (str, int, float, bool)):
                    raise RequestValidationError(
                        f"filter op {_op!r} 的区间端点必须是标量或 null; got {_bv!r}")
        has_col = isinstance(predicate.get("column"), str) and bool(predicate.get("column"))
        has_key = isinstance(predicate.get("dimensionKey"), str) and bool(predicate.get("dimensionKey"))
        if not has_col and not has_key:
            raise RequestValidationError("filter.column or filter.dimensionKey is required")
        fds = predicate.get("datasetId")
        if fds is not None and (not isinstance(fds, str) or fds not in ds_ids):
            raise RequestValidationError(f"filter references unknown datasetId: {fds!r}")
        leaves += 1
    filter_problems: list[str] = []
    for _fi, predicate in enumerate(predicates):
        try:
            check_filter(predicate)
        except RequestValidationError as exc:
            filter_problems.append(f"filters[{_fi}]: {exc}")
    if leaves > 10:
        raise RequestValidationError("filters may contain at most 10 predicates")
    # Advisory: a predicate without a value declares an interactive control with
    # nothing selected. Compile (_row_filter), server (_filter_clause) and the
    # runtime filter evaluators all treat it as NO constraint — previously an
    # `in` with no value compared against an empty list and eliminated every
    # row, so the whole report failed with "chart has no data rows".
    _NO_VALUE_OPS = {"is null", "is not null"}
    for _fi, predicate in enumerate(predicates):
        if isinstance(predicate, dict) and "any" not in predicate \
                and str(predicate.get("op", "=")).lower() not in _NO_VALUE_OPS \
                and predicate.get("value") in (None, "", []):
            request.setdefault("_chartWarnings", []).append(
                f"filters[{_fi}]（{predicate.get('column') or predicate.get('dimensionKey') or '?'}）："
                "未提供 value —— 按「未选任何值（全部）」处理，不加数据约束；若本意是过滤数据请补 value")
    style = request.get("style") or {}
    if not isinstance(style, dict) or set(style) - {"accent", "density", "radius", "fontScale", "theme", "chartTheme"}:
        raise RequestValidationError("style only supports accent/density/radius/fontScale/theme/chartTheme")
    if style.get("theme") not in {None, "notion", "default"}:
        raise RequestValidationError("invalid style.theme (expected 'notion' or 'default')")
    # chartTheme = 唯一「报告主题」轴（docs/report-theme-convergence-design.md）：
    # 白名单单一真源 = report_themes.theme_ids()（新主题进表即合法，不许第二份清单）。
    from report_themes import theme_ids as _theme_ids
    if style.get("chartTheme") not in {None, *_theme_ids()}:
        raise RequestValidationError("invalid style.chartTheme (expected one of %s)" % (", ".join(_theme_ids()),))
    if style.get("accent") not in {None, "blue", "teal", "orange", "purple"}:
        raise RequestValidationError("invalid style.accent")
    if style.get("density") not in {None, "comfortable", "compact"}:
        raise RequestValidationError("invalid style.density")
    if style.get("radius") not in {None, "none", "small", "medium"}:
        raise RequestValidationError("invalid style.radius")
    if style.get("fontScale") not in {None, "sm", "md", "lg"}:
        raise RequestValidationError("invalid style.fontScale")
    runtime = request.get("runtime") or {}
    if not isinstance(runtime, dict) or set(runtime) - {"mode", "singlefileMaxRows", "serverUrl", "tabs", "editPassword"}:
        raise RequestValidationError("runtime only supports mode/singlefileMaxRows/serverUrl/tabs/editPassword")
    if runtime.get("editPassword") is not None and (not isinstance(runtime["editPassword"], str) or not 1 <= len(runtime["editPassword"]) <= 64):
        raise RequestValidationError("runtime.editPassword must be a string of 1-64 chars")
    if runtime.get("tabs") not in {None, "auto", "on", "off"}:
        raise RequestValidationError("invalid runtime.tabs (expected auto/on/off)")
    if runtime.get("mode") not in {None, "auto", "singlefile", "server"}:
        raise RequestValidationError("invalid runtime.mode")
    _smr = runtime.get("singlefileMaxRows")
    # 类型显式检查（其余整数字段同款）：dict/list 会让 int() 抛裸 TypeError
    # 绕过可行动报错通道；bool 是 int 子类也要拒。
    if _smr is not None and (not isinstance(_smr, int) or isinstance(_smr, bool) or not 1 <= _smr <= 50000):
        raise RequestValidationError("runtime.singlefileMaxRows must be an integer 1-50000")
    if runtime.get("serverUrl") is not None:
        parsed = urlparse(str(runtime["serverUrl"]))
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise RequestValidationError("runtime.serverUrl must point to localhost")
        if parsed.path not in {"", "/", "/api", "/api/v1"} or parsed.query or parsed.fragment:
            raise RequestValidationError("runtime.serverUrl may only contain an optional /api or /api/v1 suffix")
    # Report every collected per-item problem at once (charts / sections / kpis
    # / filters). Authoring agents otherwise burn one round-trip per error,
    # fixing a single message and re-validating N times for N mistakes.
    all_problems = chart_problems + section_problems + kpi_problems + filter_problems
    if all_problems:
        raise RequestValidationError(
            f"{len(all_problems)} problem(s) in request:\n- " + "\n- ".join(all_problems))
    return request


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("request_pos", nargs="?", default=None,
                        help="path to report_request.json (positional)")
    parser.add_argument("--spec", "--request", dest="request_opt", default=None,
                        help="path to report_request.json (flag form; report_build uses --spec)")
    args = parser.parse_args()
    path = args.request_opt or args.request_pos
    if not path:
        parser.error("provide the request path (positional argument or --spec)")
    try:
        request = json.loads(Path(path).read_text(encoding="utf-8"))
        validate_request(request)
    except Exception as exc:
        print(f"Invalid report request: {exc}")
        return 2
    print("report_request.json valid")
    for warning in request.get("_chartWarnings") or []:
        print(f"[advisory] {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
