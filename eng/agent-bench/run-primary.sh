#!/usr/bin/env bash
set -euo pipefail

output="${1:?Supply a new absolute results directory outside the repository}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
verifier="ed64da101b31b89296a44c893ac67d4a0af9dca9"
config="$here/configs/primary-pairs.json"

for variant in raw typescript; do
  python3 -B "$here/runner.py" probe --probe-variant "$variant" --config "$config" \
    --verifier-commit "$verifier" --output "$output/$variant-healthy"
  python3 -B "$here/runner.py" probe --probe-variant "$variant" --seeded-negative \
    --config "$config" --verifier-commit "$verifier" --output "$output/$variant-negative"
done
exec python3 -B "$here/runner.py" run --config "$config" --verifier-commit "$verifier" \
  --fixture-gate "$output/raw-healthy/fixture/result.json" \
  --fixture-gate "$output/raw-negative/fixture/result.json" \
  --fixture-gate "$output/typescript-healthy/fixture/result.json" \
  --fixture-gate "$output/typescript-negative/fixture/result.json" \
  --stop-file "$output/stop-after-current" --output "$output/primary-pairs"
