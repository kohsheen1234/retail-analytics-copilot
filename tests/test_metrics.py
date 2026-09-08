"""The execution-grounded metric.

The assessment requires a case that must fail, and gates the whole DSPy area on the
metric being neither string-based nor incapable of failing. So there are three kinds of
test here:

  * it PASSES on SQL that is textually very different but semantically identical;
  * it FAILS on SQL that is textually nearly identical but semantically wrong;
  * it returns a bool in both DSPy call modes, because bootstrapping uses truthiness.

The headline must-fail case is the real bug this database punishes: a bare
`OrderDate BETWEEN` instead of `date(OrderDate) BETWEEN`, which silently drops
timestamped rows on the closing day of a window. A string-similarity metric would score
that pair at ~0.97 and admit it as a demonstration.
"""
from __future__ import annotations

import pytest

from agent import config
from agent.metrics import (
    NUMERIC_TOLERANCE,
    cells_equal,
    evaluate_sql,
    resolve_ordered,
    rows_equal,
    sql_metric,
    sql_metric_verbose,
)

pytestmark = pytest.mark.skipif(not config.DB_PATH.exists(), reason="northwind.sqlite not present")


def ex(gold_sql: str, ordered=None, **kw):
    d = {"id": "t", "gold_sql": gold_sql}
    if ordered is not None:
        d["ordered"] = ordered
    d.update(kw)
    return d


class Pred:
    def __init__(self, sql):
        self.sql = sql


# --- not string-based -------------------------------------------------------

def test_passes_on_textually_different_but_equivalent_sql():
    """Different table order, different aliases, different literal style, same result."""
    gold = "SELECT COUNT(*) FROM Orders WHERE ShipCountry='USA'"
    pred = ('SELECT COUNT(o.OrderID) AS total_orders FROM Orders AS o '
            'WHERE o.ShipCountry LIKE "USA"')
    assert sql_metric(ex(gold), Pred(pred)) is True


def test_passes_when_a_cte_replaces_a_subquery():
    gold = 'SELECT COUNT(*) FROM Orders WHERE OrderID IN (SELECT OrderID FROM "Order Details")'
    pred = ('WITH lines AS (SELECT DISTINCT OrderID FROM "Order Details") '
            'SELECT COUNT(*) FROM Orders WHERE OrderID IN (SELECT OrderID FROM lines)')
    assert sql_metric(ex(gold), Pred(pred)) is True


def test_fails_on_nearly_identical_sql_that_is_wrong():
    """One character of difference, a different number. This is the case that must fail."""
    gold = "SELECT COUNT(*) FROM Orders WHERE ShipCountry='USA'"
    pred = "SELECT COUNT(*) FROM Orders WHERE ShipCountry='Usa'"
    assert sql_metric(ex(gold), Pred(pred)) is False


class TestTheDateBoundaryMustFail:
    """The must-fail case with real consequences in this database."""

    GOLD = ("SELECT COUNT(*) FROM Orders o "
            "WHERE date(o.OrderDate) BETWEEN '2017-06-01' AND '2017-06-30'")
    BARE = ("SELECT COUNT(*) FROM Orders o "
            "WHERE o.OrderDate BETWEEN '2017-06-01' AND '2017-06-30'")

    def test_bare_between_fails(self):
        out = sql_metric_verbose(ex(self.GOLD), Pred(self.BARE))
        assert out.ok is False
        assert "rows differ" in out.reason

    def test_and_the_two_are_almost_identical_as_strings(self):
        """Guards the guard: if these ever stop being near-identical text, this test has
        stopped testing what it claims to."""
        import difflib
        ratio = difflib.SequenceMatcher(None, self.GOLD, self.BARE).ratio()
        assert ratio > 0.95, ratio

    def test_the_correct_form_passes(self):
        assert sql_metric(ex(self.GOLD), Pred(self.GOLD)) is True


# --- equivalence contract ---------------------------------------------------

def test_column_count_mismatch_fails():
    gold = "SELECT COUNT(*) FROM Shippers"
    pred = "SELECT COUNT(*), 1 FROM Shippers"
    out = sql_metric_verbose(ex(gold), Pred(pred))
    assert out.ok is False and "column count" in out.reason


