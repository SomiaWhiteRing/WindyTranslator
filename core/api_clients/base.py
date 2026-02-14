# core/api_clients/base.py
"""API 客户端抽象基类，定义所有 LLM 客户端的统一接口。

两个核心方法：
- chat_completion(): 统一的文本生成接口
- test_connection(): 连接测试

所有子类必须实现这两个方法，返回值格式统一为元组。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseAPIClient(ABC):
    """LLM API 客户端抽象基类。

    所有 API 客户端（Gemini、DeepSeek/OpenAI 兼容等）都应继承此类，
    确保调用方可以通过统一接口使用不同的后端。
    """

    @abstractmethod
    def chat_completion(
        self,
        model: str,
        messages: list[dict[str, str]],
        **kwargs: Any,
    ) -> tuple[bool, str | None, str | None]:
        """调用 LLM 生成文本。

        Args:
            model: 模型名称（如 "gemini-2.5-pro-preview-05-06"）。
            messages: 消息列表，格式为 [{"role": "user", "content": "..."}]。
            **kwargs: 额外参数（temperature、max_tokens 等），由子类自行处理。

        Returns:
            (success, content, error_message) 三元组：
            - success: API 调用是否成功。
            - content: 成功时返回生成的文本，失败时为 None。
            - error_message: 失败时返回错误信息，成功时为 None。
        """
        ...

    @abstractmethod
    def test_connection(self, model: str) -> tuple[bool, str]:
        """测试与 API 的连接。

        Args:
            model: 用于测试的模型名称。

        Returns:
            (success, message) 二元组：
            - success: 连接测试是否成功。
            - message: 测试结果描述或错误信息。
        """
        ...
