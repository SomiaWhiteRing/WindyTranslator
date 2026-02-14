"""Compare script output vs user's manual corrections."""
import json, sys
sys.stdout.reconfigure(encoding='utf-8')

corrected_path = r"c:\Users\旻\Documents\GitHub\RPGRewriter-Ownuse\Works\もしもコレクション6\translated\translation_translated.json"
test_path = r"c:\Users\旻\Documents\GitHub\RPGRewriter-Ownuse\Works\もしもコレクション6\translated\translation_translated_test.json"
base_path = r"c:\Users\旻\Documents\GitHub\RPGRewriter-Ownuse\Works\もしもコレクション6\translated\translation_translated_base.json"

with open(corrected_path, 'r', encoding='utf-8') as f:
    corrected = json.load(f)
with open(test_path, 'r', encoding='utf-8') as f:
    test = json.load(f)
with open(base_path, 'r', encoding='utf-8') as f:
    base = json.load(f)

_FW = '\u3000'

diffs = []
for map_name in base:
    if map_name not in corrected:
        continue
    for orig_key in base[map_name]:
        if orig_key not in corrected[map_name]:
            continue
        b_info = base[map_name][orig_key]
        c_info = corrected[map_name][orig_key]
        if not isinstance(b_info, dict) or not isinstance(c_info, dict):
            continue
        b_text = b_info.get("text", "")
        c_text = c_info.get("text", "")
        if b_text == c_text:
            continue
        if b_text.replace('\n','').replace(_FW,'') != c_text.replace('\n','').replace(_FW,''):
            continue
        if b_info.get("original_marker") != "Message":
            continue
        t_text = ""
        if map_name in test and orig_key in test[map_name]:
            t_info = test[map_name][orig_key]
            if isinstance(t_info, dict):
                t_text = t_info.get("text", "")
        diffs.append((map_name, orig_key, b_text, c_text, t_text))

match = 0
mismatch = 0
unchanged = 0

for idx, (mn, ok, b, c, t) in enumerate(diffs):
    if t == c:
        match += 1
        print(f"  E{idx+1:2d} MATCH")
    elif t == b:
        unchanged += 1
        print(f"  E{idx+1:2d} UNCHANGED (script didn't modify)")
    else:
        mismatch += 1
        print(f"  E{idx+1:2d} MISMATCH")
        c_lines = c.split('\n')
        t_lines = t.split('\n')
        max_lines = max(len(c_lines), len(t_lines))
        for li in range(max_lines):
            cl = c_lines[li] if li < len(c_lines) else "<missing>"
            tl = t_lines[li] if li < len(t_lines) else "<missing>"
            marker = " <<" if cl != tl else ""
            if marker:
                print(f"       USER: {cl}")
                print(f"       ALGO: {tl}")

print(f"\nTotal: {len(diffs)}, Match: {match}, Mismatch: {mismatch}, Unchanged: {unchanged}")
print(f"Match rate: {match/len(diffs)*100:.1f}%")
