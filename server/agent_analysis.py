"""Deterministic analysis helpers used by Agent tools.

These functions operate on already aggregated rows/series. They do not read
raw data or invent an aggregation, keeping the Flint aggregate seam owned by
``server.aggregator``.
"""
from __future__ import annotations

import math
import datetime
from statistics import median
from typing import Any, Iterable, Mapping


def _date_parser():
    """Load the repository's single temporal-semantics implementation lazily.

    Flint's aggregate contract leaves row order to the caller
    (``refs/flint/src/core/aggregate.ts:7-20``), and its default overflow
    strategy preserves encounter order when no explicit semantic sort exists
    (``refs/flint/src/core/filter-overflow.ts:181-184``).  The repository's
    ``scripts/date_parse.py`` is the shared calendar parser for server,
    compiler, and browser.  Keeping this import lazy preserves the small
    helper's standalone/test import path without creating a second date
    implementation here.
    """
    try:
        import date_parse
        return date_parse
    except ImportError:
        import sys
        from pathlib import Path
        scripts_dir = str(Path(__file__).resolve().parent.parent / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        import date_parse
        return date_parse


def _calendar_module():
    """Load the explicit business-calendar layer without a second parser."""
    try:
        from . import agent_extensions
        return agent_extensions
    except ImportError:
        import sys
        from pathlib import Path
        skill_dir = str(Path(__file__).resolve().parent.parent)
        if skill_dir not in sys.path:
            sys.path.insert(0, skill_dir)
        from server import agent_extensions
        return agent_extensions


def _temporal_structure(labels: list[Any], name: str = "",
                        calendar: Mapping[str, Any] | None = None) -> tuple[str | None, list[int | None], list[dict[str, Any]]]:
    """Return ``(grain, intervals, structural_changes)`` for a temporal axis.

    ``intervals`` contains the calendar distance between adjacent observations
    (in the inferred grain).  ``None`` means that the labels are not a single,
    consistently parseable temporal series.  This intentionally does not sort
    the input: the ToolRunner owns evidence ordering and the returned indexes
    must remain indexes into the evidence rows it supplied.
    """
    if len(labels) < 2:
        return None, [], []
    parser = _date_parser()
    calendar_layer = _calendar_module()
    calendar_config = calendar_layer.normalize_calendar(calendar)
    grains = [parser.grain(label) for label in labels]
    inferred = calendar_layer.infer_grain(labels, name)
    # ``infer_grain`` deliberately requires a year-name hint for bare years;
    # an analysis helper has already received the dimension values, so an
    # all-year axis is unambiguous here.
    if inferred is None and grains and all(grain == "year" for grain in grains):
        inferred = "year"
    # Raw values are validated by date_parse.grain; canonical fiscal labels do
    # not have a Gregorian (y,m,d) grain, so their validity is checked by the
    # calendar-aware position() call below instead of rejecting them here.
    if inferred is None:
        return None, [], []

    def position(value: Any) -> int | None:
        if inferred == "week":
            week = parser.week_parts(value)
            if not week:
                return None
            year, number = week
            if year is None:
                return number
            try:
                # Monday's ordinal gives an exact cross-year interval without
                # assuming every ISO year has 52 weeks.
                return datetime.date.fromisocalendar(year, number, 1).toordinal() // 7
            except ValueError:
                return None
        return calendar_layer.position(value, inferred, calendar_config)

    positions = [position(label) for label in labels]
    if any(value is None for value in positions):
        return None, [], []
    intervals = [right - left for left, right in zip(positions, positions[1:])]
    changes = []
    for index, distance in enumerate(intervals):
        if distance > 1:
            changes.append({
                "kind": "calendar_gap",
                "from": labels[index],
                "to": labels[index + 1],
                "grain": inferred,
                "missingUnits": distance - 1,
            })
    return inferred, intervals, changes[:8]


def _root_cause_series(labels: list[Any], values: list[Any]) -> tuple[list[Any], list[Any]]:
    """Normalize a root-cause series when its members are temporal labels.

    Root-cause output exposes labels rather than positional evidence indexes,
    so it can safely canonicalize a temporal member list before calculating
    structural gaps.  Non-temporal members retain encounter order, matching
    Flint's default overflow behavior.
    """
    pairs = list(zip(labels, values))
    grain, _intervals, _changes = _temporal_structure([pair[0] for pair in pairs])
    if grain:
        parser = _date_parser()
        pairs.sort(key=lambda pair: parser.sort_key(pair[0]))
    return [pair[0] for pair in pairs], [pair[1] for pair in pairs]


def _descriptive_change_points(labels: list[Any], values: list[Any],
                               intervals: list[int | None]) -> list[dict[str, Any]]:
    """Describe local slope changes without turning them into causes.

    This is deliberately a separate descriptive layer from Flint field
    semantics (``refs/flint/src/core/field-semantics.ts``): it never changes
    field roles, aggregation, or evidence order.  Calendar gaps remain hard
    barriers, so a change point cannot bridge an unobserved period.
    """
    points: list[dict[str, Any]] = []
    limit = min(len(labels), len(values))
    for index in range(1, limit - 1):
        if intervals and (intervals[index - 1] != 1 or intervals[index] != 1):
            continue
        try:
            prev = float(values[index - 1])
            current = float(values[index])
            nxt = float(values[index + 1])
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(value) for value in (prev, current, nxt)):
            continue
        before_delta, after_delta = current - prev, nxt - current
        if before_delta == 0 or after_delta == 0:
            continue
        before_sign = "up" if before_delta > 0 else "down"
        after_sign = "up" if after_delta > 0 else "down"
        if before_sign != after_sign:
            points.append({"kind": "direction_change", "index": index,
                           "label": labels[index], "from": before_sign,
                           "to": after_sign, "beforeDelta": before_delta,
                           "afterDelta": after_delta})
            continue
        smaller = min(abs(before_delta), abs(after_delta))
        larger = max(abs(before_delta), abs(after_delta))
        if smaller > 0 and larger / smaller >= 2:
            points.append({"kind": "acceleration_change", "index": index,
                           "label": labels[index], "direction": before_sign,
                           "beforeDelta": before_delta, "afterDelta": after_delta,
                           "magnitudeRatio": round(larger / smaller, 3)})
    return points[:8]


