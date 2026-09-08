"""Demonstrations must actually reach the prompt.

This exists because they silently did not. `to_example` stored the answer as `gold_sql`
and omitted `feedback`; `GenerateSQL` declares `feedback` as an input and `sql` as its
output, and DSPy's chat adapter renders a demo only when it can fill every declared input
and output. So `LabeledFewShot(k=2)` set two demos on the predictor, `named_predictors()`
reported two demos, and the rendered prompt was **byte-identical** to the zero-shot
baseline - 4405 chars in both - producing identical predictions on all 15 dev examples.

A silent no-op optimizer is the worst possible failure here: every number in the results
table would have been real, reproducible, and meaningless. These tests assert the
mechanism rather than the score.
"""
from __future__ import annotations

import dspy
import pytest

from agent import config
from agent.datasets import load_examples
from agent.modules import NL2SQL

pytestmark = pytest.mark.skipif(not config.DB_PATH.exists(), reason="northwind.sqlite not present")


@pytest.fixture(scope="module")
def train():
    return load_examples("train")


@pytest.fixture(scope="module")
def probe():
    return load_examples("dev")[0].inputs()


def rendered(program, probe) -> str:
    msgs = dspy.ChatAdapter().format(program.generate.signature,
                                     program.generate.demos, probe)
    return "\n".join(m.get("content", "") for m in msgs)


def test_examples_carry_every_field_the_signature_declares(train):
    sig = NL2SQL().generate.signature
    ex = train[0]
    d = ex.toDict()
    for name in sig.input_fields:
        assert name in d and d[name] not in (None, ""), f"demo is missing input {name!r}"
    for name in sig.output_fields:
        assert name in d, f"demo is missing output {name!r}"


def test_inputs_match_the_signature_inputs(train):
    assert set(train[0].inputs().keys()) == set(NL2SQL().generate.signature.input_fields)


def test_compiled_prompt_differs_from_the_zero_shot_prompt(train, probe):
    """The regression itself: if these are equal, the optimizer is a no-op."""
    from dspy.teleprompt import LabeledFewShot
    base = rendered(NL2SQL(), probe)
    ctrl = rendered(LabeledFewShot(k=2).compile(NL2SQL(), trainset=train), probe)
    assert ctrl != base
    assert len(ctrl) > len(base) * 1.5, (len(base), len(ctrl))


def test_demo_sql_appears_verbatim_in_the_prompt(train, probe):
    from dspy.teleprompt import LabeledFewShot
    prog = LabeledFewShot(k=2).compile(NL2SQL(), trainset=train)
    text = rendered(prog, probe)
    for demo in prog.generate.demos:
        sql = (demo.toDict() if hasattr(demo, "toDict") else dict(demo)).get("sql", "")
        if sql:
            assert sql[:60] in text, "a demo's SQL never reached the prompt"


def test_two_demos_fit_the_pinned_context_and_three_do_not(train, probe):
    """MAX_DEMOS is set by num_ctx, not by taste. Measured, with 512 output reserved."""
    from dspy.teleprompt import LabeledFewShot
    import optimize

    def budget(k: int) -> int:
        prog = LabeledFewShot(k=k).compile(NL2SQL(), trainset=train)
        return len(rendered(prog, probe)) // 4 + config.NUM_PREDICT

    assert optimize.MAX_DEMOS == 2
    assert budget(2) < config.NUM_CTX, "two demos must fit"
    # three leaves under ~200 tokens of headroom, which a longer question would exhaust
    assert config.NUM_CTX - budget(3) < 250
