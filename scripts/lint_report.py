#!/usr/bin/env python3
"""Warning-level lint for the v3 authoring contract.

Lint findings never block packaging. Use ``--strict`` in CI to convert
warnings into exit-code failures. The mandatory rules R1/R2/R3 still live
in ``validate_report.py``; this script covers:

  L1  Heatmap y-axis categories not monotonic by row total
  L2  Chart missing tooltip block
  L3  Legend textStyle.fontSize < 11
  L4  Axis label fontSize < 10
  L5  Chart block without visible title
  L6  Series with all-zero or empty data
  L7  Heatmap with more than 200 cells
  L8  Table rows between 0.75 * cap and cap
  L9  More than 4 KPI blocks
  L10 More than 15 charts in a single report
  L11 Legend lists more than 8 series
  L12 Dataset or editor.data has duplicate column names
  L13 Series uses only one colour (informational)
  L14 Currency/percent-like measure without measureTypes declared
  L15 Scatter axis with almost no data spread
  L16 Heatmap value span < 30% without a diverging palette, or
      visualMap.min=0 with a high data floor
  L17 Rate/percent measure aggregated with sum, or placed in a stackedBar
  L18 Editable text node is empty or whitespace-only
  L19 Report chart theme is a non-default preset
  L20 Price/intensive measure averaged while ignoring an existing category
      column — the average is mix-driven, not a price level (2026-10-13)
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
from collections.abc import Iterable, Iterator
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from validate_report import (
    ContractParser,
    MANIFEST_RE,
    ReportContractError,
    _editable_inner_html,
    extract_manifest,
    max_table_rows_for_height,
)


# ---------- finding model ----------

SEVERITY_WARN = "warn"
SEVERITY_INFO = "info"


def _finding(rule: str, severity: str, chart_id: str | None, message: str,
             fix: str | None = None, path: str | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "rule": rule,
        "severity": severity,
        "chartId": chart_id,
        "message": message,
    }
    if fix:
        entry["fix"] = fix
    if path:
        entry["path"] = path
    return entry


# ---------- shared helpers ----------

def _walk(node: Any, path: str = "") -> Iterator[tuple[str, Any]]:
    if isinstance(node, dict):
        for k, v in node.items():
            child = f"{path}.{k}" if path else k
            yield child, v
            yield from _walk(v, child)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            child = f"{path}[{i}]"
            yield child, item
            yield from _walk(item, child)


def _get_in(node: Any, *keys: str) -> Any:
    current = node
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


# ---------- L1: heatmap sort ----------

def _heatmap_axis_order(chart: dict[str, Any]) -> tuple[list[str], list[str], list[Any]] | None:
    if chart.get("viewType") != "heatmap":
        return None
    option = chart.get("option") or {}
    x_axis = option.get("xAxis") or {}
    y_axis = option.get("yAxis") or {}
    x_data = x_axis.get("data") if isinstance(x_axis, dict) else None
    y_data = y_axis.get("data") if isinstance(y_axis, dict) else None
    series = (option.get("series") or [])
    if not series:
        return None
    cells = series[0].get("data") or []
    if not (x_data and y_data and cells):
        return None
    return list(x_data), list(y_data), cells


def check_heatmap_sort(chart: dict[str, Any], findings: list[dict[str, Any]]) -> None:
    info = _heatmap_axis_order(chart)
    if not info:
        return
    x_data, y_data, cells = info
    x_index = {label: idx for idx, label in enumerate(x_data)}
    y_index = {label: idx for idx, label in enumerate(y_data)}
    # Build row totals: sum value across x for each y
    row_totals: list[float] = [0.0 for _ in y_data]
    for cell in cells:
        if not isinstance(cell, (list, tuple)) or len(cell) < 3:
            continue
        try:
            xi, yi, v = int(cell[0]), int(cell[1]), float(cell[2])
        except (TypeError, ValueError):
            continue
        if 0 <= yi < len(row_totals):
            row_totals[yi] += v
    # Determine if row_totals is monotonic in y_data order
    sequence = row_totals
    if len(sequence) < 3:
        return
    ascending = all(sequence[i] <= sequence[i + 1] for i in range(len(sequence) - 1))
    descending = all(sequence[i] >= sequence[i + 1] for i in range(len(sequence) - 1))
    if not (ascending or descending):
        findings.append(_finding(
            "L1", SEVERITY_WARN, chart["id"],
            "heatmap y-axis categories are not monotonic by row total; "
            "readers cannot scan the gradient top-to-bottom.",
            "sort breakdown categories by row total ascending or descending, "
            "or set sortOverride='measure-asc'/'measure-desc'.",
        ))


# ---------- L18: empty / whitespace-only editable text ----------

def _strip_tags(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def check_text_blank(html: str, findings: list[dict[str, Any]]) -> None:
    """Flag a data-editable-text whose visible text is empty/whitespace-only.

    Such a node renders as a blank region, which reads as an authoring defect
    (Issue 1). The block either needs real content or should be removed.
    """
    for m in re.finditer(r"<([a-zA-Z][a-zA-Z0-9-]*)\b[^>]*\bdata-editable-text\b[^>]*>", html):
        tag = m.group(1).lower()
        start = m.end()
        inner = _editable_inner_html(html, start, tag)
        if inner is None:
            continue
        if len(_strip_tags(inner)) == 0:
            findings.append(_finding(
                "L18", SEVERITY_WARN, None,
                f"editable text block (<{tag} data-editable-text>) is empty or whitespace-only "
                "and will render as a blank region.",
                "fill in the text content, or remove the block if it is not needed.",
                path=f"{tag}[data-editable-text]",
            ))


# ---------- L19: non-default report chart theme ----------

def check_chart_theme(manifest: dict[str, Any], findings: list[dict[str, Any]]) -> None:
    """Flag a report that opens with a non-default report theme.

    ``style.chartTheme`` is the single report-theme axis (整报告 house，
    docs/report-theme-convergence-design.md): it re-skins every chart AND the
    report shell (cover gradient, KPI accents, table headers, filter chips,
    semantic up/down). It is a USER-facing choice: a company report shipped in
    Power BI styling only because the request happened to pick a preset nobody
    asked for. The report must open in the default theme; the toolbar
    「主题 ▾」still lets a reader switch.
    """
    theme = str(manifest.get("chartTheme") or "default")
    if theme in ("", "default"):
        return
    findings.append(_finding(
        "L19", SEVERITY_WARN, None,
        f"report theme is {theme!r} (non-default): the report opens in that preset's colours.",
        "omit style.chartTheme (default) unless the user explicitly asked for a report style.",
        path="style.chartTheme",
    ))


# ---------- L2: missing tooltip ----------

def check_tooltip(chart: dict[str, Any], findings: list[dict[str, Any]]) -> None:
    option = chart.get("option") or {}
    if "tooltip" in option:
        return
    # Tables surface values through cells; no ECharts tooltip needed.
    if chart.get("viewType") == "table":
        return
    # When measureTypes covers the primary measure, the runtime auto-generates
    # the tooltip; the static option intentionally omits it.
    data = (chart.get("editor") or {}).get("data") or {}
    measures = data.get("measures") or []
    measure_types = data.get("measureTypes") or {}
    if measures and measure_types.get(measures[0]):
        return
    findings.append(_finding(
        "L2", SEVERITY_WARN, chart["id"],
        "chart option is missing a tooltip block; readers cannot inspect values.",
        "add \"tooltip\": {\"trigger\": \"axis\"} for category charts "
        "or {\"trigger\": \"item\"} for share-of-whole charts.",
    ))


# ---------- L3: legend font size ----------

def check_legend_size(chart: dict[str, Any], findings: list[dict[str, Any]]) -> None:
    option = chart.get("option") or {}
    legend = option.get("legend")
    if not isinstance(legend, dict):
        return
    text_style = legend.get("textStyle")
    if not isinstance(text_style, dict):
        findings.append(_finding(
            "L3", SEVERITY_WARN, chart["id"],
            "legend has no textStyle.fontSize; defaults below 11 may be unreadable.",
            "set legend.textStyle.fontSize to at least 11.",
        ))
        return
    size = text_style.get("fontSize")
    if isinstance(size, (int, float)) and size < 11:
        findings.append(_finding(
            "L3", SEVERITY_WARN, chart["id"],
            f"legend.textStyle.fontSize={size} is below the readability floor of 11.",
            "bump to 11 or larger.",
        ))


# ---------- L4: axis label font size ----------

def check_axis_font_size(chart: dict[str, Any], findings: list[dict[str, Any]]) -> None:
    option = chart.get("option") or {}
    for axis_key in ("xAxis", "yAxis"):
        axes = option.get(axis_key)
        if not isinstance(axes, list):
            axes = [axes] if isinstance(axes, dict) else []
        for index, axis in enumerate(axes):
            if not isinstance(axis, dict):
                continue
            label = axis.get("axisLabel")
            if not isinstance(label, dict):
                continue
            size = label.get("fontSize")
            if isinstance(size, (int, float)) and size < 10:
                path = f"{axis_key}[{index}].axisLabel.fontSize" if len(axes) > 1 else f"{axis_key}.axisLabel.fontSize"
                findings.append(_finding(
                    "L4", SEVERITY_WARN, chart["id"],
                    f"{path}={size} is below the readability floor of 10.",
                    "bump to 10 or larger.",
                    path=path,
                ))


# ---------- L5: chart without visible title ----------

def check_block_title(chart: dict[str, Any], parser: ContractParser, findings: list[dict[str, Any]]) -> None:
    block_id = chart.get("blockId")
    if not block_id:
        return
    title_present = False
    for tag, attrs in parser.block_text_attrs.get(block_id, []):
        classes = (attrs.get("class") or "").split()
        if "block-title" in classes:
            title_present = True
            break
    if not title_present:
        findings.append(_finding(
            "L5", SEVERITY_WARN, chart["id"],
            f"block {block_id} has no element with class block-title; chart appears anonymous.",
            "wrap a title in `<h4 class=\"block-title\">…</h4>` inside the chart's grid item.",
        ))


# ---------- L6: empty series ----------

def check_empty_series(chart: dict[str, Any], findings: list[dict[str, Any]]) -> None:
    series = (chart.get("option") or {}).get("series") or []
    if not isinstance(series, list):
        return
    view_type = chart.get("viewType")
    for index, entry in enumerate(series):
        if not isinstance(entry, dict):
            continue
        data = entry.get("data")
        empty_reason = None
        if data is None:
            empty_reason = "data is null"
        elif isinstance(data, list) and len(data) == 0:
            empty_reason = "data is empty"
        elif isinstance(data, list) and all(v == 0 or v is None for v in data):
            empty_reason = "all values are zero/null"
        if empty_reason and view_type != "table":
            findings.append(_finding(
                "L6", SEVERITY_WARN, chart["id"],
                f"series[{index}] ({entry.get('type', '?')}) {empty_reason}.",
                "remove the series or populate it with real values.",
                path=f"option.series[{index}].data",
            ))


# ---------- L7: heatmap density ----------

def check_heatmap_density(chart: dict[str, Any], findings: list[dict[str, Any]]) -> None:
    info = _heatmap_axis_order(chart)
    if not info:
        return
    x_data, y_data, cells = info
    cell_count = len(x_data) * len(y_data)
    if cell_count > 200:
        findings.append(_finding(
            "L7", SEVERITY_WARN, chart["id"],
            f"heatmap has {cell_count} cells ({len(x_data)} × {len(y_data)}); "
            "rendering and readability degrade past 200.",
            "trim the breakdown dimension, take the top N, or split into a small-multiples grid.",
        ))


# ---------- L8: table close to overflow ----------

def check_table_close_to_overflow(chart: dict[str, Any], block_layouts: dict[str, dict[str, int]],
                                   findings: list[dict[str, Any]]) -> None:
    if chart.get("viewType") != "table":
        return
    layout = block_layouts.get(chart.get("blockId", ""))
    if not layout or "h" not in layout:
        return
    grid_h = int(layout["h"])
    option = chart.get("option") or {}
    if option.get("pagination"):
        return
    rows = option.get("rows")
    if not isinstance(rows, list) or not rows:
        return
    cap = max_table_rows_for_height(grid_h)
    if cap == 0:
        return
    # L8 only fires when the table currently fits. R2 (validate_report.py)
    # is the strict failure for already-overflowing tables; L8 is the leading
    # warning that lets the author pre-empt overflow.
    if len(rows) > cap:
        return
    threshold = max(3, int(cap * 0.75))
    if len(rows) > threshold:
        findings.append(_finding(
            "L8", SEVERITY_WARN, chart["id"],
            f"table has {len(rows)} rows in gs-h={grid_h} (cap={cap}); "
            "currently fits but a small content change will introduce a scrollbar.",
            "consider raising gs-h, shrinking rows, or setting option.pagination=true.",
        ))


# ---------- L9 / L10: layout density ----------

def check_layout_density(blocks: list[dict[str, Any]], charts: list[dict[str, Any]],
                          findings: list[dict[str, Any]]) -> None:
    kpi_count = sum(1 for block in blocks if block.get("kind") == "kpi")
    if kpi_count > 4:
        findings.append(_finding(
            "L9", SEVERITY_WARN, None,
            f"{kpi_count} KPI blocks in the report; readers struggle to scan more than four.",
            "split into rows of ≤ 4 or move some KPIs into drill-down sections.",
        ))
    if len(charts) > 15:
        findings.append(_finding(
            "L10", SEVERITY_WARN, None,
            f"{len(charts)} charts in a single report; the page becomes a wall of charts.",
            "split into multiple pages or group them under navigation tabs.",
        ))


# ---------- L11: legend overflow risk ----------

def check_legend_overflow(chart: dict[str, Any], findings: list[dict[str, Any]]) -> None:
    option = chart.get("option") or {}
    legend = option.get("legend")
    if not isinstance(legend, dict):
        return
    data = legend.get("data")
    if isinstance(data, list) and len(data) > 8:
        findings.append(_finding(
            "L11", SEVERITY_WARN, chart["id"],
            f"legend lists {len(data)} entries; horizontal layout breaks at ~6-8.",
            "consolidate small slices into 'Other', or use legend.type='scroll'.",
        ))


# ---------- L12: duplicate columns ----------

def check_duplicate_columns(datasets: list[dict[str, Any]], charts: list[dict[str, Any]],
                             findings: list[dict[str, Any]]) -> None:
    def column_name(column: Any) -> str:
        return str(column.get("name", "")) if isinstance(column, dict) else str(column)
    for dataset in datasets:
        columns = dataset.get("columns") or []
        seen = set()
        for column in columns:
            name = column_name(column)
            if name in seen:
                findings.append(_finding(
                    "L12", SEVERITY_WARN, None,
                    f"dataset {dataset.get('id')} has duplicate column '{name}'.",
                    "rename to make every column unique within the dataset.",
                ))
                break
            seen.add(name)
    for chart in charts:
        editor = chart.get("editor") or {}
        data = editor.get("data") or {}
        columns = data.get("columns") or []
        seen: set[str] = set()
        for column in columns:
            name = column_name(column)
            if name in seen:
                findings.append(_finding(
                    "L12", SEVERITY_WARN, chart["id"],
                    f"editor.data has duplicate column '{name}'.",
                    "rename so dimension/measures can be unambiguously referenced.",
                ))
                break
            seen.add(name)


# ---------- L13: single-colour palette ----------

def check_single_colour_palette(chart: dict[str, Any], findings: list[dict[str, Any]]) -> None:
    option = chart.get("option") or {}
    series = option.get("series") or []
    if not series:
        return
    colors: list[str] = []
    for entry in series:
        if not isinstance(entry, dict):
            continue
        style = entry.get("itemStyle") or {}
        if isinstance(style, dict):
            color = style.get("color")
            if isinstance(color, str):
                colors.append(color)
            elif isinstance(color, dict):
                colors.append(json.dumps(color, sort_keys=True))
    palette = option.get("color")
    if isinstance(palette, list) and len(palette) == 1:
        findings.append(_finding(
            "L13", SEVERITY_INFO, chart["id"],
            "chart option.color has only one entry; series will share it.",
            "use a design-pack palette or extend option.color with a few distinct hues.",
        ))
    elif len(colors) > 1 and len(set(colors)) == 1:
        # The abandoned half of this rule, now wired up: per-series itemStyle
        # can pin every series to one hue without option.color saying so —
        # exactly the "all series read as one measure" failure L13 exists for.
        findings.append(_finding(
            "L13", SEVERITY_INFO, chart["id"],
            "every series pins the same itemStyle.color; the chart reads as a single measure.",
            "drop the per-series color and let the palette differentiate series.",
        ))


# ---------- L14: currency/percent-like measures without measureTypes ----------

# Heuristic column-name signals. Advisory only; measureTypes remains optional.
# 价族（2026-10-13 批）先于金额族判定——单价/售价应声明 ``price``（Flint Price
# intensive：缺省 average、不可加），而不是可加的 currency。
_PRICE_HINTS = ("售价", "单价", "价格", "标价", "原价", "现价", "均价", "price")
_CURRENCY_HINTS = ("金额", "收入", "成本", "利润", "额", "费")
_PERCENT_HINTS = ("率", "占比", "比例", "百分比")


def check_measure_types_declared(chart: dict[str, Any], findings: list[dict[str, Any]]) -> None:
    data = (chart.get("editor") or {}).get("data") or {}
    measures = data.get("measures") or []
    measure_types = data.get("measureTypes") or {}
    for measure in measures:
        if measure in measure_types:
            continue
        if any(hint in measure for hint in _PRICE_HINTS):
            findings.append(_finding(
                "L14", SEVERITY_WARN, chart["id"],
                f"measure '{measure}' looks like a unit price but editor.data.measureTypes does not "
                f"declare it; the column gets no ¥ formatting and no non-additive protection in the editor.",
                "add `\"measureTypes\": { \"" + measure + "\": \"price\" }` to editor.data "
                "so the runtime formats with ¥ and defaults to average (prices are not additive).",
                path=f"editor.data.measureTypes",
            ))
        elif any(hint in measure for hint in _CURRENCY_HINTS):
            findings.append(_finding(
                "L14", SEVERITY_WARN, chart["id"],
                f"measure '{measure}' looks like a currency value but editor.data.measureTypes does not "
                f"declare it; the axis/tooltip will show raw numbers.",
                "add `\"measureTypes\": { \"" + measure + "\": \"currency\" }` to editor.data "
                "so the runtime auto-formats with ¥.",
                path=f"editor.data.measureTypes",
            ))
        elif any(hint in measure for hint in _PERCENT_HINTS):
            findings.append(_finding(
                "L14", SEVERITY_WARN, chart["id"],
                f"measure '{measure}' looks like a percentage but editor.data.measureTypes does not "
                f"declare it; the axis/tooltip will show raw numbers.",
                "add `\"measureTypes\": { \"" + measure + "\": \"percent\" }` to editor.data "
                "so the runtime auto-formats with %.",
                path=f"editor.data.measureTypes",
            ))


# ---------- L15: scatter discrimination ----------

def check_scatter_discrimination(chart: dict[str, Any], findings: list[dict[str, Any]]) -> None:
    """Scatter points collapse when an axis has almost no spread."""
    if chart.get("viewType") != "scatter":
        return
    data = (chart.get("editor") or {}).get("data") or {}
    columns = data.get("columns") or []
    measures = data.get("measures") or []
    rows = data.get("rows") or []
    if len(measures) < 2 or not rows:
        return
    axis_names = {"X": measures[0], "Y": measures[1]}
    for axis, measure in axis_names.items():
        if measure not in columns:
            continue
        col = columns.index(measure)
        values = [row[col] for row in rows if isinstance(row[col], (int, float))]
        if len(values) < 3:
            continue
        lo, hi = min(values), max(values)
        if lo <= 0:
            continue
        span = (hi - lo) / hi
        if span < 0.1:
            findings.append(_finding(
                "L15", SEVERITY_WARN, chart["id"],
                f"scatter {axis} axis ('{measure}') has almost no spread "
                f"(max={hi:.4g}, min={lo:.4g}, span {span:.0%}); the points will overlap "
                f"into a line and the chart reads as empty.",
                "pick a dimension with real spread for this axis, or use a bar chart "
                "when only a few categories are compared.",
                path=f"editor.data.measures[{measures.index(measure)}]",
            ))


# ---------- L16: heatmap colour range ----------

def check_heatmap_range(chart: dict[str, Any], findings: list[dict[str, Any]]) -> None:
    """A heatmap is unreadable when the colour ramp is wasted on empty range."""
    if chart.get("viewType") != "heatmap":
        return
    data = (chart.get("editor") or {}).get("data") or {}
    columns = data.get("columns") or []
    measures = data.get("measures") or []
    rows = data.get("rows") or []
    if not measures or not rows or measures[0] not in columns:
        return
    col = columns.index(measures[0])
    values = [row[col] for row in rows if isinstance(row[col], (int, float))]
    if len(values) < 4:
        return
    lo, hi = min(values), max(values)
    if hi <= 0:
        return
    span = (hi - lo) / hi
    option = chart.get("option") or {}
    vm = option.get("visualMap") or {}
    vm_min = vm.get("min") if isinstance(vm, dict) else None
    vm_max = vm.get("max") if isinstance(vm, dict) else None
    tight = (isinstance(vm_min, (int, float)) and isinstance(vm_max, (int, float))
             and (vm_max - vm_min) < (hi - lo) * 1.25 + 1e-9)
    diverging = isinstance(vm.get("middle"), (int, float))
    if span < 0.3 and not diverging:
        findings.append(_finding(
            "L16", SEVERITY_WARN, chart["id"],
            f"heatmap values span only {span:.0%} of their max "
            f"({lo:.3g}~{hi:.3g}); a sequential ramp compresses these cells into "
            "almost identical shades, so the grid reads as blank.",
            "use a diverging palette with visualMap.middle centred on a benchmark "
            "(e.g. the overall mean) so deviations stand out, or show cell labels.",
            path="option.visualMap",
        ))
    if (isinstance(vm_min, (int, float)) and vm_min == 0
            and isinstance(vm_max, (int, float)) and lo > vm_max * 0.3):
        findings.append(_finding(
            "L16", SEVERITY_WARN, chart["id"],
            f"heatmap visualMap.min=0 but the lowest cell is {lo:.3g} "
            f"({(lo / vm_max):.0%} of max); the lower half of the ramp is empty.",
            "raise visualMap.min to the data floor so the ramp covers real values.",
            path="option.visualMap.min",
        ))


# ---------- L17: semantic aggregation/stacking validity (Flint T1 behaviour) ----------

# 不可加判据（2026-10-13 扩）：字符串形态 percent/rate/price + object 形态
# type/semantic（price = Flint Price intensive；intensive 语义标签同闸）。
_INTENSIVE_LABELS = ("Percentage", "PercentageChange", "Price", "Score",
                     "Temperature", "Sentiment", "Correlation")


def _measure_non_additive_flag(mt: Any) -> bool:
    if isinstance(mt, str):
        return mt in ("percent", "rate", "price")
    if isinstance(mt, dict):
        if mt.get("type") in ("percent", "price"):
            return True
        return mt.get("semantic") in _INTENSIVE_LABELS
    return False


def check_measure_behaviour(chart, findings):
    """Percent/rate/price measures must not be summed or stacked (semantically wrong)."""
    vt = chart.get("viewType")
    data = (chart.get("editor") or {}).get("data") or {}
    measure_types = data.get("measureTypes") or {}
    measures = data.get("measures") or []
    aggregation = data.get("aggregation") or "sum"
    non_additive = [m for m in measures if _measure_non_additive_flag(measure_types.get(m))]
    if not non_additive:
        return
    if aggregation == "sum" and vt not in ("table",):
        findings.append(_finding(
            "L17", SEVERITY_WARN, chart["id"],
            f"aggregation is sum for rate/percent/price measures {non_additive}; "
            "summing rates or unit prices across items is semantically meaningless.",
            "use average for rate/percent/price measures (Flint intensive → average).",
            path="editor.data.aggregation",
        ))
    if vt == "stackedBar":
        findings.append(_finding(
            "L17", SEVERITY_WARN, chart["id"],
            f"stacked bar with rate/percent/price measures {non_additive}; stacking "
            "non-additive values misrepresents the total.",
            "use a grouped bar or a plain bar instead.",
            path="viewType",
        ))


# ---------- L20: price averaged across a mix of incomparable slices ----------

# 品类族列名信号：存在这类列而价格指标没有按它分组时，「国家均价/品牌均价」
# 实际是品类构成差异（未加权混合均值），不是价格水平（2026-10-13 批，竞品
# 街价报告 Top15 国家均价病灶的 advisory）。
_CATEGORY_HINT_RE = re.compile(r"品类|类别|分类|品类|产品线|产业|category|family|industry", re.I)


def check_price_mix_average(chart, findings):
    """Price/intensive measure averaged on a dimension while a category column
    exists but is not a grouping — the average is mix-driven, not a price level."""
    if chart.get("viewType") in ("table", "detail"):
        return
    data = (chart.get("editor") or {}).get("data") or {}
    measure_types = data.get("measureTypes") or {}
    measures = data.get("measures") or []
    price_measures = [m for m in measures
                      if (isinstance(measure_types.get(m), dict)
                          and (measure_types[m].get("type") == "price"
                               or measure_types[m].get("semantic") == "Price"))]
    if not price_measures:
        return
    columns = data.get("columns") or []
    cat_cols = [c for c in columns if _CATEGORY_HINT_RE.search(str(c))]
    if not cat_cols:
        return
    grouped = {data.get("dimension")}
    grouped.update(data.get("dimensions") or [])
    bd = ((data.get("roles") or {}).get("breakdown"))
    if bd:
        grouped.add(bd)
    if any(c in grouped for c in cat_cols):
        return
    findings.append(_finding(
        "L20", SEVERITY_WARN, chart["id"],
        f"price measures {price_measures} are averaged over a slice that ignores the "
        f"category column(s) {cat_cols}; the average mixes incomparable product "
        "categories (a $21 band and a $3,000 foldable) and tracks mix, not price level.",
        "group by the category column, compare within one category, or switch to "
        "discount rate / price-band / median views for cross-category reads.",
        path="editor.data.dimension",
    ))


# ---------- orchestration ----------

class _LintHTMLParser(HTMLParser):
    """Capture block-title presence per data-report-block-id.

    The block container carries ``data-report-block-id``; every descendant
    element is associated with that block until the container closes. We track
    a stack of block ids rather than only the outermost.
    """

    _VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
                  "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.block_text_attrs: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self._stack: list[tuple[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        block_id = values.get("data-report-block-id")
        current = block_id or (self._stack[-1][1] if self._stack else None)
        if current:
            self.block_text_attrs.setdefault(current, []).append((tag, values))
        if tag not in self._VOID_TAGS:
            self._stack.append((tag, current))

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                del self._stack[index:]
                return


def lint_html(html: str, packaged: bool = False) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    matches = MANIFEST_RE.findall(html)
    if len(matches) != 1:
        raise ReportContractError("HTML must contain exactly one application/json #ai-report-manifest script")
    manifest = extract_manifest(html)
    parser = ContractParser()
    parser.feed(html)
    title_parser = _LintHTMLParser()
    title_parser.feed(html)

    findings: list[dict[str, Any]] = []
    blocks = manifest.get("blocks", [])
    charts = manifest.get("charts", [])
    datasets = manifest.get("datasets", [])

    chart_checkers = [
        check_heatmap_sort,
        check_tooltip,
        check_legend_size,
        check_axis_font_size,
        check_empty_series,
        check_heatmap_density,
        check_table_close_to_overflow,
        check_legend_overflow,
        check_single_colour_palette,
        check_measure_types_declared,
        check_scatter_discrimination,
        check_heatmap_range,
        check_measure_behaviour,
        check_price_mix_average,
    ]
    for chart in charts:
        for checker in chart_checkers:
            if checker in (check_table_close_to_overflow,):
                checker(chart, parser.block_layouts, findings)
            else:
                checker(chart, findings)
        check_block_title(chart, title_parser, findings)
    check_layout_density(blocks, charts, findings)
    check_duplicate_columns(datasets, charts, findings)
    check_text_blank(html, findings)
    check_chart_theme(manifest, findings)

    summary = {
        "reportId": manifest.get("reportId"),
        "warnings": sum(1 for f in findings if f["severity"] == SEVERITY_WARN),
        "info": sum(1 for f in findings if f["severity"] == SEVERITY_INFO),
        "total": len(findings),
    }
    return findings, summary


# ---------- CLI ----------

def _print_text(findings: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    print(f"Lint summary: {summary['warnings']} warning(s), {summary['info']} info, total {summary['total']}")
    if not findings:
        print("  No issues found.")
        return
    grouped: dict[str, list[dict[str, Any]]] = {}
    for finding in findings:
        key = finding.get("chartId") or "<report>"
        grouped.setdefault(key, []).append(finding)
    for key in sorted(grouped):
        print(f"\n[{key}]")
        for finding in grouped[key]:
            tag = finding["severity"].upper()
            print(f"  [{tag}] {finding['rule']}: {finding['message']}")
            if finding.get("fix"):
                print(f"         fix: {finding['fix']}")


def _print_json(findings: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    for finding in findings:
        print(json.dumps(finding, ensure_ascii=False))
    print(json.dumps({"__summary__": summary}, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser(description="Warning-level lint for a Live Report Master")
    parser.add_argument("--html", required=True, help="Path to draft or packaged report")
    parser.add_argument("--packaged", action="store_true")
    parser.add_argument("--json", action="store_true", help="emit NDJSON")
    parser.add_argument("--strict", action="store_true", help="exit 2 on any warning")
    args = parser.parse_args()
    try:
        html_text = Path(args.html).read_text(encoding="utf-8")
        findings, summary = lint_html(html_text, packaged=args.packaged)
    except ReportContractError as error:
        print(f"Unable to lint: {error}", file=sys.stderr)
        return 1
    if args.json:
        _print_json(findings, summary)
    else:
        _print_text(findings, summary)
    has_warnings = summary["warnings"] > 0
    if has_warnings and args.strict:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
