"""增量合并（按主键 upsert）—— 唯一合并算术，两处消费。

本模块职责 = **合并/upsert 语义**：「更新数据」的增量模式把增量文件按主键
并进现有表——**有则覆盖、无则插入**，天然幂等（同一增量文件导两遍结果一致，
往期追溯调整如退货/迟入账改上期值被自然覆盖）。「追加新行」（``mode:'append'``，
2026-09-22 同构文件补新一期数据、免选主键）不在这里：它是调用方
``server/app.py`` ``_process_replace_table`` 第三分支的直接拼装（基表行 +
上传行并表，无主键匹配语义），本模块不参与、也没有第二份追加算术。

单一真源契约（对齐 date_parse / derived_eval 先例）：

- **本模块是 PY 真源**：server 端点（``server/app.py`` 的 replace 分支
  ``mode:'upsert'``）在装载前调用 ``upsert_merge``；
- **JS 镜像** ``editor-runtime.js`` ``#region UPSERT_MERGE``（singlefile 内存合并）
  与本文件逐语义一致，parity 由 ``tests/fixtures/upsert_merge.json`` 双向锁定
  （PY ``tests/test_upsert_merge.py`` 与 JS ``tests/test_upsert_merge.cjs`` 读同一份）；
- **守卫 fail-closed**：主键不唯一 / 增量重复 / 主键空值 / 列集不一致 一律抛
  ``UpsertError``（可行动文案），绝不静默合并——错误数据一旦进表，聚合与导出
  全部跟着错。

合并语义（确定性）：

- 基表行序保持不变（被更新的行**原位替换**，不移动位置）；
- 新键行按增量文件顺序**追加在尾部**；
- 键匹配用 ``key_norm`` 归一：去首尾空白；整数值浮点折叠（``1902.0`` / ``"1902.0"``
  / ``1902`` 三种写法视为同一键，SQLite 回读 REAL 与文件整数的差异由此吸收）；
  ``None``/空串是非法键（guard）；
- **匹配归一、存储保留增量原值**：命中行以增量行的值整体覆盖，不把归一键回写
  存储——带前导零的编码（``"001"``）或带空白的文本键不会被悄悄改写；归一只管
  行身份；
- 值不做任何类型改写——类型收敛是 ``Engine.load_table`` 的事，合并只管行身份。
"""
from __future__ import annotations

import math
import re

KEY_MAX = 8

# Long pure-integer TEXT keys must not go through float(): 17+ digits lose
# precision (float(20260917000000001) → …000), so the same id read as int in
# the base table and as text in the delta file stopped matching — and two long
# ids differing only in their last digits collapsed onto one key and the delta
# silently overwrote the WRONG base row. They are folded textually instead.
_KEY_INT_RE = re.compile(r"[0-9]+")
# …while the xlsx float spill ("1902.0", "1902.00") still folds onto the int.
_KEY_SPILL_RE = re.compile(r"([0-9]+)\.0+")
# 其余数值文本 = 与 JS UPSERT_NUM_RE 同一条显式文法（editor-runtime.js
# #region UPSERT_MERGE）。不用内建 float() 当兜底：float() 另接受 '1_000'
# （PEP 515）、全角数字、'NaN'/'Infinity' —— JS 文法不接受。同一主键会在
# singlefile（JS 合并）与 server（PY 合并）两侧归一成不同键，该更新的行被
# 追加成重复行。
_KEY_NUM_RE = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


class UpsertError(ValueError):
    """守卫命中：message 是可直接展示给用户的可行动文案。"""


