"""后台任务生命周期与跨进程状态存储。

为什么要有它：``h_dataset_replace`` 的重管道（xlsx 解析 → 合并 → 装载 → 快照）
在 29 万行的真实文件上要跑几分钟，全部塞在 HTTP 请求里会被代理掐断；Copilot
的深度模型请求也可能超过普通 HTTP 长连接的耐心。

契约：
- 同一 (reportId, datasetId) 同时只允许一个 running 任务；``create`` 在锁内
  做查重，避免两个进程同时写同一张 SQLite 表。
- server 启动后由 ``configure(state_home)`` 打开机器级 jobs.sqlite3。轮询、取消、
  并发上限和终态清理都走这个共享状态，因此同一 state home 下的多进程 server
  可以互相看到任务。进程崩溃后，新进程只把确认已死亡 owner 的 running 任务标为
  lost，不伪造成功，也不自动重放可能产生副作用的数据写入。
- ``work(progress)`` 仍在守护线程执行；异常整体变成 job 的 error 字符串，数据侧
  保持 fail-closed。``cancel`` 是协作式：设置取消标志并唤醒可取消的 worker；
  worker 在安全边界检查后终态为 ``cancelled``，不发布 result。
- 终态记录按数量和十分钟 TTL 清理；Copilot 通过 ``max_running`` 限制并行 provider
  连接，达到上限由 HTTP 层返回可重试的 429。

本模块保留无 ``configure`` 时的进程内实现，方便纯单元测试和不需要 server 状态
目录的调用方；真实 server 在 ``make_handlers`` 初始化时显式配置共享状态目录。
"""
from __future__ import annotations

import json
import os
import hashlib
import hmac
import secrets
import sqlite3
import socket
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

_LOCK = threading.Lock()
_CONFIG_LOCK = threading.Lock()
_JOBS: dict[str, dict[str, Any]] = {}
_JOB_EVENTS: dict[str, list[dict[str, Any]]] = {}
_KEEP_FINISHED = 40
_FINISHED_TTL_SECONDS = 10 * 60
_PROCESS_TOKEN = uuid.uuid4().hex
_PERSIST_PATH: Path | None = None
_DB_READY_PATH: Path | None = None
_CANCEL_CACHE: dict[str, tuple[float, bool]] = {}
_CANCEL_CACHE_LOCK = threading.Lock()
_CANCEL_CACHE_TTL = 0.10
_RECOVERABLE_POLICIES = frozenset({"safe-read", "safe-export", "idempotent-replace"})


def _retry_secret() -> bytes:
    """Return a server-local secret for bounded, non-guessable retry tokens."""
    if _PERSIST_PATH is None:
        return hashlib.sha256(_PROCESS_TOKEN.encode()).digest()
    path = _PERSIST_PATH.with_name("agent-retry.secret")
    try:
        value = path.read_bytes()
        if len(value) >= 32:
            return value
    except OSError:
        pass
    value = secrets.token_bytes(32)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(value)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return value


def retry_token(job_id: str) -> str:
    signature = hmac.new(_retry_secret(), str(job_id).encode(), hashlib.sha256).hexdigest()
    return f"jr1.{job_id}.{signature}"


def verify_retry_token(job_id: str, token: str) -> bool:
    parts = str(token or "").split(".", 2)
    if len(parts) != 3 or parts[0] != "jr1" or parts[1] != str(job_id):
        return False
    return hmac.compare_digest(parts[2], retry_token(job_id).split(".", 2)[2])


class JobConflict(Exception):
    """同一 (reportId, datasetId) 已有 running 任务。

    ``create`` 在锁内做「查重 + 登记」原子步——HTTP 层的 ``active_for`` 预检
    （先于读 body 的快速 409）与 ``create`` 之间存在 TOCTOU 窗口，两个并发
    async=1 请求可以同时通过预检、各起一个线程写同一张 SQLite 表。这里兜底
    第二个：携带既有 jobId，端点据此回 409。"""


class JobCancelled(Exception):
    """Worker 在取消边界主动退出。"""


class JobCapacity(Exception):
    """同类后台任务已达到并发上限。"""


class JobReplay(Exception):
    """客户端重试了同一个 requestId，复用已有任务而不是重复执行。"""


class JobRequestConflict(Exception):
    """同一 requestId 被复用于不同输入，拒绝静默复用。"""


class JobRecoveryDenied(Exception):
    """丢失任务没有声明可安全重放，必须由调用方重新发起。"""


