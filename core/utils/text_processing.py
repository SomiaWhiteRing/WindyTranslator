# core/utils/text_processing.py
import re
import datetime
import logging # 使用标准日志库记录更底层的细节

# 配置一个基础的日志记录器，供 utils 模块内部使用
# 在主应用中可能会有更高级的日志配置
log = logging.getLogger(__name__)

# --- 文本验证 ---
def validate_translation(original, translated, post_processed_translation):
    """
    验证译文是否符合特定规则（如保留标记、无假名等）。

    Args:
        original (str): 原文文本。
        translated (str): 原始译文文本 (API直接返回，未 PUA 还原和后处理)。
        post_processed_translation (str): 经过 PUA 还原和 post_process_translation 处理后的译文。

    Returns:
        bool: True 如果验证通过，False 否则。
        str: 如果失败，返回失败原因描述；如果成功，返回空字符串。
    """
    try:
        # 规则 1: 检查后处理后的译文中是否残留日语假名
        # \u3040-\u309F: Hiragana, \u30A0-\u30FF: Katakana
        kana_pattern = re.compile(r'[\u3040-\u309F\u30A0-\u30FF]')
        if kana_pattern.search(post_processed_translation):
            reason = f"验证失败: 译文残留日语假名。原文: '{original[:50]}...', 处理后译文: '{post_processed_translation[:50]}...'"
            log.warning(reason)
            return False, reason

        # 规则 2: 如果原文以 \\ 开头，译文是否也以 \\ 开头 (检查原始译文)
        if original.startswith('\\\\') and not translated.startswith('\\\\'):
             reason = f"验证失败: 译文丢失了开头格式符 '\\\\'。原文: '{original[:50]}...', 译文: '{translated[:50]}...'"
             log.warning(reason)
             return False, reason

        # 规则 3: 检查反斜杠 + 半角字符 (排除 \\ 和 \n) (检查原始译文)
        pattern_backslash_ascii = r'(?<!\\)\\[ -~]'
        original_backslash_count = len(re.findall(pattern_backslash_ascii, original))
        translated_backslash_count = len(re.findall(pattern_backslash_ascii, translated))
        if original_backslash_count != translated_backslash_count:
            reason = f"验证失败: 反斜杠标记数量不匹配。原文({original_backslash_count}): '{original[:50]}...', 译文({translated_backslash_count}): '{translated[:50]}...'"
            log.warning(reason)
            return False, reason

        # 规则 4: 检查上半直角引号「 (检查原始译文)
        original_quote_count = original.count('「')
        translated_quote_count = translated.count('「')
        # 允许译文比原文多，但不允许少
        if original_quote_count > translated_quote_count:
            reason = f"验证失败: 上半引号「数量少于原文。原文({original_quote_count}): '{original[:50]}...', 译文({translated_quote_count}): '{translated[:50]}...'"
            log.warning(reason)
            return False, reason

        # 规则 5: 检查上半直角双引号『 (检查原始译文)
        original_double_quote_count = original.count('『')
        translated_double_quote_count = translated.count('『')
        # 允许译文比原文多，但不允许少
        if original_double_quote_count > translated_double_quote_count:
            reason = f"验证失败: 上半双引号『数量少于原文。原文({original_double_quote_count}): '{original[:50]}...', 译文({translated_double_quote_count}): '{translated[:50]}...'"
            log.warning(reason)
            return False, reason
            
        # 新增：规则 6: 检查 PUA 占位符是否完全还原（检查后处理后的文本）
        # 如果后处理后的文本仍然包含 PUA 字符，说明还原失败或 API 返回了 PUA 字符
        pua_pattern = re.compile(r'[\uE000-\uF8FF]') # PUA 范围
        if pua_pattern.search(post_processed_translation):
            reason = f"验证失败: 译文包含未还原的 PUA 占位符。处理后译文: '{post_processed_translation[:50]}...'"
            log.warning(reason)
            return False, reason

        # 所有检查通过
        return True, ""
    except Exception as e:
        error_msg = f"验证函数内部出错: {e}"
        log.exception(error_msg) # 使用 log.exception 记录堆栈跟踪
        return False, error_msg # 出错时视为验证失败

# --- 文本预处理/后处理 ---

def pre_process_text_for_llm(text):
    """在发送给 LLM 前替换特殊标记为 PUA 占位符"""
    if not isinstance(text, str): return text
    # 优先替换更长的模式或可能包含其他模式的模式
    processed_text = text.replace(r'\!', '\uE002') # \!
    processed_text = processed_text.replace(r'\.', '\uE005') # \.
    processed_text = processed_text.replace(r'\<', '\uE006') # \<
    # 注意 > 的替换使用了负向前瞻 (?<!\\)，避免替换 \\>
    processed_text = re.sub(r'(?<!\\)>', '\uE007', processed_text) # > (but not \>)
    processed_text = processed_text.replace(r'\|', '\uE008') # \|
    processed_text = processed_text.replace(r'\^', '\uE009') # \^
    # 再替换单字符模式
    processed_text = processed_text.replace('「', '\uE000') # 「
    processed_text = processed_text.replace('」', '\uE001') # 」
    processed_text = processed_text.replace('『', '\uE003') # 『
    processed_text = processed_text.replace('』', '\uE004') # 』
    # log.debug(f"Preprocessed: '{text[:50]}...' -> '{processed_text[:50]}...'")
    return processed_text

