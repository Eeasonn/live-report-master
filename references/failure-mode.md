# Failure-Mode Registry

> This is a historical regression record. Chart families not listed in the v1
> whitelist are deleted product surface, even when they appear in older entries.

Every discovered failure mode is registered here with its root-cause layer, the
defense layer that catches it, the detection method, and the regression test
that must fail if it ever comes back. New failures found in the wild must follow
the feedback loop at the bottom of this file.

## Defense layers

| Layer | Name | Mechanism | Tooling |
| --- | --- | --- | --- |
| L0 | 设计层 | Make the mistake impossible (semantic types instead of hand-written formatters) | `measureTypes` in `chart-adapter.js` |
| L1 | 生成层 | Strict contract checks that block packaging | `validate_report.py` (R1–R10) |
| L2 | 质量层 | Advisory lint for style and consistency | `lint_report.py` (L1–L18) |
| L3 | 渲染层 | Browser verifies the rendered truth, not just control wiring | `audit_editor.mjs` |
| L4 | 验收层 | Human/screenshot review for visual and interaction feel | manual + screenshots |

## Feedback loop（元规则）

每次在交付后或用户侧发现新的失败模式，必须按以下顺序处理，否则不视为关闭：

1. **登记**：在本文件追加一条（symptom / root cause 层 / defense 层）。
2. **建规则**：在正确的防御层加检查（优先 L0 消除，其次 L1 硬校验，再次 L2 提示，最后 L3 运行时检测）。
3. **写回归测试**：让该问题有一条"复现即失败"的测试。
4. **同步文档**：更新 mandatory-rules / lint-rules / authoring-contract / cli。
5. **验收**：跑完整套件（单元 + e2e + audit）确认全绿。

## Registry

### FM-01 表格默认出现滚动条
- **Symptom**：15 行表格放进 `gs-h=6` 的格子，默认滚动条，首屏看不到全部内容。
- **Root cause（L1 生成层）**：作者没有把行数与容器容量对齐。
- **Defense**：L1 `validate_report.py` R2（行数 ≤ 容量表）+ L2 `lint_report.py` L8（接近容量预警）。
- **Regression test**：`tests/test_validate_rules.py`（R2 T04：15 行 / gs-h=6 必须 fail；T05：9 行必须 pass；T06：pagination 豁免）。
- **Status**：已防住。

### FM-02 区块设置面板出现重复的删除/下载按钮
- **Symptom**：每个区块的设置面板尾部有"删除区块"和"下载报告"，与区块自带删除按钮、顶栏下载按钮重复。
- **Root cause（L3 运行时 UX）**：`panel()` 无差别注入 footer 动作，未与既有入口去重。
- **Defense**：L3 运行时改造（移除 panel footer）+ audit 断言 `panelNoDuplicateActions`。
- **Regression test**：`audit_editor.mjs` 检查面板内无 `[data-runtime-action=download|delete-block]`。
- **Status**：已修复（运行时）。

### FM-03 整卡悬停出现手型光标但拖动只在角落生效
- **Symptom**：编辑模式下鼠标划过整张卡片都是手型，但实际只有右上角小按钮可拖。
- **Root cause（L3 运行时 UX）**：CSS 给整个 `[data-report-block]` 设了 `cursor:grab`，而 GridStack 的 handle 只是右上角按钮。
- **Defense**：L3 运行时改造（卡片顶边中央悬浮 ⠿ 拖拽胶囊 `.ai-report-drag-strip` + hover 提示"按住拖动卡片"）+ audit 断言 `cardNotGrab` / `dragStripGrab` / `dragStripPresent`。
- **Regression test**：`audit_editor.mjs`（`dragStripPresent` / `dragStripGrab`；旧 e2e `text_editing_e2e` 的 `.ai-report-drag-strip` 真实指针拖拽已退役 `archived/tests/`）。
- **Status**：已修复（运行时）。

### FM-04 维度/数据标签显示成 JavaScript 源码
- **Symptom**：轴标签、tooltip 渲染出 `function(v){return ...}` 文本。
- **Root cause（L0 设计 + L1 生成）**：作者把 JS 函数写成字符串塞进 `option.formatter`，ECharts 无法执行时按字面量渲染；契约禁止函数值但没有足够的具体指引，作者图省事。
- **Defense**：
  - L0：`measureTypes` 声明式格式化，作者根本不用手写 formatter；
  - L1：`validate_report.py` R1 拦截 function-string；
  - L3：`audit_editor.mjs` 渲染后检查 `getOption()` 无 function-string（防止运行时复发）。
- **Regression test**：`test_measure_types.cjs`（T1–T13）+ `audit_editor.mjs` `noBadFormatters`。
- **Status**：L0/L1/L3 三层覆盖。

### FM-05 热力图/柱状图没有按度量排序
- **Symptom**：热力图 y 轴乱序，柱状图升序或随机，头部信息被挤在角落。
- **Root cause（L1 生成层）**：没有排序约定，作者按源数据顺序直接落图。
- **Defense**：L1 `validate_report.py` R3（bar/line/pie/donut/funnel/stackedBar 强制降序，`sortOverride` 显式豁免）+ L2 `lint_report.py` L1（热力图 y 轴单调性）。
- **Regression test**：`tests/test_validate_rules.py`（R3 T07：升序 bar 必须 fail；T09：`sortOverride` 豁免）。
- **Status**：已防住。

