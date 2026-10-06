#!/usr/bin/env python3
"""Isolated, sequential Copilot smoke trials. This is not a security sandbox."""

from __future__ import annotations

import argparse
import ast
import contextlib
import datetime as dt
import hashlib
import io
import itertools
import json
import os
from pathlib import Path
import random
import re
import select
import selectors
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
    occupied = set(listeners())
    mapping, held = {}, []
    try:
        for port in PORTS:
            candidates = list(range(24000, 49000))
            rng.shuffle(candidates)
            for candidate in candidates:
                if candidate in occupied:
                    continue
                sockets = []
                try:
                    for family, address in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
                        sock = socket.socket(family)
                        sockets.append(sock)
                        if family == socket.AF_INET6:
                            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                        sock.bind((address, candidate))
                        sock.listen()
                except OSError:
                    for sock in sockets:
                        sock.close()
                    continue
                mapping[port] = candidate
                held.extend(sockets)
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
    targets = [start / "README.md", start / "src/bingo-board/.env.example",
               start / "src/BingoBoard.Admin/Properties/launchSettings.json",
               *sorted((start / "scripts").glob("*"))]
    for path in targets:
        replace(path, lambda text: re.sub(
            r"(?<!\d)(5432|6379|5039|5173)(?!\d)",
            lambda match: str(ports[int(match[0])]), text),
            "Deterministic free-port remap (all fixture host references, including Aspire proxies)")
    if variant == "raw":
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
                ".addViteApp('bingoboard', '../../../start/src/bingo-board')":
                    ".addViteApp('bingoboard', '../../../start/src/bingo-board')\n"
                    "  .withNpm({ installCommand: 'ci' })",
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
    compose = shutil.which("docker-compose")
    if not compose:
        raise BenchError("Installed docker-compose executable is required for isolated plugin discovery")
    docker_home = home / ".docker"
    plugins = docker_home / "cli-plugins"
    plugins.mkdir(parents=True)
    shutil.copy2(Path(compose).resolve(), plugins / "docker-compose")
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
        "DOCKER_CONFIG": str(docker_home),
        "Parameters__admin-password": "agent-bench-local-only",
        "Authentication__AdminPassword": "agent-bench-local-only",
        "ASPIRE_CLI_TELEMETRY_OPTOUT": "true",
    }
    write_json(home / ".copilot/settings.json", {
        "trustedFolders": [str(workspace)], "disableAllHooks": True,
        "ide": {"autoConnect": False}, "autoUpdate": False,
    })
    certificate = home / ".aspire/https"
    certificate.mkdir(mode=0o700)
    openssl = shutil.which("openssl", path=env["PATH"])
    if not openssl:
        raise BenchError("openssl is required for a trial-local HTTPS certificate")
    execute([openssl, "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-days", "2",
             "-nodes", "-keyout", str(certificate / "localhost.key"),
             "-out", str(certificate / "localhost.crt"), "-subj", "/CN=localhost",
             "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"], cwd=home, env=env)
    execute([openssl, "pkcs12", "-export", "-out", str(certificate / "localhost.pfx"),
             "-inkey", str(certificate / "localhost.key"), "-in", str(certificate / "localhost.crt"),
             "-passout", "pass:"], cwd=home, env=env)
    for filename in ("localhost.key", "localhost.pfx"):
        (certificate / filename).chmod(0o600)
    env["ASPNETCORE_Kestrel__Certificates__Default__Path"] = str(certificate / "localhost.pfx")
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
        "compose": ["docker", "compose", "version"],
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
    result["compose"]["plugin_sha256"] = digest(
        (Path(env["DOCKER_CONFIG"]) / "cli-plugins/docker-compose").read_bytes())
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
        if current and current["started"] == " ".join(record.get("started", "").split()) and inside(
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
    wait_recorded_exits(records, 5)
    table = process_table()
    remaining = [record for record in records if record["pid"] in table and
                 table[record["pid"]]["started"] == record["started"]]
    for record in remaining:
        current = process_table().get(record["pid"])
        if not current or current["started"] != record["started"]:
            continue
        try:
            os.kill(record["pid"], signal.SIGKILL)
        except ProcessLookupError:
            continue
        except OSError as exc:
            failures.append({"pid": record["pid"], "error": str(exc)})
    wait_recorded_exits(remaining, 2)
    return failures


def wait_recorded_exits(records: list[dict], timeout: float) -> None:
    table = process_table()
    pids = {record["pid"] for record in records if record["pid"] in table and
            table[record["pid"]]["started"] == record["started"]}
    if not pids:
        return
    deadline = time.monotonic() + timeout
    if hasattr(select, "kqueue"):
        with contextlib.closing(select.kqueue()) as queue:
            pending = set()
            for pid in pids:
                try:
                    queue.control([select.kevent(pid, filter=select.KQ_FILTER_PROC,
                                                flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                                fflags=select.KQ_NOTE_EXIT)], 0, 0)
                    pending.add(pid)
                except ProcessLookupError:
                    continue
            while pending and time.monotonic() < deadline:
                events = queue.control([], len(pending), max(0, deadline - time.monotonic()))
                if not events:
                    break
                pending.difference_update(event.ident for event in events)
    elif hasattr(os, "pidfd_open"):
        with selectors.DefaultSelector() as selector:
            handles = []
            try:
                for pid in pids:
                    try:
                        handle = os.pidfd_open(pid)
                    except ProcessLookupError:
                        continue
                    handles.append(handle)
                    selector.register(handle, selectors.EVENT_READ)
                while selector.get_map() and time.monotonic() < deadline:
                    ready = selector.select(max(0, deadline - time.monotonic()))
                    if not ready:
                        break
                    for key, _ in ready:
                        selector.unregister(key.fileobj)
            finally:
                for handle in handles:
                    os.close(handle)
    else:
        raise BenchError("Owned-process exit notification requires kqueue or pidfd",
                         "infrastructure_error")


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
            table = process_table()
            previous = read_json(output / "preexisting-runtime.json") if (
                output / "preexisting-runtime.json").exists() else {"processes": {}}
            runtime_roots = {pid for pid, record in table.items()
                             if previous["processes"].get(str(pid)) != record["started"] and
                             (str(workspace) + "/" in record["command"] or
                              str(Path(env["HOME"]) / ".aspire") + "/" in record["command"]) and
                             not re.match(r"(?:/\S*/)?(?:bash|zsh|sh)\s", record["command"])}
            runtime_roots.update(record["pid"] for record in registered_processes(output, workspace)
                                 if "BingoBoard.Admin" in record["command"] or
                                 "node_modules/" in record["command"])
            preserved = {record["pid"]: record for root in runtime_roots
                         for record in owned_processes(root) if record["pid"] != process.pid}
            write_json(output / "budget-preserved-runtime-processes.json", list(preserved.values()))
            terminate_recorded([record for record in records if record["pid"] not in preserved])
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
    sessions = sorted((home / ".copilot/session-state").glob("*/events.jsonl"))
    if len(sessions) != 1:
        raise BenchError(f"Expected one isolated session; found {len(sessions)}")
    shutil.copyfile(sessions[0], output / "persisted-events.jsonl")
    persisted = read_events(sessions[0])
    by_id = {event["id"]: event for event in persisted if event.get("id")}
    tool_starts = [event for event in persisted if event.get("type") == "tool.execution_start"]
    events, recovered, unavailable = [], [], []
    lines = (output / "events.jsonl").read_text().splitlines()
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            # CLI secret redaction can remove JSON string escapes. Never repair
            # its bytes: require the exact event envelope in the persisted stream.
            envelope = re.search(r',"id":"([^"]+)","timestamp":"([^"]+)"'
                                 r'(?:,"parentId":(?:"[^"]*"|null))?}$', line)
            typ = re.match(r'^\{"type":"([^"]+)"', line)
            authoritative = by_id.get(envelope[1]) if envelope else None
            delta = re.match(r'^\{"type":"assistant.tool_call_delta","data":'
                             r'\{"toolCallId":"([^"\\]+)","toolName":"([^"\\]+)",', line)
            complete_calls = [event for event in tool_starts
                              if delta and event.get("data", {}).get("toolCallId") == delta[1] and
                              event.get("data", {}).get("toolName") == delta[2]]
            if (delta and envelope and not authoritative and "******" in line and
                    re.search(r'},"ephemeral":true,"id":"[^"]+","timestamp":', line) and
                    len(complete_calls) == 1 and complete_calls[0].get("timestamp", "") >= envelope[2]):
                unavailable.append({
                    "line": number, "event_id": envelope[1], "type": typ[1],
                    "stdout_line_sha256": digest(line.encode()), "tool_call_id": delta[1],
                    "authoritative_execution_event_id": complete_calls[0]["id"],
                    "reason": "Corrupted ephemeral argument fragment; complete persisted execution arguments retained"})
                continue
            if (typ and typ[1] == "assistant.reasoning" and envelope and
                    not authoritative and "******" in line):
                unavailable.append({"line": number, "event_id": envelope[1], "type": typ[1],
                                    "stdout_line_sha256": digest(line.encode()),
                                    "reason": "Corrupted optional reasoning-text event; native usage retained"})
                continue
            if (not typ or not authoritative or "******" not in line or
                    authoritative.get("type") != typ[1] or
                    authoritative.get("timestamp") != envelope[2]):
                raise BenchError(f"Unrecoverable non-JSON stdout event at line {number}") from exc
            event = authoritative
            recovered.append({"line": number, "event_id": event["id"], "type": event["type"],
                              "parse_error": str(exc), "stdout_line_sha256": digest(line.encode()),
                              "source": "exact-id/type/timestamp persisted event"})
        events.append(event)
    write_json(output / "event-stream-integrity.json", {
        "stdout_sha256": digest((output / "events.jsonl").read_bytes()),
        "persisted_sha256": digest(sessions[0].read_bytes()),
        "stdout_lines": len(lines), "recovered_redacted_events": recovered,
        "unavailable_optional_events": unavailable,
        "raw_streams_preserved": True, "json_bytes_rewritten": False,
    })
    known = {event.get("id") for event in events if event.get("id")}
    events += [event for event in persisted if event.get("id") not in known]
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


def listeners() -> dict[int, set[int]]:
    response = subprocess.run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-F", "pn"],
                              capture_output=True, text=True, timeout=15)
    if response.returncode not in (0, 1) or response.stderr.strip():
        raise BenchError("Cannot attest local TCP listeners with lsof", "infrastructure_error")
    result, pid = {}, None
    for line in response.stdout.splitlines():
        if line.startswith("p"):
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None:
            match = re.search(r":(\d+)$", line)
            if match:
                result.setdefault(int(match[1]), set()).add(pid)
    return result


