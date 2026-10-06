#!/usr/bin/env python3
"""External, deterministic verifier for the bingo runtime-repair benchmark.

Run this file outside the measured workspace. It never starts/stops applications,
installs packages, discovers containers, or writes into that workspace.

Diagnosis input:
    {"diagnoses": [{"service": "migrations", "candidates": [
        {"cause": "...", "file": "demo/start/src/.../Program.cs",
         "evidence": "..."}
    ]}]}

Trusted harness runtime metadata (not an agent answer):
    {"run_id": "trial-1", "workspace": "/absolute/trial", "variant": "raw",
     "frontend_url": "http://localhost:5173", "admin_url": "http://localhost:5000",
     "container_runtime": "podman",
     "redis": {"container_id": "<exact container ID>"},
     "postgres": {"container_id": "<exact container ID>",
                  "database": "bingo", "user": "postgres"},
     "migrations": {"state": "Exited", "exit_code": 0,
                    "log_path": "/external/trial/migrations.log"}}

Runtime traffic also requires trusted ownership evidence:
    {"ownership": {"validated": true, "run_id": "trial-1", "errors": [],
      "endpoints": {
        "admin": {"url": "http://localhost:5000", "port": 5000,
                  "listener_processes": [{"pid": 123, "started": "start-time"}]},
        "frontend": {"url": "http://localhost:5173", "port": 5173,
                     "listener_processes": [{"pid": 124, "started": "start-time"}]}},
      "containers": {"redis": {"container_id": "<exact ID>"},
                     "postgres": {"container_id": "<exact ID>"}}}}
The harness must prove listener PID/start-time ownership and container ownership
for this run, rejecting pre-existing listeners. The verifier requires complete
matching records, not merely a boolean; absent/invalid ownership means unknown
infrastructure and NO runtime traffic (including read-only HTTP/container probes).

Migration logs are optional; inline "logs" is also supported. A completed state
AND an integer exit code are required. Container IDs must be 12-64 hex digits.
Authenticated probes read POSTGRES_PASSWORD/REDIS_PASSWORD only inside those
containers, never from evaluator metadata or host-side command arguments.
Additional metadata fields are ignored, never copied into the result. Endpoints,
if supplied, must match CLI endpoints; absent metadata is unknown, not success.
--baseline is a trusted healthy snapshot with the same selected layout, outside
the candidate workspace. Without it, contract preservation remains unknown.

Results contain independent repair_success and diagnosis_success booleans,
per-check pass/fail/unknown states, and separate outcomes for each seeded fault.
Exit 0 means repair success (irrespective of diagnosis), 1 failed/unproved repair,
and 2 a CLI/output error. All deadlines are bounded; --timeout is per operation.
"""

import argparse
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request


FAULTS = ("HEALTH-01", "CONFIG-01", "INTEROP-01", "INTEROP-02")
SEED_IDS = (
    "free", "council-of-aspirations", "screen-share-fail", "pine-mentioned",
    "multiple-options", "app-bug", "scared", "damian-fowler-bicker",
    "friday-behavior", "ignore-docs", "damian-tbc", "different-opinions",
    "error-celly", "av-issue", "new-bug", "old-bug", "maddy-swears",
    "bathroom-break", "this-wont-work", "did-that-work", "aspire-pun",
    "fowler-pause", "restart-something", "do-it-live", "refactoring",
    "port-problems", "fowler-llm", "vibe-coding", "bad-ai", "live-share",
    "frustration", "coffee-mention", "github-issues", "demo-gods",
    "fowler-monorepo", "private-key-shared", "one-line-add", "one-day-work",
    "maddy-snack", "amazing",
)
VERSION_FIELDS = (
    "commitSha", "commitHash", "commitUrl", "dotNetVersion", "aspireVersion",
    "viteVersion",
)
PLAYER_CHECKS = (
    "signalr_fresh", "signalr_returning", "signalr_new_board", "admin_player",
    "live_update",
)
FAULT_CHECKS = {
    "HEALTH-01": ("redis", "contracts"),
    "CONFIG-01": ("postgres", "migrations", "contracts"),
    "INTEROP-01": ("version_direct", "version_proxy", "contracts"),
    "INTEROP-02": ("frontend", *PLAYER_CHECKS, "contracts"),
}
SOURCE_LAYOUTS = ("demo/start/src", "start/src", "src")
MAX_BYTES = 2 * 1024 * 1024


def outcome(status, detail, **observations):
    return {"status": status, "detail": detail, **observations}


def aggregate(checks):
    states = [check["status"] for check in checks]
    if "fail" in states:
        return "fail"
    return "pass" if states and all(s == "pass" for s in states) else "unknown"


def inside(path, directory):
    return path.resolve().is_relative_to(directory.resolve())


def external_path(path, workspace):
    resolved = Path(path).resolve()
    if inside(resolved, workspace):
        raise ValueError("Evaluator input/output must be outside the candidate workspace")
    return resolved


def read_json(path):
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("JSON input exceeds the size limit")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON field: " + key)
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("Invalid JSON constant: " + value)

    with path.open(encoding="utf-8") as stream:
        return json.load(stream, object_pairs_hook=unique_object, parse_constant=invalid_constant)


def find_source(workspace):
    matches = [workspace / layout for layout in SOURCE_LAYOUTS
               if (workspace / layout / "bingo-board").is_dir()]
    if len(matches) != 1:
        raise ValueError("Expected exactly one shared source layout: demo/start/src, start/src, or src")
    return matches[0]


def normalized_url(value):
    if not isinstance(value, str):
        raise ValueError("Endpoint URL must be a string")
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("Expected an absolute HTTP(S) endpoint without credentials, query, or fragment")
    try:
        parsed.port
    except ValueError as error:
        raise ValueError("Endpoint has an invalid port") from error
    return value.rstrip("/")


