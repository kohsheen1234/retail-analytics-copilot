"""Run one optimizer configuration and write its artifact.

    python optimize.py --seed 0 --config bootstrap

Writes `artifacts/<config>_seed<seed>.json` (DSPy state-only save) and
`artifacts/<config>_seed<seed>_dev.jsonl` (per-example scores, with the metric's reason
for every failure so the demo audit and the flip analysis are grounded in evidence
rather than recollection).

Determinism: `--seed` sets Python `random`, NumPy, the Ollama sampler seed, the DSPy
optimizer seed where the pinned version accepts one, and my own trainset shuffle.
Optimization may be stochastic; the seed is what makes a given run reproducible.

Timing protocol: the DSPy LM response cache is cleared before the run, so wall time and
LM calls are measured cold. Cache hits within the run are counted and reported
separately, since they do not count against the budget.
"""
from __future__ import annotations

import json
import random
import time
from pathlib import Path

import click
import dspy

from agent import config, lm
from agent.datasets import assert_no_leakage, load_examples
from agent.metrics import clear_execution_cache, sql_metric, sql_metric_verbose
from agent.modules import NL2SQL

# num_ctx is 4096 and cannot be raised. DSPy's chat adapter renders each demonstration
# with every input field, including the ~580-token live schema, so demo count is bounded
# by context rather than by taste: 2 demos leaves roughly 1.2k tokens of headroom for the
# real question plus 512 output tokens, 4 would overflow. This is the single most
# consequential number in this file and it is a property of the pinned environment.
MAX_DEMOS = 2


def build_module() -> NL2SQL:
    """The architecture the runtime also instantiates before loading state."""
    return NL2SQL()


def make_baseline(_train, _seed):
    return build_module()


def make_control(train, seed):
    """LabeledFewShot: gold demos, no model calls to construct them.

    This is the bar an optimizer has to clear. It is cheap (zero LM calls to compile) and
    its demos are correct by construction, because they are the gold SQL.
    """
    from dspy.teleprompt import LabeledFewShot
    return LabeledFewShot(k=MAX_DEMOS).compile(build_module(), trainset=train)


def make_bootstrap(train, seed):
    """BootstrapFewShot: the teacher writes candidate SQL, the metric filters it.

    The teacher defaults to a deepcopy of the student, so the teacher program *is* this
    same NL2SQL module and the teacher LM *is* the pinned phi3.5 -- the student teaches
    itself. Consequence: a demo can only be as good as something the pinned model already
    produces, so bootstrapping cannot introduce a join pattern the model never emits. It
    can only find the cases where the model got it right and pin them into the prompt.

    `max_rounds=1` on purpose: rounds beyond the first re-run the teacher at
    temperature 1.0 to bypass the cache, which would make the artifact depend on sampling
    and undermine the determinism the CLI has to satisfy.
    """
    from dspy.teleprompt import BootstrapFewShot
    opt = BootstrapFewShot(
        metric=sql_metric,
        max_bootstrapped_demos=MAX_DEMOS,
        max_labeled_demos=MAX_DEMOS,
        max_rounds=1,
        # metric_threshold deliberately unset: sql_metric returns a bool, so DSPy's
        # truthiness check is exactly the intended filter. Passing a threshold here
        # would be inert at best and, at 0.0, silently falsey.
    )
    return opt.compile(build_module(), trainset=train)


def make_bootstrap_rs(train, seed):
    """OPTIONAL O1: BootstrapFewShotWithRandomSearch over demo subsets."""
    from dspy.teleprompt import BootstrapFewShotWithRandomSearch
    opt = BootstrapFewShotWithRandomSearch(
        metric=sql_metric,
        max_bootstrapped_demos=MAX_DEMOS,
        max_labeled_demos=MAX_DEMOS,
        num_candidate_programs=4,
        max_rounds=1,
    )
    return opt.compile(build_module(), trainset=train)


CONFIGS = {
    "baseline": make_baseline,
    "control": make_control,
    "bootstrap": make_bootstrap,
    "bootstrap_rs": make_bootstrap_rs,
}