### FM-06 文本编辑不支持基础格式
- **Symptom**：文本只读编辑、无法加粗/斜体/下划线。
- **Root cause（L3 运行时功能缺失）**：`activateText` 使用 `contenteditable=plaintext-only`，无格式能力。
- **Defense**：L3 运行时改造（浮动格式工具栏 B/I/U/清除，inline 标签保持原子边界）+ audit 断言 `textFormatBar`。
- **Regression test**：`audit_editor.mjs`（选中文本执行 bold 后 innerHTML 含 `<b>`）。
- **Status**：已修复（运行时）。

### FM-07 插入 BI 图表后新图表无法使用
- **Symptom**：点击"插入图表"后新增块渲染不出来或不可编辑。
- **Root cause（L3 运行时功能 bug）**：原 audit 只断言 `manifest.charts.length+1`，从不验证新图是否真的渲染。
- **Defense**：L3 audit 渲染真值（`insertBI` 断言 canvas 存在 + series 有数据 + 无 function-string）。
- **Regression test**：`audit_editor.mjs` `insertBI`（旧 e2e `canInsertBIComponent` 已随 `bi_controls_e2e` 退役 `archived/tests/`）。
- **Status**：已修复（检测层）。

### FM-08 卡片拖拽从未生效（vendored GridStack 缺拖拽插件）
- **Symptom**：编辑模式下整卡悬停是手型光标，但实际拖动不动；格子不跟随。
- **Root cause（L3 运行时基础设施）**：`assets/vendor/gridstack.all.js` 是**不含 DD（拖拽）插件的核心版**（81KB，`GridStack.DD` 为 undefined），任何拖拽都无效。此问题从 skill 诞生起就存在，旧 audit 从不测试"拖真的能动"，所以一直潜伏。
- **Defense**：
  - 更换为含 DD 插件的 `gridstack-all.js`（v11.1.2，83KB）；
  - 运行时对 v11 的鼠标拖拽缺陷做两层补偿：① 拖拽期间把顶部拖动条从命中测试隐藏（pointer-events:none），避免释放落在拖动条上导致还原；② mouseup 时按"按下到释放的像素位移"换算新格子并手动提交（node + gs-* 属性 + 视觉位置 + endUpdate），因为 v11 内部 dropover 在鼠标从 grid 内部开始拖拽时不会触发，placeholder 不跟随；
  - audit 断言 `dragStripPresent` / `cardNotGrab` / `dragStripGrab`，e2e 真实拖拽 `dragStripMovesBlock`。
- **Regression test**：旧 e2e `dragStripMovesBlock`（真实指针拖拽后 gs-x 变化；脚本已退役 `archived/tests/`，现行锚点 = audit `manualGapInvariant` / `dragLostGuard`）。
- **Status**：已修复（运行时）。

### FM-09 文本编辑无格式工具栏
- **Symptom**：文本只能纯文本编辑，无法加粗/斜体/下划线。
- **Root cause（L3 运行时功能缺失）**：`activateText` 使用 `contenteditable=plaintext-only`。
- **Defense**：切换为完整 `contenteditable` + 浮动格式工具栏（B/I/U/清除，inline 标签保持原子边界，不用 formatBlock 避免替换容器破坏 `data-editable-text`）。
- **Regression test**：`audit_editor.mjs` `textFormatBar`（选中文本执行 bold 后 innerHTML 含粗体标记）。
- **Status**：已修复（运行时）。

### FM-10 文本/封面区块内容超出网格高度
- **Symptom**：封面摘要等文本块在较窄视口下换行变高，出现滚动条或内容被裁剪。
- **Root cause（L3 渲染层盲区）**：R2 只约束表格行数，没有任何机制检测文本块内容是否适配网格高度（宽度相关，静态无法精确预测）。
- **Defense**：L3 `audit_editor.mjs` 新增 `noBlockOverflow`——在 1280px 视口下扫描所有非运行时、非 table-wrap 元素，`scrollHeight > clientHeight + 3` 即失败。
- **Regression test**：`audit_editor.mjs` `noBlockOverflow`（封面 gs-h=3 长摘要必须被标记，gs-h=4 通过）。
- **Status**：已防住（检测层）。

### FM-11 散点图数据无区分度，点全部重叠
- **Symptom**：散点图 X/Y 轴取值几乎相同（如各渠道花费 780-784 万），散点挤成一竖线，图读起来是空的。
- **Root cause（L2 生成层）**：作者选了无区分度的维度对（对比类场景点太少或某轴无变化）。
- **Defense**：L2 `lint_report.py` L15——scatter/effectScatter 检查两轴数据跨度 `(max-min)/max`，任一轴 < 10% 即警告，建议换维度或改条形图。
- **Regression test**：lint 对 6 渠道花费散点必须告警；对 365 天日花费散点（跨度 39%）通过。
- **Status**：已防住（检测层）。