def load_metadata(path, workspace, variant, frontend_url, admin_url, apphost=None):
    if path is None:
        return {}, outcome("unknown", "No exact-run runtime metadata supplied")
    try:
        metadata = read_json(external_path(path, workspace))
        if not isinstance(metadata, dict):
            raise ValueError("Runtime metadata must be an object")
        if not isinstance(metadata.get("run_id"), str) or not metadata["run_id"].strip():
            raise ValueError("Runtime metadata must identify run_id")
        if not isinstance(metadata.get("workspace"), str):
            raise ValueError("Runtime metadata must identify workspace")
        if Path(metadata["workspace"]).resolve() != workspace.resolve():
            raise ValueError("Runtime metadata belongs to a different workspace")
        if metadata.get("variant") != variant:
            raise ValueError("Runtime metadata belongs to a different variant")
        if metadata.get("apphost") is not None:
            if not isinstance(metadata["apphost"], str) or apphost is None:
                raise ValueError("Runtime metadata AppHost does not match the selected variant")
            observed_apphost = Path(metadata["apphost"])
            if not observed_apphost.is_absolute():
                observed_apphost = workspace / observed_apphost
            if observed_apphost.resolve() != apphost.resolve():
                raise ValueError("Runtime metadata belongs to a different AppHost")
        endpoints = metadata.get("endpoints", {})
        if not isinstance(endpoints, dict):
            raise ValueError("endpoints must be an object")
        for name, expected in (("frontend", frontend_url), ("admin", admin_url)):
            for observed in (metadata.get(name + "_url"), endpoints.get(name)):
                if observed is not None and normalized_url(observed) != expected:
                    raise ValueError("Runtime metadata endpoint does not match the selected run")
        if metadata.get("container_runtime", "podman") not in ("podman", "docker"):
            raise ValueError("Unsupported container runtime")
        return metadata, outcome("pass", "External metadata is bound to the selected workspace and variant")
    except (OSError, ValueError, TypeError) as error:
        return {}, outcome("fail", str(error))


def check_ownership(metadata, frontend_url, admin_url):
    try:
        run_id = metadata.get("run_id")
        evidence = metadata.get("ownership")
        if (not isinstance(run_id, str) or not run_id.strip() or not isinstance(evidence, dict)
                or evidence.get("validated") is not True or evidence.get("run_id") != run_id
                or evidence.get("errors") != []):
            raise ValueError("Missing successful exact-run ownership validation")
        endpoints = evidence.get("endpoints")
        containers = evidence.get("containers")
        if not isinstance(endpoints, dict) or not isinstance(containers, dict):
            raise ValueError("Ownership must identify exact endpoints and containers")
        for name, url in (("frontend", frontend_url), ("admin", admin_url)):
            item = endpoints.get(name)
            if not isinstance(item, dict) or normalized_url(item.get("url")) != url:
                raise ValueError("Endpoint ownership URL does not match the selected run")
            parsed = urllib.parse.urlsplit(url)
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            if type(item.get("port")) is not int or item["port"] != port:
                raise ValueError("Endpoint ownership port does not match the selected run")
            listeners = item.get("listener_processes")
            if not isinstance(listeners, list) or not listeners:
                raise ValueError("Endpoint ownership has no listener PID/start-time evidence")
            for listener in listeners:
                if (not isinstance(listener, dict) or type(listener.get("pid")) is not int
                        or listener["pid"] <= 0 or not isinstance(listener.get("started"), str)
                        or not listener["started"].strip()):
                    raise ValueError("Endpoint ownership listener evidence is incomplete")
        for service in ("redis", "postgres"):
            spec = container_spec(metadata, service)
            item = containers.get(service)
            if not isinstance(item, dict) or item.get("container_id") != spec["container_id"]:
                raise ValueError("Container ownership ID does not match the selected run")
        return outcome("pass", "Trusted exact-run endpoint listeners and container ownership are bound")
    except (ValueError, TypeError, ProbeError) as error:
        return outcome("unknown", "Runtime ownership is not established: " + str(error))


SERVICE_ALIASES = {
    "HEALTH-01": {"redis", "cache"},
    "CONFIG-01": {"migrations", "migration", "migrationworker",
                  "bingoboardmigrationservice"},
    "INTEROP-01": {"boardadmin", "admin", "backend", "bingoboardadmin"},
    "INTEROP-02": {"bingoboard", "frontend", "player", "devfrontend",
                   "playerfrontend", "bingoboardfrontend"},
}


def canonical_service(service):
    return re.sub(r"[^a-z0-9]", "", service.lower())


def service_faults(service):
    canonical = canonical_service(service)
    exact = {fault for fault, aliases in SERVICE_ALIASES.items() if canonical in aliases}
    if exact:
        return exact
    # Harnesses may prefix resource names to isolate trials.
    words = set(re.findall(r"[a-z0-9]+", service.lower()))
    return {fault for fault, aliases in SERVICE_ALIASES.items() if words & aliases}


def diagnosis_path_matches(fault, filename, workspace, source, apphost, variant):
    if not isinstance(filename, str) or not filename.strip():
        return False
    # A line suffix is evidence, not part of the filesystem path.
    filename = re.sub(r":\d+(?:-\d+)?$", "", filename.replace("\\", "/"))
    path = Path(filename)
    resolved = path.resolve() if path.is_absolute() else (workspace / path).resolve()
    if not inside(resolved, workspace):
        return False
    targets = {
        "HEALTH-01": source.parent / "compose.yaml" if variant == "raw" else apphost,
        "CONFIG-01": source / "BingoBoard.MigrationService/Program.cs",
        "INTEROP-01": source / "BingoBoard.Admin/Program.cs",
        "INTEROP-02": source / "bingo-board/services/signalrService.js",
    }
    target = targets[fault]
    return target is not None and resolved == target.resolve()


