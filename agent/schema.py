"""Live schema rendering from PRAGMA, compact enough to fit the pinned context.

The assessment forbids hard-coded schema strings, so everything here is derived from
`PRAGMA table_info` / `PRAGMA foreign_key_list` at runtime. Two consequences shaped the
design:

* `num_ctx` is 4096 and may not be raised. A naive dump of 13 tables and ~110 columns
  with full type names costs well over 1000 tokens before a single demo is added, and
  demos are what actually teach the model this database's conventions. So types are
  abbreviated and foreign keys are rendered as a separate compact edge list rather than
  repeated per column.
* Foreign keys matter more than types for this task. Almost every question is a join,
  and the model's most damaging failure is joining on the wrong column, so the edge list
  is worth its tokens.

Empty tables are marked. `CustomerDemographics` and `CustomerCustomerDemo` hold 0 rows in
this database, and a model that joins through them produces a technically valid query
that returns nothing; saying so in the schema is cheaper than discovering it via a repair.
"""
from __future__ import annotations

from functools import lru_cache

from sqlite_tool import SQLiteTool

_TYPE_ABBREV = {
    "INTEGER": "int", "INT": "int", "TEXT": "txt", "NUMERIC": "num", "REAL": "real",
    "DATETIME": "date", "BLOB": "blob", "": "?",
}


def _abbrev(sql_type: str) -> str:
    t = (sql_type or "").upper().strip()
    return _TYPE_ABBREV.get(t, t.lower()[:6] or "?")


def quote(name: str) -> str:
    """Quote an identifier only when it needs it, matching the gold SQL's style."""
    return f'"{name}"' if not name.isidentifier() else name


def foreign_keys(tool: SQLiteTool) -> list[tuple[str, str, str, str]]:
    """(from_table, from_col, to_table, to_col) via PRAGMA foreign_key_list."""
    conn = tool._connect()
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        out: list[tuple[str, str, str, str]] = []
        for t in tables:
            for row in conn.execute(f'PRAGMA foreign_key_list("{t}")'):
                # row: id, seq, table, from, to, on_update, on_delete, match
                out.append((t, row[3], row[2], row[4]))
        return out
    finally:
        conn.close()


def row_counts(tool: SQLiteTool) -> dict[str, int]:
    conn = tool._connect()
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
    finally:
        conn.close()


@lru_cache(maxsize=4)
def _render(db_path: str) -> str:
    tool = SQLiteTool(db_path)
    schema = tool.schema()
    counts = row_counts(tool)
    fks = foreign_keys(tool)

    lines: list[str] = ["TABLES (col:type, * = primary key):"]
    for table, cols in schema.items():
        rendered = ", ".join(
            f"{c['name']}:{_abbrev(c['type'])}{'*' if c['pk'] else ''}" for c in cols)
        note = "  [EMPTY TABLE - 0 rows]" if counts.get(table) == 0 else ""
        lines.append(f"  {quote(table)}({rendered}){note}")

    if fks:
        lines.append("JOINS (foreign keys):")
        for src, src_col, dst, dst_col in fks:
            lines.append(f"  {quote(src)}.{src_col} -> {quote(dst)}.{dst_col}")
    return "\n".join(lines)


def schema_text(db_path: str | None = None) -> str:
    """Compact live schema. Cached per database path; PRAGMA is read once."""
    from agent import config
    return _render(str(db_path or config.DB_PATH))


def known_tables(db_path: str | None = None) -> list[str]:
    from agent import config
    return sorted(SQLiteTool(str(db_path or config.DB_PATH)).schema().keys())


def all_columns(db_path: str | None = None) -> set[str]:
    from agent import config
    schema = SQLiteTool(str(db_path or config.DB_PATH)).schema()
    return {c["name"] for cols in schema.values() for c in cols}


def column_owners(db_path: str | None = None) -> dict[str, list[str]]:
    """column name -> tables that have it. Derived from PRAGMA, never hardcoded."""
    from agent import config
    schema = SQLiteTool(str(db_path or config.DB_PATH)).schema()
    out: dict[str, list[str]] = {}
    for table, cols in schema.items():
        for c in cols:
            out.setdefault(c["name"], []).append(table)
    return out


def resolve_columns(identifiers: list[str], db_path: str | None = None) -> list[str]:
    """Lines saying which table owns each identifier, for identifiers we can resolve.

    Added after measuring the dominant NL-to-SQL failure: 5 of 12 dev failures were
    `no such column: o.Discount`. `Discount` exists on exactly one table, so the model was
    not inventing a column - it was attaching a real column to the wrong alias, which is a
    resolvable fact rather than a reasoning problem. `UnitPrice` genuinely lives on two
    tables, and the KPI docs say which one revenue uses, so that ambiguity is called out
    explicitly rather than left to the model.
    """
    owners = column_owners(db_path)
    lines: list[str] = []
    for name in identifiers:
        tables = owners.get(name)
        if not tables:
            continue
        if len(tables) == 1:
            lines.append(f"{name} is a column of {quote(tables[0])} only")
        else:
            lines.append(f"{name} exists on {', '.join(quote(t) for t in tables)} "
                         f"- qualify it explicitly")
    return lines
