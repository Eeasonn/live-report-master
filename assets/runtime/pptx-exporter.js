(function(global){
  'use strict';

  var EMU_PER_PX=9525;
  // 画布真值：p:sldSz 12192000×6858000 EMU = 1280×720px @96dpi（presentationXml）。
  var SLIDE_W=1280,SLIDE_H=720;

  function manifest(){return (global.AIReportRuntime&&global.AIReportRuntime.manifest)||{};}
  // O1 无框文本（2026-09-26 拍板，docs/presentation-ppt-alignment-review-2026-09-26.md）：
  // manifest.presentation.plainText 是汇报/导出级开关，三端同源——deck 克隆由
  // presentation.js 挂 .ai-pres-plaintxt（CSS 去框）、PDF 克隆同一 DOM 自动跟随、
  // PPTX 由 cardFillInk 在此短路。封面/深色卡豁免（在 cardFillInk 内先判）：
  // 去框=深底消失白字落白页。
  function plainTextOn(){var p=manifest().presentation;return !!(p&&p.plainText);}
  // C3 数据截至（ux-review-2026-09-25 读者信任批）：与 presentation.js dataAsOfText
  // 同一读取口径——manifest.meta.dataAsOf 由编译期烘焙（ISO YYYY-MM-DD），可能不
  // 存在：缺失/为空返回 ''，调用方整段省略，绝不显示 undefined。
  function metaDataAsOfText(){
    var v=manifest().meta&&manifest().meta.dataAsOf;
    if(v==null)return '';
    var s=String(v).trim();
    if(!s)return '';
    var m=/^(\d{4}-\d{2}-\d{2})(?:[T\s]|$)/.exec(s);
    return m?m[1]:s;
  }
  function blockFor(id){return (manifest().blocks||[]).find(function(b){return b.id===id;});}
  function chartFor(blockId){return (manifest().charts||[]).find(function(c){return c.blockId===blockId;});}
  function kpiFor(blockId){var m=/^block-kpi-(\d+)$/.exec(blockId||'');return m?(manifest().kpis||[])[Number(m[1])]:null;}

  function escapeXml(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
  function escAttr(s){return escapeXml(s);}
  function utf8(str){return new TextEncoder().encode(str);}
  function crc32(bytes){
    var table=crc32.table||(crc32.table=(function(){var t=[],c;for(var n=0;n<256;n++){c=n;for(var k=0;k<8;k++)c=c&1?0xEDB88320^(c>>>1):c>>>1;t[n]=c>>>0;}return t;})());
    var c=0xFFFFFFFF;
    for(var i=0;i<bytes.length;i++)c=table[(c^bytes[i])&0xFF]^(c>>>8);
    return (c^0xFFFFFFFF)>>>0;
  }
  function concat(arrays){var total=arrays.reduce(function(n,a){return n+a.length;},0);var out=new Uint8Array(total),o=0;arrays.forEach(function(a){out.set(a,o);o+=a.length;});return out;}
  function u16(v){return new Uint8Array([v&255,(v>>8)&255]);}
  function u32(v){return new Uint8Array([v&255,(v>>8)&255,(v>>16)&255,(v>>>24)&255]);}
  function dosDateTime(d){d=d||new Date();return {time:((d.getHours()<<11)|(d.getMinutes()<<5)|(d.getSeconds()>>1)),date:((((d.getFullYear()-1980)&127)<<9)|((d.getMonth()+1)<<5)|d.getDate())};}
  function deflateRaw(bytes){return new Response(new Blob([bytes]).stream().pipeThrough(new CompressionStream('deflate-raw'))).arrayBuffer().then(function(ab){return new Uint8Array(ab);});}
  async function buildZip(files){
    var localParts=[],centralParts=[],offset=0;
    var dt=dosDateTime();
    for(var i=0;i<files.length;i++){
      var name=files[i].name,raw=files[i].data,bytes=raw instanceof Uint8Array?raw:utf8(raw);
      var compressed=await deflateRaw(bytes);
      var useDeflate=compressed.length<bytes.length;
      var method=useDeflate?8:0,stored=useDeflate?compressed:bytes,crc=crc32(bytes);
      var nameBytes=utf8(name);
      var local=concat([u32(0x04034b50),u16(20),u16(0),u16(method),u16(dt.time),u16(dt.date),u32(crc),u32(stored.length),u32(bytes.length),u16(nameBytes.length),u16(0),nameBytes,stored]);
      var central=concat([u32(0x02014b50),u16(20),u16(20),u16(0),u16(method),u16(dt.time),u16(dt.date),u32(crc),u32(stored.length),u32(bytes.length),u16(nameBytes.length),u16(0),u16(0),u16(0),u16(0),u32(0),u32(offset),nameBytes]);
      localParts.push(local);centralParts.push(central);offset+=local.length;
    }
    var centralOffset=offset;var central=concat(centralParts);var eocd=concat([u32(0x06054b50),u16(0),u16(0),u16(files.length),u16(files.length),u32(central.length),u32(centralOffset),u16(0)]);
    return new Blob([concat(localParts),central,eocd],{type:'application/vnd.openxmlformats-officedocument.presentationml.presentation'});
  }
  function download(blob,name){var url=URL.createObjectURL(blob);var a=document.createElement('a');a.href=url;a.download=name;document.body.appendChild(a);a.click();document.body.removeChild(a);setTimeout(function(){URL.revokeObjectURL(url);},1000);}

  // ---------------- DOM capture helpers --------------------------------------
  function chartDataUrl(chart){
    try{
      var host=document.getElementById(chart.elementId||chart.id);
      var inst=global.echarts&&host&&global.echarts.getInstanceByDom(host);
      if(!inst&&global.echarts&&chart.option){
        // tabs 报告从未到访的页签里图表是惰性 init 的（拿不到实例）——按编译期
        // option 离屏重渲一次再导出，否则这个页签导出的是**空白图**（deck 克隆
        // 的 chart-host 已被清空，domToPng 拍不到任何东西）。与 presentation
        // chartImgFor 的两级兜底同构。
        var off=document.createElement('div');
        off.style.cssText='position:fixed;left:-99999px;top:-99999px;width:960px;height:540px;visibility:hidden;';
        document.body.appendChild(off);
        try{
          var o=global.echarts.init(off,null,{width:960,height:540});
          o.setOption(chart.option);
          var url=o.getDataURL({type:'png',pixelRatio:2,backgroundColor:'#fff'});
          o.dispose();
          return url;
        }finally{ if(off.parentNode)off.parentNode.removeChild(off); }
      }
      if(inst)return inst.getDataURL({type:'png',pixelRatio:2,backgroundColor:'#fff'});
    }catch(e){}
    return null;
  }
  // DOM → PNG 快照的内联样式（B5）：foreignObject 里的克隆节点完全脱离文档，
  // 不内嵌样式表则 class 样式全丢（表格/KPI/文本卡变成无样式截图）。
  // 规则从 spec_to_report.py SHELL_CSS 与 editor-runtime.css 精选（卡片壳 / 表格 /
  // KPI / 文本段落 / 气泡 / 进度条），剔除 @media / hover / contenteditable 与编辑态
  // 残留（列宽手柄、◎ 开关、卡片控件直接隐藏）。选择器不得含 `>`/`<`/`&`
  // （会打破 SVG 的 XML 解析）；改皮肤 token 时需同步此处。
  var SNAPSHOT_CSS=''
    +'.kpi-card{background:#fff;border-radius:12px;padding:20px 14px;border:1px solid #e5e3df;display:flex;flex-direction:column;gap:2px;justify-content:center;font-family:Inter,-apple-system,BlinkMacSystemFont,\'PingFang SC\',\'Microsoft YaHei\',sans-serif;color:#1a1a1a}'
    +'.kpi-card .label{font-size:12px;color:#787671;font-weight:500}'
    +'.kpi-card .value{font-size:25px;font-weight:700;letter-spacing:-.025em;font-variant-numeric:tabular-nums lining-nums;white-space:nowrap;color:#1a1a1a}'
    +'.kpi-card .unit{font-size:11px;color:#787671;font-weight:500;margin-left:4px}'
    // W5 挂账③（WP-12）：KPI 36px 装饰条退役 —— 快照与 SHELL_CSS 微条三态同语义：
    // 默认隐藏；带 .kpi-compare 的卡升级为「中点=前期基准」涨跌方向微条（4px×36px
    // 轨道 + 中点刻痕 + 半幅状态色，状态色与快照自己的对比文字同源 #1a7f4b/#c62f2f）。
    // foreignObject 脱离文档读不到 var()/不可靠走 :has()——方向由 domToPng 克隆期
    // 把 .kpi-compare 的 is-up/is-down 折到 .kpi-card 的 has-cmp/cmp-up/cmp-down 上。
    +'.kpi-card .accent-bar{display:none}'
    +'.kpi-card .kpi-foot{display:flex;align-items:center;gap:8px;margin-top:4px;flex-wrap:wrap}'
    +'.kpi-card .kpi-compare{font-size:11px;font-weight:600;color:#787671;white-space:nowrap}'
    +'.kpi-card .kpi-compare.is-up{color:#1a7f4b}.kpi-card .kpi-compare.is-down{color:#c62f2f}'
    +'.kpi-card.has-cmp .accent-bar{display:block;position:relative;height:4px;width:36px;border-radius:999px;background:#ede9e4;margin-top:2px;flex-shrink:0;overflow:hidden}'
    +'.kpi-card.has-cmp .accent-bar::before{content:"";position:absolute;left:50%;top:0;bottom:0;width:1px;background:#787671;opacity:.4}'
    +'.kpi-card.has-cmp.cmp-up .accent-bar::after{content:"";position:absolute;top:0;bottom:0;left:50%;width:50%;background:#1a7f4b}'
    +'.kpi-card.has-cmp.cmp-down .accent-bar::after{content:"";position:absolute;top:0;bottom:0;left:0;width:50%;background:#c62f2f}'
    +'.kpi-card .kpi-progress{display:inline-flex;align-items:center;gap:5px;min-width:80px;max-width:180px}'
    +'.kpi-card .kpi-track{flex:1 1 auto;height:4px;min-width:36px;border-radius:999px;background:#ede9e4;overflow:hidden}'
    +'.kpi-card .kpi-track i{display:block;height:100%;border-radius:999px;background:__ACCENT__}'
    +'.kpi-card .kpi-progress em{font-size:10px;font-style:normal;color:#787671}'
    +'.block{background:#fff;border-radius:12px;border:1px solid #e5e3df;padding:20px 18px;display:flex;flex-direction:column;overflow:hidden;font-family:Inter,-apple-system,BlinkMacSystemFont,\'PingFang SC\',\'Microsoft YaHei\',sans-serif;color:#1a1a1a}'
    +'.block-title{font-size:14px;font-weight:600;display:flex;align-items:center;gap:8px;margin:0 0 12px;color:#1a1a1a}'
    +'.block-title .sub{font-size:12px;font-weight:400;color:#787671}'
    +'.block-annotation{margin:8px 0 2px;font-size:13px;line-height:1.65;color:#787671;white-space:pre-wrap}'
    +'.chart-host{flex:1;width:100%;min-height:0}'
    +'.tx-p{margin:0}.tx-h1{font-size:28px;font-weight:700;letter-spacing:-.01em;color:#1a1a1a}.tx-h2{font-size:18px;font-weight:700;color:#1a1a1a}.tx-h3{font-size:14px;font-weight:600;color:#1a1a1a}'
    +'.tx-h4{font-size:13px;font-weight:600;color:#1a1a1a}.tx-h5{font-size:13px;font-weight:600;color:#5d5b54}.tx-h6{font-size:12px;font-weight:600;color:#5d5b54}.tx-h7,.tx-h8{font-size:12px;font-weight:600;color:#787671}.tx-h9{font-size:11.5px;font-weight:600;color:#787671}'
    +'.tx-quote{border-left:3px solid #e5e3df;padding:2px 0 2px 12px;color:#5d5b54}'
    +'.tx-code{font-family:ui-monospace,Menlo,monospace;background:#f6f5f4;border:1px solid #e5e3df;border-radius:8px;padding:8px 12px;font-size:12px;line-height:1.55;white-space:pre-wrap;overflow-x:auto;color:#1a1a1a}'
    +'.tx-hlblock{background:#fafaf9;border:1px solid #ede9e4;border-left:3px solid __ACCENT__;border-radius:8px;padding:8px 12px}'
    +'.tx-list-ul{position:relative;padding-left:18px}.tx-list-ul::before{content:"•";position:absolute;left:4px;color:#5d5b54}'
    +'.tx-list-ol{position:relative;padding-left:22px}.tx-list-no{position:absolute;left:0;color:#5d5b54}'
    +'.tx-task-box{accent-color:__ACCENT__;width:14px;height:14px;margin:0 6px 0 0;vertical-align:-2px}'
    +'.tx-task-done{color:#787671;text-decoration:line-through}'
    +'.tx-body{font-size:13px;line-height:1.65;color:#5d5b54}.tx-body strong{color:#1a1a1a}'
    +'.tx-body p,.block-annotation p{margin:4px 0}.tx-body ul,.tx-body ol{margin:4px 0;padding-left:20px}'
    +'.tx-body mark,.block-annotation mark,.tx-p mark{background:#f9e79f;color:inherit;padding:0 1px;border-radius:2px}'
    +'.tx-tab-num{color:__ACCENT__;font-weight:700;margin-right:3px}'
    +'.tx-tab-card{padding:10px 2px 2px;border-bottom:1px solid #ede9e4}'
    +'.tx-bubble{display:inline-block;background:#f6f5f4;color:#1a1a1a;border:1px solid #e5e3df;border-radius:999px;padding:3.4px 12px;font-size:13px;font-weight:600;margin:3px 0}'
    +'.tx-bubble p{margin:0}'
    +'.tx-bubble-strip{display:flex;flex-wrap:nowrap;align-items:center;gap:6px;overflow:hidden;margin:0 0 6px}'
    +'.tx-bubble-strip .tx-bubble{flex:0 0 auto;margin:0;white-space:nowrap}'
    +'.tx-callout{position:relative;background:#fafaf9;border:1px solid #e5e3df;border-radius:12px;padding:12px 16px}'
    +'.tx-callout-transparent{position:relative;background:transparent;border:1px solid transparent;border-radius:12px;padding:12px 16px}'
    +'.tx-white{background:#fff;border:1px solid #e5e3df;border-radius:12px;padding:12px 16px}'
    // 统一文本卡组件（2026-09-15）：类型 × 颜色。.tx-card 几何 + .tx-annot 角线 +
    // .txc-* 仅背景/描边差异——与 SHELL_CSS 逐语义对齐（占位符同 B2 通道导出期实换）。
    // W2 角标批注：右上徽章/顶衬退役，批注身份 = 左上+右下两只对角角色线
    // （2px×28px，__ACCENT__ 纯色，无 color-mix 白基）；foreignObject 内伪元素
    // 随 <style> 存活（旧徽章同通道先例）。
    +'.tx-card{position:relative;background:#fff;border:1px solid #e5e3df;border-radius:12px;padding:12px 16px;box-shadow:none}'
    +'.txc-none{background:transparent;border-color:transparent;box-shadow:none}'
    +'.txc-white{background:#fff;border-color:#e5e3df}'
    +'.tx-annot::before{content:"";position:absolute;inset:0;pointer-events:none;background:linear-gradient(__ACCENT__,__ACCENT__) 0 0/2px 28px no-repeat,linear-gradient(__ACCENT__,__ACCENT__) 0 0/28px 2px no-repeat,linear-gradient(__ACCENT__,__ACCENT__) 100% 100%/2px 28px no-repeat,linear-gradient(__ACCENT__,__ACCENT__) 100% 100%/28px 2px no-repeat}'
    +'.txc-white{background:#fff;border-color:#e5e3df}'
    +'.txc-soft{background:#fafaf9;border-color:#e5e3df}'
    +'.txc-accent{background:__ACCENT_8__;border-color:__ACCENT_24__}'
    +'.txc-green{background:#eef7ef;border-color:#cfe6d4}'
    +'.txc-amber{background:#fdf6e4;border-color:#f0dfae}'
    +'.txc-red{background:#fdf0ed;border-color:#f2cdc4}'
    +'.txc-dark{background:linear-gradient(120deg,__COVER_FROM__ 0%,__COVER_VIA__ 55%,__COVER_TO__ 100%);border-color:transparent}'
    +'.txc-dark .tx-h1,.txc-dark .tx-h2,.txc-dark .tx-h3{color:__COVER_INK__}'
    +'.txc-dark .tx-body{color:__COVER_INK_SOFT__}'
    +'.txc-dark .tx-bubble{background:rgba(255,255,255,.12);color:__COVER_INK__;border-color:rgba(255,255,255,.2)}'
    // W2：.tx-annot.txc-dark 徽章变体与 .tx-annot/.tx-callout.section-insight 豁免
    // 一并退役——角线 = __ACCENT__ 纯色，无任何视觉特判分支。
    // 封面/banner 渐变是 __COVER_*__ 占位符：导出时由 snapshotCss() 从
    // documentElement 的 --cover-* 变量解析（docs/report-theme-convergence-design.md），
    // default 回退海军蓝。foreignObject 脱离文档读不到 var()，必须导出期实换。
    +'.tx-banner{background:linear-gradient(120deg,__COVER_FROM__ 0%,__COVER_VIA__ 55%,__COVER_TO__ 100%);color:__COVER_INK__;border-radius:12px;padding:16px 20px}'
    +'.tx-banner .tx-h1,.tx-banner .tx-h2,.tx-banner .tx-h3{color:__COVER_INK__}'
    +'.tx-banner .tx-body{color:__COVER_INK_SOFT__}'
    +'.tx-banner.ai-cover-text{background:linear-gradient(120deg,__ACCENT__ 0%,#101c3f 100%)}'
    // 关键洞察卡（insight-block）与批注一致（2026-09-16）：身份 = tx-card + tx-annot
    // 对角角色线（上方统一组件规则已覆盖），3px 左色条规则删除。
    +'table{border-collapse:collapse;width:100%;font-size:12px;font-family:Inter,-apple-system,BlinkMacSystemFont,\'PingFang SC\',\'Microsoft YaHei\',sans-serif}'
    +'.ai-report-table-wrap th,.ai-report-table-wrap td{border-bottom:1px solid #e5e3df;border-right:1px solid #e5e3df;padding:7px;text-align:left;color:#5d5b54;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}'
    +'.ai-report-table-wrap th{background:#f6f5f4;font-weight:500;font-size:11px;white-space:normal;overflow-wrap:anywhere}'
    +'.ai-report-table-wrap th:last-child,.ai-report-table-wrap td:last-child{border-right:0}'
    +'.ai-report-table-wrap th.ai-cell-num,.ai-report-table-wrap td.ai-cell-num,.ai-report-table-wrap th.ai-cell-share-h,.ai-report-table-wrap td.ai-cell-share,.ai-report-table-wrap th.ai-cell-target,.ai-report-table-wrap td.ai-cell-target,.ai-report-table-wrap th.ai-cell-rate,.ai-report-table-wrap td.ai-cell-rate,.ai-report-table-wrap th.ai-td-num,.ai-report-table-wrap td.ai-td-num{text-align:right}'
    +'table.ai-report-table-striped tbody tr:nth-child(even){background:#f6f5f4}'
    // 自由表格卡（kind:'grid'）在离屏 deck 里是真实 DOM 克隆（rowspan/colspan 天然存活）：
    // 这里只补手绘网格外观，行内样式（对齐/加粗/颜色）由 gridTableHtml 内联携带。
    +'.ai-gridcard-table{border-collapse:collapse;width:100%;font-size:12px}'
    +'.ai-gridcard-table td{border:1px solid #e5e3df;padding:6px 10px;color:#5d5b54;vertical-align:top;height:26px;overflow-wrap:anywhere}'
    +'.ai-cell-num{position:relative;white-space:nowrap}'
    +'.ai-cell-bar{position:absolute;left:0;right:0;top:0;bottom:0;background:__BAR_8__;border-left:2px solid __BAR_25__}'
    +'.ai-cell-bar i{position:absolute;left:0;top:0;bottom:0;background:__BAR_18__}'
    +'.ai-cell-num span:not(.ai-cell-bar){position:relative}'
    +'.ai-cell-num.ai-cell-max{color:__ACCENT__;font-weight:700}'
    +'.ai-cell-target{color:#5d5b54;font-weight:500;white-space:nowrap}'
    +'.ai-cell-rate{font-weight:600;white-space:nowrap}.ai-cell-rate.ok{color:#1aae39}.ai-cell-rate.bad{color:#d3380e}'
    +'.ai-cell-share{color:#787671;font-variant-numeric:tabular-nums}'
    // 迷你图趋势列（WP-9）：foreignObject 快照是隔离 SVG image 上下文，SHELL_CSS 的
    // .ai-spark-svg 规则不在 → svg 无 width/height 属性、无固有尺寸 → 0×0 整列塌缩。
    // 固定 28px ≈ standard 行高档（SHELL_CSS: row-h 40 − 12）；列宽仍由表格布局分配。
    +'.ai-cell-spark{padding:4px 8px;vertical-align:middle}'
    +'.ai-spark-svg{display:block;width:100%;height:28px;min-height:12px;max-height:32px}'
    +'.ai-table-total td{background:__ACCENT_6__;font-weight:700;border-top:2px solid __ACCENT_32__}'
    +'.ai-table-subtotal td{background:__ACCENT_3__;font-weight:600;border-top:1px solid #e5e3df}'   /* B2：3% accent 派生（runtime=color-mix(--accent 3%)），非紫主题导出不再残留淡紫 */
    +'.ai-table-breadcrumb{display:flex;gap:6px;align-items:center;padding:6px 10px;margin-bottom:8px;border:1px solid #ede9e4;border-radius:6px;background:#f6f5f4;font-size:12px;color:#5d5b54}'
    +'.ai-table-crumb{border:0;background:transparent;color:__ACCENT__;font-size:12px;padding:0}.ai-table-crumb-now{font-weight:600;color:#1a1a1a}.ai-table-crumb-hint{margin-left:8px;color:#a4a097;font-size:11px}'
    +'.ai-pb{display:flex;align-items:center;gap:6px;min-width:96px}'
    +'.ai-pb-track{position:relative;flex:1 1 auto;height:10px;border-radius:6px;background:#f1efec;overflow:hidden}'
    +'.ai-pb-fill{position:absolute;left:0;top:0;bottom:0;border-radius:6px}'
    +'.ai-pb-label{min-width:42px;text-align:right;font-size:11px;color:#787671}'
    +'.ai-report-table-bar{display:flex;align-items:center;justify-content:space-between;gap:8px;margin:0 0 8px}'
    +'.ai-report-table-mode{display:inline-flex;align-items:center;gap:6px}'
    +'.ai-report-table-mode .ai-report-secondary,.ai-report-secondary{border:1px solid #e5e3df;border-radius:999px;background:#fff;color:#5d5b54;height:32px;padding:0 14px;font-weight:600;font-size:12px;font-family:inherit}'
    +'.ai-report-table-mode .ai-report-secondary.is-active{background:#000;border-color:#000;color:#fff}'
    +'.ai-table-pager{display:flex;gap:6px;align-items:center;margin:0;font-size:12px;color:#5d5b54}'
    +'.ai-table-pager button{border:1px solid #c8c4be;border-radius:8px;background:#fff;padding:0 9px;font-size:12px;color:#5d5b54;height:26px}'
    +'.ai-table-pager button.is-active{background:#000;border-color:#000;color:#fff}'
    +'.ai-table-pager select{border:1px solid #c8c4be;border-radius:8px;background:#fff;color:#1a1a1a;font-size:12px;padding:0 6px;height:26px}'
    +'.ai-sort-arrow{display:inline-block;margin-left:3px;font-size:10px;color:__ACCENT__}'
    +'.ai-col-fbtn{border:0;background:transparent;color:#a4a097;font-size:11px;padding:0 2px}.ai-col-fbtn.is-active{color:__ACCENT__}'
    +'.ai-unit-suffix{font-size:10px;color:#787671;font-weight:400}'
    +'.ai-merged{background:#f6f5f4;font-weight:600}'
    +'.filter-chips{display:flex;flex-wrap:wrap;gap:8px;align-items:center}'
    +'.filter-chip{display:inline-flex;align-items:center;padding:4px 9px;border-radius:999px;background:__ACCENT_16__;border:1px solid __ACCENT_30__;color:__INK__;font-size:12px;line-height:1.3;white-space:nowrap}'
    +'.ai-report-trunc-note{display:inline-block;font-size:11.5px;color:#787671;background:#fff;border:1px solid #e5e3df;border-radius:999px;padding:1px 9px}'
    +'.ai-report-data-note{margin:6px 0 0;text-align:right}'
    +'.ai-report-no-data-msg{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px;min-height:120px;text-align:center;padding:8px}'
    +'.ai-report-no-data-t{font-size:13px;font-weight:600;color:#5d5b54}.ai-report-no-data-sub{font-size:12px;color:#787671}'
    +'.ai-report-empty{font-size:12px;color:#787671;padding:12px}'
    // C1 表格卡汇报态降级（ux-review-2026-09-25）：快照不烙死控件——工具行
    // （明细/聚合切换、搜索、排序、筛选、Σ、字段、导出）与分页器整体隐藏
    // （presentation.js degradeDeckTableCard 已在克隆 DOM 写内联 display:none，
    // 这里是 foreignObject 通道的兜底）；只读徽章 .ai-pres-table-note 随克隆
    // DOM 入图，离屏补同款样式（选择器禁 > < &，会打破 SVG 的 XML 解析）。
    +'.ai-report-table-bar,.ai-table-pager{display:none!important}'
    +'.ai-pres-table-note{display:flex;align-items:center;justify-content:flex-end;gap:6px;margin:4px 2px 0;padding-top:4px;border-top:1px solid #e5e3df;font-size:11px;line-height:1.4;color:#787671;white-space:nowrap}'
    +'.ai-col-resizer,.ai-pb-toggle,.ai-report-block-controls,.ai-report-drag-strip,.ai-grid-actions{display:none}';

  // 导出期解析封面令牌（单点；不碰 richCardShape/normColor 文本通道）。只认
  // #hex，读不到/非法一律回退 default 海军蓝——快照永不因主题缺变量而破。
  function snapshotCss(){
    var cs=null;try{cs=getComputedStyle(document.documentElement);}catch(_e){}
    function cv(name,d){
      var v='';try{v=(cs&&cs.getPropertyValue(name))||'';}catch(_e){}
      v=String(v).trim().toLowerCase();
      if(/^#[0-9a-f]{6}$/.test(v)&&v!==d)return v;
      return d;
    }
    var from=cv('--cover-from','#070f24'),via=cv('--cover-via','#0a1530'),
        to=cv('--cover-to','#1a2a52'),ink=cv('--cover-ink','#ffffff');
    function soft(hex){
      var r=parseInt(hex.slice(1,3),16),g=parseInt(hex.slice(3,5),16),b=parseInt(hex.slice(5,7),16);
      return 'rgba('+r+','+g+','+b+',.92)';
    }
    // B2（docs/report-theme-convergence-design.md）：accent 系占位符——快照脱离文档
    // 读不到 var()，导出期从 --accent/--ink 实换（0 处硬编码紫/蓝残留）。
    var accent=cv('--accent','#5645d4'),inkVar=cv('--ink','#1a1a1a');
    function rgbOf(hex){return [parseInt(hex.slice(1,3),16),parseInt(hex.slice(3,5),16),parseInt(hex.slice(5,7),16)];}
    function mixWhite(hex,t){var a=rgbOf(hex);return '#'+a.map(function(c){var v=Math.round(c+(255-c)*t).toString(16);return v.length<2?'0'+v:v;}).join('');}
    function rgba(hex,a){var c=rgbOf(hex);return 'rgba('+c[0]+','+c[1]+','+c[2]+','+a+')';}
    // ⚠ 全部 /g：__COVER_FROM/VIA/TO/INK_SOFT__ 在 SNAPSHOT_CSS 里各出现 2 次
    // （.txc-dark 与 .tx-banner 族）——非全局 replace 只换第一处，banner 卡残留
    // 非法 token → 整条声明被丢弃 → 白底白字不可见。
    return SNAPSHOT_CSS
      .replace(/__COVER_FROM__/g,from).replace(/__COVER_VIA__/g,via)
      .replace(/__COVER_TO__/g,to).replace(/__COVER_INK_SOFT__/g,soft(ink))
      .replace(/__COVER_INK__/g,ink)
      .replace(/__ACCENT_3__/g,mixWhite(accent,0.97)).replace(/__ACCENT_6__/g,mixWhite(accent,0.94)).replace(/__ACCENT_8__/g,mixWhite(accent,0.92))
      .replace(/__ACCENT_16__/g,mixWhite(accent,0.84)).replace(/__ACCENT_24__/g,mixWhite(accent,0.76))
      .replace(/__ACCENT_30__/g,mixWhite(accent,0.70)).replace(/__ACCENT_32__/g,mixWhite(accent,0.68))
      .replace(/__BAR_8__/g,rgba(accent,0.08)).replace(/__BAR_18__/g,rgba(accent,0.18))
      .replace(/__BAR_25__/g,rgba(accent,0.25))
      .replace(/__INK__/g,inkVar)
      .replace(/__ACCENT__/g,accent);
  }

  function domToPng(element){
    try{
      // HeadlessChrome 对 SVG foreignObject 的 drawImage 可能永久挂起；真实
      // 浏览器正常走下面的 DOM capture。审计环境下返回 null，不阻塞导出测试。
      if (/HeadlessChrome/.test(navigator.userAgent)) return Promise.resolve(null);
      var clone=element.cloneNode(true);
      // W5 微条三态（快照通道）：把 .kpi-compare 的方向折到 .kpi-card 类上——
      // foreignObject 里不可靠走 :has()/兄弟选择器，克隆期标注是唯一确定性通道。
      clone.querySelectorAll('.kpi-card').forEach(function(kc){
        var cmp=kc.querySelector('.kpi-compare');
        if(!cmp)return;
        kc.classList.add('has-cmp');
        if(cmp.classList.contains('is-up'))kc.classList.add('cmp-up');
        else if(cmp.classList.contains('is-down'))kc.classList.add('cmp-down');
      });
      var html=new XMLSerializer().serializeToString(clone);
      var svg='<svg xmlns="http://www.w3.org/2000/svg" width="'+Math.max(1,element.offsetWidth||640)+'" height="'+Math.max(1,element.offsetHeight||320)+'"><foreignObject width="100%" height="100%"><div xmlns="http://www.w3.org/1999/xhtml" style="background:#fff;font-family:Inter,-apple-system,BlinkMacSystemFont,\'PingFang SC\',\'Microsoft YaHei\',sans-serif;color:#1a1a1a"><style>'+snapshotCss()+'</style>'+html+'</div></foreignObject></svg>';
      var blob=new Blob([svg],{type:'image/svg+xml;charset=utf-8'});
      return new Promise(function(resolve,reject){
        var url=URL.createObjectURL(blob),img=new Image(),done=false;
        var cleanup=function(){if(done)return;done=true;clearTimeout(timer);URL.revokeObjectURL(url);};
        var timer=setTimeout(function(){cleanup();resolve(null);},3000);
        img.onload=function(){cleanup();var c=document.createElement('canvas');c.width=img.width;c.height=img.height;var ctx=c.getContext('2d');ctx.fillStyle='#fff';ctx.fillRect(0,0,c.width,c.height);ctx.drawImage(img,0,0);resolve(c.toDataURL('image/png'));};
        img.onerror=function(){cleanup();reject(new Error('svg image failed'));};
        img.src=url;
      });
    }catch(e){return Promise.resolve(null);}
  }
  function imageFor(item){
    var chart=item.kind==='chart'||item.kind==='table'?chartFor(item.id):null;
    if(chart&&item.kind==='chart'){var d=chartDataUrl(chart);if(d)return Promise.resolve(d);}
    // domToPng 的 SVG 光栅失败路径是 reject（img.onerror）；buildFiles 的三个
    // `await imageFor(item)` 都没有 catch —— 一格快照失败会把整份 PPTX 导出打成
    // unhandled rejection。图片是尽力而为（HeadlessChrome 路径同款返回 null）：
    // 失败 = 该卡不出图，绝不做垮整份导出。
    return domToPng(item.node).then(function(d){return d;}).catch(function(){return null;});
  }

  // ---------------- PPTX XML templates ---------------------------------------
  function contentTypes(chartCount,imageCount){
    var s='<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Default Extension="png" ContentType="image/png"/><Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/><Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/><Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/><Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>';
    for(var i=1;i<=chartCount;i++)s+='<Override PartName="/ppt/charts/chart'+i+'.xml" ContentType="application/vnd.openxmlformats-officedocument.drawingml.chart+xml"/>';
    for(var j=1;j<=imageCount;j++)s+='<Override PartName="/ppt/media/image'+j+'.png" ContentType="image/png"/>';
    s+='</Types>';return s;
  }
  function rootRels(slideCount){var s='<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/></Relationships>';return s;}
  function presentationXml(slideCount){var ids='';for(var i=1;i<=slideCount;i++)ids+='<p:sldId id="'+(255+i)+'" r:id="rId'+(i+1)+'"/>';return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"><p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst><p:sldIdLst>'+ids+'</p:sldIdLst><p:sldSz cx="12192000" cy="6858000"/><p:notesSz cx="6858000" cy="9144000"/></p:presentation>';}
  function presentationRels(slideCount){var s='<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/>';for(var i=1;i<=slideCount;i++)s+='<Relationship Id="rId'+(i+1)+'" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide'+(i)+'.xml"/>';s+='</Relationships>';return s;}
  function masterXml(){return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:sldMaster xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"><p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr></p:spTree></p:cSld><p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/><p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst></p:sldMaster>';}
  function masterRels(){return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/theme1.xml"/></Relationships>';}
  function layoutXml(){return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:sldLayout xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" type="blank" preserve="1"><p:cSld name="Blank"><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/></p:spTree></p:cSld><p:clrMapOvr><a:overrideClrMapping bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/></p:clrMapOvr></p:sldLayout>';}
  function themeXml(){return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Office"><a:themeElements><a:clrScheme name="Office"><a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1><a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1><a:dk2><a:srgbClr val="44546A"/></a:dk2><a:lt2><a:srgbClr val="E7E6E6"/></a:lt2><a:accent1><a:srgbClr val="4472C4"/></a:accent1><a:accent2><a:srgbClr val="ED7D31"/></a:accent2><a:accent3><a:srgbClr val="A5A5A5"/></a:accent3><a:accent4><a:srgbClr val="FFC000"/></a:accent4><a:accent5><a:srgbClr val="5B9BD5"/></a:accent5><a:accent6><a:srgbClr val="70AD47"/></a:accent6><a:hlink><a:srgbClr val="0563C1"/></a:hlink><a:folHlink><a:srgbClr val="954F72"/></a:folHlink></a:clrScheme><a:fontScheme name="Office"><a:majorFont><a:latin typeface="Microsoft YaHei"/><a:ea typeface="Microsoft YaHei"/><a:cs typeface=""/></a:majorFont><a:minorFont><a:latin typeface="Microsoft YaHei"/><a:ea typeface="Microsoft YaHei"/><a:cs typeface=""/></a:minorFont></a:fontScheme><a:fmtScheme name="Office"><a:fillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:fillStyleLst><a:lnStyleLst><a:ln w="9525"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln></a:lnStyleLst><a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle></a:effectStyleLst><a:bgFillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:bgFillStyleLst></a:fmtScheme></a:themeElements></a:theme>';}

  function textBody(text,x,y,w,h,size,bold,algn,color,anchor){
    // 共享页内 id 计数器：richCardShape 与 textBody 曾各用各的计数、且前者不
    // 回写后者 → 同页 cNvPr id 必然撞号（OOXML 要求页内唯一，PowerPoint 会弹
    // 「无法读取的内容」修复框）。
    textBody._id=(textBody._id||1)+1;richCardShape._id=textBody._id;
    // O4：run 显式微软雅黑（含标题/页脚/表格正文，theme 只作图表轴文字兜底）。
    var paras=String(text==null?'':text).split('\n').map(function(line){return '<a:p><a:r><a:rPr lang="zh-CN" sz="'+(size||1800)+'" b="'+(bold?1:0)+'"><a:solidFill><a:srgbClr val="'+(color||'1A1A1A')+'"/></a:solidFill><a:latin typeface="Microsoft YaHei"/><a:ea typeface="Microsoft YaHei"/></a:rPr><a:t>'+escapeXml(line)+'</a:t></a:r></a:p>';}).join('');
    // anchor='ctr' 时固定框高垂直居中（页标题带）、关 spAutoFit——自动适应会把
    // 框缩成一行文字高、居中失效。
    var bodyPr=anchor?('<a:bodyPr wrap="square" rtlCol="0" anchor="'+anchor+'"/>'):'<a:bodyPr wrap="square" rtlCol="0"><a:spAutoFit/></a:bodyPr>';
    return '<p:sp><p:nvSpPr><p:cNvPr id="'+textBody._id+'" name="Text"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr><a:xfrm><a:off x="'+x+'" y="'+y+'"/><a:ext cx="'+w+'" cy="'+h+'"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/><a:ln><a:noFill/></a:ln></p:spPr><p:txBody>'+bodyPr+'<a:lstStyle/>'+paras+'</p:txBody></p:sp>';
  }
  function picture(x,y,w,h,rid){
    return '<p:pic><p:nvPicPr><p:cNvPr id="'+(picture._id=(picture._id||1000)+1)+'" name="Picture"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr><p:blipFill><a:blip r:embed="'+rid+'"/><a:stretch><a:fillRect/></a:stretch></p:blipFill><p:spPr><a:xfrm><a:off x="'+x+'" y="'+y+'"/><a:ext cx="'+w+'" cy="'+h+'"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr></p:pic>';
  }
  function chartGraphic(x,y,w,h,rid){
    return '<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="'+(chartGraphic._id=(chartGraphic._id||2000)+1)+'" name="Chart"/><p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr><p:xfrm><a:off x="'+x+'" y="'+y+'"/><a:ext cx="'+w+'" cy="'+h+'"/></p:xfrm><a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/chart"><c:chart xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" r:id="'+rid+'"/></a:graphicData></a:graphic></p:graphicFrame>';
  }
  function tableXml(columns,rows,noteText){
    var grid='<a:tblGrid>'+columns.map(function(){return '<a:gridCol w="'+Math.round(9144000/Math.max(1,columns.length))+'" />';}).join('')+'</a:tblGrid>';
    // O6 皮肤（2026-09-26）：表头浅灰实底（对齐报告表格皮肤 #f6f5f4）、bandRow=0
    // 关掉 Office 内置蓝条纹；字体随 O4 显式微软雅黑。
    function rowXml(cells,header){var h=header?500000:350000;return '<a:tr h="'+h+'">'+cells.map(function(c){return '<a:tc><a:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:rPr lang="zh-CN" sz="'+(header?1400:1200)+'" b="'+(header?1:0)+'"><a:latin typeface="Microsoft YaHei"/><a:ea typeface="Microsoft YaHei"/></a:rPr><a:t>'+escapeXml(c)+'</a:t></a:r></a:p></a:txBody>'+(header?'<a:tcPr><a:solidFill><a:srgbClr val="F6F5F4"/></a:solidFill></a:tcPr>':'<a:tcPr/>')+'</a:tc>';}).join('')+'</a:tr>';}
    // O6 截断徽章：行集被 20 行截断时表尾追加 gridSpan 说明行——与 deck 降级徽章
    // 同一读者信任口径，投屏上绝不假装完整。
    var note=noteText?'<a:tr h="230000"><a:tc gridSpan="'+Math.max(1,columns.length)+'"><a:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:rPr lang="zh-CN" sz="900"><a:solidFill><a:srgbClr val="787671"/></a:solidFill><a:latin typeface="Microsoft YaHei"/><a:ea typeface="Microsoft YaHei"/></a:rPr><a:t>'+escapeXml(noteText)+'</a:t></a:r></a:p></a:txBody><a:tcPr/></a:tc></a:tr>':'';
    return '<a:tbl><a:tblPr firstRow="1" bandRow="0"><a:tableStyleId>{5C22544A-7EE6-4342-B048-85BDC9FD1C3A}</a:tableStyleId></a:tblPr>'+grid+rowXml(columns,true)+(rows||[]).map(function(r){return rowXml(r,false);}).join('')+note+'</a:tbl>';
  }
  function nativeTableGraphic(x,y,w,h,columns,rows,noteText){
    return '<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="'+(nativeTableGraphic._id=(nativeTableGraphic._id||3000)+1)+'" name="Table"/><p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr><p:xfrm><a:off x="'+x+'" y="'+y+'"/><a:ext cx="'+w+'" cy="'+h+'"/></p:xfrm><a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/table">'+tableXml(columns,rows,noteText)+'</a:graphicData></a:graphic></p:graphicFrame>';
  }

  function chartXml(type,categories,series,palette){
    // 图表语义保真（B4）：stackedBar 必须写 grouping=stacked（旧版恒 clustered →
    // 堆叠柱被导成簇状柱）；area 用真正的 areaChart（旧分支的 extra 是死变量，
    // 面积图被导成普通折线）；lineChart 的合法 grouping 是 standard（不是 clustered）。
    var chartType='barChart',grouping='clustered',barDir='';
    if(type==='bar'||type==='barChart'){chartType='barChart';grouping='clustered';barDir='<c:barDir val="col"/>';}
    else if(type==='stackedBar'){chartType='barChart';grouping='stacked';barDir='<c:barDir val="col"/>';}
    else if(type==='line'){chartType='lineChart';grouping='standard';}
    else if(type==='area'){chartType='areaChart';grouping='standard';}
    else if(type==='pie'||type==='donut'){chartType=type==='donut'?'doughnutChart':'pieChart';}
    else if(type==='scatter'){chartType='scatterChart';}
    function strRef(name){return '<c:tx><c:strRef><c:f>'+name+'</c:f><c:strCache><c:ptCount val="1"/><c:pt idx="0"><c:v>'+escapeXml(name)+'</c:v></c:pt></c:strCache></c:strRef></c:tx>';}
    function catRef(){return '<c:cat><c:strRef><c:f>Sheet1!$A$2:$A$'+(categories.length+1)+'</c:f><c:strCache><c:ptCount val="'+categories.length+'"/>'+categories.map(function(v,i){return '<c:pt idx="'+i+'"><c:v>'+escapeXml(v)+'</c:v></c:pt>';}).join('')+'</c:strCache></c:strRef></c:cat>';}
    function valRef(vals){return '<c:val><c:numRef><c:f>Sheet1!$B$2:$B$'+(vals.length+1)+'</c:f><c:numCache><c:formatCode>General</c:formatCode><c:ptCount val="'+vals.length+'"/>'+vals.map(function(v,i){return v==null?'':'<c:pt idx="'+i+'"><c:v>'+Number(v)+'</c:v></c:pt>';}).join('')+'</c:numCache></c:numRef></c:val>';}
    // O3 原生图表写报告色板（2026-09-26 拍板）：颜色从 live option 的主题色板
    // （opt.color，chart-adapter CHART_THEMES 烘入，chartData 透传 palette）逐系列
    // 取；系列级显式 itemStyle.color 优先。pie/donut 逐数据点 dPt（白描边分瓣，
    // 点级 itemStyle.color 再优先）。写不出色的系列不写 spPr，落回 PowerPoint
    // 主题色——绝不因色板缺项破文件。
    function serColor(i,s){
      var c=s&&s.color?normColor(s.color):null;
      if(!c&&palette&&palette[i]!=null)c=normColor(palette[i]);
      return c;
    }
    function spPrXml(c,lineToo){
      if(!c)return '';
      var sf='<a:solidFill><a:srgbClr val="'+c+'"/></a:solidFill>';
      return '<c:spPr>'+sf+(lineToo?'<a:ln>'+sf+'</a:ln>':'')+'</c:spPr>';
    }
    var ser='';
    var isPie=(type==='pie'||type==='donut');
    series.forEach(function(s,i){
      var vals=s.data||[];
      var col=serColor(i,s);
      var dpts='';
      if(isPie&&col){
        var pts=s.pointColors||[];
        for(var pi=0;pi<vals.length;pi++){
          // 点级色回退链：显式点色 → 主题色板**第 i 位**（ECharts 渲染期调色板
          // 不写回 option 的 data itemStyle，pointColors 恒 null——旧版直接落
          // serColor(0)=palette[0]，全扇区第一主题色）。板位也没有才落系列色。
          var pc=normColor(pts[pi])||normColor(palette&&palette[pi])||col;
          dpts+='<c:dPt><c:idx val="'+pi+'"/><c:bubble3D val="0"/><c:spPr><a:solidFill><a:srgbClr val="'+pc+'"/></a:solidFill><a:ln w="9525"><a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill></a:ln></c:spPr></c:dPt>';
        }
      }
      ser+='<c:ser><c:idx val="'+i+'"/><c:order val="'+i+'"/>'+strRef(s.name||('系列'+(i+1)))
        +spPrXml(col,!isPie&&(type==='line'||type==='area'||type==='scatter'))
        +dpts+catRef()+valRef(vals)+'</c:ser>';
    });
    var ax='';
    if(type!=='pie'&&type!=='donut')ax='<c:axId val="100"/><c:axId val="101"/>';
    var groupingXml=(type==='pie'||type==='donut'||type==='scatter')?'':'<c:grouping val="'+grouping+'"/>';
    // ECMA-376 §21.2.2.96（CT_BarChart）：grouping="stacked" 必须显式写
    // <c:overlap val="100"/>，否则 PowerPoint 按重叠 0 渲染堆叠段错位。
    // overlap 在序列里位于 ser 之后、axId 之前（见下方拼接位）；clustered /
    // standard 的语义默认就是 0，不写（lineChart/areaChart 的 grouping 也是
    // standard，同样不加）。
    var overlapXml=(chartType==='barChart'&&grouping==='stacked')?'<c:overlap val="100"/>':'';
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><c:chart><c:plotArea><c:layout/>'+'<c:'+chartType+'>'+barDir+groupingXml+ser+overlapXml+ax+'</c:'+chartType+'>'+((type!=='pie'&&type!=='donut')?'<c:catAx><c:axId val="100"/><c:scaling><c:orientation val="minMax"/></c:scaling><c:delete val="0"/><c:axPos val="b"/><c:crossAx val="101"/></c:catAx><c:valAx><c:axId val="101"/><c:scaling><c:orientation val="minMax"/></c:scaling><c:delete val="0"/><c:axPos val="l"/><c:crossAx val="100"/></c:valAx>':'')+'</c:plotArea><c:legend><c:legendPos val="r"/><c:overlay val="0"/></c:legend><c:plotVisOnly val="1"/></c:chart></c:chartSpace>';
  }

  function chartData(item){
    var chart=chartFor(item.id);if(!chart)return null;
    var host=document.getElementById(chart.elementId||chart.id);
    var opt=(global.echarts&&host&&global.echarts.getInstanceByDom(host)?global.echarts.getInstanceByDom(host).getOption():chart.option)||{};
    var vt=chart.viewType||item.kind;
    // O3：主题色板（opt.color 由 chart-adapter setChartTheme 烘入）与系列级显式
    // itemStyle.color 随数据透传给 chartXml——原生图表颜色=报告主题色。
    var palette=Array.isArray(opt.color)?opt.color:null;
    if(vt==='table'||vt==='table.detail')return null;
    // chartXml 不支持的类型（funnel 等）返回 null：调用方回退为图片，
    // 绝不把未识别类型静默编造成柱状图（B4）。
    if(vt==='funnel')return null;
    if(vt==='pie'||vt==='donut'){var s=(opt.series&&opt.series[0])||{};var data=(s.data||[]).filter(function(d){return d!=null;}).map(function(d){return (d&&typeof d==='object')?{name:d.name,value:(d.value==null?null:d.value),pc:(d.itemStyle&&d.itemStyle.color)||null}:{name:String(d),value:0,pc:null};});return {type:vt,categories:[],series:[{name:s.name||'值',data:data.map(function(d){return d.value;}),pointColors:data.map(function(d){return d.pc;})}],labels:data.map(function(d){return d.name;}),palette:palette};}
    if(vt==='scatter'){var ss=(opt.series||[]).map(function(s){return {name:s.name||'',color:s.itemStyle&&s.itemStyle.color,data:(s.data||[]).map(function(d){return Array.isArray(d)?d[1]:0;})};});return {type:'scatter',categories:[],series:ss,palette:palette};}
    var cats=(opt.xAxis&&opt.xAxis[0]&&opt.xAxis[0].data)||(opt.xAxis&&opt.xAxis.data)||[];
    // ``typeof null === 'object'`` — a derived measure legitimately produces
    // null/empty points (no previous period), and a missing point must stay
    // missing (gap in the deck) instead of crashing the export or plotting 0.
    var series=(opt.series||[]).map(function(s){return {name:s.name||'',color:s.itemStyle&&s.itemStyle.color,data:(s.data||[]).map(function(d){
      if(d==null)return null;
      if(Array.isArray(d))return d[1];
      if(typeof d==='object')return d.value==null?null:d.value;
      return d;
    })};});
    // stackedBar 透传（chartXml 写 grouping=stacked）；bar/line/area 各归其位；
    // 其余未识别类型（composite 等）返回 null → 回退图片，不硬编造成 bar。
    var type=vt==='stackedBar'?'stackedBar':vt==='area'?'area':vt==='line'?'line':vt==='bar'?'bar':null;
    if(!type)return null;
    return {type:type,categories:cats.map(String),series:series,palette:palette};
  }

  // 段落文本 → 纯文本（B6）：fmt:true 段落是序列化 HTML（<b>/<mark>），与
  // editor-runtime richPlain 同一口径（innerHTML → textContent + 空白折叠）；
  // 作者侧 markdown token（**粗体** / ==高亮== / `代码` / ~~删除~~ / *斜体* / 链接）
  // 剥成纯文本——不剥就会以标记字面出现在幻灯片里。
  function plainText(s){
    var d=document.createElement('div');d.innerHTML=String(s==null?'':s);
    var t=(d.textContent||'').replace(/\s+/g,' ').trim();
    return t.replace(/\[([^\]]*)\]\((https?:\/\/[^)\s]+)\)/g,'$1')
      .replace(/\*\*([\s\S]+?)\*\*/g,'$1').replace(/~~([\s\S]+?)~~/g,'$1').replace(/==([\s\S]+?)==/g,'$1')
      .replace(/`([^`]+)`/g,'$1').replace(/\*([^*\s][^*]*)\*/g,'$1').replace(/_([^_\s]|[^_]*?[^_\s])_/g,'$1');
  }
  function cardTextParagraphs(item){
    // B1 筛选卡：deck 上是「生效筛选 N 条 + chips」徽章，PPTX 用同一数据源（filterCardChips）
    // 输出同样口径的纯文本——两侧不可能各写一套。
    var bFilter=blockFor(item.id);
    if(bFilter&&(bFilter.component==='filter'||item.id==='block-filter')){
      var rt0=global.AIReportRuntime;
      var chips=(rt0&&rt0.filterCardChips?rt0.filterCardChips(item.id):null)||[];
      var outF=['生效筛选 '+chips.length+' 条'];
      chips.forEach(function(c){outF.push(c.col+'：'+(c.vals||[]).join('、'));});
      return outF;
    }
    var b=blockFor(item.id);if(b&&Array.isArray(b.paragraphs)&&b.paragraphs.length)return b.paragraphs.map(function(p){return plainText(p.text||'');}).filter(Boolean);
    var k=kpiFor(item.id);
    if(k){
      // KPI 数值优先读 live DOM（.kpi-card .value，含单位后缀与格式化）；
      // 取不到再回退 manifest 的 k.value（原始值）。
      var out=[];if(k.label)out.push(plainText(k.label));
      var vEl=item.node&&item.node.querySelector?item.node.querySelector('.kpi-card .value'):null;
      var v=vEl?String(vEl.textContent||'').trim():(k.value!=null?String(k.value):'');
      if(v)out.push(v);
      return out;
    }
    if(item.title)return [plainText(item.title)];
    return [];
  }

  // ---------------- C2 文本卡段落保真（paragraphs → 原生 PPTX 文本框）----------------
  // 格式条产出的富文本（字色/加粗/列表/对齐/气泡/引用/代码块/高亮块）在 PPTX 里不再
  // 拍平：level→字号阶梯、b/strong→b=1、mark/底色→a:highlight、ul/ol/task→buChar/
  // buAutoNum/勾选前缀、align→algn、indent→marL、气泡→加粗+底色行。plainText 降级为
  // 兜底（richCardShape 返回 null 时走 cardTextParagraphs）。字号阶梯以本表为准
  // （投影片放大：h1 28 / h2 20 / h3 16 / body 13 / 深级 13→11.5，单位 pt×100）
  // ——比 Web 端 --text-* 阶梯的 h2/h3 各抬一档，属导出侧的放映可读性决策。
  var PARA_SIZE={h1:2800,h2:2000,h3:1600,h4:1300,h5:1250,h6:1200,h7:1200,h8:1150,h9:1150,body:1300,quote:1300,code:1200,hlblock:1300};
  var PARA_COLOR={h1:'1A1A1A',h2:'1A1A1A',h3:'1A1A1A',h4:'1A1A1A',h5:'5D5B54',h6:'5D5B54',h7:'787671',h8:'787671',h9:'787671',body:'5D5B54',quote:'5D5B54',code:'1A1A1A',hlblock:'1A1A1A'};
  var MARK_BG='F9E79F',HLBLOCK_BG='F1EFFC',BUBBLE_BG='EDE9E4';
  var NAMED_COLORS={white:'FFFFFF',black:'000000',red:'FF0000',green:'008000',blue:'0000FF',yellow:'FFFF00',orange:'FFA500',purple:'800080',gray:'808080',grey:'808080'};
  function mdInlineToHtml(text){
    var rt=global.AIReportRuntime;
    try{if(rt&&rt.mdInlineText)return rt.mdInlineText(String(text==null?'':text));}catch(e){}
    return null; // 调用方退回纯文本
  }
  // css 颜色 → RRGGBB（调色板 hex / rgba() 归一化；全透明 = 无底色）。与编辑器
  // 序列化器的颜色白名单口径一致（AGENTS「行内 style 归一化」）。
  function normColor(c){
    if(!c)return null;
    c=String(c).trim();
    if(c==='transparent'||c==='inherit')return null;
    if(c.charAt(0)==='#'){
      var h=c.slice(1);
      if(h.length===3)h=h.split('').map(function(x){return x+x;}).join('');
      return /^[0-9a-fA-F]{6}$/.test(h)?h.toUpperCase():null;
    }
    var m=/^rgba?\(([^)]+)\)$/i.exec(c);
    if(m){
      var parts=m[1].replace(/\//g,' ').split(/[\s,]+/).filter(Boolean).map(parseFloat);
      if(parts.length>=3&&parts.slice(0,3).every(function(x){return isFinite(x);})){
        if(parts.length>=4&&parts[3]===0)return null;
        return parts.slice(0,3).map(function(x){var v=Math.max(0,Math.min(255,Math.round(x)));return ('0'+v.toString(16)).slice(-2).toUpperCase();}).join('');
      }
    }
    return NAMED_COLORS[c.toLowerCase()]||null;
  }
  // HTML → runs（fmt:true 的信任 HTML 与 mdInlineText 产出的 markdown HTML 走同一条
  // 白名单通道；其余标签剥掉、文本保留）。\n 与 <br> 都产出 {br:true} 行内换行。
  function htmlToRuns(html){
    var runs=[];
    var d=document.createElement('div');
    d.innerHTML=html||'';
    function styleOf(el,base){
      var st={};for(var k in base)st[k]=base[k];
      var tag=el.tagName;
      if(tag==='B'||tag==='STRONG')st.b=1;
      else if(tag==='I'||tag==='EM')st.i=1;
      else if(tag==='U')st.u=1;
      else if(tag==='S'||tag==='STRIKE'||tag==='DEL')st.s=1;
      else if(tag==='CODE')st.code=1;
      else if(tag==='MARK')st.mark=1;
      else if(tag==='A'){st.u=1;st.link=1;}
      var cs=el.style||{};
      var c=normColor(cs.color);if(c)st.color=c;
      var bg=normColor(cs.backgroundColor);
      if(bg){st.mark=1;st.bg=bg;}
      return st;
    }
    function pushText(t,st){
      var parts=String(t).split('\n');
      parts.forEach(function(seg,i){
        if(i>0)runs.push({br:true});
        if(!seg)return;
        var last=runs[runs.length-1];
        var same=last&&!last.br&&last.text!=null&&['b','i','u','s','code','mark','link'].every(function(k){return !!last[k]===!!st[k];})
          &&(last.bg||null)===(st.bg||null)&&(last.color||null)===(st.color||null);
        if(same)last.text+=seg;
        else runs.push({text:seg,b:st.b,i:st.i,u:st.u,s:st.s,code:st.code,mark:st.mark,link:st.link,bg:st.bg||null,color:st.color||null});
      });
    }
    (function walk(node,st){
      for(var n=node.firstChild;n;n=n.nextSibling){
        if(n.nodeType===3)pushText(n.nodeValue,st);
        else if(n.nodeType===1){
          if(n.tagName==='BR'){runs.push({br:true});continue;}
          walk(n,styleOf(n,st));
        }
      }
    })(d,{});
    return runs;
  }
  function runXml(r,def){
    if(r.br)return '<a:br/>';
    var bold=def.bold||r.b;
    var color=r.color||((r.b&&def.color==='5D5B54')?'1A1A1A':def.color);
    var rPr='<a:rPr lang="zh-CN" sz="'+def.size+'"'+(bold?' b="1"':'')+
      ((def.italic||r.i)?' i="1"':'')+(r.s?' strike="1"':'')+(r.u||r.link?' u="sng"':'')+'>';
    // 链接默认色随报告主题（--accent 导出期实换，coverVarHex 同 cardFillInk 通道）；
    // 旧写死 '5645D4' 会让 non-default 主题的 PPTX 链接色与报告脱节。
    rPr+='<a:solidFill><a:srgbClr val="'+(r.link&&!r.color?coverVarHex('--accent','5645D4'):color)+'"/></a:solidFill>';
    if(r.mark)rPr+='<a:highlight><a:srgbClr val="'+(r.bg||MARK_BG)+'"/></a:highlight>';
    // O4 中文字体（2026-09-26 拍板）：全部 run 显式 latin+ea=微软雅黑（代码段保持
    // Consolas）——旧版只有 code 段写字体，其余落 theme（Aptos）→ 中文由查看器
    // 随机兜底，与报告「微软雅黑」口径漂移。
    if(def.mono||r.code)rPr+='<a:latin typeface="Consolas"/><a:ea typeface="Microsoft YaHei"/>';
    else rPr+='<a:latin typeface="Microsoft YaHei"/><a:ea typeface="Microsoft YaHei"/>';
    rPr+='</a:rPr>';
    return '<a:r>'+rPr+'<a:t>'+escapeXml(r.text)+'</a:t></a:r>';
  }
  function richParaXml(p,ink){
    var lvl=p.level||'body';
    var def={
      size:PARA_SIZE[lvl]||1300,
      bold:lvl!=='body'&&lvl!=='quote'&&lvl!=='code'&&lvl!=='hlblock',
      italic:lvl==='quote',
      mono:lvl==='code',
      color:ink||PARA_COLOR[lvl]||'1A1A1A'
    };
    var runs;
    var src=p.fmt?String(p.text||''):mdInlineToHtml(p.text);
    runs=src?htmlToRuns(src):[{text:String(p.text==null?'':p.text)}];
    // 段落级样式 → 逐 run 注入（PPTX 没有段落底色）：hlblock=浅紫底、bubble=加粗+浅灰底。
    if(lvl==='hlblock'||p.bubble)runs.forEach(function(r){if(!r.br){r.mark=1;if(!r.bg)r.bg=p.bubble?BUBBLE_BG:HLBLOCK_BG;}if(p.bubble&&!r.b)r.b=1;});
    var pPrAttrs='';
    var indPx=(Number(p.indent)||0)*24;
    var marL=indPx*EMU_PER_PX;
    var hanging='';
    if(p.list==='ul'||p.list==='ol'){marL+=342900;hanging=' indent="-228600"';}
    else if(lvl==='quote'||lvl==='code'||lvl==='hlblock'){marL+=182880;}
    if(marL)pPrAttrs+=' marL="'+Math.round(marL)+'"';
    pPrAttrs+=hanging;
    var algn=p.align==='center'?'ctr':p.align==='right'?'r':p.align==='justify'?'just':'';
    if(algn)pPrAttrs+=' algn="'+algn+'"';
    var pPr='<a:pPr'+pPrAttrs+'>';
    if(p.list==='ul')pPr+='<a:buFont typeface="Arial"/><a:buChar char="•"/>';
    else if(p.list==='ol')pPr+='<a:buFont typeface="Arial"/><a:buAutoNum type="arabicPeriod"/>';
    pPr+='</a:pPr>';
    var body='';
    if(p.list==='task'){
      body+='<a:r><a:rPr lang="zh-CN" sz="'+def.size+'" b="1"><a:solidFill><a:srgbClr val="'+(p.done?'1AAE39':'787671')+'"/></a:solidFill></a:rPr><a:t>'+escapeXml(p.done?'☑ ':'☐ ')+'</a:t></a:r>';
    }
    runs.forEach(function(r){body+=runXml(r,def);});
    return '<a:p>'+pPr+body+'</a:p>';
  }
  // O2 文本卡视觉（2026-09-26 拍板）：cardColor 全色板 → PPTX 实底/描边/圆角，
  // 与 deck/PDF 的 .txc-* 观感对齐。色值镜像 SNAPSHOT_CSS 的 txc-*（accent 系
  // 导出期从 --accent 实换）；封面/深色卡恒「渐变实底 + 反白墨」——封面走 W5 后
  // 的深墨口径（cover-from→via，165deg），旧「accent 实底」退役；txc-dark =
  // 120deg from→via(55%)→to。透明(none) 与 plainText 开关 → 无框。
  // 返回 null = 无框（noFill + rect + 无边线）；否则 {fill, line, ink, geom}，
  // fill/line 是完整 XML 片段，ink=null 表示用默认深墨（粉彩底不反白）。
  var CARD_TINTS={white:['FFFFFF','E5E3DF'],soft:['FAFAF9','E5E3DF'],green:['EEF7EF','CFE6D4'],amber:['FDF6E4','F0DFAE'],red:['FDF0ED','F2CDC4']};
  // coverVarHex 的返回值带 '#'（CSSOM 原样/回退字面量），不能直接进 srgbClr val——
  // 统一过 normColor 归一成无 # 大写 6 位 hex；归一失败落回调用方给的兜底字面量。
  function hexVar(name,d){
    return normColor(coverVarHex(name,'#'+d))||d;
  }
  function mixWhiteHex(hex,t){
    var h=String(hex||'').replace(/^#/,'');
    if(!/^[0-9a-fA-F]{6}$/.test(h))return null; // normColor 不收裸 hex（白名单语义），这里自行归一
    var r=parseInt(h.slice(0,2),16),g=parseInt(h.slice(2,4),16),b=parseInt(h.slice(4,6),16);
    return [r,g,b].map(function(c){var v=Math.round(c+(255-c)*t).toString(16);return v.length<2?'0'+v:v;}).join('').toUpperCase();
  }
  function cardGradFill(stops,ang){
    var gs=stops.map(function(s){return '<a:gs pos="'+s[0]+'"><a:srgbClr val="'+s[1]+'"/></a:gs>';}).join('');
    return '<a:gradFill><a:gsLst>'+gs+'</a:gsLst><a:lin ang="'+ang+'" scaled="1"/></a:gradFill>';
  }
  function cardFillInk(b){
    if(!b)return null;
    var ink=hexVar('--cover-ink','FFFFFF');
    if(b.id==='block-cover'){
      return {fill:cardGradFill([[0,hexVar('--cover-from','070F24')],[100000,hexVar('--cover-via','0A1530')]],4500000),line:'',ink:ink,geom:'roundRect'};
    }
    if(b.cardColor==='dark'||b.textStyle==='banner'){
      return {fill:cardGradFill([[0,hexVar('--cover-from','070F24')],[55000,hexVar('--cover-via','0A1530')],[100000,hexVar('--cover-to','1A2A52')]],1800000),line:'',ink:ink,geom:'roundRect'};
    }
    if(plainTextOn())return null;
    var c=b.cardColor==='default'?'white':(b.cardColor||'white');
    if(c==='none')return null;
    var pair=(c==='accent')
      ?(function(){var a=hexVar('--accent','5645D4');return [mixWhiteHex(a,0.92),mixWhiteHex(a,0.76)];})()
      :CARD_TINTS[c];
    if(!pair)return null; // 未知色值保守无框，绝不编色
    return {fill:'<a:solidFill><a:srgbClr val="'+pair[0]+'"/></a:solidFill>',
      line:'<a:ln w="9525"><a:solidFill><a:srgbClr val="'+pair[1]+'"/></a:solidFill></a:ln>',
      ink:null,geom:'roundRect'};
  }
  // 圆角对齐 Web --radius 12px：roundRect adj = 12px(=114300EMU)/min(w,h)×100000，
  // 钳 [0,50000]（细高卡上 adj 过大会把对边挤成胶囊）。
  function roundRectAdj(w,h){
    var min=Math.min(w,h);if(!(min>0))return 0;
    return Math.max(0,Math.min(50000,Math.round(114300/min*100000)));
  }
  function coverVarHex(name,d){
    try{var v=getComputedStyle(document.documentElement).getPropertyValue(name)||'';
      v=String(v).trim();
      if(/^#[0-9a-fA-F]{6}$/.test(v))return v.toUpperCase();}catch(_e){}
    return d;
  }
  function richCardShape(item,x,y,w,h){
    var b=blockFor(item.id);
    if(!b||!Array.isArray(b.paragraphs)||!b.paragraphs.length)return null;
    if(b.component==='filter'||item.id==='block-filter')return null; // 筛选卡走纯文本徽章
    var ps=b.paragraphs.filter(function(p){return p&&(p.fmt?plainText(p.text||''):String(p.text==null?'':p.text).trim());});
    if(!ps.length)return null;
    richCardShape._id=textBody._id=(textBody._id||1)+1;
    var vis=cardFillInk(b);
    var inkHex=vis?vis.ink:null;
    var bodyXml=ps.map(function(p){return richParaXml(p,inkHex);}).join('');
    var fill=vis?vis.fill:'<a:noFill/>';
    var ln=vis&&vis.line?vis.line:'<a:ln><a:noFill/></a:ln>';
    var geom=vis?vis.geom:'rect';
    var av=geom==='roundRect'?('<a:avLst><a:gd name="adj" fmla="val '+roundRectAdj(w,h)+'"/></a:avLst>'):'<a:avLst/>';
    return '<p:sp><p:nvSpPr><p:cNvPr id="'+richCardShape._id+'" name="RichText"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'+
      '<p:spPr><a:xfrm><a:off x="'+x+'" y="'+y+'"/><a:ext cx="'+w+'" cy="'+h+'"/></a:xfrm>'+
      '<a:prstGeom prst="'+geom+'">'+av+'</a:prstGeom>'+fill+ln+'</p:spPr>'+
      // O10：bodyPr inset 对齐 Web 卡 padding（tx-card 12px 16px），文本起点与 deck 同位。
      '<p:txBody><a:bodyPr wrap="square" rtlCol="0" anchor="t" lIns="152400" tIns="114300" rIns="152400"><a:normAutofit/></a:bodyPr><a:lstStyle/>'+bodyXml+'</p:txBody></p:sp>';
  }
  // server 报告（schema-only，editor.data 无行）的表格卡：先经 adapter 拉数再生成
  // 原生表格（B6）——明细走 fetchDetailRows、聚合走 aggregate；拉不到返回 null，
  // 由调用方回退图片（绝不导出只剩表头的空表）。
  async function tableData(item){
    var chart=chartFor(item.id);if(!chart)return null;var data=chart.editor&&chart.editor.data||{};
    var cols=data.columns||[];var rows=data.rows||[];
    if(rows.length)return {columns:cols.map(String),rows:rows.slice(0,30).map(function(r){return (r||[]).map(function(v){return v==null?'':String(v);});})};
    var adapter=global.AIReportChartAdapter;
    try{
      var tcfg=(chart.editor&&chart.editor.table)||{};
      var mode=tcfg.mode||'aggregate';
      if(mode==='detail'){
        if(!adapter||!adapter.fetchDetailRows)return null;
        var dcols=(Array.isArray(tcfg.detailFields)&&tcfg.detailFields.length?tcfg.detailFields:cols).slice(0,12);
        if(!dcols.length)return null;
        var res=await adapter.fetchDetailRows(data,{columns:dcols,page:1,pageSize:30});
        var drows=(res&&res.rows)||[];
        if(!drows.length)return null;
        return {columns:dcols.map(String),rows:drows.map(function(r){return (r||[]).map(function(v){return v==null?'':String(v);});})};
      }
      if(!adapter||!adapter.aggregate)return null;
      var dim=data.dimension||(data.dimensions&&data.dimensions[0]);
      var measures=data.measures||[];
      if(!dim||!measures.length)return null;
      var entries=await adapter.aggregate(data,dim,measures,(data.aggregations&&typeof data.aggregations==='object'&&!Array.isArray(data.aggregations))?data.aggregations:data.aggregation||'sum');
      entries=(entries||[]).slice(0,30);
      if(!entries.length)return null;
      return {columns:[String(dim)].concat(measures.map(String)),rows:entries.map(function(e){return [String(e.label)].concat((e.values||[]).map(function(v){return v==null?'':String(v);}));})};
    }catch(e){return null;}
  }

  async function buildFiles(mode){
    var pres=global.AIReportPresentation||{};
    var deck=pres.createDeckDOM?pres.createDeckDOM():null;if(!deck)throw new Error('无法生成汇报分页');
    document.body.appendChild(deck.container);deck.container.style.position='fixed';deck.container.style.left='-12000px';deck.container.style.top='0';
    deck.container.style.zoom='1';deck.container.style.transform='';
    deck.container.querySelectorAll('.ai-pres-slide').forEach(function(s){s.style.display='block';});
    await new Promise(function(r){requestAnimationFrame(function(){requestAnimationFrame(r);});});
    // 离屏 deck 容器必须**任何路径**都摘掉：导出中途抛错（实例已被 dispose 后
    // getOption、单格快照失败等）曾把 position:fixed 的旧 deck 永远留在 body
    // 里——泄漏 + 下次导出再叠一个（第六轮审视挂账项）。
    try{
    var files=[];var slideCount=deck.slides.length;var chartParts=[],images=[];
    for(var si=0;si<deck.slides.length;si++){
      // OOXML 的 cNvPr id 只要求**页内**唯一：形状计数器逐页归位（textBody 与
      // richCardShape 共享 textBody._id；picture/chart/table 各有独立空间），
      // 跨千页递增的理论碰撞从根上消除。
      textBody._id=0;picture._id=1000;chartGraphic._id=2000;nativeTableGraphic._id=3000;
      var slide=deck.slides[si],rels=[{Id:'rId1',Type:'http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout',Target:'../slideLayouts/slideLayout1.xml'}];
      var spTree='<p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>';
      // O7 页标题（2026-09-26）：TITLE_H 已对齐 deck 实际标题带（1280 基准
      // titleRows=2×48=96px），标题框占满整带 anchor=ctr 垂直居中——旧版 56px
      // 框顶在 y≈10px，比 deck 标题基线偏高。
      var titleH=Math.round(pres.constants.TITLE_H*EMU_PER_PX);
      spTree+=textBody(slide.title||'报告页',100000,0,11172000,titleH,null,true,null,null,'ctr');
      for(var ci=0;ci<slide.cards.length;ci++){
        var item=slide.cards[ci],node=item.node,rect=node.getBoundingClientRect(),slideRect=slide.root.getBoundingClientRect();
        // 宽档位（pageWidth 1440/1600/1920）下 deck 页比 1280×720px 画布宽：
        // 卡片坐标恒按 k 缩放到画布，否则画布右侧/底部内容在 PowerPoint 里被
        // 硬裁（1280 档 k=1，产物不变；窄视口 k>1 放大回画布，同样正确）。
        var k=(slideRect.width>0)?(SLIDE_W/slideRect.width):1;
        var x=Math.max(0,Math.round((rect.left-slideRect.left)*k*EMU_PER_PX)),y=Math.max(0,Math.round((rect.top-slideRect.top)*k*EMU_PER_PX)),w=Math.round(rect.width*k*EMU_PER_PX),h=Math.round(rect.height*k*EMU_PER_PX);
        if(mode==='native'){
          var data=chartData(item);
          if(data&&(data.type==='bar'||data.type==='stackedBar'||data.type==='line'||data.type==='area'||data.type==='pie'||data.type==='donut')){
            var idx=chartParts.length+1;chartParts.push({id:item.id,data:data});rels.push({Id:'rId'+(rels.length+1),Type:'http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart',Target:'../charts/chart'+idx+'.xml'});spTree+=chartGraphic(x,y,w,h,'rId'+(rels.length));
          }else if(item.kind==='table'){
            var td=await tableData(item);
            if(td&&td.columns.length){
              // O6 截断徽章：总数真源 = deck 克隆 DOM 的分页器/命中徽标
              // （presentation.js deckTableTotalRowsOf，与 deck 降级徽章同一取法）；
              // 拿不到总数且确实截满 20 行时只报「显示前 20 行」——绝不把服务端
              // 拉数上限（pageSize 30）当总行数编「共 N 行」。
              var shown=Math.min(20,td.rows.length);
              var totalN=null;
              try{totalN=(pres._internals&&pres._internals.deckTableTotalRowsOf)?pres._internals.deckTableTotalRowsOf(item.node,item.id):null;}catch(_tne){}
              var noteTxt='';
              if(totalN&&totalN>shown)noteTxt='共 '+totalN+' 行（显示前 '+shown+' 行）';
              else if(!totalN&&td.rows.length>shown)noteTxt='显示前 '+shown+' 行';
              spTree+=nativeTableGraphic(x,y,w,h,td.columns,td.rows.slice(0,20),noteTxt);
            }
            else{
              // 拉不到数（server 报告无行且聚合失败）：回退图片，不导出空表头表。
              try{console.warn('[pptx] 原生版：表格卡拉数失败，已回退为图片：'+item.id);}catch(_e){}
              var tfimg=await imageFor(item);if(tfimg){var tfid=images.length+1;images.push(tfimg);rels.push({Id:'rId'+(rels.length+1),Type:'http://schemas.openxmlformats.org/officeDocument/2006/relationships/image',Target:'../media/image'+tfid+'.png'});spTree+=picture(x,y,w,h,'rId'+(rels.length));}
            }
          }
          else{
            if(item.kind==='chart'){try{console.warn('[pptx] 原生版不支持的图表类型（'+(((chartFor(item.id)||{}).viewType)||'?')+'），已回退为图片：'+item.id);}catch(_e2){}}
            // C2：文本卡走段落保真（level 阶梯/加粗/列表/对齐/底色）；其余（筛选卡徽章、
            // KPI、无段落卡）走纯文本行。
            var rich=richCardShape(item,x,y,w,h);
            if(rich){spTree+=rich;}
            else{
              var ps=cardTextParagraphs(item);
              if(ps.length){spTree+=textBody(ps.join('\n'),x,y,w,h,null,false);}
              else{var img=await imageFor(item);if(img){var iid=images.length+1;images.push(img);rels.push({Id:'rId'+(rels.length+1),Type:'http://schemas.openxmlformats.org/officeDocument/2006/relationships/image',Target:'../media/image'+iid+'.png'});spTree+=picture(x,y,w,h,'rId'+(rels.length));}}
            }
          }
        }else{
          var richImg=richCardShape(item,x,y,w,h);
          if(richImg){spTree+=richImg;}
          else{
            var text=cardTextParagraphs(item);
            if(text.length){spTree+=textBody(text.join('\n'),x,y,w,h,null,false);}
            else{var img2=await imageFor(item);if(img2){var ii=images.length+1;images.push(img2);rels.push({Id:'rId'+(rels.length+1),Type:'http://schemas.openxmlformats.org/officeDocument/2006/relationships/image',Target:'../media/image'+ii+'.png'});spTree+=picture(x,y,w,h,'rId'+(rels.length));}}
          }
        }
      }
      // A4 页脚页码：与 deck/放映/PDF 同一份 N/M（deck.slides 序号）。
      // O7（2026-09-26）：右缘对齐 16px（12192000-152400），旧坐标右缩 ~118px，
      // 与 deck/PDF 的 .ai-pres-pageno right:16px 不齐。
      var footRight=12192000-152400;
      spTree+=textBody((si+1)+' / '+slideCount,footRight-1200000,6858000-330000,1200000,280000,1000,false,'r','787671');
      // C3 数据截至：与 deck 页脚（presentation.js slideEl「 · 数据截至 X」）同一
      // 读取口径（manifest.meta.dataAsOf，可能不存在——不存在整段省略，页码文本框
      // 几何不动，无该字段的报告产物不受影响）。独立右对齐文本框放在页码左侧。
      var asOfTxt=metaDataAsOfText();
      if(asOfTxt)spTree+=textBody('数据截至 '+asOfTxt,footRight-1260000-2400000,6858000-330000,2400000,280000,1000,false,'r','787671');
      var slideXml='<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"><p:cSld>'+spTree+'</p:spTree></p:cSld><p:clrMapOvr><a:overrideClrMapping bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/></p:clrMapOvr></p:sld>';
      files.push({name:'ppt/slides/slide'+(si+1)+'.xml',data:slideXml});
      files.push({name:'ppt/slides/_rels/slide'+(si+1)+'.xml.rels',data:'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'+rels.map(function(r){return '<Relationship Id="'+r.Id+'" Type="'+r.Type+'" Target="'+r.Target+'"/>';}).join('')+'</Relationships>'});
    }
    files.push({name:'[Content_Types].xml',data:contentTypes(chartParts.length,images.length)});
    files.push({name:'_rels/.rels',data:rootRels(slideCount)});
    files.push({name:'ppt/presentation.xml',data:presentationXml(slideCount)});
    files.push({name:'ppt/_rels/presentation.xml.rels',data:presentationRels(slideCount)});
    files.push({name:'ppt/slideMasters/slideMaster1.xml',data:masterXml()});
    files.push({name:'ppt/slideMasters/_rels/slideMaster1.xml.rels',data:masterRels()});
    files.push({name:'ppt/slideLayouts/slideLayout1.xml',data:layoutXml()});
    files.push({name:'ppt/theme/theme1.xml',data:themeXml()});
    chartParts.forEach(function(cp,i){var cats=(cp.data.type==='pie'||cp.data.type==='donut')?cp.data.labels:cp.data.categories;files.push({name:'ppt/charts/chart'+(i+1)+'.xml',data:chartXml(cp.data.type,cats||[],cp.data.series,cp.data.palette)});});
    images.forEach(function(img,i){files.push({name:'ppt/media/image'+(i+1)+'.png',data:dataUrlToBytes(img)});});
    return files;
    }finally{
      if(deck.container&&deck.container.parentNode){try{deck.container.parentNode.removeChild(deck.container);}catch(e){}}
      deck.container=null;
    }
  }
  function dataUrlToBytes(url){var b64=url.split(',')[1]||'';var bin=atob(b64);var out=new Uint8Array(bin.length);for(var i=0;i<bin.length;i++)out[i]=bin.charCodeAt(i);return out;}

  async function exportImage(){var files=await buildFiles('image');download(await buildZip(files),(manifest().reportId||'report')+'-图片版.pptx');}
  async function exportNative(){var files=await buildFiles('native');download(await buildZip(files),(manifest().reportId||'report')+'-原生版.pptx');}

  global.AIReportPPTX={exportImage:exportImage,exportNative:exportNative,_buildFiles:buildFiles,_buildZip:buildZip,_chartXml:chartXml,_chartData:chartData,_plainText:plainText,_snapshotCss:SNAPSHOT_CSS,_richCardShape:richCardShape,_htmlToRuns:htmlToRuns,_normColor:normColor,_paraSize:PARA_SIZE};
})(typeof window!=='undefined'?window:this);
