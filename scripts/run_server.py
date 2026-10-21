#!/usr/bin/env python3
"""Start the AI report server.

Usage:
    python scripts/run_server.py [--host 127.0.0.1] [--port 8765]
                             [--registry PATH] [--data-dir PATH]
                             [--quiet]

Loads the registry from disk, re-loads every referenced dataset into a
file-backed SQLite database, and starts the HTTP server.

For Windows, you can run this in the background with:
    start /b python scripts/run_server.py --port 8765

For ordinary Windows use, run scripts\\start_server.cmd and
scripts\\stop_server.cmd; both target only the PID file in the report bundle.
"""
from __future__ import annotations

# Running from a deployed skill folder must not litter it with __pycache__/:
# the env var is inherited by spawned children (lint/package/spec_to_report),
# the flag covers imports made after this line in this process.
import os as _os, sys as _sys
_os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
_sys.dont_write_bytecode = True
import argparse
import os
import sys
import time
from pathlib import Path

# Make the skill package importable when running from any cwd.
SKILL_DIR = Path(__file__).resolve().parent.parent
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))
if str(SKILL_DIR / "scripts") not in sys.path:
    sys.path.insert(0, str(SKILL_DIR / "scripts"))

from server.app import dispatch_queued_job, run_server          # noqa: E402
from server.loopback import urlopen_loopback                   # noqa: E402
from server.registry import Registry, DEFAULT_DATA_DIR, DEFAULT_REGISTRY   # noqa: E402
from server.store import get_engine                            # noqa: E402
from server.template import TemplateProvider                   # noqa: E402


def _select_engine_policy(registry, requested: str) -> tuple[bool, dict[str, object]]:
    """Choose a startup engine from bounded workload facts.

    The server owns one loaded engine, so this is a workload-class policy—not
    unsafe per-request engine hopping.  It distinguishes interactive/local
    reports from large scans and wide aggregate workloads, and returns the
    reason for health/diagnostic visibility.
    """
    entries = registry.raw_entries()
    total_rows = 0
    max_rows = 0
    max_columns = 0
    for entry in entries:
        manifest = entry.get("manifest") or {}
        for dataset in manifest.get("datasets") or []:
            if not isinstance(dataset, dict):
                continue
            rows = int(dataset.get("rowCount") or 0)
            columns = dataset.get("columns") or []
            total_rows += rows
            max_rows = max(max_rows, rows)
            max_columns = max(max_columns, len(columns))
    profile = {"totalRows": total_rows, "maxRows": max_rows,
               "maxColumns": max_columns, "reportCount": len(entries)}
    if requested == "sqlite":
        return False, {"requested": requested, "selected": "sqlite",
                       "workloadClass": "interactive_local", "reason": "explicit sqlite", **profile}
    if requested == "duckdb":
        return True, {"requested": requested, "selected": "duckdb",
                      "workloadClass": "explicit_scan", "reason": "explicit duckdb", **profile}
    try:
        import duckdb  # noqa: F401
        duckdb_available = True
    except ImportError:
        duckdb_available = False
    if not duckdb_available:
        return False, {"requested": requested, "selected": "sqlite",
                       "workloadClass": "interactive_local", "reason": "duckdb unavailable",
                       **profile}
    if max_rows >= 500_000 or (max_columns >= 32 and max_rows >= 100_000):
        return True, {"requested": requested, "selected": "duckdb",
                      "workloadClass": "large_scan_or_wide_aggregate",
                      "reason": "large scan/wide aggregate workload", **profile}
    return False, {"requested": requested, "selected": "sqlite",
                   "workloadClass": "interactive_local", "reason": "bounded local workload", **profile}


