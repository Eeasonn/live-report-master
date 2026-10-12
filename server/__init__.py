"""AI Report Server backend package.

The package implements a multi-report local HTTP server with SQLite
SQLite as the v1 data engine. See `app.py` for the HTTP entrypoint and
`store.py` for the data engine abstraction.
"""

__version__ = "1.0"

# Date-based skill version（YYYY.MM.DD，每个落地批次 bump——AGENTS.md 纪律）。
# 单一真源：构建横幅（report_build）/ /api/health / 管理页页脚三处同源显示，
# 远程支持时一眼识别「公司机跑的是哪个包」（曾因六个批次不 bump 无法区分新旧包）。
SKILL_VERSION = "2026.10.11"
