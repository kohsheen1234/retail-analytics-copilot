"""Component evaluation harness: measure each choice instead of asserting it.

Sub-evaluations, all LM-free so they run in seconds and can be re-run on every change:

  retrieval  recall of gold_chunks at each k, with and without each expansion pass,
             over all 35 train+dev examples. Decides k and justifies the expansions.
  routing    rules-vs-labels accuracy, plus the cost of being wrong in each direction.
  static     what fraction of genuinely broken SQL the pre-execution checker catches,
             and - critically - its false-positive rate on the 33 gold statements.
  citations  whether the table extractor exactly covers gold_tables (the scored check).

Chunking is deliberately absent: the assessment fixes it so citations are comparable
across candidates, so it is a constant, not a hyperparameter.

    python scripts/evaluate.py [retrieval|routing|static|citations|all]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.datasets import load_split                      # noqa: E402
from agent.modules import rule_route                       # noqa: E402
from agent.retriever import Retriever                      # noqa: E402
from agent.schema import known_tables                      # noqa: E402
from agent.sql_analysis import physical_tables, schema_errors  # noqa: E402
from sqlite_tool import SQLiteTool                         # noqa: E402

DB = ROOT / "data" / "northwind.sqlite"


def examples() -> list[dict]:
    return load_split("train").records + load_split("dev").records


# --------------------------------------------------------------------- retrieval
def eval_retrieval() -> dict:
    """Recall of gold_chunks. Only examples that HAVE gold chunks can be scored."""
    retr = Retriever(ROOT / "docs")
    scored = [e for e in examples() if e.get("gold_chunks")]

    rows = []
    for k in (2, 3, 4, 5, 6, 8):
        for mode in ("bm25", "bm25+conflict", "bm25+conflict+definition"):
            hit = tot = 0
            for ex in scored:
                top = retr.bm25(ex["question"], k)
                ids = {h.chunk_id for h in top}
                if mode != "bm25":
                    ids |= {h.chunk_id for h in retr.expand_for_conflicts(ex["question"], top)}
                if mode == "bm25+conflict+definition":
                    ids |= {h.chunk_id for h in retr.expand_for_definitions(ex["question"], top)}
                gold = set(ex["gold_chunks"])
                hit += len(gold & ids)
                tot += len(gold)
            # context cost: mean chunks handed to the planner
            sizes = []
            for ex in examples():
                top = retr.bm25(ex["question"], k)
                n = len(top)
                if mode != "bm25":
                    n += len(retr.expand_for_conflicts(ex["question"], top))
                if mode == "bm25+conflict+definition":
                    n += len(retr.expand_for_definitions(ex["question"], top))
                sizes.append(n)
            rows.append({"k": k, "mode": mode, "recall": round(hit / tot, 4),
                         "gold_chunks_found": hit, "gold_chunks_total": tot,
                         "mean_chunks_to_planner": round(sum(sizes) / len(sizes), 2)})
    return {"scored_examples": len(scored), "rows": rows}


# ----------------------------------------------------------------------- routing
def eval_routing() -> dict:
    exs = examples()
    conf: dict[str, int] = {}
    for ex in exs:
        conf[f"{ex['route']}->{rule_route(ex['question'])}"] = \
            conf.get(f"{ex['route']}->{rule_route(ex['question'])}", 0) + 1
    correct = sum(n for k, n in conf.items() if k.split("->")[0] == k.split("->")[1])
    return {"n": len(exs), "accuracy": round(correct / len(exs), 4),
            "confusion": dict(sorted(conf.items())),
            "disagreements": [
                {"id": ex["id"], "label": ex["route"], "rules": rule_route(ex["question"]),
                 "gold_chunks": ex.get("gold_chunks")}
                for ex in exs if rule_route(ex["question"]) != ex["route"]]}


# ------------------------------------------------------------------ static check
BROKEN_SQL = [
    # every one of these was actually produced by phi3.5 during development
    "SELECT Customers.CustomerID FROM Orders JOIN Products ON Orders.ProductID = Products.ProductID",
    "SELECT p.Discontins FROM Products p",
    ('SELECT ROUND(SUM(od.UnitPrice*od.Quantity),2) FROM Orders o '
     'JOIN "Order Details" od ON o.OrderID=od.OrderID WHERE c.CategoryName=\'Beverages\''),
    "SELECT COUNT(*) FROM Orders o WHERE o.ShipNation='France'",
    "SELECT o.OrderTotal FROM Orders o",
]


def eval_static() -> dict:
    schema = SQLiteTool(DB).schema()
    caught = sum(1 for s in BROKEN_SQL if schema_errors(s, schema))
    gold = [e["gold_sql"] for e in examples() if (e.get("gold_sql") or "").strip()]
    false_pos = [s for s in gold if schema_errors(s, schema)]
    return {"broken_detected": f"{caught}/{len(BROKEN_SQL)}",
            "gold_statements_checked": len(gold),
            "false_positives": len(false_pos),
            "false_positive_sql": false_pos[:3]}


# -------------------------------------------------------------------- citations
def eval_citations() -> dict:
    tables = known_tables(DB)
    exact = 0
    bad = []
    gold = [e for e in examples() if (e.get("gold_sql") or "").strip()]
    for ex in gold:
        got = physical_tables(ex["gold_sql"], tables)
        if got == sorted(ex["gold_tables"]):
            exact += 1
        else:
            bad.append({"id": ex["id"], "got": got, "expected": sorted(ex["gold_tables"])})
    return {"exact": f"{exact}/{len(gold)}", "mismatches": bad}


EVALS = {"retrieval": eval_retrieval, "routing": eval_routing,
         "static": eval_static, "citations": eval_citations}


def main() -> None:
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    names = list(EVALS) if which == "all" else [which]
    out = {}
    for name in names:
        out[name] = EVALS[name]()
    (ROOT / "artifacts" / "component_eval.json").write_text(
        json.dumps(out, indent=2) + "\n", encoding="utf-8")

    if "retrieval" in out:
        print("RETRIEVAL  recall of gold_chunks "
              f"({out['retrieval']['scored_examples']} examples with gold chunks)")
        print(f"  {'k':>2}  {'mode':28s} {'recall':>7}  {'found':>9}  {'chunks->planner':>16}")
        for r in out["retrieval"]["rows"]:
            print(f"  {r['k']:>2}  {r['mode']:28s} {r['recall']:>7.3f}  "
                  f"{r['gold_chunks_found']:>4}/{r['gold_chunks_total']:<4} "
                  f"{r['mean_chunks_to_planner']:>16.2f}")
    if "routing" in out:
        r = out["routing"]
        print(f"\nROUTING    rules vs provided labels: {r['accuracy']:.3f} ({r['n']} examples)")
        for k, v in r["confusion"].items():
            print(f"  {k:22s} {v}")
        for d in r["disagreements"]:
            print(f"  disagreement: {d['id']} label={d['label']} rules={d['rules']} "
                  f"gold_chunks={d['gold_chunks']}")
    if "static" in out:
        s = out["static"]
        print(f"\nSTATIC     broken SQL detected pre-execution: {s['broken_detected']}")
        print(f"           false positives on {s['gold_statements_checked']} gold "
              f"statements: {s['false_positives']}")
    if "citations" in out:
        c = out["citations"]
        print(f"\nCITATIONS  physical-table extraction exactly matches gold_tables: {c['exact']}")
        for m in c["mismatches"]:
            print(f"  {m}")
    print("\nwrote artifacts/component_eval.json")


if __name__ == "__main__":
    main()
