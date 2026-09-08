"""CLI. Flags are exactly as the assessment specifies:

    python run_agent_hybrid.py --batch sample_questions_hybrid_eval.jsonl --out outputs_hybrid.jsonl

Inference never triggers optimization. The compiled state in `artifacts/best.json` is
loaded at startup if present; if it is absent the agent runs the uncompiled baseline and
records that fact in every trace, so a missing artifact is visible rather than silent.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

import click

from models import OutputRecord, QuestionRecord

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None


def seed_everything(seed: int) -> None:
    """Every source of run-to-run variation the determinism contract names."""
    random.seed(seed)
    if np is not None:
        np.random.seed(seed)
    # The Ollama sampler seed travels with the LM (agent/lm.py -> options.seed), and
    # temperature is 0, so decoding is greedy. DSPy demo ordering is fixed by the
    # compiled artifact, which is loaded rather than resampled at inference.


def load_program(deps, artifact: Path) -> tuple[bool, str]:
    """Load compiled state into the already-constructed module.

    Returns (using_baseline, note). The module architecture is instantiated first and
    state is loaded into it, which is what DSPy's state-only save requires.
    """
    if not artifact.exists():
        return True, (f"{artifact.name} not found; running the uncompiled baseline "
                      f"(zero-shot, no demonstrations)")
    try:
        deps.nl2sql.load(str(artifact))
        n = len(getattr(deps.nl2sql.generate, "demos", []) or [])
        return False, f"loaded compiled state from {artifact.name} ({n} demonstration(s))"
    except Exception as e:
        return True, f"failed to load {artifact.name} ({e}); falling back to the baseline"


@click.command()
@click.option("--batch", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--out", required=True, type=click.Path(dir_okay=False))
@click.option("--seed", default=0, show_default=True, type=int)
def main(batch: str, out: str, seed: int) -> None:
    seed_everything(seed)

    from agent import config, lm
    from agent.graph_hybrid import Deps, answer_question, build_deps
    from agent.trace import Tracer

    lm.configure(seed=seed)
    config.TRACES_DIR.mkdir(exist_ok=True)

    deps = build_deps()
    baseline, note = load_program(deps, config.BEST_ARTIFACT)
    deps.baseline_artifact = baseline
    click.echo(f"[startup] {note}")

    questions = [QuestionRecord.model_validate_json(line)
                 for line in Path(batch).read_text(encoding="utf-8").splitlines() if line.strip()]

    with open(out, "w", encoding="utf-8") as f_out:
        for q in questions:
            # Startup provenance belongs in every trace: which artifact answered this.
            tr = Tracer(q.id, deps.traces_dir)
            tr.event("startup", {"seed": seed, "model": config.MODEL_TAG},
                     {"artifact": note, "using_baseline": baseline,
                      "num_ctx": config.NUM_CTX, "temperature": config.TEMPERATURE}, 0)
            try:
                rec = answer_question(q, deps, seed=seed)
            except Exception as e:  # a crash must still produce a contract-valid line
                from models import ReviewPacket
                rec = OutputRecord(
                    id=q.id, status="needs_review", final_answer=None, sql="",
                    confidence=None, explanation="The agent failed while answering.",
                    repairs=0, citations=[],
                    review_packet=ReviewPacket(
                        question=q.question,
                        understood="unrecoverable error before an answer was produced",
                        blocker=f"{type(e).__name__}: {e}"[:300],
                        considered=[], decision_needed="Inspect the trace and re-run."),
                )
            f_out.write(rec.model_dump_json() + "\n")
            click.echo(f"  {q.id}: {rec.status}"
                       + (f" conf={rec.confidence}" if rec.confidence is not None else "")
                       + f" repairs={rec.repairs}")


if __name__ == "__main__":
    main()
