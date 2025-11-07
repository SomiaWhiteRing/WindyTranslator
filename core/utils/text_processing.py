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
            # 放宽规则：若假名仅出现在“角色名：”的冒号之前，视为可接受
            try:
                # 同时支持全角冒号和半角冒号
                colon_pos_full = post_processed_translation.find('：')
                colon_pos_half = post_processed_translation.find(':')
                colon_idx = colon_pos_full if colon_pos_full != -1 else colon_pos_half
                if colon_idx > 0:
                    kana_positions = [m.start() for m in kana_pattern.finditer(post_processed_translation)]
                    if kana_positions and max(kana_positions) < colon_idx:
                        log.debug("忽略角色名中的假名: '%s'", post_processed_translation[:50])
                    else:
                        reason = (
                            f"验证失败: 译文残留日语假名。原文: '{original[:50]}...', 处理后译文: '{post_processed_translation[:50]}...'"
                        )
                        log.warning(reason)
                        return False, reason
                else:
                    reason = (
                        f"验证失败: 译文残留日语假名。原文: '{original[:50]}...', 处理后译文: '{post_processed_translation[:50]}...'"
                    )
                    log.warning(reason)
                    return False, reason
            except Exception:
                # 保守处理：异常时维持原有失败逻辑
                reason = (
                    f"验证失败: 译文残留日语假名。原文: '{original[:50]}...', 处理后译文: '{post_processed_translation[:50]}...'"
                )
                log.warning(reason)
                return False, reason

        # 规则 2: 如果原文以 \\ 开头，译文是否也以 \\ 开头 (检查原始译文)
        if original.startswith('\\\\') and not translated.startswith('\\\\'):
             reason = f"验证失败: 译文丢失了开头格式符 '\\\\'。原文: '{original[:50]}...', 译文: '{translated[:50]}...'"
             log.warning(reason)
             return False, reason

        # 规则 3: RPG 控制码按类型逐一对齐，减少误报（检查“译文-已还原PUA未后处理”）
        allowed_codes = [r'\\\.', r'\\<', r'\\>', r'\\\|', r'\\\^', r'\\!']
        def _count_codes(text: str):
            return {pat: len(re.findall(pat, text)) for pat in allowed_codes}
        orig_counts = _count_codes(original)
        tran_counts = _count_codes(translated)
        if any(orig_counts[k] != tran_counts[k] for k in allowed_codes):
            diff_repr = ", ".join([f"{k}:{orig_counts[k]}->{tran_counts[k]}" for k in allowed_codes])
            reason = (
                f"验证失败: 反斜杠标记数量不匹配。原文({sum(orig_counts.values())}): '{original[:50]}...', "
                f"译文({sum(tran_counts.values())}): '{translated[:50]}...'；差异: {diff_repr}"
            )
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
    processed_text = text.replace(r'\!\n', '\uE010') # \!\n
    processed_text = processed_text.replace(r'\!', '\uE002') # \!
    processed_text = processed_text.replace(r'\.', '\uE005') # \.
    processed_text = processed_text.replace(r'\<', '\uE006') # \<
    processed_text = processed_text.replace(r'\>', '\uE007') # \>
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
    processed_text = processed_text.replace('\uE007', r'\>') 
    processed_text = processed_text.replace('\uE008', r'\|')
    processed_text = processed_text.replace('\uE009', r'\^')
    processed_text = processed_text.replace('\uE010', r'\!\n')
    # log.debug(f"Restored PUA: '{text[:50]}...' -> '{processed_text[:50]}...'")
    return processed_text


