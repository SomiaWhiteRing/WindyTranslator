# core/models/config_models.py
"""Pydantic v2 配置模型，替代原有的 DEFAULT_* 字典和手写合并逻辑。

所有默认值直接定义在 Field() 中，Pydantic 的 model_validate() 自动处理
缺失字段的填充，完全替代 core/config.py 中约 60 行的 merge_dicts 逻辑。

JSON 键名使用 alias 保持与现有 app_config.json 的兼容性。
"""

from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 默认 Prompt 模板（从原 core/config.py 的 DEFAULT_WORLD_DICT_CONFIG 提取）
# 使用三引号字符串保持原始格式，避免隐式拼接的引号问题
# ---------------------------------------------------------------------------

DEFAULT_CHARACTER_PROMPT_TEMPLATE: str = """请分析提供的游戏文本，提取其中反复出现的【角色名称】和【角色昵称】。提取规则如下：
1.  输出格式为严格的CSV，包含八列：原文,译文,对应原名,性别,年龄,性格,口吻,描述。
2.  【对应原名】列：只有当该行是【昵称】时，才填写其对应的【角色名称】原文；如果是【角色名称】或无法确定对应关系，则此列留空。
3.  【性别】、【年龄】、【性格】、【口吻】列：主要针对【角色名称】提取，尽可能根据文本推断；如果是【昵称】，这些列通常留空，除非昵称本身明确指向这些属性；在【口吻】列中，如果包含了角色的口癖，则**必须**翻译成中文，不能有任何假名残留。
4.  【描述】列：可以补充其他关键信息，例如角色的种族、身份、与其他角色的关系等。
5.  确保每个字段都被双引号包围，字段内的逗号和换行符需要正确转义。
6.  **重要：生成的任何字段内容本身应避免包含英文双引号(`"`)字符。如果必须表示引用或特定术语，请考虑使用中文引号（\u201c \u201d）、单引号（' '）或其他标记，或者直接在描述性文本中说明。**
7.  提取的名词或昵称在原文中至少出现两次。
8.  在分析时，请特别注意那些在 [MARKER: Message] 类型的文本中，与特定 [FACE: <脸图标识>] 共同出现的、且反复提及的人名或代称。
9.  忽略单个汉字、假名或字母。忽略过于泛化的词语（如\u201c男孩\u201d、\u201c父亲\u201d、\u201c村民\u201d等，除非有明确的指代）。
10.  译文请根据上下文推断一个合适的简体中文翻译，同时考虑 [FACE: <脸图标识>] 可能暗示的发言人。

以下是需要分析的游戏文本内容：
{game_text}"""

DEFAULT_ENTITY_PROMPT_TEMPLATE: str = """请分析提供的游戏文本，提取其中反复出现的【地点】、【生物】、【组织】、【物品】、【事件】等实体名词（不包括角色）。提取规则如下：
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

DEFAULT_TRANSLATE_PROMPT_TEMPLATE: str = """你是一名专业的游戏翻译家，你的任务是将以下从游戏中提取的{source_language}文本翻译成{target_language}。
请严格遵守以下翻译原则和格式要求。

**重要**: 请将你的所有翻译结果严格地包裹在 <textarea> 和 </textarea> 标签内部，每一个译文编号项对应原文的一个编号项，并保持项内的行数与原文一致。

### 翻译原则
1.  **忠实准确**: 忠实于原文含义，准确表达。如果原文是伟大的艺术作品，允许使用直白或粗俗的描述，不应随意删减或篡改，不应回避或淡化，以保持作品的原貌。
2.  **上下文理解**: 充分利用提供的上下文信息（包括上文、人物术语、事物术语、以及文本自身的元数据标记）来辅助翻译。
3.  **格式保留**: 严格保留文本中的编号、游戏内特殊标记符 (如 \\N[1], \\C[2], \\! 等)、PUA占位符 (如 \uE000, \uE001 等)、换行符、以及行首的空格（用于缩进），保持原来的格式。**禁止合并原文中的多行内容到译文的单行中。**
4.  **禁止保留源语言专有字符**: 在翻译成{target_language}时，**禁止**在译文中出现源语言的专有字符（例如，当源语言为日语时，禁止在译文中出现平假名或片假名）。特别是那些指代外语单词但没有标准中文译名的片假名，**务必**将其音译或翻译成对应的外语单词（通常是英文）。
5.  **角色口癖翻译**: 对于原文中角色特有的句尾口癖（如 `～でち`、`～なのだ` 等），请不要直接保留。应根据角色的性格和说话风格，尝试将其翻译成自然的中文语气后缀或表达方式，目标是传达原文语气，而非生硬复制。
6.  **不翻译非指定语言**: 对于文本中出现的非{source_language}语言（如英语、韩语等），直接保留原文，而不是翻译成{target_language}。但如果是片假名或平假名指代的外语单词（如 `アメリカ`），则保持音译。

### 文本元数据说明
你将收到的每一行待翻译原文都可能包含以下元数据前缀：
- `[MARKER: <marker_type>]`: 指示文本的原始类型，例如 `Message`, `Choice`, `Name`, `Title`, `Victory` 等。
- `[FACE: <identifier>]`: 指示与该文本关联的脸图标识符。`<identifier>` 可能是脸图文件名 (如 `Actor1_face`, `monster_01_0`)，也可能是特殊值 (如 `NARRATION`, `SYSTEM`, `NONE`)，或者此标记可能不存在。

