"""Run with: python3 -B -m unittest discover -s eng/agent-bench/tests -p test_verify.py

All HTTP and player-module fixtures use fresh temporary snapshots. No test
discovers, starts, stops, or contacts an existing application/container.

Opt-in full application fixture:
    BINGO_VERIFY_LIVE=1 python3 -B -m unittest discover \
        -s eng/agent-bench/tests -p test_verify.py
Requires Docker, .NET 10 SDK/runtime, Node/npm, and dependency downloads. Only
the fixture's newly created exact container IDs/processes are removed/stopped.
"""

import argparse
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tarfile
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch


VERIFIER_PATH = Path(__file__).resolve().parents[1] / "verify.py"
REPO = VERIFIER_PATH.parents[2]
SPEC = importlib.util.spec_from_file_location("bingo_verify", VERIFIER_PATH)
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)
SERVICE_PATH = REPO / "demo/start/src/bingo-board/services/signalrService.js"
CONTAINER_ID = "a" * 64
VERSION = dict(zip(verify.VERSION_FIELDS,
                   ("", "dev", "https://example.invalid/repo", "10.0", "not configured", "7.3.6")))


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def write_json(path, content):
    return write(path, json.dumps(content))


def healthy_player():
    return SERVICE_PATH.read_text().replace(
        "{ clientId: persistentClientId, userName }", "persistentClientId, userName")


def seeded_player():
    return healthy_player().replace(
        "persistentClientId, userName)", "{ clientId: persistentClientId, userName })")


class SnapshotCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="bingo-verifier-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "candidate"
        self.source = self.workspace / "demo/start/src"
        self.source.mkdir(parents=True)
        self.player = self.source / "bingo-board"
        write_json(self.player / "package.json", {"type": "module"})
        write(self.player / "services/signalrService.js", healthy_player())
        write(self.source / "BingoBoard.MigrationService/Program.cs",
              'builder.Services.AddHostedService<Worker>();\n'
              'var c = configuration.GetConnectionString("db") ?? throw new Exception("db required");')
        write(self.source / "BingoBoard.MigrationService/Worker.cs",
              "await MigrateAsync(); await SeedAsync();")
        write(self.source / "BingoBoard.Admin/Program.cs",
              'app.MapGet("/api/version-info", () => versions.GetVersionInfo());')
        write(self.source / "BingoBoard.Admin/Hubs/BingoHub.cs",
              "public Task RequestBingoSet(string clientId, string userName) { return SendBoard(); }")
        write(self.source.parent / "compose.yaml",
              'services:\n  redis:\n    image: redis:7\n    ports:\n      - "6379:6379"\n')
        write(self.source.parent / "scripts/check.sh", "curl --fail \"$url/api/version-info\"\n")
        self.baseline = self.root / "healthy"
        shutil.copytree(self.workspace, self.baseline)
        self.diagnosis_path = self.root / "diagnosis.json"

    def answer(self, variant="raw", apphost=None):
        prefix = self.source.relative_to(self.workspace).as_posix()
        health_path = (self.source.parent / "compose.yaml").relative_to(self.workspace).as_posix()
        if variant != "raw":
            health_path = apphost.relative_to(self.workspace).as_posix()
        return {"diagnoses": [
            {"service": "redis", "candidates": [{
                "cause": "Redis has an invalid allkeys-lfr eviction policy.",
                "file": health_path, "evidence": "Redis log: FATAL CONFIG FILE ERROR: allkeys-lfr"}]},
            {"service": "migrations", "candidates": [{
                "cause": "Connection string key mismatch: worker reads database instead of injected db.",
                "file": prefix + "/BingoBoard.MigrationService/Program.cs",
                "evidence": "Connection string 'database' is required; GetConnectionString(\"database\")"}]},
            {"service": "boardadmin", "candidates": [{
                "cause": "Backend exposes wrong route /api/version instead of /api/version-info.",
                "file": prefix + "/BingoBoard.Admin/Program.cs",
                "evidence": "GET /api/version-info returns 404; MapGet(\"/api/version\")"}]},
            {"service": "bingoboard", "candidates": [{
                "cause": "RequestBingoSet and RequestExistingBingoSet send one object payload rather than two positional strings.",
                "file": prefix + "/bingo-board/services/signalrService.js",
                "evidence": "SignalR invoke('RequestExistingBingoSet', object) fails because the hub expects two arguments"}]},
        ]}

    def score(self, document=None, variant="raw", apphost=None):
        write_json(self.diagnosis_path, self.answer(variant, apphost) if document is None else document)
        return verify.score_diagnosis(self.diagnosis_path, self.workspace, self.source, apphost, variant)

    def metadata(self, **extra):
        return {"run_id": "fixture", "workspace": str(self.workspace), "variant": "raw", **extra}


