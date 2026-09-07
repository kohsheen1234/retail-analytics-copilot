"""LM wiring: pinned generation settings, in-project cache, and call accounting.

Three things this module exists to get right.

1. **The pinned settings actually reach Ollama.** `num_ctx` is not in litellm's mapped
   OpenAI parameter list for the Ollama chat provider, so it is reasonable to worry that
   it is dropped. It is not: litellm passes unmapped provider kwargs straight into the
   request's `options` object. Verified by proxying a real request, which received
   `{"num_ctx": 4096, "num_predict": 512, "seed": 0, "temperature": 0.0, "top_k": 0,
   "top_p": 1.0}`. `tests/test_lm_settings.py` pins the kwargs so a refactor cannot
   quietly drop one and leave us generating at Ollama's 2048 default.

2. **Nothing is written outside the project.** DSPy's disk cache defaults to
   `~/.dspy_cache`; the assessment forbids writes outside the project directory, so the
   cache is redirected to `./.dspy_cache` (gitignored).

3. **LM calls and cache hits are counted separately.** The experiment budget is stated
   in LM calls, and cache hits must be reported but do not count. DSPy records a
   `cache_hit` flag per history entry, so a counter over the global history gives both
   numbers without wrapping the client.
"""
from __future__ import annotations

import shutil
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import dspy
from dspy.clients import configure_cache
from dspy.clients.base_lm import GLOBAL_HISTORY

from agent import config

CACHE_DIR = config.ROOT / ".dspy_cache"


def lm_kwargs(seed: int) -> dict[str, Any]:
    """Exactly the generation settings in ENVIRONMENT.md. Not tunable."""
    return {
        "temperature": config.TEMPERATURE,
        "max_tokens": config.NUM_PREDICT,     # litellm maps this to ollama num_predict
        "num_ctx": config.NUM_CTX,            # forwarded verbatim into ollama options
        "top_p": config.TOP_P,
        "top_k": config.TOP_K,
        "seed": seed,
        "api_base": config.OLLAMA_BASE,
    }


def build_lm(seed: int = 0, cache: bool = True) -> dspy.LM:
    return dspy.LM(config.LM_MODEL, cache=cache, **lm_kwargs(seed))


def use_project_cache(enable: bool = True) -> None:
    configure_cache(
        enable_disk_cache=enable,
        enable_memory_cache=enable,
        disk_cache_dir=str(CACHE_DIR),
    )


def clear_cache() -> None:
    """Wipe the LM response cache. The timing protocol requires this before each
    configuration-and-seed run, so wall time and LM calls are measured cold."""
    if CACHE_DIR.exists():
        shutil.rmtree(CACHE_DIR)
    use_project_cache(True)


def configure(seed: int = 0, cache: bool = True) -> dspy.LM:
    """Install the pinned LM as the global DSPy LM and return it."""
    use_project_cache(cache)
    lm = build_lm(seed=seed, cache=cache)
    dspy.configure(lm=lm)
    return lm


@dataclass
class Usage:
    lm_calls: int = 0
    cache_hits: int = 0

    @property
    def billable_calls(self) -> int:
        """Calls that actually hit the model. Cache hits are reported, not counted."""
        return self.lm_calls - self.cache_hits


@contextmanager
def count_calls():
    """Count LM requests and cache hits made inside the block.

    Measured as a delta over DSPy's global history rather than by wrapping the client, so
    it also captures calls made from inside an optimizer.
    """
    start = len(GLOBAL_HISTORY)
    usage = Usage()
    try:
        yield usage
    finally:
        entries = GLOBAL_HISTORY[start:]
        usage.lm_calls = len(entries)
        usage.cache_hits = sum(1 for e in entries if e.get("cache_hit"))
