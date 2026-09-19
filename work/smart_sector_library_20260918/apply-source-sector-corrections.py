"""Apply individually reviewed existing-board corrections, retaining prior snapshot."""
import copy
import hashlib
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from sector_source_guard import normalized_name, source_candidates, validate_source_sector
from tradex.smart_sector_library.catalog import MarketSectorDossierV2
from tradex.smart_sector_library.storage import write_json

root = Path(__file__).parent
target = Path('tradex/src/tradex/smart_sector_library/market_membership_evidence.v2.json')
original = target.read_bytes()
payload = json.loads(original)
today = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
overrides = {}
for filename, collection in [('review-agent-035-054-source-corrections.json', 'changes'),
                             ('review-published-source-corrections.json', 'corrections')]:
    for change in json.loads((root / filename).read_text(encoding='utf-8'))[collection]:
        overrides[change['id']] = change['new_judgment']
overrides['000513.SZ']['stage'] = 'pilot'
for key, name, sector_key, source_name, publisher, reason in [
    ('002851.SZ', '数据中心(AIDC)', 'data_centers', '数据中心(AIDC)', '同花顺',
     '采用该股已有数据中心(AIDC)概念板块；AI算力是本次市场主线解释，产业链仍为AI服务器电源和数据中心供电。英伟达供货及电源产品证据保留，不再将AI算力解释文字另造为来源板块。'),
    ('000034.SZ', '东数西算(算力)', 'computing_power', '东数西算(算力)', '同花顺',
     '采用该股已有东数西算(算力)概念；自有服务器和智算基础设施与市场算力主题相互支持，不以IT分销收入否定已验证的算力产品，也不另造AI算力板块名称。'),
    ('000524.SZ', '旅游酒店', 'tourism_hotels', '旅游酒店', '东方财富',
     '采用该股已有旅游酒店板块，旅行社与酒店等实际运营业务支持旅游主线；不再将来源名称倒置为自建酒店旅游分类。')
]:
    overrides[key] = dict(id=key, sector=name, key=sector_key, source_sector_name=source_name,
                          source_sector_provider=publisher, reason=reason)

latest = {}
for index, entry in enumerate(payload['entries']):
    if entry['instrument_id'] not in latest or entry['effective_from'] >= payload['entries'][latest[entry['instrument_id']]]['effective_from']:
        latest[entry['instrument_id']] = index
audit = []
for key, index in latest.items():
    previous = payload['entries'][index]
    primaries = [c for c in previous['candidates'] if c['market_role'] == 'primary']
    if not primaries:
        continue
    assert len(primaries) == 1
    old_primary = primaries[0]
    judgment = dict(id=key, sector=old_primary['sector_name'], key=old_primary['sector_key'])
    judgment.update(overrides.get(key, {}))
    if not judgment.get('source_sector_name'):
        candidates = source_candidates(root, key)
        matches = [(publisher, name) for publisher in ('同花顺', '东方财富')
                   for name in sorted(candidates[publisher])
                   if normalized_name(name) == normalized_name(judgment['sector'])]
        if not matches:
            raise ValueError(f'{key}: requires explicit analyst correction: {judgment["sector"]}')
        judgment['source_sector_provider'], judgment['source_sector_name'] = matches[0]
    publisher, source_name = validate_source_sector(root, judgment)
    revised = copy.deepcopy(previous)
    primary = next(c for c in revised['candidates'] if c['market_role'] == 'primary')
    primary.update(sector_key=judgment['key'], sector_name=judgment['sector'])
    for source, destination in [('chain', 'chain'), ('stage', 'stage'), ('reason', 'rationale')]:
        if source in judgment:
            primary[destination] = judgment[source]
    for ref in revised['evidence']:
        field = 'ths_fact' if ref['publisher'] == '同花顺' else 'company_fact' if ref['publisher'] == '东方财富' else None
        if field and judgment.get(field):
            ref['assertions'] = [judgment[field]]
    if publisher == '东方财富':
        source = json.loads((root / 'full_market' / (key + '.json')).read_text(encoding='utf-8'))['source']
        url, stamp = source['source_url'], source['fetched_at']
        content = json.dumps(source, ensure_ascii=False, sort_keys=True).encode()
    else:
        for suffix in ('.browser.json', '.json', '.search.json'):
            path = root / 'ths_sequential' / (key + suffix)
            if path.exists():
                source = json.loads(path.read_text(encoding='utf-8'))
                if source.get('status') == 'read':
                    break
        url = source['source_url']
        stamp = source['retrieved_at'].replace(' UTC', '+00:00').replace(' ', 'T')
        content = source['body'].encode()
    proof_id = key + '-source-board-' + today
    revised['evidence'] = [e for e in revised['evidence'] if e['evidence_id'] != proof_id]
    revised['evidence'].append(dict(
        evidence_id=proof_id, source_kind='provider_membership', publisher=publisher,
        title=previous['name'] + '已有板块成员核对', source_url=url, published_at=None,
        retrieved_at=stamp, assertions=['已核对该股票的来源板块原名：' + source_name + '；主归属从其已有行业或概念板块中选择。'],
        content_sha256=hashlib.sha256(content).hexdigest()))
    primary['market_evidence_ids'] = list(dict.fromkeys(primary['market_evidence_ids'] + [proof_id]))
    revised['effective_from'] = today
    # Correction does not renew evidence expiry or rewrite a prior day's record.
    MarketSectorDossierV2.model_validate(revised)
    if previous['effective_from'] == today:
        payload['entries'][index] = revised
    else:
        payload['entries'].append(revised)
    audit.append(dict(id=key, name=previous['name'], previous_sector=old_primary['sector_name'],
                      sector=judgment['sector'], key=judgment['key'], source_sector_name=source_name,
                      source_sector_provider=publisher))

backup = root / ('sector-name-correction-before-' + today + '-' + hashlib.sha256(original).hexdigest()[:12] + '.json')
if not backup.exists():
    backup.write_bytes(original)
assert backup.read_bytes() == original
for entry in payload['entries']:
    MarketSectorDossierV2.model_validate(entry)
write_json(target, payload)
write_json(root / 'review-source-membership-audit.json', dict(
    before_snapshot=str(backup), reviewed_on=today, primary_count=len(audit),
    renamed_count=sum(x['previous_sector'] != x['sector'] for x in audit), items=audit))
print(json.dumps({'primaries_checked': len(audit), 'renamed': sum(x['previous_sector'] != x['sector'] for x in audit),
                  'before_snapshot': str(backup)}, ensure_ascii=False))
