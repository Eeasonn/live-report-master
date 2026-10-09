"""Load the skill's canonical 2D table from JSON or an Excel workbook.

The Excel contract is deliberately narrow: the first worksheet is used by
default, row 1 is the header, and the remaining used cells must form a dense
rectangular table.  Keeping this adapter independent of the renderer makes the
JSON representation useful for agents and for deterministic tests.

输入处理契约（2026-09-18 用户裁决）：**缺省直通，不做任何数据预处理**。
``load_table_file`` 缺省（``tidy=False``）对结构缺陷只做「成表所必需」的确定性
修复并把修复记录写进 ``meta.quality.notes``——重复表头改名（列名.2）、空白表头
格命名（列N）、参差行补齐/截断、数据区中间的整行空行跳过；一个数据值都不改。
``tidy=True``（report_build ``--sort`` 整理模式）恢复严格契约：上述缺陷一律
抛 ``TableValidationError`` 拒绝构建。数据质量体检（重复值/千分位/嵌套表头…）
由 ``table_input_lint`` 独立只读报告，与本模块的读取行为解耦。
"""

from __future__ import annotations

import json
import csv
import io
import posixpath
import re
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from validate_table import TableValidationError, validate_table

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CELL_REF_RE = re.compile(r"^([A-Z]+)([0-9]+)$")
_ERROR_VALUES = {
    "#N/A", "#VALUE!", "#REF!", "#DIV/0!", "#NAME?", "#NUM!", "#NULL!",
}

_MAX_NOTES = 20


def _header_cells(header: list[Any], kind: str) -> list[str]:
    """Header row as stripped strings（两种模式共用的读取步）."""
    return [("" if value is None else str(value).strip()) for value in header]


def _repair_columns(header: list[Any], kind: str, *, tidy: bool) -> tuple[list[str], list[str]]:
    """表头列名处理：tidy（整理模式）沿用原契约直接拒绝；缺省直通做确定性修复。

    修复只动**列名**（成表所必需：列名是聚合/物化的键，必须唯一非空），不动任何
    数据值：空白格命名「列N」、重复名追加 .2/.3 后缀。返回 (columns, notes)。
    """
    cells = _header_cells(header, kind)
    if tidy:
        if any(not c for c in cells):
            blank_msg = (f"{kind} 第一行必须是完整表头，表头不能有空白单元格"
                         if kind == "Excel" else f"{kind} 第一行必须是完整表头")
            raise TableValidationError(blank_msg)
        columns = cells
        if len(set(columns)) != len(columns):
            raise TableValidationError(f"{kind} 第一行表头不能重复")
        return columns, []
    notes: list[str] = []
    columns: list[str] = []
    seen: dict[str, int] = {}
    for idx, name in enumerate(cells, start=1):
        if not name:
            name = f"列{idx}"
            notes.append(f"表头第 {idx} 列为空 → 命名「{name}」")
        if name in seen:
            seen[name] += 1
            fixed = f"{name}.{seen[name]}"
            notes.append(f"表头第 {idx} 列重名「{name}」→ 改名「{fixed}」")
            name = fixed
        else:
            seen[name] = 1
        columns.append(name)
    return columns, notes


def _attach_notes(result: dict, notes: list[str]) -> dict:
    """把直通修复记录写进 meta.quality.notes（封顶 20 条，余量折叠计数）。"""
    if not notes:
        return result
    shown = notes[:_MAX_NOTES]
    if len(notes) > _MAX_NOTES:
        shown.append(f"…（其余 {len(notes) - _MAX_NOTES} 项修复略）")
    quality = result.setdefault("meta", {}).setdefault("quality", {})
    quality["notes"] = quality.get("notes", []) + shown
    return result


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(element: ET.Element, name: str) -> ET.Element | None:
    for child in element:
        if _local_name(child.tag) == name:
            return child
    return None


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element if _local_name(child.tag) == name]


def _column_number(letters: str) -> int:
    result = 0
    for char in letters:
        result = result * 26 + ord(char) - ord("A") + 1
    return result


def _shared_strings(root: ET.Element) -> list[str]:
    values: list[str] = []
    for si in root.iter():
        if _local_name(si.tag) != "si":
            continue
        values.append("".join(t.text or "" for t in si.iter() if _local_name(t.tag) == "t"))
    return values