### FM-12 热力图数值范围过窄，色带无法区分
- **Symptom**：ROAS 2.6-3.45（跨度 24%）配顺序色带，各格子颜色几乎相同，读不出差异。
- **Root cause（L2 生成层）**：编码的颜色变量没有匹配数据的区分度——窄范围 + 全幅顺序色带。
- **Defense**：L2 `lint_report.py` L16——热力图数据跨度 < 30% 且未使用发散色带（`visualMap.middle`）时警告，建议围绕基准（如整体均值）发散着色 + 显示格子数值。
- **Regression test**：lint 对 2.6-3.45 顺序色带热力图必须告警；对 middle=3.0 发散色带通过。
- **Status**：已防住（检测层）。

### FM-13 热力图/角色图在原始视图被单维聚合拍平
- **Symptom**：渠道×受众热力图只渲染 6 格（应为 30 格），其余格子空白——颜色几乎看不出差异的深层原因。
- **Root cause（L3 运行时 bug）**：`originalOption` 用 `aggregate(dimension, measures)` 重建 series 数据，把 roles.breakdown/source/target 的第二维拍平；heatmap/themeRiver/graph/sankey 全部受影响。
- **Defense**：L3 `chart-adapter.js`——带 roles 的图表在原始视图保留作者矩阵数据（只按选中度量过滤 series，不重映射数据）；L3 `audit_editor.mjs` 新增 `heatmapComplete`——断言渲染的格子数 = 轴交叉数。
- **Regression test**：`test_measure_types.cjs` T14（heatmap 原始视图保持 30 格）+ `audit_editor.mjs` `heatmapComplete`。
- **Status**：已修复（运行时）。

### FM-14 热力图 cells 索引转置（[row,col,v] 与轴不匹配）
- **Symptom**：4/10 子代理报告的热力图格子错位——cells 写 `[行维度, 列维度, v]` 但 xAxis=列、yAxis=行，视觉上格子乱序/覆盖错位。
- **Root cause（L2 生成层 + 文档盲区）**：作者不熟悉 ECharts 热力图 `[xIdx, yIdx, value]` 约定；L1 只能靠"行总和碰巧不单调"间接捕获。
- **Defense**：L3 `audit_editor.mjs` `heatmapComplete` 升级为**转置守卫**——断言每个 cell 的 `xIdx < xLen && yIdx < yLen`，直接捕获索引越界，不再依赖排序巧合。
- **Regression test**：`audit_editor.mjs` 对转置热力图必须失败（构造 xLen=3,yLen=7 但 cell yi≥3 的用例）。
- **Status**：已防住（检测层）。

### FM-15 符号前缀分类被误判为数值度量
- **Symptom**：排名变化列（"+5"/"-4"）被 `numericColumns` 判为数值列 → 编辑面板出现 2 个度量复选框，维度列表丢失该列，度量切换失控（audit semanticControls 失败）。
- **Root cause（L3 运行时）**：`Number("+5")` 返回 5，字符串数值判定过宽松。
- **Defense**：L3 `chart-adapter.js` `numericColumns` 拒绝带 `'+'` 前缀的字符串（真实数值数据不会有正号前缀，那是显示格式化产物）。
- **Regression test**：`test_measure_types.cjs` 新增（'+5' 列应归入维度而非度量）。
- **Status**：已修复（运行时）。

### FM-16 数据口径错误导致不可能数值（留存>新装）
- **Symptom**：移动 App 留存漏斗"次日留存 2415 > 新装 1625"——聚合把全表的留存标志求和，而非只对新装子集。
- **Root cause（数据层）**：聚合范围（全表 vs 子集）错误；漏斗单调性校验（R3）恰好暴露。
- **Defense**：R3 的"非递增"校验是第一道防线；建议 funnel 增加"严格递减"语义断言。生成 prompt 前置"漏斗类指标必须按正确子集口径计算"。
- **Regression test**：`tests/test_validate_rules.py`（R3 T10：递增 funnel 硬报错 ReportContractError；T11：相邻等值的非递增放行——与实现一致，非"告警"）。
- **Status**：已防住（检测层 + 口径提示）。

### FM-17 报告数字与源数据不一致（无数据真值校验）
- **Symptom**：报告展示的聚合数字与源表重算不符（口径错、列错、子集错），且 validate/lint/audit 全部通过——形式校验查不出内容错误（实例：留存>新装、激活率>100%）。
- **Root cause（系统层）**：校验链只验证"形式正确"（契约/排序/溢出/渲染），不验证"数字正确"。AI 生成报告的最大风险（幻觉/错算）无防线。
- **Defense**：L3 `audit_editor.mjs` 新增 `dataTruth`——表格图表可通过 `truth` 声明声明"报告列 ← 数据源列 + 聚合方式"，audit 重新聚合源数据并与渲染值比对（相对容差 1e-6）。未声明的列跳过。
- **Regression test**：audit `dataTruth`（`chart:${blockId}:dataTruth`）对篡改表格值的报告必须失败（构造 0.9× 销售额用例）；对完整报告通过。
- **Status**：已防住（检测层）。

