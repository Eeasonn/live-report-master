#!/usr/bin/env python3
"""Validate the v3 authoring contract before and after runtime injection.

The strict checks below enforce the mandatory rules in
references/mandatory-rules.md (R1, R2, R3). R4–R8 are enforced by the schema,
the existing chart validations, and the runtime audit.
"""

from __future__ import annotations

# Running from a deployed skill folder must not litter it with __pycache__/:
# the env var is inherited by spawned children (lint/package/spec_to_report),
# the flag covers imports made after this line in this process.
import os as _os, sys as _sys
_os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
_sys.dont_write_bytecode = True
import argparse
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterator

# jsonschema is OPTIONAL: the hand-written checks (R1-R10, shape checks) are the
# real gate. When jsonschema (or its compiled dependency rpds) is not installed —
# e.g. on an air-gapped intranet — validation degrades to _basic_manifest_check.
try:
    import jsonschema
    HAS_JSONSCHEMA = True
except Exception:
    jsonschema = None
    HAS_JSONSCHEMA = False

_WARNED = False


def _warn_once(message: str) -> None:
    global _WARNED
    if not _WARNED:
        print(f"WARNING: {message}", file=sys.stderr)
        _WARNED = True


_VIEW_TYPE_ENUM = {"bar", "stackedBar", "line", "area", "pie", "donut", "scatter", "funnel", "table"}


class ReportContractError(ValueError):
    pass


SKILL_DIR = Path(__file__).resolve().parent.parent
SCHEMA_PATH = SKILL_DIR / "references" / "report-manifest.schema.json"
MANIFEST_RE = re.compile(
    r'<script\b(?=[^>]*\bid=["\']ai-report-manifest["\'])(?=[^>]*\btype=["\']application/json["\'])[^>]*>([\s\S]*?)</script>',
    re.IGNORECASE,
)


def _basic_manifest_check(manifest: dict[str, Any]) -> None:
    """Hand-written stand-in for the JSON schema, used when jsonschema is absent.
    Mirrors report-manifest.schema.json's hard invariants."""
    if manifest.get("schemaVersion") != 3:
        raise ReportContractError("manifest schemaVersion must be 3")
    rid = manifest.get("reportId")
    if not isinstance(rid, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{1,79}", rid):
        raise ReportContractError("manifest reportId must match ^[A-Za-z][A-Za-z0-9_-]{1,79}$")
    if not isinstance(manifest.get("title"), str) or not (1 <= len(manifest["title"]) <= 200):
        raise ReportContractError("manifest title must be 1-200 chars")
    blocks = manifest.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise ReportContractError("manifest blocks must be a non-empty array")
    for b in blocks:
        if not isinstance(b, dict) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{1,79}", str(b.get("id", ""))):
            raise ReportContractError(f"invalid block entry: {b}")
        if b.get("kind") not in {"text", "kpi", "chart", "table", "grid", "group", "media", "divider", "custom"}:
            raise ReportContractError(f"invalid block kind: {b.get('kind')}")
    for c in manifest.get("charts", []):
        if not isinstance(c, dict):
            raise ReportContractError("chart entries must be objects")
        for field in ("id", "blockId", "elementId", "viewType", "option", "editor"):
            if field not in c:
                raise ReportContractError(f"chart missing required field '{field}': {c.get('id')}")
        if c["viewType"] not in _VIEW_TYPE_ENUM:
            raise ReportContractError(f"chart viewType not in catalog: {c['viewType']}")
        editor = c["editor"]
        if editor.get("editable") is True and not isinstance(editor.get("data"), dict):
            raise ReportContractError(f"editable chart must carry editor.data: {c['id']}")
        if editor.get("editable") is False and not isinstance(editor.get("reason"), str):
            raise ReportContractError(f"locked chart must carry editor.reason: {c['id']}")


class ContractParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: set[str] = set()
        self.duplicate_ids: set[str] = set()
        self.block_ids: set[str] = set()
        self.block_layouts: dict[str, dict[str, int]] = {}
        self.grid_count = 0
        self.editable_count = 0
        self.editable_depth = 0
        self.nested_editable = False
        self.tag_stack: list[tuple[str, bool]] = []
        self.external_assets: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        element_id = values.get("id")
        if element_id:
            if element_id in self.ids:
                self.duplicate_ids.add(element_id)
            self.ids.add(element_id)
        if "data-report-grid" in values:
            self.grid_count += 1
        is_editable = "data-editable-text" in values
        if is_editable:
            self.editable_count += 1
            if self.editable_depth:
                self.nested_editable = True
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.tag_stack.append((tag, is_editable))
            if is_editable:
                self.editable_depth += 1
        block_id = values.get("data-report-block-id")
        if block_id:
            self.block_ids.add(block_id)
            # Capture gs-* layout only on the data-report-block element so nested
            # children carrying the same data-report-block-id do not overwrite.
            if "gs-x" in values or "gs-y" in values or "gs-w" in values or "gs-h" in values:
                layout = self.block_layouts.setdefault(block_id, {})
                for key in ("x", "y", "w", "h"):
                    attr = f"gs-{key}"
                    if attr in values:
                        try:
                            layout[key] = int(values[attr])
                        except ValueError:
                            pass
        if tag in {"script", "img", "video", "audio", "source", "iframe"}:
            self._check_url(values.get("src"), f"{tag}[src]")
        if tag == "link" and values.get("rel", "").lower() in {"stylesheet", "preload", "modulepreload"}:
            self._check_url(values.get("href"), "link[href]")

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.tag_stack) - 1, -1, -1):
            open_tag, is_editable = self.tag_stack[index]
            if open_tag != tag:
                continue
            removed = self.tag_stack[index:]
            del self.tag_stack[index:]
            self.editable_depth -= sum(1 for _, editable in removed if editable)
            return

    def _check_url(self, value: str | None, label: str) -> None:
        if not value:
            return
        lowered = value.strip().lower()
        if lowered.startswith(("data:", "blob:", "#")):
            return
        self.external_assets.append(f"{label}={value}")


