# core/api_clients/gemini.py
import logging
from google import genai
from google.api_core import exceptions as google_exceptions # 导入 google api 核心异常

log = logging.getLogger(__name__)

class GeminiClient:
    """封装与 Google Gemini API 的交互。"""

    def __init__(self, api_key):
        """
        初始化 Gemini 客户端。

        Args:
            api_key (str): Google AI Studio 的 API Key。
        """
        if not api_key:
            raise ValueError("API Key 不能为空。")
        self.api_key = api_key
        try:
            # 配置 API 客户端
            genai.configure(api_key=self.api_key)
            log.info("Gemini API Client 配置成功。")
            # 可以选择在这里创建一个 model 实例，如果模型固定的话
            # self.model = genai.GenerativeModel(model_name)
        except Exception as e:
            log.exception(f"配置 Gemini API Client 失败: {e}")
            # 初始化失败时抛出异常，让调用者知道无法使用
            raise ConnectionError(f"配置 Gemini API Client 失败: {e}") from e

    def generate_content(self, model_name, prompt, generation_config=None, safety_settings=None):
        """
        调用 Gemini API 生成内容。

        Args:
            model_name (str): 要使用的模型名称 (例如 "gemini-1.5-pro-latest")。
            prompt (str): 发送给模型的完整提示文本。
            generation_config (dict, optional): 生成参数 (如 temperature, max_output_tokens)。
                                                例如: {"temperature": 0.7, "max_output_tokens": 4000}
            safety_settings (list, optional): 安全设置。
                                              例如: [
                                                  {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
                                                  # ... 其他设置
                                              ]

        Returns:
            tuple: (success, result_text, error_message)
                   success (bool): API 调用是否成功并获得有效响应。
                   result_text (str): 如果成功，返回模型生成的文本；否则为 None。
                   error_message (str): 如果失败，返回错误信息；否则为 None。
        """
        if not model_name:
            return False, None, "模型名称不能为空。"
        if not prompt:
            return False, None, "Prompt 不能为空。"

        try:
            log.debug(f"向 Gemini 模型 '{model_name}' 发送请求...")
            # log.debug(f"Prompt (前 100 字符): {prompt[:100]}...") # 避免记录完整敏感信息

            model = genai.GenerativeModel(model_name)

            # --- 执行 API 调用 ---
            response = model.generate_content(
                prompt,
                generation_config=generation_config,
                safety_settings=safety_settings
            )

            # --- 处理响应 ---
            # 检查是否有 block reason
            if response.prompt_feedback and response.prompt_feedback.block_reason:
                reason = response.prompt_feedback.block_reason.name
                error_msg = f"Gemini API 请求被阻止: {reason}"
                log.error(error_msg)
                # 检查是否有安全评分信息可以提供更多上下文
                for rating in response.prompt_feedback.safety_ratings:
                    log.error(f"  - 安全类别: {rating.category.name}, 概率: {rating.probability.name}")
                return False, None, error_msg

            # 检查是否有响应文本
            if response.text:
                log.debug("Gemini API 成功返回文本响应。")
                return True, response.text, None
            else:
                # 可能成功但没有文本，或者有其他完成原因
                finish_reason = "未知"
                if response.candidates and response.candidates[0].finish_reason:
                    finish_reason = response.candidates[0].finish_reason.name
                error_msg = f"Gemini API 调用成功，但未返回文本。完成原因: {finish_reason}"
                log.warning(error_msg)
                # 这种情况不一定算完全失败，但对于期望文本的任务来说是有问题的
                return False, None, error_msg # 标记为失败，因为没有得到期望的文本

        except google_exceptions.InvalidArgument as e:
            # 特别处理常见的参数错误
            error_msg = f"Gemini API 参数错误: {e}"
            log.error(error_msg)
            return False, None, error_msg
        except google_exceptions.PermissionDenied as e:
            # 处理权限或API Key错误
            error_msg = f"Gemini API 权限错误 (检查 API Key?): {e}"
            log.error(error_msg)
            return False, None, error_msg
        except google_exceptions.ResourceExhausted as e:
            # 处理配额问题
            error_msg = f"Gemini API 资源耗尽 (检查配额?): {e}"
            log.error(error_msg)
            return False, None, error_msg
        except google_exceptions.GoogleAPIError as e:
            # 捕获其他 Google API 错误
            error_msg = f"Gemini API 调用失败: {e}"
            log.exception(error_msg) # 记录堆栈跟踪
            return False, None, error_msg
        except Exception as e:
            # 捕获其他意外错误
            error_msg = f"与 Gemini API 交互时发生意外错误: {e}"
            log.exception(error_msg)
            return False, None, error_msg

    def test_connection(self, model_name):
        """
        尝试与 Gemini API 进行简单的连接测试。

        Args:
            model_name (str): 用于测试的模型名称。

        Returns:
            tuple: (success, message)
                   success (bool): 连接测试是否成功。
                   message (str): 测试结果或错误信息。
        """
        log.info(f"测试与 Gemini API (模型: {model_name}) 的连接...")
        # 使用一个非常简单的 prompt
        success, text, error = self.generate_content(model_name, "你好")
        if success and text:
            msg = "Gemini API 连接测试成功！"
            log.info(msg)
            return True, msg
        elif error:
            msg = f"Gemini API 连接测试失败: {error}"
            log.error(msg)
            return False, msg
        else: # success is False, but no specific error (e.g., empty response)
            msg = "Gemini API 连接测试失败: 未收到有效响应。"
            log.error(msg)
            return False, msg