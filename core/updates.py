"""Website-only update discovery and verified package downloads (no installation)."""

from dataclasses import dataclass
import hashlib
from http.client import HTTPException
import json
import logging
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile


DEFAULT_SITE_URL = "https://viprpg-zh-archive.q578235562.workers.dev"
TOOL = "windy-translator"
CHANNEL = "stable"
TARGET = "windows-x64"
MAX_PACKAGE_BYTES = 100_000_000
MAX_MANIFEST_BYTES = 256 * 1024
log = logging.getLogger(__name__)


class UpdateError(Exception):
    """An update cannot be checked or downloaded safely."""


class UpdateCancelled(UpdateError):
    pass


def load_build_info():
    if getattr(sys, "frozen", False):
        try:
            data = json.loads((Path(sys._MEIPASS) / "build-info.json").read_text(encoding="utf-8"))
            if data.get("schemaVersion") != 1 or data.get("target") != TARGET:
                raise ValueError("Unsupported build metadata")
            if not isinstance(data.get("applicationBuildId"), str) or not data["applicationBuildId"]:
                raise ValueError("Missing build identity")
            return data
        except (OSError, ValueError, TypeError, AttributeError):
            return {"version": "未知构建", "applicationBuildId": "", "target": TARGET}
    try:
        version = (Path(__file__).resolve().parents[1] / "RELEASE.md").read_text(encoding="utf-8-sig").splitlines()[0].removeprefix("# ")
    except (OSError, IndexError):
        version = "未知版本"
    # A source checkout is not an installed website release, even at the same commit.
    return {"version": f"{version}（源码）", "applicationBuildId": "", "target": TARGET}


def _https_url(value):
    if not isinstance(value, str) or not value or len(value) > 4096 or re.search(r"[\s\\\x00-\x1f\x7f]", value):
        raise UpdateError("网站地址必须是有效的 HTTPS 地址。")
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.fragment):
            raise ValueError()
        port = parsed.port or 443
    except ValueError as error:
        raise UpdateError("网站地址必须使用 HTTPS，且不能包含账号、密码或片段。") from error
    return parsed, (parsed.hostname.lower(), port)


def normalize_site_url(value):
    value = value.strip() if isinstance(value, str) else ""
    if not value:
        raise UpdateError("尚未配置更新网站，请先填写网站地址。")
    parsed, _ = _https_url(value)
    if parsed.path not in ("", "/") or parsed.query:
        raise UpdateError("请填写网站首页地址，不要填写下载页或接口路径。")
    return f"https://{parsed.netloc.lower()}"


def _site_link(value, site_url):
    _, origin = _https_url(value)
    _, expected = _https_url(site_url)
    if origin != expected:
        raise UpdateError("更新信息包含其他网站的链接，已停止处理。")
    return value


class _SiteRedirectHandler(HTTPRedirectHandler):
    def __init__(self, site_url):
        super().__init__()
        self.site_url = site_url

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _site_link(newurl, self.site_url)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(site_url, url, accept, timeout):
    _site_link(url, site_url)
    request = Request(url, headers={
        "User-Agent": "WindyTranslator-Updater/1",
        "Accept": accept,
        "Accept-Encoding": "identity",
        "Cache-Control": "no-cache",
    })
    try:
        response = build_opener(_SiteRedirectHandler(site_url)).open(request, timeout=timeout)
        if response.status != 200:
            response.close()
            raise UpdateError("网站返回了不完整的响应，请重新尝试。")
        return response
    except HTTPError as error:
        code = error.code
        error.close()
        if code == 404:
            raise UpdateError("网站尚未提供此更新接口或下载文件（HTTP 404）。") from error
        raise UpdateError(f"更新网站返回 HTTP {code}，请稍后重试。") from error
    except (URLError, OSError, TimeoutError) as error:
        raise UpdateError("无法连接更新网站，请检查网络和网站地址后重试。") from error


def _text(data, key, limit=200):
    value = data.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise UpdateError(f"更新信息中的 {key} 无效。")
    return value


def _number(data, key, minimum=1):
    value = data.get(key)
    if type(value) is not int or value < minimum or value > 2**53 - 1:
        raise UpdateError(f"更新信息中的 {key} 无效。")
    return value


@dataclass(frozen=True)
class Artifact:
    id: str
    url: str
    filename: str
    size: int
    sha256: str


@dataclass(frozen=True)
class UpdateInfo:
    site_url: str
    status: str
    comparison: str = "unknown"
    release_id: str = ""
    sequence: int = 0
    version: str = ""
    installed_version: str = ""
    notes: str = ""
    notes_url: str = ""
    published_at: str = ""
    artifact: Artifact = None


