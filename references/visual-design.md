# v1 视觉约束

视觉由 `standard-bi-v1` 固定模板和运行时完成，Agent 只选择命名 style token，不写任意 CSS 或 ECharts option。

- 封面使用固定蓝色渐变；不提供用户背景色编辑。
- `style.accent` 只能选择 `blue|teal|orange|purple`；`density`、`radius`、`fontScale` 只能选择 schema 中的预设。
- 图表保持简洁的轴、图例和 tooltip；图表类型只能来自 `chart-presets.json`。
- 条形图和构成图默认按主要指标降序；时间趋势按时间升序；热力图必须是完整的二维交叉单元。
- 固定模板为响应式 12 列布局；表格和封面由门禁检查可见高度，窄屏不允许文字溢出。
- 颜色用于强调系列或 KPI，不承载未声明的业务含义；洞察中的因果表述必须明确是事实还是假设。

需要增加新的视觉能力时，应先新增一个确定性 preset、运行时适配和门禁测试，再更新 v1 合同；不要让模型直接注入 option。
