# core/tasks/initialize.py
from __future__ import annotations

import os
import re
import shutil
import logging
from typing import TYPE_CHECKING

from core.external import easyrpg, rtp
from core.utils import file_system
from core.utils.engine_detection import detect_game_engine

if TYPE_CHECKING:
    from core.message_broker import MessageBroker

log = logging.getLogger(__name__)

# 定义支持的日文编码（用于检测）
JAPANESE_ENCODINGS = ['shift_jis', 'cp932', 'euc_jp']
# 定义假名匹配模式
KANA_PATTERN = re.compile(r'[\u3040-\u30FF]+') # 至少一个假名

def _detect_and_convert_encoding(file_path, target_encoding='gbk'):
    """
    检测文件是否可能为日文编码（通过解码+假名判断），如果是则转换为目标编码。

    Args:
        file_path (str): 要处理的文件路径。
        target_encoding (str): 目标编码。

    Returns:
        bool: 如果进行了转换则返回 True，否则 False。
        str: 检测到的源编码（如果转换了），否则 None。
    """
    try:
        with open(file_path, 'rb') as f_bytes:
            raw_content = f_bytes.read()
        if not raw_content:
            log.debug(f"跳过空文件: {os.path.basename(file_path)}")
            return False, None

        detected_encoding = None
        decoded_content = None

        for encoding in JAPANESE_ENCODINGS:
            try:
                # 尝试用严格模式解码
                current_decoded = raw_content.decode(encoding, errors='strict')
                # 如果解码成功且包含假名，则认为是目标文件
                if KANA_PATTERN.search(current_decoded):
                    decoded_content = current_decoded
                    detected_encoding = encoding
                    log.info(f"文件 {os.path.basename(file_path)} 使用 {encoding} 解码并检测到假名，标记转换。")
                    break # 找到一个即可
                else:
                    # 解码成功但不含假名，可能不是目标文件，继续尝试其他编码
                    log.debug(f"文件 {os.path.basename(file_path)} 使用 {encoding} 解码成功，但未检测到假名。")
                    # 不一定是最终编码，继续尝试下一个以防万一
                    # 如果所有日文编码都试过且没有假名，则认为不需要转换
                    # break # 如果解码成功就停止，可能会误判（例如纯ASCII的SJIS文件）
                    
            except UnicodeDecodeError:
                # 解码失败，尝试下一个编码
                log.debug(f"文件 {os.path.basename(file_path)} 使用 {encoding} 解码失败。")
                continue
            except Exception as decode_err: # 捕获其他可能的解码错误
                log.warning(f"尝试用 {encoding} 解码文件 {os.path.basename(file_path)} 时发生意外错误: {decode_err}")
                continue


        # 如果找到了合适的日文编码和假名，则执行转换
        if detected_encoding and decoded_content is not None:
            try:
                with open(file_path, 'w', encoding=target_encoding, errors='replace') as file_out:
                    file_out.write(decoded_content)
                log.info(f"已将文件 {os.path.basename(file_path)} 从 {detected_encoding} 转换为 {target_encoding}")
                return True, detected_encoding
            except Exception as write_err:
                log.error(f"将文件 {os.path.basename(file_path)} 写入 {target_encoding} 编码时失败: {write_err}")
                return False, detected_encoding # 转换失败
        else:
             log.debug(f"文件 {os.path.basename(file_path)} 无需转换编码。")
             return False, None

    except Exception as e:
        log.error(f"处理文件编码时出错: {file_path} - {e}")
        return False, None


