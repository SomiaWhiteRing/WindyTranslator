import codecs
import re


SUPPORTED_ENCODINGS = ("932", "936", "950", "949", "874", "1252", "1250", "1251")


def codec_name(code):
    return f"cp{code}"


def _get_value(text, section_name, key_name):
    section = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip().casefold()
            continue
        if section != section_name.casefold() or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip().casefold() == key_name.casefold():
            return value.rstrip("\r\n")
    return None


def _encoding_hint(raw):
    match = re.search(rb"(?im)^\s*Encoding\s*=\s*(\d+)\s*$", raw)
    return match.group(1).decode("ascii") if match else None


def read_ini(path, preferred_encoding, original_title=None):
    raw = path.read_bytes()
    if raw.startswith(codecs.BOM_UTF8):
        return raw.decode("utf-8-sig"), "utf-8-sig"

    candidates = []
    for code in (preferred_encoding, _encoding_hint(raw), *SUPPORTED_ENCODINGS):
        if code and code not in candidates:
            candidates.append(code)

    decoded = []
    for code in candidates:
        try:
            text = raw.decode(codec_name(code))
        except UnicodeDecodeError:
            continue
        if original_title is not None and _get_value(text, "RPG_RT", "GameTitle") != original_title:
            continue
        decoded.append((text, code))

    if not decoded:
        raise UnicodeDecodeError(codec_name(preferred_encoding), raw, 0, len(raw), "cannot decode RPG_RT.ini")
    return decoded[0]


def get_game_title(text):
    return _get_value(text, "RPG_RT", "GameTitle")


def set_easy_rpg_encoding(text, encoding):
    lines = text.splitlines(keepends=True)
    section = None
    easyrpg_index = None
    encoding_indices = []

    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip().casefold()
            if section == "easyrpg":
                easyrpg_index = index
            continue
        if section == "easyrpg" and "=" in line:
            key, _value = line.split("=", 1)
            if key.strip().casefold() == "encoding":
                encoding_indices.append(index)

    if encoding_indices:
        first = encoding_indices[0]
        key, _value = lines[first].split("=", 1)
        newline = "\r\n" if lines[first].endswith("\r\n") else "\n" if lines[first].endswith("\n") else ""
        lines[first] = f"{key}={encoding}{newline}"
        for index in reversed(encoding_indices[1:]):
            del lines[index]
        return "".join(lines)

    if easyrpg_index is None:
        if lines and lines[-1].strip():
            lines.append("\n")
        lines.extend(("[EasyRPG]\n", f"Encoding={encoding}\n"))
        return "".join(lines)

    insert_at = easyrpg_index + 1
    while insert_at < len(lines) and not lines[insert_at].strip().startswith("["):
        insert_at += 1
    lines.insert(insert_at, f"Encoding={encoding}\n")
    return "".join(lines)


def set_full_package_flag(text):
    lines = text.splitlines(keepends=True)
    section = None
    rpg_rt_index = None
    flag_indices = []

    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip().casefold()
            if section == "rpg_rt":
                rpg_rt_index = index
            continue
        if section == "rpg_rt" and "=" in line:
            key, _value = line.split("=", 1)
            if key.strip().casefold() == "fullpackageflag":
                flag_indices.append(index)

    if flag_indices:
        first = flag_indices[0]
        key, _value = lines[first].split("=", 1)
        newline = "\r\n" if lines[first].endswith("\r\n") else "\n" if lines[first].endswith("\n") else ""
        lines[first] = f"{key}=1{newline}"
        for index in reversed(flag_indices[1:]):
            del lines[index]
        return "".join(lines)

    if rpg_rt_index is None:
        raise ValueError("RPG_RT.ini 缺少 [RPG_RT] 段落")
    insert_at = rpg_rt_index + 1
    while insert_at < len(lines):
        candidate = lines[insert_at].strip()
        if candidate.startswith("[") and candidate.endswith("]") and "=" not in candidate:
            break
        insert_at += 1
    lines.insert(insert_at, "FullPackageFlag=1\n")
    return "".join(lines)


def set_game_title(text, title):
    lines = text.splitlines(keepends=True)
    section = None
    rpg_rt_index = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip().casefold()
            if section == "rpg_rt":
                rpg_rt_index = index
            continue
        if section == "rpg_rt" and "=" in line:
            key, _value = line.split("=", 1)
            if key.strip().casefold() == "gametitle":
                newline = "\r\n" if line.endswith("\r\n") else "\n"
                lines[index] = f"{key}={title}{newline}"
                return "".join(lines)

    if rpg_rt_index is None:
        raise ValueError("RPG_RT.ini 缺少 [RPG_RT] 段落")
    insert_at = rpg_rt_index + 1
    while insert_at < len(lines) and not lines[insert_at].strip().startswith("["):
        insert_at += 1
    lines.insert(insert_at, f"GameTitle={title}\n")
    return "".join(lines)


def encode_ini(text, target_encoding):
    return text.encode(codec_name(target_encoding))
