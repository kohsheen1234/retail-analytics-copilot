#!/usr/bin/env bash
set -u
cd "$(dirname "$0")/.."
for seed in 0 1; do
  echo "===== bootstrap_rs seed=$seed ====="
  .venv/bin/python optimize.py --config bootstrap_rs --seed "$seed" 2>&1
done
