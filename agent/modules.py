"""DSPy modules, plus the deterministic post-processing around them.

A deliberate boundary, documented in DECISIONS.md: the LM writes SQL and prose; it never
constructs the typed answer, the citations or the confidence. Those are computed by code
from the executed rows. Three reasons.

  * Determinism is gated. `final_answer`, `sql`, `citations`, `assumptions` and
    `repairs` must be identical across two fresh runs, while `explanation` wording is
    explicitly exempt. That exemption is a strong hint about where model text is safe.
  * Contract compliance is mechanical. `final_answer` must match `format_hint` exactly
    and table citations must exactly cover the executed SQL's physical tables. Both are
    decidable from the rows and the SQL, so asking a 3.8B model to get them right is
    strictly worse than computing them.
  * Calibration needs a rubric. A model asked for its own confidence returns a number
    with no relationship to its error rate.

`NL2SQL` holds exactly one predictor. Repair feedback arrives through a signature field
rather than a second predictor, so the module architecture is stable for state loading
and so bootstrapped demos apply to the retry path too.
"""
from __future__ import annotations

import re

import dspy

from agent.signatures import ExplainAnswer, ExtractFromDocs, GenerateSQL, RouteQuestion

_FENCE = re.compile(r"```(?:sql|sqlite)?\s*(.*?)\s*```", re.S | re.I)
_LEAD_LABEL = re.compile(r"^\s*(?:sql|query|answer|output)\s*[:=]\s*", re.I)
_SELECT_START = re.compile(r"\b(with|select)\b", re.I)
_SQL_TOKEN = re.compile(
    r"\b(select|from|where|join|on|group|order|by|having|limit|and|or|as|sum|count|avg|"
    r"min|max|round|distinct|case|when|then|else|end|between|in|not|null|like|date|"
    r"strftime|union|left|inner|outer|desc|asc|offset)\b", re.I)
# A prose sentence: starts capitalised, contains a space, ends in . : or !
_PROSE_LINE = re.compile(r"^[A-Z][^\n]*[ ][^\n]*[.:!]$")


def clean_sql(raw: str | None) -> str:
    """Recover a single bare statement from whatever the model emitted.

    phi3.5 at temperature 0 still wraps SQL in fences, prefixes 'SQL:', and appends
    commentary. This is deterministic string surgery, applied before the execution
    boundary sees anything; the boundary remains the only thing deciding what is safe to
    run.
    """
    if not raw:
        return ""
    text = raw.strip()
    if m := _FENCE.search(text):
        text = m.group(1).strip()
    text = _LEAD_LABEL.sub("", text).strip()
    # Drop anything before the first SELECT/WITH: models like to narrate first.
    if m := _SELECT_START.search(text):
        text = text[m.start():]
    else:
        return ""
    # Cut at the first statement terminator, so trailing prose or a second statement is
    # removed here rather than rejected downstream.
    if ";" in text:
        text = text.split(";", 1)[0]
    # Cut trailing model commentary. phi3.5 routinely appends an explanatory sentence
    # on its own line; a sentence is a line with no SQL keyword that reads like prose.
    kept: list[str] = []
    for line in text.splitlines():
        if kept and _PROSE_LINE.match(line.strip()) and not _SQL_TOKEN.search(line):
            break
        kept.append(line)
    text = "\n".join(kept).strip()
    return re.sub(r"\s+", " ", text).strip()


class NL2SQL(dspy.Module):
    """The module the assessment asks to optimize. One predictor, by design."""

    def __init__(self):
        super().__init__()
        self.generate = dspy.Predict(GenerateSQL)

    def forward(self, question: str, format_hint: str, db_schema: str,
                constraints: str, feedback: str = "") -> dspy.Prediction:
        out = self.generate(question=question, format_hint=format_hint, db_schema=db_schema,
                            constraints=constraints, feedback=feedback or "none")
        return dspy.Prediction(sql=clean_sql(getattr(out, "sql", "")),
                               raw_sql=getattr(out, "sql", ""))


class DocAnswer(dspy.Module):
    """RAG-only extraction. Returns the raw string; typing happens in agent/answer.py."""

    def __init__(self):
        super().__init__()
        self.extract = dspy.Predict(ExtractFromDocs)

    def forward(self, question: str, format_hint: str, context: str) -> dspy.Prediction:
        out = self.extract(question=question, format_hint=format_hint, context=context)
        value = (getattr(out, "value", "") or "").strip()
        return dspy.Prediction(value=value, insufficient=value.upper().startswith("INSUFFICIENT"))


