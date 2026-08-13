"""Tests for the runner: cycle orchestration, per-source floor, failure handling."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from terminschleuder_extractor.models import DueSource, ObservationSubmit, OrganizationMini
from terminschleuder_extractor.runner import CrawlGate, Runner


def _source(i: int =42) -> DueSource:
    return DueSource(
        id=i,
        organization=OrganizationMini(id=7, name="Acme", slug="acme"),
        url=f"https://example.com/{i}",
        platform="llm",
        fetch_interval_minutes=60,
        last_fetched_at=None,
        next_due_at=None,
    )


def _make_runner(settings, client, extractor, now=None, gate=None):
    factory = lambda source: extractor  # noqa: E731
    return Runner(
        settings,
        client=client,
        extractor_factory=factory,
        gate=gate or CrawlGate(settings.min_source_interval_seconds, ""),
        now=now or (lambda: datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)),
    )


@pytest.fixture
def extractor():
    m = MagicMock()
    m.extract.return_value = []
    return m


@pytest.fixture
def client():
    m = MagicMock()
    m.get_due_sources.return_value = [_source(42)]
    m.create_run.return_value = MagicMock(id=100)
    return m


# --- CrawlGate ---


def test_gate_first_crawl_is_due():
    gate = CrawlGate(3600, "")
    assert gate.is_due(1, datetime(2025, 1, 1, tzinfo=timezone.utc)) is True


def test_gate_blocks_within_window():
    gate = CrawlGate(3600, "")
    t0 = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    gate.mark_crawled(1, t0)
    assert gate.is_due(1, t0 + timedelta(minutes=5)) is False
    assert gate.is_due(1, t0 + timedelta(seconds=3600)) is True


def test_gate_persists_to_state_file(tmp_path):
    f = tmp_path / "state.json"
    gate = CrawlGate(3600, str(f))
    t0 = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    gate.mark_crawled(7, t0)
    gate2 = CrawlGate(3600, str(f))
    assert gate2.is_due(7, t0 + timedelta(minutes=5)) is False


def test_gate_corrupt_state_file_ignored(tmp_path):
    f = tmp_path / "state.json"
    f.write_text("{not json")
    gate = CrawlGate(3600, str(f))  # must not raise
    assert gate.is_due(1, datetime(2025, 1, 1, tzinfo=timezone.utc)) is True


# --- run_once ---


def test_run_once_happy_path(settings, client, extractor):
    extractor.extract.return_value = [
        ObservationSubmit(source=42, title="E", starts_at=datetime(2025, 9, 15, 19, 0, 0))
    ]
    runner = _make_runner(settings, client, extractor)
    code = runner.run_once()

    assert code == 0
    client.create_run.assert_called_once_with(42)
    extractor.extract.assert_called_once()
    client.submit_observations.assert_called_once()
    args = client.submit_observations.call_args[0][0]
    assert args[0].run == 100  # run id stamped onto observations
    client.finish_run_success.assert_called_once_with(100, events_found=1)
    client.finish_run_failure.assert_not_called()


def test_run_once_zero_observations_still_succeeds(settings, client, extractor):
    extractor.extract.return_value = []
    runner = _make_runner(settings, client, extractor)
    runner.run_once()
    client.submit_observations.assert_not_called()
    client.finish_run_success.assert_called_once_with(100, events_found=0)


def test_run_once_extractor_exception_reports_failure(settings, client, extractor):
    extractor.extract.side_effect = RuntimeError("boom")
    runner = _make_runner(settings, client, extractor)
    runner.run_once()
    client.finish_run_failure.assert_called_once()
    msg = client.finish_run_failure.call_args.kwargs["error_message"]
    assert "boom" in msg
    client.finish_run_success.assert_not_called()


def test_run_once_create_run_failure_skips_processing(settings, client, extractor):
    client.create_run.side_effect = RuntimeError("nope")
    runner = _make_runner(settings, client, extractor)
    runner.run_once()
    extractor.extract.assert_not_called()
    client.finish_run_failure.assert_not_called()


def test_run_once_skips_source_within_floor(settings, client, extractor, fixed_now):
    gate = CrawlGate(3600, "")
    gate.mark_crawled(42, fixed_now)  # crawled just now
    runner = _make_runner(settings, client, extractor, now=lambda: fixed_now, gate=gate)
    runner.run_once()
    client.create_run.assert_not_called()
    extractor.extract.assert_not_called()


def test_run_once_respects_max_sources(settings, client, extractor):
    sources = [_source(i) for i in range(50)]
    client.get_due_sources.return_value = sources
    settings.max_sources_per_cycle = 3
    runner = _make_runner(settings, client, extractor)
    runner.run_once()
    assert extractor.extract.call_count == 3


def test_run_once_fetch_sources_error_returns_1(settings, client, extractor):
    from terminschleuder_extractor.errors import ApiError

    client.get_due_sources.side_effect = ApiError(500, "down")
    runner = _make_runner(settings, client, extractor)
    assert runner.run_once() == 1


# --- dry_run ---


def test_dry_run_lists_sources_without_side_effects(settings, client, extractor):
    runner = _make_runner(settings, client, extractor)
    code = runner.dry_run()
    assert code == 0
    client.get_due_sources.assert_called_once()
    client.create_run.assert_not_called()
    client.submit_observations.assert_not_called()
    client.finish_run_success.assert_not_called()


# --- dispatch ---


def test_run_dispatches_dry_run(settings, client, extractor):
    settings.dry_run = True
    runner = _make_runner(settings, client, extractor)
    runner.run()
    client.get_due_sources.assert_called_once()


def test_run_dispatches_once(settings, client, extractor):
    settings.run_mode = "once"
    runner = _make_runner(settings, client, extractor)
    runner.run()
    client.create_run.assert_called_once()