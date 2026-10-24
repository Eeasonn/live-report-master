# 请求写法示例 · 业务一句话 → 最小合法请求片段

写 `report_request.json` 前对照。每例 = **用户原话 → 请求片段 → 为什么这么写**，JSON 均为
能过 `validate_request.py` 的真实形状（配套词汇全集见 `authoring-feature-map.md`，
.family 判定见 SKILL.md「模型负责什么」）。示例沿用一张虚构销售长表
`销售明细.csv`：`订单日期 / 月份 / 地区 / 门店 / 渠道 / 销售额 / 订单数 / 毛利额`。

三条使用纪律（同 feature-map）：

1. **片段不是完整请求**——外层恒为 `{"schemaVersion":1, "templateId":"standard-bi-v1", "title":"…", …片段…}`；
2. **缺省即专业默认**，示例只声明业务点名的东西；
3. **标题写发现句**（SKILL.md「标题写发现句」）——示例中的 title 是发现句示范，
   你写请求时应换成自己数据上的真发现，不是照抄字面。

---

## 1. 趋势：「看 2025 年每月销售额走势，一咧到年底」

```json
"charts": [{
  "presetId": "line.trend",
  "title": "销售额逐月爬坡，Q4 冲上 8,200 万",
  "encoding": { "dimension": "月份", "measures": ["销售额"] }
}]
```

**为什么**：时间维 → `line.trend`；月列（YYYY-MM）被 `date_parse` 识别后按日历序自动排布、
自动按月分桶，不需要声明任何分桶键。标题 = 发现句 + 时间范围由数据自己说明。

## 2. 排名 + TOP-K：「销售额前 10 的门店」

```json
"charts": [{
  "presetId": "bar.rank",
  "title": "旗舰店贡献了前 10 门店四成的销售额",
  "encoding": { "dimension": "门店", "measures": ["销售额"] },
  "topK": 10
}]
```

**为什么**：「前 N」→ `topK`（1-100，权威键；`limit` 是别名同槽位）；排序缺省即按首指标
降序，不用写 `sort`。TOP-K 是**显示截断**，合计行仍按全量算（口径不缩水）。

## 3. 构成 vs 比大小：「各渠道每个月的销售额构成」vs「各渠道之间比比谁的订单多」

```json
"charts": [{
  "presetId": "bar.stacked",
  "title": "线上渠道贡献了每个月销售额的六成以上",
  "encoding": { "dimension": "月份", "measures": ["销售额"], "breakdown": "渠道" }
}, {
  "presetId": "bar.rank",
  "title": "华东订单量反超华南",
  "encoding": { "dimension": "地区", "measures": ["订单数"], "breakdown": "渠道" }
}]
```

**为什么**：读**部分-整体构成** → `bar.stacked`（breakdown 渲染堆叠段）；读**谁大谁小** →
`bar.rank`（breakdown 渲染分组柱）。占比堆叠（每根柱归一 100%）加
`"encoding": { "stackMode": "normalize" }`——percent 类指标进堆叠的唯一合法路径。

## 4. 占比：「各渠道销售占比」

```json
"charts": [{
  "presetId": "donut.share",
  "title": "线下经销仍占半壁江山",
  "encoding": { "dimension": "渠道", "measures": ["销售额"] }
}]
```

**为什么**：切片 ≤8 才用 pie/donut（超了编译期拒绝）；按值降序自动。想要「占总计百分比」
的**可筛选派生指标**（进表格/被窗口引用）不用 pie——用示例 7 的 `share` 节点。

## 5. 派生比率：「毛利率 = 毛利额 ÷ 销售额，按月看」

```json
"datasets": [{
  "id": "primary",
  "file": "销售明细.csv",
  "derived": {
    "measures": [{
      "id": "gm", "name": "毛利率", "kind": "formula",
      "expr": { "op": "div",
                "left":  { "agg": "sum", "col": "毛利额" },
                "right": { "agg": "sum", "col": "销售额" } },
      "measureType": { "type": "percent" }
    }]
  }
}],
"charts": [{
  "presetId": "line.trend",
  "title": "毛利率稳定在 22% 上下，未随规模摊薄",
  "encoding": { "dimension": "月份", "measures": ["毛利率"] }
}]
```

