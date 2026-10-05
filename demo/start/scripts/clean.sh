#!/usr/bin/env bash
set -euo pipefail
# shellcheck source-path=SCRIPTDIR
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

if [[ $# -gt 1 || ( $# -eq 1 && "$1" != "--reset" ) ]]; then
    echo "Usage: bash clean.sh [--reset] (set CONTAINER_RUNTIME=podman or docker)" >&2
    exit 1
fi
initialize_runtime
if [[ "${1:-}" == "--reset" ]]; then
    compose down --volumes
else
    compose stop
fi
