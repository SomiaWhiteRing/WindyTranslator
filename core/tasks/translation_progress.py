"""File-oriented view of the same source and result files used by the translator."""
import csv
import json
import os
import re
from collections import defaultdict
from pathlib import Path

from core.engines import wolf
from core.tasks import supplement
from core.tasks.translate import _is_reusable_translation_result, _reuse_translation_result
from core.utils import control_tokens, output_mode
from core.utils.file_system import write_json_atomic
from core.utils.task_control import WorkBusyError, exclusive_work
from tools.translation_issue_review.paused_review import load_paused_entries


class ProgressProject:
    def __init__(self, game, works_dir, encoding):
        self.game, self.works_dir = game, works_dir
        work = output_mode.project_settings_path(game, works_dir).parent
        source = work / "untranslated" / "translation.json"
        self.output = work / "translated" / "translation_translated.json"
        self.source = {}
        self.results = {}
        self.issues = {}
        self.groups = defaultdict(list)
        self.labels = {}
        self.missing = {}
        self.busy = False
        if not source.is_file() or not Path(game).is_dir():
            return
        self.source = json.loads(source.read_text(encoding="utf-8-sig"))
        if self.output.is_file():
            self.results = json.loads(self.output.read_text(encoding="utf-8-sig"))
        self.issues = load_paused_entries(self.output, self.results)
        self.profile = control_tokens.profile_from_game(game)
        maps = {}
        rm = (Path(game) / "RPG_RT.lmt").is_file()
        self.scan_error = ""
        if rm:
            try:
                with exclusive_work(game, works_dir):
                    data = supplement.inventory(game, works_dir, encoding)
                self.missing, maps = data["missing"], data["maps"]
            except WorkBusyError:
                self.busy = True
            except (ValueError, RuntimeError, OSError) as error:
                self.scan_error = str(error)
        for name, items in self.source.items():
            group, label = name, ""
            path = Path(name)
            if rm:
                if re.fullmatch(r"map\d+\.txt", name, re.I):
                    group, label = str(path.with_suffix(".lmu")), maps.get(name.lower(), "")
                elif path.as_posix().lower() == "database/maptree.txt":
                    group, label = "RPG_RT.lmt", "地图树"
                elif name.lower() == "title.txt":
                    group, label = "RPG_RT.ini", "游戏配置"
                elif path.parts[0].lower() == "database":
                    group, label = "RPG_RT.ldb", "数据库"
            if items:
                self.labels[group] = label
                self.groups[group].extend((name, key) for key in items)

    def state(self, pair):
        name, key = pair
        result = self.results.get(name, {}).get(key, {})
        if _is_reusable_translation_result(result, self.source[name][key]):
            return "done"
        if pair in self.issues or result.get("status") == "fallback":
            return "issue"
        return "new"

    def translation(self, pair):
        name, key = pair
        if pair in self.issues:
            return self.issues[pair][1]
        return self.results.get(name, {}).get(key, {}).get("text", "")

    def reason(self, pair):
        name, key = pair
        if pair in self.issues:
            return self.issues[pair][2]
        return self.results.get(name, {}).get(key, {}).get("failure_context") or ""

    def validate(self, pair, text):
        name, key = pair
        if not text.strip():
            return "译文为空"
        if text.count("\n") != key.count("\n"):
            return "译文行数与原文不一致"
        ok, reason = control_tokens.validate_restored_text(key, text, profile=self.profile)
        if ok and self.source[name][key].get("wolf_codes"):
            ok, reason = wolf.validate_translation_transport(key, text, wolf.get_optional_transport_tags(self.game))
        return "" if ok else reason

    def save(self, edits):
        with exclusive_work(self.game, self.works_dir):
            results = json.loads(self.output.read_text(encoding="utf-8-sig")) if self.output.exists() else {}
            for (name, key), value in edits.items():
                problem = self.validate((name, key), value)
                if problem:
                    raise ValueError(problem)
                rows = results.setdefault(name, {})
                result = dict(rows.get(key, {}), text=value, manual=True)
                rows[key] = _reuse_translation_result(result, self.source[name][key])
            write_json_atomic(self.output, results)
            self.results = results
            for pair in edits:
                self.issues.pop(pair, None)
            corrections = self.output.with_name("fallback_corrections.csv")
            if corrections.is_file():
                with corrections.open(encoding="utf-8-sig", newline="") as stream:
                    rows = list(csv.reader(stream))
                kept = [row for row in rows if tuple(row[:2]) not in edits]
                if len(kept) != len(rows):
                    tmp = corrections.with_suffix(".csv.tmp")
                    with tmp.open("w", encoding="utf-8-sig", newline="") as stream:
                        csv.writer(stream, quoting=csv.QUOTE_ALL).writerows(kept)
                    os.replace(tmp, corrections)
