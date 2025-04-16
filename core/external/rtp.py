# core/external/rtp.py
import os
import zipfile
import tempfile
import logging
import shutil
from core.utils import file_system

log = logging.getLogger(__name__)

# RTP 集合源路径 (同样，最好由配置或 App 层提供)
PROGRAM_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RTP_COLLECTION_DIR = os.path.join(PROGRAM_DIR, "modules", "RTPCollection")

def install_rtp_files(target_game_dir, selected_rtp_zips):
    """
    解压选定的 RTP zip 文件并将内容安装到游戏目录。

    Args:
        target_game_dir (str): 目标游戏目录路径。
        selected_rtp_zips (list): 包含选定 RTP 文件名 (如 "2000.zip") 的列表。

    Returns:
        bool: 操作是否整体成功完成（所有选定RTP都处理完毕，即使部分文件跳过）。
               返回 False 如果遇到解压错误或主要复制错误。
    """
    if not os.path.isdir(RTP_COLLECTION_DIR):
        log.error(f"RTP 集合目录未找到: {RTP_COLLECTION_DIR}")
        return False
    if not os.path.isdir(target_game_dir):
        log.error(f"目标游戏目录不存在: {target_game_dir}")
        return False
    if not selected_rtp_zips:
        log.warning("未选择任何 RTP 文件进行安装。")
        return True # 没有选择也算“成功”完成

    overall_success = True
    log.info(f"开始安装 RTP 文件到 {target_game_dir}...")

    for rtp_zip_name in selected_rtp_zips:
        rtp_zip_path = os.path.join(RTP_COLLECTION_DIR, rtp_zip_name)

        if not os.path.exists(rtp_zip_path):
            log.error(f"找不到 RTP 文件: {rtp_zip_path}")
            overall_success = False # 标记有RTP文件缺失
            continue # 继续处理下一个

        log.info(f"正在处理 RTP 文件: {rtp_zip_name}")
        rtp_copied = 0
        rtp_skipped = 0
        temp_dir_obj = None # 用于确保 finally 中可以访问

        try:
            # 创建临时目录用于解压
            temp_dir_obj = tempfile.TemporaryDirectory(prefix="rtp_extract_")
            temp_dir_path = temp_dir_obj.name
            log.debug(f"为 {rtp_zip_name} 创建临时解压目录: {temp_dir_path}")

            # 解压 ZIP 文件
            with zipfile.ZipFile(rtp_zip_path, 'r') as zip_ref:
                zip_ref.extractall(temp_dir_path)
            log.debug(f"{rtp_zip_name} 已解压到临时目录。")

            # 遍历解压后的文件并复制到游戏目录
            log.debug(f"开始从临时目录复制文件到 {target_game_dir}")
            for root, dirs, files in os.walk(temp_dir_path):
                # 计算相对路径以确定目标目录结构
                relative_path = os.path.relpath(root, temp_dir_path)
                # 在目标游戏目录下创建对应的子目录（如果需要）
                current_target_dir = os.path.join(target_game_dir, relative_path) if relative_path != '.' else target_game_dir
                file_system.ensure_dir_exists(current_target_dir) # 创建目标子目录

                for file in files:
                    src_file_path = os.path.join(root, file)
                    dst_file_path = os.path.join(current_target_dir, file)

                    if not os.path.exists(dst_file_path):
                        if file_system.safe_copy(src_file_path, dst_file_path):
                            rtp_copied += 1
                        else:
                            log.warning(f"复制 RTP 文件失败（但继续）: {src_file_path} -> {dst_file_path}")
                            # 可以选择在这里将 overall_success 设为 False，如果单个文件失败算整体失败
                    else:
                        # log.debug(f"RTP 文件已存在，跳过: {dst_file_path}")
                        rtp_skipped += 1

            log.info(f"{rtp_zip_name} 处理完成: 复制 {rtp_copied} 个新文件，跳过 {rtp_skipped} 个已存在文件。")

        except zipfile.BadZipFile:
            log.error(f"解压 RTP 文件失败: {rtp_zip_path} 不是有效的 ZIP 文件。")
            overall_success = False
        except Exception as e:
            log.exception(f"处理 RTP 文件 {rtp_zip_name} 时发生意外错误: {e}")
            overall_success = False
        finally:
            # 清理临时目录
            if temp_dir_obj:
                try:
                    temp_dir_obj.cleanup()
                    log.debug(f"已清理临时目录: {temp_dir_obj.name}")
                except Exception as cleanup_err:
                    log.error(f"清理临时目录失败: {temp_dir_obj.name} - {cleanup_err}")

    log.info(f"所有选定的 RTP 文件处理完毕。")
    return overall_success