# CLI reference (Windows-first)

所有脚本使用 UTF-8、参数数组和 `shell=False`；在 Windows cmd 中可用 `py` 或 `python`。没有 Python 时先运行：

```cmd
cmd /c scripts\\bootstrap_windows.cmd
```

若要提前制作真正的离线部署包，在可联网机器运行一次：

```cmd
cmd /c scripts\\prepare_offline_runtime.cmd
```

它把官方 Windows embeddable Python 放入 `assets\\python-runtime`；目标机器不需要联网或安装 Python。

macOS / Unix 不运行 `.cmd`，直接使用系统 `python3`（3.10+）和 `node`；例如：

```bash
python3 .pi/skills/live-report-master/scripts/report_build.py --table data.csv --spec report_request.json
python3 .pi/skills/live-report-master/scripts/run_server.py --port 8765
```

## Profile / canonical table

```cmd
py scripts\\profile_table.py --table input.xlsx --out build\\profile.json --canonical-out build\\table.json
```

输入支持 `.xlsx`、`.csv`、`.tsv` 和规范 JSON。错误单元格变成 `null`，质量计数写入 profile；结构错误退出码为 2。

## 推荐：一键 pipeline

```cmd
py scripts\\report_build.py --table input.xlsx --spec report_request.json
```

当 `--out` 省略时，脚本自动创建一个以**报告标题**命名、Windows 安全的文件夹，并把最终报告放在其下、构建产物放进该文件夹的 `build/` 子目录：

```
<报告标题>/
├── report.html
└── build/   (profile/table/draft/manifest/status 等)
```

可选参数：`--mode auto|singlefile|server`、`--out <file>`（显式指定报告路径）、`--out-dir <dir>`（自动命名文件夹的父目录，默认当前目录）、`--workdir <dir>`、`--threshold 100000`、`--max-bytes 52428800`、`--max-cardinality 2000`、`--server http://127.0.0.1:8765`、`--skip-audit`、`--audit-required`、`--report-id <id>`（覆盖 reportId；拼版草稿原址转正必需）。pipeline 顺序为 profile → request/table 校验 → 编译 → lint → 打包或注册 → packaged 校验 → 审计。

server 模式会在目标 localhost 尚未运行时自动启动一个 SQLite server（registry/data/pid 文件均位于 `--workdir`）；已有 server 则复用，不会重复启动。

退出码：0 表示 `ready` 或 `needs_audit`；2 表示硬失败（`failed` 或 `--audit-required` 下审计失败）；中间状态保存在 `build/build-status.json`。

## 输入预处理：缺省直通 vs `--sort` 整理模式（2026-09-18 用户裁决）

对上传的二维表，`report_build` **缺省直通**——数据值一个不改：

- **构建前体检**（input-lint）只出报告：`<workdir>/input-lint.json` + 控制台摘要，**不拦截**构建；
- **读取做最小确定性修复**并记 notes：重复表头改名「销售额.2」、空行跳过、参差行补齐/截断、
  合并表头空格自动命名「列N」。

显式带 **`--sort`** 才进入**整理模式**：体检门禁生效——结构破坏（fail）在构建前直接拦截，
读取转严格（重复表头/空行/参差行拒绝），保证进来的是致密规范矩形。**`--strict-input`**
（仅与 `--sort` 搭配）把 warn 级质量警告也按失败处理。完整坑清单（合并表头/千分位文本/
日期序列号/前导零编码等 10+ 例与两模式对照表）见 `docs/input-contract.md` §5。

## 拼版草稿：`--draft`（冷启动空白画板）

```cmd
py scripts\\report_build.py --draft --title 我的画板
```

不需要 `--spec`/`--table`：起（或复用 `./拼版草稿/` home 的）专属本地服务并建一张零数据集
空白草稿，打印画板 URL 与自动生成的编辑口令。数据在草稿内「数据源配置 → 添加数据表」上传；
拼完后由 Agent 原址转正：`--report-id <draftId> --server <草稿服务> --mode server`（URL 不变）。
详见 SKILL.md「创作入口」第 4 条与 `docs/compose-user-path-design.md`。

## 自由模式：`--free`

`--free` = `templateId:"free-v1"`（声明即全部）：封面/KPI 条/筛选带/洞察只渲染显式声明的，
编译不做自动填充；`--patch` 即完整请求（不再与 suggest 默认合并）。快速看数请勿加。
判断启发与守卫见 SKILL.md「何时用 --free」节。

## 单独编译 / 校验

```cmd
py scripts\\validate_request.py report_request.json
py scripts\\spec_to_report.py --spec report_request.json --dataset build\\table.json --out build\\draft.html --mode singlefile
py scripts\\validate_report.py --html build\\draft.html
py scripts\\lint_report.py --html build\\draft.html
py scripts\\package_report.py --html build\\draft.html --bi-table build\\table.json --out build\\report.html --mode singlefile
py scripts\\validate_report.py --html build\\report.html --packaged
```

`lint_report.py --strict` 会把建议升级为失败；普通 lint 只产生警告。不要手工重复运行旧版兼容参数。

## server 模式

```cmd
cmd /c scripts\\start_server.cmd --port 8765 --registry build\\registry.json --data-dir build\\data --pid-file build\\server.pid
py scripts\\serve_report.py --server http://127.0.0.1:8765 register --data input.xlsx --spec report_request.json --report-id sales
cmd /c scripts\\open_report.cmd http://127.0.0.1:8765/reports/sales
node scripts\\audit_editor.mjs http://127.0.0.1:8765/reports/sales
cmd /c scripts\\stop_server.cmd --pid-file build\\server.pid
```

server 注册拒绝 `singlefile`，并将源表复制到 registry 的 data directory。SQLite 是唯一默认后端；没有全局安装依赖。server 报告 HTML 不含原始 rows，必须保持 server 进程运行。

## 审计

```cmd
node scripts\\audit_editor.mjs build\\report.html
```

`AI_REPORT_CHROME` 可指定 Chrome/Chromium 路径。审计检查运行时加载、无 Apply 按钮、控件即时生效、图表/表格真实绘制、无函数字符串 formatter、热力图完整性、响应式溢出、性能和 BI 插入。没有 Node 或 Chrome 时 pipeline 标记 `needs_audit`，不伪造 `ready`。
