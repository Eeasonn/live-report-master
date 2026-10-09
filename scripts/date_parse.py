#!/usr/bin/env python3
"""Single source of truth for temporal value parsing (compile side).

Both the static compiler (``spec_to_report``) and the local server
(``server/aggregator``) have to bucket/format a raw time column, and the browser
(``assets/runtime/chart-adapter.js``) has to re-bucket/re-sort the very same
values live. Because a real column may use almost any common date spelling
(``2026/9/5``, ``2026-9-5``, ``2026.9.5``, ``2026年9月5日``, ``20260905``,
``2026/09``, ``202501``, ``2026-09-05T00:00:00`` …) the parser must be shared
instead of re-derived per call site.

What goes wrong when it is not shared (a real company report):

* static sort degraded to string order — ``2026/6/1`` < ``2026/6/10`` < ``2026/6/2``
  so the compiled trend axis came out jumbled (``_time_sort_key``);
* SQLite ``strftime()`` cannot parse the slash form, so the server-side
  re-aggregation bucketed every group to NULL and the time axis rendered empty
  (``aggregator._bucket_expr``).

Mirrors ``assets/runtime/chart-adapter.js`` (``dateParts`` / ``parseDateValue`` /
``bucketLabel``) field-for-field; guarded by ``tests/test_date_parse.py``
(Python ↔ JS ↔ SQLite parity on one fixture).
"""
from __future__ import annotations

import datetime
import re
from typing import Any

# 2026-9-5 / 2026/9/5 / 2026.9.5 / 2026年9月5日 (+ optional time part)
_RE_SEPARATED = re.compile(
    r"^(\d{4})\s*[-/.年]\s*(\d{1,2})\s*"
    r"(?:[-/.月]\s*(\d{1,2})\s*[日号]?)?\s*[月日号]?\s*"   # 2026年9月 / 2026年9月5日
    r"(?:[T ]\s*\S.*)?$"                                      # optional time part
)
_RE_FLOAT_ZERO = re.compile(r"^(\d+)\.0+$")
# 季度标签（本模块自己产出的 YYYY-Qn，也接受 2025Q1 拼写）：解析回来才能对
# 已分桶的标签排序 / 判时间轴（静态 option 的维度值就是这种标签）。
_RE_QUARTER = re.compile(r"^(\d{4})\s*[-/]?\s*[Qq]\s*([1-4])$")
# 值级周字段（w35 / 2026-W35 / 2026年35周 / 第35周…）。周必须带**显式周标记**
# （w/W/周）——`2026-35` 这种 13–53 的第二段与 `2026-09` 月份写法无法区分，不认
# （用户数据的宽容优先于猜测）。周号接受 1–53，不严格校验 ISO 年真实周数。
_WEEK_RE_YEAR = re.compile(r"^(\d{4})\s*[-/]?\s*[Ww]\s*(\d{1,2})$")     # 2026-W35 / 2026w5
_WEEK_RE_BARE = re.compile(r"^[Ww](\d{1,2})$")                          # w35 / W5
_WEEK_RE_CN = re.compile(r"^(?:(\d{4})\s*年\s*)?第?\s*(\d{1,2})\s*周$")  # 2026年35周 / 第35周 / 35周

# Buckets a caller may ask for. ``quarter`` is a DERIVED bucket (like ``week``):
# source values never carry it, but a day/month column can be bucketed to it.
BUCKETS = ("year", "quarter", "month", "week", "day")


def _valid(y: int, m: int, d: int, precision: str) -> tuple[int, int, int, str] | None:
    """Calendar-validated (y, m, d, precision); None on an impossible date.

    ``precision`` is passed in explicitly (not derived from m/d) because
    ``202501`` (YYYYMM, m=1) is a *month* while ``2026`` is a *year*."""
    try:
        datetime.date(y, m, d)
    except ValueError:
        return None
    return (y, m, d, precision)


# Unix epoch seconds / milliseconds, restricted to the 2001-01-01..2100 window so
# a 10-digit count or id is never silently read as a date.
_EPOCH_MIN = 978307200          # 2001-01-01
_EPOCH_MAX = 4102444800         # 2100-01-01
_EPOCH_MS_MIN = _EPOCH_MIN * 1000
_EPOCH_MS_MAX = _EPOCH_MAX * 1000


def _epoch_date(n: int) -> tuple[int, int, int, str] | None:
    """(y, m, d, 'day') for an epoch seconds/milliseconds integer, else None."""
    seconds = None
    if _EPOCH_MIN <= n <= _EPOCH_MAX:
        seconds = n
    elif _EPOCH_MS_MIN <= n <= _EPOCH_MS_MAX:
        seconds = n // 1000
    if seconds is None:
        return None
    d = datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=seconds)
    return (d.year, d.month, d.day, "day")


