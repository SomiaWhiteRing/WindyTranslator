# core/api_clients/gemini.py
import logging
from google import genai
# **** 新增：导入 types 用于配置 ****
from google.genai import types
from google.api_core import exceptions as google_exceptions

log = logging.getLogger(__name__)

class GeminiClient:
    """封装与 Google Gemini API (新版 SDK) 的交互。"""

    def __init__(self, api_key):
        """
        初始化 Gemini 客户端 (新版 SDK)。

        Args:
            api_key (str): Google AI Studio 的 API Key。

        Raises:
            ValueError: 如果 API Key 为空。
            ConnectionError: 如果客户端初始化失败。
        """
        if not api_key:
            raise ValueError("API Key 不能为空。")
        self.api_key = api_key
        try:
            # **** 修改：创建 Client 实例 ****
            # 显式传递 api_key
            self.client = genai.Client(api_key=self.api_key)
            log.info("Gemini API Client (google-genai SDK) 初始化成功。")
            # 不再需要 genai.configure()
        except Exception as e:
            log.exception(f"初始化 Gemini API Client 失败: {e}")
            raise ConnectionError(f"初始化 Gemini API Client 失败: {e}") from e

    def generate_content(self, model_name, prompt, generation_config=None, safety_settings=None):
        """
        调用 Gemini API 生成内容 (新版 SDK)。

        Args:
            model_name (str): 要使用的模型名称 (例如 "gemini-1.5-pro-latest")。
            prompt (str): 发送给模型的完整提示文本 (作为 contents)。
            generation_config (dict, optional): 生成参数 (如 temperature, max_output_tokens)。
                                                例如: {"temperature": 0.7, "max_output_tokens": 4000}
            safety_settings (list, optional): 安全设置列表，每个元素是字典或 types.SafetySetting。
                                              例如: [{"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"}]

        Returns:
            tuple: (success, result_text, error_message)
                   success (bool): API 调用是否成功并获得有效响应。
                   result_text (str): 如果成功，返回模型生成的文本；否则为 None。
                   error_message (str): 如果失败，返回错误信息；否则为 None。
        """
        if not model_name:
            return False, None, "模型名称不能为空。"
        if not prompt:
            return False, None, "Prompt (contents) 不能为空。"

        # **** 修改：将所有可选配置合并到 config 字典中 ****
        api_config_params = {}
        if generation_config:
            api_config_params.update(generation_config)
            log.debug(f"使用 generation_config 参数: {generation_config}")

        if safety_settings:
             # 将 safety_settings 作为 config 字典的一个键
             # SDK 应该能处理字典列表形式的安全设置
            api_config_params['safety_settings'] = safety_settings
            log.debug(f"使用 safety_settings 参数: {safety_settings}")

        try:
            log.debug(f"向 Gemini 模型 '{model_name}' (新 SDK) 发送请求...")

            # **** 修改：使用 client.models.generate_content ****
            response = self.client.models.generate_content(
                model=model_name,  # 模型名称作为参数传入
                contents=prompt,   # prompt 作为 contents 参数
                config=api_config_params if api_config_params else None # 将字典传给 GenerationConfig
            )

            # --- 响应处理 (基本保持不变) ---
            if response.prompt_feedback and response.prompt_feedback.block_reason:
                reason = response.prompt_feedback.block_reason.name
                error_msg = f"Gemini API 请求被阻止: {reason}"
                log.error(error_msg)
                # 记录安全评分细节
                for rating in response.prompt_feedback.safety_ratings:
                     log.error(f"  - 安全类别: {rating.category.name}, 概率: {rating.probability.name}")
                return False, None, error_msg

            # 检查 text 属性是否存在且非空
            if hasattr(response, 'text') and response.text:
                log.debug("Gemini API (新 SDK) 成功返回文本响应。")
                return True, response.text, None
            else:
                # 检查是否有完成原因
                finish_reason = "未知"
                # 新 SDK 的响应结构可能略有不同，需要确认 candidates 路径
                if hasattr(response, 'candidates') and response.candidates:
                     # 假设第一个 candidate 包含信息
                     candidate = response.candidates[0]
                     if hasattr(candidate, 'finish_reason') and candidate.finish_reason:
                         try: # finish_reason 可能已经是枚举值
                             finish_reason = candidate.finish_reason.name
                         except AttributeError:
                             finish_reason = str(candidate.finish_reason) # 转为字符串

                # 检查是否有 parts 但 text 为空的情况（例如函数调用）
                parts_exist = hasattr(response, 'parts') and response.parts
                text_is_empty = not (hasattr(response, 'text') and response.text)

                if parts_exist and text_is_empty:
                    error_msg = f"Gemini API 调用成功，但返回内容包含 Parts (可能是函数调用)，而非直接文本。完成原因: {finish_reason}"
                else:
                    error_msg = f"Gemini API 调用成功，但未返回有效文本。完成原因: {finish_reason}"

                log.warning(error_msg)
                return False, None, error_msg

        # --- 异常处理 (基本保持不变，因为核心 API 错误可能类似) ---
        except google_exceptions.InvalidArgument as e:
            error_msg = f"Gemini API 参数错误 (新 SDK): {e}"
            log.error(error_msg)
            return False, None, error_msg
        except google_exceptions.PermissionDenied as e:
            error_msg = f"Gemini API 权限错误 (检查 API Key?) (新 SDK): {e}"
            log.error(error_msg)
            return False, None, error_msg
        except google_exceptions.ResourceExhausted as e:
            error_msg = f"Gemini API 资源耗尽 (检查配额?) (新 SDK): {e}"
            log.error(error_msg)
            return False, None, error_msg
        except google_exceptions.GoogleAPIError as e:
            error_msg = f"Gemini API 调用失败 (新 SDK): {e}"
            log.exception(error_msg)
            return False, None, error_msg
        except Exception as e:
            error_msg = f"与 Gemini API (新 SDK) 交互时发生意外错误: {e}"
            log.exception(error_msg)
            return False, None, error_msg

    def test_connection(self, model_name):
        """
        尝试与 Gemini API (新版 SDK) 进行简单的连接测试。

        Args:
            model_name (str): 用于测试的模型名称。

        Returns:
            tuple: (success, message)
                   success (bool): 连接测试是否成功。
                   message (str): 测试结果或错误信息。
        """
        log.info(f"测试与 Gemini API (新 SDK, 模型: {model_name}) 的连接...")
        # **** 修改：调用更新后的 generate_content ****
        success, text, error = self.generate_content(model_name, "你好") # 使用简单 prompt
        if success and text:
            msg = "Gemini API (新 SDK) 连接测试成功！"
            log.info(msg)
            return True, msg
        elif error:
            msg = f"Gemini API (新 SDK) 连接测试失败: {error}"
            log.error(msg)
            return False, msg
        else:
            msg = "Gemini API (新 SDK) 连接测试失败: 未收到有效响应。"
            log.error(msg)
            return False, msg