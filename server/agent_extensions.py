"""Bundled pure Agent extension seams kept together for the ≤100-file skill package.

The sections below retain the public pure functions of the former business-calendar,
confidence, alias-feedback, and report-impact modules. They do not perform I/O or
execute SQL; server callers provide the existing typed/engine seams.
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_SCRIPTS_DIR = str(_Path(__file__).resolve().parents[1] / "scripts")
if _SCRIPTS_DIR not in _sys.path:
    _sys.path.insert(0, _SCRIPTS_DIR)


# =============================================================================
# SOURCE SECTION: scripts/business_calendar.py
# =============================================================================
"""Explicit business-calendar semantics layered on top of ``date_parse``.

Flint classifies temporal fields and granules (``refs/flint/src/core/semantic-types.ts:30-43,61-79``),
but it does not decide an organisation's fiscal-year convention.  This module
keeps that domain decision explicit and bounded instead of making Agent tools
guess it from labels.  Gregorian behavior delegates to the repository's one
temporal parser; fiscal behavior shifts a date to a fiscal-year origin before
deriving year/quarter/month positions.

The first supported business calendar is a month-aligned fiscal calendar.  A
fiscal year is named by its start year (``FY2025`` starts at the configured
month in 2025).  Week labels remain ISO calendar weeks unless a caller opts
into a fiscal month/quarter/year operation; accepting a fake fiscal week would
silently invent a week-numbering policy.
"""

import datetime as _dt
import re
from typing import Any, Mapping

import date_parse


class BusinessCalendarError(ValueError):
    """A malformed or unsupported explicit calendar declaration."""


_KINDS = {"gregorian", "fiscal"}
_ALLOWED = {"kind", "fiscalStartMonth", "weekStart"}
_FISCAL_YEAR = re.compile(r"^FY(\d{4})$", re.I)
_FISCAL_QUARTER = re.compile(r"^FY(\d{4})-Q([1-4])$", re.I)
_FISCAL_MONTH = re.compile(r"^FY(\d{4})-M(0[1-9]|1[0-2])$", re.I)


def normalize_calendar(raw: Any = None, *, where: str = "calendar") -> dict[str, Any]:
    """Return a canonical, JSON-safe calendar declaration.

    ``None`` is deliberately normalized to Gregorian so callers can include
    the result in a context hash without treating omitted and explicit
    Gregorian declarations as different semantics.
    """
    if raw is None or raw == {}:
        return {"kind": "gregorian", "fiscalStartMonth": 1, "weekStart": 0}
    if isinstance(raw, str):
        raw = {"kind": raw}
    if not isinstance(raw, Mapping):
        raise BusinessCalendarError(f"{where} must be an object or 'gregorian'/'fiscal'")
    unknown = sorted(set(raw) - _ALLOWED)
    if unknown:
        raise BusinessCalendarError(f"{where} has unsupported field: {unknown[0]}")
    kind = raw.get("kind", "gregorian")
    # A malformed JSON shape must stay inside the typed calendar contract;
    # checking a list/object directly against the set would leak a raw
    # ``TypeError: unhashable type`` to the Agent caller.
    if not isinstance(kind, str) or kind not in _KINDS:
        raise BusinessCalendarError(f"{where}.kind must be gregorian or fiscal")
    start = raw.get("fiscalStartMonth", 1)
    week_start = raw.get("weekStart", 0)
    if isinstance(start, bool) or not isinstance(start, int) or not 1 <= start <= 12:
        raise BusinessCalendarError(f"{where}.fiscalStartMonth must be an integer 1-12")
    if isinstance(week_start, bool) or not isinstance(week_start, int) or not 0 <= week_start <= 6:
        raise BusinessCalendarError(f"{where}.weekStart must be an integer 0-6")
    # Week numbering is intentionally still delegated to the shared ISO
    # parser.  Do not accept a non-zero weekStart and silently ignore it;
    # explicit fiscal week rules need a separate calendar definition rather
    # than a misleading no-op option.
    if week_start != 0:
        raise BusinessCalendarError(
            f"{where}.weekStart must be 0 until an explicit non-ISO week calendar is supported")
    if kind == "gregorian" and start != 1:
        raise BusinessCalendarError(
            f"{where}.fiscalStartMonth is only valid for kind='fiscal'")
    return {"kind": kind, "fiscalStartMonth": start if kind == "fiscal" else 1,
            "weekStart": week_start}


def _parts(value: Any) -> tuple[int, int, int] | None:
    return date_parse.parts(value)


def _shifted_parts(value: Any, calendar: Mapping[str, Any] | None) -> tuple[int, int, int] | None:
    parts = _parts(value)
    if not parts:
        return None
    cfg = normalize_calendar(calendar)
    if cfg["kind"] != "fiscal":
        return parts
    y, month, day = parts
    # Shift the fiscal start month to January.  April-start 2025-04 becomes
    # 2025-01; March 2026 becomes 2025-12.  Month arithmetic avoids a second
    # date parser and is valid for YYYY/MM/DD, YYYYMM, and epoch inputs.
    shifted = y * 12 + (month - 1) + (1 - cfg["fiscalStartMonth"])
    sy, sm0 = divmod(shifted, 12)
    sm = sm0 + 1
    try:
        return sy, sm, min(day, _days_in_month(sy, sm))
    except ValueError:
        return None


def shifted_iso(value: Any, calendar: Mapping[str, Any] | None = None) -> str | None:
    """Return the fiscal-origin-shifted ISO date for SQL UDF consumers."""
    parts = _shifted_parts(value, calendar)
    if not parts:
        return None
    return f"{parts[0]:04d}-{parts[1]:02d}-{parts[2]:02d}"


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        nxt = _dt.date(year + 1, 1, 1)
    else:
        nxt = _dt.date(year, month + 1, 1)
    return (nxt - _dt.timedelta(days=1)).day


def grain(value: Any) -> str | None:
    """Delegate raw-value precision to the one shared parser."""
    return date_parse.grain(value)


def infer_grain(values: list[Any], name: str = "", threshold: float = 0.6) -> str | None:
    """Infer a grain from raw dates or canonical fiscal labels."""
    inferred = date_parse.infer_grain(values, name, threshold)
    if inferred:
        return inferred
    vals = [str(value).strip() for value in values
            if value is not None and str(value).strip()]
    if not vals:
        return None
    matches = {
        "year": sum(bool(_FISCAL_YEAR.fullmatch(value)) for value in vals),
        "quarter": sum(bool(_FISCAL_QUARTER.fullmatch(value)) for value in vals),
        "month": sum(bool(_FISCAL_MONTH.fullmatch(value)) for value in vals),
    }
    winner, count = max(matches.items(), key=lambda item: item[1])
    return winner if count / len(vals) > threshold else None


def label(value: Any, bucket: str, calendar: Mapping[str, Any] | None = None) -> str | None:
    """Return a canonical label under an explicit Gregorian/fiscal calendar."""
    cfg = normalize_calendar(calendar)
    text = str(value).strip() if value is not None else ""
    if cfg["kind"] == "fiscal" and bucket in {"year", "quarter", "month"}:
        # Idempotence matters when a server result is fed back into a second
        # analysis pass or an evidence export.
        if ((bucket == "year" and _FISCAL_YEAR.fullmatch(text)) or
                (bucket == "quarter" and _FISCAL_QUARTER.fullmatch(text)) or
                (bucket == "month" and _FISCAL_MONTH.fullmatch(text))):
            return text.upper()
    if cfg["kind"] == "gregorian":
        return date_parse.label(value, bucket)
    if bucket == "week":
        # ISO weeks carry a separate, explicit policy; do not silently claim
        # they are fiscal weeks merely because the fiscal year is configured.
        return date_parse.label(value, bucket)
    shifted = _shifted_parts(value, cfg)
    if not shifted:
        return None
    year, month, day = shifted
    if bucket == "year":
        return f"FY{year:04d}"
    if bucket == "quarter":
        return f"FY{year:04d}-Q{(month - 1) // 3 + 1}"
    if bucket == "month":
        return f"FY{year:04d}-M{month:02d}"
    if bucket == "day":
        # A day is still a point on the Gregorian date axis; fiscal semantics
        # affect period grouping, not the identity of a day.
        raw = _parts(value)
        return None if not raw else f"{raw[0]:04d}-{raw[1]:02d}-{raw[2]:02d}"
    if bucket == "raw":
        return str(value).strip()
    raise BusinessCalendarError(f"unsupported calendar bucket: {bucket!r}")


def position(value: Any, bucket: str, calendar: Mapping[str, Any] | None = None) -> int | None:
    """Return a monotonic period position for gap/change-point analysis."""
    cfg = normalize_calendar(calendar)
    if bucket == "week":
        raw = date_parse.week_parts(value)
        if not raw:
            return None
        year, number = raw
        if year is None:
            return number
        try:
            return _dt.date.fromisocalendar(year, number, 1).toordinal() // 7
        except ValueError:
            return None
    if cfg["kind"] == "fiscal":
        text = str(value or "").strip()
        fiscal_year = _FISCAL_YEAR.fullmatch(text)
        fiscal_quarter = _FISCAL_QUARTER.fullmatch(text)
        fiscal_month = _FISCAL_MONTH.fullmatch(text)
        if fiscal_year and bucket == "year":
            return int(fiscal_year.group(1))
        if fiscal_quarter and bucket == "quarter":
            return int(fiscal_quarter.group(1)) * 4 + int(fiscal_quarter.group(2)) - 1
        if fiscal_month and bucket == "month":
            return int(fiscal_month.group(1)) * 12 + int(fiscal_month.group(2)) - 1
    parts = _shifted_parts(value, cfg)
    if not parts:
        return None
    year, month, day = parts
    if bucket == "year":
        return year
    if bucket == "quarter":
        return year * 4 + (month - 1) // 3
    if bucket == "month":
        return year * 12 + month - 1
    if bucket == "day":
        raw = _parts(value)
        return _dt.date(*raw).toordinal() if raw else None
    return None


def range_of(value: Any, bucket: str | None = None,
             calendar: Mapping[str, Any] | None = None) -> tuple[str, str] | None:
    """Map a canonical Gregorian/fiscal period label to an inclusive ISO span."""
    cfg = normalize_calendar(calendar)
    if cfg["kind"] == "gregorian":
        return date_parse.range_of(value, bucket)
    text = str(value or "").strip()
    if not text:
        return None
    match = _FISCAL_YEAR.fullmatch(text)
    if match and (bucket in (None, "year")):
        fy = int(match.group(1))
        return _span_months(fy, cfg["fiscalStartMonth"], 12)
    match = _FISCAL_QUARTER.fullmatch(text)
    if match and (bucket in (None, "quarter")):
        fy, quarter = int(match.group(1)), int(match.group(2))
        return _span_months(fy, cfg["fiscalStartMonth"], 3, (quarter - 1) * 3)
    match = _FISCAL_MONTH.fullmatch(text)
    if match and (bucket in (None, "month")):
        fy, month_number = int(match.group(1)), int(match.group(2))
        return _span_months(fy, cfg["fiscalStartMonth"], 1, month_number - 1)
    return date_parse.range_of(value, bucket)


def _span_months(fiscal_year: int, start_month: int, count: int,
                 offset: int = 0) -> tuple[str, str]:
    absolute = fiscal_year * 12 + (start_month - 1) + offset
    year, month0 = divmod(absolute, 12)
    month = month0 + 1
    end_absolute = absolute + count - 1
    end_year, end_month0 = divmod(end_absolute, 12)
    end_month = end_month0 + 1
    return (f"{year:04d}-{month:02d}-01",
            f"{end_year:04d}-{end_month:02d}-{_days_in_month(end_year, end_month):02d}")


def summary(calendar: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """A bounded human/machine-readable semantic summary for ReportContext."""
    cfg = normalize_calendar(calendar)
    return {**cfg, "labelConvention": "fiscal-year-start",
            "weekPolicy": "iso-gregorian" if cfg["kind"] == "fiscal" else "iso"}



# =============================================================================
# SOURCE SECTION: server/agent_confidence.py
# =============================================================================
"""Bounded confidence scoring for typed Agent provenance.

