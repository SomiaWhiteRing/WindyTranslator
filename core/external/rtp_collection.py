"""Content-addressed RTP resources, with each pack's original filename bytes."""
from __future__ import annotations

import base64
import hashlib
import json
import zipfile
from pathlib import Path


COLLECTION_FILENAME = "rtp-content.zip"
PACK_IDS = ("2000", "2000en", "2000fix", "2003", "2003steam", "2003zh_tw")


class RtpCollection:
    def __init__(self, path: str | Path):
        self.archive = zipfile.ZipFile(path)
        try:
            manifest = json.loads(self.archive.read("manifest.json"))
            if manifest.get("schema") != 1 or set(manifest.get("packs", {})) != set(PACK_IDS):
                raise ValueError("不支持或不完整的 RTP 资源库")
            self.packs = manifest["packs"]
        except Exception:
            self.archive.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.archive.close()

    def entries(self, pack_id: str):
        if pack_id not in PACK_IDS:
            raise ValueError(f"未知 RTP 类型: {pack_id}")
        for entry in self.packs[pack_id]:
            yield entry, base64.b64decode(entry["name"], validate=True)

    def read(self, entry: dict) -> bytes:
        digest = entry["sha256"]
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("无效的 RTP 内容标识")
        info = self.archive.getinfo(f"blobs/{digest}")
        if info.file_size != entry["size"]:
            raise ValueError("RTP 资源长度不匹配")
        data = self.archive.read(info)
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("RTP 资源校验失败")
        return data
