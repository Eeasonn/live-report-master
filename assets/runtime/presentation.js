(function(global){
  'use strict';

  // ---- 汇报模式 = 单页 grid + 16:9 定位框（不复制 / 不移动卡片） -----------
  //
  // 核心想法：所有体验跟单页 grid 完全一致；唯一的差别是 grid 上叠加一套
  // 16:9 虚线矩形（slide frame），让用户知道“按 PPT 画布看每页会被分成哪几块”。
  // 卡片仍然在原 grid 里，由 GridStack 管理拖拽 / 拉伸 / 大小 / 重排。卡片
  // 跨 frame 移动也是 GridStack 自然行为，不需特别处理。
  //
  // 这里不再用“克隆 innerHTML”也不做“MOVE DOM 节点”——都是历史方案的痕迹。
  // 唯一新建的 DOM：套在 grid 上的 .ai-pres-slide-frames 容器（绝对定位的虚线
  // 框 + 右上角「第 N 页」chip），以及 PPT 预览/放映时的全屏 deck（read-only
  // 翻页用，跟编辑互不干扰）。

  var SLIDE_ASPECT=9/16;   // 16:9 → 高 = 宽 × 9/16
  var SLIDE_W_DEFAULT=1280; // 与单页 grid 的 max-width 同步
  function fitDeckScale(){
    // 不做 CSS zoom 反向补偿：deck 建在 grid 当前宽度上（buildDeckDOM 已设
    // container.style.width=geo.pageW），浏览器缩放会自然地把整个 deck
    // （框+内容+文字）一起缩放，与顶部编辑条一致。此函数仅作为兼容入口保留。
  }
  var PAD_Y=22, CARD_GAP=12;
  var deckDom=null, active=false, playing=false, current=0;
  // R13 快赢（2026-09-25）：「从单报告模式导出 PDF」会先自动进入汇报模式再打印
  //（editor-runtime 的 exportPdfFromShareCenter 经 markAutoForPdf 打标记；用户
  // 手动进入不设）——打印/取消（afterprint 或 60s 兜底）后自动退出汇报模式，
  // 把用户送回原来的单报告视图。open() 时清掉残留，防上一次导出留下的标记
  // 让下一次手动导出意外退场。
  var autoForPdf=false;
  var slideFramesOverlay=null;  // 编辑态：grid 上的虚线框容器
  var slideFramesObserver=null;  // ResizeObserver：grid resize / 卡片拖拽时重画
  var slideFramesOnUp=null;      // 与上面成对的 window mouseup/touchend 捕获监听（install/uninstall 同生共死）

  function manifest(){return (global.AIReportRuntime&&global.AIReportRuntime.manifest)||{};}
  // C3 数据截至（ux-review-2026-09-25 读者信任批）：manifest.meta.dataAsOf 由编译
  // 侧烘焙（spec_to_report build_manifest，ISO YYYY-MM-DD），可能不存在——不存在/
  // 为空整段省略，绝不显示 undefined。非纯日期形态截 ISO 时间戳到日期段，其余原样
  // 透出。deck 页脚（slideEl）与 PPTX 页脚（pptx-exporter metaDataAsOfText）同口径。
  function dataAsOfText(){
    var m=manifest();
    var v=m&&m.meta&&m.meta.dataAsOf;
    if(v==null)return '';
    var s=String(v).trim();
    if(!s)return '';
    var d=/^(\d{4}-\d{2}-\d{2})(?:[T\s]|$)/.exec(s);
    return d?d[1]:s;
  }
  function chartFor(blockId){return (manifest().charts||[]).find(function(c){return c.blockId===blockId;});}
  function kpiFor(blockId){var m=/^block-kpi-(\d+)$/.exec(blockId||'');return m?(manifest().kpis||[])[Number(m[1])]:null;}
  function blockFor(id){return (manifest().blocks||[]).find(function(b){return b.id===id;});}
  // 稳健地从图表拿静态位图（hydrateCard 兜底 + PDF/打印克隆用）：优先导出 live
  // 实例；若实例尚未完成渲染/容器被 tab 隐藏(0 尺寸)，就用现有 option 在临时挂到
  // body 的离屏容器里重渲一次，确保任何时候都能拿到非空图片（避免 0 尺寸 canvas
  // 得到坏图、克隆里显示裂图）。
  function chartImgFor(ch){
    if(!ch||ch.viewType==='table')return null;
    // host 判空：容器不存在（页签未到访/已被换容器）时 getInstanceByDom(null)
    // 在 vendored ECharts 内部 dom.getAttribute 抛 TypeError——在 try 外面，会打垮
    // 整页 hydrate/PDF 导出（与 pptx-exporter.chartDataUrl 的 `host&&` 同一守卫）。
    var host=document.getElementById(ch.elementId||ch.id);
    var linst=ch?(global.echarts&&host&&global.echarts.getInstanceByDom(host)):null;
    if(linst){
      try{var d=linst.getDataURL({type:'png',pixelRatio:2,backgroundColor:'#fff'});if(d&&d.length>50)return d;}catch(e){}
    }
    try{
      var opt=linst?linst.getOption():null;if(!opt&&ch.option)opt=ch.option;
      if(!opt)return null;
      var host=document.createElement('div');
      host.style.cssText='position:fixed;left:-99999px;top:0;width:640px;height:420px;z-index:-1;';
      document.body.appendChild(host);
      var inst=global.echarts.init(host);inst.setOption(opt,true);
      var d2=inst.getDataURL({type:'png',pixelRatio:2,backgroundColor:'#fff'});
      inst.dispose();if(host.parentNode)host.parentNode.removeChild(host);
      if(d2&&d2.length>50)return d2;
    }catch(e){}
    return null;
  }
  function cardTitle(el,id){
    var t=el&&el.querySelector&&el.querySelector('.block-title,.tx-h2,.tx-h3');
    if(t&&(t.textContent||'').trim())return String(t.textContent).replace(/\s+/g,' ').trim();
    var b=blockFor(id),c=chartFor(id),k=kpiFor(id);
    if(b&&b.paragraphs&&b.paragraphs[0])return String(b.paragraphs[0].text||'').trim();
    if(c)return c.title||'图表';
    if(k)return k.label||'指标';
    return (manifest().title)||'报告';
  }
  function kindFor(id){
    var b=blockFor(id);if(!b)return 'text';
    if(chartFor(id)&&(chartFor(id).viewType==='table'||chartFor(id).viewType==='table.detail'))return 'table';
    if(chartFor(id))return 'chart';
    if(kpiFor(id))return 'kpi';
    if(b.component==='detail')return 'table';
    return 'text';
  }
  function orderedCards(){
    var nodes=Array.prototype.slice.call(document.querySelectorAll('.grid-stack-item[data-report-block-id]'));
    nodes.sort(function(a,b){
      var ay=Number(a.getAttribute('gs-y')||0),ax=Number(a.getAttribute('gs-x')||0);
      var by=Number(b.getAttribute('gs-y')||0),bx=Number(b.getAttribute('gs-x')||0);
      if(ay!==by)return ay-by;
      if(ax!==bx)return ax-bx;
      return 0;
    });
    return nodes;
  }

  // 网格行高 = GridStack cellHeight(48)；每张卡的可见高度 = h*48 - 16（上下各 8px margin）。
  // （卡片的绝对 top = 48*y，margin 由 .grid-stack-item-content inset 8px 表达。）
  // 注意：卡片的坐标系原点不是 document.querySelector('.grid-stack')（在 tabs 报告里
  // 外层 grid 还包着 tabbar + 内层 grid），而是**卡片所在的那个 grid**——所有行/框
  // 计算必须用 cardsGrid()，否则整页框会整体偏一个 tabbar 的高度。
  var CELL_PX=48;
  var CELL_MARGIN=8;
  var ROW_PX=CELL_PX;
  // 翻页上限（纯守扩，防死循环）：布局永远不需要这么多页。
  var PACK_MAX_PAGES=5000;
  // 测量 grid 宽度：汇报阅读态 grid 被 display:none（clientWidth=0），此时必须沿用
  // 上一次测到的真实宽度，否则几何（pageRows 等）会在 1280 与真实宽之间跳变，
  // 导致 deck / 定位框 / 汇报布局三者对不上页。
  var lastGridW=0;
  function cardsGrid(){
    var cards=document.querySelectorAll('.grid-stack-item[data-report-block-id]');
    var el=cards.length?cards[0]:null;
    var g=el&&el.closest?el.closest('.grid-stack'):null;
    return g||document.querySelector('.grid-stack');
  }
  function gridWidth(){
    var grid=cardsGrid();
    var w=grid?grid.clientWidth:0;
    if(w>0)lastGridW=w;
    return lastGridW||SLIDE_W_DEFAULT;
  }
  function pageGeometry(gridW){
    gridW=gridW||SLIDE_W_DEFAULT;
    var pageW=gridW;
    var ideal=Math.round(pageW*SLIDE_ASPECT);
    var pageRows=Math.max(4,Math.round(ideal/CELL_PX));
    var pageH=pageRows*CELL_PX;
    var titleRows=Math.max(1,Math.round((pageH/7)/CELL_PX));
    return {pageW:pageW,pageH:pageH,pageRows:pageRows,titleRows:titleRows,titleH:titleRows*CELL_PX,contentRows:Math.max(1,pageRows-titleRows)};
  }
  function cardPxH(gsH){return Math.max(24,gsH*CELL_PX-2*CELL_MARGIN);}
  // 文本卡（ai-hug）/手动精确高度卡（ai-fixh）/有界高度卡（ai-bounded，WP-7）在
  // live grid 上是精确像素高（<整行）：deck 克隆必须用同一像素高，否则 16:9 页里
  // 会出现一块被整行规则撑高的空白白卡。deck 固定建在单页 grid 当前宽度上，像素
  // 高可以直接复用。ai-bounded 不冒用 .ai-fixh（那是用户手动语义）——有界卡的
  // inline px = min(内容, maxPx)+chrome，由 autoFitTableCards 有界分支写入。
  function cardHeightPx(el,gsH){
    var hug=el&&el.classList&&(el.classList.contains('ai-hug')||el.classList.contains('ai-fixh')||el.classList.contains('ai-bounded'))?parseFloat(el.style.height):0;
    return (hug&&hug>0)?hug:cardPxH(gsH);
  }
  function cardPxW(gsW,pageW){return gsW/12*pageW;}
  function gridEl(id){return id?document.querySelector('[data-report-block-id="'+CSS.escape(id)+'"]'):null;}
  // 把 {id:{x,y,w,h}} 应用到每个卡自己的 GridStack 实例（tabs 报告里卡片可能分属
  // 不同 grid；单页报告就一个）。float(true) 保住每页标题那几行留白不被 compact 挤掉。
  function applyPositions(posById,floatOn){
    var groups=[];
    Object.keys(posById).forEach(function(id){
      var el=gridEl(id);if(!el)return;
      var gEl=el.closest?el.closest('.grid-stack'):null;
      var inst=gEl&&gEl.__aiReportGrid;
      if(!inst){var outer=document.querySelector('.grid-stack');inst=outer&&outer.__aiReportGrid;gEl=gEl||outer;}
      if(!inst)return;
      var grp=null;for(var i=0;i<groups.length;i++){if(groups[i].inst===inst){grp=groups[i];break;}}
      if(!grp){grp={inst:inst,el:gEl,items:[],maxRow:0};groups.push(grp);}
      grp.items.push({el:el,p:posById[id]});
    });
    groups.forEach(function(grp){
      try{grp.inst.float(!!floatOn);}catch(e){}
      grp.items.forEach(function(it){
        var p=it.p;
        try{grp.inst.update(it.el,{x:p.x||0,y:p.y||0,w:p.w||12,h:p.h||1});}
        catch(e){it.el.setAttribute('gs-x',String(p.x||0));it.el.setAttribute('gs-y',String(p.y||0));it.el.setAttribute('gs-w',String(p.w||12));it.el.setAttribute('gs-h',String(p.h||1));}
        grp.maxRow=Math.max(grp.maxRow,(p.y||0)+(p.h||1));
      });
      try{grp.inst._updateStyles(false,grp.maxRow);}catch(e){}
      try{grp.inst._updateContainerHeight();}catch(e){}
    });
  }
  // 按视觉行（gs-y）分组，再把行顺序装进每页的内容区；某行放不下就整体顺延下一页。
  // 汇报分页与单页 grid 用**同一个**装页算法（不再有两套口径）。
  function packRows(cards,geo){
    var rows={},order=[];
    cards.forEach(function(c){var y=Number(c.getAttribute('gs-y')||0);if(!rows[y]){rows[y]=[];order.push(y);}rows[y].push(c);});
    order.sort(function(a,b){return a-b;});
    var pages=[];var cur=[],curRows=0;
    order.forEach(function(y){
      var rc=rows[y];
      var rowH=Math.max(1,Math.max.apply(null,rc.map(function(c){return Number(c.getAttribute('gs-h')||1);})));
      if(cur.length&&curRows+rowH>geo.contentRows){pages.push(cur);cur=[];curRows=0;}
      cur.push({y:y,rowH:rowH,cards:rc});curRows+=rowH;
    });
    if(cur.length)pages.push(cur);
    return pages;
  }
  // 汇报布局的唯一真源：manifest.presentation.layout = { blockId: {page,slot,x,w,h} }。
  // 与单页布局（block.layout）**完全独立**：汇报模式首次进入按 packRows 规则种子化，
  // 之后在汇报编辑里的拖拽/插入只改这份 layout，不动单页正文。page/slot 是
  // **与宽度无关**的行坐标（随当前几何重算），所以改窗口宽度不会把布局弄乱。
  function presSpec(){
    var m=manifest();
    if(!m.presentation||typeof m.presentation!=='object')m.presentation={};
    if(!m.presentation.layout||typeof m.presentation.layout!=='object')m.presentation.layout={};
    return m.presentation;
  }
  function hasPresLayout(){var l=presSpec().layout;return !!(l&&Object.keys(l).length);}
  // O1 无框文本（2026-09-26 拍板，docs/presentation-ppt-alignment-review-2026-09-26.md）：
  // manifest.presentation.plainText 是汇报/导出级开关——deck 克隆的文本家族卡挂
  // .ai-pres-plaintxt（CSS 去框，deck/PDF 两作用域），PDF 克隆同一 DOM 自动跟随；
  // PPTX 由 pptx-exporter.cardFillInk 读同一开关短路。单页正文永不挂类，观感不变。
  // 豁免（plainExempt）：封面卡与深色卡（cardColor dark / 旧 textStyle banner）——
  // 去框=深底消失白字落白页。
  function plainTextOn(){return !!presSpec().plainText;}
  function plainExempt(id){
    if(isCoverCardId(id))return true;
    var b=blockFor(id);
    return !!(b&&(b.cardColor==='dark'||b.textStyle==='banner'));
  }
  function togglePlainText(){
    presSpec().plainText=!presSpec().plainText;
    if(global.AIReportRuntime&&global.AIReportRuntime.syncManifest)global.AIReportRuntime.syncManifest();
    if(active)swapDeck(buildDeckDOM());   // 阅读态立即重排；编辑态 deck 隐藏、下次构建生效
    syncPlainBtn();
  }
  function syncPlainBtn(){
    var b=document.querySelector('[data-pres-action="plaintext"]');
    if(b)b.classList.toggle('ai-pres-on',plainTextOn());
  }
  function layoutRows(l,geo){return (l.page||0)*geo.pageRows+geo.titleRows+(l.slot||0);}
  function seedPresentationLayout(){
    var geo=pageGeometry(gridWidth());
    var maxRows=Math.max(1,geo.contentRows|0);
    var pages=packRows(orderedCards(),geo);
    var layout={};
    pages.forEach(function(rows,pi){
      var slot=0;
      rows.forEach(function(r){
        r.cards.forEach(function(c){
          var id=c.getAttribute('data-report-block-id');if(!id)return;
          layout[id]={page:pi,slot:slot,x:Number(c.getAttribute('gs-x')||0),w:Number(c.getAttribute('gs-w')||12),h:Math.min(maxRows,Math.max(1,Number(c.getAttribute('gs-h')||1)))};
        });
        slot+=Math.min(maxRows,r.rowH);
      });
    });
    var spec=presSpec();spec.layout=layout;spec.seededAt=Date.now();
    return layout;
  }
  function syncPresGrid(geo,layout){
    var pos={};
    Object.keys(layout).forEach(function(id){var l=layout[id];pos[id]={x:l.x||0,y:layoutRows(l,geo),w:l.w||12,h:l.h||1};});
    applyPositions(pos,true);
  }
  // 把「期望的 page/slot」变成**合法**的 page/slot：不落进标题带、不与已放置卡重叠
  // （同行但列不重叠是允许的）、放不下就顺延下一页。保留用户行偏移（不重排），
  // 冲突时向下推。语义与 GridStack 的碰撞解析一致，所以 DOM 与 layout 不会分叉。
  function placeItems(items,geo){
    // 比一页内容区还高的卡必须夹到页高（否则 fit() 永远返回 -1，下面的 while
    // 会无限翻页把 byPage 撑爆）。真机踩过：表格卡自动高度后 = 18/39 行，
    // 而 1600px 窗宽下 contentRows 只有 ~13 → 点「汇报模式」直接卡死并抛
    // RangeError: Invalid array length（fit 里的 byPage）。
    // 夹页后仍不丢内容：卡片内部 .ai-report-table-wrap 自己滚（汇报页是定尺 16:9 帧）。
    var maxRows=Math.max(1,geo.contentRows|0);
    var saved=items.filter(function(it){return it.page!=null&&isFinite(it.page);});
    var lastPage=0,lastEnd=0;
    saved.forEach(function(it){var end=(it.page||0)*geo.pageRows+(it.slot||0)+(it.h||1);if(end>lastEnd)lastEnd=end;lastPage=Math.max(lastPage,it.page||0);});
    var appendSlot=Math.max(0,lastEnd-lastPage*geo.pageRows);
    var ordered=items.slice().sort(function(a,b){
      var ap=(a.page==null||!isFinite(a.page))?1e9:a.page, bp=(b.page==null||!isFinite(b.page))?1e9:b.page;
      if(ap!==bp)return ap-bp;
      var as=(a.slot==null||!isFinite(a.slot))?1e9:a.slot, bs=(b.slot==null||!isFinite(b.slot))?1e9:b.slot;
      if(as!==bs)return as-bs;
      return a.ord-b.ord;
    });
    var byPage={},out={};
    function fit(page,slot,h,x,w){
      var occ=byPage[page]||(byPage[page]=[]);
      var s=Math.max(0,slot|0);
      for(;;){
        var hit=null;
        for(var i=0;i<occ.length;i++){var o=occ[i];if(s<o.end&&o.start<s+h&&x<o.x+o.w&&o.x<x+w){hit=o;break;}}
        if(!hit)break;
        s=hit.end;
      }
      if(s+h>geo.contentRows)return -1;
      occ.push({start:s,end:s+h,x:x,w:w});
      return s;
    }
    ordered.forEach(function(it){
      var h=Math.max(1,it.h||1);
      if(h>maxRows)h=maxRows;             // 高于一页 → 夹页（不然 fit 永不成功）
      var x=Math.max(0,it.x||0), w=Math.max(1,it.w||12);
      var page=(it.page==null||!isFinite(it.page))?lastPage:(it.page|0);
      var slot=(it.slot==null||!isFinite(it.slot))?appendSlot:(it.slot|0);
      var placed=fit(page,slot,h,x,w);
      var guard=0;
      while(placed<0&&(guard+=1)<=PACK_MAX_PAGES){page+=1;slot=0;placed=fit(page,slot,h,x,w);}
      if(placed<0)placed=0;               // 纯守扩：绝不挂死
      out[it.id]={page:page,slot:placed,x:x,w:w,h:h};
    });
    // 紧凑化页码（若用户拖出空洞页号）
    var pages=[];
    Object.keys(out).forEach(function(id){var p=out[id].page;if(pages.indexOf(p)<0)pages.push(p);});
    pages.sort(function(a,b){return a-b;});
    var remap={};pages.forEach(function(p,i){remap[p]=i;});
    Object.keys(out).forEach(function(id){out[id].page=remap[out[id].page]||0;});
    return out;
  }
  function applyPresentationLayout(){
    var geo=pageGeometry(gridWidth());
    var saved=presSpec().layout||{};
    var items=[];
    orderedCards().forEach(function(c,i){
      var id=c.getAttribute('data-report-block-id');if(!id)return;
      var l=saved[id];
      items.push({id:id,h:Number(c.getAttribute('gs-h')||1),x:Number(c.getAttribute('gs-x')||0),w:Number(c.getAttribute('gs-w')||12),ord:i,
        page:l?l.page:null,slot:l?l.slot:null});
    });
    var layout=placeItems(items,geo);
    presSpec().layout=layout;
    syncPresGrid(geo,layout);
  }
  // 从 live grid 反推 layout —— grid 的 change/resize 后调用。保留用户摆放的行偏移，
  // 只做合法性约束（placeItems）：不落进标题带、不重叠、装不下顺延下一页。
  var applyingPresLayout=false;
  function capturePresentationLayout(){
    if(applyingPresLayout)return;
    var geo=pageGeometry(gridWidth());
    var items=[];
    orderedCards().forEach(function(c,i){
      var id=c.getAttribute('data-report-block-id');if(!id)return;
      var h=Number(c.getAttribute('gs-h')||1);
      var y=Number(c.getAttribute('gs-y')||0);
      var page=Math.max(0,Math.floor(y/geo.pageRows));
      var slot=Math.max(0,y-page*geo.pageRows-geo.titleRows);
      items.push({id:id,h:h,x:Number(c.getAttribute('gs-x')||0),w:Number(c.getAttribute('gs-w')||12),page:page,slot:slot,ord:i});
    });
    var layout=placeItems(items,geo);
    presSpec().layout=layout;
    applyingPresLayout=true;
    try{syncPresGrid(geo,layout);}finally{applyingPresLayout=false;}
  }
  // 单页布局快照 / 还原：进汇报模式前拍快照，退出时还原，保证汇报里的调整不写回单页。
  var pageLayoutSnapshot=null;
  function snapshotPageLayout(){
    var snap={};
    orderedCards().forEach(function(c){var id=c.getAttribute('data-report-block-id');if(id)snap[id]=layoutOf(c);});
    return snap;
  }
  function layoutOf(c){return {x:Number(c.getAttribute('gs-x')||0),y:Number(c.getAttribute('gs-y')||0),w:Number(c.getAttribute('gs-w')||12),h:Number(c.getAttribute('gs-h')||1)};}
  function restoreGridLayout(snap){
    if(!snap)return;
    applyingPresLayout=true;
    try{applyPositions(snap,false);}finally{applyingPresLayout=false;}
  }
  // 布局坐标 → 页/行（整数行）；deck 与定位框都读它。
  function pagesFromLayout(cards,geo){
    var layout=presSpec().layout;
    var maxRows=Math.max(1,geo.contentRows|0);
    var groups=[];
    if(layout&&Object.keys(layout).length){
      cards.forEach(function(c){
        var id=c.getAttribute('data-report-block-id');var l=id?layout[id]:null;
        if(!l)return;
        // 老 manifest 里可能存着「高于一页」的 h（夹页前的种子）→ 渲染前同样夹一次。
        groups.push({page:l.page||0,slot:l.slot||0,x:Number(c.getAttribute('gs-x')||l.x||0),rowH:Math.min(maxRows,Math.max(1,l.h||Number(c.getAttribute('gs-h')||1))),cards:[c]});
      });
    } else {
      packRows(cards,geo).forEach(function(rows,pi){
        var slot=0;
        rows.forEach(function(r){groups.push({page:pi,slot:slot,x:Number(r.cards[0].getAttribute('gs-x')||0),rowH:r.rowH,cards:r.cards});slot+=r.rowH;});
      });
    }
    var byKey={},order=[];
    groups.forEach(function(g){
      var k=g.page+':'+g.slot;
      if(!byKey[k]){byKey[k]={page:g.page,slot:g.slot,rowH:g.rowH,cards:[]};order.push(k);}
      byKey[k].cards=byKey[k].cards.concat(g.cards);
      if(g.rowH>byKey[k].rowH)byKey[k].rowH=g.rowH;
    });
    order.sort(function(a,b){var A=byKey[a],B=byKey[b];return A.page!==B.page?A.page-B.page:A.slot-B.slot;});
    var pages=[];
    order.forEach(function(k){
      var g=byKey[k];
      if(!g.cards.length)return;
      while(pages.length<=g.page)pages.push([]);
      g.cards.sort(function(a,b){return Number(a.getAttribute('gs-x')||0)-Number(b.getAttribute('gs-x')||0);});
      pages[g.page].push(g);
    });
    // 去掉空页（布局里可能有空洞页号）
    return pages.filter(function(p){return p&&p.length;});
  }
  // 把给定卡片（含其归属页签/节）解析成本页标题：按“最近的节标题卡”归属到节，
  // 取节的标题（去掉一二三、的前缀）；无节则用报告标题。修掉 PDF 里取到“编辑
  // 验证报告”式的错标题。
  function pageTitleFor(cardIds){
    var m=manifest();
    var blocks=m.blocks||[];
    function blockOf(id){return blocks.find(function(x){return x.id===id;});}
    function sectionTitle(sec){
      if(!sec)return null;
      var s=(m.sections||[]).find(function(x){return x.id===sec;});
      if(s&&s.title)return String(s.title).replace(/^[一二三四五六七八九十]+、/,'');
      var tb=blocks.find(function(x){return x.component==='section-tab'&&x.section===sec;});
      if(tb&&tb.paragraphs&&tb.paragraphs[0])return String(tb.paragraphs[0].text||'').trim();
      return null;
    }
    // 归属必须按 **grid 视觉顺序**（gs-y）而不是 manifest.blocks 的编译顺序：
    // 概览尾部的「关键洞察」在 blocks 里排最后，但网格里在页 1/2 顶部；按 blocks
    // 往回找会把它归到最后一个节（→错误显示「区域明细」）。
    var secMap={},last=null;
    orderedCards().forEach(function(c){
      var id=c.getAttribute('data-report-block-id')||'';
      var b=blockOf(id);
      if(b&&b.component==='section-tab')last=b.section;
      secMap[id]=last;
    });
    var ids=(cardIds||[]).filter(Boolean);
    if(ids.length){
      // 本页有节标题卡 → 用**第一个**出现的节标题卡（页首跨节时按新节名）。
      for(var i=0;i<ids.length;i++){
        var b0=blockOf(ids[i]);
        if(b0&&b0.component==='section-tab'){var t0=sectionTitle(b0.section);if(t0)return t0;}
      }
      // 否则用该页首张卡在视觉顺序上的归属节；无节（概览区）→ 报告标题。
      for(var j=0;j<ids.length;j++){
        var t1=sectionTitle(secMap[ids[j]]);
        if(t1)return t1;
      }
    }
    return m.title||'报告';
  }
  // D1 页标题可编辑：manifest.presentation.titles = { [该页首卡 id]: 覆盖标题 }。
  // 无覆盖值回落 pageTitleFor 自动推导；清空输入即删除覆盖（恢复自动）。
  function presTitleOverride(cardIds){
    var t=presSpec().titles;
    var id=(cardIds||[])[0];
    if(t&&id&&typeof t[id]==='string'&&t[id].trim())return t[id].trim();
    return null;
  }
  // V2（ux-review-round2 2026-09-25）：无节概览页的首卡就是封面卡（内含同标题 h1），
  // 自动标题（无节回落报告标题）再渲染一遍 = deck 页 1 / PDF / PPTX 上下两遍标题
  //（实机截图 09b）。页的卡集合包含封面卡时抑制自动标题（封面即标题）；覆盖值
  //（presTitleOverride）优先级不变。封面卡 id 是编译侧保留 id（spec_to_report.py
  // blocks.append({"id":"block-cover",...})），用户插入的文本块不会拿到它。
  // 返回 '' = 该页抑制标题；buildDeckDOM 把空串翻译成真空白标题透传导出数据。
  function isCoverCardId(id){return id==='block-cover';}
  function pageTitleForPage(cardIds){
    var ov=presTitleOverride(cardIds);
    if(ov)return ov;
    var ids=(cardIds||[]).filter(Boolean);
    for(var i=0;i<ids.length;i++){if(isCoverCardId(ids[i]))return '';}
    return pageTitleFor(cardIds);
  }
  // 抑制标题的页在页数据里带「真空白」标题：deck/PDF 的标题条照常渲染但不可见；
  // pptx-exporter 对页标题有 falsy 占位回落（slide.title||'报告页'）——空串会被顶成
  // '报告页' 假标题，真空格是 truthy，透传后同样不可见，三路（deck/PDF 克隆/PPTX）
  // 行为一致。
  var PRES_BLANK_TITLE=' ';
  // D1 提交：编辑态定位框标题条 blur/Enter 时落 manifest 并重建 deck。
  function commitPageTitle(cardIds,txt){
    var firstId=(cardIds||[])[0];if(!firstId)return false;
    var spec=presSpec();
    if(!spec.titles||typeof spec.titles!=='object')spec.titles={};
    var cur=typeof spec.titles[firstId]==='string'?spec.titles[firstId]:'';
    var next=(txt||'').replace(/\s+/g,' ').trim();
    if(next===cur)return false;
    if(next)spec.titles[firstId]=next;else delete spec.titles[firstId];
    if(global.AIReportRuntime&&global.AIReportRuntime.syncManifest)global.AIReportRuntime.syncManifest();
    if(active&&!playing)swapDeck(buildDeckDOM());
    return true;
  }
  // 返回页列表（与 grid 行坐标系一致）：[{pageIndex, top, height, titleH, title, cards:[el], cardIds:[...]}]
  // 分页真源 = manifest.presentation.layout（declared 行）；top = pageIndex*pageH（行对齐
  // 的页 pitch），所以定位框与卡片行逐页完全对齐。标题占 titleRows 行（= titleH）。
  function paginate(){
    var grid=cardsGrid();
    var cards=orderedCards();
    if(!grid||!cards.length)return [];
    var geo=pageGeometry(gridWidth());
    var pagesRows=pagesFromLayout(cards,geo);
    if(!pagesRows.length)return [];
    var pages=pagesRows.map(function(rows,pi){
      var els=[],ids=[];
      rows.forEach(function(r){r.cards.forEach(function(c){els.push(c);ids.push(c.getAttribute('data-report-block-id')||'');});});
      return {pageIndex:pi,top:pi*geo.pageH,height:geo.pageH,titleH:geo.titleH,title:pageTitleForPage(ids),cards:els,cardIds:ids};
    });
    return pages;
  }
  // C1 汇报字号缩放（宽档补偿）：≥1440 档 deck 文本整体放大 pageW/1280 倍，让宽档
  // 报告投影观感回到 1280 基准（13px 校准）。详见 docs/presentation-text-scale-design.md。
  function deckTextScale(geo){
    var pw=(geo&&geo.pageW)||SLIDE_W_DEFAULT;
    return pw>=1440?Math.round(pw/1280*1000)/1000:1;
  }
  // C1 文本卡 hug 重排：克隆体按缩放后字号离屏重测自然高（宽度=该卡实际 px 宽，
  // 探针挂 .ai-pres-deck 类以继承同一套 deck 字号规则），不按比例猜。
  function textCardNaturalH(innerClone,wPx,scale){
    try{
      var probe=document.createElement('div');
      probe.className='ai-pres-deck';
      probe.setAttribute('aria-hidden','true');
      probe.style.cssText='position:fixed;left:-99999px;top:0;width:'+Math.max(40,wPx)+'px;visibility:hidden;pointer-events:none;margin:0;padding:0;z-index:-1;';
      probe.style.setProperty('--pres-text-scale',String(scale));
      probe.appendChild(innerClone.cloneNode(true));
      document.body.appendChild(probe);
      var h=0;
      var blk=probe.querySelector('.block');
      if(blk){
        blk.style.height='auto';blk.style.maxHeight='none';
        h=Math.ceil(blk.getBoundingClientRect().height||blk.offsetHeight||0);
      }
      if(probe.parentNode)probe.parentNode.removeChild(probe);
      return h;
    }catch(e){return 0;}
  }

  // ---- PPT 预览 / 放映：只读翻页用，不影响 grid 编辑 --------------------
  // 数据卡默认可交互：deck 克隆体的 chart-host 建时留空，show() 翻到该页时
  // hydrateCard 重挂一个 live ECharts，tooltip / 图例开关在该页可用。option 按
  // 两级取：**原实例 getOption()**（主题/条件格式/单位缩放/运行时重聚合已烘在
  // 里头——屏上是什么就重放什么）优先；原实例不在（tabs 报告从未到访的页签里
  // 图表是惰性 init 的）→ 退回**编译期静态 chart.option** 重挂——仍是可交互
  // ECharts，绝不等价于静态图。两者都没有或克隆体还没尺寸（display:none，不该
  // 发生但要防）才落 chartImgFor 静态图兜底。图表卡不重放点击下钻/语义开关
  // ——那是编辑器面板的能力，汇报页只需要读数交互。
  function hydrateCard(item){
    var chart=chartFor(item.id);if(!chart||chart.viewType==='table'||chart.viewType==='table.detail')return;
    var node=item.node;
    if(!node||!node.querySelector||node.getAttribute('data-pres-live')==='1')return;
    var cloneHost=node.querySelector('.chart-host');
    if(!cloneHost)return;
    cloneHost.removeAttribute('_echarts_instance_');
    var width=cloneHost.clientWidth||0,height=cloneHost.clientHeight||0;
    var originalHost=document.getElementById(chart.elementId||chart.id);
    var original=originalHost&&global.echarts?global.echarts.getInstanceByDom(originalHost):null;
    var opt=null;
    if(original){try{opt=original.getOption();}catch(e){}}
    if(!opt&&chart.option)opt=chart.option;
    if(global.echarts&&opt&&width>10&&height>10){
      try{
        cloneHost.innerHTML='';
        var instance=global.echarts.init(cloneHost,null,{width:width,height:height});
        instance.setOption(opt,true);
        node.setAttribute('data-pres-live','1');
        return;
      }catch(e){}
    }
    var d=chartImgFor(chart);
    if(d){
      cloneHost.innerHTML='';
      var img=document.createElement('img');
      img.src=d;img.style.width='100%';img.style.height='100%';img.style.objectFit='contain';
      cloneHost.appendChild(img);
      node.setAttribute('data-pres-live','1');
    }
  }
  // 打印 / PDF 走静态克隆：live canvas 的位图 cloneNode 复制不过去（克隆画布是
  // 空白的），所以克隆页里每个图表卡换回 chartImgFor 的静态 <img>（原实例导出，
  // 离屏重渲兜底）。表格/文本/KPI 卡本来就是真实 DOM 克隆，不受影响。
  function staticSlideClone(sl){
    var inner=sl.root.cloneNode(true);
    var wraps=inner.querySelectorAll('.ai-pres-card[data-pres-block]');
    for(var i=0;i<wraps.length;i++){
      var ch=chartFor(wraps[i].getAttribute('data-pres-block')||'');
      if(!ch||ch.viewType==='table'||ch.viewType==='table.detail')continue;
      var host=wraps[i].querySelector('.chart-host');if(!host)continue;
      var d=chartImgFor(ch);
      if(d){
        host.innerHTML='';
        var img=document.createElement('img');
        img.src=d;img.style.width='100%';img.style.height='100%';img.style.objectFit='contain';
        host.appendChild(img);
      }
    }
    return inner;
  }
  function slideEl(title,geo,pageNo){
    var slide=document.createElement('section');slide.className='ai-pres-slide';slide.dataset.slide='true';
    // 页高用**行对齐**的整数行高（≈16:9），与 grid 行/定位框完全对齐；
    // 不用 CSS aspect-ratio，否则小数行会让页与卡逐页漂移。
    if(geo){slide.style.height=geo.pageH+'px';slide.style.aspectRatio='auto';}
    var ti=document.createElement('div');ti.className='ai-pres-title';ti.textContent=title||'报告页';
    if(geo)ti.style.height=geo.titleH+'px';
    var body=document.createElement('div');body.className='ai-pres-content';
    slide.appendChild(ti);slide.appendChild(body);
    // A4 页脚页码：deck/放映/PDF（克隆自带）与 PPTX（同一 N/M 数字）同源。
    if(pageNo&&pageNo.total>0){
      var pn=document.createElement('span');
      pn.className='ai-pres-pageno';
      pn.textContent=pageNo.n+' / '+pageNo.total;
      // C3：数据截至段与 PPTX 页脚同口径（metaDataAsOf 缺失整段省略）。
      var asOfTxt=dataAsOfText();
      if(asOfTxt)pn.textContent+=' · 数据截至 '+asOfTxt;
      slide.appendChild(pn);
    }
    return {slide:slide,body:body};
  }
  function escHtml(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
  function isFilterBlock(id){var b=blockFor(id);return !!(b&&(b.component==='filter'||id==='block-filter'));}
  // B1 筛选卡汇报态 = 只读口径徽章：deck/放映/PDF/PPTX 不克隆死控件，改渲染
  // 「生效筛选 N 条 + 摘要 chips」（chip 数据源 = 编辑态同一份 filterCardChips）。
  function deckFilterBadgeHtml(blockId){
    var rt=global.AIReportRuntime;
    var chips=(rt&&rt.filterCardChips?rt.filterCardChips(blockId):null)||[];
    // V13（ux-review-round2 2026-09-25）：0 chips 无口径可宣——返回空串，组装循环
    // 对空串整卡 skip，放映页不留一句废话占版面（负断言 grep 的就是这句文案）。
    if(!chips.length)return '';
    var chipsHtml=chips.map(function(c){
      return '<span class="filter-chip">'+escHtml(c.col+'：'+(c.vals||[]).join('、'))+'</span>';
    }).join('');
    var note='生效筛选 '+chips.length+' 条';
    return '<div class="grid-stack-item-content"><div class="block ai-pres-filtercard">'
      +'<div class="block-title">筛选口径</div>'
      +'<div class="ai-pres-filter-note" data-pres-filter-badge data-filter-count="'+chips.length+'">'+escHtml(note)+'</div>'
      +(chipsHtml?'<div class="filter-chips">'+chipsHtml+'</div>':'')
      +'</div></div>';
  }
  // C1 表格卡汇报态降级（ux-review-2026-09-25 #C1，「降级先行」拍板）：deck/PPTX/
  // PDF 的表格卡克隆体保留只读表体，顶部工具行（明细/聚合切换、搜索、排序、筛选、
  // Σ、字段、导出——分页器与工具同行，整体隐藏）display:none（**不删节点**：保持
  // 与读者页 DOM 同构，将来接活分页零迁移）；卡尾追加只读徽章「共 N 行（显示前
  // M 行）」，与筛选卡只读口径徽章（deckFilterBadgeHtml）一族。投影幕上不再摆点
  // 不动的死控件，也不再烙「第 1 / 6703 页」的假分页器。
  // N 取法（优先级）：
  //   1) 克隆 DOM 分页器的「共 N 行」文案——渲染端烙进 DOM 的总数真源（本地明细
  //      路径的 total 本就是筛选+搜索后的行集，口径即读者所见）；
  //   2) 表内搜索命中徽标「命中 N 行」（[data-tbl-hit]，仅搜索激活时存在）——
  //      兜住「服务端明细 + 搜索激活 + 多页完整分页器（只显 N / M 页）」的组合；
  //   3) manifest 对应 chart 的 editor.data.rows 行数（单文件内联路径兜底；
  //      server 模式无内联行 → 拿不到）。
  // 都拿不到时 N 未知——徽章只报显示行数，绝不编数。
  function deckTableTotalRowsOf(clone,blockId){
    var els=clone.querySelectorAll('.ai-pager-total');
    for(var i=0;i<els.length;i++){
      var m=/共\s*([\d,，]+)\s*行/.exec(els[i].textContent||'');
      if(m){var n=parseInt(m[1].replace(/[,，]/g,''),10);if(n>0)return n;}
    }
    var hit=clone.querySelector('[data-tbl-hit]');
    if(hit){
      var h=/命中\s*([\d,，]+)\s*行/.exec(hit.textContent||'');
      if(h){var hn=parseInt(h[1].replace(/[,，]/g,''),10);if(hn>0)return hn;}
    }
    var c=chartFor(blockId);
    var rows=c&&c.editor&&c.editor.data&&c.editor.data.rows;
    return (rows&&rows.length>0)?rows.length:null;
  }
  // M = 克隆体内 tbody 实际渲染行数（排除合计/小计行——既有 class 判定）。
  function deckTableShownRowsOf(clone){
    var trs=clone.querySelectorAll('tbody tr');
    var n=0;
    for(var i=0;i<trs.length;i++){
      var cn=trs[i].getAttribute('class')||'';
      if(/\bai-table-total\b/.test(cn)||/\bai-table-subtotal\b/.test(cn))continue;
      n++;
    }
    return n;
  }
  function degradeDeckTableCard(wrap,blockId){
    try{
      var dead=wrap.querySelectorAll('.ai-report-table-bar,.ai-table-pager');
      for(var i=0;i<dead.length;i++)dead[i].style.display='none';
      var shown=deckTableShownRowsOf(wrap);
      if(shown<=0)return;
      var total=deckTableTotalRowsOf(wrap,blockId);
      var txt=(total&&total>shown)?('共 '+total+' 行（显示前 '+shown+' 行）')
        :(total?('共 '+total+' 行'):('显示前 '+shown+' 行'));
      var note=document.createElement('div');
      note.className='ai-pres-table-note';
      note.setAttribute('data-pres-table-badge','1');
      note.textContent=txt;
      var block=wrap.querySelector('.block');
      if(block)block.appendChild(note);else wrap.appendChild(note);
    }catch(e){}
  }
  function buildDeckDOM(){
    // 只读分页 deck（供放映/导出共用）：自行把卡片按视觉行装进 16:9 页，标题占 1/7，
    // 某行放不下（跨页）就整体顺延到下一页——不依赖改 grid 布局（grid 始终是编辑真身）。
    var container=document.createElement('div');container.className='ai-pres-deck';
    var slides=[];
    var grid=cardsGrid();
    // 报告内容(单页 grid)是 max-width 布局：deck 固定建在 grid 当前宽度上，
    // 使得卡片框与克隆内容同宽；不再做 CSS zoom 反向补偿——浏览器缩放会
    // 自然地把整个 deck（框+内容+文字）一起变大变小，和顶部编辑条一致。
    var geo=pageGeometry(gridWidth());
    container.style.width=geo.pageW+'px';
    // C1：≥1440 档写 --pres-text-scale，deck 作用域的文本/表格/KPI 字号整体放大
    // （CSS 令牌重声明见 editor-runtime.css），布局几何不动。
    var tScale=deckTextScale(geo);
    if(tScale>1)container.style.setProperty('--pres-text-scale',String(tScale));
    var cards=orderedCards();
    if(!cards.length){
      var p0=slideEl(manifest().title||'报告',geo,{n:1,total:1});
      container.appendChild(p0.slide);
      slides.push({root:p0.slide,title:p0.slide.querySelector('.ai-pres-title').textContent,cards:[]});
      return {container:container,slides:slides};
    }
    var titleH=geo.titleH;
    // 分页真源：汇报布局行（page/slot）——与编辑态定位框、放映态逐页对齐。
    var pagesData=pagesFromLayout(cards,geo);
    pagesData.forEach(function(pgRows,pi){
      var firstId='';(pgRows[0]&&pgRows[0].cards[0])&&(firstId=pgRows[0].cards[0].getAttribute('data-report-block-id')||'');
      var ids=[];pgRows.forEach(function(r){r.cards.forEach(function(c){ids.push(c.getAttribute('data-report-block-id')||'');});});
      var title=pageTitleForPage(ids)||PRES_BLANK_TITLE;
      var p=slideEl(title,geo,{n:pi+1,total:pagesData.length});
      container.appendChild(p.slide);
      var cardRecs=[];
      var pageH=geo.pageH;
      pgRows.forEach(function(r){
        var rowTop=r.slot*ROW_PX;
        r.cards.forEach(function(c){
          var bid=c.getAttribute('data-report-block-id')||'';
          var x=Number(c.getAttribute('gs-x')||0),w=Number(c.getAttribute('gs-w')||12),h=Number(c.getAttribute('gs-h')||1);
          var dH=cardHeightPx(c,h);
          var inner=c.querySelector('.grid-stack-item-content');
          // C1：文本卡在缩放档位下按缩放后度量重排 hug（离屏探针实测，失败回退原高）。
          if(inner&&tScale>1&&kindFor(bid)==='text'&&c.classList.contains('ai-hug')){
            var nat=textCardNaturalH(inner,w/12*geo.pageW,tScale);
            if(nat>0)dH=nat+18;
          }
          // 全部用百分比（相对页面），随浏览器缩放/页宽同步缩放：框和内容一起变。
          var left=(x/12*100)+'%';
          var width=(w/12*100)+'%';
          var top=((titleH+rowTop)/pageH*100)+'%';
          var height=(dH/pageH*100)+'%';
          var wrap=document.createElement('div');
          wrap.className='ai-pres-card';
          wrap.style.position='absolute';
          wrap.style.left=left;
          wrap.style.top=top;
          wrap.style.width=width;
          wrap.style.height=height;
          // B1：筛选卡退化为只读口径徽章（投影页上一排死控件最伤可信度）。
          // V13：0 chips 时整卡 skip（不 append 也不进 cardRecs）——其他卡走
          // layout 行坐标绝对定位，位置不受影响（空出的只是这张卡的槽），页码
          // 总数与 aria 页码指示由 pagesFromLayout 派生，同样不变。
          if(isFilterBlock(bid)){
            var fh=deckFilterBadgeHtml(bid);
            if(!fh)return;
            wrap.innerHTML=fh;
          } else if(inner){
            wrap.appendChild(inner.cloneNode(true));
            // 数据卡默认 live：克隆体的 chart-host 先摘掉 _echarts_instance_ 与 id
            // （id 留着会让 getElementById 命中克隆体）并留空，翻到该页时 show() →
            // hydrateCard 重挂可交互 ECharts；拿不到 live 实例才由 hydrateCard
            // 回退静态图。data-pres-block 供 staticSlideClone（PDF/打印）按卡定位。
            wrap.setAttribute('data-pres-block',bid);
            // C1：表格卡降级只读表体（隐藏工具行/分页器 + 行数徽章）——deck、PDF
            // （staticSlideClone 克隆同一 DOM）与 PPTX 快照（createDeckDOM 同入口）
            // 三路同源生效，无需各自再降级。
            if(kindFor(bid)==='table')degradeDeckTableCard(wrap,bid);
            // O1 无框文本：开关开着且非豁免卡 → 克隆壳挂类（CSS 在 deck/PDF 两
            // 作用域去框+关角线；单页正文永不挂类）。
            if(kindFor(bid)==='text'&&plainTextOn()&&!plainExempt(bid))wrap.classList.add('ai-pres-plaintxt');
            var chartHost=wrap.querySelector('.chart-host');
            if(chartHost){chartHost.removeAttribute('_echarts_instance_');chartHost.removeAttribute('id');chartHost.innerHTML='';}
          }
          if(!wrap.getAttribute('data-pres-block'))wrap.setAttribute('data-pres-block',bid);
          p.body.appendChild(wrap);
          cardRecs.push({id:bid,kind:kindFor(bid),node:wrap});
        });
      });
      slides.push({root:p.slide,title:title,cards:cardRecs});
    });
    if(!slides.length){var p2=slideEl(manifest().title||'报告',geo,{n:1,total:1});container.appendChild(p2.slide);slides.push({root:p2.slide,title:p2.slide.querySelector('.ai-pres-title').textContent,cards:[]});}
    return {container:container,slides:slides};
  }

  // ---- 16:9 定位框：编辑态下叠加在原 grid 上 ---------------------------
  // 思路：grid 的 cellHeight=48（每行 48px），一页按 16:9 取整数行（round，行对齐）。
  // 计算当前 grid 的内容高度，切分成 N 个页高的 slide frame，绝对定位叠在 grid 上
  // 即可。卡片自然能跨 frame 移动（GridStack 一直在管）。
  // 兜底（仅在 paginate 拿不到 layout 时用）：按整数行推页数与行对齐的页 pitch。
  function computeFrames(){
    var grid=cardsGrid();
    if(!grid)return [];
    var geo=pageGeometry(gridWidth());
    var cards=orderedCards();
    // 至少要看到一页（grid 可能是空的）
    if(!cards.length){
      return [{top:0,left:0,width:geo.pageW,height:geo.pageH,index:1,titleH:geo.titleH}];
    }
    var maxBottom=0;
    cards.forEach(function(c){var y=Number(c.getAttribute('gs-y')||0);var h=Number(c.getAttribute('gs-h')||1);if(y+h>maxBottom)maxBottom=y+h;});
    var count=Math.max(1,Math.ceil(maxBottom/geo.pageRows));
    var frames=[];
    for(var i=0;i<count;i++){
      frames.push({top:i*geo.pageH,left:0,width:geo.pageW,height:geo.pageH,index:i+1,titleH:geo.titleH});
    }
    return frames;
  }
  function drawSlideFrames(){
    // D1：正在就地编辑页标题时不重画——重画会销毁 contenteditable 节点、打断输入
    //（frames observer 的 mouseup 捕获会在点进标题条时触发 30ms 重画）。
    // blur 提交路径（commitPageTitle）会补一次重画，这里跳过不丢最终一致性。
    var _act=document.activeElement;
    if(_act&&_act.classList&&_act.classList.contains('ai-pres-page-title')&&_act.isContentEditable)return;
    removeSlideFrames();
    var grid=cardsGrid();
    if(!grid)return;
    // 让 frames 容器与 grid 同坐标系：position:relative 的 grid 里加一个
    // position:absolute 的 overlay 容器。
    if(getComputedStyle(grid).position==='static'){
      grid.style.position='relative';
    }
    slideFramesOverlay=document.createElement('div');
    slideFramesOverlay.className='ai-pres-slide-frames';
    slideFramesOverlay.setAttribute('data-slide-frames','1');
    // 不让 overlay 抢事件
    slideFramesOverlay.style.pointerEvents='none';
    grid.appendChild(slideFramesOverlay);
    var geo=pageGeometry(gridWidth());
    var pages=paginate();
    var frames=(pages&&pages.length)?pages:computeFrames();
    // D1：页标题只在**编辑布局态**可就地编辑（阅读/放映态的 deck 标题条是只读的）。
    var titleEditable=document.body.classList.contains('ai-report-presenting-edit');
    frames.forEach(function(f){
      var top=f.top,height=f.height,titleH=f.titleH||geo.titleH,idx=(f.pageIndex!=null?f.pageIndex+1:f.index)||1;
      var box=document.createElement('div');
      box.className='ai-pres-slide-frame';
      box.style.top=top+'px';
      box.style.left='0px';
      box.style.width=geo.pageW+'px';
      box.style.height=height+'px';
      var chip=document.createElement('span');
      chip.className='ai-pres-slide-frame-label';
      chip.textContent='第 '+idx+' / '+frames.length+' 页';
      box.appendChild(chip);
      // 1/7 页签标题条（overlay，不占卡位）：高 = titleH，样式贴齐页签标题。
      var titleBar=document.createElement('div');
      titleBar.className='ai-pres-page-title';
      titleBar.style.height=titleH+'px';
      titleBar.setAttribute('data-page-title','1');
      titleBar.textContent=f.title||'';
      if(titleEditable&&f.cardIds&&f.cardIds.length){
        // D1 页标题可编辑：以该页首卡 id 为键写 manifest.presentation.titles；
        // 清空并失焦 = 删除覆盖，回落自动推导。
        titleBar.classList.add('ai-pres-title-edit');
        titleBar.setAttribute('contenteditable','true');
        titleBar.setAttribute('spellcheck','false');
        titleBar.title='点击编辑本页标题；清空恢复自动推导';
        titleBar.addEventListener('keydown',function(ev){
          if(ev.key==='Enter'&&!ev.shiftKey){ev.preventDefault();titleBar.blur();}
          else if(ev.key==='Escape'){titleBar.textContent=f.title||'';titleBar.blur();}
        });
        titleBar.addEventListener('blur',function(){
          if(commitPageTitle(f.cardIds,titleBar.textContent))setTimeout(drawSlideFrames,30);
        });
      }
      box.appendChild(titleBar);
      slideFramesOverlay.appendChild(box);
    });
  }
  function removeSlideFrames(){
    if(slideFramesOverlay&&slideFramesOverlay.parentNode){
      slideFramesOverlay.parentNode.removeChild(slideFramesOverlay);
    }
    slideFramesOverlay=null;
  }
  function installFramesObserver(){
    uninstallFramesObserver();
    var grid=cardsGrid();
    if(!grid)return;
    if(typeof ResizeObserver!=='undefined'){
      slideFramesObserver=new ResizeObserver(function(){
        // grid 尺寸变了 → 重画 frame
        drawSlideFrames();
      });
      slideFramesObserver.observe(grid);
    }
    // GridStack 拖拽 / 拉伸结束时变更 gs-x/y/w/h，frame 列表需要重算
    // 在 mouseup / touchend 后等一帧再画，等 layout flush
    // 监听必须成对移除：setEditing(true) 每次进入编辑布局都会 install，泄漏的
    // mouseup/touchend 捕获监听在退出汇报模式后仍对每次松手跑 drawSlideFrames
    // （重开一轮 = 再叠一对，会话内只增不减）。
    var onUp=function(){setTimeout(drawSlideFrames,30);};
    slideFramesOnUp=onUp;
    window.addEventListener('mouseup',onUp,true);
    window.addEventListener('touchend',onUp,true);
  }
  function uninstallFramesObserver(){
    if(slideFramesObserver){try{slideFramesObserver.disconnect();}catch(e){}slideFramesObserver=null;}
    if(slideFramesOnUp){
      window.removeEventListener('mouseup',slideFramesOnUp,true);
      window.removeEventListener('touchend',slideFramesOnUp,true);
      slideFramesOnUp=null;
    }
  }

  // ---- 老 API 存根（保留以免外部调用方踩到） -------------------------
  function reCloneDeck(editing){/* no-op: 编辑态不需要克隆 deck */}
  function rebuildDeck(){/* no-op: 编辑态在原 grid 上 */}
  function drawSlideMarks(){drawSlideFrames();}
  function removeSlideMarks(){removeSlideFrames();}
  function installSlideMarkReflow(){installFramesObserver();}
  function uninstallSlideMarkReflow(){uninstallFramesObserver();}
  function scheduleSlideMarkRedraw(){setTimeout(drawSlideFrames,30);}
  function restoreLiveCards(){/* no-op: 卡片一直留在原 grid，不需 restore */}
  function destroySlideGrids(){/* no-op: deck 不创建独立 GridStack 实例 */}

  // ---- 放映 / 翻页 --------------------------------------------------------
  function createControls(){
    // 顶上左侧：放映 + 翻页 + 退出汇报模式。汇报模式不另起右上 toolbar，
    // 右上的按钮（含 汇报模式 退出 / 分享 / 导出 PPTX / 编辑）一律用单页的
    // #ai-report-toolbar（保持两个模式按钮区一致）。
    var c=document.createElement('div');c.className='ai-pres-controls';
    c.innerHTML='<button type="button" class="ai-pres-btn ai-pres-play" data-pres-action="play" title="按 16:9 分页放映（Esc 退出）"><span class="ai-pres-play-icon">▶</span><span>放映</span></button>'
      +'<div class="ai-pres-divider"></div>'
      +'<button type="button" class="ai-pres-btn ai-pres-prev" data-pres-action="prev" title="上一页（←）">‹</button>'
      +'<span class="ai-pres-page" data-pres-page aria-live="polite" title="当前页 / 总页数（Home 首页 / End 末页）">1 / 1</span>'
      +'<button type="button" class="ai-pres-btn ai-pres-next" data-pres-action="next" title="下一页（→ / Space）">›</button>'
      +'<button type="button" class="ai-pres-btn ai-pres-close" data-pres-action="close" title="退出放映（Esc）">退出放映</button>'
      // 顶部说明：汇报分页与单页正文两份布局相互独立（用户决策）。
      +'<span class="ai-pres-note" title="汇报模式自有一套分页布局（每页顶部预留标题行，一页装不下就整体顺延到下一页）。这里的拖拽/增删只改汇报分页，不会改单页正文；单页里的调整也不会自动覆盖已排好的汇报分页。"><span class="ai-pres-note-i">i</span>汇报布局独立于单页</span>'
      +'<button type="button" class="ai-pres-btn ai-pres-reseed" data-pres-action="reseed" title="放弃当前汇报分页，按单页布局重新生成">从单页重置</button>'
      // O1 无框文本：只影响 deck/PDF/PPTX 呈现，单页观感不变；封面与深色卡恒保留。
      +'<button type="button" class="ai-pres-btn ai-pres-plaintxt-btn" data-pres-action="plaintext" title="放映/导出时文本卡去框为素文本（单页观感不变；封面与深色卡保留）">无框文本</button>';
    return c;
  }
  // createTopToolbar 已废弃——右上 toolbar 统一走单页的 #ai-report-toolbar。
  // 保留空函数供外部兼容（以防仍有引用）。
  function createTopToolbar(){return null;}

  // 汇报模式下：tabbar 被隐藏，但 toolbar 可能 docked 在 tabbar 里（tabbar 隐藏
  // -> toolbar 跟着看不见）。把 toolbar 临时从 tabbar 拆出来贴到 body 上、float
  // 到右上（和单页模式一致）；退出汇报模式时把它塞回 tabbar 原来的位置。
  function detachToolbarForPres(){
    var toolbar=document.getElementById('ai-report-toolbar');if(!toolbar)return null;
    var nav=document.querySelector('[data-report-tabbar]');
    var originalParent=toolbar.parentNode;
    var originalNext=toolbar.nextSibling;
    var wasDocked=toolbar.classList.contains('ai-tb-docked');
    if(nav&&originalParent!==document.body){
      document.body.appendChild(toolbar);
    }
    toolbar.classList.add('ai-pres-toolbar-floating');
    return {toolbar:toolbar,parent:originalParent,next:originalNext,wasDocked:wasDocked};
  }
  function restoreToolbarFromPres(state){
    if(!state||!state.toolbar)return;
    state.toolbar.classList.remove('ai-pres-toolbar-floating');
    if(state.parent&&(state.parent===document.body||document.body.contains(state.parent))){
      if(state.next&&state.next.parentNode===state.parent){
        state.parent.insertBefore(state.toolbar,state.next);
      } else {
        state.parent.appendChild(state.toolbar);
      }
    } else if(state.parent){
      // 父节点可能被誒除/重造成脱离了 tree：退而求其次插回 body，避免丢掉 toolbar。
      document.body.appendChild(state.toolbar);
    }
  }
  // deck 重建/关闭必须带走克隆体上的 live 实例：克隆体随容器丢弃后实例不可达，
  // 不 dispose 就是纯泄漏（窗口缩放/退出编辑都是常规重建路径，攒多了拖垮放映）。
  function disposeDeckCharts(){
    if(!deckDom||!global.echarts||!deckDom.slides)return;
    deckDom.slides.forEach(function(s){(s.cards||[]).forEach(function(item){
      if(!item.node||!item.node.querySelectorAll)return;
      var hosts=item.node.querySelectorAll('.chart-host');
      for(var i=0;i<hosts.length;i++){
        try{var inst=global.echarts.getInstanceByDom(hosts[i]);if(inst)inst.dispose();}catch(e){}
      }
    });});
  }
  // A2 滚轮/触屏翻页（仅放映态）：笔记本触控板与平板演示。预览态不绑——deck 是
  // 纵向滚动的页列，滚轮必须保持原生滚动。滚轮 = 累积超 40px 翻一页 + 250ms 防抖；
  // 触屏 = 横向滑动 >60px 且显著大于纵向位移。
  function bindDeckGestures(container){
    if(!container||container.__gesturesBound)return;container.__gesturesBound=true;
    var acc=0,lock=false;
    container.addEventListener('wheel',function(e){
      if(!playing)return;
      var dy=e.deltaY||0,dx=e.deltaX||0;
      var d=Math.abs(dy)>=Math.abs(dx)?dy:dx;
      if(!d)return;
      e.preventDefault();
      if(lock)return;
      acc+=d;
      if(Math.abs(acc)>=40){
        var fwd=acc>0;acc=0;lock=true;
        setTimeout(function(){lock=false;},250);
        if(fwd)next();else prev();
      }
    },{passive:false});
    var tx=0,ty=0,tid=null;
    container.addEventListener('touchstart',function(e){
      if(!playing)return;
      var t=e.changedTouches&&e.changedTouches[0];if(!t)return;
      tx=t.clientX;ty=t.clientY;tid=t.identifier;
    },{passive:true});
    container.addEventListener('touchend',function(e){
      if(!playing)return;
      var t=e.changedTouches&&e.changedTouches[0];if(!t||t.identifier!==tid)return;
      var dx=t.clientX-tx,dy=t.clientY-ty;
      if(Math.abs(dx)>60&&Math.abs(dx)>Math.abs(dy)*1.5){if(dx<0)next();else prev();}
    },{passive:true});
  }
  // R14 快赢（2026-09-25）：放映态空白点击翻页——点击落在画面宽度左 1/3 →
  // prev()（讲过了往回翻），其余 → next()；点在卡片/控件上不翻页（closest
  // 守卫维持原状）。宽度以 deck 容器实测矩形为准（全屏放映时即视口），取不到
  // 时退回视口宽。与滚轮/触屏（bindDeckGestures）和键盘翻页语义互不影响。
  // 挂点必须与 bindDeckGestures 同源（open() + swapDeck() 各一次）：swapDeck 换
  // 新容器后旧容器连同旧监听一起丢弃，只重绑手势不重绑点击 = deck 重建后
  // 点击翻页永久失效。__clickZonesBound 防同容器重复挂（换容器天然不重复）。
  function bindDeckClickZones(container){
    if(!container||container.__clickZonesBound)return;container.__clickZonesBound=true;
    container.addEventListener('click',function(e){
      if(!playing)return;
      if(e.target&&e.target.closest&&e.target.closest('.ai-pres-controls, .ai-pres-toolbar, .ai-pres-card'))return;
      if(document.body.classList.contains('ai-report-editing'))return;
      var rect=null;
      try{rect=container.getBoundingClientRect();}catch(err){rect=null;}
      var w=(rect&&rect.width)||global.innerWidth||0;
      var leftEdge=(rect?rect.left:0)+w/3;
      if(e.clientX!=null&&w>0&&e.clientX<leftEdge)prev();else next();
    });
  }
  // 重建 deck 的唯一入口：先释放旧克隆实例，再换容器，最后 show() 同步页码并
  // hydrate 当前页（rebuildForFit / setEditing(false) / reseedLayout 共用）。
  function swapDeck(c2){
    disposeDeckCharts();
    if(deckDom&&deckDom.container&&deckDom.container.parentNode)deckDom.container.parentNode.removeChild(deckDom.container);
    deckDom=c2;deckDom.controls=document.querySelector('.ai-pres-controls');
    document.body.appendChild(deckDom.container);
    bindDeckGestures(deckDom.container);
    bindDeckClickZones(deckDom.container);
    show(current);   // 同步页码 chip（PPT-H2）+ hydrate 当前页
  }
  function removeDeck(){
    disposeDeckCharts();
    if(deckDom&&deckDom.controls&&deckDom.controls.parentNode)deckDom.controls.parentNode.removeChild(deckDom.controls);
    if(deckDom&&deckDom.topToolbar&&deckDom.topToolbar.parentNode)deckDom.topToolbar.parentNode.removeChild(deckDom.topToolbar);
    if(deckDom&&deckDom.container&&deckDom.container.parentNode)deckDom.container.parentNode.removeChild(deckDom.container);
    deckDom=null;active=false;playing=false;current=0;
    presIdleStop();   // V12①：deck 销毁兜底清 idle 计时（异常路径残留也不挂 timer）
    clearBlackout();
    document.body.classList.remove('ai-report-presenting','ai-report-playing','ai-report-presenting-edit');
    // 清除卡片上的 play 过滤标记
    var marked=document.querySelectorAll('.grid-stack-item[data-play-current]');
    for(var i=0;i<marked.length;i++)marked[i].removeAttribute('data-play-current');
    // 主 #ai-report-toolbar 的 active 态（汇报模式按钮高亮）由 CSS
    // body.ai-report-presenting #ai-report-present 控制；body 类被移除
    // 后样式自动复位。无需手动动按钮。
  }
  function ensureDeck(){if(!deckDom)deckDom=buildDeckDOM();return deckDom;}
  function open(){
    if(active){close();return;}
    autoForPdf=false;   // 手动进入清掉可能残留的「PDF 自动进入」标记（R13）
    // 汇报布局独立存储：首次进入按「每页留标题 + 装不下顺延下一页」规则种子化
    // （manifest.presentation.layout）；之后只改这份布局，单页正文不受影响。
    // 退出时用快照还原单页布局。
    pageLayoutSnapshot=snapshotPageLayout();
    if(!hasPresLayout())seedPresentationLayout();
    applyingPresLayout=true;
    try{applyPresentationLayout();}finally{applyingPresLayout=false;}
    // 方案1：进入汇报模式即重建成干净的 deck 分页（反映当前汇报布局）。
    if(deckDom)removeDeck();   // 兜底：异常路径残留的旧 deck（含 live 实例）先带走
    deckDom=buildDeckDOM();var d=deckDom;
    document.body.appendChild(d.container);active=true;current=0;playing=false;document.body.classList.add('ai-report-presenting');
    bindDeckGestures(d.container);
    bindDeckClickZones(d.container);
    fitDeckScale();
    // 汇报模式现在保留 tabbar 可见（tabs 隐藏、.ai-pres-controls 插入左侧）
    // —— toolbar 仍然 docked 在 tabbar 里，不需要再拆到 body。
    var controls=createControls();
    // 插到 tabbar 顶部位置（左侧 “放映”入口），保留作为与单页同一胝顶部条。
    // tabbar 不可见 / 不存在时退化到 body 顶上。
    var nav=document.querySelector('[data-report-tabbar]');
    if(nav){
      nav.insertBefore(controls,nav.firstChild);
    } else {
      document.body.appendChild(controls);
    }
    deckDom.controls=controls;
    bindControls();
    syncPlainBtn();   // O1：控制条重建后按钮 active 态对齐 manifest 开关
    show(0);   // 统一入口：切换活动页 + 页码 chip + prev/next 禁用态（PPT-H2）
    if(global.AIReportRuntime&&global.AIReportRuntime.isEditing&&global.AIReportRuntime.isEditing()){
      setEditing(true);
    } else {
      // 非编辑：展示干净 deck 分页（grid 隐藏，见 CSS .ai-report-presenting-edit）
      markPlayCurrent(null);
    }
  }
  function close(){
    if(playing)exitPlay();
    uninstallFramesObserver();
    removeSlideFrames();
    removeDeck();
    // 退出汇报模式：把 live grid 还原成单页布局（汇报里的调整已存入
    // manifest.presentation.layout，不污染 block.layout）。
    restoreGridLayout(pageLayoutSnapshot);
    pageLayoutSnapshot=null;
    try{if(global.AIReportRuntime&&global.AIReportRuntime.syncLayouts)global.AIReportRuntime.syncLayouts();}catch(e){}
  }
  function exitFullscreen(){if(document.exitFullscreen)document.exitFullscreen();else if(document.webkitExitFullscreen)document.webkitExitFullscreen();}
  function requestFullscreen(el){if(el.requestFullscreen)return el.requestFullscreen();if(el.webkitRequestFullscreen)return el.webkitRequestFullscreen();return Promise.resolve();}
  function mountControlsForPlay(){
    var ctrl=document.querySelector('.ai-pres-controls');
    if(ctrl&&ctrl.parentNode!==document.body)document.body.appendChild(ctrl);
  }
  function restoreControlsForPres(){
    var ctrl=document.querySelector('.ai-pres-controls');
    var nav=document.querySelector('[data-report-tabbar]');
    if(ctrl&&nav&&ctrl.parentNode!==nav)nav.insertBefore(ctrl,nav.firstChild);
  }
  function resetPlayLayout(){
    var grid=cardsGrid();
    if(grid){grid.style.transform='';grid.style.transformOrigin='';}
  }

  function play(){
    if(!deckDom)ensureDeck();
    // 从「编辑布局」(presenting-edit) 直接放映：先走「阅读模式」同一路径回到
    // 干净 deck（重建分页）。否则 presenting-edit 的 CSS（更高特定性）会把
    // .ai-pres-deck 压成 display:none，播放态只剩黑屏/空页。
    if(document.body.classList.contains('ai-report-presenting-edit')&&global.AIReportRuntime&&global.AIReportRuntime.setEditing){
      try{global.AIReportRuntime.setEditing(false);}catch(e){setEditing(false);}
    }
    playing=true;
    // PPT-H4：从当前页起播（Shift+F5 语义）——保留预览态正在看的页，不再重置到第 1 页。
    document.body.classList.add('ai-report-playing');
    presIdleWake();   // V12①：进入播放即起 3s 静止计时
    mountControlsForPlay();
    resetPlayLayout();
    var target=document.documentElement||document.body;
    // 不 await：headless / 被拒的 fullscreen 请求可能永远不 resolve，导致
    // 后续 setTimeout 不被调度。直接 fire-and-forget。
    try{
      var p=requestFullscreen(target);
      if(p&&p.catch)p.catch(function(){});
    }catch(e){/* fullscreen may be denied */}
    // 等 fullscreen 过渡后展示 deck 干净分页（活动 slide 铺满视口）
    setTimeout(function(){
      if(!playing)return;
      try{global.scrollTo(0,0);}catch(e){}
      show(current);   // PPT-H4：从当前页起播——这里原是 show(0)，放映起点被硬拉回首页
    },120);
  }
  // 退出播放（仅退出全屏，不退汇报模式）—— 按 退出 / ESC / fullscreenchange
  // 都会走到这里。
  function exitPlay(){
    if(!playing)return;
    playing=false;
    presIdleStop();   // V12①：退出播放清 idle 计时并唤出控制条
    document.body.classList.remove('ai-report-playing');
    clearBlackout();
    restoreControlsForPres();
    resetPlayLayout();
    try{exitFullscreen();}catch(e){}
    // 清掉之前打的 data-play-current 标记（避免 body.ai-report-playing 类
    // 被移除后这些标记还残留在 DOM 里）
    show(current);
  }
  // A3 B/W 黑屏/白屏（演讲者切场/候场）：纯 body class + CSS ::after 覆盖层，
  // 只在放映态生效；二键互斥，任何退出播放的路径都要顺手清掉。
  function clearBlackout(){document.body.classList.remove('ai-pres-blackout','ai-pres-whiteout');}
  function toggleBlackout(mode){
    if(!playing)return;
    var other=mode==='black'?'ai-pres-whiteout':'ai-pres-blackout';
    var cls=mode==='black'?'ai-pres-blackout':'ai-pres-whiteout';
    var on=document.body.classList.toggle(cls);
    document.body.classList.remove(other);
    return on;
  }
  // fullscreen 退出（ESC / 上下文变更） → 清理播放态，但保留汇报模式。
  // 不在这里调 close()，避免一并退出汇报模式。
  function onFullscreenChange(){
    if(!document.fullscreenElement && playing){
      playing=false;
      presIdleStop();   // V12①：全屏退出同步清 idle 计时
      document.body.classList.remove('ai-report-playing');
      clearBlackout();
      restoreControlsForPres();
      resetPlayLayout();
      show(current);
    }
  }
  // V12①（ux-review-round2 2026-09-25）控制条自动隐藏：仅放映播放态生效——鼠标/
  // 触屏静止 3s 给 .ai-pres-controls 挂 .ai-pres-idle（CSS 透明 + pointer-events:none，
  // --t-slow 过渡，见 editor-runtime.css 汇报控制条段）；移动/触屏立即唤出。预览态
  // 不藏（控制条是预览态唯一翻页入口）。timer 单实例，任何退出播放的路径经
  // presIdleStop 清理（exitPlay / fullscreenchange / removeDeck）。
  var presIdleTimer=null;
  function presIdleStop(){
    if(presIdleTimer){clearTimeout(presIdleTimer);presIdleTimer=null;}
    var c=document.querySelector('.ai-pres-controls');
    if(c)c.classList.remove('ai-pres-idle');
  }
  function presIdleWake(){
    presIdleStop();
    if(!playing)return;
    presIdleTimer=setTimeout(function(){
      presIdleTimer=null;
      var c=document.querySelector('.ai-pres-controls');
      if(c)c.classList.add('ai-pres-idle');
    },3000);
  }
  // V12② 数字键跳页：放映播放态 1-9/0 → 第 1-10 页（0=第 10 页），越界忽略。
  // 输入框/可编辑元素聚焦时不劫持（表单数字输入优先）；预览态不劫持（编辑器
  // 的快捷键分发不受影响——editor-runtime 的翻页键通道照旧）。
  function presDigitJump(d){
    if(!playing)return;
    var n=(deckDom&&deckDom.slides)?deckDom.slides.length:0;
    if(!n)return;
    var i=(d===0?10:d)-1;
    if(i>n-1)return;
    show(i);
  }
  if(global.document&&global.document.addEventListener){
    global.document.addEventListener('fullscreenchange',onFullscreenChange);
    global.document.addEventListener('webkitfullscreenchange',onFullscreenChange);
    // V12①/V12②：唤出与跳页监听常驻（全局一份，不随进出放映增减），放映态门控
    // 在 presIdleWake/presDigitJump 内部——非放映态零行为。
    global.document.addEventListener('mousemove',function(){presIdleWake();},{passive:true});
    global.document.addEventListener('touchstart',function(){presIdleWake();},{passive:true});
    global.document.addEventListener('keydown',function(e){
      if(!playing)return;
      var t=e.target;
      if(t&&(t.tagName==='INPUT'||t.tagName==='TEXTAREA'||t.tagName==='SELECT'||t.isContentEditable))return;
      var m=/^[0-9]$/.exec(String(e.key||''));
      if(m)presDigitJump(Number(m[0]));
    });
  }
  // 汇报模式 deck 画布固定参考宽，随视口/浏览器缩放整体 CSS 缩放（框+内容+文字一起变）。
  if(global.addEventListener){global.addEventListener('resize',function(){window._aiPresFit&&clearTimeout(window._aiPresFit);window._aiPresFit=setTimeout(rebuildForFit,120);});}
  function rebuildForFit(){
    // A1：放映态改窗口/投影仪换分辨率 → 不重建 deck，只重算活动页 contain 缩放
    // （layoutPlayDeckSlide），否则放映中缩放永远停在进播那一刻的视口。
    if(playing||document.body.classList.contains('ai-report-playing')){layoutPlayDeckSlide();return;}
    // 汇报态窗口/浏览器缩放改变 grid 宽度时，重建 deck 使框与内容始终同宽。
    if(!active)return;
    if(!document.body.classList.contains('ai-report-presenting'))return;
    if(document.body.classList.contains('ai-report-presenting-edit'))return;
    var grid=cardsGrid();
    if(!grid||!deckDom)return;
    var need=Math.round(deckDom.container.style.width?parseFloat(deckDom.container.style.width):0);
    if(Math.abs(need-grid.clientWidth)>1){
      swapDeck(buildDeckDOM());
    }
  }

  // A5 死代码清理后只剩一条调用形态 markPlayCurrent(null)：清空所有 play 标记
  // （非播放态）。frame 分支与它的两个供给方（layoutPlayFrame/computePlayFrames）
  // 一并删除——live deck 方案下放映不再走 grid transform。
  function markPlayCurrent(){
    var allCards=document.querySelectorAll('.grid-stack-item[data-report-block-id]');
    var allFrames=document.querySelectorAll('.ai-pres-slide-frame');
    for(var i=0;i<allCards.length;i++)allCards[i].removeAttribute('data-play-current');
    for(var j=0;j<allFrames.length;j++)allFrames[j].removeAttribute('data-play-current');
  }

  // 放映全屏展示 deck 当前活动页（contain 缩放 + 居中，不裁剪），
  // 与汇报 deck 尺寸一致，不再用 grid transform（grid 在放映态已隐藏）。
  function layoutPlayDeckSlide(){
    if(!playing||!deckDom)return;
    var vw=global.innerWidth||document.documentElement.clientWidth||SLIDE_W_DEFAULT;
    var vh=global.innerHeight||document.documentElement.clientHeight||Math.round(SLIDE_W_DEFAULT*SLIDE_ASPECT);
    var active=deckDom.container.querySelector('.ai-pres-slide.ai-pres-active');
    if(!active)return;
    var w=active.offsetWidth||SLIDE_W_DEFAULT;
    var h=active.offsetHeight||Math.round(w*SLIDE_ASPECT);
    var scale=Math.min(vw/w,vh/h);
    if(!isFinite(scale)||scale<=0)scale=1;
    active.style.transformOrigin='center center';
    active.style.transform='scale('+scale+')';
  }
  function show(i){
    if(!deckDom)return;
    var n=deckDom.slides.length||1;
    current=Math.max(0,Math.min(n-1,i||0));
    // deck 是唯一分页真源：切活动 slide（演示预览 / 放映都走它）。
    deckDom.slides.forEach(function(s,k){s.root.classList.toggle('ai-pres-active',k===current);});
    // 数据卡默认可交互（PPT 数据卡不再是静态图）：翻到哪页，就把该页图表克隆体
    // 重挂 live 实例（此时该页 display:block，克隆体有真实尺寸可量）。已 hydrate
    // 的卡由 data-pres-live 标记跳过，翻回来不重建。
    if(deckDom.slides[current]&&global.echarts)deckDom.slides[current].cards.forEach(hydrateCard);
    var prevBtn=document.querySelector('[data-pres-action="prev"]');
    var nextBtn=document.querySelector('[data-pres-action="next"]');
    if(prevBtn){prevBtn.disabled=(current<=0);prevBtn.setAttribute('aria-disabled',current<=0?'true':'false');}
    if(nextBtn){nextBtn.disabled=(current>=n-1);nextBtn.setAttribute('aria-disabled',current>=n-1?'true':'false');}
    // PPT-H2：页码/进度 chip（预览态与放映态都可见）。
    var pg=document.querySelector('[data-pres-page]');
    if(pg)pg.textContent=(current+1)+' / '+Math.max(1,n);
    if(playing){
      layoutPlayDeckSlide();
      try{global.scrollTo(0,0);}catch(e){}
    } else {
      markPlayCurrent(null);
      // 放映退出后必须清掉 layoutPlayDeckSlide 写的内联 scale——旧版没有任何
      // 路径清它，回到预览态后播过的页保持 ~1.5 倍放大、溢出页框盖住相邻页
      // （deck 的 CSS 对 .ai-pres-slide 无 transform 兜底，只有 swapDeck 重建
      // 才能自愈）。
      deckDom.slides.forEach(function(s){s.root.style.transform='';s.root.style.transformOrigin='';});
    }
  }
  function next(){if(deckDom)show(current+1);}
  function prev(){if(deckDom)show(current-1);}
  function bindControls(){
    var ctrl=document.querySelector('.ai-pres-controls');if(ctrl&&ctrl.__bound)return;if(!ctrl)return;ctrl.__bound=true;
    function runAction(e){
      var b=e.target.closest('[data-pres-action]');if(!b)return;
      var a=b.getAttribute('data-pres-action');
      if(a==='play'){if(playing){show(0);}else{play();}}
      else if(a==='close'){exitPlay();}
      else if(a==='prev')prev();
      else if(a==='next')next();
      else if(a==='toggle')close();
      else if(a==='edit'){
        if(global.AIReportRuntime&&global.AIReportRuntime.setEditing){
          var editingNow=!!(global.AIReportRuntime.isEditing&&global.AIReportRuntime.isEditing());
          global.AIReportRuntime.setEditing(!editingNow);
        }
      }
      else if(a==='reseed'){if(global.AIReportPresentation&&global.AIReportPresentation.reseedLayout)global.AIReportPresentation.reseedLayout();}
      else if(a==='plaintext'){togglePlainText();}
      else if(a==='pptx-image'){if(global.AIReportPPTX)global.AIReportPPTX.exportImage();}
      else if(a==='pptx-native'){if(global.AIReportPPTX)global.AIReportPPTX.exportNative();}
      else if(a==='share-editable'){if(global.AIReportRuntime&&global.AIReportRuntime.downloadEditable)global.AIReportRuntime.downloadEditable();}
      else if(a==='share-readonly'){if(global.AIReportRuntime&&global.AIReportRuntime.downloadReadonly)global.AIReportRuntime.downloadReadonly();}
    }
    ctrl.addEventListener('click',runAction);
    var top=document.querySelector('.ai-pres-toolbar');
    if(top&&!top.__bound){top.__bound=true;top.addEventListener('click',runAction);}
    // ctrl 每次进出汇报模式都会重建，但它的 **parentNode（tabbar/body）是持久
    // 节点**——无守卫地挂会让「反复进出汇报模式」累积 N 个闭包监听。
    if(ctrl.parentNode&&!ctrl.parentNode.__presMenuBound){
      ctrl.parentNode.__presMenuBound=true;
      ctrl.parentNode.addEventListener('click',function(e){
        var menuBtn=e.target&&e.target.closest?e.target.closest('[data-pres-action="pptx"],[data-pres-action="share"]'):null;
        if(menuBtn){var drop=menuBtn.closest('.ai-pres-drop');var menu=drop&&drop.querySelector('.ai-pres-menu');var open=menu&&menu.classList.contains('ai-pres-open');document.querySelectorAll('.ai-pres-menu').forEach(function(m){m.classList.remove('ai-pres-open');});if(menu&&!open)menu.classList.add('ai-pres-open');e.stopPropagation();}
      });
    }
  }

  function setEditing(on){
    var editing=!!on;
    document.body.classList.toggle('ai-report-presenting-edit',editing);
    document.body.classList.toggle('ai-report-deck-editing',editing);
    if(editing){
      // 编辑态：把汇报布局应用到 live grid（每页标题那几行留白，float 保住），
      // 再叠定位框——编辑态看到的行位置与放映/导出完全一致。
      if(active){
        applyingPresLayout=true;
        try{applyPresentationLayout();}finally{applyingPresLayout=false;}
        setTimeout(drawSlideFrames,30);
        // AGENTS「定位框」契约：frames 随 grid resize / 卡片拖拽重画。此前
        // installFramesObserver 无任何调用方——编辑布局态里把卡拖到更低的页、
        // 或改高卡片后，页框数/「第 N / M 页」chip/D1 页标题全部停留在进入时
        // 的旧帧（ResizeObserver + mouseup 捕获正是为这条路径而写）。
        installFramesObserver();
        try{if(global.AIReportRuntime&&global.AIReportRuntime.resizeCharts)global.AIReportRuntime.resizeCharts();}catch(e){}
      }
    } else {
      // 阅读模式：从当前 grid 重建干净 deck，切回分页预览。
      uninstallFramesObserver();
      removeSlideFrames();
      if(active&&deckDom){
        swapDeck(buildDeckDOM());
      }
    }
    // 编辑态切换：主 #ai-report-toolbar 的 #ai-report-edit-toggle 按钮文本
    // 由主 editor-runtime.js 的 setEditing 同步（编辑模式↔阅读模式），不在这里
    // 重复改。老 .ai-pres-toolbar 里的 edit 按钮已废弃，不写。
  }

  // PPT-H3 PDF 导出（低成本高保真路线）：把当前 deck 每页克隆进 #ai-print-root，
  // @media print 只打印这份根（@page 16:9 自定义页尺寸 + 逐页 page-break），
  // window.print() 由浏览器落成 PDF。deck 里的图表是 live canvas（位图 cloneNode
  // 复制不过去），克隆必须走 staticSlideClone 把图表换回静态 <img>；
  // afterprint 清理（60s 兜底，防个别环境不触发）。
  function exportPDF(){
    if(!active)return false;
    if(!deckDom)deckDom=buildDeckDOM();
    var slideW=parseFloat(deckDom.container.style.width)||((cardsGrid()&&cardsGrid().clientWidth)||SLIDE_W_DEFAULT);
    var slideH=Math.round(slideW*SLIDE_ASPECT);
    var PRINT_W=1122; // A4 横向宽 @96dpi；页高按 16:9 裁（631px），幻灯片无留白
    var oldRoot=document.getElementById('ai-print-root');
    if(oldRoot&&oldRoot.parentNode)oldRoot.parentNode.removeChild(oldRoot);
    var root=document.createElement('div');root.id='ai-print-root';
    root.style.setProperty('--ai-print-scale',String(PRINT_W/slideW));
    // C1：PDF 克隆页不在 deck 容器内，字号缩放变量必须单独补齐（deck 与 PDF 同观感）。
    if(deckDom.container.style.getPropertyValue('--pres-text-scale')){
      root.style.setProperty('--pres-text-scale',deckDom.container.style.getPropertyValue('--pres-text-scale'));
    }
    deckDom.slides.forEach(function(sl){
      var page=document.createElement('div');page.className='ai-print-page';
      var inner=staticSlideClone(sl);
      inner.classList.remove('ai-pres-active');
      inner.style.transform='';inner.style.width=slideW+'px';inner.style.height=slideH+'px';inner.style.margin='0';
      page.appendChild(inner);root.appendChild(page);
    });
    document.body.appendChild(root);
    document.body.classList.add('ai-report-printing');
    var cleaned=false;
    function cleanup(){
      if(cleaned)return;cleaned=true;
      global.removeEventListener('afterprint',cleanup);
      if(root.parentNode)root.parentNode.removeChild(root);
      document.body.classList.remove('ai-report-printing');
      // R13：本次打印页来自「单报告模式导出 PDF」的自动进入路径 → 清理收尾时
      // 自动退出汇报模式，回到用户原本的单报告视图（手动进入的导出不退场）。
      var wasAuto=autoForPdf;autoForPdf=false;
      if(wasAuto&&active)close();
    }
    global.addEventListener('afterprint',cleanup);
    global.setTimeout(cleanup,60000);
    try{global.print();}catch(e){cleanup();return false;}
    return true;
  }
  global.AIReportPresentation={
    isActive:function(){return active;},
    exportPDF:exportPDF,
    // R13：单报告模式导出 PDF 的自动进入路径在 open() 之后、exportPDF() 之前调用，
    // afterprint 清理链路据此自动退出汇报模式。
    markAutoForPdf:function(){autoForPdf=true;},
    showPage:function(i){show(i);},
    isPlaying:function(){return playing;},
    open:open,
    close:close,
    toggle:function(){if(active)close();else open();},
    setEditing:setEditing,
    play:play,
    exitPlay:exitPlay,
    next:next,
    prev:prev,
    // A3：B/W 黑屏/白屏（editor-runtime 键盘分发调用；仅放映态生效）。
    toggleBlackout:toggleBlackout,
    createDeckDOM:buildDeckDOM,
    createControls:createControls,
    createTopToolbar:createTopToolbar,
    drawSlideFrames:drawSlideFrames,
    removeSlideFrames:removeSlideFrames,
    // 汇报布局 API（editor-runtime 的 syncLayouts 在汇报模式下改走这里，
    // 保证拖拽/插入写的是 manifest.presentation.layout 而不是 block.layout）。
    captureLayout:capturePresentationLayout,
    applyLayout:applyPresentationLayout,
    isApplyingLayout:function(){return applyingPresLayout;},
    reseedLayout:function(){
      seedPresentationLayout();
      applyingPresLayout=true;
      try{applyPresentationLayout();}finally{applyingPresLayout=false;}
      if(active){
        swapDeck(buildDeckDOM());
        if(document.body.classList.contains('ai-report-presenting-edit'))setTimeout(drawSlideFrames,30);
      }
    },
    hasLayout:hasPresLayout,
    // 单页真源保护：快照/下载前临时把 grid 还原为单页布局，跑完 fn 再应用回汇报布局。
    withPageLayout:function(fn){
      if(!active||!pageLayoutSnapshot)return fn();
      restoreGridLayout(pageLayoutSnapshot);
      applyingPresLayout=true;
      var out;
      try{out=fn();}finally{
        try{applyPresentationLayout();}finally{applyingPresLayout=false;}
      }
      return out;
    },
    // 测试钩子：分页/装页的纯函数（不接 DOM，便于 node 沙箱断言与静态编译 parity）。
    _internals:{pageGeometry:pageGeometry,packRows:packRows,layoutRows:layoutRows,cardPxH:cardPxH,
      pagesFromLayout:pagesFromLayout,pageTitleFor:pageTitleFor,pageTitleForPage:pageTitleForPage,
      isCoverCardId:isCoverCardId,presDigitJump:presDigitJump,presIdleStop:presIdleStop,
      placeItems:placeItems,deckTextScale:deckTextScale,CELL_PX:CELL_PX,
      deckTableTotalRowsOf:deckTableTotalRowsOf,deckTableShownRowsOf:deckTableShownRowsOf},
    // TITLE_H = deck 页标题带的 1280 基准高：pageGeometry 里 titleRows=round((720/7)/48)=2
    // 行 → 96px（旧值 56 是 deck 早期几何的残留，PPTX 标题框曾因此比 deck 基线偏高）。
    // 消费方只有 pptx-exporter（页标题框高度）；宽档位下 deck 标题带随 pageH 变化，
    // 但 PPTX 画布恒 1280×720、卡片坐标经 k 缩放，标题框用 1280 基准即可。
    constants:{SLIDE_W:SLIDE_W_DEFAULT,SLIDE_H:Math.round(SLIDE_W_DEFAULT*SLIDE_ASPECT),ASPECT:SLIDE_ASPECT,TITLE_H:96}
  };
})(typeof window!=='undefined'?window:this);
