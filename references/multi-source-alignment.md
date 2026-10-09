# 多源数据宇宙与对齐语义（Multi-Source Alignment）设计

> 状态：**P0–P6 已实现**（`relations` 协议+校验+`dimensionKeys`；共享维度筛选级联；跨表对齐聚合同图；跨表比率 KPI；语义画布多数据源切换+对齐面板；singlefile 运行时「新增数据源」追加；server `/api/aggregate-multi` 批聚合端点）。server 模式的运行时追加表需重登记（后续）。本文定义「系统性多表支持」的协议、校验规则、聚合语义与两条落地路径（server / singlefile），作为实现与测试的唯一权威。
> 落地原则不变：**模型只给方向判断（声明表角色、对齐关系、故事编排），脚本负责物化（校验、跨表聚合、筛选级联、打包）**。

## 1. 目标

把平台从「单二维表 → 一张报告」升级为「多源数据宇宙 → 一套数据故事」，系统性覆盖三种输入场景：

1. **构建期传入多表**（`datasets[]`，现已有，但无关系）；
2. **基于已生成报告，运行时追加/替换表**（离线、脱离 Agent）；
3. **关系谱系全覆盖**：多表**完全可关联**（共享维度键）、**部分关联**（部分字段可对齐）、**毫无物理关系**（仅同一故事，各自成图）。

## 2. 现状盘点（grounded）

| 能力 | 现状 | 权威位置 |
|---|---|---|
| 多表注册 | `datasets[]` ≤8，`additionalProperties:false`，仅 `{id,label,file,columnRoles,measureTypes,hierarchy}`，**无关系字段** | `references/report-request.schema.json` `/properties/datasets` |
| 图表/KPI 归属 | `chart.datasetId` / `kpi.datasetId`，缺省第一张表 | `references/report-request.schema.json` `$defs/chart/properties/datasetId`；`scripts/spec_to_report.py:1159` |
| 报告级筛选按表裁剪 | `_filters_for_table(filters, columns)` 按「列是否在该表」投影；leaf 已支持 `datasetId` | `scripts/spec_to_report.py:2726`；`:2743-2744` |
| 聚合 API | **单表**：`POST /api/v1/datasets/:id/aggregate`，`groupBy`≤2、`measures`≤3 | `server/aggregator.py:aggregate`；`server/app.py:147` |
| 本地聚合 | `adapter.aggregate(data, dimension, measures, method)` 只对单一 `data.rows` 分组 | `assets/runtime/chart-adapter.js:962` |
| server 端拉取 | `fetchAggregate(data,…)` 单一 `data.datasetId` | `assets/runtime/chart-adapter.js:62` |
| 语义画布 | `openSemanticCanvas` 只编辑 `manifest.datasets[0]` | `assets/runtime/editor-runtime.js:2973-2974` |
| 运行时换数据 | `更新数据` 严格同 schema（`sameColumnLayout` 列名+类型全等） | `assets/runtime/editor-runtime.js:4793` |
| 数据集命名空间 | server 端 `{reportId}__{datasetId}`，每表独立 SQLite 表 | `scripts/serve_report.py`（`_compiled_manifest`） |
| 维度层级 | 单表内 1:N `hierarchy` 边（父子） | `references/report-manifest.schema.json`；语义画布「层级分类」 |

结论：**「放多张表」已具备，但每张表是孤岛**。缺的是「表之间的**对齐**」这一层：没有共同维度解析、没有跨表筛选级联、没有跨表聚合。

## 3. 核心概念

### 3.1 数据宇宙（Data Universe）
一个报告 = 一个数据宇宙：`tables[]`（数据源）+ `relations[]`（对齐关系）+ 每张表的语义（`columnRoles`/`measureTypes`/`hierarchy`）。语义不再"挂在单表里各自为政"，而是**宇宙级**：同一逻辑维度在宇宙里只有一个身份。

### 3.2 对齐（Alignment）—— 关系的唯一表达
不引入 SQL JOIN 语法，而是**声明式对齐边**。两条：

```jsonc
// report_request.json 顶层（或 datasets 同级）新增
"relations": [
  { "id": "rel-ym",
    "kind": "same-domain",                       // 同一维度（值域/粒度兼容）
    "left":  {"datasetId":"sales",   "column":"YearMonth", "bucket":"month"},
    "right": {"datasetId":"stock",   "column":"YearMonth", "bucket":"month"} },
  { "id": "rel-code",
    "kind": "referential",                       // 1:N 富化（lookup：code → label）
    "left":  {"datasetId":"sales",        "column":"ProductCode"},
    "right": {"datasetId":"product_lookup","column":"Code"} }
]
```

- `same-domain`：两列表达**同一个维度**，用同一套 label（同一 `bucket`）。用途：跨表筛选级联、跨表同图聚合、跨表占比分母、TOP-K 并集。
- `referential`：右表是左表列的 **1:N 富化/查表**（parent→child 或 code→label）。用途：事实表加标签/属性。
- 对齐边是**声明**，不是执行物化；校验与聚合时才展开。模型声明，脚本校验/物化。