**为什么**：比率必须声明为**聚合级派生指标**（SUM(毛利额)/SUM(销售额)，聚合后重算），
绝不对行级比率列取平均；`measureType.percent` 接管格式与不可加闸。图表里它就是普通
指标名，编译期把值烘进静态 option、运行时按同一求值器重算（WYSIWYG）。
**percent 显示语义（2026-10-23 拍板 D1）**：公式节点存的是**运算原值**（0.2207 这种
比率），`measureType.percent` =「此列按百分数读」——编译与运行时都在显示层 ×100
（0.2207 → 22.07%，轴模板 `{value}%` 直显），声明里不要自己先 ×100；share 与
window(rate)（示例 6/7）求值时已是百分数，直显、不重复缩放。

## 6. 环比：「给我看销售额的月环比」

```json
"datasets": [{
  "id": "primary",
  "file": "销售明细.csv",
  "derived": {
    "measures": [{
      "id": "mom", "name": "销售额月环比", "kind": "window",
      "input": { "agg": "sum", "col": "销售额" },
      "fn": "prev", "grain": "month", "offset": 1,
      "timeField": "月份", "output": "rate",
      "measureType": { "type": "percent" }
    }]
  }
}],
"charts": [{
  "presetId": "bar.rank",
  "title": "3 月环比转正，是去年 10 月以来首次",
  "encoding": { "dimension": "月份", "measures": ["销售额月环比"] }
}]
```

**为什么**：同环比 = 窗口算子 `prev` + `offset` + `grain`（不是三个内置口径）；窗口节点
要求**图表维度 == `timeField` 且分桶 == `grain`**，不符编译期直接给可行动报错——绝不比错
周期。同比 = `offset` 换成对应期数（月粒度 12）。空档（首期无前期）保持 null 不造 0。

## 7. 筛选聚合 + 占比：「有退货记录的门店占多少」

```json
"datasets": [{
  "id": "primary",
  "file": "销售明细.csv",
  "derived": {
    "measures": [
      { "id": "ret", "name": "有退货门店数", "kind": "filtered",
        "agg": "countd", "col": "门店",
        "filters": [{ "column": "退货金额", "op": "gt", "value": 0 }] },
      { "id": "retpct", "name": "退货门店占比", "kind": "share",
        "input": { "ref": "有退货门店数" }, "scope": "all",
        "measureType": { "type": "percent" } }
    ]
  }
}],
"kpis": [{ "type": "kpi.metric", "label": "有退货门店数", "measure": "有退货门店数" }],
"charts": [{
  "presetId": "bar.rank",
  "title": "华南每三家门店就有一家出现过退货",
  "encoding": { "dimension": "地区", "measures": ["有退货门店数"] }
}]
```

**为什么**：条件聚合（`AGG(CASE WHEN 谓词 THEN …)`）= `kind:"filtered"`，谓词形状与报告
筛选同词汇（**键名是 `column` 不是 `col`**）；组宇宙由图表分组决定、组内只聚通过筛选的行。
count 可省 `col`（=条件计行数）；无命中行 count/countd=0、sum/average=null（空缺不造 0）。
占比 = `share` 节点（**分子写 `input`**，`scope:'all'`=占当前聚合全部值之和、与合计行同口径；
`scope:'parent'`+`parentField`=占父级组内之和）。同环比引用它：窗口节点
`"input": { "ref": "有退货门店数" }`。

## 8. 读者筛选：「让读者自己挑地区和时间段看」

```json
"filters": [
  { "column": "地区", "op": "in" },
  { "column": "月份", "op": "daterange", "value": [null, null] }
],
"charts": [{
  "presetId": "line.trend",
  "title": "精选筛选后的销售额走势",
  "encoding": { "dimension": "月份", "measures": ["销售额"] }
}]
```

