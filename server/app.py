"""HTTP server for the report runtime.

Uses Python stdlib http.server + BaseHTTPRequestHandler. Zero pip
dependency. Designed for loopback (127.0.0.1) by default.

The handler dispatches by method + path pattern. Route patterns use
`:name` for path parameters (no regex knowledge required by callers).

Each handler receives the BaseHTTPRequestHandler instance and parsed
path parameters. They read the JSON body via `read_json(handler)` and
respond via `respond_json(handler, status, payload)`.
"""
from __future__ import annotations

import gzip
import hashlib
import hmac
import json
import csv
import io
import os
import re
import socket
import sys
import threading
import time
import copy
import math
import uuid
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, unquote, urlparse

from .aggregator import (AggregationError, aggregate, aggregate_multi, aggregate_cache_summary,
                         explain_index_candidate, index_suggestions, parse_request)
from .index_advisor import (IndexAdvisor, IndexAdvisorError, IndexProposalConflict,
                            IndexProposalNotFound)
from .store import (DERIVED_KIND, apply_dataset_derived, apply_dim_groups, columns_decl,
                    dim_group_table_defs, dim_groups_defs, ensure_dim_groups,
                    ensure_table_defs, replay_all as replay_columns,
                    table_defs)
from . import SKILL_VERSION
from . import jobs as _jobs
from .registry import Registry, snapshot_data_file
from ._platform import (
    load as index_load,
    ping as index_ping,
    prune_dead,
    register as index_register,
    unregister as index_unregister,
)
from .store import Engine, TableInfo, TableSchemaError, get_engine
from .aggregator import validate_truth

_COPILOT_JOB_TIMEOUT_SECONDS = 120
_COPILOT_RATE_WINDOW_SECONDS = 60
_COPILOT_RATE_LIMIT = 30
_COPILOT_RATE_LOCK = threading.Lock()
_COPILOT_RATE: dict[str, list[float]] = {}
_MONITOR_TOOLS = frozenset({"anomaly_scan", "compare_periods", "data_quality", "profile_dataset"})


def _validate_monitor_definition(tool: Any, args: Any) -> tuple[str, dict[str, Any]]:
    """Validate a durable monitor at creation, not only when it eventually runs.

    A monitor is a persisted Agent tool call. Reuse the same Tool Registry
    schema so malformed/side-effecting payloads fail before becoming a
    long-lived scheduled job; the scheduler remains read-only by construction.
    """
    name = str(tool or "")
    if name not in _MONITOR_TOOLS or not isinstance(args, dict):
        raise ValueError("monitor requires a supported read tool and object args")
    from .agent_tools import ToolContractError, default_registry
    try:
        registry = default_registry()
        spec = registry.get(name)
        if spec.side_effect != "none":
            raise ToolContractError("monitor tool must be read-only")
        registry.validate_args(name, args)
    except ToolContractError as exc:
        raise ValueError(f"monitor args invalid: {exc}") from exc
    return name, dict(args)


