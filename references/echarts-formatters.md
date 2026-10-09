# v1 数值格式化

Agent 不手写 ECharts option 或 formatter。请求中的 `measureTypes` 是唯一的语义格式入口，运行时根据它生成安全的模板字符串；源数据值不被改写。

支持的类型：

| 类型 | 轴模板 | tooltip 后缀 |
| --- | --- | --- |
| `currency` | `¥{value}` | `¥{c}` |
| `percent` | `{value}%` | `{c}%` |
| `count` / `number` / `unitless` | `{value}` | `{c}` |
| `duration` | `{value} 天` | `{c} 天` |

金额可用对象形式声明 `{ "type": "currency", "divisor": 10000, "unit": "万", "decimals": 1 }`；编译器先做除数和舍入，再应用模板。

以下内容均会被请求校验或报告门禁拒绝：函数字符串、箭头函数、任意 JS 表达式、外部脚本、任意 CSS 和用户背景色。若某种显示无法由上述 token 表达，应在数据准备阶段提供清洗/预格式化列，不能把回调塞进 manifest。
