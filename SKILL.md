---
name: live-report-master
description: Windows-first skill for turning a clean first-row-header Excel/CSV/JSON table into a deterministic, editable BI report. The Agent writes a constrained report_request.json; scripts handle profiling, validation, aggregation, fixed-template rendering, packaging, and optional localhost server delivery.
---

# Live Report Master

把一张二维表（Excel/CSV/JSON）变成一份**可编辑的 BI 报告**。模型只负责「看懂数据、做分析选择」，其余（布局、ECharts、SQL、打包、审计）全部由固定脚本完成。

## 模型负责什么

1. 阅读 `profile`，确定分析主题。
2. 标记列角色（`time`/`dimension`/`measure`/`id`/`ignore`）和数值语义（`measureTypes` 金额/比例/计数）。未标记的列由脚本按 profile 自动推断；`id`/`ignore` 列（订单号、SKU 等实体键）不会被当作维度。
3. 从白名单选 1–6 个图表：维度、指标、聚合方式、排序、TOP K。**先想这句分析要回答什么，再映射到 family**：
   - **排名/对比** → `bar.rank`（单维度 + 1-3 指标）；占比构成 → `bar.stacked`（加 `breakdown` 看第二维，也为「A×B 计数」的交叉表）。
   - **趋势/随时间变化** → `line.trend` / `area.trend`（需要时间或有序维度）。
   - **整体占比** → `pie.share` / `donut.share`（切片 ≤8）。
   - **阶段流转** → `funnel.stage`（≥3 有序阶段）。
   - **两个指标的关联** → `scatter.correlation`。
   - **聚合/详情证据** → `table.aggregate` / `table.detail`。
   同表别产出 3 个以上同 family 的图；数据支持（多指标→关系、时间维→趋势、多维度→交叉表）时换 family，否则报告沦为一排单调柱状图。脚本会给密集同 family 的报告打 advisory 提示。
4. 写 KPI（最多 4 个）、最多 3 条洞察、筛选条件。KPI 文字必须基于 profile 或脚本算出的 facts；模型可直接提供算好的 KPI 数值（脚本不再覆盖）。KPI 键兼容别名：`factType→type`、`column→measure`、`title→label`、`format→measureType`（`basis` 已废弃），写错旧键也能算出真实值而非「—」。**去重计数类 KPI**（覆盖国家数、活跃门店数、去重客户数）用脚本计算的口径：`{"type": "kpi.metric", "factType": "distinctCount", "dimension": "列"}`（`count` 恒为行数 COUNT(*)，不是去重数）。**不要硬编码 `value`**——静态 value 在用户筛选后不会重算，会与被筛选的图表矛盾；仅无筛选的报告才可直接给 value（声明了 filters 时 validate 会打 advisory）。

模型不写 HTML/CSS、GridStack 坐标、ECharts option、SQL 或 Windows shell。固定模板 `standard-bi-v1` 负责：封面 → 筛选 → KPI → 图表 → 洞察。明细表是编辑模式下的按需组件（支持搜索/排序）。

`profile_table.py` 是确定性事实层（类型/基数/缺失率/数值统计/Top 值）；模型推荐是语义层（主题、解释、图表选择、总结）。二者不混，模型不得改写脚本事实。

## 输入与清洗

支持 `.xlsx`（默认第一个 sheet）、`.csv`/`.tsv`、规范 JSON `{"columns":[...],"rows":[...]}`。首行是唯一非空表头，数据是致密矩形。值级错误（`#N/A`、`#VALUE!`、空串等）静默转 `null` 并写入质量元数据；结构错误（缺表头、行宽不一致、不可读）直接失败。建议用户先给清洗后的数据；不要为“修数据”猜测业务含义。

**输入契约与口径纪律**（全文 `docs/input-contract.md`）：输入必须是严格二维表——首行唯一表头、无合并/嵌套表头、行列致密、一行一条记录；合并表头、内嵌合计行、千分位文本、日期序列号、前导零编码等常见坑，构建时会自动体检并报告（2026-09-18 起**缺省直通**：只出报告不拦截，读取做最小确定性修复并记 notes；显式带 `--sort` 才进入整理模式——结构破坏拦截 + 严格读取）。行级加工让渡给 Excel 主工作簿，判别标准 = 该值是否只由这一行自己决定（占比/同环比/排名/累计/合计/去重计数这类随查询维度变化的值留给产品，别在 Excel 算死）；推荐工作流 = 原始数据表用 Ctrl+T 表格对象挂公式加工列自动下填 → 每月贴新原始行 → 导出「粘贴值」的二维表 → 全量替换或增量 upsert 导入。**口径纪律：凡是 Excel 加出来的列，必须同步在报告「数据源配置 → 字段说明」写一句口径——报告读者看得见的口径只剩这里。**

## 请求协议

Agent 只写 `report_request.json`，需通过 `scripts/validate_request.py`：

