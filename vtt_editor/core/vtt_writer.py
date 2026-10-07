"""WebVTT writer.

Rebuilds a ``.vtt`` file from a :class:`VttDocument`.

Strategy for maximum fidelity:
* If a cue's text was **not** edited, its original raw block is written back
  byte-for-byte (identifiers, settings, spacing, comments preserved).
* If the text **was** edited, only the text lines of the block are replaced –
  the timing line and every other structural line stay untouched.
  Timestamps are NEVER modified.
"""
from __future__ import annotations

from typing import List

from .vtt_parser import Cue, VttDocument, _TIMESTAMP_LINE_RE


def _rebuild_cue_block(cue: Cue) -> List[str]:
    """Return the list of lines that make up this cue block."""
    new_text_lines = cue.text.split("\n") if cue.text else []

    # Fast path: reconstruct from stored structure (works even if raw_lines
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

    # Fidelity path: keep the original raw block, replace only text lines.
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

    head = raw[: t_idx + 1]                 # notes/id/timing line(s) unchanged
    tail = raw[first_blank_after:]           # blank separator(s) unchanged

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
    """Write *doc* to *path* as UTF-8 (BOM preserved if the source had one)."""
    content = build_vtt_text(doc)
    encoding = "utf-8-sig" if getattr(doc, "had_bom", False) else "utf-8"
    with open(path, "w", encoding=encoding, newline="\n") as fh:
        fh.write(content)
