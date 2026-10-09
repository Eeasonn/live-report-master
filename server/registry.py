"""Multi-report registry with on-disk persistence.

The registry holds metadata for every registered report: its manifest,
which datasets it references, when it was last reloaded, and where the
original canonical table lives on disk.

The registry itself does NOT hold table data — that's the engine's job.
Reloading is a separate operation: re-read the disk table, push it into
the engine, and update the registry entry.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

# Defaults live OUTSIDE the skill package so running the server never bloats the
# skill (a large indexed dataset would otherwise be written into the skill dir,
# ballooning it to hundreds of MB). We use a per-user state dir that manually
# started servers, start_server.cmd, and report_build agree on. report_build
# server mode overrides these with workdir-local paths (--workdir/build), so
# shared servers keep their state with the build, not in the package.
# LRM_STATE_HOME（2026-09-20 统一 manager）：测试/CI 用它把机器级状态隔离到
# 临时目录——统一 manager 下所有构建共用一份中央注册表，套件跑真实 server
# 构建时绝不能写进用户真实状态。
_USER_STATE = Path(os.environ.get("LRM_STATE_HOME") or (Path.home() / ".ai-html-report"))
_DEFAULT_REGISTRY = _USER_STATE / "registry.json"
_DEFAULT_DATA_DIR = _USER_STATE / "data"

# Backward-compatible aliases kept for any external importers.
DEFAULT_REGISTRY = _USER_STATE / "registry.json"
DEFAULT_DATA_DIR = _USER_STATE / "data"
_SKILL = Path(__file__).resolve().parent.parent


class Registry:
    def __init__(self, path: Path | str = DEFAULT_REGISTRY):
        self._path = Path(path).expanduser()
        self._lock = threading.RLock()
        self._entries: dict[str, dict[str, Any]] = {}
        self._load()

    # -- persistence ------------------------------------------------------------

    def _load(self) -> None:
        if not self._path.exists():
            self._entries = {}
            return
        try:
            entries = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(entries, dict):
                raise ValueError("registry root must be a JSON object")
            self._entries = entries
        except (json.JSONDecodeError, OSError, ValueError):
            # Corrupt or unreadable: start fresh, but QUARANTINE the bad file
            # first（改名保留，不动内容）。此前只置空不保留——下一次任意
            # register 会用只含新条目的 dict 覆盖整份 registry.json，全机其余
            # 报告从统一 manager 静默消失且无从找回。坏文件在，人工可恢复。
            self._entries = {}
            try:
                stamp = time.strftime("%Y%m%d-%H%M%S")
                quarantine = self._path.with_name(f"{self._path.name}.corrupt-{stamp}")
                seq = 0
                while quarantine.exists():
                    seq += 1
                    quarantine = self._path.with_name(
                        f"{self._path.name}.corrupt-{stamp}-{seq}")
                os.replace(self._path, quarantine)
                print(f"[ai-report-server] WARN registry 损坏（或形状非法），坏文件已保留 → "
                      f"{quarantine.name}；本次以空注册表启动",
                      file=sys.stderr)
            except OSError:
                pass

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self._entries, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        os.replace(tmp, self._path)
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass

    # -- CRUD -------------------------------------------------------------------

    def register(self, report_id: str, entry: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            entry["reportId"] = report_id
            entry.setdefault("registeredAt", time.time())
            entry["updatedAt"] = time.time()
            self._entries[report_id] = entry
            self._save()
            return entry

    def get(self, report_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            entry = self._entries.get(report_id)
            return json.loads(json.dumps(entry)) if entry else None  # deep copy

    def unregister(self, report_id: str) -> bool:
        with self._lock:
            if report_id in self._entries:
                del self._entries[report_id]
                self._save()
                return True
            return False

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [json.loads(json.dumps(e)) for e in self._entries.values()]

    def raw_entries(self) -> list[dict[str, Any]]:
        """Read-only view of the entries WITHOUT the deep copy ``list()`` makes.

        List/health/manager paths only read a few fields per entry. ``list()``
        JSON round-trips whole entries — and a report whose runtime-added dataset
        carries rows puts tens of MB in ONE entry — which made ``/api/reports``
        take seconds and blew the supervisor's probe timeouts (report_build then
        thought the port belonged to another report and moved the report's port).
        Callers MUST treat the returned dicts as read-only.
        """
        with self._lock:
            return list(self._entries.values())

    def items_raw(self) -> list[tuple[str, dict[str, Any]]]:
        """``(report_id, entry)`` pairs without deep copies (same contract as
        ``raw_entries``: read-only dicts, caller never mutates).

        表清单/共享表判定需要**按 reportId** 找「还有谁声明了这个 dataset id」，
        ``raw_entries`` 只给值不给键——给键的迭代只能放这里，不许调用方靠
        ``entry["reportId"]`` 反推（运行时新增的条目可能还没被 register 写上）。
        """
        with self._lock:
            return list(self._entries.items())

    def summary(self) -> dict[str, int]:
        """Cheap counts for /api/health — never deep-copies the entries.

        ``list()`` JSON round-trips every entry; a report whose runtime-added
        dataset carries rows makes the registry tens of MB, and that cost pushed
        ``/api/health`` past the supervisor's short ping timeout — servers then
        looked DEAD to the 报告管理 index (which ping-prunes). Counts only need
        the dict keys, so this stays O(entries) with no serialization.
        """
        with self._lock:
            datasets: set[str] = set()
            for entry in self._entries.values():
                for d in (entry.get("datasets") or []):
                    if isinstance(d, dict) and d.get("id"):
                        datasets.add(str(d["id"]))
            return {"reportCount": len(self._entries), "datasetCount": len(datasets)}

    def reload_disk(self) -> None:
        """Re-read the registry file from disk (used by /admin/reload-registry)."""
        with self._lock:
            self._load()

    @property
    def path(self) -> Path:
        return self._path


def snapshot_data_file(source: Path, data_dir: Path | None = None, *, namespace: str | None = None) -> Path:
    """Copy the source file into the canonical data dir (avoids path drift).

    The registry records the snapshot path so server reload survives the
    original file being moved or deleted.
    """
    data_dir = (data_dir or DEFAULT_DATA_DIR).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)
    safe_namespace = re.sub(r'[^A-Za-z0-9_-]+', '_', namespace).strip('_') if namespace else ''
    # Slugification collapses distinct CJK namespaces onto one name; when it
    # actually changed the namespace, append a short stable hash so two
    # datasets never share one snapshot file (reload would swap their data).
    if safe_namespace and safe_namespace != namespace:
        import hashlib
        safe_namespace = (f"{safe_namespace}-"
                          f"{hashlib.sha1(namespace.encode('utf-8')).hexdigest()[:8]}")
    target_name = f"{safe_namespace}__{source.name}" if safe_namespace else source.name
    target = data_dir / target_name
    if source.resolve() != target.resolve():
        # avoid recursive copy
        import shutil
        shutil.copy2(source, target)
    return target
