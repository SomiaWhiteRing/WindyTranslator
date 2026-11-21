"""Find paused items in the current project; never persist a draft as a translation."""
import json
import re


def _already_saved(translated, name, key, meta):
    saved = translated.get(name, {}).get(key, {})
    if not saved:
        return False
    try:
        from core.tasks.translate import _is_reusable_translation_result
    except ImportError:
        # Standalone copies can still review a project without the translation engine.
        return saved.get("status") == "success" and bool((saved.get("text") or "").strip())
    return _is_reusable_translation_result(saved, meta)


def _legacy_failures(directory, summary, sources, translated):
    """9.3/10.1 logged per-request IDs, but did not explicitly name the terminal items."""
    reason = summary.get("reason", "")
    terminal = re.match(r"^(\d+) 条文本多次校验未通过", reason)
    reasoning_pause = reason.startswith("同一文本两次耗尽输出预算")
    if not (terminal or reasoning_pause) or not summary.get("run_id"):
        return {}
    log_path = directory / "translation_requests.jsonl"
    if not log_path.is_file():
        return {}

    candidates = {}
    reasoning_hits = {}
    with log_path.open("rb") as log:
        for line in log:
            try:
                row = json.loads(line)
            except (UnicodeDecodeError, ValueError):
                continue
            if not isinstance(row, dict) or row.get("run_id") != summary["run_id"]:
                continue
            name = row.get("file")
            issues = row.get("issues") or {}
            batch = row.get("sources") or []
            usage = row.get("usage") or {}
            output_tokens = usage.get("completion_tokens") or 0
            reasoning_only = (reasoning_pause and row.get("finish_reason") == "length"
                              and not (row.get("response") or "").strip() and output_tokens
                              and ((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0) >= output_tokens)
            for source in batch:
                key = source["original"]
                pair = (name, key)
                if _already_saved(translated, name, key, sources.get(name, {}).get(key, {})):
                    continue
                failure = issues.get(str(source["id"]))
                if failure:
                    candidates[pair] = (source["id"], failure, row.get("response", ""))
                    if reasoning_only:
                        reasoning_hits[pair] = reasoning_hits.get(pair, 0) + 1
                else:
                    candidates.pop(pair, None)

    if reasoning_pause:
        candidates = {pair: last for pair, last in candidates.items() if reasoning_hits.get(pair, 0) >= 2}
    elif len(candidates) != int(terminal[1]):
        # Parallel requests might still have failed once while a different batch paused.
        # The old journal has no terminal-item flag to disambiguate those candidates.
        return {}

    try:
        from core.tasks import translation_protocol as protocol
    except ImportError:
        protocol = None  # Standalone tool without the host's control-code decoder.
    pending = {}
    for (name, key), (identifier, failure, response) in candidates.items():
        meta = sources.get(name, {}).get(key)
        if meta is None:
            continue  # Old fragment logs can lack the enclosing source key.
        draft = None
        if protocol and response and not meta.get("wolf_codes"):
            records, _ = protocol.parse_records(response, {identifier})
            if identifier in records:
                transport = protocol.encode(meta.get("text_to_translate", key))
                ok, restored, _ = transport.restore(records[identifier])
                if ok:
                    draft = restored
        pending.setdefault(name, {})[key] = {"reason": failure, "draft": draft}
    return pending


def load_paused_entries(path, translated):
    if path.name != "translation_translated.json":
        return {}
    summary_path = path.with_name("translation_run_summary.json")
    if not summary_path.is_file():
        return {}
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8-sig"))
    except ValueError:
        return {}  # Old versions wrote this optional summary in-place.
    if not summary.get("paused") and not summary.get("pending_review"):
        return {}
    pending = summary.get("pending_review")
    if pending == {}:
        return {}
    source_path = path.parent.parent / "untranslated" / "translation.json"
    if not source_path.is_file():
        return {}
    sources = json.loads(source_path.read_text(encoding="utf-8-sig"))
    if pending is None:
        pending = _legacy_failures(path.parent, summary, sources, translated)
    entries = {}
    for name, items in pending.items():
        for key, failure in items.items():
            meta = sources.get(name, {}).get(key)
            if meta is None or _already_saved(translated, name, key, meta):
                continue
            result = {k: meta[k] for k in ("wolf_codes", "wolf_export_schema") if k in meta}
            result.update(original_marker=meta.get("original_marker", "UnknownMarker"), speaker_id=meta.get("speaker_id"))
            draft = failure.get("draft")
            if not isinstance(draft, str) or not draft.strip() or re.search(r"\[\[(?:C_*\d+|W\d+:[0-9a-f]+)\]\]", draft):
                draft = key
            entries[name, key] = (result, draft, failure["reason"])
    return entries
