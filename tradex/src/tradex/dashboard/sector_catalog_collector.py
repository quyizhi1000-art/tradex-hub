"""Collector adapter over existing full rotation snapshots and curve machinery."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from tradex.market_watch.contracts import SectorFlowSeriesV1
from tradex.market_watch.integrity import stable_sha256
from tradex.market_watch.sector_catalog import SectorCatalogStore, build_catalog, catalog_db_path, expected_minutes
from .rotation_radar import (
    ROTATION_CONFIG_VERSION, ROTATION_SCHEMA_VERSION,
    _rank_snapshot, _sector_flow_definitions_for, _sector_flow_match,
    _sector_flow_series, _sector_flow_source_family,
)
from .rotation_store import _expand_payload


@lru_cache(maxsize=8192)
def _board_key(source, taxonomy, code):
    return "board_" + stable_sha256([source, taxonomy, code])[:24]


_rank_cache_scope = None
_rank_cache = {}


def _repair_catalog_curves(catalog, observed, *, db_path=None, max_targets=32, time_budget_seconds=20.0, clock=None):
    from tradex.data_gateway.sector_flow import (
        refresh_optional_sector_intraday_fund_flow, sector_intraday_repair_window_open,
    )

    if catalog.trade_date != observed.date().isoformat():
        return False
    clock = clock or (lambda: observed)
    if not sector_intraday_repair_window_open(clock()):
        return False
    minute = observed.hour * 60 + observed.minute
    bulk_window = minute >= 900 or 690 <= minute < 780
    displayed = {e.sector_key for e in sorted(
        (e for e in catalog.entries if e.hot_state == "active" and e.point_count),
        key=lambda e: e.change_pct if e.change_pct is not None else float("-inf"), reverse=True)[:12]}
    def priority(entry):
        # Drain old/internal holes before repeatedly chasing a fresh trailing minute.
        overdue = any(int(m[:2]) * 60 + int(m[3:]) <= minute - 5 for m in entry.missing_minutes)
        return (not overdue, entry.sector_key not in displayed, entry.hot_state == "none",
                attempts.get(entry.sector_key, {}).get("last_attempt_at", ""), entry.sector_key)
    with SectorCatalogStore(db_path, read_only=False) as journal:
        attempts = journal.repair_attempts(catalog.trade_date)
        # Repair only the previous implementation's provable non-request backoff.
        # Real in-window failures retain their retry deadlines and counters.
        for key, attempt in attempts.items():
            if (attempt["outcome"] == "no_progress" and attempt["error"] == "no_missing_minutes_returned"
                    and not sector_intraday_repair_window_open(datetime.fromisoformat(attempt["last_attempt_at"]))):
                journal.defer_repair(catalog.trade_date, key, observed)
        attempts = journal.repair_attempts(catalog.trade_date)
    targets = sorted((e for e in catalog.entries
                      if e.curve_support == "same_source" and e.missing_minutes
                      and e.observed_today
                      and (e.sector_key not in attempts or
                           datetime.fromisoformat(attempts[e.sector_key]["next_retry_at"]) <= observed)),
                     key=priority)
    if not targets:
        return False
    started = time.monotonic()
    improved = False
    consecutive_failures = 0
    budget = time_budget_seconds if bulk_window else min(time_budget_seconds, 10.0)
    for entry in targets[:max_targets if bulk_window else min(max_targets, 4)]:
        attempt_at = clock()
        if (time.monotonic() - started >= budget
                or attempt_at.date().isoformat() != catalog.trade_date
                or not sector_intraday_repair_window_open(attempt_at)):
            break
        with SectorCatalogStore(db_path, read_only=False) as journal:
            journal.begin_repair(catalog.trade_date, entry.sector_key, attempt_at)
        error = None
        try:
            result = refresh_optional_sector_intraday_fund_flow(dict(
                sector_key=entry.sector_key, name=entry.name, taxonomy=entry.taxonomy,
                provider_sector_code=entry.provider_sector_code, source_family=entry.source_family,
            ), observed_at=attempt_at)
        except Exception as exc:
            result, error = {}, type(exc).__name__
        if result is None:
            with SectorCatalogStore(db_path, read_only=False) as journal:
                journal.defer_repair(catalog.trade_date, entry.sector_key, attempt_at)
            break
        minutes = {str(point["provider_as_of"])[11:16]
                   for point in (result or {}).get(entry.sector_key, ())}
        gained = bool(minutes.intersection(entry.missing_minutes))
        with SectorCatalogStore(db_path, read_only=False) as journal:
            journal.finish_repair(catalog.trade_date, entry.sector_key, attempt_at, gained=gained,
                                  error=error or (None if gained else "no_missing_minutes_returned"))
        improved = improved or gained
        consecutive_failures = 0 if gained else consecutive_failures + 1
        if consecutive_failures >= 3:
            break
    return improved


def _identity(item):
    source = _sector_flow_source_family(item.get("source"))
    key = _board_key(source, item["taxonomy"], item["board_code"])
    return key, source


def _hot_evidence(ranked, definitions):
    """Two distinct fresh provider times confirm any rule; retain the day's hits."""
    result = {}
    for definition in definitions:
        hits = []
        seen = set()
        for snapshot in ranked:
            item = _sector_flow_match(snapshot, definition)
            if not item or not item.get("effective"):
                continue
            timestamp = item.get("provider_as_of")
            if not timestamp or timestamp in seen:
                continue
            seen.add(timestamp)
            reasons = []
            if (item.get("change_pct") or 0) >= 2 and (item.get("price_percentile") or 0) >= .9:
                reasons.append("涨幅≥2%且处于同类前10%")
            if (item.get("flow_amount") or 0) > 0 and (item.get("flow_percentile") or 0) >= .9:
                reasons.append("主力净流入为正且净流占比处于同类前10%")
            if reasons:
                hits.append((timestamp, reasons))
        if len(hits) >= 2:
            current = _sector_flow_match(ranked[-1], definition)
            result[definition["key"]] = dict(
                reasons=sorted({reason for _, reasons in hits for reason in reasons}),
                first_at=hits[0][0], last_at=hits[-1][0],
                active=bool(current and current.get("effective") and current.get("provider_as_of") == hits[-1][0]),
            )
    return result


