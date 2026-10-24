#!/usr/bin/env python3
"""Backend-agnostic layout solver for chart blocks.

Flint's computeLayout, scaled to what this skill needs. Instead of hardcoded
per-viewType heights, a chart's height/aspect is derived from how many discrete
categories it draws (bars/lines) and whether its axis is continuous (scatter).

The solver is pure and returns plain numbers, so both the compiler
(``spec_to_report._default_h``/``pack_layout``) and the runtime density gate can
share the same rules without importing renderer code. This keeps singlefile and
server layouts identical (they already share ``render_grid_html``).

Key ideas:
  - **Discrete axes** (bar/line/pie/funnel): height grows with cardinality so a
    top-N/list stays legible; the base height scales with a per-row step.
  - **Continuous axes** (scatter): height is fixed by aspect; density is handled
    by the runtime dataZoom gate, not by pushing height.
  - **Universal grid step**: a single ``GRID_ROW`` constant governs the discrete
    growth so the validator's capacity math (R2) stays in sync.
"""
from __future__ import annotations

from typing import Any

# A single grid row in the 12-col grid (in "row units" used by GridStack).
# The R2 capacity formula in validate_report mirrors this: a table of N rows
# needs height H where (H*48 - 60) // 28 >= N.
# ROW_PX must match the runtime GridStack cellHeight (editor-runtime.js) and
# CELL_HEIGHT_PX in validate_report.py — the grid actually renders at this px
# per row, so a mismatch makes cards airier than the capacity math assumes.
GRID_ROW = 5          # default chart block height in rows
ROW_PX = 48           # px per grid row (cellHeight)
MARGIN_PX = 8         # grid margin between rows (editor-runtime GridStack margin)
ROW_PITCH_PX = ROW_PX + MARGIN_PX  # px per row incl. the gap below it
HEADER_PX = 88        # fixed chrome above the table body: card chrome 60 (validate_report.CHROME_PX)
                      # + table header row 28 (validate_report.HEADER_HEIGHT_PX). Must cover BOTH so
                      # ceil((n*28+88)/48) always satisfies the R2 capacity formula
                      # (h*48-60)//28 >= n; at 78 the remainder could fall one row
                      # short and n=9/16/21-row tables failed validate_report.
TABLE_ROW_PX = 28     # px per table row (matches render_grid_html body row height)
TABLE_HEADER_PX = 28  # px per thead row (validate_report.HEADER_HEIGHT_PX 同源；XU-01 行数换算的表头项)

# Chrome-aware height (Step 1 of row-pattern layout): the card chrome eats a
# fixed slice (padding 28+12, block title ~32, ds-chip row ~30, legend ~28) and
# an optional annotation caption (~22px per wrapped line); the plot keeps what
# is left. Heights are computed so the plot never drops below PLOT_MIN_PX.
# (padding top 16→28 to give the drag strip room: +12px chrome.)
CHROME_CHART_PX = 120
ANNOTATION_LINE_PX = 22
PLOT_MIN_PX = {"bar": 200, "stackedBar": 210, "line": 220, "area": 220,
               "pie": 220, "donut": 220, "funnel": 220, "scatter": 240}
# Annotation caption chars-per-line by card width (13px CJK ≈ card_px/13).
_ANNOTATION_CPL = {12: 70, 8: 46, 6: 34, 4: 22}

# Discrete growth: how many rows to add per K categories beyond the base.
_EXTRA_ROWS_PER_K = {"bar": 1, "line": 1, "area": 1, "funnel": 1, "pie": 1, "donut": 1}
_MAX_DISCRETE_ROWS = 12
# A table card is a dense row grid: the default/auto height must be tall enough
# that a 明细/聚合 table is actually usable (several rows readable) instead of a
# short strip forced to scroll. _MAX_TABLE_ROWS caps the auto height so a 24-row
# crosstab fits (max_table_rows_for_height(_MAX_TABLE_ROWS) == 24 with the shared
# R2 formula), while an author seeking more uses layout.h / pagination / topK.
_MAX_TABLE_ROWS = 16
_BASE_ROWS = {"bar": 5, "stackedBar": 5, "line": 5, "area": 5,
              "pie": 6, "donut": 6, "funnel": 6, "scatter": 5, "table": 8, "kpi": 3}


