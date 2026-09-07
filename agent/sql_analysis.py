"""Static analysis of a SQL statement, for citations and for lint checks.

Why this exists rather than `sqlite_tool.tables_used`: validation requires the table
citations to *exactly* cover the physical tables of the executed SQL — an extra or a
missing table both fail. The starter helper substring-matches known table names anywhere
in the query text, so it cannot tell a physical table from a same-named CTE, and it
matches a table name used as a column alias. Its own docstring says "Verify, do not
trust blindly", so this module does the parsing properly:

  * literals and comments are stripped before anything is inspected;
  * `FROM` / `JOIN` targets are resolved at any nesting depth, including comma joins;
  * quoting styles are normalised (`"Order Details"`, `[Order Details]`, backticks);
  * table aliases are discarded;
  * CTE names defined by the same statement are subtracted;
  * `sqlite_*` internal tables are excluded.

Everything here is pure text analysis: no database connection, no LM.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Keywords that terminate a table reference in a FROM list.
_REF_STOP = {
    "where", "group", "order", "having", "limit", "offset", "union", "intersect",
    "except", "join", "inner", "left", "right", "full", "cross", "natural", "on",
    "using", "window", "returning",
}
# Words that may follow a table name but are not an alias.
_NOT_ALIAS = _REF_STOP | {"as", "select", "with", "recursive", "and", "or", "not"}

_COMMENTS = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)
_STRINGS = re.compile(r"'(?:[^']|'')*'")

_TOKEN = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<dq>"(?:[^"]|"")*")
    | (?P<bq>`(?:[^`]|``)*`)
    | (?P<br>\[[^\]]*\])
    | (?P<word>[A-Za-z_][A-Za-z_0-9$]*)
    | (?P<num>[0-9][0-9.eE+\-]*)
    | (?P<punc>.)
    """,
    re.X,
)


@dataclass(frozen=True)
class Token:
    kind: str
    text: str

    @property
    def word(self) -> str:
        """Lowercased bare word, or '' for anything that is not a bare word."""
        return self.text.lower() if self.kind == "word" else ""


def _unquote(text: str) -> str:
    if len(text) >= 2:
        if text[0] == '"' and text[-1] == '"':
            return text[1:-1].replace('""', '"')
        if text[0] == "`" and text[-1] == "`":
            return text[1:-1].replace("``", "`")
        if text[0] == "[" and text[-1] == "]":
            return text[1:-1]
    return text


def tokenize(sql: str) -> list[Token]:
    """Tokens with comments, string literals and whitespace removed."""
    scrubbed = _STRINGS.sub("''", _COMMENTS.sub(" ", sql))
    out: list[Token] = []
    for m in _TOKEN.finditer(scrubbed):
        kind = m.lastgroup or "punc"
        if kind == "ws":
            continue
        out.append(Token("ident" if kind in {"dq", "bq", "br"} else kind, m.group()))
    return out


def _skip_parens(toks: list[Token], i: int) -> int:
    """Given toks[i] == '(', return the index just past the matching ')'."""
    depth = 0
    while i < len(toks):
        t = toks[i].text
        if t == "(":
            depth += 1
        elif t == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return i


def cte_names(toks: list[Token]) -> set[str]:
    """Names bound by a top-level WITH clause. Case-folded."""
    if not toks:
        return set()
    i = 0
    if toks[i].word != "with":
        return set()
    i += 1
    if i < len(toks) and toks[i].word == "recursive":
        i += 1
    names: set[str] = set()
    while i < len(toks):
        if toks[i].kind not in {"word", "ident"}:
            break
        names.add(_unquote(toks[i].text).lower())
        i += 1
        if i < len(toks) and toks[i].text == "(":       # optional column list
            i = _skip_parens(toks, i)
        if i < len(toks) and toks[i].word == "as":
            i += 1
        if i < len(toks) and toks[i].word in {"materialized", "not"}:
            i += 1
            if i < len(toks) and toks[i].word == "materialized":
                i += 1
        if i < len(toks) and toks[i].text == "(":
            i = _skip_parens(toks, i)
        if i < len(toks) and toks[i].text == ",":
            i += 1
            continue
        break
    return names


