# core/api_clients/deepseek.py
import logging
import json # 导入 json 用于更好地记录错误
from openai import OpenAI, APIConnectionError, AuthenticationError, RateLimitError, BadRequestError, OpenAIError
from pydantic import BaseModel, ValidationError # 导入 Pydantic
from typing import Type, Optional, Any # 导入类型提示

log = logging.getLogger(__name__)

class DeepSeekClient:
    """封装与 DeepSeek (或任何 OpenAI 兼容) API 的交互。"""

    def __init__(self, base_url, api_key):
        """
        初始化 OpenAI 兼容客户端。

        Args:
            base_url (str): API 的基础 URL (例如 "https://api.deepseek.com/v1" 或 xAI 的 URL)。
            api_key (str): API Key。
        """
        if not base_url:
            raise ValueError("API Base URL 不能为空。")
        if not api_key:
            raise ValueError("API Key 不能为空。")

        self.base_url = base_url
        self.api_key = api_key
        try:
            # 注意：使用 xai 的 api 时，需要用特定的 client，或者确认 OpenAI 的 client 是否兼容
            # 这里我们假设 OpenAI client 兼容，但如果遇到问题，可能需要 xai 提供的特定 SDK
            self.client = OpenAI(base_url=self.base_url, api_key=self.api_key)
            log.info(f"OpenAI 兼容客户端初始化成功 (URL: {self.base_url})。")
        except Exception as e:
            log.exception(f"初始化 OpenAI 兼容客户端失败: {e}")
            raise ConnectionError(f"初始化 OpenAI 兼容客户端失败: {e}") from e

    def chat_completion(self, model_name, messages, temperature=0.7, max_tokens=None, **kwargs):
        """
        调用【标准】Chat Completion API（用于非结构化输出或不支持 parse 的模型）。

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
            log.debug(f"向模型 '{model_name}' 发送【标准】Chat Completion 请求...")
            # --- 日志记录请求 ---
            log.info(f"--- API Request (Standard) ---")
            log.info(f"Model: {model_name}")
            log.info(f"API Messages:\n{json.dumps(messages, indent=2, ensure_ascii=False)}")
            log.info(f"API Kwargs: {json.dumps({'temperature': temperature, 'max_tokens': max_tokens, **kwargs}, ensure_ascii=False)}")
            log.info(f"--- End API Request ---")
            # --- END 日志记录请求 ---

            response = self.client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs
            )

            # --- 日志记录响应 ---
            log.info(f"--- API Response (Standard) ---")
            log.info(f"Raw Response Object (Type): {type(response)}")
            try:
                 # 尝试将响应对象序列化为 JSON 字符串以便记录
                 response_dict = response.model_dump() # Pydantic V2+
                 log.info(f"Raw Response Content (JSON):\n{json.dumps(response_dict, indent=2, ensure_ascii=False)}")
            except Exception as log_err:
                 log.warning(f"无法将响应序列化为 JSON 进行日志记录: {log_err}")
                 log.info(f"Raw Response Content (repr): {repr(response)}")
            # --- END 日志记录响应 ---


            if response.choices and response.choices[0].message and response.choices[0].message.content:
                content = response.choices[0].message.content
                log.debug("【标准】Chat Completion 成功返回响应内容。")
                 # --- 日志记录响应内容 ---
                log.info(f"Extracted Content: {content[:200]}...") # 记录部分提取内容
                log.info(f"--- End API Response ---")
                # --- END 日志记录响应内容 ---
                return True, content, None
            else:
                finish_reason = "未知"
                if response.choices and response.choices[0].finish_reason:
                    finish_reason = response.choices[0].finish_reason
                error_msg = f"【标准】Chat Completion 调用成功，但未返回有效内容。完成原因: {finish_reason}"
                log.warning(error_msg)
                 # --- 日志记录响应内容 ---
                log.info(f"Failure Reason: {error_msg}")
                log.info(f"--- End API Response ---")
                # --- END 日志记录响应内容 ---
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
            error_msg = f"API 请求无效 (检查参数或 Prompt?): {e}"
            log.error(error_msg)
            return False, None, error_msg
        except OpenAIError as e:
            error_msg = f"OpenAI API 调用失败: {e}"
            log.exception(error_msg)
            return False, None, error_msg
        except Exception as e:
            error_msg = f"与 OpenAI 兼容 API 交互时发生意外错误: {e}"
            log.exception(error_msg)
            return False, None, error_msg

    def chat_completion_structured(
        self,
        model_name: str,
        messages: list,
        response_format: Type[BaseModel], # 指定 Pydantic 模型作为响应格式
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs
    ) -> tuple[bool, Optional[BaseModel], Optional[str]]:
        """
        调用支持结构化输出的 Chat Completion API (例如 Grok 的 parse 方法)。

        Args:
            model_name (str): 要使用的模型名称 (例如 "grok-3")。
            messages (list): 消息列表。
            response_format (Type[BaseModel]): 用于解析响应的 Pydantic 模型类。
            temperature (float, optional): 温度值。默认为 0.7。
            max_tokens (int, optional): 最大 token 数。默认为 None。
            **kwargs: 其他传递给 `client.beta.chat.completions.parse` 的参数。

        Returns:
            tuple: (success, parsed_object, error_message)
                   success (bool): API 调用和解析是否成功。
                   parsed_object (BaseModel | None): 如果成功，返回解析后的 Pydantic 对象实例；否则为 None。
                   error_message (str): 如果失败，返回错误信息；否则为 None。
        """
        if not model_name:
            return False, None, "模型名称不能为空。"
        if not messages:
            return False, None, "消息列表不能为空。"
        if not response_format or not issubclass(response_format, BaseModel):
            return False, None, "response_format 必须是一个 Pydantic BaseModel 类。"

        try:
            log.debug(f"向模型 '{model_name}' 发送【结构化】Chat Completion 请求 (Schema: {response_format.__name__})...")
            # --- 日志记录请求 ---
            log.info(f"--- API Request (Structured) ---")
            log.info(f"Model: {model_name}")
            log.info(f"Response Format Schema: {response_format.__name__}")
            log.info(f"API Messages:\n{json.dumps(messages, indent=2, ensure_ascii=False)}")
            log.info(f"API Kwargs: {json.dumps({'temperature': temperature, 'max_tokens': max_tokens, **kwargs}, ensure_ascii=False)}")
            log.info(f"--- End API Request ---")
            # --- END 日志记录请求 ---

            # 使用 client.beta.chat.completions.parse
            completion = self.client.beta.chat.completions.parse(
                model=model_name,
                messages=messages,
                response_format=response_format, # 传入 Pydantic 模型
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs
            )

            # --- 日志记录响应 ---
            log.info(f"--- API Response (Structured) ---")
            log.info(f"Raw Response Object (Type): {type(completion)}")
            try:
                 # 尝试将响应对象序列化为 JSON 字符串以便记录
                 response_dict = completion.model_dump() # Pydantic V2+
                 log.info(f"Raw Response Content (JSON):\n{json.dumps(response_dict, indent=2, ensure_ascii=False)}")
            except Exception as log_err:
                 log.warning(f"无法将结构化响应序列化为 JSON 进行日志记录: {log_err}")
                 log.info(f"Raw Response Content (repr): {repr(completion)}")
            # --- END 日志记录响应 ---

            # Grok 的 parse 方法直接返回带 .parsed 属性的对象
            if completion.choices and completion.choices[0].message and hasattr(completion.choices[0].message, 'parsed'):
                parsed_object = completion.choices[0].message.parsed
                # 验证返回的对象是否是预期模型的实例
                if isinstance(parsed_object, response_format):
                    log.debug(f"【结构化】Chat Completion 成功返回并解析为 '{response_format.__name__}' 对象。")
                     # --- 日志记录响应内容 ---
                    try:
                        parsed_dict = parsed_object.model_dump()
                        log.info(f"Parsed Object (JSON):\n{json.dumps(parsed_dict, indent=2, ensure_ascii=False)}")
                    except Exception as log_err:
                        log.warning(f"无法将解析后的 Pydantic 对象序列化为 JSON 进行日志记录: {log_err}")
                        log.info(f"Parsed Object (repr): {repr(parsed_object)}")
                    log.info(f"--- End API Response ---")
                     # --- END 日志记录响应内容 ---
                    return True, parsed_object, None
                else:
                    error_msg = f"【结构化】Chat Completion 成功，但解析得到的对象类型 ({type(parsed_object).__name__}) 与预期的架构 ({response_format.__name__}) 不匹配。"
                    log.error(error_msg)
                    # --- 日志记录响应内容 ---
                    log.info(f"Failure Reason: {error_msg}")
                    log.info(f"Parsed Object (repr): {repr(parsed_object)}")
                    log.info(f"--- End API Response ---")
                    # --- END 日志记录响应内容 ---
                    return False, None, error_msg
            else:
                # 检查是否有其他完成原因
                finish_reason = "未知"
                if completion.choices and completion.choices[0].finish_reason:
                    finish_reason = completion.choices[0].finish_reason
                error_msg = f"【结构化】Chat Completion 调用成功，但未返回有效或可解析的内容。完成原因: {finish_reason}"
                log.warning(error_msg)
                # --- 日志记录响应内容 ---
                log.info(f"Failure Reason: {error_msg}")
                log.info(f"--- End API Response ---")
                # --- END 日志记录响应内容 ---
                return False, None, error_msg

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
            # 结构化输出模式下，如果模型无法生成符合 schema 的 JSON，也可能报此错误
            error_msg = f"API 请求无效 (检查参数、Prompt 或模型是否能生成目标 Schema?): {e}"
            log.error(error_msg)
            # 记录更详细的错误信息（如果可用）
            try:
                 if hasattr(e, 'response') and e.response and hasattr(e.response, 'text'):
                     log.error(f"Bad Request Response Body: {e.response.text}")
                 elif hasattr(e, 'body') and e.body: # 有些 openai 版本可能用 body
                     log.error(f"Bad Request Body: {e.body}")
            except Exception:
                 pass # 忽略记录详细错误时的错误
            return False, None, error_msg
        except ValidationError as e: # 捕获 Pydantic 验证错误（理论上 Grok parse 会保证，但以防万一）
             error_msg = f"API 响应未能通过 Pydantic 架构 '{response_format.__name__}' 的验证: {e}"
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
        """
        尝试与 API 进行简单的连接和认证测试（使用标准 chat_completion）。

        Args:
            model_name (str): 用于测试的模型名称。

        Returns:
            tuple: (success, message)
                   success (bool): 连接和认证测试是否成功。
                   message (str): 测试结果或错误信息。
        """
        log.info(f"测试与 OpenAI 兼容 API (模型: {model_name}, URL: {self.base_url}) 的连接...")
        # 使用一个非常简单的请求
        test_messages = [{"role": "user", "content": "Hello"}] # 使用英文，更通用
        # 使用标准方法测试基础连接
        success, content, error = self.chat_completion(model_name, test_messages, max_tokens=10)

        if success and content is not None: # 确保 content 不是空字符串等
            msg = "OpenAI 兼容 API 连接测试成功！"
            log.info(msg)
            return True, msg
        elif error:
            # 如果是 BadRequestError，并且模型支持结构化，可能提示用户尝试结构化测试
            if "BadRequestError" in error and "grok" in model_name: # 简单判断
                 error += " (如果模型支持结构化输出，请确保测试时使用的 Prompt/参数有效，或单独测试结构化输出功能)"
            msg = f"OpenAI 兼容 API 连接测试失败: {error}"
            log.error(msg)
            return False, msg
        else: # success is False, but no specific error (e.g., empty response)
            msg = "OpenAI 兼容 API 连接测试失败: 未收到有效响应。"
            log.error(msg)
            return False, msg

    # 可以选择性地添加一个专门测试结构化输出的方法
    def test_structured_connection(self, model_name: str, test_schema: Type[BaseModel]):
        """
        尝试与 API 进行结构化输出的连接和认证测试。

        Args:
            model_name (str): 用于测试的模型名称 (应支持结构化输出)。
            test_schema (Type[BaseModel]): 用于测试的简单 Pydantic 模型。

        Returns:
            tuple: (success, message)
        """
        log.info(f"测试与 OpenAI 兼容 API 的【结构化输出】(模型: {model_name}, Schema: {test_schema.__name__}, URL: {self.base_url})...")
        # 使用一个要求简单 JSON 输出的 Prompt
        test_messages = [
            {"role": "system", "content": f"Extract the requested information into a JSON object matching the {test_schema.__name__} schema."},
            {"role": "user", "content": "The user's name is Bob and age is 30."}
        ]
        success, parsed_obj, error = self.chat_completion_structured(
            model_name,
            test_messages,
            response_format=test_schema,
            max_tokens=50 # 限制 token
        )

        if success and parsed_obj is not None:
            msg = f"OpenAI 兼容 API 【结构化输出】连接测试成功！Schema: {test_schema.__name__}"
            log.info(msg)
            return True, msg
        elif error:
            msg = f"OpenAI 兼容 API 【结构化输出】连接测试失败: {error}"
            log.error(msg)
            return False, msg
        else: # success is False, but no specific error
            msg = f"OpenAI 兼容 API 【结构化输出】连接测试失败: 未收到有效响应或解析失败。"
            log.error(msg)
            return False, msg