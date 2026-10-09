# 数据源画布（关系中心）设计

> 目标：把「数据源配置 + 跨表关系」提为一等公民的可视化画布；让**筛选器与图表绑定「对
> 齐维度」而非「单张表」**，从而在有关系的数据源之间自然跨表，而不是被卡在「一张表」。
> **落地状态：里程碑 1–3 已实现**（见 §8 结尾），此文件作为设计蓝图与本设计原则的权威记录。

---

## 0. 背景与痛点

当前 skill 已经**有**跨表能力，但都藏在「单表」心智后面：

- **图表**：`crossTable`（如 `cross-month` 图）能把 sales.Sell In 与 inventory.Inventory
  画在同一条时间轴上，按共享 `dimensionKey`（月份）对齐。
- **筛选器**：`dimensionKey` 筛（如 月份）会沿关系映射到 sales.月份 与 inventory.月份，
  跨表图两边都被正确过滤。
- **跨表 KPI**：库存周转 = Sell Out ÷ Inventory 已支持。

但用户感知「卡在单表」，根因有三个：

1. **关系不可见**：`关系` 只是 manifest 里的一段文字列表，不是画布上可见的连线；用户在
   配置层「看不见、摸不着」哪些表能连、按哪列连。
2. **筛选卡绑表**：筛选器卡片上的「数据源」下拉只是**提供可筛列**（`datasetId` →
   `filterCols`），谓词本身不带 datasetId；真正生效靠对齐维度。这让「选了某表」的语义含糊。
3. **图表/筛选被「单表」话术框住**：`datasetId` 一词暗示一图/一筛只能用一张表，实则
   对齐维度可跨表。命名与心智错位。

---

## 1. 画布模型：数据源为节点，关系为连线

把「数据配置」画布演进为**数据源地图（schema map）**：

```
┌───────────────┐        same-domain(月份)        ┌───────────────┐
│   sales 销售表  │ ◄──────────────────────────────► │ inventory 库存表│
│  月份 / 产品线 / │        （共享维度，可联）          │ 月份 / 产品线 / │
│  Sell In/Sell Out│                                │  Inventory     │
└──────┬────────┘                                   └────────┬──────┘
       │ referential(产品线)  1:N 富化                        │
       ▼                                                     │
┌───────────────┐                                            │
│ product 产品线 │ ◄──────────────────────────────────────────┘
│ 产品线 / 负责人 │
└───────────────┘
```

**节点（数据源卡）**：每张表一张卡，列出：
- 数据源 id + label（`sales（销售表）`）。
- 每列 + 角色徽标（时间/分类/数值）+ 分桶粒度（时间列）。
- 列数 × 行数、被几张图引用、参与几条关系。

**边（关系连线）**：
- `same-domain`（同域）：两列值域同源、可联；该列自动成为 `dimensionKey`（对齐骨架）。
  画成**实线**，边标签 = 列名（如 月份）。
- `referential`（引用/1:N）：主外键富化。画成**虚线**，边标签 = 列名（如 产品线）。
- 连线端点可拖拽换列；松开时校验（`adapter.validateHierarchy` 同款）能否构成合法关系。

**操作**：点一条边直接编辑/删除该关系（左/右表、左/右列、同域/1:N）。**关系只负责声明对齐，不
产出对象**——跨表图/跨表筛选/跨表 KPI 一律从「插入卡片」入口承载（见 §8.3）。

---

## 2. 核心心智转变：筛选器/图表绑定「维度」，不绑定「表」

这是本设计最关键的一步。把「数据源」从绑定单位，降级为**承载维度/列的容器**。

### 2.1 维度即筛选/图表的锚点

一个**维度**（dimension）可由多张表的同名列承载，形成 `dimensionKey`：

```
dimensionKey "月份" = { sales.月份(month), inventory.月份(month) }
dimensionKey "产品线" = { sales.产品线, inventory.产品线 }
```

- **筛选卡绑定的是一个维度**（如 月份），而不是一张表。筛选卡标题显示
  `月份（销售表 × 库存表）`，让读者一看就懂它跨了哪几张表。
- 筛选谓词 = `{dimensionKey, op, value}`。作用到某张图的某张表时，沿 `dimensionKey`
  映射到该表对应列（现有 `crossTable` 过滤已实现这一点，把它前置为显式语义）。
