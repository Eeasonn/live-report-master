# -*- coding: utf-8 -*-
"""报告主题壳表（单一真源）——chartTheme 收敛为「整报告 house」。

设计契约 = docs/report-theme-convergence-design.md。图表侧 CHART_THEMES
（chart-adapter.js，源自 refs/flint/src/core/theme/presets/*.ts）原样保留；
本模块只 owning 壳切片（SHELL_CSS 令牌）+ 封面派生规则，两表以 id 对齐
（tests/test_report_themes.py 断言 id 集合一致）。

原料出处（转录，非发明）：
- 各 house 墨色三阶/accent/status = Flint preset `ink.text`/`ink.accent`/
  `ink.series.status` 实测值；
- huawei = 用户提供的实测 token（huawei.com corporate-information 提取，
  ~/Downloads/huawei-com-DESIGN.md）；分类色板官网没有 → 以品牌红为
  series[0] 推导（契约 §1-9 允许的唯一手调字段）。
"""

import re

DEFAULT_ID = "default"

_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# 深底（暗色/高饱和黄）主题只换图表、壳保持默认（契约 §1-4）。
CHART_ONLY_IDS = ("powerbi", "pop")

_TOKEN_KEYS = (
    "bg", "surface", "surfaceSoft", "theadBg", "ink", "ink2", "ink3", "line", "lineSoft",
    "accent", "editorialAccent", "coverFrom", "coverVia", "coverTo", "coverInk",
    "statusUp", "statusDown",
)

# ---------- 颜色代数（派生规则的唯一实现） ----------

def _rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _hex(rgb):
    return "#%02x%02x%02x" % rgb


def mix(a, b, t):
    """a 向 b 混合 t∈[0,1]。"""
    ra, rb = _rgb(a), _rgb(b)
    return _hex(tuple(round(ra[i] + (rb[i] - ra[i]) * t) for i in range(3)))


def shade(c, f):
    """向黑压暗 f∈[0,1]。"""
    return mix(c, "#000000", f)


def _contrast(a, b):
    def lum(h):
        def ch(v):
            v /= 255.0
            return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
        r, g, b = (_rgb(h)[i] / 255.0 for i in range(3))
        return 0.2126 * ch(r * 255) + 0.7152 * ch(g * 255) + 0.0722 * ch(b * 255)
    la, lb = lum(a), lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


# ---------- 原料表（explicit 值 + 派生槽位） ----------
# 字段：ink/ink2/ink3 墨三阶；accent；bg 页面底；paper 面色（表头/斑马/insight 底）；
# rule 结构线；status (up, down) 缺省回退现绿/红；font 图表字体（可选）；
# chart 非内置图表 preset 的图表切片（huawei 专用）；cite 出处。