def extract_manifest(html: str) -> dict[str, Any]:
    matches = MANIFEST_RE.findall(html)
    if len(matches) != 1:
        raise ReportContractError("HTML must contain exactly one application/json #ai-report-manifest script")
    try:
        manifest = json.loads(matches[0])
    except json.JSONDecodeError as error:
        raise ReportContractError(f"Manifest JSON is invalid: {error}") from error
    if HAS_JSONSCHEMA:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        try:
            jsonschema.validate(manifest, schema)
        except jsonschema.ValidationError as error:
            path = "/".join(str(part) for part in error.absolute_path) or "<root>"
            raise ReportContractError(f"Manifest schema violation at {path}: {error.message}") from error
    else:
        _warn_once("jsonschema not available; using built-in manifest shape checks "
                   "(offline/intranet mode). Install jsonschema for full schema validation.")
        _basic_manifest_check(manifest)
    return manifest


# ---------- R1: no function-string formatters in chart options ----------

_FUNCTION_STRING_RE = re.compile(r"^(?:function\s*\(|\s*function\s+|\s*[A-Za-z_$][\w$]*\s*=>|\s*\()", re.MULTILINE)


def _looks_like_function_string(value: str) -> bool:
    """Heuristic: a formatter string is suspicious if it looks like JS source.

    The skill contract forbids function values in manifest JSON. An author who
    smuggles a function via a string (``'function(v){...}'`` or ``'v => ...'``)
    causes ECharts to render the literal text in the published chart.
    """
    stripped = value.strip()
    if not stripped:
        return False
    # Arrow function: `(p) => ...`, `p => ...`, `(...args) => ...`
    if "=>" in stripped:
        # Allow only template-tag arrows? We do not allow any.
        return True
    # Classic function: `function ...` or `(function ...)`
    if stripped.startswith("function") or stripped.startswith("(function"):
        return True
    return False


def _walk_formatters(node: Any, path: str) -> Iterator[tuple[str, str]]:
    if isinstance(node, dict):
        for key, value in node.items():
            child_path = f"{path}.{key}" if path else key
            if key == "formatter" and isinstance(value, str):
                yield child_path, value
            else:
                yield from _walk_formatters(value, child_path)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from _walk_formatters(item, f"{path}[{index}]")