### FM-18 对不可加总/不可堆叠度量做 sum/堆叠（语义行为规则缺失）
- **Symptom**：比率/百分比度量被 sum 聚合或放入堆叠柱状图——sum 一组比率无意义，堆叠非可加度量扭曲总量（借鉴自 Flint T1 语义层的"聚合角色/堆叠合法性"）。
- **Root cause（系统层）**：measureTypes 只表达了"显示格式"，没有表达"哪些操作在语义上非法"。
- **Defense**：L2 `lint_report.py` L17——percent/rate 度量若用 sum 聚合或放入 stackedBar 即警告（建议 average 或分组柱）；spec_to_report 编译期按 `validate_chart_shape` 拒绝形状不匹配（funnel 多度量、heatmap 缺 breakdown、scatter 单度量）。
- **Regression test**：lint L17（percent+sum 必须告警，count+sum 通过）；`validate_chart_shape`（heatmap 缺 breakdown 必须抛错）。
- **Status**：已防住（检测层 + 编译期）。

### FM-19 纯数字字符串维度列被误判为数值度量
- **Symptom**：年份/序号等纯数字字符串维度（如 `"2019"`..`"2025"`）被 `numericColumns` 判为数值列 → 编辑面板「数值指标」多出该列、且只剩 1 个勾选度量时 audit `semanticControls` 失败（零度量回勾依赖 `document.activeElement`，合成事件不聚焦导致不回勾）。与 FM-15（`+5` 前缀）同源但无前缀。
- **Root cause（L3 运行时 + 作者侧）**：`Number("2019")` 为有限数，字符串数值判定过宽；维度列值恰为纯数字时被当作度量。
- **Defense**：作者侧将维度值改为非纯数字标签（`"2019"` → `"2019年"`）；`test_measure_types.cjs` T16 无（改用维度标签）。skill 侧（**未实施，待立项**，尚无 L 编号）：lint 对「dimension 列值全为纯数字字符串」告警。
- **Regression test**：`audit_editor.mjs` `semanticControls`（1 度量 + 2 可选项时不再误报）。已通过改用非纯数字标签规避。
- **Status**：已修复（作者侧规避）。

### FM-20 热力图切到单维度图表时维度值塌缩（矩阵单元泄漏）
- **Symptom**：带 `roles.breakdown` 的热力图切换为柱/线/饼等单维度图后，x 轴出现全部重复的原始单元（如 56 个「部门」重复或「工龄段」全为 0~12月），而非按维度聚合。
- **Root cause（L3 运行时）**：`aggregation:'none'` 对矩阵数据按单维度重聚合时返回原始单元（`method==='none'` 不做分组）。矩阵→1-D 转换需要重新聚合。
- **Defense**：L3 `chart-adapter.js` 新增 `isMatrix`/`matrixAgg`——矩阵数据切换到单维度图时把 `'none'` 强制为 `'average'`（比率语义安全）按主维度重聚合；不再泄漏单元。
- **Regression test**：`test_measure_types.cjs` T16（矩阵→bar 只产生每维度 1 项，非原始 6 单元）。
- **Status**：已修复（运行时）。

### FM-21 文本格式条点击字形不响应（event.target 误读）
- **Symptom**：加粗/斜体/下划线浮条时灵时不灵；快捷键正常。点按钮文本（`<b>B</b>` 字形）时无反应，点按钮留白时才有反应。
- **Root cause（L3 运行时）**：格式条 click 读 `event.target.getAttribute('data-format-action')`，点到内层 `<b>/<i>/<u>` 字形时 target 无该属性 → action 为空直接 return。
- **Defense**：L3 `editor-runtime.js` 改用 `event.target.closest('[data-format-action]')`；新增红/蓝/高亮/字号按钮与快捷键（⌘⇧1/2/3），并配套 R10 风格白名单校验。
- **Regression test**：`audit_editor.mjs` `textFormatBar`（真实点击命中字形仍应用内联格式；旧 e2e `text_editing_e2e` 已退役 `archived/tests/`）。
- **Status**：已修复（运行时）。

### FM-22 originalOption 把构成/层级系列数据拍平成数值数组（ECharts null-name）
- **Symptom**：author 的 pie/donut/funnel 报告在浏览器加载时 runtime 中断（`AIReportRuntime` 未定义、canvasCount=0），控制台报 `TypeError: Cannot read properties of null (reading 'name')`（ECharts `_updateData`）。manual `optionFor` 却正常。差异在于 runtime 把 `originalViewType` 设为 chart.viewType，使该类图走 `originalOption` 而非 `shareOption`；`originalOption` 用 `entries.map(e=>e.values[i])` 把系列数据拍平成纯数字，丢失 `{name,value}`，ECharts 读取 name 时崩。
- **Root cause（L3 运行时）**：`originalOption` 的无条件数据拍平未区分"分类轴系列(bar/line)"与"构成/层级系列(pie/funnel/treemap)"。
- **Defense**：L3 `chart-adapter.js` `originalOption`——只有存在 `categoryAxis` 且非构成/层级系列时才把 `item.data` 拍平；pie/funnel/sunburst/treemap/tree/gauge 保留 authored `{name,value}`。`capPieSlices` 在 `patchSemantics` 中对对象切片合并"其他"，避免对数字数组误操作。
- **Regression test**：`test_measure_types.cjs` T24（original pie 保留 name-value 对象、无 null name）。
- **Status**：已修复（运行时）。

