"""The runner: orchestrates extraction cycles against the ingestion API.

One cycle:
  1. fetch due sources from the backend (``GET /sources/due/``),
  2. for each source that passes the per-source politeness floor:
       open a run (running) → extract → bulk-submit observations → mark the
       run succeeded (events_found=N) or failed (error_message),
  3. sleep ``poll_interval_seconds`` and repeat (loop mode), or exit (once).

The per-source floor (``min_source_interval_seconds``, default 1h) is a
client-side guard *in addition* to the backend's own ``next_due_at`` cadence:
even a 5-minute cycle never re-crawls the same site more often than the floor.
Timestamps optionally persist to ``state_file`` so a restart doesn't
immediately re-crawl everything.

SIGTERM/SIGINT set a stop flag; the in-flight cycle finishes, then the loop
exits. Nothing is left half-open — a run opened before the signal is finished
(success or failure) before the process exits.
"""

from __future__ import annotations

import json
import signal
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .auth import APIKeyAuth
from .client import TerminschleuderClient
from .config import Settings
from .errors import ApiError, AuthError, ExtractorError
from .extractors import get_extractor
from .logging_setup import get_logger
from .models import DueSource, ObservationSubmit


class CrawlGate:
    """Tracks the last-crawled time per source and enforces the politeness floor.

    Optionally persists ``{source_id: iso8601}`` to ``state_file`` so a restart
    honors prior crawl times (avoids immediately re-hitting every site).
    """

    def __init__(self, min_interval_seconds: int, state_file: str = "") -> None:
        self._min_interval = min_interval_seconds
        self._state_file = Path(state_file) if state_file else None
        self._last: dict[int, datetime] = {}
        self._lock = threading.Lock()
        if self._state_file and self._state_file.exists():
            try:
                data = json.loads(self._state_file.read_text())
                for k, v in data.items():
                    self._last[int(k)] = datetime.fromisoformat(v)
            except (ValueError, OSError):
                # Corrupt state file is non-fatal: start fresh.
                self._last = {}

    def is_due(self, source_id: int, now: datetime) -> bool:
        last = self._last.get(source_id)
        if last is None:
            return True
        elapsed = (now - last).total_seconds()
        return elapsed >= self._min_interval

    def mark_crawled(self, source_id: int, now: datetime) -> None:
        with self._lock:
            self._last[source_id] = now
            self._persist()

    def next_eligible(self, source_id: int) -> datetime | None:
        last = self._last.get(source_id)
        if last is None:
            return None
        from datetime import timedelta

        return last + timedelta(seconds=self._min_interval)

    def _persist(self) -> None:
        if not self._state_file:
            return
        try:
            self._state_file.write_text(
                json.dumps({str(k): v.isoformat() for k, v in self._last.items()})
            )
        except OSError:
            pass


