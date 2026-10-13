# 请求词汇 · 功能映射表（业务问法 → 声明键）

写 `report_request.json` 前对照本表。每项功能按 **业务问法（何时会用）→ 声明位置 → 值域 → 缺省行为**
四元组组织（形状对齐 Flint `EncodingActionDef`：枚举值域 + 适用性门控 + 「不声明 = 语义默认」）。

三条使用纪律：

1. **缺省即专业默认**。每个键不声明时都有经过校准的缺省行为；只有业务点名、或数据形态明显需要时才写。
   不要为「功能齐全」堆声明——`style.chartTheme` 的历史事故（随手写 powerbi 深色交付）就是反例。
2. **值域是封闭枚举或既定形状**。非法值构建期一律可行动报错，不静默回退；校验幂等。
3. **意图级词汇优先**。先写 `presetId` / `topK` / `breakdown` / `sections` 这类意图键；
   `chart.table.*` 是表格卡的精调面，按各表「业务问法」列对号入座，不需要全声明。

图表 family 选择（排名→`bar.rank`、趋势→`line.trend`、占比→`pie.share`……）已在 SKILL.md「模型负责什么」一节，
此处不重复。显示层四键（字段别名/值别名/隐藏列/字段说明）是编辑器状态，**不进请求**；月度刷新自动继承。

---

## 1. 报告级

| 业务问法 | 声明 | 值域 | 缺省（不声明时） |
|---|---|---|---|
| 投影仪/大屏要看，页面更宽 | `pageWidth` | `1280` \| `1440` \| `1600` \| `1920`（`"full"`=1920） | `1280` |
| 数据源必须完全独立（不要合并） | `dataSourceMode` | `default` \| `independent`（independent 与 `relations` 互斥，同现报错） | `default`；未强调独立性时也**默认不合并**，某张卡确实需要跨源字段才声明 relation |
| 换一套视觉风格（品牌/媒体风） | `style.chartTheme` | `default` + 10 个 Flint preset + `huawei`；`powerbi`/`pop` 为深底档（仅图表换肤） | `default`（壳+图全套默认皮肤）。**仅用户明确点名才写**（lint L19 对非默认告警） |
| 主色换成品牌色 | `style.accent` | 任意 CSS 颜色 | 主题默认 accent（=图表首系列色） |
| 离线单文件还是本机服务 | `runtime.mode` | `auto` \| `singlefile` \| `server` | `auto`（按行数/字节/基数判定，排除 id/ignore 列） |
| 报告分章节讲故事 | `sections[]` | ≤8 节 `{title, narrative?, charts[], insight?}`；narrative/insight 支持 Markdown 子集 | 全部图表落末尾「正文」节 |
| 给单张卡片写口径/解读 | `chart.annotation` | ≤2000 字，支持 Markdown 子集 | 无 |
| 批注放卡片右侧而不是下方 | `chart.annotationPlacement` | `below` \| `right` | `below`（right = 8+4 侧车行，需恰好一张非 solo 图 + 短洞察） |
| 手工指定某卡大小/位置 | `chart.layout` | `{w,h}` 或 `{x,y,w,h}`（12 列栅格） | 自动行模式排布（solo/wide/small/std + 密度策略） |
| 表格按第 2 个维度交叉（月份做列头等） | `roles.breakdown` | 列名（表格=列区透视列；stackedBar/heatmap=系列拆分/色阶格） | 无。**聚合表格声明了交叉列缺省即渲染「交叉矩阵」**（tableLayoutOf：有交叉列=matrix），不声明=分组行/平表——中国式表格（合并单元格+列头透视）靠它 |
| 交叉表格头再嵌套多层（区域×国家×产品线做列头等） | `chart.breakdowns` | 1–3 项列名数组；与 `chart.breakdown`（单字符串）**互斥**（同现校验拒绝） | 无。产物恒写 `roles.cols` 数组 + `roles.breakdown=cols[0]` 镜像（旧消费点零改动）。**仅表格卡交叉矩阵支持多层**，交叉图表（stackedBar/heatmap）列维维持 1。例：3 行维 × 3 列维 = `dimensions:["区域","国家","产品线"]` + `breakdowns:["月份","渠道","业务组"]` → 嵌套段组表头（外层段头 colspan=内层组合数×指标扩展数，逐层合并；角头 rowspan=表头层数）+ 行 3 维合并格 + d1 段尾小计×每列组合；列键 = 各层值 `\u0001` 连接的组合键（单列维=值本身，colWidths 旧键兼容）；行总计列=行组合跨全部列组合、列总计行=列组合跨全部行（全量过滤行口径，分页只裁显示）；列序每层独立（自定义 sortOrder > 时间日历序 > 行序首现，外层优先分组）；服务端 GROUP BY ≤6 维（3行+3列 恰压线）。运行时列区 destMax 分档：table=3、heatmap/bar/stackedBar=1 |

