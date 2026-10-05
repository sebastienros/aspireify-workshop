#!/usr/bin/env bash
set -euo pipefail
# shellcheck source-path=SCRIPTDIR
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

if [[ $# -ne 0 ]]; then
    echo "Usage: bash start.sh (set CONTAINER_RUNTIME=podman or docker)" >&2
    exit 1
fi
for tool in dotnet node npm curl; do require_command "$tool"; done
initialize_runtime

for port in 5432 6379 5039 5173; do
    if (echo >/dev/tcp/localhost/"$port") 2>/dev/null; then
        echo "Port $port is already in use. Stop the conflicting service before starting." >&2
        exit 1
    fi
done

cd "$START_DIR"
dotnet build AspireifyBingo.slnx --nologo
(cd src/bingo-board && npm ci)

export ConnectionStrings__db="Host=localhost;Port=5432;Database=bingo;Username=postgres;Password=postgres"
export ConnectionStrings__cache="localhost:6379"
export Authentication__AdminPassword="${Authentication__AdminPassword:-admin}"
export Aspire__UseServiceDefaults=false
export ASPNETCORE_ENVIRONMENT=Development
export DOTNET_ENVIRONMENT=Development
export ASPNETCORE_URLS=http://localhost:5039
export BINGO_ADMIN_URL=http://localhost:5039

mkdir -p .script-state
ADMIN_PID=""
FRONTEND_PID=""
# shellcheck disable=SC2329
cleanup() {
    local status=$?
    trap - EXIT INT TERM
    for pid in "$FRONTEND_PID" "$ADMIN_PID"; do
        if [[ -n "$pid" ]]; then
            kill "$pid" 2>/dev/null || true
            wait "$pid" 2>/dev/null || true
        fi
    done
    if ! compose stop; then
        echo "Container cleanup failed. Run scripts/clean.sh." >&2
        status=1
    fi
    exit "$status"
}
trap 'cleanup' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

compose up -d
wait_for PostgreSQL postgres_ready
wait_for Redis redis_ready
(
    cd src/BingoBoard.MigrationService
    dotnet bin/Debug/net10.0/BingoBoard.MigrationService.dll
)
(
    cd src/BingoBoard.Admin
    exec dotnet bin/Debug/net10.0/BingoBoard.Admin.dll
) >.script-state/admin.log 2>&1 &
ADMIN_PID=$!
wait_for "admin backend" version_ready http://localhost:5039
(
    cd src/bingo-board
    exec node node_modules/vite/bin/vite.js --host localhost --port 5173 --strictPort
) >.script-state/frontend.log 2>&1 &
FRONTEND_PID=$!
wait_for "player frontend" http_ready http://localhost:5173/
check_application

echo "Player: http://localhost:5173 | Admin: http://localhost:5039 (user: admin)"
echo "Logs: $START_DIR/.script-state | Press Ctrl+C to stop; database data is preserved."
while kill -0 "$ADMIN_PID" 2>/dev/null && kill -0 "$FRONTEND_PID" 2>/dev/null; do
    sleep 1
done
echo "An application process exited unexpectedly. Inspect .script-state/*.log." >&2
exit 1