def compare_periods(current: float | None, previous: float | None) -> dict[str, Any]:
    if current is None or previous is None:
        return {"current": current, "previous": previous, "delta": None, "rate": None,
                "comparable": False}
    delta = float(current) - float(previous)
    rate = None if float(previous) == 0 else delta / float(previous)
    return {"current": current, "previous": previous, "delta": delta, "rate": rate,
            "comparable": True}


def contribution(rows: Iterable[Mapping[str, Any]], *, member: str = "member",
                 value: str = "value") -> list[dict[str, Any]]:
    """Rank concrete members by absolute contribution to the total."""
    normalized = []
    for row in rows:
        try:
            number = float(row.get(value))
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            normalized.append({"member": row.get(member), "value": number})
    total = sum(item["value"] for item in normalized)
    for item in normalized:
        item["share"] = None if total == 0 else item["value"] / total
    return sorted(normalized, key=lambda item: abs(item["value"]), reverse=True)


def detect_anomalies(values: Iterable[float | int | None], *, threshold: float = 3.5) -> list[dict[str, Any]]:
    """Use median absolute deviation; return indexes and robust scores."""
    series = list(values)
    numbers = [float(value) for value in series
               if value is not None and isinstance(value, (int, float)) and math.isfinite(float(value))]
    if len(numbers) < 3:
        return []
    center = median(numbers)
    deviations = [abs(value - center) for value in numbers]
    mad = median(deviations)
    scale = mad * 1.4826
    if scale == 0:
        scale = max(abs(center) * 1e-9, 1e-9)
    output = []
    for index, value in enumerate(series):
        if value is None or not isinstance(value, (int, float)):
            continue
        score = abs(float(value) - center) / scale
        if score >= threshold:
            output.append({"index": index, "value": value, "score": score,
                           "center": center})
    return output


def data_quality(rows: Iterable[Iterable[Any]], columns: list[str]) -> dict[str, Any]:
    materialized = [list(row) for row in rows]
    nulls = {column: 0 for column in columns}
    for row in materialized:
        for index, column in enumerate(columns):
            if index >= len(row) or row[index] is None or row[index] == "":
                nulls[column] += 1
    return {"rowCount": len(materialized), "columnCount": len(columns),
            "nulls": nulls, "nullRate": {
                column: (nulls[column] / len(materialized) if materialized else 0)
                for column in columns}}