class DiagnosisTests(SnapshotCase):
    def test_four_independent_singletons_are_correct(self):
        result = self.score()
        self.assertTrue(result["success"])
        self.assertEqual([result["faults"][f]["candidate_count"] for f in verify.FAULTS], [1] * 4)

    def test_a_correct_candidate_among_guesses_is_not_resolved(self):
        document = self.answer()
        document["diagnoses"][0]["candidates"].append({
            "cause": "Maybe Redis port is wrong", "file": "demo/start/compose.yaml",
            "evidence": "Connection failed"})
        result = self.score(document)
        self.assertFalse(result["success"])
        self.assertEqual(result["faults"]["HEALTH-01"]["candidate_count"], 2)
        self.assertEqual(result["faults"]["CONFIG-01"]["status"], "pass")

    def test_split_duplicate_diagnoses_are_still_multiple_guesses(self):
        document = self.answer()
        document["diagnoses"].append(copy.deepcopy(document["diagnoses"][0]))
        self.assertEqual(self.score(document)["faults"]["HEALTH-01"]["candidate_count"], 2)

    def test_same_fault_guesses_cannot_hide_under_an_unknown_service_name(self):
        document = self.answer()
        extra = copy.deepcopy(document["diagnoses"][0])
        extra["service"] = "another guess"
        document["diagnoses"].append(extra)
        self.assertEqual(self.score(document)["faults"]["HEALTH-01"]["candidate_count"], 2)

    def test_trial_prefixed_resource_names_match_the_logical_service(self):
        document = self.answer()
        document["diagnoses"][0]["service"] = "trial-01234-cache"
        self.assertTrue(self.score(document)["success"])

    def test_duplicate_json_fields_are_malformed_not_a_singleton(self):
        write(self.diagnosis_path, '{"diagnoses": [], "diagnoses": []}')
        result = verify.score_diagnosis(
            self.diagnosis_path, self.workspace, self.source, None, "raw")
        self.assertFalse(result["success"])
        self.assertIn("Duplicate", result["detail"])

    def test_missing_or_malformed_answers_fail_diagnosis_only(self):
        for malformed in ({}, {"diagnoses": "fixed"}, {"diagnoses": [None]},
                          {"diagnoses": [{"service": "redis", "candidates": []}]}):
            with self.subTest(malformed=malformed):
                self.assertFalse(self.score(malformed)["success"])
        self.diagnosis_path.unlink()
        self.assertFalse(verify.score_diagnosis(
            self.diagnosis_path, self.workspace, self.source, None, "raw")["success"])

    def test_guessed_cause_without_evidenced_conclusion_fails(self):
        document = self.answer()
        document["diagnoses"][2]["candidates"][0]["evidence"] = "I fixed it"
        self.assertEqual(self.score(document)["faults"]["INTEROP-01"]["status"], "fail")
        document["diagnoses"][2]["candidates"][0]["evidence"] = ""
        self.assertFalse(self.score(document)["success"])

    def test_service_and_target_path_are_required(self):
        document = self.answer()
        document["diagnoses"][1]["candidates"][0]["file"] = "demo/start/src/BingoBoard.Admin/Program.cs"
        self.assertEqual(self.score(document)["faults"]["CONFIG-01"]["status"], "fail")
        document = self.answer()
        document["diagnoses"][0]["service"] = "postgres"
        self.assertEqual(self.score(document)["faults"]["HEALTH-01"]["status"], "fail")

    def test_cause_negation_and_generic_symptoms_are_not_diagnoses(self):
        for cause in ("Redis is down", "allkeys-lfr is not invalid and works correctly"):
            document = self.answer()
            document["diagnoses"][0]["candidates"][0]["cause"] = cause
            self.assertEqual(self.score(document)["faults"]["HEALTH-01"]["status"], "fail")

    def test_external_paths_do_not_count_but_line_suffixes_do(self):
        document = self.answer()
        document["diagnoses"][0]["candidates"][0]["file"] = "/other/start/compose.yaml"
        self.assertFalse(self.score(document)["success"])
        document = self.answer()
        document["diagnoses"][0]["candidates"][0]["file"] += ":9-10"
        self.assertTrue(self.score(document)["success"])

    def test_only_the_selected_apphost_is_scored(self):
        apphost = write(self.workspace / "demo/checkpoints/03-observe/typescript/apphost.mts", "await run();")
        self.assertTrue(self.score(variant="typescript", apphost=apphost)["success"])
        document = self.answer("typescript", apphost)
        document["diagnoses"][0]["candidates"][0]["file"] = "demo/checkpoints/03-observe/csharp/apphost.cs"
        self.assertFalse(self.score(document, "typescript", apphost)["success"])

    def test_diagnosis_input_inside_trial_is_rejected(self):
        path = write_json(self.workspace / "answer.json", self.answer())
        self.assertFalse(verify.score_diagnosis(path, self.workspace, self.source, None, "raw")["success"])


