"""Collector-owned daily-indicator projection and persistent page-only alerts."""

from collections import Counter
from datetime import date, datetime, time, timedelta
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from tradex.data_gateway.intraday_technical_seed import (
    IntradayTechnicalSeedDayV1, fetch_intraday_seed_day,
)
from tradex.data_gateway.intraday_scan_quotes import fetch_intraday_scan_quotes, quote_time_is_current
from tradex.market_calendar import CalendarDayStatus, a_share_session, calendar_day_status
from .engine import _special_treatment_name
from .intraday_increment import with_previous_close_difference
from .macd_rules import v4_signal, price_filter_evidence
from tradex.data_gateway.macd_price_history import sixty_sessions, fetch_macd_price_histories

SHANGHAI = ZoneInfo("Asia/Shanghai")
CONTRACT = "intraday_macd_j_watch.v1"
SCREEN_VERSION = "macd-j-upturn-main-board.v5"
SCAN_MINUTES = tuple(range(570, 691, 15)) + (705,) + tuple(range(780, 901, 15))


def scan_slot(now: datetime):
    local = now.astimezone(SHANGHAI)
    if not a_share_session(local).is_trading_day:
        return None
    for minute in SCAN_MINUTES:
        slot = local.replace(hour=minute//60, minute=minute%60, second=0, microsecond=0)
        if timedelta(0) <= local-slot < timedelta(minutes=2):
            return slot
    return None


def next_scan_at(now: datetime):
    local = now.astimezone(SHANGHAI)
    return next((local.replace(hour=m//60, minute=m%60, second=0, microsecond=0).isoformat()
                 for m in SCAN_MINUTES if m > local.hour*60+local.minute), None)


def is_mainboard(instrument):
    return ((instrument.endswith(".SH") and instrument[:3] in {"600", "601", "603", "605"})
            or (instrument.endswith(".SZ") and instrument[:3] in {"000", "001", "002", "003"}))


def prior_sessions(day: date) -> tuple[date, ...]:
    result = []
    while len(result) < 9:
        day -= timedelta(days=1)
        status = calendar_day_status(day)
        if status is CalendarDayStatus.UNVERIFIED:
            raise ValueError("seed calendar unavailable")
        if status is CalendarDayStatus.VERIFIED_TRADING_DAY:
            result.append(day)
    return tuple(reversed(result))


def project_daily(points, quote):
    """Eliminate the two EMA states algebraically; no short-window EMA seeding.

    DIF[t] = (a+b)DIF[t-1] - ab DIF[t-2] + (b-a)(C[t]-C[t-1]),
    a=11/13, b=25/27. Only valid on an unchanged forward-adjustment basis.
    Published rounding is guarded at the decision boundary below.
    """
    previous = points[-1]
    a, b = 11 / 13, 25 / 27
    dif = (a + b) * previous.dif - a * b * points[-2].dif + (b - a) * (quote.last - previous.close)
    dea = previous.dea * 0.8 + dif * 0.2
    high = max(quote.high, *(p.high for p in points))
    low = min(quote.low, *(p.low for p in points))
    if high <= low:
        raise ValueError("flat nine-session range")
    rsv = (quote.last - low) / (high - low) * 100
    k = previous.k * 2 / 3 + rsv / 3
    d = previous.d * 2 / 3 + k / 3
    return dif, dea, k, d, 3 * k - 2 * d


def evaluate(seeds, universe, *, now: datetime, closing_snapshot=None, price_histories=(), preliminary=False):
    day = now.astimezone(SHANGHAI).date()
    histories = {h.instrument_id: h for h in price_histories}
    dates60 = sixty_sessions(day)
    closing_bars = None
    if closing_snapshot is not None:
        from tradex.data_gateway.stock_selection_contracts import DailyStockFactorSnapshotV1
        if (not isinstance(closing_snapshot, DailyStockFactorSnapshotV1)
                or closing_snapshot.trade_date != day or now.astimezone(SHANGHAI).hour < 15
                or calendar_day_status(day) is not CalendarDayStatus.VERIFIED_TRADING_DAY):
            raise ValueError("manual close scan requires today's validated post-close snapshot")
        closing_bars = {h.instrument_id: h.bars[-1] for h in closing_snapshot.candlestick_histories
                        if h.bars[-1].trade_date == day}
    if tuple(seed.trade_date for seed in seeds) != prior_sessions(day):
        raise ValueError("seed sessions are not the exact previous nine sessions")
    if seeds[-1].st_trade_date != day:
        raise ValueError("current ST list is not verified")
    maps = [{p.instrument_id: p for p in seed.points} for seed in seeds]
    st_ids = set(seeds[-1].special_treatment_ids)
    excluded, candidates = Counter(), []
    eligible = evaluated = 0
    evaluated_ids = set()
    for q in universe.quotes:
        mainboard = is_mainboard(q.instrument_id)
        if not mainboard:
            excluded["not_main_board"] += 1
            continue
        if q.instrument_id in st_ids or _special_treatment_name(q.name):
            excluded["special_treatment"] += 1
            continue
        eligible += 1
        stamp = q.observed_at
        if closing_bars is not None:
            bar = closing_bars.get(q.instrument_id)
            if bar is None or (q.last, q.open, q.high, q.low, q.previous_close, q.amount_cny) != (
                    bar.close, bar.open, bar.high, bar.low, bar.previous_close, bar.amount_cny):
                raise ValueError("manual close quote does not match its validated daily bar")
        elif not quote_time_is_current(stamp, now):
            excluded["stale_quote"] += 1
            continue
        if (q.amount_cny <= 0 or not q.open or not q.high or not q.low or not q.previous_close
                or not 0 < q.low <= q.last <= q.high):
            excluded["invalid_quote"] += 1
            continue
        points = [m.get(q.instrument_id) for m in maps]
        if any(p is None or p.amount_cny <= 0 for p in points):
            excluded["missing_history"] += 1
            continue
        previous = points[-1]
        if (any(abs(p.adjustment_factor / previous.adjustment_factor - 1) > 1e-8
                or abs(p.adjusted_close - p.close) > 0.011 for p in points)
                or abs(q.previous_close - previous.close) > 0.011):
            excluded["adjustment_changed"] += 1
            continue
        # Verify that this source's published states obey the standard recurrence.
        a, b = 11 / 13, 25 / 27
        consistent = all(
            abs(points[i].dif - ((a + b) * points[i-1].dif - a*b*points[i-2].dif
                + (b-a)*(points[i].close-points[i-1].close))) <= 0.004
            and abs(points[i].dea - (0.8*points[i-1].dea + 0.2*points[i].dif)) <= 0.002
            for i in range(2, len(points))
        )
        if not consistent:
            excluded["indicator_basis_mismatch"] += 1
            continue
        try:
            # Reproduce the last published KDJ using its own nine-day range
            # before trusting the same recurrence for an intraday projection.
            from types import SimpleNamespace
            prior_projection = project_daily(points[-9:-1], SimpleNamespace(
                last=previous.close, high=previous.high, low=previous.low))
            if any(abs(actual - predicted) > 0.02 for actual, predicted in
                   zip((previous.k, previous.d, previous.j), prior_projection[2:])):
                excluded["indicator_basis_mismatch"] += 1
                continue
            dif, dea, k, d, j = project_daily(points[-8:], q)
        except ValueError:
            excluded["flat_range"] += 1
            continue
        evaluated += 1
        evaluated_ids.add(q.instrument_id)
        projected = SimpleNamespace(dif=dif, dea=dea, j=j)
        kind, turn = v4_signal(points[-5:] + [projected], max_j_lead=2)
        if kind is None:
            excluded["no_v4_signal"] += 1
            continue
        js = [p.j for p in points[-5:]] + [j]
        filters = {}
        if not preliminary:
            history = histories.get(q.instrument_id)
            ratio = getattr(q, "volume_ratio", None)
            if ratio is None and history is not None:
                ratio = intraday_volume_ratio(q, history, now)
            filters, reason = price_filter_evidence(history, dates=dates60, price=q.last,
                volume_ratio=ratio, current_high=q.high, previous_close=q.previous_close)
            if reason:
                excluded[reason] += 1
                if reason in {"price_history_unavailable", "incomplete_60_session_history",
                              "volume_ratio_unavailable", "price_history_basis_mismatch"}:
                    evaluated -= 1
                    evaluated_ids.discard(q.instrument_id)
                continue
        dates = [seed.trade_date for seed in seeds[-5:]] + [day]
        trough = js[turn-1]
        candidates.append({
            "signal_kind": kind,
            "macd_gap_evidence": [p.dea-p.dif for p in points[-2:]] + [dea-dif],
            "evidence": [dict(trade_date=d.isoformat(), **{key: getattr(p, key)
                for key in ("dif", "dea", "k", "d", "j")})
                for d, p in zip(dates[:-1], points[-5:])]
                + [dict(trade_date=day.isoformat(), dif=dif, dea=dea, k=k, d=d, j=j)],
            **{key: value.isoformat() if isinstance(value, date) else value for key, value in filters.items()},
            "instrument_id": q.instrument_id, "name": q.name, "price": q.last,
            "observed_at": stamp.isoformat() if stamp else None,
            "price_basis": "daily_close" if closing_bars is not None else "intraday_quote",
            "price_trade_date": day.isoformat(), "dif": dif, "dea": dea, "k": k, "d": d, "j": j,
            "macd_cross_date": day.isoformat() if kind == "confirmed" else None,
            "macd_cross_age_sessions": 0 if kind == "confirmed" else None,
            "signal_group": "同日拐头" if turn == 5 else "此前1～2个交易日拐头",
            "zero_axis_zone": "零轴上方" if min(dif, dea) > 0 else "零轴下方" if max(dif, dea) < 0 else "零轴附近",
            "j_turn_date": dates[turn].isoformat(), "j_trough": trough,
            "low_j_tags": [label for threshold, label in [(20, "J<20"), (0, "J<0")] if trough < threshold],
        })
    return candidates, {"universe_count": len(universe.quotes), "eligible_count": eligible,
                         "evaluated_count": evaluated, "excluded_counts": dict(excluded)}, evaluated_ids


def intraday_volume_ratio(quote, history, now):
    """Current cumulative shares / elapsed trading minutes / prior-five mean per minute."""
    volumes = [b.volume_shares for b in history.bars[-5:]]
    shares = getattr(quote, "volume_shares", None)
    stamp = quote.observed_at or now
    local = stamp.astimezone(SHANGHAI)
    minute = local.hour*60 + local.minute + local.second/60
    elapsed = min(120, max(0, minute-570)) + min(120, max(0, minute-780))
    if not shares or not elapsed or len(volumes) != 5 or any(v is None or v <= 0 for v in volumes):
        return None
    return shares / (sum(volumes)/5) * 240/elapsed


def merge_observations(previous, candidates, *, now: datetime, evaluated_ids=None):
    """One scheduled fresh scan; at most one alert per instrument per session."""
    current = {c["instrument_id"]: c for c in candidates}
    records = {r["instrument_id"]: dict(r) for r in previous}
    for instrument, record in records.items():
        if instrument not in current:
            record["active"] = False
            record["unverified"] = evaluated_ids is not None and instrument not in evaluated_ids
            record["consecutive_samples"] = 0
    for instrument, candidate in current.items():
        old = records.get(instrument, {})
        stamp = datetime.fromisoformat(candidate["observed_at"])
        prior_stamp = datetime.fromisoformat(old["observed_at"]) if old else None
        continuous = bool(old.get("active") and prior_stamp and timedelta(0) < stamp-prior_stamp <= timedelta(seconds=150))
        count = old.get("consecutive_samples", 0) + 1 if continuous else 1
        if prior_stamp == stamp:
            count = old.get("consecutive_samples", 0)
        alerted = old.get("alerted_at") or now.isoformat()
        records[instrument] = {**candidate, "active": True, "unverified": False, "consecutive_samples": count,
            "first_seen_at": old.get("first_seen_at", now.isoformat()), "alerted_at": alerted}
    # Retain notified withdrawals for today's audit, drop transient one-sample noise.
    return sorted((r for r in records.values() if r["active"] or r.get("alerted_at")),
                  key=lambda r: (not r["active"], r["instrument_id"]))


def _root(root=None):
    return Path(root) if root is not None else Path.home() / ".tradex" / "intraday_macd_j"


def _read(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    os.replace(temporary, path)


def archive_scan(result, *, root=None):
    slot = result.get("last_scan_slot")
    if slot is None:
        return
    stamp = datetime.fromisoformat(slot).astimezone(SHANGHAI)
    path = _root(root) / f"scan-{stamp:%Y-%m-%d-%H%M}.json"
    if result.get("scan_kind") == "manual_intraday":
        path = path.with_name(f"scan-{stamp:%Y-%m-%d-%H%M}-manual-{stamp:%S%f}.json")
    if not path.exists():
        _write(path, result)


def read_close_confirmation(day, *, now):
    """Project the authoritative selection archive into the watch display contract.

    No new scan, provider request, cache, or archive write occurs here. Original
    intraday observations remain immutable and separate from the final result.
    """
    local = now.astimezone(SHANGHAI)
    if day > local.date() or (day == local.date() and local.hour < 15):
        return None
    from .store import read_archived_strategy_result
    result = (read_archived_strategy_result(day, "macd-j-upturn-main-board", strategy_version="v5")
              or read_archived_strategy_result(day, "macd-j-upturn-main-board", strategy_version="v4")
              or read_archived_strategy_result(day, "macd-j-upturn-main-board", strategy_version="v3")
              or read_archived_strategy_result(day, "macd-j-upturn-main-board", strategy_version="v1"))
    if result is None or result.quality == "unavailable":
        return None
    screen = result.payload
    if day == local.date() and screen.screen_version != SCREEN_VERSION:
        return None
    candidates = []
    for candidate in (*screen.candidates, *screen.pending_candidates):
        evidence = candidate.evidence[-1]
        candidates.append({
            "signal_kind": "pending_cross" if candidate.signal_rule in {"pending_cross_v4", "pending_cross_v5"} else "confirmed",
            "evidence": [p.model_dump(mode="json") for p in candidate.evidence],
            **{key: value.isoformat() if isinstance(value, date) else value for key, value in
               candidate.model_dump().items() if key in {"high_60", "high_60_date", "ma5",
               "drawdown_60_pct", "volume_ratio", "price_window_start", "price_window_end"}},
            "instrument_id": candidate.instrument_id, "name": candidate.name,
            "price": candidate.reference_close, "price_basis": "daily_close",
            "price_trade_date": day.isoformat(), "observed_at": None,
            **{key: getattr(evidence, key) for key in ("dif", "dea", "k", "d", "j")},
            "signal_group": "同日拐头" if candidate.signal_group == "same_day" else
                            "前一交易日拐头" if candidate.signal_group == "prior_1_session" else
                            "此前1～2个交易日拐头" if candidate.signal_group == "prior_2_sessions" else "此前 1～3 日拐头",
            "zero_axis_zone": {"above_zero": "零轴上方", "below_zero": "零轴下方",
                               "crossing_zero": "零轴附近"}[candidate.zero_axis_zone],
            "j_turn_date": candidate.j_turn_date.isoformat(), "j_trough": candidate.j_trough,
            "macd_cross_date": candidate.macd_cross_date.isoformat() if candidate.macd_cross_date else None,
            "macd_cross_age_sessions": candidate.macd_cross_age_sessions,
            "low_j_tags": list(candidate.low_j_tags),
        })
    return {
        "contract": CONTRACT, "trade_date": day.isoformat(),
        "scan_kind": "close_confirmation", "status": "close_confirmed",
        "generated_at": result.generated_at.isoformat(),
        "last_scan_slot": result.generated_at.isoformat(), "next_scan_at": None,
        "source_result_id": result.result_id, "source_snapshot_revision": result.source_snapshot_revision,
        "screen_version": screen.screen_version, "quality": screen.quality,
        "source_metadata": screen.source_metadata[-1].model_dump(mode="json") if screen.source_metadata else {},
        "limitations": list(screen.limitations),
        "universe_count": screen.universe_count, "eligible_count": screen.board_eligible_count,
        "requested_count": screen.board_eligible_count, "evaluated_count": screen.evaluated_count,
        "excluded_counts": screen.excluded_counts, "matched_count": screen.matched_count,
        "message": f"收盘确认：与选股中心同日正式存档一致，命中 {screen.matched_count} 只；盘中预估记录另行保留。",
        "records": [{**c, "active": True, "unverified": False} for c in candidates if c["signal_kind"] == "confirmed"],
        "scan_candidates": [c for c in candidates if c["signal_kind"] == "confirmed"],
        "pending_candidates": [c for c in candidates if c["signal_kind"] == "pending_cross"],
        "pending_count": screen.pending_count,
    }


def _with_coverage_status(result):
    """Label incomplete scans, including old archives, without rewriting history."""
    result = dict(result)
    requested, evaluated = result.get("requested_count"), result.get("evaluated_count")
    if (result.get("status") in {"monitoring", "partial"}
            and isinstance(requested, int) and isinstance(evaluated, int)
            and evaluated < requested):
        result.update(status="partial", message=(
            ("手动重扫；" if result.get("scan_kind") == "manual_intraday" else "")
            + ("午盘收市行情；" if result.get("quote_session") == "midday_close" else "")
            + f"扫描不完整：已核验 {evaluated} / {requested} 只；"
            f"已核验范围内命中 {len(result.get('scan_candidates', []))} 只，不代表全量结果。"
            "仅提示已核验信号，盘中预估，收盘前可能失效。"))
    return result


def read_scan_history(*, trade_date=None, now=None, root=None):
    now = now or datetime.now(SHANGHAI)
    root = _root(root)
    dates = sorted({p.name[5:15] for p in root.glob("scan-????-??-??-*.json")}, reverse=True)[:90]
    selected = date.fromisoformat(trade_date) if trade_date else now.astimezone(SHANGHAI).date()
    baselines = {}
    scans = [with_previous_close_difference(_with_coverage_status(value), baselines=baselines)
             for p in sorted(root.glob(f"scan-{selected}-*.json"), reverse=True)
             if (value := _read(p)) is not None]
    confirmed = read_close_confirmation(selected, now=now)
    if confirmed is not None:
        scans.insert(0, confirmed)
    from .industry_display import load_selection_industry_display
    industry_display = load_selection_industry_display(
        c["instrument_id"] for scan in scans for c in [*scan.get("scan_candidates", []), *scan.get("pending_candidates", [])]
    )
    return {"contract": "intraday_macd_j_history.v1", "trade_date": selected.isoformat(),
            "dates": sorted(set([selected.isoformat(), *dates]), reverse=True), "scans": scans,
            "industry_display": industry_display.model_dump(mode="json")}


def read_watch(*, now=None, root=None):
    now = now or datetime.now(SHANGHAI)
    day = now.astimezone(SHANGHAI).date()
    confirmed = read_close_confirmation(day, now=now)
    if confirmed is not None:
        return confirmed
    stored = _read(_root(root) / f"watch-{day}.json")
    if stored and stored.get("screen_version") != SCREEN_VERSION:
        stored = None
    result = stored or {"contract": CONTRACT, "trade_date": day.isoformat(), "records": [],
                        "status": "waiting", "message": "等待盘中扫描；仅显示当日实时信号。"}
    result = with_previous_close_difference(_with_coverage_status(result))
    if result.get("scan_kind") == "manual_close":
        result["message"] = ("收盘价推算，尚未取得选股中心同日正式确认；"
                             f"预估命中 {len(result.get('scan_candidates', []))} 只。")
        return result
    local = now.astimezone(SHANGHAI)
    if not a_share_session(now).is_trading_day or not time(9, 20) <= local.time().replace(tzinfo=None) <= time(15, 2):
        result.update(status="closed", message="非交易时段，盘中扫描暂停；尚无同日正式确认，盘中记录不代表收盘结果。")
    elif stored and now > datetime.fromisoformat(stored.get("next_scan_at") or stored["generated_at"]) + timedelta(minutes=2):
        result.update(status="stale", message="扫描数据已过期，暂停提示，等待后台恢复。")
    return result


def run_manual_close_scan(snapshot, seeds, *, now: datetime, root=None):
    """Explicit operator command; use canonical daily bars without faking quote times."""
    from types import SimpleNamespace
    from tradex.data_gateway.contracts import AShareUniverseQuoteV1
    factors = {r.instrument_id: r for r in snapshot.factors}
    quotes = []
    for history in snapshot.candlestick_histories:
        factor = factors.get(history.instrument_id)
        bar = history.bars[-1]
        if factor is None or factor.market != "主板" or bar.trade_date != snapshot.trade_date:
            continue
        quotes.append(AShareUniverseQuoteV1(
            instrument_id=history.instrument_id, name=factor.name,
            observed_at=None, last=bar.close, open=bar.open, high=bar.high, low=bar.low,
            previous_close=bar.previous_close, amount_cny=bar.amount_cny,
            change_pct=(bar.close / bar.previous_close - 1) * 100,
            volume_ratio=factor.volume_ratio, volume_shares=bar.volume_shares,
        ))
    possible, _, _ = evaluate(seeds, SimpleNamespace(quotes=quotes), now=now,
                             closing_snapshot=snapshot, preliminary=True)
    histories = fetch_macd_price_histories((c["instrument_id"] for c in possible), snapshot.trade_date, intraday=True)
    candidates, coverage, _ = evaluate(seeds, SimpleNamespace(quotes=quotes), now=now,
                                        closing_snapshot=snapshot, price_histories=histories)
    pending = [c for c in candidates if c["signal_kind"] == "pending_cross"]
    candidates = [c for c in candidates if c["signal_kind"] == "confirmed"]
    if not coverage["evaluated_count"]:
        raise ValueError("no reliable closing-price scan could be completed")
    result = {"contract": CONTRACT, "screen_version": SCREEN_VERSION, "trade_date": snapshot.trade_date.isoformat(),
        "scan_kind": "manual_close", "status": "close_scanned", "generated_at": now.isoformat(),
        "last_scan_slot": now.isoformat(), "next_scan_at": None, **coverage,
        "requested_count": len(quotes), "missing_quote_count": 0,
        "message": f"手动收盘扫描完成：使用 {snapshot.trade_date} 真实收盘日线，本次命中 {len(candidates)} 只。",
        "records": [{**c, "active": True, "unverified": False, "alerted_at": now.isoformat()} for c in candidates],
        "scan_candidates": candidates, "source_metadata": snapshot.metadata.model_dump(mode="json"),
        "pending_candidates": pending, "pending_count": len(pending),
        "baseline_dates": [s.trade_date.isoformat() for s in seeds],
        "seed_metadata": [s.metadata.model_dump(mode="json") for s in seeds],
    }
    archive_scan(result, root=root)
    _write(_root(root) / f"watch-{snapshot.trade_date}.json", result)
    return result


def refresh_watch(*, now=None, root=None, seed_loader=fetch_intraday_seed_day,
                  quote_loader=fetch_intraday_scan_quotes, price_loader=fetch_macd_price_histories, force=False):
    """Collector writer; operator force scans require the collector to be stopped."""
    now = now or datetime.now(SHANGHAI)
    session = a_share_session(now)
    preparing = session.is_trading_day and time(9, 20) <= now.astimezone(SHANGHAI).time().replace(tzinfo=None) < time(9, 30)
    slot = scan_slot(now)
    if force:
        if (not session.is_trading_day
                or not time(9, 30) <= now.astimezone(SHANGHAI).time().replace(tzinfo=None) <= time(15, 2)):
            raise ValueError("manual intraday scan requires today's trading session or lunch break")
        slot = now.astimezone(SHANGHAI)
    if slot is None and not preparing and not session.is_open:
        return read_watch(now=now, root=root)
    root = _root(root)
    day = now.astimezone(SHANGHAI).date()
    path = root / f"watch-{day}.json"
    previous = _read(path) or {}
    if not force and slot is not None and (root / f"scan-{slot:%Y-%m-%d-%H%M}.json").exists():
        return read_watch(now=now, root=root)
    if not force and slot is not None and previous.get("last_scan_slot") == slot.isoformat():
        return read_watch(now=now, root=root)
    # Collector is the only writer. Bound preparation to one source day per tick.
    if not force and previous.get("retry_after") and now < datetime.fromisoformat(previous["retry_after"]):
        return read_watch(now=now, root=root)
    result = {"contract": CONTRACT, "screen_version": SCREEN_VERSION, "trade_date": day.isoformat(), "generated_at": now.isoformat(),
              "status": "preparing", "message": "正在准备前 9 个交易日指标及今日 ST 名单。",
              "records": previous.get("records", []), "next_scan_at": next_scan_at(now)}
    if force:
        result["scan_kind"] = "manual_intraday"
    midday = time(11, 30) <= now.astimezone(SHANGHAI).time().replace(tzinfo=None) < time(13)
    result["quote_session"] = "midday_close" if midday else "intraday"
    try:
        dates, seeds = prior_sessions(day), []
        for historical_day in dates:
            st_date = day if historical_day == dates[-1] else None
            seed_path = root / f"seed-{historical_day}-st-{st_date}.json"
            raw = _read(seed_path)
            if raw is None:
                seed = seed_loader(historical_day, st_date=st_date, now=now)
                _write(seed_path, seed.model_dump(mode="json"))
                result["message"] = f"历史指标准备中：{len(seeds)+1}/9；完成后开始盘中扫描。"
                if slot is not None:
                    result["last_scan_slot"] = slot.isoformat()
                    archive_scan(result, root=root)
                _write(path, result)
                return result
            seeds.append(IntradayTechnicalSeedDayV1.model_validate(raw))
        if preparing:
            result.update(status="ready", message="历史指标已就绪，等待开盘实时行情。")
            _write(path, result)
            return result
        if slot is None:
            return read_watch(now=now, root=root)
        instruments = [p.instrument_id for p in seeds[-1].points if is_mainboard(p.instrument_id)
                       and p.instrument_id not in seeds[-1].special_treatment_ids]
        universe = quote_loader(instruments, now=now)
        # Freshness is measured after acquisition, not at request start.
        observed = datetime.now(SHANGHAI) if quote_loader is fetch_intraday_scan_quotes else now
        possible, _, _ = evaluate(seeds, universe, now=observed, preliminary=True)
        histories = price_loader((c["instrument_id"] for c in possible), day, intraday=True)
        # Acquisition may outlive the quote: refresh once, then apply the normal freshness gate.
        if price_loader is fetch_macd_price_histories:
            observed = datetime.now(SHANGHAI)
            possible_ids = {c["instrument_id"] for c in possible}
            if any(not quote_time_is_current(q.observed_at, observed)
                   for q in universe.quotes if q.instrument_id in possible_ids):
                universe = quote_loader(instruments, now=observed)
                observed = datetime.now(SHANGHAI)
        candidates, coverage, evaluated_ids = evaluate(seeds, universe, now=observed, price_histories=histories)
        pending = [c for c in candidates if c["signal_kind"] == "pending_cross"]
        candidates = [c for c in candidates if c["signal_kind"] == "confirmed"]
        quote_providers = getattr(universe, "quote_providers", {})
        for candidate in candidates:
            candidate["quote_provider"] = quote_providers.get(candidate["instrument_id"], universe.metadata.provider)
        records = merge_observations(previous.get("records", []) if previous.get("screen_version") == SCREEN_VERSION else [], candidates, now=observed,
                                     evaluated_ids=evaluated_ids)
        result.update(coverage, generated_at=observed.isoformat(), records=records,
            scan_candidates=candidates,
            pending_candidates=pending, pending_count=len(pending),
            price_history_metadata=[h.metadata.model_dump(mode="json") for h in histories],
            last_scan_slot=slot.isoformat(), requested_count=universe.requested_count,
            missing_quote_count=len(universe.missing_instrument_ids),
            status="monitoring" if coverage["evaluated_count"] else "unavailable",
            message=("午盘复核（11:30 收市行情）：命中即提示，当日去重。" if slot.hour == 11 and slot.minute == 45
                     else "每 15 分钟扫描：命中即提示，当日去重；盘中预估，收盘前可能失效。") if coverage["evaluated_count"] else "没有可可靠计算的实时行情，暂停提示。",
            source_metadata=universe.metadata.model_dump(mode="json"),
            quote_provider_counts=dict(Counter(quote_providers.values())),
            baseline_dates=[d.isoformat() for d in dates],
            seed_metadata=[s.metadata.model_dump(mode="json") for s in seeds])
    except Exception:
        import logging
        logging.getLogger(__name__).exception("intraday MACD J scan unavailable")
        result.update(status="unavailable", message="所需行情或历史指标暂不可用，等待下一次计划扫描。",
                      last_scan_slot=slot.isoformat() if slot else None,
                      retry_after=(now + timedelta(minutes=10)).isoformat())
    if force and result.get("status") == "monitoring":
        result["message"] = ("手动重扫完成；" + ("使用午盘收市行情；" if midday else "")
                             + f"命中 {len(result.get('scan_candidates', []))} 只，盘中预估。")
    result = _with_coverage_status(result)
    _write(path, result)
    archive_scan(result, root=root)
    return result
