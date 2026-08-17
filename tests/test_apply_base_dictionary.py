import unittest

from core.tasks.apply_base_dictionary import _select_applicable_base_items


class ApplyBaseDictionaryTests(unittest.TestCase):
    def test_corresponding_original_must_be_available_or_added(self):
        items = [
            {"原文": "昵称", "译文": "Nick", "对应原名": "本名"},
            {"原文": "本名", "译文": "Main", "对应原名": ""},
            {"原文": "孤立昵称", "译文": "Orphan", "对应原名": "不存在"},
        ]

        selected = _select_applicable_base_items(items, set(), lambda term: 3 if term in {"本名", "昵称"} else 0)

        self.assertEqual([item["原文"] for item in selected], ["本名", "昵称"])


if __name__ == "__main__":
    unittest.main()
