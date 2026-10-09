"""Main application window of the VTT correction tool.

Layout:
    * video preview (existing VideoPlayer / QVideoWidget) + transport bar
      (play/pause, seek slider, time labels, frame-step buttons);
    * authoritative subtitle table (ID | Start | End | Duration | German
      Text | Validation) — clicking a row PAUSES playback and seeks to the
      cue's start timestamp;
    * dedicated selected-cue editor with editable start/end timestamps,
      live duration and Apply / Revert / Delete / Split / Merge / Review;
    * collapsible side list for fast text navigation (kept from earlier UI).

Keyboard handling is focus-aware: Space / Left / Right drive the video
whenever the video widget, transport bar or subtitle table has focus, but
are left completely alone inside text editors, timestamp inputs and search
fields (there they type spaces / move the caret as usual).
"""
from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence, QShortcut, QFont
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPlainTextEdit,
    QPushButton, QLabel, QSlider, QFileDialog, QMessageBox, QDockWidget,
    QSizePolicy, QFrame, QApplication,
)

from core.project import Project, EditError
from core.vtt_parser import format_timestamp
from export.vtt_validator import validate_cues, parse_user_timestamp
from media.video_player import VideoPlayer
from media.frame_probe import probe_fps
from ui.cue_list import CueListPanel
from ui.results_table import SubtitleTable
from ui.vtt_editor import CueEditorPanel, SplitDialog, MergeDialog

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
        self._programmatic_select = False # guard: refresh must not re-emit
        self._suppress_position_sync = False  # set during a deliberate seek
        self._last_playback_state: str = "stopped"
        self._step_interval_s = 1.0       # configurable jump interval
        self._add_pending = False         # editor showing a brand-new cue

        self.setWindowTitle(APP_TITLE)
        self.resize(1280, 860)

        # ---------------------------------------------------------------- video
        self.video_player = VideoPlayer(self)
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        root.addWidget(self.video_player.video_widget, stretch=3)

        # ------------------------------------------------------- transport bar
        transport = QHBoxLayout()
        self.btn_play = QPushButton("▶ Play")
        self.btn_play.setFixedWidth(90)
        self.btn_play.clicked.connect(self.video_player.toggle_play_pause)
        transport.addWidget(self.btn_play)

        self.btn_frame_prev = QPushButton("⏮ −1 Frame")
        self.btn_frame_prev.setToolTip(
            "Pause and step back exactly one frame (Left Arrow)")
        self.btn_frame_prev.clicked.connect(lambda: self.step_frame(-1))
        transport.addWidget(self.btn_frame_prev)

        self.btn_frame_next = QPushButton("+1 Frame ⏭")
        self.btn_frame_next.setToolTip(
            "Pause and step forward exactly one frame (Right Arrow)")
        self.btn_frame_next.clicked.connect(lambda: self.step_frame(1))
        transport.addWidget(self.btn_frame_next)

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
        self.validation_summary_label = QLabel("")
        self.validation_summary_label.setStyleSheet("color: gray;")
        head.addWidget(self.validation_summary_label)
        pv.addLayout(head)

        # the dedicated selected-cue editor (start/end/text + structure ops)
        self.cue_editor = CueEditorPanel()
        self.cue_editor.video_position_provider = self._video_position_seconds
        self.cue_editor.applyRequested.connect(self._on_editor_apply)
        self.cue_editor.deleteRequested.connect(self._on_delete_cue)
        self.cue_editor.splitRequested.connect(self._on_split_cue)
        self.cue_editor.mergeRequested.connect(self._on_merge_cue)
        self.cue_editor.reviewRequested.connect(self._on_review_requested)
        self.cue_editor.prevRequested.connect(self.goto_previous)
        self.cue_editor.nextRequested.connect(self.goto_next_unedited)
        self.cue_editor.seekToStartRequested.connect(self._seek_to_seconds)
        self.cue_editor.seekToEndRequested.connect(self._seek_to_seconds)
        self.cue_editor.stepSecondsRequested.connect(self._on_step_request)
        self.cue_editor.btn_revert.clicked.connect(self._on_editor_revert)
        pv.addWidget(self.cue_editor)

        # legacy plain-text editor kept in sync with the cue text so the
        # established Enter = save & next workflow keeps working
        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText(
            "Open a VTT file…   (Enter = save & next cue)"
        )
        self.editor.setTabChangesFocus(True)
        self.editor.setMaximumHeight(96)
        self.editor.installEventFilter(self)   # intercept Enter here
        size_policy = self.editor.sizePolicy()
        size_policy.setVerticalStretch(1)
        self.editor.setSizePolicy(size_policy)
        fe = QFont("Segoe UI", 13)
        self.editor.setFont(fe)
        pv.addWidget(self.editor)
        self.editor.textChanged.connect(self._on_legacy_editor_changed)

        nav = QHBoxLayout()
        self.btn_prev = QPushButton("◀  Previous")
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

        # ------------------------------------------------- authoritative table
        self.table = SubtitleTable()
        self.table.editCommitted.connect(self._on_table_edit)
        self.table.cueSelected.connect(self._on_table_selection)
        root.addWidget(self.table, stretch=2)

        # ------------------------------------------------------------- dock list
        self.cue_panel = CueListPanel(self)
        # Clicking a cue in the side list jumps to it AND loops its segment
        self.cue_panel.cueActivated.connect(self.goto_cue_looping)
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

        m_edit.addSeparator()
        act_add = QAction("&Add Subtitle…", self)
        act_add.setShortcut(QKeySequence("Ctrl+N"))
        act_add.triggered.connect(self.add_subtitle)
        m_edit.addAction(act_add)

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

        # NOTE: Space / Left / Right are NOT registered as WindowShortcuts:
        # a WindowShortcut fires regardless of focus and would insert spaces
        # into line edits or move the caret in the text editor.  They are
        # handled in keyPressEvent instead, which only receives them when
        # focus is on the video, transport bar or subtitle table.

    # ------------------------------------------------------------------ keys
    @staticmethod
    def _is_text_focus(w: Optional[QWidget]) -> bool:
        """True when *w* (or a child of it) needs raw Space/Arrow keys."""
        if w is None:
            return False
        if isinstance(w, (QPlainTextEdit,)):
            return True
        if hasattr(w, "setCompleter"):          # QLineEdit family
            return True
        if w.inherits("QAbstractSpinBox"):
            return True
        if w.inherits("QTableView"):            # in-place cell editors
            return True
        if w.inherits("QComboBox") or w.inherits("QListWidget"):
            return True
        return False

    def _video_key_context(self, w: Optional[QWidget]) -> bool:
        """True when *w* is a widget where Space/arrows control the video.

        Text widgets are excluded first (see :meth:`_is_text_focus`), so
        typing spaces or moving the caret inside editors/inputs/search
        fields is never intercepted.
        """
        if w is None:
            return True                      # window itself has focus
        if w is self.video_player.video_widget:
            return True
        if w.window() is not self:
            return False                     # dialog popup etc.
        # transport bar buttons / slider
        for b in (self.btn_play, self.btn_frame_prev, self.btn_frame_next,
                  self.slider, self.btn_speed):
            if w is b:
                return True
        # subtitle table viewport / header / empty area
        tv = self.table.table.viewport()
        if w is tv or w is self.table.table or w is self.table.table.verticalHeader():
            return True
        hdr = self.table.table.horizontalHeader()
        if hdr is not None and (w is hdr or hdr.isAncestorOf(w)):
            return True
        # dock list panel background
        lw = self.cue_panel.list_widget.viewport()
        if w is lw or w is self.cue_panel.list_widget:
            return True
        return False

    def keyPressEvent(self, event) -> None:              # noqa: N802
        key = event.key()
        if event.modifiers() == Qt.KeyboardModifier.NoModifier:
            fw = QApplication.focusWidget()
            texty = self._is_text_focus(fw)
            if key == Qt.Key.Key_Space:
                if not texty and (fw is None or fw is self
                                  or self._video_key_context(fw)):
                    self.video_player.toggle_play_pause()
                    event.accept()               # single toggle, no repeat
                    return
            elif key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
                if not texty and (fw is None or fw is self
                                  or self._video_key_context(fw)):
                    direction = -1 if key == Qt.Key.Key_Left else 1
                    if self.project.document and self.project.cue_count:
                        # review workflow: navigate cues (pause + seek)
                        self._goto_relative(direction)
                    else:
                        self.step_frame(direction)
                    event.accept()
                    return
            elif key in (Qt.Key.Key_Comma, Qt.Key.Key_Period):
                if not texty and (fw is None or fw is self
                                  or self._video_key_context(fw)):
                    self.step_frame(-1 if key == Qt.Key.Key_Comma else 1)
                    event.accept()
                    return
        super().keyPressEvent(event)

    def event(self, e) -> bool:                          # noqa: N802
        """Catch Space/arrow presses that QPushButton/QSlider consume."""
        et = e.type()
        if et == e.Type.KeyPress:
            fw = QApplication.focusWidget()
            if fw in (self.btn_play, self.btn_frame_prev,
                      self.btn_frame_next, self.btn_speed, self.slider):
                synthetic = type(e)(e.Type.KeyPress, e.key(),
                                    e.modifiers(), e.text(),
                                    e.isAutoRepeat(), e.count())
                self.keyPressEvent(synthetic)
                return True
        elif et == e.Type.ShortcutOverride:
            fw = QApplication.focusWidget()
            if (e.modifiers() == Qt.KeyboardModifier.NoModifier
                    and e.key() in (Qt.Key.Key_Space, Qt.Key.Key_Left,
                                    Qt.Key.Key_Right, Qt.Key.Key_Comma,
                                    Qt.Key.Key_Period)
                    and not self._is_text_focus(fw)
                    and (fw is None or fw is self
                         or self._video_key_context(fw))):
                return True          # claim it before QPushButton activates
        return super().event(e)

    # ================================================================ dialogs
    def open_video_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Video", "", VIDEO_FILTER)
        if not path:
            return
        self.load_video(path)

    def load_video(self, path: str) -> bool:
        if not self.video_player.open(path):
            QMessageBox.warning(self, APP_TITLE,
                                f"Could not open video:\n{path}")
            return False
        self.project.video_path = path
        # Register the true source FPS for exact frame stepping.  The probe
        # uses a short-lived OpenCV capture that is released immediately;
        # it never runs concurrently with the player's decoder.
        fps = probe_fps(path)
        if fps:
            self.video_player.set_fps(fps)
        self.statusBar().showMessage(f"Video loaded: {os.path.basename(path)}", 4000)
        return True

    def open_vtt_dialog(self) -> None:
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Open VTT", "", VTT_FILTER)
        if not path:
            return
        self.load_vtt(path)

    def load_vtt(self, path: str) -> bool:
        try:
            doc = self.project.load_vtt(path)
        except Exception as exc:                      # noqa: BLE001
            QMessageBox.critical(self, APP_TITLE,
                                 f"Failed to parse VTT file:\n{exc}")
            return False
        n = doc.cue_count
        if n == 0:
            QMessageBox.information(
                self, APP_TITLE, "No cues found in this file.")
        self.refresh_document()
        self._update_title()
        # jump to first unreviewed cue
        target = next((c.index for c in self.project.cues if not c.reviewed), 0)
        if n:
            self.goto_cue(target, seek=True, play=False)
        self.statusBar().showMessage(
            f"Loaded {n} cues from {os.path.basename(path)}", 5000)
        return True

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
        if not self._pre_save_validation_ok():
            return False
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
        if not self._pre_save_validation_ok():
            return False
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

    def _pre_save_validation_ok(self) -> bool:
        """Warn about invalid timestamps before exporting; user decides."""
        summary = self.validate_now(refresh_ui=False)
        errors = len(summary.errors)
        warnings = len(summary.warnings)
        if errors == 0 and warnings == 0:
            return True
        box = QMessageBox(self)
        box.setWindowTitle(APP_TITLE)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(
            f"Validation found {errors} error(s) and {warnings} warning(s).")
        first = summary.issues[0].message if summary.issues else ""
        box.setInformativeText(
            f"Example: {first}\n\n"
            "Errors may produce a VTT file players cannot read.\n"
            "Save anyway, or cancel to fix the highlighted cues first?")
        b_save = box.addButton("Save anyway", QMessageBox.ButtonRole.AcceptRole)
        b_cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(b_cancel)
        box.exec()
        return box.clickedButton() is b_save

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
        anchor = self.project.cue(self.current_index)             if 0 <= self.current_index < self.project.cue_count else None
        if self.project.undo():
            if anchor is not None:
                # restore selection by stable cue identity, not row number
                for c in self.project.cues:
                    if c is anchor or (c.start, c.end, c.text) ==                             (anchor.start, anchor.end, anchor.text):
                        self.current_index = c.index
                        break
                else:
                    self.current_index = min(
                        self.current_index, self.project.cue_count - 1)
            self.refresh_document(preserve_selection=True)
            self._update_title()
            self.statusBar().showMessage("Undo", 1500)
        else:
            self.statusBar().showMessage("Nothing to undo", 2000)

    def redo(self) -> None:
        if self.project.document is None:
            return
        if self.project.redo():
            self.refresh_document(preserve_selection=True)
            self._update_title()
            self.statusBar().showMessage("Redo", 1500)
        else:
            self.statusBar().showMessage("Nothing to redo", 2000)

    # ================================================================== model→UI
    def refresh_document(self, preserve_selection: bool = True) -> None:
        """Re-render every view from the authoritative cue list."""
        if self.project.document is None:
            return
        self.table.set_cues(self.project.cues)
        self.cue_panel.rebuild(self.project.cues, self.current_index,
                               self.project.reviewed_count)
        self.validate_now()
        if 0 <= self.current_index < self.project.cue_count:
            self._add_pending = False
            self._load_cue_into_editor(self.current_index,
                                       seek=False,
                                       preserve=preserve_selection)
        else:
            self.current_index = -1
            self.cue_editor.clear()

    def validate_now(self, refresh_ui: bool = True):
        """Run the pure validator over the current cues (never mutates)."""
        if self.project.document is None:
            return None
        summary = validate_cues(self.project.cues,
                                self.video_player.duration_seconds)
        e, w, i = summary.counts()
        self.validation_summary_label.setText(
            f"✖ {e} errors · ⚠ {w} warnings · ℹ {i} info")
        if refresh_ui:
            self.table.set_validation(summary.indices_with_severity())
        return summary

    # ================================================================== cue nav
    def focus_search(self) -> None:
        self.dock.setVisible(True)
        self.cue_panel.focus_search()

    def goto_cue(self, index: int, seek: bool = True, play: bool = False,
                 loop: bool = False) -> None:
        if self.project.document is None or not (0 <= index < self.project.cue_count):
            return
        # keep any pending text edit before leaving the cue
        self._commit_editor_text(review=False)

        was_playing = self.video_player.is_playing
        self.current_index = index
        self._load_cue_into_editor(index, seek=seek, play=play, loop=loop)
        self.cue_panel.highlight_current(index)
        self.cue_panel.set_reviewed_stats(
            self.project.reviewed_count, self.project.cue_count)
        if was_playing and not play:
            # selecting a different subtitle pauses playback (feature req.)
            self.video_player.pause()

    def _load_cue_into_editor(self, index: int, seek: bool = True,
                              play: bool = False, loop: bool = False,
                              preserve: bool = True) -> None:
        cue = self.project.cue(index)
        self._loading_cue = True
        try:
            self.editor.setPlainText(cue.text)
            self.cue_editor.load_cue(index, cue)
            self.cue_editor.enable_navigation(
                index > 0, index + 1 < self.project.cue_count)
            # select all so the user can immediately type over / edit
            cursor = self.editor.textCursor()
            cursor.select(cursor.SelectionType.Document)
            self.editor.setTextCursor(cursor)
        finally:
            self._loading_cue = False
        self.cue_counter_label.setText(
            f"Cue {index + 1} / {self.project.cue_count}")
        self._refresh_status_labels(cue)
        self.start_label.setText(f"Start: {cue.start_str}")
        self.end_label.setText(f"End:   {cue.end_str}")
        # keep the table selection in sync (stable by cue index, not row)
        self._programmatic_select = True
        try:
            self.table.select_cue(index)
        finally:
            self._programmatic_select = False

        if seek and self.video_player.has_media:
            self._suppress_position_sync = True
            self.video_player.disable_loop()
            self.video_player.seek_seconds(cue.start, play=play)
            if loop:
                # Loop this cue's segment so the user can watch it repeatedly
                # while comparing with the VTT text.
                self.video_player.enable_loop_for_range(cue.start, cue.end)
                self.statusBar().showMessage(
                    f"🔁 Looping cue {index + 1} "
                    f"[{cue.start_str} → {cue.end_str}]  "
                    "(Space / manual seek stops the loop)", 5000)

    def _refresh_status_labels(self, cue) -> None:
        if cue.reviewed:
            self.status_label.setText("✓ Reviewed")
            self.status_label.setStyleSheet("color: green; font-weight: bold;")
        else:
            self.status_label.setText("○ Not reviewed")
            self.status_label.setStyleSheet("color: gray;")

    def goto_cue_looping(self, index: int) -> None:
        """Cue-list click: jump to the cue and loop its video segment."""
        self.goto_cue(index, seek=True, play=True, loop=True)

    # --------------------------------------------------- table interaction
    def _on_table_selection(self, index: int) -> None:
        if self._programmatic_select or self._updating_from_model:
            return
        # Requirement: clicking a row pauses playback and seeks to the cue
        # start; playback is never resumed automatically.
        self.goto_cue(index, seek=True, play=False)

    # flag used by callbacks below (defined early for safety)
    _updating_from_model = False

    def _on_table_edit(self, index: int, key: str, raw) -> None:
        """A cell was edited in-place in the subtitle table."""
        if self.project.document is None or not (0 <= index < self.project.cue_count):
            return
        cue = self.project.cue(index)
        try:
            if key == "text":
                new_text = str(raw)
                if new_text == cue.text:
                    return
                self.project.set_cue_text(index, new_text, reviewed=cue.reviewed)
            else:  # start / end timestamp
                value = parse_user_timestamp(str(raw))
                start = value if key == "start" else cue.start
                end = value if key == "end" else cue.end
                changed = self.project.set_cue_timing(index, start=start, end=end)
                if not changed:
                    return
        except (ValueError, EditError) as exc:
            QMessageBox.warning(self, APP_TITLE,
                                f"Invalid {key} timestamp: {exc}\n\n"
                                "The previous valid value was kept.")
            col = {"start": self.table.COL_START, "end": self.table.COL_END,
                   "text": self.table.COL_TEXT}[key]
            self.table.revert_cell(index, col, self.project.cue(index))
            return
        self._updating_from_model = True
        try:
            self.refresh_document(preserve_selection=True)
        finally:
            self._updating_from_model = False
        self._update_title()
        if key == "start":
            self.statusBar().showMessage(
                f"Cue {index + 1} start updated — seeking to the new time.", 3000)
            if self.video_player.has_media:
                self.video_player.disable_loop()
                self.video_player.seek_seconds(self.project.cue(index).start,
                                               play=False)

    # ------------------------------------------------- editor panel actions
    def _on_editor_apply(self, index: int, text: str, start: float,
                         end: float) -> None:
        if self.project.document is None:
            return
        if index < 0:
            # "Add Subtitle": the editor panel has no cue loaded yet —
            # create a new cue from the pending values instead.
            try:
                if index == -1 and self._add_pending:
                    if not (text or "").strip():
                        self.cue_editor.set_error(
                            "New subtitle text must not be empty.")
                        return
                    new_idx = self.project.add_cue(start, end, text)
                    self._add_pending = False
                    self.current_index = new_idx
                    self._updating_from_model = True
                    try:
                        self.refresh_document()
                    finally:
                        self._updating_from_model = False
                    self._update_title()
                    if self.video_player.has_media:
                        self.video_player.disable_loop()
                        self.video_player.seek_seconds(start, play=False)
                    self.statusBar().showMessage(
                        f"Added subtitle {new_idx + 1}.", 3000)
                else:
                    self.cue_editor.set_error("No subtitle selected.")
            except (ValueError, EditError) as exc:
                self.cue_editor.set_error(str(exc))
            return
        if not (0 <= index < self.project.cue_count):
            return
        cue = self.project.cue(index)
        timing_changed = abs(cue.start - start) > 1e-9 or abs(cue.end - end) > 1e-9
        text_changed = text != cue.text
        if not timing_changed and not text_changed:
            self.cue_editor.set_error("Nothing to apply — values unchanged.")
            return
        try:
            if text_changed:
                self.project.set_cue_text(index, text, reviewed=cue.reviewed)
                # keep the legacy editor in sync (no re-entrant loop)
                self._sync_legacy_editor(text)
            if timing_changed:
                self.project.set_cue_timing(index, start=start, end=end)
        except (ValueError, EditError) as exc:
            self.cue_editor.set_error(str(exc))
            return
        self._updating_from_model = True
        try:
            self.current_index = index
            self.refresh_document(preserve_selection=True)
        finally:
            self._updating_from_model = False
        self._update_title()
        self.statusBar().showMessage(
            f"Cue {index + 1} updated (duration "
            f"{self.project.cue(index).duration:.3f}s).", 3000)
        if timing_changed and self.video_player.has_media:
            # offer to seek to the new start time
            if QMessageBox.question(
                    self, APP_TITLE,
                    f"Seek the video to the new start time "
                    f"{self.project.cue(index).start_str}?",
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes) == \
                    QMessageBox.StandardButton.Yes:
                self.video_player.disable_loop()
                self.video_player.seek_seconds(
                    self.project.cue(index).start, play=False)

    def _on_delete_cue(self, index: int) -> None:
        if self.project.document is None or not (0 <= index < self.project.cue_count):
            return
        cue = self.project.cue(index)
        ret = QMessageBox.question(
            self, APP_TITLE,
            f"Delete subtitle {index + 1}?\n\n"
            f"[{cue.start_str} → {cue.end_str}]\n{cue.text}\n\n"
            "This can be undone with Ctrl+Z.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            return
        self.project.delete_cue(index)
        self.current_index = min(index, self.project.cue_count - 1)
        self._updating_from_model = True
        try:
            self.refresh_document()
        finally:
            self._updating_from_model = False
        self._update_title()

    def _on_split_cue(self, index: int) -> None:
        if self.project.document is None or not (0 <= index < self.project.cue_count):
            return
        cue = self.project.cue(index)
        dlg = SplitDialog(cue, self)
        if dlg.exec() != SplitDialog.DialogCode.Accepted:
            return
        try:
            split_at, t1, t2 = dlg.values()
            second_idx = self.project.split_cue(index, split_at, t1, t2)
        except (ValueError, EditError) as exc:
            QMessageBox.warning(self, APP_TITLE, str(exc))
            return
        self.current_index = second_idx
        self._updating_from_model = True
        try:
            self.refresh_document()
        finally:
            self._updating_from_model = False
        self._update_title()
        self.statusBar().showMessage(
            f"Cue {index + 1} split at {format_timestamp(split_at)}.", 4000)

    def _on_merge_cue(self, index: int) -> None:
        if self.project.document is None:
            return
        try:
            preview = self.project.merge_preview(index)
        except EditError as exc:
            QMessageBox.warning(self, APP_TITLE, str(exc))
            return
        gap = preview["gap"]
        if gap < -0.01 or gap > 0.05:
            kind = "overlap" if gap < 0 else "gap"
            ret = QMessageBox.question(
                self, APP_TITLE,
                f"There is a {kind} of {abs(gap):.3f}s between the two cues.\n"
                "Merge anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if ret != QMessageBox.StandardButton.Yes:
                return
        dlg = MergeDialog(preview, self)
        if dlg.exec() != MergeDialog.DialogCode.Accepted:
            return
        try:
            s, e, text = dlg.values()
            self.project.merge_with_next(index, text=text, start=s, end=e)
        except (ValueError, EditError) as exc:
            QMessageBox.warning(self, APP_TITLE, str(exc))
            return
        self.current_index = index
        self._updating_from_model = True
        try:
            self.refresh_document()
        finally:
            self._updating_from_model = False
        self._update_title()
        self.statusBar().showMessage(
            f"Merged cues {index + 1} and {index + 2}.", 4000)

    def _on_review_requested(self, index: int) -> None:
        if self.project.document is None or not (0 <= index < self.project.cue_count):
            return
        cue = self.project.cue(index)
        self.project.mark_reviewed(index, reviewed=not cue.reviewed)
        self._updating_from_model = True
        try:
            self.refresh_document(preserve_selection=True)
        finally:
            self._updating_from_model = False
        self._update_title()

    # -------------------------------------------------------------- helpers
    def _video_position_seconds(self) -> Optional[float]:
        if not self.video_player.has_media:
            return None
        return self.video_player.position_seconds

    def add_subtitle(self) -> None:
        """Prepare the editor panel for adding a new subtitle.

        Default start time = current video position when a video is open,
        otherwise 0; the user can edit everything before pressing Apply.
        """
        if self.project.document is None:
            QMessageBox.information(self, APP_TITLE,
                                    "Open a VTT file first.")
            return
        pos = self._video_position_seconds()
        start = pos if pos is not None else 0.0
        end = min(start + 2.0,
                  self.video_player.duration_seconds or start + 2.0)
        self.current_index = -1
        self._add_pending = True
        self.cue_editor.start_add_mode(start, end)
        self.cue_counter_label.setText("New subtitle")
        self.statusBar().showMessage(
            "Enter text and timings, then press Apply Changes (Enter).", 5000)

    def _seek_to_seconds(self, seconds: float) -> None:
        if not self.video_player.has_media:
            self.statusBar().showMessage("No video loaded.", 2000)
            return
        self.video_player.disable_loop()
        self.video_player.pause()
        self.video_player.seek_seconds(seconds, play=False)

    def _on_step_request(self, delta: float) -> None:
        """Editor panel requests a step.

        The panel emits integer ±1 for *frame* stepping and fractional
        values for second-based jumping; both pause first and never resume
        playback automatically.
        """
        if float(delta).is_integer() and abs(delta) == 1.0:
            self.step_frame(int(delta))
        else:
            self.step_seconds(float(delta))

    def step_frame(self, direction: int) -> None:
        """Pause and move exactly one frame (clamped, never resumes)."""
        if not self.video_player.has_media:
            self.statusBar().showMessage("No video loaded — cannot step frames.",
                                         2500)
            return
        self.video_player.step_frame(direction)
        self._sync_transport_now()

    def step_seconds(self, seconds: float) -> None:
        if not self.video_player.has_media:
            return
        self.video_player.step_seconds(seconds)
        self._sync_transport_now()

    def _sync_transport_now(self) -> None:
        ms = self.video_player.position_ms
        dur = self.video_player.duration_ms
        self.slider.blockSignals(True)
        self.slider.setValue(ms)
        self.slider.blockSignals(False)
        self.time_label.setText(f"{fmt_time(ms)} / {fmt_time(dur)}")

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
            # last cue reached → stop looping / playing
            self.video_player.disable_loop()
            self.video_player.pause()
            self.statusBar().showMessage(
                "Reached last cue. Press Ctrl+S to save.", 6000)

    def goto_previous(self) -> None:
        if self.project.document is None:
            return
        idx = max(0, self.current_index - 1)
        self.goto_cue(idx, seek=True, play=False)

    def goto_next_unedited(self) -> None:
        """Move to the next cue without touching the text."""
        if self.project.document is None:
            return
        idx = min(self.project.cue_count - 1, self.current_index + 1)
        self.goto_cue(idx, seek=True, play=False)

    def _goto_relative(self, direction: int) -> None:
        """Arrow keys: cue navigation with pause-and-seek (or frame step)."""
        if direction > 0:
            self.goto_next_unedited()
        else:
            self.goto_previous()

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

    def _sync_legacy_editor(self, text: str) -> None:
        if self.editor.toPlainText() != text:
            self._loading_cue = True
            try:
                self.editor.setPlainText(text)
            finally:
                self._loading_cue = False

    def _on_editor_revert(self) -> None:
        """Revert button: reload the cue — or cancel a pending 'Add'."""
        if self._add_pending:
            self._add_pending = False
            idx = max(0, min(self.current_index,
                             self.project.cue_count - 1))                 if self.project.cue_count else -1
            if idx >= 0:
                self.current_index = idx
                self._load_cue_into_editor(idx, seek=False)
            else:
                self.cue_editor.clear()
            self.statusBar().showMessage("Cancelled new subtitle.", 2000)

    def _on_legacy_editor_changed(self) -> None:
        """Keep the cue-editor panel's text field in sync (no model write)."""
        if self._loading_cue or self.current_index < 0 or self._add_pending:
            return
        if self.project.document is None:
            return
        # do not fight with an active edit in the panel itself
        if self.cue_editor.is_loading:
            return
        self.cue_editor.text_edit.setPlainText(self.editor.toPlainText())

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
        if self._seeking_slider:
            return
        # a manual seek (slider release / transport click) re-targets the
        # selection to the cue covering the new position — without touching
        # any timestamps
        if self._suppress_position_sync:
            if self.project.document and self.project.cue_count:
                t = ms / 1000.0
                target = min(self.project.cues,
                             key=lambda c: abs(c.start - t))
                if target.index != self.current_index:
                    self.current_index = target.index
                    self._load_cue_into_editor(target.index, seek=False)
                    self.cue_panel.highlight_current(target.index)
            self._suppress_position_sync = False
        dur = self.video_player.duration_ms
        self.slider.setValue(ms)
        self.time_label.setText(f"{fmt_time(ms)} / {fmt_time(dur)}")
        if self._suppress_position_sync:
            return
        # light-touch synchronisation: only follow playback when the current
        # cue actually stopped covering this position (no per-frame refresh)
        if self.video_player.is_playing and self.project.document:
            idx = self.current_index
            if 0 <= idx < self.project.cue_count:
                cue = self.project.cue(idx)
                pos_s = ms / 1000.0
                if pos_s > cue.end + 0.2:
                    nxt = idx + 1
                    if nxt < self.project.cue_count:
                        self.current_index = nxt
                        self._load_cue_into_editor(nxt, seek=False)

    def _on_duration(self, ms: int) -> None:
        self.slider.setRange(0, max(0, ms))
        self.time_label.setText(
            f"{fmt_time(self.video_player.position_ms)} / {fmt_time(ms)}")

    def _on_play_state(self, state: str) -> None:
        self.btn_play.setText("⏸ Pause" if state == "playing" else "▶ Play")
        self._last_playback_state = state

    def _slider_moved(self, value: int) -> None:
        if self._seeking_slider:
            self.time_label.setText(
                f"{fmt_time(value)} / {fmt_time(self.video_player.duration_ms)}")

    def _slider_released(self) -> None:
        self._seeking_slider = False
        # manual scrubbing exits the cue loop
        self.video_player.disable_loop()
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
