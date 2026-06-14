import csv
import json

import pytest

from ui.translation_review_dialog import (
    FALLBACK_CSV_HEADER,
    FallbackRecord,
    UnsupportedFallbackCsvError,
    load_fallback_records,
    save_integrated_review,
    scan_translation_data,
    translation_json_has_reviewable_issues,
)


def _write_csv(path, header, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_ALL)
        writer.writerow(header)
        writer.writerows(rows)


def _sample_data():
    return {
        "Map001.txt": {
            "短い": {
                "text": "短い",
                "status": "fallback",
                "failure_context": "残留日语",
                "original_marker": "Choice",
                "speaker_id": None,
            },
            "長い": {
                "text": "这是一个非常非常非常非常非常非常非常非常非常非常长的句子",
                "status": "success",
                "failure_context": None,
                "original_marker": "Message",
                "speaker_id": "NARRATION",
            },
        }
    }


def _no_issue_data():
    return {
        "Map001.txt": {
            "短い": {
                "text": "短句",
                "status": "success",
                "failure_context": None,
                "original_marker": "Message",
                "speaker_id": "NARRATION",
            }
        }
    }


def test_load_fallback_records_accepts_current_five_column_csv(tmp_path):
    csv_path = tmp_path / "fallback_corrections.csv"
    _write_csv(
        csv_path,
        FALLBACK_CSV_HEADER,
        [["Map001.txt", "短い", "Choice", "reason", ""]],
    )

    records = load_fallback_records(csv_path)

    assert records == [
        FallbackRecord(
            source_file_name="Map001.txt",
            original_text="短い",
            marker="Choice",
            reason="reason",
            correction="",
            raw_row=["Map001.txt", "短い", "Choice", "reason", ""],
        )
    ]


def test_load_fallback_records_rejects_old_three_column_csv(tmp_path):
    csv_path = tmp_path / "fallback_corrections.csv"
    _write_csv(csv_path, ["原文", "最终尝试结果", "修正译文"], [["短い", "reason", ""]])

    with pytest.raises(UnsupportedFallbackCsvError, match="旧版三列表"):
        load_fallback_records(csv_path)


def test_scan_translation_data_merges_fallback_and_message_overflow_without_duplicates():
    records = [
        FallbackRecord("Map001.txt", "短い", "Choice", "reason", "", []),
        FallbackRecord("Map001.txt", "長い", "Message", "reason2", "", []),
    ]

    entries = scan_translation_data(_sample_data(), records)

    assert len(entries) == 2
    by_key = {entry.key: entry for entry in entries}
    assert by_key[("Map001.txt", "短い")].is_fallback
    assert by_key[("Map001.txt", "短い")].marker == "Choice"
    assert by_key[("Map001.txt", "短い")].limit is None
    assert by_key[("Map001.txt", "長い")].is_fallback
    assert by_key[("Map001.txt", "長い")].is_over_limit


def test_scan_translation_data_skips_fallback_rows_missing_from_json():
    records = [
        FallbackRecord("Map999.txt", "不存在", "Message", "stale row", "", []),
    ]

    entries = scan_translation_data(_no_issue_data(), records)

    assert len(entries) == 1
    assert entries[0].key == ("Map001.txt", "短い")
    assert not entries[0].is_fallback


def test_integrated_save_updates_nested_json_and_removes_only_changed_fallback_rows(tmp_path):
    json_path = tmp_path / "translation_translated.json"
    csv_path = tmp_path / "fallback_corrections.csv"
    json_path.write_text(json.dumps(_sample_data(), ensure_ascii=False), encoding="utf-8")
    _write_csv(
        csv_path,
        FALLBACK_CSV_HEADER,
        [
            ["Map001.txt", "短い", "Choice", "reason", ""],
            ["Map001.txt", "長い", "Message", "reason2", ""],
        ],
    )
    records = load_fallback_records(csv_path)
    data = json.loads(json_path.read_text(encoding="utf-8"))
    entries = scan_translation_data(data, records)
    by_key = {entry.key: entry for entry in entries}
    by_key[("Map001.txt", "短い")].update_text("短句")

    result = save_integrated_review(json_path, data, entries, csv_path, records)

    saved = json.loads(json_path.read_text(encoding="utf-8"))
    assert isinstance(saved["Map001.txt"], dict)
    assert saved["Map001.txt"]["短い"]["text"] == "短句"
    assert saved["Map001.txt"]["短い"]["status"] == "success"
    assert saved["Map001.txt"]["短い"]["failure_context"] is None
    assert saved["Map001.txt"]["長い"]["status"] == "success"
    assert result.dirty_entries == 1
    assert result.corrected_fallback_entries == 1
    assert result.remaining_fallback_rows == 1
    remaining = load_fallback_records(csv_path)
    assert [record.key for record in remaining] == [("Map001.txt", "長い")]


def test_translation_json_has_reviewable_issues_detects_overflow_without_csv(tmp_path):
    json_path = tmp_path / "translation_translated.json"
    json_path.write_text(json.dumps(_sample_data(), ensure_ascii=False), encoding="utf-8")

    assert translation_json_has_reviewable_issues(json_path, None)


def test_translation_json_has_reviewable_issues_detects_fallback_without_overflow(tmp_path):
    json_path = tmp_path / "translation_translated.json"
    csv_path = tmp_path / "fallback_corrections.csv"
    json_path.write_text(json.dumps(_no_issue_data(), ensure_ascii=False), encoding="utf-8")
    _write_csv(
        csv_path,
        FALLBACK_CSV_HEADER,
        [["Map001.txt", "短い", "Message", "reason", ""]],
    )

    assert translation_json_has_reviewable_issues(json_path, csv_path)


def test_translation_json_has_reviewable_issues_ignores_stale_fallback_only_csv(tmp_path):
    json_path = tmp_path / "translation_translated.json"
    csv_path = tmp_path / "fallback_corrections.csv"
    json_path.write_text(json.dumps(_no_issue_data(), ensure_ascii=False), encoding="utf-8")
    _write_csv(
        csv_path,
        FALLBACK_CSV_HEADER,
        [["Map999.txt", "不存在", "Message", "reason", ""]],
    )

    assert not translation_json_has_reviewable_issues(json_path, csv_path)


def test_translation_json_has_reviewable_issues_returns_false_without_problems(tmp_path):
    json_path = tmp_path / "translation_translated.json"
    json_path.write_text(json.dumps(_no_issue_data(), ensure_ascii=False), encoding="utf-8")

    assert not translation_json_has_reviewable_issues(json_path, None)


def test_translation_json_has_reviewable_issues_enables_old_csv_for_error_prompt(tmp_path):
    json_path = tmp_path / "translation_translated.json"
    csv_path = tmp_path / "fallback_corrections.csv"
    json_path.write_text(json.dumps(_no_issue_data(), ensure_ascii=False), encoding="utf-8")
    _write_csv(csv_path, ["原文", "最终尝试结果", "修正译文"], [["短い", "reason", ""]])

    assert translation_json_has_reviewable_issues(json_path, csv_path)