def evaluate(program, dev) -> tuple[float, list[dict]]:
    """Score every dev example, keeping the metric's reason for each failure."""
    rows: list[dict] = []
    for ex in dev:
        try:
            pred = program(**ex.inputs())
            outcome = sql_metric_verbose(ex, pred)
            rows.append({
                "id": ex.id, "score": int(outcome.ok), "reason": outcome.reason,
                "route": ex.route, "ordered": ex.ordered,
                "predicted_sql": getattr(pred, "sql", ""), "gold_sql": ex.gold_sql,
            })
        except Exception as e:
            rows.append({"id": ex.id, "score": 0, "reason": f"crashed: {type(e).__name__}: {e}",
                         "route": ex.route, "ordered": ex.ordered,
                         "predicted_sql": "", "gold_sql": ex.gold_sql})
    score = sum(r["score"] for r in rows) / max(1, len(rows))
    return score, rows


def demo_summary(program) -> list[dict]:
    """What actually ended up in the prompt. Feeds the README demo audit."""
    out = []
    for name, predictor in program.named_predictors():
        for i, demo in enumerate(getattr(predictor, "demos", []) or []):
            d = demo.toDict() if hasattr(demo, "toDict") else dict(demo)
            out.append({
                "predictor": name, "index": i,
                "augmented": bool(d.get("augmented")),
                "question": d.get("question", ""),
                "sql": d.get("sql", ""),
            })
    return out


@click.command()
@click.option("--config", "config_name", required=True, type=click.Choice(sorted(CONFIGS)))
@click.option("--seed", default=0, show_default=True, type=int)
@click.option("--promote/--no-promote", default=False,
              help="also write artifacts/best.json from this run")
def main(config_name: str, seed: int, promote: bool) -> None:
    assert_no_leakage()          # gate: eval questions must never reach train or dev

    random.seed(seed)
    try:
        import numpy as np
        np.random.seed(seed)
    except ImportError:
        pass

    lm.configure(seed=seed)
    lm.clear_cache()             # timing protocol: cold cache per config-and-seed
    clear_execution_cache()

    train = load_examples("train")
    dev = load_examples("dev")
    random.Random(seed).shuffle(train)   # demo sampling order, seeded

    click.echo(f"[{config_name} seed={seed}] train={len(train)} dev={len(dev)} "
               f"max_demos={MAX_DEMOS}")

    config.ARTIFACTS_DIR.mkdir(exist_ok=True)
    start = time.perf_counter()
    with lm.count_calls() as usage:
        program = CONFIGS[config_name](train, seed)
        score, rows = evaluate(program, dev)
    wall = time.perf_counter() - start

    state_path = config.ARTIFACTS_DIR / f"{config_name}_seed{seed}.json"
    program.save(str(state_path), save_program=False)      # state-only JSON
    dev_path = config.ARTIFACTS_DIR / f"{config_name}_seed{seed}_dev.jsonl"
    dev_path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    demos = demo_summary(program)
    summary = {
        "config": config_name, "seed": seed, "dev_score": round(score, 4),
        "dev_correct": sum(r["score"] for r in rows), "dev_total": len(rows),
        "lm_calls": usage.lm_calls, "cache_hits": usage.cache_hits,
        "billable_lm_calls": usage.billable_calls,
        "wall_seconds": round(wall, 1), "max_demos": MAX_DEMOS,
        "train_size": len(train), "demos": demos,
        "model": config.MODEL_TAG, "num_ctx": config.NUM_CTX,
    }
    (config.ARTIFACTS_DIR / f"{config_name}_seed{seed}_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    click.echo(f"  dev score : {score:.3f} ({summary['dev_correct']}/{len(rows)})")
    click.echo(f"  LM calls  : {usage.lm_calls} (cache hits {usage.cache_hits})")
    click.echo(f"  wall time : {wall:.1f}s")
    click.echo(f"  demos     : {len(demos)}")
    click.echo(f"  artifact  : {state_path}")
    for r in rows:
        click.echo(f"    {'PASS' if r['score'] else 'FAIL'}  {r['id']:44s} {r['reason'][:70]}")

    if promote:
        import shutil
        shutil.copyfile(state_path, config.BEST_ARTIFACT)
        click.echo(f"  promoted  -> {config.BEST_ARTIFACT}")


if __name__ == "__main__":
    main()