def attest_ownership(metadata: dict, workspace: Path, env: dict, output: Path) -> dict:
    before = read_json(output / "preexisting-runtime.json")
    table = process_table()
    registered = registered_processes(output, workspace)
    roots = {record["pid"] for record in registered}
    if metadata["variant"] != "raw":
        response = execute(["aspire", "ps", "--format", "Json", "--non-interactive", "--nologo"],
                           cwd=workspace, env=env, check=False, log=output / "ownership-aspire-ps.log")
        if response.returncode:
            raise BenchError("Cannot attest exact AppHost session PID", "infrastructure_error")
        model = parse_json_output(response.stdout)
        write_json(output / "ownership-aspire-ps.json", model)
        # Match the exact path, never another AppHost's model or advertised URL.
        def find(node):
            if isinstance(node, dict):
                values = {key.lower(): value for key, value in node.items()}
                if str(metadata["apphost"]) in [str(value) for value in values.values()]:
                    for key in ("pid", "processid", "apphostpid", "clipid"):
                        if isinstance(values.get(key), int):
                            roots.add(values[key])
                for value in node.values():
                    find(value)
            elif isinstance(node, list):
                for value in node:
                    find(value)
        find(model)
    previous_processes = before["processes"]
    # Native Aspire can reparent the AppHost/DCP away from its registry PIDs.
    # Fresh executables/arguments scoped to this unique workspace/home remain
    # attributable without trusting advertised URLs or touching other listeners.
    roots.update(pid for pid, record in table.items()
                 if previous_processes.get(str(pid)) != record["started"] and
                 any(str(path) + "/" in record["command"]
                     for path in (workspace, Path(env["HOME"]))))
    roots = {pid for pid in roots if pid in table and
             previous_processes.get(str(pid)) != table[pid]["started"]}
    owned = {record["pid"]: record for root in roots for record in owned_processes(root)
             if previous_processes.get(str(record["pid"])) != record["started"]}
    live = listeners()
    errors, endpoints = [], {}
    previous_ports = {int(port) for port in before["listeners"]}
    for service in ("admin", "frontend"):
        url = metadata.get(service + "_url")
        parts = urlsplit(url or "")
        port = parts.port or (80 if parts.scheme == "http" else None)
        pids = live.get(port, set())
        records = []
        if not url or port in previous_ports or not pids:
            errors.append(f"{service}: endpoint missing, pre-existing, or not listening")
        for pid in sorted(pids):
            current = table.get(pid)
            if (not current or pid not in owned or
                    previous_processes.get(str(pid)) == current["started"]):
                errors.append(f"{service}: listener PID {pid} is not a fresh exact-trial process")
            else:
                records.append({"pid": pid, "started": current["started"]})
        endpoints[service] = {"url": url, "port": port, "listener_processes": records,
                              "preexisting": port in previous_ports, "run_id": metadata["run_id"]}
    containers = {}
    for service in ("postgres", "redis"):
        identifier = metadata.get(service, {}).get("container_id")
        container = docker_inspect(identifier, workspace, env) if identifier else None
        if not container or container["Id"] in before["container_ids"]:
            errors.append(f"{service}: container missing or pre-existing")
            continue
        labels = container["Config"].get("Labels") or {}
        name = container["Name"].lstrip("/")
        if metadata["variant"] == "raw":
            valid = labels.get("com.docker.compose.project") == metadata["run_id"]
        else:
            valid = name == f"{metadata['run_id']}-{service}"
            described_ids = {resource_value(item, "container.id")
                             for item in resources(metadata.get("aspire_description", {}))}
            valid = valid and container["Id"] in described_ids
        if not valid:
            errors.append(f"{service}: container not bound to exact trial resources")
        containers[service] = {"container_id": container["Id"], "name": name,
                               "run_id": metadata["run_id"], "preexisting": False,
                               "validated": valid}
    metadata["processes"] = list({record["pid"]: record for record in
                                 metadata.get("processes", []) + list(owned.values())}.values())
    ownership = {"validated": not errors, "run_id": metadata["run_id"], "errors": errors,
                 "endpoints": endpoints, "containers": containers}
    metadata["ownership"] = ownership
    write_json(output / "runtime-metadata.json", metadata)
    if errors:
        raise BenchError("Runtime ownership is unproved; refusing all verifier traffic: " +
                         "; ".join(errors), "infrastructure_error")
    return ownership