def test_column_names_and_aliases_do_not_matter():
    gold = "SELECT ShipperID AS a FROM Shippers ORDER BY ShipperID"
    pred = "SELECT ShipperID AS totally_different FROM Shippers ORDER BY ShipperID"
    assert sql_metric(ex(gold, ordered=True), Pred(pred)) is True


def test_execution_error_fails():
    out = sql_metric_verbose(ex("SELECT 1"), Pred("SELECT * FROM NoSuchTable"))
    assert out.ok is False and "failed" in out.reason


def test_unsafe_sql_fails_without_touching_the_database():
    out = sql_metric_verbose(ex("SELECT 1"), Pred("DELETE FROM Orders"))
    assert out.ok is False and "unsafe" in out.reason


def test_multi_statement_fails():
    out = sql_metric_verbose(ex("SELECT 1"), Pred("SELECT 1; DROP TABLE Orders"))
    assert out.ok is False and ("unsafe" in out.reason or "multiple" in out.reason)


def test_broken_gold_raises_rather_than_scoring_zero():
    """A dataset bug must be loud. Silently failing the prediction would hide it."""
    with pytest.raises(ValueError, match="gold SQL failed"):
        sql_metric(ex("SELECT * FROM NoSuchGoldTable"), Pred("SELECT 1"))


class TestRowsAndCells:
    def test_multiset_ignores_order_when_unordered(self):
        assert rows_equal([(1,), (2,)], [(2,), (1,)], ordered=False) is True

    def test_order_matters_when_ordered(self):
        assert rows_equal([(1,), (2,)], [(2,), (1,)], ordered=True) is False

    def test_duplicates_count(self):
        assert rows_equal([(1,), (1,)], [(1,)], ordered=False) is False
        assert rows_equal([(1,), (1,)], [(1,), (1,)], ordered=False) is True

    def test_null_only_equals_null(self):
        assert cells_equal(None, None) is True
        assert cells_equal(None, 0) is False
        assert cells_equal(None, "") is False
        assert cells_equal(0, None) is False

    def test_numeric_tolerance(self):
        assert cells_equal(1.0, 1.0 + NUMERIC_TOLERANCE / 2) is True
        assert cells_equal(1.0, 1.05) is False

    def test_whitespace_normalised_for_strings(self):
        assert cells_equal("Order  Details", "Order Details") is True
        assert cells_equal(" Chai\n", "Chai") is True

    def test_string_case_still_matters(self):
        assert cells_equal("Chai", "chai") is False


def test_reversed_top_n_fails_when_ordered():
    """Rankings are the reason `ordered` exists."""
    gold = "SELECT ShipperID FROM Shippers ORDER BY ShipperID DESC LIMIT 3"
    pred = "SELECT ShipperID FROM Shippers ORDER BY ShipperID ASC LIMIT 3"
    assert sql_metric(ex(gold, ordered=True), Pred(pred)) is False
    # and passes as a multiset when order genuinely does not matter
    assert sql_metric(ex(gold, ordered=False), Pred(pred)) is True


# --- ordered resolution -----------------------------------------------------

def test_explicit_ordered_flag_wins_over_inference():
    e = ex("SELECT 1 FROM Orders ORDER BY OrderID", ordered=False)
    assert resolve_ordered(e) is False


def test_ordered_is_inferred_when_absent():
    assert resolve_ordered(ex("SELECT 1 FROM Orders ORDER BY OrderID")) is True
    assert resolve_ordered(ex("SELECT COUNT(*) FROM Orders")) is False


# --- abstention contract ----------------------------------------------------

class TestAbstention:
    """RAG-only examples have empty gold SQL. The contract can fail in both directions."""

    def test_silence_is_correct_when_gold_has_no_sql(self):
        assert sql_metric(ex(""), Pred("")) is True

    def test_producing_sql_when_none_is_wanted_fails(self):
        out = sql_metric_verbose(ex(""), Pred("SELECT 1"))
        assert out.ok is False and "abstention" in out.reason

    def test_silence_fails_when_sql_is_wanted(self):
        out = sql_metric_verbose(ex("SELECT COUNT(*) FROM Orders"), Pred(""))
        assert out.ok is False and "no SQL produced" in out.reason


