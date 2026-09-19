"""Translation requests, bounded retries and immediate checkpoints."""
from collections import Counter, deque
import json
import hashlib
import logging
import os
import threading
import time
import uuid

from core.api_clients.deepseek import CompletionResult
from core.tasks import translation_protocol as protocol
from core.utils import text_processing

log = logging.getLogger(__name__)


class TranslationPaused(RuntimeError):
    pass


class TranslationSession:
    def __init__(self, config, diagnostics_path=None):
        self.config = config
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.reason = ""
        self.stats = Counter()
        self.bad_streak = 0
        self.json_format = True
        self.stream_mode = config.get("stream_mode", "auto")
        if self.stream_mode not in ("auto", "enabled", "disabled"):
            raise ValueError("stream_mode 必须为 auto、enabled 或 disabled")
        self.stream = self.stream_mode == "enabled"
        self.diagnostics_path = diagnostics_path
        self.run_id = uuid.uuid4().hex[:12]
        self.fragments = {}
        self.fragment_path = os.path.join(os.path.dirname(diagnostics_path), "translation_fragments.jsonl") if diagnostics_path else None
        if self.fragment_path and os.path.exists(self.fragment_path):
            # Decode one complete line at a time; a torn UTF-8 character in the
            # last line must not prevent loading earlier valid fragments.
            with open(self.fragment_path, "rb") as source:
                for line in source:
                    try:
                        row = json.loads(line)
                        if isinstance(row.get("text"), str):
                            self.fragments[row["key"]] = row["text"]
                    except (ValueError, KeyError, TypeError):
                        # A crash may leave only the final journal line incomplete.
                        continue

    def save_fragment(self, key, text):
        with self.lock:
            if self.fragment_path:
                with open(self.fragment_path, "a+b") as output:
                    # A killed process may leave an unterminated JSON line. Keep
                    # it separate so it cannot swallow the next paid fragment.
                    output.seek(0, os.SEEK_END)
                    if output.tell():
                        output.seek(-1, os.SEEK_END)
                        if output.read(1) != b"\n":
                            output.write(b"\n")
                    output.write((json.dumps({"key": key, "text": text}, ensure_ascii=False) + "\n").encode("utf-8"))
                    output.flush()
                    os.fsync(output.fileno())
            self.fragments[key] = text

    def pause(self, reason):
        with self.lock:
            if not self.stop.is_set():
                self.reason = reason
            self.stop.set()

    def enable_stream(self):
        with self.lock:
            if not self.stream:
                self.stream = True
                self.stats["stream_switches"] += 1

    def begin_request(self):
        # Count requests before dispatch, including those still in flight.
        with self.lock:
            if self.stop.is_set():
                raise TranslationPaused(self.reason)
            if self.stats["requests"] >= int(self.config.get("max_task_requests", 20000)):
                self.reason = "达到本轮请求次数上限，已保存成功译文，可调整上限后续跑"
                self.stop.set()
                raise TranslationPaused(self.reason)
            self.stats["requests"] += 1
            return self.stats["requests"]

    def record(self, response, request_number, file_name, count, recovered, issues, sources=None, stream=False):
        usage = response.usage or {}
        prompt = usage.get("prompt_tokens", 0) or 0
        completion = usage.get("completion_tokens", 0) or 0
        reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0) or 0
        total = usage.get("total_tokens", 0) or prompt + completion
        with self.lock:
            self.stats["input_tokens"] += prompt
            self.stats["output_tokens"] += completion
            self.stats["reasoning_tokens"] += reasoning
            self.stats["total_tokens"] += total
            self.stats["missing_usage"] += not bool(total)
            self.stats["recovered"] += recovered
            self.stats[response.error_kind or response.finish_reason or "unknown"] += 1
            if recovered:
                self.bad_streak = 0
            elif response.error_kind != "unsupported_format":
                self.bad_streak += 1
            if self.bad_streak >= int(self.config.get("max_consecutive_failures", 12)):
                self.reason = "连续请求未取得有效译文，已暂停以避免继续消耗额度；检查接口或降低批量后续跑"
                self.stop.set()
            row = dict(run_id=self.run_id, request=request_number, request_id=response.request_id,
                       stream=stream,
                       response_model=response.response_model,
                       file=file_name, items=count, recovered=recovered, finish_reason=response.finish_reason,
                       error_kind=response.error_kind, error=response.error, usage=usage,
                       content_chars=len(response.content), issues=issues)
            if issues or response.error_kind or response.finish_reason == "length":
                row["response"] = response.content  # full failure evidence, no credentials
                if sources:
                    row["sources"] = [{"id": i + 1, "original": item["original_json_key"]} for i, item in enumerate(sources)]
            if self.diagnostics_path:
                try:
                    with open(self.diagnostics_path, "a", encoding="utf-8") as output:
                        output.write(json.dumps(row, ensure_ascii=False) + "\n")
                except OSError:
                    log.exception("请求诊断写入失败；继续保存已经取得的译文")
        log.info("翻译请求 #%s: %s 条，保留 %s 条，结束=%s，token=%s/%s，错误=%s",
                 request_number, count, recovered, response.finish_reason, prompt, completion, response.error_kind or "无")