def collect_runtime(workspace: Path, env: dict, trial: dict, output: Path,
                    trial_id: str, ports: dict) -> dict:
    metadata = {"run_id": trial_id, "workspace": str(workspace.resolve()),
                "variant": trial["variant"], "container_runtime": "docker",
                "ports": ports,
                "compose_project": trial_id, "containers": [], "volumes": [],
                "processes": read_json(output / "processes.json") if (output / "processes.json").exists() else []}
    known_pids = {item["pid"] for item in metadata["processes"]}
    metadata["processes"].extend(record for record in registered_processes(output, workspace)
                                 if record["pid"] not in known_pids)
    launch = output / "runtime/raw-launch.json"
    if launch.exists():
        record = read_json(launch)
        current = process_table().get(record["pid"])
        if current and current["started"] == record["started"]:
            metadata["processes"] = list({item["pid"]: item for item in
                                         metadata["processes"] + owned_processes(record["pid"])}.values())
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


def cleanup_raw_scope_drift(workspace: Path, env: dict, output: Path, runtime: dict) -> list:
    """Clean only fresh exact-workspace resources; never make them valid grading targets."""
    before_path = output / "preexisting-runtime.json"
    before = read_json(before_path)
    cutoff = before_path.stat().st_mtime * 1000
    directory = str(workspace / "demo/start")
    expected_ports = {int(key): value for key, value in runtime["ports"].items()}
    ids = execute(["docker", "ps", "-aq", "--no-trunc", "--filter",
                   "label=com.docker.compose.project.working_dir=" + directory],
                  cwd=workspace, env=env).stdout.split()
    proof, volumes, networks = [], {}, {}
    for identifier in ids:
        container = docker_inspect(identifier, workspace, env)
        if not container:
            continue
        labels = container["Config"].get("Labels") or {}
        project = labels.get("com.docker.compose.project")
        service = labels.get("com.docker.compose.service")
        if project == runtime["run_id"]:
            continue
        target = 5432 if service == "postgres" else 6379 if service == "redis" else None
        bindings = container.get("HostConfig", {}).get("PortBindings", {}).get(f"{target}/tcp") or []
        if (identifier in before["container_ids"] or not project or target is None or
                labels.get("com.docker.compose.project.working_dir") != directory or
                labels.get("com.docker.compose.project.config_files") != directory + "/compose.yaml" or
                timestamp_ms(container["Created"]) < cutoff or
                not bindings or {int(item["HostPort"]) for item in bindings} != {expected_ports[target]}):
            raise BenchError("Refused scope-drift cleanup without fresh workspace/port ownership proof",
                             "infrastructure_error")
        proof.append({"container_id": identifier, "name": container["Name"], "project": project,
                      "labels": labels, "created": container["Created"], "preexisting": False})
        for mount in container.get("Mounts", []):
            if mount.get("Type") == "volume":
                value = parse_json_output(execute(["docker", "volume", "inspect", mount["Name"]],
                                          cwd=workspace, env=env).stdout)[0]
                if (timestamp_ms(value["CreatedAt"]) < cutoff or
                        (value.get("Labels") or {}).get("com.docker.compose.project") != project):
                    raise BenchError("Refused to remove pre-existing or unbound scope-drift volume",
                                     "infrastructure_error")
                volumes[mount["Name"]] = value
        for value in container.get("NetworkSettings", {}).get("Networks", {}).values():
            identifier = value["NetworkID"]
            if identifier:
                networks[identifier] = parse_json_output(execute(
                    ["docker", "network", "inspect", identifier], cwd=workspace, env=env).stdout)[0]
    selected = {item["container_id"] for item in proof}
    for network in networks.values():
        if (timestamp_ms(network["Created"]) < cutoff or
                not set(network.get("Containers", {})).issubset(selected)):
            raise BenchError("Refused to remove scope-drift network with foreign ownership",
                             "infrastructure_error")
    evidence = {"scope_violation": bool(proof), "grading_ownership_relaxed": False,
                "containers": proof, "volumes": volumes, "networks": networks}
    write_json(output / "scope-drift-cleanup.json", evidence)
    for item in proof:
        execute(["docker", "rm", "-f", item["container_id"]], cwd=workspace, env=env)
    for name in volumes:
        execute(["docker", "volume", "rm", name], cwd=workspace, env=env)
    for identifier in networks:
        execute(["docker", "network", "rm", identifier], cwd=workspace, env=env)
    remaining = execute(["docker", "ps", "-aq", "--no-trunc", "--filter",
                         "label=com.docker.compose.project.working_dir=" + directory],
                        cwd=workspace, env=env).stdout.split()
    evidence["remaining_exact_workspace_containers"] = remaining
    write_json(output / "scope-drift-cleanup.json", evidence)
    return ["Exact-workspace containers remain after scope-drift cleanup"] if remaining else []


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
    if trial["variant"] == "raw" and (output / "preexisting-runtime.json").exists():
        errors.extend(cleanup_raw_scope_drift(workspace, env, output, runtime))
    table = process_table()
    remaining = [record for record in records if record["pid"] in table and
                 table[record["pid"]]["started"] == record["started"]]
    if remaining:
        errors.append("Recorded trial-owned processes remain after teardown")
    write_json(output / "teardown.json", {"errors": errors, "runtime": runtime,
                                         "orphan_processes": remaining})
    return errors