- 单一维度没跨表（只有一张表承载）时，它就是一个普通 `column` 筛——与今天兼容。

### 2.2 图表生产

- 选一个**对齐维度**当 x 轴（如 月份），再从任意承载它的表拖出指标当系列
  （sales.Sell In、inventory.Inventory、product.… 经 referential 富化的列）。
- 图表卡的数据源徽标不再说「数据源：sales」而是「维度：月份 · 跨表 2 表」，或并列两张表。

### 2.3 筛选与作用域

- 沿用现有的 `global`/`tab` 作用域（已与页签挂钩）。
- 一个 tab 的筛选卡可绑定一个**跨表维度**，从而作用于该 tab 内所有引用该维度的图表
  ——包括跨表图的两张表。这正是「如果选了控制页签和某维度，应正确控制该页签下所有对齐表」。

---

## 3. 画布 UX 结构（整体为全屏 overlay，与现有「数据配置」画布同壳）

```
┌──────────────────────────────────────────────────────────────┐
│ 数据配置：数据源关系（思维导图）          [＋ 新增关系] [保存][取消] │
├──────────────────────────────────────────────────────────────┤
│ ┌──────┐  同域(月份/产品线)   ┌──────────┐                    │
│ │ sales│──────◆───────────────│ inventory │                   │
│ │ 销售表│                       │  库存表   │                   │
│ └──┬───┘  1:N(产品线,虚线)     └──────────┘                    │
│    ▼                                                          │
│ ┌──────┐                                                      │
│ │product│                                                     │
│ │ 产品线 │                                                     │
│ └──────┘                                                      │
├──────────────────────────────────────────────────────────────┤
│ 关系只建/编辑（列、类型、保存、删除）；产出走「插入卡片」：       │
│   图表(数据源多选) · 筛选器(选维度) · 数值(跨表 KPI)             │
└──────────────────────────────────────────────────────────────┘
```

- 思维导图式自动布局：以关系度最高的表为根，同层一列、层内纵向堆叠（1:N 同层多节点）。
- 三色字段卡（时间/分类/数值）沿用现有 `semAutoLayout` 分区；从分类字段卡拖端口建 1:N 层级。
- 关系编辑弹窗只保留「左/右表、左/右列、关系类型、保存、删除」，不再产出跨表对象。

---

## 4. 数据模型（向后兼容）

现有 `relations` + `dimensionKeys` 不变，新增可选 `dimension` 概念作为一等对象：

```json
{
  "dimensions": [
    { "key": "月份", "grain": "month",
      "tables": [ {"datasetId": "sales", "column": "月份", "bucket": "month"},
                  {"datasetId": "inventory", "column": "月份", "bucket": "month"} ] }
  ],
  "charts": [{
    "id": "cross-month",
    "axis": { "dimensionKey": "月份" },
    "series": [
      {"label": "Sell In",  "datasetId": "sales",    "column": "Sell In",  "aggregation": "sum"},
      {"label": "Inventory","datasetId": "inventory","column": "Inventory","aggregation": "sum"}
    ]
  }],
  "filters": [
    {"dimensionKey": "月份", "op": "in", "value": ["2025-01","2025-02","2025-03"]}
  ]
}
```

- `charts[].axis.dimensionKey` = 从「选维度」产出，替代今天手工的 `crossTable.dimensionKey`。
- 筛选卡不再需要 `datasetId`；它绑定 `dimensionKey`（可由单表承载，亦可跨表）。
- **兼容**：旧 `datasetId`/`crossTable`/`encoding` 继续被 `normalize_request` 吸收为
  等价输入，不破坏既有报告。

---

## 5. 筛选器「只锁一张表」的取舍

用户提到「筛选器是否真的只包括这个数据卡片，是否只能绑一张表？」。本设计明确回答：

- **不是一个筛选卡只绑一张表**；筛选卡绑定的是一个**维度**（可跨表）。
- 如果你要「只筛 sales 这张表」，那是一个**单表承载的维度**（如销售表特有的某列），
  它自然只作用于有该列的表。若 sales 与 inventory 共享 月份，则「筛月份」必然跨表——
  这是 same-domain 关系的语义，不该强行锁单表。