def validate_item(item, transport, raw, config, wolf_mappings=()):
    # Import lazily: these helpers are also used by legacy line translation.
    from core.tasks.translate import _restore_wolf_transport_masks
    ok, restored, reason = transport.restore(raw)
    if ok:
        ok, restored, reason = _restore_wolf_transport_masks(restored, wolf_mappings)
    if not ok:
        return None, reason
    source = item["text_to_translate"]
    if source.strip() and not restored.strip():
        return None, "译文为空"
    repaired = text_processing.repair_translation_format(source, restored)
    processed = text_processing.post_process_translation(
        repaired, source,
        apply_gbk_compatibility=config.get("_apply_gbk_compatibility_postprocess", True),
        process_quotes=not config.get("_fragment_mode", False),
    )
    # Post-processing historically strips final empty lines. Restore only empty
    # suffix lines explicitly present in the source; never invent text.
    trailing = len(source) - len(source.rstrip("\n"))
    if trailing and processed.count("\n") < source.count("\n"):
        missing = source.count("\n") - processed.count("\n")
        if missing <= trailing:
            processed += "\n" * missing
    if source.count("\n") != processed.count("\n"):
        return None, f"译文行数与原文不一致：必须为 {source.count(chr(10)) + 1} 行（含空行），实际为 {processed.count(chr(10)) + 1} 行；请逐行对应，跨行的句子也不能合并"
    from core.tasks.translate import _protected_literals_for_text
    literals = _protected_literals_for_text(source, config.get("_protected_literals") or ())
    target = str(config.get("target_language", "简体中文")).lower()
    allow_kana = "日" in target or target.startswith("ja")
    ok, reason = text_processing.validate_translation(source, repaired, processed, allowed_source_literals=literals, allow_japanese=allow_kana)
    validator = config.get("_translation_validator")
    if ok and validator:
        ok, reason = validator(source, processed)
    if not ok:
        return None, reason
    term_issue = protocol.article_term_issue(source, processed, config.get("_article_terms", ()))
    if term_issue:
        return None, term_issue
    result = {"text": processed, "status": "success", "failure_context": None,
              "original_marker": item.get("original_marker"), "speaker_id": item.get("speaker_id")}
    for key in ("wolf_codes", "wolf_export_schema"):
        if key in item:
            result[key] = item[key]
    return result, ""