### 3.3 维度键（dimension key）与对齐组
`same-domain` 边构成无向图，每个连通分量的 `(table, column)` 集合称为一个**对齐组**。组有一个稳定的**维度键**（默认取组内出现最多次的列名，或显式 `dimensionKey`）。

从此，图表/KPI/筛选可以引用：
- 普通列（现状，向后兼容）；
- 或**维度键**（新）：引擎把它展开为组内每张表各自的 `(column, bucket)`。

## 4. 协议扩展

### 4.1 request（`report_request.json`）
- `datasets[].relations` **去掉 `additionalProperties:false`**，新增：
  - `datasets[].relations[]`（或顶层 `relations[]`，二者等价、归一化后合并）。
- `relations[]` 元素 schema：
  ```json
  {
    "id": "string ^[A-Za-z][A-Za-z0-9_-]{1,79}$",
    "kind": "enum[same-domain, referential]",
    "left":  {"datasetId":"string","column":"string","bucket":"enum[raw,month,week,day,year]?"},
    "right": {"datasetId":"string","column":"string","bucket":"enum[...]?"}
  }
  ```
- 图表 `encoding.measures[]` 支持跨源：
  ```jsonc
  "measures": [
    {"column":"Sell In","aggregation":"sum","datasetId":"sales"},
    {"column":"Sell Out","aggregation":"sum","datasetId":"stock"}
  ]
  ```
  `dimension` 允许传 `dimensionKey`（引用对齐组）。
- KPI 支持跨源：`derived` 的 `left`/`right` 可带 `datasetId`（见 §6.3）。
- 报告级 `filters` 的 leaf 允许 `dimensionKey`（替代/等价于 `column`，见 §6.1）。

### 4.2 manifest（编译产物）
- `manifest.relations` = 归一化后的对齐边数组（`kind/left/right`，含每端 `datasetId/column/bucket` + 解析后的 `dimensionKey`）。
- `manifest.dimensionKeys` = `{ key: {tables:[{datasetId,column,bucket}], granularity} }`（由 `relations` 推导，供运行时快速展开）。
- 图表 `editor.data` 的 `measures[]` 元素允许带 `datasetId`（缺省 = 该图 `data.datasetId`）。

## 5. 校验规则（脚本，编译期 + 运行时一致）

1. **引用合法性**：`datasetId`/`column` 必须存在于对应表；`dimensionKey` 必须存在于 `dimensionKeys`。
2. **同域可对齐**（`same-domain` 通过条件）：
   - 两端角色相同（都是 `time` 或都是 `dimension`，不能一端 measure 一端 dimension）；
   - 若为 time：`bucket` 归一化后必须一致（否则拒绝）；
   - 值域**重叠度抽样校验**（编译期抽样，server 端用 SQL 抽样）：无交集且非空交集 → 警告（advisory，不阻断）。
3. **无环**：`referential` 边 + `hierarchy` 边并集不得成环（`same-domain` 边无方向，但也要检测 1:N 富化链不环）。
4. **粒度降级不反向**：`referential` 富化只能是「键列 → 查表键列」（等值），不允许「细粒度 → 粗粒度」在 referential 里跨层（那是 hierarchy 的职责）。
5. **上限**：`relations` ≤ 表数×(表数−1) 但有硬上限（如 ≤64 条）；单表 `columnRoles` 等上限沿用现有契约。
6. **合并无损**：跨表聚合里，若某 measure 声明 `sum` 但另一端表**没有**该维度键列，则该 measure 不出现在合并结果（等价现状的按列投影）。

## 6. 聚合与过滤语义（两条路径必须一致）

### 6.1 共享维度筛选级联
一条报告筛选若引用 `dimensionKey D`：
1. 展开为组内每张表 Ti 的 `(column_i, bucket_i)`；
2. 对 Ti 生成 Ti 侧谓词 `{column: column_i, op, value}`（值用**归一化 label** 比较，与 `bucketLabel` 一致）；
3. 没有暴露 D 的表**不受影响**（等价 `_filters_for_table` 的按列投影）。

### 6.2 跨表同图聚合（aligned aggregation）
给定：共享维度键 `D`、维度粒度 `bucket`、measures `{Ti, col_i, agg_i}`：
1. **分表聚合**：对每张 Ti 调 `aggregate(Ti, column_i, [该表 measures…], bucket_i)`（server 走现有 `/datasets/:id/aggregate`；singlefile 走本地 `aggregate`）。
2. **归一化 label**：每表结果按 `bucketLabel(column_i, bucket)` 生成**同一套** label（`month`→`YYYY-MM` 等，与 §AGENTS.md 时间分桶一致）。
3. **外连接合并**：以所有表的 label 并集为行轴，每表 measure 对齐到各自行；缺失 label 的 measure 值：
   - additive（sum/count/min/max/average）→ 按现有 `zeroBaseline` 决策补 0 或 null；
   - `none`/非聚合 → null。