This module deliberately scores provenance metadata only.  It never inspects
rows, evidence values, prompts, or model output, so callers cannot turn a
large or persuasive payload into stronger evidence by passing it here.

The score is an explainable ordering signal, not a calibrated probability.
Malformed provenance returns a closed result with zero score.  The rules are
grounded in the project's Agent plan (A12: provenance distinguishes
``observed``/``derived``/``bounded`` and does not claim probability
calibration), Flint's caller-owned aggregation contract
(``refs/flint/src/core/aggregate.ts:7-20``), and the OWASP boundary guidance
(``refs/security/owasp-llm-prompt-injection.md``): validate typed inputs and
keep untrusted data outside the authorization/evidence boundary.
"""

import math
from collections.abc import Mapping
from typing import Any


_CLASSIFICATION_SCORES = {
    "observed": 0.90,
    "derived": 0.72,
    "bounded": 0.48,
}
_LABEL_THRESHOLDS = ((0.75, "high"), (0.50, "medium"), (0.01, "low"))
_TRUNCATED_CAP = 0.45
_CAUSAL_CAP = 0.25
_LIST_PENALTY = 0.04
_MAX_LIST_PENALTY = 0.16


def _closed_result(reason: str) -> dict[str, Any]:
    """Return the only safe result when the typed input cannot be trusted."""
    return {
        "score": 0.0,
        "label": "insufficient",
        "scoreKind": "bounded_heuristic",
        "calibrated": False,
        "probability": False,
        "failClosed": True,
        "reasons": [reason],
    }


def confidence_label(score: Any) -> str:
    """Map a finite score in ``[0, 1]`` to a stable ordinal label.

    Invalid scores are treated as insufficient rather than coerced.  This
    keeps a bad upstream calculation from becoming an apparently valid label.
    """
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return "insufficient"
    if not math.isfinite(score) or score < 0.0 or score > 1.0:
        return "insufficient"
    for threshold, label in _LABEL_THRESHOLDS:
        if score >= threshold:
            return label
    return "insufficient"


def _validated_fields(provenance: Any) -> tuple[str, bool, list[str], list[str], bool] | None:
    """Validate only provenance metadata; never descend into evidence data."""
    if not isinstance(provenance, Mapping):
        return None

    required = ("classification", "truncated", "assumptions", "limitations", "causalClaim")
    if any(key not in provenance for key in required):
        return None

    classification = provenance.get("classification")
    if classification not in _CLASSIFICATION_SCORES:
        return None

    truncated = provenance.get("truncated")
    causal_claim = provenance.get("causalClaim")
    if type(truncated) is not bool or type(causal_claim) is not bool:
        return None

    assumptions = provenance.get("assumptions")
    limitations = provenance.get("limitations")
    if not isinstance(assumptions, list) or not isinstance(limitations, list):
        return None
    if any(not isinstance(item, str) or not item.strip() for item in assumptions + limitations):
        return None

    # Return only validated metadata.  In particular, do not return or copy
    # arbitrary fields that might contain raw rows or instruction-shaped text.
    return classification, truncated, assumptions, limitations, causal_claim


def assess_confidence(provenance: Any) -> dict[str, Any]:
    """Assess typed provenance with a bounded, deterministic heuristic.

    Required input fields are ``classification`` (one of ``observed``,
    ``derived``, ``bounded``), boolean ``truncated`` and ``causalClaim``, and
    string lists ``assumptions`` and ``limitations``.  The returned score is
    always in ``[0, 1]`` and is explicitly marked as uncalibrated.
    """
    fields = _validated_fields(provenance)
    if fields is None:
        return _closed_result("invalid_typed_provenance")

    classification, truncated, assumptions, limitations, causal_claim = fields
    score = _CLASSIFICATION_SCORES[classification]
    reasons = [
        {
            "factor": "classification",
            "value": classification,
            "reason": {
                "observed": "服务端观测证据",
                "derived": "服务端规则或统计推导",
                "bounded": "有界或覆盖不完整的证据",
            }[classification],
        }
    ]

    if truncated:
        score = min(score, _TRUNCATED_CAP)
        reasons.append({
            "factor": "truncated",
            "value": True,
            "reason": "证据被截断，不能外推完整成员集合",
        })

    if assumptions:
        penalty = min(len(assumptions) * _LIST_PENALTY, _MAX_LIST_PENALTY)
        score -= penalty
        reasons.append({
            "factor": "assumptions",
            "count": len(assumptions),
            "penalty": round(penalty, 3),
            "reason": "结果依赖显式假设",
        })

    if limitations:
        penalty = min(len(limitations) * _LIST_PENALTY, _MAX_LIST_PENALTY)
        score -= penalty
        reasons.append({
            "factor": "limitations",
            "count": len(limitations),
            "penalty": round(penalty, 3),
            "reason": "结果存在显式限制",
        })

    if causal_claim:
        # A causal flag cannot promote descriptive/aggregated evidence.  The
        # hard cap also makes an accidental claim visible to downstream code.
        score = min(score, _CAUSAL_CAP)
        reasons.append({
            "factor": "causalClaim",
            "value": True,
            "reason": "此评分器不验证因果关系，因果声明不得提升置信度",
        })

    score = max(0.0, min(1.0, round(score, 3)))
    return {
        "score": score,
        "label": confidence_label(score),
        "scoreKind": "bounded_heuristic",
        "calibrated": False,
        "probability": False,
        "failClosed": False,
        "reasons": reasons,
    }


def confidence_score(provenance: Any) -> float:
    """Return only the bounded score; invalid provenance returns ``0.0``."""
    return float(assess_confidence(provenance)["score"])


__all__ = ["assess_confidence", "confidence_label", "confidence_score"]


# =============================================================================
# SOURCE SECTION: server/alias_feedback.py
# =============================================================================
"""Pure alias normalization and confirmation seam for S02/S09.

This module deliberately has no persistence or server dependencies.  It keeps
three facts separate:

* authored and manifest declarations are already accepted field identity data;
* inferred declarations are candidates with a confidence score;
* an inferred candidate only becomes accepted after an explicit user
  confirmation.

