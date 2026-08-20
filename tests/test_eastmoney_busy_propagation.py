"""Regression tests for request-local Eastmoney congestion fallback."""

from __future__ import annotations

import pytest

from astock_signals import concept, dragon_tiger, industry, limit_up_board
from astock_signals.smart_router import SourceBusyError


def _busy(*args, **kwargs):
    raise SourceBusyError("busy-test")


def test_dragon_tiger_does_not_convert_busy_to_an_empty_result(monkeypatch):
    monkeypatch.setattr(dragon_tiger, "em_datacenter", _busy)

    with pytest.raises(SourceBusyError, match="busy-test"):
        dragon_tiger.get_dragon_tiger_board_json("000001", "2026-08-19", 1)


def test_industry_does_not_convert_busy_to_an_error_payload(monkeypatch):
    monkeypatch.setattr(industry, "em_push2", _busy)

    with pytest.raises(SourceBusyError, match="busy-test"):
        industry.get_industry_comparison_json("000001", "2026-08-19", 5)


@pytest.mark.parametrize(
    "fetch",
    [limit_up_board.get_limit_up_pool, limit_up_board.get_board_sentiment_json],
)
def test_limit_up_board_never_reports_busy_as_empty_success(monkeypatch, fetch):
    monkeypatch.setattr(limit_up_board, "em_get", _busy)

    with pytest.raises(SourceBusyError, match="busy-test"):
        fetch()


def test_concept_attribution_propagates_gateway_busy(monkeypatch):
    monkeypatch.setattr(concept, "em_get", _busy)
    monkeypatch.setattr(concept, "_region_boards_cache", None)

    with pytest.raises(SourceBusyError, match="busy-test"):
        concept.get_concept_blocks_json("000001")


def test_partial_fund_flow_does_not_hide_a_busy_history_request(monkeypatch):
    from tradex.data_sources import astock_signals_fetchers, em_client

    class RealtimeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": {"klines": ["10:00,1,2,3,4,5"]}}

    responses = iter([RealtimeResponse(), SourceBusyError("busy-test")])

    def fake_get(*args, **kwargs):
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(em_client, "em_get", fake_get)

    with pytest.raises(SourceBusyError, match="busy-test"):
        astock_signals_fetchers.fetch_fund_flow_em(code="000001")
