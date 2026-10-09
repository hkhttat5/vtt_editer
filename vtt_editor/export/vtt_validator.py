"""Pure WebVTT timing & content validation.

This module NEVER modifies the cue objects it receives.  It inspects a list
of cues (duck-typed: ``index`` / ``start`` / ``end`` / ``text`` attributes)
and returns structured :class:`ValidationIssue` records grouped by severity.

Severity contract (by design):
* **ERRORS**   – clearly invalid data that cannot be exported safely
                 (negative start, end <= start, NaN/inf timestamps, empty text).
* **WARNINGS** – suspicious but possibly legitimate
                 (overlaps, big gaps, overlong cues, near-duplicate texts).
* **INFO**     – purely informational
                 (small gaps = normal pauses, exact duplicates, short cues).

Gaps and overlaps are *not* repaired automatically: real subtitles contain
legitimate pauses and occasionally intentional overlaps.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional

# ---------------------------------------------------------------------------
# Timestamp validation (re-uses the parser's own parsing/formatting helpers)
# ---------------------------------------------------------------------------


def validate_timestamp_seconds(seconds) -> Optional[str]:
    """Return an actionable error message if *seconds* is not a usable
    timestamp value, otherwise ``None``."""
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return f"timestamp must be a number, got {seconds!r}"
    value = float(seconds)
    if math.isnan(value):
        return "timestamp is NaN"
    if math.isinf(value):
        return "timestamp is infinite"
    if value < 0:
        return f"timestamp must be non-negative (got {value:.3f}s)"
    # beyond any sane WebVTT document (usually a unit mistake)
    if value > 99_999_999:
        return f"timestamp implausibly large ({value:.3f}s)"
    return None


def parse_user_timestamp(text: str) -> float:
    """Parse a user-entered timestamp string into seconds.

    Accepts the standard WebVTT forms ``MM:SS.mmm`` and ``HH:MM:SS.mmm``
    (comma as decimal separator tolerated), plus bare seconds like ``12.5``.
    Raises ``ValueError`` with an actionable message on bad input.
    """
    from core.vtt_parser import _TS_RE, parse_timestamp

    s = (text or "").strip()
    if not s:
        raise ValueError("Timestamp is empty. Use MM:SS.mmm or HH:MM:SS.mmm.")
    if _TS_RE.fullmatch(s):
        value = parse_timestamp(s)
    else:
        # bare seconds ("12", "12.5", "12,5")
        try:
            value = float(s.replace(",", "."))
        except ValueError:
            raise ValueError(
                f"Invalid timestamp {text!r}. Expected MM:SS.mmm or HH:MM:SS.mmm."
            ) from None
    err = validate_timestamp_seconds(value)
    if err:
        raise ValueError(err[0].upper() + err[1:] + ".")
    return value


# ---------------------------------------------------------------------------
# Issues
# ---------------------------------------------------------------------------
SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


@dataclass
class ValidationIssue:
    cue_index: Optional[int]      # primary affected cue (None => whole doc)
    related_index: Optional[int]  # second cue for pairwise issues
    severity: str                 # "error" | "warning" | "info"
    code: str                     # machine-readable issue code
    message: str                  # human-readable, actionable description

    @property
    def label(self) -> str:
        return self.severity.capitalize()


@dataclass
class ValidationSummary:
    issues: List[ValidationIssue] = field(default_factory=list)
    video_duration: Optional[float] = None

    @property
    def errors(self) -> List[ValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> List[ValidationIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def infos(self) -> List[ValidationIssue]:
        return [i for i in self.issues if i.severity == "info"]

    def counts(self):
        return len(self.errors), len(self.warnings), len(self.infos)

    def indices_with_severity(self) -> dict:
        """Map cue index -> worst severity affecting that cue."""
        out: dict = {}
        for issue in self.issues:
            for idx in (issue.cue_index, issue.related_index):
                if idx is None:
                    continue
                prev = out.get(idx)
                if prev is None or SEVERITY_ORDER[issue.severity] < SEVERITY_ORDER[prev]:
                    out[idx] = issue.severity
        return out

    def issues_for_cue(self, cue_index: int) -> List[ValidationIssue]:
        return [i for i in self.issues
                if i.cue_index == cue_index or i.related_index == cue_index]


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------
@dataclass
class ValidatorConfig:
    min_gap_info: float = 0.05        # smaller gaps are simply ignored
    gap_warning: float = 2.0          # larger gaps become warnings
    overlap_tolerance: float = 0.010  # overlaps below this are ignored
    overlap_error: float = 0.5        # overlaps above this escalate to error
    min_duration_info: float = 0.30   # shorter cues are flagged (info)
    max_duration_warning: float = 10.0
    duplicate_time_tolerance: float = 0.05
    empty_text_is_error: bool = True


DEFAULT_CONFIG = ValidatorConfig()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def validate_cues(cues,
                  video_duration: Optional[float] = None,
                  config: Optional[ValidatorConfig] = None) -> ValidationSummary:
    """Validate *cues* (a sequence with ``index``/``start``/``end``/``text``).

    Pure function: the cues are only read, never modified.  Cues are examined
    in chronological order (sorted by start time); the original list itself
    is left untouched.
    """
    cfg = config or DEFAULT_CONFIG
    issues: List[ValidationIssue] = []

    def _valid(v):
        return validate_timestamp_seconds(v) is None

    # work on a chronologically ordered view WITHOUT reordering the input
    ordered = sorted(
        range(len(cues)),
        key=lambda k: (float(getattr(cues[k], "start", 0.0) or 0.0),
                       float(getattr(cues[k], "end", 0.0) or 0.0)))

    prev_pos = None  # (cue, end) of previous cue in chronological order

    for order_i in ordered:
        cue = cues[order_i]
        idx = getattr(cue, "index", order_i)
        start = getattr(cue, "start", None)
        end = getattr(cue, "end", None)
        text = (getattr(cue, "text", "") or "").strip()

        # ---- per-cue validity ----------------------------------------
        start_err = validate_timestamp_seconds(start)
        if start_err:
            issues.append(ValidationIssue(idx, None, "error", "invalid_start",
                                          f"Cue {idx + 1}: start time invalid — {start_err}."))
        end_missing = end is None
        end_err = None if end_missing else validate_timestamp_seconds(end)
        if end_missing:
            issues.append(ValidationIssue(idx, None, "error", "missing_end",
                                          f"Cue {idx + 1}: end time is missing."))
        elif end_err:
            issues.append(ValidationIssue(idx, None, "error", "invalid_end",
                                          f"Cue {idx + 1}: end time invalid — {end_err}."))

        timing_ok = (start_err is None and not end_missing and end_err is None)
        if timing_ok and end <= start:
            issues.append(ValidationIssue(
                idx, None, "error", "end_before_start",
                f"Cue {idx + 1}: end ({end:.3f}s) must be strictly greater than "
                f"start ({start:.3f}s)."))
            timing_ok = False

        if not text:
            sev = "error" if cfg.empty_text_is_error else "warning"
            issues.append(ValidationIssue(idx, None, sev, "empty_text",
                                          f"Cue {idx + 1}: text is empty — enter the subtitle text."))

        # ---- duration sanity -----------------------------------------
        if timing_ok:
            dur = end - start
            if dur < cfg.min_duration_info:
                issues.append(ValidationIssue(
                    idx, None, "info", "short_cue",
                    f"Cue {idx + 1}: suspiciously short ({dur:.3f}s). "
                    "Verify the timing frame by frame."))
            if dur > cfg.max_duration_warning:
                issues.append(ValidationIssue(
                    idx, None, "warning", "long_cue",
                    f"Cue {idx + 1}: unusually long ({dur:.1f}s). "
                    "Consider splitting it."))
            if video_duration is not None and video_duration > 0 and end > video_duration + 0.05:
                issues.append(ValidationIssue(
                    idx, None, "warning", "beyond_video",
                    f"Cue {idx + 1}: end ({end:.3f}s) exceeds the video duration "
                    f"({video_duration:.3f}s)."))

        # ---- relations to previous cue -------------------------------
        if prev_pos is not None:
            prev_cue, prev_end = prev_pos
            prev_idx = getattr(prev_cue, "index", prev_cue)
            if (timing_ok and prev_end is not None
                    and start_err is None and validate_timestamp_seconds(prev_end) is None):
                delta = start - prev_end
                if delta < -cfg.overlap_tolerance:
                    overlap = -delta
                    sev = "error" if overlap > cfg.overlap_error else "warning"
                    issues.append(ValidationIssue(
                        idx, prev_idx, sev, "overlap",
                        f"Cues {prev_idx + 1} and {idx + 1} overlap by "
                        f"{overlap:.3f}s. Overlaps can be intentional — "
                        "check them, do not repair blindly."))
                elif delta > cfg.gap_warning:
                    issues.append(ValidationIssue(
                        idx, prev_idx, "warning", "large_gap",
                        f"Gap of {delta:.1f}s between cues {prev_idx + 1} and "
                        f"{idx + 1}. Possibly a missing subtitle."))
                elif delta > cfg.min_gap_info:
                    issues.append(ValidationIssue(
                        idx, prev_idx, "info", "gap",
                        f"Small pause ({delta:.3f}s) between cues "
                        f"{prev_idx + 1} and {idx + 1} (normal)."))
            # duplicate detection
            if (timing_ok and prev_end is not None
                    and start_err is None
                    and validate_timestamp_seconds(prev_cue.start) is None
                    and abs(start - prev_cue.start) <= cfg.duplicate_time_tolerance
                    and abs(end - prev_end) <= cfg.duplicate_time_tolerance):
                same_text = text == (getattr(prev_cue, "text", "") or "").strip()
                if same_text:
                    issues.append(ValidationIssue(
                        idx, prev_idx, "info", "duplicate",
                        f"Cues {prev_idx + 1} and {idx + 1} have identical "
                        "timings and text (exact duplicate)."))
                else:
                    issues.append(ValidationIssue(
                        idx, prev_idx, "warning", "near_duplicate",
                        f"Cues {prev_idx + 1} and {idx + 1} share nearly the "
                        "same timing but different text."))

        if timing_ok:
            prev_pos = (cue, end)
        else:
            # keep the chain alive using whatever numeric values exist
            e = end if (end is not None and validate_timestamp_seconds(end) is None) else (
                start if (start is not None and validate_timestamp_seconds(start) is None) else None)
            prev_pos = (cue, e)

    return ValidationSummary(issues=issues, video_duration=video_duration)


def validate_document(document, video_duration: Optional[float] = None,
                      config: Optional[ValidatorConfig] = None) -> ValidationSummary:
    """Convenience wrapper accepting a :class:`VttDocument`."""
    return validate_cues(list(document.cues), video_duration, config)