_RAW = {
    # 现壳字面值——default 档「零覆盖变量」守卫的基准（契约 §5）。
    "default": {
        "label": "默认",
        "explicit": {
            "bg": "#f6f5f4", "surface": "#ffffff", "surfaceSoft": "#fafaf9", "theadBg": "#f6f5f4",
            "ink": "#1a1a1a", "ink2": "#5d5b54", "ink3": "#787671",
            "line": "#e5e3df", "lineSoft": "#ede9e4", "accent": "#5070dd", "editorialAccent": "#5645d4",
            "coverFrom": "#070f24", "coverVia": "#0a1530", "coverTo": "#1a2a52",
            "coverInk": "#ffffff", "statusUp": "#1a7f4b", "statusDown": "#c62f2f",
        },
    },
    "economist": {
        "label": "The Economist",
        "cite": "refs/flint/src/core/theme/presets/economist.ts ink",
        "ink": "#121317", "ink2": "#54585a", "ink3": "#8b9196",
        "accent": "#006ba2", "editorial": "#e3120b", "bg": "#ffffff", "paper": "#f7f7f5",
        "rule": "#c9d3da", "status": ("#006ba2", "#e3120b"),
    },
    "nyt": {
        "label": "New York Times",
        "cite": "refs/flint/src/core/theme/presets/nyt.ts ink",
        "ink": "#121212", "ink2": "#6b6b6b", "ink3": "#8a8a8a",
        "accent": "#2f6b9a", "editorial": "#c2352b", "bg": "#ffffff", "paper": "#f7f7f6",
        "rule": "#e4e4e4", "status": ("#2f6b9a", "#c2352b"),
    },
    "swiss": {
        "label": "Swiss",
        "cite": "refs/flint/src/core/theme/presets/swiss.ts ink（纸底 #f4f1ea 实测）",
        "ink": "#1a1a1a", "ink2": "#555555", "ink3": "#8a8a8a",
        "accent": "#e2231a", "editorial": "#e2231a", "bg": "#f4f1ea", "paper": "#fbf9f4",
        "rule": "#d9d5cc", "status": None,
    },
    "nature": {
        "label": "Nature",
        "cite": "refs/flint/src/core/theme/presets/nature.ts ink（preset 只给全黑，ink2/ink3 派生）",
        "ink": "#000000", "ink2": "#3d3d3d", "ink3": "#767676",
        "accent": "#0072b2", "editorial": "#000000", "bg": "#ffffff", "paper": "#f7f7f7",
        "rule": "#d9d9d9", "status": None,
    },
    "mckinsey": {
        "label": "McKinsey",
        "cite": "refs/flint/src/core/theme/presets/mckinsey.ts ink",
        "ink": "#051c2c", "ink2": "#5a6872", "ink3": "#8a969d",
        "accent": "#051c2c", "editorial": "#2251ff", "bg": "#ffffff", "paper": "#f6f8fa",
        "rule": "#d3dce1", "status": None,
    },
    "datawrapper": {
        "label": "Datawrapper",
        "cite": "refs/flint/src/core/theme/presets/datawrapper.ts ink",
        "ink": "#333333", "ink2": "#666666", "ink3": "#999999",
        "accent": "#18a1cd", "editorial": "#18a1cd", "bg": "#ffffff", "paper": "#f7f7f7",
        "rule": "#dcdcdc", "status": None,
    },
    # 深底/高饱和：chartOnly，壳 tokens 用 default 的（resolve 时展开）。
    "powerbi": {"label": "Power BI", "chart_only": True},
    "powerbi-light": {
        "label": "Power BI Light",
        "cite": "refs/flint/src/core/theme/presets/powerbi-light.ts ink",
        "ink": "#252423", "ink2": "#605e5c", "ink3": "#a19f9d",
        "accent": "#118dff", "editorial": "#118dff", "bg": "#ffffff", "paper": "#faf9f8",
        "rule": "#ededed", "status": None,
    },
    "pop": {"label": "Pop", "chart_only": True},
    "cartoon": {
        "label": "Cartoon",
        "cite": "refs/flint/src/core/theme/presets/cartoon.ts ink（accent 取 palette[0]）",
        "ink": "#2e2b28", "ink2": "#8a837a", "ink3": "#b3aa9c",
        "accent": "#3aa9ff", "editorial": "#3aa9ff", "bg": "#fffdf5", "paper": "#fffdf5",
        "rule": "#ece5d6", "status": None,
    },
    "huawei": {
        "label": "华为",
        "cite": "~/Downloads/huawei-com-DESIGN.md（huawei.com corporate-information 实测）",
        "ink": "#333333", "ink2": "#5a5858", "ink3": "#8a8888",
        "accent": "#c7000b", "editorial": "#c7000b", "bg": "#ffffff", "paper": "#f3f3f3",
        "rule": "#e0e0e0", "status": None,
        "font": "Arial, Helvetica, sans-serif",
        # 官网无分类色板：品牌红打头 + 降饱和和谐色（契约允许的唯一手调字段）。
        "chart": {
            "color": ["#c7000b", "#1f6e7a", "#d9a441", "#42586e", "#379a8b", "#8c9ba5"],
            "text": "#333333", "grid": "#e0e0e0", "axis": "#333333",
            "font": "Arial, Helvetica, sans-serif", "bg": None,
        },
    },
}

# A1（2026-09-15 拍板）：壳 accent = 图表首系列色（数据墨先行），Flint ink.accent 降级为
# editorialAccent 只喂封面渐变——消除「壳紫 vs 图表蓝绿黄」的双 accent 割裂。
# raw 字段：accent=壳 accent（=chart-adapter 色板[0]，守卫逐主题对齐）；editorial=刊物 accent。
_FALLBACK_STATUS = ("#1a7f4b", "#c62f2f")

# tokens → CSS 变量（唯一映射表）：编译期内联防 FOUC 与运行时 applyShellTheme
# 都消费 resolve_theme 输出里的 `vars`（已展开成 --var 名），JS 不留第二份映射。
_TOKEN_TO_VAR = {
    "bg": "--bg", "surface": "--surface", "surfaceSoft": "--surface-soft", "theadBg": "--thead-bg",
    "ink": "--ink", "ink2": "--ink2", "ink3": "--ink3",
    "line": "--line", "lineSoft": "--line-soft", "accent": "--accent",
    "coverFrom": "--cover-from", "coverVia": "--cover-via",
    "coverTo": "--cover-to", "coverInk": "--cover-ink",
    "statusUp": "--semantic-ok", "statusDown": "--semantic-error-deep",
}


def css_vars(tokens):
    """tokens dict → {--var: hex}；供 manifest.themeShells[id].vars 直接执行。"""
    out = {}
    for k in _TOKEN_KEYS:
        var = _TOKEN_TO_VAR.get(k)
        v = tokens.get(k)
        if var and v:
            out[var] = v
    return out


