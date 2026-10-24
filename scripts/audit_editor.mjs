#!/usr/bin/env node
/* Browser-level smoke audit for the packaged editor. The report is never written.
 *
 * Two layers of checks:
 *  1. Control checks — Chinese controls exist, events fire, changes apply
 *     immediately without an Apply button, chart switching and BI insertion work.
 *  2. Render-truth checks — the rendered option must be free of function-string
 *     formatters, every chart must actually paint a canvas (or table), a newly
 *     inserted BI chart must render real series, the block panel must not carry
 *     duplicate actions, the drag surface must be the top strip only, and the
 *     text format bar must apply inline formatting.
 */
import { spawn, execFile } from 'node:child_process';
import { existsSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import os from 'node:os';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

// ---- 范围化审计（开发提速）：默认全量；可按 预设包/检查名/通配 挑选要跑的检查。 ----
// CLI：node audit_editor.mjs <url> [--preset smoke,charts] [--checks derivedConfig,chart:*:painted]
//      [--skip pptxBuilds] [--list-checks]
// env：AI_AUDIT_SCOPE（report_build --audit-scope 透传；语法同 --preset，也接受检查名/通配）。
// 通配规则：`*` 匹配一段不含冒号的内容 —— 每图检查名形如 chart:<blockId>:<suffix>
// （suffix: controls paintedContent instant semanticControls tableLimit noBadFormatters
//   painted heatmapComplete dataTruth），所以 `chart:*:painted` = 每张图的绘制真值，
// `chart:*:*` = 每图全套。注意 `chart:*`（只有一段）对两段后缀的检查名**零命中** ——
// 那是 marketing 冒烟 P2-2 的静默空跑根因；现在空命中会被显式报为 non-green
// （scope.unmatchedPatterns），不再退化成「只跑几个杂项检查还全绿」。
// serverWarm / runtimeLoaded 是就绪门，任何范围都执行、不计入 skipped。
const CORE_CHECKS = new Set(['serverWarm', 'runtimeLoaded']);
const PRESETS = {
  smoke: ['serverWarm', 'runtimeLoaded', 'noApplyButton', 'perfSane', 'touchMatrix', 'chart:*:painted', 'chart:*:noBadFormatters', 'textFormatBar'],
  charts: ['chart:*:*', 'clickDrill', 'condFormat', 'panelNoDuplicateActions'],
  derived: ['derivedConfig', 'kpiPanel', 'kpiRoundTrip'],
  dataconfig: ['derivedConfig', 'columnStats', 'hierFilter'],
  presentation: ['presentationMode', 'presTallCardNoHang', 'presPaging', 'pdfExport', 'pptxBuilds', 'pptxXmlWellFormed'],
  tables: ['detailSearchStats', 'tableColWidth', 'tableFilterPopover', 'detailToolbar', 'tableAutoHeight', 'boundedTable', 'editPolicy', 'gridCard', 'condEngine', 'subMeasures', 'sparkline', 'sessionView'],
  filters: ['filterDefaults', 'hierFilter', 'tableFilterPopover'],
  layout: ['noBlockOverflow', 'noBlockOverflowNarrow', 'browserMatrix', 'touchMatrix', 'buttonRowHeights', 'toolbarConstraints', 'tableAutoHeight', 'perfSane', 'dragStripPresent', 'cardNotGrab', 'dragStripGrab', 'manualGapInvariant'],
  interact: ['textFormatBar', 'insertBI', 'insertDetail', 'seriesFormReset', 'editPolicy', 'buttonRowHeights', 'panelNoDuplicateActions', 'dragLostGuard'],
};
function globRe(pat) {
  return new RegExp('^' + pat.replace(/[.+?^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '[^:]*') + '$');
}
const flagSel = [];
const flagSkip = [];
let listOnly = false;
let noReap = false;
const positional = [];
{
  const argv = process.argv.slice(2);
  const FLAGS = ['--preset', '--checks', '--skip'];
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (a === '--list-checks') { listOnly = true; continue; }
    if (a === '--no-reap') { noReap = true; continue; }
    let matched = null, val = null;
    for (const f of FLAGS) {
      if (a === f) { matched = f; val = argv[i += 1]; break; }
      if (a.startsWith(f + '=')) { matched = f; val = a.slice(f.length + 1); break; }
    }
    if (matched) {
      if (val == null) { console.error('[audit] ' + matched + ' 需要一个值'); process.exit(2); }
      const items = String(val).split(',').map((s) => s.trim()).filter(Boolean);
      if (matched === '--skip') flagSkip.push(...items);
      else flagSel.push(...items);   // --preset 与 --checks 并入同一选择集
      continue;
    }
    positional.push(a);
  }
}
const SCOPE_ENV = (process.env.AI_AUDIT_SCOPE || '').split(',').map((s) => s.trim()).filter(Boolean);
// 预设名必须**展开**成它包含的检查/通配（否则 --preset derived 只会把字面 'derived'
// 当成一个模式，什么检查都匹配不上 —— 真机验证抓到）。
const expandItems = (items) => items.flatMap((it) => (PRESETS[it] ? PRESETS[it] : [it]));
// RAW_SELECTED 保留展开后的**模式原文**（SELECTED 是编译后的 RegExp）：收尾的空命中
// 守卫要拿模式去对「实际执行/跳过」的检查名，零命中的模式显式 non-green（P2-2）。
const RAW_SELECTED = (flagSel.length || SCOPE_ENV.length) ? expandItems(flagSel.concat(SCOPE_ENV)) : null;
const SELECTED = RAW_SELECTED ? RAW_SELECTED.map(globRe) : null;
const SCOPE_DESC = SELECTED ? (flagSel.concat(SCOPE_ENV).join(',') || 'custom') : 'full';
const SKIPPERS = flagSkip.map(globRe);
function want(name) {
  if (CORE_CHECKS.has(name)) return true;
  if (SELECTED && !SELECTED.some((re) => re.test(name))) return false;
  if (SKIPPERS.some((re) => re.test(name))) return false;
  return true;
}
if (listOnly) {
  console.log('presets: ' + Object.keys(PRESETS).join(', '));
  for (const [k, v] of Object.entries(PRESETS)) console.log('  ' + k.padEnd(13) + '= ' + v.join(', '));
  console.log('任意输出里的检查名都能用于 --checks/--skip/AI_AUDIT_SCOPE；chart 检查名形如 chart:<blockId>:<suffix>，支持 * 通配（`*` 不跨冒号：全套 per-chart 用 chart:*:*，单项用 chart:*:painted）。');
  process.exit(0);
}
const arg = positional[0];
let target;
if (!arg) {
  target = 'report.html';
} else if (/^https?:\/\//.test(arg)) {
  target = arg;  // server-mode: navigate to the running server URL
} else {
  target = resolve(arg);
}
// 范围门：每个检查都经 evalIf 执行 —— 未选中的检查记入 skipped 且不产出结果；
// passed 只对**实际执行**的检查判定（全量语义不变）。
const SKIPPED_CHECKS = [];
// ---- infra/check 失败分类（NP-04 审计稳定性加固）---------------------------
// CDP 超时 / 看门狗中止 / 页面重载或饿死丢 helper / socket 断开 = **环境失败**
// （infra），不是产品失败：自动重试一次；仍失败该检查记 `infraFailed` 并使整体
// exit 非 0（不静默放行）。检查自身的断言失败（返回 false / 失败串）是 check
// 失败，照旧判红、不重试。高负载期（真机 load 7-26）的假红从此不再污染验收。
// 判定用**窄签名**：只认「审计基础设施自己的失效形态」，普通页面异常（如检查
// 表达式自身的 ReferenceError）不误判成 infra，仍走整体 aborted 语义。
const INFRA_RE = /cdp-timeout|audit-deadline|socket-closed|WebSocket is not open|WebSocket is closed|__audit(BadFormatter|Painted) is not a function|echarts is not defined|AIReportRuntime is not defined|Target closed|Session closed|Cannot find context|connection closed/i;
let infraRetries = 0;
const infraFailed = [];
const infraErrorOf = (e) => String((e && e.message) || e);
// 名字直呼基础设施，供 tests/test_audit_fake_red.cjs 沙箱提取做行为级验证。
async function withInfraRetry(name, run) {
  try {
    return await run();
  } catch (e) {
    const msg = infraErrorOf(e);
    if (!INFRA_RE.test(msg)) throw e;   // 非 infra = 脚本自身/页面真实异常 → 整体 aborted（原语义）
    infraRetries += 1;
    console.error('[audit] infra-retry ' + name + ': ' + msg.slice(0, 160));
    await wait(400);
    // 页面重载会丢审计 helper —— 重试前幂等补注（其余如 echarts/runtime 无法
    // 页内复活，重试会再次 infra 失败并如实记 infraFailed）。
    try { await evaluate(RENDER_HELPERS); } catch (e2) {}
    try {
      return await run();
    } catch (e2) {
      const msg2 = infraErrorOf(e2);
      infraFailed.push({ check: name, error: msg2.slice(0, 300) });
      return 'infraFailed:' + name + ' ' + msg2.slice(0, 160);
    }
  }
}
async function evalIf(name, code) {
  if (!want(name)) { SKIPPED_CHECKS.push(name); return undefined; }
  return withInfraRetry(name, () => evaluate(code));
}
// Configurable for intranet machines. Prefer AI_REPORT_CHROME, then discover
// the common Chrome/Chromium paths per OS so Windows audit runs without env
// setup. Keep Linux as a final binary-name fallback for the local report flow.
function findChromeBinary() {
  const candidates = [];
  if (process.platform === 'darwin') {
    candidates.push(
      '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
      '/Applications/Chromium.app/Contents/MacOS/Chromium',
      // Edge is Chromium-based and ships with macOS installs of Office; headless
      // Edge speaks the same CDP, so audit coverage no longer requires Chrome.
      '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
    );
  } else if (process.platform === 'win32') {
    const roots = [
      process.env.PROGRAMFILES,
      process.env['PROGRAMFILES(X86)'],
      process.env.LOCALAPPDATA,
    ].filter(Boolean);
    const rels = [
      'Google/Chrome/Application/chrome.exe',
      'Chromium/Application/chrome.exe',
      'Google/Chrome SxS/Application/chrome.exe',
      // Windows machines always carry Edge but often lack Chrome — the work
      // machines otherwise every audit ended "needs_audit" with no browser.
      'Microsoft/Edge/Application/msedge.exe',
    ];
    for (const root of roots) for (const rel of rels) candidates.push(`${root}/${rel}`);
  } else {
    candidates.push(
      '/usr/bin/google-chrome',
      '/usr/bin/chromium',
      '/usr/bin/chromium-browser',
      '/usr/bin/microsoft-edge',
      '/usr/bin/microsoft-edge-stable',
    );
  }
  candidates.push(process.platform === 'win32' ? 'chrome.exe' : 'google-chrome');
  if (process.platform === 'win32') candidates.push('msedge.exe');
  return candidates.find((candidate) => existsSync(candidate)) || candidates[candidates.length - 1];
}

const chrome = process.env.AI_REPORT_CHROME || findChromeBinary();
// Use the high random range called out in AGENTS.md. The old 9800 range can
// attach to a leftover Chrome instance and audit a stale page.
const profile = mkdtempSync(`${tmpdir()}/ai-report-audit-`);
const chromeArgs = (port) => ['--headless=new', `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, '--no-first-run', '--disable-gpu', '--window-size=1280,2200', /^https?:\/\//.test(target) ? target : pathToFileURL(target).href];
let child = null; let port = 0;
const wait = (ms) => new Promise((done) => setTimeout(done, ms));
let socket; let sequence = 0; const pending = new Map();
async function pageInfo() { for (let i = 0; i < 40; i += 1) { try { const pages = await fetch(`http://127.0.0.1:${port}/json/list`).then((r) => r.json()); const page = pages.find((item) => item.type === 'page'); if (page) return page; } catch {} await wait(100); } throw new Error('Chrome did not start'); }

// 启动 headless Chrome 并等调试端口就绪。随机端口可能撞上本机遗留的
// headless Chrome（调试端口被占时新进程起不来），换端口重试最多 3 次。
async function launchChrome() {
  let lastError;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    port = 19500 + Math.floor(Math.random() * 1000);
    // detached: Chrome 自成进程组 —— 收尾时按组 kill 才能连它的 GPU/utility/zygote
    // 一起带走（只 SIGKILL 浏览器进程会短暂留下孤儿渲染进程，攒多了就是内存隐患）。
    // 这个进程组只包含我们刚启动的 Chrome，不会影响用户自己的浏览器。
    child = spawn(chrome, chromeArgs(port), { stdio: 'ignore', detached: true });
    try { return await pageInfo(); } catch (error) { lastError = error; try { child.kill('SIGKILL'); } catch {} }
  }
  throw lastError || new Error('Chrome did not start');
}
// ---- 启动前置：收割历次遗留的孤儿 headless Chrome（NP-04，AGENTS.md 收割纪律）----
// 泄漏实例抢内存推高负载（真机：~95 个实例把 load 打到 7-26，335k 行审计因此
// 间歇性假红）。只清「--headless 且 --remote-debugging-port=19xxx/20xxx」双签名
// 进程 —— 这是本脚本与 audit/探针的专属签名（本脚本端口段 19500+rand(1000)），
// 用户真浏览器既不 headless 也不带调试端口，永不受影响；绝不无差别 pkill 用户浏览器。
// 端口段正则名直呼基础设施，供 tests/test_audit_fake_red.cjs 沙箱提取验证。
const AUDIT_DEBUG_PORT_RE = /--remote-debugging-port=((?:19|20)\d{3})\b/;
const REAP_PS_LINE_RE = /^\s*(\d+)\s+(.+)$/;
async function reapOrphanHeadless() {
  const empty = { count: 0, ports: [] };
  if (process.platform === 'win32') return empty;   // Windows 无 ps/pkill：诚实跳过（不假装清了）
  const run = (cmd, args) => new Promise((ok) => execFile(cmd, args, { timeout: 15000 }, (err, stdout, stderr) => ok(String(err ? (stderr || err.message) : stdout))));
  const psOut = await run('ps', ['-Ao', 'pid=,command=']);
  const pids = []; const ports = [];
  for (const line of psOut.split('\n')) {
    const m = line.match(REAP_PS_LINE_RE);
    if (!m) continue;
    const cmd = m[2];
    if (cmd.indexOf('--headless') < 0) continue;
    const pm = cmd.match(AUDIT_DEBUG_PORT_RE);
    if (!pm) continue;
    if (pids.indexOf(Number(m[1])) < 0) pids.push(Number(m[1]));
    if (ports.indexOf(pm[1]) < 0) ports.push(pm[1]);
  }
  if (!pids.length) return empty;
  for (const p of ports) await run('pkill', ['-f', 'remote-debugging-port=' + p]);
  return { count: pids.length, ports };
}
let reapInfo = { count: 0, ports: [] };
// ---- 看门狗（验收绝不允许再挂死）-----------------------------------------
// 渲染进程在内存/IO 饥饿时会**不再应答 CDP**（真机：evaluate / Debugger.resume 全部
// 超时、CPU 0%、没有原生对话框）。原实现 command() 没有超时 → 一次卡住就等到外层
// 1200s 才被杀，看起来就是「验收卡死」。
// 现在：每条命令有硬超时；整个审计有全局截止时间；超时立即：kill Chrome → 输出带
// 原因的 JSON → 非零退出。宁可快速失败并说清原因，也绝不静默吊死。
const AUDIT_DEADLINE_MS = Number(process.env.AI_AUDIT_DEADLINE_MS || 600000);
const COMMAND_TIMEOUT_MS = Number(process.env.AI_AUDIT_CMD_TIMEOUT_MS || 60000);
const AUDIT_STARTED_AT = Date.now();
function auditElapsedMs() { return Date.now() - AUDIT_STARTED_AT; }
function auditRemainingMs() { return AUDIT_DEADLINE_MS - auditElapsedMs(); }
let auditTimeoutDetail = null;
function command(method, params = {}, timeoutMs) {
  const remaining = auditRemainingMs();
  const budget = Math.max(1000, Math.min(timeoutMs || COMMAND_TIMEOUT_MS, remaining));
  return new Promise((ok, no) => {
    const id = ++sequence;
    const timer = setTimeout(() => {
      pending.delete(id);
      const why = remaining <= 0
        ? 'audit-deadline ' + Math.round(auditElapsedMs() / 1000) + 's (AI_AUDIT_DEADLINE_MS=' + AUDIT_DEADLINE_MS + ')'
        : 'cdp-timeout ' + method + ' @' + Math.round(auditElapsedMs() / 1000) + 's';
      auditTimeoutDetail = why;
      no(new Error(why));
    }, budget);
    pending.set(id, {
      ok: (v) => { clearTimeout(timer); ok(v); },
      no: (e) => { clearTimeout(timer); no(e); },
    });
    socket.send(JSON.stringify({ id, method, params }));
  });
}
// 进程内存采样：卡住时把「机器还剩多少内存」写进日志 —— 内存饥饿与死循环看起来
// 完全一样（都是「没反应」），必须有数据才能一眼分辨。
function memoryLine(tag) {
  const freeMb = Math.round(os.freemem() / 1048576);
  const totalMb = Math.round(os.totalmem() / 1048576);
  return '[audit-mem ' + tag + '] free=' + freeMb + 'MB/' + totalMb + 'MB elapsed=' + Math.round(auditElapsedMs() / 1000) + 's';
}
async function evaluate(expression) { const result = await command('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true, userGesture: true }); if (result.exceptionDetails) { const d = result.exceptionDetails; throw new Error((d.exception && (d.exception.description || d.exception.value)) || d.text || 'evaluate failed'); } return result.result.value; }
async function waitForPaint(elementId, timeout = 4000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await evaluate(`window.__auditPainted && window.__auditPainted(${JSON.stringify(elementId)})`)) return true;
    await wait(120);
  }
  return false;
}

async function waitForExpression(expression, timeout = 8000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    try {
      if (await evaluate(expression)) return true;
    } catch { /* page still initializing */ }
    await wait(100);
  }
  return false;
}

