"""Pure VTT issue detection for human review.

This module EXTENDS :mod:`export.vtt_validator` (structural timing checks)
with a set of *textual* heuristics — OCR noise, corrupted characters,
incomplete words and suspicious non-German text.  Like the validator it is
a pure function: cues are only read, never modified, and nothing is ever
auto-corrected.  Every finding is a hint for the reviewer, not a verdict.

The combined entry point :func:`analyze_cues` returns an :class:`IssueSummary`
plus per-cue :class:`CueReview` records that keep the user's *reviewed* state
separate from automatically detected issues.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from export.vtt_validator import (
    SEVERITY_ORDER,
    ValidationIssue,
    ValidatorConfig,
    validate_cues,
)

# ---------------------------------------------------------------------------
# Configurable thresholds / word lists for the textual heuristics
# ---------------------------------------------------------------------------
@dataclass
class HeuristicConfig:
    max_upper_ratio: float = 0.6       # mostly-upper-case letters (>=4 letters)
    min_symbol_ratio: float = 0.30     # >30 % non-alphanumeric chars
    min_alt_ratio: float = 0.55        # alternating vowel/consonant runs
    min_token_len: int = 4             # ignore short tokens for word checks
    ocr_confusions: str = "01!lI|5$&@{}[]~^_<>+=*/#"
    # common German words (lowercase).  Used ONLY to detect text that shows
    # zero German signal; presence of one word never clears other flags.
    german_words: frozenset = frozenset("""
        der die das und ist sind ein eine oder aber wenn als dann wie auch
        nicht man wir sie ihr ihm ihn euch euer unser mein dein sein ihre
        mit für auf von zu bei nach aus über unter in an dem den des im zur
        hier dort heute morgen gestern jetzt noch schon wieder alles
        nichts etwas jemand niemand sehr gut schlecht groß klein neu alt
        geschichte apfel äpfel öl straße zoo elefant wilkommen geschichten
        weil damit obwohl während zwischen innerhalb außerhalb
        hallo tschüss bitte danke ja nein viel wenig mehr weniger
        tag nacht zeit jahr woche monat stunde minute sekunde
        mann frau kind kinder menschen leute wasser feuer luft erde
        haus hause auto buch bücher tisch stuhl tür fenster weg
    """.split())
    # strong English markers (whole-word match, case-insensitive)
    english_markers: frozenset = frozenset(
        ("the this that these those and or but with for you your we our they"
         " their have has had not what when where who which about just like"
         " good morning night day year time people water house book").split())


DEFAULT_HEURISTICS = HeuristicConfig()

_VOWELS = set("aeiouäöüàèìòùáéíóúâêîôû")
_STOPWORDS = DEFAULT_HEURISTICS.german_words | DEFAULT_HEURISTICS.english_markers


def _letters(text: str) -> str:
    return "".join(ch for ch in text if ch.isalpha())


def _consonant_runs(text: str) -> int:
    """Number of maximal runs of >=3 consecutive consonants in the text."""
    count, run = 0, 0
    for ch in text.lower():
        if not ch.isalpha():
            run = 0
            continue
        if ch in _VOWELS or ch == "ß":
            run = 0
        else:
            run += 1
            if run == 3:
                count += 1
    return count


def _is_noise_word(word: str, cfg: HeuristicConfig) -> bool:
    """A single token that looks like OCR garbage rather than a real word.

    Conservative on purpose: real German words (also long compound nouns
    like "Geschichte") must never match — only vowel-free or heavily
    consonant-clustered tokens do.
    """
    core = re.sub(r"\W", "", word.lower())
    if len(core) < cfg.min_token_len:
        return False
    if core in _STOPWORDS:
        return False
    vowels = sum(1 for ch in core if ch in _VOWELS)
    if vowels == 0 and _consonant_runs(core) >= 1:
        return True                       # e.g. "dfgh", "strmkn"
    return False


def _is_incomplete_word(word: str) -> bool:
    """Clear truncated fragments: ASCII lowercase token without any vowel."""
    w = word.strip()
    if len(w) < 3 or not w.isascii() or not w.isalnum():
        return False
    if w != w.lower():
        return False
    if w in _STOPWORDS:
        return False
    return not any(ch in _VOWELS for ch in w)


# ---------------------------------------------------------------------------
# Per-cue textual analysis
# ---------------------------------------------------------------------------
def analyze_cue_text(index: int, text: str,
                     cfg: Optional[HeuristicConfig] = None) -> List[ValidationIssue]:
    """Return textual-heuristic issues for one cue's text (never mutates)."""
    cfg = cfg or DEFAULT_HEURISTICS
    issues: List[ValidationIssue] = []
    raw = text or ""
    stripped = raw.strip()
    if not stripped:
        return issues          # emptiness is already an error in vtt_validator

    letters = _letters(stripped)
    n = len(letters)

    # ---- corrupted / replacement characters --------------------------
    bad = [ch for ch in stripped if ch == "\ufffd" or (
        ord(ch) < 32 and ch not in "\n\r\t")]
    if bad:
        shown = repr("".join(sorted(set(bad)))[:8])
        issues.append(ValidationIssue(
            index, None, "warning", "corrupted_chars",
            f"Cue {index + 1}: text contains corrupted control/replacement "
            f"characters ({shown}). Likely an encoding or OCR artifact."))

    # ---- symbol clutter / OCR character confusions --------------------
    alnum_ws = sum(1 for ch in stripped if ch.isalnum() or ch.isspace())
    if len(stripped) >= 6 and alnum_ws / len(stripped) < (1 - cfg.min_symbol_ratio):
        issues.append(ValidationIssue(
            index, None, "warning", "symbol_clutter",
            f"Cue {index + 1}: over {cfg.min_symbol_ratio:.0%} of the "
            "characters are symbols — possible OCR noise."))
    else:
        conf = sum(1 for ch in stripped if ch in cfg.ocr_confusions)
        if n >= 8 and conf / max(n, 1) > 0.25:
            issues.append(ValidationIssue(
                index, None, "warning", "ocr_symbols",
                f"Cue {index + 1}: unusual mix of digits/symbols inside words "
                "(e.g. 0/o, 1/l, 5/s confusions typical of OCR)."))

    # ---- mostly upper-case text (info only: may be intentional) -------
    if n >= 4 and sum(1 for ch in letters if ch.isupper()) / n > cfg.max_upper_ratio:
        issues.append(ValidationIssue(
            index, None, "info", "all_caps",
            f"Cue {index + 1}: mostly upper-case letters. Verify this is "
            "intentional emphasis and not an OCR misread."))

    # ---- word-level checks -------------------------------------------
    tokens = re.findall(r"[A-Za-zÄÖÜäöüß][A-Za-zÄÖÜäöüß'\-]*", stripped)
    frag = next((t for t in tokens if t.isascii() and _is_incomplete_word(t)), None)
    if frag:
        issues.append(ValidationIssue(
            index, None, "info", "word_fragment",
            f"Cue {index + 1}: '{frag}' looks like an incomplete/truncated "
            "word fragment (possibly cut off by the ROI border)."))
    noise_words = [t for t in tokens if len(_letters(t)) >= cfg.min_token_len
                   and _is_noise_word(t, cfg)]
    if noise_words:
        sample = ", ".join(repr(w) for w in noise_words[:3])
        issues.append(ValidationIssue(
            index, None, "warning", "ocr_noise",
            f"Cue {index + 1}: {sample} look like OCR noise rather than real "
            "words (vowel-free / impossible consonant clusters)."))

    # ---- suspicious non-German text (local heuristic only) ------------
    lowered = [t.lower() for t in tokens]
    de_hits = sum(1 for t in lowered if t in cfg.german_words)
    en_hits = sum(1 for t in lowered if t in cfg.english_markers)
    umlaut_or_eszett = any(ch in "äöüßÄÖÜ" for ch in stripped)
    # valid German without any stopword hit is perfectly normal → require
    # BOTH a missing German signal AND a present English signal to warn.
    if tokens and en_hits >= 2 and de_hits == 0 and not umlaut_or_eszett:
        issues.append(ValidationIssue(
            index, None, "warning", "non_german",
            f"Cue {index + 1}: text reads like English, not German "
            f"({en_hits} English marker words, no German vocabulary found). "
            "Check whether the wrong subtitle track was extracted."))
    elif (tokens and not noise_words and frag is None and de_hits == 0
          and en_hits == 0 and not umlaut_or_eszett and n >= 8):
        # nothing recognizable at all → weak info hint
        issues.append(ValidationIssue(
            index, None, "info", "unrecognized_language",
            f"Cue {index + 1}: no recognizable German (or English) words. "
            "Could be a name, a foreign-language quote — or OCR noise."))

    return issues


