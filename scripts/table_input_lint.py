#!/usr/bin/env python3
"""输入契约校验器（WP-0A）——「严格二维表输入」约束的守门人。

约束②（docs/product-iteration-plan.md §1.1）：输入严控二维表格，行级加工让渡给
Excel 加列。本模块在 ``report_build --table`` 进入编译前对输入做一次**只读体检**：

* **fail（结构破坏）**：列名空/重复（xlsx 合并单元格表头首当其冲）、行列数不齐
  （ragged rows）、首两行呈嵌套表头形态、合并单元格、无数据行；
* **warn（质量风险）**：全空行/列、疑似内嵌合计行、逐列类型推断报告（数值/文本/
  时间 + 不可解析样本数）、千分位/百分号/货币符号残留、疑似 Excel 日期序列号、
  前导零文本编码。

检查项与 ``docs/input-contract.md`` §5「常见输入质量坑清单」一一对应。

设计边界（非目标）：**不改解析行为**——``table_input.load_table_file`` 仍是唯一
读取入口，本模块自带一份**容错读取**（遇到缺陷不抛异常、如实报告），因为体检的
对象恰恰是会让正式读取器抛异常的文件。时间值判定**复用** ``date_parse``（单一
真源，不另写日期正则）；xlsx 单元格取值复用 ``table_input`` 的解码内部函数
（日期样式/共享字符串/错误格），保证体检与正式读取看到同一个值。

报告 JSON 结构稳定（供测试与后续可视化断言）::

    {"version": 1, "strict": bool, "ok": bool, "files": [report, ...]}
    report = {"file", "ok", "failCount", "warnCount", "fail": [...],
              "warn": [...], "shape": {"columnCount", "dataRows"},
              "columns": [{"name", "index", "type", "timeGrain", "nonEmpty",
                           "empty", "unparseable", "outlierSamples"}]}

每条 finding 形如 ``{"check": <稳定检查名>, "message": <可行动文案>, ...上下文}``。
"""
from __future__ import annotations

# 导入 skill 兄弟模块前先禁字节码（与 report_build/tests 同一纪律：skill 目录
# 不留 __pycache__）。被 report_build 导入时它已设置，setdefault 无害。
import os as _os
_os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
import sys as _sys
_sys.dont_write_bytecode = True

import csv
import io
import json
import re
import zipfile
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from date_parse import grain as dp_grain, infer_grain as dp_infer_grain
from table_input import (  # 容错读取复用正式读取器的解码内部函数（单一真源）
    _CELL_REF_RE,
    _cell_value,
    _children,
    _column_number,
    _date_style_indexes,
    _local_name,
    _shared_strings,
    _sheet_path,
)

LINT_VERSION = 1

# ---------------- fail（结构破坏）检查名 ----------------
# empty_header / duplicate_header / ragged_row / nested_header /
# merged_cells / no_data_rows / unreadable
# ---------------- warn（质量风险）检查名 ----------------
# empty_row / empty_column / embedded_total_row / trailing_total_row /
# thousand_separator / percent_symbol / currency_symbol / date_serial /
# leading_zero / type_outliers

