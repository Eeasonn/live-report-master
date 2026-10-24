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
from server._platform import urlopen_loopback                   # noqa: E402
from server.registry import Registry, DEFAULT_DATA_DIR, DEFAULT_REGISTRY   # noqa: E402
from server.store import (                                     # noqa: E402
    ENGINE_CACHE_VERSION,
    engine_load_from_snapshot,
    engine_supports_table_cache,
    get_engine,
    read_snapshot,
    snapshot_fingerprint,
    table_cache_stem,
)
from server.template import TemplateProvider                   # noqa: E402

# Persistent engine cache (NP-01): the cache key is
# (dataset id, snapshot sha256, declaration hash, TABLE_CACHE_TAG).
# Bump LOADER_VERSION whenever scripts/table_input.py parsing changes shape or
# values (a cached table was produced by the OLD loader and must invalidate).
TABLE_CACHE_LOADER_VERSION = "1"
TABLE_CACHE_TAG = f"engine{ENGINE_CACHE_VERSION}+loader{TABLE_CACHE_LOADER_VERSION}"


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


def _table_decl_hash(registry: Registry, ds_id: str) -> str:
    """Content hash of every registry declaration that shapes a table's content.

    The engine content of a table is NOT just its snapshot: the derived-column
    union, the 维度值分组 union and the declared index columns are replayed on
    top of the raw snapshot (aggregate-level ``derived.measures`` 不在此列——
    它们从不落表).  All unions are computed with the SAME helpers
    ``derived_columns.replay_all`` uses, so any declaration change flips this
    hash and the cached table is rebuilt instead of adopted.
    """
    import hashlib as _hashlib
    import json as _json

    from server.store import dim_group_table_defs, table_defs
    entries = registry.raw_entries()
    idx_cols: list[str] = []
    for e2 in entries:
        if not isinstance(e2, dict):
            continue
        for d2 in (e2.get("datasets") or []):
            if isinstance(d2, dict) and d2.get("id") == ds_id:
                for c in (d2.get("indexColumns") or []):
                    if c not in idx_cols:
                        idx_cols.append(c)
    payload = _json.dumps(
        {"derived": table_defs(entries, ds_id),
         "dimGroups": dim_group_table_defs(entries, ds_id),
         "indexes": idx_cols},
        ensure_ascii=False, default=str)
    return _hashlib.sha256(payload.encode("utf-8")).hexdigest()


