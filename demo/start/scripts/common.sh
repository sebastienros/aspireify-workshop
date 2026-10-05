#!/usr/bin/env bash

START_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME="${CONTAINER_RUNTIME:-auto}"

require_command() {
    command -v "$1" >/dev/null 2>&1 || {
        echo "Required command not found: $1" >&2
        return 1
    }
}

initialize_runtime() {
    if [[ "$RUNTIME" == "auto" ]]; then
        if command -v podman >/dev/null 2>&1; then
            RUNTIME=podman
        elif command -v docker >/dev/null 2>&1; then
            RUNTIME=docker
        else
            echo "Neither Podman nor Docker is installed. Install one with Compose support." >&2
            return 1
        fi
    fi
    case "$RUNTIME" in
        podman|docker) ;;
        *) echo "CONTAINER_RUNTIME must be auto, podman, or docker." >&2; return 1 ;;
    esac
    require_command "$RUNTIME"
    echo "Using container runtime: $RUNTIME"
    "$RUNTIME" info >/dev/null
    "$RUNTIME" compose version >/dev/null
}

compose() {
    "$RUNTIME" compose --project-directory "$START_DIR" -f "$START_DIR/compose.yaml" "$@"
}

postgres_ready() {
    compose exec -T postgres pg_isready -U postgres -d bingo >/dev/null 2>&1
}

redis_ready() {
    local reply
    reply="$(compose exec -T redis redis-cli ping)" && [[ "$reply" == "PONG" ]]
}

database_seeded() {
    local result
    result="$(compose exec -T postgres psql -U postgres -d bingo -At -v ON_ERROR_STOP=1 -c \
        'SELECT EXISTS (SELECT 1 FROM "AspNetUsers" WHERE "UserName" = '\''admin'\'') AND EXISTS (SELECT 1 FROM "BingoSquares");')" &&
        [[ "$result" == "t" ]]
}

http_ready() {
    curl --fail --silent --show-error --max-time 5 "$1" >/dev/null
}

version_ready() {
    local body
    body="$(curl --fail --silent --show-error --max-time 5 "$1/api/version-info")" || return 1
    printf '%s' "$body" | node --input-type=module -e '
            let body = "";
            for await (const chunk of process.stdin) body += chunk;
            const data = JSON.parse(body);
            if (!data.dotNetVersion || data.aspireVersion !== "not configured") {
                console.error("Invalid non-Aspire version response.");
                process.exit(1);
            }
        '
}

signalr_ready() {
    local body
    body="$(curl --fail --silent --show-error --max-time 5 -X POST "$1/bingohub/negotiate?negotiateVersion=1")" || return 1
    printf '%s' "$body" | node --input-type=module -e '
            let body = "";
            for await (const chunk of process.stdin) body += chunk;
            const data = JSON.parse(body);
            if (!data.connectionToken || !data.availableTransports?.length) {
                console.error("Invalid SignalR negotiation response.");
                process.exit(1);
            }
        '
}

wait_for() {
    local label="$1"
    shift
    local attempt
    for ((attempt = 0; attempt < 60; attempt++)); do
        if "$@" >/dev/null 2>&1; then
            return 0
        fi
        if [[ -n "${ADMIN_PID:-}" ]] && ! kill -0 "$ADMIN_PID" 2>/dev/null; then
            echo "Admin backend exited. See $START_DIR/.script-state/admin.log." >&2
            return 1
        fi
        if [[ -n "${FRONTEND_PID:-}" ]] && ! kill -0 "$FRONTEND_PID" 2>/dev/null; then
            echo "Player frontend exited. See $START_DIR/.script-state/frontend.log." >&2
            return 1
        fi
        sleep 1
    done
    echo "Timed out waiting for $label." >&2
    "$@"
    return 1
}

check_application() {
    local failed=0
    check() {
        local label="$1"
        shift
        if "$@"; then
            echo "OK: $label"
        else
            echo "FAIL: $label" >&2
            failed=1
        fi
    }
    check "PostgreSQL is accepting connections" postgres_ready
    check "Redis responds to PING" redis_ready
    check "Migrations and seed data are present" database_seeded
    check "Admin portal" http_ready http://localhost:5039/login
    check "Admin API (without Aspire)" version_ready http://localhost:5039
    check "Player frontend" http_ready http://localhost:5173/
    check "Frontend API proxy" version_ready http://localhost:5173
    check "Frontend SignalR proxy" signalr_ready http://localhost:5173
    return "$failed"
}