def refresh_sector_catalog(*, now=None, schedule_backfill=True, db_path=None):
    """One restartable reconcile/download/publish/check cycle, owned by Collector."""
    observed = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    if observed.tzinfo is None:
        raise ValueError("catalog recovery requires timezone")
    observed = observed.astimezone(ZoneInfo("Asia/Shanghai"))
    path = Path(db_path or catalog_db_path()).resolve()
    try:
        result = _materialize_sector_catalog(now=observed, db_path=path)
        with SectorCatalogStore(path, read_only=False) as journal:
            catalog = journal.latest()
            if catalog is None:
                return result
            journal.record_recovery(catalog, observed, state="running" if schedule_backfill else None)
        repair_clock = (lambda: now.astimezone(ZoneInfo("Asia/Shanghai"))) if now is not None else (
            lambda: datetime.now(ZoneInfo("Asia/Shanghai")))
        if schedule_backfill and _repair_catalog_curves(catalog, observed, db_path=path, clock=repair_clock):
            result = _materialize_sector_catalog(now=observed, db_path=path)
        with SectorCatalogStore(path, read_only=False) as journal:
            journal.record_recovery(journal.latest(), observed)
        return result
    except Exception as exc:
        # Keep download checkpoints and the old published revision; next cycle
        # can publish successful cache writes without downloading them again.
        with SectorCatalogStore(path, read_only=False) as journal:
            catalog = journal.latest()
            if catalog is not None:
                journal.record_recovery(catalog, observed, state="failed", error=type(exc).__name__)
        raise


