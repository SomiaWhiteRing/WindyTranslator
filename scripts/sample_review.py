"""Sample random modified entries for review."""
import json, random, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from proofread_linebreaks import proofread_entry

random.seed(99)
data = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))

modified = []
for entries in data.values():
    if not isinstance(entries, dict):
        continue
    for orig, info in entries.items():
        if not isinstance(info, dict):
            continue
        if info.get('original_marker') != 'Message' or '\n' not in orig:
            continue
        text = info.get('text', '')
        result = proofread_entry(text, orig, info.get('speaker_id'))
        if result != text:
            modified.append((orig[:40].replace('\n', '\\n'), text, result))

sample = random.sample(modified, min(30, len(modified)))
U = chr(0x2936)
report = Path(sys.argv[1]).with_name('translation_translated_proofread_report.txt')
with report.open('w', encoding='utf-8') as f:
    f.write(f'Random sample (seed=99): {len(sample)} / {len(modified)} modified\n')
    f.write('=' * 60 + '\n\n')
    for i, (key, before, after) in enumerate(sample, 1):
        f.write(f'{i}. Key: {key}...\n')
        f.write(f'   Before: {before.replace(chr(10), U)}\n')
        f.write(f'   After:  {after.replace(chr(10), U)}\n\n')
print(f'Wrote {len(sample)} samples to {report}')
