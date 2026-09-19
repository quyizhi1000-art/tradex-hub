from pathlib import Path
p=Path('tradex/src/tradex/dashboard/watch/app.js');s=p.read_text(encoding='utf-8');s=s.replace('      candidates: asArray(payload.candidates).map((candidate) => ({\n        ...candidate, industry_block_name: mapping[candidate.instrument_id] || null,\n      })),','''      candidates: asArray(payload.candidates).map((candidate) => ({
        ...candidate, industry_block_name: mapping[candidate.instrument_id] || null,
      })),
      pending_candidates: asArray(payload.pending_candidates).map((candidate) => ({
        ...candidate, industry_block_name: mapping[candidate.instrument_id] || null,
      })),''');p.write_text(s,encoding='utf-8')