def check_no_function_strings(chart: dict[str, Any]) -> None:
    option = chart.get("option") or {}
    for path, value in _walk_formatters(option, "option"):
        if _looks_like_function_string(value):
            raise ReportContractError(
                f"Chart {chart['id']} has a function-string formatter at {path}; "
                f"use an ECharts template string (e.g. \"{{value}} 万\") or pre-format the data. "
                f"See references/echarts-formatters.md."
            )


# ---------- R10: editable-text inline styles must be editor-covered ----------

# The report editor offers a fixed set of inline formats (bold / italic /
# underline / red / blue / highlight / font-size). A data-editable-text node
# that carries any other inline style would be a style the user can never
# re-create — so it must be rejected at authoring time. Only the properties the
# editor can write are allowed; class attributes are the author's CSS bucket
# (label/value/chip/...) and are left untouched.
_EDITABLE_STYLE_PROPS = {
    "font-weight", "font-style", "text-decoration",
    "color", "background-color", "font-size", "-webkit-text-decoration",
}
_EDITABLE_STYLE_RE = re.compile(r'style\s*=\s*"([^"]*)"', re.IGNORECASE)


def _editable_inner_html(html: str, start: int, tag: str) -> str | None:
    """Return the inner HTML of one data-editable-text element, or None.

    Walks from the opening `<tag ... data-editable-text ...>` at `start`, tracking
    tag balance, to the matching `</tag>`. Handles void tags and ignores
    attributes. This is independent of the streaming ContractParser so the
    style check stays robust.
    """
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
            "param", "source", "track", "wbr"}
    depth = 0
    token = re.compile(r'<!--.*?-->|<[^>]+>', re.DOTALL)
    for m in token.finditer(html, start):
        chunk = m.group(0)
        if chunk.startswith("<!--"):
            continue
        inner = chunk[1:].rstrip("/>")
        if inner.startswith("/"):
            name = inner[1:].strip().lower()
            if name == tag:
                if depth == 0:
                    return html[start:m.start()]
                depth -= 1
        elif chunk.endswith("/>"):
            continue
        else:
            name = inner.split()[0].lower() if inner.split() else ""
            if name in VOID:
                continue
            if name == tag:
                depth += 1
    return None


_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)


def _markup_only(html: str) -> str:
    """Blank out <script>/<style> bodies before scanning for markup.

    The packaged singlefile inlines the runtime JS/CSS. Their source contains
    template strings that LOOK like markup — ``'<h3 data-editable-text>'``,
    ``"<i style=\"left:0\">"`` — and the regex scanners below would treat them
    as DOM (an unbalanced fake tag swallows half the script, then "finds" the
    inline styles of unrelated JS strings). Only real markup may be checked.
    """
    return _SCRIPT_STYLE_RE.sub(lambda m: "<" + m.group(1) + ">", html)


def check_editable_text_styles(html: str) -> None:
    """R10: every data-editable-text must use only editor-covered inline styles."""
    html = _markup_only(html)
    bad: list[str] = []
    # Find each data-editable-text element and validate its inner inline styles.
    for m in re.finditer(r'<([a-zA-Z][a-zA-Z0-9-]*)\b[^>]*\bdata-editable-text\b[^>]*>', html):
        tag = m.group(1).lower()
        start = m.end()
        inner = _editable_inner_html(html, start, tag)
        if inner is None:
            continue  # unclosed; nested/markup errors are caught elsewhere
        for sm in _EDITABLE_STYLE_RE.finditer(inner):
            decls = sm.group(1).split(";")
            for decl in decls:
                decl = decl.strip()
                if not decl:
                    continue
                if ":" not in decl:
                    bad.append(f"malformed style '{decl}'")
                    continue
                prop = decl.split(":", 1)[0].strip().lower()
                if prop not in _EDITABLE_STYLE_PROPS:
                    bad.append(f"style '{prop}' is outside the editor whitelist")
    if bad:
        raise ReportContractError(
            "data-editable-text contains inline styles the editor cannot cover: "
            + "; ".join(sorted(set(bad))[:6])
            + ". Use only bold / italic / underline / red / blue / highlight / font-size."
        )


# ---------- R2: table charts must fit their grid cell ----------

