"""决策清单（build/decisions.md）：把「系统默认」与「作者声明」分开记账。

Flint `decisions.ts` 的 provenance 思想（refs/flint/src/core/decisions.ts:33-43 的
EncodingTypeDecision 携带 channelOverride/cardinalityGuard 来源标记）应用到报告构建：
每次构建回答「报告里哪些是系统定的、哪些是作者/业务要求的」，让业务读者知道还有什么可改、
改哪一层（改 request 重新构建 vs 编辑器就地调）。

- 输入全部来自构建期已有产物：spec（合并后的请求）、manifest（编译产物）、构建消息、
  补丁键列表、prev-manifest.json 存在性（策展继承）。绝不重新推理第二遍。
- 输出确定性 Markdown（无时间戳），落 `<workdir>/decisions.md`，由
  report_build._write_portable_artifacts 在侧车落盘时调用（调用点自带 try/except，
  账目失败绝不把已构建成功的报告变成失败）。
- 列表展示统一封顶 12 条（超出给计数），保证大报告上账目本身可读。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# 策展键（编辑器状态，月度刷新经 curation_inherit 继承；AGENTS「月度刷新继承」节）
_CURATION_KEYS = ("columnAliases", "valueAliases", "hiddenCols", "fieldNotes")

# 业务常问但缺省关闭的表格能力（缺省开启的：mergeSame/showTotal/showBars/striped/measureNameRow/rowDrill）
_TABLE_DEFAULT_OFF = ("rowTotal", "showSubtotal", "showRank", "groupTopN", "treeView",
                      "sparkline", "subMeasures", "condFormat", "columnFilters", "dateScope", "dimScopes")
# 缺省开启、点名才关
_TABLE_DEFAULT_ON = ("mergeSame", "showTotal", "showBars", "striped", "measureNameRow", "rowDrill")

_LIST_CAP = 12


def _short_list(items: list[str], cap: int = _LIST_CAP) -> str:
    shown = items[:cap]
    tail = f" …等共 {len(items)} 项" if len(items) > cap else ""
    return "、".join(shown) + tail


def _fmt_count(n: int, noun: str) -> str:
    return f"{n} 项" if n else "无"


def _chart_line(c: dict[str, Any], spec_chart: dict[str, Any] | None) -> str:
    title = str(c.get("title") or "(未命名)")
    vt = str(c.get("viewType") or "?")
    parts = [f"「{title}」({vt}"]
    if spec_chart:
        if spec_chart.get("topK") is not None or spec_chart.get("limit") is not None:
            # dict.get 的缺省值只在键**缺失**时生效：topK 键存在但值为 null 时会
            # 渲染成「TOP-K=None」——显式 None 判断让 null topK 回落到 limit。
            _topk = spec_chart.get("topK")
            if _topk is None:
                _topk = spec_chart.get("limit")
            parts.append(f"TOP-K={_topk}")
        tbl = spec_chart.get("table")
        if isinstance(tbl, dict) and tbl:
            parts.append("table:" + ",".join(sorted(tbl.keys())))
        if isinstance(spec_chart.get("layout"), dict):
            parts.append("手工布局")
    if c.get("question"):
        # 分析主张/问题（NA-02）：作者声明落账，读者/复核者可见这张图「想回答什么」。
        parts.append(f"主张：{str(c['question'])[:80]}")
    if c.get("sortOverride"):
        parts.append("自定义排序")
    return "，".join(parts) + ")"


def build_ledger(spec: dict[str, Any], manifest: dict[str, Any], *, mode: str,
                 patch_keys: list[str] | None = None,
                 build_messages: list[str] | None = None,
                 has_prev_manifest: bool = False) -> str:
    spec = spec or {}
    manifest = manifest or {}
    patch_keys = patch_keys or []
    lines: list[str] = []
    add = lines.append

    add("# 决策清单（系统默认 vs 作者声明）")
    add("")
    add("> 回答「报告里哪些是系统定的、哪些是作者/业务要求的」。")
    add("> 改「作者声明」项：修改 request/补丁后重新构建；改「系统默认」项：在请求里显式声明对应键（见 references/authoring-feature-map.md），或直接在报告编辑器里调整。")
    add("")

    # ---- 报告级 ----
    add("## 报告级")
    add("")
    req_mode = (spec.get("runtime") or {}).get("mode")
    add(f"- 交付模式：`{mode}`" + (f"（请求显式声明 `{req_mode}`）" if req_mode and req_mode != "auto" else "（`auto` 按行数/字节/基数自动判定）"))
    if (manifest.get("templateId") or spec.get("templateId")) == "free-v1":
        add("- 模板：`free-v1`（自由模式）——只渲染请求显式声明的区块：封面/KPI 条/筛选带"
            "均为声明制，无自动填充；系统默认区不含「KPI 自动垫满/筛选候选」")
    pw = manifest.get("pageWidth")
    add(f"- 页面宽度：`{pw}`" + ("（缺省档）" if pw in (None, 1280) else "（作者声明）"))
    theme = manifest.get("chartTheme") or "default"
    add(f"- 报告主题：`{theme}`" + ("（缺省；运行时工具栏「主题 ▾」可随时切换）" if theme == "default" else "（作者声明）"))
    # 数据源独立性硬控（D2，docs/data-source-independence-and-merge-visibility-design.md）：
    # manifest 烘焙了 dataSourceMode 就读 manifest，否则回退 spec（构建期声明）。
    ds_mode = manifest.get("dataSourceMode") or spec.get("dataSourceMode")
    if ds_mode == "independent":
        add("- 数据源独立：作者要求各数据源完全独立，未做任何合并。")
    accent = (spec.get("style") or {}).get("accent")
    if accent:
        add(f"- 主色 accent：`{accent}`（作者声明，优先于主题默认）")
    sections = manifest.get("sections") or []
    if sections:
        add(f"- 分节结构：{len(sections)} 节（作者声明：{'、'.join(str(s.get('title') or '') for s in sections[:_LIST_CAP])}）")
    add("")

    # ---- 作者显式声明 ----
    add("## 作者显式声明")
    add("")
    if patch_keys:
        add(f"- 本次以**补丁模式**构建：以下顶层键来自补丁，其余取系统默认建议 —— {_short_list(patch_keys)}")
    charts = manifest.get("charts") or []
    spec_charts = spec.get("charts") or []
    spec_by_key: dict[str, dict[str, Any]] = {}
    for sc in spec_charts:
        if isinstance(sc, dict):
            if sc.get("id"):
                spec_by_key[str(sc["id"])] = sc
            if sc.get("title"):
                spec_by_key.setdefault(f"title:{sc['title']}", sc)
    add(f"- 图表 {len(charts)} 张：")
    for c in charts:
        sc = spec_by_key.get(str(c.get("id") or "")) or spec_by_key.get(f"title:{c.get('title')}")
        add(f"  - {_chart_line(c, sc)}")
    spec_kpis = spec.get("kpis") or []
    man_kpis = manifest.get("kpis") or []
    line = f"- KPI {len(man_kpis)} 张"
    if spec_kpis:
        line += f"（作者声明 {len(spec_kpis)}）"
    if len(man_kpis) > len(spec_kpis):
        line += f"；系统按 facts 自动补足 {len(man_kpis) - len(spec_kpis)} 张"
    add(line + "。")
    spec_filters = spec.get("filters") or []
    if spec_filters:
        add(f"- 交互筛选：作者声明 {len(spec_filters)} 条。")
    else:
        cands = manifest.get("filterCandidates") or []
        add(f"- 交互筛选：未声明 → 筛选器卡按维度自动填充（候选 {len(cands) if cands else '≤6'} 列；可在编辑器增删）。")
    # ---- 对账锚点（2026-10-15）：编译期硬闸的勾稽结果落账 ----
    # spec_to_report._check_anchors 通过硬闸后把实算/声明经 manifest.quality.anchors
    # 带出来；这里渲染成人话。手册 §2.4 承诺「实测值与锚点的比对结果记入决策清单」，
    # 此前只有失败时报错、成功时悄无声息。
    _anchor_checks = (manifest.get("quality") or {}).get("anchors") or []
    if _anchor_checks:
        add("")
        add("## 对账锚点（构建期已勾稽）")
        add("")
        add("以下合计真值在构建时于物化计算列后的真实表上重算（硬闸：不符即编译失败）：")
        add("")
        for _a in _anchor_checks:
            if not isinstance(_a, dict):
                continue
            _got = _a.get("actual")
            _exp = _a.get("declared")
            _got_s = f"{_got:,.10g}" if isinstance(_got, (int, float)) else str(_got)
            _exp_s = f"{_exp:,.10g}" if isinstance(_exp, (int, float)) else str(_exp)
            add(f"- ✅ {_a.get('label')}：{_a.get('measure')} {_a.get('aggregation') or 'sum'}"
                f" 实算 **{_got_s}**（声明 {_exp_s}）")
    # ---- 隐私提示（XS-02，2026-10-24）：作者自由文本命中敏感列取值的有界 warn 落账 ----
    # 隐私闸覆盖数据面（剥离/引用闸/服务端投影），作者手写文字属作者责任面：这里只
    # 记「哪个槽位命中了哪一列的几个值」，绝不回显值本身。
    _pwarns = (manifest.get("quality") or {}).get("privacyAuthorWarnings") or []
    if _pwarns:
        add("")
        add("## 隐私提示（作者自由输入，请自查）")
        add("")
        add("以下作者文本在构建期被发现包含敏感列（privacy.sensitiveColumns）的取值。"
            "隐私闸只剥离数据，不改写作者文字——发布前请确认这些段落是否应当出现：")
        add("")
        for _w in _pwarns[:_LIST_CAP]:
            if isinstance(_w, dict):
                add(f"- ⚠️ `{_w.get('slot')}` 含敏感列「{_w.get('column')}」（dataset {_w.get('dataset')}）"
                    f"的 {_w.get('hits')} 个取值")
        if len(_pwarns) > _LIST_CAP:
            add(f"- …等共 {len(_pwarns)} 条")
    # ---- 数据事实（NA-01）：构建期 profile.facts 确定性抽取的六类事实落账 ----
    # 与对账锚点同一通道（manifest.quality.*）：手册承诺的「脚本算出的事实可核对」
    # ——模型写洞察时引用的 `fact:<类>:<序号>` id 在这里能逐条对回原始数据。
    _facts = (manifest.get("quality") or {}).get("facts") or []
    if _facts:
        add("")
        add("## 数据事实（脚本计算）")
        add("")
        add("以下事实由 profile_table 在构建期从源数据确定性抽取（非模型生成；"
            "洞察/标题引用的 `fact:<类>:<序号>` id 即这里的条目，可在源数据逐条复核）：")
        add("")
        for _f in _facts[:_LIST_CAP]:
            if isinstance(_f, dict):
                add(f"- `{_f.get('id')}` {_f.get('statement')}")
        if len(_facts) > _LIST_CAP:
            add(f"- …等共 {len(_facts)} 条")
        _skipped = (manifest.get("quality") or {}).get("factsSkipped") or {}
        for _k, _reason in _skipped.items():
            add(f"-（{_k} 未判定：{_reason}）")
    add("")

    # ---- 数据集级声明 vs 推断 ----
    add("## 数据集：声明 vs 系统推断")
    add("")
    datasets = manifest.get("datasets") or []
    spec_datasets = {str(d.get("id") or "primary"): d for d in (spec.get("datasets") or []) if isinstance(d, dict)}

    def _spec_ds(ds_id: str) -> dict:
        # server/mixed 的 manifest dataset id 带 ``{reportId}__`` 前缀（report_build
        # 注册时加的命名空间），spec 里是裸 id——不剥前缀会把「作者声明」全部误记
        # 成「系统推断」，把继承来的策展全部误记成继承（声明优先级账目失真）。
        if ds_id in spec_datasets:
            return spec_datasets[ds_id]
        if "__" in ds_id:
            return spec_datasets.get(ds_id.split("__", 1)[1]) or {}
        return {}

    for ds in datasets:
        ds_id = str(ds.get("id") or "primary")
        label = ds.get("label") or ds_id
        sd = _spec_ds(ds_id)
        top_roles = spec.get("columnRoles") or {}
        declared_roles = dict(sd.get("columnRoles") or {})
        for col, role in top_roles.items():
            declared_roles.setdefault(col, role)
        resolved_roles = ds.get("columnRoles") or {}
        inferred = {col: role for col, role in resolved_roles.items() if col not in declared_roles}
        bits = []
        if declared_roles:
            bits.append(f"声明角色 {len(declared_roles)} 列")
        if inferred:
            bits.append(f"系统推断角色 {len(inferred)} 列（{_short_list([f'{k}→{v}' for k, v in inferred.items()])}）")
        mt = sd.get("measureTypes") or {}
        if mt:
            bits.append(f"声明度量语义 {len(mt)} 列")
        d = ds.get("derived") or {}
        if isinstance(d, dict):
            if d.get("columns"):
                bits.append(f"计算列 {len(d['columns'])}")
            if d.get("measures"):
                bits.append(f"派生指标 {len(d['measures'])}")
        if ds.get("dimGroups"):
            bits.append(f"维度值分组 {len(ds['dimGroups'])}")
        if sd.get("hierarchy"):
            bits.append(f"维度层级 {len(sd['hierarchy'])}")
        if ds.get("primaryKey"):
            bits.append(f"增量主键 {len(ds['primaryKey'])} 列")
        add(f"- **{label}**（{ds.get('rowCount', '?')} 行）：" + ("；".join(bits) if bits else "全部由系统推断（角色/格式按 profile 自动识别）"))
    # 合并透明（D2，docs/data-source-independence-and-merge-visibility-design.md）：
    # P2 起 manifest 同时携带源数据集与宽表数据集，账目要让业务看到「合并发生了、
    # 怎么合的」——来源表、关联键、宽表数据集 id；manifest 没有时回退读 spec。
    rels = manifest.get("relations") or spec.get("relations") or []
    for rel in rels:
        if not isinstance(rel, dict):
            continue
        tables = [str(t) for t in (rel.get("tables") or [])]
        key_bits: list[str] = []
        for k in rel.get("keys") or []:
            if not isinstance(k, dict):
                continue
            if k.get("name"):
                key_bits.append(str(k["name"]))
            else:
                a = (k.get("a") or {}).get("col")
                b = (k.get("b") or {}).get("col")
                key_bits.append(f"{a}={b}" if a and b else "?")
        wide = f"{tables[0]}__wide" if tables else "?__wide"
        add(f"- 数据源合并：来源表 {' + '.join(tables)}、关联键 {'、'.join(key_bits) or '（未声明）'}、"
            f"宽表数据集 id（{wide}）；源表保留，合并关系可在数据源配置解散。")
    add("")

    # ---- 系统默认 ----
    add("## 系统默认（未声明即生效；可点名修改）")
    add("")
    has_layout = any(isinstance(sc, dict) and isinstance(sc.get("layout"), dict) for sc in spec_charts)
    if not has_layout:
        add("- 版面：编译器自动排布（行模式 solo/wide/std + 密度策略；卡片高度按基数与军规计算）。")
    bucket_infos = []
    for c in charts:
        b = ((c.get("editor") or {}).get("data") or {}).get("dimensionBucket") or {}
        if isinstance(b, dict) and b.get("bucket"):
            bucket_infos.append(f"{b.get('bucket')}（{b.get('source')}）")
    if bucket_infos:
        uniq = sorted(set(bucket_infos))
        add(f"- 时间分桶：系统按列精度推断 → {'、'.join(uniq)}；可在图表面板维度芯片上改粒度。")
    add(f"- 表格卡缺省：默认**开启** —— {'、'.join(_TABLE_DEFAULT_ON)}；默认**关闭**（业务点名才开）—— {'、'.join(_TABLE_DEFAULT_OFF)}。")
    add("- TOP-N 截断是显示层：卡底带「已按首指标显示前 K 项（共 N 项）」徽标，合计仍按全量过滤行统计。")
    add("")

    # ---- 数据策展继承 ----
    cur_lines = []
    for ds in datasets:
        ds_id = str(ds.get("id") or "primary")
        carried = [k for k in _CURATION_KEYS if ds.get(k)]
        if carried:
            sd = _spec_ds(ds_id)
            inherited = [k for k in carried if not sd.get(k)]
            if inherited:
                cur_lines.append(f"{ds.get('label') or ds_id}：{'、'.join(inherited)}")
    if has_prev_manifest:
        add("## 数据策展（月度刷新继承）")
        add("")
        if cur_lines:
            add(f"- 以下策展自上一份报告继承（源 `prev-manifest.json`；请求显式声明 > 继承）：{_short_list(cur_lines)}")
        else:
            add("- 检测到上一份报告（`prev-manifest.json`），本轮无新增继承项（或均由请求显式声明）。")
        add("")

    # ---- 构建提示 ----
    msgs = [m for m in (build_messages or []) if str(m).strip()]
    if msgs:
        add("## 构建提示（advisory，非阻断）")
        add("")
        for m in msgs[:_LIST_CAP]:
            add(f"- {str(m).strip()[:300]}")
        add("")

    return "\n".join(lines) + "\n"


def write_ledger(path: Path, spec: dict[str, Any], manifest: dict[str, Any], *, mode: str,
                 patch_keys: list[str] | None = None,
                 build_messages: list[str] | None = None,
                 has_prev_manifest: bool = False) -> None:
    """把决策清单写到 workdir/decisions.md；任何失败由调用点的 try/except 兜底。"""
    doc = build_ledger(spec, manifest, mode=mode, patch_keys=patch_keys,
                       build_messages=build_messages, has_prev_manifest=has_prev_manifest)
    path.write_text(doc, encoding="utf-8")


if __name__ == "__main__":  # 手动检查：python3 decision_ledger.py <manifest.json> [--mode server]
    import sys
    ap = sys.argv[1:]
    mpath = ap[0]
    mmode = "server" if "--mode" in ap and len(ap) > ap.index("--mode") + 1 else "singlefile"
    man = json.loads(Path(mpath).read_text(encoding="utf-8"))
    print(build_ledger({}, man, mode=mmode, has_prev_manifest=False))
