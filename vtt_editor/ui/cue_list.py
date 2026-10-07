"""Collapsible side panel listing all cues, with a search box.

Each row shows:  ✓/○  <number>  <first line of text>
Clicking a row jumps to that cue.  Typing in the search box filters rows.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QListWidget,
    QListWidgetItem, QLabel,
)


class CueListPanel(QWidget):
    cueActivated = Signal(int)          # cue index
    searchRequested = Signal(str)       # live filter text

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Cues")
        self.setMinimumWidth(280)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        top = QHBoxLayout()
        top.addWidget(QLabel("🔍"))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search cue texts…  (Ctrl+F)")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._filter)
        top.addWidget(self.search_edit)
        layout.addLayout(top)

        self.stats_label = QLabel("0 / 0 reviewed")
        self.stats_label.setStyleSheet("color: gray;")
        layout.addWidget(self.stats_label)

        self.list_widget = QListWidget()
        self.list_widget.setUniformItemSizes(True)
        self.list_widget.itemClicked.connect(
            lambda it: self.cueActivated.emit(int(it.data(Qt.ItemDataRole.UserRole)))
        )
        self.list_widget.itemDoubleClicked.connect(
            lambda it: self.cueActivated.emit(int(it.data(Qt.ItemDataRole.UserRole)))
        )
        layout.addWidget(self.list_widget)

        self._cue_count = 0

    # ------------------------------------------------------------------ api
    def rebuild(self, cues, current_index: int, reviewed_count: int) -> None:
        """Refresh every row from the document."""
        self.list_widget.clear()
        self._cue_count = len(cues)
        for cue in cues:
            first_line = cue.text.split("\n", 1)[0] if cue.text else ""
            if len(first_line) > 60:
                first_line = first_line[:57] + "…"
            mark = "✓" if cue.reviewed else "○"
            edited_mark = " ✎" if cue.edited else ""
            item = QListWidgetItem(f"{mark} {cue.index + 1:>4}{edited_mark}  {first_line}")
            item.setData(Qt.ItemDataRole.UserRole, cue.index)
            if cue.index == current_index:
                item.setBackground(Qt.GlobalColor.darkYellow)
                f = item.font(); f.setBold(True); item.setFont(f)
            elif cue.reviewed:
                item.setForeground(Qt.GlobalColor.darkGreen)
            self.list_widget.addItem(item)
        self.stats_label.setText(
            f"{reviewed_count} / {self._cue_count} reviewed"
        )
        self._apply_filter()

    def update_row(self, index: int, cue) -> None:
        """Update a single row in place (fast path)."""
        first_line = cue.text.split("\n", 1)[0] if cue.text else ""
        if len(first_line) > 60:
            first_line = first_line[:57] + "…"
        mark = "✓" if cue.reviewed else "○"
        edited_mark = " ✎" if cue.edited else ""
        for i in range(self.list_widget.count()):
            it = self.list_widget.item(i)
            if it.data(Qt.ItemDataRole.UserRole) == index:
                it.setText(f"{mark} {index + 1:>4}{edited_mark}  {first_line}")
                it.setForeground(
                    Qt.GlobalColor.darkGreen if cue.reviewed
                    else Qt.GlobalColor.black
                )
                break

    def highlight_current(self, index: int) -> None:
        """Bold-highlight the current cue row and scroll to it."""
        for i in range(self.list_widget.count()):
            it = self.list_widget.item(i)
            cur = it.data(Qt.ItemDataRole.UserRole) == index
            f = it.font()
            f.setBold(cur)
            it.setFont(f)
            if cur:
                self.list_widget.setCurrentItem(it)
                self.list_widget.scrollToItem(it)

    def set_reviewed_stats(self, reviewed: int, total: int) -> None:
        self.stats_label.setText(f"{reviewed} / {total} reviewed")

    def focus_search(self) -> None:
        self.search_edit.setFocus()
        self.search_edit.selectAll()

    # --------------------------------------------------------------- filtering
    def _filter(self, text: str) -> None:
        self.searchRequested.emit(text)
        self._apply_filter()

    def _apply_filter(self) -> None:
        needle = self.search_edit.text().strip().lower()
        for i in range(self.list_widget.count()):
            it = self.list_widget.item(i)
            visible = (not needle) or (needle in it.text().lower())
            it.setHidden(not visible)


