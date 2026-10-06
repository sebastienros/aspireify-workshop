#!/usr/bin/env python3
"""Transparent trial-only process/migration evidence capture; no evaluator logic."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def main():
    tool = Path(sys.argv[0]).name
    executable = os.environ["BENCH_REAL_" + tool.upper()]
    output = Path(os.environ["BENCH_RUNTIME_DIR"])
    args = sys.argv[1:]
    started = " ".join(subprocess.run(["ps", "-p", str(os.getpid()), "-o", "lstart="],
                                     capture_output=True, text=True, check=True).stdout.split())
    write(output / "pids" / f"{os.getpid()}.json", {
        "pid": os.getpid(), "ppid": os.getppid(), "started": started,
        "tool": tool, "cwd": str(Path.cwd()), "argv": [executable, *args],
    })
    migration = tool == "dotnet" and (
        any("BingoBoard.MigrationService.dll" in arg for arg in args) or
        ("run" in args and any("BingoBoard.MigrationService" in arg for arg in args)))
    if not migration:
        os.execv(executable, [executable, *args])
    marker = output / "migrations" / f"{time.time_ns()}-{os.getpid()}.json"
    log = marker.with_suffix(".log")
    process = subprocess.Popen([executable, *args], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT)
    write(marker, {"state": "Running", "exit_code": None, "pid": process.pid,
                   "log_path": str(log)})
    with log.open("wb") as captured:
        for chunk in iter(lambda: process.stdout.read1(65536), b""):
            captured.write(chunk)
            captured.flush()
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
    code = process.wait()
    write(marker, {"state": "Exited", "exit_code": code, "pid": process.pid,
                   "log_path": str(log)})
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    sys.exit(main())
