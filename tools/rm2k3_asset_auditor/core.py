from __future__ import annotations

import csv
import json
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional, Sequence

APP_NAME = "RM2K/3 素材调用检查器"
VERSION = "0.4.1"

# RPG Maker 2000/2003 standard resource folders.
RESOURCE_CATEGORIES: dict[str, tuple[str, ...]] = {
    "Backdrop": (".png", ".bmp", ".xyz"),
    "Battle": (".png", ".bmp", ".xyz"),
    "Battle2": (".png", ".bmp", ".xyz"),
    "BattleCharSet": (".png", ".bmp", ".xyz"),
    "BattleWeapon": (".png", ".bmp", ".xyz"),
    "CharSet": (".png", ".bmp", ".xyz"),
    "ChipSet": (".png", ".bmp", ".xyz"),
    "FaceSet": (".png", ".bmp", ".xyz"),
    "Frame": (".png", ".bmp", ".xyz"),
    "GameOver": (".png", ".bmp", ".xyz"),
    "Monster": (".png", ".bmp", ".xyz"),
    "Movie": (".avi", ".mpg", ".mpeg", ".wmv", ".mp4"),
    "Music": (".mid", ".midi", ".wav", ".mp3", ".ogg", ".opus", ".flac", ".wma"),
    "Panorama": (".png", ".bmp", ".xyz"),
    "Picture": (".png", ".bmp", ".xyz"),
    "Sound": (".wav", ".mp3", ".ogg", ".opus", ".flac", ".wma", ".mid", ".midi"),
    "System": (".png", ".bmp", ".xyz"),
    "System2": (".png", ".bmp", ".xyz"),
    "Title": (".png", ".bmp", ".xyz"),
}

DEFAULT_DETAIL_CATEGORIES = ("Picture", "Panorama")

# LCF event command codes from liblcf's rpg::EventCommand::Code.
EVENT_ASSET_COMMANDS: dict[int, tuple[str, str]] = {
    10130: ("FaceSet", "ChangeFaceGraphic"),
    10630: ("CharSet", "ChangeSpriteAssociation"),
    10640: ("FaceSet", "ChangeActorFace"),
    10650: ("CharSet", "ChangeVehicleGraphic"),
    10660: ("Music", "ChangeSystemBGM"),
    10670: ("Sound", "ChangeSystemSFX"),
    10680: ("System", "ChangeSystemGraphics"),
    11110: ("Picture", "ShowPicture"),
    11510: ("Music", "PlayBGM"),
    11550: ("Sound", "PlaySound"),
    11560: ("Movie", "PlayMovie"),
    11720: ("Panorama", "ChangePBG"),
    13210: ("Backdrop", "ChangeBattleBG"),
}

MOVE_ASSET_COMMANDS: dict[int, tuple[str, str]] = {
    34: ("CharSet", "MoveRoute.ChangeGraphic"),
    35: ("Sound", "MoveRoute.PlaySoundEffect"),
}

OFF_NAMES = {
    "", "(off)", "off", "(none)", "none", "(なし)", "なし", "（なし）",
}

DYNAMIC_PATTERNS = (
    re.compile(r"\\[vns]\[", re.I),
    re.compile(r"\$\{.+?\}"),
    re.compile(r"^v\[\d+\]$", re.I),
)


@dataclass(frozen=True)
class AssetFile:
    category: str
    name: str
    path: str
    extension: str
    size: int
    source: str = "project"  # project or external/RTP

    @property
    def normalized(self) -> str:
        return normalize_asset_name(self.name)


@dataclass(frozen=True)
class MapInfo:
    map_id: int
    file_name: str
    name: str
    parent_id: int = 0
    indentation: int = 0
    map_type: int = 1

    @property
    def display_name(self) -> str:
        label = self.name or "（未命名地图）"
        return f"Map{self.map_id:04d} — {label}"


@dataclass(frozen=True)
class AssetReference:
    category: str
    asset_name: str
    source_kind: str  # Map, Database, MapTree, Fallback
    source_id: str
    source_name: str
    location: str
    event_id: Optional[int] = None
    event_name: str = ""
    page: Optional[int] = None
    command_index: Optional[int] = None
    command_code: Optional[int] = None
    command_name: str = ""
    confidence: str = "exact"  # exact / approximate / dynamic
    raw_source: str = ""

    @property
    def normalized(self) -> str:
        return normalize_asset_name(self.asset_name)

    @property
    def unique_key(self) -> tuple:
        return (
            self.category,
            self.normalized,
            self.source_kind,
            self.source_id,
            self.location,
            self.event_id,
            self.page,
            self.command_index,
        )


