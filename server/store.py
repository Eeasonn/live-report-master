"""Data engine: load canonical tables into file-backed SQLite.

The engine is intentionally a small surface. Higher layers (aggregator,
truth, HTTP handler) compose these primitives.

SQLite is the v1 engine (Python stdlib, zero install). The public launcher
never selects an alternate engine.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from .types import ColumnSchema, TableInfo


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


# ---------------------- SQLite --------------------------------------------------

class SQLiteEngine(Engine):
    def __init__(self, db_path: str | os.PathLike[str] = ":memory:"):
        # check_same_thread=False is required because ThreadingHTTPServer
        # dispatches across worker threads. The internal lock protects
        # every operation.
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        if str(db_path) != ":memory:":
            # File-backed reports are read-heavy and may serve concurrent chart
            # requests. WAL keeps readers non-blocking while the registry reloads
            # a dataset; NORMAL is the balanced durability setting for a local
            # report cache.
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        self._lock = threading.Lock()
        self._tables: dict[str, TableInfo] = {}
        self._revision = 0
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
            for index, column in enumerate(dict.fromkeys(index_columns or [])):
                if column not in columns:
                    continue
                digest = hashlib.sha1(f"{name}\x00{column}".encode("utf-8")).hexdigest()[:12]
                index_name = f"idx_{digest}"
                self._conn.execute(
                    f'CREATE INDEX IF NOT EXISTS {_qcol(index_name)} ON {_qcol(name)} ({_qcol(column)})'
                )
            self._conn.commit()

            schema = [acc.schema(col) for col, acc in zip(columns, accs)]
            info = TableInfo(name=name, columns=columns, schema=schema, row_count=matched)
            self._tables[name] = info
            self._revision += 1
            return info

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
    def __init__(self, db_path: str | os.PathLike[str] = ":memory:"):
        import duckdb  # type: ignore[import-not-found]
        self._duckdb = duckdb
        self._conn = duckdb.connect(str(db_path))
        # duckdb 连接不允许跨线程并发使用；ThreadingHTTPServer 下两个图表同时
        # fetchAggregate 就是竞态。与 SQLiteEngine 同一条「每方法持锁」纪律。
        self._lock = threading.RLock()
        self._tables: dict[str, TableInfo] = {}
        self._revision = 0
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
