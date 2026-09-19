from pathlib import Path
p=Path('tradex/tests/test_intraday_macd_j.py');s=p.read_text(encoding='utf-8');s=s.replace('    evaluate, merge_observations, prior_sessions, project_daily, read_watch, refresh_watch,','    evaluate as real_evaluate, merge_observations, prior_sessions, project_daily, read_watch, refresh_watch as real_refresh_watch,')
s=s.replace('amount_cny=1e7)','amount_cny=1e7, volume_ratio=1.0)')
start=s.index('    if age <= 2:');end=s.index('\n\n\n@pytest.mark.parametrize("failure"',start)
s=s[:start]+'''    assert candidates == []
    assert coverage['excluded_counts'] == {'no_v4_signal': 1}
'''+s[end:]
insert='''
def price_histories(seeds, day):
    from tradex.data_gateway.contracts import ContractMetadata, OHLCVBarV1, OHLCVSeriesV1
    from tradex.data_gateway.macd_price_history import sixty_sessions
    dates = sixty_sessions(day)[:-1]
    seedmap = {seed.trade_date: seed.points[0] for seed in seeds if seed.points}
    bars = []
    for index, d in enumerate(dates):
        p = seedmap.get(d)
        close = p.close if p else 10
        bars.append(OHLCVBarV1(trading_date=d, open=close, close=close,
            high=80 if index == 0 else p.high if p else close+1,
            low=p.low if p else close-1, volume_shares=100000, amount_cny=1000000))
    return (OHLCVSeriesV1(metadata=ContractMetadata(contract="ohlcv_bar.v1", provider="fixture",
        fetched_at=NOW, quality="accepted"), instrument_id="600000.SH", period="daily",
        adjustment="forward", bars=tuple(bars)),)


def evaluate(seeds, universe, **kwargs):
    kwargs.setdefault("price_histories", price_histories(seeds, kwargs["now"].date()))
    return real_evaluate(seeds, universe, **kwargs)


def refresh_watch(**kwargs):
    kwargs.setdefault("price_loader", lambda instruments, day, **kw: price_histories(fixture()[0], day))
    return real_refresh_watch(**kwargs)

'''
pos=s.index('\n\n@pytest.fixture');s=s[:pos]+ '\n'+insert+s[pos:]
s=s.replace('    monkeypatch.setenv("TRADEX_DAILY_STOCK_SELECTION_DB", str(tmp_path / "selection.sqlite3"))','    monkeypatch.setenv("TRADEX_DAILY_STOCK_SELECTION_DB", str(tmp_path / "selection.sqlite3"))\n    monkeypatch.setattr("tradex.stock_selection.intraday_macd_j.fetch_macd_price_histories", lambda instruments, day, **kw: price_histories(fixture()[0], day))')
p.write_text(s,encoding='utf-8')
