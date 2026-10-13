(function(global){
  'use strict';

  // --- server-mode support -----------------------------------------------------
  // In server mode the manifest advertises mode=server; the runtime fetches
  // aggregates from the local server instead of running JS aggregation over
  // the in-memory data.rows. aggregate() always returns a Promise.
  // Mode is set by editor-runtime.js at startup (read from manifest).

  function isServerMode(){return global.__AI_REPORT_MODE__==='server';}
  // Local (singlefile) row filter — honours the same {column,op,value} shape
  // the report-wide filter bar builds and the server /aggregate accepts.
  function matchFilter(row, columns, filter){
    if(!filter||!filter.length)return true;
    // Comparison strategy mirrors Python spec_to_report._row_filter so the static
    // compile and the interactive singlefile re-filter select the SAME rows:
    //   - eq/ne/in/not-in compare NUMERICALLY when both sides are numeric, else
    //     as strings (so 9.5 == 9.50 and 007 == 7 in a numeric column).
    //   - gt/gte/lt/lte always numeric.
    //   - contains/is null/is not null string/null semantics.
    function eq(a,b){
      if(a==null||b==null)return String(a==null?'':a)===String(b==null?'':b);
      var an=Number(a),bn=Number(b);
      if(!isNaN(an)&&!isNaN(bn)&&String(a).trim()!==''&&String(b).trim()!=='')return an===bn;
      return String(a)===String(b);
    }
    for(var i=0;i<filter.length;i++){
      var f=filter[i];var ci=columns.indexOf(f.column);if(ci<0)continue;
      var v=row[ci];var op=f.op;var fv=f.value==null?'':f.value;
      // No value (or an empty selection) == no constraint — parity with the
      // compile-side _row_filter and the server _filter_clause. An `in` with
      // no value used to match nothing and silently empty the chart.
      if(op!=='is null'&&op!=='isNull'&&op!=='is not null'&&op!=='notNull'&&(f.value==null||f.value===''||(Array.isArray(f.value)&&!f.value.length)))continue;
      if(op==='is null'||op==='isNull'){if((v==null||v==='')!==true)return false;continue;}
      if(op==='is not null'||op==='notNull'){if((v==null||v==='')!==false)return false;continue;}
      if(op==='in'){var vals=Array.isArray(fv)?fv:[fv];if(!vals.some(function(x){return eq(v,x);}))return false;}
      else if(op==='not in'){var v2=Array.isArray(fv)?fv:[fv];if(v2.some(function(x){return eq(v,x);}))return false;}
      else if(op==='contains'){if(String(v??'').indexOf(String(fv))<0)return false;}
      else if(op==='eq'){if(!eq(v,fv))return false;}
      else if(op==='ne'){if(eq(v,fv))return false;}
      else if(op==='gt'){if(!(Number(v)>Number(fv)))return false;}
      else if(op==='gte'||op==='ge'){if(!(Number(v)>=Number(fv)))return false;}
      else if(op==='lt'){if(!(Number(v)<Number(fv)))return false;}
      else if(op==='lte'||op==='le'){if(!(Number(v)<=Number(fv)))return false;}
      // ge/le 是筛选型派生指标的声明词汇（derived_model.FILTER_OPS / 弹窗 fmOps 都
      // 认，PY _predicate_sql/_row_filter 映射 '>='/'<='）——JS 本地路径漏分支会被
      // 静默跳过（不筛选=命中所有行），singlefile 交互重聚合与编译静态首屏翻值。
      // 数值区间 / 日期范围：两端可空 = 单边开口；两端都空 = 无约束（上面已 continue）。
      // 日期比较在**规范化后的 ISO**上做（parseDateValue = Python date_parse.iso），
      // 所以 2026/9/5 / 2026年9月5日 / 20260905 / 202501 都能落进正确区间 —— 与
      // 编译侧 _row_filter 和服务端 ai_date() 是同一套归一化。
      else if(op==='between'){
        var pair=Array.isArray(fv)?fv:[];if(pair.length!==2)return false;
        var lo=pair[0],hi=pair[1];
        if(lo==null&&hi==null)continue;
        // 空单元格不匹配（`Number('')` 是 0，不显式挡掉就会算成「值=0」——
        // 与 Python `float('')` 抛错、SQL 的 TRIM<>'' 守卫三端一致）。
        if(v==null||String(v).trim()==='')return false;
        var n=Number(v);if(!Number.isFinite(n))return false;
        if(lo!=null&&lo!==''&&n<Number(lo))return false;
        if(hi!=null&&hi!==''&&n>Number(hi))return false;
      }
      else if(op==='daterange'){
        var dr=Array.isArray(fv)?fv:[];if(dr.length!==2)return false;
        var from=dr[0],to=dr[1];
        if(!from&&!to)continue;
        // 按**规范化后的 ISO** 比较 —— dateParts 是 Python date_parse.parts 的镜像，
        // 所以 2026/9/5、2026年9月5日、20260905、202501、epoch 都能落进正确区间
        // （SQL 侧同一套经 ai_date() UDF）。
        var dp=dateParts(v);if(!dp)return false;
        var iso=dp.y+'-'+pad2(dp.mo)+'-'+pad2(dp.d);
        // 端点也过同一解析器归一再截断（镜像编译侧 _iso_edge / 服务端 _iso_edge_date）：
        // 裸 String("2026-07").slice(0,10) 与日精度值 "2026-07-01" 字典序比较会把
        // 末月整月踢掉——三端必须同一端点语义。
        function galIsoEdge(x){var e=dateParts(x);return e?(e.y+'-'+pad2(e.mo)+'-'+pad2(e.d)):String(x==null?'':x);}
        var edgeFrom=from?galIsoEdge(from).slice(0,10):null;
        var edgeTo=to?galIsoEdge(to).slice(0,10):null;
        if(edgeFrom&&iso<edgeFrom)return false;
        if(edgeTo&&iso>edgeTo)return false;
      }
    }
    return true;
  }
  function filteredRows(data){
    var rows=(data.rows||[]);var filter=data.filter||[];if(!filter.length)return rows;
    var rawColumns=data.columns||[];
    var columns=(rawColumns.length && typeof rawColumns[0]==='object' && rawColumns[0]!==null)?rawColumns.map(function(c){return c&&c.name?c.name:c;}):rawColumns;
    return rows.filter(function(row){return matchFilter(row,columns,filter);});
  }
  // Canonical server API base. The server registers routes under /api/* and
  // accepts an optional /api/v1 prefix (stripped in app._route). We always
  // normalize to /api/v1 so client fetches are stable and never double the
  // prefix (e.g. /api/api) when serverBase came in as /api.
  function normalizeServerBase(){
    var base=String(global.__AI_REPORT_SERVER_BASE__||'/api/v1');
    base=base.replace(/\/$/,'');
    // Strip a trailing /api or /api/v1 so we can re-append a single /api/v1.
    base=base.replace(/\/api(?:\/v\d+)?$/,'');
    base=base.replace(/\/api\/api(?:\/v\d+)?$/,'');
    return (base||'')+'/api/v1';
  }
  // 服务端错误消息透传：聚合/取值端点的 4xx/5xx 带 {error:{message}}（如
  // "measure column 'X' not found"）。旧写法 `throw new Error('aggregate 500')`
  // 把可行动原因丢掉，用户只看到「数据加载失败」——计算列未重建、筛选列不存在
  // 这类问题从此不可诊断。成功路径与旧版逐位一致（返回解析后的 body）。
  function assertOk(resp,what){
    if(resp.ok)return resp.json();
    return resp.json().catch(function(){return null;}).then(function(body){
      var m=body&&body.error&&body.error.message;
      throw new Error(m||(what+' '+resp.status));
    });
  }
  // 编译侧 spec_to_report._col_non_additive 的镜像（topKOther「其他」行服务端
  // 合并门）：percent/unitless 语义类型或 率/占比/百分比/比例/rate/ratio/percent
  // 名义的列不可加。刻意不走 fieldSemantic（fieldRoles 用户改口不参与本门），
  // 与静态首屏逐位一致。
  function colNonAdditiveForMerge(data,col){
    var mt=data&&data.measureTypes?data.measureTypes[col]:null;
    var t=(mt&&typeof mt==='object')?mt.type:mt;
    if(t==='percent'||t==='unitless')return true;
    return /率|占比|百分比|比例|%.*%|rate|ratio|percent/i.test(String(col));
  }
  function fetchAggregate(data, dimension, measures, method, sink, opts){
    var serverBase=normalizeServerBase();
    var datasetId=data&&data.datasetId;
    if(!datasetId)return Promise.resolve([]);
    // Derived measures (同环比/累计/占比) are synthetic tokens with no server
    // column; the server can only aggregate real columns. Map each derived
    // measure to its BASE column (mirroring the local aggregate()'s aggMi), so
    // the server returns the base's aggregate and applyDerivedMeasures recomputes
    // the derived value client-side in applyMeasureScale.
    var derivedSpecs=data.derived||{};
    function aggColumn(m){var spec=derivedSpecs[m];return (spec&&spec.base)?spec.base:m;}
    // P2: aggregate-level derived measures additionally need their {agg,col}
    // dependency series — the server can only aggregate real columns, so each
    // dependency rides along as an extra requested series and is evaluated here.
    var plan=derivedPlanOf(data);
    // Dedupe the DISTINCT base columns the server must return and remember each
    // client measure's position in that deduped list. Without this, a chart with
    // a derived measure sends its base column twice (once for the real measure,
    // once as the derived token's base) — the server's 3-measure cap rejects the
    // request and the chart renders empty (the reported 派生-instant bug).
    var reqBases=[],reqIndex={},reqAggs=[],reqAggIndex={};
    function reqKey(col,agg){return col+'\u0000'+agg;}
    measures.forEach(function(m){
      // A P2 derived measure has no server column — its slot is a placeholder
      // overwritten by applyDerivedNodes (the helper aggs below carry the inputs).
      if(plan&&plan.names[m])return;
      var col=aggColumn(m);if(!Object.prototype.hasOwnProperty.call(reqIndex,col)){reqIndex[col]=reqBases.length;reqBases.push(col);}});
    (plan?plan.required:[]).forEach(function(dep){
      var key=derivedDepKey(dep);
      if(Object.prototype.hasOwnProperty.call(reqAggIndex,key))return;
      reqAggIndex[key]=reqAggs.length;reqAggs.push(dep);
    });
    // 指标名称占维度槽位（2026-10-08 真机：MN 拖到行区第 1 位 → dimension=dims[0]=
    // '__measures__'）：服务端没有这列，groupBy 恒 400「not found」→ 表格卡在分支路由
    // 之前就死（ND 泄压阀/mm 自取数根本没机会跑）。主管道对表格卡只要求不崩——用首个
    // 真实行维顶替维度槽位，请求/响应形状不变；表格分支的真实取数走 aggregateTable。
    var _grpDim=dimension;
    if(_grpDim==='__measures__'){
      var _rd=(Array.isArray(data.dimensions)?data.dimensions:[]).filter(function(d){return d&&d!=='__measures__';});
      _grpDim=_rd[0]||null;
    }
    var payload={
      groupBy: _grpDim?[_grpDim]:[],
      measures: reqBases.map(function(col){return {column:col,aggregation:_pickMethod(method,col,data)};})
        .concat(reqAggs.map(function(dep){
          // 筛选型依赖带 per-measure filters：服务端按 CASE WHEN 条件聚合（同列
          // 不同筛选集靠位置别名区分，响应仍按请求位置对齐）。
          var it={column:dep.col,aggregation:dep.agg};
          if(dep.filters)it.filters=dep.filters;
          return it;
        })),
      filter: data.filter||[],
      orderBy: data.orderBy||[],
      // topK 作为服务端 LIMIT 是图表的显示截断（truncated 事实照发）。但**表格卡**
      // 的派生指标（占比/环比）必须按「聚合 → 派生 → TOP-N」的既定次序先在全量
      // 分组上求值——分母若是被 topK 截断后的行集和，占比会放大几十倍（真机：净流入
      // 占比 1.6% 显示成 430%）。opts.fullSet 时拉全量分组，截断交给调用方
      // （表格管道自己的 applyTableTopN/分页）。
      limit: (opts&&opts.fullSet)?(Number(data.aggregateLimit)||20000):(data.topK || data.limit || 500),
      offset: data.offset || 0
    };
    // BI-H3：合并其余为「其他」只在作者/读者开了开关且有 topK 时下发。
    if(data.topKOther&&data.topK)payload.topKOther=true;
    // server「其他（N 项）」合并的可加性门需要语义层（measureTypes/列名义），
    // 服务端只有列名+聚合——按编译侧 _col_non_additive 同一门逐位下传，占比型
    // 基础列在尾行保持空缺（与静态首屏 mergedPartial 一致）。helper 依赖序列恒
    // 可加（它们的派生乘积在下方 otherMerged 分支单独置空）。
    if(data.topKOther&&data.topK)payload.addMask=reqBases.map(function(col){return !colNonAdditiveForMerge(data,col);})
      .concat(reqAggs.map(function(){return true;}));
    var roles=data.roles||{};
    // 指标名称虚拟维度（__measures__）是客户端展开的保留键：服务端没有这列，
    // 绝不能进 GROUP BY（真机 2026-09-28：指标名称卡进列区后主管道聚合 400
    // 「groupBy column '__measures__' not found」——表格分支自己按真实维度取数后
    // 本地展开，主管道只要求不崩）。matrix 判定同理：响应里没有第二分组列。
    if(roles.breakdown && roles.breakdown!==_grpDim && roles.breakdown!=='__measures__')payload.groupBy.push(roles.breakdown);
    if(data.dimensionBucket&&_grpDim===dimension)payload.bucket=data.dimensionBucket;
    return fetch(serverBase+'/datasets/'+encodeURIComponent(datasetId)+'/aggregate',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(payload)
    }).then(function(resp){
      return assertOk(resp,'aggregate');
    }).then(function(payload){
      // payload: {columns:[dim, m1, m2, ...], rows:[[dimVal, m1v, m2v, ...]],
      //           truncated: <bool>, groupTotal: <LIMIT 前的分组总数，仅截断时有值>}
      // 服务端 LIMIT 过的结果客户端只看到 N 行，不读这两个字段就完全不知道被截断了
      // （作者 topK=12 / 自动 top-12 时读者会以为 12 项就是全部）。groupTotal 缺失
      // （老服务端）时仍然给出说明，只是不带总数。
      if(payload&&payload.truncated){
        var _shown=(payload.rows||[]).length;
        var _tot=Number(payload.groupTotal||0);
        // BI-H3：服务端合并了「其他」行时，shown 要扣掉它（说明读「前 K 项 + 其他」）。
        if(payload.otherMerged){_shown=new Set((payload.rows||[]).map(function(r){return String(r[0]);})).size-1;}
        // 恰好等于 LIMIT 组时 truncated 也是 true（服务端只看 >=），但总数证明
        // 没有任何被丢掉的分组 → 不报截断，避免假警报。
        if(_tot>_shown)noteTruncation(sink,_shown,_tot);
        else if(!_tot)noteTruncation(sink,_shown,0);
        if(payload.otherMerged&&sink&&sink.trunc)sink.trunc.merged=true;
      }
      // The server returns one column per DISTINCT base (reqBases order).
      // Re-align each row to the CLIENT's measures array: every measure (incl. a
      // derived token) reads its base's value at its deduped position, so
      // applyDerivedMeasures can index e.values[baseIndex] correctly.
      var matrix=payload.columns&&payload.columns.length>2&&roles.breakdown&&roles.breakdown!=='__measures__';
      var valSlice=matrix?2:1;
      var entries=(payload.rows||[]).map(function(row){
        var baseVals=row.slice(valSlice);
        var values=measures.map(function(m,idx){var vi=reqIndex[aggColumn(m)];return vi==null?0:Number(baseVals[vi]);});
        // breakdown 空组（SQL NULL 在 GROUP BY 下自成一组，JSON 里就是 null）必须
      // 归一成 ''——String(null) 是字面量 'null'，图例/系列名会真的显示「null」，
      // 且与本地路径（String(row[bi] ?? '')）三端漂移（2026-10-11 审视）。
      return {label:String(row[0]!=null?row[0]:''), breakdown:matrix?String(row[1]!=null?row[1]:''):null, values:values};
      });
      if(plan){
        // helper series come after the visible base columns in the response
        reqAggs.forEach(function(dep){
          var vi=reqBases.length+reqAggIndex[derivedDepKey(dep)];
          plan.series[derivedDepKey(dep)]=(payload.rows||[]).map(function(row){
            var v=row.slice(valSlice)[vi];return v==null?null:Number(v);
          });
        });
        // 指标名称占维度槽位（_grpDim 顶替）时跳过派生求值：窗口闸门拿虚拟列
        // '__measures__' 比对 timeField 恒抛「依赖时间列…」，整张表格卡在分支路由
        // 之前死进错误态——而 mm/nf/ND 表格分支自按真实维度 aggregateTable 重取
        // 重算，主管道的派生槽本来就不会被消费（主管道对表格卡只要求不崩；
        // 本地路径 ci<0 早退本就跳过，这里对齐，2026-10-11 审视）。
        if(_grpDim===dimension)applyDerivedNodes(entries,data,plan,dimension);
        // 「其他（N 项）」行的派生指标与编译侧同口径：share / window-rate 的值
        // 已 ×100（不可加），静态首屏该格为空缺（mergedPartial）——运行时重聚合
        // 也要置空，否则首屏空缺、编辑态却出现一个「尾组合计占比」的幽灵值。
        if(payload.otherMerged){
          var _othIdx=-1;
          entries.forEach(function(e,i){if(String(e.label).indexOf('其他（')===0)_othIdx=i;});
          if(_othIdx>=0)plan.used.forEach(function(n){
            if(!derivedPercentUnits(data,n.name))return;
            var vi=measures.indexOf(n.name);
            if(vi>=0)entries[_othIdx].values[vi]=null;
          });
        }
      }
      // Server already filters/orders/limits, but it doesn't sort by the first
      // measure desc by default unless we asked it to. Most chart kinds rely
      // on the descending primary measure (R3); do it here so chart-adapter's
      // own sortEntries can refine.
      if(entries.length>1 && measures.length && !matrix){
        var mi=0;
        // BI-H3：合并的「其他（N 项）」行恒排最后——值排序不得把它卷进中间。
        var _oth=[],_rest=[];
        entries.forEach(function(e){(e.label.indexOf('其他（')===0?_oth:_rest).push(e);});
        _rest.sort(function(a,b){return (b.values[mi]||0)-(a.values[mi]||0);});
        entries=_rest.concat(_oth);
      }
      entries.matrix=!!matrix;
      // server 已合并「其他」（BI-H3）：applyTopK 见此标记直接放行，不二次截断。
      if(payload&&payload.otherMerged)entries.otherMerged=true;
      return entries;
    });
  }

  // N-dimension aggregate for the table: group by ALL dims (server groupBy now
  // accepts up to 6), each row carries a `path` (one value per dim) + `values`.
  function aggregateTable(data, dims, measures, method){
    dims=(dims||[]).filter(Boolean);
    if(!dims.length)return Promise.resolve([]);
    var datasetId=data&&data.datasetId;
    var derivedSpecs=data.derived||{};
    function aggColumn(m){var spec=derivedSpecs[m];return (spec&&spec.base)?spec.base:m;}
    var plan=derivedPlanOf(data);
    var reqBases=[],reqIndex={},reqAggs=[],reqAggIndex={};
    function reqKey(col,agg){return col+'\u0000'+agg;}
    measures.forEach(function(m){
      // A P2 derived measure has no server column — its slot is a placeholder
      // overwritten by applyDerivedNodes (the helper aggs below carry the inputs).
      if(plan&&plan.names[m])return;
      var col=aggColumn(m);if(!Object.prototype.hasOwnProperty.call(reqIndex,col)){reqIndex[col]=reqBases.length;reqBases.push(col);}});
    (plan?plan.required:[]).forEach(function(dep){
      var key=derivedDepKey(dep);
      if(Object.prototype.hasOwnProperty.call(reqAggIndex,key))return;
      reqAggIndex[key]=reqAggs.length;reqAggs.push(dep);
    });
    // 表格卡的聚合是**全量分组**管道的输入：派生指标（占比/环比）在全量上求值、
    // 截断由表格自己的 topK 切片/分页承担。topK/limit 是显示语义，绝不能当传输层
    // LIMIT——否则分母只剩截断后的行集和，占比放大几十倍（真机：1.6% 显示成 430%）。
    var payload={groupBy:dims.slice(),measures:reqBases.map(function(col){return {column:col,aggregation:_pickMethod(method,col,data)};}).concat(reqAggs.map(function(dep){
      var it={column:dep.col,aggregation:dep.agg};if(dep.filters)it.filters=dep.filters;return it;})),filter:data.filter||[],orderBy:data.orderBy||[],limit:Number(data.aggregateLimit)||20000,offset:data.offset||0};
    var serverBase=normalizeServerBase();
    // 闸门必须与 aggregate() 同款：singlefile 数据带 datasetId（编译恒写）而
    // normalizeServerBase() 恒返回 '/api/v1'——旧闸 `!datasetId||!serverBase`
    // 对 singlefile 恒假，ND/嵌套/3D 表格卡会 POST /api/v1/...（file:// 下
    // fetch 直接 reject → 整卡「数据加载失败」横幅）。
    var remote=!!(data&&(data.remote||(data.datasetId&&data.rowCount&&!Array.isArray(data.rows))));
    if(!datasetId||!serverBase||!remote)return Promise.resolve(localAggregateTable(data,dims,reqBases,payload,reqIndex,measures,method,plan,aggColumn));
    return fetch(serverBase+'/datasets/'+encodeURIComponent(datasetId)+'/aggregate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)})
      .then(function(resp){return assertOk(resp,'aggregate');})
      .then(function(p){var nd=dims.length;var entries=(p.rows||[]).map(function(row){var path=row.slice(0,nd);var baseVals=row.slice(nd);var values=measures.map(function(m,idx){var vi=reqIndex[aggColumn(m)];return vi==null?0:Number(baseVals[vi]);});return {path:path,label:String(path[path.length-1]),values:values};});
        if(plan){
          reqAggs.forEach(function(dep){
            var vi=reqBases.length+reqAggIndex[derivedDepKey(dep)];
            plan.series[derivedDepKey(dep)]=(p.rows||[]).map(function(row){var v=row.slice(nd)[vi];return v==null?null:Number(v);});
          });
          // 多维行表（ND/嵌套）的窗口派生：游标按**前 n-1 维**分区独立推进，
          // 否则环比会跨 d1 组取「上期」（与 applyDerivedNodes 的 breakdown
          // 分区同一批语义）。
          applyDerivedNodes(entries,data,plan,dims[dims.length-1],
            nd>1?entries.map(function(e){return e.path.slice(0,-1).join('\u0000');}):null);
        }
        return entries.map(function(e){return {path:e.path,values:e.values};});});
  }
  function localAggregateTable(data,dims,reqBases,payload,reqIndex,measures,method,plan,aggColumn){
    var rows=(data.rows||[]);var cols=(data.columns||[]).map(function(c){return typeof c==='object'?c.name:c;});
    var dimIdx=dims.map(function(d){return cols.indexOf(d);});
    var filt=data.filter||[];
    var groups={},ord=[];
    rows.forEach(function(r){if(!matchFilter(r,cols,filt))return;
      var path=dimIdx.map(function(ix){return ix>=0?r[ix]:null;});
      var key=path.map(function(v){return String(v==null?'':v);}).join('\u0000');
      if(!groups[key]){groups[key]={path:path};ord.push(key);}
    });
    // 兜底聚合吃**逐指标方法**（_pickMethod 与服务端载荷同一解析）：旧版把所有
    // reqBases 一律 Number()||0 累加——average/countd/min/max 全变 sum。
    // extra（筛选型派生指标的逐序列筛选）：组宇宙不变（ord 由报告级筛选决定），
    // 组内只累加通过 extra 谓词的行——空池 count/countd=0、sum/average/min/max=null，
    // 与服务端 CASE WHEN、编译侧 series_filters 三端逐位一致。
    var methods=reqBases.map(function(col){return _pickMethod(method,col,data);});
    function groupSeries(col,agg,extra){
      var vi=cols.indexOf(col);
      var acc=Object.create(null);
      rows.forEach(function(r){if(!matchFilter(r,cols,filt))return;
        if(extra&&!matchFilter(r,cols,extra))return;
        var path=dimIdx.map(function(ix){return ix>=0?r[ix]:null;});
        var key=path.map(function(v){return String(v==null?'':v);}).join('\u0000');
        var a=acc[key]||(acc[key]={s:0,n:0,nn:0,mn:Infinity,mx:-Infinity,set:null});
        if(col==='*'||agg==='count'){a.n+=1;return;}
        var raw=vi>=0?r[vi]:null;
        if(agg==='countd'){if(raw!=null){(a.set=a.set||Object.create(null))[String(raw)]=1;}return;}
        // average/min/max 与编译/服务端同口径：NULL/空串/非数值格不参与
        // （Number(null)=0 会把空格当 0——min 出假 0、平均被稀释）。
        var num=(raw==null||raw==='')?NaN:Number(raw);
        if(!isFinite(num))return;
        a.s+=num;a.nn+=1;
        if(num<a.mn)a.mn=num;
        if(num>a.mx)a.mx=num;
      });
      return ord.map(function(k){var a=acc[k];
        if(!a)return extra?((col==='*'||agg==='count'||agg==='countd')?0:null):null;
        if(col==='*'||agg==='count')return a.n;
        if(agg==='countd')return a.set?Object.keys(a.set).length:0;
        if(agg==='average')return a.nn?a.s/a.nn:null;
        if(agg==='min')return a.nn?a.mn:null;
        if(agg==='max')return a.nn?a.mx:null;
        return a.s;
      });
    }
    var colSeries=reqBases.map(function(col,j){return groupSeries(col,methods[j]);});
    // values 按 **measures** 索引对齐（与服务端 .then 的 entries 同一形状，
    // applyDerivedNodes 才能把派生槽写回正确的位次）。
    var out=ord.map(function(key,i){var g=groups[key];
      return {path:g.path,label:String(g.path[g.path.length-1]),
        values:measures.map(function(m){var vi=reqIndex[aggColumn(m)];return vi==null?0:colSeries[vi][i];})};});
    // 本地路径同样求值聚合级派生指标（与服务端 .then 链同一段逻辑）——否则
    // singlefile 的 ND/嵌套表引用占比/环比时整列退化成 base 列的和。依赖序列
    // 也走同一个 groupSeries（唯一聚合实现，不第二份）。
    if(plan&&plan.used&&plan.used.length&&out.length){
      plan.required.forEach(function(dep){
        plan.series[derivedDepKey(dep)]=groupSeries(dep.col,dep.agg,dep.filters);
      });
      var nd=dims.length;
      // 多维行表的窗口派生：游标按前 n-1 维分区独立推进（环比不跨 d1 组）。
      applyDerivedNodes(out,data,plan,dims[dims.length-1],
        nd>1?out.map(function(e){return e.path.slice(0,-1).join('\u0000');}):null);
    }
    return out;
  }
  function _pickMethod(method, measure, data){
    if(measure==='*')return 'count';
    var NUMERIC_AGGS={sum:1,average:1,min:1,max:1};
    function pick(){
      if(Array.isArray(method)){
        var idx=(data.measures||[]).indexOf(measure);
        return method[idx]||data.aggregation||'sum';
      }
      if(method && typeof method==='object'){
        return method[measure]||data.aggregation||'sum';
      }
      return method||data.aggregation||'sum';
    }
    var m=pick();
    // Channel semantics: a category/ordinal column in a measure slot degrades
    // to row-count instead of sum-silently collapsing to 0 (SQL CAST). The
    // backend-agnostic type checker reports this; we serve count to the server.
    if(m!=='count'&&m!=='none'&&NUMERIC_AGGS[m]){
      var act=channelAction(fieldSemantic(data,measure),'measure');
      if(act.action==='count')return 'count';
    }
    return m;
  }

  // 从 server 端拉一页原始明细行（read-only）。用于 server 模式的「明细」视图：
  // 客户端没有行数据，按页/排序/筛选从 /datasets/:id/rows 取。
  function fetchDetailRows(data,opts){
    var serverBase=normalizeServerBase();
    var datasetId=data&&(data.datasetId||data.id);
    if(!datasetId)return Promise.resolve(null);
    var payload={
      columns:(opts&&opts.columns)||[],
      page:Math.max(1,Number((opts&&opts.page)||1)),
      pageSize:Math.min(100,Math.max(1,Number((opts&&opts.pageSize)||50))),
      filter:(opts&&opts.filter)||[],
      orderBy:(opts&&opts.orderBy)||[]
    };
    return fetch(serverBase+'/datasets/'+encodeURIComponent(datasetId)+'/rows',{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)
    }).then(function(resp){return assertOk(resp,'rows');});
  }
  // 明细表「Σ 统计」（TBL-H4）：无分组全局聚合——groupBy:[] 时服务端返回单行，
  // measures = [{column,aggregation}]（agg ∈ sum/average/count）。响应 columns 与
  // rows 对齐，映射成 {col:value}。调用方负责把列分块（服务端 measures 上限 12）。
  function fetchGlobalAggregates(data,pairs,filter){
    var serverBase=normalizeServerBase();
    var datasetId=data&&(data.datasetId||data.id);
    if(!datasetId||!pairs||!pairs.length)return Promise.resolve({});
    var payload={groupBy:[],measures:pairs.slice(0,12),filter:filter||[],limit:1};
    return fetch(serverBase+'/datasets/'+encodeURIComponent(datasetId)+'/aggregate',{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)
    }).then(function(resp){return assertOk(resp,'aggregate');})
      .then(function(j){
        var row=(j&&j.rows&&j.rows[0])||[];
        var cols=(j&&j.columns)||[];
        var out={};cols.forEach(function(c,i){out[c]=row[i];});
        return out;
      });
  }
  // 字段配置「示例值」：只要几个真实取值（服务端 LIMIT 8 有界）——不拉整列值域。
  function fetchSampleValues(datasetId,column){
    var serverBase=normalizeServerBase();
    if(!datasetId||!column)return Promise.resolve([]);
    return fetch(serverBase+'/datasets/'+encodeURIComponent(datasetId)+'/sample/'+encodeURIComponent(column),{method:'GET'})
      .then(function(resp){return assertOk(resp,'sample');})
      .then(function(j){return (j&&j.values)||[];});
  }

  function fetchColumnValues(datasetId,column){
    var serverBase=normalizeServerBase();
    if(!datasetId||!column)return Promise.resolve([]);
    return fetch(serverBase+'/datasets/'+encodeURIComponent(datasetId)+'/values/'+encodeURIComponent(column),{method:'GET'})
      .then(function(resp){return assertOk(resp,'values');})
      .then(function(j){return (j&&j.values)||[];});
  }
  // 级联筛选选项源：某列在给定谓词（上级筛选的已选值）下的去重值域。
  // 一律走 POST /values/:col（服务端有界 LIMIT），绝不复用无界的 GET /values/:col
  // ——空谓词也走 POST（filter:[] 在服务端=无约束，LIMIT 仍然生效）。
  function fetchFilteredValues(datasetId,column,predicates){
    var serverBase=normalizeServerBase();
    if(!datasetId||!column)return Promise.resolve([]);
    return fetch(serverBase+'/datasets/'+encodeURIComponent(datasetId)+'/values/'+encodeURIComponent(column),
      {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({filter:Array.isArray(predicates)?predicates:[],limit:200})})
      .then(function(resp){return assertOk(resp,'values-filtered');})
      .then(function(j){return (j&&j.values)||[];});
  }

  var CATALOG=[
    {value:'bar',label:'柱状图',family:'比较',editable:true},
    {value:'stackedBar',label:'堆叠柱状图',family:'比较',editable:true},
    {value:'line',label:'折线图',family:'趋势',editable:true},
    {value:'area',label:'面积图',family:'趋势',editable:true},
    {value:'pie',label:'饼图',family:'构成',editable:true},
    {value:'donut',label:'环形图',family:'构成',editable:true},
    {value:'scatter',label:'散点图',family:'关系',editable:true},
    {value:'funnel',label:'漏斗图',family:'流程',editable:true},
    {value:'table',label:'表格',family:'表格',editable:true},
  ];

  function clone(value){return value==null?value:JSON.parse(JSON.stringify(value));}
  function number(value){var n=Number(value);return Number.isFinite(n)?n:0;}
  function chartCatalog(){return clone(CATALOG);}

  // 'price'（2026-10-13 批）= Flint Price intensive（refs/flint/src/core/
  // type-registry.ts:105）：格式与 currency 全同，唯一差别是不可加（缺省 average）。
  // Python 镜像 = scripts/semantics/type_registry.py CANONICAL/TYPE_RULES。
  var MEASURE_TYPE_KEYS=['currency','price','percent','count','duration','number','unitless'];
  var MEASURE_TYPE_FORMATS={
    currency:{axis:'¥{value}',prefix:'¥',suffix:''},
    price:{axis:'¥{value}',prefix:'¥',suffix:''},
    percent:{axis:'{value}%',prefix:'',suffix:'%'},
    count:{axis:'{value}',prefix:'',suffix:''},
    duration:{axis:'{value} 天',prefix:'',suffix:' 天'},
    number:{axis:'{value}',prefix:'',suffix:''},
    unitless:{axis:'{value}',prefix:'',suffix:''}
  };
  // 内置币表（拍板 6，docs/multidim-table-and-currency-design-2026-09-28.md §二）：
  // Flint CURRENCY_MAP（refs/flint/src/core/field-semantics.ts:234-238）14 币
  // + 亚太常用 8 币。**Python 镜像** = scripts/semantics/type_registry.py
  // ``CURRENCY_SYMBOLS``——两端内容（键+符号）必须一致，parity 由
  // tests/test_currency_types.py 钉死（对齐 date_parse 纪律）。
  var CURRENCY_SYMBOLS={USD:'$',EUR:'€',GBP:'£',JPY:'¥',CNY:'¥',KRW:'₩',INR:'₹',BRL:'R$',CAD:'CA$',AUD:'A$',CHF:'CHF',SEK:'kr',NOK:'kr',DKK:'kr',HKD:'HK$',TWD:'NT$',SGD:'S$',THB:'฿',MYR:'RM',PHP:'₱',IDR:'Rp',VND:'₫',RUB:'₽'};
  // 拍板 7：divisor 缺省按币种分档——CNY/JPY 维持万元口径（TYPE_RULES.currency 现状），
  // 其余币原值。只对**显式带 currency 键**且未声明 divisor/unit 的条目生效
  // （没有 currency 键 = 现状行为，零漂移）。Python 侧 = type_registry.apply_currency_defaults。
  var CURRENCY_DEFAULT_SCALES={CNY:{divisor:10000,unit:'万'},JPY:{divisor:10000,unit:'万'}};
  // 货币前缀解析的唯一入口：symbol 优先于查表，未知/缺省代码回落 ¥（CNY 缺省，
  // 拍板 1——validate_request 是拒绝未知代码的闸门，这里只做宽容渲染）。
  function currencyPrefixOf(currency,symbol){
    var sym=(symbol!=null&&String(symbol).trim()!=='')?String(symbol).trim():'';
    if(sym)return sym;
    var code=(currency!=null)?String(currency).trim().toUpperCase():'';
    return (code&&CURRENCY_SYMBOLS[code])||'¥';
  }
  // Raw measureTypes entry → display symbol（string 形态 / 缺键 = CNY 缺省 ¥）。
  // KPI（editor-runtime kpiDisplay/formatKpiValue）与拿不到 data+measure 上下文的
  // 调用点都用它。
  function currencySymbolOf(measureType){
    if(measureType&&typeof measureType==='object')return currencyPrefixOf(measureType.currency,measureType.symbol);
    return '¥';
  }
  // data+measure 上下文版（走 effectiveMeasureType 的同一取类型口径）。
  function currencySymbolFor(data,measure){
    if(!measure)return '¥';
    return currencySymbolOf(effectiveMeasureType(data,measure));
  }
  // Default display precision per semantic type. Every number on screen is
  // capped at this many decimals (a chart-/measure-level override may raise it,
  // never beyond the author's intent). Counts stay integers; money/percent are
  // normalised to 2. Set explicitly via editor.data.measureTypes[m].decimals.
  var MEASURE_TYPE_DECIMALS={currency:2,price:2,percent:2,count:0,duration:1,number:2,unitless:2};
  function roundNumber(value,decimals){var n=number(value);if(!Number.isFinite(n)||decimals==null)return n;var f=Math.pow(10,Math.max(0,decimals));return Math.round(n*f)/f;}
  function typeDecimals(type){return MEASURE_TYPE_DECIMALS[type]!=null?MEASURE_TYPE_DECIMALS[type]:2;}
  function chartDecimals(chart,data){
    // chart.editor.decimals is an explicit, global override (set by the panel's
    // 小数位数 control); otherwise fall back to per-measure defaults.
    if(chart&&chart.editor&&chart.editor.decimals!=null)return Math.max(0,Math.min(6,Number(chart.editor.decimals))||0);
    return null;
  }
  // ---- dimension semantics (P1) ----
  // Value-level "looks temporal" probe for dimensionKind: every spelling the
  // shared parser accepts (slash/dot/年月日/compact YYYYMMDD), otherwise a column
  // of 2026/9/5 would stay 'category' and lose its time axis + grain picker.
  // 季度标签（2025Q1 / 2025-Q1）与 PY date_parse.grain 同批认作时间形态：
  // 缺了它纯季度串列在 JS 侧判 category、PY 侧判 time，两端分叉。
  var DATE_RE=/(\d{4})[-/.年](\d{1,2})([-/.月]\d{1,2})?|^\d{8}$|^\d{4}[-/]?[Qq][1-4]$|^\d{4}[-/]?[Ww]\d{1,2}$|^[Ww]\d{1,2}$|^\d{1,2}周$|^第\d{1,2}周$|^\d{4}年\d{1,2}周$/;
  var ORDINAL_KEYWORDS=['月','段','级','季','周','年','档次','评分','分','Rank','rank'];
  function dimensionKind(data,dim){
    // Normalise schema objects [{name,type}] to plain name strings so indexOf
    // matches (server-mode datasets carry object columns). Without this, a
    // server-mode chart never finds its dimension in the column list and every
    // column is classified 'category' — breaking time-axis ordering/sorting.
    var columns=(data.columns||[]).map(function(c){return typeof c==='object'&&c!==null?(c.name||''):c;});
    var rows=data.rows||[],ci=columns.indexOf(dim);
    if(ci<0)return 'category';
    // A user-set field role beats auto-detection (semantic type / name / value
    // heuristics) for the axis-kind so a reclassified field sorts + renders right.
    var ovr=data&&data.fieldRoles&&data.fieldRoles[dim];
    if(ovr&&ovr.role){if(ovr.role==='time')return 'time';if(ovr.role==='ordinal')return 'ordinal';if(ovr.role==='category')return 'category';}
    // Flint semantic path (moved-in flint_core): a declared semantic type drives
    // the axis kind via its visEncodings, so live editing agrees with the
    // compiler (e.g. a DateGranule year stays a time axis, a rank stays ordinal).
    var semType=null;
    if(data.measureTypes&&data.measureTypes[dim]){var rawMt=data.measureTypes[dim];semType=typeof rawMt==='string'?rawMt:(rawMt.type||'');}
    if(semType){var enc=flintFieldSemantics(semType).encoding;
      if(enc==='temporal')return 'time';
      if(enc==='ordinal')return 'ordinal';
      // A declared quantitative measure is a value — never a time axis. Without
      // this, a count column whose values happen to be 4-digit numbers (e.g.
      // Sell In = 1902) gets matched by the bare-year heuristic below and is
      // wrongly treated as a date, corrupting rawDateSource/bucketing.
      if(enc==='quantitative')return 'category';}
    var name=String(dim);
    if(/日期|时间|date|day|month|year/i.test(name))return 'time';
    var values=rows.map(function(row){return String(row[ci]??'');}).filter(function(v){return v.trim()!=='';});
    if(!values.length)return 'category';
    // temporal: most values are date-like (yyyy / yyyy-mm-dd / yyyy年m月 / yyyy年).
    // A bare \d{4} is only a year when the column name carries a year hint
    // (年/年份/year) — otherwise 1902 is a count, not a date (mirrors Python
    // _dimension_type, so a count column never misreads as a time axis).
    var hasYearHint=/年|年份|year/i.test(name);
    var dateLike=values.filter(function(v){
      if(DATE_RE.test(v))return true;
      if(/^\d{4}年?$/.test(v.trim()))return hasYearHint;
      return false;
    }).length;
    if(dateLike/values.length>0.6)return 'time';
    // ordinal: name carries an order keyword, or values are a small numbered
    // band (满意 1分..5分, 等级 一/二/三级) — a natural order that desc-sorting
    // would jumble.
    if(ORDINAL_KEYWORDS.some(function(k){return name.indexOf(k)>=0;}))return 'ordinal';
    var banded=values.filter(function(v){return /^\d+[分星级档级]?$/.test(v.trim());}).length;
    if(values.length<=12&&banded/values.length>0.6)return 'ordinal';
    return 'category';
  }
  // ---- reference lines (markLine) ----
  // Only charts with a cartesian value axis get reference lines (bar/line/
  // area/stacked). Share charts (pie/funnel) have
  // no value axis, so a reference line would be meaningless (Issue 4).
  var REFLINE_TYPES={bar:1,line:1,area:1,stackedBar:1};
  function applyReferences(option,data,refs,decimals,seriesIndex){
    if(!refs||!Array.isArray(option.series)||!option.series.length)return option;
    var si=seriesIndex||0;
    var series=option.series[si];
    if(!series)return option;
    var seriesType=series.type;
    if(!REFLINE_TYPES[seriesType])return option;
    var measure=(data.measures||[])[si]||(data.measures||[])[0];
    var fmt=measureFormat(data,measure,decimals);
    var unit=fmt&&fmt.suffix?fmt.suffix:'';var prefix=fmt&&fmt.prefix?fmt.prefix:'';
    var values=series.data||[];
    var numbers=values.map(function(v){return number(v&&typeof v==='object'?'value' in v?v.value:v:v);}).filter(function(v){return Number.isFinite(v);});
    var avg=numbers.length?numbers.reduce(function(a,b){return a+b;},0)/numbers.length:0;
    var markLine={silent:true,symbol:'none',lineStyle:{type:'dashed',color:'#94a3b8'},label:{formatter:function(p){var val=prefix+roundNumber(p.value,decimals!=null?decimals:2)+unit;return p.name?p.name+' '+val:val;},position:'insideEndTop',fontSize:10,color:'#475569'},data:[]};
    if(refs.type==='avg'||refs.avg===true)markLine.data.push({type:'average',name:'均值',lineStyle:{color:'#94a3b8'}});
    if(refs.type==='median'||refs.median===true){if(numbers.length){var s=numbers.slice().sort(function(a,b){return a-b;});var med=s[Math.floor((s.length-1)/2)];markLine.data.push({yAxis:roundNumber(med,decimals!=null?decimals:2),name:'中位数',lineStyle:{color:'#94a3b8'}});}}
    if(refs.type==='goal'&&refs.goal!=null&&Number.isFinite(Number(refs.goal)))markLine.data.push({yAxis:Number(refs.goal),name:'目标',lineStyle:{color:'#2563eb'}});
    if(!refs.type&&refs.goal!=null&&Number.isFinite(Number(refs.goal)))markLine.data.push({yAxis:Number(refs.goal),name:'目标',lineStyle:{color:'#2563eb'}});
    if(refs.min!=null&&Number.isFinite(Number(refs.min)))markLine.data.push({yAxis:Number(refs.min),name:'下限',lineStyle:{color:'#cbd5e1'}});
    if(refs.max!=null&&Number.isFinite(Number(refs.max)))markLine.data.push({yAxis:Number(refs.max),name:'上限',lineStyle:{color:'#cbd5e1'}});
    if(markLine.data.length)series.markLine=markLine;
    return option;
  }
  // Issue 3 (redesigned) + Issue 4 (merged): highlight the max/min data
  // points on a cartesian series. Renders a small colored dot at the extreme
  // point with a value label above it and a tiny type label (最大值/最小值).
  // Colors: max=red, min=blue. mean/median belong to the reference-line feature
  // (markLine) — a mean is a level line, not a data point, so it is NOT drawn
  // as a dot. If `showValueLabels` is false (the 显示数据标签 toggle is off),
  // only the extreme points are annotated — no per-bar labels.
  function applyMarkPoint(option,data,cfg,decimals,showValueLabels,seriesIndex){
    if(!cfg||(cfg.max!==true&&cfg.min!==true&&(!Array.isArray(cfg.custom)||!cfg.custom.length)))return;
    var si=seriesIndex||0;
    var series=option.series&&option.series[si];
    if(!series||['pie','donut','funnel','table'].indexOf(series.type)>=0)return;
    var measure=(data&&data.measures||[])[si]||(data&&data.measures||[])[0];
    var fmt=measure?measureFormat(data,measure):null;
    var prefix=fmt&&fmt.prefix?fmt.prefix:'';
    var suffix=fmt&&fmt.suffix?fmt.suffix:'';
    function valueLabel(p){return prefix+roundNumber(p.value,decimals!=null?decimals:2)+suffix;}
    // Type label sits below the value; value on top, type beneath (smaller).
    function pointDef(type,name,color,kind){
      return {
        type:type,name:name,itemStyle:{color:color},
        label:{
          show:true,position:kind==='min'?'bottom':'top',fontSize:10,fontWeight:600,color:'#334155',
          formatter:function(p){return valueLabel(p)+'\n'+name;}
        }
      };
    }
    var mp={data:[],symbolSize:12,symbol:'circle',label:{show:true,position:'top',fontSize:10,fontWeight:600,color:'#334155'}};
    if(cfg.max===true)mp.data.push(pointDef('max','最大值','#dc2626','max'));
    if(cfg.min===true)mp.data.push(pointDef('min','最小值','#2563eb','min'));
    if(Array.isArray(cfg.custom))cfg.custom.forEach(function(c){if(c&&c.name)mp.data.push(c);});
    series.markPoint=mp;
    // When the user has NOT enabled per-bar data labels, keep the bars clean
    // and only the extreme points annotated.
    if(!showValueLabels&&series.label)series.label.show=false;
  }
  // Families whose geometry the adapter can rebuild safely, so semantic
  // formatting is guaranteed to line up with the data.
  var AUTO_FORMAT_SERIES={bar:1,line:1,pie:1,funnel:1};
  // ---- measure semantics: field -> channel (Flint-inspired, scaled down) ----
  // A single source of truth for "what does this column mean" (field) and
  // "what can it do in this position" (channel). Replaces scattered name-regex
  // guesses so an author's intent is explained once, then enforced everywhere.
  var T2_CANON={'currency':1,'price':1,'percent':1,'count':1,'duration':1,'number':1,'unitless':1};
  // T1/T0 family vocabulary -> canonical T2 type. Mirrors scripts/measure_semantics.py.
  // 价族（售价/单价/价格/price）→ 'price'（intensive），在金额族**之前**判定——
  // Flint 把 Price 与 Revenue 分家（design-semantics.md §2.4），价格不再折叠成可加金额。
  var FAMILY_WORDS={
    money:'currency',currency:'currency',cost:'currency',revenue:'currency',amount:'currency',sales:'currency',profit:'currency',
    '金额':'currency','成本':'currency','利润':'currency','费用':'currency','消费':'currency','销售额':'currency','营业收入':'currency',
    price:'price','售价':'price','单价':'price','价格':'price','标价':'price','原价':'price','现价':'price','均价':'price',
    percent:'percent',ratio:'percent',rate:'percent','占比':'percent','比例':'percent','百分比':'percent','率':'percent','比率':'percent',
    duration:'duration','时长':'duration','耗时':'duration','用时':'duration',
    count:'count','人数':'count','用户数':'count','数量':'count','次数':'count','记录数':'count','条数':'count',
    number:'number',measure:'number','数值':'number','数量':'number','指标':'number',quantity:'number'
  };
  function normalizeMeasureType(raw){
    if(!raw)return null;
    if(typeof raw==='object'){
      var t=raw.type||'number';
      if(!T2_CANON[t])t=FAMILY_WORDS[String(t).toLowerCase()]||t;
      if(!T2_CANON[t])return null;
      return Object.assign({},raw,{type:t});
    }
    var s=String(raw).trim().toLowerCase();
    if(T2_CANON[s])return s;
    return FAMILY_WORDS[s]||FAMILY_WORDS[String(raw).trim()]||null;
  }
  // A measure is non-additive when its semantic type says so, OR a storage
  // hint (ratio/percent tokens in the name) agrees. Computed once per field.
  // price（Flint Price intensive，2026-10-13 批）与 percent/unitless 同闸：
  // 不可 sum、不可进堆叠（channelAction reject / TOP-K addMask / 合计闸同源）。
  var NONADDITIVE_TYPE={percent:1,unitless:1,price:1};
  // Field-role override: a user-set semantic role (measure/time/category/ordinal)
  // that beats the model's auto-detection. Stored on the dataset as fieldRoles[col]
  // and read FIRST by every semantic + formatting + axis decision so a field can
  // be reclassified (time/category/numeric) after the fact.
  function fieldRole(data,col){return (data&&data.fieldRoles&&data.fieldRoles[col])||null;}
  // Effective measure type: an explicit role's measureType wins; otherwise the
  // dataset/measureTypes tag (the model's auto-detection).
  function effectiveMeasureType(data,col){
    var ovr=fieldRole(data,col);
    if(ovr&&ovr.role==='measure')return ovr.measureType||'number';
    return (data&&data.measureTypes&&data.measureTypes[col])||null;
  }
  // Name-based semantic fallback (mirrors Python spec_to_report._measure_sem_label):
  // when a measure has no authored measureType, infer a Flint semantic type from
  // column-name keywords so rate/amount/count columns keep the same zero-baseline
  // on the JS side as the Python compile (e.g. a 转化率 line should data-fit, not
  // pin 0). Returns a canonical T2 label or null.
  function semanticNameType(col){
    var n=String(col);
    if(/率|占比|百分比|比例|rate|ratio|percent/i.test(n))return 'percent';
    if(/金额|销售额|收入|价|额|revenue|amount|price|cost/i.test(n))return 'currency';
    if(/量|数|个|件|笔|count|qty|数量/i.test(n))return 'count';
    return null;
  }
  function semanticType(data,col){
    var mt=effectiveMeasureType(data,col);
    if(mt&&typeof mt==='object')return mt.type||'number';
    if(mt&&typeof mt==='string')return mt;
    return semanticNameType(col)||'number';
  }
  function fieldSemanticNonAdditive(data,col,measureType){
    if(NONADDITIVE_TYPE[measureType])return true;
    var name=String(col);
    if(/率|占比|百分比|比例|%.*%|rate|ratio|percent/i.test(name))return true;
    return false;
  }
  function fieldSemantic(data,col){
    // A user-set field role beats auto-detection (model measureTypes + shape).
    var ovr=fieldRole(data,col);
    if(ovr&&ovr.role){
      var kind=ovr.role;
      var mtype=(kind==='measure')?(ovr.measureType||'number'):null;
      return {kind:kind,measureType:mtype,nonAdditive:fieldSemanticNonAdditive(data,col,mtype)};
    }
    var mt=data&&data.measureTypes&&data.measureTypes[col];
    var resolved=mt?normalizeMeasureType(mt):null;
    var measureType=resolved&&(typeof resolved==='object'?resolved.type:resolved);
    var numeric=data?numericColumns(data).indexOf(col)>=0:false;
    var dtw=data?dimensionKind(data,col):'category';
    var kind=measureType? 'measure'
      : (numeric? 'measure'
      : (dtw==='time'?'time' : (dtw==='ordinal'?'ordinal' : 'category')));
    // A column explicitly typed as a measure wins over storage-format guess.
    if(measureType)kind='measure';
    if(kind==='measure'&&!measureType)measureType='number';
    return {kind:kind,measureType:measureType,nonAdditive:fieldSemanticNonAdditive(data,col,measureType)};
  }
  // Channel semantics: given a field and the channel it lands in, decide how the
  // chart should treat it. This is where a category dragged into a measure slot
  // degrades to a row-count (or is rejected) rather than sum-silently collapsing
  // to 0. Never mutates data — it reports an action the caller applies.
  function channelAction(field,channel,chartType){
    var kind=field.kind||'category',nt=field.measureType;
    if(channel==='measure'){
      if(kind==='measure'){
        // Non-additive (percent/unitless) cannot go into a stacked share chart.
        if(field.nonAdditive&&(chartType==='stackedBar'||chartType==='area'))
          return {action:'reject',reason:'nonAdditiveMeasureInStacked',field:field};
        return {action:'allow',field:field};
      }
      if(kind==='time')return {action:'reject',reason:'timeAsMeasure',field:field};
      // category / ordinal into a measure slot: honest reading is row-count.
      return {action:'count',reason:'categoryAsMeasure',field:field};
    }
    if(channel==='breakdown'){
      // Only a categorical/ordinal/time field can split a measure.
      if(kind==='measure')return {action:'reject',reason:'measureAsBreakdown',field:field};
      return {action:'allow',field:field};
    }
    if(channel==='dimension'){
      if(kind==='measure'&&field.nonAdditive&&chartType==='stackedBar')
        return {action:'reject',reason:'nonAdditiveMeasureInStacked',field:field};
      return {action:'allow',field:field};
    }
    return {action:'allow',field:field};
  }
  // A measure is non-additive when its semantic type says so, OR a storage
  // hint (name/ratio token) agrees. Uses the semantic layer, not ad-hoc regex.
  function nonAdditiveMeasure(data,measure){
    return fieldSemantic(data,measure).nonAdditive;
  }
  function anyNonAdditive(data){return (data.measures||[]).some(function(m){return nonAdditiveMeasure(data,m);});}
  function resolveMeasureType(raw,fallbackDecimals){
    var resolved;
    if(typeof raw==='string')resolved={type:raw,divisor:1,unit:''};
    else if(raw&&typeof raw==='object'){
      resolved={type:raw.type||'number',divisor:Number(raw.divisor)||1,unit:String(raw.unit||''),decimals:raw.decimals!=null?Number(raw.decimals):null};
      // 透传 currency/symbol（旧实现逐字段拷贝会丢新键）。代码宽容大写化——
      // validate 归一是闸门，这里只兜手改 manifest 的形态。
      resolved.currency=(raw.currency!=null&&String(raw.currency).trim()!=='')?String(raw.currency).trim().toUpperCase():undefined;
      resolved.symbol=(raw.symbol!=null&&String(raw.symbol).trim()!=='')?String(raw.symbol).trim():undefined;
      // 拍板 7：显式 currency 声明且未写 divisor/unit → 按 code 分档取缺省
      // （CNY/JPY → 万元口径，其余 → 原值）。无 currency 键不动（零漂移）。
      // price（2026-10-13 批）同享币种机制（格式与 currency 全同）。
      if((resolved.type==='currency'||resolved.type==='price')&&raw.currency!=null&&raw.divisor==null&&raw.unit==null){
        var dft=CURRENCY_DEFAULT_SCALES[resolved.currency];
        resolved.divisor=dft?dft.divisor:1;
        resolved.unit=dft?dft.unit:'';
      }
    }
    else return null;
    if(resolved.decimals==null)resolved.decimals=typeDecimals(resolved.type);
    if(typeof fallbackDecimals==='number')resolved.decimals=fallbackDecimals; // chart-level override wins
    return resolved;
  }
  function measureFormat(data,measure,fallbackDecimals){
    if(!measure)return null;
    var rawMt=effectiveMeasureType(data,measure);
    var resolved=resolveMeasureType(rawMt,fallbackDecimals);
    if(!resolved)return null;
    // Per-measure decimal override (data.decimals[measure], set by the measure
    // chip's 小数位数 selector) wins over both the type default and the global
    // chart-level override.
    if(data&&data.decimals&&data.decimals[measure]!=null)resolved.decimals=Math.max(0,Math.min(6,Number(data.decimals[measure]))||0);
    // The aggregation method can override the column's semantic format: a count
    // of a currency column is still a plain count (no ¥/万), and 'none' keeps the
    // raw value. Per-measure override (data.aggregations[measure]) wins over the
    // global data.aggregation.
    var per=(data.aggregations&&typeof data.aggregations==='object'&&!Array.isArray(data.aggregations))?data.aggregations[measure]:null;
    var meth=per||data.aggregation||'sum';
    if(meth==='count')return {type:'count',divisor:1,decimals:0,prefix:'',suffix:'',axis:'{value}'};
    if(meth==='none')return {type:'number',divisor:1,decimals:resolved.decimals,prefix:'',suffix:'',axis:'{value}'};
    // P5: format='percent' on the raw measureTypes entry overrides the
    // type-based display format. A number column carrying format='percent'
    // (the canonical P1 normalised shape) gets percent's axis formatter + %
    // suffix even though type stays 'number' (additive remains true). The
    // value is a ratio (0..1 or 0..100) and MEASURE_TYPE_FORMATS.percent's
    // axis label already includes the % suffix.
    var hasPercentFormat=rawMt&&typeof rawMt==='object'&&rawMt.format==='percent';
    var base=hasPercentFormat?MEASURE_TYPE_FORMATS.percent:MEASURE_TYPE_FORMATS[resolved.type];
    if(!base)return null;    // Unit scale (K / 万 / 百万 / 原始): the unit FOLLOWS THE Y-AXIS. The value
    // axis (0=左, 1=右) owns the unit (data.axisConfig[i].unit); every measure on
    // that axis shares it, so two measures on one axis never show inconsistent
    // units. Falls back to the per-measure unitScale / authored measureTypes
    // divisor+unit when no axis unit is set (backward compat / authored currency-万).
    var axisIndex=seriesAxisIndex(data,measure);
    var axisCfg=(data.axisConfig&&data.axisConfig[axisIndex])||{};
    var scale=(axisCfg.unit!=null)?axisCfg.unit:((data.unitScale&&data.unitScale[measure])||null);
    var eff=resolveUnitScale(data,measure,scale,resolved);
    var suffix=eff.suffix!==''?eff.suffix:base.suffix;
    // If the user set an explicit scale, its divisor wins; otherwise keep the
    // authored divisor so authored currency-万 / counts don't change.
    var divisor=(scale!=null)?eff.divisor:resolved.divisor;
    // 货币前缀动态解析（拍板 1/2）：symbol 优先于查表，缺省 ¥（CNY）。静态表
    // currency 行保留为 CNY 缺省；format='percent' 覆盖时前缀本来就是空。
    // 指标芯片「货币符号」开关（2026-09-28）：data.hideCurrency[measure]=true 时
    // 前缀**显式置空**（不是跳过覆盖——那会漏出静态表默认 ¥），type/divisor/suffix
    // 语义全保留（万元口径等不受影响），面板勾选即回显。
    var prefix=base.prefix;
    if((resolved.type==='currency'||resolved.type==='price')&&!hasPercentFormat){
      prefix=(data.hideCurrency&&data.hideCurrency[measure])?'':currencyPrefixOf(resolved.currency,resolved.symbol);
    }
    return {type:resolved.type,divisor:divisor,decimals:resolved.decimals,prefix:prefix,suffix:suffix,axis:prefix+'{value}'+suffix};
  }
  // Effective divisor + unit for a per-measure unit scale. `auto` picks K/万/百万
  // from the largest value actually plotted, so values and axis stay in sync.
  var UNIT_SCALES={k:{divisor:1000,suffix:'K'},wan:{divisor:10000,suffix:'万'},m:{divisor:1000000,suffix:'百万'},yi:{divisor:10000000,suffix:'千万'}};
  // 一根指标的 Y 轴归属（轴侧解析的唯一真源）：显式 seriesAxis 优先；其次旧的
  // axisConfig[指标下标].position（作者直接在 axisConfig 上钉过左右）。此前
  // measureFormat 只有 seriesAxis→0 两级回退，与 buildOption 的系列分轴
  // （seriesAxis→position→dual 自动）不一致 —— 右轴单位改了却只作用于左轴格式，
  // 右轴单位设置因此「无效」（真机验收 2026-09-13）。
  function seriesAxisIndex(data,measure){
    if(data&&data.seriesAxis&&data.seriesAxis[measure]!=null)return Number(data.seriesAxis[measure])||0;
    var ms=(data&&data.measures)||[];
    var mi=ms.indexOf(measure);
    var ac=(mi>=0&&data&&data.axisConfig&&data.axisConfig[mi])||null;
    if(ac&&ac.position==='right')return 1;
    // dual-auto（验收 2026-09-14）：镜像 buildOption 的 auto 分轴 —— 恰好 2 个指标、
    // 谁都没被显式钉侧、模式非 shared 时，尺度比 >3 → 第 2 个指标自动挂右轴。
    // 缺了这一级，自动分轴的右轴指标会按左轴单位换算，右轴单位跟着失效。
    // 尺度比按**原始**聚合值算（applyMeasureScale 先算好 _measureMaxBy）；
    // 不回吐 measureFormat 以免循环。
    if(ms.length===2&&mi===1){
      var acm=(data&&data.axisConfig)||{};
      var mode=acm.mode||'auto';
      if(mode==='split')return 1;
      if(mode==='auto'){
        var m0=(data._measureMaxBy&&data._measureMaxBy[ms[0]])||0;
        var m1=(data._measureMaxBy&&data._measureMaxBy[ms[1]])||0;
        var lo=Math.min(m0,m1)||1,hi=Math.max(m0,m1)||1;
        if(hi/lo>3)return 1;
      }
    }
    return 0;
  }
  // 轴名称的单位后缀（与 buildOption nameWithUnit 同一取单位口径）：取该轴上
  // 第一根系列对应指标的 measureFormat.suffix；找不到系列时回落到第 idx 个指标。
  function axisUnitSuffixText(data,option,idx){
    var ms=(data&&data.measures)||[];
    var m=null;
    ((option&&option.series)||[]).forEach(function(s){
      if(m==null&&s&&s.name&&(s.yAxisIndex||0)===idx){var i=ms.indexOf(s.name);if(i>=0)m=s.name;}
    });
    if(m==null)m=ms[idx]||ms[0]||null;
    var fmt=m?measureFormat(data,m):null;
    return (fmt&&fmt.suffix)?('（'+fmt.suffix+'）'):'';
  }
  function resolveUnitScale(data,measure,scale,resolved){
    if(scale==='raw')return {divisor:1,suffix:''};
    if(UNIT_SCALES[scale])return UNIT_SCALES[scale];
    if(scale==='auto'){
      var maxAbs=(data&&data._measureMaxBy&&data._measureMaxBy[measure]!=null)?data._measureMaxBy[measure]:0;
      if(maxAbs>=1e7)return UNIT_SCALES.yi;
      if(maxAbs>=1e6)return UNIT_SCALES.m;
      if(maxAbs>=1e4)return UNIT_SCALES.wan;
      if(maxAbs>=1000)return UNIT_SCALES.k;
      return {divisor:1,suffix:''};
    }
    // no override (undefined/null): keep authored divisor + unit
    return {divisor:resolved.divisor,suffix:resolved.unit||''};
  }
  function columnMaxAbs(data,measure){
    var rows=data&&data.rows||[],ci=(data&&data.columns||[]).indexOf(measure);
    if(ci<0)return 0;
    var m=0;for(var i=0;i<rows.length;i++){var v=number(rows[i][ci]);if(isFinite(v)&&Math.abs(v)>m)m=Math.abs(v);}
    return m;
  }
  function applyMeasureScale(entries,data,fallbackDecimals){
    if(!entries||!data||!data.measureTypes)return entries;
    var measures=data.measures||[];
    // Probe the largest value actually being plotted (per measure) so the
    // 单位=自动 scale is chosen from the real chart magnitude, not raw rows
    // (which may be absent for a chart whose data is stored separately).
    var maxByMeasure={};
    measures.forEach(function(measure,index){var m=0;entries.forEach(function(e){var v=Math.abs(number(e.values[index]));if(isFinite(v)&&v>m)m=v;});maxByMeasure[measure]=m;});
    data._measureMaxBy=maxByMeasure;
    // Derived measures (同环比/占比/累计): compute each from its base column
    // BEFORE scaling, on the raw entry values. data.derived[token] = {kind, base}.
    // 第 5 参 dataset 必须传：share=parent 要读数据集 hierarchy。ensureEditorData
    // 的合并产物（Object.assign({},dataset,selection)）本身就带 hierarchy/columns/
    // rows——漏传会让「数据集明明声明了层级」的旧图也抛「需要数据集的 hierarchy」。
    if(data.derived)applyDerivedMeasures(entries,data.measures,data.derived,data,data);
    measures.forEach(function(measure,index){
      // Derived measures are already computed (and %/count formatted); do not
      // re-round them through the base-measure scale (which coerces null→0).
      if((data.derivedMeasures&&data.derivedMeasures[measure])||(data.derived&&data.derived[measure]&&data.derived[measure].base))return;
      var format=measureFormat(data,measure,fallbackDecimals);
      if(!format)return;
      entries.forEach(function(entry){var v=number(entry.values[index]);entry.values[index]=roundNumber(format.divisor!==1?v/format.divisor:v,format.decimals);});
    });
    return entries;
  }
  // #region DERIVED_EVAL_BEGIN
  // ---- aggregate-level derived measures (P2) --------------------------------
  // Mirror of scripts/derived_eval.py (`evaluate` / `required_aggregations`).
  // Parity fixture: tests/fixtures/derived-eval.json, asserted by
  // tests/test_derived_model.cjs → the two implementations may never drift.
  //
  // Input is already-aggregated series (compile: from the raw table; runtime:
  // from the local aggregate or `/aggregate`); this evaluates `formula` nodes
  // (ratios of sums), `window` nodes (环比/同比/累计/年累计/组内排名 =
  // fn+offset+grain / rank 族) and `share`
  // nodes (占比 = 值 ÷ 分母，分母由 scope 决定) on top.
  var DERIVED_AGG_PREFIX='agg:';
  function derivedAggKey(agg,col){return DERIVED_AGG_PREFIX+agg+':'+col;}
  // 筛选型派生指标（kind='filtered'）的依赖序列键 —— scripts/derived_eval.fm_key 的
  // 镜像。filtered 的值在聚合阶段物化（服务端按 measure.filters 条件聚合 / 本地在
  // 主聚合循环里组内筛选累加），键与普通 {agg,col} 区分：同一列可带不同筛选多次请求。
  var DERIVED_FM_PREFIX='fm:';
  function derivedFmKey(node){return DERIVED_FM_PREFIX+((node&&node.id)||node.name||'');}
  function derivedDepKey(dep){return (dep&&dep.key)||derivedAggKey(dep&&dep.agg,dep&&dep.col);}
  function derivedDefaultAgg(defaultAgg,col){
    if(defaultAgg&&typeof defaultAgg==='object')return defaultAgg[col]||'sum';
    return defaultAgg||'sum';
  }
  // 求值闭包（mirror of scripts/derived_eval.py::closure_nodes）：图上指标沿 {ref}
  // 引用可达的**全部**声明节点——公式引用不在图上的派生指标（M1=M2/2）时，M2 必须
  // 先算出来，否则 derivedEvaluate 在 resolveRef 处抛「引用了未知序列」。
  function derivedClosureNodes(nodes,byName){
    var nmap={};
    (byName||[]).forEach(function(n){if(n&&n.name)nmap[n.name]=n;});
    (nodes||[]).forEach(function(n){if(n&&n.name)nmap[n.name]=n;});
    var seen={},out=[];
    function refsOf(n){
      var refs=[];
      if(!n||typeof n!=='object')return refs;
      if(n.kind==='formula'){
        var stack=[n.expr||null];
        while(stack.length){
          var cur=stack.pop();
          if(!cur||typeof cur!=='object')continue;
          if(cur.op!==undefined){stack.push(cur.left);stack.push(cur.right);}
          else if(cur.ref!==undefined&&cur.ref!==null)refs.push(cur.ref);
        }
        return refs;
      }
      var inp=n.input||{};
      if(inp&&inp.ref!==undefined&&inp.ref!==null)refs.push(inp.ref);
      return refs;
    }
    function visit(name){
      if(seen[name])return;
      seen[name]=1;
      var node=nmap[name];
      if(!node)return;
      refsOf(node).forEach(function(r){if(nmap[r])visit(r);});
    }
    (nodes||[]).forEach(function(n){if(n&&n.name)visit(n.name);});
    Object.keys(nmap).forEach(function(k){if(seen[k]&&out.indexOf(nmap[k])<0)out.push(nmap[k]);});
    (nodes||[]).forEach(function(n){if(n&&out.indexOf(n)<0)out.push(n);});
    return out;
  }
  function derivedRequiredAggregations(nodes,declared,defaultAgg,byName){
    // byName（可选）：ref 命中**已声明节点** ≠ 依赖已满足——沿节点递归收集它自己
    // 的依赖（declared 名单曾把「已声明但不在本图上」的指标当成满足，公式引用
    // 未上图指标时运行时抛可行动错误）。与 PY required_aggregations 逐位同构。
    var nmap={};
    (byName||[]).forEach(function(n){if(n&&n.name)nmap[n.name]=n;});
    (nodes||[]).forEach(function(n){if(n&&n.name)nmap[n.name]=n;});
    var names={};
    (declared||[]).forEach(function(n){names[n]=1;});
    Object.keys(nmap).forEach(function(k){names[k]=1;});
    var out=[],seen={},visited={};
    function add(agg,col,filters,key){
      var k=key||derivedAggKey(agg,col);
      if(seen[k])return;seen[k]=1;
      var dep={agg:agg,col:col,key:k};
      if(filters)dep.filters=filters;
      out.push(dep);
    }
    function defaultFor(col){return derivedDefaultAgg(defaultAgg,col);}
    function expandRef(ref){
      var target=nmap[ref];
      if(target)walkNode(target);
      else if(!names[ref])add(defaultFor(ref),ref);
    }
    function walkExpr(n){
      if(!n||typeof n!=='object')return;
      if(n.op!==undefined){walkExpr(n.left);walkExpr(n.right);}
      else if(n.col!==undefined)add(n.agg||defaultFor(n.col),n.col);
      else if(n.ref!==undefined&&n.ref!==null)expandRef(n.ref);
    }
    function walkNode(node){
      if(node.name){if(visited[node.name])return;visited[node.name]=1;}
      if(node.kind==='formula'){walkExpr(node.expr);return;}
      if(node.kind==='filtered'){
        // 条件聚合依赖（col 缺省 = count 计行数 → 服务端合成列 "*"）。
        add(node.agg||'count',node.col||'*',node.filters||[],derivedFmKey(node));
        return;
      }
      var inp=node.input||{};
      if(inp.agg&&inp.col)add(inp.agg,inp.col);
      else if(inp.ref)expandRef(inp.ref);
    }
    (nodes||[]).forEach(function(node){if(node)walkNode(node);});
    return out;
  }
  // JS Math.round(v*10^d)/10^d semantics, mirrored exactly in Python
  // (derived_eval._round_half_up = floor(v*f+0.5)/f) so both sides agree bit for bit.
  function derivedRoundHalfUp(v,d){var f=Math.pow(10,d);return Math.floor(v*f+0.5)/f;}
  function derivedFinite(v){
    if(v===null||v===undefined||v==='')return null;
    if(typeof v==='boolean')return null;
    var n=Number(v);return Number.isFinite(n)?n:null;
  }
  // label → comparable time key (calendar order via the shared date parser)
  function derivedTimeKey(label){
    if(label===null||label===undefined)return '';
    var s=String(label);
    var p=(typeof dateParts==='function')?dateParts(s):null;
    if(!p)return s;
    function pad(n,w){var t=String(Math.abs(Number(n)||0));while(t.length<w)t='0'+t;return t;}
    var neg=(p.y||0)<0?'-':'';
    // dateParts returns {y, mo, d} — NOT {y, m, d}. Reading `p.m` silently zeroed
    // every month, so all labels of one year compared EQUAL, the sort became a
    // no-op and the window offset walked the aggregate's order (real bug: 净流入
    // 月环比 was computed against the wrong periods).
    return neg+pad(p.y,4)+'-'+pad(p.mo,2)+'-'+pad(p.d,2);
  }
  // WP-6 查询级窗口算子扩展（mirror of scripts/derived_eval.py::_label_year /
  // _rank_series — 同一 fixture tests/fixtures/derived-eval.json 双向锁）。
  // 排名族 partition = 当前聚合维度组合（求值入参的整条序列），无时间语义：
  // 不走 timeField/bucket 闸门；按值降序，空值不参与（排名保持 null）。
  //   rank → 并列 1,2,2,4；dense_rank → 1,2,2,3；
  //   percent_rank → 平均并列（1, 2.5, 2.5, 4）÷ 非空行数 ×100（1 位小数，
  //     与 window.rate 同为「已 ×100 的百分数单位」，derivedPercentUnits 豁免生效）。
  var DERIVED_RANK_FNS=['rank','dense_rank','percent_rank'];
  function derivedRankSeries(src,fn){
    var vals=src.map(function(v){return derivedFinite(v);});
    var out=vals.map(function(){return null;});
    var present=[];
    vals.forEach(function(v,i){if(v!==null)present.push([v,i]);});
    present.sort(function(a,b){return (b[0]-a[0])||((a[1]<b[1])?-1:1);});
    if(fn==='percent_rank'){
      var total=present.length,pos=0;
      while(pos<total){
        var end=pos;
        while(end+1<total&&present[end+1][0]===present[pos][0])end+=1;
        var avgRank=(pos+end)/2+1;   // 该并列段 1-based 名次的平均值
        for(var k=pos;k<=end;k+=1)out[present[k][1]]=derivedRoundHalfUp(avgRank/total*100,1);
        pos=end+1;
      }
      return out;
    }
    var rank=0,dense=0;
    present.forEach(function(pair,i){
      if(i===0||pair[0]!==present[i-1][0]){rank=i+1;dense+=1;}
      out[pair[1]]=(fn==='rank')?rank:dense;
    });
    return out;
  }
  // ytd 的年重置取年：真源 = dateParts（date_parse 的 JS 镜像）；沙箱/不可解析标签
  // 退化为与 Python 侧相同的 \d{4} 正则——两侧要么同 year、要么都无 year（绝不
  // 只在一侧重置）。无 year 的标签不触发重置（确定性契约）。
  function derivedYearOf(label){
    var s=(label===null||label===undefined)?'':String(label);
    if(s){
      var p=(typeof dateParts==='function')?dateParts(s):null;
      if(p&&p.y)return Number(p.y);
    }
    var m=s.match(/(\d{4})/);
    return m?Number(m[1]):null;
  }
  function derivedEvaluate(nodes,series,labels,opts){
    opts=opts||{};
    var dimension=opts.dimension||null,bucket=opts.bucket||null,parentOf=opts.parentOf||null,partitionOf=opts.partitionOf||null;
    var byName={};(nodes||[]).forEach(function(n){byName[n.name]=n;});
    var order=derivedTopoOrder(nodes,byName);
    var computed={};
    function needSeries(key,owner){
      var vals=series?series[key]:null;
      if(vals===undefined||vals===null)throw new Error('派生指标「'+owner+'」缺少依赖序列 '+key+'（调用方必须先用 requiredAggregations 提供该聚合序列）');
      return vals;
    }
    function resolveRef(ref,owner){
      if(Object.prototype.hasOwnProperty.call(computed,ref))return computed[ref];
      if(series&&Object.prototype.hasOwnProperty.call(series,ref))return series[ref];
      var fb=derivedAggKey('sum',ref);
      if(series&&Object.prototype.hasOwnProperty.call(series,fb))return series[fb];
      // sum 键缺席时按「任意聚合的同一列」回退（标量 default_agg 调用方），
      // 与 PY resolve_ref 同序（排序后首个）保持 parity。
      var best=null;
      for(var k in series){
        if(Object.prototype.hasOwnProperty.call(series,k)&&k.indexOf('agg:')===0){
          var ci=k.indexOf(':',4);
          if(ci>=0&&k.slice(ci+1)===ref&&(best===null||k<best))best=k;
        }
      }
      if(best!==null)return series[best];
      throw new Error('派生指标「'+owner+'」引用了未知序列 '+ref+'（既不是已计算的派生指标，也不是调用方提供的指标/聚合序列）');
    }
    function evalExpr(node,owner,index){
      if(!node||typeof node!=='object')return null;
      if(node.const!==undefined)return derivedFinite(node.const);
      if(node.ref!==undefined)return derivedFinite(resolveRef(node.ref,owner)[index]);
      if(node.col!==undefined)return derivedFinite(needSeries(derivedAggKey(node.agg||'sum',node.col),owner)[index]);
      var left=evalExpr(node.left,owner,index),right=evalExpr(node.right,owner,index);
      if(left===null||right===null)return null;
      var op=node.op;
      if(op==='add')return left+right;
      if(op==='sub')return left-right;
      if(op==='mul')return left*right;
      if(op==='div')return right===0?null:left/right;
      return null;
    }
    order.forEach(function(name){
      var node=byName[name];
      if(node.kind==='formula'){
        var first=null;
        for(var k in series){if(Object.prototype.hasOwnProperty.call(series,k)){first=series[k];break;}}
        var n0=first?first.length:(labels?labels.length:0);
        var vals=[];
        for(var i=0;i<n0;i+=1)vals.push(evalExpr(node.expr||{},name,i));
        computed[name]=vals;
        return;
      }
      // ---- filtered（筛选型派生指标）：值 = 条件聚合序列本身（调用方已按
      // derivedFmKey 聚好），这里只透传；空池 count/countd=0、其余 null 由聚合方保证。
      if(node.kind==='filtered'){
        computed[name]=needSeries(derivedFmKey(node),name).slice();
        return;
      }
      // ---- share (BI-H2 占比)：与合计行/colTotals 同口径。分母 = 当前聚合全部值
      // 之和（all）或同父级组内之和（parent，需 opts.parentOf[name]）。空值不进分
      // 子/分母；分母 0 / 无父级 → null。1 位小数（与 Python _round_half_up 逐位一致）。
      if(node.kind==='share'){
        var sinp=node.input||{};
        var ssrc=(sinp.agg&&sinp.col)?needSeries(derivedAggKey(sinp.agg,sinp.col),name):resolveRef(sinp.ref||'',name);
        var svals=ssrc.map(function(v){return derivedFinite(v);});
        if(node.scope==='parent'){
          var pm=parentOf?parentOf[name]:null;
          if(!pm)throw new Error('派生指标「'+name+'」按父级占比需要「标签 → 父级」映射（parentOf）；调用方必须从原始行数据构建（或使用编译期烘焙的 parentShare）');
          var lb=labels||[];
          if(lb.length!==svals.length)throw new Error('派生指标「'+name+'」的标签数与序列长度不一致（'+lb.length+' vs '+svals.length+'）');
          var parents=svals.map(function(_,i){var l=lb[i];return (l==null)?null:pm[String(l)];});
          var sums={};
          parents.forEach(function(p,i){if(p!=null&&svals[i]!=null)sums[p]=(sums[p]||0)+svals[i];});
          computed[name]=parents.map(function(p,i){
            return (p!=null&&svals[i]!=null&&sums[p])?derivedRoundHalfUp(svals[i]/sums[p]*100,1):null;
          });
        }else{
          var total=0;
          svals.forEach(function(v){if(v!=null)total+=v;});
          computed[name]=svals.map(function(v){return (v!=null&&total)?derivedRoundHalfUp(v/total*100,1):null;});
        }
        return;
      }
      var inp=node.input||{};
      var fn=node.fn;
      // rank 族（WP-6）没有时间语义：partition = 当前聚合维度组合，不走
      // timeField/bucket 闸门（任何图表维度都可排名）。src 在闸门前解析，其余窗口
      // 算子的报错顺序与 Python 侧完全一致（闸门优先于缺序列）。
      if(DERIVED_RANK_FNS.indexOf(fn)>=0){
        var rsrc=(inp.agg&&inp.col)?needSeries(derivedAggKey(inp.agg,inp.col),name):resolveRef(inp.ref||'',name);
        computed[name]=derivedRankSeries(rsrc,fn);
        return;
      }
      var tfield=node.timeField,grain=node.grain;
      if(dimension&&tfield!==dimension)throw new Error('派生指标「'+name+'」依赖时间列 '+tfield+'，但图表维度是 '+dimension+'；请把该维度换成它（或改用其它派生指标）');
      if(bucket&&grain!==bucket)throw new Error('派生指标「'+name+'」按 '+grain+' 计算，但图表按 '+bucket+' 分桶；分桶必须与指标声明一致（否则比的是错周期）');
      if(!bucket)throw new Error('派生指标「'+name+'」需要图表声明时间分桶（'+grain+'）才能计算；请为时间维度设置分桶');
      var src=(inp.agg&&inp.col)?needSeries(derivedAggKey(inp.agg,inp.col),name):resolveRef(inp.ref||'',name);
      var lb=labels||[];
      if(lb.length!==src.length)throw new Error('派生指标「'+name+'」的标签数与序列长度不一致（'+lb.length+' vs '+src.length+'）');
      var idxOrder=src.map(function(_,i){return i;}).sort(function(a,b){return derivedTimeKey(lb[a])<derivedTimeKey(lb[b])?-1:(derivedTimeKey(lb[a])>derivedTimeKey(lb[b])?1:0);});
      // 窗口游标按分区独立推进（partitionOf = 每行第二个维度的值）：否则交叉图里
      // 「2025-02 × A」的环比会拿「2025-01 × B」当上期（跨系列错值，与 Python
      // derived_eval.evaluate 的 partition_of 同一批修复）。
      var partsSeq={};
      idxOrder.forEach(function(i){
        var key=(partitionOf&&(partitionOf[i]!==null&&partitionOf[i]!==undefined))?String(partitionOf[i]):'';
        (partsSeq[key]=partsSeq[key]||[]).push(i);
      });
      var out=src.map(function(){return null;});
      if(fn==='prev'){
        // output defaults to rate for prev (mirrors derived_model.normalize_measures,
        // so a hand-written node behaves like a normalised one); 'value' = the
        // previous period's value itself (parity with scripts/derived_eval.evaluate).
        var offset=Number(node.offset||1),output=node.output||'rate';
        Object.keys(partsSeq).forEach(function(pk){
          var seq=partsSeq[pk];
          seq.forEach(function(i,pos){
            var j=pos-offset;
            var cur=derivedFinite(src[i]),prev=j>=0?derivedFinite(src[seq[j]]):null;
            if(output==='value')out[i]=prev;
            else if(cur===null||prev===null||prev===0)out[i]=null;
            else out[i]=(output==='rate')?derivedRoundHalfUp((cur-prev)/prev*100,1):cur-prev;
          });
        });
      }else{
        // cum / ytd（WP-6：ytd = cum 按年重置，闸门与 cum 同一条）
        var isYtd=(fn==='ytd');
        Object.keys(partsSeq).forEach(function(pk){
          var seq=partsSeq[pk],running=null,curYear=null;
          seq.forEach(function(i){
            if(isYtd){
              var y=derivedYearOf(lb[i]);
              if(running!==null&&y!==null&&y!==curYear)running=null;   // 跨年首期：重新起算
              if(y!==null)curYear=y;
            }
            var cur=derivedFinite(src[i]);
            if(cur!==null)running=(running===null?cur:running+cur);
            out[i]=running;
          });
        });
      }
      computed[name]=out;
    });
    return computed;
  }
  function derivedTopoOrder(nodes,byName){
    var order=[],state={};
    function visit(name,path){
      if(state[name]===1)return;
      if(state[name]===0)throw new Error('派生指标存在循环引用：'+path.concat([name]).join(' → '));
      var node=byName[name];
      if(!node)return;
      state[name]=0;
      var refs=(node.kind==='formula')?derivedExprRefs(node.expr||{}):
        ((node.input&&node.input.ref&&byName[node.input.ref])?[node.input.ref]:[]);
      refs.forEach(function(r){if(byName[r])visit(r,path.concat([name]));});
      state[name]=1;order.push(name);
    }
    (nodes||[]).forEach(function(n){visit(n.name,[]);});
    return order;
  }
  function derivedExprRefs(node,out){
    out=out||[];
    if(!node||typeof node!=='object')return out;
    if(node.op!==undefined){derivedExprRefs(node.left,out);derivedExprRefs(node.right,out);}
    else if(node.ref!==undefined&&out.indexOf(node.ref)<0)out.push(node.ref);
    return out;
  }
  // #region COND_FORMAT —— 图表级条件格式（按值着色，BI-H7）
  // 阈值模型与表格 pbSlotColor 逐字一致（同一套语义色，editor-runtime 的
  // TABLE_KIND_SLOT_COLORS 是其镜像；parity 由 tests/test_cond_format.cjs 锁）。
  // 阈值是**绝对数值**（与图上绘制值同单位——已套用单位缩放/小数，用户看到什么就填什么）。
  var COND_SLOT_COLORS={goal:{high:'#1a7f4b',mid:'#c47f17',low:'#c62f2f'},warn:{high:'#c62f2f',mid:'#c47f17',low:'#1a7f4b'}};
  var COND_NEUTRAL='#b9b5ae';
  // 取色纯函数：value 自高向低匹配第一个达到的阈值槽位；低于低阈值 = 中性灰。
  // 与 editor-runtime.pbSlotColor(mode,thresholds,pct) 同一比较逻辑（那边输入是
  // 完成率百分比，这边是绘制值——阈值单位跟输入走，比较器相同）。
  function condSlotColor(mode,thresholds,value){
    var colors=COND_SLOT_COLORS[mode]||COND_SLOT_COLORS.goal;
    var v=Number(value);
    if(!Number.isFinite(v))return COND_NEUTRAL;
    var slots=[['high',thresholds&&thresholds.high],['mid',thresholds&&thresholds.mid],['low',thresholds&&thresholds.low]];
    for(var i=0;i<slots.length;i++){
      var p=Number(slots[i][1]);
      if(Number.isFinite(p)&&v>=p)return colors[slots[i][0]];
    }
    return COND_NEUTRAL;
  }
  // chart.editor.condFormat = { [measure]: {mode, thresholds, enabled?} }
  // 存在即启用；`enabled:false` = 显式停用但保留配置（验收 2026-09-14 弹窗总开关），
  // 彻底删除条目仍是「停用」的第二种方式。三个阈值必须都是有限数才生效
  // （配置不完整绝不静默半应用）。
  function condFormatPlanOf(chart){
    var cf=chart&&chart.editor&&chart.editor.condFormat;
    if(!cf||typeof cf!=='object')return null;
    var out={};
    Object.keys(cf).forEach(function(m){
      var c=cf[m];if(!c)return;
      if(c.enabled===false)return;   // 总开关关闭：配置保留，不上色
      var th=c.thresholds||{};
      // ⚠ Number(null)===0：未填的槽位（null/undefined/空串）必须显式排除，
      // 否则「只填两个阈值」会静默把缺失档当成 0 应用（配置不完整绝不半应用）。
      function num(v){if(v==null||v==='')return NaN;var n=Number(v);return Number.isFinite(n)?n:NaN;}
      var hi=num(th.high),mi=num(th.mid),lo=num(th.low);
      if(!Number.isFinite(hi)||!Number.isFinite(mi)||!Number.isFinite(lo))return;
      out[m]={mode:(c.mode==='warn')?'warn':'goal',thresholds:{high:hi,mid:mi,low:lo}};
    });
    return Object.keys(out).length?out:null;
  }
  function condPieces(p){
    var t=p.thresholds;
    var colors=COND_SLOT_COLORS[p.mode]||COND_SLOT_COLORS.goal;
    // 区间两两不相交（gte/lte 成对），与 condSlotColor 的「自高向低第一个达到」一致。
    return [
      {gte:t.high,color:colors.high},
      {gte:t.mid,lte:t.high,color:colors.mid},
      {gte:t.low,lte:t.mid,color:colors.low},
      {lt:t.low,color:COND_NEUTRAL}
    ];
  }
  // option 后处理（与 patchSemantics 同点生效 → 静态首屏/重聚合/导出一致）：
  //   bar/stackedBar → 系列级 itemStyle.color 回调（params.value 即该段数值；
  //     项级只覆 opacity）；
  //   line/area → visualMap piecewise（refs/echarts PiecewiseModel.ts:37 支持
  //     gte/lte/lt pieces），seriesIndex 钉住本系列，多指标多 visualMap 共存。
  function applyCondFormat(option,chart){
    var plan=condFormatPlanOf(chart);
    if(!plan||!option||!Array.isArray(option.series))return option;
    var vms=option.visualMap?(Array.isArray(option.visualMap)?option.visualMap.slice():[option.visualMap]):null;
    option.series.forEach(function(ser,si){
      if(!ser||!ser.name)return;
      var p=plan[String(ser.name)];
      if(!p)return;
      if(ser.type==='line'){
        if(!vms)vms=[];
        vms.push({type:'piecewise',show:false,dimension:1,seriesIndex:[si],pieces:condPieces(p)});
      }else if(ser.type==='bar'){
        ser.itemStyle=Object.assign({},ser.itemStyle||{},{color:function(params){
          var v=params&&params.value;
          if(v&&typeof v==='object')v=v.value;
          return condSlotColor(p.mode,p.thresholds,v);
        }});
      }
    });
    if(vms)option.visualMap=vms;
    return option;
  }
  // #endregion COND_FORMAT_END
  // #endregion DERIVED_EVAL_END
  // Resolved nodes for THIS chart. ``editor.data.derivedMeasures = {name: node}``
  // is written by editor-runtime from ``dataset.derived.measures`` (the canonical
  // definition location) exactly like ``data.measureTypes`` / ``data.aggregations``
  // ride on the chart — so the adapter can evaluate without the dataset object and
  // the nodes survive a server reload.
  // 派生「百分数单位」判据（表格显示口径的唯一真源）：share 与 window.rate 输出在
  // 求值时已经 ×100（derivedEvaluate/derived_eval share 分支）——存的是百分数
  // （1.59% 存作 1.6），图表轴模板 {value}% 与 KPI delta 都直显。表格 fmtVal 的
  // percent 约定是「比率 ×100」，对这两类派生指标必须豁免，否则 1.6% 显示成
  // 160%（真机：净流入占比 430%/730%，2026-09-15）。公式节点存的是运算原值
  // （比率），不豁免；原始列的 percent 类型也按比率 ×100，不豁免。
  function derivedPercentUnits(data,measure){
    var n=data&&data.derivedMeasures?data.derivedMeasures[measure]:null;
    if(n)return n.kind==='share'||(n.kind==='window'&&n.output==='rate');
    var legacy=data&&data.derived?data.derived[measure]:null;
    return !!(legacy&&legacy.kind==='share');
  }
  function derivedPlanOf(data){
    var nodes=data&&data.derivedMeasures;
    if(!nodes)return null;
    var list=[];
    Object.keys(nodes).forEach(function(k){var n=nodes[k];if(n&&n.name)list.push(n);});
    if(!list.length)return null;
    var ms=data.measures||[];
    var used=list.filter(function(n){return ms.indexOf(n.name)>=0;});
    if(!used.length)return null;
    var defaults={};
    // defaultAgg 必须**逐指标**解析（与编译侧 _measure_default_agg 同口径）：
    // 逐指标 aggregations 覆盖 → measureTypes[m].aggregation 声明 → 图表级聚合。
    // 旧版把图表级聚合字符串直通 _pickMethod——首屏静态值按声明聚合（如 average）
    // 算好，首次交互重聚合按 sum 取依赖序列，同一格翻值。
    ms.forEach(function(m){
      var own=(data.aggregations&&typeof data.aggregations==='object'&&!Array.isArray(data.aggregations))?data.aggregations[m]:null;
      if(own==='default')own=null;
      var mdef=data.measureTypes?data.measureTypes[m]:null;
      var declared=(mdef&&typeof mdef==='object')?mdef.aggregation:null;
      defaults[m]=_pickMethod(own||declared||data.aggregation,m,data);
    });
    var evalNodes=derivedClosureNodes(used,list);
    return {nodes:list,used:used,evalNodes:evalNodes,names:list.reduce(function(acc,n){acc[n.name]=1;return acc;},{}),
            required:derivedRequiredAggregations(evalNodes,list.map(function(n){return n.name;}),defaults,list),
            series:{}};
  }
  // Evaluate the plan onto aggregated entries (in place) — aggregate → DERIVED →
  // sort/TOP-N/pagination, the documented order. Missing helper series surface as
  // an explicit error instead of a silently empty/placeholder series.
  // Build {label: parentLabel} from the chart's OWN rows (singlefile). Server-mode
  // datasets are schema-only (no rows) — the caller falls back to the compile-time
  // ``parentShare`` bake, else raises an actionable error (never silently 0%).
  function buildParentOfFromRows(data,dimension,parentField){
    if(!data||!Array.isArray(data.rows)||!dimension||!parentField)return null;
    var names=(data.columns||[]).map(function(c){return (c&&typeof c==='object')?(c.name||''):c;});
    var di=names.indexOf(dimension),pi=names.indexOf(parentField);
    if(di<0||pi<0)return null;
    var m={};
    data.rows.forEach(function(r){
      var k=r[di],key=(k==null)?'':String(k);
      if(!Object.prototype.hasOwnProperty.call(m,key)){var p=r[pi];m[key]=(p==null)?'':String(p);}
    });
    return m;
  }
  function applyDerivedNodes(entries,data,plan,dimension,partitionOfOverride){
    if(!plan||!plan.used||!plan.used.length||!entries||!entries.length)return entries;
    var ms=data.measures||[],series={},labels=entries.map(function(e){return e.label;});
    ms.forEach(function(m,i){
      if(plan.names[m])return;   // a derived slot holds a placeholder, never feed it back
      series[m]=entries.map(function(e){return e.values[i];});
    });
    Object.keys(plan.series||{}).forEach(function(k){series[k]=plan.series[k];});
    var db=(data.roles&&data.roles.dimensionBucket)||data.dimensionBucket||{};
    // share=parent（BI-H2）需要 label→父级 映射。优先级：编译期烘焙（server 模式
    // schema-only 唯一来源，仅当图表维度未变）→ 本地行重建（singlefile）→ 可行动报错。
    var parentOf=null;
    var needParent=plan.used.some(function(n){return n&&n.kind==='share'&&n.scope==='parent';});
    if(needParent){
      parentOf={};
      plan.used.forEach(function(pn){
        if(!(pn&&pn.kind==='share'&&pn.scope==='parent'))return;
        var bake=(data.parentShare&&data.parentShare.maps)?data.parentShare.maps[pn.name]:null;
        if(bake&&(!dimension||data.parentShare.dimension===dimension)){parentOf[pn.name]=bake;return;}
        var built=buildParentOfFromRows(data,dimension,pn.parentField);
        if(!built)throw new Error('派生指标「'+pn.name+'」按父级占比需要原始行数据（当前数据源只有 schema）。请改用「占总计」口径，或保持构建时的图表维度');
        parentOf[pn.name]=built;
      });
    }
    var computed=derivedEvaluate(plan.evalNodes||plan.used,series,labels,{dimension:dimension,bucket:db.bucket||null,parentOf:parentOf,
      // 交叉图（dim × breakdown）：窗口游标按第二个维度分区（compile 侧 partition_of 同批）。
      // 多维行表（aggregateTable）显式传前 n-1 维组合键作分区（覆盖参数优先）。
      partitionOf:partitionOfOverride||((data.roles&&data.roles.breakdown)?entries.map(function(e){return (e.breakdown==null)?'':String(e.breakdown);}):null)});
    plan.used.forEach(function(n){
      var idx=ms.indexOf(n.name);
      if(idx<0)return;
      entries.forEach(function(e,i){e.values[idx]=computed[n.name][i];});
    });
    return entries;
  }
  // Helper series for the plan, aggregated one at a time through the SAME
  // aggregate implementation (so the grouping rules cannot fork). The recursive
  // call passes a data object WITHOUT ``derivedMeasures`` — that is what makes it
  // a plain base aggregation.
  async function derivedHelperSeries(data,dimension,plan){
    if(!plan||!plan.required.length)return plan;
    var sub={datasetId:data.datasetId,columns:data.columns,rows:data.rows,fieldRoles:data.fieldRoles,
             measureTypes:data.measureTypes,filter:data.filter,roles:data.roles,
             dimensionBucket:data.dimensionBucket};
    for(var i=0;i<plan.required.length;i+=1){
      var dep=plan.required[i];
      // 筛选型依赖不在这里补拉：重发一次聚合会让组集缩小（无命中的组直接消失），
      // 序列与基础 rows 错位——它由本地主聚合循环（filteredDeps）或服务端条件聚合
      // 承担，这里只认普通 {agg,col} 依赖。
      if(dep.filters)continue;
      var rows=await aggregate(sub,dimension,[dep.col],dep.agg);
      plan.series[derivedDepKey(dep)]=rows.map(function(e){return e.values[0];});
    }
    return plan;
  }
  // Sequence derived measures (P4 docs §4.3): mom/yoy/share. The old 'running'
  // kind was removed (rarely used + conflicted with the share/mom mental
  // model; runtime now rejects it so an old manifest tells the user to
  // regenerate). The new contract:
  //
  //   data.derived[token] = {
  //     kind: 'mom' | 'yoy' | 'share',
  //     base: '<baseMeasure>',
  //     label: '<displayName>',
  //     // mom / yoy only:
  //     granularity: 'week' | 'month' | 'quarter' | 'year',
  //     // share only:
  //     scope: 'all' | {kind:'parent', parentCol:'<hierarchyParent>'}
  //                  | {kind:'filter', dim:'<col>', value:'<v>' | ['<v>',...]},
  //   }
  //
  // Constraints (enforced at add-time via the picker + at runtime here):
  //   - mom/yoy require X axis to be a time dimension AND granularity ===
  //     X axis dimensionBucket.bucket (the strict-equality rule from
  //     docs §A.1 — chosen so "I'm reading by-month" is always explicit).
  //   - share.parent requires the dataset so we can walk the hierarchy to find
  //     each entry's parent dimension value; share.filter requires an existing
  //     column + ≥1 matching value.
  //
  // P4: the only derived flavour left here is SHARE. mom/yoy/running are
  // migrated to dataset-level窗口 nodes at load time and evaluated once in
  // ``derivedEvaluate``. `data`/`dataset` are threaded through every call site;
  // without the dataset the parent scope cannot be computed and we throw a clear
  // runtime error so the user gets feedback instead of NaNs.
  function applyDerivedMeasures(entries,measures,derived,data,dataset){
    if(!entries||!measures||!derived)return;
    var baseIndex={};
    measures.forEach(function(m,i){baseIndex[m]=i;});
    // share=all 的分母按**本次调用**缓存（token → total）。绝不能挂在 spec（即
    // manifest 里的 derived 节点）上：一是 syncManifest 会把 __totalCached 序列化
    // 进落盘 manifest，二是缓存永不失效——筛选/范围变化后占比仍除以旧总数。
    var shareTotals={};
    // Only the SHARE scope family is evaluated here. Every window flavour that
    // used to live here (mom/yoy/running, with the hard-coded week→52 offset)
    // is migrated into a dataset-level ``derived.measures`` node at load time
    // (editor-runtime migrateDerivedDeclarations → chartDerivedToNodes) and
    // evaluated by ``derivedEvaluate`` — ONE implementation of the window math
    // for the compile side, the runtime and the server. A spec that could not be
    // migrated losslessly (week-grain YoY, label≠token) is left empty with a
    // warning instead of being re-interpreted: a wrong period is worse than a gap.
    var parentDenominators=null,filterDenominators=null,needsParent=false,needsFilter=false;
    measures.forEach(function(token){
      var s=derived[token];if(!s||!s.base)return;
      if(s.kind!=='share')return;
      var sc=s.scope;
      if(sc&&typeof sc==='object'){
        if(sc.kind==='parent')needsParent=true;
        else if(sc.kind==='filter')needsFilter=true;
      }
    });
    if(needsParent)parentDenominators=buildShareParentDenominators(entries,measures,derived,baseIndex,data,dataset);
    if(needsFilter)filterDenominators=buildShareFilterDenominators(entries,measures,derived,baseIndex,data);
    measures.forEach(function(token,index){
      var spec=derived[token];
      if(!spec||!spec.base)return;
      var bi=baseIndex[spec.base];
      if(bi==null){entries.forEach(function(e){e.values[index]=null;});return;}
      if(spec.kind!=='share'){
        legacyUnsupportedDerived(token,spec);
        entries.forEach(function(e){e.values[index]=null;});
        return;
      }
      var sc=spec.scope;
      entries.forEach(function(e,i){
        // 空值不进分子/分母（parity: derived_eval.py share 分支——_finite 过滤后
        // 空值不进 total、分子为 None 结果 None）。旧 `number(null)=0` 会把空基值
        // 算成 0%，与 Python 编译侧分叉。
        var cur=derivedFinite(e.values[bi]);
        var denom=0;
        if(!sc||sc==='all'||(typeof sc==='string'&&sc==='all')){
          // All scope: total over every entry. Computed lazily once per token
          // (cached per call) to avoid O(N²) on multi-share.
          if(shareTotals[token]==null){shareTotals[token]=entries.reduce(function(s,x){var v=derivedFinite(x.values[bi]);return s+(v==null?0:v);},0);}
          denom=shareTotals[token];
        }else if(typeof sc==='object'&&sc.kind==='parent'){
          // Parent scope: each entry's denominator is the sum of base values
          // over the entry's parent group (parentDenominators.sums keyed by
          // parent value, parentOf maps entry label → parent label).
          if(parentDenominators&&parentDenominators.parentOf){
            var pLabel=parentDenominators.parentOf[String(e.label)];
            denom=pLabel==null?0:(parentDenominators.sums[pLabel+'|'+token]||0);
          }
        }else if(typeof sc==='object'&&sc.kind==='filter'){
          // Filter scope: denominator = sum of the base over the filtered subset;
          // entries OUTSIDE the filter render null (their "share of filter" is
          // 0 by construction, and an empty cell must stay empty, never 0.0).
          if(filterDenominators){
            var key=sc.dim+'|'+(Array.isArray(sc.value)?sc.value.join('||'):String(sc.value));
            var filtVal=Array.isArray(sc.value)?sc.value:[sc.value];
            if(filtVal.indexOf(String(e.label))>=0)denom=filterDenominators[key]||0;
          }
        }else{
          if(shareTotals[token]==null){shareTotals[token]=entries.reduce(function(s,x){var v=derivedFinite(x.values[bi]);return s+(v==null?0:v);},0);}
          denom=shareTotals[token];
        }
        e.values[index]=(cur==null||!denom)?null:roundNumber(cur/denom*100,1);
      });
    });
  }
  // A chart-scoped legacy spec we could not migrate (see chartDerivedToNodes).
  // Warn once and leave the series empty — never render a wrong period.
  // 告警去重记在模块级 Set：spec 是 manifest 存活对象，写 __warned 会被
  // syncManifest 序列化进落盘 manifest。
  var legacyWarnedTokens={};
  function legacyUnsupportedDerived(token,spec){
    if(legacyWarnedTokens[token])return;
    legacyWarnedTokens[token]=true;
    if(typeof console!=='undefined'&&console.warn){
      console.warn('applyDerivedMeasures: 旧序列派生 '+spec.kind+'（'+token+'）无法无损迁移为数据源派生指标，已置空；'
        +'请在「数据源配置 → 字段配置 → 派生指标」里重建为窗口指标（同环比/累计）。');
    }
  }
  // share=parent: build {entryLabel: parentLabel} map (or throw if dataset
  // is missing). For each (parent value) we then sum the base column to
  // get the denominator per parent bucket, so each entry's share = base /
  // parentGroupTotal.
  function buildShareParentDenominators(entries,measures,derived,baseIndex,data,dataset){
    if(!data||!data.dimension){return null;}
    if(!dataset||!Array.isArray(dataset.hierarchy)||!dataset.hierarchy.length){
      throw new Error('share=parent 需要数据集的 hierarchy（请检查数据集是否声明了父-子层级边）。');
    }
    // Find the parent dimension of data.dimension.
    var dim=data.dimension;
    var parentCol=null;
    for(var i=0;i<dataset.hierarchy.length;i+=1){
      var e=dataset.hierarchy[i];
      if(e&&e.child===dim){parentCol=e.parent;break;}
    }
    if(!parentCol){
      throw new Error('share=parent: 维度「'+dim+'」未在 hierarchy 中找到父级，请先在「数据配置」中建立层级。');
    }
    // Build the parent-of-map from dataset rows. We assume entries are
    // already aggregated per dim value, so we just need a single lookup per
    // entry.label.
    var parentOf={};
    var dcols=(dataset.columns||[]).map(function(c){return typeof c==='object'?(c.name||''):c;});
    var cIdx=dcols.indexOf(dim);var pIdx=dcols.indexOf(parentCol);
    if(cIdx<0||pIdx<0){
      throw new Error('share=parent: 维度「'+dim+'」或父级「'+parentCol+'」不在数据集列中。');
    }
    (dataset.rows||[]).forEach(function(r){
      parentOf[String(r[cIdx])]=String(r[pIdx]??'');
    });
    // Sum the base per parent group, PER SHARE TOKEN (each share has its own
    // base measure — the old single `measures[0]` fallback silently used the
    // wrong column whenever a chart had more than one share or an extra series).
    var sums={};
    measures.forEach(function(token){
      var spec=derived[token];
      if(!spec||spec.kind!=='share'||!spec.base)return;
      var sc=spec.scope;
      if(!(sc&&typeof sc==='object'&&sc.kind==='parent'))return;
      var bi=baseIndex[spec.base];
      if(bi==null)return;
      entries.forEach(function(e){
        var p=parentOf[String(e.label)];if(p==null)return;
        var key=p+'|'+token;
        sums[key]=(sums[key]||0)+number(e.values[bi]);
      });
    });
    return {parentOf:parentOf,sums:sums};
  }
  // share=filter: build {dim|value: total} map. We look at the spec's
  // dim+value and sum entries whose raw rows have that value. The chart
  // receives rows via data.rows (singlefile) or via data (server sends
  // entries pre-aggregated per dim — we use entries' labels for dim filter).
  function buildShareFilterDenominators(entries,measures,derived,baseIndex,data){
    // For each unique spec we materialise a single denominator.
    var specs={};  // dim -> {values:Set, total:number}
    measures.forEach(function(token){
      var s=derived[token];if(!s||s.kind!=='share')return;
      var sc=s.scope;
      if(!sc||typeof sc!=='object'||sc.kind!=='filter')return;
      var key=sc.dim+'|'+(Array.isArray(sc.value)?sc.value.join('||'):String(sc.value));
      if(specs[key])return;
      var vals=Array.isArray(sc.value)?sc.value:[sc.value];
      // Sum entries whose label equals the dim's value if dim === chart
      // dimension; otherwise we need data.rows. Since entries represent the
      // aggregated base per chart dimension, we can only filter-share when
      // sc.dim === chart dimension (the value is in entries[i].label). For
      // any other dim we'd need to query the dataset rows; we throw a clear
      // error so the picker can warn the user instead.
      if(!data||sc.dim!==data.dimension){
        throw new Error('share=filter: 维度「'+(sc.dim||'')+'」与图表维度「'+(data&&data.dimension||'')+'」不一致；当前实现仅支持按图表维度过滤的子集占比。');
      }
      var sum=0;
      entries.forEach(function(e){
        var label=String(e.label);
        if(vals.indexOf(label)>=0){sum+=number(e.values[baseIndex[s.base]]||0);}
      });
      specs[key]=sum;
    });
    return specs;
  }
  // Display name for a measure. A sequence-derived token (e.g.
  // "环比增长:Sell In") shows its friendly label + base (环比增长 · Sell In)
  // instead of the raw token, so the legend/tooltip read cleanly.
  // 字段别名（显示名）——只在这里生效。**取数永远用原名**，所以别名不可能让请求
  // 打到不存在的列上（那是「直接改字段本体」才会出的事故）。
  var COLUMN_ALIASES={};
  function setColumnAliases(map){COLUMN_ALIASES=map||{};}
  function aliasOf(data,col){
    if(col==null)return col;
    var k=data&&data.datasetId;
    var m=(k&&COLUMN_ALIASES[k])||null;
    if(!m){
      // 没有 datasetId（旧图表）时退化：任取一张含该列的别名表。
      var keys=Object.keys(COLUMN_ALIASES);
      for(var i=0;i<keys.length;i+=1){var mm=COLUMN_ALIASES[keys[i]];if(mm&&Object.prototype.hasOwnProperty.call(mm,col)){m=mm;break;}}
    }
    var v=m&&m[col];
    return (typeof v==='string'&&v)?v:col;
  }
  function measureDisplayName(data,measure){
    if(measure==='*')return '记录数';
    var spec=data&&data.derived&&data.derived[measure];
    if(spec&&spec.base)return (spec.label||measure)+' · '+aliasOf(data,spec.base);
    return aliasOf(data,measure);
  }
  function applyMeasureFormats(option,data,fallbackDecimals){
    var measures=data&&data.measures||[];
    if(!measures.length||!data.measureTypes)return option;
    var series=Array.isArray(option.series)?option.series:[];
    if(!series.length)return option;
    // Only rebuild formats for families we understand; otherwise leave the
    // authored option (including hand-written tooltips) completely untouched.
    var autoable=series.every(function(s){return s&&AUTO_FORMAT_SERIES[s.type];});
    if(!autoable)return option;
    var formats=measures.map(function(measure){return measureFormat(data,measure,fallbackDecimals);});
    if(!formats.some(function(format){return !!format;}))return option;
    // Value axes are mapped from each series' yAxisIndex/xAxisIndex so dual-axis
    // charts format each axis by the measure actually drawn on it.
    var axisFormats={};
    series.forEach(function(s,index){
      var format=formats[index];
      if(!format)return;
      var yi=s.yAxisIndex!=null?s.yAxisIndex:0;
      axisFormats['y'+yi]=format;
      var xi=s.xAxisIndex!=null?s.xAxisIndex:0;
      axisFormats['x'+xi]=format;
    });
    ['xAxis','yAxis'].forEach(function(key){
      var axes=Array.isArray(option[key])?option[key]:[option[key]];
      axes.forEach(function(axis,index){
        if(!axis||axis.type!=='value')return;
        var format=axisFormats[key[0]+index];
        if(!format)return;
        axis.axisLabel=Object.assign({},axis.axisLabel||{},{formatter:format.axis});
      });
    });
    // Tooltip: rebuild a template from per-series formats.
    var share=series.some(function(s){return s&&(s.type==='pie'||s.type==='funnel');});
    var trigger=(option.tooltip&&option.tooltip.trigger==='item')||share?'item':'axis';
    if(trigger==='item'){
      var primary=formats[0];
      if(!primary)return option;
      var body='{b}: '+primary.prefix+'{c}'+primary.suffix;
      if(share)body+=' ({d}%)';
      option.tooltip=Object.assign({},option.tooltip||{},{trigger:'item',formatter:body});
    }else{
      // Function formatter (not a string template): reads each series by index
      // from the axis-trigger params and formats it with its own measure format.
      // A string template like '{a1}: {c1}' leaks the raw placeholder whenever a
      // measure has no declared format or the series array is shorter than
      // expected (e.g. a category dragged into measures collapses to 0s) — a
      // function never exposes them.
      var fmt=function(params){
        var list=Array.isArray(params)?params:[params];
        var out=[list[0]&&(list[0].name!=null?list[0].name:'')];
        list.forEach(function(p,index){
          var format=formats[index]||{prefix:'',suffix:'',divisor:1};
          var label=(p.seriesName!=null&&p.seriesName!=='')?p.seriesName:(measures[index]||('指标 '+(index+1)));
          out.push(label+': '+format.prefix+(p.value!=null?p.value:'')+format.suffix);
          // 目标完成率: when the measure has a per-measure goal reference,
          // show 实际/目标 = n%. Reconstruct raw = displayed * divisor (the goal
          // is stored in raw units) so the rate matches authored numbers.
          var ref=(data.references&&data.references[measures[index]])||null;
          if(ref&&ref.type==='goal'&&ref.goal!=null&&Number.isFinite(Number(ref.goal))){
            var g=Number(ref.goal);
            var raw=(p.value!=null)?Number(p.value)*(Number(format.divisor)||1):NaN;
            if(Number.isFinite(raw)&&g!==0){
              var rate=(raw/g)*100;
              out.push('完成率: '+rate.toFixed(1)+'%');
            }
          }
        });
        return out.join('<br/>');
      };
      option.tooltip=Object.assign({},option.tooltip||{},{trigger:'axis',formatter:fmt});
    }
    return option;
  }
  function axisObject(axis){return Array.isArray(axis)?axis[0]:axis;}
  function categoryAxis(option){
    var x=axisObject(option.xAxis),y=axisObject(option.yAxis);
    if(x&&x.type==='category'&&Array.isArray(x.data))return {axis:'x',values:x.data};
    if(y&&y.type==='category'&&Array.isArray(y.data))return {axis:'y',values:y.data};
    return null;
  }
  function seriesValue(item){return number(item&&typeof item==='object'&&'value' in item?item.value:item);}
  function inferEditorData(chart,resolvedData){
    if(resolvedData&&Array.isArray(resolvedData.columns)&&(Array.isArray(resolvedData.rows)||resolvedData.datasetId))return clone(resolvedData);
    var existing=chart.editor&&chart.editor.data;
    if(existing&&Array.isArray(existing.columns)&&(Array.isArray(existing.rows)||existing.datasetId))return clone(existing);
    var option=chart.option||{},axis=categoryAxis(option),series=Array.isArray(option.series)?option.series:[];
    if(!axis||!series.length)return null;
    var usable=series.filter(function(item){return Array.isArray(item.data)&&item.data.length===axis.values.length;});
    if(!usable.length)return null;
    var columns=['维度'].concat(usable.map(function(item,index){return item.name||('指标 '+(index+1));}));
    var rows=axis.values.map(function(label,rowIndex){return [label].concat(usable.map(function(item){return seriesValue(item.data[rowIndex]);}));});
    return {columns:columns,rows:rows,dimension:'维度',measures:columns.slice(1),aggregation:'sum',inferred:true};
  }
  // Q3-Issue3: per-measure aggregation. measures is an array of column names;
  // `method` (the aggregation spec) may be:
  //   - a single string (legacy): applied to every measure, e.g. 'sum'
  //   - an array of strings parallel to measures
  //   - an object keyed by measure name: { '可用库存': 'average', ... }
  // The object form is what the editor's per-measure 指标处理 picker writes
  // (data.aggregations) and is what showTable/optionFor pass through.
  // ---- date bucketing (product boundary: time-bucket aggregation only) ----
  function pad2(n){return String(n).padStart(2,'0');}
  // Shared temporal parser — BYTE-EQUIVALENT to scripts/date_parse.py (compile +
  // server side). Supported spellings: 2026-09-05, 2026/9/5, 2026.9.5,
  // 2026年9月5日, 20260905 (YYYYMMDD), 20260905? no: 202501 (YYYYMM), 2026-09 (month),
  // 2026 (year), and a full timestamp's date part. Returning {y,m,d,prec} keeps
  // grain available (a monthly column must not become 30 daily buckets).
  var DATE_PART_RE=/^(\d{4})\s*[-/.年]\s*(\d{1,2})\s*(?:[-/.月]\s*(\d{1,2})\s*[日号]?)?\s*[月日号]?\s*(?:[T ]\s*\S.*)?$/;
  // 值级周字段（w35 / 2026-W35 / 2026年35周 / 第35周）——date_parse._week_of 的逐字镜像。
  // 周必须带显式 w/W/周 标记（2026-35 与月份写法无法区分，不认）；1–53 宽容接受。
  var WEEK_RE_YEAR=/^(\d{4})\s*[-/]?\s*[Ww]\s*(\d{1,2})$/;
  var WEEK_RE_BARE=/^[Ww](\d{1,2})$/;
  var WEEK_RE_CN=/^(?:(\d{4})\s*年\s*)?第?\s*(\d{1,2})\s*周$/;
  function weekPartsOf(v){
    if(v===null||v===undefined||typeof v==='number')return null;
    var s=String(v).trim();if(!s)return null;
    // 每个正则的分组语义不同（裸周的 group(1) 是周号，其余 group(1)=年/group(2)=周）
    // ——逐字镜像 date_parse._week_of 的 has-year unpacker，取错分组会把 w35 判
    // 成「35 年第 NaN 周」静默返回 null（PY 侧同型 bug 先修过一次）。
    var res=[[WEEK_RE_YEAR,true],[WEEK_RE_BARE,false],[WEEK_RE_CN,true]];
    for(var i=0;i<res.length;i+=1){
      var m=s.match(res[i][0]);
      if(m){
        var y=res[i][1]?((m[1]!=null&&m[1]!=='')?+m[1]:0):0;
        var w2=res[i][1]?+m[2]:+m[1];
        return (w2>=1&&w2<=53)?[y,w2]:null;
      }
    }
    return null;
  }
  function dateParts(v){
    if(v===null||v===undefined||v==='')return null;
    // 值级周字段：周没有单一 (y,m,d) 日期，提前返回 week 部件（绕过下方日历
    // 探针——mo=35 会把 new Date 校验打失败）。derivedTimeKey 用它得
    // 'YYYY-Ww'→'YYYY-ww-00' 键（周标签列内日历序）；parseDateValue 对 week
    // 返回 null（周不是一个日期刻度）。
    var wp=weekPartsOf(v);
    if(wp)return {y:wp[0],mo:wp[1],d:0,prec:'week'};
    if(v instanceof Date)return isNaN(v)?null:{y:v.getFullYear(),mo:v.getMonth()+1,d:v.getDate(),prec:'day'};
    // Unix epoch seconds / milliseconds (a common CSV/JSON export) — only inside
    // the 2001..2100 window, so a 10-digit count is not silently read as a date.
    if(typeof v==='number'&&isFinite(v)&&v===Math.floor(v)){
      var EPOCH_MIN=978307200,EPOCH_MAX=4102444800,sec=null;
      if(v>=EPOCH_MIN&&v<=EPOCH_MAX)sec=v;
      else if(v>=EPOCH_MIN*1000&&v<=EPOCH_MAX*1000)sec=Math.floor(v/1000);
      // UTC getters (not local) so the label matches the Python parser and
      // SQLite's strftime(...,'unixepoch'), which are both UTC — otherwise the
      // same epoch renders as a different DAY in CST vs UTC.
      if(sec!==null){var ed=new Date(sec*1000);return {y:ed.getUTCFullYear(),mo:ed.getUTCMonth()+1,d:ed.getUTCDate(),prec:'day'};}
    }
    var s=String(v).trim();
    if(!s)return null;
    // "202501.0" (CSV/Excel float spelling): strip the decimal part ONLY when the
    // whole token is digits + '.0…' — str.rstrip('.0') would corrupt 2020 → '202'.
    var fz=s.match(/^(\d+)\.0+$/);
    if(fz)s=fz[1];
    var m;
    // 季度标签（本模块/date_parse 产出的 'YYYY-Qn'，也接受 2025Q1）：解析回来才能
    // 对**已分桶**的标签排序/判时间轴（静态 option 的维度值就是这种标签）。
    var q=s.match(/^(\d{4})\s*[-/]?\s*[Qq]\s*([1-4])$/);
    if(q){m={y:+q[1],mo:(+q[2]-1)*3+1,d:1,prec:'quarter'};}
    else if(/^\d{8}$/.test(s)){m={y:+s.slice(0,4),mo:+s.slice(4,6),d:+s.slice(6),prec:'day'};}
    else if(/^\d{6}$/.test(s)){m={y:+s.slice(0,4),mo:+s.slice(4),d:1,prec:'month'};}
    else if(/^\d{4}$/.test(s)){m={y:+s,mo:1,d:1,prec:'year'};}
    else{
      var g=s.match(DATE_PART_RE);
      if(!g)return null;
      m={y:+g[1],mo:+g[2],d:g[3]?+g[3]:1,prec:g[3]?'day':'month'};
    }
    if(!(m.mo>=1&&m.mo<=12)||!(m.d>=1&&m.d<=31))return null;
    var probe=new Date(m.y,m.mo-1,m.d);
    if(probe.getFullYear()!==m.y||probe.getMonth()!==m.mo-1||probe.getDate()!==m.d)return null;
    return m;
  }
  function parseDateValue(v){
    var p=dateParts(v);
    if(!p||p.prec==='week')return null;   // 周值不是单一日期刻度（bucketRange 的 week 分支靠它保持 null）
    return p?new Date(p.y,p.mo-1,p.d):null;
  }
  // Column-level grain inference (mirrors date_parse.infer_grain): day precision
  // dominates → 'day', else month, and a bare yyyy only with a year-hint name.
  function valueGrain(v){var p=dateParts(v);return p?p.prec:null;}
  // ISO week of the ISO year — returns BOTH the week number and the ISO year:
  // labelling the ISO week number with the calendar year folded 2025-01-01 and
  // 2025-12-29 (ISO 2026-W01) into the same bucket (mirror of date_parse.label).
  function isoWeek(d){
    var date=new Date(d.getFullYear(),d.getMonth(),d.getDate());
    var day=(date.getDay()+6)%7; date.setDate(date.getDate()-day+3);
    var firstThursday=new Date(date.getFullYear(),0,4);
    var fday=(firstThursday.getDay()+6)%7; firstThursday.setDate(firstThursday.getDate()-fday+3);
    return {y:date.getFullYear(),w:1+Math.round((date-firstThursday)/(7*86400000))};
  }
  function bucketLabel(v,bucket){
    // 值级周字段（w35 / 2026-W35）：归一标签直出（uppercase W + 两位补零），
    // 与 date_parse.label(v,'week') 逐字一致——服务端 ai_week GROUP BY 的就是它。
    if(bucket==='week'){
      var wp=weekPartsOf(v);
      if(wp)return wp[0]?wp[0]+'-W'+pad2(wp[1]):('W'+pad2(wp[1]));
    }
    var d=parseDateValue(v);
    if(!d)return String(v);
    // 季度 = 派生分桶，标签 'YYYY-Qn'；必须与 scripts/date_parse.label 和
    // server/aggregator._bucket_expr 逐字一致（静态/服务端/浏览器三端同标签）。
    if(bucket==='quarter')return d.getFullYear()+'-Q'+String(Math.floor(d.getMonth()/3)+1);
    if(bucket==='month')return d.getFullYear()+'-'+pad2(d.getMonth()+1);
    if(bucket==='day')return d.getFullYear()+'-'+pad2(d.getMonth()+1)+'-'+pad2(d.getDate());
    if(bucket==='week'){var iw=isoWeek(d);return iw.y+'-W'+pad2(iw.w);}
    if(bucket==='year')return String(d.getFullYear());
    return String(v);
  }
  // 分桶标签 → 覆盖的日期区间（含端点）—— date_parse.range_of 的镜像。
  // 时间下钻的逆运算：点某个桶的柱子 → 该桶的 daterange 谓词 + 下一级粒度。
  // 只支持 year/quarter/month/day（与 Python 一致；周不在下钻链上）。
  function daysInMonth(y,m){return new Date(y,m,0).getDate();}
  function bucketRange(label,bucket){
    if(label==null||String(label).trim()==='')return null;
    var text=String(label).trim();
    var g=bucket||valueGrain(text);
    if(!g)return null;
    var q=text.match(/^(\d{4})\s*-\s*[Qq]([1-4])$/);
    if(g==='quarter'&&q){
      var qy=+q[1],qn=+q[2],st=(qn-1)*3+1,en=st+2;
      return [qy+'-'+pad2(st)+'-01', qy+'-'+pad2(en)+'-'+pad2(daysInMonth(qy,en))];
    }
    var p=dateParts(text);
    if(!p)return null;
    var y=p.y,m=p.mo,d=p.d;
    if(g==='year')return [y+'-01-01', y+'-12-31'];
    if(g==='quarter'){var s2=Math.floor((m-1)/3)*3+1,e2=s2+2;return [y+'-'+pad2(s2)+'-01', y+'-'+pad2(e2)+'-'+pad2(daysInMonth(y,e2))];}
    if(g==='month')return [y+'-'+pad2(m)+'-01', y+'-'+pad2(m)+'-'+pad2(daysInMonth(y,m))];
    if(g==='day')return [y+'-'+pad2(m)+'-'+pad2(d), y+'-'+pad2(m)+'-'+pad2(d)];
    return null;
  }
  // 时间下钻链：年 → 季 → 月 → 日（周不参与——月与周不构成包含关系）。
  var TIME_DRILL_CHAIN=['year','quarter','month','day'];
  function timeDrillChild(grain){
    var i=TIME_DRILL_CHAIN.indexOf(String(grain||''));
    return (i>=0&&i<TIME_DRILL_CHAIN.length-1)?TIME_DRILL_CHAIN[i+1]:null;
  }
  function distinctDayLabels(data,col){
    var cols=(data&&data.columns||[]).map(function(c){return typeof c==='object'?c.name:c;});
    var ci=cols.indexOf(col);if(ci<0)return 0;
    var rows=(data&&data.rows)||[],seen={},n=0;
    for(var r=0;r<rows.length;r++){var l=bucketLabel(rows[r][ci],'day');if(l&&!seen[l]){seen[l]=1;n++;}}
    return n;
  }
  // Pick the RAW (full-precision) date column for time-bucketing. A dimension
  // like 订单月份 is a pre-bucketed label whose day-granularity collapses to
  // the 1st of each month; bucketing it by day jumps by month. Prefer a date
  // column that yields the most distinct day buckets (the true raw source,
  // e.g. 订单日期 = 728 days vs 订单月份 = 24).
  function rawDateSource(data,dim){
    if(!data)return dim;
    var cols=(data&&data.columns||[]).map(function(c){return typeof c==='object'?c.name:c;});
    var dates=cols.filter(function(c){return dimensionKind(data,c)==='time';});
    if(!dates.length)return dim;
    var best=dates[0],bestN=distinctDayLabels(data,dates[0]);
    dates.forEach(function(c){var n=distinctDayLabels(data,c);if(n>bestN){bestN=n;best=c;}});
    return best||dim;
  }
  // ---- per-measure aggregation (with time-bucket dimension support) ----
  // measures is an array of column names; `method` may be:
  //   - a single string (legacy): applied to every measure, e.g. 'sum'
  //   - an array of strings parallel to measures
  //   - an object keyed by measure name: { '可用库存': 'average', ... }
  // The object form is what the editor's per-measure 指标处理 picker writes
  // (data.aggregations) and is what showTable/optionFor pass through.
  // Cross-table aligned aggregation (references/multi-source-alignment.md §6.2):
  // measures span DIFFERENT datasets aligned on a shared dimensionKey. Aggregate
  // each measure over its own table (locally or via /aggregate), then outer-join
  // on the normalized label. Sort: first measure desc, label asc tiebreak —
  // identical to spec_to_report._cross_table_merge for WYSIWYG.
  function localCrossMap(ds, dimCol, measureCol, agg){
    var cols=(ds.columns||[]).map(function(c){return (c&&typeof c==='object')?(c.name||''):c;});
    var di=cols.indexOf(dimCol),mi=cols.indexOf(measureCol);
    var groups={};
    (ds.rows||[]).forEach(function(row){
      if(!matchFilter(row,cols,ds.filter||[]))return;
      var label=String(row[di]!=null?row[di]:'');
      (groups[label]=groups[label]||[]).push(row);
    });
    var out={};
    Object.keys(groups).forEach(function(label){
      // 逐分支镜像 spec_to_report._cross_table_merge（WYSIWYG：静态首屏 = 运行时重聚合）：
      // NULL/''/非数值格不进 nums（Number(null)=0 会把空格当 0 —— min 出假 0、平均被
      // 稀释，与 aggregate/groupSeries 同一条修复纪律）；count = 组内记录数（never a
      // null-count of the measure column，旧 nums.length 把含空格的列计少）；countd =
      // 非空原始值去重（旧版缺这个分支，静默落进 sum）。
      var grouped=groups[label],nums=[];
      grouped.forEach(function(row){var raw=row[mi];if(raw==null||raw==='')return;var n=Number(raw);if(isFinite(n))nums.push(n);});
      var v;
      if(agg==='countd'){
        var seen=Object.create(null);
        grouped.forEach(function(row){var raw=row[mi];if(raw==null)return;seen[String(raw)]=1;});
        v=Object.keys(seen).length;
      }
      else if(agg==='count')v=grouped.length;
      else if(!nums.length)v=0;
      else if(agg==='average')v=nums.reduce(function(a,b){return a+b;},0)/nums.length;
      else if(agg==='min')v=Math.min.apply(null,nums);
      else if(agg==='max')v=Math.max.apply(null,nums);
      else v=nums.reduce(function(a,b){return a+b;},0);
      out[label]=v;
    });
    return out;
  }
  function fetchAggregateMulti(subRequests){
    var serverBase=normalizeServerBase();
    return fetch(serverBase+'/aggregate-multi',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({requests:subRequests})
    }).then(function(resp){return assertOk(resp,'aggregate-multi');})
      .then(function(payload){
        return (payload.results||[]).map(function(res){
          var rows=res.rows||[];
          var map={};
          rows.forEach(function(row){map[String(row[0]!=null?row[0]:'')]=Number(row[1])||0;});
          return map;
        });
      });
  }
  async function crossTableAggregate(data){
    var ct=data.crossTable||{};
    var measures=ct.measures||[];
    var tables=ct.dimensionTables||[];
    var byDs={};tables.forEach(function(t){byDs[t.datasetId]=t;});
    var results=[];
    var remoteSubs=[];var remoteIdx=[];
    for(var i=0;i<measures.length;i++){
      var m=measures[i];
      var t=byDs[m.datasetId];
      var ds=data.crossDatasets&&data.crossDatasets[m.datasetId];
      if(ds&&Array.isArray(ds.rows)){
        results[i]=localCrossMap(ds,t.column,m.column,m.aggregation||'sum');
      }else{
        results[i]=null;
        remoteIdx.push(i);
        remoteSubs.push({datasetId:m.datasetId,groupBy:[t.column],measures:[{column:m.column,aggregation:m.aggregation||'sum'}],filter:(ds&&ds.filter)||[],limit:500});
      }
    }
    if(remoteSubs.length){
      var remoteMaps=await fetchAggregateMulti(remoteSubs);
      remoteIdx.forEach(function(ri,k){results[ri]=remoteMaps[k]||{};});
    }
    var labels=[];var seen={};
    results.forEach(function(map){Object.keys(map||{}).forEach(function(l){if(!seen[l]){seen[l]=1;labels.push(l);}});});
    var isTime=tables.some(function(t){return t.bucket;});
    // 时间对齐轴按**日历序**排（date_parse.sort_key 的镜像：可解析日期在最前、
    // 其余按修剪后的字符串）。旧实现直接字符串比较，非零填充写法（2026/9/5 vs
    // 2026/10/1）会排出字典序乱轴 —— 与编译侧 _cross_table_merge 同一批修复。
    function crossLabelCmp(a,b){
      var pa=dateParts(a),pb=dateParts(b);
      if(pa&&pb){var ka=pa.y+'-'+pad2(pa.mo)+'-'+pad2(pa.d),kb=pb.y+'-'+pad2(pb.mo)+'-'+pad2(pb.d);return ka<kb?-1:(ka>kb?1:0);}
      if(pa)return -1;
      if(pb)return 1;
      var sa=String(a).trim(),sb=String(b).trim();
      return sa<sb?-1:(sa>sb?1:0);
    }
    labels.sort(isTime?crossLabelCmp:function(a,b){var va=(results[0]||{})[a]||0,vb=(results[0]||{})[b]||0;return (vb-va)||(a<b?-1:(a>b?1:0));});
    return labels.map(function(l){return {label:l,breakdown:null,values:results.map(function(map){return map&&map[l]!=null?map[l]:0;})}});
  }
  async function aggregate(data,dimension,measures,method,sink,opts){
    // Cross-table: merge the per-dataset aggregates into one aligned result.
    if(data&&data.crossTable&&data.crossDatasets){return await crossTableAggregate(data);}
    // Dataset-driven sourcing (mixed-mode design §4): a chart aggregates on the
    // local server when its dataset is schema-only (no rows in this report) and
    // fetches there, otherwise locally. Pure singlefile (all rows) and pure
    // server (all schema-only) reports degrade to today's behaviour — no global
    // mode coupling.
    var remote=!!(data&&(data.remote||(data.datasetId&&data.rowCount&&!Array.isArray(data.rows))));
    if(remote&&data&&data.datasetId){
      return await fetchAggregate(data, dimension, measures, method, sink, opts);
    }
    // A schema-only dataset that cannot be fetched is a hard contract break —
    // never fall back to local ``data.rows`` (absent). Return an empty result so
    // the UI can surface a distinct empty/error state instead of garbage.
    if(!remote&&data&&!Array.isArray(data.rows)){
      return [];
    }
    // Normalize schema columns (server-mode [{name,type}]) to plain string
    // names so the local fallback path works on both shapes.
    var rawColumns=data.columns||[];
    var columns=(rawColumns.length && typeof rawColumns[0]==='object' && rawColumns[0]!==null)
      ? rawColumns.map(function(c){return c&&c.name?c.name:c;})
      : rawColumns;
    // Time-bucket dimension (product boundary): when the chart declares a
    // dimensionBucket { source: 快照日期, bucket: 'month' }, `dimension` is a
    // bucket label derived from `source`. The grouping key is the source
    // column; the label is the formatted bucket (e.g. 2025-01). The source
    // column must be a raw date column in the dataset, so re-aggregation
    // (sum/avg/max/min) is meaningful — no pre-aggregated summary data.
    var bucketSpec=(data.roles&&data.roles.dimensionBucket)||data.dimensionBucket||null;
    var ci=bucketSpec&&bucketSpec.source?columns.indexOf(bucketSpec.source):columns.indexOf(dimension);
    var bucket=bucketSpec&&bucketSpec.bucket?bucketSpec.bucket:null;
    var mi=measures.map(function(m){return m==='*'?-1:columns.indexOf(m);});
    var isCountCol=measures.map(function(m){return m==='*';});
    // Derived measures (同环比/占比/累计) are virtual columns — no raw column
    // exists. Mark them so the aggregation loop skips them (they're filled by
    // applyDerivedMeasures after the base series is aggregated).
    var derivedSpecs=data.derived||{};
    var plan=derivedPlanOf(data);          // P2: aggregate-level derived measures
    var isDerived=measures.map(function(m){
      return !!((plan&&plan.names[m]))||!!(derivedSpecs[m]&&derivedSpecs[m].base);});
    function derivedBaseIndex(j){var spec=derivedSpecs[measures[j]];return spec&&spec.base?columns.indexOf(spec.base):-1;}
    if(ci<0)return [];
    // For the grouping loop, a derived measure temporarily aggregates its base
    // column so the series is present; applyDerivedMeasures overwrites it. A
    // P2-model derived measure has no single base → -1 (its slot is evaluated
    // from the helper series afterwards), so -1 is allowed for derived slots.
    var aggMi=mi.map(function(miVal,j){return isDerived[j]?derivedBaseIndex(j):miVal;});
    if(aggMi.some(function(v,j){return v<0&&!isCountCol[j]&&!isDerived[j];}))return [];
    var groupLabel=function(row){return (bucketSpec&&bucketSpec.source)?bucketLabel(row[ci],bucket||'month'):String(row[ci]??'');};
    // Second dimension (crosstab): when data.roles.breakdown names a column,
    // group by (dimension × breakdown) and attach `breakdown` to each entry so
    // tables / charts can render a true 维度×维度 matrix (row=dim, col=breakdown,
    // cell=measure). Without it, stacking only ever splits by measure, never by
    // a second dimension.
    var roles=(data.roles)||{};
    var bd=roles.breakdown;
    var bi=bd&&bd!==dimension?columns.indexOf(bd):-1;
    var groupBreakdown=function(row){return bi>=0?String(row[bi]??''):null;};
    // Normalize `method` to a per-measure array of method-strings.
    var methods=null;
    if(Array.isArray(method)){
      methods=method.slice();
    }else if(method&&typeof method==='object'){
      // Object form: { measureName: method } — resolve per measure.
      methods=measures.map(function(m){var v=method[m];return v!=null&&v!=='default'?v:(data.aggregation||'sum');});
    }else{
      methods=measures.map(function(){return method;});
    }
    while(methods.length<measures.length)methods.push(data.aggregation||'sum');
    methods=methods.slice(0,measures.length);
    function methodFor(j){var m=methods[j];if(m==null||m==='default')m=data.aggregation||'sum';return m;}
    // Honour the report-wide filter for singlefile (server re-aggregates).
    var localRows=filteredRows(data);
    // Category-as-measure: a non-numeric column cannot sum/average/min/max
    // meaningfully (number('张三')→0, so the series collapses to 0s and the
    // tooltip leaks {'a1': '{c1}'}. When the resolved method is a numeric
    // aggregation but the measure column is non-numeric, fall back to count so
    // the series shows row-count — the honest reading of a category metric.
    var NUMERIC_AGGS={sum:1,average:1,min:1,max:1};
    function effectiveMethod(j){
      if(measures[j]==='*')return 'count';
      var m=methodFor(j);
      // Channel semantics (Flint-inspired): a category/ordinal column placed in
      // a measure slot degrades to a row-count rather than sum-silently
      // collapsing to 0. Uses the field semantic layer, not ad-hoc probing.
      if(m!=='count'&&m!=='none'&&NUMERIC_AGGS[m]){
        var field=fieldSemantic(data,measures[j]);
        var act=channelAction(field,'measure');
        if(act.action!=='allow')return act.action==='count'?'count':m;
      }
      return m;
    }
    // Perf: fieldSemantic() scans all rows (to infer numeric/dimension type),
    // so it is O(rows) per call. effectiveMethod(j) depends only on the measure
    // index and data — not on the current row — so recomputing it inside the
    // per-row loop turns O(rows*measures) fieldSemantic calls into an O(n^2)
    // blow-up (e.g. 5000 rows * 2 measures = 10k scans of 5000 rows). Hoist the
    // per-measure effective method out of the loops so it is computed once per
    // measure (O(measures)) instead of once per row * measure.
    var effMethods=measures.map(function(_,j){return effectiveMethod(j);});
    // 「原值(none)」捷径只能在没有派生计划时走：plan 存在时派生槽 aggMi[j]=-1，
    // 这里 number(row[-1])=0 会把整列派生指标静默清零——必须落到下方
    // 聚合+derivedHelperSeries/applyDerivedNodes 求值路径。
    if(methods.every(function(m){return m==='none';})&&!plan)return localRows.map(function(row){return {label:groupLabel(row),breakdown:groupBreakdown(row),values:aggMi.map(function(i){return number(row[i]);})};}).filter(function(item){return item.label.trim()!=='';});
    var map=new Map();
    // 筛选型派生指标的依赖（dep.filters）：在**同一个**分组循环里组内筛选累加
    // ——组宇宙必须与基础序列一致（组不存在于报告筛选后的行集，依赖序列却也
    // 不能缺它的格），绝不能像普通依赖那样重发一次聚合（组集会缩）。
    var filteredDeps=(plan&&plan.required||[]).filter(function(dep){return dep.filters;});
    function fmAccum(row,item){
      for(var fi=0;fi<filteredDeps.length;fi+=1){
        var dep=filteredDeps[fi];
        if(!matchFilter(row,columns,dep.filters))continue;
        var st=item.__fm||(item.__fm={});
        var a=st[fi]||(st[fi]={s:0,nn:0,mn:Infinity,mx:-Infinity,set:null,n:0});
        if(dep.agg==='count'||dep.col==='*'){a.n+=1;continue;}
        var cvi=columns.indexOf(dep.col);var raw=cvi>=0?row[cvi]:null;
        if(dep.agg==='countd'){if(raw!=null)(a.set=a.set||Object.create(null))[String(raw)]=1;continue;}
        var num=(raw==null||raw==='')?NaN:Number(raw);
        if(!isFinite(num))continue;
        a.s+=num;a.nn+=1;
        if(num<a.mn)a.mn=num;
        if(num>a.mx)a.mx=num;
      }
    }
    localRows.forEach(function(row){
      var label=groupLabel(row);if(label.trim()==='')return;
      var bdv=bi>=0?groupBreakdown(row):null;
      var key=bi>=0?(label+'\u0000'+(bdv||'')):label;
      var item=map.get(key);
      if(!item){item={label:label,breakdown:bi>=0?bdv:null,values:aggMi.map(function(){return 0;}),count:0};map.set(key,item);}
      item.count+=1;
      if(filteredDeps.length)fmAccum(row,item);
      aggMi.forEach(function(i,j){
        if(i<0&&isDerived[j])return;   // derived slot with no base column: evaluated later
        var mth=effMethods[j];
        // 去重计数：按原始值字符串收集非 NULL 值（'' 是合法去重值），finalize 时
        // 取 size（与服务端 COUNT(DISTINCT col) 同语义：NULL 不计数）。
        if(mth==='countd'){
          var set=(item.__sets||(item.__sets={}))[j]||(item.__sets[j]=new Set());
          var raw=row[i];
          // 与服务端 COUNT(DISTINCT col) 同语义：只排除 NULL；'' 是一个合法的
          // 去重值（排除它会让 singlefile 与 server 报告数字分裂）。
          if(raw!=null)set.add(String(raw));
          return;
        }
        if(mth==='average'||mth==='min'||mth==='max'){
          // 与编译侧（None/'' 先滤）和服务端（AVG/MIN 跳 NULL）同口径：
          // NULL/空串/非数值格**不参与**。Number(null)=0 会把空格当 0——
          // min 出假 0、平均被稀释，静态首屏与重聚合数字分裂（2026-09-17 全量审视批）。
          var rv=row[i];
          var num=(rv==null||rv==='')?NaN:Number(rv);
          if(!isFinite(num))return;
          var nn=(item.__nn||(item.__nn={}))[j]||0;
          item.__nn[j]=nn+1;
          if(mth==='min')item.values[j]=nn===0?num:Math.min(item.values[j],num);
          else if(mth==='max')item.values[j]=nn===0?num:Math.max(item.values[j],num);
          else item.values[j]+=num;
          return;
        }
        var value=mth==='count'?1:number(row[i]);
        item.values[j]+=value;
      });
    });
    var entries=Array.from(map.values()).map(function(item){
      aggMi.forEach(function(_,j){
        var mth=effMethods[j];
        if(mth==='average')item.values[j]=(item.__nn&&item.__nn[j])?item.values[j]/item.__nn[j]:null;
        else if(mth==='countd')item.values[j]=item.__sets?item.__sets[j].size:0;
      });
      return item;
    });
    if(plan){
      await derivedHelperSeries(data,dimension,plan);
      // 筛选型依赖 finalize（与 groupSeries/localAggregateTable 同一空池语义）。
      filteredDeps.forEach(function(dep,fi){
        plan.series[derivedDepKey(dep)]=entries.map(function(e){
          var a=e.__fm&&e.__fm[fi];
          if(!a)return (dep.agg==='count'||dep.agg==='countd'||dep.col==='*')?0:null;
          if(dep.agg==='count'||dep.col==='*')return a.n;
          if(dep.agg==='countd')return a.set?Object.keys(a.set).length:0;
          if(dep.agg==='average')return a.nn?a.s/a.nn:null;
          if(dep.agg==='min')return a.nn?a.mn:null;
          if(dep.agg==='max')return a.nn?a.mx:null;
          return a.s;
        });
      });
      applyDerivedNodes(entries,data,plan,dimension);
    }
    return entries;
  }
  // Names declared in dataset.derived.measures (virtual aggregate measures).
  // Mirrors editor-runtime normalizeDatasetDerived / Python derived_model: the
  // canonical shape is ``{columns, measures}`` but an early canvas revision
  // wrote a FLAT ARRAY, which must not resurrect as a huge fake measure list.
  function derivedMeasureNames(data){
    var der=data&&data.derived;
    if(!der||typeof der!=='object'||Array.isArray(der))return [];
    var list=Array.isArray(der.measures)?der.measures:[];
    var out=[];
    list.forEach(function(node){var n=node&&node.name;if(n&&out.indexOf(n)<0)out.push(n);});
    return out;
  }
  function numericColumns(data){
    var columns=data.columns||[],rows=data.rows||[];
    // Normalise to names (schema objects carry {name}; singlefile are strings).
    var names=columns.map(function(c){return (c&&typeof c==='object')?(c.name||''):c;});
    var schemaObj=columns.length && typeof columns[0]==='object' && columns[0]!==null;
    var fr=data.fieldRoles||{};
    var mt=data.measureTypes||{};
    function roleOf(n){var r=fr[n];return r&&(r.role||null);}
    function numericCell(value){
      if(typeof value==='number')return Number.isFinite(value);
      if(value==null||value==='')return false;
      if(typeof value==='string'){
        var s=String(value).trim();
        if(s===''||s.charAt(0)==='+')return false;
        return Number.isFinite(Number(s));
      }
      return false;
    }
    var out=[];
    columns.forEach(function(c,index){
      var n=names[index];
      // 1) 用户显式设置的字段角色优先（与 dimensionKind 一致）：measure → 指标；
      //    category/time/ordinal → 永远不是指标。这就是为什么在「数据配置」里把
      //    一个字段改成「指标」后，图表编辑面板能立刻把它当作指标列。
      var r=roleOf(n);
      if(r==='measure'){out.push(n);return;}
      if(r){return;}
      // 2) 已声明的 measureTypes 条目 → 指标（在用户显式设角色之前）。
      if(mt&&mt[n]){out.push(n);return;}
      // 3) server-mode 显式 schema 类型。
      if(schemaObj&&c){if(c.type==='measure')out.push(n);return;}
      // 4) 有行数据时按值探测；无行(schema-only/server)时无法判断 → 靠 fieldRoles/measureTypes。
      if(!Array.isArray(rows)||!rows.length)return;
      var values=rows.map(function(row){return row[index];}).filter(function(value){return value!==''&&value!=null;});
      if(values.length>0&&values.every(numericCell))out.push(n);
    });
    // P3: dataset-level derived MEASURES are virtual columns (absent from
    // ``columns``). They are aggregates by construction — 占比/排序/堆叠合法性/
    // 合计 等所有「是不是指标」的判定都走 numericColumns，不在这里补上就会
    // 被当成维度。
    derivedMeasureNames(data).forEach(function(name){if(out.indexOf(name)<0)out.push(name);});
    return out;
  }
  function dimensionColumns(data){
    var columns=data.columns||[];
    var names=columns.map(function(c){return (c&&typeof c==='object')?(c.name||''):c;});
    var schemaObj=columns.length && typeof columns[0]==='object' && columns[0]!==null;
    var fr=data.fieldRoles||{};
    function roleOf(n){var r=fr[n];return r&&(r.role||null);}
    var numeric=numericColumns(data);
    var out=[];
    columns.forEach(function(c,index){
      var n=names[index];
      var r=roleOf(n);
      // 与 numericColumns 同步：显式角色优先，measure 永远不是维度；
      // id/ignore 是「不参与分析」的列，既不是指标也不是维度。
      if(r==='measure')return;
      if(r==='id'||r==='ignore')return;
      if(r){out.push(n);return;}
      if(schemaObj&&c){if(c.type==='dimension'||c.type==='category'||c.type==='time'||c.type==='id')out.push(n);return;}
      if(numeric.indexOf(n)<0)out.push(n);
    });
    return out;
  }
  // Hard per-series-type dimension/measure capacity rules (ECharts semantics).
  // The editor renders these as visible limits: the dimension/measure zones
  // show the required count, and dropping an over-limit field is blocked.
  // Rules derive from what each whitelisted series actually consumes.
  function shapeRules(viewType){
    var dim1={min:1,max:1};
    var dim2={min:1,max:2};
    switch(viewType){
      // bar/stackedBar cross a dimension × breakdown (真正有含义的「维度×维度」交叉表).
      case 'bar': case 'stackedBar':
        return {dims:dim2,meas:{min:1,max:2},note:'1-2 个维度 + 1-2 个指标'};
      // line/area are single-axis trends (time/ordinal). A second categorical
      // dimension would silently become a multi-line comparison rather than the
      // intended total trend — keep it to 1 categorical dimension (matches the
      // line.trend preset's dimensions:1).
      case 'line': case 'area':
        return {dims:dim1,meas:{min:1,max:2},note:'1 个维度（时间/有序）+ 1-2 个指标'};
      case 'pie': case 'donut': case 'funnel':
        return {dims:dim1,meas:{min:1,max:1},note:'1 个维度 + 1 个指标'};
      case 'scatter':
        return {dims:{min:0,max:0},meas:{min:2,max:3},note:'2 个指标（x/y），可选第 3 个作大小'};
      case 'table':
        // 批 A（行 3 层，docs/multidim-table-and-currency-design-2026-09-28.md §3.9）：
        // 行区容量 2→3，只放 table 视图（其他 viewtype 维持 dim2）。3 行维的形态路由
        // （嵌套平铺 / 3 层矩阵 / ND 泄压阀）在 editor-runtime showTableBody。
        return {dims:{min:1,max:3},meas:{min:1,max:100},note:'1-3 个维度 × 任意指标'};
      default:
        return {dims:{min:0,max:99},meas:{min:0,max:99},note:''};
    }
  }
  function availableTypes(chart,resolvedData){
    if(chart.editor&&chart.editor.editable===false)return [];
    var data=inferEditorData(chart,resolvedData);
    if(!data)return [];
    // 图表类型是第一设置项（2026-09 用户决策）：任何字段组合下全部类型都可切换，
    // 置灰与「推荐」徽章已全部退场——返回全量白名单，逐项 available/disabled 仅作
    // 形状兼容（永远 全可用），标题提示由 shapeRules 的 note 承担。
    return CATALOG.map(function(item){
      var c=clone(item);c.available=true;c.disabled=false;return c;
    });
  }
  function clearCoordinates(option){['xAxis','yAxis','visualMap'].forEach(function(key){delete option[key];});}
  function entriesFor(data){return aggregate(data,data.dimension,data.measures||[],data.aggregation||'sum');}
  // A matrix dataset (roles.breakdown, used by bar.stacked) carries a second
  // dimension in roles.breakdown / roles.source+target. Rebuilding it as a
  // single-dimension chart from the raw matrix cells without aggregating is
  // meaningless (it would plot every cell as a category), so we coerce a
  // non-aggregating method to an averaging method for that transition.
  function isMatrix(data){return !!(data&&data.roles&&(data.roles.breakdown||data.roles.source||data.roles.target));}
  function matrixAgg(data){
    var agg=(data&&data.aggregation)||'sum';
    if(!isMatrix(data))return agg;
    return agg==='none'?'average':agg;
  }
  // Serialise a category axis: cap the number of visible categories so a leap
  // to a high-cardinality dimension (a date range, an id, a rank) does not
  // produce an unreadable wall of labels. Top-N by the first (primary) measure.
  function capEntries(entries,limit,data){
    if(!entries||!entries.length||!limit||entries.length<=limit)return entries;
    var mi=0;
    var sorted=entries.slice().sort(function(a,b){return number(b.values[mi])-number(a.values[mi]);});
    sorted.length=limit;
    return sorted;
  }
  // Issue 15: user-specified sort of the aggregated categories. `sort` is
  // { field: dimensionOrMeasureName, dir: 'asc'|'desc' }. If `field` is the
  // dimension, sort by the category label; if it's a measure, sort by that
  // measure's value. Defaults: a temporal dimension sorts by label ascending
  // (name/date order); everything else sorts by the first measure descending.
  // 截断登记：**per-call sink**（不是模块级槽位）。renderAll 会同时发起多张卡的
  // optionFor，各自 await 服务端聚合；共享槽位会让先返回的那张读到别人的截断信息
  // （真机实测四张卡显示同一句、连时间序列都被标成截断）。sink 随调用链传递：
  // optionFor → aggregate → fetchAggregate / applyTopK。
  function noteTruncation(sink,shown,total){
    if(!sink||sink.trunc)return;
    // total=0 = 总数未知（老服务端）：仍然要告诉读者「还有更多」——
    // 把截断藏起来比不写总数危险得多。
    if(total===0){sink.trunc={shown:shown,total:null};return;}
    if(total>shown)sink.trunc={shown:shown,total:total};
  }
  function applyTopK(entries,data,type,sink){
    // TOP K (Issue 4): bar/stackedBar cap to the top-K by primary measure after
    // sorting. Server already LIMITs via data.topK; this local slice covers
    // singlefile (official sort + filter semantics). Charts with a temporal
    // dimension are never top-K'd (they are trends).
    if(!entries||!entries.length)return entries;
    var k=data&&data.topK;
    if(!k||!(type==='bar'||type==='stackedBar')||entries.length<=k)return entries;
    if(entries.otherMerged)return entries; // server 已合并「其他」（BI-H3），不再二次截断
    // BI-H3：topKOther = 把长尾合并成一行「其他（N 项）」排最后（Power BI 语义；
    // Flint filter-overflow.ts 只截断+告警，不合并——合并是本报告的显式开关）。
    // 验收修订（2026-09-13）：按**指标逐列**判定可加性 —— 聚合 ∈ {sum,count} 且
    // 非比率型（percent/率/占比）才算可加；可加列求和进「其他」行，不可加列保持
    // null（空缺比假数好）。多指标只要有一列可加就能合并（旧 gate 是
    // anyNonAdditive 全有或全无，而且它只看类型不看聚合方式，average 也能混进来）。
    var msAll=(data&&data.measures)||[];
    var addMask=msAll.map(function(m){
      var agg=((data&&data.aggregations)&&data.aggregations[m])||(data&&data.aggregation)||'sum';
      // derivedPercentUnits（share / window-rate 的值已 ×100）与编译侧
      // _col_non_additive 同门：不靠用户命名带不带「占比」，百分数单位一律
      // 不可加——尾行求和占比是假数（7.3%+6.1% ≠ 13.4% 的口径）。
      return (agg==='sum'||agg==='count')&&!nonAdditiveMeasure(data,m)&&!derivedPercentUnits(data,m);
    });
    var mergeOther=!!(data&&data.topKOther)&&addMask.some(Boolean);
    // Cross-tab (维度×维度): entries carry a `breakdown` second dimension, so a
    // flat slice(0,k) would drop all-but-one breakdown series. Cap the PRIMARY
    // dimension instead: keep the top-K distinct labels (by summed primary
    // measure) and retain their breakdown rows, preserving the stacked series.
    var hasBd=entries.some(function(e){return e.breakdown!=null;});
    if(hasBd){
      var byLabel={};entries.forEach(function(e){var a=byLabel[e.label]||(byLabel[e.label]=0);a+=number(e.values[0]);});
      var topLabels=[...Object.keys(byLabel)].sort(function(a,b){return byLabel[b]-byLabel[a];}).slice(0,k);
      var kept={};topLabels.forEach(function(l){kept[l]=1;});
      noteTruncation(sink,topLabels.length,Object.keys(byLabel).length);
      var keptRows=entries.filter(function(e){return kept[e.label];});
      if(!mergeOther)return keptRows;
      // 带 breakdown：尾部按维度标签聚合成「其他」，每个 breakdown 值补一行。
      var omitted=Object.keys(byLabel).length-topLabels.length;
      var ms=data.measures||[];
      var otherLabel='其他（'+omitted+' 项）';
      var tailByBd={},bdOrder=[];
      entries.forEach(function(e){
        if(kept[e.label])return;
        var b=e.breakdown;
        if(!Object.prototype.hasOwnProperty.call(tailByBd,b)){tailByBd[b]={label:otherLabel,breakdown:b,values:ms.map(function(_,i){return addMask[i]?0:null;})};bdOrder.push(b);}
        ms.forEach(function(_,i){if(!addMask[i])return;tailByBd[b].values[i]+=number(e.values[i]);});
      });
      if(sink&&sink.trunc)sink.trunc.merged=true;
      return keptRows.concat(bdOrder.map(function(b){return tailByBd[b];}));
    }
    noteTruncation(sink,k,entries.length);
    if(!mergeOther)return entries.slice(0,k);
    var ms2=data.measures||[];
    var head=entries.slice(0,k),tail=entries.slice(k);
    var other={label:'其他（'+tail.length+' 项）',values:ms2.map(function(_,i){
      if(!addMask[i])return null;
      return tail.reduce(function(s,e){var v=e.values[i];return s+((v==null||v==='')?0:number(v));},0);
    })};
    if(sink&&sink.trunc)sink.trunc.merged=true;
    return head.concat([other]);
  }
  function sortEntries(entries,data,type,chart){
    if(!entries||!entries.length)return entries;
    if(['table','scatter'].indexOf(type)>=0)return entries;
    // BI-H5：维度自定义顺序（editor.data.sortOrder[dim]=[值…]）**最优先**——
    // 优先级：自定义顺序 > 显式 sort 设置 > 时间轴 chrono > 默认首指标降序。
    // 仅 category 维度（时间维度不给入口）；未列出的值按既有规则附后。
    var custom=((data&&data.sortOrder)||{})[data&&data.dimension];
    if(Array.isArray(custom)&&custom.length&&dimensionKind(data,data.dimension)==='category'){
      var rank={};custom.forEach(function(v,i){rank[String(v)]=i;});
      var inRank=[],outRank=[];
      entries.forEach(function(e){(rank[String(e.label)]!=null?inRank:outRank).push(e);});
      inRank.sort(function(a,b){return rank[String(a.label)]-rank[String(b.label)];});
      var out=inRank.concat(sortEntriesDefault(outRank,data,type,chart));
      // concat 产新数组：otherMerged/matrix 数组级标记透传（同 sortEntriesDefault.finalize）。
      if(entries.otherMerged)out.otherMerged=entries.otherMerged;
      if(entries.matrix)out.matrix=entries.matrix;
      return out;
    }
    return sortEntriesDefault(entries,data,type,chart);
  }
  function sortEntriesDefault(entries,data,type,chart){
    if(!entries||!entries.length)return entries;
    var measures=data.measures||[];
    var sort=(chart&&chart.editor&&chart.editor.sort)||{};
    var field=sort.field;
    var dir=sort.dir==='desc'?-1:1;
    var isTime=dimensionKind(data,data.dimension)==='time';
    // BI-H3：服务端/local 合并出的「其他（N 项）」行**恒排最后**（值是 N 项之和，
    // 按值排序会把它卷进中间——真机：其他（17 项）排在第 2 位）。同时重排会返回
    // 新数组，挂在原数组上的 otherMerged/matrix 标记必须透传，否则 applyTopK 会
    // 把尾部再合并一次，造出假「其他（1 项）」（把正常产品错标成其他）。
    function finalize(list){
      var _oth=[],_rest=[];
      list.forEach(function(e){(String(e&&e.label||'').indexOf('其他（')===0?_oth:_rest).push(e);});
      var out=_rest.concat(_oth);
      if(entries.otherMerged)out.otherMerged=entries.otherMerged;
      if(entries.matrix)out.matrix=entries.matrix;
      return out;
    }
    // Resolve field -> index into entry.values, or -1 for the label itself.
    var valueIdx=measures.indexOf(field);
    if(valueIdx<0&&field&&field!==data.dimension)valueIdx=measures.indexOf(field);
    // 时间标签的比较器 = 日历序（dateParts 部件序）：localeCompare 对非零填充
    // 原始标签（2026/9/5 vs 2026/10/1）会排出字典序乱轴（crossLabelCmp 同款修复）。
    function timeLabelCmp(a,b){
      var pa=dateParts(a.label),pb=dateParts(b.label);
      if(pa&&pb){var ka=pa.y+'-'+pad2(pa.mo)+'-'+pad2(pa.d),kb=pb.y+'-'+pad2(pb.mo)+'-'+pad2(pb.d);return ka<kb?-1:(ka>kb?1:0);}
      if(pa)return -1;
      if(pb)return 1;
      return String(a.label).localeCompare(String(b.label),'zh-CN');
    }
    if(valueIdx<0&&(!field||field===data.dimension)){
      // Sort by label; temporal keeps name/date order, otherwise default desc
      // only when no explicit field given.
      if(isTime||field===data.dimension)return finalize(entries.slice().sort(function(a,b){return timeLabelCmp(a,b)*dir;}));
    }
    if(valueIdx>=0){
      return finalize(entries.slice().sort(function(a,b){return (number(a.values[valueIdx])-number(b.values[valueIdx]))*dir;}));
    }
    if(isTime)return finalize(entries.slice().sort(timeLabelCmp));
    // Default: first measure descending.
    return finalize(entries.slice().sort(function(a,b){return number(b.values[0])-number(a.values[0]);}));
  }
  function categoryAxisMeta(entries){
    var n=entries.length;
    // 横轴值域**避让原则恒开**（验收 2026-09-14）：interval='auto' 由 ECharts 按
    // 标签实际宽度自动抽稀（放得下就全显、放不下自动隔跳），编译期静态 option 也
    // 是这个默认。旧写法 `n>14?'auto':0` 让 ≤14 类在**任何一次编辑**后全量展示
    //（0 = 每根柱都出标签），与静态首屏的避让表现分裂 —— 真机验收抓到。
    return {
      rotate:n>10?Math.min(45,Math.max(0,n>20?45:30)):0,
      interval:'auto',
      zoom:false
    };
  }
  // Pivot aggregated entries (that carry a `breakdown` second dimension) into
  // one stacked/grouped series PER breakdown value, with x = the unique primary
  // dimension labels. Shared by categoryOption (edited path) and originalOption
  // (authored path) so a cartesian chart with roles.breakdown renders the same
  // 维度×维度 cross-tab whether it is fresh or edited (WYSIWYG).
  function breakdownSeries(option,type,data,entries,chart){
    var bdVals=[],xd=[];
    entries.forEach(function(e){if(bdVals.indexOf(e.breakdown)<0)bdVals.push(e.breakdown);if(xd.indexOf(e.label)<0)xd.push(e.label);});
    // R3 parity: stacked bars rank CATEGORY axes by their stacked total desc
    // (both static compile and this runtime render). Time/ordinal axes keep
    // their chronological/natural order (R3 waived via sort=time).
    if(type==='stackedBar'&&xd.length>1&&dimensionKind(data,data.dimension)==='category'&&data.measures&&data.measures.length===1){
      var _tot={};entries.forEach(function(e){_tot[e.label]=(_tot[e.label]||0)+number(e.values[0]);});
      xd=xd.slice().sort(function(a,b){return (_tot[b]||0)-(_tot[a]||0);});
    }
    var crowdedLine = type==='stackedBar' && bdVals.length>=5 && xd.length<=3;
    if(crowdedLine && chart) chart.densityAdjusted='line';
    var series=bdVals.map(function(bdv){
      var name=String(bdv);
      return {name:name,type:crowdedLine?'line':(type==='stackedBar'?'bar':type==='area'?'line':type),
        stack:(crowdedLine||type!=='stackedBar')?undefined:'合计',areaStyle:type==='area'?{}:undefined,smooth:crowdedLine||type==='line'||type==='area',
        data:xd.map(function(lbl){var hit=entries.filter(function(e){return e.label===lbl&&String(e.breakdown)===name;})[0];return hit?(hit.values[0]==null?null:number(hit.values[0])):0;})};
    });
    option.series=series;
    // Set the category axis to the unique primary labels (no duplicated x cats).
    var meta=categoryAxisMeta(xd);
    option.xAxis={type:'category',data:xd,boundaryGap:type!=='line'&&type!=='area',
      axisLabel:{fontSize:11,interval:meta.interval,rotate:meta.rotate,color:'#5e6c84'}};
    if(option.legend)option.legend=Object.assign({type:'scroll'},option.legend,{data:series.map(function(s){return s.name;})});
    return option;
  }
  function categoryOption(option,type,data,entries,decimals,chart){    clearCoordinates(option);
    option.tooltip=Object.assign({trigger:'axis'},option.tooltip||{});
    var meta=categoryAxisMeta(entries);
    var hasBd=entries.length&&entries.some(function(e){return e.breakdown!=null;});
    var axisLabels=hasBd?entries.reduce(function(acc,e){if(acc.indexOf(e.label)<0)acc.push(e.label);return acc;},[]):entries.map(function(entry){return entry.label;});
    option.xAxis={type:'category',data:axisLabels,boundaryGap:type!=='line'&&type!=='area',axisLabel:{fontSize:11,interval:meta.interval,rotate:meta.rotate,color:'#5e6c84'}};
    // Systematic axis design (Issue 2): a cartesian chart supports 1-2 value
    // axes. When 2 measures sit on very different scales, split them onto a
    // left/right dual axis so neither crushes the other. Otherwise keep a
    // single axis. The scale ratio is computed from the actual data (more
    // robust than the compiler's measureTypes-signature heuristic) and is
    // also honored by the measureTypes divisor, so e.g. currency-万 + count
    // on the same chart splits cleanly.
    var measures=data.measures||[];
    var chartEd=(chart&&chart.editor)||{};
    var axisCfg=chartEd.axisConfig||{};
    // Y-axis mode is now USER-CONTROLLABLE (默认自动，按尺度差异分/共用；
    // 可强制 分开/共用). This replaces the old silent ratio>3 auto rule, which
    // surprised users (dragging in a 2nd measure sometimes didn't split).
    var axisMode=axisCfg.mode||'auto';
    var dual=false;
    if(measures.length===2){
      var scales=measures.map(function(m,mi){
        var maxAbs=0;
        entries.forEach(function(entry){var v=Math.abs(number(entry.values[mi]));if(v>maxAbs)maxAbs=v;});
        return {m:m,max:maxAbs,left:axisCfg[mi]&&axisCfg[mi].position==='right'};
      });
      var lo=Math.min(scales[0].max,scales[1].max)||1;
      var hi=Math.max(scales[0].max,scales[1].max)||1;
      var ratio=hi/lo;
      if(axisMode==='split')dual=true;
      else if(axisMode==='shared')dual=false;
      else dual=ratio>3;  // auto: a scale ratio > 3 means the series don't share a readable axis.
    }
    var barOpts=chartEd.barOptions||{};
    // Crosstab (维度×维度): when the entries carry a `breakdown` second
    // dimension and there is a single measure, pivot into one series per
    // breakdown category with x = unique primary dimension values. This turns
    // a flat (dim×bd) aggregate into grouped columns instead of duplicated
    // x-categories — the honest rendering of "how many per A×B".
    var breakdownSeries=null;
    if(hasBd&&measures.length===1){
      var bdVals=[],xd=[];
      entries.forEach(function(e){if(bdVals.indexOf(e.breakdown)<0)bdVals.push(e.breakdown);if(xd.indexOf(e.label)<0)xd.push(e.label);});
      // R3 parity: stacked bars rank CATEGORY axes by their stacked total desc.
      if(type==='stackedBar'&&xd.length>1&&dimensionKind(data,data.dimension)==='category'){
        var _tot={};entries.forEach(function(e){_tot[e.label]=(_tot[e.label]||0)+number(e.values[0]);});
        xd=xd.slice().sort(function(a,b){return (_tot[b]||0)-(_tot[a]||0);});
      }
      var crowdedLine = type==='stackedBar' && bdVals.length>=5 && xd.length<=3;
      if(crowdedLine && chart) chart.densityAdjusted='line';
      breakdownSeries=bdVals.map(function(bdv){
        var name=String(bdv);
        return {name:name,type:crowdedLine?'line':(type==='stackedBar'?'bar':type==='area'?'line':type),
          stack:(crowdedLine||type!=='stackedBar')?undefined:'合计',areaStyle:type==='area'?{}:undefined,smooth:crowdedLine||type==='line'||type==='area',
          data:xd.map(function(lbl){var hit=entries.filter(function(e){return e.label===lbl&&String(e.breakdown)===name;})[0];return hit?(hit.values[0]==null?null:number(hit.values[0])):0;})};
      });
      option.series=breakdownSeries;
    }else{
      // Composite (混合图表): per-measure series type + Y-axis. data.seriesType
      // maps measure -> 'bar'|'line' (overrides the chart base type), and
      // data.seriesAxis maps measure -> 0|1 (Y-axis). Grounded in ECharts' per-
      // series {type, yAxisIndex} and Flint's per-series channel semantics.
      var baseSeriesType=(type==='stackedBar')?'bar':(type==='area'?'line':type);
      var anyAxis1=false;
      option.series=(data.measures||[]).map(function(measure,index){
        var st=(data.seriesType&&data.seriesType[measure])||baseSeriesType;
        var ax;
        if(data.seriesAxis&&data.seriesAxis[measure]!=null)ax=Number(data.seriesAxis[measure])||0;
        else if(axisCfg[index]&&axisCfg[index].position==='right')ax=1;
        else if(axisCfg[index]&&axisCfg[index].position==='left')ax=0;
        else ax=(dual&&index===1)?1:0;
        if(ax===1)anyAxis1=true;
        var s={name:measureDisplayName(data,measure),type:st,stack:type==='stackedBar'?'合计':undefined,
          areaStyle:(st==='line'&&type==='area')?{}:undefined,smooth:(st==='line'),
          data:entries.map(function(entry){return entry.values[index];})};
        if(anyAxis1||dual)s.yAxisIndex=ax;
        if(type==='bar'||type==='stackedBar'||st==='bar'){
          // Only apply bar spacing when the editor actually set a non-empty
          // value. An empty string (the panel's default) is an illegal ECharts
          // barGap/barCategoryGap and collapses the bars to ~0 height.
          if(barOpts.barGap!=null&&barOpts.barGap!=='')s.barGap=barOpts.barGap;
          if(barOpts.barCategoryGap!=null&&barOpts.barCategoryGap!=='')s.barCategoryGap=barOpts.barCategoryGap;
        }
        return s;
      });
      dual=dual||anyAxis1;
      // With ≥2 measures, some may share axis 0 while others use axis 1. Ensure
      // every series declares an explicit yAxisIndex (ECharts defaults to 0 but
      // an explicit index keeps the axis assignment unambiguous and the audit
      // of per-series yAxisIndex deterministic).
      if(dual)option.series.forEach(function(s){if(s.yAxisIndex==null)s.yAxisIndex=0;});
    }
    // 单位显示在 Y 轴上方：轴名称 = 用户填的名称（缺省取该轴首个指标名）+ 单位后缀
    // （如「销售额（万）」，单位跟轴走——同一根轴上的指标共用其单位）。
    function nameWithUnit(ai,base){
      var m=null;(data.measures||[]).forEach(function(mm){if(m==null&&(data.seriesAxis&&data.seriesAxis[mm]!=null?data.seriesAxis[mm]:0)===ai)m=mm;});
      if(!m)m=(data.measures||[])[ai]||(data.measures||[])[0]||'';
      var fmt=m?measureFormat(data,m):null;
      var unit=fmt&&fmt.suffix?('（'+fmt.suffix+'）'):'';
      return (base||'')+unit;
    }
    if(dual){
      // Two value axes; each formatted by its own measure. Axis names default
      // to the first measure drawn on that axis, unless overridden via
      // axisConfig[].name. With >2 measures some share an axis (≤2 Y axes).
      var firstOn0=null,firstOn1=null;
      option.series.forEach(function(s){if(s.yAxisIndex===1){if(firstOn1==null)firstOn1=s.name;}else if(firstOn0==null)firstOn0=s.name;});
      var name0=firstOn0||measureDisplayName(data,measures[0]),name1=firstOn1||measureDisplayName(data,measures[1]);
      option.yAxis=[
        {type:'value',name:nameWithUnit(0,axisCfg[0]&&axisCfg[0].name!=null?axisCfg[0].name:name0),axisLabel:{fontSize:11,color:'#5e6c84'},splitLine:{lineStyle:{type:'dashed',color:'#eef1f7'}}},
        {type:'value',name:nameWithUnit(1,axisCfg[1]&&axisCfg[1].name!=null?axisCfg[1].name:name1),axisLabel:{fontSize:11,color:'#5e6c84'},splitLine:{show:false}}
      ];
    }else{
      // 轴名走 measureDisplayName：合成行数指标 '*' 显示「记录数」（编译静态层
      // _measure_name 同口径）——裸回退 measures[0] 会在重聚合后把 '*' 挂上轴顶。
      var baseName=(axisCfg[0]&&axisCfg[0].name!=null)?axisCfg[0].name:measureDisplayName(data,(data.measures&&data.measures[0])||'');
      option.yAxis={type:'value',name:nameWithUnit(0,baseName),axisLabel:{fontSize:11,color:'#5e6c84'},splitLine:{lineStyle:{type:'dashed',color:'#eef1f7'}}};
    }
    option.legend=Object.assign({type:'scroll'},option.legend||{},{data:(option.series||[]).map(function(s){return s.name;})});
    if(entries.length>18&&['bar','stackedBar','line','area'].indexOf(type)>=0){
      option.dataZoom=[{type:'inside',zoomOnFocus:true},{type:'slider',height:16,bottom:2,showDataShadow:false}];
    }
    // Derived measures (同环比/累计/占比) are real series via data.derived + the
    // measure list; there is no separate overlay pass anymore.
    applyMeasureFormats(option,data,decimals);
    return option;
  }
  // ---- Flint zero-baseline (moved-in) -------------------------------------
  // Port of Flint makeZeroDecision + computePaddedDomain. A value axis's
  // baseline follows the measure's semantic zeroClass, the mark (bar/area pins
  // zero; line keeps zero; scatter data-fits), and the data range. This replaces
  // the old blanket `bar/stackedBar -> yAxis.min=0`.
  var FLINT_MEANINGFUL={currency:1,count:1,duration:1,number:1,unitless:1};
  function flintZeroClass(resolvedType){
    // Resolve through the FULL Flint registry (mirroring Python canonical_alias +
    // get_type), not just a T2 subset. This keeps Revenue/Quantity/PercentageChange/
    // Status etc. on the same zero-baseline class as the Python zero_decision, so
    // static compile and runtime patchSemantics agree.
    var t=FLINT_TYPES[flintCanonicalAlias(resolvedType)];
    if(t)return t.zero;
    if(resolvedType==='percent')return 'contextual';
    if(FLINT_MEANINGFUL[resolvedType])return 'meaningful';
    return 'unknown';
  }
  function flintPaddedDomain(values,padFraction){
    var nums=(values||[]).map(number).filter(function(v){return isFinite(v);});
    if(!(padFraction>0)||nums.length<2)return null;
    var dmin=Math.min.apply(null,nums),dmax=Math.max.apply(null,nums),span=dmax-dmin;
    if(!(span>0))return null;
    var pad=span*padFraction;
    return [dmin-pad,dmax+pad];
  }
  function flintZeroDecision(resolvedType,mark,values){
    var zc=flintZeroClass(resolvedType);
    var isBar=(mark==='bar'||mark==='area'||mark==='rect');
    var isScatter=(mark==='circle'||mark==='point');
    var nums=(values||[]).map(number).filter(function(v){return isFinite(v);});
    var dmin=nums.length?Math.min.apply(null,nums):null;
    var dmax=nums.length?Math.max.apply(null,nums):null;
    if(zc==='meaningful'){
      if(isBar)return {zero:true,pad:0};
      if(isScatter){if(dmin!=null&&dmin<=0)return {zero:true,pad:0};return {zero:false,pad:flintZeroPad(resolvedType)};}
      return {zero:true,pad:0}; // line/strip keeps the conventional zero reference
    }
    if(zc==='arbitrary'){if(isBar&&dmin!=null&&dmin<=0)return {zero:true,pad:0};return {zero:false,pad:flintZeroPad(resolvedType)||0.05};}
    if(zc==='contextual'&&nums.length){
      if(dmin<=0)return {zero:true,pad:0};
      var prox=dmax>0?dmin/dmax:0;
      if(prox<0.3)return {zero:true,pad:0};
      if(isBar)return {zero:true,pad:0};
      return {zero:false,pad:0.05};
    }
    if(isBar)return {zero:true,pad:0};
    return {zero:false,pad:0.05};
  }
  function applyFlintValueAxis(axis,dec,values){
    if(!axis||axis.type!=='value')return;
    if(dec.zero){axis.scale=false;delete axis.min;delete axis.max;}
    else{
      axis.scale=true;
      var padded=flintPaddedDomain(values,dec.pad);
      if(padded){axis.min=padded[0];axis.max=padded[1];}
    }
  }
  function axisSeriesValues(series,yIndex){
    var out=[];
    (series||[]).forEach(function(s){
      if((s.yAxisIndex||0)===yIndex&&Array.isArray(s.data))out=out.concat(s.data.map(number));
    });
    return out;
  }
  function capPieSlices(series,maxSlices){
    if(!series||series.type!=='pie'||!Array.isArray(series.data)||series.data.length<=maxSlices)return series;
    var ranked=series.data.slice().sort(function(a,b){return number(b.value)-number(a.value);});
    var head=ranked.slice(0,maxSlices-1);
    var rest=series.data.reduce(function(s,d){return s+number(d.value);},0)-head.reduce(function(s,d){return s+number(d.value);},0);
    if(rest>0)head.push({name:'其他',value:rest});
    series.data=head;
    return series;
  }
  // Post-process semantic chrome applied to EVERY view (authored + switched):
  // bar 0-baseline, reference lines, label overlap, pie cap. (log scale was
  // intentionally removed — no such option exists; see failure-mode.md.)
  function patchSemantics(option,chart,data,decimals){
    if(!option||!Array.isArray(option.series))return option;
    var ed=chart&&chart.editor;
    var vt=(chart&&chart.viewType)||'';
    // Flint zero-baseline (moved-in): per-value-axis baseline from the measure's
    // semantic type + mark + data range, replacing the old blanket bar/stackedBar
    // -> yAxis.min=0. bar/area pin zero; line keeps the reference; percent
    // data-fits when far from zero; arbitrary (rank) never forces zero. A
    // scatter correlation uses two value axes that MUST data-fit the observed
    // spread (Flint zeroDecision treats circle/point as data-fit when the data
    // is far from 0), otherwise a long-tail cluster floats in a large empty
    // region left by a 0-anchored axis.
    var mark=(vt==='bar'||vt==='stackedBar')?'bar':(vt==='area'?'area':(vt==='line'?'line':null));
    if(mark&&option.yAxis){
      var yAxes=Array.isArray(option.yAxis)?option.yAxis:[option.yAxis];
      yAxes.forEach(function(axis,idx){
        if(!axis||axis.type!=='value')return;
        var mName=null;
        (option.series||[]).forEach(function(s){if((s.yAxisIndex||0)===idx&&!mName)mName=s.name;});
        mName=mName||(data.measures||[])[idx];
        var rt=semanticType(data,mName);
        var vals=axisSeriesValues(option.series,idx);
        applyFlintValueAxis(axis,flintZeroDecision(rt,mark,vals),vals);
      });
    }
    // Issue 2: apply Y-axis config (name + side) to every view, authored or
    // switched. axisConfig is keyed by yAxisIndex: {0:{name,position},1:{...}}.
    // We set the axis .name and swap a dual axis when a position is forced.
    if(ed&&ed.axisConfig&&(option.yAxis||option.xAxis)){
      var yAxes=Array.isArray(option.yAxis)?option.yAxis:[option.yAxis];
      var hasDual=Array.isArray(option.yAxis)&&option.yAxis.length>=2;
      yAxes.forEach(function(axis,idx){
        var cfg=ed.axisConfig[idx];
        if(!cfg)return;
        // 轴单位后缀跟轴走（验收 2026-09-14）：本块跑在 buildOption 之后，
        // 直接 axis.name=cfg.name 会把 nameWithUnit 拼好的「名称（单位）」打回
        // 裸名称 —— 单位换了值也换算了，轴上却永远看不到单位，「单位设置无效」
        // 的真因。这里按该轴当前生效单位补同款后缀（名称里已含则不重复）。
        if(cfg.name!=null&&axis&&axis.type==='value'){
          var _sfx=axisUnitSuffixText(data,option,idx);
          axis.name=(_sfx&&String(cfg.name).indexOf(_sfx)<0)?String(cfg.name)+_sfx:cfg.name;
        }
        // 手工轴范围（BI-H4）：min/max 一经指定即优先于 Flint 零基线/padded 决策
        // ——本块跑在 Flint pass 之后，且 ECharts 的 scale:false 是「不与 axis.min/max
        // 冲突时才含 0」（refs/echarts/echarts.d.ts:2731-2736），显式 min/max 天然
        // 胜出。只指定一侧时另一侧保留 Flint 结果（padded 或含 0）。
        if(axis&&axis.type==='value'){
          if(cfg.min!=null&&cfg.min!==''&&Number.isFinite(Number(cfg.min)))axis.min=Number(cfg.min);
          if(cfg.max!=null&&cfg.max!==''&&Number.isFinite(Number(cfg.max)))axis.max=Number(cfg.max);
        }
      });
      // Position overrides: if a measure wants the other side, we must rebuild
      // the series yAxisIndex and reorder the axes. This only applies when the
      // option already carries per-series yAxisIndex (dual axis) OR when a
      // single measure explicitly opts into the right side.
      if(hasDual&&(ed.axisConfig[0]||{}).position==='right'){
        // Swap left/right axes so measure 0 goes right.
        option.series.forEach(function(s){if(s.yAxisIndex===0)s.yAxisIndex=1;else if(s.yAxisIndex===1)s.yAxisIndex=0;});
        var tmp=yAxes[0];yAxes[0]=yAxes[1];yAxes[1]=tmp;
      }
    }
    // Animation toggle (Issue 0): when off, skip the entrance animation so big
    // dashboards render fast. Per-series data still renders fully; only the
    // smooth grow-in is suppressed.
    // Cartesian bar/line grow-in animation is disabled BY DEFAULT: a grow-up
    // entrance animation (bar/line) can stall at the initial frame in some
    // environments (headless / low-power / tab-hidden), leaving bars at ~0
    // height and reading as a blank chart. ABI report charts should render at
    // full height immediately; the animated grow-in is a nicety, not worth a
    // blank chart. Authors can re-enable via chart.editor.animation=true.
    var _cartesianGrow=['bar','stackedBar','line','area'].indexOf(vt)>=0;
    // Default OFF (unless the author explicitly re-enables via animation:true)
    if(ed&&(_cartesianGrow?(ed.animation!==true):(ed.animation===false))){
      option.animation=false;
      option.animationDuration=0;
      option.animationDurationUpdate=0;
    }
    // Reference lines + extreme points are per-measure (keyed by measure on
    // editor.data). Apply each to its matching series. Falls back to the legacy
    // chart-level ed.references / ed.markPoint (series[0]) for authored reports.
    var perRefs=(data&&data.references)||null;
    var perMp=(data&&data.markPoint)||null;
    if((perRefs||(ed&&ed.references))&&Array.isArray(option.series)){
      option.series.forEach(function(s,si){
        var mName=s&&s.name;if(mName==null&&data.measures&&data.measures[si]!=null)mName=data.measures[si];
        var refs=(perRefs&&perRefs[mName])||null;
        if(refs)applyReferences(option,data,refs,decimals,si);
        else if(perRefs&&mName&&perRefs[mName]==null&&si===0&&ed&&ed.references)applyReferences(option,data,ed.references,decimals,si);
        else if(!perRefs&&ed&&ed.references&&si===0)applyReferences(option,data,ed.references,decimals,si);
      });
    }
    if((perMp||(ed&&ed.markPoint))&&Array.isArray(option.series)){
      option.series.forEach(function(s,si){
        var mName=s&&s.name;if(mName==null&&data.measures&&data.measures[si]!=null)mName=data.measures[si];
        var mp=(perMp&&perMp[mName])||null;
        if(mp)applyMarkPoint(option,data,mp,decimals,!!(ed&&ed.flags&&ed.flags.label),si);
        else if(!perMp&&ed&&ed.markPoint&&si===0)applyMarkPoint(option,data,ed.markPoint,decimals,!!(ed&&ed.flags&&ed.flags.label),si);
      });
    }

    // Data labels are bound to each MEASURE (per-series), not a single global
    // toggle. Read data.labels[measure] and set that series' label.show. When no
    // per-measure override exists, fall back to the chart's flags.label so
    // authored reports keep their existing behaviour. Tag override series so
    // patchFlags (run AFTER this) doesn't clobber label.show back to the global
    // toggle.
    // A cross-tab (维度×维度, e.g. stackedBar by breakdown) names each series by
    // its BREAKDOWN value (手机/平板/…), not by the measure name — so a per-measure
    // label toggle keyed by the measure would never match. When there is exactly
    // one measure + a breakdown, all series represent that single measure, so its
    // label toggle drives every series.
    var _labelsFlag=!!(ed&&ed.flags&&ed.flags.label);
    var _crosstab=!!(data&&data.roles&&data.roles.breakdown&&(data.measures||[]).length===1);
    var _ctMeasure=(_crosstab?(data.measures||[])[0]:null);
    if(option.series&&data&&data.measures){
      option.series.forEach(function(s,si){
        var mName=s&&s.name;if(mName==null&&data.measures[si]!=null)mName=data.measures[si];
        var pm=null;
        if(_crosstab&&data.labels&&data.labels[_ctMeasure]!=null)pm=data.labels[_ctMeasure];
        else pm=(data.labels&&data.labels[mName]!=null)?data.labels[mName]:null;
        if(pm!=null){s.label=Object.assign({},s.label||{},{show:!!pm});s._perMeasureLabel=1;}
      });
    }

    // Pie/donut slice cap: too many tiny slices is unreadable, so merge the tail
    // into 其他. Applied to authored AND switched pies (donut renders as pie).
    var maxSlices=(chart&&chart.editor&&chart.editor.maxSlices)||9;
    option.series.forEach(function(s){if(s&&s.type==='pie')capPieSlices(s,maxSlices);});
    var p0=option.series[0];
    if(p0&&p0.type==='pie'&&option.legend)option.legend.data=p0.data.map(function(x){return x.name;});
    var x=axisObject(option.xAxis);
    if(x&&Array.isArray(x.data)&&x.data.length>8&&vt!=='stackedBar')option.labelLayout={hideOverlap:true};
    if(data){applyGoalCompletion(option,data);}
    return option;
  }
  // 目标完成率: append actual/target = n% to the tooltip for measures that have
  // a per-measure goal (参考线→目标值). Wraps an existing function formatter; a
  // string template is left untouched (placeholders are opaque).
  function applyGoalCompletion(option,data){
    if(!option||!option.tooltip)return option;
    var refs=data.references||{};
    var mNames=data.measures||[];
    var hasGoal=mNames.some(function(m){var r=refs[m];return !!(r&&r.type==='goal'&&r.goal!=null&&Number.isFinite(Number(r.goal)));});
    if(!hasGoal)return option;
    var isItem=(option.tooltip.trigger==='item');
    var series=Array.isArray(option.series)?option.series:[];
    if(isItem){
      var s0=series[0];if(!s0)return option;
      var m0=mNames[0]||s0.name;var fmt=measureFormat(data,m0);
      var ref0=refs[m0];
      option.tooltip=Object.assign({},option.tooltip||{},{trigger:'item',formatter:function(p){
        var v=(fmt&&fmt.prefix?fmt.prefix:'')+(p.value!=null?p.value:'')+(fmt&&fmt.suffix?fmt.suffix:'');
        var out=(p.name||'')+(s0.type==='pie'||s0.type==='funnel'?': '+v+' ('+Math.round(p.percent||0)+'%)':': '+v);
        if(ref0&&ref0.type==='goal'&&ref0.goal!=null){var goal=Number(ref0.goal);var raw=Number(p.value)*(Number(fmt&&fmt.divisor)||1);if(isFinite(raw)&&goal!==0)out+='<br/>完成率: '+((raw/goal)*100).toFixed(1)+'%';}
        return out;
      }});
      return option;
    }
    // axis trigger: rebuild a formatter from per-series formats + completion.
    var formats=mNames.map(function(m){return measureFormat(data,m);});
    option.tooltip=Object.assign({},option.tooltip||{},{trigger:'axis',formatter:function(params){
      var list=Array.isArray(params)?params:[params];
      var out=[list[0]&&(list[0].name!=null?list[0].name:'')];
      list.forEach(function(p,index){
        var fmt=formats[index]||{prefix:'',suffix:'',divisor:1};
        var label=(p.seriesName!=null&&p.seriesName!=='')?p.seriesName:(mNames[index]||('指标 '+(index+1)));
        out.push(label+': '+fmt.prefix+(p.value!=null?p.value:'')+fmt.suffix);
        var ref=refs[mNames[index]];
        if(ref&&ref.type==='goal'&&ref.goal!=null&&Number.isFinite(Number(ref.goal))){
          var goal=Number(ref.goal);var raw=(p.value!=null)?Number(p.value)*(Number(fmt.divisor)||1):NaN;
          if(isFinite(raw)&&goal!==0)out.push('完成率: '+((raw/goal)*100).toFixed(1)+'%');
        }
      });
      return out.join('<br/>');
    }});
    return option;
  }
  // A true relationship scatter needs two quantitative measures on the value
  // axes — NOT a category axis. This is the principled fix for the old
  // 'category + one measure' point chart that mislabelled a scatter.
  function scatterOption(option,type,data,entries,decimals,chart){
    clearCoordinates(option);
    var ms=data.measures||[];
    var f0=measureFormat(data,ms[0],decimals),f1=measureFormat(data,ms[1],decimals);
    var xm=ms[0],ym=ms[1]||ms[0];
    var xmi=ms.indexOf(xm),ymi=ms.indexOf(ym);
    var seriesName=ms.length>1?(ms[0]+' × '+ms[1]):'散点';
    // A 3rd quantitative measure encodes bubble size (relationship + intensity).
    var sizeMeasure=ms.length>=3?ms[2]:null,szMax=0;
    var points=entries.map(function(entry){var v=[entry.values[xmi],entry.values[ymi]];if(sizeMeasure){var s=Math.abs(number(entry.values[ms.indexOf(sizeMeasure)]));if(s>szMax)szMax=s;return {name:entry.label,value:v,_sz:s};}return v;});
    if(sizeMeasure&&szMax>0)points.forEach(function(p){p.symbolSize=8+16*(p._sz/szMax);delete p._sz;});
    option.xAxis={type:'value',name:ms[0],axisLabel:{fontSize:11,color:'#5e6c84',formatter:f0?f0.axis:undefined},splitLine:{lineStyle:{type:'dashed',color:'#eef1f7'}}};
    option.yAxis={type:'value',name:ms[1],axisLabel:{fontSize:11,color:'#5e6c84',formatter:f1?f1.axis:undefined},splitLine:{lineStyle:{type:'dashed',color:'#eef1f7'}}};
    // Flint data-fit: a scatter correlation uses two value axes. Data-fit each
    // axis to its own measure's spread so a long-tail cluster (points far above
    // 0, spanning orders of magnitude) does NOT float in a large empty region
    // left by a 0-anchored axis. Grounded in flintZeroDecision(mark='point'):
    // meaningful/arbitrary measures data-fit when far from 0, pin 0 only when
    // the data touches 0. Use the RAW aggregated values (set on the option
    // before display-unit scaling) so a small value rounded to 0 at the 百万/万
    // scale does not wrongly pin the axis.
    (function(){
      var xs=option._rawScatterValues?option._rawScatterValues[0]:[],ys=option._rawScatterValues?option._rawScatterValues[1]:[];
      if(!xs.length){points.forEach(function(p){var v=Array.isArray(p)?p:(p&&p.value)||p;if(Array.isArray(v)){xs.push(number(v[0]));ys.push(number(v[1]));}});}
      var rt0=semanticType(data,ms[0]);
      applyFlintValueAxis(option.xAxis,flintZeroDecision(rt0,'point',xs),xs);
      var rt1=semanticType(data,ms[1]);
      applyFlintValueAxis(option.yAxis,flintZeroDecision(rt1,'point',ys),ys);
    })();
    var series={name:seriesName,type:type,data:points};
    option.series=[series];
    option.legend=Object.assign({},option.legend||{},{data:[seriesName]});
    option.labelLayout={hideOverlap:true};
    if(f0&&f1)option.tooltip={trigger:'item',formatter:'{a}<br/>'+ms[0]+': '+f0.prefix+'{c0}'+f0.suffix+'<br/>'+ms[1]+': '+f1.prefix+'{c1}'+f1.suffix};
  }
  // 无坐标轴图（饼/环/漏斗）图例布局：右侧垂直 + scroll 翻页，宽度按最长标签
  // 显示宽（CJK=2 半宽单位）估值——与编译侧 spec_to_report._share_legend 同一
  // 公式（WYSIWYG），对齐 Flint funnel.ts:112-136 / instantiate-spec.ts:449-540
  // 的右侧垂直图例约定，从根上消除顶部图例换行压图。
  function shareLegendLayout(labels){
    var units=3;
    (labels||[]).forEach(function(x){
      var s=String(x==null?'':x),u=0;
      for(var i=0;i<s.length;i++){var code=s.charCodeAt(i);u+=(code>=0x1100&&(code<=0x115F||code===0x2329||code===0x232A||(code>=0x2E80&&code<=0xA4CF&&code!==0x303F)||(code>=0xAC00&&code<=0xD7A3)||(code>=0xF900&&code<=0xFAFF)||(code>=0xFE30&&code<=0xFE4F)||(code>=0xFF00&&code<=0xFF60)||(code>=0xFFE0&&code<=0xFFE6))?2:1);}
      if(u>units)units=u;
    });
    var width=Math.max(64,Math.min(160,units*7+34));
    return {type:'scroll',orient:'vertical',right:4,top:'middle',width:width,itemGap:8,textStyle:{fontSize:11,color:'#54555a'}};
  }
  function shareOption(option,type,data,entries,decimals,chart){
    clearCoordinates(option);
    var measure=(data.measures||[])[0],seriesType=type==='donut'?'pie':type;
    // FM-24: pie (non-donut) needs an explicit radius — ECharts treats null/undefined
    // radius as "inherits authored option" and falls through to a default that often
    // produces a 0-size render when the option chain is reconstructed. Forcing
    // ['0%','70%'] (solid disc) makes the pie visible regardless of authored option.
    var radius=type==='donut'?['45%','72%']:['0%','70%'];
    // Issue 12: funnel labels show dimension + value (e.g. 美妆 4.6亿).
    var primary=measureFormat(data,measure);
    var fLabel=type==='funnel'?{formatter:function(p){var fmt=primary||{};var v=(fmt.prefix||'')+roundNumber(p.value,decimals!=null?decimals:2)+(fmt.suffix||'');return p.name+'  '+v;},fontSize:11,color:'#334155'}:undefined;
    option.tooltip={trigger:'item'};
    option.series=[{name:measureDisplayName(data,measure),type:seriesType,radius:seriesType==='pie'?radius:undefined,center:seriesType==='pie'?['34%','50%']:undefined,label:fLabel,left:type==='funnel'?'4%':undefined,top:type==='funnel'?12:undefined,bottom:type==='funnel'?12:undefined,width:type==='funnel'?'58%':undefined,minSize:type==='funnel'?'24%':undefined,maxSize:type==='funnel'?'100%':undefined,sort:type==='funnel'?'descending':undefined,gap:type==='funnel'?3:undefined,data:entries.map(function(entry){return {name:entry.label,value:entry.values[0]};})}];
    option.legend=Object.assign({},shareLegendLayout(entries.map(function(entry){return entry.label;})),{data:entries.map(function(entry){return entry.label;})});
    applyMeasureFormats(option,data,decimals);
  }
  function roleIndex(data,name){return (data.columns||[]).indexOf((data.roles||{})[name]);}
  function matrixEntries(data,entries){
    // Aggregate raw rows by (dimension × breakdown) summing the first measure.
    // A heatmap must render one cell per axis intersection, not one
    // raw row (which would stack duplicate [x,y] cells and render garbage).
    var columns=data.columns||[],di=columns.indexOf(data.dimension),bi=roleIndex(data,'breakdown'),mi=columns.indexOf((data.measures||[])[0]);
    if(entries&&entries.matrix)return entries.map(function(entry){return {dimension:entry.label,breakdown:entry.breakdown,value:number(entry.values[0])};});
    var map=new Map();
    (data.rows||[]).forEach(function(row){
      var d=String(row[di]??''),b=String(row[bi]??''),v=number(row[mi]);
      if(d===''||b==='')return;
      var key=d+'\u0000'+b;
      if(!map.has(key))map.set(key,{dimension:d,breakdown:b,value:0});
      map.get(key).value+=v;
    });
    return Array.from(map.values());
  }
  async function specializedOption(option,type,data,entries,decimals,chart){
    // v1 dropped heatmap (see chart-presets.json); no other viewType reaches
    // this branch. Keep it as a pass-through so the series never silently
    // loses semantic formatting.
    applyMeasureFormats(option,data,decimals);
    return option;
  }
  async function originalOption(chart,data,sink){
    var option=clone(chart.option||{}),measures=(data.measures||[]).slice(),series=Array.isArray(option.series)?option.series:[],entries=await aggregate(data,data.dimension,measures,data.aggregation||'sum');
    var noData=!entries.length;
    var decimals=chartDecimals(chart,data);
    applyMeasureScale(entries,data,decimals);
    // Issue 15: user-specified sort applies to authored views too, so a bar/
    // line chart can be reordered by a chosen measure even when it started as
    // its authored type.
    entries=sortEntries(entries,data,chart.viewType||'bar',chart);
    // sink 必须传（与编辑路径的 applyTopK 同一口径）：编译期 truncation 事实缺席
    // 或已失效（topK 被改过）时，作者视图的 TOP-N 截断只能靠它登记——静默截断 = 报告失真。
    entries=applyTopK(entries,data,chart.viewType||'bar',sink);
    var allMeasures=(chart.editor&&chart.editor.originalMeasures)||numericColumns(data);
    var roles=data.roles||{};
    // Heatmap/matrix charts are two-dimensional: a single-dimension aggregation
    // cannot rebuild their data without flattening the second dimension. Keep
    // the authored data verbatim and only filter series to the selected measures.
    // v1 dropped heatmap; source/target are the residual matrix markers. A
    // cartesian chart carrying only roles.breakdown is NOT a matrix: it is the
    // intended 维度×维度 cross-tab (bar.stacked / bar.rank / table.aggregate), so
    // it must re-aggregate into one stacked/grouped series PER breakdown value
    // (WYSIWYG: the authored view == the edited view).
    var viewType=(chart&&chart.viewType)||data.viewType||'';
    var isCartesian=viewType==='bar'||viewType==='stackedBar'||viewType==='line'||viewType==='area';
    var matrixChart=(!!(roles.source||roles.target))||(!!roles.breakdown&&!isCartesian);
    if(matrixChart){
      option.series=series.filter(function(item){return !item.name||allMeasures.indexOf(item.name)<0||measures.indexOf(item.name)>=0;});
      return applyMeasureFormats(option,data,decimals);
    }
    if(noData&&sink)sink.empty=true;
    var axis=categoryAxis(option),labels=entries.map(function(entry){return entry.label;}),shareSeries=series.some(function(s){return s&&(s.type==='pie'||s.type==='funnel');});
    // 维度×维度 cross-tab (bar.stacked / bar.rank with a breakdown): when the
    // aggregated entries carry a `breakdown` second dimension, pivot into one
    // stacked/grouped series PER breakdown value with x = unique primary labels
    // (mirrors categoryOption so the authored view == the edited view).
    var hasBd=entries.length&&entries.some(function(e){return e.breakdown!=null;});
    if(hasBd&&isCartesian){var optionRef=option;breakdownSeries(optionRef,viewType,data,entries,chart);return applyMeasureFormats(optionRef,data,decimals);}
    if(axis){
      var target=axis.axis==='x'?axisObject(option.xAxis):axisObject(option.yAxis);
      target.data=labels;
      // Issue 0: propagate axisName when set on the authored option (small UX
      // addition: "X轴标题" / "Y轴标题" in the authored HTML shows up).
      if(chart.editor&&chart.editor.axisName){
        if(chart.editor.axisName.x&&axisObject(option.xAxis))option.xAxis.name=chart.editor.axisName.x;
        if(chart.editor.axisName.y&&axisObject(option.yAxis))option.yAxis.name=chart.editor.axisName.y;
      }
    }
    option.series=series.filter(function(item){return !item.name||allMeasures.indexOf(item.name)<0||measures.indexOf(item.name)>=0;}).map(function(item,index){
      var measureIndex=item.name?measures.indexOf(item.name):index;
      // Only flatten the data to a value array for a category-axis series
      // (bar/line/area). Share series (pie/funnel) keep
      // their authored {name, value} objects — flattening loses the names and
      // breaks ECharts (null-name on _updateData).
      if(axis&&measureIndex>=0&&entries.length&&!shareSeries)item.data=entries.map(function(entry){return entry.values[measureIndex];});
      // FM-24: pie (non-donut) without explicit radius renders as a 0-size disc
      // in ECharts — force a default solid radius when the authored option left
      // it blank. Donut already carries a [45%,72%] ring in the authored option.
      if(item&&item.type==='pie'&&(item.radius==null||item.radius===undefined))item.radius=item.hasOwnProperty('innerRadius')?item.radius:['0%','70%'];
      return item;
    });
    var named=option.series.map(function(item){return item.name;}).filter(Boolean);
    if(option.legend)option.legend=Object.assign({},option.legend,{data:named});
    return applyMeasureFormats(option,data,decimals);
  }
  // ---- 报告级图表主题 (Flint theme presets → ECharts option) ----------------
  // Grounded in refs/flint/src/core/theme/presets/*.ts. Each preset carries the
  // series categorical palette + text/grid/axis colors + headline font family +
  // plot surface. The Python side (spec_to_report._apply_chart_theme) keeps an
  // identical table so static compile and runtime re-aggregation agree (WYSIWYG).
  // 'default' = no override (current ECharts default palette + hardcoded chrome).
  var CHART_THEMES={
    'economist':{label:'The Economist',color:['#006ba2','#3ebcd2','#ebb434','#379a8b','#9a3d5b','#a17ba5'],text:'#54585a',grid:'#c9d3da',axis:'#121317',font:"'Helvetica Neue',Helvetica,Arial,sans-serif",bg:null},
    'nyt':{label:'New York Times',color:['#2f6b9a','#c2352b','#4a8b6f','#7f6a9e','#d9a441'],text:'#6b6b6b',grid:'#e4e4e4',axis:'#121212',font:'Georgia, serif',bg:null},
    'swiss':{label:'Swiss',color:['#e2231a','#1a1a1a','#0067a5','#f2b705','#2a7f4f'],text:'#555555',grid:'#d9d5cc',axis:'#1a1a1a',font:"'Helvetica Neue',Helvetica,Arial,sans-serif",bg:'#f4f1ea'},
    'nature':{label:'Nature',color:['#0072b2','#e69f00','#009e73','#cc79a7','#56b4e9','#d55e00'],text:'#000000',grid:'#00000000',axis:'#000000',font:'Arial, Helvetica, sans-serif',bg:null},
    'mckinsey':{label:'McKinsey',color:['#051c2c','#2251ff','#00a9f4','#00cfb4','#8c9ba5'],text:'#5a6872',grid:'#d3dce1',axis:'#051c2c',font:"'Helvetica Neue',Helvetica,Arial,sans-serif",bg:null},
    'datawrapper':{label:'Datawrapper',color:['#18a1cd','#e2a233','#c04a4a','#2d8659','#7e5aa2'],text:'#666666',grid:'#dcdcdc',axis:'#333333',font:"'Helvetica Neue',Helvetica,Arial,sans-serif",bg:null},
    'powerbi':{label:'Power BI',color:['#118dff','#e66c37','#3bd1c7','#e044a7','#d9b300','#8764b8'],text:'#c8c6c4',grid:'#3b3a39',axis:'#3b3a39',font:"'Segoe UI',system-ui,sans-serif",bg:'#1b1a19'},
    'powerbi-light':{label:'Power BI Light',color:['#118dff','#12239e','#e66c37','#6b007b','#e044a7','#744ec2'],text:'#605e5c',grid:'#ededed',axis:'#d2d0ce',font:"'Segoe UI',system-ui,sans-serif",bg:'#ffffff'},
    'pop':{label:'Pop',color:['#ff1493','#00d9ff','#ff5a1f','#7a3cff','#00c853','#111111'],text:'#5f0047',grid:'#111111',axis:'#111111',font:"'Arial Black','Helvetica Neue',Arial,sans-serif",bg:'#fff200'},
    'cartoon':{label:'Cartoon',color:['#3aa9ff','#ff5d5d','#ffc23c','#4cc76a','#9b6cff','#ff8a3d'],text:'#8a837a',grid:'#ece5d6',axis:'#2e2b28',font:"'Comic Sans MS','Comic Neue','Chalkboard SE','Marker Felt',cursive",bg:'#fffdf5'}
  };
  var CHART_THEME_ID='default';
  // 报告主题轴收敛（docs/report-theme-convergence-design.md）：manifest 里带
  // 图表切片的主题（如 huawei，内置表没有）经 setChartTheme(id, override) 注入；
  // override 优先于内置表。壳令牌不归本文件（editor-runtime applyShellTheme）。
  var CHART_THEME_OVERRIDE=null;
  function setChartTheme(id,override){CHART_THEME_ID=(id&&(CHART_THEMES[id]||(override&&override.color)))?id:'default';CHART_THEME_OVERRIDE=(override&&override.color)?override:null;}
  function themeCatalog(){return [{id:'default',label:'默认'}].concat(Object.keys(CHART_THEMES).map(function(k){return {id:k,label:CHART_THEMES[k].label};}));}
  // W3 主题色一致性（docs/ui-ux-execution-brief-2026-09-15.md §4-W3）：default 档
  // 「无 override」时 ECharts 走出厂调色板，壳 accent 覆盖后无法联动 [0] 号位
  // （壳紫、图表仍蓝首的双 accent 割裂）。这里把出厂调色板显式转录（grounded：
  // refs/echarts/src/visual/tokens.ts:111-123 tokens.color.theme，逐位一致，非手改），
  // 供 editor-runtime 的 accent 联动在 default 档拼装 [覆盖色, ...出厂尾]。
  var ECHARTS6_DEFAULT_PALETTE=['#5070dd','#b6d634','#505372','#ff994d','#0ca8df','#ffd10a','#fb628b','#785db0','#3fbe95'];
  function defaultPalette(){return ECHARTS6_DEFAULT_PALETTE.slice();}
  // 内置切片只读访问（不含运行时 override 状态）：editor-runtime accent 联动取
  // 非 default 档的「尾色」用，避免经 chartThemeInk 读到半途的 override。
  function chartThemeSlice(id){return CHART_THEMES[id]?Object.assign({},CHART_THEMES[id]):null;}
  // 样票（editor-runtime themeSwatchSVG）取图表切片的唯一入口：override 优先。
  function chartThemeInk(id){return id===CHART_THEME_ID&&CHART_THEME_OVERRIDE?CHART_THEME_OVERRIDE:CHART_THEMES[id]||null;}
  function applyChartTheme(option){
    var t=CHART_THEME_OVERRIDE||CHART_THEMES[CHART_THEME_ID];if(!t||!option)return option;
    if(t.color)option.color=t.color.slice();
    if(t.bg)option.backgroundColor=t.bg;
    if(t.font)option.textStyle=Object.assign({},option.textStyle,{fontFamily:t.font});
    function recolor(ax){
      if(!ax)return;
      if(Array.isArray(ax)){ax.forEach(recolor);return;}
      if(t.text){var al=Object.assign({},ax.axisLabel);al.color=t.text;ax.axisLabel=al;}
      // 轴标题跟 t.text：深底主题（powerbi）否则轴名深灰压深底不可见（judge 验收）。
      // Flint axisTitle.color 是独立槽位；v1 用轴标签墨近似。
      if(t.text&&ax.name){var nt=Object.assign({},ax.nameTextStyle,{color:t.text});ax.nameTextStyle=nt;}
      if(t.axis){var lf=Object.assign({},ax.axisLine);var ls=Object.assign({},lf.lineStyle||{});ls.color=t.axis;lf.lineStyle=ls;ax.axisLine=lf;}
      if(t.grid){var sl=ax.splitLine;if(sl){var sl2=Object.assign({},sl);var sls=Object.assign({},sl2.lineStyle||{});sls.color=t.grid;sl2.lineStyle=sls;ax.splitLine=sl2;}}
    }
    recolor(option.xAxis);recolor(option.yAxis);
    // 图例文字跟 t.text：深底主题上默认深灰图例不可见（同上）。
    if(t.text&&option.legend)option.legend=Object.assign({},option.legend,{textStyle:Object.assign({},option.legend.textStyle,{color:t.text})});
    return option;
  }
  async function optionFor(chart,view,resolvedData){
    var type=view||chart.viewType||'original',data=inferEditorData(chart,resolvedData);
    if(!data)return clone(chart.option||{});
    var sink={};   // 本次 optionFor 的截断槽位（并发渲染互不干扰，见 noteTruncation）
    // Y 轴单位跟轴走：把 chart.editor.axisConfig（名称 + 每轴单位）并入 data，供
    // measureFormat 依据该指标所在的轴（seriesAxis）解析单位。
    if(chart&&chart.editor&&chart.editor.axisConfig)data.axisConfig=chart.editor.axisConfig;
    var decimals=chartDecimals(chart,data);
    // Scatter is a true measure x measure relationship: it must ALWAYS go through
    // scatterOption (dual VALUE axes + divisor/decimals), never the authored
    // 'original' path which would keep raw values / a category axis.
    if(type==='scatter'){
      var scOpt=clone(chart.option||{});
      var se=await aggregate(data,data.dimension,data.measures||[],matrixAgg(data));
      // Capture the RAW aggregated measure values (before display-unit scaling)
      // so the scatter data-fit decision is not fooled by a small value being
      // rounded to 0 at the 百万/万 scale (which would wrongly pin the axis to 0).
      var rawMs=data.measures||[];
      var _raw=[[],[]];
      se.forEach(function(en){var xv=number(en.values[rawMs.indexOf(rawMs[0])]);var yv=number(en.values[rawMs.indexOf(rawMs[1]||rawMs[0])]);_raw[0].push(xv);_raw[1].push(yv);});
      scOpt._rawScatterValues=_raw;
      applyMeasureScale(se,data,decimals);
      if(!se.length)sink.empty=true;
      scatterOption(scOpt,type,data,se,decimals,chart);
      return withTruncation(applyValueAliases(applyAliasNames(applyCondFormat(applyChartTheme(normalizeLegend(patchSemantics(scOpt,chart,data,decimals))),chart),data),data),sink,fallbackTrunc);
    }
    // The authored 'original' option is preserved ONLY while the chart is
    // unedited. Once the user drags dimensions/measures (editor.dirty=true),
    // we must RE-AGGREGATE from editor.data (the categoryOption path), because
    // originalOption() keeps the authored series and filters them to the
    // current measures — swapping a measure to a column the authored option
    // never had (e.g. 商品销售额→件数) filters every series out and the chart
    // renders empty. That empty/series state is #6's stuck chart.
    var originalPath=(type==='original'||type==='echarts'||(chart.editor&&chart.editor.originalViewType===type&&!chart.editor.dirty));
    // 编译期登记的截断事实（chart.truncation = {shown,total}）：只在图表仍用作者
    // option 时成立——此时运行时没有重新聚合，拿不到分组总数。一旦作者/读者改了
    // topK，data.topK 与登记的 shown 不再相等 → 以**实时**结果为准，旧的必须作废
    // （否则把 topK 从 12 调到 50 后还挂着「前 12 项」）。
    var fallbackTrunc=null;
    if(originalPath&&chart&&chart.truncation&&chart.truncation.total){
      var _reg=chart.truncation;
      var _cur=Number((data&&data.topK)||0);
      if(!_cur||_cur===Number(_reg.shown))fallbackTrunc={shown:Number(_reg.shown),total:Number(_reg.total)};
    }
    var option;
    if(originalPath){option=await originalOption(chart,data,sink);}
    else{
      option=clone(chart.option||{});
      if(type==='table')return null;
      var single=['bar','stackedBar','line','area','pie','donut','funnel'].indexOf(type)>=0;
      // Matrix data (heatmap breakdown) must be re-aggregated by
      // the single dimension when rebuilt as a 1-D chart; 'none' would leak raw
      // cells (the 0~12月 repeat bug). See matrixAgg().
      // Q3-Issue3: per-measure aggregation. If data.aggregations is an
      // object keyed by measure, build a parallel array. Otherwise fall back
      // to the single data.aggregation (or its matrix-aware version).
      var measureList=data.measures||[];
      var perMeasureAgg=null;
      if(data.aggregations&&typeof data.aggregations==='object'&&!Array.isArray(data.aggregations)){
        perMeasureAgg=measureList.map(function(m){return data.aggregations[m]||data.aggregation||'sum';});
      } else if(Array.isArray(data.aggregations)){
        perMeasureAgg=data.aggregations.slice(0,measureList.length);
        while(perMeasureAgg.length<measureList.length)perMeasureAgg.push(data.aggregation||'sum');
      }
      var agg=single?matrixAgg(data):(data.aggregation||'sum');
      var aggArg=perMeasureAgg||agg;
      var entries=await aggregate(data,data.dimension,measureList,aggArg,sink);
      if(!entries.length)sink.empty=true;
      applyMeasureScale(entries,data,decimals);
      // Issue 15: user-specified sort (field + direction). Temporal dimension
      // keeps its name/date order (asc by label); other charts sort by the
      // chosen field (default: first measure, descending). maxCategories cap is
      // removed (Issue 2) — high-cardinality is left to dataZoom instead.
      entries=sortEntries(entries,data,type,chart);
      // TOP K (Issue 4): bar/stackedBar may cap to the top-K by primary measure.
      // Applies after sorting so the kept entries are the true top-K; the
      // server already LIMITs via data.topK for server mode, and this local
      // slice covers singlefile (official sort + filter semantics).
      entries=applyTopK(entries,data,type,sink);
      if(['bar','stackedBar','line','area'].indexOf(type)>=0)categoryOption(option,type,data,entries,decimals,chart);
      else if(['pie','donut','funnel'].indexOf(type)>=0)shareOption(option,type,data,entries,decimals,chart);
      else option=await specializedOption(option,type,data,entries,decimals,chart);
    }
    return withTruncation(applyValueAliases(applyAliasNames(applyCondFormat(applyChartTheme(normalizeLegend(patchSemantics(option,chart,data,decimals))),chart),data),data),sink,fallbackTrunc);
  }
  // 把本轮的截断信息挂在 option 上（ECharts 会忽略未知顶层键；renderChart 读到后
  // 渲染说明并从 option 里删掉再 setOption）。同时给出数据点总数，供空态判定。
  // 系列名/轴名用显示名（别名）。**必须在最后一步做**：未编辑的编译图表走
  // originalOption（直接用作者 option 的 series 名），那条路径根本不经过
  // measureDisplayName —— 只在 measureDisplayName 里接别名会让「改完字段名图例
  // 还是旧名」（真机复现）。这里按「名字本身就是 chart 的某个指标列名」判定，
  // 派生指标标签 / breakdown 值天然不匹配，不会误改。
  function applyAliasNames(option,data){
    if(!option||typeof option!=='object')return option;
    var ms=(data&&data.measures)||[];
    var renamed=false;
    function mapName(name){
      if(typeof name!=='string'||!name)return name;
      if(ms.indexOf(name)<0)return name;
      return aliasOf(data,name);
    }
    (Array.isArray(option.series)?option.series:[]).forEach(function(se){
      if(!se)return;
      if(typeof se.name==='string'){var nm=mapName(se.name);if(nm!==se.name){se.name=nm;renamed=true;}}
    });
    [option.xAxis,option.yAxis].forEach(function(axes){
      (Array.isArray(axes)?axes:[axes]).forEach(function(ax){
        if(!ax)return;
        if(ax.name!==undefined&&ax.name!==''&&ms.indexOf(ax.name)>=0)ax.name=mapName(ax.name);
      });
    });
    // 图例同步：legend.data 是改名前按旧系列名烙好的显式数组，系列名换了它不跟
    // → 图例显示旧名，且 legendToggleSelect 按旧名找不到改名后的系列（联动失效）。
    if(renamed)rebuildLegendData(option);
    return option;
  }
  // 图例数据重建（别名改名的唯一善后入口）：饼/环/漏斗以维度值为图例键
  // （series[0].data[].name——applyValueAliases 刚把扇区名换成别名），其余图按
  // 改名后的 series[].name 重建。仅当 option 本来就带 legend.data（编译期烙好
  // 的显式数组）才重建——没有 data 的图例由 ECharts 按系列名自动派生，改名后
  // 天然同步，补一个数组反而可能覆盖作者的自定义图例子集。
  function rebuildLegendData(option){
    if(!option||!option.legend||!Array.isArray(option.legend.data))return;
    var ss=(Array.isArray(option.series)?option.series:[]).filter(Boolean);
    var share=null;
    for(var i=0;i<ss.length;i+=1){if(ss[i]&&(ss[i].type==='pie'||ss[i].type==='funnel')){share=ss[i];break;}}
    var names=(share&&Array.isArray(share.data))
      ?share.data.map(function(d){return d&&typeof d==='object'?d.name:d;})
      :ss.map(function(s){return s&&s.name;});
    option.legend.data=names.filter(function(n){return typeof n==='string'&&n!=='';});
  }
  // 维度值别名（显示层**最后一步**，与字段别名同一哲学：取数/筛选/下钻一律用原值）。
  // 类目轴刻度、饼扇与矩阵系列名里的维度值换成 ds.valueAliases 的显示文案；发生
  // 改名时在 option 上留反查表 ``__valAliasBack``（显示值 → 原值），点击下钻把
  // params.name 还原成原值再进 drillPath——否则下钻会拿别名当筛选值打出空结果。
  var VALUE_ALIASES={};
  function setValueAliases(map){VALUE_ALIASES=map||{};}
  function valueAliasOf(data,col,val){
    if(val==null||col==null)return val;
    var k=data&&data.datasetId;
    var m=(k&&VALUE_ALIASES[k])||null;
    if(!m){
      // 没有 datasetId（旧图表）时退化：任取一张含该列值别名表。
      var ks=Object.keys(VALUE_ALIASES);
      for(var i=0;i<ks.length;i+=1){var mm=VALUE_ALIASES[ks[i]];if(mm&&mm[col]&&Object.keys(mm[col]).length){m=mm;break;}}
    }
    var mm2=m&&m[col];
    if(!mm2)return val;
    var v=mm2[String(val)];
    return (typeof v==='string'&&v)?v:val;
  }
  function applyValueAliases(option,data){
    if(!option||typeof option!=='object')return option;
    var cols=[];
    var pushc=function(c){if(c&&cols.indexOf(c)<0)cols.push(c);};
    pushc(data&&data.dimension);
    ((data&&data.dimensions)||[]).forEach(pushc);
    pushc(data&&data.roles&&data.roles.breakdown);
    if(!cols.length)return option;
    var ms=(data&&data.measures)||[];
    var back=null;
    function mapVal(v){
      if(typeof v!=='string')return v;
      if(!v){
        // 空值展示样式（对标 Quick BI）：空串/空值的轴刻度、扇区名统一显示 '-'。
        for(var j=0;j<cols.length;j+=1){
          var m0=VALUE_ALIASES[(data&&data.datasetId)]&&VALUE_ALIASES[(data&&data.datasetId)][cols[j]];
          if(m0&&typeof m0['']==='string'&&m0[''])return m0[''];
        }
        return '-';
      }
      for(var i=0;i<cols.length;i+=1){
        var a=valueAliasOf(data,cols[i],v);
        if(a!==v){
          if(!back)back={};
          if(back[a]===undefined)back[a]=v;
          return a;
        }
      }
      return v;
    }
    [option.xAxis,option.yAxis].forEach(function(axes){
      (Array.isArray(axes)?axes:[axes]).forEach(function(ax){
        if(ax&&ax.type==='category'&&Array.isArray(ax.data))ax.data=ax.data.map(mapVal);
      });
    });
    (Array.isArray(option.series)?option.series:[]).forEach(function(se){
      if(!se)return;
      // 普通图的系列名 = 指标名（applyAliasNames 刚处理过），不是维度值，跳过。
      if(typeof se.name==='string'&&se.name&&ms.indexOf(se.name)<0)se.name=mapVal(se.name);
      if(Array.isArray(se.data))se.data=se.data.map(function(it){
        if(it&&typeof it==='object'&&typeof it.name==='string'&&it.name)it.name=mapVal(it.name);
        return it;
      });
    });
    if(back){
      option.__valAliasBack=back;
      // 扇区名 / 交叉系列名（breakdown 值）换新后，旧名烙的 legend.data 必须重建
      //（同 applyAliasNames 末尾：图例显示旧名 + legendToggleSelect 找不到系列）。
      rebuildLegendData(option);
    }
    return option;
  }
  function withTruncation(option,sink,fallback){
    if(!option||typeof option!=='object')return option;
    // 空态由 adapter 判定（aggregate 返回 0 行），不是由点数推断 —— 作者 option
    // 路径下 0 行只会清空轴标签，series 数据仍是作者写的，点数不为 0。
    if(sink&&sink.empty)option.aiEmpty=true;
    // 实时算出来的优先；否则退回编译期登记的事实（未编辑的图表）。
    if(sink&&sink.trunc)option.aiTruncation=sink.trunc;
    else if(fallback)option.aiTruncation=fallback;
    option.aiPointCount=optionPointCount(option);
    return option;
  }
  // 数据点计数：series[].data 里的非空项 + option.dataset.source 行数。
  function optionPointCount(option){
    var n=0;
    ((option&&option.series)||[]).forEach(function(sr){
      if(!sr||!Array.isArray(sr.data))return;
      sr.data.forEach(function(v){
        if(v==null)return;
        // 散点/热力的点是 [x,y(,value)] 数组形态：typeof 为 'object' 但不是 {value:…} 包装，
        // 必须在对象守卫之前显式计数（漏掉会让 scatterOption 真有 22 个点却判空态）。
        if(Array.isArray(v)){n+=1;return;}
        if(typeof v==='object'&&v.value==null)return;
        n+=1;
      });
    });
    var ds=option&&option.dataset;
    (Array.isArray(ds)?ds:[ds]).forEach(function(d){
      if(d&&Array.isArray(d.source))n+=d.source.length;
    });
    return n;
  }
  // 图例位置约束：直角坐标图的图例不允许放在底部（会与数值轴刻度重叠）。
  //  - scatter 单系列：图例只有一个冗余的系列名（两个指标已由轴名标注），直接隐藏。
  //  - 无坐标轴图（pie/donut/funnel）：图例一律右侧垂直 + scroll，本体左移让位
  //    （shareLegendLayout；老报告的静态 option 也在此被归一成新布局）。
  //  - 其它图例：移除 bottom/right 摆放，强制 top:0（与 bar/line 一致）。
  function normalizeLegend(option){
    if(!option||!option.legend)return option;
    var s=option.series||[];
    var onlyScatter=s.length===1&&s[0]&&s[0].type==='scatter';
    if(onlyScatter){option.legend=Object.assign({},option.legend,{show:false});return option;}
    var share=s.some(function(x){return x&&(x.type==='pie'||x.type==='funnel');});
    if(share){
      var lg0=Object.assign({},option.legend);
      if(lg0.show===false){option.legend=lg0;return option;}
      var names=Array.isArray(lg0.data)?lg0.data.map(function(d){return typeof d==='string'?d:(d&&d.name)||'';}):[];
      if(!names.length){var p=s.filter(function(x){return x&&(x.type==='pie'||x.type==='funnel');})[0];
        if(p&&Array.isArray(p.data))names=p.data.map(function(d){return d&&d.name||'';});}
      option.legend=Object.assign({},lg0,shareLegendLayout(names));
      s.forEach(function(x){
        if(!x)return;
        if(x.type==='pie'&&x.center==null)x.center=['34%','50%'];
        if(x.type==='funnel'){if(x.left==null)x.left='4%';if(x.width==null)x.width='58%';}
      });
      return option;
    }
    var lg=Object.assign({},option.legend);
    delete lg.bottom;delete lg.right;
    if(lg.top==null)lg.top=0;
    option.legend=lg;
    return option;
  }
  function patchAxisGrid(axis,show){
    if(!axis)return axis;
    if(Array.isArray(axis))return axis.map(function(item){return patchAxisGrid(item,show);});
    return Object.assign({},axis,{splitLine:Object.assign({},axis.splitLine||{},{show:show})});
  }
  function patchFlags(option,flags){
    // Top-level shallow copy (NOT JSON clone): a JSON clone would silently drop
    // function values like a series label formatter, so a funnel/pie label
    // written by the adapter lost its formatter on every render. Keep the
    // series objects by reference; only the fields we touch are replaced.
    var out=Object.assign({},option||{});
    if(flags.legend!=null)out.legend=Object.assign({},out.legend||{},{show:!!flags.legend});
    if(flags.grid!=null){if(out.xAxis)out.xAxis=patchAxisGrid(out.xAxis,!!flags.grid);if(out.yAxis)out.yAxis=patchAxisGrid(out.yAxis,!!flags.grid);}
    if(flags.label!=null&&Array.isArray(out.series))out.series=out.series.map(function(series){
      // Per-measure label override wins (tagged in patchSemantics) so the
      // global toggle doesn't clobber a measure-specific setting.
      if(series&&series._perMeasureLabel){var c=Object.assign({},series,{label:Object.assign({},series.label||{},{show:!!series.label.show})});delete c._perMeasureLabel;return c;}
      return Object.assign({},series,{label:Object.assign({},series.label||{},{show:!!flags.label})});
    });
    return out;
  }
  // ---- Flint semantic core, ported to JS (single codebase) -------------------
  // Mirrors scripts/semantics/flint_core.py + recommend.py so live editing can
  // consult the same moved-in Flint rules the compiler uses (axis kind, zero-
  // baseline, chart-type recommendation) — no split between skill and Flint.
  var FLINT_TYPES={
    DateTime:{t0:'Temporal',t1:'DateTime',v:['temporal'],aggRole:'dimension',domainShape:'open',diverging:'none',fmt:'plain',zero:'none',pad:0},
    Year:{t0:'Temporal',t1:'DateGranule',v:['temporal','ordinal'],aggRole:'dimension',domainShape:'open',diverging:'none',fmt:'integer',zero:'arbitrary',pad:0.03},
    Month:{t0:'Temporal',t1:'DateGranule',v:['ordinal'],aggRole:'dimension',domainShape:'cyclic',diverging:'none',fmt:'plain',zero:'none',pad:0},
    Week:{t0:'Temporal',t1:'DateGranule',v:['ordinal'],aggRole:'dimension',domainShape:'cyclic',diverging:'none',fmt:'plain',zero:'none',pad:0},
    Day:{t0:'Temporal',t1:'DateGranule',v:['ordinal'],aggRole:'dimension',domainShape:'cyclic',diverging:'none',fmt:'plain',zero:'none',pad:0},
    YearMonth:{t0:'Temporal',t1:'DateGranule',v:['temporal','ordinal'],aggRole:'dimension',domainShape:'open',diverging:'none',fmt:'plain',zero:'none',pad:0},
    YearWeek:{t0:'Temporal',t1:'DateGranule',v:['temporal','ordinal'],aggRole:'dimension',domainShape:'open',diverging:'none',fmt:'plain',zero:'none',pad:0},
    Duration:{t0:'Temporal',t1:'Duration',v:['quantitative'],aggRole:'additive',domainShape:'open',diverging:'none',fmt:'unit-suffix',zero:'meaningful',pad:0},
    Amount:{t0:'Measure',t1:'Amount',v:['quantitative'],aggRole:'additive',domainShape:'open',diverging:'none',fmt:'currency',zero:'meaningful',pad:0},
    Revenue:{t0:'Measure',t1:'Amount',v:['quantitative'],aggRole:'additive',domainShape:'open',diverging:'none',fmt:'currency',zero:'meaningful',pad:0},
    // Price intensive（refs/flint/src/core/type-registry.ts:105，2026-10-13 批）：
    // 单价/售价不可加、缺省 average——与 PY flint_core.Price 逐字段镜像。
    Price:{t0:'Measure',t1:'Amount',v:['quantitative'],aggRole:'intensive',domainShape:'open',diverging:'none',fmt:'currency',zero:'meaningful',pad:0},
    Cost:{t0:'Measure',t1:'Amount',v:['quantitative'],aggRole:'additive',domainShape:'open',diverging:'none',fmt:'currency',zero:'meaningful',pad:0},
    Temperature:{t0:'Measure',t1:'Physical',v:['quantitative'],aggRole:'intensive',domainShape:'open',diverging:'conditional',fmt:'unit-suffix',zero:'arbitrary',pad:0.05},
    Sentiment:{t0:'Measure',t1:'SignedMeasure',v:['quantitative'],aggRole:'intensive',domainShape:'open',diverging:'inherent',fmt:'decimal',zero:'meaningful',pad:0},
    Correlation:{t0:'Measure',t1:'SignedMeasure',v:['quantitative'],aggRole:'intensive',domainShape:'bounded',diverging:'inherent',fmt:'decimal',zero:'meaningful',pad:0},
    Quantity:{t0:'Measure',t1:'Physical',v:['quantitative'],aggRole:'additive',domainShape:'open',diverging:'none',fmt:'unit-suffix',zero:'meaningful',pad:0},
    Percentage:{t0:'Measure',t1:'Proportion',v:['quantitative'],aggRole:'intensive',domainShape:'bounded',diverging:'none',fmt:'percent',zero:'contextual',pad:0},
    PercentageChange:{t0:'Measure',t1:'SignedMeasure',v:['quantitative'],aggRole:'intensive',domainShape:'open',diverging:'conditional',fmt:'percent',zero:'contextual',pad:0.05},
    Profit:{t0:'Measure',t1:'SignedMeasure',v:['quantitative'],aggRole:'signed-additive',domainShape:'open',diverging:'conditional',fmt:'decimal',zero:'meaningful',pad:0},
    Count:{t0:'Measure',t1:'GenericMeasure',v:['quantitative'],aggRole:'additive',domainShape:'open',diverging:'none',fmt:'integer',zero:'meaningful',pad:0},
    Number:{t0:'Measure',t1:'GenericMeasure',v:['quantitative'],aggRole:'additive',domainShape:'open',diverging:'none',fmt:'decimal',zero:'meaningful',pad:0},
    Rank:{t0:'Discrete',t1:'Rank',v:['ordinal'],aggRole:'dimension',domainShape:'open',diverging:'none',fmt:'integer',zero:'arbitrary',pad:0.08},
    Score:{t0:'Discrete',t1:'Score',v:['quantitative','ordinal'],aggRole:'intensive',domainShape:'bounded',diverging:'conditional',fmt:'decimal',zero:'contextual',pad:0.05},
    Category:{t0:'Categorical',t1:'Entity',v:['nominal'],aggRole:'dimension',domainShape:'open',diverging:'none',fmt:'plain',zero:'none',pad:0},
    Name:{t0:'Categorical',t1:'Entity',v:['nominal'],aggRole:'dimension',domainShape:'open',diverging:'none',fmt:'plain',zero:'none',pad:0},
    Status:{t0:'Categorical',t1:'Coded',v:['nominal'],aggRole:'dimension',domainShape:'open',diverging:'none',fmt:'plain',zero:'none',pad:0},
    Region:{t0:'Geographic',t1:'GeoPlace',v:['nominal'],aggRole:'dimension',domainShape:'open',diverging:'none',fmt:'plain',zero:'none',pad:0}
  };
  var FLINT_ALIAS={currency:'Amount',percent:'Percentage',count:'Count',duration:'Duration',number:'Quantity',unitless:'Number',price:'Price',cost:'Cost',Rating:'Score'};
  function flintGetType(name){return FLINT_TYPES[name];}
  function flintCanonicalAlias(name){return FLINT_ALIAS[name]||name;}
  function flintInferVisCategory(values){
    if(!values||!values.length)return 'nominal';
    var nonNull=values.filter(function(v){return v!=null&&v!==''&&v===v;});
    if(!nonNull.length)return 'nominal';
    var isBool=nonNull.every(function(v){return v===true||v===false||String(v).toLowerCase()==='true'||String(v).toLowerCase()==='false';});
    if(isBool)return 'nominal';
    var isNum=nonNull.every(function(v){return v!==true&&v!==false&&!isNaN(Number(v));});
    if(isNum)return 'quantitative';
    var dateLike=nonNull.every(function(v){return /^\d|^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)/i.test(String(v).trim())&&!isNaN(Date.parse(String(v).trim()));});
    if(dateLike)return 'temporal';
    return 'nominal';
  }
  function flintEncoding(name){var t=FLINT_TYPES[flintCanonicalAlias(name)];return (t&&t.v)||['nominal'];}
  function flintZeroBaseline(name){var t=FLINT_TYPES[flintCanonicalAlias(name)];return (t&&t.zero)||'none';}
  function flintZeroPad(name){var t=FLINT_TYPES[flintCanonicalAlias(name)];return (t&&t.pad)||0;}
  function flintAdditive(name){var t=FLINT_TYPES[flintCanonicalAlias(name)];return !!(t&&(t.aggRole==='additive'||t.aggRole==='signed-additive'));}
  function flintFieldSemantics(name,values){
    var t=FLINT_TYPES[flintCanonicalAlias(name)];
    if(t){return {type:flintCanonicalAlias(name),encoding:t.v[0],additive:flintAdditive(name),aggregationDefault:t.aggRole==='additive'||t.aggRole==='signed-additive'?'sum':t.aggRole==='intensive'?'average':'none',zeroBaseline:t.zero,domainShape:t.domainShape,sortDirection:t.v[0]==='ordinal'||t.v[0]==='temporal'?'asc':'desc'};}
    var vis=flintInferVisCategory(values);
    return {type:name,encoding:vis,additive:vis==='quantitative',aggregationDefault:vis==='quantitative'?'sum':'none',zeroBaseline:vis==='quantitative'?'meaningful':'none',domainShape:'open',sortDirection:vis==='temporal'?'asc':'desc'};
  }
  function flintBuildProfile(fields){
    var p={measures:[],temporals:[],categoricals:[],ordinals:[],dims:[],cardinality:{}};
    (fields||[]).forEach(function(f){
      var values=f.values||[];var card=0,seen={};
      values.forEach(function(v){if(v!=null&&v!==''){var k=String(v);if(!seen[k]){seen[k]=1;card++;}}});
      p.cardinality[f.name]=card;
      var t=FLINT_TYPES[flintCanonicalAlias(f.semLabel||'')];
      if(t&&(t.aggRole==='additive'||t.aggRole==='signed-additive')){p.measures.push(f);}
      else if(t&&t.v[0]==='temporal'){p.temporals.push(f);}
      else if(t&&t.v[0]==='ordinal'){p.ordinals.push(f);}
      else if(t&&(t.t0==='Categorical'||t.t0==='Geographic')){p.categoricals.push(f);}
      else{var vis=flintInferVisCategory(values);if(vis==='temporal')p.temporals.push(f);else if(vis==='quantitative')p.measures.push(f);else p.categoricals.push(f);}
    });
    p.dims=p.categoricals.concat(p.ordinals,p.temporals);
    return p;
  }
  // ---- 维度层级 (hierarchy) --------------------------------------------
  // dataset.hierarchy = list of edges [{parent, child}]. Nodes are category/
  // ordinal dimensions. Constraints: a node has <=1 parent, acyclic. Built from
  // edges so it maps 1:1 to the canvas configurator (one edge = one connection).
  function hierarchyEdges(data){return (data&&data.hierarchy)||[];}
  function hierarchyIndex(data){
    var edges=hierarchyEdges(data);
    var childrenOf={},parentOf={},nodes=[],seen={};
    edges.forEach(function(e){
      if(!e||!e.parent||!e.child||e.parent===e.child)return;
      (childrenOf[e.parent]=childrenOf[e.parent]||[]).push(e.child);
      parentOf[e.child]=e.parent;
      [e.parent,e.child].forEach(function(n){if(!seen[n]){seen[n]=1;nodes.push(n);}});
    });
    var roots=nodes.filter(function(n){return !parentOf[n];});
    var depth={};
    function walk(n,d){if(depth[n]!=null)return;depth[n]=d;(childrenOf[n]||[]).forEach(function(c){if(depth[c]==null)walk(c,d+1);});}
    roots.forEach(function(r){walk(r,0);});
    return {childrenOf:childrenOf,parentOf:parentOf,roots:roots,depth:depth};
  }
  function hierarchyChildren(data,parent){return hierarchyIndex(data).childrenOf[parent]||[];}
  function hierarchyParent(data,child){return hierarchyIndex(data).parentOf[child]||null;}
  function hierarchyRoots(data){return hierarchyIndex(data).roots.slice();}
  function hierarchyLevel(data,node){return hierarchyIndex(data).depth[node]!=null?hierarchyIndex(data).depth[node]:0;}
  function hierarchyPath(data,node){var p=[];var cur=node;guard:while(cur){p.unshift(cur);cur=hierarchyParent(data,cur);if(p.length>100)break guard;}return p;}
  // Ordered drill levels for a starting category: the node's own subtree down to
  // the deepest descendant. Used to build the group header -> sub-row chain.
  function hierarchyChain(data,node,seen){
    seen=seen||{};var chain=[];
    function walk(n){if(chain.indexOf(n)>=0)return;chain.push(n);(hierarchyChildren(data,n)||[]).forEach(walk);}
    walk(node);
    return chain;
  }
  function hierarchyAsTree(data){
    var idx=hierarchyIndex(data);
    function node(n){return {name:n,children:(idx.childrenOf[n]||[]).map(node)};}
    return idx.roots.map(node);
  }
  function classifyField(data,col){return fieldSemantic(data,col);}
  // Validate a candidate edge set against the dataset's category/ordinal dims.
  // Returns {ok, errors:[...]}.
  function validateHierarchy(data,edges){
    var errors=[];var all=dimensionColumns(data);
    var cat={};all.forEach(function(c){cat[c]=true;});
    var parentOf={},parentName={};
    var known={};
    edges.forEach(function(e){if(e&&e.parent&&e.child){known[e.parent]=1;known[e.child]=1;}});
    Object.keys(known).forEach(function(n){if(!cat[n])errors.push('「'+n+'」不是维度，不能加入层级');});
    edges.forEach(function(e){
      if(!e||!e.parent||!e.child){errors.push('存在不完整的层级关系');return;}
      if(e.parent===e.child){errors.push('「'+e.parent+'」不能作为自己的子级');return;}
    });
    // <=1 parent per node
    var pmap={};
    edges.forEach(function(e){if(!e||!e.child)return;if(pmap[e.child]&&pmap[e.child]!==e.parent){errors.push('「'+e.child+'」已有父级「'+pmap[e.child]+'」，不能再设父级「'+e.parent+'」');}pmap[e.child]=e.parent;});
    // acyclic (Kahn's topological sort: a cycle leaves some node unvisited)
    function childrenOfEdges(es,n){var out=[];es.forEach(function(e){if(e&&e.parent===n)out.push(e.child);});return out;}
    function hasCycle(){
      var inDeg={};Object.keys(known).forEach(function(n){inDeg[n]=0;});
      edges.forEach(function(e){if(e&&e.child)inDeg[e.child]++;});
      var q=Object.keys(known).filter(function(n){return inDeg[n]===0;}),count=0;
      while(q.length){var n=q.shift();count++;(childrenOfEdges(edges,n)).forEach(function(c){inDeg[c]--;if(inDeg[c]===0)q.push(c);});}
      return count<Object.keys(known).length;
    }
    if(Object.keys(known).length&&hasCycle())errors.push('层级关系存在环，请调整');
    return {ok:errors.length===0,errors:errors};
  }
  function recommendChartTypes(fields){
    var p=flintBuildProfile(fields||[]),out=[];
    var measures=p.measures,catLike=p.categoricals.concat(p.ordinals),card=p.cardinality;
    function add(t,s,r){out.push({chartType:t,score:s,reason:r});}
    var lowCat=catLike.filter(function(f){var c=card[f.name]||0;return c>=2&&c<=8;});
    var hasMeasure=measures.length>=1;
    if(p.temporals.length>=1&&hasMeasure)add('Line Chart',88,'has a time field + a measure (trend over time)');
    if(measures.length>=2)add('Scatter Plot',84,'two quantitative measures (x/y relationship)');
    if(catLike.length>=1&&hasMeasure)add('Bar Chart',80,'has a category axis + a measure');
    if(catLike.length>=2&&lowCat.length>=1&&hasMeasure){add('Grouped Bar Chart',72,'has two categories + a measure');add('Stacked Bar Chart',70,'has two categories + a measure');}
    if(catLike.length===1&&!p.temporals.length&&hasMeasure){var cc=card[catLike[0].name]||0;if(cc>=2&&cc<=8)add('Pie Chart',66,'one category with a small number of slices + a measure');}
    if(catLike.length>=1&&hasMeasure&&!p.temporals.length)add('Grouped Bar Chart',62,'categories + a measure');
    if(!hasMeasure){add('Bar Chart',60,'categories without a measure (counts)');}
    if(measures.length>=2)add('Scatter Plot',20,'fallback for numeric data');else add('Bar Chart',15,'fallback');
    out.sort(function(a,b){return b.score-a.score;});
    return out;
  }
  global.AIReportChartAdapter={clone:clone,chartCatalog:chartCatalog,measureTypeKeys:MEASURE_TYPE_KEYS,derivedPercentUnits:derivedPercentUnits,setColumnAliases:setColumnAliases,aliasOf:aliasOf,setValueAliases:setValueAliases,valueAliasOf:valueAliasOf,applyValueAliases:applyValueAliases,measureTypeFormats:MEASURE_TYPE_FORMATS,currencySymbols:CURRENCY_SYMBOLS,currencyPrefixOf:currencyPrefixOf,currencySymbolOf:currencySymbolOf,currencySymbolFor:currencySymbolFor,measureFormat:measureFormat,resolveMeasureType:resolveMeasureType,normalizeMeasureType:normalizeMeasureType,fieldSemantic:fieldSemantic,channelAction:channelAction,applyMeasureScale:applyMeasureScale,applyMeasureFormats:applyMeasureFormats,chartDecimals:chartDecimals,isMatrix:isMatrix,matrixAgg:matrixAgg,roundNumber:roundNumber,capEntries:capEntries,dimensionKind:dimensionKind,bucketRange:bucketRange,timeDrillChild:timeDrillChild,valueGrain:valueGrain,TIME_DRILL_CHAIN:TIME_DRILL_CHAIN,nonAdditiveMeasure:nonAdditiveMeasure,anyNonAdditive:anyNonAdditive,colNonAdditiveForMerge:colNonAdditiveForMerge,applyReferences:applyReferences,applyDerivedMeasures:applyDerivedMeasures,derivedEvaluate:derivedEvaluate,derivedClosureNodes:derivedClosureNodes,derivedTimeKey:derivedTimeKey,derivedAggKey:derivedAggKey,derivedDepKey:derivedDepKey,derivedFmKey:derivedFmKey,derivedRequiredAggregations:derivedRequiredAggregations,applyCondFormat:applyCondFormat,condSlotColor:condSlotColor,condFormatPlanOf:condFormatPlanOf,patchSemantics:patchSemantics,inferEditorData:inferEditorData,numericColumns:numericColumns,dimensionColumns:dimensionColumns,rawDateSource:rawDateSource,availableTypes:availableTypes,shapeRules:shapeRules,aggregate:aggregate,optionFor:optionFor,patchFlags:patchFlags,filteredRows:filteredRows,fetchDetailRows:fetchDetailRows,fetchGlobalAggregates:fetchGlobalAggregates,dateParts:dateParts,fetchColumnValues:fetchColumnValues,fetchFilteredValues:fetchFilteredValues,fetchSampleValues:fetchSampleValues,aggregateTable:aggregateTable,setChartTheme:setChartTheme,themeCatalog:themeCatalog,chartThemeInk:chartThemeInk,defaultPalette:defaultPalette,chartThemeSlice:chartThemeSlice,applyChartTheme:applyChartTheme,flintGetType:flintGetType,flintCanonicalAlias:flintCanonicalAlias,flintInferVisCategory:flintInferVisCategory,flintEncoding:flintEncoding,flintZeroBaseline:flintZeroBaseline,flintZeroPad:flintZeroPad,flintAdditive:flintAdditive,flintFieldSemantics:flintFieldSemantics,recommendChartTypes:recommendChartTypes,flintZeroDecision:flintZeroDecision,flintPaddedDomain:flintPaddedDomain,hierarchyEdges:hierarchyEdges,hierarchyIndex:hierarchyIndex,hierarchyChildren:hierarchyChildren,hierarchyParent:hierarchyParent,hierarchyRoots:hierarchyRoots,hierarchyLevel:hierarchyLevel,hierarchyPath:hierarchyPath,hierarchyChain:hierarchyChain,hierarchyAsTree:hierarchyAsTree,validateHierarchy:validateHierarchy};
})(window);
