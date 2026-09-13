import json,pathlib
p=pathlib.Path('work/state_owned_screen')
d=json.loads((p/'tdx_index_20260910.json').read_text(encoding='utf8'));print([(x['ts_code'],x['name'],x['idx_count']) for x in d['rows'] if any(t in x['name'] for t in ['央','地方','国有','国改','混改'])])
