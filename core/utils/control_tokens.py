# core/utils/control_tokens.py
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

log = logging.getLogger(__name__)

PLACEHOLDER_START = 0xE100
PLACEHOLDER_END = 0xF7FF
QUOTE_TOKENS = {"「", "」", "『", "』"}
SPECIAL_ESCAPE_CHARS = set("!.|^<>${}\\")
STRUCTURAL_SPECIAL_ESCAPES = {"!", ".", "|", "^", "<", ">", "{", "}", "$", "\\"}
STRUCTURAL_CONTROL_NAME_RE = re.compile(
    r"\\(?:F|FF|FFF|FFFF|F[1-8]|M|MM|MMM|MMMM|M[1-8]|AA|FH)(?:\[|$)",
    re.I,
)
INLINE_CONTROL_NAME_RE = re.compile(r"\\(?:C|V|N|P|G|I|PX|PY|FS|FN|FSZ|FST|OC|OW|RC|FB|FI|FR)(?:\[|$)", re.I)


@dataclass(frozen=True)
class ControlCodeProfile:
    """Describes which control-code families are expected for a game."""

    engine: str = "generic"
    enabled_plugins: Tuple[str, ...] = ()
    plugin_rules: Tuple[str, ...] = ()
    protect_quotes: bool = False

    @property
    def summary(self) -> str:
        plugins = ", ".join(self.enabled_plugins[:8])
        if len(self.enabled_plugins) > 8:
            plugins += f", ...(+{len(self.enabled_plugins) - 8})"
        rules = ", ".join(self.plugin_rules) or "base"
        return f"engine={self.engine}, rules={rules}, plugins=[{plugins}]"


@dataclass(frozen=True)
class ProtectedToken:
    placeholder: str
    literal: str
    start: int
    end: int
    line: int
    column: int
    kind: str


@dataclass(frozen=True)
class ProtectedText:
    original: str
    text: str
    tokens: Tuple[ProtectedToken, ...]
    profile: ControlCodeProfile

    @property
    def placeholder_map(self) -> Dict[str, str]:
        return {token.placeholder: token.literal for token in self.tokens}

    @property
    def expected_placeholders(self) -> Tuple[str, ...]:
        return tuple(token.placeholder for token in self.tokens)


@dataclass(frozen=True)
class TokenOccurrence:
    literal: str
    start: int
    end: int
    line: int
    column: int
    kind: str


def default_profile() -> ControlCodeProfile:
    return ControlCodeProfile()


def profile_from_game(game_path: Optional[str]) -> ControlCodeProfile:
    """Build a reusable profile from an MV/MZ game directory when possible."""
    if not game_path:
        return default_profile()

    enabled_plugins = _read_enabled_mvmz_plugins(game_path)
    plugin_rules: List[str] = []
    if "LL_StandingPicture" in enabled_plugins:
        plugin_rules.append("LL_StandingPicture")
    if "LL_StandingPictureBattle" in enabled_plugins:
        plugin_rules.append("LL_StandingPictureBattle")

    engine = "mvmz" if os.path.exists(os.path.join(game_path, "js", "plugins.js")) else "generic"
    return ControlCodeProfile(
        engine=engine,
        enabled_plugins=tuple(enabled_plugins),
        plugin_rules=tuple(plugin_rules),
        protect_quotes=False,
    )


def protect_text(text: str, profile: Optional[ControlCodeProfile] = None) -> ProtectedText:
    """Replace immutable control tokens with per-text PUA placeholders."""
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    profile = profile or default_profile()
    spans = _scan_immutable_tokens(text, profile)
    if not spans:
        return ProtectedText(text, text, tuple(), profile)

    used_chars = set(text)
    next_codepoint = PLACEHOLDER_START
    pieces: List[str] = []
    tokens: List[ProtectedToken] = []
    cursor = 0

    for span in spans:
        start, end, kind = span
        pieces.append(text[cursor:start])
        placeholder, next_codepoint = _next_placeholder(used_chars, next_codepoint)
        used_chars.add(placeholder)
        literal = text[start:end]
        line, column = _line_column(text, start)
        token = ProtectedToken(
            placeholder=placeholder,
            literal=literal,
            start=start,
            end=end,
            line=line,
            column=column,
            kind=kind,
        )
        tokens.append(token)
        pieces.append(placeholder)
        cursor = end

    pieces.append(text[cursor:])
    return ProtectedText(text, "".join(pieces), tuple(tokens), profile)


