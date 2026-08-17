from __future__ import annotations

import argparse
import json
import re
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


def _print_stats(label: str, entries: dict[tuple[str, str], str]) -> None:
    texts = tuple(entries.values())
    characters_with_spaces = sum(len(text) for text in texts)
    whitespace = sum(sum(char.isspace() for char in text) for text in texts)
    words = sum(len(re.findall(r"[A-Za-z0-9_]+|[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", text)) for text in texts)
    print(f"{label}文件数：{len({filename for filename, _ in entries})}")
    print(f"{label}条目数：{len(texts)}")
    print(f"{label}字数：{words}")
    print(f"{label}字符数（不计空格）：{characters_with_spaces - whitespace}")
    print(f"{label}字符数（计空格）：{characters_with_spaces}")
    print(f"{label}段落数：{sum(bool(text.strip()) for text in texts)}")
    print(f"{label}显式行数：{sum(len(text.splitlines()) for text in texts)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="统计当前游戏原文或译文 JSON 的文本规模")
    parser.add_argument("--mode", choices=("original", "translated"), required=True)
    parser.add_argument("--original-json")
    parser.add_argument("--translated-json")
    args = parser.parse_args()
    if args.mode == "original":
        if not args.original_json:
            print("缺少原文 JSON 文件参数：--original-json", file=sys.stderr)
            return 2
        _print_stats("原文", _original_entries(_load(Path(args.original_json), "原文")))
    else:
        if not args.translated_json:
            print("缺少译文 JSON 文件参数：--translated-json", file=sys.stderr)
            return 3
        _print_stats("译文", _translated_entries(_load(Path(args.translated_json), "译文")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
