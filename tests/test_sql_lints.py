"""The static lints that gate execution, and the repairs they drive.

Each lint here exists because a real run produced a wrong number that executed cleanly.
The executor cannot see those; only a check on the SQL text can. Every lint is tested
three ways: it fires on the failure that motivated it, it stays silent on the documented
correct form, and it stays silent on every provided gold statement - a false positive
spends a repair slot on valid SQL, which is a scored defect in its own right.

Two of the lints have exact fixes and those are *applied*, not requested. Routing them
through an LM repair was measured to cost an LM call each, produce worse SQL from a 3.8B
model, and - because extra calls perturb Ollama's state - break the determinism gate that
had passed with zero repairs. The rewrites are tested for exactness, for leaving every
gold statement untouched, and for reaching the executor with zero repairs end to end.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent import config
from agent.graph_hybrid import answer_question, build_deps
from agent.modules import repair_corrupted_keywords
from agent.schema import label_collisions, view_map
from agent.sql_analysis import (
    ambiguous_columns,
    date_boundary_errors,
    label_grouping,
    physical_tables,
    rewrite_date_boundaries,
    rewrite_label_grouping,
    schema_errors,
)
from models import QuestionRecord
from sqlite_tool import SQLiteTool
from test_end_to_end import FixedExplainer, FixedRouter, ScriptedSQL, ScriptedSynth

pytestmark = pytest.mark.skipif(not config.DB_PATH.exists(), reason="northwind.sqlite not present")

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def schema():
    return SQLiteTool(str(config.DB_PATH)).schema()


def gold_statements() -> list[tuple[str, str]]:
    out = []
    for name in ("train.jsonl", "train_added.jsonl", "dev.jsonl", "dev_added.jsonl"):
        for line in (ROOT / "data" / name).read_text().splitlines():
            if line.strip():
                ex = json.loads(line)
                if ex.get("gold_sql"):
                    out.append((ex["id"], ex["gold_sql"]))
    return out


def example(example_id: str) -> dict:
    for name in ("dev.jsonl", "dev_added.jsonl", "train.jsonl", "train_added.jsonl"):
        for line in (ROOT / "data" / name).read_text().splitlines():
            if line.strip() and json.loads(line)["id"] == example_id:
                return json.loads(line)
    raise KeyError(example_id)


# --------------------------------------------------------------------------- grain

CUSTOMER_JOIN = ('FROM "Order Details" od JOIN Orders o ON o.OrderID=od.OrderID '
                 'JOIN Customers c ON c.CustomerID=o.CustomerID')


class TestLabelGrouping:
    def test_fires_on_the_dev_failure_that_shipped_a_test_account_as_a_customer(self, schema):
        sql = f"SELECT c.CompanyName, SUM(od.Quantity) q {CUSTOMER_JOIN} GROUP BY c.CompanyName ORDER BY q DESC LIMIT 3"
        [msg] = label_grouping(sql, schema)
        assert "c.CompanyName" in msg and "c.CustomerID" in msg

    def test_fires_on_categories_too(self, schema):
        sql = ('SELECT c.CategoryName, COUNT(*) FROM Products p JOIN Categories c '
               'ON c.CategoryID=p.CategoryID GROUP BY c.CategoryName')
        [msg] = label_grouping(sql, schema)
        assert "c.CategoryID" in msg

    def test_silent_when_grouped_by_the_key(self, schema):
        sql = f"SELECT c.CompanyName, SUM(od.Quantity) {CUSTOMER_JOIN} GROUP BY c.CustomerID"
        assert label_grouping(sql, schema) == []

    def test_silent_when_the_key_accompanies_the_name(self, schema):
        sql = f"SELECT c.CompanyName {CUSTOMER_JOIN} GROUP BY c.CustomerID, c.CompanyName"
        assert label_grouping(sql, schema) == []

    def test_silent_on_a_text_column_that_is_not_an_entity_label(self, schema):
        assert label_grouping("SELECT ShipCountry, COUNT(*) FROM Orders GROUP BY ShipCountry", schema) == []

    def test_silent_without_a_group_by(self, schema):
        assert label_grouping(f"SELECT COUNT(*) {CUSTOMER_JOIN}", schema) == []

    def test_resolves_a_bare_column_to_its_one_owner(self, schema):
        sql = f"SELECT CompanyName, COUNT(*) {CUSTOMER_JOIN} GROUP BY CompanyName"
        [msg] = label_grouping(sql, schema)
        assert "c.CustomerID" in msg

    @pytest.mark.parametrize("example_id,sql", gold_statements())
    def test_silent_on_every_gold_statement(self, schema, example_id, sql):
        assert label_grouping(sql, schema) == []


# --------------------------------------------------------------------------- ambiguity

class TestAmbiguousColumns:
    JOIN = 'FROM "Order Details" od JOIN Orders o ON o.OrderID=od.OrderID'

    def test_fires_on_the_dev_failure(self, schema):
        [msg] = ambiguous_columns(f"SELECT COUNT(DISTINCT OrderID) {self.JOIN}", schema)
        assert "OrderID" in msg and "od.OrderID" in msg and "o.OrderID" in msg

    def test_silent_when_qualified(self, schema):
        assert ambiguous_columns(f"SELECT COUNT(DISTINCT od.OrderID) {self.JOIN}", schema) == []

    def test_silent_on_a_single_table(self, schema):
        assert ambiguous_columns("SELECT OrderID FROM Orders", schema) == []

    def test_conservative_on_subqueries(self, schema):
        sql = f"SELECT COUNT(*) FROM (SELECT OrderID {self.JOIN}) x"
        assert ambiguous_columns(sql, schema) == []

    def test_conservative_on_using(self, schema):
        sql = 'SELECT COUNT(DISTINCT OrderID) FROM "Order Details" JOIN Orders USING (OrderID)'
        assert ambiguous_columns(sql, schema) == []

    def test_does_not_mistake_an_output_alias_for_a_column(self, schema):
        sql = f"SELECT COUNT(*) AS OrderID {self.JOIN}"
        assert ambiguous_columns(sql, schema) == []

    @pytest.mark.parametrize("example_id,sql", gold_statements())
    def test_silent_on_every_gold_statement(self, schema, example_id, sql):
        assert ambiguous_columns(sql, schema) == []


# --------------------------------------------------------------------------- dates

class TestDateBoundary:
    @pytest.mark.parametrize("sql", [
        "SELECT COUNT(*) FROM Orders WHERE OrderDate BETWEEN '2017-06-01' AND '2017-06-30'",
        "SELECT COUNT(*) FROM Orders o WHERE o.OrderDate <= '2017-06-30'",
        "SELECT COUNT(*) FROM Orders WHERE OrderDate = '2017-06-30'",
    ])
    def test_fires_on_an_inclusive_bound_over_the_bare_column(self, sql):
        assert date_boundary_errors(sql)

    @pytest.mark.parametrize("sql", [
        # half-open on the bare column is correct on mixed formats
        "SELECT COUNT(*) FROM Orders WHERE OrderDate >= '2017-06-01' AND OrderDate < '2017-07-01'",
        "SELECT COUNT(*) FROM Orders WHERE date(OrderDate) BETWEEN '2017-06-01' AND '2017-06-30'",
        "SELECT COUNT(*) FROM Orders WHERE strftime('%Y', OrderDate) = '2017'",
        "SELECT COUNT(*) FROM Orders WHERE ShippedDate IS NULL",
        "SELECT OrderDate FROM Orders",
    ])
    def test_silent_on_correct_forms(self, sql):
        assert date_boundary_errors(sql) == []

    def test_the_undercount_it_prevents_is_real(self):
        """The reason this is a repair trigger and not a note."""
        tool = SQLiteTool(str(config.DB_PATH))
        bare = tool.execute("SELECT COUNT(*) FROM Orders WHERE OrderDate BETWEEN '2017-06-01' AND '2017-06-30'").rows[0][0]
        wrapped = tool.execute("SELECT COUNT(*) FROM Orders WHERE date(OrderDate) BETWEEN '2017-06-01' AND '2017-06-30'").rows[0][0]
        assert wrapped > bare

    @pytest.mark.parametrize("example_id,sql", gold_statements())
    def test_silent_on_every_gold_statement(self, example_id, sql):
        assert date_boundary_errors(sql) == []


# --------------------------------------------------------------------------- schema messages

class TestUnknownTableMessage:
    def test_names_the_invented_table_instead_of_contradicting_itself(self, schema):
        sql = "SELECT SUM(x.Quantity) FROM Discontins x JOIN Orders o ON o.OrderID=x.OrderID"
        msgs = schema_errors(sql, schema)
        assert any("table 'Discontins' does not exist" in m for m in msgs)
        assert not any("never defined" in m for m in msgs)

    def test_columns_of_a_real_table_are_still_checked(self, schema):
        [msg] = schema_errors("SELECT p.Discontins FROM Products p", schema)
        assert "does not exist on Products" in msg


# --------------------------------------------------------------------------- views

class TestViews:
    def test_the_fixture_ships_views_and_they_resolve_to_real_tables(self, schema):
        views = view_map(str(config.DB_PATH))
        assert "order_items" in views
        assert views["order_items"] == {"Order Details"}
        for name, bases in views.items():
            assert bases, f"{name} resolved to nothing"
            assert bases <= set(schema), f"{name} -> {bases - set(schema)}"

    def test_a_view_is_cited_as_the_tables_it_reads(self, schema):
        views = view_map(str(config.DB_PATH))
        sql = "SELECT SUM(oi.Quantity) FROM order_items oi JOIN Orders o ON o.OrderID=oi.OrderID"
        assert physical_tables(sql, list(schema), views) == ["Order Details", "Orders"]
        assert "order_items" not in physical_tables(sql, list(schema), views)

    def test_without_the_map_a_view_query_cites_nothing_for_the_view(self, schema):
        sql = "SELECT COUNT(*) FROM Invoices"
        assert physical_tables(sql, list(schema)) == []


# --------------------------------------------------------------------------- data-aware labels

class TestLabelCollisions:
    def test_measures_exactly_the_one_colliding_identifier(self):
        found = label_collisions(str(config.DB_PATH))
        assert found == {"Customers": {"CompanyName": "CustomerID"}}

    def test_a_unique_label_is_not_restricted(self, schema):
        """CategoryName is unique, so grouping by it is harmless and must not fire."""
        sql = ('SELECT c.CategoryName, COUNT(*) FROM Products p JOIN Categories c '
               'ON c.CategoryID=p.CategoryID GROUP BY c.CategoryName')
        assert label_grouping(sql, schema, label_collisions(str(config.DB_PATH))) == []

    def test_an_attribute_that_merely_ends_in_name_is_not_restricted(self, schema):
        """ShipName has 90 values over 16,282 orders; people legitimately group by it."""
        sql = "SELECT o.ShipName, COUNT(*) FROM Orders o GROUP BY o.ShipName"
        assert label_grouping(sql, schema, label_collisions(str(config.DB_PATH))) == []


# --------------------------------------------------------------------------- rewrites

@pytest.fixture(scope="module")
def labels():
    return label_collisions(str(config.DB_PATH))


@pytest.fixture(scope="module")
def names(schema):
    return set(schema) | {c["name"] for cols in schema.values() for c in cols}


class TestRewriteLabelGrouping:
    def test_rewrites_the_dev_failure_to_the_key_and_reproduces_gold(self, schema, labels):
        ex = example("dev_top3_customers_revenue_2019")
        by_name = ex["gold_sql"].replace("GROUP BY cu.CustomerID", "GROUP BY cu.CompanyName")
        out, notes = rewrite_label_grouping(by_name, schema, labels)
        assert "GROUP BY cu.CustomerID" in out and notes
        tool = SQLiteTool(str(config.DB_PATH))
        assert tool.execute(out).rows == tool.execute(ex["gold_sql"]).rows

    def test_only_the_group_by_clause_is_touched(self, schema, labels):
        sql = f"SELECT c.CompanyName, COUNT(*) {CUSTOMER_JOIN} GROUP BY c.CompanyName ORDER BY c.CompanyName"
        out, _ = rewrite_label_grouping(sql, schema, labels)
        assert out.startswith("SELECT c.CompanyName") and out.endswith("ORDER BY c.CompanyName")
        assert "GROUP BY c.CustomerID" in out

    def test_other_group_terms_survive(self, schema, labels):
        sql = ("SELECT c.CompanyName, o.ShipCountry, COUNT(*) FROM Orders o JOIN Customers c "
               "ON c.CustomerID=o.CustomerID GROUP BY c.CompanyName, o.ShipCountry")
        out, _ = rewrite_label_grouping(sql, schema, labels)
        assert out.endswith("GROUP BY c.CustomerID, o.ShipCountry")

    def test_a_bare_label_is_qualified_on_the_way(self, schema, labels):
        sql = f"SELECT CompanyName, COUNT(*) {CUSTOMER_JOIN} GROUP BY CompanyName"
        out, _ = rewrite_label_grouping(sql, schema, labels)
        assert out.endswith("GROUP BY c.CustomerID")

    def test_leaves_the_rewritten_sql_lint_clean(self, schema, labels):
        sql = f"SELECT c.CompanyName, COUNT(*) {CUSTOMER_JOIN} GROUP BY c.CompanyName"
        out, _ = rewrite_label_grouping(sql, schema, labels)
        assert label_grouping(out, schema, labels) == []

    @pytest.mark.parametrize("sql", [
        'SELECT c.CategoryName, COUNT(*) FROM Products p JOIN Categories c ON c.CategoryID=p.CategoryID GROUP BY c.CategoryName',
        "SELECT o.ShipName, COUNT(*) FROM Orders o GROUP BY o.ShipName",
        f"SELECT c.CompanyName {CUSTOMER_JOIN} GROUP BY c.CustomerID, c.CompanyName",
        "SELECT COUNT(*) FROM Orders",
    ])
    def test_leaves_correct_or_harmless_sql_alone(self, schema, labels, sql):
        assert rewrite_label_grouping(sql, schema, labels) == (sql, [])

    @pytest.mark.parametrize("example_id,sql", gold_statements())
    def test_leaves_every_gold_statement_untouched(self, schema, labels, example_id, sql):
        assert rewrite_label_grouping(sql, schema, labels)[0] == sql


class TestRewriteDateBoundaries:
    def test_wraps_a_bare_between(self):
        out, notes = rewrite_date_boundaries("SELECT COUNT(*) FROM Orders WHERE OrderDate BETWEEN '2017-06-01' AND '2017-06-30'")
        assert "WHERE date(OrderDate) BETWEEN" in out and notes

    def test_wraps_a_qualified_inclusive_bound_and_nothing_else(self):
        sql = "SELECT COUNT(*) FROM Orders o WHERE o.OrderDate <= '2017-06-30' AND o.ShipCountry='France'"
        out, _ = rewrite_date_boundaries(sql)
        assert out == "SELECT COUNT(*) FROM Orders o WHERE date(o.OrderDate) <= '2017-06-30' AND o.ShipCountry='France'"

    def test_the_rewrite_recovers_the_dropped_rows(self):
        tool = SQLiteTool(str(config.DB_PATH))
        bare = "SELECT COUNT(*) FROM Orders WHERE OrderDate BETWEEN '2017-06-01' AND '2017-06-30'"
        out, _ = rewrite_date_boundaries(bare)
        assert tool.execute(out).rows[0][0] > tool.execute(bare).rows[0][0]
        assert date_boundary_errors(out) == []

    @pytest.mark.parametrize("sql", [
        "SELECT COUNT(*) FROM Orders WHERE OrderDate >= '2017-06-01' AND OrderDate < '2017-07-01'",
        "SELECT COUNT(*) FROM Orders WHERE date(OrderDate) BETWEEN '2017-06-01' AND '2017-06-30'",
        "SELECT COUNT(*) FROM Orders WHERE ShippedDate IS NULL",
        "SELECT * FROM Orders WHERE ShipName = 'OrderDate <= x'",   # inside a literal
    ])
    def test_leaves_correct_sql_alone(self, sql):
        assert rewrite_date_boundaries(sql) == (sql, [])

    @pytest.mark.parametrize("example_id,sql", gold_statements())
    def test_leaves_every_gold_statement_untouched(self, example_id, sql):
        assert rewrite_date_boundaries(sql)[0] == sql


class TestSquashedTableNames:
    """`OrderDetails` is the model reaching for "Order Details", not corrupting a keyword."""

    def test_resolves_to_the_quoted_table(self, names):
        out, fixes = repair_corrupted_keywords("SELECT SUM(od.Quantity) FROM OrderDetails od", names)
        assert out == 'SELECT SUM(od.Quantity) FROM "Order Details" od'
        assert fixes == ['OrderDetails -> "Order Details"']

    def test_no_longer_becomes_the_keyword_order(self, names):
        out, _ = repair_corrupted_keywords("SELECT 1 FROM Orders o JOIN OrderDetails od ON o.OrderID=od.OrderID", names)
        assert "JOIN order " not in out and '"Order Details"' in out

    def test_keyword_corruption_is_still_repaired(self, names):
        out, fixes = repair_corrupted_keywords("SELECT 1 FROM Orders WHERE OrderDate BETWEDIR 'a' AND 'b'", names)
        assert "BETWEEN" in out and fixes == ["BETWEDIR -> BETWEEN"]

    def test_real_identifiers_are_untouched(self, names):
        sql = "SELECT p.Discontinued, p.QuantityPerUnit FROM Products p"
        assert repair_corrupted_keywords(sql, names) == (sql, [])


# --------------------------------------------------------------------------- end to end

@pytest.fixture
def deps(tmp_path):
    d = build_deps()
    d.explainer = FixedExplainer()
    d.router = FixedRouter()
    d.synthesizer = ScriptedSynth(unknown=True)
    d.traces_dir = tmp_path / "traces"
    return d


def trace_of(deps, qid: str) -> list[dict]:
    return [json.loads(l) for l in (deps.traces_dir / f"{qid}.jsonl").read_text().splitlines() if l.strip()]


class TestGrainRewriteEndToEnd:
    """The measured failure: `dev_top3_customers_revenue_2019` answered `IT` at 0.75."""

    def _q(self):
        ex = example("dev_top3_customers_revenue_2019")
        return ex, QuestionRecord(id="e2e_grain", question=ex["question"], format_hint=ex["format_hint"])

    def test_grouping_by_label_is_rewritten_with_zero_lm_repairs(self, deps):
        ex, q = self._q()
        by_name = ex["gold_sql"].replace("GROUP BY cu.CustomerID", "GROUP BY cu.CompanyName")
        assert by_name != ex["gold_sql"]
        scripted = ScriptedSQL(by_name)              # one attempt available, and only one used
        deps.nl2sql = scripted
        rec = answer_question(q, deps)
        assert rec.status == "answered" and rec.repairs == 0
        assert len(scripted.calls) == 1
        assert [r["customer"] for r in rec.final_answer] == [r["customer"] for r in ex["gold_answer"]]
        assert "IT" not in [r["customer"] for r in rec.final_answer]

    def test_the_executed_sql_is_the_rewritten_one(self, deps):
        ex, q = self._q()
        by_name = ex["gold_sql"].replace("GROUP BY cu.CustomerID", "GROUP BY cu.CompanyName")
        deps.nl2sql = ScriptedSQL(by_name)
        rec = answer_question(q, deps)
        assert "GROUP BY cu.CustomerID" in rec.sql and "GROUP BY cu.CompanyName" not in rec.sql

    def test_the_rewrite_is_stated_as_an_assumption_and_costs_confidence(self, deps):
        ex, q = self._q()
        by_name = ex["gold_sql"].replace("GROUP BY cu.CustomerID", "GROUP BY cu.CompanyName")
        deps.nl2sql = ScriptedSQL(by_name)
        rec = answer_question(q, deps)
        assert any("CustomerID instead of CompanyName" in a for a in rec.assumptions)
        deps.nl2sql = ScriptedSQL(ex["gold_sql"])
        clean = answer_question(QuestionRecord(id="e2e_grain_clean", question=q.question,
                                               format_hint=q.format_hint), deps)
        assert rec.confidence < clean.confidence

    def test_both_the_model_sql_and_the_executed_sql_are_in_the_trace(self, deps):
        ex, q = self._q()
        by_name = ex["gold_sql"].replace("GROUP BY cu.CustomerID", "GROUP BY cu.CompanyName")
        deps.nl2sql = ScriptedSQL(by_name)
        answer_question(q, deps)
        first = next(e for e in trace_of(deps, "e2e_grain") if e["responsibility"] == "nl2sql")
        assert first["outputs"]["model_sql"] == by_name
        assert "GROUP BY cu.CustomerID" in first["outputs"]["sql"]
        assert first["outputs"]["normalised"]

    def test_the_documented_form_is_untouched(self, deps):
        ex, q = self._q()
        deps.nl2sql = ScriptedSQL(ex["gold_sql"])
        rec = answer_question(q, deps)
        assert rec.status == "answered" and rec.repairs == 0 and rec.sql == ex["gold_sql"]
        assert not any("instead of" in a for a in rec.assumptions)


class TestAmbiguityRepair:
    def test_bare_ambiguous_column_is_repaired_before_execution(self, deps):
        ex = example("dev_added_grain_lines_vs_orders_2020")
        q = QuestionRecord(id="e2e_ambig", question=ex["question"], format_hint=ex["format_hint"])
        bare = ex["gold_sql"].replace("COUNT(DISTINCT od.OrderID)", "COUNT(DISTINCT OrderID)")
        assert bare != ex["gold_sql"]
        scripted = ScriptedSQL(bare, ex["gold_sql"])
        deps.nl2sql = scripted
        rec = answer_question(q, deps)
        assert rec.status == "answered" and rec.repairs == 1
        assert "od.OrderID" in scripted.calls[1]["feedback"]
        events = trace_of(deps, "e2e_ambig")
        assert not any(e["responsibility"] == "execution" and e.get("attempt") == 0 for e in events), \
            "the ambiguous statement should never have reached the executor"
