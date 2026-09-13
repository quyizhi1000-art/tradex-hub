"""Manual portfolio commands with canonical identity and hard limits."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from tradex.data_gateway.securities import canonical_instrument_id
from tradex.instrument_taxonomy.store import InstrumentTaxonomyReader

from .contracts import ManualPortfolioEntryV1, ManualPortfolioV1
from .store import ManualPortfolioStore, digest, portfolio_revision


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _now(value: datetime | None = None) -> datetime:
    observed = value or datetime.now(SHANGHAI)
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("manual portfolio time must include a timezone")
    return observed.astimezone(SHANGHAI)


def _optional_text(value: Any, *, limit: int) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    if len(normalized) > limit:
        raise ValueError(f"文本最多 {limit} 个字符")
    return normalized


class ManualPortfolioService:
    def __init__(
        self,
        store: ManualPortfolioStore,
        *,
        taxonomy_reader_factory=InstrumentTaxonomyReader,
    ) -> None:
        self.store = store
        self._taxonomy_reader_factory = taxonomy_reader_factory

    def _identity(self, symbol: str, display_name: str | None) -> tuple[str, str | None, str, str]:
        query = str(symbol or "").strip()
        with self._taxonomy_reader_factory() as reader:
            try:
                instrument_id = canonical_instrument_id(query)
            except ValueError as code_error:
                finder = getattr(reader, "find_by_name", None)
                matches = tuple(finder(query)) if callable(finder) else ()
                if not matches:
                    raise ValueError("未在证券目录中找到该股票名称，请输入完整名称或股票代码") from code_error
                if len(matches) > 1:
                    choices = "、".join(item.instrument_id for item in matches[:5])
                    raise ValueError(f"股票名称对应多个代码，请改用代码：{choices}")
                profile = matches[0]
                instrument_id = profile.instrument_id
            else:
                inferred = canonical_instrument_id(instrument_id[:6])
                if inferred != instrument_id:
                    raise ValueError("证券代码与交易所后缀不一致")
                profile = reader.get(instrument_id)
        if profile is None:
            return instrument_id, display_name, "format_valid_unverified", "not_available"
        name = display_name or profile.name
        attribution = getattr(profile.verification_status, "value", profile.verification_status)
        return instrument_id, name, "catalog_verified", str(attribution)

    @staticmethod
    def _revision_payload(
        *, instrument_id: str, display_name: str | None, note: str | None,
        enabled: bool, code_validation_status: str, attribution_status: str,
        added_at: datetime, updated_at: datetime,
    ) -> str:
        return digest({
            "instrument_id": instrument_id,
            "display_name": display_name,
            "note": note,
            "enabled": enabled,
            "code_validation_status": code_validation_status,
            "attribution_status": attribution_status,
            "added_at": added_at.isoformat(),
            "updated_at": updated_at.isoformat(),
        })

    def add(
        self, symbol: str, *, display_name: str | None = None,
        note: str | None = None, enabled: bool = True,
        now: datetime | None = None,
    ) -> ManualPortfolioEntryV1:
        observed = _now(now)
        name = _optional_text(display_name, limit=80)
        normalized_note = _optional_text(note, limit=500)
        instrument_id, name, code_status, attribution = self._identity(symbol, name)
        revision = self._revision_payload(
            instrument_id=instrument_id, display_name=name, note=normalized_note,
            enabled=bool(enabled), code_validation_status=code_status,
            attribution_status=attribution, added_at=observed, updated_at=observed,
        )
        entry = ManualPortfolioEntryV1(
            instrument_id=instrument_id,
            display_name=name,
            note=normalized_note,
            enabled=bool(enabled),
            code_validation_status=code_status,
            attribution_status=attribution,
            added_at=observed,
            updated_at=observed,
            revision=revision,
        )
        return self.store.put_entry(entry, require_absent=True)

    def update(
        self, symbol: str, *, display_name: Any = ..., note: Any = ...,
        enabled: Any = ..., now: datetime | None = None,
    ) -> ManualPortfolioEntryV1:
        instrument_id = canonical_instrument_id(symbol)
        existing = self.store.get_entry(instrument_id)
        if existing is None:
            raise LookupError("证券代码不在持仓观察中")
        observed = _now(now)
        name = existing.display_name if display_name is ... else _optional_text(display_name, limit=80)
        normalized_note = existing.note if note is ... else _optional_text(note, limit=500)
        normalized_enabled = existing.enabled if enabled is ... else bool(enabled)
        instrument_id, name, code_status, attribution = self._identity(instrument_id, name)
        revision = self._revision_payload(
            instrument_id=instrument_id, display_name=name, note=normalized_note,
            enabled=normalized_enabled, code_validation_status=code_status,
            attribution_status=attribution, added_at=existing.added_at, updated_at=observed,
        )
        return self.store.put_entry(ManualPortfolioEntryV1(
            instrument_id=instrument_id,
            display_name=name,
            note=normalized_note,
            enabled=normalized_enabled,
            code_validation_status=code_status,
            attribution_status=attribution,
            added_at=existing.added_at,
            updated_at=observed,
            revision=revision,
        ))

    def delete(self, symbol: str) -> bool:
        return self.store.delete_entry(canonical_instrument_id(symbol))

    def read(self, *, now: datetime | None = None) -> ManualPortfolioV1:
        items = self.store.list_entries()
        return ManualPortfolioV1(
            revision=portfolio_revision(items),
            generated_at=_now(now),
            enabled_count=sum(item.enabled for item in items),
            items=items,
        )


__all__ = ["ManualPortfolioService"]
