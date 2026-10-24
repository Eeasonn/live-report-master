#!/usr/bin/env python3
"""Register a report with a running AI report server.

Commands:
    register   Build a manifest from spec + data and push it to the server.
    list       List registered reports (calls /api/reports).
    datasets   List loaded datasets (calls /api/datasets).
    unregister Remove a report from the registry.
    reload     Force the server to re-read a dataset file from disk.
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
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))
if str(SKILL_DIR / "scripts") not in sys.path:
    sys.path.insert(0, str(SKILL_DIR / "scripts"))


DEFAULT_SERVER = "http://127.0.0.1:8765"

from server._platform import urlopen_loopback  # noqa: E402


def _http_json(method: str, url: str, body: dict | None = None, timeout: int = 30) -> dict:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urlopen_loopback(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            payload = {"raw": raw.decode("utf-8", errors="replace")}
        print(f"[serve-report] HTTP {e.code}: {payload}", file=sys.stderr)
        raise SystemExit(2)
    except urllib.error.URLError as e:
        print(f"[serve-report] connection failed: {e.reason}", file=sys.stderr)
        raise SystemExit(2)


# ---------------------- CLI subcommands -----------------------------------------

def _compiled_manifest(spec_path: Path, table: dict, report_id: str, server_base: str,
                       inherit: dict | None = None) -> dict:
    """Use the canonical compiler, then convert the manifest to server form.

    ``inherit``（月度刷新继承）原样透传给 compile_spec——单表 server 注册与
    report_build 走同一条继承管线（curation_inherit）。"""
    from scripts.spec_to_report import compile_spec
    from scripts.validate_report import MANIFEST_RE
    import json as _json
    _spec = _json.loads(spec_path.read_text(encoding="utf-8"))
    _roles = dict(_spec.get("columnRoles") or {})
    # Map a declared columnRoles role to a server dataset column type so the
    # runtime sees YearMonth as a TIME column (not a numeric measure). Without
    # this, the profile (whose measure inference reads YYYYMM as numeric) types
    # YearMonth as 'measure', so it shows as 指标 and is excluded from the
    # dimension list. time→time, measure→measure, dimension/id→dimension.
    _ROLE_TO_TYPE = {"time": "time", "measure": "measure"}

    def _col_type(name: str, fallback: str) -> str:
        role = _roles.get(name)
        if role in _ROLE_TO_TYPE:
            return _ROLE_TO_TYPE[role]
        if role in ("dimension", "id"):
            return "dimension"
        return fallback
    # Namespace the dataset id per report so multiple reports never collide
    # (Issue #8): each report owns a unique dataset id like ``{reportId}__primary``.
    dataset_id = f"{report_id}__primary"
    with tempfile.TemporaryDirectory(prefix="ai-report-build-") as tmp:
        dataset_path = Path(tmp) / "table.json"
        draft_path = Path(tmp) / "draft.html"
        dataset_path.write_text(json.dumps(table, ensure_ascii=False), encoding="utf-8")
        compile_spec(str(spec_path), str(draft_path), str(dataset_path), dataset_id, "原始数据",
                     inherit=inherit)
        html = draft_path.read_text(encoding="utf-8")
        match = MANIFEST_RE.search(html)
        if not match:
            raise ValueError("compiled draft has no manifest")
        manifest = json.loads(match.group(1))
    manifest["reportId"] = report_id
    manifest["mode"] = "server"
    manifest["serverBase"] = server_base
    _profile_types = (table.get("meta") or {}).get("types") or {}
    for ds in manifest.get("datasets", []):
        rows = ds.pop("rows", None)
        if rows is not None:
            ds["rowCount"] = len(rows)
        columns = ds.get("columns") or []
        # Embed capped distinct values per non-measure column so the runtime
        # filter dropdowns have real options in server mode: the client can
        # only derive them from dataset.rows, which a server manifest strips.
        # Same 50-value cap as the client's distinctMap (editor-runtime.js).
        if rows is not None and columns:
            opts = {}
            col_idx = {c: i for i, c in enumerate(columns) if isinstance(c, str)}
            for col, ci in col_idx.items():
                if _col_type(col, _profile_types.get(col, "dimension")) == "measure":
                    continue
                seen = {}
                for r in rows:
                    v = r[ci] if isinstance(r, list) and ci < len(r) else None
                    if v is None or v == "":
                        continue
                    seen.setdefault(str(v), v)
                    if len(seen) >= 50:
                        break
                if seen:
                    opts[col] = list(seen.values())
            if opts:
                ds["filterOptions"] = opts
        if columns and isinstance(columns[0], str):
            types = _profile_types
            ds["columns"] = [{"name": col, "type": _col_type(col, types.get(col, "dimension"))} for col in columns]
            # Surface the declared roles as fieldRoles so the runtime's
            # dimensionKind honours an explicit time role (WYSIWYG) over the
            # value heuristics, and the editor pairs the correct axis kind.
            _fr = {c: {"role": r} for c, r in _roles.items() if r in ("time", "ordinal", "category") and c in columns}
            if _fr:
                ds["fieldRoles"] = {**(ds.get("fieldRoles") or {}), **_fr}
        ds["id"] = dataset_id
    for chart in manifest.get("charts", []):
        data = (chart.get("editor") or {}).get("data") or {}
        data.pop("rows", None)
        data["datasetId"] = dataset_id
        chart.setdefault("editor", {})["data"] = data
        # Server mode: line/bar/area/scatter rebuild their series via /aggregate
        # at runtime, so the baked ECharts option must NOT carry the precomputed
        # data arrays — for a large table they can balloon the manifest to tens
        # of MB (e.g. a scatter over 800k rows = 800k points in series.data).
        # Keep the baked data for share charts (pie/donut/funnel) and any
        # breakdown/matrix chart (stackedBar with a 2nd dimension), which rely on
        # the pre-computed option and do not re-aggregate. Cap any remaining data
        # array defensively so a high-cardinality dimension cannot blow up size.
        vt = chart.get("viewType")
        roles = data.get("roles") or {}
        is_share = vt in ("pie", "donut", "funnel")
        is_matrix = bool(roles.get("breakdown") or roles.get("source") or roles.get("target"))
        option = chart.get("option") or {}
        if isinstance(option.get("series"), list) and not is_share and not is_matrix:
            for s in option["series"]:
                if isinstance(s, dict):
                    if isinstance(s.get("data"), list):
                        s["data"] = s["data"][:800]
                    if isinstance(s.get("items"), list):
                        s["items"] = s["items"][:800]
        elif vt == "table":
            option.pop("rows", None)
    return manifest


def cmd_register(args):
    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    from scripts.validate_request import validate_request
    validate_request(spec)
    if spec.get("datasets"):
        # Direct register is single-table by design. For multi-dataset requests,
        # delegate to report_build, which profiles each table and chooses
        # singlefile or mixed (inline + remote) automatically.
        import subprocess
        rb = SKILL_DIR / "scripts" / "report_build.py"
        argv = [sys.executable, str(rb), "--spec", args.spec]
        if getattr(args, "data", None):
            argv += ["--table", args.data]
        if args.report_id:
            argv += ["--report-id", args.report_id]
        if args.mode:
            argv += ["--mode", args.mode]
        if getattr(args, "inherit", None):
            # 月度刷新继承对多数据集同样适用：report_build 自己解析 --inherit
            # （_inherit_args 全路接线），转发而不是无声丢弃。
            argv += ["--inherit", args.inherit]
        argv += ["--server", args.server]
        print("多数据集注册已转交 report_build（自动 singlefile/mixed）")
        return subprocess.call(argv)
    if spec.get("derivedColumns"):
        # Legacy top-level declaration — validate_request already rejects it with
        # a pointer to datasets[].derived.columns, so reaching here means the
        # caller bypassed validation. Say so instead of silently dropping it.
        raise SystemExit("derivedColumns 已废弃；请改用 datasets[0].derived.columns（见 validate_request 提示）")
    # columnRoles is optional (roles are inferred from profile by the compiler),
    # matching the singlefile path and the SKILL contract. No hard requirement here.
    runtime = spec.get("runtime") or {}
    mode = args.mode or runtime.get("mode", "server")
    server_base = "/api/v1"

    # Load data
    from server.registry import load
    table = load(Path(args.data))
    # 列统计（数据源配置字段副行）：server 模式刻意不写 canonical JSON，所以
    # report_build 把 profile 产物直接传进来；_compiled_manifest 会把 table 写成临时
    # canonical JSON，validate_table 保留 meta.stats（见那里的 keep 列表），于是
    # manifest.datasets[].columnStats 有值。缺参数时保持为空（UI 退化成“—”，不猜）。
    if args.profile:
        try:
            _prof = json.loads(Path(args.profile).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _prof = {}
        _stats = _prof.get("columnStats") if isinstance(_prof, dict) else None
        if isinstance(_stats, dict) and _stats:
            table.setdefault("meta", {})["stats"] = _stats

    if mode == "singlefile":
        raise SystemExit("serve_report register only accepts server mode; use report_build for singlefile")

    # Compile manifest through the single authoritative deterministic compiler.
    _inherit = None
    if getattr(args, "inherit", None):
        try:
            _inherit = json.loads(Path(args.inherit).read_text(encoding="utf-8"))
        except (OSError, ValueError) as _exc:
            raise SystemExit(f"--inherit 读取失败（继承是增益，可去掉该参数重跑）：{_exc}")
    manifest = _compiled_manifest(Path(args.spec), table, args.report_id, server_base,
                                  inherit=_inherit)
    if args.title:
        manifest["title"] = args.title

    # Push to server. The server namespaces the dataset id as ``{reportId}__{id}``
    # (app.py h_register), so we pass the RAW id "primary" here; the manifest
    # above already carries the final namespaced id for client fetches.
    # Row-level derived columns (计算列) are supported in server mode too: the
    # server materialises the declaration into SQLite with the SAME function the
    # compiler uses (server/derived_columns.py → scripts/derived_eval.py), and
    # the snapshot file stays raw. The declaration is taken from the compiled
    # manifest (single source) and shipped with the dataset payload.
    from server.store import columns_decl
    _primary_ds = (manifest.get("datasets") or [{}])[0] or {}
    derived_decl = columns_decl(_primary_ds.get("derived"))
    primary_pk = _primary_ds.get("primaryKey")
    groups_decl = _primary_ds.get("dimGroups")
    dataset_payload = {
        "id": "primary",
        "label": "原始数据",
        "source": str(Path(args.data).resolve()),
        "table": table,
    }
    if derived_decl:
        dataset_payload["derived"] = derived_decl
    if primary_pk:
        # 增量合并主键（datasets[].primaryKey）：从编译产物直通注册 payload，
        # h_register 持久化进注册表 meta（与 report_build 的 mixed 路径同契约）。
        dataset_payload["primaryKey"] = primary_pk
    if groups_decl:
        # 维度值分组（ds.dimGroups）与 derived/primaryKey 同一条「编译产物直通
        # 注册 payload」契约（report_build mixed 路径的 groups_by_ds 同款）：
        # h_register 只认 payload 里的声明，物化分组列 + 落注册表供重启 replay。
        # 漏了 = manifest.datasets 声称有该列（编译侧已物化进静态列清单）、
        # SQLite 没有 → 服务端引用它的聚合直接 no such column，且注册表无声明、
        # 重启 replay 也补不回来（单表 server 注册此前漏带，真机批次三对齐时
        # mixed 路径修了、这条路漏了）。
        dataset_payload["dimGroups"] = groups_decl
    payload = {
        "reportId": args.report_id,
        "title": manifest["title"],
        "manifest": manifest,
        "dataset": dataset_payload,
    }
    res = _http_json("POST", f"{args.server}/api/admin/registry/register", payload, timeout=120)
    print(f"Registered {res.get('reportId')} (dataset rowCount={res.get('rowCount')})")
    print(f"Open: {args.server}/reports/{res.get('reportId')}")


def cmd_list(args):
    res = _http_json("GET", f"{args.server}/api/reports")
    for r in res.get("reports", []):
        print(f"  {r['id']:<24}  {r['title']}  ({r['chartCount']} charts, mode={r['mode']})")


def cmd_datasets(args):
    res = _http_json("GET", f"{args.server}/api/datasets")
    for d in res.get("datasets", []):
        print(f"  {d['id']:<24}  rows={d['rowCount']:<8}  cols={d['columnCount']}")


def cmd_unregister(args):
    res = _http_json("POST", f"{args.server}/api/admin/registry/unregister",
                     {"reportId": args.report_id})
    print(f"Unregistered: {res}")


def cmd_reload(args):
    res = _http_json("POST", f"{args.server}/api/reports/{args.report_id}/reload", {})
    print(f"Reloaded: {res}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Register a report with a running AI report server")
    parser.add_argument("--server", default=DEFAULT_SERVER, help="server base URL")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_reg = sub.add_parser("register", help="compile + register a report")
    p_reg.add_argument("--data", required=True, help="data file (xlsx/csv/json)")
    p_reg.add_argument("--spec", required=True, help="report_request.json")
    p_reg.add_argument("--report-id", required=True)
    p_reg.add_argument("--title", default=None)
    p_reg.add_argument("--profile", default=None,
                       help="profile.json (profile_table) — carries per-column stats into manifest.datasets[].columnStats")
    p_reg.add_argument("--mode", choices=("auto", "server", "singlefile"), default=None)
    p_reg.add_argument("--inherit", default=None,
                       help="上一份 manifest JSON：月度刷新继承 dataset 级策展")
    p_reg.set_defaults(fn=cmd_register)

    p_list = sub.add_parser("list", help="list registered reports")
    p_list.set_defaults(fn=cmd_list)

    p_ds = sub.add_parser("datasets", help="list loaded datasets")
    p_ds.set_defaults(fn=cmd_datasets)

    p_un = sub.add_parser("unregister", help="unregister a report")
    p_un.add_argument("--report-id", required=True)
    p_un.set_defaults(fn=cmd_unregister)

    p_re = sub.add_parser("reload", help="reload a report's dataset(s) from disk")
    p_re.add_argument("--report-id", required=True)
    p_re.set_defaults(fn=cmd_reload)

    args = parser.parse_args()
    return args.fn(args) or 0


if __name__ == "__main__":
    raise SystemExit(main())