@dataclass
class AnalysisResult:
    project_root: str
    mode: str
    complete_scan: bool = True
    encoding_settings: dict[str, str] = field(default_factory=dict)
    maps: list[MapInfo] = field(default_factory=list)
    references: list[AssetReference] = field(default_factory=list)
    project_assets: list[AssetFile] = field(default_factory=list)
    external_assets: list[AssetFile] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    generated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    # AnalysisResult is populated once and then treated as immutable.  These
    # private lazy caches avoid rebuilding the same indexes for every one of
    # thousands of detail rows in large projects.
    _dedup_cache: Optional[list[AssetReference]] = field(default=None, init=False, repr=False)
    _project_index_cache: Optional[dict[tuple[str, str], list[AssetFile]]] = field(default=None, init=False, repr=False)
    _external_index_cache: Optional[dict[tuple[str, str], list[AssetFile]]] = field(default=None, init=False, repr=False)
    _file_info_cache: dict[tuple[str, str], dict[str, str]] = field(default_factory=dict, init=False, repr=False)

    def deduplicated_references(self) -> list[AssetReference]:
        if self._dedup_cache is None:
            seen: set[tuple] = set()
            out: list[AssetReference] = []
            for ref in self.references:
                if ref.unique_key in seen:
                    continue
                seen.add(ref.unique_key)
                out.append(ref)
            self._dedup_cache = out
        return self._dedup_cache

    def _asset_index(self, external: bool = False) -> dict[tuple[str, str], list[AssetFile]]:
        cache_name = "_external_index_cache" if external else "_project_index_cache"
        cached = getattr(self, cache_name)
        if cached is not None:
            return cached
        files = self.external_assets if external else self.project_assets
        idx: dict[tuple[str, str], list[AssetFile]] = {}
        for asset in files:
            idx.setdefault((asset.category, asset.normalized), []).append(asset)
        setattr(self, cache_name, idx)
        return idx

    def matching_assets(self, category: str, asset_name: str) -> list[AssetFile]:
        """Return real project/external files matching one LCF resource reference.

        Project files are returned before external/RTP files. Multiple extensions or
        duplicate files are preserved because RPG Maker references usually omit the
        extension and users may need to inspect every physical match.
        """
        key = (category, normalize_asset_name(asset_name))
        matches = self._asset_index(False).get(key, []) + self._asset_index(True).get(key, [])
        return sorted(matches, key=lambda a: (0 if a.source == "project" else 1, a.path.casefold()))

    def asset_file_info(self, category: str, asset_name: str) -> dict[str, str]:
        key = (category, normalize_asset_name(asset_name))
        cached = self._file_info_cache.get(key)
        if cached is not None:
            return cached
        matches = self.matching_assets(category, asset_name)
        project_count = sum(1 for item in matches if item.source == "project")
        external_count = sum(1 for item in matches if item.source != "project")
        if project_count and external_count:
            status = "工程和外部/RTP均存在"
        elif project_count:
            status = "工程文件"
        elif external_count:
            status = "外部/RTP文件"
        else:
            status = "未找到实际文件"
        info = {
            "file_status": status,
            "file_names": "；".join(dict.fromkeys(Path(item.path).name for item in matches)),
            "file_paths": "；".join(dict.fromkeys(item.path for item in matches)),
        }
        self._file_info_cache[key] = info
        return info

    def usage_summary(self, categories: Optional[set[str]] = None) -> list[dict]:
        grouped: dict[tuple[str, str], dict] = {}
        for ref in self.deduplicated_references():
            if categories and ref.category not in categories:
                continue
            key = (ref.category, ref.normalized)
            row = grouped.setdefault(key, {
                "category": ref.category,
                "asset_name": ref.asset_name,
                "normalized": ref.normalized,
                "reference_count": 0,
                "sources": set(),
                "confidence": set(),
            })
            row["reference_count"] += 1
            row["sources"].add(f"{ref.source_kind}:{ref.source_id}")
            row["confidence"].add(ref.confidence)
        rows = []
        for row in grouped.values():
            row["sources"] = "；".join(sorted(row["sources"]))
            row["confidence"] = ",".join(sorted(row["confidence"]))
            row.update(self.asset_file_info(row["category"], row["asset_name"]))
            rows.append(row)
        return sorted(rows, key=lambda r: (r["category"].casefold(), r["asset_name"].casefold()))

    def reference_rows(self, categories: Optional[set[str]] = None) -> list[dict]:
        rows: list[dict] = []
        for ref in self.deduplicated_references():
            if categories and ref.category not in categories:
                continue
            row = asdict(ref)
            row.update(self.asset_file_info(ref.category, ref.asset_name))
            rows.append(row)
        return rows

    def missing_references(self, categories: Optional[set[str]] = None) -> list[dict]:
        project_idx = self._asset_index(False)
        external_idx = self._asset_index(True)
        rows = []
        for ref in self.deduplicated_references():
            if categories and ref.category not in categories:
                continue
            if should_ignore_reference_name(ref.asset_name):
                continue
            if ref.confidence == "dynamic":
                status = "动态文件名，无法静态确认"
            elif (ref.category, ref.normalized) in project_idx:
                continue
            elif (ref.category, ref.normalized) in external_idx:
                status = "项目目录缺失，但外部/RTP目录存在"
            else:
                status = "项目目录缺失"
            row = asdict(ref)
            row["status"] = status
            rows.append(row)
        return sorted(rows, key=lambda r: (r["category"], r["asset_name"].casefold(), r["source_id"]))

    def unused_assets(self, categories: Optional[set[str]] = None) -> list[AssetFile]:
        used = {
            (ref.category, ref.normalized)
            for ref in self.deduplicated_references()
            if ref.confidence != "dynamic" and not should_ignore_reference_name(ref.asset_name)
        }
        out = []
        for asset in self.project_assets:
            if categories and asset.category not in categories:
                continue
            if (asset.category, asset.normalized) not in used:
                out.append(asset)
        return sorted(out, key=lambda a: (a.category, a.name.casefold()))

    def to_dict(self, categories: Optional[set[str]] = None) -> dict:
        refs = [
            x for x in self.deduplicated_references()
            if not categories or x.category in categories
        ]
        return {
            "project_root": self.project_root,
            "mode": self.mode,
            "generated_at": self.generated_at,
            "complete_scan": self.complete_scan,
            "encoding_settings": self.encoding_settings,
            "warnings": self.warnings,
            "maps": [asdict(x) for x in self.maps],
            "references": self.reference_rows(categories),
            "project_assets": [asdict(x) for x in self.project_assets],
            "external_assets": [asdict(x) for x in self.external_assets],
            "usage_summary": self.usage_summary(categories),
            "missing_references": self.missing_references(categories),
            "unused_assets": [asdict(x) for x in self.unused_assets(categories)],
        }


class AnalysisError(RuntimeError):
    pass


def strip_namespace(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def text_of(element: Optional[ET.Element], default: str = "") -> str:
    if element is None or element.text is None:
        return default
    return element.text.strip()


def direct_child(parent: ET.Element, tag: str) -> Optional[ET.Element]:
    wanted = tag.casefold()
    for child in parent:
        if strip_namespace(child.tag).casefold() == wanted:
            return child
    return None


def direct_text(parent: ET.Element, tag: str, default: str = "") -> str:
    return text_of(direct_child(parent, tag), default)


def safe_int(value: str, default: int = 0) -> int:
    try:
        return int(value.strip())
    except (AttributeError, TypeError, ValueError):
        return default


def safe_bool(value: str, default: bool = False) -> bool:
    text = str(value or "").strip().casefold()
    if text in {"1", "true", "t", "yes", "on"}:
        return True
    if text in {"0", "false", "f", "no", "off", ""}:
        return False
    return default


def normalize_asset_name(name: str) -> str:
    value = unicodedata.normalize("NFKC", str(name or "")).strip()
    value = value.replace("\\", "/").rsplit("/", 1)[-1]
    suffix = Path(value).suffix
    if suffix.casefold() in {
        ext for extensions in RESOURCE_CATEGORIES.values() for ext in extensions
    }:
        value = value[: -len(suffix)]
    return value.rstrip(" .").casefold()


def should_ignore_reference_name(name: str) -> bool:
    clean = unicodedata.normalize("NFKC", str(name or "")).strip()
    return clean.casefold() in OFF_NAMES


def is_dynamic_reference(name: str) -> bool:
    clean = str(name or "").strip()
    return any(pattern.search(clean) for pattern in DYNAMIC_PATTERNS)


def discover_assets(project_root: Path, source: str = "project") -> list[AssetFile]:
    assets: list[AssetFile] = []
    if not project_root.exists():
        return assets
    directory_lookup = {p.name.casefold(): p for p in project_root.iterdir() if p.is_dir()}
    for category, extensions in RESOURCE_CATEGORIES.items():
        folder = directory_lookup.get(category.casefold())
        if not folder:
            continue
        allowed = {e.casefold() for e in extensions}
        for path in folder.rglob("*"):
            if not path.is_file() or path.suffix.casefold() not in allowed:
                continue
            rel_name = path.relative_to(folder).as_posix()
            # LCF references normally omit extensions, but subdirectories are retained if present.
            name = rel_name[: -len(path.suffix)] if path.suffix else rel_name
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            assets.append(AssetFile(category, name, str(path), path.suffix, size, source))
    return sorted(assets, key=lambda a: (a.category, a.name.casefold()))


def detect_engine(project_root: Path) -> str:
    ini = project_root / "RPG_RT.ini"
    if ini.exists():
        try:
            text = ini.read_text(encoding="cp932", errors="ignore")
            if "RPG_RT 2000" in text or "RPG2000" in text.upper():
                return "2k"
            if "RPG_RT 2003" in text or "RPG2003" in text.upper():
                return "2k3"
        except OSError:
            pass
    # RPG_RT.ldb format alone is not a completely reliable discriminator.
    return "2k3"


def find_lcf2xml(app_dir: Optional[Path] = None, explicit: Optional[Path] = None) -> Optional[Path]:
    candidates: list[Path] = []
    if explicit:
        candidates.append(explicit)
    if app_dir:
        candidates.extend([
            app_dir / "tools" / "lcf2xml.exe",
            app_dir / "lcf2xml.exe",
            app_dir / "tools" / "lcf2xml",
            app_dir / "lcf2xml",
        ])
    found = shutil.which("lcf2xml") or shutil.which("lcf2xml.exe")
    if found:
        candidates.append(Path(found))
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate.resolve()
    return None


def _path_is_ascii(path: Path) -> bool:
    try:
        str(path).encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def _ascii_work_base(source_hint: Optional[Path] = None) -> Path:
    """Return a writable ASCII-only directory for legacy command-line tools.

    lcf2xml can be launched through Unicode-aware CreateProcessW, but some Windows
    builds still open their input through a narrow string API. Passing only a
    relative ASCII filename from an ASCII working directory avoids corruption of
    Chinese/Japanese project paths.
    """
    candidates: list[Path] = []
    override = os.environ.get("RM2K3_AUDITOR_TEMP")
    if override:
        candidates.append(Path(override))
    candidates.append(Path(tempfile.gettempdir()))
    if source_hint is not None:
        resolved = source_hint.resolve()
        candidates.extend(resolved.parents)
    if os.name == "nt":
        system_drive = os.environ.get("SystemDrive", "C:")
        candidates.extend([Path(system_drive + "\\Temp"), Path(system_drive + "\\")])

    checked: set[str] = set()
    for candidate in candidates:
        try:
            candidate = candidate.resolve()
        except OSError:
            continue
        key = str(candidate).casefold()
        if key in checked or not _path_is_ascii(candidate):
            continue
        checked.add(key)
        base = candidate / "rm2k3_auditor_temp"
        try:
            base.mkdir(parents=True, exist_ok=True)
            probe = base / ".write_test"
            probe.write_bytes(b"ok")
            probe.unlink()
            return base
        except OSError:
            continue
    raise AnalysisError(
        "找不到可写的纯英文临时目录，无法安全调用 lcf2xml。"
        "请新建例如 C:\\RM2K3_Temp，并设置环境变量 RM2K3_AUDITOR_TEMP 指向该目录。"
    )


def _decode_process_output(data: bytes) -> str:
    if not data:
        return ""
    codecs = ["utf-8"]
    if os.name == "nt":
        codecs.append("mbcs")
    codecs.extend(["cp932", "cp936", "cp1252"])
    for codec in codecs:
        try:
            return data.decode(codec)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _safe_cache_component(value: str) -> str:
    value = value.strip() or "auto"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)