def semantic_diagnosis(fault, cause, evidence):
    cause, evidence = cause.lower(), evidence.lower()
    text = cause + "\n" + evidence
    if re.search(r"\b(not the cause|not a fault|works correctly|not invalid)\b", cause):
        return False
    if fault == "HEALTH-01":
        return (bool(re.search(r"(allkeys-lfr|maxmemory.policy|eviction policy)", cause))
                and bool(re.search(r"invalid|unsupported|typo|incorrect|wrong|reject|not valid", cause))
                and bool(re.search(r"allkeys-lfr|invalid.*policy|fatal.*config|bad directive", evidence)))
    if fault == "CONFIG-01":
        return (bool(re.search(r"\bdatabase\b", cause)) and bool(re.search(r"\bdb\b", text))
                and bool(re.search(r"key|connection.string|configuration|lookup|injected", cause))
                and bool(re.search(r"mismatch|wrong|instead|expects|reads|required", cause))
                and bool(re.search(r"connection string.*database.*required|getconnectionstring.*database", evidence)))
    if fault == "INTEROP-01":
        return (bool(re.search(r"/api/version(?!-info)", text)) and "/api/version-info" in cause
                and bool(re.search(r"route|endpoint|path", cause))
                and bool(re.search(r"wrong|renamed|instead|mismatch|missing|expos|404", cause))
                and bool(re.search(r"404|mapget.*?/api/version(?!-info)", evidence)))
    return (bool(re.search(r"request(?:existing)?bingoset|board.request", cause))
            and bool(re.search(r"object|payload", cause))
            and bool(re.search(r"positional|two.*(?:argument|string)|2.*argument", text))
            and bool(re.search(r"argument|invoke|hub|signalr", evidence))
            and bool(re.search(r"object|expects.*two|expects.*2|argument.*(?:count|match)|invocation.*(?:fail|error)", evidence))
            and bool(re.search(r"requestexistingbingoset", text)))


def score_diagnosis(path, workspace, source, apphost, variant):
    results = {fault: outcome("fail", "No evidenced singleton diagnosis", candidate_count=0)
               for fault in FAULTS}
    try:
        document = read_json(external_path(path, workspace))
        if not isinstance(document, dict) or not isinstance(document.get("diagnoses"), list):
            raise ValueError("Expected an object containing a diagnoses list")
        groups = {fault: [] for fault in FAULTS}
        for row in document["diagnoses"]:
            if (not isinstance(row, dict) or not isinstance(row.get("service"), str)
                    or not isinstance(row.get("candidates"), list) or not row["candidates"]):
                raise ValueError("Every diagnosis requires service and a nonempty candidates list")
            for candidate in row["candidates"]:
                if (not isinstance(candidate, dict) or any(
                        not isinstance(candidate.get(field), str) or not candidate[field].strip()
                        for field in ("cause", "file", "evidence"))):
                    raise ValueError("Every candidate requires nonempty cause, file, and evidence strings")
            services = service_faults(row["service"])
            for fault in FAULTS:
                paths_match = any(diagnosis_path_matches(
                    fault, c["file"], workspace, source, apphost, variant)
                    for c in row["candidates"])
                if fault in services or paths_match:
                    groups[fault].extend((c, fault in services) for c in row["candidates"])
        for fault, candidates in groups.items():
            count = len(candidates)
            if count != 1:
                results[fault] = outcome(
                    "fail", "Exactly one candidate is required for this root cause",
                    candidate_count=count)
                continue
            candidate, service_matches = candidates[0]
            correct = (service_matches
                       and diagnosis_path_matches(fault, candidate["file"], workspace, source,
                                             apphost, variant)
                       and semantic_diagnosis(fault, candidate["cause"], candidate["evidence"]))
            results[fault] = outcome(
                "pass" if correct else "fail",
                "Correct singleton with supporting evidence" if correct else
                "Candidate does not match the selected root cause, path, and evidence",
                candidate_count=count)
        return {"success": all(r["status"] == "pass" for r in results.values()),
                "status": "pass" if all(r["status"] == "pass" for r in results.values()) else "fail",
                "faults": results}
    except (OSError, ValueError, TypeError) as error:
        return {"success": False, "status": "fail", "detail": str(error), "faults": results}


# Preserve literal contents while ignoring formatting and comments.
TOKENS = re.compile(
    r"""//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'"""
    r"""|`(?:\\.|[^`\\])*`|[A-Za-z_$][\w$]*|\d+(?:\.\d+)?|[^\s]"""
)


def code_tokens(text):
    return [token for token in TOKENS.findall(text)
            if not token.startswith("//") and not token.startswith("/*")]


def mask_player_requests(tokens):
    result = list(tokens)
    for name in ("requestBingoSet", "requestExistingBingoSet"):
        starts = [i for i, token in enumerate(result)
                  if token == name and i > 0 and result[i - 1] == "async"]
        if len(starts) != 1:
            raise ValueError("Expected the original player board-request methods")
        start = starts[0]
        opening = result.index("{", start)
        depth = 1
        end = opening + 1
        while end < len(result) and depth:
            depth += (result[end] == "{") - (result[end] == "}")
            end += 1
        if depth:
            raise ValueError("Unbalanced player method")
        result[opening + 1:end - 1] = ["<board-request-body>"]
    return result


def apphost_tokens(text):
    tokens = code_tokens(text)
    result = []
    index = 0
    while index < len(tokens):
        if (tokens[index] == "." and index + 3 < len(tokens)
                and tokens[index + 1].lower() == "withargs" and tokens[index + 2] == "("):
            end, depth = index + 3, 1
            while end < len(tokens) and depth:
                depth += (tokens[end] == "(") - (tokens[end] == ")")
                end += 1
            args = tokens[index + 3:end - 1]
            literals = [a[1:-1] for a in args if a.startswith(("'", '"'))]
            if depth == 0 and len(literals) == 2 and literals[0] == "--maxmemory-policy":
                index = end
                continue
        result.append(tokens[index])
        index += 1
    return result


