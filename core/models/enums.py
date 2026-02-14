# core/models/enums.py
"""类型安全的枚举定义，替代散落各处的魔法字符串。"""

from enum import Enum


class TaskName(str, Enum):
    """后台任务名称枚举。对应 app.py 中 start_task() 的 task_name 参数。"""

    INITIALIZE = "initialize"
    RENAME = "rename"
    EXPORT = "export"
    CREATE_JSON = "create_json"
    GENERATE_DICTIONARY = "generate_dictionary"
    TRANSLATE = "translate"
    RELEASE_JSON = "release_json"
    IMPORT = "import"
    EASY_FLOW = "easy_flow"
    APPLY_BASE_DICTIONARY = "apply_base_dictionary_manual"

    # 以下为 UI 动作（非后台任务），在 start_task() 中直接处理后 return
    START_GAME = "start_game"
    EDIT_DICTIONARY = "edit_dictionary"
    FIX_FALLBACK = "fix_fallback"
    CONFIGURE_GEMINI = "configure_gemini"
    CONFIGURE_DEEPSEEK = "configure_deepseek"
    SELECT_RTP = "select_rtp"


class LogLevel(str, Enum):
    """日志级别枚举，用于 UI 日志区域的颜色标记。"""

    NORMAL = "normal"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"
    DEBUG = "debug"
