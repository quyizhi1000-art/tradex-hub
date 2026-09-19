"""Atomic research publication tolerates brief Windows reader sharing locks."""
import json
import os
import time
from pathlib import Path


def write_json(path: Path, payload) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    for attempt in range(3):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 2:
                raise
            time.sleep(.1 * (attempt + 1))