Grounding:
``refs/flint/src/core/field-semantics.ts:138-170`` defines resolved field
semantics as a pure, field-identity result independent of visualization, and
``refs/flint/src/core/field-semantics.ts:212-227`` uses normalization to make
identity inputs deterministic.  The semantic type vocabulary used to validate
field identity is defined in ``refs/flint/src/core/semantic-types.ts:56-145``.
This seam applies the same deterministic, fail-closed boundary to business
aliases without changing those existing consumers.
"""


from dataclasses import dataclass, replace
import math
import re
import unicodedata
from typing import Any, Iterable, Literal, Mapping, Optional, Sequence


AliasSource = Literal["authored", "manifest", "inferred"]
AliasIssueCode = Literal[
    "invalid_declaration",
    "invalid_confidence",
    "unknown_field",
    "ambiguous_field",
    "sensitive_field",
    "duplicate",
    "conflict",
    "self_alias",
    "confirmation_required",
    "not_confirmable",
]

_MAX_TEXT = 240
_SEPARATOR_RE = re.compile(r"[\s_\-/\\]+")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class AliasRecord:
    """One normalized alias candidate.

    ``confirmed`` means the record is safe to use as a resolver mapping.  It
    is intentionally distinct from ``source``: an inferred record can be
    confirmed, while a source label alone never silently upgrades it.
    """

    canonical: str
    alias: str
    source: AliasSource
    confidence: float
    requires_confirmation: bool
    confirmed: bool

    @property
    def alias_key(self) -> str:
        return normalize_alias_key(self.alias)

    @property
    def canonical_key(self) -> str:
        return normalize_alias_key(self.canonical)


@dataclass(frozen=True)
class AliasIssue:
    """A bounded reason why a candidate cannot be accepted."""

    code: AliasIssueCode
    alias: Optional[str] = None
    canonical: Optional[str] = None
    detail: str = ""


@dataclass(frozen=True)
class AliasResolution:
    """Immutable normalization result.

    ``records`` contains every valid candidate that survived shape
    normalization.  ``accepted`` contains only authored/manifest records or
    explicitly confirmed inferred records.  A conflict or duplicate blocks
    the involved alias key; it can never be resolved by declaration order.
    """

    records: tuple[AliasRecord, ...]
    accepted: tuple[AliasRecord, ...]
    issues: tuple[AliasIssue, ...]

    @property
    def blocked(self) -> bool:
        return bool(self.issues)

    @property
    def accepted_map(self) -> dict[str, str]:
        """Return a fresh alias → canonical map for read-only consumers."""
        return {item.alias: item.canonical for item in self.accepted}

    def candidates_for(self, alias: str) -> tuple[AliasRecord, ...]:
        key = normalize_alias_key(alias)
        return tuple(item for item in self.records if item.alias_key == key)


class AliasConfirmationError(ValueError):
    """Raised when explicit confirmation cannot safely promote a candidate."""


def normalize_alias_text(value: Any) -> str:
    """Normalize display text without changing its business meaning.

    NFKC handles full-width forms, whitespace is collapsed, and surrounding
    whitespace is removed.  The returned value preserves case and separators
    for display; comparisons use :func:`normalize_alias_key`.
    """
    if not isinstance(value, str):
        return ""
    value = unicodedata.normalize("NFKC", value)
    value = " ".join(value.split()).strip()
    if not value or len(value) > _MAX_TEXT or _CONTROL_RE.search(value):
        return ""
    return value


def normalize_alias_key(value: Any) -> str:
    """Return a stable comparison key for common alias spelling variants."""
    text = normalize_alias_text(value)
    if not text:
        return ""
    return _SEPARATOR_RE.sub(" ", text).strip().casefold()


def _issue(code: AliasIssueCode, *, alias: Optional[str] = None,
           canonical: Optional[str] = None, detail: str = "") -> AliasIssue:
    return AliasIssue(code=code, alias=alias, canonical=canonical, detail=detail)


def _as_declarations(raw: Any, source: AliasSource) -> tuple[tuple[Any, Any, Any], ...]:
    """Convert supported JSON shapes into ``(canonical, alias, confidence)``.

    Mapping input follows the manifest shape ``{canonical: alias | [alias]}``.
    Sequence input accepts typed objects with ``canonical``/``alias`` and an
    optional ``confidence``.  Malformed items are retained as a sentinel so
    the caller can report a typed issue instead of guessing.
    """
    if raw is None:
        return ()
    result: list[tuple[Any, Any, Any]] = []
    if isinstance(raw, Mapping):
        for canonical, aliases in raw.items():
            values = aliases if isinstance(aliases, (list, tuple)) else [aliases]
            for item in values:
                if isinstance(item, Mapping):
                    result.append((canonical, item.get("alias", item.get("name")),
                                   item.get("confidence")))
                else:
                    result.append((canonical, item, None))
        return tuple(result)
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        return ((None, None, None),)
    for item in raw:
        if not isinstance(item, Mapping):
            result.append((None, None, None))
            continue
        canonical = item.get("canonical", item.get("field", item.get("target")))
        alias = item.get("alias", item.get("name"))
        result.append((canonical, alias, item.get("confidence")))
    return tuple(result)


def _confidence(raw: Any, source: AliasSource) -> tuple[float, Optional[AliasIssue]]:
    if source != "inferred":
        return 1.0, None
    if raw is None:
        return 0.0, None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 0.0, _issue("invalid_confidence", detail="confidence must be a finite number in [0, 1]")
    value = float(raw)
    if not math.isfinite(value) or value < 0.0 or value > 1.0:
        return 0.0, _issue("invalid_confidence", detail="confidence must be a finite number in [0, 1]")
    return round(value, 3), None


def _canonical_columns(
    columns: Iterable[str],
) -> tuple[dict[str, str], set[str], tuple[AliasIssue, ...]]:
    by_key: dict[str, str] = {}
    ambiguous_keys: set[str] = set()
    issues: list[AliasIssue] = []
    for column in columns:
        text = normalize_alias_text(column)
        key = normalize_alias_key(text)
        if not key:
            continue
        previous = by_key.get(key)
        if previous is not None and previous != text:
            ambiguous_keys.add(key)
            issues.append(_issue("ambiguous_field", canonical=text,
                                 detail=f"normalized field name collides with {previous!r}"))
        else:
            by_key[key] = text
    return by_key, ambiguous_keys, tuple(issues)


def normalize_aliases(
    columns: Iterable[str],
    *,
    authored: Any = None,
    manifest: Any = None,
    inferred: Any = None,
    sensitive_columns: Iterable[str] = (),
) -> AliasResolution:
    """Normalize alias declarations and return a fail-closed resolution.

    Authored and manifest records are accepted at confidence ``1.0``.  Every
    inferred record remains a confirmation-required candidate, regardless of
    confidence.  Duplicate or conflicting normalized alias keys, unknown or
    ambiguous canonical fields, self aliases, and sensitive fields block the
    affected key; no declaration order is used to break a tie.
    """
    column_by_key, ambiguous_column_keys, column_issues = _canonical_columns(columns)
    sensitive_keys = {normalize_alias_key(item) for item in sensitive_columns
                      if normalize_alias_key(item)}
    records: list[AliasRecord] = []
    issues: list[AliasIssue] = list(column_issues)

    source_inputs: tuple[tuple[AliasSource, Any], ...] = (
        ("authored", authored), ("manifest", manifest), ("inferred", inferred),
    )
    for typed_source, raw in source_inputs:
        for raw_canonical, raw_alias, raw_confidence in _as_declarations(raw, typed_source):
            canonical = normalize_alias_text(raw_canonical)
            alias = normalize_alias_text(raw_alias)
            if not canonical or not alias:
                issues.append(_issue("invalid_declaration", alias=alias or None,
                                     canonical=canonical or None,
                                     detail="canonical and alias must be bounded text"))
                continue
            canonical_key = normalize_alias_key(canonical)
            alias_key = normalize_alias_key(alias)
            if canonical_key in ambiguous_column_keys:
                issues.append(_issue("ambiguous_field", alias=alias, canonical=canonical,
                                     detail="canonical field name is ambiguous after normalization"))
                continue
            if canonical_key not in column_by_key:
                issues.append(_issue("unknown_field", alias=alias, canonical=canonical,
                                     detail="canonical field is not present in columns"))
                continue
            canonical = column_by_key[canonical_key]
            if canonical_key in sensitive_keys or alias_key in sensitive_keys:
                issues.append(_issue("sensitive_field", alias=alias, canonical=canonical,
                                     detail="sensitive fields cannot participate in alias resolution"))
                continue
            if alias_key == canonical_key:
                issues.append(_issue("self_alias", alias=alias, canonical=canonical,
                                     detail="alias must differ from its canonical field"))
                continue
            confidence, confidence_issue = _confidence(raw_confidence, typed_source)
            if confidence_issue is not None:
                issues.append(replace(confidence_issue, alias=alias, canonical=canonical))
                continue
            records.append(AliasRecord(
                canonical=canonical,
                alias=alias,
                source=typed_source,
                confidence=confidence,
                requires_confirmation=typed_source == "inferred",
                confirmed=typed_source != "inferred",
            ))

    grouped: dict[str, list[AliasRecord]] = {}
    for record in records:
        grouped.setdefault(record.alias_key, []).append(record)
    blocked_keys: set[str] = set()
    for alias_key, candidates in grouped.items():
        if len(candidates) < 2:
            continue
        blocked_keys.add(alias_key)
        canonical_keys = {item.canonical_key for item in candidates}
        code: AliasIssueCode = "conflict" if len(canonical_keys) > 1 else "duplicate"
        detail = "alias maps to multiple canonical fields" if code == "conflict" else "alias is declared more than once"
        for item in candidates:
            issues.append(_issue(code, alias=item.alias, canonical=item.canonical, detail=detail))

    accepted = tuple(
        record for record in records
        if record.alias_key not in blocked_keys and record.confirmed
    )
    return AliasResolution(records=tuple(records), accepted=accepted, issues=tuple(issues))


def confirm_alias(
    resolution: AliasResolution,
    alias: str,
    *,
    canonical: Optional[str] = None,
) -> AliasResolution:
    """Explicitly promote one unique inferred candidate.

    This function returns a new immutable resolution.  It never edits a
    manifest or writes a confirmation anywhere.  Existing authored/manifest
    records cannot be "confirmed" a second time, which prevents a caller from
    treating an already trusted declaration as fresh user evidence.
    """
    query_key = normalize_alias_key(alias)
    if not query_key:
        raise AliasConfirmationError("alias confirmation requires a bounded alias")
    candidates = resolution.candidates_for(alias)
    if len(candidates) != 1:
        raise AliasConfirmationError("only one alias candidate can be confirmed")
    candidate = candidates[0]
    if candidate.source != "inferred" or not candidate.requires_confirmation:
        raise AliasConfirmationError("only an unconfirmed inferred candidate can be confirmed")
    if canonical is not None and normalize_alias_key(canonical) != candidate.canonical_key:
        raise AliasConfirmationError("confirmation canonical field does not match candidate")
    if any(issue.alias is not None and normalize_alias_key(issue.alias) == query_key
           for issue in resolution.issues):
        raise AliasConfirmationError("alias candidate is blocked by a validation issue")

    confirmed = replace(candidate, requires_confirmation=False, confirmed=True)
    records = tuple(confirmed if item is candidate else item for item in resolution.records)
    accepted = tuple(list(resolution.accepted) + [confirmed])
    return AliasResolution(records=records, accepted=accepted, issues=resolution.issues)


__all__ = [
    "AliasConfirmationError",
    "AliasIssue",
    "AliasRecord",
    "AliasResolution",
    "confirm_alias",
    "normalize_alias_key",
    "normalize_alias_text",
    "normalize_aliases",
]


# =============================================================================
# SOURCE SECTION: server/report_impact_graph.py
# =============================================================================
"""Pure, bounded report/card dependency analysis.

