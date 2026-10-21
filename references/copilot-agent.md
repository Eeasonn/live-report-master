# Server-mode Report Copilot（第一条纵切）

Copilot 是报告服务器上的第二个产品入口，不替代 `live-report-master` Skill：Skill 负责确定性构建，Copilot 负责报告打开后的持续问答与受限编辑。

## 当前闭环

- 报告页面提供常驻 Copilot 侧栏；设置入口允许配置模型 ID、OpenAI-compatible API 地址、API Key 和 Agent 模式（快速 / 平衡 / 深度）。
- Copilot 上下文提供“交互卡片”选择器：可按卡片标题、页签、维度或指标搜索报告内的图表/表格，选中后自动切换页签、滚动定位并高亮目标卡片；后续问答和修改提案都绑定该卡片。
- 切换交互卡片时，旧卡片的预览会先恢复，未应用的旧提案会清除，避免把一张卡片的修改误应用到另一张卡片。
- 模型请求只在服务器端发起。`copilot-model.json` 位于 registry 同级、权限为 `0600`，浏览器只能看到是否已配置，不能看到 Key。
- Provider 可以是远程 HTTPS 服务（如 DeepSeek），也可以是本机 Ollama/llama.cpp 等 OpenAI-compatible 端点；本机端点的同步调用与 `/models` 发现会强制绕过系统代理，避免死代理导致“本机模型不可连接”，远程 HTTPS 仍遵循正常代理策略。
- 首版 Copilot 对话仍只允许真实服务端聚合问答或当前选中图表的受限修改方案；Agent 计划另有独立的 typed plan 校验/执行通道，执行器只自动运行只读工具。
- 模型不接收原始行、SQL、编辑口令或完整 manifest；服务端校验字段、聚合、分桶、TOP-N 后才执行查询。
- 修改先返回 proposal，浏览器“确认应用”只改当前会话中的图表语义；永久保存仍走既有保存入口，原始数据不变。
- 数据问答 evidence 同时带回数据集、维度、指标、聚合、分桶、筛选数量、数据截至和截断标记，前端以可折叠证据卡展示，避免只显示模型自然语言结论。
- `recommend_actions` 由服务端先执行同一真实聚合，再按观测序列、异常候选、日历缺口和截断事实生成有界的只读下一步工具意图；每个建议带 evidence 引用、目标 tool schema、限制和 `causalClaim=false`，不把模型 prose 当作行动或因果结论。
- 数据集级 `derived.measures` 也属于 Agent 可理解的指标宇宙。对窗口、公式、占比和筛选型指标，服务端先展开 `required_aggregations`，再执行一次真实聚合，最后调用同一 `derived_eval.evaluate`；绝不把虚拟指标名当作 SQL 列，也不让模型直接生成数值。
- 长耗时模型调用使用 `async:true` 返回 `jobId`，通过 `GET /api/reports/:reportId/copilot/jobs/:jobId` 轮询；侧栏“停止等待”会调用取消端点，关闭可取消的 provider socket、丢弃晚到结果并回收任务记录。每项 Copilot 后台任务还有 120 秒硬超时，超时会主动取消 provider 连接并显示可重试文案。Job 元数据保存在 state home 的 SQLite 中，可跨进程轮询/取消；owner 进程退出后 running 任务转为 `lost`，不自动重放；若任务 payload 明确声明 `safe-read`/`safe-export`，任务托盘可携带 retry token 调用 `/recover` 显式重新排队，数据更新只有全量 `idempotent-replace` 可恢复，append/upsert 保持 fail-closed。客户端每次异步 turn 携带 `requestId`，代理重试会复用原 Job，不重复调用模型；worker 还会持续写 heartbeat，便于诊断长任务。
- Copilot 后台模型任务按进程最多并发 4 项；终态记录最多保留 40 条且超过 10 分钟自动清理，避免多卡并行或迟到轮询把 provider socket、线程和结果记录无限堆积。达到上限返回 `429` 和 `Retry-After`，不是静默排队；轮询响应带 `pollAfterMs`，客户端按服务端建议退避，避免多卡同时轮询打穿状态库。
- 多轮上下文按 `chartId` 隔离：浏览器只发送当前卡片历史，服务端再次过滤并在过滤后取最近 6 轮；切卡后的旧回答仍可见，但不会成为新卡片的模型上下文。每条失败消息的重试按钮按自身索引找回同卡片原问题，必要时先切回原卡片，不使用全局最后一条问题。浏览器展示历史最多保留 80 条；异步结果绑定 pending 消息对象和当前卡片 `contextKey` 而非数组下标，`contextKey` 还包含有界的 `datasetRevision={dataRevision,rowCount,dataAsOf}`，即使同大小替换也能让数据刷新后的旧请求 fail-closed；卡片语义在等待期间变化时只显示陈旧结果，不应用旧提案；迟到的 `202` 会立即取消服务端 job。

## 语义依据

实现不自行发明可视化口径：