- `schemaVersion: 1`、`templateId: "standard-bi-v1"`、非空 `title`。
- `chart id` 可省略，脚本自动分配；`columnRoles` 可省略（脚本从 profile 推断，只校验未引用的列是否存在）。
- `charts` 1–6 项，白名单 `presetId`：`bar.rank`/`bar.stacked`/`line.trend`/`area.trend`/`scatter.correlation`/`pie.share`/`donut.share`/`funnel.stage`/`table.aggregate`/`table.detail`。KPI 用 `kpi.metric`（不占图表槽位）。
- 筛选只用白名单操作（`eq/ne/gt/gte/lt/lte/contains/in/not in/is null/is not null`），最多 10 条，`AND + 一层 OR`。
- `bar.rank`/`bar.stacked` 支持 `limit`（TOP K），运行时提供 K 选择（5/10/20/50/全部）并即时重算。TOP-N/排序/表格分页写在 **chart 级**：`chart.topK`（1-100，权威）+ `chart.limit`（别名，同槽位）+ `chart.sort`（`desc/time/none/asc`，也接受 `{column, direction}` 对象形式——column 须为第一个指标）+ `chart.pagination`（bool，仅 table 有意义，R2 表格容量豁免）。这些键与 `encoding:{...}` 块并列写在 chart 上即可，校验器会归一化进 encoding（重复校验幂等）。`sections[].charts` 可引用 chart **id 或标题**（标题需唯一，重名报错时会列出 id↔标题对照）。
- **筛选条件不带 `value`（或 value 为空）= 「未选任何值（全部）」**，不加数据约束——这是声明交互筛选控件的正常写法；编译 `_row_filter`、server `_filter_clause`、运行时 matchFilter 三层一致按 no-op 处理，validate 会打 advisory 提醒。带 value 但匹配不上任何行时，图表会以 "chart has no data rows" + filters 内容报错。
- `bar.stacked`/`bar.rank`/`table.aggregate` 支持 `breakdown`（第二维度）→ 渲染「A×B」交叉表（分组柱/透视表），用于「维度 × 维度 计数」。
- **多数据源 `datasets`**（可选，≤8 张表）：每张表独立成数据源——`{id, label, file, columnRoles?, measureTypes?, hierarchy?, role?, isDefault?}`（`role` = primary 主表 / lookup 补充表；`isDefault` = 唯一默认主表，无 `datasetId` 的图表/KPI 归它）。顶层语义是默认槽（列存在时回退到每张表）。**多表可直接上 server**：显式 `--mode server` 把每张表注册为服务端数据集（`{reportId}__{datasetId}`，schema-only manifest）；`auto` 按表选 singlefile/remote（mixed）。图表/KPI 用可选 `datasetId` 指定归属（缺省第一张表）；报告级筛选只作用于**含该列的**数据集，其余表不受影响。数据源徽标按卡片实际归属表显示。singlefile 模式下多张表内联、总行数/字节累加，超限报错。单表请求不带 `datasets` 时行为与以前完全一致。
- **表间关系 `relations`（两表保留 + 关联键 + 合并宽表数据集）**（可选，≤16 条）：`{id, name?, tables:[dsId, dsId, …], keys:[{name?, a:{ds,col}, b:{ds,col}, mode:"a"|"b"|"both"}], fields?, renames?}`。声明 relation = **源表全部保留**，把 N 张表**链式合并**出一张宽表数据集（链连通性校验，每张 tables[i] (i≥1) 必须经某 key 连回 tables[0..i-1]）。`mode` 控制**键列在宽表的存留**：`a`=仅留 a 侧键列；`b`=仅留 b 侧；`both`=都留（重名加 `<ds>__` 后缀）。`fields` 按 ds 过滤保留列。`renames` = 合并后列名的用户别名 `{合并前列名: 新名}`，在链式 join **之后**应用，所以重新合并仍能保留改过的名字。**构建期合并** → 源表与 relations 原样进 manifest，`merge_tables.materialize` 只代物化一张宽表数据集（`<root>__wide`）追加进 `datasets`（manifest = 源表 + 关系 + 宽表三件套，**server 模式也能用**）；数据源面板关系地图如实呈现「两表 + 关联键」，宽表可解散、可重新合并。**运行时编辑** → `materializeRelationWide` 与构建期**同一语义**（singlefile 本地合并；server 模式 POST `/api/reports/:id/materialize-relation`，由 `server/relations.py` 复用同一个 `merge_tables.materialize` 在服务端物化成可查宽表——服务端本就持有全部源表）。**dimensionKey** 从每个 key（a.col ↔ b.col）自动派生（同名 `name` 合并、跟随 `renames` 别名），驱动跨表筛选级联与跨表同图聚合。宽表 datasetId 恒为结构化 ASCII `<root>__wide`（展示名走 `name`/label）。**合并的列名身份 = 去掉报告命名空间的 id**（server 模式表名是 `<reportId>__<raw>`，合并按 `raw` 算碰撞后缀，所以运行时「重新合并」产出的列名与构建期逐列一致：`inv__月份` 而不是 `dssrv__inv__月份`）；合并产物带回**每列的来源信息**（`meta.provenance`），宽表因此继承来源列的 `columnTypes`/`fieldRoles`（`inv__月份` 出生即时间列）。**合并键不预填**：`+ 新增关联` / `+ 添加键` 的字段下拉留空（`选择字段…` 占位），保存时空键被拦（客户端 + 服务端 `validate_relation` 各一道）。**数据源独立是互斥词汇**：报告级 `dataSourceMode:"independent"` 声明「各数据源完全独立、禁止合并」，与 `relations` 同现即校验报错。Guard: `tests/test_relation_merge.cjs` / `tests/test_build_merge.py` / `tests/test_multi_source_relations.py` / `tests/test_server_relation.py` / `tests/test_request_independent.py`。
- **跨表图表/筛选**：`charts[]` 用 `axis:{dimensionKey}` + `series:[{column,datasetId,aggregation?}]` 表达「同一 x 轴跨表对齐」——`dimensionKey` 指向同域关系对齐出的共享维度，`series` 每项从其 `datasetId` 取指标（脚本按维度批次 merge，`/aggregate-multi`）；单表图仍用 `encoding`（`dimension`+`measures`+可选 `datasetId`）。跨表筛选用 `filters[]` 的 `{dimensionKey, op, value}`（沿对齐映射到各承载表对应列）。
- **measure 级默认语义与派生字段**：`measureTypes` 的 object 变体支持 `aggregation`（默认聚合，图表/KPI 未指定时生效）与 `decimals`（默认精度）；`semantic_types` 为受控别名（`type/aggregation/decimals`）自动并入 measureTypes。**行级派生列**（计算列）声明在 `datasets[i].derived.columns`（≤8，白名单 `add/sub/mul/div`，如 `{name:"客单价", op:"div", left:"销售额", right:"订单数"}`）——编译期物化成真实指标列（`scripts/derived_eval.py::materialize`，运行时更新数据/编辑单元格后按同公式重算），**singlefile 与 server 都支持**：server 模式由服务端按同一函数物化进 SQLite（`server/derived_columns.py`，注册表记录声明并在每次装载后按「同一表 id 的声明并集」重放）。**聚合级派生指标**声明在 `datasets[i].derived.measures`（≤16）：`kind:'formula'`（聚合后公式，`expr` = `{const}`/`{agg,col}`/`{ref}`/`{op,left,right}`，如 毛利率 = SUM(毛利额)/SUM(收入)）或 `kind:'window'`（时间窗口：`input` + `fn:'prev'|'cum'` + `offset` + `grain:'week|month|quarter|year'` + `timeField` + `output:'rate'|'delta'`，环比=prev/1、同比按粒度 月12/季4/年1、累计=cum）或 `kind:'filtered'`（**筛选聚合/条件聚合**：`agg` + `col`（count 可省=计行数）+ `filters:[{column,op,value}]` ≥1 条，op 白名单 `eq/ne/gt/ge/lt/le/between/in` 与报告筛选同形状同词汇）——组宇宙由图表分组决定、组内只聚**通过筛选的行**（SQL `AGG(CASE WHEN 谓词 THEN 列 END)`）；无命中行 count/countd=0、sum/average/min/max=null（空缺不造 0）。同环比 = 窗口节点 `input:{ref:'该筛选指标'}` 引用它即可（`w36 环比 w35` 场景）。或 `kind:'share'`（占比：`scope:'all'`=占总计，分母=当前聚合全部值之和、与合计行同口径；`scope:'parent'`+`parentField`=占父级组内之和，图表维度应是父级字段的子级）。粒度声明在指标上（不跟随图表分桶）——引用它的图表维度必须正是它的 `timeField`、且分桶与 `grain` 一致，否则编译期直接给出可行动报错（绝不比错周期）。求值发生在**聚合之后**：编译期把值写进静态 option，运行时按同一求值器重算（WYSIWYG）；`{agg,col}` 依赖会自动补聚合序列，空档（如首期无前期）保持 null 而不是 0。派生声明跟随其所属数据集——多数据集报告逐数据集物化与校验，不要求先合并成单表。**定义只在一个地方**：数据源配置 → 字段配置 的「计算列」/「派生指标」两区（图表面板只读引用：派生指标就是普通指标 + `ƒ` 徽标，窗口依赖不满足时该 chip 置灰不可拖并说明原因；面板底部的「＋ 新建派生指标」跳转到数据源配置，不是第二套定义 UI）。**KPI 卡的「对比」也引用一个窗口派生指标**（`kpi.compareMeasure`），不再有内置的 环比/同比 口径。旧报告的图表级 `data.derived`（mom/yoy/running/share）在载入时自动迁移成窗口/占比节点，迁移成功的 token 从图表副本删除（只渲染一次）；无法无损迁移的（周粒度同比、按筛选子集占比）保留旧路径并置空 + 告警，**宁可空缺也不给错周期**。
- **行数（记录数）是一等指标**：`aggregation:"count"` 在编译/server/运行时三层都统一为**行数**（`COUNT(*)`，非某列非空计数）。图表 `encoding.measures` 可用合成标记 `["*"]` 显式表达「每行=一条记录」的行数指标（显示名「记录数」）；当 `measures` 省略且数据**没有 measure 列**时，编译器自动回退为 `["*"]` + `count`。周序号/季度等周期列（如 `周`=30..36）会被 profile 判为分类而非可求和 measure，此类「记录型」数据 `profile.json` 会带 `recordDataset:true` + hint，`suggest_report.py` 自动产出 count 图表。
- **分节文本支持作者侧 Markdown 子集**：`sections[].narrative/insight` 与 `chart.annotation` 可用 `#/##/###`、`**加粗**`、`*斜体*`、`~~删除~~`、`- 列表`、`> 引用`、`` `代码` ``、`[链接](https://…)`、`---` 分隔线与空行分段——编译期白名单解析，其余一律转义为纯文本。
- **分节/故事结构**：`sections[]`（≤8 节）把图表讲成故事——每节 `{title, narrative?, charts:[图表 id], insight?}`，渲染为「叙述 → 该节图表 → 节内小结」；未归入任何节的图表自动落到末尾「正文」节（不带 sections 的报告行为与以前完全一致）。`chart.annotation`（可选，≤2000 字）给单个卡片写基于证据的解释，显示为卡片下方可编辑标注。分节/标注只影响结构，不改变图表语义。
- **文本卡编辑全内联（无右侧「文本卡片」面板）**：`sections[].textKind` ∈ `heading/paragraph/callout/quote/divider`（divider 允许空标题，纯分隔线）。每卡「卡片样式」（textStyle 透明/白底/callout/banner）与段落/气泡级别(H1/H2/H3/T)、新增、删除×、拖动排序全部在卡片上就地完成；点选文本卡/批注不再摊开侧栏。**每卡 surface/accent/radius/border（“卡片风格”）已删除**，卡片外壳统一回 Notion 默认（后续再单独设计外壳样式）。
- 图表 family 高度趋同（如 4/4 都是 bar.rank）时 `validate_request.py` 打 advisory 提示（非阻断），建议混用 family。
- **时间列支持多种写法**（`2026/9/5`、`2026-9-5`、`2026.9.5`、`2026年9月5日`、`20260905`、`2026-09`、`202501`、`2026`、epoch 秒/毫秒，以及**周类型值** `w35`/`W36`/`2026-W35`/`2026年35周`/`第35周`——周必须带显式 w/W/周 标记，`2026-35` 不认；周值列自动识别为周粒度时间轴，归一标签 `W05`/`2026-W05` 三端逐字一致，`w36 环比 w35` = 窗口 `prev` + `grain:"week"`）——编译、服务端分桶与运行时用的是同一份解析器（`scripts/date_parse.py`），所以时间轴必然按日历序排列、按日/周/月/年正确分桶；作者只需把该列声明为 `columnRoles.<col> = "time"`（或让 profile 自动识别）。
- `style` 只能取预设 accent/density/radius/fontScale/theme/chartTheme；`style.chartTheme` 是**报告主题轴（整报告 house）**（docs/report-theme-convergence-design.md）∈ `default/economist/nyt/swiss/nature/mckinsey/datawrapper/powerbi/powerbi-light/pop/cartoon/huawei`：图表配色/字体/网格 + 报告壳（封面渐变、KPI 强调条/涨跌色、表头、筛选 chip、合计行）一并跟主题换肤；`powerbi`/`pop` 是深底档，仅图表换肤、壳保持默认。**默认必须是 `default`：请求里省略 `style.chartTheme` 就行，不要自作主张挑预设**（曾有一份报告因为随手写了 powerbi 而以 Power BI 深色配色交付）。只有用户**明确要求**某种风格时才写这个键；运行时顶部工具栏「主题 ▾」可随时切换（悬停即整报告预览，菜单项带样票）。显式 `style.accent` 优先于主题默认 accent。lint L19 会对非默认主题给出警告。