def _date_style_indexes(archive: zipfile.ZipFile) -> set[int]:
    if "xl/styles.xml" not in archive.namelist():
        return set()
    root = ET.fromstring(archive.read("xl/styles.xml"))
    custom_formats: dict[int, str] = {}
    for node in root.iter():
        if _local_name(node.tag) == "numFmt":
            try:
                custom_formats[int(node.attrib["numFmtId"])] = node.attrib.get("formatCode", "")
            except (KeyError, ValueError):
                continue
    date_format_ids = set(range(14, 23)) | {27, 30, 36, 45, 46, 47, 50, 57}
    for format_id, code in custom_formats.items():
        cleaned = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', "", code).lower()
        if re.search(r"[ymdhis]", cleaned):
            date_format_ids.add(format_id)

    cell_xfs = next((node for node in root.iter() if _local_name(node.tag) == "cellXfs"), None)
    if cell_xfs is None:
        return set()
    return {
        index
        for index, xf in enumerate(_children(cell_xfs, "xf"))
        if int(xf.attrib.get("numFmtId", "0")) in date_format_ids
    }


def _cell_value(cell: ET.Element, shared: list[str], date_styles: set[int]) -> Any:
    cell_type = cell.attrib.get("t")
    value_node = _child(cell, "v")
    value = value_node.text if value_node is not None else None

    if cell_type == "inlineStr":
        node = _child(cell, "is")
        return "".join(t.text or "" for t in node.iter() if _local_name(t.tag) == "t") if node is not None else ""
    if value is None:
        return None
    if cell_type == "s":
        try:
            return shared[int(value)]
        except (IndexError, ValueError) as exc:
            raise TableValidationError(f"Excel shared string 索引无效：{value}") from exc
    if cell_type == "b":
        return value == "1"
    if cell_type in {"str", "d"}:
        return value
    if cell_type == "e":
        # Excel error cells are value-level quality issues, not structural
        # contract violations.  Keep the cell missing so all downstream
        # aggregates distinguish it from zero; the reader records the count.
        return None

    try:
        number = float(value)
    except ValueError:
        return value
    try:
        style_index = int(cell.attrib.get("s", "0"))
    except ValueError:
        style_index = 0
    if style_index in date_styles:
        # Excel serials below 60 need the extra leap-year-compatibility day.
        moment = datetime(1899, 12, 30) + timedelta(days=number + (1 if 0 < number < 60 else 0))
        if moment.time() == datetime.min.time():
            return moment.date().isoformat()
        return moment.isoformat(timespec="seconds")
    return int(number) if number.is_integer() else number


def _sheet_path(archive: zipfile.ZipFile, sheet_index: int) -> str:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    sheets = [node for node in workbook.iter() if _local_name(node.tag) == "sheet"]
    if sheet_index < 0 or sheet_index >= len(sheets):
        raise TableValidationError(
            f"Excel 工作表索引越界：{sheet_index}，工作簿共有 {len(sheets)} 个工作表"
        )
    rel_id = sheets[sheet_index].attrib.get(f"{{{_REL_NS}}}id")
    if not rel_id:
        raise TableValidationError("Excel 工作表缺少关系 ID")

    rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    for rel in rels.iter():
        if _local_name(rel.tag) == "Relationship" and rel.attrib.get("Id") == rel_id:
            target = rel.attrib.get("Target", "")
            normalized_target = target.lstrip("/")
            return posixpath.normpath(
                normalized_target if normalized_target.startswith("xl/")
                else posixpath.join("xl", normalized_target)
            )
    raise TableValidationError(f"找不到 Excel 工作表关系：{rel_id}")