- 因此**好消息**：只要画布把「维度」作为绑定单位，用户就不会误以为选了表却管到了别人；
  跨表与否由「该维度由几表承载」决定，语义清晰。

---

## 6. 与现有「数据配置」画布的关系

- 保留现有画布的**字段角色**（时间/分类/数值）、**层级分类**（1:N hierarchy）、**派生指标**
  编辑——这些仍是有价值的字段语义配置。
- 新增的是**数据源节点**与**关系连线**的可见化（思维导图自动布局）；关系只声明对齐，产出走「插入卡片」。
- 最简落地：在现有「数据配置」画布顶部加一个「数据源/关系」区块（点开显示上面的图），
  而不是推翻重来。这样风险最低、WYSIWYG 迁移成本最小。

---

## 7. 约束（沿用现有边界）

- **绝不注入 SQL join**：关系只声明「哪些列同域/主外键」，跨表由脚本按 `dimensionKey`
  批次 merge（现有 `/aggregate-multi` + `crossTableAggregate` 的底座）。
- **声明式对齐**：`same-domain` 才进 `dimensionKeys`；`referential` 只做 1:N 富化，不形成
  对齐骨架（与现有 `_derive_dimension_keys` 一致）。
- **单表回退**：无关系的报告行为与今天完全一致（维度退化为单表列）。

---

## 8. 落地里程碑（建议顺序）

> **状态：M1–M3 已实现**（`editor-runtime.js` + `spec_to_report.py` + `validate_request.py`）。

1. ✅ **画布布局**：把「数据源/关系」作为「数据配置」画布顶部的可见区块，节点 = 数据源卡
   （列 + 角色）、边 = 关系连线 `renderSemDsMap`。只读、可点选（点节点切换该数据源）。
2. ✅ **维度绑定**：筛选卡不再有独立「维度」下拉；添加筛选时列出可筛维度并标注表来源
   （跨表显示 `名称（表A × 表B）`），筛选行标签同样标注来源（`filterDimLabel`/
   `filterDimensionChoices`/`dimensionLabel`）。
3. ✅ **产出走「插入卡片」，画布不产出**：图表在「图表设置 → 数据源」多选同域关联数据源
   （`datasetSelectorField`/`applyChartDatasets`）；筛选器选维度；数值卡片新增「跨表 KPI（比率）」
   （`openCrossKpiPanel`/`addCrossTableKpi`）。请求层一等字段 `charts[].axis.dimensionKey` +
   `series[]` 已由 `validate_request` + `spec_to_report` 吸收。
4. **兼容与回归**：旧 `crossTable`/`datasetId` 输入继续工作；33/33 测试绿。

---

## 9. 风险与回退

- **风险**：把「筛选卡绑维度」改掉现有「数据源」下拉，可能影响老用户习惯。缓解：跨表维度
  显示跨表徽标；单表维度显示 `sales（销售表）` 保持可读。
- **回退**：`dimensions`/`axis.dimensionKey` 是新增可选键；任何时刻去掉它们，退回今天的
  `datasetId`/`crossTable` 行为。

---

## 10. v3 重构：合并入口 + 三段式 + 维度树形（已实现）

> 实现：`assets/runtime/editor-runtime.js`（`openSemanticCanvas` / `renderSemTableList` /
> `renderSemTable` / `semCatTreeOrder` / `semIndentColumn` / `semOutdentColumn` /
> `semMoveCatNode` / `semCanDropCat`）+
> `assets/runtime/editor-runtime.css`（`.ai-dsc-*` / `.ai-sem-*-tree`）。
> Guard：`tests/test_data_source_config.cjs`。

用户决策（面向小白业务）驱动了这次重构：

1. **合并入口**：「数据配置」+「数据源」两个工具栏按钮 → 一个 **「数据源配置」**（全屏）。
   旧 `dataSourcesPanel`（右侧面板）不再有工具栏入口；替换/追加/删除都进侧栏「更多」。
2. **三段式布局**：左侧 = **数据表列表**（主表紫点 / 补充表青点 + `默认` 徽标 +
   `⋯` 菜单：重命名 / 设为默认主表 / 替换数据 / 删除表（带依赖检查，复用 `removeDataset`））；
   右侧 = 视图 tab **表与关系**（平移 `renderSemDsMap` 关系地图 + 新增关联）|
   **字段配置**。单表报告直接进「字段配置」。
