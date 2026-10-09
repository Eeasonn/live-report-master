#!/usr/bin/env python3
"""Profile a supported 2D table for report authoring."""

from __future__ import annotations

# Running from a deployed skill folder must not litter it with __pycache__/:
# the env var is inherited by spawned children (lint/package/spec_to_report),
# the flag covers imports made after this line in this process.
import os as _os, sys as _sys
_os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
_sys.dont_write_bytecode = True
import argparse
import hashlib
import json
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from table_input import load_table_file
from validate_table import TableValidationError


def _json_value(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _profile_value(value: Any, column_type: str) -> Any:
    """Keep profiling useful while avoiding exposure of identifier values."""
    if column_type == "id" and value not in (None, ""):
        digest = hashlib.sha256(str(value).encode("utf-8", errors="replace")).hexdigest()[:10]
        return f"<id:{digest}>"
    return _json_value(value)


MANIFEST_TOP_VALUES = 5


def column_stats(profile_columns: list[dict[str, Any]], quality: dict[str, Any] | None = None,
                 top: int = MANIFEST_TOP_VALUES) -> dict[str, dict[str, Any]]:
    """Compact per-column stats for the report manifest (数据源配置 副行 / 数据质量).

    The full profile keeps up to 20 top values and every measure/mean; the
    manifest only needs a short, stable summary that the editor can render
    WITHOUT loading rows (server mode has none). This is the ONE place that
    decides that shape — the runtime never recomputes stats, it only formats
    ``dataset.columnStats`` (server mode included).

    ``distinct``/``missing`` answer "can this be a dimension", ``invalid`` is
    the number of malformed cells validate_table turned into null (never zero),
    so a data-prep user can see why a total looks short.
    """
    quality = quality or {}
    invalid = quality.get("invalidValues") or {}
    samples = quality.get("invalidSamples") or {}
    text_fallback = quality.get("textFallback") or {}
    text_fallback_samples = quality.get("textFallbackSamples") or {}
    out: dict[str, dict[str, Any]] = {}
    for item in profile_columns:
        name = item.get("name")
        if not name:
            continue
        stat: dict[str, Any] = {
            "nonNull": int(item.get("nonNull") or 0),
            "missing": int(item.get("missing") or 0),
            "distinct": int(item.get("distinct") or 0),
        }
        for key in ("min", "max", "mean", "sum"):
            value = item.get(key)
            if value is not None and isinstance(value, (int, float)) and math.isfinite(float(value)):
                stat[key] = value
            elif value is not None and key in ("min", "max") and not isinstance(value, (int, float)):
                # Time columns carry the first/last value as a raw string.
                stat[key] = value
        tops = [{"value": t.get("value"), "count": int(t.get("count") or 0)}
                for t in (item.get("topValues") or [])[:top] if isinstance(t, dict)]
        if tops:
            stat["top"] = tops
        if invalid.get(name):
            stat["invalid"] = int(invalid[name])
            bad = [str(x) for x in (samples.get(name) or [])[:3]]
            if bad:
                stat["invalidSamples"] = bad
        if text_fallback.get(name):
            # 编码列翻转（validate_table 单一真源的翻转事实）：值没丢，只是整列
            # 改按文本保留 —— 徽标口径与「已按空」刻意不同（值在，但 sum 无意义）。
            stat["textFallback"] = int(text_fallback[name])
            tf_bad = [str(x) for x in (text_fallback_samples.get(name) or [])[:3]]
            if tf_bad:
                stat["textFallbackSamples"] = tf_bad
        out[name] = stat
    return out


def build_profile(table: dict[str, Any], source: str) -> dict[str, Any]:
    rows = table["rows"]
    columns = table["columns"]
    types = table["meta"]["types"]
    result: list[dict[str, Any]] = []
    for index, name in enumerate(columns):
        values = [row[index] for row in rows]
        non_null = [value for value in values if value is not None and value != ""]
        counts = Counter(str(value) for value in non_null)
        item: dict[str, Any] = {
            "name": name,
            "type": types[name],
            "nonNull": len(non_null),
            "missing": len(values) - len(non_null),
            "distinct": len(counts),
            "topValues": [{"value": _profile_value(value, types[name]), "count": count}
                          for value, count in counts.most_common(20)],
        }
        if types[name] == "measure" and non_null:
            numeric = sorted(float(value) for value in non_null)
            item.update({
                "min": _json_value(numeric[0]),
                "max": _json_value(numeric[-1]),
                "mean": _json_value(sum(numeric) / len(numeric)),
                "sum": _json_value(sum(numeric)),
            })
        elif types[name] == "time" and non_null:
            ordered = sorted(str(value) for value in non_null)
            item.update({"min": ordered[0], "max": ordered[-1]})
        result.append(item)
    duplicate_rows = len(rows) - len({json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows})
    has_measure = any(c.get("type") == "measure" for c in result)
    record_dataset = bool(result) and not has_measure
    hints: list[str] = []
    if record_dataset:
        hints.append("recordDataset：每行 = 一条记录，无独立指标列；图表指标用行数（count / 合成指标「记录数」）")
    # Keep a small deterministic sample for the Agent's semantic pass. IDs are
    # masked in this view; the canonical table remains untouched for execution.
    sample_rows = []
    for row in rows[:50]:
        sample_rows.append([_profile_value(v, types[col]) for v, col in zip(row, columns)])
    quality = (table.get("meta") or {}).get("quality", {}) or {}
    for _fb_col, _fb_n in (quality.get("textFallback") or {}).items():
        _fb_samples = "、".join(str(x) for x in (quality.get("textFallbackSamples") or {}).get(_fb_col, [])[:3])
        hints.append(
            f"列「{_fb_col}」有 {_fb_n} 个非数值编码值（示例 {_fb_samples}）——已整列按编码列保留原值，"
            "不要声明为数值指标：去重计数（countd）可用，求和/平均不可用；如确需数值聚合请清洗源数据。")
    stats = column_stats(result, quality)
    # Carry the compact stats on the canonical table too: the singlefile / mixed
    # compile paths hand the canonical JSON to spec_to_report, and that is where
    # the manifest's ``datasets[].columnStats`` comes from. The returned profile
    # carries the same map so ``report_build --column-stats`` (server mode, which
    # deliberately never writes a canonical JSON) can forward it verbatim.
    meta = table.get("meta")
    if isinstance(meta, dict):
        meta["stats"] = stats
    return {
        "source": source,
        "columnStats": stats,
        "rowCount": len(rows),
        "columnCount": len(columns),
        "duplicateRows": duplicate_rows,
        "columns": result,
        "sampleRows": sample_rows,
        "recordDataset": record_dataset,
        "hints": hints,
        "quality": quality,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Profile a pure 2D Excel or canonical JSON table")
    parser.add_argument("--table", required=True)
    parser.add_argument("--sheet", type=int, default=0)
    parser.add_argument("--out")
    parser.add_argument("--canonical-out")
    args = parser.parse_args()
    try:
        table = load_table_file(args.table, sheet=args.sheet)
        profile = build_profile(table, str(Path(args.table).resolve()))
        profile_text = json.dumps(profile, ensure_ascii=False, indent=2)
        if args.out:
            out_path = Path(args.out)
            # The model commonly points --out at build/profile.json before the
            # build dir exists; create it instead of failing the whole step.
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(profile_text + "\n", encoding="utf-8")
            # 机器模式（report_build 两个 spawn 点都继承 stdout）把数据事实打到
            # 构建控制台——编码列翻转/脏值置空必须让构建方当场看见，而不是只在
            # 报告的数据源配置里事后发现（真机教训：15,191 行洗空直到交付后才被问起）。
            for _h in profile.get("hints") or []:
                print("[data] " + _h)
        else:
            print(profile_text)
        if args.canonical_out:
            Path(args.canonical_out).write_text(
                json.dumps(table, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        return 0
    except TableValidationError as error:
        print(f"Table contract violation: {error}", file=sys.stderr)
        return 2
    except Exception as error:
        print(f"Unable to profile table: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