This module is deliberately a read-only seam.  It accepts report declarations
that are already in memory and returns a deterministic projection of cards,
datasets, field dependencies, and advisory relationships.  It never reads a
manifest from disk, opens the data engine, performs HTTP, or returns an action
that could mutate a report.

The dependency vocabulary follows the vendored Flint contract:

* ``refs/flint/src/core/field-semantics.ts:138-150`` separates a field's
  intrinsic identity from channel-specific visual decisions.
* ``refs/flint/src/core/field-semantics.ts:473-488`` makes aggregation a
  field-semantic default (additive/signed-additive → sum, intensive → average).
* ``refs/flint/src/core/type-registry.ts:103-122`` is the authoritative split
  between additive and intensive measure types used for the small aggregation
  hint in this projection.
* ``refs/flint/src/core/aggregate.ts:7-20,37-40`` keeps aggregation at the
  caller-owned encoding boundary; this graph therefore records dependencies
  and never invents joins, aggregation results, or writes.

The public interface is :func:`build_report_impact_graph`.  ``reports`` may
contain either a report manifest directly or ``{"manifest": manifest}``.
The optional ``target`` is ``{"reportId": ..., "chartId": ...}``; when it is
present, ``affectedCards`` and ``affected`` edges are relative to that card.
``limits`` is a mapping of positive integer caps.  Unknown or malformed
declarations become bounded diagnostics instead of exceptions, so a caller can
show an incomplete graph honestly.
"""

from collections.abc import Iterable, Mapping
from typing import Any


DEFAULT_LIMITS = {
    "maxReports": 32,
    "maxDatasets": 128,
    "maxCards": 256,
    "maxFieldsPerCard": 32,
    "maxRelations": 128,
    "maxEdges": 512,
    "maxDiagnostics": 64,
}

_SENSITIVE_MARKER = "[sensitive field]"

# These are the concrete Flint registry families represented in the vendored
# registry.  The graph does not infer a type from a field name; it only uses a
# declared semantic type to expose the same useful aggregation hint.
_ADDITIVE_TYPES = {"Amount", "Quantity", "Duration", "Profit", "Count", "Number"}
_INTENSIVE_TYPES = {
    "Price", "Temperature", "Percentage", "PercentageChange", "Sentiment",
    "Correlation", "Score",
}


def build_report_impact_graph(
    reports: Iterable[Mapping[str, Any]],
    *,
    target: Mapping[str, Any] | None = None,
    limits: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a bounded, deterministic dependency graph from declarations.

    A sibling is another card that uses the same declared dataset and at least
    one non-sensitive, known field (or a sensitive field represented only by a
    redacted marker).  An affected card is a sibling of ``target``.  Cards in
    another dataset are never siblings merely because a field has the same
    name; they are emitted as explicit ``cross_dataset_candidate`` edges with
    ``joinInferred=False``.
    """
    caps = _limits(limits)
    diagnostics: dict[str, list[dict[str, Any]]] = {
        "unknown": [],
        "sensitive": [],
        "duplicates": [],
    }
    truncation = {
        "truncated": False,
        "reasons": [],
        "limits": dict(caps),
        "omitted": {"reports": 0, "datasets": 0, "cards": 0, "fields": 0,
                     "relations": 0, "edges": 0},
    }

    normalized_reports: list[dict[str, Any]] = []
    seen_reports: set[str] = set()
    # The input contract is itself bounded.  Read that bounded declaration set
    # before applying caps so stable sorting does not depend on report arrival
    # order (the returned graph remains bounded even when a caller tightens a
    # cap).
    for index, raw_report in enumerate(reports or ()):
        if not isinstance(raw_report, Mapping):
            _diagnostic(diagnostics, "unknown", caps, {
                "kind": "report", "reason": "report_declaration_not_object", "index": index,
            })
            continue
        report = _normalize_report(raw_report, index, diagnostics, caps)
        report_id = report["reportId"]
        if report_id in seen_reports:
            _diagnostic(diagnostics, "duplicates", caps, {
                "kind": "report", "reportId": report_id, "reason": "duplicate_report_id",
            })
            continue
        seen_reports.add(report_id)
        normalized_reports.append(report)

    normalized_reports.sort(key=lambda item: item["reportId"])
    if len(normalized_reports) > caps["maxReports"]:
        truncation["omitted"]["reports"] = len(normalized_reports) - caps["maxReports"]
        _truncate(truncation, "maxReports")
        normalized_reports = normalized_reports[:caps["maxReports"]]

    all_cards: list[dict[str, Any]] = []
    datasets: dict[tuple[str, str], dict[str, Any]] = {}
    seen_cards: set[tuple[str, str]] = set()
    for report in normalized_reports:
        for dataset in report["datasets"]:
            key = (report["reportId"], dataset["datasetId"])
            if key in datasets:
                continue
            if len(datasets) >= caps["maxDatasets"]:
                _truncate(truncation, "maxDatasets")
                truncation["omitted"]["datasets"] += 1
                continue
            datasets[key] = dataset
        for card in report["cards"]:
            card_key = (report["reportId"], card["chartId"])
            if card_key in seen_cards:
                _diagnostic(diagnostics, "duplicates", caps, {
                    "kind": "card", "reportId": report["reportId"],
                    "chartId": card["chartId"], "reason": "duplicate_card_id",
                })
                continue
            if len(all_cards) >= caps["maxCards"]:
                _truncate(truncation, "maxCards")
                truncation["omitted"]["cards"] += 1
                continue
            seen_cards.add(card_key)
            all_cards.append(card)

    all_cards.sort(key=lambda item: (item["reportId"], item["chartId"]))
    dataset_by_key = datasets
    for card in all_cards:
        dataset = dataset_by_key.get((card["reportId"], card["datasetId"]))
        _resolve_card_dependencies(card, dataset, diagnostics, caps, truncation)

    target_key = _target_key(target)
    target_card = next((card for card in all_cards
                        if (card["reportId"], card["chartId"]) == target_key), None)
    if target_key and target_card is None:
        _diagnostic(diagnostics, "unknown", caps, {
            "kind": "target", "reportId": target_key[0], "chartId": target_key[1],
            "reason": "target_card_not_found",
        })

    nodes: list[dict[str, Any]] = []
    for report in normalized_reports:
        nodes.append({"id": f"report:{report['reportId']}", "kind": "report",
                      "reportId": report["reportId"], "title": report["title"]})
    for key, dataset in sorted(datasets.items()):
        report_id, dataset_id = key
        nodes.append({"id": f"dataset:{report_id}:{dataset_id}", "kind": "dataset",
                      "reportId": report_id, "datasetId": dataset_id,
                      "label": dataset["label"],
                      "declaredFieldCount": len(dataset["fields"]),
                      "sensitiveFieldCount": len(dataset["sensitiveFields"])})
    for card in all_cards:
        nodes.append(_card_node(card))
    nodes.sort(key=lambda item: (item["kind"], item["id"]))

    edges: list[dict[str, Any]] = []
    # A report can have multiple datasets; use the normalized declaration list.
    seen_report_datasets: set[tuple[str, str]] = set()
    for report in normalized_reports:
        report_node = f"report:{report['reportId']}"
        for dataset in report["datasets"]:
            dataset_key = (report["reportId"], dataset["datasetId"])
            if dataset_key not in datasets or dataset_key in seen_report_datasets:
                continue
            seen_report_datasets.add(dataset_key)
            edges.append({"kind": "report_dataset", "source": report_node,
                          "target": f"dataset:{report['reportId']}:{dataset['datasetId']}"})
    for card in all_cards:
        card_node = f"card:{card['reportId']}:{card['chartId']}"
        dataset = dataset_by_key.get((card["reportId"], card["datasetId"]))
        if dataset is not None:
            edges.append({"kind": "card_dataset", "source": card_node,
                          "target": f"dataset:{card['reportId']}:{card['datasetId']}"})
        for field in card["fields"]:
            edge = {"kind": "card_field", "source": card_node,
                    "field": field["displayName"], "status": field["status"],
                    "roles": field["roles"]}
            if field["aggregation"] is not None:
                edge["aggregation"] = field["aggregation"]
            if field["sensitive"]:
                edge["sensitive"] = True
            edges.append(edge)

    sibling_cards: list[dict[str, Any]] = []
    affected_cards: list[dict[str, Any]] = []
    cross_dataset_candidates: list[dict[str, Any]] = []
    for left_index, left in enumerate(all_cards):
        for right in all_cards[left_index + 1:]:
            shared, sensitive_shared = _shared_fields(left, right)
            if left["datasetId"] == right["datasetId"] and left["datasetId"] and (shared or sensitive_shared):
                relation = _relation_card(left, right, shared, sensitive_shared)
                sibling_cards.append(relation)
                edges.append({"kind": "sibling", "source": _card_id(left),
                              "target": _card_id(right),
                              "sharedFields": shared,
                              "sensitiveFieldCount": len(sensitive_shared),
                              "crossReport": left["reportId"] != right["reportId"]})
                if target_card is not None:
                    if _same_card(left, target_card) and not _same_card(right, target_card):
                        affected_cards.append(_affected_relation(target_card, right, shared, sensitive_shared))
                        edges.append(_affected_edge(target_card, right, shared, sensitive_shared))
                    elif _same_card(right, target_card) and not _same_card(left, target_card):
                        affected_cards.append(_affected_relation(target_card, left, shared, sensitive_shared))
                        edges.append(_affected_edge(target_card, left, shared, sensitive_shared))
            elif left["datasetId"] and right["datasetId"] and left["datasetId"] != right["datasetId"]:
                same_names, sensitive_names = _same_field_names(left, right)
                if same_names or sensitive_names:
                    candidate = _relation_card(left, right, same_names, sensitive_names)
                    candidate["reason"] = "same_field_name_only"
                    candidate["joinInferred"] = False
                    cross_dataset_candidates.append(candidate)
                    edges.append({"kind": "cross_dataset_candidate", "source": _card_id(left),
                                  "target": _card_id(right), "sameFields": same_names,
                                  "sensitiveFieldCount": len(sensitive_names),
                                  "joinInferred": False})

    edges.sort(key=_edge_sort_key)
    if len(edges) > caps["maxEdges"]:
        truncation["omitted"]["edges"] = len(edges) - caps["maxEdges"]
        _truncate(truncation, "maxEdges")
        edges = edges[:caps["maxEdges"]]

    sibling_cards.sort(key=_relation_sort_key)
    affected_cards.sort(key=_relation_sort_key)
    cross_dataset_candidates.sort(key=_relation_sort_key)
    if target_card is not None:
        target_ref = _card_ref(target_card)
        sibling_cards = [item for item in sibling_cards
                         if item.get("source") == target_ref or item.get("target") == target_ref]
        cross_dataset_candidates = [item for item in cross_dataset_candidates
                                    if item.get("source") == target_ref or item.get("target") == target_ref]
    relation_omitted = sum(max(0, len(items) - caps["maxRelations"])
                           for items in (sibling_cards, affected_cards, cross_dataset_candidates))
    if relation_omitted:
        truncation["omitted"]["relations"] = relation_omitted
        _truncate(truncation, "maxRelations")
    return {
        "version": 1,
        "readOnly": True,
        "target": ({"reportId": target_key[0], "chartId": target_key[1]}
                    if target_key else None),
        "nodes": nodes,
        "edges": edges,
        "siblingCards": sibling_cards[:caps["maxRelations"]],
        "affectedCards": affected_cards[:caps["maxRelations"]],
        "crossDatasetCandidates": cross_dataset_candidates[:caps["maxRelations"]],
        "affectedChartCount": len(affected_cards),
        "crossDataset": bool(cross_dataset_candidates),
        "diagnostics": diagnostics,
        "truncation": truncation,
    }