def repair_translation_format(original_text: str, restored_translation: str) -> str:
    """
    对译文的控制码进行最小化修复：
    - 移除原文不存在的“未知”反斜杠序列（白名单之外）。
    - 按类型对齐白名单控制码数量；缺少则前置补齐，多余则自末尾移除。

    尽量不影响可行流程，仅为提高通过率与可用性。
    """
    if not isinstance(restored_translation, str):
        return restored_translation

    text = restored_translation

    # 1) 移除未知反斜杠序列（白名单之外）
    whitelist = [r'\.', r'\<', r'\>', r'\|', r'\^', r'\!']
    any_bs_ascii = re.compile(r'(?<!\\)\\[ -~]')
    whitelist_set = set(whitelist)

    def _is_whitelisted(seq: str) -> bool:
        return seq in whitelist_set

    original_unknown = [m.group(0) for m in any_bs_ascii.finditer(original_text)
                        if not _is_whitelisted(m.group(0))]
    if not original_unknown:
        matches = [m for m in any_bs_ascii.finditer(text) if not _is_whitelisted(m.group(0))]
        if matches:
            new_text_chars = list(text)
            for m in reversed(matches):
                s, e = m.span()
                for i in range(s, e):
                    new_text_chars[i] = ''
            text = ''.join(new_text_chars)

    # 2) 按类型对齐白名单控制码数量
    def _count_type(t: str, s: str) -> int:
        return len(re.findall(re.escape(t), s))

    # 记录译文修复前的前导换行位置（用于插入）
    leading_nl_len = len(text) - len(text.lstrip('\n'))

    for code in whitelist:
        o_cnt = _count_type(code, original_text)
        t_cnt = _count_type(code, text)
        if t_cnt > o_cnt:
            for _ in range(t_cnt - o_cnt):
                last_idx = text.rfind(code)
                if last_idx >= 0:
                    text = text[:last_idx] + text[last_idx + len(code):]
        elif t_cnt < o_cnt:
            missing = o_cnt - t_cnt
            insert_at = leading_nl_len
            text = text[:insert_at] + (code * missing) + text[insert_at:]

    return text

def post_process_translation(text, original_text):
    """
    对翻译后的、已还原 PUA 的文本进行最终的清理和格式调整。
    """
    if not isinstance(text, str): return text

    processed_text = text

    # 规则 1: 日语标点转中文/半角 (这些在 validate 前执行，确保验证时使用的是最终格式)
    processed_text = processed_text.replace('・', '·') # 日语点 -> 中文点
    processed_text = processed_text.replace('ー', '―') # 日语长音 -> 中文破折号
    processed_text = processed_text.replace('—', '―') # 半角破折号 -> 全角破折号
    processed_text = processed_text.replace('♪', '～') # 音符 -> 波浪号
    processed_text = processed_text.replace('~', '～') # 半角波浪号 -> 波浪号
    processed_text = processed_text.replace('⋯', '…') # 日文间隔号 -> 中文省略号

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

    # 规则 2.1: 移除重复出现的引号
    processed_text = processed_text.replace('“『', '『')
    processed_text = processed_text.replace('“「', '「')
    processed_text = processed_text.replace('』”', '』')
    processed_text = processed_text.replace('」”', '」')

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
        

    # 规则 4: 恢复前导换行符
    original_leading_newlines = ""
    for char_val in original_text: # 修正：直接迭代字符串字符
        if char_val == '\n':
            original_leading_newlines += '\n'
        else:
            break
    current_text_without_leading_newlines = processed_text.lstrip('\n')
    processed_text = original_leading_newlines + current_text_without_leading_newlines
    
    # 规则 5: 空格数量修正
    original_lines = original_text.split('\n')
    processed_lines = processed_text.split('\n')
    
    final_processed_lines = []
    
    num_original_lines = len(original_lines)
    num_processed_lines = len(processed_lines)

    for i in range(num_processed_lines):
        current_p_line = processed_lines[i]
        
        # 获取对应的原文行，如果存在的话
        o_line_exists = i < num_original_lines
        current_o_line = original_lines[i] if o_line_exists else "" # 如果原文行不存在，视为空串

        # a. 处理当前译文行行首的空格 (即上一行换行符之后的空格)
        # 只有当不是整个文本的第一行时，行首空格才是在换行符“之后”
        if i > 0:
            if current_p_line.startswith(' ') and (not o_line_exists or not current_o_line.startswith(' ')):
                # 如果译文行以空格开头，但原文对应行没有以空格开头 (或原文行不存在)
                # 则移除译文行的行首空格
                original_len = len(current_p_line)
                current_p_line = current_p_line.lstrip(' ')
                

        # b. 处理当前译文行行尾的空格 (即下一行换行符之前的空格)
        # 只有当不是整个文本的最后一行时，行尾空格才是在换行符“之前”
        if i < num_processed_lines - 1:
            if current_p_line.endswith(' ') and (not o_line_exists or not current_o_line.endswith(' ')):
                # 如果译文行以空格结尾，但原文对应行没有以空格结尾 (或原文行不存在)
                # 则移除译文行的行尾空格
                original_len = len(current_p_line)
                current_p_line = current_p_line.rstrip(' ')
                
        final_processed_lines.append(current_p_line)
        
    processed_text = '\n'.join(final_processed_lines)

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

