import csv
import json

from tools.translation_issue_review.review_app import (
    FALLBACK_CSV_HEADER,
    load_fallback_records,
    save_integrated_review,
    scan_translation_data,
    translation_json_has_reviewable_issues,
)


def _write_csv(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream, quoting=csv.QUOTE_ALL)
        writer.writerow(FALLBACK_CSV_HEADER)
        writer.writerows(rows)


def _data():
    return {"Map001.txt": {
        "短い": {"text": "短い", "status": "fallback", "failure_context": "原因", "original_marker": "Choice", "speaker_id": None},
        "長い": {"text": "这是一个非常非常非常非常非常非常非常非常非常非常长的句子", "status": "success", "failure_context": None, "original_marker": "Message", "speaker_id": "NARRATION"},
    }}


def test_integrated_save_updates_json_and_removes_corrected_fallback(tmp_path):
    json_path = tmp_path / "translation_translated.json"
    csv_path = tmp_path / "fallback_corrections.csv"
    json_path.write_text(json.dumps(_data(), ensure_ascii=False), encoding="utf-8")
    _write_csv(csv_path, [["Map001.txt", "短い", "Choice", "reason", ""], ["Map001.txt", "长", "Message", "reason2", ""]])
    records = load_fallback_records(csv_path)
    data = json.loads(json_path.read_text(encoding="utf-8"))
    entries = scan_translation_data(data, records)
    next(entry for entry in entries if entry.key == ("Map001.txt", "短い")).update_text("短句")

    result = save_integrated_review(json_path, data, entries, csv_path, records)

    saved = json.loads(json_path.read_text(encoding="utf-8"))
    assert saved["Map001.txt"]["短い"]["text"] == "短句"
    assert result.corrected_fallback_entries == 1
