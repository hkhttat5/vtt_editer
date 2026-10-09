"""Selected-subtitle editor panel for the VTT review interface.

Shows the currently selected cue with editable German text, start and end
times (``HH:MM:SS.mmm`` / ``MM:SS.mmm`` accepted), a live duration read-out
and all structural actions: Apply, Revert, Delete, Split, Merge with next,
Mark as Reviewed, plus previous/next navigation.

Every change is validated before it reaches the model; nothing is committed
silently.  The panel never touches ROI coordinates or extraction internals.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

from core.vtt_parser import format_timestamp
from export.vtt_validator import parse_user_timestamp


class TimeLineEdit(QLineEdit):
    """Small line editor that accepts VTT timestamps (validated on apply)."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setPlaceholderText("HH:MM:SS.mmm")
        f = QFont("Consolas", 10)
        self.setFont(f)
        self.setMaximumWidth(130)


class SplitDialog(QDialog):
    """Ask for the split timestamp; texts default to the original text."""

    def __init__(self, cue, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Split Subtitle")
        self.cue = cue
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.split_edit = TimeLineEdit()
        mid = cue.start + (cue.end - cue.start) / 2.0
        self.split_edit.setText(format_timestamp(mid))
        form.addRow(f"Split time\n(between {cue.start_str} and {cue.end_str}):",
                    self.split_edit)
        layout.addLayout(form)

        layout.addWidget(QLabel("First part text:"))
        self.text1 = QPlainTextEdit(cue.text)
        self.text1.setMaximumHeight(70)
        layout.addWidget(self.text1)
        layout.addWidget(QLabel("Second part text:"))
        self.text2 = QPlainTextEdit(cue.text)
        self.text2.setMaximumHeight(70)
        layout.addWidget(self.text2)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                              | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)

    def values(self):
        return (parse_user_timestamp(self.split_edit.text()),
                self.text1.toPlainText(), self.text2.toPlainText())


class MergeDialog(QDialog):
    """Confirm merge: shows both texts, lets user edit result & timing."""

    def __init__(self, preview: dict, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Merge With Next Subtitle")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Original text of cue A:"))
        ta = QPlainTextEdit(preview["text_a"])
        ta.setReadOnly(True)
        ta.setMaximumHeight(60)
        layout.addWidget(ta)
        layout.addWidget(QLabel("Original text of cue B:"))
        tb = QPlainTextEdit(preview["text_b"])
        tb.setReadOnly(True)
        tb.setMaximumHeight(60)
        layout.addWidget(tb)

        form = QFormLayout()
        self.start_edit = TimeLineEdit()
        self.start_edit.setText(format_timestamp(preview["start"]))
        self.end_edit = TimeLineEdit()
        self.end_edit.setText(format_timestamp(preview["end"]))
        row = QHBoxLayout()
        row.addWidget(self.start_edit)
        row.addWidget(QLabel("→"))
        row.addWidget(self.end_edit)
        form.addRow("Merged timing:", row)
        layout.addLayout(form)

        layout.addWidget(QLabel("Resulting text (both parts kept until you edit):"))
        self.text_edit = QPlainTextEdit(preview["combined_text"])
        self.text_edit.setMaximumHeight(90)
        layout.addWidget(self.text_edit)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                              | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)

    def values(self):
        return (parse_user_timestamp(self.start_edit.text()),
                parse_user_timestamp(self.end_edit.text()),
                self.text_edit.toPlainText())


