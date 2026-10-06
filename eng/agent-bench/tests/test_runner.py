import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import os
import subprocess
import sys
import contextlib
import io
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("runner", Path(__file__).parents[1] / "runner.py")
r = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(r)


class FixtureTests(unittest.TestCase):
    def test_all_variants_exclude_grader_history_and_alternate_apps(self):
        with tempfile.TemporaryDirectory() as directory:
            for variant in r.VARIANTS:
                root = Path(directory) / variant
                snapshot = r.archive_snapshot(r.SOURCE, root, variant)
                names = snapshot["source_files"]
                self.assertNotIn("demo/benchmark-bugs.md", names)
                self.assertFalse((root / ".git").exists())
                self.assertFalse(any("nginx" in name or "eng/" in name for name in names))
                hosts = list(root.rglob("apphost.cs")) + list(root.rglob("apphost.mts"))
                self.assertEqual(len(hosts), 0 if variant == "raw" else 1)
                if hosts:
                    self.assertIn(f"03-observe/{variant}", str(hosts[0]))

    def test_transforms_do_not_change_seeded_application_faults(self):
        with tempfile.TemporaryDirectory() as directory:
            for variant in r.VARIANTS:
                root = Path(directory) / variant
                r.archive_snapshot(r.SOURCE, root, variant)
                source = root / "demo/start/src"
                before = r.file_manifest(source)
                mapping = dict(zip(r.PORTS, (25001, 25002, 25003, 25004)))
                changes = r.transform_fixture(root, variant, "ab-test", mapping, "10.0.400")
                after = r.file_manifest(source)
                changed = {name for name in before if before[name] != after[name]}
                expected = {"BingoBoard.Admin/Properties/launchSettings.json", "bingo-board/.env.example"}
                self.assertEqual(changed, expected if variant == "raw" else set())
                compose = (root / "demo/start/compose.yaml").read_text()
                self.assertIn("allkeys-lfr", compose)
                self.assertIn("postgres:18", compose)
                self.assertIn("/var/lib/postgresql\n", compose)
                if variant == "raw":
                    self.assertIn('"25001:5432"', compose)
                    self.assertIn('"25002:6379"', compose)
                    for path in (root / "demo/start/scripts").glob("*"):
                        self.assertNotRegex(path.read_text(), r"(?<!\d)(5432|6379|5039|5173)(?!\d)")
                else:
                    self.assertIn("allkeys-lfr", r.apphost(root, variant).read_text())
                    self.assertIn("ab-test-postgres-data", r.apphost(root, variant).read_text())
                    if variant == "typescript":
                        self.assertIn(".withDataVolume({ name:", r.apphost(root, variant).read_text())
                self.assertTrue(changes)

    def test_snapshot_hash_is_reproducible(self):
        with tempfile.TemporaryDirectory() as directory:
            a = r.archive_snapshot(r.SOURCE, Path(directory) / "a", "raw")
            b = r.archive_snapshot(r.SOURCE, Path(directory) / "b", "raw")
            self.assertEqual(a, b)

    def test_git_contains_broken_snapshot_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            r.archive_snapshot(r.SOURCE, workspace, "raw")
            env = r.isolated_environment(root / "home", workspace, "ab-test")
            r.init_trial_git(workspace, env)
            self.assertEqual(r.execute(["git", "remote"], cwd=workspace, env=env).stdout, "")
            self.assertEqual(r.execute(["git", "rev-list", "--count", "HEAD"], cwd=workspace, env=env).stdout.strip(), "1")

    def test_environment_drops_parent_identity_and_credentials(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(r.os.environ, {
            "GITHUB_TOKEN": "not-a-real-secret", "COPILOT_CUSTOM_INSTRUCTIONS_DIRS": "/personal",
            "COPILOT_MODEL": "wrong", "OTEL_EXPORTER_OTLP_ENDPOINT": "https://wrong",
            "BASH_ENV": "/personal/script", "COPILOT_PROVIDER_API_KEY": "fake",
        }):
            env = r.isolated_environment(Path(directory) / "home", Path(directory) / "workspace", "ab-test")
            for key in ("GITHUB_TOKEN", "COPILOT_MODEL", "COPILOT_CUSTOM_INSTRUCTIONS_DIRS",
                        "BASH_ENV", "OTEL_EXPORTER_OTLP_ENDPOINT", "COPILOT_PROVIDER_API_KEY"):
                self.assertNotIn(key, env)
            self.assertEqual(env["COMPOSE_PROJECT_NAME"], "ab-test")

    def test_migration_capture_keeps_true_exit_and_external_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            (workspace / "demo/start").mkdir(parents=True)
            env = r.isolated_environment(root / "home", workspace, "ab-test")
            output = root / "results"
            output.mkdir()
            r.install_runtime_capture(root / "home", workspace, env, output)
            env["BENCH_REAL_DOTNET"] = sys.executable
            worker = workspace / "BingoBoard.MigrationService.dll"
            worker.write_text("import sys\nprint('real worker output')\nsys.exit(3)\n")
            result = subprocess.run(["dotnet", str(worker)], cwd=workspace, env=env,
                                    stdin=subprocess.DEVNULL, capture_output=True, text=True)
            self.assertEqual(result.returncode, 3)
            self.assertEqual(result.stdout.strip(), "real worker output")
            marker = r.read_json(next((output / "runtime/migrations").glob("*.json")))
            self.assertEqual(marker["exit_code"], 3)
            self.assertEqual(marker["state"], "Exited")
            self.assertFalse(r.inside(Path(marker["log_path"]), workspace))
            self.assertTrue((workspace / "demo/start/.script-state").is_symlink())


class MeasurementTests(unittest.TestCase):
    def event(self, typ, data, sec=0):
        return {"type": typ, "data": data, "timestamp": f"2026-10-06T00:00:{sec:02d}Z"}

    def test_usage_not_double_counted(self):
        calls = [self.event("model.call_start", {}), self.event("session.usage_checkpoint", {
            "promptCacheBreakState": [{"models": {"gpt-6-luna": {
                "model_call_id": "one", "prompt_tokens": 9130}}}]}), self.event(
            "session.usage_checkpoint", {"promptCacheBreakState": [{"models": {
                "gpt-6-luna": {"model_call_id": "one", "prompt_tokens": 9130}}}]})]
        usage = {"tokenDetails": {"input": {"tokenCount": 3}, "cache_write": {"tokenCount": 9127}},
                 "modelMetrics": {"gpt-6-luna": {"usage": {
                     "inputTokens": 9130, "cacheWriteTokens": 9127, "reasoningTokens": 0}}},
                 "agentMetrics": {"main": {"duplicated": "do not sum"}}, "totalNanoAiu": 114367500}
        summary = r.summarize(calls, usage)
        self.assertEqual(summary["token_buckets"]["input"], 3)
        self.assertIsNone(summary["token_buckets"]["output"])
        self.assertEqual(summary["nano_aiu"], 114367500)
        self.assertIsNone(summary["total_tokens"])
        self.assertEqual(summary["peak_call_input_tokens"], 9130)
        self.assertEqual(summary["model_calls"], 1)

    def test_unobserved_peak_is_null(self):
        events = [self.event("model.call_start", {}), self.event("model.call_start", {}),
                  self.event("session.usage_checkpoint", {"promptCacheBreakState": [
                      {"models": {"x": {"model_call_id": "one", "prompt_tokens": 5}}}]})]
        self.assertIsNone(r.summarize(events, {})["peak_call_input_tokens"])

    def test_order_classification_and_successful_latency(self):
        events = [self.event("session.start", {}), self.event("tool.execution_start", {
            "toolCallId": "a", "toolName": "bash", "arguments": {"command": "aspire describe"}}, 2),
            self.event("tool.execution_complete", {"toolCallId": "a", "success": True}, 3),
            self.event("tool.execution_start", {"toolCallId": "b", "toolName": "apply_patch",
                                              "arguments": {}}, 4),
            self.event("tool.execution_complete", {"toolCallId": "b", "success": False}, 5)]
        summary = r.summarize(events, {})
        self.assertEqual([tool["category"] for tool in summary["tool_sequence"]], ["evidence", "edit"])
        self.assertEqual(summary["first_runtime_evidence_ms"], 3000)
        self.assertIsNone(summary["first_edit_ms"])
        self.assertNotEqual(r.classify_tool("aspire-docs", {"query": "docs"}), "evidence")

    def test_diagnosis_is_external_and_independent(self):
        valid = {"diagnoses": [{"service": "redis", "candidates": [
            {"cause": "a cause", "file": "demo/start/compose.yaml", "evidence": "log"}]}]}
        self.assertEqual(r.diagnosis(json.dumps(valid)), (valid, None))
        parsed, error = r.diagnosis("I fixed it")
        self.assertEqual(parsed, {"diagnoses": []})
        self.assertIsNotNone(error)

    def test_anthropic_final_without_phase(self):
        events = [self.event("assistant.message", {"content": "thinking", "phase": "commentary"}),
                  self.event("assistant.message", {"content": "OK", "toolRequests": []})]
        self.assertEqual(r.final_answer(events), "OK")

    def test_authorized_haiku_native_does_not_send_effort_flag(self):
        trial = {"model": "claude-haiku-4.5", "reasoning_effort": "native"}
        args = r.agent_command(trial, Path("/external"), {"skills": [], "mcp_config": None}, "fixed")
        self.assertNotIn("--reasoning-effort", args)
        self.assertEqual(args[args.index("--context") + 1], "default")

    def test_duplicate_diagnosis_keys_are_not_normalized(self):
        _, error = r.diagnosis('{"diagnoses":[],"diagnoses":[{"service":"hidden guess"}]}')
        self.assertIn("Duplicate JSON key", error)

    def test_aspire_runtime_contract_uses_root_http_endpoint(self):
        obj = {"resources": [{"name": "boardadmin-123", "displayName": "boardadmin",
                              "urls": [{"name": "Manage squares", "url": "http://localhost:25250/squares-management"},
                                       {"name": "http", "url": "http://localhost:25250/"}]},
                             {"name": "migrations", "state": "Finished", "exitCode": 0}]}
        items = r.resources(obj)
        self.assertEqual(r.resource_url(items[0]), "http://localhost:25250")
        self.assertEqual(r.resource_value(items[1], "exitCode"), 0)
        self.assertEqual(r.resource_value(items[1], "state"), "Finished")
        with self.assertRaises(r.BenchError):
            r.resource_url({"urls": [{"url": "http://external.example"}]})

    def test_skill_and_mcp_leakage_invalidates_arm(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            events = [
                self.event("session.start", {"selectedModel": "gpt-6-luna", "reasoningEffort": "medium",
                                              "contextTier": "default", "context": {"cwd": str(workspace)}}),
                self.event("session.tools_updated", {"model": "gpt-6-luna"}),
                self.event("session.mcp_servers_loaded", {"servers": [{"name": "personal", "status": "disabled"}]}),
                self.event("session.usage_checkpoint", {"promptCacheBreakState": [{"models": {
                    "gpt-6-luna": {"model_call_id": "one", "reasoning_effort": "medium",
                                   "tools": [{"name": "view"}]}}}]}),
            ]
            treatment = {"skills": [], "discovery": {"skill": []}}
            trial = {"model": "gpt-6-luna", "mcp": False}
            self.assertFalse(r.check_isolation(events, treatment, trial, workspace)["valid"])

    def test_json_prefix_handling_is_strict(self):
        self.assertEqual(r.parse_json_output("Starting...\n{\"resources\": []}\n"), {"resources": []})
        with self.assertRaises(r.BenchError):
            r.parse_json_output("not json")


class ConfigurationTests(unittest.TestCase):
    def reusable_fixture(self, root, old_code=None):
        config = r.load_config(r.HERE / "configs/primary-pairs.json")
        config["verifier"] = {"sha256": "grader"}
        trial = next(trial for trial in r.trial_plan(config) if trial["variant"] == "raw")
        code = old_code or (r.HERE / "runner.py").read_text()
        result = {"trial": trial, "calibration": False, "status": "budget_hit",
                  "isolation": {"valid": True}, "teardown_errors": [],
                  "verifier_pin": config["verifier"], "snapshot": {"commit": r.SOURCE},
                  "prompt_sha256": r.digest((r.HERE / "task-prompt.txt").read_bytes()),
                  "prewarm": {"startup_state": "cold-stopped-prewarmed"},
                  "harness_commit": "fake",
                  "harness_files": {name: r.digest((r.HERE / name).read_bytes())
                                    for name in ("tool-shim.py", "task-prompt.txt")}}
        result["harness_files"]["runner.py"] = r.digest(code.encode())
        path = root / "batch/01-model/result.json"
        r.write_json(path, result)
        r.write_json(path.parent / "usage.json", {"currentModel": trial["model"]})
        r.write_json(path.parent / "teardown.json", {"errors": [], "orphan_processes": []})
        r.write_json(path.parent.parent / "config.json", config)
        return path, config, code

    def test_reuse_accepts_valid_budget_hits_without_rerunning(self):
        with tempfile.TemporaryDirectory() as directory:
            path, config, code = self.reusable_fixture(Path(directory))
            with patch.object(r, "execute", return_value=subprocess.CompletedProcess([], 0, code, "")):
                key, result = r.validate_reuse(path, config, r.trial_plan(config))
            self.assertEqual(key[1], "raw")
            self.assertEqual(result["status"], "budget_hit")
            self.assertEqual(result["reused_result_path"], str(path.resolve()))

    def test_reuse_rejects_changed_budget_or_execution_protocol(self):
        with tempfile.TemporaryDirectory() as directory:
            path, config, code = self.reusable_fixture(Path(directory))
            config["timeout_seconds"] += 1
            with self.assertRaisesRegex(r.BenchError, "timeout_seconds"):
                r.validate_reuse(path, config, r.trial_plan(config))
        with tempfile.TemporaryDirectory() as directory:
            code = (r.HERE / "runner.py").read_text().replace("budget_hit = False", "budget_hit = True", 1)
            path, config, code = self.reusable_fixture(Path(directory), code)
            with patch.object(r, "execute", return_value=subprocess.CompletedProcess([], 0, code, "")):
                with self.assertRaisesRegex(r.BenchError, "invoke_agent"):
                    r.validate_reuse(path, config, r.trial_plan(config))

    def test_reuse_rejects_orphans_and_missing_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            path, config, _ = self.reusable_fixture(Path(directory))
            r.write_json(path.parent / "teardown.json", {"orphan_processes": [{"pid": 99}]})
            with self.assertRaisesRegex(r.BenchError, "cleanup"):
                r.validate_reuse(path, config, r.trial_plan(config))
            (path.parent / "usage.json").unlink()
            with self.assertRaisesRegex(r.BenchError, "captured"):
                r.validate_reuse(path, config, r.trial_plan(config))

    def test_primary_stops_on_marker_or_infrastructure_failure_before_next_trial(self):
        for mode in ("marker", "infrastructure_error"):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                grader = root / "grader.py"
                grader.write_text("pass\n")
                config = r.load_config(r.HERE / "configs/primary-pairs.json")
                config["models"] = ["gpt-6-luna"]
                config["verifier"] = {"path": str(grader), "sha256": r.digest(grader.read_bytes())}
                r.write_json(root / "config.json", config)
                stop = root / "stop"
                def fake_trial(config, trial, output, *, calibration):
                    if mode == "marker":
                        stop.write_text("stop after cleanup")
                    return {"trial": trial, "status": "repair_pass" if mode == "marker" else mode}
                argv = ["runner.py", "run", "--config", str(root / "config.json"),
                        "--output", str(root / "results"), "--stop-file", str(stop)]
                with patch.object(sys, "argv", argv), patch.object(
                        r, "run_trial", side_effect=fake_trial) as run, contextlib.redirect_stdout(
                            io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(r.main(), 1)
                self.assertEqual(run.call_count, 1)
                self.assertEqual(len(r.read_json(root / "results/results.json")), 1)
                self.assertTrue((root / "results/batch-stop.json").exists())

    def test_primary_pairs_are_seeded_balanced_and_matched(self):
        config = r.load_config(r.HERE / "configs/primary-pairs.json")
        plan = r.trial_plan(config)
        self.assertEqual(plan, r.trial_plan(config))
        self.assertEqual(len(plan), 8)
        self.assertEqual(sum(plan[i]["variant"] == "raw" for i in range(0, 8, 2)), 2)
        for i in range(0, 8, 2):
            a, b = plan[i:i + 2]
            self.assertEqual(a["model"], b["model"])
            self.assertEqual(a["reasoning_effort"], b["reasoning_effort"])
            self.assertEqual({a["variant"], b["variant"]}, {"raw", "typescript"})
            for arm in (a, b):
                self.assertEqual(arm["mcp"], arm["variant"] == "typescript")
                self.assertEqual(arm["skills"], "current" if arm["mcp"] else "none")

    def test_primary_rejects_repetitions_and_duplicate_models(self):
        for extra in ({"replicates": 2}, {"models": ["gpt-6-luna", "gpt-6-luna"]},
                      {"models": ["unknown"]}, {"trials": [{"model": "gpt-6-luna"}]}):
            with self.assertRaises(r.BenchError):
                r.trial_plan({"design": "paired-primary", **extra})

    def test_primary_summary_keeps_native_buckets_and_incomplete_pairs(self):
        plan = r.trial_plan({"design": "paired-primary", "models": ["gpt-6-luna"]})
        results = [{"trial": {"model": "gpt-6-luna", "variant": "raw"},
                    "metrics": {"nano_aiu": 2, "assistant_turns": 4,
                                "token_buckets": {"input": 5, "cache_read": 20}},
                    "agent_wall_ms": 10, "status": "budget_hit", "repair_success": False}]
        incomplete = r.paired_summary(plan, results)["pairs"][0]
        self.assertFalse(incomplete["complete"])
        self.assertIsNone(incomplete["typescript_minus_raw"]["agent_wall_ms"])
        results.append({"trial": {"model": "gpt-6-luna", "variant": "typescript"},
                        "metrics": {"nano_aiu": 3, "assistant_turns": 6,
                                    "token_buckets": {"input": 7, "cache_read": 15}},
                        "agent_wall_ms": 8, "status": "repair_pass", "repair_success": True})
        pair = r.paired_summary(plan, results)["pairs"][0]
        self.assertTrue(pair["complete"])
        self.assertEqual(pair["typescript_minus_raw"]["agent_wall_ms"], -2)
        self.assertEqual(pair["typescript_minus_raw"]["assistant_turns"], 2)
        self.assertEqual(pair["typescript_minus_raw"]["token_buckets"]["cache_read"], -5)
        self.assertIsNone(pair["typescript_minus_raw"]["token_buckets"]["output"])

    def test_plan_reproducible_and_factorial_only_planned(self):
        config = {"seed": 7, "replicates": 2, "models": list(r.MODELS)}
        a, b = r.trial_plan(config), r.trial_plan(config)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 96)
        self.assertEqual({trial["replicate"] for trial in a}, {1, 2})

    def test_malformed_treatments_rejected(self):
        for trial in (
            {"model": "auto", "variant": "raw", "skills": "none", "mcp": False},
            {"model": "x", "variant": "raw", "skills": "external-dir", "mcp": False},
            {"model": "x", "variant": "raw", "skills": "none", "mcp": "false"},
        ):
            with self.assertRaises(r.BenchError):
                r.trial_plan({"trials": [trial]})

    def test_repository_artifacts_rejected(self):
        with self.assertRaises(r.BenchError):
            r.external(r.REPO / "results")

    def test_agent_flags_freeze_exact_configuration(self):
        trial = {"model": "claude-haiku-4.5"}
        args = r.agent_command(trial, Path("/external"), {"skills": [], "mcp_config": None}, "fixed")
        for flag in ("--yolo", "--no-ask-user", "--no-auto-update", "--disable-builtin-mcps",
                     "--no-custom-instructions", "--no-remote-export"):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index("--reasoning-effort") + 1], "medium")
        self.assertEqual(args[args.index("--context") + 1], "default")
        self.assertNotIn("--fleet", args)

    def test_cleanup_refuses_unowned_volume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = {"run_id": "ab-test", "containers": [], "processes": [],
                       "volumes": ["unrelated-personal-volume"]}
            with patch.object(r, "execute") as command:
                errors = r.teardown(root, {}, {"variant": "raw"}, root, runtime)
            command.assert_not_called()
            self.assertEqual(len(errors), 1)
            self.assertIn("Refused", errors[0])

    def test_candidate_diff_survives_agent_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, output = root / "workspace", root / "output"
            workspace.mkdir()
            output.mkdir()
            (workspace / "source.txt").write_text("before\n")
            env = r.isolated_environment(root / "home", workspace, "ab-test")
            fixture = r.init_trial_git(workspace, env)
            (output / "fixture-commit.txt").write_text(fixture)
            (workspace / "source.txt").write_text("after\n")
            r.execute(["git", "add", "."], cwd=workspace, env=env)
            r.execute(["git", "-c", "user.name=Agent", "-c", "user.email=agent@example.invalid",
                       "commit", "-q", "-m", "candidate"], cwd=workspace, env=env)
            r.capture_diff(workspace, env, output)
            self.assertIn("+after", (output / "candidate.patch").read_text())

    def test_not_running_aspire_information_on_stderr_is_not_json_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            response = subprocess.CompletedProcess([], 0, "", "No AppHost is currently running")
            with patch.object(r, "docker_inspect", return_value=None), patch.object(
                    r, "execute", return_value=response):
                runtime = r.collect_runtime(root, {}, {"variant": "typescript"}, root, "ab-test", {})
            self.assertEqual(runtime["apphost_state"], "not_running")
            self.assertNotIn("frontend_url", runtime)
            self.assertEqual(runtime["containers"], [])


if __name__ == "__main__":
    unittest.main()
