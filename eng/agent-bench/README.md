# Copilot CLI smoke benchmark

This stdlib Python harness measures bounded agent repair attempts. Its primary
smoke uses matched raw-bare versus TypeScript-Aspire-all-tools pairs within each
model, with one attempt per arm. It uses Copilot CLI for every vendor and runs trials
**sequentially** against the same stopped, prewarmed startup definition.

Source is pinned to broken commit `75f31a8b98bc3903c64f5bdd752927be15e272a2`;
the healthy `c902c52de9a11a7139602ec11264ec3e71a92c38` snapshot is available
only to the external verifier. `task-prompt.txt` is the exact repair prompt;
its bytes and SHA-256 are retained with every result. The prompt does not disclose
the fault count or answer key.

## Run

Requirements: Python 3.10+, Bash/POSIX process tools, the existing authenticated
`gh`/keychain or a Copilot token in the invoking environment, Docker with Compose,
installed .NET SDK **10.0.400**, Node **24.4.1**, npm **11.4.2**, Aspire
**13.6.0**, and Copilot CLI **1.0.92-5**. This shared Mac has Docker 28.0.4 and
no Podman; the runner explicitly selects Docker without installing or changing
a runtime. Executable versions and SHA-256 hashes are recorded; the SDK is
selected with a generated trial-local `global.json`, not the default SDK 11 RC.

From the repository worktree:

```bash
# Reproduce four unpaid gates and all eight primary attempts in a fresh directory:
bash eng/agent-bench/run-primary.sh /absolute/external/new-results

python3 -B -m unittest discover -s eng/agent-bench/tests -q

# Model authentication, usage capture, and bare/skills/MCP ablations only:
python3 eng/agent-bench/runner.py calibrate \
  --config eng/agent-bench/configs/calibration.json \
  --output /absolute/external/results/noop

# One explicit skill invocation, with no application startup or edits:
python3 eng/agent-bench/runner.py calibrate \
  --config eng/agent-bench/configs/skill-capability.json \
  --output /absolute/external/results/skill

# Unpaid gates: healthy and original seeded player-call fixtures in BOTH arms.
for variant in raw typescript; do
  python3 eng/agent-bench/runner.py probe --probe-variant "$variant" \
    --config eng/agent-bench/configs/primary-pairs.json \
    --verifier-commit ed64da101b31b89296a44c893ac67d4a0af9dca9 \
    --output "/absolute/external/results/$variant-healthy"
  python3 eng/agent-bench/runner.py probe --probe-variant "$variant" --seeded-negative \
    --config eng/agent-bench/configs/primary-pairs.json \
    --verifier-commit ed64da101b31b89296a44c893ac67d4a0af9dca9 \
    --output "/absolute/external/results/$variant-negative"
done

# Requires a committed verifier and compatible unpaid gates. No PR/matrix opened:
python3 eng/agent-bench/runner.py run \
  --config eng/agent-bench/configs/primary-pairs.json \
  --verifier-commit ed64da101b31b89296a44c893ac67d4a0af9dca9 \
  --fixture-gate /absolute/external/results/raw-healthy/fixture/result.json \
  --fixture-gate /absolute/external/results/raw-negative/fixture/result.json \
  --fixture-gate /absolute/external/results/typescript-healthy/fixture/result.json \
  --fixture-gate /absolute/external/results/typescript-negative/fixture/result.json \
  --output /absolute/external/results/primary-pairs

# Optional: retain a protocol-compatible already-completed arm without paying again:
# append --reuse-result /absolute/external/previous/01-model/result.json
# Optional: append --stop-file /absolute/external/stop-after-current
# Creating that marker finishes the current trial and cleanup, then stops the batch.

# Unpaid replay of an unchanged captured candidate, with fresh owned storage:
python3 eng/agent-bench/runner.py replay \
  --config eng/agent-bench/configs/primary-pairs.json \
  --verifier-commit ed64da101b31b89296a44c893ac67d4a0af9dca9 \
  --candidate-result /absolute/external/previous/01-model/result.json \
  --output /absolute/external/results/replay

# Generate an interleaved full-factorial plan WITHOUT executing it:
python3 eng/agent-bench/runner.py plan \
  --config eng/agent-bench/configs/factorial-plan.json
```

Output directories must be outside the repository and initially empty. Existing
results are never overwritten. Primary repair execution is restricted to eight
trials (four pairs, no repetitions); legacy unpaired smoke is limited to four.
The factorial configuration is planning-only. A spoiled harness trial
must be explicitly rerun into a new directory and labeled as a harness retry;
never discard the original or manually repair its candidate.

The primary authorized benchmark is:

