"""Build the offline RTP collection from the six source ZIPs."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import zipfile


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.external.rtp import _read_raw_zip_filenames
from core.external.rtp_collection import COLLECTION_FILENAME, PACK_IDS


def _write_member(archive: zipfile.ZipFile, name: str, data: bytes):
    info = zipfile.ZipInfo(name, date_time=(2000, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(info, data, compresslevel=9)


def pack_rtp_collection(source_dir: Path) -> Path:
    source_dir = Path(source_dir)
    output = source_dir / COLLECTION_FILENAME
    manifest = {"schema": 1, "packs": {}}
    seen = set()
    source_bytes = entry_count = 0
    temporary = tempfile.NamedTemporaryFile(dir=source_dir, suffix=".tmp", delete=False)
    temporary.close()
    try:
        with zipfile.ZipFile(temporary.name, "w") as destination:
            for pack_id in PACK_IDS:
                source = source_dir / f"{pack_id}.zip"
                raw_names = _read_raw_zip_filenames(source)
                source_bytes += source.stat().st_size
                entries = []
                with zipfile.ZipFile(source) as archive:
                    infos = archive.infolist()
                    if len(infos) != len(raw_names):
                        raise ValueError(f"RTP 文件清单不一致: {source.name}")
                    for info, raw_name in zip(infos, raw_names):
                        entry = {"name": base64.b64encode(raw_name).decode("ascii"), "directory": info.is_dir()}
                        if not info.is_dir():
                            data = archive.read(info)
                            digest = hashlib.sha256(data).hexdigest()
                            entry.update(sha256=digest, size=len(data))
                            if digest not in seen:
                                _write_member(destination, f"blobs/{digest}", data)
                                seen.add(digest)
                            entry_count += 1
                        entries.append(entry)
                manifest["packs"][pack_id] = entries
            _write_member(destination, "manifest.json", json.dumps(manifest, separators=(",", ":")).encode("utf-8"))
        os.replace(temporary.name, output)
    finally:
        Path(temporary.name).unlink(missing_ok=True)
    print(f"RTP: {entry_count} files, {len(seen)} unique contents; {source_bytes:,} -> {output.stat().st_size:,} bytes")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=REPO_ROOT / "modules" / "RTPCollection")
    args = parser.parse_args()
    pack_rtp_collection(args.source)
