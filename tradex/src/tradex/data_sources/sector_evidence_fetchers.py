"""Public company-business and board-membership evidence transport."""
from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from .em_client import em_get


def fetch_sector_evidence(instrument_id: str, **kwargs) -> dict:
    if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", instrument_id):
        raise ValueError("invalid instrument identifier")
    code, exchange = instrument_id.split(".")
    url = "https://emweb.eastmoney.com/PC_HSF10/CoreConception/PageAjax"
    response = em_get(url, params={"code": exchange + code}, timeout=15, max_queue_wait=5)
    response.raise_for_status()
    return {"payload": response.json(), "source_url": response.url,
            "fetched_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()}
