from datetime import date, datetime, time
from zoneinfo import ZoneInfo
import sqlite3

import pytest

from tradex.market_watch.recovery_source_cache import RecoverySourceMatrixStore


DAY = date(2026, 9, 18)
CLOSE = datetime(2026, 9, 18, 15, tzinfo=ZoneInfo("Asia/Shanghai"))


def test_partial_checkpoint_is_day_isolated_and_never_a_complete_matrix(tmp_path):
    cache = RecoverySourceMatrixStore(tmp_path / "source.sqlite3")
    assert cache.read_checkpoint(DAY) is None
    cache.record_checkpoint(trade_date=DAY, provider_as_of=CLOSE,
        previous_close={"000001.SZ": 10.0, "600000.SH": 12.0},
        stock_prices={time(10,12): {"000001.SZ": 10.2}})
    reopened = RecoverySourceMatrixStore(tmp_path / "source.sqlite3")
    assert reopened.read_checkpoint(DAY)["stock_prices"] == {"10:12": {"000001.SZ": 10.2}}
    assert reopened.read(DAY, {time(10,12)}) is None
    assert reopened.read_checkpoint(date(2026,9,17)) is None


@pytest.mark.parametrize("prices", [{time(10,12): {"wrong": 10}}, {time(10,12): {"000001.SZ": float("nan")}}, {time(10,12): {"000001.SZ": -1}}])
def test_checkpoint_rejects_invalid_or_unexpected_prices(tmp_path, prices):
    cache = RecoverySourceMatrixStore(tmp_path / "source.sqlite3")
    with pytest.raises(ValueError):
        cache.record_checkpoint(trade_date=DAY, provider_as_of=CLOSE, previous_close={"000001.SZ":10}, stock_prices=prices)


def test_checkpoint_rejects_wrong_day_proof_and_corrupt_storage(tmp_path):
    cache = RecoverySourceMatrixStore(tmp_path / "source.sqlite3")
    with pytest.raises(ValueError):
        cache.record_checkpoint(trade_date=DAY, provider_as_of=CLOSE.replace(day=17), previous_close={"000001.SZ":10}, stock_prices={})
    cache.record_checkpoint(trade_date=DAY, provider_as_of=CLOSE, previous_close={"000001.SZ":10}, stock_prices={})
    with sqlite3.connect(cache.db_path) as db:
        db.execute("UPDATE recovery_source_checkpoints SET payload_digest='bad'")
    with pytest.raises(RuntimeError, match="digest"):
        cache.read_checkpoint(DAY)