def trial_plan(config: dict) -> list[dict]:
    primary = config.get("design") == "paired-primary"
    continuation = config.get("design") == "primary-continuation"
    if continuation:
        remaining = config.get("trials", [])
        expected = {("claude-sonnet-5.5", "typescript"), ("claude-haiku-4.5", "typescript"),
                    ("claude-haiku-4.5", "raw")}
        if (len(remaining) != 3 or {(item["model"], item["variant"]) for item in remaining} != expected or
                config.get("replicates", 1) != 1 or config.get("timeout_seconds") != 600 or
                any(item["skills"] != ("none" if item["variant"] == "raw" else "current") or
                    item["mcp"] != (item["variant"] != "raw") for item in remaining)):
            raise BenchError("Continuation permits only the three untouched primary slots")
    if primary:
        if config.get("trials") or config.get("replicates", 1) != 1:
            raise BenchError("Primary smoke uses generated matched pairs and exactly one replicate")
        models = config.get("models", list(MODELS))
        if len(models) != len(set(models)) or not models or any(model not in MODELS for model in models):
            raise BenchError("Primary models must be a unique subset of the four authorized IDs")
        generator = random.Random(config.get("seed", 0))
        models = list(models)
        generator.shuffle(models)
        first_arms = ["raw", "typescript"] * ((len(models) + 1) // 2)
        first_arms = first_arms[:len(models)]
        generator.shuffle(first_arms)
        trials = []
        for model, first in zip(models, first_arms):
            for variant in (first, "typescript" if first == "raw" else "raw"):
                trials.append({"model": model, "variant": variant,
                               "skills": "none" if variant == "raw" else "current",
                               "mcp": variant != "raw", "pair_id": model,
                               "arm": "raw-bare" if variant == "raw" else "typescript-all-tools"})
    elif config.get("trials"):
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
    if not primary and not continuation:
        random.Random(config.get("seed", 0)).shuffle(expanded)
    return expanded


def validate_reuse(path: Path, config: dict, plan: list[dict]) -> tuple[tuple, dict]:
    path = external(path)
    result = read_json(path)
    previous = read_json(path.parent.parent / "config.json")
    trial = result["trial"]
    key = (trial["model"], trial["variant"], trial["replicate"])
    match = next((item for item in plan if
                  (item["model"], item["variant"], item["replicate"]) == key), None)
    if not match or any(trial.get(field) != match.get(field) for field in
                        ("skills", "mcp", "reasoning_effort")):
        raise BenchError(f"Reused trial does not match a planned arm: {path}")
    for field, default in (("source_commit", SOURCE), ("sdk", "10.0.400"),
                           ("context", "default"), ("timeout_seconds", 120),
                           ("seed", 0), ("current_skills", list(SKILLS)),
                           ("expected_versions", {})):
        if previous.get(field, default) != config.get(field, default):
            raise BenchError(f"Reused trial differs in {field}: {path}")
    if (result.get("calibration") or result.get("status") not in
            ("repair_pass", "repair_fail", "budget_hit") or
            not result.get("isolation", {}).get("valid") or
            not (path.parent / "usage.json").is_file() or
            not read_json(path.parent / "usage.json") or
            result.get("teardown_errors") or result.get("capture_error")):
        raise BenchError(f"Only fidelity-valid, captured, cleaned repair attempts may be reused: {path}")
    cleanup = read_json(path.parent / "teardown.json")
    if cleanup.get("errors") or cleanup.get("orphan_processes"):
        raise BenchError(f"Reused trial has incomplete cleanup: {path}")
    if result.get("verifier_pin", {}).get("sha256") != config["verifier"]["sha256"]:
        raise BenchError("Reused trial has a different verifier")
    if result.get("snapshot", {}).get("commit") != SOURCE or result.get("prompt_sha256") != digest(
            (HERE / "task-prompt.txt").read_bytes()):
        raise BenchError("Reused trial has a different source or prompt")
    if result.get("prewarm", {}).get("startup_state") != "cold-stopped-prewarmed":
        raise BenchError("Reused trial has a different startup definition")
    old = execute(["git", "show", result["harness_commit"] + ":eng/agent-bench/runner.py"],
                  cwd=REPO).stdout
    if digest(old.encode()) != result["harness_files"]["runner.py"]:
        raise BenchError("Reused harness code cannot be verified from its commit")
    current_functions = {node.name: ast.dump(node, include_attributes=False)
                         for node in ast.parse((HERE / "runner.py").read_text()).body
                         if isinstance(node, ast.FunctionDef)}
    current_constants = {node.targets[0].id: ast.dump(node, include_attributes=False)
                         for node in ast.parse((HERE / "runner.py").read_text()).body
                         if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)}
    parser_changed = False
    non_input_functions = {"main", "trial_plan", "paired_summary", "validate_reuse",
                           "validate_fixture_gates", "merged_events", "replay_candidate",
                           "run_fixture_probe", "start_raw_background"}
    for node in ast.parse(old).body:
        if isinstance(node, ast.FunctionDef) and node.name == "merged_events":
            parser_changed = current_functions.get(node.name) != ast.dump(node, include_attributes=False)
        if isinstance(node, ast.FunctionDef) and node.name not in non_input_functions:
            if current_functions.get(node.name) != ast.dump(node, include_attributes=False):
                raise BenchError(f"Reused execution protocol differs in {node.name}")
        elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            if current_constants.get(node.targets[0].id) != ast.dump(node, include_attributes=False):
                raise BenchError(f"Reused execution protocol differs in {node.targets[0].id}")
    for filename in ("tool-shim.py", "task-prompt.txt"):
        if result["harness_files"].get(filename) != digest((HERE / filename).read_bytes()):
            raise BenchError(f"Reused execution protocol differs in {filename}")
    if parser_changed:
        events = merged_events(path.parent, Path(result["scratch_home"]))
        fidelity = check_isolation(events, result["treatment"], result["trial"], Path(result["workspace"]))
        if not fidelity["valid"] or summarize(events, read_json(path.parent / "usage.json")) != result["metrics"]:
            raise BenchError("Reused parser amendment changed fidelity or measured metrics")
        result = {**result, "parser_reaudit": {
            "valid": True, "current_runner_sha256": digest((HERE / "runner.py").read_bytes()),
            "native_usage_unchanged": True, "agent_input_functions_unchanged": True}}
    return key, {**result, "trial": match, "reused_result_path": str(path)}