def restore_protected_text(raw_translation: str, protected: ProtectedText) -> Tuple[bool, str, str]:
    """Restore placeholders and reject missing, duplicated, or reordered tokens."""
    if not isinstance(raw_translation, str):
        return False, "", "译文不是字符串，无法还原控制码占位符。"

    expected = protected.expected_placeholders
    if not expected:
        unexpected = [ch for ch in raw_translation if _is_reserved_placeholder(ch)]
        if unexpected:
            return False, raw_translation, "译文包含原文没有的控制码占位符。"
        return True, raw_translation, ""

    expected_set = set(expected)
    actual: List[str] = []
    for ch in raw_translation:
        if ch in expected_set or _is_reserved_placeholder(ch):
            actual.append(ch)

    if tuple(actual) != expected:
        return (
            False,
            raw_translation,
            "控制码占位符序列不一致: "
            f"期望 {_format_placeholder_sequence(expected)}, 实际 {_format_placeholder_sequence(actual)}。",
        )

    restored = raw_translation
    for token in protected.tokens:
        restored = restored.replace(token.placeholder, token.literal, 1)
    return True, restored, ""


def validate_restored_text(
    original: str,
    restored_translation: str,
    protected: Optional[ProtectedText] = None,
    profile: Optional[ControlCodeProfile] = None,
) -> Tuple[bool, str]:
    """Verify that immutable control tokens survived exactly and stayed on their lines."""
    if not isinstance(original, str) or not isinstance(restored_translation, str):
        return False, "控制码校验失败: 原文或译文不是字符串。"

    profile = profile or (protected.profile if protected else default_profile())
    original_tokens = [tok.literal for tok in iter_token_occurrences(original, profile, include_quotes=False)]
    translated_tokens = [tok.literal for tok in iter_token_occurrences(restored_translation, profile, include_quotes=False)]
    if original_tokens != translated_tokens:
        return (
            False,
            "控制码校验失败: token 序列不一致。"
            f"原文={original_tokens}, 译文={translated_tokens}",
        )

    original_lines = original.split("\n")
    translated_lines = restored_translation.split("\n")
    if len(translated_lines) != len(original_lines) and original_tokens:
        return (
            False,
            f"控制码校验失败: 含控制码文本的行数不一致。原文 {len(original_lines)} 行，译文 {len(translated_lines)} 行。",
        )

    for line_index, original_line in enumerate(original_lines):
        translated_line = translated_lines[line_index] if line_index < len(translated_lines) else ""
        original_line_tokens = [tok.literal for tok in iter_token_occurrences(original_line, profile, include_quotes=False)]
        translated_line_tokens = [tok.literal for tok in iter_token_occurrences(translated_line, profile, include_quotes=False)]
        if original_line_tokens != translated_line_tokens:
            return (
                False,
                "控制码校验失败: 行内 token 序列不一致。"
                f"第 {line_index + 1} 行原文={original_line_tokens}, 译文={translated_line_tokens}",
            )

        original_leading = _edge_group(original_line, profile, leading=True, structural_only=True)
        translated_leading = _edge_group(translated_line, profile, leading=True, structural_only=True)
        if original_leading != translated_leading:
            return (
                False,
                f"控制码校验失败: 第 {line_index + 1} 行行首结构控制码组不一致。"
                f"原文={original_leading!r}, 译文={translated_leading!r}",
            )

        original_trailing = _edge_group(original_line, profile, leading=False, structural_only=True)
        translated_trailing = _edge_group(translated_line, profile, leading=False, structural_only=True)
        if original_trailing != translated_trailing:
            return (
                False,
                f"控制码校验失败: 第 {line_index + 1} 行行尾结构控制码组不一致。"
                f"原文={original_trailing!r}, 译文={translated_trailing!r}",
            )

        for group in _contiguous_token_groups(original_line, profile, structural_only=True):
            if group and group not in translated_line:
                return (
                    False,
                    f"控制码校验失败: 第 {line_index + 1} 行连续结构控制码组被拆散或改写: {group!r}",
                )

    return True, ""