def _parse(value: Any) -> tuple[int, int, int, str] | None:
    """Return (year, month, day, precision) or None when ``value`` is not a date.

    ``precision`` is the finest field the raw value actually carried
    (``year`` / ``month`` / ``day``) — it drives grain inference so a monthly
    column is not silently turned into 30 daily buckets.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime.datetime):
        return (value.year, value.month, value.day, "day")
    if isinstance(value, datetime.date):
        return (value.year, value.month, value.day, "day")
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not float(value).is_integer():
            return None
        epoch = _epoch_date(int(value))
        if epoch:
            return epoch
        s = str(int(value))
    else:
        s = str(value).strip()
        if not s:
            return None
        # "202501.0" (CSV/Excel float spelling) — strip the decimal part only
        # when the whole token is digits + '.0…' (str.rstrip('.0') would corrupt
        # a bare year: 2020 → '202').
        m0 = _RE_FLOAT_ZERO.match(s)
        if m0:
            s = m0.group(1)
    # YYYYMMDD / YYYYMM / YYYY (no separators)
    if re.fullmatch(r"\d{8}", s):
        return _valid(int(s[:4]), int(s[4:6]), int(s[6:]), "day")
    if re.fullmatch(r"\d{6}", s):
        return _valid(int(s[:4]), int(s[4:]), 1, "month")
    if re.fullmatch(r"\d{4}", s):
        return (int(s), 1, 1, "year")
    mq = _RE_QUARTER.match(s)
    if mq:
        return (int(mq.group(1)), (int(mq.group(2)) - 1) * 3 + 1, 1, "quarter")
    m = _RE_SEPARATED.match(s)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        da = int(m.group(3)) if m.group(3) else 1
        return _valid(y, mo, da, "day" if m.group(3) else "month")
    return None


def _week_of(value: Any) -> tuple[int | None, int] | None:
    """(iso_year|None, week 1..53) for a week-form value, else None.

    Bare weeks (``w35``) carry no year — cross-year ordering is not guaranteed
    for them (within one column the numeric week order is, which is what the
    axis needs). Numeric week tokens (``202535``) are deliberately NOT
    recognized: they collide with YYYYMM and counts, so a week must carry an
    explicit w/W/周 marker.
    """
    if value is None or isinstance(value, bool) or isinstance(value, (int, float)):
        return None
    s = str(value).strip()
    if not s:
        return None
    for rx, has_year in ((_WEEK_RE_YEAR, True), (_WEEK_RE_BARE, False), (_WEEK_RE_CN, True)):
        mw = rx.match(s)
        if mw:
            if has_year:
                year = int(mw.group(1)) if mw.group(1) else None
                week = int(mw.group(2))
            else:
                year, week = None, int(mw.group(1))
            return (year, week) if 1 <= week <= 53 else None
    return None


def week_parts(value: Any) -> tuple[int | None, int] | None:
    """Public week reader: (iso_year|None, week) or None. ``parts()`` stays
    (y,m,d)-shaped and returns None for week values, so existing date-math
    consumers (datediff/range_of/parseDateValue) can never receive a week."""
    return _week_of(value)


def week_label(year: int | None, week: int) -> str:
    """Canonical week label: uppercase W + zero-padded 2-digit week number.

    Zero-padding is what makes lexicographic GROUP BY / sort order correct
    (``W05`` < ``W35``); ``w5`` and ``W5`` normalize to the same label so they
    merge into one bucket everywhere (compile / server / browser)."""
    return f"{year:04d}-W{week:02d}" if year else f"W{week:02d}"


def parts(value: Any) -> tuple[int, int, int] | None:
    """(year, month, day), month/day defaulting to 1; None when not a date.

    Week-form values (``w35``) are temporal but have no single (y,m,d) date —
    they return None here on purpose; use ``week_parts()``/``grain()``/``label()``."""
    p = _parse(value)
    return (p[0], p[1], p[2]) if p else None


def grain(value: Any) -> str | None:
    """Finest temporal field the value carries: year | quarter | month | week | day."""
    w = _week_of(value)
    if w:
        return "week"
    p = _parse(value)
    return p[3] if p else None


def iso(value: Any) -> str | None:
    """'YYYY-MM-DD' (day precision, zero padded) or None."""
    p = _parse(value)
    return f"{p[0]:04d}-{p[1]:02d}-{p[2]:02d}" if p else None


def label(value: Any, bucket: str) -> str | None:
    """Bucket label ('YYYY' / 'YYYY-MM' / 'YYYY-MM-DD' / 'YYYY-Www' / 'Www') or None.

    Returns None when the value is not temporal, so callers can keep the raw
    token (see ``spec_to_report._fmt_time_label``)."""
    if bucket == "week":
        # 值级周字段（w35 / 2026-W35）：必须先于 _parse 闸——周值没有 (y,m,d)，
        # 过闸会被判「非时间」返回 None，服务端 ai_week 的 GROUP BY 就废了。
        w = _week_of(value)
        if w:
            return week_label(*w)
    p = _parse(value)
    if not p:
        return None
    y, m, d, _ = p
    if bucket == "year":
        return f"{y:04d}"
    if bucket == "quarter":
        return f"{y:04d}-Q{(m - 1) // 3 + 1}"
    if bucket == "month":
        return f"{y:04d}-{m:02d}"
    if bucket == "week":
        # 日期值的 ISO 周标签 = ISO year + ISO week number. The ISO year of a
        # year-end date often differs from the calendar year (2025-12-29 is
        # 2026-W01): pairing the ISO week number with the calendar year folded
        # 2025-01-01 and 2025-12-29 into the SAME "2025-W01" bucket and their
        # aggregates were silently summed.
        iso_y, iso_w, _ = datetime.date(y, m, d).isocalendar()
        return f"{iso_y:04d}-W{iso_w:02d}"
    if bucket == "day":
        return f"{y:04d}-{m:02d}-{d:02d}"
    return str(value).strip()


def sort_key(value: Any) -> tuple:
    """Total-order sort key: parseable dates first (chronologically), then
    opaque strings, then non-temporal numbers.

    The leading discriminant keeps the tuple comparable across mixed values
    (a column with one bad cell must not raise TypeError mid-sort)."""
    w = _week_of(value)
    if w:
        # 周值按归一标签排（带年 → 2026-W05 < 2026-W35；裸周 → W05 < W35，列内
        # 日历序）。与日期串 (0,'YYYY-MM-DD') 同档混列时字符串序仍是确定性全序。
        return (0, week_label(*w))
    p = _parse(value)
    if p:
        return (0, f"{p[0]:04d}-{p[1]:02d}-{p[2]:02d}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return (2, float(value))
    return (1, str(value).strip())


def infer_grain(values: list[Any], name: str = "", threshold: float = 0.6) -> str | None:
    """Infer the bucket grain of a whole column, or None when it is not temporal.

    Day-precision values win when they dominate (a daily column must bucket to
    days); a bare ``YYYY`` only counts as a year when the column name carries a
    year hint, so a count column (``1902``) is never read as a date."""
    vals = [v for v in values if v is not None and str(v).strip() != ""]
    if not vals:
        return None
    year_hint = bool(re.search(r"年|年份|year", str(name), re.I))
    day = month = year = quarter = week = 0
    for v in vals:
        g = grain(v)
        if g == "day":
            day += 1
        elif g == "month":
            month += 1
        elif g == "year":
            year += 1
        elif g == "quarter":
            quarter += 1
        elif g == "week":
            week += 1
    n = len(vals)
    if day / n > threshold:
        return "day"
    if (day + month) / n > threshold:
        return "month"
    # 纯季度串列（2025Q1..2025Q4）也是时间轴：不认它会被当 category 按指标
    # 降序重排（Q1..Q4 乱轴），窗口指标还会报「未声明分桶」。
    if quarter / n > threshold:
        return "quarter"
    # 值级周字段（w35..w38 / 2026-W35）：周粒度时间轴——窗口 prev(1,week) 的
    # 「w36 环比 w35」全靠它；不认会被当 category 乱序。
    if week / n > threshold:
        return "week"
    if year_hint and year / n > threshold:
        return "year"
    return None

# ---------------------- bucket label → date span (下钻的逆运算) -------------------
# 时间下钻 = 「点 2025 那根柱子 → 只看 2025 年 + 换成月粒度」。谓词落在**归一化后的
# 日期**上（daterange，见 aggregator._filter_clause / chart-adapter.matchFilter），
# 而点击的标签是**分桶标签** —— 所以需要标签 → 区间的逆运算。它必须与 label()/grain()
# 用同一套 parts，否则会出现「点了 2025-Q3，筛出来的是 2025 全年」这类静默错位。
_QUARTER_RE = re.compile(r"^\s*(\d{4})\s*-\s*[Qq]([1-4])\s*$")


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        nxt = datetime.date(year + 1, 1, 1)
    else:
        nxt = datetime.date(year, month + 1, 1)
    return (nxt - datetime.timedelta(days=1)).day


def range_of(value: Any, grain_hint: str | None = None) -> tuple[str, str] | None:
    """ISO span ``(from, to)`` covered by a bucket label (inclusive), else None.

    ``grain_hint`` overrides the inferred grain (the caller knows which axis it
    clicked). Week labels are not supported here and return None — the drill
    chain is year → quarter → month → day.
    """
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    g = grain_hint or grain(text)
    quarter = _QUARTER_RE.match(text)
    if g == "quarter" and quarter:
        y, q = int(quarter.group(1)), int(quarter.group(2))
        start = (q - 1) * 3 + 1
        end = start + 2
        return (f"{y:04d}-{start:02d}-01", f"{y:04d}-{end:02d}-{_days_in_month(y, end):02d}")
    parts_value = parts(text)
    if not parts_value:
        return None
    y, m, d = parts_value
    if g == "year":
        return (f"{y:04d}-01-01", f"{y:04d}-12-31")
    if g == "quarter":
        start = (m - 1) // 3 * 3 + 1
        end = start + 2
        return (f"{y:04d}-{start:02d}-01", f"{y:04d}-{end:02d}-{_days_in_month(y, end):02d}")
    if g == "month":
        return (f"{y:04d}-{m:02d}-01", f"{y:04d}-{m:02d}-{_days_in_month(y, m):02d}")
    if g == "day":
        return (f"{y:04d}-{m:02d}-{d:02d}", f"{y:04d}-{m:02d}-{d:02d}")
    return None