def quality_diagnostics(rows: Iterable[Iterable[Any]], columns: list[str], *,
                        measure_columns: Iterable[str] = (), time_column: str | None = None,
                        key_columns: Iterable[str] = (),
                        calendar: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Bounded data-quality facts; never upgrades anomalies into causal claims."""
    materialized = [list(row) for row in rows]
    base = data_quality(materialized, columns)
    indexes = {name: index for index, name in enumerate(columns)}
    negatives = {name: 0 for name in measure_columns if name in indexes}
    type_sets = {name: set() for name in columns}
    for row in materialized:
        for name, index in indexes.items():
            value = row[index] if index < len(row) else None
            if value is None or value == "":
                continue
            if isinstance(value, bool):
                kind = "boolean"
            elif isinstance(value, (int, float)):
                kind = "number"
            else:
                try:
                    float(str(value).strip())
                    kind = "numeric-text"
                except (TypeError, ValueError):
                    kind = "text"
            type_sets[name].add(kind)
            if name in negatives:
                try:
                    if float(value) < 0:
                        negatives[name] += 1
                except (TypeError, ValueError):
                    pass
    duplicate_count = len(materialized) - len({tuple(str(value) for value in row) for row in materialized})
    drift = {name: sorted(kinds) for name, kinds in type_sets.items() if len(kinds) > 1}
    diagnostics = {"duplicateRowsInSample": max(0, duplicate_count),
                   "negativeCounts": negatives, "typeDrift": drift,
                   "sampled": True}
    keys = [str(name) for name in key_columns if str(name) in indexes]
    if keys:
        key_indexes = [indexes[name] for name in keys]
        complete_key_rows = [tuple(row[index] if index < len(row) else None for index in key_indexes)
                             for row in materialized
                             if all(index < len(row) and row[index] not in (None, "") for index in key_indexes)]
        diagnostics["keyColumns"] = keys
        diagnostics["keyRowsChecked"] = len(complete_key_rows)
        diagnostics["keyRowsMissingValues"] = len(materialized) - len(complete_key_rows)
        diagnostics["duplicateKeysInSample"] = max(0, len(complete_key_rows) - len(set(complete_key_rows)))
    if time_column and time_column in indexes:
        values = [row[indexes[time_column]] for row in materialized
                  if indexes[time_column] < len(row) and row[indexes[time_column]] not in (None, "")]
        diagnostics["timeValuesInSample"] = len(values)
        # Calendar-gap diagnostics are explicitly sample-scoped.  They use the
        # same date parser as compile/server/browser, and never claim that a
        # sampled gap proves a full-table data defect.
        try:
            parser = _date_parser()
            # Samples are not guaranteed to arrive in calendar order. Sort
            # only the diagnostic copy; never reorder the caller's evidence.
            ordered = sorted(values, key=parser.sort_key)
            grain, _intervals, gaps = _temporal_structure(ordered, time_column or "", calendar)
            diagnostics["timeGrainInSample"] = grain
            diagnostics["timeGapCountInSample"] = len(gaps)
            diagnostics["timeGapSamples"] = gaps[:8]
        except Exception:
            diagnostics["timeGapCheck"] = "unavailable"
    return {**base, "diagnostics": diagnostics}


def trend_inflections(labels: Iterable[Any], values: Iterable[Any], *,
                      calendar: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Find peaks/troughs without bridging an unobserved calendar period.

    A SQL ``GROUP BY`` can legitimately return Jan and Mar without Feb.  The
    old adjacent-row rule then called Mar a peak when Jan < Mar > Apr, even
    though the first comparison crossed an unobserved period.  Keep the input
    order (so evidence indexes stay stable), expose the gap explicitly, and
    only emit an inflection when both neighboring calendar intervals are
    contiguous.
    """
    labels_list, values_list = list(labels), list(values)
    _grain, intervals, structural_changes = _temporal_structure(labels_list, calendar=calendar)
    change_points = _descriptive_change_points(labels_list, values_list, intervals)
    points = []
    for index in range(1, min(len(labels_list), len(values_list)) - 1):
        if intervals and (intervals[index - 1] != 1 or intervals[index] != 1):
            continue
        try:
            prev, cur, nxt = float(values_list[index - 1]), float(values_list[index]), float(values_list[index + 1])
        except (TypeError, ValueError):
            continue
        if cur >= prev and cur > nxt:
            points.append({"index": index, "label": labels_list[index], "value": cur, "kind": "peak"})
        elif cur <= prev and cur < nxt:
            points.append({"index": index, "label": labels_list[index], "value": cur, "kind": "trough"})
    numeric = []
    for value in values_list:
        if isinstance(value, bool):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            numeric.append(number)
    direction = "up" if len(numeric) >= 2 and numeric[-1] > numeric[0] else "down" if len(numeric) >= 2 and numeric[-1] < numeric[0] else "flat"
    return {"direction": direction, "inflections": points,
            "structuralChanges": structural_changes, "changePoints": change_points}


def what_if(value: Any = None, delta_percent: Any = 0, absolute_delta: Any = 0,
            scenarios: Iterable[Mapping[str, Any]] | None = None,
            links: Iterable[Mapping[str, Any]] | None = None,
            constraints: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return one or several virtual scenarios; it never writes report data.

    Multi-metric scenarios are deliberately independent transformations, not a
    fake aggregate: each named input keeps its own base/delta and the result is
    marked virtual. This preserves Flint's caller-owned aggregation contract
    (refs/flint/src/core/aggregate.ts:7-20) and avoids implying correlations
    between measures whose business model was not supplied.
    """
    def number(raw: Any, label: str) -> float:
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"what-if {label} must be numeric") from exc
        if not math.isfinite(value):
            raise ValueError(f"what-if {label} must be finite")
        return value

    def bound(value: float, rule: Any, label: str) -> tuple[float, bool]:
        if rule is None:
            return value, False
        if not isinstance(rule, Mapping):
            raise ValueError(f"what-if {label}.constraints must be an object")
        low = number(rule["min"], f"{label}.constraints.min") if rule.get("min") is not None else None
        high = number(rule["max"], f"{label}.constraints.max") if rule.get("max") is not None else None
        if low is not None and high is not None and low > high:
            raise ValueError(f"what-if {label}.constraints min must be <= max")
        if rule.get("nonNegative") is True:
            low = max(0.0, low) if low is not None else 0.0
        if rule.get("nonNegative") not in (None, True, False):
            raise ValueError(f"what-if {label}.constraints.nonNegative must be boolean")
        bounded = value
        if low is not None:
            bounded = max(low, bounded)
        if high is not None:
            bounded = min(high, bounded)
        return bounded, bounded != value

    def independent(item: Mapping[str, Any], label: str) -> dict[str, Any]:
        base = number(item.get("value"), f"{label}.value")
        pct = number(item.get("deltaPercent", 0) or 0, f"{label}.deltaPercent")
        absolute = number(item.get("absoluteDelta", 0) or 0, f"{label}.absoluteDelta")
        raw = base * (1 + pct / 100.0) + absolute
        simulated, clamped = bound(raw, item.get("constraints"), label)
        return {"base": base, "deltaPercent": pct, "absoluteDelta": absolute,
                "unconstrained": raw, "simulated": simulated,
                "constraintApplied": clamped}

    if scenarios is not None:
        if not isinstance(scenarios, list) or not scenarios or len(scenarios) > 12:
            raise ValueError("what-if scenarios must contain 1-12 items")
        names: set[str] = set()
        by_name: dict[str, dict[str, Any]] = {}
        raw_items: dict[str, Mapping[str, Any]] = {}
        for item in scenarios:
            if not isinstance(item, Mapping):
                raise ValueError("each what-if scenario must be an object")
            name = item.get("name")
            if not isinstance(name, str) or not name.strip() or len(name) > 120:
                raise ValueError("each what-if scenario requires a bounded name")
            name = name.strip()
            if name in names:
                raise ValueError(f"duplicate what-if scenario name: {name}")
            names.add(name)
            result = independent(item, name)
            result["name"] = name
            by_name[name] = result
            raw_items[name] = item

        normalized_links = []
        targets: set[str] = set()
        graph: dict[str, str] = {}
        for link in links or []:
            if not isinstance(link, Mapping):
                raise ValueError("what-if links must contain objects")
            source, target = link.get("source"), link.get("target")
            if not isinstance(source, str) or not isinstance(target, str) or source not in names or target not in names:
                raise ValueError("what-if link source/target must reference scenario names")
            if source == target or target in targets:
                raise ValueError("what-if links cannot self-link or define duplicate targets")
            if target in graph:
                raise ValueError("what-if links must form a bounded one-parent graph")
            ratio = number(link.get("ratio", 1), "link.ratio")
            offset = number(link.get("offset", 0), "link.offset")
            graph[target] = source
            targets.add(target)
            normalized_links.append({"source": source, "target": target,
                                     "ratio": ratio, "offset": offset})
        # Resolve parent links independently of input order and fail closed on
        # cycles.  A link is an explicit caller-supplied correlation rule, not
        # a correlation inferred from the report.
        visiting: set[str] = set()
        resolved: set[str] = set()
        link_by_target = {item["target"]: item for item in normalized_links}

        def resolve(name: str) -> None:
            if name in resolved:
                return
            if name in visiting:
                raise ValueError("what-if links must not contain cycles")
            visiting.add(name)
            link = link_by_target.get(name)
            if link:
                resolve(link["source"])
                raw = by_name[name]["simulated"]
                linked = by_name[link["source"]]["simulated"] * link["ratio"] + link["offset"]
                by_name[name]["unconstrained"] = linked
                by_name[name]["simulated"], by_name[name]["constraintApplied"] = bound(
                    linked, raw_items[name].get("constraints"), name)
                by_name[name]["linkedFrom"] = {"name": link["source"],
                                                 "ratio": link["ratio"], "offset": link["offset"]}
            visiting.remove(name)
            resolved.add(name)

        for name in names:
            resolve(name)
        output = [by_name[str(item.get("name"))] for item in scenarios]
        result = {"scenarios": output, "virtual": True}
        if normalized_links:
            result["links"] = normalized_links
        if constraints is not None:
            # A top-level constraint is an explicit convenience for a single
            # shared bound; apply it after links and expose the clamp.
            for item in output:
                bounded, clamped = bound(item["simulated"], constraints, str(item["name"]))
                item["simulated"] = bounded
                item["constraintApplied"] = bool(item.get("constraintApplied") or clamped)
            result["sharedConstraint"] = True
        result["constraintsApplied"] = sum(1 for item in output if item.get("constraintApplied"))
        return result
    item = {"value": value, "deltaPercent": delta_percent, "absoluteDelta": absolute_delta,
            "constraints": constraints}
    result = independent(item, "scenario")
    result.pop("unconstrained", None)
    result["virtual"] = True
    return result


def rank_root_causes(labels: Iterable[Any], values: Iterable[Any], *, threshold: float = 3.5,
                     calendar: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Rank evidence-backed follow-ups without claiming causality."""
    labels_list, values_list = _root_cause_series(list(labels), list(values))
    _grain, intervals, structural_changes = _temporal_structure(labels_list, calendar=calendar)
    change_points = _descriptive_change_points(labels_list, values_list, intervals)
    anomalies = detect_anomalies(values_list, threshold=threshold)
    members = contribution([{"member": label, "value": value}
                            for label, value in zip(labels_list, values_list)])
    hypotheses: list[dict[str, Any]] = []
    for item in anomalies[:5]:
        index = int(item["index"])
        hypotheses.append({"rank": len(hypotheses) + 1, "type": "anomaly",
                           "label": labels_list[index] if index < len(labels_list) else None,
                           "score": item["score"], "confidence": "low",
                           "nextAction": "下钻该成员并核对数据源；异常本身不等于因果原因"})
    for item in members[:5]:
        hypotheses.append({"rank": len(hypotheses) + 1, "type": "concentration",
                           "label": item["member"], "share": item["share"], "confidence": "low",
                           "nextAction": "检查该成员的期间变化与贡献构成"})
    limitations = ["仅基于聚合序列、异常分数与贡献度，未执行因果推断"]
    if change_points:
        limitations.append("changePoints 只是相邻斜率变化的描述，不代表业务原因")
    if structural_changes:
        limitations.append("存在未观测的日历周期；异常或集中度不能解释为缺失周期的因果证据")
    return {"hypotheses": hypotheses[:8], "causalClaim": False,
            "structuralChanges": structural_changes, "changePoints": change_points,
            "limitations": limitations}


def rank_root_causes_multi(series: Mapping[str, Mapping[str, Any]], *,
                           threshold: float = 3.5,
                           calendar: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Compare bounded root-cause signals across explicitly supplied measures.

    Each measure is analysed independently first; the cross-metric result only
    reports labels that recur in at least two independently observed series.
    It is a follow-up prioritisation signal, never a causal or correlation
    estimate.  This preserves Flint's caller-owned aggregate contract
    (``refs/flint/src/core/aggregate.ts:7-20``) while making multi-metric
    analysis useful without inventing a business model.
    """
    if not isinstance(series, Mapping) or not series or len(series) > 4:
        raise ValueError("multi root-cause series must contain 1-4 measures")
    metrics = []
    label_to_measures: dict[str, list[str]] = {}
    for name, payload in series.items():
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            raise ValueError("multi root-cause measure names must be bounded strings")
        if not isinstance(payload, Mapping):
            raise ValueError("multi root-cause series entries must be objects")
        labels, values = payload.get("labels"), payload.get("values")
        if not isinstance(labels, list) or not isinstance(values, list):
            raise ValueError("multi root-cause series requires labels and values arrays")
        result = rank_root_causes(labels, values, threshold=threshold, calendar=calendar)
        metrics.append({"measure": name, **result})
        for hypothesis in result.get("hypotheses") or []:
            label = hypothesis.get("label")
            if label is not None:
                label_to_measures.setdefault(str(label), []).append(name)
    shared = [{"label": label, "measures": sorted(set(names)), "confidence": "low",
               "causalClaim": False}
              for label, names in sorted(label_to_measures.items())
              if len(set(names)) >= 2][:8]
    return {"metrics": metrics, "sharedSignals": shared, "causalClaim": False,
            "limitations": ["各指标独立聚合后仅按重复标签排序，不能解释相关性或因果关系"]}


def recommend_actions(labels: Iterable[Any], values: Iterable[Any], *,
                      base_args: Mapping[str, Any], truncated: bool = False,
                      limit: int = 6,
                      calendar: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build bounded, evidence-linked next actions from an observed series.

    This is deliberately a deterministic recommendation layer, not a second
    model.  Flint's aggregate contract keeps grouping and aggregation owned by
    the caller/backend (``refs/flint/src/core/aggregate.ts:7-20``), while its
    overflow contract requires truncation to remain explicit
    (``refs/flint/src/core/filter-overflow.ts:12-20``).  The helper therefore
    consumes only server-owned aggregate rows, never accepts prose/SQL, and
    emits read-only tool intents with a bounded evidence basis.  It does not
    claim causality or mutate a report.
    """
    try:
        cap = max(1, min(int(limit), 6))
    except (TypeError, ValueError):
        cap = 6
    labels_list, values_list = list(labels), list(values)
    # Keep this series in calendar order for recommendations, matching the
    # same date_parse mirror used by trend_inflections and ToolRunner.
    parser = _date_parser()
    inferred_grain = _calendar_module().infer_grain(labels_list, "") if labels_list else None
    if inferred_grain:
        pairs = sorted(zip(labels_list, values_list), key=lambda pair: parser.sort_key(pair[0]))
        labels_list = [pair[0] for pair in pairs]
        values_list = [pair[1] for pair in pairs]
    grain, _intervals, gaps = _temporal_structure(labels_list, calendar=calendar)
    anomalies = detect_anomalies(values_list)
    trend = trend_inflections(labels_list, values_list, calendar=calendar)
    safe_args = {}
    for key in ("chartId", "datasetId", "dimension", "measure", "aggregation",
                "filter", "bucket", "grain", "timeWindow", "maxScanRows", "maxGroups"):
        value = base_args.get(key)
        if value is not None:
            safe_args[key] = value

    actions: list[dict[str, Any]] = []

    # These are deterministic read-only business follow-up templates, not
    # model-authored instructions.  They give the narrative layer an explicit
    # objective and success criterion without pretending that the Agent knows
    # the organisation's owner/approval workflow.
    templates = {
        "investigate-anomalies": {
            "objective": "定位异常期间或成员并核对原始数据",
            "ownerRole": "业务分析",
            "successCriteria": "确认异常是否可由数据缺口、口径或真实业务变化解释",
        },
        "check-calendar-gaps": {
            "objective": "核对时间序列缺口与数据完整性",
            "ownerRole": "数据质量",
            "successCriteria": "确认缺口是业务无数据还是导入/时间解析问题",
        },
        "compare-latest-periods": {
            "objective": "按当前口径核对最近两个期间",
            "ownerRole": "业务分析",
            "successCriteria": "取得可复核的期间差额与变化率",
        },
        "narrow-truncated-evidence": {
            "objective": "缩小时间或成员范围后重取完整证据",
            "ownerRole": "业务分析",
            "successCriteria": "获得未截断的服务端证据并保留筛选条件",
        },
        "validate-data-quality": {
            "objective": "先验证空值、重复、类型漂移与时间结构",
            "ownerRole": "数据质量",
            "successCriteria": "确认数据质量事实后再解释趋势",
        },
    }

    def add(action_id: str, tool: str, label: str, reason: str,
            evidence: list[str], args: Mapping[str, Any] | None = None,
            priority: str = "medium") -> None:
        if len(actions) >= cap or any(item["id"] == action_id for item in actions):
            return
        selected_args = dict(args or safe_args)
        # Recommendations are themselves typed tool calls.  Keep each action
        # within the target tool's schema; in particular data_quality is a
        # dataset-only diagnostic and must not inherit chart aggregation args.
        if tool == "data_quality":
            selected_args = {key: selected_args[key]
                             for key in ("datasetId", "maxScanRows")
                             if key in selected_args}
        actions.append({"id": action_id, "tool": tool, "label": label,
                        "reason": reason, "evidence": list(evidence),
                        "priority": priority, "effect": "read-only",
                        "requiresApproval": False,
                        "args": selected_args,
                        "template": {"kind": "read_only_followup",
                                     **templates.get(action_id, {
                                         "objective": label,
                                         "ownerRole": "业务分析",
                                         "successCriteria": "取得可复核的服务端证据",
                                     })}})

    if anomalies:
        add("investigate-anomalies", "root_cause_scan", "下钻核对异常期间/成员",
            f"观察到 {len(anomalies)} 个稳健异常分数点；异常不等于因果原因。",
            ["anomalies"], priority="high")
    if gaps:
        add("check-calendar-gaps", "data_quality", "检查时间缺口与数据完整性",
            f"时间轴存在 {len(gaps)} 个未观测日历区间，先核对数据质量。",
            ["structuralChanges"], priority="high")
    if trend.get("direction") != "flat" and len(values_list) >= 2:
        compare_args = dict(safe_args)
        if labels_list:
            compare_args["labels"] = {"previous": labels_list[-2], "current": labels_list[-1]}
        add("compare-latest-periods", "compare_periods", "比较最近两个期间",
            "序列首尾方向存在变化，使用同一口径核对最近期间差异。",
            ["direction"], args=compare_args)
    if truncated:
        add("narrow-truncated-evidence", "aggregate", "缩小范围后重取完整证据",
            "当前证据受到 TOP-N/行数或分组预算限制，不能把截断结果当作全量结论。",
            ["truncated"], priority="high")
    if not actions:
        add("validate-data-quality", "data_quality", "检查数据质量后再分析",
            "当前序列没有足够的异常或趋势信号，先验证空值、重复和时间结构。",
            ["observedSeries"], priority="low")
    narrative_bits = [f"基于 {len(values_list)} 个已观测聚合点生成建议"]
    if anomalies:
        narrative_bits.append(f"发现 {len(anomalies)} 个异常候选")
    if gaps:
        narrative_bits.append(f"发现 {len(gaps)} 个日历缺口")
    if truncated:
        narrative_bits.append("当前结果有截断限制")
    # This is a preview-only orchestration hint, not an executable AgentPlan:
    # every step is still a typed read-only tool intent and the caller must
    # normalize/authorize it through the Tool Registry before execution.
    plan_suggestion = {
        "kind": "read_only_plan_suggestion",
        "mode": "preview_only",
        "goal": "按观测信号完成后续只读核查",
        "termination": "完成建议步骤并取得服务端证据",
        "parallelizable": True,
        "requiresApproval": False,
        "steps": [{"id": "followup-" + str(item["id"]),
                    "tool": item["tool"], "args": dict(item["args"]),
                    "dependsOn": [], "expectedEvidence": list(item["evidence"])}
                   for item in actions],
    }
    return {
        "kind": "action_recommendations",
        "actions": actions,
        "planSuggestion": plan_suggestion,
        "narrative": {
            "text": "；".join(narrative_bits) + "。建议仅用于下一步核查，不是因果结论。",
            "basis": "observed",
            "causalClaim": False,
            "grain": grain,
        },
        "limitations": (["建议来自已聚合序列，不代表完整因果分析"]
                         + (["证据被截断，需缩小范围或导出完整结果"] if truncated else [])),
    }
