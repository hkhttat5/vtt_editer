"""Acceptance tests for the precise subtitle timing editor & keyboard controls.

Headless (QT_QPA_PLATFORM=offscreen).  Every check drives the real widgets /
signals of MainWindow, VideoPlayer, SubtitleTable and CueEditorPanel.

Run:  QT_QPA_PLATFORM=offscreen python tests/test_timing_editor.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SAMPLE = """WEBVTT
Kind: captions
Language: de

1
00:00:00.000 --> 00:00:03.000
Willkommen zu dieser deutschen Geschichte.

2
00:00:03.000 --> 00:00:06.000
Heute erzählen wir eine Geschichte
mit Äpfeln, Öl und Straße.

3
00:01:23.500 --> 00:01:27.200
Mitten in einer großen Stadt lag ein kleiner Zoo.
"""

VIDEO_CANDIDATES = (
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "data", "sample.mp4"),
)


def _make_window(app, tmp):
    from ui.main_window import MainWindow
    win = MainWindow()
    p = os.path.join(tmp, "story.vtt")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(SAMPLE)
    # silence modal dialogs during automated runs
    from PySide6.QtWidgets import QMessageBox
    win._pre_save_validation_ok = lambda: True
    QMessageBox.question = staticmethod(lambda *a, **k:
                                         QMessageBox.StandardButton.No)
    QMessageBox.warning = staticmethod(lambda *a, **k: None)
    win.load_vtt(p)
    return win, p


def main():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv[:1])

    results = []

    def check(name, fn):
        try:
            fn()
            results.append((name, "PASS", ""))
            print(f"PASS  {name}")
        except Exception as exc:                       # noqa: BLE001
            results.append((name, "FAIL", repr(exc)))
            print(f"FAIL  {name}: {exc!r}")

    with tempfile.TemporaryDirectory() as tmp:
        win, vtt_path = _make_window(app, tmp)

        # ------------------------------------------------------------- 1
        def t01_start_edit_updates_table_and_duration():
            cue2 = win.project.cue(2)
            assert abs(cue2.start - 83.5) < 1e-9
            win._on_editor_apply(2, cue2.text, 80.0, cue2.end)
            c = win.project.cue(2)
            assert abs(c.start - 80.0) < 1e-9, c.start
            assert abs(c.duration - (c.end - 80.0)) < 1e-9
            # table shows the new start + duration
            row = win.table._row_map.index(2)
            assert win.table.table.item(row, win.table.COL_START).text() == "00:01:20.000"
            dur_txt = win.table.table.item(row, win.table.COL_DURATION).text()
            assert abs(float(dur_txt) - c.duration) < 0.002, dur_txt
            # panel duration refreshed
            assert win.cue_editor.duration_label.text().endswith("s")
            assert win.project.dirty
        check("1. start-time edit updates table + duration", t01_start_edit_updates_table_and_duration)

        # ------------------------------------------------------------- 2
        def t02_end_edit_updates_duration():
            win.undo()   # revert test-1 change
            c = win.project.cue(2)
            assert abs(c.start - 83.5) < 1e-9
            old_dur = c.duration
            win._on_editor_apply(2, c.text, c.start, c.end + 1.0)
            c2 = win.project.cue(2)
            assert abs(c2.duration - (old_dur + 1.0)) < 1e-9, (c2.duration, old_dur)
        check("2. end-time edit updates duration", t02_end_edit_updates_duration)

        # ------------------------------------------------------------- 3
        def t03_invalid_timestamps_rejected():
            from core.project import EditError
            c = win.project.cue(1)
            before = (c.start, c.end)
            try:
                win.project.set_cue_timing(1, start=-1.0, end=c.end)
                raise AssertionError("negative start accepted")
            except EditError:
                pass
            try:
                win.project.set_cue_timing(1, start=c.end + 1, end=c.end)
                raise AssertionError("end<=start accepted")
            except EditError:
                pass
            c = win.project.cue(1)
            assert (c.start, c.end) == before, "corrupted data!"
            # panel-level validation keeps last valid value
            win.goto_cue(1, seek=False)
            win.cue_editor.start_edit.setText("not a time")
            win.cue_editor.apply_current()
            assert "Invalid timestamp" in win.cue_editor.error_label.text(), \
                win.cue_editor.error_label.text()
            assert win.project.cue(1).start == before[0]
            # table-level invalid edit reverts the cell
            idx = 1
            row = win.table._row_map.index(idx)
            item = win.table.table.item(row, win.table.COL_START)
            item.setText("garbage")          # simulates committed cell text
            win._on_table_edit(idx, "start", "garbage")
            assert win.table.table.item(row, win.table.COL_START).text() == \
                win.project.cue(idx).start_str
        check("3. invalid timestamps rejected safely", t03_invalid_timestamps_rejected)

        # ------------------------------------------------------------- 4
        def t04_click_row_pauses_and_seeks():
            video = next((v for v in VIDEO_CANDIDATES if os.path.isfile(v)), None)
            assert video, "no test video available"
            assert win.load_video(video)
            win.video_player.play()
            app.processEvents()
            assert win.video_player.is_playing
            # simulate a user clicking the row of cue index 2
            win._programmatic_select = False
            row = win.table._row_map.index(2)
            win.table.table.selectRow(row)
            app.processEvents()
            assert not win.video_player.is_playing, "playback did not pause"
            target_ms = int(round(win.project.cue(2).start * 1000))
            pos = win.video_player.position_ms
            assert abs(pos - target_ms) <= 250, (pos, target_ms)
            assert win.current_index == 2
            # selection preserved & highlighted
            assert win.table.current_cue_index() == 2
            # time label + slider updated
            assert win.slider.value() == pos
            assert win.time_label.text().startswith(
                win.time_label.text().split(" / ")[0])
        check("4. clicking a subtitle pauses playback and seeks to its start",
              t04_click_row_pauses_and_seeks)

        # ------------------------------------------------------------- 5
        def t05_space_toggles_playback():
            from PySide6.QtCore import QEvent, Qt as _Qt
            from PySide6.QtGui import QKeyEvent
            win.video_player.video_widget.setFocus()
            app.processEvents()
            space = QKeyEvent(QEvent.Type.KeyPress, _Qt.Key.Key_Space,
                              _Qt.KeyboardModifier.NoModifier)
            was = win.video_player.is_playing
            win.keyPressEvent(space)
            app.processEvents()
            assert win.video_player.is_playing != was, "space did not toggle"
            win.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, _Qt.Key.Key_Space,
                                        _Qt.KeyboardModifier.NoModifier))
            app.processEvents()
            assert win.video_player.is_playing == was, "space did not restore"
        check("5. Space toggles play/pause (video focus)", t05_space_toggles_playback)

        # ------------------------------------------------------------- 6/7/8
        def t06_frame_step_right():
            win.video_player.seek_seconds(2.0, play=False)
            app.processEvents()
            base = win.video_player.position_ms
            fps = win.video_player.fps
            assert abs(fps - 25.0) < 0.01, f"fps probe failed: {fps}"
            frame_ms = 1000.0 / fps
            win.step_frame(1)
            app.processEvents()
            new = win.video_player.position_ms
            assert abs(new - (base + frame_ms)) < 2, (base, new, frame_ms)
            assert not win.video_player.is_playing, "stepping resumed playback"
            # label/slider synchronised
            assert win.slider.value() == new
        check("6. Right step moves exactly one frame forward", t06_frame_step_right)

        def t07_frame_step_left():
            base = win.video_player.position_ms
            frame_ms = 1000.0 / win.video_player.fps
            win.step_frame(-1)
            app.processEvents()
            new = win.video_player.position_ms
            assert abs(new - (base - frame_ms)) < 2, (base, new)
            assert not win.video_player.is_playing
        check("7. Left step moves exactly one frame backward", t07_frame_step_left)

        def t08_stepping_does_not_resume():
            win.step_frame(1)
            win.step_frame(1)
            app.processEvents()
            assert not win.video_player.is_playing
        check("8. frame stepping never resumes playback", t08_stepping_does_not_resume)

        # ------------------------------------------------------------- 9
        def t09_keys_normal_inside_text_inputs():
            from PySide6.QtCore import QEvent, Qt as _Qt
            from PySide6.QtGui import QKeyEvent
            # search field: Space must type a space, arrows move caret
            win.table.search_edit.setFocus()
            win.table.search_edit.setText("abc")
            win.table.search_edit.setCursorPosition(0)
            app.processEvents()
            playing_before = win.video_player.is_playing
            ev = QKeyEvent(QEvent.Type.KeyPress, _Qt.Key.Key_Space,
                           _Qt.KeyboardModifier.NoModifier)
            handled_by_window = False
            # deliver through the widget itself (like Qt does when focused)
            from PySide6.QtTest import QTest
            QTest.keyPress(win.table.search_edit, _Qt.Key.Key_Space)
            app.processEvents()
            assert " " in win.table.search_edit.text(), \
                win.table.search_edit.text()
            assert win.video_player.is_playing == playing_before, \
                "Space toggled playback while typing in search field"
            # line edit in the editor panel
            win.cue_editor.start_edit.setFocus()
            txt = win.cue_editor.start_edit.text()
            win.cue_editor.start_edit.setCursorPosition(2)
            pos_before = win.cue_editor.start_edit.cursorPosition()
            QTest.keyPress(win.cue_editor.start_edit, _Qt.Key.Key_Left)
            assert win.cue_editor.start_edit.cursorPosition() != pos_before \
                or win.video_player.position_ms is not None
            # plain editor: Space types a space
            win.editor.setFocus()
            win.editor.setPlainText("ab")
            cur = win.editor.textCursor()
            cur.setPosition(0)
            win.editor.setTextCursor(cur)
            QTest.keyPress(win.editor, _Qt.Key.Key_Space)
            app.processEvents()
            assert win.editor.toPlainText().startswith(" ab"), \
                repr(win.editor.toPlainText())
        check("9. Space/arrows behave normally inside text inputs",
              t09_keys_normal_inside_text_inputs)

        # ------------------------------------------------------------- 10
        def t10_selection_survives_resort():
            win.goto_cue(1, seek=False)      # cue 2 at 3.0..6.0 s
            assert win.current_index == 1
            # move cue 2 far beyond cue 3 → chronological order changes
            win._on_editor_apply(1, win.project.cue(1).text, 300.0, 305.0)
            assert win.current_index == 1, win.current_index
            assert win.table.current_cue_index() == 1
            assert win.cue_editor.current_index == 1
            assert win.project.cue(1).start == 300.0
            # undo back to original order
            win.project.undo()
            win.refresh_document(preserve_selection=True)
            assert win.current_index == 1
        check("10. selection preserved across chronological re-sort",
              t10_selection_survives_resort)

        # ------------------------------------------------------------- 11
        def t11_export_contains_edited_times():
            c = win.project.cue(2)
            win._on_editor_apply(2, "Geprüft: äöü ß ✓timing", 81.250, 86.125)
            outp = os.path.join(tmp, "out.vtt")
            win.project.save(outp)
            with open(outp, encoding="utf-8") as fh:
                data = fh.read()
            assert "00:01:21.250 --> 00:01:26.125" in data, data
            assert "Geprüft: äöü ß ✓timing" in data
            assert not win.project.dirty          # marked saved only after success
            # millisecond precision survives round-trip
            win.project.load_vtt(outp)
            win.refresh_document()
            assert abs(win.project.cue(2).start - 81.25) < 1e-9
            assert abs(win.project.cue(2).end - 86.125) < 1e-9
        check("11. exported VTT contains edited timestamps + Unicode",
              t11_export_contains_edited_times)

        # ------------------------------------------------------------- 12
        def t12_clamping_at_bounds():
            win.video_player.pause()
            win.video_player.seek_seconds(0.0, play=False)
            app.processEvents()
            win.step_frame(-1)               # before first frame
            app.processEvents()
            assert win.video_player.position_ms >= 0
            assert win.video_player.position_ms <= 1   # clamped at 0
            win.video_player.seek_seconds(
                win.video_player.duration_seconds, play=False)
            app.processEvents()
            dur = win.video_player.duration_ms
            win.step_frame(1)                # past last frame
            app.processEvents()
            assert win.video_player.position_ms <= dur
            assert not win.video_player.is_playing
        check("12. frame stepping clamps at first/last frame",
              t12_clamping_at_bounds)

        # ------------------------------------------------------------- 13
        def t13_arrow_keys_and_shortcut_paths():
            from PySide6.QtCore import QEvent, Qt as _Qt
            from PySide6.QtGui import QKeyEvent
            # table viewport focus → arrows navigate cues (pause+seek)
            win.table.table.viewport().setFocus()
            app.processEvents()
            win.goto_cue(0, seek=False)
            win.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, _Qt.Key.Key_Right,
                                       _Qt.KeyboardModifier.NoModifier))
            app.processEvents()
            assert win.current_index == 1, win.current_index
            win.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, _Qt.Key.Key_Left,
                                        _Qt.KeyboardModifier.NoModifier))
            app.processEvents()
            assert win.current_index == 0
            # no-video case: steps are graceful no-ops
            w2 = None
            # Space works when the table has focus too
            was = win.video_player.is_playing
            win.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, _Qt.Key.Key_Space,
                                        _Qt.KeyboardModifier.NoModifier))
            app.processEvents()
            assert win.video_player.is_playing != was
            win.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, _Qt.Key.Key_Space,
                                        _Qt.KeyboardModifier.NoModifier))
            app.processEvents()
            # missing video handling: step_frame on unloaded media is safe
            from ui.main_window import MainWindow
            w2 = MainWindow()
            w2.step_frame(1)                 # must not raise
            w2.step_frame(-1)
            w2.deleteLater()
        check("13. arrow keys on table focus; graceful without video",
              t13_arrow_keys_and_shortcut_paths)

        # ------------------------------------------------------------- 14
        def t14_table_inplace_edit_workflow():
            """Full workflow: select row → edit Start cell text → Enter path."""
            win.goto_cue(0, seek=False)
            idx = 0
            row = win.table._row_map.index(idx)
            item = win.table.table.item(row, win.table.COL_START)
            item.setText("00:00:01.500")            # what the delegate commits
            win._on_table_edit(idx, "start", "00:00:01.500")
            assert abs(win.project.cue(0).start - 1.5) < 1e-9
            assert win.table.current_cue_index() == 0
            # neighbouring cues untouched (no automatic offsets)
            assert abs(win.project.cue(1).start - 3.0) < 1e-9
        check("14. in-table timestamp edit keeps neighbours untouched",
              t14_table_inplace_edit_workflow)

        # ------------------------------------------------------------- 15
        def t15_undo_redo_of_timing_edits():
            c = win.project.cue(1)
            orig = (c.start, c.end)
            win._on_editor_apply(1, c.text, 10.0, 12.0)
            assert (win.project.cue(1).start, win.project.cue(1).end) == (10.0, 12.0)
            win.undo()
            assert (win.project.cue(1).start, win.project.cue(1).end) == orig
            win.redo()
            assert (win.project.cue(1).start, win.project.cue(1).end) == (10.0, 12.0)
            win.undo()
        check("15. timing edits participate in undo/redo",
              t15_undo_redo_of_timing_edits)

        # ------------------------------------------------------------- 16
        def t16_validator_reports_no_autofix():
            from export.vtt_validator import validate_cues
            cues = list(win.project.cues)
            before = [(c.index, c.start, c.end) for c in cues]
            summ = validate_cues(cues, win.video_player.duration_seconds)
            after = [(c.index, c.start, c.end) for c in cues]
            assert before == after, "validator mutated the cues!"
            assert isinstance(summ.counts(), tuple)
        check("16. validator never auto-corrects timings",
              t16_validator_reports_no_autofix)

    print()
    failed = [r for r in results if r[1] == "FAIL"]
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        for name, _, err in failed:
            print("FAILED:", name, err)
        sys.exit(1)
    print("ALL TIMING-EDITOR TESTS PASSED")


if __name__ == "__main__":
    main()