def _reload_all(registry: Registry, engine, verbose: bool = True) -> None:
    from server.derived_columns import replay_all as replay_columns
    from server.loader import load
    from server.relations import is_derived, replay_entry
    # Two passes: file-backed datasets first, then derived output (wide tables
    # merge the sources; 计算列 replay needs the whole file pass done, see
    # server/derived_columns.replay_all).
    for entry in registry.list():
        for d in entry.get("datasets", []):
            if is_derived(d):
                continue
            src = d.get("source")
            if not src:
                continue
            p = Path(src)
            if not p.is_absolute():
                p = registry.path.parent / p
            if not p.exists():
                if verbose:
                    print(f"[ai-report-server] WARN dataset {d['id']!r} source not found: {src}", file=sys.stderr)
                continue
            try:
                table = load(p)
                engine.drop_table(d["id"])
                # 索引列声明随注册表 meta 走（h_register 持久化）：重载是
                # DROP+CREATE，不透传 = 索引悄悄消失（与 app._reload_dataset
                # 同一条契约，别只修一处）。
                info = engine.load_table(d["id"], table["columns"], table["rows"],
                                         index_columns=list(d.get("indexColumns") or []))
                if verbose:
                    print(f"[ai-report-server] loaded {d['id']!r} ({info.row_count} rows, {len(info.columns)} cols) from {src}")
            except Exception as e:
                if verbose:
                    print(f"[ai-report-server] WARN failed to load {d['id']!r}: {e}", file=sys.stderr)
    # 计算列（replay_columns）必须先于关系宽表（replay_entry）：宽表由引擎里的
    # 源表重合并而来，源表的计算列还没物化就合并，宽表会永久缺失该列（columns_decl
    # 对 kind:'relation' 返回 None，第三遍补不到宽表）——与 h_reload_report /
    # h_reload_registry 的「columns 先、relations 后」顺序保持一致。
    # It MUST run after EVERY file-backed load because report copies share their
    # parent's dataset id (one engine table, DROP+CREATE per load) — applying
    # declarations per dataset would let a copy without one wipe the column.
    for _ds in replay_columns(engine, registry, verbose=verbose):
        if verbose:
            print(f"[ai-report-server] materialised 计算列 on {_ds!r}")
    for entry in registry.list():
        for item in replay_entry(engine, entry, verbose=verbose):
            if verbose:
                print(f"[ai-report-server] rebuilt derived {item['id']!r} ({item['rowCount']} rows)")
        registry.register(entry["reportId"], entry)


def _find_shipped_snapshot(data_dir: Path, report_id: str, ds_id: str) -> Path | None:
    """在随包的 ``build/data/`` 里找数据集快照。

    优先级 = 数据的新鲜度：**运行时替换 > 运行时追加 > 构建期 canonical**。
    运行时「替换数据」把权威真值写进 ``.replaced.json`` 并把注册表 source 改
    指它（server/app.py ``_process_replace_table``），构建期 ``table-primary``
    旧快照并不删除、两代会同时随包（``_ship_portable_data`` 按 ``{report_id}__*``
    全量拷）——组序倒置会让便携重注册恒选中旧快照：注册表 rowCount 声称新、
    聚合全是旧数（静默错数，2026-09-26 甄别）。

    随包命名有两代：mixed 注册快照 = ``{report_id}__{ds_id}__*``（ds_id 已是
    final 名）；运行时替换/追加与单表注册 = ``{report_id}__{raw}*``——两种都
    试。兜底 glob 必须带边界（``[._-]``／``__``）：裸 ``{raw}*`` 会让 ``sales``
    命中 ``sales2__table-primary.json``，兄弟数据集数据串台且无告警。
    """
    if not data_dir.exists():
        return None
    raw = ds_id
    prefix = f"{report_id}__"
    if ds_id.startswith(prefix):
        raw = ds_id[len(prefix):]
    prefs = [
        # 运行时权威（app.py 写进中央 state home，随包两代并存时它更新）
        [data_dir / f"{report_id}__{ds_id}.replaced.json",
         data_dir / f"{report_id}__{raw}.replaced.json"],
        [data_dir / f"{report_id}__{ds_id}.added.json",
         data_dir / f"{report_id}__{raw}.added.json"],
        # 构建期 canonical（mixed 注册 / 单表注册）
        [data_dir / f"{report_id}__{ds_id}__table-primary.json",
         data_dir / f"{report_id}__{raw}__table-primary.json"],
    ]
    for group in prefs:
        for p in group:
            if p.exists():
                return p
    # 兜底（表名非 table-primary 的历史包等）：按 ``__``/``.`` 边界自匹配，
    # 绝不让 ``sales`` 命中 ``sales2__…``（首字符必须是边界符）。
    for stem in (ds_id, raw):
        hits = sorted(data_dir.glob(f"{report_id}__{stem}[._-]*"))
        if hits:
            return hits[0]
    return None


