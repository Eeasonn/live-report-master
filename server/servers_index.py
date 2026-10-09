"""Global index of locally running report servers.

Every server-mode build gets its OWN port + registry (per-report isolation),
so "what reports are running on this machine" is not answerable from any
single registry. This module keeps one small JSON index —
``~/.ai-html-report/servers.json`` — mapping port → {registryPath, pid,
startedAt}. It is written by ``server.app.run_server`` at bind time and
removed on shutdown; dead entries are pruned lazily by the 报告管理 endpoint
(`/api/reports/all`) after a failed health ping.

The index is machine-local state (like the default registry): report
directories may live anywhere, so a fixed per-user path is the only stable
anchor for the manager page.
"""
from __future__ import annotations

import contextlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .loopback import urlopen_loopback

try:  # POSIX 下用文件锁挡住「跨进程 read-modify-write」互相覆盖；Windows 退化为仅线程锁。
    import fcntl
except Exception:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

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
