"""HTML template provider: serves report views from the server.

The report HTML is small: a shell, the (row-stripped) manifest inlined,
the vendored runtime (ECharts, GridStack, editor runtime) inlined.

The full HTML is rebuilt on every request (cheap; no caching needed for
a single localhost server). The runtime that runs inside it talks to
the server via fetch() — the manifest advertises the serverBase.

LAYOUT UNIFICATION: the server view must look identical to a singlefile
report. Rather than re-implementing cover/KPI/filter/chart geometry here,
we reuse the same deterministic recipes that the canonical compiler
(``scripts/spec_to_report.py``) uses: ``pack_layout``, ``_default_h``,
``_cover_h``, ``_format_kpi`` and the ``SHELL_CSS`` stylesheet. This keeps
the server and singlefile renderers from drifting (one source of truth).
"""
from __future__ import annotations

import json
import html
import math
import re
import socket
import sys
from pathlib import Path

from . import SKILL_VERSION
from typing import Any

from .registry import Registry
from .app import _server_strip

SKILL_DIR = Path(__file__).resolve().parent.parent

# The canonical compiler lives under scripts/ and is a flat module; make it
# importable regardless of cwd so we share its layout/format/CSS recipes.
for _p in (str(SKILL_DIR), str(SKILL_DIR / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from scripts.spec_to_report import (           # noqa: E402
    SHELL_CSS,
    pack_layout,
    _default_h,
    _cover_h,
    _filter_h,
    render_grid_html,
)


def _read(name: str, base: Path) -> str:
    return (base / name).read_text(encoding="utf-8")


def _apply_shell_tokens(css: str, tokens: dict[str, Any]) -> str:
    """Inject named visual tokens into SHELL_CSS (same targets as build_html).

    Targets must match SHELL_CSS exactly (which declares --accent:#5645d4,
    --radius:12px, padding:32px 24px 64px, font-size:15px;line-height:1.55).
    The density map must agree with spec_to_report._load_theme, or the compact
    spacing differs between singlefile and server mode.
    """
    accent = tokens.get("accent") if isinstance(tokens.get("accent"), str) else "#5645d4"
    radius = {"none": "0px", "small": "8px", "medium": "12px"}.get(tokens.get("radius"), "12px")
    density = {"compact": "24px 24px 48px", "comfortable": "32px 24px 64px"}.get(tokens.get("density"), "32px 24px 64px")
    font_size = {"sm": "14px", "md": "15px", "lg": "16px"}.get(tokens.get("fontScale"), "15px")
    css = css.replace("--accent:#5645d4", "--accent:" + accent)
    css = css.replace("--radius:12px", "--radius:" + radius)
    css = css.replace("padding:32px 24px 64px", "padding:" + density)
    css = css.replace("font-size:15px;line-height:1.55", "font-size:" + font_size + ";line-height:1.55")
    return css


def _lan_ip() -> str:
    """Best-effort LAN address of this machine (for share links)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.3)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip:
            return ip
    except Exception:
        pass
    return "127.0.0.1"


# CSS custom property names only (--*). build_manifest 只会烘出这种键，但在线保存
# 会把浏览器发来的 manifest 原样落盘，渲染端必须把非 --* 的键挡在 style 属性外。
_THEME_VAR_KEY_RE = re.compile(r"^--[A-Za-z0-9_-]+$")


def _theme_vars_style(vars_map: dict[str, Any]) -> str:
    """Render ``themeShells[*].vars`` as an inline style-attribute value.

    安全（P2，2026-09-16）：vars 值曾被逐字拼进 ``<html style="...">``——一个
    ``}" onmouseover=...`` 之类的值可以逃出 style 属性注入属性/事件。现在键白名单
    为 CSS 自定义属性（--*），值经 html.escape（含引号），属性无法被逃逸。app.py
    侧 h_update_manifest 在落盘前还会丢弃非 --* 键（同一闸门的持久化侧）。
    """
    parts = []
    for key in sorted(vars_map or {}):
        k = str(key)
        if not _THEME_VAR_KEY_RE.match(k):
            continue
        parts.append(f"{k}:{html.escape(str(vars_map[key]), quote=True)}")
    return ";".join(parts)


class TemplateProvider:
    """Renders report HTML pages."""

    def __init__(self, registry: Registry, port: int = 8765):
        self.registry = registry
        self._port = int(port)
        # Pre-load static assets once. NOTE: these are cached at server start;
        # editing them requires restarting the server (documented in SKILL.md).
        self._echarts = _read("echarts.min.js", SKILL_DIR / "assets" / "vendor")
        self._gridstack_js = _read("gridstack.all.js", SKILL_DIR / "assets" / "vendor")
        self._gridstack_css = _read("gridstack.min.css", SKILL_DIR / "assets" / "vendor")
        self._editor_css = _read("editor-runtime.css", SKILL_DIR / "assets" / "runtime")
        self._runtime_js = _read("editor-runtime.js", SKILL_DIR / "assets" / "runtime")
        self._adapter_js = _read("chart-adapter.js", SKILL_DIR / "assets" / "runtime")
        self._gallery_js = _read("gallery-presets.js", SKILL_DIR / "assets" / "runtime")
        self._matcher_js = _read("column-matcher.js", SKILL_DIR / "assets" / "runtime")
        self._xlsx_js = _read("xlsx-reader.js", SKILL_DIR / "assets" / "runtime")
        self._presentation_js = _read("presentation.js", SKILL_DIR / "assets" / "runtime")
        self._pptx_js = _read("pptx-exporter.js", SKILL_DIR / "assets" / "runtime")

    def render(self, report_id: str) -> str | None:
        entry = self.registry.get(report_id)
        if entry is None:
            return None
        manifest = _server_strip(dict(entry.get("manifest", {})))
        manifest.setdefault("mode", "server")
        manifest.setdefault("serverBase", "/api/v1")
        manifest.setdefault("schemaVersion", 3)
        manifest.setdefault("shareOrigin", f"http://{_lan_ip()}:{self._port}")
        # The registry key is authoritative: the runtime derives its save URL
        # from manifest.reportId, so a stale manifest id would 404 every save.
        manifest["reportId"] = report_id

        tokens = (manifest.get("theme") or {}).get("tokens") or {}
        # Unify with the singlefile compiler: both stylesheets, token-applied.
        report_css = _apply_shell_tokens(SHELL_CSS, tokens)
        runtime_css = self._editor_css
        # legacy tokens 只保留仍有锚点的注入：editor-runtime.css var() 化（主题
        # 轴收敛 B 批）后 "#2563eb" 与 "padding:32px 24px 64px" 两个旧锚点已
        # 0 命中，替换是安静 no-op——删除，免得假装生效。
        radius = {"none": "0px", "small": "8px", "medium": "12px"}.get(tokens.get("radius"), "12px")
        runtime_css = runtime_css.replace("border-radius:12px", "border-radius:" + radius)
        font_size = {"sm": "13px", "md": "14px", "lg": "16px"}.get(tokens.get("fontScale"), "14px")
        runtime_css = runtime_css.replace("font:14px/1.45", "font:" + font_size + "/1.45")

        # Body: a saved edit session stores the editor's own grid snapshot
        # (bodyHtml, sanitized at save time in app.py). The derived render
        # (render_tabs_html / render_grid_html) re-builds the body from
        # charts+sections and never reads manifest.blocks, so serving it for an
        # edited manifest would show the PRE-EDIT layout after a refresh. Fall
        # back to the derived render for freshly compiled reports (no snapshot).
        body_html = entry.get("bodyHtml")
        if not isinstance(body_html, str) or not body_html.strip():
            body_html = _render_body(manifest)

        # 页面宽度档位（docs/page-width-and-table-autofit-design.md）：每次渲染从
        # entry manifest 现读（在线保存覆盖/副本后随新 manifest 生效）；默认 1280
        # 不写 attr，与编译侧同口径。归一真源 validate_request.resolve_page_width。
        from validate_request import resolve_page_width
        _pw = resolve_page_width(manifest.get("pageWidth"))
        shell_style = f' style="--page-max:{_pw}px"' if _pw != 1280 else ''
        # 报告主题（docs/report-theme-convergence-design.md）：主题覆盖变量写在
        # <html>（与编译侧同一层级，runtime applyShellTheme 也写 documentElement
        # ——若写 .report-shell，div 内联会压过运行时切换）。**恒写**（含 default，
        # A1 后 default accent≠SHELL_CSS 兜底字面值）；chartOnly 档 vars=default 壳。
        # 每次渲染从 entry manifest 现读，在线保存后随新 manifest 生效。
        _ct = manifest.get("chartTheme") or "default"
        _sh = (manifest.get("themeShells") or {}).get(_ct) or {}
        html_style = ''
        if _sh.get("vars"):
            html_style = ' style="' + _theme_vars_style(_sh["vars"]) + '"'

        # Keep user-authored title/insight text inside the JSON script element;
        # a literal ``</script>`` must not terminate it early.
        manifest_json = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")

        # Server-mode edit password is intentionally NOT shown on the report.
        # Allowing every reader to see the online-edit token leaks the save gate;
        # it is communicated at build time only (build console [password] line +
        # build/README.md + the server marker landing page). runtime.editPassword
        # still gates online save and is used by serverEditPassword() to prefill
        # the save dialog on the author's own machine.

        title = manifest.get("title", report_id)
        return _SHELL.format(
            title=_escape(title),
            manifest_json=manifest_json,
            report_body=body_html,
            shell_style=shell_style,
            html_style=html_style,
            report_css=report_css,
            runtime_css=runtime_css,
            gridstack_css=self._gridstack_css,
            echarts_js=self._echarts,
            gridstack_js=self._gridstack_js,
            adapter_js=self._adapter_js,
            gallery_js=self._gallery_js,
            matcher_js=self._matcher_js,
            xlsx_js=self._xlsx_js,
            presentation_js=self._presentation_js,
            pptx_js=self._pptx_js,
            runtime_js=self._runtime_js,
        )

    def render_index(self) -> str:
        reports = self.registry.list()
        items = []
        for e in reports:
            rid = e.get("reportId", "")
            title = e.get("title", rid)
            ds = ", ".join(d["id"] for d in e.get("datasets", []))
            items.append(f'<li><a href="/reports/{_escape(rid)}">{_escape(title)}</a> '
                         f'<span class="meta">id: <code>{_escape(rid)}</code> · dataset: <code>{_escape(ds)}</code></span></li>')
        body = "".join(items)
        if not items:
            # 空注册表（冷启动）入口文案（docs/compose-user-path-design.md D4）：
            # 指向 Agent 指令与本机管理页；不在这里放创建按钮——/reports 对局域网
            # 访客可见，创建入口属于报告主人的本机创作面。
            body = ('<li><em>还没有注册的报告。</em><br>'
                    '在本机对 AI 助手说「拼一份报告 / 开一张拼版画板」开始，'
                    '或打开 <a href="/manager">报告管理页</a>（仅本机）'
                    '点「＋ 新建拼版草稿」从空白画板拼起。</li>')
        return _INDEX.format(reports=body)

    def render_manager(self) -> str:
        """报告管理 page: all running report servers on this machine.

        Data comes from /api/reports/all on the SAME origin (this server reads
        the global servers index + each live server's registry). editPassword
        is only present in the payload for loopback viewers; the page renders
        whatever it receives and hides 关闭服务 for non-local visitors (the
        shutdown endpoint rejects them server-side anyway).

        页脚版本徽标（v__SKILL_VERSION__ 占位符）= 包级 SKILL_VERSION 单一真源：
        远程支持时「公司机跑的是哪个包」一眼可辨（与构建横幅 / /api/health 同源）。"""
        return _MANAGER_PAGE.replace('__SKILL_VERSION__', SKILL_VERSION)


_SHELL = """<!DOCTYPE html>
<html lang="zh-CN"{html_style}><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style id="ai-report-gridstack-css">{gridstack_css}</style>
<style id="ai-report-report-css">{report_css}</style>
<style id="ai-report-runtime-css">{runtime_css}</style>
</head>
<body>
<div class="report-shell"{shell_style}><section class="grid-stack" data-report-grid>{report_body}</section></div>
<script id="ai-report-manifest" type="application/json">{manifest_json}</script>
<script id="ai-report-echarts">{echarts_js}</script>
<script id="ai-report-gridstack">{gridstack_js}</script>
<script id="ai-report-gallery-presets">{gallery_js}</script>
<script id="ai-report-chart-adapter">{adapter_js}</script>
<script id="ai-report-column-matcher">{matcher_js}</script>
<script id="ai-report-xlsx-reader">{xlsx_js}</script>
<script id="ai-report-presentation">{presentation_js}</script>
<script id="ai-report-pptx-exporter">{pptx_js}</script>
<script id="ai-report-runtime">{runtime_js}</script>
</body></html>"""

_INDEX = """<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Report Server</title>
<style>
  body {{ font-family: -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif;
         background: #f7f8fb; color: #0f172a; margin: 0; padding: 32px; }}
  .wrap {{ max-width: 880px; margin: 0 auto; background: #fff; padding: 32px 40px;
          border-radius: 12px; box-shadow: 0 1px 3px rgba(15,23,42,.05); }}
  h1 {{ margin: 0 0 8px; font-size: 22px; }}
  .sub {{ color: #64748b; margin-bottom: 24px; }}
  ul {{ list-style: none; padding: 0; margin: 0; }}
  li {{ padding: 14px 16px; border: 1px solid #eef2f8; border-radius: 8px;
        margin-bottom: 8px; background: #fafbff; }}
  li a {{ color: #2563eb; text-decoration: none; font-weight: 600; }}
  li .meta {{ display: block; margin-top: 4px; font-size: 12px; color: #64748b; }}
  li code {{ background: #eef2f8; padding: 1px 6px; border-radius: 4px; font-size: 11px; }}
</style>
</head><body>
<div class="wrap">
  <h1>📊 AI Report Server</h1>
  <div class="sub">已注册报告（点击打开）：</div>
  <ul>{reports}</ul>
</div>
</body></html>"""

# 报告管理：本机所有运行中的报告 + 一键关闭。零依赖静态页（数据由
# /api/reports/all 提供）；设计 token 对齐 notion-DESIGN.md。
_MANAGER_PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>报告管理 · AI Report</title>
<style>
  :root{--accent:#5645d4;--ink:#1a1a1a;--ink2:#54555a;--steel:#787671;--line:#e5e3df;--line-strong:#c8c4be;--surface:#fff;--canvas:#f6f5f4;--danger:#c62f2f}
  *{box-sizing:border-box}
  body{font-family:Inter,-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;background:var(--canvas);color:var(--ink);margin:0;padding:32px 20px}
  .wrap{max-width:860px;margin:0 auto}
  h1{font-size:20px;margin:0;display:flex;align-items:center;gap:10px}
  .sub{color:var(--steel);font-size:13px;margin:8px 0 20px;line-height:1.6}
  .bar{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}
  .bar .hint{font-size:12px;color:var(--steel)}
  button{font-family:inherit}
  .btn{border:1px solid var(--line-strong);background:#fff;color:var(--ink);border-radius:8px;padding:6px 14px;font-size:12px;font-weight:600;cursor:pointer;white-space:nowrap}
  .btn:hover{background:var(--canvas)}
  .btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
  .btn.primary:hover{filter:brightness(1.05)}
  .btn.danger{background:var(--danger);border-color:var(--danger);color:#fff}
  .btn.danger:hover{filter:brightness(1.05)}
  .server{background:var(--surface);border:1px solid var(--line);border-radius:12px;margin-bottom:14px;overflow:hidden}
  .server-head{display:flex;align-items:center;gap:10px;padding:12px 18px;border-bottom:1px solid var(--line);background:#fafaf9}
  .server-head .port{font-family:ui-monospace,SFMono-Regular,monospace;font-size:11px;background:#ececea;border-radius:4px;padding:2px 8px;color:var(--ink2)}
  .server-head .meta{font-size:11px;color:var(--steel)}
  .server-head .grow{flex:1}
  .rep{display:flex;align-items:center;gap:14px;padding:14px 18px;border-bottom:1px solid var(--line)}
  .rep:last-child{border-bottom:0}
  .rep .info{flex:1;min-width:0}
  .rep .title{font-size:14px;font-weight:600;color:var(--ink);text-decoration:none}
  .rep .title:hover{color:var(--accent)}
  .rep .row2{display:flex;flex-wrap:wrap;gap:6px 10px;margin-top:5px;font-size:11px;color:var(--steel);align-items:center}
  .rep .row2 code{background:var(--canvas);border-radius:4px;padding:1px 6px;font-size:10px}
  .rep .pw{display:flex;align-items:center;gap:6px;font-size:11px;color:var(--steel)}
  .rep .editbar{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-top:7px;font-size:11px;color:var(--steel)}
  .rep .editbar .state{font-weight:600}
  .rep .editbar .state.on{color:#1a7f4b}
  .rep .editbar .state.off{color:#c62f2f}
  .switch{display:inline-flex;align-items:center;gap:6px;cursor:pointer;user-select:none;font-size:11px;color:var(--ink2)}
  .switch input{margin:0;cursor:pointer}
  .rep .btn.mini{padding:3px 10px;font-size:11px}
  .rep .pw code{background:#ececea;border-radius:4px;padding:1px 6px;font-family:ui-monospace,monospace;font-size:11px;color:var(--ink)}
  .rep .pw .btn.mini{padding:1px 8px;font-size:10px}
  .pw-form{display:inline-flex;align-items:center;gap:6px}
  .pw-input{border:1px solid var(--line-strong);border-radius:6px;padding:3px 8px;font-size:11px;font-family:inherit;width:150px}
  .pw-input:focus{outline:2px solid var(--accent);outline-offset:-1px}
  .shared-badge{font-size:10px;font-weight:700;background:#ececea;color:var(--ink2);border-radius:999px;padding:2px 9px;white-space:nowrap}
  .rep .actions{display:flex;gap:8px;align-items:center}
  .lan{color:var(--steel)}
  .empty{background:var(--surface);border:1px dashed var(--line-strong);border-radius:12px;padding:40px 24px;text-align:center;color:var(--steel);font-size:13px;line-height:1.8}
  .note{font-size:12px;color:var(--steel);line-height:1.8;margin-top:18px}
  .toast{position:fixed;left:50%;bottom:28px;transform:translateX(-50%);background:var(--ink);color:#fff;font-size:12px;padding:8px 16px;border-radius:8px;opacity:0;transition:opacity .2s;pointer-events:none}
  .toast.show{opacity:.92}
  .family-head .family-title{font-size:14px;font-weight:600;color:var(--ink)}
  .rep .badge{font-size:10px;font-weight:700;background:#ece9fb;color:#5645d4;border-radius:999px;padding:1px 7px}
  .rep .badge.badge-draft{background:#fdeeda;color:#b45309}
  .rep .log{margin-top:7px}
  .rep .log summary{font-size:11px;color:var(--accent);cursor:pointer;font-weight:600;list-style:none;user-select:none}
  .rep .log summary::-webkit-details-marker{display:none}
  .rep .log summary:before{content:'▸ '}
  .rep .log[open] summary:before{content:'▾ '}
  .rep .log-row{display:flex;gap:10px;font-size:11px;color:var(--steel);padding:3px 0 3px 12px;border-left:2px solid var(--line)}
  .rep .log-mode{font-weight:600;color:var(--ink2);min-width:64px}
</style>
</head><body>
<div class="wrap">
  <h1>🗂 报告管理 <span style="font-size:12px;font-weight:500;color:var(--steel);vertical-align:middle" id="skill-version">v__SKILL_VERSION__</span></h1>
  <div class="sub">这台电脑上所有<b>运行中</b>的 AI 报告服务。把链接发给同一网络里的同事即可访问；关闭服务后链接将不可用（可双击报告目录里的「打开报告」重新启动）。</div>
  <div class="bar">
    <span class="hint" id="updated-at"></span>
    <span><button type="button" class="btn primary" id="new-draft" title="从空白画板拼一份新报告：拖占位块、逐块一句话描述，拼完交给 AI 生成（仅本机可用）">＋ 新建拼版草稿</button>
    <button type="button" class="btn" id="refresh">↻ 刷新</button></span>
  </div>
  <div id="list"></div>
  <div class="note" id="disk-total" style="display:none"></div>
  <div class="note" id="foot-note">
    · <b>在线编辑</b>：关闭后任何人（含报告主人）都无法保存该报告，打开报告的人会看到「当前暂无法编辑保存报告，请联系管理员」；「设置口令」可随时改口令（改完请在报告页重新输入一次）。<br>
    · 「关闭服务」会停止该端口上的本地数据服务：其中的<b>所有报告（含副本）</b>都会下线，仅限在本机操作。全机共享同一服务时，该端口只有<b>第一个</b>报告族的族头有「关闭服务」，其余族头显示「共享服务」徽标；确认弹窗会点名列出全部受影响的报告。<br>
    · <b>移除</b>：把单个报告从运行列表下线（不影响同端口的其他报告）；报告文件夹仍在，可双击「打开报告」重新启动。<br>
    · <b>拼版草稿</b>是从空白画板拼报告的工作台（拖占位块 → AI 转正后原位变正式报告）；「删除草稿」只下线该草稿，不影响其他报告。<br>
    · 已停止的报告不会出现在这里；重新打开：双击报告文件夹里的 <b>打开报告.command</b>（macOS）或 <b>打开报告.cmd</b>（Windows）。
  </div>
</div>
<div class="toast" id="toast"></div>
<script>
(function(){
  'use strict';
  var isLocal=['127.0.0.1','localhost','[::1]','::1'].indexOf(location.hostname)>=0;
  var list=document.getElementById('list');
  // render() 每次全量重绘时回填的两张内存表（key=报告 id / 端口）：
  // pwByReport 供「显示/复制口令」按需取明文（行内恒遮蔽，V3）；
  // rowsByPort 供「关闭服务」的 confirm 点名该端口上全部受影响报告（V4）。
  var pwByReport={};
  var rowsByPort={};
  function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
  function toast(msg){var t=document.getElementById('toast');t.textContent=msg;t.classList.add('show');setTimeout(function(){t.classList.remove('show');},1800);}
  function copyText(url,label){
    var noun=label||'链接';
    if(navigator.clipboard&&navigator.clipboard.writeText){
      return navigator.clipboard.writeText(url).then(function(){toast(noun+'已复制');},function(){fallback();});
    }
    fallback();return Promise.resolve();
    function fallback(){var i=document.createElement('input');i.value=url;document.body.appendChild(i);i.select();var ok=false;try{ok=document.execCommand('copy');}catch(e){}document.body.removeChild(i);toast(ok?(noun+'已复制'):(noun+'复制失败，请手动复制：'+url));}
  }
  function shutdown(port){
    // V4：统一 manager 下全机一个 server——同端口的所有报告族（含副本）共享
    // 同一个服务，关闭即全部下线。confirm 必须点名列出全部受影响报告，
    // 不再只报份数（rowsByPort 由 render 按数据填充）。
    var reps=rowsByPort[String(port)]||[];
    var names=reps.map(function(r){return '「'+(r.title||r.reportId)+'」';}).join('、');
    var msg=reps.length>1
      ?('端口 '+port+' 上的服务由 '+reps.length+' 份报告共享：'+names+'。关闭后全部下线、链接不可访问。确定关闭？')
      :('确定关闭端口 '+port+' 上的报告服务？「'+((reps[0]&&(reps[0].title||reps[0].reportId))||'')+'」下线后链接不可访问。');
    if(!window.confirm(msg))return;
    fetch('http://127.0.0.1:'+port+'/api/admin/shutdown',{method:'POST',headers:{'Content-Type':'text/plain'},body:'{}'}).then(function(){
      toast('服务已关闭（端口 '+port+'）');setTimeout(load,600);
    }).catch(function(){
      // 服务可能已应答后立即退出；刷新看真实状态。
      setTimeout(load,600);
    });
  }
  // 修改日志的 mode 标签必须覆盖服务端会写进 changelog 的全部 mode：
  // initial/copy/overwrite（构建与保存）+ draft（h_create_draft 拼版草稿）
  // + replace-data/upsert-data/append-data（h_dataset_replace 的数据替换/
  // 增量合并/追加新行）。缺了会在管理页日志列显示生英文 mode（round6 审计发现）。
  function modeLabel(m){return ({initial:'初版创建',copy:'保存为副本',overwrite:'保存并覆盖',draft:'新建拼版草稿','upsert-data':'数据增量合并','replace-data':'数据全量替换','append-data':'追加新行'})[m]||m||'';}
  // 千分位（22,540,717）：管理页原先没有数字格式化工具，行数勾稽需要它。固定
  // en-US locale 保证输出确定（测试逐字断言），不接受运行环境 locale 漂移。
  function fmtNum(n){n=Number(n);if(!isFinite(n))return '0';return n.toLocaleString('en-US');}
  // 数据更新条目的行数勾稽（server/app.py h_dataset_replace 写 rows=表总行数
  // + upsert 的 inserted/updated + append 的 appended；这些条目没有 chartCount，
  // 旧渲染恒显示「0 图表」= 数据更新在唯一复盘处丢失勾稽）。mode 含 'data'
  // 才走勾稽分支；旧条目缺 rows 字段 → 回退「N 图表」绝不炸。
  function logMeta(c){
    var m=String((c&&c.mode)||'');
    var hasRows=(typeof (c&&c.rows)==='number')||(typeof (c&&c.rows)==='string'&&c.rows!==''&&!isNaN(Number(c.rows)));
    if(m.indexOf('data')<0||!hasRows)return ((c&&c.chartCount)||0)+' 图表';
    var t='共 '+fmtNum(c.rows)+' 行';
    if(c.inserted!=null||c.updated!=null)t+=' · 新增 '+fmtNum(c.inserted||0)+' · 更新 '+fmtNum(c.updated||0);
    else if(c.appended!=null)t+=' · 新增 '+fmtNum(c.appended);
    return t;
  }
  // 在线编辑策略：每个报告独立（POST 到它自己所在端口，写它自己的注册表）。
  // 用 text/plain 避免 preflight（与 /api/admin/shutdown 同一套做法）。
  function editPolicy(url,payload,label){
    return fetch(url,{method:'POST',headers:{'Content-Type':'text/plain'},body:JSON.stringify(payload)})
      .then(function(resp){return resp.json().then(function(b){return {status:resp.status,body:b};});})
      .then(function(res){
        if(res.status!==200){toast((res.body&&(res.body.error&&res.body.error.message||res.body.error))||('操作失败('+res.status+')'));return;}
        toast(label+'已生效');
        load();
      }).catch(function(){toast('操作失败：无法连接该报告的本地服务');});
  }
  function toggleEdit(reportId,port,el){
    var want=!!el.checked;
    editPolicy('http://127.0.0.1:'+port+'/api/admin/edit-policy/'+encodeURIComponent(reportId),{enabled:want},
      want?'开启在线编辑':'关闭在线编辑');
  }
  // V3：口令不再走 window.prompt 明文输入——「设置口令」按钮就地展开行内
  // 小表单（password input，回车提交），挂在按钮所在 editbar 尾部。
  function openPwForm(reportId,port,anchor){
    var bar=anchor&&anchor.closest?anchor.closest('.editbar'):null;
    if(!bar)return;
    var old=bar.querySelector('.pw-form');
    if(old)old.remove();
    var form=document.createElement('span');
    form.className='pw-form';
    form.innerHTML='<input type="password" class="pw-input" maxlength="64" placeholder="新口令（1-64 字符）" aria-label="新编辑口令">'
      +'<button type="button" class="btn mini" data-pw-submit="1" data-report="'+esc(reportId)+'" data-port="'+esc(port)+'">确定</button>'
      +'<button type="button" class="btn mini" data-pw-cancel="1">取消</button>';
    bar.appendChild(form);
    var inp=form.querySelector('input');
    inp.focus();
    inp.addEventListener('keydown',function(ev){if(ev.key==='Enter'){ev.preventDefault();submitPwForm(form);}});
  }
  function submitPwForm(form){
    var inp=form.querySelector('input.pw-input');
    var btn=form.querySelector('[data-pw-submit]');
    if(!inp||!btn)return;
    var pw=inp.value.trim();
    if(!pw){toast('口令不能为空（1-64 字符）');return;}
    editPolicy('http://127.0.0.1:'+btn.getAttribute('data-port')+'/api/admin/edit-policy/'+encodeURIComponent(btn.getAttribute('data-report')),{password:pw},'设置口令');
  }
  // V3：口令行内恒遮蔽（●●●●，不泄露长度），明文只存 pwByReport；「显示」
  // 临时可见、再点回遮蔽；「复制」走 copyText（clipboard 失败回退 execCommand）。
  function pwHtml(r){
    pwByReport[r.reportId]=r.editPassword;
    return '<span class="pw">🔑 编辑口令 <code class="pw-val" data-open="0">●●●●</code>'
      +'<button type="button" class="btn mini" data-pw-show="1" data-report="'+esc(r.reportId)+'">显示</button>'
      +'<button type="button" class="btn mini" data-pw-copy="1" data-report="'+esc(r.reportId)+'">复制</button></span>';
  }
  function togglePwShow(btn){
    var code=btn.parentElement.querySelector('.pw-val');
    if(!code)return;
    if(code.getAttribute('data-open')==='1'){
      code.textContent='●●●●';code.setAttribute('data-open','0');btn.textContent='显示';
    }else{
      code.textContent=pwByReport[btn.getAttribute('data-report')]||'';
      code.setAttribute('data-open','1');btn.textContent='隐藏';
    }
  }
  function fmtTime(t){if(!t)return '';try{return new Date(t*1000).toLocaleString();}catch(e){return '';}}
  // ＋ 新建拼版草稿（docs/compose-user-path-design.md D4 第二层）：POST /api/drafts
  // 建零数据集空白草稿（服务端生成编辑口令，草稿内「数据源配置」上传数据），成功后
  // 新开画板页签。text/plain 与 shutdown/editPolicy 同一套免 preflight 做法。
  function createDraft(){
    var title=window.prompt('新拼版草稿的名称：','拼版草稿');
    if(title===null)return;
    title=title.trim()||'拼版草稿';
    fetch('/api/drafts',{method:'POST',headers:{'Content-Type':'text/plain'},body:JSON.stringify({title:title})})
      .then(function(r){return r.json().then(function(b){return {status:r.status,body:b};});})
      .then(function(res){
        if(res.status!==200){toast((res.body&&(res.body.error&&(res.body.error.message||res.body.error)))||('创建失败('+res.status+')'));return;}
        toast('草稿已创建：'+res.body.reportId);
        window.open(location.origin+'/reports/'+res.body.reportId,'_blank');
        load();
      }).catch(function(){toast('创建失败：无法连接本地服务');});
  }
  // 删除草稿（D5）：接既有 h_unregister（仅本机回环）；草稿与来源共享的数据集
  // 由服务端「无其他报告使用才 drop 表」保护，不会误删共享数据。
  function deleteDraft(reportId,port,title){
    if(!window.confirm('删除拼版草稿「'+(title||reportId)+'」？删除后不可恢复；不影响成品报告。'))return;
    fetch('http://127.0.0.1:'+port+'/api/admin/registry/unregister',{method:'POST',headers:{'Content-Type':'text/plain'},body:JSON.stringify({reportId:reportId})})
      .then(function(){toast('草稿已删除');setTimeout(load,400);})
      .catch(function(){toast('删除失败：无法连接该草稿的本地服务');});
  }
  // V18：单报告「从列表移除」（接既有 h_unregister，仅本机回环 + 跨端口放行；
  // 无其他报告使用的数据集表由服务端一并 drop）。草稿行不渲染此按钮——
  // 「删除草稿」已是该行的下线语义，不并存两个删除。POST 到该报告自己
  // 所在端口（写它自己的注册表），与 deleteDraft 同一套做法。
  function removeReport(reportId,port,title){
    if(!window.confirm('从列表移除报告「'+(title||reportId)+'」？该报告将下线（报告文件夹仍在，可双击「打开报告」重新启动）。'))return;
    fetch('http://127.0.0.1:'+port+'/api/admin/registry/unregister',{method:'POST',headers:{'Content-Type':'text/plain'},body:JSON.stringify({reportId:reportId})})
      .then(function(r){return r.json().then(function(b){return {status:r.status,body:b};});})
      .then(function(res){
        if(res.status!==200){toast((res.body&&(res.body.error&&(res.body.error.message||res.body.error)))||('移除失败('+res.status+')'));return;}
        toast('已移除「'+(title||reportId)+'」');
        load();
      })
      .catch(function(){toast('移除失败：无法连接该报告的本地服务');});
  }
  // V8：10s 轮询与手动「刷新」都走全量重绘——重绘前快照用户保持的行内状态
  // （key=报告 id）：修改日志 <details> 展开态、允许在线编辑勾选态；重绘后
  // 恢复，轮询不再把用户正在保持的状态静默复位。
  function snapshotUserState(){
    var snap={};
    list.querySelectorAll('details.log[data-log-for]').forEach(function(d){if(d.open)snap['log:'+d.getAttribute('data-log-for')]=true;});
    list.querySelectorAll('input[data-edit-toggle]').forEach(function(c){snap['edit:'+c.getAttribute('data-report')]=!!c.checked;});
    return snap;
  }
  function restoreUserState(snap){
    list.querySelectorAll('details.log[data-log-for]').forEach(function(d){if(snap['log:'+d.getAttribute('data-log-for')])d.open=true;});
    list.querySelectorAll('input[data-edit-toggle]').forEach(function(c){var v=snap['edit:'+c.getAttribute('data-report')];if(v!==undefined)c.checked=v;});
  }
  function render(data){
    var rows=(data&&data.reports)||[];
    document.getElementById('updated-at').textContent='更新于 '+new Date().toLocaleTimeString();
    pwByReport={};rowsByPort={};
    if(!rows.length){
      list.innerHTML='<div class="empty">当前没有运行中的报告。<br>重新打开：双击报告文件夹里的 <b>打开报告.command</b>（macOS）或 <b>打开报告.cmd</b>（Windows）。<br>也可以点右上角「＋ 新建拼版草稿」，从空白画板拼一份新报告。</div>';
      return;
    }
    // V4：按端口归集全部报告（跨报告族）——关闭服务 confirm 的点名清单与
    // 「同端口只渲染一个关闭入口」都以此为据。
    rows.forEach(function(r){
      var p=String(r.port);
      (rowsByPort[p]=rowsByPort[p]||[]).push(r);
    });
    // 按报告族（family）分组：一个报告下可有多个版本（初版 + 副本）。
    var byFamily={};var order=[];
    rows.forEach(function(r){
      var f=r.family||r.reportId; if(!byFamily[f]){byFamily[f]=[];order.push(f);} byFamily[f].push(r);
    });
    var seenPorts={};
    var html='';
    order.forEach(function(f){
      var vs=byFamily[f];
      vs.sort(function(a,b){
        if((a.reportId===f)!==(b.reportId===f))return (a.reportId===f)?-1:1; // 初版在最上
        return (b.updatedAt||0)-(a.updatedAt||0);
      });
      var base=vs[0];var port=base.port;
      var firstOnPort=!seenPorts[String(port)];
      seenPorts[String(port)]=true;
      var headMeta=vs.length+' 个版本 · 端口 '+esc(port)
        +(base&&base.startedAt?' · 启动于 '+esc(base.startedAt):'')
        +(isLocal&&base&&base.pid?' · PID '+esc(base.pid):'');
      html+='<div class="server family"><div class="server-head family-head">'
          +'<span class="family-title">📊 '+esc(base.title||f)+'</span>'
          +'<span class="meta">'+headMeta+'</span>'
          +'<span class="grow"></span>'
          +(isLocal?(firstOnPort
            ?'<button type="button" class="btn danger" data-shutdown="'+esc(port)+'">⏻ 关闭服务</button>'
            :'<span class="shared-badge" title="全机只有一个报告服务：本族与本页其他「端口 '+esc(port)+'」的族共享同一服务；关闭入口在本端口的第一个族头">共享服务 · 端口 '+esc(port)+'</span>')
          :'')
          +'</div>';
      vs.forEach(function(r){
        var isBase=r.reportId===f;
        var links='本机 <code>'+esc(r.urlLocal)+'</code>'
          +(data.lanIp?'<span class="lan">局域网 <code>'+esc(r.urlLan)+'</code></span>':'');
        var log=(r.changelog||[]).slice().reverse();
        var logHtml='';
        if(log.length){
          logHtml+='<details class="log" data-log-for="'+esc(r.reportId)+'"><summary>修改日志（'+log.length+' 次）</summary>';
          log.forEach(function(c){
            logHtml+='<div class="log-row"><span class="log-mode">'+esc(modeLabel(c.mode))+'</span>'
              +'<span class="log-time">'+esc(fmtTime(c.at))+'</span>'
              +'<span class="log-meta">'+logMeta(c)+(c.from?' · 来自 '+esc(c.from):'')+'</span></div>';
          });
          logHtml+='</details>';
        }
        // 草稿行：徽标区分（D5）+ 仅草稿有「删除草稿」（成品下线走「关闭服务」，
        // 避免误删交付物）。删除是本机操作（h_unregister 仅回环）。
        var typeBadge=r.draft
          ?'<span class="badge badge-draft">拼版草稿</span>'
          :'<span class="badge">'+(isBase?'初版':'副本')+'</span>';
        html+='<div class="rep"><div class="info">'
            +'<a class="title" href="'+esc(r.urlLocal)+'" target="_blank">'+esc(r.title||r.reportId)+'</a>'
            +'<div class="row2">'+typeBadge+'<span>ID <code>'+esc(r.reportId)+'</code></span>'
            +(r.updatedAt?'<span>更新 '+esc(fmtTime(r.updatedAt))+'</span>':'')
            // XP-01 磁盘列：该报告全部数据集快照文件字节和（服务端 diskBytes）。
            +((typeof r.diskBytes==='number')?'<span title="该报告全部数据集快照文件的磁盘占用（含 replaced/added 各代）">磁盘 '+(r.diskBytes/1048576).toFixed(2)+' MB</span>':'')+'</div>'
            +'<div class="row2">'+links+'</div>'
            +'<div class="row2"><span>'+(r.draft?'拼版画板 · 未转正':((r.chartCount||0)+' 个图表'))+'</span></div>'
            +'<div class="editbar">'
            +'<span class="state '+(r.editEnabled!==false?'on':'off')+'">'+(r.editEnabled!==false?'在线编辑已开启':'在线编辑已关闭')+'</span>'
            +(isLocal?(
              '<label class="switch" title="关闭后，任何保存/覆盖请求都会被拒绝，打开报告的人会看到「请联系管理员」">'
              +'<input type="checkbox" data-edit-toggle="1" data-report="'+esc(r.reportId)+'" data-port="'+esc(port)+'"'
              +((r.editEnabled!==false)?' checked':'')+'> 允许在线编辑</label>'
              +'<button type="button" class="btn mini" data-edit-pw="1" data-report="'+esc(r.reportId)+'" data-port="'+esc(port)+'">设置口令</button>'
            ):'')
            +(r.editPassword?pwHtml(r):'')
            +'</div>'
            +logHtml
            +'</div>'
            +'<div class="actions">'
            +'<button type="button" class="btn" data-copy="'+esc(r.urlLan||r.urlLocal)+'">复制局域网链接</button>'
            +'<button type="button" class="btn primary" data-open="'+esc(r.urlLocal)+'">打开</button>'
            +(r.draft&&isLocal?'<button type="button" class="btn danger" data-del-draft="1" data-report="'+esc(r.reportId)+'" data-port="'+esc(port)+'" data-title="'+esc(r.title||r.reportId)+'">删除草稿</button>':'')
            +(!r.draft&&isLocal?'<button type="button" class="btn" data-remove="1" data-report="'+esc(r.reportId)+'" data-port="'+esc(port)+'" data-title="'+esc(r.title||r.reportId)+'">移除</button>':'')
            +'</div></div>';
      });
      html+='</div>';
    });
    // V8：全量重绘前快照用户状态，重绘后立即恢复（手动刷新同走 render）。
    var snap=snapshotUserState();
    list.innerHTML=html;
    restoreUserState(snap);
    // XP-01 页脚总量：全部报告磁盘占用合计 + 本机状态目录体量（data 快照 + db 表缓存）。
    var foot=document.getElementById('disk-total');
    if(foot){
      var totalDisk=0;rows.forEach(function(r){if(typeof r.diskBytes==='number')totalDisk+=r.diskBytes;});
      var sh=(data&&data.stateHome)||null;
      var parts=['全部报告快照合计 '+fmtMb(totalDisk)];
      if(sh&&typeof sh.dataBytes==='number')parts.push('data '+fmtMb(sh.dataBytes));
      if(sh&&typeof sh.dbBytes==='number')parts.push('db 表缓存 '+fmtMb(sh.dbBytes));
      foot.textContent='· 磁盘占用：'+parts.join(' · ')+'（治理：python scripts/run_server.py --prune 先看演练，确认后加 --prune-apply）';
      foot.style.display='block';
    }
  }
  function fmtMb(n){n=Number(n)||0;return (n/1048576).toFixed(2)+' MB';}
  function load(){
    fetch('/api/reports/all',{headers:{'Accept':'application/json'}}).then(function(r){return r.json();}).then(render).catch(function(){
      list.innerHTML='<div class="empty">无法获取报告列表（/api/reports/all 请求失败）。</div>';
    });
  }
  list.addEventListener('click',function(e){
    var el=e.target&&e.target.closest?e.target.closest('button'):null;
    if(el){
      if(el.hasAttribute('data-shutdown'))shutdown(el.getAttribute('data-shutdown'));
      else if(el.hasAttribute('data-copy'))copyText(el.getAttribute('data-copy'));
      else if(el.hasAttribute('data-open'))window.open(el.getAttribute('data-open'),'_blank');
      else if(el.hasAttribute('data-edit-pw'))openPwForm(el.getAttribute('data-report'),el.getAttribute('data-port'),el);
      else if(el.hasAttribute('data-pw-show'))togglePwShow(el);
      else if(el.hasAttribute('data-pw-copy'))copyText(pwByReport[el.getAttribute('data-report')]||'','口令');
      else if(el.hasAttribute('data-pw-submit')){var pf=el.closest('.pw-form');if(pf)submitPwForm(pf);}
      else if(el.hasAttribute('data-pw-cancel')){var pf2=el.closest('.pw-form');if(pf2)pf2.remove();}
      else if(el.hasAttribute('data-del-draft'))deleteDraft(el.getAttribute('data-report'),el.getAttribute('data-port'),el.getAttribute('data-title'));
      else if(el.hasAttribute('data-remove'))removeReport(el.getAttribute('data-report'),el.getAttribute('data-port'),el.getAttribute('data-title'));
      return;
    }
    var tg=e.target;
    if(tg&&tg.getAttribute&&tg.getAttribute('data-edit-toggle'))toggleEdit(tg.getAttribute('data-report'),tg.getAttribute('data-port'),tg);
  });
  document.getElementById('new-draft').addEventListener('click',createDraft);
  document.getElementById('refresh').addEventListener('click',load);
  load();
  setInterval(function(){if(!document.hidden)load();},10000);
})();
</script>
</body></html>"""


def _escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _render_body(manifest: dict[str, Any]) -> str:
    """Render the deterministic grid body via the SHARED renderer."""
    from scripts.spec_to_report import (render_grid_html, render_tabs_html, layout_sections,
                                        _cover_h, _flatten_filters, _filter_h, _KPI_H,
                                        _text_pseudo_items, _merge_text_pseudos)
    cover = manifest.get("cover") or {}
    # free-v1 档（docs/free-authoring-discussion.md §2）：manifest 烘了 templateId，
    # _cover_h 据此对「无声明封面」返回 0——server 重渲染与编译期首渲同几何。
    cover_h = _cover_h({"templateId": manifest.get("templateId"), "cover": cover,
                        "kpis": manifest.get("kpis") or [], "charts": manifest.get("charts") or []})
    kpi_rows = max(1, (len(manifest.get("kpis") or []) + 3) // 4) if manifest.get("kpis") else 0
    filter_h = _filter_h(max(len(_flatten_filters(manifest.get("filters") or [])), len(manifest.get("filterCandidates") or [])))
    # Header order (cover → filter → KPI → charts); KPI cards are compact.
    # kpiPosition="bottom"（free）：打包起点不预留 KPI 行，条由渲染器从 placed
    # 底部现算（render_grid_html / render_tabs_html 同读 manifest["kpiPosition"]）。
    _kpi_at_bottom = manifest.get("kpiPosition") == "bottom"
    chart_start_y = cover_h + filter_h + (0 if _kpi_at_bottom else kpi_rows * _KPI_H)
    # free texts 伪卡与编译期同构（manifest["texts"] 是 build_manifest 烘的
    # 公共形状；伪卡只进排版管道，绝不进 manifest.charts）。
    _pack_list = _merge_text_pseudos(manifest.get("charts") or [],
                                     _text_pseudo_items(manifest.get("texts")))
    if (manifest.get("tabs") or {}).get("enabled") and manifest.get("sections"):
        # Workbench tabs: overview + one view per section + implicit 明细数据.
        pseudo = {"charts": manifest.get("charts") or [], "sections": manifest.get("sections") or [],
                  "texts": manifest.get("texts") or []}
        return render_tabs_html(pseudo, manifest, cover_h)
    # Sections (if any) interleave narrative/insight bands with their charts;
    # without sections this equals pack_layout (backward compatible).
    placed, bands = layout_sections(_pack_list, manifest.get("sections") or [], start_y=chart_start_y)
    return render_grid_html(manifest, placed, cover_h, bands)
