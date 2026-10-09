/* column-matcher.js — deterministic column mapping for re-importing a DIFFERENT
 * schema table into an existing report dataset (offline, no Agent / no LLM).
 *
 * Contract (docs/column-mapping-import-design.md):
 *   1. normalize the name (full-width→half, drop space/_/-/()/, lowercase) — exact
 *      equality scores 50.
 *   2. built-in synonym families (金额类/数量类/日期类/分类类/… keyed by target
 *      column NAME substrings and incoming names) score 40.
 *   3. type compatibility between the target column's expected role and the
 *      incoming column's measured type scores 10.
 *   4. conflicts (two target columns claiming the same incoming column) resolve
 *      by best score, losers fall back to their next best option.
 *
 * The file only reads/writes plain data and assigns global.AIReportColumnMatch so
 * it can be injected ahead of editor-runtime.js and unit-tested under node.
 */
(function (root) {
  'use strict';

  // ---------- name normalization ----------
  function fullWidthToHalf(s) {
    return String(s == null ? '' : s)
      .replace(/[\uFF01-\uFF5E]/g, function (ch) { return String.fromCharCode(ch.charCodeAt(0) - 0xFEE0); })
      .replace(/\u3000/g, ' ')
      .replace(/（/g, '(').replace(/）/g, ')');
  }
  function normalizeName(name) {
    return fullWidthToHalf(name)
      .toLowerCase()
      .replace(/[_\-\s（）()\[\]【】./\\:：]/g, '')
      .trim();
  }

  // ---------- synonym families ----------
  // Each family matches an incoming name when any token of the family appears
  // inside the normalized incoming name, AND the target side shares at least one
  // family (so "金额" ↔ "sales" both belong to the money family).
  var FAMILIES = {
    money: ['金额', '销售额', '销售', '收入', '营收', '流水', '价', '额', 'gmv', 'sales', 'revenue', 'amount', 'price', 'amt', 'money', 'turnover'],
    count: ['订单数', '数量', '件数', '个数', '笔数', '量', '数', 'count', 'qty', 'quantity', 'orders', 'volume', 'num'],
    date: ['日期', '时间', '月份', '年月', '日', '月', 'date', 'time', 'month', 'day', 'year', 'dt', 'ym'],
    category: ['分类', '品类', '类别', '类型', '产品', '商品', '地区', '区域', '渠道', '城市', '省份', '客户', '用户', '类目',
               'category', 'cat', 'product', 'sku', 'region', 'city', 'province', 'channel', 'brand', 'name', 'type'],
    rate: ['率', '占比', '比例', 'ratio', 'rate', 'percent', 'pct'],
    id: ['编号', '代码', '订单号', 'skuid', 'id', 'code', 'no', 'num_'],
  };
  function familyTokens(name) {
    var tokens = [];
    for (var key in FAMILIES) {
      if (!Object.prototype.hasOwnProperty.call(FAMILIES, key)) continue;
      for (var i = 0; i < FAMILIES[key].length; i++) {
        if (name.indexOf(FAMILIES[key][i]) >= 0) { tokens.push(key); break; }
      }
    }
    return tokens;
  }
  function familyScore(targetName, incomingName) {
    var a = familyTokens(targetName), b = familyTokens(incomingName);
    for (var i = 0; i < a.length; i++) {
      if (b.indexOf(a[i]) >= 0) return 40;
    }
    // 强 token（money/count/date/rate/id）单侧命中即可加分——e.g. 目标「金额」vs 来列「gmv_total」
    var strong = { money: 1, count: 1, date: 1, rate: 1, id: 1 };
    for (var j = 0; j < b.length; j++) {
      if (strong[b[j]] && !a.length) return 40;
    }
    return 0;
  }

  // ---------- type detection / compatibility ----------
  // measure/dimension/time are the semantic families a target column expects.
  var TYPE_CODES = { number: 1, date: 2, text: 3 };
  function detectColumnType(values) {
    // returns 'number' | 'date' | 'text' from up to 200 sampled values.
    var n = 0, total = 0, numOk = 0, dateOk = 0, seen = {};
    var DATE_RE = /^\d{4}[-/年.]\d{1,2}([-/月.]\d{1,2})?$|^\d{4}年\d{1,2}月?$/;
    for (var i = 0; i < values.length && i < 200; i++) {
      var v = values[i];
      if (v === null || v === undefined || v === '') continue;
      total++;
      if (typeof v === 'number') { numOk++; }
      else {
        var s = String(v).trim();
        if (s !== '' && !isNaN(Number(s))) numOk++;
        if (DATE_RE.test(s) || !isNaN(Date.parse(s))) dateOk++;
      }
      var key = typeof v === 'number' ? '#num' : String(v);
      seen[key] = true;
    }
    var distinct = Object.keys(seen).length;
    if (!total) return 'text';
    if (numOk / total >= 0.9) return 'number';
    if (dateOk / total >= 0.8 && total >= 3) return 'date';
    return 'text';
  }

  function typeCompatible(targetExpect, incomingType) {
    // targetExpect ∈ {'number','date','text','time','any'}
    if (targetExpect === 'any' || targetExpect === incomingType) return 10;
    if (targetExpect === 'time') return (incomingType === 'date' || incomingType === 'text') ? 10 : 0;
    if (targetExpect === 'number' && incomingType === 'number') return 10;
    // target expects a number but the incoming column is numeric-looking text —
    // detectColumnType already folds numeric strings into 'number'.
    return 0;
  }

  // ---------- scoring + conflict resolution ----------
  function scorePair(target, incoming) {
    var score = 0;
    var nT = normalizeName(target.name), nI = normalizeName(incoming.name);
    if (nT && nT === nI) score += 50;
    else score += familyScore(nT, nI);
    score += typeCompatible(target.expect || 'any', incoming.type || 'any');
    return score;
  }

  // Best-first assignment with conflict backoff: greedy bipartite assignment on
  // the pair matrix — pick the global max (target, incoming) pair, remove both
  // sides, repeat. A target that lost its best candidate simply falls to its
  // next best. (两个早死分支已删：per-target 预选表与 assigned 收集器从未被读。)
  function resolve(mapping, targetList, incomingList, scores) {
    var result = {};          // targetName -> incomingName
    var byTarget = {};
    targetList.forEach(function (t) { byTarget[t.name] = 1; });
    var pending = {};
    incomingList.forEach(function (inc) { pending[inc.name] = 1; });
    while (true) {
      var best = null;
      Object.keys(byTarget).forEach(function (t) {
        if (result[t] !== undefined) return;
        Object.keys(pending).forEach(function (i) {
          var s = scores[t][i];
          if (!best || s > best.score) best = { target: t, inc: i, score: s };
        });
      });
      if (!best || best.score <= 0) break;
      result[best.target] = best.inc;
      delete byTarget[best.target];
      delete pending[best.inc];
    }
    return result;
  }

  function match(spec) {
    var targetList = (spec.targets || []).map(function (t) {
      return { name: String(t.name), expect: t.expect || 'any' };
    });
    var incomingList = (spec.incoming || []).map(function (c) {
      return { name: String(c.name), type: c.type || 'any' };
    });
    var scores = {};
    targetList.forEach(function (t) {
      scores[t.name] = {};
      incomingList.forEach(function (inc) {
        scores[t.name][inc.name] = scorePair(t, inc);
      });
    });
    var mapping = resolve(scores, targetList, incomingList, scores);
    var missing = targetList.filter(function (t) { return mapping[t.name] === undefined; }).map(function (t) { return t.name; });
    return { mapping: mapping, missing: missing, scores: scores };
  }

  var api = {
    normalizeName: normalizeName,
    familyTokens: familyTokens,
    detectColumnType: detectColumnType,
    typeCompatible: typeCompatible,
    scorePair: scorePair,
    match: match,
  };
  if (typeof module !== 'undefined' && module.exports) { module.exports = api; }
  root.AIReportColumnMatch = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
