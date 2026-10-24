# Manifest schema (runtime v3)

manifest 是编译器和浏览器运行时之间的内部契约。Agent 不直接编写它；权威 JSON schema 在 `report-manifest.schema.json`。

## 顶层

```json
{
  "schemaVersion": 3,
  "reportId": "sales",
  "title": "销售分析",
  "mode": "singlefile",
  "serverBase": "/api/v1",
  "cover": {}, "kpis": [], "insights": [], "filters": [],
  "blocks": [], "charts": [], "datasets": [],
  "source": {}, "quality": {}, "provenance": {}
}
```

`mode` 只有 `singlefile` 或 `server`。singlefile 的 dataset 可以含 `rows`；server dataset 必须只有 schema（`columns`、`rowCount`），不能泄漏原始行。server 首屏 option 可以含编译期聚合结果，但用户切字段时通过 `/api/v1` 重新请求。

`quality` 是开放对象（additionalProperties: true），除数据质量外还携带两类构建期勾稽产物：

- `quality.anchors`：对账锚点勾稽记录（声明值 vs 实算值，编译期硬闸通过后落账）；
- `quality.facts` / `quality.factsSkipped`：构建期 `profile.facts` 六类确定性事实
  （`{id, kind, statement, evidence, columns}`，id 形如 `fact:<类>:<序号>`）与无法判定类的
  跳过原因——decision_ledger「数据事实（脚本计算）」节与 lint（L21 发散依据）从这里读。

chart entry 可携带可选 `question`（NA-02 分析主张/问题一句话，作者经 `chart.question` 声明）；
insights 条目是已完成占位替换与证据脚注拼装的字符串（脚注行以「依据（脚本计算）：」开头）。

## dataset 与 chart

singlefile dataset：

```json
{"id":"primary","columns":["地区","金额"],"rows":[["华东",12.5]]}
```

server dataset：

```json
{"id":"sales__primary","columns":[{"name":"地区","type":"dimension"},{"name":"金额","type":"measure"}],"rowCount":110000}
```

可编辑 chart 至少包含 `id`、`blockId`、`elementId`、白名单 `viewType`、声明式 `option` 和：

```json
"editor": {"editable": true, "data": {
  "datasetId": "sales__primary",
  "dimension": "地区",
  "measures": ["金额"],
  "aggregation": "sum",
  "measureTypes": {"金额": "currency"},
  "filter": []
}}
```

允许的 viewType：`bar`、`stackedBar`、`line`、`area`、`scatter`、`pie`、`donut`、`funnel`、`table`。内部 `original` 仅由运行时保留 AI 初始快照，不是请求 preset，也不扩展图表白名单。

## 运行时不变量

- `elementId` 必须指向唯一 DOM 元素；每个 chart block 必须有稳定 block ID。
- option 只能是 JSON；不得包含函数字符串 formatter、外部资源或被删除的 series 类型。
- `editor.data` 的维度/指标来自 dataset 列；带副维度的图（如 `bar.stacked`）用 `roles.breakdown` 作为第二维度。
- `measureTypes` 只控制显示格式，不修改源值；缺失值保持 `null`。
- 聚合与明细区块由固定模板生成，server 明细通过分页 rows API 获取。