# A short alias makes the seam easy to discover without creating another
# implementation or another vocabulary for callers.
build_impact_graph = build_report_impact_graph


def _limits(raw: Mapping[str, Any] | None) -> dict[str, int]:
    caps = dict(DEFAULT_LIMITS)
    for name, value in (raw or {}).items():
        if name not in caps:
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            caps[name] = number
    return caps


def _normalize_report(raw: Mapping[str, Any], index: int,
                      diagnostics: dict[str, list[dict[str, Any]]], caps: Mapping[str, int]) -> dict[str, Any]:
    manifest = raw.get("manifest") if isinstance(raw.get("manifest"), Mapping) else raw
    report_id = _text(raw.get("reportId") or raw.get("id") or manifest.get("reportId"))
    if not report_id:
        report_id = f"unknown-report-{index + 1}"
        _diagnostic(diagnostics, "unknown", caps, {"kind": "report", "reportId": report_id,
                                                    "reason": "missing_report_id"})
    title = _text(raw.get("title") or manifest.get("title")) or report_id
    dataset_declarations = manifest.get("datasets") if isinstance(manifest, Mapping) else []
    datasets: list[dict[str, Any]] = []
    if not isinstance(dataset_declarations, list):
        dataset_declarations = []
        _diagnostic(diagnostics, "unknown", caps, {"kind": "report", "reportId": report_id,
                                                    "reason": "datasets_declaration_missing_or_invalid"})
    for item in dataset_declarations:
        if not isinstance(item, Mapping):
            continue
        dataset_id = _text(item.get("id") or item.get("datasetId"))
        if not dataset_id:
            _diagnostic(diagnostics, "unknown", caps, {"kind": "dataset", "reportId": report_id,
                                                        "reason": "missing_dataset_id"})
            continue
        datasets.append(_normalize_dataset(item, dataset_id))
    datasets.sort(key=lambda item: item["datasetId"])

    card_declarations = manifest.get("charts") if isinstance(manifest, Mapping) else []
    if not isinstance(card_declarations, list):
        card_declarations = manifest.get("cards") if isinstance(manifest, Mapping) else []
    if not isinstance(card_declarations, list):
        card_declarations = []
        _diagnostic(diagnostics, "unknown", caps, {"kind": "report", "reportId": report_id,
                                                    "reason": "cards_declaration_missing_or_invalid"})
    cards = [_normalize_card(item, report_id, index)
             for index, item in enumerate(card_declarations) if isinstance(item, Mapping)]
    cards.sort(key=lambda item: item["chartId"])
    return {"reportId": report_id, "title": title, "datasets": datasets, "cards": cards}


def _normalize_dataset(raw: Mapping[str, Any], dataset_id: str) -> dict[str, Any]:
    fields: set[str] = set()
    raw_columns = raw.get("columns")
    for column in raw_columns if isinstance(raw_columns, list) else []:
        if isinstance(column, Mapping):
            name = _text(column.get("name"))
        else:
            name = _text(column)
        if name:
            fields.add(name)
    roles = raw.get("columnRoles")
    if isinstance(roles, Mapping):
        fields.update(str(key) for key in roles if _text(key))
    measure_types = raw.get("measureTypes")
    if isinstance(measure_types, Mapping):
        fields.update(str(key) for key in measure_types if _text(key))
    derived = raw.get("derived")
    derived_columns: dict[str, Mapping[str, Any]] = {}
    derived_measures: dict[str, Mapping[str, Any]] = {}
    if isinstance(derived, Mapping):
        raw_derived_columns = derived.get("columns")
        for item in raw_derived_columns if isinstance(raw_derived_columns, list) else []:
            if isinstance(item, Mapping) and _text(item.get("name")):
                name = _text(item.get("name"))
                fields.add(name)
                derived_columns[name] = item
        raw_derived_measures = derived.get("measures")
        for item in raw_derived_measures if isinstance(raw_derived_measures, list) else []:
            if isinstance(item, Mapping) and _text(item.get("name")):
                name = _text(item.get("name"))
                fields.add(name)
                derived_measures[name] = item
    sensitive = set()
    privacy = raw.get("privacy")
    if isinstance(privacy, Mapping):
        raw_sensitive = privacy.get("sensitiveColumns")
        if isinstance(raw_sensitive, list):
            sensitive.update(str(value) for value in raw_sensitive if _text(value))
    raw_sensitive = raw.get("sensitiveColumns")
    if isinstance(raw_sensitive, list):
        sensitive.update(str(value) for value in raw_sensitive if _text(value))
    return {
        "datasetId": dataset_id,
        "label": _text(raw.get("label") or raw.get("name")) or dataset_id,
        "fields": fields,
        "sensitiveFields": sensitive,
        "measureTypes": measure_types if isinstance(measure_types, Mapping) else {},
        "aggregations": raw.get("aggregations") if isinstance(raw.get("aggregations"), Mapping) else {},
        "derivedColumns": derived_columns,
        "derivedMeasures": derived_measures,
    }


