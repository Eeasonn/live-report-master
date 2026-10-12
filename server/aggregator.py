"""SQL aggregator: turns /aggregate requests into SQL and runs them.

The aggregator is engine-agnostic in intent, but emits SQLite-friendly
SQL by default. DuckDB accepts the same SQL for the most part (strftime
works in DuckDB too). The engine's `execute` returns dict rows.

Returned AggregateResponse carries both the grouped rows AND the
overall aggregates (sum/mean/min/max/count) for the un-grouped dataset
— used by truth validation to recompute declared numbers.
"""
from __future__ import annotations

import re
import time
from typing import Any, Optional

from .store import Engine

# 端点归一共用 scripts/date_parse.py（store 已把 scripts/ 插进 sys.path）——
# 与编译侧 _row_filter、浏览器 matchFilter 三端同解析器（dates 三端一致守卫）。
import date_parse as _date_parse  # noqa: E402
from .types import AggregateRequest, AggregateResponse


# 名实一致（第 2 轮收口批 P3）：本正则只拒绝「非字符串 / 空串 / NUL」——它**不是**
# 严格标识符白名单，也绝不能收紧成 [A-Za-z0-9_]：CJK 列名/数据集名（销售×库存）是
# 受支持场景（app.py 路由注释同款承诺）。SQL 注入防护由 _quote 的双引号包裹 +
# 内部双写（" → ""）保证，这里只是最后一道形状闸（拒绝控制字符注入）。
NON_EMPTY_IDENT = re.compile(r"^[^\x00]+$")

SUPPORTED_AGG = {"sum", "average", "count", "countd", "min", "max", "none"}
# 数值聚合：TEXT 列（任一脏值即整列 TEXT）上这类聚合曾经被静默重定向成 count
# ——见 _measure_operand。
NUMERIC_AGGS = {"sum", "average", "min", "max"}

# WP-11 总计计算方式：per-measure 合计聚合覆盖（对标 QBI）。合计回**全量过滤
# 原始行**重算（SQL 上就是不带 GROUP BY 的 overall 聚合），不是对分组值二次聚合。
# 载荷形状：顶层 "totalCalc": {列: 聚合}（WP-11-JS 直送 editor.table.totalCalc 声明）
# 或逐指标 {"column", "aggregation", "totalCalc"}；parse_request 把两者归一进
# measures[].totalCalc。刻意不含 "none"——合计必须是一个真聚合。NULL 语义沿用
# _agg_expr 既有约定（sum 空池 0、average/min/max 空池 NULL、countd 不数 NULL）。
TOTAL_CALCS = SUPPORTED_AGG - {"none"}


class AggregationError(ValueError):
    """Raised on malformed aggregate requests. Maps to HTTP 400."""


def _quote(name: str) -> str:
    """Quote an identifier; raise on unsafe names."""
    if not isinstance(name, str) or not NON_EMPTY_IDENT.match(name):
        raise AggregationError(f"unsafe identifier: {name!r}")
    return f'"{name.replace(chr(34), chr(34) * 2)}"'


def _bucket_expr(source: str, bucket: str, engine: Optional[Engine] = None) -> tuple[str, str]:
    """Return (sql_fragment, display_label_template).

    SQLite strftime format strings:
      year    -> %Y
      month   -> %Y-%m
      quarter -> %Y-Qn      (derived: (month-1)/3+1)
      week    -> %Y-W%W (DuckDB only; SQLite uses %Y-W%W too)
      day     -> %Y-%m-%d

    ``strftime`` can only read ISO dates / timestamps / epoch numbers, so a text
    column spelled ``2026/9/5`` (or 2026.9.5 / 2026年9月5日 / 20260905) used to
    bucket to NULL — the time axis rendered EMPTY on every server-side
    re-aggregation (a real company report). When the engine exposes the
    ``ai_date`` UDF (registered in store.py from the shared scripts/date_parse.py
    parser) the value is normalized to ISO first, so the SQL bucket uses exactly
    the same parser as the compiler and the browser.
    """
    fmts = {
        "year": "%Y",
        "month": "%Y-%m",
        # %W is a CALENDAR-year Monday week (00-53), NOT ISO — engines without
        # the ai_week UDF fall back to it and disagree with the compiler/browser
        # on nearly every week of the year. With the UDF the week branch below
        # bypasses strftime entirely (see the ai_week interception).
        "week": "%Y-W%W",
        "day": "%Y-%m-%d",
    }
    # 季度是**派生**分桶（源数据没有「季」这个精度），两个方言都要拼 YYYY-Qn。
    # 标签必须与 date_parse.label / chart-adapter.bucketLabel 逐字一致（WYSIWYG）。
    if bucket == "quarter":
        fmt = "%Y-Q"      # 仅用于「无 ai_date UDF」的老引擎回退分支
    else:
        fmt = fmts.get(bucket)
        if not fmt:
            raise AggregationError(f"unsupported bucket: {bucket!r}")
    col = _quote(source)
    has_norm = bool(engine is not None and getattr(engine, "supports_date_norm", False))
    dialect = str(getattr(engine, "engine_name", "sqlite") or "sqlite").lower()
    if not has_norm:
        date_col = col
    elif dialect.startswith("duck"):
        # duckdb's strftime wants a DATE/TIMESTAMP, not a string: try_cast keeps
        # epoch integers working and turns junk into NULL instead of raising.
        # The UDF is declared VARCHAR→VARCHAR (duckdb needs concrete types), so
        # the argument is cast to text first.
        date_col = (f"CAST(coalesce(try_cast(ai_date(CAST({col} AS VARCHAR)) AS TIMESTAMP), "
                    f"try_cast({col} AS TIMESTAMP)) AS TIMESTAMP)")
    else:
        date_col = f"ai_date({col})"
    if bucket == "quarter":
        # 参数顺序两种方言相反：SQLite 是 strftime(format, value)，duckdb 是
        # strftime(value, format)。季度 = 年份 + ceil(month/3)。
        if dialect.startswith("duck"):
            # duckdb 的 ``/`` 是真除法（3.67 季），要用 ``//`` 取整。
            return (f"strftime({date_col}, '%Y') || '-Q' || "
                    f"CAST((CAST(strftime({date_col}, '%m') AS INTEGER) + 2) // 3 AS VARCHAR)"), "%Y-Q"
        # printf() 把 NULL 印成空串（不是 NULL），而 ai_date() 对非日期**原样透传**
        # （设计如此），所以判空要看 strftime 的结果，否则脏值会分组出一个假标签
        # '-Q0'（编译侧/浏览器那边是 None）。
        return (f"CASE WHEN strftime('%Y', {date_col}) IS NULL THEN NULL ELSE "
                f"printf('%s-Q%d', strftime('%Y', {date_col}), "
                f"(CAST(strftime('%m', {date_col}) AS INTEGER) + 2) / 3) END"), "%Y-Q"
    if bucket == "week" and engine is not None and getattr(engine, "supports_week_norm", False):
        # ISO 周标签必须来自共享解析器（ai_week → scripts/date_parse.label），
        # 逐字对齐编译侧与浏览器 bucketLabel（WYSIWYG）。ai_week 直吃原始值
        # （YYYYMM/斜杠日期/epoch 都由 date_parse 处理），不需要下面的 YYYYMM
        # CASE 包装；非日期值 → NULL 分组，与 strftime 分支的 NULL 行为一致。
        if dialect.startswith("duck"):
            return f"ai_week(CAST({col} AS VARCHAR))", "%Y-W%W"
        return f"ai_week({col})", "%Y-W%W"
    if has_norm and dialect.startswith("duck"):
        # ai_date already normalizes every spelling — including YYYYMM
        # (202501 → 2025-01-01) — so the numeric YYYYMM CASE below is not needed
        # and would raise on a text date (duckdb CAST is strict, not permissive).
        return f"strftime({date_col}, '{fmt}')", fmt
    # A YYYYMM column (e.g. YearMonth = 202501.0) is stored as a REAL float. Passing
    # it to strftime treats it as a Unix timestamp and yields a wrong era
    # (-4158-04). Detect the 6-digit YYYYMM form (190001..210012) and format it
    # directly so a year-month column buckets to 2025-01. Other date columns
    # (real timestamps, yyyy-mm-dd text) fall through to strftime.
    cast = "TRY_CAST" if dialect.startswith("duck") else "CAST"
    yyyymm_expr = (
        f"CASE "
        f"WHEN {cast}({col} AS INTEGER) BETWEEN 100000 AND 999999 "
        f"AND {cast}({col} AS INTEGER) % 100 BETWEEN 1 AND 12 "
        f"AND ABS({cast}({col} AS REAL) - CAST({cast}({col} AS INTEGER) AS REAL)) < 0.001 "
        f"THEN {_yyyymm_fmt(bucket, col, cast=cast)} "
        f"ELSE strftime('{fmt}', {date_col}) END"
    )
    return yyyymm_expr, fmt


