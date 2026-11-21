"""Track optional RM200x fields by their native script address."""
import json
import os
import re
import shutil
import tempfile
from collections import defaultdict
from pathlib import Path

from core.external import easyrpg, rpgrewriter
from core.utils import output_mode, rpg_rt_ini, text_processing
from core.utils.file_system import write_json_atomic


TYPES = {
    "game_title": "游戏标题", "map_names": "地图名称", "map_event_names": "地图事件名称",
    "switch_names": "开关名称", "variable_names": "变量名称",
    "common_event_names": "公共事件名称", "troop_names": "敌群名称",
}
TABLES = {
    "title.txt": ("game_title", "GameTitle"), "database/maptree.txt": ("map_names", "Name"),
    "database/switches.txt": ("switch_names", None), "database/variables.txt": ("variable_names", None),
    "database/troops.txt": ("troop_names", "TroopName"),
}
HEADER = re.compile(r"^(\*+(?:Event|Entry|Troop)\d+\*+)")


def fields(name, text):
    """Yield (type, native header id, original header, marker, value, line index)."""
    kind, marker = TABLES.get(name.as_posix().lower(), (None, None))
    if not kind:
        if re.fullmatch(r"map\d+\.txt", name.as_posix(), re.I):
            kind, marker = "map_event_names", "EventName"
        elif re.fullmatch(r"database/commons/common\d+\.txt", name.as_posix(), re.I):
            kind, marker = "common_event_names", "CommonEventName"
        else:
            return
    lines = text.splitlines()
    anchor = header = ""
    block = False
    for i, line in enumerate(lines[:-1]):
        if block:
            block = line != "##"
            continue
        if line in ("#Message#", "#Choice#", "#ScrollText#", "#StringPicture#"):
            block = True
            continue
        match = HEADER.match(line)
        if match:
            anchor, header = match[1], line
        tag = re.match(r"^#([^#]+)#", line)
        if tag and (tag[1] == marker or (marker is None and tag[1].isdigit())):
            yield kind, anchor, header, tag[1], lines[i + 1].strip(), i


def inventory(game, works_dir, encoding, *, rebuild=False):
    game = Path(game).resolve()
    origin = game / "StringScripts_Origin"
    if not origin.is_dir() or not (game / "RPG_RT.lmt").is_file():
        return {"missing": {}, "maps": {}}
    path = output_mode.project_settings_path(game, works_dir).with_name("extraction_inventory.json")
    identity = [os.path.normcase(str(game)), origin.stat().st_ctime_ns]
    if not rebuild and path.is_file():
        cached = json.loads(path.read_text(encoding="utf-8"))
        if cached["origin"] == identity:
            return cached
    encoding = easyrpg.resolve_game_encoding(str(game), encoding)
    result = {"origin": identity, "missing": {}, "maps": {}}
    with tempfile.TemporaryDirectory(prefix="windy-extract-") as tmp:
        stage = Path(tmp)
        for item in game.iterdir():
            if item.suffix.lower() in (".ldb", ".lmt", ".lmu") and item.is_file():
                shutil.copyfile(item, stage / item.name)
        code, _, error = rpgrewriter.export_text_command(
            str(stage / "RPG_RT.lmt"), encoding, dict.fromkeys(TYPES, True))
        if code:
            raise OSError(error or f"RPGRewriter 导出失败 ({code})")
        scripts = stage / "StringScripts"
        ini = game / "RPG_RT.ini"
        if ini.is_file():
            title = rpg_rt_ini.get_game_title(rpg_rt_ini.read_ini(ini, encoding)[0])
            if title:
                (scripts / "title.txt").write_text(f"#GameTitle#\n{title}\n", encoding="utf-8")
        for candidate in scripts.rglob("*.txt"):
            name = candidate.relative_to(scripts)
            records = list(fields(name, candidate.read_text(encoding="utf-8-sig")))
            if not records:
                continue
            old = origin / name
            present = {(anchor, tag) for _, anchor, _, tag, value, _ in
                       fields(name, old.read_text(encoding="utf-8-sig")) if value} if old.is_file() else set()
            for kind, anchor, header, tag, value, _ in records:
                if not value:
                    continue
                if kind == "map_names":
                    number = re.search(r"Entry(\d+)", anchor)
                    if number:
                        result["maps"][f"map{int(number[1]):04}.txt"] = value
                if (anchor, tag) not in present:
                    result["missing"].setdefault(kind, {}).setdefault(str(name), []).append(
                        [anchor, header, tag, value])
    write_json_atomic(path, result)
    return result


def merge_script(path, name, records):
    text = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    present = {(anchor, tag): (value, i) for _, anchor, _, tag, value, i in fields(name, text)}
    lines = text.splitlines(keepends=True)
    positions = {"": 0}
    for i, line in enumerate(lines):
        match = HEADER.match(line)
        if match:
            positions[match[1]] = i + 1
    inserts = defaultdict(list)
    for anchor, header, tag, value in records:
        if (anchor, tag) in present:
            current, i = present[anchor, tag]
            if not current:
                lines[i + 1] = value + "\n"
            continue
        at = positions.get(anchor, len(lines))
        if anchor not in positions:
            inserts[at].append(header + "\n")
            positions[anchor] = at
        inserts[at].append(f"#{tag}#\n{value}\n\n")
    for at in sorted(inserts, reverse=True):
        lines[at:at] = inserts[at]
    merged = "".join(lines)
    if merged != text:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False,
                                         encoding="utf-8", newline="") as output:
            output.write(merged)
        os.replace(output.name, path)


def run_supplement(game_path, works_dir, kinds, message_queue, source_encoding):
    data = inventory(game_path, works_dir, source_encoding)
    work = output_mode.project_settings_path(game_path, works_dir).parent
    source = work / "untranslated" / "translation.json"
    entries = json.loads(source.read_text(encoding="utf-8-sig"))
    for kind in kinds:
        for name, records in data["missing"].get(kind, {}).items():
            relative = Path(name)
            if relative.anchor or ".." in relative.parts:
                raise ValueError(f"提取清单中的脚本路径无效：{name}")
            origin = Path(game_path) / "StringScripts_Origin" / relative
            live = Path(game_path) / "StringScripts" / relative
            if not live.exists() and origin.is_file():
                live.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(origin, live)
            merge_script(origin, relative, records)
            merge_script(live, relative, records)
            items = entries.setdefault(name, {})
            for _, _, tag, value in records:
                items.setdefault(value, {"text_to_translate": text_processing.convert_half_to_full_katakana(value),
                                         "original_marker": tag, "speaker_id": "SYSTEM"})
    write_json_atomic(source, entries)
    for kind in kinds:
        data["missing"].pop(kind, None)
    write_json_atomic(work / "extraction_inventory.json", data)
    message_queue.put(("success", "补充提取完成。"))
    message_queue.put(("done", None))
    return True