def _derive(ink, accent, rule, paper, bg):
    """封面/线规则（契约 §3 全局派生，唯一创作环节）。

    surface（卡面）：现役 10 house 全部是浅色纸面 → 卡面恒白（与 SHELL_CSS
    :root 既有字面值 #fff 一致，历史产物观感零漂移）；未来深壳 house 在 _RAW
    显式声明 surface 即可整体生效（editor chrome 的 color-mix 混合基已指向
    var(--surface)，W3 令牌轴就位后深壳不再向白混合）。"""
    return {
        "coverFrom": shade(ink, 0.75),
        "coverVia": shade(mix(ink, accent, 0.18), 0.45),
        "coverTo": shade(accent, 0.25),
        "coverInk": "#ffffff",
        "line": rule,
        "lineSoft": mix(rule, bg, 0.35),
        "surface": "#ffffff",
        "surfaceSoft": paper,
        "theadBg": paper,
    }


def _check_hex(key, v):
    if not _HEX_RE.match(v or ""):
        raise ValueError("theme token %s=%r is not a #rrggbb hex" % (key, v))


def resolve_theme(theme_id, accent_override=None):
    """解析一个主题为完整 tokens；同一输入恒等输出（守卫断言确定性）。"""
    raw = _RAW.get(theme_id)
    if raw is None:
        raise ValueError("unknown report theme %r" % (theme_id,))
    if raw.get("chart_only"):
        # W3 漏网路径收口：chartOnly 壳镜像 default——显式 accent 覆盖同样要透传
        # （此前内层 resolve 丢掉 override，chartOnly 报告里 style.accent 完全失效；
        # 运行时 accentSeries0Of 依赖 tokens.accent ≠ 出厂首色来识别覆盖）。
        base = resolve_theme(DEFAULT_ID, accent_override)
        return {"label": raw["label"], "chartOnly": True,
                "tokens": dict(base["tokens"]), "vars": dict(base["vars"])}
    if theme_id == DEFAULT_ID:
        tokens = dict(raw["explicit"])
    else:
        ink, accent, editorial = raw["ink"], raw["accent"], raw.get("editorial") or raw["accent"]
        if accent_override:
            accent = editorial = accent_override  # 决议 2：显式 style.accent 赢（壳+刊物同换）
        status = raw.get("status") or _FALLBACK_STATUS  # 决议 3：缺省回退现绿/红
        tokens = {
            "bg": raw["bg"], "ink": ink, "ink2": raw["ink2"], "ink3": raw["ink3"],
            "accent": accent, "editorialAccent": editorial,
            "statusUp": status[0], "statusDown": status[1],
        }
        tokens.update(_derive(ink, editorial, raw["rule"], raw["paper"], raw["bg"]))
    for k in _TOKEN_KEYS:
        _check_hex(k, tokens.get(k))
    # 决议 2 对 default 同样成立：显式 style.accent 赢（否则切走再切回会跳回
    # 表内缺省紫，与 SHELL_CSS 烘焙的 --accent 漂移）。
    if accent_override and theme_id == DEFAULT_ID:
        tokens["accent"] = accent_override
        tokens["editorialAccent"] = accent_override
    return {
        "label": raw["label"],
        **({"chartOnly": True} if raw.get("chart_only") else {}),
        "tokens": tokens,
        "vars": css_vars(tokens),
        **({"chart": dict(raw["chart"])} if raw.get("chart") else {}),
    }


def theme_ids():
    return tuple(_RAW.keys())


def shell_catalog(accent_override=None):
    """整表（manifest.themeShells）——运行时切换需要全部主题，不止当前档。"""
    return {tid: resolve_theme(tid, accent_override) for tid in _RAW}


def assert_readable(theme_id, accent_override=None):
    """对比度 guard：白墨对封面深端 ≥3:1；主墨对页面底 ≥4.5:1。"""
    t = resolve_theme(theme_id, accent_override)["tokens"]
    c_cover = _contrast(t["coverInk"], t["coverTo"])
    c_ink = _contrast(t["ink"], t["bg"])
    if c_cover < 3.0:
        raise ValueError("theme %s cover contrast %.2f < 3.0 (ink on coverTo)" % (theme_id, c_cover))
    if c_ink < 4.5:
        raise ValueError("theme %s ink/bg contrast %.2f < 4.5" % (theme_id, c_ink))
    return {"cover": round(c_cover, 2), "inkBg": round(c_ink, 2)}


if __name__ == "__main__":
    for tid in theme_ids():
        t = resolve_theme(tid)["tokens"]
        c = assert_readable(tid)
        print("%-14s %-16s accent %s  cover %s..%s  contrast %s" % (
            tid, resolve_theme(tid)["label"], t["accent"], t["coverFrom"], t["coverTo"], c))
