"""月度刷新继承（增量 upsert）：rebuild 时把上一份 manifest 的 dataset 级策展继承进本次构建。

背景：持久化以报告为原子，语义以数据集为原子——月度刷新（重跑 report_build）原本会
丢掉用户在编辑器里做的全部数据策展（字段别名/值别名/隐藏列/字段说明/维度值分组/
派生声明）。本模块在 compile_spec 早期按列名继承，三条硬规则：

1. **请求显式声明 > 继承**：本轮 request 写了的键绝不覆盖（merge 只补缺失键）；
2. **按列名匹配**：只继承「新表里列仍存在」的键；列被删/改名的策展丢弃并报告，
   绝不带病进物化器（materialize/validate_measures 对坏引用是抛错不是跳过）；
3. **只碰继承来的键**：请求层自己声明的坏引用仍走原有「可行动报错」，不在这里被
   静默吞掉——prune 只处理 merge 标记为 inherited 的键。

继承源（`pick_prev_manifest`）：**中央注册表**
``$LRM_STATE_HOME/~/.ai-html-report/registry.json[reportId].manifest``（统一
manager 后在线保存/覆盖落这里）与 ``workdir/registry.json``（统一 manager 前
的每报告布局，升级窗口期仍在）按 reportId 精确命中、updatedAt 择新，回退
``workdir/manifest.json``（上次构建侧车）。
Guard: tests/test_curation_inherit.py + tests/test_eighth_review_py.py
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# dataset 级策展键。**显示层 4 键**不进 spec（validate_request 的 dataset 字段
# 白名单不认它们，normalize_request 内部会再跑一次 validate_request）——由
# :func:`apply_display` 在 manifest 组装后注入；**可物化 2 键**必须早期注入 spec
# （compile_spec 的物化器在读 spec，且这两键本就是请求白名单字段）。
DISPLAY_DS_KEYS = ("columnAliases", "valueAliases", "hiddenCols", "fieldNotes")
MATERIALIZE_DS_KEYS = ("dimGroups", "derived")
CURATED_DS_KEYS = DISPLAY_DS_KEYS + MATERIALIZE_DS_KEYS


def _central_registry_path() -> Path:
    """机器级中央注册表（server/registry.py、servers_index.py、report_build
    ._state_home 同一落点/开关；本模块被 report_build 导入，不能反向 import）。"""
    home = os.environ.get("LRM_STATE_HOME") or (Path.home() / ".ai-html-report")
    return Path(home) / "registry.json"


def _load_registry_map(path: Path) -> dict | None:
    try:
        reg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return reg if isinstance(reg, dict) and reg else None


def _manifest_of_entry(entry: Any) -> dict | None:
    m = (entry or {}).get("manifest") if isinstance(entry, dict) else None
    return m if isinstance(m, dict) and m.get("datasets") else None


def pick_prev_manifest(workdir: Path, report_id: str | None = None) -> dict | None:
    """挑最新的上一份 manifest：registry 在线条目 > 构建侧车 manifest.json。

    选取规则（与 tests/test_curation_inherit.py::test_pick_prev_manifest 锁定的
    行为逐字一致）：reportId **精确命中**优先；未提供**或未命中**时用 updated-
    时间最新的条目兜底（兜底是有意的：重建换 reportId / 换标题的场景也要能
    继承策展）。读不动一律返回 None（继承是增益，绝不让刷新构建失败）。

    统一 manager（2026-09-20）：在线保存落**机器级中央注册表**，报告 build/
    里不再有 registry.json——只读 workdir 会把「在线保存的策展」静默丢回上一
    次构建的侧车。所以：同 reportId 的中央条目精确命中也参与，与 workdir 命中
    条目比 updatedAt 取新；中央注册表**不做**「最新条目」兜底（它装着全机所有
    报告，兜底会把无关报告的策展并进本次构建）。
    """
    workdir = Path(workdir)
    best: tuple[str, dict] | None = None   # (updatedAt 文本, manifest)

    def consider(entry: Any) -> None:
        nonlocal best
        m = _manifest_of_entry(entry)
        if m is None:
            return
        key = str((entry or {}).get("updatedAt") or "")
        if best is None or key > best[0]:
            best = (key, m)

    reg_path = workdir / "registry.json"
    reg = _load_registry_map(reg_path)
    if reg:
        entry = reg.get(report_id) if report_id else None
        if isinstance(entry, dict):
            # 精确命中：与中央注册表同 id 条目（若有）比 updatedAt 后返回。
            consider(entry)
        else:
            # 无 reportId（或没命中）：挑 updatedAt 最新的条目。key 归一成
            # str——手编注册表可能缺 updatedAt（""）而机器写的是 float 时间戳，
            # 混比会 TypeError 并被上层吞成「本轮不继承」（继承静默失效）。
            # 只在 workdir 注册表内部兜底（继承源局部性：这份注册表曾属于
            # 本报告），绝不跨到中央注册表。
            dated = [(str(e.get("updatedAt") or ""), e) for e in reg.values()
                     if isinstance(e, dict) and isinstance(e.get("manifest"), dict)]
            return max(dated, default=None, key=lambda x: x[0])[1] if dated else None
    if report_id:
        central = _load_registry_map(_central_registry_path())
        if central:
            consider(central.get(report_id))
    if best is not None:
        return best[1]
    side = workdir / "manifest.json"
    if side.exists():
        try:
            m = json.loads(side.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if isinstance(m, dict) and m.get("datasets"):
            return m
    return None


def _col_names(columns: Any) -> list[str]:
    """schema 列（server 模式 [{name,type}]）或普通列名数组 → 名字列表。"""
    out = []
    for c in columns or []:
        if isinstance(c, dict):
            n = c.get("name")
            if n:
                out.append(str(n))
        elif c:
            out.append(str(c))
    return out


def _strip_report_prefix(ds_id: str, prev_report_id: str | None) -> str:
    """server manifest 的数据集 id 带 ``{reportId}__`` 前缀（h_register 命名空间），
    对齐上一份报告自己的 reportId 剥掉，才能和 request 里的裸 id 对上。"""
    if prev_report_id and isinstance(ds_id, str) and ds_id.startswith(prev_report_id + "__"):
        return ds_id[len(prev_report_id) + 2:]
    return ds_id


def merge(spec: dict, prev_manifest: dict) -> tuple[dict, list[str]]:
    """把上一份 manifest 的 dataset 级策展合并进 spec（缺失键才补）。"""
    spec, notes, _, _ = merge_with_map(spec, prev_manifest)
    return spec, notes


def merge_with_map(spec: dict, prev_manifest: dict) -> tuple[dict, list[str], dict[str, set[str]], dict]:
    """拆两路继承：可物化键进 spec.datasets（缺失才补），显示层键暂存待注入。

    返回 (spec, notes, injected, display_pending)：
    - ``injected[spec_ds_id]`` = 注进 spec 的键集合（prune 只裁这些）；
    - ``display_pending[spec_ds_id]`` = {键: 值}（apply_display 按 manifest 列
      裁剪后注入）。"""
    injected: dict[str, set[str]] = {}
    display: dict[str, dict] = {}
    prev_ds = [d for d in (prev_manifest.get("datasets") or []) if isinstance(d, dict)]
    spec_ds = [d for d in (spec.get("datasets") or []) if isinstance(d, dict)]
    notes: list[str] = []
    if not prev_ds or not spec_ds:
        return spec, notes, injected, display
    prev_report_id = prev_manifest.get("reportId")
    prev_by_raw: dict[str, dict] = {}
    for d in prev_ds:
        prev_by_raw.setdefault(_strip_report_prefix(str(d.get("id")), prev_report_id), d)
    single = len(spec_ds) == 1 and len(prev_ds) == 1
    for d in spec_ds:
        prev = prev_by_raw.get(str(d.get("id")))
        if prev is None and single:
            prev = prev_ds[0]
        if prev is None:
            continue
        got, dropped = [], []
        for key in CURATED_DS_KEYS:
            incoming = prev.get(key)
            if incoming is None or incoming == "" or incoming == []:
                continue
            if key in MATERIALIZE_DS_KEYS:
                if len(spec_ds) > 1:
                    dropped.append(key)
                    continue
                if d.get(key) is None or d.get(key) == "" or d.get(key) == []:
                    d[key] = incoming
                    got.append(key)
            else:
                if d.get(key) is None or d.get(key) == "" or d.get(key) == []:
                    display.setdefault(str(d.get("id")), {})[key] = incoming
                    got.append(key)
        if got:
            injected[str(d.get("id"))] = set(got)
            notes.append(f"dataset {d.get('id')}: 继承 " + "、".join(got))
        if dropped:
            notes.append(f"dataset {d.get('id')}: {('、'.join(dropped))} 仅单数据集支持，未继承")
    return spec, notes, injected, display


def _prune_pair_map(m: Any, cols: set[str]) -> tuple[dict, list[str]]:
    """columnAliases/fieldNotes 形状（{列: 值}）：列没了整键丢弃。"""
    if not isinstance(m, dict):
        return {}, []
    kept = {c: v for c, v in m.items() if str(c) in cols}
    return kept, [str(c) for c in m if str(c) not in cols]


def _prune_value_aliases(m: Any, cols: set[str]) -> tuple[dict, list[str]]:
    if not isinstance(m, dict):
        return {}, []
    kept, dropped = {}, []
    for col, mapping in m.items():
        if str(col) in cols and isinstance(mapping, dict) and mapping:
            kept[col] = mapping
        else:
            dropped.append(str(col))
    return kept, dropped


def _prune_hidden(cols_list: Any, cols: set[str]) -> tuple[list, list[str]]:
    if not isinstance(cols_list, list):
        return [], []
    kept = [c for c in cols_list if str(c) in cols]
    return kept, [str(c) for c in cols_list if str(c) not in cols]


def _prune_dim_groups(decls: Any, cols: set[str]) -> tuple[list, list[str]]:
    """维度值分组：source 列没了、组名与真实列冲突、组名重复 → 丢。"""
    if not isinstance(decls, list):
        return [], []
    kept, dropped, names = [], [], set()
    for g in decls:
        name, source = str((g or {}).get("name") or ""), (g or {}).get("source")
        if source not in cols:
            dropped.append(f"{name}(源列 {source} 不存在)")
        elif name in cols:
            dropped.append(f"{name}(与真实列重名)")
        elif name in names:
            dropped.append(f"{name}(重复)")
        else:
            names.add(name)
            kept.append(g)
    return kept, dropped


def _prune_row_columns(defs: Any, cols: set[str]) -> tuple[list, list[str]]:
    """行级计算列：按序累积 seen（只许引用写在前面的计算列），name 冲突/引用缺失 → 丢。"""
    if not isinstance(defs, list):
        return [], []
    kept, dropped, seen = [], [], set(cols)
    for d in defs:
        d = d or {}
        name, op = str(d.get("name") or ""), d.get("op")
        refs = []
        for side in ("left", "right"):
            ref = d.get(side)
            if op == "datediff" and ref == "__today__":
                continue
            if isinstance(ref, (int, float)) and not isinstance(ref, bool):
                continue
            refs.append(str(ref))
        bad = [r for r in refs if r not in seen]
        if not name or name in seen:
            dropped.append(f"{name or '?'}(与既有列冲突)")
        elif bad:
            dropped.append(f"{name}(引用不存在: {'、'.join(bad)})")
        else:
            kept.append(d)
            seen.add(name)
    return kept, dropped


def _prune_measures(defs: Any, cols: set[str], time_cols: set[str]) -> tuple[list, list[str]]:
    """聚合级派生指标：按**拓扑序**逐条试 validate_measures，坏一条丢一条。

    试错必须沿 topo_order 走：validate_measures 只认 trial 列表里出现过的名字，
    一条指标引用了「排在它后面」的合法指标（声明顺序 ≠ 拓扑序，DAG 允许）时，
    按声明序 trial 会把好指标误判「引用未知指标」而整条丢掉。topo_order 本身对
    未知引用/环会抛错——那种坏集合回落声明序，由 validate 逐条判掉。
    """
    if not isinstance(defs, list) or not defs:
        return (defs if isinstance(defs, list) else []), []
    import derived_model
    by_name: dict[str, dict] = {}
    for m in defs:
        if isinstance(m, dict) and m.get("name"):
            by_name.setdefault(str(m["name"]), m)
    try:
        ordered = [by_name[n] for n in derived_model.topo_order(defs) if n in by_name]
    except Exception:
        ordered = [m for m in defs if isinstance(m, dict) and m.get("name")]
    kept, dropped = [], []
    for m in ordered:
        trial = kept + [m]
        try:
            derived_model.validate_measures(trial, sorted(cols), sorted(time_cols), where="inherit")
        except Exception:
            dropped.append(str(m.get("id") or m.get("name") or "?"))
            continue
        kept = trial
    return kept, dropped


def _spec_ds_for(plan_d: dict, spec_ds: list) -> dict | None:
    """plan 条目 → spec 数据集：raw id / 精确 id / ``{reportId}__{id}`` 后缀 /
    单↔单兜底（serve_report 把 plan id 命名空间化而 spec 仍是裸 id）。"""
    want_raw = str(plan_d.get("raw") or plan_d.get("id"))
    pid = str(plan_d.get("id"))
    for sd in spec_ds:
        sid = str(sd.get("id"))
        if sid == want_raw or pid == sid or pid.endswith("__" + sid):
            return sd
    if len(spec_ds) == 1:
        return spec_ds[0]
    return None


def prune(spec: dict, tables_by_id: dict, datasets_plan: list,
          inherited: dict[str, set[str]] | None = None) -> list[str]:
    """表装载后按真实列裁剪**继承来的**策展（请求显式声明不碰，坏引用照旧抛错）。

    ``inherited[spec_ds_id] = merge 阶段实际注入的键集合``；返回丢弃报告行。
    """
    notes: list[str] = []
    spec_ds = [d for d in (spec.get("datasets") or []) if isinstance(d, dict)]
    for d in datasets_plan or []:
        raw = d.get("raw") or d.get("id")
        table = tables_by_id.get(raw)
        if not isinstance(table, dict):
            continue
        sd = _spec_ds_for(d, spec_ds)
        keys = (inherited or {}).get(str((sd or {}).get("id")))
        if sd is None or not keys:
            continue
        cols = set(_col_names(table.get("columns")))
        # 时间列宇宙：顶层 + 数据集级 columnRoles 的 time 角色（窗口指标的
        # timeField 校验需要；传空会把继承的窗口指标全部误裁）。
        roles = dict(spec.get("columnRoles") or {})
        roles.update(sd.get("columnRoles") or {})
        time_cols = {c for c, r in roles.items() if r == "time" and c in cols}
        if "columnAliases" in keys:
            kept, drop = _prune_pair_map(sd.get("columnAliases"), cols)
            if drop:
                sd["columnAliases"] = kept
                notes.append(f"dataset {raw} 字段别名丢弃（列已不存在）: {'、'.join(drop)}")
        if "fieldNotes" in keys:
            kept, drop = _prune_pair_map(sd.get("fieldNotes"), cols)
            if drop:
                sd["fieldNotes"] = kept
                notes.append(f"dataset {raw} 字段说明丢弃（列已不存在）: {'、'.join(drop)}")
        if "valueAliases" in keys:
            kept, drop = _prune_value_aliases(sd.get("valueAliases"), cols)
            if drop:
                sd["valueAliases"] = kept
                notes.append(f"dataset {raw} 值别名丢弃（列已不存在）: {'、'.join(drop)}")
        if "hiddenCols" in keys:
            kept, drop = _prune_hidden(sd.get("hiddenCols"), cols)
            if drop:
                sd["hiddenCols"] = kept
                notes.append(f"dataset {raw} 隐藏列丢弃（列已不存在）: {'、'.join(drop)}")
        if "dimGroups" in keys:
            kept, drop = _prune_dim_groups(sd.get("dimGroups"), cols)
            if drop:
                sd["dimGroups"] = kept
                notes.append(f"dataset {raw} 维度值分组丢弃: {'；'.join(drop)}")
        if "derived" in keys and isinstance(sd.get("derived"), dict):
            der = sd["derived"]
            kept_c, drop_c = _prune_row_columns(der.get("columns"), cols)
            # 指标宇宙 = 真实列 ∪ 保留的计算列（窗口/公式的 input 常引用计算列，
            # 而物化发生在本裁剪之后，真实列里还没有它们）。
            kept_m, drop_m = _prune_measures(der.get("measures"),
                                             cols | {str(c.get("name") or "") for c in kept_c},
                                             time_cols)
            if drop_c:
                der["columns"] = kept_c
                notes.append(f"dataset {raw} 计算列丢弃: {'；'.join(drop_c)}")
            if drop_m:
                der["measures"] = kept_m
                notes.append(f"dataset {raw} 派生指标丢弃: {'、'.join(drop_m)}")
            if not der.get("columns") and not der.get("measures"):
                sd.pop("derived", None)
    return notes


def apply_display(manifest: dict, display: dict) -> list[str]:
    """manifest 组装后注入显示层策展（按 manifest 数据集自己的列裁剪）。

    ``display[spec_ds_id] = {键: 值}``（merge_with_map 暂存）；manifest 数据集 id
    可能已命名空间化（server/mixed 远端），用与 prune 同一套匹配规则对回。"""
    notes: list[str] = []
    if not display:
        return notes
    ds_list = [d for d in (manifest.get("datasets") or []) if isinstance(d, dict)]
    for mds in ds_list:
        mid = str(mds.get("id"))
        pending = None
        for sid, bucket in display.items():
            if sid == mid or mid.endswith("__" + sid):
                pending = bucket
                break
        if pending is None and len(ds_list) == 1 and len(display) == 1:
            pending = next(iter(display.values()))
        if not pending:
            continue
        cols = set(_col_names(mds.get("columns")))
        if "columnAliases" in pending:
            kept, drop = _prune_pair_map(pending.get("columnAliases"), cols)
            if kept:
                mds["columnAliases"] = kept
            if drop:
                notes.append(f"dataset {mid} 字段别名丢弃（列已不存在）: {'、'.join(drop)}")
        if "fieldNotes" in pending:
            kept, drop = _prune_pair_map(pending.get("fieldNotes"), cols)
            if kept:
                mds["fieldNotes"] = kept
            if drop:
                notes.append(f"dataset {mid} 字段说明丢弃（列已不存在）: {'、'.join(drop)}")
        if "valueAliases" in pending:
            kept, drop = _prune_value_aliases(pending.get("valueAliases"), cols)
            if kept:
                mds["valueAliases"] = kept
            if drop:
                notes.append(f"dataset {mid} 值别名丢弃（列已不存在）: {'、'.join(drop)}")
        if "hiddenCols" in pending:
            kept, drop = _prune_hidden(pending.get("hiddenCols"), cols)
            if kept:
                mds["hiddenCols"] = kept
            if drop:
                notes.append(f"dataset {mid} 隐藏列丢弃（列已不存在）: {'、'.join(drop)}")
    return notes