def _materialize_sector_catalog(*, now, db_path):
    """No new quote requests: materialize all observed boards, with bounded repair."""
    from tradex.data_gateway.sector_flow import (
        read_sector_intraday_fund_flow_backfill,
    )

    global _rank_cache_scope, _rank_cache
    observed = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    path = Path(db_path or catalog_db_path()).resolve()
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as source:
        source.row_factory = sqlite3.Row
        rows = source.execute(
            "SELECT * FROM rotation_snapshots WHERE trade_date=(SELECT MAX(trade_date) FROM rotation_snapshots) "
            "AND schema_version=? AND config_version=? ORDER BY minute_bucket",
            (ROTATION_SCHEMA_VERSION, ROTATION_CONFIG_VERSION)).fetchall()
    if not rows:
        return {"action": "unavailable", "reason": "no_rotation_snapshots"}
    day = rows[-1]["trade_date"]
    actual_cutoff = datetime.fromisoformat(rows[-1]["minute_bucket"])
    close = actual_cutoff.replace(hour=15, minute=0, second=0, microsecond=0)
    coverage = max(actual_cutoff, min(observed, close))
    due_minutes = expected_minutes(coverage)
    if due_minutes:
        coverage = max(actual_cutoff, datetime.fromisoformat(f"{day}T{due_minutes[-1]}:00+08:00"))
    supplements = read_sector_intraday_fund_flow_backfill(trading_date=day)
    source_revision = stable_sha256({
        "rules": "sector-catalog-v1.3",
        "expected_minutes": due_minutes,
        "rows": [(r["minute_bucket"], hashlib.sha256(r["payload_blob"]).hexdigest()) for r in rows],
        "supplements": supplements,
    })
    with SectorCatalogStore(path) as reader:
        previous = reader.latest()
    if previous and previous.source_revision == source_revision:
        return {"action": "existing", "catalog_revision": previous.catalog_revision, **previous.counts}
    # Re-normalize only changed source minutes; the cache is confined to this
    # Collector projection and bounded to the one current day/store.
    scope = (str(path), day)
    if _rank_cache_scope != scope:
        _rank_cache_scope, _rank_cache = scope, {}
    snapshots, ranked, retained = [], [], {}
    for row in rows:
        minute = row["minute_bucket"]
        digest = hashlib.sha256(row["payload_blob"]).digest()
        cached = _rank_cache.get(minute)
        if cached is None or cached[0] != digest:
            normalized = _expand_payload(row["payload_blob"], minute, row["market_phase"])
            rank = _rank_snapshot(normalized)
            metadata = {k: v for k, v in normalized.items() if k != "boards"}
            cached = (digest, metadata, rank)
        retained[minute] = cached
        snapshots.append(cached[1])
        ranked.append(cached[2])
    _rank_cache = retained
    as_of = datetime.fromisoformat(rows[-1]["minute_bucket"])
    quote_as_of = as_of
    # A provider-verified historical curve can reach close even when raw quote
    # snapshots end earlier. Keep the two watermarks explicit; never extend quotes.
    for points in supplements.values():
        for point in points[-1:]:
            value = point.get("provider_as_of")
            stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
            if stamp.tzinfo and stamp.date().isoformat() == day:
                as_of = max(as_of, stamp.replace(second=0, microsecond=0))
    boards, aliases = {}, {}
    for snapshot in ranked:
        for item in snapshot.values():
            key, source = _identity(item)
            boards[key] = item
            aliases.setdefault(key, set()).add(item["name"])
    roles, legacy = {}, {}
    # Classify only exact identities supported by the existing reviewed lists.
    for direction in ("defense", "offense"):
        for definition in _sector_flow_definitions_for(direction):
            for snapshot in reversed(ranked):
                item = _sector_flow_match(snapshot, definition)
                if item:
                    key, _ = _identity(item)
                    roles.setdefault(key, set()).add(direction)
                    legacy.setdefault(key, set()).add(definition["key"])
                    break
    identities, definitions = [], []
    present = {_identity(item)[0] for item in ranked[-1].values()}
    for key, item in sorted(boards.items()):
        _, source = _identity(item)
        verified = source == "eastmoney" and str(item["board_code"]).startswith("BK")
        identities.append(dict(
            sector_key=key, name=item["name"], aliases=tuple(sorted(aliases[key])),
            taxonomy=item["taxonomy"], source_family=source, provider_sector_code=item["board_code"],
            roles=tuple(sorted(roles.get(key, ()))), legacy_keys=tuple(sorted(legacy.get(key, ()))),
            observed_today=True, present_latest=key in present,
            quote_as_of=datetime.fromisoformat(item["provider_as_of"]) if item.get("provider_as_of") else None,
            quote_change_pct=item.get("change_pct"),
            curve_support="same_source" if verified else "unverified",
        ))
        definitions.append(dict(key=key, name=item["name"], category_key="unclassified",
                                category_name="全量目录", follow_eligible=False,
                                catalog_identity=item["id"], source_family=source))
    # Keep prior directory entries visible; absence is not a proven delisting.
    if previous:
        for old in previous.entries:
            if old.sector_key in boards:
                continue
            identities.append(old.model_dump(include={"sector_key", "name", "aliases", "taxonomy", "source_family",
                "provider_sector_code", "roles", "legacy_keys", "curve_support"}) |
                dict(observed_today=False, present_latest=False))
            definitions.append(dict(key=old.sector_key, name=old.name, category_key="unclassified",
                category_name="目录缺失待核对", follow_eligible=False, catalog_identity="absent", source_family=old.source_family))
    curves = {}
    for identity, definition in zip(identities, definitions, strict=True):
        key = identity["sector_key"]
        points = supplements.get(key, ())
        if not points:
            points = next((supplements[k] for k in identity["legacy_keys"] if k in supplements), ())
        raw = _sector_flow_series(snapshots, ranked, definition, points, series_as_of=as_of)
        curves[key] = SectorFlowSeriesV1.model_validate(raw)
    hot = _hot_evidence(ranked, definitions)
    catalog = build_catalog(identities=identities, curves=curves, hot_evidence=hot, previous=previous,
                            source_revision=source_revision, as_of=as_of, generated_at=observed, quote_as_of=quote_as_of,
                            coverage_as_of=max(as_of, coverage))
    with SectorCatalogStore(path, read_only=False) as writer:
        writer.record(catalog, curves)
    return {"action": "recorded", "catalog_revision": catalog.catalog_revision, **catalog.counts}
