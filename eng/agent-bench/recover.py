#!/usr/bin/env python3
"""Recover captured measurements and attach source-unchanged unpaid replay evidence."""

import argparse
import importlib.util
import json
from pathlib import Path
import shutil


spec = importlib.util.spec_from_file_location("runner", Path(__file__).with_name("runner.py"))
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


def recover(source: Path, replay_path: Path, output: Path) -> dict:
    source, replay_path, output = r.external(source), r.external(replay_path), r.external(output)
    original, replay = r.read_json(source), r.read_json(replay_path)
    manifest = r.read_json(source.parent / "candidate-files.json")
    if (replay.get("source_result") != str(source) or replay.get("status") != "replay_captured" or
            replay.get("paid_model_calls") != 0 or replay.get("source_edits") is not False or
            replay.get("candidate_sha256") != r.manifest_hash(manifest) or
            not replay.get("candidate_unchanged_after_verification") or
            not replay.get("original_candidate_still_unchanged") or replay.get("teardown_errors") or
            not replay.get("ownership", {}).get("validated") or
            replay.get("verifier_pin", {}).get("sha256") != original["verifier_pin"]["sha256"]):
        raise r.BenchError("Replay is not proven unchanged, owned, clean and bound to this attempt")
    if r.file_manifest(Path(original["workspace"]), ignore=True) != manifest:
        raise r.BenchError("Original candidate changed after capture")
    if original.get("teardown_errors") or original.get("calibration"):
        raise r.BenchError("Recovery requires a cleanly torn-down repair attempt")
    config = r.read_json(source.parent.parent / "config.json")
    config_path = output.parent / "config.json"
    if config_path.exists() and r.read_json(config_path) != config:
        raise r.BenchError("Recovery batch configuration conflict")
    if output.exists():
        raise r.BenchError("Use a new recovery directory")
    output.mkdir(parents=True)
    for name in ("events.jsonl", "usage.json", "teardown.json", "candidate-files.json",
                 "configuration.json", "prompt.txt"):
        shutil.copyfile(source.parent / name, output / name)
    events = r.merged_events(output, Path(original["scratch_home"]))
    fidelity = r.check_isolation(events, original["treatment"], original["trial"],
                                Path(original["workspace"]))
    if not fidelity["valid"]:
        raise r.BenchError("Recovered events do not prove treatment fidelity")
    answer = r.final_answer(events)
    (output / "diagnosis.json").write_text(answer or "{}")
    (output / "final-answer.txt").write_text(answer or "")
    grade = replay["verification"]
    result = {
        **original, "error": None, "isolation": fidelity,
        "metrics": r.summarize(events, r.read_json(output / "usage.json")),
        "status": "budget_hit" if original["agent"]["budget_hit"] else
                  "repair_pass" if grade["repair_success"] else "repair_fail",
        "repair_success": grade["repair_success"], "diagnosis_success": grade["diagnosis_success"],
        "verification": grade, "post_teardown_verification": True,
        "postprocessing_recovery": {
            "source_result": str(source), "source_result_sha256": r.digest(source.read_bytes()),
            "replay_result": str(replay_path), "replay_wall_ms": replay.get("replay_wall_ms"),
            "current_runner_sha256": r.digest((r.HERE / "runner.py").read_bytes()),
            "original_status": original["status"], "original_error": original.get("error"),
            "paid_model_calls": 0, "timings_replaced": False,
            "native_usage_sha256": r.digest((source.parent / "usage.json").read_bytes()),
        },
    }
    r.write_json(output / "result.json", result)
    r.write_json(config_path, config)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-result", type=Path, required=True)
    parser.add_argument("--replay-result", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = recover(args.candidate_result, args.replay_result, args.output)
    except (r.BenchError, OSError, ValueError) as exc:
        parser.exit(1, str(exc) + "\n")
    print(json.dumps({"status": result["status"], "paid_model_calls": 0,
                      "post_teardown_verification": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
