"""Platform layer for the report server (zero-behavior consolidation).

Merged from three former modules — every public name, signature and behavior
is unchanged, only the file location moved (NQ-02, 100-file budget):

- ``types.py``: internal type glossary (ColumnSchema / TableInfo /
  AggregateRequest / AggregateResponse). Linters ignore it (no runtime side
  effects); treat docstrings here as the source of truth that other modules
  reference.
- ``loopback.py``: loopback HTTP that never honors process/system proxies.
- ``servers_index.py``: global index of locally running report servers.
"""
from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

try:  # POSIX 下用文件锁挡住「跨进程 read-modify-write」互相覆盖；Windows 退化为仅线程锁。
    import fcntl
except Exception:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]


# ============================================================================
# Section 1 — former server/types.py
# ============================================================================


@dataclass
class ColumnSchema:
    """Schema for a single column in a loaded dataset."""
    name: str
    sql_type: str            # TEXT | INTEGER | REAL
    inferred_type: str        # dimension | measure | time | id
    non_null: int = 0
    distinct: int = 0
    min: Optional[float] = None
    max: Optional[float] = None
    mean: Optional[float] = None
    sum: Optional[float] = None
    sample_min: Optional[str] = None   # for time columns
    sample_max: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None or k in ("name", "sql_type", "inferred_type")}


@dataclass
class TableInfo:
    """Loaded table metadata held by the engine."""
    name: str
    columns: list[str]
    schema: list[ColumnSchema]
    row_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "columns": [c.to_dict() for c in self.schema],
            "rowCount": self.row_count,
        }


@dataclass
class AggregateRequest:
    """Parsed POST /aggregate body."""
    group_by: list[str] = field(default_factory=list)
    measures: list[dict[str, Any]] = field(default_factory=list)  # [{column, aggregation, divisor?}]
    bucket: Optional[dict[str, str]] = None                      # {source, bucketed, bucket}
    filter: list[dict[str, Any]] = field(default_factory=list)
    order_by: list[dict[str, str]] = field(default_factory=list)  # [{column, direction}]
    limit: Optional[int] = None
    offset: int = 0
    # BI-H3：TOP-K「其他」合并——LIMIT 截断时补一行/一组合计尾部「其他（N 项）」。
    top_k_other: bool = False
    # 客户端语义层下传的可加性掩码（与 measures 平行）：服务端只有列名+聚合方式，
    # 拿不到 measureTypes——占比/比率型列由客户端判定后否决进「其他」行
    # （true=可加，False=否决，None/缺省=沿用聚合方式判断，向后兼容老客户端）。
    add_mask: Optional[list] = None
    # Agent/query safety budgets.  These are hard fail-closed limits, not
    # semantic truncation: a request exceeding them returns an actionable
    # error rather than silently changing Flint's grouped result.
    max_scan_rows: Optional[int] = None
    max_groups: Optional[int] = None


@dataclass
class AggregateResponse:
    """Response body for /aggregate."""
    columns: list[str]
    rows: list[list[Any]]
    aggregates: dict[str, dict[str, float]] = field(default_factory=dict)
    truncated: bool = False
    truncated_reason: Optional[str] = None
    group_count: int = 0
    # Pre-LIMIT group total (only populated when the result was truncated) —
    # the runtime renders「已显示前 N 项（共 M 项）」from it. 0 = unknown.
    group_total: int = 0
    # BI-H3：本次响应合并了「其他（N 项）」尾部行时的标记（{"omitted": N}）。
    other_merged: Optional[dict[str, Any]] = None
    # Bounded query-budget diagnostics; never includes raw rows or SQL.
    query_budget: Optional[dict[str, Any]] = None
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "columns": self.columns,
            "rows": self.rows,
            "aggregates": self.aggregates,
            "truncated": self.truncated,
            "groupCount": self.group_count,
            "groupTotal": self.group_total,
            "elapsedMs": self.elapsed_ms,
        }
        if self.truncated_reason:
            out["truncatedReason"] = self.truncated_reason
        if self.other_merged:
            out["otherMerged"] = self.other_merged
        if self.query_budget:
            out["queryBudget"] = self.query_budget
        return out


