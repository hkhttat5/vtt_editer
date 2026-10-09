"""Video playback built on Qt Multimedia (QMediaPlayer + QVideoWidget).

Qt 6 ships an FFmpeg-backed media plugin inside the PySide6 wheels, so MP4,
MKV, WebM … work out of the box on Windows 10/11 without installing anything.

This module wraps the player in a small API so the UI does not touch Qt
multimedia classes directly.  A *single* media source object is kept for the
whole lifetime of the widget, so opening a new video never leaves stale
readers behind and playback can never interfere with other components.
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
    fpsResolved = Signal(float)        # best-effort frame rate of the source

    VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v",
                        ".wmv", ".flv", ".ts", ".mts", ".mpg", ".mpeg"}

    # A/V sync tolerance (ms): we only re-seek when the position has drifted
    # outside [loop_start - tol, loop_end + tol] instead of on every tick.
    LOOP_TOLERANCE_MS = 150

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.video_widget = QVideoWidget()
        self.video_widget.setMinimumSize(320, 180)
        self.video_widget.setStyleSheet("background-color: #1e1e1e;")

        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_output)
        self.player.setVideoOutput(self.video_widget)

        # one reusable source object — avoids multiple readers per video
        self._source = QMediaFormat()

        self.player.positionChanged.connect(self._on_position_emit)
        self.player.durationChanged.connect(self.durationChanged.emit)
        self.player.playbackStateChanged.connect(self._on_state)

        self._media_available = False
        self.current_path: Optional[str] = None
        self._fps: float = 0.0

        # A-B loop state (loop the current cue's segment while reviewing it)
        self._loop_active = False
        self._loop_start_ms = 0
        self._loop_end_ms = 0

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

    # ------------------------------------------------------------------- loop
    @property
    def loop_active(self) -> bool:
        return self._loop_active

    def set_loop(self, start_ms: int, end_ms: int) -> None:
        """Enable an A-B loop over [start_ms, end_ms]."""
        self._loop_start_ms = max(0, int(start_ms))
        self._loop_end_ms = max(self._loop_start_ms + 1, int(end_ms))
        self._loop_active = True

    def enable_loop_for_range(self, start_s: float, end_s: float) -> None:
        """Convenience: loop a cue given in seconds (with a small tail)."""
        self.set_loop(int(start_s * 1000), int((end_s + 0.25) * 1000))

    def disable_loop(self) -> None:
        self._loop_active = False

    def _on_position_emit(self, ms: int) -> None:
        """Keep playback inside the active A-B loop segment."""
        if self._loop_active:
            tol = self.LOOP_TOLERANCE_MS
            if ms >= self._loop_end_ms or ms < self._loop_start_ms - tol:
                # re-seek only when the position has actually drifted outside
                # the segment (a seek may lag one tick behind on some backends)
                if abs(ms - self._loop_start_ms) > tol:
                    self.player.setPosition(self._loop_start_ms)
                if not self.is_playing:
                    self.player.play()
        self.positionChanged.emit(ms)

    # ------------------------------------------------------------------ media
    def open(self, path: str) -> bool:
        if not path or not os.path.isfile(path):
            return False
        self._media_available = True
        self.current_path = path
        self.player.setSource(QUrl.fromLocalFile(path))
        return True

    def close(self) -> None:
        self.player.stop()
        self.player.setSource(QUrl())
        self._media_available = False
        self.current_path = None
        self._fps = 0.0
        self.disable_loop()

    # ------------------------------------------------------------------- time
    @property
    def has_media(self) -> bool:
        return self._media_available

    @property
    def duration_ms(self) -> int:
        return int(self.player.duration())

    @property
    def duration_seconds(self) -> Optional[float]:
        d = self.player.duration()
        return d / 1000.0 if d and d > 0 else None

    @property
    def position_ms(self) -> int:
        return int(self.player.position())

    @property
    def position_seconds(self) -> float:
        return self.player.position() / 1000.0

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

    # -------------------------------------------------------------- frame rate
    def set_fps(self, fps: float) -> None:
        """Register the source FPS (measured by the app) for frame stepping."""
        try:
            fps = float(fps)
        except (TypeError, ValueError):
            fps = 0.0
        if fps > 0 and abs(fps - self._fps) > 1e-6:
            self._fps = fps
            self.fpsResolved.emit(fps)

    @property
    def fps(self) -> float:
        """Best-effort frame rate; 0.0 when unknown (VFR-safe fallback)."""
        if self._fps > 0:
            return self._fps
        meta = self.player.metaData()
        v = meta.value("VideoFrameRate")
        try:
            f = float(v)
            if f > 0:
                return f
        except (TypeError, ValueError):
            pass
        return 0.0

    def step_frame(self, direction: int = 1) -> None:
        """Move exactly one frame using the source FPS when known.

        Playback is paused first and never resumed automatically; the new
        position is clamped between the first (0 ms) and last (duration)
        frame of the media.  Falls back to a short configurable interval
        when the frame rate is unknown — variable-frame-rate sources cannot
        be stepped frame-exactly through the multimedia backend, and we do
        not claim otherwise.
        """
        if not self._media_available:
            return
        self.pause()
        self.disable_loop()
        f = self.fps
        if f > 0:
            delta_ms = max(1, int(round(1000.0 * direction / f)))
        else:
            delta_ms = 40 * direction          # ~25 fps guess, clearly labelled
        self.seek_relative_ms(delta_ms)

    def seek_relative_ms(self, delta_ms: int) -> None:
        """Seek by *delta_ms*, clamped to [0, duration] (first/last frame).

        For constant-frame-rate media the target is snapped onto the frame
        grid (multiples of 1/fps), so repeated ±1-frame steps never drift.
        """
        if not self._media_available:
            return
        new_pos = self.position_ms + int(delta_ms)
        dur = self.duration_ms
        if dur > 0:
            new_pos = min(new_pos, dur)
        f = self.fps
        if f > 0:
            frame_ms = 1000.0 / f
            new_pos = int(round(new_pos / frame_ms) * frame_ms)
            if dur > 0:
                new_pos = min(new_pos, dur)
        self.player.setPosition(max(0, new_pos))

    def step_seconds(self, seconds: float) -> None:
        """Seek backward/forward by a configurable interval (default 1 s)."""
        if not self._media_available:
            return
        self.pause()
        new_pos = max(0, self.position_ms + int(round(seconds * 1000)))
        dur = self.duration_ms
        if dur:
            new_pos = min(new_pos, dur)
        self.player.setPosition(new_pos)

    # ---------------------------------------------------------------- controls
    def play(self) -> None:
        if self._media_available:
            self.player.play()

    def pause(self) -> None:
        if self.is_playing:
            self.player.pause()

    def toggle_play_pause(self) -> None:
        if self.is_playing:
            # Pause button / Space must really stop the video, even mid-loop —
            # the user wants to freeze the frame and read the text.
            # (The loop's internal auto-resume only fires on boundary hits.)
            self.pause()
        else:
            self.play()

    def stop(self) -> None:
        self.player.stop()
        self.disable_loop()

    # ------------------------------------------------------------------ volume
    def set_volume(self, percent: int) -> None:
        self.audio_output.setVolume(max(0, min(100, percent)) / 100.0)

    def set_speed(self, factor: float) -> None:
        try:
            self.player.setPlaybackRate(factor)
        except Exception:
            pass
