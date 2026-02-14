# core/api_clients/deepseek.py
"""OpenAI 兼容 API 客户端（DeepSeek、火山引擎等）。"""

from __future__ import annotations

import logging
from typing import Any

from openai import OpenAI, APIConnectionError, AuthenticationError, RateLimitError, BadRequestError, OpenAIError

from core.api_clients.base import BaseAPIClient

log = logging.getLogger(__name__)


class DeepSeekClient(BaseAPIClient):
    """封装与 DeepSeek (或任何 OpenAI 兼容) API 的交互。"""

    def __init__(self, base_url: str, api_key: str) -> None:
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

    def chat_completion(
        self,
        model: str,
        messages: list[dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> tuple[bool, str | None, str | None]:
        """调用 Chat Completion API。

        Args:
            model: 模型名称。
            messages: 消息列表，格式如 [{"role": "user", "content": "..."}]。
            temperature: 控制随机性的温度值。
            max_tokens: 限制生成的最大 token 数。None 表示由模型决定。
            **kwargs: 其他传递给 `client.chat.completions.create` 的参数。

        Returns:
            (success, content, error_message) 三元组。
        """
        if not model:
            return False, None, "模型名称不能为空。"
        if not messages:
            return False, None, "消息列表不能为空。"

        try:
            log.debug(f"向模型 '{model}' 发送 Chat Completion 请求...")

            response = self.client.chat.completions.create(
                model=model,
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

    def test_connection(self, model: str) -> tuple[bool, str]:
        """尝试与 API 进行简单的连接和认证测试。

        Args:
            model: 用于测试的模型名称。

        Returns:
            (success, message) 二元组。
        """
        log.info(f"测试与 OpenAI 兼容 API (模型: {model}, URL: {self.base_url}) 的连接...")
        test_messages = [{"role": "user", "content": "你好，请回复一个字。"}]
        success, content, error = self.chat_completion(model, test_messages)

        if success and content is not None: # 确保 content 不是空字符串等
            msg = "OpenAI 兼容 API 连接测试成功！"
            log.info(msg)
            return True, msg
        elif error:
            msg = f"OpenAI 兼容 API 连接测试失败: {error}"
            log.error(msg)
            return False, msg
        else: # success is False, but no specific error (e.g., empty response)
            msg = "OpenAI 兼容 API 连接测试失败: 未收到有效响应。"
            log.error(msg)
            return False, msg