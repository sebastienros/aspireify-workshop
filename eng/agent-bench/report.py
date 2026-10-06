#!/usr/bin/env python3
"""Revise derived handoff tables offline, without changing captured measurements."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import shutil


REPORTING_VERSION = "usage-tables-v2"
USAGE_COLUMNS = (
    "cache_read_tokens", "cache_write_tokens", "uncached_input_tokens", "output_tokens",
    "non_cached_input_output_tokens", "recorded_input_tokens", "recorded_input_output_tokens",
    "ai_credits", "input_bucket_identity_verified",
)
TOKEN_SEMANTICS = (
    "Non-cached input+output = uncached-input + output; cache-read and cache-write are "
    "separate input buckets and excluded from that subtotal. Recorded input+output = "
    "native model inputTokens + output tokens, with inputTokens verified against "
    "uncached-input + cache-read + cache-write when all records are available. "
    "Reasoning is retained separately, never added again. AI Credits = native nanoAIU / "
    "1,000,000,000, not a flat billed-token rate. Missing values remain null."
)


def count(value, name: str) -> int | None:
    if value is not None and (type(value) is not int or value < 0):
        raise ValueError(f"{name} must be a nonnegative integer or null")
    return value


def known_sum(values: list[int | None]) -> int | None:
    return sum(values) if values and all(value is not None for value in values) else None


def usage_columns(usage: dict) -> dict:
    buckets = {key: count(usage.get("tokenDetails", {}).get(key, {}).get("tokenCount"), key)
               for key in ("input", "cache_read", "cache_write", "output")}
    models = [value.get("usage", {}) for value in usage.get("modelMetrics", {}).values()]
    native = {key: known_sum([count(model.get(key), key) for model in models])
              for key in ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens")}
    for key, bucket in (("outputTokens", "output"), ("cacheReadTokens", "cache_read"),
                        ("cacheWriteTokens", "cache_write")):
        if native[key] is not None and buckets[bucket] is not None and native[key] != buckets[bucket]:
            raise ValueError(f"Native {key} does not match tokenDetails.{bucket}")
    input_sum = known_sum([buckets[key] for key in ("input", "cache_read", "cache_write")])
    verified = None
    if input_sum is not None and native["inputTokens"] is not None:
        if input_sum != native["inputTokens"]:
            raise ValueError("Native inputTokens does not equal uncached-input + cache-read + cache-write")
        verified = True
    nano = count(usage.get("totalNanoAiu"), "totalNanoAiu")
    return {
        "cache_read_tokens": buckets["cache_read"], "cache_write_tokens": buckets["cache_write"],
        "uncached_input_tokens": buckets["input"], "output_tokens": buckets["output"],
        "non_cached_input_output_tokens": known_sum([buckets["input"], buckets["output"]]),
        "recorded_input_tokens": native["inputTokens"],
        "recorded_input_output_tokens": known_sum([native["inputTokens"], buckets["output"]]),
        "ai_credits": nano / 1_000_000_000 if nano is not None else None,
        "input_bucket_identity_verified": verified,
    }


def usage_from_metrics(metrics: dict) -> dict:
    return usage_columns({
        "tokenDetails": {key: {"tokenCount": value}
                         for key, value in (metrics.get("token_buckets") or {}).items()},
        "modelMetrics": {key: {"usage": value}
                         for key, value in (metrics.get("model_usage") or {}).items()},
        "totalNanoAiu": metrics.get("nano_aiu"),
    })


def read_json(path: Path):
    return json.loads(path.read_text())


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_csv(path: Path, rows: list[dict]) -> None:
    fields = list(dict.fromkeys(["model", "variant", *USAGE_COLUMNS,
                                *(key for row in rows for key in row)]))
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fields)
        writer.writeheader()
        writer.writerows(rows)


def display(value) -> str:
    if value is None:
        return "unknown"
    if type(value) is bool:
        return "pass" if value else "fail"
    if isinstance(value, float):
        return f"{value:.9f}".rstrip("0").rstrip(".")
    return str(value)


def table(rows: list[dict], *, primary: bool) -> list[str]:
    headers = ["Model", "Arm", "Cache-read", "Cache-write", "Uncached input", "Output",
               "Non-cached input+output", "Recorded input", "Recorded input+output", "AI Credits"]
    if primary:
        headers.extend(["Agent seconds", "Runtime workflows", "Frozen preservation4", "Frozen strict repair4",
                        "Supplemental diagnosis5", "Qualifications"])
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for row in rows:
        values = [row["model"], row["variant"], *(display(row[key]) for key in USAGE_COLUMNS[:-1])]
        if primary:
            notes = []
            if row.get("agent_scope_violation"):
                notes.append("unsafe scope violation; retained failure")
            if row.get("protocol_deviation"):
                notes.append("protocol deviation; see preserved qualifications")
            if row.get("supplemental_cleanup_corrected"):
                notes.append("supplemental cleanup corrected")
            if row.get("status") == "budget_hit":
                notes.append("budget-hit; see replay qualification")
            if row.get("runtime_workflows_success") is True and row.get("strict_preservation_oracle4") is False:
                notes.append("runtime pass; strict-only rejection (see qualifications)")
            if row.get("diagnosis_format_valid") is False:
                notes.append("malformed JSON, not a wrong-root conclusion")
                diagnosis = "format fail"
            elif row.get("diagnosis_oracle5_total_roots") is not None:
                diagnosis = (f"{row['diagnosis_oracle5_passed_roots']}/{row['diagnosis_oracle5_total_roots']} "
                             f"({display(row.get('diagnosis_oracle5_supplemental'))})")
            else:
                diagnosis = display(row.get("diagnosis_oracle5_supplemental"))
            values.extend([f"{row['agent_wall_ms'] / 1000:.3f}",
                           display(row.get("runtime_workflows_success")),
                           display(row.get("strict_preservation_oracle4")),
                           display(row.get("strict_repair_oracle4")),
                           diagnosis,
                           "; ".join(notes) or "-"])
        lines.append("| " + " | ".join(values) + " |")
    return lines


def revise_handoff(source: Path, output: Path, reporter_commit: str | None = None) -> dict:
    source, output = source.resolve(), output.resolve()
    repo = Path(__file__).resolve().parents[2]
    if output.is_relative_to(repo) or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("Use a new external output directory, separate from the source handoff")
    if output.exists():
        raise ValueError("Refusing to overwrite an existing handoff directory")
    inputs = {}

    def track(path: Path) -> None:
        path = path.resolve()
        if not path.is_file():
            raise ValueError(f"Missing reporting input: {path}")
        inputs[str(path)] = sha256(path)

    paths = sorted(path for path in source.rglob("*") if path.is_file())
    for path in paths:
        if path.is_symlink():
            raise ValueError(f"Symlink reporting input is not allowed: {path}")
        track(path)
    frozen = read_json(source / "frozen-artifact-hashes.json")
    for path, expected in frozen.items():
        track(Path(path))
        if inputs[str(Path(path).resolve())] != expected:
            raise ValueError(f"Frozen artifact changed: {path}")
    rows = read_json(source / "aggregate.json")
    index = read_json(source / "artifact-index.json")
    reasons = read_json(source / "diagnosis-reasons.json")
    faults = {(row["model"], row["variant"]):
              (row.get("supplemental_oracle5_details") or {}).get("faults", {}) for row in reasons}
    indexed = {(row["model"], row["variant"]): row for row in index}
    keys = [(row["model"], row["variant"]) for row in rows]
    if len(set(keys)) != len(keys) or set(keys) != set(indexed) or len(index) != len(rows):
        raise ValueError("Aggregate/index must identify the same unique primary attempts")
    for row in rows:
        path = Path(indexed[row["model"], row["variant"]]["usage_path"])
        track(path)
        usage = read_json(path)
        if row["native_nano_aiu"] != usage.get("totalNanoAiu"):
            raise ValueError(f"Captured nanoAIU differs from the aggregate: {path}")
        for bucket in ("input", "cache_read", "cache_write", "output"):
            if row[bucket] != usage.get("tokenDetails", {}).get(bucket, {}).get("tokenCount"):
                raise ValueError(f"Captured {bucket} differs from the aggregate: {path}")
        row.update(usage_columns(usage))
        roots = faults.get((row["model"], row["variant"]), {})
        row.update({"diagnosis_oracle5_passed_roots":
                    sum(root["status"] == "pass" for root in roots.values()) if roots else None,
                    "diagnosis_oracle5_total_roots": len(roots) if roots else None})
    updated = {(row["model"], row["variant"]): row for row in rows}
    costs = read_json(source / "costs.json")
    for pilot in costs["pilots"]:
        path = Path(pilot["path"]) / "usage.json"
        track(path)
        usage = read_json(path)
        if pilot["native_nano_aiu"] != usage.get("totalNanoAiu"):
            raise ValueError(f"Captured pilot nanoAIU differs from the costs: {path}")
        pilot.update(usage_columns(usage))
    primary_nano = known_sum([row["native_nano_aiu"] for row in rows])
    pilot_nano = known_sum([row["native_nano_aiu"] for row in costs["pilots"]])
    if primary_nano != costs["primary_total_native_nano_aiu"] or pilot_nano != costs["pilot_total_native_nano_aiu"]:
        raise ValueError("Native totals must retain every primary attempt and keep pilots separate")
    costs.update({"primary_total_ai_credits": primary_nano / 1_000_000_000,
                  "pilot_total_ai_credits": pilot_nano / 1_000_000_000,
                  "primary_ai_credits_by_arm": {
                      arm: sum(row["native_nano_aiu"] for row in rows if row["variant"] == arm) / 1_000_000_000
                      for arm in dict.fromkeys(row["variant"] for row in rows)}})
    paired = read_json(source / "paired-results.json")
    for pair in paired:
        for arm in ("raw", "typescript"):
            if arm in pair:
                pair[arm].update({key: updated[pair["model"], arm][key] for key in USAGE_COLUMNS})
        if "raw" in pair and "typescript" in pair:
            pair["typescript_minus_raw_usage"] = {
                key: pair["typescript"][key] - pair["raw"][key]
                if pair["typescript"][key] is not None and pair["raw"][key] is not None else None
                for key in USAGE_COLUMNS[:-1]}
    for collection in (index, reasons):
        for row in collection:
            row.update({key: updated[row["model"], row["variant"]][key] for key in USAGE_COLUMNS})
    old_report = (source / "report.md").read_text().splitlines()
    first_table = next(i for i, line in enumerate(old_report) if line.startswith("|"))
    end_table = first_table
    while end_table < len(old_report) and old_report[end_table].startswith("|"):
        end_table += 1
    body = [
        "# Eight matched primary smoke attempts - explicit token buckets and AI Credits", "",
        *table(rows, primary=True), "", TOKEN_SEMANTICS, "",
        f"Primary total: **{display(costs['primary_total_ai_credits'])} AI Credits** "
        f"({primary_nano} native nanoAIU), including failures and the unsafe scope violation.",
        "By arm: " + "; ".join(f"{arm} {display(credits)} AI Credits"
                               for arm, credits in costs["primary_ai_credits_by_arm"].items()) + ".",
        "", "## Excluded pilots - separate cost accounting", "",
        *table(costs["pilots"], primary=False), "",
        f"Excluded pilot total: **{display(costs['pilot_total_ai_credits'])} AI Credits** "
        f"({pilot_nano} native nanoAIU); not included in primary totals.", "",
        "## Preserved outcome and protocol qualifications", "",
        *old_report[end_table:], "",
        "Offline table revision only: no model calls, runtime requests, candidate reads/edits or new scoring.",
        "Input hashes and reporter version/commit are in reporting-provenance.json.",
    ]
    provenance = {"reporting_version": REPORTING_VERSION, "reporter_commit": reporter_commit,
                  "reporter_sha256": sha256(Path(__file__)), "source_handoff": str(source),
                  "input_sha256": inputs, "token_semantics": TOKEN_SEMANTICS,
                  "model_calls": 0, "runtime_evaluated": False, "candidate_reads": False,
                  "original_scoring_timing_cost_modified": False}
    output.mkdir(parents=True)
    for path in paths:
        relative = path.relative_to(source)
        if relative.as_posix() == "publication.json":
            relative = Path("publication-before-usage-v2.json")
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    shutil.copyfile(source / "report.md", output / "report-before-usage-v2.md")
    for name, value in (("aggregate.json", rows), ("paired-results.json", paired), ("costs.json", costs),
                        ("artifact-index.json", index), ("diagnosis-reasons.json", reasons),
                        ("reporting-provenance.json", provenance)):
        write_json(output / name, value)
    write_csv(output / "aggregate.csv", rows)
    write_csv(output / "excluded-pilots.csv", costs["pilots"])
    (output / "report.md").write_text("\n".join(body))
    for path, expected in inputs.items():
        if sha256(Path(path)) != expected:
            raise ValueError(f"Reporting input changed during generation: {path}")
    return {"output": str(output), "primary_attempts": len(rows),
            "primary_ai_credits": costs["primary_total_ai_credits"],
            "pilot_ai_credits": costs["pilot_total_ai_credits"], "reporting_version": REPORTING_VERSION}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--handoff", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reporter-commit")
    args = parser.parse_args()
    try:
        result = revise_handoff(args.handoff, args.output, args.reporter_commit)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, str(exc) + "\n")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
