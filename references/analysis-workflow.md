# v1 分析工作流

这份文档规定 Agent 在固定模板中需要做的最少分析工作。Agent 不生成 HTML、CSS、GridStack 坐标、ECharts option 或 SQL。

## 1. 读取事实画像

先运行 `profile_table.py`，查看行列数、类型、缺失率、基数、数值范围、时间覆盖和样例。值级错误已经被静默转为 `null`，结构性错误会在校验阶段阻止编译。

## 2. 选择语义

结合用户目标和 profile：

- 为每列选择 `time`、`dimension`、`measure`、`id` 或 `ignore`；
- 为指标标注 `currency`、`percent`、`count`、`duration`、`number` 或 `unitless`；
- 从 `chart-presets.json` 选择 1–6 个 preset，并填写维度、指标、聚合、排序和必要的第二维度；
- 只写最多 4 个 KPI、最多 3 条洞察。数字必须来自 `facts` 或 profile，不得凭空手算。

用户明确指定的角色、过滤器、标题和 preset 优先于模型推荐；仍需通过 v1 校验和数据形状门禁。

## 3. 固定编译

`spec_to_report.py` 确定性生成 `standard-bi-v1`：封面、筛选摘要、KPI、图表和洞察。编辑模式可按需插入明细组件；编译器同时输出 `data-quality.md`。编译器负责排序、时间分桶、数值格式化、表格高度和稳定 ID。

## 4. 复核

运行 request/table/manifest 校验、lint，再按环境运行浏览器审计。没有 Node/Chrome 时状态为 `needs_audit`，不是数据报告编译失败；交付前应在可用环境补跑审计。
