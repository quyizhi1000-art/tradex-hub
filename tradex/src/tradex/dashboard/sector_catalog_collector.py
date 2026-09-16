"""Collector adapter over existing full rotation snapshots and curve machinery."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from tradex.market_watch.contracts import SectorFlowSeriesV1
from tradex.market_watch.integrity import stable_sha256
from tradex.market_watch.sector_catalog import SectorCatalogStore, build_catalog, catalog_db_path
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
_last_optional_target = {}


def _repair_one_hotspot(catalog, observed):
    from tradex.data_gateway.sector_flow import refresh_optional_sector_intraday_fund_flow

    if catalog.trade_date != observed.date().isoformat():
        return
    targets = sorted((e for e in catalog.entries if e.hot_state != "none"
                      and e.curve_support == "same_source" and e.missing_minutes
                      and not e.legacy_keys), key=lambda e: e.sector_key)
    if not targets:
        return
    last = _last_optional_target.get(catalog.trade_date, "")
    entry = next((e for e in targets if e.sector_key > last), targets[0])
    result = refresh_optional_sector_intraday_fund_flow(dict(
        sector_key=entry.sector_key, name=entry.name, taxonomy=entry.taxonomy,
        provider_sector_code=entry.provider_sector_code, source_family=entry.source_family,
    ), observed_at=observed)
    if result is not None:
        _last_optional_target.clear()
        _last_optional_target[catalog.trade_date] = entry.sector_key


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
    supplements = read_sector_intraday_fund_flow_backfill(trading_date=day)
    source_revision = stable_sha256({
        "rules": "sector-catalog-v1.2",
        "rows": [(r["minute_bucket"], hashlib.sha256(r["payload_blob"]).hexdigest()) for r in rows],
        "supplements": supplements,
    })
    with SectorCatalogStore(path) as reader:
        previous = reader.latest()
    if previous and previous.source_revision == source_revision:
        if schedule_backfill:
            _repair_one_hotspot(previous, now or datetime.now(ZoneInfo("Asia/Shanghai")))
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
                            source_revision=source_revision, as_of=as_of, generated_at=observed, quote_as_of=quote_as_of)
    with SectorCatalogStore(path, read_only=False) as writer:
        writer.record(catalog, curves)
    if schedule_backfill:
        _repair_one_hotspot(catalog, now or datetime.now(ZoneInfo("Asia/Shanghai")))
    return {"action": "recorded", "catalog_revision": catalog.catalog_revision, **catalog.counts}
