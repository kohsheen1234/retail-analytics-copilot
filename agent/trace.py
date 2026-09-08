"""Replayable per-question event log at traces/<id>.jsonl.

One JSON object per line, using the starter's `TraceEvent` contract: timestamp,
responsibility, attempt, inputs, outputs, elapsed_ms. Written incrementally so a crash
mid-question still leaves everything up to the failure on disk.

Two things kept out of the log on purpose: nothing outside the project directory is
written, and result rows are truncated to a preview. A trace carrying 1000 rows per
attempt would be unreadable in the live session, which is the trace's actual job.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from models import TraceEvent

PREVIEW_ROWS = 5
PREVIEW_CHARS = 800


def _shrink(value: Any) -> Any:
    """Keep the log readable and JSON-serialisable."""
    if isinstance(value, (str, bytes)):
        text = value.decode("utf-8", "replace") if isinstance(value, bytes) else value
        return text if len(text) <= PREVIEW_CHARS else text[:PREVIEW_CHARS] + f"...[+{len(text)-PREVIEW_CHARS} chars]"
    if isinstance(value, dict):
        return {k: _shrink(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        head = [_shrink(v) for v in list(value)[:PREVIEW_ROWS]]
        extra = len(value) - PREVIEW_ROWS
        return head + [f"...[+{extra} more]"] if extra > 0 else head
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)


class Tracer:
    """Collects events for one question and appends them to its trace file."""

    def __init__(self, question_id: str, traces_dir: Path):
        self.question_id = question_id
        traces_dir.mkdir(parents=True, exist_ok=True)
        self.path = traces_dir / f"{question_id}.jsonl"
        self.path.write_text("", encoding="utf-8")   # fresh run, not appended history
        self.events: list[TraceEvent] = []

    def event(self, responsibility: str, inputs: dict, outputs: dict,
              elapsed_ms: int, attempt: int = 0) -> None:
        ev = TraceEvent(
            ts=datetime.now(timezone.utc).isoformat(),
            question_id=self.question_id,
            responsibility=responsibility,
            attempt=attempt,
            inputs=_shrink(inputs),
            outputs=_shrink(outputs),
            elapsed_ms=elapsed_ms,
        )
        self.events.append(ev)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(ev.model_dump_json() + "\n")

    def step(self, responsibility: str, inputs: dict, attempt: int = 0):
        return _Step(self, responsibility, inputs, attempt)


class _Step:
    """Context manager that times a responsibility and records it even if it raises."""

    def __init__(self, tracer: Tracer, responsibility: str, inputs: dict, attempt: int):
        self.tracer, self.responsibility, self.inputs, self.attempt = tracer, responsibility, inputs, attempt
        self.outputs: dict[str, Any] = {}

    def __enter__(self) -> "_Step":
        self.start = time.perf_counter()
        return self

    def record(self, **outputs: Any) -> None:
        self.outputs.update(outputs)

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is not None:
            self.outputs["error"] = f"{exc_type.__name__}: {exc}"
        self.tracer.event(self.responsibility, self.inputs, self.outputs,
                          int((time.perf_counter() - self.start) * 1000), self.attempt)
        return False
