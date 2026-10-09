"""
二维表数据校验器
- 严格校验输入是否为合法二维表
- 推断列类型（category / measure / time）
- 失败时抛出明确的错误信息
"""

from __future__ import annotations

import re
from typing import Any

# 时间格式模式
TIME_PATTERNS = [
    re.compile(r"^\d{4}-\d{1,2}$"),  # 2024-01
    re.compile(r"^\d{4}/\d{1,2}$"),
    re.compile(r"^\d{4}-\d{1,2}-\d{1,2}$"),  # 2024-01-15
    re.compile(r"^\d{4}/\d{1,2}/\d{1,2}$"),
    re.compile(r"^\d{4}Q[1-4]$"),  # 2024Q1
    re.compile(r"^\d{4}年\d{1,2}月$"),
    re.compile(r"^\d{4}年\d{1,2}月\d{1,2}日$"),
]

VALID_TYPES = {"category", "measure", "time", "id"}

# Name hints that identify an entity/identifier key column (order id, sku,
# account, 编号, 单号, etc.). Such columns are keys, not analytical dimensions:
# they must NOT be counted toward singlefile cardinality and should not appear
# as filter chips or chart dimensions by default.
_ID_NAME_HINTS = ("id", "accout", "account", "sku", "code", "no.", "单号", "编号", "ID", "批次", "卡号", "手机", "号")


def _looks_like_id(name: str, values: list[str]) -> bool:
    """A column is an entity/id key when it is (a) named like a key or (b) very
    high cardinality (mostly unique) for a non-numeric, non-time column. This
    lets the skill auto-classify NOID/W3 accout/订单ID/SKU/单号 etc. so the model
    does not have to hand-mark every key column, and so singlefile cardinality
    is not blown up by unique ids masquerading as dimensions."""
    if any(h in name for h in _ID_NAME_HINTS):
        return True
    non_blank = [v for v in values if not _is_blank(v)]
    if len(non_blank) < 20:
        return False
    unique = len(set(str(v) for v in non_blank))
    return unique / len(non_blank) >= 0.9


# 编码列翻转门（2026-09-21）：measure 推断列的非数值细胞一旦「结构显著」，整列
# 翻转为 id 保留原值，而不是洗空。真机案例：Shop number 94.8% 纯数字被判
# measure，15,191 个字母门店号（A431/B06/M018，268 家真实门店）被静默置空 →
# countd(门店数) 系统性偏小且无人知晓。门 conditions 缺一不可：
# - ≥ _ID_FLIP_MIN_ROWS 行且占比 ≥ _ID_FLIP_MIN_RATIO（2%）——几行脏值不翻转，
#   合法指标列的零星 "N/A" 仍走 measure+置空老路；
# - 过半非数值呈「字母+数字」编码形（_code_like）——纯脏值标记（N/A、-、未知）
#   是数据质量问题，不是编码列。
# 翻转后：原值全保留（countd 可用），sum/avg 不再有意义（profile hint 说明）。
_ID_FLIP_MIN_ROWS = 20
_ID_FLIP_MIN_RATIO = 0.02


def _code_like(v: Any) -> bool:
    s = str(v)
    return bool(re.search(r"[A-Za-z]", s) and re.search(r"\d", s))


class TableValidationError(ValueError):
    """二维表数据格式错误"""


def _is_blank(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, str) and v.strip() == "":
        return True
    return False


def _to_number(v: Any) -> float | None:
    """尝试转成数字，失败返回 None"""
    if _is_blank(v):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.replace(",", "").replace(" ", "").replace("¥", "").replace("$", "").replace("%", "")
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _looks_like_time(s: str) -> bool:
    return any(p.match(s) for p in TIME_PATTERNS)


# Calendar-period ordinal columns: a numeric column named exactly like a period
# word (周/星期/week/quarter…) whose values are small period integers is an
# ordinal, NOT a summable measure. e.g. 周 = 30..36 (week-of-year) must not be
# summed; the real metric of such record data is row count. Kept strict (exact
# name) so 周销量/weekly_sales (a real measure) is never re-classified.
_PERIOD_ORDINALS = {
    "周": (1, 53), "周数": (1, 53), "周次": (1, 53), "星期": (1, 53),
    "week": (1, 53), "week_no": (1, 53), "weeknum": (1, 53), "wk": (1, 53),
    "季度": (1, 4), "quarter": (1, 4),
}


def _looks_like_period_ordinal(name: str, values: list[Any]) -> bool:
    """True when a numeric column is a week/quarter ordinal, not a measure."""
    key = (name or "").strip().lower()
    rng = _PERIOD_ORDINALS.get(key)
    if rng is None:
        return False
    non_blank = [v for v in values if not _is_blank(v)]
    if not non_blank:
        return False
    nums = [_to_number(v) for v in non_blank]
    if not all(n is not None for n in nums):
        return False
    lo, hi = rng
    return all(lo <= n <= hi for n in nums)