# GridStack cellHeight is 48 px + 8px margin (compact, design-token aligned;
# must match editor-runtime GridStack.init and layout_solver ROW_PX). Block
# chrome (padding + title + margin) is roughly 60 px. The table header row is
# ~28 px and a body row is ~28 px.
CHROME_PX = 60
CELL_HEIGHT_PX = 48
ROW_HEIGHT_PX = 28
HEADER_HEIGHT_PX = 28


def max_table_rows_for_height(grid_h: int) -> int:
    if grid_h <= 0:
        return 0
    usable = grid_h * CELL_HEIGHT_PX - CHROME_PX
    if usable <= HEADER_HEIGHT_PX:
        return 0
    return (usable - HEADER_HEIGHT_PX) // ROW_HEIGHT_PX


def table_height_mode(chart: dict[str, Any]) -> tuple[str, int | None]:
    """WP-7：读 manifest 图表 editor.table.heightMode 三态（请求层形状 WP-3 冻结，
    编译原样透传落 editor.table）。返回 (mode, bounded maxPx)；未知值收敛 grow。"""
    editor = chart.get("editor")
    tbl = editor.get("table") if isinstance(editor, dict) else None
    hm = tbl.get("heightMode") if isinstance(tbl, dict) else None
    if hm == "fixed":
        return "fixed", None
    if isinstance(hm, dict):
        b = hm.get("bounded")
        if isinstance(b, dict):
            mx = b.get("maxPx")
            if isinstance(mx, int) and not isinstance(mx, bool) and 200 <= mx <= 4000:
                return "bounded", mx
    return "grow", None


