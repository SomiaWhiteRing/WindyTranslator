"""Translation error log analyzer.

Reads translation_errors.log and aggregates counts by error type and root cause.
"""
import argparse
import collections
import json
import re
from pathlib import Path

ENTRY_START_RE = re.compile(r'^\[(?P<timestamp>[^\]]+)] (?P<error_type>.+?) \(尝试 (?P<attempt>\d+)/(?:\d+)\)')
KEY_VALUE_RE = re.compile(r'^\s{2}(?P<key>[^:：]+)[：:][ ]?(?P<value>.*)$')
SEPARATOR_LINE = '-' * 20


def extract_reason_core(error_type: str, raw_reason: str) -> str:
    """Reduce verbose failure descriptions to a comparable core reason."""
    if not raw_reason:
        return ''
    reason = raw_reason.strip()
    prefix = f"{error_type}:"
    if reason.startswith(prefix):
        reason = reason[len(prefix):].strip()
    if reason.startswith('失败原因'):
        reason = reason.split('失败原因', 1)[-1].lstrip('：: ').strip()
    match = re.search(r'验证失败[:：]\s*([^。\n]+)', reason)
    if match:
        reason = match.group(1).strip()
    for token in ('。', ' (', '，原文', ' 原文', ';', '；'):
        idx = reason.find(token)
        if idx != -1:
            reason = reason[:idx]
            break
    reason = reason.strip()
    if reason.startswith('验证失败'):
        reason = reason.split('验证失败', 1)[-1].lstrip('：: ').strip()
    if reason.startswith('失败原因'):
        reason = reason.split('失败原因', 1)[-1].lstrip('：: ').strip()
    return reason


def parse_log(path: Path):
    entries = []
    current = None
    with path.open('r', encoding='utf-8') as fh:
        for line in fh:
            line = line.rstrip('\n')
            start_match = ENTRY_START_RE.match(line)
            if start_match:
                if current:
                    entries.append(current)
                current = {
                    'timestamp': start_match.group('timestamp'),
                    'error_type': start_match.group('error_type').strip(),
                    'attempt': int(start_match.group('attempt')),
                }
                continue
            if current is None:
                continue
            if line == SEPARATOR_LINE:
                entries.append(current)
                current = None
                continue
            kv_match = KEY_VALUE_RE.match(line)
            if kv_match:
                key = kv_match.group('key').strip()
                value = kv_match.group('value').strip()
                current[key] = value
    if current:
        entries.append(current)
    return entries


def summarize(entries, top_n=10):
    error_counter = collections.Counter()
    reason_counter = collections.Counter()
    file_counter = collections.Counter()
    for entry in entries:
        error_type = entry.get('error_type', '未知错误')
        error_counter[error_type] += 1
        failure_reason = entry.get('失败原因')
        core_reason = extract_reason_core(error_type, failure_reason)
        if core_reason:
            reason_counter[core_reason] += 1
        if '所属文件' in entry:
            file_counter[entry['所属文件']] += 1
    total = sum(error_counter.values())
    return {
        'total_errors': total,
        'by_error_type': error_counter.most_common(),
        'top_reasons': reason_counter.most_common(top_n),
        'top_files': file_counter.most_common(top_n),
    }


def main():
    parser = argparse.ArgumentParser(description='Aggregate translation error log stats.')
    parser.add_argument('log_path', type=Path, help='Path to translation_errors.log')
    parser.add_argument('--top', type=int, default=10, help='Number of top items to display for reasons/files')
    parser.add_argument('--json', action='store_true', help='Output JSON instead of human-readable summary')
    args = parser.parse_args()

    if not args.log_path.exists():
        parser.error(f'File not found: {args.log_path}')

    entries = parse_log(args.log_path)
    stats = summarize(entries, top_n=args.top)

    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    else:
        total = stats['total_errors']
        print(f'总错误条目: {total}')
        print('\n按错误类型统计:')
        for error_type, count in stats['by_error_type']:
            pct = (count / total * 100) if total else 0
            print(f'  - {error_type}: {count} ({pct:.1f}%)')
        print('\n核心失败原因 Top {0}:'.format(args.top))
        if stats['top_reasons']:
            for reason, count in stats['top_reasons']:
                pct = (count / total * 100) if total else 0
                print(f'  - {reason}: {count} ({pct:.1f}%)')
        else:
            print('  (无数据)')
        print('\n受影响文件 Top {0}:'.format(args.top))
        if stats['top_files']:
            for filename, count in stats['top_files']:
                print(f'  - {filename}: {count}')
        else:
            print('  (无数据)')


if __name__ == '__main__':
    main()
