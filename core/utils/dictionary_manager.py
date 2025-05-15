# core/dictionary_manager.py
import os
import csv
import logging
from . import file_system # 从 utils 导入

log = logging.getLogger(__name__)

# --- 基础字典文件路径常量 ---
# 假设 PROGRAM_DIR 是项目根目录，如果不是，需要调整获取方式
PROGRAM_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BASE_DICT_DIR = os.path.join(PROGRAM_DIR, "modules", "dict")
BASE_CHARACTER_DICT_FILENAME = "base_character_dictionary.csv"
BASE_ENTITY_DICT_FILENAME = "base_entity_dictionary.csv"
BASE_CHARACTER_DICT_PATH = os.path.join(BASE_DICT_DIR, BASE_CHARACTER_DICT_FILENAME)
BASE_ENTITY_DICT_PATH = os.path.join(BASE_DICT_DIR, BASE_ENTITY_DICT_FILENAME)

# --- 基础字典表头常量 (与 DictEditorWindow 中的对应) ---
BASE_CHARACTER_HEADERS = ['原文', '译文', '对应原名', '性别', '年龄', '性格', '口吻', '描述']
BASE_ENTITY_HEADERS = ['原文', '译文', '类别', '描述']


def _load_single_base_dict(file_path, expected_headers):
    """
    加载单个基础字典文件。

    Args:
        file_path (str): 基础字典 CSV 文件路径。
        expected_headers (list): 期望的 CSV 表头。

    Returns:
        list[dict]: 解析后的字典数据列表，如果文件不存在或无效则返回空列表。
    """
    if not os.path.exists(file_path):
        log.warning(f"基础字典文件未找到: {file_path}")
        return []
    
    data = []
    try:
        with open(file_path, 'r', encoding='utf-8-sig', newline='') as f:
            reader = csv.DictReader(f)
            # 可选：简单的表头验证
            if not reader.fieldnames or not all(h in reader.fieldnames for h in expected_headers[:2]): #至少检查前两个
                log.warning(f"基础字典文件 {file_path} 表头不匹配或不完整。字段: {reader.fieldnames}")
                # 即使表头不完全匹配，也尝试加载，由调用者处理
            
            for row_dict in reader:
                # 确保至少有'原文'，并且不为空
                if row_dict.get('原文', '').strip():
                    data.append(dict(row_dict)) # 转换为普通 dict
                else:
                    log.debug(f"跳过基础字典文件 {file_path} 中的空原文行: {row_dict}")
        log.info(f"成功从 {file_path} 加载 {len(data)} 条基础字典条目。")
        return data
    except Exception as e:
        log.exception(f"加载基础字典文件 {file_path} 失败: {e}")
        return []

def load_base_dictionaries():
    """
    加载人物和事物基础字典。

    Returns:
        tuple: (character_dict_data, entity_dict_data)
               两个列表，每个列表包含从对应基础字典文件加载的字典条目。
    """
    file_system.ensure_dir_exists(BASE_DICT_DIR) # 确保目录存在
    
    # 如果文件不存在，尝试创建带表头的空文件
    if not os.path.exists(BASE_CHARACTER_DICT_PATH):
        _create_empty_base_dict_file(BASE_CHARACTER_DICT_PATH, BASE_CHARACTER_HEADERS)
    if not os.path.exists(BASE_ENTITY_DICT_PATH):
        _create_empty_base_dict_file(BASE_ENTITY_DICT_PATH, BASE_ENTITY_HEADERS)

    char_data = _load_single_base_dict(BASE_CHARACTER_DICT_PATH, BASE_CHARACTER_HEADERS)
    entity_data = _load_single_base_dict(BASE_ENTITY_DICT_PATH, BASE_ENTITY_HEADERS)
    return char_data, entity_data

def _save_single_base_dict(file_path, dict_data, headers):
    """
    保存单个基础字典数据到文件。

    Args:
        file_path (str): 目标 CSV 文件路径。
        dict_data (list[dict]): 要保存的字典数据列表。
        headers (list): CSV 文件的表头。

    Returns:
        bool: True 如果保存成功，False 否则。
    """
    try:
        file_system.ensure_dir_exists(os.path.dirname(file_path)) # 确保目录存在
        with open(file_path, 'w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=headers, quoting=csv.QUOTE_ALL, extrasaction='ignore')
            writer.writeheader()
            if dict_data: # 确保 dict_data 不是 None 或空
                writer.writerows(dict_data)
        log.info(f"基础字典数据已保存到: {file_path}")
        return True
    except Exception as e:
        log.exception(f"保存基础字典文件 {file_path} 失败: {e}")
        return False

def save_base_dictionaries(char_dict_data, entity_dict_data):
    """
    保存人物和事物基础字典数据。

    Args:
        char_dict_data (list[dict]): 人物基础字典数据。
        entity_dict_data (list[dict]): 事物基础字典数据。

    Returns:
        bool: True 如果两个字典都成功保存，False 否则。
    """
    char_saved = _save_single_base_dict(BASE_CHARACTER_DICT_PATH, char_dict_data, BASE_CHARACTER_HEADERS)
    entity_saved = _save_single_base_dict(BASE_ENTITY_DICT_PATH, entity_dict_data, BASE_ENTITY_HEADERS)
    return char_saved and entity_saved

def _create_empty_base_dict_file(file_path, headers):
     """如果基础字典文件不存在，则创建一个带指定表头的空文件。"""
     try:
         # 确保目录存在
         file_system.ensure_dir_exists(os.path.dirname(file_path))
         with open(file_path, 'w', encoding='utf-8-sig', newline='') as f:
             writer = csv.writer(f, quoting=csv.QUOTE_ALL)
             writer.writerow(headers)
         log.info(f"已创建空的基础字典文件: {file_path}")
         return True
     except Exception as e:
         log.exception(f"创建空基础字典文件失败: {file_path} - {e}")
         # 不在这里 messagebox，让调用者处理
         return False