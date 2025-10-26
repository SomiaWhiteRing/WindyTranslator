import os
import csv
import logging
from typing import Dict, Set, Tuple, Optional

from core.utils.file_system import get_application_path
from . import file_system

log = logging.getLogger(__name__)

# 固定位置：modules/dict 下的默认数据库映射文件
BASE_DICT_DIR = os.path.join(get_application_path(), "modules", "dict")
DEFAULT_DB_FILENAME = "default_database_dictionary.csv"
DEFAULT_DB_PATH = os.path.join(BASE_DICT_DIR, DEFAULT_DB_FILENAME)


def _load_from_modules_csv() -> Tuple[Dict[str, str], Set[str]]:
    mapping: Dict[str, str] = {}
    originals: Set[str] = set()
    if not os.path.exists(DEFAULT_DB_PATH):
        log.info(f"未找到默认数据库映射文件: {DEFAULT_DB_PATH}")
        return mapping, originals
    try:
        with open(DEFAULT_DB_PATH, 'r', encoding='utf-8-sig', newline='') as f:
            reader = csv.DictReader(f)
            headers = reader.fieldnames or []
            if '原文' not in headers:
                log.warning(f"默认数据库映射文件表头异常: {headers}")
            for row in reader:
                original = (row.get('原文') or '').strip()
                if not original:
                    continue
                originals.add(original)
                trans = (row.get('译文') or '').strip()
                if trans:
                    mapping[original] = trans
        log.info(f"已从默认数据库映射文件加载：原文 {len(originals)}，译文 {len(mapping)}。")
    except Exception as e:
        log.exception(f"读取默认数据库映射文件失败: {DEFAULT_DB_PATH} - {e}")
    return mapping, originals


def load_default_db_mapping() -> Tuple[Dict[str, str], Set[str]]:
    """固定位置加载默认数据库映射 (modules/dict/default_database_dictionary.csv)。"""
    # 确保目录存在（仅在部署环境初始化时有用）
    file_system.ensure_dir_exists(BASE_DICT_DIR)
    return _load_from_modules_csv()


def should_exclude_text(text: Optional[str], default_originals: Set[str]) -> bool:
    if not text or not isinstance(text, str):
        return False
    return text in default_originals


def get_prefill_for_text(original: str,
                         mapping: Dict[str, str],
                         original_marker: Optional[str] = None,
                         speaker_id: Optional[str] = None
                         ) -> Optional[dict]:
    if original not in mapping:
        return None
    return {
        'text': mapping[original],
        'status': 'success',  # 对后续流程最兼容
        'failure_context': None,
        'original_marker': original_marker or 'UnknownMarker',
        'speaker_id': speaker_id
    }