def iter_token_occurrences(
    text: str,
    profile: Optional[ControlCodeProfile] = None,
    include_quotes: bool = True,
) -> Iterable[TokenOccurrence]:
    profile = profile or default_profile()
    for start, end, kind in _scan_immutable_tokens(text, profile):
        if kind == "quote" and not include_quotes:
            continue
        line, column = _line_column(text, start)
        yield TokenOccurrence(
            literal=text[start:end],
            start=start,
            end=end,
            line=line,
            column=column,
            kind=kind,
        )


def extract_token_literals(text: str, profile: Optional[ControlCodeProfile] = None) -> List[str]:
    return [token.literal for token in iter_token_occurrences(text, profile)]


def extract_control_literals(text: str, profile: Optional[ControlCodeProfile] = None) -> List[str]:
    return [token.literal for token in iter_token_occurrences(text, profile, include_quotes=False)]


def strip_token_literals(text: str, profile: Optional[ControlCodeProfile] = None) -> str:
    """Remove immutable tokens for language checks in smoke tests."""
    if not isinstance(text, str):
        return ""
    profile = profile or default_profile()
    spans = _scan_immutable_tokens(text, profile)
    if not spans:
        return text
    pieces: List[str] = []
    cursor = 0
    for start, end, _kind in spans:
        pieces.append(text[cursor:start])
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def _scan_immutable_tokens(text: str, profile: ControlCodeProfile) -> List[Tuple[int, int, str]]:
    spans: List[Tuple[int, int, str]] = []
    i = 0
    length = len(text)
    while i < length:
        ch = text[i]
        if profile.protect_quotes and ch in QUOTE_TOKENS:
            spans.append((i, i + 1, "quote"))
            i += 1
            continue

        if ch == "\\":
            end = _scan_backslash_token(text, i)
            if end > i:
                spans.append((i, end, _classify_control_token(text[i:end], profile)))
                i = end
                continue

        i += 1
    return spans


def _scan_backslash_token(text: str, start: int) -> int:
    length = len(text)
    if start + 1 >= length:
        return start + 1

    i = start + 1
    ch = text[i]
    if ch in SPECIAL_ESCAPE_CHARS:
        return i + 1

    if ch.isascii() and (ch.isalpha() or ch.isdigit() or ch == "_"):
        i += 1
        while i < length and text[i].isascii() and (text[i].isalnum() or text[i] == "_"):
            i += 1
        while i < length and text[i] == "[":
            bracket_end = _consume_bracket_group(text, i)
            if bracket_end <= i:
                break
            i = bracket_end
        return i

    # A backslash followed by non-command text (for example "\ティサ") is not a
    # control token. Preserve only the literal backslash; the following Japanese
    # text must remain translatable.
    return start + 1


def _classify_control_token(literal: str, profile: ControlCodeProfile) -> str:
    if literal == "\\":
        return "literal-backslash"
    if _is_structural_control_literal(literal):
        return "structural-control"
    if _is_inline_control_literal(literal):
        return "inline-control"
    if (
        ("LL_StandingPicture" in profile.plugin_rules or "LL_StandingPictureBattle" in profile.plugin_rules)
        and re.match(r"\\(?:F|FF|FFF|FFFF|F[1-8]|M|MM|MMM|MMMM|M[1-8]|AA|FH)\[", literal, re.I)
    ):
        return "plugin:LL_StandingPicture"
    return "control"


def _is_structural_control_literal(literal: str) -> bool:
    if not literal.startswith("\\"):
        return False
    if len(literal) == 2 and literal[1] in STRUCTURAL_SPECIAL_ESCAPES:
        return True
    return bool(STRUCTURAL_CONTROL_NAME_RE.match(literal))


def _is_inline_control_literal(literal: str) -> bool:
    return bool(INLINE_CONTROL_NAME_RE.match(literal))


def _is_structural_occurrence(token: TokenOccurrence) -> bool:
    return token.kind in {"structural-control", "plugin:LL_StandingPicture"} or _is_structural_control_literal(token.literal)


