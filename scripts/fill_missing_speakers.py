"""Find translations that dropped speaker names and help fill them back in.

The script scans a translation JSON (translation_translated.json), detects
entries where the original line starts with a speaker name but the translated
text does not, and lets you choose how to prepend the name. It can auto-suggest
translations from character/entity dictionaries and apply the chosen name to
all affected lines for that speaker.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

CONTROL_SEQ_RE = re.compile(r"^(\\[><\|\.!\^]|\\[A-Za-z]+\[\d+\]|\\[A-Za-z])")
TRAILING_PUNCT = "：:！!？?」』）)】］>＞、，。、…⋯‥／/　 "
QUOTE_PREFIXES = ("「", "『", "（", "(", '"', "“", "”", "'")
WRAP_PAIRS = {
    "【": "】",
    "〔": "〕",
    "［": "］",
    "〚": "〛",
    "〈": "〉",
    "《": "》",
    "<": ">",
    "＜": "＞",
    "(": ")",
    "（": "）",
    "[": "]",
    "｛": "｝",
    "{": "}",
    "「": "」",
    "『": "』",
}


@dataclass
class Candidate:
    map_name: str
    original: str
    translation: str
    speaker_prefix: str
    speaker_name: str  # raw first-line speaker
    core: str  # core name without wrappers/punctuation
    wrap_prefix: str
    wrap_suffix: str
    suffix_punct: str  # trailing punctuation (e.g., full-width colon)


def strip_leading_controls(text: str) -> Tuple[str, str]:
    """Split off RPG Maker control codes that appear at the start of a string."""
    prefix = ""
    remainder = text
    while True:
        match = CONTROL_SEQ_RE.match(remainder)
        if not match:
            break
        prefix += match.group(0)
        remainder = remainder[match.end() :]
    return prefix, remainder


def extract_speaker(text: str, max_len: int) -> Optional[Tuple[str, str, str]]:
    """
    Return (speaker_line, prefix, base_name) if the text looks like it starts with a speaker.

    The speaker is assumed to be the first line (before the first newline).
    """
    if "\n" not in text:
        return None
    speaker_line, _rest = text.split("\n", 1)
    speaker_line = speaker_line.strip()
    if not speaker_line or len(speaker_line) > max_len:
        return None
    prefix, base_name = strip_leading_controls(speaker_line)
    if not base_name:
        return None
    if base_name.startswith(QUOTE_PREFIXES):
        return None
    return speaker_line, prefix, base_name


def split_suffix(name: str) -> Tuple[str, str]:
    """Split off common trailing punctuation (e.g., colons after names)."""
    core = name.rstrip(TRAILING_PUNCT)
    suffix = name[len(core) :]
    return core, suffix


def decompose_name(name: str) -> Tuple[str, str, str, str]:
    """
    Break a speaker name into core, wrap prefixes/suffixes, and trailing punctuation.

    Example: "【ダークネスⅢ】：" -> core="ダークネスⅢ", wrap_prefix="【", wrap_suffix="】", suffix="："
    """
    working = name.strip()
    suffix = ""
    wrap_prefix = ""
    wrap_suffix = ""

    # Peel trailing punctuation unless it closes the leading wrapper.
    while working:
        opener = working[0]
        expected_closer = WRAP_PAIRS.get(opener)
        tail = working[-1]
        if tail in TRAILING_PUNCT and (not expected_closer or tail != expected_closer):
            suffix = tail + suffix
            working = working[:-1]
            continue
        break

    # Unwrap matching pairs from the outside in.
    changed = True
    while changed and len(working) >= 2:
        changed = False
        opener = working[0]
        expected_closer = WRAP_PAIRS.get(opener)
        if expected_closer and working.endswith(expected_closer):
            wrap_prefix += opener
            wrap_suffix = expected_closer + wrap_suffix
            working = working[1:-1].strip()
            changed = True

    core, extra_suffix = split_suffix(working)
    suffix = extra_suffix + suffix
    return core, wrap_prefix, wrap_suffix, suffix


def load_translation(path: Path) -> Dict:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("Unexpected JSON format: root should be an object.")
    return data


def load_name_lookup(paths: Iterable[Path]) -> Dict[str, str]:
    """Load name translations from one or more CSV files with 原文/译文 columns."""
    lookup: Dict[str, str] = {}
    rows: List[dict] = []
    def get_field(row: dict, key: str) -> str:
        return (row.get(key) or row.get(f"\ufeff{key}") or "").strip()

    for path in paths:
        if not path:
            continue
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                rows.append(row)
    # First pass: direct translations.
    for row in rows:
        raw = get_field(row, "原文")
        translated = get_field(row, "译文")
        if raw and translated:
            lookup.setdefault(raw, translated)
    # Second pass: alias -> base name (对应原名) when no direct translation.
    for row in rows:
        raw = get_field(row, "原文")
        alias_of = get_field(row, "对应原名")
        if not raw or not alias_of:
            continue
        if raw in lookup:
            continue
        base_translation = lookup.get(alias_of)
        if base_translation:
            lookup[raw] = base_translation
    return lookup


def has_name_already(
    translated_text: str, base_name: str, candidate_names: List[str]
) -> bool:
    """Check if the translated text already starts with a speaker name.

    Besides exact matches (candidate_names), also accept any non-empty first line
    that does not start with quote punctuation — useful when the name is present
    but not in the dictionary (e.g., manual translation of the name).
    """
    if not translated_text:
        return False
    stripped = translated_text.lstrip()
    _, stripped_no_controls = strip_leading_controls(stripped)
    for name in candidate_names:
        if not name:
            continue
        if stripped.startswith(name) or stripped_no_controls.startswith(name):
            return True
    # Fallback: if the first line has text (after removing control codes) and is
    # not a direct quote, assume it already contains a speaker name even if it
    # is not in our dictionary.
    first_line = translated_text.split("\n", 1)[0].strip()
    if first_line:
        _, first_no_controls = strip_leading_controls(first_line)
        if first_no_controls and not first_no_controls.startswith(QUOTE_PREFIXES):
            return True
    return False


def collect_candidates(
    data: Dict,
    name_lookup: Dict[str, str],
    max_name_length: int,
) -> Dict[str, List[Candidate]]:
    """Gather entries where a speaker name is present in the original but missing in the translation."""
    grouped: Dict[str, List[Candidate]] = {}
    for map_name, entries in data.items():
        if not isinstance(entries, dict):
            continue
        for original, info in entries.items():
            if not isinstance(info, dict):
                continue
            extracted = extract_speaker(original, max_name_length)
            if not extracted:
                continue
            speaker_line, prefix, base_name = extracted
            core, wrap_prefix, wrap_suffix, suffix_punct = decompose_name(base_name)
            translated_text = str(info.get("text") or "")
            # Candidates used to detect "already has name" in translation.
            candidate_names = []
            # Raw name and wrapper variations.
            candidate_names.append(base_name)
            if core and (wrap_prefix or wrap_suffix):
                candidate_names.append(f"{wrap_prefix}{core}{wrap_suffix}")
            if core != base_name:
                candidate_names.append(core)
            # Lookup translations.
            trans_core = name_lookup.get(core) or name_lookup.get(base_name)
            if trans_core:
                candidate_names.append(trans_core)
                candidate_names.append(f"{wrap_prefix}{trans_core}{wrap_suffix}")
                if suffix_punct:
                    candidate_names.append(f"{trans_core}{suffix_punct}")
                    candidate_names.append(f"{wrap_prefix}{trans_core}{wrap_suffix}{suffix_punct}")
            if has_name_already(translated_text, base_name, candidate_names):
                continue
            grouped.setdefault(base_name, []).append(
                Candidate(
                    map_name=map_name,
                    original=original,
                    translation=translated_text,
                    speaker_prefix=prefix,
                    speaker_name=base_name,
                    core=core or base_name,
                    wrap_prefix=wrap_prefix,
                    wrap_suffix=wrap_suffix,
                    suffix_punct=suffix_punct,
                )
            )
    return grouped


def format_single_line(text: str, limit: int = 120) -> str:
    """Render text on one line with escaped newlines and optional truncation."""
    rendered = text.replace("\r\n", "\n").replace("\n", "\\n")
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 3] + "..."


def prompt_for_replacement(
    speaker: str,
    entries: List[Candidate],
    suggested: str,
    max_preview: int,
) -> Optional[str]:
    """
    Ask the user how to fill the missing speaker name.

    Returns the chosen replacement (without control codes) or None if skipped.
    """
    print(f"\nSpeaker: {speaker} (missing in {len(entries)} line(s))")
    print(f"Suggested: {suggested or '(none)'}")
    preview_count = min(max_preview, len(entries))
    for idx in range(preview_count):
        sample = entries[idx]
        print(f"  [{idx + 1}] orig: {format_single_line(sample.original)}")
        print(f"      curr: {format_single_line(sample.translation)}")
        fixed_preview = (
            f"{sample.speaker_prefix}{suggested}\\n{sample.translation}"
        )
        print(f"      fix : {format_single_line(fixed_preview)}")
    print("Choices: [Enter]=use suggested, 's'=skip, 'q'=stop here, or type a custom replacement")
    while True:
        resp = input("Replacement > ").strip()
        if resp == "":
            if not suggested:
                return None
            return suggested
        if resp.lower() in {"s", "skip"}:
            return None
        if resp.lower() in {"q", "quit"}:
            raise KeyboardInterrupt
        return resp


def apply_replacements(
    data: Dict,
    grouped: Dict[str, List[Candidate]],
    replacements: Dict[str, str],
) -> int:
    """Apply chosen replacements to the translation data."""
    updated = 0
    for speaker, entries in grouped.items():
        if speaker not in replacements:
            continue
        name_to_use = replacements[speaker]
        for entry in entries:
            new_text = f"{entry.speaker_prefix}{name_to_use}\n{entry.translation}"
            data[entry.map_name][entry.original]["text"] = new_text
            updated += 1
    return updated


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Find lines where the speaker name from the original text is missing "
            "in the translation, then interactively fill them in."
        )
    )
    parser.add_argument(
        "translation_path",
        type=Path,
        help="Path to translation_translated.json",
    )
    parser.add_argument(
        "--character-dict",
        type=Path,
        help="Path to character_dictionary.csv for suggested name translations.",
    )
    parser.add_argument(
        "--entity-dict",
        type=Path,
        help="Path to entity_dictionary.csv for additional name suggestions.",
    )
    parser.add_argument(
        "--max-name-length",
        type=int,
        default=18,
        help="Maximum length of the first-line speaker name to consider.",
    )
    parser.add_argument(
        "--auto-accept",
        action="store_true",
        help="Skip prompts and apply suggested names for all speakers.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Where to write the updated JSON (default: <stem>_with_names.json).",
    )
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="Write changes back to the input file.",
    )
    parser.add_argument(
        "--max-preview",
        type=int,
        default=3,
        help="How many sample lines to show for each speaker during prompts.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Do not ask for final confirmation before writing.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    translation_path: Path = args.translation_path
    data = load_translation(translation_path)

    dict_paths = []
    if args.character_dict:
        dict_paths.append(args.character_dict)
    else:
        # Default: ../character_dictionary.csv relative to the translation file.
        maybe_char = translation_path.parent.parent / "character_dictionary.csv"
        dict_paths.append(maybe_char)
    if args.entity_dict:
        dict_paths.append(args.entity_dict)
    name_lookup = load_name_lookup(dict_paths)
    if name_lookup:
        print(f"Loaded name suggestions: {len(name_lookup)} entries.")
    else:
        print("No name dictionary loaded (continuing without suggestions).")

    grouped = collect_candidates(
        data,
        name_lookup,
        max_name_length=args.max_name_length,
    )
    total_missing = sum(len(items) for items in grouped.values())
    print(f"Found {total_missing} line(s) missing speaker names across {len(grouped)} speaker(s).")
    if not grouped:
        return

    replacements: Dict[str, str] = {}
    speakers_sorted = sorted(grouped.items(), key=lambda item: len(item[1]), reverse=True)
    try:
        for speaker, entries in speakers_sorted:
            sample = entries[0]
            core = sample.core
            suffix = sample.suffix_punct
            wrap_prefix = sample.wrap_prefix
            wrap_suffix = sample.wrap_suffix
            base_translation = name_lookup.get(core) or name_lookup.get(speaker) or ""
            if not base_translation and args.auto_accept:
                # Skip auto-filling when we don't have a translated name.
                continue
            # Reconstruct with wrappers/suffix so previews mirror original formatting.
            suggested = f"{wrap_prefix}{base_translation}{wrap_suffix}{suffix}"
            if args.auto_accept:
                replacements[speaker] = suggested
                continue
            choice = prompt_for_replacement(
                speaker,
                entries,
                suggested,
                max_preview=args.max_preview,
            )
            if choice is not None:
                replacements[speaker] = choice
    except KeyboardInterrupt:
        print("\nStopped by user. Applying choices made so far.")

    if not replacements:
        print("No replacements selected; nothing to do.")
        return

    updated = apply_replacements(data, grouped, replacements)
    if updated == 0:
        print("No lines updated.")
        return

    if args.in_place:
        output_path = translation_path
    elif args.output:
        output_path = args.output
    else:
        output_path = translation_path.with_name(
            f"{translation_path.stem}_with_names{translation_path.suffix}"
        )

    if not args.yes and not args.auto_accept:
        confirm = input(f"Write {updated} updated line(s) to {output_path}? [y/N] ").strip().lower()
        if confirm not in {"y", "yes"}:
            print("Aborted without writing.")
            return

    output_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=4),
        encoding="utf-8",
    )
    print(f"Wrote {updated} updated line(s) to {output_path}.")


if __name__ == "__main__":
    main()