# 纯数字文本（不带千分位/符号/百分号）——千分位等残留正是要被 warn 的对象
_NUMBER_RE = re.compile(r"^-?(?:\d+(?:\.\d+)?|\.\d+)$")
_THOUSANDS_RE = re.compile(r"^\d{1,3}(?:,\d{3})+(?:\.\d+)?$")
_CURRENCY_HEAD = "¥￥$€£"
# 疑似内嵌合计行：整格值就是这些词（与 docs/input-contract.md §5#4 对齐）
_TOTAL_WORDS = {"合计", "小计", "总计", "共计", "total", "subtotal", "grand total"}
# 疑似 Excel 日期序列号：数值落在 20000–60000（≈1954-10..2064-05）
_SERIAL_MIN, _SERIAL_MAX = 20000, 60000
# 数字形态时间值（YYYY / YYYYMM / YYYYMMDD）的年份合理界。date_parse 对纯数字只做
# 日历有效性（月 01–12、日存在），**不设年份界**——datetime.date 接受 1..9999，于是
# 曝光量/收入里的大整数（127011 →「1270年11月」）会被误判成 time、整列被报
# type_outliers（marketing_campaign 冒烟 P2-3）。体检侧对齐 date_parse 的 epoch 界
# （978307200..4102444800 即 2001..2100）同一收紧哲学：数字形态必须落在 1900–2100 年，
# 界外按数值报告。带分隔符的写法（2026/9/5）形状明确、不进这道闸。
_YEAR_FLOOR, _YEAR_CEIL = 1900, 2100
# 与 date_parse._RE_FLOAT_ZERO 同形（"202501.0" → "202501"；整 token 数字+.0 才剥）
_RE_FLOAT_ZERO = re.compile(r"^(\d+)\.0+$")
# 列名暗示日期（序列号检查只对这类列生效，避免误伤普通数值列）
_DATE_NAME_RE = re.compile(r"日期|时间|date|time", re.I)
# 裸四位年按时间处理需要列名年提示（与 date_parse.infer_grain 同一规则）
_YEAR_HINT_RE = re.compile(r"年|年份|year", re.I)
# Excel 序列号纪元（与 table_input 的 1899-12-30 基准一致）
_SERIAL_EPOCH = datetime(1899, 12, 30)

# 逐格分类的记忆化：时间判定走 date_parse（单一真源），其余是廉价分支。
# 数字走「精确快速路径」：date_parse 对整数只可能给出三种答案——
#   * 4/6/8 位（1000..99991231）的年/月/日形态；
#   * 978307200..4102444800 的 epoch 秒（ai_date 的行为，但 BI 业务表里这个
#     量级几乎总是营收/计数而非时间戳，体检报告有意从严按数值报告）；
#   * 其余一律非时间。
# 字符串逐值缓存（维度列重复率高，335k 行 PSI 只需几百次真实解析）。
_KIND_CACHE: dict[tuple[bool, str], str] = {}