def _shipped_report_bundle(registry, folder: Path, verbose: bool = True):
    """读随包 ``build/manifest.json`` + ``build/data/`` → (report_id, manifest, items)。

    冷启动注册（``_register_report_folder`` 直写注册表）与温启动注册
    （``_register_report_warm`` 经 HTTP 调活 server）共用同一份解析；快照查找
    复用 ``_find_shipped_snapshot``（兼容 mixed 与单表两代命名）。manifest
    缺失/非法 → 返回 None（原因打印到 stderr，不抛）。items 每项 =
    ``{ds, snapshot}``（manifest 数据集条目 + 随包快照路径）或 ``{ds, relation}``
    （运行时「立即合并」的关系宽表：没有快照文件，携带 manifest.relations 里
    的关系声明——注册时按 ``relations.derived_entry`` 登记，replay 从源表重建；
    2026-09-26 之前这里只打 WARN 跳过，收件机跨源图表永久 400）。两者都不是
    才跳过并提示。
    """
    import json as _json
    import re as _re

    from server.relations import wide_dataset_id

    build = folder / "build"
    manifest_path = build / "manifest.json"
    if not manifest_path.exists():
        if verbose:
            print(f"[ai-report-server] WARN --register-report: {manifest_path} 不存在，跳过", file=sys.stderr)
        return None
    try:
        manifest = _json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"[ai-report-server] WARN --register-report: manifest 读取失败：{exc}", file=sys.stderr)
        return None
    report_id = str(manifest.get("reportId") or "")
    if not _re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{1,79}", report_id):
        print(f"[ai-report-server] WARN --register-report: manifest.reportId 非法：{report_id!r}", file=sys.stderr)
        return None
    data_dir = build / "data"
    items: list[dict] = []
    relations = [r for r in (manifest.get("relations") or []) if isinstance(r, dict)]
    wide_ids: dict[str, dict] = {}
    for rel in relations:
        try:
            wide_ids[str(wide_dataset_id(rel))] = rel
        except (ValueError, TypeError):
            continue
    for ds in (manifest.get("datasets") or []):
        if not isinstance(ds, dict) or not ds.get("id"):
            continue
        src = _find_shipped_snapshot(data_dir, report_id, str(ds["id"]))
        if src is None:
            rel = wide_ids.get(str(ds["id"]))
            if rel is not None:
                items.append({"ds": ds, "relation": rel})
                continue
            if verbose:
                print(f"[ai-report-server] WARN 数据集 {ds['id']!r} 在 build/data/ 无快照，跳过注册"
                      "（relations 宽表由 replay 从源表重建，可忽略）", file=sys.stderr)
            continue
        items.append({"ds": ds, "snapshot": src})
    return report_id, manifest, items


