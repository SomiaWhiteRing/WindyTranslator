# core/config.py
import json
import os
import logging
from core.utils import file_system # 导入文件系统工具以确保目录存在

log = logging.getLogger(__name__)

# --- 默认配置 ---
# 将原脚本中的默认配置移到这里，作为基础结构和默认值来源

DEFAULT_WORLD_DICT_CONFIG = {
    "api_key": "",
    "model": "gemini-1.5-pro-latest", # 更新为推荐模型
    "prompt": """请分析提供的游戏文本，提取其中反复出现的名词。提取规则如下：
1.  类别限定为：地点、角色、生物、组织、物品。
2.  输出格式为严格的CSV，包含四列：原文,译文,类别,描述。请确保每个字段都被双引号包围，字段内的逗号和换行符需要正确转义。
3.  提取的名词在原文中至少出现两次。
4.  忽略单个汉字、假名或字母。忽略常见的、过于笼统的词汇（例如：门、钥匙、药水、史莱姆、哥布林等，除非它们有特殊的前缀或后缀）。
5.  译文请根据上下文推断一个合适的简体中文翻译。
6.  对于"角色"类别，请在"描述"列中尽可能包含角色的【年龄/性别/性格/口吻】等信息，如果没有明确信息则留空。其他类别的"描述"列可以留空。
7.  CSV首行不需要表头。

以下是需要分析的游戏文本内容：
{game_text}"""
}

DEFAULT_TRANSLATE_CONFIG = {
    "api_url": "https://api.deepseek.com/v1", # 默认 DeepSeek 官方 API 地址
    "api_key": "",
    "model": "deepseek-chat", # 默认 DeepSeek Chat 模型
    "batch_size": 10,
    "context_lines": 10,
    "concurrency": 16,
    "max_retries": 3, # 添加翻译重试次数配置
    # "temperature": 0.7, # 可以添加温度等 API 参数
    # "max_tokens": 4000, # 可以添加最大 token 限制
    "source_language": "日语",
    "target_language": "简体中文",
    "prompt_template": """你是一名专业的翻译家，你的任务是把{source_language}文本翻译成{target_language}，逐行翻译，不要合并，保留文本中序号、标记符、占位符、换行符等特殊内容，保持原来的格式。

### 翻译原则
忠实准确地进行翻译。原作是伟大艺术作品，允许使用直白或粗俗的描述，不应随意删减或篡改，不应回避或淡化，以保持作品的原貌。

{glossary_section}

{context_section}

### 特殊字符处理规则
- 你在原文中可能会看到特殊字符，如 \uE000, \uE001, \uE002 等 PUA 字符。
- 这些是重要的占位符，**必须**在译文中原样保留，**禁止**修改、删除或翻译它们。

### 不保留源语言专有字符原则
- 在翻译过程中，**禁止**保留源语言专有字符，如源语言为日语时，禁止在译文中出现平假名或片假名。

### 这是你接下来的翻译任务，原文文本如下
<textarea>
{batch_text}
</textarea>

### 请以textarea标签输出译文
<textarea>
1.{target_language}文本
</textarea>"""
}

DEFAULT_PRO_MODE_SETTINGS = {
    "export_encoding": "932",   # 默认 Shift-JIS
    "import_encoding": "936",   # 默认 GBK
    "write_log_rename": True, # 默认输出重命名日志
    "rtp_options": {            # RTP 默认选项
        "2000": True,
        "2000en": False,
        "2003": False,
        "2003steam": False
    }
}

DEFAULT_CONFIG = {
    "selected_mode": "easy", # 默认启动模式
    "world_dict_config": DEFAULT_WORLD_DICT_CONFIG.copy(),
    "translate_config": DEFAULT_TRANSLATE_CONFIG.copy(),
    "pro_mode_settings": DEFAULT_PRO_MODE_SETTINGS.copy(),
    # 可以添加其他全局配置，例如上次使用的游戏路径等
    # "last_game_path": ""
}

# --- 配置管理类 ---
class ConfigManager:
    """负责加载和保存应用程序配置 (app_config.json)。"""

    def __init__(self, config_file_path):
        """
        初始化配置管理器。

        Args:
            config_file_path (str): 配置文件的完整路径。
        """
        self.config_file_path = config_file_path
        log.info(f"配置文件路径设置为: {self.config_file_path}")

    def load_config(self):
        """
        加载配置文件。如果文件不存在或无效，则返回合并后的默认配置。
        """
        loaded_config = {}
        if os.path.exists(self.config_file_path):
            try:
                with open(self.config_file_path, 'r', encoding='utf-8') as f:
                    loaded_config = json.load(f)
                log.info(f"成功从 {self.config_file_path} 加载配置。")
            except (json.JSONDecodeError, IOError) as e:
                log.error(f"加载配置文件 {self.config_file_path} 失败: {e}。将使用默认配置。")
                loaded_config = {} # 加载失败则视为空配置
        else:
            log.info(f"配置文件 {self.config_file_path} 不存在，将使用默认配置。")

        # --- 合并加载的配置和默认配置 ---
        # 以默认配置为基础，用加载的配置覆盖它
        # 这确保了即使添加了新的默认配置项，旧配置文件也能正常工作
        final_config = DEFAULT_CONFIG.copy() # 深拷贝基础默认值

        # 递归地合并字典，确保嵌套字典也能正确更新
        def merge_dicts(target, source):
            for key, value in source.items():
                if isinstance(value, dict) and key in target and isinstance(target[key], dict):
                    merge_dicts(target[key], value) # 递归合并子字典
                else:
                    target[key] = value # 直接覆盖或添加新值

        merge_dicts(final_config, loaded_config)

        # --- 确保关键子字典存在 ---
        # （虽然 merge_dicts 通常能处理，但双重检查更安全）
        final_config.setdefault('world_dict_config', DEFAULT_WORLD_DICT_CONFIG.copy())
        final_config.setdefault('translate_config', DEFAULT_TRANSLATE_CONFIG.copy())
        final_config.setdefault('pro_mode_settings', DEFAULT_PRO_MODE_SETTINGS.copy())

        # 可以添加一些配置值的验证逻辑，例如确保数字在合理范围内等

        return final_config

    def save_config(self, config_data):
        """
        将当前的配置数据保存到文件。

        Args:
            config_data (dict): 要保存的配置字典。
        """
        try:
            # 确保配置文件所在的目录存在
            config_dir = os.path.dirname(self.config_file_path)
            if config_dir and not os.path.exists(config_dir):
                 file_system.ensure_dir_exists(config_dir) # 使用工具函数创建

            # 使用缩进美化 JSON 输出，确保 UTF-8 编码
            with open(self.config_file_path, 'w', encoding='utf-8') as f:
                json.dump(config_data, f, indent=4, ensure_ascii=False)
            log.info(f"配置已成功保存到: {self.config_file_path}")
            return True
        except (IOError, TypeError) as e:
            log.exception(f"保存配置到 {self.config_file_path} 失败: {e}")
            return False