#!/usr/bin/env python3
"""Compile a v1 ``report_request.json`` into a deterministic HTML draft.

The Agent supplies semantic choices only; this compiler guarantees the mechanical correctness that agents
keep getting wrong — R3 sorting, FM-14 heatmap cell indices, R2/L8 table
heights, measureTypes application, and zero function strings.

Internal normalized shape (not an authoring interface):
{
  "reportId": "id", "title": "title",
  "cover": {"title": "...", "meta": ["chip", ...], "summary": "..."},
  "kpis": [{"label": "...", "value": "...", "delta": "...", "accent": "blue|green|amber|violet"}],
  "charts": [
    {
      "id": "chart-x", "title": "...", "viewType": "bar|line|pie|donut|funnel|scatter|stackedBar|table",
      "dimension": "列名", "measures": ["度量"], "aggregation": "sum",
      "measureTypes": {"度量": "currency" | {...}},
      "data": {"columns": [...], "rows": [[...]]},       // chart-sized, agent-computed
      "sort": "desc" | "time" | "none",                  // desc re-sorts data (R3)
      "layout": {"x": 0, "y": 0, "w": 6, "h": 5},        // omit x/y for auto-pack
      "roles": {"breakdown": "..."},                     // 交叉列：heatmap + 交叉视图（聚合表格/堆叠柱）；
                                                         //   聚合表格声明了 breakdown 缺省即渲染**交叉矩阵**
                                                         //   （tableLayoutOf：有交叉列=matrix，无=grouped）
      "table": {"layout": "grouped|matrix", ...},        // 表格卡设置（可省：有交叉列缺省 matrix）
      "truth": {"dataset": "primary", "dimension": "...", "aggregation": "sum",
                "mapping": {"报告列": "数据源列"}},        // table only (optional)
      "option": {...}                                     // styling overrides only
    }
  ],
  "insights": ["<li>...</li>"],
  "source": {"label": "...", "notes": "..."},
  "provenance": {...}
}
"""
from __future__ import annotations

# Running from a deployed skill folder must not litter it with __pycache__/:
# the env var is inherited by spawned children (lint/package/spec_to_report),
# the flag covers imports made after this line in this process.
import os as _os, sys as _sys
_os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
_sys.dont_write_bytecode = True
import argparse
import html as _html
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

from semantics import flint_core as fc  # noqa: E402
from semantics.type_registry import currency_symbol  # noqa: E402  (货币符号解析：编译 ↔ 运行时同表)
import date_parse  # noqa: E402  (shared temporal parser: compile ↔ server ↔ runtime)
import derived_eval  # noqa: E402  (shared row-level derived columns: compile ↔ server)

# ---------- shared ----------

SHELL_CSS = """\
:root{--bg:#f6f5f4;--surface:#fff;--surface-soft:#fafaf9;--ink:#1a1a1a;--ink-deep:#000000;--ink2:#5d5b54;--ink3:#787671;--line:#e5e3df;--line-soft:#ede9e4;--accent:#5645d4;--blue:#5645d4;--green:#1aae39;--amber:#dd5b00;--violet:#7b3ff2;--shadow:0 1px 3px rgba(15,15,15,.04);--shadow-lg:0 6px 24px rgba(15,15,15,.07);--radius:12px;--text-h1:28px;--text-h2:18px;--text-h3:14px;--text-body:13px;--semantic-ok:#1a7f4b;--semantic-error-deep:#c62f2f;--semantic-warn:#c47f17;--semantic-warn-bg:#fdf3d9;--semantic-warn-border:#f0dca6;--focus-ring:#1a1a1a}
*{box-sizing:border-box}html,body{margin:0;padding:0;background:var(--bg);color:var(--ink);font-family:Inter,-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;font-size:15px;line-height:1.55}
.report-shell{max-width:var(--page-max,1280px);margin:0 auto;padding:32px 24px 64px}
/* 首屏加载纪律（2026-09-24，singlefile「打开时所有图表挤在左上角」根治）：
   gridstack.min.css 把 .grid-stack-item 设为 position:absolute，而纵向几何（top/height）
   是 GridStack v11 init 时才动态注入的逐行规则（[gs-y="N"]{top:…}）——初始化前所有卡片
   都收缩在容器原点互相重叠；singlefile 打开要先解析 ~2.5MB 内联 JS，这个堆叠窗口被拉长
   到肉眼可见。解开 = 初始化前隐藏卡片，运行时 initGridElement 在 GridStack.init 之后挂
   .ai-grid-ready 揭示（boot 是同一同步任务，读者从「加载中」直接跳到成品首帧）；预备期
   给画布保留高 + 轻提示，避免空白页观感。隐藏/揭示两条必须成对存在，只加隐藏不加揭示
   = 永久白屏（守卫 tests/test_boot_reveal.cjs）。inactive 页签是 display:none，预备期
   不会叠出多份保留高；deck/PDF 克隆只复制卡内容不复制 .grid-stack，不受影响。 */
.grid-stack>.grid-stack-item{visibility:hidden}
.grid-stack.ai-grid-ready>.grid-stack-item{visibility:visible}
.grid-stack:not(.ai-grid-ready){min-height:55vh}
.grid-stack:not(.ai-grid-ready)::before{content:"正在加载报告…";display:flex;align-items:center;justify-content:center;height:55vh;font-size:13px;color:var(--ink3,#787671);letter-spacing:.05em}
/* 旧 `.cover` 卡壳规则已删（W4 小单）：封面自段落化改造起恒由 .block.tx-banner.ai-cover-text
   渲染（_cover_article_html），class="cover" 无任何发射点——死规则清零。 */
.kpi-card{background:var(--surface);border-radius:var(--radius);padding:20px 14px 20px;box-shadow:var(--shadow);border:1px solid var(--line);height:100%;display:flex;flex-direction:column;gap:2px;justify-content:center}
/* R15（2026-09-25）：对比副行的「· 上期 X」基期段（runtime kpiFoot 注入，紧随
   .kpi-compare 之后）。中性墨色 + 表格数字，窄卡靠 kpi-foot 既有 flex-wrap 换行。
   V10（二轮 2026-09-25）：辅助字可读下限——<12px 的墨色用 ink2（ink3 浅底 ≈4.2:1
   不达 WCAG AA），字号 11px→11.5px。 */
.kpi-card .kpi-compare-base{font-size:11.5px;font-weight:500;color:var(--ink2);font-variant-numeric:tabular-nums lining-nums;white-space:nowrap}
.kpi-card .kpi-foot{display:flex;align-items:center;gap:8px;margin-top:4px;flex-wrap:wrap}.kpi-card .kpi-compare{font-size:11.5px;font-weight:600;color:var(--ink2);font-variant-numeric:tabular-nums lining-nums;white-space:nowrap}.kpi-card .kpi-compare.is-up{color:var(--semantic-ok)}.kpi-card .kpi-compare.is-down{color:var(--semantic-error-deep)}.kpi-card .kpi-progress{display:inline-flex;align-items:center;gap:5px;flex:1 1 auto;min-width:80px;max-width:180px}.kpi-card .kpi-progress .kpi-track{flex:1 1 auto;height:4px;min-width:36px;border-radius:999px;background:var(--line-soft,#ede9e4);overflow:hidden}.kpi-card .kpi-progress .kpi-track i{display:block;height:100%;border-radius:999px;background:var(--accent,#5645d4)}.kpi-card .kpi-progress em{font-size:11px;font-style:normal;color:var(--ink2);font-variant-numeric:tabular-nums}.kpi-card .label{font-size:12px;color:var(--ink3);font-weight:500}.kpi-card .value{font-size:25px;font-weight:700;letter-spacing:-.025em;font-variant-numeric:tabular-nums lining-nums;white-space:nowrap;color:var(--ink)}.kpi-card .unit{font-size:11.5px;color:var(--ink2);font-weight:500;margin-left:4px}.kpi-card .accent-bar{display:none}
/* W5 壳审美去默认化：36px 纯装饰 accent 横条退役。默认（无对比/无目标）完全退化，拒绝无语义装饰；
   卡片带对比值时（运行时 kpiFoot 注入 .kpi-compare.is-up/.is-down）升级为「中点=前期基准」的
   涨跌方向微条：右半填充=上行、左半=下行，状态色与对比文字同源（--semantic-ok /
   --semantic-error-deep，随主题 statusUp/statusDown 换肤）。幅度编译期不可得（对比 = 窗口
   派生指标，运行时按时间桶求值；请求层 compare/target 也被编译 canonical shape 丢弃），故以
   存在性 class + :has() 表达三态；带目标的卡已有 .kpi-progress 语义完成度条，本条保持隐藏。 */
.kpi-card:has(.kpi-compare) .accent-bar{display:block;position:relative;height:4px;width:36px;border-radius:999px;background:var(--line-soft,#ede9e4);margin-top:2px;flex-shrink:0;overflow:hidden}
.kpi-card:has(.kpi-compare) .accent-bar::before{content:'';position:absolute;left:50%;top:0;bottom:0;width:1px;background:var(--ink3,#787671);opacity:.4}
.kpi-card:has(.kpi-compare.is-up) .accent-bar::after{content:'';position:absolute;top:0;bottom:0;left:50%;width:50%;background:var(--semantic-ok,#1a7f4b)}
.kpi-card:has(.kpi-compare.is-down) .accent-bar::after{content:'';position:absolute;top:0;bottom:0;left:0;width:50%;background:var(--semantic-error-deep,#c62f2f)}
/* KPI 取数失败（主题3 错误态统一）：不静默回退旧值——显示「—」+ 错误角标（title 带原因），
   编辑态角标即重试入口（.kpi-err-retry，指针/hover 在 editor-runtime.css）。 */
.kpi-card .kpi-err-val{font-size:25px;font-weight:700;letter-spacing:-.025em;color:var(--ink3)}
.kpi-card .kpi-err-badge{display:inline-flex;align-items:center;justify-content:center;width:18px;height:18px;margin-left:6px;border-radius:999px;border:1px solid var(--semantic-warn-border);background:var(--semantic-warn-bg);color:var(--semantic-warn);font-size:11.5px;font-weight:700;line-height:1;padding:0;font-family:inherit;vertical-align:super}
.block{background:var(--surface);border-radius:var(--radius);border:1px solid var(--line);box-shadow:var(--shadow);padding:20px 18px 20px;height:100%;display:flex;flex-direction:column;overflow:hidden}
.block.ai-report-filter{padding:4px 14px;justify-content:center;overflow:visible;min-height:0}
.block-title{font-size:var(--text-h3);font-weight:600;display:flex;align-items:center;gap:8px;margin:0 0 12px}
.filter-chips{display:flex;flex-wrap:wrap;gap:8px;align-items:center}[data-report-filter] .filter-chips{flex-wrap:wrap;overflow:visible}.filter-chip{display:inline-flex;align-items:center;padding:4px 9px;border-radius:999px;background:color-mix(in srgb,var(--accent,#5645d4) 16%,var(--surface,#fff));border:1px solid color-mix(in srgb,var(--accent,#5645d4) 30%,var(--surface,#fff));color:var(--ink,#1a1a1a);font-size:12px;line-height:1.3;white-space:nowrap;flex:none}
.filter-chip .chip-full{display:none}.filter-chip-collapsible:hover .chip-short,.filter-chip-collapsible:focus-visible .chip-short{display:none}.filter-chip-collapsible:hover .chip-full,.filter-chip-collapsible:focus-visible .chip-full{display:inline}
.block-title .sub{font-size:12px;font-weight:400;color:var(--ink3)}
.chart-host{flex:1;width:100%;min-height:0}
/* 读者可见的口径说明：空态（筛选后无数据）+ TOP-N 截断（显示了 K / 共 N 项）。
   截断必须让读者看到 —— 静默只显示前 12 项会被当成「只有 12 项」（报告失真）。 */
.ai-report-no-data-msg{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px;height:100%;min-height:120px;text-align:center;padding:8px}
.ai-report-no-data-t{font-size:13px;font-weight:600;color:var(--ink2)}
.ai-report-no-data-sub{font-size:12px;color:var(--ink3)}
.ai-report-no-data-btn{height:32px;padding:0 14px;border:1px solid var(--line);background:#fff;border-radius:8px;font-size:12.5px;color:var(--ink);cursor:pointer}
.ai-report-no-data-btn:hover{border-color:var(--accent);color:var(--accent)}
.ai-report-data-note{flex:0 0 auto;margin:2px 2px 0;text-align:right;min-width:0;max-width:100%;overflow:hidden}
.ai-report-data-note-table{margin:6px 0 0;text-align:right}
.ai-report-trunc-note{display:inline-block;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11.5px;color:var(--ink2);background:var(--surface);border:1px solid var(--line);border-radius:999px;padding:1px 9px}
/* 表格「可横向滚动」提示（列宽自适应模式）：钉在 .ai-report-table-wrap 滚动容器
   内、height:0 零布局影响（不参与 autoFitTableCards 的 chrome 计算），徽标悬在
   表头右上；内层复用 .ai-report-trunc-note 的 pill 外观。
   align-items:flex-start（W4 随手修）：0 高 flex 容器缺省 stretch 会把 pill 压成
   2px 高、再被它自身的 overflow:hidden 裁掉——徽标不可见，且 noBlockOverflowNarrow
   误判「+18 溢出」；flex-start 让 pill 以内容高悬垂在 0 高容器外，零布局影响不变。 */
.ai-report-hscroll-note{position:sticky;top:0;left:0;z-index:5;display:flex;justify-content:flex-end;align-items:flex-start;height:0;margin:0 2px;pointer-events:none}
/* 条件格式启用后的卡片标注（runtime 插入，绝对定位在卡片右上角——不参与布局，
   不动 R2/容量数学）：图上颜色是阈值语义，读者需要知道颜色是「编码」不是装饰。 */
.ai-cffmt-badge{position:absolute;top:10px;right:12px;z-index:6;display:inline-flex;align-items:center;gap:4px;padding:2px 9px;border-radius:999px;font-size:11px;font-weight:600;line-height:1.5;color:var(--ink2);background:var(--surface);border:1px solid var(--line);pointer-events:auto;white-space:nowrap}
.ai-cffmt-badge i{width:7px;height:7px;border-radius:50%;display:inline-block}
.ai-cffmt-badge i:nth-of-type(1){background:var(--semantic-ok)}
.ai-cffmt-badge i:nth-of-type(2){background:var(--semantic-warn)}
.ai-cffmt-badge i:nth-of-type(3){background:var(--semantic-error-deep)}
/* 关键洞察卡（block-insight，2026-09-16 用户拍板与批注一致）：容器只剩 height
   与 strong 墨色——卡片身份走统一组件（emit 处加 tx-card + tx-annot 对角角色线，
   3px 左色条退役）。 */
.insight-block{height:100%}
.insight-block .tx-body strong{color:var(--ink)}
.source-block{background:var(--surface-soft,#fafaf9);border:1px dashed var(--line);border-radius:var(--radius);padding:14px 18px;font-size:12px;color:var(--ink3);height:100%}
.source-block code{font-family:ui-monospace,Menlo,monospace;background:var(--line-soft,#ede9e4);padding:1px 6px;border-radius:4px;font-size:11px;color:var(--ink2)}
/* Story sections (repo docs/story-sections-design.md): full-width narrative bands,
   per-section insight callouts, chart provenance chips and card annotations.
   All text blocks are ONE text-card model: virtual paragraphs (tx-p, levels
   h1/h2/h3/body — sizes come ONLY from the --text-* tokens) inside one card
   component (类型 text/annotation × 颜色 .txc-*，见下方统一组件区)。The legacy
   container classes remain as aliases: .section-narrative == 文本/default,
   .section-insight == 批注/soft。W2 角标批注重设计（2026-09-15）后 .section-insight
   只剩语义标记（关键洞察/本节小结的扩展挂点），不再有任何视觉特判——批注身份
   统一由 .tx-annot 的对角角色线承担。 */
.section-narrative{background:transparent;border:none;box-shadow:none;padding:8px 2px 2px}
.section-insight{background:var(--surface-soft);border:1px solid var(--line);border-radius:var(--radius);padding:10px 16px 12px}
/* 页签 (tab) card = section header in a single-grid report; it reads like the
   transparent narrative but carries the positional number (tx-tab-num) on its
   H2 title. The number is derived only on render (never stored), so reordering
   a 页签 card renumbers the bar + card together. 缺省透明（txc-none，分隔线语言）；
   显式 cardColor 有色 → .txc-* 标准卡盒 + 序号退场（编译 _tab_card_article 与
   运行时 rerenderTextBlock/applyInlineCardColor 同口径），.tx-card 的盒几何在
   级联上盖过本类的透明/无边框声明，无需新增颜色定义。 */
.tx-tab-card{background:transparent;border:none;box-shadow:none;padding:10px 2px 2px;border-bottom:1px solid var(--line-soft,#e5e3df);scroll-margin-top:60px}
.tx-tab-num{color:var(--accent);font-weight:700;margin-right:3px}
.tx-p{margin:0}
.tx-p+.tx-p{margin-top:6px}
.tx-h1{font-size:var(--text-h1);font-weight:700;letter-spacing:-.01em}
.tx-h2{font-size:var(--text-h2);font-weight:700}
.tx-h2.tx-eyebrow{letter-spacing:0}
.tx-h3{font-size:var(--text-h3);font-weight:600}
.tx-body{font-size:var(--text-body);line-height:1.65;color:var(--ink2)}
/* 深级标题（H4–H9，飞书段落模型对齐）：报告字号阶梯在 body(13px) 之下收底，
   级别语义保留、视觉以字重/墨色递减区分——BI 报告不出现 <11px 的正文级文字。 */
.tx-h4{font-size:var(--text-body);font-weight:600;color:var(--ink,#1a1a1a)}
.tx-h5{font-size:var(--text-body);font-weight:600;color:var(--ink2,#5d5b54)}
.tx-h6{font-size:12px;font-weight:600;color:var(--ink2,#5d5b54)}
.tx-h7{font-size:12px;font-weight:600;color:var(--ink3,#787671)}
.tx-h8{font-size:12px;font-weight:500;color:var(--ink3,#787671)}
.tx-h9{font-size:11.5px;font-weight:500;color:var(--ink2,#5d5b54)}
/* 段落级类型（对齐飞书 T▾ 菜单）：引用 / 代码块 / 高亮块。都是 tx-p 级别的
   单行样式——结构属性存 paragraphs[]，不进 text 字符串（见 _para_norm）。 */
.tx-quote{border-left:3px solid var(--line,#e5e3df);padding:2px 0 2px 12px;color:var(--ink2,#5d5b54)}
.tx-code{font-family:ui-monospace,Menlo,monospace;background:#f6f5f4;border:1px solid var(--line,#e5e3df);border-radius:8px;padding:8px 12px;font-size:12px;line-height:1.55;white-space:pre-wrap;overflow-x:auto;color:var(--ink,#1a1a1a)}
.tx-hlblock{background:var(--surface-soft,#fafaf9);border:1px solid var(--line-soft,#ede9e4);border-left:3px solid var(--accent,#5645d4);border-radius:8px;padding:8px 12px}
/* 列表/任务段落：标记是渲染期派生（ol 序号 span、task checkbox、ul ::before），
   挂靠在 tx-p 上（不生成 ul/ol 包裹——那会破坏 data-p-index 单行结构）。 */
.tx-list-ul{position:relative;padding-left:18px}
.tx-list-ul::before{content:"•";position:absolute;left:4px;color:var(--ink2,#5d5b54)}
.tx-list-ol{position:relative;padding-left:22px}
.tx-list-no{position:absolute;left:0;color:var(--ink2,#5d5b54)}
/* task checkbox 走行内（flex 会吃掉软换行 <br> 的行盒，编辑态无法换行） */
.tx-task-box{accent-color:var(--accent,#5645d4);width:14px;height:14px;margin:0 6px 0 0;vertical-align:-2px}
.tx-task-done{color:var(--ink3,#787671);text-decoration:line-through}
/* 透明样式 = 彩色孪生的「仅背景差异」版：盒几何（border 宽度/radius/padding）逐位对齐
   .tx-white / .tx-callout（用户契约：透明与彩色间不允许任何非背景布局差异）。
   border 用 1px transparent 保位——none 会让内容比白底宽 2px、首段上移 4px。 */
.tx-transparent{background:transparent;border:1px solid transparent;border-radius:var(--radius);padding:12px 16px;box-shadow:none}
.tx-white{background:var(--surface,#fff);border:1px solid var(--line);border-radius:var(--radius);padding:12px 16px;box-shadow:var(--shadow-sm,none)}
/* 旧批注卡（tx-callout 两变体）——仅「未再编辑的旧报告 HTML」服务。W2 角标批注
   重设计（2026-09-15）退役右上「批注」徽章与 D1 顶衬：几何回归与 .tx-white 孪生
   对齐（12px 16px，仅背景差异）；新渲染永远走统一组件 .tx-card+.tx-annot
   （角线身份），这里不再有徽章伪元素。 */
.tx-callout{position:relative;background:var(--surface-soft);border:1px solid var(--line);border-radius:var(--radius);padding:12px 16px}
.tx-callout-transparent{position:relative;background:transparent;border:1px solid transparent;border-radius:var(--radius);padding:12px 16px;box-shadow:none}
.tx-bubble{display:inline-block;background:var(--surface-soft,#f6f5f4);color:var(--ink,#1a1a1a);border:1px solid var(--line,#e5e3df);border-radius:999px;padding:3.4px 12px;font-size:var(--text-body);font-weight:600;margin:3px 0}
/* 气泡高度不可调：内部 md <p> 的上下外边距（13px×2）会把胶囊虚高 26px，必须清零；
   上下 padding 各减 15%（4px→3.4px）收紧胶囊高度。 */
.tx-bubble p{margin:0}
.tx-banner{background:linear-gradient(120deg,var(--cover-from,#070f24) 0%,var(--cover-via,#0a1530) 55%,var(--cover-to,#1a2a52) 100%);color:var(--cover-ink,#fff);border:none;border-radius:var(--radius);padding:16px 20px;box-shadow:var(--shadow-lg)}
.tx-banner .tx-h1,.tx-banner .tx-h2,.tx-banner .tx-h3{color:var(--cover-ink,#fff)}
.tx-banner .tx-body{color:color-mix(in srgb,var(--cover-ink,#fff) 92%,transparent)}
/* 封面卡 = 段落化的 banner 文本块（block-cover 用 _paragraphs_html 渲染）：标题 h1、
   短 meta 项为气泡 chip（每条自成一行、靠左自适应宽）、长导语/摘要为正文行。
   编辑态行内手柄/删除×/拖排与其它文本卡完全一致（见 editor-runtime.css）。 */
.tx-banner.ai-cover-text{padding:22px 32px;display:flex;flex-direction:column;justify-content:center;position:relative;
background:linear-gradient(165deg,var(--cover-from,#070f24) 0%,var(--cover-via,#0a1530) 100%)}
/* W5 壳审美收敛（默认观感改 CSS 表达，令牌与 12 主题 coverFrom/Via/To 派生公式不动）：
   封面从「满饱和 accent 渐变」退为深墨底——from→via 渐变近零彩度，页面唯一彩色让位数据层；
   数据色只保留封面底部 3px hairline（via→to 渐变，house 色向右浮现）。白字对比度对深墨底
   只升不降（原可读性 guard 即按更亮的 coverTo 校验）。纸底方案否决：coverInk=#ffffff 是
   12 主题统一令牌且 pptx 封面原生形状 = cover-from 实底 + 白墨，改纸底须动令牌轴，超出本工单红线。 */
.tx-banner.ai-cover-text::after{content:'';position:absolute;left:0;right:0;bottom:0;height:3px;background:linear-gradient(90deg,var(--cover-via,#0a1530),var(--cover-to,#1a2a52))}
.tx-banner.ai-cover-text .tx-h1{margin:0 0 2px}
.tx-banner.ai-cover-text .tx-p+.tx-p{margin-top:8px}
.tx-banner.ai-cover-text .tx-bubble-strip{align-self:flex-start}
.tx-banner.ai-cover-text .tx-bubble-strip .tx-bubble{background:rgba(255,255,255,.12);color:#fff;border:1px solid rgba(255,255,255,.2)}
/* 卡片气泡行：标题正下方一条不换行的横排气泡（数据源默认气泡 + 用户新增气泡）。
   与文本卡的 .tx-bubble 胶囊同款风格；超过卡宽横向滚动，绝不换行。 */
.tx-bubble-strip{display:flex;flex-wrap:nowrap;align-items:center;gap:6px;overflow-x:auto;overflow-y:hidden;margin:0 0 6px;padding:1px 0;min-height:0}
.tx-bubble-strip .tx-bubble{flex:0 0 auto;margin:0;white-space:nowrap}
/* 气泡条内的相邻气泡必须归零：外层 .tx-p+.tx-p 的 margin-top（常规 6px、封面 8px）
   会给第二个气泡叠加 margin，在 flex 居中时会把它顶偏约 3-4px，导致标题栏气泡
   显示不对齐。用更高特异性覆盖封面的 .tx-banner.ai-cover-text .tx-p+.tx-p。 */
.tx-bubble-strip .tx-p+.tx-p{margin-top:0}
.tx-banner.ai-cover-text .tx-bubble-strip .tx-p+.tx-p{margin-top:0}
/* C3（2026-09-25）：封面副标题区之下的「数据截至 YYYY-MM-DD」小字行（
   _cover_article_html 的 .cover-asof，data-ai-static 不进段落模型）。中性色 =
   封面墨 cover-ink 的降透明派生（深墨 banner 上没有 --ink2 可言，同一中性系口径
   换算到封面表面）；上一行间距由自身 margin 承担（它隔断了 .tx-p+.tx-p 链）。 */
.cover-asof{font-size:12px;line-height:1.5;color:color-mix(in srgb,var(--cover-ink,#fff) 62%,transparent);margin:8px 0 0}
/* 统一文本卡组件（2026-09-15 收敛；同日 W2「角标批注」重设计）：卡片 = 类型
   (文本/批注) × 颜色 两个正交维度，一套 .tx-card 几何 + .tx-annot 对角角色线 +
   .txc-* 仅背景/描边差异——不再有五种互相耦合的 textStyle。旧五值
   (tx-transparent/tx-white/tx-callout 系列/tx-banner) 保留在上方，只为「未再编辑的
   旧报告 HTML」服务；一切新写入（编译/运行时换样式）都走本组件。声明在旧规则之后：
   同卡同时带新旧类时（迁移间隙）新组件按级联获胜。
   注意：CSS 注释体内不许出现「星号+斜杠」序列（tx-callout* 写法会把注释提前终止，
   吃掉下一条规则——真机踩过）。 */
.tx-card{position:relative;background:var(--surface,#fff);border:1px solid var(--line);border-radius:var(--radius);padding:12px 16px;box-shadow:var(--shadow-sm,none)}
/* 2026-09-15 默认值改版：default=白底（上面的 .tx-card 基础即白底）；「透明」改为
   显式色值 .txc-none。页签卡缺省同用 txc-none（透明页签观感 + 序号）；显式 cardColor
   有色时换挂 .txc-*（标准卡盒，序号退场）——一套色板服务所有文本家族卡。 */
.txc-none{background:transparent;border-color:transparent;box-shadow:none}
/* W2 角标批注（2026-09-15 用户拍板）：批注卡与文本卡完全同构（同底/同字/同距/
   同圆角——不再有 26px 徽章顶衬），身份仅由左上 + 右下两只对角角色线承担。
   【规格（docs/ui-ux-execution-brief-2026-09-15.md §4-W2，防误读）】
   - 角线 2px 宽 × 28px 长，L 形贴内角；单伪元素 + 4 层 linear-gradient 两角实现
     （优选方案：一次绘制，减少重绘）。
   - 颜色 = var(--accent) 纯色，禁止 color-mix 白基（W3 令牌轴：深色壳不破）。
   - 显隐 = 阅读态常显；编辑态弱化至 50%（editor-runtime.css body.ai-report-editing）。
   - 与选中态（W4 四角）的区分表——两套角标体系不得误读：
       │          │ 批注角线            │ 选中角标（W4）        │
       │ 角数/位置 │ 对角 2 只（左上+右下）│ 四角 4 只             │
       │ 颜色     │ accent 主题色        │ 中性墨色（--focus-ring）│
       │ 臂长     │ 28px                │ 6px                  │
       │ 显隐     │ 阅读态常显           │ 仅编辑态选中时浮现     │
       │ 位置     │ 卡边框内侧           │ 卡外 2px（margin 带）  │
     选中态已随 W4 落地为四角 L 角标（editor-runtime.css
     [data-report-block].ai-report-selected::before），旧 2px accent 整框退役；
     同一张批注卡被选中时：外侧 4 只小中性角标 + 内侧 2 只长 accent 角线，互不遮挡。 */
.tx-annot::before{content:"";position:absolute;inset:0;pointer-events:none;background:linear-gradient(var(--accent,#5645d4),var(--accent,#5645d4)) 0 0/2px 28px no-repeat,linear-gradient(var(--accent,#5645d4),var(--accent,#5645d4)) 0 0/28px 2px no-repeat,linear-gradient(var(--accent,#5645d4),var(--accent,#5645d4)) 100% 100%/2px 28px no-repeat,linear-gradient(var(--accent,#5645d4),var(--accent,#5645d4)) 100% 100%/28px 2px no-repeat}
.txc-white{background:var(--surface,#fff);border-color:var(--line);box-shadow:var(--shadow-sm,none)}
.txc-soft{background:var(--surface-soft,#f6f5f4);border-color:var(--line)}
.txc-accent{background:color-mix(in srgb,var(--accent,#5645d4) 7%,var(--surface,#fff));border-color:color-mix(in srgb,var(--accent,#5645d4) 26%,var(--surface,#fff))}
.txc-green{background:#eef7ef;border-color:#cfe6d4}
.txc-amber{background:#fdf6e4;border-color:#f0dfae}
.txc-red{background:#fdf0ed;border-color:#f2cdc4}
.txc-dark{background:linear-gradient(120deg,var(--cover-from,#070f24) 0%,var(--cover-via,#0a1530) 55%,var(--cover-to,#1a2a52) 100%);border-color:transparent;box-shadow:var(--shadow-lg)}
.txc-dark .tx-h1,.txc-dark .tx-h2,.txc-dark .tx-h3{color:var(--cover-ink,#fff)}
.txc-dark .tx-body{color:color-mix(in srgb,var(--cover-ink,#fff) 92%,transparent)}
.txc-dark .tx-bubble{background:rgba(255,255,255,.12);color:var(--cover-ink,#fff);border-color:rgba(255,255,255,.2)}
/* W2：.section-insight 视觉豁免（徽章退场 + 3px 左条）一并退役——重设计后统一为
   角线语言；关键洞察/本节小结若需要区分，用 .section-insight 类（语义标记）留扩展，
   不在视觉上特判。 */
/* 图表口径注释（chart.annotation，与文本批注卡是两个功能）：W2 左条变体退役，
   过渡形态 = 素净脚注样式（W6 类型学边界会把口径说明正式收编为脚注）。 */
.block-annotation{margin:8px 0 2px;font-size:13px;line-height:1.65;color:var(--ink3);white-space:pre-wrap}
.block-annotation a{color:var(--accent)}
/* 批注位置：默认在图表下方；ann-right 把批注移到图表右侧列（标题/芯片仍占满宽）。 */
.block.ann-right{flex-direction:row;flex-wrap:wrap;align-content:flex-start}
.block.ann-right .block-title{flex:0 0 100%}
.block.ann-right .chart-host{flex:1 1 0;min-width:0}
.block.ann-right .block-annotation{flex:0 0 30%;min-width:180px;margin:0 0 0 12px}
/* Author-side Markdown content inside text blocks (repo repo docs/markdown-text-design.md). */
.tx-body p,.block-annotation p{margin:4px 0}
.tx-body ul,.block-annotation ul,.tx-body ol,.block-annotation ol{margin:4px 0;padding-left:20px}
.tx-body li,.block-annotation li{margin:2px 0}
.tx-body blockquote{border-left:3px solid var(--line-soft);margin:6px 0;padding:2px 0 2px 12px;color:var(--ink2)}
.tx-body hr,.block-annotation hr{border:0;border-top:1px solid var(--line-soft);margin:8px 0}
.tx-body code,.block-annotation code{font-family:ui-monospace,Menlo,monospace;background:#ede9e4;padding:0 4px;border-radius:4px;font-size:12px}
.tx-body pre{background:#f6f5f4;border:1px solid var(--line);border-radius:8px;padding:10px 12px;overflow:auto;font-size:12px;line-height:1.5;margin:6px 0}
.tx-body pre code{background:transparent;padding:0}
.tx-body a,.block-annotation a{color:var(--accent)}
.tx-body mark,.block-annotation mark,.tx-p mark{background:#f9e79f;color:inherit;padding:0 1px;border-radius:2px}
/* P2 text-card skeletons (textKind) + card-level style presets (whitelisted).
   Every token maps to a fixed rule; nothing arbitrary is ever interpolated.
   W2 左条退役：t-callout 的 5px accent 左边条删除——批注/强调身份统一角线语言
   （.tx-annot），textKind 骨架只保留底色/描边。 */
.t-heading{background:transparent;border:none;box-shadow:none}
.t-paragraph{background:transparent;border:none;box-shadow:none}
.t-callout{background:linear-gradient(135deg,color-mix(in srgb,var(--accent,#5645d4) 6%,var(--surface,#fff)) 0%,color-mix(in srgb,var(--accent,#5645d4) 14%,var(--surface,#fff)) 100%);border:1px solid color-mix(in srgb,var(--accent,#5645d4) 22%,var(--surface,#fff))}
.t-quote{background:#fafaf9;border:1px solid var(--line-soft);border-left:5px solid var(--accent,#5645d4)}
.t-divider{background:transparent;border:none;box-shadow:none;align-items:center;justify-content:center;padding:0;flex-direction:row}
.t-divider .section-title,.t-divider .section-body,.t-divider .tx-p,.t-divider h3,.t-divider p{display:none}
.t-divider::after{content:"";display:block;width:100%;border-top:1px solid var(--line-soft)}
/* Workbench tabs (repo repo docs/workbench-tabs-design.md): top tab bar + real Tab views. */
.report-shell-tabs{max-width:var(--page-max,1280px);margin:0 auto}
/* The tab bar is the report's single top bar: one white rounded strip. The
   right-hand actions group (layout toggle + docked runtime toolbar) is moved
   in by editor-runtime.js → dockToolbarIntoTabbar and pushed right via its own
   margin-left:auto (editor-runtime.css). */
.ai-report-tabbar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;padding:6px 12px;margin:0 0 10px;background:var(--surface);border:1px solid var(--line);border-radius:var(--radius)}
/* V11（ux-review-round2 2026-09-25）：阅读态页签多时不再换行堆叠（换行把封面往下压
   成两三条）——横向滚动 + 细滚动条 + 两端 12px 渐隐 mask 提示可滚。渐隐带宽 = 左右
   padding：静止时首/尾页签不落进渐隐带，滚动中边缘内容才被隐；mask 只影响外观，
   flex 结构、focus-visible 黑环与活动页签形态都不动。编辑态保持换行：docked 工具条
   的下拉菜单是 position:absolute，会被 overflow 滚动容器剪裁，且「＋ 页签」要常驻
   可见（审查证据 = 读者封面截图的堆叠页签）。 */
body:not(.ai-report-editing) .ai-report-tabbar{flex-wrap:nowrap;overflow-x:auto;scrollbar-width:thin;scrollbar-color:var(--line,#e5e3df) transparent;-webkit-overflow-scrolling:touch;mask-image:linear-gradient(90deg,transparent 0,#000 12px,#000 calc(100% - 12px),transparent 100%);-webkit-mask-image:linear-gradient(90deg,transparent 0,#000 12px,#000 calc(100% - 12px),transparent 100%)}
body:not(.ai-report-editing) .ai-report-tabbar::-webkit-scrollbar{height:6px}
body:not(.ai-report-editing) .ai-report-tabbar::-webkit-scrollbar-thumb{background:var(--line,#e5e3df);border-radius:999px}
body:not(.ai-report-editing) .ai-report-tabbar::-webkit-scrollbar-track{background:transparent}
/* C3（2026-09-25）：阅读态 tabbar 右侧「数据截至 YYYY-MM-DD」徽章（runtime
   refreshDataAsofBadge 注入，meta.dataAsOf 缺省即不渲染；编辑态 data-visible=false
   隐藏）。中性系令牌 + 11px，与页签同高错开，不做交互元素。 */
.ai-report-data-asof{margin-left:auto;flex:0 0 auto;display:inline-flex;align-items:center;height:24px;padding:0 10px;border:1px solid var(--line);border-radius:999px;font-size:11px;font-weight:500;color:var(--ink2);white-space:nowrap;font-variant-numeric:tabular-nums lining-nums}
.ai-report-data-asof[data-visible="false"]{display:none}
.ai-report-tab{appearance:none;border:1px solid var(--line);background:transparent;color:var(--ink2);border-radius:999px;padding:0 14px;font-size:13px;font-weight:600;cursor:pointer;transition:background .15s,color .15s,border-color .15s;display:inline-flex;align-items:center;height:32px}
.ai-report-tab:hover{border-color:var(--accent);background:var(--line-soft)}
.ai-report-tab.ai-report-tab-active{background:var(--ink-deep);border-color:var(--ink-deep);color:#fff}
.ai-report-tab-add{appearance:none;border:1px dashed var(--line);background:transparent;color:var(--ink3);border-radius:999px;padding:0 14px;font-size:13px;cursor:pointer;display:none;height:32px;align-items:center}
body.ai-report-editing .ai-report-tab-add{display:inline-flex}
.ai-report-tab-add:hover{border-color:var(--accent)}
/* A3/4.3：壳层焦点环 = 单一焦点色令牌（--focus-ring，:root 声明）——读者侧
   页签/筛选 chip/布局切换不再用 UA 默认蓝，也不随报告主题 accent 漂移。 */
.ai-report-tab:focus-visible,.ai-report-tab-add:focus-visible,.filter-chip:focus-visible,.ai-report-layout-toggle:focus-visible{outline:2px solid var(--focus-ring,#1a1a1a);outline-offset:2px}
.ai-report-tab-view[hidden],.ai-report-tab-view.ai-report-view-hidden{display:none}
.ai-report-tab-view{min-height:24px}
.table-host{flex:1;width:100%;min-height:0;overflow:auto;display:flex;flex-direction:column}
/* 明细视图卡：runtime 往 host 里渲染 工具条(.ai-report-table-bar，约40px) + wrap。
   wrap 自带 height:100%（editor-runtime.css）——按 host 全高解析时正好多出工具条的
   40px，被 host 裁成恒定纵向滚动条（真机 900px 视口 PSI 卡 +40px，autofit 量
   host.scrollHeight 永不收敛）。flex 纵向让工具条占实高、wrap 吃剩余空间。 */
.table-host>.ai-report-table-bar{flex:0 0 auto}
.table-host>.ai-report-table-wrap{flex:1 1 auto;min-height:0}
/* 运行时 BI 表格卡（host 是 .chart-host，渲染时挂 .ai-table-host）：与 .table-host
   同一条 flex 纪律——gridstack v11 自带 item-content overflow:hidden auto，
   title+bar+wrap(height:100%) 的 ~60px 结构差会顶出第二层「卡级」纵向滚动条
   （真机：新插入明细卡 wrap+bi-card 双层滚动，gs-h 恒 8 无人重算）。wrap 是唯一
   滚动层，height:100% 在此上下文作废。 */
.ai-table-host{display:flex;flex-direction:column;min-height:0}
.ai-table-host>.ai-report-table-bar{flex:0 0 auto}
.ai-table-host>.ai-report-table-wrap{flex:1 1 auto;min-height:0;height:auto}
table.ai-report-table{width:100%;border-collapse:collapse;font-size:13px}
/* WP-1 表头变量钩子：--thead-ink/--thead-align 缺省回落到历史字面值（外观零变化）。
   运行时表格卡把卡级覆盖写在 .ai-report-table-wrap 内联（editor-runtime.css [data-*]
   规则消费）；这里让编译侧静态 ai-report-table 表同样认这两个变量名。 */
/* thead 冻结收窄（WP-7 表述对齐）：sticky 只锚在**有界滚动容器**里——静态表只有
   .table-host（明细固定视图，overflow:auto）是滚动容器；运行时表格卡的有界冻结由
   editor-runtime.css 的 [data-bounded] 作用域规则承担。无界 grow 卡上 sticky 会吸到
   视口顶盖住数据行（judge 抓过的真机现象），故不再无作用域下发。 */
table.ai-report-table thead th{background:var(--thead-bg,#f6f5f4);color:var(--thead-ink,var(--ink2));padding:8px 10px;text-align:var(--thead-align,left);font-weight:500;font-size:11px;border-bottom:1px solid var(--line)}
.table-host table.ai-report-table thead th{position:sticky;top:0;z-index:4}
table.ai-report-table tbody td{padding:7px 10px;border-bottom:1px solid var(--line-soft);color:var(--ink2)}
table.ai-report-table tbody tr.striped td{background:#fafaf9}table.ai-report-table td.num{text-align:right}
/* WP-9 迷你图趋势列（sparkline）：行尾纯 SVG polyline；--row-h 是 WP-1 密度档行高变量
   （tblWrapOpen 写在 .ai-report-table-wrap 内联），SVG 高度随档自适应；颜色 var(--accent)
   跟报告主题。趋势格零背景样式——fullRow 整行背景经透明格透出。 */
table.ai-report-table td.ai-cell-spark{padding:4px 8px;vertical-align:middle}
.ai-spark-svg{display:block;width:100%;height:calc(var(--row-h,40px) - 12px);min-height:12px;max-height:32px}
/* 自由表格卡（kind:'grid'，读者可见外观）：手绘网格 = 全网格线 + 可合并 + 行内样式；
   单一渲染真源 gridTableHtml（editor-runtime.js），样式只在 SHELL_CSS 声明。 */
.ai-gridcard .block-title{margin-bottom:10px}
.ai-gridcard-table{border-collapse:collapse;width:100%;font-size:12.5px;font-family:inherit;background:var(--surface)}
.ai-gridcard-table td{border:1px solid var(--line);padding:6px 10px;color:var(--ink2);vertical-align:top;min-width:24px;height:29px;line-height:1.5;overflow-wrap:anywhere}
.ai-gridcard .ai-report-table-wrap.ai-gridcard-wrap{flex:1 1 auto;min-height:0;overflow:auto}
/* 拼版占位卡（kind:'compose'，docs/compose-canvas-design.md）：读者可见样式唯一
   声明处。灰底虚线卡 = 「未完成声明」，展示逐块 NL 描述与图表类型意向；生成
   （转正）后由真实卡片替换，本样式随占位块一起消失。 */
.ai-compose-card{position:relative;height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px;text-align:center;padding:16px 18px;border:1.5px dashed var(--accent,#5070dd);border-radius:10px;background:color-mix(in srgb,var(--accent,#5070dd) 4%,var(--surface,#fff))}
.ai-compose-badge{position:absolute;top:8px;right:10px;font-size:11px;color:var(--ink2);border:1px solid var(--line);border-radius:99px;padding:1px 8px;background:var(--surface)}
.ai-compose-icon{width:34px;height:34px;border-radius:9px;display:flex;align-items:center;justify-content:center;font-size:15px;font-weight:600;color:var(--accent,#5070dd);background:color-mix(in srgb,var(--accent,#5070dd) 12%,transparent)}
.ai-compose-nl{max-width:92%;font-size:13px;line-height:1.6;color:var(--ink2);white-space:pre-wrap;overflow-wrap:anywhere}
.ai-compose-nl.is-empty{color:var(--ink3)}
.ai-compose-meta{font-size:11px;color:var(--ink2)}
/* 空白画布引导（draft 且零块时由运行时注入 .grid-stack 内，非 grid-stack-item）。 */
.ai-compose-empty{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;padding:64px 24px;color:var(--ink3);font-size:13.5px;text-align:center}
.ai-compose-empty .ai-compose-empty-actions{display:flex;gap:10px}
.ai-compose-empty button{cursor:pointer;border:1px solid var(--line);background:var(--surface);color:var(--ink);border-radius:8px;padding:7px 14px;font-size:13px}
.ai-compose-empty button:hover{border-color:var(--accent,#5070dd);color:var(--accent,#5070dd)}
/* V5（ux-review-round2 2026-09-25）阅读页打印基线：此前 SHELL_CSS 0 条 @media print，
   读者在报告页 Ctrl+P = 空白/错乱（编辑器 runtime 里唯一的 print 样式只服务汇报导出
   的 #ai-print-root）。目标 = 浏览器直接打印可读，不是替代「分享中心 → 导出 PDF」：
   藏 chrome（顶栏/页签条/「＋ 页签」/横滚提示徽标）与筛选卡的控件件（保留卡片标题
   与已选值 chips 文案——chips 是 span，不受 button/select/input 藏匿影响）；卡片尽量
   不跨页切割；页宽放开到纸面。保持克制，只兜可读下限。 */
@media print{
  #ai-report-toolbar,.ai-report-tabbar,.ai-report-tab-add,.ai-report-hscroll-note{display:none!important}
  [data-report-filter] button,[data-report-filter] select,[data-report-filter] input{display:none!important}
  .grid-stack-item,.block{break-inside:avoid}
  .report-shell,.report-shell-tabs{max-width:100%;padding:0;margin:0}
  body{background:#fff}
}
"""

PALETTE = {"blue": "#5645d4", "green": "#1aae39", "amber": "#dd5b00", "violet": "#7b3ff2", "cyan": "#2a9d99", "pink": "#ff64c8"}

def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0

def _num_or_null(v):
    """Like ``_num`` but keeps a missing/blank cell as ``None``.

    Derived measures need this: a 环比 value whose previous period is absent must
    be an EMPTY point (ECharts breaks the line), never 0 — 0 would read as "0%
    growth". Base measures keep the historical ``_num`` → 0 coercion so existing
    static outputs stay byte-identical.
    """
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def _series_value(c: dict, m: str, v) -> Any:
    """Series value for one measure cell.

    null-preserving for derived measures AND for explicit ``None`` cells: the
    BI-H3 merged 「其他」 row leaves non-additive measures (average/min/max) as
    ``None`` (a gap is honest, 0 would be a fake number). Blank *strings* keep
    the historical ``_num`` → 0 coercion so existing static outputs stay
    byte-identical.
    """
    if m in (c.get("derivedMeasures") or []) or v is None:
        return _num_or_null(v)
    return _num(v)


def _non_additive(c: dict) -> tuple[bool, str]:
    """Return (is_non_additive, hint) for a chart's primary measure.

    Rates / percentages / balances are not additive: summing or stacking them
    yields a meaningless total. The compiler refuses such a chart so the
    editor never has to explain a broken number. 2026-10-13 批：price（Flint
    Price intensive，单价/售价）同闸——把价格加总是「竞品街价报告」病灶。
    """
    measure = (c.get("measures") or [None])[0]
    if not measure:
        return False, ""
    mt = (c.get("measureTypes") or {}).get(measure)
    if isinstance(mt, str):
        t = mt
    elif isinstance(mt, dict):
        t = mt.get("type")
    else:
        t = None
    if t in ("percent", "unitless", "price"):
        return True, f"{measure} 为 {t} 类型"
    from measure_semantics import semantic_label_of
    label = semantic_label_of(mt, measure)
    if label in ("Percentage", "PercentageChange", "Price", "Score", "Temperature",
                 "Sentiment", "Correlation"):
        return True, f"{measure} 为 {label}（intensive，不可加）"
    if any(k in measure for k in ("率", "占比", "百分比", "比例")):
        return True, f"{measure} 含百分比语义"
    return False, ""

def _money(v):
    if v >= 1e8: return f"¥{v/1e8:.2f}亿"
    if v >= 1e4: return f"¥{v/1e4:.1f}万"
    return f"¥{v:,.0f}"

# ---------- layout ----------

# KPI card grid height. Cards are 2-row (label + value) after the derived
# delta line was removed, so 1 grid row each is enough (3 was too tall).
_KPI_H = 3  # KPI cards get 3 grid rows so the 28px drag strip top-space fits without overflow


def _chart_width(c: dict) -> int:
    """Effective grid width of a chart block.

    Mirrors ``pack_layout`` so the rendered ``gs-w`` always matches the width the
    packer used when placing the block. Wide blocks (table/scatter→12,
    breakdown crosstab→8, else 6) must be drawn at the same width they were packed
    at, otherwise the reserved columns are left empty and the grid collapses into
    a single left column (card misalignment)."""
    from layout_solver import pack_width
    lay = c.get("layout") or {}
    if "w" in lay:
        return min(12, max(1, int(lay["w"])))
    return pack_width(c)


def _autopaginate_table(chart: dict, grid_h: int) -> bool:
    """R2 自动分页（2026-09-21）：table 图表的静态行数超过格高容量、且作者未声明
    分页/有界高度时，在编译产物副本上补 ``option.pagination=true``。豁免口径与
    ``validate_report.check_table_fits_container`` 完全对齐（pagination 豁免 +
    heightMode.bounded 自有预算）；容量换算同一真源 ``max_table_rows_for_height``。
    只在 pack_layout 的自动拼版路径调用（那里才有浅拷贝可写，且绝不改调用方的
    chart）；显式 x/y 的作者布局不重塑（既有契约）。返回是否补了标记。"""
    if chart.get("viewType") != "table":
        return False
    option = chart.get("option") or {}
    rows = option.get("rows")
    if not isinstance(rows, list) or not rows or option.get("pagination"):
        return False
    # 有界滚动自有预算，R2 对其豁免行数。豁免读取走 layout_solver 的双形状
    # 真源：pack_layout 时图表还是**请求形状**（table 挂顶层，editor.table 要
    # 到 manifest 组装才落）——只读 editor.table 的话，作者声明的
    # heightMode.bounded 在编译期恒读不到，有界表被再补一道 pagination=True，
    # 与「豁免口径与 check_table_fits_container 完全对齐」的自述矛盾。
    from layout_solver import _table_height_mode_px
    if _table_height_mode_px(chart) is not None:
        return False
    from validate_report import max_table_rows_for_height
    if len(rows) <= max_table_rows_for_height(int(grid_h)):
        return False
    chart["option"] = dict(option, pagination=True)
    return True


def pack_layout(charts: list[dict], start_y: int = 0) -> list[dict]:
    """Auto-pack 12-col grid below the fixed template header.

    Widths/heights come from the backend-agnostic layout solver (layout_solver)
    so singlefile and server layouts stay identical and heights follow
    cardinality instead of being hardcoded per viewType.

    Authoring path (no chart carries an explicit ``layout.x/y``): charts are
    composed into full-width rows by ``layout_solver.compose_rows`` (solo 12 /
    duo 6+6 / wide+small 8+4 / trio 4+4+4), so a row never leaves an empty
    column gap. Heights inside a row align to the tallest member.

    Edited path (any chart has explicit x/y — i.e. the user arranged it):
    deterministic first-fit-with-backfill exactly as before, so authored
    layouts are never reshaped by recompiles."""
    from layout_solver import solve_layout, compose_rows
    explicit = any("x" in (c.get("layout") or {}) and "y" in (c.get("layout") or {})
                   for c in charts)
    if not explicit:
        placed: list[tuple[dict, int, int]] = []
        y = 0
        for row in compose_rows(charts):
            # Align every card in the row to the tallest member's height.
            eff = []
            for c, w in row:
                lay = c.get("layout") or {}
                solver_h = int(solve_layout(c, width=w)["h"])
                h = max(int(lay["h"]), solver_h) if "h" in lay else solver_h
                eff.append(h)
            row_h = max(eff) if eff else 1
            # B4 annotation blocks sit directly below the row; reserve the
            # tallest annotation's rows so no later card overlaps it.
            row_ann = max([_annotation_block_h(c, w) for c, w in row] + [0])
            x = 0
            for c, w in row:
                # Never mutate the caller's chart (pack_layout must stay
                # idempotent — tests compile the same spec twice): the composed
                # w/h ride on a shallow copy that flows through placed → render.
                cc = dict(c)
                lay = dict(c.get("layout") or {})
                lay["w"] = w
                if "h" not in (c.get("layout") or {}):
                    lay["h"] = row_h
                cc["layout"] = lay
                # R2 自动分页（2026-09-21）：真机 289K 行报告 8 张表因「行数超限」
                # 构建失败，执行方只能逐卡手补 pagination。这里按 R2 同一豁免口径
                # 自动补标记并打印，构建日志可见、可用 pagination=false 显式关掉。
                if _autopaginate_table(cc, int(lay.get("h") or row_h)):
                    print(f"[layout] 图表 {cc.get('id', '?')}（table）：静态行数超过 "
                          f"gs-h={cc['layout'].get('h')} 的格容量 → 已自动开启分页"
                          "（如需整表渲染请声明 pagination=false 并加大 layout.h）")
                placed.append((cc, x, start_y + y))
                x += w
            y += row_h + row_ann
        return placed
    # Track occupied rows as a list of (x, w) spans per row index.
    rows: list[list[tuple[int, int]]] = []
    placed = []

    def _is_pinned(c: dict) -> bool:
        lay = c.get("layout") or {}
        return "x" in lay and "y" in lay

    # 两段式（2026-09-28「全部放开」④）：显式坐标路径先钉完所有钉位卡，未钉卡
    # 再从**已钉内容最低行之下**顺次 free-gap 落位。旧的单遍混合曾经未钉卡钻进
    # 钉卡之间的缝隙、甚至落在钉卡上方——部分坐标（free+草图转译的设计入口）
    # 的排版于是不可预期。密度损失（钉卡之间的洞不再被自动填充）是刻意取舍：
    # 可预期性 > 紧凑度；要贴着钉卡放就把它也钉上。
    flow = [c for c in charts if _is_pinned(c)] + [c for c in charts if not _is_pinned(c)]
    flow_floor = 0
    for c in flow:
        lay = c.get("layout") or {}
        w = _chart_width(c)
        h = int(lay.get("h", solve_layout(c, width=w)["h"]))
        if _is_pinned(c):
            x, y = int(lay["x"]), int(lay["y"])
            placed.append((c, x, start_y + y))
            # The block may span several rows; record it so later blocks don't overlap it.
            for r in range(y, y + h):
                while len(rows) <= r:
                    rows.append([])
                rows[r].append((x, w))
            flow_floor = max(flow_floor, y + h)
            continue
        # Find the lowest row index — never above the pinned extent — with a free
        # gap of width `w` free across the block's full height (a block spans `h`
        # rows, so every covered row must have that gap clear or the block would
        # overlap an earlier card).
        placed_row = None
        placed_col = None
        r = flow_floor
        while True:
            spans = sorted(rows[r]) if r < len(rows) else []
            for x in _free_gaps(spans, w, 12):
                # Verify the same column gap is clear on every row the block spans.
                if all(x in _free_gaps(sorted(rows[rr]) if rr < len(rows) else [], w, 12)
                       for rr in range(r, r + h)):
                    placed_row, placed_col = r, x
                    break
            if placed_row is not None:
                break
            r += 1
        # Record occupancy for every row the block covers.
        for rr in range(placed_row, placed_row + h):
            while len(rows) <= rr:
                rows.append([])
            rows[rr].append((placed_col, w))
        flow_floor = max(flow_floor, placed_row + h)
        placed.append((c, placed_col, start_y + placed_row))
    return placed


def _free_gaps(spans: list[tuple[int, int]], w: int, total: int = 12) -> list[int]:
    """Leftmost x positions where a `w`-wide block fits between occupied spans.

    `spans` are (start_x, width) tuples on a `total`-column row. Returns all
    free gap start positions sized >= `w`, in left-to-right order."""
    if not spans:
        return list(range(0, total - w + 1))
    gaps: list[int] = []
    cur = 0
    for x, sw in sorted(spans):
        free = x - cur
        if free >= w:
            gaps.append(cur)
        cur = max(cur, x + sw)
    if total - cur >= w:
        gaps.append(cur)
    return gaps

def _default_h(c: dict) -> int:
    """Default grid height for a chart block, via the layout solver
    (chrome-aware; the annotation caption is measured at the card's width)."""
    from layout_solver import solve_layout
    return solve_layout(c, width=_chart_width(c))["h"]


def _block_h(c: dict) -> int:
    """Effective grid height for a block (explicit layout.h wins)."""
    lay = c.get("layout") or {}
    if "h" in lay:
        return int(lay["h"])
    from layout_solver import solve_layout
    return int(solve_layout(c, width=_chart_width(c))["h"])


# ---------------------- text sizing: display-width, not char count -------------
# Every text band/annotation/insight height comes from ONE line counter. Counting
# CHARACTERS (the old `len(text)/64` rule) silently under-estimated CJK+Latin
# mixed prose: at the real 12-col width (~940px inner at a 1024px viewport) a
# 1196px-wide paragraph needs 2 lines while 118 chars "fits" in 2 lines of the
# estimate — the extra REAL line is ~21px, and the card renders with a clipped
# last line (audit: `noBlockOverflow … tx-tab-card:+15`). The runtime
# `autoFitTextBlocks` can now also grow a stretched card, but sizing must still
# be right at compile time (the static export has no runtime pass).
#
# Capacity is measured in HALF-WIDTH UNITS (CJK/full-width = 2, everything else =
# 1) so mixed prose converges: ≈130 units ≈ 65 CJK ≈ 130 Latin per line at the
# narrow-safe (1024px) viewport. Slightly conservative on purpose — a band that
# is one row too tall is invisible, a clipped line is not.
_ROW_PITCH_PX = 56          # GridStack cellHeight 48 + margin 8
_BAND_PAD_PX = 40           # .block / .tx-* card padding (20 + 20)
_TEXT_LINE_PX = 22          # body 13px × 1.65 line-height
_TEXT_UNITS_PER_LINE = 130  # 12-col capacity in half-width units


def _text_units(text: str) -> int:
    """Display width in half-width units (CJK/full-width = 2, else 1)."""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _text_lines(text: str, cpl: int = _TEXT_UNITS_PER_LINE) -> int:
    """Wrapped line count of ONE paragraph at ``cpl`` half-width units/line."""
    if not text:
        return 1
    return max(1, -(-_text_units(text) // cpl))


def _text_rows(px: int, min_h: int, max_h: int) -> int:
    """Snap a required content height to grid rows (h rows give 56h - 8 px)."""
    return max(min_h, min(max_h, -(-(px + 8) // _ROW_PITCH_PX)))


def _text_band_h(text: str, min_h: int, max_h: int, cpl: int = _TEXT_UNITS_PER_LINE,
                 line_px: int = _TEXT_LINE_PX, title_px: int = 0) -> int:
    """Grid height for a narrative/insight band (padding + wrapped lines + title).

    ``text`` may be ``title\\nbody`` (how ``layout_sections`` composes a section
    band): the title renders as its OWN line at its own size, so it is never
    counted into the body's wrap capacity (that off-by-one-line was the bug).
    ``cpl`` is a capacity in half-width units (see ``_text_units``).
    """
    if not text:
        return min_h
    head, sep, body = text.partition("\n")
    lines = _text_lines(body if sep else head, cpl)
    px = _BAND_PAD_PX + title_px + lines * line_px
    return _text_rows(px, min_h, max_h)


def _annotation_block_h(c: dict, width: int | None = None) -> int:
    """Grid rows for an independent annotation text block (B4 migration).

    A chart that carries ``chart.annotation`` is compiled into a standalone
    transparent text block directly below the chart instead of an in-card
    caption. The block is one body paragraph, wrapped at a chart-width-dependent
    capacity (mirrors the runtime addAnnotationBlock 'below' placement); at least
    2 rows and at most 5. The 26px chrome covers the D1 badge layout (W2 角标
    批注后 .tx-annot 与 .tx-card 同构：padding 12×2 + 描边 1×2)，地板 2 行保证
    对角角线之外仍有一行正文可读，封顶 5 防长口径注释淹没下一张卡.
    ``width`` overrides the composed row width (used during packing, where the
    shallow copy's layout.w is not yet set). Charts without an annotation
    contribute 0 rows (no visual change)."""
    ann = (c.get("annotation") or "").strip()
    if not ann:
        return 0
    w = width if width is not None else _chart_width(c)
    # Full-width cards get the full capacity; narrower ones scale with the
    # column span (a w=6 annotation block holds ~half the characters).
    cpl = _TEXT_UNITS_PER_LINE if w >= 12 else max(24, _TEXT_UNITS_PER_LINE * w // 12)
    # W2 角标批注（2026-09-15）：D1 徽章顶衬退役，.tx-annot 与 .tx-card 完全同构
    # （padding 12×2 + 1px 描边×2 = chrome 26px）。地板 2 行、封顶 5 维持——地板是
    # 对角角线（28px）+ 一行正文的最小可读余量，封顶防长口径注释淹没下一张卡。
    px = 26 + _text_lines(ann, cpl) * _TEXT_LINE_PX
    return _text_rows(px, 2, 5)


def _text_pseudo_items(texts: list[dict] | None) -> list[dict]:
    """texts 声明 → 排版伪卡（viewType="text"，2026-09-28「全部放开」批）。

    伪卡**只进排版管道**（layout_sections/pack_layout → placed），绝不进
    manifest.charts——编辑器 chips/审计 per-chart/pptx 图表枚举等服务端管道
    从不看见它们；渲染端按 viewType 分支成段模型文本卡。宽度恒整行（12），
    layout 只放行 x/y/h 钉位；高度 = _text_band_h 行数预算（地板 2、封顶 12，
    标题占独立一行不计入正文换行容量）。"""
    out: list[dict] = []
    for i, t in enumerate(texts or []):
        tid = t.get("id") or f"text-{i + 1}"
        title = str(t.get("title") or "")
        body = str(t.get("text") or "")
        style = dict(t.get("style") or {})
        combined = (title + "\n" + body) if title and body else (title or body)
        th = _text_band_h(combined, 2, 12, title_px=24)
        lay = dict(t.get("layout") or {})
        lay.pop("w", None)
        lay["w"] = 12
        if "h" not in lay:
            lay["h"] = th
        out.append({"id": tid, "viewType": "text", "layout": lay,
                    "title": title, "text": body,
                    # kind=narrative 让 _band_paragraphs 走「h2 标题 + body 正文」
                    # 分支——缺省分支是「本节小结」，文本卡会顶着一个错误的标题。
                    "paragraphs": _band_paragraphs({"kind": "narrative", "title": title, "narrative": body}),
                    "textKind": style.get("textStyle") or "",
                    "cardColor": style.get("cardColor") or "default",
                    "_after": t.get("after") or "", "_textH": th})
    return out


def _merge_text_pseudos(charts: list[dict], pseudos: list[dict]) -> list[dict]:
    """把 texts 伪卡 splice 进排版列表：`after` 命中图表 id → 紧随其后；否则
    追加在末尾（body 流的最后）。声明顺序在伪卡之间保持稳定。"""
    merged = list(charts)
    for p in pseudos:
        after = p.get("_after")
        idx = None
        if after:
            for i, c in enumerate(merged):
                if c.get("id") == after:
                    idx = i + 1
                    break
        merged.insert(idx if idx is not None else len(merged), p)
    return merged


def _text_block_article(c: dict, x: int, y: int) -> str:
    """独立文本卡 article（free texts）。段模型 + 统一卡样式（tx-card /
    txc-* 色板 / 批注形 tx-annot），与批注、叙述带同一 data-text-block 管线，
    编辑器的段落编辑/自动高度原样可用。"""
    kind, color = _resolve_card_style(c.get("textKind") or None, c.get("cardColor") or None, "text")
    cls = _card_classes(kind, color)
    return (f'<article class="grid-stack-item" data-report-block="text" data-report-block-id="block-{_html.escape(str(c.get("id", "")), quote=True)}" '
            f'gs-x="{x}" gs-y="{y}" gs-w="12" gs-h="{int((c.get("layout") or {}).get("h") or 3)}">'
            f'<div class="grid-stack-item-content"><div class="block {cls}" data-text-block>'
            f'{_paragraphs_html(c.get("paragraphs") or [])}</div></div></article>')


def _flow(placed: list, bands: list) -> list:
    """Merge chart placements and section text bands into ascending-y order.

    ``placed`` items are (chart, x, y); ``bands`` are layout dicts with a ``y``.
    Returns steps of ("chart", chart, x, y) | ("band", band) so the DOM reads
    the same top-to-bottom story as the grid. Chart y is non-decreasing under
    pack_layout, and bands always sit strictly between chart groups, so the sort
    is stable and lossless."""
    steps: list = [("chart", c, x, y) for c, x, y in placed]
    for b in bands:
        steps.append(("band", b))
    steps.sort(key=lambda step: step[3] if step[0] == "chart" else step[1]["y"])
    return steps


def layout_sections(charts: list[dict], sections: list[dict], start_y: int) -> tuple:
    """Section-aware 12-col packing.

    Walks each section in order: a full-width narrative band, then that
    section's charts (row-composed by ``pack_layout``), then an insight band;
    unassigned charts pack last (implicit body). A section with exactly ONE
    non-solo chart and a short insight renders the insight as a 4-col sidecar
    beside the 8-col chart (chart+text combo row) instead of a full-width
    band. Without sections this is exactly ``pack_layout(charts, start_y)``
    with empty bands (backward compatible: legacy geometry is unchanged)."""
    from layout_solver import solve_layout, width_class
    placed: list = []
    bands: list = []
    y = int(start_y)
    by_id = {c.get("id"): c for c in charts}
    used: set = set()

    def pack_group(group: list[dict]) -> None:
        nonlocal y
        if (len(group) == 1
                and (group[0].get("annotation") or "").strip()
                and group[0].get("annotationPlacement") == "right"
                and width_class(group[0]) != "solo"
                and not ("x" in (group[0].get("layout") or {}) and "y" in (group[0].get("layout") or {}))):
            pack_group_with_annotation_sidecar(group[0])
            return
        for item in pack_layout(group, start_y=y):
            placed.append(item)
            y = max(y, item[2] + _block_h(item[0]))
            _emit_annotation_band(item)

    def pack_group_with_annotation_sidecar(c: dict) -> None:
        nonlocal y
        authored_h = (c.get("layout") or {}).get("h")
        solver_h = int(solve_layout(c, width=8)["h"])
        h = max(int(authored_h), solver_h) if authored_h is not None else solver_h
        cc = dict(c)
        lay = dict(c.get("layout") or {})
        lay["w"], lay["h"] = 8, h
        cc["layout"] = lay
        placed.append((cc, 0, y))
        band = {"kind": "annotation", "chart": c.get("id", ""), "annotation": c.get("annotation", ""),
                "x": 8, "y": y, "w": 4, "h": h, "textKind": "", "style": {}}
        band["paragraphs"] = _band_paragraphs(band)
        bands.append(band)
        y += h

    def pack_group_with_sidecar(group: list[dict], insight: str, sec: dict, style: dict) -> None:
        """Chart + insight combo: a single non-solo chart takes 8 cols and the
        section insight rides beside it as a 4-col sidecar (same height)."""
        nonlocal y
        c = group[0]
        authored_h = (c.get("layout") or {}).get("h")
        solver_h = int(solve_layout(c, width=8)["h"])
        h = max(int(authored_h), solver_h) if authored_h is not None else solver_h
        cc = dict(c)  # shallow copy: never mutate the caller's chart dict
        lay = dict(c.get("layout") or {})
        lay["w"], lay["h"] = 8, h
        cc["layout"] = lay
        placed.append((cc, 0, y))
        _emit_annotation_band((cc, 0, y))
        band = {"kind": "insight", "section": sec.get("id", ""), "insight": insight,
                "x": 8, "y": y, "w": 4, "h": h,
                "textKind": sec.get("textKind") or "", "style": style}
        band["paragraphs"] = _band_paragraphs(band)
        bands.append(band)
        y += h

    def _emit_annotation_band(item: tuple) -> None:
        """B4 migration: a chart carrying ``chart.annotation`` emits its note as
        an independent transparent text block directly below the chart (the
        runtime addAnnotationBlock 'below' placement), and the section's y cursor
        is advanced past it so later cards never overlap."""
        nonlocal y
        c, x, gy = item
        ann = (c.get("annotation") or "").strip()
        if not ann:
            return
        ch = int((c.get("layout") or {}).get("h") or _block_h(c))
        w = _chart_width(c)
        ah = _annotation_block_h(c)
        band = {"kind": "annotation", "chart": c.get("id", ""), "annotation": ann,
                "x": x, "y": gy + ch, "w": w, "h": ah,
                "textKind": "", "style": {}}
        band["paragraphs"] = _band_paragraphs(band)
        bands.append(band)
        y = max(y, gy + ch + ah)


    for sec in sections or []:
        cids = list(sec.get("chartIds") or [])
        group = [by_id[cid] for cid in cids if cid in by_id]
        used.update(c["id"] for c in group)
        title = sec.get("title") or ""
        narrative = sec.get("narrative") or ""
        text_kind = sec.get("textKind") or ""
        style = sec.get("style") or {}
        if title or narrative or text_kind == "divider":
            if text_kind == "divider" and not narrative:
                h = 1
            else:
                combined = (title + "\n" + narrative) if (title and narrative) else (title or narrative)
                h = _text_band_h(combined, 2, 10, title_px=24)
            band = {"kind": "narrative", "section": sec.get("id", ""), "title": title,
                    "narrative": narrative, "x": 0, "y": y, "w": 12, "h": h,
                    "textKind": text_kind, "style": style}
            band["paragraphs"] = _band_paragraphs(band)
            bands.append(band)
            y += h
        insight = sec.get("insight") or ""
        # Sidecar (chart 8 + insight 4) only when the combo can actually fit:
        # exactly one non-solo chart, no authored position, and a short insight
        # (a 4-col column fits roughly 8 chars/line/row of height).
        sidecar = (
            insight
            and len(group) == 1
            and width_class(group[0]) != "solo"
            and not ("x" in (group[0].get("layout") or {}) and "y" in (group[0].get("layout") or {}))
            and len(insight) <= 160
        )
        if sidecar:
            pack_group_with_sidecar(group, insight, sec, style)
            continue
        pack_group(group)
        if insight:
            h = _text_band_h(insight, 2, 6, line_px=22, title_px=20)
            band = {"kind": "insight", "section": sec.get("id", ""), "insight": insight,
                    "x": 0, "y": y, "w": 12, "h": h,
                    "textKind": sec.get("textKind") or "", "style": style}
            band["paragraphs"] = _band_paragraphs(band)
            bands.append(band)
            y += h
    pack_group([c for c in charts if c.get("id") not in used])
    return placed, bands


def _filter_cardinality(spec: dict) -> list[str]:
    """Truncate high-cardinality comparison charts to top-N (>=12 categories)
    so bars/lines stay legible; record the truncation for provenance.

    An AUTHORED topK/limit (explicit TOP-N intent) opts out — the author's K is
    honoured at every layer instead of being overridden by this default 12."""
    notes = []
    for c in spec.get("charts", []):
        vt = c.get("viewType")
        if vt not in ("bar", "stackedBar", "line"):
            continue
        authored_topk = int(c.get("topK") or 0)
        if authored_topk:
            continue
        data = c.get("data") or {}
        cols = data.get("columns") or []
        rows = data.get("rows") or []
        measures = c.get("measures") or []
        if not rows or not measures or measures[0] not in cols:
            continue
        if len(rows) <= 12:
            continue
        mi = cols.index(measures[0])
        # A temporal dimension (date/month/year) must keep its chronology —
        # truncating by value would jumble the trend. Keep the full series and
        # let the runtime dataZoom navigate; for non-temporal (nominal) dims do
        # the top-N-by-value truncation that keeps bars legible.
        dim = c.get("dimension") or cols[0]
        if _dimension_type(c) == "time":
            notes.append(f"{c.get('id')}: time dimension kept ordered (dataZoom for {len(rows)}) — no value-truncation")
            continue
        di = cols.index(dim)
        bd = (c.get("roles") or {}).get("breakdown")
        if bd and bd in cols:
            # 交叉图（维度 × breakdown）：top-K 的单位是**主维度标签**（入选标签
            # 保留全部 breakdown 行），不是聚合单元格——按单元格切片会把同一标签
            # 的系列砍掉一部分，堆叠合计/图例失真，且首屏（残图）与运行时重算
            # （按标签整组保留）不一致，WYSIWYG 破裂。对齐 runtime applyTopK
            # hasBd 分支的语义：按主维度标签聚合总值排名，>12 个标签才截。
            totals: dict = {}
            for r in rows:
                lab = r[di] if di < len(r) else None
                totals[lab] = totals.get(lab, 0.0) + _num(r[mi]) if di < len(r) else totals.get(lab, 0.0)
            if len(totals) <= 12:
                continue
            keep = {lab for lab, _v in sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:12]}
            data["rows"] = [r for r in rows if r[di] in keep]
            c["topK"] = 12
            c["truncation"] = {"shown": len(keep), "total": len(totals), "auto": True}
            notes.append(f"{c.get('id')}: top-12 labels by {measures[0]} kept with all {bd} rows (auto-truncated from {len(rows)} cells / {len(totals)} labels)")
            continue
        top = sorted(rows, key=lambda r: _num(r[mi]), reverse=True)[:12]
        data["rows"] = top
        # Write the cap back onto the chart so editor.data.topK carries it into
        # the runtime (adapter caps live bars/stacked bars the same way the
        # static option does — otherwise the live view shows ALL categories
        # while the authored/exported option shows 12: the "Top N 卡片展示全部"
        # mismatch). The runtime TOP K selector lets a reader raise/lower it.
        c["topK"] = 12
        # Record the truncation as a FACT on the chart. The reader must be told
        # that 12 of N categories are shown — a silent Top-N makes them believe
        # the 12 are everything (report distortion). The runtime renders this
        # when the chart still uses the authored option (editor.data untouched);
        # once the reader/editor re-aggregates, the live count supersedes it.
        _labels = {str(r[cols.index(c.get("dimension") or cols[0])]) for r in rows
                   if len(r) > cols.index(c.get("dimension") or cols[0])}
        c["truncation"] = {"shown": 12, "total": len(_labels), "auto": True}
        notes.append(f"{c.get('id')}: top-12 by {measures[0]} (auto-truncated from {len(rows)})")
    if notes:
        prov = spec.setdefault("provenance", {})
        prov.setdefault("calculations", []).extend(notes)
    return notes

def _filter_h(filter_cols: int) -> int:
    """Grid height for the report filter block.

    P1 redesign (repo repo docs/semantic-config-and-derived-redesign.md §9): the filter
    card is now an *optional* surface — when no filters are configured the
    block is omitted entirely (``height = 0``) and the report's first chart
    row gains back the space. When at least one filter is active the card
    stays a compact 1-row strip (28px controls, nowrap, horizontal scroll)
    because the chip+select row never needs more than one row of vertical
    space. The toolbar's "筛选 ▾" popover handles editing/adding filters, so
    the in-flow card has nothing extra to show.

    Returns 0 when ``filter_cols == 0`` (caller should skip rendering the
    block entirely — see ``render_grid_html`` / ``render_tabs_html`` for the
    conditional append). Returns 1 otherwise.

    Note: server mode imports this same helper, so the height stays in lock
    step across singlefile and server (see ``server/template.py``).
    """
    cols = max(0, int(filter_cols or 0))
    if cols == 0:
        return 0
    if cols <= 2:
        return 2
    if cols <= 4:
        return 3
    return 5


def _is_free(spec: dict) -> bool:
    """free-v1 档判定（docs/free-authoring-discussion.md §2）。

    自由模式 = 声明即全部：编译器不做任何「替作者做主」的填充——封面只在显式
    声明 spec.cover 时存在、KPI 条不自动垫满、筛选带不自动出候选。聚合/布局/
    主题/审计这些引擎能力不受影响（它们不是模板）。"""
    return spec.get("templateId") == "free-v1"


def _cover_h(spec: dict, asof: str | None = None) -> int:
    if _is_free(spec) and not (spec.get("cover") or {}):
        return 0
    cov = spec.get("cover") or {}
    # C3 数据截至（2026-09-25）：封面副标题下的「数据截至」行（~17px）也占卡内容。
    # asof 显式参数给编译期（此时 meta/cover.dataAsOf 还没烘）；server 重渲染
    #（template._render_body）不传参，读 manifest.cover 上已投影的 dataAsOf——
    # 两条路径对同一封面算出同一行数，几何不漂。
    if not asof:
        asof = str(cov.get("dataAsOf") or "").strip() or None
    summary = cov.get("summary") or ""
    # Cover content (28px title + meta chips + up to 2-line summary + 32px
    # vertical padding) is ~185–210px. Grid cellHeight is 48px + 8px margin,
    # so 4 rows gives ~216px usable — enough for the content with a little
    # headroom. Long summaries (3+ lines) need one more row, and so does any
    # demoted meta lead line (over-_COVER_CHIP_MAX meta items render as extra
    # summary lines via _cover_chips_lead but are not part of ``summary``).
    has_lead = any(len(str(m).strip()) > _COVER_CHIP_MAX for m in (cov.get("meta") or []))
    if len(summary) > 120 or has_lead:
        return 5
    # 数据截至行只给紧凑 4 行档加一行余量（5 行档本身有足够 headroom）。
    if asof:
        return 5
    return 4

# ---------- chart compilation ----------

def _dimension_type(c: dict) -> str:
    """Heuristic dimension semantic: 'time' | 'ordinal' | 'category'.

    Guides default sort so a temporal/ordinal axis keeps its natural order
    instead of being re-sorted by value (which would jumble a date axis or a
    satisfaction scale). Nominal dimensions keep the R3 value-desc default.
    """
    # A declared quantitative measure is never a time axis (mirrors JS
    # dimensionKind). Without this, a count column whose values happen to be
    # 4-digit numbers (Sell In = 1902) is matched by the bare-year heuristic and
    # classified as a date.
    _mt = c.get("measureTypes") or {}
    _dim_mt = _mt.get(c.get("dimension") or "")
    if isinstance(_dim_mt, dict) and _dim_mt.get("type"):
        if _dim_mt["type"] in ("count", "number", "unitless", "currency", "price", "percent"):
            return "category"
    elif isinstance(_dim_mt, str) and _dim_mt in ("count", "number", "unitless", "currency", "price", "percent"):
        return "category"
    dim = (c.get("dimension") or ((c.get("data") or {}).get("columns") or [""])[0] or "")
    rows = (c.get("data") or {}).get("rows") or []
    name = str(dim)
    name_hits_time = bool(re.search(r"日期|时间|date|day|month|year", name, re.I))
    if name_hits_time and not rows:
        # 无行数据可佐证时名字命中即 time（服务端 schema-only 形状）。
        return "time"
    rows = (c.get("data") or {}).get("rows") or []
    # Date-like VALUES (yyyy / yyyy-mm / yyyy年m月) trump an ordinal keyword in
    # the column name: '入职月份' must be a time axis, not a jumbled ordinal.
    # 名字命中时间关键词也必须值域佐证（≥阈值可解析才 time）：'Month' +
    # Jan..Dec 这类文本月份列按 time 处理会让 _time_sort_key 全落字符串档、
    # 首屏就排出字母序乱轴。
    if rows:
        cols = (c.get("data") or {}).get("columns") or []
        ci = cols.index(dim) if dim in cols else 0
        vals = [_strip_float_zero(r[ci]) for r in rows if str(r[ci]).strip()]
        # A bare 4-digit number is only a year when the column name carries a
        # year hint (年份/年/year); otherwise 1902 (a count) is not a date.
        # Uses the shared parser, so every supported spelling counts as temporal
        # (2026/9/5, 2026.9.5, 2026年9月5日, 20260905, 202501.0, …) instead of the
        # old `\d{4}[-/年]\d{1,2}` regex which missed several of them.
        has_year_hint = bool(re.search(r"年|年份|year", name, re.I))
        date_like = 0
        for v in vals:
            g = date_parse.grain(v)
            if g in ("month", "day", "quarter", "week") or (g == "year" and has_year_hint):
                date_like += 1
        if vals and date_like / len(vals) > 0.6:
            return "time"
        if name_hits_time and not vals:
            return "time"
    if re.search(r"月|段|级|季|周|年|评分|分", name):
        return "ordinal"
    return "category"

def _strip_float_zero(v: str) -> str:
    """Remove a trailing '.0' float suffix from a numeric string token.

    CSV/Excel parse an integer column (e.g. YearMonth = 202501) as a float
    (202501.0). str() yields '202501.0'. We must strip the '.0' as a WHOLE token
    — str.rstrip('.0') strips the character CLASS {'.','0'} and corrupts a value
    like 2020 → '202'. Only drop a literal trailing '.0'."""
    s = str(v)
    if s.endswith(".0"):
        return s[:-2]
    return s


def _fmt_time_label(v, bucket: str) -> str | None:
    """Format a temporal value to a bucketed label, or None when not parseable.

    Delegates to ``date_parse`` (the shared parser) so EVERY supported spelling —
    ``2026/9/5``, ``2026.9.5``, ``2026年9月5日``, ``20260905``, ``202501.0``,
    ``2026-09-05T00:00:00`` — formats identically to the runtime's ``bucketLabel``
    (WYSIWYG) and to the server's SQL bucketing. An unrecognized value returns
    None so the caller can fall back to the raw token."""
    return date_parse.label(v, bucket)


def _apply_static_topn(chart: dict, rows: list, cols: list, dim: str) -> list:
    """Cap dimension-led static rows to the authored TOP-N (chart topK/limit).

    Mirrors the runtime's dimension-level Top-K (adapter.applyTopK + the table
    slice): keep the K dimension labels with the largest first-measure total and
    retain ALL their rows (so a stacked/grouped breakdown keeps every series of
    a kept label). Never applied to a time/ordinal axis (trends are not Top-N)
    and only to views that can rank a nominal dimension."""
    if not rows or not cols or dim not in cols:
        return rows
    # 只信显式 topK 声明：chart["limit"] 是编译产物盖章的 encoding.limit 缺省
    #（500），回落它会让每个未声明 TOP-N 的图被烙上 topK=500 + truncation
    #（与 2024 行同一条判据，那边已修，这里是同批残留点）。
    k = int(chart.get("topK") or 0)
    if k <= 0:
        return rows
    vt = chart.get("viewType")
    if vt not in ("bar", "stackedBar", "table") or _dimension_type(chart) in ("time", "ordinal"):
        return rows
    di = cols.index(dim)
    measures = [m for m in (chart.get("measures") or []) if m and m != COUNT_MEASURE] or None
    mi = None
    if measures:
        real = [m for m in measures if m in cols]
        if real:
            mi = cols.index(real[0])
    if mi is None:
        # Synthetic row-count ("*"): value lives in the 记录数 display column.
        if COUNT_MEASURE in (chart.get("measures") or []):
            if COUNT_MEASURE_LABEL in cols:
                mi = cols.index(COUNT_MEASURE_LABEL)
    if mi is None:
        return rows
    total: dict[str, float] = {}
    order: list[str] = []
    for row in rows:
        if len(row) <= max(di, mi):
            continue
        label = str(row[di])
        try:
            v = float(row[mi])
        except (TypeError, ValueError):
            v = 0.0
        if label not in total:
            total[label] = 0.0
            order.append(label)
        total[label] += v
    if len(order) <= k:
        return rows
    top = sorted(order, key=lambda lab: (-total[lab], order.index(lab)))[:k]
    kept = set(top)
    # 作者 TOP-N 也是截断：把「显示了 K 项 / 共 N 项」记为事实（读者可见）。
    chart["truncation"] = {"shown": k, "total": len(order), "authored": True}
    kept_rows = [r for r in rows if len(r) > di and str(r[di]) in kept]
    # BI-H3：作者声明 topKOther → 尾部聚合成「其他（N 项）」行排最后（与运行时
    # adapter.applyTopK / 服务端 aggregator 同语义）。验收修订（2026-09-13）：
    # 按**指标逐列**判定可加性（图表级聚合 + 逐指标 aggregations 覆盖）——可加列
    # （sum/count）求和进「其他」行；不可加列保持空缺（不写假数）。多指标只要
    # 有一列可加就合并（旧 gate 是全有或全无，一列平均整个开关静默失效）。
    if not chart.get("topKOther"):
        return kept_rows
    tail = [r for r in rows if len(r) > di and str(r[di]) not in kept]
    if not tail:
        return kept_rows
    omitted = len(order) - k
    other_label = f"其他（{omitted} 项）"
    m_idx = [cols.index(m) for m in (chart.get("measures") or []) if m in cols]
    if not m_idx and mi is not None:
        m_idx = [mi]
    aggs_map = chart.get("aggregations") or {}
    chart_agg = chart.get("aggregation", "sum")

    def _agg_of(j: int) -> str:
        a = aggs_map.get(cols[j], chart_agg)
        return a if isinstance(a, str) else "sum"

    def _col_non_additive(j: int) -> bool:
        # 与运行时 adapter.applyTopK 的 addMask 同一门（chart-adapter.js：聚合
        # ∈ {sum,count} 且 !nonAdditiveMeasure）。percent/unitless 语义类型或
        # 率/占比/rate/ratio/percent 名义的列**不可加**——把尾部占比求和是假数
        # （7.3% + 6.1% ≠ 13.4% 的口径），运行时重聚合会把它置 null；编译侧
        # 不同门，首屏与编辑态就在同一格分裂。
        col = cols[j]
        mt = (chart.get("measureTypes") or {}).get(col)
        t = mt.get("type") if isinstance(mt, dict) else mt
        if t in ("percent", "unitless", "price"):
            return True
        return bool(re.search(r"率|占比|百分比|比例|%.*%|rate|ratio|percent", str(col), re.I))

    add_idx = [j for j in m_idx if _agg_of(j) in ("sum", "count") and not _col_non_additive(j)]
    if not add_idx:
        # 没有任何可加列：合并会给全空的「其他」行 —— 回退纯截断。
        return kept_rows
    other_cols = [j for j in range(len(cols)) if j != di and j not in m_idx]

    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0
    if len(other_cols) == 1:
        # breakdown 列：按 breakdown 值各补一行（保留交叉结构）。
        bj = other_cols[0]
        by_bd: dict[str, list] = {}
        bd_order: list[str] = []
        for r in tail:
            bv = str(r[bj]) if len(r) > bj else ""
            if bv not in by_bd:
                by_bd[bv] = [0.0] * len(m_idx)
                bd_order.append(bv)
            for pos, j in enumerate(m_idx):
                if j not in add_idx or len(r) <= j:
                    continue
                by_bd[bv][pos] += _num(r[j])
        for bv in bd_order:
            row = [""] * len(cols)
            row[di] = other_label
            row[bj] = bv
            for pos, j in enumerate(m_idx):
                row[j] = by_bd[bv][pos] if j in add_idx else None
            kept_rows.append(row)
    else:
        row = [""] * len(cols)
        row[di] = other_label
        for j in m_idx:
            row[j] = (sum(_num(r[j]) for r in tail if len(r) > j)
                      if j in add_idx else None)
        kept_rows.append(row)
    if any(j not in add_idx for j in m_idx):
        # 有列保持空缺（不可加）——登记事实，让说明可以如实告知读者。
        chart["truncation"]["mergedPartial"] = True
    chart["truncation"]["merged"] = True
    return kept_rows


def _bucketed_table(table: dict, dim: str, bucket: str) -> dict | None:
    """Raw table with the time column's values replaced by bucket labels.

    Lets ``_deterministic_rows`` group directly at the bucket grain for the
    NON-ADDITIVE aggregations (average/countd), which cannot be re-bucketed
    from pre-aggregated rows (a mean of daily means weights every day equally;
    a sum of daily distinct-counts double-counts). Unparseable labels keep the
    raw token — the same fallback ``_bucket_static_rows`` uses."""
    cols = table.get("columns") or []
    if not dim or dim not in cols:
        return None
    ci = cols.index(dim)
    out_rows: list[list] = []
    for r in table.get("rows") or []:
        if len(r) <= ci:
            continue
        label = _fmt_time_label(r[ci], bucket)
        if label is None and (r[ci] is None or str(r[ci]).strip() == ""):
            # 空值保持空（服务端 NULL 分组同款），不渲染成 'None' 标签。
            label = r[ci]
        nr = list(r)
        nr[ci] = label if label is not None else str(r[ci])
        out_rows.append(nr)
    return {"columns": cols, "rows": out_rows}


def _bucket_static_rows(chart: dict, rows: list, cols: list, dim: str, bucket: str,
                        raw_table: dict | None = None, filters: Any = None) -> list:
    """Re-aggregate pre-built static rows to the same time grain the runtime
    uses, so a raw daily Date column bucketed to month becomes 12 monthly points
    instead of 365 rows with repeated month labels."""
    if not rows or bucket in (None, "raw"):
        return rows
    agg = chart.get("aggregation", "sum")
    measures = list(chart.get("measures") or [])
    if dim not in cols or not measures:
        return rows
    # 不可加聚合不能从预聚合行重分桶（average = 日均值的无权平均、countd =
    # 每日去重数求和，都是假数）：有原始表时把时间列替换成桶标签，让
    # _deterministic_rows（唯一聚合实现）直接在目标粒度上重算。
    if agg in ("average", "countd") and raw_table:
        bt = _bucketed_table(raw_table, dim, bucket)
        if bt is not None and bt.get("rows"):
            bd = (chart.get("roles") or {}).get("breakdown")
            _ms = [m for m in measures if m in cols or m == COUNT_MEASURE]
            _max_ms = len(cols) - 1 - (1 if bd and bd in cols else 0)
            try:
                return _deterministic_rows(bt, dim, _ms[:max(1, _max_ms)], agg, bd,
                                           filters, limit=5000, sort_and_limit=False)
            except ValueError:
                pass
    di = cols.index(dim)
    # 交叉列（roles.breakdown）必须留在分组键里：只按时间标签分组会把不同
    # breakdown 值的行合并求和、breakdown 列落 None（静态 option 退化成一条
    # 名为「None」的系列），与运行时 per-breakdown 系列的 WYSIWYG 口径分裂。
    bd = (chart.get("roles") or {}).get("breakdown")
    bdi = cols.index(bd) if bd in cols else None
    grouped: dict[tuple, list[list]] = {}
    for row in rows:
        if len(row) <= di:
            continue
        label = _fmt_time_label(row[di], bucket) or str(row[di])
        bd_val = row[bdi] if (bdi is not None and len(row) > bdi) else None
        grouped.setdefault((label, bd_val), []).append(row)
    out = []
    for (label, bd_val), group in grouped.items():
        new = [None] * len(cols)
        new[di] = label
        if bdi is not None:
            new[bdi] = bd_val
        first = group[0]
        for ci, col in enumerate(cols):
            if ci == di or col not in measures:
                continue
            new[ci] = first[ci] if len(first) > ci else None
        derived_names = set(chart.get("derivedMeasures") or [])
        for measure in measures:
            # An aggregate-level DERIVED measure is already evaluated at the output
            # grain (by _rows_with_derived) and is not additive — re-aggregating it
            # here would both sum a rate and turn its empty periods into 0.
            if measure in derived_names:
                continue
            # Resolve the value column: synthetic "*" (row-count) reads the display
            # column 记录数; a real measure reads its own column.
            if measure == COUNT_MEASURE:
                mi = cols.index(COUNT_MEASURE_LABEL) if COUNT_MEASURE_LABEL in cols else (len(cols) - 1)
            elif measure in cols:
                mi = cols.index(measure)
            else:
                continue
            vals = []
            for r in group:
                try:
                    vals.append(float(r[mi]))
                except (TypeError, ValueError, IndexError):
                    pass
            # Rows here are PRE-AGGREGATED per the raw grain (e.g. per-day counts).
            # Re-bucketing to a coarser grain must SUM the per-grain values for both
            # sum and count (a month total of daily counts = sum of the counts), NOT
            # count the rows in the bucket (= 1 per pre-aggregated row).
            if agg in ("sum", "count"):
                value = sum(vals) if vals else 0
            elif agg == "min":
                value = min(vals) if vals else 0
            elif agg == "max":
                value = max(vals) if vals else 0
            elif agg == "average":
                value = sum(vals) / len(vals) if vals else 0
            else:
                value = sum(vals) if vals else 0
            new[mi] = value
        out.append(new)
    return out


def _infer_time_bucket(chart: dict, raw_cols: list, dim: str) -> str | None:
    """Infer a month bucket for a RAW time dimension whose values are date-like.

    A raw ``YearMonth`` column (values 202501…) is already at month grain; without
    a dimensionBucket the runtime renders the raw float ("202501.0") instead of a
    bucketed "2025-01". Default a YYYYMM / yyyy-mm / yyyy-mm-dd time dimension to
    month so it groups + labels by month (both sides agree)."""
    if _dimension_type(chart) != "time":
        return None
    data = chart.get("data") or {}
    rows = data.get("rows") or []
    if not dim or not rows:
        return None
    # The chart's rows are in the chart's own column order (dimension-first),
    # NOT raw_cols order. Locate the dimension index in chart.data.columns, and
    # only proceed when the column is a real raw column (so it groups by grain).
    chart_cols = data.get("columns") or []
    if dim not in chart_cols or dim not in raw_cols:
        return None
    ci = chart_cols.index(dim)
    vals = [_strip_float_zero(r[ci]) for r in rows if len(r) > ci and str(r[ci]).strip()]
    if not vals:
        return None
    # Only trigger when the values are clearly a year-month / date (YYYYMM,
    # yyyy-mm, yyyy-mm-dd, or a bare yyyy that has a year hint). Numeric counts
    # (1902, 2024) must NOT bucket — but a bare yyyy in a 年份 column is legit.
    # Grain from the shared parser: day precision dominates → day bucket (a
    # 2026/9/5 column must not collapse to months); month/day mixed → month;
    # bare yyyy only with a year-hint column name. Counts (1902) never bucket.
    # ``_strip_float_zero`` already normalized "20260905.0"-style tokens.
    return date_parse.infer_grain(vals, dim)


def _infer_bucket_from_chart(c: dict, dim: str) -> str | None:
    """Grain of a RAW time dimension, inferred from the chart's OWN rows.

    ``_infer_time_bucket`` needs the raw table columns; when build_manifest runs
    without a raw table (the current single-table/server path) the chart's static
    rows still carry the dimension's real values (one per group), so the same
    shared parser gives the same grain. Emitting a ``dimensionBucket`` is what
    makes the runtime label/group by month (YearMonth 202501 → 2025-01) instead of
    rendering the raw value, and it is REQUIRED by a window derived measure
    (which must know the chart's grain to compare the right periods).
    """
    data = c.get("data") or {}
    cols = data.get("columns") or []
    if not dim or dim not in cols or _dimension_type(c) != "time":
        return None
    ci = cols.index(dim)
    vals = [_strip_float_zero(r[ci]) for r in (data.get("rows") or [])
            if len(r) > ci and str(r[ci]).strip()]
    return date_parse.infer_grain(vals, dim) if vals else None


def _sorted_rows(c: dict):
    """Return rows sorted per c['sort']; desc sorts by first numeric measure.

    Default sort is inferred from dimension semantics: time/ordinal dimensions
    keep their authored (natural) order, nominal dimensions sort by value desc.
    """
    data = c.get("data") or {}
    rows = [list(r) for r in data.get("rows", [])]
    sort = c.get("sort")
    if sort is None:
        sort = "none" if _dimension_type(c) in ("time", "ordinal") else "desc"
    measures = c.get("measures") or []
    cols = data.get("columns") or []
    if sort == "desc" and measures and cols:
        if measures[0] in cols:
            mi = cols.index(measures[0])
        elif measures[0] == COUNT_MEASURE and COUNT_MEASURE_LABEL in cols:
            # 合成行数列（"*"）读「记录数」展示列——与 _apply_static_topn 同一
            # 回退；旧版落 mi=1，breakdown 形状 [dim, bd, 记录数] 上那是**拆分
            # 维度列**，静态行按 bd 值降序、x 轴组间交错。
            mi = cols.index(COUNT_MEASURE_LABEL)
        else:
            mi = 1
        if mi < 0: mi = 1
        try:
            rows.sort(key=lambda r: -_num(r[mi]))
        except Exception:
            pass
    if sort == "asc" and rows and cols:
        # value-asc on the dimension label — the SAME key _chart_manifest_entry
        # uses for option.rows (str(label)), so the static table's first paint
        # agrees with the runtime re-render instead of staying in the
        # _deterministic_rows measure-desc order.
        di = cols.index(c.get("dimension") or cols[0]) if (c.get("dimension") or cols[0]) in cols else 0
        try:
            rows.sort(key=lambda r: (str(r[di]) if di < len(r) else ""))
        except Exception:
            pass
    # A time/ordinal axis must keep its authored chronology. _deterministic_rows
    # sorts desc by the first measure, which would jumble a date trend; fix the
    # chronological order here so the static option matches the runtime aggregation
    # (the runtime re-sorts a temporal dimension by its label ascending).
    if sort in ("time", "none") and dim_type_is_time(c) and rows and cols:
        di = cols.index(c.get("dimension") or cols[0]) if (c.get("dimension") or cols[0]) in cols else 0
        try:
            rows.sort(key=lambda r: _time_sort_key(r[di]))
        except Exception:
            pass
    return rows

def dim_type_is_time(c: dict) -> bool:
    """True when the chart's primary dimension is a time axis."""
    return _dimension_type(c) == "time"


def _time_sort_key(v: Any):
    """Sort key that orders a temporal value chronologically.

    Delegates to ``date_parse.sort_key``. Raw strings used to be compared as-is,
    which only worked for equal-width zero-padded spellings: a ``2026/6/1``
    column sorted as 2026/6/1 < 2026/6/10 < 2026/6/2 and the compiled trend axis
    came out jumbled (a real company report)."""
    return date_parse.sort_key(v)


COUNT_MEASURE = "*"          # synthetic row-count measure (SQL COUNT(*))
COUNT_MEASURE_LABEL = "记录数"


def _measure_name(m: str) -> str:
    """Display name for a measure; the row-count token renders as 记录数."""
    return COUNT_MEASURE_LABEL if m == COUNT_MEASURE else m


def _measure_sem_label(c: dict, measure: str) -> str | None:
    """Resolve a measure's Flint semantic label from chart measureTypes.
    Returns a Flint semantic type (Revenue/Percentage/Count/...) or None.
    委托 measure_semantics.semantic_label_of（声明 semantic > type > 名称关键词，
    价族→Price/金额族→Revenue 的拆分在那一处维护——2026-10-13 批）。"""
    from measure_semantics import semantic_label_of
    mt = (c.get("measureTypes") or {}).get(measure)
    if mt is None and isinstance(c.get("encoding"), dict):
        # encoding 级 measureTypes 覆盖优先（与 _used chart_mt 同一优先序）
        mt = (c["encoding"].get("measureTypes") or {}).get(measure)
    return semantic_label_of(mt, measure)


def _flint_value_axis(c: dict, measure: str, mark: str, values: list, base: dict | None = None) -> dict:
    """ECharts value axis aligned to the moved-in Flint zero-baseline logic.
    zero:true → scale:false (include 0); zero:false → scale:true + padded domain.
    Delegates the decision to the canonical ``flint_core.zero_decision`` so the
    Python compile and the JS runtime share one source of truth (parity-tested)."""
    dec = fc.zero_decision(_measure_sem_label(c, measure), mark, values)
    axis = base or {"type": "value"}
    if dec["zero"]:
        axis["scale"] = False
        axis.pop("min", None)
        axis.pop("max", None)
    else:
        axis["scale"] = True
        padded = fc.padded_domain(values, dec["pad"])
        if padded:
            axis["min"], axis["max"] = padded
    return axis


def _series_from_rows(c: dict, rows: list) -> list[dict]:
    """Build ECharts series (bar/line/funnel/pie) from dimension+measures."""
    data = c.get("data") or {}
    cols = data.get("columns") or []
    dim = c.get("dimension") or cols[0]
    measures = c.get("measures") or [cols[1]]
    di = cols.index(dim) if dim in cols else 0
    labels = [r[di] for r in rows]
    vt = c.get("viewType")
    if vt in ("bar", "stackedBar", "line", "area"):
        # Composite (混合图表): per-measure series type (seriesType) + Y-axis
        # (seriesAxis) override the chart base type / auto axis. Honor them here
        # so the static (authoring) option matches the runtime re-aggregation.
        st_map = c.get("seriesType") or {}
        sa_map = c.get("seriesAxis") or {}
        base_type = "line" if vt in ("line", "area") else "bar"
        any_axis1 = False
        series = []
        for j, m in enumerate(measures):
            mi = cols.index(m) if m in cols else (j + 1 if j + 1 < len(cols) else 1)
            st = st_map.get(m, base_type)
            ax = sa_map.get(m)
            s = {"name": _measure_name(m), "type": st,
                 "stack": "合计" if vt == "stackedBar" else None,
                 "data": [_series_value(c, m, r[mi]) for r in rows]}
            if vt in ("line", "area"):
                s["smooth"] = True
            if vt == "area":
                s["areaStyle"] = {}
            if ax is not None:
                s["yAxisIndex"] = int(ax)
                if int(ax) == 1:
                    any_axis1 = True
            series.append(s)
        # Dual-axis fallback: a line with measures on different scales.
        if not any_axis1 and vt == "line" and len(measures) >= 2:
            mt = c.get("measureTypes") or {}
            sigs = []
            for m in measures:
                spec = mt.get(m)
                t = spec.get("type") if isinstance(spec, dict) else spec
                div = spec.get("divisor", 1) if isinstance(spec, dict) else 1
                sigs.append((t, div))
            if len(set(sigs)) > 1:
                for j, s in enumerate(series):
                    s["yAxisIndex"] = 0 if j == 0 else 1
                any_axis1 = True
        if any_axis1:
            for s in series:
                s.setdefault("yAxisIndex", 0)
        return series
    if vt in ("pie", "donut", "funnel"):
        mi = cols.index(measures[0]) if measures and measures[0] in cols else 1
        st = "pie" if vt in ("pie", "donut") else "funnel"
        data_pts = [{"name": r[di], "value": _series_value(c, measures[0], r[mi])} for r in rows]
        # 无坐标轴图本体左移让位给右侧垂直图例（与运行时 shareOption 一致，
        # WYSIWYG）：饼/环 center 34%，漏斗 left 4% + width 58%。
        if st == "pie":
            return [{"name": _measure_name(measures[0]) if measures else "", "type": st,
                     "radius": ["45%", "72%"] if vt == "donut" else ["0%", "70%"],
                     "center": ["34%", "50%"],
                     "sort": "descending", "data": data_pts}]
        return [{"name": _measure_name(measures[0]) if measures else "", "type": st,
                 "left": "4%", "top": 12, "bottom": 12, "width": "58%",
                 "minSize": "24%", "maxSize": "100%",
                 "sort": "descending", "gap": 3, "data": data_pts}]
    return []

def _share_legend(labels: list) -> dict:
    """无坐标轴图（饼/环/漏斗）的图例布局：右侧垂直排布 + type scroll 翻页，
    从根上消除顶部图例换行压到图表本体的问题。对齐 Flint 的约定
    （refs/flint/src/echarts/templates/funnel.ts:112-136：legend 右侧
    orient vertical + 超量 scroll + 本体让位；instantiate-spec.ts:449-540：
    单图例一律右侧垂直并按标签宽度预留位置）。宽度按最长标签的显示宽
    （CJK=2 半宽单位，复用 _text_units）估值，运行时 shareLegendLayout 是
    同一公式的 JS 镜像（WYSIWYG）。"""
    units = max([_text_units(str(x)) for x in labels] + [3])
    width = max(64, min(160, units * 7 + 34))
    return {"type": "scroll", "orient": "vertical", "right": 4, "top": "middle",
            "width": width, "itemGap": 8,
            "textStyle": {"fontSize": 11, "color": "#54555a"}}

def _breakdown_series(c: dict, rows: list) -> tuple[list[dict], list]:
    """Build per-breakdown series for a 维度×维度 cross-tab (roles.breakdown).

    Mirrors the runtime `breakdownSeries`/`categoryOption` so the STATIC option
    matches the runtime re-aggregation (WYSIWYG): pivot into one stacked/grouped
    series PER breakdown value, with x = the unique primary dimension labels.

    Returns (series, unique_x_labels). rows are flat [dim, breakdown, measure…].
    """
    data = c.get("data") or {}
    cols = data.get("columns") or []
    dim = c.get("dimension") or cols[0]
    measures = c.get("measures") or []
    breakdown = (c.get("roles") or {}).get("breakdown") or (c.get("breakdown") or cols[1] if len(cols) > 1 else None)
    di = cols.index(dim) if dim in cols else 0
    bi = cols.index(breakdown) if breakdown and breakdown in cols else 1
    mi = cols.index(measures[0]) if measures and measures[0] in cols else 2
    vt = c.get("viewType")
    # unique breakdown values and unique primary labels, preserving order
    bd_vals: list = []
    x_labels: list = []
    for r in rows:
        bdv = r[bi]
        if bdv not in bd_vals:
            bd_vals.append(bdv)
        if r[di] not in x_labels:
            x_labels.append(r[di])
    # Ranked stacked bars read best with categories ordered by their stacked
    # total desc (R3 mandatory rule). Rows arrive per-cell sorted; reorder the
    # x categories by the sum across all breakdown series so the static option
    # satisfies R3 and matches the runtime render (WYSIWYG). Time/ordinal axes
    # keep their chronological/natural order (R3 is waived via sort=time).
    if vt == "stackedBar" and len(x_labels) > 1 and _dimension_type(c) not in ("time", "ordinal"):
        totals: dict[Any, float] = {lbl: 0.0 for lbl in x_labels}
        for r in rows:
            if r[di] in totals:
                totals[r[di]] += _num(r[mi])
        x_labels.sort(key=lambda lbl: -totals.get(lbl, 0.0))
    # Crowded stacked crosstab: many breakdown series over very few primary
    # labels (e.g. 8 months stacked on one product) is unreadable as bars.
    # Re-render as a line family so the density auto-adjusts instead of
    # squeezing eight 20px bars into one tiny category band.
    crowded_line = vt == "stackedBar" and len(bd_vals) >= 5 and len(x_labels) <= 3
    if crowded_line:
        c["densityAdjusted"] = "line"
    # one series per breakdown value; each series' data aligned to x_labels
    series = []
    for bdv in bd_vals:
        s = {"name": str(bdv), "type": "line" if crowded_line else ("bar" if vt == "stackedBar" else ("line" if vt in ("line", "area") else "bar")),
             "stack": None if crowded_line else ("合计" if vt == "stackedBar" else None)}
        if vt in ("line", "area"):
            s["smooth"] = True
        if crowded_line:
            s["smooth"] = True
        if vt == "area":
            s["areaStyle"] = {}
        s["data"] = []
        for lbl in x_labels:
            hit = next((r for r in rows if r[di] == lbl and r[bi] == bdv), None)
            s["data"].append(_num(hit[mi]) if hit is not None else 0)
        series.append(s)
    # 占比堆叠（2026-10-13 语义批，Flint resolveStackable 'normalize'）：显式
    # stackMode='normalize' 时每根柱各段归一到 100%（段值/段和×100）。放在 R3
    # 堆叠排序**之后**——排序按原始总量，归一只改显示值。
    if vt == "stackedBar" and c.get("stackMode") == "normalize":
        for _xi in range(len(x_labels)):
            _tot = 0.0
            for s in series:
                _v = s["data"][_xi]
                if _v is not None:
                    _tot += _v
            if _tot > 0:
                for s in series:
                    if s["data"][_xi] is not None:
                        s["data"][_xi] = s["data"][_xi] / _tot * 100.0
    return series, x_labels

def _chart_option(c: dict, rows: list) -> dict:
    vt = c.get("viewType")
    data = c.get("data") or {}
    cols = data.get("columns") or []
    dim = c.get("dimension") or cols[0]
    measures = c.get("measures") or []
    di = cols.index(dim) if dim in cols else 0
    base = dict(c.get("option") or {})
    if vt in ("bar", "stackedBar", "line", "area", "scatter"):
        base.setdefault("tooltip", {"trigger": "axis" if vt != "scatter" else "item"})
    elif vt in ("pie", "donut", "funnel"):
        base.setdefault("tooltip", {"trigger": "item"})
    labels = [r[di] for r in rows]
    if vt in ("bar", "stackedBar", "line", "area"):
        base.setdefault("grid", {"left": 55, "right": 30, "top": 40, "bottom": 30})
        base.setdefault("legend", {"top": 0, "type": "scroll", "textStyle": {"fontSize": 12}})
        # 维度×维度 cross-tab: when roles.breakdown is set, pivot into one series
        # per breakdown value with x = unique primary labels (mirrors the runtime
        # breakdownSeries), so the static option equals the runtime render (WYSIWYG).
        _bd = (c.get("roles") or {}).get("breakdown")
        if _bd and _bd in cols and vt in ("bar", "stackedBar", "line", "area"):
            series, x_labels = _breakdown_series(c, rows)
            base["xAxis"] = {"type": "category", "data": x_labels,
                             # 避让原则恒开（验收 2026-09-14）：与运行时 categoryAxisMeta
                             # 同一契约 —— ECharts 按标签宽度自动抽稀，任何路径不全量展示。
                             "axisLabel": {"fontSize": 11, "color": "#5e6c84", "interval": "auto"},
                             "axisLine": {"lineStyle": {"color": "#cfd6e4"}}}
            base["series"] = series
            if series and measures:
                # single value axis from the first measure, using its real column index
                _m0 = measures[0]
                _mi = cols.index(_m0) if _m0 in cols else 2
                _vvals = [r[_mi] for r in rows if len(r) > _mi]
                base["yAxis"] = _flint_value_axis(c, _m0, "bar", _vvals, {"type": "value", "name": _measure_name(_m0), "nameTextStyle": {"fontSize": 10}, "axisLabel": {"fontSize": 11, "color": "#5e6c84"}, "splitLine": {"lineStyle": {"type": "dashed", "color": "#eef1f7"}}})
            return base
        base["xAxis"] = {"type": "category", "data": labels,
                         "axisLabel": {"fontSize": 11, "color": "#5e6c84", "interval": "auto"},
                         "axisLine": {"lineStyle": {"color": "#cfd6e4"}}}
        series = _series_from_rows(c, rows)
        mark = {"bar": "bar", "stackedBar": "bar", "line": "line", "area": "area"}.get(vt, "bar")

        def _axis_measure(idx):
            for s in series:
                if s.get("yAxisIndex", 0) == idx:
                    return s["name"]
            return measures[0] if measures else None

        def _axis_values(measure):
            if not measure:
                return []
            mi = cols.index(measure) if measure in cols else (measures.index(measure) + 1 if measure in measures else 1)
            return [r[mi] for r in rows]

        dual = any("yAxisIndex" in s for s in series)
        if dual:
            m0, m1 = _axis_measure(0), _axis_measure(1)
            base["yAxis"] = [
                _flint_value_axis(c, m0, mark, _axis_values(m0), {"type": "value", "name": m0, "nameTextStyle": {"fontSize": 10},
                 "axisLabel": {"fontSize": 11, "color": "#5e6c84"},
                 "splitLine": {"lineStyle": {"type": "dashed", "color": "#eef1f7"}}}),
                _flint_value_axis(c, m1, mark, _axis_values(m1), {"type": "value", "name": m1, "nameTextStyle": {"fontSize": 10},
                 "axisLabel": {"fontSize": 11, "color": "#5e6c84"},
                 "splitLine": {"show": False}}),
            ]
        else:
            m0 = measures[0] if measures else None
            base["yAxis"] = _flint_value_axis(c, m0, mark, _axis_values(m0), {"type": "value", "name": _measure_name(m0) if m0 else None, "nameTextStyle": {"fontSize": 10},
                "axisLabel": {"fontSize": 11, "color": "#5e6c84"},
                "splitLine": {"lineStyle": {"type": "dashed", "color": "#eef1f7"}}})
        base["series"] = series
    elif vt in ("pie", "donut", "funnel"):
        # 图例右侧垂直 scroll（_share_legend），本体由 _series_from_rows 左移让位。
        base.setdefault("legend", _share_legend(labels))
        base["series"] = _series_from_rows(c, rows)
    elif vt == "scatter":
        base.setdefault("grid", {"left": 55, "right": 30, "top": 30, "bottom": 45})
        # Flint data-fit: a scatter correlation uses two value axes. Without a
        # fitted domain a long-tail cluster (points far above 0, spread over
        # orders of magnitude) floats in a large empty region left by a
        # 0-anchored axis. Delegate to _flint_value_axis so both value axes
        # data-fit their own measure's spread (grounded in flint_core.zero_decision).
        xvals = [r[1] for r in rows if len(r) > 1]
        yvals = [r[2] for r in rows if len(r) > 2]
        base["xAxis"] = _flint_value_axis(c, measures[0] if measures else None, "point", xvals, {"type": "value", "name": measures[0] if measures else None, "nameTextStyle": {"fontSize": 10}, "axisLabel": {"fontSize": 11, "color": "#5e6c84"}, "splitLine": {"lineStyle": {"type": "dashed", "color": "#eef1f7"}}})
        base["yAxis"] = _flint_value_axis(c, measures[1] if len(measures) > 1 else None, "point", yvals, {"type": "value", "name": measures[1] if len(measures) > 1 else None, "nameTextStyle": {"fontSize": 10}, "axisLabel": {"fontSize": 11, "color": "#5e6c84"}, "splitLine": {"lineStyle": {"type": "dashed", "color": "#eef1f7"}}})
        # scatter data from spec rows: each row is [name, x, y, ...]
        base["series"] = [{"name": c.get("title", ""), "type": "scatter",
                           "data": [{"name": str(r[0]), "value": [_num(r[1]), _num(r[2])]} for r in rows]}]
    elif vt == "table":
        base["columns"] = cols
        base["rows"] = rows
        base["striped"] = True
        # A table whose authored rows exceed the card's no-scroll capacity may
        # declare pagination at the REQUEST level (chart.pagination=true), which
        # R2 accepts (validate_report reads this flag). Default remains false so
        # an authored table that overflows still fails R2 until the author makes
        # an explicit choice (topK/pagination/bigger layout.h).
        base["pagination"] = bool(c.get("pagination"))
    return base

def _check_table_align_columns(chart: dict, columns: list) -> None:
    """chart.table.align 的「键=真实列名」校验（WP-3，docs/product-iteration-plan.md §2.2）。

    validate_request 只做结构校验（{列名: {h,v}} 的形状与枚举）；列名存在性要等
    编译时有 canonical 表列（含物化计算列/维度值分组列）才能核对——放错列名的
    对齐在这里报可行动错误，而不是静默不出效果。
    """
    tbl = chart.get("table")
    align = tbl.get("align") if isinstance(tbl, dict) else None
    if not isinstance(align, dict) or not align:
        return
    known = list(columns or [])
    unknown = [c for c in align if c not in known]
    if unknown:
        raise ValueError(
            f"图表 {chart.get('id', '?')} chart.table.align 引用了不存在的列：{unknown}；"
            f"数据集真实列：{known}")


def _check_table_column_filters(chart: dict, columns: list) -> None:
    """chart.table.columnFilters 的「键=真实列名」校验（WP-14 过滤器字段区）。

    validate_request 只锁形状（{列名: {type:values|range,…}}）；列名存在性在编译侧
    核（canonical 表列含物化计算列/维度值分组列）——拼错列名的筛选在这里报可行动
    错误，而不是静默过滤不出效果。过滤器字段不要求出现在行/列区（QBI「隐藏字段
    仍可过滤」的组件级声明位），所以存在性宇宙 = 全部真实列。
    """
    tbl = chart.get("table")
    cf = tbl.get("columnFilters") if isinstance(tbl, dict) else None
    if not isinstance(cf, dict) or not cf:
        return
    known = list(columns or [])
    unknown = [c for c in cf if c not in known]
    if unknown:
        raise ValueError(
            f"图表 {chart.get('id', '?')} chart.table.columnFilters 引用了不存在的列：{unknown}；"
            f"数据集真实列：{known}")


# ---- WP-11 总计计算方式（表格合计 per-measure，对标 QBI）------------------------
# 语义（唯一，三处载体共用）：totalValue(col) = totalCalc[col]（声明了覆盖时）按
# **全量过滤原始行**重算；未覆盖的指标不归本模块管（运行时 colTotals 维持现状口径）。
# 三载体：① 编译静态合计 = option.totalRow（本文件）；② 服务端在线合计 =
# /aggregate 载荷 totalCalc → aggregates[col][聚合]（server/aggregator.py）；
# ③ JS 本地镜像由 WP-11-JS 消费同一 editor.table.totalCalc 声明。

def _check_table_total_calc_columns(chart: dict, columns: list,
                                    derived_names: Any = None) -> None:
    """chart.table.totalCalc 的「键=真实列名」校验（WP-11，align 同款先例）。

    存在性宇宙 = canonical 表列（含物化计算列）∪ 聚合级派生指标名——派生指标
    是真实存在但**不可加**的声明，拼错名字该报错、拼对了走静默跳过闸门
    （_effective_table_total_calc），两种命运必须在编译期分开。
    """
    tbl = chart.get("table")
    tc = tbl.get("totalCalc") if isinstance(tbl, dict) else None
    if not isinstance(tc, dict) or not tc:
        return
    known = list(columns or []) + [n for n in (derived_names or []) if isinstance(n, str)]
    unknown = [c for c in tc if c not in known]
    if unknown:
        raise ValueError(
            f"图表 {chart.get('id', '?')} chart.table.totalCalc 引用了不存在的列：{unknown}；"
            f"数据集真实列：{list(columns or [])}")


def _total_calc_col_numeric(raw_table: dict | None, col: str) -> bool:
    """totalCalc 的非数值跳过闸门：列不是数值列 → 静默保持默认合计。

    理由（契约，照抄任务裁决）：非数值列（文本/类目/时间）的 sum/avg 无意义，
    服务端 CAST 还会把文本塌成 0 出假数。判定两级：meta.types 声明 measure →
    数值；没有声明 → 抽样从严（出现任一不可解析值即放弃覆盖——拿不准就保持
    现状，绝不输出可疑的「合计」）。"""
    if raw_table is None:
        return False
    cols = raw_table.get("columns") or []
    if col not in cols:
        return False
    meta_t = ((raw_table.get("meta") or {}).get("types") or {}).get(col)
    if meta_t == "measure":
        return True
    if meta_t:
        return False
    ci = cols.index(col)
    hits = total = 0
    for r in (raw_table.get("rows") or [])[:500]:
        v = r[ci] if len(r) > ci else None
        if v is None or (isinstance(v, str) and not v.strip()):
            continue
        total += 1
        try:
            float(v)
            hits += 1
        except (TypeError, ValueError):
            pass
    return total > 0 and hits == total


def _effective_table_total_calc(chart: dict, raw_table: dict | None,
                                derived_names: Any = None) -> dict[str, str]:
    """过完跳过闸门后仍生效的 totalCalc 声明 {列: 聚合}。

    闸门（全部静默，编译日志不报错）：share/window/公式类**聚合级派生指标**与
    非数值列的声明被忽略——派生指标不可加且语义上「占比的合计」无意义（占比的
    分母口径由 derived_eval 自己管）；行级计算列是物化真列，不在闸内。
    """
    tbl = chart.get("table")
    tc = tbl.get("totalCalc") if isinstance(tbl, dict) else None
    if not isinstance(tc, dict) or not tc:
        return {}
    measures = set(chart.get("measures") or [])
    derived = {n for n in (derived_names or []) if isinstance(n, str)}
    out: dict[str, str] = {}
    for col, agg in tc.items():
        if not isinstance(agg, str) or col == COUNT_MEASURE:
            continue
        # 只对本表声明的指标生效：合计行展示的就是这些列。
        if col not in measures:
            continue
        if col in derived:
            continue
        if not _total_calc_col_numeric(raw_table, col):
            continue
        out[col] = agg
    return out


def _static_total_row(raw_table: dict | None, filters: Any,
                      total_calc: dict[str, str]) -> dict[str, Any]:
    """按 totalCalc 从**全量过滤原始行**重算合计 {列: 值}（编译静态载体）。

    NULL 语义逐条对齐 server `_agg_expr`/overall 的既有约定（同一份语义两种
    载体，parity 由 tests/test_table_total_calc.py 锁）：
      sum     = COALESCE(SUM(real), 0)   → 空池 0.0
      average = AVG(real)                → 空池 None（SQL NULL，渲染「—」）
      min/max = MIN/MAX(real)            → 空池 None
      count   = COUNT(*)                 → 过滤后行数（不是数值池大小）
      countd  = COUNT(DISTINCT col)      → 非空原始值去重数（'' 不算 NULL，与 SQL 同）
    数值池 = 可 float 的非空值（与 _deterministic_rows 的取数口径一致）。
    """
    out: dict[str, Any] = {}
    if raw_table is None or not total_calc:
        return out
    cols = raw_table.get("columns") or []
    all_rows = [r for r in (raw_table.get("rows") or []) if _row_filter(r, cols, filters)]
    for col, agg in total_calc.items():
        if col not in cols:
            continue
        ci = cols.index(col)
        if agg == "count":
            out[col] = float(len(all_rows))   # float：与 server overall（float(v)）逐位同型
            continue
        if agg == "countd":
            out[col] = float(len({r[ci] for r in all_rows if len(r) > ci and r[ci] is not None}))
            continue
        pool: list[float] = []
        for r in all_rows:
            v = r[ci] if len(r) > ci else None
            if v is None or (isinstance(v, str) and not v.strip()):
                continue
            try:
                pool.append(float(v))
            except (TypeError, ValueError):
                pass
        if agg == "sum":
            out[col] = sum(pool) if pool else 0.0
        elif agg == "average":
            out[col] = (sum(pool) / len(pool)) if pool else None
        elif agg == "min":
            out[col] = min(pool) if pool else None
        elif agg == "max":
            out[col] = max(pool) if pool else None
    return out


def _chart_manifest_entry(c: dict, rows: list, raw_table: dict | None = None,
                          dataset_id: str = "primary",
                          derived_nodes: dict | None = None,
                          total_calc_table: dict | None = None) -> dict:
    # Cross-table chart (P2): the merged rows/columns are already aligned by
    # _cross_table_merge; do NOT re-map to a single raw table. Preserve the
    # crossTable binding so the runtime re-aggregates the SAME alignment.
    if c.get("_crossTable"):
        data = c.get("data") or {}
        cols = data.get("columns") or []
        dim = c.get("dimension") or (cols[0] if cols else "")
        entry_data = {
            "datasetId": c.get("datasetId") or dataset_id,
            "columns": cols,
            "dimension": dim,
            "measures": c.get("measures") or [],
            "aggregation": c.get("aggregation", "sum"),
            "measureTypes": c.get("measureTypes") or {},
            "filter": c.get("filter") or [],
            "crossTable": c["_crossTable"],
        }
        entry = {"id": c["id"], "blockId": "block-" + c["id"], "elementId": c["id"],
                 "title": c.get("title", ""), "viewType": c.get("viewType"),
                 "option": _chart_option(c, rows),
                 "editor": {"editable": True, "data": entry_data}}
        # 表格卡作者设置（批次一 + WP-3/WP-11/WP-14）：跨表图表与单表同一条透传
        # 契约——请求层 chart.table 原样落 editor.table（运行时 showTableBody 的
        # 唯一读取口），绝不静默丢设置。totalCalc 的静态合计按**合并后全量行**重算
        # （_cross_table_merge 已按各表 filter 过滤，这里不再二次过滤）；派生指标
        # 的闸门与单表同一条（_effective_table_total_calc，宇宙=合并列 ∪ 派生名，
        # 编译期 _check_table_total_calc_columns 已在 compile 交叉分支核对存在性）。
        if isinstance(c.get("table"), dict) and c.get("viewType") == "table":
            entry["editor"]["table"] = dict(c["table"])
            _tc_effective = _effective_table_total_calc(
                c, data,
                set(c.get("derivedMeasures") or []) | set(derived_nodes or {}))
            if _tc_effective:
                entry["option"]["totalRow"] = _static_total_row(data, [], _tc_effective)
        if c.get("annotation"):
            entry["annotation"] = c["annotation"]
        if isinstance(c.get("style"), dict):
            entry["style"] = c["style"]
        if c.get("sort") == "time":
            entry["sortOverride"] = "time"
        elif c.get("sort") == "asc":
            entry["sortOverride"] = "category-asc"
        return entry
    data = c.get("data") or {}
    cols = data.get("columns") or []
    # Product boundary: author charts reference the raw dataset so re-aggregation
    # (sum/avg/max/min) stays meaningful instead of re-aggregating a pre-built
    # summary. `editable.data` keeps the raw columns+rows plus the author's
    # selections; `dimensionBucket` declares a time-bucket dimension (e.g. the
    # authored "月份" column is derived from the raw "快照日期" column, bucketed
    # by month). Non-bucketed charts use the raw column directly.
    dim = c.get("dimension") or (cols[0] if cols else "")
    all_types = c.get("measureTypes") or (infer_measure_types((raw_table or {}).get("columns", [])) if raw_table else {})
    selected_measures = set(c.get("measures") or [])
    semantic_types = {k: v for k, v in all_types.items() if k in selected_measures}
    # Product boundary: an author chart references the raw dataset only when its
    # dimension/measures map onto raw columns (so re-aggregation is meaningful).
    # Charts built from DERIVED measure columns (e.g. a stacked-bar pivot where
    # each status is its own measure column) keep their pre-computed summary —
    # those are fixed compositions outside the time-bucket boundary.
    _author_measures = c.get("measures") or []
    # An aggregate-level DERIVED measure (dataset.derived.measures) is also
    # "raw-referenceable": it is computed at the chart's grouping from raw
    # columns, so the chart still belongs to the raw-re-aggregation boundary.
    # Without this a chart whose only measure is derived fell into the
    # pre-computed branch → no dimensionBucket → the runtime could not bucket it.
    _derived_measure_names = set(c.get("derivedMeasures") or [])
    _raw_hits = [m for m in _author_measures
                 if m == COUNT_MEASURE or m in (raw_table or {}).get("columns", [])
                 or m in _derived_measure_names] if raw_table else []
    _dim_is_raw = (dim in (raw_table or {}).get("columns", [])) or bool((c.get("dimensionBucket") or {}).get("source"))
    # Re-aggregation is meaningful only for aggregate views; 'none' (detail /
    # Top-N rows) is a fixed composition that must stay pre-computed.
    _agg = c.get("aggregation", "sum")
    if raw_table is not None and len(_raw_hits) == len(_author_measures) and _dim_is_raw and _agg in ("sum", "average", "count", "countd", "min", "max"):
        raw_cols = raw_table.get("columns", [])
        raw_rows = raw_table.get("rows", [])
        db = c.get("dimensionBucket") or {}
        # Product boundary: ALL author charts reference the raw dataset so
        # re-aggregation stays meaningful. If the authored dimension is not a
        # raw column but a date column exists, auto-bucket by month as a sane
        # fallback (author may override via dimensionBucket).
        raw_dim = db.get("source") or (dim if dim in raw_cols else None)
        bucket = db.get("bucket")
        # Auto-detect time-bucket dimension: authored dim not in raw cols, but a
        # date-like raw col exists → treat as month-bucketed (the common case).
        if raw_dim is None:
            date_cols = [col for col in raw_cols if any(k in col for k in ("日期", "时间", "date", "day", "月"))]
            if date_cols:
                raw_dim = date_cols[0]
                bucket = bucket or ("month" if any(k in raw_dim for k in ("日期", "时间", "date")) else None)
        # A RAW time dimension (e.g. YearMonth = 202501) is already at a date
        # grain but without a dimensionBucket it renders the raw float "202501.0".
        # Infer a month/day/year bucket when its values are date-like, so the
        # static option and the runtime re-aggregation both label by month.
        if bucket is None and raw_dim and raw_dim in raw_cols:
            bucket = _infer_time_bucket(c, raw_cols, raw_dim)
        if raw_dim is None:
            raw_dim = dim  # fall back to authored dim (may not group raw, but display-only)
        # Keep the FULL raw columns+rows so editor.data is rectangular and the
        # 未选择 zone lists every field (Issue 1). dimension/measures cherry-pick
        # the author's selections; unused raw cols stay available to drag in.
        measures_raw = [m for m in (c.get("measures") or [])
                        if m in raw_cols or m in _derived_measure_names] or (c.get("measures") or [])
        entry_data = {
            "datasetId": dataset_id,
            "columns": raw_cols,
            "dimension": raw_dim,
            "measures": measures_raw,
            # 行维度有序列表（P2 三维交叉表；批 A/B 多维表格：运行时 _tblDims 与
            # multi 分支行数都读它——请求声明 1–3 个行维必须透传，else 分支已有同款）。
            **({"dimensions": list(c["dimensions"])} if c.get("dimensions") else {}),
            "aggregation": c.get("aggregation", "sum"),
            # 占比堆叠透传（2026-10-13）：运行时 breakdownSeries 与静态 option 同闸。
            **({"stackMode": c["stackMode"]} if c.get("stackMode") else {}),
            "measureTypes": semantic_types,
            "filter": c.get("filter") or [],
        }
        # dimensionBucket: {source: raw date col, bucketed: authored dim, bucket: month/week/day}
        if bucket:
            # Bucket regardless of whether the authored dim equals the raw source
            # (a raw date column bucketed to month, e.g. intent DSL x.order日期 +
            # timeUnit:month), so the runtime aggregates the raw rows by grain.
            # ``precision`` = 该列值域推出的**源数据精度**（与 bucket 分开记）：
            # 作者/读者可以把 bucket 改粗（年），但下钻不该比源数据更细 ——
            # 月精度（202501）下钻到「日」只会得到 1 个点，是误导而非能力。
            # 编译期是唯一知道整列值域的地方，所以精度在这里落地（server 模式
            # 运行时没有行数据，靠它才判定得出来）。
            entry_data["dimensionBucket"] = {"source": raw_dim, "bucketed": dim, "bucket": bucket,
                                             # precision = the RAW column's own grain
                                             # (re-inferred here, never the authored
                                             # bucket): an authored coarse bucket
                                             # (year over daily data) must not clamp
                                             # the drill-down floor to the coarse
                                             # level — only the source value domain
                                             # decides what "finer" means.
                                             "precision": _infer_time_bucket(c, raw_cols, raw_dim) or bucket}
        elif raw_dim != dim:
            # Authored dim differs from the raw grouping column but no explicit
            # bucket: still emit a dimensionBucket so the runtime uses raw_dim as
            # the grouping key and labels by the authored dim if possible.
            entry_data["dimensionBucket"] = {"source": raw_dim, "bucketed": dim, "bucket": db.get("bucket") or "raw"}
    else:
        entry_data = {
            "datasetId": dataset_id,
            "columns": cols,
            "dimension": dim,
            "measures": c.get("measures") or [],
            # 行维度有序列表（P2 三维交叉表，行区第 2 个 = 行头嵌套外层→内层）
            **({"dimensions": list(c["dimensions"])} if c.get("dimensions") else {}),
            "aggregation": c.get("aggregation", "sum"),
            **({"stackMode": c["stackMode"]} if c.get("stackMode") else {}),
            # Raw time dimension: declare its grain so the runtime groups and
            # labels by the same bucket the compiler used (and so a window derived
            # measure knows which period it is comparing).
            **({"dimensionBucket": {"source": dim, "bucketed": dim,
                                    "bucket": _infer_bucket_from_chart(c, dim),
                                    "precision": _infer_bucket_from_chart(c, dim)}}
               if _infer_bucket_from_chart(c, dim) else {}),
            "measureTypes": semantic_types,
            "filter": c.get("filter") or [],
        }
        if c.get("dimensionBucket"):
            entry_data["dimensionBucket"] = c.get("dimensionBucket")
    # P2: hand the chart the RESOLVED aggregate-level nodes it uses
    # (``editor.data.derivedMeasures = {name: node}``), the same way
    # ``measureTypes``/``aggregations`` ride on the chart: the dataset keeps the
    # single definition, while the chart carries what it evaluates — so the runtime
    # needs no dataset lookup and the compiled manifest is self-describing (the
    # boot migration then only serves legacy manifests).
    _used_names = set(c.get("derivedMeasures") or [])
    if _used_names and derived_nodes:
        # 烘**闭包**而非裸 used 集合：窗口指标引用另一派生指标（window.ref →
        # filtered/formula）时，只烘 used 节点会让运行时 required_aggregations 把
        # 引用名当普通列下发（服务端 400「column not found」→ 整卡错误态、
        # semanticControls 假红，2026-10-09 单窗口验收卡实锤）。与运行时
        # derivedClosureNodes / syncChartDerivedMeasures 同一闭包语义（单一真源
        # derived_eval.closure_nodes）。
        _used_nodes = [derived_nodes[n] for n in _used_names if n in derived_nodes]
        _nodes = {n["name"]: n for n in derived_eval.closure_nodes(_used_nodes, derived_nodes)}
        if _nodes:
            entry_data["derivedMeasures"] = _nodes
    # BI-H2 share=parent：把「标签 → 父级」映射烘进图表 editor.data（parentShare），
    # 服务端报告（schema-only、无行数据）编辑重算时才有父级分母可用。键 = 图表分组
    # 维度的原始值（String(value)）；运行时只在图表维度未变时使用烘焙值，维度变了
    # 会给出可行动报错而不是拿错的映射算比例。
    _share_parent = [n for n in (entry_data.get("derivedMeasures") or {}).values()
                     if isinstance(n, dict) and n.get("kind") == "share" and n.get("scope") == "parent"]
    if _share_parent and raw_table is not None:
        _raw_cols = raw_table.get("columns", [])
        _raw_di = _raw_cols.index(dim) if dim in _raw_cols else None
        if _raw_di is not None:
            _ps_maps: dict = {}
            for _pn in _share_parent:
                _pf = _pn.get("parentField")
                if _pf not in _raw_cols:
                    continue
                _pi = _raw_cols.index(_pf)
                _m: dict = {}
                for _r in raw_table.get("rows", []):
                    _k = _r[_raw_di]
                    _key = "" if _k is None else str(_k)
                    if _key not in _m:
                        _pv = _r[_pi]
                        _m[_key] = "" if _pv is None else str(_pv)
                _ps_maps[_pn["name"]] = _m
            if _ps_maps:
                entry_data["parentShare"] = {"dimension": dim, "maps": _ps_maps}
    # When a time bucket is applied (e.g. YearMonth 202501 → 2025-01), format the
    # static option's dimension labels to the same bucketed grain the runtime
    # re-aggregation produces, so the authored view == the edited view (WYSIWYG).
    option_rows = rows
    # Authored TOP-N must be honored by the STATIC option too (not just the
    # runtime): cap the dimension-led rows so option.series / option.rows truly
    # show Top-K, otherwise a "Top 15" title renders all N categories in the
    # static artifact (export / PPTX / screenshot) while only the live view caps.
    option_rows = _apply_static_topn(c, rows, cols, dim)
    # sort:'asc' = ascending by dimension label (R3 exempt via sortOverride);
    # reorder the static rows the same way the runtime 排序方式 renders them.
    if c.get("sort") == "asc" and option_rows and dim in cols:
        _di = cols.index(dim)
        if (c.get("truncation") or {}).get("merged"):
            # 「其他（N 项）」行恒排最后（运行时 sortEntriesDefault 对 otherMerged
            # 的契约）：label 升序按 codepoint 比较，CJK 维度值（手机 U+624B…）
            # 全部排在「其」(U+5176) 之后，合并行会被翻到轴首。
            _head = [r for r in option_rows
                     if not (len(r) > _di and str(r[_di]).startswith("其他（"))]
            _tail = [r for r in option_rows
                     if len(r) > _di and str(r[_di]).startswith("其他（")]
            option_rows = sorted(_head, key=lambda r: (str(r[_di]) if len(r) > _di else "")) + _tail
        else:
            option_rows = sorted(option_rows, key=lambda r: (str(r[_di]) if len(r) > _di else ""))
    _dbk = entry_data.get("dimensionBucket") or {}
    if _dbk.get("bucket") and _dbk.get("bucket") != "raw" and option_rows:
        option_rows = _bucket_static_rows(c, option_rows, cols, dim, _dbk.get("bucket"),
                                          raw_table=raw_table, filters=c.get("filter"))
    entry = {
        "id": c["id"], "blockId": "block-" + c["id"], "elementId": c["id"],
        "title": c.get("title", ""),
        "viewType": c.get("viewType"),
        "option": _chart_option(c, option_rows),
        "editor": {"editable": True, "data": entry_data},
    }
    if c.get("densityAdjusted"):
        entry["densityAdjusted"] = c["densityAdjusted"]
    # 截断事实（读者可见的「已显示前 K 项（共 N 项）」）：编译期是唯一知道自己
    # 砍掉了多少类目的地方——运行时对未编辑的图表直接用作者 option，不会重新聚合。
    if isinstance(c.get("truncation"), dict) and c["truncation"].get("total"):
        entry["truncation"] = c["truncation"]
    if c.get("annotation"):
        entry["annotation"] = c["annotation"]
    if isinstance(c.get("style"), dict):
        entry["style"] = c["style"]
    # Propagate the authored TOP-N + sort intent into editor.data so the runtime
    # TOP K selector and 排序方式 default to the authored values (e.g. a
    # "...TOP10" title means topK=10 + 降序), instead of falling back to 全部 +
    # 升序 which visibly contradicts the chart's title.
    # 只信**显式声明**（chart.topK / encoding.topK——编译期已并入 topK）。旧版
    # 回落到 c["limit"]，而编译产物恒盖章 limit=500（encoding.get("limit",500)），
    # 于是每张未声明 TOP-N 的图都被烙上 editor.data.topK=500，运行时 TOP-K
    # 选择器把系统默认当成作者意图展示。
    authored_limit = int(c.get("topK") or 0)
    if authored_limit > 0:
        # bar/stackedBar honor topK; other types keep the pre-aggregated cap.
        entry_data.setdefault("topK", authored_limit)
    if c.get("topKOther") is True:
        entry_data.setdefault("topKOther", True)
    authored_sort = c.get("sort")
    if authored_sort == "asc":
        entry_data.setdefault("sort", {"field": c.get("dimension"), "dir": "asc"})
    elif authored_sort == "time":
        # 时间意图只有维度值真的可日历解析时才写 dimension-asc：不可解析的
        # 「1月..12月」标签运行时只能 localeCompare（1月,10月,11月…乱轴）。
        # 判定必须与 _sorted_rows 的 chrono 门同源（dim_type_is_time），静态
        # 不重排、运行时回落首指标降序默认，三方一致。
        if _dimension_type(c) == "time":
            entry_data.setdefault("sort", {"field": c.get("dimension"), "dir": "asc"})
    elif authored_sort == "desc":
        # 作者显式降序必须如实透传：旧版落入 chrono 分支被改写成 asc，
        # 静态首屏按指标降序排名、首次重聚合即翻回升序。
        measures = entry_data.get("measures") or c.get("measures") or []
        sort_field = measures[0] if measures else c.get("dimension")
        entry_data.setdefault("sort", {"field": sort_field, "dir": "desc"})
    else:
        measures = entry_data.get("measures") or c.get("measures") or []
        sort_field = measures[0] if measures else c.get("dimension")
        # Default ranked charts (bar/rank, stackedBar without a time axis) sort
        # by the leading measure descending; a time-trend stays ascending.
        # 运行时默认（sortEntriesDefault 无 editor.sort 时）只对 kind==='time'
        # 走日历序，ordinal 当 category 处理（首指标降序）——这里的方向必须与
        # 静态行序 + 运行时默认三方一致，否则首次重聚合即翻轴。
        _is_chrono = _dimension_type(c) == "time"
        entry_data.setdefault("sort", {"field": sort_field, "dir": "asc" if _is_chrono else "desc"})
    if entry_data.get("sort"):
        # 运行时渲染真正消费的槽位是 chart.editor.sort（sortEntriesDefault 读它；
        # editor.data.sort 只有面板 hydrate 兜底读）。编译期把生效排序镜像过去，
        # 未提交过面板的图第一次重聚合也复现静态行序，而不是回落到运行时默认。
        entry["editor"]["sort"] = dict(entry_data["sort"])
    if c.get("sort") == "time":
        entry["sortOverride"] = "time"
    elif c.get("sort") == "asc":
        entry["sortOverride"] = "category-asc"

    if c.get("roles"):
        entry["editor"]["data"]["roles"] = c["roles"]
    # 表格卡作者设置（批次一，docs/data-table-capability-plan.md）：请求层 chart.table
    # （布局/合并同类单元格/数据条/行总计/合计行位置与别名等，validate_request 已校验形状）
    # 原样落 editor.table —— 运行时 showTableBody 的唯一读取口，编辑态改设置即覆盖。
    if isinstance(c.get("table"), dict) and c.get("viewType") == "table":
        entry["editor"]["table"] = dict(c["table"])
        # WP-11 总计计算方式：有生效覆盖时，把静态合计烘进 option.totalRow（{列: 值}，
        # 全量过滤原始行按指定聚合重算）。未生效（派生指标/非数值/非本表指标被闸）
        # 或无声明 → 不写该键，产物与现状逐字节一致。运行时 option 不认识 totalRow
        # 也不受影响（WP-11-JS 消费）；服务端在线合计走 /aggregate totalCalc 载荷。
        # total_calc_table：mixed/server 模式下 remote 数据集的 chart.datasetId 已
        # 改名空间化 id，entry 分支拿不到 raw_table（tables_by_id 按 raw id 键）——
        # 载体①曾因此整段未烘（marketing 复验 N1）。它只喂 totalCalc 重算，不改
        # entry 走哪个分支（分支切换会改 server 产物的 editor.data 形状，另案）。
        _tc_effective = _effective_table_total_calc(
            c, total_calc_table or raw_table,
            set(c.get("derivedMeasures") or []) | set(derived_nodes or {}))
        if _tc_effective:
            entry["option"]["totalRow"] = _static_total_row(
                total_calc_table or raw_table, c.get("filter"), _tc_effective)
    # Composite (混合图表): carry per-measure series type / Y-axis into editor.data
    # so the runtime renders bar+line with ≤2 value axes (intent DSL derived).
    if c.get("seriesType"):
        entry["editor"]["data"]["seriesType"] = c.get("seriesType")
    if c.get("seriesAxis"):
        entry["editor"]["data"]["seriesAxis"] = c.get("seriesAxis")
    if c.get("truth"):
        entry["truth"] = c["truth"]
    return entry

# ---------- manifest + HTML ----------

def _derive_dimension_keys(relations: list) -> dict:
    """v4 single-mode: each ``keys[]`` entry IS a dimensionKey edge.

    The new schema collapses ``same-domain``/``referential`` into one kind
    (merge); the dimensionKey concept survives via each ``keys[i]``
    declaring that ``a.col`` and ``b.col`` are the SAME logical dimension.
    Multiple keys with the same explicit ``name`` (or, unnamed, with the same
    ``a.col``) merge into one dimensionKey group. Returns
    ``{dimensionKey: {tables: [{datasetId, column}, ...]}}`` for cross-table
    filter cascading and aligned aggregation (multi-source-alignment §3.3/§5)."""
    out: dict = {}
    for rel in relations or []:
        if not isinstance(rel, dict):
            continue
        # renames 在链式 join 之后应用，所以键列在宽表里的名字是别名；
        # dimensionKey 必须用别名，否则跨表过滤/对齐找不到那一列。
        rmap = {s: d for s, d in (rel.get("renames") or {}).items()
                if isinstance(s, str) and isinstance(d, str)}
        for k in (rel.get("keys") or []):
            if not isinstance(k, dict):
                continue
            a, b = k.get("a") or {}, k.get("b") or {}
            name = (k.get("name") or rmap.get(a.get("col"), a.get("col"))
                    or rmap.get(b.get("col"), b.get("col")) or "").strip()
            if not name:
                continue
            entry = out.setdefault(name, {"tables": []})
            seen = {(t["datasetId"], t["column"]) for t in entry["tables"]}
            for side in (a, b):
                ds, col = side.get("ds"), side.get("col")
                if not (isinstance(ds, str) and isinstance(col, str)):
                    continue
                col = rmap.get(col, col)
                key2 = (ds, col)
                if key2 in seen:
                    continue
                entry["tables"].append({"datasetId": ds, "column": col})
                seen.add(key2)
    return out


def _data_asof_from_table(table: dict | None, roles: dict | None) -> str | None:
    """主数据集的「数据截至」（manifest.meta.dataAsOf）：时间列各列最大值里**最晚
    的那个**，经 date_parse（ONE parser 纪律，scripts/date_parse.py）标准化为 ISO
    ``YYYY-MM-DD``。无时间列 / 无可解析日期值 → None（调用方省略 meta 键，绝不写
    null）——读者/封面/汇报页脚三端读同一个字段回答「这份数据到几号」。

    时间列判定两级（对齐维度语义 parity 规则 1：声明的度量永不是时间轴）：
    声明的 columnRoles ``time`` 角色优先；未声明时用 ``date_parse.infer_grain``
    探测（裸四位年必须列名带年提示——计数列 ``1902`` 绝不被读成日期），已显式
    声明 measure/id/ignore 的列永不参与探测。推断只取样首部行（分类不是数据
    真值），最大值仍对全列扫描；比较用 ``date_parse.sort_key``（日历序元组），
    标准化用 ``date_parse.iso``。"""
    cols = list((table or {}).get("columns") or [])
    rows = list((table or {}).get("rows") or [])
    if not cols or not rows:
        return None
    roles = dict(roles or {})
    time_cols = [c for c, r in roles.items() if r == "time" and c in cols]
    if not time_cols:
        for ci, col in enumerate(cols):
            if roles.get(col) in ("measure", "id", "ignore"):
                continue
            sample = [row[ci] for row in rows[:400]
                      if isinstance(row, (list, tuple)) and ci < len(row)]
            if date_parse.infer_grain(sample, str(col)):
                time_cols.append(col)
    best_key: tuple | None = None
    best_iso: str | None = None
    for col in time_cols:
        ci = cols.index(col)
        for row in rows:
            if not isinstance(row, (list, tuple)) or ci >= len(row):
                continue
            sk = date_parse.sort_key(row[ci])
            if sk[0] != 0:
                continue  # 该细胞不是可解析日期（判别位 1/2 = 文本/数值）
            if best_key is None or sk > best_key:
                best_key = sk
                best_iso = date_parse.iso(row[ci])
    return best_iso


def build_manifest(spec: dict, charts: list[dict], placed: list[tuple[dict, int, int]],
                   cover_h: int, raw_table: dict | None = None,
                   dataset_id: str = "primary", bands: list | None = None,
                   tables_by_id: dict | None = None,
                   primary_table: dict | None = None) -> dict:
    blocks = []
    chart_entries = []
    # free 档：封面只在显式声明时进块清单（渲染端同门控，见 render_grid_html /
    # render_tabs_html 的 `if cov`）。
    if not _is_free(spec) or (spec.get("cover") or {}):
        blocks.append({"id": "block-cover", "kind": "text"})
    # Keep the in-flow filter card whenever a request declares filters OR the
    # compiler seeded safe dimension candidates. The runtime uses filterCols as
    # the initial visible controls and can add/remove more in edit mode.
    candidates = spec.get("filterCandidates") or []
    if _filter_h(max(len(_flatten_filters(spec.get("filters") or [])), len(candidates))) > 0:
        _f_cols = [f.get("column") for f in _flatten_filters(spec.get("filters") or []) if f.get("column")]
        for col in candidates:
            if col not in _f_cols:
                _f_cols.append(col)
        blocks.append({"id": "block-filter", "kind": "text", "component": "filter",
                       "scope": "global", "filterCols": _f_cols})
    for k in range(len(spec.get("kpis", []))):
        blocks.append({"id": f"block-kpi-{k}", "kind": "kpi"})

    def add_chart(c: dict) -> None:
        """Append the chart block + manifest entry bound to its OWN dataset."""
        cds = c.get("datasetId") or dataset_id
        ds_table = None
        if tables_by_id:
            ds_table = tables_by_id.get(cds)
        elif raw_table is not None and cds == dataset_id:
            ds_table = raw_table
        # Mixed/server：remote 数据集的 chart.datasetId 已改名空间化 id（remote_final
        # 改名前 _dsRaw 钉在图上），tables_by_id 按 raw id 键——entry 走的分支刻意
        # 保持现状（切到 raw-referenceable 会改 server 产物 editor.data 形状，另案），
        # 但 totalCalc 的静态合计（载体①）必须拿得到原始行：单独解析 tc_source。
        tc_source = ds_table
        if tc_source is None and tables_by_id and c.get("_dsRaw"):
            tc_source = tables_by_id.get(c["_dsRaw"])
        blocks.append({"id": "block-" + c["id"], "kind": "chart"})
        _der_nodes = {n["name"]: n for n in _derived_measure_nodes(spec, cds)
                     if isinstance(n, dict) and n.get("name")}
        chart_entries.append(_chart_manifest_entry(c, _sorted_rows(c), ds_table, cds, _der_nodes,
                                                   tc_source))

    # Story sections interleave narrative/insight text bands between their
    # charts (repo repo docs/story-sections-design.md). Without sections (bands empty)
    # this is exactly the flat cover → filter → KPI → charts → insight order.
    if bands:
        for step in _flow(placed, bands):
            if step[0] == "band":
                b = step[1]
                if b["kind"] == "annotation":
                    # B4 migration: an independent chart-annotation text block.
                    ann_color = _band_card_color(b)
                    blocks.append({"id": f"block-ann-{b.get('chart', '')}", "kind": "text",
                                   "component": "chart-annotation", "chart": b.get("chart", ""),
                                   "paragraphs": _band_paragraphs(b),
                                   "textStyle": _band_text_style(b),
                                   "cardColor": ann_color,
                                   "layout": {"x": b.get("x", 0), "y": b["y"], "w": b.get("w", 6), "h": b["h"]}})
                    continue
                suffix = "narrative" if b["kind"] == "narrative" else "insight"
                # In tabs-enabled reports the narrative band IS the section's 页签
                # (tab) card: component section-tab (the runtime numbers/tracks it),
                # and the stored h2 title is stripped of its enumeration prefix so
                # the positional tab number can re-prefix it 1:1 in card + bar.
                comp = f"section-{suffix}"
                paras = _band_paragraphs(b)
                if b["kind"] == "narrative" and tabs_enabled(spec):
                    comp = "section-tab"
                    if paras and paras[0].get("level") == "h2":
                        paras[0] = dict(paras[0], text=_strip_enumerator(paras[0].get("text")))
                    # A tab card never becomes a fillable 批注 (tx-annot) look —
                    # but style.cardColor DOES apply (缺省透明，显式有色走 .txc-*
                    # 卡盒并退掉序号；_tab_card_article 是唯一渲染口)。
                blocks.append({"id": f"block-sec-{b['section']}-{suffix}", "kind": "text",
                               "section": b["section"], "component": comp,
                               "paragraphs": paras, "textStyle": _band_text_style(b),
                               "cardColor": _band_card_color(b),
                               "layout": {"x": b.get("x", 0), "y": b["y"], "w": b.get("w", 12), "h": b["h"]}})
            else:
                c_step = step[1]
                if c_step.get("viewType") == "text":
                    # free texts 伪卡：成段模型文本块（不进 charts，编辑器把它
                    # 当普通文本卡编辑段落）。
                    _tk, _tc = _resolve_card_style(c_step.get("textKind") or None,
                                                   c_step.get("cardColor") or None, "text")
                    blocks.append({"id": "block-" + c_step.get("id", ""), "kind": "text",
                                   "component": "text", "paragraphs": c_step.get("paragraphs") or [],
                                   "textStyle": _tk, "cardColor": _tc})
                    continue
                add_chart(c_step)
    else:
        for c, x, y in placed:
            if c.get("viewType") == "text":
                _tk, _tc = _resolve_card_style(c.get("textKind") or None,
                                               c.get("cardColor") or None, "text")
                blocks.append({"id": "block-" + c.get("id", ""), "kind": "text",
                               "component": "text", "paragraphs": c.get("paragraphs") or [],
                               "textStyle": _tk, "cardColor": _tc})
                continue
            add_chart(c)
    # 关键洞察卡：standard 档恒有（历史契约，空声明渲染空卡）；free 档声明制。
    if spec.get("insights") or not _is_free(spec):
        blocks.append({"id": "block-insight", "kind": "text", "textStyle": "annotation",
                       "cardColor": "default",
                       "paragraphs": _insight_paragraphs(spec.get("insights", [])[:3])})
    # Optional editable data table (多维表格-style). Opt-in via spec.dataTable so
    # the default template stays clean (template contract: no data-report-detail
    # unless requested). The runtime renders the editable grid into the host.
    if spec.get("dataTable"):
        blocks.append({"id": "block-detail", "kind": "table", "component": "detail",
                       "layout": {"x": 0, "y": 0, "w": 12, "h": 10}})
    # Evidence/detail/quality/source are sidecar or on-demand views. Keeping
    # them out of the default grid prevents a long, low-signal report tail.
    style = spec.get("style") or {}
    accent = {"blue": "#5645d4", "teal": "#2a9d99", "orange": "#dd5b00", "purple": "#7b3ff2"}.get(style.get("accent"), "#5645d4")
    manifest = {
        "schemaVersion": 3,
        "reportId": spec.get("reportId", "report"),
        "title": spec.get("title", "报告"),
        "templateId": spec.get("templateId") or "standard-bi-v1",
        "cover": (spec.get("cover") or {}),
        # 烘焙上限分档随 2026-09-28「全部放开」：free 12/10，standard 4/3
        # （与 validate_request 的容量上限同一口径，切片只是防御性重复）。
        "kpis": spec.get("kpis", [])[:12 if _is_free(spec) else 4],
        "insights": spec.get("insights", [])[:10 if _is_free(spec) else 3],
        "texts": [dict(t) for t in (spec.get("texts") or [])],
        "kpiPosition": (spec.get("kpiPosition") or "top"),
        "source": spec.get("source", {}),
        "filters": spec.get("filters", []),
        "filterCandidates": spec.get("filterCandidates", []),
        "quality": (spec.get("provenance") or {}).get("quality", {}),
        "blocks": blocks,
        "charts": chart_entries,
        "datasets": spec.get("datasets", []),
        "relations": spec.get("relations") or [],
        "dimensionKeys": _derive_dimension_keys(spec.get("relations") or []),
        "theme": {"name": (style.get("theme") or "notion"), "tokens": {"accent": accent, "density": style.get("density", "comfortable"), "radius": style.get("radius", "medium"), "fontScale": style.get("fontScale", "md")},
                  "typography": {"fontFamily": "Inter, -apple-system, BlinkMacSystemFont, 'PingFang SC', 'Microsoft YaHei', sans-serif", "base": 15}},
        "chartTheme": (style.get("chartTheme") or "default"),
                  "provenance": spec.get("provenance", {}),
                  "columnRoles": dict(spec.get("columnRoles") or {}),
                  "runtime": dict(spec.get("runtime") or {}),
    }
    # 数据截至（meta.dataAsOf）：编译期取主数据集（datasets[0]，即声明/缺省的
    # primary 槽）时间列最大值的最晚者，date_parse 标准化为 ISO。单文件与
    # server 两条编译路径同走 build_manifest（compile_spec 是唯一入口），无
    # 分支遗漏；无时间列时整个 meta 键省略（schema 上 meta.required=[dataAsOf]，
    # 绝不写 null）。三端（封面/阅读态徽章/汇报页脚）读同一个字段。
    _asof_table = primary_table if primary_table is not None else raw_table
    if _asof_table is not None:
        _ds_entry = next((d for d in (spec.get("datasets") or [])
                          if isinstance(d, dict) and d.get("id") == dataset_id), None)
        _roles = dict(spec.get("columnRoles") or {})
        if isinstance(_ds_entry, dict):
            _roles.update(_ds_entry.get("columnRoles") or {})
        _asof = _data_asof_from_table(_asof_table, _roles)
        if _asof:
            manifest["meta"] = {"dataAsOf": _asof}
            # C3（2026-09-25）：封面投影。meta.dataAsOf 是唯一权威；cover.dataAsOf
            # 只喂封面渲染（_cover_article_html 的「数据截至」行）与 server 重渲染
            # 的 _cover_h 行数预留——同一字段投影，绝不两处推断。
            # 只投影进**非空** cover：free 档无声明封面的 manifest.cover 是 `or {}`
            # 留下的空 dict——isinstance 通过，写进 dataAsOf 后变 truthy，渲染端
            # `if cov` 判真 → 渲出一条只带「数据截至」的幽灵封面卡（gs-h=0 被
            # GridStack 归一成 1 行，真机截图坐实）。_cover_h 的 free 门算 0，
            # 与渲染门必须同源：空 cover 保持 falsy 才是「没有封面」。
            if isinstance(manifest.get("cover"), dict) and manifest["cover"]:
                manifest["cover"]["dataAsOf"] = _asof
    # 报告页面宽度（docs/page-width-and-table-autofit-design.md）：只落归一后的
    # int；request 缺省 → 1280 现状（旧 manifest 零迁移）。归一/校验真源在
    # validate_request.resolve_page_width —— 不许第二份档位表。
    from validate_request import resolve_page_width
    manifest["pageWidth"] = resolve_page_width(spec.get("pageWidth"))
    # 数据源独立档（docs/data-source-independence-and-merge-visibility-design.md D2）：
    # 请求缺省由 validate_request 归一为 "default"；烘进 manifest 供决策清单/
    # 审计与前台核对「这份报告的数据源是否被合并过」。
    manifest["dataSourceMode"] = spec.get("dataSourceMode") or "default"
    # 报告主题轴收敛（docs/report-theme-convergence-design.md）：themeShells 烘
    # 全表（运行时切换需要所有主题的壳令牌）；id 与 chart-adapter CHART_THEMES
    # 对齐由 tests/test_report_themes.py 源级断言。显式 style.accent 覆盖主题
    # accent（决议 2）；chartOnly 档 tokens=default（深底主题不上壳）。
    import report_themes
    manifest["themeShells"] = report_themes.shell_catalog(accent if style.get("accent") else None)
    sections = spec.get("sections") or []
    if sections:
        # Persist the authored story so the runtime and server re-render path
        # (server/template.py → layout_sections) can rebuild the same bands.
        manifest["sections"] = sections
    # tabs 键与 sections 同门：无 sections（free 未声明且未合成）时不烘——
    # 「tabs.enabled=true 但没有任何页签卡」是会给运行时埋雷的谎话形状。
    if tabs_enabled(spec) and (spec.get("sections") or []):
        manifest["tabs"] = {"enabled": True}
    return manifest

def _flatten_filters(filters: Any) -> list[dict]:
    if isinstance(filters, dict):
        out = list(filters.get("all") or [])
        any_items = list(filters.get("any") or [])
        if any_items:
            out.append({"_any": any_items})
        return [item for item in out if isinstance(item, dict)]
    return [item for item in (filters or []) if isinstance(item, dict)]

def _format_filter_item(item: dict, esc, collapse: int | None = None) -> str:
    if "_any" in item:
        return "任一条件：" + " 或 ".join(_format_filter_item(x, esc, collapse) for x in item["_any"] if isinstance(x, dict))
    value = item.get("value", "")
    if isinstance(value, list):
        # A long membership list (e.g. 35 Product Families) collapses to its
        # first values + a count so the chip never takes over a whole row.
        # Lists of >= 5 items collapse (a 5-region in-list still measures
        # ~130px per item and would push the chip row onto a 2nd line of the
        # fixed 1-row filter band, clipping it).
        shown = value[:3] if collapse and len(value) >= collapse else value
        value = "、".join(str(v) for v in shown)
        if collapse and len(shown) < len(item.get("value") or []):
            value += f" 等 {len(item['value'])} 项"
    _op = str(item.get("op", "=")).strip().lower()
    _opmap = {"=": "等于", "!=": "不等于", ">": "大于", ">=": "大于等于", "<": "小于", "<=": "小于等于",
              "eq": "等于", "ne": "不等于", "gt": "大于", "gte": "大于等于", "lt": "小于", "lte": "小于等于",
              "in": "属于", "not in": "不属于", "notin": "不属于", "contains": "包含",
              "is null": "为空", "is not null": "不为空"}
    _col = item.get("column") or item.get("dimensionKey", "")
    if _op in ("is null", "is not null"):
        return esc(_col) + " " + esc(_opmap[_op])
    op = _opmap.get(_op, item.get("op", "="))
    return esc(_col) + " " + esc(op) + " " + esc(value)

# A filter value list >= this collapses to "A、B、C 等 N 项" (the chip stays
# one line: hover/focus swaps in the full list via .chip-full).
_FILTER_CHIP_COLLAPSE = 5


def _filter_chip_html(item: dict, esc) -> str:
    """One filter chip. Long value lists render collapsed; hover/focus (the
    chip is focusable via tabindex) swaps in the full list."""
    full = _format_filter_item(item, esc)
    short = _format_filter_item(item, esc, collapse=_FILTER_CHIP_COLLAPSE)
    if short == full:
        return f'<span class="filter-chip">{full}</span>'
    return (f'<span class="filter-chip filter-chip-collapsible" tabindex="0">'
            f'<span class="chip-short">{short}</span><span class="chip-full">{full}</span></span>')


def _filter_card_article(block_id: str, scope: str, filter_chips: str, y: int, filter_h: int, esc) -> str:
    """A 筛选器 card (block-filter band). Readers see just the filter conditions;
    the compact 作用范围 (global/tab) selector is an EDIT-ONLY chrome that the
    runtime's renderOneFilterBar mounts when editing. ``data-filter-scope`` stays on
    the host so the split-mode global dim rule applies regardless of edit state."""
    if filter_h <= 0:
        return ""
    chips = filter_chips or ""
    return (f'<article class="grid-stack-item" data-report-block="filter" data-report-block-id="{esc(block_id)}" '
            f'gs-x="0" gs-y="{y}" gs-w="12" gs-h="{filter_h}"><div class="grid-stack-item-content">'
            f'<div class="block ai-report-filter" data-report-filter data-filter-block="{esc(block_id)}" '
            f'data-filter-scope="{esc(scope)}"><div class="filter-chips">{chips}</div></div></div></article>')

# A cover meta item longer than this is a sentence, not a chip label: it is
# demoted to the summary's first line so the chip row carries only short labels.
_COVER_CHIP_MAX = 24


def _cover_chips_lead(cov: dict) -> tuple[list[str], list[str]]:
    """Split cover meta into (short chips, long lead lines)."""
    chips: list[str] = []
    leads: list[str] = []
    for m in (cov.get("meta") or []):
        s = str(m).strip()
        if not s:
            continue
        (chips if len(s) <= _COVER_CHIP_MAX else leads).append(s)
    return chips, leads


def _cover_meta_summary_html(cov: dict, esc) -> tuple[str, str]:
    """(meta chips html, summary html) for the cover block.

    Each meta chip carries ``data-cover-meta-index`` so the runtime can treat
    it as an editable/deletable bubble (B4b: cover 元数据气泡 content model); the
    ``.meta`` container is the add target (``+ 元数据`` button).
    """
    chips, leads = _cover_chips_lead(cov)
    full = list(cov.get("meta") or [])
    meta = ""
    for i, raw in enumerate(full):
        s = str(raw).strip()
        if not s:
            continue
        if len(s) <= _COVER_CHIP_MAX:
            meta += (f'<span class="chip" data-cover-meta-index="{i}" '
                     f'data-editable-text>{esc(s)}</span>')
    summary = cov.get("summary") or "本报告由标准模板生成，指标和图表均基于输入数据。"
    lead_html = ""
    if leads:
        lead_html = ('<span class="summary-lead">' + "<br>".join(esc(x) for x in leads) + "</span><br>")
    return meta, lead_html + esc(summary)


def _cover_paragraphs(cov: dict) -> list:
    """Unified paragraph model for the cover card (block-cover).

    The cover is a full-width banner text card: report title (h1), meta items
    (short ones ≤ _COVER_CHIP_MAX become bubble chips, longer ones plain body
    rows), then the summary (body). Rows keep their authored relative order so
    the editor can reorder them freely like any other text card (drag grips /
    row delete × / inline format bar). Mirrored by editor-runtime.js
    ``coverParagraphsFromFields`` so a runtime edit never reshapes the card.
    """
    out: list = []
    t = str(cov.get("title") or "").strip()
    if t:
        out.append({"level": "h1", "text": cov.get("title", "")})
    chips: list[str] = []
    leads: list[str] = []
    for m in (cov.get("meta") or []):
        s = str(m).strip()
        if not s:
            continue
        (chips if len(s) <= _COVER_CHIP_MAX else leads).append(s)
    for c in chips:
        out.append({"level": "body", "bubble": True, "text": c})
    for l in leads:
        out.append({"level": "body", "text": l})
    sm = str(cov.get("summary") or "").strip()
    if sm:
        out.append({"level": "body", "text": cov.get("summary", "")})
    return out


def _cover_article_html(cov: dict, cover_h: int) -> str:
    """Cover card as a paragraph-based banner text block."""
    esc = lambda value: _html.escape(str(value if value is not None else ""), quote=True)  # noqa: E731
    # C3 数据截至（2026-09-25）：cover.dataAsOf（build_manifest 从 meta.dataAsOf
    # 投影，同一字段绝不两处推断）存在时，在副标题区（气泡 chip + 导语）之下、摘要
    # 之上插一行「数据截至 YYYY-MM-DD」小字。它**不进段落模型**（data-ai-static，
    # 无 data-p-index/data-editable-text）：runtime coverParagraphsFromFields 的镜像
    # 不含它，进了模型反而会在运行时段落重排时丢行/错位。代价 = 用户在编辑器里
    # 重排封面段落后这行会消失（重新构建即恢复）；tabbar 徽章才是常驻面。
    # 无 dataAsOf 时（meta 键整个省略的旧报告/无时间列）三段输出与旧版逐字节一致。
    asof = str((cov or {}).get("dataAsOf") or "").strip()
    paras = _cover_paragraphs(cov)
    head, tail = paras, []
    if asof and paras and not paras[-1].get("bubble"):
        # _cover_paragraphs 把 summary 恒放在最后（非气泡 body 行）——尾段 = 摘要。
        head, tail = paras[:-1], paras[-1:]
    asof_html = ""
    if asof:
        asof_html = f'<div class="cover-asof" data-ai-static="1">数据截至 {esc(asof)}</div>'
    inner = ('<div class="block tx-banner ai-cover-text" data-text-block>'
             + _paragraphs_html(head) + asof_html + _paragraphs_html(tail)
             + '</div>')
    return (f'<article class="grid-stack-item" data-report-block="text" data-report-block-id="block-cover" '
            f'gs-x="0" gs-y="0" gs-w="12" gs-h="{cover_h}">'
            f'<div class="grid-stack-item-content">{inner}</div></article>')


def _format_kpi(kp: dict, spec: dict | None = None, manifest: dict | None = None) -> tuple[str, str, str]:
    value = kp.get("rawValue", kp.get("value"))
    kind = str(kp.get("semanticType") or kp.get("measureType") or "number")
    if isinstance(kind, dict): kind = str(kind.get("type") or "number")
    # P5: a number column with format='percent' (the canonical P1
    # normalised shape) renders as a percentage here too. Look up the
    # KPI's bound measure on either the top-level measureTypes or the
    # primary dataset's measureTypes, and override the kind when the
    # format flag is set.
    kpi_mt: Any = None
    if kind not in ("percent", "missingRate", "growthRate"):
        col = kp.get("countDim") or kp.get("measure")
        mt = None
        if col and spec:
            mt = (spec.get("measureTypes") or {}).get(col)
            if not mt:
                # Per-dataset slot (P1 shape) — fall back to the first dataset.
                ds = (spec.get("datasets") or [{}])[0]
                mt = (ds.get("measureTypes") or {}).get(col)
        if mt is None and col and manifest:
            ds = (manifest.get("datasets") or [{}])[0]
            mt = (ds.get("measureTypes") or {}).get(col)
        kpi_mt = mt
        if isinstance(mt, dict) and mt.get("format") == "percent":
            kind = "percent"
        elif mt == "percent":
            kind = "percent"
    # 币种符号（拍板 1/6，docs/multidim-table-and-currency-design-2026-09-28.md §二）：
    # symbol > 内置币表 > ¥ 缺省（type_registry.currency_symbol 与运行时
    # adapter.currencySymbolOf 同一解析）。大数压缩单位恒中文「万/亿」（拍板 6）。
    kpi_symbol = currency_symbol(kpi_mt)
    unit = ""
    try:
        number = float(value)
        if kind == "currency":
            if abs(number) >= 1e8: return f"{kpi_symbol}{number / 1e8:,.2f}", kind, "亿"
            if abs(number) >= 1e4: return f"{kpi_symbol}{number / 1e4:,.2f}", kind, "万"
            return f"{kpi_symbol}{number:,.0f}", kind, ""
        if kind in {"percent", "missingRate", "growthRate"}:
            return f"{number * 100:,.2f}", "percent", "%"
        if kind in {"count", "integer"} or number.is_integer():
            return f"{number:,.0f}", kind, ""
        if kind == "duration": unit = "天"
        return f"{number:,.2f}", kind, unit
    except (TypeError, ValueError):
        return str(value if value not in (None, "") else "—"), kind, ""

def build_html(spec: dict, manifest: dict, placed: list[tuple[dict, int, int]], cover_h: int,
               theme: dict | None = None, bands: list | None = None) -> str:
    esc = lambda value: _html.escape(str(value if value is not None else ""), quote=True)
    kpis = spec.get("kpis", [])
    insights = spec.get("insights", [])
    source = spec.get("source", {})
    # cover
    cov = spec.get("cover") or {}
    meta = "".join(f'<span class="chip">{esc(m)}</span>' for m in cov.get("meta", []))
    # kpis
    kpi_html = ""
    # Header order (cover → filter → KPI → charts): the filter bar sits directly
    # under the cover and above the (now compact) KPI cards, so the fixed-order
    # template reads top-down. Compute filter_h first so KPI y can offset past it.
    kpi_rows = max(1, (len(kpis) + 3) // 4) if kpis else 0
    filter_items = _flatten_filters(spec.get("filters") or [])
    filter_summary = "；".join(_format_filter_item(f, esc) for f in filter_items)
    filter_chips = "".join(_filter_chip_html(f, esc) for f in filter_items)
    filter_h = _filter_h(max(len(filter_items), len(manifest.get("filterCandidates") or [])))
    y_filter = cover_h  # filter bar sits above the KPI row
    # kpiPosition="bottom"（free）：KPI 条移到图表/文本区、洞察卡之后。placed/bands
    # 的底部在进循环前就能确定性算出，KPI 卡的 gs-y 与后续 max_y 记账同源不漂。
    _kpi_at_bottom = manifest.get("kpiPosition") == "bottom"
    _placed_bottom = max((y + ((c.get("layout") or {}).get("h") or _default_h(c)) for c, _x, y in placed), default=0)
    _bands_bottom = max((b.get("y", 0) + b.get("h", 0) for b in (bands or [])), default=0)
    _ins_rows = _insight_h(insights) if insights else 0
    _flow_bottom = max(_placed_bottom, _bands_bottom, cover_h + filter_h)
    if _kpi_at_bottom:
        kpi_y0 = _flow_bottom + _ins_rows
        max_y0 = cover_h + filter_h
    else:
        kpi_y0 = cover_h + filter_h
        max_y0 = cover_h + filter_h + kpi_rows * _KPI_H
    for k, kp in enumerate(kpis):
        acc = kp.get("accent", "")
        display, kind, unit = _format_kpi(kp, spec)
        raw = kp.get("rawValue")
        raw_attr = "" if raw is None else f' value="{esc(raw)}"'
        unit_html = f'<span class="unit">{esc(unit)}</span>' if unit else ""
        kpi_html += (f'<article class="grid-stack-item" data-report-block="kpi" data-report-block-id="block-kpi-{k}" '
                     f'gs-x="{k % 4 * 3}" gs-y="{kpi_y0 + (k // 4) * _KPI_H}" gs-w="3" gs-h="{_KPI_H}"><div class="grid-stack-item-content">'
                     f'<div class="kpi-card {esc(acc)}{_text_block_classes("", kp.get("style"))}"><span class="label" data-editable-text>{esc(kp.get("label", ""))}</span>'
                     f'<data class="value" data-kpi-kind="{esc(kind)}"{raw_attr}>{esc(display)}{unit_html}</data>'
                     f'<div class="accent-bar"></div></div></div></article>')
    # charts
    chart_html = ""
    max_y = max_y0
    for c, x, y in placed:
        if c.get("viewType") == "text":
            # free texts 伪卡 → 独立文本卡（段模型），绝不建 chart-host。
            chart_html += _text_block_article(c, x, y)
            max_y = max(max_y, y + ((c.get("layout") or {}).get("h") or 3))
            continue
        bid = "block-" + c["id"]
        vt = c.get("viewType")
        inner = f'<div id="{c["id"]}" class="chart-host"></div>'
        if vt == "table":
            cols = (c.get("data") or {}).get("columns") or []
            rows = _sorted_rows(c)
            th = "".join(f"<th class='num'>{col}</th>" for col in cols[1:])
            first = f"<th>{cols[0]}</th>" if cols else ""
            trs = ""
            for r in rows:
                tds = "".join(f"<td class='num'>{_fmt_cell(v, cols[j], c)}</td>" for j, v in enumerate(r[1:], 1))
                trs += f"<tr class='striped'><td>{r[0]}</td>{tds}</tr>"
            inner = (f'<div id="{c["id"]}" class="table-host"><table class="ai-report-table striped">'
                     f'<thead><tr>{first}{th}</tr></thead><tbody>{trs}</tbody></table></div>')
        chart_html += (f'<article class="grid-stack-item" data-report-block="chart" data-report-block-id="{bid}" '
                       f'gs-x="{x}" gs-y="{y}" gs-w="{_chart_width(c)}" gs-h="{(c.get("layout") or {}).get("h", _default_h(c))}">'
                       f'<div class="grid-stack-item-content"><div class="block"><h4 class="block-title" data-editable-text data-chart-title-for="{esc(c.get("id", ""))}">{esc(c.get("title", ""))}</h4>'
                       f'{inner}</div></div></article>')
        max_y = max(max_y, y + (c.get("layout") or {}).get("h", _default_h(c)))
    # Fixed skeleton sections after the authored charts.
    evidence = next((c for c in spec.get("charts", []) if c.get("viewType") == "table"), None)
    evidence = evidence or ((spec.get("charts") or [None])[0])
    evidence_cols = ((evidence or {}).get("data") or {}).get("columns") or []
    evidence_rows = _sorted_rows(evidence)[:20] if evidence else []
    def static_table(cols, rows):
        if not cols:
            return '<div class="ai-report-empty">暂无聚合证据</div>'
        head = "".join(f"<th>{esc(col)}</th>" for col in cols)
        body = "".join("<tr>" + "".join(f"<td>{esc(_fmt_cell(v, cols[j], evidence or {}))}</td>" for j, v in enumerate(row)) + "</tr>" for row in rows)
        return f'<div class="table-host"><table class="ai-report-table"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'
    aggregate_html = static_table(evidence_cols, evidence_rows)
    first_ds = (manifest.get("datasets") or [{}])[0]
    detail_cols = [c.get("name") if isinstance(c, dict) else c for c in (first_ds.get("columns") or [])]
    detail_html = static_table(detail_cols, (first_ds.get("rows") or [])[:50])
    y_ins = max(max_y, y_filter + filter_h)
    manifest_json = json.dumps(manifest, ensure_ascii=False).replace("</", "<\\/")
    cover_summary = cov.get("summary") or "本报告由标准模板生成，指标和图表均基于输入数据。"
    source_label = source.get("label") or "输入数据"
    source_notes = source.get("notes") or "聚合、格式化和质量标记由 v1 编译器确定性生成。"
    # Request v1 exposes only named visual tokens. Keep the CSS deterministic
    # and never interpolate arbitrary user CSS or background colors.
    css_tokens = (theme or {}).get("tokens") or {}
    css = SHELL_CSS.replace("--accent:#5645d4", "--accent:" + str(css_tokens.get("accent", "#5645d4")))
    css = css.replace("--radius:12px", "--radius:" + str(css_tokens.get("radius", "12px")))
    css = css.replace("padding:32px 24px 64px", "padding:" + str(css_tokens.get("shellPadding", "32px 24px 64px")))
    css = css.replace("font-size:15px;line-height:1.55", "font-size:" + str(css_tokens.get("fontSize", "15px")) + ";line-height:1.55")
    # The grid body is a single shared renderer (see render_grid_html). The
    # spec is authoritative for the dynamic text sections (cover/kpis/insights/
    # filters) at render time, so reflect them into the manifest view first.
    body_manifest = dict(manifest)
    body_manifest["cover"] = spec.get("cover") or body_manifest.get("cover") or {}
    body_manifest["kpis"] = spec.get("kpis") or body_manifest.get("kpis") or []
    body_manifest["insights"] = spec.get("insights") or body_manifest.get("insights") or []
    body_manifest["filters"] = spec.get("filters") or body_manifest.get("filters") or []
    # 页面宽度档位（docs/page-width-and-table-autofit-design.md）：shell 内联
    # --page-max 防 FOUC；默认 1280 不写 attr（既有报告产物逐字节不变）。
    # runtime boot applyShellWidth() 是编辑态下的权威执行者。
    from validate_request import resolve_page_width as _resolve_pw
    _pw = _resolve_pw(body_manifest.get("pageWidth"))
    shell_style = f' style="--page-max:{_pw}px"' if _pw != 1280 else ''
    # 主题覆盖变量（docs/report-theme-convergence-design.md）：**恒写**当前主题的
    # 内联变量（含 default——A1 后 default accent=#5070dd≠SHELL_CSS 兜底字面值，
    # 不恒写就会闪回旧紫）。chartOnly 档 vars=default 壳。runtime applyShellTheme
    # 同口径（切主题即整体覆写，不删属性）。
    _ct = (body_manifest.get("chartTheme") or "default")
    _sh = (body_manifest.get("themeShells") or {}).get(_ct) or {}
    theme_style = ''
    if _sh.get("vars"):
        theme_style = ' style="' + ";".join(f"{k}:{v}" for k, v in sorted(_sh["vars"].items())) + '"'
    if (manifest.get("tabs") or {}).get("enabled") and (spec.get("sections") or []):
        # Workbench tabs (repo repo docs/workbench-tabs-design.md): overview + one grid view
        # per section + implicit 明细数据 view; runtime toggles [data-tab-view].
        grid_body = render_tabs_html(spec, body_manifest, cover_h)
        shell = f'<div class="report-shell report-shell-tabs"{shell_style}>\n{grid_body}\n</div>'
    else:
        grid_body = render_grid_html(body_manifest, placed, cover_h, bands)
        shell = f'<div class="report-shell"{shell_style}>\n<section class="grid-stack" data-report-grid>\n{grid_body}\n</section>\n</div>'
    return f"""<!DOCTYPE html>
<html lang="zh-CN"{theme_style}><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(spec.get("title", "报告"))}</title><style>{css}</style></head><body>{shell}
<script id="ai-report-manifest" type="application/json">{manifest_json}</script>
</body></html>
"""

# Author-side Markdown (repo repo docs/markdown-text-design.md): headings, hr, quote,
# lists, code and the inline emphasis set are parsed by _md() — everything else
# is escaped plain text; no user input ever becomes markup.
_MD_HEAD = re.compile(r"^(#{1,3})\s+(.*)$")
_MD_HR = re.compile(r"^\s*(-{3,}|\*{3,})\s*$")
_MD_QUOTE = re.compile(r"^>\s?(.*)$")
_MD_UL = re.compile(r"^[-*]\s+(.*)$")
_MD_OL = re.compile(r"^\d+\.\s+(.*)$")
_MD_CODEBLOCK = re.compile(r"^(    |\t)(.*)$")


def _md_inline(text: str, depth: int = 0) -> str:
    """Inline Markdown for a whitelisted text run → safe HTML (escape-first).

    Tokens: `code`, **bold**, ~~del~~, ==highlight== (→ <mark>), *em* / _em_,
    [label](http(s)://…). Mirrored 1:1 by editor-runtime.js mdInlineText."""
    if depth > 4:
        return _html.escape(str(text), quote=True)
    out = []
    pos = 0
    # Alternation over the whitelisted inline tokens (ordered: code first so a
    # backtick span is never treated as emphasis).
    token = re.compile(r"(`[^`]+`|\*\*.+?\*\*|~~.+?~~|==[^=]+==|\*[^*\s][^*]*\*|_([^_\s]|[^_]*?[^_])_|\[[^\]]+\]\((https?://[^)\s]+)\))")
    for m in token.finditer(text):
        if m.start() > pos:
            out.append(_html.escape(text[pos:m.start()], quote=True))
        tok = m.group(0)
        if tok.startswith("`"):
            out.append("<code>" + _html.escape(tok[1:-1], quote=True) + "</code>")
        elif tok.startswith("**"):
            out.append("<strong>" + _md_inline(tok[2:-2], depth + 1) + "</strong>")
        elif tok.startswith("~~"):
            out.append("<del>" + _md_inline(tok[2:-2], depth + 1) + "</del>")
        elif tok.startswith("=="):
            out.append("<mark>" + _md_inline(tok[2:-2], depth + 1) + "</mark>")
        elif tok.startswith("*"):
            out.append("<em>" + _md_inline(tok[1:-1], depth + 1) + "</em>")
        elif tok.startswith("_"):
            out.append("<em>" + _md_inline(tok[1:-1], depth + 1) + "</em>")
        elif tok.startswith("["):
            link = re.match(r"^\[([^\]]*)\]\((https?://[^)\s]+)\)$", tok)
            if link:
                label = _md_inline(link.group(1), depth + 1)
                href = _html.escape(link.group(2), quote=True)
                out.append(f'<a href="{href}" rel="noopener noreferrer" target="_blank">{label}</a>')
            else:
                out.append(_html.escape(tok, quote=True))
        else:
            out.append(_html.escape(tok, quote=True))
        pos = m.end()
    if pos < len(text):
        out.append(_html.escape(text[pos:], quote=True))
    return "".join(out)


# ---- Rich inline serialization (light-Word / Feishu-style WYSIWYG) ----------
# contenteditable 富文本提交的 HTML → **白名单序列化**回存储文本。序列化产物
# 仍是一个「文本」字符串（协议零改动：paragraphs[].text 还是 str），渲染端
# `_paragraphs_html`/`paragraphsHtml` 对 fmt 段落按**信任的预序列化 HTML** 直出
# （不经 _md/_md_inline 再解析——markup 语义由序列化器保证，不由解析器保证）。
_RICH_TOKEN_RE = re.compile(r"<(/?)(b|strong|i|em|u|s|strike|del|mark|code|span|a|br)\b([^>]*)>")
_RICH_COLOR_RE = re.compile(r"(?:^|;\s*)color\s*:\s*([#a-zA-Z0-9(),\s]+?)(?:;|$)")
_RICH_BG_RE = re.compile(r"(?:^|;\s*)background(?:-color)?\s*:\s*([#a-zA-Z0-9(),\s]+?)(?:;|$)")
_RICH_SIZE_RE = re.compile(r"(?:^|;\s*)font-size\s*:\s*([0-9.]+)(px|em)", re.IGNORECASE)
_RICH_HREF_RE = re.compile(r'href\s*=\s*"([^"]*)"')


def _rich_norm_color(value: str) -> str | None:
    """Normalize an inline color to a lowercase hex/named token; None = drop."""
    v = (value or "").strip().lower()
    if not v:
        return None
    m = re.match(r"^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", v)
    if m:
        r_, g_, b_ = (max(0, min(255, int(m.group(i)))) for i in (1, 2, 3))
        return f"#{r_:02x}{g_:02x}{b_:02x}"
    if re.match(r"^#[0-9a-f]{3}([0-9a-f]{3})?$", v):
        return v
    # A tiny named-color floor (the palette the format bar actually writes).
    return v if v in ("white", "black", "red", "green", "orange", "yellow", "gray", "grey", "blue", "purple", "transparent") else None


def _rich_span_openers(attrs: str) -> list[tuple[str, str]]:
    """Whitelisted (open, close) pairs carried by a <span style/class> opener."""
    styles = ""
    sm = re.search(r'style\s*=\s*"([^"]*)"', attrs)
    if sm:
        styles = sm.group(1)
    clsm = re.search(r'class\s*=\s*"([^"]*)"', attrs)
    if clsm and "ai-text-highlight" in clsm.group(1):
        styles = styles + ";background:#f9e79f"
    pairs: list[tuple[str, str]] = []
    # execCommand underline/strike leak through as span styles when styleWithCSS
    # is on — fold them into the u/s whitelist tokens.
    um = re.search(r"(?:^|;)\s*text-decoration[^:]*:\s*([a-z-]+)", styles)
    deco = um.group(1) if um else ""
    if deco == "underline":
        pairs.append(("<u>", "</u>"))
    elif deco in ("line-through", "strikethrough"):
        pairs.append(("<s>", "</s>"))
    # styleWithCSS 下 execCommand bold 产出 span+font-weight（真机探针抓到：
    # 不折叠则加粗在提交时丢失）。
    wm = re.search(r"(?:^|;)\s*font-weight\s*:\s*([a-z0-9]+)", styles)
    if wm and wm.group(1) in ("bold", "bolder", "600", "700", "800", "900"):
        pairs.append(("<b>", "</b>"))
    cm = _RICH_COLOR_RE.search(styles)
    color = _rich_norm_color(cm.group(1)) if cm else None
    if color:
        pairs.append((f'<span style="color:{color}">', "</span>"))
    bm = _RICH_BG_RE.search(styles)
    bg = _rich_norm_color(bm.group(1)) if bm else None
    if bg and bg != "transparent":
        pairs.append((f'<mark style="background:{bg}">', "</mark>"))
    szm = _RICH_SIZE_RE.search(styles)
    if szm:
        px = float(szm.group(1)) if szm.group(2) == "px" else float(szm.group(1)) * 13
        size = min(2.0, max(0.75, round(px / 13, 2)))
        if size != 1:
            pairs.append((f'<span style="font-size:{size:g}em">', "</span>"))
    return pairs


def _rich_close_tag(out: list, stack: list, tag: str) -> None:
    """Pop entries until (and including) the nearest opener of ``tag``; every
    popped entry's closers are emitted (reverse order) — malformed nesting from
    contenteditable is normalized instead of leaking open styles."""
    while stack:
        t, closers = stack.pop()
        for c in closers:
            out.append(c)
        if t == tag:
            return


def _rich_serialize_inline(html: str, depth: int = 0) -> str:
    """Serialize a contenteditable inline run to the whitelisted token string.

    Everything not in the whitelist is dropped (tags) or escaped (text) — an
    attacker-supplied paragraph can never carry markup into storage/render.
    Openers are tracked on a stack so closing tags emit matching closers and
    unclosed whitelisted tags never leak styles past the paragraph (EOF closes
    everything). Text fragments are entity-DEcoded then escaped once — innerHTML
    arrives entity-encoded and double-escaping would render literals."""
    if depth > 8:
        return ""
    html = _RICH_BLOCK_RE.sub("", _RICH_COMMENT_RE.sub("", html or ""))
    out: list[str] = []
    stack: list[tuple[str, list[str]]] = []  # (source tag, closers in pop order)
    pos = 0

    def emit_text(fragment: str) -> None:
        if fragment:
            out.append(_html.escape(_html.unescape(_RICH_TEXT_RE.sub("", fragment)), quote=True))

    for m in _RICH_TOKEN_RE.finditer(html):
        emit_text(html[pos:m.start()])
        closing = bool(m.group(1))
        tag = m.group(2).lower()
        attrs = m.group(3) or ""
        if tag == "br":
            out.append("\n")
        elif tag == "span":
            if not closing:
                pairs = _rich_span_openers(attrs)
                for o, _c in pairs:
                    out.append(o)
                if pairs:
                    stack.append(("span", [c for _o, c in reversed(pairs)]))
            else:
                _rich_close_tag(out, stack, "span")
        elif tag in ("b", "strong"):
            if closing:
                _rich_close_tag(out, stack, "b")
            else:
                out.append("<b>")
                stack.append(("b", ["</b>"]))
        elif tag in ("i", "em"):
            if closing:
                _rich_close_tag(out, stack, "i")
            else:
                out.append("<i>")
                stack.append(("i", ["</i>"]))
        elif tag == "u":
            if closing:
                _rich_close_tag(out, stack, "u")
            else:
                out.append("<u>")
                stack.append(("u", ["</u>"]))
        elif tag in ("s", "strike", "del"):
            if closing:
                _rich_close_tag(out, stack, "s")
            else:
                out.append("<s>")
                stack.append(("s", ["</s>"]))
        elif tag == "mark":
            if closing:
                _rich_close_tag(out, stack, "mark")
            else:
                out.append("<mark>")
                stack.append(("mark", ["</mark>"]))
        elif tag == "code":
            if closing:
                _rich_close_tag(out, stack, "code")
            else:
                out.append("<code>")
                stack.append(("code", ["</code>"]))
        elif tag == "a" and not closing:
            hm = _RICH_HREF_RE.search(attrs)
            href = (hm.group(1) if hm else "").strip()
            if href.startswith("http://") or href.startswith("https://"):
                out.append(f'<a href="{_html.escape(href, quote=True)}" target="_blank" rel="noopener noreferrer">')
                stack.append(("a", ["</a>"]))
        elif tag == "a" and closing:
            _rich_close_tag(out, stack, "a")
        pos = m.end()
    emit_text(html[pos:])
    while stack:
        _t, closers = stack.pop()
        for c in closers:
            out.append(c)
    return "".join(out)


_RICH_TEXT_RE = re.compile(r"<[^>]+>")
_RICH_BLOCK_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_RICH_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _rich_par_text(node_html: str) -> str:
    """Public entry: contenteditable innerHTML → stored paragraph text."""
    return _rich_serialize_inline(node_html)


def _md(text: str) -> str:
    """Author-side Markdown subset → safe HTML (repo docs/markdown-text-design.md).

    Block level: #/##/### headings, --- hr, > quote, - / 1. lists, 4-space code,
    blank-line paragraphs. Inline level: **bold**, *em*, _em_, ~~del~~,
    ==highlight== (→ <mark>), `code`, [label](http(s)://…). Everything else is
    escaped plain text — escape-first, so only tags _md itself emits survive."""
    if text is None:
        return ""
    lines = str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[str] = []
    i = 0
    n = len(lines)
    para: list[str] = []

    def flush_para() -> None:
        if para:
            blocks.append("<p>" + _md_inline("\n".join(para).strip()) + "</p>")
            para.clear()

    while i < n:
        line = lines[i]
        m_head = _MD_HEAD.match(line)
        m_hr = _MD_HR.match(line)
        m_quote = _MD_QUOTE.match(line)
        m_ul = _MD_UL.match(line)
        m_ol = _MD_OL.match(line)
        m_code = _MD_CODEBLOCK.match(line)
        if not line.strip():
            flush_para()
            i += 1
            continue
        if m_head:
            flush_para()
            level = min(3, len(m_head.group(1)))
            blocks.append(f"<h{level + 1}>" + _md_inline(m_head.group(2)) + f"</h{level + 1}>")
            i += 1
            continue
        if m_hr:
            flush_para()
            blocks.append('<hr class="md-hr">')
            i += 1
            continue
        if m_quote:
            flush_para()
            quote_lines: list[str] = []
            while i < n and _MD_QUOTE.match(lines[i]):
                quote_lines.append(_MD_QUOTE.match(lines[i]).group(1))
                i += 1
            blocks.append("<blockquote>" + "".join(
                "<p>" + _md_inline(q) + "</p>" for q in quote_lines if q.strip()) + "</blockquote>")
            continue
        if m_ul or m_ol:
            flush_para()
            tag = "ul" if m_ul else "ol"
            items: list[str] = []
            while i < n:
                li = _MD_UL.match(lines[i]) if tag == "ul" else _MD_OL.match(lines[i])
                if not li:
                    break
                items.append("<li>" + _md_inline(li.group(1)) + "</li>")
                i += 1
            blocks.append(f"<{tag}>" + "".join(items) + f"</{tag}>")
            continue
        if m_code and not para:
            code_lines: list[str] = []
            while i < n and _MD_CODEBLOCK.match(lines[i]):
                code_lines.append(lines[i][4:] if lines[i].startswith("    ") else lines[i][1:])
                i += 1
            blocks.append("<pre><code>" + _html.escape("\n".join(code_lines), quote=True) + "</code></pre>")
            continue
        para.append(line)
        i += 1
    flush_para()
    html = "".join(blocks)
    # Everything that is not emitted by _md itself was _html.escape()d at the
    # leaf, so the produced markup is already injection-safe (only <a> carries
    # attributes, and those are fixed rel + validated http(s) href).
    return html


def _insight_text(insight) -> str:
    """Extract the display text of an insight, whether it is a plain string or a
    dict authored with any of the common keys (text/content/description/body/
    summary/finding/title/heading). Returns "" for an unusable item so the
    renderer can fall back to a placeholder instead of an empty bullet."""
    if insight is None:
        return ""
    if isinstance(insight, str):
        return insight
    if isinstance(insight, dict):
        body = insight.get("text") or insight.get("content") or insight.get("description") \
            or insight.get("body") or insight.get("summary") or insight.get("finding") or ""
        title = insight.get("title") or insight.get("heading") or ""
        if title and body:
            return f"{title}：{body}"
        return title or body or ""
    return str(insight)


def _text_block_classes(kind: str, style: dict | None) -> str:
    """Whitelisted text-card skeleton classes (P2).

    Every token maps to a fixed SHELL_CSS rule — nothing arbitrary is ever
    interpolated. ``kind`` is a textKind skeleton (heading/paragraph/callout/
    quote/divider). Per-card surface/accent/radius/border (卡片风格) was removed.
    """
    classes = []
    if kind in ("heading", "callout", "quote", "divider"):
        # paragraph is the base look — no skeleton class (matches the runtime).
        classes.append("t-" + kind)
    return " " + " ".join(classes) if classes else ""


# 段落结构属性白名单（对齐飞书文档段落模型；镜像 editor-runtime.js PARA_LEVELS/
# paraNorm）。level 覆盖 正文/一~九级标题/引用/代码块/高亮块；list（ol/ul/task）、
# align、indent、done 是段落级结构属性，挂在 paragraphs[] 对象上——不进 text 字符串。
_PARA_LEVELS = ("h1", "h2", "h3", "h4", "h5", "h6", "h7", "h8", "h9", "body", "quote", "code", "hlblock")


def _para_norm(p: dict) -> dict:
    """Normalize one paragraph's structural attributes (mirror JS paraNorm).

    list 仅 body 段有效（标题/引用/代码块不与列表组合）；align 缺省 left 不落盘；
    indent 1..8 级（0=无）；done 仅 task 列表有意义。未知值一律收敛到缺省——
    渲染端永不透出白名单外的结构。"""
    level = str(p.get("level") or "body")
    if level not in _PARA_LEVELS:
        level = "body"
    lst = str(p.get("list") or "")
    if lst not in ("ol", "ul", "task") or level != "body":
        lst = ""
    align = str(p.get("align") or "")
    if align not in ("center", "right", "justify"):
        align = ""
    try:
        indent = int(p.get("indent") or 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        indent = 0
    indent = min(8, max(0, indent))
    return {"level": level, "list": lst, "align": align, "indent": indent,
            "done": bool(p.get("done")) and lst == "task"}


def _para_ol_no(paragraphs: list, i: int) -> int:
    """Ordered-list run number: count back over consecutive body+ol paragraphs
    (a bubble strip or any other level breaks the run). Mirror JS paraOlNo."""
    n = 1
    j = i - 1
    while j >= 0:
        q = paragraphs[j]
        if isinstance(q, dict) and not q.get("bubble") \
                and str(q.get("level") or "body") == "body" and str(q.get("list") or "") == "ol":
            n += 1
            j -= 1
        else:
            break
    return n


def _paragraphs_html(paragraphs: list, eyebrow_first: bool = False, tab_num: int | None = None) -> str:
    """Render a text card's virtual paragraphs as editable tx-p divs.

    ``paragraphs`` is a list of {level, text, fmt?, bubble?, list?, align?,
    indent?, done?}; ``fmt:true`` paragraphs carry pre-serialized rich-inline
    HTML (contenteditable WYSIWYG commits — light-Word model,
    docs/markdown-text-design.md §7) and are emitted verbatim; plain text runs
    through the author-side Markdown pipeline. Mirrored 1:1 by editor-runtime.js
    ``paragraphsHtml`` so a runtime re-render (paragraph add/delete/level
    change) never reshapes the card (WYSIWYG). ``eyebrow_first`` (tabs mode)
    demotes the first paragraph to the zero-tracking eyebrow look. ``tab_num``
    prefixes the FIRST h2 title with a non-editable "N. " chip."""
    out: list[str] = []
    paragraphs = list(paragraphs or [])
    n = len(paragraphs)
    i = 0
    while i < n:
        p = paragraphs[i]
        if not isinstance(p, dict):
            p = {"level": "body", "text": str(p)}
        np_ = _para_norm(p)
        text = "" if p.get("text") is None else str(p.get("text"))
        # Mirror editor-runtime.js paragraphsHtml 1:1: 连续的气泡行成组渲染为
        # 一条不换行的横排 .tx-bubble-strip（标题正下方），气泡固定 normal 字号
        # 且永远按正文（body）渲染，禁止段落级。
        if p.get("bubble"):
            run: list[str] = []
            while i < n and isinstance(paragraphs[i], dict) and paragraphs[i].get("bubble"):
                pp = paragraphs[i]
                txx = "" if pp.get("text") is None else str(pp.get("text"))
                run.append(f'<div class="tx-p tx-bubble" data-editable-text data-p-index="{i}">{txx if pp.get("fmt") else _md(txx)}</div>')
                i += 1
            out.append('<div class="tx-bubble-strip">' + "".join(run) + "</div>")
            continue
        # 代码块段落：纯文本转义直出（不解析 Markdown，换行靠 pre-wrap）；其余
        # fmt 段落信任直出；纯文本 body 走块级 Markdown、标题/引用/高亮块只做
        # 行内（块级解析会破坏 data-p-index 单行结构）。
        if np_["level"] == "code":
            inner = text if p.get("fmt") else _html.escape(text, quote=True)
        elif p.get("fmt"):
            inner = text
        else:
            inner = _md(text) if np_["level"] == "body" else _md_inline(text)
        # 列表/任务标记是渲染期派生（存储=list/done 属性），运行时编辑进入时
        # 摘除、提交重渲染时按序号/勾选态补回（序号 = 连排计数）。
        marker = ""
        if np_["list"] == "ol":
            marker = f'<span class="tx-list-no" contenteditable="false">{_para_ol_no(paragraphs, i)}.</span> '
        elif np_["list"] == "task":
            checked = " checked" if np_["done"] else ""
            marker = (f'<input type="checkbox" class="tx-task-box" data-task-for="{i}"'
                      f' contenteditable="false"{checked}>')
        # A 页签 card's first h2 gets the positional number as a non-editable
        # prefix (the numbered look is derived, the stored text stays bare).
        tab_prefix = ""
        if tab_num is not None and i == 0 and np_["level"] == "h2":
            tab_prefix = f'<span class="tx-tab-num" contenteditable="false">{tab_num}.</span> '
        cls = (f"tx-p tx-{np_['level']}"
               + (f" tx-list-{np_['list']}" if np_["list"] else "")
               + (" tx-task-done" if np_["done"] else "")
               + (" tx-eyebrow" if eyebrow_first and i == 0 else ""))
        style = ""
        if np_["align"]:
            style += f"text-align:{np_['align']};"
        if np_["indent"]:
            style += f"padding-left:{np_['indent'] * 24}px;"
        style_attr = f' style="{style}"' if style else ""
        out.append(f'<div class="{cls}"{style_attr} data-editable-text data-p-index="{i}">{marker}{tab_prefix}{inner}</div>')
        i += 1
    return "".join(out)


def _band_paragraphs(b: dict) -> list:
    """Paragraph model for a section band. An explicit ``paragraphs`` list wins;
    else derive from the legacy title/narrative/insight fields so manifests
    authored before the text-card model render identically."""
    paras = b.get("paragraphs")
    if isinstance(paras, list) and paras:
        return [p for p in paras if isinstance(p, dict)]
    if b.get("kind") == "narrative":
        out: list = []
        if str(b.get("title") or "").strip():
            out.append({"level": "h2", "text": b.get("title", "")})
        if str(b.get("narrative") or "").strip():
            out.append({"level": "body", "text": b.get("narrative", "")})
        return out
    if b.get("kind") == "annotation":
        # B4 migration: a chart's authored annotation becomes an independent
        # transparent text block (one body paragraph), matching the runtime
        # addAnnotationBlock paragraph model.
        ann = str(b.get("annotation") or "").strip()
        return [{"level": "body", "text": ann}] if ann else []
    out = [{"level": "h3", "text": "本节小结"}]
    if str(b.get("insight") or "").strip():
        out.append({"level": "body", "text": b.get("insight", "")})
    return out


# 统一文本卡（2026-09-15 收敛 + 同日默认值改版）：textStyle 只有两个「类型」值
# （text/annotation），颜色是正交的 cardColor（色板见 SHELL_CSS .txc-*；常量真源在
# validate_request，经 resolve_page_width 同一先例复用）。**default = 白底**
# （.tx-card 基础即白底）；「透明」是显式色值 none（.txc-none）。
# 旧五值 textStyle 读时归一（注意 transparent→none 才能保住旧透明观感）：
# transparent→text/none、white→text/default、callout→annotation/soft、
# callout-transparent→annotation/none、banner→text/dark——旧 manifest 不必改写。
_LEGACY_CARD_STYLE = {
    "transparent": ("text", "none"),
    "white": ("text", "default"),
    "callout": ("annotation", "soft"),
    "callout-transparent": ("annotation", "none"),
    "banner": ("text", "dark"),
}


def _resolve_card_style(ts: str | None, color: str | None, default_kind: str) -> tuple[str, str]:
    """(kind, color) 归一：新值直读，旧五值映射，未知收敛缺省。"""
    from validate_request import CARD_COLORS
    if ts in _LEGACY_CARD_STYLE:
        kind, legacy_color = _LEGACY_CARD_STYLE[ts]
        return kind, (color if color in CARD_COLORS else legacy_color)
    if ts in ("text", "annotation"):
        return ts, (color if color in CARD_COLORS else "default")
    return default_kind, (color if color in CARD_COLORS else "default")


def _card_classes(kind: str, color: str) -> str:
    """统一组件的 class 片段：tx-card 恒在；批注加 tx-annot；非默认颜色加 txc-*。"""
    cls = "tx-card"
    if kind == "annotation":
        cls += " tx-annot"
    if color != "default":
        cls += " txc-" + color
    return cls


def _band_text_style(b: dict) -> str:
    """Card TYPE (textStyle) for a band: text | annotation（旧五值已归一）。

    ``style.textStyle`` wins; the kind supplies the default (narrative reads as
    the plain text card, insight as the annotation callout, annotation as the
    annotation type matching the runtime addAnnotationBlock)."""
    style = b.get("style") or {}
    kind, _ = _resolve_card_style(style.get("textStyle"), style.get("cardColor"),
                                  "annotation" if b.get("kind") == "annotation" else
                                  ("text" if b.get("kind") == "narrative" else "annotation"))
    return kind


def _band_card_color(b: dict) -> str:
    """Card COLOR (cardColor) for a band: style.cardColor wins; a legacy
    ``style.textStyle`` maps to its paired color (callout→soft …).2026-09-15
    默认值改版后**全部缺省白底**（default）——浅灰只剩旧 callout 显式声明的映射。"""
    style = b.get("style") or {}
    default_kind = ("annotation" if b.get("kind") == "annotation" else
                    ("text" if b.get("kind") == "narrative" else "annotation"))
    _, color = _resolve_card_style(style.get("textStyle"), style.get("cardColor"), default_kind)
    return color


def _insight_paragraphs(insights: list) -> list:
    """Paragraph model for the 关键洞察 card: an h3 title + one body paragraph
    per insight item (placeholder body when the report has no insights)."""
    out: list = [{"level": "h3", "text": "关键洞察"}]
    for item in insights or []:
        text = _insight_text(item)
        if text:
            out.append({"level": "body", "text": text})
    if len(out) == 1:
        out.append({"level": "body", "text": "暂未提供可验证的文字洞察，请结合图表和证据表阅读。"})
    return out


def _strip_enumerator(text: str) -> str:
    """Drop a leading section enumerator (一、二、… / 1. 2. / 1、) so a tab card
    can show a bare title that the positional tab number re-prefixes."""
    s = str(text or "").strip()
    cleared = re.sub(r"^[0-9０-９一二三四五六七八九十百]+[、.．:：]\s*", "", s)
    return cleared or s


_TAB_TITLE_RE = re.compile(r"^[0-9０-９]+[.．]\s*")


def _tab_card_article(b: dict, num: int) -> str:
    """Render a section narrative band as a 页签 (tab) card: a numbered H2 title
    ("N. 大盘走势") + the narrative body. 缺省（cardColor default/none）保持历史
    透明观感（txc-none + 位置序号 tx-tab-num）；显式有色（style.cardColor，同一
    .txc-* 色板）走标准卡盒——序号退场（有卡就不再需要「N.」分隔线语言；运行时
    rerenderTextBlock / applyInlineCardColor 同口径）。类型恒为页签（不渲染
    tx-annot 角线，与 applyCardClassesToWrapper 的页签分支一致）。
    The stored paragraph[0].text stays bare (title only) — the positional
    number is a display prefix so reordering a card renumbers every tab."""
    esc = lambda value: _html.escape(str(value if value is not None else ""), quote=True)
    sid = b.get("section", "")
    paras = _band_paragraphs(b)
    if paras and paras[0].get("level") == "h2":
        title = _TAB_TITLE_RE.sub("", _strip_enumerator(paras[0].get("text")))
        paras[0] = dict(paras[0], text=title)
    style = b.get("style") or {}
    extra = _text_block_classes(b.get("textKind") or "", style)
    color = _band_card_color(b)
    colored = color not in ("default", "none")
    color_cls = ("txc-" + color) if colored else "txc-none"
    tab_num = None if colored else num
    inner = (f'<div class="block tx-card {color_cls} tx-tab-card{extra}" data-text-block data-tab-card data-tab-id="{esc(sid)}">'
             f'{_paragraphs_html(paras, tab_num=tab_num)}'
             "</div>")
    return (f'<article class="grid-stack-item" data-report-block="text" data-report-block-id="block-sec-{esc(sid)}-narrative" '
            f'gs-x="{b.get("x", 0)}" gs-y="{b.get("y", 0)}" gs-w="12" gs-h="{b.get("h", 2)}">'
            f'<div class="grid-stack-item-content">{inner}</div></article>')


def _section_band_article(b: dict, compact_title: bool = False) -> str:
    """HTML for a section narrative/insight text band, or an independent
    annotation text block (B4 migration, chart-width).

    Content is the unified paragraph model (``_band_paragraphs`` →
    ``_paragraphs_html``); the outer class keeps the legacy alias
    (.section-narrative == transparent, .section-insight == callout) plus the
    resolved tx-* style class so a runtime 样式 swap only flips one token.
    ``compact_title`` (tabs mode): the tab bar already names the section, so
    the narrative's heading degrades to a zero-tracking eyebrow paragraph."""
    esc = lambda value: _html.escape(str(value if value is not None else ""), quote=True)
    sid = b.get("section", "")
    extra = _text_block_classes(b.get("textKind") or "", b.get("style") or {})
    paras = _band_paragraphs(b)
    card_cls = _card_classes(_band_text_style(b), _band_card_color(b))
    if b.get("kind") == "annotation":
        # B4 migration: a chart's rendered annotation as its OWN text block
        # (mirrors the runtime addAnnotationBlock: an independent text card that
        # edits via the unified paragraph model, data-p-index → block.paragraphs).
        bid = f"block-ann-{b.get('chart', '')}"
        inner = (f'<div class="block {card_cls}{extra}" data-text-block>'
                 f'{_paragraphs_html(paras)}'
                 "</div>")
        return (f'<article class="grid-stack-item" data-report-block="text" data-report-block-id="{bid}" '
                f'gs-x="{b.get("x", 0)}" gs-y="{b.get("y", 0)}" gs-w="{b.get("w", 6)}" gs-h="{b.get("h", 2)}">'
                f'<div class="grid-stack-item-content">{inner}</div></article>')
    if b.get("kind") == "narrative":
        bid = f"block-sec-{sid}-narrative"
        # card_cls honoured like the annotation branch (it already contains
        # tx-card + any tx-annot/txc-* delta): build_manifest persists
        # textStyle/cardColor for narrative bands, so the static grid
        # (runtime.tabs="off") must carry the resolved class too.
        inner = (f'<div class="block section-narrative {card_cls}{extra}" data-text-block>'
                 f'{_paragraphs_html(paras, eyebrow_first=compact_title)}'
                 "</div>")
    else:
        bid = f"block-sec-{sid}-insight"
        inner = (f'<div class="block section-insight {card_cls}{extra}" data-text-block>'
                 f'{_paragraphs_html(paras)}'
                 "</div>")
    return (f'<article class="grid-stack-item" data-report-block="text" data-report-block-id="{bid}" '
            f'gs-x="{b.get("x", 0)}" gs-y="{b.get("y", 0)}" gs-w="{b.get("w", 12)}" gs-h="{b.get("h", 2)}">'
            f'<div class="grid-stack-item-content">{inner}</div></article>')


def _authored_cover(cov: dict | None) -> dict:
    """渲染端封面归一：剥掉 dataAsOf 后再无作者内容 = 没有封面。

    build_manifest 的 dataAsOf 投影（2026-09-28 前的版本）曾把 free 无声明封面
    的 `or {}` 空 dict 写成 truthy——那份 manifest 已烘焙进旧产物（server 模式
    每次渲染现读 entry manifest），渲染端在此自愈：只剩 dataAsOf 的 cover 视同
    无封面，读者不再看到一条孤立的「数据截至」横幅。meta.dataAsOf（tabbar
    徽章/汇报页脚的真源）不受影响。"""
    if not isinstance(cov, dict) or not cov:
        return {}
    if set(cov) - {"dataAsOf"}:
        return cov
    return {}


def render_grid_html(manifest: dict, placed: list[tuple[dict, int, int]], cover_h: int,
                     bands: list | None = None) -> str:
    """Render the deterministic grid body (cover → filter → KPI → charts →
    insight) from the manifest. This is the SINGLE source of truth for the
    report body — both the singlefile compiler and the server template reuse it,
    so layouts cannot drift. ``manifest`` carries cover/kpis/insights/filters/
    charts/datasets/sections; ``placed`` carries (chart, x, y) from
    layout_sections; ``bands`` (optional) carries the section narrative/insight
    text bands from layout_sections. Without bands the body is byte-identical to
    the flat template."""
    import math
    esc = lambda value: _html.escape(str(value if value is not None else ""), quote=True)
    cov = _authored_cover(manifest.get("cover"))
    kpis = manifest.get("kpis") or []
    kpi_html = ""
    kpi_rows = max(1, (len(kpis) + 3) // 4) if kpis else 0
    filter_items = _flatten_filters(manifest.get("filters") or [])
    filter_chips = "".join(_filter_chip_html(f, esc) for f in filter_items)
    filter_h = _filter_h(max(len(filter_items), len(manifest.get("filterCandidates") or [])))
    y_filter = cover_h  # filter bar sits above the (compact) KPI row
    # kpiPosition="bottom"（free 限定）：KPI 条移到图表/文本区、洞察卡之后。
    # placed/bands 底部先确定性算出，KPI gs-y 与 max_y 记账同源。
    _kpi_at_bottom = manifest.get("kpiPosition") == "bottom"
    _placed_bottom = max((y + ((c.get("layout") or {}).get("h") or _default_h(c)) for c, _x, y in placed), default=0)
    _bands_bottom = max((b.get("y", 0) + b.get("h", 0) for b in (bands or [])), default=0)
    if _kpi_at_bottom:
        # KPI 条在洞察卡之下（insight 渲染于 y_ins=max_y，高 _insight_h）——
        # 与下方 insight article 的 gs-h 同一函数，几何不漂。insights 在下方
        # 才赋值，这里直读 manifest。
        _insights_now = manifest.get("insights") or []
        kpi_y0 = max(_placed_bottom, _bands_bottom, cover_h + filter_h) + (_insight_h(_insights_now) if _insights_now else 0)
        max_y0 = cover_h + filter_h
    else:
        kpi_y0 = cover_h + filter_h
        max_y0 = cover_h + filter_h + kpi_rows * _KPI_H
    # 2026-09-28「全部放开」：kpi 条按每行 4 张折行渲染全部声明卡（旧 kpis[:4]
    # 硬截断是 standard ≤4 上限时代的防御，free 12 张的 5-12 号曾被静默丢弃）。
    for k, kp in enumerate(kpis):
        acc = kp.get("accent", "")
        display, kind, unit = _format_kpi(kp, manifest=manifest)
        raw = kp.get("rawValue")
        raw_attr = "" if raw is None else f' value="{esc(raw)}"'
        unit_html = f'<span class="unit">{esc(unit)}</span>' if unit else ""
        kpi_html += (f'<article class="grid-stack-item" data-report-block="kpi" data-report-block-id="block-kpi-{k}" '
                     f'gs-x="{k % 4 * 3}" gs-y="{kpi_y0 + (k // 4) * _KPI_H}" gs-w="3" gs-h="{_KPI_H}"><div class="grid-stack-item-content">'
                     f'<div class="kpi-card {esc(acc)}{_text_block_classes("", kp.get("style"))}"><span class="label" data-editable-text>{esc(kp.get("label", "指标"))}</span>'
                     f'<data class="value" data-kpi-kind="{esc(kind)}"{raw_attr}>{esc(display)}{unit_html}</data>'
                     f'<div class="accent-bar"></div></div></div></article>')
    max_y = max_y0
    chart_html = ""
    chart_articles: dict[str, str] = {}
    for c, x, y in placed:
        if c.get("viewType") == "text":
            # free texts 伪卡 → 独立文本卡（段模型），绝不建 chart-host。
            article = _text_block_article(c, x, y)
            chart_html += article
            max_y = max(max_y, y + ((c.get("layout") or {}).get("h") or 3))
            continue
        bid = c.get("blockId") or ("block-" + c.get("id", "chart"))
        vt = c.get("viewType")
        inner = f'<div id="{esc(c.get("elementId", c.get("id")))}" class="chart-host"></div>'
        if vt == "table":
            cols = (c.get("data") or {}).get("columns") or []
            rows = _sorted_rows(c)
            th = "".join(f"<th class='num'>{esc(col)}</th>" for col in cols[1:])
            first = f"<th>{esc(cols[0])}</th>" if cols else ""
            trs = "".join("<tr class='striped'><td>" + esc(str(r[0])) + "</td>" +
                           "".join(f"<td class='num'>{esc(_fmt_cell(v, cols[j], c))}</td>" for j, v in enumerate(r[1:], 1)) +
                           "</tr>" for r in rows)
            inner = (f'<div id="{esc(c.get("elementId", c.get("id")))}" class="table-host"><table class="ai-report-table striped">'
                     f'<thead><tr>{first}{th}</tr></thead><tbody>{trs}</tbody></table></div>')
        w = _chart_width(c)
        h = (c.get("layout") or {}).get("h", _default_h(c))
        ann = (c.get("annotation") or "").strip()
        # 内联批注已移除：批注改由数据卡「添加批注」下拉创建为独立透明文本块
        # （见 editor-runtime.js addAnnotationBlock），不再在图表卡内部渲染。
        ann_html = ""
        _style_cls = _text_block_classes("", c.get("style"))
        _ann_pos = ""
        article = (f'<article class="grid-stack-item" data-report-block="chart" data-report-block-id="{esc(bid)}" '
                   f'gs-x="{x}" gs-y="{y}" gs-w="{w}" gs-h="{h}"><div class="grid-stack-item-content">'
                   f'<div class="block{_style_cls}{_ann_pos}"><h4 class="block-title" data-editable-text data-chart-title-for="{esc(c.get("id", ""))}">{esc(c.get("title", ""))}</h4>'
                   f'{inner}{ann_html}</div></div></article>')
        chart_html += article
        chart_articles[bid] = article
        max_y = max(max_y, y + h)
    if bands:
        for b in bands:
            max_y = max(max_y, b["y"] + b["h"])
    insights = manifest.get("insights") or []
    # 关键洞察 renders through the unified paragraph model (h3 title + one body
    # paragraph per insight); body text still flows through the author-side
    # Markdown pipeline inside _paragraphs_html.
    insight_paras = _insight_paragraphs(insights)
    y_ins = max(max_y, y_filter + filter_h)
    # free 档：封面声明制（manifest.cover 为空 = 没有封面卡）；standard 档恒有
    # （normalize_request 自动组装，cov 恒真值）。
    body = ([_cover_article_html(cov, cover_h)] if cov else [])
    # P1: only render the filter card when there ARE filters (filter_h >= 1).
    # Without filters the toolbar popover is the sole entry point and the
    # grid's first row gains 56px.
    if filter_h >= 1:
        body.append(_filter_card_article("block-filter", "global", filter_chips, y_filter, filter_h, esc))
    body.append(kpi_html)
    if bands:
        # Story sections: merge section text bands with chart cards in ascending
        # y order so the DOM reads the same top-to-bottom story as the grid.
        flow = _flow(placed, bands)
        mid = []
        for step in flow:
            if step[0] == "band":
                mid.append(_section_band_article(step[1]))
            else:
                _c = step[1]
                mid.append(chart_articles.get(_c.get("blockId") or ("block-" + _c.get("id")), ""))
        body.append("".join(mid))
    else:
        body.append(chart_html)
    # 关键洞察卡：standard 档恒渲染（历史契约）；free 档声明制——无 insights
    # 就没有卡，明细卡的定位随之改挂在图表底（max_y）上。
    has_insight = bool(insights) or manifest.get("templateId") != "free-v1"
    if has_insight:
        body.append(f'<article class="grid-stack-item" data-report-block="text" data-report-block-id="block-insight" gs-x="0" gs-y="{y_ins}" gs-w="12" gs-h="{_insight_h(insights)}"><div class="grid-stack-item-content"><div class="insight-block tx-card tx-annot" data-text-block>{_paragraphs_html(insight_paras)}</div></div></article>')
    # Optional editable data table (多维表格-style). Rendered only when the
    # manifest requests a detail block (spec.dataTable); the runtime fills the
    # full editable grid (toolbar + editable cells + row add/delete) into host.
    if any(b.get("id") == "block-detail" for b in manifest.get("blocks", [])):
        insight_h = _insight_h(insights)
        detail_y = (y_ins + insight_h) if has_insight else max_y
        # kpiPosition=bottom：明细卡让到 KPI 条之下，不与条重叠。
        if _kpi_at_bottom:
            detail_y = max(detail_y, kpi_y0 + kpi_rows * _KPI_H)
        detail_h = 12
        body.append(f'<article class="grid-stack-item" data-report-block="table" data-report-block-id="block-detail" gs-x="0" gs-y="{detail_y}" gs-w="12" gs-h="{detail_h}"><div class="grid-stack-item-content"><div class="block"><h4 class="block-title">明细数据</h4><div class="ai-report-detail-toolbar"></div><div class="table-host" data-report-detail></div></div></div></article>')
    # 拼版占位卡（kind:'compose'，docs/compose-canvas-design.md）：自带显式
    # layout 坐标（客户端插入时已定），按位渲染即可。占位块是「未完成声明」，
    # 不进任何图表/ECharts 管道；转正（Agent 翻译构建）后由真实卡片替换。
    for b in manifest.get("blocks", []):
        if b.get("kind") != "compose":
            continue
        body.append(_compose_block_article(b, esc))
    return "".join(body)


# 拼版占位卡的类型标签（与 editor-runtime.js COMPOSE_TYPE_LABELS 逐对一致；
# tests/test_compose_draft.cjs 断言两端 parity——只许同时改）。
COMPOSE_TYPE_LABELS = {
    "": "自动（按数据形态）",
    "bar": "柱状/折线", "line": "折线", "stackedBar": "堆叠柱",
    "pie": "饼图", "donut": "环形图", "funnel": "漏斗",
    "scatter": "散点", "heatmap": "热力", "table": "表格",
}
_COMPOSE_SLOT_META = {
    "chart": ("图", "图表占位"),
    "kpi": ("K", "指标占位"),
    "text": ("文", "文本占位"),
    "table": ("表", "表格占位"),
}


def _compose_block_article(block: dict, esc) -> str:
    compose = block.get("compose") or {}
    slot = compose.get("slot") if compose.get("slot") in _COMPOSE_SLOT_META else "chart"
    icon, slot_label = _COMPOSE_SLOT_META[slot]
    ctype = compose.get("chartType") or ""
    type_label = COMPOSE_TYPE_LABELS.get(ctype, ctype)
    nl = str(compose.get("nl") or "").strip()

    def _int_or(v: Any, default: int) -> int:
        # 在线保存的 blocks 原样落盘，手改 manifest 可能把 layout 写成非数字——
        # 渲染端（GET /reports/<id> 每请求走这里）解析失败必须收敛缺省，
        # 绝不让整份报告 500 打不开（第七轮审视挂账收口）。
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    lay = block.get("layout") or {}
    w = _int_or(lay.get("w") or 6, 6)
    h = _int_or(lay.get("h") or 6, 6)
    nl_html = (f'<div class="ai-compose-nl">{esc(nl)}</div>' if nl
               else '<div class="ai-compose-nl is-empty">点此描述这一块要什么（也可直接绑定字段）</div>')
    meta = slot_label if not ctype else f"{slot_label} · {type_label}"
    return (f'<article class="grid-stack-item" data-report-block="compose" data-report-block-id="{esc(block.get("id"))}" '
            f'gs-x="{_int_or(lay.get("x") or 0, 0)}" gs-y="{_int_or(lay.get("y") or 0, 0)}" gs-w="{w}" gs-h="{h}">'
            f'<div class="grid-stack-item-content"><div class="ai-compose-card" data-compose-slot="{esc(slot)}">'
            f'<span class="ai-compose-badge">拼版占位</span>'
            f'<span class="ai-compose-icon">{icon}</span>{nl_html}'
            f'<div class="ai-compose-meta">{esc(meta)}</div>'
            f'</div></div></article>')


def _insight_h(insights: list) -> int:
    """Grid height for the 关键洞察 card, estimated from content (title +
    wrapped bullet lines) instead of a fixed 5-row block that leaves a gap.

    Same display-width line counter as the section bands (``_text_lines``) — a
    bullet with mixed CJK/Latin text must not be counted by character."""
    texts = [_insight_text(i) for i in insights] or [""]
    lines = sum(_text_lines(t) for t in texts)
    # padding 28 = tx-card 上下 12px×2 + 边框 2px + 余量（旧 insight-block 20px 大衬里的 42 已随
    # 「与批注一致」改版退役，2026-09-16）。
    px = 30 + lines * 22 + 8 * max(0, len(texts) - 1) + 28  # h3 + li lines + gaps + padding
    return _text_rows(px, 3, 8)


def _derived_percent_units(c: dict, col: str) -> bool:
    """PY mirror of ``chart-adapter.derivedPercentUnits``: True when the column
    is an aggregate-level derived measure whose stored values are ALREADY
    percent-scaled (share / window+rate) — ``_fmt_cell`` must not ×100 again
    (the old "1.6% shows as 160%" export/runtime incident, compile side)."""
    n = (c.get("_derived_nodes") or {}).get(col)
    return bool(n) and (n.get("kind") == "share"
                        or (n.get("kind") == "window" and n.get("output") == "rate"))


def _fmt_cell(v, col, c):
    mt = (c.get("measureTypes") or {}).get(col)
    fmt = None
    if isinstance(mt, dict):
        t = mt.get("type")
        fmt = mt.get("format")
    elif isinstance(mt, str):
        t = mt
    else:
        t = None
    # P5: format='percent' wins over the type-based formatter (a currency
    # column authored with format='percent' renders as %, dropping the ¥
    # prefix — the canonical P1 normalised shape after we retired the
    # keyword auto-inference).
    # Null contract: an empty group (sum/average/min/max over zero numeric
    # rows → None) stays empty — _num(None) is 0.0 and would fabricate "0.00%"
    # / "¥0.0万" / a literal "None" in the static table (runtime renders null
    # as empty, so the first paint must agree).
    if v is None or v == "":
        return ""
    if fmt == "percent" or t == "percent":
        # 契约 = 原始比率（0.052）×100 显示（与运行时 fmtVal / _format_fact /
        # _format_kpi 同一口径）；share / window(rate) 派生指标在求值时**已
        # ×100**（derivedPercentUnits 豁免），再乘就是 1.6% → 160% 的旧事故。
        _scale = 1 if _derived_percent_units(c, col) else 100
        return f"{_num(v) * _scale:.2f}%"
    if t == "currency":
        # 币种符号（拍板 1/6）：symbol > 内置币表 > ¥ 缺省，与运行时
        # measureFormat 同一解析（type_registry.currency_symbol 单一真源）。
        _sym = currency_symbol(mt)
        # 分层单位（<1e4 直接显示元）：旧版恒除以 1e4，40 → "¥0.0万"、500 →
        # "¥0.1万"，万元级以下的静态表格卡全部错误数量级。与 _money 同一分层
        # 规则（负数先取绝对值判档、符号照抄）。
        _n = _num(v)
        _a = abs(_n)
        if _a >= 1e8:
            return f"{_sym}{_n/1e8:,.2f}亿"
        if _a >= 1e4:
            return f"{_sym}{_n/1e4:,.1f}万"
        return f"{_sym}{_n:,.0f}"
    if isinstance(v, float) and math.isfinite(v) and v != int(v):
        return f"{v:,.2f}"
    return f"{v:,}" if isinstance(v, (int, float)) else str(v)

# ---------- chart-type x data-shape compatibility (Flint throw-before-render) ----------


# ---------- workbench tabs (repo repo docs/workbench-tabs-design.md) --------------------
def _tabs_requested(spec: dict) -> str:
    return (spec.get("runtime") or {}).get("tabs", "auto")


def tabs_enabled(spec: dict) -> bool:
    """True when the report renders as workbench tabs (true Tab views).

    现在强制所有报告都启用页签（workbench tabs）作为默认渲染形式。
    之所以从“默认开”变为“始终强制”：
    - 页签里的筛选器作用范围 / 脚本上下文与页签绑定，没有页签会出现
      筛选含义不一致、全局筛选不生效等歧义。
    - 汇报模式 (PPT 分页) 是按 page-by-page 跳页，页签为它提供了
      “这一页属于哪个区块”的可选粒度。
    - 多区块报告里页签是避免“信息被单页长流漏掉”的唯一靠谱路径。
    Agent / 用户依旧可以通过 runtime.tabs="off" 明确关闭。"""
    want = _tabs_requested(spec)
    if want == "off":
        return False
    return True


def _ensure_default_sections(spec: dict) -> dict:
    """若 spec 没有 sections，但开启了 tabs，合并所有图表到一个默认「正文」页签，
    让所有报告都能被 tabs 正确渲染。
    - 已传入 sections：原样返回。
    - 没传 sections 且有图表：生成一个 id='section-body'、title='正文' 的
      section，把所有图表 id 写入 ``charts`` 字段（validate_request 接受的
      字段名）。
    - 没传 sections 也没图表：生成一个空的「正文」section，避免渲染出空 tabs。
    返回新 spec（不修改入参）。"""
    if spec.get("sections"):
        return spec
    if _tabs_requested(spec) == "off":
        return spec
    # free-v1（声明即全部，docs/free-authoring-discussion.md §2）：未声明
    # sections 且未显式请求页签（runtime.tabs 缺省 auto）时不合成「正文」默认
    # 节——自动合成是模板行为，会往极简 free 报告里塞一张无内容的「正文」页签
    # 卡。显式 runtime.tabs="on" 视为作者要页签，合成照旧；走平铺渲染路径
    # （render_grid_html），筛选卡本就以 scope:"global" 声明，无页签歧义。
    if _is_free(spec) and _tabs_requested(spec) == "auto":
        return spec
    charts = spec.get("charts") or []
    section = {
        "id": "section-body",
        "title": "正文",
        "narrative": "",
        "insight": "",
        "charts": [c.get("id") for c in charts if c.get("id")],
    }
    new_spec = dict(spec)
    new_spec["sections"] = [section]
    return new_spec


def _tabs_chart_article(manifest: dict, c: dict, x: int, y: int) -> str:
    esc = lambda value: _html.escape(str(value if value is not None else ""), quote=True)
    if c.get("viewType") == "text":
        # free texts 伪卡：页签 body 里的独立文本卡，与平铺渲染同一 article。
        return _text_block_article(c, x, y)
    bid = c.get("blockId") or ("block-" + c.get("id", "chart"))
    vt = c.get("viewType")
    inner = f'<div id="{esc(c.get("elementId", c.get("id")))}" class="chart-host"></div>'
    if vt == "table":
        cols = (c.get("data") or {}).get("columns") or []
        rows = _sorted_rows(c)
        th = "".join(f"<th class='num'>{esc(col)}</th>" for col in cols[1:])
        first = f"<th>{esc(cols[0])}</th>" if cols else ""
        trs = "".join("<tr class='striped'><td>" + esc(str(r[0])) + "</td>" +
                       "".join(f"<td class='num'>{esc(_fmt_cell(v, cols[j], c))}</td>" for j, v in enumerate(r[1:], 1)) +
                       "</tr>" for r in rows)
        inner = (f'<div id="{esc(c.get("elementId", c.get("id")))}" class="table-host"><table class="ai-report-table striped">'
                 f'<thead><tr>{first}{th}</tr></thead><tbody>{trs}</tbody></table></div>')
    w = _chart_width(c)
    h = (c.get("layout") or {}).get("h", _default_h(c))
    ann = (c.get("annotation") or "").strip()
    # 内联批注已移除（批注改由「添加批注」创建为独立文本块），不再在图表卡内渲染。
    ann_html = ""
    _ann_pos = ""
    return (f'<article class="grid-stack-item" data-report-block="chart" data-report-block-id="{esc(bid)}" '
            f'gs-x="{x}" gs-y="{y}" gs-w="{w}" gs-h="{h}"><div class="grid-stack-item-content">'
            f'<div class="block{_text_block_classes("", c.get("style"))}{_ann_pos}"><h4 class="block-title" data-editable-text data-chart-title-for="{esc(c.get("id", ""))}">{esc(c.get("title", ""))}</h4>'
            f'{inner}{ann_html}</div></div></article>')


def _tabs_view_charts(spec: dict) -> dict:
    """Chart objects grouped by view: {'overview': [], <section-id>: [...], 'body': [...]}."""
    charts = spec.get("charts") or []
    by_id = {c["id"]: c for c in charts}
    views: dict = {"overview": []}
    used: set = set()
    for sec in spec.get("sections") or []:
        ids = [cid for cid in (sec.get("chartIds") or []) if cid in by_id]
        views[sec["id"]] = [by_id[cid] for cid in ids]
        used.update(ids)
    views["body"] = [c for c in charts if c["id"] not in used]
    return views


def render_tabs_html(spec: dict, manifest: dict, cover_h: int) -> str:
    """Tabbed body as ONE continuous grid: cover → filter → KPI → 关键洞察
    (the 概览 head), then per section a 页签 (tab) card + its charts + insight
    band, then unassigned/detail. The top tab bar is derived from the 页签
    cards (bare title, numbered positionally on the card). The runtime toggles
    合并页签显示: merged (default) stacks everything and a tab is a scroll
    anchor; unchecking hides blocks outside the active tab range."""
    esc = lambda value: _html.escape(str(value if value is not None else ""), quote=True)
    views = _tabs_view_charts(spec)
    sections = {s["id"]: s for s in (spec.get("sections") or [])}
    sections_list = list(spec.get("sections") or [])
    insights = manifest.get("insights") or []
    insight_paras = _insight_paragraphs(insights)
    insight_h = _insight_h(insights)
    has_detail = any(b.get("id") == "block-detail" for b in manifest.get("blocks", []))

    # ---- overview head (cover/filter/kpi + 关键洞察) --------------------------
    cov = _authored_cover(manifest.get("cover"))
    kpis = manifest.get("kpis") or []
    kpi_rows = max(1, (len(kpis) + 3) // 4) if kpis else 0
    filter_items = _flatten_filters(manifest.get("filters") or [])
    filter_chips = "".join(_filter_chip_html(f, esc) for f in filter_items)
    filter_h = _filter_h(max(len(filter_items), len(manifest.get("filterCandidates") or [])))
    kpi_y0 = cover_h + filter_h
    _kpi_at_bottom = manifest.get("kpiPosition") == "bottom"
    kpi_html = ""
    # 「全部放开」：kpi 条渲染全部声明卡并按每行 4 张折行（旧 kpis[:4] 截断退役）。
    for k, kp in enumerate(kpis):
        acc = kp.get("accent", "")
        display, kind, unit = _format_kpi(kp, spec, manifest)
        raw = kp.get("rawValue")
        raw_attr = "" if raw is None else f' value="{esc(raw)}"'
        unit_html = f'<span class="unit">{esc(unit)}</span>' if unit else ""
        kpi_html += (f'<article class="grid-stack-item" data-report-block="kpi" data-report-block-id="block-kpi-{k}" '
                     f'gs-x="{k % 4 * 3}" gs-y="{kpi_y0 + (k // 4) * _KPI_H}" gs-w="3" gs-h="{_KPI_H}"><div class="grid-stack-item-content">'
                     f'<div class="kpi-card {esc(acc)}{_text_block_classes("", kp.get("style"))}"><span class="label" data-editable-text>{esc(kp.get("label", "指标"))}</span>'
                     f'<data class="value" data-kpi-kind="{esc(kind)}"{raw_attr}>{esc(display)}{unit_html}</data>'
                     f'<div class="accent-bar"></div></div></div></article>')
    # free 档：概览头三件套全声明制——封面/洞察缺席时不渲染、不占高；全部缺席
    # 时概览段整个消失（页签从第一个 section 开始）。
    _free = manifest.get("templateId") == "free-v1"
    _has_insight = bool(insights) or not _free
    if not _has_insight:
        insight_h = 0
    overview = ([_cover_article_html(cov, cover_h)] if cov else [])
    if filter_h >= 1:
        overview.append(_filter_card_article("block-filter", "global", filter_chips, cover_h, filter_h, esc))
    # kpiPosition=bottom（页签模式）：概览头顺序改为 封面→筛选→洞察→KPI 条——
    # 条在页签 body 的内容之后没有单一 y（body 逐页签排版），落在概览尾是
    # 「KPI 收尾」语义的页签化表达。
    if kpis and not _kpi_at_bottom:
        overview.append(kpi_html)
    y_ov_ins = cover_h + filter_h + (0 if _kpi_at_bottom else kpi_rows * _KPI_H)
    if _has_insight:
        overview.append(f'<article class="grid-stack-item" data-report-block="text" data-report-block-id="block-insight" gs-x="0" gs-y="{y_ov_ins}" gs-w="12" gs-h="{insight_h}"><div class="grid-stack-item-content"><div class="insight-block tx-card tx-annot" data-text-block>{_paragraphs_html(insight_paras)}</div></div></article>')
    if kpis and _kpi_at_bottom:
        overview.append(kpi_html)

    # ---- single-grid body: tab card per section + content ---------------------
    seg = ["overview"] if overview else []
    y = cover_h + filter_h + (0 if _kpi_at_bottom else kpi_rows * _KPI_H) + insight_h
    body_html = []
    body_charts = views.get("body") or []
    # free texts 伪卡跟未分配图表同一 body 流（texts 不进 sections）。
    body_charts = _merge_text_pseudos(body_charts, _text_pseudo_items(spec.get("texts") if isinstance(spec, dict) else None))
    for idx, sec in enumerate(sections_list):
        seg.append(sec["id"])
        num = idx + 1
        sec_charts = views.get(sec["id"], [])
        placed, bands = layout_sections(sec_charts, [sec], start_y=y)
        piece = []
        for step in _flow(placed, bands):
            if step[0] == "band":
                b = step[1]
                if b["kind"] == "narrative":
                    piece.append(_tab_card_article(b, num))
                else:
                    piece.append(_section_band_article(b))
            else:
                piece.append(_tabs_chart_article(manifest, step[1], step[2], step[3]))
        body_html.append("".join(piece))
        y = max([y] + [gy + ((c.get("layout") or {}).get("h") or _default_h(c)) for c, gx, gy in placed] + [b.get("y", 0) + b.get("h", 0) for b in bands])
    # Unassigned charts + detail form the 明细数据 tail.
    has_body = bool(body_charts) or has_detail
    if has_body:
        seg.append("body")
        body_placed, body_bands = layout_sections(body_charts, [], start_y=y)
        tail = [_tabs_chart_article(manifest, c, x, gy) for c, x, gy in body_placed]
        for b in body_bands:
            tail.append(_section_band_article(b))
        # 明细尾段的起点也必须计入批注/小结文本带（body_bands）——旧版只 max
        # 图表底，未分配图表的批注带会与明细数据卡重叠。
        y_body = max([y] + [gy + ((c.get("layout") or {}).get("h") or _default_h(c)) for c, x, gy in body_placed]
                     + [b.get("y", 0) + b.get("h", 0) for b in body_bands])
        if has_detail:
            tail.append(f'<article class="grid-stack-item" data-report-block="table" data-report-block-id="block-detail" gs-x="0" gs-y="{y_body}" gs-w="12" gs-h="12"><div class="grid-stack-item-content"><div class="block"><h4 class="block-title">明细数据</h4><div class="ai-report-detail-toolbar"></div><div class="table-host" data-report-detail></div></div></div></article>')
        body_html.append("".join(tail))

    # ---- tab bar (bare section titles, no positional number) ------------------
    def label(vid: str) -> str:
        if vid == "overview":
            return "概览"
        if vid == "body":
            return "明细数据"
        sec = sections.get(vid)
        t = (sec.get("title") or vid) if sec else vid
        return _strip_enumerator(t) or t
    tabs_html = ['<nav class="ai-report-tabbar" data-report-tabbar>']
    for vid in seg:
        active = " ai-report-tab-active" if vid == "overview" else ""
        tabs_html.append(f'<button type="button" class="ai-report-tab{active}" data-tab-go="{esc(vid)}" data-tab-label="{esc(label(vid))}">{esc(label(vid))}</button>')
    tabs_html.append('<button type="button" class="ai-report-tab-add" data-tab-add>＋ 页签</button>')
    tabs_html.append("</nav>")
    # ONE grid (the single continuous flow); every block carries data-block-tab
    # so the runtime can partition/hide ranges in split mode.
    return ("".join(tabs_html)
            + f'<section class="grid-stack ai-report-tab-view" data-report-grid data-tab-view="overview">'
            + "".join(overview) + "".join(body_html)
            + "</section>")


# ---------- chart-type x data-shape compatibility (Flint throw-before-render) ----------
_SHAPE_RULES = {
    "funnel": {"min_measures": 1, "note": "needs one measure (stage counts)"},
    "pie": {"min_measures": 1, "note": "needs one measure"},
    "donut": {"min_measures": 1, "note": "needs one measure"},
    "scatter": {"min_measures": 2, "note": "needs >=2 measures (x, y)"},
    "area": {"min_measures": 1, "note": "needs >=1 measure"},
    "bar": {"min_measures": 1, "note": "needs >=1 measure"},
    "stackedBar": {"min_measures": 1, "note": "needs >=1 measure"},
    "line": {"min_measures": 1, "note": "needs >=1 measure"},
    "table": {},
}


def validate_chart_shape(c: dict) -> list[str]:
    """Return a list of problems; empty means the chart shape is valid."""
    problems = []
    vt = c.get("viewType")
    rule = _SHAPE_RULES.get(vt)
    if rule is None:
        problems.append(f"viewType '{vt}' is not supported")
        return problems
    cols = (c.get("data") or {}).get("columns") or []
    rows = (c.get("data") or {}).get("rows") or []
    measures = c.get("measures") or []
    dimension = c.get("dimension") or (cols[0] if cols else "")
    if not rows:
        # Actionable diagnosis: the most common cause (a real authoring run lost
        # ~6 build cycles to it) is request filters that eliminate every row —
        # e.g. `{"op": "in"}` with no value used to match nothing.
        filt = c.get("filter")
        hint = ""
        if filt:
            try:
                _ftext = json.dumps(filt, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                _ftext = str(filt)
            hint = (f"; 当前 filters 把所有行都过滤掉了，请检查 {_ftext[:200]}"
                    "（无 value 的条件 = 不加约束；有 value 但匹配不上任何行才会清空图表）")
        problems.append("chart has no data rows" + hint)
        return problems
    if dimension not in cols:
        problems.append(f"dimension '{dimension}' not in columns {cols}")
    for m in measures:
        if m != COUNT_MEASURE and m not in cols:
            problems.append(f"measure '{m}' not in columns {cols}")
    if rule.get("min_measures") and len(measures) < rule["min_measures"]:
        problems.append(f"{vt} {rule['note']} (got {len(measures)} measures)")
    # funnel/pie/donut with 2+ measures: only the first is used -> warn-hard
    if vt in ("funnel", "pie", "donut") and len(measures) > 1:
        problems.append(f"{vt} only uses measures[0]; extra measures {measures[1:]} are ignored")
    # line with a categorical (non-temporal) dimension and sort != time: suspicious
    if vt == "line" and c.get("sort") not in ("time", "none") and _dimension_type(c) not in ("time", "ordinal"):
        problems.append("line chart without a temporal x axis or explicit sort; use sort:time for trends")
    # Non-additive measures (rates/percentages) must never be summed or stacked (P1).
    # 占比堆叠豁免（2026-10-13）：stackMode='normalize' 时堆叠是「归一显示」而非
    # 把段值当总量读——Flint resolveStackable 对 Percentage 的 normalize 形态；
    # sum 聚合在归一下变为「段占总和的权重」，口径成立。
    _norm_stack = vt == "stackedBar" and c.get("stackMode") == "normalize"
    if vt == "stackedBar" and not _norm_stack:
        bad = _non_additive(c)
        if bad[0]:
            problems.append(f"stackedBar with non-additive measure: {bad[1]}; use a grouped bar or average")
    agg = c.get("aggregation")
    _derived_names = set(c.get("derivedMeasures") or [])
    if agg == "sum" and not _derived_names and not _norm_stack:
        # A DERIVED measure is computed AFTER aggregation (formula/window), never
        # re-summed from rows, so an 'sum' chart aggregation cannot double-count it.
        # The rule exists to stop summing percentages that come from the data.
        na, hint = _non_additive(c)
        if na:
            problems.append(f"aggregation 'sum' on non-additive measure: {hint}; use 'average' or 'none'")
    # A funnel needs at least 3 ordered stages to be a meaningful conversion flow (P1).
    if vt == "funnel":
        labels = {str(r[0]) for r in rows}
        if len(labels) < 3:
            problems.append("funnel needs >=3 distinct ordered stages")
    # Scatter needs 2+ quantitative measures (real value axes).
    if vt == "scatter" and len(measures) < 2:
        problems.append(f"{vt} needs >=2 measures (x and y axes)")
    return problems


# ---------- CLI ----------

def infer_measure_types(columns):
    """Heuristic semantic types from column names, so BI-inserted charts and
    authored charts share formatting. Authors may override in the spec's
    dataset.measureTypes. Reuses the family->canonical map from
    ``measure_semantics`` so every path (validate, compile, runtime) agrees.

    2026-10-13 批（B2 双轨合一）：整词匹配（resolve_t2）之外补**子串**推断
    （semantic_label_of——与缺省聚合/编译闸同一推断源）。此前『订单金额』
    『官方售价』这类真实列名整词匹配不中，类型永缺席（L14 提示兜底），编辑器
    侧格式化/不可加保护全盲。子串档只收 **Price/Revenue/Cost**（可加性语义
    修正 + ¥ 格式化）；两个家族刻意不收——率族（P1 裁决：百分比是显示格式
    不是类型，test_keyword_heuristic_retired 钉着）与量词族（『数』太泛，
    自动 typing 会造成显示漂移）。
    """
    from measure_semantics import normalize_measure_type, semantic_label_of
    types = {}
    for col in columns:
        canon = normalize_measure_type(col or "")
        if canon is None:
            label = semantic_label_of(None, col)
            entry = {
                "Price": {"type": "price", "divisor": 10000, "unit": "万"},
                "Revenue": {"type": "currency", "divisor": 10000, "unit": "万"},
                "Cost": {"type": "currency", "divisor": 10000, "unit": "万"},
            }.get(label)
            if entry is None:
                # A name gives no signal (or a deliberately retired family);
                # leave to the storage-type fallback (number).
                continue
            types[col] = entry
        else:
            types[col] = canon
    return types


def _load_theme(spec: dict) -> dict:
    """Resolve only the named v1 style tokens; arbitrary CSS/design packs are out.

    ``style.theme`` selects a design preset (default ``notion``). The current
    shipping skin is Notion — the model only needs ``{"style":{"theme":"notion"}}``
    to opt in (it is also the default), and the Notion token set below drives the
    accent/radius/surfaces baked by SHELL_CSS + editor-runtime.css."""
    style = spec.get("style") or {}
    theme = style.get("theme") or "notion"
    accent_map = {"blue": "#5645d4", "teal": "#2a9d99", "orange": "#dd5b00", "purple": "#7b3ff2"}
    radius_map = {"none": "0px", "small": "8px", "medium": "12px"}
    density_map = {"compact": "24px 24px 48px", "comfortable": "32px 24px 64px"}
    font_map = {"sm": "14px", "md": "15px", "lg": "16px"}
    # Unauthored fontScale = "md": the manifest itself records typography.base=15
    # and SHELL_CSS defaults to 15px — a 14px shell fallback made the static
    # shell disagree with both (and with the runtime applyShellTheme reflow).
    font_size = font_map.get(style.get("fontScale"), "15px")
    # Notion preset: purple primary, 12px cards, surface background. (Both
    # branches shared the same token shape — only `name` differs.)
    return {"name": theme, "tokens": {
        "accent": accent_map.get(style.get("accent"), "#5645d4"),
        "radius": radius_map.get(style.get("radius"), "12px"),
        "shellPadding": density_map.get(style.get("density"), "32px 24px 64px"),
        "fontSize": font_size,
    }}


# ---------- request v1 -> deterministic compilation ----------------------------

PRESET_TO_VIEW = {
    "bar.rank": "bar", "bar.stacked": "stackedBar", "line.trend": "line",
    "area.trend": "area", "scatter.correlation": "scatter", "pie.share": "pie",
    "donut.share": "donut", "funnel.stage": "funnel",
    "table.aggregate": "table", "table.detail": "table",
}

# Reverse map (view -> canonical preset) so a Flint-compiled intent chart can
# be expressed as a classic preset chart the rest of the pipeline understands.
VIEW_TO_PRESET = {"bar": "bar.rank", "stackedBar": "bar.stacked", "line": "line.trend",
                  "area": "area.trend", "scatter": "scatter.correlation", "pie": "pie.share",
                  "donut": "donut.share", "funnel": "funnel.stage", "table": "table.aggregate"}


def _request_roles(request: dict, table: dict) -> dict[str, str]:
    return _table_roles_for(table, request.get("columnRoles") or {})


def _table_roles_for(table: dict, authored: dict) -> dict[str, str]:
    """Effective per-column roles for one table: authored roles + profile-type
    inference for the remaining columns. Columns referenced by a role but absent
    from the table are a hard error (unknown column)."""
    roles = dict(authored or {})
    inferred = (table.get("meta") or {}).get("types") or {}
    for col in table.get("columns", []):
        if col in roles:
            continue
        typ = inferred.get(col, "category")
        roles[col] = {"category": "dimension", "measure": "measure", "time": "time"}.get(typ, "ignore")
    unknown = [c for c in roles if c not in table.get("columns", [])]
    if unknown:
        raise ValueError(f"columnRoles 引用了未知列：{unknown}")
    invalid = [c for c, role in roles.items() if role not in {"time", "dimension", "measure", "id", "ignore"}]
    if invalid:
        raise ValueError(f"存在非法 column role：{invalid}")
    return roles


def _cross_table_merge(dim_key: str, group_tables: list, measures: list, ds_tables: dict, filters_by_ds: dict) -> tuple[list, list]:
    """Aligned cross-table aggregation (references/multi-source-alignment.md §6.2).

    ``group_tables`` = [{datasetId, column, bucket}] of ONE same-domain alignment
    group; ``measures`` = [{column, datasetId, aggregation}] (each bound to its own
    table). Aggregates each measure over its own table (grouped by that table's
    dimension column), then outer-joins on the normalized label. Missing labels
    yield 0.0 (additive). Sort: first measure desc, label asc tiebreak — identical
    to the runtime chart-adapter cross merge for WYSIWYG.

    MVP boundary: raw labels only (no time-bucket re-labelling) — the two tables
    must already store the SAME dimension labels."""
    group_by_ds = {e["datasetId"]: e for e in group_tables}
    results: list[tuple[dict, dict]] = []
    for m in measures:
        dsid = m["datasetId"]
        col = m["column"]
        agg = m.get("aggregation", "sum")
        tbl = ds_tables[dsid]
        dcol = group_by_ds[dsid]["column"]
        cols = tbl["columns"]
        ci = cols.index(col)
        di = cols.index(dcol)
        filters = filters_by_ds.get(dsid) or []
        groups: dict[str, list] = {}
        for row in tbl["rows"]:
            if not _row_filter(row, cols, filters):
                continue
            label = str(row[di] if row[di] is not None else "")
            groups.setdefault(label, []).append(row)
        valmap: dict[str, Any] = {}
        for label, grouped in groups.items():
            nums = []
            for row in grouped:
                try:
                    if row[ci] is not None and row[ci] != "":
                        nums.append(float(row[ci]))
                except (TypeError, ValueError):
                    pass
            if agg == "countd":
                # 去重计数：原始值去重、只排 NULL（与 _deterministic_rows /
                # server COUNT(DISTINCT) / JS Set 同一契约）。
                seen: set = set()
                for row in grouped:
                    v = row[ci]
                    if v is None:
                        continue
                    try:
                        seen.add(v)
                    except TypeError:
                        seen.add(str(v))
                v = len(seen)
            elif agg == "count":
                # count = 组内记录数（never a null-count of the measure column;
                # same contract as _deterministic_rows / server COUNT(*) / JS
                # runtime aggregate), not len(nums).
                v = len(grouped)
            elif not nums:
                v = 0.0
            elif agg == "average":
                v = sum(nums) / len(nums)
            elif agg == "min":
                v = min(nums)
            elif agg == "max":
                v = max(nums)
            else:
                v = sum(nums)
            valmap[label] = v
        results.append((m, valmap))
    labels: list[str] = []
    seen: set = set()
    for _m, valmap in results:
        for label in valmap:
            if label not in seen:
                seen.add(label)
                labels.append(label)
    def _key(label: str):
        first = results[0][1].get(label, 0.0) if results else 0.0
        return (-_num(first), str(label))
    # A bucketed (time/ordinal) shared dimension keeps chronological order;
    # a nominal dimension sorts by the first measure desc (bar-rank).
    # 排序键 = date_parse.sort_key（日历序）：旧实现直接字符串比较，非零填充
    # 写法（2026/6/1 vs 2026/6/10 vs 2026/6/2）排出字典序乱轴——与运行时
    # chart-adapter crossTableAggregate 同一批修复（WYSIWYG 两侧同键）。
    if any(e.get("bucket") for e in group_tables):
        labels.sort(key=_time_sort_key)
    else:
        labels.sort(key=_key)
    columns = [dim_key] + [m["column"] for m in measures]
    rows = [[label] + [valmap.get(label, 0.0) for _m, valmap in results] for label in labels]
    return columns, rows


def _row_filter(row: list[Any], columns: list[str], filters: Any) -> bool:
    if not filters:
        return True
    def test(cond: dict[str, Any]) -> bool:
        if "any" in cond:
            return any(test(item) for item in cond.get("any") or [])
        col = cond.get("column")
        if col not in columns:
            return False
        value = row[columns.index(col)]
        op = str(cond.get("op", "=")).lower()
        expected = cond.get("value")
        # A predicate without a value (or an empty selection) declares an
        # interactive filter control with nothing picked — "no constraint".
        # Previously `in` with no value compared against an empty list and
        # eliminated EVERY row, so the whole report failed with
        # "chart has no data rows" (real authoring run, 2026-09).
        if expected is None or expected == "" or (isinstance(expected, (list, tuple, set)) and not expected):
            if op not in ("is null", "is not null"):
                return True
        # ge/le 别名与 server/aggregator._predicate_sql 同批（derived_model.FILTER_OPS
        # 的声明词汇、UI 下拉同款 → 本管道规范词汇 gte/lte）：缺别名时筛选型派生
        # 指标的 ge/le 谓词走到底部 .get(op, False) 把整组行全部筛空（静态首屏
        # 全 None 假数；服务端则是 400，2026-10-11 第 13 轮审视实锤）。
        op = {"eq": "=", "ne": "!=", "gt": ">", "gte": ">=", "ge": ">=",
              "lt": "<", "lte": "<=", "le": "<="}.get(op, op)
        if op == "is null": return value is None or value == ""
        if op == "is not null": return value is not None and value != ""
        if op in {"in", "not in"}:
            hit = value in (expected or [])
            return hit if op == "in" else not hit
        # 数值区间 / 日期范围（读者侧的区间筛选控件）。两端都可为空 =
        # 单边开口；两端都空 = 无约束（与「空值 = 不约束」同一条规矩）。
        # 日期范围比较在**规范化后的 ISO**上做（date_parse 单一解析器），
        # 所以 `2026/9/5` / `2026年9月5日` / `20260905` / `202501` 都能落在
        # 正确区间里 —— 与服务端 ai_date() 和 JS parseDateValue 是同一套。
        if op == "between":
            vals = list(expected or [])
            if len(vals) != 2:
                return False
            lo, hi = vals
            if lo is None and hi is None:
                return True
            try:
                num = float(value)
            except (TypeError, ValueError):
                return False
            if lo is not None and num < float(lo):
                return False
            if hi is not None and num > float(hi):
                return False
            return True
        if op == "daterange":
            vals = list(expected or [])
            if len(vals) != 2:
                return False
            start, end = vals
            if not start and not end:
                return True
            iso = date_parse.iso(value)
            if not iso:
                return False
            # 端点与值侧同走 date_parse.iso 归一（值侧早已归一）：作者写
            # 2026/9/1 这类自然写法时按原始字符串字典序比较会静默清空全部行
            # （"2026-09-05" < "2026/9/1"）。解析失败的端点保持原串（诚实让
            # 该端点约束落空，而不是造一个错误区间）。
            def _iso_edge(x: Any) -> str:
                if x is None or str(x).strip() == "":
                    return ""
                _p = date_parse.iso(x)
                return (_p or str(x))[:10]
            if start and iso < _iso_edge(start):
                return False
            if end and iso > _iso_edge(end):
                return False
            return True
        if op == "contains": return str(expected or "") in str(value or "")
        try:
            left, right = float(value), float(expected)
        except (TypeError, ValueError):
            left, right = str(value), str(expected)
        return {"=": left == right, "!=": left != right, ">": left > right,
                ">=": left >= right, "<": left < right, "<=": left <= right}.get(op, False)
    if isinstance(filters, dict):
        return all(test(item) for item in filters.get("all", [])) and (not filters.get("any") or any(test(item) for item in filters["any"]))
    return all(test(item) for item in filters)


def _deterministic_rows(table: dict, dimension: str | None, measures: list[str], aggregation: str,
                        breakdown: str | None = None, filters: Any = None, limit: int = 500,
                        sort_and_limit: bool = True, series_filters: Any = None) -> list[list[Any]]:
    """Group + aggregate to one row per dimension value (the ONE aggregation path).

    ``sort_and_limit=False`` returns every group in first-appearance order without
    the desc-by-first-measure sort/truncation. The aggregate-level derived-measure
    path (``_rows_with_derived``) needs that: it aggregates several series
    separately and they must stay row-aligned, so sorting/truncating a series
    before the window comparison would compare the wrong periods.

    ``series_filters``（筛选型派生指标）：组宇宙仍由 ``filters``（报告级筛选）决定，
    但**取值只聚组内通过该谓词的行**（docs/filtered-measures-and-week-fields-
    2026-10-09.md D8）——无命中行 count/countd = 0、sum/average/min/max = None，
    与服务端 CASE WHEN 条件聚合、JS 本地累加器三端逐位一致。
    """
    columns, source_rows = table.get("columns", []), table.get("rows", [])
    if dimension and dimension not in columns:
        raise ValueError(f"图表维度不存在：{dimension}")
    for measure in measures:
        if measure != COUNT_MEASURE and measure not in columns:
            raise ValueError(f"图表指标不存在：{measure}")
    if breakdown and breakdown not in columns:
        raise ValueError(f"图表拆分维度不存在：{breakdown}")
    di = columns.index(dimension) if dimension else None
    bi = columns.index(breakdown) if breakdown else None
    # COUNT_MEASURE ("*") is a synthetic row-count: no column index; value = rows in group.
    mis = [None if m == COUNT_MEASURE else columns.index(m) for m in measures]
    groups: dict[tuple[Any, ...], list[list[Any]]] = {}
    for row in source_rows:
        if not _row_filter(row, columns, filters):
            continue
        key = tuple(row[i] for i in (di, bi) if i is not None)
        groups.setdefault(key, []).append(row)
    out: list[list[Any]] = []
    for key, grouped in groups.items():
        if series_filters:
            grouped = [r for r in grouped if _row_filter(r, columns, series_filters)]
        values: list[Any] = list(key)
        for mi in mis:
            if mi is None or aggregation == "count":
                # Row-count semantics (parity with server COUNT(*) and the JS
                # runtime): the metric is the number of records in the group,
                # never a null-count of the measure column.
                value = len(grouped)
            else:
                if aggregation == "countd":
                    # 去重计数（对标 Quick BI BI_COUNTD）：组内**原始值**去重、
                    # 只排 NULL（'' 是合法去重值）——与服务端 COUNT(DISTINCT col)、
                    # 运行时 Set 同一契约。走 float() 管道会把 '001'/'1' 折叠成
                    # 同一数值，也会把文本列整组变 None/行数（静态首屏假数）。
                    seen: set = set()
                    for row in grouped:
                        v = row[mi]
                        if v is None:
                            continue
                        try:
                            seen.add(v)
                        except TypeError:
                            seen.add(str(v))
                    value = len(seen)
                else:
                    nums = []
                    for row in grouped:
                        try:
                            if row[mi] is not None and row[mi] != "": nums.append(float(row[mi]))
                        except (TypeError, ValueError):
                            pass
                    if not nums: value = None
                    elif aggregation == "average": value = sum(nums) / len(nums)
                    elif aggregation == "min": value = min(nums)
                    elif aggregation == "max": value = max(nums)
                    else: value = sum(nums)
            values.append(value)
        out.append(values)
    if not sort_and_limit:
        return out
    primary = len(key) if groups else (1 if dimension else 0)
    out.sort(key=lambda row: (_num(row[primary]) if primary < len(row) else 0), reverse=True)
    return out[:max(1, min(5000, limit))]


def _derived_measure_nodes(spec: dict, dsid: str) -> list[dict]:
    """Normalised aggregate-level derived-measure nodes declared for one dataset."""
    for d in spec.get("datasets") or []:
        if isinstance(d, dict) and d.get("id") == dsid:
            return [dict(n) for n in ((d.get("derived") or {}).get("measures") or [])
                    if isinstance(n, dict) and n.get("name")]
    return []


def _raw_time_bucket(table: dict, dim: str | None, role: str | None) -> str | None:
    """Grain of a RAW time dimension, from the shared parser (compile ↔ server ↔ runtime).

    The derived-measure evaluation runs while the chart's rows are being built, so it
    cannot use ``_infer_time_bucket`` (which reads ``chart.data.rows``). Same parser,
    same column values → same grain as the ``dimensionBucket`` build_manifest writes
    later, so the evaluated series and the axis labels cannot disagree.
    """
    if not dim or role != "time":
        return None
    cols = table.get("columns") or []
    if dim not in cols:
        return None
    ci = cols.index(dim)
    vals = [_strip_float_zero(r[ci]) for r in (table.get("rows") or [])
            if len(r) > ci and str(r[ci]).strip()][:500]
    return date_parse.infer_grain(vals, dim) if vals else None


def _measure_default_agg(measure_types: dict, col: str, chart_agg: str) -> str:
    """Per-measure default aggregation tiers (2026-10-13 批):

    显式 ``measureTypes[col].aggregation`` > aggRole 缺省（intensive→average，
    Flint resolveAggregationDefault）> 图表级聚合。第二层此前缺失——多指标图里
    非首指标恒继承图表级 sum，价格/比率列被静默加总。"""
    mdef = (measure_types or {}).get(col)
    if isinstance(mdef, dict) and mdef.get("aggregation"):
        return mdef["aggregation"]
    from measure_semantics import default_aggregation
    sem = default_aggregation(mdef, col)
    if sem:
        return sem
    return chart_agg


def _rows_with_derived(table: dict, dimension: str | None, measures: list[str], nodes: list[dict],
                       aggregation: str, breakdown: str | None, filters: Any, limit: int,
                       measure_types: dict, dimension_bucket: dict | None) -> list[list[Any]]:
    """Grouped rows for a chart whose measures include aggregate-level derived ones.

    Order matters and mirrors the runtime: aggregate → **evaluate derived** → sort →
    TOP-N → pagination. Each series is aggregated separately through
    ``_deterministic_rows(sort_and_limit=False)`` so the aggregation rules (count =
    group size, average, null handling, bucket labels) stay identical to every other
    chart; the series are then row-aligned by the shared group order.
    """
    import derived_eval
    by_name = {n["name"]: n for n in nodes}
    used = [n for n in nodes if n["name"] in set(measures)]
    default_agg = {m: _measure_default_agg(measure_types, m, aggregation)
                   for m in measures if m not in by_name}
    # 求值集 = 图上指标沿 ref 引用**传递闭包**（公式引用的派生指标也要先算出来）；
    # required_aggregations 同样沿闭包收集依赖——declared 名单曾经把「已声明但不在
    # 本图上」的指标当成依赖已满足，合法声明直接编译崩溃且报错误导。
    eval_nodes = derived_eval.closure_nodes(used, by_name)
    required = derived_eval.required_aggregations(eval_nodes, declared=set(by_name),
                                                  default_agg=default_agg, by_name=by_name)

    # 图表声明了时间分桶时，全部依赖/基础序列必须**在目标粒度上**聚合（把时间列
    # 替换成桶标签后走同一 _deterministic_rows）。否则窗口派生的静态值按原始粒度
    # 求值——「日数据按月看趋势 + 月环比」会把 01-01→01-02 的日环比烙进 2026-01
    # 桶（与运行时按桶重聚合的真月环比分叉，首次编辑即翻值）。
    _bucket_grain = (dimension_bucket or {}).get("bucket")
    eval_table = table
    if _bucket_grain and _bucket_grain != "raw" and dimension:
        _bt = _bucketed_table(table, dimension, _bucket_grain)
        if _bt is not None:
            eval_table = _bt

    def series_of(col: str, method: str, extra_filters: Any = None) -> tuple[list, list, list]:
        rows = _deterministic_rows(eval_table, dimension, [col], method, breakdown, filters,
                                   limit=5000, sort_and_limit=False, series_filters=extra_filters)
        labels = [r[0] for r in rows]
        bds = [r[1] for r in rows] if breakdown else []
        return labels[:], bds, [r[-1] for r in rows]

    labels: list = []
    bds: list = []
    series: dict = {}
    for dep in required:
        # dep 携带自己的键（filtered = fm:{id}，普通 = agg:{agg}:{col}）与可选的
        # 逐序列筛选（筛选型派生指标）：组宇宙不变、组内只聚通过谓词的行。
        labels, bds, series[dep.get("key") or derived_eval.agg_key(dep["agg"], dep["col"])] = \
            series_of(dep["col"], dep["agg"], dep.get("filters"))
    for m in measures:
        if m in by_name:
            continue
        labels, bds, series[m] = series_of(m, default_agg.get(m, aggregation))
    if not labels and dimension:
        # 兜底取数列：优先真实基础指标，filtered 依赖的合成列 "*"（count 计行数）
        # 不是真实列会抛「指标不存在」，跳过它取下一个真实依赖列，最后落到维度列
        # 本身（count 行数只为生成标签序列）。
        _fb = measures[0] if measures and measures[0] not in by_name else None
        if _fb is None:
            for _dep in required:
                if _dep.get("col") and _dep.get("col") != "*":
                    _fb = _dep["col"]
                    break
        labels, bds, _ = series_of(_fb or dimension, default_agg.get(measures[0], aggregation)
                                   if (measures and _fb == measures[0]) else "count")
    # BI-H2 share=parent：从原始表构建「标签 → 父级」映射（每个 parent-share 节点
    # 一张表，键值两侧都归一成 str）。键用原始维度值（与运行时 groupLabel 的
    # String(value) 一致）；分桶时间维度上键不会命中 → 那些档保持 null（可见，不造数）。
    parent_of = None
    if any(n.get("kind") == "share" and n.get("scope") == "parent" for n in used):
        parent_of = {}
        _cols = table.get("columns", [])
        _di = _cols.index(dimension) if dimension in _cols else None
        for _n in used:
            if not (_n.get("kind") == "share" and _n.get("scope") == "parent"):
                continue
            _pf = _n.get("parentField")
            if _di is None or _pf not in _cols:
                continue
            _pi = _cols.index(_pf)
            _m: dict = {}
            for _r in table.get("rows", []):
                _k = _r[_di]
                _key = "" if _k is None else str(_k)
                if _key not in _m:
                    _pv = _r[_pi]
                    _m[_key] = "" if _pv is None else str(_pv)
            parent_of[_n["name"]] = _m
    computed = derived_eval.evaluate(eval_nodes, series, labels,
                                    dimension=dimension,
                                    bucket=(dimension_bucket or {}).get("bucket"),
                                    time_sort_key=_time_sort_key,
                                    parent_of=parent_of,
                                    # 交叉图（dim × breakdown）：窗口游标按第二个
                                    # 维度分区，环比/累计不跨系列取数（与运行时
                                    # applyDerivedNodes 的 partitionOf 同一批修复）。
                                    partition_of=(bds or None) if breakdown else None)
    out: list[list[Any]] = []
    for i, label in enumerate(labels):
        row: list[Any] = [label] + ([bds[i]] if breakdown else [])
        for m in measures:
            row.append(computed[m][i] if m in by_name else series[m][i])
        out.append(row)
    # Row shape is [label, (breakdown,) m1, m2…]: the first measure lives at
    # index 2 with a breakdown, 1 without — index 0/1 are dimension cells, and
    # sorting on them made this desc sort a no-op (labels → _num → 0.0), so the
    # [:limit] truncation below kept arrival order instead of TOP-N.
    primary = (2 if breakdown else 1)
    out.sort(key=lambda r: (_num(r[primary]) if primary < len(r) else 0), reverse=True)
    return out[:max(1, min(5000, limit))]


def _compute_fact(fact: dict, table: dict, filters: Any = None) -> Any:
    typ = fact.get("type")
    dim, measure = fact.get("dimension"), fact.get("measure")
    agg = fact.get("aggregation", "sum")
    # A KPI may declare only measure+aggregation (no ``type``/fact). Infer the
    # effective fact type from the aggregation so the value is computed instead
    # of rendering as "—". ``count`` aggregates row-count, not null-counts.
    if typ is None and measure is not None:
        if agg == "count":
            typ = "count"
        elif agg == "countd":
            typ = "distinctCount"
        elif agg == "average":
            typ = "average"
        elif agg == "min":
            typ = "min"
        elif agg == "max":
            typ = "max"
        else:
            typ = "total"
    elif measure is not None and typ not in {"total", "count", "distinctCount", "topN", "shareOfTotal",
                                              "trend", "growthRate", "rankChange", "missingRate", "min", "max", "average"}:
        # ``type`` is an author-facing family label (common case: "metric"). A
        # measure + aggregation is enough to derive the real fact type instead
        # of silently dropping the KPI.
        if agg == "count":
            typ = "count"
        elif agg == "countd":
            typ = "distinctCount"
        elif agg == "average":
            typ = "average"
        elif agg == "min":
            typ = "min"
        elif agg == "max":
            typ = "max"
        else:
            typ = "total"
    # Authors often write the aggregation verb as the fact ``type``
    # ({"type": "sum", "measure": ...}); map it to the equivalent fact type so
    # the value computes instead of rendering "—".
    typ = {"sum": "total", "avg": "average", "mean": "average"}.get(typ, typ)
    rows = []
    if typ in {"topN", "shareOfTotal", "rankChange"}:
        # Share/topN/trend need a dimensional breakdown; guard against a missing
        # dimension/measure so a KPI never crashes the whole compile with a
        # ValueError — it just yields no rows and the card falls back gracefully.
        try:
            rows = _deterministic_rows(table, dim, [measure] if measure else [], agg, filters=filters, limit=5000)
        except ValueError:
            rows = []
    if typ in {"total", "count", "distinctCount", "missingRate", "min", "max", "average", "growthRate"}:
        # A KPI must NEVER silently render "—" when a value is derivable. Resolve
        # the measure column defensively (a missing/renamed column falls back to
        # row-count) so the card always carries a computed number.
        ci = table["columns"].index(measure) if measure in table["columns"] else -1
        if ci < 0:
            if typ == "count":
                return len([r for r in table["rows"] if _row_filter(r, table["columns"], filters)])
            if typ == "distinctCount" and dim in table["columns"]:
                dcol = table["columns"].index(dim)
                return len({str(r[dcol]) for r in table["rows"] if _row_filter(r, table["columns"], filters) and r[dcol] not in (None, "")})
            return None
        values = [row[ci] for row in table["rows"] if _row_filter(row, table["columns"], filters)]
        nums = []
        for value in values:
            try: nums.append(float(value))
            except (TypeError, ValueError): pass
        if typ == "count": return len([r for r in table["rows"] if _row_filter(r, table["columns"], filters)])
        if agg == "countd" and typ == "distinctCount":
            # 聚合声明的 countd = **指标列自身**的去重计数（服务端 COUNT(DISTINCT col)
            # / 运行时 Set 同一契约），不是 distinctCount 家族的「维度值去重」。
            _seen: set = set()
            for row in table["rows"]:
                if not _row_filter(row, table["columns"], filters):
                    continue
                v = row[ci]
                if v is None:
                    continue
                try:
                    _seen.add(v)
                except TypeError:
                    _seen.add(str(v))
            return len(_seen)
        if typ == "distinctCount":
            _dim_col = table["columns"].index(dim) if dim in table["columns"] else ci
            return len({str(r[_dim_col]) for r in table["rows"] if _row_filter(r, table["columns"], filters) and r[_dim_col] not in (None, "")})
        if typ == "missingRate":
            total = len(values); return (sum(v in (None, "") for v in values) / total) if total else 0
        if not nums:
            # Non-numeric measure for a numeric agg: fall back to row-count so the
            # card is never blank; only "—" when the table itself is empty.
            return len(table["rows"]) if typ == "count" else (None if not table["rows"] else len(table["rows"]))
        if typ == "min": return min(nums)
        if typ == "max": return max(nums)
        if typ == "average": return sum(nums) / len(nums)
        # growthRate has no declarable period here (use derived mom/yoy, which
        # computes it for real); returning 0.0 fabricated a "0.00%" card.
        # None → the card is dropped at compile time, never shipped as fake data.
        if typ == "growthRate": return None
        return sum(nums)
    if typ == "topN":
        return [{"label": r[0], "value": r[1]} for r in rows[: int(fact.get("n", 1))]]
    if typ == "shareOfTotal":
        total = sum(_num(r[1]) for r in rows)
        return ({"label": rows[0][0], "value": rows[0][1], "share": rows[0][1] / total} if rows and total else None)
    if typ == "trend":
        # rows 只在取数分支赋值；trend 没有分支给它赋值 → 恒 []，而 [] 非 None
        # 会被 KPI 卡保留并把字面 "[]" 渲染出来。与其他不可算 fact 一样返回
        # None（丢卡），绝不渲染占位符。
        return None
    if typ == "rankChange":
        # Never computed from data (see growthRate above) — None drops the card
        # instead of rendering a fabricated "0".
        return None
    return None


def _format_fact(value: Any, fact: dict | None = None) -> str:
    if value is None: return "—"
    if isinstance(value, float) or isinstance(value, int):
        # Percent semantics (a KPI with measureType/semanticType percent, or a
        # fact type missingRate/growthRate) render as a percentage of 1.0 -> x%.
        typ = (fact or {}).get("type") if isinstance(fact, dict) else None
        mtype = (fact or {}).get("measureType") or (fact or {}).get("semanticType") if isinstance(fact, dict) else None
        if isinstance(mtype, dict): mtype = mtype.get("type")
        if typ in {"missingRate", "growthRate", "percent"} or mtype == "percent" or mtype == "growthRate":
            return f"{(float(value) * 100):,.2f}".rstrip("0").rstrip(".") + "%"
        if isinstance(value, float): return f"{value:,.2f}".rstrip("0").rstrip(".")
        return f"{value:,}"
    if isinstance(value, list): return ", ".join(str(x.get("label", x)) for x in value)
    if isinstance(value, dict): return str(value.get("label", value))
    return str(value)


def _is_num(v: Any) -> bool:
    try:
        float(v)
        return v not in (None, "")
    except (TypeError, ValueError):
        return False


def _derive_column_values(table: dict, col: str, filters: Any = None) -> list[float]:
    """Numeric values for one column after applying the report filter."""
    columns = table.get("columns", [])
    if col not in columns:
        return []
    ci = columns.index(col)
    return [float(r[ci]) for r in table.get("rows", [])
            if _row_filter(r, columns, filters) and _is_num(r[ci])]


def _derive_col_sum(table: dict, col: str, filters: Any = None) -> float | None:
    vals = _derive_column_values(table, col, filters)
    return sum(vals) if vals else None


def _compute_cross_rate(num: dict, den: dict, ds_tables: dict, dimension_keys: dict, spec: dict) -> float | None:
    """Cross-table rate (P3): numerator/denominator aggregate on DIFFERENT
    tables (e.g. 售罄率 = SellOut(sales) / SellIn(stock)). Each side is
    ``{datasetId, column, aggregation}``; filters cascade per table."""
    def side(spec_side: dict) -> float | None:
        dsid = spec_side.get("datasetId")
        col = spec_side.get("column")
        agg = spec_side.get("aggregation", "sum")
        tbl = ds_tables[dsid]
        filters = _filters_for_table(spec.get("filters"), tbl.get("columns") or [], dimension_keys, dsid)
        ci = tbl["columns"].index(col)
        n_rows = 0
        vals = []
        for row in tbl["rows"]:
            if not _row_filter(row, tbl["columns"], filters):
                continue
            n_rows += 1
            try:
                if row[ci] is not None and row[ci] != "":
                    vals.append(float(row[ci]))
            except (TypeError, ValueError):
                pass
        if agg == "countd":
            # 去重计数：原始值去重、只排 NULL（与其他 countd 消费点同一契约）。
            seen: set = set()
            for row in tbl["rows"]:
                if not _row_filter(row, tbl["columns"], filters):
                    continue
                v = row[ci]
                if v is None:
                    continue
                try:
                    seen.add(v)
                except TypeError:
                    seen.add(str(v))
            return len(seen)
        if agg == "count":
            # count = record count (same contract as _deterministic_rows /
            # server COUNT(*) / JS aggregate), not a null-count of the column.
            return n_rows
        if not vals:
            return None
        if agg == "average":
            return sum(vals) / len(vals)
        if agg == "min":
            return min(vals)
        if agg == "max":
            return max(vals)
        return sum(vals)
    n = side(num)
    d = side(den)
    if n is None or d in (None, 0):
        return None
    return n / d


def _compute_derived(kpi: dict, table: dict, roles: dict | None = None, filters: Any = None) -> dict | None:
    """Compute a KPI's derived (delta) value from its declarative ``derived``
    spec. Returns {value, kind, label, rawValue} or None when there is no
    derived spec or the inputs are not resolvable (so the card falls back to a
    static delta text).

    Supported ``derived.type``:
      - rate          {numerator, denominator} -> percent (e.g. 毛利率)
      - share         {measure, dimension}     -> top-category share of total
      - yoy / mom     {measure, dimension?}    -> growth vs prev year/month
      - compare       {measure, compareTo: total|first|last|value, baseValue}
      - min/max/average/total/count/distinctCount/missingRate/rankChange
    """
    derived = kpi.get("derived")
    if not isinstance(derived, dict):
        return None
    dtyp = derived.get("type")
    if not dtyp:
        return None
    if dtyp == "rate":
        # Cross-table rate is computed as the KPI's MAIN value (P3), not a delta;
        # its numerator/denominator are dicts and must not flow into the
        # single-table column-sum path below.
        if isinstance(derived.get("numerator"), dict) or isinstance(derived.get("denominator"), dict):
            return None
        num = _derive_col_sum(table, derived.get("numerator") or derived.get("measure"), filters)
        den = _derive_col_sum(table, derived.get("denominator") or derived.get("basedOn"), filters)
        if num is None or den in (None, 0):
            return None
        return {"value": num / den, "kind": "percent", "label": derived.get("label")}
    if dtyp == "share":
        measure = derived.get("measure") or kpi.get("measure")
        dim = derived.get("dimension")
        if not measure or not dim:
            return None
        total = _derive_col_sum(table, measure, filters)
        if not total:
            return None
        try:
            rows = _deterministic_rows(table, dim, [measure], "sum", filters=filters, limit=5000)
        except ValueError:
            rows = []
        if not rows:
            return None
        top_label, top_val = rows[0][0], _num(rows[0][1])
        return {"value": top_val / total, "kind": "percent",
                "label": derived.get("label", f"{top_label}占比")}
    if dtyp in ("yoy", "mom"):
        measure = derived.get("measure") or kpi.get("measure")
        roles = roles or {}
        timedim = derived.get("dimension") or next((c for c, r in roles.items() if r == "time"), None)
        if not measure or not timedim or timedim not in table.get("columns", []):
            return None
        try:
            buckets = _deterministic_rows(table, timedim, [measure], "sum", filters=filters, limit=1000)
        except ValueError:
            buckets = []
        if len(buckets) < 2:
            return None
        try:
            # Calendar order via the shared parser (single source of truth): a
            # plain str() sort scrambles 2026/9/10 vs 2026/9/5 and picks the
            # wrong cur/prev pair for the 环比/同比 delta.
            buckets.sort(key=lambda r: _time_sort_key(r[0]))
        except Exception:
            pass
        cur = _num(buckets[-1][1]) if buckets[-1] and len(buckets[-1]) > 1 else None
        if dtyp == "yoy":
            # 同比 = **同期上一年**（对齐 kpiCompareNode 合成的窗口节点与
            # derived_model 语义）。旧实现也只回看一期，把环比当同比标出去。
            # 首选**日历匹配**（去年同期的桶，允许序列不连续）；标签不可解析时
            # 退化为按粒度位置回看（连续序列下与日历一致）。
            def _parts_of(lab: Any):
                try:
                    return date_parse.parts(lab)
                except Exception:
                    return None
            _cur_parts = _parts_of(str(buckets[-1][0]))
            prev = None
            if _cur_parts:
                _want = (_cur_parts[0] - 1, _cur_parts[1], _cur_parts[2])
                for _r in buckets[:-1]:
                    _rp = _parts_of(str(_r[0]))
                    if _rp and _rp[:3] == _want and len(_r) > 1:
                        prev = _num(_r[1])
                        break
            if prev is None:
                _grain = date_parse.infer_grain([str(r[0]) for r in buckets], str(timedim))
                _span = {"day": 365, "week": 52, "month": 12, "quarter": 4, "year": 1}
                _off = _span.get(_grain or "month", 12)
                _j = len(buckets) - 1 - _off
                prev = _num(buckets[_j][1]) if _j >= 0 and buckets[_j] and len(buckets[_j]) > 1 else None
        else:
            prev = _num(buckets[-2][1]) if buckets[-2] and len(buckets[-2]) > 1 else None
        if cur is None or prev in (None, 0):
            return None
        return {"value": (cur - prev) / prev, "kind": "percent",
                "label": derived.get("label", "同比" if dtyp == "yoy" else "环比")}
    if dtyp == "compare":
        measure = derived.get("measure") or kpi.get("measure")
        cur = _derive_col_sum(table, measure, filters)
        if cur is None:
            return None
        comp = derived.get("compareTo")
        if comp == "total":
            base = _derive_col_sum(table, measure, None)
        elif comp == "first":
            vals = _derive_column_values(table, measure, filters)
            base = vals[0] if vals else None
        elif comp == "last":
            vals = _derive_column_values(table, measure, filters)
            base = vals[-1] if vals else None
        else:
            base = derived.get("baseValue")
        if base in (None, 0):
            return None
        return {"value": (cur - base) / base, "kind": "percent",
                "label": derived.get("label", "对比" if comp == "total" else f"较{comp}")}
    if dtyp in ("min", "max", "average", "total", "count", "distinctCount",
                "missingRate", "rankChange"):
        sub = dict(derived)
        sub["type"] = dtyp
        sub.setdefault("measure", kpi.get("measure"))
        sub.setdefault("aggregation", kpi.get("aggregation", "sum"))
        if dtyp in ("count", "distinctCount", "missingRate") and not sub.get("dimension"):
            sub["dimension"] = kpi.get("countDim") or sub.get("measure")
        val = _compute_fact(sub, table, filters)
        if val is None:
            return None
        kind = "percent" if dtyp in ("missingRate",) else (kpi.get("measureType") if isinstance(kpi.get("measureType"), str) else "number")
        return {"value": val, "kind": kind, "label": derived.get("label")}
    return None


# Row-level derived columns (计算列) now live in ``scripts/derived_eval.py`` so
# the compile path and the server path share ONE implementation (a server report
# materialises the same columns into SQLite at register/reload time). The alias
# keeps older imports/readers working.
DERIVED_OP_FNS = derived_eval.DERIVED_OP_FNS


def _materialize_derived(table: dict, defs: list) -> dict:
    """Append whitelist-op derived columns to one dataset (docs §2).

    Thin delegate to ``derived_eval.materialize`` — the single implementation
    also used by ``server/derived_columns.py``. See that module for the full
    contract (determinism, null semantics, why it must not be re-written).
    """
    return derived_eval.materialize(table, defs)


def _normalize_sections(spec: dict) -> list[dict]:
    """Project validated author sections into canonical manifest shape.

    validate_request has already auto-assigned section ids and checked chart
    references, so this is a lossless projection into {id, title, narrative,
    insight, chartIds} consumed by layout_sections / render_grid_html. The P2
    textKind/style presets ride along untouched (whitelisted upstream)."""
    out = []
    for sec in spec.get("sections") or []:
        item = {"id": sec["id"], "title": sec.get("title", ""),
                "narrative": sec.get("narrative", ""), "insight": sec.get("insight", ""),
                "chartIds": list(sec.get("charts") or [])}
        if sec.get("textKind"):
            item["textKind"] = sec["textKind"]
        if sec.get("style"):
            item["style"] = dict(sec["style"])
        out.append(item)
    return out


def _filters_for_table(filters: Any, columns: list, dimension_keys: dict | None = None, dataset_id: str | None = None) -> Any:
    """Project the report filter tree onto one dataset's columns (P1).

    Predicates whose column is absent from the dataset are dropped, so a
    report-level filter authored for one table never zeroes out an unrelated
    table that merely lacks that column. A predicate may instead carry a
    ``dimensionKey`` (a shared dimension from manifest.dimensionKeys): it is
    expanded to THIS table's column when the table participates in that
    alignment group (references/multi-source-alignment.md §6.1)."""
    if not filters:
        return None
    dk = dimension_keys or {}

    def _dimension_key_column(key: str) -> str | None:
        group = dk.get(key) or {}
        for entry in group.get("tables", []):
            if entry.get("datasetId") == dataset_id:
                return entry.get("column")
        return None

    def prune(cond: Any) -> Any:
        if isinstance(cond, dict) and "any" in cond:
            children = [prune(item) for item in (cond.get("any") or [])]
            children = [item for item in children if item]
            return {"any": children} if children else None
        if not isinstance(cond, dict):
            return None
        col = cond.get("column")
        if not col and cond.get("dimensionKey"):
            col = _dimension_key_column(cond["dimensionKey"])
        if not isinstance(col, str) or col not in columns:
            return None
        leaf: dict = {"column": col, "op": cond.get("op", "="), "value": cond.get("value")}
        if "datasetId" in cond:
            leaf["datasetId"] = cond["datasetId"]
        return leaf

    if isinstance(filters, dict):
        alls = [prune(item) for item in (filters.get("all") or [])]
        anys = [prune(item) for item in (filters.get("any") or [])]
        alls = [item for item in alls if item]
        anys = [item for item in anys if item]
        if not alls and not anys:
            return None
        out = {}
        if alls:
            out["all"] = alls
        if anys:
            out["any"] = anys
        return out
    out = [prune(item) for item in (filters or [])]
    out = [item for item in out if item]
    return out or None


def _normalize_percent_format(spec: dict) -> None:
    """Rewrite ``{type:'percent'}`` to ``{type:'number', format:'percent'}``.

    P1 (docs §5): ``percent`` is a display format, not a type. Authoring
    ``measureTypes[col] = {type:'percent'}`` is ambiguous — it conflates "the
    raw value is a 0-100 integer that should NOT be summed" (rare) with
    "display this column as a percentage" (the common case). We normalise to
    the safe default — additive number + percent format — so old reports stay
    readable. The rare case can still author ``type:'percent'`` directly via
    ``measure_types`` (T2 canonical) and that path is non-additive by Flint
    rules. The transform is in-place; runs once per column. No-op when the
    object already has an explicit ``format`` field (the user already picked
    the contract)."""
    mt = spec.get("measureTypes")
    if not isinstance(mt, dict):
        return
    for col, raw in list(mt.items()):
        if not isinstance(raw, dict):
            continue
        if raw.get("type") == "percent" and "format" not in raw:
            raw["type"] = "number"
            raw["format"] = "percent"
            mt[col] = raw
    spec["measureTypes"] = mt


def _ensure_dataset_derived_slot(spec: dict) -> None:
    """Make sure every dataset has a ``derived`` slot with both canonical keys.

    P1: derived declarations are per-dataset (``derived.columns`` = 行级计算列,
    ``derived.measures`` = 聚合级派生指标). The compiler reads
    ``tables_by_id[ds_id].derived.columns`` (see ``_materialize_derived``);
    pre-creating the slots here keeps downstream code uniform — no scattered
    ``(ds.get('derived') or {}).get('columns') or []``.
    """
    for ds in spec.get("datasets") or []:
        if not isinstance(ds, dict):
            continue
        derived = ds.get("derived")
        if derived is None:
            ds["derived"] = {"columns": [], "measures": []}
            continue
        if not isinstance(derived, dict):
            continue  # validate_request already rejected non-object shapes
        derived.setdefault("columns", [])
        derived.setdefault("measures", [])


def _ds_role(declared_ds: list, did: str) -> str:
    """Table role for a manifest dataset card ('primary' fact / 'lookup').

    ``lookup`` tables are enrichment-only (referential 1:N targets); they never
    take default attribution and are de-emphasised in the runtime data-source
    pickers. Absent role means ``primary`` so legacy requests are unchanged."""
    for x in declared_ds:
        if x.get("id") == did:
            return x.get("role") or "primary"
    return "primary"


def _ds_primary_key(declared_ds: list, did: str) -> list[str] | None:
    """Declared upsert primary key (datasets[].primaryKey) for a dataset.

    Returns None when not declared so legacy manifests stay byte-identical
    (the key is only written when the request actually declares one)."""
    for x in declared_ds:
        if isinstance(x, dict) and x.get("id") == did:
            pk = x.get("primaryKey")
            return [str(c) for c in pk] if pk else None
    return None


def _normalize_ctx(spec: dict, table: dict, tables: dict | None = None) -> dict:
    """Per-dataset compilation context for P1 (unrelated tables in one report).

    ``spec`` carries the validated ``datasets`` declarations (ids/labels with
    optional per-table columnRoles/measureTypes/hierarchy); ``table`` is the CLI
    primary table; ``tables`` maps every declared dataset id to its loaded table.
    Top-level semantics act as the default slot for the first dataset; each
    dataset's own semantics override, intersected with its actual columns. A
    request without declarations resolves to the single implicit ``primary``
    dataset (byte-identical legacy behaviour)."""
    declared = spec.get("datasets") or []
    top_roles = dict(spec.get("columnRoles") or {})
    top_mt = dict(spec.get("measureTypes") or {})
    if not declared:
        return {"order": ["primary"], "tables": {"primary": table},
                "roles": {"primary": _table_roles_for(table, top_roles)},
                "measureTypes": {"primary": top_mt}, "default": "primary"}
    tables_all: dict = dict(tables or {})
    for d in declared:
        if d["id"] not in tables_all:
            raise ValueError(f"缺少数据集表：{d['id']}")
    order = [d["id"] for d in declared]
    roles: dict = {}
    measure_types: dict = {}
    for d in declared:
        t = tables_all[d["id"]]
        own = dict(d.get("columnRoles") or {})
        merged = dict(own)
        for col, role in top_roles.items():
            if col not in merged and col in t.get("columns", []):
                merged[col] = role
        roles[d["id"]] = _table_roles_for(t, merged)
        mt = dict(d.get("measureTypes") or {})
        for col, semantic in top_mt.items():
            if col not in mt and col in t.get("columns", []):
                mt[col] = semantic
        measure_types[d["id"]] = mt
    # Default attribution = the explicit ``isDefault`` primary, else the first
    # primary, else the first table (legacy requests resolve to the same id they
    # always did). Lookup tables never become the default.
    default_id = next((d["id"] for d in declared if d.get("isDefault")), None) or next(
        (d["id"] for d in declared if (d.get("role") or "primary") == "primary"), order[0])
    return {"order": order, "tables": tables_all, "roles": roles,
            "measureTypes": measure_types, "default": default_id}


def _ensure_default_kpis(spec: dict, table: dict, roles: dict, filters: Any) -> None:
    """Keep the compact KPI strip full (4 cards) without overwriting authored KPIs.

    The editor and fixed template already expect a 4-up strip. When an author
    provides fewer than four KPIs, backfill with safe, deterministic facts:
    row count, first dimension distinct count, first measure total, and second
    measure total (when available). These are display defaults only; the model
    can always author all four explicitly."""
    kpis = list(spec.get("kpis") or [])
    if len(kpis) >= 4:
        return
    columns = list(table.get("columns") or [])
    dims = [c for c, r in roles.items() if r in ("dimension", "time")]
    measures = [c for c, r in roles.items() if r == "measure"]
    used_labels = {str(k.get("label") or "") for k in kpis}

    def add(label: str, candidate: dict) -> None:
        if len(kpis) >= 4 or label in used_labels:
            return
        used_labels.add(label)
        kpis.append({"label": label, **candidate})

    def _backfill_kpi(col: str) -> None:
        # 缺省聚合按语义档（2026-10-13 接线 Flint resolveAggregationDefault）：
        # 显式 measureTypes.aggregation > aggRole 缺省（intensive→average）> sum。
        # 价格「合计」是把所有 Offer 价格加总，无读者数字（竞品街价报告病灶）；
        # intensive 指标补「均值」卡。
        from measure_semantics import default_aggregation
        mdef = (spec.get("measureTypes") or {}).get(col)
        agg = None
        if isinstance(mdef, dict) and mdef.get("aggregation"):
            agg = mdef["aggregation"]
        if agg is None:
            agg = default_aggregation(mdef, col)
        if agg is None:
            agg = "sum"
        label = f"{col}合计" if agg == "sum" else f"{col}均值"
        add(label, {"type": "total", "measure": col, "aggregation": agg})

    add("记录数", {"type": "count"})
    if dims:
        add(f"{dims[0]}数", {"type": "distinctCount", "dimension": dims[0]})
    if measures:
        _backfill_kpi(measures[0])
    if len(measures) > 1:
        _backfill_kpi(measures[1])
    spec["kpis"] = kpis


def _default_filter_candidates(table: dict, roles: dict) -> list[str]:
    """Seed the filter band with safe, low-cardinality dimension columns."""
    cardinality = (table.get("meta") or {}).get("cardinality") or {}
    candidates: list[str] = []
    for col in table.get("columns") or []:
        role = roles.get(col)
        if role not in ("dimension",):
            continue
        card = int(cardinality.get(col, 0) or 0)
        if card <= 0 and isinstance(table.get("rows"), list):
            idx = (table.get("columns") or []).index(col) if col in (table.get("columns") or []) else -1
            if idx >= 0:
                sample = table["rows"][:2000]
                card = len({str(row[idx]) for row in sample if idx < len(row) and row[idx] not in (None, "")})
        if card <= 0:
            continue
        # Prefer readable dimensions; avoid entity keys and huge lists.
        if re.search(r"^(id|sku|po|batch|code|no|号|批次|单号)", col, re.IGNORECASE):
            continue
        candidates.append((col, card))
    candidates.sort(key=lambda item: item[1])
    return [col for col, _ in candidates[:4]]


def normalize_request(spec: dict, table: dict, ctx: dict | None = None) -> dict:
    """Turn a validated request v1 into the compiler's internal shape.

    Single-dataset default: everything compiles against ``table`` exactly as v1.
    P1 multi-dataset (unrelated tables in one shell): ``ctx`` carries per-dataset
    tables/roles/measureTypes/order; every chart/KPI resolves its own datasetId
    (default: first dataset) so each table only aggregates its own rows. Report
    filters are projected per table. Report-level facts/insights/cover stay on
    the primary (first) dataset.

    P1 (repo repo docs/semantic-config-and-derived-redesign.md §5): before delegating
    to the per-dataset context we run two in-place normalisations so the rest
    of the compiler only ever sees the canonical shape:
      - ``measureTypes[col] = {type:'percent'}`` (no `format` field) is
        rewritten to ``{type:'number', format:'percent'}`` — `%` is a display
        format, not a type. ``type:'percent'`` stays accepted for the rare
        "raw 0-100 column that must not be summed" case.
      - The legacy top-level ``derivedColumns`` key has already been rejected
        by ``validate_request`` above; the new home is ``datasets[i].derived.columns``.
        For requests that still author per-dataset declarations without that
        field we leave the per-table slot empty.
    """
    # Keep direct invocations strict as well; report_build is not the only entry
    # point that can call this module.
    from validate_request import validate_request
    validate_request(spec)
    _normalize_percent_format(spec)
    _ensure_dataset_derived_slot(spec)
    ctx = ctx or _normalize_ctx(spec, table)
    ds_order: list = ctx["order"]
    ds_tables: dict = ctx["tables"]
    ds_roles: dict = ctx["roles"]
    ds_mt: dict = ctx["measureTypes"]
    dimension_keys = _derive_dimension_keys(spec.get("relations") or [])
    primary_id = ctx.get("default") or ds_order[0]

    table = ds_tables[primary_id]
    roles = ds_roles[primary_id]
    primary_filters = _filters_for_table(spec.get("filters"), table.get("columns") or [], dimension_keys, primary_id)
    facts = {fact["id"]: fact for fact in spec.get("facts", []) if fact.get("id")}
    fact_values = {fid: _compute_fact(fact, table, primary_filters) for fid, fact in facts.items()}
    if not _is_free(spec):
        _ensure_default_kpis(spec, table, roles, primary_filters)
    title = spec.get("title") or "数据分析报告"
    if len(ds_order) == 1:
        row_meta = f"{len(table['rows']):,} 条记录"
    else:
        total = sum(len((ds_tables[i] or {}).get("rows") or []) for i in ds_order)
        row_meta = f"{len(ds_order)} 张表 · {total:,} 行"
    # 封面组装：standard 档从 title/subtitle/analysisGoal 自动组装（请求声明的
    # cover 优先）；free 档声明即全部——spec.cover 缺席 = 整张报告没有封面。
    if _is_free(spec):
        cover = spec.get("cover") or None
    else:
        cover = spec.get("cover") or {"title": title, "meta": [spec.get("subtitle", ""), row_meta],
                                      "summary": spec.get("analysisGoal", "")}
    style = spec.get("style") or {}
    accent_map = {"blue": "#5645d4", "teal": "#2a9d99", "orange": "#dd5b00", "purple": "#7b3ff2"}
    out: dict[str, Any] = {"reportId": spec.get("reportId", "report"), "title": title,
                           "templateId": spec.get("templateId") or "standard-bi-v1",
                           "cover": cover, "kpis": [], "charts": [], "insights": [],
                           "source": spec.get("source") or {}, "filters": spec.get("filters") or [],
                           "filterCandidates": [] if _is_free(spec) else _default_filter_candidates(table, roles),
                           # 透传链双子陷阱（pageWidth 同款）：新顶层字段漏这里 =
                           # normalize 之后 spec 永远拿不到。texts/kpiPosition 是
                           # 2026-09-28「全部放开」批的新顶层字段。
                           "texts": [dict(t) for t in (spec.get("texts") or [])],
                           "kpiPosition": spec.get("kpiPosition"),
                           "runtime": dict(spec.get("runtime") or {}),
                           "style": style,
                           "sections": _normalize_sections(spec),
                           "provenance": spec.get("provenance") or {},
                           "columnRoles": dict(spec.get("columnRoles") or {}),
                           "dataTable": spec.get("dataTable"),
                           "fieldRoles": dict(spec.get("fieldRoles") or {}),
                           "hierarchy": list(spec.get("hierarchy") or []),
                           "relations": spec.get("relations") or [],
                           "pageWidth": spec.get("pageWidth"),
                           "dataSourceMode": spec.get("dataSourceMode") or "default",
                           "theme": {"name": (style.get("theme") or "notion"), "tokens": {"accent": accent_map.get(style.get("accent"), "#5645d4")}}}
    out["provenance"].setdefault("facts", fact_values)
    out["provenance"].setdefault("quality", (table.get("meta") or {}).get("quality", {}))
    measure_types = spec.get("measureTypes") or {}

    def _canon_kpi(kpi: dict) -> dict:
        """Map author-friendly KPI keys to the compiler's canonical names so a
        model that writes factType/column/title/format/basis still computes a
        real value instead of silently rendering "—". Canonical keys that are
        already present win; `basis` is no longer part of the contract and is
        dropped. Unknown keys are left untouched (validated elsewhere)."""
        out = dict(kpi)
        aliases = {"factType": "type", "column": "measure", "field": "measure",
                   "title": "label", "format": "measureType"}
        for src, dst in aliases.items():
            if src in out and dst not in out:
                out[dst] = out[src]
        out.pop("basis", None)
        return out

    # KPI 规范化循环：free 12 / standard 4（2026-09-28「全部放开」——旧裸 [:4]
    # 把 free 第 5 张起的 KPI 在 normalize 就静默丢掉，渲染端放开了也看不见）。
    _norm_kpi_cap = 12 if _is_free(spec) else 4
    for index, raw_kpi in enumerate(spec.get("kpis", [])[:_norm_kpi_cap]):
        kpi = _canon_kpi(raw_kpi)
        kid = kpi.get("datasetId") or primary_id
        ktbl = ds_tables[kid]
        kroles = ds_roles[kid]
        kmt = ds_mt[kid]
        kfilters = _filters_for_table(spec.get("filters"), ktbl.get("columns") or [], dimension_keys, kid)
        fact = facts.get(kpi.get("factId"))
        # measure 级默认聚合：KPI 未显式指定且带 measure 时，声明
        # measureTypes.aggregation > aggRole 缺省（intensive→average，2026-10-13
        # 接线 Flint resolveAggregationDefault）——价格/比率 KPI 不再默认 sum。
        if kpi.get("aggregation") is None and kpi.get("measure"):
            mdef = kmt.get(kpi["measure"])
            if isinstance(mdef, dict) and mdef.get("aggregation"):
                kpi["aggregation"] = mdef["aggregation"]
            else:
                from measure_semantics import default_aggregation
                _sem = default_aggregation(mdef, kpi["measure"])
                if _sem:
                    kpi["aggregation"] = _sem
        # A KPI may carry an author-verified ``value`` (often computed externally by
        # the agent, e.g. a growth rate or distinct-user count the model derived
        # from the data). Respect it verbatim instead of re-computing — otherwise
        # _compute_fact returns None for a value-only KPI and the card renders
        # "—". Only compute when the KPI supplies an explicit fact or a
        # measure+type/aggregation binding. `aggregation` alone is NOT a binding
        # (e.g. `{aggregation:"count"}` with no measure/type/kind) — it must not
        # push a value-only KPI into compute and lose the authored number.
        authored_value = kpi.get("value", kpi.get("rawValue"))
        has_binding = bool(kpi.get("measure") or kpi.get("factId") or kpi.get("type") or kpi.get("dimension"))
        # Cross-table rate KPI (P3): numerator/denominator bound to DIFFERENT tables.
        _dkt = (kpi.get("derived") or {}).get("type")
        _num = (kpi.get("derived") or {}).get("numerator")
        _den = (kpi.get("derived") or {}).get("denominator")
        cross_rate = None
        if _dkt == "rate" and isinstance(_num, dict) and isinstance(_den, dict) and (_num.get("datasetId") or _den.get("datasetId")):
            cross_rate = _compute_cross_rate(_num, _den, ds_tables, dimension_keys, spec)
        if cross_rate is not None:
            value = cross_rate
        elif authored_value is not None and not has_binding:
            value = authored_value
        elif fact:
            # Facts are authored per report on the primary dataset; a KPI bound to
            # another dataset recomputes its fact against that table directly.
            if kid == primary_id:
                value = fact_values.get(kpi.get("factId"))
            else:
                value = _compute_fact(fact, ktbl, kfilters)
        else:
            value = _compute_fact(kpi, ktbl, kfilters)
        # A KPI whose value cannot be computed (no authored value and no
        # resolvable fact/measure binding) is dropped at compile time — the
        # report never ships a card that renders "—".
        if value is None:
            continue
        measure_name = None if cross_rate is not None else ((fact or kpi).get("measure") if isinstance((fact or kpi), dict) else None)
        semantic_type = {"type": "percent"} if cross_rate is not None else (kpi.get("measureType") or (kmt.get(measure_name) if measure_name else None) or (fact or {}).get("type") or "number")
        # 口径驱动：delta 由声明的 derived 派生真算，而不是写死的一句话。
        # 若无法派生（无 derived 或输入不可解），回落到静态 delta 文本。
        derived = _compute_derived(kpi, ktbl, kroles, kfilters) if kpi.get("derived") else None
        delta_text = kpi.get("delta", "")
        delta_kind, delta_label = None, None
        if derived:
            delta_kind = derived.get("kind", "number")
            delta_label = derived.get("label")
            dval = derived.get("value")
            if dval is not None:
                if delta_kind == "percent":
                    delta_text = f"{float(dval) * 100:,.2f}".rstrip("0").rstrip(".") + "%"
                elif delta_kind == "currency":
                    # 币种符号查绑定指标的 measureTypes（拍板 1/6），缺省 ¥。
                    _dsym = currency_symbol((kmt or {}).get(measure_name) if measure_name else None)
                    delta_text = f"{_dsym}{float(dval):,.2f}".rstrip("0").rstrip(".")
                else:
                    delta_text = f"{float(dval):,.2f}".rstrip("0").rstrip(".")
        out["kpis"].append({"label": kpi.get("label", kpi.get("measure", "指标")),
                            "value": _format_fact(value, fact or kpi), "rawValue": value,
                            "semanticType": semantic_type, "factId": kpi.get("factId"),
                            "style": dict(kpi["style"]) if isinstance(kpi.get("style"), dict) else None,
                            "datasetId": kid,
                            "measure": measure_name, "delta": delta_text,
                            "deltaKind": delta_kind, "deltaLabel": delta_label,
                            "derived": kpi.get("derived"),
                            "labelOverridden": bool(kpi.get("labelOverridden", False)),
                            "deltaOverridden": bool(kpi.get("deltaOverridden", False)),
                            "accent": kpi.get("accent", ["blue", "green", "amber", "violet"][index % 4]),
                            "factType": (fact or kpi).get("type") if isinstance((fact or kpi), dict) else None,
                            "aggregation": (fact or kpi).get("aggregation") if isinstance((fact or kpi), dict) else None,
                            "countDim": ((fact or kpi).get("dimension") if isinstance((fact or kpi), dict) and (fact or kpi).get("type") in {"count", "distinctCount"} else None)})
    # Phase B (intent DSL): expand any Flint-style chartSpec into a classic
    # preset chart so the rest of the pipeline (roles, aggregation, layout,
    # manifest) handles it identically. compile_intent derives the viewType,
    # dimension, measures, aggregation, seriesType/seriesAxis and dimensionBucket
    # from the declared semantics + intent, so the author never writes options.
    semantic_types = spec.get("semantic_types") or {}
    if any(isinstance(c, dict) and c.get("chartSpec") for c in spec.get("charts", [])):
        from compile_intent import compile_chart_spec, IntentError
        _expanded = []
        for c in spec.get("charts", []):
            if isinstance(c, dict) and c.get("chartSpec"):
                if (c.get("datasetId") or primary_id) != primary_id:
                    raise ValueError(f"intent DSL (chartSpec) 图表 {c.get('id', '?')} 暂只支持主数据集")
                compiled = compile_chart_spec(c["chartSpec"], semantic_types, table)
                _i = len(_expanded) + 1
                nc = dict(compiled)
                nc["id"] = c.get("id", f"chart-{_i}")
                nc["presetId"] = VIEW_TO_PRESET.get(compiled.get("viewType"), compiled.get("viewType"))
                nc["title"] = compiled.get("title") or c.get("title", "")
                # preserve any author-supplied overrides the compiler didn't emit
                # （WP-3：chart.table 随 intent 产物一起透传，compile 落 editor.table）
                for _k in ("limit", "topK", "sort", "pagination", "topKOther", "table"):
                    if _k in c:
                        nc.setdefault(_k, c[_k])
                # chartSpec authors may use the limit alias; canonicalise to topK
                # so downstream (auto-truncation skip + runtime cap) sees one key.
                if nc.get("topK") is None and "limit" in c:
                    _lk = int(c.get("limit") or 0)
                    nc["topK"] = _lk or None
                if c.get("annotation"):
                    nc.setdefault("annotation", c["annotation"])
                _expanded.append(nc)
            else:
                _expanded.append(c)
        spec["charts"] = _expanded
    # 请求层允许 1-12 张图（validate_request: "charts must contain 1-12 items"），
    # normalize_request 是同一份请求的内部整形——旧版在这里 [:6] 截断，第 7 张起的
    # 图**通过校验却被编译静默丢弃**（第七轮审视实锤：7 图请求编译产物只有 6 张）。
    # 上限与 validate_request 同一档，不放第二份更小的清单。
    for index, chart in enumerate(spec.get("charts", [])[:12]):
        preset = chart.get("presetId") or chart.get("viewType")
        view = PRESET_TO_VIEW.get(preset, preset)
        # encoding 视图 = encoding 块 ∪ chart 级同名字段（chart 级不覆盖显式 encoding 声明）。
        # 旧写法 `or chart` 只在**完全没有** encoding 块时才回落到 chart 本体——一旦
        # chart.breakdown(s) 被校验层归一成小 encoding 块（拍板 4），chart 级
        # dimension/dimensions/measures 就被整体丢掉（行 3 维静默退 1 维）。合并视图让
        # 「encoding 内声明」与「chart 级声明」两个位点等价（batch B E2E 实测抓到）。
        _enc0 = chart.get("encoding")
        if isinstance(_enc0, dict):
            encoding = dict(_enc0)
            for _ck in ("dimension", "dimensions", "measures", "aggregation", "breakdown", "breakdowns",
                        "sort", "limit", "topK", "topKOther", "pagination", "bucket", "measureTypes", "roles"):
                if _ck not in encoding and chart.get(_ck) is not None:
                    encoding[_ck] = chart[_ck]
        else:
            encoding = chart
        # Cross-table chart: encoding.measures are {column, datasetId, ...} dicts
        # bound to DIFFERENT tables, aligned on a shared dimensionKey (P2).
        raw_measures = encoding.get("measures")
        cross = (isinstance(raw_measures, list) and raw_measures
                 and all(isinstance(m, dict) and m.get("datasetId") for m in raw_measures))
        # 里程碑 3：charts[].axis.dimensionKey + series[] 作为一等字段（替代手工
        # encoding.measures+datasetId）。axis.dimensionKey 必须已声明为共享维度。
        axisSpec = chart.get("axis") or {}
        seriesSpec = chart.get("series") or []
        if not cross and axisSpec.get("dimensionKey") and isinstance(seriesSpec, list) and seriesSpec \
           and all(isinstance(s, dict) and s.get("datasetId") for s in seriesSpec):
            cross = True
            raw_measures = seriesSpec
            encoding = dict(encoding)
            encoding["dimension"] = axisSpec.get("dimensionKey")
        if cross:
            dim_key = encoding.get("dimension")
            group = (dimension_keys or {}).get(dim_key) or {}
            group_tables = group.get("tables") or []
            if not group_tables:
                raise ValueError(f"跨表图表 {chart.get('id','?')} 的维度 {dim_key!r} 不是已声明的共享维度（请在 relations 里声明 same-domain 对齐）")
            measures = [{"column": m["column"], "datasetId": m["datasetId"],
                         "aggregation": m.get("aggregation", encoding.get("aggregation") or "sum")}
                        for m in raw_measures]
            for m in measures:
                mtbl = ds_tables[m["datasetId"]]
                if m["column"] not in mtbl["columns"]:
                    raise ValueError(f"跨表图表 {chart.get('id','?')} 指标 {m['column']!r} 不在数据集 {m['datasetId']}")
            filters_by_ds = {ds: _filters_for_table(spec.get("filters"), ds_tables[ds].get("columns") or [], dimension_keys, ds)
                             for ds in {m["datasetId"] for m in measures} | {e["datasetId"] for e in group_tables}}
            cols, rows = _cross_table_merge(dim_key, group_tables, measures, ds_tables, filters_by_ds)
            # WP-3：align 键=真实列名，跨表图表用合并后的真实列核对。
            _check_table_align_columns(chart, cols)
            # WP-14：columnFilters 同款存在性核对（跨表合并列宇宙）。
            _check_table_column_filters(chart, cols)
            # WP-11：totalCalc 同款存在性核对（宇宙=合并列 ∪ 该卡声明的派生指标名
            # ——拼错列名报可行动错误，拼对但不可加走 _effective_table_total_calc
            # 的静默闸门；与单表分支三道对称）。
            _check_table_total_calc_columns(chart, cols, chart.get("derivedMeasures") or [])
            measure_types = {}
            for m in measures:
                mm = ds_mt[m["datasetId"]].get(m["column"])
                if mm:
                    measure_types[m["column"]] = mm
            out["charts"].append({"id": chart.get("id", f"chart-{index+1}"), "title": chart.get("title", ""),
                                  "annotation": chart.get("annotation", ""),
                                  "annotationPlacement": chart.get("annotationPlacement", "below"),
                                  "style": dict(chart["style"]) if isinstance(chart.get("style"), dict) else None,
                                  # layout 恒 dict（不许 None）：pack_layout 显式坐标分支把
                                  # 图表**原样**透传给 build_html，那边 `c.get("layout", {})`
                                  # 的默认值只救「键缺失」不救「键=None」——曾经任一卡带
                                  # x/y 且其余卡无 layout 时整条构建 AttributeError 崩死
                                  # （free+草图转译是部分坐标的设计入口，真机复现）。
                                  "layout": dict(chart["layout"]) if isinstance(chart.get("layout"), dict) else {},
                                  "datasetId": group_tables[0]["datasetId"],
                                  "viewType": view, "dimension": dim_key, "measures": [m["column"] for m in measures],
                                  "aggregation": measures[0]["aggregation"], "measureTypes": measure_types,
                                  "roles": {}, "filter": spec.get("filters") or [],
                                  "seriesType": None, "seriesAxis": None, "dimensionBucket": None,
                                  "data": {"columns": cols, "rows": rows}, "sort": ("time" if any(e.get("bucket") for e in group_tables) else "none"),
                                  "limit": int(encoding.get("limit", 500)),
                                  "topK": (int(encoding.get("topK") or 0) or None)
                                  if encoding.get("topK") is not None or "limit" not in encoding
                                  else int(encoding.get("limit") or 0),
                                  "topKOther": encoding.get("topKOther") is True,
                                  "pagination": encoding.get("pagination") is True,
                                  # 表格卡作者设置（批次一 + WP-3）：跨表图表同一条
                                  # 透传契约，compile 落 editor.table，不静默丢设置。
                                  "table": dict(chart["table"]) if isinstance(chart.get("table"), dict) else None,
                                  "_crossTable": {"dimensionKey": dim_key, "measures": measures, "dimensionTables": group_tables}})
            continue
        dsid = chart.get("datasetId") or primary_id
        tbl = ds_tables[dsid]
        rl = ds_roles[dsid]
        mt = ds_mt[dsid]
        chart_filters = _filters_for_table(spec.get("filters"), tbl.get("columns") or [], dimension_keys, dsid)
        # WP-3：align 的「键=真实列名」存在性在编译侧核（此处 canonical 表列已含
        # 物化计算列/维度值分组列——_materialize_derived 先于 normalize_request）。
        _check_table_align_columns(chart, tbl.get("columns") or [])
        # WP-14：columnFilters 同款存在性核对（宇宙=全部真实列，过滤器字段不要求
        # 出现在行/列区——QBI「隐藏字段仍可过滤」的组件级声明位）。
        _check_table_column_filters(chart, tbl.get("columns") or [])
        # WP-11：totalCalc 同款存在性核对（宇宙含聚合级派生指标名——拼错报错，
        # 拼对但不可加走 _effective_table_total_calc 的静默闸门）。
        _check_table_total_calc_columns(
            chart, tbl.get("columns") or [],
            [n.get("name") for n in _derived_measure_nodes(spec, dsid) if isinstance(n, dict)])
        dim = encoding.get("dimension") or next((c for c, r in rl.items() if r in {"dimension", "time"}), None)
        if not dim:
            dim = next((c for c in tbl.get("columns", []) if rl.get(c) not in {"measure", "id", "ignore"}), (tbl.get("columns") or [""])[0])
        # 多维表格批 B 补口（docs/multidim-table-and-currency-design-2026-09-28.md §3.7，
        # 3×3 验收报告实测）：作者显式声明 dimensions（行区有序数组）而未声明 dimension
        # 时，声明即完整行区——不再把缺省时间列垫进首位（垫入使行维数 +1，≥4 时运行时
        # ND 泄压阀吞掉交叉矩阵，真机四卡全部退化为长表）。dimension 取行区首列，维持
        # 「dimension 恒在 dimensions 第 1 位」不变式，静态 option/排序管道照旧。
        _authored_dims = encoding.get("dimensions") or []
        if _authored_dims and not encoding.get("dimension"):
            dim = _authored_dims[0]
        breakdown = encoding.get("breakdown") or (encoding.get("roles") or {}).get("breakdown")
        # 多维表格批 B（拍板 4，docs/multidim-table-and-currency-design-2026-09-28.md
        # §3.7）：breakdowns（1–3 项列表）为多层列维权威；validate_request 已做互斥
        # 校验。产物 roles 恒写 cols 数组 + breakdown=cols[0] 镜像（运行时 tblColDimsOf
        # 读 cols、缺省回退 breakdown——读时迁移，旧产物零破坏）。仅有单 breakdown 时
        # 保持旧形状（roles 只带 breakdown），产物与历史逐字节一致。
        _bd_list = encoding.get("breakdowns") or []
        if _bd_list:
            _bd_list = [str(d) for d in _bd_list][:3]
            if not breakdown:
                breakdown = _bd_list[0]
            # 拆分维度存在性核对（对齐 _deterministic_rows 的 breakdown 单列检查——
            # cols[1..] 不进静态行，拼错列只会在运行时聚合期爆，编译期先给可行动错）。
            _unknown_bd = [d for d in _bd_list if d not in (tbl.get("columns") or [])]
            if _unknown_bd:
                raise ValueError(
                    f"图表 {chart.get('id', '?')} 的 chart.breakdowns 引用了不存在的列：{_unknown_bd}")
        elif breakdown:
            # 单 breakdown 也归一进 cols 数组（恒等式：roles 带交叉列 ⇒ cols 必在场；
            # 运行时 tblColDimsOf 读 cols、缺省回退 breakdown——旧产物读路径零破坏）。
            _bd_list = [breakdown]
        db = chart.get("dimensionBucket")
        if db is None and encoding.get("bucket") and dim:
            db = {"source": dim, "bucketed": dim, "bucket": encoding.get("bucket")}
        authored_measures = encoding.get("measures")
        if authored_measures:
            measures = list(authored_measures)
        else:
            measures = [c for c, r in rl.items() if r == "measure"][:3]
            if not measures:
                # 记录型数据：没有指标列时，行数（COUNT(*)）就是指标。
                measures = [COUNT_MEASURE]
        authored_agg = encoding.get("aggregation")
        if authored_agg is None:
            if measures and measures[0] == COUNT_MEASURE:
                agg = "count"
            else:
                # measure 级默认聚合（2026-10-13 接线 Flint
                # resolveAggregationDefault）：measureTypes[measure].aggregation
                # 显式声明 > aggRole 缺省（intensive→average，如 Price/Percentage/
                # Score）> sum。此前 aggregationDefault 两端定义零消费者，缺省恒
                # sum——价格/比率列被当销售额加总。解析值会烘进产物 aggregation，
                # 运行时重聚合与静态 option 同源（WYSIWYG）。
                mdef = mt.get(measures[0]) if measures else None
                mdef_agg = mdef.get("aggregation") if isinstance(mdef, dict) else None
                if mdef_agg is None:
                    from measure_semantics import default_aggregation
                    mdef_agg = default_aggregation(mdef, measures[0] if measures else "")
                agg = mdef_agg or "sum"
        else:
            agg = authored_agg
        # Per-chart measure semantics (shared by the derived-measure evaluation below
        # and the emitted chart entries): an encoding-level override wins over the
        # dataset's measureTypes map.
        chart_mt = dict(encoding.get("measureTypes") if encoding.get("measureTypes") is not None else mt)
        _used_der: list = []   # aggregate-level derived measures used by THIS chart (see below)
        if view == "scatter":
            # Scatter plots (x, y) pairs per data point. Emitting one row per raw
            # table row bakes O(rows) points into the static option (e.g. 5000),
            # bloating the HTML and mirroring the runtime re-aggregation. Instead,
            # group by the dimension and aggregate the two value axes (sum/avg/...)
            # so the static option matches the runtime render (e.g. one point per
            # 商品类目). When no dimension is supplied, fall back to raw rows so a
            # true per-row correlation is still possible.
            if dim:
                _der_nodes = _derived_measure_nodes(spec, dsid)
                _used_der = [n for n in _der_nodes if n.get("name") in set(measures[:2])]
                if _used_der:
                    _bucket = (db or {}).get("bucket") or _raw_time_bucket(tbl, dim, rl.get(dim))
                    rows = _rows_with_derived(tbl, dim, measures[:2], _der_nodes, agg, breakdown, chart_filters,
                                              int(encoding.get("limit", 500)), chart_mt,
                                              {"bucket": _bucket} if _bucket else None)
                else:
                    rows = _deterministic_rows(tbl, dim, measures[:2], agg, breakdown, chart_filters, int(encoding.get("limit", 500)))
                data_columns = ([dim] if dim else ["维度"]) + measures[:2]
            else:
                # 无维度兜底：行首用**行号**（旧版误用外层 enumerate 的图表序号，
                # 所有点 name 相同），且必须过图级筛选（聚合分支传了 chart_filters）。
                _sc_cols = tbl["columns"]
                rows = [[i, *[r[_sc_cols.index(m)] for m in measures[:2]]]
                        for i, r in enumerate(tbl["rows"]) if _row_filter(r, _sc_cols, chart_filters)]
                data_columns = ([dim] if dim else ["维度"]) + measures[:2]
        else:
            # Aggregate-level derived measures (dataset.derived.measures): the static
            # option must contain their EVALUATED series, or the first paint (and any
            # export) shows a card the runtime only fills in after an edit (the old
            # chart-scoped mom/yoy was runtime-only — the WYSIWYG gap this closes).
            _der_nodes = _derived_measure_nodes(spec, dsid)
            _used_der = [n for n in _der_nodes if n.get("name") in set(measures)]
            if _used_der:
                _bucket = (db or {}).get("bucket") or _raw_time_bucket(tbl, dim, rl.get(dim))
                rows = _rows_with_derived(tbl, dim, measures, _der_nodes, agg, breakdown, chart_filters,
                                          int(encoding.get("limit", 500)), chart_mt,
                                          {"bucket": _bucket} if _bucket else None)
            else:
                rows = _deterministic_rows(tbl, dim, measures, agg, breakdown, chart_filters, int(encoding.get("limit", 500)))
            data_columns = ([dim] if dim else []) + ([breakdown] if breakdown else []) + [_measure_name(m) for m in measures]
        # Strategy preference: a multi-measure grouped bar is expressed as a
        # bar+line composite (first measure bars, second measure line) so two
        # metrics do not compete as indistinguishable same-colour bars. Authors
        # may still override via chart.seriesType/seriesAxis.
        composite_series_type = dict(chart["seriesType"]) if isinstance(chart.get("seriesType"), dict) else None
        composite_series_axis = dict(chart["seriesAxis"]) if isinstance(chart.get("seriesAxis"), dict) else None
        if view == "bar" and len(measures) >= 2 and not breakdown and composite_series_type is None:
            composite_series_type = {measures[0]: "bar"}
            for _mi, _m in enumerate(measures[1:], 1):
                composite_series_type[_m] = "line" if _mi == 1 else "bar"
            composite_series_axis = {}
        if COUNT_MEASURE in measures:
            chart_mt.setdefault(COUNT_MEASURE, {"type": "count"})
        # A derived measure's own semantic type rides into the chart's format map
        # (作者不必再写一遍 measureTypes): a window `output:'rate'` renders as %,
        # a delta/formula keeps its number format. An explicit author override wins.
        for _n in _used_der:
            if _n.get("measureType") and _n["name"] not in chart_mt:
                chart_mt[_n["name"]] = dict(_n["measureType"])
        # A trend (line/area) over a detected time dimension keeps its
        # chronological order by default (sort=time), so an authored trend never
        # renders a count-desc jumbled date axis. Other charts default to
        # value-desc (R3 rank) — but a **declared time dimension** keeps chrono
        # on EVERY view type (R3 只对 category 轴排名；时间轴任何图表都按日历序，
        # 与运行时 sortEntriesDefault 的 isTime 默认一致，否则首次重聚合即翻轴)。
        authored_sort = encoding.get("sort")
        default_sort = "desc"
        if authored_sort is None and dim and rl.get(dim) == "time":
            default_sort = "time"
        if authored_sort is None:
            authored_sort = default_sort
        out["charts"].append({"id": chart.get("id", f"chart-{index+1}"), "title": chart.get("title", ""),
                              "annotation": chart.get("annotation", ""),
                              "annotationPlacement": chart.get("annotationPlacement", "below"),
                              "style": dict(chart["style"]) if isinstance(chart.get("style"), dict) else None,
                              # 与单表路径同款：layout 恒 dict，None 会在 pack_layout
                              # 显式分支 + build_html `get("layout", {})` 上崩（见上注）。
                              "layout": dict(chart["layout"]) if isinstance(chart.get("layout"), dict) else {},
                              "datasetId": dsid,
                              "viewType": view, "dimension": dim, "measures": measures,
                              "aggregation": agg, "measureTypes": chart_mt,
                              # 占比堆叠（2026-10-13 语义批）：图级 stackMode 透传，
                              # 运行时 breakdownSeries 与静态 option 同闸。
                              **({"stackMode": encoding["stackMode"]} if encoding.get("stackMode") == "normalize" else {}),
                              "roles": ({"cols": list(_bd_list), "breakdown": _bd_list[0]} if _bd_list else {}),
                              # 行维度有序列表（P2 三维交叉表）：dimension 恒在第 1 位
                              "dimensions": (lambda dl: ([dim] + [d for d in dl if d != dim] if dim else dl))(encoding.get("dimensions") or []) or None,
                              "filter": chart_filters,
                              "seriesType": composite_series_type,
                              "seriesAxis": composite_series_axis,
                              "dimensionBucket": db,
                              # Aggregate-level derived measures used by this chart
                              # (internal): option building keeps their missing cells
                              # as null instead of 0, and it documents which series
                              # the static option evaluated.
                              **({"derivedMeasures": [n["name"] for n in _used_der]} if _used_der else {}),
                              # 派生节点全文（内部）：_fmt_cell 的 percent 豁免
                              # 判据（_derived_percent_units）要读 kind/output，
                              # 名单本身不够；build_manifest 只挑字段，不会外泄。
                              **({"_derived_nodes": {n["name"]: n for n in _used_der}} if _used_der else {}),
                              "data": {"columns": data_columns, "rows": rows}, "sort": authored_sort,
                              "limit": int(encoding.get("limit", 500)),
                              "topK": (int(encoding.get("topK") or 0) or None)
                              if encoding.get("topK") is not None or "limit" not in encoding
                              else int(encoding.get("limit") or 0),
                              "topKOther": encoding.get("topKOther") is True,
                              "pagination": encoding.get("pagination") is True,
                              # 表格卡作者设置（批次一）：intent 产物透传，compile 落 editor.table。
                              "table": dict(chart["table"]) if isinstance(chart.get("table"), dict) else None})
    for insight in spec.get("insights", [])[:3]:
        if isinstance(insight, dict):
            # A dict insight may use any of several keys. Prefer explicit text,
            # then compose title+body from the common fields so author-shaped
            # insights ({title, content}, {description}, {finding}) render as a
            # real bullet instead of an empty <li>.
            body = insight.get("text") or insight.get("content") or insight.get("description") \
                or insight.get("body") or insight.get("summary") or insight.get("finding") or ""
            title = insight.get("title") or insight.get("heading") or ""
            text = f"{title}：{body}" if title and body else (title or body or str(insight))
        else:
            text = str(insight)
        for fid, value in fact_values.items():
            text = text.replace("{{fact:" + fid + "}}", _format_fact(value, facts.get(fid)))
        out["insights"].append(text)
    return out


def compile_spec(spec_path: str, out_path: str, dataset_path: str | None = None,
                 dataset_id: str = "primary", dataset_label: str = "明细",
                 datasets: list | None = None, column_stats_path: str | None = None,
                 inherit: dict | None = None, report_id: str | None = None,
                 sort_input: bool = False) -> str:
    """Compile a request into a draft report.

    Single-table legacy: ``dataset_path``/``dataset_id``/``dataset_label`` name
    one table. P1 multi-table (unrelated tables in one shell): ``datasets`` is
    an ordered list of {id, label, file} covering EVERY dataset — the first is
    the primary/default slot; each file is a canonical JSON table (or an
    .xlsx/.csv/.tsv read by table_input). Charts/KPIs resolve their own
    datasetId (default: first dataset).

    ``inherit``（月度刷新继承）= 上一份 manifest（curation_inherit.pick_prev_manifest
    挑出）。dataset 级策展按列名继承进本次构建：请求显式声明 > 继承；列已不存在的
    继承键在表装载后被裁掉并打印报告，绝不带病进物化器。"""
    with open(spec_path, encoding="utf-8") as f:
        spec = json.load(f)
    # An unauthored spec reportId resolves to the build's ASCII slug (the
    # registry key): report_build passes --report-id on every path, but until
    # now the flag was never READ, so pure-singlefile manifests shipped
    # reportId "report" (download names collide; server registration re-slugs
    # its copy but the in-file manifest kept the placeholder).
    if spec.get("reportId") in (None, "", "report") and report_id:
        spec["reportId"] = report_id
    from validate_request import validate_request
    validate_request(spec)
    inherit_map: dict = {}
    display_pending: dict = {}
    if isinstance(inherit, dict):
        from curation_inherit import merge_with_map
        spec, _inh_notes, inherit_map, display_pending = merge_with_map(spec, inherit)
        for _n in _inh_notes:
            print(f"[inherit] {_n}")
    # 强制所有报告都以「页签」为默认渲染形式：若 spec 未提供 sections，则
    # 生成一个默认的「正文」页签把全部图表收进云，让 build_manifest / render_tabs_html
    # 走单一页签路径。运行时与下游的页签逻辑（recomputeTabs、stampTabs、tabCard）也
    # 依耆这个页签存在才会被点亮。runtime.tabs="off" 会跳过补生。
    spec = _ensure_default_sections(spec)
    declared_ds = list(spec.get("datasets") or [])
    # 派生声明按「数据集各管各」读取（2026-09-22 D1/P2：单数据集假设随构建期
    # relations 逻辑直通一起退场——A 表的派生引用 A 表列，物化/校验/烘焙全部
    # 逐数据集循环；不再有 datasets[0] 特权槽）。
    def _ds_decl(ds_id) -> dict:
        for _x in declared_ds:
            if isinstance(_x, dict) and _x.get("id") == ds_id:
                return _x
        return {}
    if datasets is None and dataset_path:
        datasets = [{"id": dataset_id, "label": dataset_label, "file": dataset_path}]
    # 列统计（数据源配置的字段副行 / 数据质量）：唯一真源是 report_build 的 profile
    # 产物（profile_table.column_stats）。单表 **server** 模式刻意不写 canonical JSON
    # （335k 行中间产物可达 GB 级），所以那份 profile 必须显式传进来；多数据集/合并
    # 宽表路径传的是 canonical JSON，``meta.stats`` 已随之携带，这里只是兜底。
    stats_by_id: dict = {}
    if column_stats_path:
        try:
            _prof = json.loads(Path(column_stats_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _prof = {}
        _stats = _prof.get("columnStats") if isinstance(_prof, dict) else None
        if isinstance(_stats, dict) and _stats:
            stats_by_id[dataset_id] = _stats

    def _column_stats_for(raw_id: str, table_obj: dict) -> dict:
        got = stats_by_id.get(raw_id)
        if isinstance(got, dict) and got:
            return dict(got)
        return dict((table_obj.get("meta") or {}).get("stats") or {})

    if not datasets:
        # No data provided (schema-only / manifest-less compile path).
        tables_by_id = {}
    else:
        tables_by_id = {}
        for d in datasets:
            raw = d.get("raw") or d["id"]
            fp = Path(d["file"])
            if fp.suffix.lower() == ".json":
                tables_by_id[raw] = json.loads(fp.read_text(encoding="utf-8"))
            else:
                from table_input import load_table_file
                tables_by_id[raw] = load_table_file(fp, tidy=bool(sort_input))
        # P1: derive columns live under datasets[i].derived.columns. The
        # compile path reads from the first dataset's slot — multi-dataset
        # derived columns remain a P1.5 boundary (already noted in docs §2).
        if inherit_map:
            # 继承的策展先按真实列裁剪（列被删/改名的键丢弃并报告），再进物化器
            # ——materialize/validate_measures 对坏引用是抛错，不裁就会让月度刷新
            # 因为一条过期声明而整体失败。
            from curation_inherit import prune as _ci_prune
            for _n in _ci_prune(spec, tables_by_id, datasets, inherit_map):
                print(f"[inherit] {_n}")
        # 行级计算列 / 维度值分组：逐数据集物化（声明在哪张表，就物化到哪张表）。
        for _d in datasets:
            # 匹配键用 raw（请求级 id）：server 模式的 plan.id 带报告命名空间
            # （{reportId}__{id}，Issue #8），声明白名单里没有这个名字。
            _decl = _ds_decl(_d.get("raw") or _d["id"])
            if not _decl:
                continue
            _raw = _d.get("raw") or _d["id"]
            if _raw not in tables_by_id:
                continue
            _own_cols = ((_decl.get("derived") or {}).get("columns") or [])
            if _own_cols:
                tables_by_id[_raw] = _materialize_derived(tables_by_id[_raw], _own_cols)
            # 维度值分组（dimGroups）：与计算列同一条「物化成真列」管线 —— 物化后
            # 聚合/表格/筛选把它当普通维度，编译/服务端/运行时三端零第二实现。
            _own_groups = _decl.get("dimGroups") or []
            if _own_groups:
                import derived_eval as _de
                tables_by_id[_raw] = _de.materialize_dim_groups(tables_by_id[_raw], _own_groups)
        for _d in declared_ds:
            # 增量合并主键（datasets[].primaryKey）：请求层校验过形状，列存在性
            # 在这里对真实表再校验一次（请求层不读文件）——拼错的列名在构建期
            # 就给出可行动报错，而不是等第一次增量合并才失败。
            _pk = _d.get("primaryKey")
            if _pk:
                _raw = _d.get("id")
                _real = list((tables_by_id.get(_raw) or {}).get("columns") or [])
                _miss = [c for c in _pk if c not in _real]
                if _miss:
                    raise ValueError(
                        f"dataset {_raw} primaryKey 引用了表中不存在的列：{'、'.join(_miss)}")
        # 聚合级派生指标：声明/归一/校验在 derived_model（请求层），这里对**各自
        # 数据集的真实表列**再校验一次（请求层只见过 columnRoles/measureTypes，
        # 从不见真实表）。逐数据集循环——曾经「只认 datasets[0] + 多数据集报错」
        # 的单数据集闸随 relations 逻辑直通一起退场。
        for _d in datasets:
            _decl = _ds_decl(_d.get("raw") or _d["id"])
            _own_meas = [dict(x) for x in ((_decl.get("derived") or {}).get("measures") or [])]
            if not _own_meas:
                continue
            import derived_model
            _raw = _d.get("raw") or _d["id"]
            real_cols = list((tables_by_id.get(_raw) or {}).get("columns") or [])
            declared_roles = dict(spec.get("columnRoles") or {})
            declared_roles.update((_decl.get("columnRoles") or {}))
            time_cols = [c for c, r in declared_roles.items() if r == "time" and c in real_cols]
            try:
                derived_model.validate_measures(
                    _own_meas, real_cols, time_cols,
                    where=f"dataset {_raw} derived.measures")
            except derived_model.DerivedError as exc:
                raise ValueError(str(exc)) from None
        primary_raw = datasets[0].get("raw") or datasets[0]["id"]
        primary_table = tables_by_id[primary_raw]
        authored_mt = dict(spec.get("measureTypes") or {})
        if not declared_ds and datasets:
            # Legacy single-table path may carry a non-default id (server register
            # namespaces it as {reportId}__primary): build the per-dataset context
            # keyed by THAT id so charts/datasets/ctx ids stay aligned.
            pseudo = dict(spec)
            pseudo["datasets"] = [{"id": primary_raw,
                                   "columnRoles": spec.get("columnRoles"),
                                   "measureTypes": spec.get("measureTypes")}]
            ctx = _normalize_ctx(pseudo, primary_table, tables_by_id)
        else:
            ctx = _normalize_ctx(spec, primary_table, tables_by_id)
        spec = normalize_request(spec, primary_table, ctx)
        # Mixed-mode (repo repo docs/mixed-mode-design.md §3): remote datasets are emitted
        # schema-only under their final (namespaced) id; charts/KPIs bound to
        # them are rewritten so the runtime fetches the namespaced dataset.
        remote_final = {}
        for d in datasets:
            raw = d.get("raw") or d["id"]
            if d.get("remote"):
                remote_final[raw] = d["id"]
        if remote_final:
            for _c in spec.get("charts", []):
                if _c.get("datasetId") in remote_final:
                    _c["_dsRaw"] = _c["datasetId"]
                    _c["datasetId"] = remote_final[_c["datasetId"]]
            for _k in spec.get("kpis", []):
                if _k.get("datasetId") in remote_final:
                    _k["datasetId"] = remote_final[_k["datasetId"]]
        for d in datasets:
            raw = d.get("raw") or d["id"]
            t = tables_by_id[raw]
            if d.get("remote"):
                # Schema-only: the browser fetches aggregates from the registry
                # table keyed by this namespaced id (Phase B runtime remote path).
                ds = {"id": d["id"], "label": d.get("label", d["id"]),
                      "columns": t["columns"], "rowCount": len(t.get("rows") or []), "remote": True,
                      "role": _ds_role(declared_ds, d.get("raw") or d["id"]),
                      "isDefault": (d.get("raw") or d["id"]) == ctx.get("default") or len(datasets) == 1,
                      "columnTypes": dict((t.get("meta") or {}).get("types") or {}),
                      "columnStats": _column_stats_for(raw, t),
                      "columnRoles": dict(ctx["roles"].get(d.get("raw") or d["id"]) or {})}
                # Row-level derived columns must ride along in REMOTE (mixed /
                # server) mode too: the server holds no rows in the manifest, so
                # the declaration is the only way it can re-materialise the same
                # columns into SQLite (server/derived_columns.py). Without this
                # the manifest claimed the column existed while the table did not.
                # Aggregate-level derived measures (P1 model) ride along for the
                # same reason: the runtime/UI reads them from the dataset.
                # 声明按所属数据集透传（2026-09-22 逐数据集化）：不再是 datasets[0] 特权。
                _own = _ds_decl(d.get("raw") or d["id"]) or _ds_decl(d["id"])
                decl = {}
                _own_cols = [dict(x) for x in ((_own.get("derived") or {}).get("columns") or [])]
                _own_meas = [dict(x) for x in ((_own.get("derived") or {}).get("measures") or [])]
                if _own_cols:
                    decl["columns"] = _own_cols
                if _own_meas:
                    decl["measures"] = _own_meas
                if decl:
                    ds["derived"] = decl
                _own_groups = _own.get("dimGroups") or []
                if _own_groups:
                    # 维度值分组声明同路透传：注册 payload（report_build
                    # groups_by_ds）从 manifest.datasets[].dimGroups 读取；
                    # 声明丢了 server 就不会物化这些列，server 报告与
                    # singlefile（JS 镜像兜底）行为分叉。
                    ds["dimGroups"] = [dict(g) for g in _own_groups]
                _pk_decl = _ds_primary_key(declared_ds, d.get("raw") or d["id"])
                if _pk_decl:
                    ds["primaryKey"] = _pk_decl
                spec.setdefault("datasets", []).append(ds)
                continue
            ds = {"id": d["id"], "label": d.get("label", d["id"]),
                  "columns": t["columns"], "rows": t["rows"],
                  "role": _ds_role(declared_ds, d.get("raw") or d["id"]),
                  "isDefault": (d.get("raw") or d["id"]) == ctx.get("default") or len(datasets) == 1,
                  "columnTypes": dict((t.get("meta") or {}).get("types") or {}),
                  "columnStats": _column_stats_for(raw, t),
                  "columnRoles": dict(ctx["roles"].get(d.get("raw") or d["id"]) or {})}
            # Keep the whitelist formulas so the runtime recomputes the
            # materialised column after data updates / grid edits. The
            # manifest shape is the canonical ``{"columns": [...]}`` slot
            # (mirrors ``spec.datasets[i].derived.columns``); legacy shape
            # was a flat list — see docs §5 for the normalisation rule.
            # ``measures`` (aggregate-level nodes) ride along when declared.
            # 声明按所属数据集透传（2026-09-22 逐数据集化）。
            _own = _ds_decl(d.get("raw") or d["id"]) or _ds_decl(d["id"])
            decl = {}
            _own_cols = [dict(x) for x in ((_own.get("derived") or {}).get("columns") or [])]
            _own_meas = [dict(x) for x in ((_own.get("derived") or {}).get("measures") or [])]
            if _own_cols:
                decl["columns"] = _own_cols
            if _own_meas:
                decl["measures"] = _own_meas
            if decl:
                ds["derived"] = decl
            _own_groups = _own.get("dimGroups") or []
            if _own_groups:
                ds["dimGroups"] = [dict(g) for g in _own_groups]
            # Field semantic roles + hierarchy live per dataset at runtime. The
            # top-level authored roles/hierarchy belong to the primary dataset
            # (default slot); later datasets may declare their own hierarchy.
            if d["id"] == datasets[0]["id"]:
                if "fieldRoles" not in ds:
                    ds["fieldRoles"] = dict(spec.get("fieldRoles") or {})
                if "hierarchy" not in ds:
                    ds["hierarchy"] = list(spec.get("hierarchy") or [])
            else:
                declared_h = next((x.get("hierarchy") for x in declared_ds if x.get("id") == d["id"]), None)
                if declared_h:
                    ds["hierarchy"] = list(declared_h)
            if "measureTypes" not in ds:
                # Seed each dataset's measureTypes from its own semantics (source
                # of truth) merged with any name-inferred types, so the runtime
                # resolves a KPI/chart measureType on binding change (口径联动).
                inferred = infer_measure_types(t["columns"]) or {}
                inferred.update(ctx["measureTypes"][d["id"]])
                ds["measureTypes"] = inferred
            _pk_decl = _ds_primary_key(declared_ds, d.get("raw") or d["id"])
            if _pk_decl:
                ds["primaryKey"] = _pk_decl
            spec.setdefault("datasets", []).append(ds)
    # C: cardinality-aware overflow filter — truncate comparison charts to a
    # readable top-N and note it in provenance (Flint filterOverflow pattern).
    truncations = _filter_cardinality(spec)
    # B: throw-before-render — reject charts whose shape cannot match the type.
    for c in spec.get("charts", []):
        problems = validate_chart_shape(c)
        if problems:
            raise ValueError(f"spec chart '{c.get('id')}' invalid: " + "; ".join(problems))
    primary_ds_id = (datasets or [{"id": dataset_id}])[0]["id"]
    # meta.dataAsOf 的取数表 = 主数据集的**原始**表（tables_by_id 按 raw id 键；
    # server/mixed 下 primary 可能是 remote 命名空间槽，按 final id 查会落空）。
    _primary_table = None
    if datasets:
        _primary_table = (tables_by_id or {}).get(datasets[0].get("raw") or datasets[0]["id"])
    # C3（2026-09-25）：cover_h 需要「封面会不会有数据截至行」的知识，提前用同一
    # _data_asof_from_table（同一主表 + 同一 roles 合并口径，与 build_manifest 逐位
    # 同参）算一次；meta/cover.dataAsOf 的权威落盘仍在 build_manifest。
    _asof_roles = dict(spec.get("columnRoles") or {})
    _asof_ds = next((d for d in (spec.get("datasets") or [])
                     if isinstance(d, dict) and d.get("id") == primary_ds_id), None)
    if isinstance(_asof_ds, dict):
        _asof_roles.update(_asof_ds.get("columnRoles") or {})
    _cover_asof = _data_asof_from_table(_primary_table, _asof_roles) if _primary_table is not None else None
    cover_h = _cover_h(spec, asof=_cover_asof)
    theme = _load_theme(spec)
    # Fixed header order (cover → filter → KPI → charts). Charts pack after the
    # filter bar (cover_h + filter_h) and the compact KPI row.
    kpi_rows = max(1, (len(spec.get("kpis", [])) + 3) // 4) if spec.get("kpis") else 0
    # The filter card renders whenever filters OR seeded candidates exist
    # (build_manifest / render_grid_html use max(filters, candidates)); the
    # chart packing start must reserve the SAME height or the first chart row
    # overlaps the filter card + KPI strip in the static grid.
    filter_h = _filter_h(max(len(_flatten_filters(spec.get("filters") or [])),
                             len(spec.get("filterCandidates") or [])))
    # kpiPosition="bottom"（free 限定，2026-09-28）：KPI 条移到图表/文本区之后，
    # 打包起点不再预留 KPI 行；条的实际 y 由 build_html / render_grid_html 从
    # placed 底部现算（两处同读 manifest["kpiPosition"]）。
    _kpi_at_bottom = spec.get("kpiPosition") == "bottom"
    chart_start_y = cover_h + filter_h + (0 if _kpi_at_bottom else kpi_rows * _KPI_H)
    _pack_list = _merge_text_pseudos(spec.get("charts", []), _text_pseudo_items(spec.get("texts")))
    placed, bands = layout_sections(_pack_list, spec.get("sections") or [], start_y=chart_start_y)
    manifest = build_manifest(spec, spec.get("charts", []), placed, cover_h, None, primary_ds_id, bands, tables_by_id or None,
                              _primary_table)
    if display_pending:
        # 显示层策展（别名/值别名/隐藏/说明）在 manifest 组装后注入：这些键不是
        # 请求字段（normalize_request 内部的 validate_request 不认），但运行时
        # JS 镜像与注册 payload 都从 manifest.datasets 读。
        from curation_inherit import apply_display
        for _n in apply_display(manifest, display_pending):
            print(f"[inherit] {_n}")
    html = build_html(spec, manifest, placed, cover_h, theme, bands)
    Path(out_path).write_text(html, encoding="utf-8")
    return out_path

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Compile report spec -> draft HTML")
    ap.add_argument("--spec", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--dataset", help="canonical table JSON to embed")
    ap.add_argument("--dataset-id", default="primary")
    ap.add_argument("--dataset-label", default="明细")
    ap.add_argument("--datasets", help="JSON file with an ordered [{id,label,file}] dataset list (P1 multi-table)")
    ap.add_argument("--mode", choices=("auto", "server", "singlefile"), default="auto",
                    help="Packaging mode: auto (default by rowCount), server, or singlefile")
    ap.add_argument("--threshold", type=int, default=100000,
                    help="rowCount threshold for auto mode (default 100000)")
    ap.add_argument("--report-id", default="report", help="default report id")
    ap.add_argument("--edit-password", default=None,
                    help="server-mode online-edit password (report_build generates one when the request has none)")
    ap.add_argument("--edit-password-auto", action="store_true",
                    help="the password above was generated by report_build (recorded in runtime.editPasswordGenerated)")
    ap.add_argument("--column-stats", default=None,                    help="profile.json from profile_table: per-column stats ride into manifest.datasets[].columnStats "
                         "(the data-source UI renders them; server mode keeps no rows to recompute from)")
    ap.add_argument("--inherit", default=None,
                    help="上一份 manifest JSON：月度刷新继承 dataset 级策展"
                         "（curation_inherit.pick_prev_manifest 挑出的文件）")
    ap.add_argument("--sort-input", action="store_true",
                    help="整理模式：输入表按严格契约读取（重复表头/空白行/参差行拒绝）；"
                         "缺省直通（确定性修复 + notes，绝不因质量问题拒绝）")
    args = ap.parse_args()
    inherit_manifest = None
    if args.inherit:
        try:
            inherit_manifest = json.loads(Path(args.inherit).read_text(encoding="utf-8"))
        except (OSError, ValueError) as _exc:
            raise SystemExit(f"--inherit 读取失败（继承是增益，可去掉该参数重跑）：{_exc}")

    # Read the spec, decide mode based on rowCount, then compile.
    import json as _json
    spec = _json.loads(Path(args.spec).read_text(encoding="utf-8"))
    runtime = spec.get("runtime") or {}
    if args.mode != "auto":
        mode = args.mode
    else:
        mode = runtime.get("mode", "auto")
        if mode == "auto":
            mode = "auto"
    threshold = runtime.get("singlefileMaxRows", args.threshold)

    if mode == "auto":
        # Will resolve after we know the dataset size
        mode = None

    # Compile, then post-process manifest
    datasets_list = None
    if args.datasets:
        datasets_list = _json.loads(Path(args.datasets).read_text(encoding="utf-8"))
        if not isinstance(datasets_list, list):
            raise SystemExit("--datasets must be a JSON array of {id,label,file}")
    out = compile_spec(args.spec, args.out, args.dataset, args.dataset_id, args.dataset_label,
                       datasets=datasets_list, column_stats_path=args.column_stats,
                       inherit=inherit_manifest, report_id=args.report_id,
                       sort_input=args.sort_input)

    # Decide mode based on rowCount
    if mode is None:
        rows_count = 0
        if args.dataset:
            try:
                tab = _json.loads(Path(args.dataset).read_text(encoding="utf-8"))
                rows_count = len(tab.get("rows") or [])
            except Exception:
                pass
        mode = "server" if rows_count > threshold else "singlefile"

    # Patch the generated HTML to inject mode/serverBase and (for server mode) strip rows
    src = Path(args.out).read_text(encoding="utf-8")
    import re as _re
    m = _re.search(r'<script id="ai-report-manifest" type="application/json">(.*?)</script>', src, _re.S)
    if m:
        manifest = _json.loads(m.group(1))
        if mode == "server":
            # v1 keeps one manifest schema for both storage modes. Server mode
            # is expressed by top-level mode/serverBase and schema-only rows.
            manifest["schemaVersion"] = 3
            manifest["mode"] = mode
        if mode == "server":
            # Reports are served from the same localhost origin. Keeping this
            # path relative also makes a generated server bundle relocatable.
            manifest["serverBase"] = "/api/v1"
            # Strip rows from datasets
            for ds in manifest.get("datasets", []):
                if "rows" in ds:
                    ds["rowCount"] = len(ds["rows"])
                    del ds["rows"]
                # Convert columns to object form if currently string list.
                # Use ``columnTypes`` (mirrors the canonical table's
                # ``meta.types``) so a YYYYMM / measure column keeps its real
                # type — the previous unconditional ``"dimension"`` caused
                # measure columns to show up as dimensions in the runtime
                # filter dropdowns and broke ``dimensionKind`` axis parity.
                # Falls back to ``"dimension"`` only when types are missing
                # (legacy manifests without ``columnTypes``).
                if ds.get("columns") and isinstance(ds["columns"][0], str):
                    cols_str = ds["columns"]
                    types = ds.get("columnTypes") or {}
                    role_map = ds.get("columnRoles") or {}
                    def _col_type(name: str) -> str:
                        rt = types.get(name)
                        if rt in ("time", "measure", "category", "id", "dimension"):
                            return rt
                        rr = role_map.get(name)
                        if rr in ("time", "measure"):
                            return rr
                        if rr in ("dimension", "id"):
                            return "dimension"
                        return "dimension"
                    ds["columns"] = [{"name": c, "type": _col_type(c)} for c in cols_str]
            # Strip rows from chart editor.data
            for chart in manifest.get("charts", []):
                ed = chart.get("editor", {}).get("data", {})
                if "rows" in ed:
                    del ed["rows"]
                ed.setdefault("datasetId", args.dataset_id)
        # report_build 传入的在线编辑口令写进 manifest.runtime（server 模式**默认就有**
        # 口令，初版即可保存；值同时被 build/README.md、marker 落地页与报告管理页使用）。
        # 必须在这里注入：compile_spec 会重新从磁盘读 spec，内存里的修改到不了产物。
        if args.edit_password:
            _rt = manifest.setdefault("runtime", {})
            _rt["editPassword"] = args.edit_password
            if args.edit_password_auto:
                _rt["editPasswordGenerated"] = True
        new_payload = _json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))
        src = src[:m.start(1)] + new_payload + src[m.end(1):]
        Path(args.out).write_text(src, encoding="utf-8")

    print(f"Compiled {args.out} (mode={mode})")
