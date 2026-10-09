"""Authoritative subtitle table for the VTT review interface.

Columns: ID | Start | End | Duration | German Text | Validation Status.

The table is a *view* over the single authoritative cue list held by the
:class:`core.project.Project`; it never keeps its own copy of the data.
Sorting is chronological (by start time) and reorders only the view — the
cue timings themselves are never touched implicitly.

Editing: double-click Start / End / Text to edit; edits are validated by the
caller through :attr:`editCommitted` before they reach the model.
"""
from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
    QStyle,
)

# validation-status filter modes
FILTER_ALL = 0
FILTER_ERRORS = 1
FILTER_WARNINGS = 2
FILTER_UNREVIEWED = 3
FILTER_REVIEWED = 4

SEVERITY_COLORS = {
    "error":   QColor(255, 205, 205),
    "warning": QColor(255, 238, 200),
    "info":    QColor(224, 235, 255),
}
STATUS_ICONS = {"error": "✖", "warning": "⚠", "info": "ℹ"}


class SubtitleTable(QWidget):
    """Six-column editable subtitle table with search & status filtering."""

    cueSelected = Signal(int)        # cue index (model index, not row)
    editCommitted = Signal(int, str, object)  # (cue index, column key, new value)
    addRequested = Signal()

    COL_ID, COL_START, COL_END, COL_DURATION, COL_TEXT, COL_STATUS = range(6)
    EDITABLE_COLS = {COL_START: "start", COL_END: "end", COL_TEXT: "text"}

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._cues = []                       # live list reference from Project
        self._row_map: List[int] = []         # row -> cue index
        self._severity: dict = {}             # cue index -> worst severity
        self._updating = False
        self._filter_mode = FILTER_ALL
        self._search = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # ---- toolbar: search + filter ------------------------------------
        bar = QHBoxLayout()
        bar.addWidget(QLabel("🔍"))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search German text…  (ä ö ü ß supported)")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self.set_search)
        bar.addWidget(self.search_edit, stretch=1)

        self.filter_combo = QComboBox()
        for label in ("All cues", "Errors", "Warnings",
                      "Unreviewed cues", "Reviewed cues"):
            self.filter_combo.addItem(label)
        self.filter_combo.currentIndexChanged.connect(self._on_filter_changed)
        bar.addWidget(self.filter_combo)

        self.btn_add = None  # wired by the main window
        layout.addLayout(bar)

        # ---- the table ----------------------------------------------------
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["ID", "Start", "End", "Duration", "German Text", "Validation"])
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(self.COL_TEXT, QHeaderView.ResizeMode.Stretch)
        for col in (self.COL_ID, self.COL_DURATION, self.COL_STATUS):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(self.COL_ID, 46)
        self.table.setColumnWidth(self.COL_START, 120)
        self.table.setColumnWidth(self.COL_END, 120)
        self.table.setColumnWidth(self.COL_DURATION, 70)
        self.table.setColumnWidth(self.COL_STATUS, 90)

        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                   | QAbstractItemView.EditTrigger.SelectedClicked
                                   | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.table.verticalHeader().setVisible(False)
        mono = QFont("Consolas", 10)
        self.table.setFont(mono)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self.table.itemChanged.connect(self._on_item_changed)
        # Qt's default item delegate already commits an in-place edit on
        # Enter and cancels it on Escape — no extra wiring is required.
        layout.addWidget(self.table, stretch=1)

        self.count_label = QLabel("0 cues")
        self.count_label.setStyleSheet("color: gray;")
        layout.addWidget(self.count_label)

    # ------------------------------------------------------------------ api
    def set_cues(self, cues) -> None:
        """Bind the table to the authoritative cue list (no copy)."""
        self._cues = cues
        self.refresh()

    def set_validation(self, severity_by_index: dict) -> None:
        self._severity = dict(severity_by_index)
        self.refresh(preserve_selection=True)

    def current_cue_index(self) -> int:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return -1
        r = rows[0].row()
        return self._row_map[r] if 0 <= r < len(self._row_map) else -1

    def select_cue(self, index: int, scroll: bool = True) -> None:
        """Select the row showing *index* (keeps selection after refreshes)."""
        self._updating = True
        try:
            if index in self._row_map:
                row = self._row_map.index(index)
                from PySide6.QtCore import QItemSelectionModel as _QISM
                self.table.selectionModel().select(
                    self.table.model().index(row, 0),
                    _QISM.SelectionFlag.ClearAndSelect
                    | _QISM.SelectionFlag.Rows)
                if scroll:
                    self.table.scrollTo(
                        self.table.model().index(row, 0),
                        QAbstractItemView.ScrollHint.PositionAtCenter)
            else:
                self.table.clearSelection()
        finally:
            self._updating = False

    def visible_cue_indices(self) -> List[int]:
        return list(self._row_map)

    # ------------------------------------------------------------- rendering
    def refresh(self, preserve_selection: bool = True) -> None:
        selected = self.current_cue_index() if preserve_selection else -1
        self._updating = True
        try:
            self._rebuild_rows()
        finally:
            self._updating = False
        if preserve_selection and selected >= 0:
            self.select_cue(selected, scroll=False)
        elif self._row_map and not preserve_selection:
            self.select_cue(self._row_map[0], scroll=False)

    def _matches(self, cue) -> bool:
        if self._search:
            if self._search not in (cue.text or "").lower():
                return False
        mode = self._filter_mode
        sev = self._severity.get(cue.index)
        if mode == FILTER_ERRORS:
            return sev == "error"
        if mode == FILTER_WARNINGS:
            return sev == "warning"
        if mode == FILTER_UNREVIEWED:
            return not cue.reviewed
        if mode == FILTER_REVIEWED:
            return cue.reviewed
        return True

    def _rebuild_rows(self) -> None:
        self.table.setRowCount(0)
        self._row_map = []
        # chronological view — sorting NEVER changes cue timings
        ordered = sorted(self._cues, key=lambda c: (c.start, c.end))
        visible = [c for c in ordered if self._matches(c)]
        self.table.setRowCount(len(visible))
        for row, cue in enumerate(visible):
            self._row_map.append(cue.index)
            sev = self._severity.get(cue.index)
            bg = SEVERITY_COLORS.get(sev)

            id_item = QTableWidgetItem(str(cue.index + 1))
            id_item.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                     | Qt.AlignmentFlag.AlignVCenter)
            start_item = QTableWidgetItem(cue.start_str)
            end_item = QTableWidgetItem(cue.end_str)
            dur_item = QTableWidgetItem(f"{cue.duration:.3f}")
            dur_item.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                      | Qt.AlignmentFlag.AlignVCenter)
            first_line = (cue.text or "").replace("\n", " ⏎ ")
            if len(first_line) > 120:
                first_line = first_line[:117] + "…"
            text_item = QTableWidgetItem(first_line)
            font = text_item.font()
            font.setFamily("Segoe UI")
            text_item.setFont(font)
            mark = "✓" if cue.reviewed else "○"
            if sev in STATUS_ICONS:
                mark = f"{mark} {STATUS_ICONS[sev]}"
            status_item = QTableWidgetItem(mark)
            status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            for col, item in enumerate(
                    (id_item, start_item, end_item, dur_item, text_item, status_item)):
                item.setData(Qt.ItemDataRole.UserRole, cue.index)
                if col in (self.COL_ID, self.COL_DURATION, self.COL_STATUS):
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if col in (self.COL_START, self.COL_END):
                    item.setData(Qt.ItemDataRole.EditRole, item.text())
                if bg is not None and col != self.COL_TEXT:
                    item.setBackground(bg)
                if cue.reviewed and col == self.COL_TEXT:
                    item.setForeground(QColor(0, 110, 0))
                self.table.setItem(row, col, item)

        total = len(self._cues)
        shown = len(visible)
        self.count_label.setText(
            f"{shown} / {total} cues" if shown != total else f"{total} cues")

    # ------------------------------------------------------------ callbacks
    def _on_filter_changed(self, idx: int) -> None:
        self._filter_mode = idx
        self.refresh()

    def set_search(self, text: str) -> None:
        self._search = (text or "").strip().lower()
        self.refresh()

    def focus_search(self) -> None:
        self.search_edit.setFocus()
        self.search_edit.selectAll()

    def _on_selection_changed(self) -> None:
        if self._updating:
            return
        idx = self.current_cue_index()
        if idx >= 0:
            self.cueSelected.emit(idx)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating:
            return
        col = item.column()
        key = self.EDITABLE_COLS.get(col)
        if key is None:
            return
        idx = item.data(Qt.ItemDataRole.UserRole)
        if idx is None:
            return
        raw = item.data(Qt.ItemDataRole.DisplayRole)
        self.editCommitted.emit(int(idx), key, raw)

    def revert_cell(self, cue_index: int, col: int, cue) -> None:
        """Restore a cell's text after a rejected edit."""
        row = self._row_map.index(cue_index) if cue_index in self._row_map else -1
        if row < 0:
            return
        self._updating = True
        try:
            texts = {self.COL_START: cue.start_str, self.COL_END: cue.end_str,
                     self.COL_TEXT: (cue.text or "").replace("\n", " ⏎ ")}
            it = self.table.item(row, col)
            if it is not None:
                it.setText(texts.get(col, ""))
        finally:
            self._updating = False
