"""Issue-details panel for the automatic VTT review workflow.

Shows, for the currently selected subtitle: cue ID, start/end timestamps,
text, and every automatically detected issue with its category, severity,
reason ("why was this flagged?") and a suggested review action.

The panel is purely informational — it never rewrites or deletes subtitle
text and never guesses "corrected" German sentences.
"""
from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont, QTextCursor
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QSizePolicy, QTextBrowser, QVBoxLayout,
    QWidget,
)

# category → human readable label + suggested review action
CATEGORY_INFO = {
    "empty_text":      ("Empty subtitle",
                        "Fill in the missing text (e.g. re-OCR the frame) or delete the cue."),
    "invalid_timestamp": ("Invalid timestamp",
                          "Correct the start/end time format in the editor."),
    "end_before_start": ("End ≤ start time",
                         "Fix the end time so it lies after the start time."),
    "overlap":         ("Overlapping subtitles",
                        "Review both cues; adjust timings only if the video shows they are wrong."),
    "gap":             ("Unexpected gap between subtitles",
                        "Check whether a subtitle is missing in this region."),
    "short_duration":  ("Very short duration",
                        "Verify the cue isn't a fragment split off by mistake."),
    "long_duration":   ("Very long duration",
                        "Consider splitting the cue at a natural sentence boundary."),
    "beyond_video":    ("Beyond video duration",
                        "Verify the timestamps against the end of the video."),
    "duplicate_text":  ("Duplicate subtitle text",
                        "Repetition can be legitimate — confirm it matches the video."),
    "ocr_noise":       ("Possible OCR noise",
                        "Compare with the video frame; correct misread words manually."),
    "corrupted_chars": ("Corrupted characters",
                        "Re-check the source frame; replace damaged characters manually."),
    "symbol_clutter":  ("Symbol clutter / OCR noise",
                        "Compare with the video frame and clean up the text manually."),
    "ocr_symbols":     ("OCR character confusion",
                        "Watch out for 0/o, 1/l, 5/s style confusions; verify against the frame."),
    "all_caps":        ("Unusual capitalisation",
                        "Confirm upper-case usage is intentional."),
    "word_fragment":   ("Incomplete word",
                        "The word may be cut off by the ROI border; check the frame."),
    "non_german":      ("Suspicious non-German text",
                        "Check whether the wrong language track was extracted."),
    "unrecognized_language": ("Unrecognisable vocabulary",
                        "Could be a name or foreign quote — verify against the video."),
}

SEVERITY_ICON = {"error": "❌", "warning": "⚠️", "info": "ℹ️"}
SEVERITY_COLOR = {"error": "#b00000", "warning": "#a06000", "info": "#5555aa"}


class IssuePanel(QWidget):
    """Details of all issues detected for one cue."""

    reviewRequested = Signal(int)          # mark this cue reviewed
    nextIssueRequested = Signal()          # jump to next unresolved issue

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._index: int = -1
        self.setMinimumWidth(280)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 6)
        root.setSpacing(4)

        head = QHBoxLayout()
        title = QLabel("Issue Details")
        bold = QFont(); bold.setBold(True)
        title.setFont(bold)
        head.addWidget(title)
        head.addStretch(1)
        self.status_icon = QLabel("—")
        self.status_icon.setFont(QFont("Segoe UI Emoji", 14))
        head.addWidget(self.status_icon)
        root.addLayout(head)

        self.meta_label = QLabel("No subtitle selected.")
        self.meta_label.setStyleSheet("color: gray;")
        self.meta_label.setWordWrap(True)
        root.addWidget(self.meta_label)

        self.text_preview = QLabel("")
        self.text_preview.setWordWrap(True)
        self.text_preview.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.text_preview.setStyleSheet(
            "background:#f4f4f0; padding:4px; border:1px solid #ccc;")
        root.addWidget(self.text_preview)

        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(False)
        self.browser.setMinimumHeight(120)
        root.addWidget(self.browser, 1)

        btns = QHBoxLayout()
        self.btn_review = QPushButton("Mark as Reviewed")
        self.btn_review.setToolTip(
            "Mark the selected subtitle as checked (F9).\n"
            "The reviewed flag survives revalidation; a later edit that\n"
            "introduces a new issue will require another review.")
        self.btn_review.clicked.connect(
            lambda: self._index >= 0 and self.reviewRequested.emit(self._index))
        self.btn_next = QPushButton("Next Issue (F8)")
        self.btn_next.clicked.connect(self.nextIssueRequested)
        btns.addWidget(self.btn_review)
        btns.addWidget(self.btn_next)
        btns.addStretch(1)
        root.addLayout(btns)

    # ------------------------------------------------------------------ api
    def show_cue(self, index: int, cue, issues: List, reviewed: bool) -> None:
        """Render details for cue *index* (issues may be empty)."""
        self._index = index
        if cue is None:
            self.clear()
            return
        worst = None
        for sev in ("error", "warning", "info"):
            if any(i.severity == sev for i in issues):
                worst = sev
                break
        if reviewed:
            icon = "✓"
            color = "#1a7a1a"
        elif worst == "error":
            icon, color = SEVERITY_ICON["error"], SEVERITY_COLOR["error"]
        elif worst == "warning":
            icon, color = SEVERITY_ICON["warning"], SEVERITY_COLOR["warning"]
        else:
            icon, color = "○", "gray"
        self.status_icon.setText(icon)
        self.status_icon.setStyleSheet(f"color: {color};")

        preview = (cue.text or "").replace("\n", " ⏎ ")
        if len(preview) > 120:
            preview = preview[:117] + "…"
        self.meta_label.setText(
            f"Subtitle ID: {index + 1}   "
            f"{cue.start_str} → {cue.end_str}   "
            f"({'reviewed' if reviewed else 'not reviewed'})")
        self.text_preview.setText(f'Text: "{preview}"')

        html_parts = []
        if not issues:
            note = ("No issues detected for this subtitle." if not reviewed
                    else "Reviewed ✓ — no open issues.")
            html_parts.append(f"<p><i>{note}</i></p>")
        for iss in issues:
            cat_label, action = CATEGORY_INFO.get(
                iss.code, (iss.code.replace("_", " ").title(),
                           "Review this subtitle manually."))
            col = SEVERITY_COLOR.get(iss.severity, "#333")
            icon = SEVERITY_ICON.get(iss.severity, "•")
            related = ""
            if iss.related_index is not None and iss.related_index != iss.cue_index:
                related = (f" &nbsp;<span style='color:#666;'>related cue: "
                           f"{iss.related_index + 1}</span>")
            html_parts.append(
                f"<div style='margin-bottom:6px;'>"
                f"<b>{icon} {cat_label}</b> "
                f"<span style='color:{col};font-weight:bold;'>"
                f"[{iss.severity}]</span>{related}<br>"
                f"<span style='color:#333;'>Reason:</span> {self._esc(iss.message)}<br>"
                f"<span style='color:#0a6;font-size:smaller;'>"
                f"Suggested action: {self._esc(action)}</span></div>")
        self.browser.setHtml("".join(html_parts))
        self.btn_review.setEnabled(True)

    def clear(self) -> None:
        self._index = -1
        self.status_icon.setText("—")
        self.status_icon.setStyleSheet("")
        self.meta_label.setText("No subtitle selected.")
        self.text_preview.setText("")
        self.browser.setHtml("<p><i>Select a subtitle to see detected issues.</i></p>")
        self.btn_review.setEnabled(False)

    @property
    def current_index(self) -> int:
        return self._index

    @staticmethod
    def _esc(text: str) -> str:
        from html import escape
        return escape(text or "")