def _register_report_folder(registry, folder: Path, port: int, verbose: bool = True) -> str | None:
    """把打包来的报告文件夹（含 ``build/manifest.json`` + ``build/data/``）注册进
    本机中央 registry（按 reportId 幂等 upsert）。

    便携启动器（可移植自举 2026-09-25）冷启动时传 ``--register-report <folder>``：
    对方机器的中央 registry 是空的，没有这一步报告永远 404。数据文件从文件夹的
    ``build/data/`` 拷进本机状态目录（沿用 registry 的 state-home 相对 source 约定，
    与 h_register→snapshot_data_file 的产物同形）；``serverBase`` 归一为相对
    ``/api/v1``——注册后的报告与本机服务的实际端口解耦（LRM_PORT 换端口不漂）。
    快照**无条件覆盖**（copy2 同内容幂等）：更新版报告文件夹重注册时 manifest
    已换新，若因 dest.exists() 跳过拷贝就还是旧数据——manifest 声称新行数、
    聚合全是旧数，静默错数。
    """
    import shutil as _shutil

    bundle = _shipped_report_bundle(registry, folder, verbose=verbose)
    if bundle is None:
        return None
    report_id, manifest, items = bundle
    ds_entries: list[dict] = []
    for item in items:
        ds = item["ds"]
        ds_id = str(ds["id"])
        if "relation" in item:
            # 运行时「立即合并」的关系宽表：没有快照文件——按 relations 同款
            # 声明登记（source=None + derived.kind=relation），boot/reload 的
            # replay_entry 从已装载的源表重建引擎表。只打 WARN 跳过的旧行为
            # 会让收件机引用宽表的图表永久 400（无声明 = replay 不接管）。
            from server.relations import derived_entry
            try:
                ds_entries.append(derived_entry(ds_id, item["relation"],
                                                int(ds.get("rowCount") or 0)))
            except (TypeError, ValueError) as exc:
                print(f"[ai-report-server] WARN --register-report: 宽表 {ds_id!r} "
                      f"关系声明非法，跳过：{exc}", file=sys.stderr)
            continue
        ds, src = item["ds"], item["snapshot"]
        dest = registry.data_dir / src.name
        _shutil.copy2(src, dest)
        try:
            source_value = os.path.relpath(dest, registry.path.parent)
        except ValueError:
            source_value = str(dest)
        entry = {"id": ds_id, "label": str(ds.get("label") or ds_id),
                 "rowCount": ds.get("rowCount"), "source": source_value}
        if isinstance(ds.get("derived"), dict) and ds["derived"]:
            entry["derived"] = ds["derived"]
        if isinstance(ds.get("dimGroups"), list) and ds["dimGroups"]:
            entry["dimGroups"] = ds["dimGroups"]
        if isinstance(ds.get("primaryKey"), list) and ds["primaryKey"]:
            entry["primaryKey"] = [str(c) for c in ds["primaryKey"]]
        ds_entries.append(entry)
    if not ds_entries:
        print("[ai-report-server] WARN --register-report: 没有可注册的数据集，跳过", file=sys.stderr)
        return None

    # 相对 serverBase：注册后的报告与本机服务端口解耦（构建期可能烘焙了
    # 构建机的绝对地址——换机器/换端口都不可用）。
    manifest["serverBase"] = "/api/v1"
    manifest["reportId"] = report_id
    title = str(manifest.get("title") or report_id)
    from server.app import _apply_policy_to_manifest, _change_entry, edit_policy_of
    policy = edit_policy_of({"manifest": manifest})
    _apply_policy_to_manifest(manifest, policy)
    registry.register(report_id, {
        "title": title,
        "manifest": manifest,
        "datasets": ds_entries,
        "family": report_id,
        "edit": policy,
        "changelog": [_change_entry("initial", report_id, title, manifest)],
    })
    if verbose:
        print(f"[ai-report-server] 已从报告文件夹注册 {report_id!r}（{len(ds_entries)} 个数据集）"
              f"→ http://127.0.0.1:{port}/reports/{report_id}")
    return report_id


def _find_live_lrm_server(state_home: Path, preferred_port: int,
                          host: str = "127.0.0.1") -> str | None:
    """本机活着的、可复用的 LRM 报告 server（report_build._find_live_server 同判据）。

    先查 ``~/.ai-html-report/servers.json`` 索引、再扫 preferred..+199 端口段；
    复用判据 = /api/health 带 ``registryPath`` 标记（LRM marker——别的应用的
    health 绝不认）**且**对方注册表落在本 state home 内（LRM_STATE_HOME 隔离
    world 互不串扰，与 report_build._in_state_home 同一条边界）。
    """
    import json as _json
    import urllib.error as _urlerr
    import urllib.request as _urlreq

    def _registry_of(base: str) -> str | None:
        try:
            with urlopen_loopback(base.rstrip("/") + "/api/health", timeout=2) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
        except (OSError, ValueError, _urlerr.URLError):
            return None
        reg = data.get("registryPath") if isinstance(data, dict) else None
        return str(reg) if isinstance(reg, str) and reg else None

    def _reusable(base: str) -> bool:
        reg = _registry_of(base)
        if reg is None:
            return False
        try:
            reg_abs = os.path.abspath(reg)
            home_abs = os.path.abspath(str(state_home))
            return os.path.commonpath([reg_abs, home_abs]) == home_abs
        except ValueError:
            return False

    try:
        # 启动器的 $URL 烘焙在本端口上：优先探测它（活 LRM server 正服务
        # 本端口 = 温启动注册的确定目标），再走索引与端口段。
        preferred = f"http://{host}:{int(preferred_port)}"
        if _reusable(preferred):
            return preferred
    except (OSError, ValueError, TypeError):
        pass
    try:
        index = _json.loads((state_home / "servers.json").read_text(encoding="utf-8"))
        for port_str in sorted(index, key=lambda s: int(s) if str(s).isdigit() else 10 ** 9):
            base = f"http://{host}:{int(port_str)}"
            if _reusable(base):
                return base
    except (OSError, ValueError, TypeError):
        pass
    for p in range(preferred_port, preferred_port + 200):
        base = f"http://{host}:{p}"
        if _reusable(base):
            return base
    return None


