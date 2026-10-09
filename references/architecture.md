# Architecture (v1)

## 设计原则

这是一个 Windows-first、固定模板、确定性编译的报告 skill。Agent 只提交 `report_request.json` 的分析决策；Python 脚本负责输入读取、质量处理、模式选择、HTML/manifest 生成、SQLite 注册和门禁。v1 不兼容旧 skill 的 spec/manifest，也不自动切换 DuckDB。

## 数据流

```text
Excel / CSV / JSON
        │
        ▼
table_input → validate_table → profile_table
        │                       │
        │                       └─ profile/facts（给 Agent 阅读）
        ▼
report_request.json（角色、事实、图表 preset、文案）
        │
        ▼
spec_to_report（固定 standard-bi-v1 骨架）
        │
   ┌────┴────┐
   │         │
singlefile  server
   │         │
内嵌 rows   SQLite 文件 + schema-only manifest
   │         │
report.html 浏览器 fetch /api/v1 聚合与分页明细
```

## 固定报告骨架

顺序和最大数量固定：封面、筛选摘要、0–4 个 KPI、1–6 个图表、0–3 条洞察。明细为编辑模式按需插入的组件；质量报告写入 `data-quality.md`，来源与口径保留在 manifest/provenance。模板只接受预设 token，不接受任意 CSS 或背景色。

## 模式边界

自动模式使用以下硬门槛：singlefile ≤100,000 行、canonical JSON ≤50 MB、最高 category/time 基数 ≤2,000；server ≤5,000,000 行且 ≤200 列。显式选择 singlefile 超限即失败，显式 server 则跳过 singlefile 门槛但仍检查 server 上限。超过 server 上限要求用户预聚合或拆分。

singlefile 把规范表行内嵌，适合离线分发。server 将原始表导入每个报告的 SQLite 文件，开启 WAL 并为常用列建立索引；浏览器只收到列 schema、首屏聚合和受限查询结果。每个报告使用命名空间 dataset，避免多报告相互覆盖。

## Windows 启动

`bootstrap_windows.cmd` 查找 `py`/`python`/随包运行时，必要时允许 Agent 通过 cmd 安装 Python。`start_server.cmd`、`open_report.cmd`、`stop_server.cmd` 不依赖 WSL 或 bash，默认只绑定 localhost。Node/Chrome 仅用于可选浏览器审计，不是编译和 server 的依赖。

## 安全边界

- HTTP server 默认 `127.0.0.1`，不提供鉴权或公网部署承诺。
- API 使用列名白名单和参数化 SQL；禁止任意 SQL、任意文件路径、任意表达式。
- 过滤器、分组、度量、排序、分页和导出（若启用）均有数量上限。
- 原始文件只在注册时读取并复制到明确的数据目录；报告 manifest 不暴露本机路径。

## 可观测状态

pipeline 输出 `build-status.json`：`ready`、`needs_audit`、`failed`。profile 的 `quality` 会进入报告质量区块。server 的 `/api/health` 报告 SQLite、registry 和数据目录状态。
