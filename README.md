# Live Report Master

把一张二维表（Excel / CSV / JSON）变成一份**可编辑的 BI 报告**。

> 📖 **产品手册（图文 + 全流程实例）**：https://eeasonn.github.io/live-report-master/

这是一个 Agent Skill：模型只负责「看懂数据、做分析选择」（列角色、图表选型、KPI、洞察），其余一切——布局求解、ECharts option 生成、SQL 聚合、打包、审计——全部由确定性脚本完成。

## 工作方式

```
二维表 → profile_table.py（确定性事实层）
       → Agent 写 report_request.json（语义层，白名单校验）
       → validate_request → compile_spec → 布局/聚合/渲染 → validate_report
       → 单文件 HTML / 本地服务端报告（GridStack + ECharts，编辑器可拖拽改卡）
```

- 模型不写 HTML/CSS、GridStack 坐标、ECharts option 或 SQL。
- 请求协议见 `references/report-request.schema.json`，产物契约见 `references/report-manifest.schema.json`。
- 运行零 pip 依赖（Python ≥3.9 标准库 + 内置 vendor 的 ECharts/GridStack）。

## 目录结构

| 路径 | 内容 |
|---|---|
| `SKILL.md` | 技能主契约：请求协议、图表白名单、口径纪律 |
| `scripts/` | 事实层 / 校验 / 编译 / 布局 / 打包（确定性脚本） |
| `server/` | 本地报告服务端（SQLite/DuckDB 聚合、注册表、环回 HTTP） |
| `assets/runtime/` | 浏览器端编辑器运行时（GridStack 卡片、ECharts 适配、汇报模式、PPTX 导出） |
| `references/` | 请求/产物 schema、图表预设、写作特性映射 |

## 安装

将本目录作为 Agent 技能安装（如放到 `.pi/skills/` / `.claude/skills/` 下），随后对 Agent 说「用 live-report-master 把这张表做成报告」即可；也可以直接运行：

```bash
python3 scripts/report_build.py --table your_data.csv --spec report_request.json
```

详细用法与请求字段说明见 [SKILL.md](SKILL.md)。