def _read_ref(toks: list[Token], i: int, found: list[str]) -> int:
    """Read one table reference at toks[i]; append its name if it is an identifier."""
    if i >= len(toks):
        return i
    if toks[i].text == "(":
        # Descend, do not skip. A derived table or parenthesised join still references
        # real tables inside it: in `FROM (SELECT OrderID FROM Orders) x JOIN Products p`
        # jumping over the parens loses Orders, which is a missing citation and a
        # validation failure. Returning i+1 lets the outer scanner walk the inner
        # FROM/JOIN keywords as normal.
        return i + 1
    if toks[i].kind not in {"word", "ident"}:
        return i + 1
    if toks[i].word in _REF_STOP:               # e.g. FROM ... immediately followed by JOIN
        return i
    name = _unquote(toks[i].text)
    i += 1
    # schema-qualified: main.Orders  ->  keep the last part
    while i + 1 < len(toks) and toks[i].text == "." and toks[i + 1].kind in {"word", "ident"}:
        name = _unquote(toks[i + 1].text)
        i += 2
    if i < len(toks) and toks[i].text == "(":   # table-valued function, not a table
        return _skip_parens(toks, i)
    found.append(name)
    if i < len(toks) and toks[i].word == "as":  # alias
        i += 1
        if i < len(toks) and toks[i].kind in {"word", "ident"}:
            i += 1
    elif i < len(toks) and toks[i].kind in {"word", "ident"} and toks[i].word not in _NOT_ALIAS:
        i += 1
    return i


def referenced_names(sql: str) -> list[str]:
    """Every name appearing in a FROM or JOIN position, CTE names not yet removed."""
    toks = tokenize(sql)
    found: list[str] = []
    i = 0
    while i < len(toks):
        w = toks[i].word
        if w == "from":
            i = _read_ref(toks, i + 1, found)
            while i < len(toks) and toks[i].text == ",":
                i = _read_ref(toks, i + 1, found)
        elif w == "join":
            i = _read_ref(toks, i + 1, found)
        else:
            i += 1
    return found


def physical_tables(sql: str, known_tables: list[str] | set[str]) -> list[str]:
    """Physical tables referenced by `sql`, in the spelling used by the schema.

    Names not present in `known_tables` are dropped: they are CTEs, aliases, or a
    hallucinated table. A hallucinated table is caught separately by execution failing,
    which is the signal we want to keep rather than mask here.
    """
    by_fold = {t.lower(): t for t in known_tables}
    ctes = cte_names(tokenize(sql))
    out: list[str] = []
    for name in referenced_names(sql):
        fold = name.lower()
        if fold in ctes or fold.startswith("sqlite_"):
            continue
        real = by_fold.get(fold)
        if real is not None and real not in out:
            out.append(real)
    return sorted(out)


def has_top_level_order_by(sql: str) -> bool:
    """True if the statement has an ORDER BY outside any parentheses.

    Used to infer the `ordered` flag for gold examples that omit it (see DECISIONS.md).
    """
    toks = tokenize(sql)
    depth = 0
    for i, t in enumerate(toks):
        if t.text == "(":
            depth += 1
        elif t.text == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and t.word == "order" and i + 1 < len(toks) and toks[i + 1].word == "by":
            return True
    return False


# --- Date handling lint -------------------------------------------------------
# `OrderDate` holds mixed 'YYYY-MM-DD' and 'YYYY-MM-DD HH:MM:SS' values, so a bare
# lexicographic comparison silently drops timestamped rows on the closing day of a
# window. See DECISIONS.md and tests/test_sql_dates.py.

_NORMALISERS = ("date", "datetime", "julianday", "strftime", "substr", "cast", "unixepoch")
_WRAPPED = re.compile(
    r"(?:" + "|".join(_NORMALISERS) + r")\s*\([^()]*$", re.I)
_COMPARISON = re.compile(r"^\s*(between\b|>=|<=|<>|!=|=|<|>)", re.I)
_DATE_COL = re.compile(r'(?:(?:[A-Za-z_]\w*|"[^"]+"|\[[^\]]+\])\s*\.\s*)?'
                       r'(?:([A-Za-z_]\w*[Dd]ate)|"([^"]*[Dd]ate)"|\[([^\]]*[Dd]ate)\])')


def unwrapped_date_comparisons(sql: str) -> list[str]:
    """Date-ish columns compared to something without a normalising function.

    Heuristic and deliberately advisory: it drives a repair attempt and a trace note, it
    never rewrites the model's SQL. False negatives are possible (a normaliser applied to
    the literal side rather than the column side); false positives are possible on a
    column merely *named* like a date but stored as a number.
    """
    body = _STRINGS.sub("''", _COMMENTS.sub(" ", sql))
    hits: list[str] = []
    for m in _DATE_COL.finditer(body):
        col = m.group(1) or m.group(2) or m.group(3)
        if _WRAPPED.search(body[: m.start()]):
            continue
        if not _COMPARISON.match(body[m.end():]):
            continue
        if col not in hits:
            hits.append(col)
    return hits
