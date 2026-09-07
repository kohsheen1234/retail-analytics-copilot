"""The pinned generation settings must actually reach Ollama.

`num_ctx` is not in litellm's mapped OpenAI parameter list for the Ollama chat provider,
so it is worth pinning down that it is forwarded rather than silently dropped: running at
Ollama's 2048 default would truncate prompts and quietly degrade every answer, and the
assessment forbids changing the context size in either direction.

These tests do not call the model.
"""
from __future__ import annotations

from agent import config
from agent.lm import lm_kwargs


def test_settings_match_environment_md():
    assert config.NUM_CTX == 4096
    assert config.NUM_PREDICT == 512
    assert config.TEMPERATURE == 0.0
    assert config.TOP_P == 1.0
    assert config.TOP_K == 0
    assert config.MODEL_TAG == "phi3.5:3.8b-mini-instruct-q4_K_M"


def test_lm_kwargs_carry_every_pinned_option():
    kw = lm_kwargs(seed=0)
    assert kw["num_ctx"] == 4096          # -> ollama options.num_ctx
    assert kw["max_tokens"] == 512        # -> ollama options.num_predict
    assert kw["temperature"] == 0.0
    assert kw["top_p"] == 1.0
    assert kw["top_k"] == 0
    assert kw["seed"] == 0


def test_seed_is_threaded_through():
    assert lm_kwargs(seed=7)["seed"] == 7


def test_model_string_targets_the_pinned_tag():
    assert config.LM_MODEL == "ollama_chat/phi3.5:3.8b-mini-instruct-q4_K_M"
