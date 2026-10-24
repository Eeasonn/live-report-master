"""Data engine: load canonical tables into file-backed SQLite.

The engine is intentionally a small surface. Higher layers (aggregator,
truth, HTTP handler) compose these primitives.

SQLite is the v1 engine (Python stdlib, zero install). The public launcher
never selects an alternate engine.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import sqlite3
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from ._platform import ColumnSchema, TableInfo


class TableSchemaError(ValueError):
    """装载期表结构错误（如重复列名）。映射为 HTTP 400 —— 在先 DROP 后 CREATE
    的装载顺序下，带着重复列名走完 CREATE 会把既有同名表摧毁（启动先删后建，
    建失败表就没了，且重启重放同一份畸形快照永远失败）。"""

# Shared temporal parser (scripts/date_parse.py): the SAME implementation the
# compiler uses, so SQL bucketing cannot drift from the compiled/live labels.
_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_DIR) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(_SCRIPTS_DIR))
import date_parse as _date_parse  # noqa: E402
from . import agent_extensions as _business_calendar  # noqa: E402


# 名实一致（第 2 轮收口批 P3）：本正则只拒绝「非字符串 / 空串 / NUL」——它**不是**
# 严格标识符白名单，也绝不能收紧成 [A-Za-z0-9_]：CJK 列名（销售×库存）是受支持
# 场景（app.py 路由注释同款承诺）。SQL 注入防护由 _qcol/_quote 的双引号包裹 +
# 内部双写（" → ""）保证，这里只是最后一道形状闸（拒绝控制字符注入）。
NON_EMPTY_IDENT = __import__('re').compile(r"^[^\x00]+$")


# ---------------------- type inference --------------------------------------------

def _coerce_sample(values: Iterable[Any]) -> list[Any]:
    """Filter None/empty; coerce for sampling."""
    out: list[Any] = []
    for v in values:
        if v is None or v == "":
            continue
        out.append(v)
    return out


def _is_numeric(values: list[Any]) -> bool:
    return all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values)


def _is_boolean(values: list[Any]) -> bool:
    return all(isinstance(v, bool) for v in values)


def _is_date_like(values: list[Any]) -> bool:
    """Heuristic: 80%+ values parse as ISO date/datetime."""
    if not values:
        return False
    hits = 0
    for v in values[:500]:
        if isinstance(v, (_dt.date, _dt.datetime)):
            hits += 1
        elif isinstance(v, str):
            s = v.replace("Z", "+00:00")[:19]
            try:
                _dt.datetime.fromisoformat(s)
                hits += 1
            except ValueError:
                pass
    return hits / len(values[:500]) > 0.8


def _infer_inferred_type(values: list[Any]) -> str:
    """Public inference: dimension | measure | time | id."""
    vals = _coerce_sample(values)
    if not vals:
        return "dimension"
    if _is_numeric(vals):
        return "measure"
    if _is_date_like(vals):
        return "time"
    # unique short strings -> id
    distinct = {str(v) for v in vals}
    if len(distinct) == len(vals) and all(len(s) <= 32 for s in list(distinct)[:200]):
        return "id"
    return "dimension"


def _infer_sql_type(values: list[Any]) -> str:
    """SQLite-friendly SQL type."""
    vals = _coerce_sample(values)
    if not vals:
        return "TEXT"
    if _is_boolean(vals):
        return "INTEGER"
    if _is_numeric(vals):
        # distinguish integer vs float
        if all(isinstance(v, int) for v in vals):
            return "INTEGER"
        return "REAL"
    return "TEXT"


def _serialize_for_sql(value: Any) -> Any:
    """Convert Python value to a SQLite-friendly form."""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    if isinstance(value, (int, float, str)):
        return value
    return str(value)


def _qcol(name: str) -> str:
    """Quote a column identifier for SQL."""
    if not isinstance(name, str) or not NON_EMPTY_IDENT.match(name):
        raise ValueError(f"unsafe column name: {name!r}")
    return f'"{name.replace(chr(34), chr(34) * 2)}"'


# ---------------------- single-pass column accumulator ---------------------------

class _ColAcc:
    """Per-column running statistics for ``load_table``（单遍装载，2026-09-25）。

    每个字段都与旧「全量样本 + 二次推断」实现逐位对齐：

    - ``sql_type``  = ``_infer_sql_type(coerced_sample)``（布尔→INTEGER；
      全数值→INTEGER/REAL；否则 TEXT）。
    - ``inferred_type`` = ``_infer_inferred_type(coerced_sample)``：全数值→
      measure；前 500 个非空值 80% 可解析为日期→time；distinct==非空数且前
      200 个（集合迭代序）长度 ≤32→id；否则 dimension。
    - ``distinct``  = ``len({_serialize_for_sql(v) for v in non_null})``——键
      用序列化值（bool→1、date→ISO），与旧 schema.distinct 同一转换。
    - measure 统计在行序上做 float 运行累加（sum 左结合，与旧 ``sum(nums)``
      逐位一致）；time 的 sample_min/max = ``sorted(str(v))[0/-1][:10]`` 的
      等价运行 min/max。
    """

    __slots__ = ("non_null", "distinct", "has_bool", "has_str", "has_other", "has_float",
                 "num_count", "num_min", "num_max", "num_sum", "str_min", "str_max", "head500")

    def __init__(self) -> None:
        self.non_null = 0
        self.distinct: set[Any] = set()
        self.has_bool = False
        self.has_str = False
        self.has_other = False
        self.has_float = False
        self.num_count = 0
        self.num_min: float | None = None
        self.num_max: float | None = None
        self.num_sum = 0.0
        self.str_min: str | None = None
        self.str_max: str | None = None
        self.head500: list[Any] = []

    def add(self, v: Any) -> None:
        if v is None or v == "":
            return
        self.non_null += 1
        if len(self.head500) < 500:
            self.head500.append(v)
        t = type(v)
        if t is bool:
            self.has_bool = True
        elif t is int or t is float:
            self.num_count += 1
            if t is float:
                self.has_float = True
            f = v if t is float else float(v)
            if self.num_min is None or f < self.num_min:
                self.num_min = f
            if self.num_max is None or f > self.num_max:
                self.num_max = f
            self.num_sum += f
        # distinct 键与旧实现同一转换（快路径跳过函数调用：str/int/float 原样，
        # bool→int、date→ISO、其余→str）。
        if t is str or t is int or t is float:
            self.distinct.add(v)
        elif t is bool:
            self.distinct.add(int(v))
        else:
            self.has_other = True
            self.distinct.add(_serialize_for_sql(v))
        if t is str:
            self.has_str = True
        s = v if t is str else str(v)
        if self.str_min is None or s < self.str_min:
            self.str_min = s
        if self.str_max is None or s > self.str_max:
            self.str_max = s

    @property
    def _all_numeric(self) -> bool:
        return self.non_null > 0 and not self.has_bool and self.num_count == self.non_null

    @property
    def sql_type(self) -> str:
        if self.non_null == 0:
            return "TEXT"
        # 全布尔（旧 `_is_boolean`：所有非空值都是 bool，不得混入字符串/其他）。
        if self.has_bool and self.num_count == 0 and not self.has_str and not self.has_other:
            return "INTEGER"
        if self._all_numeric:
            return "REAL" if self.has_float else "INTEGER"
        return "TEXT"

    def _date_like(self) -> bool:
        """镜像 ``_is_date_like``：前 500 个非空值 80%+ 可解析为 ISO 日期。"""
        vals = self.head500
        if not vals:
            return False
        hits = 0
        for v in vals:
            if isinstance(v, (_dt.date, _dt.datetime)):
                hits += 1
            elif isinstance(v, str):
                s = v.replace("Z", "+00:00")[:19]
                try:
                    _dt.datetime.fromisoformat(s)
                    hits += 1
                except ValueError:
                    pass
        return hits / len(vals) > 0.8

    @property
    def inferred_type(self) -> str:
        if self.non_null == 0:
            return "dimension"
        if self._all_numeric:
            return "measure"
        if self._date_like():
            return "time"
        # unique short strings -> id（前 200 个按集合迭代序，与旧实现一致；
        # 键经 str() 转换后再量长度——数值键 len() 会 TypeError）。
        if self.len_distinct == self.non_null:
            from itertools import islice
            for s in islice(self.distinct, 200):
                if len(str(s)) > 32:
                    return "dimension"
            return "id"
        return "dimension"

    @property
    def len_distinct(self) -> int:
        return len(self.distinct)

    def schema(self, name: str) -> ColumnSchema:
        col_schema = ColumnSchema(
            name=name,
            sql_type=self.sql_type,
            inferred_type=self.inferred_type,
            non_null=self.non_null,
            distinct=self.len_distinct,
        )
        inf_t = col_schema.inferred_type
        if inf_t == "measure" and self.non_null:
            col_schema.min = self.num_min
            col_schema.max = self.num_max
            col_schema.mean = self.num_sum / self.non_null
            col_schema.sum = self.num_sum
        elif inf_t == "time" and self.non_null:
            col_schema.sample_min = (self.str_min or "")[:10]
            col_schema.sample_max = (self.str_max or "")[:10]
        return col_schema


# ---------------------- engine interface -----------------------------------------

class Engine:
    """Common interface. Both engines below implement it."""

    @property
    def revision(self) -> int:
        """Monotonic data revision used by bounded read caches.

        Every table load/drop/mutation advances the revision.  Consumers can
        include it in a cache key without guessing whether a same-sized
        replacement changed values.
        """
        return int(getattr(self, "_revision", 0))

    # XC-01 (2026-10-24): startup-scoped instance identity for aggregator
    # cache/in-flight keys.  ``revision`` alone describes data state, not which
    # engine holds it — two ``create_engine()`` instances can legitimately hold
    # same-named tables at the same revision (report copies share dataset ids),
    # so keys must carry a token that is unique per live instance.  Both
    # concrete engines set this in ``__init__``; aggregator falls back to an
    # id-prefixed marker for lightweight adapters that bypass this base class.
    instance_token: str = ""

    def load_table(self, name: str, columns: list[str], rows: list[list[Any]],
                   index_columns: Optional[list[str]] = None) -> TableInfo:
        raise NotImplementedError

    def execute(self, sql: str, params: tuple = (), *, cancel_check=None,
                timeout_seconds: float | None = None) -> list[dict[str, Any]]:
        raise NotImplementedError

    def execute_mutation(self, sql: str, params: tuple = ()) -> int:
        """Run a DELETE/UPDATE statement and return the affected row count.

        ``execute`` discards ``cursor.rowcount`` (it only shapes result sets);
        row-level data management (数据管理的行删除) needs the real count both
        for the response payload and for the post-delete rowCount bookkeeping.
        """
        raise NotImplementedError

    def list_tables(self) -> list[str]:
        raise NotImplementedError

    def table_info(self, name: str) -> Optional[TableInfo]:
        raise NotImplementedError

    def drop_table(self, name: str) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    @property
    def engine_name(self) -> str:
        raise NotImplementedError

    # True when the engine has the shared temporal-normalization UDF (`ai_date`).
    # aggregator._bucket_expr uses it to normalize non-ISO date spellings
    # (2026/9/5, 2026年9月5日, 20260905 …) before strftime; without it those
    # columns bucket to NULL and the time axis renders empty.
    supports_date_norm: bool = False
    supports_num_norm: bool = False
    # True when the engine has the ``ai_is_date`` UDF (0/1 date-parseability from
    # the shared date_parse). aggregator._filter_clause uses it as the daterange
    # row guard: ai_date passes junk through by design, so only this predicate
    # can exclude non-date text the way compile/browser do.
    supports_date_check: bool = False
    # True when the engine has the ``ai_week`` UDF (ISO week bucket label from
    # the shared date_parse). SQLite strftime %W is a Monday-based 00-53 week
    # of the CALENDAR year — not ISO — so %Y-W%W disagreed with the compiler
    # and the browser on nearly every week of the year (2026-01-01 → W00 vs
    # 2026-W01; year-end dates folded into the wrong ISO year).
    supports_week_norm: bool = False
    # True when the explicit month-aligned fiscal-calendar UDF is available.
    # It is opt-in through an Agent bucket.calendar declaration; omitted
    # calendars remain Gregorian and byte-compatible.
    supports_calendar_norm: bool = False


# ---------------------- shared temporal normalization ---------------------------

def _ai_num(value: Any) -> Any:
    """SQL UDF: mirror of JS ``Number()`` for ordering/range predicates.

    TEXT 列里混入非数字脏值时，`col >= ?` 在 SQLite 里按亲和性乱比（'100'<'70'
    按字典序、文本恒大于数字），浏览器本地路径却是 Number() 强转——两端行集
    分裂。这里把「可解析 → REAL，不可解析 → NULL」收敛成同一语义（NULL 再被
    `IS NOT NULL` 闸掉 = JS 的 NaN 比较恒 False）。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        f = float(value)
        return f if f == f else None   # NaN → NULL（JS NaN 比较恒 False）
    s = str(value).strip()
    if not s:
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    return f if f == f else None       # 'nan' 字面量同上


