from pathlib import Path
p=Path('tradex/tests/macd_j_selection.test.cjs');s=p.read_text(encoding='utf-8').replace('attributes: {}, setAttribute','attributes: {}, querySelector() {return null;}, setAttribute').replace("'stockSelectionDisplayCandidates', 'stockSelectionVisibleCandidates', 'renderMacdJScreen'", "'stockSelectionDisplayCandidates', 'stockSelectionVisibleCandidates', 'appendMacdPriceEvidence', 'renderMacdPending', 'renderMacdJScreen'");s+='''
test('v4 shows separate pending warnings and price evidence with inclusive MA5 rule', () => {
  const c=harness();
  const row={...payload.candidates[0],high_60:84.7,high_60_date:'2026-08-20',drawdown_60_pct:-31.157,ma5:58.31,volume_ratio:1.5};
  c.renderMacdJScreen({...payload,screen_version:'macd-j-upturn-main-board.v4',candidates:[row],pending_count:1,pending_candidates:[{...row,name:'预警样本'}]});
  assert.match(c.byId('stock-macd-j-rule').textContent,/价格≥MA5/);
  assert.match(JSON.stringify(c.byId('stock-macd-j-table-body')),/84.7/);
  assert.doesNotMatch(JSON.stringify(c.byId('stock-macd-j-table-body')),/预警样本/);
  assert.match(JSON.stringify(c.byId('stock-macd-j-pending')),/预警样本/);
  assert.match(JSON.stringify(c.byId('stock-macd-j-pending')),/连续两天/);
});
''';p.write_text(s,encoding='utf-8')
p=Path('tradex/tests/intraday_macd_j.test.cjs');s=p.read_text(encoding='utf-8').replace("'stockSelectionDisplayCandidates', 'renderIntradayMacdJIndustryFilters'", "'stockSelectionDisplayCandidates', 'appendMacdPriceEvidence', 'renderMacdPending', 'renderIntradayMacdJIndustryFilters'");p.write_text(s,encoding='utf-8')
p=Path('tradex/src/tradex/stock_selection/insights.py');s=p.read_text(encoding='utf-8').replace('    if strategy_id in EVIDENCE_FIELDS:', '''    if strategy_id == "macd-j-upturn-main-board" and candidate.get("signal_rule") == "strict_cross_v4":
        result.update({key: candidate.get(key) for key in ("macd_cross_date", "high_60", "high_60_date",
            "drawdown_60_pct", "ma5", "volume_ratio", "price_window_start", "price_window_end")})
    if strategy_id in EVIDENCE_FIELDS:''');p.write_text(s,encoding='utf-8')
