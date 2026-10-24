"""Cross-report alias learning directory (XA-02 · roadmap NA-06).

A tenant-scoped, strictly read-only suggestion store: when a user explicitly
confirms a field alias in one report (``alias_confirmation`` ProposalTransaction),
the observation is recorded here so *structurally identical* datasets in other
reports can surface it as a suggestion.  The suggestion never auto-resolves:
``confidence`` is a monotone function of confirmations and is hard-capped at
0.9 — every learned row comes back with ``requiresConfirmation: True`` and the
existing ``resolve_field`` → ``propose_alias_confirmation`` gate stays the only
path to persistence (docs/product-optimization-roadmap-2026-10-23.md NA-06:
tenant-scoped、只读建议、requiresConfirmation 闸不变、本地 SQLite 落 state home).

Privacy: the learning directory NEVER learns sensitive columns.  The write gate
lives at the single apply chokepoint (``agent_proposals._apply_alias_confirmation``,
which owns the dataset's ``privacy.sensitiveColumns``); ``suggest`` consumers
filter canonical names against the same column universe (NS-01).

Failure policy: the store is an observational enhancement.  Every failure mode
(missing dir, corrupt DB, closed env switch) is fail-closed — record returns
False, suggest returns [] — and never raises into the proposal transaction or
the tool runner.  Corruption warns once per process.

Env:
  LRM_STATE_HOME       state home root (same switch as server/registry.py)
  LRM_TENANT           tenant scope (default "local"; reserved for multi-tenant)
  LRM_ALIAS_LEARNING   "0" disables the store (reads and writes become no-ops)
"""
from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .agent_extensions import normalize_alias_key, normalize_alias_text

_logger = logging.getLogger("lrm.alias_learning")

_DB_NAME = "alias-learning.sqlite3"
_MAX_ROWS_PER_SIG = 200          # 每 (tenant, dataset_sig) 上限，LRU by last_seen
_MAX_SUGGESTIONS = 5
_CONFIDENCE_BASE = 0.5           # 1 次确认
_CONFIDENCE_STEP = 0.1
_CONFIDENCE_CAP = 0.9            # 永不达 1.0：学习建议永不自动采用
_FUZZY_RATIO = 0.80              # 近似命中下限（exact 优先）
_SEPARATOR = "\x1f"              # 列名拼接分隔符（控制字符不可能出现在列名里）