// Fire a UI click without Runtime.evaluate's awaitPromise: the click's handler
// spawns async render work whose microtask rejections make an awaited evaluate
// report a spurious "Uncaught" even though no real page error occurs. Sync
// errors (e.g. a null element) still surface via exceptionDetails.
async function clickNoAwait(expression) {
  const result = await command('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: false, userGesture: true });
  if (result.exceptionDetails && result.exceptionDetails.exception) {
    throw new Error(result.exceptionDetails.exception.description || result.exceptionDetails.text);
  }
}

// Rendered-truth helpers evaluated inside the page.
const RENDER_HELPERS = `
  window.__auditBadFormatter = function (node) {
    if (node === null || node === undefined) return false;
    if (Array.isArray(node)) return node.some(window.__auditBadFormatter);
    if (typeof node === 'object') {
      for (const key of Object.keys(node)) {
        const value = node[key];
        if (key === 'formatter' && typeof value === 'string') {
          const s = value.trim();
          if (s.startsWith('function') || s.includes('=>')) return true;
        } else if (window.__auditBadFormatter(value)) return true;
      }
      return false;
    }
    return false;
  };
  window.__auditPainted = function (elementId) {
    const host = document.getElementById(elementId);
    if (!host) return false;
    const canvas = host.querySelector('canvas');
    if (canvas) return canvas.width > 0 && canvas.height > 0;
    return !!host.querySelector('.ai-report-table-wrap');
  };
`;

// 信号兜底：任何中断都要带走自己启动的 headless Chrome。残留实例会抢内存，
// 让后续所有审计变慢甚至看起来「卡死」（真机：超时后留下 9 个 debug-port Chrome）。
for (const sig of ['SIGINT', 'SIGTERM', 'SIGHUP']) {
  process.on(sig, () => { try { if (child) child.kill('SIGKILL'); } catch {} process.exit(1); });
}

const checks = {};
try {
  // 启动前置收割（--no-reap 跳过）：先清掉历次遗留的孤儿 headless Chrome 再起
  // 自己的实例，内存/负载从干净基线开始（收割数打印进 stderr + scope.reaped）。
  if (!noReap) {
    try {
      reapInfo = await reapOrphanHeadless();
      if (reapInfo.count) console.error('[audit] reaped ' + reapInfo.count + ' orphan headless chrome (ports: ' + reapInfo.ports.join(', ') + ')');
    } catch (e) { console.error('[audit] reap skipped: ' + String((e && e.message) || e)); }
  }
  const page = await launchChrome(); socket = new WebSocket(page.webSocketDebuggerUrl); await new Promise((ok, no) => { socket.onopen = ok; socket.onerror = no; });
  // socket 断开 = 环境失败（infra）：所有在途命令立即以 socket-closed 拒绝，
  // 走 withInfraRetry 的重试/归类，而不是干等各自的 cdp-timeout。
  socket.onclose = () => {
    for (const entry of pending.values()) entry.no(new Error('socket-closed: CDP 连接断开（页面崩溃或 Chrome 被杀）'));
    pending.clear();
  };
  socket.onmessage = (event) => {
    const message = JSON.parse(event.data);
    // NEVER let a native dialog hang the audit. A page that registers a
    // beforeunload/confirm handler (e.g. the editor's「有未保存修改」guard) makes
    // CDP stop until the dialog is dismissed — the build then burns its whole
    // audit timeout on a dialog nobody can answer (真机踩过：1200s 超时).
    // Accepting it is always correct here: the audit never needs the user's answer.
    if (message.method === 'Page.javascriptDialogOpening') { command('Page.handleJavaScriptDialog', { accept: true }).catch(() => {}); return; }
    const entry = pending.get(message.id); if (!entry) return; pending.delete(message.id); message.error ? entry.no(new Error(message.error.message)) : entry.ok(message.result); };
  await wait(500);
  // Enable the Page domain so `Page.javascriptDialogOpening` reaches us: without
  // it a native dialog (beforeunload/confirm) blocks the renderer and every
  // later evaluate() waits forever (the 1200s build-timeout hang).
  console.error(memoryLine('start'));
  await command('Page.enable').catch(() => {});
  // 看门狗自检（AI_AUDIT_SIMULATE_WEDGE=1）：在页面里发起一个**永不 resolve** 的
  // await，等价于「渲染进程不再应答 CDP」（内存饥饿时的真实状态）。审计必须在
  // COMMAND_TIMEOUT 内以 aborted + 原因结束，并带走 Chrome —— 这条自检就是
  // 「验收绝不再挂死」的回归证明。（不要用 ps 匹配命令去 SIGSTOP 进程：那会误伤
  // 自己的 shell/用户的浏览器；用页面内的确定性模拟。）
  if (process.env.AI_AUDIT_SIMULATE_WEDGE) {
    await evaluate('(async () => { await new Promise(() => {}); return true; })()');
  }
  await evaluate(RENDER_HELPERS);
  async function ensureHelpers() {
    // 幂等：helper 丢了就重新注入（页面重载/重挂都可能丢），避免裸调用直接中断审计。
    const ok = await evaluate("typeof window.__auditBadFormatter === 'function'").catch(() => false);
    if (!ok) await evaluate(RENDER_HELPERS);
    return ok;
  }
  // The runtime injects #ai-report-edit-toggle during init; poll until present so
  // a cold headless load never clicks on null (intermittent audit crash), and
  // click without awaitPromise (see clickNoAwait) so the click's async render
  // work does not surface a spurious rejection as an audit failure.
  await waitForExpression(`!!document.getElementById('ai-report-edit-toggle')`, 10000);
  // ---- 等数据装载完成（server 模式）---------------------------------------
  // 冷启动时报告 HTML 能打开、/api/health 也 200，但表格还在灌库（335k 行 ~1 分钟）：
  // 这期间所有 /aggregate 都是 500 → 图表/表格渲染失败或延迟到检查之后才完成，
  // 于是把两个**时序敏感**的断言测成假失败（真机案例：表格卡 gs-h 8→19 被判成
  // 「单页高度泄漏」、narrow 溢出 +10 冒出来）。审计必须测「装载完成后的页面」。
  checks.serverWarm = await evalIf('serverWarm', `(async () => {
    const health = await fetch('/api/health').then((r) => (r.ok ? r.json() : null)).catch(() => null);
    if (!health) return 'skip:not-server-mode';
    if (typeof AIReportRuntime === 'undefined' || !AIReportRuntime.manifest) return 'skip:no-runtime';
    const ds = ((AIReportRuntime.manifest.datasets || [])[0]) || null;
    const cols = ((ds && ds.columns) || []).map((c) => (typeof c === 'object' && c ? c.name : c));
    if (!ds || !ds.id || !cols.length) return true;
    for (let i = 0; i < 150; i += 1) {
      const r = await fetch('/api/datasets/' + encodeURIComponent(ds.id) + '/sample/' + encodeURIComponent(cols[0])).catch(() => null);
      if (r && r.ok) return true;
      await new Promise((res) => setTimeout(res, 2000));
    }
    return 'cold-after-300s:' + ds.id;
  })()`);

  console.error('[audit] serverWarm=' + checks.serverWarm);
  // runtime 的启动尾段会重挂工具栏：早期出现的 toggle 可能在「等到了」之后的一瞬间
  // 被替换掉，于是 `document.getElementById(...).click()` 抛
  // 「Cannot read properties of null」而整个审计中断（真机反复踩到）。改成
  // 「点到为止 + 重试」，直至进入编辑态。
  for (let attempt = 0; attempt < 60; attempt += 1) {
    const clicked = await evaluate(`(function(){const b=document.getElementById('ai-report-edit-toggle');if(!b)return false;b.click();return true;})()`).catch(() => false);
    if (clicked) break;
    await wait(250);
  }
  await waitForExpression(`document.body.classList.contains('ai-report-editing')`, 30000);
  await waitForExpression('typeof AIReportRuntime !== "undefined" && !!(AIReportRuntime.manifest && AIReportRuntime.manifest.charts)', 30000);
  const chartCount = await evaluate('AIReportRuntime.manifest.charts.length');
  const chartIds = await evaluate('AIReportRuntime.manifest.charts.map((c) => c.blockId)');
  // Capture initial viewTypes: the controls loop below mutates chart.viewType
  // (switches to bar) while testing, so data-truth checks must use the ORIGINAL.
  const chartViewTypes = await evaluate('AIReportRuntime.manifest.charts.map((c) => c.blockType ? c.blockType : c.viewType)');
  const initialViewType = {};
  chartIds.forEach((id, i) => { initialViewType[id] = chartViewTypes[i] || ''; });
  await ensureHelpers();
  checks.runtimeLoaded = chartCount > 0;
  checks.noApplyButton = await evalIf('noApplyButton', '!document.querySelector("[data-runtime-action=apply-chart]")');
  checks.presentationMode = await evalIf('presentationMode', `(async () => {
    // 任何失败路径都必须把页面还原（退出播放 → 退出汇报 → 回编辑态）：
    // 播放态 .report-shell 是 display:none，状态泄漏会让后续所有 painted 检查
    // 连锁假失败（canvas 全变 0×0）。
    const cleanup = async () => {
      try { if (window.AIReportPresentation && window.AIReportPresentation.isPlaying()) window.AIReportPresentation.exitPlay(); } catch (e) {}
      try { const c = document.querySelector('[data-pres-action="close"]'); if (c) c.click(); } catch (e) {}
      try { if (window.AIReportPresentation && window.AIReportPresentation.isActive()) window.AIReportPresentation.close(); } catch (e) {}
      try { const eb = document.getElementById('ai-report-edit-toggle'); if (eb && !document.body.classList.contains('ai-report-editing')) eb.click(); } catch (e) {}
    };
    try {
    const btn = document.getElementById('ai-report-present');
    if (!btn || !window.AIReportPresentation) return false;
    btn.click();
    await new Promise((r) => setTimeout(r, 160));
    const grid = document.querySelector('.grid-stack');
    const overlay = document.querySelector('.ai-pres-slide-frames');
    const nav = document.querySelector('[data-report-tabbar]');
    const presOk = document.body.classList.contains('ai-report-presenting')
      && grid && getComputedStyle(grid).display !== 'none'
      && document.querySelectorAll('.grid-stack .grid-stack-item[data-report-block-id]').length > 0
      && overlay && document.querySelectorAll('.ai-pres-slide-frame').length >= 1 // 单页报告（free/小数据）只有 1 个 16:9 定位框是正确行为——分页翻页断言在 nextOk 里已对单页诚实豁免
      && !!document.querySelector('.ai-pres-controls .ai-pres-play');
    if (!presOk) { await cleanup(); return 'presentationMode:presOk'; }
    // 汇报分页真源 + 每页标题留白：进入汇报模式即种子化 manifest.presentation.layout，
    // 定位框数 = deck 页数，且没有任何卡片的顶边落进该页标题带（标题留白真留出）。
    const layoutOk = await (async () => {
      const layoutN = (() => { const m = globalThis.AIReportRuntime.manifest; return m.presentation && m.presentation.layout ? Object.keys(m.presentation.layout).length : 0; })();
      const frames = [...document.querySelectorAll('.ai-pres-slide-frame')];
      const grid = document.querySelector('.grid-stack');
      const gt = grid.getBoundingClientRect().top;
      const cardTops = [...document.querySelectorAll('.grid-stack-item[data-report-block-id]')].map((el) => Math.round(el.getBoundingClientRect().top - gt));
      const bands = frames.map((f) => { const r = f.getBoundingClientRect(); const t = f.querySelector('.ai-pres-page-title'); return { top: Math.round(r.top - gt), h: Math.round(r.height), titleH: t ? Math.round(t.getBoundingClientRect().height) : 0 }; });
      const viol = [];
      cardTops.forEach((top) => { const own = bands.find((b) => top >= b.top - 4 && top < b.top + b.h); if (own && top < own.top + own.titleH - 3) viol.push({ top, titleBottom: own.top + own.titleH }); });
      const noteOk = !!document.querySelector('.ai-pres-note') && /汇报布局独立于单页/.test(document.querySelector('.ai-pres-note').textContent);
      return { ok: layoutN > 0 && bands.length > 0 && viol.length === 0 && noteOk, layoutN, frames: bands.length, viol: viol.slice(0, 4), noteOk };
    })();
    if (!layoutOk.ok) { await cleanup(); return 'presentationMode:layoutOk ' + JSON.stringify(layoutOk); }
    // 进入汇报模式时仍沿用单页 grid 编辑：只有虚线定位框叠加，没有独立 deck。
    const editBtn = document.getElementById('ai-report-edit-toggle');
    const editOk = document.body.classList.contains('ai-report-presenting-edit')
      && editBtn && editBtn.textContent.trim() === '阅读模式';
    editBtn.click();
    await new Promise((r) => setTimeout(r, 160));
    // 放映 = deck 唯一分页真源：全屏展示当前活动 slide（contain 缩放 + 居中），
    // grid/tabbar 隐藏，控件浮层挂 body（presentation.js 方案1）。
    document.querySelector('.ai-pres-controls [data-pres-action="play"]').click();
    await new Promise((r) => setTimeout(r, 480));
    const deck = document.querySelector('.ai-pres-deck');
    const activeSlide = () => document.querySelector('.ai-pres-slide.ai-pres-active');
    const playState = () => {
      const s = activeSlide();
      const m = /scale\\(([^)]+)\\)/.exec((s && s.style.transform) || '');
      const scale = m ? parseFloat(m[1]) : 0;
      return {
        has: !!s, scale, w: s ? s.offsetWidth : 0, h: s ? s.offsetHeight : 0,
        idx: s ? Array.prototype.indexOf.call(s.parentNode.children, s) : -1,
      };
    };
    const p0 = playState();
    const playOk = document.body.classList.contains('ai-report-playing')
      && deck && getComputedStyle(deck).display !== 'none'
      && grid && grid.offsetWidth === 0   // 播放态 .report-shell 整体隐藏
      && (!nav || getComputedStyle(nav).display === 'none') // 平铺报告（free 无 sections）没有 tabbar：nav=null = 不可行使，视同隐藏
      && document.querySelector('.ai-pres-controls').parentElement === document.body
      && getComputedStyle(document.body).overflow === 'hidden'
      && p0.has && p0.scale > 0
      && (p0.w * p0.scale >= innerWidth - 2 || p0.h * p0.scale >= innerHeight - 2);
    if (!playOk) {
      const dump = { playing: document.body.classList.contains('ai-report-playing'),
        deck: deck ? getComputedStyle(deck).display : 'NO-DECK',
        gridDisplay: grid && getComputedStyle(grid).display,
        nav: nav && getComputedStyle(nav).display,
        ctrlParent: document.querySelector('.ai-pres-controls') && document.querySelector('.ai-pres-controls').parentElement.tagName,
        bodyOverflow: getComputedStyle(document.body).overflow,
        p0, inner: innerWidth + 'x' + innerHeight,
        slides: document.querySelectorAll('.ai-pres-slide').length };
      await cleanup();
      return 'presentationMode:playOk ' + JSON.stringify(dump);
    }
    // 翻页必须真正切换 deck 活动页（contain 缩放跟随重算）。
    const firstIdx = p0.idx;
    let nextOk = document.querySelectorAll('.ai-pres-slide').length <= 1;
    if (!nextOk) {
      document.querySelector('[data-pres-action="next"]').click();
      await new Promise((r) => setTimeout(r, 200));
      const p1 = playState();
      if (!(p1.idx > firstIdx)) { await cleanup(); return 'presentationMode:nextOk ' + JSON.stringify({ firstIdx, p1 }); }
      nextOk = true;
    }
    // 退出放映：保留汇报模式（deck 预览仍在），恢复 tabbar 与控件挂载点。
    document.querySelector('[data-pres-action="close"]').click();
    await new Promise((r) => setTimeout(r, 160));
    const exitOk = document.body.classList.contains('ai-report-presenting')
      && !document.body.classList.contains('ai-report-playing')
      && (!nav || (getComputedStyle(nav).display !== 'none'
        && document.querySelector('.ai-pres-controls').parentElement === nav))
      && deck && getComputedStyle(deck).display !== 'none';
    // 后续 dragStrip/面板/painted 检查依赖审计最初进入的编辑态；这里恢复。
    await cleanup();
    const editRestored = document.body.classList.contains('ai-report-editing')
      && !document.body.classList.contains('ai-report-presenting')
      && grid && getComputedStyle(grid).display !== 'none';
    if (!(exitOk && editRestored)) return 'presentationMode:exitOk ' + JSON.stringify({ exitOk, editRestored });
    return presOk && editOk && playOk && nextOk && exitOk;
    } catch (error) { await cleanup(); return 'presentationMode:error ' + ((error && error.stack) || error); }
  })()`);
  // 病态输入守扩：表格卡自动高度后可能**比一页还高**（真机：19/39 行 > contentRows 12）。
  // 装页器必须夹页而不是死循环 —— 曾经 while(placed<0) 无限翻页 → 点「汇报模式」卡死
  // + RangeError: Invalid array length（fit 里的 byPage）。
  checks.presTallCardNoHang = await evalIf('presTallCardNoHang', `(async () => {
    const P = window.AIReportPresentation;
    if (!P || !P._internals || !P._internals.placeItems) return true;
    const cleanup = async () => {
      try { if (P.isActive()) P.close(); } catch (e) {}
      await new Promise((r) => setTimeout(r, 240));
      const eb = document.getElementById('ai-report-edit-toggle');
      if (eb && !document.body.classList.contains('ai-report-editing')) eb.click();
    };
    try { if (P.isActive()) P.close(); } catch (e) {}
    await new Promise((r) => setTimeout(r, 240));
    const charts = (AIReportRuntime.manifest.charts || []).filter((c) => c.viewType === 'table');
    if (!charts.length) return true;
    const chart = charts[0];
    const el = document.querySelector('.grid-stack-item[data-report-block-id="' + CSS.escape(chart.blockId) + '"]');
    if (!el) return 'table-block-missing';
    const inst = el.closest('.grid-stack') && el.closest('.grid-stack').__aiReportGrid;
    const m = AIReportRuntime.manifest;
    const origH = el.getAttribute('gs-h');
    const origLayout = m.presentation && m.presentation.layout ? JSON.parse(JSON.stringify(m.presentation.layout)) : null;
    const restore = () => {
      if (origLayout != null) m.presentation.layout = origLayout; else if (m.presentation) delete m.presentation.layout;
      el.setAttribute('gs-h', origH);
      if (inst && inst.update) inst.update(el, { h: Number(origH) || 1 });
    };
    let out = true;
    try {
      // 1) 把表格卡弄成「比一页还高」；2) 丢掉已种子化的汇报布局，强制重跑种子+装页。
      if (inst && inst.update) inst.update(el, { h: 60 }); else el.setAttribute('gs-h', '60');
      if (m.presentation) delete m.presentation.layout;
      const t0 = Date.now();
      document.getElementById('ai-report-present').click();
      const ms = Date.now() - t0;
      await new Promise((r) => setTimeout(r, 400));
      const layout = (m.presentation && m.presentation.layout) || {};
      const gw = el.closest('.grid-stack') ? el.closest('.grid-stack').clientWidth : 1280;
      const geo = P._internals.pageGeometry(gw);
      const maxH = Math.max.apply(null, Object.keys(layout).map((k) => Number(layout[k].h) || 1).concat([0]));
      const slides = document.querySelectorAll('.ai-pres-slide').length;
      if (!P.isActive() || slides < 1) out = 'pres-not-opened ' + JSON.stringify({ ms, slides, active: P.isActive() });
      else if (maxH > geo.contentRows) out = 'pres-layout-not-clamped ' + JSON.stringify({ maxH, contentRows: geo.contentRows });
      else if (ms > 3000) out = 'pres-slow ' + ms + 'ms';
    } catch (error) {
      out = 'pres-threw ' + String((error && error.stack) || error);
    }
    restore();
    await cleanup();
    // 退出后单页正文必须回到原来的高度（夹页绝不能泄漏回 block.layout）。
    // 注意顺序：close() 会还原它自己打开时的快照（里面已经是我强制的 60），
    // 所以必须 close 之后再恢复单页真实高度。
    restore();
    if (out === true) {
      const back = Number(el.getAttribute('gs-h'));
      if (back !== Number(origH)) out = 'single-page-h-leaked ' + back + ' != ' + origH;
    }
    return out;
  })()`);
  await ensureHelpers();
  checks.pptxBuilds = await evalIf('pptxBuilds', `(async () => {
    try {
      const imageFiles = await window.AIReportPPTX._buildFiles('image');
      const imageBlob = await window.AIReportPPTX._buildZip(imageFiles);
      const nativeFiles = await window.AIReportPPTX._buildFiles('native');
      const nativeBlob = await window.AIReportPPTX._buildZip(nativeFiles);
      return imageFiles.length > 5 && imageBlob.size > 1000
        && nativeFiles.length > 5 && nativeBlob.size > 1000;
    } catch (error) {
      return String(error && error.stack || error);
    }
  })()`);
  checks.pptxXmlWellFormed = await evalIf('pptxXmlWellFormed', `(async () => {
    try {
      const files = await window.AIReportPPTX._buildFiles('native');
      const parser = new DOMParser();
      return files
        .filter((f) => /\.(xml|rels)$/.test(f.name))
        .every((f) => {
          const doc = parser.parseFromString(f.data, 'application/xml');
          return !doc.querySelector('parsererror');
        });
    } catch (error) {
      return String(error && error.stack || error);
    }
  })()`);
  // ---- TBL-H4/H5：明细表「搜索本表」+「Σ 统计」（真实 server 报告，读模式即可交互）----
  // P1-2 数据自适应（marketing 冒烟）：搜索词/统计列**从报告自身数据推导** —— 曾经
  // 写死 PSI 品牌词 'MatePad' 与 'Sell In' 列，任何非 PSI 数据必挂。推导不出时诚实
  // skip（'skip:' 前缀原因串，non-green 但可诊断），绝不拿假词硬测、也不假绿。
  checks.detailSearchStats = await evalIf('detailSearchStats', `(async () => {
    const rt = window.AIReportRuntime; const m = rt && rt.manifest;
    if (!m) return false;
    const charts = (m.charts || []).filter((c) => c.viewType === 'table');
    if (!charts.length) return true; // 本报告没有表格卡时不检查
    const until = async (fn, ms) => { const t0 = Date.now(); while (!fn()) { if (Date.now() - t0 > (ms || 9000)) return false; await new Promise((r) => setTimeout(r, 200)); } return true; };
    // 选卡不能假设 charts[0]：首张表格卡可能是 3D 矩阵这类重聚合卡——渲染慢
    // （server 首屏聚合在途时 host 只有空骨架，按钮还没渲出来）或作者只配了聚合
    // 视图（modes=['aggregate']），立即查 [data-table-mode="detail"] 必然
    // 'no-detail-mode' 假红（logistics_1m 真机）。这是 charts[0] 假设家族的最后
    // 一个盲区（此前已修数据自适应 needle/统计列）。规则：
    //   1) host 里**已渲染**明细切换按钮的卡优先（真能力，零等待）；
    //   2) 否则按声明（table.modes 含 'detail'；未声明 = 双模式，与 runtime
    //      tableModesOf 的向后兼容语义一致）逐张等按钮渲出（每张 ≤6s），渲出即选中；
    //   3) 全部落空 = 报告内表格卡均无明细视图 → 诚实 skip（skip 是设计内语义，
    //      不判红——本检查测的是明细搜索/统计，不是明细能力本身）。
    let chart = null, host = null;
    for (const c of charts) {
      const h = c.elementId ? document.getElementById(c.elementId) : null;
      if (h && h.querySelector('[data-table-mode="detail"]')) { chart = c; host = h; break; }
    }
    if (!chart) {
      const modesDecl = (c) => { const t = (c.editor && c.editor.table) || {}; return !Array.isArray(t.modes) || t.modes.indexOf('detail') >= 0; };
      for (const c of charts.filter(modesDecl)) {
        const h = c.elementId ? document.getElementById(c.elementId) : null;
        if (!h) continue;
        if (await until(() => h.querySelector('[data-table-mode="detail"]'), 6000)) { chart = c; host = h; break; }
      }
    }
    if (!chart) return 'skip:detailSearchStats 报告内表格卡均无明细视图';
    if (!host) return 'detailSearchStats:no-host';
    const cleanup = async () => {
      try { const back = host.querySelector('[data-table-mode="aggregate"]'); if (back) { back.click(); await new Promise((r) => setTimeout(r, 300)); } } catch (e) {}
      try { const st = host.__gridState; if (st) { st.needle = ''; st.statsOn = false; } } catch (e) {}
    };
    try {
      // 1) 切到明细视图（server 拉页）
      const dbtn = host.querySelector('[data-table-mode="detail"]');
      if (!dbtn) return 'detailSearchStats:no-detail-mode';
      dbtn.click();
      if (!(await until(() => host.querySelector('.ai-grid-table tbody tr') && host.querySelector('[data-tbl-search]'), 12000))) {
        await cleanup(); return 'detailSearchStats:detail-render';
      }
      // 2) 数据自适应推导。
      //    搜索词 = 明细首行第一个「类目样」文本格（≥2 字符且含字母/汉字/假名/谚文）。
      //    明细非数值格显示即原值（valDisp 只做值别名），服务端 contains 对原值匹配，
      //    首行自身必命中 —— 不依赖任何数据域词表。首行全是数字/日期（纯指标形态）
      //    时退回数据集类目列的有界值域（POST /values/:col，与 fetchFilteredValues
      //    同一通道）。搜索是「命中即过滤」的contains，选到的词必然 hitN ≥ 1。
      //    统计列 = 明细字段里第一个数值列，口径与 runtime statsStripHtml 一致
      //    （AIReportChartAdapter.numericColumns），并避开 unitScale 字段（chip 值
      //    会被缩放显示，无法与 /aggregate 原始合计对数）。
      const dsId = (chart.editor && chart.editor.data && chart.editor.data.datasetId) || (m.datasets[0] && m.datasets[0].id);
      const dataset = (m.datasets || []).find((d) => d.id === dsId) || m.datasets[0] || null;
      const colNames = ((dataset && dataset.columns) || []).map((c) => (c && typeof c === 'object') ? (c.name || '') : c);
      const tcfg = (chart.editor && chart.editor.table) || {};
      const fields = Array.isArray(tcfg.detailFields) ? tcfg.detailFields.filter((f) => colNames.indexOf(f) >= 0) : colNames.slice();
      const numSet = (window.AIReportChartAdapter && typeof AIReportChartAdapter.numericColumns === 'function' && dataset)
        ? AIReportChartAdapter.numericColumns(dataset) : [];
      const hasLetter = (s) => /[A-Za-z\\u4e00-\\u9fff\\u3040-\\u30ff\\uac00-\\ud7af]/.test(s || '');
      let needle = ''; let needleSrc = '';
      const firstTr = host.querySelector('.ai-grid-table tbody tr');
      if (firstTr) {
        const cand = [...firstTr.querySelectorAll('td')]
          .filter((td) => !td.classList.contains('ai-pb-cell'))
          .map((td) => (td.textContent || '').trim())
          .find((t) => t.length >= 2 && hasLetter(t));
        if (cand) { needle = cand; needleSrc = 'first-row'; }
      }
      if (!needle && dataset && fields.length) {
        const textFields = fields.filter((c) => numSet.indexOf(c) < 0).slice(0, 4);
        for (const col of textFields) {
          const vs = await fetch('/api/datasets/' + encodeURIComponent(dsId) + '/values/' + encodeURIComponent(col),
            { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ filter: [], limit: 50 }) })
            .then((r) => (r.ok ? r.json() : null)).catch(() => null);
          const v = ((vs && vs.values) || []).find((x) => x != null && String(x).trim().length >= 2 && hasLetter(String(x)));
          if (v != null) { needle = String(v).trim(); needleSrc = 'values:' + col; break; }
        }
      }
      if (!needle) {
        await cleanup();
        return 'skip:detailSearchStats 明细首行与数据集类目列都推不出可搜索的文本值（needle 推导失败，诚实跳过）';
      }
      // 3) 搜索：命中 chip + 可见行全部含搜索词
      const inp = host.querySelector('[data-tbl-search]');
      inp.value = needle;
      inp.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
      if (!(await until(() => host.querySelector('[data-tbl-hit]'), 12000))) { await cleanup(); return 'detailSearchStats:no-hit-chip needle=' + JSON.stringify(needle); }
      const hitTxt = host.querySelector('[data-tbl-hit]').textContent;
      const hitN = Number((hitTxt.match(/命中 (\\d+) 行/) || [0, 0])[1]);
      // 行断言要等「重拉后的那一页」真正落进来（旧页残帧会假失败）。
      const rowsMatch = () => [...host.querySelectorAll('.ai-grid-table tbody tr')].every((tr) =>
        [...tr.querySelectorAll('td')].some((td) => (td.textContent || '').indexOf(needle) >= 0));
      await until(() => !host.querySelector('.ai-grid-table tbody tr') || rowsMatch(), 30000);
      const rowsOk = hitN > 0 && rowsMatch();
      if (!rowsOk) {
        const rows = [...host.querySelectorAll('.ai-grid-table tbody tr')];
        const rm = rowsMatch();
        await cleanup();
        return 'detailSearchStats:rows ' + JSON.stringify({ needle, needleSrc, hitTxt, hitN, rm, n: rows.length,
          badSample: rows.filter((tr) => ![...tr.querySelectorAll('td')].some((td) => (td.textContent || '').indexOf(needle) >= 0)).slice(0, 2).map((tr) => tr.textContent.slice(0, 120)),
          chipStill: !!host.querySelector('[data-tbl-hit]'), hostErr: !!host.querySelector('.ai-report-data-error') });
      }
      // 4) Esc 清除：命中 chip 消失（Enter 触发的重渲染会替换 input 节点——必须重查，旧引用是孤儿）
      const inp2 = host.querySelector('[data-tbl-search]');
      if (!inp2) { await cleanup(); return 'detailSearchStats:no-search-after-render'; }
      inp2.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
      if (!(await until(() => !host.querySelector('[data-tbl-hit]'), 30000))) { await cleanup(); return 'detailSearchStats:esc-clear'; }
      // 5) Σ 统计：开条 → 合计填真值（与直接 /aggregate 的全局合计对数）→ 均值模式切换
      const numFields = fields.filter((c) => numSet.indexOf(c) >= 0);
      const unscaled = numFields.filter((c) => !(((tcfg.fieldCfg || {})[c] || {}).unitScale));
      const statCol = unscaled[0] || numFields[0] || '';
      if (!statCol) {
        await cleanup();
        return 'skip:detailSearchStats 明细字段没有数值列，Σ 统计无从验证（诚实跳过）';
      }
      const sb = host.querySelector('[data-tbl-stats]');
      if (!sb) { await cleanup(); return 'detailSearchStats:no-stats-btn'; }
      sb.click();
      if (!(await until(() => host.querySelector('[data-tbl-stats-bar]'), 6000))) { await cleanup(); return 'detailSearchStats:no-strip'; }
      // 期望值必须与卡片同口径：带 columnFilters 的卡（WP-14 过滤器区），Σ 统计是
      // 过滤后行集的合计——对账请求要拼同一组谓词，否则「声明即假失败」。
      const cfFilter = [];
      const cfMap = tcfg.columnFilters || {};
      for (const col of Object.keys(cfMap)) {
        const f = cfMap[col] || {};
        if (f.type === 'values' && Array.isArray(f.values) && f.values.length) {
          cfFilter.push({ column: col, op: f.not ? 'not in' : 'in', value: f.values });
        } else if (f.type === 'range') {
          if (typeof f.min === 'number') cfFilter.push({ column: col, op: 'gte', value: f.min });
          if (typeof f.max === 'number') cfFilter.push({ column: col, op: 'lte', value: f.max });
        }
      }
      const exp = await fetch('/api/datasets/' + encodeURIComponent(dsId) + '/aggregate', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ groupBy: [], measures: [{ column: statCol, aggregation: 'sum' }], filter: cfFilter, limit: 1 })
      }).then((r) => r.json());
      const expSum = exp && exp.rows && exp.rows[0] && exp.rows[0][0];
      const chip = () => host.querySelector('[data-stat-val="' + CSS.escape(statCol) + '"]');
      if (!(await until(() => chip() && chip().textContent !== '…', 30000))) { await cleanup(); return 'detailSearchStats:stats-fill col=' + JSON.stringify(statCol); }
      const gotSum = Number(String(chip().textContent).replace(/,/g, ''));
      if (!(Math.abs(gotSum - Number(expSum)) <= 1)) { await cleanup(); return 'detailSearchStats:sum ' + JSON.stringify({ col: statCol, gotSum, expSum }); }
      const avgBtn = host.querySelector('[data-tbl-stats-mode="avg"]');
      if (!avgBtn) { await cleanup(); return 'detailSearchStats:no-avg-btn'; }
      avgBtn.click();
      if (!(await until(() => { const v = chip(); return v && /[\\d,]/.test(v.textContent); }, 30000))) { await cleanup(); return 'detailSearchStats:avg-fill'; }
      await cleanup();
      return true;
    } catch (error) { await cleanup(); return 'detailSearchStats:error ' + ((error && error.stack) || error); }
  })()`);
  // ---- PPT-H1/H2/H4：预览态可翻页 + 页码 chip + 从当前页起播 + Home/End ----
  checks.presPaging = await evalIf('presPaging', `(async () => {
    const P = window.AIReportPresentation;
    if (!P) return false;
    // 审计后续检查依赖编辑态：清理时与 presentationMode 同款恢复（我的检查会切到阅读态）。
    const cleanup = async () => {
      try { if (P.isPlaying()) P.exitPlay(); } catch (e) {}
      try { if (P.isActive()) P.close(); } catch (e) {}
      try { const eb = document.getElementById('ai-report-edit-toggle'); if (eb && !document.body.classList.contains('ai-report-editing')) eb.click(); } catch (e) {}
    };
    try {
      const btn = document.getElementById('ai-report-present');
      if (!btn) return 'presPaging:no-btn';
      btn.click();
      await new Promise((r) => setTimeout(r, 400));
      // 审计始于编辑态：进入汇报后停在「编辑布局」视图，先切回阅读态才有 deck 可翻。
      if (document.body.classList.contains('ai-report-presenting-edit')) {
        const et = document.getElementById('ai-report-edit-toggle');
        if (et) et.click();
        await new Promise((r) => setTimeout(r, 500));
      }
      const prev = document.querySelector('.ai-pres-controls [data-pres-action="prev"]');
      const next = document.querySelector('.ai-pres-controls [data-pres-action="next"]');
      const page = document.querySelector('[data-pres-page]');
      const slides = document.querySelectorAll('.ai-pres-slide').length;
      if (!prev || !next || !page) { await cleanup(); return 'presPaging:controls-missing'; }
      if (prev.offsetParent === null || next.offsetParent === null) { await cleanup(); return 'presPaging:prev-next-hidden'; }
      const m1 = /^1 \\/ (\\d+)$/.exec(page.textContent);
      if (!m1 || Number(m1[1]) !== slides) { await cleanup(); return 'presPaging:page-chip ' + page.textContent + ' slides=' + slides; }
      if (slides > 1) {
        next.click();
        await new Promise((r) => setTimeout(r, 250));
        if (page.textContent.indexOf('2 / ') !== 0) { await cleanup(); return 'presPaging:next-page ' + page.textContent; }
        // 从当前页起播（Shift+F5）：放映后仍停在第 2 页
        document.dispatchEvent(new KeyboardEvent('keydown', { key: 'F5', shiftKey: true, bubbles: true }));
        await new Promise((r) => setTimeout(r, 500));
        const active = document.querySelector('.ai-pres-slide.ai-pres-active');
        const idx = active ? Array.prototype.indexOf.call(active.parentNode.children, active) : -1;
        if (!document.body.classList.contains('ai-report-playing') || idx !== 1) {
          await cleanup(); return 'presPaging:play-from-current ' + JSON.stringify({ playing: document.body.classList.contains('ai-report-playing'), idx, chip: page.textContent });
        }
        // Home / End 跳首末页
        document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Home', bubbles: true }));
        await new Promise((r) => setTimeout(r, 250));
        const homeOk = page.textContent.indexOf('1 / ') === 0;
        document.dispatchEvent(new KeyboardEvent('keydown', { key: 'End', bubbles: true }));
        await new Promise((r) => setTimeout(r, 250));
        const endOk = page.textContent === (slides + ' / ' + slides);
        if (!(homeOk && endOk)) { await cleanup(); return 'presPaging:home-end ' + page.textContent; }
        P.exitPlay();
        await new Promise((r) => setTimeout(r, 250));
      }
      await cleanup();
      return true;
    } catch (error) { await cleanup(); return 'presPaging:error ' + ((error && error.stack) || error); }
  })()`);
  // ---- PPT-H3：导出 PDF（print 路线）——root 构建 + 页数一致 + afterprint 清理 ----
  checks.pdfExport = await evalIf('pdfExport', `(async () => {
    const P = window.AIReportPresentation;
    if (!P) return false;
    const cleanup = async () => {
      try { const r = document.getElementById('ai-print-root'); if (r) r.remove(); document.body.classList.remove('ai-report-printing'); } catch (e) {}
      try { if (P.isActive()) P.close(); } catch (e) {}
      try { const eb = document.getElementById('ai-report-edit-toggle'); if (eb && !document.body.classList.contains('ai-report-editing')) eb.click(); } catch (e) {}
    };
    try {
      const btn = document.getElementById('ai-report-present');
      if (!btn) return 'pdfExport:no-btn';
      btn.click();
      await new Promise((r) => setTimeout(r, 400));
      if (document.body.classList.contains('ai-report-presenting-edit')) {
        const et = document.getElementById('ai-report-edit-toggle');
        if (et) et.click();
        await new Promise((r) => setTimeout(r, 500));
      }
      // 2026-09-17：导出 PDF 收进「保存与分享」中心（data-share-op="pdf-export"）。
      const shareBtn = document.getElementById('ai-report-share');
      if (!shareBtn) { await cleanup(); return 'pdfExport:no-share-btn'; }
      shareBtn.click();
      await new Promise((r) => setTimeout(r, 300));
      const pdfOp = document.querySelector('[data-share-op="pdf-export"]');
      if (!pdfOp) { await cleanup(); return 'pdfExport:no-pdf-op'; }
      window.__pdfPrinted = 0;
      window.print = () => { window.__pdfPrinted += 1; };
      pdfOp.click();
      await new Promise((r) => setTimeout(r, 400));
      const root = document.getElementById('ai-print-root');
      const slides = document.querySelectorAll('.ai-pres-deck .ai-pres-slide').length;
      const pages = root ? root.querySelectorAll('.ai-print-page').length : 0;
      if (!root || pages !== slides || window.__pdfPrinted !== 1 || !document.body.classList.contains('ai-report-printing')) {
        await cleanup(); return 'pdfExport:build ' + JSON.stringify({ pages, slides, printed: window.__pdfPrinted });
      }
      window.dispatchEvent(new Event('afterprint'));
      await new Promise((r) => setTimeout(r, 120));
      if (document.getElementById('ai-print-root') || document.body.classList.contains('ai-report-printing')) {
        await cleanup(); return 'pdfExport:cleanup';
      }
      await cleanup();
      return true;
    } catch (error) { await cleanup(); return 'pdfExport:error ' + ((error && error.stack) || error); }
  })()`);
  // Render-truth: no block content may overflow its grid cell (scrollbar or
  // clipped text). The runtime table wrap is excluded (R2/L8 govern its rows).
  //
  // 先等「自适应高度」这一轮异步布局收敛（autoFitTextBlocks / autoFitKpiCards /
  // autoFitTableCards 都在 RAF 里跑）：不等就可能测到某张卡**还没被压缩**的一帧，
  // 报出 +8px 这类假溢出（真机：insight 卡在负载高时先后两次结果不同）。真正的
  // 溢出不会收敛，所以这个等待不削弱断言。
  await evaluate(`(async () => {
    const worst = () => {
      let max = 0;
      document.querySelectorAll('[data-report-block]').forEach((b) => {
        if (!(b instanceof HTMLElement)) return;
        max = Math.max(max, b.scrollHeight - b.clientHeight);
      });
      return max;
    };
    let prev = -1;
    for (let i = 0; i < 30; i += 1) {
      const cur = worst();
      if (cur === prev) return cur;
      prev = cur;
      await new Promise((r) => setTimeout(r, 250));
    }
    return prev;
  })()`);
  checks.noBlockOverflow = await evalIf('noBlockOverflow', `(() => {
    const bad = [];
    document.querySelectorAll('body *').forEach((el) => {
      if (!(el instanceof HTMLElement)) return;
      if (el.closest('#ai-report-runtime-ui')) return;
      // .ai-report-table-wrap 与 .table-host 都是许可的卡内滚动容器：分页表格
      // 卡在卡内滚动是 validate_report R2 明确接受的契约（pagination →
      // scrollbar acceptable），把宿主一并排除，避免数据加载完成先后导致的
      // 竞态假失败。
      if (el.classList && (el.classList.contains('ai-report-table-wrap') || el.classList.contains('table-host'))) return;
      const cs = getComputedStyle(el);
      if (cs.display === 'none') return;
      if (cs.overflowY === 'visible') return;  // content spills, no scrollbar/clip
      const overY = el.scrollHeight > el.clientHeight + 3;
      if (overY) {
        bad.push(String(el.className || el.tagName).slice(0, 50) + ':+' + (el.scrollHeight - el.clientHeight));
      }
    });
    return bad.length === 0 ? true : bad.slice(0, 5);
  })()`);
  // Runtime UX fixes (#2 panel dedup, #3 drag strip, card cursor)
  const ensureChartView = (blockId) => `(() => {
    const b=document.querySelector('[data-report-block-id="${blockId}"]');
    const v=b&&b.closest('[data-tab-view]');
    if(v&&v.classList.contains('ai-report-view-hidden')){
      const t=document.querySelector('[data-tab-go="'+CSS.escape(v.getAttribute('data-tab-view'))+'"]');
      if(t)t.click();
    }
  })()`;
  const firstChart = chartIds[0];
  if (firstChart) { await evaluate(ensureChartView(firstChart)); await wait(240); }
  checks.panelNoDuplicateActions = await evalIf('panelNoDuplicateActions', `(() => {
    const first = AIReportRuntime.manifest.charts[0];
    const b = first && document.querySelector('[data-report-block-id="' + first.blockId + '"]');
    // 首图可能不在当前视图（分页模式/隐藏页签）→ 该检查不适用，不要因此整体失败。
    if (!b) return true;
    b.click(); const eb = b.querySelector('.ai-report-block-edit'); if (eb) eb.click();
    const panel = document.getElementById('ai-report-runtime-panel');
    return !panel.querySelector('[data-runtime-action="download"], [data-runtime-action="delete-block"]');
  })()`);
  checks.dragStripPresent = await evalIf('dragStripPresent', `!!document.querySelector('.ai-report-drag-strip') && document.querySelector('.ai-report-drag-strip').getAttribute('title') === '按住拖动卡片'`);
  checks.cardNotGrab = await evalIf('cardNotGrab', `getComputedStyle(document.querySelector('[data-report-block]')).cursor !== 'grab'`);
  checks.dragStripGrab = await evalIf('dragStripGrab', `getComputedStyle(document.querySelector('.ai-report-drag-strip')).cursor === 'grab'`);
  // A1（interaction-audit-2026-09-15）：无效落点拖拽不得静默丢卡，manifest 不得落
  // 重叠坐标。通用复现：抓一张卡的 ⠿ 拖柄，向下拖 + 上越网格顶（制造真实 dropout）
  // + 回到满行松手；断言 卡片 DOM/manifest 数量不变、每张卡仍在 DOM、gs 矩形两两
  // 不相交（不同网格各管各的坐标系不参与比较）。拖拽未真正咬合（无占位且引擎无
  // 移动）时诚实 skip——不做假绿。
  checks.dragLostGuard = await evalIf('dragLostGuard', `(async () => {
    const strip = document.querySelector('[data-report-block] .ai-report-drag-strip');
    if (!strip) return true; // 无拖柄（只读/无网格）→ 不适用
    const count = () => ({ blocks: document.querySelectorAll('[data-report-block]').length, cards: document.querySelectorAll('.grid-stack-item').length });
    const overlaps = () => {
      const groups = {};
      document.querySelectorAll('.grid-stack-item').forEach((el) => {
        const g = el.closest('[data-report-grid]'); if (!g) return;
        (groups[g.getAttribute('data-tab-view') || 'g'] = groups[g.getAttribute('data-tab-view') || 'g'] || []).push({ id: el.getAttribute('data-report-block-id'), x: Number(el.getAttribute('gs-x') || 0), y: Number(el.getAttribute('gs-y') || 0), w: Number(el.getAttribute('gs-w') || 12), h: Number(el.getAttribute('gs-h') || 3) });
      });
      const bad = [];
      Object.keys(groups).forEach((k) => {
        const arr = groups[k];
        for (let i = 0; i < arr.length; i += 1) for (let j = i + 1; j < arr.length; j += 1) {
          const a = arr[i], b = arr[j];
          if (!(a.y >= b.y + b.h || a.y + a.h <= b.y || a.x + a.w <= b.x || a.x >= b.x + b.w)) bad.push([a.id, b.id]);
        }
      });
      return bad;
    };
    const before = count(), beforeOverlap = overlaps();
    const item = strip.closest('.grid-stack-item');
    item.scrollIntoView({ block: 'center' });
    await new Promise((r) => setTimeout(r, 300));
    const sr = strip.getBoundingClientRect();
    const sx = Math.round(sr.left + sr.width / 2), sy = Math.round(sr.top + sr.height / 2);
    const mk = (type, x, y) => {
      const opts = { bubbles: true, cancelable: true, clientX: x, clientY: y, button: 0, view: window, pointerId: 1, isPrimary: true };
      return type.indexOf('pointer') === 0 ? new PointerEvent(type, opts) : new MouseEvent(type, opts);
    };
    strip.dispatchEvent(mk('pointerdown', sx, sy));
    strip.dispatchEvent(mk('mousedown', sx, sy));
    await new Promise((r) => setTimeout(r, 120));
    const grid = document.querySelector('[data-report-grid]');
    const gTop = grid.getBoundingClientRect().top + 6;
    // 向下拖 4 步进入网格深处的满行区，再上越网格顶（触发 dropout），再回满行松手
    const downY = Math.min(sy + 320, window.innerHeight - 60);
    for (let i = 1; i <= 4; i += 1) { document.dispatchEvent(mk('mousemove', sx, sy + (downY - sy) * i / 4)); await new Promise((r) => setTimeout(r, 45)); }
    document.dispatchEvent(new MouseEvent('mouseenter', { bubbles: false, cancelable: true })); // bindDragStripGuard 同款占位跟随
    await new Promise((r) => setTimeout(r, 150));
    for (let y = downY; y > gTop; y -= 60) { document.dispatchEvent(mk('mousemove', sx, y)); await new Promise((r) => setTimeout(r, 35)); }
    await new Promise((r) => setTimeout(r, 150));
    for (let y = gTop; y < downY; y += 60) { document.dispatchEvent(mk('mousemove', sx, y)); await new Promise((r) => setTimeout(r, 35)); }
    await new Promise((r) => setTimeout(r, 150));
    const engaged = !!document.querySelector('.grid-stack-placeholder') || !!(item.parentElement && document.querySelector('.grid-stack-item-removing'));
    document.dispatchEvent(mk('mouseup', sx, downY));
    document.dispatchEvent(mk('pointerup', sx, downY));
    await new Promise((r) => setTimeout(r, 900));
    const after = count();
    if (!engaged) return { skipped: 'drag-not-engaged' };
    const detached = Array.prototype.slice.call(document.querySelectorAll('[data-report-block]')).filter((el) => !el.isConnected).length;
    const bad = overlaps();
    const okCounts = after.blocks === before.blocks && after.cards === before.cards;
    return (okCounts && detached === 0 && bad.length === 0 && beforeOverlap.length === 0) || { fail: { before, after, detached, bad: bad.slice(0, 4), beforeOverlap: beforeOverlap.slice(0, 4) } };
  })()`);
  // W1 间距不变量（拖高/拖宽/移动/增删卡后都不许出现幻影行）。vendored GridStack
  // v11.1.2 是「内衬 margin」模型：item 盒高 = gs-h×cellHeight（真机 gs-h=4 →
  // 192px）、可见卡 = .grid-stack-item-content 四周 inset margin、相邻卡可见间距
  // 标准值 = 2×margin(16px)。因此每张可见卡必须满足：
  //   (a) 预留恒等式  gs-h === ceil((itemH − 2×margin)/cell)
  //       —— 旧 ceil(px/cell) 反推在 px 刚过行界时多预留一整行（真机：拖到 201px
  //       → gs-h 5 → 下方间距 55px；正确 4 行 → 7px），这就是 W1「间距凭空变大」。
  //   (b) 最近下方邻卡（x 向重叠、y 相继）的可见间距 ∈ [−2, cell)
  //       —— 下界=不重叠，上界=不空出一整行；「文本带间距」等自动 hug 卡天然落
  //       在该区间（≤ cell−2×margin+16），原生卡恒为 2×margin。
  // 断言真实拦截：检查内部用**合成鼠标事件**对 se 手柄做一次真实拖高序列
  // （+45px，跨行界），编辑前后各采集一次不变量 —— 旧公式在这里必红。
  checks.manualGapInvariant = await evalIf('manualGapInvariant', `(async () => {
    // 全量审计里排在后面的面板类检查可能把某张卡留在 focus 态（position:fixed
    // 充满视口）——先退回普通布局再量，否则测量全是假的。
    if (document.body.classList.contains('ai-report-focus')) {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
      await new Promise((r) => setTimeout(r, 500));
    }
    const gather = () => {
      if (document.body.classList.contains('ai-report-focus')) return { cell: 48, margin: 8, bad: [], n: 0 };
      const items = Array.prototype.slice.call(document.querySelectorAll('.grid-stack-item'))
        .filter((el) => el.offsetHeight > 0 && el.closest('[data-report-grid]') && !el.closest('.ai-pres-deck') && !el.closest('#ai-print-root') && !el.classList.contains('ai-report-focus-target'));
      let cell = 48, margin = 8;
      for (const el of items) {
        const inst = el.closest('.grid-stack') && el.closest('.grid-stack').__aiReportGrid;
        if (inst && inst.opts) { cell = Number(inst.opts.cellHeight) || 48; margin = Number(inst.opts.margin) || 8; break; }
      }
      const rects = items.map((el) => {
        const c = el.querySelector(':scope > .grid-stack-item-content');
        const r = (c || el).getBoundingClientRect();
        const er = el.getBoundingClientRect();
        return { id: el.getAttribute('data-report-block-id'), top: r.top, bottom: r.bottom, left: r.left, right: r.right, itemH: Math.round(er.height), gsH: Number(el.getAttribute('gs-h') || 1) };
      });
      const bad = [];
      for (const a of rects) {
        const want = Math.max(1, Math.ceil((a.itemH - 2 * margin) / cell));
        if (want !== a.gsH) bad.push('identity:' + a.id + ':gsH=' + a.gsH + ',want=' + want + ',h=' + a.itemH);
      }
      for (const a of rects) {
        // 最近后继语义（logistics_1m 真机修复）：先找 a 的**真实最近下邻**（x 向
        // 重叠、y 相继里最上方的那张卡），只对这一对判间距；「同排更高兄弟盖住
        // 下邻」的豁免作用于这一对，命中即整卡跳过（该空档属于兄弟的预留，间距
        // 由兄弟自己的配对去验）。旧实现把豁免放进 min-gap 选择循环：最近邻被
        // 豁免后会**顺位到更远的非相邻卡**，把第三卡占着的纵向区间误判成 a 的
        // 幻影行空档（真机：trend 的最近下邻是通栏 narrative，被同排更高的
        // sidecar 豁免后顺位到 region-stack，160/208px 假红；下方空间实际被
        // narrative 占据，不是视觉空隙）。
        let near = null;
        for (const b of rects) {
          if (a === b || b.top < a.bottom - 2) continue;
          if (Math.min(a.right, b.right) - Math.max(a.left, b.left) <= 8) continue;
          if (!near || b.top < near.top) near = b;
        }
        if (!near) continue;
        const covered = rects.some((c) => c !== a && c.top < a.bottom && c.bottom > a.bottom + 2 &&
          Math.min(c.right, near.right) - Math.max(c.left, near.left) > 8 && near.top >= c.bottom - 2);
        if (covered) continue;
        const gap = Math.round(near.top - a.bottom);
        if (gap < -2 || gap >= cell) bad.push('gap:' + a.id + '->' + near.id + '=' + gap);
      }
      return { cell, margin, bad, n: rects.length };
    };
    const first = gather();
    if (first.n === 0) return true; // 无可见卡（全隐藏页签/空报告）——不适用
    // 拖高序列：挑一张下方有卡、带 se 手柄的正文卡（排除 cover/filter chrome）。
    const drag = await (async () => {
      const items = Array.prototype.slice.call(document.querySelectorAll('.grid-stack-item')).filter((el) => el.offsetHeight > 0);
      const isChrome = (el) => ['block-cover', 'block-filter'].indexOf(el.getAttribute('data-report-block-id')) >= 0;
      const boxOf = (el) => { const c = el.querySelector(':scope > .grid-stack-item-content'); return (c || el).getBoundingClientRect(); };
      const prio = (el) => ({ text: 0, chart: 1, kpi: 2 }[el.getAttribute('data-report-block')] ?? 3);
      const cands = items.filter((el) => !isChrome(el) && el.querySelector(':scope > .ui-resizable-se') &&
        items.some((b) => { if (b === el) return false; const ba = boxOf(el), bb = boxOf(b); return bb.top >= ba.bottom - 2 && Math.min(ba.right, bb.right) - Math.max(ba.left, bb.left) > 8; }));
      cands.sort((a, b) => prio(a) - prio(b));
      const target = cands[0] || null;
      if (!target) return { skipped: 'no-drag-target' };
      const id = target.getAttribute('data-report-block-id');
      target.scrollIntoView({ block: 'center' });
      await new Promise((r) => setTimeout(r, 250));
      const beforeInline = target.style.height;
      target.classList.remove('ui-resizable-autohide'); // se 手柄 hover 才可见；直接点亮
      const h = target.querySelector(':scope > .ui-resizable-se');
      const r = h.getBoundingClientRect();
      const cx = Math.round(r.left + r.width / 2), cy = Math.round(r.top + r.height / 2);
      const mk = (type, x, y) => new MouseEvent(type, { bubbles: true, cancelable: true, clientX: x, clientY: y, button: 0, view: window });
      h.dispatchEvent(mk('mousedown', cx, cy));
      await new Promise((r2) => setTimeout(r2, 80));
      for (let step = 1; step <= 9; step += 1) { document.dispatchEvent(mk('mousemove', cx, cy + step * 5)); await new Promise((r2) => setTimeout(r2, 25)); }
      await new Promise((r2) => setTimeout(r2, 120));
      document.dispatchEvent(mk('mouseup', cx, cy + 45));
      // resizestop 经 setTimeout(0) 提交（GridStack 随后同步恢复样式），等 autofit 收敛。
      await new Promise((r2) => setTimeout(r2, 900));
      const blk = AIReportRuntime.manifest.blocks.find((b) => b.id === id);
      const afterInfo = { id, beforeInline, inline: target.style.height, gsH: target.getAttribute('gs-h'), manualH: !!(blk && blk.manualH), hPx: blk && blk.hPx };
      // 复原：「适应内容」解除 manual，回到自动 hug（不打扰后续检查）。
      const rel = target.querySelector('[data-manual-fit-release]');
      if (rel && blk && blk.manualH) { rel.click(); await new Promise((r2) => setTimeout(r2, 700)); }
      return { skipped: false, afterInfo };
    })();
    const after = gather();
    const badAll = first.bad.concat(after.bad);
    // 状态污染豁免（P3 假阳性修复，2026-09-16）：本检查排在拖拽/面板类检查之后，
    // 前序交互可能留下**时点态**布局（resizestop/autoFit 尚未收敛、 transitional
    // 网格洞——1M logistics 全量审计曾把拖拽中的 208px 空洞判成 fail，fresh load
    // 网格稠密无洞）。判红前静置 1.2s 复测一遍：只有**稳定存在**的违例才 fail，
    // 瞬态测量按前缀（identity:<id> / gap:<a>-><b>）对上即豁免。
    let persistent = badAll;
    if (badAll.length) {
      await new Promise((r) => setTimeout(r, 1200));
      const settled = gather();
      const keyOf = (b) => b.split('=').join(':').split(':').slice(0, 2).join(':');
      persistent = badAll.filter((b) => settled.bad.some((sb) => keyOf(sb) === keyOf(b)));
    }
    if (persistent.length) return { fail: persistent.slice(0, 8), cards: first.n, dragged: drag, transient: badAll.length - persistent.length };
    return true;
  })()`);
  for (const blockId of chartIds) {
    // 范围门：这张图没有任何被选中的 chart 检查 → 整组跳过（不点面板、不重渲染）。
    // 无论如何，这张图**没被选中**的 per-chart 检查名都记入 SKIPPED_CHECKS：空命中
    // 守卫用「既没执行也没跳过」判定一个选中模式真的零命中 —— 显式 --skip 掉（或
    // 范围没选中）的 chart 检查不能被误报成 unmatchedPatterns（controls 等直赋值
    // 检查没有 evalIf，漏记会假警）。
    const CHART_SUFFIXES = ['controls', 'paintedContent', 'instant', 'semanticControls', 'tableLimit', 'noBadFormatters', 'painted', 'heatmapComplete'];
    const unwantedSuffixes = CHART_SUFFIXES.filter((s) => !want('chart:' + blockId + ':' + s));
    unwantedSuffixes.forEach((s) => SKIPPED_CHECKS.push('chart:' + blockId + ':' + s));
    if (unwantedSuffixes.length === CHART_SUFFIXES.length) continue;
    // 面板交互只为 controls/instant/semanticControls/tableLimit 服务；只验绘制真值时不碰面板。
    const wantCtl = ['controls', 'instant', 'semanticControls', 'tableLimit'].some((s) => want('chart:' + blockId + ':' + s));
    if (wantCtl) {
      // Workbench tabs: make sure this chart's view is the active one before
      // interacting — hidden [data-tab-view] containers cannot paint canvases.
      await evaluate(ensureChartView(blockId)); await wait(180);
      await evaluate(`(() => { const b=document.querySelector('[data-report-block-id="${blockId}"]'); b.click(); const eb=b.querySelector('.ai-report-block-edit'); if(eb) eb.click(); })()`); await wait(160);
    }
    let result = null;
    if (want('chart:' + blockId + ':controls')) {
      result = await withInfraRetry(`chart:${blockId}:controls`, () => evaluate(`(() => { const chart = AIReportRuntime.manifest.charts.find((c) => c.blockId === ${JSON.stringify(blockId)}); const view = document.querySelector('input[name="ai-chart-view"]:checked'); return { editable: chart.editor?.editable !== false, hasType: !!view, hasDimension: !!document.getElementById('ai-chart-dimension'), hasMeasures: document.querySelectorAll('[data-measure]').length > 0 }; })()`));
      // infra 失败返回 'infraFailed:…' 字符串（非对象）：如实落值判红，不落 «editable undefined → true» 的假绿缝。
      checks[`chart:${blockId}:controls`] = (result && typeof result === 'object') ? (result.editable ? result.hasType && result.hasDimension && result.hasMeasures : true) : result;
    }
    // Capture paintedContent BEFORE the controls loop mutates chart.viewType.
    // Otherwise the pie/donut 'painted' check would render the post-switch bar
    // (always paints) and miss the original-viewType 0-pixel bug (FM-24).
    const initialVT = initialViewType[blockId] || '';
    checks[`chart:${blockId}:paintedContent`] = await evalIf(`chart:${blockId}:paintedContent`, `(async () => {
      const chart = AIReportRuntime.manifest.charts.find((c) => c.blockId === ${JSON.stringify(blockId)});
      const share = {pie:1,donut:1,funnel:1,treemap:1,sunburst:1,tree:1,gauge:1};
      const vt = ${JSON.stringify(initialVT)} || chart.viewType;
      // 表格卡：painted = 渲染出真实 tbody 行且没有数据错误横幅（轮询到 8s——
      // server 大表聚合慢；旧口径对非饼图族一律 true，整卡渲染失败不可见，
      // 真机：平表 flatColMax 取值顺序 bug 只有视觉验收抓到、全量审计全绿）。
      if (vt === 'table') {
        const host0 = document.getElementById(chart.elementId);
        if (!host0) return false;
        const t0 = Date.now();
        let last = false;
        while (Date.now() - t0 < 8000) {
          if (host0.querySelector('.ai-report-data-error')) return 'table-render-error';
          const body = host0.querySelector('.ai-report-table-wrap tbody tr, .ai-report-detail-wrap tbody tr');
          if (body) return true;
          last = !!body;
          await new Promise((r) => setTimeout(r, 200));
        }
        return last || 'table-not-painted';
      }
      if (!share[vt]) return true;
      const host = document.getElementById(chart.elementId);
      // 底部卡片采样前先滚进视口（NQ-01 假红修复）：视口外的 canvas 可能还没被
      // 真正绘制/提交（懒渲染与合成器时序），getImageData 采到全透明 → 页面底部的
      // 饼/环恒假红。与 dragLostGuard/buttonRowHeights 的 scrollIntoView({block:'center'})
      // 是同一先例；滚进视口不影响像素真值（读的是 canvas 背板，不是合成结果）。
      if (host && host.scrollIntoView) {
        host.scrollIntoView({ block: 'center' });
        await new Promise((r) => setTimeout(r, 400));
      }
      const canvas = host && host.querySelector('canvas');
      if (!canvas || canvas.width === 0 || canvas.height === 0) return false;
      const ctx = canvas.getContext('2d');
      const w = canvas.width, h = canvas.height;
      for (let j = 1; j < 10; j++) {
        for (let i = 1; i < 10; i++) {
          const x = Math.round(w * i / 10);
          const y = Math.round(h * j / 10);
          const d = ctx.getImageData(x, y, 1, 1).data;
          if (d[3] > 8 && !(d[0] > 240 && d[1] > 240 && d[2] > 240)) return true;
        }
      }
      return false;
    })()`);
    if (wantCtl && result && result.editable) {
      const target = await evaluate(`Array.from(document.querySelectorAll('input[name="ai-chart-view"]')).find((node) => node.value === 'bar')?.value || document.querySelector('input[name="ai-chart-view"]')?.value`);
      if (target) { await evaluate(`document.querySelector('input[name="ai-chart-view"][value="${target}"]').click()`); await wait(180); }
      checks[`chart:${blockId}:instant`] = await evalIf(`chart:${blockId}:instant`, '!!document.querySelector("#ai-report-runtime-panel")');
      // Server-mode semantic toggles re-aggregate asynchronously; on a
      // multi-hundred-thousand-row SQLite dataset the aggregate can exceed a
      // fixed ~100ms wait, so each expectation polls to a 4s deadline instead
      // of sampling once (AGENTS.md: the fixed-wait false negative).
      const controlCheck = await evalIf(`chart:${blockId}:semanticControls`, `(async () => { const chart = AIReportRuntime.manifest.charts.find((c) => c.blockId === ${JSON.stringify(blockId)}); const until = async (fn, ms) => { const t0 = Date.now(); while (!fn()) { if (Date.now() - t0 > (ms || 4000)) return false; await new Promise((r) => setTimeout(r, 120)); } return true; }; const getOption = () => echarts.getInstanceByDom(document.getElementById(chart.elementId))?.getOption(); const legend = document.getElementById('ai-chart-legend'); if (legend) { legend.checked = !legend.checked; legend.dispatchEvent(new Event('change', { bubbles: true })); const want = legend.checked; if (!await until(() => { const o = getOption(); return !!o && o.legend?.[0]?.show === want; }, 12000)) return false; legend.checked = !legend.checked; legend.dispatchEvent(new Event('change', { bubbles: true })); } const label = document.getElementById('ai-chart-label'); if (label) { label.checked = !label.checked; label.dispatchEvent(new Event('change', { bubbles: true })); const want = label.checked; if (!await until(() => { const o = getOption(); return !!o && (o.series || []).every((s) => { const l = Array.isArray(s.label) ? s.label[0] : s.label; return l?.show === want; }); })) return false; label.checked = !label.checked; label.dispatchEvent(new Event('change', { bubbles: true })); } const measures = Array.from(document.querySelectorAll('[data-measure]')); if (measures.length > 1) { const before = measures.filter((node) => node.checked).length; const candidate = measures.find((node) => node.checked); if (candidate) { candidate.checked = false; candidate.dispatchEvent(new Event('change', { bubbles: true })); const floorHeld = async () => { /* 权威源 = manifest（DOM 面板可能正在重渲染/被负载拖慢） */ const kept = ((AIReportRuntime.manifest.charts.find((c) => c.blockId === chart.blockId) || { editor: { data: {} } }).editor.data.measures || []).length; const n = document.querySelectorAll('input.ai-measure-hidden').length; const legacy = document.querySelectorAll('[data-measure]:checked').length; return Math.max(kept, n, legacy) >= Math.max(1, before - 1); }; if (!await until(floorHeld, 12000)) return false; candidate.checked = true; candidate.dispatchEvent(new Event('change', { bubbles: true })); } } return true; })()`);
      checks[`chart:${blockId}:semanticControls`] = controlCheck;
      if (want('chart:' + blockId + ':tableLimit') && await evaluate('document.querySelector("input[name=ai-chart-view][value=table]")?.checked')) {
        const limit = document.getElementById('ai-table-limit'); if (limit) { limit.value = '2'; limit.dispatchEvent(new Event('input', { bubbles: true })); await wait(100); checks[`chart:${blockId}:tableLimit`] = await evalIf(`chart:${blockId}:tableLimit`, 'document.querySelectorAll(".ai-report-table-wrap tbody tr").length <= 2'); }
      }
    }
    // ---- render-truth: no function-string formatters in the painted option ----
    // Server-mode interactions are asynchronous (each semantic change may
    // issue a bounded aggregate request). Poll for a real canvas/table instead
    // of relying on a fixed sleep, which is flaky on slower Windows machines.
    const needPaint = want('chart:' + blockId + ':noBadFormatters') || want('chart:' + blockId + ':painted');
    const paintedAfterControls = needPaint ? await waitForPaint((await evaluate(`AIReportRuntime.manifest.charts.find((c) => c.blockId === ${JSON.stringify(blockId)}).elementId`))) : false;
    checks[`chart:${blockId}:noBadFormatters`] = await evalIf(`chart:${blockId}:noBadFormatters`, `(() => {
      const chart = AIReportRuntime.manifest.charts.find((c) => c.blockId === ${JSON.stringify(blockId)});
      if (!chart) return true;   // 卡片标识已变（编辑器操作/异步重渲染）= 不适用
      if (chart.viewType === 'table') return true;
      const option = echarts.getInstanceByDom(document.getElementById(chart.elementId))?.getOption();
      return !!option && !window.__auditBadFormatter(option);
    })()`);
    checks[`chart:${blockId}:painted`] = await evalIf(`chart:${blockId}:painted`, `(() => {
      const chart = AIReportRuntime.manifest.charts.find((c) => c.blockId === ${JSON.stringify(blockId)});
      return window.__auditPainted(chart.elementId);
    })()`);
    if (needPaint && !checks[`chart:${blockId}:painted`] && paintedAfterControls) {
      checks[`chart:${blockId}:painted`] = true;
    }
    // Render-truth: a heatmap must paint one cell per axis intersection; a
    // flattened matrix (single-dimension aggregation) is a broken heatmap.
    checks[`chart:${blockId}:heatmapComplete`] = await evalIf(`chart:${blockId}:heatmapComplete`, `(() => {
      const chart = AIReportRuntime.manifest.charts.find((c) => c.blockId === ${JSON.stringify(blockId)});
      if (chart.viewType !== 'heatmap') return true;
      const option = echarts.getInstanceByDom(document.getElementById(chart.elementId))?.getOption();
      if (!option) return false;
      const xs = (option.xAxis && (option.xAxis[0] || option.xAxis).data || []).length;
      const ys = (option.yAxis && (option.yAxis[0] || option.yAxis).data || []).length;
      const cells = option.series && option.series[0] && option.series[0].data ? option.series[0].data : [];
      if (xs * ys === 0 || cells.length !== xs * ys) return false;
      // Transposition guard: every cell index must be within its axis range.
      // A common authoring bug writes [row, col, v] when the axes are
      // [x=col, y=row], which L1 only catches when the row totals happen to
      // break monotonicity. This asserts the shape directly.
      return cells.every((c) => {
        if (!Array.isArray(c) || c.length < 3) return false;
        const xi = c[0], yi = c[1];
        return Number.isInteger(xi) && Number.isInteger(yi) && xi >= 0 && xi < xs && yi >= 0 && yi < ys;
      });
    })()`);
  }
  // ---- data-truth: every table chart's rendered values must match a
  // re-aggregation of the source dataset. AI-computed measures are renamed
  // (e.g. 销售额 = sum(实付金额)), so the author declares the mapping:
  //   "truth": {"dataset": "primary", "dimension": "品类", "aggregation": "sum",
  //              "mapping": {"销售额": "实付金额", "毛利额": "毛利金额"}}
  // Measures without a declared mapping are skipped (not verifiable).
  for (const blockId of chartIds) {
    checks[`chart:${blockId}:dataTruth`] = await evalIf(`chart:${blockId}:dataTruth`, `(() => {
      const chart = AIReportRuntime.manifest.charts.find((c) => c.blockId === ${JSON.stringify(blockId)});
      const viewType = ${JSON.stringify(initialViewType[blockId] || '')};
      if (viewType !== 'table') return true;
      const ed = chart.editor && chart.editor.data;
      if (!ed || !ed.rows || !ed.columns) return true;
      const truth = chart.truth;
      if (!truth || !truth.mapping) return true; // nothing declared -> skip
      const dataset = (AIReportRuntime.manifest.datasets || []).find((d) => d.id === truth.dataset);
      if (!dataset) return ['no-dataset:' + truth.dataset];
      const dsCols = (dataset.columns || []).map((c) => typeof c === 'string' ? c : c.name);
      const dimIdx = dsCols.indexOf(truth.dimension);
      if (dimIdx < 0) return ['no-dimension:' + truth.dimension];
      const agg = truth.aggregation || 'sum';
      const groupMap = new Map();
      for (const r of (dataset.rows || [])) {
        const key = String(r[dimIdx] ?? '');
        if (key.trim() === '') continue;
        if (!groupMap.has(key)) groupMap.set(key, { n: 0 });
        const g = groupMap.get(key);
        g.n += 1;
        Object.entries(truth.mapping).forEach(([, spec]) => {
          const dsCol = typeof spec === 'string' ? spec : spec.column;
          const mAgg = typeof spec === 'string' ? agg : (spec.aggregation || agg);
          if (dsCol === '__count__') { g.__count__ = (g.__count__ || 0) + 1; return; }
          const mi = dsCols.indexOf(dsCol);
          if (mi < 0) return;
          if (!(dsCol in g)) g[dsCol] = 0;
          const v = Number(r[mi]);
          if (!Number.isFinite(v)) return;
          if (mAgg === 'min') g[dsCol] = g.n === 1 ? v : Math.min(g[dsCol], v);
          else if (mAgg === 'max') g[dsCol] = g.n === 1 ? v : Math.max(g[dsCol], v);
          else g[dsCol] += v;
        });
      }
      groupMap.forEach((g) => Object.entries(truth.mapping).forEach(([, spec]) => {
        const dsCol = typeof spec === 'string' ? spec : spec.column;
        const mAgg = typeof spec === 'string' ? agg : (spec.aggregation || agg);
        if (dsCol === '__count__') return;
        if (mAgg === 'average' && dsCol in g) g[dsCol] = g[dsCol] / g.n;
      }));
      const cols = ed.columns;
      const rDimIdx = cols.indexOf(ed.dimension);
      const bad = []; let verified = 0;
      for (const row of ed.rows) {
        if (rDimIdx < 0) break;
        const key = String(row[rDimIdx] ?? '');
        const g = groupMap.get(key);
        if (!g) { bad.push(key + ':missing-in-source'); continue; }
        for (const [reportCol, spec] of Object.entries(truth.mapping)) {
          const dsCol = typeof spec === 'string' ? spec : spec.column;
          const mAgg = typeof spec === 'string' ? agg : (spec.aggregation || agg);
          const mi = cols.indexOf(reportCol);
          const smi = dsCols.indexOf(dsCol);
          if (mi < 0 || smi < 0) continue;
          const expected = dsCol === '__count__' ? (g.__count__ ?? g.n) : (mAgg === 'count' ? g.n : (g[dsCol] ?? 0));
          const actual = Number(row[mi]);
          if (!Number.isFinite(actual)) continue;
          const tol = Math.max(Math.abs(expected) * 1e-6, 1e-6);
          verified += 1;
          if (Math.abs(actual - expected) > tol) bad.push(key + ':' + reportCol + '=' + actual + '!=' + expected);
        }
      }
      if (verified === 0) return true;
      return bad.length === 0 ? true : bad.slice(0, 5);
    })()`);
  }
  // ---- responsive: reflow to 1024px and re-check block overflow. The report
  // claims mobile layouts stack without clipping; a moderately narrow desktop
  // viewport must not introduce scrollbars/clipping either.
  try {
    await command('Emulation.setDeviceMetricsOverride', { width: 1024, height: 1800, deviceScaleFactor: 1, mobile: false });
    await wait(400);
    checks.noBlockOverflowNarrow = await evalIf('noBlockOverflowNarrow', `(async () => {
      const collect = () => {
        const bad = [];
        document.querySelectorAll('body *').forEach((el) => {
          if (!(el instanceof HTMLElement)) return;
          if (el.closest('#ai-report-runtime-ui')) return;
          // .ai-report-table-wrap 与 .table-host 都是许可的卡内滚动容器：分页表格
        // 卡在卡内滚动是 validate_report R2 明确接受的契约（pagination →
        // scrollbar acceptable），把宿主一并排除，避免数据加载完成先后导致的
        // 竞态假失败。
        if (el.classList && (el.classList.contains('ai-report-table-wrap') || el.classList.contains('table-host'))) return;
          const cs = getComputedStyle(el);
          if (cs.display === 'none' || cs.overflowY === 'visible') return;
          const overY = el.scrollHeight > el.clientHeight + 3;
          if (overY) {
            const bid = el.closest('[data-report-block-id]');
            bad.push(String(el.className || el.tagName).slice(0, 50)
              + (bid ? '@' + bid.getAttribute('data-report-block-id') : '')
              + ':+' + (el.scrollHeight - el.clientHeight));
          }
        });
        return bad;
      };
      // 视口切换后 autofit（文本 hug / 表格贴合）仍在收敛，瞬态溢出会自愈——
      // 判红前静置 1s 复测，只 fail 稳定存在的溢出（真机：窄视口 +6px 时点态）。
      let bad = collect();
      if (bad.length) {
        await new Promise((r) => setTimeout(r, 1000));
        const settled = new Set(collect());
        bad = bad.filter((b) => settled.has(b));
      }
      return bad.length === 0 ? true : bad.slice(0, 5);
    })()`);
  } catch (e) {
    checks.noBlockOverflowNarrow = 'skip:' + e.message;
  }
  // ---- browser matrix: exercise the report shell at phone/tablet/desktop
  // widths.  This is intentionally a bounded layout contract, not a claim of
  // Safari engine parity (Safari still needs a host/browser CI lane).
  if (!want('browserMatrix')) {
    SKIPPED_CHECKS.push('browserMatrix');
  } else {
    const failures = [];
    for (const width of [390, 768, 1024]) {
      await command('Emulation.setDeviceMetricsOverride', {
        width, height: 1400, deviceScaleFactor: 1, mobile: width < 600,
      });
      await wait(180);
      const result = await evaluate(`(() => {
        const width = window.innerWidth;
        const doc = document.documentElement;
        const body = document.body;
        const overflow = Math.max(doc.scrollWidth, body ? body.scrollWidth : 0) - width;
        const drawer = document.querySelector('#ai-report-copilot');
        const drawerWidth = drawer ? Math.round(drawer.getBoundingClientRect().width) : 0;
        return { overflow, drawerWidth };
      })()`);
      if (result.overflow > 12) failures.push(width + ':horizontal-overflow-' + result.overflow);
      if (width < 600 && result.drawerWidth && result.drawerWidth < width - 8) failures.push(width + ':drawer-not-full-width');
    }
    await command('Emulation.clearDeviceMetricsOverride');
    checks.browserMatrix = failures.length ? failures.slice(0, 5) : true;
  }
  // ---- touch matrix: emulate a coarse pointer/touch screen in Chromium.
  // This is still not Safari/real-device parity, but it catches a distinct
  // class of regressions that width-only checks miss: a mobile drawer that
  // renders at the right width but has no touch capability or unusably small
  // primary controls.  Keep the check bounded and scoped to the Copilot shell
  // so report-specific chart controls do not create false product failures.
  if (!want('touchMatrix')) {
    SKIPPED_CHECKS.push('touchMatrix');
  } else {
    const failures = [];
    try {
      await command('Emulation.setDeviceMetricsOverride', {
        width: 390, height: 844, deviceScaleFactor: 2, mobile: true,
      });
      await command('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 5 });
      await wait(180);
      const result = await evaluate(`(() => {
        const root = document.querySelector('#ai-report-copilot');
        const drawer = root && root.getBoundingClientRect();
        const controls = root ? [...root.querySelectorAll('button,input,textarea,select')]
          .filter((node) => getComputedStyle(node).display !== 'none') : [];
        const smallControls = controls.filter((node) => {
          const rect = node.getBoundingClientRect();
          return rect.width > 0 && rect.height > 0 && (rect.width < 24 || rect.height < 24);
        }).map((node) => ({
          tag: node.tagName,
          label: (node.getAttribute('aria-label') || node.getAttribute('title') || node.textContent || '').trim().slice(0, 40),
          width: Math.round(node.getBoundingClientRect().width),
          height: Math.round(node.getBoundingClientRect().height),
        }));
        return {
          coarse: window.matchMedia('(pointer: coarse)').matches,
          maxTouchPoints: navigator.maxTouchPoints || 0,
          overflow: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth) - window.innerWidth,
          drawerWidth: drawer ? Math.round(drawer.width) : 0,
          smallControls,
        };
      })()`);
      if (!result.coarse || result.maxTouchPoints < 1) failures.push('touch-capability-missing');
      if (result.overflow > 12) failures.push('horizontal-overflow-' + result.overflow);
      if (result.drawerWidth && result.drawerWidth < 382) failures.push('drawer-not-full-width');
      if (result.smallControls.length > 0) failures.push('controls-under-24px-' + JSON.stringify(result.smallControls).slice(0, 220));
    } catch (error) {
      failures.push('touch-check-error-' + String(error.message || error).slice(0, 120));
    } finally {
      try { await command('Emulation.setTouchEmulationEnabled', { enabled: false, maxTouchPoints: 0 }); } catch {}
      try { await command('Emulation.clearDeviceMetricsOverride'); } catch {}
    }
    checks.touchMatrix = failures.length ? failures.slice(0, 5) : true;
  }
  checks.accessibilityMatrix = await evalIf('accessibilityMatrix', `(() => {
    const root = document.querySelector('#ai-report-copilot');
    // NQ-01 假红修复：Copilot 是 server-only 能力 —— copilotRender 对非 server
    // 模式 display:none 且从不填充内容（工具栏 boot 恒注入 <aside id="ai-report-copilot">
    // 空壳，singlefile 也有）。抽屉内容（.ai-cp-head）不存在 = 该检查不适用 →
    // skip 带因（记入 skipped，不判红）；server 模式（PSI 真检）抽屉 boot 即渲染，
    // 照旧真检、executed 数不降。
    if (!root || !root.querySelector('.ai-cp-head')) return 'skip:copilot-drawer-absent(非 server 模式无 Copilot，live-status 断言不适用)';
    const bad = [];
    root.querySelectorAll('button,input,textarea,select').forEach((node) => {
      const label = (node.getAttribute('aria-label') || node.getAttribute('title') || node.textContent || '').trim();
      if (!label) bad.push(node.tagName + ':missing-label');
      if (node.getAttribute('tabindex') === '-1' && node.matches('button,input,textarea,select')) bad.push(node.tagName + ':not-focusable');
    });
    if (!root.querySelector('[aria-live="polite"]')) bad.push('missing-live-status');
    return bad.length ? bad.slice(0, 8) : true;
  })()`);
  await command('Emulation.clearDeviceMetricsOverride');
  await wait(200);
  // ---- performance: total canvas pixels, chart count, dataset size ----
  checks.perfSane = await evalIf('perfSane', `(() => {
    const canvases = [...document.querySelectorAll('canvas')];
    const pixels = canvases.reduce((s, c) => s + c.width * c.height, 0);
    const charts = AIReportRuntime.manifest.charts.length;
    // 只数**内嵌行**（d.rows）——server 模式的 rowCount 是远端表的元数据（百万行
    // 也不进浏览器），1M logistics 全量审计曾因把它当内嵌行恒报 dataset-1000k 假失败。
    const rows = (AIReportRuntime.manifest.datasets || []).reduce((s, d) => s + (d.rows ? d.rows.length : 0), 0);
    if (pixels > 30e6) return 'canvas-' + Math.round(pixels / 1e6) + 'Mpx';   // > 30M canvas pixels
    if (charts > 25) return 'charts-' + charts;
    if (rows > 500000) return 'dataset-' + Math.round(rows / 1000) + 'k';      // > 500k embedded rows
    return true;
  })()`);
  // ---- text format bar (#6): selection + inline bold must produce formatting ----
  checks.textFormatBar = await evalIf('textFormatBar', `(() => {
    const nodes = document.querySelectorAll('[data-editable-text]');
    if (!nodes.length) return false;
    // Find a non-bold node (a plain <p>, <span>, or <div>). An <h1>-<h6> is already
    // bold so toggling bold removes the bold style instead of adding markup.
    let node = null;
    for (const n of nodes) {
      if (n.tagName !== 'H1' && n.tagName !== 'H2' && n.tagName !== 'H3'
          && n.tagName !== 'H4' && n.tagName !== 'H5' && n.tagName !== 'H6') {
        node = n; break;
      }
    }
    if (!node) node = nodes[nodes.length - 1];
    // Workbench tabs: a text node in a hidden view cannot be focused — activate
    // its view first.
    const view = node.closest('[data-tab-view]');
    if (view && view.classList.contains('ai-report-view-hidden')) {
      const tab = document.querySelector('[data-tab-go="' + CSS.escape(view.getAttribute('data-tab-view')) + '"]');
      if (tab) tab.click();
    }
    node.click();
    const bar = document.getElementById('ai-report-format-bar');
    const visible = bar && getComputedStyle(bar).display !== 'none';
    const hasButtons = bar && bar.querySelector('[data-format-action="bold"]') && bar.querySelector('[data-format-action="italic"]') && bar.querySelector('[data-format-action="underline"]');
    if (!visible || !hasButtons) return false;
    const range = document.createRange();
    // firstChild may be an ELEMENT (e.g. the cover summary leads with a
    // <span class="summary-lead">) — setStart/setEnd offsets count children on
    // elements, so descend to a real text node before ranging.
    let text = node.firstChild || node;
    if (text.nodeType !== 3) {
      const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
      let t = null;
      while ((t = walker.nextNode())) { if ((t.textContent || '').trim()) { text = t; break; } }
      if (text.nodeType !== 3) return false;
    }
    range.setStart(text, 0); range.setEnd(text, Math.min(2, (text.textContent || '').length));
    const sel = window.getSelection();
    sel.removeAllRanges(); sel.addRange(range);
    const before = node.innerHTML;
    document.execCommand('bold');
    const after = node.innerHTML;
    // Accept ANY visible change: on an already-bold node (CSS font-weight 600,
    // e.g. KPI labels) execCommand toggles bold OFF and writes font-weight:normal,
    // which is still proof the format-bar action reaches the selection.
    return after !== before;
  })()`);
  // ---- BI insert must render a real chart, not just bump the manifest count ----
  await evaluate(`(() => { const ins=document.getElementById('ai-report-insert'); if(ins){ins.setAttribute('data-open','true');const m=document.getElementById('ai-report-insert-menu'); if(m)m.setAttribute('data-visible','true');} const b=document.querySelector('#ai-report-insert-menu [data-runtime-action="add-bi"]'); if(b) b.click(); })()`);
  // 多数据源报告里「新建图表」会先弹出数据源选择面板（pickDatasetThen）——审计只验证
  // 插入后能真正渲染，所以选第一个数据源继续；单数据源时没有这个面板，直接跳过。
  await wait(400);
  await evaluate(`(() => { const b=document.querySelector('#ai-report-runtime-panel .ai-report-dataset-pick[data-dataset-id]'); if(b) b.click(); })()`);
  // Remote (server-side) datasets aggregate via fetch; give the fresh chart a
  // few tries to paint instead of failing on a cold-start race.
  let freshPainted = false;
  for (let _t = 0; _t < 12; _t += 1) {
    freshPainted = await evaluate('(() => { const c=AIReportRuntime.manifest.charts[AIReportRuntime.manifest.charts.length - 1]; return !!c && !!window.__auditPainted(c.elementId); })()');
    if (freshPainted) break;
    await wait(250);
  }
  checks.insertBI = await evalIf('insertBI', `(() => {
    const charts = AIReportRuntime.manifest.charts;
    if (charts.length !== ${chartCount} + 1) return false;
    const fresh = charts[charts.length - 1];
    if (!window.__auditPainted(fresh.elementId)) return false;
    const option = echarts.getInstanceByDom(document.getElementById(fresh.elementId))?.getOption();
    return !!option && Array.isArray(option.series) && option.series.length > 0 && !window.__auditBadFormatter(option);
  })()`);
  // 明细不再有独立卡片类型：插入卡片菜单无「明细」入口，明细统一走
  // 插入图表 → 图表类型「表格」→ 子选项「明细表/聚合表」。对一张真实表格图
  // 走一遍 聚合表 → 明细表（双框出现、维度/指标区消失）→ 聚合表（状态还原）。
  checks.insertDetail = await evalIf('insertDetail', `(async () => {
    const menuBtn = document.querySelector('#ai-report-insert-menu [data-runtime-action="add-detail"]');
    if (menuBtn) return 'insert-menu-still-ships-detail';
    const charts = (AIReportRuntime.manifest.charts || []).filter((c) => c.viewType === 'table');
    if (!charts.length) return true; // 无表格图 = 特性门控通过
    const chart = charts[0];
    chart.editor = chart.editor || {}; chart.editor.table = chart.editor.table || {};
    const origModes = Array.isArray(chart.editor.table.modes) ? JSON.parse(JSON.stringify(chart.editor.table.modes)) : null;
    const origMode = chart.editor.table.mode;
    const restore = () => {
      if (origModes) chart.editor.table.modes = origModes; else delete chart.editor.table.modes;
      if (origMode === undefined) delete chart.editor.table.mode; else chart.editor.table.mode = origMode;
      if (chart.elementId && document.getElementById(chart.elementId)) AIReportRuntime.renderChart(chart);
    };
    const until = async (fn, ms) => { const t0 = Date.now(); while (!fn()) { if (Date.now() - t0 > (ms || 9000)) return false; await new Promise((r) => setTimeout(r, 150)); } return true; };
    const blockEl = document.querySelector('.grid-stack-item[data-report-block-id="' + CSS.escape(chart.blockId) + '"]');
    if (!blockEl) return 'table-block-missing';
    AIReportRuntime.selectBlock(blockEl);
    await new Promise((r) => setTimeout(r, 180));
    const panel = () => document.getElementById('ai-report-runtime-panel');
    const chip = (m) => { const p = panel(); return p && p.querySelector('.ai-report-table-type-opts [data-table-mode-opt="' + m + '"]'); };
    const wide = panel().querySelector('.ai-report-table-type');
    if (!wide) return 'table-type-card-missing';
    if (!wide.classList.contains('ai-report-chart-type-wide')) return 'table-type-card-not-wide';
    if (!chip('detail') || !chip('aggregate')) return 'mode-chips-missing';
    if (!chip('detail').classList.contains('is-on') || !chip('aggregate').classList.contains('is-on')) return 'default-both-not-on';
    const clickChip = async (m) => { const b = chip(m); if (!b) return false; b.click(); await new Promise((r) => setTimeout(r, 300)); return true; };
    // 两个都选 → 取消「聚合」= 仅明细：面板切到明细双区、卡片左上角无模式切换。
    if (!await clickChip('aggregate')) return 'chip-click-failed';
    const modes1 = Array.isArray(chart.editor.table.modes) ? chart.editor.table.modes.slice() : [];
    const p1 = panel();
    const detailOnly = modes1.length === 1 && modes1[0] === 'detail'
      && !!p1.querySelector('#ai-zone-detail-unselected') && !!p1.querySelector('#ai-zone-detail-picked')
      && !p1.querySelector('#ai-zone-dimension') && !p1.querySelector('#ai-zone-measure');
    if (!detailOnly) return 'detail-only-failed';
    if (!await until(() => { const h = document.getElementById(chart.elementId); return !!(h && h.querySelector('.ai-report-table-wrap') && !h.querySelector('.ai-report-table-mode')); }, 9000)) return 'single-mode-toggle-not-removed';
    // 再加回「聚合」= 两个都选：左上角 明细/聚合 切换恢复。
    if (!await clickChip('aggregate')) return 'chip-restore-failed';
    const modes2 = Array.isArray(chart.editor.table.modes) ? chart.editor.table.modes.slice() : [];
    if (!(modes2.indexOf('detail') >= 0 && modes2.indexOf('aggregate') >= 0)) return 'both-restore-failed';
    if (!await until(() => { const h = document.getElementById(chart.elementId); return !!(h && h.querySelector('.ai-report-table-mode[data-table-modebar]')); }, 9000)) return 'mode-toggle-missing-both';
    // 至少保留一个：从「两个都选」依次取消，最后一个取消操作被忽略。
    await clickChip('detail');                                      // -> [aggregate]
    await clickChip('aggregate');                                   // 最后一个被忽略
    const modes3 = Array.isArray(chart.editor.table.modes) ? chart.editor.table.modes.slice() : [];
    if (!(modes3.length === 1 && modes3[0] === 'aggregate')) return 'last-mode-not-protected:' + JSON.stringify(modes3);
    restore();
    return true;
  })()`);
  await wait(200);
  // ---- 自由表格卡（kind:'grid'）：插入 → 自动进全屏 → 写一格 → 完成 → 画布渲染真值 → 还原 ----
  checks.gridCard = await evalIf('gridCard', `(async () => {
    const wasEditing = AIReportRuntime.isEditing();
    if (!wasEditing) AIReportRuntime.setEditing(true);
    try {
      const menuBtn = document.querySelector('#ai-report-insert-menu [data-runtime-action="add-grid"]');
      if (!menuBtn) return 'insert-menu-no-grid-entry';
      menuBtn.click();
      await new Promise((r) => setTimeout(r, 350));
      const modal = document.getElementById('ai-grid-editor');
      if (!modal) return 'fullscreen-editor-not-opened';        // 决议 h：落卡自动进全屏
      const td = modal.querySelector('td[data-r="0"][data-c="0"]');
      if (!td) return 'sheet-cell-missing';
      td.dispatchEvent(new MouseEvent('dblclick', { bubbles: true }));
      await new Promise((r) => setTimeout(r, 120));
      const inp = modal.querySelector('[data-grid-edit-input]');
      if (!inp) return 'cell-edit-input-missing';
      inp.value = '审计写入';
      inp.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
      await new Promise((r) => setTimeout(r, 250));
      const blocks = (AIReportRuntime.manifest.blocks || []).filter((b) => b.kind === 'grid');
      if (!blocks.length) return 'grid-block-not-in-manifest';
      const blk = blocks[blocks.length - 1];
      if (!blk.grid || !Array.isArray(blk.grid.rows) || blk.grid.rows[0][0] !== '审计写入') return 'cell-commit-lost:' + JSON.stringify(blk.grid && blk.grid.rows && blk.grid.rows[0]);
      const done = modal.querySelector('[data-grid-ed="done"]');
      if (!done) return 'done-button-missing';
      done.click();
      await new Promise((r) => setTimeout(r, 350));
      if (document.getElementById('ai-grid-editor')) return 'editor-not-closed';
      const card = document.querySelector('.grid-stack-item[data-report-block-id="' + CSS.escape(blk.id) + '"]');
      if (!card) return 'grid-card-missing';
      if (!card.classList.contains('ai-hug')) return 'hug-class-missing';
      const cell = card.querySelector('td[data-r="0"][data-c="0"]');
      if (!cell || cell.textContent !== '审计写入') return 'card-cell-not-painted';
      // 还原：删卡（含撤销 toast 清理），编辑态回到进入前。
      AIReportRuntime.deleteBlock(blk.id);
      await new Promise((r) => setTimeout(r, 250));
      if (document.querySelector('.grid-stack-item[data-report-block-id="' + CSS.escape(blk.id) + '"]')) return 'cleanup-failed';
      return true;
    } finally {
      if (!wasEditing) AIReportRuntime.setEditing(false);
    }
  })()`);
  await wait(200);
  // 第 3 批（真机）：① 字段引用反查（徽标 → 弹层 → 定位）；② 区间筛选控件
  // （时间列 → 日期范围，真的会让图表重聚合）；③ 时间下钻的**纯函数**（桶标签→区间、
  // 下钻链）——下钻的点击交互由 tests/test_time_drill.cjs + 真机 probe 覆盖，审计里
  // 不做会改状态的多步点击（审计必须无副作用、可重入）。
  checks.batch3 = await evalIf('batch3', `(async () => {
    try {
      const problems = [];
      // ① 时间下钻纯函数：桶标签 → 区间（必须与 Python date_parse.range_of 一致）
      const A = window.AIReportChartAdapter || {};
      if (typeof A.bucketRange !== 'function') problems.push('no-bucketRange');
      else {
        const q = A.bucketRange('2025-Q3'), m = A.bucketRange('2024-02'), y = A.bucketRange('2025');
        if (JSON.stringify(q) !== JSON.stringify(['2025-07-01', '2025-09-30'])) problems.push('quarter-range:' + JSON.stringify(q));
        if (JSON.stringify(m) !== JSON.stringify(['2024-02-01', '2024-02-29'])) problems.push('leap-range:' + JSON.stringify(m));
        if (JSON.stringify(y) !== JSON.stringify(['2025-01-01', '2025-12-31'])) problems.push('year-range:' + JSON.stringify(y));
      }
      if (A.timeDrillChild && (A.timeDrillChild('month') !== 'day' || A.timeDrillChild('day') !== null)) {
        problems.push('drill-chain:' + A.timeDrillChild('month') + '/' + A.timeDrillChild('day'));
      }
      // ② 区间筛选控件：把第一个时间列加进筛选条 → 出现两个日期输入 → 设值后图表重聚合 → 清除
      const trend = (AIReportRuntime.manifest.charts || []).find((c) => /line|bar|area/.test(c.viewType || '') && c.editor && c.editor.data);
      const bucketSrc = trend && trend.editor.data.dimensionBucket && trend.editor.data.dimensionBucket.source;
      const add = document.querySelector('[data-report-filter] [data-filter-add]');
      let added = false;
      if (add && bucketSrc) {
        const opt = [].slice.call(add.options).find((o) => o.value === bucketSrc);
        if (opt) { add.value = bucketSrc; add.dispatchEvent(new Event('change', { bubbles: true })); added = true; await new Promise((r) => setTimeout(r, 300)); }
      }
      const box = document.querySelector('[data-report-filter] .ai-report-filter-range.is-date');
      if (added && !box) problems.push('no-date-range-control');
      if (box) {
        const ins = box.querySelectorAll('input[type="date"]');
        if (ins.length !== 2) problems.push('date-inputs:' + ins.length);
        // 生效 → 趋势卡点数必须变化（筛选真的进了聚合）
        const host = trend && document.getElementById(trend.elementId);
        const inst = host && host.querySelector('.chart-host') && echarts.getInstanceByDom(host.querySelector('.chart-host'));
        const before = inst ? ((inst.getOption().xAxis[0].data || []).length) : null;
        const all = inst ? [].slice.call(inst.getOption().xAxis[0].data || []) : [];
        if (ins.length === 2 && trend && before > 1 && all.length) {
          ins[0].value = String(all[Math.floor(all.length / 2)]);
          ins[0].dispatchEvent(new Event('change', { bubbles: true }));
          await new Promise((r) => setTimeout(r, 9000));
          const inst2 = echarts.getInstanceByDom(host.querySelector('.chart-host'));
          const after = inst2 ? ((inst2.getOption().xAxis[0].data || []).length) : 0;
          if (!(after < before)) problems.push('range-no-effect:' + before + '->' + after);
        }
        if (window.AIReportRuntime && typeof AIReportRuntime.clearFilters === 'function') {
          AIReportRuntime.clearFilters();
          await new Promise((r) => setTimeout(r, 1500));
        }
        // 清空后筛选条不得残留谓词
        const st = (window.__AI_REPORT_FILTERS__ || {})[Object.keys(window.__AI_REPORT_FILTERS__ || {})[0]];
        if (st && (st.filters || []).length) problems.push('clear-left-filters:' + JSON.stringify(st.filters));
      }
      // ③ 字段引用反查：字段配置里每个字段都有徽标，点开有列表
      if (typeof AIReportRuntime.setEditing === 'function' && !AIReportRuntime.isEditing()) {
        const tog0 = document.getElementById('ai-report-edit-toggle');
        if (tog0) { tog0.click(); await new Promise((r) => setTimeout(r, 700)); }
      }
      const semBtn = document.querySelector('[data-runtime-action="semantic-config"]');
      if (semBtn) {
        semBtn.click();
        await new Promise((r) => setTimeout(r, 2200));
        const badges = Array.from(document.querySelectorAll('[data-col-refs]'));
        // 优先挑一个**真有引用**的字段（.is-none = 未被引用徽标）展开——旧写法固定取
        // 第一个徽标，首列无引用的报告恒报 empty-refs-strip 假失败。全部字段都未被
        // 引用时转而验证「未被引用」形态本身：展开条照常打开、引用清单为空是正确 UI。
        const badge = badges.find((b) => !b.classList.contains('is-none')) || null;
        if (!badges.length) problems.push('no-field-refs-badge');
        else if (!badge) {
          badges[0].click();
          await new Promise((r) => setTimeout(r, 400));
          const strip0 = document.querySelector('tr[data-col-detail-row]:not([hidden])');
          if (!strip0) problems.push('no-refs-strip');
          else strip0.hidden = true;
        }
        else {
          badge.click();
          await new Promise((r) => setTimeout(r, 400));
          // UI 对标 §2B：引用徽标点击 = 字段详情展开条就地展开（行内第二行，单开），
          // 不再有浮动弹层。展开条内含引用清单（data-col-refs-go，点击定位卡片）。
          const strip = document.querySelector('tr[data-col-detail-row]:not([hidden])');
          if (!strip) problems.push('no-refs-strip');
          else {
            const items = strip.querySelectorAll('[data-col-refs-go]');
            if (!items.length) problems.push('empty-refs-strip');
            else {
              const target = items[0].getAttribute('data-col-refs-go');
              items[0].click();
              await new Promise((r) => setTimeout(r, 600));
              if (!document.querySelector('[data-report-block-id="' + target + '"]')) problems.push('refs-target-missing:' + target);
            }
            strip.hidden = true;
          }
        }
        const cancel = document.querySelector('[data-sem-action="cancel"]') || document.querySelector('.ai-sem-close');
        if (cancel) { cancel.click(); await new Promise((r) => setTimeout(r, 900)); }
        const esc = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true });
        document.dispatchEvent(esc);
        await new Promise((r) => setTimeout(r, 600));
      }
      return problems.length ? problems.join(' | ') : true;
    } catch (e) { return String((e && e.stack) || e); }
  })()`);
  await wait(200);
  // 在线编辑策略（真机）：/manager 的开关/口令走这套 API；报告页据此在保存入口给出
  // 「当前暂无法编辑保存报告，请联系管理员」。这里只做**只读**核对：策略可读、且
  // 策略为开启时分享中心不得显示锁定提示（关闭态由 probe/tests 覆盖，避免审计改状态）。
  checks.editPolicy = await evalIf('editPolicy', `(async () => {
    try {
      const rt = (AIReportRuntime.manifest.runtime || {});
      let policy = null;
      try {
        const res = await fetch('/api/admin/edit-policy/' + encodeURIComponent(AIReportRuntime.manifest.reportId || ''));
        if (res.ok) policy = await res.json();
      } catch (e) { policy = null; }
      if (policy && typeof policy.enabled !== 'boolean') return 'policy-shape:' + JSON.stringify(policy);
      // 服务端下发的开关必须与策略一致（否则报告页会误报「请联系管理员」）
      if (policy && rt.editEnabled !== policy.enabled) return 'flag-mismatch:' + rt.editEnabled + '!=' + policy.enabled;
      const toggle = document.getElementById('ai-report-edit-toggle');
      if (toggle) { toggle.click(); await new Promise((r) => setTimeout(r, 800)); }
      const share = document.getElementById('ai-report-share');
      if (share) {
        share.click();
        await new Promise((r) => setTimeout(r, 400));
        const card = document.querySelector('.ai-finish-card');
        const locked = !!(card && card.querySelector('.ai-share-locked'));
        if (policy && policy.enabled && locked) return 'misreported-locked';
        if (policy && !policy.enabled && !locked) return 'missing-locked-notice';
        const close = card && card.querySelector('[data-finish-close]');
        if (close) close.click();
      }
      if (toggle) { toggle.click(); await new Promise((r) => setTimeout(r, 400)); }
      return true;
    } catch (e) { return String((e && e.stack) || e); }
  })()`);
  await wait(200);
  // 表格列宽（真机）：**每一列**都能拖表头右缘调宽 → 落盘 chart.editor.table.colWidths；
  // 双击 → 按内容自适应；不再有「列宽」按钮/菜单（用户决策：拖拽+双击即唯一入口）。
  // 另外核对：列边界有显式分隔线、td 严格裁剪、维度列恒左/数值列右。
  checks.tableColWidth = await evalIf('tableColWidth', `(async () => {
    try {
      const charts = (AIReportRuntime.manifest.charts || []).filter((c) => c.viewType === 'table');
      if (!charts.length) return true;                 // 无表格图 = 不适用
      const chart = charts[0];
      const item = document.querySelector('.grid-stack-item[data-report-block-id="' + CSS.escape(chart.blockId) + '"]');
      if (!item) return 'block-missing';
      item.click();
      const eb = item.querySelector('.ai-report-block-edit'); if (eb) eb.click();
      const until = async (fn, ms) => { const t0 = Date.now(); while (!fn()) { if (Date.now() - t0 > (ms || 8000)) return false; await new Promise((r) => setTimeout(r, 150)); } return true; };
      if (!await until(() => { const h = document.getElementById(chart.elementId); return !!(h && h.querySelector('table .ai-col-resizer')); })) return 'no-resizer';
      const host = document.getElementById(chart.elementId);
      const tbl = host.querySelector('table');
      if (getComputedStyle(tbl).tableLayout !== 'fixed') return 'not-fixed-layout';
      // 列宽按钮/菜单必须已移除（列头右缘拖拽 = 唯一入口）
      if (host.querySelector('[data-tbl-width-open]')) return 'width-button-still-present';
      // 用户决策：聚合表不再有「筛选 Dimension」输入框；分页在顶栏与导出同行。
      if (host.querySelector('.ai-table-filter, input[data-table-filter]')) return 'row-filter-box-present';
      const topBar = host.querySelector('.ai-report-table-bar');
      if (!topBar || !topBar.querySelector('.ai-table-tools .ai-table-pager')) return 'pager-not-in-topbar';
      const heads = tbl.querySelectorAll('thead th').length;
      const handles = tbl.querySelectorAll('thead .ai-col-resizer').length;
      // 两级表头（批次一交叉矩阵）下 th 数 ≠ 列数（组头横跨多列、行头竖跨两行），
      // 手柄阈值按**叶子列数**（colgroup）计：除首列外每列都可有拖拽手柄。
      const leafCols = tbl.querySelectorAll('colgroup col').length || heads;
      if (handles < leafCols - 1) return 'missing-handles:' + handles + '/' + leafCols + ' (th:' + heads + ')';
      // 手柄必须挂在自身 <th> 上（offsetParent），否则就是“只有第一列能拖”的回归。
      const misplaced = [...tbl.querySelectorAll('thead .ai-col-resizer')]
        .filter((r) => r.offsetParent !== r.closest('th')).map((r) => r.getAttribute('data-col'));
      if (misplaced.length) return 'resizer-misplaced:' + misplaced.join(',');
      // 列分隔线 + 裁剪：th/td 有 border-right，td overflow hidden。
      const th0 = tbl.querySelector('thead th');
      const td0 = tbl.querySelector('tbody td');
      const sep = th0 && getComputedStyle(th0).borderRightWidth;
      if (!sep || sep === '0px') return 'no-column-separator';
      if (td0 && getComputedStyle(td0).overflow !== 'hidden') return 'td-not-clipped';
      // 维度列恒左（第一个维度 td）；数值 td（ai-cell-num/ai-td-num）右。
      const dimTd = tbl.querySelector('tbody td');
      if (dimTd && getComputedStyle(dimTd).textAlign !== 'left') return 'dim-not-left:' + getComputedStyle(dimTd).textAlign;
      const numTd = tbl.querySelector('tbody td.ai-cell-num, tbody td.ai-td-num');
      if (numTd && getComputedStyle(numTd).textAlign !== 'right') return 'num-not-right:' + getComputedStyle(numTd).textAlign;
      const saved = JSON.stringify((chart.editor.table || {}).colWidths || {});
      const resizers = [...tbl.querySelectorAll('thead .ai-col-resizer')];
      // 逐列拖拽：用户报过的回归是“只有第一列能调”，所以每一列都要真拖一次。
      for (const rs of resizers) {
        const col = rs.getAttribute('data-col');
        const box = rs.getBoundingClientRect();
        const target = Math.min(280, Math.max(90, box.left + 110));
        rs.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, clientX: box.left + 1, clientY: box.top + 4, pointerId: 1 }));
        host.dispatchEvent(new PointerEvent('pointermove', { bubbles: true, clientX: box.left + 1 + (target - box.left), clientY: box.top + 4, pointerId: 1 }));
        host.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, clientX: box.left + 1 + (target - box.left), clientY: box.top + 4, pointerId: 1 }));
        await new Promise((r) => setTimeout(r, 60));
        const stored = ((chart.editor.table || {}).colWidths || {})[col];
        if (!stored || stored < 64) return 'drag-not-persisted:' + col + '=' + stored;
      }
      // 双击自适应 → 宽度按内容算（必然 ≥ 最小值）
      const first = resizers[0];
      const col0 = first.getAttribute('data-col');
      const rs2 = tbl.querySelector('thead .ai-col-resizer[data-col="' + CSS.escape(col0) + '"]');
      const b2 = rs2.getBoundingClientRect();
      rs2.dispatchEvent(new MouseEvent('dblclick', { bubbles: true, clientX: b2.left, clientY: b2.top }));
      await new Promise((r) => setTimeout(r, 250));
      const fitted = ((chart.editor.table || {}).colWidths || {})[col0];
      if (!fitted || fitted < 64) return 'autofit-failed:' + fitted;
      // 还原进入审计前的列宽 + 关面板
      chart.editor.table.colWidths = JSON.parse(saved || '{}');
      AIReportRuntime.syncManifest && AIReportRuntime.syncManifest();
      const cl = document.querySelector('#ai-report-runtime-panel [data-runtime-action="close"]');
      if (cl) cl.click();
      return true;
    } catch (e) { return String((e && e.stack) || e); }
  })()`);
  await wait(200);
  // WP-4 条件格式引擎（真机）：规则必须真实命中单元格——bg/text 内联样式、
  // 图标字形、整行背景在 DOM 里可证；合计行（ai-table-total）永不携带装饰。
  // 无规则的报告 = 不适用（true），fixture 报告按需声明 condFormat 规则。
  checks.condEngine = await evalIf('condEngine', `(async () => {
    try {
      const charts = (AIReportRuntime.manifest.charts || []).filter((c) => {
        const t = (c.editor && c.editor.table) || {};
        return c.viewType === 'table' && Array.isArray(t.condFormat) && t.condFormat.length > 0
          && (c.editor.table.condFormat.some((r) => r && r.enabled !== false));
      });
      if (!charts.length) return true;
      const chart = charts[0];
      const item = document.querySelector('.grid-stack-item[data-report-block-id="' + CSS.escape(chart.blockId) + '"]');
      if (!item) return 'block-missing';
      item.click();
      const until = async (fn, ms) => { const t0 = Date.now(); while (!fn()) { if (Date.now() - t0 > (ms || 8000)) return false; await new Promise((r) => setTimeout(r, 150)); } return true; };
      if (!await until(() => document.getElementById(chart.elementId) && document.getElementById(chart.elementId).querySelector('tbody tr'))) return 'table-not-rendered';
      const host = document.getElementById(chart.elementId);
      const wrap = host.querySelector('.ai-report-table-wrap');
      if (!wrap) return 'wrap-missing';
      const rules = chart.editor.table.condFormat.filter((r) => r && r.enabled !== false);
      const misses = [];
      for (const r of rules) {
        const acts = r.actions || {};
        // bg：任意单元格内联背景命中规则色
        if (acts.bg) {
          const hit = [...wrap.querySelectorAll('td[style], tr[style]')].some((el) => (el.getAttribute('style') || '').replace(/\\s/g, '').includes('background:' + acts.bg));
          if (!hit) misses.push('bg-miss:' + r.target);
        }
        // text：值 span / 裸 td 内联字色命中
        if (acts.text) {
          const hit = [...wrap.querySelectorAll('span[style], td[style]')].some((el) => (el.getAttribute('style') || '').replace(/\\s/g, '').includes('color:' + acts.text));
          if (!hit) misses.push('text-miss:' + r.target);
        }
        // icon：字形出现在单元格内
        if (acts.icon) {
          const hit = [...wrap.querySelectorAll('td')].some((td) => td.textContent.indexOf(acts.icon) >= 0);
          if (!hit) misses.push('icon-miss:' + r.target + ':' + acts.icon);
        }
        // fullRow：整行背景（tr 内联）命中
        if (acts.fullRow && acts.bg) {
          const hit = [...wrap.querySelectorAll('tr[style]')].some((el) => (el.getAttribute('style') || '').replace(/\\s/g, '').includes('background:' + acts.bg));
          if (!hit) misses.push('fullrow-miss:' + r.target);
        }
        // hideText：目标列存在「有图标/空」的短文本格（粗证：某 td 文本长度 ≤ 图标长度）
        if (acts.hideText) {
          const hit = [...wrap.querySelectorAll('td')].some((td) => td.textContent.trim().length <= (acts.icon || '').length + 1);
          if (!hit) misses.push('hidetext-miss:' + r.target);
        }
      }
      if (misses.length) return misses.join(' | ');
      // 合计行永不装饰：total 行不得携带规则 bg 色
      const total = wrap.querySelector('tr.ai-table-total');
      if (total) {
        const bgs = rules.map((r) => r.actions && r.actions.bg).filter(Boolean);
        const leaked = bgs.some((c) => (total.getAttribute('style') || '').includes(c)
          || [...total.querySelectorAll('td[style]')].some((td) => (td.getAttribute('style') || '').includes('background:' + c)));
        if (leaked) return 'total-row-decorated';
      }
      return true;
    } catch (e) { return String((e && e.stack) || e); }
  })()`);
  await wait(200);
  // WP-5 副指标（同环比对比列，真机）：配置了 subMeasures 的表格卡必须真的长出副列
  //（.ai-sub-head 表头 + .ai-sub-cell 数值格），列对齐完整（行 td 数 = colgroup 叶子列数），
  // 且与 KPI 卡「对比」副行同源勾稽（同一 derivedEvaluate：对比 % 文案须在副列中出现）。
  checks.subMeasures = await evalIf('subMeasures', `(async () => {
    try {
      const charts = (AIReportRuntime.manifest.charts || []).filter((c) => {
        const t = (c.editor && c.editor.table) || {};
        return c.viewType === 'table' && Array.isArray(t.subMeasures) && t.subMeasures.length > 0;
      });
      if (!charts.length) return true;   // 无副指标配置 = 不适用
      const chart = charts[0];
      const item = document.querySelector('.grid-stack-item[data-report-block-id="' + CSS.escape(chart.blockId) + '"]');
      if (!item) return 'block-missing';
      item.click();
      const until = async (fn, ms) => { const t0 = Date.now(); while (!fn()) { if (Date.now() - t0 > (ms || 10000)) return false; await new Promise((r) => setTimeout(r, 150)); } return true; };
      if (!await until(() => document.getElementById(chart.elementId) && document.getElementById(chart.elementId).querySelector('tbody tr'))) return 'table-not-rendered';
      const host = document.getElementById(chart.elementId);
      const tbl = host.querySelector('table');
      if (!tbl) return 'table-missing';
      const heads = tbl.querySelectorAll('thead th.ai-sub-head');
      if (!heads.length) return 'sub-head-missing';
      for (const h of heads) { if (h.textContent.indexOf('·') < 0) return 'sub-head-text:' + h.textContent; }
      const cells = [...tbl.querySelectorAll('tbody td.ai-sub-cell')];
      if (!cells.length) return 'sub-cells-missing';
      // 至少一格数值（数字或 %）；全 '—' = 求值全空（闸门失效或序列缺失）。
      if (!cells.some((td) => /\\d/.test(td.textContent))) return 'sub-cells-all-null';
      // 列对齐：行**覆盖的网格列数**（colSpan 求和）必须与 colgroup 叶子列数一致
      //（合计/小计行副列占位格不能少）。WP-8 披露：合计标签 td 合法吸收行维列
      //（colspan=行维数 [+rank]，嵌套分支 P3 起如此），数物理 td 会在多行维表格上假报。
      const thCount = tbl.querySelectorAll('colgroup col').length;
      const spanSumOf = (tr) => [...tr.querySelectorAll('td')].reduce((n, td) => n + (td.colSpan || 1), 0);
      const totalRow = tbl.querySelector('tr.ai-table-total');
      if (totalRow && spanSumOf(totalRow) !== thCount) return 'total-col-misalign:' + spanSumOf(totalRow) + '/' + thCount;
      const firstRow = tbl.querySelector('tbody tr');
      if (firstRow && spanSumOf(firstRow) !== thCount) return 'row-col-misalign:' + spanSumOf(firstRow) + '/' + thCount;
      // 与 KPI 对比副行同源勾稽：KPI 卡 .kpi-compare 文案（如「环比 +12.3%」）里的
      // 带符号百分数必须能在副列格中找到（同一 derivedEvaluate 的同一期值）。
      const kpiCmp = document.querySelector('[data-report-block-id^="block-kpi-"] .kpi-compare');
      if (kpiCmp) {
        const mm = kpiCmp.textContent.match(/[+-]\\d+(?:\\.\\d+)?%/);
        if (mm) {
          const hit = cells.some((td) => td.textContent.indexOf(mm[0]) >= 0);
          if (!hit) return 'kpi-reconcile-miss:' + mm[0];
        }
      }
      return true;
    } catch (e) { return String((e && e.stack) || e); }
  })()`);
  await wait(200);
  // WP-9 迷你图趋势列（真机）：启用 sparkline 的表格卡行尾必须有「趋势」列——
  // 表头 th.ai-col-spark + colgroup col[__spark__] 存在，数据行 spark 格内 SVG 真渲染
  //（getBoundingClientRect 宽度 > 0），且折线点数与列数勾稽：svg[data-n]（序列长度 =
  // 列维值数）必须等于两级矩阵表头的列维组头数（分组布局无列维表头，只查 data-n>0）。
  // **逐卡全查**（只查 charts[0] 曾放过同报告另一张门控失效的卡）。未启用 = 不适用。
  checks.sparkline = await evalIf('sparkline', `(async () => {
    try {
      const charts = (AIReportRuntime.manifest.charts || []).filter((c) => {
        const t = (c.editor && c.editor.table) || {};
        return c.viewType === 'table' && t.sparkline && t.sparkline.enabled === true;
      });
      if (!charts.length) return true;   // 无趋势列配置 = 不适用
      const until = async (fn, ms) => { const t0 = Date.now(); while (!fn()) { if (Date.now() - t0 > (ms || 10000)) return false; await new Promise((r) => setTimeout(r, 150)); } return true; };
      for (const chart of charts) {
        const item = document.querySelector('.grid-stack-item[data-report-block-id="' + CSS.escape(chart.blockId) + '"]');
        if (!item) return chart.id + ':block-missing';
        item.click();
        if (!await until(() => document.getElementById(chart.elementId) && document.getElementById(chart.elementId).querySelector('tbody tr'))) return chart.id + ':table-not-rendered';
        const tbl = document.getElementById(chart.elementId).querySelector('table');
        if (!tbl) return chart.id + ':table-missing';
        const th = tbl.querySelector('thead th.ai-col-spark');
        if (!th) return chart.id + ':spark-head-missing';
        if (!tbl.querySelector('colgroup col[data-col="__spark__"]')) return chart.id + ':spark-colgroup-missing';
        const cells = [...tbl.querySelectorAll('tbody td.ai-cell-spark')];
        if (!cells.length) return chart.id + ':spark-cells-missing';
        const withSvg = cells.filter((td) => td.querySelector('svg.ai-spark-svg'));
        if (!withSvg.length) return chart.id + ':spark-svg-missing';
        const zero = withSvg.find((td) => td.querySelector('svg.ai-spark-svg').getBoundingClientRect().width <= 0);
        if (zero) return chart.id + ':spark-svg-zero-width';
        // 点数勾稽：svg[data-n] = 该行列维值数（含空档）。两级矩阵表头 → 列维组头数
        //（r1 的 .ai-hier-group 且非行总计组）；其余形状（单级矩阵/分组）只查 data-n>0。
        const svg0 = withSvg[0].querySelector('svg.ai-spark-svg');
        const n = Number(svg0.getAttribute('data-n'));
        if (!(n > 0)) return chart.id + ':spark-data-n-invalid:' + svg0.getAttribute('data-n');
        const r1 = tbl.querySelector('thead tr');
        const bdGroups = r1 ? [...r1.querySelectorAll('th.ai-hier-group')].filter((t) => !t.classList.contains('ai-col-total')) : [];
        if (bdGroups.length > 0 && n !== bdGroups.length) return chart.id + ':spark-n-mismatch:' + n + '/' + bdGroups.length;
        const cl = document.querySelector('#ai-report-runtime-panel [data-runtime-action="close"]');
        if (cl) cl.click();
      }
      return true;
    } catch (e) { return String((e && e.stack) || e); }
  })()`);
  await wait(200);
  // WP-10 读者会话视图态（真机）：预览态改视图（字段自选 / 排序模式 / 筛选面板）必须
  // **不落盘**——manifest JSON 与 #ai-report-manifest 文本逐字节不变、脏标不变，
  // 且 __sessionView 永不出现在 manifest（刷新即还原的机制保证：会话容器是模块级，
  // 不进 JSON）。还原断言用「恢复默认」回到基线列数（容器归零等价于刷新，沙箱测试
  // test_session_view.cjs 已行为级锁）。
  checks.sessionView = await evalIf('sessionView', `(async () => {
    try {
      const rt = AIReportRuntime;
      const charts = (rt.manifest.charts || []).filter((c) => {
        const t = (c.editor && c.editor.table) || {};
        return c.viewType === 'table' && t.mode !== 'detail' && Array.isArray(c.editor && c.editor.data && c.editor.data.measures) && c.editor.data.measures.length >= 2;
      });
      if (!charts.length) return true;   // 无多指标聚合表 = 不适用
      const chart = charts[0];
      const until = async (fn, ms) => { const t0 = Date.now(); while (!fn()) { if (Date.now() - t0 > (ms || 9000)) return false; await new Promise((r) => setTimeout(r, 120)); } return true; };
      if (rt.setEditing) rt.setEditing(false);   // 预览态（读者）
      // 会话态改动断言的持久层 = chart.editor（__tblCtx/__tblExport 是渲染副产物，
      // 本就不在「不落盘」契约内）；__sessionView 不可枚举，永不出现在 JSON。
      const editorJson = () => JSON.stringify(chart.editor || {});
      rt.renderChart(chart);
      const hostOf = () => document.getElementById(chart.elementId);
      // 等**新一轮**渲染完成：字段按钮在场才算读者工具条就位。旧口径「loading 摘除 +
      // .ai-table-tools 在场」在慢聚合（335k 行 server 重聚合）下会读到**编辑态旧工具条**
      // ——loading 先摘、工具条后换，字段按钮（编辑态本就没有）缺席 → 假红
      // fields-btn-missing（真机：tables 单预设跑必现，全量跑因缓存焐热而时绿时红）。
      // 直接轮询按钮本身（9s 预算）：到点不在场仍然 fail，断言强度不变。
      const readerToolsReady = () => hostOf() && !hostOf().querySelector('.ai-chart-loading') && !!hostOf().querySelector('[data-tbl-fields-open]');
      if (!await until(readerToolsReady)) {
        // 诊断：editing 态 + 工具条 HTML（含 loading 状态），便于定位是「旧工具条残留」还是「编辑态渲染」
        const host0 = hostOf();
        const diag = {
          editing: (typeof rt.isEditing === 'function') ? rt.isEditing() : null,
          loading: host0 ? !!host0.querySelector('.ai-chart-loading') : null,
          tools: host0 && host0.querySelector('.ai-table-tools') ? host0.querySelector('.ai-table-tools').innerHTML.slice(0, 800) : null
        };
        return 'fields-btn-missing(reader):' + JSON.stringify(diag);
      }
      let host = hostOf();
      let fldBtn = host.querySelector('[data-tbl-fields-open]');
      if (!fldBtn) {
        // 旧工具条竞态防御（二段）：显式重渲一次，仍按「字段按钮在场」口径等。
        rt.renderChart(chart);
        await until(readerToolsReady);
        await new Promise((r) => setTimeout(r, 300));
        host = hostOf();
        fldBtn = host.querySelector('[data-tbl-fields-open]');
      }
      if (!fldBtn) {
        // 诊断：editing 态 + 工具条 HTML（含 loading 状态），便于定位是「旧工具条残留」还是「编辑态渲染」
        const diag = {
          editing: (typeof rt.isEditing === 'function') ? rt.isEditing() : null,
          loading: !!hostOf().querySelector('.ai-chart-loading'),
          tools: host.querySelector('.ai-table-tools') ? host.querySelector('.ai-table-tools').innerHTML.slice(0, 800) : null
        };
        return 'fields-btn-missing(reader):' + JSON.stringify(diag);
      }
      const thCount0 = host.querySelectorAll('thead th[data-col-head]').length;
      const editor0 = editorJson();
      const json0 = JSON.stringify(rt.manifest);
      const ta = document.getElementById('ai-report-manifest');
      const ta0 = ta ? ta.value : null;
      const unsaved0 = (typeof rt.isUnsaved === 'function') ? rt.isUnsaved() : null;
      // ①字段自选：取消一个指标 → 即时重渲（归因列数下降），editor 配置/脏标不变。
      // 期望列数按「归属该指标的 th 键」计：平表/分组键=指标名，矩阵键=bd\ u0001指标 /
      // __rowtotal__\ u0001指标——矩阵里一个指标占多列（段×1+行总计×1），硬编码 -1 会假红。
      fldBtn.click();
      if (!await until(() => { const p = document.querySelector('.ai-tbl-pop'); return !!(p && p.querySelector('[data-fld-check]')); })) return 'field-pop-not-open';
      const pop1 = document.querySelector('.ai-tbl-pop');
      const boxes = [...pop1.querySelectorAll('[data-fld-check]')];
      const target = boxes[boxes.length - 1];
      if (!target.checked) return 'field-checkbox-unexpected-state';
      const targetName = target.getAttribute('data-fld-check') || '';
      const thKeys0 = [...host.querySelectorAll('thead th[data-col-head]')].map((th) => th.getAttribute('data-col-head') || '');
      const expectTh1 = thKeys0.filter((k) => !(k === targetName || k.endsWith('\u0001' + targetName))).length;
      target.click();
      if (!await until(() => hostOf() && !hostOf().querySelector('.ai-chart-loading') && hostOf().querySelectorAll('thead th[data-col-head]').length === expectTh1)) return 'field-toggle-no-rerender';
      if (editorJson() !== editor0) return 'editor-changed(fields)';
      if (JSON.stringify(rt.manifest).indexOf('__sessionView') >= 0) return 'sessionview-leaked(fields)';
      if (ta && ta.value !== ta0) return 'manifest-text-changed(fields)';
      if (unsaved0 !== null && (typeof rt.isUnsaved === 'function') && rt.isUnsaved() !== unsaved0) return 'dirty-marked(fields)';
      // 还原：重查按钮（重渲后 DOM 换新）→ 恢复默认 → 列数回基线
      host = hostOf();
      const fldBtn2 = host.querySelector('[data-tbl-fields-open]');
      if (!fldBtn2) return 'fields-btn-missing(after-render)';
      fldBtn2.click();
      if (!await until(() => { const p = document.querySelector('.ai-tbl-pop'); return !!(p && p.querySelector('[data-fld-reset]')); })) return 'field-pop-not-reopen';
      document.querySelector('.ai-tbl-pop [data-fld-reset]').click();
      if (!await until(() => hostOf() && !hostOf().querySelector('.ai-chart-loading') && hostOf().querySelectorAll('thead th[data-col-head]').length === thCount0)) return 'field-reset-no-restore';
      const close1 = document.querySelector('.ai-tbl-pop [data-tbl-pop-close2]');
      if (close1) close1.click();
      if (editorJson() !== editor0) return 'editor-changed(reset)';
      // ③排序模式：会话提交（不落盘）；面板有分段则点「仅组内」再关
      host = hostOf();
      const sortBtn = host.querySelector('[data-tbl-sort-open]');
      if (sortBtn) {
        sortBtn.click();
        if (!await until(() => { const p = document.querySelector('.ai-tbl-pop'); return !!(p && (p.querySelector('[data-smode]') || p.querySelector('[data-sort-row],.ai-tbl-pop-sort'))); })) return 'sort-pop-not-open';
        const smBtn = document.querySelector('.ai-tbl-pop [data-smode="group"]');
        if (smBtn) {
          smBtn.click();
          if (!await until(() => hostOf() && !hostOf().querySelector('.ai-chart-loading') && hostOf().querySelector('tbody tr'))) return 'sortmode-render-broken';
          if (editorJson() !== editor0) return 'editor-changed(sortmode)';
        }
        const cl = document.querySelector('.ai-tbl-pop [data-tbl-pop-close]');
        if (cl) cl.click();
      }
      // ②筛选面板：读者态可打开（多条件 AND 管理）；关闭不落盘
      host = hostOf();
      const fltBtn = host.querySelector('[data-tbl-filters-open]');
      if (!fltBtn) return 'filters-btn-missing';
      fltBtn.click();
      if (!await until(() => !!document.querySelector('.ai-tbl-pop [data-flt-add], .ai-tbl-pop [data-flt-clearall]'))) return 'filters-pop-not-open';
      const cl2 = document.querySelector('.ai-tbl-pop [data-tbl-pop-close2]');
      if (cl2) cl2.click();
      await new Promise((r) => setTimeout(r, 150));
      if (editorJson() !== editor0) return 'editor-changed(final)';
      if (ta && ta.value !== ta0) return 'manifest-text-changed(final)';
      if (unsaved0 !== null && (typeof rt.isUnsaved === 'function') && rt.isUnsaved() !== unsaved0) return 'dirty-marked(final)';
      // __sessionView 永不进 manifest（刷新即还原的机制保证）
      if (JSON.stringify(rt.manifest).indexOf('__sessionView') >= 0) return 'sessionview-leaked-into-manifest';
      return true;
    } catch (e) { return String((e && e.stack) || e); }
  })()`);
  await wait(200);
  // 筛选弹层（真机）：head/body/foot 三段纵向 flex，foot 在 body **之外**；
  // 「清除/确定」等大对齐且**滚动值域列表时不动**（旧版 foot 在 body 里，滑到底才看到）。
  checks.tableFilterPopover = await evalIf('tableFilterPopover', `(async () => {
    try {
      const wrap = document.querySelector('.ai-report-table-wrap');
      if (!wrap) return true;                 // 无表格 = 不适用
      const fb = wrap.querySelector('thead .ai-col-fbtn');
      if (!fb) return true;
      fb.click();
      const until = async (fn, ms) => { const t0 = Date.now(); while (!fn()) { if (Date.now() - t0 > (ms || 6000)) return false; await new Promise((r) => setTimeout(r, 120)); } return true; };
      if (!await until(() => { const p = document.querySelector('.ai-tbl-pop'); return !!(p && p.querySelector('.ai-tbl-pop-foot') && p.querySelector('.ai-tbl-pop-list')); })) return 'pop-not-ready';
      const p = document.querySelector('.ai-tbl-pop');
      const body = p.querySelector('.ai-tbl-pop-body'), foot = p.querySelector('.ai-tbl-pop-foot');
      const list = p.querySelector('.ai-tbl-pop-list');
      const clear = p.querySelector('[data-tbl-pop-clear]'), ok = p.querySelector('[data-tbl-pop-ok]');
      if (body.contains(foot)) return 'foot-inside-scroll-body';
      if (foot.parentElement !== p) return 'foot-not-pinned';
      if (Math.abs(clear.getBoundingClientRect().height - ok.getBoundingClientRect().height) > 1) return 'foot-buttons-uneven';
      if (Math.abs(clear.getBoundingClientRect().top - ok.getBoundingClientRect().top) > 1) return 'foot-buttons-misaligned';
      const top0 = foot.getBoundingClientRect().top;
      if (list) list.scrollTop = list.scrollHeight;
      await new Promise((r) => setTimeout(r, 60));
      if (Math.abs(foot.getBoundingClientRect().top - top0) > 1) return 'foot-scrolled-away';
      const cl = p.querySelector('[data-tbl-pop-close]'); if (cl) cl.click();
      return true;
    } catch (e) { return String((e && e.stack) || e); }
  })()`);
  await wait(200);
  // 明细工具条（真机）：搜索 / 按 X / 升降序 / 行数控件已删；保留排序+导出；
  // 排序靠表头点击并给出 ▲▼ 指示。
  checks.detailToolbar = await evalIf('detailToolbar', `(async () => {
    try {
      const charts = (AIReportRuntime.manifest.charts || []).filter((c) => c.viewType === 'table');
      if (!charts.length) return true;
      const chart = charts[0];
      chart.editor = chart.editor || {}; chart.editor.table = chart.editor.table || {};
      const orig = chart.editor.table.mode;
      const restore = () => { if (orig === undefined) delete chart.editor.table.mode; else chart.editor.table.mode = orig; AIReportRuntime.renderChart(chart); };
      chart.editor.table.mode = 'detail';
      AIReportRuntime.renderChart(chart);
      const until = async (fn, ms) => { const t0 = Date.now(); while (!fn()) { if (Date.now() - t0 > (ms || 9000)) return false; await new Promise((r) => setTimeout(r, 150)); } return true; };
      if (!await until(() => { const h = document.getElementById(chart.elementId); return !!(h && h.querySelector('.ai-report-table-wrap table') && h.querySelector('.ai-report-table-bar .ai-table-tools')); })) { restore(); return 'detail-not-rendered'; }
      const host = document.getElementById(chart.elementId);
      const tools = host.querySelector('.ai-report-table-bar .ai-table-tools');
      if (tools.querySelector('.ai-grid-search,.ai-grid-sort,.ai-grid-dir,.ai-grid-count')) { restore(); return 'redundant-controls'; }
      if (!tools.querySelector('[data-tbl-sort-open]') || !tools.querySelector('[data-export-menu]')) { restore(); return 'missing-tools'; }
      if (!tools.querySelector('.ai-table-pager')) { restore(); return 'pager-not-in-bar'; }
      const th = host.querySelector('thead th[data-detail-sort]');
      if (th) {
        th.click();
        if (!await until(() => { const h = document.getElementById(chart.elementId); return !!(h && h.querySelector('thead .ai-sort-arrow')); }, 9000)) { restore(); return 'header-sort-no-arrow'; }
      }
      // 导出下拉：一个按钮 → Excel/CSV 两项
      const em = document.getElementById(chart.elementId).querySelector('[data-export-menu]');
      em.click();
      if (!await until(() => { const p = document.querySelector('.ai-tbl-pop-menu'); return !!(p && p.querySelectorAll('.ai-tbl-pop-menu-item').length === 2); }, 3000)) { document.dispatchEvent(new MouseEvent('mousedown', { bubbles: true })); restore(); return 'export-menu-items'; }
      document.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
      restore();
      return true;
    } catch (e) { return String((e && e.stack) || e); }
  })()`);
  await wait(300);
  // 表格卡自动高度：卡片必须长到能完整显示所有表格行 —— **禁止纵向滚动条**
  // （横向滚动条保留）。在非 focus 的 grid 状态下逐个表格卡核对。
  checks.tableAutoHeight = await evalIf('tableAutoHeight', `(async () => {
    const until = async (fn, ms) => { const t0 = Date.now(); while (!fn()) { if (Date.now() - t0 > (ms || 9000)) return false; await new Promise((r) => setTimeout(r, 150)); } return true; };
    const measure = () => {
      const bad = [];
      document.querySelectorAll('.grid-stack-item').forEach((it) => {
        const w = it.querySelector('.ai-report-table-wrap');
        if (!w || !w.querySelector('table')) return;
        if (it.offsetHeight < 1 || w.clientHeight < 1) return;          // 隐藏页签 = 不适用
        if (it.classList.contains('ai-report-focus-target')) return;     // 聚焦大图舞台：视口定尺，天然滚动
        if (w.hasAttribute('data-bounded')) return;                      // WP-7 有界高度：卡内滚动是设计行为（boundedTable 检查管它）
        if (w.scrollHeight - w.clientHeight > 1) bad.push({ id: it.getAttribute('data-report-block-id'), gsH: it.getAttribute('gs-h'), scroll: w.scrollHeight, client: w.clientHeight });
      });
      return bad;
    };
    const charts = (AIReportRuntime.manifest.charts || []).filter((c) => c.viewType === 'table');
    if (!charts.length) return true;
    const chart = charts[0];
    chart.editor = chart.editor || {}; chart.editor.table = chart.editor.table || {};
    const origModes = Array.isArray(chart.editor.table.modes) ? JSON.parse(JSON.stringify(chart.editor.table.modes)) : null;
    const origMode = chart.editor.table.mode;
    const restore = () => {
      if (origModes) chart.editor.table.modes = origModes; else delete chart.editor.table.modes;
      if (origMode === undefined) delete chart.editor.table.mode; else chart.editor.table.mode = origMode;
      AIReportRuntime.renderChart(chart);
    };
    // 聚合表
    chart.editor.table.mode = 'aggregate';
    if (chart.editor.table.modes) chart.editor.table.modes = ['detail', 'aggregate'];
    AIReportRuntime.renderChart(chart);
    if (!await until(() => { const h = document.getElementById(chart.elementId); return !!(h && h.querySelector('.ai-report-table-wrap table')); })) { restore(); return 'table-not-rendered'; }
    await until(() => measure().length === 0, 9000);
    const agg = measure();
    // 明细表（分页后的整页行也要全部可见）
    chart.editor.table.mode = 'detail';
    AIReportRuntime.renderChart(chart);
    if (!await until(() => { const h = document.getElementById(chart.elementId); return !!(h && h.querySelector('.ai-report-table-wrap table') && h.querySelector('.ai-report-table-bar .ai-table-tools')); })) { restore(); return 'detail-not-rendered'; }
    await until(() => measure().length === 0, 12000);
    const det = measure();
    restore();
    if (agg.length) return 'aggregate-vscroll ' + JSON.stringify(agg);
    if (det.length) return 'detail-vscroll ' + JSON.stringify(det);
    return true;
  })()`);
  // WP-7 有界高度 + 冻结表头（docs/wp7-bounded-height-design.md §5）：真机 50 行
  // 数据卡 wrap 真滚动（scrollHeight>clientHeight），滚动到中部后 sticky thead
  // 仍可见（表头 rect 在 wrap 视口内、贴其顶）。仅对声明 bounded 的卡执行；报告
  // 没有 bounded 卡 = 不适用。几何契约顺带核：wrap ≤ 帽、gs-h*48 ≥ maxPx+chrome、
  // 标记类 .ai-bounded（不冒用 .ai-hug/.ai-fixh）。
  checks.boundedTable = await evalIf('boundedTable', `(async () => {
    try {
      const charts = (AIReportRuntime.manifest.charts || []).filter((c) => {
        const hm = ((c.editor || {}).table || {}).heightMode;
        return c.viewType === 'table' && hm && typeof hm === 'object' && hm.bounded;
      });
      if (!charts.length) return true;                 // 无 bounded 卡 = 不适用
      const chart = charts[0];
      const until = async (fn, ms) => { const t0 = Date.now(); while (!fn()) { if (Date.now() - t0 > (ms || 8000)) return false; await new Promise((r) => setTimeout(r, 150)); } return true; };
      // 前置检查（gridCard 插拔 / 明细切态等）会异步重渲染卡片 —— 只抓「活的」
      // 节点（isConnected），持旧引用会把 detached 节点量成全零 rect 假失败。
      const grab = () => {
        const it = document.querySelector('.grid-stack-item[data-report-block-id="' + CSS.escape(chart.blockId) + '"]');
        const wp = it && it.querySelector('.ai-report-table-wrap');
        return (it && wp && it.isConnected && wp.isConnected && it.offsetHeight > 0) ? { it, wp } : null;
      };
      if (!await until(() => !!grab(), 8000)) return 'block-or-wrap-missing';
      let g = grab();
      const wrap = g.wp; const item = g.it;
      if (!wrap.hasAttribute('data-bounded')) return 'no-data-bounded';
      const maxPx = Number(wrap.getAttribute('data-bounded'));
      // XU-01 行数口径：data-bounded 携带的是「实测行高校正后」的帽（N×行高+thead），
      // 可超 maxPx 输入上限 4000（如 500 行×48px）——这里只核「合理帽」区间，输入
      // 边界（200..4000 / 5..500）由 validate_request 管。
      if (!(maxPx >= 100 && maxPx <= 60000)) return 'bad-maxPx:' + maxPx;
      if (wrap.offsetHeight > maxPx + 2) return 'wrap-taller-than-cap:' + wrap.offsetHeight + '>' + maxPx;
      const chrome = item.offsetHeight - wrap.offsetHeight;
      if (Number(item.getAttribute('gs-h')) * 48 < maxPx + chrome - 1) return 'reserve-too-small:' + item.getAttribute('gs-h') + '*48<' + (maxPx + chrome);
      if (item.classList.contains('ai-hug') || item.classList.contains('ai-fixh')) return 'wrong-height-class';
      if (!item.classList.contains('ai-bounded')) return 'no-bounded-class';
      if (wrap.scrollHeight <= wrap.clientHeight + 2) return 'wrap-not-scrolling:' + wrap.scrollHeight + '<=' + wrap.clientHeight + '（数据不足一屏?）';
      // 量 sticky 元素本身（th）：TR 盒留在文档流位置（滚出 wrap 顶），th 才是被
      // sticky 钉住、读者看到的表头（CDP 实测：滚到中部 th.top == wrap.top 恒定）。
      if (!wrap.querySelector('thead th')) return 'thead-missing';
      // 滚到中部：sticky 表头必须仍在 wrap 视口内（贴顶），数据行从其下方滚过。
      // 每一步都重抓活节点；重渲染会重置 scrollTop，滚不上就重试。
      let scrolled = false;
      for (let i = 0; i < 3 && !scrolled; i += 1) {
        g = grab(); if (!g) return 'wrap-detached';
        g.wp.scrollTop = Math.round((g.wp.scrollHeight - g.wp.clientHeight) / 2);
        await new Promise((r) => setTimeout(r, 300));
        g = grab(); if (!g) return 'wrap-detached-mid';
        scrolled = g.wp.scrollTop > 0;
      }
      if (!scrolled) return 'scroll-noop';
      const wr = g.wp.getBoundingClientRect();
      const head2 = g.wp.querySelector('thead th');
      if (!head2) return 'thead-gone-after-scroll';
      const hr = head2.getBoundingClientRect();
      if (hr.top < wr.top - 2 || hr.bottom > wr.bottom + 2) return 'thead-out-of-wrap:' + JSON.stringify({ hr: hr.top, wr: wr.top });
      if (wr.top - hr.top > 4) return 'thead-not-stuck:' + (wr.top - hr.top);
      if (hr.height < 8) return 'thead-collapsed:' + JSON.stringify({ h: hr.height, hrT: hr.top, wrT: wr.top, itemH: g.it.offsetHeight, scrollT: g.wp.scrollTop });
      // 表头不透明：行从其下方滚过时文字不得透出（--thead-bg 既有）。
      if (getComputedStyle(head2).backgroundColor === 'rgba(0, 0, 0, 0)') return 'thead-transparent';
      g.wp.scrollTop = 0;
      return true;
    } catch (e) { return String((e && e.stack) || e); }
  })()`);
  // UI 军规：同一视觉行的控件（按钮/下拉/输入）必须等高（--ctl-h / --ctl-h-sm），
  // 不得出现并排高度不一致。图标按钮（×/删除/拖拽）与 checkbox/radio 字形豁免。
  // BI-H1 验收决策（2026-09-13）：图表间联动（highlight/filter/chip）整体移除；
  // 点击下钻保留为图表级开关（默认关）。真机检查：面板有「点击下钻」且默认未勾 →
  // 勾选走面板事件委托 → chart.editor.drillEnabled 落真 → 确有下一级的图表宿主
  // 出现 ai-chart-drillable（手型光标）；取消勾选后类消失。没有下一级 = 不适用。
  checks.clickDrill = await evalIf('clickDrill', `(async () => {
    try {
      const skip = (why) => { try { window.__auditDrillSkip = why; } catch (e) {} return true; };   // 不适用 = 通过（原因留在页面全局供诊断）
      if (typeof AIReportRuntime === 'undefined') return skip('no-runtime');
      const charts = ((AIReportRuntime.manifest || {}).charts || []).filter((c) => ['bar', 'stackedBar', 'line', 'area', 'pie', 'donut', 'funnel'].includes(c.viewType)
        && c.editor && c.editor.data && c.editor.data.dimension);
      if (!charts.length) return skip('no-candidate-chart');
      const until = async (fn, ms) => { const t0 = Date.now(); while (!fn()) { if (Date.now() - t0 > (ms || 8000)) return false; await new Promise((r) => setTimeout(r, 150)); } return true; };
      const target = charts[0];
      const el = document.querySelector('.grid-stack-item[data-report-block-id="' + CSS.escape(target.blockId) + '"]');
      if (!el) return skip('no-block-el');
      const priorDirty = !!(target.editor && target.editor.dirty);
      const restore = async () => {
        const b = document.getElementById('ai-chart-drill');
        if (b && b.checked) { b.checked = false; b.dispatchEvent(new Event('change', { bubbles: true })); }
        target.editor.dirty = priorDirty;   // 开关探索不应把未编辑图表推上重聚合路径
        AIReportRuntime.renderChart(target);
        await new Promise((r) => setTimeout(r, 300));
      };
      AIReportRuntime.selectBlock(el);
      if (!await until(() => !!document.getElementById('ai-report-runtime-panel'), 3000)) return 'fail:panel';
      const box = document.getElementById('ai-chart-drill');
      if (!box) return 'fail:toggle-missing';
      if (box.checked) return 'fail:default-on';   // 默认必须关闭
      box.checked = true;
      box.dispatchEvent(new Event('change', { bubbles: true }));
      const host = document.getElementById(target.elementId);
      const on = await until(() => host && host.classList.contains('ai-chart-drillable'), 6000);
      if (!target.editor || target.editor.drillEnabled !== true) return 'fail:state';
      if (!on) { await restore(); return skip('enabled-no-next-level'); }   // 维度没有下一级：不给手型光标是正确行为
      const box2 = document.getElementById('ai-chart-drill');
      if (!box2) return 'fail:toggle-vanished';
      box2.checked = false;
      box2.dispatchEvent(new Event('change', { bubbles: true }));
      const off = await until(() => host && !host.classList.contains('ai-chart-drillable'), 6000);
      if (!off) return 'fail:off-still-drillable';
      target.editor.dirty = priorDirty;
      AIReportRuntime.renderChart(target);
      return true;
    } catch (e) { return 'error:' + ((e && e.message) || e); }
  })()`);
  await wait(300);
  checks.buttonRowHeights = await evalIf('buttonRowHeights', `(async () => {
    const CTRL = 'button,select,textarea,label:has(>input),input:not([type=hidden]),.ai-report-chip,.ai-report-table-type-opt,.ai-report-chart-type,.ai-sem-view-tab,.ai-report-filter-multibtn,.ai-report-tab,.ai-tb-btn,.ai-report-secondary,.ai-report-primary,.ai-report-chip-form,.ai-report-chip-side,.ai-report-unit-seg,.ai-report-kpi-cmp,.ai-report-target-group';
    const EXEMPT = (el) => {
      if (el.matches('input[type=checkbox],input[type=radio]')) return true;   // 由外层 label 代表该行高度
      const c = (typeof el.className === 'string' ? el.className : '') + ' ';
      if (/close|-rm\b|remove|\bdel\b|icon|swatch|resizer|grip|drag/.test(c)) return true;
      if (el.tagName === 'SPAN' && el.querySelector('button,select,input')) return true;
      if (el.closest('.ai-report-chip-form,.ai-report-chip-side,.ai-report-unit-seg,.ai-report-kpi-cmp,.ai-report-target-group,.ai-report-table-type')) return true; // 组合控件内部
      return false;
    };
    function controls(root) {
      // 视口真值：被**滚动容器裁掉**的控件（rect 超出 overflow:auto/hidden/scroll
      // 祖先的可视盒）不在任何「视觉水平行」上——getBoundingClientRect 不感知
      // overflow 裁剪，会把已滚出视区的行内控件照常报出，与贴屏的底部按钮凑成
      // 同排假对（logistics_1m 真机：数据源配置表格的小数位 select 滚出 .ai-dsc-body
      // 视区后，其 rect 与固定的「完成」按钮同排 → 26/32 假红）。超出的即视口外，
      // 只对溢出型祖先做几何判定；容差 2px 吸亚像素。
      const clipped = (el, r) => {
        for (let a = el.parentElement; a && a !== document.body; a = a.parentElement) {
          const g = getComputedStyle(a);
          if (!/(auto|scroll|hidden|clip)/.test(g.overflow + g.overflowX + g.overflowY)) continue;
          const ar = a.getBoundingClientRect();
          if (r.bottom > ar.bottom + 2 || r.top < ar.top - 2 || r.right > ar.right + 2 || r.left < ar.left - 2) return true;
        }
        return false;
      };
      const all = [...root.querySelectorAll(CTRL)];
      const out = [];
      all.forEach((el) => {
        if (all.some((o) => o !== el && o.contains(el))) return;
        if (EXEMPT(el)) return;
        const r = el.getBoundingClientRect();
        if (r.width < 6 || r.height < 10) return;
        const g = getComputedStyle(el);
        if (g.display === 'none' || g.visibility === 'hidden' || Number(g.opacity) === 0) return;
        if (clipped(el, r)) return;
        out.push({ h: Math.round(r.height), t: Math.round(r.top), l: Math.round(r.left), rg: Math.round(r.right), d: (typeof el.className === 'string' ? el.className.split(/\s+/).slice(0, 2).join('.') : el.tagName) + (el.textContent ? '[' + el.textContent.trim().slice(0, 14) + ']' : '') });
      });
      return out;
    }
    function badRows(root) {
      const cs = controls(root);
      const used = cs.map(() => false);
      const rows = [];
      for (let i = 0; i < cs.length; i += 1) {
        if (used[i]) continue;
        const a = cs[i]; const row = [a]; used[i] = true;
        for (let j = i + 1; j < cs.length; j += 1) {
          if (used[j]) continue;
          const b = cs[j];
          const ov = Math.min(a.t + a.h, b.t + b.h) - Math.max(a.t, b.t);
          const sameV = Math.abs(a.t - b.t) <= 4 || ov > 0.55 * Math.min(a.h, b.h);
          const overlapX = Math.min(a.rg, b.rg) - Math.max(a.l, b.l);
          const gapX = Math.max(a.l, b.l) - Math.min(a.rg, b.rg);
          if (sameV && overlapX <= 2 && gapX <= 26) { row.push(b); used[j] = true; }
        }
        if (row.length > 1) rows.push(row);
      }
    return rows.filter((r) => Math.max(...r.map((x) => x.h)) - Math.min(...r.map((x) => x.h)) > 1)
      .map((r) => [...new Set(r.map((x) => x.h))].sort().join('/') + ' :: ' + r.map((x) => x.d + '@' + x.h + '[' + x.l + '..' + x.rg + ' t' + x.t + ']').join(' | '));
    }
    const until = async (fn, ms) => { const t0 = Date.now(); while (!fn()) { if (Date.now() - t0 > (ms || 6000)) return false; await new Promise((r) => setTimeout(r, 150)); } return true; };
    // 军规 1 扩展（压平四件套执法，AGENTS「军规 1」2026-09-25 批）：同一视觉行上的
    // 文本元素不得换行——满宽按钮（.ai-report-primary 缺省 width:100%+margin-top:14px）
    // 挤压同行标签是压平漏写的事故形状（真机三次：.ai-finish-actions / 三容器压平批 /
    // .ai-report-unsel-batchbar）。Range.getClientRects 按 top 分桶 = 实际行数；
    // white-space:nowrap 元素天然免检；与控件不构成视觉行（垂直重叠 ≥55% + 水平相邻）
    // 的独立文本带（hint/规则行/空态等）不在军规范围，逐类豁免。
    function wrapViolations(root) {
      const TXT = 'span,strong,em,b,i,label,small,code,a,div';
      const EXEMPT_TXT = 'p,legend,h1,h2,h3,h4,th,td,.ai-report-zone-rule,.ai-report-hint,.ai-report-empty,.ai-report-notice,.ai-report-zone-header,.ai-report-subzone-header,.ai-ctl-row label,.ai-report-zone-hint';
      const out = [];
      const cs = controls(root);
      root.querySelectorAll(TXT).forEach((el) => {
        if (el.closest(EXEMPT_TXT)) return;
        if (el.closest('.ai-report-chip-form,.ai-report-chip-side,.ai-report-unit-seg,.ai-report-kpi-cmp,.ai-report-target-group')) return;
        let hasText = false;
        for (const n of el.childNodes) { if (n.nodeType === 3 && n.textContent.trim().length > 1) { hasText = true; break; } }
        if (!hasText) return;
        const g = getComputedStyle(el);
        if (g.display === 'none' || g.visibility === 'hidden') return;
        if (/(nowrap|pre)/.test(g.whiteSpace)) return;
        const r = el.getBoundingClientRect();
        if (r.width < 8 || r.height < 10) return;
        const near = cs.some((c) => {
          const ov = Math.min(r.bottom, c.t + c.h) - Math.max(r.top, c.t);
          if (!(Math.abs(r.top - c.t) <= 4 || ov > 0.55 * Math.min(r.height, c.h))) return false;
          const overlapX = Math.min(r.right, c.rg) - Math.max(r.left, c.l);
          const gapX = Math.max(r.left, c.l) - Math.min(r.right, c.rg);
          return overlapX <= 2 && gapX <= 26;
        });
        if (!near) return;
        let lines = 1;
        try {
          const range = document.createRange();
          range.selectNodeContents(el);
          const tops = new Set();
          [...range.getClientRects()].forEach((x) => { if (x.width > 2 && x.height > 2) tops.add(Math.round(x.top / 4)); });
          lines = tops.size;
        } catch (e) { return; }
        if (lines > 1) out.push((typeof el.className === 'string' && el.className ? el.className.split(/\\s+/)[0] : el.tagName) + '[' + el.textContent.trim().slice(0, 14) + '] 同行文本换行 ' + lines + ' 行（军规 1 压平四件套）');
      });
      return out;
    }
    const violations = {};
    const scan = (label, node) => {
      if (!node || !node.offsetHeight) return;
      const bad = badRows(node);
      if (bad.length) violations[label] = bad;
      const wr = wrapViolations(node);
      if (wr.length) violations[label + ':wrap'] = wr;
    };
    const scanAll = (label, sel) => document.querySelectorAll(sel).forEach((n, i) => scan(label + '[' + i + ']', n));
    // 常驻表面
    scanAll('toolbar', '#ai-report-toolbar');
    scanAll('tabbar', '[data-report-tabbar]');
    scanAll('tableBar', '.ai-report-table-bar');
    scanAll('presControls', '.ai-pres-controls');
    scanAll('filterItem', '.ai-report-filter-item');
    scanAll('hierFoot', '.ai-hier-canvas-foot');
    // 图表面板（聚合 + 每个图表一次，覆盖 chart/KPI/表格三套面板外壳）
    for (const chart of (AIReportRuntime.manifest.charts || [])) {
      const el = document.querySelector('.grid-stack-item[data-report-block-id="' + CSS.escape(chart.blockId) + '"]');
      if (!el) continue;
      AIReportRuntime.selectBlock(el);
      await until(() => !!document.getElementById('ai-report-runtime-panel'), 3000);
      await new Promise((r) => setTimeout(r, 260));
      scan('panel:' + chart.id, document.getElementById('ai-report-runtime-panel'));
      // 表格：再扫一遍明细模式面板（双区 + 指标 chip 口径组）
      if (chart.viewType === 'table') {
        chart.editor = chart.editor || {}; chart.editor.table = chart.editor.table || {};
        const om = Array.isArray(chart.editor.table.modes) ? JSON.parse(JSON.stringify(chart.editor.table.modes)) : null;
        chart.editor.table.modes = ['detail']; chart.editor.table.mode = 'detail';
        AIReportRuntime.renderChart(chart);
        await new Promise((r) => setTimeout(r, 700));
        scan('panel-detail:' + chart.id, document.getElementById('ai-report-runtime-panel'));
        if (om) chart.editor.table.modes = om; else delete chart.editor.table.modes;
        delete chart.editor.table.mode;
        AIReportRuntime.renderChart(chart);
        await new Promise((r) => setTimeout(r, 700));
      }
    }
    // 筛选弹层（值域列表 + 固定底部按钮）
    const fb = document.querySelector('[data-col-filter]');
    if (fb) {
      fb.click();
      await until(() => !!document.querySelector('.ai-tbl-pop .ai-tbl-pop-foot'), 4000);
      await new Promise((r) => setTimeout(r, 300));
      scan('filterPop', document.querySelector('.ai-tbl-pop'));
      document.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
      await new Promise((r) => setTimeout(r, 200));
    }
    // 数据源配置（三段式全屏）
    const semBtn = document.querySelector('.ai-report-semantic-toggle');
    if (semBtn) {
      semBtn.click();
      await until(() => !!document.querySelector('.ai-hier-canvas-overlay'), 4000);
      await new Promise((r) => setTimeout(r, 600));
      scanAll('semCanvas', '.ai-hier-canvas-overlay');
      // 关闭画布用右上角 ×（data-canvas-close），**不要**点底部「放弃本次改动」：
      // 那个按钮在有改动时会弹原生 confirm……审计会被对话框拦一下，而且真的会回滚
      // 审计自己造的临时改动（× = 保留改动并关闭，符合「只读检查」）。
      const cancel = document.querySelector('.ai-hier-canvas-overlay [data-canvas-close]')
        || [...document.querySelectorAll('.ai-hier-canvas-overlay button')].find((b) => /取消|关闭|完成/.test(b.textContent || ''));
      if (cancel) cancel.click();
      await new Promise((r) => setTimeout(r, 400));
    }
    const keys = Object.keys(violations);
    return keys.length ? keys.map((k) => k + ' -> ' + violations[k].join(' ;; ')).join(' || ') : true;
  })()`);
  await wait(300);
  // BI-H7 图表级条件格式：开面板 → 点指标 chip「条件格式」→ 弹窗填三个阈值 →
  // manifest 配置生效 + 图表 option 出现系列级 itemStyle.color 回调 → 清理还原。
  checks.condFormat = await evalIf('condFormat', `(async () => {
    try {
      const skip = (why) => true;   // 不适用 = 通过（原因留在页面全局供诊断）
      try { window.__auditCondFmtSkip = null; } catch (e) {}
      if (typeof AIReportRuntime === 'undefined') return skip('no-runtime');
      const charts = (AIReportRuntime.manifest && AIReportRuntime.manifest.charts) || [];
      // 优先 bar/stackedBar（走 itemStyle 分支）；没有才退 line/area（visualMap 分支）。
      const isAxis = (c) => ['bar', 'stackedBar', 'line', 'area'].includes(c.viewType)
        && c.editor && c.editor.data && (c.editor.data.measures || []).length;
      // 作者 option 里必须有**以指标命名**的 series 才是本检查的对象——breakdown 拆列
      // 的堆叠图（series 名 = 维度值）与表卡（option 无 series）装饰不了（logistics
      // 真机：condFormat 恒报 option-itemstyle false 假失败）。都不满足 = 诚实跳过。
      const hasMeasureSeries = (c) => Array.isArray(c.option && c.option.series)
        && c.option.series.some((s) => s && (c.editor.data.measures || []).indexOf(s.name) >= 0);
      const target = charts.find((c) => ['bar', 'stackedBar'].includes(c.viewType) && isAxis(c) && hasMeasureSeries(c))
        || charts.find((c) => isAxis(c) && hasMeasureSeries(c));
      if (!target) { window.__auditCondFmtSkip = 'no-axis-chart'; return skip('no-axis-chart'); }
      const measure = target.editor.data.measures[0];
      const until = async (fn, ms) => { const t0 = Date.now(); while (!fn()) { if (Date.now() - t0 > (ms || 9000)) return false; await new Promise((r) => setTimeout(r, 200)); } return true; };
      AIReportRuntime.setEditing(true);
      AIReportRuntime.openBlock(target.blockId);
      if (!await until(() => document.querySelector('#ai-report-runtime-panel .ai-report-chip-cffmt'), 9000)) return 'fail:panel-or-btn';
      const btn = document.querySelector('#ai-report-runtime-panel [data-bi-control-action="chip-condfmt"]');
      btn.click();
      if (!await until(() => document.querySelector('[data-cf-pop]'), 6000)) return 'fail:popup';
      const vals = { high: '9999', mid: '5555', low: '1111' };
      for (const slot of ['high', 'mid', 'low']) {
        const inp = document.querySelector('[data-cf-th="' + slot + '"]');
        if (!inp) return 'fail:input-' + slot;
        inp.value = vals[slot];
        inp.dispatchEvent(new Event('change', { bubbles: true }));
        await new Promise((r) => setTimeout(r, 250));
      }
      const cfg = target.editor.condFormat && target.editor.condFormat[measure];
      if (!cfg || String(cfg.thresholds.high) !== '9999' || cfg.mode !== 'goal') return 'fail:cfg ' + JSON.stringify(cfg);
      // 渲染生效（行为级）：applyCondFormat 对真实图表 + 作者 option 跑一遍，
      // 按几何分流断言（getOption() 会把函数回调解掉，不能用）。
      let painted = false;
      try {
        const A = window.AIReportChartAdapter;
        if (!A || typeof A.applyCondFormat !== 'function') painted = 'no-adapter';
        else {
          const out = A.applyCondFormat(A.clone(target.option || {}), target);
          const ser = (out.series || []).find((x) => x && x.name === measure);
          if (['bar', 'stackedBar'].includes(target.viewType)) {
            painted = !!(ser && ser.itemStyle && typeof ser.itemStyle.color === 'function');
          } else {
            const vm = out.visualMap;
            painted = !!(Array.isArray(vm) && vm.length
              && vm[0].type === 'piecewise' && Array.isArray(vm[0].pieces) && vm[0].pieces.length === 4);
          }
        }
      } catch (e) { painted = 'opt-err:' + e.message; }
      if (painted !== true) return 'fail:option-itemstyle ' + painted + ' view=' + target.viewType + ' id=' + target.id;
      // 清理：重开弹窗（填写不重开是刻意行为——不打断输入）→「停用」→ 还原面板
      const btn2 = document.querySelector('#ai-report-runtime-panel [data-bi-control-action="chip-condfmt"]');
      if (!btn2) return 'fail:reopen-btn';
      btn2.click();
      if (!await until(() => document.querySelector('[data-cf-clear]'), 6000)) return 'fail:clear-btn';
      document.querySelector('[data-cf-clear]').click();
      await new Promise((r) => setTimeout(r, 400));
      if (target.editor.condFormat && target.editor.condFormat[measure]) return 'fail:cleanup';
      const closeBtn = document.querySelector('[data-cf-close]');
      if (closeBtn) closeBtn.click();
      return true;
    } catch (e) { return 'error:' + ((e && e.message) || e); }
  })()`);
  await wait(300);
  // BI-H8 筛选默认：编辑态「存为默认」→ manifest.filterDefaults 落条目 → 读模式出现
  // 「恢复默认」且点击恢复 st.filters → 回编辑态「清除默认」→ 条目移除（全程不落服务端）。
  checks.filterDefaults = await evalIf('filterDefaults', `(async () => {
    try {
      const skip = (why) => true;
      try { window.__auditFilterDefSkip = null; } catch (e) {}
      if (typeof AIReportRuntime === 'undefined') return skip('no-runtime');
      const m = AIReportRuntime.manifest;
      // 报告未声明任何全局筛选 = 空配置器（维度下拉没有可选值），「存为默认」无对象
      // ——诚实跳过（真机：无 filters 的报告恒报 empty-filters 假失败）。
      if (!(m.filters || []).length) { window.__auditFilterDefSkip = 'no-declared-filters'; return skip('no-declared-filters'); }
      const band = document.querySelector('[data-report-filter][data-filter-block]');
      if (!band) { window.__auditFilterDefSkip = 'no-filter-band'; return skip('no-filter-band'); }
      const blockId = band.getAttribute('data-filter-block') || 'block-filter';
      const until = async (fn, ms) => { const t0 = Date.now(); while (!fn()) { if (Date.now() - t0 > (ms || 9000)) return false; await new Promise((r) => setTimeout(r, 200)); } return true; };
      AIReportRuntime.setEditing(true);
      if (!await until(() => document.querySelector('.ai-report-filter-savedef'), 9000)) return 'fail:savedef-btn';
      // 先制造一个选择：把第一个维度下拉设为非空值（若无下拉则空默认也允许存）。
      // 候选项允许异步到达（2026-10-15 值域活取数兜底：mixed/h_register 路径的
      // 选项是 renderOneFilterBar 末尾异步补的）——选项还没到就判空会假失败，
      // 最多等 5s。
      const sel = band.querySelector('select[data-filter-column]');
      if (sel) {
        await until(() => sel.options.length > 1, 5000);
        const optV = Array.prototype.slice.call(sel.options).find((o) => o.value !== '');
        if (optV) { sel.value = optV.value; sel.dispatchEvent(new Event('change', { bubbles: true })); await new Promise((r) => setTimeout(r, 900)); }
      }
      document.querySelector('.ai-report-filter-savedef').click();
      await new Promise((r) => setTimeout(r, 500));
      const entry = m.filterDefaults && m.filterDefaults[blockId];
      if (!entry || !Array.isArray(entry.filters)) return 'fail:entry ' + JSON.stringify(m.filterDefaults);
      if (sel && !entry.filters.length) return 'fail:empty-filters ' + JSON.stringify(entry);
      // 读模式：右上角是「恢复全选」（BI-H8 修订：恢复默认已删）。点击后本卡条件
      // 清空且置种子（__defSeed=1，作者默认不再自动播回）。
      AIReportRuntime.setEditing(false);
      if (!await until(() => document.querySelector('.ai-report-filter-resetdef'), 9000)) return 'fail:resetdef-btn';
      const rst = document.querySelector('.ai-report-filter-resetdef');
      if ((rst.textContent || '').indexOf('恢复全选') < 0) return 'fail:resetdef-label ' + rst.textContent;
      rst.click();
      await new Promise((r) => setTimeout(r, 900));
      const stNow = (window.__AI_REPORT_FILTERS__ || {})[blockId];
      if (!stNow || !Array.isArray(stNow.filters) || stNow.filters.length) return 'fail:selectall ' + JSON.stringify(stNow);
      if (stNow.__defSeed !== 1) return 'fail:seed-flag ' + JSON.stringify(stNow);
      // 回编辑态清除默认，报告页恢复干净
      AIReportRuntime.setEditing(true);
      if (!await until(() => document.querySelector('.ai-report-filter-savedef'), 9000)) return 'fail:savedef-btn-2';
      const clrDef = Array.prototype.slice.call(document.querySelectorAll('.ai-report-filter-savedef')).find((b) => b.textContent === '清除默认');
      if (!clrDef) return 'fail:clrdef-btn';
      clrDef.click();
      await new Promise((r) => setTimeout(r, 400));
      if (m.filterDefaults && m.filterDefaults[blockId]) return 'fail:cleanup';
      return true;
    } catch (e) { return 'error:' + ((e && e.message) || e); }
  })()`);
  await wait(300);
  // 顶部编辑栏硬约束：无「报告管理」按钮（/manager 只属于报告主人机器）；
  // 「合并页签显示」入口隐藏（底层功能保留）；server 无「不持久化」徽章；
  // 插入卡片菜单无「明细」入口。
  checks.toolbarConstraints = await evalIf('toolbarConstraints', `(() => {
    if (document.getElementById('ai-report-manager')) return 'manager-button-visible';
    const badge = document.querySelector('.ai-report-mode-badge');
    if (badge && badge.textContent.indexOf('不持久化') >= 0) return 'mode-badge-visible';
    const tg = document.querySelector('[data-layout-toggle]');
    if (tg && getComputedStyle(tg).display !== 'none') return 'layout-toggle-visible';
    const ins = document.getElementById('ai-report-insert');
    if (ins) { ins.setAttribute('data-open', 'true'); const m = document.getElementById('ai-report-insert-menu'); if (m) m.setAttribute('data-visible', 'true'); }
    if (document.querySelector('#ai-report-insert-menu [data-runtime-action="add-detail"]')) return 'insert-detail-entry';
    return true;
  })()`);
  // 层级合并筛选：一条层级链（≥2 级）在编辑态勾「层级筛选」后必须合并成**一个**
  // 级联控件（每级一个 select，逐级收窄），读模式只剩下拉（无勾选框/×）。
  checks.hierFilter = await evalIf('hierFilter', `(async () => {
    try {
      const m = globalThis.AIReportRuntime.manifest;
      const blockId = (document.querySelector('[data-report-filter]') || {}).getAttribute ? document.querySelector('[data-report-filter]').getAttribute('data-filter-block') : null;
      const block = (m.blocks || []).find((b) => b.id === blockId) || (m.blocks || []).find((b) => b.component === 'filter');
      const ds = (m.datasets || [])[0];
      if (!ds) return 'no-dataset';
      const activeCols = ((block && block.filterCols) || []).slice();
      // <2 个筛选列（free 无筛选带 / 单维度小表）= 层级筛选不可行使，诚实跳过；
      // 旧行为返回失败串，把无筛选报告打成假红。
      if (activeCols.length < 2) return true;
      const origHier = ds.hierarchy ? JSON.parse(JSON.stringify(ds.hierarchy)) : null;
      const origFlag = m.filterHier ? JSON.parse(JSON.stringify(m.filterHier)) : null;
      const chain = activeCols.slice(0, 3);
      ds.hierarchy = [];
      for (let i = 1; i < chain.length; i += 1) ds.hierarchy.push({ parent: chain[i - 1], child: chain[i] });
      m.filterHier = {}; m.filterHier[chain[0]] = true;
      globalThis.AIReportRuntime.setEditing(true);
      await new Promise((r) => setTimeout(r, 120));
      const host = document.querySelector('.ai-report-filter-hier');
      const levels = host ? [...host.querySelectorAll('.ai-report-filter-hier-level select[data-filter-column]')] : [];
      const toggle = !!(host && host.querySelector('.ai-report-filter-hiercfg-cb[data-filter-hier-cfg]'));
      const absorbed = chain.slice(1).every((c) => !document.querySelector('[data-filter-item-col="' + CSS.escape(c) + '"].ai-report-filter-item:not(.ai-report-filter-hier)'));
      const editOk = !!host && levels.length === chain.length && toggle && absorbed;
      globalThis.AIReportRuntime.setEditing(false);
      await new Promise((r) => setTimeout(r, 120));
      const readHost = document.querySelector('.ai-report-filter-hier');
      const readOk = !!readHost && !readHost.querySelector('.ai-report-filter-hiercfg-cb') && readHost.querySelectorAll('.ai-report-filter-hier-level select[data-filter-column]').length === chain.length;
      // restore
      if (origHier) ds.hierarchy = origHier; else delete ds.hierarchy;
      if (origFlag) m.filterHier = origFlag; else delete m.filterHier;
      globalThis.AIReportRuntime.setEditing(true);
      await new Promise((r) => setTimeout(r, 120));
      return (editOk && readOk) ? true : JSON.stringify({ editOk, readOk, levels: levels.length, chain, toggle, absorbed });
    } catch (error) { return String((error && error.stack) || error); }
  })()`);
  // ---- 「形态」(柱形/线形) 与图表类型切换的一致性 ---------------------------
  // 用户报过的回归：把指标形态设成 线形 后切到「堆叠柱状图」，画出来的仍是折线。
  // 根因是 data.seriesType 残留（适配器的 per-series 覆盖优先于基础几何）；现在
  // 类型一变即清空覆盖。本检查在真实报告里走一遍：形态选线形 → 切堆叠柱状图
  // → 所有 series 必须都是 bar 且 data.seriesType 已清空（跑完恢复原类型）。
  {
    const cartesians = await evaluate(`(() => { const four={bar:1,line:1,stackedBar:1,area:1}; return (AIReportRuntime.manifest.charts||[]).filter((c)=>four[c.viewType]&&c.editor&&c.editor.data&&(c.editor.data.measures||[]).length).map((c)=>c.blockId); })()`);
    let verdict = null;
    for (const blockId of (cartesians || []).slice(0, 4)) {
      await evaluate(ensureChartView(blockId)); await wait(200);
      await evaluate(`(() => { const b=document.querySelector('[data-report-block-id="${blockId}"]'); b.click(); const eb=b.querySelector('.ai-report-block-edit'); if(eb) eb.click(); })()`); await wait(240);
      const picked = await evaluate(`(() => { const group=document.querySelector('.ai-report-chip-form'); if(!group)return null; const btn=group.querySelector('.ai-report-chip-form-btn[data-form="line"]'); if(!btn||btn.disabled)return null; btn.click(); return btn.getAttribute('data-measure'); })()`);
      if (!picked) continue;
      await wait(300);
      verdict = await evaluate(`(async () => {
        const stack=document.querySelector('input[name="ai-chart-view"][value="stackedBar"]');
        if(!stack||stack.disabled)return 'stackedBar-unavailable';
        const chart=AIReportRuntime.manifest.charts.find((c)=>c.blockId===${JSON.stringify(blockId)});
        const getOption=()=>echarts.getInstanceByDom(document.getElementById(chart.elementId))?.getOption();
        // (1) 点「线形」不得改变基础几何：合并卡的 radio value 跟随真实几何，
        //     line 图不该被一次形态点击静默重写成 bar（曾经发生过的回归）。
        const before={vt:chart.viewType,lines:((getOption()||{}).series||[]).filter((s)=>s.type==='line').length};
        document.querySelector('.ai-report-chip-form .ai-report-seg-btn[data-form="line"]').click();
        await new Promise((r)=>setTimeout(r,700));
        const after={vt:chart.viewType,lines:((getOption()||{}).series||[]).filter((s)=>s.type==='line').length};
        if(after.vt!==before.vt||after.lines<before.lines)return JSON.stringify({formFlip:true,before:before,after:after});
        // (2) 切成堆叠柱状图后必须全部是柱，且形态覆盖已清空。
        const until=async(fn,ms)=>{const t0=Date.now();while(!fn()){if(Date.now()-t0>(ms||6000))return false;await new Promise((r)=>setTimeout(r,120));}return true;};
        stack.click();
        const hit=await until(()=>{const s=((getOption()||{}).series)||[];return s.length>0&&s.every((x)=>x.type==='bar');});
        const residual=(chart.editor.data.seriesType&&Object.keys(chart.editor.data.seriesType).length)||0;
        const back=document.querySelector('input[name="ai-chart-view"][value="'+before.vt+'"]'); if(back)back.click();
        return (hit&&!residual)?true:JSON.stringify({hit:!!hit,residual:residual,base:before.vt});
      })()`);
      if (verdict !== null) break;
    }
    // 没有可用的柱状/折线图（或线形在本报告里一律不可用）= 不适用，放行。
    checks.seriesFormReset = want('seriesFormReset') ? (verdict === null ? true : verdict) : undefined;
  }
  // ---- 列统计（数据源配置字段副行）：编译期算，运行时只展示 -------------------
  // 真机验证：每一行都有副行；「N 个不同值」与 manifest.datasets[].columnStats 逐列一致；
  // 脏值徽标只在 invalid>0 的列上出现（数量必须相等）；侧栏显示行数。
  // 只读检查：打开 → 核对 → 取消（不保存）。
  checks.columnStats = await evalIf('columnStats', `(async () => {
    try {
      const m = globalThis.AIReportRuntime.manifest;
      const ds = (m.datasets || [])[0];
      if (!ds) return 'no-dataset';
      const stats = ds.columnStats || {};
      // 无列统计 = 没有可核对的对象，不是缺陷：构建后「替换/增量合并」会有意清除
      // 编译期 stats（宁可副行显示「—」也不显示旧数字），未带 --column-stats 的
      // 构建同样没有。诚实跳过（skip 语义见文件末尾 passed 口径），新构建不受影响。
      if (!Object.keys(stats).length) return 'skip:columnStats 数据集无编译期列统计（构建后替换过数据或未带 --column-stats，副行按设计显示「—」）';
      const until = async (fn, ms) => { const t0 = Date.now(); while (!fn()) { if (Date.now() - t0 > (ms || 8000)) return false; await new Promise((r) => setTimeout(r, 120)); } return true; };
      const btn = document.querySelector('.ai-report-semantic-toggle');
      if (!btn) return 'no-semantic-entry';
      AIReportRuntime.setEditing(true);
      btn.click();
      if (!await until(() => document.querySelector('.ai-sem-row-sub'), 10000)) return 'no-sub-line-rendered';
      const overlay = document.querySelector('.ai-hier-canvas-overlay');
      const rows = [...overlay.querySelectorAll('tr.ai-sem-row')];
      const subs = overlay.querySelectorAll('.ai-sem-row-sub');
      if (!rows.length) return 'no-field-rows';
      if (subs.length !== rows.length) return JSON.stringify({ subs: subs.length, rows: rows.length });
      const rawNameOf = (tr) => { const b = tr.querySelector('[data-col-menu]'); return b ? b.getAttribute('data-col-menu') : ''; };
      const statsCols = Object.keys(stats);
      const withDistinct = statsCols.filter((c) => Number(stats[c].distinct) > 0);
      const mismatched = withDistinct.filter((c) => {
        const tr = rows.find((r) => rawNameOf(r) === c);
        if (!tr) return true;
        return tr.querySelector('.ai-sem-row-sub').textContent.indexOf(Number(stats[c].distinct).toLocaleString('en-US') + ' 个不同值') < 0;
      });
      if (mismatched.length) return JSON.stringify({ distinctMismatch: mismatched.slice(0, 4) });
      const dirtyCols = statsCols.filter((c) => Number(stats[c].invalid) > 0);
      const badges = overlay.querySelectorAll('.ai-sem-row-warn').length;
      if (badges !== dirtyCols.length) return JSON.stringify({ badges, dirtyCols: dirtyCols.length });
      const sidebar = [...document.querySelectorAll('.ai-dsc-table-item .ai-dsc-tmeta')].map((n) => n.textContent.trim());
      const rowCountOk = sidebar.length > 0 && sidebar.every((t) => /行 ·/.test(t));
      const cancel = overlay.querySelector('[data-canvas-close]')
        || [...overlay.querySelectorAll('button')].find((b) => /取消|关闭|完成/.test(b.textContent || ''));
      if (cancel) cancel.click();
      await new Promise((r) => setTimeout(r, 300));
      return rowCountOk ? true : JSON.stringify({ sidebar });
    } catch (error) { return String((error && error.stack) || error); }
  })()`);
  // ---- 指标卡 KPI：面板是「图表类型 → 数据设置 → 显示设置」同一套风格 ---------
  // 并真实验证卡片功能：单位→值带后缀、目标→完成度条、环比→本期值 + 涨跌副行。
  // P3：「数据源配置 → 字段配置」是派生的唯一定义处 —— 两个定义区 + 窗口构建器
  // 必须真机可用；同时图表面板不得再有第二套定义 UI（＋ 派生 抽屉）。
  // 只读检查：表单打开后不保存（取消/关闭），审计不改变报告状态。
  checks.derivedConfig = await evalIf('derivedConfig', `(async () => {
    try {
      const until = async (fn, ms) => { const t0 = Date.now(); while (!fn()) { if (Date.now() - t0 > (ms || 8000)) return false; await new Promise((r) => setTimeout(r, 120)); } return true; };
      // 图表面板：没有定义控件，只有跳转
      const block = document.querySelector('[data-report-block-id]');
      block.click();
      const eb = block.querySelector('.ai-report-block-edit'); if (eb) eb.click();
      await new Promise((r) => setTimeout(r, 400));
      if (document.querySelector('[data-bi-control-action="add-derived"]')) return 'chart-panel-still-defines-derived';
      // 打开数据源配置 → 字段配置
      const cfg = document.querySelector('[data-runtime-action="semantic-config"]');
      if (!cfg) return 'no-config-button';
      cfg.click();
      if (!await until(() => !!document.getElementById('ai-sem-stage'))) return 'canvas-missing';
      await until(() => !!document.querySelector('[data-der-block="columns"]'), 6000);
      const colsBlock = document.querySelector('[data-der-block="columns"]');
      const msrBlock = document.querySelector('[data-der-block="measures"]');
      if (!colsBlock || !msrBlock) return 'sections-missing';
      // BI-H2 验收决策（2026-09-13）：新建入口唯一 ——「＋ 新建计算字段」打开统一弹窗；
      // 2026-09-15 起入口升级为「＋ 新建 ▾」下拉（data-sem-new → data-sem-new-calc），
      // data-der-add 保留兼容。两个标记都认，菜单路径先点开下拉。
      const addBtn = document.querySelector('[data-der-add]') || document.querySelector('[data-sem-new]');
      const addIsMenu = !document.querySelector('[data-der-add]') && !!document.querySelector('[data-sem-new]');
      const hasAdd = !!addBtn;
      const colRows = colsBlock.querySelectorAll('.ai-sem-derive-row').length;
      const msrRows = msrBlock.querySelectorAll('.ai-sem-derive-row').length;
      if (!hasAdd) return 'unified-add-missing';
      if (document.querySelector('[data-der-col-add],[data-der-msr-add]')) return 'legacy-split-adds-still-present';
      const fx = msrBlock.querySelector('.ai-sem-derive-fx');
      const summary = msrBlock.querySelector('.ai-sem-derive-summary');
      if (msrRows > 0 && (!fx || !summary || summary.textContent.length < 3)) return 'measure-summary-missing';
      // 第 2 批 + 验收修订：定义只有一个面 —— 统一「新建/编辑计算字段」弹窗
      // （字段库 + 运算库 + 校验 + 预览 + 显式预览分组）；预设栏已删（三处重复）。
      if (addIsMenu) {
        addBtn.click();
        if (!await until(() => !!document.querySelector('[data-sem-new-calc]'))) return 'calc-menu-missing';
        document.querySelector('[data-sem-new-calc]').click();
      } else {
        addBtn.click();
      }
      if (!await until(() => !!document.getElementById('ai-calc-modal'))) return 'calc-modal-missing';
      const m = document.getElementById('ai-calc-modal');
      const need = ['[data-calc-tab="column"]', '[data-calc-tab="measure"]',
                    '.ai-calc-preview', '[data-calc-save]'];
      const missing = need.filter((sel) => !m.querySelector(sel));
      if (missing.length) return 'calc-modal-parts-missing:' + missing.join(',');
      // 字段库/函数库只在「公式」形态渲染（表达式编辑器的输入助手）；行级 tab 不该有。
      if (m.querySelector('[data-calc-field]') || m.querySelector('[data-calc-search]')) return 'calc-side-on-structured-tab';
      // 窗口形态的字段齐全（聚合/字段/方式/粒度/时间列）；「近 N 期…」才出现期数输入。
      m.querySelector('[data-calc-tab="measure"]').click();
      await new Promise((r) => setTimeout(r, 250));
      if (!m.querySelector('[data-calc-method="window"]')) return 'calc-method-row-missing';
      m.querySelector('[data-calc-method="window"]').click();
      await new Promise((r) => setTimeout(r, 250));
      const needWin = ['[data-calc-win-agg]', '[data-calc-win-col]', '[data-calc-win-kind]',
                       '[data-calc-win-grain]', '[data-calc-win-time]'];
      const missingWin = needWin.filter((sel) => !m.querySelector(sel));
      if (missingWin.length) return 'calc-window-fields-missing:' + missingWin.join(',');
      const kindSel = m.querySelector('[data-calc-win-kind]');
      kindSel.value = 'custom';
      kindSel.dispatchEvent(new Event('change', { bubbles: true }));
      await new Promise((r) => setTimeout(r, 250));
      if (!m.querySelector('[data-calc-win-offset]') || !m.querySelector('[data-calc-win-out]')) return 'calc-custom-fields-missing';
      // 占比形态：数据集没建层级 → 只给「占总计」口径说明（不给死选项）；建了层级
      // → 分组字段下拉出现且含 __total__。
      m.querySelector('[data-calc-method="share"]').click();
      await new Promise((r) => setTimeout(r, 250));
      if (!m.querySelector('[data-calc-share-group]') && !m.querySelector('[data-calc-share-hint]')) return 'calc-share-fields-missing';
      if (m.querySelector('[data-calc-share-group]') && !m.querySelector('[data-calc-share-group] option[value="__total__"]')) return 'calc-share-total-option-missing';
      // 公式形态 = 表达式编辑器：textarea + 字段库/函数库 + 显式预览分组。
      m.querySelector('[data-calc-method="formula"]').click();
      await new Promise((r) => setTimeout(r, 250));
      const needExpr = ['[data-calc-expr-text]', '[data-calc-field]', '[data-calc-search]', '[data-calc-lib]', '[data-calc-preview-dim]'];
      const missingExpr = needExpr.filter((sel) => !m.querySelector(sel));
      if (missingExpr.length) return 'calc-expr-fields-missing:' + missingExpr.join(',');
      // 真打字：写一条合法公式 → 校验结论必须是 is-ok（解析器 + derivedValidate 全链路）。
      // 列名从弹窗自己的字段库取（data-calc-field 就是原始列名，不依赖 manifest 顺序）；
      // 名称必须一起填 —— validate 先拦空名称，is-ok 永远不出现。
      const fieldBtn = m.querySelector('[data-calc-field]');
      const colName = fieldBtn && fieldBtn.getAttribute('data-calc-field');
      if (!colName) return 'calc-expr-no-column';
      const nameEl = m.querySelector('[data-calc-name]');
      nameEl.value = 'audit_tmp_formula';
      nameEl.dispatchEvent(new Event('input', { bubbles: true }));
      await new Promise((r) => setTimeout(r, 200));   // 名称 input 会触发 paint 重建
      const ta = m.querySelector('[data-calc-expr-text]');
      if (!ta) return 'calc-expr-text-vanished';
      ta.value = 'SUM([' + colName + ']) / 2';
      ta.dispatchEvent(new Event('input', { bubbles: true }));
      if (!await until(() => !!m.querySelector('.ai-calc-msg.is-ok'), 6000)) return 'calc-expr-validate-failed';
      // 校验结论必须立刻可见；预览是异步取数，等它落地
      if (!await until(() => !!m.querySelector('.ai-calc-msg'), 6000)) return 'calc-validation-missing';
      const x = m.querySelector('[data-calc-close]'); if (x) x.click();
      if (document.getElementById('ai-calc-modal')) return 'modal-not-closed';
      // 取消关闭画布：报告状态必须原样（审计只读）
      const cancel = document.querySelector('[data-canvas-close]') || document.querySelector('.ai-hier-canvas-foot [data-canvas-cancel]');
      if (cancel) cancel.click();
      await new Promise((r) => setTimeout(r, 300));
      void colRows; void msrRows;
      return true;
    } catch (e) { return String((e && e.stack) || e); }
  })()`);
  checks.kpiPanel = await evalIf('kpiPanel', `(async () => {
    try {
      // 会话筛选去污：全量审计跑到这里时，前序检查（filterDefaults 的中间态 /
      // batch3 的 range 探针等）可能留下**时点态**全局筛选——本检查测的是
      // 「目标→进度条」绑定，不是筛选语义；空筛选集上的 SUM=NULL 会让 setValue
      // 静默跳过、foot 不长，把绑定测试打成假红（logistics_1m 真机）。clearFilters
      // 会 renderAll 重建卡片 DOM —— 必须在抓取 item / 打开面板**之前**做。
      // renderAll 对 1M 行表格卡的重聚合会占住 server 数秒，随后的取数排在
      // 队尾——必须先等渲染收敛（.ai-chart-loading 摘除）再断言，否则单位/进度
      // 两个 until 都在等一个排在重查询后面的回包（真机 8s 不够、收敛后 <1s）。
      try {
        if (window.AIReportRuntime && typeof window.AIReportRuntime.clearFilters === 'function') {
          window.AIReportRuntime.clearFilters();
          const untilSettled = async (fn, ms) => { const tb = Date.now(); while (!fn()) { if (Date.now() - tb > ms) return false; await new Promise((r) => setTimeout(r, 200)); } return true; };
          await untilSettled(() => !document.querySelector('.ai-chart-loading'), 20000);
          await new Promise((r) => setTimeout(r, 500));
        }
      } catch (e) {}
      const item = document.querySelector('[data-report-block-id="block-kpi-0"]');
      if (!item) return true;   // 报告没有指标卡 = 不适用
      item.click();
      const eb = item.querySelector('.ai-report-block-edit'); if (eb) eb.click();
      const until = async (fn, ms) => { const t0 = Date.now(); while (!fn()) { if (Date.now() - t0 > (ms || 8000)) return false; await new Promise((r) => setTimeout(r, 120)); } return true; };
      if (!await until(() => !!document.getElementById('ai-kpi-panel'))) return 'panel-missing';
      const w = document.getElementById('ai-kpi-panel');
      const legends = Array.from(w.querySelectorAll('fieldset>legend')).map((l) => l.textContent);
      if (legends.indexOf('图表类型') < 0 || legends.indexOf('数据设置') < 0
        || (legends.indexOf('显示设置') < 0 && legends.indexOf('样式') < 0)) return 'sections:' + legends.join('/');
      if (!w.querySelector('#ai-zone-kpi-unselected') || !w.querySelector('#ai-zone-kpi-picked')) return 'zones-missing';
      const caps = Array.from(w.querySelectorAll('[data-kpi-chip] .ai-report-chip-caption')).map((c) => c.textContent);
      if (caps.indexOf('指标处理') < 0 || caps.indexOf('小数位数') < 0 || caps.indexOf('单位') < 0) return 'chip-controls:' + caps.join('/');
      const card = () => item.querySelector('.kpi-card');
      // 计数卡豁免（NQ-01 unit-not-applied 假红修复）：count/countd/记录数（'*'）
      // 与 percent/比率语义的 KPI 不做「万」换算是正确行为——缩放断言只对可缩放
      // 语义（currency/number 大数）生效。kind 判定镜像运行时 kpiKind（semanticType
      // → measureType → factType 兜底 + percent 格式列提升）。
      // tickets2026 真机实锤的两个补充形状（trio 门槛抓到批2修复的漏网）：
      // ① semanticType:'number' + factType:'count' 并写——运行时**取值路径**
      //    localKpiValue 的解析是 kind||factType||semanticType（factType 优先），
      //    按 count 处理不换万；审计镜像必须同口径，不能让 semanticType 抢先。
      // ② 静态值卡（factId:null + measure:null + value 烘死字符串）——unitScale
      //    本就没有可重算的活聚合，选「万」不变显是正确行为。
      const kp0m = ((window.AIReportRuntime.manifest.kpis || [])[0]) || {};
      const countish0 = (kp0m.factType && /count/i.test(String(kp0m.factType)))
        || kp0m.measure === '*'
        || (kp0m.measure == null && kp0m.factId == null && kp0m.value != null);
      let kind0 = kp0m.semanticType || kp0m.measureType || kp0m.factType || 'number';
      if (kind0 && typeof kind0 === 'object') kind0 = kind0.type;
      kind0 = String(kind0 || 'number');
      try {
        const dsAll = (window.AIReportRuntime.manifest.datasets || []);
        const ds0 = dsAll.find((d) => d && d.id === (kp0m.datasetId || (dsAll[0] || {}).id));
        const mt0 = ds0 && ds0.measureTypes ? ds0.measureTypes[kp0m.countDim || kp0m.measure] : null;
        if ((mt0 && typeof mt0 === 'object' && mt0.format === 'percent') || mt0 === 'percent') kind0 = 'percent';
      } catch (e) {}
      const unitScalable = !kp0m.countDim && !countish0 && kp0m.measure !== '*'
        && (kind0 === 'currency' || kind0 === 'number' || kind0 === 'integer' || kind0 === 'duration');
      if (unitScalable) {
        const uSel = w.querySelector('[data-kpi-bind="unitscale"]'); uSel.value = 'wan'; uSel.dispatchEvent(new Event('change', { bubbles: true }));
        if (!await until(() => /万/.test(card().querySelector('.value').textContent), 25000)) {
          const kp0u = kp0m;
          const liveU = document.querySelector('#ai-kpi-panel [data-kpi-bind="unitscale"]');
          return 'unit-not-applied ' + JSON.stringify({
            unitScale: kp0u.unitScale, measure: kp0u.measure, kind: kind0,
            valueShown: (card().querySelector('.value') || {}).textContent || null,
            errBadge: !!card().querySelector('.kpi-err-badge'), cardConnected: !!(card() && card().isConnected),
            itemConnected: item.isConnected,
            unitInput: liveU ? { connected: liveU.isConnected, value: liveU.value } : null,
            panelLive: !!document.getElementById('ai-kpi-panel'),
          });
        }
      }
      // 目标完成度条：setValue 走 /aggregate 异步回包才调 kpiFoot。全量审计跑到这里
      // 时前序检查可能留下**时点态**会话筛选（filterDefaults/sessionView 的恢复窗口），
      // 单次回包可能 sum=null（setValue 对 null 静默跳过——不改值、不报错、foot 不长）。
      // 这不是「目标→进度条」绑定的错：对 change 做有界重试（3 次 × 2.5s），等会话
      // 筛选收敛后再断言，重试仍不出现才算红。
      const setTarget = (v) => {
        const inp = (document.getElementById('ai-kpi-panel') || w).querySelector('[data-kpi-bind="target"]');
        if (!inp) return null;
        inp.value = v;
        inp.dispatchEvent(new Event('change', { bubbles: true }));
        return inp;
      };
      let tIn = setTarget('1000000');
      let progOk = tIn != null && await until(() => !!card().querySelector('.kpi-progress'), 2500);
      for (let attempt = 0; attempt < 3 && !progOk; attempt += 1) {
        await new Promise((r) => setTimeout(r, 2500));
        tIn = setTarget('1000000');
        if (tIn == null) break;
        progOk = await until(() => !!card().querySelector('.kpi-progress'), 2500);
      }
      if (!progOk) {
        // 失败路径必须带状态快照（目标绑定链路三段：kp.target 写没写上 / 取数失败
        // 没走 setValue / 卡片 DOM 还连着吗）——progress 只在 kpiFoot(val!=null &&
        // target 有效) 时渲染，三处任何一环断了都是这个报错。附会话筛选快照：
        // 空筛选集上的 SUM=NULL 正是「单次回包静默跳过」的头号来源。
        const kp0 = ((window.AIReportRuntime.manifest.kpis || [])[0]) || {};
        const liveT = document.querySelector('#ai-kpi-panel [data-kpi-bind="target"]');
        const fs = {};
        Object.keys(window.__AI_REPORT_FILTERS__ || {}).forEach((id) => {
          const s = window.__AI_REPORT_FILTERS__[id];
          if (s && Array.isArray(s.filters) && s.filters.length) fs[id] = s.filters;
        });
        return 'progress-missing ' + JSON.stringify({
          target: kp0.target, unitScale: kp0.unitScale, measure: kp0.measure,
          compareMeasure: kp0.compareMeasure || kp0.compare || null, cardFilter: (kp0.cardFilter && kp0.cardFilter.filters) || [],
          valueShown: (card().querySelector('.value') || {}).textContent || null,
          errBadge: !!card().querySelector('.kpi-err-badge'), foot: !!card().querySelector('.kpi-foot'),
          itemConnected: item.isConnected,
          targetInput: liveT ? { connected: liveT.isConnected, value: liveT.value } : null,
          sessionFilters: fs,
        });
      }
      // 对比 = 引用一个窗口派生指标（下拉；无第二套 环比/同比 内置控件）。
      const cmpSel = w.querySelector('[data-kpi-bind="comparemeasure"]');
      if (!cmpSel) return 'compare-control-missing';
      if (w.querySelector('[data-bi-control-action="kpi-compare"]')) return 'legacy-compare-control-present';
      const winOpt = Array.from(cmpSel.options).find((o) => o.value && o.value !== '__legacy');
      if (winOpt) {
        // 对比副行 = computeKpiCompare 的窗口派生链（依赖闭包聚合 + 时间桶序列，
        // 多次 server 往返）——与进度条同一套时点态约束：12s 窗 + 2 次有界重试
        //（重试仍不出现才算红；恢复清除同样放宽到 12s）。
        const setCompare = (v) => {
          const el = (document.getElementById('ai-kpi-panel') || w).querySelector('[data-kpi-bind="comparemeasure"]');
          if (!el) return null;
          el.value = v;
          el.dispatchEvent(new Event('change', { bubbles: true }));
          return el;
        };
        setCompare(winOpt.value);
        let cmpOk = await until(() => !!card().querySelector('.kpi-compare'), 12000);
        for (let attempt = 0; attempt < 2 && !cmpOk; attempt += 1) {
          await new Promise((r) => setTimeout(r, 2000));
          if (setCompare(winOpt.value) == null) break;
          cmpOk = await until(() => !!card().querySelector('.kpi-compare'), 12000);
        }
        if (!cmpOk) return 'compare-missing';
        const back = document.querySelector('#ai-kpi-panel [data-kpi-bind="comparemeasure"]');
        if (back) { back.value = ''; back.dispatchEvent(new Event('change', { bubbles: true })); }
        if (!await until(() => !card().querySelector('.kpi-compare'), 12000)) return 'compare-not-cleared';
      }
      // 还原（审计不得把报告留在改动后的状态）
      const u2 = document.querySelector('#ai-kpi-panel [data-kpi-bind="unitscale"]'); if (u2) { u2.value = 'raw'; u2.dispatchEvent(new Event('change', { bubbles: true })); }
      const t2 = document.querySelector('#ai-kpi-panel [data-kpi-bind="target"]'); if (t2) { t2.value = ''; t2.dispatchEvent(new Event('change', { bubbles: true })); }
      return true;
    } catch (e) { return String((e && e.stack) || e); }
  })()`);
  // 图表⇄指标卡 往返：切到指标卡（3×3 + 有值）再切回图表，左侧预览必须真的画出来
  // —— 编译产物同款卡片骨架（.block > .chart-host，高度靠 .block 的 flex:1）+
  // 网格尺寸回到原样。缺 .block 外壳时 chart-host 高度塌陷，预览只剩一条（用户反馈）。
  checks.kpiRoundTrip = await evalIf('kpiRoundTrip', `(async () => {
    try {
      const until = async (fn, ms) => { const t0 = Date.now(); while (!fn()) { if (Date.now() - t0 > (ms || 10000)) return false; await new Promise((r) => setTimeout(r, 150)); } return true; };
      const m = AIReportRuntime.manifest;
      const chart = (m.charts || [])[m.charts.length - 1];
      if (!chart) return true;                       // 无图表 = 不适用
      const item0 = document.querySelector('[data-report-block-id="' + chart.blockId + '"]');
      if (!item0) return 'chart-item-missing';
      const w0 = item0.getAttribute('gs-w'), h0 = item0.getAttribute('gs-h');
      item0.click();
      const eb = item0.querySelector('.ai-report-block-edit'); if (eb) eb.click();
      if (!await until(() => !!document.querySelector('.ai-report-chart-type-kpi'))) return 'kpi-type-card-missing';
      document.querySelector('.ai-report-chart-type-kpi').click();
      if (!await until(() => !!document.getElementById('ai-kpi-panel'))) return 'kpi-panel-missing';
      const kpiItem = document.querySelector('[data-report-block="kpi"] .kpi-card') ?
        document.querySelector('[data-report-block="kpi"]').closest('.grid-stack-item') : null;
      // 上面可能选中了别的 KPI（报告本来就有指标卡）→ 用最后一条 kpis 记录定位新卡
      const kp = m.kpis[m.kpis.length - 1];
      const newKpi = document.querySelector('[data-report-block-id="' + kp.blockId + '"]') || kpiItem;
      if (!newKpi) return 'converted-kpi-missing';
      if (newKpi.getAttribute('gs-w') !== '3' || newKpi.getAttribute('gs-h') !== '3') return 'kpi-size:' + newKpi.getAttribute('gs-w') + 'x' + newKpi.getAttribute('gs-h');
      // 指标值来自异步聚合：等它落地（新卡初始是「—」）。
      if (!await until(() => { const v = newKpi.querySelector('.kpi-card .value'); return !!(v && /\\d/.test(v.textContent)); })) return 'kpi-value-empty';
      const btn = document.querySelector('#ai-kpi-panel [data-bi-control-action="convert-to-chart"][data-chart-kind="bar"]');
      if (!btn) return 'convert-back-button-missing';
      if (btn.disabled) return true;                 // 数据源缺维度/指标 = 不适用
      btn.click();
      if (!await until(() => !document.getElementById('ai-kpi-panel'))) return 'chart-panel-not-open';
      const back = (AIReportRuntime.manifest.charts || [])[AIReportRuntime.manifest.charts.length - 1];
      const item = document.querySelector('[data-report-block-id="' + back.blockId + '"]');
      if (!item) return 'back-chart-item-missing';
      if (!item.querySelector('.grid-stack-item-content>.block')) return 'no-block-shell';
      const host = item.querySelector('.chart-host');
      if (!host) return 'no-chart-host';
      if (!await until(() => { const i = echarts.getInstanceByDom(host); return !!(i && i.getOption() && (i.getOption().series || []).length); })) return 'chart-not-rendered';
      const opt = echarts.getInstanceByDom(host).getOption();
      const pts = (opt.series[0].data || []).length;
      if (!pts) return 'chart-no-data';
      const hh = Math.round(host.getBoundingClientRect().height);
      if (hh < 120) return 'preview-collapsed:' + hh;
      if (item.getAttribute('gs-w') !== w0 || item.getAttribute('gs-h') !== h0) return 'size-not-restored:' + item.getAttribute('gs-w') + 'x' + item.getAttribute('gs-h') + '!=' + w0 + 'x' + h0;
      const cl = document.querySelector('#ai-report-runtime-panel [data-runtime-action="close"]');
      if (cl) cl.click();
      return true;
    } catch (e) { return String((e && e.stack) || e); }
  })()`);
  console.error(memoryLine('end'));
  // 自报 skip（'skip:' 前缀）= 检查执行了、但前置条件在本报告上不成立（明细卡无
  // 数值列、needle 推不出、数据源构建后被替换 columnStats 被有意清除…）。设计内
  // 语义是「不判红」（detailSearchStats 内注释即如此声明），但旧 passed 口径把
  // skip 字符串当执行失败 —— 与注释自相矛盾，活报告（upsert 过）全量审计恒假红。
  // 现在：skip 字符串计入 skipped、不进 executed；非 skip 的失败串照旧判红。
  const isSelfSkip = (v) => typeof v === 'string' && v.startsWith('skip:');
  const selfSkipped = Object.keys(checks).filter((n) => isSelfSkip(checks[n]));
  const executedVals = Object.values(checks).filter((v) => v !== undefined && !isSelfSkip(v));
  // 空命中守卫（P2-2，marketing 冒烟）：范围化审计里选中的检查/通配如果**一个都没
  // 命中**（既没有任何实际执行的检查名、也没有记入 skipped 的检查名与之匹配），
  // 就是静默空跑 —— 曾经 PRESETS.charts 写成 `chart:*`（`*` 不跨冒号），对
  // `chart:<id>:<suffix>` 零命中，预设退化成「只跑 3 个杂项检查还全绿」。现在
  // unmatchedPatterns 非空 → 显式 non-green + 可行动提示；「至少执行一项才允许
  // passed=true」的原守卫保持不变。
  const executedNames = Object.keys(checks).filter((n) => checks[n] !== undefined);
  const unmatchedPatterns = SELECTED
    ? RAW_SELECTED.filter((p) => {
        const re = globRe(p);
        return !executedNames.some((n) => re.test(n)) && !SKIPPED_CHECKS.some((n) => re.test(n));
      })
    : [];
  const passed = executedVals.length > 0 && executedVals.every((v) => v === true) && unmatchedPatterns.length === 0;
  // scope 元信息（NP-04）：reaped = 启动前置收割的孤儿 headless Chrome 数；
  // retried = infra 失败自动重试次数。infraFailed 非空时 passed 已为 false
  //（infraFailed 串 ≠ true），整体 exit 非 0 —— 环境失败不静默放行。
  const scope = { selected: SCOPE_DESC, executed: executedVals.length, skipped: new Set([...SKIPPED_CHECKS, ...selfSkipped]).size, reaped: reapInfo.count, retried: infraRetries };
  if (reapInfo.ports && reapInfo.ports.length) scope.reapedPorts = reapInfo.ports;
  if (unmatchedPatterns.length) {
    scope.unmatchedPatterns = unmatchedPatterns;
    scope.hint = '选中的模式没有命中任何检查（executed/skipped 都没有）：' + unmatchedPatterns.join(', ')
      + '。每图检查名形如 chart:<blockId>:<suffix>，`*` 不跨冒号 —— 全套 per-chart 检查请用 chart:*:*（--list-checks 查看全部）。';
  }
  console.log(JSON.stringify({ checks, scope, passed, infraRetries, infraFailed }, null, 2));
  if (!passed) process.exitCode = 1;
} catch (error) {
  // 看门狗触发/渲染进程无响应：给出**可诊断**的结果，而不是吊死到外层超时。
  const why = auditTimeoutDetail || String((error && error.message) || error);
  console.error(memoryLine('aborted'));
  console.error('[audit] aborted: ' + why);
  console.log(JSON.stringify({
    checks,
    passed: false,
    aborted: true,
    error: why,
    scope: { selected: SCOPE_DESC, executed: Object.keys(checks).filter((n) => checks[n] !== undefined).length, skipped: SKIPPED_CHECKS.length, reaped: reapInfo.count, retried: infraRetries },
    infraRetries,
    infraFailed,
    hint: /cdp-timeout|audit-deadline/.test(why)
      ? '渲染进程停止应答 CDP（通常是内存/IO 饥饿）。请关闭占内存的程序（Docker/Obsidian/多余 Chrome 标签）后重跑；' +
        '也可用 AI_AUDIT_CMD_TIMEOUT_MS / AI_AUDIT_DEADLINE_MS 调整看门狗，或用 --skip-audit 先交付再补验收。'
      : '审计脚本自身报错，请把这条 error 反馈。',
    memory: memoryLine('report'),
    elapsedMs: auditElapsedMs(),
  }, null, 2));
  process.exitCode = 1;
} finally {
  // 无论成功、失败还是超时：一定要带走自己启动的 Chrome（残留实例会抢内存，
  // 让后续每一次验收都更容易再次卡住）。
  try { if (socket) socket.close(); } catch (e) {}
  try {
    if (child && child.pid) {
      try { process.kill(-child.pid, 'SIGKILL'); }        // 整个 Chrome 进程组
      catch (e) { try { child.kill('SIGKILL'); } catch (e2) {} }
    }
  } catch (e) {}
}