### FM-23 类目截断按值重排时间维（破坏趋势）
- **Symptom**：高基数时间维（如 84 个月）柱/线图被 `capEntries` / `_filter_cardinality` 按度量值排序并截断，时间轴变成按值重排（如 2021-07 排在 2019 之前），趋势被毁。
- **Root cause（L3 运行时 + 编译期）**：类目基数守护"top-N by value"对时间维不适用——时间必须保序。
- **Defense**：L3 `capEntries`/`optionFor` 对 `dimensionKind==='time'` 的时间维**跳过按值截断**（交给 dataZoom 导航）；编译期 `_filter_cardinality` 对时间维不截断、`_dimension_type` 先判值日期再判名词（"入职月份"→time 而非 ordinal）。
- **Regression test**：`test_measure_types.cjs`（capEntries 对 time 维不重排）；随机报表 c3 线图按月份升序。
- **Status**：已修复（运行时 + 编译期）。

### FM-24 viewType:pie 渲染空白（radius 未强制默认）
- **Symptom**：饼图（`viewType: "pie"`）在浏览器加载后 canvas 存在且 `chart.getOption()` 返回完整 `{type:"pie", data:[4项], radius:null}`，但 `canvas.toDataURL().length ≈ 10K`（仅图例文字），整个饼形区域像素全为透明 [0,0,0,0]；同一页的 `viewType: "donut"` 渲染正常。
- **Root cause（L3 运行时）**：`chart-adapter.js` `shareOption` 对 pie（非 donut）显式 `radius: undefined`，写入 ECharts 后变成 `radius: null`；ECharts 对 null radius 走默认 fallback，但当 option 链路经过 `clearCoordinates` + `applyMeasureFormats` 重构后，半径无法被正确继承，导致饼形几何尺寸塌缩为 0。
- **Defense**：L3 `chart-adapter.js` `shareOption`——`pie` 强制 `radius: ['0%','70%']`（实心圆盘），`donut` 保持 `['45%','72%']`；不允许 pie 类型把 radius 留空。
- **Regression test**：`audit_editor.mjs` 的 `chart:${blockId}:paintedContent`（pie/donut/funnel 采样画布像素，0 像素即失败）+ 旧 e2e `chartType:pie:painted`（脚本已退役 `archived/tests/`）。
- **Status**：已修复（运行时）。

### FM-25 shadowed `var measures` 吞掉数值指标 fieldset
- **Symptom**：`fieldControls` 返回值里 数值指标 fieldset 和 与维度互换 按钮都消失；面板上仅剩"分类维度"和"相同类别的处理"。作者后续如果加新字段会奇怪为什么整段 HTML 被吞。
- **Root cause（L3 运行时）**：在 `editor-runtime.js fieldControls` 函数内部，第二段 `var measures=data.measures||[]`（检测"全是 count 列"用的局部变量）覆盖了第一段同名变量（值是"数值指标 fieldset 字符串"）。`return dimension+measures+aggregation` 把数组转字符串时只产出 `,` 分隔的列名，没有 fieldset。
- **Defense**：L3 修复——把第二处 `var measures` 重命名为 `var measureList`；audit 的 `controls`/`semanticControls` 检查会在面板打开时查 `[data-measure]`，shadowed 场景会失败。
- **Regression test**：audit `chart:${blockId}:controls`（`document.querySelectorAll('[data-measure]').length > 0` 期望 true）。该检查会在面板打开后立即触发，已自动覆盖此场景。
- **Status**：已修复（运行时 + 检测层）。

### FM-26 themeRiver 缺 singleAxis 导致 ECharts 崩溃
- **Symptom**：BI 图表切换到「主题河流图」时，浏览器控制台报 `TypeError: Cannot read properties of undefined (reading 'get')`（ECharts `getInitialData`），图表完全不渲染；其余 20 种图表类型正常。
- **Root cause（L3 运行时）**：`chart-adapter.js specializedOption` 的 themeRiver 分支只构造 `series`（data 为 `[date, value, seriesName]`），但 themeRiver 必须配一个 `type:'time'` 的 `singleAxis` 时间轴。缺了这个组件，ECharts 在初始化系列数据时访问未定义的坐标系对象。
- **Defense**：L3 修复——themeRiver 分支补上 `option.singleAxis={type:'time',boundaryGap:false}`，并把数据按日期升序排序。
- **Regression test**：旧 e2e `chartType:themeRiver:painted` 断言（脚本已退役 `archived/tests/`）（此前会因渲染崩溃失败）。
- **Status**：已修复（运行时 + 检测层）。