class _TableCache:
    """Per-boot coordinator for the persistent engine cache (NP-01).

    Cache files live in ``<state-home>/db/<datasetId>.sqlite`` and are written
    ONLY by ``store.SQLiteEngine.export_table_cache`` (temp file + os.replace).
    ``LRM_NO_DB_CACHE=1`` (or true/yes) disables reads AND writes so the boot
    behaves exactly like the pre-NP-01 reload path.  DuckDB engines never
    enable it (``engine_supports_table_cache`` is False there).
    """

    def __init__(self, registry: Registry, engine, verbose: bool = True):
        self.registry = registry
        self.engine = engine
        self.verbose = verbose
        self.enabled = (
            os.environ.get("LRM_NO_DB_CACHE", "").strip().lower()
            not in ("1", "true", "yes")
            and engine_supports_table_cache(engine))
        self.db_dir = registry.path.parent / "db"
        self.adopted: dict[str, str] = {}   # ds_id -> snapshot fingerprint (fresh)
        self.loaded: dict[str, str] = {}    # ds_id -> snapshot fingerprint (needs export)
        self._fps: dict[str, str] = {}      # resolved source path -> fingerprint
        self._expected: set[str] = set()    # stems allowed to survive this boot
        if self.enabled:
            try:
                self.db_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                self.enabled = False

    def _say(self, msg: str, to_stderr: bool = False) -> None:
        if self.verbose:
            print(msg, file=sys.stderr if to_stderr else None)

    def fingerprint_for(self, p: Path) -> str:
        """sha256 of the snapshot file (memoized per resolved path)."""
        key = str(p)
        fp = self._fps.get(key)
        if fp is None:
            fp = snapshot_fingerprint(p)
            self._fps[key] = fp
        return fp

    def try_adopt(self, ds_id: str, fingerprint: str) -> bool:
        """Restore the table from cache when snapshot+declarations are unchanged.

        Returns True when the engine already holds the current table content
        (adopted now, or earlier in this same boot for a shared dataset id) so
        the caller skips ``load(p)`` + ``engine.load_table`` entirely.  Any
        cache problem returns False and the caller reloads (fail-closed).
        """
        if not self.enabled:
            return False
        stem = table_cache_stem(ds_id)
        self._expected.add(stem)
        if self.adopted.get(ds_id) == fingerprint:
            return True  # shared dataset id already adopted with this content
        info = self.engine.adopt_table_cache(
            ds_id, self.db_dir / f"{stem}.sqlite", fingerprint,
            _table_decl_hash(self.registry, ds_id), TABLE_CACHE_TAG)
        if info is None:
            return False
        self.adopted[ds_id] = fingerprint
        self._say(f"[ai-report-server] cache-hit {ds_id!r} ({info.row_count} rows, "
                  f"{len(info.columns)} cols) from db/{stem}.sqlite")
        return True

    def mark_loaded(self, ds_id: str, fingerprint: str) -> None:
        """A table was reloaded from its snapshot: its cache needs re-export."""
        self.loaded[ds_id] = fingerprint
        self.adopted.pop(ds_id, None)

    def flush(self) -> None:
        """Export reloaded tables to cache files; prune orphans; drop tmp junk.

        Runs AFTER the derived-column and relation replays so a cache file
        always holds the post-replay table content (a later adoption can then
        skip the replays for that table — they are no-ops on an up-to-date
        table because the declaration hash is part of the cache key).
        """
        if not self.enabled:
            return
        for ds_id, fingerprint in sorted(self.loaded.items()):
            stem = table_cache_stem(ds_id)
            ok = self.engine.export_table_cache(
                ds_id, self.db_dir / f"{stem}.sqlite", fingerprint,
                _table_decl_hash(self.registry, ds_id), TABLE_CACHE_TAG)
            if ok:
                self._say(f"[ai-report-server] cache-write {ds_id!r} -> db/{stem}.sqlite")
            else:
                self._say(f"[ai-report-server] WARN cache-write {ds_id!r} failed "
                          f"（引擎表不受影响，下次启动将重新装载）", to_stderr=True)
        # Orphan hygiene: a cache file whose dataset has no existing source this
        # boot can never be validated again — remove it.  Leftover tmp files
        # from a crashed export go the same way.
        try:
            entries = list(self.db_dir.glob("*.sqlite"))
        except OSError:
            entries = []
        for f in entries:
            if f.name[: -len(".sqlite")] not in self._expected:
                try:
                    f.unlink()
                    self._say(f"[ai-report-server] cache-prune {f.name}（数据集已无快照）")
                except OSError:
                    pass
        for f in list(self.db_dir.glob("*.sqlite.tmp-*")):
            try:
                f.unlink()
            except OSError:
                pass


def _reload_all(registry: Registry, engine, verbose: bool = True) -> None:
    from server.store import replay_all as replay_columns
    from server.registry import is_derived, replay_entry
    # Persistent engine cache (NP-01): snapshot-content-hash keyed table cache.
    # Disabled (or DuckDB engines / LRM_NO_DB_CACHE=1) every step below is the
    # untouched pre-NP-01 path.
    cache = _TableCache(registry, engine, verbose=verbose)
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
            # 哈希先于装载：命中即免掉 JSON 解析 + DROP+CREATE（冷启动主收益）。
            # 快照在 exists() 与读盘之间被删属边界竞态：fail-closed 降级为普通重载。
            try:
                fp = cache.fingerprint_for(p) if cache.enabled else None
            except OSError:
                fp = None
            if fp is not None and cache.try_adopt(d["id"], fp):
                continue
            try:
                # XP-01（2026-10-24）：装载统一走 engine_load_from_snapshot——
                # .json 快照走 canonical 装载器（历史行为逐位一致），.sqlite 快照
                # 走 ATTACH+INSERT SELECT（跳过整表 JSON 解析），csv/xlsx 构建期
                # 源拷贝照旧；DuckDB 引擎自动落回旧路径。
                # 顺序契约（勿改）：先解析后落表——快照解析失败（如 0 行 JSON）
                # 时引擎里的旧表原样保留（与历史行为逐位一致）；DROP+CREATE 由
                # load_table/_load_sqlite_snapshot 内部完成。
                # 索引列声明随注册表 meta 走（h_register 持久化）：不透传 = 索引
                # 悄悄消失（与 app._reload_dataset 同一条契约，别只修一处）。
                info = engine_load_from_snapshot(engine, d["id"], p,
                                                 index_columns=list(d.get("indexColumns") or []))
                if fp is not None:
                    cache.mark_loaded(d["id"], fp)
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
    # Cache export LAST: the persisted table must be the post-replay content.
    cache.flush()


