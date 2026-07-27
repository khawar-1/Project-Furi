"""
Jarvis OS — per-run browse trace.

WHY (2026-07-26). Four live browser tasks failed on one afternoon, and every
diagnosis had to be reconstructed from a single log line per step:

    browse step 1: 'Buy Yonex badminton racket Online at Bes' (158 elements)
        -> {'action': 'extract', 'fields': ['name', 'price']}

That line says what the model DECIDED. It does not say what happened next, how
long it took, whether the extract returned anything, or which channel made the
decision. So the record could not distinguish "the page is broken" from "our
reader is blind" — and the run reported the former when the truth was the latter.
Root-causing it took reading the source, not the log.

A trace fixes that at the source: one JSONL line per step carrying what was SEEN
(url, element count, text size), what was DECIDED and BY WHAT (fast path / DOM /
vision), what RESULTED, and how long it took. It is also what makes the browse
benchmark scoreable — steps and wall-clock per task are fields here, not
estimates.

DISCIPLINE, all three non-negotiable:
  - **Never raises, and never perturbs.** A trace is an observation of a run,
    never a participant in it (the narration.py rule). Every public method
    swallows everything — and it times itself with `perf_counter`, deliberately
    NOT the `monotonic` clock the loop's own deadline reads. That is not
    fastidiousness: the loop's wall-clock-deadline test fakes `time.monotonic` to
    fire on its second call, so merely ASKING that clock for a timestamp consumed
    the run's deadline and the run then sailed past it. An observer that changes
    what it observes is worse than no observer.
  - **Bounded.** Capped lines per run, capped string sizes, capped files kept —
    an agent loop that spins must not fill a disk.
  - **Local and honest.** It records what happened, including page titles and
    URLs the run visited. Same posture as ~/.jarvis/logs/backend.log, which
    already logs those; nothing sensed elsewhere is added here.

TRACE_DIR is the injectable seam (the STT_MODEL_FACTORY pattern) so tests write
to a scratch directory and never touch the real ~/.jarvis.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from loguru import logger

# The seam. Tests point this at a tmp path; None disables tracing entirely.
TRACE_DIR: Optional[Path] = Path.home() / ".jarvis" / "logs" / "browse"

_MAX_LINES = 300          # a run is capped at 25 actions; this is generous slack
_MAX_STR = 400            # any single string field
_KEEP_FILES = 60          # newest runs kept; older trace files are swept


class BrowseTrace:
    """One run's trace. Construct at the top of run_browse, `step()` per step,
    `finish()` on every exit path."""

    def __init__(self, goal: str, *, commit: bool = False, run_id: str = "") -> None:
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.started = time.perf_counter()
        self._lines = 0
        self._path: Optional[Path] = None
        self._step_started = self.started
        try:
            directory = TRACE_DIR
            if directory is None:
                return
            directory = Path(directory)
            directory.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
            self._path = directory / f"{stamp}_{self.run_id}.jsonl"
            _sweep(directory)
            self._write(
                {
                    "event": "start",
                    "goal": _clip(goal),
                    "mode": "commit" if commit else "read",
                }
            )
        except Exception as exc:  # noqa: BLE001 — tracing never breaks a run
            logger.debug(f"browse trace unavailable: {type(exc).__name__}: {exc}")
            self._path = None

    # ------------------------------------------------------------------ writes
    def mark_step_start(self) -> None:
        """Called when a step begins, so `step()` can report its own duration
        rather than the whole run's."""
        self._step_started = time.perf_counter()

    def step(
        self,
        *,
        index: int,
        observation: Any = None,
        action: Any = None,
        source: str = "",
        result: str = "",
        note: str = "",
        records: int = 0,
    ) -> None:
        """One step. `source` is which channel decided (fast-path / dom / vision)
        — the field that would have shown, at a glance, that vision was stalling
        every step on cooling keys."""
        payload: dict[str, Any] = {
            "event": "step",
            "i": index,
            "ms": _ms(self._step_started),
        }
        if observation is not None:
            payload["url"] = _clip(str(getattr(observation, "url", "") or ""))
            payload["title"] = _clip(str(getattr(observation, "title", "") or ""), 120)
            payload["elements"] = int(getattr(observation, "element_total", 0) or 0)
            payload["text_len"] = len(
                str(getattr(observation, "text_full", "") or getattr(observation, "page_text", "") or "")
            )
        if action is not None:
            payload["action"] = _clip(json.dumps(action, default=str, ensure_ascii=False), 600)
        if source:
            payload["source"] = source
        if result:
            payload["result"] = result
        if note:
            payload["note"] = _clip(note)
        if records:
            payload["records"] = int(records)
        self._write(payload)

    def finish(
        self,
        *,
        success: bool,
        steps: int = 0,
        error: str = "",
        llm_calls: int = 0,
        vision_calls: int = 0,
        records: int = 0,
    ) -> None:
        self._write(
            {
                "event": "finish",
                "success": bool(success),
                "steps": int(steps),
                "error": _clip(error),
                "llm_calls": int(llm_calls),
                "vision_calls": int(vision_calls),
                "records": int(records),
                "ms": _ms(self.started),
            }
        )

    @property
    def path(self) -> Optional[Path]:
        return self._path

    def _write(self, payload: dict) -> None:
        if self._path is None or self._lines >= _MAX_LINES:
            return
        try:
            payload.setdefault("run", self.run_id)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
            self._lines += 1
        except Exception as exc:  # noqa: BLE001 — never breaks a run
            logger.debug(f"browse trace write failed: {type(exc).__name__}: {exc}")
            self._path = None


# ---------------------------------------------------------------------- helpers
def _clip(value: str, limit: int = _MAX_STR) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit] + "..."


def _ms(since: float) -> int:
    return int((time.perf_counter() - since) * 1000)


def _sweep(directory: Path) -> None:
    """Keep the newest _KEEP_FILES traces. Best-effort — a sweep failure must not
    stop a run from being traced."""
    try:
        files = sorted(
            (p for p in directory.glob("*.jsonl") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in files[_KEEP_FILES:]:
            try:
                os.remove(stale)
            except OSError:
                pass
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"browse trace sweep skipped: {type(exc).__name__}: {exc}")
