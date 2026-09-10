"""Measure end-to-end accuracy against reported confidence.

`confidence` is defined as the probability the answer is correct, and the assessment
compares confidence against correctness across all questions. That makes both directions
an error: over-confidence on wrong answers, and under-confidence on right ones. The rubric
in agent/answer.py started from an assumed base rate; this measures the real one.

Runs the full agent over the dev set (which has gold answers) and reports a reliability
table.

    python scripts/calibration.py
"""
from __future__ import annotations

import json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent import config, lm                                    # noqa: E402
from agent.datasets import load_split                           # noqa: E402
from agent.graph_hybrid import answer_question, build_deps      # noqa: E402
from models import QuestionRecord                               # noqa: E402


def matches(got, gold) -> bool:
    """Element-wise comparison with float tolerance, mirroring the grading contract."""
    if isinstance(gold, list):
        return (isinstance(got, list) and len(got) == len(gold)
                and all(matches(g, w) for g, w in zip(got, gold)))
    if isinstance(gold, dict):
        return (isinstance(got, dict) and set(got) == set(gold)
                and all(matches(got[k], gold[k]) for k in gold))
    if isinstance(gold, bool):
        return got is gold
    if isinstance(gold, (int, float)):
        try:
            return abs(float(got) - float(gold)) <= 0.011
        except (TypeError, ValueError):
            return False
    return str(got).strip() == str(gold).strip()


def main() -> None:
    lm.configure(seed=0)
    deps = build_deps()
    deps.traces_dir = ROOT / "traces_calibration"
    from run_agent_hybrid import load_program
    baseline, note = load_program(deps, config.BEST_ARTIFACT)
    deps.baseline_artifact = baseline
    print(f"[startup] {note}")

    rows = []
    for rec in load_split("dev").records:
        q = QuestionRecord(id=rec["id"], question=rec["question"],
                           format_hint=rec["format_hint"])
        out = answer_question(q, deps)
        correct = (out.status == "answered"
                   and matches(out.final_answer, rec["gold_answer"]))
        rows.append({"id": rec["id"], "status": out.status,
                     "confidence": out.confidence, "correct": bool(correct),
                     "repairs": out.repairs,
                     "assumptions": len(out.assumptions)})
        print(f"  {'OK ' if correct else 'BAD'} {rec['id']:42s} "
              f"conf={out.confidence} status={out.status}")

    answered = [r for r in rows if r["status"] == "answered"]
    acc = sum(r["correct"] for r in answered) / max(1, len(answered))
    mean_conf = sum(r["confidence"] for r in answered) / max(1, len(answered))
    print(f"\n  answered        : {len(answered)}/{len(rows)}")
    print(f"  accuracy        : {acc:.3f}")
    print(f"  mean confidence : {mean_conf:.3f}")
    print(f"  calibration gap : {mean_conf - acc:+.3f}  "
          f"({'over' if mean_conf > acc else 'under'}-confident)")

    (config.ARTIFACTS_DIR / "calibration.json").write_text(
        json.dumps({"accuracy": round(acc, 4), "mean_confidence": round(mean_conf, 4),
                    "gap": round(mean_conf - acc, 4), "n_answered": len(answered),
                    "rows": rows}, indent=2) + "\n")
    print("  wrote artifacts/calibration.json")


if __name__ == "__main__":
    main()
