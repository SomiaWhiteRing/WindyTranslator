# core/api_clients/deepseek.py
import logging
import re
import json
import httpx
from urllib.parse import urlsplit
from dataclasses import dataclass, field
from typing import Optional
from openai import OpenAI, APIConnectionError, AuthenticationError, RateLimitError, BadRequestError, OpenAIError

log = logging.getLogger(__name__)


def translation_thinking_options(model_name, mode="auto", reasoning_effort=None):
    """DeepSeek V4 defaults to high thinking, which can exhaust text budgets.

    Only known switchable DeepSeek families receive the vendor parameter.
    Explicit reasoning settings take precedence over the translation default.
    """
    if mode not in ("auto", "enabled", "disabled"):
        raise ValueError("thinking_mode 必须为 auto、enabled 或 disabled")
    model = str(model_name).lower().rsplit("/", 1)[-1]
    switchable = re.match(r"deepseek-(?:chat|flash|v4(?:\.\d+)?-(?:flash|pro))(?:$|[-:])", model)
    if mode == "auto":
        if not switchable or reasoning_effort:
            return {}
        mode = "disabled"
    return {"thinking": {"type": mode}}


@dataclass(frozen=True)
class CompletionResult:
    """Per-request metadata; never stored on the shared client instance."""

    content: str = ""
    finish_reason: str = ""
    usage: dict = field(default_factory=dict)
    request_id: str = ""
    response_model: str = ""
    error_kind: str = ""
    error: str = ""
    status_code: Optional[int] = None
    retry_with_stream: bool = False


def _read_unexpected_stream(body, api_key):
    """Some gateways return SSE even when stream=False; retain text and usage."""
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    chunks, usage, request_id, model, finish = [], {}, "", "", ""
    done = False
    try:
        for line in body.splitlines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                done = True
                break
            event = json.loads(payload)
            if not isinstance(event, dict):
                raise ValueError("invalid event")
            usage = event.get("usage") or usage
            request_id = event.get("id") or request_id
            model = event.get("model") or model
            for choice in event.get("choices") or []:
                if choice.get("index", 0) != 0:
                    continue
                delta = choice.get("delta") or choice.get("message") or {}
                content = delta.get("content")
                if isinstance(content, str):
                    chunks.append(content)
                finish = choice.get("finish_reason") or finish
    except (ValueError, TypeError, AttributeError):
        done = False
    text = "".join(chunks)
    if text and finish and done:
        return CompletionResult(content=text, finish_reason=finish, usage=usage,
                                request_id=request_id, response_model=model, status_code=200)
    if finish == "length" and done:
        return CompletionResult(finish_reason=finish, usage=usage, request_id=request_id,
                                response_model=model, status_code=200, error_kind="empty",
                                error="流式响应耗尽输出预算但没有返回译文")
    if usage or chunks or done:
        return CompletionResult(usage=usage, request_id=request_id, response_model=model, status_code=200,
                                error_kind="transient", error="中转站返回不完整流式响应或仅用量，未取得完整译文",
                                retry_with_stream=bool(done and usage and not chunks and not finish))
    detail = body.replace(api_key, "[REDACTED]")[:240]
    return CompletionResult(error_kind="transient", error="接口返回非 JSON 响应: " + detail, status_code=200)


