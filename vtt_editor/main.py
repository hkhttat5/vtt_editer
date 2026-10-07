#!/usr/bin/env python3
"""VTT Corrector — desktop tool for manually reviewing/correcting WebVTT
subtitles against a German learning video.

Usage:
    python main.py [video.mp4] [subtitles.vtt]

Workflow (MVP):
    Open Video → Open VTT → Play → Edit text → Enter → Next cue → Save As
"""
from __future__ import annotations

import os
import sys

# make the package root importable when run as `python main.py`
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from ui.main_window import MainWindow


def main(argv=None) -> int:
    argv = list(sys.argv if argv is None else argv)

    # High-DPI friendliness on Windows
    try:
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    except Exception:
        pass

    app = QApplication(argv)
    app.setApplicationName("VTT Corrector")
    app.setStyle("Fusion")

    win = MainWindow()

    # optional CLI arguments:  main.py [video] [vtt]
    args = [a for a in argv[1:] if not a.startswith("-")]
    video = next((a for a in args if a.lower().endswith(
        (".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".wmv"))), None)
    vtt = next((a for a in args if a.lower().endswith(".vtt")), None)

    win.show()

    if video and os.path.isfile(video):
        win.video_player.open(video)
        win.project.video_path = video
    if vtt and os.path.isfile(vtt):
        win.project.load_vtt(vtt)
        win.cue_panel.rebuild(win.project.cues, -1, 0)
        if win.project.cue_count:
            win.goto_cue(0)

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