def _normalize_card(raw: Mapping[str, Any], report_id: str, index: int) -> dict[str, Any]:
    data = raw.get("editor", {}).get("data") if isinstance(raw.get("editor"), Mapping) else None
    if not isinstance(data, Mapping):
        data = raw.get("data") if isinstance(raw.get("data"), Mapping) else raw
    chart_id = _text(raw.get("id") or raw.get("chartId")) or f"unknown-card-{index + 1}"
    if not _text(raw.get("id") or raw.get("chartId")):
        # Keep the missing declaration visible in diagnostics after the card
        # has acquired a deterministic identity for graph construction.
        missing_id = True
    else:
        missing_id = False
    return {
        "reportId": report_id,
        "chartId": chart_id,
        "title": _text(raw.get("title")) or chart_id,
        "datasetId": _text(data.get("datasetId") or raw.get("datasetId")),
        "data": data,
        "fields": [],
        "missingId": missing_id,
    }


def _resolve_card_dependencies(card: dict[str, Any], dataset: Mapping[str, Any] | None,
                               diagnostics: dict[str, list[dict[str, Any]]], caps: Mapping[str, int],
                               truncation: dict[str, Any]) -> None:
    if card["missingId"]:
        _diagnostic(diagnostics, "unknown", caps, {"kind": "card", "reportId": card["reportId"],
                                                    "chartId": card["chartId"], "reason": "missing_chart_id"})
    if not card["datasetId"]:
        _diagnostic(diagnostics, "unknown", caps, {"kind": "card", "reportId": card["reportId"],
                                                    "chartId": card["chartId"], "reason": "missing_dataset_id"})
    if dataset is None:
        _diagnostic(diagnostics, "unknown", caps, {"kind": "card", "reportId": card["reportId"],
                                                    "chartId": card["chartId"], "reason": "dataset_declaration_missing"})

    refs = _card_field_refs(card["data"])
    if dataset is not None:
        derived_refs = set(dataset.get("derivedMeasures", {})) | set(dataset.get("derivedColumns", {}))
        for name in list(refs):
            if name in dataset.get("derivedMeasures", {}):
                refs.update(_derived_measure_refs(name, dataset, set()))
            elif name in dataset.get("derivedColumns", {}):
                refs.update(_derived_column_refs(name, dataset, set()))
        known = set(dataset.get("fields", set()))
        sensitive = set(dataset.get("sensitiveFields", set()))
    else:
        derived_refs, known, sensitive = set(), set(), set()

    names = sorted(refs)
    if len(names) > caps["maxFieldsPerCard"]:
        truncation["omitted"]["fields"] += len(names) - caps["maxFieldsPerCard"]
        _truncate(truncation, "maxFieldsPerCard")
        names = names[:caps["maxFieldsPerCard"]]
    fields = []
    for name in names:
        is_sensitive = name in sensitive
        is_known = name in known
        status = "sensitive" if is_sensitive else ("known" if is_known else "unknown")
        if not is_known and not is_sensitive:
            _diagnostic(diagnostics, "unknown", caps, {"kind": "field", "reportId": card["reportId"],
                                                        "chartId": card["chartId"], "field": name,
                                                        "reason": "field_not_declared_by_dataset"})
        if is_sensitive:
            _diagnostic(diagnostics, "sensitive", caps, {"kind": "field", "reportId": card["reportId"],
                                                          "chartId": card["chartId"],
                                                          "field": _SENSITIVE_MARKER,
                                                          "reason": "field_redacted"})
        roles = _field_roles(card["data"], name)
        fields.append({
            "name": name,
            "displayName": _SENSITIVE_MARKER if is_sensitive else name,
            "status": status,
            "sensitive": is_sensitive,
            "roles": roles,
            "aggregation": _field_aggregation(dataset, name, roles),
            "derived": name in derived_refs,
        })
    card["fields"] = fields


def _card_field_refs(data: Mapping[str, Any]) -> set[str]:
    refs: set[str] = set()

    def add(value: Any) -> None:
        if isinstance(value, str) and value.strip():
            refs.add(value.strip())
        elif isinstance(value, list):
            for item in value:
                add(item)
        elif isinstance(value, Mapping):
            for key in ("column", "field", "name"):
                add(value.get(key))

    add(data.get("dimension"))
    for key in ("dimensions", "measures"):
        add(data.get(key))
    roles = data.get("roles")
    if isinstance(roles, Mapping):
        for value in roles.values():
            add(value)
    for key in ("breakdown", "timeField"):
        add(data.get(key))
    for key in ("filter", "filters", "columnFilters", "dimScopes", "filterScopes"):
        _add_filter_refs(data.get(key), refs)
    pivot = data.get("pivot")
    if isinstance(pivot, Mapping):
        for key in ("rows", "columns", "values", "measures"):
            add(pivot.get(key))
    return refs


def _add_filter_refs(value: Any, refs: set[str]) -> None:
    if isinstance(value, Mapping):
        for key in ("column", "field", "dimensionKey"):
            if isinstance(value.get(key), str) and value[key].strip():
                refs.add(value[key].strip())
        for nested in value.values():
            if isinstance(nested, (Mapping, list)):
                _add_filter_refs(nested, refs)
    elif isinstance(value, list):
        for item in value:
            _add_filter_refs(item, refs)


def _field_roles(data: Mapping[str, Any], name: str) -> list[str]:
    roles: set[str] = set()
    if data.get("dimension") == name or name in _string_values(data.get("dimensions")):
        roles.add("dimension")
    if name in _string_values(data.get("measures")):
        roles.add("measure")
    if data.get("breakdown") == name or ((data.get("roles") or {}).get("breakdown") == name):
        roles.add("breakdown")
    for key in ("filter", "filters", "columnFilters", "dimScopes", "filterScopes"):
        if _contains_filter_field(data.get(key), name):
            roles.add("filter")
    pivot = data.get("pivot")
    if isinstance(pivot, Mapping):
        if name in _string_values(pivot.get("rows")):
            roles.add("pivot_row")
        if name in _string_values(pivot.get("columns")):
            roles.add("pivot_column")
        if name in _string_values(pivot.get("values")) or name in _string_values(pivot.get("measures")):
            roles.add("pivot_value")
    return sorted(roles) or ["reference"]


def _contains_filter_field(value: Any, name: str) -> bool:
    if isinstance(value, Mapping):
        if any(value.get(key) == name for key in ("column", "field", "dimensionKey")):
            return True
        return any(_contains_filter_field(item, name) for item in value.values())
    if isinstance(value, list):
        return any(_contains_filter_field(item, name) for item in value)
    return False


def _string_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return []


def _field_aggregation(dataset: Mapping[str, Any] | None, name: str, roles: list[str]) -> str | None:
    if dataset is None or "measure" not in roles:
        return None
    aggregations = dataset.get("aggregations")
    measure_types = dataset.get("measureTypes")
    declared = aggregations.get(name) if isinstance(aggregations, Mapping) else None
    info = measure_types.get(name) if isinstance(measure_types, Mapping) else None
    if isinstance(info, Mapping):
        declared = declared or info.get("aggregation")
        semantic = info.get("type") or info.get("semantic")
    else:
        semantic = info
    if declared:
        return str(declared)
    if semantic in _ADDITIVE_TYPES:
        return "sum"
    if semantic in _INTENSIVE_TYPES:
        return "average"
    return None


def _derived_measure_refs(name: str, dataset: Mapping[str, Any], seen: set[str]) -> set[str]:
    if name in seen:
        return set()
    seen.add(name)
    node = dataset.get("derivedMeasures", {}).get(name)
    if not isinstance(node, Mapping):
        return set()
    refs: set[str] = set()
    kind = node.get("kind")
    if kind == "formula":
        refs.update(_expr_refs(node.get("expr")))
    else:
        inp = node.get("input")
        if isinstance(inp, Mapping):
            refs.update(value for key in ("ref", "col") if isinstance((value := inp.get(key)), str))
        elif isinstance(inp, str):
            refs.add(inp)
        if isinstance(node.get("col"), str):
            refs.add(node["col"])
        if isinstance(node.get("parentField"), str):
            refs.add(node["parentField"])
        if isinstance(node.get("timeField"), str):
            refs.add(node["timeField"])
        for item in node.get("filters") or []:
            if isinstance(item, Mapping) and isinstance(item.get("column"), str):
                refs.add(item["column"])
    derived_names = set(dataset.get("derivedMeasures", {}))
    out = set(refs)
    for ref in sorted(refs & derived_names):
        out.update(_derived_measure_refs(ref, dataset, seen))
    return out


