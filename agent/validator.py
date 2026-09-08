"""Validation: the four checks the assessment specifies, each able to fail independently.

  1. `final_answer` matches `format_hint` exactly (via the starter's `check_format`).
  2. Table citations *exactly* cover the physical tables of the final executed SQL --
     CTE aliases and `sqlite_*` excluded, and both extra and missing tables fail.
  3. Every cited chunk id exists in the corpus **and** was actually passed into planning
     or synthesis. Existence alone is not enough: citing a real chunk the answer never
     saw is an invented citation.
  4. Non-empty rows back a numeric answer.

Returns a list of failures rather than raising, so the repair loop can act on the
specific failure and the trace can record all of them at once.
"""
from __future__ import annotations

from dataclasses import dataclass

from agent.answer import validate_shape
from agent.sql_analysis import physical_tables

NUMERIC_HINTS = {"int", "float"}


@dataclass(frozen=True)
class Failure:
    check: str
    detail: str

    def __str__(self) -> str:
        return f"{self.check}: {self.detail}"


def split_citations(citations: list[str]) -> tuple[list[str], list[str]]:
    """(table citations, chunk citations). A chunk id is the thing containing '::'."""
    chunks = [c for c in citations if "::" in c]
    tables = [c for c in citations if "::" not in c]
    return tables, chunks


def validate(*, final_answer, format_hint: str, sql: str, citations: list[str],
             columns: list[str], rows: list[tuple], known_tables: list[str],
             corpus_chunk_ids: set[str], seen_chunk_ids: set[str],
             route: str) -> list[Failure]:
    failures: list[Failure] = []
    tables, chunks = split_citations(citations)

    # 1. type
    if not validate_shape(final_answer, format_hint):
        failures.append(Failure("format", f"{final_answer!r} does not match {format_hint!r}"))

    # 2. table citations exactly cover the executed SQL
    if sql.strip():
        expected = set(physical_tables(sql, known_tables))
        got = set(tables)
        if missing := expected - got:
            failures.append(Failure("citations.tables", f"missing {sorted(missing)}"))
        if extra := got - expected:
            failures.append(Failure("citations.tables", f"not referenced by the SQL: {sorted(extra)}"))
    elif tables:
        failures.append(Failure("citations.tables",
                                f"no SQL executed but tables cited: {sorted(tables)}"))

    # 3. chunk citations must exist and must have been seen
    for cid in chunks:
        if cid not in corpus_chunk_ids:
            failures.append(Failure("citations.chunks", f"{cid} is not in the corpus"))
        elif cid not in seen_chunk_ids:
            failures.append(Failure("citations.chunks",
                                    f"{cid} was never passed into planning or synthesis"))

    # 4. a numeric answer needs rows behind it
    if format_hint.strip() in NUMERIC_HINTS and route != "rag" and not rows:
        failures.append(Failure("evidence", "numeric answer with no rows returned"))

    return failures
