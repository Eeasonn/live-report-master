# 军规

这些是 `report_request.json`/runtime 的强制规则。request、table 和 manifest 校验失败即停止打包；lint 和浏览器审计补充可读性与交互检查。

1. **请求唯一入口**：只接受 `schemaVersion: 1`、`templateId: standard-bi-v1`、可选的 `columnRoles`（未标记列由脚本按 profile 推断）和 1–6 个白名单 preset；旧版 spec/manifest 不迁移。
2. **固定模板**：只能生成封面、筛选、KPI、图表和洞察区块；明细只能通过编辑模式的按需组件插入，质量报告输出为 `data-quality.md`。模型不得写 HTML/CSS、GridStack 坐标或 ECharts option。
3. **图表白名单**：`bar.rank`、`bar.stacked`、`line.trend`、`area.trend`、`scatter.correlation`、`pie.share`、`donut.share`、`funnel.stage`、`table.aggregate`、`table.detail`。象形柱、箱线、矩形树、旭日、树、主题河流、雷达、仪表盘、地图、关系图、桑基、平行坐标、蜡烛图、热力图、effectScatter、custom/lines 全部删除。
4. **结构完整**：首行表头唯一非空；数据是致密矩形二维表；值级错误转 `null` 并记录质量；结构错误直接失败。
5. **角色与口径**：维度/指标必须来自输入列；指标聚合仅 `sum|average|count|min|max`；比例/率禁止 sum 和 stackedBar；散点至少两个指标。
6. **排序确定**：比较图默认主要指标降序，时间趋势按时间升序；只有 `sort: time|category-asc` 等显式语义才能改变默认。
7. **格式安全**：只使用六种 `measureTypes`；不允许函数字符串、箭头函数、任意 JS、外链资源或任意 CSS。
8. **数据边界**：singlefile ≤100,000 行/50MB/单维基数≤2,000；server ≤5,000,000 行/200 列；超限先转 server 或预聚合，不能静默截断事实。
9. **服务安全**：server 仅绑定 localhost，SQLite 文件落在指定 workdir；API 只提供 health、register、schema、受限 aggregate 和分页 rows，不提供任意 SQL/路径。
10. **状态诚实**：`ready` 表示门禁全过；`needs_audit` 只表示环境缺 Node/Chrome 或明确跳过审计；`failed` 才是报告无效。