# Calendar-period YYYYMM columns (a 6-digit numeric month) are time, NOT a
# summable measure. A column stored as Excel/CSV ``202501`` (int) or
# ``202501.0`` (float, the common CSV/Excel parse) or the literal string
# ``"202501"`` would otherwise fall through to the numeric→measure branch
# and lose its time-axis meaning — the chart then sums year numbers instead
# of bucketing by month, and ``suggest_report`` emits no time-trend chart.
# Range: 190001..210012 (month must be 1..12) — keeps plain counts (e.g.
# ``Sell In = 1902``) and bare-year columns (2024, 2025) safely on the
# measure side.
_YYYYMM_MIN = 190001  # 1900-01
_YYYYMM_MAX = 210012  # 2100-12


def _looks_like_yyyymm(values: list[Any]) -> bool:
    """True when a numeric column is a YYYYMM month, not a measure."""
    non_blank = [v for v in values if not _is_blank(v)]
    if not non_blank:
        return False
    for v in non_blank:
        if isinstance(v, bool):  # bool is an int subclass — exclude explicitly.
            return False
        if isinstance(v, (int, float)):
            n = float(v)
        elif isinstance(v, str):
            stripped = v.strip()
            # Reject strings that look like decimals ("202501.5") or carry
            # any sign/whitespace we don't expect from a clean YYYYMM cell.
            if not re.fullmatch(r"\d{6}(?:\.0)?", stripped):
                return False
            n = float(stripped)
        else:
            return False
        if not n.is_integer():
            return False
        ival = int(n)
        if not (_YYYYMM_MIN <= ival <= _YYYYMM_MAX):
            return False
        month = ival % 100
        if not (1 <= month <= 12):
            return False
    return True


def _infer_column_type(values: list[Any], name: str = "") -> str:
    """根据列值（及列名）推断类型"""
    non_blank = [v for v in values if not _is_blank(v)]
    if not non_blank:
        return "category"

    # 周期序号列（周/季度等小整数）是 ordinal，不是可求和的 measure。
    if _looks_like_period_ordinal(name, non_blank):
        return "category"

    # YYYYMM numeric/string column → time, BEFORE the numeric→measure
    # branch. This is checked first because every cell in a YYYYMM column
    # is numeric and would otherwise be classified as a measure (mirrors
    # JS ``dimensionKind`` / ``bucketLabel`` — YYYYMM must bucket to month).
    if _looks_like_yyyymm(non_blank):
        return "time"

    # YYYYMMDD 紧凑日期（20260905）：走共享解析器 date_parse（日历校验 + 年份界
    # 1900–2100，与 table_input_lint 同一口径）。旧版让数值分支把整列判成
    # measure——与体检器对同一文件给出相反结论（lint: time / loader: measure），
    # 时间轴消失、日期被求和。
    import date_parse as _dp
    _compact = 0
    for _v in non_blank:
        _s = str(_v).strip()
        if re.fullmatch(r"\d{8}(?:\.0)?", _s):
            _g = _dp.grain(_s)
            if _g == "day" and 1900 <= int(_s[:4]) <= 2100:
                _compact += 1
    if non_blank and _compact / len(non_blank) >= 0.8:
        return "time"

    # Numeric-dominant columns are measures. A small number of malformed cells
    # is treated as a value-quality issue and normalized to null downstream;
    # structural errors (row width, headers) still fail hard.
    numeric_count = sum(1 for v in non_blank if _to_number(v) is not None)
    if numeric_count == len(non_blank) or numeric_count / len(non_blank) >= 0.8:
        return "measure"

    # 字符串且像时间 → time
    if all(isinstance(v, str) and _looks_like_time(v) for v in non_blank):
        return "time"

    # Entity/id key (name hint or high-cardinality) → id (never a dimension).
    if _looks_like_id(name, non_blank):
        return "id"

    # 其他 → category
    return "category"