def bounded_reserve_rows(max_px: int) -> int:
    """有界卡的最小整行预留（帽换算真源 = layout_solver.bounded_table_rows 同算式；
    这里独立成函数供 R2 不变量与报错文案共用）。运行时镜像 = editor-runtime.js
    boundedReserveRows。"""
    return -(-(int(max_px) + CHROME_PX) // CELL_HEIGHT_PX)


def check_table_fits_container(chart: dict[str, Any], block_layouts: dict[str, dict[str, int]]) -> None:
    if chart.get("viewType") != "table":
        return
    layout = block_layouts.get(chart.get("blockId", ""))
    if not layout or "h" not in layout:
        return
    grid_h = int(layout["h"])
    mode, max_px = table_height_mode(chart)
    if mode == "bounded" and max_px:
        # 内部滚动豁免「行数 vs grid_h」（同下方 pagination 豁免理由：maxPx 裁的是
        # 显示窗口，行数可超无滚动容量）；但整行预留必须 ≥ 帽（maxPx+CHROME_PX）——
        # 预留不足 = 有界内容溢出格子被裁。行数换算真源 = layout_solver.bounded_table_rows。
        if grid_h * CELL_HEIGHT_PX < max_px + CHROME_PX:
            raise ReportContractError(
                f"Chart {chart['id']} (table) declares heightMode.bounded.maxPx={max_px}, which needs "
                f"gs-h>={bounded_reserve_rows(max_px)} rows (reserve {grid_h * CELL_HEIGHT_PX}px < "
                f"{max_px + CHROME_PX}px). Raise layout.h or lower maxPx. "
                f"See layout_solver.bounded_table_rows."
            )
        return
    option = chart.get("option") or {}
    if option.get("pagination"):
        return  # author opted in to pagination, scrollbar is acceptable
    rows = option.get("rows")
    if not isinstance(rows, list) or not rows:
        return
    cap = max_table_rows_for_height(grid_h)
    if len(rows) > cap:
        raise ReportContractError(
            f"Chart {chart['id']} (table) has {len(rows)} rows in a grid cell of gs-h={grid_h}; "
            f"max rows without scroll is {cap}. Reduce rows (chart.topK caps to the top N), "
            f"or set chart.pagination=true to display with paging, or raise layout.h. "
            f"See references/mandatory-rules.md R2."
        )


# ---------- R3: sortable charts must be sorted by primary measure ----------

# "price"（2026-10-13 批）：Flint Price intensive——不可加、缺省 average。
MEASURE_TYPE_VOCABULARY = {"currency", "price", "percent", "count", "duration", "number", "unitless"}


def _valid_measure_type(value: Any) -> bool:
    """True when value is a string type or an object form with a valid type."""
    if isinstance(value, str):
        return value in MEASURE_TYPE_VOCABULARY
    if isinstance(value, dict) and isinstance(value.get("type"), str):
        return value["type"] in MEASURE_TYPE_VOCABULARY
    return False


def check_measure_types(chart: dict[str, Any]) -> None:
    """R9: editor.data.measureTypes keys must be selected measures; values must be valid."""
    editor = chart.get("editor") or {}
    data = editor.get("data") or {}
    measure_types = data.get("measureTypes")
    if not isinstance(measure_types, dict) or not measure_types:
        return
    measures = data.get("measures") or []
    for key, value in measure_types.items():
        if key not in measures:
            raise ReportContractError(
                f"Chart {chart['id']} measureTypes key '{key}' is not among the selected "
                f"measures {measures}; keys must reference measure columns. "
                f"See references/mandatory-rules.md R9."
            )
        if not _valid_measure_type(value):
            raise ReportContractError(
                f"Chart {chart['id']} measureTypes['{key}']={value!r} is not a valid semantic type; "
                f"use a string ({sorted(MEASURE_TYPE_VOCABULARY)}) or an object "
                f"{{type, divisor?, unit?}}. See references/mandatory-rules.md R9."
            )


# ---------- R3: sortable charts must be sorted by primary measure ----------

SORTABLE_VIEW_TYPES = {"bar", "stackedBar", "line", "area", "scatter"}
SHARE_VIEW_TYPES = {"pie", "donut", "funnel"}


def _is_non_increasing(values: list[float]) -> bool:
    if len(values) < 2:
        return True
    return all(values[i] >= values[i + 1] for i in range(len(values) - 1))


def _extract_share_values(series_data: list[Any]) -> list[float]:
    values: list[float] = []
    for entry in series_data:
        if isinstance(entry, dict) and "value" in entry:
            try:
                values.append(float(entry["value"]))
            except (TypeError, ValueError):
                return []
        elif isinstance(entry, (int, float)):
            values.append(float(entry))
        else:
            return []
    return values


def _extract_primary_series(series_list: list[dict[str, Any]]) -> Any:
    if not series_list:
        return None
    return series_list[0].get("data")


def _extract_series_values(series_data: list[Any]) -> list[float]:
    """Flatten a series' data to numeric values, or [] if not purely numeric."""
    values: list[float] = []
    for entry in series_data:
        if isinstance(entry, (int, float)):
            values.append(float(entry))
        elif isinstance(entry, dict) and "value" in entry:
            try:
                values.append(float(entry["value"]))
            except (TypeError, ValueError):
                return []
        else:
            return []
    return values


def _stacked_totals(series_list: list[dict[str, Any]]) -> list[float] | None:
    """Per-category totals across all series for a stacked bar. None when ambiguous."""
    columns: list[list[float]] | None = None
    for entry in series_list:
        if not isinstance(entry, dict):
            return None
        values = _extract_series_values(entry.get("data") or [])
        if not values:
            return None
        if columns is None:
            columns = [[v] for v in values]
        elif len(columns) == len(values):
            for i, v in enumerate(values):
                columns[i].append(v)
        else:
            return None
    if columns is None:
        return None
    return [sum(col) for col in columns]


def check_sort_order(chart: dict[str, Any]) -> None:
    view_type = chart.get("viewType")
    if view_type == "original":
        return
    if view_type not in SORTABLE_VIEW_TYPES and view_type not in SHARE_VIEW_TYPES:
        return
    if chart.get("sortOverride"):
        # Author opted out with an explicit ordering intent.
        return
    series_list = (chart.get("option") or {}).get("series") or []
    if view_type == "stackedBar":
        totals = _stacked_totals(series_list)
        if totals is None:
            return
        if not _is_non_increasing(totals):
            raise ReportContractError(
                f"Chart {chart['id']} (stackedBar) category order is not descending by "
                f"stacked total; sort categories by the sum of all series desc (the default), "
                f"or author chart.sort='time' (time axis) / chart.sort='asc' (label-ascending, R3-exempt). "
                f"See references/mandatory-rules.md R3."
            )
        return
    primary_data = _extract_primary_series(series_list)
    if primary_data is None:
        return
    if view_type in SORTABLE_VIEW_TYPES:
        # 交叉表（roles.breakdown 维度×维度透视）：series = **每个拆分值一条**，
        # 不是主指标——「首系列按主指标降序」的检查对象不存在。编译侧
        # _breakdown_series 与运行时 breakdownSeries 两端对分组交叉柱都按同一
        # 透视序渲染（只有 stackedBar 额外按堆叠合计排序，由上方 totals 分支把
        # 守）；旧实现对 bar/line/area 交叉柱误报 R3 违规，把合法的维度×维度
        # 请求整份构建拒绝（第七轮审视实锤）。
        _ed = (chart.get("editor") or {}).get("data") or {}
        if isinstance(_ed.get("roles"), dict) and _ed["roles"].get("breakdown"):
            return
        values = _extract_series_values(primary_data) if isinstance(primary_data, list) else []
        if len(values) < 2:
            return
        if not _is_non_increasing(values):
            raise ReportContractError(
                f"Chart {chart['id']} ({view_type}) primary measure series is not sorted descending; "
                f"sort the data by the primary measure desc (the default), or author "
                f"chart.sort='time' (time axis) / chart.sort='asc' (label-ascending, R3-exempt). "
                f"See references/mandatory-rules.md R3."
            )
    else:  # SHARE_VIEW_TYPES
        if not isinstance(primary_data, list) or len(primary_data) < 2:
            return
        values = _extract_share_values(primary_data)
        if not values or not _is_non_increasing(values):
            actual = primary_data[:3] if isinstance(primary_data, list) else primary_data
            raise ReportContractError(
                f"Chart {chart['id']} ({view_type}) data is not sorted by value descending "
                f"(head: {actual!r}); sort by value desc or author chart.sort='asc' "
                f"(label-ascending, R3-exempt). See references/mandatory-rules.md R3."
            )


# ---------- existing validation pipeline ----------

def validate_html(html: str, packaged: bool = False, forced_mode: str = "auto") -> dict[str, Any]:
    if not re.search(r"<!doctype\s+html", html, re.IGNORECASE):
        raise ReportContractError("HTML must include a doctype")
    if not re.search(r"<html\b", html, re.IGNORECASE) or not re.search(r"<body\b", html, re.IGNORECASE):
        raise ReportContractError("HTML must be a complete document")
    manifest = extract_manifest(html)
    check_editable_text_styles(html)
    parser = ContractParser()
    parser.feed(html)
    if parser.nested_editable:
        raise ReportContractError("data-editable-text elements must not be nested")
    if parser.duplicate_ids:
        raise ReportContractError(f"Duplicate DOM ids: {sorted(parser.duplicate_ids)}")
    if parser.grid_count < 1:
        raise ReportContractError("HTML must contain at least one data-report-grid")
    block_ids = [block["id"] for block in manifest["blocks"]]
    if len(block_ids) != len(set(block_ids)):
        raise ReportContractError("Manifest block ids must be unique")
    missing_blocks = set(block_ids) - parser.block_ids
    if missing_blocks:
        raise ReportContractError(f"Manifest blocks missing from DOM: {sorted(missing_blocks)}")
    datasets: dict[str, dict[str, Any]] = {}
    for dataset in manifest.get("datasets", []):
        if dataset["id"] in datasets:
            raise ReportContractError(f"Duplicate dataset id: {dataset['id']}")
        columns = dataset.get("columns", [])
        # In server mode, columns may be a list of {name, type} objects; collapse to names for the uniq check.
        col_names = [c["name"] if isinstance(c, dict) else c for c in columns]
        if len(col_names) != len(set(col_names)):
            raise ReportContractError(f"Dataset {dataset['id']} columns must be unique")
        if any(not isinstance(row, list) or len(row) != len(col_names) for row in dataset.get("rows", [])):
            raise ReportContractError(f"Dataset {dataset['id']} rows are not rectangular")
        datasets[dataset["id"]] = dataset
    if packaged and not datasets:
        raise ReportContractError("Packaged reports must include at least one dataset for inserting BI components")
    chart_ids: set[str] = set()
    expected_series = {
        "bar": "bar", "stackedBar": "bar", "line": "line", "area": "line",
        "pie": "pie", "donut": "pie", "scatter": "scatter", "funnel": "funnel",
    }
    mandatory_violations: list[str] = []
    for chart in manifest.get("charts", []):
        if chart["id"] in chart_ids:
            raise ReportContractError(f"Duplicate chart id: {chart['id']}")
        chart_ids.add(chart["id"])
        if chart["blockId"] not in set(block_ids):
            raise ReportContractError(f"Chart {chart['id']} references unknown block {chart['blockId']}")
        if chart["elementId"] not in parser.ids:
            raise ReportContractError(f"Chart target is missing: {chart['elementId']}")
        editor = chart["editor"]
        data = editor.get("data")
        if editor["editable"]:
            dataset_id = data.get("datasetId")
            if data.get("crossTable"):
                # Cross-table chart (P2): measures span datasets; validate against
                # the merged editor.data.columns (dimensionKey + measure columns),
                # not any single dataset's raw columns.
                resolved = data
                raw_columns = data.get("columns", [])
                columns = [c["name"] if isinstance(c, dict) else c for c in raw_columns]
                rows = []
            else:
                if dataset_id and dataset_id not in datasets:
                    raise ReportContractError(f"Chart {chart['id']} references unknown dataset {dataset_id}")
                resolved = datasets.get(dataset_id, data)
                raw_columns = resolved.get("columns", [])
                columns = [c["name"] if isinstance(c, dict) else c for c in raw_columns]
                rows = resolved.get("rows", [])
            # Aggregate-level derived measures (dataset.derived.measures) are
            # VIRTUAL columns — they have no raw column by construction (that is
            # what makes a ratio-of-sums / 同环比 possible). They are evaluated by
            # the adapter from the helper aggregations, so accept them here; a
            # typo'd name is still caught because it is in neither set.
            derived_measures = set()
            for _ds in (resolved.get("derived") or {}).get("measures") or []:
                if isinstance(_ds, dict) and _ds.get("name"):
                    derived_measures.add(_ds["name"])
            if not isinstance(resolved, dict):
                derived_measures = set()
            for _m in data.get("measures", []):
                # __measureval__ = 指标名称维度（docs/measure-names-dimension-design.md）
                # 的虚拟值列——运行时按行所属指标解析，无真实列。
                if _m == "*" or _m == "__measureval__" or _m in columns or _m in derived_measures:
                    continue
                raise ReportContractError(
                    f"Chart {chart['id']} editor data references unknown columns: {_m!r}")
            _dim = data.get("dimension")
            if _dim and _dim not in columns and _dim != "__measures__":
                # __measures__ = 指标名称虚拟维度（保留键，不在数据集列宇宙）。
                raise ReportContractError(f"Chart {chart['id']} editor data references unknown columns")
            # 列区透视（0 行维 + 1 列维）：行区为空、roles.breakdown 在场的交叉表
            # 合法——dimension 可省略；breakdown 是真列由下方角色校验兜底。
            if not _dim and not (data.get("roles") or {}).get("breakdown"):
                raise ReportContractError(f"Chart {chart['id']} editor data references unknown columns")
            if any(not isinstance(row, list) or len(row) != len(columns) for row in rows):
                raise ReportContractError(f"Chart {chart['id']} editor data rows are not rectangular")
            # roles 值列存在性（批 B：cols 是**列表**形状——展平后逐项核对）。cols[i]
            # 只被运行时 aggregateTable 按数据集解析、不进静态 data.columns，故 cols
            # 成员只查字符串形状（存在性由编译侧 breakdowns 检查把关）；其余角色值
            # （breakdown 镜像/source/target）仍须是真列。
            _roles_obj = data.get("roles", {})
            _cols_members = set(_roles_obj.get("cols") or []) if isinstance(_roles_obj.get("cols"), list) else set()
            for _rv in _roles_obj.values():
                for _item in (_rv if isinstance(_rv, list) else [_rv]):
                    if not isinstance(_item, str) or not _item:
                        continue
                    if _item in columns or _item in _cols_members or _item == "__measures__":
                        continue
                    raise ReportContractError(f"Chart {chart['id']} editor roles reference unknown columns")
        view_type = chart["viewType"]
        expected = expected_series.get(view_type)
        if view_type == "stackedBar" and chart.get("densityAdjusted") == "line":
            expected = "line"
        series_types = {
            item.get("type") for item in chart.get("option", {}).get("series", [])
            if isinstance(item, dict)
        }
        if expected and expected not in series_types:
            raise ReportContractError(
                f"Chart {chart['id']} declares viewType={view_type} but its option has no {expected} series"
            )
        # ---------- Mandatory rules R1 / R2 / R3 (collected) ----------
        for checker in (check_no_function_strings, check_sort_order, check_measure_types):
            try:
                checker(chart)
            except ReportContractError as error:
                mandatory_violations.append(str(error))
        try:
            check_table_fits_container(chart, parser.block_layouts)
        except ReportContractError as error:
            mandatory_violations.append(str(error))

    # ---------- Server-mode incremental checks (R-S1 ~ R-S5) -----------------
    effective_mode = manifest.get("mode") or ("server" if forced_mode == "server" else None)
    if effective_mode == "server" or forced_mode == "server":
        # R-S3: mode + serverBase required
        if not manifest.get("mode"):
            mandatory_violations.append("R-S3: server-mode manifest must declare top-level 'mode':'server'")
        if not manifest.get("serverBase"):
            mandatory_violations.append("R-S3: server-mode manifest must declare top-level 'serverBase'")
        # R-S2: datasets[*].rows must not appear
        for ds_id, ds in datasets.items():
            if isinstance(ds, dict) and "rows" in ds:
                mandatory_violations.append(
                    f"R-S2: server-mode dataset ('{ds_id}') must not contain 'rows'; remove or switch to singlefile mode"
                )
        # R-S1: editor.data.rows must not appear
        for chart in manifest.get("charts", []):
            if not isinstance(chart, dict): continue
            ed = chart.get("editor", {}).get("data", {})
            if isinstance(ed, dict) and "rows" in ed:
                mandatory_violations.append(
                    f"R-S1: server-mode chart '{chart.get('id','?')}' editor.data.rows is forbidden"
                )
        # R-S5: editor.data.datasetId required
        for chart in manifest.get("charts", []):
            if not isinstance(chart, dict): continue
            ed = chart.get("editor", {}).get("data", {})
            if isinstance(ed, dict) and not ed.get("datasetId"):
                mandatory_violations.append(
                    f"R-S5: server-mode chart '{chart.get('id','?')}' editor.data must declare datasetId"
                )
        # R-S4: option.series must have data (no white-screen)
        for chart in manifest.get("charts", []):
            if not isinstance(chart, dict): continue
            option = chart.get("option", {})
            series = option.get("series") if isinstance(option, dict) else None
            view = chart.get("viewType")
            if view not in ("table", "original"):
                if not series or not any(isinstance(s, dict) and s.get("data") for s in series):
                    mandatory_violations.append(
                        f"R-S4: server-mode chart '{chart.get('id','?')}' option.series missing pre-aggregated data"
                    )
    if mandatory_violations:
        bullets = "\n  - ".join(mandatory_violations)
        raise ReportContractError(
            f"{len(mandatory_violations)} mandatory rule violation(s):\n  - {bullets}"
        )
    if parser.external_assets:
        raise ReportContractError(f"External rendering assets are not allowed: {parser.external_assets[:3]}")
    runtime_count = html.count('id="ai-report-runtime"') + html.count("id='ai-report-runtime'")
    if packaged and runtime_count != 1:
        raise ReportContractError("Packaged HTML must contain exactly one #ai-report-runtime")
    if not packaged and runtime_count:
        raise ReportContractError("Draft HTML already contains an editor runtime")
    return {
        "reportId": manifest["reportId"],
        "blocks": len(block_ids),
        "charts": len(manifest.get("charts", [])),
        "datasets": len(datasets),
        "editableTextNodes": parser.editable_count,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a Live Report Master v3 document")
    parser.add_argument("--html", required=True)
    parser.add_argument("--packaged", action="store_true")
    parser.add_argument("--mode", choices=("auto", "server", "singlefile"), default="auto",
                        help="validate as if the manifest declares this mode")
    args = parser.parse_args()
    try:
        result = validate_html(Path(args.html).read_text(encoding="utf-8"),
                               packaged=args.packaged, forced_mode=args.mode)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except ReportContractError as error:
        print(f"Report contract violation: {error}", file=sys.stderr)
        return 2
    except Exception as error:
        print(f"Unable to validate report: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
