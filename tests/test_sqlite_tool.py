from sqlite_tool import SQLiteTool


def test_rejects_non_select(db_path):
    t = SQLiteTool(db_path)
    for sql in ["DELETE FROM Orders", "UPDATE Orders SET Freight=0", "DROP TABLE Orders",
                "PRAGMA query_only=0", "INSERT INTO Shippers VALUES (9,'x','y')"]:
        r = t.execute(sql)
        assert r.error and r.error.startswith("unsafe"), sql


def test_rejects_multi_statement(db_path):
    r = SQLiteTool(db_path).execute("SELECT 1; DELETE FROM Orders")
    assert r.error and "multiple" in r.error


def test_read_only_even_if_guard_bypassed(db_path):
    # Belt and braces: the connection itself is read-only.
    t = SQLiteTool(db_path)
    conn = t._connect()
    try:
        import sqlite3
        try:
            conn.execute("DELETE FROM Shippers")
            assert False, "write succeeded on read-only connection"
        except sqlite3.OperationalError:
            pass
    finally:
        conn.close()


def test_row_limit(db_path):
    r = SQLiteTool(db_path, row_limit=5).execute("SELECT OrderID FROM Orders")
    assert r.row_count == 5 and r.truncated


def test_schema_has_order_details(db_path):
    s = SQLiteTool(db_path).schema()
    assert "Order Details" in s and any(c["name"] == "Discount" for c in s["Order Details"])


def test_date_boundary_includes_datetime_rows_on_last_day(db_path):
    """Regression for the mixed OrderDate formats. Naive BETWEEN drops datetime rows on the last day."""
    t = SQLiteTool(db_path)
    naive = t.execute("SELECT COUNT(*) FROM Orders WHERE OrderDate BETWEEN '2017-06-01' AND '2017-06-30'").rows[0][0]
    correct = t.execute("SELECT COUNT(*) FROM Orders WHERE date(OrderDate) BETWEEN '2017-06-01' AND '2017-06-30'").rows[0][0]
    assert correct > naive


def test_with_must_terminate_in_select(db_path):
    t = SQLiteTool(db_path)
    ok = t.execute("WITH x AS (SELECT ShipperID FROM Shippers) SELECT COUNT(*) FROM x")
    assert ok.error is None and ok.rows[0][0] == 3
    bad = t.execute("WITH x AS (SELECT 1) DELETE FROM Shippers WHERE ShipperID IN (SELECT * FROM x)")
    assert bad.error and bad.error.startswith("unsafe")
    bad2 = t.execute("WITH x AS (SELECT 1) INSERT INTO Shippers(CompanyName) SELECT 'z' FROM x")
    assert bad2.error and bad2.error.startswith("unsafe")


def test_rejects_explain_and_attach(db_path):
    t = SQLiteTool(db_path)
    for sql in ["EXPLAIN SELECT 1", "SELECT 1 FROM Orders WHERE 1=1 ATTACH DATABASE 'x' AS y", "WITH x AS (PRAGMA table_info(Orders)) SELECT 1"]:
        r = t.execute(sql)
        assert r.error and r.error.startswith("unsafe"), sql
