"""CLI entry point: ``python -m terminschleuder_extractor``.

Flags mirror and override env vars. ``--self-test`` verifies wiring without
secrets or network; ``--dry-run`` lists due sources without creating runs;
``--once`` runs a single cycle; default (loop) runs a cycle per
``poll_interval_seconds``.
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from .config import Settings
from .errors import ConfigError
from .logging_setup import configure_logging, get_logger


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="terminschleuder_extractor",
        description=(
            "LLM-based extractor for the terminschleuder ingestion API. "
            "Fetches due sources, extracts events via an OpenAI-compatible "
            "endpoint, and submits them as pending observations."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="run a single cycle and exit")
    mode.add_argument("--dry-run", action="store_true", help="list due sources; create no runs")
    mode.add_argument(
        "--self-test",
        action="store_true",
        help="verify wiring (config/auth/extractor) without secrets or network, then exit",
    )
    parser.add_argument("--log-level", default=None, help="e.g. DEBUG, INFO (default: INFO)")
    parser.add_argument(
        "--log-format", choices=["console", "json"], default=None, help="default: console"
    )
    parser.add_argument("--max-sources", type=int, default=None, help="cap sources per cycle")
    parser.add_argument(
        "--poll-interval", type=int, default=None, help="seconds between cycles (loop mode)"
    )
    parser.add_argument(
        "--min-source-interval",
        type=int,
        default=None,
        help="per-source politeness floor in seconds (never re-crawl a site sooner)",
    )
    return parser


def _apply_overrides(settings: Settings, args: argparse.Namespace) -> Settings:
    if args.log_level is not None:
        settings.log_level = args.log_level
    if args.log_format is not None:
        settings.log_format = args.log_format
    if args.max_sources is not None:
        settings.max_sources_per_cycle = args.max_sources
    if args.poll_interval is not None:
        settings.poll_interval_seconds = args.poll_interval
    if args.min_source_interval is not None:
        settings.min_source_interval_seconds = args.min_source_interval
    if args.dry_run:
        settings.dry_run = True
    if args.once:
        settings.run_mode = "once"
    return settings


def self_test(settings: Settings) -> int:
    """Construct wiring without network and print OK. Lets CI verify the image."""
    log = get_logger("extractor.selftest")
    # Config + auth (does NOT require an API key — only headers() would).
    from .auth import APIKeyAuth

    auth = APIKeyAuth(settings.api_key)
    log.info("self_test_ok", api_key_configured=auth.configured, llm_base_url=settings.llm_base_url)
    # Extractor registry resolves (imports the LLM extractor, no network).
    from .extractors import available, get_extractor_class

    get_extractor_class(None)
    log.info("self_test_extractors", registered=available())
    # Client construction does not perform a request.
    from .client import TerminschleuderClient

    # Build a client only if an API key is present (otherwise just skip).
    if auth.configured:
        TerminschleuderClient(settings.api_base_url, auth, timeout=settings.http_timeout_seconds)
    print("OK")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = Settings()
    except Exception as exc:  # pydantic-validation errors at load
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    settings = _apply_overrides(settings, args)
    configure_logging(settings)

    if args.self_test:
        return self_test(settings)

    from .runner import Runner

    try:
        return Runner(settings).run()
    except ConfigError as exc:
        get_logger("extractor").error("config_error", error=str(exc))
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())