# --- DSPy dual-mode ---------------------------------------------------------

class TestBothCallModes:
    GOLD = "SELECT COUNT(*) FROM Shippers"

    def test_returns_bool_when_scoring(self):
        assert isinstance(sql_metric(ex(self.GOLD), Pred(self.GOLD), trace=None), bool)

    def test_returns_bool_when_bootstrapping(self):
        v = sql_metric(ex(self.GOLD), Pred(self.GOLD), trace=[("predictor", {}, {})])
        assert isinstance(v, bool) and v is True

    def test_failure_is_falsey_in_bootstrap_mode(self):
        """The property BootstrapFewShot relies on: `success = metric_val`.

        A float in (0, 1) would be truthy here and would admit a wrong demo. bool makes
        the filter and the scorer agree.
        """
        v = sql_metric(ex(self.GOLD), Pred("SELECT COUNT(*)+1 FROM Shippers"),
                       trace=[("predictor", {}, {})])
        assert v is False and not v

    def test_bool_aggregates_numerically_for_evaluate(self):
        assert sum([True, False, True]) / 3 == pytest.approx(2 / 3)


def test_accepts_a_plain_string_prediction():
    """Robustness: some DSPy paths hand back a bare string rather than a Prediction."""
    assert sql_metric(ex("SELECT COUNT(*) FROM Shippers"), "SELECT COUNT(*) FROM Shippers") is True


def test_every_gold_sql_scores_itself_as_correct():
    """Sanity floor across the real dataset: the metric must be reflexive on all 33
    SQL-bearing examples, otherwise it is broken rather than strict."""
    from agent.datasets import load_split
    n = 0
    for which in ("train", "dev"):
        for rec in load_split(which).records:
            if (rec.get("gold_sql") or "").strip():
                assert sql_metric(rec, Pred(rec["gold_sql"])) is True, rec["id"]
                n += 1
    assert n == 33


# --- OPTIONAL O2: the router metric -----------------------------------------

class TestRouterMetric:
    """O2 requires a second metric that can fail. Exact match over three labels does."""

    def test_exact_match_passes(self):
        from agent.metrics import router_metric
        assert router_metric({"route": "hybrid"}, dspy_pred("hybrid")) is True

    def test_wrong_label_fails(self):
        from agent.metrics import router_metric
        assert router_metric({"route": "hybrid"}, dspy_pred("sql")) is False

    def test_no_partial_credit_for_being_close(self):
        """`sql` on a `hybrid` question is wrong, not two-thirds right."""
        from agent.metrics import router_metric
        assert router_metric({"route": "hybrid"}, dspy_pred("sql")) in (False,)

    def test_case_and_whitespace_are_forgiven(self):
        from agent.metrics import router_metric
        assert router_metric({"route": "rag"}, dspy_pred("  RAG \n")) is True

    def test_unparseable_prediction_fails(self):
        from agent.metrics import router_metric
        assert router_metric({"route": "rag"}, dspy_pred("I think this needs the docs")) is False

    def test_missing_label_fails_rather_than_passing_vacuously(self):
        from agent.metrics import router_metric
        assert router_metric({}, dspy_pred("rag")) is False

    def test_returns_bool_in_both_modes(self):
        from agent.metrics import router_metric
        for trace in (None, [("p", {}, {})]):
            assert isinstance(router_metric({"route": "sql"}, dspy_pred("sql"), trace), bool)

    def test_the_shipped_rule_router_cannot_reach_1_0_on_provided_labels(self):
        """The provided labels contradict each other, so a perfect score is unreachable.

        Five revenue questions are labelled `hybrid`; train_top3_categories_revenue is
        labelled `sql` with the identical formula. Any self-consistent classifier must
        lose at least one of them.
        """
        from agent.datasets import load_split
        from agent.modules import rule_route
        recs = [r for r in load_split("train").records + load_split("dev").records if r.get("route")]
        wrong = [r["id"] for r in recs if rule_route(r["question"]) != r["route"]]
        assert wrong == ["train_top3_categories_revenue"], wrong


def dspy_pred(route: str):
    import dspy
    return dspy.Prediction(route=route)