class CueEditorPanel(QWidget):
    """Dedicated editor for the selected subtitle."""

    applyRequested = Signal(int, str, float, float)   # idx, text, start, end
    deleteRequested = Signal(int)
    splitRequested = Signal(int)
    mergeRequested = Signal(int)
    reviewRequested = Signal(int)
    prevRequested = Signal()
    nextRequested = Signal()
    seekToStartRequested = Signal(float)
    seekToEndRequested = Signal(float)
    stepSecondsRequested = Signal(float)     # frame stepping via main window

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._index: int = -1
        self._loading = False

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 6)
        root.setSpacing(6)

        head = QHBoxLayout()
        self.id_label = QLabel("Subtitle ID: –")
        f = QFont(); f.setBold(True)
        self.id_label.setFont(f)
        head.addWidget(self.id_label)
        head.addStretch(1)
        self.review_label = QLabel("○ Not reviewed")
        head.addWidget(self.review_label)
        self.btn_prev = QPushButton("◀ Previous")
        self.btn_next = QPushButton("Next ▶")
        self.btn_prev.clicked.connect(self.prevRequested)
        self.btn_next.clicked.connect(self.nextRequested)
        head.addWidget(self.btn_prev)
        head.addWidget(self.btn_next)
        root.addLayout(head)

        self.text_edit = QPlainTextEdit()
        self.text_edit.setPlaceholderText("German subtitle text (ä ö ü ß)")
        self.text_edit.setMaximumHeight(90)
        tf = QFont("Segoe UI", 12)
        self.text_edit.setFont(tf)
        root.addWidget(self.text_edit)

        times = QHBoxLayout()
        times.addWidget(QLabel("Start:"))
        self.start_edit = TimeLineEdit()
        times.addWidget(self.start_edit)
        btn_s = QPushButton("⊳")
        btn_s.setToolTip("Seek video to this start time")
        btn_s.setFixedWidth(28)
        btn_s.clicked.connect(lambda: self._seek(self.start_edit))
        times.addWidget(btn_s)
        times.addSpacing(12)
        times.addWidget(QLabel("End:"))
        self.end_edit = TimeLineEdit()
        times.addWidget(self.end_edit)
        btn_e = QPushButton("⊳")
        btn_e.setToolTip("Seek video to this end time")
        btn_e.setFixedWidth(28)
        btn_e.clicked.connect(lambda: self._seek(self.end_edit))
        times.addWidget(btn_e)
        times.addSpacing(12)
        times.addWidget(QLabel("Duration:"))
        self.duration_label = QLabel("–")
        times.addWidget(self.duration_label)
        times.addStretch(1)
        root.addLayout(times)

        frames = QHBoxLayout()
        for label, delta in (("−1 frame", -1), ("+1 frame", +1),
                             ("−1 s", -1.0), ("+1 s", +1.0)):
            b = QPushButton(label)
            b.setToolTip(f"Step the paused video ({label}) and copy its "
                         "position into Start")
            b.clicked.connect(lambda _c=False, d=delta: self.stepSecondsRequested.emit(d))
            frames.addWidget(b)
        self.copy_start_btn = QPushButton("Start ← video position")
        self.copy_start_btn.clicked.connect(self._copy_video_position)
        self.copy_end_btn = QPushButton("End ← video position")
        self.copy_end_btn.clicked.connect(lambda: self._copy_video_position(end=True))
        frames.addWidget(self.copy_start_btn)
        frames.addWidget(self.copy_end_btn)
        frames.addStretch(1)
        root.addLayout(frames)

        actions = QHBoxLayout()
        self.btn_apply = QPushButton("Apply Changes")
        self.btn_apply.setDefault(True)
        self.btn_apply.clicked.connect(self.apply_current)
        self.btn_revert = QPushButton("Revert Current Edit")
        self.btn_revert.clicked.connect(self.revert_current)
        self.btn_review = QPushButton("Mark as Reviewed")
        self.btn_review.clicked.connect(
            lambda: self._index >= 0 and self.reviewRequested.emit(self._index))
        self.btn_delete = QPushButton("Delete Subtitle")
        self.btn_delete.clicked.connect(
            lambda: self._index >= 0 and self.deleteRequested.emit(self._index))
        self.btn_split = QPushButton("Split Subtitle…")
        self.btn_split.clicked.connect(
            lambda: self._index >= 0 and self.splitRequested.emit(self._index))
        self.btn_merge = QPushButton("Merge With Next…")
        self.btn_merge.clicked.connect(
            lambda: self._index >= 0 and self.mergeRequested.emit(self._index))
        for b in (self.btn_apply, self.btn_revert, self.btn_review,
                  self.btn_split, self.btn_merge, self.btn_delete):
            actions.addWidget(b)
        actions.addStretch(1)
        root.addLayout(actions)

        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: #b00000;")
        self.error_label.setWordWrap(True)
        root.addWidget(self.error_label)

        self.start_edit.textChanged.connect(self._update_duration)
        self.end_edit.textChanged.connect(self._update_duration)
        # remember current video position provider (set by main window)
        self.video_position_provider = None   # callable -> seconds or None

    # ------------------------------------------------------------------ api
    def load_cue(self, index: int, cue) -> None:
        self._loading = True
        try:
            self._index = index
            self.id_label.setText(f"Subtitle ID: {index + 1}")
            self.text_edit.setPlainText(cue.text)
            self.start_edit.setText(cue.start_str)
            self.end_edit.setText(cue.end_str)
            self.error_label.setText("")
            self._refresh_review(cue)
            self._update_duration()
            enabled = cue is not None
            for b in (self.btn_apply, self.btn_revert, self.btn_review,
                      self.btn_delete, self.btn_split, self.btn_merge):
                b.setEnabled(enabled)
            self.btn_merge.setEnabled(index + 1 < self._cue_total())
        finally:
            self._loading = False

    def clear(self) -> None:
        self._index = -1
        self._loading = True
        try:
            self.id_label.setText("Subtitle ID: –")
            self.text_edit.setPlainText("")
            self.start_edit.setText("")
            self.end_edit.setText("")
            self.duration_label.setText("–")
            self.error_label.setText("")
            for b in (self.btn_apply, self.btn_revert, self.btn_review,
                      self.btn_delete, self.btn_split, self.btn_merge,
                      self.btn_prev, self.btn_next):
                b.setEnabled(False)
        finally:
            self._loading = False

    def enable_navigation(self, has_prev: bool, has_next: bool) -> None:
        self.btn_prev.setEnabled(has_prev)
        self.btn_next.setEnabled(has_next)

    def set_error(self, message: str) -> None:
        self.error_label.setText(message)

    def edited_values(self):
        """Parse the pending edits; raises ValueError with an actionable msg."""
        text = self.text_edit.toPlainText()
        start = parse_user_timestamp(self.start_edit.text())
        end = parse_user_timestamp(self.end_edit.text())
        return text, start, end

    def apply_current(self) -> None:
        if self._index < 0 or self._loading:
            return
        try:
            text, start, end = self.edited_values()
        except ValueError as exc:
            self.set_error(str(exc))
            return
        self.error_label.setText("")
        self.applyRequested.emit(self._index, text, start, end)

    def revert_current(self) -> None:
        if self._index < 0:
            return
        self.set_error("")

    @property
    def current_index(self) -> int:
        return self._index

    @property
    def is_loading(self) -> bool:
        return self._loading

    # ------------------------------------------------------------- internal
    def _cue_total(self) -> int:
        parent = self.window()
        proj = getattr(parent, "project", None)
        return proj.cue_count if proj else 0

    def _refresh_review(self, cue) -> None:
        if cue.reviewed:
            self.review_label.setText("✓ Reviewed")
            self.review_label.setStyleSheet("color: green; font-weight: bold;")
        else:
            self.review_label.setText("○ Not reviewed")
            self.review_label.setStyleSheet("color: gray;")

    def _update_duration(self) -> None:
        if self._loading:
            return
        try:
            s = parse_user_timestamp(self.start_edit.text())
            e = parse_user_timestamp(self.end_edit.text())
            self.duration_label.setText(f"{max(0.0, e - s):.3f} s")
        except ValueError:
            self.duration_label.setText("? s")

    def _seek(self, line_edit: TimeLineEdit) -> None:
        try:
            v = parse_user_timestamp(line_edit.text())
        except ValueError as exc:
            self.set_error(str(exc))
            return
        self.error_label.setText("")
        if line_edit is self.start_edit:
            self.seekToStartRequested.emit(v)
        else:
            self.seekToEndRequested.emit(v)

    def _copy_video_position(self, end: bool = False) -> None:
        if self.video_position_provider is None:
            return
        pos = self.video_position_provider()
        if pos is None:
            return
        target = self.end_edit if end else self.start_edit
        target.setText(format_timestamp(pos))