图表类型白名单：`bar`、`stackedBar`、`line`、`area`、`pie`、`donut`、`funnel`、`scatter`、`table`。其余（热力图、仪表盘、地图、雷达、桑基等）不在范围内，不可绕过白名单。

## 业务问法 → 请求词汇（写请求前对照）

只声明**意图**——每个键不声明都有校准过的缺省行为，专业默认住在缺省值里，业务点名才写。
完整四元组表（业务问法 → 声明键 → 值域 → 缺省）见 `references/authoring-feature-map.md`，速查：

- 页面更宽/大屏 → `pageWidth`；换品牌风格 → `style.chartTheme`（**仅用户明确点名才写**）；主色 → `style.accent`。
- 表格「只看近 N 期/某时间段」（周/月/季/年；周列恒看近 N 周）→ `chart.table.dateScope`；每个维度各限取值 → `dimScopes`；预置列筛选 → `columnFilters`。
- 「组内前 N / 区域内排名」→ `chart.table.groupTopN`；柱图 Top-K → `chart.topK`；表格只显示前 N 行 → `chart.table.limit`。
- 「达标标红/异常高亮」→ `chart.table.condFormat`（阈值向业务要，不要用 与均值/与0 凭空造阈值）；「合计应为平均/去重」→ `chart.table.totalCalc`。
- 指标旁同比/环比副列 → `chart.table.subMeasures`；行尾趋势列 → `sparkline`；行总计列 → `rowTotal`；段尾小计 → `showSubtotal`（以上缺省全关，点名才开）。
- 每行算一列（客单价/库龄）→ `derived.columns`；聚合后比率/同环比/累计/占比 → `derived.measures`；枚举值归大组（省→大区）→ `dimGroups`。
- KPI 对比副行 → `kpi.compareMeasure`（引用窗口派生指标）；读者交互筛选 → `filters`（不带 `value` = 控件无预选，正常写法）。

**三条硬规则（W36 真机教训，违反即返工）**：

- **复合指标逐条对账**：提示词点名的每一个非基础度量（比率/均值/占比/同环比/累计/店均/去重计数，如 WOS=Stock/Sales）= 请求里一条显式声明（`datasets[i].derived.measures` 聚合级 / `derived.columns` 行级，词汇见 `references/authoring-feature-map.md`）；交付前逐个核对提示词点名的指标都落了声明，绝不静默降级成只有基础 SUM。
- **筛选语义不改道**：提示词明说「筛选/过滤/只看 X」的维度值条件 → 落 `filters`（报告级）或 `chart.table.columnFilters`（卡级，表格卡）；禁止翻译成维度/breakdown/交叉列。图表卡需要逐卡筛选时用报告级 `filters` 或改用表格卡建模。
- **数据源独立**：提示词出现「独立数据源/不要合并/各自来源」→ 请求写 `dataSourceMode:"independent"` 且**不声明** `relations`（二者互斥，校验拦截）；未强调时**默认不合并**——只有某张卡确实需要跨源字段才声明 relation。

布局可以更自由：每个 `chart` 可带 `layout: {w,h}`（不写 `x/y` 时仍自动排布；写 `x/y` 则按作者坐标放置）。`annotationPlacement: "right"` 会把批注放在图表右侧 8+4 侧边栏；默认 `below` 保持在图表下方。编译器会在 KPI 不足 4 个时自动补足 4 张，并根据低基数维度自动生成筛选器候选，让筛选卡默认更完整。

密度策略：多指标 `bar` 会自动渲染为柱+线组合图；`stackedBar` 在少数主类目但大量 breakdown 序列时会自动改成折线，避免堆叠柱过度拥挤。筛选器卡允许换行，多个筛选条件不再被单行横向滚动挤成不可用状态。

## Windows 执行

**先直接尝试运行，不要先做环境检查/引导**：本技能只依赖 Python 3.10+ 标准库（无第三方依赖），自带沙箱/环境的 Agent 直接 `py scripts\report_build.py ...` 即可。只有直接运行失败（找不到 Python、版本过旧）才运行 `scripts/bootstrap_windows.cmd`（自动找 `py`/`python`，必要时安装/离线运行时）。不要依赖 WSL、bash、PowerShell 复杂转义。制作离线包时运行 `scripts/prepare_offline_runtime.cmd`（下载官方 Windows embeddable Python 到 `assets/python-runtime`）。

## macOS / Unix 执行

在 macOS 或类 Unix 宿主上直接使用系统 `python3`（需 3.10+）和 `node`，同样先直接运行、无需引导。**先 cd 到数据文件所在目录再运行所有脚本**（报告文件夹与其全部临时文件默认落在数据同级）：

```bash
python3 .pi/skills/live-report-master/scripts/profile_table.py --table data.csv --out build/profile.json
python3 .pi/skills/live-report-master/scripts/report_build.py --table data.csv --spec report_request.json
open "<输出目录>/<报告标题>.html"   # singlefile（文件名=报告标题）
```

server 模式用 `python3 .pi/skills/live-report-master/scripts/run_server.py` 替代 `.cmd`，停止服务发送 Ctrl-C 或删除对应 pid 文件；其余命令与 Windows 相同，只是把 `py scripts\\...` 换成 `python3 scripts/...`。

### 一键构建

```cmd
py scripts\report_build.py --table input.xlsx --patch report_intent.json    :: 先直接运行
cmd /c scripts\bootstrap_windows.cmd                                        :: 仅当找不到 Python 时
```

**默认 + 补丁（提速路径，推荐）**：模型不必手写完整请求。先让 `suggest_report.py` 从数据形态确定性地产出一份**完整合法**的默认请求，模型只写要改的那几点（补丁），其余全部回退到默认：

```cmd
py scripts\\suggest_report.py --table input.xlsx --out report_request.json   :: 可选：先看默认建议
py scripts\\report_build.py --table input.xlsx --patch report_intent.json    :: 补丁覆盖默认后构建
```

- **在线编辑默认开启**：server/mixed 模式构建时若没写 `runtime.editPassword`，脚本会**自动生成一个口令**并在构建控制台 / `build/README.md` / 报告落地页打印——初版开箱即可保存。要改口令或关闭在线编辑，打开报告管理页 `<serverBase>/manager`：每条报告有「允许在线编辑」开关与「设置口令」按钮；关闭后任何保存都会提示「当前暂无法编辑保存报告，请联系管理员」（分享只读/可编辑链接不受影响）。