def _register_report_warm(base: str, registry, folder: Path, verbose: bool = True) -> str | None:
    """温启动注册：本机已有活着的 LRM server（统一 manager）时，把随包报告
    文件夹经 HTTP 幂等 upsert 进它，调用方随后退出（**绝不拉第二个 server 进程**）。

    便携启动器温启动（2026-09-26）：server 已活着时启动器模板完全不传
    ``--register-report``，更新版报告文件夹的新数据毫无机会——现在启动器在
    健康检查通过时也调本路径。数据落盘走 h_register 既有管线：payload 的
    dataset.source 指向随包快照，活 server 的 snapshot_data_file 把它**无条件
    copy2** 进中央 state home（同内容幂等、更新版覆盖旧版）→ 引擎装载 →
    注册表按 reportId upsert。``serverBase`` 归一相对 ``/api/v1``（端口解耦）
    的既有逻辑保持。
    """
    import json as _json
    import urllib.error as _urlerr
    import urllib.request as _urlreq

    bundle = _shipped_report_bundle(registry, folder, verbose=verbose)
    if bundle is None:
        return None
    report_id, manifest, items = bundle
    prefix = f"{report_id}__"
    payload_ds: list[dict] = []
    for item in items:
        ds = item["ds"]
        ds_id = str(ds["id"])
        if "relation" in item:
            # 关系宽表走声明直通：h_register 对 derived.kind=relation 的 payload
            # 登记 derived 条目并就地从源表 replay（快照/行都不存在）。
            payload_ds.append({
                "id": ds_id,
                # h_register（multi）按 raw 派生快照命名空间：final id 去掉报告
                # 前缀（宽表声明不用它命名快照，但保持 payload 同形）。
                "raw": ds_id[len(prefix):] if ds_id.startswith(prefix) else ds_id,
                "label": str(ds.get("label") or ds_id),
                "rowCount": ds.get("rowCount"),
                "derived": {"kind": "relation", "relation": item["relation"]},
            })
            continue
        try:
            table = _json.loads(item["snapshot"].read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"[ai-report-server] WARN --register-report: 快照 {item['snapshot'].name} "
                  f"读取失败：{exc}", file=sys.stderr)
            return None
        entry = {
            "id": ds_id,
            # h_register（multi）按 raw 派生快照命名空间：final id 去掉报告前缀，
            # 同一文件夹重复注册得到同一快照名（幂等 upsert 的前提）。
            "raw": ds_id[len(prefix):] if ds_id.startswith(prefix) else ds_id,
            "label": str(ds.get("label") or ds_id),
            "rowCount": ds.get("rowCount"),
            "source": str(item["snapshot"]),
            "table": {"columns": list(table.get("columns") or []),
                      "rows": [list(r) for r in (table.get("rows") or [])]},
        }
        if isinstance(ds.get("derived"), dict) and ds["derived"]:
            entry["derived"] = ds["derived"]
        if isinstance(ds.get("dimGroups"), list) and ds["dimGroups"]:
            entry["dimGroups"] = ds["dimGroups"]
        if isinstance(ds.get("primaryKey"), list) and ds["primaryKey"]:
            entry["primaryKey"] = [str(c) for c in ds["primaryKey"]]
        payload_ds.append(entry)
    if not payload_ds:
        print("[ai-report-server] WARN --register-report: 没有可注册的数据集，跳过", file=sys.stderr)
        return None
    manifest["serverBase"] = "/api/v1"
    manifest["reportId"] = report_id
    body = {"reportId": report_id, "title": str(manifest.get("title") or report_id),
            "manifest": manifest, "multi": True, "datasets": payload_ds}
    req = _urlreq.Request(base.rstrip("/") + "/api/admin/registry/register",
                          data=_json.dumps(body, ensure_ascii=False).encode("utf-8"),
                          headers={"Content-Type": "application/json; charset=utf-8"},
                          method="POST")
    try:
        with urlopen_loopback(req, timeout=300) as resp:
            result = _json.loads(resp.read().decode("utf-8"))
    except (_urlerr.HTTPError, _urlerr.URLError, OSError, ValueError) as exc:
        print(f"[ai-report-server] WARN 温启动注册失败（{base}）：{exc}", file=sys.stderr)
        return None
    if verbose:
        print(f"[ai-report-server] 已温注册 {report_id!r} → 活着的报告服务 "
              f"{base}/reports/{report_id}（datasetIds={result.get('datasetIds')}）")
    return report_id