def _find_shipped_snapshot(data_dir: Path, report_id: str, ds_id: str) -> Path | None:
    """在随包的 ``build/data/`` 里找数据集快照（XP-01：.json/.sqlite 双后缀）。

    优先级 = 数据的新鲜度：**运行时替换 > 运行时追加 > 构建期 canonical**。
    运行时「替换数据」把权威真值写进 ``.replaced.{json,sqlite}`` 并把注册表
    source 改指它（server/app.py ``_process_replace_table``），构建期
    ``table-primary`` 旧快照并不删除、两代会同时随包（``_ship_portable_data``
    按 ``{report_id}__*`` 全量拷）——组序倒置会让便携重注册恒选中旧快照：注册表
    rowCount 声称新、聚合全是旧数（静默错数，2026-09-26 甄别）。

    随包命名有两代：mixed 注册快照 = ``{report_id}__{ds_id}__*``（ds_id 已是
    final 名）；运行时替换/追加与单表注册 = ``{report_id}__{raw}*``——两种都
    试。兜底 glob 必须带边界（``[._-]``／``__``）：裸 ``{raw}*`` 会让 ``sales``
    命中 ``sales2__table-primary.json``，兄弟数据集数据串台且无告警。

    同一候选位两种格式并存（.replaced.json + .replaced.sqlite）= 格式换轨期的
    过渡态：**以较新 mtime 为权威**并打印一次 WARN（两份内容理论上同源，但
    mtime 新的那份才是最后一次写盘的）。
    """
    if not data_dir.exists():
        return None
    raw = ds_id
    prefix = f"{report_id}__"
    if ds_id.startswith(prefix):
        raw = ds_id[len(prefix):]

    def _pick(stem: str) -> Path | None:
        hits = [p for suf in (".json", ".sqlite")
                if (p := data_dir / f"{stem}{suf}").exists()]
        if not hits:
            return None
        if len(hits) > 1:
            _warn_dual_format(hits)
        return max(hits, key=lambda p: p.stat().st_mtime)

    prefs = [
        # 运行时权威（app.py 写进中央 state home，随包两代并存时它更新）
        [f"{report_id}__{ds_id}.replaced", f"{report_id}__{raw}.replaced"],
        [f"{report_id}__{ds_id}.added", f"{report_id}__{raw}.added"],
        # 构建期 canonical（mixed 注册 / 单表注册）
        [f"{report_id}__{ds_id}__table-primary", f"{report_id}__{raw}__table-primary"],
    ]
    for group in prefs:
        for stem in group:
            p = _pick(stem)
            if p is not None:
                return p
    # 兜底（表名非 table-primary 的历史包等）：按 ``__``/``.`` 边界自匹配，
    # 绝不让 ``sales`` 命中 ``sales2__…``（首字符必须是边界符）。
    for stem in (ds_id, raw):
        hits = sorted(data_dir.glob(f"{report_id}__{stem}[._-]*"))
        if hits:
            if len({p.suffix for p in hits if p.suffix in (".json", ".sqlite")}) > 1:
                _warn_dual_format(hits)
            return max(hits, key=lambda p: p.stat().st_mtime)
    return None


_DUAL_FORMAT_WARNED: set[tuple[str, str]] = set()