def _monitor_evidence_hash(value: Any) -> str:
    """Hash stable monitor evidence so repeated unchanged runs can be deduped."""
    def stable(item: Any) -> Any:
        if isinstance(item, Mapping):
            return {str(key): stable(child) for key, child in sorted(item.items(), key=lambda pair: str(pair[0]))
                    if str(key) not in {"elapsedMs", "evidenceHash", "changed", "previousEvidenceHash"}}
        if isinstance(item, list):
            return [stable(child) for child in item]
        return item
    payload = json.dumps(stable(value), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _agent_subject_hash(handler: Any) -> str:
    """Return a stable, non-reversible identity bucket for governance/rate limits."""
    claims = getattr(handler, "agent_claims", None) or {}
    subject = str(claims.get("subject") or "")
    if not subject:
        address = getattr(handler, "client_address", ("unknown", 0))
        subject = f"ip:{address[0]}"
    return hashlib.sha256(subject.encode("utf-8")).hexdigest()[:32]


def _report_tenant_id(entry: Mapping[str, Any] | None) -> str:
    """Read an optional report tenant boundary without exposing its value."""
    if not isinstance(entry, Mapping):
        return ""
    manifest = entry.get("manifest") if isinstance(entry.get("manifest"), Mapping) else {}
    security = manifest.get("security") if isinstance(manifest.get("security"), Mapping) else {}
    value = entry.get("tenantId") or manifest.get("tenantId") or security.get("tenantId")
    return str(value or "").strip()[:120]


def _validate_tenant_id(value: Any) -> str:
    tenant = str(value or "").strip()
    if tenant and not re.fullmatch(r"[A-Za-z0-9._:-]{1,120}", tenant):
        raise BadRequest("tenantId must contain only bounded ASCII scope characters")
    return tenant


def _tenant_scope_hash(handler: Any) -> str:
    claims = getattr(handler, "agent_claims", None) or {}
    tenant = str(claims.get("tenantId") or "")
    return hashlib.sha256(tenant.encode("utf-8")).hexdigest()[:32] if tenant else "none"


def _governance_scope(handler: Any, report_id: str) -> str:
    """Scope provider budgets by report + hashed actor, never raw identity."""
    return f"{report_id}\x00{_tenant_scope_hash(handler)}\x00{_agent_subject_hash(handler)}"


def _notification_proposal_id(report_id: str, channel: str, destination: str,
                              message: str) -> str:
    proposal = {"reportId": report_id, "channel": channel,
                "destination": destination, "message": message,
                "requiresApproval": True, "sideEffect": "notify"}
    return "notify-" + hashlib.sha256(
        json.dumps(proposal, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:20]


def _serialize_evidence_export(value: Any, fmt: str, cancel_check: Callable[[], bool] | None = None) -> tuple[bytes, str, str]:
    """Serialize bounded Agent evidence without exposing server paths."""
    def check_cancel() -> None:
        if cancel_check is not None and cancel_check():
            raise _jobs.JobCancelled("证据导出已取消")

    if fmt == "json":
        encoder = json.JSONEncoder(ensure_ascii=False, indent=2, default=str)
        chunks: list[str] = []
        for index, chunk in enumerate(encoder.iterencode(value)):
            if index % 64 == 0:
                check_cancel()
            chunks.append(chunk)
        check_cancel()
        return "".join(chunks).encode("utf-8"), "application/json; charset=utf-8", "json"
    if fmt == "markdown":
        raw_json, _content_type, _extension = _serialize_evidence_export(value, "json", cancel_check)
        lines = ["# Agent evidence", "", "```json", raw_json.decode("utf-8"), "```", ""]
        return "\n".join(lines).encode("utf-8"), "text/markdown; charset=utf-8", "md"
    if fmt == "csv":
        rows = value.get("rows") if isinstance(value, dict) else None
        columns = value.get("columns") if isinstance(value, dict) else None
        if not isinstance(rows, list):
            rows = []
        if not isinstance(columns, list):
            columns = []
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        if columns:
            writer.writerow(columns)
        for index, row in enumerate(rows):
            if index % 128 == 0:
                check_cancel()
            writer.writerow(row if isinstance(row, list) else [row])
        check_cancel()
        return stream.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8", "csv"
    raise BadRequest("format must be json, csv or markdown")


# ---------------------- response helpers ----------------------------------------

def read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    # 非数字 Content-Length（畸形直呼）是 400 而不是未捕获 ValueError 的 500；
    # 负数按无 body 处理（rfile.read(负数) 的语义未定义，绝不依赖）。
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        raise BadRequest("invalid Content-Length header")
    if length <= 0:
        return {}
    if length > 512 * 1024 * 1024:
        # 上传上限与 h_register 的 5M 行校验同量级但**先于读入**——否则一个
        # 谎报/超大 body 会先把内存吃满才被行数校验拒掉。
        raise BadRequest("request body too large (max 512MB)")
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise BadRequest(f"invalid JSON body: {e}") from e


def _read_body_bytes(handler: BaseHTTPRequestHandler, cap: int = 512 * 1024 * 1024) -> bytes:
    """Raw body reader for non-JSON uploads（同一 512MB 先于读入的守卫口径）."""
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        raise BadRequest("invalid Content-Length header")
    if length <= 0:
        raise BadRequest("empty request body")
    if length > cap:
        raise BadRequest(f"request body too large (max {cap // (1024 * 1024)}MB)")
    return handler.rfile.read(length)


def _reject_duplicate_columns(cols: list, where: str) -> None:
    """重复表头闸（fail-closed，单一文案真源）。

    下游按列名对位投影（``new_names.index(c)`` 恒取首现）而结构闸又是集合
    比较——``[A,A,B]`` 对 ``[A,B]`` 会放行，第二个 A 整列**静默丢弃**。CSV/TSV
    直传路径（``_parse_delimited_table``）与 JSON/xlsx 上传入口（replace/add）
    必须共用同一道闸、同一文案（2026-09-26 结构闸口径统一）。"""
    names = [str(c) for c in cols]
    dupes = sorted({c for c in names if names.count(c) > 1})
    if dupes:
        raise BadRequest(
            where + "表头含重复列名（" + ", ".join(dupes[:5]) +
            "）。上传按列名对位，重复列的数据会被丢弃——请先在源文件里改成唯一列名再上传。")


def _parse_delimited_table(handler: BaseHTTPRequestHandler) -> dict:
    """CSV/TSV raw body → {columns, rows}（2026-09-18 大增量直传）。

    浏览器把原始文件字节直接 POST 上来（Content-Type: text/csv |
    text/tab-separated-values），由服务端 Python csv 解析——客户端不再
    parse+stringify 几十 MB 的增量。字符串值保留原样（与浏览器 parseDelimited
    同口径：数值判定交给装载期的 ai_num/类型推断）；utf-8-sig 容忍 BOM；
    短行补空、长行截断（与客户端同一宽表化规则）。
    """
    import csv as _csv
    import io as _io

    delim = "\t" if (handler.headers.get("Content-Type") or "").lower().startswith(
        "text/tab-separated-values") else ","
    raw = _read_body_bytes(handler)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        # Excel on zh-CN Windows commonly writes CSV as ANSI/GBK — mirror the
        # compile-side reader (scripts/table_input.py::_read_csv) and fall back
        # to GB18030 instead of rejecting the increment with a 400.
        text = raw.decode("gb18030", errors="replace")
    try:
        records = list(_csv.reader(_io.StringIO(text), delimiter=delim))
    except _csv.Error as e:
        raise BadRequest(f"invalid CSV: {e}") from e
    records = [r for r in records if any(str(f).strip() for f in r)]
    if len(records) < 2:
        raise BadRequest("CSV/TSV 至少需要表头 + 一行数据")
    cols = [str(c).strip() for c in records[0]]
    if not cols or any(not c for c in cols):
        raise BadRequest("CSV/TSV 表头含空列名")
    # 重复表头直接拒（结构闸 fail-closed）：下游按列名对位投影，重名第二列的
    # 数据会被静默丢弃（编译侧同场景是改名后走列集闸 400——两条路径口径必须
    # 一致，都不能静默改宽度）。
    _reject_duplicate_columns(cols, "CSV/TSV ")
    rows = []
    for r in records[1:]:
        rows.append(list(r[:len(cols)]) + [""] * (len(cols) - len(r)))
    return {"columns": cols, "rows": rows}


_MANIFEST_COLUMN_TYPES = ("category", "measure", "time", "id", "dimension")


def _manifest_typed_columns(names: list, rows: list | None = None,
                            prev_cols: list | None = None,
                            declared_types: dict | None = None) -> list[dict]:
    """manifest 数据集条目的列形状 = ``[{name, type}]``（编译侧远程分支同款，
    spec_to_report 远程 strip-rows 的服务端孪生）。

    运行时的列分类（chart-adapter ``numericColumns``/``dimensionColumns``：
    fieldRoles → measureTypes → **columns[i].type** → 值探测）在 schema-only 的
    server 数据集上只能靠 ``columns[i].type``——服务端没有行数据供前端探测。
    type 缺失（历史上的 ``[{"name": c}]`` 写法）会让每个基础列都「不可分析」：
    插入图表 alert「数据集至少需要一个维度和一个指标」、字段面板丢整个指标区
    （第八轮审视真机实锤：PSI 一次 upsert 后插入流程全断）。

    type 真源优先级：替换前同名列的既有 type（刷新契约 = 结构延续，绝不让一次
    数据刷新把作者已经用着的列语义洗掉）> 上传表自带 meta.types
    （load_table_file → validate_table 的推断产物）> 值推断
    （validate_table._infer_column_type，与编译期同一实现，无第二份推断）>
    兜底 dimension。"""
    prev: dict[str, str] = {}
    for oc in (prev_cols or []):
        if isinstance(oc, dict) and oc.get("name") and oc.get("type") in _MANIFEST_COLUMN_TYPES:
            prev[str(oc["name"])] = oc["type"]
    declared = declared_types or {}
    out: list[dict] = []
    for i, name in enumerate(names):
        name = str(name)
        t = prev.get(name) or declared.get(name)
        if t not in _MANIFEST_COLUMN_TYPES:
            t = None
            if rows:
                try:
                    from validate_table import _infer_column_type
                    values = [row[i] for row in rows if isinstance(row, list) and i < len(row)]
                    t = _infer_column_type(values, name)
                except Exception:
                    t = None
            out.append({"name": name, "type": t if t in _MANIFEST_COLUMN_TYPES else "dimension"})
        else:
            out.append({"name": name, "type": t})
    return out


def respond_json(handler: BaseHTTPRequestHandler, status: int, payload: Any,
                 extra_headers: dict[str, str] | None = None) -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    _send_body(handler, status, body, "application/json; charset=utf-8",
               {"Cache-Control": "no-store"}, extra_headers)


# ---------------------- transport: gzip + ETag（性能批 2026-09-25）---------------
# 报告页 HTML 因模板内联全部 vendored 资产（echarts 1.1MB + runtime 1.5MB +
# adapter/CSS/gridstack ≈ 3.2MB），旧实现无压缩、恒 no-store——每次打开全量
# 重下。现在：响应体 >1KB 且客户端 Accept-Encoding 允许时 gzip（实测 3.2MB →
# ~0.8MB）；页面 HTML 另发内容哈希 ETag + Cache-Control: no-cache（协商缓存，
# 每次打开都 revalidate——报告保存后 ETag 变化自然拿到新 200，未变则 304 零
# 载荷）。API JSON 恒 no-store（状态可变），只压不缓存。

_GZIP_MIN_BYTES = 1024


def _accepts_gzip(handler: BaseHTTPRequestHandler) -> bool:
    """RFC 7231 Accept-Encoding 判定（token 级；q=0 拒绝）。"""
    ae = handler.headers.get("Accept-Encoding") or ""
    for part in ae.split(","):
        token, _sep, params = part.partition(";")
        if token.strip().lower() != "gzip":
            continue
        q = params.strip()
        if q[:2].lower() == "q=":
            try:
                if float(q[2:]) <= 0:
                    continue
            except ValueError:
                pass
        return True
    return False


def _send_body(handler: BaseHTTPRequestHandler, status: int, body: bytes, content_type: str,
               headers: dict[str, str] | None = None,
               extra_headers: dict[str, str] | None = None,
               compress: bool = True) -> None:
    out_headers = dict(headers or {})
    if extra_headers:
        out_headers.update(extra_headers)
    if compress and len(body) > _GZIP_MIN_BYTES and _accepts_gzip(handler):
        body = gzip.compress(body, 6)
        out_headers["Content-Encoding"] = "gzip"
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    for key, value in out_headers.items():
        handler.send_header(key, value)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _send_html_page(handler: BaseHTTPRequestHandler, html: str) -> None:
    """页面 HTML：gzip + 内容哈希 ETag 协商缓存（未变 → 304 零载荷）。"""
    body = html.encode("utf-8")
    etag = '"' + hashlib.sha1(body).hexdigest() + '"'
    if (handler.headers.get("If-None-Match") or "").strip() == etag:
        handler.send_response(304)
        handler.send_header("ETag", etag)
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Vary", "Accept-Encoding")
        handler.end_headers()
        return
    _send_body(handler, 200, body, "text/html; charset=utf-8",
               {"Cache-Control": "no-cache", "ETag": etag, "Vary": "Accept-Encoding"},
               compress=True)


def is_loopback(handler: BaseHTTPRequestHandler) -> bool:
    """True when the request originates from THIS machine.

    Gate for machine-local operations (shutting a server down, showing edit
    passwords): a LAN visitor of a shared report must never be able to stop
    the host's servers or read its passwords."""
    host = handler.client_address[0] if handler.client_address else ""
    return host in {"127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"}


_LOOPBACK_HOSTNAMES = {"127.0.0.1", "localhost", "::1"}


def _hostport_of(value: str, default_port: int) -> tuple[str, int]:
    """Split a Host/Origin authority into (hostname.lower(), port)."""
    s = (value or "").strip()
    if not s:
        return ("", 0)
    if s.startswith("["):  # IPv6 literal: [::1]:8765
        host, _, rest = s[1:].partition("]")
        port = int(rest[1:]) if rest.startswith(":") and rest[1:].isdigit() else default_port
        return (host.lower(), port)
    host, sep, port = s.rpartition(":")
    if sep and port.isdigit():
        return (host.lower(), int(port))
    return (s.lower(), default_port)


def _same_origin_guard(handler: BaseHTTPRequestHandler) -> bool:
    """Drive-by protection for the management endpoints (安全 P1，2026-09-16).

    ``is_loopback`` alone trusts the TCP peer — but a MALICIOUS WEB PAGE open
    in the author's browser also connects from 127.0.0.1, so it passes the
    gate and can silently read edit passwords / shut servers down. Browsers
    attest the embedding page with an ``Origin`` header on cross-site
    fetch/XHR, so:

    - no ``Origin`` → allow (curl / build tooling / same-origin GET, which
      most browsers send without an Origin header);
    - ``Origin`` host:port equals the request ``Host`` → allow (exact
      same-origin, incl. a LAN author saving a manifest via the LAN address);
    - both hostnames loopback but ports differ → allow: the 报告管理 page on
      port A shuts down sibling servers on port B (cross-port BY DESIGN);
    - anything else (``https://evil.example``, ``Origin: null`` from a
      file:// page) → 403.

    Management responses also never send ``Access-Control-Allow-Origin: *``
    anymore — that header let ANY web page READ the password-carrying
    responses cross-origin. Residual (accepted): DNS-rebinding pages send no
    Origin; the password-less endpoints stay exposure-bound to a rebinding
    page that already knows the port. h_update_manifest remains password-
    gated, so rebinding alone gains nothing there."""
    origin = (handler.headers.get("Origin") or "").strip()
    if not origin:
        return True
    try:
        o = urlparse(origin)
    except ValueError:
        return False
    ohost = (o.hostname or "").lower()
    if not ohost:
        return False  # e.g. Origin: null (file:// / sandboxed iframe)
    oport = o.port or (443 if (o.scheme or "").lower() == "https" else 80)
    hhost, hport = _hostport_of(handler.headers.get("Host"), 80)
    if ohost == hhost and oport == hport:
        return True
    if ohost in _LOOPBACK_HOSTNAMES and hhost in _LOOPBACK_HOSTNAMES:
        return True
    return False


def respond_error(handler: BaseHTTPRequestHandler, status: int, code: str, message: str) -> None:
    respond_json(handler, status, {"error": {"code": code, "message": message}})


def write_dataset_snapshot(path: Path, columns: list, rows: list) -> Path:
    """数据集快照写盘（XP-01 双格式，2026-10-24）：薄委托 ``store.write_snapshot``。

    逐行流式直写临时文件 + ``os.replace`` 原子落盘（JSON 是 2026-09-25 性能 P1
    的原实现；SQLite 是 XP-01 新缺省——``LRM_SNAPSHOT_FORMAT``，缺省 sqlite）。
    传入的 ``path`` 带何种后缀都会被换成当前格式的后缀，**返回实际落盘路径**：
    调用点凡是把 path 记进 registry/manifest 的，都必须改用返回值（否则注册表
    source 指向一个不存在的 .json，重启后数据集蒸发）。IO/SQLite 失败仍抛
    OSError，由调用方按既有文案转 BadRequest（fail-closed 不静默）。"""
    from .store import write_snapshot
    return write_snapshot(path, columns, rows)


def _register_full_entry(registry, report_id: str, entry: dict) -> None:
    """Persist a complete entry for non-job handlers.

    Replace jobs must use ``_commit_replace_result`` instead: their entry is a
    stale task-start snapshot and writing it wholesale can roll back a newer
    editor save.  Keeping ordinary full-entry writes behind this explicitly
    named seam also makes that distinction visible to the source-level guard.
    """
    registry.register(report_id, entry)


def _commit_replace_result(registry, reportId: str, entry: dict, datasetId: str) -> None:
    """替换/增量/追加任务的收尾落表（lost-update 防护，2026-09-26）。

    异步任务线程（xlsx 重解析/合并要跑几分钟）手里的 ``entry`` 是任务启动时的
    副本；期间作者的一次在线保存（h_update_manifest overwrite）已经把新
    manifest 写进注册表——整条 ``registry.register(entry)`` 会把保存静默回滚
    （真机现象：任务跑完，几分钟前保存的图表布局全部消失）。这里改为重读注册
    表**最新** entry，只把本任务触碰的字段合并上去：

    - 目标数据集的注册表记账（rowCount/primaryKey/source、retiredColumns 清除）；
    - 目标数据集的 manifest 数据集记账（rowCount/columns/primaryKey、过期
      columnStats/filterOptions 清除）——任务产物是替换后的真值，**恒覆盖**
      （编辑器保存的副本对这些字段只会更旧）；
    - 本任务的一条 changelog 追加到最新历史之后。

    其余字段（刚保存的 manifest、标题、口令策略、bodyHtml）以最新为准。
    """
    latest = registry.get(reportId)
    if latest is None:
        # The report may have been deleted while the durable job was running.
        # Never resurrect a stale task snapshot: that would undo the deletion
        # and reintroduce the lost-update bug this merge helper is meant to
        # prevent.  The engine/snapshot work is already complete, but there is
        # no live registry entry to commit it to.
        return
    task_meta = next((d for d in (entry.get("datasets") or [])
                      if isinstance(d, dict) and d.get("id") == datasetId), None)
    if isinstance(task_meta, dict):
        hit = False
        for d in (latest.get("datasets") or []):
            if isinstance(d, dict) and d.get("id") == datasetId:
                d.update(task_meta)
                # 重建成功 = 退役物化列已随 DROP+CREATE 消失（与主管道同语义）
                d.pop("retiredColumns", None)
                hit = True
        if not hit:
            latest.setdefault("datasets", []).append(dict(task_meta))
    task_mds = next((d for d in ((entry.get("manifest") or {}).get("datasets") or [])
                     if isinstance(d, dict) and d.get("id") == datasetId), None)
    if isinstance(task_mds, dict):
        manifest = latest.get("manifest")
        if not isinstance(manifest, dict):
            manifest = {}
            latest["manifest"] = manifest
        mds = manifest.get("datasets")
        if not isinstance(mds, list):
            mds = []
            manifest["datasets"] = mds
        hit = False
        for d in mds:
            if isinstance(d, dict) and d.get("id") == datasetId:
                d.update(task_mds)
                # 数据一换，编译期算出的列统计/值域即过期（与主管道同语义）
                d.pop("columnStats", None)
                d.pop("filterOptions", None)
                hit = True
        if not hit:
            mds.append(dict(task_mds))
    task_log = entry.get("changelog") or []
    if task_log:
        # 任务恒在副本末尾追加恰好一条 changelog——取尾条合到最新历史之后。
        log = list(latest.get("changelog") or [])
        log.append(task_log[-1])
        latest["changelog"] = log
    registry.register(reportId, latest)


def _touch_dataset_revision(entry: dict, dataset_id: str, token: str | None = None) -> str:
    """Advance the opaque dataset revision in registry and manifest metadata.

    ``rowCount``/``dataAsOf`` are useful but do not change when a same-sized
    replacement edits values. The revision is intentionally an opaque,
    bounded token: it is only for stale-card fencing, never a data or path
    identifier. ReportContext includes it alongside the semantic card key so
    a delayed Agent result cannot target a same-shaped new snapshot.
    """
    value = str(token or f"r{time.time_ns():x}")[:40]
    for item in (entry.get("datasets") or []):
        if isinstance(item, dict) and item.get("id") == dataset_id:
            item["dataRevision"] = value
    manifest = entry.get("manifest")
    if isinstance(manifest, dict):
        for item in (manifest.get("datasets") or []):
            if isinstance(item, dict) and item.get("id") == dataset_id:
                item["dataRevision"] = value
    return value


class BadRequest(ValueError): pass
class NotFound(KeyError): pass
class Conflict(ValueError): pass
class ValidationFailed(ValueError): pass


# ════════ S2 数据面隐私闸（XU-04/FS-01，2026-10-24 Wave 3，E1 拍板=闸）════════
# NS-01 已经闸了两头：构建期 `_strip_sensitive_tables` 物理剥离（正常报告的表里
# 根本没有敏感列）+ Agent 面 `copilot._sensitive_columns/_assert_filter_privacy`
# 脱敏。这里补第三道：**server UI 数据面端点**（聚合/明细/值域）对「manifest 声明了
# privacy.sensitiveColumns 但物理表仍有该列」的历史报告/手改 manifest 收口。
# 语义与 Agent 面同口径（E1）：引用敏感列 → 400 可行动报错（只列列名，绝不回显
# 敏感值）；明细读 → 投影剔除（作者/读者面无感）。无声明 = 敏感集合为空，所有
# helper 第一行 short-circuit——代码路径与今天逐位一致（零漂移硬契约）。
# 列宇宙归一口径同 NS-01 编译侧（scripts/spec_to_report.py::_ds_privacy）：只收
# 非空字符串、列名逐字匹配（构建期剥离就是按这个集合剔的，这里不做大小写/空白
# 变换，避免「闸的集合 ≠ 剥离的集合」）。

def sensitive_cols_of(entry: Any, dataset_id: str) -> set[str]:
    """manifest.datasets[].privacy.sensitiveColumns 的数据面读取口（纯内存，无 IO）。

    只读调用方手里的注册表条目 dict（注册/重载时已加载进内存），绝不逐请求
    重读磁盘 manifest；id 对不上或形状不对 = 无声明 = 空集合（short-circuit）。"""
    out: set[str] = set()
    if not isinstance(entry, dict):
        return out
    datasets = (entry.get("manifest") or {}).get("datasets")
    if not isinstance(datasets, list):
        return out
    for d in datasets:
        if not (isinstance(d, dict) and d.get("id") == dataset_id):
            continue
        privacy = d.get("privacy")
        if not isinstance(privacy, dict):
            continue
        sc = privacy.get("sensitiveColumns")
        if isinstance(sc, list):
            out.update(c for c in sc if isinstance(c, str) and c)
    return out


def dataset_sensitive_cols(registry: Any, dataset_id: str) -> set[str]:
    """一张物理表的敏感列宇宙 = 所有声明它的注册条目取**并集**（保守）。

    多份报告可共享同一 dataset id（副本），任何一份声明了敏感列，数据面就按
    敏感对待——与 h_dataset_rows_delete 的 declaring 遍历同型。只遍历内存
    registry，无磁盘 IO。"""
    out: set[str] = set()
    try:
        items = registry.items_raw()
    except Exception:
        return out
    for _rid, e in items:
        out |= sensitive_cols_of(e, dataset_id)
    return out


def assert_not_sensitive(cols: Any, sensitive: set, what: str) -> None:
    """引用敏感列 → BadRequest（路由层统一转 400）。文案可行动：指明列名、
    用途去向、两条出路；**绝不回显敏感值**（只列列名）。sensitive 空 = 放行。"""
    if not sensitive:
        return
    hit = sorted({c for c in cols if isinstance(c, str) and c in sensitive})
    if hit:
        raise BadRequest(
            f"敏感列保护（privacy.sensitiveColumns）：{'、'.join(hit)} 不能用于{what}。"
            "该列已在构建期从报告数据中剥离；数据面仍出现它，说明本报告是历史构建"
            "或 manifest 被手工修改——请修正引用（筛选/分组/指标改用非敏感列），"
            "或重新构建报告后再试。")


def project_sensitive_rows(columns: list, rows: list, sensitive: set) -> tuple[list, list]:
    """明细投影剔除（E1：rows 读不 400，作者/读者面对敏感列无感）。

    columns 与每行按位置剥掉敏感列；sensitive 空 = 原样返回入参对象（零拷贝
    零漂移）。行长度与表头不一致的脏行保守整行保留（不按错位索引乱剥）。"""
    if not sensitive:
        return columns, rows
    keep = [i for i, c in enumerate(columns) if isinstance(c, str) and c not in sensitive]
    if len(keep) == len(columns):
        return columns, rows
    cols2 = [columns[i] for i in keep]
    rows2 = [[row[i] for i in keep]
             if isinstance(row, list) and len(row) == len(columns) else row
             for row in rows]
    return cols2, rows2


def _aggregate_payload_cols(payload: Any) -> list[str]:
    """一个聚合请求体里引用的全部列名（groupBy/measures/filter/orderBy/totalCalc/
    bucket.source/aggregations 键）。只做形状容忍的平铺收集，不做语义校验
    （校验仍归 aggregator.parse_request）。"""
    out: list[str] = []
    if not isinstance(payload, dict):
        return out
    out.extend(c for c in (payload.get("groupBy") or []) if isinstance(c, str))

    def _walk_filter(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                _walk_filter(item)
        elif isinstance(node, dict):
            if isinstance(node.get("column"), str):
                out.append(node["column"])
            for key in ("all", "any"):
                if node.get(key) is not None:
                    _walk_filter(node.get(key))

    filters = payload.get("filter")
    if isinstance(filters, dict) and not ({"all", "any"} & set(filters)):
        # 单谓词简写形状（{column,op,value}）——与 _filter_clause 的 list 形状并行。
        _walk_filter([filters])
    else:
        _walk_filter(filters)
    for m in (payload.get("measures") or []):
        if isinstance(m, str):
            out.append(m)
        elif isinstance(m, dict):
            if isinstance(m.get("column"), str):
                out.append(m["column"])
            _walk_filter(m.get("filters"))
    for o in (payload.get("orderBy") or []):
        if isinstance(o, dict) and isinstance(o.get("column"), str):
            out.append(o["column"])
    total_calc = payload.get("totalCalc")
    if isinstance(total_calc, dict):
        out.extend(c for c in total_calc if isinstance(c, str))
    aggregations = payload.get("aggregations")
    if isinstance(aggregations, dict):
        out.extend(c for c in aggregations if isinstance(c, str))
    bucket = payload.get("bucket") or payload.get("dimensionBucket")
    if isinstance(bucket, dict) and isinstance(bucket.get("source"), str):
        out.append(bucket["source"])
    return out


def assert_aggregate_privacy(payload: Any, sensitive: set, what: str) -> None:
    """聚合族端点（aggregate / aggregate-multi / chart aggregate）的统一闸。

    sensitive 空 = 立即返回（连列收集都不做——无声明时代的代码路径逐位不变）。"""
    if not sensitive:
        return
    assert_not_sensitive(_aggregate_payload_cols(payload), sensitive, what)


def _family_of(report_id: str) -> str:
    """The report family a reportId belongs to.

    New copies are registered as ``{family}-{name}`` and carry an explicit
    ``family`` on their entry. This regex only handles LEGACY ``-vN`` copies
    created before the family field existed: ``PSI-v2`` → ``PSI``.
    """
    return re.sub(r"-v\d+$", "", report_id)


def base36(n: int) -> str:
    """Compact ASCII id suffix for draft reportIds (compose-<base36(ms)>)."""
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = digits[r] + out
    return out or "0"


def _next_draft_id(registry) -> str:
    """Allocate a fresh ``compose-<base36>`` draft reportId (collision-safe).

    reportId 契约 ^[A-Za-z][A-Za-z0-9_-]{1,79}$：compose-<base36 时间戳>，
    撞名（理论不可能）就递增尾号。h_create_draft 与 h_create_draft_blank 共用。"""
    stamp = int(time.time() * 1000)
    for n in range(0, 1000):
        cand = f"compose-{base36(stamp + n)}"
        if registry.get(cand) is None:
            return cand
    raise BadRequest("too many drafts")


def _change_entry(mode: str, report_id: str, title: str, manifest: dict, source: str | None = None) -> dict:
    """One changelog record (version history) for a save/build event."""
    item: dict[str, Any] = {
        "at": time.time(),
        "mode": mode,
        "reportId": report_id,
        "title": title,
        "chartCount": len((manifest or {}).get("charts") or []),
    }
    if source:
        item["from"] = source
    return item


# ---------------------- router --------------------------------------------------

class Router:
    """Path-pattern router. Patterns use ':name' for parameters."""

    def __init__(self):
        self._routes: list[tuple[str, str, Callable[..., None]]] = []   # (method, pattern, fn)
        self._compiled: list[tuple[str, str, re.Pattern, Callable[..., None]]] = []

    def add(self, method: str, pattern: str, handler: Callable[..., None]) -> None:
        rx = re.compile("^" + re.sub(r":([A-Za-z_][A-Za-z0-9_]*)", r"(?P<\1>[^/]+)", pattern) + "/?$")
        self._routes.append((method, pattern, handler))
        self._compiled.append((method, pattern, rx, handler))

    def resolve(self, method: str, path: str) -> tuple[Callable[..., None], dict[str, str]] | None:
        # strip query string
        path = path.split("?", 1)[0]
        for m, _pat, rx, fn in self._compiled:
            if m != method:
                continue
            mt = rx.match(path)
            if mt:
                return fn, mt.groupdict()
        return None


# ---------------------- handlers ------------------------------------------------

# ---------- 在线编辑策略（runtime.editPassword / 管理页开关） ------------------
# 每份报告有一条编辑策略 {enabled, password}：真源是注册表条目的 ``edit`` 字段
# （报告管理页设置），回退到编译期写进 manifest 的 ``runtime.editPassword``。
# 构建时默认就会生成口令，所以「初版默认即可保存」；管理员可以关闭在线编辑，此后
# 任何保存请求都被拒，客户端提示「当前暂无法编辑保存报告，请联系管理员」。
EDIT_DISABLED_MESSAGE = "当前暂无法编辑保存报告，请联系管理员"


def edit_policy_of(entry: dict | None) -> dict:
    """Resolve a report entry's online-edit policy (never raises)."""
    entry = entry or {}
    policy = entry.get("edit")
    if isinstance(policy, dict) and ("enabled" in policy or "password" in policy):
        pw = str(policy.get("password") or "")
        return {"enabled": bool(policy.get("enabled")), "password": pw}
    runtime = (entry.get("manifest") or {}).get("runtime") or {}
    pw = str(runtime.get("editPassword") or "")
    enabled = runtime.get("editEnabled")
    if enabled is None:
        enabled = bool(pw)
    return {"enabled": bool(enabled), "password": pw}


def check_edit_gate(body: dict, policy: dict) -> str | None:
    """Return an error message when an online write is not allowed, else None.

    Dataset/mutation endpoints (add / replace / save) all go through the same
    policy so a report whose editing the manager disabled cannot be modified
    through ANY endpoint — gating only ``/manifest`` left add/replace open.
    """
    if not policy.get("enabled"):
        return EDIT_DISABLED_MESSAGE + "（管理员已关闭本报告的在线编辑）"
    expected = str(policy.get("password") or "")
    if not expected:
        return EDIT_DISABLED_MESSAGE + "（本报告尚未设置编辑口令）"
    import hmac
    if not hmac.compare_digest(str(body.get("password") or "").encode("utf-8"), expected.encode("utf-8")):  # encode：compare_digest 对 str 只收 ASCII，中文口令会 TypeError→500
        return "编辑口令错误"
    return None


def _apply_policy_to_manifest(manifest: dict | None, policy: dict) -> None:
    """Mirror the policy INTO the served manifest (runtime.editPassword /
    runtime.editEnabled) so (a) a saved copy inherits the same policy and
    (b) the report page can prefill the password + know whether saving is
    allowed at all (disabled → 「请联系管理员」)."""
    if not isinstance(manifest, dict):
        return
    runtime = manifest.setdefault("runtime", {})
    runtime["editEnabled"] = bool(policy.get("enabled"))
    if policy.get("password"):
        runtime["editPassword"] = policy["password"]


def _generate_password(length: int = 8) -> str:
    """Human-friendly random password (no ambiguous 0/O/1/l/I characters)."""
    import secrets
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _cleanup_queued_replace_input(registry: Registry, job: dict[str, Any]) -> bool:
    """Delete a queued replace input after cancellation, without path escape."""
    payload = job.get("payload") if isinstance(job, dict) else None
    name = payload.get("inputName") if isinstance(payload, dict) else None
    if not isinstance(name, str) or not re.fullmatch(
            r"replace-[A-Fa-f0-9]{24}(?:\.[A-Za-z0-9]{1,9})?", name):
        return False
    queue_dir = (Path(registry.path.parent) / "queued-replace").resolve()
    path = (queue_dir / name).resolve()
    if path.parent != queue_dir:
        return False
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def _cleanup_evidence_export(registry: Registry, job: dict[str, Any]) -> bool:
    """Remove an export artifact when cancellation wins terminal publication."""
    job_id = job.get("id") if isinstance(job, dict) else None
    if not isinstance(job_id, str) or not re.fullmatch(r"job-[A-Fa-f0-9]{12}", job_id):
        return False
    export_dir = (Path(registry.path.parent) / "copilot-exports").resolve()
    removed = False
    for extension in ("json", "csv", "md"):
        path = export_dir / f"export-{job_id}.{extension}"
        try:
            path.unlink(missing_ok=True)
            removed = removed or not path.exists()
        except OSError:
            pass
    return removed


def _prune_evidence_exports(registry: Registry, *, now: float | None = None) -> int:
    """Reclaim 600-second export artifacts even when nobody downloads them."""
    export_dir = (Path(registry.path.parent) / "copilot-exports").resolve()
    if not export_dir.is_dir():
        return 0
    current = time.time() if now is None else float(now)
    removed = 0
    try:
        entries = list(export_dir.iterdir())[:512]
    except OSError:
        return 0
    for path in entries:
        if not path.is_file() or not re.fullmatch(
                r"export-job-[A-Fa-f0-9]{12}\.(?:json|csv|md)", path.name):
            continue
        try:
            if current - path.stat().st_mtime > 600:
                path.unlink(missing_ok=True)
                removed += 1
        except OSError:
            continue
    return removed


def dispatch_queued_job(engine: Engine, registry: Registry,
                        job: dict[str, Any], progress: Callable[[str], None]) -> dict[str, Any]:
    """Dispatch durable queue payloads that are safe to reconstruct elsewhere.

    Monitor/Agent jobs carry only bounded typed payloads; the worker reloads
    the current report manifest instead of serializing rows or Python
    closures.  Write/notify kinds fail closed so an external worker cannot
    accidentally execute a future side-effecting job.
    """
    payload = job.get("payload") or {}
    report_id = str(job.get("reportId") or "")
    entry = registry.get(report_id)
    if entry is None:
        raise ValueError("report for queued Agent job is not registered")
    job_id = str(job.get("id") or "")
    cancel_check = lambda: _jobs.is_cancel_requested(job_id)

    if job.get("kind") == "agent_plan":
        plan = payload.get("plan") if isinstance(payload, dict) else None
        if not isinstance(plan, dict):
            raise ValueError("queued Agent plan payload is invalid")
        from .agent_executor import execute_plan
        from .agent_tools import default_registry
        progress("校验分析计划")
        return execute_plan(plan, default_registry(), engine=engine, entry=entry,
                            role="viewer", cancel_check=cancel_check, progress=progress,
                            max_seconds=float((plan.get("budget") or {}).get("maxSeconds", 120)))

    if job.get("kind") == "copilot":
        if not isinstance(payload, dict) or not isinstance(payload.get("message"), str):
            raise ValueError("queued Copilot payload is invalid")
        from .copilot import load_config, model_complete, run_turn
        progress("模型处理中")
        return run_turn(
            entry, engine, load_config(registry), payload["message"],
            str(payload.get("chartId") or ""), payload.get("history") or [],
            complete=lambda cfg, msgs: model_complete(
                cfg, msgs, cancel_check=cancel_check,
                health_path=registry.path.parent / "provider-health.sqlite3",
                governance_scope=str(payload.get("governanceScope") or report_id)),
            cancel_check=cancel_check, timeout_seconds=120,
            context_key=payload.get("contextKey"),
            filter_scopes=payload.get("filterScopes"))

    if job.get("kind") == "evidence_export":
        source_id = str(payload.get("sourceId") or "") if isinstance(payload, dict) else ""
        fmt = str(payload.get("format") or "json").lower() if isinstance(payload, dict) else "json"
        source = _jobs.get(source_id)
        if source is None or source.get("reportId") != report_id or source.get("status") != "done":
            raise ValueError("source evidence job is not complete")
        progress("整理证据")
        evidence = (source.get("result") or {}).get("steps") if isinstance(source.get("result"), dict) else source.get("result")
        raw, content_type, extension = _serialize_evidence_export(
            evidence, fmt, cancel_check=cancel_check)
        if len(raw) > 20 * 1024 * 1024:
            raise ValueError("证据导出超过 20MB 上限，请缩小查询范围")
        export_dir = registry.path.parent / "copilot-exports"
        export_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = export_dir / f"export-{job_id}.{extension}"
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            with tmp.open("wb") as stream:
                for offset in range(0, len(raw), 64 * 1024):
                    if cancel_check():
                        raise _jobs.JobCancelled("证据导出已取消")
                    stream.write(raw[offset:offset + 64 * 1024])
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
            if cancel_check():
                # Cancellation may arrive after the last chunk but before the
                # worker publishes its terminal state. Do not leave a file
                # whose source Job will be reported as cancelled.
                path.unlink(missing_ok=True)
                raise _jobs.JobCancelled("证据导出已取消")
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        return {"exportId": job_id, "format": fmt, "bytes": len(raw),
                "contentType": content_type}

    if job.get("kind") == "replace":
        if not isinstance(payload, dict):
            raise ValueError("queued replace payload is invalid")
        input_name = str(payload.get("inputName") or "")
        if not re.fullmatch(r"replace-[A-Fa-f0-9]{24}(?:\.[A-Za-z0-9]{1,9})?", input_name):
            raise ValueError("queued replace input name is invalid")
        mode = str(payload.get("mode") or "replace")
        if mode not in {"replace", "upsert", "append"}:
            raise ValueError("queued replace mode is invalid")
        key_cols = payload.get("keyCols")
        if key_cols is not None and (not isinstance(key_cols, list) or
                                     any(not isinstance(item, str) for item in key_cols)):
            raise ValueError("queued replace keyCols is invalid")
        input_dir = Path(registry.path.parent) / "queued-replace"
        path = (input_dir / input_name).resolve()
        if path.parent != input_dir.resolve() or not path.exists():
            raise ValueError("queued replace input is missing")
        try:
            suffix = path.suffix.lower()
            if suffix == ".json":
                with path.open("r", encoding="utf-8") as stream:
                    table = json.load(stream)
            elif suffix == ".sqlite":
                # XP-01：队列持久化输入随写盘格式走（write_dataset_snapshot 缺省
                # sqlite）——按后缀分派读回，旧 .json 输入照读。
                from .store import read_snapshot as _read_snapshot
                _cols, _rows_iter = _read_snapshot(path)
                _rows_iter = list(_rows_iter)
                table = {"columns": _cols, "rows": _rows_iter}
            else:
                progress(f"解析文件（{suffix.lstrip('.') or 'upload'}）中…")
                from table_input import load_table_file as _load_table_file
                loaded = _load_table_file(str(path))
                table = {"columns": [str(c) for c in loaded.get("columns") or []],
                         "rows": [list(r) for r in loaded.get("rows") or []]}
            if not isinstance(table, dict):
                raise ValueError("queued replace table is invalid")
            progress("执行数据替换")
            return _process_replace_table_impl(engine, registry, report_id, str(job.get("datasetId") or ""),
                                               table, mode, key_cols, progress)
        finally:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    if job.get("kind") != "monitor":
        raise ValueError(f"worker dispatcher does not support job kind {job.get('kind')!r}")
    monitor_id = str(payload.get("monitorId") or job.get("datasetId", "").removeprefix("monitor:"))
    monitor = next((item for item in _jobs.list_monitors(report_id)
                    if item.get("id") == monitor_id), None)
    if monitor is None:
        raise ValueError("monitor definition no longer exists")
    from .agent_executor import envelope
    from .agent_tool_runner import execute_read_tool
    progress("执行监控查询")
    value = execute_read_tool(monitor["tool"], monitor["args"], engine=engine, entry=entry,
                              cancel_check=cancel_check,
                              timeout_seconds=60)
    evidence_hash = _monitor_evidence_hash(value)
    previous_hash = None
    previous_job_id = str(payload.get("previousJobId") or "")
    if previous_job_id:
        previous_job = _jobs.get(previous_job_id)
        previous_result = previous_job.get("result") if isinstance(previous_job, Mapping) else None
        if isinstance(previous_result, Mapping) and isinstance(previous_result.get("evidenceHash"), str):
            previous_hash = previous_result["evidenceHash"]
    return {"monitorId": monitor["id"], "name": monitor["name"],
            "tool": monitor["tool"], "evidence": value,
            "evidenceHash": evidence_hash,
            "previousEvidenceHash": previous_hash,
            "changed": previous_hash is None or previous_hash != evidence_hash,
            "provenance": envelope(monitor["tool"], monitor["args"], value)}


def _process_replace_table_impl(engine, registry, reportId, datasetId, table, mode, key_cols, progress=None):
    """h_dataset_replace 的重管道（唯一实现：同步请求线程 / 异步 job 线程共用）。

    形状/列集闸 → 合并（upsert）/替换/追加（append）→ 计算列与维度值分组
    重算 → 装载 → 快照落盘 → manifest/注册表记账。fail-closed：任何闸失败
    抛 BadRequest/NotFound，数据零改动。返回响应 payload（不写 HTTP 响应）。
    """
    def _tick(label):
        if progress:
            try:
                progress(label)
            except Exception:  # noqa: BLE001 — 阶段上报绝不拖垮主流程
                pass

    entry = registry.get(reportId)
    if entry is None:
        raise NotFound(f"report {reportId!r} not registered")
    cols = table.get("columns") or []
    rows = table.get("rows") or []
    if not isinstance(cols, list) or not cols or len(cols) > 200:
        raise BadRequest("table columns must contain 1-200 fields")
    # 重复表头闸（与 CSV 直传同口径）：缺列/多列闸是集合比较，[A,A,B] 对
    # [A,B] 放行后 index(c) 按首现投影 → 第二个 A 整列静默丢弃。
    _reject_duplicate_columns(cols, "表格")
    if not isinstance(rows, list) or not rows:
        raise BadRequest("table rows must contain at least one record")
    if len(rows) > 5_000_000:
        raise BadRequest("table rows must contain at most 5,000,000 records")
    if any(not isinstance(r, list) or len(r) != len(cols) for r in rows):
        raise BadRequest("table rows must be rectangular")
    datasets_meta = list(entry.get("datasets") or [])
    meta = next((d for d in datasets_meta if d.get("id") == datasetId), None)
    if meta is None:
        raise NotFound(f"dataset {datasetId!r} not in report {reportId!r}")
    current: list[str] = []
    info_existing = engine.table_info(datasetId)
    if info_existing is not None:
        current = [str(c) for c in (info_existing.columns or [])]
    if not current:
        manifest_ds = next((d for d in ((entry.get("manifest") or {}).get("datasets") or [])
                            if d.get("id") == datasetId), None)
        current = [c.get("name") if isinstance(c, dict) else str(c)
                   for c in ((manifest_ds or {}).get("columns") or [])]
    # 计算列（derived.columns）与维度值分组列都是**物化产物**，不是源字段：
    # 装载后的表里有它们，但用户上传的是原始文件（没有这些列），也可能上传
    # 导出过的物化表（有）。两种都必须接受 —— 比对只针对原始字段，物化列
    # 一律按声明重算（旧版只排除计算列，带分组列的表永远「缺列」被拒）。
    # 声明取**注册表并集**（与下方 dimGroups / h_register 同口径）：共享表的
    # 另一份副本可能已声明本条目 meta 里还没有的列，只读本条目会把引擎里
    # 真实存在的物化列误判成「上传缺列」而拒绝整次替换。
    decl_defs = table_defs(registry.raw_entries(), datasetId,
                           extra=columns_decl(meta.get("derived")))
    decl = {"kind": "columns", "defs": decl_defs} if decl_defs else None
    derived_names = {str(d.get("name")) for d in decl_defs}
    group_union = dim_group_table_defs(registry.raw_entries(), datasetId,
                                       extra=dim_groups_defs(meta.get("dimGroups")))
    derived_names.update(str(g.get("name")) for g in group_union)
    # 退役物化列（曾声明过、声明已被删）：从**全部**注册表条目取并集——共享表
    # 的孤儿列对每一份副本都是孤儿。它们不参与「原始列」比对。
    retired_names = {str(c) for e2 in registry.raw_entries() if isinstance(e2, dict)
                     for d2 in (e2.get("datasets") or []) if isinstance(d2, dict)
                     and d2.get("id") == datasetId
                     for c in (d2.get("retiredColumns") or [])}
    current_raw = [c for c in current if c not in derived_names and c not in retired_names]
    new_names = [str(c) for c in cols]
    upload_raw = [c for c in new_names if c not in derived_names]
    missing = [c for c in current_raw if c not in upload_raw]
    extra = [c for c in upload_raw if c not in current_raw]
    if missing or extra:
        raise BadRequest(
            "表格结构不一致，无法原位替换（缺列：" + ",".join(missing[:5]) +
            "；多列：" + ",".join(extra[:5]) + "）。请使用列映射向导或新建数据源。")
    upsert_stats = None
    if mode == "upsert":
        _tick("合并增量行中…")
        import sys as _sys
        _scripts_dir = str(Path(__file__).resolve().parent.parent / "scripts")
        if _scripts_dir not in _sys.path:
            _sys.path.insert(0, _scripts_dir)
        from upsert_merge import UpsertError, upsert_merge  # noqa: PLC0415  (scripts/ on sys.path above)
        if info_existing is None:
            # 空基表闸（2026-09-26）：引擎表缺失（重启时快照损坏被 _reload_all
            # WARN 跳过）而注册表/manifest 声称有行——base_rows=[] 会把「增量
            # 合并」变成破坏性全量替换，历史行被上传行集静默顶掉。fail-closed
            # 400：先修复数据源（重新上传全量文件）或显式改用全量替换。声明
            # 行数为 0/缺失（真空表）才允许空基表合并。
            _mds = next((d for d in ((entry.get("manifest") or {}).get("datasets") or [])
                         if isinstance(d, dict) and d.get("id") == datasetId), None)
            claimed = 0
            for cand in (meta.get("rowCount"), (_mds or {}).get("rowCount")):
                try:
                    claimed = max(claimed, int(cand or 0))
                except (TypeError, ValueError):
                    continue
            if claimed > 0:
                raise BadRequest(
                    f"增量合并被拒绝：引擎内没有数据源 {datasetId!r} 的表（重启时快照可能"
                    f"损坏被跳过），而注册表声称有 {claimed:,} 行。此时合并会把这些行永久"
                    "替换成本次上传的增量——请先在数据源配置里重新上传全量文件修复，"
                    "或改用「全量替换」。")
            base_rows: list[list] = []
        else:
            from .aggregator import _quote
            select = ", ".join(_quote(c) for c in current_raw)
            base_rows = [[rec.get(c) for c in current_raw]
                         for rec in engine.execute(
                             f'SELECT {select} FROM {_quote(datasetId)}')]
        try:
            # 上传行先投影到 current_raw（与 replace 的 keep_idx 同一处理）：
            # 导出过的物化表带着计算列回灌时，多余列直接丢弃而不是 400——
            # 同一份文件「全量替换能成功、增量合并被拒」自相矛盾（真机复验）。
            up_idx = [new_names.index(c) for c in current_raw]
            proj_rows = [[r[i] if i < len(r) else None for i in up_idx] for r in rows]
            merged = upsert_merge(current_raw, base_rows, proj_rows, key_cols,
                                  delta_columns=list(current_raw))
        except UpsertError as exc:
            raise BadRequest(str(exc)) from exc
        upsert_stats = merged["stats"]
        raw_payload = {"columns": current_raw, "rows": merged["rows"]}
    elif mode == "append":
        # 追加新行（2026-09-22，W35/W36 真机复盘）：同构文件补新一期数据时，
        # 按周期键判断增量往往 100% 是新行——upsert 的主键选择是纯仪式，
        # 弹窗却把它设成硬闸。append = 不查重不覆盖，基表行 + 上传行直接
        # 并表；重复导入同一文件会产生重复行（弹窗文案已声明此代价）。
        _tick("并入追加行中…")
        keep_idx = [new_names.index(c) for c in current_raw]
        append_rows = [[r[i] for i in keep_idx] for r in rows]
        if info_existing is not None:
            from .aggregator import _quote
            select = ", ".join(_quote(c) for c in current_raw)
            base_rows = [[rec.get(c) for c in current_raw]
                         for rec in engine.execute(
                             f'SELECT {select} FROM {_quote(datasetId)}')]
        else:
            base_rows = []
        if len(base_rows) + len(append_rows) > 5_000_000:
            raise BadRequest(
                f"追加后总行数 {len(base_rows) + len(append_rows):,} 超过 5,000,000 行上限，"
                "请改用全量替换或拆分增量。")
        _tick(f"装载 {len(base_rows) + len(append_rows):,} 行中…")
        raw_payload = {"columns": current_raw, "rows": base_rows + append_rows}
    else:
        _tick("装载替换行中…")
        keep_idx = [new_names.index(c) for c in current_raw]
        raw_payload = {"columns": current_raw,
                       "rows": [[r[i] for i in keep_idx] for r in rows]}
    _tick("重算计算列/维度值分组…")
    table_payload = apply_dataset_derived(raw_payload,
                                          decl or {"kind": "columns", "defs": []})
    # 维度值分组列同为物化产物：按并集声明重算（来源与上方比对同一份）。
    table_payload = apply_dim_groups(table_payload, group_union)
    info = engine.load_table(datasetId, table_payload["columns"], table_payload["rows"],
                             index_columns=list(meta.get("indexColumns") or []))
    for d in datasets_meta:
        if d.get("id") == datasetId:
            d["rowCount"] = info.row_count
            if key_cols is not None:
                # 主键声明随本次合并持久化（下次刷新向导免重选；也是 D1
                # 模板化的种子）。replace/append 不改动已存声明。
                d["primaryKey"] = key_cols
            # 重建成功 = 退役物化列已随 DROP+CREATE 消失，记录清掉。
            d.pop("retiredColumns", None)
    # 重启存活：`_reload_all` 是从注册表里的 **source 快照** 重载表的，所以替换
    # 必须把新数据落到快照文件并改指 source —— 否则重启后数据悄悄退回旧版本
    # （同一类 bug：改了内存/引擎，却没改重载的真源）。
    data_dir = getattr(registry, "data_dir", None)
    if data_dir:
        from pathlib import Path as _Path
        # 流式写盘（write_dataset_snapshot 内含临时文件 + os.replace 原子落盘）：
        # 裸 write_text 半途失败会留半份快照，重启 _reload_all 灌进半个表。
        # 写盘失败显式 400——引擎内已是新数据，必须告诉作者「重启会回退」，
        # 不能静默吞掉（状态撕裂不可归因）。
        # XP-01：write_dataset_snapshot 按当前快照格式（缺省 sqlite）换后缀并
        # 返回实际路径——回填 snap_path，注册表 source 记的一定是真实文件
        # （否则指向一个不存在的 .json，重启后数据集蒸发）。
        snap_path = _Path(data_dir) / (_snapshot_stem(datasetId) + ".replaced.json")
        try:
            snap_path = write_dataset_snapshot(snap_path, table_payload["columns"],
                                               table_payload["rows"])
        except OSError as exc:
            raise BadRequest(
                f"替换数据的快照写盘失败：{exc}。引擎内已是新数据，但重启会回退到旧快照；"
                "请处理磁盘/权限问题后重做一次「替换数据」。") from exc
        try:
            rel = os.path.relpath(snap_path, registry.path.parent)
        except ValueError:
            rel = str(snap_path)
        for d in datasets_meta:
            if d.get("id") == datasetId:
                d["source"] = rel
    entry["datasets"] = datasets_meta
    manifest = entry.get("manifest") or {}
    for d in (manifest.get("datasets") or []):
        if d.get("id") == datasetId:
            d["rowCount"] = info.row_count
            # 列对象形状契约（server 模式 = [{name, type}]）：替换/增量不得把
            # 编译期写好的 type 洗成裸名字——否则运行时分类全失效（插入图表
            # alert「至少需要一个维度和一个指标」、字段面板丢指标区）。同名列
            # 保留既有 type；替换真带来了新列时按值推断（validate_table 单一
            # 真源）。_manifest_typed_columns 的 docstring 有完整失败链。
            d["columns"] = _manifest_typed_columns(
                [str(c) for c in table_payload["columns"]],
                table_payload.get("rows") or [],
                prev_cols=d.get("columns"))
            if key_cols is not None:
                d["primaryKey"] = key_cols
            # 列统计（字段副行）是**编译期从旧数据算出来的**：数据一换就过期。
            # 宁可让 UI 显示“—”，也不能继续展示旧数字（客户端同一路径也删）。
            d.pop("columnStats", None)
            # filterOptions（筛选/值域勾选的选项源）同性质：编译期从旧 rows
            # 取样的截断值域。不删的话新数据里的新类目值永远不出现在筛选
            # 下拉里（客户端会退回 /values 有界通道现拉）。
            d.pop("filterOptions", None)
    manifest["datasets"] = manifest.get("datasets") or []
    entry["manifest"] = manifest
    _touch_dataset_revision(entry, datasetId)
    log = list(entry.get("changelog") or [])
    log.append({"at": time.time(),
                "mode": "upsert-data" if mode == "upsert"
                else ("append-data" if mode == "append" else "replace-data"),
                "datasetId": datasetId, "rows": info.row_count,
                **({"inserted": upsert_stats["inserted"],
                    "updated": upsert_stats["updated"]} if upsert_stats else {}),
                **({"appended": len(rows)} if mode == "append" else {}),
                "title": entry.get("title") or reportId})
    entry["changelog"] = log
    # 收尾合并落表（不整条覆盖）：异步任务跑的几分钟里作者可能已在线保存
    # 过 manifest，整条 register 会把保存静默回滚（lost update）。
    _commit_replace_result(registry, reportId, entry, datasetId)
    payload = {"datasetId": datasetId, "rowCount": info.row_count,
               "columns": table_payload["columns"]}
    if upsert_stats:
        payload["upsert"] = upsert_stats
    if mode == "append":
        payload["appended"] = len(rows)
    return payload



def make_handlers(engine: Engine, registry: Registry) -> Router:
    """Build the route table. Handlers close over engine/registry."""
    # Job metadata lives beside the shared registry so another server process
    # or ``run_server --worker`` process can poll/cancel/claim it.  The
    # embedded worker keeps the default server self-contained; configure()
    # marks only confirmed-dead owners as lost.
    _jobs.configure(registry.path.parent)
    # F05 is intentionally server-local and approval-gated.  The advisor's
    # proposal files are metadata only; no index is created until its apply
    # route receives an explicit admin/editor approval object.
    index_advisor = IndexAdvisor(engine, registry.path.parent / "index-proposals")
    r = Router()

    def h_health(_handler):
        # 只数条目，不做 list() 的深拷贝（见 Registry.summary）：registry 里可能有
        # 带行的运行时数据集（数十 MB），深拷贝会让 health 超过 ping 超时 → 报告管理
        # 把活着的服务当成死的（索引被 prune、列表为空）。
        summary = registry.summary()
        respond_json(_handler, 200, {
            "status": "ok",
            "version": SKILL_VERSION,
            "engine": engine.engine_name,
            "enginePolicy": getattr(engine, "engine_policy", None),
            "datasetCount": summary["datasetCount"],
            "reportCount": summary["reportCount"],
            "jobs": _jobs.summary(),
            "aggregateCache": aggregate_cache_summary(),
            "registryPath": str(registry.path),
            "startedAt": _server_started_at(),
        })

    def h_index_suggestions(_handler):
        """Read-only physical-index advice from observed semantic queries."""
        if not _same_origin_guard(_handler):
            respond_json(_handler, 403, {"error": {"code": "Forbidden", "message": "跨站请求已拒绝"}})
            return
        query = parse_qs(urlparse(_handler.path).query)
        dataset_id = (query.get("datasetId") or [None])[0]
        respond_json(_handler, 200, index_suggestions(engine, dataset_id))

    def h_index_explain(_handler):
        """Read-only EXPLAIN probe; never creates or changes an index."""
        if not _same_origin_guard(_handler):
            respond_json(_handler, 403, {"error": {"code": "Forbidden", "message": "跨站请求已拒绝"}})
            return
        query = parse_qs(urlparse(_handler.path).query)
        dataset_id = (query.get("datasetId") or [""])[0]
        column = (query.get("column") or [""])[0]
        if not dataset_id or not column:
            raise BadRequest("datasetId and column are required")
        try:
            result = explain_index_candidate(engine, dataset_id, column)
        except ValueError as exc:
            raise BadRequest(str(exc)) from exc
        respond_json(_handler, 200, result)

    def _index_admin_origin(handler):
        """Gate index proposal writes to the local management surface."""
        if not is_loopback(handler) or not _same_origin_guard(handler):
            respond_json(handler, 403, {"error": {"code": "Forbidden",
                                                   "message": "索引审批仅限本机管理端"}})
            return False
        return True

    def _index_advisor_call(call):
        try:
            return call()
        except IndexProposalNotFound as exc:
            raise NotFound(str(exc)) from exc
        except IndexProposalConflict as exc:
            raise Conflict(str(exc)) from exc
        except IndexAdvisorError as exc:
            if getattr(exc, "status", 0) == 404:
                raise NotFound(str(exc)) from exc
            if getattr(exc, "status", 0) == 409:
                raise Conflict(str(exc)) from exc
            if getattr(exc, "status", 0) == 422:
                raise ValidationFailed(str(exc)) from exc
            raise BadRequest(str(exc)) from exc

    def _persist_index_column(dataset_id: str, column: str) -> None:
        """Persist an approved index declaration for every shared report.

        ``IndexAdvisor`` has already created and verified the SQLite index.  We
        only mirror the declaration here so the existing DROP+CREATE reload
        pipeline carries the approved index across a restart; this callback is
        never reached by suggestions, preview, or dry-run.
        """
        original: dict[str, dict[str, Any]] = {}
        try:
            for report_id, raw_entry in registry.items_raw():
                if not isinstance(raw_entry, dict):
                    continue
                if not any(isinstance(ds, dict) and ds.get("id") == dataset_id
                           for ds in (raw_entry.get("datasets") or [])):
                    continue
                entry = registry.get(report_id)
                if not isinstance(entry, dict):
                    continue
                changed = False
                original[report_id] = copy.deepcopy(entry)
                for ds in (entry.get("datasets") or []):
                    if isinstance(ds, dict) and ds.get("id") == dataset_id:
                        columns = list(ds.get("indexColumns") or [])
                        if column not in columns:
                            columns.append(column)
                            ds["indexColumns"] = columns
                            changed = True
                manifest = entry.get("manifest")
                if isinstance(manifest, dict):
                    for ds in (manifest.get("datasets") or []):
                        if isinstance(ds, dict) and ds.get("id") == dataset_id:
                            columns = list(ds.get("indexColumns") or [])
                            if column not in columns:
                                columns.append(column)
                                ds["indexColumns"] = columns
                                changed = True
                if changed:
                    registry.register(report_id, entry)
        except Exception:
            # DDL apply treats this callback as part of the same logical
            # transaction.  Restore any manifests already written so a
            # registry I/O failure cannot leave a declaration for a rolled-back
            # physical index.
            for report_id, entry in original.items():
                try:
                    registry.register(report_id, entry)
                except Exception:
                    pass
            raise

    def h_index_proposal_preview(_handler):
        """Build a proposal without writing the proposal store or database."""
        if not _index_admin_origin(_handler):
            return
        body = read_json(_handler)
        if not isinstance(body, dict):
            raise BadRequest("request body must be an object")
        result = _index_advisor_call(lambda: index_advisor.preview(
            str(body.get("datasetId") or ""), str(body.get("column") or ""),
            role=body.get("role"),
            min_observations=body.get("minObservations", 2)))
        respond_json(_handler, 200, {**result, "preview": True, "dryRun": True})

    def h_index_proposal_create(_handler):
        """Persist proposal metadata only; this route never executes DDL."""
        if not _index_admin_origin(_handler):
            return
        body = read_json(_handler)
        if not isinstance(body, dict):
            raise BadRequest("request body must be an object")
        result = _index_advisor_call(lambda: index_advisor.propose(
            str(body.get("datasetId") or ""), str(body.get("column") or ""),
            role=body.get("role"),
            min_observations=body.get("minObservations", 2)))
        respond_json(_handler, 200, result)

    def h_index_proposal_get(_handler, proposalId):
        if not _index_admin_origin(_handler):
            return
        result = _index_advisor_call(lambda: index_advisor.get(proposalId))
        respond_json(_handler, 200, result)

    def h_index_proposal_approve(_handler, proposalId):
        if not _index_admin_origin(_handler):
            return
        body = read_json(_handler)
        if not isinstance(body, dict):
            raise BadRequest("request body must be an object")
        approval = body.get("approval") if isinstance(body.get("approval"), dict) else body
        result = _index_advisor_call(lambda: index_advisor.approve(proposalId, approval))
        respond_json(_handler, 200, result)

    def h_index_proposal_apply(_handler, proposalId):
        """Apply only after a current-fence check and explicit approval."""
        if not _index_admin_origin(_handler):
            return
        body = read_json(_handler)
        if not isinstance(body, dict):
            raise BadRequest("request body must be an object")
        dry_run = body.get("dryRun") is True
        approval = body.get("approval") if isinstance(body.get("approval"), dict) else body
        result = _index_advisor_call(lambda: index_advisor.apply(
            proposalId, approval=approval, dry_run=dry_run,
            persist_index=_persist_index_column))
        respond_json(_handler, 200, result)

    # The model configuration is owner-local until report author identity is
    # separated from the legacy manifest edit password. Conversation turns are
    # same-origin and can be read-only for LAN viewers; applying/saving remains
    # behind the existing editor permission.
    def _copilot_origin(handler):
        if not _same_origin_guard(handler):
            respond_json(handler, 403, {"error": {"code": "Forbidden", "message": "跨站 Copilot 请求已拒绝"}})
            return False
        return True

    def _copilot_owner(handler):
        if not _copilot_origin(handler):
            return False
        if not is_loopback(handler):
            respond_json(handler, 403, {"error": {"code": "Forbidden", "message": "模型配置仅限报告服务器本机"}})
            return False
        return True

    def _audit(handler, report_id: str, action: str, target: str = "",
               detail: dict[str, Any] | None = None) -> None:
        """Persist an action with the request's bounded cross-job trace id."""
        actor = "subject:" + _agent_subject_hash(handler)
        _jobs.record_audit(report_id, actor, action, target, detail,
                           trace_id=getattr(handler, "trace_id", None))

    def _agent_auth(handler, report_id: str, minimum_role: str = "viewer") -> bool:
        """Validate an optional scoped Agent bearer token.

        The legacy same-origin surface remains compatible for existing local
        reports.  Once a caller presents ``Authorization: Bearer ar1...``,
        however, the token is authoritative: a viewer token cannot perform an
        editor action and an expired/foreign token is rejected.  This gives
        the server a real role seam without silently trusting spoofable role
        headers or breaking already-issued edit-password links.
        """
        authorization = str(handler.headers.get("Authorization") or "").strip()
        if not authorization:
            return True
        if not authorization.lower().startswith("bearer "):
            respond_json(handler, 401, {"error": {"code": "Unauthorized", "message": "Agent 身份令牌格式无效"}},
                         {"WWW-Authenticate": "Bearer"})
            return False
        token = authorization[7:].strip()
        from .copilot import CopilotError, verify_agent_token
        try:
            report_entry = registry.get(report_id)
            claims = verify_agent_token(registry.path.parent, token, report_id, minimum_role,
                                        tenant_id=_report_tenant_id(report_entry) or None)
            handler.agent_claims = claims
        except CopilotError as exc:
            respond_json(handler, 403, {"error": {"code": "Forbidden", "message": str(exc)}})
            return False
        return True

    def _agent_editor_auth(handler, report_id: str) -> bool:
        return _agent_auth(handler, report_id, "editor")

    def _agent_context_guard(entry: dict, report_id: str, body: dict,
                             plan: dict | None = None) -> str:
        """Reject stale card bindings before a plan/job can be persisted."""
        plan = plan if isinstance(plan, dict) else {}
        context = plan.get("context") if isinstance(plan.get("context"), dict) else {}
        chart_id = body.get("chartId") or context.get("chartId") or ""
        raw_key = body.get("contextKey") or context.get("contextKey")
        raw_revision = context.get("datasetRevision")
        from .copilot import CopilotError, normalize_filter_scopes, validate_context_key, validate_dataset_revision
        request_has_scopes = isinstance(body, dict) and "filterScopes" in body
        raw_request_scopes = body.get("filterScopes") if request_has_scopes else None
        raw_plan_scopes = context.get("filterScopes")
        canonical_scopes = None
        if raw_request_scopes is not None:
            try:
                canonical_scopes = normalize_filter_scopes(entry, str(chart_id), raw_request_scopes)
                if raw_plan_scopes is not None:
                    plan_scopes = normalize_filter_scopes(entry, str(chart_id), raw_plan_scopes)
                    if plan_scopes != canonical_scopes:
                        raise Conflict("当前筛选范围已变化，请重新生成分析计划")
            except Conflict:
                raise
            except CopilotError as exc:
                raise BadRequest(str(exc)) from exc
        elif raw_plan_scopes is not None:
            try:
                canonical_scopes = normalize_filter_scopes(entry, str(chart_id), raw_plan_scopes)
            except CopilotError as exc:
                raise BadRequest(str(exc)) from exc
        if canonical_scopes is not None:
            body["filterScopes"] = canonical_scopes
            if isinstance(context, dict):
                context["filterScopes"] = canonical_scopes
        if raw_revision is not None and chart_id:
            try:
                validate_dataset_revision(entry, str(chart_id), raw_revision)
            except CopilotError as exc:
                raise Conflict(str(exc)) from exc
        if not raw_key:
            return ""
        try:
            return validate_context_key(entry, str(chart_id), raw_key, canonical_scopes)
        except CopilotError as exc:
            raise Conflict(str(exc)) from exc

    def h_copilot_auth_token(_handler, reportId):
        """Issue a short-lived, report-scoped role token from the local host.

        Issuance requires the existing edit password and loopback origin.  The
        token itself is signed with a server-local secret and is never logged.
        This is the transitional identity seam until a deployment supplies its
        external SSO/identity provider.
        """
        if not _copilot_owner(_handler):
            return
        entry = registry.get(reportId)
        if entry is None:
            raise NotFound(f"report {reportId!r} not registered")
        body = read_json(_handler)
        gate = check_edit_gate(body, edit_policy_of(entry))
        if gate:
            respond_json(_handler, 403, {"error": {"code": "Forbidden", "message": gate}})
            return
        role = str(body.get("role") or "viewer")
        try:
            ttl = int(body.get("ttlSeconds") or 3600)
        except (TypeError, ValueError):
            raise BadRequest("ttlSeconds must be an integer") from None
        subject = str(body.get("subject") or "local-user")[:120]
        from .copilot import CopilotError, issue_agent_token
        try:
            result = issue_agent_token(registry.path.parent, reportId, role,
                                       ttl_seconds=ttl, subject=subject,
                                       tenant_id=_report_tenant_id(entry) or None)
        except CopilotError as exc:
            raise BadRequest(str(exc)) from exc
        _audit(_handler, reportId, "agent.auth.issue", subject,
               {"role": role, "ttlSeconds": ttl})
        respond_json(_handler, 200, result)

    def h_copilot_config_get(_handler):
        if not _copilot_origin(_handler):
            return
        from .copilot import public_config
        respond_json(_handler, 200, public_config(registry))

    def h_copilot_config_set(_handler):
        if not _copilot_owner(_handler):
            return
        from .copilot import CopilotError, save_config
        try:
            result = save_config(registry, read_json(_handler))
        except CopilotError as exc:
            raise BadRequest(str(exc)) from exc
        respond_json(_handler, 200, result)

    def h_copilot_provider_models(_handler):
        """Discover a provider-owned model list without exposing the API key."""
        if not _copilot_owner(_handler):
            return
        from .copilot import CopilotError, load_config, provider_model_catalog
        try:
            result = provider_model_catalog(load_config(registry))
        except CopilotError as exc:
            raise BadRequest(str(exc)) from exc
        respond_json(_handler, 200, result)

    def h_copilot_test(_handler):
        if not _copilot_owner(_handler):
            return
        from .copilot import CopilotError, load_config, model_complete
        try:
            # DeepSeek JSON mode requires the prompt to mention JSON.  The
            # connection test must exercise the same response contract as a
            # real Agent turn rather than failing on a provider-side mode
            # guard before authentication/model connectivity is checked.
            model_complete(load_config(registry), [{"role": "user", "content": 'Return JSON only: {"ok":true}'}],
                           health_path=registry.path.parent / "provider-health.sqlite3")
        except CopilotError as exc:
            raise BadRequest(str(exc)) from exc
        respond_json(_handler, 200, {"ok": True})

    def h_copilot_usage(_handler):
        if not _copilot_owner(_handler):
            return
        from .copilot import ProviderGovernance
        path = registry.path.parent / "provider-health.sqlite3"
        respond_json(_handler, 200, {"usage": ProviderGovernance(path).usage_summary()})

    def h_copilot_tools(_handler):
        """Expose the non-sensitive Agent tool catalog for UI/diagnostics."""
        if not _copilot_origin(_handler):
            return
        from .agent_tools import default_registry
        tools = []
        for spec in default_registry().list():
            tools.append({"name": spec.name, "inputSchema": spec.input_schema,
                          "outputSchema": spec.output_schema,
                          "permissions": sorted(spec.permissions),
                          "estimatedCost": spec.estimated_cost,
                          "timeoutSeconds": spec.timeout_seconds,
                          "cancellable": spec.cancellable,
                          "sideEffect": spec.side_effect,
                          "requiresApproval": spec.requires_approval,
                          "semanticReferences": list(spec.semantic_references),
                          "acceptanceTests": list(spec.acceptance_tests)})
        respond_json(_handler, 200, {"version": 1, "tools": tools})

    def h_copilot_plan_validate(_handler, reportId):
        """Validate a typed plan without executing a tool or side effect."""
        if not _copilot_origin(_handler):
            return
        if not _agent_auth(_handler, reportId):
            return
        entry = registry.get(reportId)
        if entry is None:
            raise NotFound(f"report {reportId!r} not registered")
        from .agent_planner import normalize_plan, plan_hash, preview_plan
        from .agent_tools import default_registry, ToolContractError
        body = read_json(_handler)
        raw_plan = body.get("plan") if isinstance(body, dict) else None
        raw_plan = raw_plan if isinstance(raw_plan, dict) else body
        _agent_context_guard(entry, reportId, body, raw_plan)
        try:
            normalized = normalize_plan(raw_plan, default_registry(), role="viewer",
                                        context={"reportId": reportId})
        except ToolContractError as exc:
            raise BadRequest(str(exc)) from exc
        respond_json(_handler, 200, {"valid": True, "plan": normalized,
                                     "planHash": plan_hash(normalized),
                                     "requiresApproval": any(step["requiresApproval"]
                                                              for step in normalized["steps"]),
                                     "preview": preview_plan(normalized, entry)})

    def h_copilot_plan_execute(_handler, reportId):
        """Schedule a server-owned, read-first plan execution.

        This endpoint deliberately has no provider call and no write path. A
        plan containing a proposal stops at ``needsApproval``; the executor
        only invokes typed read tools backed by the existing aggregator.
        """
        if not _copilot_origin(_handler):
            return
        if not _agent_auth(_handler, reportId):
            return
        entry = registry.get(reportId)
        if entry is None:
            raise NotFound(f"report {reportId!r} not registered")
        from .agent_planner import normalize_plan, plan_hash
        from .agent_tools import ToolContractError, default_registry
        body = read_json(_handler)
        raw_plan = body.get("plan") if isinstance(body, dict) else None
        if not isinstance(raw_plan, dict):
            raw_plan = body if isinstance(body, dict) else {}
        _agent_context_guard(entry, reportId, body, raw_plan)
        try:
            normalized = normalize_plan(raw_plan, default_registry(), role="viewer",
                                        context={"reportId": reportId})
        except ToolContractError as exc:
            raise BadRequest(str(exc)) from exc
        expected_hash = body.get("planHash") if isinstance(body, dict) else None
        if expected_hash == "":
            expected_hash = None
        actual_hash = plan_hash(normalized)
        if expected_hash is not None and expected_hash != actual_hash:
            raise BadRequest("plan 已变化，请重新确认后执行")
        request_id = body.get("requestId") if isinstance(body, dict) else None
        if request_id is not None and (not isinstance(request_id, str)
                                       or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", request_id)):
            raise BadRequest("requestId 格式无效")
        allowed, retry_after = _copilot_rate_limit(_handler, reportId)
        if not allowed:
            respond_json(_handler, 429, {"error": {"code": "TooManyRequests",
                "message": "Copilot 请求较频繁，请稍后重试。"}},
                         {"Retry-After": str(retry_after)})
            return
        chart_key = str((normalized.get("context") or {}).get("chartId") or "__report__")[:120]
        request_hash = actual_hash
        if request_id:
            try:
                existing = _jobs.find_by_request(reportId, request_id, request_hash=request_hash)
            except _jobs.JobRequestConflict as exc:
                respond_json(_handler, 409, {"error": {"code": "RequestConflict",
                    "message": "requestId 已对应另一份分析计划，请生成新的 requestId。",
                    "jobId": str(exc)}})
                return
            if existing is not None:
                respond_json(_handler, 202, {"jobId": existing["id"],
                    "status": existing["status"], "reused": True})
                return
        try:
            job_id = _jobs.create("agent_plan", reportId, "plan:" + chart_key,
                                  max_running=2, request_key=request_id,
                                  request_hash=request_hash,
                                  priority=60, payload={"plan": normalized,
                                                       "recoveryPolicy": "safe-read"}, queued=True,
                                  trace_id=getattr(_handler, "trace_id", None))
        except _jobs.JobReplay as exc:
            existing = _jobs.get(str(exc))
            respond_json(_handler, 202, {"jobId": str(exc),
                "status": (existing or {}).get("status", "running"), "reused": True})
            return
        except _jobs.JobRequestConflict as exc:
            respond_json(_handler, 409, {"error": {"code": "RequestConflict",
                "message": "requestId 已对应另一份分析计划，请生成新的 requestId。",
                "jobId": str(exc)}})
            return
        except _jobs.JobConflict as exc:
            respond_json(_handler, 409, {"error": {"code": "Conflict",
                "message": "当前报告已有一项 Agent 分析任务，请等待完成或停止等待后再试。",
                "jobId": str(exc)}})
            return
        except _jobs.JobCapacity:
            respond_json(_handler, 429, {"error": {"code": "TooManyRequests",
                "message": "Agent 后台任务较多，请稍后重试。"}},
                         {"Retry-After": "3"})
            return

        # Queue payload is typed plan metadata only; worker reloads the report
        # manifest and engine, so no rows or Python closure cross the process.
        respond_json(_handler, 202, {"jobId": job_id, "status": "queued",
                                     "planHash": actual_hash, "retryToken": _jobs.retry_token(job_id)})

    def h_copilot_proposal_validate(_handler, reportId):
        """Return a complete before/after diff without mutating the report."""
        if not _copilot_origin(_handler):
            return
        if not _agent_auth(_handler, reportId):
            return
        entry = registry.get(reportId)
        if entry is None:
            raise NotFound(f"report {reportId!r} not registered")
        from .agent_proposals import (validate_alias_transaction_scope, validate_derived_transaction_scope,
                                       validate_proposal, validate_transaction_scope)
        from .agent_tools import ToolContractError
        body = read_json(_handler)
        try:
            proposal = body.get("proposal") or body
            result = validate_proposal(entry, proposal)
            if proposal.get("kind") == "alias_confirmation":
                result["transaction"] = validate_alias_transaction_scope(
                    entry, [proposal], engine=engine)
            elif proposal.get("kind") == "derived_measure":
                result["transaction"] = validate_derived_transaction_scope(
                    entry, [proposal], engine=engine)
            else:
                result["transaction"] = validate_transaction_scope(entry, [proposal], engine=engine)
        except ToolContractError as exc:
            raise BadRequest(str(exc)) from exc
        respond_json(_handler, 200, result)

    def h_copilot_proposal_history(_handler, reportId):
        """Return bounded proposal metadata only; manifests stay server-side."""
        if not _copilot_origin(_handler):
            return
        if not _agent_editor_auth(_handler, reportId):
            return
        if registry.get(reportId) is None:
            raise NotFound(f"report {reportId!r} not registered")
        from .agent_proposals import list_snapshot_metadata, manifest_hash
        current = registry.get(reportId) or {}
        current_hash = manifest_hash(current.get("manifest") or {})
        respond_json(_handler, 200, {
            "reportId": reportId,
            "currentHash": current_hash,
            "snapshots": list_snapshot_metadata(registry.path.parent, reportId,
                                                 current_hash=current_hash),
        })

    def h_copilot_proposal_apply(_handler, reportId):
        """Atomically apply one or more validated chart proposals with a snapshot."""
        if not _same_origin_guard(_handler):
            respond_json(_handler, 403, {"error": {"code": "Forbidden", "message": "跨站请求已拒绝"}})
            return
        if not _agent_editor_auth(_handler, reportId):
            return
        entry = registry.get(reportId)
        if entry is None:
            raise NotFound(f"report {reportId!r} not registered")
        body = read_json(_handler)
        _audit(_handler, reportId, "proposal.apply",
               str((body.get("proposal") or {}).get("chartId")
                    or (body.get("proposal") or {}).get("datasetId") or ""),
               {"count": len(body.get("proposals") or [body.get("proposal")]),
                "kind": (body.get("proposal") or {}).get("kind") or "chart"})
        gate = check_edit_gate(body, edit_policy_of(entry))
        if gate:
            respond_json(_handler, 403, {"error": {"code": "Forbidden", "message": gate}})
            return
        raw = body.get("proposals") if isinstance(body.get("proposals"), list) else [body.get("proposal") or body]
        if not raw or len(raw) > 8 or any(not isinstance(item, dict) for item in raw):
            raise BadRequest("proposals must contain 1-8 objects")
        from .agent_proposals import (apply_proposals, manifest_hash, validate_alias_transaction_scope,
                                       validate_derived_transaction_scope, validate_proposal,
                                       validate_transaction_scope)
        checks = []
        normalized_raw = []
        for proposal in raw:
            try:
                check = validate_proposal(entry, proposal)
            except Exception as exc:
                raise BadRequest(f"proposal 未通过服务端校验：{exc}") from exc
            expected = proposal.get("proposalId")
            if expected and expected != check["proposalId"]:
                raise Conflict("proposal 已过期，请重新预览")
            checks.append(check)
            normalized = dict(proposal)
            if proposal.get("kind") == "alias_confirmation":
                # The field-resolution tool intentionally returns only the
                # chart/alias/canonical proposal.  Fill the dataset identity
                # from the server-owned validation result before the atomic
                # apply path persists it.
                normalized["datasetId"] = check.get("after", {}).get("datasetId")
                normalized["alias"] = proposal.get("alias", proposal.get("name"))
            elif proposal.get("kind") == "derived_measure":
                # Apply must persist exactly the node the server normalized —
                # never a re-shape of the raw model payload.
                after = check.get("after") or {}
                normalized["datasetId"] = after.get("datasetId")
                if isinstance(after.get("addedMeasure"), dict):
                    normalized["measure"] = after["addedMeasure"]
            after_aggregations = check.get("after", {}).get("aggregations", {})
            if isinstance(after_aggregations, dict):
                measure_name = str(proposal.get("measure") or "")
                if measure_name in after_aggregations:
                    normalized["aggregation"] = after_aggregations[measure_name]
            normalized_raw.append(normalized)
        raw = normalized_raw
        alias_only = all(item.get("kind") == "alias_confirmation" for item in raw)
        derived_only = all(item.get("kind") == "derived_measure" for item in raw)
        chart_only = all(item.get("kind") not in ("alias_confirmation", "derived_measure")
                          for item in raw)
        if not alias_only and not derived_only and not chart_only:
            raise BadRequest("alias/派生指标 proposal 不能与图表 proposal 混合提交")
        try:
            if alias_only:
                transaction_scope = validate_alias_transaction_scope(entry, raw, engine=engine)
            elif derived_only:
                transaction_scope = validate_derived_transaction_scope(entry, raw, engine=engine)
            else:
                transaction_scope = validate_transaction_scope(entry, raw, engine=engine)
        except Exception as exc:
            raise BadRequest(f"proposal transaction scope invalid: {exc}") from exc
        before_entry = copy.deepcopy(entry)
        before_manifest = before_entry.get("manifest") or {}
        after_manifest = apply_proposals(before_manifest, raw)
        after_manifest["reportId"] = reportId
        _apply_policy_to_manifest(after_manifest, edit_policy_of(entry))
        after_hash = manifest_hash(after_manifest)
        before_hash = manifest_hash(before_manifest)
        import secrets
        snapshot_id = "proposal-" + secrets.token_hex(8)
        snapshot_dir = registry.path.parent / "copilot-snapshots"
        snapshot_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        snapshot_path = snapshot_dir / f"{snapshot_id}.json"
        snapshot = {"snapshotId": snapshot_id, "reportId": reportId, "createdAt": time.time(),
                    "beforeHash": before_hash, "afterHash": after_hash, "beforeEntry": before_entry}
        tmp = snapshot_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(snapshot, ensure_ascii=False, default=str), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, snapshot_path)
        new_entry = copy.deepcopy(entry)
        new_entry["manifest"] = after_manifest
        new_entry["title"] = after_manifest.get("title") or entry.get("title")
        log = list(entry.get("changelog") or [])
        log.append({**_change_entry("agent-proposal", reportId, new_entry["title"], after_manifest),
                    "snapshotId": snapshot_id, "proposalIds": [item["proposalId"] for item in checks]})
        new_entry["changelog"] = log
        new_entry["edit"] = edit_policy_of({"manifest": after_manifest})
        # Keep the after-state beside the before-state so a later explicit
        # redo can be fenced by the same manifest hashes. Older snapshots that
        # lack this field remain rollback-only and fail closed for redo.
        snapshot["afterEntry"] = copy.deepcopy(new_entry)
        tmp = snapshot_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(snapshot, ensure_ascii=False, default=str), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, snapshot_path)
        registry.register(reportId, new_entry)
        replay_columns(engine, registry)
        respond_json(_handler, 200, {"ok": True, "reportId": reportId, "snapshotId": snapshot_id,
                                     "proposalIds": [item["proposalId"] for item in checks],
                                     "transaction": transaction_scope,
                                     "manifest": _server_strip(after_manifest)})

    def h_copilot_proposal_rollback(_handler, reportId):
        """Rollback a proposal snapshot only if no later manifest edit won the race."""
        if not _same_origin_guard(_handler):
            respond_json(_handler, 403, {"error": {"code": "Forbidden", "message": "跨站请求已拒绝"}})
            return
        if not _agent_editor_auth(_handler, reportId):
            return
        entry = registry.get(reportId)
        if entry is None:
            raise NotFound(f"report {reportId!r} not registered")
        body = read_json(_handler)
        _audit(_handler, reportId, "proposal.rollback", str(body.get("snapshotId") or ""))
        gate = check_edit_gate(body, edit_policy_of(entry))
        if gate:
            respond_json(_handler, 403, {"error": {"code": "Forbidden", "message": gate}})
            return
        snapshot_id = str(body.get("snapshotId") or "")
        if not re.fullmatch(r"proposal-[A-Fa-f0-9]{16}", snapshot_id):
            raise BadRequest("snapshotId 格式无效")
        path = registry.path.parent / "copilot-snapshots" / f"{snapshot_id}.json"
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise NotFound("proposal snapshot 不存在或已过期") from exc
        if snapshot.get("reportId") != reportId or not isinstance(snapshot.get("beforeEntry"), dict):
            raise NotFound("proposal snapshot 不属于当前报告")
        from .agent_proposals import manifest_hash
        if manifest_hash(entry.get("manifest") or {}) != snapshot.get("afterHash"):
            raise Conflict("报告已发生后续编辑，拒绝覆盖新版本；请先读取当前报告再决定是否回滚")
        restored = snapshot["beforeEntry"]
        registry.register(reportId, restored)
        replay_columns(engine, registry)
        respond_json(_handler, 200, {"ok": True, "reportId": reportId, "snapshotId": snapshot_id,
                                     "redoSnapshot": snapshot_id if isinstance(snapshot.get("afterEntry"), dict) else None,
                                     "manifest": _server_strip(restored.get("manifest") or {})})

    def h_copilot_proposal_redo(_handler, reportId):
        """Redo a rolled-back proposal only when the report is still at beforeHash."""
        if not _same_origin_guard(_handler):
            respond_json(_handler, 403, {"error": {"code": "Forbidden", "message": "跨站请求已拒绝"}})
            return
        if not _agent_editor_auth(_handler, reportId):
            return
        entry = registry.get(reportId)
        if entry is None:
            raise NotFound(f"report {reportId!r} not registered")
        body = read_json(_handler)
        _audit(_handler, reportId, "proposal.redo", str(body.get("snapshotId") or ""))
        gate = check_edit_gate(body, edit_policy_of(entry))
        if gate:
            respond_json(_handler, 403, {"error": {"code": "Forbidden", "message": gate}})
            return
        snapshot_id = str(body.get("snapshotId") or "")
        if not re.fullmatch(r"proposal-[A-Fa-f0-9]{16}", snapshot_id):
            raise BadRequest("snapshotId 格式无效")
        path = registry.path.parent / "copilot-snapshots" / f"{snapshot_id}.json"
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise NotFound("proposal snapshot 不存在或已过期") from exc
        after_entry = snapshot.get("afterEntry")
        if snapshot.get("reportId") != reportId or not isinstance(after_entry, dict):
            raise NotFound("该旧 snapshot 不支持 redo")
        from .agent_proposals import manifest_hash
        if manifest_hash(entry.get("manifest") or {}) != snapshot.get("beforeHash"):
            raise Conflict("报告已发生后续编辑，拒绝覆盖新版本；请先读取当前报告再决定是否重做")
        before_entry = copy.deepcopy(entry)
        after_hash = manifest_hash(after_entry.get("manifest") or {})
        redo_id = "proposal-" + __import__("secrets").token_hex(8)
        redo_path = registry.path.parent / "copilot-snapshots" / f"{redo_id}.json"
        redo_snapshot = {"snapshotId": redo_id, "reportId": reportId, "createdAt": time.time(),
                         "beforeHash": snapshot.get("beforeHash"), "afterHash": after_hash,
                         "beforeEntry": before_entry, "afterEntry": copy.deepcopy(after_entry)}
        tmp = redo_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(redo_snapshot, ensure_ascii=False, default=str), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, redo_path)
        registry.register(reportId, after_entry)
        replay_columns(engine, registry)
        respond_json(_handler, 200, {"ok": True, "reportId": reportId,
                                     "snapshotId": redo_id,
                                     "manifest": _server_strip(after_entry.get("manifest") or {})})

    def h_copilot_turn(_handler, reportId):
        if not _copilot_origin(_handler):
            return
        if not _agent_auth(_handler, reportId):
            return
        entry = registry.get(reportId)
        if entry is None:
            raise NotFound(f"report {reportId!r} not registered")
        body = read_json(_handler)
        _audit(_handler, reportId, "copilot.turn", str(body.get("chartId") or ""),
               {"async": bool(body.get("async"))})
        from .copilot import CopilotError, load_config, model_complete, run_turn
        _agent_context_guard(entry, reportId, body)
        allowed, retry_after = _copilot_rate_limit(_handler, reportId)
        if not allowed:
            respond_json(_handler, 429, {"error": {"code": "TooManyRequests",
                "message": "Copilot 请求较频繁，请稍后重试。"}},
                         {"Retry-After": str(retry_after)})
            return

        # Deep/remote model calls can outlive a reverse proxy's request
        # timeout.  Reuse the existing in-memory job contract used by large
        # dataset replacement: the HTTP request only schedules work, while
        # the browser polls a bounded status/result record.  The query itself
        # remains the same typed Copilot plan and the server still owns all
        # aggregation; this is transport back-pressure, not a second Agent
        # execution path.
        want_async = body.get("async") is True or body.get("async") == "1"
        if want_async:
            message = body.get("message")
            if not isinstance(message, str) or not message.strip() or len(message) > 2000:
                raise BadRequest("问题必须为 1–2000 字")
            history = body.get("history")
            if history is not None and (not isinstance(history, list) or len(history) > 200):
                raise BadRequest("对话历史过长，请新建对话后重试")
            # Canonicalize untrusted history before it enters the durable queue
            # or request hash.  The model path applies the same chart/context
            # isolation, but storing arbitrary LAN payload strings would let a
            # client inflate jobs.sqlite without adding useful context.
            safe_history = []
            for item in (history or [])[-200:]:
                if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
                    continue
                content = item.get("content")
                if not isinstance(content, str):
                    continue
                safe_history.append({
                    "role": item["role"], "content": content[:500],
                    **({"chartId": str(item["chartId"])[:240]} if item.get("chartId") is not None else {}),
                    **({"contextKey": str(item["contextKey"])[:32000]} if item.get("contextKey") is not None else {}),
                })
            history = safe_history
            request_hash = hashlib.sha256(json.dumps(
                {"message": message, "chartId": body.get("chartId") or "",
                 "contextKey": body.get("contextKey") or "",
                 "filterScopes": body.get("filterScopes"),
                 "history": history or []},
                ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()
            chart_key = str(body.get("chartId") or "__report__")[:120]
            request_id = body.get("requestId")
            if request_id is not None:
                if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", request_id):
                    raise BadRequest("requestId 格式无效")
            if request_id:
                # A proxy retry after a lost 202 must not charge the model twice.
                # The persistent store is the final atomic gate for two processes
                # racing with the same requestId.
                try:
                    existing = _jobs.find_by_request(reportId, request_id, request_hash=request_hash)
                except _jobs.JobRequestConflict as exc:
                    respond_json(_handler, 409, {"error": {"code": "RequestConflict",
                        "message": "requestId 已对应另一份请求，请生成新的 requestId。",
                        "jobId": str(exc)}})
                    return
                if existing is not None:
                    respond_json(_handler, 202, {"jobId": existing["id"],
                        "status": existing["status"], "reused": True})
                    return
            try:
                # Bound model sockets/threads across cards. Same-card
                # duplication is still rejected by the atomic conflict check.
                job_id = _jobs.create("copilot", reportId, "chart:" + chart_key,
                                      max_running=4, request_key=request_id,
                                      request_hash=request_hash,
                                      priority=70,
                                      payload={"message": message, "chartId": body.get("chartId") or "",
                                               "history": history or [], "contextKey": body.get("contextKey"),
                                               "filterScopes": body.get("filterScopes"),
                                               "governanceScope": _governance_scope(_handler, reportId),
                                               "recoveryPolicy": "safe-read"},
                                      queued=True,
                                      trace_id=getattr(_handler, "trace_id", None))
            except _jobs.JobReplay as exc:
                existing = _jobs.get(str(exc))
                respond_json(_handler, 202, {"jobId": str(exc),
                    "status": (existing or {}).get("status", "running"), "reused": True})
                return
            except _jobs.JobRequestConflict as exc:
                respond_json(_handler, 409, {"error": {"code": "RequestConflict",
                    "message": "requestId 已对应另一份请求，请生成新的 requestId。",
                    "jobId": str(exc)}})
                return
            except _jobs.JobConflict as exc:
                respond_json(_handler, 409, {"error": {"code": "Conflict",
                    "message": "当前交互卡片已有一项 Copilot 任务，请等待完成或停止等待后再试。",
                    "jobId": str(exc)}})
                return
            except _jobs.JobCapacity:
                respond_json(_handler, 429, {"error": {"code": "TooManyRequests",
                    "message": "Copilot 后台任务较多，请稍后重试。"}},
                             {"Retry-After": "3"})
                return

            # The same durable queue is consumed by the embedded worker or an
            # external ``run_server --worker --worker-kinds copilot`` process.
            respond_json(_handler, 202, {"jobId": job_id, "status": "queued",
                                         "retryToken": _jobs.retry_token(job_id)})
            return
        try:
            result = run_turn(entry, engine, load_config(registry), body.get("message"),
                              body.get("chartId") or "", body.get("history") or [],
                              complete=lambda cfg, msgs: model_complete(
                                  cfg, msgs, health_path=registry.path.parent / "provider-health.sqlite3",
                                  governance_scope=_governance_scope(_handler, reportId)),
                              context_key=body.get("contextKey"),
                              filter_scopes=body.get("filterScopes"))
        except CopilotError as exc:
            raise BadRequest(str(exc)) from exc
        respond_json(_handler, 200, result)

    def h_copilot_job(_handler, reportId, jobId):
        """Poll an async Copilot turn; metadata is shared, worker execution is local."""
        if not _copilot_origin(_handler):
            return
        if not _agent_auth(_handler, reportId):
            return
        job = _jobs.get(jobId)
        if job is None or job.get("kind") not in {"copilot", "agent_plan", "evidence_export", "monitor"} or job.get("reportId") != reportId:
            respond_json(_handler, 404, {"error": {"code": "NotFound",
                "message": "Copilot 任务不存在或已过期，结果不会被伪造。"}})
            return
        payload = {"id": job["id"], "status": job["status"], "phase": job["phase"],
                   "startedAt": job["startedAt"], "finishedAt": job["finishedAt"],
                   "traceId": job.get("traceId"),
                   "canRecover": bool(job.get("canRecover")),
                   "pollAfterMs": 900 if job["status"] == "running" else 0,
                   "events": _jobs.events(jobId), "retryToken": _jobs.retry_token(jobId)}
        if job.get("result") is not None:
            payload["result"] = job["result"]
        if job.get("error"):
            payload["error"] = job["error"]
        respond_json(_handler, 200, payload)

    def h_copilot_jobs(_handler, reportId):
        """Bounded multi-card task tray metadata; evidence stays behind job poll."""
        if not _copilot_origin(_handler):
            return
        if not _agent_auth(_handler, reportId):
            return
        if registry.get(reportId) is None:
            raise NotFound(f"report {reportId!r} not registered")
        tasks = []
        for job in _jobs.list_jobs(reportId):
            if job.get("kind") not in {"copilot", "agent_plan", "evidence_export", "monitor"}:
                continue
            tasks.append({key: job.get(key) for key in (
                "id", "kind", "datasetId", "status", "phase", "startedAt", "finishedAt",
                "cancelRequested", "cancelReason", "error", "traceId", "retryToken", "canRecover")})
        respond_json(_handler, 200, {"tasks": tasks})

    def h_copilot_job_cancel(_handler, reportId, jobId):
        """Request cooperative cancellation; never claim a running model was killed."""
        if not _copilot_origin(_handler):
            return
        if not _agent_auth(_handler, reportId):
            return
        job = _jobs.get(jobId)
        if job is None or job.get("kind") not in {"copilot", "agent_plan", "evidence_export", "monitor"} or job.get("reportId") != reportId:
            respond_json(_handler, 404, {"error": {"code": "NotFound",
                "message": "Copilot 任务不存在或已过期。"}})
            return
        body = read_json(_handler)
        retry_token = body.get("retryToken") if isinstance(body, dict) else None
        if retry_token and not _jobs.verify_retry_token(jobId, str(retry_token)):
            respond_json(_handler, 403, {"error": {"code": "Forbidden", "message": "任务 retry token 无效"}})
            return
        accepted = _jobs.cancel(jobId)
        current = _jobs.get(jobId) or job
        if current.get("status") == "cancelled" and current.get("kind") == "evidence_export":
            _cleanup_evidence_export(registry, current)
        respond_json(_handler, 200, {"ok": accepted, "status": current.get("status"),
                                     "phase": current.get("phase")})

    def h_copilot_job_recover(_handler, reportId, jobId):
        """Requeue only an explicitly replayable lost read/export task."""
        if not _copilot_origin(_handler):
            return
        if not _agent_auth(_handler, reportId):
            return
        job = _jobs.get(jobId)
        if job is None or job.get("reportId") != reportId \
                or job.get("kind") not in {"copilot", "agent_plan", "evidence_export", "monitor"}:
            raise NotFound("Copilot 任务不存在或已过期。")
        body = read_json(_handler)
        token = body.get("retryToken") if isinstance(body, dict) else None
        if not token:
            respond_json(_handler, 403, {"error": {"code": "Forbidden",
                "message": "重试 lost 任务必须提供 retry token"}})
            return
        try:
            recovered = _jobs.recover_lost(jobId, str(token))
        except _jobs.JobRecoveryDenied as exc:
            respond_json(_handler, 403, {"error": {"code": "Forbidden", "message": str(exc)}})
            return
        except _jobs.JobConflict as exc:
            respond_json(_handler, 409, {"error": {"code": "Conflict",
                "message": "同一数据源已有任务进行中，请先等待或取消它。", "jobId": str(exc)}})
            return
        respond_json(_handler, 202, {"jobId": jobId, "status": recovered.get("status"),
                                     "phase": recovered.get("phase"),
                                     "retryToken": _jobs.retry_token(jobId), "recovered": True})

    def h_copilot_evidence_export(_handler, reportId):
        """Export a completed evidence Job asynchronously; never inline large rows."""
        if not _copilot_origin(_handler):
            return
        if not _agent_auth(_handler, reportId):
            return
        body = read_json(_handler)
        _audit(_handler, reportId, "evidence.export", str(body.get("jobId") or ""),
               {"format": body.get("format")})
        source_id = body.get("jobId")
        source = _jobs.get(str(source_id or ""))
        if source is None or source.get("reportId") != reportId or source.get("status") != "done" \
                or source.get("kind") not in {"copilot", "agent_plan"}:
            raise NotFound("可导出的 Agent 证据任务不存在或尚未完成")
        fmt = str(body.get("format") or "json").lower()
        if fmt not in {"json", "csv", "markdown"}:
            raise BadRequest("format must be json, csv or markdown")
        request_id = body.get("requestId")
        if request_id is not None and (not isinstance(request_id, str)
                                       or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", request_id)):
            raise BadRequest("requestId 格式无效")
        request_hash = hashlib.sha256((str(source_id) + "\x00" + fmt).encode()).hexdigest()
        if request_id:
            existing = _jobs.find_by_request(reportId, request_id, request_hash=request_hash)
            if existing is not None:
                respond_json(_handler, 202, {"jobId": existing["id"], "status": existing["status"], "reused": True})
                return
        try:
            export_job = _jobs.create("evidence_export", reportId, "export:" + str(source_id)[:80],
                                      max_running=2, request_key=request_id, request_hash=request_hash,
                                      priority=40, payload={"sourceId": str(source_id), "format": fmt,
                                                           "recoveryPolicy": "safe-export"},
                                      queued=True,
                                      trace_id=getattr(_handler, "trace_id", None))
        except _jobs.JobReplay as exc:
            existing = _jobs.get(str(exc))
            respond_json(_handler, 202, {"jobId": str(exc), "status": (existing or {}).get("status", "running"), "reused": True})
            return
        except _jobs.JobConflict as exc:
            respond_json(_handler, 409, {"error": {"code": "Conflict", "message": "相同证据正在导出。", "jobId": str(exc)}})
            return
        except _jobs.JobCapacity:
            respond_json(_handler, 429, {"error": {"code": "TooManyRequests", "message": "导出任务较多，请稍后重试。"}},
                         {"Retry-After": "3"})
            return
        # Evidence export is also a durable queued job; the worker reads only
        # the completed source Job result and writes a bounded 0600 artifact.
        respond_json(_handler, 202, {"jobId": export_job, "status": "queued",
                                     "retryToken": _jobs.retry_token(export_job)})

    def h_copilot_evidence_download(_handler, reportId, exportId):
        if not _copilot_origin(_handler):
            return
        if not _agent_auth(_handler, reportId):
            return
        job = _jobs.get(exportId)
        if job is None or job.get("kind") != "evidence_export" or job.get("reportId") != reportId \
                or job.get("status") != "done" or not isinstance(job.get("result"), dict):
            raise NotFound("证据导出不存在或尚未完成")
        result = job["result"]
        extension = {"json": "json", "csv": "csv", "markdown": "md"}.get(result.get("format"))
        if not extension:
            raise NotFound("证据导出格式无效")
        path = registry.path.parent / "copilot-exports" / f"export-{exportId}.{extension}"
        try:
            if time.time() - path.stat().st_mtime > 600:
                path.unlink(missing_ok=True)
                raise FileNotFoundError
            raw = path.read_bytes()
        except OSError as exc:
            raise NotFound("证据导出已过期") from exc
        content_type = result.get("contentType") or "application/octet-stream"
        _send_body(_handler, 200, raw, content_type,
                   {"Cache-Control": "no-store", "Content-Disposition": f'attachment; filename="evidence-{exportId}.{extension}"'},
                   compress=False)

    def h_copilot_monitors(_handler, reportId):
        if not _copilot_origin(_handler):
            return
        if not _agent_auth(_handler, reportId):
            return
        if registry.get(reportId) is None:
            raise NotFound(f"report {reportId!r} not registered")
        respond_json(_handler, 200, {"monitors": _jobs.list_monitors(reportId)})

    def h_copilot_audit(_handler, reportId):
        if not _copilot_origin(_handler):
            return
        if not _agent_auth(_handler, reportId):
            return
        if registry.get(reportId) is None:
            raise NotFound(f"report {reportId!r} not registered")
        respond_json(_handler, 200, {"events": _jobs.audit_events(reportId)})

    def h_copilot_monitor_create(_handler, reportId):
        if not _copilot_origin(_handler):
            return
        if not _agent_editor_auth(_handler, reportId):
            return
        entry = registry.get(reportId)
        if entry is None:
            raise NotFound(f"report {reportId!r} not registered")
        body = read_json(_handler)
        _audit(_handler, reportId, "monitor.create", str(body.get("name") or ""),
               {"tool": body.get("tool")})
        gate = check_edit_gate(body, edit_policy_of(entry))
        if gate:
            respond_json(_handler, 403, {"error": {"code": "Forbidden", "message": gate}})
            return
        name = str(body.get("name") or "").strip()
        tool = str(body.get("tool") or "")
        args = body.get("args") or {}
        try:
            interval = int(body.get("intervalSeconds") or 3600)
        except (TypeError, ValueError):
            raise BadRequest("intervalSeconds must be an integer") from None
        if not name or len(name) > 120:
            raise BadRequest("monitor requires a bounded name")
        try:
            tool, args = _validate_monitor_definition(tool, args)
        except ValueError as exc:
            raise BadRequest(str(exc)) from exc
        if interval < 30 or interval > 86400:
            raise BadRequest("intervalSeconds must be between 30 and 86400")
        monitor_id = _jobs.create_monitor(reportId, name, tool, args, interval)
        respond_json(_handler, 201, {"id": monitor_id, "reportId": reportId, "enabled": True,
                                     "tool": tool, "intervalSeconds": interval})

    def h_copilot_monitor_toggle(_handler, reportId, monitorId):
        if not _copilot_origin(_handler):
            return
        if not _agent_editor_auth(_handler, reportId):
            return
        entry = registry.get(reportId)
        if entry is None:
            raise NotFound(f"report {reportId!r} not registered")
        body = read_json(_handler)
        _audit(_handler, reportId, "monitor.toggle", monitorId,
               {"enabled": bool(body.get("enabled"))})
        gate = check_edit_gate(body, edit_policy_of(entry))
        if gate:
            respond_json(_handler, 403, {"error": {"code": "Forbidden", "message": gate}})
            return
        changed = _jobs.set_monitor_enabled(monitorId, reportId, bool(body.get("enabled")))
        if not changed:
            raise NotFound("monitor 不存在")
        respond_json(_handler, 200, {"ok": True, "id": monitorId, "enabled": bool(body.get("enabled"))})

    def h_copilot_monitor_delete(_handler, reportId, monitorId):
        if not _copilot_origin(_handler):
            return
        if not _agent_editor_auth(_handler, reportId):
            return
        entry = registry.get(reportId)
        if entry is None:
            raise NotFound(f"report {reportId!r} not registered")
        body = read_json(_handler)
        _audit(_handler, reportId, "monitor.delete", monitorId)
        gate = check_edit_gate(body, edit_policy_of(entry))
        if gate:
            respond_json(_handler, 403, {"error": {"code": "Forbidden", "message": gate}})
            return
        if not _jobs.delete_monitor(monitorId, reportId):
            raise NotFound("monitor 不存在")
        respond_json(_handler, 200, {"ok": True, "id": monitorId})

    def h_copilot_notification_propose(_handler, reportId):
        """Validate a notification proposal; never contact an external service."""
        if not _copilot_origin(_handler):
            return
        if not _agent_editor_auth(_handler, reportId):
            return
        entry = registry.get(reportId)
        if entry is None:
            raise NotFound(f"report {reportId!r} not registered")
        body = read_json(_handler)
        gate = check_edit_gate(body, edit_policy_of(entry))
        if gate:
            respond_json(_handler, 403, {"error": {"code": "Forbidden", "message": gate}})
            return
        channel = str(body.get("channel") or "").lower()
        destination = str(body.get("destination") or "").strip()
        message = str(body.get("message") or "").strip()
        if channel not in {"email", "slack", "webhook"}:
            raise BadRequest("channel must be email, slack or webhook")
        if not destination or len(destination) > 500 or not message or len(message) > 2000:
            raise BadRequest("notification destination/message is invalid")
        proposal = {"reportId": reportId, "channel": channel,
                    "destination": destination, "message": message,
                    "requiresApproval": True, "sideEffect": "notify"}
        proposal_id = _notification_proposal_id(reportId, channel, destination, message)
        _audit(_handler, reportId, "notification.propose", proposal_id,
               {"channel": channel, "requiresApproval": True})
        respond_json(_handler, 200, {"status": "needsApproval", "proposalId": proposal_id,
                                     "proposal": {**proposal, "message": "[已隐藏，需审批后发送]"},
                                     "message": "通知尚未发送；需要明确审批并配置外部通知适配器。"})

    def h_copilot_notification_send(_handler, reportId):
        """Send an explicitly approved webhook through a server allowlist.

        Email/Slack adapters remain disabled; webhook delivery is opt-in via
        ``LRM_NOTIFICATION_WEBHOOK_URL`` and accepts only the exact configured
        destination.  The request body never controls the network destination.
        """
        if not _copilot_origin(_handler):
            return
        if not _agent_editor_auth(_handler, reportId):
            return
        entry = registry.get(reportId)
        if entry is None:
            raise NotFound(f"report {reportId!r} not registered")
        body = read_json(_handler)
        gate = check_edit_gate(body, edit_policy_of(entry))
        if gate:
            respond_json(_handler, 403, {"error": {"code": "Forbidden", "message": gate}})
            return
        if body.get("approved") is not True:
            respond_json(_handler, 409, {"error": {"code": "ApprovalRequired",
                                                       "message": "通知必须由用户明确批准后发送。"}})
            return
        channel = str(body.get("channel") or "").lower()
        destination = str(body.get("destination") or "").strip()
        message = str(body.get("message") or "").strip()
        proposal_id = str(body.get("proposalId") or "")
        if channel != "webhook" or not destination or not message or len(message) > 2000:
            raise BadRequest("仅支持 webhook，且 message 必须为 1–2000 字")
        expected_id = _notification_proposal_id(reportId, channel, destination, message)
        if not hmac.compare_digest(proposal_id, expected_id):
            raise Conflict("通知 proposal 已变化，请重新预览并批准")
        configured_url = str(os.environ.get("LRM_NOTIFICATION_WEBHOOK_URL") or "").strip()
        if not configured_url or destination != configured_url:
            respond_json(_handler, 501, {"error": {"code": "NotificationAdapterNotConfigured",
                                                   "message": "webhook 未配置或 destination 不在服务端 allowlist；未发送。"}})
            return
        parsed = urlparse(configured_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise BadRequest("通知 webhook 必须是无凭证 HTTPS URL")
        delivery = _jobs.claim_notification(proposal_id, reportId, channel, destination)
        if not delivery.get("send"):
            state = delivery.get("status")
            if state == "sent":
                respond_json(_handler, 200, {"sent": True, "deduplicated": True,
                                             "channel": channel, "proposalId": proposal_id,
                                             "attempts": delivery.get("attempts", 0)})
                return
            if state == "in_flight":
                respond_json(_handler, 202, {"sent": False, "status": "in_flight",
                                             "channel": channel, "proposalId": proposal_id})
                return
            if state == "retry_exhausted":
                respond_json(_handler, 429, {"error": {"code": "NotificationRetryExhausted",
                    "message": "通知发送失败次数已达上限，请重新生成并批准新的通知提案。"}})
                return
            raise Conflict("通知 proposal 与既有 delivery 不一致，请重新生成提案")
        from urllib.request import Request as _NotificationRequest, urlopen as _notification_urlopen
        payload = json.dumps({"text": message, "reportId": reportId,
                              "proposalId": proposal_id}, ensure_ascii=False).encode("utf-8")
        try:
            req = _NotificationRequest(configured_url, data=payload,
                                       headers={"Content-Type": "application/json",
                                                "User-Agent": "live-report-master-notifier/1",
                                                "X-Idempotency-Key": proposal_id}, method="POST")
            with _notification_urlopen(req, timeout=10) as response:
                status = int(getattr(response, "status", 200) or 200)
        except Exception as exc:
            _jobs.finish_notification(proposal_id, sent=False, error=type(exc).__name__,
                                      attempts=delivery.get("attempts"))
            _audit(_handler, reportId, "notification.send.error", proposal_id,
                   {"channel": channel, "error": type(exc).__name__,
                    "attempts": delivery.get("attempts", 0)})
            raise BadRequest(f"webhook 发送失败：{type(exc).__name__}") from exc
        _jobs.finish_notification(proposal_id, sent=True, attempts=delivery.get("attempts"))
        _audit(_handler, reportId, "notification.send", proposal_id,
               {"channel": channel, "status": status,
                "attempts": delivery.get("attempts", 0)})
        respond_json(_handler, 200, {"sent": True, "channel": channel,
                                     "proposalId": proposal_id, "status": status,
                                     "attempts": delivery.get("attempts", 0)})

    def h_list_datasets(_handler):
        out = []
        for name in engine.list_tables():
            info = engine.table_info(name)
            if info:
                out.append({
                    "id": name,
                    "label": name,
                    "rowCount": info.row_count,
                    "columnCount": len(info.columns),
                })
        respond_json(_handler, 200, {"datasets": out})

    def h_dataset_schema(_handler, datasetId):
        info = engine.table_info(datasetId)
        if info is None:
            raise NotFound(f"dataset {datasetId!r} not loaded")
        # S2 数据面闸（XU-04，E1 投影语义）：字段元数据里的敏感列整列剔除
        # （min/max/mean 等统计也是敏感值的聚合，列名本身也不该再出现在作者面）。
        _s2_sensitive = dataset_sensitive_cols(registry, datasetId)
        payload = {
            "id": datasetId,
            "label": datasetId,
            "columns": [
                {
                    "name": c.name,
                    "type": c.inferred_type,
                    "sqlType": c.sql_type,
                    "cardinality": c.distinct,
                    "distinct": c.distinct,
                    **({"min": c.min, "max": c.max, "mean": c.mean, "sum": c.sum} if c.inferred_type == "measure" else {}),
                    **({"sampleMin": c.sample_min, "sampleMax": c.sample_max} if c.inferred_type == "time" else {}),
                }
                for c in info.schema
                if not (_s2_sensitive and c.name in _s2_sensitive)
            ],
            "rowCount": info.row_count,
        }
        respond_json(_handler, 200, payload)

    def h_dataset_aggregate(_handler, datasetId):
        info = engine.table_info(datasetId)
        if info is None:
            raise NotFound(f"dataset {datasetId!r} not loaded")
        body = read_json(_handler)
        # S2 数据面闸（XU-04）：引用敏感列 → 400；无声明 = 零漂移放行。
        assert_aggregate_privacy(body, dataset_sensitive_cols(registry, datasetId),
                                 f"数据集 {datasetId} 的聚合查询")
        request = parse_request(body)
        response = aggregate(engine, datasetId, request)
        respond_json(_handler, 200, response.to_dict())

    def h_aggregate_multi(_handler):
        """P6: one POST for several single-table aggregates (cross-table charts)."""
        body = read_json(_handler)
        sub_requests = body.get("requests")
        if not isinstance(sub_requests, list) or not sub_requests:
            raise BadRequest("aggregate-multi requires a non-empty requests array")
        if len(sub_requests) > 16:
            raise BadRequest("aggregate-multi supports at most 16 sub-requests")
        # S2 数据面闸（XU-04）：逐子请求按各自 datasetId 闸敏感列；无声明 = 零漂移。
        for sub in sub_requests:
            if isinstance(sub, dict) and isinstance(sub.get("datasetId"), str):
                assert_aggregate_privacy(sub, dataset_sensitive_cols(registry, sub["datasetId"]),
                                         f"数据集 {sub['datasetId']} 的批量聚合子查询")
        results = aggregate_multi(engine, sub_requests)
        respond_json(_handler, 200, {"results": results})

    def h_dataset_rows(_handler, datasetId):
        """Read-only, bounded detail rows for the detail table."""
        info = engine.table_info(datasetId)
        if info is None:
            raise NotFound(f"dataset {datasetId!r} not loaded")
        body = read_json(_handler)
        columns = list(body.get("columns") or info.columns)
        if not columns:
            raise BadRequest("at least one column is required")
        unknown = [c for c in columns if c not in info.columns]
        if unknown:
            raise BadRequest(f"unknown columns: {unknown}")
        # S2 数据面闸（XU-04，E1 投影语义）：明细读**不 400**——敏感列从请求列集
        # 与返回行里直接剔除（作者/读者面对该列无感）；即使调用方显式点名也不回。
        # includeRowIds 的 rowid 是伪列不是列值，不受影响。无声明 = 零漂移。
        _s2_sensitive = dataset_sensitive_cols(registry, datasetId)
        if _s2_sensitive:
            columns = [c for c in columns if c not in _s2_sensitive]
        try:
            page = max(1, int(body.get("page", 1)))
            page_size = min(100, max(1, int(body.get("pageSize", 50))))
        except (TypeError, ValueError) as exc:
            raise BadRequest(f"invalid pagination: {exc}") from exc
        from .aggregator import _filter_clause, _order_clause, _quote
        where, params = _filter_clause(body.get("filter") or [], set(info.columns), engine)
        order_by = list(body.get("orderBy") or [])
        if any(not isinstance(item, dict) or not isinstance(item.get("column"), str) for item in order_by):
            raise BadRequest("orderBy entries must be objects with a column")
        if any(item.get("column") not in info.columns for item in order_by):
            raise BadRequest("orderBy references an unknown column")
        order, _ = _order_clause(order_by, info.columns[0])
        # includeRowIds（2026-09-28 数据管理）：附带 SQLite/DuckDB 的 rowid 伪列，
        # 供「删除选中行」回传（h_dataset_rows_delete 的 rowids 模式）。引擎不
        # 支持 rowid 时给可行动 400，绝不静默丢列（客户端按 rowIds 对齐选择框）。
        include_rid = bool(body.get("includeRowIds"))
        select = ", ".join(_quote(c) for c in columns)
        if include_rid:
            select = 'rowid AS "__ai_rid", ' + select
        offset = (page - 1) * page_size
        if not columns:
            # 投影退化（S2）：请求列集全是敏感列——按投影语义回 200 空列空行
            #（不 400、不泄漏），元数据（total/分页）照实。
            total_row = engine.execute(f'SELECT COUNT(*) AS n FROM {_quote(datasetId)} {where}', tuple(params))
            total = int(total_row[0]["n"] if total_row else 0)
            respond_json(_handler, 200, {
                "columns": [], "rows": [], "page": page, "pageSize": page_size,
                "total": total, "hasNext": False,
                **({"rowIds": []} if include_rid else {}),
            })
            return
        try:
            rows = engine.execute(
                f'SELECT {select} FROM {_quote(datasetId)} {where} {order} LIMIT {page_size} OFFSET {offset}',
                tuple(params),
            )
        except Exception as exc:
            if include_rid:
                raise BadRequest(f"当前存储引擎不支持 rowid 行定位（{exc}）；"
                                 "请改用「按筛选删除」。")
            raise
        total_row = engine.execute(f'SELECT COUNT(*) AS n FROM {_quote(datasetId)} {where}', tuple(params))
        total = int(total_row[0]["n"] if total_row else 0)
        payload = {
            "columns": columns,
            "rows": [[row.get(c) for c in columns] for row in rows],
            "page": page,
            "pageSize": page_size,
            "total": total,
            "hasNext": offset + len(rows) < total,
        }
        if include_rid:
            payload["rowIds"] = [row.get("__ai_rid") for row in rows]
        respond_json(_handler, 200, payload)

    def h_dataset_values(_handler, datasetId, column):
        """Distinct values for a column (per-card filter options)."""
        info = engine.table_info(datasetId)
        if info is None:
            raise NotFound(f"dataset {datasetId!r} not loaded")
        if column not in info.columns:
            raise BadRequest(f"unknown column: {column}")
        # S2 数据面闸（XU-04）：敏感列的值域就是敏感值本体 → 400（不回显值）。
        assert_not_sensitive([column], dataset_sensitive_cols(registry, datasetId),
                             f"数据集 {datasetId} 的值域查询")
        from .aggregator import _quote
        rows = engine.execute(
            f'SELECT DISTINCT {_quote(column)} AS v FROM {_quote(datasetId)} ORDER BY 1',
            (),
        )
        values = [r["v"] for r in rows if r is not None and r["v"] is not None]
        respond_json(_handler, 200, {"values": values})

    def h_dataset_values_filtered(_handler, datasetId, column):
        """Bounded distinct values under a filter (级联筛选的选项源).

        POST body {filter: [predicates], limit: N} → distinct `column` values
        over rows matching the predicates, ORDER BY value, hard-capped
        (default 200, max 500). Unlike GET /values/:column (unbounded, whole
        domain) this is always bounded, so a cascade child dropdown never
        pulls a high-cardinality column's full value domain."""
        info = engine.table_info(datasetId)
        if info is None:
            raise NotFound(f"dataset {datasetId!r} not loaded")
        if column not in info.columns:
            raise BadRequest(f"unknown column: {column}")
        # S2 数据面闸（XU-04）：同 GET /values——敏感列值域 400。
        assert_not_sensitive([column], dataset_sensitive_cols(registry, datasetId),
                             f"数据集 {datasetId} 的级联值域查询")
        body = read_json(_handler)
        preds = body.get("filter") or []
        if not isinstance(preds, list):
            raise BadRequest("filter must be an array of predicates")
        try:
            limit = max(1, min(int(body.get("limit") or 200), 500))
        except (TypeError, ValueError):
            limit = 200
        from .aggregator import _filter_clause, _quote
        # _filter_clause returns the clause WITH its "WHERE " prefix.
        where, params = _filter_clause(preds, set(info.columns), engine)
        rows = engine.execute(
            f'SELECT DISTINCT {_quote(column)} AS v FROM {_quote(datasetId)} '
            f'{where} ORDER BY 1 LIMIT {limit}',
            tuple(params),
        )
        respond_json(_handler, 200, {"values": [r["v"] for r in rows if r is not None and r["v"] is not None]})

    def h_dataset_sample(_handler, datasetId, column):
        """A few REAL distinct values for one column (字段配置的「示例值」列).

        Bounded server-side (LIMIT 8) so opening the field config never pulls a
        column's whole value domain (h_dataset_values has no limit and can be
        megabytes on a high-cardinality measure)."""
        info = engine.table_info(datasetId)
        if info is None:
            raise NotFound(f"dataset {datasetId!r} not loaded")
        if column not in info.columns:
            raise BadRequest(f"unknown column: {column}")
        # S2 数据面闸（XU-04）：示例值 = 敏感值本体 → 400（与 values 同一家族）。
        assert_not_sensitive([column], dataset_sensitive_cols(registry, datasetId),
                             f"数据集 {datasetId} 的示例值查询")
        from .aggregator import _quote
        rows = engine.execute(
            f'SELECT DISTINCT {_quote(column)} AS v FROM {_quote(datasetId)} '
            f'WHERE {_quote(column)} IS NOT NULL ORDER BY 1 LIMIT 8',
            (),
        )
        respond_json(_handler, 200, {"values": [r["v"] for r in rows if r["v"] is not None]})

    def h_report_tables(_handler, reportId):
        """数据表组全量清单（2026-09-28）：本报告在本机服务端**真实存储**的每张表。

        数据源更新/上传新表/删除数据源的日常动线会在 SQLite 里留下「清单外」的
        物理表：数据源删除只改 manifest（客户端 removeDataset 从不通知服务端），
        引擎表与快照文件原样留着（真机实锤：PSI 报告下躺着一张 335k 行的
        ``PSI__ds-*`` 历史表，UI 不可见、无法删、白占磁盘）。这里把全部物理表
        摊开：``referenced=true`` = 本报告仍在声明；其余 = 历史/未引用表（含重启
        后未被引擎装载、但文件里还活着的）。被**其它**报告声明（共享副本）的表
        不进本报告的组——删除它们是另一份报告的事。
        """
        entry = registry.get(reportId)
        if entry is None:
            raise NotFound(f"report {reportId!r} not registered")
        own_ids = _report_dataset_ids(entry)
        others: dict[str, list[str]] = {}
        for rid, e in registry.items_raw():
            if rid == reportId:
                continue
            for did in _report_dataset_ids(e):
                others.setdefault(did, []).append(rid)
        labels: dict[str, Any] = {}
        for d in ((entry.get("manifest") or {}).get("datasets") or []):
            if isinstance(d, dict) and d.get("id"):
                labels[str(d["id"])] = d.get("label")
        tables = []
        for name, shape in _physical_tables(engine).items():
            referenced = name in own_ids
            if not referenced and (name in others
                                   or not name.startswith(str(reportId) + "__")):
                continue
            item: dict[str, Any] = {"name": name, "referenced": referenced,
                                    "rows": shape.get("rows") or 0,
                                    "cols": shape.get("cols") or 0}
            if shape.get("unloaded"):
                item["unloaded"] = True
            if referenced:
                item["label"] = labels.get(name)
            shared = sorted(others.get(name, []))
            if shared:
                item["sharedWith"] = shared
            nbytes = _sqlite_table_bytes(engine, name)
            if nbytes is not None:
                item["bytes"] = nbytes
            tables.append(item)
        tables.sort(key=lambda t: (not t["referenced"], str(t["name"])))
        respond_json(_handler, 200, {"reportId": reportId, "tables": tables})

    def h_dataset_drop(_handler, reportId, datasetId):
        """把一张数据表从本机服务端**真删掉**（引擎表 + 快照文件 + 注册表记账）。

        与「删除数据源」（客户端 removeDataset，只改 manifest）互补：那条路径
        服役于「表还要在别处用/只是移出报告」，本端点才是磁盘释放。语义：

        - 无其它报告声明同一 dataset id → ``engine.drop_table`` + 快照清理
          （``_snapshot_files_for``：replaced/added/pre-delete/构建期 ns 前缀）；
        - 有其它报告声明（副本共享同一物理表）→ **只脱离本报告**（清注册表
          记账），引擎表与快照原样保留，响应 ``{dropped:false, detached:true,
          sharedWith:[…]}``——共享表被 DROP 会让副本的每张图表 400；
        - 未装载的孤儿表（重启后不在引擎缓存）照样可删：DROP TABLE IF EXISTS
          不依赖缓存。

        编辑闸与 add/replace/save 同一策略（check_edit_gate 契约：任何端点都
        改不了被锁的报告）。
        """
        entry = registry.get(reportId)
        if entry is None:
            raise NotFound(f"report {reportId!r} not registered")
        body = read_json(_handler)
        gate = check_edit_gate(body, edit_policy_of(entry))
        if gate:
            respond_json(_handler, 403, {"error": {"code": "Forbidden", "message": gate}})
            return
        own_ids = _report_dataset_ids(entry)
        exists = engine.table_info(datasetId) is not None
        if not exists:
            # 未装载也可能物理存在（重启后的孤儿）——按物理清单再判一次。
            exists = datasetId in _physical_tables(engine)
        if not exists and datasetId not in own_ids:
            raise NotFound(f"dataset {datasetId!r} is neither loaded nor declared "
                           f"by report {reportId!r}")
        others = sorted(rid for rid, e in registry.items_raw()
                        if rid != reportId and datasetId in _report_dataset_ids(e))
        dropped = False
        if others:
            dropped = False
        else:
            engine.drop_table(datasetId)
            dropped = True
        entry["datasets"] = [d for d in (entry.get("datasets") or [])
                             if not (isinstance(d, dict) and d.get("id") == datasetId)]
        purged: list[str] = []
        if dropped:
            data_dir = getattr(registry, "data_dir", None)
            if data_dir:
                for p in _snapshot_files_for(Path(data_dir), str(reportId), datasetId):
                    try:
                        p.unlink()
                        purged.append(p.name)
                    except OSError:
                        pass
        log = list(entry.get("changelog") or [])
        log.append({"at": time.time(), "mode": "drop-table", "datasetId": datasetId,
                    "dropped": dropped, **({"sharedWith": others} if others else {}),
                    **({"purgedSnapshots": purged} if purged else {}),
                    "title": entry.get("title") or reportId})
        entry["changelog"] = log
        _register_full_entry(registry, reportId, entry)
        respond_json(_handler, 200, {"datasetId": datasetId, "dropped": dropped,
                                     "detached": not dropped,
                                     **({"sharedWith": others} if others else {}),
                                     "purgedSnapshots": purged})

    def h_dataset_rows_delete(_handler, datasetId):
        """行级数据管理（2026-09-28）：按选中行 / 按筛选条件真删数据行。

        两种模式（body ``mode``，缺省由载荷推断：有 ``rowIds`` 走 rowids）：

        - ``rowids``：``rowIds`` 来自 ``POST /rows`` 的 ``includeRowIds``（SQLite/
          DuckDB 的 rowid 伪列）。单次上限 10,000 个——批量删除走 filter 模式。
        - ``filter``：谓词数组与聚合/明细同一条 ``_filter_clause`` 编译（同一
          语义、同一大小写不敏感 contains），``DELETE … WHERE <子句>``。
          双保险：``confirmCount`` 与服务端实算命中数不符 → 409（视图过期保护）；
          命中数 = 全表 → 必须显式 ``allowAll:true``（防「空筛选=全删」手滑）。

        删后记账与 `_process_replace_table` 同一铁律：**重载真源必须跟着改**，
        否则重启后删除被旧快照悄悄复活。每个声明本表的报告条目都更新
        （副本共享表）：rowCount、过期 columnStats/filterOptions 清除、
        既有快照先转存 ``{stem}.pre-delete.json``（单份回滚保险，不自动加载），
        再把删后全表流式重写为 ``{stem}.replaced.json`` 并改指 source。
        若本表是某条合并关系的源表，宽表已脏 → 尽力自动重物化（同一
        load_relation 管道），失败只记 ``wideErrors`` 不回滚删除。
        """
        info = engine.table_info(datasetId)
        if info is None:
            raise NotFound(f"dataset {datasetId!r} not loaded")
        body = read_json(_handler)
        declaring: dict[str, dict] = {}
        for rid, e in registry.items_raw():
            if datasetId in _report_dataset_ids(e):
                declaring[rid] = registry.get(rid) or {}
        if not declaring:
            # manifest 未列但注册表记账仍在（刚 add 尚未首次同步的窗口）：记账
            # 条目也认——行删除的记账覆盖它，语义与重载管道一致。
            for rid, e in registry.items_raw():
                if any(isinstance(d, dict) and d.get("id") == datasetId
                       for d in (e.get("datasets") or [])):
                    declaring[rid] = registry.get(rid) or {}
        if not declaring:
            raise NotFound(f"dataset {datasetId!r} is not declared by any registered report")
        for rid in sorted(declaring):
            gate = check_edit_gate(body, edit_policy_of(declaring[rid]))
            if gate:
                respond_json(_handler, 403, {"error": {"code": "Forbidden", "message": gate}})
                return
        from .aggregator import _filter_clause, _quote
        mode = body.get("mode") or ("rowids" if body.get("rowIds") else "filter")
        if mode not in ("rowids", "filter"):
            raise BadRequest("mode must be 'rowids' or 'filter'")
        # S2 数据面闸（XU-04）：filter 模式的删除谓词引用敏感列 → 400（按敏感值
        # 删行=以敏感值为操作数且回显命中数）；rowids 模式按伪列删，照旧放行。
        if mode == "filter":
            _s2_sensitive: set[str] = set()
            for _e in declaring.values():
                _s2_sensitive |= sensitive_cols_of(_e, datasetId)
            assert_not_sensitive(_aggregate_payload_cols({"filter": body.get("filter")}),
                                 _s2_sensitive, f"数据集 {datasetId} 的按筛选删除")
        where, params = "", ()
        if mode == "rowids":
            raw_ids = body.get("rowIds")
            if not isinstance(raw_ids, list) or not raw_ids:
                raise BadRequest("rowIds must be a non-empty array of row ids")
            if len(raw_ids) > 10_000:
                raise BadRequest("rowIds supports at most 10,000 ids per call")
            ids: list[int] = []
            for x in raw_ids:
                if isinstance(x, bool) or not isinstance(x, int) or x < 1:
                    raise BadRequest(f"invalid row id: {x!r}")
                if x not in ids:
                    ids.append(x)
            marks = ", ".join("?" for _ in ids)
            where, params = f"WHERE rowid IN ({marks})", tuple(ids)
        else:
            preds = body.get("filter")
            if not isinstance(preds, list) or not preds:
                raise BadRequest(
                    "filter predicates required（空筛选=全表删除，请改用 allowAll 显式声明）")
            try:
                where, params = _filter_clause(preds, set(info.columns), engine)
            except AggregationError as exc:
                raise BadRequest(str(exc)) from exc
            if not where:
                raise BadRequest("filter matched nothing to compile")
        total_before = int((engine.execute(
            f'SELECT COUNT(*) AS n FROM {_quote(datasetId)}') or [{"n": 0}])[0]["n"])
        if mode == "filter":
            hits = int((engine.execute(
                f'SELECT COUNT(*) AS n FROM {_quote(datasetId)} {where}', params)
                or [{"n": 0}])[0]["n"])
            confirm = body.get("confirmCount")
            if confirm is not None:
                try:
                    confirm_n = int(confirm)
                except (TypeError, ValueError):
                    raise BadRequest("confirmCount must be an integer")
                if confirm_n != hits:
                    respond_json(_handler, 409, {"error": {"code": "Conflict",
                        "message": f"筛选命中数已变化（请求按 {confirm_n} 行确认，服务端实算 {hits} 行）。"},
                        "actualCount": hits})
                    return
            if hits >= total_before and not body.get("allowAll"):
                raise BadRequest(
                    f"该筛选命中全部 {hits:,} 行（整表删除）。如确定要清空本表，"
                    "请勾选「我知道这是整表删除」后重试。")
        engine.execute_mutation(f'DELETE FROM {_quote(datasetId)} {where}', params)
        total_after = int((engine.execute(
            f'SELECT COUNT(*) AS n FROM {_quote(datasetId)}') or [{"n": 0}])[0]["n"])
        deleted = total_before - total_after
        try:
            info.row_count = total_after
        except Exception:  # noqa: BLE001 — TableInfo 不可变也只是缓存不新鲜
            pass
        # 收尾：每个声明本表的报告条目都改记账 + 重写重载真源（副本共享表同删）。
        rewritten = 0
        data_dir = getattr(registry, "data_dir", None)
        for rid in sorted(declaring):
            entry = declaring[rid]
            for d in (entry.get("datasets") or []):
                if isinstance(d, dict) and d.get("id") == datasetId:
                    d["rowCount"] = total_after
            _touch_dataset_revision(entry, datasetId)
            manifest = entry.get("manifest") or {}
            for d in (manifest.get("datasets") or []):
                if isinstance(d, dict) and d.get("id") == datasetId:
                    d["rowCount"] = total_after
                    d.pop("columnStats", None)
                    d.pop("filterOptions", None)
            manifest["datasets"] = manifest.get("datasets") or []
            entry["manifest"] = manifest
            if data_dir:
                stem = _snapshot_stem(datasetId)
                snap_path = Path(data_dir) / f"{stem}.replaced.json"
                try:
                    # 既有**权威**快照 → 单份 pre-delete 备份（下次删除覆盖它；
                    # 永不进 _reload_all 的候选，纯人工回滚用）。权威 = reload 的
                    # 候选序：replaced.* > added.* > 记账 source 指向的构建期文件。
                    # XP-01：备份名后缀跟随源文件（.json/.sqlite 两代并存）——
                    # 内容是 sqlite 时叫 .pre-delete.json 会骗过按后缀分派的读回。
                    src_names = {Path(d.get("source") or "").name
                                 for d in (entry.get("datasets") or [])
                                 if isinstance(d, dict) and d.get("id") == datasetId}
                    prev_files = _snapshot_files_for(Path(data_dir), str(rid), datasetId)
                    backup_src = next((p for p in prev_files
                                       if p.name.startswith(f"{stem}.replaced.")), None) \
                        or next((p for p in prev_files
                                 if p.name.startswith(f"{stem}.added.")), None) \
                        or next((p for p in prev_files if p.name in src_names), None)
                    if backup_src is not None:
                        os.replace(backup_src, Path(data_dir)
                                   / f"{stem}.pre-delete{backup_src.suffix or '.json'}")
                    cols = list(info.columns)
                    def _row_iter():
                        for rec in engine.execute(
                                f'SELECT {", ".join(_quote(c) for c in cols)} '
                                f'FROM {_quote(datasetId)}'):
                            yield [rec.get(c) for c in cols]
                    # XP-01：write_dataset_snapshot 按当前快照格式换后缀并返回实际
                    # 路径——回填 snap_path，注册表 source 记的一定是真实文件。
                    snap_path = write_dataset_snapshot(snap_path, cols, _row_iter())
                    rewritten += 1
                    rel = os.path.relpath(snap_path, registry.path.parent)
                    for d in (entry.get("datasets") or []):
                        if isinstance(d, dict) and d.get("id") == datasetId:
                            d["source"] = rel
                except OSError:
                    # 写盘失败不回滚删除（引擎内已删），但必须如实告知重启会复活。
                    respond_json(_handler, 500, {"error": {"code": "IoError",
                        "message": f"行已删除，但快照重写失败：重启后这 {deleted:,} 行会复活。"
                                   "请检查磁盘/权限后重做一次删除或替换。"}})
                    return
            log = list(entry.get("changelog") or [])
            log.append({"at": time.time(), "mode": "delete-rows", "by": mode,
                        "datasetId": datasetId, "deleted": deleted,
                        "rows": total_after, "title": entry.get("title") or rid})
            entry["changelog"] = log
            registry.register(rid, entry)
        # 宽表已脏：源表行集变了 → 尽力重物化每条受影响的合并关系。
        rematerialized: list[dict[str, Any]] = []
        wide_errors: list[str] = []
        seen_rel: set[str] = set()
        for rid in sorted(declaring):
            for rel in ((declaring[rid].get("manifest") or {}).get("relations") or []):
                if not isinstance(rel, dict) or datasetId not in (rel.get("tables") or []):
                    continue
                key = str(rel.get("id") or rel)
                if key in seen_rel:
                    continue
                seen_rel.add(key)
                try:
                    res = _materialize_relation_core(rid, rel)
                    rematerialized.append({"datasetId": res["datasetId"],
                                           "rowCount": res["rowCount"]})
                except Exception as exc:  # noqa: BLE001 — 重物化失败不回滚删除
                    wide_errors.append(f"{rid}:{rel.get('name') or key}: {exc}")
        payload: dict[str, Any] = {"datasetId": datasetId, "deleted": deleted,
                                   "rowCount": total_after,
                                   "snapshotsRewritten": rewritten}
        if rematerialized:
            payload["rematerialized"] = rematerialized
        if wide_errors:
            payload["wideErrors"] = wide_errors
        respond_json(_handler, 200, payload)

    def h_dataset_add(_handler, reportId):
        """Persist a runtime-added dataset into an existing server report.

        Body (JSON): ``{password, dataset:{id,label,table:{columns,rows}}}``（原路径）。

        Body (raw, 2026-09-20 新增表直传): ``Content-Type: text/csv |
        text/tab-separated-values | application/octet-stream`` + ``?id=&label=&suffix=``
        查询串 + ``X-Edit-Password`` 头——大文件不再浏览器解析。csv/tsv 走
        ``_parse_delimited_table``；xlsx/octet-stream 落临时文件后走编译侧同一
        ``table_input.load_table_file``（编码回退/直通修复同一真源）。响应额外带
        ``columns`` + ``sample``（前 5 行）供客户端阶段 2 表单（改名/角色）。
        """
        entry = registry.get(reportId)
        if entry is None:
            raise NotFound(f"report {reportId!r} not registered")
        ctype = (_handler.headers.get("Content-Type") or "").strip().lower()
        if ctype.startswith(("text/csv", "text/tab-separated-values")) \
                or "spreadsheetml" in ctype or ctype.startswith("application/octet-stream"):
            gate = check_edit_gate({"password": _handler.headers.get("X-Edit-Password") or ""},
                                   edit_policy_of(entry))
            if gate:
                respond_json(_handler, 403, {"error": {"code": "Forbidden", "message": gate}})
                return
            qs = parse_qs(urlparse(_handler.path).query)
            ds = {"id": (qs.get("id") or [""])[0].strip(),
                  "label": (qs.get("label") or [""])[0].strip()}
            suffix = (qs.get("suffix") or [""])[0].strip()
            # suffix 只用于临时文件扩展名（loader.load_table_file 按扩展名分派）。
            # 直呼 API 传进路径字符（../、/）会让 mkstemp 试图在子目录建文件 →
            # 未捕获 OSError → 500；形状不对一律收敛缺省 .xlsx。
            if not re.fullmatch(r"\.[A-Za-z0-9]{1,9}", suffix):
                suffix = ".xlsx"
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,39}", ds["id"]):
                raise BadRequest("query id required (^[A-Za-z][A-Za-z0-9_-]{0,39}$)")
            if not ds["label"]:
                ds["label"] = ds["id"]
            if ctype.startswith(("text/csv", "text/tab-separated-values")):
                table = _parse_delimited_table(_handler)
                declared_types = None
            else:
                raw = _read_body_bytes(_handler)
                import tempfile as _tempfile
                fd, tmp = _tempfile.mkstemp(suffix=suffix or ".xlsx")
                os.close(fd)
                try:
                    with open(tmp, "wb") as tf:
                        tf.write(raw)
                    from table_input import load_table_file as _load_table_file
                    try:
                        loaded = _load_table_file(tmp)
                    except Exception as exc:
                        raise BadRequest(f"failed to parse uploaded file: {exc}")
                    table = {"columns": [str(c) for c in loaded.get("columns") or []],
                             "rows": [list(r) for r in loaded.get("rows") or []]}
                    # validate_table 已对整表推断过列类型（meta.types）——带上它，
                    # manifest 条目就不用再推断一遍（同一真源的第二消费）。
                    declared_types = dict((loaded.get("meta") or {}).get("types") or {})
                finally:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
        else:
            body = read_json(_handler)
            gate = check_edit_gate(body, edit_policy_of(entry))
            if gate:
                respond_json(_handler, 403, {"error": {"code": "Forbidden", "message": gate}})
                return
            ds = body.get("dataset") or {}
            table = ds.get("table") or {}
            declared_types = None
        raw_id = str(ds.get("id") or "")
        if not raw_id:
            raise BadRequest("dataset.id required")
        cols = table.get("columns") or []
        rows = table.get("rows") or []
        if not isinstance(cols, list) or not cols or len(cols) > 200:
            raise BadRequest("dataset columns must contain 1-200 fields")
        # 重复表头闸（与 replace / CSV 直传同口径，2026-09-26）：此前 JSON 路径
        # 直接落装载期的重复列 TableSchemaError，文案不可行动。
        _reject_duplicate_columns(cols, "数据集")
        if not isinstance(rows, list) or len(rows) > 5_000_000:
            raise BadRequest("dataset rows must contain at most 5,000,000 records")
        if any(not isinstance(r, list) or len(r) != len(cols) for r in rows):
            raise BadRequest("dataset rows must be rectangular")
        ds_id = f"{reportId}__{raw_id}"
        datasets_meta = list(entry.get("datasets") or [])
        # 重复 id 检查必须在 load_table 之前：load_table 是 DROP+CREATE，先装载
        # 再 400 会把已注册的同名引擎表替换成未注册的上传数据（列形状可能不同）
        # ——manifest/注册表还描述着旧形状，聚合随即 no such column，直到重启。
        if any(d.get("id") == ds_id for d in datasets_meta):
            raise BadRequest("dataset id already exists")
        info = engine.load_table(ds_id, cols, rows)
        label = ds.get("label") or raw_id
        # 重启存活：`_reload_all` 只从注册表条目的 **source 快照** 重载表
        # （source=None 的条目直接跳过）。不落快照的话，运行时新增的数据源在
        # 重启后引擎表消失、注册表条目还在——引用它的图表全部「dataset not
        # loaded」。与 h_dataset_replace 同一条快照持久化契约（含原子落盘 +
        # 失败显式 400：裸 write_text 半途失败会留半份快照，重启 _reload_all
        # 灌进半个表；静默降级则引擎表活着而注册表 source 指向不存在的文件，
        # 重启后数据集蒸发——状态撕裂必须让作者看见，不能吞掉）。
        source_value = None
        data_dir = getattr(registry, "data_dir", None)
        if data_dir:
            from pathlib import Path as _Path
            snap_path = _Path(data_dir) / (_snapshot_stem(ds_id) + ".added.json")
            try:
                # XP-01：按当前快照格式（缺省 sqlite）换后缀并返回实际路径——
                # 回填 snap_path，注册表 source 记的一定是真实存在的文件。
                snap_path = write_dataset_snapshot(snap_path, cols, rows)
            except OSError as exc:
                raise BadRequest(
                    f"新增数据源的快照写盘失败：{exc}。引擎内已有该表，但重启会丢失；"
                    "请处理磁盘/权限问题后重新上传。") from exc
            try:
                source_value = os.path.relpath(snap_path, registry.path.parent)
            except ValueError:
                source_value = str(snap_path)
        datasets_meta.append({"id": ds_id, "label": label, "rowCount": info.row_count,
                              "source": source_value,
                              "dataRevision": f"r{time.time_ns():x}"})
        manifest = entry.get("manifest") or {}
        mds = manifest.get("datasets")
        if not isinstance(mds, list):
            mds = []
            manifest["datasets"] = mds
        mds.append({"id": ds_id, "label": label,
                    "columns": _manifest_typed_columns([str(c) for c in cols], rows,
                                                       declared_types=declared_types),
                    "rowCount": info.row_count})
        entry["datasets"] = datasets_meta
        entry["manifest"] = manifest
        _touch_dataset_revision(entry, ds_id, datasets_meta[-1].get("dataRevision"))
        _register_full_entry(registry, reportId, entry)
        respond_json(_handler, 200, {"datasetId": ds_id, "rowCount": info.row_count,
                                     "columns": [str(c) for c in cols],
                                     "sample": [list(r) for r in rows[:5]]})

    def _process_replace_table(reportId, datasetId, table, mode, key_cols, progress=None):
        return _process_replace_table_impl(engine, registry, reportId, datasetId, table, mode, key_cols, progress)

    def _start_replace_job(reportId, datasetId, table, mode, key_cols, tmp_path=None,
                           trace_id=None):
        """Persist the upload as a durable queue input and return 202.

        The worker payload contains only a state-home-relative filename and
        typed mode/key metadata; raw rows never enter jobs.sqlite JSON.
        """
        import secrets as _secrets
        queue_dir = Path(registry.path.parent) / "queued-replace"
        queue_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        token = _secrets.token_hex(12)
        if tmp_path is not None:
            suffix = Path(tmp_path).suffix or ".upload"
            input_path = queue_dir / f"replace-{token}{suffix}"
            try:
                os.replace(tmp_path, input_path)
            except OSError as exc:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise BadRequest(f"无法持久化异步上传文件：{exc}") from exc
        else:
            input_path = queue_dir / f"replace-{token}.json"
            try:
                # XP-01：按当前快照格式换后缀并返回实际路径——payload 里的
                # inputName 必须是真实落盘文件名（worker 按它读回）。
                input_path = write_dataset_snapshot(input_path,
                                                    (table or {}).get("columns") or [],
                                                    (table or {}).get("rows") or [])
            except OSError as exc:
                raise BadRequest(f"无法持久化异步数据：{exc}") from exc
        payload = {"inputName": input_path.name, "mode": mode,
                   "keyCols": key_cols,
                   # Full replacement is idempotent against the same durable
                   # input; upsert/append are deliberately manual after a
                   # worker loss because their base table may have changed.
                   "recoveryPolicy": "idempotent-replace" if mode == "replace" else "manual"}
        try:
            job_id = _jobs.create("replace", reportId, datasetId, max_running=1,
                                  priority=30, payload=payload, queued=True,
                                  trace_id=trace_id)
        except _jobs.JobConflict as exc:
            # ``tmp_path`` is the request-thread xlsx temp file.  It may have
            # already been moved into the queue, but unlinking the original is
            # still required for the failed-move edge case and is harmless
            # after a successful move.
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            try:
                input_path.unlink(missing_ok=True)
            except OSError:
                pass
            return 409, {"error": {"code": "Conflict",
                "message": "该数据源已有一项进行中的数据更新任务，请等它完成（或失败）后再发起。",
                "jobId": str(exc)}}
        except Exception:
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            try:
                input_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return 202, {"jobId": job_id, "mode": mode, "datasetId": datasetId,
                     "status": "queued", "retryToken": _jobs.retry_token(job_id)}

    def h_dataset_replace(_handler, reportId, datasetId):
        """Replace/merge/append an existing dataset's rows in place (真持久化).

        The browser cannot do this itself in server mode: the manifest is
        schema-only, so the old ``applyUpdatedDataset`` only mutated an
        in-memory ``ds.rows`` and then told the user "数据已更新" while the
        charts kept reading the (unchanged) server table. This endpoint loads
        the uploaded table under the SAME table id (DROP+CREATE), re-applies
        the dataset's 计算列 declarations and refreshes the row count.

        Body (JSON): ``{password, table:{columns, rows}, mode?, keyCols?}``. Column
        names must match the current table as a set (a different layout needs
        the column-mapping wizard instead of a silent re-shape).

        Body (raw, 2026-09-18 大增量直传): ``Content-Type: text/csv | text/tab-separated-values``
        + 原始文件字节，``?mode=&keyCols=`` 查询串 + ``X-Edit-Password`` 请求头——
        浏览器零解析零 stringify，服务端 Python csv 解析后走同一条校验/合并管线。
        2026-09-21 起 ``application/octet-stream``（xlsx，``&suffix=`` 传扩展名）
        同收：落临时文件走编译侧同一 ``load_table_file``（与 h_dataset_add 的 raw
        分支同构）——此前 .xlsx 被当 CSV 解析二进制，报解析器内部错误不可行动。

        ``mode``（缺省 "replace"，向后兼容）：
        - ``replace`` 全量替换 —— 上传行集就是新表；
        - ``upsert``   增量合并 —— 按主键 ``keyCols`` 并进现有行（有则覆盖、
          无则插入）。合并算术唯一真源在 ``scripts/upsert_merge.py``（JS 镜像
          与 fixture parity 见该模块 docstring）；守卫（基表主键不唯一 / 增量
          重复 / 主键空值 / 列集不一致）fail-closed 抛可行动错误，不改任何数据。
        - ``append``   追加新行（2026-09-22）—— 不查重不覆盖，免选主键：同构
          文件补新一期数据时增量往往 100% 是新行，upsert 主键选择是纯仪式
          （W35/W36 真机复盘）。重复导入同一文件会产生重复行。

        ``async=1``（2026-09-22，仅 raw 直传形态）：重管道（xlsx 解析 → 合并 →
        装载 → 快照）在后台 job 线程执行，POST 立即 ``202 {jobId}``；客户端轮询
        ``GET …/replace/jobs/:jobId``（同一 edit 口令闸）。没有它，29 万行的
        xlsx 在请求线程里跑几分钟，公司内网代理掐线后客户端永远不知道结果
        （真机 W35 导入静默失败）。任务只在内存：服务重启后轮询 404，客户端
        必须如实呈现「状态未知」，绝不轮询出假成功。
        """
        entry = registry.get(reportId)
        if entry is None:
            raise NotFound(f"report {reportId!r} not registered")
        # 双形态入参（2026-09-18 大增量直传）：JSON（原路径，向后兼容）或**原始
        # CSV/TSV**——几十 MB 的增量不再让浏览器 parse+stringify 卡主线程，文件
        # 字节直接 POST，由服务端 Python csv 解析。直传形态 mode/keyCols 走查询
        # 串，口令走 X-Edit-Password 请求头（不进 URL，不落访问日志）。
        ctype = (_handler.headers.get("Content-Type") or "").strip().lower()
        if ctype.startswith(("text/csv", "text/tab-separated-values")) \
                or "spreadsheetml" in ctype or ctype.startswith("application/octet-stream"):
            gate = check_edit_gate({"password": _handler.headers.get("X-Edit-Password") or ""},
                                   edit_policy_of(entry))
            if gate:
                respond_json(_handler, 403, {"error": {"code": "Forbidden", "message": gate}})
                return
            qs = parse_qs(urlparse(_handler.path).query)
            mode = (qs.get("mode") or ["upsert"])[0]
            want_async = (qs.get("async") or [""])[0] == "1"
            key_cols = [c for c in ((qs.get("keyCols") or [""])[0]).split(",") if c.strip()] or None
            if mode not in ("replace", "upsert", "append"):
                raise BadRequest("mode must be 'replace', 'upsert' or 'append'")
            if mode == "upsert" and (not isinstance(key_cols, list) or not key_cols):
                raise BadRequest("mode=upsert requires keyCols (non-empty string list)")
            if want_async:
                # 同一数据源同时只允许一个写任务（并发写同一张 SQLite 表无意义
                # 且危险）；第二个发起 409，任务条会显示既有任务的结果。
                dup = _jobs.active_for(reportId, datasetId)
                if dup:
                    respond_json(_handler, 409, {"error": {"code": "Conflict",
                        "message": "该数据源已有一项进行中的数据更新任务，请等它完成（或失败）后再发起。"}})
                    return
            if ctype.startswith(("text/csv", "text/tab-separated-values")):
                table = _parse_delimited_table(_handler)
                if want_async:
                    status, payload = _start_replace_job(
                        reportId, datasetId, table, mode, key_cols,
                        trace_id=getattr(_handler, "trace_id", None))
                    respond_json(_handler, status, payload)
                    return
            else:
                # xlsx 直传（2026-09-21，与 h_dataset_add 的 raw 分支同构）：此前
                # 前端把 .xlsx 按 text/csv 发、服务端按 csv 解析二进制 → 解析器
                # 内部错误「new-line character seen in unquoted field」，完全不可
                # 行动。落临时文件走编译侧同一 load_table_file（编码回退/直通修复
                # 同一真源）。suffix 只作临时文件扩展名，形状不对收敛缺省 .xlsx。
                suffix = (qs.get("suffix") or [""])[0].strip()
                if not re.fullmatch(r"\.[A-Za-z0-9]{1,9}", suffix):
                    suffix = ".xlsx"
                raw = _read_body_bytes(_handler)
                import tempfile as _tempfile
                fd, tmp = _tempfile.mkstemp(suffix=suffix)
                os.close(fd)
                with open(tmp, "wb") as tf:
                    tf.write(raw)
                if want_async:
                    # 重解析交给 job；临时文件由 job 的 finally 删除。
                    status, payload = _start_replace_job(
                        reportId, datasetId, None, mode, key_cols, tmp_path=tmp,
                        trace_id=getattr(_handler, "trace_id", None))
                    respond_json(_handler, status, payload)
                    return
                try:
                    from table_input import load_table_file as _load_table_file
                    try:
                        loaded = _load_table_file(tmp)
                    except Exception as exc:
                        raise BadRequest(f"failed to parse uploaded file: {exc}")
                    table = {"columns": [str(c) for c in loaded.get("columns") or []],
                             "rows": [list(r) for r in loaded.get("rows") or []]}
                finally:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
        else:
            body = read_json(_handler)
            gate = check_edit_gate(body, edit_policy_of(entry))
            if gate:
                respond_json(_handler, 403, {"error": {"code": "Forbidden", "message": gate}})
                return
            mode = body.get("mode") or "replace"
            key_cols = body.get("keyCols")
            table = body.get("table") or {}
        if mode not in ("replace", "upsert", "append"):
            raise BadRequest("mode must be 'replace', 'upsert' or 'append'")
        if mode == "upsert":
            if not isinstance(key_cols, list) or not key_cols \
                    or not all(isinstance(c, str) and c.strip() for c in key_cols):
                raise BadRequest("mode=upsert requires keyCols (non-empty string list)")
            key_cols = [str(c) for c in key_cols]
        else:
            # replace / append 都不带主键——append 免选主键（2026-09-22）。
            key_cols = None
        # 同步路径同样预检在跑的异步任务（async 分支的 active_for 同一闸）：
        # 否则一个 async 直传在后台重装这张表时，同步 replace 可以并发
        # DROP+CREATE 同一张 SQLite 表 + 注册表 read-modify-write 互相覆盖
        # （丢更新）。此处只是预检——同步请求不登记 job，TOCTOU 窗口仍存在，
        # 但「明知道有任务在跑还并发写」的主路径被堵死。
        dup = _jobs.active_for(reportId, datasetId)
        if dup:
            respond_json(_handler, 409, {"error": {"code": "Conflict",
                "message": "该数据源有异步导入进行中，请等任务完成（或失败）后再发起。"}})
            return
        respond_json(_handler, 200,
                     _process_replace_table(reportId, datasetId, table, mode, key_cols))

    def h_dataset_replace_job(_handler, reportId, datasetId, jobId):
        """轮询数据更新后台任务（2026-09-22 异步化配套，与 replace 同一 edit 闸）。

        任务元数据写在 state home 的 jobs.sqlite3；服务重启后仍可查到已完成任务，
        但确认 owner 已退出的 running 任务会变为 lost，绝不自动重放数据写入。
        真正不存在或已过期才返回 404，客户端必须如实呈现状态，绝不能把 404
        轮询成假成功。
        """
        entry = registry.get(reportId)
        if entry is None:
            raise NotFound(f"report {reportId!r} not registered")
        gate = check_edit_gate({"password": _handler.headers.get("X-Edit-Password") or ""},
                               edit_policy_of(entry))
        if gate:
            respond_json(_handler, 403, {"error": {"code": "Forbidden", "message": gate}})
            return
        job = _jobs.get(jobId)
        if job is None or job["reportId"] != reportId or job["datasetId"] != datasetId:
            respond_json(_handler, 404, {"error": {"code": "NotFound",
                "message": "任务不存在或已过期，请用数据源配置里的行数确认数据是否已更新。"}})
            return
        payload = {"id": job["id"], "status": job["status"], "phase": job["phase"],
                   "startedAt": job["startedAt"], "finishedAt": job["finishedAt"],
                   "pollAfterMs": 1200 if job["status"] == "running" else 0,
                   "canRecover": bool(job.get("canRecover")),
                   "retryToken": _jobs.retry_token(jobId)}
        if job["result"] is not None:
            payload["result"] = job["result"]
        if job["error"]:
            payload["error"] = job["error"]
        respond_json(_handler, 200, payload)

    def h_dataset_replace_job_cancel(_handler, reportId, datasetId, jobId):
        """Cooperatively cancel a queued/running replace without claiming rollback."""
        entry = registry.get(reportId)
        if entry is None:
            raise NotFound(f"report {reportId!r} not registered")
        body = read_json(_handler)
        gate = check_edit_gate({"password": body.get("password") or
                                _handler.headers.get("X-Edit-Password") or ""},
                               edit_policy_of(entry))
        if gate:
            respond_json(_handler, 403, {"error": {"code": "Forbidden", "message": gate}})
            return
        job = _jobs.get(jobId)
        if job is None or job.get("kind") != "replace" or job.get("reportId") != reportId \
                or job.get("datasetId") != datasetId:
            raise NotFound("数据更新任务不存在或已过期")
        retry = body.get("retryToken") if isinstance(body, dict) else None
        if retry and not _jobs.verify_retry_token(jobId, str(retry)):
            respond_json(_handler, 403, {"error": {"code": "Forbidden", "message": "任务 retry token 无效"}})
            return
        accepted = _jobs.cancel(jobId, "数据更新任务已取消")
        current = _jobs.get(jobId) or job
        if current.get("status") == "cancelled":
            # A queued replace has not reached the worker's ``finally`` block,
            # so reclaim its 0600 input here. This is resource cleanup only:
            # Flint's caller-owned aggregation/data contract is untouched
            # (refs/flint/src/core/aggregate.ts:7-20), and cancellation never
            # claims to roll back a running table mutation.
            _cleanup_queued_replace_input(registry, current)
        respond_json(_handler, 200, {"ok": accepted, "status": current.get("status"),
                                     "phase": current.get("phase")})

    def h_dataset_replace_job_recover(_handler, reportId, datasetId, jobId):
        """安全重排仅限全量 replace；upsert/append 丢失后必须人工重发。"""
        entry = registry.get(reportId)
        if entry is None:
            raise NotFound(f"report {reportId!r} not registered")
        body = read_json(_handler)
        gate = check_edit_gate({"password": body.get("password") or
                                _handler.headers.get("X-Edit-Password") or ""},
                               edit_policy_of(entry))
        if gate:
            respond_json(_handler, 403, {"error": {"code": "Forbidden", "message": gate}})
            return
        job = _jobs.get(jobId)
        if job is None or job.get("kind") != "replace" or job.get("reportId") != reportId \
                or job.get("datasetId") != datasetId:
            raise NotFound("数据更新任务不存在或已过期")
        token = body.get("retryToken") if isinstance(body, dict) else None
        if not token:
            respond_json(_handler, 403, {"error": {"code": "Forbidden",
                "message": "重试 lost 任务必须提供 retry token"}})
            return
        try:
            recovered = _jobs.recover_lost(jobId, str(token))
        except _jobs.JobRecoveryDenied as exc:
            respond_json(_handler, 403, {"error": {"code": "Forbidden", "message": str(exc)}})
            return
        except _jobs.JobConflict as exc:
            respond_json(_handler, 409, {"error": {"code": "Conflict",
                "message": "同一数据源已有任务进行中，请先等待或取消它。", "jobId": str(exc)}})
            return
        respond_json(_handler, 202, {"jobId": jobId, "status": recovered.get("status"),
                                     "phase": recovered.get("phase"),
                                     "retryToken": _jobs.retry_token(jobId), "recovered": True})

    def h_dataset_materialize(_handler, reportId, datasetId):
        """数据源配置保存后在线物化新计算列/维度值分组（2026-09-18 自动刷新闭环）。

        The canvas used to say 「待构建」in server mode: a just-declared row-level
        derived column existed only in the browser until a full rebuild, so any
        existing card referencing it failed with ``no such column``. The server
        already holds the live SQLite table and the single math source
        (``scripts/derived_eval`` via ``ensure_table_defs`` / ``ensure_dim_groups``,
        the same functions ``replay_all`` runs on boot/reload) — so the editor now
        calls THIS endpoint right after the canvas saves.

        Body: ``{password, columns?: [defs], dimGroups?: [defs]}`` — the client's
        current declarations for the dataset. The handler unions them with the
        registry (shared-table rule: several report copies may declare columns
        for the SAME table id), materialises only the MISSING columns (idempotent;
        a broken declaration raises → 400, data unchanged), then persists the
        declarations into the registry entry (datasets meta + served manifest, so
        ``replay_all`` keeps them across restarts and a page reload sees the new
        columns). Deleting/renaming declarations is NOT handled here — physical
        columns are never dropped (retiredColumns stays the replace endpoint's
        business).

        Gate = the same ``check_edit_gate`` policy as add/replace/save: a report
        without an edit password cannot auto-materialize (client skips silently).
        """
        entry = registry.get(reportId)
        if entry is None:
            raise NotFound(f"report {reportId!r} not registered")
        body = read_json(_handler)
        gate = check_edit_gate(body, edit_policy_of(entry))
        if gate:
            respond_json(_handler, 403, {"error": {"code": "Forbidden", "message": gate}})
            return
        meta = next((d for d in (entry.get("datasets") or [])
                     if isinstance(d, dict) and d.get("id") == datasetId), None)
        if meta is None:
            raise NotFound(f"dataset {datasetId!r} not in report {reportId!r}")
        cols_body = body.get("columns")
        groups_body = body.get("dimGroups")
        if cols_body is not None:
            if not isinstance(cols_body, list) or \
                    not all(isinstance(d, dict) and str(d.get("name") or "").strip() for d in cols_body):
                raise BadRequest("columns must be a list of {name, op, left, right} declarations")
        if groups_body is not None:
            if not isinstance(groups_body, list) or \
                    not all(isinstance(d, dict) and d.get("name") and d.get("source") for d in groups_body):
                raise BadRequest("dimGroups must be a list of {name, source, groups} declarations")
        # 并集 = 注册表既有声明 ∪ 本次请求（extra）。副本共享表：另一份副本声明过、
        # 本条目还没记录的列也一并补齐（与 h_dataset_replace 的比对口径同一规则）。
        col_defs = table_defs(registry.raw_entries(), datasetId,
                              extra=({"kind": DERIVED_KIND, "defs": cols_body} if cols_body else None))
        grp_defs = dim_group_table_defs(registry.raw_entries(), datasetId,
                                        extra=groups_body if groups_body else None)
        if not col_defs and not grp_defs:
            raise BadRequest("nothing to materialize: provide columns[] or dimGroups[]")
        # 索引列（h_register/h_dataset_replace 写进 meta）重建时带回，索引不因
        # 物化重建表而消失——与 replay_all 同一条 union 口径。
        idx_cols: list[str] = []
        for e2 in registry.raw_entries():
            if not isinstance(e2, dict):
                continue
            for d2 in (e2.get("datasets") or []):
                if isinstance(d2, dict) and d2.get("id") == datasetId:
                    for c in (d2.get("indexColumns") or []):
                        if c not in idx_cols:
                            idx_cols.append(c)
        info_before = engine.table_info(datasetId)
        have_before = set(info_before.columns) if info_before is not None else set()
        try:
            added_cols = ensure_table_defs(engine, datasetId, col_defs, index_columns=idx_cols)
            added_groups = ensure_dim_groups(engine, datasetId, grp_defs, index_columns=idx_cols)
        except Exception as e:  # noqa: BLE001  (声明算错必须 400 说人话，不是 500)
            raise BadRequest(f"materialize failed: {e}") from e
        info_after = engine.table_info(datasetId)
        new_names = [c for c in (info_after.columns if info_after else []) if c not in have_before]
        col_names = {str(d.get("name")) for d in col_defs}
        grp_names = {str(d.get("name")) for d in grp_defs}
        # 声明持久化：datasets meta（重启 replay 扫这里）+ 服务 manifest（页面重载
        # 后客户端 ds.columns 才有新列；derived.measures 必须保留——聚合级派生指标
        # 是客户端求值的活数据，清掉就丢）。
        if cols_body is not None:
            meta["derived"] = {"kind": DERIVED_KIND, "defs": [dict(d) for d in cols_body]}
        if groups_body is not None:
            meta["dimGroups"] = [dict(g) for g in groups_body]
        manifest = entry.get("manifest") or {}
        for d in (manifest.get("datasets") or []):
            if not (isinstance(d, dict) and d.get("id") == datasetId):
                continue
            have = {c.get("name") if isinstance(c, dict) else str(c) for c in (d.get("columns") or [])}
            for name in new_names:
                if name in have:
                    continue
                ctype = "number" if name in col_names else "category"
                d.setdefault("columns", []).append({"name": name, "type": ctype})
            if cols_body is not None:
                slot = dict(d.get("derived") or {})
                slot["columns"] = [dict(x) for x in cols_body]
                d["derived"] = slot
            if groups_body is not None:
                d["dimGroups"] = [dict(g) for g in groups_body]
        entry["manifest"] = manifest
        registry.register(reportId, entry)
        respond_json(_handler, 200, {"ok": True, "datasetId": datasetId,
                                     "addedColumns": added_cols, "addedDimGroups": added_groups,
                                     "columns": (info_after.columns if info_after else [])})

    def _materialize_relation_core(reportId: str, rel: dict) -> dict[str, Any]:
        """合并关系 → 物化宽表（h_relation_materialize 与行删除后的自动重物化
        共用的唯一实现；端点壳只做 HTTP 闸与校验）。

        幂等：重复调用替换同一宽表与注册表记账（重合并是常规操作）。返回响应
        payload（含 datasetId/label/rowCount），失败抛 KeyError/ValueError/聚合错。
        """
        entry = registry.get(reportId)
        if entry is None:
            raise NotFound(f"report {reportId!r} not registered")
        chain = [t for t in (rel.get("tables") or []) if isinstance(t, str)]
        from .registry import (derived_entry, load_relation, relation_wide_semantics,
                               wide_label, wide_manifest_dataset)
        try:
            # The merge runs under the report-namespace-free identity of each table
            # so its column NAMES equal what a build-time merge produces.
            ds_id, info, merged = load_relation(engine, rel, reportId)
        except KeyError as e:  # a chain table vanished mid-flight
            raise BadRequest(f"dataset not loaded: {e}")
        except ValueError as e:
            raise BadRequest(f"merge failed: {e}")
        label = wide_label(rel)
        datasets_meta = [d for d in (entry.get("datasets") or []) if d.get("id") != ds_id]
        datasets_meta.append(derived_entry(ds_id, rel, info.row_count))
        entry["datasets"] = datasets_meta
        manifest = entry.get("manifest") or {}
        # Per-column types/roles carried over from the SOURCE columns (provenance),
        # so a merged 月份 column is born 时间 instead of a bare 维度. Without this
        # the browser has no rows to infer from and types every column as 维度.
        semantics = relation_wide_semantics(merged, entry, rel, reportId)
        wide_ds = wide_manifest_dataset(ds_id, label, merged, info.row_count, semantics)
        mds = [d for d in (manifest.get("datasets") or []) if d.get("id") != ds_id]
        prev = [d for d in (manifest.get("datasets") or []) if d.get("id") == ds_id]
        if prev:
            # Preserve roles the user set in the editor for columns that still exist.
            for key in ("columnTypes", "fieldRoles"):
                cur = dict((prev[0].get(key) or {}))
                cur.update(wide_ds.get(key) or {})
                if cur:
                    wide_ds[key] = cur
        mds.append(wide_ds)
        manifest["datasets"] = mds
        # Keep the relation spec on the manifest so the runtime can re-open the
        # merge editor and a future rebuild knows what produced this table.
        rels = [r for r in (manifest.get("relations") or []) if r.get("id") != rel.get("id")]
        rels.append(rel)
        manifest["relations"] = rels
        entry["manifest"] = manifest
        registry.register(reportId, entry)
        return {"datasetId": ds_id, "label": label, "columns": wide_ds["columns"],
                "columnTypes": wide_ds.get("columnTypes") or {},
                "fieldRoles": wide_ds.get("fieldRoles") or {},
                "rowCount": info.row_count}

    def h_relation_materialize(_handler, reportId):
        """Materialize a runtime-authored relation into a REAL wide dataset.

        The client cannot merge in server mode (the manifest is schema-only),
        but the server holds every source table in SQLite — so merge here and
        load the result as a table the charts can query immediately. Uses the
        SAME merge function as the build-time pre-pass (server/relations.py →
        scripts/merge_tables.py), so a later rebuild reproduces it exactly.

        Idempotent: re-POSTing the same relation replaces the wide table and
        the registry entry (a re-merge after an edit is a normal operation)."""
        entry = registry.get(reportId)
        if entry is None:
            raise NotFound(f"report {reportId!r} not registered")
        body = read_json(_handler)
        # 物化是**变更操作**（写注册表 + 改 manifest datasets/relations + 装载
        # 引擎表）——与 add/replace/save 走同一编辑策略，管理员关掉在线编辑后
        # 它不能成为绕行口（check_edit_gate 契约：任何端点都改不了被锁的报告）。
        gate = check_edit_gate(body, edit_policy_of(entry))
        if gate:
            respond_json(_handler, 403, {"error": {"code": "Forbidden", "message": gate}})
            return
        rel = body.get("relation")
        if not isinstance(rel, dict):
            raise BadRequest("relation (object) required")
        chain = [t for t in (rel.get("tables") or []) if isinstance(t, str)]
        if len(chain) < 2:
            raise BadRequest("relation.tables must contain >=2 datasetIds")
        missing = [t for t in chain if engine.table_info(t) is None]
        if missing:
            raise BadRequest(
                f"dataset(s) not loaded on this server: {missing}; "
                f"upload them first (POST /api/reports/{{id}}/datasets)"
            )
        # Same rules as a build-time request — never a second validator.
        from validate_request import validate_relation
        try:
            validate_relation(rel, chain)
        except Exception as e:  # RequestValidationError
            raise BadRequest(f"relation invalid: {e}")
        respond_json(_handler, 200, _materialize_relation_core(reportId, rel))

    def h_list_reports(_handler):
        out = []
        for entry in registry.raw_entries():   # 只读少量字段：不做 list() 的大对象深拷贝
            out.append({
                "id": entry.get("reportId"),
                "title": entry.get("title"),
                "datasetIds": [d["id"] for d in entry.get("datasets", [])],
                "chartCount": len(entry.get("manifest", {}).get("charts", [])),
                "mode": entry.get("manifest", {}).get("mode", "server"),
                "createdAt": entry.get("registeredAt"),
            })
        respond_json(_handler, 200, {"reports": out})

    def h_report_impact_graph(handler, reportId):
        """Return a bounded, read-only cross-report/card dependency graph."""
        if not _copilot_origin(handler):
            return
        if not _agent_auth(handler, reportId, "viewer"):
            return
        entry = registry.get(reportId)
        if entry is None:
            raise NotFound(f"report {reportId!r} not registered")
        query = parse_qs(urlparse(str(getattr(handler, "path", ""))).query)
        chart_id = (query.get("chartId") or [None])[0]
        tenant = _report_tenant_id(entry)
        # Tenant filtering is server-owned.  A missing tenant is the legacy
        # local-report scope and must not be mixed with explicitly scoped data.
        reports = []
        for candidate in registry.raw_entries():
            if _report_tenant_id(candidate) != tenant:
                continue
            reports.append(candidate)
        from .agent_extensions import build_report_impact_graph
        result = build_report_impact_graph(
            reports,
            target={"reportId": reportId, "chartId": chart_id} if chart_id else None,
        )
        respond_json(handler, 200, result)

    def h_get_report(_handler, reportId):
        entry = registry.get(reportId)
        if entry is None:
            raise NotFound(f"report {reportId!r} not registered")
        manifest = entry.get("manifest", {})
        # server mode: strip any rows from datasets / editor.data before sending
        manifest = _server_strip(manifest)
        respond_json(_handler, 200, manifest)

    def h_chart_aggregate(_handler, reportId, chartId):
        entry = registry.get(reportId)
        if entry is None:
            raise NotFound(f"report {reportId!r} not registered")
        chart = _find_chart(entry, chartId)
        if chart is None:
            raise NotFound(f"chart {chartId!r} not in report {reportId!r}")
        if handler_is_post(_handler):
            body = read_json(_handler)
            data = body.get("data") or body
            # 图表缺 editor.data（历史/手写 manifest）→ 400 可行动报错，不是裸 KeyError 500。
            _chart_data = data if body.get("data") else (chart.get("editor") or {}).get("data")
            if not isinstance(_chart_data, dict) or not _chart_data.get("datasetId"):
                raise BadRequest(f"chart {chartId!r} has no editor.data (datasetId unknown); 请重新构建或补全 manifest")
            _ds_id = _chart_data.get("datasetId")
            payload = _chart_request_payload(chart, data)
            # S2 数据面闸（XU-04）：图表聚合引用敏感列 → 400（含派生指标依赖展开
            # 后的列）；无声明 = 零漂移。
            assert_aggregate_privacy(payload, dataset_sensitive_cols(registry, _ds_id),
                                     f"报告 {reportId} 图表 {chartId} 的聚合查询")
            request = parse_request(payload)
            response = aggregate(engine, _ds_id, request)
        else:
            _chart_data = (chart.get("editor") or {}).get("data")
            if not isinstance(_chart_data, dict) or not _chart_data.get("datasetId"):
                raise BadRequest(f"chart {chartId!r} has no editor.data (datasetId unknown); 请重新构建或补全 manifest")
            _ds_id = _chart_data.get("datasetId")
            payload = _chart_request_payload(chart, _chart_data)
            assert_aggregate_privacy(payload, dataset_sensitive_cols(registry, _ds_id),
                                     f"报告 {reportId} 图表 {chartId} 的聚合查询")
            request = parse_request(payload)
            response = aggregate(engine, _ds_id, request)
        respond_json(_handler, 200, response.to_dict())

    def h_chart_truth(_handler, reportId, chartId):
        entry = registry.get(reportId)
        if entry is None:
            raise NotFound(f"report {reportId!r} not registered")
        chart = _find_chart(entry, chartId)
        if chart is None:
            raise NotFound(f"chart {chartId!r} not in report {reportId!r}")
        body = read_json(_handler)
        try:
            tolerance = float(body.get("tolerance", 0.001))
        except (TypeError, ValueError):
            raise BadRequest("tolerance must be a number") from None
        truth = chart.get("truth")
        if not truth:
            respond_json(_handler, 200, {"chartId": chartId, "results": [], "allOk": True,
                                        "tolerance": tolerance, "skipped": "truth has no mapping"})
            return
        ds_id = truth.get("dataset")
        if not ds_id:
            ed = ((chart.get("editor") or {}).get("data") or {})
            ds_id = ed.get("datasetId")
        if not ds_id:
            raise BadRequest("chart has no dataset to validate truth against")
        info = engine.table_info(ds_id)
        if info is None:
            raise NotFound(f"dataset {ds_id!r} not loaded")
        # S2 数据面闸（XU-04）：真值校验会按映射列重算并回显 recomputed 值——
        # 映射/维度引用敏感列 → 400（否则敏感列的聚合读数从 results 泄漏）。
        _s2_sensitive = dataset_sensitive_cols(registry, ds_id)
        if _s2_sensitive:
            _s2_truth_cols = [truth.get("dimension")] + [
                (src.get("column", rc) if isinstance(src, dict) else src)
                for rc, src in (truth.get("mapping") or {}).items()]
            assert_not_sensitive(_s2_truth_cols, _s2_sensitive,
                                 f"报告 {reportId} 图表 {chartId} 的数据真值校验")
        declared = _extract_declared_series(chart)
        result = validate_truth(engine, ds_id, truth, declared, tolerance)
        respond_json(_handler, 200, {"chartId": chartId, **result})

    def h_reload_report(_handler, reportId):
        # 全量重载会把磁盘快照重灌进引擎（DROP+CREATE）——报告主人本机的管理
        # 操作，与 h_reload_registry/h_register 同一门禁（安全 P1：补上 TCP 对端
        # 门 + 同源守卫）。浏览器运行时从不调用它；调用方是本机构建工具
        # （serve_report / report_build），不带 Origin，照常放行。
        if not is_loopback(_handler) or not _same_origin_guard(_handler):
            respond_json(_handler, 403, {"error": {"code": "Forbidden",
                                                   "message": "仅能在运行报告的本机重载数据"}})
            return
        entry = registry.get(reportId)
        if entry is None:
            raise NotFound(f"report {reportId!r} not registered")
        reloaded = []
        for d in entry.get("datasets", []):
            if _is_derived_ds(d):
                continue
            info = _reload_dataset(engine, d, registry)
            if info:
                reloaded.append({"id": info.name, "rowCount": info.row_count})
        # Derived wide tables depend on the source tables, so they replay AFTER
        # the ordinary pass (two-pass: a derived entry listed before its
        # sources would otherwise merge against nothing). Registry-declared
        # 计算列 ALSO replay across ALL entries here — the reload of one report
        # must not drop a column another report (sharing the table id) declares.
        from .registry import replay_entry
        for _ds in replay_columns(engine, registry, verbose=True):
            reloaded.append({"id": _ds, "derivedColumns": True})
        for item in replay_entry(engine, entry, verbose=True):
            reloaded.append(item)
        registry.register(reportId, entry)
        respond_json(_handler, 200, {"reportId": reportId, "datasets": reloaded})

    def h_update_manifest(_handler, reportId):
        """Persist an edited whole manifest back to the served report.

        Gate: the report must declare an edit password (manifest.runtime
        .editPassword set at build). Overwrite updates the SAME reportId so
        everyone who opens the (shared) link sees the new version; copy
        registers a sibling reportId (``reportId-v2``…) leaving the original
        untouched. Request body: {password, manifest, as: 'overwrite'|'copy'}."""
        # 同源守卫（安全 P1）：LAN 作者带对口令经局域网地址保存仍放行（Origin ==
        # Host 精确同源）；恶意页的跨站 Origin 一律 403。口令闸在后面另行生效。
        if not _same_origin_guard(_handler):
            respond_json(_handler, 403, {"error": {"code": "Forbidden",
                                                   "message": "跨站请求已拒绝"}})
            return
        entry = registry.get(reportId)
        if entry is None:
            raise NotFound(f"report {reportId!r} not registered")
        body = read_json(_handler)
        password = body.get("password") or ""
        incoming = body.get("manifest")
        policy = edit_policy_of(entry)
        if not policy["enabled"]:
            respond_json(_handler, 403, {"error": {"code": "Forbidden",
                                                   "message": EDIT_DISABLED_MESSAGE + "（管理员已关闭本报告的在线编辑）"}})
            return
        expected_pw = policy["password"]
        if not expected_pw:
            respond_json(_handler, 403, {"error": {"code": "Forbidden",
                                                   "message": EDIT_DISABLED_MESSAGE + "（本报告尚未设置编辑口令）"}})
            return
        import hmac
        if not hmac.compare_digest(str(password).encode("utf-8"), str(expected_pw).encode("utf-8")):  # encode：同 check_edit_gate，非 ASCII 口令
            respond_json(_handler, 403, {"error": {"code": "Forbidden", "message": "编辑口令错误"}})
            return
        if not isinstance(incoming, dict) or (not incoming.get("charts") and not incoming.get("draft")):
            # draft=true（拼版草稿，compose-canvas）是零图表 manifest 的唯一通行
            # 证：草稿的整条工作流就是在零块画布上攒占位块并逐次 syncManifest。
            # 非 draft manifest 仍要求 charts 非空（零声明必须走请求层闸门）。
            raise BadRequest("manifest must be an object with charts")
        security = incoming.get("security") if isinstance(incoming.get("security"), dict) else {}
        tenant_values = [_validate_tenant_id(value) for value in
                         (incoming.get("tenantId"), security.get("tenantId"))
                         if value not in (None, "")]
        if tenant_values and any(value != tenant_values[0] for value in tenant_values[1:]):
            raise BadRequest("tenantId declarations must agree")
        current_tenant = _report_tenant_id(entry)
        if current_tenant and tenant_values and tenant_values[0] != current_tenant:
            raise Conflict("报告 tenantId 不可在在线保存中切换")
        # themeShells.vars 键形状校验（安全 P2）：非 CSS 自定义属性键在落盘前丢弃。
        _sanitize_theme_vars(incoming)
        # pageWidth 在 template.render 里走 resolve_page_width（非法值抛错）——
        # 不在这里拦住，坏值落盘后每次 GET /reports/<id> 都 500，报告永久打不开
        # （重启也救不回来）。保存时给可行动 400，与请求层「绝不静默回退」同口径。
        try:
            from validate_request import resolve_page_width
            resolve_page_width(incoming.get("pageWidth"))
        except Exception as exc:
            raise BadRequest(f"manifest.pageWidth invalid: {exc}")
        stored_body = _sanitize_body_html(body.get("bodyHtml")) if isinstance(body.get("bodyHtml"), str) else None
        # 保存下来的 manifest 必须带当前策略：副本靠它继承同一口令/开关状态。
        _apply_policy_to_manifest(incoming, policy)
        as_mode = body.get("as") or "overwrite"
        if as_mode == "overwrite":
            incoming["reportId"] = reportId
            new_entry = dict(entry)
            new_entry.pop("bodyHtml", None)
            if stored_body:
                new_entry["bodyHtml"] = stored_body
            new_entry["manifest"] = incoming
            new_entry["title"] = incoming.get("title") or entry.get("title")
            new_entry.setdefault("family", entry.get("family") or _family_of(reportId))
            new_entry["edit"] = edit_policy_of({"manifest": incoming})
            # A relation edited away in the browser must not leave an orphan
            # wide table behind on the server (the runtime drops it locally via
            # dropRelationAt). Derived datasets are keyed by relation id.
            dropped = _prune_derived_datasets(engine, new_entry, incoming, registry)
            # 数据集「计算列」的声明以 manifest 为真源：把它同步进注册表条目（重载
            # 靠它重放），并立即对齐一次，让刚声明的列在服务端马上可用（列只增不减——
            # 表可能被多份报告共享，不能因本份删了声明就抹掉别份的列）。
            _sync_derived_columns(new_entry, incoming)
            log = list(entry.get("changelog") or [])
            log.append(_change_entry("overwrite", reportId, new_entry["title"], incoming))
            new_entry["changelog"] = log
            registry.register(reportId, new_entry)
            # register FIRST so the union replay sees this report's new declaration
            replay_columns(engine, registry)
            respond_json(_handler, 200, {"ok": True, "reportId": reportId, "mode": "overwrite",
                                        "droppedDatasets": dropped})
        elif as_mode == "copy":
            family = entry.get("family") or _family_of(reportId)
            copy_name = (body.get("copyName") or "").strip()
            if copy_name:
                if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,79}", copy_name):
                    raise BadRequest("copyName must match ^[A-Za-z][A-Za-z0-9_-]{0,79}$ (English letters/digits/-/_)")
                new_id = f"{family}-{copy_name}"
                if registry.get(new_id) is not None:
                    raise Conflict(f"a report named {new_id!r} already exists; choose a different name")
            else:
                new_id = None
                for n in range(2, 1000):
                    cand = f"{family}-v{n}"
                    if registry.get(cand) is None:
                        new_id = cand
                        break
                if new_id is None:
                    raise BadRequest("too many copies")
            incoming["reportId"] = new_id
            new_title = incoming.get("title") or entry.get("title")
            # Inherit the family lineage + changelog so a version's log shows its
            # full history, then append this copy event.
            inherited_log = list(entry.get("changelog") or [])
            inherited_log.append(_change_entry("copy", new_id, new_title, incoming, source=reportId))
            new_entry = {
                "title": new_title,
                "manifest": incoming,
                "datasets": [dict(d) for d in entry.get("datasets", [])],
                "family": family,
                "changelog": inherited_log,
                # 与覆盖分支（new_entry["edit"] = edit_policy_of(...)）同一条纪律：
                # 副本靠 entry.edit 继承口令/开关状态；缺了会回落 manifest.runtime
                # （行为碰巧等价，但两条分支不对称，注册表口径分裂）。
                "edit": edit_policy_of({"manifest": incoming}),
            }
            if stored_body:
                new_entry["bodyHtml"] = stored_body
            # 与覆盖分支同一条纪律：本会话声明/删除的计算列、关系宽表也要在副本上
            # 对齐——否则副本 manifest 引用的列引擎里没有（400 no such column），
            # 注册表又没有声明，重启 replay 也补不上。
            _prune_derived_datasets(engine, new_entry, incoming, registry)
            _sync_derived_columns(new_entry, incoming)
            registry.register(new_id, new_entry)
            replay_columns(engine, registry)
            respond_json(_handler, 200, {"ok": True, "reportId": new_id, "mode": "copy"})
        else:
            raise BadRequest("as must be 'overwrite' or 'copy'")

    def h_create_draft(_handler, reportId):
        """空白拼版草稿（compose-canvas，docs/compose-canvas-design.md）。

        草稿 = 注册表里的一个条目（manifest.draft=true、blocks=[]、零图表），与
        「副本」同模型：不新建报告目录、不起第二个 server，随本 server 的注册表
        持久化。数据集**声明**从来源报告的 manifest 复制（无行——引擎里同 id 的
        表已装载），注册表 datasets[] 从来源条目原样引用（同一磁盘快照），所以
        草稿的聚合/筛选/字段面板开箱即用。转正（P5）= Agent 读 GET
        /api/reports/<draftId> 翻译成 free-v1 请求后
        ``report_build --report-id <draftId> --server <本server>`` 覆盖同一条目，
        URL 不变。编辑策略继承来源（同副本纪律），口令一致的会话可直接保存。
        """
        if not _same_origin_guard(_handler):
            respond_json(_handler, 403, {"error": {"code": "Forbidden",
                                                   "message": "跨站请求已拒绝"}})
            return
        entry = registry.get(reportId)
        if entry is None:
            raise NotFound(f"report {reportId!r} not registered")
        body = read_json(_handler)
        title = str(body.get("title") or "拼版草稿").strip()[:80] or "拼版草稿"
        wanted = body.get("datasets")
        if not isinstance(wanted, list) or not wanted:
            raise BadRequest("datasets required（至少选择一个数据集声明）")
        src_entry_ds = {d.get("id"): d for d in entry.get("datasets", [])
                        if isinstance(d, dict) and d.get("id")}
        reg_datasets: list[dict] = []
        manifest_datasets: list[dict] = []
        for ds in wanted:
            if not isinstance(ds, dict) or not ds.get("id"):
                raise BadRequest("each dataset requires an id")
            ds_id = str(ds["id"])
            if ds_id not in src_entry_ds:
                raise NotFound(f"dataset {ds_id!r} is not registered on report {reportId!r}")
            reg_datasets.append(dict(src_entry_ds[ds_id]))
            manifest_datasets.append(ds)
        src_manifest = entry.get("manifest") or {}
        new_id = _next_draft_id(registry)
        manifest = {
            "schemaVersion": src_manifest.get("schemaVersion") or 3,
            "reportId": new_id,
            "title": title,
            "templateId": "free-v1",
            # draft=true 是零块 manifest 的唯一通行证：h_update_manifest 的
            # charts 非空闸、汇报/导出守卫、schema blocks minItems 都认它。
            "draft": True,
            "mode": "server",
            "pageWidth": src_manifest.get("pageWidth") or 1280,
            "datasets": manifest_datasets,
            "charts": [],
            "blocks": [],
            "kpis": [],
            "insights": [],
            "filters": [],
            "filterCandidates": [],
            "sections": [],
            # 字段语义/跨表键从来源继承：草稿的字段面板、筛选卡、跨表 KPI 与
            # 来源报告同一套列宇宙（ds 声明里也各带 measureTypes）。
            "columnRoles": src_manifest.get("columnRoles") or {},
            "measureTypes": src_manifest.get("measureTypes") or {},
            "dimensionKeys": src_manifest.get("dimensionKeys") or {},
            "runtime": src_manifest.get("runtime") or {},
        }
        # 策略唯一真源 = 来源**条目**（edit_policy_of 的第一优先级是 entry.edit）。
        # 旧写法从复制来的 manifest.runtime 再解析一遍——_apply_policy_to_manifest
        # 镜像使其今日恒等价，但那是两份来源碰巧相等；直接读条目消除双源，并把
        # 同一策略镜像进草稿 manifest（副本继承 + 报告页 editEnabled 提示的通道）。
        policy = edit_policy_of(entry)
        _apply_policy_to_manifest(manifest, policy)
        new_entry = {
            "title": title,
            "manifest": manifest,
            "datasets": reg_datasets,
            "family": entry.get("family") or _family_of(reportId),
            "changelog": [_change_entry("draft", new_id, title, manifest, source=reportId)],
            # 与副本分支同一条纪律：草稿靠条目策略继承口令/开关状态，来源会话
            # 已解锁的口令对草稿同样有效。
            "edit": policy,
        }
        # 草稿 datasets 声明里可能带计算列/维度值分组声明（复制自来源 manifest）
        # ——同步进注册表条目，重启 replay 才能补上列。声明是来源并集的子集，
        # 幂等（列只增不减）。
        _sync_derived_columns(new_entry, manifest)
        registry.register(new_id, new_entry)
        respond_json(_handler, 200, {"ok": True, "reportId": new_id,
                                     "url": f"/reports/{new_id}", "mode": "draft"})

    def h_create_draft_blank(_handler):
        """空白拼版草稿，零数据集（docs/compose-user-path-design.md D4 第一/二层）。

        冷启动入口：``report_build --draft``（CLI）与报告管理页「＋ 新建拼版草稿」
        都打到这里——不依赖任何来源报告，建 ``draft=true``、``datasets=[]``、零块的
        free-v1 草稿条目（reportId 同 ``compose-<base36>`` 契约）。数据由用户在草稿内
        「数据源配置 → 添加数据表」补齐（h_dataset_add 既有管道）；转正仍走
        ``report_build --report-id <draftId> --server <本server> --mode server``
        覆盖同一条目，URL 不变。服务端生成编辑口令——草稿的整条工作流（拖占位块 →
        syncManifest）必须能保存，没有口令每一步保存都被编辑闸挡死。
        """
        if not _same_origin_guard(_handler):
            respond_json(_handler, 403, {"error": {"code": "Forbidden",
                                                   "message": "跨站请求已拒绝"}})
            return
        body = read_json(_handler)
        title = str(body.get("title") or "拼版草稿").strip()[:80] or "拼版草稿"
        new_id = _next_draft_id(registry)
        password = _generate_password()
        manifest = {
            "schemaVersion": 3,
            "reportId": new_id,
            "title": title,
            "templateId": "free-v1",
            # draft=true 是零块 manifest 的唯一通行证（同 h_create_draft）。
            "draft": True,
            "mode": "server",
            "pageWidth": 1280,
            "datasets": [],
            "charts": [],
            "blocks": [],
            "kpis": [],
            "insights": [],
            "filters": [],
            "filterCandidates": [],
            "sections": [],
            "columnRoles": {},
            "measureTypes": {},
            "dimensionKeys": {},
            "runtime": {},
        }
        policy = {"enabled": True, "password": password}
        _apply_policy_to_manifest(manifest, policy)
        entry = {
            "title": title,
            "manifest": manifest,
            "datasets": [],
            "family": _family_of(new_id),
            "changelog": [_change_entry("draft", new_id, title, manifest)],
            "edit": policy,
        }
        registry.register(new_id, entry)
        respond_json(_handler, 200, {"ok": True, "reportId": new_id,
                                     "url": f"/reports/{new_id}", "mode": "draft",
                                     "editPassword": password})

    def h_reload_registry(_handler):
        # 同 h_register：全量重载是管理操作（且会把磁盘快照重灌进引擎）。
        if not is_loopback(_handler) or not _same_origin_guard(_handler):
            respond_json(_handler, 403, {"error": {"code": "Forbidden",
                                                   "message": "仅能在运行报告的本机注册/管理报告"}})
            return
        registry.reload_disk()
        # also reload any referenced datasets
        for entry in registry.list():
            for d in entry.get("datasets", []):
                if _is_derived_ds(d):
                    continue
                try:
                    _reload_dataset(engine, d, registry)
                except Exception as e:  # noqa: BLE001
                    print(f"[ai-report-server] WARN reload dataset {d.get('id')!r} failed: {e}",
                          file=sys.stderr)
            # second pass: 计算列 (union over ALL entries — a table shared by
            # report copies must keep every report's declared column) + derived
            # wide tables (need their sources loaded)
            from .registry import replay_entry
            try:
                replay_columns(engine, registry)
                replay_entry(engine, entry)
            except Exception as e:  # noqa: BLE001
                print(f"[ai-report-server] WARN replay {entry.get('reportId')!r} failed: {e}",
                      file=sys.stderr)
                continue
            # replay_entry 会改写 entry['manifest']（宽表列清单/rowCount）——
            # 回写注册表，否则修好的 manifest 只活在深拷贝里，重启即丢。
            registry.register(entry.get("reportId"), entry)
        respond_json(_handler, 200, {"reloaded": True, "reportCount": len(registry.list())})

    def h_register(_handler):
        # 注册/注销/全量重载是报告管理操作（AGENTS 报告管理节：仅报告主人本机
        # 可知晓、可访问）——h_register 还接受任意本地路径的数据集快照，对 LAN
        # 开放等于允许任意本机可读文件被拷进数据目录再经 /rows 读走。与
        # h_shutdown/h_manager 同一门禁；serve_report/report_build 均从本机注册。
        if not is_loopback(_handler) or not _same_origin_guard(_handler):
            respond_json(_handler, 403, {"error": {"code": "Forbidden",
                                                   "message": "仅能在运行报告的本机注册/管理报告"}})
            return
        body = read_json(_handler)
        report_id = body.get("reportId")
        if not isinstance(report_id, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{1,79}", report_id):
            raise BadRequest("reportId must match ^[A-Za-z][A-Za-z0-9_-]{1,79}$")
        manifest = body.get("manifest") or {}
        # pageWidth 非法值落盘后每次 GET /reports/<id> 都 500（template.render 的
        # resolve_page_width 抛错），与保存路径（h_update_manifest）同一条闸——
        # 注册路径漏掉等于报告可以带病进场且永久打不开。
        if isinstance(manifest, dict) and manifest.get("pageWidth") is not None:
            try:
                from validate_request import resolve_page_width
                resolve_page_width(manifest.get("pageWidth"))
            except Exception as exc:
                raise BadRequest(f"manifest.pageWidth invalid: {exc}")
        if isinstance(manifest, dict):
            security = manifest.get("security") if isinstance(manifest.get("security"), dict) else {}
            tenant_values = [body.get("tenantId"), manifest.get("tenantId"), security.get("tenantId")]
            tenant_values = [_validate_tenant_id(value) for value in tenant_values if value not in (None, "")]
            if tenant_values and any(value != tenant_values[0] for value in tenant_values[1:]):
                raise BadRequest("tenantId declarations must agree")
            if tenant_values and body.get("tenantId") and not manifest.get("tenantId"):
                manifest["tenantId"] = tenant_values[0]
        # NC-02 冲突闸（2026-10-23，缺陷 #8 拍板 D2）：upsert 只在 **title 一致**
        # 时放行（正常重建/月度刷新契约——否则现有报告全部无法重建）；title 不同
        # 几乎必然是 slug 撞车（典型：历史遗留的通用 id "report"、手写的短 id）
        # ——注册是按 reportId 整条覆盖，静默 upsert 会把别人报告的 manifest 连
        # 数据一起洗掉（2026-10-20 课程交付破坏性实锤，靠 archived 备份恢复）。
        # 拦下并给出可行动出口：换 --report-id / 请求顶层 reportId，或确认要
        # 覆盖同一 id 时显式带 force=true（roadmap D2）。
        _existing = registry.get(report_id)
        if _existing is not None and not body.get("force"):
            _new_title = (manifest.get("title") if isinstance(manifest, dict) else None) \
                or body.get("title") or report_id
            _old_manifest = _existing.get("manifest") if isinstance(_existing.get("manifest"), dict) else {}
            _old_title = _old_manifest.get("title") or _existing.get("title") or report_id
            if str(_new_title) != str(_old_title):
                raise Conflict(
                    f"reportId '{report_id}' 已被标题「{_old_title}」的报告占用，本次注册的标题是「{_new_title}」。"
                    "为防静默覆盖已有报告：请改用 --report-id（或请求顶层 reportId）换一个 id 注册；"
                    "确认要覆盖同一 id 时，在注册 payload 显式传 force=true 后重试。")
        multi = bool(body.get("multi"))
        ds_items = body.get("datasets")
        if isinstance(ds_items, list):
            if not ds_items:
                raise BadRequest("datasets required")
            entries = ds_items
        else:
            single = body.get("dataset") or {}
            if not single:
                raise BadRequest("dataset required")
            entries = [single]
        if multi and len(entries) > 8:
            raise BadRequest("a report may register at most 8 datasets")

        ds_infos: list[dict] = []
        loaded: list[dict] = []
        has_relation_payload = False
        for ds in entries:
            if not isinstance(ds, dict) or not ds.get("id"):
                raise BadRequest("each dataset requires an id")
            # Mixed/multi registration sends FINAL (already namespaced) ids and
            # skips manifest rewriting; the single-dataset legacy flow keeps the
            # {report_id}__{raw} isolation (Issue #8) for backward compatibility.
            if multi:
                ds_id = str(ds["id"])
                raw_ds_id = str(ds.get("raw") or ds_id)
            else:
                raw_ds_id = str(ds["id"])
                ds_id = f"{report_id}__{raw_ds_id}"
            # 关系宽表（运行时「立即合并」产物，便携出货 2026-09-26）：payload
            # 只带 derived.kind=relation 声明——没有快照也没有行。登记声明条目
            # （source=None），装载轮结束后由 replay_entry 从已装载的源表重建
            # 引擎表（与 boot 的「先普通表后宽表」同序）。不收这类声明的话，
            # 收件机注册表没有宽表条目 → replay 不接管 → 跨源图表永久 400。
            rel_decl = ds.get("derived") if isinstance(ds.get("derived"), dict) else None
            if rel_decl is not None and rel_decl.get("kind") == "relation" \
                    and not ds.get("source") and not (ds.get("table") or {}).get("columns"):
                rel = rel_decl.get("relation")
                if not isinstance(rel, dict) or \
                        not [t for t in (rel.get("tables") or []) if isinstance(t, str)]:
                    raise BadRequest("relation-derived dataset requires derived.relation.tables "
                                     "(>=2 dataset ids)")
                from .registry import wide_label
                ds_infos.append({
                    "id": ds_id,
                    "label": str(ds.get("label") or wide_label(rel)),
                    "rowCount": ds.get("rowCount") if isinstance(ds.get("rowCount"), int) else 0,
                    "source": None,
                    "derived": {"kind": "relation", "relation": rel},
                })
                has_relation_payload = True
                continue
            snapshot_path = None
            if ds.get("source"):
                from pathlib import Path
                try:
                    snapshot_path = snapshot_data_file(
                        Path(ds["source"]),
                        getattr(registry, "data_dir", None),
                        namespace=f"{report_id}__{raw_ds_id}",
                    )
                except (OSError, ValueError) as e:
                    raise BadRequest(f"failed to snapshot dataset: {e}")
            table = ds.get("table") or {}
            cols = table.get("columns") or []
            rows = table.get("rows") or []
            if not isinstance(cols, list) or not cols or len(cols) > 200:
                raise BadRequest("dataset columns must contain 1-200 fields")
            if not isinstance(rows, list) or len(rows) > 5_000_000:
                raise BadRequest("dataset rows must contain at most 5,000,000 records")
            if any(not isinstance(row, list) or len(row) != len(cols) for row in rows):
                raise BadRequest("dataset rows must be rectangular")
            # Row-level derived columns (数据集里的「计算列」)：服务端必须把同一份
            # 声明物化成真列，否则 manifest 声称存在、SQLite 却没有 → 引用它的聚合
            # 直接 no such column。声明也写进注册表，供重启/重载时按原始快照重放。
            # 表被多份报告共享（副本继承 datasets），所以取「本表声明并集」：
            # 只补缺失的列，绝不因本份报告没声明而抹掉别份的列。
            derived_decl = columns_decl(ds.get("derived"))
            union_defs = table_defs(registry.raw_entries(), ds_id, extra=derived_decl)
            table = apply_dataset_derived(table, {"kind": "columns", "defs": union_defs})
            # 维度值分组（dimGroups）：同一条物化管线（并集 + 只补缺列）。
            group_union = dim_group_table_defs(registry.raw_entries(), ds_id,
                                               extra=dim_groups_defs(ds.get("dimGroups")))
            table = apply_dim_groups(table, group_union)
            cols = table.get("columns") or []
            rows = table.get("rows") or []
            # 索引列声明（查询加速）随注册 payload 持久化进注册表 meta：重载
            # （_reload_dataset / replay_all / run_server._reload_all）从 meta
            # 透传回 load_table。此前整条管道「只读不写」——payload 三个来源
            # （report_build/serve_report/h_dataset_replace）无一发送该键、
            # ds_entry 不落、重载不传，注释却声称已接通。校验 fail-closed：
            # 字符串列表、≤8、均为本数据集（物化后的）真实列名。
            index_cols = ds.get("indexColumns")
            if index_cols:
                if not isinstance(index_cols, list) or len(index_cols) > 8 \
                        or not all(isinstance(c, str) and c.strip() for c in index_cols):
                    raise BadRequest("indexColumns must be a list of at most 8 column names")
                unknown_idx = [c for c in index_cols if c not in cols]
                if unknown_idx:
                    raise BadRequest(f"indexColumns 引用未知列（{unknown_idx[:5]}）；"
                                     "必须是该数据集的真实列名")
                index_cols = [str(c) for c in index_cols]
            info = engine.load_table(ds_id, cols, rows, index_columns=list(index_cols or []))
            loaded.append((ds_id, info))
            if not multi:
                manifest = _namespace_manifest(manifest, raw_ds_id, ds_id)
            source_value = ds.get("source")
            if snapshot_path is not None:
                try:
                    source_value = os.path.relpath(snapshot_path, registry.path.parent)
                except ValueError:
                    source_value = str(snapshot_path)
            ds_entry = {
                "id": ds_id,
                "label": ds.get("label") or ds_id,
                "rowCount": info.row_count,
                "source": source_value,
            }
            if derived_decl:
                ds_entry["derived"] = derived_decl
            if ds.get("dimGroups"):
                # 维度值分组声明：与 derived 同一条持久化契约——不落注册表的话
                # 重启 replay_all 扫不到声明，分组列从引擎表消失，引用它的
                # 聚合全部 400（重启/副本场景必踩）。
                ds_entry["dimGroups"] = dim_groups_defs(ds.get("dimGroups"))
            if ds.get("primaryKey"):
                # 增量合并主键声明：随注册持久化，重启/刷新向导都从注册表读。
                ds_entry["primaryKey"] = [str(c) for c in ds["primaryKey"]]
            if index_cols:
                # 索引列声明随 meta 持久化（重载/物化重建时透传回 load_table）。
                ds_entry["indexColumns"] = index_cols
            ds_infos.append(ds_entry)

        # The manifest must carry the FINAL registry reportId: the runtime
        # builds its server-save URL (POST /reports/:id/manifest) from
        # manifest.reportId, so a manifest still saying "report" would 404 on
        # every 覆盖/副本 save.
        manifest["reportId"] = report_id
        title = manifest.get("title") or body.get("title") or report_id
        # 构建期写了 editPassword 就默认「在线编辑已开启」；没有口令的报告默认关闭
        # （管理页可一键开启，服务端会补一个口令）。
        policy = edit_policy_of({"manifest": manifest})
        _apply_policy_to_manifest(manifest, policy)
        entry = registry.register(report_id, {
            "title": title,
            "manifest": manifest,
            "datasets": ds_infos,
            "family": report_id,
            "edit": policy,
            "changelog": [_change_entry("initial", report_id, title, manifest)],
        })
        if has_relation_payload:
            # 温启动注册路径没有后续的 _reload_all：宽表声明落表后立刻从已装载
            # 的源表重建（源表在同一 payload 的装载轮里已就位）。失败不拖垮
            # 注册（可行动 WARN；重启/管理页重载还会再试）；重建校正的
            # rowCount/manifest 回写注册表。
            from .registry import replay_entry as _replay_wide
            entry = registry.get(report_id) or entry
            for item in _replay_wide(engine, entry, verbose=True):
                print(f"[ai-report-server] rebuilt derived {item['id']!r} ({item['rowCount']} rows)")
            registry.register(report_id, entry)
        respond_json(_handler, 200, {
            "reportId": report_id,
            "datasetId": ds_infos[0]["id"] if ds_infos else "",
            "rowCount": sum(info.row_count for _, info in loaded),
            "datasetIds": [d["id"] for d in ds_infos],
        })

    def h_unregister(_handler):
        # 同 h_register：注销会顺带 DROP 引擎表，绝不能对 LAN 开放。
        if not is_loopback(_handler) or not _same_origin_guard(_handler):
            respond_json(_handler, 403, {"error": {"code": "Forbidden",
                                                   "message": "仅能在运行报告的本机注册/管理报告"}})
            return
        body = read_json(_handler)
        report_id = body.get("reportId")
        if not report_id:
            raise BadRequest("reportId required")
        old_entry = registry.get(report_id)
        removed = registry.unregister(report_id)
        # also drop the table(s) from the engine if no other report uses them
        if removed:
            # try to drop dataset tables that have no remaining reports
            used: set[str] = set()
            for e in registry.list():
                for d in e.get("datasets", []):
                    used.add(d["id"])
            for dataset in (old_entry or {}).get("datasets", []):
                name = dataset.get("id")
                if name and name not in used:
                    engine.drop_table(name)
        respond_json(_handler, 200, {"reportId": report_id, "removed": removed})

    def h_serve_view(_handler, reportId):
        # Serve the report view HTML (manifest inlined).
        # The actual HTML template lives in scripts/server.py (passed in).
        template_provider = _handler.server.template_provider  # type: ignore[attr-defined]
        html = template_provider.render(reportId)
        if html is None:
            raise NotFound(f"report {reportId!r} not registered")
        _send_html_page(_handler, html)

    def h_index(_handler):
        """Root URL. When the registry already holds reports, ``/`` opens the
        BASE report directly (the html-deploy-server link style: colleagues
        just open http://<ip>:<port>/ with no path). The full list lives at
        /reports; an empty registry serves the list page with its hint."""
        registry_entries = registry.list()
        first = next((e.get("reportId") for e in registry_entries if e.get("reportId")), None)
        if first:
            _handler.send_response(302)
            _handler.send_header("Location", f"/reports/{first}")
            _handler.send_header("Content-Length", "0")
            _handler.send_header("Cache-Control", "no-store")
            _handler.end_headers()
            return
        template_provider = _handler.server.template_provider  # type: ignore[attr-defined]
        html = template_provider.render_index()
        _send_html_page(_handler, html)

    def h_reports_index(_handler):
        """The full report list (moved off ``/`` so the root can deep-link)."""
        template_provider = _handler.server.template_provider  # type: ignore[attr-defined]
        html = template_provider.render_index()
        _send_html_page(_handler, html)

    def h_manager(_handler):
        """报告管理 page: every running report server on this machine.

        报告管理只属于报告主人机器（构建控制台 / README / marker 落地页打印
        链接），不对局域网访客开放：非回环来源一律 403，报告页工具栏也没有
        任何入口。"""
        if not is_loopback(_handler):
            respond_json(_handler, 403, {"error": "报告管理仅限在本机（localhost）访问"})
            return
        template_provider = _handler.server.template_provider  # type: ignore[attr-defined]
        html = template_provider.render_manager()
        _send_html_page(_handler, html)

    def h_reports_all(handler):
        """List every report served by every live report server on THIS machine.

        Sources: the global servers index (~/.ai-html-report/servers.json,
        maintained by run_server) → health-ping each port → read that server's
        registry from disk (same machine; no extra network hop). Dead entries
        are pruned so the manager never shows ghosts. 该端点是报告管理页的数据源，
        与 /manager 一样仅限本机回环访问（局域网访客既看不到列表也拿不到口令）。
        响应**禁带** ACAO:*（安全 P1）：带 * 时任意网页都能跨站读到口令明文；
        管理页数据源是同源相对 fetch，不需要 CORS。"""
        if not is_loopback(handler) or not _same_origin_guard(handler):
            respond_json(handler, 403, {"error": "报告管理仅限在本机（localhost）访问"})
            return
        # 自愈：先把「本机这个服务」登记一次再列。索引曾出现过活着的服务却没条目
        # 的情况（跨进程 read-modify-write 丢更新 / 忙时 health ping 超时被 prune），
        # 于是 /manager 显示「没有运行中的报告」——报告主人正在看的这台服务反而
        # 不在列表里。每次查列表都刷新一次自己的条目，这类丢条目就自愈了。
        try:
            _srv = getattr(handler, "server", None)
            _self_port = int(_srv.server_address[1]) if _srv is not None else 0
            _self_reg = getattr(getattr(_srv, "registry", None), "path", None)
            if _self_port and _self_reg:
                index_register(_self_port, _self_reg, os.getpid())
        except Exception:
            pass
        prune_dead()
        lan = _lan_ip()
        rows: list[dict] = []
        for port_str, entry in sorted(index_load().items(), key=lambda kv: int(kv[0])):
            port = int(port_str)
            reg_path_str = str(entry.get("registryPath") or "")
            reg_path = Path(reg_path_str)
            if not index_ping(port) or not reg_path.exists():
                continue
            try:
                reg = Registry(reg_path)
            except Exception:
                continue
            report_dir = reg_path.parent.parent
            # XP-01：磁盘占用列——快照数据目录与该注册表同级（统一 manager 下
            # 即本机 state home；隔离 LRM_STATE_HOME 的测试注册表自然取各自的）。
            reg_data_dir = reg_path.parent / "data"
            for e in reg.raw_entries():   # 报告管理每 10s 拉一次：绝不能深拷贝整份注册表
                manifest = e.get("manifest") or {}
                rid = str(e.get("reportId") or "")
                row = {
                    "reportId": rid,
                    "title": e.get("title") or manifest.get("title") or rid,
                    "port": port,
                    "mode": manifest.get("mode", "server"),
                    # 拼版草稿标记（docs/compose-user-path-design.md D5）：管理页
                    # 据此渲染「拼版草稿」徽标并只对草稿出「删除草稿」按钮。
                    "draft": bool(manifest.get("draft")),
                    "chartCount": len(manifest.get("charts") or []),
                    "createdAt": e.get("registeredAt"),
                    "updatedAt": e.get("updatedAt"),
                    "family": e.get("family") or _family_of(rid),
                    "changelog": e.get("changelog") or [],
                    "reportDir": str(report_dir),
                    "pid": entry.get("pid"),
                    "startedAt": entry.get("startedAt"),
                    "urlLocal": f"http://127.0.0.1:{port}/reports/{rid}",
                    "urlLan": f"http://{lan}:{port}/reports/{rid}",
                    # XP-01 磁盘列：该报告全部数据集快照文件字节和（失败按 0 计，
                    # 绝不让 stat 抖动拖垮管理页）。
                    "diskBytes": _report_disk_bytes(reg_data_dir, e),
                }
                policy = edit_policy_of(e)
                row["editEnabled"] = bool(policy["enabled"])
                if is_loopback(handler):
                    row["editPassword"] = policy["password"] if policy["enabled"] else ""
                rows.append(row)
        # XP-01：顶层本机状态目录体量（data 快照 + db 表缓存），管理页页脚总量。
        state_home = Path(registry.path.parent)
        state_home_bytes = {
            "dataBytes": _dir_file_bytes(state_home / "data"),
            "dbBytes": _dir_file_bytes(state_home / "db"),
        }
        respond_json(handler, 200, {"reports": rows, "lanIp": lan,
                                    "stateHome": state_home_bytes})

    def h_edit_policy_get(_handler, reportId):
        """Read one report's online-edit policy (报告管理页用，仅限本机回环）。"""
        if not is_loopback(_handler) or not _same_origin_guard(_handler):
            respond_json(_handler, 403, {"error": EDIT_DISABLED_MESSAGE})
            return
        entry = registry.get(reportId)
        if entry is None:
            raise NotFound(f"report {reportId!r} not registered")
        policy = edit_policy_of(entry)
        respond_json(_handler, 200, {"reportId": reportId, "enabled": policy["enabled"], "password": policy["password"]})

    def h_edit_policy_set(_handler, reportId):
        """开启/关闭在线编辑 + 设置口令（报告管理页，仅限本机回环）。

        body: {enabled?: bool, password?: str}
          - enabled=false → 之后任何保存请求都被拒（提示「请联系管理员」）
          - enabled=true  → 确保存在口令（没有就自动生成一个）
          - password=xxx  → 改成 xxx（1-64 字符；空串等价于「只切换开关」）
        策略写进注册表条目 ``edit``，并镜像到 manifest.runtime.editPassword /
        editEnabled（副本靠 manifest 继承，报告页靠 editEnabled 提示）。
        """
        if not is_loopback(_handler) or not _same_origin_guard(_handler):
            respond_json(_handler, 403, {"error": EDIT_DISABLED_MESSAGE})
            return
        entry = registry.get(reportId)
        if entry is None:
            raise NotFound(f"report {reportId!r} not registered")
        body = read_json(_handler) or {}
        policy = edit_policy_of(entry)
        if "enabled" in body:
            policy["enabled"] = bool(body.get("enabled"))
        raw_pw = body.get("password")
        if isinstance(raw_pw, str) and raw_pw.strip():
            new_pw = raw_pw.strip()
            if len(new_pw) > 64:
                raise BadRequest("编辑口令最多 64 个字符")
            policy["password"] = new_pw
        if policy["enabled"] and not policy["password"]:
            policy["password"] = _generate_password()
        new_entry = dict(entry)
        new_entry["edit"] = {"enabled": bool(policy["enabled"]), "password": policy["password"]}
        _apply_policy_to_manifest(new_entry.get("manifest"), policy)
        registry.register(reportId, new_entry)
        respond_json(_handler, 200, {
            "reportId": reportId,
            "enabled": bool(policy["enabled"]),
            "password": policy["password"] if policy["enabled"] else "",
        })

    def h_shutdown(handler):
        """Stop THIS report server (loopback callers only).

        Removes the port from the global index, answers, then stops
        serve_forever — run_server returns and the process exits. LAN visitors
        of a shared report must never be able to kill the host's servers."""
        if not is_loopback(handler) or not _same_origin_guard(handler):
            respond_json(handler, 403, {"error": {"code": "Forbidden",
                                                  "message": "仅能在运行报告的本机关闭服务"}})
            return
        port = int(handler.server.server_address[1])  # type: ignore[attr-defined]
        index_unregister(port)
        respond_json(handler, 200, {"ok": True, "port": port})
        server_ref = handler.server  # type: ignore[attr-defined]
        threading.Timer(0.3, server_ref.shutdown).start()
        threading.Timer(3.0, lambda: os._exit(0)).start()  # failsafe: never linger

    # ---- API routes ----
    r.add("GET",  "/api/health", h_health)
    r.add("GET",  "/api/admin/index-suggestions", h_index_suggestions)
    r.add("GET",  "/api/admin/index-suggestions/explain", h_index_explain)
    r.add("POST", "/api/admin/index-suggestions/proposals/preview", h_index_proposal_preview)
    r.add("POST", "/api/admin/index-suggestions/proposals", h_index_proposal_create)
    r.add("GET",  "/api/admin/index-suggestions/proposals/:proposalId", h_index_proposal_get)
    r.add("POST", "/api/admin/index-suggestions/proposals/:proposalId/approve", h_index_proposal_approve)
    r.add("POST", "/api/admin/index-suggestions/proposals/:proposalId/apply", h_index_proposal_apply)
    r.add("GET",  "/api/copilot/config", h_copilot_config_get)
    r.add("POST", "/api/copilot/config", h_copilot_config_set)
    r.add("GET",  "/api/copilot/provider-models", h_copilot_provider_models)
    r.add("POST", "/api/copilot/test", h_copilot_test)
    r.add("GET",  "/api/copilot/usage", h_copilot_usage)
    r.add("GET",  "/api/copilot/tools", h_copilot_tools)
    r.add("POST", "/api/reports/:reportId/copilot/auth/token", h_copilot_auth_token)
    r.add("POST", "/api/reports/:reportId/copilot/plan/validate", h_copilot_plan_validate)
    r.add("POST", "/api/reports/:reportId/copilot/plan/execute", h_copilot_plan_execute)
    r.add("POST", "/api/reports/:reportId/copilot/proposals/validate", h_copilot_proposal_validate)
    r.add("GET",  "/api/reports/:reportId/copilot/proposals/history", h_copilot_proposal_history)
    r.add("POST", "/api/reports/:reportId/copilot/proposals/apply", h_copilot_proposal_apply)
    r.add("POST", "/api/reports/:reportId/copilot/proposals/rollback", h_copilot_proposal_rollback)
    r.add("POST", "/api/reports/:reportId/copilot/proposals/redo", h_copilot_proposal_redo)
    r.add("POST", "/api/reports/:reportId/copilot/evidence/export", h_copilot_evidence_export)
    r.add("GET",  "/api/reports/:reportId/copilot/evidence/:exportId", h_copilot_evidence_download)
    r.add("GET",  "/api/reports/:reportId/copilot/monitors", h_copilot_monitors)
    r.add("GET",  "/api/reports/:reportId/copilot/audit", h_copilot_audit)
    r.add("POST", "/api/reports/:reportId/copilot/monitors", h_copilot_monitor_create)
    r.add("POST", "/api/reports/:reportId/copilot/monitors/:monitorId", h_copilot_monitor_toggle)
    r.add("DELETE", "/api/reports/:reportId/copilot/monitors/:monitorId", h_copilot_monitor_delete)
    r.add("POST", "/api/reports/:reportId/copilot/notifications/propose", h_copilot_notification_propose)
    r.add("POST", "/api/reports/:reportId/copilot/notifications/send", h_copilot_notification_send)
    r.add("GET",  "/api/reports/:reportId/copilot/jobs/:jobId", h_copilot_job)
    r.add("GET",  "/api/reports/:reportId/copilot/jobs", h_copilot_jobs)
    r.add("POST", "/api/reports/:reportId/copilot/jobs/:jobId/cancel", h_copilot_job_cancel)
    r.add("POST", "/api/reports/:reportId/copilot/jobs/:jobId/recover", h_copilot_job_recover)
    r.add("POST", "/api/reports/:reportId/copilot", h_copilot_turn)
    r.add("GET",  "/api/reports/all", h_reports_all)
    r.add("GET",  "/api/reports/:reportId/copilot/impact-graph", h_report_impact_graph)
    r.add("POST", "/api/admin/shutdown", h_shutdown)
    r.add("GET",  "/api/admin/edit-policy/:reportId", h_edit_policy_get)
    r.add("POST", "/api/admin/edit-policy/:reportId", h_edit_policy_set)
    r.add("GET",  "/api/datasets", h_list_datasets)
    r.add("GET",  "/api/datasets/:datasetId/schema", h_dataset_schema)
    r.add("POST", "/api/datasets/:datasetId/aggregate", h_dataset_aggregate)
    r.add("POST", "/api/aggregate-multi", h_aggregate_multi)
    r.add("POST", "/api/datasets/:datasetId/rows", h_dataset_rows)
    r.add("POST", "/api/datasets/:datasetId/rows/delete", h_dataset_rows_delete)
    r.add("GET",  "/api/datasets/:datasetId/values/:column", h_dataset_values)
    r.add("POST", "/api/datasets/:datasetId/values/:column", h_dataset_values_filtered)
    r.add("GET",  "/api/datasets/:datasetId/sample/:column", h_dataset_sample)
    r.add("POST", "/api/reports/:reportId/materialize-relation", h_relation_materialize)
    r.add("POST", "/api/reports/:reportId/datasets", h_dataset_add)
    r.add("POST", "/api/reports/:reportId/datasets/:datasetId/replace", h_dataset_replace)
    r.add("GET",  "/api/reports/:reportId/datasets/:datasetId/replace/jobs/:jobId", h_dataset_replace_job)
    r.add("POST", "/api/reports/:reportId/datasets/:datasetId/replace/jobs/:jobId/cancel", h_dataset_replace_job_cancel)
    r.add("POST", "/api/reports/:reportId/datasets/:datasetId/replace/jobs/:jobId/recover", h_dataset_replace_job_recover)
    r.add("POST", "/api/reports/:reportId/datasets/:datasetId/materialize", h_dataset_materialize)
    r.add("GET",  "/api/reports/:reportId/tables", h_report_tables)
    r.add("POST", "/api/reports/:reportId/datasets/:datasetId/drop", h_dataset_drop)
    r.add("GET",  "/api/reports", h_list_reports)
    r.add("GET",  "/api/reports/:reportId", h_get_report)
    r.add("GET",  "/api/reports/:reportId/charts/:chartId/aggregate", h_chart_aggregate)
    r.add("POST", "/api/reports/:reportId/charts/:chartId/aggregate", h_chart_aggregate)
    r.add("POST", "/api/reports/:reportId/charts/:chartId/truth", h_chart_truth)
    r.add("POST", "/api/reports/:reportId/reload", h_reload_report)
    r.add("POST", "/api/reports/:reportId/manifest", h_update_manifest)
    r.add("POST", "/api/reports/:reportId/draft", h_create_draft)
    r.add("POST", "/api/drafts", h_create_draft_blank)
    r.add("POST", "/api/admin/reload-registry", h_reload_registry)
    r.add("POST", "/api/admin/registry/register", h_register)
    r.add("POST", "/api/admin/registry/unregister", h_unregister)
    r.add("GET",  "/reports/:reportId", h_serve_view)
    r.add("GET",  "/reports", h_reports_index)
    r.add("GET",  "/manager", h_manager)
    r.add("GET",  "/", h_index)
    r.add("GET",  "/index.html", h_reports_index)

    def _monitor_loop():
        """Enqueue read-only monitors; local or external workers may claim them."""
        last_export_prune = 0.0
        while True:
            try:
                now = time.time()
                if now - last_export_prune >= 30:
                    _prune_evidence_exports(registry, now=now)
                    last_export_prune = now
                for monitor in _jobs.due_monitors():
                    report_id = monitor["reportId"]
                    entry = registry.get(report_id)
                    if entry is None:
                        _jobs.set_monitor_enabled(monitor["id"], report_id, False)
                        continue
                    try:
                        job_id = _jobs.enqueue("monitor", report_id, "monitor:" + monitor["id"],
                                               priority=10, payload={"monitorId": monitor["id"],
                                                                      "previousJobId": monitor.get("lastJobId"),
                                                                      "recoveryPolicy": "safe-read"},
                                               trace_id="monitor:" + monitor["id"])
                    except (_jobs.JobConflict, _jobs.JobCapacity):
                        continue
                    if not _jobs.claim_monitor(monitor["id"], job_id, monitor["intervalSeconds"]):
                        _jobs.cancel(job_id, "monitor 已被其他进程领取")
                        continue
                # The embedded worker keeps the default one-process server
                # self-sufficient; ``run_server --worker`` can consume the
                # same queue from another process/machine.
                for _ in range(8):
                    if not _jobs.worker_once(
                            lambda job, progress: dispatch_queued_job(engine, registry, job, progress),
                            kinds=["monitor", "agent_plan", "copilot", "evidence_export", "replace"], lease_seconds=60):
                        break
            except Exception as exc:  # monitor failure must not kill the server loop
                print(f"[ai-report-server] WARN monitor scheduler: {type(exc).__name__}", file=sys.stderr)
            time.sleep(2)

    threading.Thread(target=_monitor_loop, daemon=True, name="lrm-monitor-scheduler").start()
    return r


# ---------------------- helpers -------------------------------------------------

def _lan_ip() -> str:
    """Best-effort LAN address of this machine (for share/manager links)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.3)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip:
            return ip
    except Exception:
        pass
    return "127.0.0.1"


# The body snapshot (bodyHtml) is editor-supplied HTML that template.py serves
# verbatim to every viewer of the report. The shell injects all runtime script
# tags itself, so script blocks in the snapshot are never legitimate — strip
# them (defense against a compromised/rogue editor session persisting XSS).
_BODY_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script\s*>", re.IGNORECASE | re.DOTALL)
_BODY_SCRIPT_OPEN_RE = re.compile(r"<script\b[^>]*/?>", re.IGNORECASE)
# 存储型 XSS 面收口（在 <script> 之外）：on* 事件属性（<svg onload>/<img onerror>）、
# 嵌入文档/插件标签（iframe/object/embed/frame/base/link/meta——报告正文没有合法
# 用途）、javascript: 伪协议 URL。正则消毒不是完整白名单，但把「只剥 script」
# 的裸黑洞补到常识水位；bodyHtml 的合法词汇本来就有白名单（富文本通道）。
_BODY_DANGEROUS_RE = re.compile(
    r"</?(?:iframe|object|embed|frame|frameset|base|link|meta)\b[^>]*>", re.IGNORECASE)
_BODY_EVENT_ATTR_RE = re.compile(
    r"\son[a-zA-Z]+\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE)
_BODY_JS_URL_RE = re.compile(
    r"(\s(?:href|src|action|xlink:href)\s*=\s*)(\"|')\s*javascript:[^\"']*\2",
    re.IGNORECASE)
_BODY_JS_URL_UNQ_RE = re.compile(
    r"(\s(?:href|src|action|xlink:href)\s*=\s*)javascript:[^\s>]+", re.IGNORECASE)


def _sanitize_body_html(html_text: str) -> str:
    out = _BODY_SCRIPT_RE.sub("", html_text or "")
    out = _BODY_SCRIPT_OPEN_RE.sub("", out)   # 未闭合的 <script>：浏览器会吞掉余下文档
    out = _BODY_DANGEROUS_RE.sub("", out)
    out = _BODY_EVENT_ATTR_RE.sub("", out)
    out = _BODY_JS_URL_RE.sub(r"\1\2#\2", out)
    out = _BODY_JS_URL_UNQ_RE.sub(r"\1#", out)
    return out


def _sanitize_theme_vars(manifest: dict) -> None:
    """Drop non ``--*`` keys from ``themeShells[*].vars`` before persisting.

    安全（P2，2026-09-16）：build_manifest 只烘 CSS 自定义属性键，但在线保存把
    浏览器发来的 manifest 原样落盘；非 --* 键不属于任何合法令牌（渲染端
    template._theme_vars_style 还会再做键白名单 + 值转义——这是同一闸门的
    持久化侧，落盘工件保持干净）。"""
    shells = manifest.get("themeShells")
    if not isinstance(shells, dict):
        return
    for shell in shells.values():
        if not isinstance(shell, dict) or not isinstance(shell.get("vars"), dict):
            continue
        cleaned = {k: v for k, v in shell["vars"].items()
                   if isinstance(k, str) and k.startswith("--")}
        if len(cleaned) != len(shell["vars"]):
            shell["vars"] = cleaned


def handler_is_post(handler: BaseHTTPRequestHandler) -> bool:
    return handler.command == "POST"


def _find_chart(entry: dict, chart_id: str) -> dict | None:
    for c in entry.get("manifest", {}).get("charts", []):
        if c.get("id") == chart_id:
            return c
    return None


def _chart_request_payload(chart: dict, data: dict) -> dict:
    """Build the aggregate JSON payload from chart.editor.data (see _chart_to_request).

    Split from _chart_to_request so the S2 data-plane gate can inspect the raw
    column references (groupBy/measures/filter/orderBy/bucket.source) before
    parse_request consumes them."""
    measures = [m for m in (data.get("measures") or []) if isinstance(m, str)]
    measure_items = [{"column": m, "aggregation": data.get("aggregation", "sum")} for m in measures]
    derived_map = data.get("derivedMeasures")
    if isinstance(derived_map, dict) and derived_map:
        real = [m for m in measures if m not in derived_map]
        nodes = [n for n in derived_map.values()
                 if isinstance(n, dict) and n.get("name") and n["name"] in measures]
        if nodes:
            import sys as _sys
            from pathlib import Path as _Path
            _scripts_dir = _Path(__file__).resolve().parent.parent / "scripts"
            if str(_scripts_dir) not in _sys.path:
                _sys.path.insert(0, str(_scripts_dir))
            from derived_eval import required_aggregations  # noqa: PLC0415
            default_agg = data.get("aggregations") if isinstance(data.get("aggregations"), dict) \
                else (data.get("aggregation") or "sum")
            deps = required_aggregations(nodes, {n["name"] for n in nodes}, default_agg)
            measure_items = [{"column": m, "aggregation": data.get("aggregation", "sum")} for m in real] \
                + [{"column": d["col"], "aggregation": d["agg"],
                    **({"filters": d["filters"]} if d.get("filters") else {})} for d in deps]
    payload = {
        "groupBy": [data.get("dimension")] if data.get("dimension") else [],
        "measures": measure_items,
        "filter": data.get("filter") or [],
        "orderBy": data.get("orderBy") or [],
        "limit": data.get("limit"),
        "offset": data.get("offset", 0),
    }
    roles = data.get("roles") or {}
    if roles.get("breakdown") and roles["breakdown"] not in payload["groupBy"]:
        payload["groupBy"].append(roles["breakdown"])
    bucket = data.get("dimensionBucket")
    if bucket:
        payload["bucket"] = bucket
    return payload


def _chart_to_request(chart: dict, data: dict) -> Any:
    """Translate chart.editor.data into an AggregateRequest.

    聚合级派生指标（``derivedMeasures``：formula/window/share）在服务端没有对应列，
    直接把派生名当 measure 列会 400「measure column not found」——调试端点曾因此对
    任何含派生指标的图误报「图坏了」（客户端运行时不走此端点，只有外部脚本/调试直呼
    会踩到）。与客户端 fetchAggregate/aggregateTable 同一契约：派生名是占位符、不在
    服务端聚合列里；其 ``{agg,col}`` 依赖序列按 derived_eval.required_aggregations
    展开成额外 measure 一并返回，派生值仍由调用方在聚合结果上求值（derivedEvaluate），
    服务端绝不求值派生指标（AGENTS：derived.measures 不进注册表表声明）。
    """
    return parse_request(_chart_request_payload(chart, data))


def _server_strip(manifest: dict) -> dict:
    """Strip rows from a server-mode manifest before sending to client.

    Defensive: even if the manifest accidentally contains rows (e.g. compiled
    for singlefile but served in server mode), drop them. Never raises on
    malformed/missing optional fields (Issue #7): datasets/charts may be absent
    or non-list, editor/option may be non-dict.
    """
    if not isinstance(manifest, dict):
        return {}
    try:
        manifest = copy.deepcopy(manifest)
    except Exception:
        # Deep-copy must not break serving a report.
        return dict(manifest)
    if manifest.get("mode") != "server":
        manifest["mode"] = "server"
    datasets = manifest.get("datasets")
    if isinstance(datasets, list):
        for ds in datasets:
            if isinstance(ds, dict) and "rows" in ds and ds.get("remote") is not False:
                rows = ds.pop("rows", None) or []
                ds.setdefault("rowCount", len(rows))
    charts = manifest.get("charts")
    if isinstance(charts, list):
        for c in charts:
            if not isinstance(c, dict):
                continue
            ed = (c.get("editor") or {}).get("data")
            if isinstance(ed, dict) and "rows" in ed:
                ed2 = dict(ed)
                ed2.pop("rows", None)
                c.setdefault("editor", {})["data"] = ed2
            opt = c.get("option")
            if c.get("viewType") == "table" and isinstance(opt, dict):
                opt.pop("rows", None)
    return manifest


def _snapshot_stem(ds_id: str) -> str:
    """Filesystem-safe snapshot stem for a dataset id.

    Plain slugification COLLAPSES distinct CJK ids onto the same name
    (``R1__销售数据`` / ``R1__采购数据`` → ``R1``): two datasets of one report
    then share one snapshot file and reload swaps their data on restart. When
    slugification actually changed the id, append a short stable hash so every
    id keeps its own file; ASCII ids keep their historical name.
    """
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", str(ds_id or "")).strip("_") or "dataset"
    if stem != ds_id:
        import hashlib as _hashlib
        stem = f"{stem}-{_hashlib.sha1(str(ds_id).encode('utf-8')).hexdigest()[:8]}"
    return stem


def _report_dataset_ids(entry: dict | None) -> set[str]:
    """本报告**仍在使用**的 dataset id = manifest.datasets 的 id 集合。

    manifest 是用户数据源清单的唯一权威（图表全部绑定它）：编辑器删除数据源
    只改 manifest（removeDataset → syncManifest），注册表记账（entry.datasets）
    会留 stale 条目——真机孤儿表（PSI 下 335k 行的 PSI__ds-* 残留）正是
    「记账还在、manifest 已摘」的形状。若把记账也算「在用」，孤儿永远出不不了
    清单、也删不掉。记账只服务重载管道，不参与使用判定。
    """
    out: set[str] = set()
    if not isinstance(entry, dict):
        return out
    for d in ((entry.get("manifest") or {}).get("datasets") or []):
        if isinstance(d, dict) and d.get("id"):
            out.add(str(d["id"]))
    return out


def _snapshot_files_for(data_dir: Path, reportId: str, datasetId: str) -> list[Path]:
    """某数据集在本机数据目录里的全部快照/备份文件（显式后缀匹配，不裸前缀）。

    三代命名都要接住：构建期 ``snapshot_data_file(namespace=reportId__dsId)``
    → ``{ns}__{source.name}``；运行时 replace/append → ``{stem}.replaced.json``
    / ``{stem}.added.json``；行删除备份 → ``{stem}.pre-delete.json``。ns 与 stem
    都可能是 slug+hash 形状（``_snapshot_stem``/``snapshot_data_file`` 同一规则）。
    只匹配「stem + 已知后缀/分隔符」，``PSI__primary`` 的清理绝不误吃
    ``PSI__primaryX``（前缀后必须紧跟 ``.`` 或 ``__`` 才算命中）。
    """
    stems = {_snapshot_stem(datasetId)}
    ns = f"{reportId}__{datasetId}"
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", ns).strip("_")
    if slug and slug != ns:
        slug = f"{slug}-{hashlib.sha1(ns.encode('utf-8')).hexdigest()[:8]}"
    if slug:
        stems.add(slug)
    hits: list[Path] = []
    try:
        entries = list(Path(data_dir).iterdir())
    except OSError:
        return hits
    for p in entries:
        n = p.name
        for st in stems:
            if (n == f"{st}.replaced.json" or n == f"{st}.added.json"
                    or n == f"{st}.pre-delete.json" or n.startswith(f"{st}.")
                    or n.startswith(f"{st}__")):
                hits.append(p)
                break
    return hits


def _dir_file_bytes(d: Path) -> int:
    """Total bytes of regular files directly inside ``d`` (missing → 0)."""
    total = 0
    try:
        for p in d.iterdir():
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                pass
    except OSError:
        pass
    return total


def _report_disk_bytes(data_dir: Path, entry: dict) -> int:
    """One report's snapshot disk usage = byte sum of its dataset snapshot files.

    Files are collected with the SAME matcher the reload/cleanup pipeline uses
    (``_snapshot_files_for``: replaced/added/pre-delete/构建期 ns 前缀，json 与
    sqlite 两代后缀都接）, over both the registry accounting ids and the
    manifest dataset ids (记账可能落后于 manifest —— 真删以 manifest 为权威，
    磁盘占用则两边都算，报告主人看到的是真实占地的总和). Shared tables count
    once per report (dedup by path inside one report; cross-report attribution
    intentionally double-counts — each report's row answers「这份报告占多少」).
    """
    rid = str(entry.get("reportId") or "")
    ids: list[str] = []
    for d in (entry.get("datasets") or []):
        if isinstance(d, dict) and d.get("id") and str(d["id"]) not in ids:
            ids.append(str(d["id"]))
    for d in ((entry.get("manifest") or {}).get("datasets") or []):
        if isinstance(d, dict) and d.get("id") and str(d["id"]) not in ids:
            ids.append(str(d["id"]))
    seen: set[str] = set()
    total = 0
    for ds_id in ids:
        for p in _snapshot_files_for(data_dir, rid, ds_id):
            key = str(p)
            if key in seen:
                continue
            seen.add(key)
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def _physical_tables(engine: Engine) -> dict[str, dict[str, Any]]:
    """物理表清单：引擎已装载的表 + 存储文件里「活着但未装载」的表。

    ``engine.list_tables()`` 只回 _tables 缓存（重启后 _reload_all 只装 manifest
    声明的表）——数据源删掉后留在 SQLite 文件里的历史表必须从 sqlite_master /
    information_schema 捞出来，否则「数据表组全量清单」重启后永远看不见孤儿。
    未装载的表没有缓存 schema，行列数由 ``PRAGMA table_info`` / COUNT(*) 现算
    （best-effort，读不出来就给 0 并标 ``unloaded``）。
    """
    out: dict[str, dict[str, Any]] = {}
    for n in engine.list_tables():
        info = engine.table_info(n)
        out[n] = {"rows": info.row_count if info else 0,
                  "cols": len(info.columns) if info else 0}
    is_duck = str(getattr(engine, "engine_name", "")).lower().startswith("duck")
    try:
        if is_duck:
            rows = engine.execute(
                "SELECT table_name AS name FROM information_schema.tables "
                "WHERE table_schema = 'main'")
        else:
            rows = engine.execute(
                "SELECT name AS name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
        for r in rows:
            n = str(r.get("name") or "")
            if not n or n in out:
                continue
            shape = {"rows": 0, "cols": 0, "unloaded": True}
            try:
                cnt = engine.execute(f'SELECT COUNT(*) AS n FROM {_quote_ident(n)}')
                shape["rows"] = int(cnt[0]["n"]) if cnt else 0
                if is_duck:
                    cols = engine.execute(
                        "SELECT column_name AS name FROM information_schema.columns "
                        "WHERE table_schema = 'main' AND table_name = ?", (n,))
                else:
                    cols = engine.execute(f'PRAGMA table_info({_quote_ident(n)})')
                shape["cols"] = len(cols)
            except Exception:  # noqa: BLE001 — 形状读不出就按 0 展示，绝不 500 整个清单
                pass
            out[n] = shape
    except Exception:  # noqa: BLE001 — 元数据查询失败退回「仅已装载表」
        pass
    return out


def _sqlite_table_bytes(engine: Engine, name: str) -> int | None:
    """单表磁盘占用（SQLite dbstat 虚表，best-effort；DuckDB/无 dbstat → None）。"""
    if str(getattr(engine, "engine_name", "")).lower().startswith("duck"):
        return None
    try:
        rows = engine.execute(
            "SELECT SUM(pgsize) AS n FROM dbstat WHERE name = ?", (name,))
        n = rows[0].get("n") if rows else None
        return int(n) if n is not None else None
    except Exception:  # noqa: BLE001 — dbstat 编译选项缺失是常态，不是错误
        return None


def _quote_ident(name: str) -> str:
    from .aggregator import _quote  # noqa: PLC0415 — 模块底部 helper，避免顶层环
    return _quote(name)


def _prune_derived_datasets(engine, entry: dict, manifest: dict, registry: Any = None) -> list[str]:
    """Drop derived wide tables whose relation no longer exists in ``manifest``.

    A relation deleted in the browser (``dropRelationAt``) removes its wide
    dataset from the client manifest; the server must follow or the next
    restart would resurrect an orphan table. Returns the dropped dataset ids.

    BUT the wide table may be SHARED: report copies inherit the parent's
    ``datasets[]`` (same relation id + same wide dataset id), so another
    registered report can still declare the relation this one just dropped.
    Dropping the engine table would 400 every chart of those copies until the
    next restart — so when ``registry`` is given, only drop when NO other
    entry still declares the same dataset id as a relation-derived dataset
    (same "only shrink when orphan" doctrine as the derived-columns union).
    """
    live_relations = {r.get("id") for r in (manifest.get("relations") or []) if isinstance(r, dict)}

    def declared_elsewhere(ds_id: Any) -> bool:
        if registry is None or not ds_id:
            return False
        this_id = entry.get("reportId")
        for other in registry.raw_entries():
            if not isinstance(other, dict) or other.get("reportId") == this_id:
                continue
            for d in other.get("datasets") or []:
                if isinstance(d, dict) and d.get("id") == ds_id and _is_derived_ds(d):
                    return True
        return False

    kept, dropped = [], []
    for d in entry.get("datasets") or []:
        if not _is_derived_ds(d):
            kept.append(d)
            continue
        rel = (d.get("derived") or {}).get("relation") or {}
        if rel.get("id") in live_relations:
            kept.append(d)
            continue
        if declared_elsewhere(d.get("id")):
            kept.append(d)
            continue
        engine.drop_table(d.get("id"))
        dropped.append(d.get("id"))
    if dropped:
        entry["datasets"] = kept
        mds = manifest.get("datasets")
        if isinstance(mds, list):
            manifest["datasets"] = [d for d in mds if d.get("id") not in dropped]
    return dropped


def _sync_derived_columns(entry: dict, manifest: dict) -> None:
    """Align a registry entry's 计算列/维度值分组 declarations with the saved manifest.

    The manifest is the single source for ``dataset.derived.columns`` (and
    ``dataset.dimGroups``); the registry copy exists only so the reload pass can
    re-materialise them. A declaration removed in the browser must therefore
    leave the registry too (otherwise the next boot would resurrect it).
    Relation-derived datasets carry their own marker and are pruned elsewhere —
    never touched here.
    """
    decl_by_id: dict[str, Any] = {}
    groups_by_id: dict[str, Any] = {}
    for ds in manifest.get("datasets") or []:
        if isinstance(ds, dict) and ds.get("id"):
            decl_by_id[ds["id"]] = ds.get("derived")
            groups_by_id[ds["id"]] = ds.get("dimGroups")
    for d in entry.get("datasets") or []:
        if not isinstance(d, dict):
            continue
        kind = (d.get("derived") or {}).get("kind")
        if kind == "relation" or d.get("id") not in decl_by_id:
            continue
        def _retire(names: list[str]) -> None:
            # 引擎表按「永不删列」纪律保留着被退役的物化列；这里把名字记进
            # retiredColumns，h_dataset_replace 的列集比对据此把它们从「上传缺列」
            # 里豁免（否则删掉一个计算列/维度分组定义后，原位替换数据被孤儿列
            # 永久卡死 400，UI 没有出路）。
            if not names:
                return
            merged = list(dict.fromkeys([str(c) for c in (d.get("retiredColumns") or [])]
                                        + [str(n) for n in names if n]))
            if merged:
                d["retiredColumns"] = merged

        decl = columns_decl(decl_by_id[d["id"]])
        if decl:
            d["derived"] = decl
        elif kind == "columns":
            _retire([str(x.get("name")) for x in columns_decl(d.get("derived")) if isinstance(x, dict)])
            d.pop("derived", None)
        # 维度值分组声明与计算列同一条同步纪律（浏览器删了定义，注册表也要删，
        # 否则下次 boot 复活——replay_all 读的就是这里）。
        groups = dim_groups_defs(groups_by_id[d["id"]])
        if groups:
            d["dimGroups"] = groups
        else:
            _retire([str(g.get("name")) for g in (dim_groups_defs(d.get("dimGroups")) or [])])
            d.pop("dimGroups", None)


def _is_derived_ds(d: dict) -> bool:
    """A registry dataset entry with no source file — rebuilt from a relation.

    NOTE: a 计算列 ('columns') entry is NOT this kind — it keeps its raw source
    and is reloaded normally; its declaration is replayed in the second pass
    (store.replay_all — former server/derived_columns.py), never here.
    """
    return isinstance(d, dict) and (d.get("derived") or {}).get("kind") == "relation"


def _reload_dataset(engine, d: dict, registry: Registry | None = None) -> TableInfo | None:
    """Re-read a dataset from disk and reload into the engine."""
    src = d.get("source")
    if not src:
        return None
    from pathlib import Path
    p = Path(src)
    if not p.is_absolute() and registry is not None:
        p = registry.path.parent / p
    if not p.exists():
        return None
    # XP-01：装载统一走 store.engine_load_from_snapshot——.json 走 canonical
    # 装载器（历史行为逐位一致），.sqlite 快照走 ATTACH 拷贝，csv/xlsx 源拷贝
    # 照旧；DuckDB 等无该能力的引擎自动落回旧路径。失败语义与旧实现对齐：
    # 快照读不出/不存在 → None（该数据集跳过，不 500 整个重载，引擎旧表原样
    # 保留——先解析后落表）；表结构错误（TableSchemaError，旧 load_table 的
    # 传播类）照旧向上抛。DROP+CREATE 由 load_table/_load_sqlite_snapshot 内部
    # 完成，此处不再显式 pre-drop（旧实现 pre-drop 会把「解析失败」放大成
    # 「连旧表一起丢」，非本批语义）。
    from .store import engine_load_from_snapshot
    try:
        # 索引列声明随注册表 meta 透传（h_register 持久化）：重载是 DROP+CREATE，
        # 不带回 = 索引悄悄消失（与 run_server._reload_all 同一条契约）。
        return engine_load_from_snapshot(engine, d["id"], p,
                                         index_columns=list(d.get("indexColumns") or []))
    except TableSchemaError:
        raise
    except Exception:
        return None


def _namespace_manifest(manifest: dict, old_id: str, new_id: str) -> dict:
    """Copy a manifest and replace dataset references for report isolation."""
    out = copy.deepcopy(manifest)
    def walk(value):
        if isinstance(value, dict):
            for key, child in list(value.items()):
                if key in {"datasetId", "dataset"} and child == old_id:
                    value[key] = new_id
                else:
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(out)
    for dataset in out.get("datasets", []):
        if isinstance(dataset, dict) and dataset.get("id") == old_id:
            dataset["id"] = new_id
    return out


def _extract_declared_series(chart: dict) -> list:
    """Pull the data points from chart.option.series for truth comparison."""
    series = chart.get("option", {}).get("series", [])
    if not series:
        return []
    s = series[0]
    return s.get("data") or []


# ---------------------- HTTP server ---------------------------------------------

_server_started_at_value: float = time.time()


def _server_started_at() -> float:
    return _server_started_at_value


def _copilot_rate_limit(handler: BaseHTTPRequestHandler, report_id: str) -> tuple[bool, int]:
    """Shared report/client guard; fall back to process-local tests when unconfigured."""
    key = f"{report_id}:subject:{_agent_subject_hash(handler)}"
    if getattr(_jobs, "_PERSIST_PATH", None) is not None:
        return _jobs.consume_rate_limit(key, _COPILOT_RATE_LIMIT, int(_COPILOT_RATE_WINDOW_SECONDS))
    now = time.monotonic()
    with _COPILOT_RATE_LOCK:
        bucket = [stamp for stamp in _COPILOT_RATE.get(key, [])
                  if now - stamp < _COPILOT_RATE_WINDOW_SECONDS]
        if len(bucket) >= _COPILOT_RATE_LIMIT:
            retry = max(1, math.ceil(_COPILOT_RATE_WINDOW_SECONDS - (now - bucket[0])))
            _COPILOT_RATE[key] = bucket
            return False, retry
        bucket.append(now)
        _COPILOT_RATE[key] = bucket
        if len(_COPILOT_RATE) > 1024:
            stale = [k for k, values in _COPILOT_RATE.items()
                     if not values or now - values[-1] >= _COPILOT_RATE_WINDOW_SECONDS]
            for stale_key in stale[:512]:
                _COPILOT_RATE.pop(stale_key, None)
        return True, 0


def run_server(
    host: str,
    port: int,
    registry: Registry,
    engine: Engine | None = None,
    template_provider: Any = None,
) -> None:
    """Start the threaded HTTP server (blocks)."""
    engine = engine or get_engine()
    router = make_handlers(engine, registry)

    class Handler(BaseHTTPRequestHandler):
        # Quieter logs
        def log_message(self, fmt, *args):
            return  # silence default stderr logging

        def end_headers(self):
            # One bounded correlation id per HTTP request.  Clients may supply
            # an id for distributed tracing, but never arbitrary newlines or
            # unbounded log/header data.  Returning it on errors too makes a
            # failed Agent turn diagnosable without exposing prompts/rows.
            trace = getattr(self, "trace_id", "")
            if trace:
                self.send_header("X-Trace-Id", trace)
            super().end_headers()

        def _route(self, method):
            supplied_trace = str(self.headers.get("X-Trace-Id") or "").strip()
            self.trace_id = supplied_trace if re.fullmatch(r"[A-Za-z0-9._:-]{1,96}", supplied_trace) else uuid.uuid4().hex
            url = urlparse(self.path)
            path = url.path
            if path == "/api/v1":
                path = "/api"
            elif path.startswith("/api/v1/"):
                path = "/api/" + path[len("/api/v1/"):]
            res = router.resolve(method, path)
            if res is None:
                respond_error(self, 404, "NotFound", f"no route for {method} {url.path}")
                return
            fn, params = res
            # Percent-decode path params. Route MATCHING intentionally stays on
            # the raw path (patterns are just [^/]+, so a %2F can never smuggle
            # in an extra segment), but handlers must see the real name: a
            # datasetId or column can be non-ASCII (a merge wide table is named
            # by the user: 销售×库存). Decoding here fixes EVERY route at once,
            # instead of each handler remembering its own unquote().
            params = {k: unquote(v) for k, v in params.items()}
            try:
                fn(self, **params)
            except BadRequest as e:
                respond_error(self, 400, "BadRequest", str(e))
            except TableSchemaError as e:
                respond_error(self, 400, "BadRequest", str(e))
            except AggregationError as e:
                respond_error(self, 400, "BadRequest", str(e))
            except NotFound as e:
                respond_error(self, 404, "NotFound", str(e))
            except Conflict as e:
                respond_error(self, 409, "Conflict", str(e))
            except ValidationFailed as e:
                respond_error(self, 422, "ValidationFailed", str(e))
            except Exception as e:  # pragma: no cover - defensive
                import traceback
                respond_error(self, 500, "InternalError", f"{type(e).__name__}: {e}")
                if not getattr(self.server, "quiet", False):  # type: ignore[attr-defined]
                    traceback.print_exc()

        def do_GET(self):    self._route("GET")
        def do_POST(self):   self._route("POST")
        def do_PUT(self):    self._route("PUT")
        def do_DELETE(self): self._route("DELETE")

    class Server(ThreadingHTTPServer):
        allow_reuse_address = True
        daemon_threads = True

    server = Server((host, port), Handler)
    server.engine = engine          # type: ignore[attr-defined]
    server.registry = registry      # type: ignore[attr-defined]
    server.template_provider = template_provider  # type: ignore[attr-defined]
    server.routes = router          # type: ignore[attr-defined]

    global _server_started_at_value
    _server_started_at_value = time.time()

    # Announce this server in the global machines-local index so the 报告管理
    # page (on ANY of this machine's report servers) can list + stop it. The
    # entry is removed on shutdown; a crashed server's stale entry is pruned
    # lazily by h_reports_all's health ping.
    index_register(port, registry.path, os.getpid())

    print(f"[ai-report-server] listening on http://{host}:{port}  (engine={engine.engine_name})")
    print(f"[ai-report-server] 本机访问 http://127.0.0.1:{port}/reports/<id>   "
          f"局域网访问 http://{_lan_ip()}:{port}/reports/<id>   "
          f"报告管理 http://127.0.0.1:{port}/manager")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[ai-report-server] shutting down...")
        server.shutdown()
    finally:
        index_unregister(port)