def _kind(view_type: str) -> str:
    return "discrete" if view_type in ("bar", "stackedBar", "line", "area", "pie", "donut", "funnel") \
        else "continuous" if view_type == "scatter" else "table"


def _cardinality(chart: dict[str, Any]) -> int:
    """Distinct-category count of a chart's dimension column (0 when unknown —
    e.g. server mode strips rows from the manifest)."""
    data = chart.get("data") or {}
    rows = data.get("rows") or []
    cols = data.get("columns") or []
    dimension = chart.get("dimension") or ""
    di = cols.index(dimension) if dimension in cols else -1
    if di < 0:
        return 0
    db = chart.get("dimensionBucket") or {}
    bucket = db.get("bucket")
    seen = set()
    if bucket and bucket != "raw":
        for r in rows:
            try:
                seen.add(_bucket_label(r[di], bucket) or str(r[di]))
            except (IndexError, TypeError, ValueError):
                continue
    else:
        for r in rows:
            try:
                seen.add(r[di])
            except (IndexError, TypeError):
                continue
    return len(seen)


def _bucket_label(value: Any, bucket: str) -> str | None:
    """Minimal bucketed-label helper for cardinality sizing — delegates to the
    SHARED parser (``date_parse``)。旧实现自 second 套正则且 `datetime.date(2025,13,1)`
    对脏值（202513）抛 ValueError 未捕获，整个编译崩掉；date_parse 对同一值
    安全返回 None。"""
    if value is None or value == "":
        return None
    import date_parse
    p = date_parse.parts(value)
    if not p:
        return None
    y, mo, d = p
    if bucket == "year":
        return f"{y:04d}"
    if bucket == "month":
        return f"{y:04d}-{mo:02d}"
    if bucket == "day":
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return str(value).strip()


def bounded_table_rows(max_px: int, chrome_px: int = 60, row_px: int = ROW_PX) -> int:
    """WP-7 有界高度：gs-h 预留 = ceil((maxPx + chrome) / ROW_PX)，按**帽**算、不按
    内容算（内容在 wrap 内滚动，预留恒定 —— 帽不随内容抖动）。chrome_px 缺省 60 与
    validate_report.CHROME_PX 同源（R2 新不变量 gs-h*CELL_HEIGHT_PX ≥ maxPx+CHROME_PX
    即「预留 ≥ 帽」）。运行时镜像 = editor-runtime.js boundedReserveRows（同算式，
    parity 由 tests/test_bounded_table.{cjs,py} 双端锁）。"""
    import math
    return max(1, math.ceil((int(max_px) + int(chrome_px)) / row_px))


def max_body_rows_px(n: int, row_px: int = TABLE_ROW_PX, head_px: int = TABLE_HEADER_PX) -> int:
    """XU-01 行数口径换算：maxBodyRows → px = N×行高 + thead 高。缺省行高/表头与
    validate_report.ROW_HEIGHT_PX / HEADER_HEIGHT_PX 同源（28/28）。运行时镜像 =
    editor-runtime.js boundedRowsMaxPx（TABLE_ROW_HEIGHT_PX/TABLE_HEADER_HEIGHT_PX），
    同一张期望表由 tests/test_bounded_table.{cjs,py} 双端锁。真实渲染行高随密度档
    （32/40/48），运行时 autoFitTableCards 用实测 tbody tr 高度校正重写 max-height
    （自愈）；编译侧恒用缺省行高，保证 parity 确定。falsy/非正参数落缺省（与 JS
    boundedRowsMaxPx 的 `Number(rowPx)>0` 判定逐位一致）。"""
    rp = int(row_px) if row_px and int(row_px) > 0 else TABLE_ROW_PX
    hp = int(head_px) if head_px and int(head_px) > 0 else TABLE_HEADER_PX
    return int(n) * rp + hp


