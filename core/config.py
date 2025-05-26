import json
import os
import logging
from core.utils import file_system # 导入文件系统工具以确保目录存在

log = logging.getLogger(__name__)

# --- 默认配置 ---

# --- 默认世界观字典生成配置 ---
DEFAULT_WORLD_DICT_CONFIG = {
    "api_key": "",
    "model": "gemini-2.5-pro-preview-03-25",
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
8.  忽略单个汉字、假名或字母。忽略过于泛化的词语（如“男孩”、“女孩”、“村民”等，除非有明确的指代）。
9.  译文请根据上下文推断一个合适的简体中文翻译。
10.  CSV首行不需要表头。

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
    "api_url": "https://generativelanguage.googleapis.com/v1beta", # 更新为gemini的API地址
    "api_key": "",
    "model": "gemini-2.5-flash-preview-05-20", # 更新为gemini模型
    "batch_size": 32, 
    "context_lines": 8, 
    "concurrency": 16,
    "max_retries": 3,
    "source_language": "日语",
    "target_language": "简体中文",
    # 更新Prompt模板
    "prompt_template": """你是一名专业的游戏翻译家，你的任务是将以下从游戏中提取的{source_language}文本翻译成{target_language}。
请严格遵守以下翻译原则和格式要求。

**重要**: 请将你的所有翻译结果严格地包裹在 <textarea> 和 </textarea> 标签内部，并且每一行翻译对应原文的一个编号项。

### 翻译原则
1.  **忠实准确**: 忠实于原文含义，准确表达。如果原文是伟大的艺术作品，允许使用直白或粗俗的描述，不应随意删减或篡改，不应回避或淡化，以保持作品的原貌。
2.  **上下文理解**: 充分利用提供的上下文信息（包括上文、人物术语、事物术语、以及文本自身的元数据标记）来辅助翻译。
3.  **格式保留**: 严格保留文本中的编号、游戏内特殊标记符 (如 \\N[1], \\C[2], \\! 等)、PUA占位符 (如 \uE000, \uE001 等)、换行符、以及行首的空格（用于缩进），保持原来的格式。**禁止合并原文中的多行内容到译文的单行中。**
4.  **禁止保留源语言专有字符**: 在翻译成{target_language}时，**禁止**在译文中出现源语言的专有字符（例如，当源语言为日语时，禁止在译文中出现平假名或片假名）。特别是那些指代外语单词但没有标准中文译名的片假名，**务必**将其音译或翻译成对应的外语单词（通常是英文）。
5.  **角色口癖翻译**: 对于原文中角色特有的句尾口癖（如 `～でち`、`～なのだ` 等），请不要直接保留。应根据角色的性格和说话风格，尝试将其翻译成自然的中文语气后缀或表达方式，目标是传达原文语气，而非生硬复制。

### 文本元数据说明
你将收到的每一行待翻译原文都可能包含以下元数据前缀：
- `[MARKER: <marker_type>]`: 指示文本的原始类型，例如 `Message`, `Choice`, `Name`, `Title`, `Victory` 等。
- `[FACE: <identifier>]`: 指示与该文本关联的脸图标识符。`<identifier>` 可能是脸图文件名 (如 `Actor1_face`, `monster_01_0`)，也可能是特殊值 (如 `NARRATION`, `SYSTEM`, `NONE`)，或者此标记可能不存在。

### 根据元数据调整翻译策略
1.  **对话类文本 (`[MARKER: Message]`)**:
    *   **有脸图 (`[FACE: <文件名>]`)**: 这通常表示一个角色正在说话。请结合对话内容和下方的人物术语表（特别是“口吻”和“性格”字段），尝试推断出该脸图标识符可能对应的角色。在翻译时，请使用符合该角色身份、性格和当前情境的口吻及人称代词。
    *   **无明确脸图 (`[FACE: NARRATION]`, `[FACE: NONE]` 或无 `[FACE]` 标记)**:
        *   这通常表示**旁白、场景描述、背景介绍或角色不明确的叙述**。
        *   请使用**严格的第三人称叙述**（例如，避免使用“我”、“我们”、“你”、“你们”），除非原文中明确出现了这些代词。
        *   语气应保持**客观、中立**，如同故事的叙述者或解说者。
        *   如果文本内容明显是某个角色的内心独白，则可以根据上下文和人物术语表判断并使用第一人称。**但如果 `[FACE: NARRATION]` 标记存在，优先考虑其非角色直接发言的性质。**
 
2.  **系统/UI/词条类文本 (例如 `[MARKER: Name]`, `[MARKER: Choice]`, `[MARKER: Victory]`, `[MARKER: LevelUp]`, `[MARKER: ShopA:BuyScreen]`, 等其他非 Message 类型)**:
    *   这些通常是游戏界面上的元素、菜单选项、物品名称、技能名称、战斗提示、状态信息等。
    *   翻译时应力求**简洁、准确、书面化**，符合游戏术语或UI文本的常见风格。
    *   这类文本通常不由特定角色说出，**即使罕见地附带了 `[FACE]` 标记（例如某些系统消息可能伴随特定UI图标或角色头像），也应优先按照其“系统文本”的本质进行翻译，避免代入角色口吻。**

### 人物术语参考 (格式: 原文|译文|对应原名|性别|年龄|性格|口吻|描述)
如果提供了此部分，请务必参考。它可以帮助你识别角色、理解他们的特征，并保持译名和称呼的一致性。
{character_glossary_section}

### 事物术语参考 (格式: 原文|译文|类别 - 描述)
如果提供了此部分，请务必参考以确保非角色名词（如地点、物品、技能等）翻译的准确性和一致性。
{entity_glossary_section}

### 上文内容 ({source_language})
如果提供了此部分，它可以帮助你理解当前对话发生的背景。
<context>
{context_section}
</context>

### 翻译任务：将以下所有编号的 {source_language} 文本翻译为 {target_language}
请仔细阅读每一行的元数据标记和原文内容，然后给出翻译。
<textarea>
{batch_text}
</textarea>

**输出要求**：
请严格按照下面的格式，在 `<textarea>` 和 `</textarea>` 标签内部输出**所有编号项**的译文列表，确保译文的行数与原文列表中的编号项数完全一致。每一行译文对应原文的一个编号项。
<textarea>
1. 译文行1
2. 译文行2
...
N. 译文行N
</textarea>

### 输出前自我检查
请在生成最终输出前，再次检查以下几点：
1.  是否严格保留了原文中所有的特殊代码（如 `\\N[1]`, `\\C[0]`, `\\>`, `\uE000` 等）及其位置？（目标：是）
2.  译文中是否还有残留的日语假名（包括指代英文单词的片假名）？（目标：无）
3.  输出的编号数是否与输入的编号数完全一致，且一一对应？（目标：是）
4.  对于对话类文本，是否根据推断的发言人使用了恰当的人称和语气？（目标：是）
5.  对于系统/UI/词条类文本，翻译是否简洁、准确、书面化？（目标：是）
6.  是否所有翻译内容都包含在 `<textarea>` 和 `</textarea>` 标签内？（目标：是）
"""
}

# --- 默认专业模式配置 ---
DEFAULT_PRO_MODE_SETTINGS = {
    "export_encoding": "932",   # 默认 Shift-JIS
    "import_encoding": "936",   # 默认 GBK
    "rewrite_rtp_fix": False,    # 默认进行RTP修正（然后发现这个功能并没有什么卯月就让它变成黑历史吧
    "rtp_options": {            # RTP 默认选项
        "2000": True,
        "2000en": False,
        "2003": False,
        "2003steam": False
    }
}

# --- 完整的默认应用配置 ---
DEFAULT_CONFIG = {
    "selected_mode": "easy", # 默认启动模式
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
        # 填充 pro_mode_settings 下的其他顶级键
        for key, default_value in DEFAULT_PRO_MODE_SETTINGS.items():
             if key != 'rtp_options':
                 pro_node.setdefault(key, default_value)

        # 确保顶层 selected_mode 存在
        final_config.setdefault('selected_mode', DEFAULT_CONFIG['selected_mode'])

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