def _read_sheet_stream(
    archive: zipfile.ZipFile,
    sheet_name: str,
    shared: list[str],
    date_styles: set[int],
) -> tuple[list[list[Any]], int]:
    """流式读一张工作表为稠密矩阵（含表头行），返回 (matrix, error_cells)。

    旧实现整树 DOM（``ET.fromstring`` 全量 + rows_by_number 字典 + matrix 二次
    拷贝）：29 万行 × 31 列的表 XML 解压后数百 MB，DOM + 两份拷贝把峰值内存推到
    4GB 级（真机 SWAP 后 35 分钟跑不完一份数据）。iterparse 逐行取值后清空，
    峰值 ≈ 一行的 DOM + sharedStrings 表 + 最终矩阵本身。

    语义与旧 dict 实现逐位对齐：行号稀疏 → 补全 None 空行；单元格列位以 ``r``
    地址为准（乱序/重复地址后者胜，同 dict 覆盖语义）；行缺 ``r`` 属性按出现
    顺序连续编号；``t="e"`` 错误格计数并置 None。
    """
    with archive.open(sheet_name) as probe:
        # <mergeCells 只能以标签形式出现（文本/属性值里的裸 < 必须转义），字节
        # 扫描不会误伤含「mergeCells」字样的单元格文本。先扫先报，与旧实现的
        # 报错顺序一致（不浪费一整趟行解析才发现合并单元格）。tail 接住上一块
        # 的末 16 字节：不接的话，token 恰好骑在 1MB 块边界上的大表会整趟漏检
        # ——合并单元格被静默当稀疏数据读进来（左上角有值、其余全 None）。
        tail = b""
        while True:
            chunk = probe.read(1 << 20)
            if not chunk:
                break
            if b"<mergeCells" in tail + chunk:
                raise TableValidationError("只支持纯二维表，Excel 合并单元格不受支持")
            tail = chunk[-16:]

    row_numbers: list[int] = []
    dense_rows: list[list[Any]] = []
    max_column = 0
    error_cells = 0
    contiguous = True
    sheet_data: ET.Element | None = None
    with archive.open(sheet_name) as stream:
        for event, elem in ET.iterparse(stream, events=("start", "end")):
            if event == "start":
                if sheet_data is None and _local_name(elem.tag) == "sheetData":
                    sheet_data = elem
                continue
            if _local_name(elem.tag) != "row":
                continue
            row_number = int(elem.attrib.get("r", len(row_numbers) + 1))
            if row_number != len(row_numbers) + 1:
                contiguous = False
            values: dict[int, Any] = {}
            for cell in _children(elem, "c"):
                match = _CELL_REF_RE.match(cell.attrib.get("r", ""))
                if not match:
                    raise TableValidationError("Excel 单元格缺少有效地址")
                column_number = _column_number(match.group(1))
                if cell.attrib.get("t") == "e":
                    error_cells += 1
                values[column_number] = _cell_value(cell, shared, date_styles)
                if column_number > max_column:
                    max_column = column_number
            width = max(values) if values else 0
            dense_rows.append([values.get(i) for i in range(1, width + 1)])
            row_numbers.append(row_number)
            # 清空当前行 + 已收行集（行是 sheetData 的孩子，只清 elem 不够——
            # sheetData 自身会攒下几十万个空 Element）。解析器只往栈顶 open
            # 元素追加，不回读已结束的子节点，清空是安全的。
            elem.clear()
            if sheet_data is not None:
                sheet_data.clear()

    if not dense_rows or max_column == 0:
        raise TableValidationError("Excel 第一个工作表没有可用的二维数据")
    max_row = max(row_numbers)
    if contiguous:
        for row in dense_rows:
            if len(row) < max_column:
                row.extend([None] * (max_column - len(row)))
        return dense_rows, error_cells
    by_number = dict(zip(row_numbers, dense_rows))
    matrix: list[list[Any]] = []
    for row_number in range(1, max_row + 1):
        row = by_number.get(row_number)
        if row is None:
            matrix.append([None] * max_column)
            continue
        if len(row) < max_column:
            row.extend([None] * (max_column - len(row)))
        matrix.append(row)
    return matrix, error_cells


def _read_xlsx(path: Path, sheet_index: int = 0, *, tidy: bool = False,
               validate: bool = True) -> dict:
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise TableValidationError(f"无法读取 .xlsx 文件：{path.name}") from exc

    with archive:
        names = set(archive.namelist())
        if "xl/workbook.xml" not in names:
            raise TableValidationError("文件不是有效的 .xlsx 工作簿（缺少 workbook.xml）")
        shared = []
        if "xl/sharedStrings.xml" in names:
            shared = _shared_strings(ET.fromstring(archive.read("xl/sharedStrings.xml")))
        date_styles = _date_style_indexes(archive)
        sheet_name = _sheet_path(archive, sheet_index)
        if sheet_name not in names:
            raise TableValidationError(f"找不到工作表文件：{sheet_name}")
        matrix, error_cells = _read_sheet_stream(archive, sheet_name, shared, date_styles)

    header = matrix[0]
    columns, notes = _repair_columns(header, "Excel", tidy=tidy)

    # A completely empty row inside the used rectangle is not a record.
    data_rows = matrix[1:]
    while data_rows and all(value is None for value in data_rows[-1]):
        data_rows.pop()
    if not data_rows:
        raise TableValidationError("Excel 表头下面没有数据行")
    if tidy:
        if any(all(value is None for value in row) for row in data_rows):
            raise TableValidationError("Excel 数据区不能包含空白整行")
    else:
        kept: list[list[Any]] = []
        skipped: list[int] = []
        for offset, row in enumerate(data_rows, start=2):  # 2 = 工作表里的真实行号（表头占第 1 行）
            if all(value is None for value in row):
                skipped.append(offset)
                continue
            kept.append(row)
        data_rows = kept
        if skipped:
            shown = ", ".join(str(n) for n in skipped[:10]) + ("…" if len(skipped) > 10 else "")
            notes.append(f"数据区空白整行已跳过（工作表行号：{shown}）")
    if not validate:
        # 裸读（load_table_files 并集内部用）：跳过类型推断/洗值——类型必须以
        # **合并后全集**判定（W35 某列全数字、W36 混入文本，按文件各自推断会
        # 各自洗值，数据真值事故），notes/错误格计数由并集方归因合并。
        quality: dict[str, Any] = {"notes": list(notes)}
        if error_cells:
            quality["excelErrorCells"] = error_cells
        return {"columns": columns, "rows": data_rows, "meta": {"quality": quality}}
    result = validate_table({"columns": columns, "rows": data_rows})
    if error_cells:
        result.setdefault("meta", {}).setdefault("quality", {})["excelErrorCells"] = error_cells
    return _attach_notes(result, notes)


