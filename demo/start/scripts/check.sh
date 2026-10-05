#!/usr/bin/env bash
set -euo pipefail
# shellcheck source-path=SCRIPTDIR
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

if [[ $# -ne 0 ]]; then
    echo "Usage: bash check.sh (set CONTAINER_RUNTIME=podman or docker)" >&2
    exit 1
fi
require_command curl
require_command node
initialize_runtime
check_application