4. **排序/TOP-K**：合并后按主 measure 降序（复用现有 R3 排序），截断 cap（server 每表 ≤500 → 合并后 ≤2000 兜底）。
5. **truth**：合并后的 KPI/图 still 可被 `validate_report` 用每表 overall aggregates 复算。

实现选择（记录在案）：
- **MVP**：客户端（`chart-adapter.js`）并行 N 次单表聚合，再本地合并（server 无新端点，singlefile 纯本地）。
- **优化**：server 新增 `POST /api/v1/aggregate-multi`（一次性收多表请求，SQL 侧各聚合后合并），减少往返。

### 6.3 跨表 KPI / 比率
- 复用 `derived`：`left`/`right` 允许带 `datasetId`。
- 例：`售罄率 = SellOut(sales) / SellIn(sales)`（同源）或 `库存周转 = Inventory(stock) / SellOut(sales)`（跨源）。
- server 端仍**只聚合真实列**（跨源比率在客户端用两侧 overall aggregate 相除），与现有派生原则一致。

## 7. 运行时（编辑器）改动

1. **语义画布 → 宇宙画布**：
   - 顶部加「数据源」切换条（选择要配置角色/层级的表）；
   - 新增「对齐」视图：把各表列拖出端口，画 `same-domain` / `referential` 边（复用现有 hierarchy 边 UX）；
   - `semCommitLive` 把 `relations` + 每表语义一起写回 `manifest`（不再是只写 `datasets[0]`）。
2. **新增数据源（append）**：
   - 「更新数据」新增「追加为新数据源」分支（不再强制同 schema 替换）：
     - singlefile：新表 rows 内联（受总行数/字节/基数上限约束，超限提示转 server）；
     - server：新表注册为新 dataset（`{reportId}__{newId}`），复用 `register`/`reload`。
   - 追加后自动跑**对齐检测**（同名/同角色/同值域），给出建议边，用户确认。
   - 既有图表不受影响（仍绑旧表），新表可被新图表选用。
3. **跨表图表组建**：`ensureEditorData` 与 `adapter.optionFor` 支持 measures 跨源 + `dimensionKey` 展开（见 §6.2）。
4. **每卡数据源徽标**：单源徽标已有；跨源 measure 的卡片显示「来源：A + B」多徽标。

## 8. 数据故事与模板
- 节（`sections[]`）可混排来自不同表的图表（现状已支持，因为图按 `datasetId` 归属）——故事线天然跨表。
- 跨表 KPI 条让概览层讲「进销存/库存」这类跨源故事。
- 非纯数据/文本输入：走**文本卡**路径（`component`/`paragraphs`），不在本节展开（另立 `text-inputs.md`）。

## 9. 安全与边界
- 无任意 SQL / 无任意 join 注入：`relations` 只表达「列 ↔ 列」等值对齐，展开为受限单表聚合。
- 每表独立 caps（现有 `groupBy`≤2、`measures`≤3、limit≤20000），合并层新增「结果行 ≤ 2000、对齐组 ≤ 32」兜底。
- 关系数、对齐组规模、抽样校验成本均有硬上限。

## 10. 迁移与向后兼容
- 无 `relations` 的报告 = 现状（每表孤岛），行为不变。
- `measures[]` 不带 `datasetId` = 现状（默认该图 dataset）。
- `filters` 不带 `dimensionKey` = 现状（按列投影）。
- 旧 manifest 无 `relations`/`dimensionKeys` 时，运行时按「无对齐」处理，不报错。

## 11. 分阶段落地

| 阶段 | 内容 | 量级 |
|---|---|---|
| P0 协议+校验 | `relations` schema、`dimensionKeys` 推导、校验规则 | 低 |
| P1 筛选级联 | `dimensionKey` 筛选 → 展开到对齐组各表（server + 本地） | 中 |
| P2 跨表聚合 | 跨源 measures 同图 + 客户端合并 + truth | **高** |
| P3 跨表 KPI | `derived` 跨源比率 | 中 |
| P4 宇宙画布 | 语义画布多数据源 + 对齐边 | 高 |
| P5 运行时追加表 | 「新增数据源」+ 对齐检测 | 中 |
| P6 优化 | server `aggregate-multi` 端点 | 中 |

**不空谈的 MVP = P0 + P1 + P2 的一个最小可见结果**：至少一张跨表图 + 一条级联筛选，证明「共同维度」真正串起多表。

## 12. 待确认决策点
1. `relations` 放顶层还是 `datasets[]` 内（倾向：归一化后统一存顶层 `manifest.relations`，request 两者都接受）。
2. 对齐组 label 归一化：是否强制所有 `same-domain` 端声明同一 `bucket`（倾向强制，简单可靠）。
3. 跨表合并缺失值：additive 补 0 还是 null（倾向沿用 Flint `zeroBaseline` 决策）。
4. MVP 的跨表聚合用「客户端 N 次聚合合并」还是直接上 server `aggregate-multi`（倾向客户端，先不做新端点）。
5. `referential` 富化首版范围：仅「等值查表补标签」，不做逐行 join 物化。