def compose_contract(text):
    return "\n".join(line.rstrip() for line in text.splitlines()
                     if not re.fullmatch(
                         r'\s+command:\s*\[\s*["\']redis-server["\']\s*,\s*'
                         r'["\']--maxmemory-policy["\']\s*,\s*["\'][^"\']+["\']\s*\]\s*',
                         line))


def check_contracts(workspace, source, variant, apphost, baseline):
    if baseline is None:
        return outcome("unknown", "No external healthy baseline; removal of waits/contracts cannot be excluded")
    try:
        baseline = external_path(baseline, workspace)
        if inside(workspace, baseline):
            raise ValueError("Baseline must be separate from, not contain, the candidate workspace")
        baseline_source = find_source(baseline)
        problems = []
        extensions = {".cs", ".csproj", ".razor", ".js", ".vue", ".json", ".props", ".targets"}
        for original in sorted(baseline_source.rglob("*")):
            relative = original.relative_to(baseline_source)
            if (not original.is_file() or original.suffix not in extensions
                    or any(part in {"bin", "obj", "node_modules", ".git"} for part in relative.parts)):
                continue
            candidate = source / relative
            if not candidate.is_file() or not inside(candidate, workspace):
                problems.append(str(relative) + " missing/outside workspace")
                continue
            before, after = original.read_text(encoding="utf-8-sig"), candidate.read_text(encoding="utf-8-sig")
            if str(relative) == "bingo-board/services/signalrService.js":
                equal = mask_player_requests(code_tokens(before)) == mask_player_requests(code_tokens(after))
            elif original.suffix in {".json", ".csproj", ".props", ".targets"}:
                equal = before.strip() == after.strip()
            else:
                equal = code_tokens(before) == code_tokens(after)
            if not equal:
                problems.append(str(relative) + " changed outside permitted repair bodies")
        baseline_start = baseline_source.parent
        candidate_start = source.parent
        for name in ("Directory.Packages.props", "NuGet.config", "AspireifyBingo.slnx"):
            original, candidate = baseline_start / name, candidate_start / name
            if original.is_file() and (
                    not candidate.is_file() or not inside(candidate, workspace)
                    or original.read_bytes() != candidate.read_bytes()):
                problems.append(name + " dependency/build contract changed")
        scripts = baseline_start / "scripts"
        if scripts.is_dir():
            for original in sorted(scripts.iterdir()):
                if original.is_file():
                    candidate = candidate_start / "scripts" / original.name
                    if (not candidate.is_file() or not inside(candidate, workspace)
                            or candidate.read_bytes() != original.read_bytes()):
                        problems.append("scripts/" + original.name + " readiness/lifecycle contract changed")
        if variant == "raw":
            original, candidate = baseline_start / "compose.yaml", candidate_start / "compose.yaml"
            if (not original.is_file() or not candidate.is_file()
                    or compose_contract(original.read_text()) != compose_contract(candidate.read_text())):
                problems.append("Selected raw Compose contract changed")
        else:
            original = baseline / apphost.relative_to(workspace)
            if not original.is_file():
                problems.append("Selected AppHost missing in baseline")
            elif apphost_tokens(original.read_text()) != apphost_tokens(apphost.read_text()):
                problems.append("Selected AppHost waits/configuration/health contracts changed")
        if problems:
            return outcome("fail", "Healthy baseline contracts are not preserved", violations=problems)
        return outcome("pass", "Selected variant preserves baseline waits, db lookup, routes, seeding, and player behavior")
    except (OSError, ValueError) as error:
        return outcome("fail", "Cannot verify healthy baseline contracts: " + str(error))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_get(url, timeout):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(urllib.request.Request(url, headers={"Accept": "*/*"}), timeout=timeout) as response:
            body = response.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise ValueError("HTTP response exceeds the size limit")
            return response.status, response.headers.get("Content-Type", ""), body
    except urllib.error.HTTPError as error:
        with error:
            return error.code, error.headers.get("Content-Type", ""), b""


def check_frontend(url, timeout):
    try:
        status, content_type, body = http_get(url + "/", timeout)
        text = body.decode("utf-8")
        valid = (status == 200 and "text/html" in content_type.lower()
                 and re.search(r'id=["\']app["\']', text)
                 and re.search(r"<script\b[^>]*\bsrc=", text, re.I))
        return outcome("pass" if valid else "fail",
                       "Player frontend serves the application shell" if valid else
                       "Frontend did not return the expected 200 player application HTML",
                       http_status=status)
    except (OSError, ValueError) as error:
        return outcome("fail", "Frontend is unreachable or invalid: " + str(error))


def check_version(url, timeout):
    try:
        status, content_type, body = http_get(url + "/api/version-info", timeout)
        if status != 200 or "application/json" not in content_type.lower():
            return outcome("fail", "Version-info must return HTTP 200 JSON, not a redirect/page",
                           http_status=status), None
        data = json.loads(body)
        valid = (isinstance(data, dict)
                 and all(isinstance(data.get(key), str) for key in VERSION_FIELDS)
                 and all(data[key].strip() for key in VERSION_FIELDS if key != "commitSha"))
        if not valid:
            return outcome("fail", "Version-info JSON does not satisfy the original version contract"), None
        return outcome("pass", "Version-info returned the original JSON contract", http_status=200), data
    except (OSError, ValueError) as error:
        return outcome("fail", "Version-info is unreachable or invalid: " + str(error)), None


class ProbeError(Exception):
    def __init__(self, status, detail, exit_code=None):
        super().__init__(detail)
        self.status = status
        self.exit_code = exit_code