def paired_summary(plan: list[dict], results: list[dict]) -> dict:
    pairs = []
    fields = ("status", "repair_success", "diagnosis_success", "setup_ms", "warmup_ms",
              "agent_wall_ms", "verification_ms", "teardown_ms", "trial_id", "workspace")
    for model in dict.fromkeys(trial["model"] for trial in plan):
        arms = {}
        for result in results:
            if result["trial"]["model"] != model:
                continue
            variant = result["trial"]["variant"]
            arms[variant] = {**{field: result.get(field) for field in fields},
                            "metrics": result.get("metrics"),
                            "isolation": result.get("isolation"),
                            "verification": result.get("verification"),
                            "toolchain": result.get("toolchain"),
                            "prewarm": result.get("prewarm"),
                            "transformations": result.get("transformations"),
                            "reused_result_path": result.get("reused_result_path")}
        raw, aspire = arms.get("raw", {}), arms.get("typescript", {})
        deltas = {}
        for field in ("agent_wall_ms", "setup_ms", "warmup_ms", "verification_ms"):
            a, b = raw.get(field), aspire.get(field)
            deltas[field] = b - a if isinstance(a, (int, float)) and isinstance(b, (int, float)) else None
        for field in ("nano_aiu", "model_calls", "assistant_turns"):
            a = (raw.get("metrics") or {}).get(field)
            b = (aspire.get("metrics") or {}).get(field)
            deltas[field] = b - a if isinstance(a, (int, float)) and isinstance(b, (int, float)) else None
        token_deltas = {}
        for field in ("input", "output", "cache_read", "cache_write"):
            a = ((raw.get("metrics") or {}).get("token_buckets") or {}).get(field)
            b = ((aspire.get("metrics") or {}).get("token_buckets") or {}).get(field)
            token_deltas[field] = b - a if isinstance(a, (int, float)) and isinstance(b, (int, float)) else None
        pairs.append({"model": model, "arms": arms, "complete": len(arms) == 2,
                      "typescript_minus_raw": {**deltas, "token_buckets": token_deltas}})
    return {"design": "paired-primary", "replicates": 1, "pairs": pairs,
            "inference": "Descriptive n=1 observations only; no confidence, significance, or general efficiency claim.",
            "timing": "Agent-only deltas exclude setup, dependency prewarm, verification and cleanup.",
            "token_semantics": "Native buckets are compared separately; no assumed total or cache/reasoning inclusion."}


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
              "protocol_sha256": protocol_hash(),
              "harness_commit": execute(["git", "rev-parse", "HEAD"], cwd=REPO).stdout.strip(),
              "harness_files": file_manifest(HERE, ignore=True),
              "verifier_pin": config.get("verifier"),
              "status": "configuration_error", "repair_success": None, "diagnosis_success": None,
              "verification_ms": None, "warmup_ms": None, "setup_ms": None,
              "agent_wall_ms": None, "teardown_ms": None}
    previous_listeners = listeners() if not calibration else {}
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
        result["ports"] = ports
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
            write_json(output / "preexisting-runtime.json", {
                "listeners": {str(port): sorted(pids) for port, pids in previous_listeners.items()},
                "processes": {str(pid): record["started"] for pid, record in process_table().items()},
                "container_ids": execute(["docker", "ps", "-aq", "--no-trunc"],
                                         cwd=workspace, env=env).stdout.split(),
            })
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
        result["diagnosis_format_valid"] = report_error is None if not calibration else None
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
            result["ownership"] = attest_ownership(runtime, workspace, env, output)
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
        scope = output / "scope-drift-cleanup.json"
        if scope.exists() and read_json(scope).get("scope_violation"):
            result.update(agent_scope_violation=True, repair_success=False,
                          runtime_workflows_success=None, status="agent_scope_violation")
        write_json(output / "result.json", result)
    return result


def load_config(path: Path) -> dict:
    config = read_json(path)
    if config.get("reasoning_effort", "medium") != "medium" or config.get("context", "default") != "default":
        raise BenchError("These smoke trials require exact medium/default")
    if type(config.get("timeout_seconds", 120)) is not int or config.get("timeout_seconds", 120) <= 0:
        raise BenchError("timeout_seconds must be a positive integer")
    return config


def protocol_hash() -> str:
    tree = ast.parse((HERE / "runner.py").read_text())
    ignored = {"main", "trial_plan", "paired_summary", "validate_reuse", "validate_fixture_gates"}
    nodes = [ast.dump(node, include_attributes=False) for node in tree.body
             if isinstance(node, (ast.Assign, ast.ClassDef)) or
             isinstance(node, ast.FunctionDef) and node.name not in ignored]
    return digest(json.dumps({"code": nodes, "prompt": digest((HERE / "task-prompt.txt").read_bytes()),
                              "shim": digest((HERE / "tool-shim.py").read_bytes())},
                             sort_keys=True).encode())


