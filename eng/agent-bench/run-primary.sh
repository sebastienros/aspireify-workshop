#!/usr/bin/env bash
set -euo pipefail

output="${1:?Supply a new absolute results directory outside the repository}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
verifier="d7b758209fd2deecda5075a1292853f3d569e214"
config="$here/configs/primary-pairs.json"

python3 -B "$here/runner.py" probe --config "$config" --verifier-commit "$verifier" \
  --output "$output/healthy"
python3 -B "$here/runner.py" probe --seeded-negative --config "$config" \
  --verifier-commit "$verifier" --output "$output/seeded-negative"
exec python3 -B "$here/runner.py" run --config "$config" --verifier-commit "$verifier" \
  --fixture-gate "$output/healthy/fixture/result.json" \
  --fixture-gate "$output/seeded-negative/fixture/result.json" \
  --stop-file "$output/stop-after-current" --output "$output/primary-pairs"
