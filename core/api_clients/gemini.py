# core/api_clients/gemini.py
"""Google Gemini API 客户端，基于 google-genai SDK。"""

from __future__ import annotations

import logging
from typing import Any

from google import genai
from google.genai import types  # noqa: F401 — 保留以备 safety_settings 使用
from google.api_core import exceptions as google_exceptions

from core.api_clients.base import BaseAPIClient

log = logging.getLogger(__name__)


class GeminiClient(BaseAPIClient):
    """封装与 Google Gemini API (google-genai SDK) 的交互。"""

    def __init__(self, api_key: str) -> None:
        """初始化 Gemini 客户端。

        Args:
            api_key: Google AI Studio 的 API Key。

        Raises:
            ValueError: 如果 API Key 为空。
            ConnectionError: 如果客户端初始化失败。
        """
        if not api_key:
            raise ValueError("API Key 不能为空。")
        self.api_key = api_key
        try:
            self.client = genai.Client(api_key=self.api_key)
            log.info("Gemini API Client (google-genai SDK) 初始化成功。")
        except Exception as e:
            log.exception(f"初始化 Gemini API Client 失败: {e}")
            raise ConnectionError(f"初始化 Gemini API Client 失败: {e}") from e

    # ------------------------------------------------------------------
    # BaseAPIClient 统一接口
    # ------------------------------------------------------------------

    def chat_completion(
        self,
        model: str,
        messages: list[dict[str, str]],
        **kwargs: Any,
    ) -> tuple[bool, str | None, str | None]:
        """统一的文本生成接口（BaseAPIClient 要求）。

        将 messages 列表中最后一条 user 消息的 content 提取出来，
        委托给 Gemini 原生的 generate_content 方法。

        Args:
            model: 模型名称。
            messages: 消息列表，格式为 [{"role": "user", "content": "..."}]。
            **kwargs: 传递给 generate_content 的额外参数
                      （generation_config、safety_settings 等）。

        Returns:
            (success, content, error_message) 三元组。
        """
        if not messages:
            return False, None, "消息列表不能为空。"

        # 从 messages 中提取 prompt 文本（取最后一条 user 消息）
        prompt = messages[-1].get("content", "")

        # 从 kwargs 中分离 Gemini 特有的参数
        generation_config = kwargs.pop("generation_config", None)
        safety_settings = kwargs.pop("safety_settings", None)

        return self.generate_content(
            model, prompt,
            generation_config=generation_config,
            safety_settings=safety_settings,
        )

    def test_connection(self, model: str) -> tuple[bool, str]:
        """测试与 Gemini API 的连接。

        Args:
            model: 用于测试的模型名称。

        Returns:
            (success, message) 二元组。
        """
        log.info(f"测试与 Gemini API (模型: {model}) 的连接...")
        success, text, error = self.generate_content(model, "你好")
        if success and text:
            msg = "Gemini API 连接测试成功！"
            log.info(msg)
            return True, msg
        elif error:
            msg = f"Gemini API 连接测试失败: {error}"
            log.error(msg)
            return False, msg
        else:
            msg = "Gemini API 连接测试失败: 未收到有效响应。"
            log.error(msg)
            return False, msg

    # ------------------------------------------------------------------
    # Gemini 原生方法（dict_generation.py 直接调用）
    # ------------------------------------------------------------------

    def generate_content(
        self,
        model: str,
        prompt: str,
        generation_config: dict[str, Any] | None = None,
        safety_settings: list[dict[str, str]] | None = None,
    ) -> tuple[bool, str | None, str | None]:
        """调用 Gemini API 生成内容。

        Args:
            model: 模型名称（如 "gemini-2.5-pro-preview-05-06"）。
            prompt: 发送给模型的完整提示文本。
            generation_config: 生成参数（如 temperature、max_output_tokens）。
            safety_settings: 安全设置列表。

        Returns:
            (success, text, error_message) 三元组。
        """
        if not model:
            return False, None, "模型名称不能为空。"
        if not prompt:
            return False, None, "Prompt (contents) 不能为空。"

        api_config_params: dict[str, Any] = {}
        if generation_config:
            api_config_params.update(generation_config)
            log.debug(f"使用 generation_config 参数: {generation_config}")

        if safety_settings:
            api_config_params['safety_settings'] = safety_settings
            log.debug(f"使用 safety_settings 参数: {safety_settings}")

        try:
            log.debug(f"向 Gemini 模型 '{model}' 发送请求...")

            response = self.client.models.generate_content(
                model=model,
                contents=prompt,
                config=api_config_params if api_config_params else None,
            )

            # --- 响应处理 ---
            if response.prompt_feedback and response.prompt_feedback.block_reason:
                reason = response.prompt_feedback.block_reason.name
                error_msg = f"Gemini API 请求被阻止: {reason}"
                log.error(error_msg)
                for rating in response.prompt_feedback.safety_ratings:
                    log.error(f"  - 安全类别: {rating.category.name}, 概率: {rating.probability.name}")
                return False, None, error_msg

            if hasattr(response, 'text') and response.text:
                log.debug("Gemini API 成功返回文本响应。")
                return True, response.text, None

            # 无有效文本的情况
            finish_reason = "未知"
            if hasattr(response, 'candidates') and response.candidates:
                candidate = response.candidates[0]
                if hasattr(candidate, 'finish_reason') and candidate.finish_reason:
                    try:
                        finish_reason = candidate.finish_reason.name
                    except AttributeError:
                        finish_reason = str(candidate.finish_reason)

            parts_exist = hasattr(response, 'parts') and response.parts
            text_is_empty = not (hasattr(response, 'text') and response.text)

            if parts_exist and text_is_empty:
                error_msg = f"Gemini API 调用成功，但返回内容包含 Parts (可能是函数调用)，而非直接文本。完成原因: {finish_reason}"
            else:
                error_msg = f"Gemini API 调用成功，但未返回有效文本。完成原因: {finish_reason}"

            log.warning(error_msg)
            return False, None, error_msg

        except google_exceptions.InvalidArgument as e:
            error_msg = f"Gemini API 参数错误: {e}"
            log.error(error_msg)
            return False, None, error_msg
        except google_exceptions.PermissionDenied as e:
            error_msg = f"Gemini API 权限错误 (检查 API Key?): {e}"
            log.error(error_msg)
            return False, None, error_msg
        except google_exceptions.ResourceExhausted as e:
            error_msg = f"Gemini API 资源耗尽 (检查配额?): {e}"
            log.error(error_msg)
            return False, None, error_msg
        except google_exceptions.GoogleAPIError as e:
            error_msg = f"Gemini API 调用失败: {e}"
            log.exception(error_msg)
            return False, None, error_msg
        except Exception as e:
            error_msg = f"与 Gemini API 交互时发生意外错误: {e}"
            log.exception(error_msg)
            return False, None, error_msg
