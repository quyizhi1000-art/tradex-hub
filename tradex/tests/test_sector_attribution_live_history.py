"""Opt-in replay against immutable historical THS pool dates.

The default unit suite remains offline.  Set ``TRADEX_LIVE_HISTORY=1`` to
verify that the provider still returns the audited full pools and that all
eleven observed sectors retain their calibrated attribution results.
"""

from __future__ import annotations

import hashlib
import json
import os

import pytest

from tradex.dashboard.sector_attribution import SECTOR_KEYS, attribute_limit_up_records
from tradex.data_sources.ths_fetchers import fetch_ths_limit_up_pool


pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(
        os.environ.get("TRADEX_LIVE_HISTORY") != "1",
        reason="set TRADEX_LIVE_HISTORY=1 for the audited provider replay",
    ),
]


def _levels(**nonzero):
    expected = {key: (0, 0, "medium") for key in SECTOR_KEYS}
    expected.update(nonzero)
    return expected


AUDITED_HISTORY = {
    "20260205": {
        "pool_total": 44,
        "sha256": "99c89e6b1b4d8ef21a03601c0bf0cd346fa8ea25e47e6acec8aa79657d59c0b8",
        "sectors": _levels(
            bank=(1, 1, "medium"),
            internet_finance=(1, 1, "medium"),
            retail=(1, 1, "medium"),
            baijiu=(1, 1, "medium"),
        ),
    },
    "20260527": {
        "pool_total": 47,
        "sha256": "5a1ab6e396158849cb99a2b316e62946faf48d21a6da29626ea35e60c8c6ab2c",
        "sectors": _levels(
            electric_power=(3, 2, "strong"),
            retail=(2, 2, "strong"),
            baijiu=(1, 1, "medium"),
        ),
    },
    "20260622": {
        "pool_total": 132,
        "sha256": "f48e7249829c2bcf0229ae7d0c15c2dc861b4c46e0a90a7dc1205220bd63e13d",
        "sectors": _levels(
            securities=(4, 1, "strong"),
            internet_finance=(4, 1, "strong"),
            oil_gas=(1, 1, "medium"),
            agriculture=(1, 5, "medium"),
            nonferrous=(4, 1, "strong"),
            rare_earth=(2, 1, "medium"),
            precious_metals=(2, 1, "medium"),
        ),
    },
}


def _pool_digest(records):
    rows = [
        {
            "code": str(record.get("代码") or "").strip(),
            "name": str(record.get("名称") or "").strip(),
            "reason": str(record.get("涨停原因") or "").strip(),
            "board": str(record.get("连板") or "").strip(),
        }
        for record in records
    ]
    rows.sort(key=lambda row: (row["code"], row["name"], row["reason"], row["board"]))
    encoded = json.dumps(
        rows,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@pytest.mark.parametrize("trade_date", tuple(AUDITED_HISTORY))
def test_full_historical_pool_replay_covers_all_eleven_sectors(trade_date):
    expected = AUDITED_HISTORY[trade_date]
    frame = fetch_ths_limit_up_pool(trade_date)
    records = frame.to_dict(orient="records")
    result = attribute_limit_up_records(records)

    assert frame.attrs["data_date"] == trade_date
    assert frame.attrs["trade_status"]["name"] == "已收盘"
    assert frame.attrs["source_valid"] is True
    assert frame.attrs["pool_total"] == expected["pool_total"]
    assert frame.attrs["unique_total"] == expected["pool_total"]
    assert frame.attrs["reason_coverage"] == 1.0
    assert frame.attrs["board_count_coverage"] == 1.0
    assert _pool_digest(records) == expected["sha256"]
    assert {
        key: (
            result["sectors"][key]["matched_count"],
            result["sectors"][key]["max_board_count"],
            result["sectors"][key]["vote"],
        )
        for key in SECTOR_KEYS
    } == expected["sectors"]

    assert set().union(*(
        {
            key
            for key, values in item["sectors"].items()
            if values[0] > 0
        }
        for item in AUDITED_HISTORY.values()
    )) == set(SECTOR_KEYS)