def _update_rpg_rt_ini(ini_path, target_encoding_code='936'):
    """
    检查并更新 RPG_RT.ini 文件，确保 FullPackageFlag=1 和 Encoding=936 存在。
    假设 ini 文件已被转换为 GBK 编码。

    Args:
        ini_path (str): RPG_RT.ini 文件的路径。
        target_encoding_code (str): EasyRPG 期望的编码代号。
    """
    if not os.path.exists(ini_path):
        log.warning(f"未找到 RPG_RT.ini 文件，跳过配置修改: {ini_path}")
        return False # 表示未进行修改

    log.info(f"检查并更新 RPG_RT.ini: {ini_path}")
    needs_write = False
    lines = []
    try:
        # 假设文件已被之前的步骤转换为 gbk
        with open(ini_path, 'r', encoding='gbk', errors='replace') as f:
            lines = f.readlines()
    except Exception as e:
        log.error(f"读取 RPG_RT.ini 文件失败 ({ini_path}): {e}")
        return False # 无法读取，不修改

    # --- 分析内容 ---
    rpg_rt_section_index = -1
    easyrpg_section_index = -1
    full_package_found = False
    encoding_found = False

    current_section = None
    for i, line in enumerate(lines):
        line_strip = line.strip()
        if line_strip.startswith('['):
            current_section = line_strip
            if current_section == '[RPG_RT]':
                rpg_rt_section_index = i
            elif current_section == '[EasyRPG]':
                easyrpg_section_index = i
        elif current_section == '[RPG_RT]' and line_strip.lower() == 'fullpackageflag=1':
            full_package_found = True
        elif current_section == '[EasyRPG]' and line_strip.lower() == f'encoding={target_encoding_code}':
            encoding_found = True

    # --- 构建新内容 ---
    output_lines = list(lines) # 创建副本以修改

    # 1. 添加 FullPackageFlag=1
    if rpg_rt_section_index != -1 and not full_package_found:
        # 找到 [RPG_RT] 段落的结束位置（下一个段落或文件末尾）
        insert_fpf_at = len(output_lines)
        for j in range(rpg_rt_section_index + 1, len(output_lines)):
            if output_lines[j].strip().startswith('['):
                insert_fpf_at = j
                break
        output_lines.insert(insert_fpf_at, "FullPackageFlag=1\n")
        log.info("在 [RPG_RT] 段落中添加 FullPackageFlag=1")
        needs_write = True
    elif rpg_rt_section_index == -1:
         log.warning("RPG_RT.ini 中未找到 [RPG_RT] 段落，无法添加 FullPackageFlag。")


    # 2. 添加 Encoding=936
    if not encoding_found:
        if easyrpg_section_index != -1: # 如果 [EasyRPG] 段落存在
            insert_enc_at = len(output_lines)
            for j in range(easyrpg_section_index + 1, len(output_lines)):
                if output_lines[j].strip().startswith('['):
                    insert_enc_at = j
                    break
            output_lines.insert(insert_enc_at, f"Encoding={target_encoding_code}\n")
            log.info(f"在 [EasyRPG] 段落中添加 Encoding={target_encoding_code}")
        else: # 如果 [EasyRPG] 段落不存在，在文件末尾添加
            # 确保末尾有换行符
            if output_lines and not output_lines[-1].endswith('\n'):
                 output_lines[-1] = output_lines[-1].rstrip() + '\n'
            # 可能需要在前面加一个空行
            if output_lines and output_lines[-1].strip(): # 如果最后一行不是空行
                 output_lines.append('\n')
            output_lines.append("[EasyRPG]\n")
            output_lines.append(f"Encoding={target_encoding_code}\n")
            log.info(f"添加 [EasyRPG] 段落和 Encoding={target_encoding_code}")
        needs_write = True

    # --- 写回文件 ---
    if needs_write:
        try:
            # 清理空行
            final_output_lines = [l for l in output_lines if l.strip()]
            # 确保段落间有空行（可选美化）
            formatted_lines = []
            for i, line in enumerate(final_output_lines):
                 formatted_lines.append(line)
                 if line.strip().startswith('[') and i > 0 and final_output_lines[i-1].strip():
                     formatted_lines.insert(-1, '\n') # 在段落前插入空行

            with open(ini_path, 'w', encoding='gbk', errors='replace') as f:
                # f.writelines(final_output_lines)
                f.writelines(formatted_lines) # 使用格式化后的行
            log.info("RPG_RT.ini 更新完成。")
            return True # 表示已修改
        except Exception as e:
            log.error(f"写回更新后的 RPG_RT.ini 失败 ({ini_path}): {e}")
            return False # 修改失败
    else:
        log.info("RPG_RT.ini 无需修改。")
        return False # 表示未修改

# --- 主任务函数 ---
def run_initialize(game_path: str, rtp_options: dict, broker: MessageBroker) -> None:
    """执行游戏初始化流程：复制 EasyRPG，安装 RTP，转换编码，更新 ini。"""
    try:
        broker.status("正在初始化游戏环境...")
        broker.log("步骤 0: 开始初始化...")

        detected = detect_game_engine(game_path)
        if detected and detected.engine == "vxace":
            broker.log("检测到 RPG Maker VX Ace：初始化步骤自动跳过（无需 EasyRPG/RTP/编码转换）。", "success")
            broker.success("初始化完成（VX Ace：跳过）")
            broker.status("初始化完成（VX Ace：跳过）")
            broker.done()
            return

        # 1. 复制 EasyRPG 文件
        broker.log("复制 EasyRPG 文件...")
        success_easyrpg, copied_e, skipped_e = easyrpg.copy_easyrpg_files(game_path)
        if success_easyrpg:
            broker.log(f"EasyRPG 文件复制完成 (复制 {copied_e}, 跳过 {skipped_e})", "success")
        else:
            broker.log("EasyRPG 文件复制过程中出现错误。", "error")

        # 2. 安装 RTP 文件
        selected_rtps = [name + ".zip" for name, selected in rtp_options.items() if selected]
        if selected_rtps:
            broker.log(f"安装选定的 RTP 文件: {', '.join(selected_rtps)}")
            success_rtp = rtp.install_rtp_files(game_path, selected_rtps)
            if success_rtp:
                broker.log("RTP 文件安装完成。", "success")
            else:
                broker.log("RTP 文件安装过程中出现错误。", "error")
        else:
            broker.log("未选择任何 RTP 文件进行安装。", "warning")

        # 3. 转换文本文件编码 (日文 Shift-JIS/EUC-JP -> GBK)
        broker.log("检查并转换文本文件编码 (日文 -> GBK)...")
        converted_count = 0
        checked_count = 0
        target_files = [item for item in os.listdir(game_path)
                        if os.path.isfile(os.path.join(game_path, item)) and
                        (item.lower().endswith('.txt') or item.lower().endswith('.ini'))]

        for filename in target_files:
            file_path = os.path.join(game_path, filename)
            checked_count += 1
            converted, _ = _detect_and_convert_encoding(file_path, target_encoding='gbk')
            if converted:
                converted_count += 1
        broker.log(f"编码检查完成: 检查 {checked_count} 个文件，转换 {converted_count} 个。", "success")

        # 4. 检查并更新 RPG_RT.ini
        ini_path = os.path.join(game_path, "RPG_RT.ini")
        broker.log("检查并更新 RPG_RT.ini 配置...")
        _update_rpg_rt_ini(ini_path, target_encoding_code='936')

        broker.success("初始化完成")
        broker.status("初始化完成")
        broker.done()

    except Exception as e:
        log.exception("初始化任务执行期间发生意外错误。")
        broker.error(f"初始化过程中发生严重错误: {e}")
        broker.status("初始化失败")
        broker.done()