def _yyyymm_fmt(bucket: str, col: str, cast: str = "CAST") -> str:
    """SQL expression formatting a 6-digit YYYYMM integer to a bucket label."""
    int_expr = f'{cast}({cast}({col} AS INTEGER) / 100 AS INTEGER)'
    month_expr = f'{cast}({cast}({col} AS INTEGER) % 100 AS INTEGER)'
    if bucket == "year":
        return f"printf('%04d', {int_expr})"
    if bucket == "month":
        return f"printf('%04d-%02d', {int_expr}, {month_expr})"
    if bucket == "day":
        return f"printf('%04d-%02d-01', {int_expr}, {month_expr})"
    if bucket == "quarter":
        return (f"printf('%04d-Q%d', {int_expr}, "
                f"({month_expr} + 2) / 3)")
    if bucket == "week":
        # YYYY-01 is the week label; week granularity on a monthly column is
        # ambiguous, so fall back to the month label (YYYY-W01 of Jan 1).
        return f"printf('%04d-W01', {int_expr})"
    raise AggregationError(f"unsupported bucket: {bucket!r}")


def _agg_expr_sql(operand: str, method: str) -> tuple[str, Optional[str]]:
    """(sql_expr, alias) for an ALREADY-QUOTED operand expression."""
    if method == "count":
        return "COUNT(*)", "count"
    if method == "countd":
        # 去重计数（对标 Quick BI BI_COUNTD，2026-09-14）：COUNT(DISTINCT col)，
        # NULL 不计数（与本地路径 Set 语义一致）。SQLite/DuckDB 原生支持。
        # 刻意用**原始列**去重（不是 ai_num）：与浏览器 Set 的原始值语义一致。
        return f"COUNT(DISTINCT {operand})", None
    if method == "sum":
        return f"COALESCE(SUM({operand}), 0)", None
    if method == "average":
        return f"AVG({operand})", None
    if method == "min":
        return f"MIN({operand})", None
    if method == "max":
        return f"MAX({operand})", None
    if method == "none":
        return operand, None
    raise AggregationError(f"unsupported aggregation: {method!r}")


def _agg_expr(column: str, method: str) -> tuple[str, Optional[str]]:
    """Return (sql_expr, result_alias). None alias means use raw value.

    `count` is special: it does not aggregate on a measure column but counts
    rows per group. Returns COUNT(*).
    """
    return _agg_expr_sql(_quote(column), method)


def _measure_operand(col: str, method: str, sql_type: str, table: str,
                     engine: Optional[Engine]) -> tuple[str, str]:
    """Aggregation operand for a measure column → (operand, effective_method).

    A single dirty cell ("N/A") makes the WHOLE column TEXT at load time
    (``_infer_sql_type``); the old redirect then turned every declared
    sum/average/min/max into count — a 335k-row revenue chart silently became
    a row counter on the first server-side re-aggregation. When the engine
    exposes ``ai_num`` and the column actually carries parseable numbers,
    aggregate over ``ai_num(col)`` instead: unparseable values → NULL →
    skipped, exactly the compile-side / browser semantics. A column with no
    parseable numbers at all keeps the honest count redirect (a category
    column asked for sum still answers with row counts, not a 0)."""
    col_q = _quote(col)
    if method not in NUMERIC_AGGS:
        # count/countd/none 的操作数恒为原始列（countd 按原始值去重）。
        return col_q, method
    if sql_type in ("INTEGER", "REAL"):
        # 数值列维持历史 CAST：REAL 列行为逐字节不变，INTEGER 列 SUM 也继续
        # 返回 REAL。
        return f"CAST({col_q} AS REAL)", method
    if engine is None or not getattr(engine, "supports_num_norm", False):
        return col_q, "count"
    probe = (f"SELECT 1 AS n FROM (SELECT ai_num({col_q}) AS v FROM {_quote(table)} "
             f"WHERE {col_q} IS NOT NULL LIMIT 200) WHERE v IS NOT NULL LIMIT 1")
    try:
        if engine.execute(probe):
            return f"ai_num({col_q})", method
    except Exception:  # pragma: no cover - probe must never fail the request
        return col_q, "count"
    return col_q, "count"


