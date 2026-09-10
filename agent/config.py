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

# Two checksums, because they disagree. See DECISIONS.md, "Where the two hashes actually
# come from": the published value first appears in the revised brief, which claims the
# database ships in the pack; no delivered pack contains one, so PUBLISHED describes a file
# that was never served here.
#   PUBLISHED: the value printed in the assessment.
#   OBSERVED:  the file actually delivered, which reproduces all 23 provided gold
#              answers exactly (floats to 2dp on ~4.5e8 magnitudes). That is far
#              stronger evidence of identity than a hash, so the file is used and the
#              published checksum is treated as the thing that is wrong.
DB_SHA256_PUBLISHED = "2f4f5c68dfcd33ba27373eae48c7a4869800c68095ee0f9f0da494f83382a877"
DB_SHA256_OBSERVED = "fb24a4f796eb43fab8af439fd8ea73f79859a40eeaf6f024efd485cae75a3dd5"

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

# --- Explanation ---------------------------------------------------------------
# The explanation is generated deterministically from the plan and the executed SQL
# rather than by an LM call. Measured reason, not preference: phi3.5 hits num_predict=512
# on essentially every call ("LM response was truncated due to exceeding max_tokens=512")
# and one call costs 25-60s on this machine. The hidden set must finish in under five
# minutes, and an explanation call per question is the difference between meeting that
# budget and missing it. `explanation` wording is also the one output field the
# determinism contract explicitly exempts, which makes it the cheapest thing to give up.
# The DSPy Explainer module is kept and tested; set USE_LM_EXPLANATION=1 to use it.
USE_LM_EXPLANATION = os.environ.get("USE_LM_EXPLANATION", "0") == "1"

# --- Synthesis ------------------------------------------------------------------
# The DSPy synthesis module reads the executed rows independently and its answer is
# compared with the deterministic build. On by default: "Synthesis (DSPy module)" is a
# named CORE responsibility, and without it no DSPy module participates in synthesis on
# the SQL path at all.
#
# Measured cost on this machine: 6 eval questions took 370s with it against roughly 240s
# without, i.e. ~22s per SQL question for the extra call. The assessment says the hidden
# set should run in under 5 minutes on the *reference* machine, which ENVIRONMENT.md
# leaves as <fill>, so that budget cannot be checked from here; the live session allots 10
# minutes for the run, which 370s fits inside. Set SYNTH_SECOND_OPINION=0 to drop the call
# if the run needs to be faster - the shipped answer does not change, only the confidence
# signal and the trace record are lost.
SYNTH_SECOND_OPINION = os.environ.get("SYNTH_SECOND_OPINION", "1") == "1"

# --- Confidence rubric --------------------------------------------------------
# Deterministic, documented in DECISIONS.md. Never exceeds CONF_PENALTY_LINE when a
# load-bearing assumption was invented rather than sourced from the corpus.
CONF_PENALTY_LINE = 0.70