补丁是一个**稀疏 JSON**，只写需要覆盖的键：`title` / `subtitle` / `analysisGoal` / `charts`（整体替换）/ `kpis` / `insights` / `filters` / `sections` / `columnRoles`（逐列合并）/ `measureTypes` / `style` / `runtime` / `relations` / `datasets`。未写的键用默认值（图表、角色、KPI、筛选均由编译期自动推断/补足）；`suggest_report.py --patch` 可预览合并结果。`--spec`（全量请求）与 `--patch`（补丁）二选一。

### 创作入口：说 / 选 / 画（用户诉求的三种模态）

用户不必懂请求词汇——三种入口殊途同归，最后都汇成 `report_request.json`（详见
`references/brief-template.md` 与 `references/sketch-transcription.md`）：

1. **说（缺省）**：一句话直接构建——profile 后走「默认 + 补丁」。适合快速看数。
2. **选（诉求单）**：用户想要结构化表达时，按 `references/brief-template.md` 的
   固定槽位给诉求单（可由你在 profile 后代填，最多追问 3 个带默认值的问题）。
   落盘 `build/brief.md`，随刷新增量维护。
3. **画（草图）**：用户发来手绘/截图/参考报告图片时，按
   `references/sketch-transcription.md` 转译为 free-v1 请求（区域→块、相对位置→
   layout 坐标），**先出草案回确认，不静默采信**。

4. **拼版画板（画模态的在线形态，2026-09-19 落地；用户路径收敛 2026-09-20）**：空白
   草稿画布（`draft:true` 的 free-v1 草稿报告，随 server 注册表持久化）；
   用户拖占位块（kind:'compose'）自由摆位，逐块三态填充——面板直接绑字段 / 一句话 NL
   （`block.compose.nl`）/ 留白交给系统。**入口分三层（殊途同归，按场景就近）**：
   - **真冷启动（本机还没有任何报告/server）**：用户说「拼版 / 开画板 / 我自己拼一份」
     即路由到这里——跑 `python3 <skill>/scripts/report_build.py --draft --title <草稿名>`：
     起专属草稿 server（home 缺省 `./拼版草稿/`）+ 建零数据集空白草稿，把打印的画板
     URL 交给用户开拼；数据可在画板内「数据源配置 → 添加数据表」上传，或转正构建时
     再给 `--table`。
   - **服务已活着**：报告管理页 `<serverBase>/manager`「＋ 新建拼版草稿」（管理页可
     标识/删除草稿）。
   - **报告内**：任何报告编辑态顶栏「＋ 新建拼版草稿」（以当前数据集为底开**新**草稿）。
   **示例画廊与占位块只在草稿里可用**——成品报告的插入菜单只有真实卡类型（图表/文本/
   表格/筛选器），转正后自动收窄。
   **转译工作流（用户拼完后）**：
   ① `GET {serverBase}/api/reports/<draftId>` 读草稿 manifest，每个 compose 块的
   `compose.{slot,nl,chartType}` + `layout` 就是逐块诉求；② 按 sketch-transcription
   同一条契约逐块翻译（时间词带锚点、留白按数据形态补、**必出逐块草案确认**），汇成
   free-v1 请求（charts 带相同 layout 坐标）；③ 原址转正：`report_build --table <数据>
   --spec <request> --report-id <draftId> --server <草稿所在 server> --mode server`
   ——h_register 覆盖同 reportId，草稿 URL 直接变成正式报告。草稿态汇报/PPTX/PDF
   一律拦截提示；`h_update_manifest` 对 draft 放行零图表保存。

**何时用 `--free`（自由模式，templateId=free-v1）**：`report_build.py --free` 下
「声明即全部」——封面/KPI 条/筛选带/洞察只渲染显式声明的，编译器不再自动垫满、
不再出筛选候选、无声明封面则整张报告没有封面（标准档缺省路径一字不变）。判断
启发：用户在「要什么」之外还成体系地说了「不要什么」（极简查数页、只要三张图、
不要封面），才用 free；否则自动填充是增益不是噪音。free 请求至少声明
charts/kpis/insights/sections/cover/dataTable/texts 之一，否则校验报错；`charts` 键可
整个省略（纯 KPI/文本报告，视同空数组）。`facts` 是口径数值，须由 insights
（`{{fact:id}}`）或 KPI（factId）引用才可见，不单独构成区块。free 的容量上限 =
charts 30 / kpis 12 / insights 10 / sections 20 / texts 20（standard 维持
1-12/4/3/8/—）；KPI 条每行 4 张自动折行。独立文本块 `texts:[{id?,title?,text,
style?{textStyle,cardColor},layout?{x,y,h},after?}]` 是「加一段说明文字」的直接
词汇：整行宽段模型卡（编辑器可编辑段落），`after`=图表 id 时紧随其后，
layout.x/y 可钉任意位置。`kpiPosition:"bottom"` 把 KPI 条移到图表/文本区与洞察
卡之后（「KPI 放底部」；缺省 top，standard 不支持）。

**先草案后收口**：构建不强制审计——第一轮先出草案给用户对着实物提修改
（「趋势换占比」→ 补丁重构建），收敛后再按交付纪律补 `--audit-required` 全量。
`build/decisions.md` 是「哪些是系统定的」清单，自由/草案模式下就是用户的购物车。

模型**无需指定输出目录**。省略 `--out` 时，脚本自动创建以报告标题命名、Windows 安全的文件夹：

```
<报告标题>/
├── <报告标题>.html      ← singlefile：最终报告（双击离线打开）；server/mixed：指引入口页（URL/口令/管理页，不是数据）
├── 打开报告.command     ← server/mixed 模式：双击即启动本地服务并打开浏览器（macOS）
├── 打开报告.cmd         ← server/mixed 模式：同上（Windows）
└── build/               ← 构建产物（profile/table/draft/manifest/decisions 决策清单等）
```

- **singlefile**：交付一个 `<报告标题>.html`，双击即可离线打开，无需任何服务。
- **server / mixed**：交付**双击即用**的 `打开报告.command`（macOS）/ `打开报告.cmd`（Windows）。它会自动启动本地数据服务（绑定 `0.0.0.0`，注册表/数据落在机器级 `~/.ai-html-report/`，全机所有报告共用）并打开浏览器到报告 URL；`<报告标题>.html` 只是**指引入口页**（载有本机/局域网地址、编辑口令、报告管理页链接），数据不在文件里。服务停止/本机关机后他人无法访问。
  **硬规则（2026-09-20 用户反馈）：只要模式是 server 或 mixed，就绝不把 `<报告标题>.html` 当报告附件/文件交付**——mixed 的内联数据虽在文件里，但服务端数据集必然加载不出来，用户会看到「半份报告」。交付 = 报告 URL + 启动器/指引入口页 + 管理页地址。

`--out`/`--workdir` 仍可显式指定（向后兼容）；`--out-dir` 指定标题文件夹的父目录（默认当前目录）。

**报告文件夹（含其全部临时/构建文件：build/、merged spec、build-status.json）默认创建在用户提供的数据文件同级目录**——不传 `--out-dir`、无论从哪个目录运行都成立；`--out-dir` 可显式覆盖。Agent 手写的 intent/spec json 也放在数据同级。**一切中间产物只落在报告输出目录，绝不写进 skill 目录**：运行脚本时不要把 skill 目录当工作目录，也不要在 skill 目录内存放任何临时文件。入口脚本已禁写字节码（`PYTHONDONTWRITEBYTECODE`），正常运行不会在 skill 目录产生 `__pycache__`。

`report_build.py` 以 argv + `shell=False` 依次执行：profile → request/table 校验 → 编译 → lint → 打包 → 校验 → 浏览器审计。退出状态写入 `build/build-status.json`：

- `ready`：全部门禁通过（含真实浏览器审计）；
- `needs_audit`：报告有效，但 Node/Chrome 缺失或明确跳过审计；
- `failed`：结构/数据/编译/门禁失败。

### 常用参数

- `--mode auto|singlefile|server`（默认 `auto`）：先 profile 判模式。
- `--out <file>`/`--out-dir <dir>`/`--workdir <dir>`：输出路径（见上文自动命名）。
- `--threshold N`：singlefile 行上限（默认 100,000）。
- `--max-bytes B`：singlefile JSON 上限（默认 50 MB）。
- `--max-cardinality C`：singlefile 最高基数上限（默认 2,000）。
- `--report-id id`/`--server url`：覆盖 `request.reportId`/`runtime.serverUrl`。
- `--skip-audit`/`--audit-required`：跳过审计 / 审计失败即硬失败。

