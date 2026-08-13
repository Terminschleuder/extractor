#!/usr/bin/env bash
# Build and run the terminschleuder-extractor container.
#   ./start.sh                  # build + run the loop (default, restarts on failure)
#   ./start.sh --once            # run a single cycle and exit
#   ./start.sh --dry-run --log-level DEBUG
#   ./start.sh --self-test       # verify wiring without secrets/network
set -euo pipefail
cd "$(dirname "$0")"

# Always build first so the image is current.
docker compose build extractor

if [[ $# -gt 0 ]]; then
    # Run one-off with the given flags, remove the container afterwards.
    docker compose run --rm extractor "$@"
else
    # Long-running loop. Ctrl-C tears it down.
    docker compose up
fi