def command(args, timeout, cwd=None):
    try:
        result = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                                timeout=timeout, check=False)
    except FileNotFoundError as error:
        raise ProbeError("unknown", "Required executable is not installed: " + args[0]) from error
    except subprocess.TimeoutExpired as error:
        raise ProbeError("fail", "Runtime probe exceeded its deadline") from error
    except OSError as error:
        raise ProbeError("fail", "Unable to execute runtime probe: " + str(error)) from error
    if result.returncode != 0:
        # Do not include arbitrary command output: harness environments may contain secrets.
        raise ProbeError("fail", "Runtime probe exited with code " + str(result.returncode),
                         exit_code=result.returncode)
    if len(result.stdout) > MAX_BYTES:
        raise ProbeError("fail", "Runtime probe output exceeds the size limit")
    return result.stdout


def container_spec(metadata, service):
    spec = metadata.get(service)
    if spec is None:
        raise ProbeError("unknown", "No exact " + service + " container supplied for this run")
    if (not isinstance(spec, dict) or not isinstance(spec.get("container_id"), str)
            or not re.fullmatch(r"[a-fA-F0-9]{12,64}", spec["container_id"])):
        raise ProbeError("fail", "Expected an exact container ID, not a name or inferred resource")
    return spec


def inspect_container(runtime, container_id, timeout):
    try:
        values = json.loads(command([runtime, "inspect", "--type", "container", container_id], timeout))
        if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
            raise ValueError("Invalid container inspection result")
        data = values[0]
        if not isinstance(data.get("Id"), str) or not data["Id"].lower().startswith(container_id.lower()):
            raise ValueError("Container inspection returned a different ID")
        state = data.get("State", {})
        if (not isinstance(state, dict) or state.get("Running") is not True
                or state.get("Restarting") is True):
            raise ValueError("Selected container is not running stably")
        health = state.get("Health", state.get("Healthcheck", {}))
        if isinstance(health, dict) and health.get("Status") not in (None, "healthy"):
            raise ValueError("Selected container health is not healthy")
        count = data.get("RestartCount", 0)
        if type(count) is not int or count < 0:
            raise ValueError("Invalid container restart count")
        return count
    except (ValueError, TypeError) as error:
        raise ProbeError("fail", str(error)) from error


AUTH_SCRIPTS = {
    "redis": 'if [ -n "${REDIS_PASSWORD:-}" ]; then '
             'export REDISCLI_AUTH="$REDIS_PASSWORD"; fi; exec "$@"',
    "postgres": 'if [ -n "${POSTGRES_PASSWORD:-}" ]; then '
                'export PGPASSWORD="$POSTGRES_PASSWORD"; fi; exec "$@"',
}


def authenticated_command(runtime, container_id, service, argv, timeout):
    # The fixed script runs inside the selected container; SQL/user/database
    # remain separate argv entries, and secret values never cross the boundary.
    return command([runtime, "exec", container_id, "sh", "-c", AUTH_SCRIPTS[service],
                    "bingo-verifier-auth", *argv], timeout)


def check_redis(metadata, runtime, timeout, previous=None):
    try:
        spec = container_spec(metadata, "redis")
        container_id = spec["container_id"]
        restarts = inspect_container(runtime, container_id, timeout)
        ping = authenticated_command(runtime, container_id, "redis",
                                     ["redis-cli", "--raw", "PING"], timeout).strip()
        if re.search(r"\bNOAUTH\b|\bWRONGPASS\b|AUTH failed", ping, re.I):
            raise ProbeError("unknown", "Selected Redis authentication evidence is unavailable or rejected")
        if ping != "PONG":
            raise ProbeError("fail", "Selected Redis did not answer PING with PONG")
        if previous is not None and restarts != previous:
            raise ProbeError("fail", "Selected Redis restarted during runtime verification")
        return outcome("pass", "Exact-run Redis is running and answers PING", restart_count=restarts)
    except ProbeError as error:
        return outcome(error.status, str(error))


def postgres_sql():
    seed_list = ",".join("'" + seed + "'" for seed in SEED_IDS)
    return (
        'SELECT json_build_object('
        "'migration_count', (SELECT count(*) FROM \"__EFMigrationsHistory\" "
        "WHERE \"MigrationId\" = '20260206183116_InitialCreate'),"
        "'seed_count', (SELECT count(*) FROM \"BingoSquares\" "
        'WHERE "IsActive" AND length(trim("Label")) > 0 AND "Id" IN (' + seed_list + ")),"
        "'admin_count', (SELECT count(*) FROM \"AspNetUsers\" "
        "WHERE \"NormalizedUserName\" = 'ADMIN' AND length(\"PasswordHash\") > 0));"
    )


def check_postgres(metadata, runtime, timeout):
    try:
        spec = container_spec(metadata, "postgres")
        container_id = spec["container_id"]
        database, user = spec.get("database"), spec.get("user")
        if not all(isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_-]+", value)
                   for value in (database, user)):
            raise ProbeError("fail", "Postgres metadata must name the exact database and user")
        inspect_container(runtime, container_id, timeout)
        command([runtime, "exec", container_id, "pg_isready", "-h", "127.0.0.1",
                 "-U", user, "-d", database], timeout)
        try:
            output = authenticated_command(
                runtime, container_id, "postgres",
                ["psql", "-w", "-h", "127.0.0.1", "-X", "-A", "-t",
                 "-v", "ON_ERROR_STOP=1", "-U", user, "-d", database,
                 "-c", postgres_sql()], timeout)
        except ProbeError as error:
            if error.exit_code == 2:
                raise ProbeError(
                    "unknown", "Selected Postgres probe cannot authenticate/connect with container-local credentials"
                ) from error
            raise
        try:
            data = json.loads(output)
            if (not isinstance(data, dict) or any(type(data.get(key)) is not int
                    for key in ("migration_count", "seed_count", "admin_count"))):
                raise ValueError("Invalid Postgres observation")
        except (ValueError, TypeError) as error:
            raise ProbeError("fail", "Postgres returned invalid schema/seed observations") from error
        valid = data["migration_count"] == 1 and data["seed_count"] == len(SEED_IDS) and data["admin_count"] == 1
        return outcome("pass" if valid else "fail",
                       "Database is ready with expected migration, all seeded squares, and admin" if valid else
                       "Database is missing the expected schema or seed data", **data)
    except ProbeError as error:
        return outcome(error.status, str(error))