# ---------------------------------------------------------------------------
# Duplicate text across the whole document (timing independent)
# ---------------------------------------------------------------------------
def find_duplicate_texts(cues) -> List[ValidationIssue]:
    """Flag identical visible texts appearing on multiple cues.

    Repeated *valid* expressions are legitimate in subtitles, so this is
    only an informational hint pointing at every occurrence.
    """
    seen: dict = {}
    for cue in cues:
        key = (cue.text or "").strip().lower()
        if not key:
            continue
        seen.setdefault(key, []).append(getattr(cue, "index", 0))
    issues: List[ValidationIssue] = []
    for key, idxs in seen.items():
        if len(idxs) > 1:
            first = idxs[0]
            for i in idxs[1:]:
                issues.append(ValidationIssue(
                    i, first, "info", "duplicate_text",
                    f"Cue {i + 1}: same text as cue {first + 1} "
                    f"('{key[:40]}'). Repetition can be legitimate — verify."))
    return issues


# ---------------------------------------------------------------------------
# Combined summary with per-cue review state
# ---------------------------------------------------------------------------
@dataclass
class CueReview:
    """Per-cue roll-up: detected issues + the user's reviewed flag."""
    index: int
    issues: List[ValidationIssue] = field(default_factory=list)
    reviewed: bool = False

    @property
    def worst(self) -> Optional[str]:
        if not self.issues:
            return None
        return min((i.severity for i in self.issues),
                   key=lambda s: SEVERITY_ORDER[s])

    @property
    def has_unresolved(self) -> bool:
        """Unresolved = warning/error flagged and not reviewed yet.

        Purely informational findings (``info``) never block the review.
        """
        return (not self.reviewed
                and self.worst in ("error", "warning"))


