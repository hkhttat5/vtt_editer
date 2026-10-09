"""Project model: document state, dirty tracking, undo/redo, editing ops.

Editing operations (text, timings, add/delete/split/merge/review) all go
through :meth:`Project._apply`, which snapshots the cue list *before* the
change with a **deep copy** and pushes it onto the undo stack.  Undo restores
the exact previous event data — text, timestamps, flags and raw blocks —
so historical states can never be mutated accidentally.

The validator (:mod:`export.vtt_validator`) is a pure function; this module
never repairs timings implicitly.
"""
from __future__ import annotations

import copy
import os
from typing import List, Optional

from .vtt_parser import Cue, VttDocument, read_vtt, format_timestamp
from .vtt_writer import write_vtt


class EditError(ValueError):
    """Raised when an editing operation is invalid (actionable message)."""


class Project:
    """Holds the parsed :class:`VttDocument` plus editing state."""

    AUTOSAVE_EVERY = 10          # edits
    MAX_UNDO = 200               # configurable history limit

    def __init__(self, max_undo: Optional[int] = None) -> None:
        self.document: Optional[VttDocument] = None
        self.video_path: Optional[str] = None
        self.max_undo = self.MAX_UNDO if max_undo is None else max(1, int(max_undo))
        self._undo: List[List[Cue]] = []
        self._redo: List[List[Cue]] = []
        self.edits_since_autosave = 0
        self.dirty = False               # unsaved changes to the *saved* file?

    # ------------------------------------------------------------------ load
    def load_vtt(self, path: str) -> VttDocument:
        self.document = read_vtt(path)
        self.renumber()
        self._undo.clear()
        self._redo.clear()
        self.edits_since_autosave = 0
        self.dirty = False
        return self.document

    # ----------------------------------------------------------------- cues
    @property
    def cues(self) -> List[Cue]:
        return self.document.cues if self.document else []

    def cue(self, index: int) -> Cue:
        return self.cues[index]

    @property
    def cue_count(self) -> int:
        return len(self.cues)

    def renumber(self) -> None:
        """Keep ``Cue.index`` in sync with the list position."""
        for i, c in enumerate(self.cues):
            c.index = i

    # -------------------------------------------------------- snapshot logic
    def _snapshot(self) -> List[Cue]:
        """Deep copy of the current cue list (safe for history)."""
        return copy.deepcopy(list(self.cues))

    def _push_undo(self, before: List[Cue]) -> None:
        self._undo.append(before)
        if len(self._undo) > self.max_undo:
            self._undo.pop(0)
        self._redo.clear()

    def _apply(self, mutate) -> bool:
        """Run *mutate* against the live cue list with full-state undo."""
        if self.document is None:
            raise EditError("No VTT document is open.")
        before = self._snapshot()
        changed = mutate(before)
        if not changed:
            return False
        self.renumber()
        self._push_undo(before)
        self.dirty = True
        self.edits_since_autosave += 1
        return True

    # ------------------------------------------------------------- validation
    @staticmethod
    def _check_timing(start: float, end: float) -> None:
        from export.vtt_validator import validate_timestamp_seconds
        err = validate_timestamp_seconds(start)
        if err:
            raise EditError(f"Invalid start time: {err}.")
        err = validate_timestamp_seconds(end)
        if err:
            raise EditError(f"Invalid end time: {err}.")
        if end <= start:
            raise EditError(
                f"End time ({format_timestamp(end)}) must be strictly greater "
                f"than start time ({format_timestamp(start)}).")

    # ----------------------------------------------------------------- edit
    def set_cue_text(self, index: int, new_text: str, reviewed: bool = True) -> bool:
        """Apply a text edit (returns True when something actually changed)."""
        cue = self.cues[index]
        if cue.text == new_text and cue.reviewed == reviewed:
            return False

        def mutate(_before):
            c = self.cues[index]
            c.text = new_text
            c.edited = c.edited or (new_text != _before[index].text)
            c.reviewed = reviewed
            return True
        return self._apply(mutate)

    def set_cue_timing(self, index: int, start: Optional[float] = None,
                       end: Optional[float] = None) -> bool:
        """Set start/end of one cue (validated; no implicit repair)."""
        cue = self.cues[index]
        s = cue.start if start is None else float(start)
        e = cue.end if end is None else float(end)
        self._check_timing(s, e)
        if abs(cue.start - s) < 1e-9 and abs(cue.end - e) < 1e-9:
            return False

        def mutate(_b):
            c = self.cues[index]
            c.start, c.end = s, e
            return True
        return self._apply(mutate)

    def mark_reviewed(self, index: int, reviewed: bool = True) -> bool:
        cue = self.cues[index]
        if cue.reviewed == reviewed:
            return False

        def mutate(_b):
            self.cues[index].reviewed = reviewed
            return True
        return self._apply(mutate)

    # ------------------------------------------------------------ structure
    def _make_cue(self, start: float, end: float, text: str,
                  settings: str = "") -> Cue:
        self._check_timing(start, end)
        if not (text or "").strip():
            raise EditError("Subtitle text must not be empty.")
        cue = Cue(index=-1, start=float(start), end=float(end), text=text,
                  identifier=None, settings=settings or "",
                  note_lines=[], raw_lines=[])
        cue.edited = True
        return cue

    def add_cue(self, start: float, end: float, text: str,
                insert_at: Optional[int] = None, reviewed: bool = False) -> int:
        """Insert a new cue; returns its index."""
        cue = self._make_cue(start, end, text)

        def mutate(_b):
            pos = len(self.cues) if insert_at is None else max(
                0, min(insert_at, len(self.cues)))
            self.cues.insert(pos, cue)
            return True
        self._apply(mutate)
        # find where it landed after renumbering
        for i, c in enumerate(self.cues):
            if c is cue:
                return i
        return len(self.cues) - 1

    def delete_cue(self, index: int) -> None:
        if not (0 <= index < self.cue_count):
            raise EditError(f"No cue at position {index + 1}.")

        def mutate(_b):
            del self.cues[index]
            return bool(self.cues) or True
        self._apply(mutate)

    def split_cue(self, index: int, split_time: float,
                  first_text: Optional[str] = None,
                  second_text: Optional[str] = None) -> int:
        """Split cue *index* into two cues at *split_time*.

        The original text is preserved on both halves unless the caller
        explicitly supplies edited texts.  Never guesses a linguistic split.
        Returns the index of the second half.
        """
        cue = self.cues[index]
        start, end = cue.start, cue.end
        if not (start < split_time < end):
            raise EditError(
                f"Split time ({format_timestamp(split_time)}) must be strictly "
                f"between start ({format_timestamp(start)}) and "
                f"end ({format_timestamp(end)}).")
        t1 = cue.text if first_text is None else first_text
        t2 = cue.text if second_text is None else second_text
        if not (t1 or "").strip() or not (t2 or "").strip():
            raise EditError("Both halves of a split cue need non-empty text.")

        def mutate(_b):
            orig = self.cues[index]
            first = Cue(index=orig.index, start=start, end=split_time, text=t1,
                        identifier=orig.identifier, settings=orig.settings,
                        note_lines=list(orig.note_lines),
                        raw_lines=list(orig.raw_lines),
                        reviewed=False, edited=orig.edited)
            second = Cue(index=orig.index + 1, start=split_time, end=end, text=t2,
                         identifier=None, settings="", note_lines=[],
                         raw_lines=[], reviewed=False, edited=True)
            self.cues[index:index + 1] = [first, second]
            return True
        self._apply(mutate)
        return index + 1

    def merge_preview(self, index: int) -> dict:
        """Read-only preview of merging cue *index* with the next one."""
        if index + 1 >= self.cue_count:
            raise EditError("There is no next cue to merge with.")
        a, b = self.cues[index], self.cues[index + 1]
        delta = b.start - a.end
        return {
            "start": a.start,
            "end": b.end,
            "gap": delta,                      # negative => overlap
            "text_a": a.text,
            "text_b": b.text,
            "combined_text": (a.text + "\n" + b.text) if a.text and b.text
                             else (a.text or b.text),
        }

    def merge_with_next(self, index: int, text: Optional[str] = None,
                        start: Optional[float] = None,
                        end: Optional[float] = None) -> bool:
        """Merge cue *index* with the next chronological cue.

        Both texts are preserved in the combined default until the user
        confirms/edits; nothing is silently discarded.
        """
        if index + 1 >= self.cue_count:
            raise EditError("There is no next cue to merge with.")
        a, b = self.cues[index], self.cues[index + 1]
        preview = self.merge_preview(index)
        s = preview["start"] if start is None else float(start)
        e = preview["end"] if end is None else float(end)
        merged_text = preview["combined_text"] if text is None else text
        self._check_timing(s, e)
        if not (merged_text or "").strip():
            raise EditError("Merged cue text must not be empty — both texts "
                            "would otherwise be lost.")

        def mutate(_b):
            ca, cb = self.cues[index], self.cues[index + 1]
            ca.start, ca.end, ca.text = s, e, merged_text
            ca.edited = True
            ca.reviewed = False
            del self.cues[index + 1]
            return True
        return self._apply(mutate)

    # ------------------------------------------------------------- undo/redo
    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo(self) -> bool:
        """Restore the exact previous document state (all fields)."""
        if not self._undo or self.document is None:
            return False
        self._redo.append(self._snapshot())
        self.document.cues = self._undo.pop()
        self.renumber()
        self.dirty = True
        return True

    def redo(self) -> bool:
        if not self._redo or self.document is None:
            return False
        self._undo.append(self._snapshot())
        self.document.cues = self._redo.pop()
        self.renumber()
        self.dirty = True
        return True

    # ---------------------------------------------------------------- save
    def save(self, path: Optional[str] = None) -> str:
        assert self.document is not None
        target = path or self.document.path
        assert target, "No path given for saving"
        write_vtt(self.document, target)   # atomic temp-file replacement
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
        try:
            write_vtt(self.document, path)      # never touches the original file
        except OSError:
            return None
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