def _lcf2xml_timeout_seconds(source: Path) -> float:
    """Return a bounded per-file timeout for legacy lcf2xml processes.

    A malformed LCF file or an incompatible converter build can otherwise keep
    the GUI worker alive forever.  The environment override is useful for very
    slow machines or unusually large projects.
    """
    override = os.environ.get("RM2K3_LCF2XML_TIMEOUT", "").strip()
    if override:
        try:
            return max(10.0, float(override))
        except ValueError:
            pass
    try:
        size_mb = source.stat().st_size / (1024 * 1024)
    except OSError:
        size_mb = 0.0
    return min(180.0, max(45.0, 45.0 + size_mb * 60.0))


def validate_lcf_header(source: Path) -> Optional[str]:
    expected = {
        ".ldb": b"\x0bLcfDataBase",
        ".lmt": b"\x0aLcfMapTree",
        ".lmu": b"\x0aLcfMapUnit",
    }.get(source.suffix.casefold())
    if expected is None:
        return None
    try:
        size = source.stat().st_size
        with source.open("rb") as fh:
            head = fh.read(max(32, len(expected)))
    except OSError as exc:
        return f"无法读取文件：{exc}"
    if size == 0:
        return "文件大小为 0 字节"
    if not head.startswith(expected):
        if head and not any(head):
            return "文件开头全部为 00，疑似零填充或数据损坏"
        actual = head[: len(expected)].hex(" ").upper()
        return f"LCF 文件头不正确；实际开头：{actual}"
    return None