class IssueSummary:
    """All detected issues + review progress (compatible with ValidationSummary)."""

    def __init__(self, reviews=None, video_duration=None, starts=None):
        self.reviews: List[CueReview] = reviews or []
        self.video_duration: Optional[float] = video_duration
        self._starts_map = dict(starts or {})

    # -- ValidationSummary-compatible helpers (kept for existing callers) --
    @property
    def issues(self) -> List[ValidationIssue]:
        out: List[ValidationIssue] = []
        for r in self.reviews:
            out.extend(r.issues)
        return out

    @property
    def errors(self):
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self):
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def infos(self):
        return [i for i in self.issues if i.severity == "info"]

    def counts(self):
        return len(self.errors), len(self.warnings), len(self.infos)

    def indices_with_severity(self) -> dict:
        return {r.index: r.worst for r in self.reviews if r.worst}

    def issues_for_cue(self, cue_index: int) -> List[ValidationIssue]:
        for r in self.reviews:
            if r.index == cue_index:
                return list(r.issues)
        return []

    # -- review-oriented helpers -----------------------------------------
    def review(self, cue_index: int) -> Optional[CueReview]:
        for r in self.reviews:
            if r.index == cue_index:
                return r
        return None

    def unresolved_indices(self) -> List[int]:
        """Cue indices with unresolved issues, chronological order."""
        rows = sorted(
            (r for r in self.reviews if r.has_unresolved),
            key=lambda r: (self._starts_map.get(r.index, 0.0), r.index))
        return [r.index for r in rows]

    @property
    def reviewed_count(self) -> int:
        return sum(1 for r in self.reviews if r.reviewed)

    @property
    def total_count(self) -> int:
        return len(self.reviews)

    @property
    def unresolved_error_count(self) -> int:
        return sum(1 for r in self.reviews
                   if r.has_unresolved and r.worst == "error")

    @property
    def unresolved_warning_count(self) -> int:
        return sum(1 for r in self.reviews
                   if r.has_unresolved and r.worst == "warning")


def analyze_cues(cues,
                 video_duration: Optional[float] = None,
                 config: Optional[ValidatorConfig] = None,
                 heuristics: Optional[HeuristicConfig] = None) -> IssueSummary:
    """Full review analysis: structural validation + textual heuristics.

    Pure function — cues are read only.  The user's ``reviewed`` flag is
    carried into the per-cue review records so revalidation never clears it.
    """
    structural = validate_cues(list(cues), video_duration, config)
    per_cue: dict = {}
    for issue in structural.issues:
        for idx in (issue.cue_index, issue.related_index):
            if idx is not None:
                per_cue.setdefault(idx, []).append(issue)

    for cue in cues:
        idx = getattr(cue, "index", 0)
        for issue in analyze_cue_text(idx, getattr(cue, "text", ""), heuristics):
            per_cue.setdefault(idx, []).append(issue)

    for issue in find_duplicate_texts(cues):
        for idx in (issue.cue_index, issue.related_index):
            if idx is None:
                continue
            bucket = per_cue.setdefault(idx, [])
            if not any(x.code == issue.code and x.cue_index == issue.cue_index
                       and x.related_index == issue.related_index
                       for x in bucket):
                bucket.append(issue)

    reviews: List[CueReview] = []
    starts = {}
    for cue in cues:
        idx = getattr(cue, "index", 0)
        try:
            starts[idx] = float(getattr(cue, "start", 0.0) or 0.0)
        except (TypeError, ValueError):
            starts[idx] = 0.0
        seen_msg, uniq = set(), []
        for issue in per_cue.get(idx, []):
            if issue.message not in seen_msg:
                seen_msg.add(issue.message)
                uniq.append(issue)
        reviews.append(CueReview(index=idx, issues=uniq,
                                 reviewed=bool(getattr(cue, "reviewed", False))))
    return IssueSummary(reviews=reviews, video_duration=video_duration,
                        starts=starts)
