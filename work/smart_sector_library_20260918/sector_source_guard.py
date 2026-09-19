"""Reject analyst-invented primary sectors; no requests or inferred mappings."""
import json
import re
from pathlib import Path


def normalized_name(value):
    return re.sub(r'[ⅡⅢ]+$', '', value.strip()).removesuffix('概念')


def source_candidates(root, instrument_id):
    root = Path(root)
    company = root / 'full_market' / (instrument_id + '.json')
    em = json.loads(company.read_text(encoding='utf-8')) if company.exists() else {}
    result = {'东方财富': set(em.get('source', {}).get('memberships', [])), '同花顺': set()}
    for suffix in ('.browser.json', '.json', '.search.json'):
        path = root / 'ths_sequential' / (instrument_id + suffix)
        if not path.exists():
            continue
        raw = json.loads(path.read_text(encoding='utf-8'))
        if raw.get('status') != 'read':
            continue
        if raw.get('concepts'):
            result['同花顺'].update(row['name'].strip() for row in raw['concepts'])
        else:
            body = raw.get('body', '')
            blocks = body.split('-' * 80) if suffix == '.search.json' else [body]
            for block in blocks:
                if suffix == '.search.json' and not any(
                    f'https://basic.10jqka.com.cn/{instrument_id[:6]}/concept.html' in line
                    for line in block.splitlines()[:2]
                ):
                    continue
                for line in block.splitlines():
                    line = re.sub(r'^L\d+:\s*', '', line)
                    match = re.match(r'\s*\d+\s*\|\s*([^|]+?)\s*\|', line)
                    if match:
                        result['同花顺'].add(match.group(1).strip())
        break
    return result


def validate_source_sector(root, judgment):
    if not judgment.get('sector'):
        return None
    source_name = judgment.get('source_sector_name')
    publisher = {'ths_concept': '同花顺', 'eastmoney_f10': '东方财富'}.get(
        judgment.get('source_sector_provider'), judgment.get('source_sector_provider'))
    candidates = source_candidates(root, judgment['id'])
    if not source_name or publisher not in candidates or source_name not in candidates[publisher]:
        raise ValueError(f"{judgment['id']}: primary requires an existing source sector and publisher")
    if normalized_name(judgment['sector']) != normalized_name(source_name):
        raise ValueError(f"{judgment['id']}: invented sector {judgment['sector']!r} does not match {source_name!r}")
    return publisher, source_name
