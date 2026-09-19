"""Invalidate the saved map revision without reserializing the rest of an LSD.

LCF field definitions: EasyRPG/liblcf src/generated/lcf/{lsd,lmu}/chunks.h.
No game text is decoded. Unknown payloads remain opaque and are copied verbatim.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile


MAX_FILE_BYTES = 128 * 1024 * 1024
LSD_HEADER = b"\x0bLcfSaveData"
LMU_HEADER = b"\x0aLcfMapUnit"
PARTY_LOCATION = 0x68
MAP_SAVE_COUNT = 0x83


class RefreshError(ValueError):
    """An input or a write could not be handled without risking the original."""


@dataclass(frozen=True)
class Chunk:
    tag: int
    start: int
    length_start: int
    payload_start: int
    end: int


@dataclass(frozen=True)
class RefreshPlan:
    save_path: Path
    map_path: Path
    map_id: int
    x: int
    y: int
    old_count: int
    new_count: int
    map_counts: tuple
    original: bytes
    candidate: bytes
    map_digest: str

    @property
    def changed(self):
        return self.original != self.candidate


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _read_file(path):
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise RefreshError(f"无法读取 {path}：{exc}") from exc
    if len(data) > MAX_FILE_BYTES:
        raise RefreshError(f"文件超过 128 MiB，未处理：{path}")
    return data


def _read_uint(data, offset):
    value = 0
    for _ in range(5):
        if offset >= len(data):
            raise RefreshError("LCF 整数字段被截断。")
        byte = data[offset]
        offset += 1
        value = (value << 7) | (byte & 0x7f)
        if value > 0xffffffff:
            raise RefreshError("LCF 整数字段超过 32 位。")
        if not byte & 0x80:
            return value, offset
    raise RefreshError("LCF 整数字段长度异常。")


def _uint(value):
    result = [value & 0x7f]
    value >>= 7
    while value:
        result.append((value & 0x7f) | 0x80)
        value >>= 7
    return bytes(reversed(result))


def _chunks(data, start=0, *, terminated=False):
    result = []
    seen = set()
    offset = start
    while offset < len(data):
        begin = offset
        tag, length_start = _read_uint(data, offset)
        if tag == 0:
            if length_start != len(data):
                raise RefreshError("LCF 结束标记之后存在额外数据，未处理。")
            return result, begin
        if tag in seen:
            raise RefreshError(f"LCF 包含重复区块 0x{tag:X}，未处理。")
        seen.add(tag)
        length, payload_start = _read_uint(data, length_start)
        offset = payload_start + length
        if offset > len(data):
            raise RefreshError(f"LCF 区块 0x{tag:X} 长度越界。")
        result.append(Chunk(tag, begin, length_start, payload_start, offset))
    if terminated:
        raise RefreshError("玩家位置区块缺少结束标记。")
    return result, offset


def _find(chunks, tag):
    return next((chunk for chunk in chunks if chunk.tag == tag), None)


def _integer(data, chunks, tag, default=0):
    chunk = _find(chunks, tag)
    if chunk is None:
        return default
    payload = data[chunk.payload_start:chunk.end]
    value, end = _read_uint(payload, 0)
    if end != len(payload):
        raise RefreshError(f"LCF 整数区块 0x{tag:X} 长度不正确。")
    return value


def _save_parts(data):
    if not data.startswith(LSD_HEADER):
        raise RefreshError("文件不是标准 LcfSaveData 存档。")
    root, _ = _chunks(data, len(LSD_HEADER))
    party = _find(root, PARTY_LOCATION)
    if party is None:
        raise RefreshError("存档缺少玩家位置区块。")
    payload = data[party.payload_start:party.end]
    fields, terminator = _chunks(payload, terminated=True)
    if not 1 <= _integer(payload, fields, 0x0b) <= 9999:
        raise RefreshError("存档地图编号不在标准 Map0001–Map9999 范围内。")
    return party, payload, fields, terminator


def _without_count(payload, fields):
    field = _find(fields, MAP_SAVE_COUNT)
    return payload if field is None else payload[:field.start] + payload[field.end:]


def _patch_count(data, value):
    party, payload, fields, terminator = _save_parts(data)
    field = _find(fields, MAP_SAVE_COUNT)
    encoded = _uint(value)
    if field is None:
        # Keep the ordering expected by older readers without moving other fields.
        insert_at = next((c.start for c in fields if c.tag > MAP_SAVE_COUNT), terminator)
        updated = payload[:insert_at] + _uint(MAP_SAVE_COUNT) + _uint(len(encoded)) + encoded + payload[insert_at:]
    else:
        updated = payload[:field.length_start] + _uint(len(encoded)) + encoded + payload[field.end:]
    candidate = data[:party.length_start] + _uint(len(updated)) + updated + data[party.end:]

    new_party, new_payload, new_fields, _ = _save_parts(candidate)
    if (data[:party.start] != candidate[:new_party.start]
            or data[party.end:] != candidate[new_party.end:]
            or _without_count(payload, fields) != _without_count(new_payload, new_fields)
            or _integer(new_payload, new_fields, MAP_SAVE_COUNT) != value):
        raise RefreshError("候选存档的字节保留校验失败，未写入。")
    return candidate


def plan_refresh(project, save):
    save_path = Path(save).expanduser().resolve()
    if save_path.suffix.casefold() != ".lsd":
        raise RefreshError("请选择 .lsd 存档文件。")
    original = _read_file(save_path)
    _, payload, fields, _ = _save_parts(original)
    map_id = _integer(payload, fields, 0x0b)
    project = Path(project).expanduser().resolve()
    if not project.is_dir():
        raise RefreshError("请选择汉化后的游戏目录。")
    map_name = f"Map{map_id:04d}.lmu"
    matches = [p for p in project.iterdir() if p.name.casefold() == map_name.casefold() and p.is_file()]
    if len(matches) != 1:
        raise RefreshError(f"游戏目录中没有唯一的 {map_name}，无法确定地图计数。")
    map_path = matches[0]
    map_data = _read_file(map_path)
    if not map_data.startswith(LMU_HEADER):
        raise RefreshError(f"{map_path.name} 不是标准 LcfMapUnit 地图。")
    map_fields, _ = _chunks(map_data, len(LMU_HEADER))
    counts = (_integer(map_data, map_fields, 0x5b), _integer(map_data, map_fields, 0x5a))
    old_count = _integer(payload, fields, MAP_SAVE_COUNT)
    # Different from BOTH legacy and Steam counts: no guessed engine detection.
    new_count = old_count if old_count not in counts else next(v for v in range(3) if v not in counts)
    candidate = original if new_count == old_count else _patch_count(original, new_count)
    return RefreshPlan(save_path, map_path, map_id,
                       _integer(payload, fields, 0x0c), _integer(payload, fields, 0x0d),
                       old_count, new_count, counts, original, candidate, digest(map_data))


def _commit(path, original, candidate, action, *, create_backup=True):
    """Optionally back up, stage beside the save, then replace; game must be closed."""
    if _read_file(path) != original:
        raise RefreshError("存档在读取后发生变化，请关闭游戏并重新读取。")
    if original == candidate:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = path.with_name(f"{path.name}.{action}.{stamp}.{digest(original)[:12]}.bak") if create_backup else None
    temporary = None
    backup_complete = False
    try:
        if backup is not None:
            with backup.open("xb") as stream:
                stream.write(original)
                stream.flush()
                os.fsync(stream.fileno())
            if _read_file(backup) != original:
                raise RefreshError("备份校验失败，原存档未修改。")
            backup_complete = True
        with tempfile.NamedTemporaryFile(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(candidate)
            stream.flush()
            os.fsync(stream.fileno())
        if _read_file(temporary) != candidate:
            raise RefreshError("临时存档校验失败，原存档未修改。")
        os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
        if _read_file(path) != original:
            raise RefreshError("存档在写入前发生变化，请关闭游戏并重新读取。")
        os.replace(temporary, path)
        temporary = None
    except (OSError, RefreshError) as exc:
        note = f"\n原存档备份：{backup}" if backup_complete else "\n原存档未修改。"
        raise RefreshError(f"操作未完成：{exc}{note}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return backup


def apply_refresh(plan, *, create_backup=True):
    """Apply the plan; return its backup path, or None when no backup is made."""
    if digest(_read_file(plan.map_path)) != plan.map_digest:
        raise RefreshError("地图在预览后发生变化，请重新读取存档。")
    return _commit(plan.save_path, plan.original, plan.candidate, "map-refresh", create_backup=create_backup)


def restore_backup(save, backup):
    save_path = Path(save).expanduser().resolve()
    backup_path = Path(backup).expanduser().resolve()
    pattern = re.escape(save_path.name) + r"\.(?:map-refresh|before-restore)\.\d{8}T\d{12}Z\.([0-9a-f]{12})\.bak"
    match = re.fullmatch(pattern, backup_path.name, re.IGNORECASE)
    if not match or backup_path == save_path:
        raise RefreshError("请选择由本工具为同名存档生成的备份。")
    candidate = _read_file(backup_path)
    if digest(candidate)[:12] != match.group(1).lower():
        raise RefreshError("备份内容与文件名中的校验值不一致，未恢复。")
    _save_parts(candidate)
    original = _read_file(save_path)
    # Keep the current file too, including a damaged file the user is restoring.
    return _commit(save_path, original, candidate, "before-restore")
