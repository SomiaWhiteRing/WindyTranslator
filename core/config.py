import json
import os
import logging
from core.utils import file_system # 导入文件系统工具以确保目录存在
from core.updates import DEFAULT_SITE_URL
from core.tasks.translation_protocol import DEFAULT_SYSTEM_PROMPT, DEFAULT_USER_PROMPT

log = logging.getLogger(__name__)

# --- 默认配置 ---

# --- 默认世界观字典生成配置 ---
DEFAULT_WORLD_DICT_CONFIG = {
    "provider": "gemini",
    "api_key": "",
    "api_url": "",
    "model": "gemini-2.5-pro-preview-05-06",
    "openai_temperature": 0.2,
    "openai_max_tokens": None,
    "openai_extra_params": {},
    "character_dict_filename": "character_dictionary.csv", # 人物词典文件名
    "entity_dict_filename": "entity_dictionary.csv",       # 事物词典文件名
    "enable_base_dictionary": True, # <--- 启用基础字典

    # 更新人物提取 Prompt，添加口吻不能有假名残留的要求
    "character_prompt_template": """请分析提供的游戏文本，提取其中反复出现的【角色名称】和【角色昵称】。提取规则如下：
1.  输出格式为严格的CSV，包含八列：原文,译文,对应原名,性别,年龄,性格,口吻,描述。
2.  【对应原名】列：只有当该行是【昵称】时，才填写其对应的【角色名称】原文；如果是【角色名称】或无法确定对应关系，则此列留空。
3.  【性别】、【年龄】、【性格】、【口吻】列：主要针对【角色名称】提取，尽可能根据文本推断；如果是【昵称】，这些列通常留空，除非昵称本身明确指向这些属性；在【口吻】列中，如果包含了角色的口癖，则**必须**翻译成中文，不能有任何假名残留。
4.  【描述】列：可以补充其他关键信息，例如角色的种族、身份、与其他角色的关系等。
5.  确保每个字段都被双引号包围，字段内的逗号和换行符需要正确转义。
6.  **重要：生成的任何字段内容本身应避免包含英文双引号(`"`)字符。如果必须表示引用或特定术语，请考虑使用中文引号（“ ”）、单引号（' '）或其他标记，或者直接在描述性文本中说明。**
7.  提取的名词或昵称在原文中至少出现两次。
8.  在分析时，请特别注意那些在 [MARKER: Message] 类型的文本中，与特定 [FACE: <脸图标识>] 共同出现的、且反复提及的人名或代称。
9.  忽略单个汉字、假名或字母。忽略过于泛化的词语（如“男孩”、“父亲”、“村民”等，除非有明确的指代）。
10.  译文请根据上下文推断一个合适的简体中文翻译，同时考虑 [FACE: <脸图标识>] 可能暗示的发言人。

以下是需要分析的游戏文本内容：
{game_text}""",

    # 默认事物提取 Prompt (包含人物词典参考)
    "entity_prompt_template": """请分析提供的游戏文本，提取其中反复出现的【地点】、【生物】、【组织】、【物品】、【事件】等实体名词（不包括角色）。提取规则如下：
1.  输出格式为严格的CSV，包含四列：原文,译文,类别,描述。
2.  【类别】限定为：地点、生物、组织、物品、事件。
3.  确保每个字段都被双引号包围，字段内的逗号和换行符需要正确转义。
4.  提取的实体名词在原文中至少出现两次。
5.  忽略单个汉字、假名或字母。忽略常见的、过于笼统的词汇（例如：门、钥匙、药水、史莱姆、哥布林等，除非它们有特殊的前缀或后缀，或在特定上下文中具有重要意义）。
6.  译文请根据上下文推断一个合适的简体中文翻译。
7.  CSV首行不需要表头。

### 人物词典参考 (CSV格式)
以下是已提取的人物词典内容，采用CSV格式（原文,译文,对应原名,性别,年龄,性格,口吻,描述）。请在提取和翻译地点、物品等实体时，参考此词典中的'原文'和'译文'，确保与人物相关的用词保持一致。如果游戏文本中提到了某个地点或物品属于某个人物，请在【描述】列中注明。
```csv
{character_reference_csv_content}
```

以下是需要分析的游戏文本内容：
{game_text}

请输出事物词典 (原文,译文,类别,描述)，严格CSV格式。"""
}

# --- 默认翻译配置 ---
DEFAULT_TRANSLATE_CONFIG = {
    "api_url": "",
    "api_key": "",
    "model": "",
    "batch_size": 32,
    "context_lines": 8,
    "concurrency": 4,
    "max_retries": 1,
    "retry_failed_items_only": False,
    "source_language": "日语",
    "target_language": "简体中文",
    # New fields deliberately supersede every old prompt_template value.
    "system_prompt": DEFAULT_SYSTEM_PROMPT,
    "user_prompt_template": DEFAULT_USER_PROMPT,
    "temperature": 0.7,
    "request_timeout": 120,
    "thinking_mode": "auto",
    "stream_mode": "auto",
    "max_requests_per_batch": 16,
    "max_task_requests": 20000,
}

# --- 默认专业模式配置 ---
DEFAULT_PRO_MODE_SETTINGS = {
    "export_source_encoding": "auto",
    "import_encoding": "936",   # 默认 GBK
    "export_scope": {
        "game_text": True,
        "game_title": True,
        "map_names": False,
        "map_event_names": False,
        "switch_names": False,
        "variable_names": False,
        "common_event_names": False,
        "troop_names": False,
    },
    "auto_import_after_release": False,
    "rtp_options": {            # RTP 默认选项
        "2000": True,
        "2000en": False,
        "2003": False,
        "2003steam": False,
        "2003zh_tw": False
    }
}

