#!/usr/bin/env python3
"""Deterministic, Windows-safe report build pipeline.

The Agent writes a constrained v1 request; this script owns every mechanical
step. Commands are executed as argv lists (never ``shell=True``), profiling is
performed before auto-mode selection, and the result is reported as
``ready``, ``needs_audit`` or ``failed``.
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
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import re
import secrets
from pathlib import Path
from typing import Any

SKILL = Path(__file__).resolve().parent.parent
SCRIPTS = Path(__file__).resolve().parent

DEFAULT_SINGLEFILE_ROWS = 100_000
DEFAULT_SINGLEFILE_BYTES = 50 * 1024 * 1024
# Date-based skill version, printed on every build. Skill copies deployed to
# other machines (opencode/weclaw skills dirs) drift silently; a stale copy
# shows old UI (语义配置/更新数据, no 汇报模式) and old validator messages.
# When behavior looks wrong, compare this line against the repo checkout.
# 单一真源 = server/__init__.py 的 SKILL_VERSION（health/管理页同源）；每个落地批次 bump。
if str(SKILL) not in sys.path:
    sys.path.insert(0, str(SKILL))
from server import SKILL_VERSION  # noqa: E402
from server.loopback import urlopen_loopback  # noqa: E402
_WINDOWS_RESERVED_RE = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$", re.IGNORECASE)


def _safe_filename(name: str, fallback: str = "报告") -> str:
    r"""Turn a report title into a safe, human-readable folder name.

    Strips characters that are illegal on Windows (\/:*?"<>|) and any path
    separators, trims whitespace/dots, and falls back to an ASCII id if the
    result would be empty. This is the folder that contains the report and its
    build artifacts (``<title>/build/`` + ``<title>/<title>.html``)."""
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '', name).strip().strip('.').strip()
    if not cleaned:
        return re.sub(r'[^A-Za-z0-9_-]', '', fallback) or "report"
    if _WINDOWS_RESERVED_RE.fullmatch(cleaned):
        return "_" + cleaned[:119]
    return cleaned[:120]
DEFAULT_SINGLEFILE_CARDINALITY = 2_000
DEFAULT_SERVER_ROWS = 5_000_000
DEFAULT_SERVER_COLUMNS = 200
DEFAULT_SERVER_URL = "http://127.0.0.1:8765"

# 便携版 Python 自举制品（可移植启动器 2026-09-25）：URL + SHA256 逐字 pin。
# 哈希来自 2026-09-25 真实下载（python.org embeddable + astral-sh/python-build-
# stand-alone 20241016 install_only_stripped），不是文档值——换版本必须重新下载
# 计算并同步更新 launcher 模板里的文件名。embeddable 含 _sqlite3.pyd + sqlite3.dll
# + python312.zip 内 sqlite3 包装包（已核对），server 零 pip 依赖即可跑。
_PORTABLE_PY_SHA = {
    "win_amd64": "8d3f33be9eb810f23c102f08475af2854e50484b8e4e06275e937be61ce3d2fb",
    "win_arm64": "d34db37675973785a2a539cd1c8dde1b6d45665f48c615ef55274b3798bf9fd3",
    "mac_arm64": "95dd397e3aef4cc1846867cf20be704bdd74edd16ea8032caf01e48f0c53d65d",
    "mac_x64": "848405b92bda20fad1f9bba99234c7d3f11e0b31e46f89835d1cb3d735e932aa",
    "linux_x64": "3a4d53a7ba3916c0c1f35cbbe57068e2571b138389f29cf5c35367fec8f4c617",
    "linux_arm64": "c8f5ed70ee3c19da72d117f7b306adc6ca1eaf26afcbe1cc1be57d1e18df184c",
}



def _release_build_memory() -> None:
    """Best-effort GC before the browser audit.

    ``report_build`` has already held ~1GB of Python objects (335k rows x 11
    columns) by the time it launches the audit, and the audit boots a headless
    renderer that also allocates heavily (ECharts + a cloned presentation deck +
    an in-page PPTX zip). When both peaks overlap the renderer stops answering
    CDP while the machine swaps: 真机观察到审计 8 分钟不返回、renderer CPU 0%、
    也没有任何原生对话框 —— 纯内存/IO 饥饿。
    ``main``'s own locals stay alive (the table is still needed for the HTML), so
    this only drops what is already unreferenced; it is cheap and helps.
    """
    import gc

    gc.collect()


def run(argv: list[str], cwd: Path, timeout: int = 900) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    # ``start_new_session`` puts the child (and everything it spawns — notably the
    # headless Chrome an audit launches) in its own process group, so a timeout
    # can kill the WHOLE tree. Without it, subprocess only SIGKILLs node and every
    # orphaned headless Chrome keeps running: after a couple of timed-out audits
    # the machine has a dozen debug-port Chromes competing for memory and every
    # later audit wedges (真机踩过：1200s 超时 + 9 个残留 Chrome).
    try:
        return subprocess.run(
            argv, cwd=str(cwd), shell=False, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, env=env,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired as exc:
        _kill_process_group(exc)
        raise


def _kill_process_group(exc: subprocess.TimeoutExpired) -> None:
    pid = getattr(exc, "pid", None)
    if not pid:
        return
    import signal as _signal
    for sig in (_signal.SIGTERM, _signal.SIGKILL):
        try:
            os.killpg(os.getpgid(pid), sig)
        except Exception:
            return
        try:
            os.waitpid(pid, os.WNOHANG)
        except Exception:
            pass


def _json_size(table: dict[str, Any]) -> int:
    return len(json.dumps(table, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def choose_mode(table: dict[str, Any], requested: str, *, max_rows: int,
                max_bytes: int, max_cardinality: int, roles: dict | None = None,
                lookup: bool = False) -> str:
    roles = roles or {}
    rows = table.get("rows") or []
    if len(rows) > DEFAULT_SERVER_ROWS or len(table.get("columns") or []) > DEFAULT_SERVER_COLUMNS:
        raise ValueError(
            f"数据超过首版 server 上限（{DEFAULT_SERVER_ROWS:,} 行 / {DEFAULT_SERVER_COLUMNS} 列），"
            "请先预聚合或拆分数据"
        )
    # An explicit server request has no reason to serialize the full table to
    # estimate the singlefile payload. Auto mode also short-circuits once the
    # row count alone rules singlefile out; this matters for hundreds of
    # thousands of rows on a resource-constrained workstation.
    if requested == "server":
        return "server"
    if requested == "auto" and len(rows) > max_rows:
        return "server"
    # Cardinality governs the singlefile payload size: high-cardinality
    # *chips/filters* blow up the embedded manifest. An ``id`` or ``ignore``
    # column is never rendered as a filter chip or chart dimension (it is an
    # entity key), so it must NOT push a report into server mode — otherwise a
    # 50k-row order table with a unique order-ID column could never ship as a
    # portable single-file. Only category/time columns actually used as
    # dimensions here count.
    types = (table.get("meta") or {}).get("types") or {}
    # Prefer the deterministic per-column cardinality from the profile (works
    # for both the shape table and the real canonical table). Fall back to
    # row-iteration only when a real table is present and cardinality is absent.
    card = (table.get("meta") or {}).get("cardinality") or {}
    high_card = 0
    # A lookup (enrichment-only) table never drives chart dimensions, so its key
    # cardinality must not push a report into server mode (same rationale as an
    # ``id``/``ignore`` column). Its rows/bytes still count towards the portable
    # single-file payload below — only the cardinality gate is skipped.
    for index, column in ([] if lookup else enumerate(table.get("columns") or [])):
        t = types.get(column)
        role = roles.get(column)
        if role in {"id", "ignore"}:
            continue
        if t in {"category", "time"} or role in {"dimension", "time"}:
            if card.get(column) is not None:
                high_card = max(high_card, int(card.get(column) or 0))
            else:
                values = {str(row[index]) for row in rows if isinstance(row, (list, tuple)) and index < len(row) and row[index] not in (None, "")}
                high_card = max(high_card, len(values))
    json_bytes = _json_size(table)
    fits_singlefile = json_bytes <= max_bytes and high_card <= max_cardinality
    if requested == "singlefile":
        # 显式 singlefile 同样受行数门约束：报错文案一直写着「行数 X/max_rows」，
        # 实现却只查字节/基数——40 万行 + --mode singlefile 会静默嵌入全量 JSON。
        if len(rows) > max_rows:
            raise ValueError(
                f"singlefile 超出行数上限（{len(rows):,}/{max_rows:,}）；"
                "请改用 server 或先预聚合"
            )
        if not fits_singlefile:
            raise ValueError(
                f"singlefile 超出上限（JSON {_json_size(table):,}/{max_bytes:,} bytes，"
                f"最高基数 {high_card:,}/{max_cardinality:,}）；"
                "请改用 server 或先预聚合"
            )
        return "singlefile"
    return "singlefile" if fits_singlefile else "server"


def profile_shape(profile: dict[str, Any]) -> dict[str, Any]:
    """Build the small shape needed for mode selection without raw rows.

    Carries per-column cardinality (from the profile's ``distinct`` count) so
    the mode decision never needs to iterate real rows — which avoids both the
    premature canonical-JSON build (Issue #4) and iterating None placeholders.
    """
    columns = profile.get("columns") or []
    return {
        "columns": [item.get("name", "") for item in columns],
        "rows": [None] * int(profile.get("rowCount", 0)),
        "meta": {
            "types": {item.get("name", ""): item.get("type") for item in columns},
            # Per-column cardinality from the deterministic profile.
            "cardinality": {item.get("name", ""): int(item.get("distinct") or 0) for item in columns},
            "quality": profile.get("quality") or {},
            # Compact per-column stats (非空/缺失/不同值/范围/Top) — server mode has
            # no rows in the manifest, so the data-source UI can only show what the
            # profile measured at build time (same map the canonical JSON carries).
            "stats": profile.get("columnStats") or {},
        },
    }


def _audit_diagnostics(report: dict | None) -> list[dict[str, str]]:
    """Stable, machine-readable findings for a planning/repair Agent.

    Human-readable audit messages remain evidence, never executable repair
    instructions. The Agent may retry transient infrastructure failures, but
    must not rewrite measures or filters to make a red data-truth check green.
    """
    if not report:
        return []
    findings = []
    if report.get('error'):
        findings.append({'check': 'audit-runner', 'kind': 'infrastructure',
                         'detail': str(report['error'])[:1000], 'repair': 'retry-audit'})
    for name, value in (report.get('checks') or {}).items():
        if value is True or value is None or (isinstance(value, str) and value.startswith('skip:')):
            continue
        # Data-truth/semantic mismatches are unsafe to auto-repair. Rendering
        # and CDP timeouts may be retried, but never silently published.
        kind = 'data-truth' if any(s in name.lower() for s in ('datatruth', 'semantic', 'formatter')) else 'behavior'
        findings.append({'check': str(name), 'kind': kind, 'detail': str(value)[:1000],
                         'repair': 'manual-review' if kind == 'data-truth' else 'inspect-runtime'})
    if not report.get('passed') and not findings:
        findings.append({'check': 'audit-runner', 'kind': 'infrastructure',
                         'detail': str(report.get('scope') or 'no executed checks')[:1000],
                         'repair': 'retry-audit'})
    return findings


def _write_status(path: Path, status: str, *, mode: str, messages: list[str],
                  audit: dict | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"status": status, "mode": mode, "messages": messages,
               "phase": "publish" if status == "ready" else "audit" if status == "needs_audit" else "failed"}
    if audit is not None:
        payload["audit"] = {"passed": bool(audit.get("passed")),
                            "scope": audit.get("scope"), "aborted": bool(audit.get("aborted"))}
        payload["diagnostics"] = _audit_diagnostics(audit)
    path.write_text(json.dumps(payload,
                               ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _run_input_lint(inputs: list[Path], workdir: Path, *, strict: bool,
                    gate: bool = True) -> tuple[dict | None, bool]:
    """WP-0A 输入契约体检（docs/product-iteration-plan.md §3 WP-0A）。

    ``--table``（及 ``datasets[].file``）进编译前做一次「严格二维表」体检：
    结果写 ``<workdir>/input-lint.json`` 并打印控制台。

    ``gate=False``（缺省，直通模式，2026-09-18 用户裁决）：体检**只出报告**——
    结构破坏/质量警告一律不拦截构建，控制台摘要注明「--sort 可开启整理门禁」。
    ``gate=True``（``--sort`` 整理模式）：结构破坏（fail）拦截构建，
    ``--strict-input`` 把质量警告（warn）也升为失败。体检器自身不可用
    （导入失败）只告警不拦截——不让守门工具的故障挡住本来健康的构建。
    返回 ``(doc, ok)``；``doc`` 为 None 表示体检器不可用（已跳过）。
    """
    try:
        sys.path.insert(0, str(SCRIPTS))
        import table_input_lint
    except Exception as exc:
        print(f"[input-lint] 体检器不可用，跳过：{exc}", file=sys.stderr)
        return None, True
    reports = []
    for p in inputs:
        try:
            reports.append(table_input_lint.lint_file(p))
        except Exception as exc:  # lint_file 自身不抛；这里是绝对兜底
            reports.append({"file": str(p), "ok": False, "failCount": 1, "warnCount": 0,
                            "fail": [{"check": "unreadable",
                                      "message": f"输入体检无法读取：{type(exc).__name__}: {exc}"}],
                            "warn": [], "columns": [],
                            "shape": {"columnCount": 0, "dataRows": 0}})
    doc = {"version": table_input_lint.LINT_VERSION, "strict": bool(strict),
           "ok": all(r.get("ok") for r in reports), "files": reports}
    out = workdir / "input-lint.json"
    try:
        out.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"[input-lint] input-lint.json 写入失败：{exc}", file=sys.stderr)
    for r in reports:
        shape = r.get("shape") or {}
        rows = shape.get("dataRows")
        rows_txt = f"{rows:,}" if isinstance(rows, int) else "?"
        print(f"[input-lint] {Path(str(r.get('file') or '')).name}: "
              f"{shape.get('columnCount', '?')} 列 × {rows_txt} 行 — "
              f"FAIL {r.get('failCount', 0)} / WARN {r.get('warnCount', 0)}"
              + ("" if gate else "（仅报告，不拦截；--sort 开启整理门禁）"))
        for f in (r.get("fail") or [])[:10]:
            print(f"[input-lint]   FAIL [{f.get('check')}] {f.get('message')}")
        for w in (r.get("warn") or [])[:10]:
            print(f"[input-lint]   WARN [{w.get('check')}] {w.get('message')}")
    total_warn = sum(r.get("warnCount", 0) for r in reports)
    if not gate:
        # 直通模式：体检是纯报告（数据一个值不改、构建绝不拦截）。
        # strict 参数在直通模式下无意义（没有门禁可升级），忽略。
        return doc, True
    ok = bool(doc["ok"]) and (not strict or total_warn == 0)
    return doc, ok


def _inherit_args(workdir: Path, report_id: str | None) -> list[str]:
    """月度刷新继承（增量 upsert）：把上一份 manifest 物化成 ``prev-manifest.json``
    传给编译器。来源 = registry.json 的在线条目（在线保存/覆盖后的最新版）优先，
    回退构建侧车 manifest.json。继承是增益：挑选/落盘任何失败都静默跳过，绝不
    让刷新构建因此失败。"""
    try:
        sys.path.insert(0, str(SCRIPTS))
        from curation_inherit import pick_prev_manifest
        prev = pick_prev_manifest(workdir, report_id)
    except Exception:
        return []
    if not isinstance(prev, dict):
        return []
    out = workdir / "prev-manifest.json"
    try:
        out.write_text(json.dumps(prev, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError:
        return []
    return ["--inherit", str(out)]


def _audit_report(node: str, audit_target: str, workdir: Path) -> dict:
    """Run the headless browser audit and return its report dict.

    A timeout becomes a FAILED report instead of an uncaught TimeoutExpired:
    the bare traceback used to crash the build AFTER the artifacts were written
    and left build-status.json at the PREVIOUS build's value (possibly "ready")
    — exactly the state --audit-required must never fabricate."""
    try:
        audit = run([node, str(SCRIPTS / "audit_editor.mjs"), audit_target], cwd=workdir, timeout=1200)
    except subprocess.TimeoutExpired:
        report = {"passed": False,
                  "error": "browser audit timed out after 1200s（headless 进程组已收割）；"
                           "产物已生成，请清理残留 Chrome 后单独重跑审计确认"}
        (workdir / "browser-audit.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return report
    try:
        report = json.loads(audit.stdout)
    except Exception:
        report = {"passed": False, "error": audit.stdout[-1000:] or audit.stderr[-1000:]}
    (workdir / "browser-audit.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def _unregister_staged_report(server_base: str, report_id: str) -> None:
    """Best-effort cleanup of an isolated canary, never the published id."""
    req = urllib.request.Request(
        server_base.rstrip('/') + '/api/admin/registry/unregister',
        data=json.dumps({'reportId': report_id}).encode('utf-8'),
        headers={'Content-Type': 'application/json'}, method='POST')
    with urlopen_loopback(req, timeout=30) as resp:
        resp.read()
    # h_unregister drops engine tables but deliberately retains normal report
    # snapshots for recovery. A one-shot canary has no recovery value: remove
    # only its random, private namespace after confirmed unregister, otherwise
    # every 335k-row audit would leak another ~40 MiB in the central state.
    for path in (_state_home() / 'data').glob(report_id + '__*'):
        if path.is_file():
            path.unlink(missing_ok=True)


def _register_single_server(server_base: str, table_path: Path, spec_path: Path,
                            report_id: str, profile_json: Path, inherit_argv: list[str],
                            workdir: Path) -> subprocess.CompletedProcess:
    return run([sys.executable, str(SCRIPTS / 'serve_report.py'), '--server', server_base,
                'register', '--data', str(table_path), '--spec', str(spec_path),
                '--report-id', report_id, "--profile", str(profile_json),
                '--mode', 'server'] + inherit_argv, cwd=workdir, timeout=1200)


def _post_registry_payload(server_base: str, payload: bytes) -> dict:
    req = urllib.request.Request(server_base.rstrip('/') + '/api/admin/registry/register',
                                 data=payload, headers={'Content-Type': 'application/json'},
                                 method='POST')
    with urlopen_loopback(req, timeout=300) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _canary_multi_body(body: dict, stage_id: str) -> dict:
    """Clone only metadata; raw 335k-row tables remain shared until POST completes."""
    final_id = body['reportId']
    ids = {d['id']: d['id'].replace(final_id + '__', stage_id + '__', 1)
           for d in body['datasets']}
    def remap(value):
        if isinstance(value, str):
            return ids.get(value, value)
        if isinstance(value, list):
            return [remap(v) for v in value]
        if isinstance(value, dict):
            return {k: remap(v) for k, v in value.items()}
        return value
    manifest = remap(body['manifest'])
    manifest['reportId'] = stage_id
    return {'reportId': stage_id, 'title': body['title'], 'manifest': manifest,
            'multi': True,
            'datasets': [{k: (v if k == 'table' else remap(v)) for k, v in d.items()}
                         for d in body['datasets']]}


def _safe_report_id(title: str) -> str:
    """ASCII-safe reportId fallback for non-technical (often Chinese) titles.

    The server registry requires ``^[A-Za-z][A-Za-z0-9_-]{1,79}$`` — a raw
    Chinese title would fail registration with HTTP 400, forcing the model to
    hand-write a reportId. Slug the title deterministically instead."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", (title or "")).strip("-")
    slug = re.sub(r"^[^A-Za-z]+", "", slug).strip("-")  # must start with a letter
    if not slug:
        slug = "report"
    if len(slug) < 2:
        # registry 正则要求 ≥2 字符：单个字母的 slug（"C端…" → "C"）必然 400。
        slug = f"{slug}-report"
    return slug[:79]


def _resolve_report_id(args, spec) -> str:
    """Explicit id first (validated!), then spec, then the title slug.

    An INVALID explicit id used to sail through to registration and die there
    with a bare HTTP 400; fail here with an actionable message instead."""
    rid = args.report_id or spec.get("reportId") or _safe_report_id(spec.get("title") or "")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{1,79}", rid):
        raise ValueError(
            f"reportId {rid!r} 不合法（需字母开头、2-80 位字母/数字/-/_）；"
            "请用 --report-id 或请求顶层 reportId 指定一个 ASCII id")
    return rid


def _lan_ip() -> str:
    """Best-effort LAN address of this machine (for shareable report URLs)."""
    import socket
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


def _print_server_access(server_base: str, report_id: str) -> None:
    """Print every URL + token a non-technical user needs after a server build.

    The console (and SKILL.md guidance) is the ONLY place the author learns
    how to reach/share/stop the report — make it complete: the exact report URL
    (统一 manager 后根路径打开的是注册表第一份报告，不再是「本报告」，所以主链
    接恒用 /reports/<id> 精确地址), the LAN URL, and the manager page. The edit
    password is printed by the caller when set."""
    port = server_base.rsplit(":", 1)[-1]
    print(f"[open] 打开报告（本机）：{server_base}/reports/{report_id}"
          f"（根路径 {server_base} 打开本机默认报告）")
    print(f"[open] 局域网访问：http://{_lan_ip()}:{port}/reports/{report_id}"
          f"（同一网络内的其他电脑/手机直接打开即可；需保持本机开机且服务运行）")
    print(f"[open] 报告管理（查看/关闭本机所有运行中的报告）：{server_base}/manager")


def _ship_portable_data(workdir: Path, report_id: str) -> None:
    """把已注册报告的状态快照随包出货（可移植自举 2026-09-25）。

    注册成功后，state home（``~/.ai-html-report/data/``）里落着该报告的数据
    快照（``{report_id}__*``）；把它们拷进 ``build/data/``，并把注册表里的
    **权威 manifest**（final 数据集 id/rowCount/runtime 口令块）覆盖写到
    ``build/manifest.json``。没有这一步，报告文件夹打包发给别人的时候不带
    数据——纯 server 构建本不落 build/data/，收报告人的 ``--register-report``
    （便携启动器冷启动）就无从注册。失败只降级（打印警告），绝不拖垮构建。
    """
    home = _state_home()
    data_dir = home / "data"
    dest = workdir / "data"
    shipped = 0
    try:
        if data_dir.exists():
            dest.mkdir(parents=True, exist_ok=True)
            for f in sorted(data_dir.glob(f"{report_id}__*")):
                if f.is_file():
                    shutil.copy2(f, dest / f.name)
                    shipped += 1
    except OSError as exc:
        print(f"[portable] 数据快照随包出货失败（不影响本机使用）：{exc}", file=sys.stderr)
    try:
        reg_path = home / "registry.json"
        reg = json.loads(reg_path.read_text(encoding="utf-8")) if reg_path.exists() else {}
        entry = reg.get(report_id) if isinstance(reg, dict) else None
        if isinstance(entry, dict) and isinstance(entry.get("manifest"), dict):
            (workdir / "manifest.json").write_text(
                json.dumps(entry["manifest"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError) as exc:
        print(f"[portable] 权威 manifest 出货失败（不影响本机使用）：{exc}", file=sys.stderr)
    if shipped:
        print(f"[portable] 已随包出货 {shipped} 个数据快照 + 权威 manifest → build/data/")


def _write_portable_artifacts(workdir: Path, *, mode: str, report_id: str,
                              draft: Path, output: Path, table: dict[str, Any],
                              server_base: str = DEFAULT_SERVER_URL,
                              spec: dict[str, Any] | None = None,
                              patch_keys: list[str] | None = None,
                              build_messages: list[str] | None = None,
                              has_prev_manifest: bool = False) -> None:
    """Write the small, inspectable sidecar bundle next to every build.

    The HTML remains the user-facing artifact. Sidecars make the output useful
    on Windows without requiring the Agent to reopen a large HTML file: a
    manifest snapshot, quality report, provenance and copyable cmd launchers.
    """
    source = output if mode == "singlefile" else draft
    text = source.read_text(encoding="utf-8")
    match = re.search(r'<script id="ai-report-manifest" type="application/json">(.*?)</script>', text, re.S)
    manifest: dict[str, Any] = {}
    if match:
        try:
            manifest = json.loads(match.group(1))
        except json.JSONDecodeError:
            manifest = {}
    if mode == "server":
        manifest["mode"] = "server"
        manifest.setdefault("serverBase", "/api/v1")
        for dataset in manifest.get("datasets", []):
            rows = dataset.pop("rows", None)
            if rows is not None:
                dataset["rowCount"] = len(rows)
        for chart in manifest.get("charts", []):
            data = (chart.get("editor") or {}).get("data") or {}
            data.pop("rows", None)
            chart.setdefault("editor", {})["data"] = data
    (workdir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    quality = (table.get("meta") or {}).get("quality", {})
    (workdir / "quality.json").write_text(json.dumps({
        "rowCount": len(table.get("rows") or []), "columnCount": len(table.get("columns") or []),
        "missingOrInvalid": quality,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    columns = table.get("columns") or []
    rows = table.get("rows") or []
    quality_md = ["# 数据质量报告", "", f"- 数据行数：{len(rows):,}", f"- 字段数：{len(columns):,}", "", "## 字段概览", "", "| 字段 | 非空数 | 空值数 | 示例 |", "|---|---:|---:|---|"]
    schema_only = bool(rows) and all(not isinstance(row, (list, tuple)) for row in rows[: min(20, len(rows))])
    for index, column in enumerate(columns):
        if schema_only:
            safe_column = str(column).replace("|", "\\|")
            quality_md.append(f"| {safe_column} | — | — | server 模式仅保留 schema |")
            continue
        values = [row[index] if isinstance(row, (list, tuple)) and index < len(row) else None for row in rows]
        non_empty = sum(v not in (None, "") for v in values)
        missing = len(values) - non_empty
        sample = next((str(v).replace("|", "\\|") for v in values if v not in (None, "")), "")[:80]
        safe_column = str(column).replace("|", "\\|")
        quality_md.append(f"| {safe_column} | {non_empty:,} | {missing:,} | {sample} |")
    if quality:
        quality_md += ["", "## 编译器标记", "", "```json", json.dumps(quality, ensure_ascii=False, indent=2), "```"]
    else:
        quality_md += ["", "未发现需要特别处理的值级质量问题。"]
    (workdir / "data-quality.md").write_text("\n".join(quality_md) + "\n", encoding="utf-8")
    # 决策清单（系统默认 vs 作者声明）：业务读者据此知道还有什么可改、改哪一层。
    # 内层独立兜底——账目失败只丢 decisions.md，不拖累 quality/README 等其余侧车。
    try:
        from decision_ledger import write_ledger
        write_ledger(workdir / "decisions.md", spec or {}, manifest, mode=mode,
                     patch_keys=patch_keys, build_messages=build_messages,
                     has_prev_manifest=has_prev_manifest)
    except Exception as exc:
        print(f"[decisions] 决策清单生成失败（不影响构建）：{exc}", file=sys.stderr)
    provenance = manifest.get("provenance") or {}
    provenance.setdefault("reportId", manifest.get("reportId") or report_id)
    provenance.setdefault("mode", mode)
    provenance.setdefault("rowCount", len(table.get("rows") or []))
    (workdir / "provenance.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    readme = f"""# AI 报告构建产物

- 模式：`{mode}`
- 报告 ID：`{report_id}`
- 数据行数：`{len(table.get('rows') or []):,}`

## 打开

"""
    if mode == "singlefile":
        readme += f"双击 `{output.name}` 打开离线报告。\n"
    elif mode in ("server", "mixed"):
        _port = server_base.rsplit(":", 1)[-1]
        _mode_note = ("报告由本机服务端渲染" if mode == "server"
                      else "明细数据由本机服务端渲染（`report.html` 是指引入口页，数据不在文件里）")
        readme += (
            f"双击报告目录里的 `打开报告.command`（macOS）/ `打开报告.cmd`（Windows）/ `打开报告.sh`（Linux）即可打开报告；"
            f"它会自动启动本地数据服务并打开浏览器（需保持本机开机）。{_mode_note}。\n"
            "\n## 发给同事（可移植）\n\n"
            "- 把**整个报告文件夹**打包（压缩成 zip）发送即可；对方解压后双击上面的启动器，"
            "无需安装任何东西。\n"
            "- 对方电脑没有 Python（3.9+）时，启动器会**自动下载便携版 Python**（约 11–15MB，"
            "SHA256 校验，仅需一次联网）；不需要管理员权限。\n"
            "- 完全内网/离线的机器：把便携版 Python 预先放进文件夹的 `runtime/python/`"
            "（macOS/Linux：`bin/python3` 在其内；Windows：`python.exe` 在其根目录），"
            "或设置环境变量 `LRM_PYTHON` 指向已有 Python，即可跳过下载。\n"
            "- macOS 首次打开若提示「无法验证开发者」：右键启动器 → 「打开」；"
            "Windows 若出 SmartScreen 蓝框：点「更多信息」→「仍要运行」。\n"
            "\n## 环境变量钩子\n\n"
            "| 变量 | 作用 |\n|---|---|\n"
            "| `LRM_PYTHON` | 指定 Python 可执行文件路径（最高优先） |\n"
            "| `LRM_PORT` | 覆盖服务端口（构建期烘焙的端口被占用时用） |\n"
            "| `LRM_STATE_HOME` | 覆盖状态目录（默认 `~/.ai-html-report`） |\n"
            "| `LRM_NO_OPEN=1` | 只启动服务，不自动开浏览器 |\n"
            "\n## 访问地址\n\n"
            f"- 本机：`{server_base}/reports/{report_id}`（精确地址；根路径 `{server_base}` 打开本机默认报告）\n"
            f"- 局域网：`http://{_lan_ip()}:{_port}/reports/{report_id}`"
            "（同一网络内的其他电脑/手机直接打开即可）\n"
            f"- 报告管理：`{server_base}/manager`"
            "（集中查看/关闭这台电脑上所有运行中的报告）\n"
        )
        _pw = (manifest.get("runtime") or {}).get("editPassword")
        if _pw:
            _auto = "（构建时自动生成）" if (manifest.get("runtime") or {}).get("editPasswordGenerated") else ""
            readme += ("\n## 在线编辑\n\n"
                       f"- 编辑口令：`{_pw}`{_auto}（用于「保存与分享 → 保存为副本 / 保存并覆盖」；口令只在构建控制台、README 与报告落地页记录，报告页不显示）\n"
                       f"- 修改口令 / 关闭在线编辑：打开报告管理页 `{server_base}/manager`，在对应报告上「设置口令」或取消勾选「允许在线编辑」。"
                       "关闭后任何人保存都会提示「当前暂无法编辑保存报告，请联系管理员」。\n")
    else:
        readme += (
            f"启动本地服务后，用浏览器打开 {server_base}/reports/{report_id}。\n"
        )
    readme += "\n`manifest.json`、`quality.json`、`data-quality.md` 和 `provenance.json` 是可审计的侧车文件。\n"
    (workdir / "README.md").write_text(readme, encoding="utf-8")
    if mode in ("server", "mixed"):
        def ignore_generated(_path: str, names: list[str]) -> set[str]:
            return {name for name in names if name == "__pycache__" or name.endswith(".pyc")}

        # The server package imports the compiler (``server/template.py`` ->
        # ``scripts.spec_to_report`` -> ``semantics/*``), so the WHOLE scripts tree
        # must be bundled — not a whitelist — or the launcher's re-open fails with
        # ``ModuleNotFoundError: scripts.spec_to_report``.
        shutil.copytree(SCRIPTS, workdir / "scripts", dirs_exist_ok=True, ignore=ignore_generated)
        shutil.copytree(SKILL / "server", workdir / "server", dirs_exist_ok=True, ignore=ignore_generated)
        for asset_dir in ("runtime", "vendor", "python-runtime"):
            shutil.copytree(SKILL / "assets" / asset_dir, workdir / "assets" / asset_dir,
                            dirs_exist_ok=True, ignore=ignore_generated)
        _write_server_launcher(output.parent, server_base, report_id)


def _write_server_launcher(report_dir: Path, server_base: str, report_id: str,
                           label: str = "打开报告") -> None:
    """One-click, double-clickable launcher for non-technical users.

    Sits at the TOP of the report folder (next to the landing page). It starts
    the bundled local server against the MACHINE-GLOBAL registry/data
    (``~/.ai-html-report/`` — resolved at RUNTIME via $HOME / %USERPROFILE%, so
    the folder still works after being copied to another machine), then opens
    the browser. Any launcher resurrects the same shared server serving ALL
    local reports（统一 manager）; binding 0.0.0.0 keeps the LAN share link
    reachable. ``label`` renames the launcher files (拼版草稿 home uses 打开画板).

    可移植自举（2026-09-25）：报告文件夹整体打包发到任何机器，双击即可——
    Python 解析顺序 = ``$LRM_PYTHON`` → 报告文件夹自带 ``runtime/python/`` →
    系统 Python（≥3.9 版本闸）→ 自动下载便携版 Python（SHA256 校验，一次性）。
    环境钩子：``LRM_PYTHON``/``LRM_PORT``/``LRM_STATE_HOME``/``LRM_NO_OPEN``。
    Windows 下载/校验/解压逻辑在旁边的 ``_bootstrap_python.ps1``（批处理内联
    PowerShell 的引号地狱在含空格/撇号路径下必炸，独立 ps1 才可测）。"""
    from urllib.parse import urlparse
    parsed = urlparse(server_base)
    port = parsed.port or 8765
    url = f"http://127.0.0.1:{port}/reports/{report_id}"
    health = f"http://127.0.0.1:{port}/api/health"

    ps1 = """# AI 报告 · Windows 便携版 Python 自举（由构建脚本生成，请勿手改）。
# 下载 python.org embeddable 包（含 sqlite3），SHA256 校验后解压到 <Dest>\\python。
# 离线机器：手动把 embeddable zip 解压到 <Dest>\\python（python.exe 在其根目录）。
param([Parameter(Mandatory=$true)][string]$Dest)
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$pyDir = Join-Path $Dest 'python'
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') {
  $url = 'https://www.python.org/ftp/python/3.12.8/python-3.12.8-embed-arm64.zip'
  $sha = '__SHA_WIN_ARM64__'
} else {
  $url = 'https://www.python.org/ftp/python/3.12.8/python-3.12.8-embed-amd64.zip'
  $sha = '__SHA_WIN_AMD64__'
}
$zip = Join-Path $Dest '_py.zip'
Write-Host '[打开报告] 下载便携版 Python……'
Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
$got = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
if ($got -ne $sha) { Remove-Item $zip -Force; throw "SHA256 mismatch: $got" }
if (Test-Path $pyDir) { Remove-Item $pyDir -Recurse -Force }
Expand-Archive -Path $zip -DestinationPath $pyDir -Force
Remove-Item $zip -Force
& (Join-Path $pyDir 'python.exe') -c "import sqlite3, csv, json, zipfile, xml.etree.ElementTree"
if ($LASTEXITCODE -ne 0) { throw 'portable python self-test failed' }
Write-Host "[打开报告] 便携版 Python 就绪：$pyDir"
"""
    mac = """#!/bin/bash
# 双击打开报告：自动启动本地数据服务并打开浏览器（需保持本机开机）。
# 整个报告文件夹可直接打包发给同事——本机没有 Python 会自动下载便携版（一次）。
# 环境变量：LRM_PYTHON=指定 Python 路径  LRM_PORT=覆盖端口  LRM_STATE_HOME=状态目录  LRM_NO_OPEN=1 只启动不开浏览器
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/build" 2>/dev/null || exit 1
PORT="__PORT__"
URL="__URL__"
HEALTH="__HEALTH__"
if [ -n "${LRM_PORT:-}" ]; then PORT="$LRM_PORT"; URL="http://127.0.0.1:$PORT/reports/__REPORT_ID__"; HEALTH="http://127.0.0.1:$PORT/api/health"; fi
STATE="${LRM_STATE_HOME:-$HOME/.ai-html-report}"
mkdir -p "$STATE/data"
PYOK() { "$1" -c 'import sys;raise SystemExit(0 if sys.version_info>=(3,9) else 1)' >/dev/null 2>&1; }
PY="${LRM_PYTHON:-}"
if [ -n "$PY" ] && [ ! -x "$PY" ]; then PY=""; fi
if [ -z "$PY" ] && [ -x "$HERE/runtime/python/bin/python3" ]; then PY="$HERE/runtime/python/bin/python3"; fi
if [ -z "$PY" ]; then
  # Homebrew 路径优先于裸 python3（macOS 的 /usr/bin/python3 是 CLT 桩，
  # CLT 未装时执行会弹系统安装框——只在最后才碰它）。
  for c in /opt/homebrew/bin/python3 /usr/local/bin/python3 python3 python; do
    command -v "$c" >/dev/null 2>&1 || continue
    CAND="$(command -v "$c")"
    if PYOK "$CAND"; then PY="$CAND"; break; fi
  done
fi
if [ -z "$PY" ]; then
  case "$(uname -s)/$(uname -m)" in
    Darwin/arm64)  F="cpython-3.12.7+20241016-aarch64-apple-darwin-install_only_stripped.tar.gz"; SHA="__SHA_MAC_ARM64__";;
    Darwin/x86_64) F="cpython-3.12.7+20241016-x86_64-apple-darwin-install_only_stripped.tar.gz"; SHA="__SHA_MAC_X64__";;
    Linux/x86_64)  F="cpython-3.12.7+20241016-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"; SHA="__SHA_LINUX_X64__";;
    Linux/aarch64|Linux/arm64) F="cpython-3.12.7+20241016-aarch64-unknown-linux-gnu-install_only_stripped.tar.gz"; SHA="__SHA_LINUX_ARM64__";;
    *) echo "[打开报告] 暂不支持的平台 $(uname -s)/$(uname -m)：请安装 Python 3.9+ 后重试，或设置 LRM_PYTHON。"; exit 1;;
  esac
  echo "[打开报告] 未检测到 Python 3.9+，正在下载便携版 Python（约 15MB，一次即可）……"
  mkdir -p "$HERE/runtime" || exit 1
  U="https://github.com/astral-sh/python-build-standalone/releases/download/20241016/$F"
  if ! curl -fL --retry 2 -o "$HERE/runtime/_py.tgz" "$U"; then
    echo "[打开报告] 下载失败（需要联网一次）。离线机器：把便携版 Python 解压到 $HERE/runtime/python（bin/python3 在其内）后重试，或设置 LRM_PYTHON。"
    exit 1
  fi
  ACT="$(sha256sum "$HERE/runtime/_py.tgz" 2>/dev/null || shasum -a 256 "$HERE/runtime/_py.tgz")"
  case "$ACT" in
    "$SHA"*) ;;
    *) echo "[打开报告] 下载文件 SHA256 校验失败，已删除，请重试。"; rm -f "$HERE/runtime/_py.tgz"; exit 1;;
  esac
  mkdir -p "$HERE/runtime/_pytmp"
  if ! tar -xzf "$HERE/runtime/_py.tgz" -C "$HERE/runtime/_pytmp"; then
    echo "[打开报告] 解压失败，请重试。"; rm -rf "$HERE/runtime/_py.tgz" "$HERE/runtime/_pytmp"; exit 1
  fi
  rm -f "$HERE/runtime/_py.tgz"
  rm -rf "$HERE/runtime/python"
  mv "$HERE/runtime/_pytmp/python" "$HERE/runtime/python" && rm -rf "$HERE/runtime/_pytmp"
  PY="$HERE/runtime/python/bin/python3"
fi
PYOK "$PY" || { echo "[打开报告] Python 版本过低（需要 3.9+）：$PY"; exit 1; }
# 检查本地数据服务是否已启动（大数据集冷启动可达数十秒）。
# 冷启动带 --register-report：把本文件夹注册进本机报告注册表（对方机器
# registry 为空，没有这一步报告永远 404）。
# 温启动（2026-09-26）：server 已活着也要轻量重注册一次——更新版报告文件夹
# 的 manifest+快照才能覆盖进中央 state home（run_server 检测到活 server 时
# 经 HTTP 幂等 upsert 后退出，不拉新进程）。
if ! curl -s -m 2 "$HEALTH" >/dev/null 2>&1; then
  nohup "$PY" scripts/run_server.py --host 0.0.0.0 --port "$PORT" --registry "$STATE/registry.json" --data-dir "$STATE/data" --pid-file "$STATE/server.pid" --register-report "$HERE" --quiet >/dev/null 2>&1 &
  for _i in $(seq 1 100); do curl -s -m 1 "$HEALTH" >/dev/null 2>&1 && break; sleep 0.3; done
else
  "$PY" scripts/run_server.py --port "$PORT" --registry "$STATE/registry.json" --data-dir "$STATE/data" --register-report "$HERE" --quiet
fi
if ! curl -s -o /dev/null -m 3 -w '%{http_code}' "$URL" 2>/dev/null | grep -q 200; then
  echo "[打开报告] 提示：$URL 尚未就绪或被其他报告服务占用端口。可设 LRM_PORT=其他端口 重新运行本启动器。"
fi
if [ "${LRM_NO_OPEN:-}" != "1" ]; then open "$URL" 2>/dev/null || xdg-open "$URL" 2>/dev/null; fi
"""
    win = """@echo off
chcp 65001 >nul
setlocal EnableExtensions
rem 双击打开报告：自动启动本地数据服务并打开浏览器（需保持本机开机）。
rem 整个报告文件夹可直接打包发给同事——本机没有 Python 会自动下载便携版（一次）。
rem 环境变量：LRM_PYTHON=指定 Python 路径  LRM_PORT=覆盖端口  LRM_STATE_HOME=状态目录  LRM_NO_OPEN=1 只启动不开浏览器
cd /d "%~dp0build" || exit /b 1
set "PORT=__PORT__"
set "URL=__URL__"
set "HEALTH=__HEALTH__"
if defined LRM_PORT set "PORT=%LRM_PORT%"
if defined LRM_PORT set "URL=http://127.0.0.1:%LRM_PORT%/reports/__REPORT_ID__"
if defined LRM_PORT set "HEALTH=http://127.0.0.1:%LRM_PORT%/api/health"
set "STATE=%USERPROFILE%\\.ai-html-report"
if defined LRM_STATE_HOME set "STATE=%LRM_STATE_HOME%"
if not exist "%STATE%\\data" mkdir "%STATE%\\data"
set "PY="
set "PYARGS="
if defined LRM_PYTHON if exist "%LRM_PYTHON%" set "PY=%LRM_PYTHON%"
if not defined PY if exist "%~dp0runtime\\python\\python.exe" set "PY=%~dp0runtime\\python\\python.exe"
if not defined PY call :tryone python
if not defined PY call :tryone python3
if not defined PY py -3 -c "import sys;raise SystemExit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1 && (set "PY=py" & set "PYARGS=-3")
if not defined PY (
  echo [打开报告] 未检测到 Python 3.9+，正在下载便携版 Python（约 11MB，一次即可）……
  if not exist "%~dp0runtime" mkdir "%~dp0runtime"
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0_bootstrap_python.ps1" -Dest "%~dp0runtime"
  if errorlevel 1 (
    echo [打开报告] 便携版下载失败（需要联网一次）。
    echo [打开报告] 离线机器：把便携版 Python 解压到 "%~dp0runtime\python"（python.exe 在其根目录）后重试，
    echo [打开报告] 或设置环境变量 LRM_PYTHON 指向已安装的 Python。
    pause
    exit /b 1
  )
  set "PY=%~dp0runtime\\python\\python.exe"
)
"%PY%" %PYARGS% -c "import sys;raise SystemExit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1
if errorlevel 1 (
  echo [打开报告] Python 版本过低（需要 3.9+）：%PY%
  pause
  exit /b 1
)
rem 检查本地数据服务是否已启动（大数据集冷启动可达数十秒）。
rem 冷启动带 --register-report：把本文件夹注册进本机报告注册表（对方机器
rem registry 为空，没有这一步报告永远 404）。
rem 温启动（2026-09-26）：server 已活着也要轻量重注册一次——更新版报告文件夹
rem 的 manifest+快照才能覆盖进中央 state home（run_server 检测到活 server 时
rem 经 HTTP 幂等 upsert 后退出，不拉新进程）。
powershell -NoProfile -Command "try{(Invoke-WebRequest -Uri '%HEALTH%' -UseBasicParsing -TimeoutSec 2).StatusCode}catch{exit 1}" >nul 2>&1
if not errorlevel 1 (
  "%PY%" %PYARGS% "scripts\\run_server.py" --port %PORT% --registry "%STATE%\\registry.json" --data-dir "%STATE%\\data" --register-report "%~dp0." --quiet
  goto serveok
)
start "AI Report Server" /b "%PY%" %PYARGS% "scripts\\run_server.py" --host 0.0.0.0 --port %PORT% --registry "%STATE%\\registry.json" --data-dir "%STATE%\\data" --pid-file "%STATE%\\server.pid" --register-report "%~dp0." --quiet
set /a TRIES=0
:waitup
timeout /t 1 /nobreak >nul
powershell -NoProfile -Command "try{(Invoke-WebRequest -Uri '%HEALTH%' -UseBasicParsing -TimeoutSec 2).StatusCode}catch{exit 1}" >nul 2>&1
if not errorlevel 1 goto serveok
set /a TRIES+=1
if %TRIES% lss 100 goto waitup
:serveok
rem 大数据集首次装载需要几十秒：等报告 URL 就绪再开浏览器（上限 3 分钟）。
set /a UTRIES=0
:urlwait
powershell -NoProfile -Command "try{(Invoke-WebRequest -Uri '%URL%' -UseBasicParsing -TimeoutSec 3).StatusCode}catch{exit 1}" >nul 2>&1
if not errorlevel 1 goto urlok
set /a UTRIES+=1
if %UTRIES% lss 180 (
  timeout /t 1 /nobreak >nul
  goto urlwait
)
echo [打开报告] 提示：%URL% 3 分钟内未就绪（大数据集首装可能更久，可稍后刷新浏览器）；也可能是端口被其他报告服务占用——可设 LRM_PORT=其他端口 重新运行本启动器。
:urlok
if "%LRM_NO_OPEN%"=="1" exit /b 0
start "AI Report" "%URL%"
exit /b 0

:tryone
%~1 -c "import sys;raise SystemExit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1
if errorlevel 1 goto :eof
set "PY=%~1"
goto :eof
"""
    mac = (mac.replace("__PORT__", str(port)).replace("__URL__", url)
              .replace("__HEALTH__", health).replace("__REPORT_ID__", report_id)
              .replace("__SHA_MAC_ARM64__", _PORTABLE_PY_SHA["mac_arm64"])
              .replace("__SHA_MAC_X64__", _PORTABLE_PY_SHA["mac_x64"])
              .replace("__SHA_LINUX_X64__", _PORTABLE_PY_SHA["linux_x64"])
              .replace("__SHA_LINUX_ARM64__", _PORTABLE_PY_SHA["linux_arm64"]))
    win = (win.replace("__PORT__", str(port)).replace("__URL__", url)
              .replace("__HEALTH__", health).replace("__REPORT_ID__", report_id)
              .replace("__SHA_WIN_AMD64__", _PORTABLE_PY_SHA["win_amd64"])
              .replace("__SHA_WIN_ARM64__", _PORTABLE_PY_SHA["win_arm64"]))
    ps1 = (ps1.replace("__SHA_WIN_AMD64__", _PORTABLE_PY_SHA["win_amd64"])
              .replace("__SHA_WIN_ARM64__", _PORTABLE_PY_SHA["win_arm64"]))
    mac_path = report_dir / f"{label}.command"
    win_path = report_dir / f"{label}.cmd"
    sh_path = report_dir / f"{label}.sh"
    mac_path.write_text(mac, encoding="utf-8")
    win_path.write_bytes(win.replace("\n", "\r\n").encode("utf-8"))  # cmd.exe 需要 CRLF
    sh_path.write_text(mac, encoding="utf-8")  # Linux 与 macOS 同一脚本（uname 分支已覆盖）
    (report_dir / "_bootstrap_python.ps1").write_text(ps1, encoding="utf-8")
    for p in (mac_path, sh_path):
        try:
            os.chmod(p, 0o755)
        except OSError:
            pass


def _server_root(value: str) -> str:
    """Normalize the request's localhost URL to the server process root."""
    base = value.strip().rstrip("/")
    for suffix in ("/api/v1", "/api"):
        if base.endswith(suffix):
            base = base[:-len(suffix)]
            break
    from urllib.parse import urlparse
    parsed = urlparse(base)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("server URL must point to localhost")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("server URL may not contain a path, query or fragment")
    return base


def _server_ready(base: str) -> bool:
    try:
        with urlopen_loopback(base.rstrip("/") + "/api/health", timeout=2) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def _edit_password_chars() -> str:
    """Alphabet for generated passwords: no ambiguous 0/O/1/l/I (read aloud)."""
    return "abcdefghjkmnpqrstuvwxyz23456789"


def _edit_password_args(spec: dict, mode: str) -> list[str]:
    """CLI args carrying the effective edit password to the compiler.

    Called at every server/mixed compile site so the manifest runtime always has
    a password (prefill + README + /manager), without rewriting the author's
    request file. Empty list for singlefile (nothing to gate)."""
    password = ensure_edit_password(spec, mode)
    if not password:
        return []
    args = ["--edit-password", password]
    if (spec.get("runtime") or {}).get("editPasswordGenerated"):
        args.append("--edit-password-auto")
    return args


def ensure_edit_password(spec: dict, mode: str) -> str | None:
    """Guarantee a server-mode report is saveable out of the box.

    Returns the effective edit password (server/mixed modes only). When the
    request does not declare ``runtime.editPassword`` we GENERATE one and write it
    into the spec before compiling — otherwise the first 保存 attempt answered
    「该报告未启用编辑口令（请在构建时设置 runtime.editPassword）」, which is a
    dead end for the person who just built the report. The password is printed at
    build time, recorded in build/README.md and the landing page, and can later be
    changed or the whole online-edit feature switched off from /manager.
    """
    if mode not in ("server", "mixed"):
        return None
    runtime = spec.setdefault("runtime", {})
    if not isinstance(runtime, dict):
        runtime = {}
        spec["runtime"] = runtime
    existing = runtime.get("editPassword")
    if isinstance(existing, str) and existing.strip():
        return existing.strip()
    import secrets
    alphabet = _edit_password_chars()
    password = "".join(secrets.choice(alphabet) for _ in range(8))
    runtime["editPassword"] = password
    runtime["editPasswordGenerated"] = True
    return password


def _index_pid_alive(port: int) -> bool:
    """True when the global servers index lists a LIVE pid for ``port``.

    A cold start of a 300k-row snapshot takes ~1 minute, during which
    ``/api/health`` does not answer yet. Treating that as "port is free" made a
    rebuild allocate the NEXT port and persist it (so the report silently moved
    from its launcher/marker URL) — and the build's own `_ensure_server` window
    then gave up on the half-booted new server. The index's pid is the reliable
    "a server is coming up here" signal.

    Reads ``_state_home()``（与 server/registry.py、servers_index.py、
    ``_find_live_server`` 同一开关）：写死 ``~/.ai-html-report`` 会让
    ``LRM_STATE_HOME`` 隔离下的测试/CI 读到**用户真实索引**——真机上活着的
    server pid 把测试端口误判为「半启动」跳过，测试状态里正在冷启动的 server
    反而不可见（并发构建又可能撞同一端口）。
    """
    try:
        data = json.loads((_state_home() / "servers.json").read_text(encoding="utf-8"))
        entry = data.get(str(int(port))) or {}
        pid = int(entry.get("pid") or 0)
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _state_home() -> Path:
    """Machine-global server state dir（server/registry.py、servers_index.py 同一落点）.

    统一 manager（2026-09-20）：注册表、数据快照、pid 都落在
    ``~/.ai-html-report/``，本机所有报告共用——报告管理页因此能看到
    所有时间构建的所有报告，构建/启动器重启的也是同一份注册表。
    ``LRM_STATE_HOME`` 覆盖（测试/CI 状态隔离，与 server/registry.py、
    servers_index.py 同一开关）。"""
    return Path(os.environ.get("LRM_STATE_HOME") or (Path.home() / ".ai-html-report"))


def _lrm_server_registry(base: str) -> str | None:
    """registryPath when ``base`` answers /api/health like OUR report server.

    Any web app can expose /api/health; the LRM marker is the ``registryPath``
    key that h_health always sends. Distinguishing them stops a build from
    "reusing" (registering into) some unrelated local service."""
    try:
        with urlopen_loopback(base.rstrip("/") + "/api/health", timeout=2) as response:
            data = json.loads(response.read().decode("utf-8"))
            reg = data.get("registryPath") if isinstance(data, dict) else None
            return str(reg) if isinstance(reg, str) and reg else None
    except (OSError, ValueError, urllib.error.URLError):
        return None


def _lrm_server_alive(base: str) -> bool:
    """True when ``base`` answers /api/health like OUR report server."""
    return _lrm_server_registry(base) is not None


def _in_state_home(registry_path: str, home: Path) -> bool:
    """True when a live server's registry lives under OUR state home.

    复用的状态边界（第九轮实测）：LRM_STATE_HOME 是测试/CI 的隔离开关——隔离
    world 里的构建绝不能复用（= 注册进）真实 home 的活 server。端口段扫描曾只
    认 registryPath 标记不认边界，隔离套件的构建把报告注册进了用户真实 8765 的
    注册表（AGENTS「套件内的真实 server 构建不再注册进用户活着的服务/真实注册
    表」被击穿）。默认 home 不受影响：生产构建照旧复用全机唯一 server。"""
    try:
        reg = os.path.abspath(registry_path)
        home_abs = os.path.abspath(str(home))
        return os.path.commonpath([reg, home_abs]) == home_abs
    except ValueError:
        return False


def _reusable_server(base: str, home: Path) -> bool:
    """复用判据 = LRM 标记 + 对方注册表落在本构建的状态 home 内。"""
    reg = _lrm_server_registry(base)
    return reg is not None and _in_state_home(reg, home)


def _find_live_server(host: str, preferred_port: int) -> str | None:
    """Locate a live LRM report server on THIS machine (ports preferred..+199).

    Checks the global servers index first (port → registryPath, maintained by
    run_server at bind time), then scans the port range for a health endpoint
    carrying the LRM marker — covers servers started before the index existed
    or pruned from it. 复用有状态边界：只认注册表落在本构建 ``_state_home()``
    里的 server（``_reusable_server``）——跨 home 的活 server 是另一个 world。"""
    home = _state_home()
    try:
        index = json.loads((_state_home() / "servers.json").read_text(encoding="utf-8"))
        for port_str in sorted(index, key=lambda s: int(s) if str(s).isdigit() else 10**9):
            base = f"http://{host}:{int(port_str)}"
            if _reusable_server(base, home):
                return base
    except (OSError, ValueError, TypeError):
        pass
    for p in range(preferred_port, preferred_port + 200):
        base = f"http://{host}:{p}"
        if _reusable_server(base, home):
            return base
    return None


def _port_bindable(host: str, port: int) -> bool:
    """裸 TCP bind 探测：一个新 socket 现在还能不能绑上 ``(host, port)``。

    ``_server_ready``（HTTP health）与 ``_index_pid_alive``（LRM 索引）对
    「被非 HTTP 服务占用的端口」两头都不中——分配恒选中它，``_ensure_server``
    子进程 bind 失败退出，构建永久失败（2026-09-26 甄别）。分配前按子进程的
    绑定语义试绑：不带 SO_REUSEADDR 的严格试绑失败时，POSIX 再用
    SO_REUSEADDR 试一次——只剩 TIME_WAIT 残留的端口子进程
    （http.server ``allow_reuse_address = 1``）本就能绑回来，不该因此漂移；
    活动监听器在 macOS/Linux 上两次都失败（实测 SO_REUSEADDR 抢不占活监听）。
    Windows 的 SO_REUSEADDR 允许抢占活动监听，不能作为「空闲」证据——只信
    严格试绑。"""
    import socket as _socket

    def _try(reuse: bool) -> bool:
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        try:
            if reuse and os.name != "nt":
                s.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
            s.bind((host, int(port)))
            return True
        except OSError:
            return False
        finally:
            try:
                s.close()
            except OSError:
                pass

    if _try(False):
        return True
    return os.name != "nt" and _try(True)


def _allocate_free_port(host: str, preferred_port: int) -> int:
    """First port with no running server, no half-booted server coming up, and
    no raw TCP occupier.

    The servers index's pid is the reliable "a server is booting here" signal —
    a 300k-row cold start takes ~1 minute before /api/health answers, and
    treating that as "free" made rebuilds move the report to a new port while
    its launcher/marker still pointed at the old one. The bind probe closes the
    remaining gap: an occupied port that answers no HTTP and carries no index
    entry used to be handed out forever, so every `_ensure_server` child died
    on bind and the build failed permanently."""
    for p in range(preferred_port, preferred_port + 200):
        if _server_ready(f"http://{host}:{p}") or _index_pid_alive(p):
            continue
        if not _port_bindable(host, p):
            continue
        return p
    raise RuntimeError(f"no free report-server port in {preferred_port}..{preferred_port + 199}")


def _resolve_report_server(base_hint: str, workdir: Path, report_id: str) -> str:
    """Resolve the SHARED report-server base from a localhost hint.

    统一 manager（2026-09-20）：全机只跑**一个**报告 server。优先复用本机任何
    活着的 LRM server（``_find_live_server``），本报告由后续的注册调用热注册
    进去（h_register 按 reportId upsert）；没有活 server 才从 preferred 起分配
    空闲端口并由 ``_ensure_server`` 用中央注册表/数据目录启动一个。因此新构建
    天然避开已有报告的端口（直接复用），管理页一处看全。选中的端口仍记进
    ``workdir/server.port`` 供观测/排障。"""
    from urllib.parse import urlparse
    parsed = urlparse(base_hint)
    host = parsed.hostname or "127.0.0.1"
    preferred = parsed.port or 8765
    live = _find_live_server(host, preferred)
    if live:
        try:
            (workdir / "server.port").write_text(live.rsplit(":", 1)[-1], encoding="utf-8")
        except OSError:
            pass
        return live
    port = _allocate_free_port(host, preferred)
    try:
        (workdir / "server.port").write_text(str(port), encoding="utf-8")
    except OSError:
        pass
    return f"http://{host}:{port}"


def _ensure_server(base: str, workdir: Path) -> subprocess.Popen[str] | None:
    """Start the bundled SQLite server when the requested localhost is down.

    统一 manager（2026-09-20）：server 的注册表/数据快照/pid 都落在机器级
    ``~/.ai-html-report/``（_state_home，与 server/registry.py 默认一致）——
    任何构建/启动器拉起的服务都装着全机所有报告，管理页一处看全。``workdir``
    只作为进程 cwd 保留。
    """
    if _server_ready(base):
        return None
    from urllib.parse import urlparse
    parsed = urlparse(base)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8765
    home = _state_home()
    home.mkdir(parents=True, exist_ok=True)
    registry = home / "registry.json"
    data_dir = home / "data"
    pid_file = home / "server.pid"
    data_dir.mkdir(parents=True, exist_ok=True)
    child = subprocess.Popen(
        [sys.executable, str(SCRIPTS / "run_server.py"), "--host", "0.0.0.0",
         "--port", str(port), "--registry", str(registry), "--data-dir", str(data_dir),
         "--pid-file", str(pid_file), "--quiet"],
        cwd=str(workdir), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        start_new_session=(os.name != "nt"),
    )
    for _ in range(600):  # 120s: a 335k+ row CSV snapshot reload legitimately takes
        # ~1 minute into SQLite on a cold start; the old 30s window aborted builds
        # whose server was still booting (the "无法启动 report server" report).
        if _server_ready(base):
            return child
        if child.poll() is not None:
            # 并发构建竞态：另一个进程抢先绑定了同一端口，本子进程绑定失败退出——
            # 只要端口最终就绪（赢家服务起来了），本次构建照常注册，不算失败。
            if _server_ready(base):
                return None
            break
        time.sleep(0.2)
    raise RuntimeError(f"无法启动 report server：{base}")


def _resolve_draft_server(workdir: Path, host: str, preferred_port: int) -> str:
    """Pick the SHARED server port for drafts (统一 manager，2026-09-20).

    草稿与报告不再各起 server：优先复用本机任何活着的 LRM server（草稿条目
    经 POST /api/drafts 落进它的注册表，报告管理页同一处看全），没有活 server
    才从 preferred 起分配空闲端口（``_ensure_server`` 会以中央注册表/数据目录
    启动）。``workdir/server.port`` 只作观测记录。"""
    live = _find_live_server(host, preferred_port)
    if live:
        try:
            (workdir / "server.port").write_text(live.rsplit(":", 1)[-1], encoding="utf-8")
        except OSError:
            pass
        return live
    port = _allocate_free_port(host, preferred_port)
    try:
        (workdir / "server.port").write_text(str(port), encoding="utf-8")
    except OSError:
        pass
    return f"http://{host}:{port}"


def _run_draft_mode(args) -> int:
    """``report_build --draft``：冷启动一张空白拼版画板。

    不需要 --spec/--table：起（或复用）草稿 home 的专属 server，POST /api/drafts
    建零数据集空白草稿（服务端生成编辑口令），把画板 URL 交给用户。数据在草稿内
    「数据源配置 → 添加数据表」上传补齐；拼完由 Agent 转正：
    ``report_build --spec <request> --table <数据> --report-id <draftId>
    --server <本server> --mode server`` 覆盖同一条目，URL 不变。
    草稿 home 与报告目录同布局（build/ 下 registry.json + server.port + scripts/
    快照），启动器「打开画板」与「打开报告」同一机制（launcher 契约依赖快照）。
    """
    title = (args.title or "").strip()[:80] or "拼版草稿"
    home = Path(args.out_dir).resolve() if args.out_dir else Path.cwd() / "拼版草稿"
    workdir = Path(args.workdir).resolve() if args.workdir else home / "build"
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        base_hint = _server_root(args.server) if args.server else DEFAULT_SERVER_URL
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    from urllib.parse import urlparse
    parsed = urlparse(base_hint)
    host = parsed.hostname or "127.0.0.1"
    preferred = parsed.port or 8765
    try:
        server_base = _resolve_draft_server(workdir, host, preferred)
        _ensure_server(server_base, workdir)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    def _ignore_generated(_path: str, names: list[str]) -> set[str]:
        return {name for name in names if name == "__pycache__" or name.endswith(".pyc")}

    # 启动器契约依赖 build/scripts 快照（server/template.py → scripts.spec_to_report
    # 的 import 链），与 server 模式打包同一机制整树复制。
    shutil.copytree(SCRIPTS, workdir / "scripts", dirs_exist_ok=True, ignore=_ignore_generated)
    shutil.copytree(SKILL / "server", workdir / "server", dirs_exist_ok=True, ignore=_ignore_generated)
    req = urllib.request.Request(server_base.rstrip("/") + "/api/drafts",
                                 data=json.dumps({"title": title}).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urlopen_loopback(req, timeout=30) as resp:
            created = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print(f"创建拼版草稿失败：{exc}", file=sys.stderr)
        return 1
    draft_id = created.get("reportId")
    _write_server_launcher(home, server_base, draft_id, label="打开画板")
    print(f"[draft] 拼版画板已就绪：{server_base}/reports/{draft_id}")
    print("- 在画板里拖占位块、逐块写一句话描述；数据在「数据源配置 → 添加数据表」上传。")
    if created.get("editPassword"):
        print(f"- 编辑口令（画板内保存用，自动生成）：{created['editPassword']}")
    print(f"- 拼完交给 AI 转正（URL 不变）：report_build --spec <request> --table <数据> "
          f"--report-id {draft_id} --server {server_base} --mode server")
    print(f"- 重新打开画板：双击 {home}/打开画板.command（macOS）或 打开画板.cmd（Windows）；"
          f"管理页 {server_base}/manager")
    return 0


def _relations_materialized(spec: dict) -> bool:
    # 「物化」语义 2026-09-22 起 = 逻辑直通 + 宽表代物化（见 _resolve_relations_spec）：
    # 声明了 relation 就把宽表造成一张普通数据集，源表与关系元数据原样保留。
    return any(isinstance(r, dict) and isinstance(r.get("tables"), list) and len(r["tables"]) >= 2
               for r in (spec.get("relations") or []))


def _default_primary_id(spec: dict) -> str | None:
    ds = [d for d in (spec.get("datasets") or []) if isinstance(d, dict)]
    for d in ds:
        if d.get("isDefault") and (d.get("role") or "primary") == "primary":
            return d.get("id")
    for d in ds:
        if (d.get("role") or "primary") == "primary":
            return d.get("id")
    return ds[0].get("id") if ds else None


def _resolve_relations_spec(spec: dict, spec_path: Path, workdir: Path):
    """Build-time relations pre-pass — 逻辑直通（docs/data-source-independence-and-merge-visibility-design.md D1）.

    前台「关联键合并」的模型 = 两张源表保留 + 关联键 + 按需物化的宽表数据集
    （数据源关系地图可见、可解散）。旧构建期走的是「预物化改写」：把请求重写成
    单数据集、relations 从 manifest 里消失 → 数据源面板只剩一张 ``__wide`` 表，
    前台关系地图没有输入可画。现在把 manifest 摆成与前台「立即合并」之后
    **完全相同**的状态：

        datasets  = [源表…, <base>__wide]      relations = [rel]（原样保留）

    宽表用同一个 ``merge_tables.materialize`` 物化成真实表（与 server
    materialize-relation / 重启 replay 字节一致，零第二实现），作为普通数据集走
    既有 multi-table 路径（singlefile 内联 / mixed 拆分 / server 注册全兼容）。
    跨源卡必须显式绑 ``datasetId: "<base>__wide"``；纯源表卡各绑各的源表。

    Returns ``(spec, spec_path, merged_file)`` for the resolved request, or
    ``None`` when no relations are declared. Raises ``ValueError`` with
    guidance when the declared files cannot be read.
    """
    if not _relations_materialized(spec):
        return None
    from merge_tables import materialize, merged_dataset_id, col_names

    declared = [d for d in (spec.get("datasets") or []) if isinstance(d, dict)]
    if len(declared) < 2:
        return None
    tables: dict[str, Any] = {}
    for d in declared:
        f = d.get("file")
        if not f:
            continue
        fp = Path(f)
        src = fp if fp.is_absolute() else (spec_path.parent / fp).resolve()
        prof_out = workdir / f"profile-merge-{d.get('id')}.json"
        canon_out = workdir / f"table-merge-{d.get('id')}.json"
        pr = run([sys.executable, str(SCRIPTS / "profile_table.py"), "--table", str(src),
                  "--out", str(prof_out), "--canonical-out", str(canon_out)], cwd=workdir)
        if pr.returncode != 0:
            raise ValueError(f"合并预检失败（数据集 {d.get('id')}）：{(pr.stderr or '')[-500:]}")
        tables[d["id"]] = json.loads(canon_out.read_text(encoding="utf-8"))
    base = _default_primary_id(spec)
    if base not in tables:
        raise ValueError("合并宽表需要一张可读的默认主表（datasets[].isDefault / role=primary）")
    merged = materialize(tables, spec.get("relations") or [], base)
    mid = merged_dataset_id(base)
    merged_path = workdir / f"table-{mid}.json"
    merged_path.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")

    out = json.loads(json.dumps(spec))  # deep copy so the caller keeps the original
    # 宽表数据集条目：普通表（file 指向合并产物，绝对路径；不带 role —— 枚举只有
    # primary/lookup，且追加在末尾 + 无 isDefault，永远不会成为默认主表）。
    # columnRoles/measureTypes 挂宽表条目自己身上：``renames`` 在 materialize 内
    # 应用，合并表携带的是 POST-rename 列名；按 pre-rename 名声明的角色必须翻译，
    # 否则静默丢角色。逐数据集声明优先、顶层默认槽兜底（沿用旧改写逻辑，只是
    # 落点从顶层 columnRoles/measureTypes 改为宽表条目）。
    mcols = set(col_names(merged))
    rmap: dict[str, str] = {}
    for rel in (spec.get("relations") or []):
        for src, dst in ((rel.get("renames") or {}) if isinstance(rel, dict) else {}).items():
            if isinstance(src, str) and isinstance(dst, str) and src not in rmap:
                rmap[src] = dst
    roles: dict[str, Any] = {}
    mtypes: dict[str, Any] = {}
    for src_spec in [*declared, spec]:  # per-dataset first, then the top-level default slot
        for col, r in (src_spec.get("columnRoles") or {}).items():
            col = rmap.get(col, col)
            if col in mcols and col not in roles:
                roles[col] = r
        for col, t in (src_spec.get("measureTypes") or {}).items():
            col = rmap.get(col, col)
            if col in mcols and col not in mtypes:
                mtypes[col] = t
    rel0 = (spec.get("relations") or [])[0]
    wide_label = (rel0.get("name") if isinstance(rel0, dict) and rel0.get("name") else None) or f"{base} 合并宽表"
    out.setdefault("datasets", []).append({
        "id": mid,
        "label": wide_label,
        "file": str(merged_path.resolve()),
        "columnRoles": roles,
        "measureTypes": mtypes,
    })
    # 源数据集 file 绝对化：resolved spec 落在 workdir，相对路径会按 workdir
    # 重解析（真机：a.csv 相对路径 → workdir 下不存在 → Table contract violation）。
    for d in out.get("datasets") or []:
        if not isinstance(d, dict) or d.get("id") == mid or not d.get("file"):
            continue
        fp = Path(str(d["file"]))
        if not fp.is_absolute():
            d["file"] = str((spec_path.parent / fp).resolve())
    # relations 原样保留；源数据集与每卡 datasetId 绑定一概不动（跨源卡应绑
    # datasetId=mid）。compile 把 relations/dimensionKeys 烘进 manifest，运行时
    # 关系地图据此画两卡 + 连线；server 端 replay/relation 物化管线原样可用。
    new_path = workdir / "spec-resolved.json"
    new_path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out, new_path, str(merged_path)


def _run_union(ds_key: str, files: list[str], out_path: Path) -> None:
    """构建入口并集步（2026-09-22）：N 个同构文件 → 一张规范表 JSON。

    语义单一真源 = ``table_input.load_table_files``（列集按名对齐、类型以合并
    后全集为准、notes 逐文件归因）。产物落在 workdir，下游 profile/编译/服务端
    仍按单表走（``--table`` 收 canonical JSON 是既有能力）——并集只发生在这里
    一次。没有它，「W35+W36 同结构补新一期」在构建期没有正道，执行 Agent 会被
    逼着手工合并/转换源文件（违反禁改原始数据红线，真机复盘两次踩中）。
    """
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    from table_input import TableValidationError, load_table_files
    try:
        table = load_table_files(files)
    except TableValidationError as exc:
        raise ValueError(f"数据集 {ds_key} 同构并集失败：{exc}") from exc
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"columns": table["columns"], "rows": table["rows"]},
                                   ensure_ascii=False), encoding="utf-8")
    print(f"[union] 数据集 {ds_key}：{len(files)} 个文件并集 → "
          f"{len(table['rows'])} 行 × {len(table['columns'])} 列（列集一致，类型以合并全集为准）")