def _date_expr(col: str, engine: Any) -> str:
    """Normalized-date SQL expression for range comparisons (``ai_date`` UDF)."""
    if engine is not None and getattr(engine, "supports_date_norm", False):
        return f"ai_date({col})"
    return col


def _predicate_sql(f: dict[str, Any], available_columns: set[str] | None, engine: Any,
                   depth: int = 0) -> tuple[str, list[Any]]:
    """ONE predicate → (sql_fragment, params) — the single predicate builder.

    Extracted from ``_filter_clause`` so the 筛选型派生指标 (conditional
    aggregation, ``measure.filters``) compiles its CASE WHEN predicates through
    the exact same op semantics as the query WHERE — three-way row-set parity
    (compile ``_row_filter`` / browser ``matchFilter`` / SQL) is inherited from
    the existing filter pipeline, never re-derived. May return ``1 = 1`` for a
    value-less predicate (documented "no constraint" behavior)."""
    if not isinstance(f, dict):
        raise AggregationError("each filter predicate must be an object")
    if "any" in f:
        # 深度闸：客户端最多一层 OR 组；深嵌套小载荷不该能 RecursionError 打挂 500。
        if depth >= 2:
            raise AggregationError("filter OR-nesting supports at most 2 levels")
        nested = list(f.get("any") or [])
        # 上限 40：表内搜索把「全部已选列」的 contains 谓词并成一个 OR 组，
        # 明细表列数可能超过 10（TBL-H5）——仍是有界的，且只这一层。
        if not nested or len(nested) > 40:
            raise AggregationError("OR group must contain 1-40 predicates")
        clauses, values = [], []
        for child in nested:
            clause, child_values = _predicate_sql(child, available_columns, engine, depth + 1)
            clauses.append(clause)
            values.extend(child_values)
        return "(" + " OR ".join(clauses) + ")", values
    if not isinstance(f.get("column"), str) or not f.get("column"):
        raise AggregationError("filter.column is required")
    if available_columns is not None and f["column"] not in available_columns:
        raise AggregationError(f"filter column {f['column']!r} not found")
    col = _quote(f["column"])
    op = str(f.get("op", "=")).lower()
    # ge/le：derived_model.FILTER_OPS（筛选型派生指标的声明词汇，UI 下拉同款）
    # 接受它们，而本管道的规范词汇是 gte/lte（ALLOWED_FILTER_OPS / matchFilter 同款）
    # ——不收别名时，合法声明直接落「unsupported filter op」400（compile 侧
    # _row_filter 同一缺口，2026-10-11 第 13 轮审视实锤）。
    op = {"eq": "=", "ne": "!=", "gt": ">", "gte": ">=", "ge": ">=",
          "lt": "<", "lte": "<=", "le": "<="}.get(op, op)
    val = f.get("value")
    if op == "is null":
        return f"{col} IS NULL", []
    if op == "is not null":
        return f"{col} IS NOT NULL", []
    # 表内搜索「搜索本表」（TBL-H5）：跨列 contains —— 2026-09-25 起与本地路径
    # 统一为**不区分大小写**（两侧 toLowerCase/LOWER；此前 instr 字节级子串，
    # 搜 a431 找不到门店号 A431）。LOWER 在 SQLite/DuckDB 对 ASCII 稳定；
    # NULL 值两方言都不命中，与 JS 一致。
    if op == "contains":
        needle = "" if val is None else str(val)
        if not needle:
            return "1 = 1", []
        dialect = str(getattr(engine, "engine_name", "sqlite") or "sqlite").lower()
        text_col = f"LOWER(CAST({col} AS VARCHAR))"
        needle_l = needle.lower()
        if dialect.startswith("duck"):
            return f"contains({text_col}, ?)", [needle_l]
        return f"instr({text_col}, ?) > 0", [needle_l]
    # 值域筛选的「(空白)」勾选项（editor-runtime tableFilterPredicates 把 '__blank__'
    # 哨兵翻译成这个 op）：NULL 或 trim 后空串。与 between/daterange 的「空值不匹配」
    # 不冲突——那是区间语义；这里是读者显式勾选空值。
    if op == "blank":
        return f"({col} IS NULL OR TRIM(CAST({col} AS VARCHAR)) = '')", []
    # A predicate with no value (or an empty selection) means "no constraint"
    # — parity with the compile-side _row_filter and the JS matchFilter.
    # `in` with no value used to compile to `0 = 1` and silently emptied
    # every aggregate (real authoring run, 2026-09).
    if val is None or val == "" or (isinstance(val, (list, tuple, set)) and not val):
        if op not in ("is null", "is not null"):
            return "1 = 1", []
    if op in {"in", "not in"}:
        if isinstance(val, str):
            # 字符串会被 list() 拆成单字符逐个匹配（"abc" → 'a','b','c'）——
            # 给出错误结果还不报错。客户端恒发数组；直呼 API 传错形状就 400。
            raise AggregationError("filter op 'in'/'not in' requires a list of values")
        values = list(val or [])
        if not values:
            return "1 = 1", []
        placeholders = ", ".join("?" for _ in values)
        return f"{col} {op.upper()} ({placeholders})", values
    # 数值区间 / 日期范围（读者侧区间筛选控件）。两端可空 = 单边开口；
    # 两端都空 = 无约束（已在上面「空值不约束」处返回 1 = 1）。
    if op in {"between", "daterange"}:
        vals = list(val or [])
        if len(vals) != 2:
            raise AggregationError(f"{op} expects [low, high]")
        low, high = vals
        if low is None and high is None:
            return "1 = 1", []
        num_norm = op == "between" and engine is not None and getattr(engine, "supports_num_norm", False)
        if op == "daterange":
            ref = _date_expr(col, engine)
        elif num_norm:
            # TEXT 列混入脏值时 `col >= ?` 按亲和性乱比（'100'<'70' 字典序），
            # 与 JS Number() 强转分裂——ai_num 把「可解析→REAL，否则 NULL」
            # 收敛成同一语义（NULL 不匹配 = JS NaN 比较恒 False）。
            ref = f"ai_num({col})"
        else:
            ref = col
        # 空值守卫：Python/JS 侧 `float('')`/`Number('')` 都不成立，于是空单元格
        # 在区间筛选下**不匹配**；但 SQL 里 `'' <= '2026-01-01'` 为真（空串排序最前），
        # 不守卫就会多出「日期为空的行」——三端选出的行集必须完全一致（真机 fixture 抓到）。
        # ai_num 路径 NULL 已涵盖空串/脏值，无需 TRIM 闸。
        if num_norm:
            parts: list[str] = [f"({ref} IS NOT NULL)"]
        elif op == "daterange" and engine is not None and getattr(engine, "supports_date_check", False):
            # ai_date 对非日期**原样透传**（epoch/时间戳要留给 strftime），所以
            # 「IS NOT NULL + TRIM」守卫拦不住 'N/A' 这类脏文本——它与 ISO 边界
            # 的比较是字典序（字母恒大于数字开头的日期串），服务端会放行编译侧
            # （date_parse.iso → None → 排除）与浏览器（dateParts → null → 排除）
            # 都筛掉的行，三端行集分裂。ai_is_date 是同一个解析器的 0/1 判定。
            parts = [f"(ai_is_date({col}) = 1)"]
        else:
            parts = [f"({ref} IS NOT NULL AND TRIM(CAST({ref} AS VARCHAR)) <> '')"]
        params: list[Any] = []
        # 端点与值侧同走一份解析器归一（镜像编译侧 _row_filter._iso_edge）：
        # 裸截断 str("2026-07")[:10]="2026-07" 撞上 ai_date("202607")="2026-07-01"
        # 的日精度值，字典序 "2026-07-01" <= "2026-07" 为假——上界把整个末月
        # 踢掉（2026-09-18 真机：daterange 到 2026-07 少了 7 月桶）。
        def _iso_edge_date(x: Any) -> str:
            if x is None or str(x).strip() == "":
                return ""
            parsed = _date_parse.iso(x)
            return (parsed or str(x))[:10]

        try:
            if op == "daterange":
                lo_p = None if low is None else _iso_edge_date(low)
                hi_p = None if high is None else _iso_edge_date(high)
            else:
                lo_p = None if low is None else float(low)
                hi_p = None if high is None else float(high)
        except (TypeError, ValueError):
            # 阈值本身非数字：JS 侧 Number(阈值)=NaN → 比较恒 False → 零行。
            return ("1 = 0", []) if num_norm else ("1 = 0", [])
        if lo_p is not None:
            parts.append(f"{ref} >= ?")
            params.append(lo_p)
        if hi_p is not None:
            parts.append(f"{ref} <= ?")
            params.append(hi_p)
        return "(" + " AND ".join(parts) + ")", params
    if op == "like":
        # 休眠 op（前端从不发送）：保留但按字面匹配收口——值里的 %/_ 用 ESCAPE
        # 关掉通配语义，否则与浏览器本地 matchFilter 的行集分裂。
        _esc = str(val).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return f"{col} LIKE ? ESCAPE '\\'", [_esc]
    if op in (">", ">=", "<", "<="):
        # JS matchFilter 对这四个 op 恒用 Number() 强转：值或阈值解析失败 →
        # NaN → 该行/全部行不命中。ai_num 同语义；阈值非数字 → 一行不命中。
        if engine is not None and getattr(engine, "supports_num_norm", False):
            try:
                num = float(val)
            except (TypeError, ValueError):
                return "1 = 0", []
            ref = f"ai_num({col})"
            return f"({ref} IS NOT NULL AND {ref} {op} ?)", [num]
        return f"{col} {op} ?", [val]
    if op in ("=", "!="):
        return f"{col} {op} ?", [val]
    raise AggregationError(f"unsupported filter op: {op!r}")


