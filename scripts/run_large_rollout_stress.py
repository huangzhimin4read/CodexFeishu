"""Exercise the streaming rollout reader against a generated 700 MiB source."""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codex_feishu_bridge.codex.rollout_observer import IncrementalRolloutReader


TARGET_BYTES = 700 * 1024 * 1024
SESSION = b'{"type":"session_meta","payload":{"rollout_version":"1","id":"stress-thread"}}\n'
RECORD_PREFIX = b'{"type":"internal_telemetry","payload":{"padding":"'
RECORD_SUFFIX = b'"}}\n'
VISIBLE = (
    b'{"type":"response_item","payload":{"type":"message","role":"assistant",'
    b'"phase":"final_answer","thread_id":"stress-thread","turn_id":"stress-turn",'
    b'"item_id":"final","content":[{"type":"output_text","text":"done"}]}}\n'
)


def main() -> int:
    started = time.monotonic()
    # The system TEMP drive can be small or shared by unrelated services. Keep
    # this disposable 700 MiB fixture on the project workspace volume and let
    # TemporaryDirectory remove it on every normal/exceptional exit path.
    temp_root = ROOT / ".runtime" / "stress-temp"
    temp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="codex-feishu-rollout-stress-", dir=temp_root
    ) as temp:
        source = Path(temp) / "rollout.jsonl"
        padding = b"x" * (1024 * 1024 - len(RECORD_PREFIX) - len(RECORD_SUFFIX))
        record = RECORD_PREFIX + padding + RECORD_SUFFIX
        with source.open("wb") as handle:
            handle.write(SESSION)
            while handle.tell() + len(VISIBLE) < TARGET_BYTES:
                handle.write(record)
            handle.write(VISIBLE)
        size = source.stat().st_size
        reader = IncrementalRolloutReader()
        first = reader.read(source, expected_thread_id="stress-thread")
        if size < TARGET_BYTES:
            raise RuntimeError("generated source is smaller than 700 MiB")
        if len(first.events) != 1 or first.events[0].text != "done":
            raise RuntimeError("large source visible-event result is incorrect")
        if first.cursor.committed_offset != size:
            raise RuntimeError("large source cursor did not reach the complete-record boundary")
        second = reader.read(
            source,
            first.cursor,
            expected_thread_id="stress-thread",
        )
        if second.events or second.cursor != first.cursor:
            raise RuntimeError("large source replay was not idempotent")
        with source.open("ab") as handle:
            handle.write(b'{"type":"response_item","payload":')
        partial = reader.read(
            source,
            first.cursor,
            expected_thread_id="stress-thread",
        )
        if partial.events or partial.cursor != first.cursor:
            raise RuntimeError("partial tail changed the committed cursor")
        print(
            json.dumps(
                {
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "events": len(first.events),
                    "ignored_records": first.ignored_records,
                    "size_bytes": size,
                    "status": "PASS",
                },
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
