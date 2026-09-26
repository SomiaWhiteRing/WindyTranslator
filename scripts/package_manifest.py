"""Generate and verify the immutable distribution file list after packaging."""
import json
import argparse
from pathlib import Path
import sys
import tempfile
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.update_install import HELPER, HELPER_PATH, MANIFEST, digest, extract_package, load_manifest, managed_name, verify_files, write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--verify-zip", action="store_true")
    args = parser.parse_args()
    root = args.path.resolve()
    if args.verify_zip:
        if not 0 < root.stat().st_size <= 95_000_000:
            raise ValueError("Distribution must not exceed 95,000,000 bytes")
        with zipfile.ZipFile(root) as archive:
            entry = archive.getinfo("WindyTranslator/_internal/build-info.json")
            if entry.file_size > 16384:
                raise ValueError("Build metadata is too large")
            build_id = json.loads(archive.read(entry))["applicationBuildId"]
        with tempfile.TemporaryDirectory(prefix="windy-package-verify-") as temporary:
            staged = Path(temporary) / "staged"
            staged.mkdir()
            extract_package(root, staged, build_id, lambda *_: None)
        print(f"Verified distribution ZIP: {root.stat().st_size} bytes")
        return
    if not (root / HELPER_PATH).is_file() or (root / HELPER).exists():
        raise ValueError(f"Updater must be packaged only at {HELPER_PATH}")
    build = json.loads((root / "_internal/build-info.json").read_text(encoding="utf-8"))
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Package contains a link: {path}")
        if path.is_file() and path.relative_to(root).as_posix() != MANIFEST:
            name = managed_name(path.relative_to(root).as_posix())
            files[name] = {"size": path.stat().st_size, "sha256": digest(path)}
    write_json(root / MANIFEST, {"schemaVersion": 1, "applicationBuildId": build["applicationBuildId"], "files": files})
    verify_files(root, load_manifest(root))
    print(f"Verified {len(files)} distribution files")


if __name__ == "__main__":
    main()