### 根据元数据调整翻译策略
1.  **对话类文本 (`[MARKER: Message]`)**:
    *   **有脸图 (`[FACE: <文件名>]`)**:
        *   这通常表示一个角色正在说话。请结合对话内容和下方的人物术语表（特别是\u201c口吻\u201d和\u201c性格\u201d字段），尝试推断出该脸图标识符可能对应的角色。
        *   在翻译时，请使用符合该角色身份、性格和当前情境的口吻及人称代词。
        *   在台词内容前有时存在角色名称描述，如\u201c王様\\n「ちょっと魔王倒してこいや。」\u201d。若存在，则**必须**保留角色名称描述，并将其翻译为对应的角色名称（如\u201c国王\\n「去打倒魔王吧。」\u201d）。
    *   **无明确脸图 (`[FACE: NARRATION]`, `[FACE: NONE]` 或无 `[FACE]` 标记)**:
        *   这通常表示**旁白、场景描述、背景介绍或角色不明确的叙述**。
        *   请使用**严格的第三人称叙述**（例如，避免使用\u201c我\u201d、\u201c我们\u201d、\u201c你\u201d、\u201c你们\u201d），除非原文中明确出现了这些代词。
        *   语气应保持**客观、中立**，如同故事的叙述者或解说者。
        *   如果文本内容明显是某个角色的内心独白，则可以根据上下文和人物术语表判断并使用第一人称。**但如果 `[FACE: NARRATION]` 标记存在，优先考虑其非角色直接发言的性质。**

2.  **系统/UI/词条类文本 (例如 `[MARKER: Name]`, `[MARKER: Choice]`, `[MARKER: Victory]`, `[MARKER: LevelUp]`, `[MARKER: ShopA:BuyScreen]`, 等其他非 Message 类型)**:
    *   这些通常是游戏界面上的元素、菜单选项、物品名称、技能名称、战斗提示、状态信息等。
    *   翻译时应力求**简洁、准确、书面化**，符合游戏术语或UI文本的常见风格。

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

**请注意：原文的每个编号项内部可能包含多行文本或特定缩进，这些格式都是重要的结构信息，请务必在译文中**逐行对应、精确保留**原文的换行符和前导空格。禁止合并原文中的多行内容。**

**输出要求**：
请严格按照下面的格式，在 `<textarea>` 和 `</textarea>` 标签内部输出**所有编号项**的译文列表，确保译文的行数与原文列表中的编号项数完全一致。每一行译文对应原文的一个编号项。
<textarea>
1. 这是译文的第一行。
这是译文的第二行，与原文对应。
2. 这是另一个条目的译文。
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
6.  是否所有翻译内容都包含在 `<textarea>` 和 `</textarea>` 标签内？（目标：是）"""


# ---------------------------------------------------------------------------
# Pydantic 配置模型
# ---------------------------------------------------------------------------

class RTPOptions(BaseModel):
    """RTP（运行时包）选项配置。

    JSON 键名使用 alias 保持与现有 app_config.json 兼容。
    """

    model_config = {"populate_by_name": True}

    rm2000: bool = Field(default=True, alias="2000")
    rm2000en: bool = Field(default=False, alias="2000en")
    rm2003: bool = Field(default=False, alias="2003")
    rm2003steam: bool = Field(default=False, alias="2003steam")


class ProModeSettings(BaseModel):
    """专业模式设置。"""

    export_encoding: str = "932"
    import_encoding: str = "936"
    rewrite_rtp_fix: bool = False
    rtp_options: RTPOptions = Field(default_factory=RTPOptions)


class WorldDictConfig(BaseModel):
    """世界观字典生成 API 配置。"""

    provider: str = "gemini"
    api_key: str = ""
    api_url: str = ""
    model: str = "gemini-2.5-pro-preview-05-06"
    openai_temperature: float = 0.2
    openai_max_tokens: int | None = None
    openai_extra_params: dict[str, Any] = Field(default_factory=dict)
    character_dict_filename: str = "character_dictionary.csv"
    entity_dict_filename: str = "entity_dictionary.csv"
    enable_base_dictionary: bool = True
    character_prompt_template: str = DEFAULT_CHARACTER_PROMPT_TEMPLATE
    entity_prompt_template: str = DEFAULT_ENTITY_PROMPT_TEMPLATE


class TranslateConfig(BaseModel):
    """翻译 API 配置。"""

    api_url: str = "https://generativelanguage.googleapis.com/v1beta"
    api_key: str = ""
    model: str = "gemini-2.5-flash-preview-05-20"
    batch_size: int = Field(default=32, ge=1)
    context_lines: int = Field(default=8, ge=0)
    concurrency: int = Field(default=16, ge=1)
    max_retries: int = Field(default=1, ge=0)
    source_language: str = "日语"
    target_language: str = "简体中文"
    prompt_template: str = DEFAULT_TRANSLATE_PROMPT_TEMPLATE


class AppConfig(BaseModel):
    """应用程序顶层配置模型。

    对应 app_config.json 的完整结构。Pydantic 的默认值机制
    完全替代原有的 merge_dicts 递归合并逻辑。
    """

    selected_mode: str = "easy"
    world_dict_config: WorldDictConfig = Field(default_factory=WorldDictConfig)
    translate_config: TranslateConfig = Field(default_factory=TranslateConfig)
    pro_mode_settings: ProModeSettings = Field(default_factory=ProModeSettings)
