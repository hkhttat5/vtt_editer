"""Main application window of the VTT correction tool."""
from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import (
    QAction, QKeySequence, QShortcut, QFont,
)
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPlainTextEdit,
    QPushButton, QLabel, QSlider, QFileDialog, QMessageBox, QDockWidget,
    QSizePolicy, QFrame, QApplication,
)

from core.project import Project
from core.vtt_parser import format_timestamp
from media.video_player import VideoPlayer
from ui.cue_list import CueListPanel

APP_TITLE = "VTT Corrector"

VIDEO_FILTER = (
    "Video files (*.mp4 *.mkv *.webm *.mov *.avi *.m4v *.wmv *.flv *.ts "
    "*.mpg *.mpeg);;All files (*)"
)
VTT_FILTER = "WebVTT subtitles (*.vtt);;All files (*)"


def fmt_time(ms: int) -> str:
    s, _ = divmod(int(ms), 1000)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


class MainWindow(QMainWindow):

    def __init__(self) -> None:
        super().__init__()
        self.project = Project()
        self.current_index: int = -1
        self._seeking_slider = False      # guard: sliderMoved vs valueChanged
        self._loading_cue = False         # guard while populating the editor

        self.setWindowTitle(APP_TITLE)
        self.resize(1180, 820)

        # ---------------------------------------------------------------- video
        self.video_player = VideoPlayer(self)
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        root.addWidget(self.video_player.video_widget, stretch=1)

        # ------------------------------------------------------- transport bar
        transport = QHBoxLayout()
        self.btn_play = QPushButton("▶ Play")
        self.btn_play.setFixedWidth(90)
        self.btn_play.clicked.connect(self.video_player.toggle_play_pause)
        transport.addWidget(self.btn_play)

        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setMinimumWidth(110)
        transport.addWidget(self.time_label)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.sliderPressed.connect(lambda: setattr(self, "_seeking_slider", True))
        self.slider.sliderReleased.connect(self._slider_released)
        self.slider.valueChanged.connect(self._slider_moved)
        transport.addWidget(self.slider, stretch=1)

        self.speed_label = QLabel("Speed:")
        self.btn_speed = QPushButton("1.0×")
        self.btn_speed.setCheckable(False)
        self.btn_speed.clicked.connect(self._cycle_speed)
        transport.addWidget(self.speed_label)
        transport.addWidget(self.btn_speed)
        self._speeds = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]
        self._speed_i = 2

        root.addLayout(transport)

        # ------------------------------------------------------------ cue panel
        panel = QFrame()
        panel.setFrameShape(QFrame.Shape.StyledPanel)
        pv = QVBoxLayout(panel)
        pv.setContentsMargins(10, 8, 10, 8)
        pv.setSpacing(6)

        head = QHBoxLayout()
        self.cue_counter_label = QLabel("Cue – / –")
        f = QFont(); f.setBold(True); f.setPointSize(f.pointSize() + 2)
        self.cue_counter_label.setFont(f)
        head.addWidget(self.cue_counter_label)
        head.addStretch(1)
        self.status_label = QLabel("○ Not reviewed")
        head.addWidget(self.status_label)
        pv.addLayout(head)

        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText(
            "Open a VTT file…   (Enter = save & next cue)"
        )
        self.editor.setTabChangesFocus(True)
        self.editor.installEventFilter(self)   # intercept Enter here
        size_policy = self.editor.sizePolicy()
        size_policy.setVerticalStretch(1)
        self.editor.setSizePolicy(size_policy)
        fe = QFont("Segoe UI", 13)
        self.editor.setFont(fe)
        pv.addWidget(self.editor, stretch=1)

        nav = QHBoxLayout()
        self.btn_prev = QPushButton("◀  Previous  (←)")
        self.btn_next = QPushButton("Next / Save  (Enter)  ▶")
        self.btn_next.setDefault(True)
        bfont = QFont(); bfont.setBold(True)
        self.btn_next.setFont(bfont)
        self.btn_prev.clicked.connect(self.goto_previous)
        self.btn_next.clicked.connect(self.commit_and_next)
        nav.addWidget(self.btn_prev)
        nav.addStretch(1)
        nav.addWidget(self.btn_next)
        pv.addLayout(nav)

        times = QHBoxLayout()
        self.start_label = QLabel("Start: –")
        self.end_label = QLabel("End:   –")
        for lbl in (self.start_label, self.end_label):
            lf = QFont("Consolas", 11)
            lbl.setFont(lf)
        times.addWidget(self.start_label)
        times.addSpacing(30)
        times.addWidget(self.end_label)
        times.addStretch(1)
        self.autosave_label = QLabel("")
        self.autosave_label.setStyleSheet("color: gray;")
        times.addWidget(self.autosave_label)
        pv.addLayout(times)

        root.addWidget(panel)

        # ------------------------------------------------------------- dock list
        self.cue_panel = CueListPanel(self)
        self.cue_panel.cueActivated.connect(self.goto_cue)
        self.dock = QDockWidget("Cues", self)
        self.dock.setWidget(self.cue_panel)
        self.dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock)

        # ---------------------------------------------------------------- menu
        self._build_menu()

        # ------------------------------------------------------------- signals
        self.video_player.positionChanged.connect(self._on_position)
        self.video_player.durationChanged.connect(self._on_duration)
        self.video_player.playbackStateChanged.connect(self._on_play_state)

        self._update_title()

    # ================================================================== menu
    def _build_menu(self) -> None:
        mb = self.menuBar()
        m_file = mb.addMenu("&File")

        act_video = QAction("&Open Video…", self)
        act_video.setShortcut(QKeySequence("Ctrl+O"))
        act_video.triggered.connect(self.open_video_dialog)
        m_file.addAction(act_video)

        act_vtt = QAction("Open &VTT…", self)
        act_vtt.setShortcut(QKeySequence("Ctrl+Shift+O"))
        act_vtt.triggered.connect(self.open_vtt_dialog)
        m_file.addAction(act_vtt)

        m_file.addSeparator()

        act_save = QAction("&Save VTT", self)
        act_save.setShortcut(QKeySequence.Save)          # Ctrl+S
        act_save.triggered.connect(self.save_current)
        m_file.addAction(act_save)
        self.act_save = act_save

        act_saveas = QAction("Save &As…", self)
        act_saveas.setShortcut(QKeySequence("Ctrl+Shift+S"))
        act_saveas.triggered.connect(self.save_as)
        m_file.addAction(act_saveas)

        m_file.addSeparator()

        act_quit = QAction("&Exit", self)
        act_quit.setShortcut(QKeySequence("Ctrl+Q"))
        act_quit.triggered.connect(self.close)
        m_file.addAction(act_quit)

        m_edit = mb.addMenu("&Edit")
        act_undo = QAction("&Undo", self)
        act_undo.setShortcut(QKeySequence.Undo)          # Ctrl+Z
        act_undo.triggered.connect(self.undo)
        m_edit.addAction(act_undo)

        act_redo = QAction("&Redo", self)
        act_redo.setShortcut(QKeySequence.Redo)          # Ctrl+Shift+Z
        act_redo.triggered.connect(self.redo)
        m_edit.addAction(act_redo)

        m_view = mb.addMenu("&View")
        act_dock = QAction("Show/&Hide Cue List", self)
        act_dock.setShortcut(QKeySequence("Ctrl+L"))
        act_dock.triggered.connect(
            lambda: self.dock.setVisible(not self.dock.isVisible())
        )
        m_view.addAction(act_dock)

        act_find = QAction("&Search Cues…", self)
        act_find.setShortcut(QKeySequence.Find)          # Ctrl+F
        act_find.triggered.connect(self.focus_search)
        m_view.addAction(act_find)

        # global shortcuts that must work even inside the text editor:
        sc_next = QShortcut(QKeySequence(Qt.Key.Key_Right), self)
        sc_next.setContext(Qt.ShortcutContext.WindowShortcut)
        sc_next.activated.connect(self.goto_next_unedited)

        sc_prev = QShortcut(QKeySequence(Qt.Key.Key_Left), self)
        sc_prev.setContext(Qt.ShortcutContext.WindowShortcut)
        sc_prev.activated.connect(self.goto_previous)

        sc_space = QShortcut(QKeySequence(Qt.Key.Key_Space), self)
        sc_space.setContext(Qt.ShortcutContext.WindowShortcut)
        sc_space.activated.connect(self.video_player.toggle_play_pause)

    # ================================================================ dialogs
    def open_video_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Video", "", VIDEO_FILTER)
        if not path:
            return
        if not self.video_player.open(path):
            QMessageBox.warning(self, APP_TITLE,
                                f"Could not open video:\n{path}")
            return
        self.project.video_path = path
        self.statusBar().showMessage(f"Video loaded: {os.path.basename(path)}", 4000)

    def open_vtt_dialog(self) -> None:
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Open VTT", "", VTT_FILTER)
        if not path:
            return
        try:
            doc = self.project.load_vtt(path)
        except Exception as exc:                      # noqa: BLE001
            QMessageBox.critical(self, APP_TITLE,
                                 f"Failed to parse VTT file:\n{exc}")
            return
        n = doc.cue_count
        if n == 0:
            QMessageBox.information(
                self, APP_TITLE, "No cues found in this file.")
        self.cue_panel.rebuild(self.project.cues, -1, 0)
        self._update_title()
        # jump to first unreviewed cue
        target = next((c.index for c in self.project.cues if not c.reviewed), 0)
        if n:
            self.goto_cue(target)
        self.statusBar().showMessage(
            f"Loaded {n} cues from {os.path.basename(path)}", 5000)

    # ============================================================ unsaved guard
    def _confirm_discard(self) -> bool:
        """Returns True when it is safe to continue (discard or saved)."""
        if not (self.project.document and self.project.dirty):
            return True
        box = QMessageBox(self)
        box.setWindowTitle(APP_TITLE)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText("You have unsaved changes.")
        box.setInformativeText("Save changes?")
        b_save = box.addButton(QMessageBox.StandardButton.Save)
        b_no = box.addButton("Don't Save", QMessageBox.ButtonRole.DestructiveRole)
        b_cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked is b_save:
            return self.save_current()
        if clicked is b_no:
            return True
        return False                                  # Cancel

    def closeEvent(self, event) -> None:              # noqa: N802
        if self._confirm_discard():
            event.accept()
        else:
            event.ignore()

    # ================================================================= saving
    def save_current(self) -> bool:
        if self.project.document is None:
            QMessageBox.information(self, APP_TITLE, "No VTT file is open.")
            return False
        # commit pending editor text first
        self._commit_editor_text(review=False)
        try:
            if self.project.document.path:
                path = self.project.save()
            else:
                return self.save_as()
        except OSError as exc:
            QMessageBox.critical(self, APP_TITLE, f"Save failed:\n{exc}")
            return False
        self._update_title()
        self.statusBar().showMessage(f"Saved: {path}", 4000)
        self._maybe_autosave()
        return True

    def save_as(self) -> bool:
        if self.project.document is None:
            QMessageBox.information(self, APP_TITLE, "No VTT file is open.")
            return False
        self._commit_editor_text(review=False)
        start = ""
        if self.project.document.path:
            base = os.path.basename(self.project.document.path)
            stem = os.path.splitext(base)[0]
            start = os.path.join(os.path.dirname(self.project.document.path),
                                 f"{stem}_corrected.vtt")
        elif self.project.video_path:
            stem = os.path.splitext(os.path.basename(self.project.video_path))[0]
            start = f"{stem}.vtt"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save VTT As", start, VTT_FILTER)
        if not path:
            return False
        if not path.lower().endswith(".vtt"):
            path += ".vtt"
        try:
            self.project.save(path)
        except OSError as exc:
            QMessageBox.critical(self, APP_TITLE, f"Save failed:\n{exc}")
            return False
        self._update_title()
        self.statusBar().showMessage(f"Saved: {path}", 4000)
        return True

    def _maybe_autosave(self) -> None:
        if self.project.should_autosave():
            p = self.project.do_autosave()
            if p:
                self.autosave_label.setText(
                    f"Autosaved → {os.path.basename(p)}")

    # ================================================================= undo/redo
    def undo(self) -> None:
        if self.project.document is None:
            return
        idx = self.project.undo()
        if idx is None:
            self.statusBar().showMessage("Nothing to undo", 2000)
            return
        self.cue_panel.update_row(idx, self.project.cue(idx))
        self.cue_panel.set_reviewed_stats(
            self.project.reviewed_count, self.project.cue_count)
        if idx == self.current_index:
            self._load_cue_into_editor(idx)
        self._update_title()

    def redo(self) -> None:
        if self.project.document is None:
            return
        idx = self.project.redo()
        if idx is None:
            self.statusBar().showMessage("Nothing to redo", 2000)
            return
        self.cue_panel.update_row(idx, self.project.cue(idx))
        self.cue_panel.set_reviewed_stats(
            self.project.reviewed_count, self.project.cue_count)
        if idx == self.current_index:
            self._load_cue_into_editor(idx)
        self._update_title()

    # ================================================================== cue nav
    def focus_search(self) -> None:
        self.dock.setVisible(True)
        self.cue_panel.focus_search()

    def goto_cue(self, index: int, seek: bool = True, play: bool = True) -> None:
        if self.project.document is None or not (0 <= index < self.project.cue_count):
            return
        # do NOT auto-commit text on plain navigation; instead keep the edit
        # visible if user presses Left/Right accidentally?  Spec says previous
        # must not lose edits → we commit current text before leaving.
        self._commit_editor_text(review=False)

        self.current_index = index
        self._load_cue_into_editor(index)
        cue = self.project.cue(index)
        if seek and self.video_player.has_media:
            self.video_player.seek_seconds(cue.start, play=play)
        self.cue_panel.highlight_current(index)
        self.cue_panel.set_reviewed_stats(
            self.project.reviewed_count, self.project.cue_count)

    def _load_cue_into_editor(self, index: int) -> None:
        cue = self.project.cue(index)
        self._loading_cue = True
        try:
            self.editor.setPlainText(cue.text)
            # select all so the user can immediately type over / edit
            cursor = self.editor.textCursor()
            cursor.select(cursor.SelectionType.Document)
            self.editor.setTextCursor(cursor)
            self.editor.setFocus()
        finally:
            self._loading_cue = False
        self.cue_counter_label.setText(
            f"Cue {index + 1} / {self.project.cue_count}")
        self._refresh_status_labels(cue)
        self.start_label.setText(f"Start: {cue.start_str}")
        self.end_label.setText(f"End:   {cue.end_str}")

    def _refresh_status_labels(self, cue) -> None:
        if cue.reviewed:
            self.status_label.setText("✓ Reviewed")
            self.status_label.setStyleSheet("color: green; font-weight: bold;")
        else:
            self.status_label.setText("○ Not reviewed")
            self.status_label.setStyleSheet("color: gray;")

    # -------------------------------------------------------------- Enter flow
    def commit_and_next(self) -> None:
        """THE key action: save current text, mark reviewed, go to next cue."""
        if self.project.document is None or self.current_index < 0:
            return
        text = self.editor.toPlainText()
        idx = self.current_index
        changed = self.project.set_cue_text(idx, text, reviewed=True)
        self.cue_panel.update_row(idx, self.project.cue(idx))
        self.cue_panel.set_reviewed_stats(
            self.project.reviewed_count, self.project.cue_count)
        if changed:
            self._update_title()
        self._maybe_autosave()

        nxt = idx + 1
        if nxt < self.project.cue_count:
            self.goto_cue(nxt, seek=True, play=True)
        else:
            self.video_player.pause()
            self.statusBar().showMessage(
                "Reached last cue. Press Ctrl+S to save.", 6000)

    def goto_previous(self) -> None:
        if self.project.document is None:
            return
        idx = max(0, self.current_index - 1)
        self.goto_cue(idx, seek=True, play=True)

    def goto_next_unedited(self) -> None:
        """Arrow Right: move without touching the text."""
        if self.project.document is None:
            return
        idx = min(self.project.cue_count - 1, self.current_index + 1)
        self.goto_cue(idx, seek=True, play=True)

    def _commit_editor_text(self, review: bool) -> None:
        """Write current editor content into the cue model (no review flag)."""
        if self.project.document is None or self.current_index < 0:
            return
        if self._loading_cue:
            return
        text = self.editor.toPlainText()
        cue = self.project.cue(self.current_index)
        if cue.text != text:
            self.project.set_cue_text(
                self.current_index, text, reviewed=cue.reviewed or review)
            self.cue_panel.update_row(self.current_index, cue)

    # --------------------------------------------------------- editor keypress
    def eventFilter(self, obj, event):               # noqa: N802
        if obj is self.editor and event.type() == event.Type.KeyPress:
            if (event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                    and not event.modifiers()):
                self.commit_and_next()
                return True
        return super().eventFilter(obj, event)

    # =============================================================== transport
    def _on_position(self, ms: int) -> None:
        if not self._seeking_slider:
            dur = self.video_player.duration_ms
            self.slider.setValue(ms)
            self.time_label.setText(f"{fmt_time(ms)} / {fmt_time(dur)}")

    def _on_duration(self, ms: int) -> None:
        self.slider.setRange(0, max(0, ms))
        self.time_label.setText(
            f"{fmt_time(self.video_player.position_ms)} / {fmt_time(ms)}")

    def _on_play_state(self, state: str) -> None:
        self.btn_play.setText("⏸ Pause" if state == "playing" else "▶ Play")

    def _slider_moved(self, value: int) -> None:
        if self._seeking_slider:
            self.time_label.setText(
                f"{fmt_time(value)} / {fmt_time(self.video_player.duration_ms)}")

    def _slider_released(self) -> None:
        self._seeking_slider = False
        self.video_player.seek_ms(self.slider.value())

    def _cycle_speed(self) -> None:
        self._speed_i = (self._speed_i + 1) % len(self._speeds)
        s = self._speeds[self._speed_i]
        self.btn_speed.setText(f"{s:g}×")
        self.video_player.set_speed(s)

    # ==================================================================== misc
    def _update_title(self) -> None:
        name = "–"
        if self.project.document and self.project.document.path:
            name = os.path.basename(self.project.document.path)
        dirty = "*" if (self.project.document and self.project.dirty) else ""
        self.setWindowTitle(f"{APP_TITLE}{dirty} — {name}")