| Model | Fixture | Skills | MCP | Effort / context |
| --- | --- | --- | --- | --- |
| `gpt-6-luna` | raw + TypeScript checkpoint 03 | none / current | off / on | medium / default |
| `gpt-6.1-sol` | raw + TypeScript checkpoint 03 | none / current | off / on | medium / default |
| `claude-haiku-4.5` | raw + TypeScript checkpoint 03 | none / current | off / on | native / default |
| `claude-sonnet-5.5` | raw + TypeScript checkpoint 03 | none / current | off / on | medium / default |

The raw arm has no Aspire skills or MCP. The paired TypeScript arm has the
current seven-skill bundle, exact-workspace Aspire MCP, and the Aspire CLI.
Model, effort, context, source faults, frozen prompt, verifier, startup definition,
image versions and 600-second agent budget are held fixed within each pair.
Seeded adjacent pairs balance raw-first and TypeScript-first ordering. Reusing
an already-run arm puts it and its counterpart first, with that ordering deviation
recorded. C#, skill-size arms and ablations are secondary, not part of this run.
The old confounded `smoke.json` is retained only for historical reproduction.

**Haiku exception:** Copilot 1.0.92-5 rejects `--reasoning-effort medium` for
`claude-haiku-4.5` before creating a session or dispatching a paid model call.
Following the explicit user decision, Haiku omits that flag and records requested
policy `native` and actual effort `null` when unavailable. The other models must
confirm medium in both session and model-call evidence. Keep the rejected
original configuration as configuration-error calibration evidence, not a
repair failure. Paired deltas are descriptive observations at **n=1** per arm:
no confidence intervals, significance, or general efficiency claims.

## Configuration and fixture

JSON configs accept either explicit `trials` or a Cartesian product of `models`,
`variants` (`raw`, `typescript`, `csharp`), `skills` (`none`, `current`), and
boolean `mcp`, plus `replicates`, `seed`, and `timeout_seconds`. Trial order is
seeded and reproducible; identities/paths are fresh UUIDs. A timeout bounds
agent-only time, separately from setup, warmup and external verification.
`reasoning_effort_by_model` carries the explicit Haiku exception.

An explicit trial can instead use `"skills": "external-dir"` and
`"skill_dir": "/absolute/variant-directory"` with named `<skill>/SKILL.md`
subdirectories. All input files are hashed and symlinks rejected. Optional
extra trial labels are preserved. No smaller/bigger variants are synthesized;
those comparisons and the full matrix remain pending explicit definitions and
execution authorization.

`"design": "paired-primary"` generates only the matched raw/TypeScript treatments,
requires one replicate and a unique subset of the four authorized models, and
permits at most eight paid attempts. It stops before further paid work on
configuration, infrastructure, model/authentication, or cleanup failures, but
does not retry genuine unsuccessful repairs or budget hits. Reuse requires matching
configuration, prompt, source, verifier, recorded execution-function code and tool
shim, valid treatment evidence, native usage, and completed cleanup. Scheduler-only
changes do not invalidate otherwise identical prior evidence.

Primary paid execution is refused unless all four unpaid gate artifacts have the
current execution-protocol hash, verifier, SDK/version/seed pins, proven ownership
and clean teardown. A failed healthy fixture is never charged as an agent repair
failure. Protocol changes invalidate prior gates and prior-arm reuse; original
costs/findings remain separate historical evidence. The initial Sol/TypeScript
result is infrastructure-invalid, not an agent failure: scratch HTTPS setup,
endpoint attribution and authenticated data-store probing were then incomplete.
The later Luna pair is also retained as pilot evidence, not reused: raw lacked
isolated Compose plugin discovery, while the subsequent common SDK fix changes
tool exposure in both arms. Original agent timings and usage are never replaced
by unpaid replay timings or mixed into the corrected primary comparison.

`git archive` exports only `demo/start`, the root ignore rules, and exactly one
selected `demo/checkpoints/03-observe/<language>` for Aspire arms. The raw arm has
no AppHost. Static nginx deployment files, other checkpoints, evaluator files,
benchmark code, the bug report and repository history are excluded. Layout is
preserved so existing `../../../start` references still work. The trial is
initialized with one commit containing only its **broken** prepared snapshot and
no remote.

Recorded fixture transformations are deliberately not repairs:

- Pin the installed .NET SDK.
- Normalize all arms to PostgreSQL 18 and Redis 8. The raw PostgreSQL mount is
  changed to `/var/lib/postgresql`, the PostgreSQL 18 layout. Aspire documentation
  confirms `WithDataVolume` is version-aware at invocation time; image tags are
  selected before it. Fresh trial volumes never reuse PostgreSQL 17 data.
