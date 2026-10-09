# Local server API (v1)

由 `scripts/run_server.py` 启动，默认 `http://127.0.0.1:8765`。浏览器使用 `/api/v1` 前缀；服务端同时接受 `/api` 作为内部别名。报告页面为 `/reports/{reportId}`。

## 响应与安全

成功响应为 JSON，错误为 `{ "error": {"code": "...", "message": "..."} }`。列名和报告/dataset ID 区分大小写。服务默认只绑定 localhost；不提供公网鉴权。所有 SQL 通过参数和引用标识符生成，不接受任意 SQL、路径或表达式。

## 端点

`GET /api/v1/health` 返回 server、SQLite、registry、报告数和 dataset 数。

`GET /api/v1/reports` 列出已注册报告；`GET /api/v1/reports/{reportId}` 返回 schema-only manifest；`GET /reports/{reportId}` 返回固定模板 HTML。

`GET /api/v1/datasets` 列出 dataset。`GET /api/v1/datasets/{datasetId}/schema` 返回列名、推断类型、基数和 rowCount，不返回 rows。

`POST /api/v1/datasets/{datasetId}/aggregate`：

```json
{
  "groupBy": ["部门"],
  "measures": ["PV", "收益"],
  "aggregations": {"PV":"sum", "收益":"average"},
  "filter": {"all":[{"column":"年份","op":"gte","value":2024}],"any":[]},
  "orderBy": [{"column":"PV","direction":"desc"}],
  "limit": 500,
  "offset": 0
}
```

`groupBy` 最多 2 列，`measures` 最多 3 列，过滤条件最多 10 个（一个 OR 组），`limit` 默认 500、最大 5,000，`offset` 用于分页。聚合仅允许 `sum`、`average`、`count`、`min`、`max`。响应含 `columns`、`rows`、`aggregates`、`truncated`、`groupCount` 和耗时信息。

`POST /api/v1/datasets/{datasetId}/rows` 返回只读明细：`page` 默认 1、`pageSize` 默认 50、最大 100；支持同样的 filter 和白名单 orderBy，并返回 `total`、`hasNext`。v1 不在浏览器暴露全量明细。

`POST /api/datasets/{datasetId}/values/{column}`（运行时内部路由，serve_report 挂载为 `/api/...`）：**有界**的过滤后去重值域，级联筛选子列选项源。body `{"filter":[谓词], "limit":N}`，谓词与 aggregate 的 filter 同语义（复用 `aggregator._filter_clause`，返回值自带 `WHERE ` 前缀）；`limit` 服务端钳制（默认 200、上限 500），空 filter = 无约束。**与 GET 同路径路由的区别**：`GET .../values/{column}` 无 filter、无 LIMIT（整列值域），仅供旧路径兼容——级联场景禁止复用它，高基数列会回传数万条。

管理员端点（由 `serve_report.py` 调用）：

- `POST /api/admin/registry/register`：注册 manifest + source table；每个报告的数据复制进 data-dir 并使用 `{reportId}__{datasetId}` 命名空间。
- `POST /api/admin/registry/unregister`：注销报告并在无其他引用时卸载 SQLite 表。
- `POST /api/admin/reload-registry`、`POST /api/reports/{reportId}/reload`：重新载入已登记源文件。

## 限制与生命周期

单个 server 报告最多 5,000,000 行、200 列；超过应在注册前预聚合或拆分。SQLite 文件位于 `--data-dir`，registry 位于 `--registry`；server 退出后文件可复用，浏览器页面必须在 server 运行时打开。
