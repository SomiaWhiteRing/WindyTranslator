import json
import os
import subprocess
import sys
from pathlib import Path


TOOL = Path("tools/text_word_counter/word_counter.py")


def test_word_counter_reports_original_json_stats(tmp_path):
    original = tmp_path / "original.json"
    original.write_text(json.dumps({"Map001.txt": {"a": {"text_to_translate": "原文 一\n"}, "b": {"text_to_translate": "   "}}}, ensure_ascii=False), encoding="utf-8")
    result = subprocess.run([sys.executable, str(TOOL), "--mode", "original", "--original-json", str(original)], capture_output=True, text=True, encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert result.returncode == 0
    assert "原文文件数：1" in result.stdout
    assert "原文条目数：2" in result.stdout
    assert "原文字数：3" in result.stdout
    assert "原文字符数（不计空格）：3" in result.stdout
    assert "原文字符数（计空格）：8" in result.stdout
    assert "原文段落数：1" in result.stdout
    assert "原文显式行数：2" in result.stdout


def test_word_counter_reports_missing_original(tmp_path):
    result = subprocess.run([sys.executable, str(TOOL), "--mode", "original", "--original-json", str(tmp_path / "missing.json")], capture_output=True, text=True, encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert result.returncode == 2
    assert "缺少原文 JSON 文件" in result.stderr


def test_word_counter_original_mode_only_reads_original(tmp_path):
    original = tmp_path / "original.json"
    original.write_text(json.dumps({"Map001.txt": {"a": {"text_to_translate": "原文"}}}, ensure_ascii=False), encoding="utf-8")
    result = subprocess.run([sys.executable, str(TOOL), "--mode", "original", "--original-json", str(original)], capture_output=True, text=True, encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert result.returncode == 0
    assert "原文条目数：1" in result.stdout
    assert "译文条目数" not in result.stdout


def test_word_counter_translated_mode_requires_only_translated(tmp_path):
    translated = tmp_path / "translated.json"
    translated.write_text(json.dumps({"Map001.txt": {"a": {"text": "译文"}}}, ensure_ascii=False), encoding="utf-8")
    result = subprocess.run([sys.executable, str(TOOL), "--mode", "translated", "--translated-json", str(translated)], capture_output=True, text=True, encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert result.returncode == 0
    assert "译文条目数：1" in result.stdout
    assert "原文条目数" not in result.stdout
