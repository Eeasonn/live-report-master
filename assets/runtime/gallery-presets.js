/* 拖拽拼版画廊 · 预设库（docs/gallery-drag-compose-discussion.md）
 *
 * 三段数据，全部自足（不读用户表、不依赖 adapter）：
 *   patterns[] 版面 pattern —— blocks 相对坐标（12 列网格 × 48px 行），一次落位；
 *                              kind ∈ kpi(n 张一排)/chart/viewType + text。
 *   charts[]   图表示例卡 —— demo 数据由 editor-runtime 的 GALLERY 区现渲缩略
 *                              （Flint 先例：模板自带测试数据 + gallery 覆盖）；
 *                              viewType 必须是编辑器真实支持的档位。
 *   dataCards[] 数据卡快捷项 —— 点击走既有 addValueOn/addText/addGrid/addFilter。
 *
 * 只读数据，零逻辑；加载方 = server/template.py（<script id="ai-report-gallery-presets">）。
 */
(function(global){
  'use strict';

  var MONTHS = ['1月','2月','3月','4月','5月','6月'];
  var REGIONS = ['华东','华南','华北'];

  global.AI_GALLERY_PRESETS = {
    version: 1,

    /* ---- 图表示例卡（缩略 = demo 现渲；插入 = addBIOn 的 preset 参数） ---- */
    charts: [
      { id:'line-trend', title:'折线 · 趋势', family:'趋势', viewType:'line', w:8, h:6,
        desc:'随时间变化的走势；适合月度/日粒度单指标。',
        demo:{ x:MONTHS, series:[{name:'销售额', data:[120,132,101,134,90,230]}] } },
      { id:'area-trend', title:'面积 · 累计感', family:'趋势', viewType:'area', w:8, h:6,
        desc:'强调量级与累积感的时间走势。',
        demo:{ x:MONTHS, series:[{name:'销量', data:[56,72,61,88,110,96]}] } },
      { id:'bar-rank', title:'柱形 · 对比排行', family:'比较', viewType:'bar', w:6, h:6,
        desc:'类目间对比；配合降序即排行。',
        demo:{ x:REGIONS, series:[{name:'销售额', data:[230,180,150]}] } },
      { id:'bar-group', title:'分组柱 · 双系列', family:'比较', viewType:'bar', w:6, h:6,
        demo:{ x:REGIONS, series:[{name:'今年', data:[230,180,150]},{name:'去年', data:[200,160,120]}] },
        desc:'同维度两个指标的并列对比。' },
      { id:'bar-stacked', title:'堆叠柱 · 构成', family:'比较', viewType:'stackedBar', w:8, h:6, breakdown:true,
        desc:'一个维度按第二个维度拆分的构成对比（交叉列）。',
        demo:{ x:REGIONS, series:[{name:'线上', data:[120,90,70]},{name:'线下', data:[110,90,80]}] } },
      { id:'pie-share', title:'饼图 · 占比', family:'构成', viewType:'pie', w:4, h:6,
        desc:'类目占比；建议 ≤5 片。',
        demo:{ series:[{name:'占比', data:[{name:'华东',value:35},{name:'华南',value:28},{name:'华北',value:22},{name:'其他',value:15}]}] } },
      { id:'donut-share', title:'环形 · 占比', family:'构成', viewType:'donut', w:4, h:6,
        desc:'占比 + 中心留白可读总量。',
        demo:{ series:[{name:'占比', data:[{name:'线上',value:46},{name:'线下',value:32},{name:'其他',value:22}]}] } },
      { id:'funnel-stage', title:'漏斗 · 转化', family:'流程', viewType:'funnel', w:6, h:6,
        desc:'有序阶段的递减转化（需阶段语义维度）。',
        demo:{ series:[{name:'转化', data:[{name:'线索',value:100},{name:'跟进',value:62},{name:'成交',value:31}]}] } },
      { id:'scatter-corr', title:'散点 · 关联', family:'关系', viewType:'scatter', w:6, h:6,
        desc:'两个指标的相关性（每个点 = 一条记录/一个类目）。',
        demo:{ series:[{name:'订单', data:[[20,140],[35,180],[52,210],[48,190],[66,260],[80,300]]}] } },
      { id:'heatmap-cross', title:'热力 · 交叉密度', family:'关系', viewType:'heatmap', w:8, h:6,
        desc:'维度 × 维度的交叉矩阵，颜色 = 值密度。',
        demo:{ x:MONTHS.slice(0,4), y:REGIONS,
               data:[[0,0,30],[1,0,52],[2,0,41],[3,0,66],[0,1,22],[1,1,48],[2,1,36],[3,1,58],[0,2,12],[1,2,33],[2,2,28],[3,2,44]] } },
      { id:'table-agg', title:'聚合表 · 证据', family:'表格', viewType:'table', w:8, h:6,
        desc:'维度 × 指标的聚合证据表；中国式表头 + 合并默认开。',
        demo:{ cols:['类目','销售额','毛利'], rows:[['华东','2,300','460'],['华南','1,800','390'],['华北','1,500','300']] } },
      { id:'table-pivot', title:'交叉矩阵 · 多维', family:'表格', viewType:'table', w:12, h:6,
        desc:'行 × 列交叉 + 合计（放列区即透视）。',
        demo:{ cols:['区域','线上','线下','合计'], rows:[['华东','1,200','1,100','2,300'],['华南','900','900','1,800']] } }
    ],

    /* ---- 版面 pattern（blocks 相对坐标，落到当前网格底部） ----
     * kpi 块 n=一排张数（每张 3 列）；chart 块落到画布时用真实数据首维首指标。 */
    patterns: [
      { id:'p-kpi-trend', title:'KPI + 趋势', desc:'三张指标卡打底，一张全宽趋势收口。',
        blocks:[ {kind:'kpi', n:3, x:0, y:0},
                 {kind:'chart', viewType:'line', title:'趋势', x:0, y:3, w:12, h:6} ] },
      { id:'p-dash3', title:'仪表盘三图', desc:'KPI 条 + 趋势 + 占比 + 排行，一屏看全。',
        blocks:[ {kind:'kpi', n:4, x:0, y:0},
                 {kind:'chart', viewType:'line', title:'趋势', x:0, y:3, w:8, h:6},
                 {kind:'chart', viewType:'pie', title:'构成', x:8, y:3, w:4, h:6},
                 {kind:'chart', viewType:'bar', title:'排行', x:0, y:9, w:12, h:6} ] },
      { id:'p-chart-table', title:'左图右表', desc:'图给印象，表给证据（8+4 同行）。',
        blocks:[ {kind:'chart', viewType:'bar', title:'对比', x:0, y:0, w:8, h:6},
                 {kind:'chart', viewType:'table', title:'明细', x:8, y:0, w:4, h:6} ] },
      { id:'p-min1', title:'单图极简', desc:'free 报告的最小单元：一张图开门。',
        blocks:[ {kind:'chart', viewType:'line', title:'趋势', x:0, y:0, w:12, h:6} ] },
      { id:'p-2x2', title:'四图双列', desc:'2×2 双列四图，多角度并列。',
        blocks:[ {kind:'chart', viewType:'bar', title:'对比', x:0, y:0, w:6, h:6},
                 {kind:'chart', viewType:'line', title:'趋势', x:6, y:0, w:6, h:6},
                 {kind:'chart', viewType:'pie', title:'构成', x:0, y:6, w:6, h:6},
                 {kind:'chart', viewType:'scatter', title:'关联', x:6, y:6, w:6, h:6} ] },
      { id:'p-chart-insight', title:'图 + 洞察', desc:'左图右小结（8+4 sidecar）。',
        blocks:[ {kind:'chart', viewType:'bar', title:'对比', x:0, y:0, w:8, h:6},
                 {kind:'text', textStyle:'annotation', title:'本节小结', x:8, y:0, w:4, h:6} ] },
      { id:'p-trend-share', title:'趋势 + 占比', desc:'时间走势与当期构成上下呼应。',
        blocks:[ {kind:'chart', viewType:'line', title:'走势', x:0, y:0, w:12, h:6},
                 {kind:'chart', viewType:'donut', title:'最新占比', x:0, y:6, w:4, h:6},
                 {kind:'chart', viewType:'stackedBar', title:'构成', x:4, y:6, w:8, h:6} ] },
      { id:'p-kpi-table', title:'KPI + 明细表', desc:'三卡 + 全宽交叉矩阵，查数型报告。',
        blocks:[ {kind:'kpi', n:3, x:0, y:0},
                 {kind:'chart', viewType:'table', title:'交叉明细', x:0, y:3, w:12, h:7} ] }
    ],

    /* ---- 数据卡快捷项（点击 = 既有插入原语） ---- */
    dataCards: [
      { id:'d-kpi', title:'指标卡', desc:'一个指标 + 大数字（可加对比/目标）。', action:'kpi' },
      { id:'d-text', title:'文本卡', desc:'段落说明 / 本节小结。', action:'text' },
      { id:'d-grid', title:'自由表格', desc:'轻 Excel 编辑的手工表格。', action:'grid' },
      { id:'d-filter', title:'筛选器', desc:'列筛选卡（全局 / 页签作用域）。', action:'filter' }
    ]
  };
})(typeof window !== 'undefined' ? window : this);
