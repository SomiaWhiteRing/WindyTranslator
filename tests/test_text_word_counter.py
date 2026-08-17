import json
import os
import subprocess
import sys
from pathlib import Path


TOOL = Path("tools/text_word_counter/word_counter.py")


def test_word_counter_reports_paired_json_counts(tmp_path):
    original = tmp_path / "original.json"
    translated = tmp_path / "translated.json"
    original.write_text(json.dumps({"Map001.txt": {"a": {"text_to_translate": "原文一"}, "b": {"text_to_translate": "原文二"}}}, ensure_ascii=False), encoding="utf-8")
    translated.write_text(json.dumps({"Map001.txt": {"a": {"text": "译文"}}}, ensure_ascii=False), encoding="utf-8")
    result = subprocess.run([sys.executable, str(TOOL), "--original-json", str(original), "--translated-json", str(translated)], capture_output=True, text=True, encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert result.returncode == 0
    assert "原文条目数：2" in result.stdout
    assert "译文字数：2" in result.stdout
    assert "原文缺失条目数：1" in result.stdout


def test_word_counter_reports_missing_original(tmp_path):
    translated = tmp_path / "translated.json"
    translated.write_text("{}", encoding="utf-8")
    result = subprocess.run([sys.executable, str(TOOL), "--original-json", str(tmp_path / "missing.json"), "--translated-json", str(translated)], capture_output=True, text=True, encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert result.returncode == 2
    assert "缺少原文 JSON 文件" in result.stderr