### FM-27 matrixEntries 未聚合，热力图/河流图重复单元
- **Symptom**：BI 自选「维度 × 副维度」的热力图/主题河流图，matrixEntries 返回原始行（5000 行），同一 `(维度, 副维度)` 交叉点被重复写入多个 cell，视觉上格子互相覆盖、数值错乱。
- **Root cause（L3 运行时）**：`matrixEntries` 只做 `rows.map(...)` 不聚合；heatmap 分支用 `x.indexOf(item.dimension)` 生成 cell 索引，重复单元叠在同一 `[x,y]` 上。
- **Defense**：L3 修复——`matrixEntries` 改为按 `(dimension, breakdown)` 分组并求和第一个度量（Map 聚合），保证每个轴交叉点只有一个 cell。
- **Regression test**：旧 e2e `chartType:heatmap:painted` / `chartType:themeRiver:painted`（脚本已退役 `archived/tests/`）；audit 既有 `heatmapComplete`（cells.length === xs*ys）对作者侧热力图仍然生效。
- **Status**：已修复（运行时 + 检测层）。

### FM-28 迁移把聚合维度清空导致 gauge 总值为 0
- **Symptom**：月度双轴折线图切换到「仪表盘」后，仪表盘显示 `value:0`（应为 ¥48.35亿）。其余类型正常或显示为摘要。
- **Root cause（L3 运行时）**：`updateChart` 的硬规则迁移对 `dims.max===0` 的类型（gauge/scatter/parallel）执行 `dims.slice(0,0)` 清空维度，`selection.dimension=dims[0]=undefined`。`aggregate()` 必须按维度分组，dimension 为 undefined 时 `columns.indexOf(undefined)=-1` 返回 `[]`，仪表盘 total=0。
- **Defense**：L3 修复——迁移对 `dims.max===0` 类型保留 `data.dimension`（聚合分组键），仅把 `data.dimensions` 显示列表清空。**聚合维度永不因形状规则被清空。**
- **Regression test**：audit `chart:${blockId}:paintedContent`（0 渲染像素即失败）+ 旧 e2e `bug:gauge:nonZeroTotal`（脚本已退役 `archived/tests/`）。
- **Status**：已修复（运行时）。

### FM-29 AI推荐重置按钮点击内层文字无效
- **Status**：**功能已退场**（2026-09 用户决策：「AI 推荐」易被误认为 Agent 产品，横幅/重置/推荐徽章整体删除，`closest('[data-bi-control-action]')` 的 action 解析保留）。
- **Symptom**：点击「重置为 AI 推荐」按钮的图标/文字（`<span>` 字形）时无反应；点按钮留白才有效。
- **Root cause（L3 运行时）**：面板 click 处理器用 `event.target.getAttribute('data-bi-control-action')`，点到内层 `<span class="ai-report-ai-banner-label">` 时 target 无该属性 → action 为空直接 return。与 FM-21（格式条字形）同源。
- **Defense**：L3 修复——改用 `event.target.closest('[data-bi-control-action]')` 解析 action（覆盖 swap / chip-agg 等）。
- **Regression test**：功能已删，无存活回归点；`closest` 解析由 audit `chart:${blockId}:controls` 的点击类交互检查覆盖。

### FM-30 作者图表内嵌预聚合摘要导致重聚合无意义（产品边界收敛）
- **Symptom**：作者生成的图表（如月度走势）把预聚合的 12 行摘要放进 `editor.data`；切到数据表格后，切换 sum/avg/max/min 数字不变（每组只 1 条记录，sum=avg=max=min）。
- **Root cause（L0 产品边界 + 编译期）**：`spec_to_report.py` 曾把「图表尺寸的预聚合 rows」写入 `editor.data`。对已聚合数据再做聚合是数学空操作，用户误以为聚合功能坏了。
- **Defense**：L0/L1 边界——作者图表默认引用**原始数据集**（`editor.data` = 原始 columns/rows），仅当 dimension/measures 映射到原始列且 aggregation 为 sum/avg/count/min/max 时。维度聚合收敛为「时间分桶（month/week/day/year）」一种能力（`dimensionBucket`）；派生列图（stacked 状态列）、明细/TopN（aggregation=none）保持预聚合（属固定构图，不在边界内）。
- **Regression test**：audit `chart:${blockId}:dataTruth`（口径错误会被真值校验捕获）+ 旧 e2e `bucket:monthSumDiffersFromAvg`（脚本已退役 `archived/tests/`）（sum 与 avg 必须不同）。
- **Status**：已修复（编译期 + 运行时）。

### FM-31 拖拽改变主维度后残留旧的时间分桶声明
- **Symptom**：把时间维度（如「快照日期」）拖出分类维度区、改为拖入非时间维度（如「产品线」）后，图表仍显示时间，而不是新维度。
- **Root cause（L0 运行时）**：拖拽走 `writeZoneState`（而非 `updateChart`），它更新了 `data.dimension` 却没同步 `dimensionBucket`。`aggregate()` 用 `dimensionBucket.source` 作为分组键，旧桶声明仍指向「快照日期」，于是仍按日期分桶。
- **Defense（L0/L1）**：`writeZoneState` 在主维度改变时重新同步 `dimensionBucket`——新维度为时间类型才保留（更新 `source`），否则删除。拖拽后图表按新维度分组。
- **Regression test**：`test_editor_iteration.cjs`「ZONES: 主维度变更重同步 dimensionBucket」（writeZoneState 重同步分支源级断言；旧 e2e `bug:dimensionDrag:clearsBucket` 已退役 `archived/tests/`）。
- **Status**：已修复。

