from __future__ import annotations

import re


_CONTROL_CODE_RE = re.compile(r"\\[A-Za-z]\[\d+\]|\\[\.!<>|^]")


def calc_display_width(text: str) -> int:
    """Return RPG Maker display width; control codes take no space."""
    width = 0
    for char in _CONTROL_CODE_RE.sub("", text):
        codepoint = ord(char)
        if (
            0x2E80 <= codepoint <= 0x9FFF
            or 0xF900 <= codepoint <= 0xFAFF
            or 0xFE30 <= codepoint <= 0xFE4F
            or 0xFF01 <= codepoint <= 0xFF60
            or 0xFFE0 <= codepoint <= 0xFFE6
            or 0x3000 <= codepoint <= 0x303F
            or 0x3040 <= codepoint <= 0x309F
            or 0x30A0 <= codepoint <= 0x30FF
            or 0x2010 <= codepoint <= 0x203B
        ):
            width += 2
        else:
            width += 1
    return width
