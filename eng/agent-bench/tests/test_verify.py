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
import sys
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

    def owned_metadata(self, frontend, admin=None):
        admin = admin or frontend
        endpoints = {}
        for name, url in (("frontend", frontend), ("admin", admin)):
            parsed = verify.urllib.parse.urlsplit(url)
            endpoints[name] = {"url": url, "port": parsed.port or 80,
                               "run_id": "fixture", "preexisting": False,
                               "listener_processes": [{"pid": 123, "started": "fixture-start"}]}
        return self.metadata(
            redis={"container_id": CONTAINER_ID},
            postgres={"container_id": CONTAINER_ID, "user": "postgres", "database": "bingo"},
            ownership={"validated": True, "run_id": "fixture", "errors": [], "endpoints": endpoints,
                       "containers": {service: {"container_id": CONTAINER_ID, "name": "fixture-" + service,
                                               "run_id": "fixture", "preexisting": False, "validated": True}
                                      for service in ("redis", "postgres")}})


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

    def test_correct_root_path_not_exclusive_service_vocabulary_is_required(self):
        document = self.answer()
        document["diagnoses"][1]["candidates"][0]["file"] = "demo/start/src/BingoBoard.Admin/Program.cs"
        self.assertEqual(self.score(document)["faults"]["CONFIG-01"]["status"], "fail")
        document = self.answer()
        document["diagnoses"][0]["service"] = "postgres"
        self.assertEqual(self.score(document)["faults"]["HEALTH-01"]["status"], "pass")

    def test_specifics_in_evidence_support_a_concise_causal_conclusion(self):
        document = self.answer()
        document["diagnoses"][0]["candidates"][0].update(
            cause="Misspelled Redis eviction policy prevented startup.",
            evidence="Redis exited with FATAL CONFIG FILE ERROR for allkeys-lfr; allkeys-lru then passed PING.")
        document["diagnoses"][1]["candidates"][0].update(
            cause="Migration worker requested a connection-string name different from the supplied database resource.",
            evidence="Connection string 'database' is required; changed lookup to 'db', exit 0 and 40 seeded squares.")
        document["diagnoses"][2]["candidates"][0].update(
            cause="Backend did not expose the version endpoint used by the player and checks.",
            evidence="Direct and proxy /api/version-info returned HTTP404 while /api/version worked; corrected route returns JSON.")
        document["diagnoses"][3]["candidates"][0].update(
            cause="Frontend passed a single object to hub methods expecting separate clientId and userName arguments.",
            evidence="Original board invocation failed with no board; after fixing both request methods a 25-square board arrived.")
        self.assertTrue(self.score(document)["success"])

    def test_distinct_faults_with_overlapping_service_labels_do_not_merge(self):
        document = self.answer()
        document["diagnoses"][1]["service"] = "Database migration and admin startup"
        document["diagnoses"][2]["service"] = "Player frontend version-info proxy and API"
        document["diagnoses"][3]["service"] = "Player board requests and live updates / admin"
        result = self.score(document)
        self.assertTrue(result["success"])
        self.assertEqual([r["candidate_count"] for r in result["faults"].values()], [1] * 4)

    def test_distinct_extra_footer_and_lifecycle_findings_are_not_seeded_candidates(self):
        document = self.answer()
        document["diagnoses"].append({"service": "Player version footer", "candidates": [{
            "cause": "Frontend used incorrect casing for runtime-version JSON field.",
            "file": "demo/start/src/bingo-board/App.vue",
            "evidence": "/api/version-info JSON returns dotNetVersion, footer used dotnetVersion; display now correct."}]})
        document["diagnoses"].append({"service": "Workflow verification", "candidates": [{
            "cause": "No source cause. Container name was in use after restart, not a defect in application source.",
            "file": "demo/start/compose.yaml",
            "evidence": "A container-name race on restart was resolved; resources became healthy."}]})
        result = self.score(document)
        self.assertTrue(result["success"])
        self.assertEqual(result["faults"]["HEALTH-01"]["candidate_count"], 1)
        self.assertEqual(result["faults"]["INTEROP-02"]["candidate_count"], 1)

    def test_multiple_known_roots_in_one_row_still_group_by_category(self):
        document = self.answer()
        candidate = document["diagnoses"].pop(3)["candidates"][0]
        document["diagnoses"][2]["candidates"].append(candidate)
        self.assertTrue(self.score(document)["success"])

    def test_same_fault_alternatives_in_same_or_separate_rows_remain_unresolved(self):
        for split in (True, False):
            document = self.answer()
            alternative = {"cause": "Maybe the Redis port is wrong",
                           "file": "demo/start/compose.yaml", "evidence": "Connection failed"}
            if split:
                document["diagnoses"].append({"service": "A different label", "candidates": [alternative]})
            else:
                document["diagnoses"][0]["candidates"].append(alternative)
            with self.subTest(split=split):
                self.assertEqual(self.score(document)["faults"]["HEALTH-01"]["candidate_count"], 2)

    def test_wrong_conclusions_cannot_be_rescued_by_quoted_correct_evidence(self):
        for index, cause in (
                (0, "A Redis port conflict caused startup failure."),
                (0, "Maybe Redis has an invalid allkeys-lfr policy or a port conflict."),
                (0, "The Redis policy allkeys-lru is invalid."),
                (0, "Redis allkeys-lfr is a supported policy and works correctly."),
                (1, "The worker reads db while startup supplies database."),
                (2, "The endpoint works correctly; there is no route fault."),
                (2, "Backend exposes /api/version-info but frontend requests /api/version."),
                (3, "The SignalR payload works correctly; hub is not the cause.")):
            document = self.answer()
            document["diagnoses"][index]["candidates"][0]["cause"] = cause
            with self.subTest(cause=cause):
                self.assertEqual(self.score(document)["faults"][verify.FAULTS[index]]["status"], "fail")

    def test_wrong_path_and_missing_observations_remain_failures(self):
        for index in range(4):
            document = self.answer()
            document["diagnoses"][index]["candidates"][0]["file"] = "demo/start/src/bingo-board/App.vue"
            self.assertEqual(self.score(document)["faults"][verify.FAULTS[index]]["status"], "fail")
            document = self.answer()
            document["diagnoses"][index]["candidates"][0]["evidence"] = "I think this is fixed"
            self.assertEqual(self.score(document)["faults"][verify.FAULTS[index]]["status"], "fail")

    def test_trailing_prose_and_fenced_json_remain_malformed(self):
        for content in (json.dumps(self.answer()) + "\nI fixed the app",
                        "```json\n" + json.dumps(self.answer()) + "\n```"):
            write(self.diagnosis_path, content)
            result = verify.score_diagnosis(
                self.diagnosis_path, self.workspace, self.source, None, "raw")
            self.assertFalse(result["success"])

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

    def test_ownership_requires_complete_exact_trial_evidence_not_a_boolean(self):
        frontend, admin = "http://localhost:5001", "http://localhost:5000"
        good = self.owned_metadata(frontend, admin)
        self.assertEqual(verify.check_ownership(good, frontend, admin)["status"], "pass")
        broken = []
        for ownership in (None, {"validated": True},
                          {**good["ownership"], "run_id": "another-run"},
                          {**good["ownership"], "errors": ["preexisting listener"]},
                          {**good["ownership"], "validated": False}):
            broken.append({**good, "ownership": ownership})
        for field, value in (("url", "http://localhost:5039"), ("port", 5039),
                             ("listener_processes", []),
                             ("listener_processes", [{"pid": 123}]),
                             ("listener_processes", [{"pid": True, "started": "fixture"}])):
            metadata = copy.deepcopy(good)
            metadata["ownership"]["endpoints"]["admin"][field] = value
            broken.append(metadata)
        metadata = copy.deepcopy(good)
        metadata["ownership"]["containers"]["redis"]["container_id"] = "b" * 64
        broken.append(metadata)
        for metadata in broken:
            with self.subTest(metadata=metadata):
                self.assertEqual(verify.check_ownership(metadata, frontend, admin)["status"], "unknown")

    def test_ownership_rejects_missing_or_invalid_per_resource_freshness_and_run_identity(self):
        frontend, admin = "http://localhost:5001", "http://localhost:5000"
        good = self.owned_metadata(frontend, admin)
        for group, service, field, bad_values in (
                ("endpoints", "admin", "preexisting", (None, True, 0, "")),
                ("endpoints", "frontend", "run_id", (None, "another-run", "")),
                ("containers", "redis", "preexisting", (None, True, 0, "")),
                ("containers", "postgres", "run_id", (None, "another-run", "")),
                ("containers", "postgres", "validated", (None, False, 1, "true")),
                ("containers", "redis", "name", (None, "", " ", 123))):
            for value in bad_values:
                metadata = copy.deepcopy(good)
                item = metadata["ownership"][group][service]
                if value is None:
                    del item[field]
                else:
                    item[field] = value
                with self.subTest(group=group, service=service, field=field, value=value):
                    self.assertEqual(verify.check_ownership(metadata, frontend, admin)["status"], "unknown")

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
                             ["docker", "exec", CONTAINER_ID, "sh", "-c",
                              verify.AUTH_SCRIPTS["redis"], "bingo-verifier-auth",
                              "redis-cli", "--raw", "PING"])
        for inspect, ping in ((self.inspection(), "not-PONG\n"),
                              (self.inspection(RestartCount=1), "PONG\n"),
                              (self.inspection(Id="b" * 64), "PONG\n"),
                              (self.inspection(State={"Running": False}), "PONG\n"),
                              (self.inspection(State={"Running": True, "Health": {"Status": "unhealthy"}}), "PONG\n")):
            with patch.object(verify, "command", side_effect=[inspect, ping]):
                self.assertEqual(verify.check_redis(metadata, "docker", 1, 0)["status"], "fail")

    def test_redis_missing_or_rejected_authentication_is_unknown(self):
        for ping in ("NOAUTH Authentication required.\n", "WRONGPASS invalid password\n",
                     "AUTH failed: WRONGPASS\nPONG\n"):
            with patch.object(verify, "command", side_effect=[self.inspection(), ping]):
                result = verify.check_redis({"redis": {"container_id": CONTAINER_ID}}, "docker", 1)
            self.assertEqual(result["status"], "unknown")
            self.assertNotIn(ping.strip(), result["detail"])

    def test_postgres_connection_auth_failure_is_unknown_but_schema_failure_is_not(self):
        metadata = {"postgres": {"container_id": CONTAINER_ID, "user": "postgres", "database": "db"}}
        for code, expected in ((2, "unknown"), (1, "fail")):
            with patch.object(verify, "command", side_effect=[
                    self.inspection(), "ready",
                    verify.ProbeError("fail", "Runtime probe exited with code " + str(code), exit_code=code)]):
                self.assertEqual(verify.check_postgres(metadata, "docker", 1)["status"], expected)

    def test_container_local_credentials_are_quoted_and_not_in_host_argv(self):
        secret = "synthetic ' $(not-executed); $HOME\nvalue"
        for service, source, destination in (
                ("redis", "REDIS_PASSWORD", "REDISCLI_AUTH"),
                ("postgres", "POSTGRES_PASSWORD", "PGPASSWORD")):
            for value in (secret, ""):
                environment = {**os.environ, source: value}
                environment.pop(destination, None)
                script = ("import os,sys; "
                          "print(os.environ.get(sys.argv[1], '') == os.environ.get(sys.argv[2], '')); "
                          "print(sys.argv[3] == \"literal '$() argument\")")
                argv = [sys.executable, "-c", script, destination, source, "literal '$() argument"]

                def local_container_command(args, timeout):
                    self.assertEqual(args[:5], ["docker", "exec", CONTAINER_ID, "sh", "-c"])
                    self.assertNotIn(secret, args)
                    return subprocess.run(args[3:], env=environment, capture_output=True,
                                          text=True, timeout=timeout, check=True).stdout

                with self.subTest(service=service, authenticated=bool(value)), \
                        patch.object(verify, "command", side_effect=local_container_command):
                    self.assertEqual(verify.authenticated_command(
                        "docker", CONTAINER_ID, service, argv, 1), "True\nTrue\n")

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
            argv = call.call_args_list[2].args[0]
            self.assertEqual(argv[:5], ["docker", "exec", CONTAINER_ID, "sh", "-c"])
            self.assertEqual(argv[5], verify.AUTH_SCRIPTS["postgres"])
            self.assertEqual(argv[7:12], ["psql", "-w", "-h", "127.0.0.1", "-X"])
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

    def test_exact_provider_backed_version_alias_is_additive_and_preserves_contracts(self):
        path = Path("BingoBoard.Admin/Program.cs")
        healthy = (REPO / "demo/start/src" / path).read_text().replace(
            'app.MapGet("/api/version",', 'app.MapGet("/api/version-info",')
        mapping = ('app.MapGet("/api/version-info", (AppVersionInfoProvider versionInfoProvider)'
                   ' => versionInfoProvider.GetVersionInfo());')
        alias = mapping.replace('"/api/version-info"', '"/api/version"')
        write(self.baseline / "demo/start/src" / path, healthy)
        for replacement in (alias + "\n" + mapping, mapping + "\n" + alias):
            write(self.source / path, healthy.replace(mapping, replacement))
            with self.subTest(alias_order=replacement):
                self.assertEqual(self.check()["status"], "pass")

    def test_version_alias_exception_does_not_permit_weakened_or_fake_handlers(self):
        path = Path("BingoBoard.Admin/Program.cs")
        healthy = (REPO / "demo/start/src" / path).read_text().replace(
            'app.MapGet("/api/version",', 'app.MapGet("/api/version-info",')
        mapping = ('app.MapGet("/api/version-info", (AppVersionInfoProvider versionInfoProvider)'
                   ' => versionInfoProvider.GetVersionInfo());')
        alias = mapping.replace('"/api/version-info"', '"/api/version"')
        write(self.baseline / "demo/start/src" / path, healthy)
        cases = {
            "seeded-only": healthy.replace(mapping, alias),
            "fake-restored-handler": healthy.replace(
                mapping, alias + '\napp.MapGet("/api/version-info", () => new { success = true });'),
            "changed-alias-handler": healthy.replace(
                mapping, 'app.MapGet("/api/version", () => new { success = true });\n' + mapping),
            "conditional-restored": healthy.replace(mapping, alias + "\nif (false) { " + mapping + " }"),
            "conditional-alias": healthy.replace(mapping, mapping + "\nif (true) { " + alias + " }"),
            "duplicate-alias": healthy.replace(mapping, alias + "\n" + alias + "\n" + mapping),
            "duplicate-restored": healthy.replace(mapping, alias + "\n" + mapping + "\n" + mapping),
            "different-alias-path": healthy.replace(mapping, alias.replace('"/api/version"', '"/api/other"') + "\n" + mapping),
            "removed-authentication": healthy.replace(
                mapping, alias + "\n" + mapping).replace("app.UseAuthentication();", ""),
            "removed-health": healthy.replace(
                mapping, alias + "\n" + mapping).replace("app.MapDefaultEndpoints();", ""),
            "changed-provider": healthy.replace(
                mapping, alias + "\n" + mapping).replace("GetVersionInfo()", "GetFakeVersionInfo()"),
        }
        for name, candidate in cases.items():
            write(self.source / path, candidate)
            with self.subTest(case=name):
                self.assertEqual(self.check()["status"], "fail")

    def test_alias_permission_requires_the_exact_healthy_baseline_mapping(self):
        mapping = ('app.MapGet("/api/version-info", (AppVersionInfoProvider versionInfoProvider)'
                   ' => versionInfoProvider.GetVersionInfo());')
        alias = mapping.replace('"/api/version-info"', '"/api/version"')
        self.assertFalse(verify.admin_program_contract(alias, alias + mapping))
        self.assertFalse(verify.admin_program_contract(mapping + mapping, alias + mapping + mapping))

    def test_shared_provider_handler_preserves_original_mapping_with_optional_alias(self):
        mapping = ('app.MapGet("/api/version-info", (AppVersionInfoProvider versionInfoProvider)'
                   ' => versionInfoProvider.GetVersionInfo());')
        declaration = ('var versionInfoHandler = (AppVersionInfoProvider versionInfoProvider)'
                       ' => versionInfoProvider.GetVersionInfo();')
        restored = 'app.MapGet("/api/version-info", versionInfoHandler);'
        alias = 'app.MapGet("/api/version", versionInfoHandler);'
        healthy = (REPO / "demo/start/src/BingoBoard.Admin/Program.cs").read_text().replace(
            'app.MapGet("/api/version",', 'app.MapGet("/api/version-info",')
        write(self.baseline / "demo/start/src/BingoBoard.Admin/Program.cs", healthy)
        for mappings in (restored, alias + "\n" + restored, restored + "\n" + alias):
            candidate = healthy.replace(mapping, declaration + "\n" + mappings)
            write(self.source / "BingoBoard.Admin/Program.cs", candidate)
            with self.subTest(mappings=mappings):
                self.assertEqual(self.check()["status"], "pass")

    def test_shared_handler_does_not_permit_changed_binding_control_flow_or_safety(self):
        mapping = ('app.MapGet("/api/version-info", (AppVersionInfoProvider versionInfoProvider)'
                   ' => versionInfoProvider.GetVersionInfo());')
        declaration = ('var versionInfoHandler = (AppVersionInfoProvider versionInfoProvider)'
                       ' => versionInfoProvider.GetVersionInfo();')
        restored = 'app.MapGet("/api/version-info", versionInfoHandler);'
        alias = 'app.MapGet("/api/version", versionInfoHandler);'
        healthy = "app.UseAuthentication();\n" + mapping + "\napp.MapDefaultEndpoints();"
        equivalent = declaration + "\n" + alias + "\n" + restored
        for replacement in (
                equivalent.replace("GetVersionInfo()", "GetFakeVersionInfo()"),
                equivalent.replace("AppVersionInfoProvider", "FakeVersionInfoProvider"),
                equivalent.replace(restored, 'app.MapGet("/api/version-info", otherHandler);'),
                equivalent.replace(alias, 'app.MapGet("/api/version", otherHandler);'),
                equivalent + "\n" + restored,
                declaration + "\n" + alias,
                declaration + "\nif (false) { " + alias + restored + " }",
                declaration + "\nversionInfoHandler = otherHandler;\n" + alias + restored,
                equivalent.replace('"/api/version"', '"/api/other"'),
                equivalent + "\napp.Use(otherMiddleware);",
                equivalent.replace("=> versionInfoProvider.GetVersionInfo()", "=> new { success = true }")):
            with self.subTest(replacement=replacement):
                self.assertFalse(verify.admin_program_contract(healthy, healthy.replace(mapping, replacement)))
        for removed in ("app.UseAuthentication();", "app.MapDefaultEndpoints();"):
            self.assertFalse(verify.admin_program_contract(
                healthy, healthy.replace(mapping, equivalent).replace(removed, "")))
        self.assertFalse(verify.admin_program_contract(mapping + mapping, equivalent + mapping))

    def test_script_changes_still_fail_even_when_shared_version_handler_is_equivalent(self):
        mapping = ('app.MapGet("/api/version-info", (AppVersionInfoProvider versionInfoProvider)'
                   ' => versionInfoProvider.GetVersionInfo());')
        equivalent = ('var versionInfoHandler = (AppVersionInfoProvider versionInfoProvider)'
                      ' => versionInfoProvider.GetVersionInfo();\n'
                      'app.MapGet("/api/version", versionInfoHandler);\n'
                      'app.MapGet("/api/version-info", versionInfoHandler);')
        write(self.baseline / "demo/start/src/BingoBoard.Admin/Program.cs", mapping)
        write(self.source / "BingoBoard.Admin/Program.cs", equivalent)
        for name in ("common.sh", "common.ps1"):
            original = (REPO / "demo/start/scripts" / name).read_text()
            write(self.baseline / "demo/start/scripts" / name, original)
            write(self.source.parent / "scripts" / name, original)
            self.assertEqual(self.check()["status"], "pass")
            changed = (original.replace('    "$RUNTIME" compose version >/dev/null\n',
                                        '    docker-compose version >/dev/null\n')
                       if name == "common.sh" else original.replace(
                           "    Invoke-Native $Runtime @('compose', 'version') | Out-Null\n",
                           "    Invoke-Native 'docker-compose' @('version') | Out-Null\n"))
            write(self.source.parent / "scripts" / name, changed)
            result = self.check()
            self.assertEqual(result["status"], "fail")
            self.assertEqual(result["violations"], ["scripts/" + name + " readiness/lifecycle contract changed"])
            write(self.source.parent / "scripts" / name, original)

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

    def test_missing_ownership_is_unknown_not_agent_claim_success(self):
        # Closing our own allocated listener yields a known unused fixture endpoint.
        with HttpFixture() as url:
            pass
        write_json(self.diagnosis_path, self.answer())
        result = verify.verify(self.args(url))
        self.assertFalse(result["repair_success"])
        self.assertTrue(result["diagnosis_success"])
        self.assertEqual(result["checks"]["frontend"]["status"], "unknown")
        self.assertEqual(result["checks"]["redis"]["status"], "unknown")
        self.assertEqual(result["checks"]["signalr_fresh"]["status"], "unknown")

    def test_unowned_stale_endpoint_has_no_network_container_or_signalr_traffic(self):
        write_json(self.diagnosis_path, self.answer())
        metadata = self.owned_metadata("http://localhost:5001", "http://localhost:5039")
        metadata["ownership"]["validated"] = False
        path = write_json(self.root / "runtime.json", metadata)
        args = self.args("http://localhost:5001", "http://localhost:5039")
        args.runtime_metadata = str(path)
        with patch.object(verify, "http_get") as http, \
                patch.object(verify, "command") as command, \
                patch.object(verify, "check_signalr") as signalr:
            result = verify.verify(args)
            http.assert_not_called()
            command.assert_not_called()
            signalr.assert_not_called()
        self.assertEqual(result["repair"]["status"], "unknown")
        self.assertTrue(result["diagnosis_success"])

    def test_preexisting_endpoint_or_unvalidated_container_blocks_all_runtime_traffic(self):
        write_json(self.diagnosis_path, self.answer())
        for group, service, field, value in (
                ("endpoints", "admin", "preexisting", True),
                ("containers", "postgres", "validated", False)):
            metadata = self.owned_metadata("http://localhost:5001", "http://localhost:5039")
            metadata["ownership"][group][service][field] = value
            args = self.args("http://localhost:5001", "http://localhost:5039")
            args.runtime_metadata = str(write_json(self.root / "runtime.json", metadata))
            with self.subTest(group=group), patch.object(verify, "http_get") as http, \
                    patch.object(verify, "command") as command, \
                    patch.object(verify, "check_signalr") as signalr:
                result = verify.verify(args)
                http.assert_not_called()
                command.assert_not_called()
                signalr.assert_not_called()
            self.assertEqual(result["repair"]["status"], "unknown")
            self.assertTrue(result["diagnosis_success"])

    def test_owned_but_unreachable_runtime_is_explicitly_failed(self):
        with HttpFixture() as url:
            pass
        write_json(self.diagnosis_path, self.answer())
        metadata_path = write_json(self.root / "runtime.json", self.owned_metadata(url))
        args = self.args(url)
        args.runtime_metadata = str(metadata_path)
        with patch.object(verify, "check_postgres", return_value=verify.outcome("unknown", "fixture")), \
                patch.object(verify, "check_redis", return_value=verify.outcome("unknown", "fixture")), \
                patch.object(verify, "check_signalr", return_value={
                    name: verify.outcome("fail", "unreachable fixture") for name in verify.PLAYER_CHECKS}):
            self.assertEqual(verify.verify(args)["checks"]["frontend"]["status"], "fail")

    def test_successful_runtime_is_separate_from_malformed_diagnosis(self):
        write_json(self.diagnosis_path, {"claim": "all fixed"})
        passed = verify.outcome("pass", "fixture runtime observation")
        with HttpFixture() as url, patch.object(verify, "check_postgres", return_value=passed), \
                patch.object(verify, "check_migrations", return_value=passed), \
                patch.object(verify, "check_redis", return_value={**passed, "restart_count": 0}), \
                patch.object(verify, "check_signalr", return_value={name: passed for name in verify.PLAYER_CHECKS}):
            args = self.args(url)
            metadata_path = write_json(self.root / "runtime.json", self.owned_metadata(url))
            args.runtime_metadata = str(metadata_path)
            result = verify.verify(args)
        self.assertTrue(result["repair_success"])
        self.assertFalse(result["diagnosis_success"])

    def test_direct_and_proxy_versions_must_agree(self):
        write_json(self.diagnosis_path, self.answer())
        with HttpFixture() as admin, HttpFixture(version={**VERSION, "dotNetVersion": "different"}) as frontend:
            args = self.args(frontend, admin)
            args.runtime_metadata = str(write_json(
                self.root / "runtime.json", self.owned_metadata(frontend, admin)))
            with patch.object(verify, "check_postgres", return_value=verify.outcome("unknown", "fixture")), \
                    patch.object(verify, "check_redis", return_value=verify.outcome("unknown", "fixture")), \
                    patch.object(verify, "check_signalr", return_value={
                        name: verify.outcome("unknown", "fixture") for name in verify.PLAYER_CHECKS}):
                result = verify.verify(args)
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

    def test_runtime_and_preservation_success_are_separate_and_repair_requires_both(self):
        runtime_names = ("workspace", "runtime_metadata", "ownership", "postgres", "migrations",
                         "redis", "frontend", "version_direct", "version_proxy", *verify.PLAYER_CHECKS)
        for runtime_status in ("pass", "fail", "unknown"):
            for contract_status in ("pass", "fail", "unknown"):
                checks = {name: verify.outcome(runtime_status, "fixture") for name in runtime_names}
                checks["contracts"] = verify.outcome(contract_status, "fixture")
                diagnosis = {"success": False, "status": "fail", "faults": {}}
                result = verify.build_result("typescript", checks, diagnosis)
                with self.subTest(runtime=runtime_status, contracts=contract_status):
                    self.assertEqual(result["oracle_version"], "5")
                    self.assertEqual(result["runtime_workflows_success"], runtime_status == "pass")
                    self.assertEqual(result["contract_preservation_success"], contract_status == "pass")
                    self.assertEqual(result["contract_success"], result["contract_preservation_success"])
                    self.assertEqual(result["repair_success"],
                                     runtime_status == "pass" and contract_status == "pass")
                    self.assertEqual(result["runtime_workflows"]["status"], runtime_status)
                    self.assertEqual(result["contract_preservation"]["status"], contract_status)
                    self.assertFalse(result["diagnosis_success"])

    def test_missing_checks_remain_unknown_in_separate_success_dimensions(self):
        result = verify.build_result("raw", {}, {"success": True, "status": "pass", "faults": {}})
        self.assertFalse(result["runtime_workflows_success"])
        self.assertFalse(result["contract_preservation_success"])
        self.assertFalse(result["contract_success"])
        self.assertFalse(result["repair_success"])
        self.assertTrue(result["diagnosis_success"])
        self.assertEqual(result["runtime_workflows"]["status"], "unknown")
        self.assertEqual(result["contract_preservation"]["status"], "unknown")

    def test_supplemental_diagnosis_only_has_provenance_and_no_runtime_or_candidate_reads(self):
        write_json(self.diagnosis_path, self.answer())
        output = self.root / "supplemental-v5.json"
        args = ["--diagnosis-only", "--workspace", str(self.workspace), "--variant", "raw",
                "--diagnosis", str(self.diagnosis_path), "--output", str(output),
                "--oracle-commit", "a" * 40]
        shutil.rmtree(self.workspace)
        with patch.object(verify, "verify") as runtime, patch.object(verify, "http_get") as http, \
                patch.object(verify, "command") as command, patch.object(verify, "find_source") as source, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(verify.main(args), 0)
            runtime.assert_not_called()
            http.assert_not_called()
            command.assert_not_called()
            source.assert_not_called()
        result = verify.read_json(output)
        self.assertEqual(result["oracle_version"], "5")
        self.assertEqual(result["oracle_commit"], "a" * 40)
        self.assertFalse(result["runtime_evaluated"])
        self.assertNotIn("repair_success", result)
        self.assertEqual(result["diagnosis_input_sha256"],
                         verify.hashlib.sha256(self.diagnosis_path.read_bytes()).hexdigest())
        before = output.read_bytes()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            verify.main(args)
        self.assertEqual(output.read_bytes(), before)

    def test_supplemental_malformed_answer_is_not_prose_extracted(self):
        write(self.diagnosis_path, "```json\n" + json.dumps(self.answer()) + "\n```")
        args = ["--diagnosis-only", "--workspace", str(self.workspace), "--variant", "raw",
                "--diagnosis", str(self.diagnosis_path), "--output", str(self.root / "supplemental.json"),
                "--oracle-commit", "a" * 40]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(verify.main(args), 1)
        result = verify.read_json(self.root / "supplemental.json")
        self.assertFalse(result["diagnosis_success"])
        self.assertEqual(result["diagnosis"]["status"], "fail")


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
        self.run_application_fixture(authenticated=False)

    def test_authenticated_typescript_snapshot_then_seeded_player_payload(self):
        # Exercise the selected TS snapshot and Aspire-style password settings
        # without starting/discovering any existing AppHost or application.
        self.run_application_fixture(authenticated=True)

    def run_application_fixture(self, authenticated):
        for tool in ("docker", "dotnet", "node", "npm", "git", "lsof", "ps"):
            self.assertIsNotNone(shutil.which(tool), "Live fixture requires " + tool)
        with tempfile.TemporaryDirectory(prefix="bingo-verifier-live-") as directory:
            root = Path(directory)
            archive = root / "healthy.tar"
            with archive.open("wb") as stream:
                paths = ["demo/start"]
                if authenticated:
                    paths.append("demo/checkpoints/03-observe/typescript")
                subprocess.run(["git", "archive", "c902c52", *paths], cwd=REPO,
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
            container_names = {}
            env = {**os.environ, "DOTNET_CLI_USE_MSBUILD_SERVER": "0",
                   "DOTNET_CLI_TELEMETRY_OPTOUT": "1"}

            def run(args, timeout=120, cwd=None, environment=None):
                completed = subprocess.run(args, cwd=cwd, env=environment or env,
                                           capture_output=True, text=True, timeout=timeout)
                self.assertEqual(completed.returncode, 0,
                                 "Fixture command failed: " + args[0] + "\n" +
                                 (completed.stderr or completed.stdout)[-3000:])
                return completed.stdout.strip()

            def create_container(image, port, *arguments, container_command=(), environment=None):
                container_id = run(["docker", "run", "--detach", "--label",
                                    "bingo-verifier-fixture=" + str(root),
                                    "-p", "127.0.0.1::" + str(port), *arguments, image,
                                    *container_command], environment=environment)
                self.assertRegex(container_id, r"^[a-f0-9]{64}$")
                containers.append(container_id)
                mapping = json.loads(run(["docker", "inspect", "--type", "container", container_id]))
                container_names[container_id] = mapping[0]["Name"].lstrip("/")
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

            def listener_evidence(url, process):
                port = verify.urllib.parse.urlsplit(url).port
                listeners = run(["lsof", "-nP", "-iTCP:" + str(port), "-sTCP:LISTEN", "-t"])
                self.assertEqual(set(listeners.splitlines()), {str(process.pid)},
                                 "Fixture endpoint must belong exclusively to its registered process")
                started = run(["ps", "-p", str(process.pid), "-o", "lstart="])
                self.assertTrue(started)
                return {"url": url, "port": port, "run_id": root.name, "preexisting": False,
                        "listener_processes": [{"pid": process.pid, "started": started}]}

            try:
                db_password = "Fixture-" + uuid.uuid4().hex + "'$!"
                cache_password = "Fixture-" + uuid.uuid4().hex + "'$!"
                postgres, db_port = create_container(
                    "postgres:18", 5432, "-e", "POSTGRES_DB=bingo",
                    "-e", "POSTGRES_USER=postgres", "-e", "POSTGRES_PASSWORD",
                    environment={**env, "POSTGRES_PASSWORD": db_password})
                if authenticated:
                    redis, cache_port = create_container(
                        "redis:8", 6379, "-e", "REDIS_PASSWORD",
                        environment={**env, "REDIS_PASSWORD": cache_password},
                        container_command=["sh", "-c", 'exec redis-server --requirepass "$REDIS_PASSWORD"'])
                else:
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
                           ";Database=bingo;Username=postgres;Password=\"" + db_password + "\"",
                           "ConnectionStrings__cache": "127.0.0.1:" + cache_port +
                           (",password=" + cache_password if authenticated else ""),
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
                variant = "typescript" if authenticated else "raw"
                apphost = (workspace / "demo/checkpoints/03-observe/typescript/apphost.mts"
                           if authenticated else None)
                metadata_path = write_json(root / "runtime.json", {
                    "run_id": root.name, "workspace": str(workspace), "variant": variant,
                    "container_runtime": "docker", "frontend_url": frontend_url, "admin_url": admin_url,
                    "redis": {"container_id": redis},
                    "postgres": {"container_id": postgres, "database": "bingo", "user": "postgres"},
                    "migrations": {"state": "Exited", "exit_code": migration.returncode,
                                   "log_path": str(root / "migration.log")},
                    "ownership": {
                        "validated": True, "run_id": root.name, "errors": [],
                        "endpoints": {"admin": listener_evidence(admin_url, admin),
                                      "frontend": listener_evidence(frontend_url, frontend)},
                        "containers": {
                            service: {"container_id": container_id, "name": container_names[container_id],
                                      "run_id": root.name, "preexisting": False, "validated": True}
                            for service, container_id in (("redis", redis), ("postgres", postgres))
                        },
                    },
                })
                write_json(root / "diagnosis.json", {"diagnoses": []})
                args = argparse.Namespace(
                    workspace=str(workspace), variant=variant,
                    apphost=str(apphost) if apphost is not None else None,
                    frontend_url=frontend_url, admin_url=admin_url,
                    diagnosis=str(root / "diagnosis.json"), output=str(root / "result.json"),
                    runtime_metadata=str(metadata_path), baseline=str(baseline),
                    timeout=3, container_runtime="docker")
                result = verify.verify(args)
                self.assertTrue(result["repair_success"], json.dumps(result["checks"], indent=2))
                self.assertFalse(result["diagnosis_success"])
                for password in (db_password, cache_password):
                    self.assertNotIn(password, json.dumps(result))
                    self.assertNotIn(password, metadata_path.read_text())
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