def _pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _open_db() -> sqlite3.Connection | None:
    path = _PERSIST_PATH
    if path is None:
        return None
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    # Results can contain report evidence; do not let a shared machine expose
    # them through the default umask. SQLite may create WAL/SHM sidecars later,
    # so tighten those whenever a connection opens as well.
    for private_path in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        try:
            os.chmod(private_path, 0o600)
        except OSError:
            pass
    global _DB_READY_PATH
    if _DB_READY_PATH != path:
        with _CONFIG_LOCK:
            if _DB_READY_PATH != path:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS jobs (
                        id TEXT PRIMARY KEY,
                        kind TEXT NOT NULL,
                        report_id TEXT NOT NULL,
                        dataset_id TEXT NOT NULL,
                        status TEXT NOT NULL,
                        phase TEXT NOT NULL,
                        started_at REAL NOT NULL,
                        finished_at REAL,
                        result_json TEXT,
                        error TEXT,
                        cancel_requested INTEGER NOT NULL DEFAULT 0,
                        cancel_reason TEXT,
                        owner_pid INTEGER NOT NULL,
                        owner_token TEXT NOT NULL,
                        updated_at REAL NOT NULL,
                        heartbeat_at REAL NOT NULL,
                        request_key TEXT,
                        request_hash TEXT,
                        priority INTEGER NOT NULL DEFAULT 50,
                        payload_json TEXT,
                        lease_until REAL NOT NULL DEFAULT 0,
                        trace_id TEXT
                    );
                    CREATE INDEX IF NOT EXISTS jobs_active_key
                        ON jobs(status, report_id, dataset_id);
                    CREATE INDEX IF NOT EXISTS jobs_kind_status
                        ON jobs(kind, status);
                    CREATE TABLE IF NOT EXISTS job_events (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id TEXT NOT NULL,
                        status TEXT NOT NULL,
                        phase TEXT NOT NULL,
                        at REAL NOT NULL,
                        detail TEXT,
                        trace_id TEXT
                    );
                    CREATE INDEX IF NOT EXISTS job_events_job ON job_events(job_id, seq);
                    CREATE TABLE IF NOT EXISTS monitors (
                        id TEXT PRIMARY KEY,
                        report_id TEXT NOT NULL,
                        name TEXT NOT NULL,
                        tool TEXT NOT NULL,
                        args_json TEXT NOT NULL,
                        interval_seconds INTEGER NOT NULL,
                        next_run REAL NOT NULL,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        last_job_id TEXT,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS monitors_due ON monitors(enabled, next_run);
                    CREATE TABLE IF NOT EXISTS agent_audit (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        report_id TEXT NOT NULL,
                        actor TEXT NOT NULL,
                        action TEXT NOT NULL,
                        target TEXT,
                        detail_json TEXT,
                        at REAL NOT NULL,
                        trace_id TEXT
                    );
                    CREATE INDEX IF NOT EXISTS agent_audit_report ON agent_audit(report_id, seq);
                    CREATE TABLE IF NOT EXISTS rate_limits (
                        key TEXT PRIMARY KEY,
                        window_started REAL NOT NULL,
                        request_count INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE TABLE IF NOT EXISTS notification_deliveries (
                        proposal_id TEXT PRIMARY KEY,
                        report_id TEXT NOT NULL,
                        channel TEXT NOT NULL,
                        destination_hash TEXT NOT NULL,
                        status TEXT NOT NULL,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        last_error TEXT,
                        updated_at REAL NOT NULL
                    );
                """)
                # Small forward-compatible migration for a database created by
                # an earlier preview or an interrupted first boot.
                columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
                for name, sql in (
                    ("cancel_reason", "ALTER TABLE jobs ADD COLUMN cancel_reason TEXT"),
                    ("owner_pid", "ALTER TABLE jobs ADD COLUMN owner_pid INTEGER NOT NULL DEFAULT 0"),
                    ("owner_token", "ALTER TABLE jobs ADD COLUMN owner_token TEXT NOT NULL DEFAULT ''"),
                    ("updated_at", "ALTER TABLE jobs ADD COLUMN updated_at REAL NOT NULL DEFAULT 0"),
                    ("request_key", "ALTER TABLE jobs ADD COLUMN request_key TEXT"),
                    ("heartbeat_at", "ALTER TABLE jobs ADD COLUMN heartbeat_at REAL NOT NULL DEFAULT 0"),
                    ("request_hash", "ALTER TABLE jobs ADD COLUMN request_hash TEXT"),
                    ("priority", "ALTER TABLE jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 50"),
                    ("payload_json", "ALTER TABLE jobs ADD COLUMN payload_json TEXT"),
                    ("lease_until", "ALTER TABLE jobs ADD COLUMN lease_until REAL NOT NULL DEFAULT 0"),
                    ("trace_id", "ALTER TABLE jobs ADD COLUMN trace_id TEXT"),
                ):
                    if name not in columns:
                        conn.execute(sql)
                event_columns = {row[1] for row in conn.execute("PRAGMA table_info(job_events)")}
                if "trace_id" not in event_columns:
                    conn.execute("ALTER TABLE job_events ADD COLUMN trace_id TEXT")
                audit_columns = {row[1] for row in conn.execute("PRAGMA table_info(agent_audit)")}
                if "trace_id" not in audit_columns:
                    conn.execute("ALTER TABLE agent_audit ADD COLUMN trace_id TEXT")
                conn.execute("CREATE INDEX IF NOT EXISTS jobs_request_key ON jobs(report_id, request_key)")
                _DB_READY_PATH = path
    return conn


def configure(state_home: Path | str | None) -> None:
    """Enable shared job metadata under ``state_home`` and recover dead owners.

    The actual worker remains owned by the process that accepted the request.
    This function deliberately does not replay a running task after restart:
    data replacement is not safe to duplicate, and a model request may have
    side effects at the provider boundary.
    """
    global _PERSIST_PATH, _DB_READY_PATH
    if state_home is None:
        _PERSIST_PATH = None
        _DB_READY_PATH = None
        return
    state = Path(state_home).expanduser()
    with _CONFIG_LOCK:
        _PERSIST_PATH = state / "jobs.sqlite3"
        _DB_READY_PATH = None
    conn = _open_db()
    if conn is None:
        return
    now = time.time()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT id, owner_pid, owner_token, lease_until, cancel_requested, cancel_reason "
            "FROM jobs WHERE status = 'running'"
        ).fetchall()
        cancelled_after_restart: list[tuple[str, str]] = []
        for row in rows:
            owner_pid = int(row["owner_pid"] or 0)
            owner_token = str(row["owner_token"] or "")
            owner_dead = owner_token != _PROCESS_TOKEN and (
                owner_pid == os.getpid() or not _pid_alive(owner_pid)
                or (float(row["lease_until"] or 0) > 0
                    and float(row["lease_until"] or 0) < now)
            )
            if owner_dead:
                # A cancellation request is durable intent.  If the worker
                # dies after the request but before its cooperative terminal
                # write, do not resurrect the job as ``lost`` (which would
                # incorrectly invite recovery/replay); finalize it as
                # cancelled and discard any possible late result.  This is the
                # restart boundary of the same owner-fenced cancellation
                # contract used by ``_write_terminal`` below.
                if bool(row["cancel_requested"]):
                    reason = str(row["cancel_reason"] or "任务已取消")[:500]
                    conn.execute(
                        """UPDATE jobs SET status='cancelled', phase='cancelled',
                           error=?, finished_at=?, updated_at=?, lease_until=0
                           WHERE id=? AND status='running'""",
                        (reason, now, now, row["id"]),
                    )
                    cancelled_after_restart.append((str(row["id"]), reason))
                else:
                    conn.execute(
                        """UPDATE jobs SET status='lost', phase='lost',
                           error=?, finished_at=?, updated_at=?, lease_until=0 WHERE id=?""",
                        ("服务进程已退出，任务未完成；请重新发起", now, now, row["id"]),
                    )
        conn.commit()
        # Record events after the recovery transaction commits; opening a
        # second SQLite connection while BEGIN IMMEDIATE is held can deadlock
        # on the same state home.
        for job_id, reason in cancelled_after_restart:
            _record_event(job_id, "cancelled", "cancelled", reason)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    _prune()


def _encode_result(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _job_trace_id(job_id: str) -> str | None:
    """Read the durable correlation id without exposing payload/result data."""
    conn = _open_db()
    if conn is not None:
        try:
            row = conn.execute("SELECT trace_id FROM jobs WHERE id=?", (job_id,)).fetchone()
            return str(row["trace_id"]) if row and row["trace_id"] else None
        finally:
            conn.close()
    with _LOCK:
        job = _JOBS.get(job_id) or {}
        return str(job.get("traceId")) if job.get("traceId") else None


def _record_event(job_id: str, status: str, phase: str, detail: str | None = None,
                  trace_id: str | None = None) -> None:
    """Append a small lifecycle event; event failure never changes job outcome."""
    now = time.time()
    conn = _open_db()
    trace_id = trace_id or _job_trace_id(job_id)
    if conn is not None:
        try:
            conn.execute("INSERT INTO job_events(job_id,status,phase,at,detail,trace_id) VALUES (?,?,?,?,?,?)",
                         (job_id, status, phase, now, detail, trace_id))
        except Exception:
            pass
        finally:
            conn.close()
        return
    with _LOCK:
        _JOB_EVENTS.setdefault(job_id, []).append({"status": status, "phase": phase,
            "at": now, "detail": detail, "traceId": trace_id})


def events(job_id: str, *, limit: int = 64) -> list[dict[str, Any]]:
    """Read a bounded lifecycle trace without returning job result payloads."""
    limit = max(1, min(int(limit), 128))
    conn = _open_db()
    if conn is not None:
        try:
            rows = conn.execute("SELECT status,phase,at,detail,trace_id FROM job_events WHERE job_id=? ORDER BY seq DESC LIMIT ?",
                                (job_id, limit)).fetchall()
            return [{"status": row["status"], "phase": row["phase"], "at": row["at"],
                     "detail": row["detail"], "traceId": row["trace_id"]}
                    for row in reversed(rows)]
        finally:
            conn.close()
    with _LOCK:
        return list(_JOB_EVENTS.get(job_id, [])[-limit:])


def create_monitor(report_id: str, name: str, tool: str, args: dict[str, Any], interval_seconds: int) -> str:
    monitor_id = "mon-" + uuid.uuid4().hex[:12]
    now = time.time()
    conn = _open_db()
    payload = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
    if conn is not None:
        try:
            conn.execute("INSERT INTO monitors(id,report_id,name,tool,args_json,interval_seconds,next_run,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                         (monitor_id, report_id, name[:120], tool, payload, int(interval_seconds), now + int(interval_seconds), now, now))
        finally:
            conn.close()
        return monitor_id
    with _LOCK:
        _JOB_EVENTS.setdefault("__monitors__", [])
    return monitor_id


def list_monitors(report_id: str) -> list[dict[str, Any]]:
    conn = _open_db()
    if conn is None:
        return []
    try:
        rows = conn.execute("SELECT * FROM monitors WHERE report_id=? ORDER BY created_at", (report_id,)).fetchall()
        return [{"id": row["id"], "reportId": row["report_id"], "name": row["name"],
                 "tool": row["tool"], "args": json.loads(row["args_json"]),
                 "intervalSeconds": row["interval_seconds"], "nextRun": row["next_run"],
                 "enabled": bool(row["enabled"]), "lastJobId": row["last_job_id"]} for row in rows]
    finally:
        conn.close()


def due_monitors(limit: int = 8) -> list[dict[str, Any]]:
    conn = _open_db()
    if conn is None:
        return []
    try:
        rows = conn.execute("SELECT * FROM monitors WHERE enabled=1 AND next_run<=? ORDER BY next_run LIMIT ?",
                            (time.time(), int(limit))).fetchall()
        return [{"id": row["id"], "reportId": row["report_id"], "name": row["name"],
                 "tool": row["tool"], "args": json.loads(row["args_json"]),
                 "intervalSeconds": row["interval_seconds"], "lastJobId": row["last_job_id"]} for row in rows]
    finally:
        conn.close()


def claim_monitor(monitor_id: str, job_id: str, interval_seconds: int) -> bool:
    conn = _open_db()
    if conn is None:
        return False
    try:
        now = time.time()
        cur = conn.execute("UPDATE monitors SET next_run=?,last_job_id=?,updated_at=? WHERE id=? AND enabled=1 AND next_run<=?",
                           (now + int(interval_seconds), job_id, now, monitor_id, now))
        return cur.rowcount == 1
    finally:
        conn.close()


def set_monitor_enabled(monitor_id: str, report_id: str, enabled: bool) -> bool:
    conn = _open_db()
    if conn is None:
        return False
    try:
        cur = conn.execute("UPDATE monitors SET enabled=?,updated_at=? WHERE id=? AND report_id=?",
                           (1 if enabled else 0, time.time(), monitor_id, report_id))
        return cur.rowcount == 1
    finally:
        conn.close()


def delete_monitor(monitor_id: str, report_id: str) -> bool:
    conn = _open_db()
    if conn is None:
        return False
    try:
        cur = conn.execute("DELETE FROM monitors WHERE id=? AND report_id=?", (monitor_id, report_id))
        return cur.rowcount == 1
    finally:
        conn.close()


def record_audit(report_id: str, actor: str, action: str, target: str = "",
                 detail: dict[str, Any] | None = None,
                 trace_id: str | None = None) -> None:
    """Persist a bounded Agent action trail without credentials or raw rows."""
    conn = _open_db()
    if conn is None:
        return
    safe_detail = {}
    for key, value in (detail or {}).items():
        if str(key).lower() in {"password", "apikey", "api_key", "token", "secret"}:
            safe_detail[str(key)] = "[redacted]"
        else:
            safe_detail[str(key)] = value
    try:
        conn.execute("INSERT INTO agent_audit(report_id,actor,action,target,detail_json,at,trace_id) VALUES (?,?,?,?,?,?,?)",
                     (report_id, str(actor)[:120], str(action)[:120], str(target)[:160],
                      json.dumps(safe_detail, ensure_ascii=False, separators=(",", ":"))[:4000],
                      time.time(), str(trace_id or "")[:96] or None))
    finally:
        conn.close()


def audit_events(report_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
    conn = _open_db()
    if conn is None:
        return []
    try:
        rows = conn.execute("SELECT actor,action,target,detail_json,at,trace_id FROM agent_audit WHERE report_id=? ORDER BY seq DESC LIMIT ?",
                            (report_id, max(1, min(int(limit), 500)))).fetchall()
        result = []
        for row in reversed(rows):
            try: detail = json.loads(row["detail_json"] or "{}")
            except ValueError: detail = {}
            result.append({"actor": row["actor"], "action": row["action"], "target": row["target"],
                           "detail": detail, "at": row["at"], "traceId": row["trace_id"]})
        return result
    finally:
        conn.close()


def claim_notification(proposal_id: str, report_id: str, channel: str,
                       destination: str, *, max_attempts: int = 3) -> dict[str, Any]:
    """Atomically deduplicate an approved external notification delivery.

    The proposal id is stable for the exact approved payload; only its hash and
    bounded delivery metadata are persisted.  A proxy retry therefore cannot
    send the same webhook twice, while a failed delivery can be retried a small
    number of times without creating an unbounded notification storm.
    """
    if _PERSIST_PATH is None:
        # The real notification endpoint is server-mode only, but keep the
        # process-local fallback deterministic for isolated unit callers.
        return {"send": True, "status": "sending", "attempts": 1}
    conn = _open_db()
    destination_hash = hashlib.sha256(str(destination).encode("utf-8")).hexdigest()
    try:
        now = time.time()
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status,attempts,updated_at,destination_hash FROM notification_deliveries WHERE proposal_id=?",
            (str(proposal_id),)).fetchone()
        if row is not None and row["destination_hash"] != destination_hash:
            conn.rollback()
            return {"send": False, "status": "conflict", "attempts": int(row["attempts"] or 0)}
        if row is not None and row["status"] == "sent":
            conn.commit()
            return {"send": False, "status": "sent", "attempts": int(row["attempts"] or 0)}
        if row is not None and row["status"] == "sending" and now - float(row["updated_at"] or 0) < 30:
            conn.commit()
            return {"send": False, "status": "in_flight", "attempts": int(row["attempts"] or 0)}
        attempts = int(row["attempts"] or 0) + 1 if row is not None else 1
        if attempts > max(1, int(max_attempts)):
            conn.commit()
            return {"send": False, "status": "retry_exhausted", "attempts": attempts - 1}
        conn.execute(
            """INSERT INTO notification_deliveries
               (proposal_id,report_id,channel,destination_hash,status,attempts,last_error,updated_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(proposal_id) DO UPDATE SET status='sending',attempts=?,last_error=NULL,updated_at=?""",
            (str(proposal_id), str(report_id), str(channel), destination_hash,
             "sending", attempts, None, now, attempts, now),
        )
        conn.commit()
        return {"send": True, "status": "sending", "attempts": attempts}
    finally:
        conn.close()


def finish_notification(proposal_id: str, *, sent: bool, error: str | None = None,
                        attempts: int | None = None) -> None:
    """Publish the bounded terminal state for a claimed notification."""
    if _PERSIST_PATH is None:
        return
    conn = _open_db()
    try:
        where = "proposal_id=? AND status='sending'"
        params: list[Any] = ["sent" if sent else "failed",
                             None if sent else str(error or "")[:300], time.time(), str(proposal_id)]
        if attempts is not None:
            where += " AND attempts=?"
            params.append(int(attempts))
        conn.execute("UPDATE notification_deliveries SET status=?,last_error=?,updated_at=? WHERE " + where,
                     tuple(params))
    finally:
        conn.close()


def _redis_read_response(stream) -> Any:
    prefix = stream.read(1)
    if prefix == b":":
        return int(stream.readline().strip())
    if prefix == b"$":
        length = int(stream.readline().strip())
        if length < 0:
            return None
        value = stream.read(length); stream.read(2)
        return value.decode("utf-8", errors="replace")
    if prefix == b"-":
        raise RuntimeError(stream.readline().decode("utf-8", errors="replace").strip())
    if prefix == b"*":
        count = int(stream.readline().strip())
        return [_redis_read_response(stream) for _ in range(max(0, count))]
    raise RuntimeError("unsupported Redis response")


def _redis_command(parts: list[str]) -> Any:
    """Minimal RESP client used only when LRM_REDIS_URL is explicitly configured."""
    parsed = urlparse(os.environ.get("LRM_REDIS_URL", ""))
    if parsed.scheme != "redis" or not parsed.hostname:
        raise RuntimeError("LRM_REDIS_URL must be redis://host:port/db")
    payload = [f"*{len(parts)}\r\n".encode()]
    for part in parts:
        raw = str(part).encode("utf-8")
        payload.append(f"${len(raw)}\r\n".encode() + raw + b"\r\n")
    with socket.create_connection((parsed.hostname, parsed.port or 6379), timeout=1.5) as sock:
        stream = sock.makefile("rwb")
        if parsed.password:
            auth = [b"*2\r\n", b"$4\r\nAUTH\r\n", f"${len(unquote(parsed.password).encode())}\r\n".encode(),
                    unquote(parsed.password).encode(), b"\r\n"]
            stream.write(b"".join(auth)); stream.flush(); _redis_read_response(stream)
        stream.write(b"".join(payload)); stream.flush()
        return _redis_read_response(stream)


def _consume_redis_rate_limit(key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    script = ("local c=redis.call('INCR',KEYS[1]); "
              "if c==1 then redis.call('EXPIRE',KEYS[1],ARGV[1]) end; "
              "return {c,redis.call('TTL',KEYS[1])}")
    result = _redis_command(["EVAL", script, "1", "lrm:rate:" + digest, str(max(1, int(window_seconds)))])
    if not isinstance(result, list) or len(result) < 2:
        raise RuntimeError("invalid Redis rate response")
    count, ttl = int(result[0]), max(1, int(result[1]))
    return (count <= int(limit), 0 if count <= int(limit) else ttl)


def consume_rate_limit(key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
    """Consume a shared Redis bucket when configured, otherwise SQLite."""
    if os.environ.get("LRM_REDIS_URL"):
        try:
            return _consume_redis_rate_limit(key, limit, window_seconds)
        except Exception:
            # Explicit Redis configuration is fail-closed: silently falling
            # back to per-host SQLite would defeat a tenant-wide rate limit.
            return False, 5
    conn = _open_db()
    if conn is None:
        return True, 0
    now = time.time()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT window_started,request_count FROM rate_limits WHERE key=?", (key,)).fetchone()
        started = float(row["window_started"]) if row else now
        count = int(row["request_count"]) if row else 0
        if now - started >= window_seconds:
            started, count = now, 0
        if count >= limit:
            conn.execute("INSERT OR REPLACE INTO rate_limits(key,window_started,request_count) VALUES (?,?,?)",
                         (key, started, count))
            conn.commit()
            return False, max(1, int(window_seconds - (now - started)))
        conn.execute("INSERT OR REPLACE INTO rate_limits(key,window_started,request_count) VALUES (?,?,?)",
                     (key, started, count + 1))
        conn.commit()
        return True, 0
    finally:
        conn.close()


def _decode_result(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def recovery_policy(job: dict[str, Any] | None) -> str | None:
    """Return the explicit replay policy carried by a durable job payload.

    A lost worker must never be retried merely because its input happens to be
    persisted: an ``append`` import can duplicate rows and a future write tool
    may have external side effects.  Only the dispatcher that created a job
    may opt into a named, idempotent recovery policy.
    """
    if not isinstance(job, dict):
        return None
    payload = job.get("payload")
    if not isinstance(payload, dict):
        return None
    policy = str(payload.get("recoveryPolicy") or "").strip()
    return policy or None


def is_recoverable(job: dict[str, Any] | None) -> bool:
    return bool(job and job.get("status") == "lost"
                and recovery_policy(job) in _RECOVERABLE_POLICIES)


def _row_to_job(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = {
        "id": row["id"], "kind": row["kind"],
        "reportId": row["report_id"], "datasetId": row["dataset_id"],
        "status": row["status"], "phase": row["phase"],
        "startedAt": row["started_at"], "finishedAt": row["finished_at"],
        "result": _decode_result(row["result_json"]), "error": row["error"],
        "cancelRequested": bool(row["cancel_requested"]),
        "cancelReason": row["cancel_reason"],
        "requestKey": row["request_key"] if "request_key" in row.keys() else None,
        "requestHash": row["request_hash"] if "request_hash" in row.keys() else None,
        "heartbeatAt": row["heartbeat_at"] if "heartbeat_at" in row.keys() else None,
        "ownerToken": row["owner_token"] if "owner_token" in row.keys() else None,
        "priority": int(row["priority"] or 50) if "priority" in row.keys() else 50,
        "payload": _decode_result(row["payload_json"]) if "payload_json" in row.keys() else None,
        "leaseUntil": float(row["lease_until"] or 0) if "lease_until" in row.keys() else 0,
        "traceId": row["trace_id"] if "trace_id" in row.keys() else None,
    }
    item["recoveryPolicy"] = recovery_policy(item)
    item["canRecover"] = is_recoverable(item)
    return item


def _persistent_get(job_id: str) -> dict[str, Any] | None:
    conn = _open_db()
    if conn is None:
        return None
    try:
        return _row_to_job(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
    finally:
        conn.close()


def list_jobs(report_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    """Return bounded lifecycle metadata for a report task tray, never results."""
    limit = max(1, min(int(limit), 100))
    conn = _open_db()
    if conn is not None:
        try:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE report_id=? ORDER BY updated_at DESC LIMIT ?",
                (report_id, limit)).fetchall()
            result = []
            for row in rows:
                item = _row_to_job(row) or {}
                item["result"] = None
                item["retryToken"] = retry_token(item["id"])
                result.append(item)
            return result
        finally:
            conn.close()
    with _LOCK:
        rows = [dict(job) for job in _JOBS.values() if job.get("reportId") == report_id]
    rows.sort(key=lambda item: float(item.get("updatedAt") or item.get("startedAt") or 0), reverse=True)
    for item in rows[:limit]:
        item["result"] = None
        item["retryToken"] = retry_token(item["id"])
    return rows[:limit]


def create(kind: str, report_id: str, dataset_id: str, *, max_running: int | None = None,
           request_key: str | None = None, request_hash: str | None = None,
           priority: int = 50, payload: Any = None, queued: bool = False,
           trace_id: str | None = None) -> str:
    _prune()
    job_id = "job-" + uuid.uuid4().hex[:12]
    now = time.time()
    conn = _open_db()
    if conn is not None:
        try:
            conn.execute("BEGIN IMMEDIATE")
            if request_key:
                replay = conn.execute(
                    "SELECT id, request_hash FROM jobs WHERE report_id=? AND request_key=?",
                    (report_id, request_key),
                ).fetchone()
                if replay is not None:
                    if request_hash and replay["request_hash"] != request_hash:
                        raise JobRequestConflict(replay["id"])
                    raise JobReplay(replay["id"])
            duplicate = conn.execute(
                "SELECT id FROM jobs WHERE status IN ('running','queued') AND report_id=? AND dataset_id=?",
                (report_id, dataset_id),
            ).fetchone()
            if duplicate is not None:
                raise JobConflict(duplicate["id"])
            if max_running is not None:
                running = conn.execute(
                    "SELECT COUNT(*) FROM jobs WHERE status IN ('running','queued') AND kind=?", (kind,)
                ).fetchone()[0]
                if running >= max_running:
                    raise JobCapacity(kind)
            status = "queued" if queued else "running"
            owner_pid = 0 if queued else os.getpid()
            owner_token = "" if queued else _PROCESS_TOKEN
            conn.execute(
                """INSERT INTO jobs
                   (id,kind,report_id,dataset_id,status,phase,started_at,finished_at,
                    result_json,error,cancel_requested,cancel_reason,owner_pid,owner_token,updated_at,heartbeat_at,request_key,request_hash,
                    priority,payload_json,lease_until,trace_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (job_id, kind, report_id, dataset_id, status, "queued", now, None,
                 None, None, 0, None, owner_pid, owner_token, now, now, request_key, request_hash,
                 max(0, min(int(priority), 100)), _encode_result(payload), 0, str(trace_id or "")[:96] or None),
            )
            conn.commit()
            _record_event(job_id, status, "queued", trace_id=trace_id)
            return job_id
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    with _LOCK:
        if request_key:
            for job in _JOBS.values():
                if job["reportId"] == report_id and job.get("requestKey") == request_key:
                    if request_hash and job.get("requestHash") != request_hash:
                        raise JobRequestConflict(job["id"])
                    raise JobReplay(job["id"])
        for job in _JOBS.values():
            if job["status"] in {"running", "queued"} and job["reportId"] == report_id \
                    and job["datasetId"] == dataset_id:
                raise JobConflict(job["id"])
        if max_running is not None:
            running = sum(1 for job in _JOBS.values()
                          if job["status"] in {"running", "queued"} and job["kind"] == kind)
            if running >= max_running:
                raise JobCapacity(kind)
        status = "queued" if queued else "running"
        _JOBS[job_id] = {
            "id": job_id, "kind": kind,
            "reportId": report_id, "datasetId": dataset_id,
            "status": status, "phase": "queued",
            "startedAt": now, "finishedAt": None,
            "result": None, "error": None, "cancelRequested": False,
            "cancelReason": None,
            "requestKey": request_key,
            "requestHash": request_hash,
            "ownerToken": _PROCESS_TOKEN,
            "heartbeatAt": now,
            "priority": max(0, min(int(priority), 100)),
            "payload": payload,
            "leaseUntil": 0,
            "traceId": str(trace_id or "")[:96] or None,
        }
        _JOB_EVENTS[job_id] = [{"status": status, "phase": "queued", "at": now,
                                "detail": None, "traceId": str(trace_id or "")[:96] or None}]
    return job_id


def enqueue(kind: str, report_id: str, dataset_id: str, *, priority: int = 50,
            payload: Any = None, request_key: str | None = None,
            request_hash: str | None = None, trace_id: str | None = None) -> str:
    """Persist a queued task for a local or external worker to claim."""
    return create(kind, report_id, dataset_id, priority=priority, payload=payload,
                  request_key=request_key, request_hash=request_hash, queued=True,
                  trace_id=trace_id)


def claim_next(*, kinds: list[str] | None = None, lease_seconds: int = 60) -> dict[str, Any] | None:
    """Atomically claim the highest-priority queued job across processes."""
    now = time.time()
    lease_until = now + max(5, int(lease_seconds))
    kind_clause = ""
    kind_args: tuple[Any, ...] = ()
    if kinds:
        kind_clause = " AND kind IN (" + ",".join("?" for _ in kinds) + ")"
        kind_args = tuple(kinds)
    conn = _open_db()
    if conn is not None:
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT id FROM jobs WHERE status='queued'" + kind_clause
                + " ORDER BY priority DESC, started_at ASC LIMIT 1", kind_args).fetchone()
            if row is None:
                conn.rollback()
                return None
            cur = conn.execute(
                "UPDATE jobs SET status='running',owner_pid=?,owner_token=?,lease_until=?,updated_at=?,heartbeat_at=? "
                "WHERE id=? AND status='queued'",
                (os.getpid(), _PROCESS_TOKEN, lease_until, now, now, row["id"]))
            if cur.rowcount != 1:
                conn.rollback()
                return None
            conn.commit()
            _record_event(row["id"], "running", "claimed")
            return _persistent_get(row["id"])
        finally:
            conn.close()
    with _LOCK:
        candidates = [job for job in _JOBS.values()
                      if job["status"] == "queued" and (not kinds or job["kind"] in kinds)]
        if not candidates:
            return None
        job = sorted(candidates, key=lambda item: (-int(item.get("priority", 50)), item["startedAt"]))[0]
        job["status"] = "running"
        job["ownerToken"] = _PROCESS_TOKEN
        job["heartbeatAt"] = now
        job["leaseUntil"] = lease_until
        _JOB_EVENTS.setdefault(job["id"], []).append({"status": "running", "phase": "claimed", "at": now, "detail": None})
        return dict(job)


def renew_lease(job_id: str, *, lease_seconds: int = 60) -> bool:
    """Extend a claimed job lease only for its current fenced owner."""
    now = time.time(); until = now + max(5, int(lease_seconds))
    conn = _open_db()
    if conn is not None:
        try:
            cur = conn.execute(
                "UPDATE jobs SET lease_until=?,heartbeat_at=?,updated_at=? WHERE id=? AND owner_token=? AND status='running'",
                (until, now, now, job_id, _PROCESS_TOKEN))
            return cur.rowcount == 1
        finally:
            conn.close()
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job or job.get("ownerToken") != _PROCESS_TOKEN or job.get("status") != "running":
            return False
        job["leaseUntil"] = until; job["heartbeatAt"] = now
        return True


def worker_once(dispatch: Callable[[dict[str, Any], Callable[[str], None]], Any], *,
                kinds: list[str] | None = None, lease_seconds: int = 60) -> bool:
    """Claim and execute one durable queue item in an external worker process.

    ``dispatch`` receives the persisted job envelope and a phase callback. It
    must be idempotent or use the job's request hash; terminal writes remain
    owner-fenced, so a worker killed after claiming can only leave ``lost``
    state for a later explicit retry.
    """
    job = claim_next(kinds=kinds, lease_seconds=lease_seconds)
    if job is None:
        return False
    job_id = job["id"]

    # A dispatcher may spend minutes parsing xlsx or waiting on a provider
    # without emitting a semantic phase.  Keep the lease alive independently
    # of progress callbacks; otherwise a second worker can incorrectly treat a
    # healthy owner as lost and race the terminal write.
    heartbeat_stop = threading.Event()

    def heartbeat() -> None:
        interval = max(1.0, min(5.0, float(lease_seconds) / 3.0))
        while not heartbeat_stop.wait(interval):
            renew_lease(job_id, lease_seconds=lease_seconds)

    heartbeat_thread = threading.Thread(target=heartbeat, daemon=True,
                                        name=f"lrm-queue-heartbeat-{job_id}")
    heartbeat_thread.start()

    def progress(phase: str) -> None:
        set_phase(job_id, str(phase)[:240])
        renew_lease(job_id, lease_seconds=lease_seconds)

    try:
        result = dispatch(job, progress)
        current = get(job_id)
        if current and current.get("cancelRequested"):
            _write_terminal(job_id, "cancelled", "cancelled", None,
                            current.get("cancelReason") or "任务已取消")
        else:
            _write_terminal(job_id, "done", "done", result, None)
    except JobCancelled as exc:
        _write_terminal(job_id, "cancelled", "cancelled", None, str(exc) or "任务已取消")
    except Exception as exc:  # noqa: BLE001 - worker boundary must persist failure
        _write_terminal(job_id, "error", "error", None, str(exc)[:500])
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=0.2)
    return True


def active_for(report_id: str, dataset_id: str) -> str | None:
    conn = _open_db()
    if conn is not None:
        try:
            row = conn.execute(
                "SELECT id FROM jobs WHERE status='running' AND report_id=? AND dataset_id=?",
                (report_id, dataset_id),
            ).fetchone()
            return row["id"] if row else None
        finally:
            conn.close()
    with _LOCK:
        for job in _JOBS.values():
            if job["status"] == "running" and job["reportId"] == report_id \
                    and job["datasetId"] == dataset_id:
                return job["id"]
    return None


def find_by_request(report_id: str, request_key: str, request_hash: str | None = None) -> dict[str, Any] | None:
    """Find a prior request in the same report for idempotent HTTP retries."""
    if not request_key:
        return None
    conn = _open_db()
    if conn is not None:
        try:
            job = _row_to_job(conn.execute(
                "SELECT * FROM jobs WHERE report_id=? AND request_key=?",
                (report_id, request_key),
            ).fetchone())
            if job and request_hash and job.get("requestHash") != request_hash:
                raise JobRequestConflict(job["id"])
            return job
        finally:
            conn.close()
    with _LOCK:
        for job in _JOBS.values():
            if job.get("reportId") == report_id and job.get("requestKey") == request_key:
                result = dict(job)
                if request_hash and result.get("requestHash") != request_hash:
                    raise JobRequestConflict(result["id"])
                return result
    return None


def cancel(job_id: str, reason: str = "任务已取消") -> bool:
    """Request cancellation; return False for unknown/terminal jobs."""
    conn = _open_db()
    if conn is not None:
        try:
            now = time.time()
            row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row["status"] not in {"running", "queued"}:
                return False
            if row["status"] == "queued":
                cur = conn.execute(
                    "UPDATE jobs SET status='cancelled',phase='cancelled',cancel_requested=1,cancel_reason=?,finished_at=?,updated_at=? WHERE id=? AND status='queued'",
                    (reason, now, now, job_id))
            else:
                cur = conn.execute(
                    """UPDATE jobs SET cancel_requested=1, cancel_reason=?, phase='cancelling',
                       updated_at=? WHERE id=? AND status='running'""",
                    (reason, now, job_id),
                )
            if cur.rowcount == 1:
                with _CANCEL_CACHE_LOCK:
                    _CANCEL_CACHE[job_id] = (time.monotonic(), True)
                _record_event(job_id, "cancelled" if row["status"] == "queued" else "running",
                              "cancelled" if row["status"] == "queued" else "cancelling", reason)
            return cur.rowcount == 1
        finally:
            conn.close()
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None or job["status"] not in {"running", "queued"}:
            return False
        job["cancelRequested"] = True
        job["cancelReason"] = reason
        if job["status"] == "queued":
            job["status"] = "cancelled"; job["phase"] = "cancelled"; job["finishedAt"] = time.time()
        else:
            job["phase"] = "cancelling"
        _JOB_EVENTS.setdefault(job_id, []).append({"status": job["status"], "phase": job["phase"],
                                                    "at": time.time(), "detail": reason})
        with _CANCEL_CACHE_LOCK:
            _CANCEL_CACHE[job_id] = (time.monotonic(), True)
        return True


def recover_lost(job_id: str, token: str) -> dict[str, Any]:
    """Safely requeue one lost durable job after a worker restart.

    Recovery is explicit and token-protected.  The original job id/request hash
    are retained, so a proxy retry still observes the same idempotency record;
    only jobs whose creator declared a replay policy can return to ``queued``.
    Append imports and unknown future side-effecting tools therefore remain
    ``lost`` and require a fresh user request instead of risking duplication.
    """
    if not verify_retry_token(job_id, token):
        raise JobRecoveryDenied("任务 retry token 无效")
    conn = _open_db()
    if conn is not None:
        try:
            now = time.time()
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            job = _row_to_job(row)
            if job is None:
                raise JobRecoveryDenied("任务不存在或已过期")
            if job.get("status") != "lost":
                raise JobRecoveryDenied("只有 lost 任务可以安全重试")
            if not is_recoverable(job):
                raise JobRecoveryDenied("该任务不声明可安全重放，请重新发起原操作")
            duplicate = conn.execute(
                "SELECT id FROM jobs WHERE status IN ('running','queued') "
                "AND report_id=? AND dataset_id=? AND id<>?",
                (job["reportId"], job["datasetId"], job_id),
            ).fetchone()
            if duplicate is not None:
                raise JobConflict(duplicate["id"])
            cur = conn.execute(
                """UPDATE jobs SET status='queued', phase='queued',
                   finished_at=NULL, error=NULL, cancel_requested=0,
                   cancel_reason=NULL, owner_pid=0, owner_token='',
                   updated_at=?, heartbeat_at=?, lease_until=0
                   WHERE id=? AND status='lost'""",
                (now, now, job_id),
            )
            if cur.rowcount != 1:
                raise JobRecoveryDenied("任务状态已变化，请刷新任务列表")
            conn.commit()
            _record_event(job_id, "queued", "queued", "lost 任务已安全重新排队")
            return get(job_id) or {}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None or job.get("status") != "lost":
            raise JobRecoveryDenied("只有 lost 任务可以安全重试")
        if not is_recoverable(job):
            raise JobRecoveryDenied("该任务不声明可安全重放，请重新发起原操作")
        for other in _JOBS.values():
            if other.get("id") != job_id and other.get("status") in {"queued", "running"} \
                    and other.get("reportId") == job.get("reportId") \
                    and other.get("datasetId") == job.get("datasetId"):
                raise JobConflict(other["id"])
        now = time.time()
        job.update({"status": "queued", "phase": "queued", "finishedAt": None,
                    "error": None, "cancelRequested": False, "cancelReason": None,
                    "ownerToken": _PROCESS_TOKEN, "heartbeatAt": now, "leaseUntil": 0})
        _JOB_EVENTS.setdefault(job_id, []).append({"status": "queued", "phase": "queued",
                                                    "at": now, "detail": "lost 任务已安全重新排队"})
        job_copy = dict(job)
    return job_copy


def is_cancel_requested(job_id: str) -> bool:
    now_mono = time.monotonic()
    with _CANCEL_CACHE_LOCK:
        cached = _CANCEL_CACHE.get(job_id)
        if cached is not None and now_mono - cached[0] < _CANCEL_CACHE_TTL:
            return cached[1]
    conn = _open_db()
    if conn is not None:
        try:
            row = conn.execute(
                "SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            value = bool(row and row["cancel_requested"])
            with _CANCEL_CACHE_LOCK:
                _CANCEL_CACHE[job_id] = (now_mono, value)
            return value
        finally:
            conn.close()
    with _LOCK:
        job = _JOBS.get(job_id)
        value = bool(job and job.get("cancelRequested"))
    with _CANCEL_CACHE_LOCK:
        _CANCEL_CACHE[job_id] = (now_mono, value)
    return value


def _write_terminal(job_id: str, status: str, phase: str,
                    result: Any, error: str | None) -> None:
    now = time.time()
    conn = _open_db()
    if conn is not None:
        try:
            cur = conn.execute(
                """UPDATE jobs SET
                       status=CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE ? END,
                       phase=CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE ? END,
                       result_json=CASE WHEN cancel_requested=1 THEN NULL ELSE ? END,
                       error=CASE WHEN cancel_requested=1
                                  THEN COALESCE(NULLIF(cancel_reason, ''), ?)
                                  ELSE ? END,
                       finished_at=?, updated_at=?, lease_until=0
                   WHERE id=? AND owner_token=? AND status='running'""",
                (status, phase, _encode_result(result), "任务已取消", error,
                 now, now, job_id, _PROCESS_TOKEN),
            )
            terminal = conn.execute(
                "SELECT status, phase, error FROM jobs WHERE id=? AND owner_token=?",
                (job_id, _PROCESS_TOKEN),
            ).fetchone() if cur.rowcount == 1 else None
        finally:
            conn.close()
        if terminal is not None:
            # Cancellation and terminal publication are one fenced SQL update:
            # a cancel that wins the race must not leak the completed result.
            _record_event(job_id, terminal["status"], terminal["phase"], terminal["error"])
        with _CANCEL_CACHE_LOCK:
            if len(_CANCEL_CACHE) > _KEEP_FINISHED * 2:
                _CANCEL_CACHE.clear()
        return
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is not None and job.get("ownerToken") == _PROCESS_TOKEN and job.get("status") == "running":
            cancelled = bool(job.get("cancelRequested"))
            terminal_status = "cancelled" if cancelled else status
            terminal_phase = "cancelled" if cancelled else phase
            terminal_error = (job.get("cancelReason") or "任务已取消") if cancelled else error
            job["status"] = terminal_status
            job["phase"] = terminal_phase
            job["result"] = None if cancelled else result
            job["error"] = terminal_error
            job["finishedAt"] = now
            job["leaseUntil"] = 0
            _JOB_EVENTS.setdefault(job_id, []).append({"status": terminal_status, "phase": terminal_phase,
                                                        "at": now, "detail": terminal_error})


def _touch(job_id: str) -> None:
    now = time.time()
    conn = _open_db()
    if conn is not None:
        try:
            conn.execute("UPDATE jobs SET heartbeat_at=?, updated_at=? WHERE id=? AND owner_token=? AND status='running'",
                         (now, now, job_id, _PROCESS_TOKEN))
        finally:
            conn.close()
        return
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is not None and job.get("ownerToken") == _PROCESS_TOKEN and job.get("status") == "running":
            job["heartbeatAt"] = now


def run(job_id: str, work: Callable[[Callable[[str], None]], dict]) -> None:
    """在守护线程执行 ``work(progress)``；结果/异常写回共享注册表。"""

    def _execute() -> None:
        heartbeat_stop = threading.Event()

        def heartbeat() -> None:
            while not heartbeat_stop.wait(5.0):
                _touch(job_id)

        heartbeat_thread = threading.Thread(target=heartbeat, daemon=True,
                                             name=f"lrm-heartbeat-{job_id}")
        heartbeat_thread.start()

        def progress(label: str) -> None:
            set_phase(job_id, label)

        try:
            result = work(progress)
            job = get(job_id)
            cancelled = bool(job and job.get("cancelRequested"))
            reason = (job or {}).get("cancelReason") or "任务已取消"
            _write_terminal(job_id, "cancelled" if cancelled else "done",
                            "cancelled" if cancelled else "done",
                            None if cancelled else result, reason if cancelled else None)
        except JobCancelled as exc:
            job = get(job_id)
            reason = (job or {}).get("cancelReason") or str(exc) or "任务已取消"
            _write_terminal(job_id, "cancelled", "cancelled", None, reason)
        except Exception as exc:  # noqa: BLE001 — 任务边界必须兜住一切，错误进轮询
            job = get(job_id)
            cancelled = bool(job and job.get("cancelRequested"))
            reason = (job or {}).get("cancelReason") or str(exc) or "任务已取消"
            _write_terminal(job_id, "cancelled" if cancelled else "error",
                            "cancelled" if cancelled else "error", None,
                            reason if cancelled else (str(exc) or exc.__class__.__name__))
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=0.2)
            _prune()

    threading.Thread(target=_execute, daemon=True, name=f"lrm-job-{job_id}").start()


def set_phase(job_id: str, phase: str) -> None:
    conn = _open_db()
    if conn is not None:
        try:
            now = time.time()
            cur = conn.execute("UPDATE jobs SET phase=?, updated_at=?, heartbeat_at=? WHERE id=? AND owner_token=? AND status='running'",
                         (phase, now, now, job_id, _PROCESS_TOKEN))
            if cur.rowcount == 1:
                _record_event(job_id, "running", phase)
        finally:
            conn.close()
        return
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is not None and job.get("ownerToken") == _PROCESS_TOKEN and job.get("status") == "running":
            job["phase"] = phase
            job["heartbeatAt"] = time.time()
            _JOB_EVENTS.setdefault(job_id, []).append({"status": "running", "phase": phase,
                                                        "at": time.time(), "detail": None})


def get(job_id: str) -> dict[str, Any] | None:
    _prune()
    if _PERSIST_PATH is not None:
        return _persistent_get(job_id)
    with _LOCK:
        job = _JOBS.get(job_id)
        return dict(job) if job is not None else None


def summary() -> dict[str, int]:
    """Cheap lifecycle counters for health/diagnostics, without result payloads."""
    conn = _open_db()
    if conn is not None:
        try:
            out = {"running": 0, "done": 0, "error": 0,
                   "cancelled": 0, "lost": 0}
            for row in conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"):
                if row["status"] in out:
                    out[row["status"]] = int(row["n"])
            return out
        finally:
            conn.close()
    with _LOCK:
        out = {"running": 0, "done": 0, "error": 0,
               "cancelled": 0, "lost": 0}
        for job in _JOBS.values():
            if job["status"] in out:
                out[job["status"]] += 1
        return out


def _prune() -> None:
    """终态任务只留最近 N 条且不超过 TTL，避免结果长期占内存或磁盘。"""
    conn = _open_db()
    if conn is not None:
        try:
            now = time.time()
            conn.execute(
                """DELETE FROM jobs WHERE status <> 'running'
                   AND ? - COALESCE(finished_at, ?) > ?""",
                (now, now, _FINISHED_TTL_SECONDS),
            )
            old = conn.execute(
                """SELECT id FROM jobs WHERE status <> 'running'
                   ORDER BY COALESCE(finished_at, 0) DESC LIMIT -1 OFFSET ?""",
                (_KEEP_FINISHED,),
            ).fetchall()
            for row in old:
                conn.execute("DELETE FROM jobs WHERE id=?", (row["id"],))
        finally:
            conn.close()
        return
    with _LOCK:
        now = time.time()
        expired = [key for key, job in _JOBS.items()
                    if job["status"] != "running"
                    and now - (job.get("finishedAt") or now) > _FINISHED_TTL_SECONDS]
        for key in expired:
            _JOBS.pop(key, None)
        finished = sorted(
            ((key, job.get("finishedAt") or 0.0) for key, job in _JOBS.items()
             if job["status"] != "running"),
            key=lambda pair: pair[1],
        )
        for key, _ in finished[:-_KEEP_FINISHED] if len(finished) > _KEEP_FINISHED else []:
            _JOBS.pop(key, None)
    with _CANCEL_CACHE_LOCK:
        if len(_CANCEL_CACHE) > _KEEP_FINISHED * 2:
            _CANCEL_CACHE.clear()
