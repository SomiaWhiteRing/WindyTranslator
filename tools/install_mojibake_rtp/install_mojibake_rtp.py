from __future__ import annotations

import argparse
import importlib
from pathlib import Path


def _load_rtp_module():
    try:
        return importlib.import_module("core.external.rtp")
    except ImportError as exc:
        raise RuntimeError(f"无法加载内置 RTP 安装器: {exc}") from exc


def install(project: Path, rtp_archive: str) -> bool:
    if not project.is_dir():
        raise ValueError(f"游戏目录不存在: {project}")

    rtp = _load_rtp_module()
    archive = Path(rtp_archive)
    if not archive.is_file():
        raise FileNotFoundError(f"缺少乱码 RTP 归档: {archive}")
    collection = archive.parent
    rtp.RTP_COLLECTION_DIR = str(collection)
    return rtp.install_rtp_files(str(project), [archive.name])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--rtp-archive", required=True)
    args = parser.parse_args()
    try:
        print("正在安装乱码版本的 RTP...", flush=True)
        if not install(Path(args.project), args.rtp_archive):
            print("乱码 RTP 安装失败。", flush=True)
            return 1
        print("乱码 RTP 安装完成：仅补充缺失文件，未覆盖已有文件。", flush=True)
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"错误：{exc}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