def _digit_year(value: Any) -> int | None:
    """数字形态（4/6/8 位纯数字，容许 .0 浮点尾）的年份部分；其他形状返回 None。

    只用于圈定「数字被读成时间」的形状（date_parse 的 \\d{4}/\\d{6}/\\d{8} 分支）；
    带分隔符/纪元秒等非纯数字形状返回 None，不进年份闸。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not value.is_integer():
            return None
        s = str(int(value))
    else:
        s = str(value).strip()
        m0 = _RE_FLOAT_ZERO.match(s)
        if m0:
            s = m0.group(1)
        if not s.isdigit():
            return None
    if len(s) not in (4, 6, 8):
        return None
    return int(s[:4])


def _is_digit_time(value: Any, year_hint: bool) -> bool:
    """时间判定复用 date_parse.grain（单一真源），数字形态再叠年份合理界。"""
    g = dp_grain(value)
    if not g:
        return False
    year = _digit_year(value)
    if year is not None and not (_YEAR_FLOOR <= year <= _YEAR_CEIL):
        return False
    return g != "year" or year_hint


def _cell_kind(v: Any, year_hint: bool) -> str:
    """单格类型：number | time | text | empty。时间判定复用 date_parse。"""
    if v is None:
        return "empty"
    if isinstance(v, bool):
        return "text"
    if isinstance(v, (int, float)):
        if isinstance(v, float) and not v.is_integer():
            return "number"
        n = int(v)
        # 1000..99991231 之外的量级（epoch 秒等）有意从严按数值报告（见模块头注释）
        if 1000 <= n <= 99991231 and _is_digit_time(n, year_hint):
            return "time"
        return "number"
    s = str(v).strip()
    if not s:
        return "empty"
    key = (year_hint, s)
    kind = _KIND_CACHE.get(key)
    if kind is None:
        if _is_digit_time(s, year_hint):
            kind = "time"
        elif _NUMBER_RE.match(s):
            kind = "number"
        else:
            kind = "text"
        _KIND_CACHE[key] = kind
    return kind


def _is_blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and v.strip() == "")


def _is_total_marker(v: Any) -> bool:
    return isinstance(v, str) and v.strip().lower() in _TOTAL_WORDS


def _looks_percent(s: str) -> bool:
    return bool(re.fullmatch(r"\d+(?:\.\d+)?\s*%", s))


def _looks_currency(s: str) -> bool:
    if not s:
        return False
    if s[0] in _CURRENCY_HEAD:
        return bool(re.fullmatch(r"[¥￥$€£]\s*[\d,]+(?:\.\d+)?", s))
    return s.endswith("元") and bool(re.fullmatch(r"[\d,]+(?:\.\d+)?\s*元", s))


def _serial_date_label(n: int) -> str:
    """Excel 日期序列号 → 可读日期（1899-12-30 基准，与 table_input 一致）。"""
    try:
        return (_SERIAL_EPOCH + timedelta(days=int(n))).date().isoformat()
    except (OverflowError, ValueError):
        return "?"


# --------------------------------------------------------------------------
# 容错读取：目标恰恰是会让正式读取器抛异常的文件，所以这里只收集、不抛。
# --------------------------------------------------------------------------

def _table_from_csv(path: Path) -> tuple[list[str], list[list[Any]], list[dict]]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"读取失败：{exc}") from exc
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        # Excel on zh-CN Windows 常写 GBK——与 table_input._read_csv 同一回退。
        text = raw.decode("gb18030", errors="replace")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
    except csv.Error:
        first_line = sample.splitlines()[0] if sample.splitlines() else ""
        if path.suffix.lower() == ".tsv" or "\t" in first_line:
            dialect = csv.excel_tab
        else:
            dialect = csv.excel
    parsed = list(csv.reader(io.StringIO(text), dialect))
    if not parsed:
        return [], [], []
    header = [str(v).strip() for v in parsed[0]]
    rows: list[list[Any]] = []
    for row in parsed[1:]:
        rows.append([v if v != "" else None for v in row])
    return header, rows, []


def _table_from_xlsx(path: Path) -> tuple[list[str], list[list[Any]], list[dict]]:
    archive = zipfile.ZipFile(path)  # BadZipFile → 上层 unreadable
    with archive:
        names = set(archive.namelist())
        if "xl/workbook.xml" not in names:
            raise ValueError("文件不是有效的 .xlsx 工作簿（缺少 workbook.xml）")
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            shared = _shared_strings(ET.fromstring(archive.read("xl/sharedStrings.xml")))
        date_styles = _date_style_indexes(archive)
        sheet_name = _sheet_path(archive, 0)
        if sheet_name not in names:
            raise ValueError(f"找不到工作表文件：{sheet_name}")
        root = ET.fromstring(archive.read(sheet_name))
        merge_refs: list[str] = []
        for node in root.iter():
            if _local_name(node.tag) == "mergeCells":
                merge_refs.extend(
                    mc.attrib.get("ref", "") for mc in _children(node, "mergeCell"))
        rows_by_number: dict[int, dict[int, Any]] = {}
        max_column = 0
        for row_node in root.iter():
            if _local_name(row_node.tag) != "row":
                continue
            row_number = int(row_node.attrib.get("r", len(rows_by_number) + 1))
            values: dict[int, Any] = {}
            for cell in _children(row_node, "c"):
                match = _CELL_REF_RE.match(cell.attrib.get("r", ""))
                if not match:
                    raise ValueError("Excel 单元格缺少有效地址")
                column_number = _column_number(match.group(1))
                # 错误格（t="e"）在正式读取里记 quality 并置 None，这里同口径。
                values[column_number] = _cell_value(cell, shared, date_styles)
                max_column = max(max_column, column_number)
            rows_by_number[row_number] = values
        if not rows_by_number or max_column == 0:
            return [], [], [{"check": "merged_cells", "count": len(merge_refs),
                             "refs": merge_refs[:10]}] if merge_refs else []
        max_row = max(rows_by_number)
        matrix = [
            [rows_by_number.get(row_number, {}).get(column_number)
             for column_number in range(1, max_column + 1)]
            for row_number in range(1, max_row + 1)
        ]
    header = ["" if v is None else str(v).strip() for v in matrix[0]]
    fails: list[dict] = []
    if merge_refs:
        fails.append({"check": "merged_cells", "count": len(merge_refs),
                      "refs": merge_refs[:10]})
    return header, [list(r) for r in matrix[1:]], fails


def _table_from_json(data: Any) -> tuple[list[str], list[list[Any]], list[dict]]:
    if isinstance(data, dict) and isinstance(data.get("columns"), list) \
            and isinstance(data.get("rows"), list):
        cols = ["" if c is None else str(c).strip() for c in data["columns"]]
        rows = [list(r) if isinstance(r, list) else [] for r in data["rows"]]
        return cols, rows, []
    if isinstance(data, dict) and isinstance(data.get("rows"), list) \
            and data["rows"] and all(isinstance(r, dict) for r in data["rows"]):
        cols = list(data["rows"][0].keys())
        rows = [[r.get(c) for c in cols] for r in data["rows"]]
        return cols, rows, []
    raise ValueError('canonical JSON 需要形如 {"columns": [...], "rows": [[...]]}')


def _unreadable(path: Path, message: str) -> dict:
    return {
        "file": str(path), "ok": False, "failCount": 1, "warnCount": 0,
        "fail": [{"check": "unreadable", "message": message}],
        "warn": [], "columns": [], "shape": {"columnCount": 0, "dataRows": 0},
    }


def lint_file(path: str | Path) -> dict:
    """对一份输入表做体检，返回稳定结构的报告 dict（不抛异常）。"""
    p = Path(path)
    suffix = p.suffix.lower()
    try:
        if suffix == ".json":
            data = json.loads(p.read_text(encoding="utf-8"))
            columns, rows, extra_fails = _table_from_json(data)
        elif suffix in {".csv", ".tsv"}:
            columns, rows, extra_fails = _table_from_csv(p)
        elif suffix == ".xlsx":
            columns, rows, extra_fails = _table_from_xlsx(p)
        elif suffix == ".xls":
            return _unreadable(p, "暂不支持旧版 .xls；请另存为 .xlsx 后重试")
        else:
            return _unreadable(p, "表格输入必须是 .xlsx、.csv、.tsv 或 canonical .json 文件")
    except Exception as exc:  # 体检对象可能就是坏文件——报告而不是崩
        return _unreadable(p, f"无法读取：{type(exc).__name__}: {exc}")
    return lint_columns_rows(columns, rows, file=str(p), extra_fail=extra_fails)


# --------------------------------------------------------------------------
# 纯检查核心：lint_columns_rows（可测试、与载体无关）
# --------------------------------------------------------------------------

def lint_columns_rows(columns: list[Any], rows: list[list[Any]], *,
                      file: str = "", extra_fail: list[dict] | None = None) -> dict:
    fail: list[dict] = [dict(f) for f in (extra_fail or [])]
    warn: list[dict] = []
    raw_names = ["" if c is None else str(c).strip() for c in (columns or [])]
    ncols = len(raw_names)
    matrix = [list(r) if isinstance(r, (list, tuple)) else [] for r in (rows or [])]

    # ---- 表头：空名 / 重复名（合并单元格表头首当其冲）----
    empty_cols = [i + 1 for i, n in enumerate(raw_names) if not n]
    if ncols == 0:
        fail.append({"check": "no_data_rows",
                     "message": "没有可用的二维数据（缺表头行）——首行必须是唯一的表头行"})
    elif empty_cols:
        fail.append({
            "check": "empty_header",
            "message": (f"表头第 {empty_cols} 列列名为空——合并单元格/缺表头会让列名读取失败；"
                        "表头必须一行、一列一名、零合并（docs/input-contract.md §1.1–1.2）"),
            "columns": empty_cols,
        })
    dup_counts = Counter(n for n in raw_names if n)
    if dup_counts:
        for name, count in sorted(dup_counts.items()):
            if count > 1:
                idxs = [i + 1 for i, n in enumerate(raw_names) if n == name]
                fail.append({
                    "check": "duplicate_header",
                    "message": (f"列名「{name}」出现 {count} 次（第 {idxs} 列）——引用歧义；"
                                "请唯一命名（如 销售额_含税 / 销售额_不含税）"),
                    "name": name, "columns": idxs,
                })

    # ---- 行列整齐化：pad / truncate 并记录 ragged ----
    ragged_rows: list[int] = []
    if ncols:
        normalized: list[list[Any]] = []
        for i, row in enumerate(matrix):
            if len(row) == ncols:
                normalized.append(row)
                continue
            ragged_rows.append(i + 2)  # 表头=第 1 行，数据行从第 2 行起
            fixed = list(row[:ncols]) + [None] * (ncols - len(row))
            normalized.append(fixed)
        matrix = normalized
    if ragged_rows:
        shown = ragged_rows[:10]
        fail.append({
            "check": "ragged_row",
            "message": (f"{len(ragged_rows)} 行的列数与表头({ncols})不一致（如第 {shown} 行）——"
                        "数据区必须是致密矩形；空值留空单元格，不要多格/少格"),
            "rows": shown, "count": len(ragged_rows), "expected": ncols,
        })

    # ---- 末尾整行空白修剪（与正式读取器同一口径；余下的空行即中间空行）----
    while matrix and all(_is_blank(v) for v in matrix[-1]):
        matrix.pop()
    nrows = len(matrix)
    if nrows == 0 and ncols:
        fail.append({"check": "no_data_rows",
                     "message": "表头下面没有数据行——只有表头的表无法构建报告"})

    # ---- 单趟扫描：类型计数 / 各类残留 / 合计标记 / 空行空列 / 嵌套表头证据 ----
    year_hints = [_YEAR_HINT_RE.search(n) is not None for n in raw_names]
    name_is_date = [_DATE_NAME_RE.search(n) is not None for n in raw_names]
    counts = [{"number": 0, "time": 0, "text": 0, "empty": 0} for _ in range(ncols)]
    samples: list[dict[str, list[str]]] = [{"number": [], "time": [], "text": []}
                                           for _ in range(ncols)]
    thousand: list[list[str]] = [[] for _ in range(ncols)]
    percent: list[list[str]] = [[] for _ in range(ncols)]
    currency: list[list[str]] = [[] for _ in range(ncols)]
    leadzero: list[list[str]] = [[] for _ in range(ncols)]
    serial_count = [0] * ncols
    serial_sample: list[str | None] = [None] * ncols
    blank_rows: list[int] = []
    marker_rows: list[int] = []
    marker_words: Counter = Counter()
    numeric_below = [0] * ncols    # 第 2 个数据行以下（嵌套表头对比基线）
    nonblank_below = [0] * ncols

    for i, row in enumerate(matrix):
        row_blank = True
        row_has_marker = False
        for c in range(ncols):
            v = row[c] if c < len(row) else None
            if not _is_blank(v):
                row_blank = False
                if i >= 1:  # 第 2 个数据行起 = 嵌套表头检测的「下方」基线
                    nonblank_below[c] += 1
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        numeric_below[c] += 1
                    elif isinstance(v, str) and _NUMBER_RE.match(v.strip()):
                        numeric_below[c] += 1
            kind = _cell_kind(v, year_hints[c])
            counts[c][kind] += 1
            if kind != "empty" and len(samples[c][kind]) < 3:
                samples[c][kind].append(str(v))
            if isinstance(v, str):
                s = v.strip()
                if not s:
                    continue
                head = s[0]
                if "," in s and head.isdigit() and _THOUSANDS_RE.match(s) \
                        and len(thousand[c]) < 3:
                    thousand[c].append(s)
                if s.endswith("%") and _looks_percent(s) and len(percent[c]) < 3:
                    percent[c].append(s)
                if (head in _CURRENCY_HEAD or s.endswith("元")) \
                        and _looks_currency(s) and len(currency[c]) < 3:
                    currency[c].append(s)
                if head == "0" and len(s) >= 2 and _NUMBER_RE.match(s) \
                        and len(leadzero[c]) < 3:
                    leadzero[c].append(s)
                if _is_total_marker(v):
                    row_has_marker = True
                    marker_words[s.lower()] += 1
            if name_is_date[c] and kind == "number":
                # 疑似 Excel 日期序列号：CSV 里是数字文本，xlsx 里是 int/float——
                # 只要该列的值是纯数值且落在序列号区间就计一次。
                try:
                    n = float(v)  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    n = None
                if n is not None and float(n).is_integer() \
                        and _SERIAL_MIN <= n <= _SERIAL_MAX:
                    serial_count[c] += 1
                    if serial_sample[c] is None:
                        serial_sample[c] = f"{int(n)}≈{_serial_date_label(int(n))}"
        if row_blank:
            blank_rows.append(i + 2)
        if row_has_marker:
            marker_rows.append(i + 2)

    # ---- 嵌套表头（首两行呈两级表头形态）----
    if nrows >= 2 and ncols >= 2:
        first = matrix[0]
        texty = 0
        outliers = 0
        for c in range(ncols):
            fv = first[c]
            if _is_blank(fv):
                continue
            if isinstance(fv, bool):
                numeric_first = False
            elif isinstance(fv, (int, float)):
                numeric_first = True
            else:
                numeric_first = bool(_NUMBER_RE.match(str(fv).strip()))
            if numeric_first:
                continue
            texty += 1
            if nonblank_below[c] >= 2 and \
                    numeric_below[c] / nonblank_below[c] >= 0.8:
                outliers += 1
        labels = Counter(
            str(first[c]).strip() for c in range(ncols)
            if not _is_blank(first[c])
            and not (isinstance(first[c], (int, float))
                     and not isinstance(first[c], bool))
            and not _NUMBER_RE.match(str(first[c]).strip()))
        repeated = max(labels.values()) if labels else 0
        cond_repeat = repeated >= 2 and texty >= max(2, ncols // 2)
        cond_type = (ncols >= 3 and texty >= ncols * 0.6
                     and outliers >= max(2, ncols // 2))
        if cond_repeat or cond_type:
            detail = []
            if repeated >= 2:
                detail.append(f"第 2 行有重复跨列标签（如「{labels.most_common(1)[0][0]}」×{repeated}）")
            if cond_type:
                detail.append(f"第 2 行在 {outliers}/{ncols} 列上与下方数值行类型相悖")
            fail.append({
                "check": "nested_header",
                "message": ("首两行呈嵌套（两级）表头形态：" + "；".join(detail) +
                            "——表头只保留一行；分组标签应融化为长表的一列"
                            "（docs/input-contract.md §1.3）"),
                "firstDataRow": 2, "columnCount": ncols,
                "repeatedLabelSpan": repeated, "numericOutlierColumns": outliers,
            })

    # ---- warn：空行 / 空列 / 内嵌合计行 ----
    if blank_rows:
        shown = blank_rows[:10]
        warn.append({
            "check": "empty_row",
            "message": (f"第 {shown} 行整行为空（共 {len(blank_rows)} 行）——空行会被当成记录"
                        "或导致读取失败；不要用空行做视觉分隔，请删除"),
            "rows": shown, "count": len(blank_rows),
        })
    empty_col_report = [{"name": raw_names[c], "index": c + 1}
                        for c in range(ncols)
                        if counts[c]["number"] + counts[c]["time"] + counts[c]["text"] == 0]
    if empty_col_report:
        names = [e["name"] or f"第{e['index']}列" for e in empty_col_report]
        warn.append({
            "check": "empty_column",
            "message": (f"列 {names} 整列为空——会被当噪音维度/占位列，请删除"
                        "（表格对象 Ctrl+T 天然不会产生空列）"),
            "columns": [e["index"] for e in empty_col_report],
            "names": names,
        })
    last_row_no = nrows + 1
    inner_markers = [r for r in marker_rows if r != last_row_no]
    trailing_markers = [r for r in marker_rows if r == last_row_no]
    words = "、".join(f"「{w}」" for w in list(marker_words)[:4])
    if inner_markers:
        shown = inner_markers[:10]
        warn.append({
            "check": "embedded_total_row",
            "message": (f"第 {shown} 行疑似内嵌合计行（标记值 {words}）——会被当普通记录参与聚合，"
                        "指标翻倍；合计/小计由报告按当前筛选行集生成，请删除这些行"),
            "rows": shown, "count": len(inner_markers),
        })
    if trailing_markers:
        warn.append({
            "check": "trailing_total_row",
            "message": (f"末行（第 {last_row_no} 行）疑似合计行（标记值 {words}）——"
                        "同样会被当普通记录聚合导致指标翻倍，建议删除"),
            "rows": trailing_markers,
        })

    # ---- warn + 报告：逐列类型推断（date_parse 复用）与残留 ----
    columns_report: list[dict] = []
    for c in range(ncols):
        cnt = counts[c]
        non_empty = cnt["number"] + cnt["time"] + cnt["text"]
        if non_empty == 0:
            ctype, grain_label, unparseable, outlier_samples = "empty", None, 0, []
        else:
            # P2-3 收尾：数值占主导的列上，零星「数字形态时间格」（长得像 YYYYMM 的
            # 大整数/其文本，如数值列里混入的 "193211"）按 number 口径合并 —— 对齐
            # date_parse.infer_grain 的 0.6 主导阈值：一列里只有零星值像月份，
            # 它们是「像月份的数」，不是日期列；真日期列（≥60% 主导）不受影响。
            # 「数字形态时间格」合并判定要对**全列** time 值核实，不能只信前 3 个
            # 样本（samples 每类只存 3 个）——混合形态列（YYYYMM 在前、2026/9/5
            # 在后）曾被前 3 个样本骗过、整列误报 number + type_outliers。
            _time_all_numeric = False
            if 0 < cnt["time"] < cnt["number"] and cnt["time"] / non_empty < 0.6:
                _time_all_numeric = all(
                    _digit_year(str(v)) is not None
                    for row in matrix
                    for v in [row[c] if c < len(row) else None]
                    if _cell_kind(v, year_hints[c]) == "time")
            if cnt["time"] and cnt["time"] < cnt["number"] \
                    and cnt["time"] / non_empty < 0.6 \
                    and _time_all_numeric:
                cnt["number"] += cnt["time"]
                cnt["time"] = 0
            dominant = max(("number", "time", "text"), key=lambda k: cnt[k])
            ctype = dominant if cnt[dominant] / non_empty >= 0.6 else "mixed"
            grain_label = None
            if dominant == "time":
                g = dp_infer_grain([r[c] for r in matrix], raw_names[c])
                grain_label = g
            unparseable = non_empty - cnt[dominant]
            merged: list[str] = []
            for k in ("number", "time", "text"):
                if k != dominant:
                    merged.extend(samples[c][k])
            outlier_samples = merged[:3]
        columns_report.append({
            "name": raw_names[c], "index": c + 1, "type": ctype,
            "timeGrain": grain_label if ctype in {"time", "mixed"} else None,
            "nonEmpty": non_empty, "empty": cnt["empty"],
            "unparseable": unparseable if ctype != "empty" else 0,
            "outlierSamples": outlier_samples,
        })
        name = raw_names[c] or f"第{c + 1}列"
        if thousand[c]:
            warn.append({
                "check": "thousand_separator",
                "message": (f"列「{name}」有带千分位的文本数字（如 {thousand[c][0]}）——"
                            "会被当文本处理/静默强转；值请存纯数字，千分位样式用单元格"
                            "「数字格式」表达，不打进值里"),
                "column": name, "samples": list(thousand[c]),
            })
        if percent[c]:
            warn.append({
                "check": "percent_symbol",
                "message": (f"列「{name}」有带百分号的文本（如 {percent[c][0]}）——"
                            "请存数值并在字段说明写清口径（0–1 还是 0–100）"),
                "column": name, "samples": list(percent[c]),
            })
        if currency[c]:
            warn.append({
                "check": "currency_symbol",
                "message": (f"列「{name}」有带货币符号的文本（如 {currency[c][0]}）——"
                            "会被当文本处理；值请存纯数字，货币单位写进字段说明"),
                "column": name, "samples": list(currency[c]),
            })
        if leadzero[c]:
            warn.append({
                "check": "leading_zero",
                "message": (f"列「{name}」有前导零文本编码（如 {leadzero[c][0]}）——"
                            "CSV 经 Excel 再开会静默吞掉前导零；编码列请设文本格式并用 "
                            "xlsx 交付"),
                "column": name, "samples": list(leadzero[c]),
            })
        if serial_count[c]:
            warn.append({
                "check": "date_serial",
                "message": (f"列「{name}」有 {serial_count[c]} 个值落在 Excel 日期序列号区间"
                            f"（{_SERIAL_MIN}–{_SERIAL_MAX}，如 {serial_sample[c]}）——"
                            "产品不认序列号；日期列请保持日期类型或 ISO 文本（2026-09-15）"),
                "column": name, "count": serial_count[c],
            })
        if 0 < unparseable and non_empty >= 3:
            if ctype == "mixed":
                warn.append({
                    "check": "type_outliers",
                    "message": (f"列「{name}」类型混杂（数值 {cnt['number']} / 时间 {cnt['time']} / "
                                f"文本 {cnt['text']}）——混型列无法稳定聚合/建时间轴，"
                                "请统一该列的类型"),
                    "column": name, "columnType": ctype, "count": non_empty - max(cnt.values()),
                    "samples": outlier_samples,
                })
            else:
                kind_cn = {"number": "数值", "time": "时间", "text": "文本"}[ctype]
                warn.append({
                    "check": "type_outliers",
                    "message": (f"列「{name}」主体为{kind_cn}，但 {unparseable} 个值"
                                f"不属于该类型（示例 {outlier_samples}）——混入的异类值会被"
                                "记为空/静默强转，请核对原始数据"),
                    "column": name, "columnType": ctype, "count": unparseable,
                    "samples": outlier_samples,
                })

    fail_count = len(fail)
    warn_count = len(warn)
    return {
        "file": file,
        "ok": fail_count == 0,
        "failCount": fail_count,
        "warnCount": warn_count,
        "fail": fail,
        "warn": warn,
        "shape": {"columnCount": ncols, "dataRows": nrows},
        "columns": columns_report,
    }


def lint_inputs(paths: list[str | Path], *, strict: bool = False) -> dict:
    """多份输入的整体报告（report_build 写入 input-lint.json 的形状）。"""
    reports = [lint_file(p) for p in paths]
    return {
        "version": LINT_VERSION,
        "strict": bool(strict),
        "ok": all(r.get("ok") for r in reports),
        "files": reports,
    }


# --------------------------------------------------------------------------
# CLI：python3 table_input_lint.py <table...> [--strict] [--json]
# --------------------------------------------------------------------------

def _main(argv: list[str]) -> int:
    args = [a for a in argv if a != "--json" and a != "--strict"]
    as_json = "--json" in argv
    strict = "--strict" in argv
    if not args:
        print(__doc__.strip().splitlines()[0], file=_sys.stderr)
        print("用法：table_input_lint.py <table.xlsx|csv|tsv|json> [--strict] [--json]",
              file=_sys.stderr)
        return 2
    doc = lint_inputs(args, strict=strict)
    if as_json:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
    else:
        for r in doc["files"]:
            shape = r.get("shape") or {}
            rows = shape.get("dataRows")
            rows_txt = f"{rows:,}" if isinstance(rows, int) else "?"
            print(f"[input-lint] {_brief(r)}: {shape.get('columnCount', '?')} 列 × "
                  f"{rows_txt} 行 — FAIL {r.get('failCount', 0)} / WARN {r.get('warnCount', 0)}")
            for f in (r.get("fail") or [])[:10]:
                print(f"[input-lint]   FAIL [{f.get('check')}] {f.get('message')}")
            for w in (r.get("warn") or [])[:10]:
                print(f"[input-lint]   WARN [{w.get('check')}] {w.get('message')}")
            for col in (r.get("columns") or []):
                grain = f"/{col['timeGrain']}" if col.get("timeGrain") else ""
                print(f"[input-lint]   col {col['index']:>2} {col['name']}: "
                      f"{col['type']}{grain} 非空 {col['nonEmpty']:,} 不可解析 {col['unparseable']}")
    total_fail = sum(r.get("failCount", 0) for r in doc["files"])
    total_warn = sum(r.get("warnCount", 0) for r in doc["files"])
    if total_fail:
        return 2
    if strict and total_warn:
        return 2
    return 0


def _brief(r: dict) -> str:
    f = r.get("file") or ""
    return Path(f).name if f else "(memory)"


if __name__ == "__main__":
    raise SystemExit(_main(_sys.argv[1:]))