### FM-32 空字符串 barGap/barCategoryGap 导致柱状图空白
- **Symptom**：柱状图/堆叠柱状图渲染成空白（坐标轴、图例、x 轴标签都在，但没有任何柱子）；折线等其他类型正常。
- **Root cause（L1 编译器）**：图表设置面板的「柱间距/类目间距」默认值是空字符串 `""`。`categoryOption` 里 `if(barOpts.barGap!=null)` 判断把 `""`（falsey 但 `!=null` 为 true）也写进了 `series[].barGap`/`barCategoryGap`。ECharts 期望数字或 '20%' 这类值，空字符串破坏 bar 布局计算，柱子高度塌缩到接近 0 → 视觉空白。
- **Defense（L1）**：`categoryOption` 只在 `barGap!==''` 且 `barCategoryGap!==''` 时才注入这两个字段（空值不写）。同时 cartesian 柱/线类默认关闭入场动画，避免 grow-up 动画在某些环境停帧导致柱子停在 ~0 高度。
- **Regression test**：旧 e2e `bug:bar:noEmptyGap`（脚本已退役 `archived/tests/`）（series 的 barGap/barCategoryGap 不得为空字符串，且需有数据）+ audit `chart:${blockId}:painted`。
- **Status**：已修复。

### FM-33 柱状图入场动画停帧导致柱子停在 0 高度
- **Symptom**：渲染后柱子只见非常矮的一截（或空白），但数据/ECharts 坐标映射正确。
- **Root cause（L1）**：柱/线 grow-up 入场动画在 headless / 低功耗 / 后台标签页环境下 requestAnimationFrame 不推进，动画永远停在初始帧，柱子停在 ~0 高度。之前 `animation` 默认开启。
- **Defense（L1）**：cartesian 柱/线类 `animation` 默认关闭（除非作者显式 `chart.editor.animation=true`），图表立即以完整高度渲染——报表图不需要入场动画，即时显示更大。
- **Regression test**：audit `chart:${blockId}:painted`；旧 e2e `chartType:bar:painted`（脚本已退役 `archived/tests/`）。
- **Status**：已修复。

### FM-34 精简显示设置 & 重做极值点（本轮 UI 收敛）
- **删除**：坐标轴对数切换（logScale）、类目上限（maxCategories）、重点区间（markArea）、轴指示器（axisPointer）、调色板/系列色板（colorPalette）、饼/环形形状（pieOptions roseType/angle）、柱密度（barOptions）、漏斗形状（funnelOptions）、仪表盘类型（gauge）。
- **重做极值点（markPoint）**：旧实现圆点过大(label symbolSize 48)、数值与类型挤在一起难辨认、全部选中反而看不到内容。新实现：柱顶点用彩色圆点（最大=红 #dc2626 / 最小=蓝 #2563eb / 均值=绿 #10b981），数值在圆点上方、类型小字（最大值/最小值/均值）在下；未勾选「显示数据标签」时仅标注极值点（不逐柱标注）。
- **新增**：用户指定排序（`chart.editor.sort={field,dir}`）。时间维度默认按名称/日期升序；其他图表默认按第一个指标降序；可在显示设置里改字段+方向。适用 cartesian/share/hierarchy。
- **背景色改预选**：透明/白/浅灰/浅蓝/浅黄 5 个预设（原自由 hex 输入）。
- **修复**：表格隔行底色 `ai-report-table-striped` 之前无匹配 CSS 规则而失效；现已补上 `tr:nth-child(even)` 背景。
- **修复**：`patchFlags` 曾用 `clone()`（JSON 序列化）导致 series label 的 formatter 函数丢失（funnel/pie 维度+值标签失效）；已改为顶层浅拷贝。
- **测试**：旧 e2e（已退役 `archived/tests/`）曾移除已删功能的检查，新增 `feature:sort`、`viewOpt:funnel:labelHasValue`、`viewOpt:sunburst:isNested`；`test_measure_types.cjs` 移除 gauge/T20 log 检查。
- **Status**：已落地（UI 收敛变更记录，非 failure mode）。

### FM-35 markPoint 勾选反向 + 参考线/极值点合并（本轮 8 项）
- **markPoint 勾选反向**：`applyMarkPoint` 曾用 `cfg.max!==false`（undefined 也被视为启用），而 checkbox 用同一判断默认勾选，但 `chart.editor.markPoint` 初始未设置 → 显示与勾选不一致（取消反而显示）。修复：checkbox 与 applyMarkPoint 统一用明确的 `===true`，初始 markPoint 未勾选。
- **均值改为参考线 + 参考线合并**：均值不再用 markPoint 圆点（平均值是横线级别，非数据点），改为 markLine 参考线（`type:'average'`）；最大/最小仍用彩色圆点（红/蓝）标注数值+类型。参考线只在有 value 轴的图（bar/line/area/pictorial/stacked）显示，饼/漏斗等不显示。
- **旭日图两层**：`updateChart` 之前只对 heatmap/themeRiver 设置 `roles.breakdown`，sunburst 拖入第 2 维度不生效。已让 sunburst 也使用 dims[1] 作为 breakdown（产品线→仓库→金额两级）。
- **表格小数位**：tableControls 新增 `ai-table-decimals`（公共小数位设置），showTable 应用 divisor+decimals。修复 `ai-report-decimals` id 冲突（display vs table 重复）。
- **文本编辑**：formatBar 新增黑色按钮（`ai-text-default` #1f2937，⌘⇧0）；快捷键提示 title 用 `\n` 换行提升可读性；document 捕获阶段 click 关闭 format bar（点空白退出）。
- **只读下载**：只读 HTML 曾保留 editor-runtime init 脚本，加载时重建 runtime-ui（编辑按钮）。修复：buildReadonlyHtml 给 body 加 `data-ai-report-readonly`，editor-runtime init 检测后跳过初始化。
- **Regression test**：`test_measure_types.cjs`（markPoint/参考线相关 T 序列）+ 旧 e2e `feature:markPoint:applied`、`viewOpt:sunburst:isNested`（脚本已退役 `archived/tests/`）；audit `dataTruth`、`paintedContent`。
- **Status**：已落地（UI 收敛变更记录，非 failure mode）。

