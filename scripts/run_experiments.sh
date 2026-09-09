#!/usr/bin/env bash
# Every configuration at both seeds. Cache is cleared per config-and-seed by optimize.py,
# per the assessment's timing protocol.
set -u
cd "$(dirname "$0")/.."
for seed in 0 1; do
  for cfg in baseline control control_handpicked bootstrap; do
    echo "===== $cfg seed=$seed ====="
    .venv/bin/python optimize.py --config "$cfg" --seed "$seed" 2>&1
  done
done