**为什么**：**维度筛选不带 `value` = 交互控件无预选（全部）**，是声明筛选控件的正常写法
（validate 会打 advisory 提醒，非错误）；时间范围控件用 `daterange` + `value:[null,null]`
（开口区间 = 不约束，任一端可填 ISO 日期定死）。标准模板恒要 1-12 张图，纯筛选声明不构成
请求。时间列自动渲染日期范围控件、维度渲染值域多选。提示词明说「只看 X」的**定死范围**
才是 `chart.table.columnFilters` / `dimScopes`（读者不可改）——两层不许混（SKILL.md
「筛选语义不改道」）。

## 9. 多维交叉矩阵：「各国各省份做行、品牌做列，看销售额明细聚合」

```json
"charts": [{
  "presetId": "table.aggregate",
  "title": "华南在各品牌上的销售额全面领先",
  "encoding": { "dimension": "国家", "measures": ["销售额"] },
  "dimensions": ["国家", "省份"],
  "breakdowns": ["品牌"]
}]
```

**为什么**：**行区**多于一维时用 `chart.dimensions`（1–3 项，`encoding.dimension` 恒为第 1 槽，
声明即完整行区、不会偷偷垫时间列）；**列区**用 `chart.breakdowns`（1–3 项）。本例 = 2 行维 ×
1 列维的交叉矩阵（嵌套行头合并格 + 列段组表头 + 段尾小计），3×3 是上限（服务端 GROUP BY
6 维压线）。**标题里的每个维度词都必须真的落在行区/列区/指标里**——只写了 `dimension:国家`
却起名「各国各品牌」会被 lint L22 拦（标题承诺了数据没画的视图）。

---

## 10. 证据绑定 + 分析主张：「把洞察写得有据可查」

构建日志的 `[fact]` 行（或 `profile.json` 的 `facts`）给出脚本算好的确定性事实
（id 形如 `fact:concentration:1`）。洞察引用它们：主张句只说事实支持的话，
`facts` 槽位绑定 id，报告里渲染成洞察下方的「依据（脚本计算）」脚注——

```json
"charts": [{
  "id": "rank-region",
  "presetId": "bar.rank",
  "title": "欧洲终端业务部以 36.8% 的 Sell In 份额领先",
  "question": "哪个大区的 Sell In 体量最大、集中度有多高？（依据 fact:concentration:1）",
  "encoding": { "dimension": "Management Region", "measures": ["Sell In"] }
}],
"insights": [
  { "text": "Sell In 高集中：前 3 个大区占 84.8%，头部市场波动即可显著改变大盘。",
    "facts": ["fact:concentration:1"] }
]
```

**为什么**：`chart.question` 声明这张卡回答的主张/问题（lint L23 只提醒「有图无主张」，
不查对错）；`insights[].facts` 是证据绑定——**引用不存在的 fact id 构建失败**（报错会
列出可用 id，防幻觉闸），合法引用则在洞察下方渲染「依据（脚本计算）：fact:… <事实
陈述>」脚注，读者与复核者都能对回 `decisions.md`「数据事实（脚本计算）」节逐条核对。
free 档的 `{{fact:id}}` 占位与 `facts:[]` 绑定走同一套解析校验。

---

## 反例速查（写了就返工）

- ❌ `"measures": ["毛利率"]` 但没在 `derived.measures` 声明 → 「指标不存在」构建失败。
- ❌ 比率列直接 `sum` / 进 `bar.stacked` → 不可加闸编译期报错（intensive 纪律）。
- ❌ 窗口指标放到 `dimension` ≠ `timeField` 的图上 → 编译期可行动报错，不是静默空图。
- ❌ KPI 硬编码 `"value": 1234` → 用户筛选后不重算、与图表矛盾（声明了 filters 还会吃 advisory）。
- ❌ 「只看华东」写进 `dimension`/`breakdown` → 筛选语义不改道，落 `filters` 或 `columnFilters`。
- ❌ `insights[].facts` / `{{fact:id}}` 引用编造的 id → 构建失败（防幻觉闸）；先读 `[fact]` 行再绑定。