def convert_lcf_files(
    lcf2xml: Path,
    files: Sequence[Path],
    cache_dir: Path,
    engine: str = "2k3",
    encoding: str = "932",
    progress: Optional[Callable[[str], None]] = None,
    *,
    skip_errors: bool = False,
    errors: Optional[list[str]] = None,
    stage_label: str = "转换",
) -> dict[Path, Path]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    result: dict[Path, Path] = {}
    ext_map = {".ldb": ".edb", ".lmt": ".emt", ".lmu": ".emu"}
    work_base = _ascii_work_base(files[0] if files else cache_dir)
    total = len(files)
    for index, source in enumerate(files, 1):
        if progress:
            progress(f"{stage_label} {index}/{total}：{source.name}（编码 {encoding or '自动'}）")
        suffix = source.suffix.casefold()
        if suffix not in ext_map:
            message = f"不支持的 LCF 文件类型：{source.name}"
            if skip_errors:
                if errors is not None:
                    errors.append(message)
                continue
            raise AnalysisError(message)
        expected = cache_dir / f"{source.stem}{ext_map[suffix]}"
        if expected.exists() and expected.stat().st_mtime >= source.stat().st_mtime:
            result[source] = expected
            continue

        header_problem = validate_lcf_header(source)
        if header_problem:
            message = f"LCF 文件预检查失败：{source.name}\n{header_problem}\n原文件：{source}"
            if skip_errors:
                if errors is not None:
                    errors.append(message)
                continue
            raise AnalysisError(message)

        try:
            with tempfile.TemporaryDirectory(prefix="job_", dir=str(work_base)) as td:
                work_dir = Path(td)
                # RPG Maker project filenames are normally ASCII. Use a generated
                # fallback name anyway so the tool never receives a non-ASCII arg.
                staged_name = source.name if source.name.isascii() else f"input{suffix}"
                staged_source = work_dir / staged_name
                shutil.copy2(source, staged_source)
                staged_expected = work_dir / f"{Path(staged_name).stem}{ext_map[suffix]}"
                cmd = [str(lcf2xml), f"--{engine}"]
                if encoding and encoding.casefold() != "auto":
                    cmd += ["--encoding", encoding]
                # Deliberately pass a relative ASCII filename. This is the core
                # fix for lcf2xml failures under Chinese/Japanese project paths.
                cmd.append(staged_source.name)
                timeout_seconds = _lcf2xml_timeout_seconds(source)
                proc = subprocess.run(
                    cmd,
                    cwd=str(work_dir),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=False,
                    timeout=timeout_seconds,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if proc.returncode != 0 or not staged_expected.exists():
                    detail = (_decode_process_output(proc.stderr) or _decode_process_output(proc.stdout) or "未知错误").strip()
                    raise AnalysisError(
                        f"lcf2xml 转换失败：{source.name}\n{detail}\n"
                        f"原文件：{source}\n已使用纯英文临时目录重试：{work_dir}"
                    )
                shutil.copy2(staged_expected, expected)
                result[source] = expected
        except subprocess.TimeoutExpired as exc:
            timeout_seconds = _lcf2xml_timeout_seconds(source)
            message = (
                f"lcf2xml 转换超时：{source.name}\n"
                f"超过 {timeout_seconds:.0f} 秒仍未完成，进程已终止。\n"
                f"原文件：{source}\n"
                "这通常表示该文件异常、转换器版本不兼容，或机器当时资源不足。"
            )
            if skip_errors:
                if errors is not None:
                    errors.append(message)
                continue
            raise AnalysisError(message) from exc
        except (OSError, AnalysisError) as exc:
            message = str(exc)
            if skip_errors:
                if errors is not None:
                    errors.append(message)
                continue
            if isinstance(exc, AnalysisError):
                raise
            raise AnalysisError(f"lcf2xml 转换失败：{source.name}\n{exc}") from exc
    return result


def _map_name_lookup(root: ET.Element) -> dict[int, str]:
    names: dict[int, str] = {}
    for node in root.iter():
        if strip_namespace(node.tag) != "MapInfo":
            continue
        map_id = safe_int(node.get("id", "0"))
        if map_id > 0:
            names[map_id] = direct_text(node, "name")
    return names


def parse_map_tree(
    xml_path: Path,
    label_xml_path: Optional[Path] = None,
) -> tuple[list[MapInfo], list[AssetReference]]:
    """Parse map hierarchy and map-tree resources using independent encodings.

    ``xml_path`` supplies asset filenames and numeric structure.
    ``label_xml_path`` optionally supplies map display names decoded with a
    different codepage. This supports partially translated projects where map
    names and material filenames no longer share one encoding.
    """
    root = ET.parse(xml_path).getroot()
    label_root = ET.parse(label_xml_path).getroot() if label_xml_path else root
    map_names = _map_name_lookup(label_root)
    maps: list[MapInfo] = []
    raw: dict[int, dict] = {}
    for node in root.iter():
        if strip_namespace(node.tag) != "MapInfo":
            continue
        map_id = safe_int(node.get("id", "0"))
        if map_id <= 0:
            continue
        name = map_names.get(map_id, direct_text(node, "name"))
        info = MapInfo(
            map_id=map_id,
            file_name=f"Map{map_id:04d}.lmu",
            name=name,
            parent_id=safe_int(direct_text(node, "parent_map")),
            indentation=safe_int(direct_text(node, "indentation")),
            map_type=safe_int(direct_text(node, "type"), 1),
        )
        maps.append(info)
        music_name = ""
        music_node = direct_child(node, "music")
        if music_node is not None:
            for child in music_node.iter():
                if strip_namespace(child.tag) == "Music":
                    music_name = direct_text(child, "name")
                    break
        raw[map_id] = {
            "info": info,
            "music_type": safe_int(direct_text(node, "music_type")),
            "music_name": music_name,
            "background_type": safe_int(direct_text(node, "background_type")),
            "background_name": direct_text(node, "background_name"),
        }

    def inherited(map_id: int, type_key: str, name_key: str) -> tuple[str, Optional[int]]:
        visited: set[int] = set()
        current_id = map_id
        while current_id in raw and current_id not in visited:
            visited.add(current_id)
            row = raw[current_id]
            mode = safe_int(str(row[type_key]))
            if mode == 2:
                return str(row[name_key] or ""), current_id
            if mode == 1:
                return "", None
            current_id = row["info"].parent_id
        return "", None

    refs: list[AssetReference] = []
    for info in maps:
        music_name, music_origin = inherited(info.map_id, "music_type", "music_name")
        if not should_ignore_reference_name(music_name):
            origin = raw.get(music_origin or -1, {}).get("info")
            inherited_note = ""
            if origin is not None and origin.map_id != info.map_id:
                inherited_note = f"（继承自 Map{origin.map_id:04d}「{origin.name}」）"
            refs.append(AssetReference(
                category="Music", asset_name=music_name,
                source_kind="MapTree", source_id=f"Map{info.map_id:04d}", source_name=info.name,
                location=f"地图属性 / 自动 BGM{inherited_note}",
                command_name="MapInfo.music", raw_source=str(xml_path),
            ))
        backdrop_name, backdrop_origin = inherited(info.map_id, "background_type", "background_name")
        if not should_ignore_reference_name(backdrop_name):
            origin = raw.get(backdrop_origin or -1, {}).get("info")
            inherited_note = ""
            if origin is not None and origin.map_id != info.map_id:
                inherited_note = f"（继承自 Map{origin.map_id:04d}「{origin.name}」）"
            refs.append(AssetReference(
                category="Backdrop", asset_name=backdrop_name,
                source_kind="MapTree", source_id=f"Map{info.map_id:04d}", source_name=info.name,
                location=f"地图属性 / 战斗背景{inherited_note}",
                command_name="MapInfo.background_name", raw_source=str(xml_path),
            ))
    maps.sort(key=lambda m: m.map_id)
    return maps, refs


def _all_with_parent(root: ET.Element) -> Iterator[tuple[ET.Element, Optional[ET.Element]]]:
    stack: list[tuple[ET.Element, Optional[ET.Element]]] = [(root, None)]
    while stack:
        node, parent = stack.pop()
        yield node, parent
        for child in reversed(list(node)):
            stack.append((child, node))


def _nearest_ancestor(node: ET.Element, parent_map: dict[ET.Element, ET.Element], tags: set[str]) -> Optional[ET.Element]:
    current = node
    while current in parent_map:
        current = parent_map[current]
        if strip_namespace(current.tag) in tags:
            return current
    return None


def _entity_label(node: Optional[ET.Element], fallback: str = "") -> tuple[str, str]:
    if node is None:
        return "", fallback
    entity_id = node.get("id", "")
    entity_name = direct_text(node, "name") or fallback
    return entity_id, entity_name


def _append_ref(refs: list[AssetReference], **kwargs) -> None:
    name = str(kwargs.get("asset_name", "")).strip()
    if should_ignore_reference_name(name):
        return
    kwargs["asset_name"] = name
    if is_dynamic_reference(name):
        kwargs["confidence"] = "dynamic"
    refs.append(AssetReference(**kwargs))


def parse_map_xml(
    xml_path: Path,
    map_info: MapInfo,
    chipset_names: Optional[dict[int, str]] = None,
    label_xml_path: Optional[Path] = None,
) -> list[AssetReference]:
    root = ET.parse(xml_path).getroot()
    label_root = ET.parse(label_xml_path).getroot() if label_xml_path else root
    event_names = {
        safe_int(node.get("id", "0")): direct_text(node, "name")
        for node in label_root.iter()
        if strip_namespace(node.tag) == "Event" and safe_int(node.get("id", "0")) > 0
    }
    parent_map = {child: parent for parent in root.iter() for child in parent}
    refs: list[AssetReference] = []

    # LMU map-level resources. In liblcf this is named parallax, while the
    # original editor commonly calls the folder Panorama. Ignore stale names
    # when the parallax flag is disabled.
    map_node = next((x for x in root.iter() if strip_namespace(x.tag) == "Map"), root)
    parallax_name = direct_text(map_node, "parallax_name")
    if safe_bool(direct_text(map_node, "parallax_flag")) and parallax_name:
        _append_ref(refs,
            category="Panorama", asset_name=parallax_name,
            source_kind="Map", source_id=f"Map{map_info.map_id:04d}", source_name=map_info.name,
            location="地图属性 / 远景图（Parallax）", command_name="Map.parallax_name",
            raw_source=str(xml_path))

    chipset_id = safe_int(direct_text(map_node, "chipset_id"))
    if chipset_names and chipset_id in chipset_names:
        _append_ref(refs,
            category="ChipSet", asset_name=chipset_names[chipset_id],
            source_kind="Map", source_id=f"Map{map_info.map_id:04d}", source_name=map_info.name,
            location=f"地图属性 / 图块集 ID {chipset_id}", command_name="Map.chipset_id",
            raw_source=str(xml_path))

    # Event page graphics and commands.
    for event in root.iter():
        if strip_namespace(event.tag) != "Event":
            continue
        event_id = safe_int(event.get("id", "0"))
        event_name = event_names.get(event_id, direct_text(event, "name"))
        pages_container = direct_child(event, "pages")
        pages = []
        if pages_container is not None:
            pages = [x for x in pages_container if strip_namespace(x.tag) == "EventPage"]
        else:
            pages = [x for x in event.iter() if strip_namespace(x.tag) == "EventPage"]
        for page_index, page in enumerate(pages, 1):
            for tag in ("character_name", "charset_name"):
                value = direct_text(page, tag)
                if value:
                    _append_ref(refs,
                        category="CharSet", asset_name=value,
                        source_kind="Map", source_id=f"Map{map_info.map_id:04d}", source_name=map_info.name,
                        location=f"事件 {event_id}「{event_name}」/ 第 {page_index} 页 / 行走图",
                        event_id=event_id, event_name=event_name, page=page_index,
                        command_name=tag, raw_source=str(xml_path))
            commands = [x for x in page.iter() if strip_namespace(x.tag) == "EventCommand"]
            for command_index, command in enumerate(commands, 1):
                code = safe_int(direct_text(command, "code"))
                value = direct_text(command, "string")
                mapping = EVENT_ASSET_COMMANDS.get(code)
                if mapping and value:
                    category, command_name = mapping
                    _append_ref(refs,
                        category=category, asset_name=value,
                        source_kind="Map", source_id=f"Map{map_info.map_id:04d}", source_name=map_info.name,
                        location=f"事件 {event_id}「{event_name}」/ 第 {page_index} 页 / 命令 {command_index}",
                        event_id=event_id, event_name=event_name, page=page_index,
                        command_index=command_index, command_code=code, command_name=command_name,
                        raw_source=str(xml_path))
                # Change Map Tileset stores an LDB ID rather than a filename.
                if code == 11710 and chipset_names:
                    parameters_node = direct_child(command, "parameters")
                    params = []
                    if parameters_node is not None:
                        params = [safe_int(text_of(x), -1) for x in parameters_node.iter() if x is not parameters_node and text_of(x)]
                    chipset_id = params[0] if params else -1
                    if chipset_id in chipset_names:
                        _append_ref(refs,
                            category="ChipSet", asset_name=chipset_names[chipset_id],
                            source_kind="Map", source_id=f"Map{map_info.map_id:04d}", source_name=map_info.name,
                            location=f"事件 {event_id}「{event_name}」/ 第 {page_index} 页 / 命令 {command_index}",
                            event_id=event_id, event_name=event_name, page=page_index,
                            command_index=command_index, command_code=code, command_name="ChangeMapTileset",
                            raw_source=str(xml_path))

            # Move routes can occur inside event commands or page autonomous movement.
            move_commands = [x for x in page.iter() if strip_namespace(x.tag) == "MoveCommand"]
            for move_index, move_command in enumerate(move_commands, 1):
                code = safe_int(direct_text(move_command, "command_id"), -1)
                value = direct_text(move_command, "parameter_string")
                mapping = MOVE_ASSET_COMMANDS.get(code)
                if mapping and value:
                    category, command_name = mapping
                    _append_ref(refs,
                        category=category, asset_name=value,
                        source_kind="Map", source_id=f"Map{map_info.map_id:04d}", source_name=map_info.name,
                        location=f"事件 {event_id}「{event_name}」/ 第 {page_index} 页 / 移动路线 {move_index}",
                        event_id=event_id, event_name=event_name, page=page_index,
                        command_index=move_index, command_code=code, command_name=command_name,
                        raw_source=str(xml_path))

    # Some malformed or extension-generated maps may place commands outside EventPage.
    known_command_nodes = {id(x) for event in root.iter() if strip_namespace(event.tag) == "Event" for x in event.iter() if strip_namespace(x.tag) == "EventCommand"}
    for idx, command in enumerate((x for x in root.iter() if strip_namespace(x.tag) == "EventCommand"), 1):
        if id(command) in known_command_nodes:
            continue
        code = safe_int(direct_text(command, "code"))
        value = direct_text(command, "string")
        mapping = EVENT_ASSET_COMMANDS.get(code)
        if mapping and value:
            category, command_name = mapping
            _append_ref(refs,
                category=category, asset_name=value,
                source_kind="Map", source_id=f"Map{map_info.map_id:04d}", source_name=map_info.name,
                location=f"地图级命令 {idx}", command_index=idx, command_code=code,
                command_name=command_name, raw_source=str(xml_path))
    return refs


# Direct database field rules. Context-sensitive rules are handled separately.
DB_FIELD_RULES: dict[str, str] = {
    "character_name": "CharSet",
    "charset_name": "CharSet",
    "boat_name": "CharSet",
    "ship_name": "CharSet",
    "airship_name": "CharSet",
    "face_name": "FaceSet",
    "faceset_name": "FaceSet",
    "chipset_name": "ChipSet",
    "picture_name": "Picture",
    "panorama_name": "Panorama",
    "parallax_name": "Panorama",
    "title_name": "Title",
    "gameover_name": "GameOver",
    "system_name": "System",
    "system2_name": "System2",
    "frame_name": "Frame",
    "monster_name": "Monster",
    "animation_name": "Battle",
    "weapon_name": "BattleWeapon",
    "battle_background_name": "Backdrop",
    "battle_background": "Backdrop",
    "battletest_background": "Backdrop",
    "background_a_name": "Backdrop",
    "background_b_name": "Backdrop",
    "battlecharset_name": "BattleCharSet",
    "battleweapon_name": "BattleWeapon",
}

ENTITY_TAGS = {
    "Actor", "Class", "Skill", "Item", "Enemy", "Troop", "TroopPage",
    "Terrain", "Chipset", "Animation", "CommonEvent", "BattlerAnimation",
}


def parse_database_asset_catalog(xml_path: Path) -> dict[str, dict[int, str]]:
    """Return ID-to-filename catalogs needed to resolve map/event numeric IDs."""
    root = ET.parse(xml_path).getroot()
    catalogs: dict[str, dict[int, str]] = {"ChipSet": {}}
    for node in root.iter():
        tag = strip_namespace(node.tag)
        item_id = safe_int(node.get("id", "0"))
        if item_id <= 0:
            continue
        if tag == "Chipset":
            name = direct_text(node, "chipset_name")
            if name:
                catalogs["ChipSet"][item_id] = name
    return catalogs


def _database_entity_name_lookup(root: ET.Element) -> dict[tuple[str, str], str]:
    lookup: dict[tuple[str, str], str] = {}
    for node in root.iter():
        entity_id = node.get("id", "")
        if not entity_id:
            continue
        name = direct_text(node, "name")
        if name:
            lookup[(strip_namespace(node.tag), entity_id)] = name
    return lookup


def _entity_label_decoded(
    node: Optional[ET.Element],
    lookup: dict[tuple[str, str], str],
    fallback: str = "",
) -> tuple[str, str]:
    if node is None:
        return "", fallback
    entity_id = node.get("id", "")
    entity_name = lookup.get((strip_namespace(node.tag), entity_id), direct_text(node, "name") or fallback)
    return entity_id, entity_name


def parse_database_xml(
    xml_path: Path,
    label_xml_path: Optional[Path] = None,
) -> list[AssetReference]:
    root = ET.parse(xml_path).getroot()
    label_root = ET.parse(label_xml_path).getroot() if label_xml_path else root
    entity_names = _database_entity_name_lookup(label_root)
    parent_map = {child: parent for parent in root.iter() for child in parent}
    refs: list[AssetReference] = []
    seen_nodes: set[int] = set()

    for node in root.iter():
        tag = strip_namespace(node.tag)
        tag_cf = tag.casefold()
        value = text_of(node)
        if not value:
            continue
        category = DB_FIELD_RULES.get(tag_cf)
        if tag_cf in {"background_name", "battler_name"}:
            ancestor_tags = []
            cur = node
            while cur in parent_map:
                cur = parent_map[cur]
                ancestor_tags.append(strip_namespace(cur.tag).casefold())
            if tag_cf == "battler_name":
                category = "Monster" if "enemy" in ancestor_tags else "BattleCharSet"
            else:
                # In LDB, background_name belongs to terrain/battle backdrop data.
                category = "Backdrop"
        if not category:
            continue
        entity = _nearest_ancestor(node, parent_map, ENTITY_TAGS)
        entity_id, entity_name = _entity_label_decoded(entity, entity_names)
        entity_tag = strip_namespace(entity.tag) if entity is not None else "System"
        location = f"数据库 / {entity_tag}"
        if entity_id:
            location += f" {entity_id}"
        if entity_name:
            location += f"「{entity_name}」"
        location += f" / {tag}"
        _append_ref(refs,
            category=category, asset_name=value,
            source_kind="Database", source_id="RPG_RT.ldb", source_name="数据库",
            location=location, command_name=tag, raw_source=str(xml_path))
        seen_nodes.add(id(node))

    # Structured Music/Sound objects in System and elsewhere.
    for node in root.iter():
        tag = strip_namespace(node.tag)
        if tag not in {"Music", "Sound"}:
            continue
        name = direct_text(node, "name")
        if not name:
            continue
        category = tag
        entity = _nearest_ancestor(node, parent_map, ENTITY_TAGS)
        entity_id, entity_name = _entity_label_decoded(entity, entity_names)
        context = strip_namespace(parent_map[node].tag) if node in parent_map else "System"
        location = f"数据库 / {context}"
        if entity is not None:
            location += f" / {strip_namespace(entity.tag)} {entity_id}「{entity_name}」"
        _append_ref(refs,
            category=category, asset_name=name,
            source_kind="Database", source_id="RPG_RT.ldb", source_name="数据库",
            location=location, command_name=f"{tag}.name", raw_source=str(xml_path))

    # Event commands in common events and battle event pages.
    for command in root.iter():
        if strip_namespace(command.tag) != "EventCommand":
            continue
        code = safe_int(direct_text(command, "code"))
        value = direct_text(command, "string")
        mapping = EVENT_ASSET_COMMANDS.get(code)
        if not mapping or not value:
            continue
        category, command_name = mapping
        entity = _nearest_ancestor(command, parent_map, {"CommonEvent", "TroopPage", "Troop"})
        entity_id, entity_name = _entity_label_decoded(entity, entity_names)
        entity_tag = strip_namespace(entity.tag) if entity is not None else "EventCommand"
        siblings = []
        parent = parent_map.get(command)
        if parent is not None:
            siblings = [x for x in parent if strip_namespace(x.tag) == "EventCommand"]
        command_index = siblings.index(command) + 1 if command in siblings else None
        location = f"数据库 / {entity_tag}"
        if entity_id:
            location += f" {entity_id}"
        if entity_name:
            location += f"「{entity_name}」"
        if command_index:
            location += f" / 命令 {command_index}"
        _append_ref(refs,
            category=category, asset_name=value,
            source_kind="Database", source_id="RPG_RT.ldb", source_name="数据库",
            location=location, command_index=command_index, command_code=code,
            command_name=command_name, raw_source=str(xml_path))

    for move_command in root.iter():
        if strip_namespace(move_command.tag) != "MoveCommand":
            continue
        code = safe_int(direct_text(move_command, "command_id"), -1)
        value = direct_text(move_command, "parameter_string")
        mapping = MOVE_ASSET_COMMANDS.get(code)
        if not mapping or not value:
            continue
        category, command_name = mapping
        entity = _nearest_ancestor(move_command, parent_map, {"CommonEvent", "TroopPage", "Troop"})
        entity_id, entity_name = _entity_label_decoded(entity, entity_names)
        entity_tag = strip_namespace(entity.tag) if entity is not None else "MoveCommand"
        _append_ref(refs,
            category=category, asset_name=value,
            source_kind="Database", source_id="RPG_RT.ldb", source_name="数据库",
            location=f"数据库 / {entity_tag} {entity_id}「{entity_name}」/ 移动路线",
            command_code=code, command_name=command_name, raw_source=str(xml_path))
    return refs


def _is_plausible_text(value: str) -> bool:
    if not value or len(value) > 120:
        return False
    if any(ord(ch) < 32 and ch not in "\t\r\n" for ch in value):
        return False
    printable = sum(ch.isprintable() for ch in value)
    return printable / max(1, len(value)) > 0.92


def _decode_ber(data: bytes, offset: int, max_bytes: int = 5) -> tuple[Optional[int], int]:
    value = 0
    pos = offset
    for _ in range(max_bytes):
        if pos >= len(data):
            return None, offset
        byte = data[pos]
        pos += 1
        value = (value << 7) | (byte & 0x7F)
        if not (byte & 0x80):
            return value, pos
    return None, offset



def _python_codec_name(value: str) -> Optional[str]:
    value = (value or "").strip()
    if not value or value.casefold() == "auto":
        return None
    aliases = {"65001": "utf-8", "936": "gbk", "950": "cp950", "932": "cp932"}
    if value in aliases:
        return aliases[value]
    if value.isdigit():
        return "cp" + value
    return value


def _encoding_candidates(*values: str) -> tuple[str, ...]:
    ordered: list[str] = []
    for value in values:
        codec = _python_codec_name(value)
        if codec and codec not in ordered:
            ordered.append(codec)
    for codec in ("cp932", "gbk", "utf-8", "cp1252"):
        if codec not in ordered:
            ordered.append(codec)
    return tuple(ordered)

def extract_lcf_strings(path: Path, encodings: Sequence[str] = ("cp932", "utf-8", "cp1252")) -> list[tuple[int, str]]:
    """Best-effort extraction of BER-length-prefixed strings for fallback mode.

    This is intentionally not presented as an exact parser. It is useful for finding
    plain filenames when liblcf/lcf2xml is unavailable.
    """
    data = path.read_bytes()
    out: dict[tuple[int, str], None] = {}
    for offset in range(len(data)):
        length, start = _decode_ber(data, offset)
        if length is None or length < 2 or length > 120 or start + length > len(data):
            continue
        raw = data[start:start + length]
        if b"\x00" in raw:
            continue
        for encoding in encodings:
            try:
                text = raw.decode(encoding)
            except UnicodeDecodeError:
                continue
            text = text.strip()
            if _is_plausible_text(text):
                out[(start, text)] = None
                break
    return sorted(out.keys())


def extract_event_command_references(
    path: Path,
    encodings: Sequence[str] = ("cp932", "gbk", "utf-8", "cp1252"),
) -> list[tuple[int, int, str, str]]:
    """Find asset-bearing EventCommand records without parsing the surrounding LCF tree.

    Returns (offset, code, category, string). Category and filename are usually
    reliable because the complete EventCommand record is validated; event/page
    location is unavailable, so callers must keep confidence=approximate.
    """
    data = path.read_bytes()
    results: list[tuple[int, int, str, str]] = []
    for offset in range(len(data)):
        code, pos = _decode_ber(data, offset)
        if code not in EVENT_ASSET_COMMANDS:
            continue
        indent, pos = _decode_ber(data, pos)
        if indent is None or indent > 100:
            continue
        length, pos = _decode_ber(data, pos)
        if length is None or length < 0 or length > 240 or pos + length > len(data):
            continue
        raw = data[pos:pos + length]
        pos += length
        decoded = None
        for encoding in encodings:
            try:
                candidate = raw.decode(encoding).strip()
            except UnicodeDecodeError:
                continue
            if not candidate or _is_plausible_text(candidate):
                decoded = candidate
                break
        if decoded is None:
            continue
        param_count, pos = _decode_ber(data, pos)
        if param_count is None or param_count > 100:
            continue
        valid = True
        for _ in range(param_count):
            _parameter, pos = _decode_ber(data, pos)
            if _parameter is None:
                valid = False
                break
        if not valid:
            continue
        category, _command_name = EVENT_ASSET_COMMANDS[code]
        results.append((offset, code, category, decoded))
    return results


def _asset_name_lookup(assets: Sequence[AssetFile]) -> dict[str, set[str]]:
    lookup: dict[str, set[str]] = {}
    for asset in assets:
        lookup.setdefault(asset.normalized, set()).add(asset.category)
    return lookup


def fallback_scan(
    project_root: Path,
    selected_map_ids: Optional[set[int]] = None,
    include_database: bool = True,
    external_roots: Sequence[Path] = (),
    map_encoding: str = "932",
    event_encoding: str = "932",
    filename_encoding: str = "932",
    progress: Optional[Callable[[str], None]] = None,
) -> AnalysisResult:
    project_assets = discover_assets(project_root)
    external_assets: list[AssetFile] = []
    for root in external_roots:
        external_assets.extend(discover_assets(root, source="external"))
    lookup = _asset_name_lookup(project_assets + external_assets)
    refs: list[AssetReference] = []
    warnings = [
        "当前使用快速字符串扫描模式：可发现大量素材名，但不能保证类别与事件位置完全准确。",
        "安装 EasyRPG lcf2xml 后重新检查，可获得地图—事件—页—命令级精确定位。",
    ]
    if selected_map_ids is not None or not include_database:
        warnings.append("当前只检查了部分地图或未包含数据库；“未使用素材”仅表示未在本次检查范围内发现引用。")
    map_files = sorted(project_root.glob("Map[0-9][0-9][0-9][0-9].lmu"))
    maps: list[MapInfo] = []
    for path in map_files:
        map_id = safe_int(path.stem[3:])
        maps.append(MapInfo(map_id, path.name, ""))
    sources: list[tuple[Path, str, str]] = []
    if include_database and (project_root / "RPG_RT.ldb").exists():
        sources.append((project_root / "RPG_RT.ldb", "Database", "RPG_RT.ldb"))
    for path in map_files:
        map_id = safe_int(path.stem[3:])
        if selected_map_ids is not None and map_id not in selected_map_ids:
            continue
        sources.append((path, "Map", path.stem))
    if (project_root / "RPG_RT.lmt").exists() and selected_map_ids is None:
        sources.append((project_root / "RPG_RT.lmt", "MapTree", "RPG_RT.lmt"))
    elif selected_map_ids is not None:
        warnings.append("快速模式无法可靠地把 RPG_RT.lmt 中的继承属性归属到所选地图，因此本次部分地图检查未扫描 LMT。")

    for path, kind, source_id in sources:
        if progress:
            progress(f"快速扫描 {path.name}")
        try:
            if kind == "MapTree":
                candidates = _encoding_candidates(filename_encoding, map_encoding)
            else:
                candidates = _encoding_candidates(filename_encoding, event_encoding)
            strings = extract_lcf_strings(path, candidates)
            command_refs = extract_event_command_references(path, _encoding_candidates(filename_encoding))
        except OSError as exc:
            warnings.append(f"无法读取 {path.name}: {exc}")
            continue
        seen: set[tuple[str, str]] = set()
        for command_index, (offset, code, category, value) in enumerate(command_refs, 1):
            normalized = normalize_asset_name(value)
            key = (category, normalized)
            # Keep repeated references at different locations, but avoid a second
            # generic string-hit for the same name below.
            seen.add(key)
            command_name = EVENT_ASSET_COMMANDS[code][1]
            refs.append(AssetReference(
                category=category,
                asset_name=value,
                source_kind=kind,
                source_id=source_id,
                source_name="",
                location=f"事件命令记录 {command_index} / 二进制偏移 0x{offset:X}",
                command_index=command_index,
                command_code=code,
                command_name=command_name,
                confidence="dynamic" if is_dynamic_reference(value) else "approximate",
                raw_source=str(path),
            ))
        for offset, value in strings:
            normalized = normalize_asset_name(value)
            categories = lookup.get(normalized)
            if not categories:
                continue
            for category in categories:
                key = (category, normalized)
                if key in seen:
                    continue
                seen.add(key)
                refs.append(AssetReference(
                    category=category,
                    asset_name=value,
                    source_kind=kind,
                    source_id=source_id,
                    source_name="",
                    location=f"字符串记录 / 二进制偏移 0x{offset:X}",
                    confidence="approximate",
                    raw_source=str(path),
                ))
    return AnalysisResult(
        project_root=str(project_root), mode="fallback", complete_scan=False,
        encoding_settings={
            "map": map_encoding, "event_database": event_encoding, "filename": filename_encoding,
        },
        maps=maps, references=refs,
        project_assets=project_assets, external_assets=external_assets,
        warnings=warnings,
    )


def exact_scan(
    project_root: Path,
    lcf2xml: Path,
    selected_map_ids: Optional[set[int]] = None,
    include_database: bool = True,
    external_roots: Sequence[Path] = (),
    engine: Optional[str] = None,
    encoding: Optional[str] = None,
    map_encoding: str = "932",
    event_encoding: str = "932",
    filename_encoding: str = "932",
    cache_dir: Optional[Path] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> AnalysisResult:
    if encoding is not None:
        # Backward-compatible single-codepage API.
        map_encoding = event_encoding = filename_encoding = encoding
    engine = engine or detect_engine(project_root)
    project_assets = discover_assets(project_root)
    external_assets: list[AssetFile] = []
    for root in external_roots:
        external_assets.extend(discover_assets(root, source="external"))

    lmt = project_root / "RPG_RT.lmt"
    ldb = project_root / "RPG_RT.ldb"
    map_files = sorted(project_root.glob("Map[0-9][0-9][0-9][0-9].lmu"))
    if not lmt.exists() or not ldb.exists():
        raise AnalysisError("所选文件夹不是完整的 RPG Maker 2000/2003 工程：缺少 RPG_RT.lmt 或 RPG_RT.ldb。")
    if cache_dir is None:
        cache_dir = project_root / ".rm_asset_auditor_cache"

    selected_map_files: list[Path] = []
    for path in map_files:
        map_id = safe_int(path.stem[3:])
        if selected_map_ids is None or map_id in selected_map_ids:
            selected_map_files.append(path)

    filename_cache = cache_dir / ("filename_" + _safe_cache_component(filename_encoding))
    map_cache = cache_dir / ("map_" + _safe_cache_component(map_encoding))
    event_cache = cache_dir / ("event_" + _safe_cache_component(event_encoding))
    warnings: list[str] = []

    # Filenames determine resource matching and therefore must be converted
    # successfully for LMT/LDB. Individual malformed LMU files are reported and
    # skipped so one bad map no longer aborts the entire project inspection.
    asset_base = convert_lcf_files(
        lcf2xml, [lmt, ldb], filename_cache, engine, filename_encoding, progress,
        stage_label="素材编码·基础数据",
    )
    asset_map_errors: list[str] = []
    asset_maps = convert_lcf_files(
        lcf2xml, selected_map_files, filename_cache, engine, filename_encoding, progress,
        skip_errors=True, errors=asset_map_errors, stage_label="素材编码·地图",
    )
    if asset_map_errors:
        warnings.append(
            "以下地图无法按素材文件名编码转换，已跳过；本次未使用素材结果不完整，清理功能已锁定：\n"
            + "\n\n".join(asset_map_errors)
        )

    # Map display names can use a different codepage from map-tree BGM/backdrop
    # filenames. A failure here only degrades labels and does not lose references.
    label_lmt = asset_base[lmt]
    if (map_encoding or "auto").casefold() != (filename_encoding or "auto").casefold():
        label_errors: list[str] = []
        converted = convert_lcf_files(
            lcf2xml, [lmt], map_cache, engine, map_encoding, progress,
            skip_errors=True, errors=label_errors, stage_label="地图名编码·地图树",
        )
        if lmt in converted:
            label_lmt = converted[lmt]
        elif label_errors:
            warnings.append(
                "地图名编码转换失败，地图名称暂按素材文件名编码显示：\n"
                + "\n".join(label_errors)
            )

    # Event/database labels can likewise be decoded independently. The asset XML
    # remains authoritative for filenames; label XML is used only for names shown
    # in locations such as event and database object names.
    label_ldb = asset_base[ldb]
    label_maps = asset_maps
    if (event_encoding or "auto").casefold() != (filename_encoding or "auto").casefold():
        label_base_errors: list[str] = []
        converted = convert_lcf_files(
            lcf2xml, [ldb], event_cache, engine, event_encoding, progress,
            skip_errors=True, errors=label_base_errors, stage_label="事件编码·数据库",
        )
        if ldb in converted:
            label_ldb = converted[ldb]
        elif label_base_errors:
            warnings.append(
                "数据库文字编码转换失败，数据库对象名称暂按素材文件名编码显示：\n"
                + "\n".join(label_base_errors)
            )
        label_map_errors: list[str] = []
        label_maps = convert_lcf_files(
            lcf2xml, selected_map_files, event_cache, engine, event_encoding, progress,
            skip_errors=True, errors=label_map_errors, stage_label="事件编码·地图",
        )
        if label_map_errors:
            warnings.append(
                "部分地图的事件文字编码转换失败；这些地图仍会检查素材，但事件名称可能显示为素材编码结果：\n"
                + "\n\n".join(label_map_errors)
            )

    maps, lmt_refs = parse_map_tree(asset_base[lmt], label_lmt)
    info_by_id = {m.map_id: m for m in maps}
    refs: list[AssetReference] = []
    selected_set = selected_map_ids if selected_map_ids is not None else {m.map_id for m in maps}
    refs.extend([
        ref for ref in lmt_refs
        if safe_int(ref.source_id[3:]) in selected_set
    ])

    catalogs = parse_database_asset_catalog(asset_base[ldb])
    if include_database:
        if progress:
            progress("解析数据库")
        refs.extend(parse_database_xml(asset_base[ldb], label_ldb))

    for path in selected_map_files:
        asset_xml = asset_maps.get(path)
        if asset_xml is None:
            continue
        map_id = safe_int(path.stem[3:])
        info = info_by_id.get(map_id, MapInfo(map_id, path.name, ""))
        if progress:
            progress(f"解析 {info.display_name}")
        label_xml = label_maps.get(path, asset_xml)
        refs.extend(parse_map_xml(
            asset_xml, info, catalogs.get("ChipSet", {}), label_xml_path=label_xml
        ))

    if selected_map_ids is not None or not include_database:
        warnings.append("当前只检查了部分地图或未包含数据库；“未使用素材”仅表示未在本次检查范围内发现引用。")
    existing_ids = {safe_int(p.stem[3:]) for p in map_files}
    tree_ids = {m.map_id for m in maps if m.map_type == 1}
    missing_map_files = sorted(tree_ids - existing_ids)
    if missing_map_files:
        warnings.append("地图树中存在但工程目录缺少的地图文件：" + ", ".join(f"Map{x:04d}.lmu" for x in missing_map_files))

    return AnalysisResult(
        project_root=str(project_root), mode="exact",
        complete_scan=not asset_map_errors and not missing_map_files,
        encoding_settings={
            "map": map_encoding,
            "event_database": event_encoding,
            "filename": filename_encoding,
        },
        maps=maps, references=refs,
        project_assets=project_assets, external_assets=external_assets,
        warnings=warnings,
    )


def analyze_project(
    project_root: Path,
    selected_map_ids: Optional[set[int]] = None,
    include_database: bool = True,
    external_roots: Sequence[Path] = (),
    lcf2xml: Optional[Path] = None,
    app_dir: Optional[Path] = None,
    engine: Optional[str] = None,
    encoding: Optional[str] = None,
    map_encoding: str = "932",
    event_encoding: str = "932",
    filename_encoding: str = "932",
    force_fallback: bool = False,
    progress: Optional[Callable[[str], None]] = None,
) -> AnalysisResult:
    project_root = project_root.resolve()
    if not project_root.is_dir():
        raise AnalysisError("工程文件夹不存在。")
    if encoding is not None:
        map_encoding = event_encoding = filename_encoding = encoding
    parser = None if force_fallback else find_lcf2xml(app_dir, lcf2xml)
    if parser:
        return exact_scan(
            project_root=project_root, lcf2xml=parser,
            selected_map_ids=selected_map_ids, include_database=include_database,
            external_roots=external_roots, engine=engine,
            map_encoding=map_encoding, event_encoding=event_encoding,
            filename_encoding=filename_encoding, progress=progress,
        )
    return fallback_scan(
        project_root=project_root, selected_map_ids=selected_map_ids,
        include_database=include_database, external_roots=external_roots,
        map_encoding=map_encoding, event_encoding=event_encoding,
        filename_encoding=filename_encoding, progress=progress,
    )


def move_assets_to_backup(
    assets: Sequence[AssetFile], project_root: Path,
    backup_root: Optional[Path] = None,
) -> tuple[Path, list[tuple[Path, Path]]]:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_root = backup_root or (project_root / f"_UnusedAssets_Backup_{timestamp}")
    moved: list[tuple[Path, Path]] = []
    for asset in assets:
        src = Path(asset.path)
        if not src.exists():
            continue
        try:
            rel = src.relative_to(project_root)
        except ValueError as exc:
            raise AnalysisError(f"拒绝移动工程目录之外的文件：{src}") from exc
        dst = backup_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            dst = dst.with_name(f"{dst.stem}_{timestamp}{dst.suffix}")
        shutil.move(str(src), str(dst))
        moved.append((src, dst))
    manifest = backup_root / "move_manifest.csv"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with manifest.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(["原路径", "备份路径", "操作时间"])
        for src, dst in moved:
            writer.writerow([str(src), str(dst), datetime.now().isoformat(timespec="seconds")])
    return backup_root, moved


def permanently_delete_assets(assets: Sequence[AssetFile], project_root: Path) -> list[Path]:
    deleted: list[Path] = []
    project_root = project_root.resolve()
    for asset in assets:
        path = Path(asset.path).resolve()
        try:
            path.relative_to(project_root)
        except ValueError as exc:
            raise AnalysisError(f"拒绝删除工程目录之外的文件：{path}") from exc
        if path.is_file():
            path.unlink()
            deleted.append(path)
    return deleted


def export_csv(rows: Iterable[dict], path: Path) -> None:
    rows = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def export_full_report(
    result: AnalysisResult,
    output_dir: Path,
    categories: Optional[set[str]] = None,
) -> list[Path]:
    """Export JSON and Excel-friendly UTF-8-SIG CSV reports.

    When categories is supplied, every exported view is limited to those resource
    types, matching the categories currently selected in the GUI.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    generated = []
    json_path = output_dir / "完整检查报告.json"
    json_path.write_text(
        json.dumps(result.to_dict(categories), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    generated.append(json_path)
    usage_path = output_dir / "素材调用汇总.csv"
    export_csv(result.usage_summary(categories), usage_path)
    generated.append(usage_path)
    detail_path = output_dir / "素材调用明细.csv"
    export_csv(result.reference_rows(categories), detail_path)
    generated.append(detail_path)
    missing_path = output_dir / "缺失素材.csv"
    export_csv(result.missing_references(categories), missing_path)
    generated.append(missing_path)
    unused_path = output_dir / "未使用素材.csv"
    export_csv([asdict(x) for x in result.unused_assets(categories)], unused_path)
    generated.append(unused_path)
    return generated
