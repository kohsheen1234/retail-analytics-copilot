"""CLI shell. Do not change the flags. Replace `answer_question` with your graph."""
from __future__ import annotations

import json
import random
from pathlib import Path

import click

from models import OutputRecord, QuestionRecord, ReviewPacket

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None


def seed_everything(seed: int) -> None:
    random.seed(seed)
    if np is not None:
        np.random.seed(seed)
    # Add: DSPy LM seed / Ollama sampler seed once you wire the model.


def answer_question(q: QuestionRecord, seed: int) -> OutputRecord:
    """Stub. Replace with a call into agent.graph_hybrid."""
    return OutputRecord(
        id=q.id,
        status="needs_review",
        review_packet=ReviewPacket(
            question=q.question,
            understood="stub agent, not implemented",
            blocker="graph not wired",
            considered=[],
            decision_needed="implement the agent",
        ),
    )


@click.command()
@click.option("--batch", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--out", required=True, type=click.Path(dir_okay=False))
@click.option("--seed", default=0, show_default=True, type=int)
def main(batch: str, out: str, seed: int) -> None:
    seed_everything(seed)
    Path("traces").mkdir(exist_ok=True)
    with open(batch, encoding="utf-8") as f_in, open(out, "w", encoding="utf-8") as f_out:
        for line in f_in:
            if not line.strip():
                continue
            q = QuestionRecord.model_validate_json(line)
            rec = answer_question(q, seed)
            f_out.write(rec.model_dump_json() + "\n")


if __name__ == "__main__":
    main()