def validate_table(data: Any) -> dict:
    """
    校验并规范化二维表数据。

    接受格式：
    {
        "columns": ["A", "B"],
        "rows": [["x", 1], ["y", 2]],
        "meta": {"types": {"A": "category", "B": "measure"}}  # 可选
    }

    返回规范化后的字典（含推断的 types）。
    失败抛 TableValidationError。
    """
    if not isinstance(data, dict):
        raise TableValidationError(f"数据必须是 dict，实际是 {type(data).__name__}")

    if "columns" not in data:
        raise TableValidationError("缺少 'columns' 字段")
    if "rows" not in data:
        raise TableValidationError("缺少 'rows' 字段")

    columns = data["columns"]
    rows = data["rows"]

    if not isinstance(columns, list) or not columns:
        raise TableValidationError("'columns' 必须是非空数组")
    if not all(isinstance(c, str) and c.strip() for c in columns):
        raise TableValidationError("'columns' 的每个元素必须是非空字符串")

    if not isinstance(rows, list):
        raise TableValidationError("'rows' 必须是数组")
    if len(rows) == 0:
        raise TableValidationError("'rows' 不能为空")

    n_cols = len(columns)

    # 校验每行长度
    for i, row in enumerate(rows):
        if not isinstance(row, list):
            raise TableValidationError(f"第 {i + 1} 行不是数组")
        if len(row) != n_cols:
            raise TableValidationError(
                f"第 {i + 1} 行列数不匹配：期望 {n_cols}（{columns}），实际 {len(row)}"
            )

    # 校验 meta.types
    declared_types = {}
    if "meta" in data:
        meta = data["meta"]
        if not isinstance(meta, dict):
            raise TableValidationError("'meta' 必须是 dict")
        if "types" in meta:
            types = meta["types"]
            if not isinstance(types, dict):
                raise TableValidationError("'meta.types' 必须是 dict")
            for col, t in types.items():
                if col not in columns:
                    raise TableValidationError(f"meta.types 引用了未知列 '{col}'")
                if t not in VALID_TYPES:
                    raise TableValidationError(
                        f"列 '{col}' 的类型 '{t}' 非法，必须是 {VALID_TYPES}"
                    )
                declared_types[col] = t

    # 推断未声明的列类型
    inferred_types: dict[str, str] = {}
    for j, col in enumerate(columns):
        if col in declared_types:
            inferred_types[col] = declared_types[col]
        else:
            col_values = [row[j] for row in rows]
            inferred_types[col] = _infer_column_type(col_values, col)

    quality_invalid: dict[str, int] = {}
    quality_samples: dict[str, list[str]] = {}
    quality_text_fallback: dict[str, int] = {}
    text_fallback_samples: dict[str, list[str]] = {}

    # measure 列的非数值细胞，二选一（见 _ID_FLIP_* 常量处的契约）：
    # - 结构性编码污染 → 整列翻转 id，原值全保留（countd 可用）；
    # - 零星脏值 → 置空（never zero）+ meta.quality 报告，供数据体检/徽标呈现。
    # 显式声明 meta.types=measure 的列不翻转（作者的明确意愿压过推断）。
    for col in list(inferred_types):
        if inferred_types[col] != "measure" or col in declared_types:
            continue
        j = columns.index(col)
        bad: list[tuple[int, Any]] = []
        non_blank = 0
        for i, row in enumerate(rows):
            v = row[j]
            if _is_blank(v):
                continue  # 允许空值
            non_blank += 1
            if _to_number(v) is None:
                bad.append((i + 1, v))
        if not bad:
            continue
        if (len(bad) >= _ID_FLIP_MIN_ROWS and non_blank
                and len(bad) / non_blank >= _ID_FLIP_MIN_RATIO
                and sum(1 for _, v in bad if _code_like(v)) >= len(bad) / 2):
            inferred_types[col] = "id"
            quality_text_fallback[col] = len(bad)
            text_fallback_samples[col] = [str(v) for _, v in bad[:3]]
            continue
        quality_invalid[col] = len(bad)
        quality_samples[col] = [f"第{r}行={v!r}" for r, v in bad[:3]]

    # 规范化数字值：measure 列统一转 float
    normalized_rows = []
    for row in rows:
        new_row = []
        for j, col in enumerate(columns):
            v = row[j]
            if inferred_types[col] == "measure":
                num = _to_number(v)
                new_row.append(num if num is not None else None)
            else:
                new_row.append(v)
        normalized_rows.append(new_row)

    meta = {"types": inferred_types}
    # Carried-through profiling facts of a canonical table: ``stats`` (每列非空/
    # 缺失/不同值/范围 — 数据源配置的副行) and ``cardinality`` (mode gate). Both are
    # produced by ``profile_table`` from the SAME rows, and re-loading the canonical
    # JSON must not silently drop them — before this, ``meta.cardinality`` vanished
    # on every canonical re-load and the default-filter seeding fell back to
    # re-scanning rows. ``types``/``quality`` stay authoritative for THIS load.
    incoming_meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    for keep in ("stats", "cardinality"):
        if isinstance(incoming_meta.get(keep), dict) and incoming_meta[keep]:
            meta[keep] = incoming_meta[keep]
    if quality_invalid or quality_text_fallback:
        meta["quality"] = {
            "invalidValues": quality_invalid,
            "invalidSamples": quality_samples,
            "invalidValueCount": sum(quality_invalid.values()),
        }
        if quality_text_fallback:
            # 翻转事实随表走：profile 据此生成 hint + 徽标（字段副行可见），
            # 构建控制台经 profile_table 的 [data] 行透出。
            meta["quality"]["textFallback"] = quality_text_fallback
            meta["quality"]["textFallbackSamples"] = text_fallback_samples
    return {
        "columns": list(columns),
        "rows": normalized_rows,
        "meta": meta,
    }


def table_summary(table: dict) -> str:
    """给人看的简短数据摘要"""
    cols = table["columns"]
    types = table["meta"]["types"]
    n_rows = len(table["rows"])
    parts = [f"{n_rows} 行 × {len(cols)} 列"]
    type_counts = {"category": 0, "measure": 0, "time": 0}
    for c in cols:
        # VALID_TYPES 还含 "id"/"ignore"——.get 收敛，别让摘要函数 KeyError。
        t = types[c] if types[c] in type_counts else None
        type_counts[t] = type_counts.get(t, 0) + 1 if t else 0
    parts.append(f"维度 {type_counts['category'] + type_counts['time']}，指标 {type_counts['measure']}")
    return "，".join(parts)
