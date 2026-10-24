# report_request.json v1

请求协议是模型和确定性编译器之间唯一的输入接口；请以 `report-request.schema.json` 为准，并运行 `validate_request.py`。旧版 `spec.json` 不属于本 skill 的输入。

最小结构：

```json
{
  "schemaVersion": 1,
  "templateId": "standard-bi-v1",
  "title": "报告标题",
  "columnRoles": {"日期":"time", "部门":"dimension", "金额":"measure"},
  "measureTypes": {"金额":"currency"},
  "charts": [{"id":"trend", "presetId":"line.trend", "dimension":"日期", "measures":["金额"], "aggregation":"sum"}]
}
```

## 字段

- `reportId` 只允许以字母开头、长度 2–80 的字母数字下划线连字符。
- `columnRoles` 可省略（脚本按 profile 推断）；你标记的列被尊重，`id`/`ignore` 列保留在明细表但不参与图表。
- `facts` 是要由脚本计算的事实请求（`total`、`count`、`distinctCount`、`topN`、`shareOfTotal`、`trend`、`growthRate`、`rankChange`、`missingRate`、`min`、`max`、`average`）。
- `kpis` 最多 4 个，`insights` 最多 3 个。数值应引用 facts 或 profile，不要手算并覆盖事实。
- `insights` 条目可以是字符串，或 `{text, facts:["fact:…"]}` 对象（证据绑定）：`facts` 引用构建期
  `profile.facts` 的确定性事实 id（形如 `fact:granularity:1`，构建日志 `[fact]` 行逐条可核），
  渲染为洞察后的「依据（脚本计算）」脚注；**引用不存在的 id 构建失败**（防幻觉闸，报错会列出可用 id）。
- `chart.question`（每张图可选）：这张卡要回答的分析主张/问题，一句话 ≤200 字。lint 只提醒
  「有图无主张」（advisory），不查对错——主张的证据请用上面的 `insights[].facts` 绑定。
- `charts` 最多 6 个；`presetId` 及 encoding 见 `chart-presets.json`。允许的聚合为 `sum`、`average`、`count`、`min`、`max`。
- `filters` 使用受限 DSL；server 端只执行 AND 和一个 OR 组，不接受 SQL/JS 表达式。
- `style` 只能使用预设 token；没有背景色字段。
- `runtime.mode`：`auto`、`singlefile`、`server`；`singlefileMaxRows` 上限 100,000；`serverUrl` 只允许 localhost/宿主明确提供的地址。
- `datasets[].privacy.sensitiveColumns`（敏感列声明）：字符串数组（1–64 项、去重、每项必须命中
  本表真实列名，compile 期对真实表校验）。语义 = **构建期物理剥离，不可恢复**：编译器把敏感列从
  数据集 `rows`/`columns` 剔除（singlefile 内联 rows、mixed 内联表、server/mixed 表声明一视同仁），
  `columnStats`/`profile.facts` 的证据引用同步剪除；manifest 保留 `privacy` 声明本身，供 server
  Agent 链（agent_context / agent_tool_runner 的脱敏投影）按列名消费。**引用即构建失败**：任何
  chart（dimension/measures/breakdown/表格筛选与范围）、KPI（含 derived.rate 两臂）、报告筛选、
  facts、对账锚点、计算列/派生指标、维度值分组、层级边、primaryKey、relations 合并键引用敏感列，
  都会在编译期得到指明槽位与列名的可行动报错——从引用中移除该列，或把它移出敏感列声明后重建。
  **作者自由输入不在闸内**（XS-02）：洞察/`texts`/`chart.annotation`/`chart.question`/封面摘要/KPI 文案等
  手写文本由作者负责；构建期用剥离前的敏感列取值做有界子串扫描（每列 ≤5,000 个去重值、值长 ≥4），
  命中 → `[privacy] WARN`（槽位 + 列名 + 命中个数，不回显值）+ `manifest.quality.privacyAuthorWarnings`
  + decisions.md「隐私提示」节；不改变构建结果与 exit code。

## 构建期 profile.facts（确定性事实层）

`profile_table.py` 在构建期从源数据确定性抽取六类事实（每条 `{id, kind, statement, evidence, columns}`，
总数 ≤24，id 形如 `fact:<类>:<序号>`）：

| kind | 内容 |
| --- | --- |
| `granularity` | 最小唯一键组合（≤3 列）；找不到也给「最接近组合」的诚实陈述 |
| `top_mover` | 最近一期对上一期的环比极值成员（主维度 × 主指标；无时间列则跳过并说明） |
| `concentration` | 主要维度上的 CR3 与 HHI（按主指标；intensive 语义自动用均值） |
| `calendar_gap` | 时间列的缺失期（复用 date_parse 粒度；连续时给「无缺失期」的正面事实） |
| `correlation` | 数值列两两 Pearson top3（陈述恒带「相关非因果」） |
| `sign` | 跨零（正负混合）数值列 → 发散配色候选（lint L21 的事实依据） |

无法判定的类返回跳过原因（profile 的 `factsSkipped`），不编造。写洞察时：先读 facts（构建日志
`[fact]` 行 / `profile.json`），主张句只陈述 facts 支持的内容，并用 `insights[].facts` 绑定 id；
`{{fact:id}}` 占位（free 档）与 `facts:[]` 绑定共用同一解析与校验。

编译器会把请求正规化为固定模板：缺失的 dimension/measure 会从 columnRoles 做确定性回退；图表布局、表格高度、排序和首屏 option 由脚本生成，不由模型控制。
