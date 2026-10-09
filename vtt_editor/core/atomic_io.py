"""Crash-safe file writing helpers.

Serialise fully into a temporary file *next to* the target first and only
then replace the target (``os.replace`` is atomic on Windows and POSIX).
A failed save can therefore never corrupt the previous VTT file.
"""
from __future__ import annotations

import os
import tempfile


def atomic_write_text(path: str, content: str, encoding: str = "utf-8",
                      newline: str = "\n") -> None:
    """Write *content* to *path* atomically.

    Any ``OSError`` raised while serialising or writing propagates to the
    caller; the original file at *path* stays untouched in that case.
    """
    path = os.path.abspath(path)
    directory = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=os.path.splitext(path)[1],
                                    dir=directory)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline=newline) as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        # remove the partial temp file, leave the real target alone
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise
