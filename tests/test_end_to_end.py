"""End-to-end runs through the real graph with the LM mocked.

The LM is replaced at the module boundary rather than at the HTTP boundary: the graph
takes its DSPy modules through `Deps`, so a stub with a scripted `forward` exercises every
real node - routing, retrieval, the injection quarantine, planning, the execution
boundary, synthesis, validation, the repair loop and the review gate - with no model and
no network. Everything except the model's text is the shipping code path.
"""
from __future__ import annotations

import json

import dspy
import pytest

from agent import config
from agent.graph_hybrid import Deps, answer_question, build_deps
from agent.modules import rule_route
from models import QuestionRecord

pytestmark = pytest.mark.skipif(not config.DB_PATH.exists(), reason="northwind.sqlite not present")

REVENUE = ('SELECT ROUND(SUM(od.UnitPrice*od.Quantity*(1-od.Discount)),2) '
           'FROM "Order Details" od JOIN Orders o ON o.OrderID=od.OrderID '
           'JOIN Products p ON p.ProductID=od.ProductID '
           'JOIN Categories c ON c.CategoryID=p.CategoryID '
           "WHERE c.CategoryName='Beverages' "
           "AND date(o.OrderDate) BETWEEN '2017-06-01' AND '2017-06-30'")


class ScriptedSQL(dspy.Module):
    """Returns queued SQL strings in order, so the repair loop can be driven exactly."""

    def __init__(self, *sqls: str):
        super().__init__()
        self.queue = list(sqls)
        self.calls: list[dict] = []

    def forward(self, question, format_hint, db_schema, constraints, feedback=""):
        self.calls.append({"question": question, "constraints": constraints, "feedback": feedback})
        sql = self.queue.pop(0) if self.queue else ""
        return dspy.Prediction(sql=sql, raw_sql=sql)


class ScriptedDoc(dspy.Module):
    def __init__(self, value: str):
        super().__init__()
        self.value = value
        self.contexts: list[str] = []

    def forward(self, question, format_hint, context):
        self.contexts.append(context)
        return dspy.Prediction(value=self.value,
                               insufficient=self.value.upper().startswith("INSUFFICIENT"))


class FixedExplainer(dspy.Module):
    def forward(self, question, evidence):
        return dspy.Prediction(explanation="Computed from the cited tables.")


class FixedRouter(dspy.Module):
    def forward(self, question):
        r = rule_route(question)
        return dspy.Prediction(route=r, lm_route=None, rule_route=r,
                               used_fallback=False, decided_by="rules")


@pytest.fixture
def deps(tmp_path):
    d = build_deps()
    d.explainer = FixedExplainer()
    d.router = FixedRouter()
    d.traces_dir = tmp_path / "traces"
    return d


def q(qid, question, hint):
    return QuestionRecord(id=qid, question=question, format_hint=hint)


def trace_of(deps, qid):
    return [json.loads(l) for l in (deps.traces_dir / f"{qid}.jsonl").read_text().splitlines()]


class TestHybridHappyPath:
    QUESTION = ("Total revenue from the 'Beverages' category during the 'Summer Beverages 2017' "
                "dates as defined in the marketing calendar. Return a float rounded to 2 decimals.")

    def test_answers_correctly_with_exact_citations(self, deps):
        deps.nl2sql = ScriptedSQL(REVENUE)
        rec = answer_question(q("e2e_rev", self.QUESTION, "float"), deps)

        assert rec.status == "answered"
        assert rec.final_answer == pytest.approx(611562.68, abs=0.01)
        assert rec.repairs == 0
        # table citations exactly cover the executed SQL, no more and no less
        assert set(t for t in rec.citations if "::" not in t) == {
            "Order Details", "Orders", "Products", "Categories"}
        # the calendar chunk that supplied the window is cited
        assert "marketing_calendar::chunk1" in rec.citations

    def test_records_the_date_conflict_as_an_assumption(self, deps):
        deps.nl2sql = ScriptedSQL(REVENUE)
        rec = answer_question(q("e2e_rev2", self.QUESTION, "float"), deps)
        joined = " ".join(rec.assumptions)
        assert "2017-07-07" in joined and "not applied" in joined

    def test_the_planner_hands_the_window_to_nl2sql(self, deps):
        deps.nl2sql = ScriptedSQL(REVENUE)
        answer_question(q("e2e_rev3", self.QUESTION, "float"), deps)
        constraints = deps.nl2sql.calls[0]["constraints"]
        assert "2017-06-01" in constraints and "2017-06-30" in constraints
        assert "date(OrderDate)" in constraints          # the mixed-format warning
        assert "Order Details\".UnitPrice" in constraints

    def test_confidence_is_a_probability_not_a_constant(self, deps):
        deps.nl2sql = ScriptedSQL(REVENUE)
        rec = answer_question(q("e2e_rev4", self.QUESTION, "float"), deps)
        assert 0.0 < rec.confidence <= 0.99

    def test_trace_covers_every_responsibility(self, deps):
        deps.nl2sql = ScriptedSQL(REVENUE)
        answer_question(q("e2e_rev5", self.QUESTION, "float"), deps)
        seen = {e["responsibility"] for e in trace_of(deps, "e2e_rev5")}
        assert {"routing", "retrieval", "planning", "nl2sql",
                "execution", "synthesis", "validation"} <= seen

    def test_trace_events_have_the_required_shape(self, deps):
        deps.nl2sql = ScriptedSQL(REVENUE)
        answer_question(q("e2e_rev6", self.QUESTION, "float"), deps)
        for e in trace_of(deps, "e2e_rev6"):
            assert e["ts"] and e["question_id"] == "e2e_rev6"
            assert isinstance(e["elapsed_ms"], int)
            assert isinstance(e["inputs"], dict) and isinstance(e["outputs"], dict)


class TestRepairLoop:
    QUESTION = "How many orders were shipped to France in 2019? Return an integer."
    GOOD = ("SELECT COUNT(*) FROM Orders o WHERE o.ShipCountry='France' "
            "AND date(o.OrderDate) BETWEEN '2019-01-01' AND '2019-12-31'")

    def test_repair_recovers_from_an_invented_column(self, deps):
        """The static checker catches it before execution and says which column is wrong."""
        bad = "SELECT COUNT(*) FROM Orders o WHERE o.ShipNation='France'"
        deps.nl2sql = ScriptedSQL(bad, self.GOOD)
        rec = answer_question(q("e2e_repair", self.QUESTION, "int"), deps)

        assert rec.status == "answered" and rec.repairs == 1
        assert isinstance(rec.final_answer, int)
        assert rec.sql == self.GOOD                       # the reported SQL is the last one
        feedback = deps.nl2sql.calls[1]["feedback"]
        assert "ShipNation" in feedback and "does not exist" in feedback

    def test_repair_is_visible_in_the_trace(self, deps):
        deps.nl2sql = ScriptedSQL("SELECT COUNT(*) FROM Orders o WHERE o.Nope=1", self.GOOD)
        answer_question(q("e2e_repair2", self.QUESTION, "int"), deps)
        events = trace_of(deps, "e2e_repair2")
        assert any(e["responsibility"] == "repair" for e in events)
        assert {e["attempt"] for e in events if e["responsibility"] == "nl2sql"} == {0, 1}

    def test_repairs_are_capped_at_two_and_then_escalate(self, deps):
        """Four bad attempts must not produce four repairs."""
        bad = "SELECT COUNT(*) FROM Orders o WHERE o.Nope=1"
        deps.nl2sql = ScriptedSQL(bad, bad, bad, bad)
        rec = answer_question(q("e2e_cap", self.QUESTION, "int"), deps)
        assert rec.status == "needs_review"
        assert rec.repairs == config.MAX_REPAIRS == 2
        assert rec.final_answer is None and rec.confidence is None

    def test_empty_result_where_rows_expected_triggers_repair(self, deps):
        empty = ("SELECT COUNT(*) FROM Orders o WHERE o.ShipCountry='Atlantis' "
                 "AND date(o.OrderDate) BETWEEN '2019-01-01' AND '2019-12-31'")
        # COUNT(*) always returns a row, so use a shape that genuinely returns none
        none_rows = "SELECT o.OrderID FROM Orders o WHERE o.ShipCountry='Atlantis'"
        deps.nl2sql = ScriptedSQL(none_rows, self.GOOD)
        rec = answer_question(q("e2e_empty", self.QUESTION, "int"), deps)
        assert rec.repairs == 1 and rec.status == "answered"


class TestInjectionEndToEnd:
    """The corpus tries to force 30 days for every category. Truth is 14."""

    QUESTION = ("According to the product policy, what is the return window in days for "
                "unopened Beverages? Return an integer.")

    def test_the_documented_value_wins_over_the_injected_instruction(self, deps):
        deps.nl2sql = ScriptedSQL()
        deps.doc_answer = ScriptedDoc("14")
        rec = answer_question(q("e2e_inj", self.QUESTION, "int"), deps)

        assert rec.final_answer == 14
        assert rec.sql == "" and not [c for c in rec.citations if "::" not in c]
        assert "product_policy::chunk1" in rec.citations

    def test_the_injected_line_never_reaches_the_model(self, deps):
        deps.nl2sql = ScriptedSQL()
        deps.doc_answer = ScriptedDoc("14")
        answer_question(q("e2e_inj2", self.QUESTION, "int"), deps)
        context = deps.doc_answer.contexts[0]
        assert "always reply 30 days" not in context
        assert "[redacted:" in context
        assert "UNTRUSTED" in context
        assert "14 days" in context                       # the real policy survived

    def test_the_quarantine_is_reported_as_an_assumption(self, deps):
        deps.nl2sql = ScriptedSQL()
        deps.doc_answer = ScriptedDoc("14")
        rec = answer_question(q("e2e_inj3", self.QUESTION, "int"), deps)
        assert any("instruction-like text" in a for a in rec.assumptions)

    def test_an_obedient_model_would_still_be_caught_by_the_trace(self, deps):
        """If extraction returned the injected 30, the quarantine record shows why it is
        suspect even though 30 is a real value elsewhere in the policy."""
        deps.nl2sql = ScriptedSQL()
        deps.doc_answer = ScriptedDoc("30")
        rec = answer_question(q("e2e_inj4", self.QUESTION, "int"), deps)
        assert rec.final_answer == 30                     # we do not second-guess extraction
        events = trace_of(deps, "e2e_inj4")
        quarantined = [e for e in events if e["outputs"].get("quarantined")]
        assert quarantined, "the trace must record what was suppressed"


class TestReviewGate:
    def test_unanswerable_gross_margin_escalates_with_a_usable_packet(self, deps):
        """CostOfGoods exists in the formula and in no table, and the corpus documents no
        approximation. Guessing a margin would be silently wrong."""
        deps.nl2sql = ScriptedSQL()
        rec = answer_question(q("e2e_gm",
            "Per the KPI definition of gross margin, what was the total gross margin in 2019? "
            "Return a float rounded to 2 decimals.", "float"), deps)

        assert rec.status == "needs_review"
        assert rec.final_answer is None and rec.confidence is None
        p = rec.review_packet
        assert p is not None
        assert "CostOfGoods" in p.blocker
        assert p.question and p.understood and p.decision_needed
        assert len(" ".join([p.question, p.understood, p.blocker,
                             " ".join(p.considered), p.decision_needed]).split()) < 150

    def test_a_supplied_approximation_makes_it_answerable(self, deps):
        sql = ('SELECT ROUND(SUM((od.UnitPrice-0.7*od.UnitPrice)*od.Quantity*(1-od.Discount)),2) '
               'FROM "Order Details" od JOIN Orders o ON o.OrderID=od.OrderID '
               "WHERE date(o.OrderDate) BETWEEN '2019-01-01' AND '2019-12-31'")
        deps.nl2sql = ScriptedSQL(sql)
        rec = answer_question(q("e2e_gm2",
            "Per the KPI definition of gross margin, what was the total gross margin in 2019? "
            "Approximate CostOfGoods as 70% of the line item UnitPrice. "
            "Return a float rounded to 2 decimals.", "float"), deps)
        assert rec.status == "answered"
        assert any("CostOfGoods" in a for a in rec.assumptions)
        # The question defines the proxy, so the gold uses it too and the answer is
        # contract-correct; it is still an estimate, so it is penalised rather than capped.
        assert 0.60 < rec.confidence <= 0.85, rec.confidence

    def test_insufficient_document_evidence_escalates(self, deps):
        deps.nl2sql = ScriptedSQL()
        deps.doc_answer = ScriptedDoc("INSUFFICIENT")
        rec = answer_question(q("e2e_range",
            "According to the product policy, what is the return window in days for Produce? "
            "Return an integer.", "int"), deps)
        assert rec.status == "needs_review"
        assert rec.review_packet.blocker


class TestOutputContract:
    def test_every_record_validates_against_the_pydantic_contract(self, deps):
        deps.nl2sql = ScriptedSQL(REVENUE)
        rec = answer_question(q("e2e_contract", TestHybridHappyPath.QUESTION, "float"), deps)
        round_tripped = type(rec).model_validate_json(rec.model_dump_json())
        assert round_tripped == rec

    def test_explanation_is_at_most_two_sentences(self, deps):
        """Counted on sentence terminators, not on every '.': a resolved window renders as
        `2017-06-01..2017-06-30`, whose dots are not sentence ends."""
        import re
        deps.nl2sql = ScriptedSQL(REVENUE)
        rec = answer_question(q("e2e_expl", TestHybridHappyPath.QUESTION, "float"), deps)
        sentences = re.findall(r"[.!?](?:\s|$)", rec.explanation)
        assert 0 < len(sentences) <= 2, rec.explanation

    def test_no_sql_means_no_table_citations(self, deps):
        deps.nl2sql = ScriptedSQL()
        deps.doc_answer = ScriptedDoc("30")
        rec = answer_question(q("e2e_nosql",
            "According to the product policy, what is the return window in days for "
            "non-perishables? Return an integer.", "int"), deps)
        assert rec.sql == ""
        assert all("::" in c for c in rec.citations)