# --- 完整的默认应用配置 ---
DEFAULT_CONFIG = {
    "selected_mode": "easy", # 默认启动模式
    "enable_completion_notification": False,
    "completion_notification_identity_registered": False,
    "updates": {"site_url": DEFAULT_SITE_URL, "check_on_startup": False},
    # 使用深拷贝确保子字典独立
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
        合并逻辑会确保新旧配置文件的兼容性。
        """
        loaded_config = {}
        if os.path.exists(self.config_file_path):
            try:
                with open(self.config_file_path, 'r', encoding='utf-8') as f:
                    loaded_config = json.load(f)
                log.info(f"成功从 {self.config_file_path} 加载配置。")
            except (json.JSONDecodeError, IOError) as e:
                log.error(f"加载配置文件 {self.config_file_path} 失败: {e}。将使用合并默认配置。")
                loaded_config = {} # 加载失败则视为空配置
        else:
            log.info(f"配置文件 {self.config_file_path} 不存在，将使用默认配置。")

        # --- 递归合并加载的配置和默认配置 ---
        # 以默认配置为基础，用加载的配置覆盖它
        # 这确保了即使添加了新的默认配置项，旧配置文件也能正常工作
        final_config = {} # 从空字典开始，以确保正确的深拷贝

        def merge_dicts(target, source):
            """递归合并字典，source 的值覆盖 target 的值。"""
            for key, value in source.items():
                if isinstance(value, dict):
                    # 获取或创建目标字典中的子字典
                    node = target.setdefault(key, {})
                    # 如果目标中的节点不是字典（例如旧配置中是None或字符串），则直接用源的值覆盖
                    if isinstance(node, dict):
                        merge_dicts(node, value)
                    else:
                        target[key] = value # 类型不匹配，直接覆盖
                # 处理列表（例如安全设置，虽然当前默认配置没有，但以防万一）
                # 注意：简单覆盖列表，不进行元素级合并
                elif isinstance(value, list):
                    target[key] = value
                else:
                    # 简单类型直接覆盖或添加新值
                    target[key] = value

        # 先将默认配置深拷贝到 final_config
        # 使用 json 模块进行深拷贝是一种简洁的方法
        final_config = json.loads(json.dumps(DEFAULT_CONFIG))

        # 然后将加载的配置合并到 final_config 中
        merge_dicts(final_config, loaded_config)

        # --- 验证和确保关键子字典及内部键存在 (双重保险) ---
        # 使用 setdefault 的嵌套方式确保结构完整性
        # 对 world_dict_config 进行检查和填充默认值
        world_dict_node = final_config.setdefault('world_dict_config', {})
        if not isinstance(world_dict_node, dict): # 如果加载的不是字典，强制重置为默认
            world_dict_node = final_config['world_dict_config'] = json.loads(json.dumps(DEFAULT_WORLD_DICT_CONFIG))
        for key, default_value in DEFAULT_WORLD_DICT_CONFIG.items():
            world_dict_node.setdefault(key, default_value) # 填充缺失的键

        # 对 translate_config 进行检查和填充默认值
        translate_node = final_config.setdefault('translate_config', {})
        if not isinstance(translate_node, dict):
            translate_node = final_config['translate_config'] = json.loads(json.dumps(DEFAULT_TRANSLATE_CONFIG))
        for key, default_value in DEFAULT_TRANSLATE_CONFIG.items():
            translate_node.setdefault(key, default_value)
        for obsolete in ("prompt_template", "max_context_chars", "max_batch_chars", "max_tokens", "max_task_tokens"):
            translate_node.pop(obsolete, None)

        # 对 pro_mode_settings 进行检查和填充默认值
        pro_node = final_config.setdefault('pro_mode_settings', {})
        if not isinstance(pro_node, dict):
            pro_node = final_config['pro_mode_settings'] = json.loads(json.dumps(DEFAULT_PRO_MODE_SETTINGS))
        else: # 如果是字典，再检查内部的 rtp_options
            rtp_node = pro_node.setdefault('rtp_options', {})
            if not isinstance(rtp_node, dict):
                 rtp_node = pro_node['rtp_options'] = json.loads(json.dumps(DEFAULT_PRO_MODE_SETTINGS['rtp_options']))
            for key, default_value in DEFAULT_PRO_MODE_SETTINGS['rtp_options'].items():
                rtp_node.setdefault(key, default_value)
            export_scope_node = pro_node.setdefault('export_scope', {})
            if not isinstance(export_scope_node, dict):
                export_scope_node = pro_node['export_scope'] = json.loads(json.dumps(DEFAULT_PRO_MODE_SETTINGS['export_scope']))
            for key, default_value in DEFAULT_PRO_MODE_SETTINGS['export_scope'].items():
                export_scope_node.setdefault(key, default_value)
        # 填充 pro_mode_settings 下的其他顶级键
        for key, default_value in DEFAULT_PRO_MODE_SETTINGS.items():
             if key not in ('rtp_options', 'export_scope'):
                 pro_node.setdefault(key, default_value)

        # 确保顶层 selected_mode 存在
        final_config.setdefault('selected_mode', DEFAULT_CONFIG['selected_mode'])
        final_config.setdefault('enable_completion_notification', DEFAULT_CONFIG['enable_completion_notification'])
        final_config.setdefault(
            'completion_notification_identity_registered',
            DEFAULT_CONFIG['completion_notification_identity_registered'],
        )

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