class DeepSeekClient:
    """封装与 DeepSeek (或任何 OpenAI 兼容) API 的交互。"""

    def __init__(self, base_url, api_key):
        """
        初始化 OpenAI 兼容客户端。

        Args:
            base_url (str): API 的基础 URL (例如 "https://api.deepseek.com/v1" 或火山引擎的 URL)。
            api_key (str): API Key。
        """
        if not base_url:
            raise ValueError("API Base URL 不能为空。")
        if not api_key:
            raise ValueError("API Key 不能为空。")

        self.base_url = base_url
        self.api_key = api_key
        try:
            self.client = OpenAI(base_url=self.base_url, api_key=self.api_key)
            log.info(f"OpenAI 兼容客户端初始化成功 (URL: {self.base_url})。")
        except Exception as e:
            log.exception(f"初始化 OpenAI 兼容客户端失败: {e}")
            raise ConnectionError(f"初始化 OpenAI 兼容客户端失败: {e}") from e

    def complete(self, model_name, messages, **kwargs):
        """One HTTP attempt. The translation scheduler owns retry and cost limits."""
        if not model_name or not messages:
            return CompletionResult(error_kind="configuration", error="模型或消息为空")
        timeout = kwargs.pop("timeout", 120)
        stream = kwargs.pop("stream", False)
        thinking_mode = kwargs.pop("thinking_mode", "auto")
        try:
            options = translation_thinking_options(model_name, thinking_mode, kwargs.get("reasoning_effort"))
        except ValueError as error:
            return CompletionResult(error_kind="configuration", error=str(error))
        # Model aliases do not imply that a proxy forwards vendor parameters.
        # The official API documents this switch; proxy users can opt in.
        if thinking_mode == "auto" and urlsplit(getattr(self, "base_url", "")).hostname != "api.deepseek.com":
            options = {}
        if options:
            # Preserve an explicitly supplied vendor body, including thinking.
            kwargs["extra_body"] = {**options, **(kwargs.get("extra_body") or {})}
        try:
            if stream:
                kwargs.setdefault("stream_options", {"include_usage": True})
                # Read the wire format so [DONE], visible text and final billing
                # are checked together. The SDK iterator hides [DONE].
                lines = []
                with self.client.with_options(max_retries=0, timeout=timeout).chat.completions.with_streaming_response.create(
                    model=model_name, messages=messages, stream=True, **kwargs
                ) as response:
                    try:
                        for line in response.iter_lines():
                            lines.append(line)
                    except httpx.TransportError:
                        # Preserve any usage received before a disconnected stream.
                        pass
                return _read_unexpected_stream("\n".join(lines), self.api_key)
            response = self.client.with_options(max_retries=0, timeout=timeout).chat.completions.create(
                model=model_name, messages=messages, stream=False, **kwargs
            )
            if isinstance(response, (str, bytes)):
                return _read_unexpected_stream(response, self.api_key)
            choice = response.choices[0] if response.choices else None
            content = choice.message.content if choice and choice.message else ""
            finish = choice.finish_reason if choice else ""
            usage = response.usage.model_dump() if response.usage else {}
            return CompletionResult(
                content=content or "", finish_reason=finish or "", usage=usage,
                request_id=response.id or "", response_model=getattr(response, "model", "") or "", status_code=200,
                error_kind="" if content else ("refusal" if finish == "content_filter" else "empty"),
                error="" if content else f"接口未返回译文 (finish_reason={finish})",
            )
        except OpenAIError as error:
            status = getattr(error, "status_code", None)
            body = getattr(error, "body", None)
            # Do not log request headers or credentials, including those echoed by a gateway.
            detail = str(body or error).replace(self.api_key, "[REDACTED]")[:1000]
            lower = detail.lower()
            if status in (401, 403):
                kind = "authentication"
            elif status == 402 or any(s in lower for s in ("insufficient_quota", "insufficient balance", "余额不足", "额度不足", "credit balance", "quota_exceeded")):
                kind = "quota"
            elif status == 429 or isinstance(error, (APIConnectionError, RateLimitError)) or (status and status >= 500):
                kind = "transient"
            elif status == 400 and "response_format" in lower:
                kind = "unsupported_format"
            else:
                kind = "configuration"
            return CompletionResult(error_kind=kind, error=detail, status_code=status)

    def chat_completion(self, model_name, messages, temperature=0.7, max_tokens=None, **kwargs):
        """
        调用 Chat Completion API。

        Args:
            model_name (str): 要使用的模型名称。
            messages (list): 消息列表，格式如 [{"role": "user", "content": "..."}]。
            temperature (float, optional): 控制随机性的温度值。默认为 0.7。
            max_tokens (int, optional): 限制生成的最大 token 数。默认为 None (由模型决定)。
            **kwargs: 其他传递给 `client.chat.completions.create` 的参数。

        Returns:
            tuple: (success, result_content, error_message)
                   success (bool): API 调用是否成功并获得有效响应。
                   result_content (str): 如果成功，返回模型生成的消息内容；否则为 None。
                   error_message (str): 如果失败，返回错误信息；否则为 None。
        """
        if not model_name:
            return False, None, "模型名称不能为空。"
        if not messages:
            return False, None, "消息列表不能为空。"

        try:
            log.debug(f"向模型 '{model_name}' 发送 Chat Completion 请求...")
            # log.debug(f"Messages (概览): {[m.get('role', '?') for m in messages]}") # 避免记录完整内容

            response = self.client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs
            )

            if response.choices and response.choices[0].message and response.choices[0].message.content:
                content = response.choices[0].message.content
                log.debug("Chat Completion 成功返回响应内容。")
                return True, content, None
            else:
                # 检查是否有其他完成原因
                finish_reason = "未知"
                if response.choices and response.choices[0].finish_reason:
                    finish_reason = response.choices[0].finish_reason
                error_msg = f"Chat Completion 调用成功，但未返回有效内容。完成原因: {finish_reason}"
                log.warning(response)
                return False, None, error_msg # 标记为失败

        except AuthenticationError as e:
            error_msg = f"API 认证失败 (检查 API Key?): {e}"
            log.error(error_msg)
            return False, None, error_msg
        except RateLimitError as e:
            error_msg = f"API 请求频率超限: {e}"
            log.error(error_msg)
            return False, None, error_msg
        except APIConnectionError as e:
            error_msg = f"无法连接到 API 服务器 ({self.base_url}): {e}"
            log.error(error_msg)
            return False, None, error_msg
        except BadRequestError as e:
            # 通常是请求参数问题，例如 prompt 过长、模型不支持等
            error_msg = f"API 请求无效 (检查参数或 Prompt?): {e}"
            log.error(error_msg)
            return False, None, error_msg
        except OpenAIError as e: # 捕获其他 OpenAI SDK 定义的错误
            error_msg = f"OpenAI API 调用失败: {e}"
            log.exception(error_msg)
            return False, None, error_msg
        except Exception as e:
            error_msg = f"与 OpenAI 兼容 API 交互时发生意外错误: {e}"
            log.exception(error_msg)
            return False, None, error_msg

    def test_connection(self, model_name):
        """Use a small translation to check both connectivity and response format."""
        from core.tasks.translation_protocol import build_messages, encode, parse_records
        source = "ゲームを開始する"
        messages = build_messages([{"text_to_translate": source}], [encode(source)], [], [], [], {})
        result = self.complete(model_name, messages, max_tokens=2048, temperature=0.2)
        if result.retry_with_stream:
            result = self.complete(model_name, messages, max_tokens=2048, temperature=0.2, stream=True)
        records, error = parse_records(result.content, {1})
        if records.get(1) and result.finish_reason != "length":
            return True, "连接和翻译格式检查成功"
        return False, result.error or f"接口响应未通过格式检查: {error or result.finish_reason}"
