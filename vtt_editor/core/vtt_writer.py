"""WebVTT writer.

Rebuilds a ``.vtt`` file from a :class:`VttDocument`.

Strategy for maximum fidelity:
* If a cue's text was **not** edited and its timestamps are unchanged, the
  original raw block is written back byte-for-byte (identifiers, settings,
  spacing, comments preserved).
* If the text **was** edited, only the text lines of the block are replaced –
  every other structural line stays untouched.
* If the **timestamps were corrected** by the reviewer, only the timing line
  is rewritten from the cue's current ``start``/``end`` values; the cue
  settings after ``-->`` are kept.

Writes go through :func:`core.atomic_io.atomic_write_text` so a failed save
can never corrupt the previous file.
"""
from __future__ import annotations

import re
from typing import List

from .atomic_io import atomic_write_text
from .vtt_parser import Cue, VttDocument, _TIMESTAMP_LINE_RE

# matches "<ts> --> <ts>" inside an original timing line
_TIMING_SPLIT_RE = re.compile(
    r"^(?P<pre>.*?)(?P<start>\d{1,3}:\d{2}:\d{2}[.,]\d{1,9}|\d{1,3}:\d{2}[.,]\d{1,9}"
    r"|\d{1,2}[.,]\d{1,9})\s*-->\s*(?P<end>\d{1,3}:\d{2}:\d{2}[.,]\d{1,9}"
    r"|\d{1,3}:\d{2}[.,]\d{1,9}|\d{1,2}[.,]\d{1,9})(?P<post>.*)$"
)


def _timing_line_changed(cue: Cue) -> bool:
    """True when the model's timestamps differ from the parsed raw block."""
    if not cue.raw_lines:
        return False
    for ln in cue.raw_lines:
        if _TIMESTAMP_LINE_RE.match(ln):
            m = _TIMING_SPLIT_RE.match(ln)
            if not m:
                return True
            from .vtt_parser import parse_timestamp
            try:
                old_start = parse_timestamp(m.group("start"))
                old_end = parse_timestamp(m.group("end"))
            except ValueError:
                return True
            return abs(old_start - cue.start) > 0.0005 or abs(old_end - cue.end) > 0.0005
    return False


def _rebuild_cue_block(cue: Cue) -> List[str]:
    """Return the list of lines that make up this cue block."""
    new_text_lines = cue.text.split("\n") if cue.text else []

    # Reconstruct from stored structure (works even if raw_lines
    # were not captured, e.g. programmatically created cues).
    if not cue.raw_lines:
        block: List[str] = []
        block.extend(cue.note_lines)
        if cue.note_lines:
            block.append("")
        if cue.identifier is not None:
            block.append(cue.identifier)
        timing = f"{cue.start_str} --> {cue.end_str}"
        if cue.settings:
            timing += f" {cue.settings}"
        block.append(timing)
        block.extend(new_text_lines)
        return block

    # Fidelity path: keep the original raw block, replace text lines and —
    # only when they actually changed — the timing line.
    raw = list(cue.raw_lines)
    try:
        t_idx = next(
            i for i, ln in enumerate(raw) if _TIMESTAMP_LINE_RE.match(ln)
        )
    except StopIteration:      # should not happen
        t_idx = len(raw) - 1

    # find where the text lines start/end inside the raw block
    first_blank_after = len(raw)
    for j in range(t_idx + 1, len(raw)):
        if not raw[j].strip():
            first_blank_after = j
            break

    head = raw[: t_idx + 1]                 # notes/id/timing line(s)
    tail = raw[first_blank_after:]           # blank separator(s) unchanged

    if _timing_line_changed(cue):
        timing = f"{cue.start_str} --> {cue.end_str}"
        if cue.settings:
            timing += f" {cue.settings}"
        head = head[:t_idx] + [timing]

    return head + new_text_lines + tail


def build_vtt_text(doc: VttDocument) -> str:
    """Serialise the whole document back to WebVTT text."""
    out: List[str] = []

    header = doc.header_lines or ["WEBVTT"]
    out.extend(header)
    out.append("")  # blank line after header

    for cue in doc.cues:
        block = _rebuild_cue_block(cue)
        # ensure exactly one blank line separates blocks
        out.extend(block)
        if block and block[-1].strip():
            out.append("")

    if doc.trailing_lines:
        out.extend(doc.trailing_lines)

    text = "\n".join(out)
    # normalise: collapse >2 consecutive blank lines at the very end
    text = text.rstrip("\n") + "\n"
    return text


def write_vtt(doc: VttDocument, path: str) -> None:
    """Write *doc* to *path* as UTF-8 (BOM preserved if the source had one).

    The whole document is serialised first; only then is the file replaced —
    atomically via a temporary file — so a failure can never corrupt an
    existing target file.
    """
    content = build_vtt_text(doc)
    encoding = "utf-8-sig" if getattr(doc, "had_bom", False) else "utf-8"
    atomic_write_text(path, content, encoding=encoding, newline="\n")