def _register_num_norm(conn: Any, dialect: str, module: Any = None) -> bool:
    """Best-effort registration of the ``ai_num`` UDF; False when unsupported."""
    try:
        cache: dict[Any, Any] = {}

        def _fn(value: Any) -> Any:
            try:
                key = value if isinstance(value, (int, float, str, type(None))) else repr(value)
                if key in cache:
                    return cache[key]
                out = _ai_num(value)
                if len(cache) < 100000:
                    cache[key] = out
                return out
            except TypeError:  # unhashable
                return _ai_num(value)

        if dialect == "sqlite":
            conn.create_function("ai_num", 1, _fn, deterministic=True)
            return True
        for params, ret in ((["VARCHAR"], "DOUBLE"),):
            try:
                conn.create_function("ai_num", _fn, params, ret)
                return True
            except Exception as exc:  # pragma: no cover
                last = exc
        raise last  # noqa: F821
    except Exception:
        return False


def _ai_date_normalize(value: Any) -> Any:
    """SQL UDF: normalize a date-like value to ISO 'YYYY-MM-DD', else pass through.

    Delegates to ``scripts/date_parse.py`` — the SAME parser the compiler and the
    browser use — so a bucket computed in SQL equals the compiled label and the
    live re-bucketed label (WYSIWYG, no third date implementation).

    Non-temporal values are returned UNCHANGED (not NULL) so strftime keeps
    working on epoch integers / ISO timestamps, which it already understands.
    """
    if value is None or isinstance(value, (bytes, bytearray)):
        return value
    try:
        iso = _date_parse.iso(value)
    except Exception:  # pragma: no cover - defensive: never break the query
        return value
    return iso if iso else value


def _register_date_norm(conn: Any, dialect: str, module: Any = None) -> bool:
    """Best-effort registration of the ``ai_date`` UDF; False when unsupported."""
    try:
        # Memoize: a GROUP BY evaluates the UDF per row, and a date column has
        # few distinct values (417k rows / 101 distinct days in the real case).
        cache: dict[Any, Any] = {}

        def _fn(value: Any) -> Any:
            try:
                key = value if isinstance(value, (int, float, str, type(None))) else repr(value)
                if key in cache:
                    return cache[key]
                out = _ai_date_normalize(value)
                if len(cache) < 100000:
                    cache[key] = out
                return out
            except TypeError:  # unhashable
                return _ai_date_normalize(value)

        if dialect == "sqlite":
            conn.create_function("ai_date", 1, _fn, deterministic=True)
            return True
        # duckdb takes SQL type names as strings (duckdb 1.x has no `duckdb.types`).
        last: Exception | None = None
        for params, ret in ((["VARCHAR"], "VARCHAR"), (["STRING"], "STRING")):
            try:
                conn.create_function("ai_date", _fn, params, ret)
                return True
            except Exception as exc:  # pragma: no cover - duckdb API differs by version
                last = exc
        if last:
            raise last
        return False
    except Exception:
        return False
    except Exception:
        return False


def _ai_week_label(value: Any) -> Any:
    """SQL UDF: ISO week bucket label ('YYYY-Www') via the shared date_parse,
    else NULL for non-temporal values.

    ``strftime('%Y-W%W')`` is NOT the ISO week (calendar-year Monday week,
    00-53): it produced ``W00`` buckets the compiler never emits and folded
    year-end dates into the wrong week. Delegating to ``date_parse.label`` is
    the same single-source contract as ``ai_date`` (WYSIWYG with the compiled
    static option and the browser ``bucketLabel``)."""
    if value is None or isinstance(value, (bytes, bytearray)):
        return None
    try:
        return _date_parse.label(value, "week")
    except Exception:  # pragma: no cover - defensive: never break the query
        return None


def _register_week_norm(conn: Any, dialect: str, module: Any = None) -> bool:
    """Best-effort registration of the ``ai_week`` UDF; False when unsupported."""
    try:
        cache: dict[Any, Any] = {}

        def _fn(value: Any) -> Any:
            try:
                key = value if isinstance(value, (int, float, str, type(None))) else repr(value)
                if key in cache:
                    return cache[key]
                out = _ai_week_label(value)
                if len(cache) < 100000:
                    cache[key] = out
                return out
            except TypeError:  # unhashable
                return _ai_week_label(value)

        if dialect == "sqlite":
            conn.create_function("ai_week", 1, _fn, deterministic=True)
            return True
        # duckdb 的 python UDF 默认（null_handling='DEFAULT'）**不允许返回 NULL**——
        # ai_week 对非时间值返回 NULL 是文档化契约（与 strftime 分支的 NULL 分组一致），
        # 脏值列一跑就 InvalidInputException。null_handling='SPECIAL' 允许返回 NULL
        # （_fn 本就自己处理 None 入参）；老版本 duckdb 不认识该参数时退回普通注册。
        last: Exception | None = None
        for params, ret in ((["VARCHAR"], "VARCHAR"), (["STRING"], "STRING")):
            try:
                conn.create_function("ai_week", _fn, params, ret, null_handling="SPECIAL")
                return True
            except TypeError:
                try:
                    conn.create_function("ai_week", _fn, params, ret)
                    return True
                except Exception as exc:  # pragma: no cover - duckdb API differs by version
                    last = exc
            except Exception as exc:  # pragma: no cover - duckdb API differs by version
                last = exc
        if last:
            raise last
        return False

    except Exception:
        return False


def _fiscal_date_normalize(value: Any, start_month: Any) -> Any:
    """Shift a raw date so a configured fiscal start month becomes January."""
    try:
        month = int(start_month)
    except (TypeError, ValueError):
        return None
    if not 1 <= month <= 12:
        return None
    try:
        return _business_calendar.shifted_iso(
            value, {"kind": "fiscal", "fiscalStartMonth": month})
    except Exception:  # pragma: no cover - UDF must not break a query
        return None


def _register_calendar_norm(conn: Any, dialect: str, module: Any = None) -> bool:
    """Best-effort registration of the two-argument fiscal-date UDF."""
    try:
        if dialect == "sqlite":
            conn.create_function("ai_fiscal_date", 2, _fiscal_date_normalize, deterministic=True)
            return True
        last: Exception | None = None
        for params, ret in ((["VARCHAR", "INTEGER"], "VARCHAR"),
                            (["STRING", "INTEGER"], "STRING")):
            try:
                conn.create_function("ai_fiscal_date", _fiscal_date_normalize,
                                     params, ret, null_handling="SPECIAL")
                return True
            except TypeError:
                try:
                    conn.create_function("ai_fiscal_date", _fiscal_date_normalize,
                                         params, ret)
                    return True
                except Exception as exc:  # pragma: no cover
                    last = exc
            except Exception as exc:  # pragma: no cover
                last = exc
        if last:
            raise last
        return False
    except Exception:
        return False