## 交付后回复（必做）

报告构建完成后，最终回复用户时**必须**包含以下三点（server/mixed 模式还必须原样转述构建日志里的 `[open]` 三行——本机/局域网/报告管理三个地址——以及 `[password]` 编辑口令；server/mixed 生成的 `<报告标题>.html` 是指引入口页，同样载有这些信息；**该文件绝不作为附件交付**）：

1. **报告位置与打开方式**（singlefile：双击 html 即可离线打开；server：双击 `打开报告` 或访问报告 URL；局域网同事用 `[open]` 里的局域网地址）。
2. **两种继续修改的方式**：可以继续在对话里提出修改（我来重新构建/更新报告）；也可以直接在页面上编辑。
3. **页面编辑能力简介**（一两句话）：编辑态下可内联编辑文本（格式条/卡片样式/拖拽排序）、点图表卡改维度/指标/图表类型/TOP-K/目标值（**柱状图与折线图合并为一张 2 列宽的「柱状/折线图」卡（标签居中，卡内不再分 柱|线；其右侧是「指标卡 KPI」类型卡）；基础几何由每个指标的「形态」分段按钮（柱形/线形）决定——全柱形=柱状图、全线形=折线图、混排=柱线组合；「形态」只在柱状/折线图出现，换图表类型会清空形态覆盖；指标的 Y 轴（左/右）与 Y 轴单位（原始/K/万/百万）也用同一套显性化按钮组**；表格类型下有 明细表/聚合表 子选项：明细表自选字段并给指标字段配 小数位/单位/目标进度条；聚合表指标支持 列级小数位/单位（表头自动括号标注）/目标数据条/占比列/合计行/分组小计行）、**「未选择」区按 时间/维度/指标/其他 四个虚线框分组**（其他 = id/忽略/未识别列，只展示不可拖入，需先在「数据源配置」改角色）、插入图表/指标/文本/筛选器卡片、**指标卡 KPI 是「图表类型」里的一种（同一张类型网格里双向切换：图表面板的 KPI 卡 → 变成指标卡；指标卡面板里点其它类型 → 变回该图表）（与「表格 → 明细表」同级）：插入卡片只有 图表/文本/筛选器，新建图表后在图表类型里点「指标卡 KPI」即把该图表卡转换为指标卡；设置风格与图表面板一致（图表类型 / 数据设置（未选择·已选择 + 指标处理/小数位数/单位）/ 显示设置），支持 目标值完成度条 与 对比（**对比 = 引用一个窗口派生指标**，开启后数值即本期值，副行由该指标按时间桶最新一期给出）**、「数据源配置」全屏页管理数据表（新增/删除/默认主表/表角色）与表间关联、按 时间/维度/指标 分组配置字段角色与维度层级（维度行拖 ⠿ 建层级：**只允许把一个字段拖进另一个字段成为其子级**，提示语「拖 ⠿ 调层级：将字段加入其他字段中以形成父子关系」；脱层 = 拖到「维度」表头）、筛选器实时联动（字段配置里建好维度层级后，筛选器按层级级联——子列选项随上级选中值收窄并自动清理失效选择；编辑态还可勾「层级筛选」把整条链合并成一个多层下钻控件——每级一个 chip，点开弹出该级选项列表，选完本级自动弹出下一级（区域 ▸ 办事处 ▸ 国家，逐级下钻）；点柱/饼切片或下钻表行可按层级下钻，多分支层级会弹「下钻到」选目标层级）；「汇报模式」提供行对齐的 16:9 分页预览与放映（**汇报分页与单页正文两份布局相互独立**，每页顶部预留标题带，装不下顺延下一页；顶部有说明与「从单页重置」）、可导出 PPTX；「保存与分享」中心可保存副本/覆盖、分享可编辑/只读链接（server 模式）或下载文件（singlefile），并承载 导出 PPTX（图片版/原生版）与导出 PDF（汇报模式下中心无分享项，两种形态共用一个链接）。「报告管理」页没有页面入口——把构建日志 `[open]` 里的 `/manager` 链接直接告诉报告主人（仅限其本机打开，用于集中查看/关闭这台电脑上所有运行中的报告）。

## 模式与数据量

`runtime.mode`：`auto`/`singlefile`/`server`，默认 `auto`。

- **singlefile**：行 ≤ 100,000、JSON ≤ 50 MB、最高维度基数 ≤ 2,000。全量行内嵌，适合离线分享。超出任一上限转 server 或预聚合。
- **server**：行 ≤ 5,000,000、列 ≤ 200。数据导入本地 SQLite（WAL + 索引），浏览器只拿 schema + 按需聚合结果。超过上限先拆分或预聚合。

`auto` 判定会**排除 `id`/`ignore` 列**：唯一订单号等实体键不把报告推向 server，只有真正作为维度/时间用的列才计入基数上限。

每次构建在输出目录生成 `data-quality.md`；HTML 首屏不重复展示质量/来源/证据尾部。

## Server 模式

**统一 manager（2026-09-20）**：全机只跑**一个**报告 server。注册表/数据快照/pid 都落在用户状态目录 `~/.ai-html-report/`；`report_build` 构建时优先**复用本机任何活着的报告服务**（`/api/health` 带 `registryPath` 标记）并把新报告热注册进去，没有活服务才从 8765 起找空闲端口启动一个。因此：报告管理页（任一报告服务的 `/manager`）一处能看到本机**所有时间构建的所有报告**（含拼版草稿）；旧版「每报告一个端口/注册表」的报告在重新构建一次后并入统一管理。任何报告文件夹里的 `打开报告` 启动器拉起的都是同一份中央注册表——报告目录拷到别的机器也能用（`$HOME`/`%USERPROFILE%` 运行时解析）。

**给业务用户（非技术）**：不要碰下面的命令行——`report_build.py --mode server` 已在报告文件夹里生成一个**双击即用**的 `打开报告.command`（macOS）/ `打开报告.cmd`（Windows），双击即自动启动本报告打包的本地服务并打开浏览器。以下命令仅供 Agent/运维手动管理服务：

```cmd
cmd /c scripts\\start_server.cmd --port 8765
py scripts\\serve_report.py --server http://127.0.0.1:8765 register --data input.xlsx --spec report_request.json --report-id sales
cmd /c scripts\\open_report.cmd http://127.0.0.1:8765/reports/sales
cmd /c scripts\\stop_server.cmd
```

`run_server.py`：`--host`（默认 `0.0.0.0`，局域网可访问）、`--port`（默认 `8765`）、`--registry`（默认 `~/.ai-html-report/registry.json`）、`--data-dir`（默认 `~/.ai-html-report/data/`）、`--engine sqlite`、`--pid-file`。默认绑定所有网卡；server 模式的「保存与分享 → 分享」复制局域网链接（只读=`?readonly=1`），依赖本机保持开机/服务运行，关机后他人无法访问；API 仅提供健康检查、注册/注销、schema、受限 aggregate、分页 rows，不暴露任意 SQL 或公网鉴权。

**server 运维三件套（业务用户友好）**：

- **报告管理页 `/manager`**：每台电脑上任意一个报告 server 都会提供，集中列出本机**所有**运行中的报告（按报告族（family）分组：初版 + 各副本版本，含标题、本机/局域网链接、编辑口令、PID、更新时间与版本修改日志），并可一键「关闭服务」；**仅限本机回环访问**——`GET /manager` 与数据源 `GET /api/reports/all` 对非 loopback 调用方一律 403，报告页面里没有任何进入它的按钮，链接只通过构建控制台 `[open]` 三行 / `build/README.md` / marker 落地页告知报告主人（同端口上的全部报告含副本一起下线）。全局服务索引 `~/.ai-html-report/servers.json`（`server/servers_index.py`，server 启动注册、退出移除、死条目懒清理）；关闭端点 `POST /api/admin/shutdown` 同样仅接受 loopback 调用。
- **server 模式的 `report.html` 是构建落地页**（不是报告本体）：载有本机/局域网访问地址、编辑口令、`/manager` 链接、重新打开与关闭服务说明；报告本体在 `http://127.0.0.1:<port>/reports/<id>`。
- **局域网分享**：绑定 `0.0.0.0` + 「保存与分享 → 分享」复制局域网链接；同一 WiFi/网段内的其他电脑、手机直接打开即可（首次可能需要允许 macOS/Windows 防火墙入站）。