def translate_batch(items, context, characters, entities, client, config, file_name=None):
    from core.tasks.translate import _mask_wolf_transport, _protected_literals_for_text
    session = config.get("_translation_session") or TranslationSession(config)
    checkpoint = config.get("_checkpoint_translation")
    completed = {}
    queue = deque([items])
    max_requests = max(1, int(config.get("max_requests_per_batch", 16)))
    request_budget = config.get("_batch_request_budget") or {"used": 0, "limit": max_requests}
    attempts = Counter()
    batch_attempts = Counter()
    retry_failed_only = config.get("retry_failed_items_only", False)
    reasoning_exhaustions = Counter()
    failures = {}
    transient_retries = 0
    while queue:
        if session.stop.is_set():
            raise TranslationPaused(session.reason)
        pending = queue.popleft()
        if not config.get("_fragment_mode"):
            limit = int(config.get("max_segment_chars", 1200))
            single_limit = int(config.get("max_single_item_chars") or max(2400, limit * 2))
            long_index = next((index for index, item in enumerate(pending)
                               if len(item["text_to_translate"]) > single_limit), None)
            if long_index is not None and len(pending) > 1:
                # Isolate long items before any request; a context-size rejection
                # stops the task before content retries can split this batch.
                groups = (pending[:long_index], pending[long_index:long_index + 1],
                          pending[long_index + 1:])
                queue.extendleft(group for group in reversed(groups) if group)
                continue
            if long_index is not None:
                item = pending[0]
                result = translate_long_item(item, context, characters, entities, client,
                                             {**config, "_translation_session": session}, file_name)
                completed[item["original_json_key"]] = result
                if checkpoint:
                    checkpoint(file_name, {item["original_json_key"]: result})
                continue
        split = list(protocol.pack_batches(pending, int(config.get("batch_size", 32))))
        if len(split) > 1:
            queue.extendleft(reversed(split))
            continue
        batch_key = tuple(item["original_json_key"] for item in pending)
        if request_budget["used"] >= request_budget["limit"]:
            session.pause("批次达到请求预算，成功译文已保存；剩余条目可续跑")
            raise TranslationPaused(session.reason)
        transports, mappings = [], []
        for item in pending:
            source = item["text_to_translate"]
            masked, mapping = _mask_wolf_transport(source, config.get("_optional_wolf_transport_tags", ())) if config.get("_mask_wolf_transport") else (source, ())
            literals = _protected_literals_for_text(source, config.get("_protected_literals") or ())
            transports.append(protocol.encode(masked, config.get("_control_code_profile"), literals))
            mappings.append(mapping)
        feedback = [{"id": i + 1, **failures[item["original_json_key"]]}
                    for i, item in enumerate(pending) if item["original_json_key"] in failures] if retry_failed_only else []
        messages = protocol.build_messages(pending, transports, context, characters, entities, config, feedback)
        request_number = session.begin_request()
        request_budget["used"] += 1
        kwargs = dict(temperature=config.get("temperature", 0.7), timeout=config.get("request_timeout", 120), stream=session.stream)
        if session.json_format:
            kwargs["response_format"] = {"type": "json_object"}
        if config.get("reasoning_effort"):
            kwargs["reasoning_effort"] = config["reasoning_effort"]
        kwargs["thinking_mode"] = config.get("thinking_mode", "auto")
        try:
            response = client.complete(config.get("model"), messages, **kwargs)
        except Exception:
            # Unknown client failures are not safe to silently retry (may already be billed).
            session.record(CompletionResult(error_kind="unknown", error="接口调用发生未分类异常"),
                           request_number, file_name, len(pending), 0, {}, pending, stream=kwargs["stream"])
            session.pause("接口调用发生未分类异常，已停止后续请求")
            raise
        if response.error_kind in ("authentication", "quota", "configuration"):
            session.record(response, request_number, file_name, len(pending), 0, {}, stream=kwargs["stream"])
            session.pause(f"接口错误 ({response.error_kind}): {response.error}")
            raise TranslationPaused(session.reason)
        if response.error_kind == "unsupported_format":
            session.record(response, request_number, file_name, len(pending), 0, {}, stream=kwargs["stream"])
            # Other in-flight requests may have already disabled the shared
            # option. Only this request's parameters prove it was a retry.
            if "response_format" not in kwargs:
                session.pause("接口在取消 response_format 后仍拒绝请求，请检查接口配置")
                raise TranslationPaused(session.reason)
            with session.lock:
                session.json_format = False
            queue.appendleft(pending)
            continue
        if response.error_kind == "transient":
            session.record(response, request_number, file_name, len(pending), 0, {}, stream=kwargs["stream"])
            if response.retry_with_stream and not kwargs["stream"] and session.stream_mode == "auto":
                # One task-wide mode change, billed and bounded like every request.
                # Other already-running non-streaming requests may finish later.
                session.enable_stream()
                queue.appendleft(pending)
                continue
            transient_retries += 1
            if transient_retries > max(1, int(config.get("max_retries", 1))):
                session.pause("接口持续返回临时错误或不完整响应，已保存译文并暂停，可稍后续跑")
                raise TranslationPaused(session.reason)
            session.stop.wait(min(8, 2 ** transient_retries))
            queue.appendleft(pending)
            continue
        transient_retries = 0
        batch_attempts[batch_key] += 1
        records, parse_error = protocol.parse_records(response.content, set(range(1, len(pending) + 1)))
        successes, remainder, issues = {}, [], {}
        for i, item in enumerate(pending):
            key = item["original_json_key"]
            attempts[key] += 1
            result, reason = validate_item(item, transports[i], records[i + 1], config, mappings[i]) if i + 1 in records else (None, parse_error or response.error or "缺少该条译文")
            if result is not None:
                successes[key] = result
            else:
                failures[key] = {"reason": reason}
                if i + 1 in records:
                    failures[key]["previous_translation"] = records[i + 1]
                issues[i + 1] = reason
                remainder.append(item)
        # Normal mode accepts a batch together; the experimental mode commits
        # its successful subset before sending only the failed items again.
        accepted = successes if retry_failed_only or not remainder else {}
        session.record(response, request_number, file_name, len(pending), len(accepted), issues, pending, stream=kwargs["stream"])
        completed.update(accepted)
        if accepted and checkpoint:
            checkpoint(file_name, accepted)
        usage = response.usage or {}
        output_tokens = usage.get("completion_tokens", 0) or 0
        reasoning_tokens = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0) or 0
        if response.finish_reason == "length" and not response.content.strip() and output_tokens and reasoning_tokens >= output_tokens:
            for item in remainder:
                reasoning_exhaustions[item["original_json_key"]] += 1
            if any(reasoning_exhaustions[item["original_json_key"]] >= 2 for item in remainder):
                session.pause("同一文本两次耗尽输出预算于模型思考，未返回译文；已暂停以节省额度，请降低思考强度或检查中转站参数支持")
                raise TranslationPaused(session.reason)
        if not remainder:
            continue
        max_retries = max(0, int(config.get("max_retries", 1)))
        if retry_failed_only:
            exhausted = [item for item in remainder if attempts[item["original_json_key"]] >= max(2, max_retries + 2)]
            retry_items = remainder
        else:
            if batch_attempts[batch_key] <= max_retries:
                queue.appendleft(pending)
                continue
            exhausted = remainder if len(pending) == 1 else []
            retry_items = pending
        if exhausted:
            session.pause(f"{len(exhausted)} 条文本多次校验未通过，已保存其余译文。详情见请求诊断日志，修正后可续跑")
            raise TranslationPaused(session.reason)
        # Retry selection is independent of transport compatibility and budgets.
        if len(retry_items) > 1:
            mid = (len(retry_items) + 1) // 2
            queue.appendleft(retry_items[mid:])
            queue.appendleft(retry_items[:mid])
            with session.lock:
                session.stats["splits"] += 1
        else:
            queue.appendleft(retry_items)
    return {item["original_json_key"]: completed[item["original_json_key"]] for item in items}