# **** 新增：半角片假名转全角片假名函数 ****
def convert_half_to_full_katakana(text):
    """
    将字符串中的半角片假名（包括标点、浊音/半浊音符号）转换为全角形式。

    Args:
        text (str): 输入字符串。

    Returns:
        str: 转换后的字符串。
    """
    if not isinstance(text, str):
        return text

    # 半角片假名、标点、特殊符号 -> 全角对应字符 映射表
    # 包含 U+FF61 到 U+FF9F 的范围
    hankaku_chars = "｡｢｣､･ｦｧｨｩｪｫｬｭｮｯｰｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾛﾜﾝﾞﾟ"
    zenkaku_chars = "。「」、・ヲァィゥェォャュョッーアイウエオカキクケコサシスセソタチツテトナニヌネノハヒフヘホマミムメモヤユヨラリルレロワン゛゜"

    if len(hankaku_chars) != len(zenkaku_chars):
        log.error("内部错误：半角/全角片假名映射长度不匹配！")
        return text # 避免出错，返回原文

    # 创建基础转换表
    trans_table = str.maketrans(hankaku_chars, zenkaku_chars)

    # 应用基础转换
    text = text.translate(trans_table)

    # 处理需要合并的浊音和半浊音
    # 顺序很重要，先处理带浊音/半浊音的组合
    replacements = {
        'カ゛': 'ガ', 'キ゛': 'ギ', 'ク゛': 'グ', 'ケ゛': 'ゲ', 'コ゛': 'ゴ',
        'サ゛': 'ザ', 'シ゛': 'ジ', 'ス゛': 'ズ', 'セ゛': 'ゼ', 'ソ゛': 'ゾ',
        'タ゛': 'ダ', 'チ゛': 'ヂ', 'ツ゛': 'ヅ', 'テ゛': 'デ', 'ト゛': 'ド',
        'ハ゛': 'バ', 'ヒ゛': 'ビ', 'フ゛': 'ブ', 'ヘ゛': 'ベ', 'ホ゛': 'ボ',
        'ウ゛': 'ヴ', # 特殊的ウ浊音
        'ハ゜': 'パ', 'ヒ゜': 'ピ', 'フ゜': 'プ', 'ヘ゜': 'ペ', 'ホ゜': 'ポ',
    }

    # 执行替换
    for half_comb, full_comb in replacements.items():
        text = text.replace(half_comb, full_comb)

    return text


# --- 语言相关的简单检测 ---
def has_japanese_letters(text):
    """
    粗略检测文本中是否包含日文字符（假名或汉字）。
    包含范围：平假名、片假名、半角片假名、CJK汉字。

    用途：用于在源语言为日语时，判断某条文本是否需要翻译。
    """
    if not isinstance(text, str) or not text:
        return False
    # 平假名 3040-309F，片假名 30A0-30FF，片假名扩展 31F0-31FF，半角片假名 FF66-FF9F，CJK 4E00-9FFF
    pattern = re.compile(r"[\u3040-\u309F\u30A0-\u30FF\u31F0-\u31FF\uFF66-\uFF9F\u4E00-\u9FFF]")
    return pattern.search(text) is not None
