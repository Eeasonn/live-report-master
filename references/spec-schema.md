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
- `charts` 最多 6 个；`presetId` 及 encoding 见 `chart-presets.json`。允许的聚合为 `sum`、`average`、`count`、`min`、`max`。
- `filters` 使用受限 DSL；server 端只执行 AND 和一个 OR 组，不接受 SQL/JS 表达式。
- `style` 只能使用预设 token；没有背景色字段。
- `runtime.mode`：`auto`、`singlefile`、`server`；`singlefileMaxRows` 上限 100,000；`serverUrl` 只允许 localhost/宿主明确提供的地址。

编译器会把请求正规化为固定模板：缺失的 dimension/measure 会从 columnRoles 做确定性回退；图表布局、表格高度、排序和首屏 option 由脚本生成，不由模型控制。