def validate_fixture_gates(paths: list[Path], config: dict) -> list[dict]:
    gates = []
    for path in paths:
        path = external(path)
        gate = read_json(path)
        previous = read_json(path.parent.parent / "config.json")
        if (gate.get("status") != "fixture_probe_pass" or type(gate.get("paid_model_calls")) is not int or
                gate["paid_model_calls"] != 0 or
                gate.get("probe") not in ("healthy", "seeded-player-payload") or
                not gate.get("ownership", {}).get("validated") or gate.get("teardown_errors") or
                gate.get("protocol_sha256") != protocol_hash() or
                gate.get("verifier_pin", {}).get("sha256") != config["verifier"]["sha256"]):
            raise BenchError("Fixture gate is missing, invalid, unclean, or from a different protocol")
        for field, default in (("sdk", "10.0.400"), ("seed", 0), ("expected_versions", {})):
            if previous.get(field, default) != config.get(field, default):
                raise BenchError("Fixture gate differs in " + field)
        gates.append({"path": str(path), "probe": gate.get("probe"), "variant": gate.get("probe_variant"),
                      "protocol_sha256": gate["protocol_sha256"]})
    expected = {(variant, probe) for variant in ("raw", "typescript")
                for probe in ("healthy", "seeded-player-payload")}
    if len(gates) != 4 or {(gate["variant"], gate["probe"]) for gate in gates} != expected:
        raise BenchError("Primary pairs require healthy and seeded-negative unpaid gates for BOTH raw and TypeScript")
    return gates


def start_raw_background(workspace: Path, env: dict, output: Path) -> dict:
    process = subprocess.Popen(["bash", "scripts/start.sh"], cwd=workspace / "demo/start",
                               env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, start_new_session=True)
    record = process_table().get(process.pid)
    if not record:
        raise BenchError("Raw startup exited before its PID could be recorded", "infrastructure_error")
    write_json(output / "runtime/raw-launch.json", record)
    deadline = time.monotonic() + 180
    pending = b""
    with (output / "raw-start.log").open("wb") as log, selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while time.monotonic() < deadline:
            if not selector.select(max(0, deadline - time.monotonic())):
                break
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                break
            log.write(chunk)
            log.flush()
            pending += chunk
            lines = pending.split(b"\n")
            pending = lines.pop()
            if any(line.startswith(b"Player: http://localhost:") and b"| Admin:" in line for line in lines):
                return record
    raise BenchError("Raw startup never reached its built-in ready marker; see raw-start.log",
                     "infrastructure_error")


def run_fixture_probe(config: dict, output: Path, *, negative: bool,
                      variant: str = "typescript") -> dict:
    """Unpaid exact-fixture lifecycle gate; never touch an agent candidate."""
    output.mkdir(parents=True)
    trial = {"variant": variant, "skills": "current" if variant != "raw" else "none",
             "mcp": variant != "raw"}
    trial_id = "ab-" + uuid.uuid4().hex[:12]
    root = Path(tempfile.mkdtemp(prefix=trial_id + "-", dir="/tmp")).resolve()
    workspace, home = root / "workspace", root / "home"
    previous_listeners = listeners()
    ports, reservations = reserve_ports(config.get("seed", 0) + 1)
    env, runtime = None, None
    result = {"probe": "seeded-player-payload" if negative else "healthy",
              "trial_id": trial_id, "workspace": str(workspace),
              "paid_model_calls": 0, "status": "infrastructure_error",
              "probe_variant": variant,
              "protocol_sha256": protocol_hash(), "verifier_pin": config["verifier"]}
    try:
        result["snapshot"] = archive_snapshot(HEALTHY, workspace, variant)
        if negative:
            path = "demo/start/src/bingo-board/services/signalrService.js"
            seeded = subprocess.run(["git", "show", SOURCE + ":" + path], cwd=REPO,
                                    capture_output=True, check=True).stdout
            (workspace / path).write_bytes(seeded)
            result["seeded_negative"] = {"path": path, "source_commit": SOURCE,
                                         "sha256": digest(seeded)}
        result["transformations"] = transform_fixture(
            workspace, variant, trial_id, ports, config.get("sdk", "10.0.400"))
        env = isolated_environment(home, workspace, trial_id)
        result["toolchain"] = toolchain(workspace, env, config.get("expected_versions", {}))
        configure_treatment(workspace, home, env, trial, output, config.get("current_skills", list(SKILLS)))
        fixture = init_trial_git(workspace, env)
        (output / "fixture-commit.txt").write_text(fixture)
        install_runtime_capture(home, workspace, env, output)
        write_json(output / "preexisting-runtime.json", {
            "listeners": {str(port): sorted(pids) for port, pids in previous_listeners.items()},
            "processes": {str(pid): record["started"] for pid, record in process_table().items()},
            "container_ids": execute(["docker", "ps", "-aq", "--no-trunc"],
                                     cwd=workspace, env=env).stdout.split(),
        })
        result["prewarm"] = prewarm(workspace, env, trial, output)
        for sock in reservations:
            sock.close()
        host = apphost(workspace, variant)
        if host:
            execute(["aspire", "start", "--isolated", "--non-interactive", "--nologo",
                     "--apphost", str(host)], cwd=workspace, env=env, timeout=180,
                    log=output / "probe-start.log")
            for resource in ("boardadmin", "bingoboard"):
                execute(["aspire", "wait", resource, "--apphost", str(host), "--timeout", "120",
                         "--non-interactive", "--nologo"], cwd=workspace, env=env, timeout=140,
                        log=output / f"probe-wait-{resource}.log")
        else:
            start_raw_background(workspace, env, output)
        runtime = collect_runtime(workspace, env, trial, output, trial_id, ports)
        result["ownership"] = attest_ownership(runtime, workspace, env, output)
        baseline = output / "baseline"
        archive_snapshot(HEALTHY, baseline, variant)
        transform_fixture(baseline, variant, trial_id, ports, config.get("sdk", "10.0.400"))
        write_json(output / "diagnosis.json", {"diagnoses": []})
        argv = [sys.executable, config["verifier"]["path"], "--workspace", str(workspace),
                           "--variant", variant,
                           "--frontend-url", runtime["frontend_url"], "--admin-url", runtime["admin_url"],
                           "--diagnosis", str(output / "diagnosis.json"), "--baseline", str(baseline),
                           "--runtime-metadata", str(output / "runtime-metadata.json"),
                           "--output", str(output / "verification.json"), "--container-runtime", "docker"]
        if host:
            argv.extend(["--apphost", str(host)])
        checked = execute(argv, cwd=output, timeout=120, check=False, log=output / "verification.log")
        result["verification"] = read_json(output / "verification.json")
        grade = result["verification"]
        basic = all(grade["checks"][key]["status"] == "pass" for key in
                    ("postgres", "redis", "migrations", "frontend", "version_direct", "version_proxy"))
        rejected = any(grade["checks"][key]["status"] == "fail" for key in
                       ("signalr_fresh", "signalr_returning", "signalr_new_board"))
        expected = checked.returncode == (1 if negative else 0) and basic and (
            grade["repair_success"] is False and rejected if negative else grade["repair_success"] is True)
        result["status"] = "fixture_probe_pass" if expected else "fixture_probe_fail"
    except (BenchError, OSError, ValueError, subprocess.SubprocessError) as exc:
        result["error"] = str(exc)
    finally:
        for sock in reservations:
            sock.close()
        if env and (workspace / ".git").exists():
            try:
                capture_diff(workspace, env, output)
                runtime = runtime or collect_runtime(workspace, env, trial, output, trial_id, ports)
                result["teardown_errors"] = teardown(workspace, env, trial, output, runtime)
            except (BenchError, OSError, ValueError, subprocess.SubprocessError) as exc:
                result["teardown_errors"] = [str(exc)]
            if result.get("teardown_errors"):
                result["status"] = "infrastructure_error"
        write_json(output / "result.json", result)
    return result