3. **维度树形取代层级画布**：字段配置按 **5 个角色**（时间/维度/指标/ID/忽略，`SEM_FIELD_ROLE_OPTIONS`）分组；**维度层级 = 拖拽树**——维度行带
   ⠿ 手柄（`data-cat-grip`，draggable），行三分区落点：上 25%=排到它前面（同级）/
   中间=作为它的子级/下 25%=后面，drop 统一走 `semMoveCatNode`（守卫 `semCanDropCat`
   禁环：目标不得是自己或自己的子孙）。→← 箭头保留为快捷方式（`semIndentColumn`
   = 挂到上一行下的 child 落点，`semOutdentColumn` = 删父边回顶层）。
   层级真源仍是同一份 `dataset.hierarchy` 边列表（1:N 单父，**数据模型零改动**）；拖拽树使层级成为**真树**（一个父列可挂多个子列），下游两件套随之激活：
   **多分支下钻**（图表/下钻表按 `drillPath[].child`/`hierSteps` 显式步骤走树，多子级弹「下钻到」菜单，`tests/test_hierarchy_features.cjs`）与**级联筛选**（筛选器子列选项随上级选中值的共现收窄，server 走有界 `POST /values/:col`）。
   另有一份**展示顺序** `dataset.hierarchyOrder`（扁平 DFS 序列，会话镜像
   `semCanvas.catOrder`）解决边表达不了的「同父兄弟谁在前」——`semCatTreeOrder`
   以它定秩（缺失字段退回列序并写回，幂等），`semWriteSemanticsToDs` 落盘 /
   `cancelSemCanvas` 回滚 / `renameColumnEverywhere` 同步改名；编译侧不读它。
   旧的 SVG 端口连线画布退役（渲染代码保留但不可达）。
   每行另有只读 **类型** 徽标（`dataset.columnTypes` = profile 推断的默认类型，
   `semDefaultTypeLabel`）对照可改的「角色」，覆盖后可见 默认≠当前。
4. **表角色协议**（`references/report-request.schema.json` / `validate_request`）：
   `datasets[].role ∈ {primary, lookup}` + `datasets[].isDefault`（恰好一张主表为默认）。
   默认主表决定**缺省归属**（无 `datasetId` 的图表/KPI 归它，`_normalize_ctx.default`）；
   补充表**免除模式判定的基数门槛**（`choose_mode(lookup=True)`），但行数/字节仍计入
   singlefile。manifest 的 dataset 带 `role`/`isDefault`（`package_report._with_dataset` 保留）。
5. **术语**：保留产品惯用的「维度/指标/时间」；只把关联术语白话化——
   `same-domain` → **并排对比**，`referential` → **补充信息**；引入 `主表/补充表`。

**已完成（#4）**：关联编辑弹窗白话化 + 合并宽表。`openRelationEditor` 重写为
「**补充信息**（referential 左连接）/ **并排对比**（same-domain 外连接）」两张选项卡 +
效果预览 + 「合并成一张表」勾选 + 删除/保存；不再出现「同域维度 / 1:N 富化」抽象术语。
勾选合并并保存 → `materializeRelationWide(idx)` 产出派生数据集 `<左表>__wide`（进侧栏/关系图）。

> 第 6 轮：合并的**列名身份**是「去掉报告命名空间」的 id（`server/relations.py::merge_identity` ↔ JS
> `relMergeId`），所以运行时合并与构建期合并逐列同名；合并产物带 `meta.provenance`（每列 → 来源列），
> 宽表因此继承来源列的 `columnTypes`/`fieldRoles`。**键不预填**，空键保存被拦。宽表未物化时页面顶部
> 给「立即合并」提示并把 新名/隐藏/键模式 置灰（不做点了没反应的死控件）。
- **补充信息 → LEFT JOIN**：右表按键去重后接列，**左表行数不变**，未匹配行保留、补列置空；
- **并排对比 → FULL OUTER**：键并集，两边各自数值，单边有值的另一边置空；
- 键通过 `mergeKeyLabel(v,bucket)` 归一（数字浮点 `.0` 去尾、时间按 bucket 归一），
  列名冲突加表前缀；**server 模式拒绝合并**（需行数据）。