def main() -> int:
    parser = argparse.ArgumentParser(description="Start the AI report server")
    parser.add_argument("--host", default="0.0.0.0", help="bind host (default 0.0.0.0 for LAN sharing)")
    parser.add_argument("--port", type=int, default=8765, help="port (default 8765)")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY,
                        help="registry file (default: ~/.ai-html-report/registry.json)")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR,
                        help="snapshot dir for dataset files (default: ~/.ai-html-report/data/)")
    parser.add_argument("--engine", choices=("auto", "sqlite", "duckdb"), default="sqlite",
                        help="query engine; sqlite is stable default, auto selects DuckDB for large registries when installed")
    parser.add_argument("--worker", action="store_true",
                        help="run as a durable queue worker instead of binding an HTTP server")
    parser.add_argument("--worker-kinds", default="monitor",
                        help="comma-separated durable job kinds for --worker (default: monitor)")
    parser.add_argument("--register-report", type=Path, action="append", default=None,
                        help="报告文件夹自注册（便携启动器冷启动传参；可重复）。"
                             "把 <folder>/build/manifest.json + build/data/ 注册进本机中央 registry")
    # 旧 --cors 旗标已删（第 2 轮收口批 P3）：从未被任何代码路径读取（响应头的
    # CORS 全靠各 handler 自己发），且安全 P1 批后管理端点明确禁发 ACAO:* ——留着
    # 只会让人误以为加个参数就能放开跨域。
    parser.add_argument("--quiet", action="store_true", help="suppress reload warnings")
    parser.add_argument("--pid-file", type=Path, default=None, help="write process id for safe stop")
    args = parser.parse_args()

    from server.store import create_engine
    registry = Registry(args.registry)
    registry.data_dir = args.data_dir.expanduser()
    # Create the user-state dirs before the SQLite engine opens report.db; the
    # default registry/data live outside the skill package and may not exist.
    registry.path.parent.mkdir(parents=True, exist_ok=True)
    registry.data_dir.mkdir(parents=True, exist_ok=True)

    if args.register_report:
        # --register-report 双模（2026-09-26 温启动重注册）：活着的 LRM server
        # 正服务**本端口**（= 启动器 $URL 背后的进程）→ 轻量注册（快照无条件
        # 覆盖 + HTTP 幂等 upsert）后直接退出，绝不拉第二个 server（统一
        # manager）。活 server 在别的端口时不走温路径——冷启动仍按原样注册进
        # 注册表并在本端口拉起服务（启动器打开的 URL 才不会漂）。
        live = _find_live_lrm_server(registry.path.parent, args.port)
        if live is not None and live.rstrip("/").endswith(f":{args.port}"):
            failed = False
            for _folder in args.register_report:
                if _register_report_warm(live, registry, _folder.expanduser(),
                                         verbose=not args.quiet) is None:
                    failed = True
            return 1 if failed else 0
        for _folder in args.register_report:
            _register_report_folder(registry, _folder.expanduser(), args.port,
                                    verbose=not args.quiet)

    use_duckdb, engine_policy = _select_engine_policy(registry, args.engine)
    db_path = registry.path.with_name("report.duckdb" if use_duckdb else "report.db")
    engine = create_engine(use_duckdb=use_duckdb, db_path=db_path)
    engine.engine_policy = engine_policy

    _reload_all(registry, engine, verbose=not args.quiet)

    if args.worker:
        from server import jobs
        jobs.configure(registry.path.parent)
        kinds = [item.strip() for item in str(args.worker_kinds).split(",") if item.strip()]
        print(f"[ai-report-worker] listening for kinds={kinds}")
        try:
            while True:
                worked = jobs.worker_once(
                    lambda job, progress: dispatch_queued_job(engine, registry, job, progress),
                    kinds=kinds, lease_seconds=60)
                if not worked:
                    time.sleep(0.5)
        except KeyboardInterrupt:
            print("\n[ai-report-worker] shutting down...")
        return 0

    if args.pid_file:
        args.pid_file.parent.mkdir(parents=True, exist_ok=True)
        args.pid_file.write_text(str(os.getpid()) + "\n", encoding="utf-8")

    template = TemplateProvider(registry, port=args.port)
    try:
        run_server(args.host, args.port, registry, engine, template)
    except OSError as e:
        print(f"[ai-report-server] failed to bind {args.host}:{args.port}: {e}", file=sys.stderr)
        return 2
    finally:
        if args.pid_file:
            try:
                args.pid_file.unlink(missing_ok=True)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