def _ai_is_date(value: Any) -> int:
    """SQL UDF: 1 when the value parses as a calendar date (shared date_parse), else 0.

    ``ai_date`` deliberately PASSES non-temporal values through (epoch integers /
    ISO timestamps must keep reaching strftime), so a ``daterange`` predicate
    built on it cannot tell junk text from a date — ``'N/A' >= '2026-01-01'`` is
    lexicographic truth. The compile side (``spec_to_report._row_filter``) and
    the browser (``chart-adapter.dateParts``) EXCLUDE unparseable values from a
    daterange; this predicate is the same parser's yes/no answer so the server
    selects the same rows (three-way row-set parity, tests/test_filter_range.py).
    """
    if value is None or isinstance(value, (bytes, bytearray)):
        return 0
    try:
        return 1 if _date_parse.parts(value) else 0
    except Exception:  # pragma: no cover - defensive: never break the query
        return 0


def _register_date_check(conn: Any, dialect: str, module: Any = None) -> bool:
    """Best-effort registration of the ``ai_is_date`` UDF; False when unsupported."""
    try:
        cache: dict[Any, Any] = {}

        def _fn(value: Any) -> Any:
            try:
                key = value if isinstance(value, (int, float, str, type(None))) else repr(value)
                if key in cache:
                    return cache[key]
                out = _ai_is_date(value)
                if len(cache) < 100000:
                    cache[key] = out
                return out
            except TypeError:  # unhashable
                return _ai_is_date(value)

        if dialect == "sqlite":
            conn.create_function("ai_is_date", 1, _fn, deterministic=True)
            return True
        last: Exception | None = None
        for params, ret in ((["VARCHAR"], "BOOLEAN"), (["STRING"], "BOOLEAN")):
            try:
                conn.create_function("ai_is_date", _fn, params, ret)
                return True
            except Exception as exc:  # pragma: no cover - duckdb API differs by version
                last = exc
        if last:
            raise last
        return False
    except Exception:
        return False


# ---------------------- persistent table cache (NP-01) ---------------------------
#
# Boot reload (`run_server._reload_all`) DROP+CREATEs every dataset from its JSON
# snapshot on EVERY server start — a 980k-row snapshot (282MB) costs ~15s per
# boot while the aggregate itself runs in ~200ms. The persistent table cache
# stores each loaded table in its own SQLite file under
# ``<state-home>/db/<datasetId>.sqlite`` so a later boot whose snapshot content
# AND derived-column declarations are unchanged can restore the table without
# re-parsing the snapshot.
#
# Concurrency contract (probed, tests/test_persistent_engine.py):
#   * Cache files are IMMUTABLE once published. They are written only through
#     ``export_table_cache``: build a fresh ``*.tmp-<pid>`` file, then
#     ``os.replace`` it into place (atomic on POSIX/Windows). No in-place DML
#     ever targets a cache file — adoption COPIES the cached table into the
#     engine's own main database and detaches immediately, so every runtime
#     statement (index-advisor ``CREATE INDEX``, row deletes, wide-table joins,
#     derived replay) keeps hitting the main db exactly as before.
#     (Probe: ``CREATE INDEX`` cannot target a table living in an attached db —
#     "no such table: main.<t>" — and an unqualified ``DROP`` with no main copy
#     silently destroys the attached cache file. Copy-on-adopt neutralizes both.)
#   * ``os.replace`` over a file another process is reading is safe: open
#     readers keep the old inode, new openers see the new file, and the two
#     contents are identical by construction (same snapshot fingerprint).
#   * Thread model unchanged: adoption/export run under the engine lock during
#     boot, before the HTTP server (or worker loop) starts;
#     ``check_same_thread=False`` + per-method locking is untouched.
#   * Fail-closed: adoption verifies the meta table (fingerprint + decl hash +
#     cache version), ``PRAGMA quick_check``, table presence and ``COUNT(*)``
#     against the recorded row count. ANY mismatch/exception → the caller falls
#     back to the normal snapshot reload and the cache file is re-exported.
#     A cache never serves data it did not verify.
#   * ``LRM_NO_DB_CACHE=1`` (also true/yes) disables the whole layer in
#     ``run_server`` — no reads, no writes, behavior byte-identical to the
#     pre-NP-01 reload path.
ENGINE_CACHE_VERSION = "1"


def engine_supports_table_cache(engine: Any) -> bool:
    """True when the engine implements the persistent table cache (SQLite only)."""
    return getattr(engine, "supports_table_cache", False) is True


def snapshot_fingerprint(path: str | os.PathLike[str]) -> str:
    """Streamed sha256 hex of a snapshot file (content-addressed cache key).

    Every dataset write path (replace/upsert/append/行删除/注册拷贝) lands the
    authoritative data in ONE canonical snapshot file via temp+os.replace, so
    the file's content hash advances on every data change — no registry hooks
    needed for invalidation.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def table_cache_stem(ds_id: str) -> str:
    """Filesystem-safe cache-file stem for a dataset id.

    Mirrors ``app._snapshot_stem`` (slug + short sha1 when slugification changed
    the id). store.py cannot import app.py (circular), so the rule is duplicated
    deliberately; ``tests/test_persistent_engine.py`` asserts parity.
    """
    import re as _re
    import hashlib as _hashlib
    stem = _re.sub(r"[^A-Za-z0-9_-]+", "_", str(ds_id or "")).strip("_") or "dataset"
    if stem != ds_id:
        stem = f"{stem}-{_hashlib.sha1(str(ds_id).encode('utf-8')).hexdigest()[:8]}"
    return stem


_META_TABLE = "_ai_cache_meta"


# ---------------------- dataset snapshots: dual format (XP-01, 2026-10-24) -------
#
# Dataset snapshots used to be JSON-only. XP-01 (E2 拍板) adds a SQLite snapshot
# format and makes it the DEFAULT for server-written generations (.replaced/.added/
# queued durable inputs), while old JSON snapshots stay PERMANENTLY readable
# (dual-format read compatibility — no migration of existing files).
#
# Format spec (`.sqlite` snapshot):
#   * table ``t`` — the data, DDL built from the SAME type inference as
#     ``SQLiteEngine.load_table`` (one implementation: ``_ColAcc``);
#   * meta table ``_lrm_snapshot`` (key/value):
#       format    = "lrm-snapshot"
#       version   = SNAPSHOT_FORMAT_VERSION ("1")
#       columns   = JSON [{name, type(sql), inferred(dimension|measure|time|id)}]
#                   — the writer's ``_ColAcc`` verdicts. SQL storage cannot see
#                   Python bools (they are INTEGER on disk) or pre-affinity value
#                   types, so the type/inferred verdicts MUST travel with the
#                   file; everything else (non_null/distinct/min/max/sum/sample)
#                   is recomputed by SQL at load time.
#       rowCount  = matched row count (rectangular rows, same rule as load_table)
#       createdAt = ISO timestamp
#   * write path: temp file + executemany(generator) + os.replace (same atomic
#     contract as the JSON writer).
#
# Fingerprint/cache note: ``snapshot_fingerprint`` is the file's sha256 —
# format-agnostic by construction. Flipping a dataset's snapshot format changes
# the bytes → the NP-01 table cache key changes → that dataset's cache is
# rebuilt ONCE on the next boot (documented one-time cost, not a behavior
# change). JSON→SQLite conversion of existing files is NEVER automatic.
SNAPSHOT_FORMATS = ("json", "sqlite")
SNAPSHOT_FORMAT_VERSION = "1"
_SNAPSHOT_META_TABLE = "_lrm_snapshot"


def snapshot_format() -> str:
    """Snapshot write format: env ``LRM_SNAPSHOT_FORMAT``, default ``sqlite``.

    Illegal values raise (fail-closed) — a typo must not silently flip the
    durable data format of every future snapshot.
    """
    raw = (os.environ.get("LRM_SNAPSHOT_FORMAT") or "").strip().lower() or "sqlite"
    if raw not in SNAPSHOT_FORMATS:
        raise ValueError(
            f"LRM_SNAPSHOT_FORMAT 非法：{raw!r}（可选：{'/'.join(SNAPSHOT_FORMATS)}）")
    return raw


def snapshot_suffix(fmt: str) -> str:
    """File suffix for a snapshot format ('.json' / '.sqlite')."""
    if fmt not in SNAPSHOT_FORMATS:
        raise ValueError(f"未知快照格式：{fmt!r}（可选：{'/'.join(SNAPSHOT_FORMATS)}）")
    return ".json" if fmt == "json" else ".sqlite"


def is_snapshot_path(path: str | os.PathLike[str]) -> bool:
    """True when the path names a dataset snapshot by suffix (.json/.sqlite)."""
    return Path(path).suffix.lower() in tuple(snapshot_suffix(f) for f in SNAPSHOT_FORMATS)


def write_snapshot(path: str | os.PathLike[str], columns: list, rows: Any,
                   fmt: str | None = None) -> Path:
    """Write a dataset snapshot in ``fmt`` (default ``snapshot_format()``).

    ``path`` may carry either suffix (callers historically pass ``.json``); the
    actual file is written at ``path`` with the format's suffix swapped in, and
    the REAL path is returned — callers that record the path into the registry/
    manifest MUST use the return value. JSON keeps the exact streaming writer
    the server has used since 2026-09-25; SQLite writes through a temp file fed
    by ``executemany`` over a generator, then ``os.replace`` (same atomic
    contract). IO/SQLite failures surface as OSError so existing callers'
    BadRequest conversion keeps working unchanged.
    """
    fmt = snapshot_format() if fmt is None else str(fmt).lower()
    if fmt not in SNAPSHOT_FORMATS:
        raise ValueError(f"未知快照格式：{fmt!r}（可选：{'/'.join(SNAPSHOT_FORMATS)}）")
    target = Path(path)
    suffix = snapshot_suffix(fmt)
    if target.suffix.lower() != suffix:
        target = target.with_suffix(suffix)
    if fmt == "json":
        _write_json_snapshot(target, columns, rows)
    else:
        _write_sqlite_snapshot(target, columns, rows)
    return target


def _write_json_snapshot(path: Path, columns: list, rows: Any) -> None:
    """Streaming JSON snapshot writer (verbatim body of the 2026-09-25 P1)."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write('{"columns":')
            f.write(json.dumps(list(columns), ensure_ascii=False))
            f.write(',"rows":[')
            first = True
            for row in rows:
                if first:
                    first = False
                else:
                    f.write(",\n")
                f.write(json.dumps(list(row), ensure_ascii=False))
            f.write("]}")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _snapshot_rows_materialized(rows: Any) -> list[list[Any]]:
    """Rows as a re-iterable list.

    The SQLite writer needs TWO passes (type inference before CREATE TABLE,
    insert after); a one-shot iterator (e.g. the row-delete rewrite's engine
    generator) is drained exactly once into a list — same 1× memory the caller
    already materialized to produce it. Lists/tuples iterate twice without a
    copy.
    """
    if isinstance(rows, (list, tuple)):
        return rows
    return list(rows)