> **server 资产启动时缓存，改完必须重启**。`TemplateProvider.__init__` 在进程启动时一次性读取 `editor-runtime.js`/`chart-adapter.js`/`editor-runtime.css` 及模板并缓存。改完 runtime/模板后先停旧进程再重启，否则看到的仍是旧代码页面。

## 渲染一致性

单文件与 server 两条路径共用同一套布局/格式/样式，避免漂移。布局（`pack_layout`、`_default_h`、`_cover_h`）、KPI（`_format_kpi`）、样式（`SHELL_CSS`）都在 `scripts/spec_to_report.py`；`server/template.py` 的 `_render_body` 直接复用，不再自写一套。修改布局/样式优先改共享函数，不要只在一处打补丁。

## 数据源配置（字段配置 4 列 · 数据集级默认）

编辑器的「数据源配置」是全屏三段式：左侧数据表列表（可搜索，含合并组虚线框 + `N 行 · N 列 · N 图`），
右侧 = **字段配置**（默认）或 **画布模式**（表级关系图：表卡片只列出关联键 + 对端表 + 行数/列数，连线带键名；
点卡片配字段、点连线开合并配置；节点位置随报告保存，拖乱了有「重新排布」）。

**字段配置只有 4 列**：`字段（重命名=别名） | 角色 | 默认聚合 / 格式 | 示例值`，外加**字段行下的数据事实副行**：
`类型 · N 个不同值 · 缺失 x% · 范围 a – b`（数值列另有 `⚠ n 行非数值已按空` 警示；含大量非数值
编码（如 A431/B06 形态）的列在构建期整列按**编码列**保留原值，副行显示 `ℹ n 个编码值已按文本保留`
——countd 可用、sum 不可用，构建控制台有对应 `[data]` 提示）。
这些数字**只在编译期算一次**（`profile_table.column_stats` → manifest `datasets[].columnStats`），
运行时只展示、绝不重算（server 报告 manifest 里没有行）；数据被替换后统计立即作废（显示“—”而不是旧数字）。

**重命名 = 别名（只改显示名）**：字段行显示「显示名 + 原名 + ✎/↺」，改名弹窗会告知影响面
（几张图表 / 指标卡 / 派生定义）并说明「源数据列名不变」。取数（SQL/服务端聚合）始终用原名，
所以改名不会让任何图表失效；要改数据本身的列名用宽表的「改源字段」。
**显示名不能重复**（两个字段同名会让图例/表头分不清）——重复时弹窗内联报错、不保存。
**字段说明**（`说明` 按钮，≤120 字）：写「这列是什么 / 怎么算的」，只改编辑体验
（字段行 + 图表面板字段 tips），取数与图表仍用源列名。
**读者可见的口径说明**（编辑器自动生成，不需要请求里写）：图表/表格若被 TOP-N 截断，卡片下会
显示「已按首指标显示前 K 项（共 N 项）」；筛选后 0 行时给「当前筛选条件下没有数据」+ 一键清除筛选。

- **所有改动实时生效**，底部按钮是「**完成**」/「**放弃本次改动**」（放弃 = 回滚字段语义与关联到打开时的快照，
  有改动时会先确认一次；新增/删除的数据表不在回滚范围内，用撤销）。
- **排序控件**（搜索框右侧）：`分组`（默认，角色分组 + 维度树）/ `名称` / `不同值` / `缺失`；
  后三者是**扁平**列表（再点当前键切换升降序）——树形缩进与按基数排序互斥，扁平模式下不显示 ⠿ 手柄。

- **角色 = 时间 / 维度 / 指标 / 忽略**（4 项；「ID」与「忽略」行为相同，已合并）。字段被改得与系统
  推断不同时，行内显示「系统默认：X」提示。维度行带 ⠿ 手柄，拖到另一行 = 建立父子层级（拖到
  「维度」分组表头 = 回到顶层）。
- **默认聚合 / 格式**（仅指标）= 数据集级**默认值**：新建图表、新拖入的指标会套用它，图表内可再单独
  覆盖（对齐 Power BI 的默认汇总方式/格式）。格式写 `measureTypes[col].type`（常规/百分比/货币/整数/
  时长/纯数），小数位 0-6。
- **时间粒度不在这一层配**：哪个图表要多粗的粒度就在那个图表的维度芯片上选（原始/年/季/月/周/日，
  季度是派生分桶）。数据集级「分桶」是个陷阱（改了对图表没影响），不要再加回来。
- **计算列**（行级，物化成真列，可当普通字段引用）：`字段 op 字段/常量`，可引用**写在前面的**计算列；
  空值/非数值一律为空（不写 0）。
- **计算列与派生指标共用一个「新建/编辑计算字段」弹窗**：两个 tab（行级 / 聚合级）× 四个形态
  （公式 / 时间窗口 / 占比 / **筛选聚合**——组内只聚满足筛选条件的行，如 数「销量=0 且 库存>1」的门店），
  带预设栏、可搜索的字段库、运算库（每项标出展开形态）、实时校验和
  **前 5 行/期真实数据预览**。定义只在数据源配置里发生，图表面板只读引用。
  支持的运算就是求值器真能算的（四则/常量/求和·平均·计数·最大·最小/环比·同比·累计·近N期/占比-占总计·占父级/筛选聚合-条件计数求和）：
  周粒度的同比、按筛选子集的占比这类算不对的口径**不会出现在选项里**；指标 chip 上的
  「显示为占比」勾选 = 快捷创建（或复用）数据源层的占总计派生指标并交换该芯片（再点还原）。
- **一层撤销**：删除卡片/字段/计算列/派生指标/合并/数据表、字段改名之后，底部会出现
  「已删除 X · 撤销」（Cmd/Ctrl+Z 等价）。只退一步。
- **派生指标**（聚合级，后聚合求值）：公式、窗口（环比/同比/累计，要声明自己的
  时间列 + 粒度，图表维度必须一致）、占比（占总计/占父级）或**筛选聚合**（条件聚合，sum/count 可加、countd/average/min/max 不可加）。
  窗口节点引用筛选指标即得同环比（`w36 环比 w35`）。图表面板只读引用（`ƒ` 徽标）。
- **字段引用反查**：字段配置每行有引用徽标（`被 N 张图表引用` / 虚线 `未被引用`），点开列出
  所有引用点（图表·维度/指标/拆分/轴/明细列/时间分桶源、指标卡、计算列、派生指标、筛选器、合并键），
  点条目滚动定位到那张卡片并高亮 —— 「改这个字段会影响什么」一眼可见。
- 字段搜索框只过滤行（排序控件在它右侧）；侧栏「数据表」也有搜索框（只过滤 DOM，不重渲染），
  侧栏表菜单「预览数据」看前 50 行真实数据（server 模式走有界接口）。
- **字段表本身**：表头 sticky（SHELL_CSS 的 `table.ai-report-table thead th`）、行悬停高亮、
  分组表头显示每组字段数。

### 更新数据：三模式（替换 / 增量合并 / 追加新行）+ 常驻任务条（2026-09-22）

「数据源配置 → 替换数据」弹窗有三个模式，按用户场景选（默认全量替换；用户说「补一周/一个月数据」→ 追加或增量）：

- **全量替换**：新文件整体换掉当前表（列结构不同走列映射向导）。
- **增量合并（upsert）**：按主键并入——有则覆盖、无则插入；幂等（重复导入同一文件结果一致）。适合往期追溯修正。
- **追加新行（append，免选主键）**：不查重不覆盖，文件全部行并入表尾。**同构文件补新一期数据（如 W35+W36、月度新一批）选这个**——按周期键判断增量全是新行时，upsert 选主键是纯仪式；代价 = 重复导入同一文件会产生重复行（弹窗已声明）。

**大文件（>4MB，server 模式）走直传 + 后台任务**：文件字节直传服务端（csv/tsv/xlsx），解析/合并/装载在后台 job 跑（`async=1` → `202 {jobId}` → 轮询）；右下角**常驻任务条**呈现上传百分比 → 服务端阶段 → 终态。**终态必须用户手动关闭**（成功带新增行数勾稽；失败带可行动文案 + 重试按钮，数据零改动）——绝不以一条几秒即逝的 toast 判断成败（W35 真机静默失败的根因）。xlsx 大文件解析约每 10 万行 10~40 秒属正常，任务条会显示已耗时。

