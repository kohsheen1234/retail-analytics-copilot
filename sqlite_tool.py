"""Read-only SQLite execution boundary and schema introspection.

Guarantees: read-only URI, PRAGMA query_only, exactly one statement whose
top-level statement is SELECT (a leading WITH is allowed only when it ends in
SELECT), no PRAGMA/ATTACH/EXPLAIN/DDL/DML/transaction commands, row limit,
wall-clock timeout. Extend it if you
want; do not weaken it. Your tests must cover the rejection paths.
"""
from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

_LEADING_COMMENTS = re.compile(r"^(\s*(--[^\n]*\n|/\*.*?\*/))*\s*", re.S)


class UnsafeSQL(ValueError):
    pass


@dataclass
class ExecResult:
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    error: str | None = None
    elapsed_ms: int = 0


_FORBIDDEN_ANYWHERE = re.compile(
    r"\b(pragma|attach|detach|explain|vacuum|reindex|create|drop|alter|begin|commit|rollback|savepoint|release)\b", re.I)
_WRITE_VERBS = {"insert", "update", "delete", "replace"}


def _depth0_keywords(sql_no_literals: str) -> list[str]:
    """Statement-level keywords that appear outside any parentheses (CTE bodies and subqueries are nested)."""
    depth, out = 0, []
    for tok in re.findall(r"\(|\)|[A-Za-z_]+", sql_no_literals):
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(tok.lower())
    return out


def normalize_statement(sql: str) -> str:
    body = _LEADING_COMMENTS.sub("", sql).strip()
    if body.endswith(";"):
        body = body[:-1].rstrip()
    if not body:
        raise UnsafeSQL("empty statement")
    if not re.match(r"^(select|with)\b", body, re.I):
        raise UnsafeSQL("only a single SELECT (optionally prefixed by WITH) is allowed")
    if not sqlite3.complete_statement(body + ";"):
        raise UnsafeSQL("incomplete statement")
    bare = _strip_literals(body)
    if ";" in bare:
        raise UnsafeSQL("multiple statements are not allowed")
    if _FORBIDDEN_ANYWHERE.search(bare):
        raise UnsafeSQL("statement contains a forbidden command")
    top = _depth0_keywords(bare)
    if any(k in _WRITE_VERBS for k in top):
        raise UnsafeSQL("the terminal statement must be SELECT, not a write")
    if "select" not in top:
        raise UnsafeSQL("no top-level SELECT found")
    return body


def _strip_literals(sql: str) -> str:
    return re.sub(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"", "", sql)


class SQLiteTool:
    def __init__(self, db_path: str | Path, row_limit: int = 1000, timeout_s: float = 10.0):
        self.db_path = Path(db_path)
        self.row_limit = row_limit
        self.timeout_s = timeout_s

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only = 1")
        return conn

    def execute(self, sql: str) -> ExecResult:
        start = time.perf_counter()
        try:
            stmt = normalize_statement(sql)
        except UnsafeSQL as e:
            return ExecResult(error=f"unsafe: {e}")
        conn = self._connect()
        deadline = start + self.timeout_s
        conn.set_progress_handler(lambda: 1 if time.perf_counter() > deadline else 0, 1000)
        try:
            cur = conn.execute(stmt)
            columns = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchmany(self.row_limit + 1)
            truncated = len(rows) > self.row_limit
            rows = rows[: self.row_limit]
            return ExecResult(columns=columns, rows=rows, row_count=len(rows), truncated=truncated,
                              elapsed_ms=int((time.perf_counter() - start) * 1000))
        except sqlite3.OperationalError as e:
            msg = "timeout" if "interrupted" in str(e).lower() else str(e)
            return ExecResult(error=msg, elapsed_ms=int((time.perf_counter() - start) * 1000))
        except sqlite3.DatabaseError as e:
            return ExecResult(error=str(e), elapsed_ms=int((time.perf_counter() - start) * 1000))
        finally:
            conn.close()

    def schema(self) -> dict[str, list[dict]]:
        """Live schema via PRAGMA. Returns {table: [{name, type, pk}, ...]}."""
        conn = self._connect()
        try:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            return {
                t: [{"name": r[1], "type": r[2], "pk": bool(r[5])} for r in conn.execute(f'PRAGMA table_info("{t}")')]
                for t in tables
            }
        finally:
            conn.close()

    def tables_used(self, sql: str) -> list[str]:
        """Best-effort list of known tables referenced in sql, for citations. Verify, do not trust blindly."""
        known = self.schema().keys()
        lowered = sql.lower()
        return [t for t in known if re.search(r'(?<![\w"])"?' + re.escape(t.lower()) + r'"?(?![\w"])', lowered)]