def _bounded_max_px(height_mode: Any) -> int | None:
    """chart.table.heightMode 三态（WP-3 冻结形状：grow|fixed|{'bounded':{'maxPx'}}）
    → bounded 档的 maxPx；非 bounded/形状非法一律 None（与 validate_request 同口径，
    未知值收敛缺省）。XU-01 行数口径：{'bounded':{'maxBodyRows': int 5..500}} 按
    max_body_rows_px 换算；与 maxPx 同给时**行数优先**（运行时同规则并 warn 一次，
    请求层 validate_request 对双键直接报错——这里兜的是手写 manifest）。
    运行时镜像 = editor-runtime.js heightModeOf。"""
    if isinstance(height_mode, dict):
        b = height_mode.get("bounded")
        if isinstance(b, dict):
            n = b.get("maxBodyRows")
            if isinstance(n, int) and not isinstance(n, bool) and 5 <= n <= 500:
                return max_body_rows_px(n)
            mx = b.get("maxPx")
            if isinstance(mx, int) and not isinstance(mx, bool) and 200 <= mx <= 4000:
                return mx
    return None


def _table_height_mode_px(chart: dict[str, Any]) -> int | None:
    """读取请求层 chart.table（编译流）或 editor.table（manifest 图表兜底，服务器
    形状同 _chart_dimension 的双形状策略）声明的 bounded maxPx。"""
    tbl = chart.get("table")
    if not isinstance(tbl, dict):
        editor = chart.get("editor")
        tbl = editor.get("table") if isinstance(editor, dict) else None
    if not isinstance(tbl, dict):
        return None
    return _bounded_max_px(tbl.get("heightMode"))


