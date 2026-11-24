"""EasyRPG Kai message advances, in native 320x240 pixels."""
import re
import subprocess
from functools import partial
from pathlib import Path

from core.utils.text_processing import apply_gbk_compatibility


def _query(encoding, game_path, request, *options):
    player = Path(__file__).resolve().parents[2] / "modules" / "EasyRPG" / "Player.exe"
    command = [str(player), "--measure-widths", encoding, "--project-path", str(Path(game_path).resolve())]
    return subprocess.run(
        [*command, *options], input=request, capture_output=True, text=True, encoding="utf-8",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=True,
    ).stdout


def query_widths(encoding: str, characters: set[str], game_path: Path) -> str:
    request = " ".join(str(ord(c)) for c in sorted(characters))
    table = _query(encoding, game_path, request)
    if table.split("\n", 1)[0] != f"KAI-WIDTHS-1 {encoding}":
        raise ValueError("Invalid EasyRPG Kai width table")
    return table


def query_font_slots(encoding, game_path):
    return _query(encoding, game_path, "", "--message-font-slots")


class MessageWidths:
    def __init__(self, encoding, game_path, texts):
        self.encoding = encoding
        self.game_path = Path(game_path)
        self.advances = {}
        self.escape = {"932": "¥", "949": "₩"}.get(encoding, "\\")
        self.cache_characters(set(self.compatible("".join(texts))) | {" ", "\\", self.escape})
        font, *pages = query_font_slots(encoding, game_path).splitlines()
        self.inherited = font.split()[1]
        self.message_slots = {}
        # Original scripts preserve the message order, including empty messages.
        # Translation JSON deduplicates text within a file, possibly across slots.
        files = {}
        for row in pages:
            source, event, page, slots = row.split()
            if source == "Common":
                name = f"Database/Commons/Common{int(event):04}.txt"
            elif source == "Troop":
                name = "Database/Troops.txt"
            else:
                name = source.capitalize() + ".txt"
            files.setdefault(name, {})[int(event), int(page)] = slots
        for name, page_slots in files.items():
            path = self.game_path / "StringScripts_Origin" / name
            if not path.is_file():
                continue
            event = int(path.stem[6:]) if path.stem.startswith("Common") else 0
            page = 0
            messages = {}
            lines = iter(path.read_text(encoding="utf-8-sig").split("\n"))
            for line in lines:
                header = re.match(r"\*+(?:Event|Troop)(\d+)\*+", line)
                if header:
                    event = int(header[1])
                    page = 0
                elif header := re.match(r"-+Page(\d+)-+", line):
                    page = int(header[1])
                elif line.startswith("#Message#"):
                    block = []
                    for content in lines:
                        if content.strip() == "##":
                            break
                        block.append(content)
                    messages.setdefault((event, page), []).append("\n".join(block).rstrip("\n"))
            for location, keys in messages.items():
                slots = page_slots.get(location, "")
                for i, original in enumerate(keys):
                    slot = slots[i] if len(slots) == len(keys) else self.inherited
                    key = (name.casefold(), original)
                    if self.message_slots.get(key, slot) != slot:
                        slot = "?"
                    self.message_slots[key] = slot

    def compatible(self, text):
        return text if self.encoding == "65001" else apply_gbk_compatibility(text)

    def cache_characters(self, characters):
        missing = characters - self.advances.keys()
        if missing:
            for row in query_widths(self.encoding, missing, self.game_path).splitlines()[1:]:
                code, gothic, mincho = map(int, row.split())
                self.advances[chr(code)] = (gothic, mincho)

    def for_message(self, name, original):
        slot = self.message_slots.get((name.replace("\\", "/").casefold(), original), self.inherited)
        return partial(self.measure, slot=slot)

    def measure(self, line: str, slot=None) -> int:
        line = self.compatible(line)
        self.cache_characters(set(line))
        gothic = mincho = 0
        i = 0
        while i < len(line):
            ch = line[i]
            i += 1
            if ch == "$" and i < len(line) and line[i].isascii() and line[i].isalpha():
                gothic += 12
                mincho += 12
                i += 1
                continue
            if ch not in ("\\", self.escape) or i == len(line):
                g, m = self.advances[ch]
                gothic += g
                mincho += m
                continue
            command = line[i].lower()
            i += 1
            if line.startswith("[", i) and command in "csvnwetf":
                depth, end = 1, i + 1
                while depth and end < len(line):
                    if line[end] == "[":
                        depth += 1
                    elif line[end] == "]":
                        depth -= 1
                    end += 1
                if not depth:
                    i = end
            if command == "_":
                g, m = self.advances[" "]
                gothic += g // 2
                mincho += m // 2
            elif command in ("\\", self.escape):
                g, m = self.advances[command]
                gothic += g
                mincho += m
        slot = slot or self.inherited
        return gothic if slot == "0" else mincho if slot == "1" else max(gothic, mincho)