def _read_csv(path: Path, *, tidy: bool = False, validate: bool = True) -> dict:
    """Read UTF-8/UTF-8-BOM (with GB18030 fallback) CSV/TSV without third parties."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise TableValidationError(f"读取 CSV/TSV 失败：{path.name}: {exc}") from exc
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        # Excel on zh-CN Windows commonly writes CSV as ANSI/GBK. Fall back to
        # GB18030 before failing; this keeps the same zero-dependency reader.
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
    try:
        parsed = list(csv.reader(io.StringIO(text), dialect))
    except csv.Error as exc:
        raise TableValidationError(f"CSV 解析失败：{path.name}: {exc}") from exc
    if not parsed:
        raise TableValidationError("CSV 没有可用数据")
    header = parsed[0]
    columns, notes = _repair_columns(header, "CSV", tidy=tidy)
    error_cells = 0
    rows: list[list[Any]] = []
    ragged: list[int] = []
    blank_rows: list[int] = []
    for row_index, row in enumerate(parsed[1:], start=2):
        if len(row) != len(columns):
            if tidy:
                raise TableValidationError(
                    f"CSV 第 {row_index} 行列数不匹配：期望 {len(columns)}，实际 {len(row)}"
                )
            ragged.append(row_index)
            row = list(row[:len(columns)]) + [""] * (len(columns) - len(row))
        normalized: list[Any] = []
        for value in row:
            stripped = value.strip()
            if stripped in _ERROR_VALUES:
                error_cells += 1
                normalized.append(None)
            else:
                normalized.append(value if value != "" else None)
        # 整行全空不是一条记录（与 xlsx 直通同一裁决）：跳过并记录，不进数据集。
        if all(v is None for v in normalized):
            blank_rows.append(row_index)
            continue
        rows.append(normalized)
    if ragged:
        shown = ", ".join(str(n) for n in ragged[:10]) + ("…" if len(ragged) > 10 else "")
        notes.append(f"列数不齐的行已按表头补齐/截断（文件行号：{shown}）")
    if blank_rows and not tidy:
        shown = ", ".join(str(n) for n in blank_rows[:10]) + ("…" if len(blank_rows) > 10 else "")
        notes.append(f"空白整行已跳过（文件行号：{shown}）")
    while rows and all(value is None for value in rows[-1]):
        rows.pop()
    if not rows:
        raise TableValidationError("CSV 表头下面没有数据行")
    if not validate:
        quality2: dict[str, Any] = {"notes": list(notes)}
        if error_cells:
            quality2["errorCells"] = error_cells
        return {"columns": columns, "rows": rows, "meta": {"quality": quality2}}
    result = validate_table({"columns": columns, "rows": rows})
    if error_cells:
        result.setdefault("meta", {}).setdefault("quality", {})["errorCells"] = error_cells
    return _attach_notes(result, notes)


def load_table_file(path: str | Path, sheet: int = 0, *, tidy: bool = False,
                    validate: bool = True) -> dict:
    """Load and validate a canonical table from JSON, CSV or Excel.

    ``tidy=False``（缺省，直通模式）：结构缺陷做确定性修复（重复表头改名、空白
    表头格命名、参差行补齐、空白整行跳过），修复记录写 ``meta.quality.notes``，
    绝不因数据质量问题拒绝——对应 report_build 缺省（无 ``--sort``）。
    ``tidy=True``（整理模式，report_build ``--sort``）：上述缺陷一律抛
    ``TableValidationError``（原始严格契约）。
    """
    file_path = Path(path)
    suffix = file_path.suffix.lower()
    if suffix == ".json":
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TableValidationError(f"读取 JSON 表失败：{file_path.name}: {exc}") from exc
        # JSON exports sometimes carry spreadsheet errors as strings.
        if isinstance(data, dict) and isinstance(data.get("rows"), list):
            quality = 0
            rows = []
            for row in data["rows"]:
                normalized = []
                for value in row:
                    if isinstance(value, str) and value.strip() in _ERROR_VALUES:
                        quality += 1
                        normalized.append(None)
                    else:
                        normalized.append(value)
                rows.append(normalized)
            data = {**data, "rows": rows}
            result = validate_table(data)
            if quality:
                result.setdefault("meta", {}).setdefault("quality", {})["errorCells"] = quality
            return result
        return validate_table(data)
    if suffix in {".csv", ".tsv"}:
        return _read_csv(file_path, tidy=tidy, validate=validate)
    if suffix == ".xlsx":
        return _read_xlsx(file_path, sheet_index=sheet, tidy=tidy, validate=validate)
    if suffix == ".xls":
        raise TableValidationError("暂不支持旧版 .xls；请另存为 .xlsx 后重试")
    raise TableValidationError("表格输入必须是 .xlsx、.csv、.tsv 或 canonical .json 文件")


def load_table_files(paths: list[str | Path], sheet: int = 0, *, tidy: bool = False) -> dict:
    """N 张同构表纵向并集（UNION，2026-09-22 构建期多文件）。

    为什么：W35+W36 这类「同结构补新一期」此前在构建期没有正道，执行 Agent 被
    逼着手工合并/转换源文件（违反「禁改原始数据」红线）。``datasets[].file``
    收数组后，构建入口把 N 个文件并成一张规范表——profile/编译/服务端全链路
    仍按单表走（并集只发生在构建入口一次）。

    语义：
    - 每个文件先各自走同一套读取修复（重复表头改名/参差行补齐/空白整行跳过；
      tidy 严格门与 ``load_table_file`` 同一实现）；
    - **列集闸 = 顺序无关、按名对齐**（与增量合并 upsert 同一口径：手工维护的
      周表列序偶尔会动）；不一致抛可行动错误（缺列/多列逐个列出）；
    - **类型以合并后全集为准**：每文件只裸读（``validate=False``，不推断不洗
      值），拼接后对合并表整体跑一次 ``validate_table``；
    - notes 逐文件归因（「文件 N（名）：note」），错误格计数求和；
    - 单文件入参 = ``load_table_file`` 原路径逐字节不变。
    """
    file_paths = [Path(p) for p in paths]
    if not file_paths:
        raise TableValidationError("并集需要至少一个数据文件")
    if len(file_paths) == 1:
        return load_table_file(file_paths[0], sheet, tidy=tidy)
    raws: list[tuple[Path, dict]] = []
    for fp in file_paths:
        if fp.suffix.lower() == ".json":
            # canonical JSON 本就是规范态（导出即验证过）：取验证后的列行，
            # notes/质量键照搬——并集的类型权威仍在下方合并后的整体 validate。
            raws.append((fp, load_table_file(fp, sheet, tidy=tidy)))
        else:
            raws.append((fp, load_table_file(fp, sheet, tidy=tidy, validate=False)))
    base_cols = [str(c) for c in raws[0][1]["columns"]]
    base_name = raws[0][0].name
    combined: list[list] = [list(r) for r in raws[0][1]["rows"]]
    notes: list[str] = []
    error_total = 0
    for idx, (fp, raw) in enumerate(raws, start=1):
        quality = raw.get("meta", {}).get("quality", {}) or {}
        tag = f"文件 {idx}（{fp.name}）"
        notes.extend(f"{tag}：{n}" for n in (quality.get("notes") or []))
        error_total += int(quality.get("errorCells") or 0) + int(quality.get("excelErrorCells") or 0)
        if idx == 1:
            continue
        cols = [str(c) for c in raw["columns"]]
        missing = [c for c in base_cols if c not in cols]
        extra = [c for c in cols if c not in base_cols]
        if missing or extra:
            raise TableValidationError(
                f"并集{tag}的列结构与文件 1（{base_name}）不一致"
                f"（缺列：{','.join(missing[:5])}；多列：{','.join(extra[:5])}）。"
                "同构并集要求所有文件字段一致（列序可以不同）。")
        proj = [cols.index(c) for c in base_cols]
        width = len(cols)
        for r in raw["rows"]:
            combined.append([r[i] if i < width else None for i in proj])
    result = validate_table({"columns": base_cols, "rows": combined})
    quality = result.setdefault("meta", {}).setdefault("quality", {})
    if error_total:
        quality["errorCells"] = error_total
    if notes:
        quality["notes"] = quality.get("notes", []) + notes
    return result


__all__ = ["load_table_file", "load_table_files"]