# ============================================================================
# Section 2 — former server/loopback.py
#
# A system proxy (macOS scutil --proxy, e.g. a local Clash on 7890 that is not
# running after a reboot) makes urllib route ``http://127.0.0.1:<port>/api/health``
# through the dead proxy: every loopback health check, port scan and register
# POST then fails with ``Connection refused`` even though the report server is
# fine (observed 2026-09-26: every run_tests server group failed "server did not
# come up" while curl answered instantly — curl ignores macOS system proxies,
# urllib does not). All loopback traffic in this skill must go through
# :func:`urlopen_loopback`.
# ============================================================================

_OPENER = None


def _is_loopback_host(host: str) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def urlopen_loopback(url, timeout=2, data=None, method=None):
    """urlopen for a loopback URL with proxies forcibly disabled.

    ``url`` may be a str or a pre-built ``urllib.request.Request`` (headers
    preserved). A non-loopback URL falls through to the stock urlopen: a user
    may point serve_report --server at a LAN host that legitimately needs the
    system proxy.
    """
    req = url if isinstance(url, urllib.request.Request) \
        else urllib.request.Request(url, data=data, method=method)
    host = urlparse(req.full_url).hostname or ""
    if not _is_loopback_host(host):
        return urllib.request.urlopen(req, timeout=timeout)
    global _OPENER
    if _OPENER is None:
        _OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return _OPENER.open(req, timeout=timeout)


# ============================================================================
# Section 3 — former server/servers_index.py
#
# Global index of locally running report servers. Keeps one small JSON index —
# ``~/.ai-html-report/servers.json`` — mapping port → {registryPath, pid,
# startedAt}. It is written by ``server.app.run_server`` at bind time and
# removed on shutdown; dead entries are pruned lazily by the 报告管理 endpoint
# (`/api/reports/all`) after a failed health ping. The index is machine-local
# state (like the default registry): report directories may live anywhere, so
# a fixed per-user path is the only stable anchor for the manager page.
# ============================================================================

_LOCK = threading.Lock()

# Same per-user state dir as server.registry defaults.
# LRM_STATE_HOME：测试/CI 隔离（与 server/registry.py 同一开关）。
_USER_STATE = Path(os.environ.get("LRM_STATE_HOME") or (Path.home() / ".ai-html-report"))


def index_path() -> Path:
    return _USER_STATE / "servers.json"


def _read() -> dict:
    try:
        data = json.loads(index_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write(data: dict) -> None:
    path = index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


@contextlib.contextmanager
def _cross_process_lock():
    """Best-effort inter-process lock around a read-modify-write of the index.

    The index is a single small JSON file shared by every report server on the
    machine. Without this, a shutdown that read the file before another server
    registered would rewrite it from its stale copy and silently DROP the newer
    entry — the 报告管理 page then shows "没有运行中的报告" even though servers
    are running. POSIX uses fcntl.flock; elsewhere the thread lock still applies.
    """
    if fcntl is None:
        yield
        return
    lock_path = _USER_STATE / "servers.json.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "a+")
    except OSError:
        yield
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _pid_alive(pid: int | None) -> bool:
    """True when ``pid`` still names a live process (busy-but-alive ⇒ keep)."""
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    except Exception:
        return False
    return True


def register(port: int, registry_path: Path | str, pid: int | None = None) -> None:
    """Record (or refresh) the server bound to ``port``."""
    with _LOCK, _cross_process_lock():
        data = _read()
        data[str(int(port))] = {
            "port": int(port),
            "registryPath": str(registry_path),
            "pid": int(pid) if pid else None,
            "startedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        _write(data)


def unregister(port: int) -> None:
    with _LOCK, _cross_process_lock():
        data = _read()
        if data.pop(str(int(port)), None) is not None:
            _write(data)


def prune_dead() -> None:
    """Drop entries whose port no longer answers AND whose process is gone.

    A server that is merely BUSY (loading a 300k-row snapshot, single-threaded
    accept loop) can miss the 0.6s health ping; pruning it would erase a live
    server from the index for good. So an entry survives when either the ping
    answers or its pid is still alive.
    """
    with _LOCK, _cross_process_lock():
        data = _read()
        alive = {
            k: v for k, v in data.items()
            if ping(int(k)) or _pid_alive(v.get("pid"))
        }
        if len(alive) != len(data):
            _write(alive)


def load() -> dict:
    with _LOCK:
        return _read()


def ping(port: int, timeout: float = 1.5) -> bool:
    """True when a report server currently answers on 127.0.0.1:``port``."""
    try:
        with urlopen_loopback(f"http://127.0.0.1:{int(port)}/api/health", timeout=timeout) as resp:
            return resp.status == 200
    except (OSError, urllib.error.URLError):
        return False
