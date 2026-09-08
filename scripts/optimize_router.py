"""OPTIONAL O2: optimize the Router with its own metric, before/after on dev.

Four configurations on the same dev split used for NL-to-SQL:

  rules            the shipped deterministic router (no LM, no compile)
  lm_baseline      the LM classifier, zero-shot          <- "before"
  lm_labeled       LabeledFewShot demos                  <- "after" (cheap)
  lm_bootstrap     BootstrapFewShot, filtered by router_metric

The comparison that matters is not lm_baseline vs lm_labeled but whether *any* LM
configuration beats `rules`, because rules is what ships. Reported either way.

    python scripts/optimize_router.py [--seed 0]
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import dspy  # noqa: E402

from agent import config, lm  # noqa: E402
from agent.datasets import load_split  # noqa: E402
from agent.metrics import router_metric  # noqa: E402
from agent.modules import Router, rule_route  # noqa: E402

INCONSISTENT = {"train_top3_categories_revenue"}   # documented in agent/metrics.py


def to_examples(records: list[dict]) -> list[dspy.Example]:
    return [
        dspy.Example(id=r["id"], question=r["question"], route=r["route"]).with_inputs("question")
        for r in records if r.get("route")
    ]


def score(fn, examples) -> tuple[float, list[dict]]:
    rows = []
    for ex in examples:
        try:
            pred = fn(ex.question)
        except Exception as e:
            pred = f"error: {type(e).__name__}"
        ok = router_metric(ex, dspy.Prediction(route=pred if isinstance(pred, str) else ""))
        rows.append({"id": ex.id, "label": ex.route, "predicted": pred, "score": int(ok)})
    return sum(r["score"] for r in rows) / max(1, len(rows)), rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    random.seed(args.seed)
    lm.configure(seed=args.seed)
    lm.clear_cache()

    train = to_examples(load_split("train").records)
    dev = to_examples(load_split("dev").records)
    print(f"router O2: train={len(train)} dev={len(dev)} seed={args.seed}")

    results = {}

    # rules: no LM at all
    t0 = time.perf_counter()
    with lm.count_calls() as u:
        acc, rows = score(rule_route, dev)
    results["rules"] = dict(dev_accuracy=round(acc, 4), lm_calls=u.lm_calls,
                            cache_hits=u.cache_hits, wall_seconds=round(time.perf_counter() - t0, 1),
                            per_example=rows)

    def lm_runner(program):
        return lambda q: (program(question=q).route or "")

    configs: list[tuple[str, object]] = [("lm_baseline", Router(use_lm=True))]

    from dspy.teleprompt import BootstrapFewShot, LabeledFewShot
    configs.append(("lm_labeled",
                    LabeledFewShot(k=4).compile(Router(use_lm=True), trainset=train)))
    configs.append(("lm_bootstrap",
                    BootstrapFewShot(metric=router_metric, max_bootstrapped_demos=4,
                                     max_labeled_demos=4, max_rounds=1)
                    .compile(Router(use_lm=True), trainset=train)))

    for name, program in configs:
        lm.clear_cache()
        t0 = time.perf_counter()
        with lm.count_calls() as u:
            acc, rows = score(lm_runner(program), dev)
        results[name] = dict(dev_accuracy=round(acc, 4), lm_calls=u.lm_calls,
                             cache_hits=u.cache_hits,
                             wall_seconds=round(time.perf_counter() - t0, 1),
                             demos=len(getattr(program.classify, "demos", []) or []),
                             per_example=rows)

    # accuracy excluding the one example whose provided label contradicts its siblings
    for name, r in results.items():
        kept = [x for x in r["per_example"] if x["id"] not in INCONSISTENT]
        r["dev_accuracy_excluding_inconsistent_label"] = round(
            sum(x["score"] for x in kept) / max(1, len(kept)), 4)

    out = {"seed": args.seed, "metric": "router_metric (exact route match, returns bool)",
           "note": ("The provided route labels are not self-consistent; ~0.97 is the "
                    "practical ceiling for any self-consistent classifier. See "
                    "agent/metrics.router_metric."),
           "configurations": results}
    path = config.ARTIFACTS_DIR / f"router_o2_seed{args.seed}.json"
    path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")

    print(f"\n{'config':14s} {'dev acc':>8} {'excl.incons':>12} {'demos':>6} {'lm_calls':>9} {'wall_s':>8}")
    for name in ("rules", "lm_baseline", "lm_labeled", "lm_bootstrap"):
        r = results[name]
        print(f"{name:14s} {r['dev_accuracy']:>8.3f} "
              f"{r['dev_accuracy_excluding_inconsistent_label']:>12.3f} "
              f"{r.get('demos', 0):>6} {r['lm_calls']:>9} {r['wall_seconds']:>8.1f}")
    print(f"\nwrote {path.name}")


if __name__ == "__main__":
    main()
