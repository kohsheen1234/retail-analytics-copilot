"""Paths and pinned runtime settings.

Every generation setting here is transcribed from `starter/ENVIRONMENT.md`. They are
constants, not defaults to be tuned: `num_ctx` in particular may not be raised.

An empirical check that these actually reach Ollama (rather than being silently dropped
by litellm's parameter mapping) lives in `tests/test_lm_settings.py`.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DOCS_DIR = ROOT / "docs"
DATA_DIR = ROOT / "data"
ARTIFACTS_DIR = ROOT / "artifacts"
TRACES_DIR = ROOT / "traces"

DB_PATH = Path(os.environ.get("NORTHWIND_DB", DATA_DIR / "northwind.sqlite"))

# Published in the assessment, so it can live in the repo. The brief warns that the
# Northwind build circulating online under the same name is a different file whose
# numbers will not match, so the checksum is worth asserting rather than assuming.
DB_SHA256 = "2f4f5c68dfcd33ba27373eae48c7a4869800c68095ee0f9f0da494f83382a877"

BEST_ARTIFACT = ARTIFACTS_DIR / "best.json"
SELECTION_FILE = ARTIFACTS_DIR / "selection.json"

# --- Model, per ENVIRONMENT.md ------------------------------------------------
MODEL_TAG = "phi3.5:3.8b-mini-instruct-q4_K_M"
OLLAMA_BASE = os.environ.get("OLLAMA_HOST_URL", "http://localhost:11434")
LM_MODEL = f"ollama_chat/{MODEL_TAG}"

NUM_CTX = 4096          # do not raise
NUM_PREDICT = 512       # -> litellm max_tokens -> ollama num_predict
TEMPERATURE = 0.0
TOP_P = 1.0
TOP_K = 0

# --- Execution boundary -------------------------------------------------------
SQL_ROW_LIMIT = 1000
SQL_TIMEOUT_S = 10.0

# --- Retrieval ----------------------------------------------------------------
RETRIEVE_K = 4

# --- Repair -------------------------------------------------------------------
MAX_REPAIRS = 2         # hard cap from the assessment

# --- Confidence rubric --------------------------------------------------------
# Deterministic, documented in DECISIONS.md. Never exceeds CONF_PENALTY_LINE when a
# load-bearing assumption was invented rather than sourced from the corpus.
CONF_PENALTY_LINE = 0.70
