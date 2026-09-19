"""Collector-owned publication of persisted, exact sector-flow history."""

from __future__ import annotations

from datetime import datetime

from tradex.data_gateway.sector_flow import expected_sector_intraday_minutes
from tradex.data_gateway.sector_flow_store import SectorFundFlowStore

from .collection_store import validate_snapshot_for_collection
from .contracts import MarketWatchSnapshotV1, SectorFlowTrajectoryV1


_FIELDS = ("sector_flow_trajectory", "offense_sector_flow_trajectory")


class IntradayTrajectoryPublisher:
    """Enrich the latest valid snapshot, without acquiring or backdating data.

    Called only by the Collector's serial loop. Curve headers avoid rebuilding
    unchanged payloads. A failed publish is retried, including after restart.
    """

    def __init__(self, *, history, ledger, rotation_loader, sector_store_factory=SectorFundFlowStore):
        self.history = history
        self.ledger = ledger
        self.rotation_loader = rotation_loader
        self.sector_store_factory = sector_store_factory
        self._last_key = None

    def __call__(self, observed: datetime) -> dict:
        try:
            return self._publish(observed)
        except Exception as error:
            self._last_key = None
            with self.sector_store_factory() as curves:
                repair = curves.read_intraday_repair(observed.date())
                if repair is not None and repair["status"] in {"partial", "complete"}:
                    curves.update_intraday_repair(
                        observed.date(), status="partial", observed_at=observed,
                        last_error=f"轨迹发布未通过验收，采集器将重试：{type(error).__name__}: {error}"[:300],
                    )
            raise

    def _publish(self, observed: datetime) -> dict:
        pointer = self.ledger.get_envelope(as_of=observed).latest_accepted_real
        if pointer is None or pointer.trade_date != observed.date():
            return {"action": "skipped", "reason": "no_current_day_accepted_snapshot"}

        # Recover a history commit whose subsequent ledger binding failed.
        record = next((item for item in self.history.get_collection_records(pointer.trade_date)
                       if item["minute_bucket"] == pointer.minute_bucket.isoformat(timespec="seconds")), None)
        if record is None:
            raise RuntimeError("trajectory publication base history is missing")
        if record["payload_digest"] != pointer.source_snapshot_revision:
            if record["record_kind"] != "accepted_real":
                raise RuntimeError("trajectory publication base is no longer acceptable")
            self.ledger.reconcile_history_record(record)
            pointer = self.ledger.get_envelope(as_of=observed).latest_accepted_real

        with self.sector_store_factory() as curves:
            manifest = curves.get_required_curve_manifest(pointer.trade_date)
            repair = curves.read_intraday_repair(pointer.trade_date)
            repair_key = None if repair is None else (
                repair["requested_at"], repair["required_through"], repair["status"],
                repair["remaining_targets"],
            )
            key = (pointer.source_snapshot_revision, manifest, repair_key)
            if key == self._last_key:
                return {"action": "unchanged", "source_snapshot_revision": pointer.source_snapshot_revision}
            if not manifest:
                return {"action": "skipped", "reason": "no_required_curves"}

            item = self.history.get_snapshot_by_pointer(
                trade_date=pointer.trade_date, minute_bucket=pointer.minute_bucket,
                snapshot_id=pointer.snapshot_id, payload_digest=pointer.source_snapshot_revision,
            )
            if item is None:
                raise RuntimeError("trajectory publication base revision changed")
            original = validate_snapshot_for_collection(item["payload"])
            payload = original.model_dump(mode="json")
            rotation = self.rotation_loader(original.as_of)
            for field in _FIELDS:
                trajectory = SectorFlowTrajectoryV1.model_validate(rotation[field])
                if trajectory.trade_date != pointer.trade_date or trajectory.as_of is None or trajectory.as_of > original.as_of:
                    raise ValueError("rebuilt trajectory exceeds the accepted snapshot time")
                if any(point.provider_as_of > original.as_of or point.provider_as_of.date() != pointer.trade_date
                       for sector in trajectory.sectors for point in sector.points):
                    raise ValueError("rebuilt trajectory contains points outside the accepted snapshot")
                old = getattr(original, field)
                new_times = {sector.sector_key: {point.provider_as_of for point in sector.points}
                             for sector in trajectory.sectors}
                if old is not None and any(
                    not {point.provider_as_of for point in sector.points}.issubset(new_times.get(sector.sector_key, set()))
                    for sector in old.sectors
                ):
                    raise ValueError("rebuilt trajectory would remove existing real points")
                payload[field] = trajectory.model_dump(mode="json")

            rebuilt = MarketWatchSnapshotV1.model_validate(payload)
            current = self.ledger.get_envelope(as_of=observed).latest_accepted_real
            if current != pointer:
                return {"action": "conflict", "reason": "collection_pointer_changed"}
            persistence = self.history.record(rebuilt, expected_payload_digest=pointer.source_snapshot_revision)
            if persistence.get("action") == "conflict":
                return persistence
            digest = persistence.get("payload_digest")
            persisted = self.history.get_snapshot_by_pointer(
                trade_date=pointer.trade_date, minute_bucket=pointer.minute_bucket,
                snapshot_id=pointer.snapshot_id, payload_digest=digest,
            )
            if persisted is None:
                raise RuntimeError("published trajectory revision cannot be read back")
            verified = validate_snapshot_for_collection(persisted["payload"])
            bound = self.ledger.reconcile_history_record(persisted)
            if bound.get("action") not in {"imported", "unchanged"}:
                raise RuntimeError("published trajectory revision cannot be bound")
            latest = self.ledger.get_envelope(as_of=observed).latest_accepted_real
            if latest is None or latest.source_snapshot_revision != digest:
                raise RuntimeError("published trajectory revision does not match ledger")

            result = {"action": persistence["action"], "source_snapshot_revision": digest,
                      "published_through": original.as_of.isoformat()}
            # The downloader's pending/running work remains under its own owner.
            # Cache-ready partial records are retried here even with zero downloads.
            if repair is not None and repair["status"] in {"partial", "complete"}:
                current_repair = curves.read_intraday_repair(pointer.trade_date)
                if current_repair != repair:
                    # A Web request or downloader advanced while we rebuilt.
                    # Leave its lifecycle alone and inspect it on the next tick.
                    return {**result, "publication_status": "retry", "reason": "repair_request_changed"}
                cutoff = datetime.fromisoformat(repair["required_through"])
                expected = expected_sector_intraday_minutes(cutoff)
                sectors = {sector.sector_key: sector
                           for field in _FIELDS for sector in getattr(verified, field).sectors}
                required_keys = {row[0] for row in manifest}
                missing = [sector_key for sector_key in sorted(required_keys)
                           if sector_key not in sectors or not expected.issubset({
                               point.provider_as_of.replace(second=0, microsecond=0)
                               for point in sectors[sector_key].points
                           })]
                complete = bool(expected and required_keys) and not missing and original.as_of >= cutoff
                status = "complete" if complete else "partial"
                error = None if complete else (
                    f"已发布至 {original.as_of:%H:%M}；目标 {cutoff:%H:%M}；"
                    f"{len(missing)} 条轨迹尚未完整交付，等待有效快照后自动发布"
                )
                if repair["status"] != status or repair["last_error"] != error:
                    repair = curves.update_intraday_repair(pointer.trade_date, status=status,
                                                          observed_at=observed, last_error=error)
                repair_key = (repair["requested_at"], repair["required_through"], repair["status"], repair["remaining_targets"])
                result.update(publication_status=status, required_through=cutoff.isoformat(), missing_target_keys=missing)
            self._last_key = (digest, manifest, repair_key)
            return result
