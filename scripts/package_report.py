#!/usr/bin/env python3
"""Inject the local editor runtime into an Agent-authored HTML report."""

from __future__ import annotations

# Running from a deployed skill folder must not litter it with __pycache__/:
# the env var is inherited by spawned children (lint/package/spec_to_report),
# the flag covers imports made after this line in this process.
import os as _os, sys as _sys
_os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
_sys.dont_write_bytecode = True
import argparse
import html
import json
import socket
import sys
from pathlib import Path

from table_input import load_table_file
from validate_report import MANIFEST_RE, ReportContractError, validate_html

SKILL_DIR = Path(__file__).resolve().parent.parent
RUNTIME_DIR = SKILL_DIR / "assets" / "runtime"
VENDOR_DIR = SKILL_DIR / "assets" / "vendor"


def _asset(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _lan_ip() -> str:
    """Best-effort LAN address of this machine (for the shareable URL)."""
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


def _with_dataset(source: str, table: dict, dataset_id: str, label: str) -> str:
    match = MANIFEST_RE.search(source)
    if not match:
        raise ReportContractError("Draft HTML is missing #ai-report-manifest")
    manifest = json.loads(match.group(1))
    # Carry semantic types forward: the draft may have declared dataset-level
    # measureTypes (inferred or authored); the packager rebuilds the dataset
    # entry from the canonical table, so preserve them for BI inheritance.
    existing = next((d for d in manifest.get("datasets", []) if d.get("id") == dataset_id), None)
    # privacy.sensitiveColumns（NS-01 主修，2026-10-24）：编译产物里的数据集已经
    # 物理剥离敏感列，而本函数从 canonical 原表重建 rows/columns——不投影就会把
    # 敏感值重新内联进终稿（E0 泄漏复活）。按草稿 manifest 的声明把敏感列投影掉；
    # 声明本体经下方 keep-list 随 dataset 前进（server Agent 链继续消费）。
    if existing and isinstance(existing, dict):
        declared = existing.get("privacy") or {}
        sensitive = [c for c in (declared.get("sensitiveColumns") or [])
                     if isinstance(c, str)] if isinstance(declared, dict) else []
        if sensitive:
            cols = table.get("columns") or []

            def _name(c):
                return c if isinstance(c, str) else (c or {}).get("name")

            keep_idx = [i for i, c in enumerate(cols) if _name(c) not in sensitive]
            if len(keep_idx) < len(cols):
                rows = [row for row in (table.get("rows") or [])
                        if isinstance(row, list) and len(row) >= len(cols)]
                table = {"columns": [cols[i] for i in keep_idx],
                         "rows": [[row[i] for i in keep_idx] for row in rows]}
    dataset = {"id": dataset_id, "label": label, "columns": table["columns"], "rows": table["rows"]}
    if existing and isinstance(existing, dict):
        # columnAliases = 字段显示名（editor-only 状态）；fieldNotes = 字段说明
        # （「这列是什么 / 怎么算的」，同样只在编辑器里编辑/展示）；derived = 计算列 /
        # 派生指标的**声明**（列数据已物化，但声明丢了「数据源配置」里就看不到定义、
        # 没法再编辑）；hierarchyOrder = 层级树里同父兄弟的展示顺序。这些都是运行时
        # 状态，丢了不报错、只是静默退化——所以必须显式列出。columnStats = 每列
        # 非空/缺失/不同值/范围（数据源配置的字段副行 + 数据质量），同样只能在编译期
        # 从 profile 拿到。primaryKey = 增量合并（upsert）主键声明（2026-09-15 批）——
        # 丢了「替换数据→增量合并」向导就失去预填主键，用户得重选 7 列复合键。
        for keep in ("measureTypes", "fieldRoles", "hierarchy", "columnTypes", "columnRoles",
                     "columnAliases", "fieldNotes", "derived", "hierarchyOrder", "columnStats",
                     "hiddenCols", "valueAliases", "dimGroups", "primaryKey", "privacy"):
            if isinstance(existing.get(keep), (dict, list)) and keep not in dataset:
                dataset[keep] = existing[keep]
        # v3 table role (primary/lookup) + the single default primary: the
        # runtime sidebar renders the role dot / 默认 badge from these, and the
        # chart/KPI default attribution (编译层 primary_id) depends on isDefault.
        if isinstance(existing.get("role"), str) and "role" not in dataset:
            dataset["role"] = existing["role"]
        if "isDefault" in existing and "isDefault" not in dataset:
            dataset["isDefault"] = bool(existing["isDefault"])
    manifest["datasets"] = [item for item in manifest.get("datasets", []) if item.get("id") != dataset_id] + [dataset]
    payload = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return source[:match.start(1)] + payload + source[match.end(1):]


def package_html(source: str, table: dict | None = None, dataset_id: str = "primary",
                 dataset_label: str = "原始数据", datasets: list | None = None) -> str:
    """Package a compiled report with the local editor runtime.

    Single-table legacy: merge one ``table`` under ``dataset_id``/``dataset_label``.
    P1 multi-table: ``datasets`` is an ordered list of {id, label, file}; every
    canonical table is merged into manifest.datasets under its own id/label."""
    if table is not None:
        source = _with_dataset(source, table, dataset_id, dataset_label)
    for d in datasets or []:
        if d.get("remote"):
            continue  # mixed mode: schema-only entry is already in the manifest
        src = d.get("file")
        if not src:
            continue
        path = Path(src)
        t = load_table_file(path) if path.suffix.lower() != ".json" else json.loads(path.read_text(encoding="utf-8"))
        source = _with_dataset(source, t, d["id"], d.get("label", d["id"]))
    validate_html(source, packaged=False)
    if "</body>" not in source.lower():
        raise ReportContractError("Draft HTML must contain a closing body tag")
    styles = _asset(RUNTIME_DIR / "editor-runtime.css")
    echarts = _asset(VENDOR_DIR / "echarts.min.js")
    gridstack = _asset(VENDOR_DIR / "gridstack.all.js")
    gridstack_css = _asset(VENDOR_DIR / "gridstack.min.css")
    adapter = _asset(RUNTIME_DIR / "chart-adapter.js")
    matcher = _asset(RUNTIME_DIR / "column-matcher.js")
    runtime = _asset(RUNTIME_DIR / "editor-runtime.js")
    xlsx_reader = _asset(RUNTIME_DIR / "xlsx-reader.js")
    presentation = _asset(RUNTIME_DIR / "presentation.js")
    pptx_exporter = _asset(RUNTIME_DIR / "pptx-exporter.js")
    injection = (
        f"<style id=\"ai-report-gridstack-css\">{gridstack_css}</style>"
        f"<style id=\"ai-report-runtime-css\">{styles}</style>"
        f"<script id=\"ai-report-echarts\">{echarts}</script>"
        f"<script id=\"ai-report-gridstack\">{gridstack}</script>"
        f"<script id=\"ai-report-chart-adapter\">{adapter}</script>"
        f"<script id=\"ai-report-column-matcher\">{matcher}</script>"
        f"<script id=\"ai-report-xlsx-reader\">{xlsx_reader}</script>"
        f"<script id=\"ai-report-presentation\">{presentation}</script>"
        f"<script id=\"ai-report-pptx-exporter\">{pptx_exporter}</script>"
        f"<script id=\"ai-report-runtime\">{runtime}</script>"
    )
    marker = source.lower().rfind("</body>")
    packaged = source[:marker] + injection + source[marker:]
    validate_html(packaged, packaged=True)
    return packaged


def build_server_marker(result: dict, server_url: str, title: str = "",
                        edit_password: str = "", mode_label: str = "server 模式") -> str:
    """The server-mode ``report.html`` landing page.

    The report itself lives on the local server, so this file's ONLY job is to
    hand a non-technical user everything they cannot easily find: the URLs
    (local + LAN), the edit password, the manager page, and how to reopen or
    stop the service. The audit JSON stays in a collapsed <details> block.
    ``mode_label`` 只改标题措辞（mixed = 混合模式：明细数据在本机服务端）。"""
    from urllib.parse import urlparse
    parsed = urlparse(server_url)
    port = parsed.port or 8765
    lan = _lan_ip()
    host = f"127.0.0.1:{port}"
    lan_host = f"{lan}:{port}"
    # 精确地址风格（统一 manager 修订，2026-09-20）：落地页的**主链接/CTA 一律指
    # 向本报告的完整 URL**。统一 manager 前每份报告独占一个 server，根路径重定向
    # 到「主报告」就是本报告；统一后全机共用一份注册表，根路径打开的是注册表里
    # 第一份报告——多报告机器上按根路径链接会打开**别人的报告**。
    rid = result.get('reportId', '')
    url_local = f"http://{host}/reports/{rid}"
    url_lan = f"http://{lan_host}/reports/{rid}"
    url_manager = f"http://{host}/manager"

    def esc(value: str) -> str:
        return html.escape(str(value or ""), quote=True)

    pw_html = ""
    if edit_password:
        pw_html = (f'<div class="row"><span class="k">🔑 在线编辑口令</span>'
                   f'<span class="v"><code class="pw">{esc(edit_password)}</code>'
                   f'<span class="dim">「保存与分享 → 保存为副本 / 保存并覆盖」时输入；口令只在此落地页、构建控制台与 README 记录，报告页不显示</span></span></div>')
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title or '报告')} · 构建完成</title>
<style>
  body{{font-family:Inter,-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;background:#f6f5f4;color:#1a1a1a;margin:0;padding:40px 20px}}
  .wrap{{max-width:760px;margin:0 auto;background:#fff;border:1px solid #e5e3df;border-radius:12px;padding:32px 36px}}
  h1{{margin:0 0 4px;font-size:20px}} .sub{{color:#787671;font-size:13px;margin:0 0 24px}}
  .row{{display:flex;gap:16px;padding:12px 0;border-bottom:1px solid #f0eeea;align-items:baseline}}
  .row:last-of-type{{border-bottom:0}}
  .k{{flex:0 0 130px;font-size:12px;color:#787671}}
  .v{{flex:1;font-size:14px;line-height:1.8;word-break:break-all}}
  .v .dim{{display:block;font-size:12px;color:#787671}}
  code{{background:#ececea;border-radius:4px;padding:2px 8px;font-family:ui-monospace,SFMono-Regular,monospace;font-size:12px}}
  code.pw{{font-size:15px;font-weight:700;background:#e6e0f5;color:#372a8f}}
  a{{color:#5645d4;text-decoration:none;font-weight:600}} a:hover{{text-decoration:underline}}
  .cta{{margin:22px 0 4px;display:flex;gap:10px;flex-wrap:wrap}}
  .cta a{{display:inline-block;background:#5645d4;color:#fff;border-radius:8px;padding:10px 22px;font-size:14px}}
  .cta a.ghost{{background:#fff;color:#1a1a1a;border:1px solid #c8c4be;font-weight:600}}
  .note{{margin-top:20px;font-size:12px;color:#787671;line-height:1.9;border-top:1px solid #e5e3df;padding-top:16px}}
  details{{margin-top:16px;font-size:12px;color:#54555a}} summary{{cursor:pointer}}
  pre{{background:#fafaf9;border:1px solid #e5e3df;border-radius:8px;padding:12px;overflow:auto;font-size:11px}}
</style></head><body><div class="wrap">
<h1>✅ 报告已构建（{esc(mode_label)}）</h1>
<p class="sub">{esc(title or '（未命名报告）')} · {result.get('charts', 0)} 个图表 · {result.get('blocks', 0)} 个卡片</p>
<div class="row"><span class="k">📍 本机访问</span><span class="v"><a href="{esc(url_local)}">{esc(url_local)}</a><span class="dim">打开本报告；也可双击报告目录里的 打开报告.command / 打开报告.cmd</span></span></div>
<div class="row"><span class="k">🌐 局域网访问</span><span class="v"><a href="{esc(url_lan)}">{esc(url_lan)}</a><span class="dim">同一网络内的其他电脑/手机直接打开即可；需保持这台电脑开机且服务运行</span></span></div>
{pw_html}
<div class="row"><span class="k">🗂 报告管理</span><span class="v"><a href="{esc(url_manager)}">{esc(url_manager)}</a><span class="dim">集中查看这台电脑上所有运行中的报告，并可一键关闭服务</span></span></div>
<div class="row"><span class="k">🛑 关闭服务</span><span class="v"><span class="dim">在 报告管理 页一键关闭；或结束报告目录 build/ 里 server.pid 记录的进程。关闭后所有访问链接立即失效。</span></span></div>
<div class="cta"><a href="{esc(url_local)}">打开报告</a><a class="ghost" href="{esc(url_manager)}">报告管理</a></div>
<details><summary>构建审计信息（JSON）</summary><pre>{esc(json.dumps(result, ensure_ascii=False, indent=2))}</pre></details>
</div></body></html>"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Package an AI-authored report with the local editor runtime")
    parser.add_argument("--html", required=True)
    parser.add_argument("--out")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--bi-table", help="Embed a canonical JSON or .xlsx table for inserting new BI components")
    parser.add_argument("--datasets", help="JSON file with an ordered [{id,label,file}] multi-dataset list (P1)")
    parser.add_argument("--dataset-id", default="primary")
    parser.add_argument("--dataset-label", default="原始数据")
    parser.add_argument("--server-url", default="", help="URL shown by a server-mode marker")
    parser.add_argument("--mode", choices=("auto", "server", "singlefile"), default="auto",
                        help="Packaging mode: auto (default by rowCount), server, or singlefile")
    parser.add_argument("--marker-only", action="store_true",
                        help="Validate the draft and emit ONLY the landing/marker page "
                             "(mixed 模式交付口径：报告由本机服务端渲染，交付物是指引页，"
                             "不是半能离线打开的数据 HTML)")
    parser.add_argument("--threshold", type=int, default=50000,
                        help="rowCount threshold for auto mode")
    args = parser.parse_args()
    try:
        source = Path(args.html).read_text(encoding="utf-8")
        # Decide mode
        mode = args.mode
        if mode == "auto":
            table = load_table_file(args.bi_table) if args.bi_table else None
            row_count = len(table["rows"]) if table else 0
            mode = "server" if row_count > args.threshold else "singlefile"

        if mode == "server" or args.marker_only:
            # Validate the draft but emit a small landing HTML that carries
            # everything the author needs: URLs (local + LAN), the edit
            # password, the manager page, and reopen/stop hints (the report
            # itself is not self-contained once any dataset is served).
            # --marker-only（mixed）：draft 仍按其实际模式（单文件壳 + 远端标记）
            # 校验——server 档的 R-S2 禁 rows 会误报内联数据集。
            forced = "server" if mode == "server" else "auto"
            result = validate_html(source, packaged=False, forced_mode=forced)
            if not args.out:
                print(json.dumps(result, ensure_ascii=False))
                return 0
            # Pull title + edit password from the draft manifest so the
            # landing page never leaves the author guessing the token that
            # gates online save.
            m_title, m_pw = "", ""
            m = MANIFEST_RE.search(source)
            if m:
                try:
                    m_manifest = json.loads(m.group(1))
                    m_title = m_manifest.get("title") or ""
                    m_pw = ((m_manifest.get("runtime") or {}).get("editPassword")) or ""
                except json.JSONDecodeError:
                    pass
            label = "server 模式" if mode == "server" else "混合模式 · 明细数据在本机服务端"
            Path(args.out).write_text(
                build_server_marker(result, args.server_url or "", m_title, m_pw, label),
                encoding="utf-8")
            print(f"Server-mode report validated. Marker: {args.out}")
            print("(The report itself lives at http://<host>:<port>/reports/<report-id>)")
            return 0

        if args.validate_only:
            print(validate_html(source, packaged="ai-report-runtime" in source, forced_mode="singlefile"))
            return 0
        if not args.out:
            raise ReportContractError("--out is required when packaging")
        table = load_table_file(args.bi_table) if args.bi_table else None
        ds_list = None
        if args.datasets:
            ds_list = json.loads(Path(args.datasets).read_text(encoding="utf-8"))
            if not isinstance(ds_list, list):
                raise ReportContractError("--datasets must be a JSON array")
        Path(args.out).write_text(package_html(source, table, args.dataset_id, args.dataset_label, ds_list), encoding="utf-8")
        print(f"Packaged report: {args.out}")
        return 0
    except ReportContractError as error:
        print(f"Report contract violation: {error}", file=sys.stderr)
        return 2
    except Exception as error:
        print(f"Unable to package report: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
