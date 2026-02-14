"""
统一 Map0009 中歌名翻译的小工具。
以【】条目的翻译为准，覆盖歌曲列表中的翻译。
"""

import json
import sys
import re

FILE = r"Works\028_DirtyHero\translated\translation_translated.json"


def main():
    with open(FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    map9 = data["Map0009.txt"]

    # 1) 从【】条目中收集"标准"歌名翻译
    #    格式: key = "【歌名】\n出典：xxx", value.text = "【翻译】\n出处：xxx"
    canonical: dict[str, str] = {}  # 日文歌名 -> 中文翻译
    for key, val in map9.items():
        m = re.match(r"^【(.+?)】", key)
        if m:
            jp_name = m.group(1)
            tm = re.match(r"^【(.+?)】", val["text"])
            if tm:
                zh_name = tm.group(1)
                canonical[jp_name] = zh_name

    print("=== 收集到的标准翻译 ===")
    for jp, zh in canonical.items():
        print(f"  {jp} -> {zh}")

    # 2) 找到歌曲列表条目（多行歌名列表，StringPicture）
    #    这些条目的 key 和 text 都是 \n 分隔的多行歌名
    changes = []
    for key, val in map9.items():
        if val.get("original_marker") != "StringPicture":
            continue
        # 歌曲列表条目的特征：key 中包含多个 \n 且不以【开头
        if key.startswith("【") or key.startswith("\n"):
            continue
        lines_jp = key.split("\n")
        lines_zh = val["text"].split("\n")
        if len(lines_jp) < 3 or len(lines_jp) != len(lines_zh):
            continue

        new_lines_zh = list(lines_zh)
        changed = False
        for i, jp_line in enumerate(lines_jp):
            # 处理带括号后缀的情况，如 "穏ヤカナ眠リ(前半)"
            # 先尝试完整匹配
            if jp_line in canonical:
                expected = canonical[jp_line]
                if lines_zh[i] != expected:
                    print(f"\n[修正] {jp_line}")
                    print(f"  旧: {lines_zh[i]}")
                    print(f"  新: {expected}")
                    new_lines_zh[i] = expected
                    changed = True
                continue

            # 尝试匹配 "歌名(后缀)" 的模式
            m = re.match(r"^(.+?)(\(.+?\))$", jp_line)
            if m:
                base_jp = m.group(1)
                suffix_jp = m.group(2)
                if base_jp in canonical:
                    # 在中文翻译中也找对应的后缀
                    mz = re.match(r"^(.+?)(\(.+?\))$", lines_zh[i])
                    if mz:
                        old_base_zh = mz.group(1)
                        suffix_zh = mz.group(2)
                        expected_base = canonical[base_jp]
                        if old_base_zh != expected_base:
                            expected = expected_base + suffix_zh
                            print(f"\n[修正] {jp_line}")
                            print(f"  旧: {lines_zh[i]}")
                            print(f"  新: {expected}")
                            new_lines_zh[i] = expected
                            changed = True

        if changed:
            new_text = "\n".join(new_lines_zh)
            changes.append((key, new_text))

    # 3) 同样检查描述文本中引用的歌名（「」或《》包裹的）
    #    这部分比较复杂，暂时跳过，只处理歌曲列表

    if not changes:
        print("\n没有需要修改的内容。")
        return

    print(f"\n=== 共 {len(changes)} 个条目需要修改 ===")

    # 应用修改
    for key, new_text in changes:
        map9[key]["text"] = new_text

    with open(FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

    print("已写入文件。")


if __name__ == "__main__":
    main()
