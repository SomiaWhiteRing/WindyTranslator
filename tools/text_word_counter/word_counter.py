from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _load(path: Path, label: str) -> dict:
    if not path.is_file():
        print(f"缺少{label} JSON 文件：{path}", file=sys.stderr)
        raise SystemExit(2 if label == "原文" else 3)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"{label} JSON 无法读取：{path}\n{exc}", file=sys.stderr)
        raise SystemExit(4)
    if not isinstance(payload, dict):
        print(f"{label} JSON 根节点不是对象：{path}", file=sys.stderr)
        raise SystemExit(5)
    return payload


def _original_entries(payload: dict) -> dict[tuple[str, str], str]:
    result = {}
    for filename, entries in payload.items():
        if not isinstance(entries, dict):
            continue
        for key, item in entries.items():
            if isinstance(item, dict) and isinstance(item.get("text_to_translate"), str):
                result[(str(filename), str(key))] = item["text_to_translate"]
    return result


def _translated_entries(payload: dict) -> dict[tuple[str, str], str]:
    result = {}
    for filename, entries in payload.items():
        if not isinstance(entries, dict):
            continue
        for key, item in entries.items():
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                result[(str(filename), str(key))] = item["text"]
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="统计当前游戏原文和译文 JSON 的字符数")
    parser.add_argument("--original-json", required=True)
    parser.add_argument("--translated-json", required=True)
    args = parser.parse_args()
    original = _original_entries(_load(Path(args.original_json), "原文"))
    translated = _translated_entries(_load(Path(args.translated_json), "译文"))
    original_keys = set(original)
    translated_keys = set(translated)
    print(f"原文条目数：{len(original)}")
    print(f"译文条目数：{len(translated)}")
    print(f"原文字数：{sum(len(value) for value in original.values())}")
    print(f"译文字数：{sum(len(value) for value in translated.values())}")
    print(f"原文缺失条目数：{len(original_keys - translated_keys)}")
    print(f"译文多出条目数：{len(translated_keys - original_keys)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