_corruption_warned = False
_write_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS alias_obs (
  tenant TEXT,
  dataset_sig TEXT,
  canonical TEXT,
  alias TEXT,
  confirmations INT,
  last_seen REAL,
  sample_report TEXT,
  PRIMARY KEY (tenant, dataset_sig, canonical, alias)
)
"""


def enabled() -> bool:
    """LRM_ALIAS_LEARNING=0 (or false/no) turns the whole store into a no-op."""
    return str(os.environ.get("LRM_ALIAS_LEARNING", "")).strip().lower() \
        not in {"0", "false", "no", "off"}


def default_state_home() -> Path:
    """Same resolution rule as server/registry.py::_USER_STATE / _platform.py."""
    return Path(os.environ.get("LRM_STATE_HOME") or (Path.home() / ".ai-html-report"))


def tenant_id() -> str:
    """Tenant scope; reserved multi-tenant key, defaults to the local tenant."""
    raw = os.environ.get("LRM_TENANT") or ""
    text = normalize_alias_text(raw) or "local"
    return text


def dataset_signature(dataset_columns: Iterable[Any]) -> str:
    """Stable signature of a dataset's column-name set (sorted sha1, first 12).

    Same-shape datasets across reports share a signature (so learning hits),
    heterogeneous datasets never cross-contaminate.  Column *order* and
    duplicates are irrelevant by construction.
    """
    names = sorted({str(item) for item in (dataset_columns or []) if str(item)})
    return hashlib.sha1(_SEPARATOR.join(names).encode("utf-8")).hexdigest()[:12]


def _warn_corruption_once(exc: Exception) -> None:
    global _corruption_warned
    if not _corruption_warned:
        _corruption_warned = True
        _logger.warning("alias-learning store corrupt; failing closed (%s)", exc)


def _connect(state_home: Any) -> sqlite3.Connection:
    root = Path(state_home)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    conn = sqlite3.connect(str(root / _DB_NAME), timeout=2)
    return conn


def record_confirmation(state_home: Any, tenant: str, dataset_columns: Iterable[Any],
                        canonical: Any, alias: Any, report_id: Any = "") -> bool:
    """Record one applied alias confirmation (the ONLY write path).

    Called exclusively from the ``alias_confirmation`` ProposalTransaction success
    branch (``agent_proposals._apply_alias_confirmation``) — never from preview or
    validation.  Sensitive-column rejection lives at that call site (it owns the
    dataset privacy declaration); here we additionally reject anything that is not
    a real column of the declaring dataset and trivial self-aliases.
    """
    if not enabled():
        return False
    canonical_text = normalize_alias_text(canonical)
    alias_text = normalize_alias_text(alias)
    if not canonical_text or not alias_text:
        return False
    if normalize_alias_key(canonical_text) == normalize_alias_key(alias_text):
        return False
    columns = [str(item) for item in (dataset_columns or []) if str(item)]
    column_keys = {normalize_alias_key(item) for item in columns if normalize_alias_key(item)}
    if normalize_alias_key(canonical_text) not in column_keys:
        return False
    sig = dataset_signature(columns)
    report_text = normalize_alias_text(report_id) or ""
    now = time.time()
    try:
        with _write_lock:
            conn = _connect(state_home)
            try:
                with conn:
                    conn.execute(_SCHEMA)
                    conn.execute(
                        "INSERT INTO alias_obs (tenant, dataset_sig, canonical, alias,"
                        " confirmations, last_seen, sample_report) VALUES (?, ?, ?, ?, 1, ?, ?)"
                        " ON CONFLICT (tenant, dataset_sig, canonical, alias) DO UPDATE SET"
                        " confirmations = confirmations + 1, last_seen = excluded.last_seen,"
                        " sample_report = excluded.sample_report",
                        (str(tenant or "local"), sig, canonical_text, alias_text, now, report_text))
                    # 有界：每 sig 最多 _MAX_ROWS_PER_SIG 条，LRU by last_seen（淘汰最旧）。
                    conn.execute(
                        "DELETE FROM alias_obs WHERE tenant = ? AND dataset_sig = ? AND rowid NOT IN"
                        " (SELECT rowid FROM alias_obs WHERE tenant = ? AND dataset_sig = ?"
                        "  ORDER BY last_seen DESC LIMIT ?)",
                        (str(tenant or "local"), sig, str(tenant or "local"), sig, _MAX_ROWS_PER_SIG))
            finally:
                conn.close()
        return True
    except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
        # 学习库任何故障都不得影响 apply 事务：fail-closed no-op + 一次 WARN。
        if isinstance(exc, sqlite3.DatabaseError):
            _warn_corruption_once(exc)
        else:
            _logger.warning("alias-learning record failed: %s", exc)
        return False


def _confidence(confirmations: int) -> float:
    """Monotone in confirmations, hard-capped at 0.9 (never auto-adopted)."""
    try:
        count = max(1, int(confirmations))
    except (TypeError, ValueError):
        count = 1
    return round(min(_CONFIDENCE_CAP, _CONFIDENCE_BASE + _CONFIDENCE_STEP * (count - 1)), 3)


def suggest(state_home: Any, tenant: str, dataset_columns: Iterable[Any],
            query: Any) -> list[dict[str, Any]]:
    """Read-only learned suggestions for an unresolved field query.

    Returns at most :data:`_MAX_SUGGESTIONS` items shaped
    ``{alias, canonical, confirmations, confidence, source: "learned",
    requiresConfirmation: True}``.  A row is only returned when its canonical
    field exists in the *current* dataset's columns; ``confidence`` is derived
    solely from confirmations (capped at 0.9), so a suggestion can never be
    mistaken for an authored/manifest alias.  Corruption → [] (fail-closed).
    """
    if not enabled():
        return []
    query_key = normalize_alias_key(query)
    if not query_key:
        return []
    columns = [str(item) for item in (dataset_columns or []) if str(item)]
    canonical_by_key: dict[str, str] = {}
    for column in columns:
        key = normalize_alias_key(column)
        if key and key not in canonical_by_key:
            canonical_by_key[key] = column
    if not canonical_by_key:
        return []
    # 只读路径不建文件：库还不存在 = 从未学过任何别名。
    if not (Path(state_home) / _DB_NAME).exists():
        return []
    sig = dataset_signature(columns)
    rows: list[dict[str, Any]] = []
    try:
        conn = _connect(state_home)
        try:
            conn.execute(_SCHEMA)
            cursor = conn.execute(
                "SELECT canonical, alias, confirmations, last_seen FROM alias_obs"
                " WHERE tenant = ? AND dataset_sig = ? LIMIT 400",
                (str(tenant or "local"), sig))
            raw = cursor.fetchall()
        finally:
            conn.close()
    except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
        if isinstance(exc, sqlite3.DatabaseError):
            _warn_corruption_once(exc)
        else:
            _logger.warning("alias-learning suggest failed: %s", exc)
        return []
    import difflib
    exact: list[tuple[int, float, int, str, str]] = []
    fuzzy: list[tuple[int, float, int, str, str]] = []
    for canonical_text, alias_text, confirmations, last_seen in raw:
        canonical_text = normalize_alias_text(canonical_text)
        alias_text = normalize_alias_text(alias_text)
        if not canonical_text or not alias_text:
            continue
        canonical_key = normalize_alias_key(canonical_text)
        # NS-01：canonical 必须在当前数据集列宇宙里才返回。
        if canonical_key not in canonical_by_key:
            continue
        # 查询已精确点名 canonical 列时，它的学习别名没有增量价值（resolve_field
        # 的 manifest 精确命中本来就会短路）——不制造噪音。
        if canonical_key == query_key:
            continue
        alias_key = normalize_alias_key(alias_text)
        if not alias_key:
            continue
        try:
            count = int(confirmations)
        except (TypeError, ValueError):
            count = 1
        seen = float(last_seen or 0.0)
        rank = (count, seen)
        if alias_key == query_key:
            exact.append((-count, -seen, count, canonical_text, alias_text))
        else:
            ratio = difflib.SequenceMatcher(None, query_key, alias_key).ratio()
            if ratio >= _FUZZY_RATIO:
                fuzzy.append((-ratio, -seen, count, canonical_text, alias_text))
    exact.sort()
    fuzzy.sort()
    for _sort, _r, count, canonical_text, alias_text in (exact + fuzzy)[:_MAX_SUGGESTIONS]:
        rows.append({
            "alias": alias_text,
            "canonical": canonical_by_key.get(normalize_alias_key(canonical_text), canonical_text),
            "confirmations": count,
            "confidence": _confidence(count),
            "source": "learned",
            "requiresConfirmation": True,
        })
    return rows