def main() -> int:
    ap = argparse.ArgumentParser(description="Build an editable AI report (Windows-safe)")
    ap.add_argument("--table", default=None, help=".xlsx, .csv or canonical JSON; optional when every dataset[] declares a file")
    ap.add_argument("--spec", default=None, help="Agent-authored report_request.json (full request)")
    ap.add_argument("--patch", default=None, help="Agent-authored report_intent.json (patch over an auto-derived default; requires --table)")
    ap.add_argument("--out", default=None, help="report path; if omitted, an auto-named folder (report-title/build/ + report-title/<title>.html) is created under --out-dir")
    ap.add_argument("--out-dir", default=None, help="parent dir for the auto-named report-title folder (default: the input data file's directory; falls back to current directory)")
    ap.add_argument("--workdir", default=None, help="scratch/output directory (default: <title>/build)")
    ap.add_argument("--mode", choices=("auto", "server", "singlefile"), default="auto")
    ap.add_argument("--threshold", type=int, default=None,
                    help="singlefile row limit (cannot exceed 100000)")
    ap.add_argument("--max-bytes", type=int, default=DEFAULT_SINGLEFILE_BYTES,
                    help="singlefile canonical JSON limit (cannot exceed 50 MiB)")
    ap.add_argument("--max-cardinality", type=int, default=DEFAULT_SINGLEFILE_CARDINALITY,
                    help="singlefile dimension cardinality limit (cannot exceed 2000)")
    ap.add_argument("--report-id", default=None, help="override request.reportId")
    ap.add_argument("--server", default=None, help="override request.runtime.serverUrl")
    ap.add_argument("--skip-audit", action="store_true")
    ap.add_argument("--audit-scope", default=None,
                    help="范围化审计（开发提速）：预设 smoke/charts/derived/dataconfig/presentation/tables/filters/layout/interact 或检查名/通配（chart:*:painted），逗号组合；缺省 = 全量")
    ap.add_argument("--audit-required", action="store_true",
                    help="fail if browser audit cannot run")
    ap.add_argument("--strict-input", action="store_true",
                    help="输入体检（input-lint）把质量警告（warn）也按失败处理；仅 --sort 生效（整理模式）")
    ap.add_argument("--sort", action="store_true",
                    help="整理输入（缺省直通）：开启输入体检门禁（结构破坏拦截构建）+ 严格读取"
                         "（重复表头/空白行/参差行拒绝）。缺省对上传二维表不做任何处理——"
                         "体检仅出报告（input-lint.json），读取做最小确定性修复并记录 notes")
    ap.add_argument("--free", action="store_true",
                    help="自由模式（templateId=free-v1，docs/free-authoring-discussion.md §2）："
                         "spec/patch 即完整请求——--patch 不再与 suggest 默认合并，编译不做封面/"
                         "KPI 条/筛选带自动填充，声明什么渲染什么；快速看数请勿加本参数")
    ap.add_argument("--draft", action="store_true",
                    help="冷启动拼版草稿（docs/compose-user-path-design.md D4 第一层）："
                         "不需要 --spec/--table——起（或复用）专属草稿 server 并建零数据集"
                         "空白画板草稿，打印画板 URL；数据在草稿内「数据源配置」上传，"
                         "拼完由 Agent 以 --report-id <draftId> --server <草稿server> 原址转正")
    ap.add_argument("--title", default=None,
                    help="with --draft: 草稿标题（缺省「拼版草稿」）")
    args = ap.parse_args()
    stage_audit = None  # populated only by the single-table server canary path

    if args.draft:
        return _run_draft_mode(args)

    if args.audit_required and args.skip_audit:
        ap.error('--audit-required cannot be combined with --skip-audit')
    if args.audit_required and (args.audit_scope or os.environ.get('AI_AUDIT_SCOPE')):
        ap.error('--audit-required requires the full audit; omit --audit-scope / AI_AUDIT_SCOPE')

    # 范围化审计：--audit-scope 经环境变量透传给 audit_editor.mjs（run() 会复制
    # os.environ），三个审计调用点无需各自改动。缺省不设置 = 全量审计。
    if args.audit_scope:
        os.environ["AI_AUDIT_SCOPE"] = args.audit_scope

    # Version banner FIRST so a stale deployed skill copy is obvious from the
    # build log alone (company machines install skill snapshots that drift).
    print(f"[live-report-master] skill v{SKILL_VERSION} "
          f"(singlefile≤{DEFAULT_SINGLEFILE_ROWS // 1000}k rows / server / 汇报模式 / 统一表格明细↔聚合)")

    if not (1 <= args.max_bytes <= DEFAULT_SINGLEFILE_BYTES):
        ap.error(f"--max-bytes must be between 1 and {DEFAULT_SINGLEFILE_BYTES}")
    if not (1 <= args.max_cardinality <= DEFAULT_SINGLEFILE_CARDINALITY):
        ap.error(f"--max-cardinality must be between 1 and {DEFAULT_SINGLEFILE_CARDINALITY}")
    if bool(args.spec) == bool(args.patch):
        ap.error("provide exactly one of --spec (full request) or --patch (intent patch)")

    workdir = None
    out = None
    spec_path = Path(args.spec).resolve() if args.spec else None
    patch_path = Path(args.patch).resolve() if args.patch else None

    # Load the spec (full request) or derive+merge it (patch) first so the
    # report title is available for the auto-named output folder. Then resolve
    # the output/workdir paths:
    #   * if --out is given, it is used verbatim (backward compat);
    #   * otherwise an auto-named ``<title>/<title>.html`` folder is created under
    #     --out-dir (default cwd), with build artifacts in ``<title>/build/``.
    # This removes the need for the model to know/guess a target directory.
    messages: list[str] = []
    patch_keys: list[str] = []
    table_path = Path(args.table).resolve() if args.table else None
    try:
        if args.patch:
            # Agent-authored intent patch over a deterministic default: the model
            # writes only the few points it cares about (Option B input surface).
            # The merged request is written next to the patch so relative dataset
            # ``file`` paths still resolve against where the Agent authored them.
            # --free：patch 即完整请求，不经 suggest 默认（自由模式的入口语义，
            # docs/free-authoring-discussion.md §2.2）。
            from suggest_report import suggest_request, merge_request
            if table_path is None:
                raise ValueError("--patch requires --table (to profile and derive the default request)")
            patch = json.loads(patch_path.read_text(encoding="utf-8"))
            patch_keys = sorted(patch.keys())
            if args.free:
                spec = dict(patch)
                spec.setdefault("schemaVersion", 1)
                spec["templateId"] = "free-v1"
            else:
                default = suggest_request(table_path)
                spec = merge_request(default, patch)
            spec_path = patch_path.with_suffix(".merged.json")
            spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        else:
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            if args.free:
                # 语法糖：--free 落进请求本体（真源 = templateId 字段，产物可审计）。
                spec["templateId"] = "free-v1"
    except Exception as exc:
        # A minimal status path lets the failure be surfaced without a workdir.
        # A weak model sometimes runs this with CWD inside the skill folder;
        # never drop scratch status there (stderr already carries the error).
        _status_path = Path(args.workdir or ".") / "build-status.json"
        # CWD 可能落在 skill 根目录（有 SKILL.md）**或 scripts/ 子目录**（SKILL.md
        # 在上一级）——两处都是部署目录，都禁落草稿状态文件（≤100 文件约束）。
        _sp = _status_path.parent.resolve()
        _in_skill = (_sp / "SKILL.md").exists() or (_sp.parent / "SKILL.md").exists() or _sp.name == "scripts"
        if not _in_skill:
            _write_status(_status_path, "failed", mode="unknown",
                          messages=[f"读取/生成 spec 失败：{exc}"])
        print(f"[spec] FAILED: {exc}", file=sys.stderr)
        return 1

    title = spec.get("title") or "报告"
    if args.out:
        out = Path(args.out).resolve()
        workdir = Path(args.workdir).resolve() if args.workdir else out.parent
    else:
        # The report folder (and ALL its build artifacts) defaults to sit
        # BESIDE the user-provided data file — never in whatever directory the
        # agent happened to run from. --out-dir still overrides explicitly.
        if args.out_dir:
            out_dir = Path(args.out_dir).resolve()
        elif table_path is not None:
            out_dir = table_path.parent.resolve()
        else:
            ds_files = []
            for d in (spec.get("datasets") or []):
                if isinstance(d, dict) and d.get("file"):
                    _fl = d["file"] if isinstance(d["file"], list) else [d["file"]]
                    ds_files.append(str(_fl[0]))
            out_dir = (spec_path.parent / ds_files[0]).resolve().parent if ds_files else Path.cwd()
        title_dir = out_dir / _safe_filename(title)
        workdir = (args.workdir and Path(args.workdir).resolve()) or (title_dir / "build")
        out = title_dir / f"{_safe_filename(title)}.html"
    workdir.mkdir(parents=True, exist_ok=True)
    table_json = workdir / "table.json"
    profile_json = workdir / "profile.json"
    draft = workdir / "draft.html"
    status_path = workdir / "build-status.json"

    # WP-0A 输入契约体检：--table（及 datasets[].file）进编译前恒体检一次。
    # 结果落 <workdir>/input-lint.json；结构破坏（fail）拦截构建，--strict-input
    # 把质量警告（warn）也升为失败。体检放在 merge 预检之前——对象是用户原始
    # 输入，构建产物（合并宽表 JSON）不重复体检。
    lint_files: list[Path] = []
    if table_path is not None:
        lint_files.append(table_path)
    for _ds in (spec.get("datasets") or []):
        if not (isinstance(_ds, dict) and _ds.get("file")):
            continue
        _fl = _ds["file"] if isinstance(_ds["file"], list) else [_ds["file"]]
        for _one in _fl:
            _f = Path(str(_one))
            if not _f.is_absolute():
                _f = (spec_path.parent / _f).resolve()
            if _f not in lint_files:
                lint_files.append(_f)
    if lint_files:
        # 2026-09-18 用户裁决：输入预处理默认关（直通）——体检只出报告不拦截；
        # --sort（整理模式）恢复门禁：结构破坏拦截，--strict-input 升级 warn。
        _lint_doc, _lint_ok = _run_input_lint(lint_files, workdir,
                                              strict=args.strict_input, gate=bool(args.sort))
        if not _lint_ok:
            _lint_files_rep = (_lint_doc or {}).get("files") or []
            _n_fail = sum(r.get("failCount", 0) for r in _lint_files_rep)
            _n_warn = sum(r.get("warnCount", 0) for r in _lint_files_rep)
            reason = (f"输入体检未通过（--sort 整理模式）：结构问题 {_n_fail} 项"
                      + (f"；--strict-input 将 {_n_warn} 项质量警告按失败处理"
                         if args.strict_input and _n_warn else "")
                      + f"（详见 {workdir / 'input-lint.json'}；修复输入后重跑，或去掉 --sort 以直通模式构建，契约见 docs/input-contract.md）")
            _write_status(status_path, "failed", mode="unknown", messages=[reason])
            print(f"[input-lint] FAILED: {reason}", file=sys.stderr)
            return 2

    # Build-time relations 逻辑直通（D1，2026-09-22）：宽表作为普通数据集追加、
    # 源表与 relations 原样保留 —— manifest 与前台「立即合并」后状态一致
    # （数据源面板：两张表 + 关联键 + 合并组）。
    try:
        mat = _resolve_relations_spec(spec, spec_path, workdir)
    except (ValueError, KeyError) as exc:
        # KeyError（relations 里 dataset 漏 id）也是可行动输入错误：裸 traceback
        # 会从 main() 逃逸，_write_status 不执行 —— build-status.json 停留在上一
        # 次构建的旧值（可能还是 ready）。
        _write_status(status_path, "failed", mode="unknown", messages=[str(exc)])
        print(f"[merge] FAILED: {exc}", file=sys.stderr)
        return 2
    if mat is not None:
        spec, spec_path, merged_file = mat
        messages.append("已按 relations 逻辑直通：源表保留 + 关联键 + 宽表数据集（前台关系地图可见）")
        print(f"[merge] relations → 逻辑直通（源表保留 + 关联键 + 宽表数据集 {Path(merged_file).stem}）")

    # Request v1 is deliberately validated before any expensive work. There
    # is no legacy-spec compatibility path in this new skill.
    request_check = run([sys.executable, str(SCRIPTS / "validate_request.py"), str(spec_path)], cwd=workdir)
    if request_check.returncode != 0:
        message = (request_check.stderr or request_check.stdout)[-4000:]
        _write_status(status_path, "failed", mode="unknown", messages=[message])
        print(message, file=sys.stderr)
        return 2

    # 校验器对请求的**归一化**（dataset id 缺失/非法/重复时的确定性兜底重命名、
    # chart 编码归一等）只发生在上面那个子进程里——不会回写本进程的对象。拿着
    # 未归一的 spec 继续走，`ds['id']` 直接 KeyError（缺 id），重复 id 则把两份
    # 数据集的 profile/canonical 写到同一批文件相互覆盖。进程内再跑一次同一
    # 校验器，用它的返回值（归一化后的 spec）继续构建（对已过文件校验的请求
    # 是幂等的）。
    try:
        if str(SCRIPTS) not in sys.path:
            sys.path.insert(0, str(SCRIPTS))
        import validate_request as _vr
        spec = _vr.validate_request(spec)
    except Exception as exc:
        message = f"request validation failed: {exc}"[-4000:]
        _write_status(status_path, "failed", mode="unknown", messages=[message])
        print(message, file=sys.stderr)
        return 2

    declared_ds = spec.get("datasets") or []
    if declared_ds:
        # P1 multi-table (unrelated tables in one shell): each declared dataset
        # is profiled + canonicalised, then a single compile emits every table.
        # Charts bind their own datasetId. Modes: singlefile (all inline),
        # auto→mixed (small inline + big registered), and an explicit
        # `--mode server` which now registers EVERY table server-side (P1.5,
        # schema-only manifest).
        hints = spec.get("runtime") or {}
        threshold = args.threshold if args.threshold is not None else int(
            hints.get("singlefileMaxRows", DEFAULT_SINGLEFILE_ROWS))
        if not (1 <= threshold <= DEFAULT_SINGLEFILE_ROWS):
            ap.error(f"singlefile row limit must be between 1 and {DEFAULT_SINGLEFILE_ROWS}")
        requested_mode = args.mode if args.mode != "auto" else hints.get("mode", "auto")
        # P1.5: an explicit `--mode server` registers EVERY declared dataset as a
        # server-side table (schema-only manifest) instead of being silently
        # downgraded. `auto` still picks per-table singlefile/remote (mixed).
        force_server = requested_mode == "server"
        report_id = _resolve_report_id(args, spec)
        # Same as the single-dataset path: the compiled manifest must carry the
        # final slug reportId or the runtime's server-save URL 404s. The merge
        # file is report_build's own artifact, so rewriting it is safe.
        spec["reportId"] = report_id
        if spec_path is not None and (spec_path.name.endswith(".merged.json")
                                      or spec_path.name.endswith(".resolved.json")):
            spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        inherit_argv = _inherit_args(workdir, report_id)
        try:
            base_hint = _server_root(args.server or hints.get("serverUrl") or DEFAULT_SERVER_URL)
        except ValueError as exc:
            _write_status(status_path, "failed", mode="unknown", messages=[str(exc)])
            print(str(exc), file=sys.stderr)
            return 2
        # Per-report isolation: resolve an isolated free port so a multi-report
        # build/serve never collides with another report's server.
        server_base = _resolve_report_server(base_hint, workdir, report_id)
        top_roles = spec.get("columnRoles") or {}
        plan_json = workdir / "datasets.json"
        ds_plan = []
        total_bytes = 0
        ds_modes = []
        for _idx, ds in enumerate(declared_ds):
            file = ds.get("file")
            files = [str(x) for x in file] if isinstance(file, list) else ([str(file)] if file else [])
            if _idx == 0 and not files and table_path is not None:
                files = [str(table_path)]
            if not files:
                msg = f"数据集 {ds.get('id') or _idx + 1} 缺少 file（首个数据集可用 --table 提供）"
                _write_status(status_path, "failed", mode="unknown", messages=[msg])
                print(msg, file=sys.stderr)
                return 2
            if len(files) == 1:
                src = Path(files[0])
            else:
                # 构建期同构并集：入口并一次，下游全按单表走。
                src = workdir / f"table-{ds.get('id') or _idx + 1}-union.json"
                try:
                    _run_union(str(ds.get("id") or _idx + 1), files, src)
                except ValueError as exc:
                    _write_status(status_path, "failed", mode="unknown", messages=[str(exc)])
                    print(str(exc), file=sys.stderr)
                    return 2
            src = src if src.is_absolute() else (spec_path.parent / src).resolve()
            prof_out = workdir / f"profile-ds-{ds['id']}.json"
            canon_out = workdir / f"table-{ds['id']}.json"
            pr = run([sys.executable, str(SCRIPTS / "profile_table.py"), "--table", str(src),
                      "--out", str(prof_out), "--canonical-out", str(canon_out)], cwd=workdir)
            if pr.returncode != 0:
                _write_status(status_path, "failed", mode="unknown", messages=[pr.stderr[-2000:]])
                print(pr.stderr[-2000:], file=sys.stderr)
                return 1
            profile = json.loads(prof_out.read_text(encoding="utf-8"))
            shape = profile_shape(profile)
            ds_roles = dict(ds.get("columnRoles") or {})
            for col, role in top_roles.items():
                if col not in ds_roles and col in set(shape.get("columns") or []):
                    ds_roles[col] = role
            try:
                ds_modes.append(choose_mode(shape, requested_mode,
                                            max_rows=threshold, max_bytes=args.max_bytes,
                                            max_cardinality=args.max_cardinality, roles=ds_roles,
                                            lookup=(ds.get("role") or "primary") == "lookup"))
            except ValueError as exc:
                msg = f"数据集 {ds.get('id')}: {exc}"
                _write_status(status_path, "failed", mode="unknown", messages=[msg])
                print(msg, file=sys.stderr)
                return 1
            total_bytes += canon_out.stat().st_size
            ds_plan.append({"id": ds["id"], "label": ds.get("label") or Path(src).stem,
                            "file": str(canon_out.resolve())})
        any_server = any(m != "singlefile" for m in ds_modes)
        too_big = total_bytes > args.max_bytes
        if requested_mode == "singlefile" and (any_server or too_big):
            msg = ("多数据集显式 singlefile 超出上限（逐表或合计字节超限）；请改 auto 走混合模式，或预聚合/合并数据")
            _write_status(status_path, "failed", mode="unknown", messages=[msg])
            print(msg, file=sys.stderr)
            return 1
        if not force_server and not any_server and not too_big:
            # ---- all datasets fit singlefile: embed every table (P1 behaviour) ----
            mode = "singlefile"
            plan_json.write_text(json.dumps(ds_plan, ensure_ascii=False) + "\n", encoding="utf-8")
            print(f"[datasets] {len(ds_plan)} 张表 (total {total_bytes:,} bytes, singlefile)")
            compile_argv = [sys.executable, str(SCRIPTS / "spec_to_report.py"),
                            "--spec", str(spec_path), "--out", str(draft), "--datasets", str(plan_json),
                            "--mode", mode, "--report-id", report_id] + _edit_password_args(spec, mode) + inherit_argv \
                           + (["--sort-input"] if args.sort else [])
            result = run(compile_argv, cwd=workdir)
            if result.returncode != 0:
                _write_status(status_path, "failed", mode=mode, messages=[result.stderr[-2000:]])
                print(result.stderr[-2000:], file=sys.stderr)
                return 1
            result = run([sys.executable, str(SCRIPTS / "validate_report.py"), "--html", str(draft)], cwd=workdir)
            if result.returncode != 0:
                _write_status(status_path, "failed", mode=mode, messages=[result.stderr[-2000:]])
                print(result.stderr[-2000:], file=sys.stderr)
                return 1
            lint = run([sys.executable, str(SCRIPTS / "lint_report.py"), "--html", str(draft)], cwd=workdir)
            if lint.returncode not in {0, 2}:
                _write_status(status_path, "failed", mode=mode, messages=[lint.stderr[-2000:]])
                return 1
            if lint.stdout.strip():
                messages.extend(lint.stdout.strip().splitlines()[-10:])
            result = run([sys.executable, str(SCRIPTS / "package_report.py"),
                          "--html", str(draft), "--out", str(out),
                          "--datasets", str(plan_json), "--mode", "singlefile"], cwd=workdir)
            if result.returncode != 0:
                _write_status(status_path, "failed", mode=mode, messages=[result.stderr[-2000:]])
                print(result.stderr[-2000:], file=sys.stderr)
                return 1
            result = run([sys.executable, str(SCRIPTS / "validate_report.py"), "--html", str(out), "--packaged"], cwd=workdir)
            if result.returncode != 0:
                _write_status(status_path, "failed", mode=mode, messages=[result.stderr[-2000:]])
                return 1
            audit_target = str(out)
            try:
                table = json.loads(Path(ds_plan[0]["file"]).read_text(encoding="utf-8"))
                _write_portable_artifacts(workdir, mode=mode, report_id=report_id, draft=draft, output=out,
                                          table=table, server_base=DEFAULT_SERVER_URL,
                                          spec=spec, patch_keys=patch_keys, build_messages=list(messages),
                                          has_prev_manifest=(workdir / 'prev-manifest.json').exists())
            except Exception as exc:
                messages.append(f"sidecar artifact warning: {exc}")
            status = "ready"
            report = None
            if args.skip_audit:
                status = "needs_audit"
                messages.append("browser audit skipped by request")
            else:
                node = shutil.which("node")
                if not node:
                    status = "needs_audit"
                    messages.append("Node.js not found; browser audit deferred")
                else:
                    report = _audit_report(node, audit_target, workdir)
                    if not report.get("passed"):
                        status = "failed" if args.audit_required else "needs_audit"
                        messages.append(f"browser audit failed or deferred: {report}")
            _write_status(status_path, status, mode=mode, messages=messages, audit=report)
            print(f"=== DONE ({mode}, {status}): {out} ===")
            return 0 if status != "failed" else 2
        # ---- MIXED / 全远端 server：小表内联，大表（或 force_server 下全部）登记到本地服务 ----
        mode = "server" if force_server else "mixed"
        compile_mode = "server" if force_server else "singlefile"
        mixed_plan = []
        src_by_id = {}
        for _idx, ds in enumerate(declared_ds):
            file = ds.get("file")
            # 与上方 profile 循环同一形状归一：file 收数组（同构并集）——直接
            # Path(list) 是 TypeError，mixed/server 路径曾因此裸 traceback
            # （union 批只在 singlefile e2e 验过，mixed 分支漏改）。
            files = [str(x) for x in file] if isinstance(file, list) else ([str(file)] if file else [])
            _src = table_path if (_idx == 0 and not files) else (Path(files[0]) if files else None)
            if _src is None:
                _src = table_path
            src_by_id[ds["id"]] = _src if _src.is_absolute() else (spec_path.parent / _src).resolve()
        for _idx, ds in enumerate(declared_ds):
            canon = workdir / f"table-{ds['id']}.json"
            if not force_server and ds_modes[_idx] == "singlefile":
                mixed_plan.append({"id": ds["id"], "label": ds.get("label") or src_by_id[ds["id"]].stem,
                                   "file": str(canon)})
            else:
                mixed_plan.append({"id": f"{report_id}__{ds['id']}", "raw": ds["id"],
                                   "label": ds.get("label") or src_by_id[ds["id"]].stem,
                                   "file": str(canon), "remote": True})
        plan_json.write_text(json.dumps(mixed_plan, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"[datasets] {len(mixed_plan)} 张表（mixed：内联 + 远端登记）")
        # mixed reports are edited/saved through the server, so the edit password
        # gate must be generated for mode="mixed" — keying it off compile_mode
        # ("singlefile") returned None and every 覆盖/副本 save was rejected
        # until someone set a password in /manager by hand.
        compile_argv = [sys.executable, str(SCRIPTS / "spec_to_report.py"),
                        "--spec", str(spec_path), "--out", str(draft), "--datasets", str(plan_json),
                        "--mode", compile_mode, "--report-id", report_id] + _edit_password_args(spec, mode) + inherit_argv \
                       + (["--sort-input"] if args.sort else [])
        result = run(compile_argv, cwd=workdir)
        if result.returncode != 0:
            _write_status(status_path, "failed", mode=mode, messages=[result.stderr[-2000:]])
            print(result.stderr[-2000:], file=sys.stderr)
            return 1
        result = run([sys.executable, str(SCRIPTS / "validate_report.py"), "--html", str(draft)], cwd=workdir)
        if result.returncode != 0:
            _write_status(status_path, "failed", mode=mode, messages=[result.stderr[-2000:]])
            print(result.stderr[-2000:], file=sys.stderr)
            return 1
        lint = run([sys.executable, str(SCRIPTS / "lint_report.py"), "--html", str(draft)], cwd=workdir)
        if lint.returncode not in {0, 2}:
            _write_status(status_path, "failed", mode=mode, messages=[lint.stderr[-2000:]])
            return 1
        if lint.stdout.strip():
            messages.extend(lint.stdout.strip().splitlines()[-10:])
        # Do not replace the public marker before the publish gate has passed.
        # It is written after successful registry promotion below.
        # 注册本报告的远端（server）数据集（复用全机唯一活着的 server）。
        try:
            _ensure_server(server_base, workdir)
        except Exception as exc:
            _write_status(status_path, "failed", mode=mode, messages=[str(exc)])
            print(str(exc), file=sys.stderr)
            return 1
        import re as _re
        # --out 现在是指引页（marker），manifest 恒从编译草稿取（mixed 与
        # force_server 同一通道；数据 HTML 不再是交付物）。
        src_html = draft.read_text(encoding="utf-8")
        _m = _re.search(r'<script id="ai-report-manifest" type="application/json">(.*?)</script>', src_html, _re.S)
        if not _m:
            _write_status(status_path, "failed", mode=mode, messages=["packaged html missing manifest"])
            print("packaged html missing manifest", file=sys.stderr)
            return 1
        manifest = json.loads(_m.group(1))
        inline_ids = {d["id"] for d in mixed_plan if not d.get("remote")}
        for ds in manifest.get("datasets", []):
            if isinstance(ds, dict) and "remote" not in ds:
                ds["remote"] = ds.get("id") not in inline_ids
        payload_ds = []
        # 数据集声明的行级派生列（「计算列」）必须随注册上传：服务端用同一份
        # derived_eval 物化成真列（server/derived_columns.py），否则 manifest 声称
        # 该列存在、SQLite 却没有 → 引用它的聚合直接 no such column。声明来自编译
        # 产物（单一真源），不是重新从 spec 推断一遍。
        derived_by_ds = {ds.get("id"): ds.get("derived") for ds in manifest.get("datasets", [])
                         if isinstance(ds, dict) and ds.get("derived")}
        groups_by_ds = {ds.get("id"): ds.get("dimGroups") for ds in manifest.get("datasets", [])
                        if isinstance(ds, dict) and ds.get("dimGroups")}
        # 增量合并主键（datasets[].primaryKey）同样从编译产物直通进注册表：
        # 刷新向导的缺省预填读的就是这份声明。
        pk_by_ds = {ds.get("id"): ds.get("primaryKey") for ds in manifest.get("datasets", [])
                    if isinstance(ds, dict) and ds.get("primaryKey")}
        for d in mixed_plan:
            if not d.get("remote"):
                continue
            item = {"id": d["id"], "label": d["label"],
                    "source": str(d["file"]),
                    "table": json.loads(Path(d["file"]).read_text(encoding="utf-8"))}
            decl = derived_by_ds.get(d["id"]) or derived_by_ds.get(d.get("raw"))
            if decl:
                item["derived"] = decl
            gdecl = groups_by_ds.get(d["id"]) or groups_by_ds.get(d.get("raw"))
            if gdecl:
                item["dimGroups"] = gdecl
            pk = pk_by_ds.get(d["id"]) or pk_by_ds.get(d.get("raw"))
            if pk:
                item["primaryKey"] = pk
            payload_ds.append(item)
        body = {"reportId": report_id, "title": manifest.get("title") or title,
                "manifest": manifest, "multi": True, "datasets": payload_ds}
        # Multi-dataset canary: every dataset id in the manifest/payload is
        # namespaced to stage_id, never the live id. Persist the final payload
        # to disk and release the huge rows before launching Chrome; after a
        # green audit, promote by registering the untouched original payload.
        if args.audit_required and not args.skip_audit:
            node = shutil.which('node')
            if not node:
                _write_status(status_path, 'failed', mode=mode,
                              messages=['Node.js not found; required pre-publish audit unavailable'])
                return 2
            stage_id = ('stg-' + report_id[:58].rstrip('-') + '-' + secrets.token_hex(5))[:79]
            final_payload = workdir / 'pending-registration.json'
            final_payload.write_bytes(json.dumps(body, ensure_ascii=False).encode('utf-8'))
            try:
                stage_body = _canary_multi_body(body, stage_id)
                _post_registry_payload(server_base, json.dumps(stage_body, ensure_ascii=False).encode('utf-8'))
                stage_body = None
                payload_ds = None
                body = None
                _release_build_memory()
                stage_audit = _audit_report(node, f'{server_base.rstrip("/")}/reports/{stage_id}', workdir)
                if not stage_audit.get('passed'):
                    final_payload.unlink(missing_ok=True)
                    _write_status(status_path, 'failed', mode=mode,
                                  messages=['pre-publish canary audit failed; published report unchanged'],
                                  audit=stage_audit)
                    return 2
            except Exception as exc:
                final_payload.unlink(missing_ok=True)
                _write_status(status_path, 'failed', mode=mode,
                              messages=[f'canary registration/audit failed: {exc}; published report unchanged'])
                return 1
            finally:
                try:
                    _unregister_staged_report(server_base, stage_id)
                except Exception as exc:
                    messages.append(f'canary cleanup warning: {exc}')
            reg_payload = final_payload.read_bytes()
            final_payload.unlink(missing_ok=True)
        else:
            reg_payload = json.dumps(body, ensure_ascii=False).encode('utf-8')
        try:
            reg_ok = _post_registry_payload(server_base, reg_payload)
        except Exception as exc:
            _write_status(status_path, "failed", mode=mode, messages=[f"注册远端数据集失败：{exc}"])
            print(f"注册远端数据集失败：{exc}", file=sys.stderr)
            return 1
        reg_payload = None
        # mixed/server output is a marker only, never a half-working offline
        # report. Write it *after* a successful registry promotion.
        result = run([sys.executable, str(SCRIPTS / "package_report.py"),
                      "--html", str(draft), "--out", str(out),
                      "--server-url", server_base,
                      "--mode", ("server" if force_server else "singlefile"),
                      "--marker-only"], cwd=workdir)
        if result.returncode != 0:
            _write_status(status_path, "failed", mode=mode, messages=[result.stderr[-2000:]])
            print(result.stderr[-2000:], file=sys.stderr)
            return 1
        messages.append(f"mixed 登记远端数据集：{reg_ok.get('datasetIds')}")
        _ship_portable_data(workdir, report_id)
        audit_target = f"{server_base.rstrip('/')}/reports/{report_id}"
        try:
            inline_table = next((json.loads(Path(d["file"]).read_text(encoding="utf-8")) for d in mixed_plan if not d.get("remote")), None)
            if force_server:
                # 全远端 server：连同启动器/README/marker 落地页一并写（mode=server）
                first_table = json.loads(Path(mixed_plan[0]["file"]).read_text(encoding="utf-8")) if mixed_plan else None
                _write_portable_artifacts(workdir, mode="server", report_id=report_id, draft=draft, output=out,
                                          table=first_table, server_base=server_base,
                                          spec=spec, patch_keys=patch_keys, build_messages=list(messages),
                                          has_prev_manifest=(workdir / 'prev-manifest.json').exists())
            elif inline_table is not None:
                _write_portable_artifacts(workdir, mode=mode, report_id=report_id, draft=draft, output=out,
                                          table=inline_table, server_base=server_base,
                                          spec=spec, patch_keys=patch_keys, build_messages=list(messages),
                                          has_prev_manifest=(workdir / 'prev-manifest.json').exists())
        except Exception as exc:
            messages.append(f"sidecar artifact warning: {exc}")
        # sidecar 落盘之后、打开审计之前释放编译期大对象：审计要开一个完整的 headless
        # 渲染进程（ECharts + 汇报 deck 克隆 + 页内生成 PPTX），而这里还握着整张表的又一份
        # 副本（payload_ds，33.5 万行 × 11 列 ≈ 1GB Python 对象）。两个峰值撞在一起会把
        # 渲染进程压到不再应答 CDP（真机：renderer CPU 0%、空闲只剩 50MB、无对话框）。
        payload_ds = None
        body = None
        table = None
        _release_build_memory()
        status = "ready"
        report = None
        if args.skip_audit:
            status = "needs_audit"
            messages.append("browser audit skipped by request")
        elif stage_audit is not None:
            report = stage_audit
            messages.append("pre-publish canary passed full browser audit")
        else:
            node = shutil.which("node")
            if not node:
                status = "needs_audit"
                messages.append("Node.js not found; browser audit deferred")
            else:
                report = _audit_report(node, audit_target, workdir)
                if not report.get("passed"):
                    status = "failed" if args.audit_required else "needs_audit"
                    messages.append(f"browser audit failed or deferred: {report}")
        _write_status(status_path, status, mode=mode, messages=messages, audit=report)
        print(f"=== DONE ({mode}, {status}): {out} | {audit_target} ===")
        if status != "failed":
            _print_server_access(server_base, report_id)
        return 0 if status != "failed" else 2

    if table_path is None:
        ap.error("--table is required when the request has no datasets[] entry")
    print("[profile] ...")
    # Profile first. For server-sized inputs this avoids creating a giant
    # intermediate canonical JSON file; only singlefile mode needs rows on disk.
    result = run([sys.executable, str(SCRIPTS / "profile_table.py"),
                  "--table", str(table_path), "--out", str(profile_json)], cwd=workdir)
    if result.returncode != 0:
        _write_status(status_path, "failed", mode="unknown", messages=[result.stderr[-2000:]])
        print(result.stderr[-2000:], file=sys.stderr)
        return 1
    profile = json.loads(profile_json.read_text(encoding="utf-8"))
    shape = profile_shape(profile)
    roles = spec.get("columnRoles") or {}
    columns = set(shape.get("columns") or [])
    # Missing columns are auto-derived from the profile by ``normalize_request``
    # (category->dimension, measure->measure, time->time, else ignore), so the
    # model only needs to specify the columns it cares about. Only reject roles
    # that reference a column that does not exist in the table.
    unknown = sorted(set(roles) - columns)
    if unknown:
        msg = f"columnRoles 引用了未知列：{unknown}"
        _write_status(status_path, "failed", mode="unknown", messages=[msg])
        print(msg, file=sys.stderr)
        return 2
    hints = spec.get("runtime") or {}
    threshold = args.threshold if args.threshold is not None else int(
        hints.get("singlefileMaxRows", DEFAULT_SINGLEFILE_ROWS)
    )
    if not (1 <= threshold <= DEFAULT_SINGLEFILE_ROWS):
        ap.error(f"singlefile row limit must be between 1 and {DEFAULT_SINGLEFILE_ROWS}")
    report_id = _resolve_report_id(args, spec)
    # The compiled manifest must carry the FINAL (ASCII-slug) reportId: the
    # runtime builds its server-save URL from manifest.reportId, so a manifest
    # still saying "report" makes every 覆盖/副本 POST hit /reports/report/… and
    # 404 while the registry key is the slug. The merge file is report_build's
    # own artifact, so rewriting it is safe.
    spec["reportId"] = report_id
    if spec_path is not None and (spec_path.name.endswith(".merged.json")
                                  or spec_path.name.endswith(".resolved.json")):
        spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    inherit_argv = _inherit_args(workdir, report_id)
    try:
        base_hint = _server_root(args.server or hints.get("serverUrl") or DEFAULT_SERVER_URL)
    except ValueError as exc:
        _write_status(status_path, "failed", mode="unknown", messages=[str(exc)])
        print(str(exc), file=sys.stderr)
        return 2
    # Per-report isolation: resolve an isolated free port so a multi-report
    # build/serve never collides with another report's server.
    server_base = _resolve_report_server(base_hint, workdir, report_id)
    requested_mode = args.mode if args.mode != "auto" else hints.get("mode", "auto")
    # Decide the mode from the PROFILE shape (which now carries per-column
    # cardinality) BEFORE building any canonical JSON. This fixes Issue #4:
    # previously the code built table.json based on a guess, then ran
    # choose_mode — so a server-shaped dataset never needed the rows and a
    # singlefile dataset wasted a full serialization during mode selection.
    try:
        mode = choose_mode(shape, requested_mode, max_rows=threshold,
                           max_bytes=args.max_bytes, max_cardinality=args.max_cardinality,
                           roles=roles)
        if mode == "singlefile":
            canonical_result = run([sys.executable, str(SCRIPTS / "profile_table.py"),
                                    "--table", str(table_path), "--out", str(profile_json),
                                    "--canonical-out", str(table_json)], cwd=workdir)
            if canonical_result.returncode != 0:
                _write_status(status_path, "failed", mode="unknown", messages=[canonical_result.stderr[-2000:]])
                print(canonical_result.stderr[-2000:], file=sys.stderr)
                return 1
            table = json.loads(table_json.read_text(encoding="utf-8"))
            # choose_mode 是在 profile 形状（rows=[None]*rowCount）上预判字节的，
            # 那份「JSON 字节」恒几 KB——真实 canonical JSON 落盘后必须按**实际
            # 字节**复核上限，否则显式/auto singlefile 会静默嵌入超限全量 JSON
            #（多数据集路径本来就按真实文件字节判，这里对齐同一口径）。
            real_bytes = table_json.stat().st_size
            if real_bytes > args.max_bytes:
                raise ValueError(
                    f"singlefile 超出字节上限（canonical JSON {real_bytes:,}/{args.max_bytes:,} bytes）；"
                    "请改用 server 或先预聚合"
                )
        else:
            table = shape
    except ValueError as exc:
        _write_status(status_path, "failed", mode="unknown", messages=[str(exc)])
        print(str(exc), file=sys.stderr)
        return 1
    print(f"[mode] {mode} (rows={len(table['rows'])}, bytes={_json_size(table):,})")

    # 默认即启用在线编辑：server/mixed 模式没写口令就自动生成一个（见 ensure_edit_password）。
    # 口令通过命令行传给编译器（_edit_password_args），绝不改写作者自己的 request 文件。
    ensure_edit_password(spec, mode)

    if mode == "singlefile":
        compile_dataset = str(table_json if table_json.exists() else table_path)
    else:
        # server 模式不写 canonical table.json：同一 workdir 从 singlefile 切到
        # server 重建时，捡到的是上一次构建的**陈旧** canonical 表（旧列集），
        # 而服务端数据走 serve_report --data table_path（新文件）→ 静态
        # option/列统计与 SQLite 列集漂移。显式忽略（不删，保住 singlefile 产物）。
        compile_dataset = str(table_path)
    # 派生字段（计算列 / dataset.derived.columns）在 server 与 singlefile 两种模式都支持：
    # 编译侧物化进行间列（spec_to_report → derived_eval），服务端注册时用同一函数
    # 物化进 SQLite（serve_report 携带声明 → server/derived_columns）。历史上这里
    # 直接拒绝 server 模式，使得「派生统一到数据源管理」对 server 报告不可用。
    compile_argv = [sys.executable, str(SCRIPTS / "spec_to_report.py"),
                    "--spec", str(spec_path), "--out", str(draft),
                    "--dataset", compile_dataset, "--dataset-id", "primary",
                    "--dataset-label", "原始数据", "--mode", mode,
                    # The compiler re-reads the spec from disk, so the in-memory
                    # spec["reportId"] above does NOT reach it — pass the slug
                    # explicitly or the singlefile manifest keeps "report".
                    "--report-id", report_id,
                    # 列统计（数据源配置副行）只在 profile 里算一次；server 模式不写
                    # canonical JSON，所以这份 profile 是唯一的携带通道。
                    "--column-stats", str(profile_json)] + _edit_password_args(spec, mode) + inherit_argv \
                   + (["--sort-input"] if args.sort else [])
    result = run(compile_argv, cwd=workdir)
    if result.returncode != 0:
        _write_status(status_path, "failed", mode=mode, messages=[result.stderr[-2000:]])
        print(result.stderr[-2000:], file=sys.stderr)
        return 1

    validate_argv = [sys.executable, str(SCRIPTS / "validate_report.py"), "--html", str(draft)]
    if mode == "server":
        validate_argv += ["--mode", "server"]
    result = run(validate_argv, cwd=workdir)
    if result.returncode != 0:
        _write_status(status_path, "failed", mode=mode, messages=[result.stderr[-2000:]])
        print(result.stderr[-2000:], file=sys.stderr)
        return 1
    lint = run([sys.executable, str(SCRIPTS / "lint_report.py"), "--html", str(draft)], cwd=workdir)
    if lint.returncode not in {0, 2}:
        _write_status(status_path, "failed", mode=mode, messages=[lint.stderr[-2000:]])
        return 1
    if lint.stdout.strip():
        messages.extend(lint.stdout.strip().splitlines()[-10:])

    if mode == "singlefile":
        result = run([sys.executable, str(SCRIPTS / "package_report.py"),
                      "--html", str(draft), "--out", str(out),
                      "--bi-table", str(table_json), "--dataset-id", "primary",
                      "--dataset-label", "原始数据", "--mode", "singlefile"], cwd=workdir)
        if result.returncode != 0:
            _write_status(status_path, "failed", mode=mode, messages=[result.stderr[-2000:]])
            print(result.stderr[-2000:], file=sys.stderr)
            return 1
        result = run([sys.executable, str(SCRIPTS / "validate_report.py"),
                      "--html", str(out), "--packaged"], cwd=workdir)
        if result.returncode != 0:
            _write_status(status_path, "failed", mode=mode, messages=[result.stderr[-2000:]])
            return 1
        # 单文件路径也一样：HTML 已经写完，审计要开的渲染进程正需要这段内存。
        # 大对象释放统一放在下方 sidecar 落盘之后（AGENTS：release 在 sidecar
        # 之前 = _write_portable_artifacts 拿到 table=None，quality/README 被
        # except 吞掉静默丢失——历史上真踩过）。
        audit_target = str(out)
    else:
        try:
            _ensure_server(server_base, workdir)
        except Exception as exc:
            _write_status(status_path, "failed", mode=mode, messages=[str(exc)])
            print(str(exc), file=sys.stderr)
            return 1
        # Agent publish gate: an --audit-required server build first registers
        # an isolated canary id, runs the full browser audit against it, and
        # only then touches the published report id. The temporary dataset id
        # is namespaced by the canary, so a red audit cannot overwrite the
        # current report's SQLite table or registry entry. Flint's per-encoding
        # aggregate contract (refs/flint/src/core/aggregate.ts:48-55) is one
        # reason a static manifest check is insufficient: live reaggregation
        # must be exercised before publishing.
        stage_audit = None
        if args.audit_required and not args.skip_audit:
            node = shutil.which('node')
            if not node:
                _write_status(status_path, 'failed', mode=mode,
                              messages=['Node.js not found; required pre-publish audit unavailable'])
                return 2
            stage_id = ('stg-' + report_id[:58].rstrip('-') + '-' + secrets.token_hex(5))[:79]
            try:
                staged = _register_single_server(server_base, table_path, spec_path,
                                                 stage_id, profile_json, inherit_argv, workdir)
                if staged.returncode != 0:
                    _write_status(status_path, 'failed', mode=mode,
                                  messages=[f'canary registration failed: {staged.stderr[-2000:]}'])
                    return 1
                stage_audit = _audit_report(node, f'{server_base}/reports/{stage_id}', workdir)
                if not stage_audit.get('passed'):
                    _write_status(status_path, 'failed', mode=mode,
                                  messages=['pre-publish canary audit failed; published report unchanged'],
                                  audit=stage_audit)
                    return 2
            except subprocess.TimeoutExpired:
                _write_status(status_path, 'failed', mode=mode,
                              messages=['canary registration timed out; published report unchanged'])
                return 1
            finally:
                try:
                    _unregister_staged_report(server_base, stage_id)
                except Exception as exc:
                    messages.append(f'canary cleanup warning: {exc}')
        try:
            result = _register_single_server(server_base, table_path, spec_path,
                                             report_id, profile_json, inherit_argv, workdir)
        except subprocess.TimeoutExpired:
            # 超时同样要走 status 落盘：裸 traceback 会让 build-status.json 停在
            # 上一次构建的旧值（可能还是 ready）。
            msg = "register timed out after 1200s（服务端未确认注册）"
            _write_status(status_path, "failed", mode=mode, messages=[msg])
            print(msg, file=sys.stderr)
            return 1
        if result.returncode != 0:
            _write_status(status_path, "failed", mode=mode, messages=[result.stderr[-2000:]])
            print(result.stderr[-2000:], file=sys.stderr)
            return 1
        _ship_portable_data(workdir, report_id)
        result = run([sys.executable, str(SCRIPTS / "package_report.py"),
                      "--html", str(draft), "--mode", "server", "--out", str(out),
                      "--server-url", f"{server_base}/reports/{report_id}"], cwd=workdir)
        if result.returncode != 0:
            _write_status(status_path, "failed", mode=mode, messages=[result.stderr[-2000:]])
            print(result.stderr[-2000:], file=sys.stderr)
            return 1
        _release_build_memory()
        audit_target = f"{server_base}/reports/{report_id}"

    try:
        _write_portable_artifacts(workdir, mode=mode, report_id=report_id,
                                  draft=draft, output=out, table=table,
                                  server_base=server_base, spec=spec,
                                  patch_keys=patch_keys, build_messages=list(messages),
                                  has_prev_manifest=(workdir / 'prev-manifest.json').exists())
    except Exception as exc:
        # Sidecars are useful audit aids but must never turn an already-valid
        # report into a false failure. Keep the reason visible in build status.
        messages.append(f"sidecar artifact warning: {exc}")

    # sidecar 落盘之后、打开审计之前释放编译期大对象（与 mixed 路径同一顺序；
    # _write_portable_artifacts 还要读 table 写 quality/README，释放必须在其后）。
    payload_ds = None
    body = None
    table = None
    _release_build_memory()

    # Server-mode edit password: never leave the author guessing the token that
    # gates online save. Print it at build time (report page no longer shows it).
    # mixed counts too: the report is edited through the server just the same.
    if mode in ("server", "mixed"):
        _edit_password = (spec.get("runtime") or {}).get("editPassword")
        if _edit_password:
            _auto = "（本次自动生成）" if (spec.get("runtime") or {}).get("editPasswordGenerated") else ""
            print(f"[password] 本报告在线编辑口令：{_edit_password}{_auto}"
                  f" 用于「保存与分享 → 保存为副本 / 保存并覆盖」；"
                  f"可随时在 {server_base}/manager 修改口令或关闭在线编辑")

    status = "ready"
    report = None
    if args.skip_audit:
        status = "needs_audit"
        messages.append("browser audit skipped by request")
    elif stage_audit is not None:
        # The audited canary was compiled from the same spec/table and used a
        # distinct namespace; only reportId differs from the promoted entry.
        report = stage_audit
        messages.append("pre-publish canary passed full browser audit")
    else:
        node = shutil.which("node")
        if not node:
            status = "needs_audit"
            messages.append("Node.js not found; browser audit deferred")
        else:
            report = _audit_report(node, audit_target, workdir)
            if not report.get("passed"):
                status = "failed" if args.audit_required else "needs_audit"
                messages.append(f"browser audit failed or deferred: {report}")

    _write_status(status_path, status, mode=mode, messages=messages, audit=report)
    print(f"=== DONE ({mode}, {status}): {out} ===")
    if mode == "server" and status != "failed":
        _print_server_access(server_base, report_id)
    return 0 if status != "failed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