**执行 Agent 纪律**：用户报「数据没更新」时先看两处——任务条终态文案 + 数据源配置里的行数；**绝不建议用户手工合并/转换源文件**来绕过导入慢（禁改原始数据；大文件慢是解析器问题，流式解析后 29 万行 xlsx 约秒级~分钟级）。**「补多期数据」两条正道按阶段选**：报告还没建 → `datasets[].file` 传**路径数组**（构建期同构并集，`W35+W36` 一行声明，类型以合并全集为准）；报告已建 → 页面「替换数据 → 追加新行」（免主键，直传+任务条）。

### 数据表组管理：全量清单 + 按表真删除 + 行级数据管理（2026-09-28）

数据源更新/上传新表/删除数据源的动线会在服务端留下「清单外」的物理表与快照（真机实锤：PSI 报告下曾躺着一张 335k 行的已删数据源残留表，白占磁盘且 UI 不可见）。三个入口：

- **侧栏「服务端数据表」块**（数据源配置底部，server 模式）：列出本报告**全部**物理表——在用表之外，历史/未引用表带「未引用」徽标 + 行数/占用，逐表「删除」= 引擎表 + 快照文件 + 注册表记账一起清（`GET /api/reports/:id/tables` + `POST …/datasets/:ds/drop`）。**副本共享表**（另一份报告的 manifest 仍声明同一 dataset id）只脱离不删——共享表被 DROP 会让副本的每张图表 400。
- **「删除表」是真的**：编辑器 ⋯ 菜单删除数据源在 server 模式会补发 drop 释放存储；旧实现只改 manifest，这就是历史表堆积的成因。编辑口令未设置/错误的报告 drop 会 403——存储仍可稍后在「服务端数据表」块删除。
- **「管理数据」弹窗**（原「预览数据」升级）：筛选构建器（列/操作/值，谓词与聚合同一条编译）+ 勾选行 + 「删除选中行」/「删除筛选命中行」。server 模式真删 SQLite 行并**重写重载真源快照**（既有快照先转存 `{stem}.pre-delete.json` 单份备份，人工回滚用）；命中全部行的筛选必须显式确认整表删除；行删除后受影响的合并宽表自动重物化。singlefile 在内存行上做同一语义，经保存通道固化。

**执行 Agent 纪律**：用户要「清理不用的表/释放空间」→ 数据源配置侧栏底部看「服务端数据表」块，按表删除前先确认该表不在任何报告的数据源清单里（`referenced:false`）；删行是**不可撤销**操作（有 pre-delete 快照兜底），引导用户先用筛选缩小范围再删。

## 报告筛选

请求里的 `filters` 是**交互式控件**而非静态说明。`editor-runtime.js` 在 `[data-report-filter]` 为每个筛选列渲染控件，并以 `{column,op,value}` 写回；变更后会重聚合所有图表（server 走 `/aggregate`，singlefile 本地过滤）并重算 KPI。未声明 `filters` 时，自动从维度列填充（最多 6 个）；高基数列只列出声明过的候选值。

**控件形态按列的角色**：维度 → 值域下拉（可切多选）；**时间列 → 日期范围**（起–止，任一留空 = 开口）；
**数值指标 → min–max 区间**。区间谓词 `between` / `daterange` 三端一致（编译 `_row_filter`、
服务端 `_filter_clause`、浏览器 `matchFilter`），日期比较在**规范化 ISO** 上做，所以
`2026/9/5`、`2026年9月5日`、`20260905`、`202501` 都能落进正确区间；空单元格在区间筛选下三端都不匹配。
结果为空时卡片显示原因 + 生效筛选数 + 「清除筛选」。

**时间层级下钻**：点击时间轴上的一根柱子/一个点 = 只看该桶 + 换更细一级（**年→季→月→日**），
卡片顶部出现 `全部 › 2025 › 返回上级 ▲`。粒度不会比**源数据精度**更细（月精度列没有「日」这一级）——
不提供会算错的入口。

**筛选器现在是「卡片」，带作用范围**：报告顶部的 `block-filter` 就是一个 `component='filter'`、`scope='global'` 的筛选器卡片；编辑器「插入卡片」还提供 **筛选器** 卡片（`addFilter`）。**每张筛选器卡片在「编辑」模式下**、左上角显示一个紧凑的**作用范围**选择器（`作用范围 全局/页签`，`renderOneFilterBar` 仅在 `editing` 时挂载；读者视图只显示筛选条件，不显示作用范围）：

- `全局` —— 作用于全报告所有块（合并页签显示下正常生效）。
- `页签` —— 只控制该卡片所在页签的内容（无论合并或分页均生效）；卡片被拖到某页签卡下方且范围=页签时，即为该页签内的筛选器。

范围与页签归属决定了有效筛选：一个块的**有效筛选** = 所有 `全局` 卡片的筛选 + 与它同页签的所有 `页签` 卡片的筛选（`effFilterForBlock`）。**分页模式下 `全局` 卡片被置灰忽略**（`body:not(.ai-report-single-page) [data-filter-scope="global"]{opacity:.5;pointer-events:none}`），并 `ensureTabFilters()` 给每个还没有页签内筛选器的内容页签自动补一张 `scope='tab'` 卡片——这样「切到不同页签就是相对独立的」。回调里 `global.__AI_REPORT_FILTERS__` 是块 id → `{scope, filters}` 的运行时筛选登记表，作用范围一旦在编辑态改动即写回 `block.scope` 并 `syncManifest`。

## 汇报模式与 PPTX 导出

顶栏按钮分布固定为「? 帮助 - 保存与分享 -（编辑工具，仅编辑态）- 汇报模式 - 编辑模式」，阅读模式下 ? 左侧常驻提示「编辑后需保存才生效！」；「汇报模式」入口在顶栏；它把卡片装入**行对齐的 16:9 分页**：每页顶部预留 1/7 高的标题带（`titleRows`），卡片只能落在内容区，某视觉行放不下就整体顺延到下一页的内容区（永不劈卡）。

**汇报布局独立存储（与单页正文互不影响）**：真源是 `manifest.presentation.layout = {blockId: {page, slot, x, w, h}}`（page/slot 是与宽度无关的行坐标；`slot` = 页内内容行偏移）。首次进入汇报模式时按「每页留标题 + 装不下顺延下一页」**种子化**；之后在汇报编辑里的拖拽/插入只改这份 layout——`editor-runtime.syncLayouts` 在 `AIReportPresentation.isActive()` 时改走 `captureLayout`，再由 `placeItems` 规范化（保留用户行偏移，保证不落进标题带、不与已放置卡重叠、放不下顺延下一页；`applyLayout` 也走同一函数）。退出汇报模式时用进入前的快照把 grid 还原为单页布局，`block.layout` 始终只描述单页正文。校验：快照/下载（`buildGridBodySnapshot`/`buildDownloadBlob`/`buildReadonlyHtml`）前会经 `withPageLayout` 临时还原单页布局，避免把每页标题空行当成正文布局写进 bodyHtml/singlefile。顶部一行说明「汇报布局独立于单页」，并提供「从单页重置」重新种子化。

**几何（单一真源，编辑/放映/导出共用）**：`pageGeometry(gridW)` 用真实 GridStack `cellHeight = 48`（`CELL_PX`）取**整数行**的页高（`pageRows = round(pageW*9/16/48)`，页高 = pageRows×48，≈16:9），标题带 = `round(页高/7/48)` 行（`titleH`），内容行 = 页行 − 标题行。卡片可见高 = `h*48 - 16`（上下各 8px margin）。**卡片坐标系原点取 `cardsGrid()`（卡片所在的那个 `.grid-stack`）**——tabs 报告里外层 `.grid-stack` 还包着 tabbar + 内层 grid，用外层原点会让整页框偏一个 tabbar 高。因此 deck（`buildDeckDOM`，页高用行对齐值、卡片用 `%` 定位）、编辑态定位框（`drawSlideFrames`/`paginate`）、放映（`computePlayFrames`/`layoutPlayFrame`）逐页像素级一致。

deck 固定建在**卡片所在 grid 的当前宽度**上并把 `container.style.width` 设为该宽，卡片全用 `%` 定位，因此卡片框与克隆内容同宽、随浏览器缩放自然同步变大变小——不再用 CSS `zoom` 反向补偿（那会把 deck 钉在视口尺寸上，导致只有顶部编辑条随缩放、内容不变）。填入 deck 的图表由 `chartImgFor` 导出为静态图：优先 `getInstanceByDom(...).getDataURL()`，若 live 实例尚未完成渲染或被页签隐藏（0 尺寸 canvas 抓出坏图），就用现有 `option` 在一个临时挂到 `body` 的离屏容器里重渲一次再取图。汇报态窗口/浏览器缩放改变宽度时按 120ms 节流重建 deck（`rebuildForFit`，仅当宽度变化>1px；编辑态/放映态跳过）。左上角「放映」进入浏览器全屏，隐藏编辑用的定位框并把当前页按视口 contain 缩放铺满，支持方向键 / 空格 / 点击翻页。