class Explainer(dspy.Module):
    """Prose only. `explanation` wording is exempt from the determinism comparison, which
    is precisely why this is the one place model text reaches the output unfiltered."""

    def __init__(self):
        super().__init__()
        self.explain = dspy.Predict(ExplainAnswer)

    def forward(self, question: str, evidence: str) -> dspy.Prediction:
        out = self.explain(question=question, evidence=evidence)
        text = (getattr(out, "explanation", "") or "").strip()
        text = re.sub(r"\s+", " ", text)
        # Two sentences maximum, per the contract.
        parts = re.split(r"(?<=[.!?])\s+", text)
        return dspy.Prediction(explanation=" ".join(parts[:2])[:400])


class Router(dspy.Module):
    """Deterministic by default; the LM classifier is opt-in.

    This started as an LM router with a rule fallback and was inverted on evidence. On
    the first end-to-end run the LM router cost **22 seconds per question** and got the
    very first question wrong: it labelled "According to the product policy, what is the
    return window..." as `hybrid` rather than `rag`, which sent a pure document lookup
    down the SQL path, burned all three NL-to-SQL attempts and escalated a question whose
    answer is sitting in `product_policy::chunk1`.

    The rule prior got that question right, agrees with 34 of the 35 provided `route`
    labels, and costs nothing. The single disagreement is `train_top3_categories_revenue`,
    which the pack labels `sql` while labelling five structurally identical revenue
    questions `hybrid` -- so the label set is not self-consistent enough to justify 22
    seconds and a wrong answer.

    Latency is not a side issue: the hidden set must complete in under 5 minutes, and at
    22s per question routing alone would consume roughly half that budget before any SQL
    is written. The LM path is kept, tested, and available via `use_lm=True` for the O2
    optional task, but it is not what ships.
    """

    VALID = ("rag", "sql", "hybrid")

    def __init__(self, use_lm: bool = False):
        super().__init__()
        self.use_lm = use_lm
        self.classify = dspy.Predict(RouteQuestion)

    def forward(self, question: str) -> dspy.Prediction:
        prior = rule_route(question)
        if not self.use_lm:
            return dspy.Prediction(route=prior, lm_route=None, rule_route=prior,
                                   used_fallback=False, decided_by="rules")
        try:
            out = self.classify(question=question)
            raw = (getattr(out, "route", "") or "").strip().lower()
        except Exception:
            raw = ""
        match = next((v for v in self.VALID if re.search(rf"\b{v}\b", raw)), None)
        return dspy.Prediction(route=match or prior, lm_route=raw or None,
                               rule_route=prior, used_fallback=match is None,
                               decided_by="lm" if match else "rules-fallback")


_DOC_ONLY = re.compile(
    r"\b(return window|returns?|refund|policy|perishable|non-perishable|unopened|opened)\b", re.I)
# Terms whose meaning is fixed by a document rather than by the schema. Bare "revenue"
# and "margin" are included: there is no Revenue column, so any revenue question is
# answered through kpi_definitions::chunk3's formula and should cite it. The provided
# labels are inconsistent on this - five revenue examples are labelled `hybrid` with
# gold_chunks ["kpi_definitions::chunk3"], while train_top3_categories_revenue is
# labelled `sql` with gold_chunks [] despite using the identical formula. Following the
# majority: retrieving the KPI chunk for a revenue question is correct, and the cost of
# being wrong is asymmetric (a little context vs a missing citation).
_DOC_RULE = re.compile(
    r"\b(kpi|aov|average order value|gross margin|margin|revenue|reporting group|"
    r"pantry|campaign|marketing calendar|summer beverages|winter classics|legacy)\b", re.I)
# Aggregation intent. Deliberately excludes "maximum"/"minimum": "the maximum return
# window in days" is a document lookup, not a database aggregate, and the provided
# dev_policy_perishables_max_days is labelled `rag`.
_AGG = re.compile(
    r"\b(how many|how much|total|count|sum|average|avg|top \d+|highest|lowest|most|"
    r"least|number of|percentage|share|per |by )\b", re.I)
# Entity nouns only count as database intent alongside aggregation intent. On their own
# they misfire: "the product policy" contains "product".
_ENTITY = re.compile(
    r"\b(orders?|customers?|products?|suppliers?|employees?|categor(?:y|ies)|shippers?|"
    r"freight|revenue|quantity|margin|aov|line items?)\b", re.I)


def rule_route(question: str) -> str:
    """Deterministic prior. Cheap, testable, and never worse than a coin flip.

    Bias: when a document rule and database work are both plausible, prefer `hybrid`.
    Retrieving documents for a pure-SQL question costs a little context; skipping
    retrieval on a document-dependent question loses the citation and usually the answer.
    """
    agg = bool(_AGG.search(question))
    entity = bool(_ENTITY.search(question))
    doc_rule = bool(_DOC_RULE.search(question))

    # A policy or returns question with no aggregation is a document lookup, even though
    # "product policy" contains an entity noun.
    if _DOC_ONLY.search(question) and not agg:
        return "rag"
    if doc_rule and (agg or entity):
        return "hybrid"
    if agg or entity:
        return "sql"
    return "rag"