def key_norm(value) -> str | None:
    """主键归一（JS upsertKeyNorm 的镜像，逐分支一致）。非法键返回 None。

    两侧已知尾差（有意不锁）：值 <1e-4 的分数形态两侧 plain/指数阈值不同
    （JS String(1e-5)='0.00001' vs PY repr='1e-05'）；指数写法的零填充已在
    两侧归一为 JS 形态（'1e-07'→'1e-7'）。真实主键列不携带该形态的值。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    s = str(value).strip()
    if not s:
        return None
    m = _KEY_INT_RE.fullmatch(s)
    if m:
        return s.lstrip("0") or "0"
    m = _KEY_SPILL_RE.fullmatch(s)
    if m:
        return m.group(1).lstrip("0") or "0"
    if _KEY_NUM_RE.fullmatch(s):
        f = float(s)
        if not math.isfinite(f):  # '1e999' 溢出为 inf：保守保留原文（JS isFinite 同款）
            return s
        if f.is_integer():
            return str(int(f))  # 整数值浮点折叠：与纯数字分支同键（'1.5e21'→全位）
        r = repr(f)
        if "e" in r:  # Python 指数零填充（1e-07）；JS String 无（1e-7）
            m2, _, ex = r.partition("e")
            r = m2 + "e" + ("-" if ex.startswith("-") else "+") + ex.lstrip("+-").lstrip("0")
        return r
    return s


def _key_of(row: list, idx: int) -> str | None:
    return key_norm(row[idx]) if idx < len(row) else None


def upsert_merge(columns: list[str], base_rows: list[list],
                 delta_rows: list[list], key_cols: list[str],
                 delta_columns: list[str] | None = None) -> dict:
    """把 ``delta_rows`` 按主键 ``key_cols`` 并入 ``base_rows``（不改动入参）。

    返回 ``{"rows": 合并后行, "stats": {"inserted": n, "updated": m}}``；
    守卫命中抛 ``UpsertError``。
    """
    cols = [str(c) for c in (columns or [])]
    if not cols:
        raise UpsertError("当前表没有列，无法增量合并")
    keys = [str(c) for c in (key_cols or [])]
    if not keys:
        raise UpsertError("增量合并必须选择主键列（至少 1 列）")
    if len(keys) > KEY_MAX:
        raise UpsertError(f"主键列最多 {KEY_MAX} 列（当前 {len(keys)} 列）")
    if len(set(keys)) != len(keys):
        raise UpsertError("主键列有重复：" + "、".join(keys))
    missing_keys = [k for k in keys if k not in cols]
    if missing_keys:
        raise UpsertError("主键列不在当前表中：" + "、".join(missing_keys))
    key_idx = [cols.index(k) for k in keys]

    # 列集守卫：增量文件列集合必须与现表一致（部分列更新会静默把未带列写成空，
    # 比直接拒绝危险得多；需要部分列更新时走列映射向导 + 全量替换）。
    if delta_columns is not None:
        dcols = [str(c) for c in delta_columns]
        missing = [c for c in cols if c not in dcols]
        extra = [c for c in dcols if c not in cols]
        if missing or extra:
            raise UpsertError("增量文件列结构与当前表不一致"
                              "（缺列：" + "、".join(missing[:5]) +
                              "；多列：" + "、".join(extra[:5]) + "）。"
                              "请使用全量替换（列映射向导）或调整文件。")
        dmap = [dcols.index(c) for c in cols]
        delta_aligned = [[(row[i] if i < len(row) else None) for i in dmap]
                         for row in delta_rows]
    else:
        delta_aligned = [list(row) for row in delta_rows]

    # 空值守卫：主键含 NULL/空串时行身份不明，合并语义无定义 → 拒绝并指出位置。
    for label, rows, off in (("当前表", base_rows, 0), ("增量文件", delta_aligned, 0)):
        for rno, row in enumerate(rows, 1):
            for ki, kname in zip(key_idx, keys):
                if _key_of(row, ki) is None:
                    raise UpsertError(
                        f"{label}第 {rno} 行的主键列「{kname}」为空——"
                        "增量合并要求主键值非空；请修正文件或改用全量替换。")

    def _dup_examples(rows: list[list], label: str) -> str | None:
        seen: dict[tuple, int] = {}
        dups: list[str] = []
        for row in rows:
            k = tuple(_key_of(row, ki) for ki in key_idx)
            if k in seen and len(dups) < 3:
                dups.append("×".join(k))
            seen[k] = seen.get(k, 0) + 1
        if dups:
            return f"{label}存在重复主键（示例：" + "、".join(dups) + "）——" + (
                "请先清理重复行再增量合并，或改用全量替换。"
                if label == "增量文件" else
                "当前表主键不唯一，增量合并语义无定义；请改用全量替换。")
        return None

    dup = _dup_examples(base_rows, "当前表")
    if dup:
        raise UpsertError(dup)
    dup = _dup_examples(delta_aligned, "增量文件")
    if dup:
        raise UpsertError(dup)

    # 合并：基表行序不变，命中的行原位替换；新键按增量顺序追加。
    base_index: dict[tuple, int] = {}
    for rno, row in enumerate(base_rows):
        base_index[tuple(_key_of(row, ki) for ki in key_idx)] = rno
    merged = [list(r) for r in base_rows]
    inserted = 0
    updated = 0
    for row in delta_aligned:
        k = tuple(_key_of(row, ki) for ki in key_idx)
        at = base_index.get(k)
        if at is None:
            merged.append(list(row))
            inserted += 1
        else:
            merged[at] = list(row)
            updated += 1
    return {"rows": merged, "stats": {"inserted": inserted, "updated": updated}}
