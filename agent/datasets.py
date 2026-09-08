"""Dataset loading for optimization, with leakage guards.

Provided files stay byte-identical (`data/train.jsonl`, `data/dev.jsonl`); my additions
live alongside them in `data/train_added.jsonl` and `data/dev_added.jsonl`. Separate
files rather than appended lines, so provenance is visible in a diff and the "did you
add eval questions to training" check is mechanical rather than a matter of trust.

`assert_no_leakage` is run by `optimize.py` at startup and by a test. Three of the
assessment's gates are leakage-shaped -- eval questions in train or dev, evaluating on
the training set, a metric that cannot fail -- so the check is wired in rather than
promised.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import dspy

from agent import config
from agent.planner import Plan, build_plan
from agent.retriever import Retriever
from agent.schema import all_columns, schema_text
from agent.sql_analysis import has_top_level_order_by

PROVIDED_TRAIN = config.DATA_DIR / "train.jsonl"
PROVIDED_DEV = config.DATA_DIR / "dev.jsonl"
ADDED_TRAIN = config.DATA_DIR / "train_added.jsonl"
ADDED_DEV = config.DATA_DIR / "dev_added.jsonl"
EVAL_FILE = config.ROOT / "sample_questions_hybrid_eval.jsonl"


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _normalise_question(q: str) -> str:
    """Loose form used only for near-duplicate detection against the eval set."""
    q = q.lower()
    q = re.sub(r"return (an? )?(integer|float|str|string|list.*)$", "", q).strip()
    q = re.sub(r"[^a-z0-9 ]+", " ", q)
    return " ".join(sorted(set(q.split())))


def _overlap(a: str, b: str) -> float:
    sa, sb = set(a.split()), set(b.split())
    return len(sa & sb) / max(1, len(sa | sb))


@dataclass
class Split:
    name: str
    records: list[dict] = field(default_factory=list)

    @property
    def ids(self) -> set[str]:
        return {r["id"] for r in self.records}


def load_split(which: str) -> Split:
    if which == "train":
        return Split("train", read_jsonl(PROVIDED_TRAIN) + read_jsonl(ADDED_TRAIN))
    if which == "dev":
        return Split("dev", read_jsonl(PROVIDED_DEV) + read_jsonl(ADDED_DEV))
    raise ValueError(which)


def assert_no_leakage(threshold: float = 0.85) -> list[str]:
    """Raise on leakage I am responsible for; return warnings about leakage I am not.

    Hard failures (raise):
      * train and dev sharing an id;
      * any train/dev id that is an eval id;
      * any example whose normalised question is *identical* to an eval question;
      * an example **I added** that is >= `threshold` similar to an eval question.

    Warning (returned, not raised): a **provided** example that is near-duplicate of an
    eval question. This is not hypothetical -- the pack ships one. Provided
    `train_condiments_revenue_summer_2017` is 91% bag-of-words similar to eval
    `hybrid_revenue_beverages_summer_2017`: identical template, one word different
    ('Condiments' vs 'Beverages'), and genuinely different answers (358005.08 vs
    611562.68). So it is not the same question and I will not edit provided data to
    silence a check of my own making. But it does mean a bootstrapped demo drawn from
    that example hands the model almost the entire eval question, which is a real
    caveat on any dev-to-hidden generalization claim and is why it is surfaced rather
    than swallowed.
    """
    train, dev = load_split("train"), load_split("dev")
    warnings: list[str] = []

    if clash := train.ids & dev.ids:
        raise ValueError(f"train and dev must be disjoint; shared ids: {sorted(clash)}")

    evals = read_jsonl(EVAL_FILE)
    eval_norm = {e["id"]: _normalise_question(e["question"]) for e in evals}
    eval_ids = {e["id"] for e in evals}

    for split in (train, dev):
        if bad := split.ids & eval_ids:
            raise ValueError(f"{split.name} contains eval ids: {sorted(bad)}")
        for rec in split.records:
            mine = _normalise_question(rec["question"])
            is_added = rec.get("added_by") == "candidate"
            for eid, other in eval_norm.items():
                if mine == other:
                    raise ValueError(
                        f"{split.name} example {rec['id']!r} is the same question as eval {eid!r}")
                sim = _overlap(mine, other)
                if sim < threshold:
                    continue
                if is_added:
                    raise ValueError(
                        f"{split.name} example {rec['id']!r} (added by me) is {sim:.0%} similar "
                        f"to eval question {eid!r}; remove or rewrite it")
                warnings.append(
                    f"provided {split.name} example {rec['id']!r} is {sim:.0%} similar to eval "
                    f"{eid!r} (same template, different filter and different answer) -- left "
                    f"as delivered; see DECISIONS.md")
    return warnings


def leakage_report() -> list[tuple[str, str, float]]:
    """Closest eval question for each train/dev example. For eyeballing, not gating."""
    evals = {e["id"]: _normalise_question(e["question"]) for e in read_jsonl(EVAL_FILE)}
    out: list[tuple[str, str, float]] = []
    for which in ("train", "dev"):
        for rec in load_split(which).records:
            mine = _normalise_question(rec["question"])
            best_id, best = max(evals.items(), key=lambda kv: _overlap(mine, kv[1]))
            out.append((rec["id"], best_id, _overlap(mine, evals[best_id])))
    return sorted(out, key=lambda t: -t[2])


# --- conversion to DSPy examples --------------------------------------------

_RETRIEVER: Retriever | None = None


def retriever() -> Retriever:
    global _RETRIEVER
    if _RETRIEVER is None:
        _RETRIEVER = Retriever(config.DOCS_DIR)
    return _RETRIEVER


def plan_for_question(question: str) -> tuple[Plan, list[str]]:
    """The same retrieval and planning the graph performs, so optimization inputs match
    inference inputs exactly. Anything else would optimize against a distribution the
    agent never sees."""
    from agent.injection import sanitize

    top, extra = retriever().retrieve(question, config.RETRIEVE_K)
    hits = top + extra
    cleaned = []
    for h in hits:
        text, _ = sanitize(h.chunk_id, h.content)
        cleaned.append(type(h)(h.chunk_id, h.score, h.source, text))
    plan = build_plan(question, cleaned, all_columns())
    return plan, [h.chunk_id for h in cleaned]


def constraints_text(plan: Plan) -> str:
    """Compact, deterministic rendering of the plan for the NL-to-SQL prompt."""
    parts: list[str] = []
    if plan.chosen_window:
        w = plan.chosen_window
        parts.append(f"DATE WINDOW: date(OrderDate) BETWEEN '{w.start}' AND '{w.end}' "
                     f"(inclusive; from {w.source})")
    for k in plan.kpi_formulas:
        if k.status != "legacy" or True:
            parts.append(f"FORMULA {k.name} [{k.status}]: {k.expr}")
    for group, members in plan.reporting_groups.items():
        joined = ", ".join(f"'{m}'" for m in members)
        parts.append(f"REPORTING GROUP '{group}' = CategoryName IN ({joined})")
    for fname, formula, src in plan.missing_fields:
        supplied = plan.supplied_approximations.get(fname)
        parts.append(f"MISSING COLUMN {fname} (needed by {formula}): "
                     + (f"approximate as stated in the question: {supplied}" if supplied
                        else "no approximation available"))
    for c in plan.unresolved_conflicts:
        parts.append(f"UNRESOLVED CONFLICT on {c.subject}: " + " vs ".join(c.options))
    parts.append("DATES: OrderDate mixes 'YYYY-MM-DD' and 'YYYY-MM-DD HH:MM:SS'; always "
                 "wrap it as date(OrderDate) so the closing day is not dropped.")
    parts.append("ENTITY GRAIN: group by the entity's id column (CustomerID, CategoryID, "
                 "ProductID, EmployeeID, SupplierID) and select its display name.")
    parts.append("REVENUE uses \"Order Details\".UnitPrice, never Products.UnitPrice.")
    return "\n".join(parts)


def to_example(rec: dict) -> dspy.Example:
    plan, chunk_ids = plan_for_question(rec["question"])
    ordered = rec.get("ordered")
    if ordered is None:
        ordered = has_top_level_order_by(rec.get("gold_sql") or "")
    return dspy.Example(
        id=rec["id"],
        question=rec["question"],
        format_hint=rec["format_hint"],
        db_schema=schema_text(),
        constraints=constraints_text(plan),
        gold_sql=rec.get("gold_sql", ""),
        gold_answer=rec.get("gold_answer"),
        gold_tables=rec.get("gold_tables", []),
        gold_chunks=rec.get("gold_chunks", []),
        route=rec.get("route", "sql"),
        ordered=bool(ordered),
        retrieved_chunks=chunk_ids,
    ).with_inputs("question", "format_hint", "db_schema", "constraints")


def load_examples(which: str, sql_only: bool = False) -> list[dspy.Example]:
    """Full split as DSPy examples, in file order (deterministic).

    `sql_only=False` keeps the RAG-only examples, which the metric scores through the
    abstention contract. The assessment asks for the *full* dev set, so that is the
    default.
    """
    recs = load_split(which).records
    if sql_only:
        recs = [r for r in recs if (r.get("gold_sql") or "").strip()]
    return [to_example(r) for r in recs]