## 2. 数据集级（`datasets[]` 或顶层默认槽）

| 业务问法 | 声明 | 值域 | 缺省 |
|---|---|---|---|
| 一个数据集来自多个同构文件（补 W35+W36 两周/多月） | `datasets[].file` | 路径字符串 **或 1–8 个路径的数组**（构建期同构并集 UNION：列集一致、顺序无关，类型以合并后全集判定；不参与 relations） | 单文件；报告建好后补数据走「追加新行」 |
| 这列是时间/维度/指标/忽略 | `columnRoles.<col>` | `time` \| `dimension` \| `measure` \| `id` \| `ignore`（id 与 ignore 同行为） | profile 自动推断；只校验未引用列存在 |
| 金额/百分比/计数格式与默认聚合 | `measureTypes.<col>` | `{type: currency\|price\|percent\|count\|duration\|number\|unitless, semantic?, aggregation?, decimals?}` | 类型按 profile 推断（价族名称自动判 price）；聚合按 Flint 聚合角色缺省（additive→sum，**intensive（price/percent/score）→average**）；精度按类型默认 |
| 指定币种（美元/欧元/日元…） | `measureTypes.<col>.currency` | ISO 代码，必须在内置 23 币表内（USD/EUR/GBP/JPY/CNY/KRW/INR/BRL/CAD/AUD/CHF/SEK/NOK/DKK/HKD/TWD/SGD/THB/MYR/PHP/IDR/VND/RUB）；如 `{"type":"currency","currency":"USD","decimals":2}`（`type:"price"` 同样可用） | 缺省 CNY（¥）；CNY/JPY 显式声明时缺省 `divisor:10000, unit:'万'`，其余币原值；未知代码校验拒绝（自定义显示改用 `symbol`，≤4 字符，优先于查表）。币种只在数据源配置（字段配置）声明，图表级不提供覆盖。**price 与 currency 的唯一差别是可加性**：price=Flint Price intensive（不可加、缺省 average），currency=Amount（可加、缺省 sum） |
| 派生指标的语义注解 | `datasets[i].derived.measures[].semantic` | 同上 Flint 受控标签（Rating→Score 别名归一；未知标签编译报错） | 自动（按名称推断，如含「率」→percent）。显式声明接管格式/不可加闸/轴域，不依赖名字（`对客折扣` 也能拿 percent）；数据源配置弹窗「语义」下拉可设 |
| 更细的聚合语义注解（Flint T2） | `measureTypes.<col>.semantic` | Flint 标签：`Price/Revenue/Cost/Amount/Profit/Percentage/Count/Number/Quantity/Duration/Score/Rating/Temperature/Sentiment/Correlation/Rank`…（受控词汇 = flint_core 注册表） | 无。声明后编译按该类型聚合角色执行（如 `semantic:"Price"` → 不可加、缺省 average；`Profit` → signed-additive 可加但发散色）；`type` 继续管格式族，二者正交 |
| 每行算一个数（客单价=额/数） | `derived.columns[]` | ≤8 `{name, op: add\|sub\|mul\|div, left, right}`；时间差 `{op:"datediff", left, right, unit}`（端可为列或 `__today__`） | 无；物化成真列后处处当普通指标 |
| 聚合后算比率/同环比/累计/占比/**筛选聚合** | `derived.measures[]` | ≤16，`kind: formula`（AST）/ `window`（fn: prev\|cum\|ytd\|rank…，必填 timeField+grain）/ `share`（scope: all\|parent）/ `filtered`（agg+col+filters≥1，op: eq/ne/gt/ge/lt/le/between/in，条件聚合 AGG(CASE WHEN…)；count 可省 col） | 无；窗口指标要求引用图表维度==timeField 且分桶==grain，否则编译期可行动报错；filtered 无时间语义，被窗口 ref 引用即得同环比 |
| 省份归并成大区、年龄分段 | `dimGroups[]` | `{name, source, groups:[{label, members[]}], other?}`；未命中归 other（不声明则保留原值） | 无；物化成新维度列，不进计算字段引用宇宙 |
| 区域▸办事处▸国家逐级下钻 | `hierarchy` | 父子边数组（维度行拖 ⠿ 同一模型） | 无；筛选级联/下钻/层级筛选均依赖它 |
| 月度增量按主键合并 | `primaryKey[]` | 1–8 列（须真实存在） | 无（替换数据=全量覆盖） |
| 跨源字段要同图/同报告分析 | `relations[]` | ≤16 条链式 `{tables, keys, mode, fields?, renames?}`；声明 = 两表保留 + 关联键 + 宽表数据集（`<tables[0]>__wide`），数据源关系地图可见、可解散 | 无；`dataSourceMode:"independent"` 时禁止声明 |

## 3. 表格卡 `chart.table`（按意图分组）

### 3.1 布局与表头

| 业务问法 | 键 | 值域 | 缺省 |
|---|---|---|---|
| 明确要「交叉矩阵」或「分组」布局 | `layout` | `grouped` \| `matrix` | **有交叉列=交叉矩阵、无=分组行**（列区落列即透视）；显式声明恒优先。形状路由：1行0列=平表、2行0列=嵌套、1行1列=矩阵、2行1列=3D、≥3维=ND 长表 |
| 同值单元格合并（中国式表格） | `mergeSame` | bool | **开**（对当前排序后行集相邻合并；排序/翻页后重算） |
| 多级行维压成一列 | `mergeRowDims` | `{name?, width?}` | 关（嵌套缩进形态） |
| 行维树形折叠展示 | `treeView` | bool（与 mergeSame 互斥） | 关 |
| 矩阵列头要不要显示指标名行 | `measureNameRow` | bool | **开**（单度量+列维也出两级表头） |
| 行更密/更疏 | `density` | `compact`(32px) \| `standard`(40px) \| `relaxed`(48px) | `standard` |
| 隔行斑马纹 | `striped` | bool | **开** |
| 长文本换行而不是截断 | `wrapText` | bool | 关（截断 + 横滚提示） |
| 表头配色/隐藏列表头 | `headStyle` | `{bg?, fg?, align?: left\|center\|right, hideColHead?}` | 主题表头 |
| 某列水平/垂直对齐 | `align` | `{列名: {h?: left\|center\|right\|auto, v?: top\|middle\|bottom\|auto}}` | auto（文本左、数值右） |

### 3.2 合计与小计

| 业务问法 | 键 | 值域 | 缺省 |
|---|---|---|---|
| 要不要合计行 | `showTotal` | bool | **开**（按全量过滤行统计，分页只裁行不裁口径） |
| 合计行放顶/底 | `totalPos` | `top` \| `bottom` | `bottom` |
| 合计行叫别的名字 | `totalLabel` | 非空 ≤12 字 | `合计` |
| 矩阵要「行总计」列 | `rowTotal` | bool | 关 |
| 分组/嵌套要段尾小计 | `showSubtotal` | bool | 关（树形态自动开） |
| 小计行叫别的名字 | `subtotalLabel` | 非空 ≤12 字 | `小计` |
| 合计不按 sum 算（均值/去重计数…） | `totalCalc` | `{指标列: sum\|average\|count\|countd\|min\|max}`（合计回全量过滤行重算，非二次聚合） | `sum` |

### 3.3 排序、取数与分页

| 业务问法 | 键 | 值域 | 缺省 |
|---|---|---|---|
| 按指定列升降序 | `sortLevels` | `[{col, dir: asc\|desc}]` | 编译期 R3 排序（首指标 desc；时间列日历序） |
| 只显示前 N 行 | `limit` | int 1–100（截断是显示层，合计仍全量；卡底有「共 N 项」徽标） | 不截断 |
| 每个区域内取前 N（组内排名） | `groupTopN` | int 0–100（分区=行区第 1 维，名次=首指标降序） | `0` = 关 |
| 分页单位 | `pageBy` | `group`（组永不跨页） \| `row` | `group` |
| 分页器形态 | `pagerMode` | `full` \| `simple`（跳过总数计算） | `full` |
| 页容量档位上限 | `pageSizeMax` | `10\|20\|30\|50\|80\|100\|200\|500` | `20` |
| 要序号列 | `showRank` | bool | 关 |
| 序号列叫别的名字 | `rankLabel` | 非空 ≤12 字 | `序号` |
| 合计行不显示序号 | `rankHideOnTotal` | bool | 关 |

### 3.4 范围与预置筛选

| 业务问法 | 键 | 值域 | 缺省 |
|---|---|---|---|
| 只看近 N 期 / 某时间段（时间维） | `dateScope` | `{col, mode: recent\|range, n?, unit?: month\|quarter\|year, anchor?: data\|today, min?, max?}` | 全史；anchor 默认 `data`（数据最新期，经营月报口径） |
| 每个维度各限取值范围 | `dimScopes[]` | `{col, kind: time\|values, …time 同上 / values: [...标量]}`（每维一条） | 不限 |
| 预置列筛选（卡级数据范围限制） | `columnFilters` | `{列名: {type: values, values[], not?} \| {type: range, min?, max?}}`（`not:true`=排除；`values` 含 `__blank__` 哨兵=空白行） | 无 |

> 筛选双层模型（2026-10-10 收敛）：`columnFilters`/`dimScopes`/声明 `filters` 是**配置级数据范围限制**——对整卡生效、读者不可改；被它们排除的值，读者筛选器（报告级筛选卡 / 卡头「筛选 ▾」常驻条，多选）的选项里**不再出现**。表格工具条不再提供筛选入口（AND 面板已退役）。

### 3.5 单元格强化

| 业务问法 | 键 | 值域 | 缺省 |
|---|---|---|---|
| 数值列内嵌数据条 | `showBars` | bool（相对列最大值；有目标走目标条） | **开** |
| 条件格式（红黄绿/加粗/图标…） | `condFormat[]` | `{target, basis: 固定值\|百分比区间\|动态字段\|与0\|与均值, op?, value?, source?, actions{text,bg,bold,italic,icon,scale,bar,hideText,fullRow}, affectTotals?, enabled?}` | 无 |
| 行尾迷你趋势列 | `sparkline` | `{enabled, label?}`（列区=时间维时可用） | 关 |
| 指标旁带同比/环比副列 | `subMeasures` | `[{measure, offsetN≥1, grain: year\|quarter\|month\|week\|day, show: diff\|pct\|value, pos: "right"}]` | 无 |

### 3.6 高度与交互

| 业务问法 | 键 | 值域 | 缺省 |
|---|---|---|---|
| 表格限高内滚动（不撑开版面） | `heightMode` | `grow` \| `fixed` \| `{bounded:{maxPx: 200–4000}}` | `grow`（按行数自适应，封顶 R2 容量） |
| 点击聚合行跳明细 | `rowDrill` | bool（需启用明细视图） | **开** |
| 关掉卡头「导出」/改导出名/加水印 | `export` | `{enabled?, name?≤40, watermark?≤20}` | 开；无水印 |

## 4. KPI 与筛选

| 业务问法 | 声明 | 值域 | 缺省 |
|---|---|---|---|
| 顶部指标卡 | `kpis[]` | ≤4 `{type:"kpi.metric", label?, measure?, agg?, factType?, dimension?}`；去重计数 `factType:"distinctCount"`；**不要硬编码 value**（筛选后不重算） | 编译期按 facts 自动补足 4 张 |
| KPI 带同比/环比副行 | `kpi.compareMeasure` | 引用一个**窗口派生指标**（数据源层声明） | 无对比 |
| 读者可交互筛选 | `filters[]` | ≤10 条，op 白名单 `eq/ne/gt/gte/lt/lte/contains/in/not in/is null/is not null/between/daterange`；`AND + 一层 OR` | 未声明时自动从维度列填充（≤6）；**不带 value = 交互控件无预选（全部）**，是正常写法 |

## 5. 何时该主动声明（判断启发）

- 业务原话出现「**只看/近 N 期/最近一个季度**」→ `dateScope` 或 `dimScopes`。
- 出现「**排名/前 N/Top**」且载体是表格 → `groupTopN`；载体是柱图 → `topK`。
- 出现「**完成率/达标/异常标红**」→ `condFormat`（阈值让业务给，缺省与均值/与0 不要凭空造阈值）。
- 出现「**合计不对，应该是平均/去重**」→ `totalCalc`。
- 数据有**比率列**（利润率/增长率）→ 考虑 `condFormat` 百分比区间 + 不要把这些列当可加指标堆进合计。
- 数据有**单价/售价列**（价格、售价、unit price）→ 声明 `type:"price"`（或依赖价族名称自动推断——构建与编辑器已同源）：缺省聚合自动变 average、不可 sum/堆叠。**跨品类视角不要用均价**——分品类切片、价格带/中位数、折扣率（lint L20 会提示品类列在场但未分组的情况）。
- 数据有**百分比列** → 先看存值表示：0–1 比率（表格与图表显示自动 ×100，轴模板 `{value}%` 直显 15% 而非 0.15%）还是 0–100 百分数（直显，`type:'percent'` 的设计本意）；表示检测自动化（Flint §3.4，profile 带 `percentRepresentation`），不再需要手工豁免。
- 堆叠图要**占比堆叠**（每根柱各段归一到 100%）→ `encoding.stackMode:'normalize'`（Flint resolveStackable 的 normalize 形态）：percent 类指标进堆叠的唯一合法路径（缺省 sum 堆叠对 percent 仍是编译错误）。
- 堆叠方式编辑器可切换：堆叠柱卡的「堆叠方式」select（绝对值/占比 100%）读写 `editor.data.stackMode`，换走堆叠视图自动删键。
- **发散色上屏**（2026-10-13 收尾批）：柱状图指标语义发散（`Profit/PercentageChange/Sentiment/Correlation/Temperature` 注解，或窗口 rate 派生天然 PercentageChange）且数据真有负值 → 负段 `#dc2626`、正段系列色（PY 静态 + JS 运行时同闸）。
- **轴域语义**（同批）：`semantic:'Score'`（值全在 0–100）→ 值轴钉 0–100；`semantic:'Rank'` → 轴反转（第 1 名在顶）。作者显式 min/max 恒优先。
- **逐指标聚合闸**：price/percent/unitless 及 share/window.rate 派生的指标芯片下拉不再提供「求和」；旧报告残留 sum 读取时归一 average 落盘自愈（服务端信任 manifest，错误请求在编辑器侧不再产生）。
- 数据有**利润/情感/相关性这类正负列** → 数据跨零时 lint L21 会建议条件格式（与0 basis 红负绿正）——发散型指标正负同色读不出方向。
- 绑定指标到维度前自问「**该口径在此切片上是否仍衡量同一对象**」（intensive 指标的可比性审查，见 SKILL.md 模型负责第 5 条）。
- 业务给了**编码值**要求中文显示 → 显示层值别名（编辑器配，请求层不收；见文首说明）。
- **多级维度 + 多指标 + 时间列** → 默认 3D 交叉表已合适；除非业务点名平表/树形，不要乱写 `layout`。
- 提示词点名的每一个**非基础度量**（比率/均值/占比/同环比/累计/店均/去重计数，如 WOS=Stock/Sales）→ `derived.measures` / `derived.columns` 各一条显式声明；交付前逐个对账，不许静默降级成「只有基础 SUM」。
- 提示词明说「**筛选/过滤/只看 X**」的维度值条件 → 区分两层：给**读者**的可交互筛选 → `filters`（报告级筛选卡）；**卡内数据范围限制**（作者定死、读者不可改）→ `chart.table.columnFilters` / `dimScopes`；禁止改道维度/breakdown/交叉列「顺便」实现。
- 提示词出现「**数据源独立/不要合并/各自来源**」→ `dataSourceMode:"independent"` 且不声明 `relations`（互斥词汇，同现报错）；未强调时默认不合并，某张卡确实需要跨源字段才声明 relation。