def _warn_dual_format(hits: list[Path]) -> None:
    """Same-stem dual-format coexistence → newer mtime wins, WARN once per set."""
    names = tuple(sorted(p.name for p in hits))
    if names in _DUAL_FORMAT_WARNED:
        return
    _DUAL_FORMAT_WARNED.add(names)
    newest = max(hits, key=lambda p: p.stat().st_mtime)
    print(f"[ai-report-server] WARN 同一数据集两种快照格式并存 "
          f"（{' / '.join(names)}）；以较新 mtime 为权威 → {newest.name}。"
          "可运行 run_server --prune 回收被取代的旧格式代次。",
          file=sys.stderr)


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

    from server.registry import wide_dataset_id

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
            from server.registry import derived_entry
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
            # XP-01：随包快照按后缀分派读回（.json 走 json 解析、.sqlite 走
            # store.read_snapshot 流式读），旧 JSON 包照读零漂移。
            if item["snapshot"].suffix.lower() == ".sqlite":
                _cols, _rows_iter = read_snapshot(item["snapshot"])
                table = {"columns": list(_cols), "rows": list(_rows_iter)}
            else:
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


# ---------------------- XP-01 · --prune 快照保留策略（2026-10-24） -----------------
#
# state home 的 data/（数据快照）与 db/（NP-01 表缓存）没有任何治理会无限膨胀
# （真机 data/ 964MB + db/ 360MB，含大量历史测试报告与被取代代次）。prune 只碰
# 这两个目录；registry.json / servers.json / jobs.sqlite3 / *.secret /
# copilot-* / server.pid 等一律不进回收范围，且 apply 阶段逐文件再验一次
# 「父目录必须是本次扫描的 data/ 或 db/」——即使计划列表被塞进白名单外的路径
# 也删不掉（白名单闸，tests/test_prune.py 源级+行为级锁定）。
#
# 三类可回收：
#   ① orphan-snapshot      data/ 里带 ``{reportId}__`` 前缀、但不属于任何注册
#                          表条目（数据集记账/manifest/source 全都接不上）的文件；
#   ② superseded-generation 同一数据集的各代快照（.replaced/.added/构建期
#                          canonical/pre-delete）中，注册表 source 当前引用的那份
#                          与最新 mtime 的那两份保留，其余可回收；
#   ③ orphan-cache         db/*.sqlite 的 stem 不对应任何注册数据集；
#      stale-cache         或 stem 合法但 ``_ai_cache_meta.fingerprint`` ≠ 当前
#                          快照指纹（下次 boot 也会重建，离线删掉即回收）。
#
# 安全闸：state home 的 server.pid 对应进程活着 → --prune-apply 拒绝执行
#（提示先停服务）；--prune-force 显式放行。dry-run（--prune）恒可运行。
PRUNE_SCOPE_DIRS = ("data", "db")


def _snapshot_files_for_dir(data_dir: Path, report_id: str, ds_id: str) -> list[Path]:
    """app._snapshot_files_for 的本地别名（单一实现，不起第二套匹配规则）。"""
    from server.app import _snapshot_files_for
    return _snapshot_files_for(data_dir, report_id, ds_id)


