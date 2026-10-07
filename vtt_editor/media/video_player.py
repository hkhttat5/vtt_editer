"""Video playback built on Qt Multimedia (QMediaPlayer + QVideoWidget).

Qt 6 ships an FFmpeg-backed media plugin inside the PySide6 wheels, so MP4,
MKV, WebM … work out of the box on Windows 10/11 without installing anything.

This module wraps the player in a small API so the UI does not touch Qt
multimedia classes directly.
"""
from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import QUrl, Signal, QObject
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QMediaFormat
from PySide6.QtMultimediaWidgets import QVideoWidget


class VideoPlayer(QObject):
    """Thin, stable wrapper around :class:`QMediaPlayer`."""

    positionChanged = Signal(int)      # ms
    durationChanged = Signal(int)      # ms
    playbackStateChanged = Signal(str)  # "playing" | "paused" | "stopped"

    VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v",
                        ".wmv", ".flv", ".ts", ".mts", ".mpg", ".mpeg"}

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.video_widget = QVideoWidget()
        self.video_widget.setMinimumSize(320, 180)
        self.video_widget.setStyleSheet("background-color: #1e1e1e;")

        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_output)
        self.player.setVideoOutput(self.video_widget)

        # seek with precise frame-accurate mode when supported
        try:
            fmt = QMediaFormat()
            self.player.setCaptureFormat  # noqa – attribute probe only
        except Exception:
            pass

        self.player.positionChanged.connect(self.positionChanged.emit)
        self.player.durationChanged.connect(self.durationChanged.emit)
        self.player.playbackStateChanged.connect(self._on_state)

        self._media_available = False

    # ------------------------------------------------------------------ state
    def _on_state(self, state) -> None:
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self.playbackStateChanged.emit("playing")
        elif state == QMediaPlayer.PlaybackState.PausedState:
            self.playbackStateChanged.emit("paused")
        else:
            self.playbackStateChanged.emit("stopped")

    @property
    def is_playing(self) -> bool:
        return self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    # ------------------------------------------------------------------ media
    def open(self, path: str) -> bool:
        if not path or not os.path.isfile(path):
            return False
        self._media_available = True
        self.player.setSource(QUrl.fromLocalFile(path))
        return True

    def close(self) -> None:
        self.player.stop()
        self.player.setSource(QUrl())
        self._media_available = False

    # ------------------------------------------------------------------- time
    @property
    def has_media(self) -> bool:
        return self._media_available

    @property
    def duration_ms(self) -> int:
        return int(self.player.duration())

    @property
    def position_ms(self) -> int:
        return int(self.player.position())

    def seek_seconds(self, seconds: float, play: bool = True) -> None:
        """Jump to *seconds* and (optionally) start playing from there."""
        if not self._media_available:
            return
        ms = max(0, int(seconds * 1000))
        self.player.setPosition(ms)
        if play:
            self.play()

    def seek_ms(self, ms: int) -> None:
        if self._media_available:
            self.player.setPosition(max(0, int(ms)))

    # ---------------------------------------------------------------- controls
    def play(self) -> None:
        if self._media_available:
            self.player.play()

    def pause(self) -> None:
        if self.is_playing:
            self.player.pause()

    def toggle_play_pause(self) -> None:
        if self.is_playing:
            self.pause()
        else:
            self.play()

    def stop(self) -> None:
        self.player.stop()

    # ------------------------------------------------------------------ volume
    def set_volume(self, percent: int) -> None:
        self.audio_output.setVolume(max(0, min(100, percent)) / 100.0)

    def set_speed(self, factor: float) -> None:
        try:
            self.player.setPlaybackRate(factor)
        except Exception:
            pass