def check_migrations(metadata, workspace):
    spec = metadata.get("migrations")
    if spec is None:
        return outcome("unknown", "No exact-run migration completion evidence supplied")
    if not isinstance(spec, dict):
        return outcome("fail", "Migration evidence must be an object")
    if not isinstance(spec.get("state"), str) or type(spec.get("exit_code")) is not int:
        return outcome("unknown", "Migration completion requires an observed state and integer exit_code")
    if spec["state"].lower() not in {"exited", "finished", "completed"} or spec["exit_code"] != 0:
        return outcome("fail", "Migration worker did not complete successfully",
                       state=spec["state"], exit_code=spec["exit_code"])
    try:
        logs = spec.get("logs", "")
        if not isinstance(logs, str):
            raise ValueError("Migration logs must be text")
        if spec.get("log_path") is not None:
            log_path = external_path(spec["log_path"], workspace)
            if log_path.stat().st_size > MAX_BYTES:
                raise ValueError("Migration log exceeds the size limit")
            logs += log_path.read_text(encoding="utf-8")
        # EF can log failed history-table probes/retried commands during a healthy
        # first migration. Only terminal worker/startup errors contradict exit 0.
        if re.search(
                r"connection string .*required|unhandled exception|"
                r"backgroundservice failed|hosting failed to start|application startup exception",
                logs, re.I):
            return outcome("fail", "Migration logs contradict successful completion")
        return outcome("pass", "Trusted exact-run evidence reports successful migration exit", exit_code=0)
    except (OSError, ValueError, TypeError) as error:
        return outcome("fail", "Invalid migration evidence: " + str(error))


SIGNALR_SCRIPT = r"""
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import {randomUUID} from 'node:crypto';

const config = JSON.parse(process.argv[2]);
const results = {};
const timeout = config.timeout * 1000;
const originalLog = console.log.bind(console);
console.log = console.info = console.warn = console.error = () => {};
const require = createRequire(pathToFileURL(config.package));
let signalr, player, admin;
const connections = [];
const summary = (status, detail) => ({status, detail});
const bounded = (promise) => {
  let timer;
  return Promise.race([
    promise,
    new Promise((_, reject) => {timer = setTimeout(() => reject(new Error('Event/invocation deadline exceeded')), timeout);})
  ]).finally(() => clearTimeout(timer));
};
function watch(target, event, predicate = () => true, service = false) {
  let resolve, reject, timer;
  const promise = new Promise((yes, no) => {resolve = yes; reject = no;});
  const add = (name, callback) => service ? target.addEventListener(name, callback) : target.on(name, callback);
  const remove = (name, callback) => service ? target.removeEventListener(name, callback) : target.off(name, callback);
  function cleanup() {clearTimeout(timer); remove(event, receive); remove(service ? 'error' : 'Error', fail);}
  function receive(data) {
    try {if (predicate(data)) {cleanup(); resolve(data);}}
    catch (error) {cleanup(); reject(error);}
  }
  function fail() {cleanup(); reject(new Error('Hub emitted Error instead of the expected success event'));}
  add(event, receive); add(service ? 'error' : 'Error', fail);
  timer = setTimeout(() => {cleanup(); reject(new Error('Expected ' + event + ' was not observed'));}, timeout);
  return {promise, cancel: cleanup};
}
async function eventCall(target, event, invoke, predicate, service = false) {
  const pending = watch(target, event, predicate, service);
  try {
    const [, value] = await bounded(Promise.all([Promise.resolve().then(invoke), pending.promise]));
    return value;
  } finally {pending.cancel();}
}
function board(value, clientId) {
  if (!value || typeof value.id !== 'string' || !value.id || value.clientId !== clientId ||
      !Array.isArray(value.squares) || value.squares.length !== 25 ||
      value.squares.some(s => !s || typeof s.id !== 'string' || !s.id ||
        typeof s.label !== 'string' || !s.label.trim() || typeof s.isChecked !== 'boolean') ||
      new Set(value.squares.map(s => s.id)).size !== 25 ||
      value.squares[12].id !== 'free' || value.squares[12].isChecked !== true) {
    throw new Error('Received event lacks a real 25-square board for this persistent client');
  }
  return value;
}
function signature(value) {
  return JSON.stringify([value.id, value.clientId, value.squares.map(s => [s.id, s.label, s.isChecked])]);
}
async function stage(name, invoke) {
  try {await invoke(); results[name] = summary('pass', 'Required runtime events and data observed');}
  catch (error) {results[name] = summary('fail', String(error.message).slice(0, 300));}
}
try {
  // Load SignalR in Node mode before adding the minimal browser globals used by the player module.
  signalr = require('@microsoft/signalr');
  globalThis.window = {BACKEND_CONFIG: {adminUrl: config.frontend}, location: new URL(config.frontend)};
  const storage = new Map();
  globalThis.localStorage = {getItem: k => storage.get(k) ?? null,
    setItem: (k, v) => storage.set(k, String(v)), removeItem: k => storage.delete(k)};
  const candidate = await import(pathToFileURL(config.service).href);
  if (typeof candidate.SignalRService !== 'function') throw new Error('Candidate does not export the player SignalRService');
  const clientId = 'benchmark-' + randomUUID(), userName = 'Verifier ' + randomUUID();
  admin = new signalr.HubConnectionBuilder().withUrl(config.admin + '/bingohub', {
    skipNegotiation: true, transport: signalr.HttpTransportType.WebSockets
  }).configureLogging(signalr.LogLevel.None).build();
  connections.push(admin);
  await bounded(admin.start());
  player = new candidate.SignalRService();
  connections.push(player);
  await bounded(player.connect());
  let fresh, current, connectionId;
  async function requestBoard(event, method, adminEvent) {
    const observed = watch(admin, adminEvent, data => data?.userName === userName);
    try {
      const [value, notification] = await bounded(Promise.all([
        eventCall(player, event, () => player[method](clientId, userName), () => true, true),
        observed.promise
      ]));
      const received = board(value, clientId);
      if (!notification || typeof notification.connectionId !== 'string' || !notification.connectionId ||
          notification.bingoSetId !== received.id) {
        throw new Error('Admin did not observe the player request with its positional user name and real board');
      }
      connectionId = notification.connectionId;
      return received;
    } finally {observed.cancel();}
  }
  await stage('signalr_fresh', async () => {
    fresh = await requestBoard('bingoSetReceived', 'requestBingoSet', 'ClientBingoSetGenerated');
    current = fresh;
  });
  await stage('admin_player', async () => {
    if (!fresh) throw new Error('No real fresh board available');
    const clients = await eventCall(admin, 'ConnectedClientsList', () => admin.invoke('GetConnectedClients'));
    if (!Array.isArray(clients) || !connectionId ||
        !clients.some(c => c.connectionId === connectionId && c.currentBingoSetId === fresh.id)) {
      throw new Error('Admin does not see this exact player connection with its generated board');
    }
  });
  await stage('signalr_returning', async () => {
    if (!fresh) throw new Error('No real fresh board available');
    await bounded(player.disconnect());
    player = new candidate.SignalRService();
    connections.push(player);
    await bounded(player.connect());
    const previousConnection = connectionId;
    const returned = await requestBoard(
      'existingBingoSetReceived', 'requestExistingBingoSet', 'ClientBingoSetUpdated');
    if (connectionId === previousConnection) throw new Error('Returning-player probe did not establish a new connection');
    if (signature(returned) !== signature(fresh)) throw new Error('Returning player did not retrieve the persisted original board');
    current = returned;
  });
  await stage('signalr_new_board', async () => {
    if (!current) throw new Error('No real board available');
    const previousId = current.id;
    const next = await requestBoard('bingoSetReceived', 'requestBingoSet', 'ClientBingoSetGenerated');
    if (next.id === previousId) throw new Error('New-board request reused the original board ID');
    const persisted = await requestBoard(
      'existingBingoSetReceived', 'requestExistingBingoSet', 'ClientBingoSetUpdated');
    if (signature(persisted) !== signature(next)) throw new Error('New board was not persisted for the returning player');
    current = persisted;
  });
  await stage('live_update', async () => {
    if (!current || !connectionId) throw new Error('No registered player board available');
    const square = current.squares.find(s => s.id !== 'free');
    const original = square.isChecked;
    async function update(state) {
      await eventCall(player, 'squareUpdated',
        () => admin.invoke('AdminUpdateSquare', connectionId, square.id, state),
        data => data?.squareId === square.id && data.isChecked === state, true);
      const persisted = await requestBoard(
        'existingBingoSetReceived', 'requestExistingBingoSet', 'ClientBingoSetUpdated');
      if (persisted.id !== current.id || persisted.squares.find(s => s.id === square.id)?.isChecked !== state) {
        throw new Error('Admin update event was not reflected in the persisted player board');
      }
    }
    try {await update(!original);}
    finally {await update(original);}
  });
} catch (error) {
  const missing = error.code === 'MODULE_NOT_FOUND' || error.code === 'ERR_MODULE_NOT_FOUND';
  for (const name of config.checks) {
    if (!results[name]) results[name] = summary(missing ? 'unknown' : 'fail',
      missing ? 'Candidate installed SignalR package is unavailable' : String(error.message).slice(0, 300));
  }
} finally {
  for (const connection of connections.reverse()) {
    try {await bounded(connection.disconnect ? connection.disconnect() : connection.stop());}
    catch {results.cleanup = summary('fail', 'Verifier SignalR connection cleanup failed');}
  }
}
originalLog(JSON.stringify(results));
// Stop only this probe process, never an application or container.
process.exit(0);
"""


