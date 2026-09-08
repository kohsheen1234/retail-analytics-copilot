"""Build data/train_added.jsonl and data/dev_added.jsonl.

Gold answers are produced by executing gold SQL I wrote, through the same read-only
boundary the agent uses. Nothing here is hand-typed from memory.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent.schema import known_tables
from agent.sql_analysis import physical_tables, has_top_level_order_by
from sqlite_tool import SQLiteTool

DB = "data/northwind.sqlite"
TOOL = SQLiteTool(DB, row_limit=1000)
TABLES = known_tables(DB)

REV = "od.UnitPrice*od.Quantity*(1-od.Discount)"

TRAIN = [
    dict(
        id="train_added_boundary_last_day_of_window",
        question="How many orders were placed on 2017-06-30, the last day of the "
                 "'Summer Beverages 2017' window in the marketing calendar? Return an integer.",
        format_hint="int", route="hybrid", ordered=False,
        stresses="date boundaries",
        gold_sql="SELECT COUNT(*) FROM Orders o WHERE date(o.OrderDate) = '2017-06-30'",
        gold_chunks=["marketing_calendar::chunk1"],
    ),
    dict(
        id="train_added_pantry_reporting_group_qty",
        question="Total quantity sold all-time for the 'Pantry' reporting group as defined in "
                 "the catalog. Return an integer.",
        format_hint="int", route="hybrid", ordered=False,
        stresses="reporting groups",
        gold_sql=f'SELECT SUM(od.Quantity) FROM "Order Details" od '
                 f'JOIN Products p ON p.ProductID=od.ProductID '
                 f'JOIN Categories c ON c.CategoryID=p.CategoryID '
                 f"WHERE c.CategoryName IN ('Grains/Cereals','Produce')",
        gold_chunks=["catalog::chunk1"],
    ),
    dict(
        id="train_added_grain_top_customer_account_2021",
        question="Which customer account generated the highest revenue in 2021? Group by the "
                 "customer account, not by company name. Return {customer:str, revenue:float}.",
        format_hint="{customer:str, revenue:float}", route="hybrid", ordered=True,
        stresses="aggregation grain, duplicate labels",
        gold_sql=f'SELECT cu.CompanyName, ROUND(SUM({REV}),2) r FROM "Order Details" od '
                 f'JOIN Orders o ON o.OrderID=od.OrderID '
                 f'JOIN Customers cu ON cu.CustomerID=o.CustomerID '
                 f"WHERE date(o.OrderDate) BETWEEN '2021-01-01' AND '2021-12-31' "
                 f'GROUP BY cu.CustomerID ORDER BY r DESC LIMIT 1',
        gold_chunks=["kpi_definitions::chunk3"],
    ),
    dict(
        id="train_added_null_unshipped_orders_2018",
        question="How many orders placed in 2018 have no shipped date recorded? Return an integer.",
        format_hint="int", route="sql", ordered=False,
        stresses="NULL handling",
        gold_sql="SELECT COUNT(*) FROM Orders o WHERE o.ShippedDate IS NULL "
                 "AND date(o.OrderDate) BETWEEN '2018-01-01' AND '2018-12-31'",
        gold_chunks=[],
    ),
    dict(
        id="train_added_ordered_top3_categories_qty_2019",
        question="Top 3 categories by total quantity sold in 2019, highest first. "
                 "Return list[{category:str, quantity:int}].",
        format_hint="list[{category:str, quantity:int}]", route="sql", ordered=True,
        stresses="ordered output",
        gold_sql='SELECT c.CategoryName, SUM(od.Quantity) q FROM "Order Details" od '
                 'JOIN Orders o ON o.OrderID=od.OrderID '
                 'JOIN Products p ON p.ProductID=od.ProductID '
                 'JOIN Categories c ON c.CategoryID=p.CategoryID '
                 "WHERE date(o.OrderDate) BETWEEN '2019-01-01' AND '2019-12-31' "
                 'GROUP BY c.CategoryID ORDER BY q DESC LIMIT 3',
        gold_chunks=[],
    ),
]

DEV = [
    dict(
        id="dev_added_boundary_inclusive_june_2017_qty",
        question="Total quantity sold on orders placed between 2017-06-01 and 2017-06-30 "
                 "inclusive. Return an integer.",
        format_hint="int", route="sql", ordered=False,
        stresses="date boundaries",
        gold_sql='SELECT SUM(od.Quantity) FROM "Order Details" od '
                 'JOIN Orders o ON o.OrderID=od.OrderID '
                 "WHERE date(o.OrderDate) BETWEEN '2017-06-01' AND '2017-06-30'",
        gold_chunks=[],
    ),
    dict(
        id="dev_added_reporting_group_count",
        question="How many distinct reporting groups are there, after applying the catalog's "
                 "reporting group rules to the product categories? Return an integer.",
        format_hint="int", route="hybrid", ordered=False,
        stresses="reporting groups",
        gold_sql="SELECT COUNT(DISTINCT CASE WHEN c.CategoryName IN ('Grains/Cereals','Produce') "
                 "THEN 'Pantry' ELSE c.CategoryName END) FROM Categories c",
        gold_chunks=["catalog::chunk1"],
    ),
    dict(
        id="dev_added_tie_categories_with_12_products",
        question="List every category that has exactly 12 products, ordered alphabetically by "
                 "category name. Return list[{category:str, products:int}].",
        format_hint="list[{category:str, products:int}]", route="sql", ordered=True,
        stresses="tie handling, ordered output",
        gold_sql="SELECT c.CategoryName, COUNT(*) n FROM Products p "
                 "JOIN Categories c ON c.CategoryID=p.CategoryID "
                 "GROUP BY c.CategoryID HAVING n = 12 ORDER BY c.CategoryName ASC",
        gold_chunks=[],
    ),
    dict(
        id="dev_added_grain_lines_vs_orders_2020",
        question="For orders placed in 2020, how many order lines were there and how many "
                 "distinct orders? Return {lines:int, orders:int}.",
        format_hint="{lines:int, orders:int}", route="sql", ordered=False,
        stresses="aggregation grain",
        gold_sql='SELECT COUNT(*) lines, COUNT(DISTINCT od.OrderID) orders '
                 'FROM "Order Details" od JOIN Orders o ON o.OrderID=od.OrderID '
                 "WHERE date(o.OrderDate) BETWEEN '2020-01-01' AND '2020-12-31'",
        gold_chunks=[],
    ),
    dict(
        id="dev_added_legacy_aov_2014",
        question="Using the legacy AOV definition from the KPI docs, what was the Average Order "
                 "Value for calendar year 2014? Return a float rounded to 2 decimals.",
        format_hint="float", route="hybrid", ordered=False,
        stresses="retired KPI definition applied to pre-2016 data",
        gold_sql='SELECT ROUND(SUM(od.UnitPrice*od.Quantity)*1.0/COUNT(DISTINCT o.OrderID),2) '
                 'FROM "Order Details" od JOIN Orders o ON o.OrderID=od.OrderID '
                 "WHERE date(o.OrderDate) BETWEEN '2014-01-01' AND '2014-12-31'",
        gold_chunks=["kpi_definitions::chunk1"],
    ),
]


def shape(format_hint: str, columns: list[str], rows: list[tuple]):
    """Turn executed rows into a gold_answer matching format_hint."""
    hint = format_hint.strip()
    if hint in {"int", "float", "str"}:
        v = rows[0][0]
        return int(v) if hint == "int" else (round(float(v), 2) if hint == "float" else str(v))
    if hint.startswith("list[{"):
        fields = [f.strip().split(":") for f in hint[len("list[{"):-2].split(",")]
        return [
            {n.strip(): (int(v) if t.strip() == "int" else round(float(v), 2) if t.strip() == "float" else str(v))
             for (n, t), v in zip(fields, row)}
            for row in rows
        ]
    fields = [f.strip().split(":") for f in hint[1:-1].split(",")]
    return {n.strip(): (int(v) if t.strip() == "int" else round(float(v), 2) if t.strip() == "float" else str(v))
            for (n, t), v in zip(fields, rows[0])}


def build(spec: dict) -> dict:
    res = TOOL.execute(spec["gold_sql"])
    assert res.error is None, (spec["id"], res.error)
    assert res.rows, (spec["id"], "gold SQL returned no rows")
    inferred = has_top_level_order_by(spec["gold_sql"])
    rec = {
        "id": spec["id"],
        "question": spec["question"],
        "format_hint": spec["format_hint"],
        "route": spec["route"],
        "ordered": spec["ordered"],
        "gold_sql": spec["gold_sql"],
        "gold_answer": shape(spec["format_hint"], res.columns, res.rows),
        "gold_tables": physical_tables(spec["gold_sql"], TABLES),
        "gold_chunks": spec["gold_chunks"],
        "added_by": "candidate",
        "stresses": spec["stresses"],
    }
    print(f"  {rec['id']:48s} ordered={spec['ordered']!s:5s} (inferred {inferred!s:5s}) "
          f"rows={res.row_count} tables={rec['gold_tables']}")
    print(f"      -> {json.dumps(rec['gold_answer'])[:130]}")
    return rec


if __name__ == "__main__":
    print("TRAIN (added):")
    train = [build(s) for s in TRAIN]
    print("DEV (added):")
    dev = [build(s) for s in DEV]

    Path("data/train_added.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in train), encoding="utf-8")
    Path("data/dev_added.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in dev), encoding="utf-8")
    print(f"\nwrote {len(train)} train + {len(dev)} dev added examples")