- Reserve deterministic currently-free host ports and rewrite **every**
  coupled script, README, environment template, launch profile and Compose host
  mapping. Launch-profile remapping also applies to Aspire proxy endpoints:
  `--isolated` alone did not randomize the advertised fixed 5039 proxy.
  Both IPv4 and IPv6 loopback sockets are reserved, excluding all previously
  observed listeners. Container target ports stay 5432/6379. Reservations are released just
  before agent work; normal startup guards catch a subsequent race.
- Give Aspire containers and the data volume unique trial-owned names.
- Select `npm ci` for the TypeScript frontend installer so locked package
  metadata is not rewritten by the integration's default `npm install`.
- Use trial-local transparent `dotnet`/`node` executable shims to register PIDs.
  Migration executions tee their real output and record their real exit code
  externally. Raw `.script-state` points to external application logs. No
  application's seeded source fault is changed by preparation.

The same deterministic transformations are applied to the healthy grader
snapshot. NuGet/npm are restored and source projects built; Aspire arms also
restore their AppHost dependencies. Images are pulled and their IDs/digests
recorded. Setup fails if warmup alters tracked inputs. No application services
are started before measured agent work. SDK tool executables remain available
through an allowlisted PATH/DOTNET_ROOT, but caches and configuration are fresh.
Shared image tags/caches remain a shared-daemon limitation, not a VM guarantee.

## Treatment isolation and safety

Every trial has fresh temporary **HOME**, **COPILOT_HOME**, XDG directories,
NuGet/npm caches and an environment allowlist. It does not inherit parent
session identity, OTLP exporters, personal instructions, model/provider
overrides, plugin configuration, BASH_ENV or credential files. The token is
obtained in memory via the existing environment or `gh auth token`, passed only
to Copilot, and stripped from shell/MCP environments with `--secret-env-vars`.
It is never placed in argv, source, config or results.

Temporary roots deliberately use short `/tmp` paths: macOS's default
`/var/folders` location can exceed its 104-byte Unix-domain socket limit. Native
Aspire script installations derive configuration/cache/log roots from the
installed binary prefix before consulting HOME. The runner therefore copies
**only the installed executable bytes**, without its installer sidecar or any
personal configuration, into scratch `ASPIRE_HOME/bin` and uses that executable
for both CLI and MCP. SDK first-use certificate generation is disabled; no trust
or global certificate configuration is performed by the harness. OpenSSL creates
an untrusted, short-lived localhost certificate only under each scratch home,
with private key/PFX permissions 0600. The same certificate environment is prepared
for both arms; no personal keychain certificate or credential file is copied.
Docker Compose discovery also depends on HOME: only the installed Compose
executable bytes are copied to scratch `DOCKER_CONFIG/cli-plugins`, with version
and hash recorded. No personal Docker configuration or credentials are copied.
The isolated `docker compose version` command must pass before any paid dispatch.

Current skills are installed with `aspire agent init --skill-locations github`
and an explicit seven-skill bundle, `--mcp=false`. No personal directories are
copied. CLI builtin skills are disabled in the **scratch** config so a no-skill
arm is genuinely empty. Enabled discovery entries must exactly match the selected
project skills and paths. Custom instruction discovery must be empty. Builtin
MCPs are disabled; an MCP arm configures only Aspire stdio in the exact trial
cwd. Tools/schema hashes, MCP connection statuses, discovery and invocation
events are saved and checked for leakage.

In 1.0.92-5 `session.skills_loaded` is absent, even when selected project skills
are available. The harness records this as **unavailable/null**, never invents
an event, uses exact effective skill discovery/path/content hashes, and records
observed `skill` invocations separately. The skill capability probe demonstrates
that the selected `aspire` skill can actually be loaded. If a future CLI emits
the event, its payload is also required to match. `session.tools_updated` carries
only a model ID; actual tool schemas come from usage checkpoints. Persisted
`session.start` (not emitted in stdout) is merged with stdout events to prove
the actual model, effort, context and cwd. Unknown/truncated tools or unexpected
skills/MCPs/approval events invalidate the treatment.

Agent stdin is DEVNULL. Invocations use YOLO, no ask_user, no auto-update, JSON
output and external usage/log paths, with remote export/control and delegation
disabled. No-skill arms use `--no-custom-instructions`; skill arms use proven
empty instruction discovery instead (that CLI flag also suppresses skills).
Process-name killing, broad container pruning, all-AppHost stopping and remote
publication commands are denied. The fixed prompt limits all activity to trial
resources.

**This is NOT a security sandbox.** YOLO and the shared Docker daemon mean a
malicious/misbehaving agent could still reach outside its trial using arbitrary
shell code. Denials and instructions are defense in depth, not adversarial
containment. Do not run untrusted tasks here; use dedicated VMs/daemons for a
larger benchmark. The harness never changes global settings, installs a runtime,
stops personal services or discovers containers broadly for cleanup.

