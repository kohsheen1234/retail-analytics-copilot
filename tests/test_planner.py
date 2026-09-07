"""Constraint extraction, and above all: conflicts surfaced rather than resolved silently.

The campaign date conflict is the corpus's central trap. `marketing_calendar::chunk1`
declares Summer Beverages 2017 as 2017-06-01..2017-06-30; `campaign_memo::chunk0` revises
the end to 2017-07-07 and claims precedence. Which one is correct depends on the
question, and the provided gold SQL settles it: questions saying "as defined in the
marketing calendar" use 06-30 and do *not* apply the memo.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent.planner import (
    build_plan,
    extract_date_windows,
    extract_kpis,
    extract_reporting_groups,
    pinned_sources,
    supplied_approximations,
)
from agent.retriever import Hit, Retriever

ROOT = Path(__file__).resolve().parents[1]
COLUMNS = {"OrderID", "OrderDate", "UnitPrice", "Quantity", "Discount", "ProductName",
           "CategoryName", "CompanyName", "Country", "Freight", "Discontinued"}


@pytest.fixture(scope="module")
def retriever():
    return Retriever(ROOT / "docs")


def all_chunks(retriever):
    """Every chunk as a Hit, so source-pinning is tested independently of ranking."""
    return [Hit(c.id, 0.0, "bm25", c.content) for c in retriever.chunks]


def plan_for(retriever, question, k=4):
    top, extra = retriever.retrieve(question, k)
    return build_plan(question, top + extra, COLUMNS)


class TestCampaignDateConflict:
    PINNED = ("Total revenue from the 'Beverages' category during the 'Summer Beverages 2017' "
              "dates as defined in the marketing calendar. Return a float rounded to 2 decimals.")
    UNPINNED = "How much revenue did the Summer Beverages 2017 campaign generate in total?"

    def test_question_pinning_the_calendar_uses_the_calendar_window(self, retriever):
        p = plan_for(retriever, self.PINNED)
        assert (p.chosen_window.start, p.chosen_window.end) == ("2017-06-01", "2017-06-30")
        assert p.chosen_window.source == "marketing_calendar::chunk1"

    def test_the_conflict_is_still_recorded_when_resolved(self, retriever):
        """Resolved is not the same as hidden: both candidate windows must be visible."""
        p = plan_for(retriever, self.PINNED)
        conflict = next(c for c in p.conflicts if c.kind == "date_window")
        assert conflict.resolved_by == "question-pins-source"
        joined = " ".join(conflict.options)
        assert "2017-06-30" in joined and "2017-07-07" in joined
        assert "marketing_calendar::chunk1" in joined and "campaign_memo::chunk0" in joined

    def test_resolution_is_reported_as_an_assumption(self, retriever):
        """An empty assumptions list on a question needing interpretation is a defect."""
        p = plan_for(retriever, self.PINNED)
        assert any("2017-07-07" in a and "not applied" in a for a in p.assumptions)

    def test_unpinned_question_honours_the_memo_precedence_claim(self, retriever):
        p = plan_for(retriever, self.UNPINNED)
        assert (p.chosen_window.start, p.chosen_window.end) == ("2017-06-01", "2017-07-07")
        conflict = next(c for c in p.conflicts if c.kind == "date_window")
        assert conflict.resolved_by == "document-precedence"
        assert any("takes precedence" in a for a in p.assumptions)

    def test_unresolvable_conflict_is_left_unresolved(self):
        """With no precedence claim and no pin, the planner must not pick a winner --
        this is what routes a question to the review gate."""
        from agent.retriever import Hit
        hits = [
            Hit("cal_a::chunk1", 1.0, "bm25", "## Spring Sale 2017\n- Dates: 2017-03-01 to 2017-03-31"),
            Hit("cal_b::chunk0", 1.0, "bm25", "# Spring Sale 2017 Update\n- Revised end date: 2017-04-15."),
        ]
        p = build_plan("How long did Spring Sale 2017 run?", hits, COLUMNS)
        assert p.unresolved_conflicts, "a conflict with no precedence rule must stay unresolved"
        assert p.unresolved_conflicts[0].resolution is None

    def test_winter_campaign_has_no_conflict(self, retriever):
        p = plan_for(retriever, "How many distinct orders were placed during 'Winter Classics 2017' "
                                "as defined in the marketing calendar?")
        assert (p.chosen_window.start, p.chosen_window.end) == ("2017-12-01", "2017-12-31")
        assert not [c for c in p.conflicts if c.kind == "date_window"]


class TestKpiDefinitions:
    def test_aov_current_and_legacy_are_both_found(self, retriever):
        p = plan_for(retriever, "Using the current AOV definition from the KPI docs, what was "
                                "the Average Order Value during 'Winter Classics 2017'?")
        aov = {k.status: k for k in p.kpi_formulas if k.name == "AOV"}
        assert set(aov) == {"current", "legacy"}
        assert aov["current"].effective == "2016-01-01"
        assert "1 - Discount" in aov["current"].expr
        assert "Discount" not in aov["legacy"].expr      # the legacy formula omits it

    def test_current_definition_wins_by_default(self, retriever):
        p = plan_for(retriever, "What was the Average Order Value during 'Winter Classics 2017' "
                                "per the KPI docs?")
        c = next(c for c in p.conflicts if c.kind == "kpi_definition")
        assert c.resolved_by == "current-by-default"
        assert c.resolution.startswith("current:")

    def test_legacy_is_used_only_when_explicitly_requested(self, retriever):
        p = plan_for(retriever, "Using the legacy AOV definition from the KPI docs, what was the "
                                "Average Order Value in 2018?")
        c = next(c for c in p.conflicts if c.kind == "kpi_definition")
        assert c.resolved_by == "question-requests-legacy"
        assert c.resolution.startswith("legacy:")

    def test_revenue_formula_keeps_its_price_source_caveat(self, retriever):
        """'line item unit price at time of sale, not the current catalog price' is the
        instruction that keeps SQL off Products.UnitPrice."""
        p = plan_for(retriever, "Top 3 products by total revenue all-time, using the Revenue "
                                "definition in the KPI docs.")
        rev = next(k for k in p.kpi_formulas if k.name == "Revenue")
        assert "line item unit price" in rev.expr.lower()


class TestMissingColumns:
    GM_WITH_FACTOR = ("Per the KPI definition of gross margin, who was the top customer by gross "
                      "margin in calendar year 2017? Approximate CostOfGoods as 70% of the line "
                      "item UnitPrice.")
    GM_WITHOUT = "Per the KPI definition of gross margin, who was the top customer by gross margin in 2017?"

    def test_costofgoods_is_detected_as_absent_from_the_schema(self, retriever):
        p = plan_for(retriever, self.GM_WITHOUT)
        assert [f for f, _, _ in p.missing_fields] == ["CostOfGoods"]

    def test_no_approximation_anywhere_is_blocking(self, retriever):
        """The corpus authorises 'a documented approximation' but never documents one."""
        p = plan_for(retriever, self.GM_WITHOUT)
        assert p.blocking_missing_fields, "must escalate rather than invent a margin"

    def test_an_approximation_supplied_by_the_question_unblocks_it(self, retriever):
        p = plan_for(retriever, self.GM_WITH_FACTOR)
        assert "CostOfGoods" in p.supplied_approximations
        assert not p.blocking_missing_fields

    def test_supplied_approximation_parser(self):
        got = supplied_approximations("Approximate CostOfGoods as 70% of the line item UnitPrice.")
        assert "CostOfGoods" in got and "70%" in got["CostOfGoods"]


class TestReportingGroups:
    def test_pantry_group_is_extracted(self, retriever):
        p = plan_for(retriever, "Total revenue by reporting group in 2018.")
        groups = p.reporting_groups
        assert "Pantry" in groups, groups
        assert set(groups["Pantry"]) == {"Grains/Cereals", "Produce"}


class TestSourcePinning:
    @pytest.mark.parametrize("question,expected", [
        ("Total revenue as defined in the marketing calendar.", "marketing_calendar"),
        ("According to the product policy, what is the return window?", "product_policy"),
        ("using the Revenue definition in the KPI docs", "kpi_definitions"),
    ])
    def test_pins(self, retriever, question, expected):
        assert expected in pinned_sources(question, all_chunks(retriever))

    def test_no_pin_when_the_question_names_no_source(self, retriever):
        assert pinned_sources("How much revenue did the Summer Beverages 2017 campaign generate?",
                              all_chunks(retriever)) == ()


def test_date_window_parser_handles_separator_variants():
    from agent.retriever import Hit
    for sep in ("to", "through", "-", "–"):
        hits = [Hit("d::chunk1", 1.0, "bm25", f"## Test Sale 2017\n- Dates: 2017-01-01 {sep} 2017-02-02")]
        w = extract_date_windows(hits)
        assert (w[0].start, w[0].end) == ("2017-01-01", "2017-02-02"), sep


def test_generalises_to_an_unseen_document():
    """The live session hands over a new document. The rules are shape-based, so a
    campaign and a KPI in a file nobody has seen still parse."""
    from agent.retriever import Hit
    hits = [
        Hit("loyalty_program::chunk1", 3.0, "bm25",
            "## Autumn Loyalty 2019\n- Dates: 2019-09-01 to 2019-11-30\n- Notes: double points."),
        Hit("loyalty_program::chunk2", 2.0, "bm25",
            "## Points Per Dollar\n- PPD = SUM(Points) / SUM(UnitPrice * Quantity)"),
    ]
    p = build_plan("What were sales during Autumn Loyalty 2019?", hits, COLUMNS)
    assert (p.chosen_window.start, p.chosen_window.end) == ("2019-09-01", "2019-11-30")
    assert any(k.name == "PPD" for k in p.kpi_formulas)
    assert "Points" in {f for f, _, _ in p.missing_fields}
