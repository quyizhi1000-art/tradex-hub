"""User's explicit 17:39 authorization: one early post-close run, real clock."""
import json
import sqlite3
import threading
from datetime import date
from pathlib import Path

from tradex.analysis_jobs import AnalysisJobStore, DAILY_STOCK_SELECTION
from tradex.analysis_worker import AnalysisRuntime, exclusive_worker_lock
from tradex.data_gateway.stock_technicals import fetch_stock_technical_window
from tradex.data_gateway.stock_technicals_contracts import StockTechnicalWindowV1
from tradex.data_sources import get_router, register_all_sources
from tradex.market_calendar import a_share_session, CalendarDayStatus, TradingSessionPhase

OUT = Path(__file__).parent
DAY = date(2026, 9, 17)
STRATEGY = 'macd-j-upturn-main-board'


def evidence(store):
    with sqlite3.connect(store.db_path) as db:
        return {table: dict(db.execute(f'SELECT {key}, payload_digest FROM {table}'))
                for table, key in [('daily_stock_selections', 'selection_id'),
                                   ('stock_selection_strategy_results', 'result_id'),
                                   ('daily_stock_selection_outcomes', 'selection_id'),
                                   ('stock_selection_strategy_outcomes', 'outcome_id')]}


known = StockTechnicalWindowV1.model_validate_json((OUT/'technical_20260916.json').read_text(encoding='utf-8'))
register_all_sources()
router = get_router()


class RecordedDaysRouter:
    def route_validated(self, capability, mapper, **kwargs):
        day = date.fromisoformat(kwargs['trade_date'])
        for record in known.days:
            if record.trade_date == day and not kwargs['check_st']:
                return record, record.metadata.provider
        return router.route_validated(capability, mapper, **kwargs)


with exclusive_worker_lock(), AnalysisJobStore() as jobs:
    runtime = AnalysisRuntime(jobs)
    stop = threading.Event()
    def heartbeat():
        while not stop.is_set():
            jobs.set_runtime_state('running', detail='User-authorized 2026-09-17 early post-close selection')
            stop.wait(15)
    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        before = evidence(runtime.selection_store)
        (OUT/'archive-before-0917.json').write_text(json.dumps(before), encoding='utf-8')
        with sqlite3.connect(runtime.selection_store.db_path) as source:
            with sqlite3.connect(OUT/'selection-before-0917.sqlite3') as destination:
                source.backup(destination)

        def authorized_due(now, *, automatic):
            session = a_share_session(now)
            assert now.date() == DAY and not automatic
            assert session.calendar_status is CalendarDayStatus.VERIFIED_TRADING_DAY
            assert session.phase is TradingSessionPhase.CLOSED
        # In-memory exception for this invocation; the production 18:00/18:30
        # policy and the actual generated_at clock remain unchanged.
        runtime.selection_service._validate_due = authorized_due
        load_base = runtime.selection_service._factor_loader
        def capture(day):
            snapshot = load_base(day)
            now = runtime.selection_service._now()
            if snapshot.metadata.provider_as_of and snapshot.metadata.provider_as_of > now:
                from tradex.data_gateway.contracts import QualityStatus
                snapshot = snapshot.model_copy(update={'metadata': snapshot.metadata.model_copy(update={
                    'provider_as_of': None, 'quality': QualityStatus.DEGRADED,
                    'quality_flags': (*snapshot.metadata.quality_flags, 'scheduled_source_as_of_not_yet_reached'),
                })})
            (OUT/'snapshot-2026-09-17.json').write_text(snapshot.model_dump_json(), encoding='utf-8')
            print(json.dumps({'phase': 'base_snapshot', 'trade_date': str(day), 'count': len(snapshot.factors)}), flush=True)
            return snapshot
        runtime.selection_service._factor_loader = capture
        def technicals(dates):
            window = fetch_stock_technical_window(dates, router=RecordedDaysRouter())
            (OUT/'technical_20260917.json').write_text(window.model_dump_json(), encoding='utf-8')
            print(json.dumps({'phase': 'technical_snapshot', 'signal_date': str(window.days[-1].trade_date),
                              'count': len(window.days[-1].points), 'st_count': len(window.days[-1].special_treatment_ids)}), flush=True)
            return window
        runtime.selection_service._technical_loader = technicals
        with sqlite3.connect(jobs.db_path) as db:
            assert db.execute("SELECT count(*) FROM analysis_jobs WHERE state IN ('queued','running')").fetchone()[0] == 0
        job = jobs.enqueue(DAILY_STOCK_SELECTION, trade_date=DAY, trigger='user-authorized-early-post-close-1739')
        print(json.dumps({'job_id': job['job_id']}), flush=True)
        result = runtime.execute_next_job()
        assert result and result['job_id'] == job['job_id'], result
        runtime.materialize_selection_views(force=True)
        stored = runtime.selection_store.get_strategy_result(DAY, STRATEGY, strategy_version='v1')
        assert stored is not None, result
        after = evidence(runtime.selection_store)
        assert all(after[t].get(k) == v for t, rows in before.items() for k, v in rows.items())
        receipt = {'job': result, 'result': stored.model_dump(mode='json'), 'existing_archives_unchanged': True,
                   'authorization': 'User explicitly authorized early generation at 2026-09-17 17:39; actual clock retained'}
        (OUT/'acceptance-0917.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'generated_at': stored.generated_at.isoformat(), 'matched': stored.payload.matched_count,
            'same_day': stored.payload.same_day_count, 'prior_3_sessions': stored.payload.prior_3_sessions_count,
            'existing_archives_unchanged': True}), flush=True)
    finally:
        stop.set(); thread.join()
        runtime.close()