def solve_layout(chart: dict[str, Any], width: int | None = None) -> dict[str, Any]:
    """Compute a deterministic layout hint for a chart block.

    Returns ``{h, w, kind, cardinality, baseH, overflow, dataZoom, step}`` where:
      - ``h``      — suggested grid height (rows); chrome-aware (title + chip +
                     legend + annotation caption are accounted for) so the plot
                     never drops below ``PLOT_MIN_PX``, then grows with discrete
                     cardinality.
      - ``w``      — suggested grid width (columns, 1..12).
      - ``kind``   — 'discrete' | 'continuous' | 'table'.
      - ``dataZoom``— True when a discrete chart exceeds the density budget and
                     should render a slider/inside zoom instead of growing forever.
      - ``overflow``— True when cardinality exceeds what the block can fit even
                     with dataZoom. The compiler may split/drop to keep legible.
    """
    import math
    view_type = chart.get("viewType") or "bar"
    # 文本伪卡（free texts，2026-09-28「全部放开」批）：高度由编译侧按显示宽度
    # 行数预算好（_textH），排版引擎只搬运不猜测——行数计量真源在 spec_to_report
    # 的 _text_band_h（CJK/全角=2 半宽单位行模型），这里没有正文字形信息。
    if view_type == "text":
        h = int(chart.get("_textH") or 3)
        return {"h": h, "w": 12, "kind": "text", "cardinality": 0, "baseH": h,
                "overflow": False, "dataZoom": False, "step": None}
    data = chart.get("data") or {}
    rows = data.get("rows") or []
    cardinality = _cardinality(chart)
    # If no rows/cardinality (server defers to aggregate), fall back to a stable default.
    kind = _kind(view_type)
    base = _BASE_ROWS.get(view_type, GRID_ROW)
    h = base
    step = None
    overflow = False
    data_zoom = False

    if kind == "table":
        n = len(rows)
        # WP-7 有界高度：heightMode={bounded:{maxPx}} → gs-h 预留按**帽**算
        # （bounded_table_rows，内容在 wrap 内滚动、预留恒定）；行数 vs grid_h 的
        # R2 容量对 bounded 豁免（内部滚动，同 pagination 理由），「预留 ≥ 帽」
        # 不变量由 validate_report.check_table_fits_container 把关。
        bounded_px = _table_height_mode_px(chart)
        if bounded_px:
            h = max(base, bounded_table_rows(bounded_px))
            return {"h": h, "w": 12 if n else 6, "kind": kind, "cardinality": n,
                    "baseH": base, "overflow": False, "dataZoom": False, "step": table_step(n),
                    "boundedPx": bounded_px}
        # R2 capacity (shared formula): max_rows_for_height(h) = (h*48 - 88) // 28.
        # Auto-size the card to fit the authored rows when known; when no rows are
        # present (server mode strips rows, n = 0) fall back to the taller base so
        # the table is not a short strip. Cap at _MAX_TABLE_ROWS so a tall crosstab
        # still fits without pushing the whole page; beyond that a scrollbar/paging
        # (overflow) is the host's job rather than growing the page forever.
        h = math.ceil((n * TABLE_ROW_PX + HEADER_PX) / ROW_PX) if n else base
        h = max(base, min(_MAX_TABLE_ROWS, h))
        overflow = h >= _MAX_TABLE_ROWS
        data_zoom = overflow
        return {"h": h, "w": 12 if n else 6, "kind": kind, "cardinality": n,
                "baseH": base, "overflow": overflow, "dataZoom": data_zoom, "step": table_step(n)}

    if kind == "continuous":
        plot_min = PLOT_MIN_PX.get(view_type, 240)
        h = math.ceil((CHROME_CHART_PX + plot_min) / ROW_PITCH_PX)
        h += _annotation_rows(chart, width or 6)
        data_zoom = False
        return {"h": h, "w": 6, "kind": kind, "cardinality": cardinality, "baseH": base,
                "overflow": False, "dataZoom": data_zoom, "step": None}

    # Discrete: chrome-aware base (plot never below PLOT_MIN_PX) + annotation
    # caption rows, then grow with cardinality, capped; beyond the cap zoom.
    plot_min = PLOT_MIN_PX.get(view_type, 200)
    h = math.ceil((CHROME_CHART_PX + plot_min) / ROW_PITCH_PX)
    h += _annotation_rows(chart, width or 6)
    extra = 0
    if cardinality > 0:
        per = _EXTRA_ROWS_PER_K.get(view_type, 0)
        # One extra row per 10 categories beyond the first block, capped.
        extra = min(max(0, _MAX_DISCRETE_ROWS - h), per * max(0, (cardinality - 10) // 10))
        if cardinality <= 10:
            extra = 0
    h = h + extra
    # Density budget: >18 categories is unreadable without zoom (matches the
    # runtime slider threshold); beyond that the block stops growing and zooms.
    if cardinality > 18:
        data_zoom = True
        overflow = cardinality > 40
    step = 10 if cardinality <= 10 else (18 if cardinality <= 18 else None)
    return {"h": h, "w": 6, "kind": kind, "cardinality": cardinality, "baseH": base,
            "overflow": overflow, "dataZoom": data_zoom, "step": step}


def _annotation_rows(chart: dict[str, Any], width: int) -> int:
    """In-card annotation no longer renders: annotations are separate text blocks
    created by the data card「添加批注」dropdown. So charts reserve no caption rows."""
    return 0


def table_step(n: int) -> int | None:
    """Row step hint for a table: 1 pageable group per 20 rows."""
    return 20 if n > 20 else None


def _chart_dimension(chart: dict[str, Any]) -> str:
    """Resolve a chart's dimension name.

    In server mode the manifest chart carries the dimension under
    ``editor.data`` (rows are stripped); in singlefile it also lives on the top
    level. Try both so width/height rules (e.g. a time-series trend) agree."""
    d = chart.get("dimension")
    if d:
        return str(d)
    ed = chart.get("editor") or {}
    d = (ed.get("data") or {}).get("dimension")
    return str(d or "")


def _is_time_series(chart: dict[str, Any]) -> bool:
    """A line/area chart over a chronological dimension is a trend; it wants a
    full-width stage (12) so the time axis has room to breathe and the series is
    legible, rather than being squeezed into a half-width card."""
    view_type = chart.get("viewType") or "bar"
    if view_type not in ("line", "area"):
        return False
    dim = _chart_dimension(chart)
    # Temporal name keyword wins (日期/时间/year/month/date).
    if any(k in str(dim).lower() for k in ("日期", "时间", "year", "month", "date", "day")):
        return True
    # Otherwise a date-like VALUE pattern (yyyy / yyyy-mm / yyyy年m月) on the
    # dimension column signals a chronology (YEARMONTH like 202501 is temporal).
    data = chart.get("data") or {}
    cols = data.get("columns") or []
    rows = data.get("rows") or []
    if dim and dim in cols and rows:
        ci = cols.index(dim)
        vals = [r[ci] for r in rows if str(r[ci]).strip()]
        if vals:
            # Shared temporal parser (scripts/date_parse.py): counts every
            # supported spelling — 2026/9/5, 2026.9.5, 2026年9月5日, 20260905,
            # 202501.0 — so a slash-dated trend is still sized as a full-width
            # time series instead of a half-width card.
            from date_parse import grain as _date_grain
            date_like = sum(1 for v in vals if _date_grain(v) is not None)
            if date_like / len(vals) > 0.6:
                return True
    return False


def pack_width(chart: dict[str, Any]) -> int:
    """Default width for a block: tables/scatter and time-series trends are wide;
    the rest half. A crosstab (breakdown) wants 8; KPI cards are a quarter."""
    view_type = chart.get("viewType") or "bar"
    if view_type in ("table", "scatter"):
        return 12
    # 文本伪卡恒整行（free texts）：半宽文本卡不走 compose_rows 配对——宽度镜像
    # 陷阱（compose 宽 vs pack_width 重算宽不一致）不值得为「文字并排」开放，
    # 那是编辑器拖拽的领地。
    if view_type == "text":
        return 12
    if view_type in ("kpi",):
        return 3
    # A time-series trend (line/area over a chronological dimension) is a
    # headline view — give it the full stage (12) instead of a half-width.
    if _is_time_series(chart):
        return 12
    # A crosstab (breakdown) wants more room than a plain single-dimension chart.
    if (chart.get("roles") or {}).get("breakdown"):
        return 8
    return 6


# ---------------------------------------------------------------------------
# Row patterns (Step 2): compose charts into full 12-col rows by semantics
# instead of first-fitting fixed-width cards (which left 4/6-col gaps).
# ---------------------------------------------------------------------------

# A pie/donut/funnel with this many categories (or fewer) fits a narrow 4-col
# slot; more labels need a half-width card.
_SMALL_MAX_CARD = 4
# Three consecutive low-cardinality charts may share one row as a trio.
_TRIO_MAX_CARD = 6


def _series_count(chart: dict[str, Any]) -> int:
    """Effective series count: declared measures, or wide-format rows where each
    non-dimension column is a series (0 when unknown on the server path)."""
    measures = chart.get("measures") or []
    if measures:
        return len(measures)
    ed = chart.get("editor") or {}
    ed_measures = (ed.get("data") or {}).get("measures") or []
    if ed_measures:
        return len(ed_measures)
    data = chart.get("data") or {}
    cols = data.get("columns") or []
    dim = _chart_dimension(chart)
    if cols and dim and dim in cols:
        return max(1, len(cols) - 1)
    return 1


def width_class(chart: dict[str, Any]) -> str:
    """Row-pattern width class for a chart:
      - ``solo``  (12): table / scatter / time-series trend — needs the full stage.
      - ``wide``  (8):  crosstab (breakdown) or stacked bar with 3+ series.
      - ``small`` (4):  pie/donut/funnel with very few categories.
      - ``std``   (6):  everything else."""
    view_type = chart.get("viewType") or "bar"
    if view_type in ("table", "scatter", "kpi"):
        return "solo"
    if view_type == "text":
        return "solo"
    if _is_time_series(chart):
        return "solo"
    if (chart.get("roles") or {}).get("breakdown"):
        return "wide"
    if view_type == "stackedBar" and _series_count(chart) >= 3:
        return "wide"
    if view_type in ("pie", "donut", "funnel") and 0 < _cardinality(chart) <= _SMALL_MAX_CARD:
        return "small"
    return "std"


def compose_rows(charts: list[dict[str, Any]]) -> list[list[tuple[dict[str, Any], int]]]:
    """Compose charts into rows that each fill the 12-col grid.

    Deterministic, in author order. Every row sums to 12 columns:
      - solo                 → [12]
      - wide + small         → [8, 4]
      - wide + std           → [6, 6]   (the wide demotes; a half pair reads
                              better than an 8+4 where the partner overflows)
      - lone wide            → [12]     (promote: no gap left behind)
      - 3 consecutive low-cardinality std/small → [4, 4, 4] (trio)
      - std pairs            → [6, 6]
      - lone std/small       → [12]     (promote)
    A chart with an authored ``layout.w`` keeps it and takes its own row.
    """
    rows: list[list[tuple[dict[str, Any], int]]] = []
    i, n = 0, len(charts)

    def authored_w(c: dict[str, Any]) -> int | None:
        lay = c.get("layout") or {}
        return int(lay["w"]) if "w" in lay else None

    def effective_class(c: dict[str, Any]) -> str:
        # A width-only layout hint should still participate in row composition.
        # Standard slot widths map to the same semantic classes the solver would
        # otherwise infer; non-standard widths fall back to semantic inference.
        aw = authored_w(c)
        if aw == 12:
            return "solo"
        if aw == 8:
            return "wide"
        if aw == 4:
            return "small"
        if aw == 6:
            return "std"
        return width_class(c)

    while i < n:
        c = charts[i]
        cls = effective_class(c)
        if cls == "solo":
            rows.append([(c, 12)])
            i += 1
            continue
        if cls == "wide":
            nxt = charts[i + 1] if i + 1 < n else None
            nxt_cls = effective_class(nxt) if nxt is not None else None
            if nxt_cls == "small":
                rows.append([(c, 8), (nxt, 4)])
                i += 2
            elif nxt_cls == "std":
                rows.append([(c, 6), (nxt, 6)])
                i += 2
            else:
                rows.append([(c, 12)])
                i += 1
            continue
        if cls == "std" and i + 1 < n:
            nxt = charts[i + 1]
            if effective_class(nxt) == "wide":
                # A wide card appearing AFTER a standard card should pair as 6+6
                # instead of leaving the standard card to be promoted to 12 and
                # then promoting the wide card to 12 on the next row.
                rows.append([(c, 6), (nxt, 6)])
                i += 2
                continue
        # std/small run: gather consecutive combinable charts. A chart with an
        # explicit layout.w is combinable for pairing (its class already reflects
        # the authored width) but never demoted by the trio squeeze: the docstring
        # contract is "an authored layout.w keeps it", and [4,4,4] would silently
        # narrow an authored 6.
        run: list[dict[str, Any]] = []
        j = i
        while j < n and effective_class(charts[j]) in ("std", "small"):
            run.append(charts[j])
            j += 1
        while len(run) >= 3 and all(0 < _cardinality(c) <= _TRIO_MAX_CARD for c in run[:3]) \
                and not any(authored_w(c) for c in run[:3]):
            rows.append([(c, 4) for c in run[:3]])
            run = run[3:]
        while len(run) >= 2:
            rows.append([(run[0], 6), (run[1], 6)])
            run = run[2:]
        if run:
            rows.append([(run[0], 12)])
        i = j
    return rows
