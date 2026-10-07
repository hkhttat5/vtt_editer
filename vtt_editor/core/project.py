"""Project model: document state, dirty tracking, undo/redo stack, autosave."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional

from .vtt_parser import VttDocument, read_vtt
from .vtt_writer import write_vtt


@dataclass
class EditCommand:
    """Undoable edit of a single cue's text."""
    cue_index: int
    old_text: str
    new_text: str
    old_reviewed: bool
    new_reviewed: bool


class Project:
    """Holds the parsed :class:`VttDocument` plus editing state."""

    AUTOSAVE_EVERY = 10          # edits
    MAX_UNDO = 500

    def __init__(self) -> None:
        self.document: Optional[VttDocument] = None
        self.video_path: Optional[str] = None
        self._undo: List[EditCommand] = []
        self._redo: List[EditCommand] = []
        self.edits_since_autosave = 0
        self.dirty = False               # unsaved changes to the *saved* file?

    # ------------------------------------------------------------------ load
    def load_vtt(self, path: str) -> VttDocument:
        self.document = read_vtt(path)
        self._undo.clear()
        self._redo.clear()
        self.edits_since_autosave = 0
        self.dirty = False
        return self.document

    # ----------------------------------------------------------------- cues
    @property
    def cues(self):
        return self.document.cues if self.document else []

    def cue(self, index: int):
        return self.cues[index]

    @property
    def cue_count(self) -> int:
        return len(self.cues)

    # ----------------------------------------------------------------- edit
    def set_cue_text(self, index: int, new_text: str, reviewed: bool = True) -> bool:
        """Apply a text edit (returns True when something actually changed)."""
        cue = self.cues[index]
        if cue.text == new_text and cue.reviewed == reviewed:
            # still mark reviewed even if text identical
            if not cue.reviewed and reviewed:
                cmd = EditCommand(index, cue.text, new_text, cue.reviewed, True)
                cue.reviewed = True
                self._push_undo(cmd)
                self.dirty = True
                return True
            return False
        cmd = EditCommand(index, cue.text, new_text, cue.reviewed, reviewed)
        cue.text = new_text
        cue.edited = cue.edited or (new_text != cmd.old_text)
        cue.reviewed = reviewed
        self._push_undo(cmd)
        self.dirty = True
        self.edits_since_autosave += 1
        return True

    def mark_reviewed(self, index: int) -> bool:
        cue = self.cues[index]
        if cue.reviewed:
            return False
        cmd = EditCommand(index, cue.text, cue.text, False, True)
        cue.reviewed = True
        self._push_undo(cmd)
        self.dirty = True
        self.edits_since_autosave += 1
        return True

    def _push_undo(self, cmd: EditCommand) -> None:
        self._undo.append(cmd)
        if len(self._undo) > self.MAX_UNDO:
            self._undo.pop(0)
        self._redo.clear()

    # ------------------------------------------------------------- undo/redo
    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def _original_text(self, cue_index: int) -> str:
        """The text the cue had when the file was loaded."""
        cue = self.cues[cue_index]
        if cue.raw_lines:
            try:
                from .vtt_parser import _TIMESTAMP_LINE_RE
                t_idx = next(
                    i for i, ln in enumerate(cue.raw_lines)
                    if _TIMESTAMP_LINE_RE.match(ln)
                )
                return "\n".join(
                    ln for ln in cue.raw_lines[t_idx + 1:] if ln.strip()
                )
            except StopIteration:
                pass
        return cue.text

    def undo(self) -> Optional[int]:
        """Undo last edit; returns affected cue index (or None)."""
        if not self._undo:
            return None
        cmd = self._undo.pop()
        cue = self.cues[cmd.cue_index]
        cue.text, cue.reviewed = cmd.old_text, cmd.old_reviewed
        # recompute "edited" flag: differs from original?
        cue.edited = cue.text != self._original_text(cmd.cue_index)
        self._redo.append(cmd)
        self.dirty = True
        return cmd.cue_index

    def redo(self) -> Optional[int]:
        if not self._redo:
            return None
        cmd = self._redo.pop()
        cue = self.cues[cmd.cue_index]
        cue.text, cue.reviewed = cmd.new_text, cmd.new_reviewed
        cue.edited = cue.edited or (cmd.new_text != cmd.old_text)
        self._undo.append(cmd)
        self.dirty = True
        return cmd.cue_index

    # ---------------------------------------------------------------- save
    def save(self, path: Optional[str] = None) -> str:
        assert self.document is not None
        target = path or self.document.path
        assert target, "No path given for saving"
        write_vtt(self.document, target)
        if path is None:
            self.document.path = target
        self.dirty = False
        self.edits_since_autosave = 0
        return target

    def should_autosave(self) -> bool:
        return (
            self.document is not None
            and self.dirty
            and self.edits_since_autosave >= self.AUTOSAVE_EVERY
        )

    def autosave_path(self) -> Optional[str]:
        if not self.document or not self.document.path:
            return None
        base, _ext = os.path.splitext(self.document.path)
        return f"{base}_corrected.autosave.vtt"

    def do_autosave(self) -> Optional[str]:
        path = self.autosave_path()
        if not path or self.document is None:
            return None
        write_vtt(self.document, path)      # never touches the original file
        self.edits_since_autosave = 0
        return path

    # ------------------------------------------------------------- search
    def search(self, needle: str) -> List[int]:
        """Return cue indices whose text contains *needle* (case-insensitive)."""
        if not needle:
            return []
        low = needle.lower()
        return [c.index for c in self.cues if low in c.text.lower()]

    @property
    def reviewed_count(self) -> int:
        return sum(1 for c in self.cues if c.reviewed)