class MetadataAndContainerTests(SnapshotCase):
    def load(self, metadata):
        path = write_json(self.root / "runtime.json", metadata)
        return verify.load_metadata(path, self.workspace, "raw", "http://localhost:5001", "http://localhost:5000")

    def test_missing_metadata_is_unknown(self):
        self.assertEqual(verify.load_metadata(
            None, self.workspace, "raw", "http://localhost:5001", "http://localhost:5000")[1]["status"], "unknown")

    def test_metadata_bound_to_exact_workspace_variant_and_endpoints(self):
        result, check = self.load(self.metadata(frontend_url="http://localhost:5001/"))
        self.assertEqual(check["status"], "pass")
        self.assertEqual(result["run_id"], "fixture")
        for extra in ({"workspace": str(self.root / "different")}, {"variant": "typescript"},
                      {"run_id": ""}, {"admin_url": "http://localhost:9999"},
                      {"endpoints": {"frontend": "http://localhost:9999"}}):
            with self.subTest(extra=extra):
                self.assertEqual(self.load(self.metadata(**extra))[1]["status"], "fail")

    def test_runtime_apphost_must_match_the_selected_apphost(self):
        path = write_json(self.root / "runtime.json",
                          self.metadata(variant="typescript", apphost="other.mts"))
        apphost = self.workspace / "selected.mts"
        _, result = verify.load_metadata(
            path, self.workspace, "typescript", "http://localhost:5001", "http://localhost:5000", apphost)
        self.assertEqual(result["status"], "fail")

    def test_migration_missing_exit_is_unknown_not_zero_by_default(self):
        for item in (None, {"state": "Exited"}, {"exit_code": 0},
                     {"state": "Exited", "exit_code": False}):
            metadata = {} if item is None else {"migrations": item}
            self.assertEqual(verify.check_migrations(metadata, self.workspace)["status"], "unknown")

    def test_migration_exit_state_and_logs_are_all_required_when_supplied(self):
        for state, code, expected in (("Exited", 0, "pass"), ("Finished", 0, "pass"),
                                      ("Running", 0, "fail"), ("Exited", 1, "fail")):
            metadata = {"migrations": {"state": state, "exit_code": code}}
            self.assertEqual(verify.check_migrations(metadata, self.workspace)["status"], expected)
        log = write(self.root / "migration.log", "fail: Connection string 'database' is required")
        metadata = {"migrations": {"state": "Exited", "exit_code": 0, "log_path": str(log)}}
        self.assertEqual(verify.check_migrations(metadata, self.workspace)["status"], "fail")
        log.write_text("fail: Microsoft.EntityFrameworkCore.Database.Command\n"
                       "Failed executing DbCommand: __EFMigrationsHistory does not exist\n"
                       "Migrating the database...\nSeeded 40 missing bingo squares.")
        self.assertEqual(verify.check_migrations(metadata, self.workspace)["status"], "pass")
        metadata["migrations"]["log_path"] = str(self.workspace / "agent.log")
        self.assertEqual(verify.check_migrations(metadata, self.workspace)["status"], "fail")

    def test_missing_container_and_missing_tool_are_unknown(self):
        self.assertEqual(verify.check_redis({}, "podman", 0.1)["status"], "unknown")
        with patch.object(verify, "command", side_effect=verify.ProbeError("unknown", "tool missing")):
            result = verify.check_redis({"redis": {"container_id": CONTAINER_ID}}, "podman", 0.1)
        self.assertEqual(result["status"], "unknown")

    def test_container_names_and_short_ids_are_not_discovery(self):
        for invalid in ("redis", "workshop-redis-1", "abc", "a" * 65, "a" * 12 + ";stop", 123456789012):
            with patch.object(verify, "command") as command:
                result = verify.check_redis({"redis": {"container_id": invalid}}, "docker", 1)
                self.assertEqual(result["status"], "fail")
                command.assert_not_called()

    def inspection(self, **extra):
        return json.dumps([{"Id": CONTAINER_ID, "State": {"Running": True},
                            "RestartCount": 0, **extra}])

    def test_ping_and_stable_exact_container_are_required(self):
        metadata = {"redis": {"container_id": CONTAINER_ID}}
        with patch.object(verify, "command", side_effect=[self.inspection(), "PONG\n"]) as call:
            result = verify.check_redis(metadata, "docker", 1, 0)
            self.assertEqual(result["status"], "pass")
            self.assertEqual(call.call_args_list[1].args[0],
                             ["docker", "exec", CONTAINER_ID, "redis-cli", "--raw", "PING"])
        for inspect, ping in ((self.inspection(), "NOAUTH\n"),
                              (self.inspection(RestartCount=1), "PONG\n"),
                              (self.inspection(Id="b" * 64), "PONG\n"),
                              (self.inspection(State={"Running": False}), "PONG\n"),
                              (self.inspection(State={"Running": True, "Health": {"Status": "unhealthy"}}), "PONG\n")):
            with patch.object(verify, "command", side_effect=[inspect, ping]):
                self.assertEqual(verify.check_redis(metadata, "docker", 1, 0)["status"], "fail")

    def test_postgres_schema_and_all_seed_rows_are_verified_live(self):
        metadata = {"postgres": {"container_id": CONTAINER_ID, "user": "postgres", "database": "bingo"}}
        observed = {"migration_count": 1, "seed_count": len(verify.SEED_IDS), "admin_count": 1}
        with patch.object(verify, "command", side_effect=[self.inspection(), "ready", json.dumps(observed)]) as call:
            result = verify.check_postgres(metadata, "docker", 1)
            self.assertEqual(result["status"], "pass")
            sql = call.call_args_list[2].args[0][-1]
            self.assertIn('"__EFMigrationsHistory"', sql)
            self.assertIn("'20260206183116_InitialCreate'", sql)
            self.assertTrue(all("'" + seed + "'" in sql for seed in verify.SEED_IDS))
        for key in observed:
            bad = {**observed, key: 0}
            with patch.object(verify, "command", side_effect=[self.inspection(), "ready", json.dumps(bad)]):
                self.assertEqual(verify.check_postgres(metadata, "docker", 1)["status"], "fail")

    def test_command_deadlines_and_missing_executable_are_explicit(self):
        with self.assertRaises(verify.ProbeError) as context:
            verify.command(["nonexistent-bingo-verifier-executable"], 0.1)
        self.assertEqual(context.exception.status, "unknown")
        with patch.object(verify.subprocess, "run", side_effect=subprocess.TimeoutExpired("probe", 1)):
            with self.assertRaises(verify.ProbeError) as context:
                verify.command(["probe"], 1)
            self.assertEqual(context.exception.status, "fail")