class Runner:
    """Drives extraction cycles. Construct once, then ``run()``."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: TerminschleuderClient | None = None,
        extractor_factory: Callable[[DueSource], object] | None = None,
        gate: CrawlGate | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._log = get_logger("extractor.runner")
        self._owns_client = client is None
        self._client = client or TerminschleuderClient(
            settings.api_base_url,
            APIKeyAuth(settings.api_key),
            timeout=settings.http_timeout_seconds,
        )
        self._gate = gate or CrawlGate(
            settings.min_source_interval_seconds, settings.state_file
        )
        # Default factory: resolve extractor by source platform, build LLM extractor.
        self._extractor_factory = extractor_factory or self._default_extractor_factory
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._stop = False

    def _default_extractor_factory(self, source: DueSource) -> object:
        return get_extractor(source.platform, settings=self._settings)

    # --- signal handling ---
    def _install_signal_handlers(self) -> None:
        def handler(signum: int, _frame: object) -> None:
            self._log.info("stop_requested", signal=signum)
            self._stop = True

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handler)
            except ValueError:
                # Signal handlers can't be set in non-main threads (tests).
                pass

    # --- public API ---
    def run(self) -> int:
        """Dispatch based on flags. Returns a process exit code."""
        if self._settings.dry_run:
            return self.dry_run()
        if self._settings.run_mode == "once":
            return self.run_once()
        return self.run_loop()

    def run_once(self) -> int:
        return self._cycle()

    def run_loop(self) -> int:
        self._install_signal_handlers()
        self._log.info(
            "loop_starting",
            poll_interval=self._settings.poll_interval_seconds,
            min_source_interval=self._settings.min_source_interval_seconds,
            model=self._settings.llm_model,
        )
        while not self._stop:
            self._cycle()
            if self._stop:
                break
            self._sleep(self._settings.poll_interval_seconds)
        self._log.info("loop_stopped")
        if self._owns_client:
            self._client.close()
        return 0

    def dry_run(self) -> int:
        """List due sources and whether each passes the floor. No runs, no submits."""
        log = self._log.bind(mode="dry-run")
        try:
            sources = self._client.get_due_sources()
        except (ApiError, AuthError) as exc:
            log.error("dry_run_failed", error=str(exc))
            return 1
        now = self._now()
        log.info("dry_run_sources", total=len(sources))
        for src in sources[: self._settings.max_sources_per_cycle]:
            due = self._gate.is_due(src.id, now)
            eligible = self._gate.next_eligible(src.id)
            log.info(
                "source",
                source_id=src.id,
                url=src.url,
                platform=src.platform,
                next_due_at=src.next_due_at.isoformat() if src.next_due_at else None,
                would_crawl=due,
                next_eligible_at=eligible.isoformat() if eligible else None,
            )
        if self._owns_client:
            self._client.close()
        return 0

    # --- internals ---
    def _cycle(self) -> int:
        log = self._log.bind(cycle=self._now().isoformat())
        try:
            sources = self._client.get_due_sources()
        except (ApiError, AuthError) as exc:
            log.error("cycle_failed_fetching_sources", error=str(exc))
            return 1

        limited = sources[: self._settings.max_sources_per_cycle]
        log.info("cycle_started", due_sources=len(sources), processing=len(limited))
        for source in limited:
            if self._stop:
                log.info("cycle_interrupted")
                break
            self._process_source(source)
        log.info("cycle_finished")
        return 0

    def _process_source(self, source: DueSource) -> None:
        now = self._now()
        if not self._gate.is_due(source.id, now):
            eligible = self._gate.next_eligible(source.id)
            self._log.info(
                "source_skipped_too_soon",
                source_id=source.id,
                next_eligible_at=eligible.isoformat() if eligible else None,
            )
            return
        self._gate.mark_crawled(source.id, now)

        log = self._log.bind(source_id=source.id, url=source.url)
        try:
            run = self._client.create_run(source.id)
        except Exception as exc:  # noqa: BLE001 — no run to report; just skip
            log.error("create_run_failed", error=f"{type(exc).__name__}: {exc}")
            return
        log = log.bind(run_id=run.id)
        log.info("run_created")

        try:
            extractor = self._extractor_factory(source)
            observations: list[ObservationSubmit] = extractor.extract(source)  # type: ignore[attr-defined]
            for obs in observations:
                obs.run = run.id
            if observations:
                self._client.submit_observations(observations)
                log.info("observations_submitted", count=len(observations))
            self._client.finish_run_success(run.id, events_found=len(observations))
            log.info("run_succeeded", events_found=len(observations))
        except Exception as exc:  # noqa: BLE001 — any failure is reported, not raised
            message = f"{type(exc).__name__}: {exc}"
            log.error("run_failed", error=message)
            try:
                self._client.finish_run_failure(run.id, error_message=message)
            except (ApiError, AuthError) as finish_exc:
                log.error("finish_run_failed", error=str(finish_exc))

    def _sleep(self, seconds: int) -> None:
        # Interruptible sleep: check the stop flag every second so SIGTERM
        # is observed promptly even with a long poll interval.
        remaining = seconds
        while remaining > 0 and not self._stop:
            time.sleep(min(1, remaining))
            remaining -= 1