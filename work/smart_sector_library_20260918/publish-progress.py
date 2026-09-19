"""Publish this acquisition run's progress; never assigns memberships."""
import json
import time
from pathlib import Path
from tradex.smart_sector_library.research import publish

root = Path(__file__).parent
progress_path = root / 'full_market/progress.json'
deadline = time.monotonic() + 7200
previous = None
while time.monotonic() < deadline:
    progress = json.loads(progress_path.read_text(encoding='utf-8'))
    current = (progress.get('processed'), progress.get('status'))
    if current != previous:
        print(json.dumps(publish(root), ensure_ascii=False), flush=True)
        previous = current
    if progress.get('status') != 'running' or time.time() - progress_path.stat().st_mtime > 120:
        break
    time.sleep(30)