def _filter_clause(filters: list[dict[str, Any]] | dict[str, Any], available_columns: set[str] | None = None,
                   engine: Any = None) -> tuple[str, list[Any]]:
    """Build a bounded AND + one-level OR filter clause.

    ``engine`` is optional and only used by the ``daterange`` op: the comparison
    then happens on the SAME normalized date expression the bucketing uses
    (``ai_date(col)``, i.e. ``scripts/date_parse.py``), so `2026/9/5`,
    `2026年9月5日`, `20260905` and epoch integers all land in the right range —
    exactly like the compile-side ``_row_filter`` and the browser ``matchFilter``.
    """
    if not filters:
        return "", []
    if isinstance(filters, dict):
        all_filters = list(filters.get("all") or [])
        any_filters = list(filters.get("any") or [])
        if len(any_filters) > 10 or len(all_filters) > 10 or len(all_filters) + len(any_filters) > 10:
            raise AggregationError("filter supports at most 10 predicates")
        groups: list[dict[str, Any]] = all_filters
        if any_filters:
            groups.append({"any": any_filters})
    else:
        groups = list(filters)
    if len(groups) > 10:
        raise AggregationError("filter supports at most 10 predicates")
    parts: list[str] = []
    params: list[Any] = []
    for f in groups:
        clause, values = _predicate_sql(f, available_columns, engine)
        parts.append(clause)
        params.extend(values)
    return "WHERE " + " AND ".join(parts), params


def _predicates_sql(filters_list: list[Any], available_columns: set[str] | None,
                    engine: Any) -> tuple[str, list[Any]]:
    """AND-list of predicates for a measure-level filter (筛选型派生指标)."""
    parts: list[str] = []
    params: list[Any] = []
    for f in filters_list or []:
        clause, values = _predicate_sql(f, available_columns, engine)
        parts.append(clause)
        params.extend(values)
    return " AND ".join(parts) if parts else "1 = 1", params


def _cond_agg_sql(operand: str, method: str, pred_sql: str) -> str:
    """Conditional aggregation AGG(CASE WHEN pred THEN operand END) — 筛选型派生指标的
    SQL 形态（docs/filtered-measures-and-week-fields-2026-10-09.md D5/D8）。

    空池语义三端逐位一致：count/countd → 0（COUNT 家族自然语义）；
    sum/average/min/max → NULL（空缺不造 0）。所以 sum 分支**不带** COALESCE——
    与无条件聚合的 ``COALESCE(SUM(...),0)`` 是刻意不同的两条。"""
    if method == "count":
        return f"COUNT(CASE WHEN {pred_sql} THEN 1 END)"
    if method == "countd":
        return f"COUNT(DISTINCT CASE WHEN {pred_sql} THEN {operand} END)"
    if method == "sum":
        return f"SUM(CASE WHEN {pred_sql} THEN {operand} END)"
    if method == "average":
        return f"AVG(CASE WHEN {pred_sql} THEN {operand} END)"
    if method == "min":
        return f"MIN(CASE WHEN {pred_sql} THEN {operand} END)"
    if method == "max":
        return f"MAX(CASE WHEN {pred_sql} THEN {operand} END)"
    if method == "none":
        return operand
    raise AggregationError(f"unsupported aggregation: {method!r}")