def parse_manifest(data, site_url, build_id=""):
    if not isinstance(data, dict) or type(data.get("schemaVersion")) is not int or data["schemaVersion"] != 1:
        raise UpdateError("网站使用了尚不支持的更新协议。")
    if any(data.get(key) != value for key, value in (("tool", TOOL), ("channel", CHANNEL), ("target", TARGET))):
        raise UpdateError("网站返回的工具、平台或更新渠道不匹配。")
    _number(data, "selectionRevision", 0)
    if data.get("status") == "paused":
        return UpdateInfo(site_url, "paused")
    if data.get("status") != "available":
        raise UpdateError("网站返回了无法识别的更新状态。")
    release_id = _text(data, "releaseId")
    sequence = _number(data, "releaseSequence")
    version = _text(data, "version")
    notes_url = _site_link(_text(data, "notesUrl", 4096), site_url)
    published_at = _text(data, "publishedAt", 80)
    notes = data.get("notes", "")
    if not isinstance(notes, str) or len(notes) > 60_000:
        raise UpdateError("版本说明无效或过长。")
    raw = data.get("artifact")
    if not isinstance(raw, dict) or raw.get("format") != "zip":
        raise UpdateError("当前客户端只支持下载 Windows ZIP 更新包。")
    filename = _text(raw, "filename", 180)
    if (re.search(r'[<>:"/\\|?*\x00-\x1f]', filename) or filename.endswith((" ", "."))
            or not filename.lower().endswith(".zip") or filename.startswith(".")
            or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", filename.split(".")[0])):
        raise UpdateError("更新包文件名无效。")
    size = _number(raw, "sizeBytes")
    if size >= MAX_PACKAGE_BYTES:
        raise UpdateError("此更新包超过客户端支持的 100 MB 上限。")
    sha256 = _text(raw, "sha256", 64).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise UpdateError("更新包缺少有效的 SHA-256 校验信息。")
    artifact = Artifact(_text(raw, "id"), _site_link(_text(raw, "url", 4096), site_url), filename, size, sha256)
    comparison, installed_version = "unknown", ""
    installed = data.get("installedRelease")
    if installed is not None:
        if not isinstance(installed, dict) or not build_id or installed.get("applicationBuildId") != build_id:
            raise UpdateError("网站返回的已安装版本与当前程序不匹配。")
        installed_id = _text(installed, "releaseId")
        installed_sequence = _number(installed, "releaseSequence")
        installed_version = _text(installed, "version")
        if (sequence == installed_sequence) != (release_id == installed_id):
            raise UpdateError("网站版本身份与发布序号不一致。")
        comparison = "newer" if sequence > installed_sequence else "older" if sequence < installed_sequence else "current"
    return UpdateInfo(site_url, "available", comparison, release_id, sequence, version,
                      installed_version, notes, notes_url, published_at, artifact)


def _cancelled(cancel):
    if cancel is not None and cancel.is_set():
        raise UpdateCancelled("操作已取消。")


def check_update(site_url, build_id="", cancel=None):
    site_url = normalize_site_url(site_url)
    url = f"{site_url}/api/tools/{TOOL}/updates/{CHANNEL}/{TARGET}"
    if build_id:
        url += "?" + urlencode({"applicationBuildId": build_id})
    _cancelled(cancel)
    try:
        with _open(site_url, url, "application/json", 15) as response:
            if response.headers.get_content_type() != "application/json":
                raise UpdateError("更新网站返回了网页或其他内容，未能获取更新信息。")
            payload = response.read(MAX_MANIFEST_BYTES + 1)
        _cancelled(cancel)
        if len(payload) > MAX_MANIFEST_BYTES:
            raise UpdateError("网站返回的更新信息过大。")
        data = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as error:
        raise UpdateError("网站返回的更新信息不是有效 JSON。") from error
    except (OSError, HTTPException) as error:
        raise UpdateError("读取更新信息失败，请稍后重试。") from error
    return parse_manifest(data, site_url, build_id)


def download_update(info, destination, cancel=None, progress=None, build_id=""):
    if info.status != "available" or info.artifact is None:
        raise UpdateError("请先检查并选择可用的更新版本。")
    # Recheck withdrawal and recommendation changes before opening a local file.
    fresh = check_update(info.site_url, build_id, cancel)
    if fresh.status != "available" or (fresh.release_id, fresh.sequence, fresh.artifact) != (info.release_id, info.sequence, info.artifact):
        raise UpdateError("网站推荐版本已变更或暂停，请重新检查更新。")
    destination = Path(destination).absolute()
    if destination.suffix.lower() != ".zip":
        raise UpdateError("请将更新包保存为 ZIP 文件。")
    artifact = info.artifact
    temporary = None
    try:
        with _open(info.site_url, artifact.url, "application/zip, application/octet-stream", 20) as response:
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isdecimal() or int(length) != artifact.size):
                raise UpdateError("网站返回的文件长度与更新信息不一致。")
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise UpdateError("网站改变了下载文件的传输编码，请稍后重试。")
            with tempfile.NamedTemporaryFile(mode="wb", dir=destination.parent,
                                             prefix=f".{destination.stem}-", suffix=".part", delete=False) as stream:
                temporary = Path(stream.name)
                checksum = hashlib.sha256()
                received = 0
                started = last_progress = time.monotonic()
                while True:
                    _cancelled(cancel)
                    if time.monotonic() - started > 1800:
                        raise UpdateError("下载超过 30 分钟，请检查网络后重试。")
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > artifact.size:
                        raise UpdateError("下载文件超出声明长度，已停止下载。")
                    stream.write(chunk)
                    checksum.update(chunk)
                    if progress and time.monotonic() - last_progress >= 0.1:
                        progress(received, artifact.size)
                        last_progress = time.monotonic()
            _cancelled(cancel)
            if received != artifact.size or checksum.hexdigest() != artifact.sha256:
                raise UpdateError("下载文件不完整或 SHA-256 校验失败，请重新下载。")
            if not zipfile.is_zipfile(temporary):
                raise UpdateError("下载文件不是有效的 ZIP 更新包。")
            _cancelled(cancel)
            os.replace(temporary, destination)
            if progress:
                progress(received, artifact.size)
        return destination
    except UpdateError:
        raise
    except (OSError, ValueError, HTTPException) as error:
        raise UpdateError("下载或保存失败，请检查网络、磁盘空间和目录权限后重试。") from error
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                log.warning("无法清理未完成的更新下载：%s", temporary, exc_info=True)
