"""Optimize Chinese translation line breaks for RPGMaker text boxes.

Reads translation_translated.json, redistributes line breaks in Message entries
to fall at natural punctuation boundaries while respecting text box width limits
(38 bytes with face graphic, 50 bytes without).

Preserves the exact number of \\n in each entry.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Dict, List, Optional

# --- Constants ---

# RPGMaker control codes: zero display width
# Matches: \C[9], \N[1], \V[2], etc. and simple codes \!, \., \<, \>, \|, \^
_CONTROL_CODE_RE = re.compile(
    r'\\[A-Za-z]\[\d+\]'   # parameterized: \C[9], \N[1], etc.
    r'|\\[\.!<>|^]'         # simple escape sequences
)

# Punctuation marks that are preferred line break points (break AFTER these)
_BREAK_PUNCTUATION = set('。！？…，、：；」』）')

# Speaker IDs that indicate no face graphic is shown
_NO_FACE_SPEAKER_IDS = {"NARRATION", "SYSTEM", "NONE", ""}

# Width limits
_WIDTH_WITH_FACE = 38
_WIDTH_WITHOUT_FACE = 50

# Full-width space used for indentation on continuation lines
_FULLWIDTH_SPACE = '\u3000'


# --- Utility functions ---

def calc_display_width(text: str) -> int:
    """Calculate RPGMaker text box display width in bytes.

    Control codes have 0 width. CJK and full-width characters count as 2 bytes,
    everything else counts as 1 byte.
    """
    # Strip control codes first
    stripped = _CONTROL_CODE_RE.sub('', text)
    width = 0
    for ch in stripped:
        cp = ord(ch)
        if (
            0x2E80 <= cp <= 0x9FFF      # CJK radicals, kangxi, ideographs
            or 0xF900 <= cp <= 0xFAFF    # CJK compatibility ideographs
            or 0xFE30 <= cp <= 0xFE4F    # CJK compatibility forms
            or 0xFF01 <= cp <= 0xFF60    # fullwidth ASCII variants
            or 0xFFE0 <= cp <= 0xFFE6    # fullwidth signs
            or cp == 0x3000              # fullwidth space
            or 0x3000 <= cp <= 0x303F    # CJK symbols and punctuation
            or 0x3040 <= cp <= 0x309F    # hiragana
            or 0x30A0 <= cp <= 0x30FF    # katakana
        ):
            width += 2
        else:
            width += 1
    return width


def has_face_graphic(speaker_id: Optional[str]) -> bool:
    """Check if speaker_id indicates a face graphic is displayed."""
    if not speaker_id:
        return False
    return speaker_id not in _NO_FACE_SPEAKER_IDS


def get_max_width(speaker_id: Optional[str]) -> int:
    """Return max line width based on whether a face graphic is shown."""
    return _WIDTH_WITH_FACE if has_face_graphic(speaker_id) else _WIDTH_WITHOUT_FACE


def is_name_line(original_first_line: str) -> bool:
    """Determine if the first line of original text is a character name.

    Heuristic based on original Japanese text structure:
    - Short line (display width <= 20 bytes)
    - Does not end with sentence-ending punctuation
    - Does not start with quote marks
    """
    if not original_first_line:
        return False
    stripped = original_first_line.strip()
    if not stripped:
        return False
    # Lines starting with quotes are dialogue, not names
    if stripped[0] in ('「', '『', '（', '(', '"', '\uE000', '\uE003'):
        return False
    # Lines ending with sentence-ending punctuation are dialogue
    if stripped[-1] in ('。', '！', '？', '」', '』', '）', ')'):
        return False
    # Short lines are likely names
    return calc_display_width(stripped) <= 20


def _strip_indent(line: str) -> str:
    """Strip leading full-width spaces used for indentation."""
    return line.lstrip(_FULLWIDTH_SPACE)


def _calc_width_at_positions(content: str) -> List[int]:
    """Pre-compute cumulative display width at each character position.

    Returns a list where width_at[i] = display width of content[0:i].
    Length is len(content) + 1, with width_at[0] = 0.
    """
    widths = [0] * (len(content) + 1)
    cum = 0
    i = 0
    while i < len(content):
        m = _CONTROL_CODE_RE.match(content, i)
        if m:
            # Control code: 0 width for all chars in it
            for j in range(i, m.end()):
                widths[j + 1] = cum
            i = m.end()
            continue
        ch = content[i]
        cp = ord(ch)
        if (
            0x2E80 <= cp <= 0x9FFF
            or 0xF900 <= cp <= 0xFAFF
            or 0xFE30 <= cp <= 0xFE4F
            or 0xFF01 <= cp <= 0xFF60
            or 0xFFE0 <= cp <= 0xFFE6
            or 0x3000 <= cp <= 0x303F
            or 0x3040 <= cp <= 0x309F
            or 0x30A0 <= cp <= 0x30FF
        ):
            cum += 2
        else:
            cum += 1
        widths[i + 1] = cum
        i += 1
    return widths


def _segment_width(widths: List[int], start: int, end: int) -> int:
    """Get display width of content[start:end] using pre-computed widths."""
    return widths[end] - widths[start]


def redistribute_lines(
    content: str, num_lines: int, max_width: int, indent: str = _FULLWIDTH_SPACE
) -> Optional[List[str]]:
    """Split content into exactly num_lines segments respecting width and punctuation.

    Uses exhaustive search to find the optimal set of break points that:
    1. Keeps all lines within max_width
    2. Prefers breaking at strong punctuation (。！？」』）) over weak (，、：；)
       and weak over ellipsis (……)
    3. Uses asymmetric cost to prefer shorter earlier lines (matching human editing style)

    Returns a list of lines, or None if a valid distribution cannot be found.
    The first line has no indent; subsequent lines are prefixed with `indent`.
    """
    if num_lines <= 0:
        return None
    if num_lines == 1:
        if calc_display_width(content) <= max_width:
            return [content]
        return None

    indent_width = calc_display_width(indent)
    widths = _calc_width_at_positions(content)
    content_len = len(content)

    # Build weighted break candidates: (position, penalty)
    # Tiers:
    #   - Strong punctuation (。！？」』）): penalty 0
    #   - Weak punctuation (，、：；): penalty 1
    #   - Ellipsis (……): context-dependent (8 sentence-end, 16 mid-sentence)
    # \! is merged with preceding punctuation — break AFTER the punct+\! unit.
    _STRONG_PUNCT = set('。！？」』）')
    _ELLIPSIS_PENALTY = 8.0
    _WEAK_PUNCT_PENALTY = 1.0

    candidates: Dict[int, float] = {}  # position -> penalty (keep lowest)

    def _add_candidate(pos: int, penalty: float):
        if pos in candidates:
            candidates[pos] = min(candidates[pos], penalty)
        else:
            candidates[pos] = penalty

    i = 0
    while i < len(content):
        m = _CONTROL_CODE_RE.match(content, i)
        if m:
            if m.group(0) == r'\!':
                _add_candidate(m.end(), 0.0)
            i = m.end()
            continue
        ch = content[i]
        if ch == '…':
            j = i
            while j < len(content) and content[j] == '…':
                j += 1
            m2 = _CONTROL_CODE_RE.match(content, j)
            if m2 and m2.group(0) == r'\!':
                _add_candidate(m2.end(), _ELLIPSIS_PENALTY)
            else:
                _add_candidate(j, _ELLIPSIS_PENALTY)
            i = j
            continue
        if ch in _STRONG_PUNCT:
            end = i + 1
            m2 = _CONTROL_CODE_RE.match(content, end)
            if m2 and m2.group(0) == r'\!':
                _add_candidate(m2.end(), 0.0)
            else:
                _add_candidate(end, 0.0)
        elif ch in _BREAK_PUNCTUATION:
            end = i + 1
            m2 = _CONTROL_CODE_RE.match(content, end)
            if m2 and m2.group(0) == r'\!':
                _add_candidate(m2.end(), _WEAK_PUNCT_PENALTY)
            else:
                _add_candidate(end, _WEAK_PUNCT_PENALTY)
        i += 1

    # Discourage breaking at first of two close strong punctuation marks
    strong_positions = sorted(pos for pos, pen in candidates.items() if pen == 0.0)
    for si in range(len(strong_positions) - 1):
        pos1, pos2 = strong_positions[si], strong_positions[si + 1]
        if _segment_width(widths, pos1, pos2) <= 10:
            candidates[pos1] = max(candidates[pos1], 10.0)

    all_candidates = sorted(candidates.items(), key=lambda x: x[0])

    total_display_width = widths[content_len]
    target_per_line = total_display_width / num_lines

    def _line_cost(seg_w: float) -> float:
        """Asymmetric cost: shorter-than-target is cheap, longer is expensive."""
        diff = seg_w - target_per_line
        if diff > 0:
            return diff * 1.3
        else:
            return abs(diff) * 0.8

    def _do_search(cands: List[tuple]) -> tuple[Optional[List[int]], float]:
        """Run exhaustive search over candidate break points. Returns (break positions, score) or (None, inf)."""
        best: List[Optional[List[int]]] = [None]
        best_score = [float('inf')]

        def _search(line_idx: int, pos: int, chosen: List[int], cum_score: float):
            remaining_lines = num_lines - line_idx
            if remaining_lines == 1:
                eff_w = max_width - indent_width if line_idx > 0 else max_width
                last_w = _segment_width(widths, pos, content_len)
                if last_w > eff_w:
                    return
                total_score = cum_score + _line_cost(last_w)
                if total_score < best_score[0]:
                    best_score[0] = total_score
                    best[0] = chosen[:]
                return

            if cum_score >= best_score[0]:
                return

            eff_w = max_width if line_idx == 0 else max_width - indent_width

            for bp, penalty in cands:
                if bp <= pos:
                    continue
                seg_w = _segment_width(widths, pos, bp)
                if seg_w > eff_w:
                    break
                chosen.append(bp)
                _search(line_idx + 1, bp, chosen, cum_score + _line_cost(seg_w) + penalty)
                chosen.pop()

        _search(0, 0, [], 0.0)
        return best[0], best_score[0]

    # First pass: punctuation-only candidates
    best_result, _ = _do_search(all_candidates)

    # Build CJK-augmented candidate list
    _CJK_PENALTY = 20.0
    cjk_candidates = dict(candidates)
    for k in range(1, num_lines):
        eff_indent = indent_width if k > 0 else 0
        ideal_cum = k * target_per_line + (k - 1) * eff_indent if k > 1 else target_per_line
        ci = 0
        while ci < content_len:
            m = _CONTROL_CODE_RE.match(content, ci)
            if m:
                ci = m.end()
                continue
            cp = ord(content[ci])
            is_cjk = (0x2E80 <= cp <= 0x9FFF or 0x3040 <= cp <= 0x30FF)
            if is_cjk and content[ci] not in _BREAK_PUNCTUATION:
                pos = ci + 1
                cum_w = widths[pos]
                if abs(cum_w - ideal_cum) <= 6 and pos not in cjk_candidates:
                    cjk_candidates[pos] = _CJK_PENALTY
            ci += 1
    fallback_cands = sorted(cjk_candidates.items(), key=lambda x: x[0])

    if best_result is None:
        # Fallback: punct-only failed, try CJK-augmented
        best_result, _ = _do_search(fallback_cands)

    if best_result is None:
        return None

    # Build lines from break positions
    all_breaks = [0] + best_result + [content_len]
    lines: List[str] = []
    for i in range(num_lines):
        lines.append(content[all_breaks[i]:all_breaks[i + 1]])

    # Add indentation to continuation lines
    result: List[str] = []
    for i, line in enumerate(lines):
        if i > 0 and indent and line:
            result.append(indent + line)
        else:
            result.append(line)

    # Validate
    for line in result:
        if calc_display_width(line) > max_width:
            return None
    if any(not line.strip() for line in result):
        return None

    return result


def optimize_entry(
    text: str, original_text: str, speaker_id: Optional[str]
) -> str:
    """Optimize line breaks in a single translated Message entry.

    Tries to preserve the original line count, but allows ±1 variation
    when the original count doesn't produce a valid layout (max 4 lines total).
    Returns the optimized text, or the original text unchanged if optimization
    is not possible or not needed.
    """
    original_newline_count = original_text.count('\n')
    if original_newline_count == 0:
        return text

    max_width = get_max_width(speaker_id)
    original_lines = original_text.split('\n')
    translated_lines = text.split('\n')

    # Determine if the first line is a character name (based on original structure)
    name_line: Optional[str] = None
    content_start_idx = 0

    if len(original_lines) >= 2 and is_name_line(original_lines[0]):
        name_line = translated_lines[0]
        content_start_idx = 1

    # Extract content lines (the lines to redistribute)
    content_lines = translated_lines[content_start_idx:]

    if len(content_lines) <= 1:
        return text

    # Target line count from original text structure
    num_content_lines = len(original_lines) - content_start_idx

    # Join content into a single string, stripping indentation
    content_parts = []
    for line in content_lines:
        content_parts.append(_strip_indent(line))
    content = ''.join(content_parts)

    if not content.strip():
        return text

    # Determine indentation pattern from original
    # Check if original continuation lines use full-width space indent
    has_indent = any(
        original_lines[i].startswith(_FULLWIDTH_SPACE)
        for i in range(content_start_idx + 1, len(original_lines))
    )
    indent = _FULLWIDTH_SPACE if has_indent else ''

    # Redistribute with original line count
    max_content = 4 - (1 if name_line is not None else 0)
    new_content_lines = redistribute_lines(content, num_content_lines, max_width, indent)

    # Try fewer lines if: original failed, or result has layout issues
    should_try_fewer = (new_content_lines is None)
    if new_content_lines is not None and len(new_content_lines) >= 2:
        line_widths = [calc_display_width(_strip_indent(l)) for l in new_content_lines]
        # Orphaned punctuation on last line (≤ 4 bytes like 」)
        if line_widths[-1] <= 4:
            should_try_fewer = True
        # Very short first line with 3+ content lines: try collapsing
        elif len(new_content_lines) >= 3 and line_widths[0] < max_width * 0.3:
            should_try_fewer = True

    if should_try_fewer:
        for alt in range(num_content_lines - 1, 0, -1):
            if alt > max_content:
                continue
            alt_result = redistribute_lines(content, alt, max_width, indent)
            if alt_result is not None:
                new_content_lines = alt_result
                break

    # If still None, try more lines
    if new_content_lines is None and num_content_lines + 1 <= max_content:
        new_content_lines = redistribute_lines(content, num_content_lines + 1, max_width, indent)

    if new_content_lines is None:
        return text

    # Reconstruct full text
    if name_line is not None:
        result_lines = [name_line] + new_content_lines
    else:
        result_lines = new_content_lines

    result = '\n'.join(result_lines)

    return result


def process_json(data: Dict) -> tuple[int, int, int]:
    """Process all entries in the translation JSON, optimizing line breaks.

    Returns (total_message_entries, modified_count, skipped_count).
    """
    total = 0
    modified = 0
    skipped = 0

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

            optimized = optimize_entry(text, original_text, speaker_id)
            if optimized != text:
                info["text"] = optimized
                modified += 1
            else:
                skipped += 1

    return total, modified, skipped


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Optimize Chinese translation line breaks for RPGMaker text boxes."
    )
    parser.add_argument(
        "translation_path",
        type=Path,
        help="Path to translation_translated.json",
    )
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="Write changes back to the input file.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Where to write the updated JSON (default: <stem>_optimized.json).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only report statistics, do not write any files.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    translation_path: Path = args.translation_path

    if not translation_path.exists():
        print(f"Error: file not found: {translation_path}")
        return

    print(f"Loading {translation_path} ...")
    with translation_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    total, modified, skipped = process_json(data)
    print(f"Processed {total} Message entries: {modified} modified, {skipped} unchanged.")

    if args.dry_run or modified == 0:
        if modified == 0:
            print("No changes needed.")
        return

    if args.in_place:
        output_path = translation_path
    elif args.output:
        output_path = args.output
    else:
        output_path = translation_path.with_name(
            f"{translation_path.stem}_optimized{translation_path.suffix}"
        )

    print(f"Writing to {output_path} ...")
    output_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=4),
        encoding="utf-8",
    )
    print(f"Done. Wrote {modified} optimized entries to {output_path}.")


if __name__ == "__main__":
    main()