def _expr_refs(expr: Any) -> set[str]:
    if not isinstance(expr, Mapping):
        return set()
    refs: set[str] = set()
    if isinstance(expr.get("col"), str):
        refs.add(expr["col"])
    if isinstance(expr.get("ref"), str):
        refs.add(expr["ref"])
    refs.update(_expr_refs(expr.get("left")))
    refs.update(_expr_refs(expr.get("right")))
    return refs


def _derived_column_refs(name: str, dataset: Mapping[str, Any], seen: set[str]) -> set[str]:
    if name in seen:
        return set()
    seen.add(name)
    node = dataset.get("derivedColumns", {}).get(name)
    if not isinstance(node, Mapping):
        return set()
    refs = {value for key in ("left", "right")
            if isinstance((value := node.get(key)), str)}
    derived_names = set(dataset.get("derivedColumns", {}))
    out = set(refs)
    for ref in sorted(refs & derived_names):
        out.update(_derived_column_refs(ref, dataset, seen))
    return out


def _shared_fields(left: Mapping[str, Any], right: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    left_by_name = {item["name"]: item for item in left["fields"]}
    right_by_name = {item["name"]: item for item in right["fields"]}
    shared, sensitive = [], []
    for name in sorted(set(left_by_name) & set(right_by_name)):
        a, b = left_by_name[name], right_by_name[name]
        if a["status"] == "unknown" or b["status"] == "unknown":
            continue
        if a["sensitive"] or b["sensitive"]:
            sensitive.append(_SENSITIVE_MARKER)
        else:
            shared.append(name)
    # Keep the redacted count faithful when two different sensitive fields
    # overlap; their names remain absent from every returned object.
    return list(dict.fromkeys(shared)), sensitive


def _same_field_names(left: Mapping[str, Any], right: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    # Cross-dataset same-name matching is deliberately weaker than dependency
    # matching.  Unknown fields are excluded and the caller receives an
    # explicit no-join edge instead of a sibling/affected relation.
    return _shared_fields(left, right)


def _relation_card(left: Mapping[str, Any], right: Mapping[str, Any],
                   shared: list[str], sensitive: list[str]) -> dict[str, Any]:
    return {
        "source": _card_ref(left), "target": _card_ref(right),
        "sourceReportId": left["reportId"], "sourceChartId": left["chartId"],
        "targetReportId": right["reportId"], "targetChartId": right["chartId"],
        "sourceTitle": left["title"], "targetTitle": right["title"],
        "datasetId": left["datasetId"] if left["datasetId"] == right["datasetId"] else None,
        "sharedFields": shared,
        "sensitiveFieldCount": len(sensitive),
    }


def _affected_relation(target: Mapping[str, Any], card: Mapping[str, Any],
                       shared: list[str], sensitive: list[str]) -> dict[str, Any]:
    return {"source": _card_ref(target), "target": _card_ref(card),
            "reportId": card["reportId"], "chartId": card["chartId"],
            "title": card["title"], "datasetId": card["datasetId"],
            "sharedFields": shared, "sensitiveFieldCount": len(sensitive)}


def _affected_edge(target: Mapping[str, Any], card: Mapping[str, Any],
                   shared: list[str], sensitive: list[str]) -> dict[str, Any]:
    return {"kind": "affected", "source": _card_id(target), "target": _card_id(card),
            "sharedFields": shared, "sensitiveFieldCount": len(sensitive),
            "reason": "same_dataset_shared_field"}


def _card_node(card: Mapping[str, Any]) -> dict[str, Any]:
    return {"id": _card_id(card), "kind": "card", "reportId": card["reportId"],
            "chartId": card["chartId"], "title": card["title"],
            "datasetId": card["datasetId"],
            "fields": [{"name": item["displayName"], "status": item["status"],
                        "roles": item["roles"], "sensitive": item["sensitive"],
                        **({"aggregation": item["aggregation"]}
                           if item["aggregation"] is not None else {})}
                       for item in card["fields"]],
            "fieldCount": len(card["fields"]),
            "unknownFieldCount": sum(item["status"] == "unknown" for item in card["fields"]),
            "sensitiveFieldCount": sum(item["sensitive"] for item in card["fields"])}


def _target_key(target: Mapping[str, Any] | None) -> tuple[str, str] | None:
    if not isinstance(target, Mapping):
        return None
    report_id, chart_id = _text(target.get("reportId")), _text(target.get("chartId"))
    return (report_id, chart_id) if report_id and chart_id else None


def _card_id(card: Mapping[str, Any]) -> str:
    return f"card:{card['reportId']}:{card['chartId']}"


def _card_ref(card: Mapping[str, Any]) -> str:
    return f"{card['reportId']}:{card['chartId']}"


def _same_card(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return left["reportId"] == right["reportId"] and left["chartId"] == right["chartId"]


def _edge_sort_key(edge: Mapping[str, Any]) -> tuple[str, str, str, str]:
    fields = ",".join(edge.get("sharedFields") or edge.get("sameFields") or [])
    return (str(edge.get("kind", "")), str(edge.get("source", "")),
            str(edge.get("target", "")), fields)


def _relation_sort_key(item: Mapping[str, Any]) -> tuple[str, str, str]:
    return (str(item.get("source", "")), str(item.get("target", "")),
            ",".join(item.get("sharedFields") or []))


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _truncate(state: dict[str, Any], reason: str) -> None:
    state["truncated"] = True
    if reason not in state["reasons"]:
        state["reasons"].append(reason)
        state["reasons"].sort()


def _diagnostic(bucket: dict[str, list[dict[str, Any]]], name: str,
                caps: Mapping[str, int], value: dict[str, Any]) -> None:
    if len(bucket[name]) < caps["maxDiagnostics"]:
        bucket[name].append(value)
        bucket[name].sort(key=lambda item: tuple(str(item.get(key, ""))
                                                 for key in ("kind", "reportId", "chartId", "field", "reason")))


# =============================================================================
# SOURCE SECTION: server/psi_domain.py
# =============================================================================
"""Pure PSI domain rules: balance identity, weeks-of-supply, stockout classes.

PSI is the flagship report domain, and its readers constantly ask three
questions the model must never free-style numbers for (evidence discipline):

* 「期末库存 = 期初库存 + Sell In − Sell Out」 holds per period — the FIRST
  period has no declared opening stock, so it is honestly ``unverifiable``
  instead of inventing a zero opening;
* 在库周数 (weeks of supply) = inventory ÷ weekly sell-out, where the weekly
  rate is the period sell-out spread over the WEEKS IN THAT PERIOD (a month is
  52/12 weeks — a month is not 4 weeks);
* 断销有货/有货无销 are two explicit bucket classes over member × period.

This section is deliberately PURE: it takes aligned per-period numbers and
returns plain dicts. Engine access, sensitive-column policy and aggregation
stay in ``agent_tool_runner`` on the existing aggregate seam
(``refs/flint/src/core/aggregate.ts:7-20``). Calendar positions reuse the one
shared ``date_parse``-backed ``position`` helper from the business-calendar
section above — no second period arithmetic.
"""

# Weeks contained in one period, per grain. Grounded in the window-grain rule
# that 52 weeks ≠ 1 year is handled explicitly elsewhere; months use 52/12
# (average weeks per month), never a rounded 4.
PSI_GRAIN_WEEKS = {"day": 1.0 / 7.0, "week": 1.0, "month": 52.0 / 12.0,
                   "quarter": 13.0, "year": 52.0}

_PSI_GRAINS = frozenset(PSI_GRAIN_WEEKS)


class PsiDomainError(ValueError):
    """A PSI domain rule received an out-of-contract input."""


def psi_normalize_grain(grain_name: Any) -> str:
    """Validate an explicit (or inferred) PSI time grain."""
    if grain_name not in _PSI_GRAINS:
        raise PsiDomainError(
            f"PSI 工具只支持 {'/'.join(sorted(_PSI_GRAINS))} 粒度；收到 {grain_name!r}")
    return grain_name


def psi_balance_periods(series: list[Mapping[str, Any]], *, grain: str | None = None,
                        tolerance: float = 1e-6, abs_tolerance: float = 0.5
                        ) -> dict[str, Any]:
    """Reconcile 「期末库存 = 期初 + Sell In − Sell Out」 over ordered periods.

    ``series`` must already be in calendar order with aligned numeric fields
    (``period``/``sellIn``/``sellOut``/``inventory``).  The first period has no
    declared opening stock → ``status: "unverifiable"`` with ``expected`` and
    ``delta`` left ``null``; a period with no observed inventory is likewise
    unverifiable.  The opening of period N is the last OBSERVED closing stock,
    so a data gap degrades coverage honestly instead of compounding a guess.

    XC-02 tolerance tiers (2026-10-24): a period is ``balanced`` when
    ``abs(delta) <= max(tolerance * scale, abs_tolerance)`` with
    ``scale = max(|期初|, |期末|, |流入|, |流出|)`` — the relative tier keeps the
    legacy ``1e-6`` default, the absolute tier (default ``0.5``, half a unit of
    integer stock) catches sub-unit rounding jitter that a pure relative
    tolerance would misreport as ``mismatched``.  The applied pair is reported
    as ``toleranceUsed: {relative, absolute}`` so a caller can audit the
    verdict rule, not just the verdict.
    """
    records: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    opening: float | None = None
    previous_position: int | None = None
    previous_period: Any = None
    for item in series:
        period = item.get("period")
        sell_in = item.get("sellIn")
        sell_out = item.get("sellOut")
        inventory = item.get("inventory")
        record: dict[str, Any] = {"period": period, "sellIn": sell_in,
                                   "sellOut": sell_out, "inventory": inventory}
        if opening is None or inventory is None:
            record.update({"opening": None, "expected": None, "observed": inventory,
                            "delta": None, "deltaRate": None, "status": "unverifiable"})
        else:
            expected = opening + (sell_in or 0.0) - (sell_out or 0.0)
            delta = inventory - expected
            scale = max(abs(opening), abs(inventory),
                        abs(sell_in or 0.0), abs(sell_out or 0.0))
            epsilon = max(tolerance * scale, abs_tolerance)
            record.update({
                "opening": opening, "expected": expected, "observed": inventory,
                "delta": delta,
                "deltaRate": (round(delta / abs(expected), 6) if expected else None),
                "status": "balanced" if abs(delta) <= epsilon else "mismatched",
            })
        records.append(record)
        if inventory is not None:
            opening = float(inventory)
        if grain:
            pos = position(period, grain)
            if pos is not None and previous_position is not None and pos - previous_position > 1:
                gaps.append({"after": previous_period, "resumedAt": period,
                             "missingPeriods": pos - previous_position - 1})
            if pos is not None:
                previous_position, previous_period = pos, period
    checked = [r for r in records if r["status"] != "unverifiable"]
    summary: dict[str, Any] = {
        "periods": len(records),
        "unverifiable": len(records) - len(checked),
        "balanced": sum(1 for r in checked if r["status"] == "balanced"),
        "mismatched": sum(1 for r in checked if r["status"] == "mismatched"),
    }
    if checked:
        summary["maxAbsDelta"] = max(abs(r["delta"]) for r in checked)
    return {"records": records, "gaps": gaps, "summary": summary,
            "toleranceUsed": {"relative": tolerance, "absolute": abs_tolerance}}


def psi_weeks_of_supply(series: list[Mapping[str, Any]], *, grain: str,
                        window: int = 3) -> dict[str, Any]:
    """在库周数 per period plus latest-vs-window-average comparison.

    weeks = inventory ÷ (period sell-out ÷ weeks-in-period).  A period with
    zero/missing sell-out has NO weeks-of-supply value (``null``) — inventing
    ∞ or 0 would both be a fabricated number.  ``window`` averages the most
    recent N computable periods (bounded 1-12 by the tool schema).
    """
    weeks_in_period = PSI_GRAIN_WEEKS[psi_normalize_grain(grain)]
    records: list[dict[str, Any]] = []
    for item in series:
        inventory = item.get("inventory")
        sell_out = item.get("sellOut")
        weekly = sell_out / weeks_in_period if isinstance(sell_out, (int, float)) else None
        weeks = (inventory / weekly) if (weekly and weekly > 0
                                         and isinstance(inventory, (int, float))) else None
        records.append({
            "period": item.get("period"), "inventory": inventory, "sellOut": sell_out,
            "weeklySellOut": round(weekly, 6) if weekly is not None else None,
            "weeksOfSupply": round(weeks, 6) if weeks is not None else None,
        })
    computable = [r["weeksOfSupply"] for r in records if r["weeksOfSupply"] is not None]
    span = max(1, min(int(window or 3), 12))
    window_values = computable[-span:]
    average = sum(window_values) / len(window_values) if window_values else None
    latest = records[-1] if records else None
    comparison: dict[str, Any] = {"window": len(window_values), "average": average,
                                   "latestWeeks": (latest or {}).get("weeksOfSupply")}
    if average and comparison["latestWeeks"] is not None:
        comparison["delta"] = round(comparison["latestWeeks"] - average, 6)
        comparison["deltaRate"] = round((comparison["latestWeeks"] - average) / average, 6)
    return {"periods": records, "latest": latest, "average": comparison,
            "grainWeeks": weeks_in_period}


def psi_stockout_classes(rows: list[Mapping[str, Any]], *, top_members: int = 20,
                         granularity: str = "sum"
                         ) -> dict[str, Any]:
    """Classify member × period buckets into the two stockout directions.

    ``rows`` are aligned ``{member, period, sellOut, inventory}`` buckets:

    * ``soldNoStock`` — Sell Out > 0 且库存 ≤ 0/缺失（有销无货，断销风险）；
    * ``stockedNoSales`` — 库存 > 0 且 Sell Out ≤ 0（有货无销，滞销风险）。

    ``granularity`` (XC-02, 2026-10-24) selects the judgment unit:

    * ``"sum"`` (default, legacy) — each row is an already SUM-aggregated
      member × period bucket; positive and negative inventory net out inside
      the group before the verdict.  Output shape is unchanged.
    * ``"row"`` — each row is an independent raw-table observation; hits are
      counted per row (no netting), so a group carrying ``+5/−5`` rows reports
      hits where the SUM view sees a healthy zero.  Row mode adds a per-class
      ``rowRatio`` over all scanned rows and bounded hit ``samples`` (≤50, with
      a truncation flag) so the verdict stays auditable; member ``periods``
      become distinct-period lists.

    Output is bounded: per-member period lists are capped and the member list
    is a ranked top-N (by hit count, then name) — never the full cross product.
    """
    if granularity not in ("sum", "row"):
        raise PsiDomainError(f"stockout granularity must be 'sum' or 'row'; got {granularity!r}")
    row_mode = granularity == "row"
    max_periods = 12
    max_samples = 50
    classes = {
        "soldNoStock": {"label": "有销无货（Sell Out>0 且库存≤0/缺失）"},
        "stockedNoSales": {"label": "有货无销（库存>0 且 Sell Out≤0）"},
    }
    members: dict[str, dict[str, list]] = {key: {} for key in classes}
    totals: dict[str, dict[str, int]] = {key: {} for key in classes}
    counts = {key: 0 for key in classes}
    samples: dict[str, list] = {key: [] for key in classes}
    # Row mode only: full distinct-period count per member, so ``periodsTruncated``
    # measures DISTINCT periods even though raw rows repeat a period.
    distinct_periods: dict[str, dict[str, dict]] = {key: {} for key in classes}
    for row in rows:
        sell_out = row.get("sellOut")
        inventory = row.get("inventory")
        member = str(row.get("member"))
        hits = []
        if isinstance(sell_out, (int, float)) and sell_out > 0 and \
                (inventory is None or (isinstance(inventory, (int, float)) and inventory <= 0)):
            hits.append("soldNoStock")
        if isinstance(inventory, (int, float)) and inventory > 0 and \
                (sell_out is None or (isinstance(sell_out, (int, float)) and sell_out <= 0)):
            hits.append("stockedNoSales")
        for key in hits:
            counts[key] += 1
            totals[key][member] = totals[key].get(member, 0) + 1
            bucket = members[key].setdefault(member, [])
            if row_mode:
                period = row.get("period")
                seen = distinct_periods[key].setdefault(member, {})
                if period not in seen:
                    seen[period] = True
                    if len(bucket) < max_periods:
                        bucket.append(period)
                if len(samples[key]) < max_samples:
                    samples[key].append({"member": row.get("member"), "period": period,
                                         "sellOut": sell_out, "inventory": inventory})
            elif len(bucket) < max_periods:
                bucket.append(row.get("period"))
    out: dict[str, Any] = {"bucketCount": len(rows)}
    if row_mode:
        out["granularity"] = "row"
        out["scannedRows"] = len(rows)
    for key, meta in classes.items():
        # Rank by the TRUE hit count (kept separately — the period list is
        # capped in place and can never measure its own truncation).
        ranked = sorted(totals[key].items(), key=lambda kv: (-kv[1], kv[0]))
        cap = max(1, min(int(top_members or 20), 50))
        entry: dict[str, Any] = {
            "label": meta["label"], "bucketCount": counts[key],
            "memberCount": len(totals[key]),
            "members": [{"member": name, "bucketCount": count,
                          "periods": members[key].get(name, []),
                          "periodsTruncated": count > len(members[key].get(name, []))}
                         for name, count in ranked[:cap]],
        }
        if row_mode:
            entry["rowRatio"] = (round(counts[key] / len(rows), 6) if rows else None)
            entry["samples"] = samples[key]
            entry["samplesTruncated"] = counts[key] > len(samples[key])
            for item in entry["members"]:
                # In row mode ``count`` is raw hit ROWS; truncation of the
                # distinct-period list is measured against distinct periods.
                item["periodsTruncated"] = (
                    len(distinct_periods[key].get(item["member"], {}))
                    > len(item["periods"]))
        out[key] = entry
    return out