def _write_sqlite_snapshot(path: Path, columns: list, rows: Any) -> None:
    """SQLite snapshot writer: temp db + executemany(generator) + os.replace."""
    cols = [str(c) for c in columns]
    ncols = len(cols)
    if len(set(cols)) != ncols:
        _dupes = sorted({c for c in cols if cols.count(c) > 1})
        raise OSError(f"快照列名重复，拒绝落盘：{', '.join(_dupes[:5])}")
    materialized = _snapshot_rows_materialized(rows)
    # Same single-pass type inference as load_table (one implementation: _ColAcc).
    accs = [_ColAcc() for _ in range(ncols)]
    matched = 0
    for row in materialized:
        if len(row) != ncols:
            continue
        matched += 1
        for i in range(ncols):
            accs[i].add(row[i])
    tmp = path.with_name(path.name + ".tmp")
    try:
        conn = sqlite3.connect(str(tmp))
        try:
            col_defs = ", ".join(f'{_qcol(c)} {acc.sql_type}' for c, acc in zip(cols, accs))
            conn.execute(f'CREATE TABLE "t" ({col_defs})')
            placeholders = ", ".join("?" for _ in cols)
            insert_sql = (f'INSERT INTO "t" ({", ".join(_qcol(c) for c in cols)}) '
                          f'VALUES ({placeholders})')

            def _serialized_rows() -> Iterator[list[Any]]:
                for row in materialized:
                    if len(row) != ncols:
                        continue
                    out = []
                    for v in row:
                        t = type(v)
                        if t is str or t is int or t is float or v is None:
                            out.append(v)
                        elif t is bool:
                            out.append(int(v))
                        else:
                            out.append(_serialize_for_sql(v))
                    yield out
            conn.executemany(insert_sql, _serialized_rows())
            conn.execute(
                f'CREATE TABLE {_SNAPSHOT_META_TABLE} (key TEXT PRIMARY KEY, value TEXT)')
            meta = {
                "format": "lrm-snapshot",
                "version": SNAPSHOT_FORMAT_VERSION,
                "columns": json.dumps(
                    [{"name": c, "type": acc.sql_type, "inferred": acc.inferred_type}
                     for c, acc in zip(cols, accs)], ensure_ascii=False),
                "rowCount": str(matched),
                "createdAt": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            conn.executemany(
                f'INSERT INTO {_SNAPSHOT_META_TABLE} (key, value) VALUES (?, ?)',
                list(meta.items()))
            conn.commit()
            check = conn.execute('PRAGMA quick_check(1)').fetchall()
            if [str(r[0]) for r in check] != ["ok"]:
                raise OSError(f"快照完整性自检失败：{path.name}")
        finally:
            conn.close()
        os.replace(tmp, path)
    except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        # Callers convert OSError → BadRequest (fail-closed 不静默) — keep that
        # contract across both formats.
        if isinstance(exc, OSError):
            raise
        raise OSError(f"SQLite 快照写盘失败：{exc}") from exc


def read_snapshot(path: str | os.PathLike[str]) -> tuple[list, Any]:
    """Read a dataset snapshot in either format → (columns, rows-iterable).

    ``.json`` → parsed via ``json.load`` (columns list + rows list) — the same
    plain parse the warm register path has always used. ``.sqlite`` → columns
    from the meta table, rows streamed from ``SELECT * ORDER BY rowid`` (insert
    order); the rows generator holds a read-only connection and closes it when
    exhausted, so consumers MUST drain it (or let it go out of scope).
    """
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix == ".json":
        with p.open("r", encoding="utf-8") as stream:
            doc = json.load(stream)
        if not isinstance(doc, dict):
            raise ValueError(f"快照文档形状非法（非对象）：{p.name}")
        return list(doc.get("columns") or []), list(doc.get("rows") or [])
    if suffix == ".sqlite":
        return _read_sqlite_snapshot(p)
    raise ValueError(f"不是可识别的数据集快照（.json/.sqlite）：{p.name}")


def _read_sqlite_snapshot(path: Path) -> tuple[list, Iterator[list[Any]]]:
    uri = path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        try:
            meta = dict(conn.execute(
                f'SELECT key, value FROM {_SNAPSHOT_META_TABLE}').fetchall())
        except sqlite3.Error as exc:
            raise ValueError(f"SQLite 快照缺少元表：{path.name}") from exc
        if str(meta.get("format")) != "lrm-snapshot":
            raise ValueError(f"不是 LRM SQLite 快照：{path.name}")
        columns = [str(c.get("name")) for c in json.loads(meta.get("columns") or "[]")]
        if not columns:
            raise ValueError(f"SQLite 快照元数据缺少列清单：{path.name}")
        cur = conn.execute(f'SELECT * FROM "t" ORDER BY rowid')

        def _rows() -> Iterator[list[Any]]:
            try:
                while True:
                    batch = cur.fetchmany(4096)
                    if not batch:
                        return
                    for r in batch:
                        yield list(r)
            finally:
                conn.close()
        return columns, _rows()
    except Exception:
        conn.close()
        raise


def _table_ddl(conn: sqlite3.Connection, schema: str, name: str) -> Optional[str]:
    row = conn.execute(
        f'SELECT sql FROM {schema}.sqlite_master WHERE type = \'table\' AND name = ?',
        (name,)).fetchone()
    return None if row is None else (row[0] or None)


def _table_index_ddls(conn: sqlite3.Connection, schema: str, name: str) -> list[str]:
    rows = conn.execute(
        "SELECT sql FROM {schema}.sqlite_master WHERE type = 'index' "
        "AND tbl_name = ? AND sql IS NOT NULL".format(schema=schema), (name,)).fetchall()
    return [r[0] for r in rows if r[0]]


class SQLiteEngine(Engine):
    # Persistent table cache (NP-01): see the block comment above
    # ENGINE_CACHE_VERSION. DuckDB engines leave this False and run the
    # legacy reload path untouched.
    supports_table_cache = True

    def __init__(self, db_path: str | os.PathLike[str] = ":memory:"):
        # check_same_thread=False is required because ThreadingHTTPServer
        # dispatches across worker threads. The internal lock protects
        # every operation.
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._db_path = str(db_path)
        self._file_backed = str(db_path) != ":memory:"
        if self._file_backed:
            # File-backed reports are read-heavy and may serve concurrent chart
            # requests. WAL keeps readers non-blocking while the registry reloads
            # a dataset; NORMAL is the balanced durability setting for a local
            # report cache.
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        self._lock = threading.Lock()
        self._tables: dict[str, TableInfo] = {}
        self._revision = 0
        # XC-01: unique per-instance identity for aggregator cache keys and
        # in-flight markers (never reuse ``id()`` — CPython recycles ids).
        self.instance_token = uuid.uuid4().hex
        self.supports_date_norm = _register_date_norm(self._conn, dialect="sqlite")
        self.supports_num_norm = _register_num_norm(self._conn, dialect="sqlite")
        self.supports_week_norm = _register_week_norm(self._conn, dialect="sqlite")
        self.supports_calendar_norm = _register_calendar_norm(self._conn, dialect="sqlite")
        self.supports_date_check = _register_date_check(self._conn, dialect="sqlite")

    def load_table(self, name: str, columns: list[str], rows: list[list[Any]],
                   index_columns: Optional[list[str]] = None) -> TableInfo:
        if len(set(columns)) != len(columns):
            _dupes = sorted({c for c in columns if columns.count(c) > 1})
            raise TableSchemaError(
                "duplicate column names in table payload: " + ", ".join(map(str, _dupes[:5]))
                + "（重复列名会让 CREATE TABLE 失败并摧毁既有同名表）")
        # 单遍统计（性能批 2026-09-25）：旧实现对全表做 3-4 次物化拷贝（列主序
        # col_samples + serialized 两次行拷贝）且类型推断跑两遍——335k 行实测
        # 1.95s 里 ~1.5s 是纯多余拷贝。这里改为一次遍历产出全部列统计（类型
        # 标志、精确 distinct、min/max/sum 运行累积、时间 min/max），不再物化
        # 任何中间表；ColumnSchema 输出与旧实现逐位一致（见 _ColAcc 逐字段注）。
        ncols = len(columns)
        accs = [_ColAcc() for _ in range(ncols)]
        matched = 0
        for row in rows:
            if len(row) != ncols:
                continue  # 不齐行与旧行为一致：不装载、不计数
            matched += 1
            for i in range(ncols):
                accs[i].add(row[i])

        col_defs: list[str] = []
        for col, acc in zip(columns, accs):
            # _qcol（内部双写 "）与 INSERT 侧同一转义——裸 f'"{col}"' 遇到含
            # 双引号的表头（如 13" 屏幕）会生成畸形 CREATE TABLE 直接 500。
            col_defs.append(f'{_qcol(col)} {acc.sql_type}')

        with self._lock:
            self._conn.execute(f'DROP TABLE IF EXISTS {_qcol(name)}')
            self._conn.execute(f'CREATE TABLE {_qcol(name)} ({", ".join(col_defs)})')
            placeholders = ", ".join("?" for _ in columns)
            insert_sql = f'INSERT INTO {_qcol(name)} ({", ".join(_qcol(c) for c in columns)}) VALUES ({placeholders})'
            # 生成器直喂 executemany：序列化发生在 C 循环里，不再物化 serialized
            # 全表（旧实现这一步 0.6s + 一份全表内存）。快路径跳过 _serialize_for_sql
            # 的函数调用（str/int/float/None 原样透传，bool→int 同旧转换）。
            def _serialized_rows() -> Iterator[list[Any]]:
                for row in rows:
                    if len(row) != ncols:
                        continue
                    out = []
                    for v in row:
                        t = type(v)
                        if t is str or t is int or t is float or v is None:
                            out.append(v)
                        elif t is bool:
                            out.append(int(v))
                        else:
                            out.append(_serialize_for_sql(v))
                    yield out
            self._conn.executemany(insert_sql, _serialized_rows())
            # Deterministic, bounded index set.  Indexes are selected from
            # profile/request columns by the caller; never from model SQL.
            self._create_indexes_locked(name, columns, index_columns)
            self._conn.commit()

            schema = [acc.schema(col) for col, acc in zip(columns, accs)]
            info = TableInfo(name=name, columns=columns, schema=schema, row_count=matched)
            self._tables[name] = info
            self._revision += 1
            return info

    def _create_indexes_locked(self, name: str, columns: list[str],
                               index_columns: Optional[list[str]]) -> None:
        """The deterministic index set of ``load_table`` (caller holds the lock).

        Shared verbatim by ``load_table_from_snapshot`` so a snapshot-loaded
        table carries the same bounded indexes as a row-fed one (重载是
        DROP+CREATE，不带回 = 索引悄悄消失 —— 两条装载路径同一条契约).
        """
        for index, column in enumerate(dict.fromkeys(index_columns or [])):
            if column not in columns:
                continue
            digest = hashlib.sha1(f"{name}\x00{column}".encode("utf-8")).hexdigest()[:12]
            index_name = f"idx_{digest}"
            self._conn.execute(
                f'CREATE INDEX IF NOT EXISTS {_qcol(index_name)} ON {_qcol(name)} ({_qcol(column)})'
            )

    # ------------------------ snapshot loading (XP-01) -----------------------

    def load_table_from_snapshot(self, name: str, path: str | os.PathLike[str],
                                 index_columns: Optional[list[str]] = None) -> TableInfo:
        """Materialise a dataset from its on-disk snapshot/source into the engine.

        Dispatch by suffix:
          * ``.json``  → the canonical loader (``server.registry.load`` →
            ``table_input.load_table_file``) then ``load_table`` — the EXACT
            pre-XP-01 reload path, so validation/rename semantics cannot drift;
          * ``.sqlite`` → ATTACH + ``INSERT INTO main SELECT`` (DDL rebuilt from
            the meta table's column types), column statistics recomputed by SQL
            to bit parity with ``load_table(rows)``'s ``table_info`` (locked by
            tests/test_snapshot_sqlite.py);
          * anything else → the canonical loader too (构建期 canonical 快照是
            源文件拷贝：csv/xlsx 的装载语义与历史行为一致).

        NP-01 adopt/export logic is untouched (the cache fingerprint is still
        the snapshot FILE's sha256 — format-agnostic). A format flip therefore
        rebuilds that dataset's table cache once (fingerprint changed).
        """
        p = Path(path)
        suffix = p.suffix.lower()
        if suffix == ".sqlite":
            return self._load_sqlite_snapshot(name, p, index_columns)
        # .json（含历史快照与构建期 canonical 表 JSON）与 csv/xlsx 源拷贝：
        # 全部走 canonical 装载器（validate_table 同一真源），再 load_table。
        from .registry import load as _registry_load
        table = _registry_load(p)
        return self.load_table(name, list(table.get("columns") or []),
                               list(table.get("rows") or []),
                               index_columns=index_columns)

    def _snapshot_column_stats(self, table: str, col: str, sql_type: str,
                               inferred: str) -> ColumnSchema:
        """Recompute one column's stats by SQL, bit-parity with ``_ColAcc.schema``.

        ``_ColAcc`` computes on the PYTHON values BEFORE storage affinity:
        - it skips None and "" (empty string) values;
        - an INTEGER/REAL column's counted values were ALL numeric (that is what
          the type inference means), so after storage any leftover TEXT value in
          such a column came from a skipped "" (TEXT junk in a numeric column)
          and must not enter the stats — hence the numeric-storage-class filter;
        - a TEXT column's counted values are stored verbatim (TEXT affinity),
          so the plain non-empty filter reproduces ``non_null``/``distinct`` and
          lexicographic MIN/MAX reproduce the time ``sample_min/max``.

        MEASURE min/max/sum are accumulated in PYTHON over a rowid-ordered scan
        (one ordered pass per measure column): ``_ColAcc`` does a left-assoc
        float accumulation in row order, while SQLite ≥3.44 SUM uses
        Kahan-Babuška compensation — the two agree numerically but differ in
        the last bits on wide-magnitude columns (980k-row tickets2026 实测
        sum 尾差 ~1e-6 相对)，逐位 parity 必须走同一条累加路径。``float(v)``
        of the stored value is exact (IEEE double round-trip), so the
        accumulation is bit-identical to the row-fed path. Type/inferred
        verdicts come from the writer's meta — SQL storage cannot see Python
        bools (they are INTEGER on disk).

        Known lossy edge (documented): a Python float ``nan`` is stored as NULL
        by SQLite, so ``non_null`` can drop by the nan count — the engine table
        content is equally NULL (identical to ``load_table``'s own storage), so
        query results agree; only the schema stat could differ.
        """
        q = _qcol(col)
        if sql_type in ("INTEGER", "REAL"):
            guard = f'CASE WHEN typeof({q}) IN (\'integer\',\'real\') THEN 1 END'
            d_guard = f'CASE WHEN typeof({q}) IN (\'integer\',\'real\') THEN {q} END'
            num = f'CASE WHEN typeof({q}) IN (\'integer\',\'real\') THEN CAST({q} AS REAL) END'
            numeric_typed = True
        else:
            guard = f'CASE WHEN {q} IS NOT NULL AND {q} <> \'\' THEN 1 END'
            d_guard = f'CASE WHEN {q} IS NOT NULL AND {q} <> \'\' THEN {q} END'
            num = d_guard
            numeric_typed = False
        if inferred == "measure":
            # min/max/sum/mean：Python 行序累加（_ColAcc 同一路径），distinct
            # 仍由 SQL 现算（顺序无关，COUNT(DISTINCT) 精确）。
            num_min: float | None = None
            num_max: float | None = None
            total = 0.0
            non_null = 0
            for (v,) in self._conn.execute(f'SELECT {q} FROM {_qcol(table)}'):
                if v is None:
                    continue
                if numeric_typed and not isinstance(v, (int, float)):
                    continue  # 存储类过滤（数值列里的 TEXT 垃圾 = 被跳过的 ""）
                if not numeric_typed and v == "":
                    continue
                f = v if isinstance(v, float) else float(v)
                non_null += 1
                if num_min is None or f < num_min:
                    num_min = f
                if num_max is None or f > num_max:
                    num_max = f
                total += f
            distinct = int(self._conn.execute(
                f'SELECT COUNT(DISTINCT {d_guard}) FROM {_qcol(table)}').fetchone()[0] or 0)
            col_schema = ColumnSchema(
                name=col, sql_type=sql_type, inferred_type=inferred,
                non_null=non_null, distinct=distinct)
            if non_null:
                col_schema.min = num_min
                col_schema.max = num_max
                col_schema.sum = total
                col_schema.mean = total / non_null
            return col_schema
        row = self._conn.execute(
            f'SELECT COUNT({guard}) AS nn, COUNT(DISTINCT {d_guard}) AS d, '
            f'MIN({num}) AS mn, MAX({num}) AS mx '
            f'FROM {_qcol(table)}').fetchone()
        col_schema = ColumnSchema(
            name=col, sql_type=sql_type, inferred_type=inferred,
            non_null=int(row["nn"] or 0), distinct=int(row["d"] or 0))
        if inferred == "time" and col_schema.non_null:
            col_schema.sample_min = str(row["mn"] or "")[:10]
            col_schema.sample_max = str(row["mx"] or "")[:10]
        return col_schema

    def _load_sqlite_snapshot(self, name: str, path: Path,
                              index_columns: Optional[list[str]]) -> TableInfo:
        """ATTACH a SQLite snapshot, copy ``t`` into MAIN, recompute stats by SQL.

        Fail-closed: any meta/shape/count mismatch raises (the caller's reload
        loop surfaces it as a per-dataset WARN — a snapshot never silently
        loads as something else). Partial copies are rolled back before the
        error propagates (DROP main copy + DETACH).
        """
        alias = f"ai_snap_{int(time.time() * 1000) % 1_000_000}"
        with self._lock:
            attached = False
            copied = False
            try:
                self._conn.execute(f'ATTACH DATABASE ? AS {alias}', (str(path),))
                attached = True
                meta = dict(self._conn.execute(
                    f'SELECT key, value FROM {alias}.{_SNAPSHOT_META_TABLE}').fetchall())
                if str(meta.get("format")) != "lrm-snapshot":
                    raise ValueError(f"不是 LRM SQLite 快照：{path.name}")
                colmeta = json.loads(meta.get("columns") or "[]")
                if not isinstance(colmeta, list) or not colmeta:
                    raise ValueError(f"快照元数据缺少列清单：{path.name}")
                cols = [str(c["name"]) for c in colmeta]
                types = {str(c["name"]): str(c.get("type") or "TEXT").upper()
                         for c in colmeta}
                inferred = {str(c["name"]): str(c.get("inferred") or "dimension")
                            for c in colmeta}
                declared_rows = int(meta.get("rowCount") or 0)
                ddl_cols = ", ".join(f'{_qcol(c)} {types[c]}' for c in cols)
                self._conn.execute(f'DROP TABLE IF EXISTS main.{_qcol(name)}')
                self._conn.execute(f'CREATE TABLE main.{_qcol(name)} ({ddl_cols})')
                copied = True
                self._conn.execute(
                    f'INSERT INTO main.{_qcol(name)} SELECT * FROM {alias}."t"')
                self._create_indexes_locked(name, cols, index_columns)
                count = int(self._conn.execute(
                    f'SELECT COUNT(*) FROM main.{_qcol(name)}').fetchone()[0])
                if declared_rows and count != declared_rows:
                    raise ValueError(
                        f"快照行数不符（meta {declared_rows} ≠ 表 {count}）：{path.name}")
                self._conn.commit()
                schema = [self._snapshot_column_stats(name, c, types[c], inferred[c])
                          for c in cols]
                info = TableInfo(name=name, columns=cols, schema=schema, row_count=count)
                self._tables[name] = info
                self._revision += 1
                self._conn.execute(f'DETACH DATABASE {alias}')
                attached = False
                return info
            except (sqlite3.Error, KeyError, ValueError, TypeError) as exc:
                try:
                    if copied:
                        self._conn.execute(f'DROP TABLE IF EXISTS main.{_qcol(name)}')
                    if attached:
                        self._conn.commit()
                        self._conn.execute(f'DETACH DATABASE {alias}')
                except sqlite3.Error:
                    pass
                if isinstance(exc, sqlite3.Error):
                    raise OSError(f"SQLite 快照装载失败：{path.name}: {exc}") from exc
                raise

    def execute(self, sql: str, params: tuple = (), *, cancel_check=None,
                timeout_seconds: float | None = None) -> list[dict[str, Any]]:
        with self._lock:
            deadline = time.monotonic() + timeout_seconds if timeout_seconds else None
            cancelled = False
            def _progress():
                nonlocal cancelled
                if cancel_check is not None and cancel_check():
                    cancelled = True
                    return 1
                if deadline is not None and time.monotonic() >= deadline:
                    cancelled = True
                    return 1
                return 0
            if cancel_check is not None or deadline is not None:
                self._conn.set_progress_handler(_progress, 1000)
            try:
                cur = self._conn.execute(sql, params)
                if cur.description is None:
                    return []
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
            except sqlite3.OperationalError as exc:
                if cancelled:
                    raise TimeoutError("查询已取消或超过时间预算") from exc
                raise
            finally:
                if cancel_check is not None or deadline is not None:
                    self._conn.set_progress_handler(None, 0)

    def execute_mutation(self, sql: str, params: tuple = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            self._revision += 1
            return cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0

    def list_tables(self) -> list[str]:
        return list(self._tables.keys())

    def table_info(self, name: str) -> Optional[TableInfo]:
        return self._tables.get(name)

    def drop_table(self, name: str) -> None:
        with self._lock:
            self._conn.execute(f'DROP TABLE IF EXISTS {_qcol(name)}')
            self._tables.pop(name, None)
            self._revision += 1

    # ---------------- persistent table cache (NP-01) -------------------------

    def _info_payload(self, name: str) -> dict[str, Any]:
        """Full dataclass round-trip of a TableInfo (to_dict drops None fields)."""
        import dataclasses
        info = self._tables[name]
        return {"name": info.name, "columns": list(info.columns),
                "schema": [dataclasses.asdict(c) for c in info.schema],
                "row_count": info.row_count}

    @staticmethod
    def _info_from_payload(payload: dict[str, Any]) -> "TableInfo":
        schema = [ColumnSchema(**{k: v for k, v in item.items()})
                  for item in (payload.get("schema") or [])]
        return TableInfo(name=str(payload.get("name") or ""),
                         columns=[str(c) for c in (payload.get("columns") or [])],
                         schema=schema,
                         row_count=int(payload.get("row_count") or 0))

    def adopt_table_cache(self, name: str, cache_file: str | os.PathLike[str],
                          fingerprint: str, decl_hash: str,
                          cache_version: str = ENGINE_CACHE_VERSION) -> Optional[TableInfo]:
        """Restore ``name`` from a verified cache file instead of a snapshot reload.

        Verifies the embedded meta (fingerprint / decl hash / cache version),
        ``PRAGMA quick_check``, table presence and row count, then copies the
        table into the engine's MAIN database (verbatim DDL + C-level
        ``INSERT … SELECT`` + verbatim index DDL) and detaches. Returns the
        restored TableInfo, or None when the cache is absent/stale/corrupt —
        the caller then reloads from the snapshot (fail-closed, never serves
        an unverified cache).
        """
        path = Path(cache_file)
        if not path.exists():
            return None
        alias = f"ai_adopt_{int(time.time() * 1000) % 1_000_000}"
        with self._lock:
            copied = False
            attached = False
            restored: Optional[TableInfo] = None
            try:
                self._conn.execute(f'ATTACH DATABASE ? AS {alias}', (str(path),))
                attached = True
                meta = dict(self._conn.execute(
                    f'SELECT key, value FROM {alias}.{_META_TABLE}').fetchall())
                verified = (
                    str(meta.get("cache_version")) == str(cache_version)
                    and str(meta.get("fingerprint")) == str(fingerprint)
                    and str(meta.get("decl_hash")) == str(decl_hash)
                    and str(meta.get("table")) == str(name))
                if verified:
                    ddl = _table_ddl(self._conn, alias, name)
                    check = self._conn.execute(
                        f'PRAGMA {alias}.quick_check(1)').fetchall()
                    info = None
                    try:
                        info = self._info_from_payload(json.loads(meta["info"]))
                    except (KeyError, ValueError, TypeError):
                        verified = False
                    verified = bool(
                        verified and ddl and info is not None
                        and [str(r[0]) for r in check] == ["ok"]
                        and info.name == name)
                if verified:
                    # Copy into MAIN so every downstream consumer (unqualified
                    # reads, index-advisor CREATE INDEX, row deletes, cross-table
                    # joins, derived replay) keeps the exact pre-NP-01 semantics.
                    self._conn.execute(f'DROP TABLE IF EXISTS main.{_qcol(name)}')
                    self._conn.execute(ddl)  # verbatim: affinities/quoting preserved
                    copied = True
                    self._conn.execute(
                        f'INSERT INTO main.{_qcol(name)} '
                        f'SELECT * FROM {alias}.{_qcol(name)}')
                    for index_ddl in _table_index_ddls(self._conn, alias, name):
                        self._conn.execute(index_ddl)
                    count = self._conn.execute(
                        f'SELECT COUNT(*) FROM main.{_qcol(name)}').fetchone()[0]
                    if int(count) != int(info.row_count):
                        raise ValueError("cached row count mismatch")
                    self._conn.commit()
                    self._conn.execute(f'DETACH DATABASE {alias}')
                    attached = False
                    self._tables[name] = info
                    self._revision += 1
                    restored = info
            except (sqlite3.Error, KeyError, ValueError, TypeError):
                restored = None
            if restored is None and (attached or copied):
                # Fail-closed tail: drop any partial main copy, detach the alias.
                try:
                    if copied:
                        self._conn.execute(f'DROP TABLE IF EXISTS main.{_qcol(name)}')
                    if attached:
                        self._conn.commit()
                        self._conn.execute(f'DETACH DATABASE {alias}')
                except sqlite3.Error:
                    pass
            return restored

    def export_table_cache(self, name: str, cache_file: str | os.PathLike[str],
                           fingerprint: str, decl_hash: str,
                           cache_version: str = ENGINE_CACHE_VERSION) -> bool:
        """Atomically publish ``name`` (post-replay content) as a cache file.

        Builds a fresh ``<file>.tmp-<pid>`` database on its own connection
        (``CREATE TABLE`` cannot be schema-qualified, so the tmp file's main db
        is the writer and the engine's db file is ATTACHed read-only as the
        source), copies the table + indexes + meta, sanity-checks it, then
        ``os.replace``s it into place. Returns False (cache simply absent this
        boot) on any failure — the loaded table in the engine is untouched.
        """
        if name not in self._tables:
            return False
        if not self._file_backed:
            # A ``:memory:`` engine db cannot be ATTACHed by the tmp writer
            # connection; skip caching (behavior falls back to plain reload).
            return False
        path = Path(cache_file)
        # tmp 名带 time_ns：pid 唯一只隔离进程，同进程两个引擎实例（测试/未来
        # 多引擎）并发导出时也绝不互踩对方的临时文件。
        tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{time.time_ns()}")
        with self._lock:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    tmp.unlink()
                except FileNotFoundError:
                    pass
                writer = sqlite3.connect(str(tmp))
                try:
                    writer.execute("ATTACH DATABASE ? AS engine_main",
                                   (str(self._db_path),))
                    ddl = _table_ddl(writer, "engine_main", name)
                    if not ddl:
                        return False
                    writer.execute(ddl)
                    writer.execute(
                        f'INSERT INTO {_qcol(name)} '
                        f'SELECT * FROM engine_main.{_qcol(name)}')
                    for index_ddl in _table_index_ddls(writer, "engine_main", name):
                        writer.execute(index_ddl)
                    writer.execute(
                        f'CREATE TABLE {_META_TABLE} '
                        f'(key TEXT PRIMARY KEY, value TEXT)')
                    meta = {
                        "cache_version": str(cache_version),
                        "table": str(name),
                        "fingerprint": str(fingerprint),
                        "decl_hash": str(decl_hash),
                        "row_count": str(self._tables[name].row_count),
                        "info": json.dumps(self._info_payload(name), ensure_ascii=False),
                        "exported_at": str(time.time()),
                    }
                    writer.executemany(
                        f'INSERT INTO {_META_TABLE} (key, value) VALUES (?, ?)',
                        list(meta.items()))
                    writer.commit()
                    check = writer.execute('PRAGMA quick_check(1)').fetchall()
                    if [str(r[0]) for r in check] != ["ok"]:
                        return False
                finally:
                    writer.close()  # release the tmp handle before os.replace
                os.replace(tmp, path)
                return True
            except (sqlite3.Error, OSError, ValueError, TypeError):
                try:
                    tmp.unlink()
                except OSError:
                    pass
                return False

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    @property
    def engine_name(self) -> str:
        return "sqlite"



# ---------------------- DuckDB (optional) ---------------------------------------

class DuckDBEngine(Engine):
    # Persistent table cache is SQLite-only (NP-01): duckdb keeps the legacy
    # reload path; run_server consults engine_supports_table_cache().
    supports_table_cache = False

    def __init__(self, db_path: str | os.PathLike[str] = ":memory:"):
        import duckdb  # type: ignore[import-not-found]
        self._duckdb = duckdb
        self._conn = duckdb.connect(str(db_path))
        # duckdb 连接不允许跨线程并发使用；ThreadingHTTPServer 下两个图表同时
        # fetchAggregate 就是竞态。与 SQLiteEngine 同一条「每方法持锁」纪律。
        self._lock = threading.RLock()
        self._tables: dict[str, TableInfo] = {}
        self._revision = 0
        # XC-01: unique per-instance identity for aggregator cache keys and
        # in-flight markers (never reuse ``id()`` — CPython recycles ids).
        self.instance_token = uuid.uuid4().hex
        self.supports_date_norm = _register_date_norm(self._conn, dialect="duckdb", module=duckdb)
        self.supports_num_norm = _register_num_norm(self._conn, dialect="duckdb", module=duckdb)
        self.supports_week_norm = _register_week_norm(self._conn, dialect="duckdb", module=duckdb)
        self.supports_calendar_norm = _register_calendar_norm(self._conn, dialect="duckdb", module=duckdb)
        self.supports_date_check = _register_date_check(self._conn, dialect="duckdb", module=duckdb)

    def load_table(self, name: str, columns: list[str], rows: list[list[Any]],
                   index_columns: list[str] | None = None) -> TableInfo:
        del index_columns  # duckdb 分支不建索引；签名与 SQLiteEngine 对齐（否则 h_register 传参即 TypeError）
        if len(set(columns)) != len(columns):
            _dupes = sorted({c for c in columns if columns.count(c) > 1})
            raise TableSchemaError(
                "duplicate column names in table payload: " + ", ".join(map(str, _dupes[:5]))
                + "（重复列名会让 CREATE TABLE 失败并摧毁既有同名表）")
        col_samples: list[list[Any]] = [[] for _ in columns]
        for row in rows:
            for i, v in enumerate(row):
                if i < len(col_samples):
                    col_samples[i].append(v)
        schema: list[ColumnSchema] = []
        for col, sample in zip(columns, col_samples):
            sql_t = _infer_sql_type(sample)
            inf_t = _infer_inferred_type(sample)
            col_schema = ColumnSchema(
                name=col,
                sql_type=sql_t,
                inferred_type=inf_t,
                non_null=len(_coerce_sample(sample)),
                distinct=len({str(v) for v in _coerce_sample(sample)}),
            )
            if inf_t == "measure":
                nums = [float(v) for v in _coerce_sample(sample)]
                if nums:
                    col_schema.min = min(nums)
                    col_schema.max = max(nums)
                    col_schema.mean = sum(nums) / len(nums)
                    col_schema.sum = sum(nums)
            elif inf_t == "time":
                ordered = sorted(str(v) for v in _coerce_sample(sample))
                if ordered:
                    col_schema.sample_min = ordered[0][:10]
                    col_schema.sample_max = ordered[-1][:10]
            schema.append(col_schema)

        with self._lock:
            self._conn.execute(f'DROP TABLE IF EXISTS {_qcol(name)}')
            defs = ", ".join(f'{_qcol(c)} {_infer_sql_type(s)}' for c, s in zip(columns, col_samples))
            self._conn.execute(f'CREATE TABLE {_qcol(name)} ({defs})')
            placeholders = ", ".join("?" for _ in columns)
            self._conn.executemany(
                f'INSERT INTO {_qcol(name)} ({", ".join(_qcol(c) for c in columns)}) VALUES ({placeholders})',
                [[_serialize_for_sql(v) for v in row] for row in rows if len(row) == len(columns)],
            )
            # 表元数据与 DROP+CREATE 同一把锁：锁外写会让并发请求瞬间按旧列清单
            # 查新表（SQLiteEngine.load_table 同款纪律）。
            info = TableInfo(name=name, columns=columns, schema=schema,
                             row_count=len([row for row in rows if len(row) == len(columns)]))
            self._tables[name] = info
            self._revision += 1
        return info

    def execute(self, sql: str, params: tuple = (), *, cancel_check=None,
                timeout_seconds: float | None = None) -> list[dict[str, Any]]:
        if cancel_check is not None and cancel_check():
            raise TimeoutError("查询已取消")
        with self._lock:
            cur = self._conn.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def execute_mutation(self, sql: str, params: tuple = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            self._revision += 1
            # duckdb 的 DELETE 返回的是「Changed N rows」计数对象；rowcount 由
            # DB-API 承载，不可判定时按 0 兜底（调用方随后用 COUNT(*) 复核）。
            return cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0

    def list_tables(self) -> list[str]:
        return list(self._tables.keys())

    def table_info(self, name: str) -> Optional[TableInfo]:
        return self._tables.get(name)

    def drop_table(self, name: str) -> None:
        with self._lock:
            self._conn.execute(f'DROP TABLE IF EXISTS {_qcol(name)}')
            self._tables.pop(name, None)
            self._revision += 1

    def close(self) -> None:
        self._conn.close()

    @property
    def engine_name(self) -> str:
        return "duckdb"


# ---------------------- factory -------------------------------------------------

_engine_singleton: Engine | None = None


def create_engine(use_duckdb: bool = False, db_path: str | os.PathLike[str] = ":memory:") -> Engine:
    """Create an engine. By default SQLite; DuckDB if requested AND available.

    Returns SQLiteEngine when duckdb is not installed (warning printed once).
    """
    if not use_duckdb:
        return SQLiteEngine(db_path)
    try:
        import duckdb  # noqa: F401  (probe)
        return DuckDBEngine(db_path)
    except ImportError:
        import sys
        print("duckdb not installed; falling back to SQLite. pip install duckdb for 5-10x faster aggregations.", file=sys.stderr)
        return SQLiteEngine(db_path)


def get_engine(db_path: str | os.PathLike[str] = ":memory:") -> Engine:
    """Process-singleton engine (one per server process)."""
    global _engine_singleton
    if _engine_singleton is None:
        _engine_singleton = create_engine(db_path=db_path)
    return _engine_singleton


def reset_engine() -> None:
    """Test helper."""
    global _engine_singleton
    if _engine_singleton is not None:
        _engine_singleton.close()
    _engine_singleton = None


def engine_load_from_snapshot(engine: Engine, name: str, path: str | os.PathLike[str],
                              index_columns: Optional[list[str]] = None) -> TableInfo:
    """Reload one dataset from its snapshot file into ``engine``.

    The single entry the reload paths share (``run_server._reload_all`` /
    ``app._reload_dataset`` / cold ``--register-report`` via the boot reload).
    Engines implementing ``load_table_from_snapshot`` (SQLiteEngine) dispatch
    by snapshot suffix (json → canonical loader, sqlite → ATTACH copy); legacy
    engines (DuckDB) keep the untouched pre-XP-01 read-then-``load_table`` path.

    Ordering contract: READ FIRST, then DROP+CREATE inside ``load_table`` — a
    snapshot that fails to PARSE leaves the engine's existing table intact
    (历史行为；解析失败绝不能放大成「连旧表一起丢」).
    """
    loader = getattr(engine, "load_table_from_snapshot", None)
    if loader is not None:
        return loader(name, path, index_columns=index_columns)
    from .registry import load as _registry_load
    table = _registry_load(path)
    engine.drop_table(name)
    return engine.load_table(name, list(table.get("columns") or []),
                             list(table.get("rows") or []),
                             index_columns=index_columns)


# ---------------------- derived columns (former server/derived_columns.py) -------
#
# Merged NQ-02 wave 2, zero behavior change: the module's public names
# (DERIVED_KIND, columns_decl, is_derived_columns, apply_dataset_derived,
# table_defs, ensure_table_defs, ensure_dim_groups, replay_all, dim_groups_defs,
# dim_group_table_defs, apply_dim_groups) and their bodies are unchanged — only
# the module shell is absorbed. The former module's sys.path bootstrap for
# scripts/ is redundant here (store.py already inserts scripts/ at import, and
# the lazy ``from derived_eval import …`` below relies on exactly that), and the
# lazy ``from .store import _qcol`` self-imports inside ensure_table_defs /
# ensure_dim_groups resolve to this module's own _qcol.

"""Server-side row-level derived columns (数据集「计算列」).

A server report serves a SCHEMA-ONLY manifest, so the browser never sees the
rows — but the server holds every source table in SQLite (``h_register`` /
``h_dataset_add`` both go through ``store.Engine.load_table``), so a row-level
derived column is materialised HERE. Without it, a request declaring
``datasets[].derived.columns`` produced a manifest whose column list contained
e.g. ``净流入`` while the SQLite table did not → every aggregate on it died with
``no such column``. Server mode used to reject such requests outright
(``report_build.py`` / ``serve_report.py``), which made "派生统一到数据源管理"
impossible for server reports.

Design constraint: ZERO parity risk
-----------------------------------
This module reuses ``scripts/derived_eval.materialize`` — the very function
``spec_to_report._materialize_derived`` calls. There is exactly ONE
implementation, so the SQLite column is byte-identical to the compiled/static
one. Do NOT re-implement the arithmetic here (or as generated SQL).

Where the declaration lives — and why it is a TABLE-level union
----------------------------------------------------------------
The registry entry records it (``datasets[i].derived = {kind:'columns', defs}``)
and the snapshot ``source`` file stays RAW (it is a copy of the user's upload),
so the declaration must be replayed on every load/reload — mirroring
``server/relations.py``'s replay for wide tables.

BUT report copies (保存为副本 / 版本) inherit their parent's ``datasets[]``:
several reports can therefore share ONE dataset id — and the engine table is
keyed by that id, while ``load_table`` is DROP+CREATE. Loading per entry is
last-writer-wins, so a copy WITHOUT a declaration silently erased the base
report's derived column on the next boot (reproduced on the real 335k-row PSI
report: 6 copies reloaded the same ``PSI__primary`` table). Hence the rule:

  * the file-backed pass loads tables only;
  * a SECOND pass applies the UNION of the declarations that point at each
    table id — exactly the two-pass doctrine already used for relations.

A name declared twice with different formulas resolves to the first registered
report (registry order); a column is never dropped from a shared table.
"""

# Registry/payload marker. ``kind`` distinguishes this from the wide-table
# 'relation' marker so ``app._is_derived_ds`` / ``_prune_derived_datasets``
# (which are about relation datasets that have NO source file) never touch a
# columns-derived dataset — those DO have a source and reload normally.
DERIVED_KIND = "columns"


def columns_decl(value: Any) -> dict | None:
    """Normalise a ``derived`` payload into ``{kind:'columns', defs:[...]}``.

    Returns ``None`` when the payload is absent/empty or is another kind
    ('relation'), so callers can use it as a plain guard. Tolerates the
    manifest slot shape ``{columns:[...]}`` so a manifest dataset's ``derived``
    object can be passed straight through.

    **Only the row-level ``columns`` slot is materialised here** — the
    aggregate-level ``derived.measures`` are NOT the server's business: they are
    evaluated after aggregation by the SAME evaluator on the compile side and in
    the client (``scripts/derived_eval.evaluate`` / ``chart-adapter.derivedEvaluate``),
    so persisting them as a registry *table* declaration would be dead data that
    could drift. They still survive a restart / report copy because the served
    manifest (registry entry ``manifest.datasets[].derived``) carries them
    verbatim — see ``tests/test_server_derived_columns.py::test_manifest_keeps_measures``.
    """
    if not isinstance(value, dict):
        return None
    if value.get("kind") not in (None, DERIVED_KIND):
        return None
    defs = value.get("defs")
    if defs is None:
        defs = value.get("columns")
    defs = [d for d in (defs or []) if isinstance(d, dict) and d.get("name")]
    if not defs:
        return None
    return {"kind": DERIVED_KIND, "defs": [dict(d) for d in defs]}


def is_derived_columns(d: Any) -> bool:
    return isinstance(d, dict) and (d.get("derived") or {}).get("kind") == DERIVED_KIND


def apply_dataset_derived(table: dict, derived: Any) -> dict:
    """Materialise declared 计算列 onto a freshly built ``{columns, rows}`` table.

    Idempotent: a def whose column already exists is skipped, so a payload that
    already carries the column (or a double apply) cannot double-append. Broken
    declarations still raise — they must surface, not vanish.
    """
    decl = columns_decl(derived)
    if not decl or not isinstance(table, dict):
        return table
    present = set(table.get("columns") or [])
    pending = [d for d in decl["defs"] if d.get("name") not in present]
    if not pending:
        return table
    from derived_eval import materialize  # noqa: PLC0415  (scripts/ on sys.path above)

    return materialize(table, pending)


def table_defs(entries: Iterable[dict], ds_id: str, extra: Any = None) -> list[dict]:
    """Union of 计算列 defs declared for ONE table id across every registry entry.

    ``entries`` may be registry entries (``{datasets:[...]}``) or dataset dicts
    directly. First declaration wins per column name — deterministic because
    registry order is insertion order (base report registered before its
    copies).
    """
    out: list[dict] = []
    seen: set[str] = set()

    def feed(value: Any) -> None:
        decl = columns_decl(value)
        if not decl:
            return
        for item in decl["defs"]:
            name = item["name"]
            if name in seen:
                continue
            seen.add(name)
            out.append(item)

    feed(extra)
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        datasets = entry.get("datasets") if "datasets" in entry else [entry]
        for d in datasets or []:
            if isinstance(d, dict) and d.get("id") == ds_id:
                feed(d.get("derived"))
    return out


def ensure_table_defs(engine, ds_id: str, defs: list[dict],
                      index_columns: list[str] | None = None) -> int:
    # index_columns 透传给 load_table：补列走 drop+load 重建，不把原表索引带回
    # 的话，大表的筛选/分组索引会在一次 replay 后静默消失。
    """Materialise any of ``defs`` still missing from a loaded engine table.

    Returns the number of columns added (0 = nothing to do). Reads the table
    back through the engine so a table shared by several reports converges to
    the union of their declarations instead of the last writer's shape.
    """
    if not defs:
        return 0
    info = engine.table_info(ds_id)
    if info is None:
        return 0
    have = set(info.columns)
    missing = [d for d in defs if d.get("name") not in have]
    if not missing:
        return 0
    from derived_eval import materialize  # noqa: PLC0415

    cols = list(info.columns)
    raw = engine.execute(f'SELECT {", ".join(_qcol(c) for c in cols)} FROM {_qcol(ds_id)}')
    table = {"columns": cols, "rows": [[r[c] for c in cols] for r in raw]}
    out = materialize(table, missing)
    engine.drop_table(ds_id)
    engine.load_table(ds_id, out["columns"], out["rows"], index_columns=list(index_columns or []))
    return len(missing)


def ensure_dim_groups(engine, ds_id: str, defs: list[dict],
                      index_columns: Optional[list[str]] = None) -> int:
    """Materialise any 维度值分组 defs still missing from a loaded engine table."""
    if not defs:
        return 0
    info = engine.table_info(ds_id)
    if info is None:
        return 0
    have = set(info.columns)
    missing = [d for d in defs if d.get("name") not in have]
    if not missing:
        return 0
    from derived_eval import materialize_dim_groups  # noqa: PLC0415

    cols = list(info.columns)
    raw = engine.execute(f'SELECT {", ".join(_qcol(c) for c in cols)} FROM {_qcol(ds_id)}')
    table = {"columns": cols, "rows": [[r[c] for c in cols] for r in raw]}
    out = materialize_dim_groups(table, missing)
    engine.drop_table(ds_id)
    engine.load_table(ds_id, out["columns"], out["rows"], index_columns=list(index_columns or []))
    return len(missing)


def replay_all(engine, registry, verbose: bool = False) -> list[str]:
    """Apply the UNION of 计算列 declarations to every loaded table.

    Call AFTER the file-backed load pass (the sources must exist first) and
    before relations are replayed — same ordering rule as
    ``server/relations.replay_entry``.

    Deliberately table-centric rather than entry-centric: report copies share
    dataset ids, so replaying one entry would leave a table reloaded by another
    report without its column. Returns the dataset ids it touched.
    """
    entries = registry.raw_entries() if hasattr(registry, "raw_entries") else list(registry or [])
    ds_ids = sorted({d["id"] for e in entries if isinstance(e, dict)
                     for d in (e.get("datasets") or []) if isinstance(d, dict) and d.get("id")})
    touched: list[str] = []
    for ds_id in ds_ids:
        defs = table_defs(entries, ds_id)
        gdefs = dim_group_table_defs(entries, ds_id)
        if not defs and not gdefs:
            continue
        # 该表在注册表里声明的索引列（h_register/h_dataset_replace 写进 meta）：
        # 重建时带回，索引不因 replay 消失。
        idx_cols: list[str] = []
        for e2 in entries:
            if not isinstance(e2, dict):
                continue
            for d2 in (e2.get("datasets") or []):
                if isinstance(d2, dict) and d2.get("id") == ds_id:
                    for c in (d2.get("indexColumns") or []):
                        if c not in idx_cols:
                            idx_cols.append(c)
        try:
            if ensure_table_defs(engine, ds_id, defs, index_columns=idx_cols):
                touched.append(ds_id)
        except Exception as e:  # noqa: BLE001  (best-effort boot/reload path)
            if verbose:
                print(f"[ai-report-server] WARN 计算列 replay {ds_id!r} failed: {e}",
                      file=sys.stderr)
        try:
            if ensure_dim_groups(engine, ds_id, gdefs, index_columns=idx_cols):
                touched.append(ds_id)
        except Exception as e:  # noqa: BLE001
            if verbose:
                print(f"[ai-report-server] WARN 维度分组 replay {ds_id!r} failed: {e}",
                      file=sys.stderr)
    return touched


def dim_groups_defs(value: Any) -> list[dict] | None:
    """Normalise a ``ds.dimGroups`` payload into a plain list (None = absent)."""
    if not isinstance(value, list) or not value:
        return None
    defs = [d for d in value if isinstance(d, dict) and d.get("name") and d.get("source")]
    return defs or None


def dim_group_table_defs(entries: Iterable[dict], ds_id: str, extra: Any = None) -> list[dict]:
    """Union of 维度值分组 defs for ONE table id across registry entries（先到先得）。

    与 table_defs 同一条并集语义：副本共享表，只补缺失列、永不删列。
    """
    out: list[dict] = []
    seen: set[str] = set()

    def feed(value: Any) -> None:
        defs = dim_groups_defs(value)
        if not defs:
            return
        for item in defs:
            name = item["name"]
            if name in seen:
                continue
            seen.add(name)
            out.append(item)

    feed(extra)
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        datasets = entry.get("datasets") if "datasets" in entry else [entry]
        for d in datasets or []:
            if isinstance(d, dict) and d.get("id") == ds_id:
                feed(d.get("dimGroups"))
    return out


def apply_dim_groups(table: dict, defs: list[dict]) -> dict:
    """Materialise 维度值分组 defs onto a freshly built table (idempotent)."""
    if not defs or not isinstance(table, dict):
        return table
    present = set(table.get("columns") or [])
    pending = [d for d in defs if d.get("name") not in present]
    if not pending:
        return table
    from derived_eval import materialize_dim_groups  # noqa: PLC0415

    return materialize_dim_groups(table, pending)
