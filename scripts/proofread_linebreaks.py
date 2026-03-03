"""Lightweight Chinese line break proofreader for RPGMaker translations.

Conservative fixes only:
1. Merge orphaned short last lines into the previous line
2. Shift line breaks to the nearest punctuation boundary

Skips entries with special control codes (\c[], \s[], \n[], \v[]).
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import jieba.posseg as pseg

from optimize_linebreaks import (
    calc_display_width,
    get_max_width,
    is_name_line,
    redistribute_lines,
    _CONTROL_CODE_RE,
    _FULLWIDTH_SPACE,
)

# Punctuation that are natural break points (break AFTER these)
_BREAK_PUNCT = set('，。！？；：、…）」』')

# Control codes that indicate special formatting — skip these entries
_SPECIAL_CODE_RE = re.compile(r'\\[cCsSnNvV]\[', re.IGNORECASE)

_ORPHAN_MAX_WIDTH = 6  # display bytes; ~3 CJK chars

# Multi-speaker line pattern: line starts with speaker name + colon
_MULTI_SPEAKER_RE = re.compile(r'^[^\n：:]{1,10}[：:]')

# Consecutive fullwidth spaces used for intentional alignment
_ALIGN_SPACES_RE = re.compile(r'\u3000{2,}')

# POS tags indicating independent clause starts — don't pull these forward
_INDEPENDENT_POS = {'e', 'y', 'zg', 'c', 'r'}  # interjection, modal, morpheme, conjunction, pronoun


def _has_special_codes(text: str) -> bool:
    return bool(_SPECIAL_CODE_RE.search(text))


def _is_multi_speaker(lines: List[str]) -> bool:
    """Detect multi-speaker format where each line is a different character."""
    speaker_lines = sum(1 for l in lines if _MULTI_SPEAKER_RE.match(l))
    return speaker_lines >= 2


def _find_punct_in_window(content: str, pos: int, window: int = 4, forward: bool = True) -> Optional[int]:
    """Find the best punctuation position near `pos` within a window.

    If forward=False, only search BEFORE pos (move break earlier = push text to next line).
    Returns the position RIGHT AFTER the punctuation char, or None.
    """
    best: Optional[Tuple[int, int]] = None  # (distance, break_pos)

    # Find what visible char index corresponds to `pos`
    target_char = 0
    ci = 0
    while ci < pos and ci < len(content):
        m = _CONTROL_CODE_RE.match(content, ci)
        if m:
            ci = m.end()
            continue
        target_char += 1
        ci += 1

    # Scan content looking for punctuation near target_char
    ci = 0
    visible = 0
    while ci < len(content):
        m = _CONTROL_CODE_RE.match(content, ci)
        if m:
            ci = m.end()
            continue
        ch = content[ci]
        if ch in _BREAK_PUNCT:
            dist = visible - target_char  # negative = before pos, positive = after
            if not forward and dist >= 0:
                # Skip punctuation at or after the break point
                visible += 1
                ci += 1
                continue
            abs_dist = abs(dist)
            if abs_dist <= window:
                break_pos = ci + 1
                # Skip \! after punctuation
                m2 = _CONTROL_CODE_RE.match(content, break_pos)
                if m2 and m2.group(0) == r'\!':
                    break_pos = m2.end()
                if best is None or abs_dist < best[0]:
                    best = (abs_dist, break_pos)
        visible += 1
        ci += 1

    return best[1] if best else None


def fix_orphan(lines: List[str], orig_content_lines: List[str], max_width: int) -> Optional[List[str]]:
    """Merge orphaned last line into previous if it fits.

    Skips if the original text also has a short last line (intentional structure).
    """
    if len(lines) < 2:
        return None
    last = lines[-1]
    if calc_display_width(last) > _ORPHAN_MAX_WIDTH:
        return None
    prev = lines[-2]
    # Don't merge after \! (pause command — line break is intentional)
    if prev.endswith(r'\!'):
        return None
    # If original also has a short last line, it's intentional
    if len(orig_content_lines) >= 2:
        orig_last = orig_content_lines[-1]
        if calc_display_width(orig_last) <= _ORPHAN_MAX_WIDTH * 2:
            return None
    merged = prev + last
    if calc_display_width(merged) > max_width:
        return None
    return lines[:-2] + [merged]


def fix_break_positions(lines: List[str], max_width: int) -> List[str]:
    """Shift breaks to nearest punctuation. Iterates until stable."""
    if len(lines) < 2:
        return lines

    result = list(lines)
    for _round in range(5):
        changed = False
        i = 0
        while i < len(result) - 1:
            line_a = result[i]
            line_b = result[i + 1]

            # Skip empty lines (intentional blank lines)
            if not line_a or not line_b:
                i += 1
                continue

            # Check if break is already at punctuation
            stripped_a = _CONTROL_CODE_RE.sub('', line_a)
            if stripped_a and stripped_a[-1] in _BREAK_PUNCT:
                i += 1
                continue

            # \! at end = pause command, don't touch this break
            if re.search(r'\\!$', line_a):
                i += 1
                continue

            # Skip lines with intentional alignment spacing
            if _ALIGN_SPACES_RE.search(line_a) or _ALIGN_SPACES_RE.search(line_b):
                i += 1
                continue

            # Strip leading fullwidth space from line_b before joining;
            # we'll re-add indent to the new line_b after splitting
            had_indent = line_b.startswith(_FULLWIDTH_SPACE)
            bare_b = line_b.lstrip(_FULLWIDTH_SPACE) if had_indent else line_b
            joined = line_a + bare_b
            break_pos = len(line_a)

            # Very short line: merge into next line, let next round re-break
            # Skip empty lines (intentional blank lines from original)
            if stripped_a and calc_display_width(stripped_a) <= _ORPHAN_MAX_WIDTH:
                if calc_display_width(joined) <= max_width:
                    result[i] = joined
                    result.pop(i + 1)
                    changed = True
                    continue

            # Prefer backward (push text to next line)
            new_break = _find_punct_in_window(joined, break_pos, window=8, forward=False)
            # Fallback: forward pull, filtered by jieba POS tagging.
            # Reject if pulled content starts with an independent clause
            # (interjection, modal particle, conjunction, etc.)
            if new_break is None:
                fwd = _find_punct_in_window(joined, break_pos, window=8, forward=True)
                if fwd is not None:
                    pulled_text = _CONTROL_CODE_RE.sub('', joined[break_pos:fwd])
                    non_punct = ''.join(c for c in pulled_text if c not in _BREAK_PUNCT)
                    if non_punct:
                        first_word = next(pseg.cut(non_punct))
                        if first_word.flag not in _INDEPENDENT_POS:
                            new_break = fwd

            # Don't break in the middle of ellipsis (……)
            if (new_break is not None and new_break > 0
                    and joined[new_break - 1] == '…'
                    and new_break < len(joined) and joined[new_break] == '…'):
                new_break = None

            if new_break is not None and new_break != break_pos:
                new_a = joined[:new_break]
                new_b = joined[new_break:]
                # Manage fullwidth space indent: add if inside open quote,
                # strip if no longer needed
                # Check quote state from all preceding lines + new_a
                preceding = ''.join(result[:i]) + new_a
                in_quote = (preceding.count('「') > preceding.count('」')
                            or preceding.count('『') > preceding.count('』'))
                new_b_stripped = new_b.lstrip(_FULLWIDTH_SPACE)
                if (in_quote and new_b_stripped
                        and new_b_stripped[0] not in ('」', '』', '）')
                        and not new_b.startswith(_FULLWIDTH_SPACE)):
                    new_b = _FULLWIDTH_SPACE + new_b
                elif not in_quote and new_b.startswith(_FULLWIDTH_SPACE):
                    new_b = new_b.lstrip(_FULLWIDTH_SPACE)
                if (new_b and
                        calc_display_width(new_a) <= max_width and
                        calc_display_width(new_b) <= max_width):
                    result[i] = new_a
                    result[i + 1] = new_b
                    changed = True
            i += 1
        if not changed:
            break
    return result


def proofread_entry(text: str, original_text: str, speaker_id: Optional[str]) -> str:
    """Apply conservative fixes to a single entry."""
    if '\n' not in text:
        return text
    if _has_special_codes(text):
        return text

    max_width = get_max_width(speaker_id)
    orig_lines = original_text.split('\n')
    lines = text.split('\n')

    # Detect name line
    content_start = 0
    if len(orig_lines) >= 2 and is_name_line(orig_lines[0]):
        content_start = 1

    name_parts = lines[:content_start]
    content_lines = lines[content_start:]

    if len(content_lines) < 2:
        return text

    # Skip multi-speaker format (each line is a different character)
    if _is_multi_speaker(content_lines):
        return text

    # Fix 1: shift breaks to punctuation
    orig_content_lines = orig_lines[content_start:]
    content_lines = fix_break_positions(content_lines, max_width)

    # Fix 2: merge orphan last line
    merged = fix_orphan(content_lines, orig_content_lines, max_width)
    if merged is not None:
        content_lines = merged

    return '\n'.join(name_parts + content_lines)


def process_json(data: Dict) -> Tuple[int, int, int, List[Tuple[str, str, str]]]:
    """Process all entries. Returns (total, modified, skipped, samples)."""
    total = modified = skipped = 0
    samples: List[Tuple[str, str, str]] = []  # (key_preview, before, after)

    for map_name, entries in data.items():
        if not isinstance(entries, dict):
            continue
        for original_text, info in entries.items():
            if not isinstance(info, dict):
                continue
            if info.get("original_marker") != "Message":
                continue
            if '\n' not in original_text:
                continue

            total += 1
            text = info.get("text", "")
            speaker_id = info.get("speaker_id")
            result = proofread_entry(text, original_text, speaker_id)

            if result != text:
                info["text"] = result
                modified += 1
                if len(samples) < 50:
                    key_short = original_text[:40].replace('\n', '\\n')
                    samples.append((key_short, text, result))
            else:
                skipped += 1

    return total, modified, skipped, samples


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Lightweight Chinese line break proofreader for RPGMaker."
    )
    parser.add_argument("translation_path", type=Path)
    parser.add_argument("--in-place", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    path: Path = args.translation_path
    if not path.exists():
        print(f"Error: {path} not found")
        return

    print(f"Loading {path} ...")
    data = json.loads(path.read_text(encoding="utf-8"))

    total, modified, skipped, samples = process_json(data)
    print(f"Processed {total} entries: {modified} modified, {skipped} unchanged.")

    if samples:
        report = path.with_name(f"{path.stem}_proofread_report.txt")
        nl = '\n'
        with report.open("w", encoding="utf-8") as f:
            f.write(f"Proofread report: {modified} modified / {total} total\n")
            f.write("=" * 60 + "\n\n")
            for key, before, after in samples:
                f.write(f"Key: {key}...\n")
                f.write(f"  Before: {before.replace(nl, chr(0x2936))}\n")
                f.write(f"  After:  {after.replace(nl, chr(0x2936))}\n\n")
        print(f"Sample report written to {report}")

    if args.dry_run or modified == 0:
        return

    if args.in_place:
        out = path
    elif args.output:
        out = args.output
    else:
        out = path.with_name(f"{path.stem}_proofread{path.suffix}")

    print(f"\nWriting to {out} ...")
    out.write_text(json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8")
    print(f"Done. {modified} entries written.")


if __name__ == "__main__":
    main()