### FM-36 卡片拖拽后其它卡片拖不动 / 落点漂移（改用原生 GridStack）
- **Symptom**：把一张卡片拖到某位置后，其它卡片再也拖不动；且鼠标 hover / 落点位置与实际不符。
- **Root cause（L1 编辑器）**：进入编辑时 `grid.enableMove(true)` 启用 GridStack 原生拖拽（`handle:.ai-report-drag-strip`）。但旧的 `bindDragStripGuard` 自定义 guard 在 mouseup 里**再次手动 `grid.update` + 手动改 DOM gs-x/gs-y + `engine.endUpdate`**。原生拖拽与手动更新**双写冲突**，导致 GridStack engine 的 node 状态与 DOM 不同步 → 其它卡片读到错乱状态拖不动、落点漂移。
- **Defense（L1）**：改用 GridStack 原生拖拽全权负责移动与 engine 更新。`bindDragStripGuard` 只保留两件事：mousemove 时 dispatch `mouseenter` 让 drop placeholder 跟随、mouseup 时恢复 pointer-events 并 `syncLayouts()`。移除所有手动 move/位置计算。
- **Regression test**：audit `dragStripPresent` / `dragStripGrab` / `cardNotGrab` / `manualGapInvariant`、`dragLostGuard`（拖拽条契约与拖动不丢守护）；真实拖拽行为仍需真实浏览器人工验证（headless 无法触发原生 GridStack 拖拽；旧 e2e 回归脚本已退役 `archived/tests/`）。
- **Status**：已修复（需真实浏览器验证）。

### FM-37 只读下载 body 标记丢失致编辑按钮仍出现；非坐标轴图仍显示参考线
- **只读 body 标记丢失**：`buildReadonlyHtml` 里 `document.documentElement.cloneNode(true)` 后，克隆文档的 `doc.body` 为 null（body 在 documentElement 下，`body` 属性在克隆树上不可靠），导致 `if(doc.body)` 为 false，`data-ai-report-readonly` 标记**静默没写入** → 加载时 init guard 不拦截 → 编辑器重建编辑按钮。修复：改用 `doc.querySelector('body')`。
- **非坐标轴图显示参考线/极值点设置**：`displayControls` 无条件渲染参考线/极值点 fieldset，饼图/漏斗等非坐标轴图也显示。修复：仅 `bar/stackedBar/line/area/pictorialBar` 显示。
- **旭日图默认只有单层**：`writeZoneState` 拖入第 2 维度时未给 sunburst 设置 `roles.breakdown`，导致层次结构需点「显示数据标签」才渲染。修复：sunburst/heatmap/themeRiver/treemap 在 `dimensions.length>1` 时设置 `roles.breakdown=dims[1]`。
- **Regression test**：`test_measure_types.cjs` + audit `paintedContent`、`dataTruth`；旧 e2e `chartType:sunburst:painted`、`rule:bar:1to2`（脚本已退役 `archived/tests/`）。
- **Status**：已修复。

### FM-38 只读报告布局错乱（整段拦截 runtime 导致 GridStack/ECharts 未初始化）
- **Symptom**：下载的只读报告布局全乱（卡片不定位、蓝色竖条、表格表头丢失）。
- **Root cause（L1）**：早期的 `data-ai-report-readonly` guard 用了 **整段 `return`**，把整个 editor-runtime（含 GridStack 布局 + ECharts 渲染）都拦截了。只读版卡片带 `gs-*` 属性但 GridStack 未初始化 → 无 left/top 定位 → 卡片堆叠/错位；图表也未渲染（蓝色竖条）。
- **Defense（L1）**：把 guard 从「整段 return」改为「只读标记时跳过**编辑向**部分」（注入 runtime-ui、drag-strip、文本编辑、事件绑定），但**保留** `makeGrid()`（GridStack 布局）+ `renderAll()`（ECharts 渲染）。即只读版正常布局+渲染，只是无编辑入口。
- **Regression test**：`test_measure_types.cjs` + 旧 e2e（脚本已退役 `archived/tests/`）；readonly 布局需真实浏览器人工验证。
- **Status**：已修复。