- 字段到通道的语义解析依据 `refs/flint/src/core/resolve-semantics.ts`；
- 类别排序与时间轴不可按数值乱序依据 `refs/flint/src/core/encoding-actions.ts`；
- TOP-N 是在布局前筛选类别成员，且需要产生截断事实依据 `refs/flint/src/core/filter-overflow.ts`。

因此 breakdown 的 TOP-N 与主维度 `topK` 分开建模：先按聚合指标选成员，再过滤二维结果，避免直接对时间×成员行做 LIMIT。

## API

- `GET /api/copilot/config`：返回非敏感模型状态；
- `POST /api/copilot/config`：仅服务器本机同源请求可写配置；
- `GET /api/copilot/provider-models`：仅服务器本机同源请求读取服务商 bounded `/models` 目录；只返回合法模型 ID，不返回 API Key，也不把未知模型自动 alias 化；
- `POST /api/copilot/test`：服务器端测试模型连接；
- `GET /api/copilot/tools`：返回非敏感的 Agent tool catalog，供 UI/诊断读取；工具执行仍由服务端权限和 plan 校验控制；
- `POST /api/reports/:reportId/copilot/plan/validate`：只校验 typed plan、依赖、权限和审批要求，不执行任何工具或副作用；
- `POST /api/reports/:reportId/copilot/plan/execute`：以共享 Job 执行已规范化的只读计划；每一步返回状态、证据和 provenance。proposal、写入、导出和通知工具在 `needsApproval` 停止，不会因模型输出自动产生副作用；响应使用同一 `/copilot/jobs/:jobId` 轮询/取消接口；
- `POST /api/reports/:reportId/copilot/proposals/validate`：对图表 proposal 做服务端字段/数据集校验并返回稳定 `proposalId`、完整 before/after diff 和审批标记；只读，不写 manifest。真正的 apply/rollback transaction 见下一条；浏览器本地预览本身不算已发布。
- `POST /api/reports/:reportId/copilot/proposals/apply` / `.../rollback`：编辑口令保护的服务端 ProposalTransaction，支持 1–8 张卡同一 snapshot、optimistic manifest hash、原子落盘和冲突拒绝；回滚不会覆盖之后的新编辑。
- ProposalTransaction 在 apply 前返回并校验 `transaction.datasetIds` / `sourceCount` / `engineRevision`；跨数据源卡片仍只做 manifest 原子提交，不会把未加载数据源静默混入事务。
- `GET /api/admin/index-suggestions` 与 `/explain` 只读；`POST .../proposals/preview|proposals`、`.../:proposalId/approve|apply` 是本机、双审批、hash-fenced 的 SQLite 单列索引流程，DuckDB 明确 fail-closed，服务端不会自动建索引。
- `POST /api/reports/:reportId/copilot/evidence/export` + `GET .../evidence/:exportId`：大证据走 Job，支持 JSON/CSV/Markdown、20MB 上限和 10 分钟 TTL。
- `GET/POST/DELETE /api/reports/:reportId/copilot/monitors`：只读 anomaly/compare/quality/profile 监控定义，统一 Job scheduler；通知默认未启用。
- `GET /api/reports/:reportId/copilot/audit`：读取脱敏的 Agent action trail；actor 当前为请求 subject 的 hash，真实外部 IdP 身份映射仍待接入。
- `POST /api/reports/:reportId/copilot`：同源报告对话，返回 `answer`、`evidence` 或 `proposal`。
- `POST /api/reports/:reportId/copilot/jobs/:jobId/recover`：仅接受带 retry token 且 payload 声明安全恢复策略的 `lost` 任务；任务托盘显示安全重试入口，未知副作用返回拒绝。
- `GET/POST /api/reports/:reportId/datasets/:datasetId/replace/jobs/:jobId`、`.../cancel`、`.../recover`：数据更新任务的轮询、协作式取消和仅限全量幂等替换的显式恢复；append/upsert 丢失后不自动重放。
- `/api/health` 附带不含结果内容的 Job 生命周期计数（`running/done/error/cancelled/lost`），用于管理页和运维诊断。

计划执行边界：规范化计划包含 `version`、`budget`、`acceptanceTests`、依赖和终止条件；预算受服务端上限约束。`requestId` 绑定输入 hash，重试同一输入才复用 Job，复用到不同输入会返回 `409 RequestConflict`。Job 的 terminal/heartbeat/phase 写回受 owner token fencing 保护，进程恢复后的 `lost` 不会被旧 worker 改回成功。证据 envelope 区分 `observed` 与 `derived`，并列出 assumptions、limitations 和 Flint semantic reference；这不是模型自报的置信度。

当前报告页面上的 Copilot 对话可被局域网访问，但配置写入仍限本机；修改应用是会话内操作，保存权限仍由报告编辑策略控制。后续应把“报告访问者”和“报告作者”身份从 manifest 编辑口令中独立出来，再开放远程写操作。
