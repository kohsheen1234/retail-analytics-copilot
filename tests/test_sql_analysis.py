"""Citation-grade table extraction and the date lint.

Validation requires table citations to *exactly* cover the physical tables of the
executed SQL, so both false positives (a CTE, an alias) and false negatives (a comma
join, a quoted name) are scored defects. These are the cases
`sqlite_tool.tables_used` gets wrong.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.sql_analysis import (
    cte_names,
    has_top_level_order_by,
    physical_tables,
    tokenize,
    unwrapped_date_comparisons,
)

ROOT = Path(__file__).resolve().parents[1]

KNOWN = ["Categories", "CustomerCustomerDemo", "CustomerDemographics", "Customers",
         "Employees", "EmployeeTerritories", "Order Details", "Orders", "Products",
         "Regions", "Shippers", "Suppliers", "Territories"]


@pytest.mark.parametrize("sql,expected", [
    # plain
    ("SELECT COUNT(*) FROM Orders", ["Orders"]),
    # double-quoted name containing a space, with an alias
    ('SELECT SUM(od.Quantity) FROM "Order Details" od', ["Order Details"]),
    # bracket and backtick quoting
    ("SELECT * FROM [Order Details]", ["Order Details"]),
    ("SELECT * FROM `Order Details`", ["Order Details"]),
    # explicit AS alias must not be read as a table
    ("SELECT * FROM Orders AS o JOIN Customers AS c ON c.CustomerID=o.CustomerID",
     ["Customers", "Orders"]),
    # comma join
    ("SELECT * FROM Orders o, Customers cu WHERE cu.CustomerID=o.CustomerID",
     ["Customers", "Orders"]),
    # nested subquery in FROM
    ("SELECT * FROM (SELECT OrderID FROM Orders) x JOIN Products p ON 1=1",
     ["Orders", "Products"]),
    # subquery in WHERE
    ("SELECT * FROM Orders WHERE OrderID IN (SELECT OrderID FROM \"Order Details\")",
     ["Order Details", "Orders"]),
    # schema-qualified
    ("SELECT * FROM main.Orders", ["Orders"]),
    # LEFT/INNER/CROSS join variants
    ("SELECT * FROM Orders LEFT JOIN Employees ON 1=1 INNER JOIN Shippers ON 1=1",
     ["Employees", "Orders", "Shippers"]),
])
def test_physical_tables(sql, expected):
    assert physical_tables(sql, KNOWN) == expected


def test_cte_name_is_not_cited_as_a_table():
    """A CTE is not a physical table. The spec excludes CTE aliases explicitly."""
    sql = ('WITH monthly AS (SELECT OrderID FROM Orders) '
           'SELECT COUNT(*) FROM monthly')
    assert cte_names(tokenize(sql)) == {"monthly"}
    assert physical_tables(sql, KNOWN) == ["Orders"]


def test_cte_shadowing_a_real_table_name_is_still_not_a_table():
    """The hard case: a CTE deliberately named after a real table.

    A substring matcher reports Products twice over and cannot tell that the outer FROM
    refers to the CTE, not the table.
    """
    sql = ('WITH Products AS (SELECT 1 AS ProductID) '
           'SELECT COUNT(*) FROM Products')
    assert physical_tables(sql, KNOWN) == []


def test_multiple_ctes_and_recursive():
    sql = ("WITH RECURSIVE a AS (SELECT 1), b AS (SELECT OrderID FROM Orders) "
           "SELECT * FROM a JOIN b ON 1=1")
    assert cte_names(tokenize(sql)) == {"a", "b"}
    assert physical_tables(sql, KNOWN) == ["Orders"]


def test_table_name_used_as_a_column_alias_is_not_cited():
    """`AS Products` is an output column, not a referenced table."""
    sql = "SELECT CategoryName AS Products FROM Categories"
    assert physical_tables(sql, KNOWN) == ["Categories"]


def test_table_name_inside_a_string_literal_is_not_cited():
    sql = "SELECT COUNT(*) FROM Categories WHERE CategoryName = 'Products'"
    assert physical_tables(sql, KNOWN) == ["Categories"]


def test_table_name_in_a_comment_is_not_cited():
    sql = "SELECT COUNT(*) FROM Orders -- also joins Suppliers one day\n"
    assert physical_tables(sql, KNOWN) == ["Orders"]


def test_matches_every_provided_gold_tables():
    """23 free labelled cases: each provided gold SQL ships with its gold_tables."""
    checked = 0
    for name in ("train.jsonl", "dev.jsonl"):
        for line in (ROOT / "data" / name).read_text().splitlines():
            if not line.strip():
                continue
            ex = json.loads(line)
            if not ex["gold_sql"]:
                continue
            assert physical_tables(ex["gold_sql"], KNOWN) == sorted(ex["gold_tables"]), ex["id"]
            checked += 1
    assert checked == 23


@pytest.mark.parametrize("sql,ordered", [
    ("SELECT 1 FROM Orders", False),
    ("SELECT 1 FROM Orders ORDER BY OrderID", True),
    # ORDER BY only inside a subquery is not a top-level ordering
    ("SELECT * FROM (SELECT OrderID FROM Orders ORDER BY OrderID) x", False),
    ("SELECT * FROM (SELECT 1 ORDER BY 1) x ORDER BY 1", True),
])
def test_top_level_order_by(sql, ordered):
    assert has_top_level_order_by(sql) is ordered


class TestDateLint:
    """OrderDate holds mixed 'YYYY-MM-DD' and 'YYYY-MM-DD HH:MM:SS' values, so a bare
    comparison drops timestamped rows on a window's closing day."""

    def test_flags_bare_between(self):
        sql = "SELECT 1 FROM Orders WHERE OrderDate BETWEEN '2017-06-01' AND '2017-06-30'"
        assert unwrapped_date_comparisons(sql) == ["OrderDate"]

    def test_accepts_date_wrapped(self):
        sql = "SELECT 1 FROM Orders WHERE date(OrderDate) BETWEEN '2017-06-01' AND '2017-06-30'"
        assert unwrapped_date_comparisons(sql) == []

    def test_accepts_qualified_and_wrapped(self):
        sql = "SELECT 1 FROM Orders o WHERE date(o.OrderDate) BETWEEN '2017-06-01' AND '2017-06-30'"
        assert unwrapped_date_comparisons(sql) == []

    def test_flags_qualified_bare(self):
        sql = "SELECT 1 FROM Orders o WHERE o.OrderDate >= '2017-06-01'"
        assert unwrapped_date_comparisons(sql) == ["OrderDate"]

    def test_accepts_strftime(self):
        sql = "SELECT 1 FROM Orders WHERE strftime('%Y', OrderDate) = '2017'"
        assert unwrapped_date_comparisons(sql) == []

    def test_ignores_a_date_column_that_is_only_selected(self):
        sql = "SELECT OrderDate FROM Orders"
        assert unwrapped_date_comparisons(sql) == []

    def test_every_provided_gold_sql_is_lint_clean(self):
        """The gold SQL is the reference for correct date handling; if the lint fired on
        it, the lint would be wrong."""
        for name in ("train.jsonl", "dev.jsonl"):
            for line in (ROOT / "data" / name).read_text().splitlines():
                if not line.strip():
                    continue
                ex = json.loads(line)
                if ex["gold_sql"]:
                    assert unwrapped_date_comparisons(ex["gold_sql"]) == [], ex["id"]
