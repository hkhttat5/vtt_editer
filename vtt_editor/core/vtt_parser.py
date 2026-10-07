"""Robust WebVTT parser.

Parses a .vtt file into :class:`VttDocument` while preserving *all* original
information (header notes, cue identifiers, cue settings, multi-line text,
NOTE/STYLE blocks) so the file can be faithfully re-written later.

Only the TEXT of cues is meant to be edited by the application; timestamps
are never modified.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------
# VTT timestamps: hh:mm:ss.mmm  /  mm:ss.mmm  /  ss.mmm   (dot OR comma)
_TS_RE = re.compile(
    r"(\d{1,3}):(\d{2}):(\d{2})[.,](\d{1,9})"   # with hours
    r"|(\d{1,3}):(\d{2})[.,](\d{1,9})"          # mm:ss
    r"|(\d{1,2})[.,](\d{1,9})"                  # ss only
)


def parse_timestamp(text: str) -> float:
    """Convert a VTT timestamp string into seconds (float)."""
    m = _TS_RE.search(text)
    if not m:
        raise ValueError(f"Not a valid VTT timestamp: {text!r}")
    parts = [p for p in m.groups() if p is not None]
    frac = parts[-1]
    secs_items = parts[:-1]
    # normalise fraction to milliseconds precision
    frac_val = int(frac[:3].ljust(3, "0")) / 1000.0 + (
        float("0." + frac[3:]) if len(frac) > 3 else 0.0
    )
    if len(secs_items) == 3:
        h, mi, s = (int(x) for x in secs_items)
    elif len(secs_items) == 2:
        h, (mi, s) = 0, (int(x) for x in secs_items)
    else:
        h, mi, s = 0, 0, int(secs_items[0])
    return h * 3600 + mi * 60 + s + frac_val


def format_timestamp(seconds: float) -> str:
    """Format seconds back to canonical ``hh:mm:ss.mmm`` (VTT uses dots)."""
    if seconds < 0:
        seconds = 0.0
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{milli:03d}"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class Cue:
    """One subtitle cue.  ``raw_lines`` keeps the original block verbatim."""

    index: int                      # order inside the file (0-based)
    start: float                    # seconds
    end: float
    text: str                       # joined multi-line text ('\n' preserved)
    identifier: Optional[str] = None  # optional numeric/named cue id line
    settings: str = ""              # text after the '-->' on the timing line
    note_lines: List[str] = field(default_factory=list)  # NOTE blocks before cue
    raw_lines: List[str] = field(default_factory=list)   # original block lines
    reviewed: bool = False
    edited: bool = False

    @property
    def start_str(self) -> str:
        return format_timestamp(self.start)

    @property
    def end_str(self) -> str:
        return format_timestamp(self.end)


@dataclass
class VttDocument:
    header_lines: List[str] = field(default_factory=list)  # WEBVTT + notes/header
    cues: List[Cue] = field(default_factory=list)
    trailing_lines: List[str] = field(default_factory=list)
    path: Optional[str] = None
    encoding: str = "utf-8-sig"      # remember BOM presence for writing back
    had_bom: bool = False

    @property
    def cue_count(self) -> int:
        return len(self.cues)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
_TIMESTAMP_LINE_RE = re.compile(
    r"^\s*"
    r"(?P<start>\d{1,3}:\d{2}:\d{2}[.,]\d{1,9}|\d{1,3}:\d{2}[.,]\d{1,9}|\d{1,2}[.,]\d{1,9})"
    r"\s*-->\s*"
    r"(?P<end>\d{1,3}:\d{2}:\d{2}[.,]\d{1,9}|\d{1,3}:\d{2}[.,]\d{1,9}|\d{1,2}[.,]\d{1,9})"
    r"(?P<settings>.*)$"
)

_ARC_OFFSET_RE = re.compile(r"^-?\d{1,3}:\d{2}:\d{2}[.,]\d{1,9}$|^-?\d{1,3}:\d{2}[.,]\d{1,9}$")


def _is_arc_timestamp(line: str) -> bool:
    """A line that is *only* a timestamp (cue id) must NOT be treated as one."""
    return bool(_ARC_OFFSET_RE.match(line.strip())) and "-->" not in line


def parse_vtt_text(content: str) -> VttDocument:
    """Parse WebVTT *content* (already decoded) into a :class:`VttDocument`."""
    doc = VttDocument()
    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    i, n = 0, len(lines)

    # ---- header ----------------------------------------------------------
    header: List[str] = []
    if i < n and lines[i].startswith("\ufeff"):
        lines[i] = lines[i][1:]
    if i < n and lines[i].lstrip().startswith("WEBVTT"):
        header.append(lines[i])
        i += 1
        # header continues until first blank line
        while i < n and lines[i].strip():
            header.append(lines[i])
            i += 1
    doc.header_lines = header

    # skip blank lines between header and first cue
    while i < n and not lines[i].strip():
        i += 1

    pending_notes: List[str] = []
    cue_idx = 0

    def flush_trailing(start: int) -> None:
        doc.trailing_lines.extend(lines[start:n])

    while i < n:
        block_start = i
        note_block: List[str] = []
        identifier: Optional[str] = None

        # consume NOTE / STYLE / REGION blocks preceding a cue
        while i < n:
            stripped = lines[i].strip()
            if stripped.startswith(("NOTE", "STYLE", "REGION")):
                j = i
                while j < n and lines[j].strip():
                    j += 1
                note_block.extend(lines[i:j])
                while j < n and not lines[j].strip():
                    j += 1
                i = j
                continue
            break

        if i >= n:
            # trailing notes at end of file
            doc.trailing_lines.extend(note_block)
            break

        # candidate identifier line: anything that is not a timing line
        if i < n and lines[i].strip() and "-->" not in lines[i]:
            identifier = lines[i]
            i += 1

        if i >= n:
            doc.trailing_lines.extend(note_block)
            if identifier is not None:
                doc.trailing_lines.append(identifier)
            break

        m = _TIMESTAMP_LINE_RE.match(lines[i])
        if not m:
            # Not a cue after all – treat everything as trailing garbage
            # (keeps information instead of dropping it).
            flush_trailing(block_start)
            break

        timing_line = lines[i]
        start_s = parse_timestamp(m.group("start"))
        end_s = parse_timestamp(m.group("end"))
        settings = (m.group("settings") or "").strip()
        i += 1

        # text lines until blank line or EOF
        text_lines: List[str] = []
        while i < n and lines[i].strip():
            # stop early if another timing line appears without blank separator
            if _TIMESTAMP_LINE_RE.match(lines[i]):
                break
            text_lines.append(lines[i])
            i += 1

        # skip the blank separator(s)
        sep_start = i
        while i < n and not lines[i].strip():
            i += 1

        cue = Cue(
            index=cue_idx,
            start=start_s,
            end=end_s,
            text="\n".join(text_lines),
            identifier=identifier,
            settings=settings,
            note_lines=note_block,
            raw_lines=lines[block_start:i],
        )
        doc.cues.append(cue)
        cue_idx += 1
        pending_notes = []

    return doc


def read_vtt(path: str) -> VttDocument:
    """Read and parse a ``.vtt`` file (UTF-8, BOM-aware)."""
    with open(path, "rb") as fh:
        raw = fh.read()
    had_bom = raw.startswith(b"\xef\xbb\xbf")
    content = raw.decode("utf-8-sig", errors="replace")
    doc = parse_vtt_text(content)
    doc.path = path
    doc.had_bom = had_bom
    doc.encoding = "utf-8-sig" if had_bom else "utf-8"
    return doc