def replay_candidate(config: dict, source_result: Path, output: Path) -> dict:
    started = time.monotonic()
    original = read_json(external(source_result))
    source = Path(original["workspace"])
    expected = read_json(source_result.parent / "candidate-files.json")
    if file_manifest(source, ignore=True) != expected:
        raise BenchError("Original final candidate changed; replay is refused")
    output.mkdir(parents=True)
    root = Path(tempfile.mkdtemp(prefix=original["trial_id"] + "-replay-", dir="/tmp")).resolve()
    workspace, home = root / "workspace", root / "home"
    workspace.mkdir()
    for name, checksum in expected.items():
        path = source / name
        target = workspace / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        if digest(target.read_bytes()) != checksum:
            raise BenchError("Candidate copy hash mismatch: " + name)
    trial = original["trial"]
    trial_id = original["trial_id"]
    ports = {int(port): value for port, value in (original.get("ports") or {}).items()}
    if not ports:
        prepared = source_result.parent / "baseline/demo/start"
        script = (prepared / "scripts/start.sh").read_text()
        profile = read_json(prepared / "src/BingoBoard.Admin/Properties/launchSettings.json")
        admin = urlsplit(profile["profiles"]["http"]["applicationUrl"]).port
        postgres = re.search(r"Host=localhost;Port=(\d+)", script)
        redis = re.search(r'ConnectionStrings__cache="localhost:(\d+)"', script)
        frontend = {int(port) for port in re.findall(r"http://localhost:(\d+)", script)
                    if int(port) != admin}
        if not postgres or not redis or len(frontend) != 1 or not admin:
            raise BenchError("Original prepared port mapping cannot be proven")
        ports = {5432: int(postgres[1]), 6379: int(redis[1]), 5039: admin, 5173: frontend.pop()}
    before_listeners = listeners()
    if set(ports.values()) & set(before_listeners):
        raise BenchError("Original candidate's fixed ports are now occupied; no remapping permitted")
    result = {"status": "infrastructure_error", "post_teardown_replay": True, "paid_model_calls": 0,
              "source_result": str(source_result), "workspace": str(workspace),
              "original_agent_wall_ms": original["agent_wall_ms"],
              "original_usage_path": str(source_result.parent / "usage.json"),
              "candidate_sha256": manifest_hash(expected), "source_edits": False,
              "primary_eligibility": False, "verifier_pin": config["verifier"]}
    env, runtime = None, None
    try:
        env = isolated_environment(home, workspace, trial_id)
        result["toolchain"] = toolchain(workspace, env, config.get("expected_versions", {}))
        for service in ("postgres", "redis"):
            if docker_inspect(f"{trial_id}-{service}", workspace, env):
                raise BenchError("An original trial resource name is occupied; replay refused")
        volume = f"{trial_id}_bingo-postgres-data" if trial["variant"] == "raw" else f"{trial_id}-postgres-data"
        if execute(["docker", "volume", "inspect", volume], cwd=workspace, env=env,
                   check=False).returncode == 0:
            raise BenchError("Original trial volume still exists; replay requires fresh storage")
        (output / "fixture-commit.txt").write_text(init_trial_git(workspace, env))
        install_runtime_capture(home, workspace, env, output)
        write_json(output / "preexisting-runtime.json", {
            "listeners": {str(port): sorted(pids) for port, pids in before_listeners.items()},
            "processes": {str(pid): record["started"] for pid, record in process_table().items()},
            "container_ids": execute(["docker", "ps", "-aq", "--no-trunc"],
                                     cwd=workspace, env=env).stdout.split(),
        })
        result["prewarm"] = prewarm(workspace, env, trial, output)
        host = apphost(workspace, trial["variant"])
        if host:
            execute(["aspire", "start", "--isolated", "--non-interactive", "--nologo",
                     "--apphost", str(host)], cwd=workspace, env=env, timeout=180,
                    log=output / "replay-start.log")
            for resource in ("boardadmin", "bingoboard"):
                execute(["aspire", "wait", resource, "--apphost", str(host), "--timeout", "120",
                         "--non-interactive", "--nologo"], cwd=workspace, env=env, timeout=140,
                        log=output / f"replay-wait-{resource}.log")
        else:
            start_raw_background(workspace, env, output)
        runtime = collect_runtime(workspace, env, trial, output, trial_id, ports)
        result["ownership"] = attest_ownership(runtime, workspace, env, output)
        if file_manifest(workspace, ignore=True) != expected:
            raise BenchError("Candidate source changed during replay startup; grading refused")
        baseline = output / "baseline"
        archive_snapshot(HEALTHY, baseline, trial["variant"])
        transform_fixture(baseline, trial["variant"], trial_id, ports, config.get("sdk", "10.0.400"))
        report = source_result.parent / "diagnosis.json"
        if not report.exists():
            report = source_result.parent / "stream-recovery/diagnosis.json"
        if report.exists():
            shutil.copyfile(report, output / "diagnosis.json")
        else:
            events = merged_events(source_result.parent, Path(original["scratch_home"]))
            (output / "diagnosis.json").write_text(final_answer(events) or "{}")
        argv = [sys.executable, config["verifier"]["path"], "--workspace", str(workspace),
                "--variant", trial["variant"], "--frontend-url", runtime["frontend_url"],
                "--admin-url", runtime["admin_url"], "--diagnosis", str(output / "diagnosis.json"),
                "--baseline", str(baseline), "--runtime-metadata", str(output / "runtime-metadata.json"),
                "--output", str(output / "verification.json"), "--container-runtime", "docker"]
        if host:
            argv.extend(["--apphost", str(host)])
        checked = execute(argv, cwd=output, timeout=120, check=False, log=output / "verification.log")
        result["verification"] = read_json(output / "verification.json")
        result["status"] = "replay_captured" if checked.returncode in (0, 1) else "infrastructure_error"
        result["candidate_unchanged_after_verification"] = file_manifest(workspace, ignore=True) == expected
        if not result["candidate_unchanged_after_verification"]:
            raise BenchError("Candidate source changed during verification")
    except (BenchError, OSError, ValueError, subprocess.SubprocessError) as exc:
        result["status"], result["error"] = "infrastructure_error", str(exc)
    finally:
        if env and (workspace / ".git").exists():
            try:
                capture_diff(workspace, env, output)
                runtime = runtime or collect_runtime(workspace, env, trial, output, trial_id, ports)
                result["teardown_errors"] = teardown(workspace, env, trial, output, runtime)
            except (BenchError, OSError, ValueError, subprocess.SubprocessError) as exc:
                result["teardown_errors"] = [str(exc)]
            if result.get("teardown_errors"):
                result["status"] = "infrastructure_error"
        result["original_candidate_still_unchanged"] = file_manifest(source, ignore=True) == expected
        result["replay_wall_ms"] = (time.monotonic() - started) * 1000
        write_json(output / "result.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "calibrate", "run", "probe", "replay"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verifier-commit", help="Export the exact committed grader outside the trial")
    parser.add_argument("--reuse-result", type=Path, action="append", default=[],
                        help="Reuse a compatible captured repair result without another paid attempt")
    parser.add_argument("--stop-file", type=Path,
                        help="External marker checked only between trials, never interrupting cleanup")
    parser.add_argument("--seeded-negative", action="store_true",
                        help="For unpaid probe only: healthy fixture with exact seeded player-call file")
    parser.add_argument("--fixture-gate", type=Path, action="append", default=[],
                        help="Primary run requires four compatible unpaid raw/TypeScript probe result.json files")
    parser.add_argument("--probe-variant", choices=("raw", "typescript"), default="typescript")
    parser.add_argument("--candidate-result", type=Path, help="Original result.json for unpaid unchanged-source replay")
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
        if args.seeded_negative and args.command != "probe":
            raise BenchError("--seeded-negative is only an unpaid fixture probe")
        if args.command == "replay" and not args.candidate_result:
            raise BenchError("Unpaid replay requires --candidate-result")
        if args.command in ("run", "probe", "replay"):
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
            limit = (3 if config.get("design") == "primary-continuation" else
                     8 if config.get("design") == "paired-primary" else 4)
            if args.command == "run" and len(plan) > limit:
                raise BenchError(f"Paid repair execution is smoke-only (at most {limit} trials)")
        if args.reuse_result and (args.command != "run" or config.get("design") != "paired-primary"):
            raise BenchError("Result reuse is available only for matched primary repair pairs")
        gates = validate_fixture_gates(args.fixture_gate, config) if (
            args.command == "run" and config.get("design") in ("paired-primary", "primary-continuation")) else []
        reused = {}
        for path in args.reuse_result:
            key, result = validate_reuse(path, config, plan)
            if key in reused:
                raise BenchError("Duplicate reused arm")
            reused[key] = result
        if reused:
            reused_models = {key[0] for key in reused}
            models = sorted(dict.fromkeys(trial["model"] for trial in plan),
                            key=lambda model: model not in reused_models)
            ranks = {model: index for index, model in enumerate(models)}
            plan.sort(key=lambda trial: (
                ranks[trial["model"]],
                (trial["model"], trial["variant"], trial["replicate"]) not in reused))
        stop_file = external(args.stop_file) if args.stop_file else None
        output.mkdir(parents=True, exist_ok=True)
        write_json(output / "config.json", config)
        if args.command == "probe":
            result = run_fixture_probe(config, output / "fixture", negative=args.seeded_negative,
                                       variant=args.probe_variant)
            write_json(output / "results.json", [result])
            print(json.dumps({"probe": result["probe"], "status": result["status"],
                              "error": result.get("error")}))
            return 0 if result["status"] == "fixture_probe_pass" else 1
        if args.command == "replay":
            result = replay_candidate(config, args.candidate_result, output / "replay")
            write_json(output / "results.json", [result])
            print(json.dumps({"status": result["status"], "post_teardown_replay": True,
                              "error": result.get("error")}))
            return 0 if result["status"] == "replay_captured" else 1
        write_json(output / "plan.json", plan)
        write_json(output / "arm-manifest.json", {
            "design": config.get("design", "unpaired-smoke"), "plan": plan,
            "source_commit": config.get("source_commit", SOURCE),
            "verifier": config.get("verifier"), "timeout_seconds": config.get("timeout_seconds", 120),
            "context": config.get("context", "default"), "seed": config.get("seed", 0),
            "prompt_sha256": digest((HERE / "task-prompt.txt").read_bytes()),
            "reused": {str(key): result["reused_result_path"] for key, result in reused.items()},
            "unpaid_fixture_gates": gates, "protocol_sha256": protocol_hash(),
            "ordering_adjustment": "Already-executed arms and their matched counterparts come first"
            if reused else None,
        })
        results = []
        for index, trial in enumerate(plan):
            if stop_file and stop_file.exists():
                write_json(output / "batch-stop.json", {"reason": "external-stop-marker",
                                                       "completed": len(results), "next_trial": trial})
                break
            key = (trial["model"], trial["variant"], trial["replicate"])
            if key in reused:
                result = reused[key]
            else:
                result = run_trial(config, trial, output / f"{index + 1:02d}-{trial['model']}",
                                   calibration=args.command == "calibrate")
            results.append(result)
            print(json.dumps({"trial": index + 1, "model": trial["model"], "status": result["status"]}))
            write_json(output / "results.json", results)
            if config.get("design") in ("paired-primary", "primary-continuation"):
                write_json(output / "paired-results.json", paired_summary(plan, results))
            if result.get("teardown_errors"):
                print("Teardown failed; refusing to start another trial", file=sys.stderr)
                break
            if args.command == "run" and result["status"] in (
                    "configuration_error", "infrastructure_error", "model_or_authentication_error",
                    "agent_scope_violation"):
                write_json(output / "batch-stop.json", {"reason": result["status"],
                                                       "completed": len(results), "failed_trial": trial})
                print("Harness/model failure; refusing another paid trial", file=sys.stderr)
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
