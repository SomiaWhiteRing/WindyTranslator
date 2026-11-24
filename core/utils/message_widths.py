"""Default EasyRPG Kai message advances, in native 320x240 pixels."""
from pathlib import Path
import subprocess

from core.utils.text_processing import apply_gbk_compatibility


def query_widths(encoding: str, characters: set[str] | None = None) -> str:
    player = Path(__file__).resolve().parents[2] / "modules" / "EasyRPG" / "Player.exe"
    request = "*" if characters is None else " ".join(str(ord(c)) for c in sorted(characters))
    result = subprocess.run(
        [str(player), "--measure-widths", encoding],
        input=request, capture_output=True, text=True, encoding="utf-8",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=True,
    )
    if result.stdout.split("\n", 1)[0] != f"KAI-WIDTHS-1 {encoding}":
        raise ValueError("Invalid EasyRPG Kai width table")
    return result.stdout


class MessageWidths:
    def __init__(self, encoding: str):
        self.encoding = encoding
        self.advances = {}
        for line in query_widths(encoding).splitlines()[1:]:
            code, gothic, mincho = map(int, line.split())
            self.advances[chr(code)] = max(gothic, mincho)
        self.escape = {"932": "¥", "949": "₩"}.get(encoding, "\\")

    def measure(self, line: str) -> int:
        if self.encoding != "65001":
            line = apply_gbk_compatibility(line)
        pixels = 0
        i = 0
        while i < len(line):
            ch = line[i]
            i += 1
            if ch == "$" and i < len(line) and line[i].isascii() and line[i].isalpha():
                pixels += 12
                i += 1
                continue
            if ch not in ("\\", self.escape) or i == len(line):
                pixels += self.advances.get(ch, 12)
                continue
            command = line[i].lower()
            i += 1
            if line.startswith("[", i) and command in "csvnwef":
                depth, end = 1, i + 1
                while depth and end < len(line):
                    if line[end] == "[":
                        depth += 1
                    elif line[end] == "]":
                        depth -= 1
                    end += 1
                if not depth:
                    i = end
            if command == "n":
                pixels += 72  # Runtime actor name: estimate six Han glyphs.
            elif command == "v":
                pixels += 36  # Runtime variable: estimate six digits.
            elif command == "_":
                pixels += self.advances.get(" ", 6) // 2
            elif command in ("\\", self.escape):
                pixels += self.advances.get(command, 12)
        return pixels
