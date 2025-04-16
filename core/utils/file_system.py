# core/utils/file_system.py
import os
import shutil
import logging

log = logging.getLogger(__name__)

def ensure_dir_exists(dir_path):
    """确保目录存在，如果不存在则创建它。"""
    if not os.path.exists(dir_path):
        try:
            os.makedirs(dir_path)
            log.info(f"已创建目录: {dir_path}")
            return True
        except OSError as e:
            log.error(f"创建目录失败: {dir_path} - {e}")
            return False
    return True

def safe_copy(src, dst):
    """安全地复制文件，记录日志并处理异常。"""
    try:
        shutil.copy2(src, dst) # copy2 保留元数据
        log.debug(f"文件已复制: {src} -> {dst}")
        return True
    except Exception as e:
        log.error(f"复制文件失败: {src} -> {dst} - {e}")
        return False

def safe_move(src, dst):
    """安全地移动文件或目录，记录日志并处理异常。"""
    try:
        shutil.move(src, dst)
        log.debug(f"文件/目录已移动: {src} -> {dst}")
        return True
    except Exception as e:
        log.error(f"移动文件/目录失败: {src} -> {dst} - {e}")
        return False

def safe_remove(path):
    """安全地删除文件或目录（递归），记录日志并处理异常。"""
    try:
        if os.path.isfile(path) or os.path.islink(path):
            os.remove(path)
            log.debug(f"文件/链接已删除: {path}")
        elif os.path.isdir(path):
            shutil.rmtree(path)
            log.debug(f"目录已删除: {path}")
        else:
            log.warning(f"尝试删除不存在或类型未知的路径: {path}")
            return False # Indicate path didn't exist or wasn't file/dir
        return True
    except Exception as e:
        log.error(f"删除失败: {path} - {e}")
        return False

# 可以在这里添加更多文件系统相关的辅助函数，例如：
# - get_relative_path(base_path, target_path)
# - find_files(directory, pattern)
# - read_file_content(file_path, encoding='utf-8')
# - write_file_content(file_path, content, encoding='utf-8')