def _order_clause(orders: list[dict[str, str]], fallback_col: Optional[str]) -> tuple[str, list[Any]]:
    if not orders:
        if fallback_col:
            return f"ORDER BY {_quote(fallback_col)} DESC", []
        return "", []
    parts: list[str] = []
    for o in orders:
        col = _quote(o["column"])
        direction = "DESC" if o.get("direction", "desc").lower() == "desc" else "ASC"
        parts.append(f"{col} {direction}")
    return "ORDER BY " + ", ".join(parts), []


def aggregate_multi(engine: Engine, requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Batch several single-table aggregation sub-requests in ONE HTTP round-trip
    (P6). Each sub-request is ``{datasetId, groupBy, measures, filter, bucket,
    orderBy, limit, offset}``; the cross-table caller merges the aligned rows
    client-side — the alignment stays a logical client operation (see
    references/multi-source-alignment.md §6.2)."""
    out: list[dict[str, Any]] = []
    for sub in requests:
        dsid = sub.get("datasetId")
        if not isinstance(dsid, str) or not dsid:
            raise AggregationError("aggregate-multi sub-request requires datasetId")
        agg_req = parse_request(sub)
        resp = aggregate(engine, dsid, agg_req)
        out.append(resp.to_dict())
    return out


def aggregate(engine: Engine, table_name: str, request: AggregateRequest) -> AggregateResponse:
    """Run an aggregation request against the engine.

    Aggregates map: the response includes sum/mean/min/max/count for each
    measure column over the WHOLE filtered set (not per-group), to enable
    truth validation and consistent meta display.
    """
    start = time.monotonic()
    info = engine.table_info(table_name)
    if info is None:
        raise AggregationError(f"table {table_name!r} not loaded")

    available_cols = set(info.columns)
    group_by = list(request.group_by)
    if len(group_by) > 6:
        raise AggregationError("groupBy may contain at most 6 dimensions")
    # Cap is about response size, not about how many metrics a card may show. A
    # 聚合表 (Excel-like table) legitimately carries 广告系列 × 费用/点击/转化… — the
    # old cap of 3 made the server reject it with a 400 and the card rendered
    # EMPTY with no visible reason (a real company report). 12 covers any sane
    # table card; derived measures are deduped to their base column client-side
    # anyway (chart-adapter.fetchAggregate).
    if len(request.measures) > 12:
        raise AggregationError("measures may contain at most 12 columns")

    # resolve group_by columns (allow bucket mapping)
    bucket_label: Optional[str] = None
    bucket_dim: Optional[str] = None
    if request.bucket:
        source = request.bucket.get("source")
        bucketed = request.bucket.get("bucketed") or source
        bucket = request.bucket.get("bucket", "month")
        if source and source not in available_cols:
            raise AggregationError(f"bucket source column {source!r} not found")
        bucket_label = bucketed
        bucket_dim = bucketed

    for col in group_by:
        # skip availability for the bucket alias — it'll be replaced by the bucketed SQL
        if bucket_dim and col == bucket_dim:
            continue
        if col not in available_cols:
            raise AggregationError(f"groupBy column {col!r} not found in table")

    # measures
    if not request.measures:
        raise AggregationError("at least one measure is required")
    for m in request.measures:
        col = m.get("column")
        agg = m.get("aggregation", "sum")
        if not col:
            raise AggregationError("measure.column required")
        if col == "*":
            # 合成行数列只认 count（_agg_expr 的 COUNT(*) 分支）；其他聚合会拼出
            # SUM(CAST("*" AS REAL)) 这类 no such column SQL，把本该 400 的请求
            # 变成 500（P3 收口：入口校验即拒绝）。
            if agg != "count":
                raise AggregationError(f'measure column "*" only supports aggregation "count"; got {agg!r}')
        elif col not in available_cols:
            raise AggregationError(f"measure column {col!r} not found")
        if agg not in SUPPORTED_AGG:
            raise AggregationError(f"unsupported aggregation: {agg!r}")
    for order in request.order_by:
        if order.get("column") not in available_cols:
            raise AggregationError(f"orderBy column {order.get('column')!r} not found in table")

    # build SELECT
    select_parts: list[str] = []
    result_cols: list[str] = []
    select_aliases: list[str] = []  # for SQL output column naming

    # group-by dimensions (with possible bucket substitution)
    for dim in group_by:
        if bucket_dim and dim == bucket_dim and request.bucket:
            expr, _ = _bucket_expr(request.bucket["source"], request.bucket.get("bucket", "month"), engine)
            select_parts.append(f"{expr} AS {_quote(dim)}")
        else:
            select_parts.append(_quote(dim))
        result_cols.append(dim)
    # Final column list for response

    # measures
    measure_aggs: list[tuple[str, str, str, str, str, bool]] = []  # (display_col, agg_method, sql_expr, sql_alias, operand, has_filters)
    # SELECT 级参数（筛选型派生指标的 CASE WHEN 谓词占位符）：SQL 里 SELECT 先于
    # WHERE，执行参数必须 select_params + where_params 同序拼接。
    select_params: list[Any] = []
    # Category-as-measure: numeric aggs (sum/avg/min/max) on a non-numeric
    # column collapse to 0 under CAST(... AS REAL). Serve count instead so the
    # honest row-count is returned. Numeric-ness is derived from the loaded
    # schema (sql_type TEXT => non-numeric; INTEGER/REAL => numeric).
    schema_type_by_col = {cs.name: cs.sql_type for cs in (info.schema or []) if cs and cs.name}
    # 派生指标的 helper 聚合会请求 (列,聚合) 重复的 measure（如 base 列已显示、
    # 公式分母又请求一次 sum(营业收入)）。SQL 结果按别名成 dict，重复别名会把两列
    # 折叠成一列（dict(zip(cols,row))），客户端按**请求位置**对齐取数 → 取到越界
    # null（真机：矩阵派生毛利率整列 —）。因此 measure 恒用唯一位置别名 _m{i}，
    # 响应 columns 仍按请求名（含重复）回带，行值按位置与请求一一对应。
    for _mi, m in enumerate(request.measures):
        col = m.get("column")
        agg = m.get("aggregation", "sum")
        mf = m.get("filters")
        # TEXT 列上的数值聚合：能解析出数字 → ai_num(col) 真聚合；全列无数字
        # → 保持诚实的 count 重定向（类目列求 sum 答「行数」而不是 0）。
        operand, agg = _measure_operand(col, agg, schema_type_by_col.get(col, "REAL"),
                                        table_name, engine)
        if mf:
            # 筛选型派生指标的依赖序列（docs/filtered-measures-and-week-fields-
            # 2026-10-09.md D6/D7）：谓词与 WHERE 同一构建器（_predicate_sql），
            # 条件聚合走 _cond_agg_sql（空池 count/countd=0、其余 NULL）。
            if not isinstance(mf, list) or not mf or len(mf) > 10:
                raise AggregationError("measure.filters must be a non-empty list of at most 10 predicates")
            pred_sql, pred_params = _predicates_sql(mf, available_cols, engine)
            expr = _cond_agg_sql(operand, agg, pred_sql)
            select_params.extend(pred_params)
        else:
            expr, _ = _agg_expr_sql(operand, agg)
        alias = f"_m{_mi}"
        select_parts.append(f"{expr} AS {alias}")
        result_cols.append(col)
        measure_aggs.append((col, agg, expr, alias, operand, bool(mf)))
    # 重复（列,聚合）请求时取**首见**别名：调用方按请求位置取数（上方注释），
    # 首见 = 该（列,聚合）组合第一次被请求的位置；dict 推导后写覆盖前写会让它
    # 漂到重复请求的最后一次位置（P3 收口：first-wins 与位置对齐语义一致）。
    alias_of_measure: dict[str, str] = {}
    for col, _agg, _expr, alias, _operand, _mf in measure_aggs:
        alias_of_measure.setdefault(col, alias)
    # WP-11 总计计算方式：聚合覆盖按指标列归一（防直接构造 AggregateRequest 绕过
    # parse_request 的校验）。合成行数列 "*" 的覆盖被忽略（COUNT(*) 无第二种合计）。
    total_calc_by_col: dict[str, str] = {}
    for m in request.measures:
        col = m.get("column")
        tc = m.get("totalCalc")
        if tc is None or not col or col == "*":
            continue
        if tc not in TOTAL_CALCS:
            raise AggregationError(f"totalCalc[{col!r}] must be one of {sorted(TOTAL_CALCS)}; got {tc!r}")
        total_calc_by_col[col] = tc

    # build FROM + WHERE
    from_sql = f'FROM {_quote(table_name)}'
    where_sql, where_params = _filter_clause(request.filter, available_cols, engine)

    # GROUP BY clause
    group_sql = ""
    if group_by:
        group_cols = []
        for dim in group_by:
            if bucket_dim and dim == bucket_dim and request.bucket:
                expr, _ = _bucket_expr(request.bucket["source"], request.bucket.get("bucket", "month"), engine)
                group_cols.append(expr)
            else:
                group_cols.append(_quote(dim))
        group_sql = "GROUP BY " + ", ".join(group_cols)

    # ORDER BY
    fallback_col = None
    if not request.order_by:
        # default: first measure, desc
        if measure_aggs:
            fallback_col = measure_aggs[0][3]
    # 显式 orderBy 命中 measure 列时排序走其位置别名（SELECT 已不再按列名出列）；
    # 维度列（含分桶别名）不变。
    mapped_orders = [
        {**o, "column": alias_of_measure.get(o.get("column"), o.get("column"))}
        for o in request.order_by
    ]
    order_sql, _ = _order_clause(mapped_orders, fallback_col)

    # LIMIT/OFFSET are always bounded server-side to protect the browser.
    requested_limit = 500 if request.limit is None else int(request.limit)
    if requested_limit < 1 or requested_limit > 20000:
        raise AggregationError("limit must be between 1 and 20000")
    offset = max(0, int(request.offset or 0))
    limit_sql = f"LIMIT {requested_limit} OFFSET {offset}"

    sql = f"SELECT {', '.join(select_parts)} {from_sql} {where_sql} {group_sql} {order_sql} {limit_sql}"
    rows = engine.execute(sql, tuple(select_params) + tuple(where_params))

    truncated = len(rows) >= requested_limit
    truncated_reason = "result limited; use offset pagination or a smaller Top-N" if truncated else None

    # Pre-LIMIT group total — ONLY when the result was actually cut. The client
    # needs it to tell the reader「已显示前 12 项（共 40 项）」: a silent Top-N
    # makes the reader believe 12 is everything (report distortion, not layout).
    # Cost is bounded to the truncated case (one COUNT over a subquery), and the
    # post-LIMIT ``group_count`` keeps its existing meaning for other consumers.
    group_total = len(rows)
    if truncated and group_sql:
        try:
            count_sql = (f"SELECT COUNT(*) AS n FROM (SELECT 1 {from_sql} {where_sql} "
                         f"{group_sql}) AS _grp")
            group_total = int(engine.execute(count_sql, tuple(where_params))[0]["n"] or 0)
        except Exception:  # never fail the response for a cosmetic count
            group_total = len(rows)

    # Convert rows to JSON-friendly (datetime -> isoformat, etc.)
    cleaned: list[list[Any]] = []
    for row in rows:
        cleaned.append([_jsonable(v) for v in row.values()])

    # ---- BI-H3：TOP-K「其他」合并 -------------------------------------------
    # LIMIT 截断且作者开了 topKOther 时，把尾部分组聚合成「其他（N 项）」补在最后
    # （Power BI 语义；Flint filter-overflow 只截断+告警不合并，合并是本报告的显式
    # 开关）。仅可加聚合（sum/count）参与——含 average/min/max 时回退纯截断
    # （尾部平均 ≠ 全量−头部，合并会给假数）。「其他」行只在这个最后阶段生成，
    # 不参与筛选/排序/派生（那些都已在上面的主查询里完成）。
    other_merged: Optional[dict[str, Any]] = None
    # BI-H3 验收修订（2026-09-13）：合并从「全有或全无」改为**按指标逐列**——
    # 可加列（sum/count）求和进「其他」行；不可加列（average/min/max）保持 NULL
    # （空缺比假数好）。多指标图表只要有一列可加就能合并（旧 gate 是 all()，一列
    # 不可加整个开关就静默失效）。
    # 客户端 addMask 否决（2026-09-17 挂账推进）：占比/比率型**列**（percent/unitless
    # 类型或率/占比名义，编译侧 _col_non_additive 同一门）即使聚合方式是 sum 也不
    # 可加——服务端没有 measureTypes，尾行求和占比是假数（7.3%+6.1%→13.4）。
    _client_add = request.add_mask if isinstance(request.add_mask, list) else None

    def _client_ok(_i: int) -> bool:
        if _client_add is None or _i >= len(_client_add) or _client_add[_i] is None:
            return True
        return bool(_client_add[_i])

    add_mask = [agg in ("sum", "count") and _client_ok(_i)
                for _i, (_col, agg, _expr, _alias, _operand, _mfx) in enumerate(measure_aggs)]
    if (request.top_k_other and truncated and group_sql and offset == 0
            and group_by and group_total > len(cleaned) and any(add_mask)):
        dim0 = group_by[0]
        if bucket_dim and dim0 == bucket_dim and request.bucket:
            dim0_expr, _ = _bucket_expr(request.bucket["source"], request.bucket.get("bucket", "month"), engine)
        else:
            dim0_expr = _quote(dim0)
        # 尾行谓词：排除「已显示」的维度值。NULL 维度行必须显式处理——
        # `NULL NOT IN (...)` 恒为 NULL（假），NULL 行会被静默排除在「其他」
        # 合计之外（显示值+其他 ≠ 总计，读者勾稽对不上）。NULL 已跻身前 K 时
        # 才把 NULL 行也排除；其余情况归入尾行。参数表只放真实字符串标签，
        # 绝不把 str(None)='None' 当筛选值。
        shown_labels = [r[0] for r in cleaned]
        str_labels = [str(v) for v in shown_labels if v is not None]
        # str_labels 为空（前 K 全是 NULL 维度行，或零显示行）时不得发 `NOT IN ()`
        # ——SQLite 语法错，except 兜底会静默丢掉「其他」行（显示值+其他 ≠ 总计）。
        _null_parts = []
        if str_labels:
            _null_parts.append(f"{dim0_expr} NOT IN ({', '.join('?' for _ in str_labels)})")
        if any(v is None for v in shown_labels):
            _null_parts.append(f"{dim0_expr} IS NOT NULL")
        not_in = "(" + " AND ".join(_null_parts) + ")" if _null_parts else "(1=1)"
        tail_where = (where_sql + f" AND {not_in}") if where_sql else ("WHERE " + not_in)
        agg_exprs = [
            (expr if add_mask[_ti] else "NULL") + f" AS _t{_ti}"
            for _ti, (_col, _agg, expr, _alias, _operand, _mfx) in enumerate(measure_aggs)
        ]
        bd = group_by[1] if len(group_by) > 1 else None
        try:
            if bd is None:
                omitted = group_total - len(cleaned)
                tail_sql = f"SELECT {', '.join(agg_exprs)} {from_sql} {tail_where}"
                tail = engine.execute(tail_sql, tuple(select_params) + tuple(where_params) + tuple(str_labels))
                trow = tail[0] if tail else None
                # 位置别名 _t{i}：行值按请求 measure 顺序取（同名 (列,聚合) 重复请求
                # 也各占一列，与主查询对齐契约一致）。
                tvals = list(trow.values()) if trow is not None else [0] * len(measure_aggs)
                cleaned.append([f"其他（{omitted} 项）"] + [
                    _jsonable(v) for v in tvals])
                other_merged = {"omitted": omitted}
            else:
                # 矩阵（dim × breakdown）：尾部按 breakdown 各补一行；「N 项」=
                # 被省略的**主维度**标签数（group_total 是 dim×bd 组数，需另数主维度）。
                shown_dims = {str(r[0]) for r in cleaned}
                dim_total = int(engine.execute(
                    f"SELECT COUNT(*) AS n FROM (SELECT 1 {from_sql} {where_sql} GROUP BY {dim0_expr}) AS _d",
                    tuple(where_params))[0]["n"] or 0)
                omitted = max(0, dim_total - len(shown_dims))
                if bd == bucket_dim and request.bucket:
                    bd_expr, _ = _bucket_expr(request.bucket["source"], request.bucket.get("bucket", "month"), engine)
                else:
                    bd_expr = _quote(bd)
                tail_sql = (f"SELECT {bd_expr} AS {_quote(bd)}, {', '.join(agg_exprs)} "
                            f"{from_sql} {tail_where} GROUP BY {bd_expr}")
                for trow in engine.execute(tail_sql, tuple(select_params) + tuple(where_params) + tuple(str_labels)):
                    tvals = list(trow.values())
                    # 第一列 = bd 值，其后按 _t{i} 顺序 = measure_aggs 顺序
                    cleaned.append([f"其他（{omitted} 项）", _jsonable(tvals[0])] + [
                        _jsonable(v) for v in tvals[1:]])
                other_merged = {"omitted": omitted}
        except Exception:
            other_merged = None  # 合并失败回退纯截断，绝不让响应 500

    # ---- overall aggregates (over the un-grouped filtered set) ---------------
    # 性能批（2026-09-25）：旧实现对每个 measure 单独发 1 条聚合查询（非 count
    # 再发 1 条 COUNT、有 totalCalc 再发 1 条）——12 指标卡 = 25 条全表扫描，
    # 实测占整个重聚合的 ~30%（90ms/287ms）。全部合并成**一条** SELECT（位置
    # 别名 _o{i} 防同表达式折叠），响应形状与 NULL 语义逐位不变。
    overall: dict[str, dict[str, float]] = {}

    def _overall_sql(operand: str, method: str) -> str:
        # 与 _agg_expr_sql 同一张算子表（overall 不需要别名）。
        expr, _alias = _agg_expr_sql(operand, method)
        return expr

    # 计划：(col, key, operand, null_keeps_none)。null_keeps_none=False 的键
    # （主聚合）沿用旧「NULL → 0.0」兜底；totalCalc 键保留 NULL（与编译侧
    # _static_total_row 空池 NULL 语义逐位对齐，parity 测试锁两位一致）。
    plan: list[tuple[str, str, str, bool]] = []
    need_count = False
    for col, agg, _expr, _alias, operand, has_filters in measure_aggs:
        if agg == "none":
            continue
        # 条件聚合（筛选型派生指标）不进 overall：overall 按列归一，同列多个筛选集
        # 会互相覆盖；这些是派生依赖序列，客户端也不会读它们的 overall。
        if has_filters:
            continue
        plan.append((col, agg, operand, False))
        if agg != "count":
            need_count = True
        tc = total_calc_by_col.get(col)
        if tc:
            plan.append((col, tc, operand, True))
    if plan:
        select_exprs = [_overall_sql(operand, key) for _col, key, operand, _keep in plan]
        if need_count:
            select_exprs.append("COUNT(*)")
        exprs_sql = ", ".join(f"{e} AS _o{i}" for i, e in enumerate(select_exprs))
        try:
            res_row = engine.execute(f"SELECT {exprs_sql} {from_sql} {where_sql}", tuple(where_params))
            vals = list(res_row[0].values()) if res_row else [None] * len(select_exprs)
        except Exception:
            # 求值失败按旧行为兜底：主聚合 0.0、totalCalc None、count 0（绝不 500）。
            vals = [None] * len(select_exprs)
        for i, (col, key, _operand, keep_none) in enumerate(plan):
            v = vals[i] if i < len(vals) else None
            overall.setdefault(col, {})[key] = float(v) if v is not None else (None if keep_none else 0.0)
        if need_count:
            cnt = vals[-1] if vals else None
            cnt_f = float(cnt) if cnt is not None else 0.0
            for col, _agg, _expr, _alias, _operand, _mfx in measure_aggs:
                if _agg != "none" and _agg != "count":
                    overall.setdefault(col, {})["count"] = cnt_f

    elapsed_ms = int((time.monotonic() - start) * 1000)
    return AggregateResponse(
        columns=result_cols,
        rows=cleaned,
        aggregates=overall,
        truncated=truncated,
        truncated_reason=truncated_reason,
        group_count=len(cleaned) + offset,
        group_total=group_total,
        other_merged=other_merged,
        elapsed_ms=elapsed_ms,
    )


def _jsonable(value: Any) -> Any:
    """Make a value JSON-serializable."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


# ---------------------- request parsing -----------------------------------------

def parse_request(payload: dict[str, Any]) -> AggregateRequest:
    """Parse a JSON body into an AggregateRequest."""
    group_by = list(payload.get("groupBy") or [])
    # 重复维度去重（保序）：SELECT 两个同名列会让「响应列数 ≠ 行值数」，
    # dict(zip) 折叠后按位置取值的契约自破（客户端不发这种请求，但直呼 API
    # 会拿到错位响应——在解析口收口）。
    _seen_dims: set[str] = set()
    group_by = [d for d in group_by
                if not (d in _seen_dims or _seen_dims.add(d))]
    measures_raw = list(payload.get("measures") or [])
    # 字符串 measure 形状（[{column}] 的简写）由下方 measures 循环的
    # isinstance(m, str) 分支处理——曾有一段「measures 为空时改写 legacy 形状」
    # 的前置分支，但 measures 非空时它根本不会进入、为空时 legacy 又必为空：
    # 永不触发的死分支（第八轮审视删除）。

    measures = []
    aggregation_map = payload.get("aggregations") if isinstance(payload.get("aggregations"), dict) else {}
    # WP-11 总计计算方式：顶层 totalCalc 映射（{列: 聚合}，editor.table.totalCalc
    # 的直送形状）在此只做形状/枚举校验；落载到逐指标 item["totalCalc"] 在下面的
    # measures 循环里（列成员资格由 aggregate() 对已校验的 measure 列自然保证）。
    total_calc = payload.get("totalCalc")
    if total_calc is None:
        total_calc = {}
    if not isinstance(total_calc, dict):
        raise AggregationError("totalCalc must be an object {column: aggregation}")
    for tc_col, tc_agg in total_calc.items():
        if not isinstance(tc_col, str) or not tc_col:
            raise AggregationError(f"totalCalc columns must be non-empty strings; got {tc_col!r}")
        if tc_agg not in TOTAL_CALCS:
            raise AggregationError(f"totalCalc[{tc_col!r}] must be one of {sorted(TOTAL_CALCS)}; got {tc_agg!r}")
    for m in measures_raw:
        if isinstance(m, str):
            measures.append({"column": m, "aggregation": aggregation_map.get(m, payload.get("aggregation", "sum"))})
        else:
            item = dict(m)
            item.setdefault("aggregation", aggregation_map.get(item.get("column"), payload.get("aggregation", "sum")))
            # 顶层映射优先，逐指标 "totalCalc" 键兜底（两种形状归一为一个槽位）。
            tc = total_calc.get(item.get("column"), item.get("totalCalc"))
            if tc is not None:
                if tc not in TOTAL_CALCS:
                    raise AggregationError(
                        f"totalCalc[{item.get('column')!r}] must be one of {sorted(TOTAL_CALCS)}; got {tc!r}")
                item["totalCalc"] = tc
            measures.append(item)

    bucket = payload.get("bucket") or payload.get("dimensionBucket")
    if isinstance(bucket, dict):
        bucket = dict(bucket)

    filters_raw = payload.get("filter") or []
    filters = filters_raw if isinstance(filters_raw, dict) else list(filters_raw)
    order_by = list(payload.get("orderBy") or [])
    limit = payload.get("limit")
    if limit is not None:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            raise AggregationError(f"invalid limit: {limit!r}")
    offset = payload.get("offset", 0)
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        raise AggregationError(f"invalid offset: {offset!r}")
    top_k_other = payload.get("topKOther") is True
    # 客户端 addMask（「其他」行合并的语义层否决）：只认 bool/None 的平行数组，
    # 形状不对整体忽略（老客户端/直呼 API 零影响，聚合方式判断照旧生效）。
    add_mask = payload.get("addMask")
    if add_mask is not None:
        if not isinstance(add_mask, list) or any(v is not None and not isinstance(v, bool) for v in add_mask):
            add_mask = None

    return AggregateRequest(
        group_by=group_by,
        measures=measures,
        bucket=bucket,
        filter=filters,
        order_by=order_by,
        limit=limit,
        offset=offset,
        top_k_other=top_k_other,
        add_mask=add_mask,
    )
