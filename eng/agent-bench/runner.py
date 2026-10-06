#!/usr/bin/env python3
"""Isolated, sequential Copilot smoke trials. This is not a security sandbox."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import itertools
import json
import os
from pathlib import Path
import random
import re
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from urllib.parse import urlsplit, urlunsplit


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
SOURCE = "75f31a8b98bc3903c64f5bdd752927be15e272a2"
HEALTHY = "c902c52de9a11a7139602ec11264ec3e71a92c38"
MODELS = ("gpt-6-luna", "gpt-6.1-sol", "claude-haiku-4.5", "claude-sonnet-5.5")
VARIANTS = ("raw", "typescript", "csharp")
NOOP = "Reply with the word OK only. Do not use tools, edit files, or start any services."
SKILL_PROBE = (
    "Load the project-local aspire skill using the skill tool, then reply with the word OK only. "
    "Do not use any other tools, edit files, start services, or delegate."
)
SKILLS = (
    "aspire", "aspire-init", "aspireify", "aspire-orchestration",
    "aspire-monitoring", "aspire-deployment", "aspire-project-v2-migration",
)
EXCLUDED = (
    "task", "read_agent", "write_agent", "list_agents", "run_dynamic_workflow",
    "dynamic_workflows_manage", "session_store_sql",
)
CORE_TOOLS = {
    "bash", "read_bash", "stop_bash", "list_bash", "apply_patch", "view",
    "web_fetch", "fetch_copilot_cli_documentation", "skill", "sql", "rg", "glob",
    "report_intent", "multi_tool_use.parallel", "create", "edit", "grep",
}
PORTS = (5432, 6379, 5039, 5173)
IGNORED = {".git", ".aspire", "node_modules", "bin", "obj", ".script-state", "dist", "__pycache__"}


class BenchError(RuntimeError):
    def __init__(self, message: str, status: str = "configuration_error"):
        super().__init__(message)
        self.status = status


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def read_json(path: Path):
    return json.loads(path.read_text())


def inside(path: Path, parent: Path) -> bool:
    return path.resolve().is_relative_to(parent.resolve())


def external(path: Path) -> Path:
    path = path.expanduser().resolve()
    if inside(path, REPO):
        raise BenchError("Artifacts, credentials, and graders must be outside the repository")
    return path


def file_manifest(root: Path, ignore: bool = False) -> dict:
    files = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if ignore and any(part in IGNORED for part in rel.parts):
            continue
        if path.is_symlink():
            raise BenchError(f"Symlinks are not allowed in benchmark inputs: {rel}")
        if path.is_file():
            files[rel.as_posix()] = digest(path.read_bytes())
    return files


def manifest_hash(files: dict) -> str:
    return digest(json.dumps(files, sort_keys=True, separators=(",", ":")).encode())


def execute(cmd: list[str], *, cwd: Path, env: dict | None = None,
            timeout: int = 120, log: Path | None = None, check: bool = True):
    """Bounded commands; command output is external and never includes auth setup."""
    process = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        terminate_recorded(owned_processes(process.pid))
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=10)
        if log:
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text((exc.stdout or b"").decode() if isinstance(exc.stdout, bytes)
                           else (exc.stdout or ""))
        raise BenchError(f"Setup/inspection timed out: {cmd[0]}", "infrastructure_error") from exc
    result = subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)
    if log:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(result.stdout + result.stderr)
    if check and result.returncode:
        raise BenchError(f"Command exited {result.returncode}: {cmd[0]}; see external log",
                         "infrastructure_error")
    return result


def archive_snapshot(commit: str, workspace: Path, variant: str) -> dict:
    paths = [".gitignore", "demo/start"]
    if variant != "raw":
        paths.append(f"demo/checkpoints/03-observe/{variant}")
    archive = subprocess.run(["git", "archive", "--format=tar", commit, "--", *paths],
                             cwd=REPO, capture_output=True, check=True).stdout
    workspace.mkdir(parents=True)
    extracted = {}
    with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
        for member in stream:
            rel = Path(member.name)
            if rel.is_absolute() or ".." in rel.parts:
                raise BenchError("Unsafe archive path")
            if not member.isdir() and not member.isfile():
                raise BenchError(f"Non-regular archive entry: {member.name}")
            # The static nginx deployment is not part of either local startup arm.
            if "nginx" in rel.parts or member.name.endswith("Dockerfile.nginx"):
                continue
            if any(part in (".github", ".agents", ".claude", "eng") for part in rel.parts):
                raise BenchError(f"Unexpected agent/evaluator configuration: {member.name}")
            dest = workspace / rel
            if member.isdir():
                dest.mkdir(parents=True, exist_ok=True)
            else:
                data = stream.extractfile(member).read()
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                dest.chmod(member.mode & 0o777)
                extracted[member.name] = digest(data)
    return {"commit": commit, "archive_sha256": digest(archive),
            "source_files": extracted, "source_sha256": manifest_hash(extracted)}


def reserve_ports(seed: int) -> tuple[dict, list[socket.socket]]:
    rng = random.Random(seed)
    mapping, held = {}, []
    try:
        for port in PORTS:
            candidates = list(range(24000, 49000))
            rng.shuffle(candidates)
            for candidate in candidates:
                sock = socket.socket()
                try:
                    sock.bind(("127.0.0.1", candidate))
                    sock.listen()
                except OSError:
                    sock.close()
                    continue
                mapping[port] = candidate
                held.append(sock)
                break
            else:
                raise BenchError("No free trial ports", "infrastructure_error")
    except BaseException:
        for sock in held:
            sock.close()
        raise
    return mapping, held


def transform_fixture(workspace: Path, variant: str, trial_id: str,
                      ports: dict, sdk: str) -> list[dict]:
    changes = []

    def replace(path: Path, transform, reason: str):
        before = path.read_bytes()
        after = transform(before.decode()).encode()
        if before != after:
            path.write_bytes(after)
            changes.append({"path": path.relative_to(workspace).as_posix(), "reason": reason,
                            "before_sha256": digest(before), "after_sha256": digest(after)})

    start = workspace / "demo/start"
    # Redis's command and every application source fault remain byte-for-byte unchanged.
    replace(start / "compose.yaml", lambda text: text.replace("postgres:17", "postgres:18")
            .replace("redis:7", "redis:8")
            .replace("/var/lib/postgresql/data", "/var/lib/postgresql"),
            "Common postgres:18/redis:8; PostgreSQL 18 volume-root layout")
    if variant == "raw":
        targets = [start / "README.md", start / "src/bingo-board/.env.example",
                   start / "src/BingoBoard.Admin/Properties/launchSettings.json",
                   *sorted((start / "scripts").glob("*"))]
        for path in targets:
            replace(path, lambda text: re.sub(
                r"(?<!\d)(5432|6379|5039|5173)(?!\d)",
                lambda match: str(ports[int(match[0])]), text),
                "Deterministic free-port remap (all raw host references)")
        replace(start / "compose.yaml", lambda text: text.replace(
            '"5432:5432"', f'"{ports[5432]}:5432"').replace(
            '"6379:6379"', f'"{ports[6379]}:6379"'),
            "Compose host ports only; container ports unchanged")
    else:
        path = workspace / f"demo/checkpoints/03-observe/{variant}/apphost."
        path = Path(str(path) + ("mts" if variant == "typescript" else "cs"))
        if variant == "typescript":
            substitutions = {
                ".withDataVolume()": f".withDataVolume({{ name: '{trial_id}-postgres-data' }})",
                ".addPostgres('postgres')": ".addPostgres('postgres')\n"
                    f"  .withContainerName('{trial_id}-postgres').withImageTag('18')",
                ".addRedis('cache')": ".addRedis('cache')\n"
                    f"  .withContainerName('{trial_id}-redis').withImageTag('8')",
            }
        else:
            substitutions = {
                ".WithDataVolume()": f'.WithDataVolume("{trial_id}-postgres-data")',
                '.AddPostgres("postgres")': '.AddPostgres("postgres")\n'
                    f'    .WithContainerName("{trial_id}-postgres").WithImageTag("18")',
                '.AddRedis("cache")': '.AddRedis("cache")\n'
                    f'    .WithContainerName("{trial_id}-redis").WithImageTag("8")',
            }
        def substitute(text):
            for old, new in substitutions.items():
                if text.count(old) != 1:
                    raise BenchError(f"Fixture API pattern changed: {old}")
                text = text.replace(old, new)
            return text
        replace(path, substitute, "Unique trial containers/volume; explicit common image tags")
    global_json = workspace / "global.json"
    write_json(global_json, {"sdk": {"version": sdk, "rollForward": "disable",
                                   "allowPrerelease": False}})
    changes.append({"path": "global.json", "reason": "Pin installed stable .NET SDK",
                    "before_sha256": None, "after_sha256": digest(global_json.read_bytes())})
    return changes


def isolated_environment(home: Path, workspace: Path, trial_id: str) -> dict:
    home.mkdir(parents=True, exist_ok=True)
    for name in (".copilot", ".config", ".cache", ".local/share", "tmp"):
        (home / name).mkdir(parents=True, exist_ok=True)
    dotnet = Path(shutil.which("dotnet") or "").resolve()
    if not dotnet.is_file():
        raise BenchError("dotnet executable not found")
    aspire = shutil.which("aspire")
    if not aspire:
        raise BenchError("aspire executable not found")
    native_bin = home / ".aspire/bin"
    native_bin.mkdir(parents=True, exist_ok=True)
    # Native script installs prefer their binary's installation prefix over HOME
    # and ASPIRE_HOME. Copy only executable bytes (no installer sidecar/config).
    shutil.copy2(Path(aspire).resolve(), native_bin / "aspire")
    # Executable lookup is retained, not personal config, auth files or parent identity.
    env = {
        "PATH": str(native_bin) + os.pathsep + os.environ["PATH"],
        "HOME": str(home), "USERPROFILE": str(home), "ASPIRE_HOME": str(home / ".aspire"),
        "COPILOT_HOME": str(home / ".copilot"), "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"), "XDG_DATA_HOME": str(home / ".local/share"),
        "XDG_STATE_HOME": str(home / ".local/state"), "TMPDIR": str(home / "tmp"),
        "DOTNET_ROOT": str(dotnet.parent), "DOTNET_CLI_HOME": str(home),
        "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1", "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
        "DOTNET_GENERATE_ASPNET_CERTIFICATE": "false", "DOTNET_NOLOGO": "1",
        "MSBUILDDISABLENODEREUSE": "1", "DOTNET_CLI_DO_NOT_USE_MSBUILD_SERVER": "1",
        "UseSharedCompilation": "false",
        "NUGET_PACKAGES": str(home / ".nuget/packages"), "NPM_CONFIG_CACHE": str(home / ".npm"),
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "COPILOT_ALLOW_ALL": "true", "COPILOT_AUTO_UPDATE": "false",
        "COPILOT_DISABLE_TERMINAL_TITLE": "1", "NO_COLOR": "1", "CI": "true",
        "CONTAINER_RUNTIME": "docker", "COMPOSE_PROJECT_NAME": trial_id,
        "Parameters__admin-password": "agent-bench-local-only",
        "Authentication__AdminPassword": "agent-bench-local-only",
        "ASPIRE_CLI_TELEMETRY_OPTOUT": "true",
    }
    write_json(home / ".copilot/settings.json", {
        "trustedFolders": [str(workspace)], "disableAllHooks": True,
        "ide": {"autoConnect": False}, "autoUpdate": False,
    })
    return env


def authentication() -> str:
    for name in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(name):
            return os.environ[name]
    # The credential remains in memory; never put stdout in a log or argv.
    result = subprocess.run(["gh", "auth", "token"], stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=15)
    if result.returncode or not result.stdout.strip():
        raise BenchError("No Copilot credential from env or existing gh/keychain",
                         "authentication_error")
    return result.stdout.strip()


def toolchain(workspace: Path, env: dict, expected: dict) -> dict:
    commands = {
        "copilot": ["copilot", "--no-auto-update", "--version"],
        "aspire": ["aspire", "--version"], "node": ["node", "--version"],
        "npm": ["npm", "--version"], "dotnet": ["dotnet", "--version"],
        "docker": ["docker", "version", "--format", "{{.Client.Version}} / {{.Server.Version}}"],
    }
    result = {}
    for name, command in commands.items():
        binary = shutil.which(command[0], path=env["PATH"])
        if not binary:
            raise BenchError(f"Missing required executable: {name}")
        version_text = execute(command, cwd=workspace, env=env).stdout.strip()
        if name == "copilot":
            match = re.search(r"^GitHub Copilot CLI ([^\s]+)\.$", version_text, re.MULTILINE)
            if not match:
                raise BenchError("Unrecognized Copilot version response")
            version = match[1]
        else:
            version = version_text.splitlines()[-1]
        if name in expected and expected[name] != version:
            raise BenchError(f"Version pin mismatch for {name}: {version}")
        resolved = Path(binary).resolve()
        result[name] = {"version": version, "version_text": version_text, "executable": str(resolved),
                        "executable_sha256": digest(resolved.read_bytes())}
    result["python"] = {"version": sys.version, "executable": sys.executable}
    result["platform"] = {"sys_platform": sys.platform, "machine": os.uname().machine}
    result["runtime_selection"] = {
        "selected": "docker", "podman_present": shutil.which("podman") is not None,
        "reason": "Explicit shared-host Docker runtime; no runtime installation or switching",
    }
    return result


def install_runtime_capture(home: Path, workspace: Path, env: dict, output: Path) -> dict:
    shims = home / "tool-shims"
    shims.mkdir()
    runtime = output / "runtime"
    runtime.mkdir()
    for name in ("dotnet", "node"):
        real = Path(shutil.which(name, path=env["PATH"])).resolve()
        env["BENCH_REAL_" + name.upper()] = str(real)
        shim = shims / name
        shutil.copyfile(HERE / "tool-shim.py", shim)
        shim.chmod(0o755)
    env["BENCH_RUNTIME_DIR"] = str(runtime)
    env["PATH"] = str(shims) + os.pathsep + env["PATH"]
    script_logs = runtime / "raw-script-state"
    script_logs.mkdir()
    (workspace / "demo/start/.script-state").symlink_to(script_logs, target_is_directory=True)
    return {"shim_sha256": digest((HERE / "tool-shim.py").read_bytes()),
            "runtime_directory": str(runtime), "script_log_directory": str(script_logs),
            "behavior": "Original executables and exit codes; migration stdout tee and PID registration"}


def registered_processes(output: Path, workspace: Path) -> list[dict]:
    table = process_table()
    result = []
    for path in (output / "runtime/pids").glob("*.json"):
        record = read_json(path)
        current = table.get(record.get("pid"))
        if current and current["started"] == record.get("started") and inside(
                Path(record.get("cwd", "/")), workspace):
            result.append(current)
    return result


def configure_treatment(workspace: Path, home: Path, env: dict, trial: dict,
                        output: Path, current_skills: list) -> dict:
    mode = trial["skills"]
    if mode == "current":
        execute(["aspire", "agent", "init", "--workspace-root", str(workspace),
                 "--skill-locations", "github", "--skills", ",".join(current_skills),
                 "--mcp=false", "--non-interactive", "--nologo"],
                cwd=workspace, env=env, timeout=120, log=output / "skills-install.log")
    elif mode == "external-dir":
        source = Path(trial["skill_dir"]).expanduser().resolve()
        if not source.is_dir() or not list(source.glob("*/SKILL.md")):
            raise BenchError("external-dir must contain named skill directories with SKILL.md")
        file_manifest(source)  # Reject symlinks before copying.
        shutil.copytree(source, workspace / ".github/skills")
    elif mode != "none":
        raise BenchError(f"Unsupported skills treatment: {mode}")
    skill_root = workspace / ".github/skills"
    skills = sorted(path.parent.name for path in skill_root.glob("*/SKILL.md"))
    manifest = file_manifest(skill_root) if skill_root.exists() else {}
    if mode == "current" and set(skills) != set(current_skills):
        raise BenchError(f"Installed skill set differs: {skills}")
    if mode == "none" and skills:
        raise BenchError("No-skill treatment contains skills")
    mcp_path = output / "mcp.json"
    if trial["mcp"]:
        write_json(mcp_path, {"mcpServers": {"aspire": {
            "type": "local", "command": str(Path(shutil.which("aspire", path=env["PATH"])).resolve()),
            "args": ["agent", "mcp", "--non-interactive", "--nologo"],
            "cwd": str(workspace), "tools": ["*"],
        }}})
    # This discovery is read-only and model-free. Output is authoritative when the
    # CLI omits session.skills_loaded for an empty skill set.
    discovery = {}
    for kind in ("skill", "instruction"):
        raw = execute(["copilot", "--no-auto-update", kind, "list", "--json"],
                      cwd=workspace, env=env, log=output / f"{kind}-discovery.json").stdout
        try:
            discovery[kind] = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BenchError(f"Invalid {kind} discovery JSON") from exc
    for entry in discovery["skill"]:
        if entry.get("source") == "builtin" and entry.get("enabled"):
            execute(["copilot", "--no-auto-update", "skill", "disable", entry["name"]],
                    cwd=workspace, env=env)
    discovery["skill"] = json.loads(execute(
        ["copilot", "--no-auto-update", "skill", "list", "--json"],
        cwd=workspace, env=env, log=output / "skill-discovery-effective.json").stdout)
    enabled = [entry for entry in discovery["skill"] if entry.get("enabled")]
    if {entry["name"] for entry in enabled} != set(skills):
        raise BenchError("Enabled skills differ from the explicit treatment")
    if discovery["instruction"]:
        raise BenchError("Unexpected custom instructions discovered")
    return {"skills": skills, "skill_files": manifest, "skills_sha256": manifest_hash(manifest),
            "mcp": trial["mcp"], "mcp_config": str(mcp_path) if trial["mcp"] else None,
            "discovery": discovery}


def init_trial_git(workspace: Path, env: dict) -> str:
    execute(["git", "init", "-q", "--initial-branch=trial"], cwd=workspace, env=env)
    (workspace / ".git/info").mkdir(exist_ok=True)
    with (workspace / ".git/info/exclude").open("a") as exclude:
        exclude.write("\n/demo/start/.script-state\n")
    execute(["git", "add", "."], cwd=workspace, env=env)
    execute(["git", "-c", "user.name=Benchmark fixture",
             "-c", "user.email=fixture@example.invalid", "commit", "-q", "-m", "Broken fixture"],
            cwd=workspace, env=env)
    if execute(["git", "remote"], cwd=workspace, env=env).stdout.strip():
        raise BenchError("Trial repository unexpectedly has a remote")
    if execute(["git", "rev-list", "--count", "HEAD"], cwd=workspace, env=env).stdout.strip() != "1":
        raise BenchError("Trial repository must contain only the broken fixture commit")
    return execute(["git", "rev-parse", "HEAD"], cwd=workspace, env=env).stdout.strip()


def agent_command(trial: dict, output: Path, treatment: dict, prompt: str) -> list[str]:
    command = [
        "copilot", "--yolo", "--no-ask-user", "--no-auto-update", "--disable-builtin-mcps",
        "--no-bash-env", "--no-remote", "--no-remote-export",
        "--model", trial["model"], "--context", "default",
        "--output-format", "json", "--usage-output-file", str(output / "usage.json"),
        "--log-dir", str(output / "logs"), "--secret-env-vars=COPILOT_GITHUB_TOKEN",
        "--excluded-tools=" + ",".join(EXCLUDED),
    ]
    if trial.get("reasoning_effort", "medium") == "medium":
        command.extend(["--reasoning-effort", "medium"])
    # In this CLI version --no-custom-instructions also suppresses project skills.
    # Skill arms instead prove an empty instruction inventory in the isolated HOME.
    if not treatment["skills"]:
        command.append("--no-custom-instructions")
    for denied in (
        "shell(git push)", "shell(gh)", "shell(docker prune)", "shell(docker system prune)",
        "shell(docker volume prune)", "shell(aspire stop --all)", "shell(pkill)",
        "shell(killall)",
    ):
        command.append("--deny-tool=" + denied)
    if treatment["mcp_config"]:
        command.extend(["--additional-mcp-config", "@" + treatment["mcp_config"]])
    command.extend(["-p", prompt])
    return command


def process_table() -> dict:
    rows = subprocess.run(["ps", "-axo", "pid=,ppid=,pgid=,lstart=,command="],
                          capture_output=True, text=True, check=True).stdout
    result = {}
    for row in rows.splitlines():
        parts = row.strip().split(None, 8)
        if len(parts) == 9:
            pid, ppid, pgid = map(int, parts[:3])
            result[pid] = {"pid": pid, "ppid": ppid, "pgid": pgid,
                           "started": " ".join(parts[3:8]), "command": parts[8]}
    return result


def owned_processes(root_pid: int) -> list[dict]:
    table = process_table()
    owned = {root_pid}
    changed = True
    while changed:
        before = len(owned)
        for pid, row in table.items():
            if row["ppid"] in owned or row["pgid"] == root_pid:
                owned.add(pid)
        changed = len(owned) != before
    return [table[pid] for pid in sorted(owned) if pid in table]


def terminate_recorded(records: list[dict]) -> list[dict]:
    failures = []
    # Children first; never match by process name or kill an unrecorded process.
    for record in reversed(records):
        current = process_table().get(record["pid"])
        if not current or current["started"] != record["started"]:
            continue
        try:
            os.kill(record["pid"], signal.SIGTERM)
        except ProcessLookupError:
            continue
        except OSError as exc:
            failures.append({"pid": record["pid"], "error": str(exc)})
    return failures


def invoke_agent(command: list, workspace: Path, env: dict, output: Path,
                 timeout: int) -> dict:
    env = {**env, "COPILOT_GITHUB_TOKEN": authentication()}
    start = time.monotonic()
    with (output / "events.jsonl").open("w") as stdout, (output / "stderr.log").open("w") as stderr:
        process = subprocess.Popen(command, cwd=workspace, env=env, stdin=subprocess.DEVNULL,
                                   stdout=stdout, stderr=stderr, start_new_session=True)
        budget_hit = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            budget_hit = True
            records = owned_processes(process.pid)
            write_json(output / "timeout-processes.json", records)
            terminate_recorded(records)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                # PID reuse is excluded while Popen still owns this live process.
                process.kill()
                process.wait(timeout=10)
        duration = (time.monotonic() - start) * 1000
    records = owned_processes(process.pid)
    write_json(output / "processes.json", records)
    return {"pid": process.pid, "returncode": process.returncode, "budget_hit": budget_hit,
            "wall_ms": duration, "processes": records}


def read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    result = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BenchError(f"Non-JSON event in {path.name}") from exc
        result.append(event)
    return result


def merged_events(output: Path, home: Path) -> list[dict]:
    events = read_events(output / "events.jsonl")
    sessions = sorted((home / ".copilot/session-state").glob("*/events.jsonl"))
    if len(sessions) != 1:
        raise BenchError(f"Expected one isolated session; found {len(sessions)}")
    shutil.copyfile(sessions[0], output / "persisted-events.jsonl")
    known = {event.get("id") for event in events if event.get("id")}
    events += [event for event in read_events(sessions[0]) if event.get("id") not in known]
    return sorted(events, key=lambda event: event.get("timestamp", ""))


def cache_states(events: list[dict]) -> list[dict]:
    # Checkpoints repeat cumulative state. Deduplicate by model call ID, not totals.
    calls = {}
    for event in events:
        if event.get("type") == "session.usage_checkpoint":
            for state in event.get("data", {}).get("promptCacheBreakState", []):
                for model, data in state.get("models", {}).items():
                    key = data.get("model_call_id")
                    if key:
                        calls[key] = {**data, "model": model}
    return list(calls.values())


def check_isolation(events: list[dict], treatment: dict, trial: dict, workspace: Path) -> dict:
    errors = []
    start = next((e["data"] for e in events if e.get("type") == "session.start"), {})
    if start.get("selectedModel") != trial["model"]:
        errors.append("Actual session model differs or is unavailable")
    expected_effort = trial.get("reasoning_effort", "medium")
    if ((expected_effort == "medium" and start.get("reasoningEffort") != "medium") or
            start.get("contextTier") != "default"):
        errors.append("Actual effort/default-context configuration not confirmed")
    if Path(start.get("context", {}).get("cwd", "/")).resolve() != workspace.resolve():
        errors.append("Actual session cwd differs")
    loaded_skills, loaded_servers, tool_updates = [], [], []
    skill_event_seen = False
    invoked_skills = []
    for event in events:
        typ, data = event.get("type", ""), event.get("data", {})
        if typ == "session.skills_loaded":
            skill_event_seen = True
            if "skills" not in data:
                errors.append("Unrecognized skills_loaded payload; cannot prove isolation")
            loaded_skills.extend(data.get("skills", []))
        if typ == "tool.execution_start" and data.get("toolName") == "skill":
            invoked_skills.append(data.get("arguments", {}).get("skill"))
        if typ == "session.mcp_servers_loaded":
            loaded_servers.extend(data.get("servers", []))
        if typ == "session.tools_updated":
            tool_updates.append(data)
        if ("approval" in typ or "permission_request" in typ or
                (typ == "tool.execution_start" and data.get("toolName") == "ask_user")):
            errors.append("Interactive approval/ask_user event")
    names = {item if isinstance(item, str) else item.get("name") for item in loaded_skills}
    expected = set(treatment["skills"])
    if skill_event_seen and names != expected:
        errors.append(f"Loaded skills differ: expected {sorted(expected)}, observed {sorted(names)}")
    # 1.0.92-5 omits skills_loaded entirely (even with a catalog in the prompt).
    # Do not fabricate an event: check the effective discovery inventory and paths,
    # retain its hashes, and separately report observed skill invocations.
    discovered = [item for item in treatment["discovery"].get("skill", []) if item.get("enabled")]
    if {item.get("name") for item in discovered} != expected:
        errors.append("Effective discovered skills differ from treatment")
    for item in discovered:
        if item.get("source") != "project" or not inside(
                Path(item.get("path", "/")), workspace / ".github/skills"):
            errors.append("Effective skill discovery escaped the project treatment")
    if any(name not in expected for name in invoked_skills):
        errors.append("Unselected skill invoked")
    for item in loaded_skills:
        if isinstance(item, dict):
            source = item.get("path") or item.get("location")
            if source and not inside(Path(source), workspace / ".github/skills"):
                errors.append("Skill path escaped the selected project treatment")
    active = {server.get("name") for server in loaded_servers if server.get("status") != "disabled"}
    expected_servers = {"aspire"} if trial["mcp"] else set()
    if active != expected_servers:
        errors.append(f"Active MCP servers differ: {sorted(active)}")
    for server in loaded_servers:
        if server.get("name") not in {"aspire", "github-mcp-server", "githubiq"}:
            errors.append("Unexpected MCP configuration discovered, even if disabled")
        if server.get("name") == "aspire" and server.get("status") not in ("connected", "loaded", "ready"):
            errors.append(f"Aspire MCP not ready: {server.get('status')}")
    calls = cache_states(events)
    tools = {}
    for call in calls:
        if expected_effort == "medium" and call.get("reasoning_effort") != "medium":
            errors.append("A model call did not confirm medium effort")
        if call.get("model") != trial["model"]:
            errors.append("A model call used a different model")
        if call.get("tools_truncated", 0):
            errors.append("Tool inventory truncated; isolation not established")
        for tool in call.get("tools", []):
            tools[tool["name"]] = tool
        for segment in call.get("system_segments", []):
            if segment.get("segment") in ("user_instructions", "memories", "personal_instructions"):
                errors.append("Personal instruction/memory prompt segment")
    if not tools or not tool_updates:
        errors.append("No actual tool inventory/update evidence")
    for name in tools:
        if name not in CORE_TOOLS and not (trial["mcp"] and (
                name.startswith("aspire-") or name.startswith("aspire_") or name == "api_tool")):
            errors.append(f"Unexpected tool loaded: {name}")
    discovery_text = json.dumps(treatment["discovery"])
    # Discovery paths must never point at the original user's home or parent project.
    if str(Path.home()) in discovery_text or str(REPO) in discovery_text:
        errors.append("Personal/parent discovery path leaked")
    return {"valid": not errors, "errors": sorted(set(errors)), "session_start": start,
            "requested_effort_policy": expected_effort,
            "actual_effort": start.get("reasoningEffort"),
            "actual_context": start.get("contextTier"),
            "skills_loaded": loaded_skills if skill_event_seen else None,
            "skills_loaded_event_available": skill_event_seen,
            "skills_available": discovered, "skills_invoked": invoked_skills,
            "mcp_servers_loaded": loaded_servers,
            "tools_updated": tool_updates, "actual_tools": list(tools.values()),
            "empty_skill_event_omitted": not expected and not loaded_skills}


def classify_tool(name: str, arguments: dict) -> str:
    text = json.dumps(arguments).lower()
    if name in ("apply_patch", "edit", "create", "patch_text"):
        return "edit"
    if "sleep " in text or "--retry" in text or "lsof " in text or "netstat " in text:
        return "polling-guessing"
    if any(term in text for term in ("aspire describe", "aspire logs", "aspire otel",
                                    "docker logs", "list_console_logs", "list_traces",
                                    "list_resources", "list_structured_logs")):
        return "evidence"
    if any(term in text for term in ("aspire start", "aspire wait", "aspire stop",
                                    "compose up", "dotnet run", "npm run dev", "scripts/start")):
        return "orchestration"
    if any(term in text for term in ("dotnet test", "npm test", "curl ", "scripts/check")):
        return "verify"
    if any(term in text for term in (".env", "appsettings", "launchsettings", "compose.yaml")):
        return "config-archaeology"
    return "explore"


def timestamp_ms(value: str) -> float:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000


def summarize(events: list[dict], usage: dict) -> dict:
    start = next((e for e in events if e.get("type") == "session.start"), {})
    origin = timestamp_ms(start["timestamp"]) if start.get("timestamp") else None
    tools, started = [], {}
    for event in events:
        typ, data = event.get("type"), event.get("data", {})
        if typ == "tool.execution_start":
            name = data.get("toolName", "")
            item = {"index": len(tools), "name": name, "arguments": data.get("arguments", {}),
                    "timestamp": event.get("timestamp"), "success": None,
                    "tool_call_id": data.get("toolCallId")}
            item["category"] = classify_tool(name, item["arguments"])
            tools.append(item)
            started[item["tool_call_id"]] = item
        elif typ == "tool.execution_complete" and data.get("toolCallId") in started:
            item = started[data["toolCallId"]]
            item["success"] = data.get("success")
            item["completed_at"] = event.get("timestamp")
    def first(category, completed=False):
        for item in tools:
            if item["category"] == category and (not completed or item["success"] is True):
                value = item.get("completed_at") if completed else item["timestamp"]
                if value and origin is not None:
                    return timestamp_ms(value) - origin
        return None
    details = usage.get("tokenDetails", {})
    model_usage = {name: value.get("usage", {}) for name, value in usage.get("modelMetrics", {}).items()}
    inputs = [call["prompt_tokens"] for call in cache_states(events) if "prompt_tokens" in call]
    return {
        "token_buckets": {key: details.get(key, {}).get("tokenCount")
                          for key in ("input", "cache_read", "cache_write", "output")},
        "model_usage": model_usage, "nano_aiu": usage.get("totalNanoAiu"),
        "api_ms": usage.get("totalApiDurationMs"), "actual_model": usage.get("currentModel"),
        "last_call_input_tokens": usage.get("lastCallInputTokens"),
        "peak_call_input_tokens": max(inputs) if inputs and len(inputs) == sum(
            e.get("type") == "model.call_start" for e in events) else None,
        "observed_peak_call_input_tokens": max(inputs) if inputs else None,
        "calls_with_input_tokens": len(inputs),
        "tool_output_tokens": None, "total_tokens": None,
        "token_semantics": "Native buckets/model usage retained; no bucket, cache or reasoning sums",
        "assistant_turns": sum(e.get("type") == "assistant.turn_start" for e in events),
        "model_calls": sum(e.get("type") == "model.call_start" for e in events),
        "tool_calls": len(tools), "tool_sequence": tools,
        "first_runtime_evidence_ms": first("evidence", True),
        "first_edit_ms": first("edit", True), "first_evidence_tokens": None,
    }


def final_answer(events: list[dict]) -> str | None:
    messages = [event["data"].get("content") for event in events
                if event.get("type") == "assistant.message" and
                event.get("data", {}).get("phase") in (None, "final_answer") and
                not event.get("data", {}).get("toolRequests")]
    return messages[-1] if messages else None


def diagnosis(answer: str | None) -> tuple[dict, str | None]:
    if answer is None:
        return {"diagnoses": []}, "No final answer"
    text = answer.strip()
    if text.startswith("```json") and text.endswith("```"):
        text = text[7:-3].strip()
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result
    try:
        parsed = json.loads(text, object_pairs_hook=unique_object)
    except ValueError as exc:
        return {"diagnoses": []}, f"Invalid final JSON: {exc}"
    if not isinstance(parsed, dict) or not isinstance(parsed.get("diagnoses"), list):
        return {"diagnoses": []}, "Missing diagnoses array"
    return parsed, None


def parse_json_output(text: str):
    # Aspire can prefix a JSON response with human-oriented startup lines.
    decoder = json.JSONDecoder()
    for match in re.finditer(r"[\[{]", text):
        try:
            value, end = decoder.raw_decode(text[match.start():])
        except json.JSONDecodeError:
            continue
        if not text[match.start() + end:].strip():
            return value
    raise BenchError("No complete JSON response from Aspire", "infrastructure_error")


def prewarm(workspace: Path, env: dict, trial: dict, output: Path) -> dict:
    started = time.monotonic()
    records = []
    commands = [
        (["docker", "pull", "postgres:18"], workspace),
        (["docker", "pull", "redis:8"], workspace),
        (["dotnet", "build", "AspireifyBingo.slnx", "--nologo", "--disable-build-servers"],
         workspace / "demo/start"),
        (["npm", "ci", "--no-audit", "--no-fund"], workspace / "demo/start/src/bingo-board"),
    ]
    if trial["variant"] != "raw":
        appdir = workspace / f"demo/checkpoints/03-observe/{trial['variant']}"
        if trial["variant"] == "typescript":
            commands.append((["npm", "ci", "--no-audit", "--no-fund"], appdir))
        commands.append((["aspire", "restore", "--apphost", str(apphost(workspace, trial["variant"])),
                          "--non-interactive", "--nologo"], workspace))
        if trial["variant"] == "typescript":
            commands.append((["npm", "run", "aspire:build"], appdir))
        else:
            commands.append((["dotnet", "build", str(apphost(workspace, trial["variant"])),
                              "--nologo", "--disable-build-servers"], appdir))
    for index, (command, cwd) in enumerate(commands):
        begin = time.monotonic()
        execute(command, cwd=cwd, env=env, timeout=600, log=output / f"prewarm-{index}.log")
        records.append({"command": command, "cwd": str(cwd),
                        "wall_ms": (time.monotonic() - begin) * 1000})
    images = {}
    for image in ("postgres:18", "redis:8"):
        images[image] = json.loads(execute(["docker", "image", "inspect", image],
                                         cwd=workspace, env=env).stdout)[0]
    dirty = execute(["git", "status", "--porcelain"], cwd=workspace, env=env).stdout.strip()
    if dirty:
        raise BenchError("Prewarm changed tracked inputs; not silently normalized")
    return {"wall_ms": (time.monotonic() - started) * 1000, "commands": records,
            "images": images, "startup_state": "cold-stopped-prewarmed",
            "differences": "Aspire arms additionally restore/build AppHost dependencies"}


def apphost(workspace: Path, variant: str) -> Path | None:
    if variant == "raw":
        return None
    extension = "mts" if variant == "typescript" else "cs"
    return workspace / f"demo/checkpoints/03-observe/{variant}/apphost.{extension}"


def docker_inspect(identifier: str, workspace: Path, env: dict) -> dict | None:
    result = execute(["docker", "container", "inspect", identifier], cwd=workspace, env=env,
                     check=False)
    return json.loads(result.stdout)[0] if not result.returncode else None


def resources(description) -> list[dict]:
    if isinstance(description, list):
        return [item for item in description if isinstance(item, dict)]
    if isinstance(description, dict):
        for key in ("resources", "Resources"):
            if isinstance(description.get(key), list):
                return description[key]
        if "name" in description or "Name" in description:
            return [description]
    return []


def resource_value(resource: dict, key: str):
    for actual, value in resource.items():
        if actual.lower() == key.lower():
            return value
    props = resource.get("properties", resource.get("Properties", {}))
    if isinstance(props, dict):
        return next((value for name, value in props.items()
                     if name.lower().replace(".", "") == key.lower().replace(".", "")), None)
    if isinstance(props, list):
        return next((prop.get("value", prop.get("Value")) for prop in props
                     if str(prop.get("name", prop.get("Name", ""))).lower().replace(".", "") ==
                     key.lower().replace(".", "")), None)
    return None


def resource_url(resource: dict) -> str | None:
    urls = resource_value(resource, "urls") or resource_value(resource, "endpoints") or []
    candidates = []
    for item in urls:
        if isinstance(item, dict):
            if item.get("isInternal", item.get("IsInternal")):
                continue
            value = item.get("url", item.get("Url"))
            name = item.get("name", item.get("Name", ""))
        else:
            value, name = item, ""
        if isinstance(value, str) and value.startswith("http://"):
            parts = urlsplit(value)
            if parts.hostname not in ("localhost", "127.0.0.1", "::1"):
                raise BenchError("Aspire reported a non-loopback trial endpoint")
            candidates.append((0 if name == "http" else 1,
                               urlunsplit((parts.scheme, parts.netloc, "", "", ""))))
    return min(candidates)[1] if candidates else None


def collect_runtime(workspace: Path, env: dict, trial: dict, output: Path,
                    trial_id: str, ports: dict) -> dict:
    metadata = {"run_id": trial_id, "workspace": str(workspace.resolve()),
                "variant": trial["variant"], "container_runtime": "docker",
                "compose_project": trial_id, "containers": [], "volumes": [],
                "processes": read_json(output / "processes.json") if (output / "processes.json").exists() else []}
    known_pids = {item["pid"] for item in metadata["processes"]}
    metadata["processes"].extend(record for record in registered_processes(output, workspace)
                                 if record["pid"] not in known_pids)
    if trial["variant"] == "raw":
        metadata.update(frontend_url=f"http://localhost:{ports[5173]}",
                        admin_url=f"http://localhost:{ports[5039]}")
        ids = execute(["docker", "ps", "-aq", "--filter",
                       f"label=com.docker.compose.project={trial_id}"],
                      cwd=workspace, env=env).stdout.split()
        for identifier in ids:
            container = docker_inspect(identifier, workspace, env)
            if not container:
                continue
            service = container["Config"]["Labels"].get("com.docker.compose.service")
            metadata["containers"].append(container["Id"])
            if service in ("postgres", "redis"):
                metadata[service] = {"container_id": container["Id"]}
            for mount in container.get("Mounts", []):
                if mount.get("Type") == "volume":
                    metadata["volumes"].append(mount["Name"])
    else:
        host = apphost(workspace, trial["variant"])
        for service in ("postgres", "redis"):
            container = docker_inspect(f"{trial_id}-{service}", workspace, env)
            if container:
                metadata[service] = {"container_id": container["Id"]}
                metadata["containers"].append(container["Id"])
                metadata["volumes"].extend(mount["Name"] for mount in container.get("Mounts", [])
                                           if mount.get("Type") == "volume")
        response = execute(["aspire", "describe", "--apphost", str(host), "--format", "Json",
                            "--non-interactive", "--nologo"],
                           cwd=workspace, env=env, check=False, log=output / "runtime-describe.log")
        # Information messages go to stderr even with exit 0 / --format Json.
        if "No AppHost is currently running" in response.stdout + response.stderr:
            metadata["apphost_state"] = "not_running"
        elif response.returncode == 0:
            model = parse_json_output(response.stdout)
            write_json(output / "runtime-describe.json", model)
            metadata["aspire_description"] = model
            for resource in resources(model):
                name = resource_value(resource, "displayName") or resource_value(resource, "name")
                for logical in ("boardadmin", "bingoboard", "migrations"):
                    if isinstance(name, str) and name.startswith(logical + "-"):
                        name = logical
                        break
                if name in ("boardadmin", "bingoboard"):
                    url = resource_url(resource)
                    if url:
                        metadata["admin_url" if name == "boardadmin" else "frontend_url"] = url
                elif name == "migrations":
                    metadata["migrations"] = {
                        "state": resource_value(resource, "state"),
                        "exit_code": resource_value(resource, "exitCode"),
                    }
            logs = execute(["aspire", "logs", "migrations", "--apphost", str(host),
                            "--non-interactive", "--nologo"],
                           cwd=workspace, env=env, check=False, log=output / "migrations.log")
            if logs.returncode == 0 and "migrations" in metadata:
                metadata["migrations"]["log_path"] = str(output / "migrations.log")
        metadata["apphost"] = str(host)
    migration_records = sorted((output / "runtime/migrations").glob("*.json"))
    if migration_records:
        metadata["migrations"] = read_json(migration_records[-1])
    # Do not infer worker success from database rows or from an absent process.
    metadata.setdefault("migrations", {"state": "unknown", "exit_code": None})
    if "postgres" in metadata:
        metadata["postgres"].update(database="bingo" if trial["variant"] == "raw" else "db",
                                    user="postgres")
    metadata["volumes"] = sorted(set(metadata["volumes"]))
    write_json(output / "runtime-metadata.json", metadata)
    return metadata


def capture_diff(workspace: Path, env: dict, output: Path):
    fixture = (output / "fixture-commit.txt").read_text().strip() if (output / "fixture-commit.txt").exists() else "HEAD"
    patch = execute(["git", "diff", "--binary", fixture], cwd=workspace, env=env).stdout
    (output / "candidate.patch").write_text(patch)
    status = execute(["git", "status", "--porcelain"], cwd=workspace, env=env).stdout
    (output / "candidate-status.txt").write_text(status)
    # Capture untracked candidate files too, without build products or configuration state.
    files = file_manifest(workspace, ignore=True)
    write_json(output / "candidate-files.json", files)
    raw = subprocess.run(["git", "ls-files", "--others", "--exclude-standard", "-z"],
                         cwd=workspace, env=env, capture_output=True, check=True).stdout
    with tarfile.open(output / "candidate-untracked.tar", "w") as tar:
        for item in raw.decode().split("\0"):
            if item:
                path = workspace / item
                if path.is_file() and not path.is_symlink():
                    tar.add(path, arcname=item, recursive=False)


def teardown(workspace: Path, env: dict, trial: dict, output: Path, runtime: dict) -> list:
    errors = []
    host = apphost(workspace, trial["variant"])
    if host:
        # Exact trial AppHost only. --force/--volumes are intentionally not used:
        # enumerate owned data volumes below instead of asking for broad cleanup.
        result = execute(["aspire", "stop", "--apphost", str(host), "--non-interactive", "--nologo"],
                         cwd=workspace, env=env, timeout=60, check=False,
                         log=output / "teardown-aspire.log")
        if result.returncode:
            errors.append("Exact-target Aspire stop failed; see teardown-aspire.log")
    records = runtime.get("processes", [])
    errors.extend(terminate_recorded(records))
    for identifier in runtime.get("containers", []):
        result = execute(["docker", "rm", "-f", identifier], cwd=workspace, env=env, check=False)
        if result.returncode and docker_inspect(identifier, workspace, env):
            errors.append(f"Trial container removal failed: {identifier}")
    allowed_volume = (f"{runtime['run_id']}_bingo-postgres-data" if trial["variant"] == "raw"
                      else f"{runtime['run_id']}-postgres-data")
    for name in runtime.get("volumes", []):
        if name != allowed_volume:
            errors.append(f"Refused to remove unexpected volume: {name}")
            continue
        result = execute(["docker", "volume", "rm", name], cwd=workspace, env=env, check=False)
        if result.returncode:
            errors.append(f"Trial volume removal failed: {name}")
    table = process_table()
    remaining = [record for record in records if record["pid"] in table and
                 table[record["pid"]]["started"] == record["started"]]
    if remaining:
        errors.append("Recorded trial-owned processes remain after teardown")
    write_json(output / "teardown.json", {"errors": errors, "runtime": runtime,
                                         "orphan_processes": remaining})
    return errors


def trial_plan(config: dict) -> list[dict]:
    if config.get("trials"):
        trials = [dict(trial) for trial in config["trials"]]
    else:
        trials = [{"model": model, "variant": variant, "skills": skills, "mcp": mcp}
                  for model, variant, skills, mcp in itertools.product(
                      config.get("models", MODELS), config.get("variants", VARIANTS),
                      config.get("skills", ["none", "current"]), config.get("mcp", [False, True]))]
    for trial in trials:
        if not isinstance(trial.get("model"), str) or not trial["model"]:
            raise BenchError("A literal model ID is required (auto routing is not a benchmark arm)")
        if trial["model"] == "auto" or trial.get("variant") not in VARIANTS:
            raise BenchError("Invalid model/variant")
        if trial.get("skills") not in ("none", "current", "external-dir"):
            raise BenchError("Invalid skills treatment")
        if type(trial.get("mcp")) is not bool:
            raise BenchError("mcp must be true or false")
        if trial["skills"] == "external-dir" and not trial.get("skill_dir"):
            raise BenchError("external-dir needs an explicit skill_dir")
        trial["reasoning_effort"] = config.get("reasoning_effort_by_model", {}).get(
            trial["model"], config.get("reasoning_effort", "medium"))
        if trial["reasoning_effort"] != "medium" and not (
                trial["model"] == "claude-haiku-4.5" and trial["reasoning_effort"] == "native"):
            raise BenchError("Only the explicitly authorized Haiku-native effort exception is supported")
    replicates = config.get("replicates", 1)
    if type(replicates) is not int or replicates < 1:
        raise BenchError("replicates must be a positive integer")
    expanded = [{**trial, "replicate": replicate}
                for replicate in range(1, replicates + 1) for trial in trials]
    random.Random(config.get("seed", 0)).shuffle(expanded)
    return expanded


def run_trial(config: dict, trial: dict, output: Path, *, calibration: bool) -> dict:
    started = time.monotonic()
    output.mkdir(parents=True)
    trial_id = "ab-" + uuid.uuid4().hex[:12]
    # Darwin's default /var/folders path exceeds AF_UNIX's 104-byte path budget.
    root = Path(tempfile.mkdtemp(prefix=trial_id + "-", dir="/tmp")).resolve()
    workspace, home = root / "workspace", root / "home"
    result = {"schema_version": 1, "trial_id": trial_id, "trial": trial,
              "workspace": str(workspace), "scratch_home": str(home), "calibration": calibration,
              "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
              "harness_commit": execute(["git", "rev-parse", "HEAD"], cwd=REPO).stdout.strip(),
              "harness_files": file_manifest(HERE, ignore=True),
              "verifier_pin": config.get("verifier"),
              "status": "configuration_error", "repair_success": None, "diagnosis_success": None,
              "verification_ms": None, "warmup_ms": None, "setup_ms": None,
              "agent_wall_ms": None, "teardown_ms": None}
    ports, reservations = reserve_ports(config.get("seed", 0) + trial.get("replicate", 1))
    env, runtime = None, None
    prompt = ((SKILL_PROBE if config.get("calibration_skill_probe") else NOOP)
              if calibration else (HERE / "task-prompt.txt").read_text())
    result["prompt"] = prompt
    result["prompt_sha256"] = digest(prompt.encode())
    (output / "prompt.txt").write_text(prompt)
    try:
        commit = config.get("source_commit", SOURCE)
        if commit != SOURCE:
            raise BenchError("Smoke source pin must be the full bug commit")
        result["snapshot"] = archive_snapshot(commit, workspace, trial["variant"])
        result["ports"] = ports if trial["variant"] == "raw" else None
        result["transformations"] = transform_fixture(
            workspace, trial["variant"], trial_id, ports, config.get("sdk", "10.0.400"))
        env = isolated_environment(home, workspace, trial_id)
        result["toolchain"] = toolchain(workspace, env, config.get("expected_versions", {}))
        result["treatment"] = configure_treatment(
            workspace, home, env, trial, output, config.get("current_skills", list(SKILLS)))
        result["fixture_commit"] = init_trial_git(workspace, env)
        (output / "fixture-commit.txt").write_text(result["fixture_commit"] + "\n")
        if not calibration:
            result["runtime_capture"] = install_runtime_capture(home, workspace, env, output)
        result["fixture_sha256"] = manifest_hash(file_manifest(workspace, ignore=True))
        if not calibration:
            warmup = prewarm(workspace, env, trial, output)
            result["prewarm"] = warmup
            result["warmup_ms"] = warmup["wall_ms"]
        result["setup_ms"] = (time.monotonic() - started) * 1000
        write_json(output / "configuration.json", result)
        # Identical startup definition in both arms: nothing has been started yet.
        for sock in reservations:
            sock.close()
        command = agent_command(trial, output, result["treatment"], prompt)
        result["agent_argv"] = command
        agent = invoke_agent(command, workspace, env, output, config.get("timeout_seconds", 120))
        result["agent"] = agent
        result["agent_wall_ms"] = agent["wall_ms"]
        if not list((home / ".copilot/session-state").glob("*/events.jsonl")):
            stderr = (output / "stderr.log").read_text()
            if agent["budget_hit"]:
                status = "budget_hit"
            else:
                status = "configuration_error" if "reasoning effort" in stderr or "Model " in stderr else "model_or_authentication_error"
            result["metrics"] = {"model_calls": 0 if "reasoning effort" in stderr else None,
                                 "nano_aiu": None, "usage_available": False}
            raise BenchError(f"CLI failed before session creation: {stderr.strip()}", status)
        events = merged_events(output, home)
        usage = read_json(output / "usage.json") if (output / "usage.json").exists() else {}
        result["metrics"] = summarize(events, usage)
        bundles = list((home / "Library/Caches/copilot/pkg").glob("*/*"))
        result["copilot_runtime_files"] = {
            str(path.relative_to(home)): digest(path.read_bytes())
            for bundle in bundles for path in (bundle / "cli-main.js", bundle / "index.js")
            if path.is_file()
        }
        result["isolation"] = check_isolation(events, result["treatment"], trial, workspace)
        answer = final_answer(events)
        (output / "final-answer.txt").write_text(answer or "")
        _, report_error = diagnosis(answer)
        # Preserve malformed/duplicate-key reports for the grader; do not repair
        # the agent's answer by parsing and serializing it on the way through.
        (output / "diagnosis.json").write_text(answer or "{}")
        result["diagnosis_format_error"] = report_error if not calibration else None
        capture_diff(workspace, env, output)
        if agent["budget_hit"]:
            result["status"] = "budget_hit"
        elif not result["isolation"]["valid"]:
            result["status"] = "configuration_error"
        elif not usage:
            result["status"] = "infrastructure_error"
        elif agent["returncode"]:
            result["status"] = "model_or_authentication_error"
        elif calibration:
            sequence = result["metrics"]["tool_sequence"]
            valid_tools = (len(sequence) == 1 and sequence[0]["name"] == "skill" and
                           sequence[0]["success"] is True) if config.get("calibration_skill_probe") else not sequence
            result["status"] = "calibration_pass" if answer == "OK" and valid_tools else "configuration_error"
        else:
            result["status"] = "candidate"
        if not calibration and result["isolation"]["valid"] and result["status"] in ("candidate", "budget_hit"):
            agent_status = result["status"]
            begin = time.monotonic()
            runtime = collect_runtime(workspace, env, trial, output, trial_id, ports)
            verifier = config["verifier"]
            baseline = output / "baseline"
            result["baseline"] = archive_snapshot(HEALTHY, baseline, trial["variant"])
            transform_fixture(baseline, trial["variant"], trial_id, ports, config.get("sdk", "10.0.400"))
            if not runtime.get("frontend_url") or not runtime.get("admin_url"):
                result["status"] = "repair_fail"
                result["repair_success"] = False
                raise BenchError("No trial endpoints discovered; external runtime grading unavailable",
                                 "repair_fail")
            argv = [sys.executable, str(Path(verifier["path"]).resolve()),
                    "--workspace", str(workspace), "--variant", trial["variant"],
                    "--frontend-url", runtime["frontend_url"],
                    "--admin-url", runtime["admin_url"],
                    "--diagnosis", str(output / "diagnosis.json"), "--output", str(output / "verification.json"),
                    "--runtime-metadata", str(output / "runtime-metadata.json"), "--baseline", str(baseline),
                    "--container-runtime", "docker"]
            if trial["variant"] != "raw":
                argv.extend(["--apphost", str(apphost(workspace, trial["variant"]))])
            checked = execute(argv, cwd=output, timeout=120, check=False, log=output / "verification.log")
            result["verification_ms"] = (time.monotonic() - begin) * 1000
            if checked.returncode not in (0, 1) or not (output / "verification.json").exists():
                result["status"] = "infrastructure_error"
            else:
                grade = read_json(output / "verification.json")
                result["verification"] = grade
                result["repair_success"] = grade.get("repair_success")
                result["diagnosis_success"] = grade.get("diagnosis_success")
                result["status"] = (agent_status if agent_status == "budget_hit" else
                                    "repair_pass" if result["repair_success"] else "repair_fail")
    except BenchError as exc:
        result["status"], result["error"] = exc.status, str(exc)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        result["status"], result["error"] = "infrastructure_error", str(exc)
    finally:
        teardown_started = time.monotonic()
        for sock in reservations:
            sock.close()
        if env and (workspace / ".git").exists():
            # Persist the candidate before touching any trial-owned runtime.
            try:
                capture_diff(workspace, env, output)
            except (BenchError, OSError, subprocess.SubprocessError) as exc:
                result["capture_error"] = str(exc)
                result["status"] = "infrastructure_error"
            if not calibration:
                try:
                    runtime = runtime or collect_runtime(workspace, env, trial, output, trial_id, ports)
                    result["teardown_errors"] = teardown(workspace, env, trial, output, runtime)
                except (BenchError, OSError, ValueError) as exc:
                    result["teardown_errors"] = [str(exc)]
            elif (output / "processes.json").exists():
                result["teardown_errors"] = terminate_recorded(read_json(output / "processes.json"))
        result["total_wall_ms"] = (time.monotonic() - started) * 1000
        result["teardown_ms"] = (time.monotonic() - teardown_started) * 1000
        write_json(output / "result.json", result)
    return result


def load_config(path: Path) -> dict:
    config = read_json(path)
    if config.get("reasoning_effort", "medium") != "medium" or config.get("context", "default") != "default":
        raise BenchError("These smoke trials require exact medium/default")
    if type(config.get("timeout_seconds", 120)) is not int or config.get("timeout_seconds", 120) <= 0:
        raise BenchError("timeout_seconds must be a positive integer")
    return config


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "calibrate", "run"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verifier-commit", help="Export the exact committed grader outside the trial")
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        plan = trial_plan(config)
        if args.command == "plan":
            print(json.dumps(plan, indent=2))
            return 0
        if not args.output:
            raise BenchError("--output must point outside the repository")
        output = external(args.output)
        if output.exists() and any(output.iterdir()):
            raise BenchError("Use a new empty output directory; results are never overwritten")
        if args.command == "run":
            if args.verifier_commit:
                commit = execute(["git", "rev-parse", "--verify", args.verifier_commit + "^{commit}"],
                                 cwd=REPO).stdout.strip()
                code = subprocess.run(["git", "show", commit + ":eng/agent-bench/verify.py"],
                                      cwd=REPO, capture_output=True, check=True).stdout
                grader = output / "grader/verify.py"
                grader.parent.mkdir(parents=True, exist_ok=True)
                grader.write_bytes(code)
                config["verifier"] = {"path": str(grader), "sha256": digest(code), "commit": commit}
            verifier = config.get("verifier")
            if not verifier or not verifier.get("path") or not verifier.get("sha256"):
                raise BenchError("Repair trials are gated: supply a pinned external verifier first")
            path = external(Path(verifier["path"]))
            if digest(path.read_bytes()) != verifier["sha256"]:
                raise BenchError("Verifier content hash mismatch")
            if len(plan) > 4:
                raise BenchError("Paid repair execution is smoke-only (at most four trials)")
        output.mkdir(parents=True, exist_ok=True)
        write_json(output / "config.json", config)
        write_json(output / "plan.json", plan)
        results = []
        for index, trial in enumerate(plan):
            result = run_trial(config, trial, output / f"{index + 1:02d}-{trial['model']}",
                               calibration=args.command == "calibrate")
            results.append(result)
            print(json.dumps({"trial": index + 1, "model": trial["model"], "status": result["status"]}))
            write_json(output / "results.json", results)
            if result.get("teardown_errors"):
                print("Teardown failed; refusing to start another trial", file=sys.stderr)
                break
        return 0 if len(results) == len(plan) and all(
            result["status"] in ("calibration_pass", "repair_pass") for result in results) else 1
    except BenchError as exc:
        print(f"{exc.status}: {exc}", file=sys.stderr)
        return 2
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"infrastructure_error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
