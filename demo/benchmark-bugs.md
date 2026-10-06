# Runtime repair benchmark: evaluator answer key

The `sebros/bench-bugs` branch is based on the working `sebros/bench` branch. It contains four intentional runtime faults across Redis, the migration worker, the admin backend, and the player frontend. These are not build failures.

This document is for evaluators, not repairing agents. For a blind evaluation, exclude this file from the agent's working copy and prompts. Keep `sebros/bench` as the healthy reference, and give each run its own working copy of the broken branch.

## Scope

The raw application in [`start`](start/README.md) and all four Aspire checkpoints share `start/src`. The application-level faults therefore affect both execution paths without maintaining separate broken copies. The Redis fault is repeated in `start/compose.yaml` and in both AppHost languages in every checkpoint.

Startup scripts, dependency versions, credentials, dependency ordering, health endpoints, and telemetry setup are unchanged. No benchmark runner or additional monitoring integration is added.

## Fault inventory

| ID | Category | Service | Seeded fault | Observable failure | Correct repair |
| --- | --- | --- | --- | --- | --- |
| HEALTH-01 | Unhealthy service | Redis (`redis` in raw Compose, `cache` in Aspire) | Startup passes `--maxmemory-policy allkeys-lfr`, which is not a Redis eviction policy. | Redis exits with a configuration error instead of accepting connections. Raw readiness checks fail; Aspire cannot satisfy the admin's cache dependency. | Use the valid `allkeys-lru` policy, or remove the added policy arguments to restore the baseline. Keep raw Compose and the C#/TypeScript AppHosts equivalent. |
| CONFIG-01 | Wrong configuration | Migration worker (`migrations`) | Reads the `database` connection string, while scripts and AppHost references still provide `db`. | The worker exits with `Connection string 'database' is required.` before database migration. Aspire cannot satisfy `WaitForCompletion(migrations)`. | Restore the worker's lookup and error message to `db`. Do not work around the mismatch by duplicating injected connection strings or skipping migrations. |
| INTEROP-01 | Wrong endpoint | Admin backend (`boardadmin`) | Exposes `/api/version` instead of the `/api/version-info` endpoint expected by its consumers. | Requests to `/api/version-info` return 404, directly and through the frontend proxy. Raw API checks fail; the browser reports a runtime-version fetch failure. | Restore the backend route to `/api/version-info`, preserving the existing frontend, startup-check, and proxy contracts. |
| INTEROP-02 | Bad payload | Player frontend (`bingoboard`, or `dev-frontend` in checkpoint 04) | Both board-request methods send one object containing `clientId` and `userName` instead of two positional SignalR arguments. | The WebSocket connection can succeed, but board requests are rejected because the hub expects two arguments. Neither a new player nor a returning player can retrieve a board. | Restore `(persistentClientId, userName)` arguments for both `RequestBingoSet` and `RequestExistingBingoSet`. |

## Changed locations

- **HEALTH-01:** [`start/compose.yaml`](start/compose.yaml); `checkpoints/{01-apphost,02-customize,03-observe,04-compose}/csharp/apphost.cs`; the corresponding `typescript/apphost.mts` files.
- **CONFIG-01:** [`start/src/BingoBoard.MigrationService/Program.cs`](start/src/BingoBoard.MigrationService/Program.cs).
- **INTEROP-01:** [`start/src/BingoBoard.Admin/Program.cs`](start/src/BingoBoard.Admin/Program.cs). The consumers remain in `start/src/bingo-board/App.vue`, `vite.config.js`, the raw startup checks, and checkpoint 04's proxies.
- **INTEROP-02:** [`start/src/bingo-board/services/signalrService.js`](start/src/bingo-board/services/signalrService.js). The unchanged server contracts are `RequestBingoSet(string clientId, string? userName)` and `RequestExistingBingoSet(string clientId, string? userName)` in `BingoBoard.Admin/Hubs/BingoHub.cs`.

## Failure ordering and runtime evidence

All four faults are enabled together. Upstream failures deliberately mask downstream behavior: this is a layered repair scenario, not four independently runnable cases.

The raw startup scripts stop when Redis readiness fails, before invoking the migration worker. In Aspire, Redis and the migration worker can fail independently because migrations depend on PostgreSQL, not Redis. The admin waits for both a healthy cache and successful migrations, and the frontend waits for the admin.

Repair HEALTH-01 and CONFIG-01 before exercising INTEROP-01 and INTEROP-02. A process reaching `Running`, a healthy `/health` response, or a successful SignalR negotiation is not enough to prove the application is repaired.

For an Aspire-assisted run, use the exact AppHost with the orchestration and monitoring skills. Resource state and dependency waits expose the startup blockers; Redis container logs expose the rejected policy; migration console logs expose the configuration mismatch. After startup is repaired, HTTP request traces expose the 404, and the browser console and SignalR completion errors expose the bad invocation payload. Browser console messages are not automatically available through Aspire in this repository; collecting them requires browser tooling or a separately enabled browser-logging integration.

Checkpoint 03 and 04 already enable ServiceDefaults and OpenTelemetry. Earlier checkpoints still expose resource state and console logs. Leave those differences intact when comparing agent performance. For the raw run, use Compose state/logs, `.script-state/` logs when using the helpers, and the browser's network/console information.

## Repair acceptance criteria

1. PostgreSQL is ready and Redis responds to PING; the cache does not continually exit or restart.
2. The migration worker exits successfully after migrations and seeding, using the existing `db` configuration contract.
3. The admin starts, and `/api/version-info` returns JSON successfully both directly and through the chosen frontend.
4. A fresh player and a player with an existing persistent client ID can retrieve a board. Requesting another board also succeeds.
5. The admin sees the player connection, and an admin square update reaches the player over SignalR.
6. The same repairs work in the raw solution and in both Aspire AppHost languages. No dependency waits, readiness checks, error reporting, or required application behavior are disabled to make the run appear successful.

No application is started as part of seeding this branch. Builds and static contract checks can remain green while these runtime faults are present.