Candidate diffs (against the original fixture commit, even if an agent creates
commits), untracked files, final report, raw usage and events are captured before
teardown. Only recorded PIDs, the exact selected AppHost, enumerated containers
bound to the trial and its exact named data volume may be removed. Cleanup
failures/orphans stop the batch. No prune, killall, pkill, `aspire stop --all`,
session-root deletion or broad recursive filesystem removal is used. Temporary
workspaces and scratch homes remain available for inspection.

Before any verifier HTTP, SignalR or container-exec traffic, the runner proves
endpoint ownership with actual TCP listener PIDs/start times, exact registered
processes or fresh unique-workspace/native-Aspire process ancestry, and the
selected AppHost registry. It rejects every pre-existing listener port/process.
Containers must be new exact Compose-project resources or match the selected
Aspire description's IDs and trial-specific names. Pre-trial listener/PID/container
inventories are read-only attribution evidence, never broad cleanup targets.
Missing or conflicting proof is infrastructure-unknown and no probe is sent;
this gate never grades or mutates the user's unrelated workshop at 5039.
At an agent budget hit, model/tool workers stop but attributed application
processes are retained for grading. Final teardown waits on kernel process-exit
notifications (kqueue/pidfd), then escalates only still-live recorded same-start
PIDs; it never kills by process name. Start-time whitespace is normalized in both
PID capture and comparison, including single-digit calendar days. Unpaid raw
startup preserves the foreground script's behavior and waits for its built-in
ready marker with an event selector; it does not mistake a long-lived script for
a setup timeout.

## Results and grading

Each trial saves `result.json`, `configuration.json`, exact `prompt.txt`,
`usage.json`, JSONL stdout/persisted events, stderr/logs, `candidate.patch`,
candidate/untracked manifests, final answer, `diagnosis.json`, trusted external
runtime metadata, verification output, and cleanup evidence. Batch `config.json`,
`plan.json` and `results.json` retain the schedule and pinned external grader.
`arm-manifest.json` pins exact arms, budget, prompt, ordering and reused artifacts.
Primary `paired-results.json` preserves per-arm grading, full tool/model metrics,
runtime/prewarm versions and transformations, plus TypeScript-minus-raw timing,
nanoAIU, call/turn and separate native-token-bucket deltas. Incomplete pairs retain
null deltas rather than fabricating results.
Actual CLI runtime entry-point hashes supplement executable/version hashes.

Raw event streams are immutable. CLI credential redaction can corrupt JSON
escaping: malformed core records are recovered only from a valid isolated
persisted event with the exact same ID, type and timestamp, and only when the
damaged record contains the redaction marker. `event-stream-integrity.json`
records hashes, affected lines and authoritative counterparts. Corrupted optional
reasoning text without a persisted counterpart is explicitly unavailable; native
usage remains authoritative. Unrecoverable core events invalidate the treatment.

Usage JSON is authoritative. Native input/cache-read/cache-write/output buckets,
per-model input/output/cache/reasoning values and nanoAIU/API time are retained
without summing duplicate `agentMetrics` or assuming cache/reasoning inclusion.
Unavailable metrics are null; no synthetic "total tokens" is calculated.
Observed per-call context maxima are distinct from a true peak: the true peak
is null unless every model call has input-token evidence. Turns, model calls,
ordered classified tools, and successful first runtime-evidence/edit latencies
are recorded where observable. Classifications are conservative heuristics;
there is no tokenizer estimate for tool-output tokens.

The external grader receives exact trial container IDs, worker state/exit
evidence and selected endpoints. Missing evidence is unknown, not success.
`repair_success` and `diagnosis_success` are independent: a malformed final JSON
does not turn a working runtime into a failed repair. Reports require one final
candidate **per root cause**, not one candidate across the whole task. Agent
claims are never used as runtime evidence. Budget hits, configuration/arm
failures, authentication/model errors and infrastructure errors are separate
from unsuccessful repairs. Failures are not silently normalized or repaired.
Oracle version 4 additionally reports `runtime_workflows_success` and
`contract_preservation_success` independently; overall repair requires both.
It permits equivalent provider-backed version handlers/aliases without requiring
one textual repair, but preserves strict readiness/lifecycle script contracts.

Unpaid replays copy only the captured candidate manifest and verify every byte
before startup and after verification. They refuse occupied original ports,
containers or volumes, never remap or edit source, use fresh storage, and retain
the original timing/usage references. They report `paid_model_calls: 0` and
`primary_eligibility: false`; post-teardown evidence is not the original timed run.
