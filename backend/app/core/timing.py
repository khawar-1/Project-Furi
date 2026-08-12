"""
Furi OS — per-turn timing instrumentation.

One TurnTimer per chat turn collects named stage durations and emits a SINGLE
summary log line at INFO, e.g.:

    chat turn timings [a1b2c3d4]: restore=2ms route=310ms context=145ms
    persist=18ms ttft=420ms total=1610ms

This exists so "Furi feels slow" is attributable to a stage instead of a
guess, and so a latency regression shows up in the logs of the run that
introduced it. Stage names are free-form; `mark()` records time-since-start
(used for time-to-first-token), `stage()`/`start()`+`stop()` record a span.

Strictly best-effort observability: logging failures are swallowed, `log()`
emits at most once per timer (a turn that ends inside a router logs there;
the fallthrough path must never double-log), no global state, no dependency.
"""
import time
from contextlib import contextmanager
from typing import Iterator, Optional

from loguru import logger


class TurnTimer:
    """Collects (name, milliseconds) stages for one turn."""

    def __init__(self, label: str, session_id: Optional[str] = None) -> None:
        self.label = label
        self.session_id = session_id
        self._start = time.perf_counter()
        self._stages: list[tuple[str, float]] = []
        self._open: dict[str, float] = {}
        self._logged = False

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """Time a block; the duration is recorded even when the block raises."""
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.record(name, (time.perf_counter() - t0) * 1000.0)

    def start(self, name: str) -> None:
        """Open a span without re-indenting the code it brackets."""
        self._open[name] = time.perf_counter()

    def stop(self, name: str) -> None:
        """Close a span opened by start(); unknown names are ignored."""
        t0 = self._open.pop(name, None)
        if t0 is not None:
            self.record(name, (time.perf_counter() - t0) * 1000.0)

    def record(self, name: str, ms: float) -> None:
        self._stages.append((name, ms))

    def mark(self, name: str) -> None:
        """Record the time elapsed since the turn started (e.g. ttft)."""
        self.record(name, self.elapsed_ms())

    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self._start) * 1000.0

    def log(self) -> None:
        """Emit the one summary line; only the first call logs."""
        if self._logged:
            return
        self._logged = True
        try:
            parts = [f"{name}={ms:.0f}ms" for name, ms in self._stages]
            parts.append(f"total={self.elapsed_ms():.0f}ms")
            sid = f" [{self.session_id[:8]}]" if self.session_id else ""
            logger.info(f"{self.label} timings{sid}: {' '.join(parts)}")
        except Exception:
            pass
