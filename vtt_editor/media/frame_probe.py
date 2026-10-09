"""Small, well-integrated FPS probe for frame stepping.

The Qt multimedia backend does not expose a reliable frame rate on every
platform, so we read the source FPS with OpenCV *once* when a video is
opened (a separate short-lived ``cv2.VideoCapture`` that is released
immediately — it never runs concurrently with playback and never touches
the player's own decoder).

If OpenCV is unavailable or the file cannot be probed, the caller falls
back to Qt metadata / a labelled ~25 fps guess; in those cases we do NOT
claim frame-exact stepping (variable-frame-rate sources cannot be stepped
frame-exactly through the multimedia backend).
"""
from __future__ import annotations

import os
from typing import Optional


def probe_fps(path: str) -> Optional[float]:
    """Return the constant frame rate of *path*, or None when unknown."""
    if not path or not os.path.isfile(path):
        return None
    try:
        import cv2  # local import: optional dependency
    except Exception:
        return None
    cap = None
    try:
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            return None
        fps = float(cap.get(cv2.CAP_PROP_FPS))
        return fps if fps > 0 else None
    except Exception:
        return None
    finally:
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass
