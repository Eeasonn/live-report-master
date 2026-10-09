# Chart semantics (v1 whitelist)

模型只需在这些受控 preset 中选择；`scripts/spec_to_report.py` 负责把 preset 映射为确定性的
ECharts option。**模型不写 ECharts option、formatter、HTML、CSS 或脚本**——只能选择
`presetId + encoding`。所有 option 字段都来自官方 ECharts 语义
（见 `references/echarts-semantics.md`），杜绝模型臆造。

| preset | 典型数据形态 | 约束 |
|---|---|---|
| `kpi.metric` | 单个数值指标（无维度） | 最多 4 个；每个一条 KPI 卡 |
| `bar.rank` | 一个分类维度 + 1–3 个指标 | 默认按首指标降序；支持 TOP K |
| `bar.stacked` | 分类维度 + 一个 breakdown + 1–2 个可加指标 | 不用于率/均值等不可加指标；支持 TOP K |
| `line.trend` / `area.trend` | 时间/有序维度 + 1–2 个指标 | 按时间升序；面积图强调累计规模 |
| `scatter.correlation` | 两个数值指标 | 至少 2 个指标；不把分类列当数值轴 |
| `pie.share` / `donut.share` | 一个分类维度 + 一个可加指标 | 切片 ≤8，按值降序 |
| `funnel.stage` | 有序阶段维度 + 一个计数/金额指标 | 阶段顺序必须明确 |
| `table.aggregate` | 任意维度 + 指标 | 聚合证据，默认 ≤20 行 |
| `table.detail` | 原始列 | 只读、分页、默认 50 行、最大 100 行；支持排序列 |

> **v1 已删除 `heatmap.matrix`**（两维矩阵热力图），不再出现在白名单、编译器、校验器、
> lint 或运行时。热力图相关代码路径一并移除，任何遗留引用都应清理。

## 维度、指标和聚合

- `columnRoles` 是模型的显式声明；profile 的类型推断只作为校验和回退。
- `sum` 只用于可加量；率、均值、比例用 `average` 或预计算派生值并在 `provenance` 说明。
- 缺失保持 `null`，**不当作 0**。
- 时间维度支持 month/week/day/year 分桶。
- 所有图表最多 3 个指标；饼/环/漏斗只消费第一个；散点使用两个；表格最多 3 个。
- 一列只能一个角色：`time / dimension / measure / id / ignore`；未声明默认 `ignore`。

## TOP K

`bar.rank` / `bar.stacked` 支持 `limit`（TOP K）。运行时提供 TOP K 控件（5/10/20/50/全部），
变更后走官方 `dataset.transform`（sort+filter，singlefile/Web Worker）或 server 的
`ORDER BY … LIMIT k`（server）。模型只设置 `limit`，不写 option。

## 筛选

报告级筛选器只支持白名单操作 `eq/ne/gt/gte/lt/lte/contains/in/not in/is null/is not null`，
最多 10 个条件，`AND + 一层 OR`；所有组件共享同一份 filter。运行时把 filter 应用到全部
图表、KPI、明细与 facts 的重算（singlefile 用官方 `dataset.transform` filter；server 走
`/aggregate` 的 `filter` 参数）。insight 文本不重新调用模型，并标注「基于初始口径生成」。

## 明确删除

象形柱、箱线、矩形树、旭日、树、主题河流、雷达、仪表盘、地图、关系图、桑基、平行坐标、
蜡烛、`effectScatter`、`custom`/`lines`、**`heatmap`**，以及背景色、任意 CSS、背景图、
外部字体、函数字符串 formatter。不要用其他图伪装这些语义。