PPTX 导出是**按需生成**，普通 `report_build` 不生成文件。导出入口在「保存与分享」中心（2026-09-17 起不再占顶栏按钮；中心提供 PPTX 图片版 / PPTX 原生版 / 导出 PDF 三项，汇报模式下中心不提供「分享」——汇报分页与单报告正文各自独立存储但共用同一个分享链接）；导出 PDF 从单报告模式发起会先自动进入汇报模式再生成打印页。它提供两个版本：图片版把文本卡片写成可编辑文本框、图表/KPI/表格渲染为图片；原生版把文本、表格和柱/线/饼图写成 OOXML 原生可编辑元素，不支持原生映射的图表自动降级为图片。导出复用汇报模式分页器，保证屏幕预览与 PPT 分页一致。

- **筛选卡绑定「维度」而非「表」**：多数据源时筛选卡可筛任意可筛维度（含跨表 `dimensionKey`），维度标签标注承载表（`名称（表A × 表B）`）；跨表维度谓词写 `{dimensionKey, op, value}`，作用到各承载表时沿对齐映射到对应列，单表维度退化为普通列筛。

## 编辑器面板排版约束（必须遵守）

编辑器的右侧「图表设置」面板是固定的两级标题体系 + 分区，改动时必须保持层级分明、间距紧凑，
不得让各块顶部留大段空白，也不得让子标题与主标题同字号：

- **一级“块标题”**（图表类型 / 数据设置 / 显示设置 / 表格设置）：`14px / 700 / #1a1a1a`，
  每个块顶部一条 `border-top:#d9d4cc` 发丝分隔线（与上一块分界），标题下留适度呼吸空间。
- **二级“子标题”**（排序 / 图表标题 / Y 轴设置）：`12px / 700 / #787671`，比一级明显更小更浅、
  不加发丝线，与一级左缘对齐，仅靠字重/字号/颜色表达层级；不能用后代选择器放大一级样式到子标题。
- **间距**：`.ai-report-fieldset{margin:14px 0 0; padding:12px 0 0}`，
  `legend{padding:0 0 8px}`，`label{margin:10px 0 4px}`；任何改动不得用大 margin/padding 撑出成片空白。
- **标题层级必须用直接子代选择器**：`.ai-report-fieldset.ai-report-section>legend`，否则嵌套的子
  fieldset legend 会被一并放大（层级塌陷）。
- **小数位数按指标（`data.decimals[measure]`）**：小数位设置在每一个指标 chip 内（`chipDecimals`），
  不在显示设置里放全局小数位；`measureFormat` 优先读 `data.decimals[measure]`，再回退到类型默认精度。
- **排序字段/排序方式并排**：`.ai-report-sort-row{display:grid;grid-template-columns:1fr 1fr}`，
  不要把两个下拉上下堆叠浪费纵向空间。
- **图表标题不设面板输入**：标题直接点击图表上方 `.block-title`（`data-editable-text` 内联编辑），
  侧边栏不再放「图表标题」输入框；`chart.title` 仅镜像当前 DOM 标题用于下载持久化。
- **Y 轴设置不重复**：左/右 Y 轴由每个指标 chip 的「Y 轴」控制；侧边栏 Y 轴设置只放「轴名称」
  （`左轴名称`/`右轴名称` 并排），不放 共用/分开 模式或 左/右位置 下拉（避免与 chip 重复、显得乱）。
- **数据表格能力**：`table` 视图支持列宽拖拽（按 `data-col` 名称定位 `<col>`，修复了序号列导致的偏移）、
  双击列头重置列宽、点击表头排序（`data-sort-col` + ▲/▼ 指示）、显示/隐藏合计行（`table.showTotal`）、
  冻结首列（序号/维度列 `position:sticky`，序号列宽作为维度列 `--freeze-offset`）。
- **大图（焦点）视图居中拉伸**：`body.ai-report-focus .grid-stack-item.ai-report-focus-target>.grid-stack-item-content>.block`
  用 `max-width:1080px; margin:0 auto` 居中和限制最大宽度，避免图表在很宽的舞台上被拉成又扁又挤的形状；
  高度仍占满可用空间。进入焦点态后需在 CSS 过渡（.28s）结束后再 `resizeCharts()` 两次，
  否则 ECharts 在过渡中量到 0 高度导致空白。
- **散点图坐标轴默认数据拟合**：`scatter` 两条值轴走 Flint `zeroDecision`（`mark='point'`），
  远离 0 时 `scale:true` 数据拟合，禁止 0 锚定导致长尾散点浮在大片空白里（前后端必须一致，
  见 `references/chart-semantics.md` 与 `test_wysiwyg` / `test_measure_types` 回归）。

## 确定性门禁

- request/table/manifest 均有严格 schema 校验。
- 编译器自动生成固定布局、稳定图表 id、排序、时间分桶、measureTypes 格式化、表格高度。
- 不允许函数字符串 formatter、外部脚本/样式/字体/图片/数据；运行资产全部内联。
- server manifest 不含原始 rows；图表切换经受限 API 重聚合，带防竞态渲染序号。
- 多报告隔离：dataset id 用 `{reportId}__{datasetId}` 命名空间（如 `sales__primary`），不共用 `primary`。

## 测试与验收

测试套件不在本 skill 目录内（skill 目录只保留运行所需文件）；它在开发仓库根部的 `tests/run_tests.py`：

```cmd
python tests/run_tests.py   # 在开发仓库根目录执行
```

确定性命门组（`run_tests.py` 全绿为发布门槛）：`contract`（请求/预设/筛选白名单、模式判定、dataset 命名空间）、
`template-geometry`（模板几何/KPI/筛选/按需区块）、`validate-rules`（R2 表格高度、R3 排序、漏斗单调性）、
`fm-coverage`（失败模式绑定检查）、`kpi-derived`（派生 KPI/售罄率等）、`semantics-intent`（Flint intent 编译）、
`flint-parity`（Python↔JS Flint 原理一致）、`wysiwyg`（静态 option 与运行时 option 一致，含 breakdown 交叉表、散点轴）、
`server-mode`（server 壳 token 注入一致、`columnRoles` 可省略）、`runtime-contract`（图表目录、序列契约）、
`measure-types`（measureTypes 格式化、散点值轴、矩阵重聚合、筛选语义一致性）。

浏览器审计（发布/变更时必跑）：

```cmd
node scripts\\audit_editor.mjs <report.html|server url>
```

浏览器 E2E：历史脚本（`bi_controls_e2e`、`bi_feature_matrix_e2e`、`formatters_e2e`、`text_editing_e2e`）已退役归档（开发仓库 `archived/tests/`，不随技能分发）；现行浏览器回归 = `audit_editor.mjs` 的预设/检查名（`--preset` / `--checks`，覆盖绘制真值、控件、表格、交互），文本焦点抖动等场景仍建议用真实 Chrome 人工验证。

> **需要浏览器/Node**：`run_tests.py` 无 Node 时跳过 JS 测试并 WARN；浏览器审计无 Chrome 时结果为 `needs_audit`，不伪装成 `ready`。

## 参考资料

- 请求字段：`references/report-request.schema.json`
- 请求词汇功能映射表（业务问法→声明键→值域→缺省，写请求前对照）：`references/authoring-feature-map.md`
- 多源数据宇宙与对齐语义（已实现）：`references/multi-source-alignment.md`
- 数据源配置（三段式：表列表 + 表与关系 + 字段配置）：`references/data-source-canvas-design.md`
- 图表语义与白名单：`references/chart-semantics.md`、`references/chart-presets.json`
- 官方 ECharts 语义：`references/echarts-semantics.md`（dataset transform = TOP K + 筛选机制）
- CLI 与状态：`references/cli.md`
- server API：`references/server-api.md`
- 数据策略与上限：`references/data-strategy.md`
- manifest 运行时契约：`references/manifest-schema.md`

先读 profile，再写 request；不要手工编辑编译后的 HTML。用户明确指定的标题、角色、样式和图表优先于 profile 推断，但仍须落在白名单及门禁内。
