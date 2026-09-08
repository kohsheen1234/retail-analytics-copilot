"""The validator, including the citation check.

Each of the four checks must be able to fail on its own, and the table-citation check
must fail on *extra* citations as well as missing ones -- the assessment is explicit that
both directions fail, and over-citing is the easier mistake to make accidentally.
"""
from __future__ import annotations

import pytest

from agent.validator import Failure, split_citations, validate

KNOWN = ["Categories", "Customers", "Order Details", "Orders", "Products", "Shippers"]
CORPUS = {"kpi_definitions::chunk1", "kpi_definitions::chunk3",
          "marketing_calendar::chunk1", "campaign_memo::chunk0", "product_policy::chunk1"}


def run(**kw):
    base = dict(
        final_answer=1, format_hint="int",
        sql="SELECT COUNT(*) FROM Orders", citations=["Orders"],
        columns=["c"], rows=[(1,)], known_tables=KNOWN,
        corpus_chunk_ids=CORPUS, seen_chunk_ids=set(CORPUS), route="sql",
    )
    base.update(kw)
    return validate(**base)


def checks(failures: list[Failure]) -> set[str]:
    return {f.check for f in failures}


def test_clean_case_passes():
    assert run() == []


class TestFormat:
    def test_wrong_scalar_type_fails(self):
        assert "format" in checks(run(final_answer=1.5, format_hint="int"))

    def test_missing_dict_field_fails(self):
        out = run(final_answer={"category": "Beverages"},
                  format_hint="{category:str, quantity:int}")
        assert "format" in checks(out)

    def test_list_of_objects_passes(self):
        out = run(final_answer=[{"product": "Chai", "revenue": 1.5}],
                  format_hint="list[{product:str, revenue:float}]")
        assert out == []

    def test_unknown_format_hint_fails_rather_than_raising(self):
        assert "format" in checks(run(format_hint="tuple[int]"))


class TestTableCitations:
    def test_missing_table_fails(self):
        out = run(sql='SELECT 1 FROM Orders o JOIN Customers c ON 1=1', citations=["Orders"])
        assert any("missing" in f.detail and "Customers" in f.detail for f in out)

    def test_extra_table_fails(self):
        out = run(citations=["Orders", "Products"])
        assert any("not referenced" in f.detail and "Products" in f.detail for f in out)

    def test_cte_alias_must_not_be_cited(self):
        out = run(sql='WITH t AS (SELECT OrderID FROM Orders) SELECT COUNT(*) FROM t',
                  citations=["Orders", "t"])
        assert any("not referenced" in f.detail for f in out)

    def test_cte_over_real_table_name_cites_nothing(self):
        out = run(sql='WITH Products AS (SELECT 1 AS x) SELECT COUNT(*) FROM Products',
                  citations=[])
        assert out == []

    def test_derived_table_inner_tables_must_be_cited(self):
        out = run(sql='SELECT COUNT(*) FROM (SELECT OrderID FROM Orders) z', citations=[])
        assert any("missing" in f.detail and "Orders" in f.detail for f in out)

    def test_rag_only_answer_must_cite_no_tables(self):
        out = run(sql="", citations=["Orders"], route="rag", rows=[])
        assert any("no SQL executed but tables cited" in f.detail for f in out)


class TestChunkCitations:
    def test_nonexistent_chunk_fails(self):
        out = run(citations=["Orders", "kpi_definitions::chunk99"])
        assert any("not in the corpus" in f.detail for f in out)

    def test_real_but_unseen_chunk_fails(self):
        """Citing a chunk the answer never looked at is an invented citation."""
        out = run(citations=["Orders", "campaign_memo::chunk0"],
                  seen_chunk_ids={"kpi_definitions::chunk3"})
        assert any("never passed into planning" in f.detail for f in out)

    def test_seen_chunk_passes(self):
        out = run(citations=["Orders", "kpi_definitions::chunk3"],
                  seen_chunk_ids={"kpi_definitions::chunk3"})
        assert out == []


class TestEvidence:
    def test_numeric_answer_with_no_rows_fails(self):
        assert "evidence" in checks(run(rows=[], format_hint="int"))

    def test_rag_route_is_exempt(self):
        """A policy lookup legitimately has no rows."""
        out = run(rows=[], format_hint="int", route="rag", sql="", citations=["product_policy::chunk1"])
        assert out == []

    def test_string_answer_with_no_rows_is_not_an_evidence_failure(self):
        out = run(rows=[], format_hint="str", final_answer="x", sql="", citations=[])
        assert "evidence" not in checks(out)


def test_split_citations():
    tables, chunks = split_citations(["Orders", "Order Details", "kpi_definitions::chunk1"])
    assert tables == ["Orders", "Order Details"]
    assert chunks == ["kpi_definitions::chunk1"]


def test_failures_are_independent():
    """Four broken things must produce four distinct checks, not stop at the first."""
    out = run(final_answer="x", format_hint="int", citations=["Products", "bad::chunk0"], rows=[])
    assert {"format", "citations.tables", "citations.chunks", "evidence"} <= checks(out)