def _read_cache_fingerprint(cache_file: Path) -> str | None:
    """Read ``_ai_cache_meta.fingerprint`` from an NP-01 cache file (None=unreadable)."""
    import sqlite3 as _sqlite3
    try:
        conn = _sqlite3.connect(cache_file.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT key, value FROM _ai_cache_meta").fetchall()
        finally:
            conn.close()
    except (OSError, _sqlite3.Error, ValueError):
        return None
    for key, value in rows:
        if str(key) == "fingerprint":
            return str(value)
    return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _server_pid_from(state_home: Path) -> int | None:
    """state home 根的 server.pid 里记录的 pid；文件缺失/内容非法 → None。

    注意这只认「启动器/构建器写下的那个常驻服务」的 pid 文件（report_build
    `_ensure_server` 与便携启动器都以 ``<state>/server.pid`` 为约定）。
    """
    pid_file = state_home / "server.pid"
    if not pid_file.exists():
        return None
    try:
        return int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _build_prune_plan(state_home: Path, registry, data_dir: Path,
                      db_dir: Path) -> dict:
    """Classify reclaimable files. Pure scan — touches NOTHING on disk."""
    from server.store import table_cache_stem
    entries = [e for e in registry.raw_entries() if isinstance(e, dict)]

    referenced_names: set[str] = set()
    group_sources: dict[tuple[str, str], set[str]] = {}
    groups: dict[tuple[str, str], list[Path]] = {}
    ds_ids: list[str] = []
    for e in entries:
        rid = str(e.get("reportId") or "")
        for d in (e.get("datasets") or []):
            if not isinstance(d, dict) or not d.get("id"):
                continue
            ds_id = str(d["id"])
            if ds_id not in ds_ids:
                ds_ids.append(ds_id)
            key = (rid, ds_id)
            groups.setdefault(key, [])
            src = str(d.get("source") or "")
            if src:
                name = Path(src).name
                referenced_names.add(name)
                group_sources.setdefault(key, set()).add(name)
            for p in _snapshot_files_for_dir(data_dir, rid, ds_id):
                if p not in groups[key]:
                    groups[key].append(p)

    plan: list[dict] = []

    def _add(path: Path, category: str) -> None:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        plan.append({"path": str(path), "category": category, "bytes": size})

    # ② 被取代代次（先分类组内文件，剩下的才轮到 ① 孤儿判定）
    kept_names: set[str] = set()
    for key, files in sorted(groups.items()):
        if not files:
            continue
        keep = set(group_sources.get(key) or set())
        newest = max(files, key=lambda p: (p.stat().st_mtime if p.exists() else 0))
        keep.add(newest.name)
        kept_names.update(p.name for p in files)
        for p in sorted(files):
            if p.name not in keep:
                _add(p, "superseded-generation")

    # ① 孤儿快照：带报告前缀但任何注册条目都接不上
    try:
        data_files = sorted(p for p in data_dir.iterdir() if p.is_file())
    except OSError:
        data_files = []
    for f in data_files:
        if f.name in kept_names or f.name in referenced_names:
            continue
        if "__" in f.name:
            _add(f, "orphan-snapshot")

    # ③ 孤儿/过期表缓存
    expected: dict[str, str] = {}   # cache stem -> ds_id
    for ds_id in ds_ids:
        expected[table_cache_stem(ds_id)] = ds_id
    ds_snapshot: dict[str, Path | None] = {}
    for (rid, ds_id), _files in groups.items():
        if ds_id in ds_snapshot:
            continue
        src = None
        for e in entries:
            hit = next((d for d in (e.get("datasets") or [])
                        if isinstance(d, dict) and str(d.get("id") or "") == ds_id
                        and d.get("source")), None)
            if hit is not None:
                p = Path(str(hit["source"]))
                src = p if p.is_absolute() else state_home / p
                break
        ds_snapshot[ds_id] = src
    try:
        cache_files = sorted(db_dir.glob("*.sqlite"))
    except OSError:
        cache_files = []
    fps: dict[str, str] = {}

    def _fp(p: Path) -> str:
        key = str(p)
        if key not in fps:
            fps[key] = snapshot_fingerprint(p)
        return fps[key]

    for f in cache_files:
        stem = f.name[:-len(".sqlite")]
        ds_id = expected.get(stem)
        if ds_id is None:
            _add(f, "orphan-cache")
            continue
        snap = ds_snapshot.get(ds_id)
        if snap is None or not snap.exists():
            _add(f, "orphan-cache")
            continue
        cached_fp = _read_cache_fingerprint(f)
        if cached_fp is None or cached_fp != _fp(snap):
            _add(f, "stale-cache")

    subtotals: dict[str, int] = {}
    for item in plan:
        subtotals[item["category"]] = subtotals.get(item["category"], 0) + item["bytes"]
    return {
        "stateHome": str(state_home),
        "mode": "dry-run",
        "entries": plan,
        "subtotals": subtotals,
        "totalBytes": sum(item["bytes"] for item in plan),
    }


def _inside_scope(path: Path, scope: list[Path]) -> bool:
    """白名单闸：路径必须直接位于本次扫描的 data/ 或 db/ 目录内。"""
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return any(resolved.parent == scope_dir for scope_dir in scope)


def _apply_prune(state_home: Path, plan: dict, data_dir: Path,
                 db_dir: Path) -> dict:
    """Delete EXACTLY the planned files, re-verifying the scope whitelist per path.

    逐文件 try/except 不中断；白名单外（含被塞进列表的 registry.json 等）一律
    skip 并记录原因，绝不扩大删除面。
    """
    scope = [data_dir.resolve(), db_dir.resolve()]
    deleted: list[dict] = []
    skipped: list[dict] = []
    for item in plan["entries"]:
        p = Path(item["path"])
        if not _inside_scope(p, scope):
            skipped.append({"path": item["path"],
                            "reason": "outside data//db scope（白名单外，拒绝删除）"})
            continue
        try:
            p.unlink()
            deleted.append(item)
        except OSError as exc:
            skipped.append({"path": item["path"], "reason": str(exc)})
    applied = dict(plan)
    applied["mode"] = "applied"
    applied["deleted"] = deleted
    applied["deletedBytes"] = sum(i["bytes"] for i in deleted)
    applied["skipped"] = skipped
    return applied


def _print_prune_plan(plan: dict, tail: dict | None = None) -> None:
    print(f"[ai-report-prune] state home: {plan['stateHome']}  mode: {plan['mode']}")
    if not plan["entries"]:
        print("[ai-report-prune] 没有可回收文件（data/ 与 db/ 已收敛）。")
    for item in plan["entries"]:
        print(f"{item['path']} | {item['category']} | {item['bytes']}")
    print("[ai-report-prune] 分类小计：")
    for cat, n in sorted(plan["subtotals"].items()):
        print(f"  {cat} | {n} bytes")
    print(f"[ai-report-prune] 总可回收：{plan['totalBytes']} bytes "
          f"({plan['totalBytes'] / 1048576:.2f} MB)")
    if tail:
        for item in tail.get("deleted") or []:
            print(f"[ai-report-prune] 已删除 {item['path']}（{item['bytes']} bytes）")
        for item in tail.get("skipped") or []:
            print(f"[ai-report-prune] 跳过 {item['path']}：{item['reason']}")
        print(f"[ai-report-prune] 实际删除 {len(tail.get('deleted') or [])} 个文件、"
              f"{tail.get('deletedBytes', 0)} bytes。")


def _run_prune(args, registry) -> int:
    import json as _json
    state_home = registry.path.parent
    data_dir = args.data_dir.expanduser()
    db_dir = state_home / "db"
    plan = _build_prune_plan(state_home, registry, data_dir, db_dir)
    tail: dict | None = None
    if args.prune_apply:
        pid = _server_pid_from(state_home)
        if pid is not None and _pid_alive(pid) and not args.prune_force:
            print(f"[ai-report-prune] 拒绝执行 --prune-apply：server.pid 记录的进程 "
                  f"（pid {pid}）仍在运行。请先在报告管理页关闭服务，或确认服务已停后重试；"
                  "确要边跑边删请显式加 --prune-force。", file=sys.stderr)
            return 2
        tail = _apply_prune(state_home, plan, data_dir, db_dir)
    _print_prune_plan(plan, tail)
    if args.prune_json:
        out = tail if tail is not None else dict(plan)
        out["serverPid"] = _server_pid_from(state_home)
        try:
            args.prune_json.expanduser().parent.mkdir(parents=True, exist_ok=True)
            args.prune_json.expanduser().write_text(
                _json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"[ai-report-prune] 结构化结果已写入 {args.prune_json}")
        except OSError as exc:
            print(f"[ai-report-prune] 结构化结果写盘失败：{exc}", file=sys.stderr)
            return 2
    return 0


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
    parser.add_argument("--prune", action="store_true",
                        help="快照保留策略演练（dry-run）：逐文件列出 data/ 与 db/ 可回收项，不删除任何东西")
    parser.add_argument("--prune-apply", action="store_true",
                        help="真删 dry-run 列表中的文件；server.pid 活着时拒绝执行（先停服务，或 --prune-force 放行）")
    parser.add_argument("--prune-json", type=Path, default=None,
                        help="把 prune 结构化结果（entries/subtotals/total/deleted/skipped）写到 PATH")
    parser.add_argument("--prune-force", action="store_true",
                        help="--prune-apply 的显式放行：server.pid 对应进程仍活着时也执行")
    args = parser.parse_args()

    from server.store import create_engine
    registry = Registry(args.registry)
    registry.data_dir = args.data_dir.expanduser()
    # Create the user-state dirs before the SQLite engine opens report.db; the
    # default registry/data live outside the skill package and may not exist.
    registry.path.parent.mkdir(parents=True, exist_ok=True)
    registry.data_dir.mkdir(parents=True, exist_ok=True)

    if args.prune or args.prune_apply:
        # 保留策略（XP-01）是离线维护命令：不拉引擎、不起 HTTP，扫完即退出。
        return _run_prune(args, registry)

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