Guard：`tests/test_relation_merge.cjs`（含 LEFT JOIN 行数不变 / FULL OUTER 键并集 / bucket / server guard）。

**仍属增强项**：合并宽表的 `fieldRoles`/`measureTypes` 继承仅按同名列搬运（不推导）；
server 模式的构建期物化（SQL 视图）未做。

---

## 11. 构建期合并宽表（server 也可用）

> 模块：`scripts/merge_tables.py`；集成：`report_build._materialize_spec`（pre-pass）；
> 协议：`relations[].materialize: true`（`validate_request` + request schema）。
> Guard：`tests/test_build_merge.py`。

**动机**：多数据集路径目前只支持 singlefile（server 多表注册仍未做）。若在**构建期**
把 relations 物化成一张单表，报告就退回**普通单表路径** —— singlefile 与 **server** 都自然可用。

**流程**（`report_build` 在 `validate_request` 之前跑 pre-pass）：
1. 请求里任一 relation 标了 `materialize: true` → 逐数据集 `profile_table --canonical-out`；
2. `merge_tables.materialize(tables, relations, base)` 把每条关系折进**默认主表**
   （referential 先折、same-domain 后折：LEFT JOIN / FULL OUTER，与运行时语义严格一致）；
3. 写 `table-<base>__wide.json`，并把请求**改写为单数据集**：删掉 `datasets`/`relations`、
   清掉每张卡片上的 `datasetId`（默认归 `primary`）、`axis/series` 跨表图折叠为 `encoding`、
   把各表 `columnRoles`/`measureTypes` 合并进顶层默认槽；
4. 单表路径照常跑 → `--mode server` 得到 schema-only 的 server 报告。

**边界**：合并只把「左端已在运行表里的键」折进去；锚在别的表上的关系跳过（不误 join）。
跨表图（`axis.dimensionKey`+`series`）会折叠成单表 `encoding`，键列名 = dimensionKey
（`_derive_dimension_keys` 取的正是组内最高频列名）。列名冲突加 `<datasetId>·` 前缀。

**另注：多数据集 server 注册（P1.5）已实现**（`--mode server` 注册每张表 + mixed 路径，
命名空间 `{reportId}__{datasetId}`），所以「不想合并、直接上 server」也可用；
合并宽表只是另一条更简单的心智路径。

---

## 12. 画布可用性补齐（位置持久化 / 重新排布 / 可滚动）

> 实现：`assets/runtime/editor-runtime.js::renderSemDsMap`（工具条 + 松手落盘）+
> `references/report-manifest.schema.json`（`dsMapPos`）+ `editor-runtime.css`（`.ai-sem-dsmap*`）。
> Guard：`tests/test_data_source_config.cjs`（`canvas:` 前缀的断言）。

1. **节点位置随报告保存**：`manifest.dsMapPos = { <datasetId>: {x, y} }`。以前只在
   `semCanvas.dsMapPos`（会话镜像）里，关掉画布重开就回到自动布局 —— 「我排好的图呢」。
   - manifest 根是 `additionalProperties:false`，所以 schema 必须**显式声明** `dsMapPos`；
     不声明会被 `validate_report` 的 jsonschema 校验直接拒（构建失败）。
   - 拖拽**松手才写回 manifest + syncManifest**（拖拽中每帧写会白发几十次渲染）。
   - 位置属于「本次编辑」：`取消 / 放弃本次改动` 会把它一起回滚（`semCanvas.origDsMapPos`）。
   - 未记录的节点仍走自动布局（按关系度选根、按层列排），所以老报告与新建表都不需要初始值。
2. **「重新排布」按钮**：清掉所有手工位置、按关系重算一遍 —— 拖乱之后唯一的出口
   （「自动布局」在 v3 画布里被删掉了，只留下不可达的实现；现在它是显式按钮，不是隐式行为）。
3. **容器纵向可滚**：`.ai-sem-dsmap` 从 `overflow-x:auto` 改成 `overflow:auto` ——
   多表竖排时 `viewBox` 高度会超过容器的 `max-height:480px`，旧规则把下面的表直接裁掉（看不见也点不到）。
4. **节点卡补行数**：`N 行 · N 列（· 补充表）`（`ds.rowCount` 或本地 `rows.length`）。