def translate_long_item(item, context, characters, entities, client, config, file_name):
    """Translate long articles in recoverable fragments, validate the reassembly."""
    from core.tasks.translate import _protected_literals_for_text
    from core.utils import control_tokens
    session = config["_translation_session"]
    source = item["text_to_translate"]
    literals = _protected_literals_for_text(source, config.get("_protected_literals") or ())
    parts = protocol.split_long_text(source, int(config.get("max_segment_chars", 1200)), config.get("_control_code_profile"), literals)
    budget = {"used": 0, "limit": max(int(config.get("max_requests_per_batch", 16)), len(parts) * 3)}
    identity = json.dumps([file_name, item["original_json_key"], config.get("source_language"), config.get("target_language"), config.get("_control_code_profile").summary if config.get("_control_code_profile") else "generic"], ensure_ascii=False)
    pieces = []
    article_terms = {}
    context_count = max(0, int(config.get("context_lines", 4)))
    known_terms = {row.get("原文") for row in [*characters, *entities]}
    for index, (part, separator) in enumerate(parts):
        fragment_identity = identity + str(index) + part
        if any(part.count(left) != part.count(right) for left, right in (("「", "」"), ("『", "』"))):
            # Old checkpoints may contain a closing quote added at a split.
            # Invalidate those fragments without discarding unrelated paid work.
            fragment_identity += "\0quotes_after_reassembly_v1"
        key = hashlib.sha256(fragment_identity.encode("utf-8")).hexdigest()
        fragment = {**item, "original_json_key": part, "text_to_translate": part}
        cached = session.fragments.get(key)
        if cached is not None:
            ok, reason = control_tokens.validate_restored_text(part, cached, profile=config.get("_control_code_profile"))
            if not ok or protocol.article_term_issue(part, cached, article_terms.values()):
                cached = None
        if cached is None:
            def save(_file, results, cache_key=key):
                session.save_fragment(cache_key, results[part]["text"])
            fragment_config = {**config, "_fragment_mode": True, "_batch_request_budget": budget, "_checkpoint_translation": save,
                               "_article_terms": list(article_terms.values())}
            translated = translate_batch([fragment], context, [*article_terms.values(), *characters], entities, client, fragment_config, file_name)
            cached = translated[part]["text"]
        pieces.append(cached + separator)
        for entry in protocol.quoted_term_pairs(part, cached):
            if entry["原文"] not in known_terms and len(article_terms) < 64:
                article_terms.setdefault(entry["原文"], entry)
        context = [*context, fragment][-context_count:] if context_count else []
    joined = text_processing.repair_translation_quotes("".join(pieces), source)
    # Quotes can span fragments. Repair them once against the entire source,
    # without repeating the other cleanup that each fragment already received.
    ok, reason = control_tokens.validate_restored_text(source, joined, profile=config.get("_control_code_profile"))
    if ok and source.count("\n") != joined.count("\n"):
        ok, reason = False, "长文本拼接后的行数不一致"
    if ok and config.get("_translation_validator"):
        ok, reason = config["_translation_validator"](source, joined)
    if not ok:
        session.pause("长文本拼接校验失败: " + reason)
        raise TranslationPaused(session.reason)
    result = {"text": joined, "status": "success", "failure_context": None,
              "original_marker": item.get("original_marker"), "speaker_id": item.get("speaker_id")}
    for name in ("wolf_codes", "wolf_export_schema"):
        if name in item:
            result[name] = item[name]
    return result