class ContractTests(SnapshotCase):
    def check(self, variant="raw", apphost=None):
        return verify.check_contracts(self.workspace, self.source, variant, apphost, self.baseline)

    def test_no_baseline_means_unknown_not_pass(self):
        self.assertEqual(verify.check_contracts(
            self.workspace, self.source, "raw", None, None)["status"], "unknown")

    def test_raw_snapshot_needs_no_apphosts(self):
        self.assertEqual(self.check()["status"], "pass")
        write(self.source.parent / "compose.yaml",
              'services:\n  redis:\n    image: redis:7\n'
              '    command: ["redis-server", "--maxmemory-policy", "allkeys-lru"]\n'
              '    ports:\n      - "6379:6379"\n')
        self.assertEqual(self.check()["status"], "pass")

    def test_source_pattern_cannot_substitute_for_runtime_payload_check(self):
        write(self.player / "services/signalrService.js", seeded_player())
        self.assertEqual(self.check()["status"], "pass")

    def test_skipping_migrations_seeding_endpoint_or_checks_fails(self):
        paths = (
            self.source / "BingoBoard.MigrationService/Program.cs",
            self.source / "BingoBoard.MigrationService/Worker.cs",
            self.source / "BingoBoard.Admin/Program.cs",
            self.source.parent / "scripts/check.sh",
        )
        for path in paths:
            before = path.read_text()
            path.write_text("/* claim success without behavior */")
            with self.subTest(path=path):
                self.assertEqual(self.check()["status"], "fail")
            path.write_text(before)
        write(self.player / "services/signalrService.js", healthy_player().replace(
            "this.connection.on('SquareUpdated'", "this.connection.on('RemovedSquareUpdated'"))
        self.assertEqual(self.check()["status"], "fail")

    def test_selected_language_only_and_waits_preserved(self):
        for language, filename, text in (
            ("typescript", "apphost.mts",
             "const cache = builder.addRedis('cache'); admin.waitFor(cache).waitForCompletion(migrations);"),
            ("csharp", "apphost.cs",
             'var cache = builder.AddRedis("cache"); admin.WaitFor(cache).WaitForCompletion(migrations);'),
        ):
            path = Path("demo/checkpoints/03-observe") / language / filename
            apphost = write(self.workspace / path, text)
            write(self.baseline / path, text)
            self.assertEqual(self.check(language, apphost)["status"], "pass")
            apphost.write_text(text.replace("waitForCompletion(migrations)", "waitFor(migrations)")
                               .replace("WaitForCompletion(migrations)", "WaitFor(migrations)"))
            self.assertEqual(self.check(language, apphost)["status"], "fail")

    def test_only_redis_policy_arguments_are_permitted_apphost_changes(self):
        path = Path("demo/checkpoints/03-observe/typescript/apphost.mts")
        baseline = "const cache = builder.addRedis('cache'); admin.waitFor(cache);"
        apphost = write(self.workspace / path, baseline.replace(
            "addRedis('cache')", "addRedis('cache').withArgs(['--maxmemory-policy','allkeys-lru'])"))
        write(self.baseline / path, baseline)
        self.assertEqual(self.check("typescript", apphost)["status"], "pass")
        apphost.write_text(baseline.replace("addRedis('cache')", "addRedis('cache').withArgs(['--port','1'])"))
        self.assertEqual(self.check("typescript", apphost)["status"], "fail")

    def test_raw_port_remap_must_also_be_in_trusted_baseline(self):
        compose = self.source.parent / "compose.yaml"
        compose.write_text(compose.read_text().replace("6379:6379", "16379:6379"))
        self.assertEqual(self.check()["status"], "fail")
        (self.baseline / "demo/start/compose.yaml").write_text(compose.read_text())
        self.assertEqual(self.check()["status"], "pass")

    def test_central_dependency_contract_cannot_be_weakened(self):
        write(self.baseline / "demo/start/Directory.Packages.props", "<Packages>original</Packages>")
        write(self.source.parent / "Directory.Packages.props", "<Packages>replacement</Packages>")
        self.assertEqual(self.check()["status"], "fail")


