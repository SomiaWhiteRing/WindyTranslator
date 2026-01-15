"""Find original text entries that are prefixes of longer entries.

This helps spot cases where one in-game sentence is split across multiple
messages (often for portrait changes), which can look odd after blindly
adding closing quotes to every segment.
"""

import argparse
import json
from pathlib import Path
from typing import Dict, Iterable, List, Tuple


def load_translation(path: Path) -> Dict:
    """Load the translation JSON file."""
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def normalize_key(value: str) -> str:
    """Normalize newlines so prefix checks are consistent."""
    return value.replace("\r\n", "\n")


def find_prefix_pairs(
    strings: Iterable[str],
    min_prefix_len: int,
    min_diff_len: int,
    adjacent_only: bool = True,
) -> List[Tuple[str, str]]:
    """
    Return (prefix, extended) pairs where the first string starts the second.

    If adjacent_only is True, only consider immediately adjacent entries (in the
    original order of the JSON file) to avoid far-apart false positives.
    """
    matches: List[Tuple[str, str]] = []
    if adjacent_only:
        ordered = [(normalize_key(s), s) for s in strings]
        for idx in range(len(ordered) - 1):
            base_norm, base_raw = ordered[idx]
            if len(base_norm) < min_prefix_len:
                continue
            target_norm, target_raw = ordered[idx + 1]
            if target_norm.startswith(base_norm):
                diff = target_norm[len(base_norm) :]
                if len(diff.strip()) >= min_diff_len:
                    matches.append((base_raw, target_raw))
    else:
        items = sorted((normalize_key(s), s) for s in strings)
        total = len(items)
        for idx, (base_norm, base_raw) in enumerate(items):
            if len(base_norm) < min_prefix_len:
                continue
            next_idx = idx + 1
            while next_idx < total:
                target_norm, target_raw = items[next_idx]
                if not target_norm.startswith(base_norm):
                    break
                diff = target_norm[len(base_norm) :]
                if len(diff.strip()) >= min_diff_len:
                    matches.append((base_raw, target_raw))
                next_idx += 1
    return matches


def format_single_line(text: str, limit: int) -> str:
    """Render text on one line with escaped newlines and optional truncation."""
    rendered = normalize_key(text).replace("\n", "\\n")
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 3] + "..."


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Detect original strings that are prefixes of longer strings, "
            "so you can catch split sentences that gained duplicate closing quotes."
        )
    )
    parser.add_argument(
        "translation_path",
        type=Path,
        help="Path to translation_translated.json",
    )
    parser.add_argument(
        "--min-prefix-len",
        type=int,
        default=10,
        help="Minimum length (in characters) of the prefix to consider.",
    )
    parser.add_argument(
        "--min-diff-len",
        type=int,
        default=1,
        help="Minimum length of the trailing part after the prefix.",
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        default=140,
        help="Maximum characters to print per line in text output.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON with all matches instead of human-readable text.",
    )
    parser.add_argument(
        "--with-translation",
        action="store_true",
        help="Include translations in JSON output.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Write the output to a file (by default only prints to console).",
    )
    parser.add_argument(
        "--non-adjacent",
        action="store_true",
        help="Include non-adjacent prefix matches (falls back to full sorted search).",
    )
    args = parser.parse_args()

    data = load_translation(args.translation_path)
    if not isinstance(data, dict):
        parser.error("Unexpected JSON format: root should be an object.")

    results: Dict[str, List[Tuple[str, str]]] = {}
    for map_name, entries in data.items():
        if not isinstance(entries, dict):
            continue
        pairs = find_prefix_pairs(
            entries.keys(),
            args.min_prefix_len,
            args.min_diff_len,
            adjacent_only=not args.non_adjacent,
        )
        if pairs:
            results[map_name] = pairs

    output_text: str
    if args.json:
        payload = {}
        for map_name, pairs in results.items():
            payload[map_name] = []
            for prefix, extended in pairs:
                item = {"prefix": prefix, "extended": extended}
                if args.with_translation:
                    map_entries = data.get(map_name, {})
                    item["prefix_translation"] = (
                        map_entries.get(prefix, {}) or {}
                    ).get("text")
                    item["extended_translation"] = (
                        map_entries.get(extended, {}) or {}
                    ).get("text")
                payload[map_name].append(item)
        output_text = json.dumps(payload, ensure_ascii=False, indent=2)
    else:
        lines: List[str] = []
        if not results:
            lines.append("No candidates found.")
        else:
            for map_name, pairs in results.items():
                lines.append(f"{map_name}: {len(pairs)} candidate(s)")
                for idx, (prefix, extended) in enumerate(pairs, start=1):
                    lines.append(
                        f"  [{idx}] prefix   : {format_single_line(prefix, args.max_chars)}"
                    )
                    lines.append(
                        f"      extended: {format_single_line(extended, args.max_chars)}"
                    )
                lines.append("")
        output_text = "\n".join(lines)

    print(output_text)
    if args.output:
        args.output.write_text(output_text, encoding="utf-8")
        print(f"\nWrote output to: {args.output}")

if __name__ == "__main__":
    main()