def _consume_bracket_group(text: str, start: int) -> int:
    depth = 0
    i = start
    length = len(text)
    while i < length:
        ch = text[i]
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return i + 1
        elif ch in "\r\n" and depth > 0:
            # Bracketed control-code parameters should not span message lines.
            return i
        i += 1
    return length


def _edge_group(text: str, profile: ControlCodeProfile, leading: bool, structural_only: bool = False) -> str:
    tokens = list(iter_token_occurrences(text, profile, include_quotes=False))
    if structural_only:
        tokens = [token for token in tokens if _is_structural_occurrence(token)]
    if not tokens:
        return ""

    if leading:
        group: List[str] = []
        expected_start = 0
        for token in tokens:
            if token.start != expected_start:
                break
            group.append(token.literal)
            expected_start = token.end
        return "".join(group)

    group_rev: List[str] = []
    expected_end = len(text)
    for token in reversed(tokens):
        if token.end != expected_end:
            break
        group_rev.append(token.literal)
        expected_end = token.start
    return "".join(reversed(group_rev))


def _contiguous_token_groups(text: str, profile: ControlCodeProfile, structural_only: bool = False) -> List[str]:
    tokens = list(iter_token_occurrences(text, profile, include_quotes=False))
    if structural_only:
        tokens = [token for token in tokens if _is_structural_occurrence(token)]
    groups: List[str] = []
    current: List[str] = []
    expected_start: Optional[int] = None
    for token in tokens:
        if expected_start is None or token.start == expected_start:
            current.append(token.literal)
        else:
            if len(current) > 1:
                groups.append("".join(current))
            current = [token.literal]
        expected_start = token.end
    if len(current) > 1:
        groups.append("".join(current))
    return groups


def _next_placeholder(used_chars: set, start_codepoint: int) -> Tuple[str, int]:
    codepoint = start_codepoint
    while codepoint <= PLACEHOLDER_END:
        ch = chr(codepoint)
        if ch not in used_chars:
            return ch, codepoint + 1
        codepoint += 1
    raise ValueError("可用 PUA 控制码占位符已耗尽。")


def _is_reserved_placeholder(ch: str) -> bool:
    if len(ch) != 1:
        return False
    return PLACEHOLDER_START <= ord(ch) <= PLACEHOLDER_END


def _line_column(text: str, index: int) -> Tuple[int, int]:
    line = text.count("\n", 0, index)
    last_newline = text.rfind("\n", 0, index)
    column = index if last_newline < 0 else index - last_newline - 1
    return line, column


def _format_placeholder_sequence(placeholders: Sequence[str]) -> str:
    return "[" + ", ".join(f"U+{ord(ch):04X}" for ch in placeholders) + "]"


def _read_enabled_mvmz_plugins(game_path: str) -> List[str]:
    plugins_path = os.path.join(game_path, "js", "plugins.js")
    if not os.path.exists(plugins_path):
        return []

    try:
        with open(plugins_path, "r", encoding="utf-8-sig") as f:
            content = f.read()
    except OSError as exc:
        log.warning("读取 plugins.js 失败: %s", exc)
        return []

    parsed = _parse_plugins_js_json(content)
    if parsed is None:
        return _parse_plugins_js_fallback(content)

    enabled: List[str] = []
    for plugin in parsed:
        if not isinstance(plugin, dict):
            continue
        status = plugin.get("status")
        is_enabled = status is True or str(status).lower() == "true"
        name = plugin.get("name")
        if is_enabled and name:
            enabled.append(str(name))
    return enabled


def _parse_plugins_js_json(content: str) -> Optional[List[dict]]:
    marker = content.find("$plugins")
    if marker < 0:
        return None
    start = content.find("[", marker)
    end = content.rfind("]")
    if start < 0 or end <= start:
        return None
    payload = content[start : end + 1]
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, list) else None


def _parse_plugins_js_fallback(content: str) -> List[str]:
    enabled: List[str] = []
    object_pattern = re.compile(r'\{[^{}]*"name"\s*:\s*"([^"]+)"[^{}]*"status"\s*:\s*(true|false)', re.I)
    for match in object_pattern.finditer(content):
        name, status = match.groups()
        if status.lower() == "true":
            enabled.append(name)
    return enabled
