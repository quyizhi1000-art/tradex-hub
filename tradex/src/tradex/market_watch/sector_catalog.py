"""Versioned observed-board directory and revision-bound, read-only curve access.

The Collector is the sole writer. A complete observed directory is deliberately
not a claim of complete provider or market coverage. Historical curves are never
copied into a new trading day.
"""

from __future__ import annotations

import os
import hashlib
import json
import re
import sqlite3
import zlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .contracts import ContractModel, SectorFlowSeriesV1
from .integrity import canonical_json_bytes, stable_sha256, SectorFlowSeriesIntegrityV1


def catalog_db_path() -> Path:
    return Path(os.environ.get("TRADEX_ROTATION_DB") or Path.home() / ".tradex" / "rotation_radar.sqlite3")


def catalog_series_integrity(series: SectorFlowSeriesV1) -> SectorFlowSeriesIntegrityV1:
    """Same canonical point digest, without revalidating an already strict series.

    The full directory has hundreds of thousands of typed points. Rewalking each
    point as a generic Mapping/Sequence and validating it again delays collection.
    """
    points = series.points
    encoded = json.dumps([p.model_dump(mode="json") for p in points], ensure_ascii=False,
                         sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return SectorFlowSeriesIntegrityV1(
        sector_key=series.sector_key, point_count=len(points),
        first_provider_as_of=points[0].provider_as_of if points else None,
        last_provider_as_of=points[-1].provider_as_of if points else None,
        points_revision=hashlib.sha256(encoded).hexdigest(),
    )


class SectorCatalogEntryV1(ContractModel):
    sector_key: str = Field(pattern=r"^board_[0-9a-f]{24}$")
    name: str = Field(min_length=1)
    aliases: tuple[str, ...] = ()
    taxonomy: Literal["industry", "concept"]
    source_family: str = Field(min_length=1)
    provider_sector_code: str = Field(min_length=1)
    roles: tuple[Literal["defense", "offense"], ...] = ()
    legacy_keys: tuple[str, ...] = ()
    observed_today: bool
    present_latest: bool
    change: Literal["new", "renamed", "missing", "unchanged"]
    curve_support: Literal["same_source", "unverified"]
    point_count: int = Field(ge=0, le=256)
    points_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    first_provider_as_of: datetime | None = None
    last_provider_as_of: datetime | None = None
    expected_point_count: int = Field(ge=0, le=242)
    missing_minutes: tuple[str, ...] = ()
    change_pct: float | None = None
    quote_as_of: datetime | None = None
    cumulative_cny: float | None = None
    hot_reasons: tuple[str, ...] = ()
    hot_state: Literal["none", "active", "retained"] = "none"
    first_hot_at: datetime | None = None
    last_hot_at: datetime | None = None


class SectorCatalogV1(ContractModel):
    contract: Literal["sector_catalog.v1"] = "sector_catalog.v1"
    schema_version: Literal[1] = 1
    trade_date: str
    as_of: datetime
    quote_as_of: datetime | None = None
    coverage_as_of: datetime | None = None
    generated_at: datetime
    source_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    catalog_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    coverage_basis: Literal["observed_provider_snapshots"] = "observed_provider_snapshots"
    market_coverage: Literal["unverified"] = "unverified"
    entries: tuple[SectorCatalogEntryV1, ...]
    counts: dict[str, int]

    @model_validator(mode="after")
    def validate_manifest(self):
        if self.as_of.tzinfo is None or self.generated_at.tzinfo is None:
            raise ValueError("catalog timestamps require timezone")
        if self.as_of.date().isoformat() != self.trade_date:
            raise ValueError("catalog as_of must identify trading day")
        if self.coverage_as_of is not None and (self.coverage_as_of.tzinfo is None
                or self.coverage_as_of.date().isoformat() != self.trade_date
                or self.coverage_as_of < self.as_of):
            raise ValueError("coverage cutoff must cover actual data in the same trading day")
        keys = [entry.sector_key for entry in self.entries]
        if len(keys) != len(set(keys)) or self.counts != catalog_counts(self.entries):
            raise ValueError("catalog entries/counts mismatch")
        if stable_sha256(self.model_dump(mode="json", exclude={"catalog_revision"})) != self.catalog_revision:
            # Additive optional watermark fields must not invalidate previously
            # archived manifests; verify their original explicitly supplied shape.
            original = self.model_dump(mode="json", exclude={"catalog_revision"}, exclude_unset=True)
            if stable_sha256(original) != self.catalog_revision:
                raise ValueError("catalog digest mismatch")
        return self


class SectorCatalogRecoveryV1(ContractModel):
    contract: Literal["sector_catalog_recovery.v1"] = "sector_catalog_recovery.v1"
    trade_date: str
    catalog_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    checked_at: datetime
    coverage_as_of: datetime
    state: Literal["pending", "running", "backoff", "complete", "unavailable", "failed"]
    total_series: int = Field(ge=0)
    missing_series: int = Field(ge=0)
    repairable_series: int = Field(ge=0)
    attempt_count: int = Field(ge=0)
    failed_targets: int = Field(ge=0)
    next_retry_at: datetime | None = None
    last_error: str | None = None

    @model_validator(mode="after")
    def validate_recovery(self):
        if not 0 <= self.repairable_series <= self.missing_series <= self.total_series:
            raise ValueError("recovery coverage counts mismatch")
        if self.state == "complete" and self.missing_series:
            raise ValueError("recovery cannot complete with unpublished minutes")
        if any(t.tzinfo is None for t in (self.checked_at, self.coverage_as_of, self.next_retry_at) if t is not None):
            raise ValueError("recovery timestamps require timezone")
        return self


def catalog_counts(entries) -> dict[str, int]:
    return {
        "total": len(entries),
        "observed": sum(e.observed_today for e in entries),
        "mapped": sum(bool(e.roles) for e in entries),
        "unclassified": sum(not e.roles for e in entries),
        "with_curve": sum(e.point_count > 0 for e in entries),
        "history_missing": sum(bool(e.missing_minutes) for e in entries),
        "curve_unverified": sum(e.curve_support == "unverified" for e in entries),
        "missing_latest": sum(not e.present_latest for e in entries),
        "hot": sum(e.hot_state != "none" for e in entries),
        "new": sum(e.change == "new" for e in entries),
        "renamed": sum(e.change == "renamed" for e in entries),
    }


def expected_minutes(as_of: datetime) -> tuple[str, ...]:
    """Provider minute curves begin at 09:31, not the auction or 09:30."""
    end = as_of.hour * 60 + as_of.minute
    return tuple(f"{minute // 60:02d}:{minute % 60:02d}"
                 for minute in (*range(571, 691), *range(781, 901)) if minute <= end)


def build_catalog(*, identities, curves, hot_evidence, previous, source_revision,
                  as_of: datetime, generated_at: datetime, quote_as_of=None, coverage_as_of=None) -> SectorCatalogV1:
    previous_entries = {e.sector_key: e for e in previous.entries} if previous else {}
    same_day = previous is not None and previous.trade_date == as_of.date().isoformat()
    required = expected_minutes(coverage_as_of or as_of)
    entries = []
    for identity in identities:
        identity = dict(identity)
        quote_change = identity.pop("quote_change_pct", None)
        key = identity["sector_key"]
        old = previous_entries.get(key)
        curve = curves[key]
        integrity = catalog_series_integrity(curve)
        times = {p.provider_as_of.strftime("%H:%M") for p in curve.points}
        evidence = hot_evidence.get(key, {})
        reasons = tuple(evidence.get("reasons", ()))
        was_hot = bool(same_day and old and old.hot_state != "none")
        present = identity["present_latest"]
        active = bool(reasons and evidence.get("active") and present)
        hot = bool(reasons) or was_hot
        aliases = set(identity.pop("aliases", ())) | ({*old.aliases, old.name} if old else set())
        aliases.discard(identity["name"])
        change = ("missing" if not present else "new" if old is None
                  else "renamed" if old.name != identity["name"] else "unchanged")
        if same_day and old and change == "unchanged" and old.change in {"new", "renamed"}:
            change = old.change
        latest = curve.latest
        entries.append(SectorCatalogEntryV1(
            **identity, aliases=tuple(sorted(aliases)), change=change,
            **integrity.model_dump(exclude={"sector_key"}),
            expected_point_count=len(required),
            missing_minutes=tuple(t for t in required if t not in times),
            change_pct=quote_change if quote_change is not None else latest.change_pct if latest else None,
            cumulative_cny=latest.cumulative_cny if latest else None,
            hot_reasons=tuple(sorted(set(reasons) | (set(old.hot_reasons) if was_hot else set()))),
            hot_state="active" if active else "retained" if hot else "none",
            first_hot_at=(old.first_hot_at if was_hot else evidence.get("first_at")),
            last_hot_at=evidence.get("last_at") or (old.last_hot_at if was_hot else None),
        ))
    entries.sort(key=lambda e: (e.hot_state != "active", e.hot_state == "none", e.taxonomy, e.name, e.sector_key))
    body = dict(contract="sector_catalog.v1", schema_version=1,
                trade_date=as_of.date().isoformat(), as_of=as_of,
                quote_as_of=quote_as_of or as_of,
                coverage_as_of=coverage_as_of or as_of,
                generated_at=generated_at, source_revision=source_revision,
                coverage_basis="observed_provider_snapshots", market_coverage="unverified",
                entries=tuple(entries), counts=catalog_counts(entries))
    return SectorCatalogV1(**body, catalog_revision=stable_sha256(body))


class SectorCatalogStore:
    """Additional tables in the existing rotation store; atomic manifest + curves."""

    def __init__(self, path=None, *, read_only=True):
        self.path = Path(path or catalog_db_path()).resolve()
        self.read_only = read_only
        self.connection = None
        if read_only and not self.path.exists():
            return
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path.as_uri() + ("?mode=ro" if read_only else "?mode=rwc"), uri=True, timeout=5)
        if read_only:
            self.connection.execute("PRAGMA query_only=ON")
        else:
            self.connection.executescript("""
                CREATE TABLE IF NOT EXISTS sector_catalog_days (
                    trade_date TEXT PRIMARY KEY, revision TEXT NOT NULL,
                    source_revision TEXT NOT NULL, manifest BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS sector_catalog_curves (
                    trade_date TEXT NOT NULL, sector_key TEXT NOT NULL,
                    revision TEXT NOT NULL, payload BLOB NOT NULL,
                    PRIMARY KEY(trade_date, sector_key));
                CREATE TABLE IF NOT EXISTS sector_catalog_repair_attempts (
                    trade_date TEXT NOT NULL, sector_key TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL, failures INTEGER NOT NULL,
                    last_attempt_at TEXT NOT NULL, next_retry_at TEXT NOT NULL,
                    outcome TEXT NOT NULL, error TEXT,
                    PRIMARY KEY(trade_date, sector_key));
                CREATE TABLE IF NOT EXISTS sector_catalog_recovery (
                    trade_date TEXT PRIMARY KEY, payload TEXT NOT NULL);
            """)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self.connection:
            self.connection.close()

    def repair_attempts(self, day):
        if self.connection is None or not self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='sector_catalog_repair_attempts'").fetchone():
            return {}
        cursor = self.connection.execute("SELECT * FROM sector_catalog_repair_attempts WHERE trade_date=?", (day,))
        columns = [c[0] for c in cursor.description]
        return {row[1]: dict(zip(columns, row)) for row in cursor}

    def begin_repair(self, day, key, observed):
        """Commit a short retry lease before I/O, so crashes cannot spin on restart."""
        with self.connection:
            self.connection.execute("""INSERT INTO sector_catalog_repair_attempts
                VALUES (?,?,1,0,?,?,'running',NULL) ON CONFLICT(trade_date,sector_key)
                DO UPDATE SET attempt_count=attempt_count+1, last_attempt_at=excluded.last_attempt_at,
                next_retry_at=excluded.next_retry_at, outcome='running', error=NULL""",
                (day, key, observed.isoformat(), (observed + timedelta(seconds=30)).isoformat()))

    def finish_repair(self, day, key, observed, *, gained, error=None):
        previous = self.repair_attempts(day)[key]
        failures = 0 if gained else previous["failures"] + 1
        delay = min(1800, 30 * 2 ** min(failures - 1, 6)) if failures else 30
        with self.connection:
            self.connection.execute("""UPDATE sector_catalog_repair_attempts
                SET failures=?,next_retry_at=?,outcome=?,error=? WHERE trade_date=? AND sector_key=?""",
                (failures, (observed + timedelta(seconds=delay)).isoformat(),
                 "downloaded" if gained else "no_progress", error, day, key))

    def defer_repair(self, day, key, observed):
        """Admission rejection is not a failed source request; preserve the audit count."""
        with self.connection:
            self.connection.execute("""UPDATE sector_catalog_repair_attempts SET
                failures=0,next_retry_at=?,outcome='deferred',error=NULL
                WHERE trade_date=? AND sector_key=?""", (observed.isoformat(), day, key))

    def recovery(self, day):
        if self.connection is None or not self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='sector_catalog_recovery'").fetchone():
            return None
        row = self.connection.execute("SELECT payload FROM sector_catalog_recovery WHERE trade_date=?", (day,)).fetchone()
        return SectorCatalogRecoveryV1.model_validate_json(row[0]).model_dump(mode="json") if row else None

    def record_recovery(self, catalog, observed, *, state=None, error=None):
        """Completion is bound to published coverage, never a successful download."""
        missing = [e for e in catalog.entries if e.missing_minutes]
        missing_keys = {e.sector_key for e in missing}
        supported = [e for e in missing if e.observed_today and e.curve_support == "same_source"]
        attempts = self.repair_attempts(catalog.trade_date)
        due = [e for e in supported if e.sector_key not in attempts or
               datetime.fromisoformat(attempts[e.sector_key]["next_retry_at"]) <= observed]
        same_day = catalog.trade_date == observed.date().isoformat()
        deadlines = [attempts[e.sector_key]["next_retry_at"] for e in supported if e.sector_key in attempts]
        if state is None:
            state = ("complete" if not missing else "unavailable" if not same_day or not supported
                     else "pending" if due else "backoff")
        value = dict(contract="sector_catalog_recovery.v1", trade_date=catalog.trade_date,
                     catalog_revision=catalog.catalog_revision, checked_at=observed.isoformat(),
                     coverage_as_of=(catalog.coverage_as_of or catalog.as_of).isoformat(), state=state,
                     total_series=len(catalog.entries), missing_series=len(missing),
                     repairable_series=len(supported) if same_day else 0,
                     attempt_count=sum(a["attempt_count"] for a in attempts.values()),
                     failed_targets=sum(a["failures"] > 0 for key, a in attempts.items() if key in missing_keys),
                     next_retry_at=min(deadlines) if deadlines and not due and same_day else None,
                     last_error=error)
        value = SectorCatalogRecoveryV1.model_validate(value).model_dump(mode="json")
        with self.connection:
            self.connection.execute("INSERT OR REPLACE INTO sector_catalog_recovery VALUES (?,?)",
                                    (catalog.trade_date, json.dumps(value, ensure_ascii=False)))
        return value

    def latest(self) -> SectorCatalogV1 | None:
        if self.connection is None:
            return None
        if not self.connection.execute("SELECT 1 FROM sqlite_master WHERE name='sector_catalog_days'").fetchone():
            return None
        row = self.connection.execute("SELECT manifest FROM sector_catalog_days ORDER BY trade_date DESC LIMIT 1").fetchone()
        return SectorCatalogV1.model_validate_json(zlib.decompress(row[0])) if row else None

    def record(self, catalog: SectorCatalogV1, curves: dict[str, SectorFlowSeriesV1]):
        if self.read_only or self.connection is None:
            raise RuntimeError("catalog writer is required")
        if set(curves) != {e.sector_key for e in catalog.entries}:
            raise ValueError("catalog must retain every curve, including unavailable ones")
        rows = []
        for entry in catalog.entries:
            curve = curves[entry.sector_key]
            if catalog_series_integrity(curve).points_revision != entry.points_revision:
                raise ValueError("curve digest does not match catalog")
            rows.append((catalog.trade_date, entry.sector_key, catalog.catalog_revision,
                         zlib.compress(canonical_json_bytes(curve))))
        with self.connection:
            self.connection.execute("DELETE FROM sector_catalog_curves WHERE trade_date=?", (catalog.trade_date,))
            self.connection.executemany("INSERT INTO sector_catalog_curves VALUES (?,?,?,?)", rows)
            self.connection.execute("INSERT OR REPLACE INTO sector_catalog_days VALUES (?,?,?,?)",
                                    (catalog.trade_date, catalog.catalog_revision, catalog.source_revision,
                                     zlib.compress(canonical_json_bytes(catalog))))

    def detail(self, revision: str, keys: tuple[str, ...]) -> dict:
        if not re.fullmatch(r"[0-9a-f]{64}", revision or ""):
            raise ValueError("invalid catalog revision")
        if not keys or len(keys) > 32 or len(set(keys)) != len(keys):
            raise ValueError("select 1 to 32 unique sector keys per request")
        if any(not re.fullmatch(r"board_[0-9a-f]{24}", k) for k in keys):
            raise ValueError("invalid sector key")
        if self.connection is None:
            raise LookupError("catalog unavailable")
        with self.connection:
            self.connection.execute("BEGIN")
            catalog = self.latest()
            if catalog is None:
                raise LookupError("catalog unavailable")
            if catalog.catalog_revision != revision:
                raise RuntimeError("catalog_revision_changed")
            entries = {e.sector_key: e for e in catalog.entries}
            if any(k not in entries for k in keys):
                raise ValueError("sector is not in this catalog")
            series = []
            for key in keys:
                row = self.connection.execute(
                    "SELECT revision,payload FROM sector_catalog_curves WHERE trade_date=? AND sector_key=?",
                    (catalog.trade_date, key)).fetchone()
                if not row or row[0] != revision:
                    raise RuntimeError("catalog_revision_changed")
                item = SectorFlowSeriesV1.model_validate_json(zlib.decompress(row[1]))
                if catalog_series_integrity(item).points_revision != entries[key].points_revision:
                    raise RuntimeError("catalog_curve_integrity_failed")
                series.append(item.model_dump(mode="json"))
        return dict(contract="sector_catalog_detail.v1", schema_version=1,
                    catalog_revision=revision, source_revision=catalog.source_revision,
                    trade_date=catalog.trade_date, as_of=catalog.as_of.isoformat(), sectors=series)