def restore_pua_placeholders(text):
    """将译文中的 PUA 占位符还原为原始标记"""
    if not isinstance(text, str): return text
    # 按照与 pre_process 相反但逻辑对应的顺序还原
    processed_text = text.replace('\uE000', '「')
    processed_text = processed_text.replace('\uE001', '」')
    processed_text = processed_text.replace('\uE002', r'\!')
    processed_text = processed_text.replace('\uE003', '『')
    processed_text = processed_text.replace('\uE004', '』')
    processed_text = processed_text.replace('\uE005', r'\.')
    processed_text = processed_text.replace('\uE006', r'\<')
    processed_text = processed_text.replace('\uE007', '>') # 直接还原为 >
    processed_text = processed_text.replace('\uE008', r'\|')
    processed_text = processed_text.replace('\uE009', r'\^')
    # log.debug(f"Restored PUA: '{text[:50]}...' -> '{processed_text[:50]}...'")
    return processed_text

def post_process_translation(text, original_text):
    """
    对翻译后的、已还原 PUA 的文本进行最终的清理和格式调整。
    """
    if not isinstance(text, str): return text

    processed_text = text

    # 规则 1: 日语标点转中文/半角 (这些在 validate 前执行，确保验证时使用的是最终格式)
    processed_text = processed_text.replace('・', '·') # 日语点 -> 中文点
    processed_text = processed_text.replace('ー', '—') # 日语长音 -> 中文破折号
    processed_text = processed_text.replace('♪', '~') # 音符 -> 波浪号

    # 规则 2: 移除不必要的引号 (如果原文没有，译文却有)
    # 这个逻辑比较微妙，需要基于还原 PUA 后的引号
    if '「' not in original_text and '「' in processed_text:
         log.debug(f"Removing extra '「' from translation: '{processed_text[:50]}...'")
         processed_text = processed_text.replace('「', '')
    if '」' not in original_text and '」' in processed_text:
         log.debug(f"Removing extra '」' from translation: '{processed_text[:50]}...'")
         processed_text = processed_text.replace('」', '')
    if '『' not in original_text and '『' in processed_text:
         log.debug(f"Removing extra '『' from translation: '{processed_text[:50]}...'")
         processed_text = processed_text.replace('『', '')
    if '』' not in original_text and '』' in processed_text:
         log.debug(f"Removing extra '』' from translation: '{processed_text[:50]}...'")
         processed_text = processed_text.replace('』', '')

    # 规则 3: 引号平衡 (确保 「」 和 『』 成对出现，如果缺结尾，则补上)
    # 分别检查两种引号
    open_bracket_count = processed_text.count('「')
    close_bracket_count = processed_text.count('」')
    if open_bracket_count > close_bracket_count:
        missing_count = open_bracket_count - close_bracket_count
        log.debug(f"Adding {missing_count} missing '」' to translation: '{processed_text[:50]}...'")
        processed_text += '」' * missing_count

    open_double_bracket_count = processed_text.count('『')
    close_double_bracket_count = processed_text.count('』')
    if open_double_bracket_count > close_double_bracket_count:
        missing_count = open_double_bracket_count - close_double_bracket_count
        log.debug(f"Adding {missing_count} missing '』' to translation: '{processed_text[:50]}...'")
        processed_text += '』' * missing_count
        
    # log.debug(f"Post-processed: '{text[:50]}...' -> '{processed_text[:50]}...'")
    return processed_text


# --- 其他文本工具 ---

def sanitize_filename(filename):
    """移除或替换文件名中的非法字符，用于创建基于游戏名的目录等。"""
    # 移除 Windows 和 Linux/Mac 不允许的字符
    sanitized = re.sub(r'[\\/*?:"<>|]', '_', filename)
    # 移除控制字符 (ASCII 0-31)
    sanitized = re.sub(r'[\x00-\x1f]', '', sanitized)
    # 可以选择性地替换空格
    # sanitized = sanitized.replace(' ', '_')
    # 避免文件名以点或空格结尾（Windows限制）
    sanitized = sanitized.rstrip('. ')
    # 避免使用保留名称 (CON, PRN, AUX, NUL, COM1-9, LPT1-9)，虽然不太可能遇到
    reserved_names = {'CON', 'PRN', 'AUX', 'NUL'} | {f'COM{i}' for i in range(1, 10)} | {f'LPT{i}' for i in range(1, 10)}
    if sanitized.upper() in reserved_names:
        sanitized = "_" + sanitized
    # 防止空文件名
    if not sanitized:
        sanitized = "untitled"
    return sanitized