def check_signalr(source, frontend_url, admin_url, timeout):
    service = source / "bingo-board/services/signalrService.js"
    package = source / "bingo-board/package.json"
    if (not service.is_file() or not package.is_file()
            or not inside(service, source) or not inside(package, source)):
        return {name: outcome("fail", "Candidate player module/package is missing or outside the workspace")
                for name in PLAYER_CHECKS}
    if not shutil.which("node"):
        return {name: outcome("unknown", "Node is unavailable for the candidate player runtime probe")
                for name in PLAYER_CHECKS}
    config = {"service": str(service), "package": str(package), "frontend": frontend_url,
              "admin": admin_url, "timeout": timeout, "checks": list(PLAYER_CHECKS)}
    try:
        with tempfile.TemporaryDirectory(prefix="bingo-verifier-") as directory:
            script = Path(directory) / "probe.mjs"
            script.write_text(SIGNALR_SCRIPT, encoding="utf-8")
            output = command(["node", str(script), json.dumps(config)], timeout * 20 + 5,
                             cwd=package.parent)
        data = json.loads(output)
        if not isinstance(data, dict):
            raise ValueError("SignalR probe did not return an object")
        for name in PLAYER_CHECKS:
            item = data.get(name)
            if (not isinstance(item, dict) or item.get("status") not in {"pass", "fail", "unknown"}
                    or not isinstance(item.get("detail"), str)):
                raise ValueError("SignalR probe returned an invalid or missing check")
        if data.get("cleanup", {}).get("status") == "fail":
            data["live_update"] = outcome("fail", "Verifier connection cleanup failed")
        return {name: data[name] for name in PLAYER_CHECKS}
    except ProbeError as error:
        return {name: outcome(error.status, str(error)) for name in PLAYER_CHECKS}
    except (OSError, ValueError, TypeError) as error:
        return {name: outcome("fail", "Invalid SignalR runtime observation: " + str(error))
                for name in PLAYER_CHECKS}