class HttpFixture:
    def __init__(self, version=None, version_status=200, html=None, content_type="application/json"):
        data = VERSION if version is None else version
        html = '<html><div id="app"></div><script type="module" src="/main.js"></script></html>' if html is None else html

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/api/version-info":
                    self.send_response(version_status)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Location", "/")
                    self.end_headers()
                    self.wfile.write(json.dumps(data).encode())
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(html.encode())

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return "http://127.0.0.1:" + str(self.server.server_port)

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class HttpAndCliTests(SnapshotCase):
    def test_frontend_and_original_json_contract(self):
        with HttpFixture() as url:
            self.assertEqual(verify.check_frontend(url, 1)["status"], "pass")
            result, data = verify.check_version(url, 1)
            self.assertEqual(result["status"], "pass")
            self.assertEqual(data, VERSION)

    def test_404_redirect_html_fake_json_and_empty_frontend_fail(self):
        for status, data, content_type in ((404, VERSION, "application/json"),
                                           (302, VERSION, "application/json"),
                                           (200, {"success": True}, "application/json"),
                                           (200, VERSION, "text/html")):
            with self.subTest(status=status, data=data), HttpFixture(
                    version=data, version_status=status, content_type=content_type) as url:
                self.assertEqual(verify.check_version(url, 1)[0]["status"], "fail")
        with HttpFixture(html="<html>Everything is fixed</html>") as url:
            self.assertEqual(verify.check_frontend(url, 1)["status"], "fail")

    def args(self, frontend, admin=None, **extra):
        return argparse.Namespace(
            workspace=str(self.workspace), variant="raw", apphost=None,
            frontend_url=frontend, admin_url=admin or frontend,
            diagnosis=str(self.diagnosis_path), output=str(self.root / "result.json"),
            runtime_metadata=None, baseline=str(self.baseline), timeout=0.2,
            container_runtime=None, **extra)

    def test_unreachable_runtime_is_a_failure_not_agent_claim_success(self):
        # Closing our own allocated listener yields a known unused fixture endpoint.
        with HttpFixture() as url:
            pass
        write_json(self.diagnosis_path, self.answer())
        result = verify.verify(self.args(url))
        self.assertFalse(result["repair_success"])
        self.assertTrue(result["diagnosis_success"])
        self.assertEqual(result["checks"]["frontend"]["status"], "fail")
        self.assertEqual(result["checks"]["redis"]["status"], "unknown")
        self.assertEqual(result["checks"]["signalr_fresh"]["status"], "unknown")

    def test_successful_runtime_is_separate_from_malformed_diagnosis(self):
        write_json(self.diagnosis_path, {"claim": "all fixed"})
        metadata_path = write_json(self.root / "runtime.json", self.metadata())
        passed = verify.outcome("pass", "fixture runtime observation")
        with HttpFixture() as url, patch.object(verify, "check_postgres", return_value=passed), \
                patch.object(verify, "check_migrations", return_value=passed), \
                patch.object(verify, "check_redis", return_value={**passed, "restart_count": 0}), \
                patch.object(verify, "check_signalr", return_value={name: passed for name in verify.PLAYER_CHECKS}):
            args = self.args(url)
            args.runtime_metadata = str(metadata_path)
            result = verify.verify(args)
        self.assertTrue(result["repair_success"])
        self.assertFalse(result["diagnosis_success"])

    def test_direct_and_proxy_versions_must_agree(self):
        write_json(self.diagnosis_path, self.answer())
        with HttpFixture() as admin, HttpFixture(version={**VERSION, "dotNetVersion": "different"}) as frontend:
            result = verify.verify(self.args(frontend, admin))
        self.assertEqual(result["checks"]["version_direct"]["status"], "pass")
        self.assertEqual(result["checks"]["version_proxy"]["status"], "fail")

    def test_output_is_external_persistent_and_contains_unknowns(self):
        args = ["--workspace", str(self.workspace), "--variant", "raw",
                "--frontend-url", "http://127.0.0.1:1", "--admin-url", "http://127.0.0.1:1",
                "--diagnosis", str(self.diagnosis_path), "--output", str(self.root / "result.json"),
                "--timeout", "0.1"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(verify.main(args), 1)
        output = verify.read_json(self.root / "result.json")
        self.assertEqual(output["schema_version"], 1)
        self.assertFalse(output["repair_success"])
        self.assertFalse(output["diagnosis_success"])
        self.assertEqual(output["checks"]["migrations"]["status"], "unknown")
        args[args.index("--output") + 1] = str(self.workspace / "output.json")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as context:
            verify.main(args)
        self.assertEqual(context.exception.code, 2)
        self.assertFalse((self.workspace / "output.json").exists())

    def test_absent_workspace_and_wrong_apphost_language_are_explicit(self):
        args = self.args("http://127.0.0.1:1")
        args.workspace = str(self.root / "nonexistent")
        self.assertEqual(verify.verify(args)["checks"]["workspace"]["status"], "fail")
        args.workspace = str(self.workspace)
        args.variant = "typescript"
        args.apphost = str(write(self.workspace / "apphost.cs", "run();"))
        self.assertEqual(verify.verify(args)["checks"]["workspace"]["status"], "fail")

    def test_unknown_never_aggregates_as_success(self):
        self.assertEqual(verify.aggregate([verify.outcome("pass", ""), verify.outcome("unknown", "")]), "unknown")
        self.assertEqual(verify.aggregate([verify.outcome("unknown", ""), verify.outcome("fail", "")]), "fail")
        self.assertEqual(verify.aggregate([]), "unknown")
        for value in ("nan", "inf", "0", "-1", "61", "bad"):
            with self.assertRaises(argparse.ArgumentTypeError):
                verify.bounded_timeout(value)


# This controlled hub emulator replaces only the installed transport dependency.
# The verifier still imports and invokes the real candidate JS module, never a
# replacement player implementation. It deliberately models swallowed hub Error
# events, skipNegotiation's null client connectionId, and persisted board state.
FAKE_SIGNALR = r"""
const {randomUUID} = require('node:crypto');
const mode = __MODE__;
const connections = new Map(), boards = new Map();
const clone = value => JSON.parse(JSON.stringify(value));
function makeBoard(id) {
  const squares = Array.from({length:25}, (_, i) => ({
    id: i === 12 ? 'free' : 'square-' + i, label:'Square ' + i, isChecked: i === 12
  }));
  return {id: randomUUID(), clientId:id, squares: mode === 'empty-board' ? [] : squares};
}
class Connection {
  constructor() {this.handlers = new Map(); this.connectionId = null; this.id = randomUUID();}
  on(name, f) {if (!this.handlers.has(name)) this.handlers.set(name, []); this.handlers.get(name).push(f);}
  off(name, f) {this.handlers.set(name, (this.handlers.get(name) || []).filter(x => x !== f));}
  emit(name, data) {for (const f of this.handlers.get(name) || []) f(clone(data));}
  onreconnecting() {}
  onreconnected() {}
  onclose(f) {this.closed = f;}
  async start() {connections.set(this.id, this);}
  async stop() {connections.delete(this.id); if (this.closed) this.closed();}
  async invoke(name, ...args) {
    if (name === 'GetConnectedClients') {
      this.emit('ConnectedClientsList', mode === 'admin-empty' ? [] :
        [...connections.values()].map(c => ({connectionId:c.id, currentBingoSetId:c.boardId})));
      return;
    }
    if (name === 'AdminUpdateSquare') {
      const [connectionId, squareId, state] = args, player = connections.get(connectionId);
      if (!player) {this.emit('Error', 'Unknown player'); return;}
      const value = boards.get(player.clientId), square = value.squares.find(s => s.id === squareId);
      if (mode !== 'live-event-only') square.isChecked = state;
      if (mode !== 'live-no-event') player.emit('SquareUpdated', {squareId, isChecked:state});
      return;
    }
    if (name !== 'RequestBingoSet' && name !== 'RequestExistingBingoSet') throw new Error('Unexpected invocation');
    if (args.length !== 2 || args.some(x => typeof x !== 'string')) throw new Error('Invocation argument count/types do not match the hub');
    if (mode === 'hub-error') {this.emit('Error', 'Backend failed but invocation completed'); return;}
    const [clientId, userName] = args;
    let value = boards.get(clientId), event = 'ExistingBingoSetReceived', notify = 'ClientBingoSetUpdated';
    if (name === 'RequestBingoSet' || !value) {
      value = makeBoard(clientId); boards.set(clientId, value);
      event = 'BingoSetReceived'; notify = 'ClientBingoSetGenerated';
    } else if (mode === 'fresh-on-return') {
      event = 'BingoSetReceived';
    } else if (mode === 'lost-persistence') {
      value = makeBoard(clientId);
    }
    this.clientId = clientId; this.boardId = value.id;
    this.emit(event, value);
    for (const c of connections.values()) if (c !== this) c.emit(notify, {
      connectionId:this.id, bingoSetId:value.id, userName
    });
  }
}
class HubConnectionBuilder {
  withUrl() {return this;}
  withAutomaticReconnect() {return this;}
  configureLogging() {return this;}
  build() {return new Connection();}
}
exports.HubConnectionBuilder = HubConnectionBuilder;
exports.LogLevel = {None:0, Information:2};
exports.HttpTransportType = {WebSockets:1};
"""


@unittest.skipUnless(shutil.which("node"), "Node is required for player-module behavioral fixtures")
class PlayerRuntimeTests(SnapshotCase):
    def probe(self, mode="healthy", service=None):
        module = self.player / "node_modules/@microsoft/signalr"
        write_json(module / "package.json", {"name": "@microsoft/signalr", "main": "index.cjs"})
        write(module / "index.cjs", FAKE_SIGNALR.replace("__MODE__", json.dumps(mode)))
        if service is not None:
            write(self.player / "services/signalrService.js", service)
        return verify.check_signalr(self.source, "http://localhost:12345", "http://localhost:12346", 0.15)

    def test_actual_candidate_module_passes_fresh_returning_new_board_and_live_update(self):
        result = self.probe()
        self.assertEqual({name: item["status"] for name, item in result.items()},
                         dict.fromkeys(verify.PLAYER_CHECKS, "pass"))

    def test_seeded_object_payload_fails_despite_healthy_transport(self):
        result = self.probe(service=seeded_player())
        self.assertEqual(result["signalr_fresh"]["status"], "fail")
        self.assertIn("argument", result["signalr_fresh"]["detail"])

    def test_fix_is_behavioral_not_a_source_string(self):
        service = healthy_player().replace(
            "this.connection.invoke('RequestBingoSet', persistentClientId, userName)",
            "this.connection.invoke('RequestBingoSet', ...[persistentClientId, userName])").replace(
            "this.connection.invoke('RequestExistingBingoSet', persistentClientId, userName)",
            "this.connection.invoke('RequestExistingBingoSet', ...[persistentClientId, userName])")
        self.assertTrue(all(r["status"] == "pass" for r in self.probe(service=service).values()))

    def test_both_request_methods_must_be_repaired(self):
        service = healthy_player().replace(
            "invoke('RequestExistingBingoSet', persistentClientId, userName)",
            "invoke('RequestExistingBingoSet', {clientId:persistentClientId, userName})")
        result = self.probe(service=service)
        self.assertEqual(result["signalr_fresh"]["status"], "pass")
        self.assertEqual(result["signalr_returning"]["status"], "fail")
        self.assertEqual(result["signalr_new_board"]["status"], "fail")

    def test_swallowed_hub_errors_empty_boards_and_wrong_return_events_fail(self):
        for mode, name in (("hub-error", "signalr_fresh"), ("empty-board", "signalr_fresh"),
                           ("fresh-on-return", "signalr_returning"),
                           ("lost-persistence", "signalr_returning"), ("admin-empty", "admin_player")):
            with self.subTest(mode=mode):
                self.assertEqual(self.probe(mode)[name]["status"], "fail")

    def test_live_update_requires_event_and_persisted_square_not_invocation_success(self):
        for mode in ("live-event-only", "live-no-event"):
            with self.subTest(mode=mode):
                result = self.probe(mode)
                self.assertEqual(result["signalr_fresh"]["status"], "pass")
                self.assertEqual(result["live_update"]["status"], "fail")

    def test_removed_request_behavior_and_missing_dependency_are_not_success(self):
        service = healthy_player().replace(
            "await this.connection.invoke('RequestBingoSet', persistentClientId, userName)", "await Promise.resolve()")
        self.assertEqual(self.probe(service=service)["signalr_fresh"]["status"], "fail")
        shutil.rmtree(self.player / "node_modules")
        result = verify.check_signalr(self.source, "http://localhost:12345", "http://localhost:12346", 0.15)
        self.assertTrue(all(item["status"] == "unknown" for item in result.values()))


@unittest.skipUnless(os.environ.get("BINGO_VERIFY_LIVE") == "1", "Full runtime fixture is opt-in")
class LiveApplicationTests(unittest.TestCase):
    def test_healthy_application_then_seeded_player_payload(self):
        for tool in ("docker", "dotnet", "node", "npm", "git"):
            self.assertIsNotNone(shutil.which(tool), "Live fixture requires " + tool)
        with tempfile.TemporaryDirectory(prefix="bingo-verifier-live-") as directory:
            root = Path(directory)
            archive = root / "healthy.tar"
            with archive.open("wb") as stream:
                subprocess.run(["git", "archive", "c902c52", "demo/start"], cwd=REPO,
                               stdout=stream, check=True, timeout=30)
            workspace = root / "candidate"
            workspace.mkdir()
            with tarfile.open(archive) as stream:
                stream.extractall(workspace, filter="data")
            write_json(workspace / "global.json", {
                "sdk": {"version": "10.0.100", "rollForward": "latestFeature"}})
            baseline = root / "baseline"
            shutil.copytree(workspace, baseline)
            start = workspace / "demo/start"
            source = start / "src"
            player = source / "bingo-board"
            containers, processes, logs = [], [], []
            env = {**os.environ, "DOTNET_CLI_USE_MSBUILD_SERVER": "0",
                   "DOTNET_CLI_TELEMETRY_OPTOUT": "1"}

            def run(args, timeout=120, cwd=None, environment=None):
                completed = subprocess.run(args, cwd=cwd, env=environment or env,
                                           capture_output=True, text=True, timeout=timeout)
                self.assertEqual(completed.returncode, 0,
                                 "Fixture command failed: " + args[0] + "\n" +
                                 (completed.stderr or completed.stdout)[-3000:])
                return completed.stdout.strip()

            def create_container(image, port, *arguments):
                container_id = run(["docker", "run", "--detach", "--label",
                                    "bingo-verifier-fixture=" + str(root),
                                    "-p", "127.0.0.1::" + str(port), *arguments, image])
                self.assertRegex(container_id, r"^[a-f0-9]{64}$")
                containers.append(container_id)
                mapping = json.loads(run(["docker", "inspect", "--type", "container", container_id]))
                return container_id, mapping[0]["NetworkSettings"]["Ports"][str(port) + "/tcp"][0]["HostPort"]

            def available_port():
                with socket.socket() as listener:
                    listener.bind(("127.0.0.1", 0))
                    return listener.getsockname()[1]

            def spawn(args, cwd, environment, name):
                log = (root / (name + ".log")).open("w", encoding="utf-8")
                logs.append(log)
                process = subprocess.Popen(args, cwd=cwd, env=environment, stdout=log,
                                           stderr=subprocess.STDOUT)
                processes.append(process)
                return process

            def wait_http(url, process):
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        self.fail("Fresh fixture process exited before readiness; logs are in " + str(root))
                    try:
                        if verify.http_get(url, 1)[0] == 200:
                            return
                    except OSError:
                        pass
                    time.sleep(0.1)
                self.fail("Fresh fixture HTTP readiness deadline exceeded")

            try:
                postgres, db_port = create_container(
                    "postgres:18", 5432, "-e", "POSTGRES_DB=bingo",
                    "-e", "POSTGRES_USER=postgres", "-e", "POSTGRES_PASSWORD=postgres")
                redis, cache_port = create_container("redis:8", 6379)
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    ready = subprocess.run(
                        ["docker", "exec", postgres, "pg_isready", "-U", "postgres", "-d", "bingo"],
                        capture_output=True, timeout=5)
                    if ready.returncode == 0:
                        break
                    time.sleep(0.1)
                else:
                    self.fail("Fresh Postgres fixture did not become ready")
                run(["dotnet", "build", "AspireifyBingo.slnx", "--nologo", "-v:q",
                     "--disable-build-servers", "/p:UseSharedCompilation=false"],
                    timeout=240, cwd=start)
                app_env = {**env, "ConnectionStrings__db":
                           "Host=127.0.0.1;Port=" + db_port +
                           ";Database=bingo;Username=postgres;Password=postgres",
                           "ConnectionStrings__cache": "127.0.0.1:" + cache_port,
                           "Authentication__AdminPassword": "Fixture-" + uuid.uuid4().hex + "!1",
                           "ASPNETCORE_ENVIRONMENT": "Development"}
                migration_dll = source / "BingoBoard.MigrationService/bin/Debug/net10.0/BingoBoard.MigrationService.dll"
                migration = subprocess.run(["dotnet", str(migration_dll)], env=app_env,
                                           capture_output=True, text=True, timeout=60,
                                           cwd=migration_dll.parent)
                write(root / "migration.log", migration.stdout + migration.stderr)
                self.assertEqual(migration.returncode, 0, "Fresh fixture migrations failed")
                admin_url = "http://127.0.0.1:" + str(available_port())
                admin_dll = source / "BingoBoard.Admin/bin/Debug/net10.0/BingoBoard.Admin.dll"
                admin = spawn(["dotnet", str(admin_dll)], admin_dll.parent,
                              {**app_env, "ASPNETCORE_URLS": admin_url}, "admin")
                wait_http(admin_url + "/api/version-info", admin)
                # The first probe confirms dependencies really are absent before restoration.
                absent = verify.check_signalr(source, admin_url, admin_url, 0.1)
                self.assertEqual(absent["signalr_fresh"]["status"], "unknown")
                run(["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=player, timeout=180)
                frontend_url = "http://127.0.0.1:" + str(available_port())
                frontend = spawn(["node", "node_modules/vite/bin/vite.js", "--host", "127.0.0.1",
                                  "--port", frontend_url.rsplit(":", 1)[1]],
                                 player, {**env, "BINGO_ADMIN_URL": admin_url}, "frontend")
                wait_http(frontend_url + "/", frontend)
                metadata_path = write_json(root / "runtime.json", {
                    "run_id": root.name, "workspace": str(workspace), "variant": "raw",
                    "container_runtime": "docker", "frontend_url": frontend_url, "admin_url": admin_url,
                    "redis": {"container_id": redis},
                    "postgres": {"container_id": postgres, "database": "bingo", "user": "postgres"},
                    "migrations": {"state": "Exited", "exit_code": migration.returncode,
                                   "log_path": str(root / "migration.log")},
                })
                write_json(root / "diagnosis.json", {"diagnoses": []})
                args = argparse.Namespace(
                    workspace=str(workspace), variant="raw", apphost=None,
                    frontend_url=frontend_url, admin_url=admin_url,
                    diagnosis=str(root / "diagnosis.json"), output=str(root / "result.json"),
                    runtime_metadata=str(metadata_path), baseline=str(baseline),
                    timeout=3, container_runtime="docker")
                result = verify.verify(args)
                self.assertTrue(result["repair_success"], json.dumps(result["checks"], indent=2))
                self.assertFalse(result["diagnosis_success"])
                candidate_service = player / "services/signalrService.js"
                candidate_service.write_text(candidate_service.read_text().replace(
                    "persistentClientId, userName)", "{ clientId: persistentClientId, userName })"))
                result = verify.verify(args)
                self.assertFalse(result["repair_success"])
                self.assertEqual(result["checks"]["contracts"]["status"], "pass")
                self.assertEqual(result["checks"]["signalr_fresh"]["status"], "fail")
                self.assertEqual(result["checks"]["redis"]["status"], "pass")
                self.assertEqual(result["checks"]["postgres"]["status"], "pass")
            finally:
                for process in reversed(processes):
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
                for log in logs:
                    log.close()
                for container_id in reversed(containers):
                    subprocess.run(["docker", "rm", "--force", "--volumes", container_id],
                                   capture_output=True, timeout=30, check=True)


if __name__ == "__main__":
    unittest.main()
