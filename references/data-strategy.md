# Data strategy (v1)

## 先做 profile

`profile_table.py` 读取完整输入并输出 canonical JSON、列类型、基数、缺失率、数值统计、Top 值和 masked sample。profile 是事实来源；模型只在此基础上选择主题、角色、图表和文案。

## 模式阈值

- singlefile：≤100,000 rows、≤50 MB canonical JSON、最高 category/time 基数 ≤2,000。浏览器会持有全部 rows，因此不应把未聚合的百万级明细强行塞入 HTML。
- server：≤5,000,000 rows、≤200 columns。原始数据进 SQLite 文件，浏览器只持有 schema 和受限聚合/分页结果。
- 超过 server 上限：拒绝并要求预聚合、拆分或另行建设数据服务。v1 不自动安装 DuckDB，也不静默采样改变结论。

## 图表数据

编译器优先引用 raw dataset，保证切换 `sum`/`average`/`min`/`max` 有实际意义。时间维度只支持 month/week/day/year 分桶；Top-N、派生列和明细表必须在请求中声明并记录口径。单个图表聚合结果最多 5,000 组，前端默认显示 20–100 行。

## 缺失、错误和质量

区分 `null`、空字符串和数字 0。值级错误单元格规范化为 `null`，profile.quality 记录列、计数和样例；结构性错误直接失败。缺失率、排除规则、分桶和 Top-N 必须写入 `provenance` 或质量区块。不要把缺失值当作零，也不要使用因果措辞描述纯观察数据。