def verify(args):
    workspace = Path(args.workspace).resolve()
    checks = {}
    try:
        if not workspace.is_dir():
            raise ValueError("Candidate workspace does not exist")
        source = find_source(workspace)
        if not inside(source, workspace):
            raise ValueError("Shared source resolves outside the candidate workspace")
        apphost = None
        if args.variant != "raw":
            if not args.apphost:
                raise ValueError("--apphost is required for the selected Aspire variant")
            apphost = Path(args.apphost)
            if not apphost.is_absolute():
                apphost = workspace / apphost
            apphost = apphost.resolve()
            expected = ".mts" if args.variant == "typescript" else ".cs"
            if not apphost.is_file() or not inside(apphost, workspace) or apphost.suffix != expected:
                raise ValueError("Selected AppHost is missing, outside the workspace, or the wrong language")
        frontend_url, admin_url = normalized_url(args.frontend_url), normalized_url(args.admin_url)
        checks["workspace"] = outcome("pass", "Selected workspace, shared source, and variant exist")
    except (OSError, ValueError) as error:
        checks["workspace"] = outcome("fail", str(error))
        return build_result(args.variant, checks, {"success": False, "status": "fail",
                            "detail": "Workspace cannot be evaluated", "faults": {}})

    metadata, checks["runtime_metadata"] = load_metadata(
        args.runtime_metadata, workspace, args.variant, frontend_url, admin_url, apphost)
    diagnosis = score_diagnosis(args.diagnosis, workspace, source, apphost, args.variant)
    checks["contracts"] = check_contracts(workspace, source, args.variant, apphost, args.baseline)
    checks["ownership"] = check_ownership(metadata, frontend_url, admin_url)
    if checks["ownership"]["status"] != "pass":
        for name in ("postgres", "migrations", "redis", "frontend",
                     "version_direct", "version_proxy", *PLAYER_CHECKS):
            checks[name] = outcome("unknown", "No runtime traffic: exact-run ownership is unproved")
        return build_result(args.variant, checks, diagnosis)
    runtime = args.container_runtime or metadata.get("container_runtime", "podman")
    checks["postgres"] = check_postgres(metadata, runtime, args.timeout)
    checks["migrations"] = check_migrations(metadata, workspace)
    checks["redis"] = check_redis(metadata, runtime, args.timeout)
    checks["frontend"] = check_frontend(frontend_url, args.timeout)
    checks["version_direct"], direct = check_version(admin_url, args.timeout)
    checks["version_proxy"], proxied = check_version(frontend_url, args.timeout)
    if direct is not None and proxied is not None and direct != proxied:
        checks["version_proxy"] = outcome("fail", "Frontend version JSON differs from the selected admin")
    if checks["runtime_metadata"]["status"] == "pass":
        checks.update(check_signalr(source, frontend_url, admin_url, args.timeout))
    else:
        checks.update({name: outcome("unknown", "Cannot mutate player state without exact-run metadata")
                       for name in PLAYER_CHECKS})
    if checks["redis"]["status"] == "pass":
        previous = checks["redis"]["restart_count"]
        checks["redis"] = check_redis(metadata, runtime, args.timeout, previous)
    return build_result(args.variant, checks, diagnosis)


def build_result(variant, checks, diagnosis):
    required = ("workspace", "runtime_metadata", "ownership", "contracts", "postgres", "migrations",
                "redis", "frontend", "version_direct", "version_proxy", *PLAYER_CHECKS)
    missing = outcome("unknown", "Check could not be performed")
    for name in required:
        checks.setdefault(name, dict(missing))
    success = all(checks[name]["status"] == "pass" for name in required)
    faults = {}
    for fault, names in FAULT_CHECKS.items():
        diagnosed = diagnosis.get("faults", {}).get(fault, outcome("fail", "No valid diagnosis"))
        faults[fault] = {
            "repair": aggregate([checks[name] for name in names]),
            "diagnosis": diagnosed["status"],
            "candidate_count": diagnosed.get("candidate_count", 0),
            "checks": list(names),
        }
    return {"schema_version": 1, "variant": variant, "repair_success": success,
            "diagnosis_success": diagnosis["success"],
            "repair": {"success": success, "status": aggregate([checks[n] for n in required])},
            "diagnosis": diagnosis, "checks": checks, "faults": faults}


def bounded_timeout(value):
    try:
        number = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Timeout must be a number") from error
    if not math.isfinite(number) or not 0 < number <= 60:
        raise argparse.ArgumentTypeError("Timeout must be finite, positive, and at most 60 seconds")
    return number


def parser():
    result = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    result.add_argument("--workspace", required=True)
    result.add_argument("--variant", required=True, choices=("raw", "typescript", "csharp"))
    result.add_argument("--apphost")
    result.add_argument("--frontend-url", required=True)
    result.add_argument("--admin-url", required=True)
    result.add_argument("--diagnosis", required=True)
    result.add_argument("--output", required=True)
    result.add_argument("--runtime-metadata")
    result.add_argument("--baseline")
    result.add_argument("--timeout", type=bounded_timeout, default=10.0)
    result.add_argument("--container-runtime", choices=("podman", "docker"))
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        destination = external_path(args.output, Path(args.workspace))
        if any(destination == Path(value).resolve() for value in
               (args.diagnosis, args.runtime_metadata) if value is not None):
            raise ValueError("Output cannot overwrite evaluator inputs")
        if args.baseline and inside(destination, Path(args.baseline)):
            raise ValueError("Output cannot overwrite the trusted baseline")
        result = verify(args)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent,
                                         prefix=".verifier-", delete=False) as stream:
            temporary = Path(stream.name)
            try:
                json.dump(result, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        try:
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        print(json.dumps({"repair_success": result["repair_success"],
                          "diagnosis_success": result["diagnosis_success"],
                          "output": str(destination)}))
        return 0 if result["repair_success"] else 1
    except (OSError, ValueError) as error:
        parser().error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
